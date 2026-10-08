"""RunPod serverless handler for the MiniMax-H3 ComfyUI stack.

Boots ComfyUI once per worker, then each job picks a workflow template by name,
patches its widgets (prompt / camera_tag / duration / unet config), uploads any
input media, queues the prompt, and returns the produced media as base64.

Job input:
  {
    "workflow": "MiniMaxH3-Turbo-R2VA-GGUF.json",   # file in /opt/workflows
    "prompt":   "...",                             # enhancer/directive text
    "config":   "native|native_turbo|r2va_native|r2va_native_turbo|10eros|10eros_turbo",
    "camera_tag": "[ORBIT]",                       # optional
    "seconds":  6,                                 # optional duration override
    "images":   ["https://... or base64"],         # optional, fills LoadImage nodes in id order
    "video":    "https://... or base64"            # optional, fills VHS_LoadVideo
  }
"""

import asyncio
import base64
import glob
import json
import os
import random
import re
import subprocess
import threading
import time
import urllib.request
import uuid
from pathlib import Path

import runpod

COMFY_DIR = os.environ.get("COMFYUI_DIR", "/workspace/runpod-slim/ComfyUI")
COMFY_URL = "http://127.0.0.1:8188"
WORKFLOWS_DIR = Path(os.environ.get("WORKFLOWS_DIR", "/opt/workflows"))
JOB_TIMEOUT_S = int(os.environ.get("JOB_TIMEOUT_S", "1800"))

# Config → unet filename needle + sampler values (mirrors chat_service.py _MINIMAX_CONFIGS)
MINIMAX_CONFIGS = {
    "native":            {"unet": "fl2va",  "steps": 20, "sampler": "res_multistep", "scheduler": "simple", "shift_video": 12.0, "shift_audio": 3.0, "lora": None, "tau": None},
    "native_turbo":      {"unet": "fl2va",  "steps": 8,  "sampler": "euler",         "scheduler": "simple", "shift_video": 6.0,  "shift_audio": 3.0, "lora": "fl2v_turbo",  "tau": 1.3},
    "r2va_singularity":       {"unet": "Singularity", "steps": 20, "sampler": "res_multistep", "scheduler": "simple", "lora": None, "tau": None, "sigma_off": True},
    "r2va_singularity_turbo": {"unet": "Singularity", "steps": 6,  "sampler": "euler",         "scheduler": "beta",   "lora": "ref2v_turbo_4step", "tau": 1.3, "sigma_off": True},
    "10eros":            {"unet": "10eros", "steps": 20, "sampler": "res_multistep", "scheduler": "simple", "shift_video": 12.0, "shift_audio": 3.0, "lora": None, "tau": None},
    "10eros_turbo":      {"unet": "10eros", "steps": 8,  "sampler": "euler",         "scheduler": "simple", "shift_video": 6.0,  "shift_audio": 3.0, "lora": "fusion_turbo", "tau": 1.3},
}


def _http(path, payload=None, timeout=30):
    url = COMFY_URL + path
    try:
        if payload is None:
            return urllib.request.urlopen(url, timeout=timeout).read()
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=timeout).read()
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:2000]
        raise RuntimeError(f"ComfyUI {path} → HTTP {e.code}: {body}") from e


_BOOT_DONE = threading.Event()
_BOOT_ERR = None


def _boot_comfy():
    # Extra flags come from COMFYUI_EXTRA_ARGS (space-separated). Keep the
    # default empty: --fast/--use-sage-attention/--async-offload corrupt the
    # int8-convrot Qwen/MMh3 stacks (flat garbage output).
    cmd = [
        "python3", "main.py",
        "--listen", "127.0.0.1", "--port", "8188",
        "--disable-auto-launch",
    ] + os.environ.get("COMFYUI_EXTRA_ARGS", "").split()
    comfy_log = open("/workspace/comfy.log", "ab", buffering=0)
    proc = subprocess.Popen(cmd, cwd=COMFY_DIR, stdout=comfy_log, stderr=subprocess.STDOUT)
    deadline = time.time() + 600
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("ComfyUI exited during boot")
        try:
            _http("/system_stats", timeout=5)
            return proc
        except Exception:
            time.sleep(2)
    raise RuntimeError("ComfyUI did not become ready in 600s")


def _boot_in_bg():
    # ComfyUI boots on a daemon thread so the runpod handler is live (and the
    # `health` action answers) even while ComfyUI is still starting — or on
    # machines where it can't start at all (e.g. Hub test pods without the
    # network volume). Real jobs wait on _BOOT_DONE via _ensure_comfy().
    global _BOOT_ERR
    try:
        _boot_comfy()
    except Exception as exc:
        _BOOT_ERR = exc
        print(f"[handler] ComfyUI boot failed: {exc}", flush=True)
    finally:
        _BOOT_DONE.set()


async def _ensure_comfy():
    await asyncio.to_thread(_BOOT_DONE.wait)
    if _BOOT_ERR is not None:
        raise RuntimeError(f"ComfyUI failed to boot: {_BOOT_ERR} (see /workspace/comfy.log)")


def _media_bytes(value):
    if isinstance(value, str) and value.startswith("http"):
        return urllib.request.urlopen(value, timeout=120).read()
    return base64.b64decode(value)


def _upload(filename, data):
    boundary = uuid.uuid4().hex
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{filename}\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        COMFY_URL + "/upload/image", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    resp = json.loads(urllib.request.urlopen(req, timeout=300).read())
    return resp.get("name", filename)


def _load_workflow(name):
    """Workflow templates must be saved in ComfyUI *API format* (Export API),
    where every node is keyed by id and inputs carry widget names — the UI
    format's positional widgets_values can't be patched reliably."""
    path = WORKFLOWS_DIR / name
    if not path.is_file():
        path = WORKFLOWS_DIR / f"{name}.json"
    if not path.is_file():
        raise RuntimeError(f"workflow template not found: {name} (available: {sorted(p.name for p in WORKFLOWS_DIR.glob('*.json'))})")
    wf = json.loads(path.read_text())
    if "nodes" in wf:
        raise RuntimeError(f"{name} is a UI-format graph; re-export it with 'Export API' so widgets are named")
    return wf


def _patch(prompt, job):
    cfg = MINIMAX_CONFIGS.get(job.get("config"))
    base = re.sub(r"[^\w.-]+", "-", str(job.get("job_name") or f"job_{uuid.uuid4().hex[:8]}")).strip("-")
    images = [_upload(f"{base}_{i}.png", _media_bytes(v)) for i, v in enumerate(job.get("images") or [])]
    video_name = None
    if job.get("video"):
        video_name = _upload(f"{base}.mp4", _media_bytes(job["video"]))

    def _find_file(kind, needle):
        """Locate a model file under the volume + local model dirs."""
        hits = sorted(
            h for pat in (f"/runpod-volume/models/{kind}/**/*", f"{COMFY_DIR}/models/{kind}/**/*")
            for h in glob.glob(pat, recursive=True)
            if needle.lower() in Path(h).name.lower())
        # "fl2va"/"ref2va" needles must not grab 10Eros files which also contain them
        if needle.lower() in ("fl2va", "ref2va"):
            hits = [h for h in hits if "eros" not in Path(h).name.lower()] or hits
        return Path(hits[0]).name if hits else None

    img_idx = 0
    drop_lora = None
    sigma_off = {}
    for nid, node in prompt.items():
        ct = node.get("class_type", "")
        inp = node.get("inputs", {})

        if "LoadImage" in ct:
            if img_idx < len(images):
                inp["image"] = images[img_idx]
                if "LoadImageMiniState" in inp:
                    inp["LoadImageMiniState"] = images[img_idx]
                if "LoadImagePixState" in inp:
                    try:
                        state = json.loads(inp["LoadImagePixState"])
                        state["orig_name"] = images[img_idx]
                        inp["LoadImagePixState"] = json.dumps(state)
                    except Exception:
                        inp.pop("LoadImagePixState", None)
                img_idx += 1
        elif "LoadVideo" in ct and video_name:
            inp["video"] = video_name
        if job.get("prompt") and "prompt" in inp and not isinstance(inp["prompt"], list):
            inp["prompt"] = job["prompt"]
        if job.get("prompt") and ct == "WildcardProcessor":
            inp["text"] = job["prompt"]
            inp["populated_text"] = job["prompt"]
        if job.get("camera_tag") and "camera_tag" in inp:
            inp["camera_tag"] = job["camera_tag"]
        if job.get("vl_preset"):
            for wkey in ("preset_prompt", "enhancement_style"):
                if wkey in inp:
                    inp[wkey] = job["vl_preset"]
        if job.get("enhance") is False and "passthrough" in inp:
            inp["passthrough"] = True
        if job.get("seconds"):
            sec = int(job["seconds"])
            if "duration" in inp:
                inp["duration"] = f"{sec}s"
            elif "length" in inp and not isinstance(inp["length"], list):
                inp["length"] = sec * 24
            elif "value" in inp and ct.startswith("Primitive"):
                inp["value"] = sec
        if "lora_name" in inp:
            cur_lora = str(inp.get("lora_name", "")).lower()
            if "realism" in cur_lora or "character_swap" in cur_lora:
                # Style LoRA slot (Singularity recipe): job.lora =
                # none | realism | char_swap. Disabled/missing → drop the
                # node entirely below (ComfyUI validates lora_name even at
                # strength 0), so mark it for removal.
                sel = job.get("lora") or "none"
                drop_lora = nid
                if sel != "none":
                    needle = {"realism": "realism", "char_swap": "character_swap"}.get(sel, sel)
                    found = _find_file("loras", needle)
                    if not found:
                        raise RuntimeError(f"Style LoRA '{sel}' not found on the volume")
                    inp["lora_name"] = found
                    for skey in ("strength_model", "strength_clip"):
                        if skey in inp:
                            inp[skey] = 1.0
            elif cfg is not None:
                if cfg["lora"]:
                    found = _find_file("loras", cfg["lora"])
                    if found:
                        inp["lora_name"] = found
                        for skey in ("strength_model", "strength_clip"):
                            if skey in inp:
                                inp[skey] = 1.0
                else:
                    for skey in ("strength_model", "strength_clip"):
                        if skey in inp:
                            inp[skey] = 0.0
        elif cfg is not None and "unet_name" in inp:  # MiniMax H3 generator/enhancer node
            needle = cfg["unet"]
            if needle.lower() not in str(inp.get("unet_name", "")).lower():
                found = _find_file("unet", needle) or _find_file("diffusion_models", needle)
                if found:
                    inp["unet_name"] = found
            for wkey, ckey in (("steps", "steps"), ("sampler_name", "sampler"), ("scheduler", "scheduler"),
                               ("shift_video", "shift_video"), ("shift_audio", "shift_audio")):
                if wkey in inp and not isinstance(inp[wkey], list):
                    inp[wkey] = cfg[ckey]
            if cfg.get("tau") and "tau" in inp:
                inp["tau"] = cfg["tau"]
        if cfg is not None:
            # Sampler/step/shift values live on dedicated nodes, not on the
            # unet loader — patch them by class_type or the preset would only
            # swap weights/LoRA while the template settings silently win.
            if ct == "KSamplerSelect" and "sampler_name" in inp:
                inp["sampler_name"] = cfg["sampler"]
            elif ct == "BasicScheduler":
                for wkey in ("steps", "scheduler"):
                    if wkey in inp:
                        inp[wkey] = cfg[wkey]
            elif ct == "MiniMaxH3SigmaShift":
                if cfg.get("sigma_off"):
                    # API format ignores UI bypass modes — physically remove
                    # the node and rewire its consumers to its model source.
                    sigma_off[nid] = inp.get("model")
                else:
                    for wkey in ("shift_video", "shift_audio"):
                        if wkey in inp:
                            inp[wkey] = cfg[wkey]
    if sigma_off:
        for nid in sigma_off:
            del prompt[nid]
        for n in prompt.values():
            for k, v in n.get("inputs", {}).items():
                if isinstance(v, list) and v and v[0] in sigma_off:
                    n["inputs"][k] = sigma_off[v[0]]
    # LoadImage nodes left on the template filename (fewer uploads than loaders —
    # e.g. single-image edits with an optional image2) would fail validation;
    # drop them and unlink their consumers instead.
    uploaded = set(images)
    dead = [nid for nid, n in prompt.items()
            if "LoadImage" in n.get("class_type", "")
            and n.get("inputs", {}).get("image") not in uploaded]
    # Same for reference video loaders left on their template value when the
    # job carries no video (R2VA recipes have an optional ref_videos input).
    dead += [nid for nid, n in prompt.items()
             if "LoadVideo" in n.get("class_type", "")
             and n.get("inputs", {}).get("video") != video_name]
    if dead:
        for nid in dead:
            del prompt[nid]
        for n in prompt.values():
            for k in [k for k, v in n.get("inputs", {}).items()
                      if isinstance(v, list) and v and v[0] in dead]:
                del n["inputs"][k]
    if drop_lora is not None:
        # Rewire the dropped style LoRA's consumers to its model source.
        src = prompt[drop_lora].get("inputs", {}).get("model")
        del prompt[drop_lora]
        if isinstance(src, list):
            for n in prompt.values():
                for k, v in n.get("inputs", {}).items():
                    if isinstance(v, list) and v and v[0] == drop_lora:
                        n["inputs"][k] = src
    # Post-processing toggles: the graph chain is  save.images <- rife.frames
    # <- upscale.images <- VAEDecode.  upscale=false rewires rife.frames to the
    # decoder output; rife=false rewires save.images to rife's own source.
    upscaler = next((n for n in prompt.values() if "upscaler_trt_model" in n.get("inputs", {})), None)
    rife = next((n for n in prompt.values() if "rife_trt_model" in n.get("inputs", {})), None)
    saver = next((n for n in prompt.values()
                  if isinstance(n.get("inputs", {}).get("images"), list)
                  and n["inputs"]["images"]
                  and rife is not None
                  and n["inputs"]["images"][0] == next(k for k, v in prompt.items() if v is rife)), None) if rife else None
    if not job.get("upscale", True) and upscaler is not None and rife is not None:
        rife["inputs"]["frames"] = upscaler["inputs"]["images"]
    if not job.get("rife", True) and rife is not None and saver is not None:
        saver["inputs"]["images"] = rife["inputs"]["frames"]
        if "frame_rate" in saver["inputs"]:
            saver["inputs"]["frame_rate"] = saver["inputs"]["frame_rate"] // int(rife["inputs"].get("multiplier", 2) or 2)
    # Upscaler model / target overrides — the loader node feeds "upscaler_trt_model".
    upscaler_loader = next((n for n in prompt.values()
                            if n.get("class_type", "").endswith("LoadUpscalerTensorrtModel")), None)
    if upscaler_loader is not None and job.get("upscale_model"):
        upscaler_loader["inputs"]["model"] = job["upscale_model"]
    if upscaler is not None and job.get("upscale_target"):
        upscaler["inputs"]["resize_to"] = job["upscale_target"]
    # Output size: "1344x768" rewrites the size/latent state and syncs the
    # Qwen encoder's reference resolution to the long edge. The new Pixaroma
    # helper nodes (Sizes/Dropdown) replace the old PixaromaResolution node.
    # Dropdowns feeding a TextEncodeQwenImage21 "resolution" input only —
    # other PixaromaDropdown nodes may control unrelated widgets.
    enc_ids = {nid for nid, n in prompt.items()
               if n.get("class_type", "").startswith("TextEncodeQwenImage21")}
    dropdown_ids = set()
    for nid in enc_ids:
        res = prompt[nid].get("inputs", {}).get("resolution")
        if isinstance(res, list) and res and str(res[0]) in prompt:
            dropdown_ids.add(str(res[0]))
    m = re.match(r"(\d+)\s*x\s*(\d+)", str(job.get("image_size") or ""))
    if m:
        w, h = int(m.group(1)), int(m.group(2))
        for nid, node in prompt.items():
            ct = node.get("class_type", "")
            inp = node.get("inputs", {})
            if ct == "PixaromaResolution":
                try:
                    state = json.loads(inp.get("ResolutionState") or "{}")
                except Exception:
                    state = {}
                state["w"], state["h"] = w, h
                inp["ResolutionState"] = json.dumps(state)
            elif ct == "PixaromaSizes":
                try:
                    state = json.loads(inp.get("SizesState") or "{}")
                except Exception:
                    state = {}
                state["w"], state["h"] = w, h
                inp["SizesState"] = json.dumps(state)
            elif ct == "EmptyLatentImagePresets":
                pw, ph = (w, h) if w >= h else (h, w)
                ratios = {
                    (1024, 1024): "1:1",
                    (1152, 896): "1.286:1",
                    (1216, 832): "1.46:1",
                    (1344, 768): "1.75:1",
                    (1536, 640): "2.4:1",
                }
                ratio = ratios.get((pw, ph))
                if ratio:
                    inp["dimensions"] = f"{pw} x {ph} ({ratio})"
                    inp["invert"] = h > w
            elif ct == "PixaromaDropdown" and str(nid) in dropdown_ids and inp.get("DropdownState"): 
                try:
                    state = json.loads(inp["DropdownState"])
                except Exception:
                    continue
                if state.get("type") == "int":
                    state["value"] = str(max(w, h))
                    inp["DropdownState"] = json.dumps(state)
            elif ct.startswith("TextEncodeQwenImage21") and isinstance(inp.get("resolution"), int):
                inp["resolution"] = max(w, h)
    # Explicit encoder resolution (job["resolution"]) overrides the
    # image_size-derived value on the PixaromaDropdown / direct int input.
    res_override = job.get("resolution")
    if res_override:
        try:
            res_value = str(int(res_override))
        except (TypeError, ValueError):
            res_value = str(res_override)
        for nid in dropdown_ids:
            inp = prompt[nid].setdefault("inputs", {})
            try:
                state = json.loads(inp.get("DropdownState") or "{}")
            except Exception:
                continue
            state["value"] = res_value
            inp["DropdownState"] = json.dumps(state)
        for nid in enc_ids:
            inp = prompt[nid].setdefault("inputs", {})
            if isinstance(inp.get("resolution"), int):
                inp["resolution"] = int(res_value)
    # Per-job seed: baked seeds would repeat identical results across jobs.
    # An explicit job["seed"] pins every seed consumer to the same value.
    job_seed = job.get("seed")
    if job_seed is None:
        job_seed = random.randrange(0, 2**32)
    for node in prompt.values():
        inp = node.get("inputs", {})
        class_type = node.get("class_type", "")
        node_seed = job_seed % (2**32)
        if class_type == "PixaromaSeed":
            inp["seed"] = node_seed
            try:
                state = json.loads(inp.get("SeedState") or "{}")
            except Exception:
                state = {}
            state["runSeed"] = node_seed
            inp["SeedState"] = json.dumps(state)
            continue
        for skey in ("seed", "noise_seed"):
            if isinstance(inp.get(skey), int) and not isinstance(inp.get(skey), bool):
                inp[skey] = node_seed
    return prompt


def _apply_node_params(prompt, params):
    """Apply generic {node_id:widget} patches, after the MMH3 presets."""
    for key, value in (params or {}).items():
        node_id, _, widget = key.rpartition(":")
        node = prompt.get(node_id)
        if isinstance(node, dict) and widget:
            node.setdefault("inputs", {})[widget] = value
    return prompt


def _interrupt_prompt(prompt_id):
    """Best-effort ComfyUI abort for a cancelled job: dequeue if pending,
    interrupt if mid-flight."""
    for path, payload in (("/queue", {"delete": [prompt_id]}), ("/interrupt", {})):
        try:
            _http(path, payload, timeout=5)
        except Exception as exc:
            print(f"[handler] {path} failed during cancel: {exc}", flush=True)


async def _queue_and_wait(prompt):
    # Async + awaited sleeps: the runpod SDK stops a job by cancelling its
    # task, so the CancelledError can only be delivered at an await — a sync
    # handler would freeze the event loop and never see the stop signal.
    client_id = uuid.uuid4().hex
    resp = json.loads(await asyncio.to_thread(
        _http, "/prompt", {"prompt": prompt, "client_id": client_id}, 60))
    prompt_id = resp["prompt_id"]
    deadline = time.time() + JOB_TIMEOUT_S
    try:
        while time.time() < deadline:
            hist = json.loads(await asyncio.to_thread(_http, f"/history/{prompt_id}", None, 30))
            entry = hist.get(prompt_id)
            if entry and entry.get("status", {}).get("status_str") == "error":
                msgs = [str(m) for m in entry.get("status", {}).get("messages", [])]
                raise RuntimeError(f"ComfyUI job failed: {' | '.join(msgs)[:800]}")
            if entry and entry.get("status", {}).get("completed"):
                return entry
            await asyncio.sleep(3)
    except asyncio.CancelledError:
        # Even if this await gets cancelled again, the orphan thread still
        # completes and ComfyUI gets interrupted.
        await asyncio.to_thread(_interrupt_prompt, prompt_id)
        raise
    raise TimeoutError(f"prompt {prompt_id} exceeded {JOB_TIMEOUT_S}s")


def _collect_new_files(since_ts):
    """Fallback: anything written under ComfyUI's output dir since the job ran."""
    out_dir = Path(COMFY_DIR) / "output"
    try:
        files = sorted(p for p in out_dir.rglob("*") if p.is_file() and p.stat().st_mtime >= since_ts)
    except Exception:
        return []
    out = []
    for p in files:
        if p.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp", ".mp4", ".webm", ".gif", ".wav", ".mp3", ".flac"):
            continue
        out.append({"filename": p.name, "b64": base64.b64encode(p.read_bytes()).decode()})
    return out


def _collect_outputs(history_entry, prompt=None):
    out, texts = [], {}
    for node_id, node_out in (history_entry.get("outputs") or {}).items():
        print(f"[handler] output node {node_id}: keys={list(node_out.keys())}", flush=True)
        for key in ("images", "gifs", "videos"):
            for item in node_out.get(key, []) or []:
                params = f"filename={item['filename']}&subfolder={item.get('subfolder','')}&type={item.get('type','output')}"
                data = _http(f"/view?{params}", timeout=300)
                out.append({"filename": item["filename"], "b64": base64.b64encode(data).decode()})
        src = (prompt or {}).get(node_id, {}).get("inputs", {}).get("source")
        if not isinstance(src, list):  # only ShowText-style nodes carry live text
            continue
        for t in node_out.get("text", []) or []:  # ShowText nodes → prompt trace
            src_ct = (prompt or {}).get(str(src[0]), {}).get("class_type", "")
            label = "Wildcards expanded" if "Wildcard" in src_ct else "Final prompt (QwenVL)"
            texts[label] = t if isinstance(t, str) else str(t)
    return out, texts


async def handler(job):
    print(f"[handler] job {job.get('id')} received", flush=True)
    inputs = job.get("input") or {}
    if inputs.get("action") == "health":
        # Hub smoke test: answers 200 without touching ComfyUI or models, so it
        # also works on an endpoint deployed without the network volume.
        return {
            "status": "ok",
            "comfyui": "failed" if _BOOT_ERR else ("ready" if _BOOT_DONE.is_set() else "booting"),
            "comfyui_error": str(_BOOT_ERR) if _BOOT_ERR else None,
            "workflows": sorted(p.name for p in WORKFLOWS_DIR.glob("*.json")),
            "volume_mounted": os.path.isdir("/runpod-volume"),
        }
    # Any real workload needs ComfyUI — wait for the background boot first.
    await _ensure_comfy()
    # _patch uploads input media (blocking, up to minutes for video) — run it
    # off-thread so a cancel signal can still be delivered during uploads.
    if isinstance(inputs.get("prompt_graph"), dict):
        # Raw API-format graph shipped inside the job (ForgeHub mode): the same
        # _patch applies — media upload, config preset, unet/lora needle, prompt.
        prompt = await asyncio.to_thread(_patch, inputs["prompt_graph"], inputs)
    else:
        wf = _load_workflow(inputs.get("workflow") or "")
        prompt = await asyncio.to_thread(_patch, wf, inputs)
    prompt = _apply_node_params(prompt, inputs.get("params"))
    print(f"[handler] queuing prompt ({len(prompt)} nodes)", flush=True)
    started = time.time()
    comfy_log = Path("/workspace/comfy.log")
    log_mark = comfy_log.stat().st_size if comfy_log.is_file() else 0
    history = await _queue_and_wait(prompt)
    print(f"[handler] comfy execution took {time.time() - started:.1f}s", flush=True)
    outputs, texts = await asyncio.to_thread(_collect_outputs, history, prompt)
    try:  # surface ComfyUI warnings/errors from this job in the worker log
        with open(comfy_log, "rb") as f:
            f.seek(log_mark)
            interesting = [l for l in f.read().decode(errors="replace").splitlines()
                           if re.search(r"warn|error|missing|unexpected|fail|dtype|clip|traceback", l, re.I)]
            for l in interesting[-40:]:
                print(f"[comfy] {l}", flush=True)
    except Exception:
        pass
    if not outputs:
        outputs = _collect_new_files(started)
        print(f"[handler] fallback sweep — {len(outputs)} file(s) in output/", flush=True)
    print(f"[handler] done — {len(outputs)} output(s)", flush=True)
    return {"outputs": outputs, "texts": texts}


if __name__ == "__main__":
    threading.Thread(target=_boot_in_bg, daemon=True).start()
    runpod.serverless.start({"handler": handler})

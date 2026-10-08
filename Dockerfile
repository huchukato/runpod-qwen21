# ComfyUI-RunPod-Serverless — Qwen Image Edit 2.1 variant (CUDA 13.0)
#
# - Qwen Image 2.1 official INT8 ConvRot DiT + Qwen3-VL-8B Heretic INT8 TE +
#   bf16 VAE + detailer LoRA, loaded from the network volume (~30GB on top
#   of the shared stack; needs a 48GB GPU)
# - Missing volume files are auto-downloaded at worker boot from
#   models-manifest.txt (populate-volume.sh, VOLUME_AUTOPOPULATE=false to skip)
# - runpod serverless handler drives ComfyUI per-job (handler.py)
#
# Build (from repo root):
#   docker build -f Dockerfile.qwen21 -t huchukato/comfyui-runpod-serverless:qwen21 .

FROM huchukato/comfyui-base:cu130

# ──────────────────────────────────────────────────────────────────────────────
# Custom nodes — image-edit stack only (no VHS / TensorRT)
# ──────────────────────────────────────────────────────────────────────────────
ENV GIT_TERMINAL_PROMPT=0
RUN dl() { \
    local user="$1" repo="$2" branch="${3:-main}" dir="$4"; \
    echo "Cloning $user/$repo ($branch) -> $dir"; \
    git clone --depth 1 --branch "$branch" "https://github.com/$user/$repo.git" "/opt/comfyui-baked/custom_nodes/$dir"; \
  } && \
    cd /opt/comfyui-baked/custom_nodes && \
    dl rgthree rgthree-comfy main rgthree-comfy && \
    dl huchukato ComfyUI-QwenVL-Mod main ComfyUI-QwenVL-Mod && \
    dl huchukato ComfyUI-TagForge v2.3.0 ComfyUI-TagForge && \
    dl pixaroma ComfyUI-Pixaroma main ComfyUI-Pixaroma && \
    dl kijai ComfyUI-KJNodes main ComfyUI-KJNodes && \
    dl yolain ComfyUI-Easy-Use main ComfyUI-Easy-Use && \
    dl ltdrdata ComfyUI-Impact-Pack Main ComfyUI-Impact-Pack && \
    dl ltdrdata ComfyUI-Impact-Subpack main ComfyUI-Impact-Subpack


# ──────────────────────────────────────────────────────────────────────────────
# Requirements + runpod serverless SDK
# ──────────────────────────────────────────────────────────────────────────────
RUN bash -c 'cd /opt/comfyui-baked/custom_nodes && for node_dir in */; do \
        if [ -f "$node_dir/requirements.txt" ]; then \
            echo "Installing requirements for $node_dir..." && \
            pip install --no-cache-dir -r "$node_dir/requirements.txt" || echo "Failed to install requirements for $node_dir"; \
        fi; \
    done' && \
    pip install --no-cache-dir --no-deps "transformers>=5.2.0" && \
    pip install --no-cache-dir 'runpod>=1.12' requests "huggingface_hub[cli]" hf_transfer && \
    pip cache purge

# ──────────────────────────────────────────────────────────────────────────────
# Model dirs (empty — real models come from the network volume via
# extra_model_paths.yaml written by the entrypoint)
# ──────────────────────────────────────────────────────────────────────────────
RUN mkdir -p /opt/comfyui-baked/models/{vae,vae_approx,diffusion_models,unet,text_encoders,clip_projections,loras,checkpoints,LLM,clip,clip_vision,ultralytics/bbox,sams}

ENV HF_TOKEN=""

# ──────────────────────────────────────────────────────────────────────────────
# Wildcards baked into TagForge (same set the pod image downloads at boot)
# ──────────────────────────────────────────────────────────────────────────────
RUN WILDCARD_DIR="/opt/comfyui-baked/custom_nodes/ComfyUI-TagForge/wildcards" && \
    WILDCARD_BASE="https://github.com/huchukato/ComfyUI-Garage/raw/master/wildcards" && \
    for wf in pmp/act.yaml pmp/actff.yaml pmp/actffm.yaml pmp/actmmf.yaml pmp/actsolo.yaml \
              pmp/blwjob.yaml pmp/prmpt.yaml pmp/qwen21.yaml \
              pmp/prmpt/acc.yaml pmp/prmpt/char.yaml pmp/prmpt/clths.yaml pmp/prmpt/exprss.yaml \
              pmp/prmpt/hair.yaml pmp/prmpt/imgcmp.yaml pmp/prmpt/lctns.yaml pmp/prmpt/light.yaml pmp/prmpt/lens.yaml \
              pmp/prmpt/pose.yaml pmp/prmpt/styles.yaml pmp/prmpt/dynmc.yaml; do \
        mkdir -p "$WILDCARD_DIR/$(dirname "$wf")" && \
        wget -q --tries=3 --timeout=30 "$WILDCARD_BASE/$wf" -O "$WILDCARD_DIR/$wf" || \
        echo "⚠️ wildcard $wf download failed"; \
    done

# ──────────────────────────────────────────────────────────────────────────────
# Serverless entrypoint + handler + workflow templates (API format)
# ──────────────────────────────────────────────────────────────────────────────
COPY handler.py /opt/serverless/handler.py
COPY serverless-entrypoint.sh /opt/serverless/entrypoint.sh
COPY populate-volume.sh /opt/serverless/populate-volume.sh
COPY models-manifest.txt /opt/serverless/models-manifest.txt
COPY workflows-api/qwen21/ /opt/workflows/
COPY workflows-api/pony/ /opt/workflows/
RUN chmod +x /opt/serverless/entrypoint.sh /opt/serverless/populate-volume.sh

ENV COMFYUI_DIR="/workspace/runpod-slim/ComfyUI" \
    WORKFLOWS_DIR="/opt/workflows" \
    JOB_TIMEOUT_S="1800" \
    VENV_SUFFIX="cu130"

# Base image carries ENTRYPOINT ["/start.sh"] (pod mode) — a bare CMD would be
# passed to it as args and the runpod handler would never start. Override the
# entrypoint so the container runs the serverless handler directly.
ENTRYPOINT ["/opt/serverless/entrypoint.sh"]

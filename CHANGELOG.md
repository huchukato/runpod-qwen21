# Changelog

## v1.0.0

Lazy ComfyUI boot + restrict Hub test to CUDA 13.x hosts
hub.json: listing title
Qwen Image 2.1 serverless worker — initial Hub release


## v1.0.1

_(no changes)_


## v1.0.2

Fix missing threading import breaking worker startup


## v1.0.3

Dockerfile: repo-local name and build command
Sync with serverless repo: TagForge v2.3.0 pin, dynmc wildcard, single-save Pony wf, Singularity handler presets
Manifest: garage-hosted detectors + upscalers + TRT engines; unified Pony wf
hub.json: drop endpointType LB so only Serverless endpoint deploy is offered
FaceDet support: Easy-Use + Impact Pack + detector models + workflow
Move ForgeHub CTA under the intro
Drop redundant ForgeHub link from companion stack (CTA banner below)
Replace ForgeHub CTA with slim 3.6:1 banner
Slim ForgeHub CTA banner (960x404, 103KB)
Cap ForgeHub CTA banner at 640px
Add ForgeHub download CTA banner
Link VAE source repo in models table


## v1.0.4

Fix Face Detailer chain in entrypoint


## v1.0.5

S3 offload for large outputs (s3Config -> presigned URL, ~20MB gateway cap) + boto3
Drop negative prompt from Qwen Image 2.1 workflows (cfg=1 ignores it)


## v1.0.6

_(no changes)_


## v1.0.7

Fix manifest: qwen21-only models (was a copy of the mmh3 manifest)


## v1.0.8

Disable xet in volume populate: chunk cache overflows 30GB container disk (EDQUOT)


## v1.0.9

handler: boot-sync workflows from ForgeHub; manifest: add pmpIncaseStyle; README: absolute image URLs
Dockerfile: drop baked wildcard wget — wildcards ship via TagForge boot-sync from Garage


## v1.0.10

Drop CUDA 12.9 from allowedCudaVersions — image is CUDA 13.0 only


## v1.0.11

hub test: 4090 -> 5090 — the 4090 pool is empty and the 5090 is the only CUDA-13-capable type with stock right now
hub test: the smoke test's GPU is a MIG 24GB slice — the 4090 pool is empty (the test would burn on deployment the same way mmh3's did)
image_size: the easy fullLoader branch — the Pony workflow's resolution picker (space-format SDXL buckets + latent w/h); the branch was briefly in the mmh3 handler by mistake


## v1.0.12

remove the Hub smoke test entirely — every test deployment burned on the GPU supply drought
skip the Hub smoke test: rename tests.json -> tests_.json — the test GPU allocation burned the release on the supply drought; reversible when the supply returns


## v1.0.13

tests.json: align with ltx-25 (4090, no autopopulate env)
restore Hub smoke test — RunPod may require tests.json for indexing now
handler: passthrough follows the enhance flag in both directions; pin runpod<2
Dockerfile: NODES_BUST cache-bust arg for custom node re-clones
README: Requirements now say Max workers = 1 (Runpod deploy default is 3; one warm worker with queued jobs beats N cold starts on this stack)
README: the Edit workflow's Storyboard/Edit preset menu, the Pony resolution picker, the image_size job field semantics and the full baked node list


## v1.0.14

drop baked workflows-api (ForgeHub sync is the only source), manifest: turbo + uncensored step90000 + elusarcas on garage
manifest: swap pmpInCaseEnhanced for pmpRealAnime, add realism_stable_yogi_illustrious_v55
manifest: qwen 2.1 turbo (8-step) — the Turbo workflow needs it


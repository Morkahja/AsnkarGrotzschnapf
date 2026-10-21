# Development Task 010 — DreamShaper XL in the Prompt Lab

Status: verified.

## Model

DreamShaper XL 1.0 Alpha 2 Fixed VAE FP16 is an installed SDXL checkpoint. The lab loads it with `--model`, CPU offload, and VAE tiling. Defaults are 1024×1024, 30 steps, CFG 5.5, `euler_a`, and a typography/photorealism negative prompt. Commercial use is unverified, so print packs from this model are not publication-ready for Etsy.

Production remains on Z-Image Turbo.

## Verification

`PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -q` ran 72 tests, all passing.

One lab generation used Ollama Surreal Abstract on THE LAMB, then DreamShaper XL with the visual prompt alone:

`data/experiments/dreamshaper-010/20261021T031354719051Z-interpreted-body-seed-42.png`

Duration 44.1 seconds. PNG is 1024×1024. Metadata records the checkpoint path, checksum, negative prompt, `euler_a`, CFG 5.5, 30 steps, VAE tiling, CPU offload, and `commercial_use: unverified`. Approving a temporary pack left `publication_ready` false because of that commercial status even though the poem rights cleared. Earlier experiments and packs were not rewritten.

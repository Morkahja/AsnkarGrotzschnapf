# Development Task 005 — Model selection

Status: implemented.

## Goal

The prompt lab can switch among the installed image models. Production generation stays on Z-Image Turbo until that default is reviewed.

## Models

- Z-Image Turbo uses the diffusion model, Qwen3 text encoder, and VAE already configured for production. Negative prompting is off, CFG is 1, and the sampler is Euler.
- Z-Image uses `z-image-Q4_K_M.gguf` with the same text encoder and VAE. Its defaults are 512×512, 35 steps, CFG 4, Euler, and the shared negative prompt.
- RealVisXL V5 is the single checkpoint `RealVisXL_V5.0_fp16.safetensors`, loaded with `--model`. Its defaults are 1024×1024, 30 steps, CFG 5, `dpm++2m` with the Karras scheduler, clip skip 2, and the shared negative prompt.

A model appears in the list when its files are on disk. The defaults are filled in when the model is selected, and every field stays editable.

## Metadata

Each new experiment records the model name, model path, command type, positive prompt, negative prompt, seed, resolution, steps, CFG, sampler, clip skip, duration, and output path. Older experiment files stay as they were saved.

## Production

`production_model()` returns Z-Image Turbo. `anskar generate --pending` still uses that model and the Painterly wrapped prompt.

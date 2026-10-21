# Development Task 002 — Local image generation

Status: implemented.

## Goal

Connect the poetry archive to the installed local image generator. Do not publish, do not mark the pending poem used, and do not change the installed runtime.

## Generator

Executable: `/home/babuni/local-ai/stable-diffusion.cpp/build-vulkan/bin/sd-cli`

Models in `/home/babuni/local-ai/models/z-image-turbo/`:

- Diffusion: `z_image_turbo-Q4_K.gguf`
- Text encoder: `Qwen3-4B-Instruct-2507-UD-Q4_K_XL.gguf`
- VAE: `ae.safetensors`

Verified settings: Vulkan device `vulkan0`, 8 Euler steps, CFG 1.0, CPU offloading, diffusion flash attention, and an initial resolution of 512×512.

## Behavior

`anskar generate --pending` reads the pending poem and sends its stored text, unchanged, to `sd-cli` through `subprocess` without a shell. The PNG is saved under `data/artworks/`. The artwork record stores the poem, prompt hash, model, seed, resolution, duration, command settings, and output path.

`anskar generate --pending --dry-run` prints that plan and does not start the generator.

A poem can have one successful artwork. An existing PNG is not overwritten. A failed or timed-out attempt can be retried. The pending selection stays pending until a person approves the artwork.

## First test

After the dry run, generate one 512×512 artwork from the pending poem, THE LAMB by William Blake. This is a functional test, not a print-ready file.

Result, 2026-10-21:

- File: `data/artworks/poem-1-selection-1-seed-1493270611.png`
- Dimensions: 512×512
- Duration: 11.7 seconds
- Seed: 1493270611
- Errors: none
- Pending poem: THE LAMB remains pending and is not marked used

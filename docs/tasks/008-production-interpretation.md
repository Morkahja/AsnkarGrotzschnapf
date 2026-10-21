# Development Task 008 — Production interpretation

Status: implemented.

## Production command

`anskar generate --pending` asks the local Ollama model `qwen3:8b` for one visual prompt. The default abstraction is Surreal Abstract. The default house style is Painterly. The default image model is Z-Image Turbo. `--model` can select another installed model. The poem stays in the archive and in the artwork metadata. The image generator receives the visual prompt.

Ollama is unloaded before image generation. `--style none` and composition `text` still send the stored poem by itself. A poem with a successful artwork is not generated again, and an existing PNG is not overwritten. Generation does not approve the artwork and does not mark the poem used.

Width and height can be set up to 1024. A long edge of at least 1024 is recorded as print-sized. A smaller image is an experimental export. Print packs say which of the two the approved image is.

## Verification

The live pending poem already has an artwork, so the live command refuses a second one. The production path was run once at 1024×1024 against a copy of the archive. The image is `data/production-check/poem-1-selection-1-seed-1049191551.png`. It was not approved and it was not published. The original artwork in `data/artworks/` was left in place.

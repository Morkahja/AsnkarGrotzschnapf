# Anskar Grotzschnapf

Permanent instructions for the development agent. One-off assignments live in `docs/tasks/`.

## Vision

Anskar Grotzschnapf is a fictional artist who discovers poetry and turns an entire poem into artwork. The finished system runs once a day on this computer:

1. Choose one complete poem from the local archive.
2. Turn that poem into one visual prompt, then generate the image locally.
3. Prepare the image as a high-quality downloadable art print.
4. Publish the print to Etsy.
5. Publish the artwork and the poem to Instagram.
6. Record the result, then wait for the next day.

Artwork generation uses the local `sd-cli` installation. An explicitly approved lab image can be prepared as a local print pack. Do not publish that pack to Etsy or Instagram until a task says so.

## Local-first architecture

- Python is the primary language. The standard library is the default. Add a dependency only when the standard library cannot do the job.
- SQLite is the persistent store. The archive database is `data/archive.sqlite`.
- Daily automation will be a systemd timer on this machine. Do not install that timer until the pipeline it starts actually exists.
- Local reasoning uses the installed Ollama model `qwen3:8b` on this computer. Do not install another language model. The prompt lab and production generation ask it for one visual prompt. It is unloaded before image generation.
- Use an external API only to generate an image, publish a finished work, or acquire source material.
- Do not introduce n8n, Docker, cloud hosting, or an external agent-orchestration framework.

Module boundaries:

- `anskar.archive` — poems, rights, selection
- `anskar.artwork` — local image generation from the pending poem
- `anskar.lab` — manual local prompt experiments
- `anskar.printmaker` — local print packs from an approved lab image
- `anskar.instagram` — Instagram Login for an approved pack. A real post requires `publish --confirm`. Tokens stay outside the repository and are never printed. No post is sent until that confirmation is given.
- `anskar-daily.service` and `anskar-daily.timer` — a later systemd schedule
- `anskar.interpret` — local Ollama interpretation for the prompt lab and production generation

## Archive rules

Store every poem in full. Keep the title, author, sourced death year, origin when a source establishes it, original language, the language of the stored text, translator when one exists, source URL, license notes, rights status, import time, and use history.

Rights status is one of `unverified`, `documented`, or `verified`.

- `documented` means the recorded sources support the decision.
- `verified` means a separate legal confirmation. Do not set `verified`. The importer refuses that status.
- A death year is marked verified only when its citation is stored in the same record. That flag records the citation. It is not the rights status `verified`. Do not invent a year, a country, or a license.

Swedish economic rights last through the end of the seventieth year after the year of the author's death (section 43 of the Swedish Copyright Act, SFS 1960:729). A work is inside that term when `death_year <= current_year - 71`. Calculate the cutoff from the date. Do not hard-code 1955.

Import a poem for commercial use in Sweden only when all of the following are true:

- The author's death year is sourced and on the public side of the cutoff.
- The stored text is in the author's language.
- No translator is named. A translation is a separate work. It stays ineligible until the translator's own death year is sourced and the same term has ended.
- The source URL and the rights note are present.
- The poem body is the poem itself, not an ebook header, footer, or license.

Project Gutenberg's United States public-domain statement is a source fact. It is not Swedish clearance.

A poem is used only after an explicit confirmation. Selection keeps one pending poem. Repeating the selection command returns that same poem until it is confirmed or skipped. Skipping dismisses the pending poem and does not mark it used.

## Artwork rules

The daily pipeline generates an image only for the pending poem. The poem's stored text stays unchanged, including line breaks and punctuation. Production asks Ollama for one visual prompt and sends that prompt by itself. The poem and the visual prompt are stored separately. The production default is Surreal Abstract with the Painterly preset on Z-Image Turbo. Another installed model can be chosen with `--model`, and that choice stays explicit. `--style none` and composition `text` send the stored poem by itself. Dreamlike and Symbolic are the other built-in presets. Width and height are configurable, in multiples of 8 from 64 through 2400. The production default is 1600×1600, which is an 8×8 inch print at 200 PPI. `--print-inches` chooses another square size at that same 200 PPI, rounding each side up to a multiple of 8. A requested size is never silently reduced. Print quality is the pixel count divided by the physical side, not a DPI label. 300 PPI or more is excellent, 200–299 is the recommended standard, 150–199 is acceptable, and below 150 is not print-ready at that size. Square sizes are 4, 6, 8, 10, and 12 inches. A 1024-pixel square is acceptable at 6 inches (171 PPI) and not print-ready at 8 inches (128 PPI). A 1600-pixel square is standard at 8 inches (200 PPI). Portrait and landscape keep their aspect ratio. Interpolation is not treated as extra detail. The prompt lab below is a separate manual tool and uses the same presets.

Use the local `sd-cli` executable. Do not call a paid or cloud image API. Do not reinstall or modify the installed models, runtime, or GPU drivers.

Model paths, sampler settings, resolution, and the process timeout are configuration. A poem gets one successful production artwork. Never overwrite an existing PNG. Generation does not mark the poem used. The poem stays pending until a person approves the artwork.

## Prompt lab

`anskar lab` serves a page on this computer for trying prompts by hand. It is not the daily pipeline and it does not publish.

The page can generate from text typed into it. House style is a checkbox, a style prompt, and a preset, kept separate from the title and the text. Composition can send the selected text alone, the style followed by that text, or the style as a wrapped instruction. Direct generation uses that composition. Interpreted generation asks the local Ollama model `qwen3:8b` for one visual prompt, shows it in an editable field, and sends that prompt by itself. The selected house style shapes the surface. Abstraction changes the construction: Gentle, Surreal Abstract, or Experimental. Surreal Abstract is the default. Dreamlike is the default preset. Z-Image and RealVisXL can carry a negative prompt that discourages photographs and lettering. Z-Image Turbo leaves that negative prompt off. An approved lab image can be prepared under `data/publish-packs/` without being published. A pack is publication-ready when the source text matches a poem with documented Swedish commercial-use rights and at least one supported print size is 150 PPI or better. Falling short of 300 PPI does not reject it. The download is the approved image. Print preparation does not upscale it or generate another one. No upscaler weights are installed. Instagram previews are a smaller copy when the long edge is above 1080, and they are never enlarged. A DPI label does not add detail, and interpolation does not either. A source text that does not fit the context window is refused rather than shortened. The Ollama model is unloaded before image generation when it is loaded. The page can switch among the installed models. Z-Image Turbo and Z-Image use separate diffusion, text-encoder, and VAE files. RealVisXL V5 and DreamShaper XL are SDXL checkpoints loaded with `--model`. DreamShaper XL defaults to 1024×1024, 30 steps, CFG 5.5, `euler_a`, CPU offload, and VAE tiling, with a typography and photorealism negative prompt. Its commercial license is unverified, so a DreamShaper pack is never publication-ready for Etsy until that status is documented. Each model prefills its own steps, CFG, sampler, resolution, clip skip, and negative prompt, and those values stay editable. Each saved run records the model, the command type, the positive prompt, the negative prompt, the sampler settings, the model checksum when known, and, for an interpretation, the source text, the visual prompt, any edit, and the Ollama settings. Files go under `data/experiments/`, one folder per named comparison. Each image has a metadata file beside it. Never overwrite an existing experiment file. Older experiment files stay as they were saved. The lab does not write to the poetry archive and does not mark a poem used.

## Development rules

- Inspect the directory and the toolchain before installing or changing anything.
- Do not use `sudo` without explicit approval.
- Do not delete user data.
- Do not install paid services, create external accounts, or publish anything externally.
- Do not overwrite existing archive rows on import.
- Preserve poem text exactly, including line breaks. Do not summarize, rewrite, or truncate it.
- Keep modules small enough to test without a network connection.
- Explain a significant failure, then fix it when the fix is safe.

## Agent responsibilities

The agent maintains this repository on the local machine, implements the task in `docs/tasks/`, and stops at the boundary that task sets. Permanent behavior belongs in this file. A finished task stays in `docs/tasks/` as a record; it does not become a standing instruction by itself.

Before claiming a task is done, run the tests and the command the task names. Report what was created, the layout, the imported count, the test results, and any decision that still needs a person.

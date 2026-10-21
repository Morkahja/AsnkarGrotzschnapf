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
- `anskar.printmaker` — local print packs from an approved lab image, or from a production artwork in daily mode
- `anskar.instagram` — Instagram Login. Manual `publish` still requires `--confirm`. The daily pipeline publishes automatically to `@anskargrotzschnapf` only. Tokens stay outside the repository and are never printed. Each caption ends with five hashtags in order: one literary tag from the poem (Ollama when available), one artistic-style tag, `#AIgeneratedart`, `#DigitalArtForSale`, and `#AnskarGrotzschnapf`.
- `anskar.etsy` — Etsy Open API digital-download listings. OAuth (`etsy setup` then `etsy connect`) stores tokens under `~/.config/anskar/` with automatic refresh. Manual `etsy publish` requires `--confirm`. OAuth success does not change seller verification. While the shop is `pending_verification`, the daily pipeline queues the print and still finishes. When the integration state is `ready`, each new daily pack is listed automatically as a quantity-1 digital download. The Swedish buyer-facing total is whole SEK (`230 + 20R + 10S`, VAT included). Etsy digital listings have no “prices include VAT” shop toggle: Etsy adds local digital VAT for EU buyers on top of the net list price, so the API stores net = inclusive × 4/5 so Swedish viewers (25% VAT) see that whole-krona total. Gallery images are the web preview (downscaled PNG, plus a lossy listing JPEG when ImageMagick is available) and three corner close-ups cut from that preview — never the print master. The paid download stays the single full-resolution print PNG. `etsy refresh-images --confirm` rebuilds those local gallery files and replaces photos on every active listing without touching the download file. Listing attributes set intended print width/height in inches, Unframed, Square / 1:1, one piece, Abstract & geometric, and materials `digital download`. Skip physical-only suggestions such as frame mat. AI and digital-download disclosure are included. `etsy maintain` (and `anskar-etsy-maintain.timer`) syncs sales, applies the day-30 20% discount, and removes unsold listings after 42 days. Historical backlog is never bulk-published without an explicit command.
- `anskar.daily` — fully automatic daily run: select → interpret → generate 1600×1600 → pack → Instagram → Etsy → mark poem used. Desktop notifications (`notify-send`) fire when the run starts, when image generation begins, when Instagram publishes, when Etsy publishes or queues, and when the run finishes or fails. Set `ANSKAR_NOTIFY=0` to silence them (tests do this).
- `anskar-daily.service` and `anskar-daily.timer` — noon local schedule (`anskar schedule install`). Optional system units with `WakeSystem=true` are staged by `anskar schedule prepare-system` (sudo to activate; do not leave both user and system timers publishing). Production runs use `systemd-inhibit` so sleep cannot interrupt generation or Instagram. The noon run never suspends the computer; KDE idle-suspend handles that. At most one live artwork per Swedish local calendar day (`Europe/Stockholm`); `Persistent=false` prevents catch-up backlog. `anskar schedule test-cycle` is a separate one-time rehearsal: it does not change the noon timer, a test artwork does not use the noon slot, and only that confirmed rehearsal may suspend afterwards, and only when the session stayed idle.
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

Commercial-use eligibility is decided at import and stored on the poem as `commercial_use_sweden`. Daily production trusts a documented `yes`. It does not repeat the Swedish-term calculation, and it does not ask for another approval of material the archive already cleared. Uncleared poem text is not published. Image-model commercial use comes from the installed model profile; an unverified profile is not publication-ready.

Manual mode: a poem is used only after an explicit `select --confirm`. Selection keeps one pending poem. Repeating the selection command returns that same poem until it is confirmed or skipped. Skipping dismisses the pending poem and does not mark it used.

Daily mode (`anskar daily-run`): the pipeline selects one eligible poem, auto-approves the production pack after generation and metadata checks, publishes to Instagram, and marks the poem used after Instagram succeeds. Etsy has an explicit integration state (`pending_verification`, `ready`, `disabled`). While Etsy is `pending_verification`, listings are queued only and must not block the daily run. When Etsy is `ready`, new daily runs may publish listings; backlog publishing still needs an explicit command. Partial Instagram failures are resumable. Prompt Lab and manual commands stay available and still require human confirmation where they already did.

## Artwork rules

The daily pipeline generates an image only for the pending poem. The poem's stored text stays unchanged, including line breaks and punctuation. Production asks Ollama for one visual prompt and sends that prompt by itself. The poem and the visual prompt are stored separately. The production default is Surreal Abstract with the Painterly preset on Z-Image Turbo. Another installed model can be chosen with `--model`, and that choice stays explicit. `--style none` and composition `text` send the stored poem by itself. Dreamlike and Symbolic are the other built-in presets. Width and height are configurable, in multiples of 8 from 64 through 2400. The production default is 1600×1600, which is an 8×8 inch print at 200 PPI. `--print-inches` chooses another square size at that same 200 PPI, rounding each side up to a multiple of 8. A requested size is never silently reduced. Print quality is the pixel count divided by the physical side, not a DPI label. 300 PPI or more is excellent, 200–299 is the recommended standard, 150–199 is acceptable, and below 150 is not print-ready at that size. Square sizes are 4, 6, 8, 10, and 12 inches. A 1024-pixel square is acceptable at 6 inches (171 PPI) and not print-ready at 8 inches (128 PPI). A 1600-pixel square is standard at 8 inches (200 PPI). Portrait and landscape keep their aspect ratio. Interpolation is not treated as extra detail. The prompt lab below is a separate manual tool and uses the same presets.

Use the local `sd-cli` executable. Do not call a paid or cloud image API. Do not reinstall or modify the installed models, runtime, or GPU drivers.

Model paths, sampler settings, resolution, and the process timeout are configuration. A poem gets one successful production artwork. Never overwrite an existing PNG. Generation alone does not mark the poem used. In manual mode the poem stays pending until a person confirms. In daily mode the poem is marked used after Instagram publication succeeds (Etsy may still be queued).

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

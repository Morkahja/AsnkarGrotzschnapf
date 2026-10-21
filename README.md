# Anskar Grotzschnapf

Local poetry archive and daily artist. Once a day this machine can:

1. Choose one cleared poem from the archive  
2. Generate a 1600×1600 artwork locally (`sd-cli`)  
3. Prepare a print pack  
4. Publish to Instagram (`@anskargrotzschnapf`)  
5. List a quantity-1 SEK digital download on Etsy when the shop is `ready`

**Version 1.0** is that automated loop with the noon wake timer and Etsy maintenance armed. Manual Instagram and Etsy `publish` commands still require `--confirm`.

Permanent agent instructions: [AGENTS.md](AGENTS.md). Short history: [CHANGELOG.md](CHANGELOG.md). Design/task records: [`docs/tasks/`](docs/tasks/).

## Setup

Python 3.14 provides SQLite in the standard library. `setuptools` is not installed, so the project is not installed as a package and nothing is fetched from the network.

```bash
python3 -m venv --without-pip .venv
```

The virtual environment at `.venv` was created with that command.

## Daily automation (hands-off)

Prefer **one** noon publisher:

- **PC may sleep overnight:** system timer with `WakeSystem=true` (staged by `schedule prepare-system`, enabled with sudo). Keep the **user** noon timer disabled.
- **PC stays awake:** user timer via `schedule install --enable`.

Also enable the Etsy maintain user timer (sales, day-30 discount, day-42 removal).

```bash
PYTHONPATH=src .venv/bin/python -m anskar schedule status
PYTHONPATH=src .venv/bin/python -m anskar schedule install --enable
PYTHONPATH=src .venv/bin/python -m anskar schedule prepare-system
```

After `prepare-system`, activate the system wake timer (and disable the user noon timer) with the commands that command prints. Do not leave both noon timers enabled.

```bash
PYTHONPATH=src .venv/bin/python -m anskar daily-status
PYTHONPATH=src .venv/bin/python -m anskar daily-run          # one-off live run
PYTHONPATH=src .venv/bin/python -m anskar daily-resume <id>  # finish an incomplete run
```

`daily-run` auto-resumes an incomplete live run. At most one live Instagram artwork per Swedish calendar day (`Europe/Stockholm`). Desktop notifications fire on major stages; set `ANSKAR_NOTIFY=0` to silence them.

## Commands

Import the Project Gutenberg collection:

```bash
PYTHONPATH=src .venv/bin/python -m anskar import data/collections/gutenberg-public-domain.json
```

Select a poem. The same pending poem is shown on every later run until it is confirmed or skipped.

```bash
PYTHONPATH=src .venv/bin/python -m anskar select
PYTHONPATH=src .venv/bin/python -m anskar select --confirm
PYTHONPATH=src .venv/bin/python -m anskar select --skip
```

`--confirm` marks the pending poem used. `--skip` dismisses it and leaves it unused. The database is `data/archive.sqlite`. Set `ANSKAR_DB` to use a different file.

Generate one PNG from the pending poem. The stored poem is not rewritten. By default Ollama writes one Surreal Abstract visual prompt, Painterly shapes its surface, and Z-Image Turbo makes the image. The poem stays pending. Ollama is unloaded before image generation.

```bash
PYTHONPATH=src .venv/bin/python -m anskar generate --pending --dry-run
PYTHONPATH=src .venv/bin/python -m anskar generate --pending
PYTHONPATH=src .venv/bin/python -m anskar generate --pending --print-inches 10
PYTHONPATH=src .venv/bin/python -m anskar generate --pending --style none
PYTHONPATH=src .venv/bin/python -m anskar generate --pending --style dreamlike
PYTHONPATH=src .venv/bin/python -m anskar generate --pending --model z-image
```

`--style` accepts `painterly` (the default), `dreamlike`, `symbolic`, or `none`. `--composition` accepts `wrapped` (the default), `style_text`, or `text`. `none` and `text` send the stored poem by itself. The other compositions ask Ollama for the image prompt. `--abstraction` accepts `gentle`, `surreal` (the default), or `experimental`. `--model` accepts an installed model. The default is Z-Image Turbo. The default image is 1600×1600, an 8×8 inch print at 200 PPI. `--print-inches` sets a square side at 200 PPI and rounds up to a multiple of 8. `--width` and `--height` are exact pixels, multiples of 8 from 64 through 2400. A request is not silently reduced. A poem that already has a successful artwork is not generated again.

The generator is the installed `sd-cli`. Paths and sampler settings can be overridden with `ANSKAR_SD_CLI`, `ANSKAR_SD_MODEL_DIR`, `ANSKAR_SD_DIFFUSION_MODEL`, `ANSKAR_SD_TEXT_ENCODER`, `ANSKAR_SD_VAE`, `ANSKAR_SD_BACKEND`, `ANSKAR_SD_STEPS`, `ANSKAR_SD_SAMPLER`, `ANSKAR_SD_CFG`, `ANSKAR_SD_WIDTH`, `ANSKAR_SD_HEIGHT`, `ANSKAR_SD_OFFLOAD_TO_CPU`, `ANSKAR_SD_FLASH_ATTN`, `ANSKAR_SD_TIMEOUT`, and `ANSKAR_ARTWORKS_DIR`. Images are written to `data/artworks/`. A JSON file beside each new image keeps the poem and the visual prompt. Approving that file for print is a separate command.

Open the local prompt lab in a browser. It generates from text you type and saves each result under `data/experiments/`. Direct generation sends that text through the house style. Interpret with Ollama asks the local `qwen3:8b` model for a visual prompt, which stays editable, and then sends that prompt by itself. Abstraction can be Gentle, Surreal Abstract, or Experimental. The model list includes Z-Image Turbo, Z-Image, RealVisXL V5, and DreamShaper XL when those files are installed. DreamShaper XL is for experiments and local previews until its commercial license is documented. House style defaults to Dreamlike; Painterly and Symbolic stay available under Advanced. It does not change the poetry archive. Ollama is expected at `127.0.0.1:11434`.

Prepare a local print pack from one saved image. The pack copies that file as the print master. It does not upscale or generate a second image. It classifies 4, 6, 8, 10, and 12 inch sizes from the real pixel count, writes a digital-download listing, and makes a smaller Instagram preview when the long edge is above 1080. Preparing a pack does not publish. A size below 150 PPI is marked unsuitable.

```bash
PYTHONPATH=src .venv/bin/python -m anskar print data/experiments/<experiment>/<file>.json
```

```bash
PYTHONPATH=src .venv/bin/python -m anskar lab
```

The lab page is `http://127.0.0.1:8765/`. Use another port with `--port`.

### Instagram

Instagram uses the official Instagram API with Instagram Login. The token stays in `~/.config/anskar/instagram.json`, outside this repository. Put the token in a file by itself and install it from that file. The token is not printed.

```bash
PYTHONPATH=src .venv/bin/python -m anskar instagram install-token --from /path/to/token-file
PYTHONPATH=src .venv/bin/python -m anskar instagram status
PYTHONPATH=src .venv/bin/python -m anskar instagram prepare <pack-id>
PYTHONPATH=src .venv/bin/python -m anskar instagram publish <pack-id> --dry-run
PYTHONPATH=src .venv/bin/python -m anskar instagram publish <pack-id> --confirm
```

`status` only reads the account. A public https image URL (often via R2) must be configured before a live post. When fewer than seven days remain on the token, `instagram renew` refreshes it through Meta's `ig_refresh_token` grant. Meta “API access blocked” is a developer-dashboard restriction, not a missing local file.

### Etsy

Etsy Open API credentials stay in `~/.config/anskar/etsy.json`. Register the exact callback `https://localhost:8743/etsy/callback` on the Etsy app, store the keystring and shared secret, then authorize once:

```bash
PYTHONPATH=src .venv/bin/python -m anskar etsy setup
PYTHONPATH=src .venv/bin/python -m anskar etsy connect
PYTHONPATH=src .venv/bin/python -m anskar etsy status
PYTHONPATH=src .venv/bin/python -m anskar etsy test
PYTHONPATH=src .venv/bin/python -m anskar etsy set-state ready
PYTHONPATH=src .venv/bin/python -m anskar etsy maintain
PYTHONPATH=src .venv/bin/python -m anskar etsy refresh-descriptions
```

OAuth alone does not enable publishing — seller verification is separate (`etsy set-state ready`). Buyer listing copy is short: digital download note, intended print size, the source poem, and one local-AI disclosure. Gallery photos are preview-based; the paid file is the print PNG. Tokens refresh automatically. Secrets are never printed.

## One-time wake rehearsal

This suspends the computer. It does not change the noon timer. `--publish` creates one real Instagram post; Etsy is queued only.

```bash
PYTHONPATH=src .venv/bin/python -m anskar schedule test-cycle
PYTHONPATH=src .venv/bin/python -m anskar schedule test-cycle --publish
```

The result is kept in `data/test-cycle/latest.json`. Cancel before the wake with `anskar schedule test-cycle --cancel`.

After pulling timer unit changes, refresh installed units (`schedule install` / `prepare-system` + sudo copy for the system timer).

## Tests

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

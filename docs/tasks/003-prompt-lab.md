# Development Task 003 — Prompt lab

Status: implemented.

## Goal

Provide a local page for trying text-to-image prompts by hand. This is not publishing automation and it does not change the poetry archive.

## Use

```bash
PYTHONPATH=src .venv/bin/python -m anskar lab
```

Open `http://127.0.0.1:8765/`.

The page accepts a title and a multiline text, and can send the title, the text, or both. Seed, width, height, steps, and CFG are set on the page. House style presets and model selection were added in later tasks. Each run saves a PNG and a JSON metadata file under `data/experiments/<experiment-name>/`. Existing files are not overwritten. New seed repeats the current text with another seed.

Name an experiment, then vary the mode or the text while leaving the other settings fixed. First stanza replaces the text box for one try and can be restored. Saved runs can be loaded back into the form.

The generator remains the installed local `sd-cli` and Z-Image Turbo models. House style presets were added in task 004 and are separate from the title field.

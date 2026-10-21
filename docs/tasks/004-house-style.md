# Development Task 004 — House style

Status: implemented.

## Goal

Give the prompt lab and the production generator one house style, with reusable presets, instead of pasting style text into the title field.

## Presets

Dreamlike, Painterly, and Symbolic are stored in `src/anskar/style.py`. Painterly is the production default. The poem or typed text is not rewritten. The style is composed around it.

## Composition

- Text only: the selected text is the whole prompt. A chosen preset is still recorded.
- Style + selected text: the style prompt, a blank line, then the selected text.
- Wrapped instruction: the style prompt, a blank line, the label `The following text:`, then the selected text.

`anskar generate --pending` uses Painterly and wrapped instruction unless another preset or composition is chosen. `--style none` sends the stored poem by itself.

## Prompt lab

`anskar lab` adds `Use house style`, a preset selector, and a style prompt. Each new run records the preset, the full style prompt, the final composed prompt, and whether house style was enabled. Experiment files saved before this task are left as they are.

## Production

```bash
PYTHONPATH=src .venv/bin/python -m anskar generate --pending --dry-run
PYTHONPATH=src .venv/bin/python -m anskar generate --pending --style none
```

A poem that already has a successful artwork is not generated again. The poetry archive is unchanged.

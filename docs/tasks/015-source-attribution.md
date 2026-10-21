# Task 015 — Daily Pipeline Source Attribution

## Selected poem

`O Captain! My Captain!` by Walt Whitman (poem_id **8**), daily run `daily-20261021T050508Z-4283c0e7`.

## Root cause

Printmaker matched poems with `WHERE body = ?` after calling `.strip()` on the source text. Whitman’s archived body begins with indented lines (`  O Captain!…`). Stripping removed those leading spaces, so the exact body match failed even though:

- metadata `poem_id`, `title`, `author`, and `source_text` were correct
- `source_text` equaled the archived body before strip
- `visual_prompt` was stored separately and was not used as the poem

## Fix

- Normalize newlines only; do not strip poem text.
- Prefer `poem_id` lookup and require the stored `source_text` to equal the archived body.
- Refuse a match when `source_text` equals the visual prompt.
- Carry `poem_id` / title / author on the pack manifest.
- Allow rebuilding a non-ready, unpublished pack from the same original image.
- Allow `daily-resume` of a failed run that already has an `image_path`.

## Artwork

Preserved at:

`data/artworks/poem-8-selection-3-seed-679240630.png` (1600×1600)

Pack rebuilt publication-ready (no regenerate, no publish):

`data/publish-packs/production-poem-8-seed-679240630-3966d2ce`

## Resume

`anskar daily-resume daily-20261021T050508Z-4283c0e7` can continue from that image/pack without generating again. Not executed in this task (no live publish).

## Tests

118 OK, including indented Whitman archive matching and failed-pack resume without regeneration.

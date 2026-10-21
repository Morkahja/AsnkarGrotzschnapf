# Task 014 — Duplicate Artwork Recovery

## Problem

The first live `anskar daily-run` stopped with `DuplicateArtworkError` for THE TIGER after a dry-run had already written a valid 1600×1600 production PNG and publish pack while leaving the poem pending.

## Cause

1. Dry-run completed artwork + pack for poem 3 (THE TIGER) without marking the poem used.
2. Live `daily-run` reselected that pending poem and called generation.
3. Generation refused to overwrite the existing succeeded artwork and halted the day.

## Fix

- `anskar.daily.recovery` decides generate / resume / skip_legacy before image generation.
- Compatible production artwork (1600×1600, experiment `production`) with a publication-ready pack is resumed; no regenerate, no overwrite.
- Incompatible legacy artwork is preserved; the pending selection is skipped and another eligible poem is chosen (bounded attempts).
- New selections exclude poems that already have a succeeded artwork.
- Unused poems with an unpublished ready pack are recovered even after a skip.
- Permanent `media` / `empty` / `duplicate` failures mark the daily run failed so they do not block the next day.
- Selection decisions are stored in `daily_runs.selection_log`.

## Verification (non-publishing)

`PYTHONPATH=src python3 -m anskar daily-run --dry-run` resumed THE TIGER pack `production-poem-3-seed-1343611714-b4017c1b` in about one second without regenerating.

- Existing artwork preserved at `data/artworks/poem-3-selection-2-seed-1343611714.png` (1600×1600).
- THE LAMB remains confirmed (used); not selected again.
- No live Instagram publish; daily timer not enabled.

## Tests

Full suite: 115 tests OK.

## Status after this task

- Poem now pending for production: THE TIGER (resumable with existing pack).
- Eligible unused poems: 7.
- Eligible for a genuinely new artwork: 5 (THE SICK ROSE, LONDON, XVIII, CXVI, The Raven).
- Daily pipeline can proceed automatically on the next live `daily-run` by publishing the recovered TIGER pack (Etsy stays queued while verification is pending).

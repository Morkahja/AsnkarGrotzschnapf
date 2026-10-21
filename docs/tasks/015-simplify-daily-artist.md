# Task 015 — Simplify the daily artist

## Workflow

`anskar daily-run` does one day, then stops:

1. Select one unused poem whose archive row is already `documented` and `commercial_use_sweden = yes`.
2. If that poem already has a 1600×1600 production PNG, keep that file. Prepare the print pack only when one is missing.
3. Otherwise ask Ollama for one visual prompt and generate one surreal, painterly image at 1600×1600.
4. Printmaker copies that image into the Instagram JPEG and the digital-print package.
5. Publish to Instagram through the existing Cloudflare R2 path.
6. Store the permalink.
7. Mark that poem used.
8. Queue the Etsy files while the shop is `pending_verification`.
9. Finish. Etsy does not block the day.

Prompt Lab and manual `select --confirm` stay separate. The daily timer was not enabled.

## What was removed

- Swedish-term checks are not repeated during generation or publication. Import stores `commercial_use_sweden`, and the daily run trusts a documented `yes`. The pack is linked by poem id. A visual prompt is never treated as the poem, and uncleared text is not published.
- Image-model commercial use is the installed model profile (`model_allows_publication`). An unverified profile stays not publication-ready.
- Daily mode has no approval screen and no extra confirmation step.
- Finishing a run marks that run's poem used. A different poem left pending is left pending.
- An existing production artwork is resumed even when its pack still has to be built. The PNG is not regenerated and not overwritten.
- A failed Instagram container (including Meta code 9007, media id not available) is retried on that same container. A second container is not created while the first one is still finished.

## Verification

Full suite: 125 tests OK.

Simulated day, on a copy of `data/archive.sqlite`, using the real Whitman artwork `data/artworks/poem-8-selection-3-seed-679240630.png`:

- Printmaker accepted it: publication-ready, 1600×1600, 200 PPI, poem id 8.
- Instagram publication was reached with a stand-in publisher and a permalink was stored.
- Etsy queue status `pending_shop_activation`, integration `pending_verification`.
- Resume of that completed day did not publish again and did not generate again.
- The live archive and the three production PNGs were unchanged.

## Still needs a person

The live run `daily-20261021T050508Z-4283c0e7` (O Captain! My Captain!) is `pack_prepared`. An Instagram attempt for that pack failed before a permalink existed. The artwork and pack are still on disk. `anskar daily-resume daily-20261021T050508Z-4283c0e7` will try that container again. This task did not retry it and did not enable the timer.

THE LAMB and THE TIGER are already confirmed. The next new image, after the Whitman run finishes, comes from the remaining eligible poems: THE SICK ROSE, LONDON, XVIII, CXVI, The Raven.

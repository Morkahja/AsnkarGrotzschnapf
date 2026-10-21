# Development Task 018 — Fully Autonomous Etsy Publishing

Status: implemented (mocked writes only; live listings wait for `set-state ready`).

## Behaviour

When seller state is `ready`, each new daily pack is listed automatically:

- Quantity 1, type `download`, `should_auto_renew=false`
- Price `229 + 20R + 10S` SEK (shop currency must be SEK)
- Preview via `uploadListingImage`; print master via `uploadListingFile`
- AI disclosure + digital-download + single-sale wording
- Poem text included only when commercial reproduction is cleared

`anskar etsy maintain` (and `anskar-etsy-maintain.timer`, every 6 hours) syncs paid sales, applies the day-30 20% discount from `activated_at`, and deletes unsold listings after 42 days. Sold artworks are never relisted.

Historical backlog is not bulk-published. Instagram remains independent of Etsy failures.

## Enable live publishing

1. Confirm seller onboarding in the Etsy seller account.
2. `anskar etsy readiness [pack-id]`
3. `anskar etsy set-state ready`
4. Next `anskar daily-run` creates the first live listing automatically.
5. `anskar schedule install` (optionally `--enable`) installs the maintain timer.

## Inspect

```bash
anskar etsy offerings
anskar etsy maintain
anskar etsy status
```

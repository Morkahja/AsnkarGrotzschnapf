# Development Task 013 — Fully automatic daily pipeline

Status: corrected. Instagram completes the daily run; Etsy stays queued while shop verification is pending.

## Corrected daily flow

```text
anskar daily-run
anskar daily-run --dry-run
```

1. Select one eligible unused poem.
2. Interpret (Surreal Abstract) + Painterly house style → generate 1600×1600.
3. Auto-prepare the publish pack.
4. Publish to `@anskargrotzschnapf` via R2.
5. Mark the poem used.
6. Queue Etsy digital-print assets (`etsy_status: pending_shop_activation`).
7. Mark the daily run `completed`.

**Successful Instagram publication completes the day.** Etsy must not block it.

## Independent states

Each run reports:

- `artwork_status`
- `instagram_status`
- `etsy_status`
- `daily_status` / `status`

## Etsy integration state

```text
anskar etsy status
anskar etsy set-state pending_verification|ready|disabled
anskar etsy setup          # credentials only; does not enable publishing
```

Default / current: `pending_verification`.

While pending: no Etsy API calls, no credential requirement, no daily-run failure. Pack files and `etsy/queue.json` are preserved.

When `ready`: new daily runs may publish Etsy. Backlog still needs an explicit `etsy publish <pack> --confirm`.

## Schedule

```text
anskar schedule install
anskar schedule status
```

Do not enable the timer until a watched live Instagram daily-run has succeeded.

## Artistic direction

Production uses Surreal Abstract + Painterly, reinforces the interpreted prompt with anti-photorealism constraints, and refuses obviously literal creature-in-landscape prompts before generation. These checks cannot reliably score gallery quality.

## Tests

Full suite includes Instagram-complete-with-Etsy-pending, no-credentials completion, resume without duplicate Instagram, next-poem selection, and optional Etsy activation.

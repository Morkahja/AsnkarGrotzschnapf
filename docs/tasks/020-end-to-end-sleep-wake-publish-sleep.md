# Task 020 — Complete End-to-End Sleep/Wake/Publish/Sleep

Status: implemented (ready for a live rehearsal; not executed during development).

## Cause (2026-10-21 rehearsal)

After RTC wake, `--run` settled and started the worker via `runuser` → `systemd-inhibit` → Python `--work`. That nested command exited in ~22ms without starting the production pipeline. `_subprocess` captured stderr, ignored the non-zero exit, and left `data/test-cycle/latest.json` at `status: scheduled`. `--finish` then stayed awake; systemd still reported success.

## Fix

- Hold `systemd-inhibit` as root **around** `runuser` (never the reverse).
- Drop privileges with an explicit `env HOME=…` for the artist account (no `--preserve-environment`).
- Propagate worker exit codes; inherit worker stdio into the journal; three-hour work timeout.
- Persist rehearsal stages (`scheduled` → `resumed` → `readiness` → `generating` / `publishing` → `published` / `failed` → `sleeping` / `completed`).
- After a successful publish: notify, filter Anskar's own inhibitor, request suspend, record `suspend_requested` vs `confirmed_queued` / `failed` / `skipped`.

Noon timer, CPU offline workaround, and Etsy maintain behavior are unchanged. Development did not suspend or publish live.

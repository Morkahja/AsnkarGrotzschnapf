# Task 021 — Harden daily, Etsy, and pack safety after review

Status: done (implemented 2026-10-21). Soft Instagram media×3 fails the run and leaves the poem eligible (does not permanently skip archive poems). Decisions locked with the operator on 2026-10-21.

## Decisions

| Topic | Choice |
|-------|--------|
| Stuck noon after Instagram timeout/API | Auto-resume incomplete run instead of refusing |
| One Swedish day | A live non–dry-run Instagram post consumes the day |
| Etsy listing failure when shop is ready | Day still completes; durable queue; `etsy maintain` retries those packs only |
| Rehearsal Etsy | Persist queue-only on the run; resume must not list |
| Permanent Instagram `media` | After 3 failures: fail/notify; leave poem eligible (do not permanently skip) |
| Pack id `..`, OAuth, reconnect, model gate, manifest, docs | Fix as recommended below |
| Sales sync / false sold / draft crash / noon conflict | Fix as recommended below |
| Timer randomized delay | Cut system (and user) timer delay so the inhibited oneshot starts soon after wake |

Rehearsal 120s paint timeout is already fixed (3h work budget). Do not reopen it.

Etsy listing photos no longer use the print master (done separately): gallery hero is `etsy/preview.jpg` or `etsy/preview.png`, detail crops come from the preview, and only `etsy/print.png` is the digital download.

## Scope

### 1. Daily auto-resume and one-a-day

**Files:** [src/anskar/daily/pipeline.py](src/anskar/daily/pipeline.py), [src/anskar/daily/ledger.py](src/anskar/daily/ledger.py), [systemd/anskar-daily.service](systemd/anskar-daily.service), [systemd/system/anskar-daily.service](systemd/system/anskar-daily.service), [tests/test_daily.py](tests/test_daily.py)

- Change `daily-run` / `begin_run` so that when an incomplete live run exists (`selected` / `generated` / `pack_prepared`, and also unfinished `instagram_published`), the noon path **resumes that run** instead of raising `busy`. Keep explicit `daily-resume <id>` for operators.
- Swedish-day guard: a non–dry-run `run_kind=daily` row with Instagram already published (`instagram_status=published` or status in `instagram_published` / `completed`) **consumes** the calendar day. A new artwork must not start that day; only resume/finish bookkeeping is allowed.
- Persist on the run: `queue_etsy_only` (bool) and enough history to count permanent `media` attempts per poem/pack (e.g. counter or selection_log entries).
- Rehearsal: set and store `queue_etsy_only=True` (and/or honour `run_kind=test`). `resume_run` and auto-resume must pass that flag into `_queue_or_publish_etsy`.
- After 3 permanent `media` failures for the same poem/pack: `skip_pending`, clear the failed blocking state as needed, select another eligible poem (same day if the day is not yet consumed by Instagram; otherwise next noon). Notify if notify is on.
- Include `run_kind` (and queue-only) in `public_run` / status output.
- Fix CLI: resume success text = poem marked used after Instagram (Etsy may still be queued).
- Narrow `_step_generate` TypeError fallback so it only applies when the callable rejects unexpected kwargs (or remove it and update test doubles).

### 2. Etsy queue retry from maintain

**Files:** [src/anskar/daily/pipeline.py](src/anskar/daily/pipeline.py), [src/anskar/etsy/maintain.py](src/anskar/etsy/maintain.py), [src/anskar/etsy/store.py](src/anskar/etsy/store.py) or pack markers, [tests/test_etsy_publish.py](tests/test_etsy_publish.py), [tests/test_daily.py](tests/test_daily.py)

- On ready-shop listing failure: keep pack `etsy_status` / `queue.json` as retry-queued with durable `etsy_last_error`. Do **not** clear that error when marking the daily run `completed`.
- `etsy maintain`: after sales/discount/expiry, publish packs that are retry-queued from a prior daily attempt only. Do not bulk-publish historical backlog that was never attempted.
- Paginate shop transactions (and receipt fallback) until exhausted, with a hard safety cap.
- Remote `sold_out` / `remove` / `removed` / quantity 0: map to a non-sale terminal state (e.g. `expired` or new `removed`), **not** `sold`, and do not call `record_sale` without a transaction id.
- Draft create: persist listing id as soon as the API returns it; on resume with empty id, avoid creating a second draft (lookup by artwork / existing draft before `create_draft_listing`).

### 3. Etsy OAuth and pack path

**Files:** [src/anskar/etsy/connect.py](src/anskar/etsy/connect.py), [src/anskar/etsy/oauth.py](src/anskar/etsy/oauth.py), [src/anskar/etsy/listing.py](src/anskar/etsy/listing.py), [tests/test_etsy_oauth.py](tests/test_etsy_oauth.py)

- Start the HTTPS callback listener **before** opening the browser.
- Only signal wait completion on the registered callback path with a parsed result (success or auth error), not on unrelated GET/404.
- Reconnect: do not return `(existing_shop_id, EXPECTED_SHOP_NAME)` without API proof. Require `--confirm-shop` and/or `get_shop` when the new login’s shop list does not match.
- Reject pack ids `.` and `..` in Etsy `load_pack` (match Instagram).

### 4. Printmaker / model / manifest

**Files:** [src/anskar/models.py](src/anskar/models.py), [src/anskar/printmaker.py](src/anskar/printmaker.py), [src/anskar/daily/pipeline.py](src/anskar/daily/pipeline.py), [tests/test_printmaker.py](tests/test_printmaker.py)

- `model_allows_publication`: missing, unknown, or not-installed model → `False`.
- Never `rmtree` a pack directory when the manifest is missing/unreadable, or when publish markers exist (`published`, `instagram_published`, `etsy_published`). Refuse instead.
- Constrain image `output_path` fallback to the same allowed roots as the metadata file.
- `_write_etsy_queue_marker`: on unreadable/non-dict manifest, do **not** write `{}` over the file; leave manifest alone and write only `etsy/queue.json` (or fail soft).

### 5. Schedule / wake

**Files:** [src/anskar/cli.py](src/anskar/cli.py), [src/anskar/daily/rehearsal.py](src/anskar/daily/rehearsal.py), [systemd/anskar-daily.timer](systemd/anskar-daily.timer), [systemd/system/anskar-daily.timer](systemd/system/anskar-daily.timer), [tests/test_rehearsal.py](tests/test_rehearsal.py), [tests/test_schedule.py](tests/test_schedule.py)

- CLI `test-cycle`: resolve noon next elapse and pass `production_next` into `schedule_test_cycle`.
- Cut `RandomizedDelaySec` on daily timers (prefer `0` or at most `1m`) so the inhibited oneshot starts soon after RTC wake. Re-install note in README/schedule docs if units must be re-copied.

### 6. Docs and small consistency

**Files:** [README.md](README.md), [AGENTS.md](AGENTS.md) only if a permanent rule changes, [src/anskar/daily/recovery.py](src/anskar/daily/recovery.py) / pipeline pack reuse

- README: remove “nothing is published”; describe daily Instagram + Etsy when ready; keep manual `--confirm` for one-off publish.
- Align `_reusable_pack` with recovery: do not reattach a pack already marked Instagram/Etsy published unless the run is clearly finishing that same publication.
- Optional small cleanups if touched: drop unused `completed_live_run_for_utc_day`, surface chmod failures on credential writes as warnings (do not leave silent 0644 without notice).

## Out of scope

- Live Etsy or Instagram publishes during development.
- Bulk backlog publish of old packs.
- Changing noon to suspend the machine.
- Re-opening the rehearsal 3h work timeout.

## Tests to add or extend

- Incomplete `pack_prepared` + `daily-run` auto-resumes (does not raise busy).
- Same Swedish day blocked when Instagram already published even if status is not `completed`.
- Rehearsal / `queue_etsy_only` stored; resume does not list when ready.
- Three `media` failures → poem skipped; another poem selected.
- Maintain publishes retry-queued pack; does not publish arbitrary unpublished backlog.
- Transaction pagination; remote removed ≠ sold.
- Etsy rejects `..`; OAuth wait ignores favicon; model missing → not publication-ready; bad manifest not wiped/rmtree’d.
- CLI rehearsal conflict when noon next is within 15 minutes.

## Done when

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

passes, and the behaviours above are covered by tests. No live publish required for this task.

## Implementation order

1. Ledger / daily auto-resume + one-a-day + queue-only + media skip (highest operational risk).
2. Etsy maintain retry + sales/draft correctness.
3. OAuth, pack id, model/manifest safety.
4. Schedule delay + rehearsal conflict wiring.
5. README/CLI text and pack-finder alignment.
6. Full unittest suite.

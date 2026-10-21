# Task 019 — Wake, paint, publish, sleep rehearsal

## Command

Diagnostic wake only (no image, no post):

```bash
PYTHONPATH=src .venv/bin/python -m anskar schedule test-cycle
```

Full rehearsal, including one real Instagram post:

```bash
PYTHONPATH=src .venv/bin/python -m anskar schedule test-cycle --publish
```

Both ask for `yes` before suspending the computer. The wake is a one-time system timer about three minutes ahead (`WakeSystem=true`), named `anskar-test-cycle.timer`. The noon units are not modified. A test artwork does not use up the Swedish-day noon slot. Etsy is queued and not listed.

## Inspect

`data/test-cycle/latest.json` holds the scheduled wake and, after the service finishes, the title, success or failure, permalink, notification result, and whether the computer was asked to sleep.

A Plasma notification is also sent. If `notify-send` is unavailable, the JSON report is still written.

## Cancel

Before the machine suspends, or after a wake if the temporary units are still installed:

```bash
PYTHONPATH=src .venv/bin/python -m anskar schedule test-cycle --cancel
```

That disables and removes only `anskar-test-cycle.service` and `anskar-test-cycle.timer`.

## Sleep

The service waits for production dependencies after wake (CPU safety config, project/Python, Ollama, sd-cli models, network, sleep inhibit). A `degraded` systemd state from an unrelated unit does not block painting. After a successful post it suspends only when the session stayed idle through a short grace period and no other sleep inhibitor is held. Any uncertainty, user activity, or failed post leaves the computer awake. The noon run still never suspends.

## Tests

Full suite: 195 tests OK. No live Instagram post was made.

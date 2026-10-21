# Task 016 — Automatic Wake, Paint & Sleep

Status: implemented in repository; **system wake not enabled** (needs explicit approval).

## What was implemented

1. **User timer templates** fixed to **12:00** noon with **`Persistent=false`** (no catch-up backlog; `anskar schedule install` no longer restores 06:30).
2. **Sleep inhibit** around `daily-run` via `systemd-inhibit` in both user and system service units.
3. **System units** under `systemd/system/` with **`WakeSystem=true`** for RTC wake from deep sleep; process runs as `babuni`, never root.
4. **`anskar schedule prepare-system`** stages rendered units in `data/systemd-system/` and prints sudo activation/rollback commands without enabling anything.
5. **`anskar schedule verify-wake`** — read-only RTC check (no suspend, no publish).
6. **Swedish local day** uniqueness for live runs (`Europe/Stockholm`).
7. **After-run power policy** — never `systemctl suspend`; if idle status is unknown or the user is active, stay awake and leave sleep to KDE.

## Current machine state (at implementation time)

- User timer **enabled** at noon (`Persistent=false`).
- System wake timer **not** installed in `/etc`.
- Hardware RTC wake previously verified by the operator.

## Return to sleep

**Depends on KDE inactivity-based suspend.** Anskar does not force suspension after a run. When the noon job finishes and the session looks idle, the service exits and normal PowerDevil/idle settings may sleep the machine. If idle status cannot be determined, the computer stays awake.

## Activation (only with your approval)

```bash
# 1. Refresh user units (optional; keeps user timer as-is unless you also --enable)
PYTHONPATH=src .venv/bin/python -m anskar schedule install

# 2. Stage system wake units (no sudo yet)
PYTHONPATH=src .venv/bin/python -m anskar schedule prepare-system

# 3. When you approve system wake (one publisher only):
systemctl --user disable --now anskar-daily.timer
sudo cp data/systemd-system/anskar-daily.service /etc/systemd/system/
sudo cp data/systemd-system/anskar-daily.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now anskar-daily.timer

# Check
systemctl list-timers anskar-daily.timer
cat /sys/class/rtc/rtc0/wakealarm || true
PYTHONPATH=src .venv/bin/python -m anskar schedule status
```

## Rollback

```bash
sudo systemctl disable --now anskar-daily.timer
sudo rm -f /etc/systemd/system/anskar-daily.service /etc/systemd/system/anskar-daily.timer
sudo systemctl daemon-reload
PYTHONPATH=src .venv/bin/python -m anskar schedule install --enable
```

## Inspect failures

```bash
# User timer
journalctl --user -u anskar-daily.service -b

# System timer (after activation)
journalctl -u anskar-daily.service -b
systemctl status anskar-daily.service
```

## KDE

Keep Plasma power settings that suspend after inactivity if you want the machine to sleep again after a noon wake. No Anskar setting replaces that.

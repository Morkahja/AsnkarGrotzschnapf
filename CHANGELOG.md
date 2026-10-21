# Changelog

Development history for Anskar Grotzschnapf. Detailed design notes stay in [`docs/tasks/`](docs/tasks/). This file is the short public log.

## [1.0.0] — 2026-10-21

First operational “set and forget” daily artist:

- Noon systemd timer selects one cleared poem, generates 1600×1600 artwork locally, packs a print, publishes to Instagram, and lists a quantity-1 SEK digital download on Etsy when the shop is `ready`.
- System wake timer (`WakeSystem=true`) can start the noon run from sleep; user and system noon timers must not both be enabled.
- `anskar-etsy-maintain.timer` syncs sales, applies the day-30 20% discount, and removes unsold listings after 42 days.
- Review hardening (task 021): auto-resume incomplete runs, one live Instagram post per Swedish day, durable Etsy retry queue, soft Instagram media failures, safer OAuth/packs/manifests, cleaner buyer listing copy.
- Etsy gallery uses preview images and detail crops; the paid download stays the full-resolution print PNG.
- Desktop notifications for daily stages (`ANSKAR_NOTIFY=0` to silence).

Earlier milestones (archive import, prompt lab, Instagram Login, Etsy OAuth, wake rehearsal) are recorded as tasks 010–020 under `docs/tasks/`.

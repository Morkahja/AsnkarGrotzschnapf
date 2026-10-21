# Task 019a — Fix Wake-Up Readiness Check

Status: implemented.

`systemctl is-system-running` may report `degraded` because of an unrelated failed unit (for example `offline-core2.service`). That alone must not stop production.

Post-wake readiness now checks Anskar's real dependencies: resume settle, expected offline CPUs (`2,10`), project/Python, Ollama (with a start attempt), sd-cli and model files, Instagram network reachability, and `systemd-inhibit`. Failures name the dependency. Bounded retries cover slow post-suspend recovery.

Ollama on this machine is a tarball install with no systemd unit. When `systemctl start ollama` fails, readiness starts `ollama serve` as the artist user and keeps probing for about two and a half minutes after wake.

The CPU safety workaround is only verified, never modified.

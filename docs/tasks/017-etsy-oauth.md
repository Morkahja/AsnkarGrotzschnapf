# Development Task 017 — Etsy OAuth Connection

Status: implemented. Authorization only; no listing create/update/delete.

## Goal

Connect the Etsy Open API v3 app **Anskar Gortzschnapf Publisher** to shop **Anskartprints** with OAuth 2.0 authorization-code + PKCE (S256), secure local credential storage, and automatic token refresh.

## Callback URL (register exactly)

```text
https://localhost:8743/etsy/callback
```

Register this exact HTTPS URL on the Etsy app (Etsy rejected `https://127.0.0.1:8743/etsy/callback`). Anskar listens once during `etsy connect` on loopback (`127.0.0.1:8743`) for that hostname, using a self-signed certificate under `~/.config/anskar/etsy-tls/` with `DNS:localhost` in the SAN. The browser will warn once about the certificate; accept it for localhost only. The listener shuts down after the callback.

## Local credentials

```bash
PYTHONPATH=src .venv/bin/python -m anskar etsy setup
```

Stores the API keystring and shared secret at `~/.config/anskar/etsy.json` with mode `0600`. Optional override: `ANSKAR_ETSY_CREDENTIALS`.

## Authorize

```bash
PYTHONPATH=src .venv/bin/python -m anskar etsy connect
```

Opens the Etsy consent page (use `--no-browser` to print the URL only). After approval, tokens and the verified shop id are stored. Seller verification state stays `pending_verification` — OAuth alone never sets `ready`.

## Verify

```bash
PYTHONPATH=src .venv/bin/python -m anskar etsy status
PYTHONPATH=src .venv/bin/python -m anskar etsy test
```

`status` shows OAuth configuration, token validity, scopes, shop identity, and the separate seller verification state without printing secrets. `test` performs read-only authenticated shop lookups.

## Out of scope

- Creating, modifying, or deleting listings
- Changing `pending_verification` to `ready`
- Daily / Instagram / Printmaker / Prompt Lab changes

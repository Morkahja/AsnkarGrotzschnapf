# Development Task 017a — Fix Etsy OAuth Callback

Status: implemented.

Etsy accepted `https://localhost:8743/etsy/callback` and rejected `https://127.0.0.1:8743/etsy/callback`.

Anskar now uses the registered localhost URL for authorize, callback validation, and token exchange. The HTTPS listener still binds only to `127.0.0.1`. TLS certs include `DNS:localhost` in the SAN; incompatible older certs are replaced safely.

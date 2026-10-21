# Development Task 011 — Instagram API integration

Status: tested locally. No Instagram post was created.

## Connection

Instagram Login uses `https://graph.instagram.com/v22.0`. The read check is `GET /me` with `user_id`, `username`, and `account_type`. Publishing, when explicitly confirmed, creates a media container, waits until it is `FINISHED`, publishes that container, and stores the media id and permalink.

The token file is `~/.config/anskar/instagram.json`, mode 600, outside this repository. Install it with `anskar instagram install-token --from FILE`. The token is not printed. Long-lived tokens last about 60 days. `anskar instagram renew` calls Meta's `ig_refresh_token` grant when fewer than seven days remain, and it refuses to rotate a token earlier than that.

The only Instagram token already on this computer belongs to `@babunigaming`, a professional Creator account. It is not `@anskargrotzschnapf`, so it was not stored for Anskar. The read itself succeeded, which confirms the profile call. `instagram_business_content_publish` was not used.

## Local post

Prepared pack: `data/instagram/lab-comparison-seed-88421021-082b1aae`.

The source pack `data/publish-packs/lab-comparison-seed-88421021-082b1aae` was not modified. The JPEG is the 1024×1024 preview converted without added text. The caption is the recorded title THE LAMB, the recorded author William Blake, the poem, and the stored AI disclosure. No Etsy link is configured. The accessibility text uses the title, author, and disclosure.

`publish --dry-run` stopped because no public https image URL is configured. A URL belongs in `~/.config/anskar/instagram-hosts.json` under `images`, preferably an approved Etsy listing image. This computer is not exposed, and no hosting service was added.

## Tests

`PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -q` ran 92 tests, all passing. They cover the matching account, an expired token, an invalid token, missing permissions, the wrong account, a missing image URL, invalid media, a duplicate post, timeouts, and a mocked publish.

## Still required before a real post

- Install the `@anskargrotzschnapf` token from a local file. Do not paste it into the chat.
- Add a public https image URL for the pack.
- Run `anskar instagram publish <pack-id> --confirm` only after those two are in place.

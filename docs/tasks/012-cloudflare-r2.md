# Development Task 012 — Cloudflare R2 publishing

Status: integrated and prepared. No Instagram post was published.

## R2 staging

Anskar stages Instagram JPEGs in the private Cloudflare R2 bucket `anskar-grotzschnapf` (region `auto`, object prefix `instagram/`, presigned GET lifetime 3600 seconds). The adapter lives in `anskar.instagram.r2` and uses boto3 with the S3v4 signature, following the same private-bucket pattern as Azeroth Moments without sharing that project's credentials or modifying that repository.

Credentials are stored outside Git at `~/.config/anskar/r2.json` (directory mode 700, file mode 600). Enter them only through the interactive terminal command. Do not paste Access Key ID, Secret Access Key, endpoint, or presigned URLs into chat or logs.

```text
anskar instagram setup-r2
anskar instagram test-r2
anskar instagram cleanup-r2
anskar instagram cleanup-r2 --delete
```

## Instagram flow

`anskar instagram publish PACK --dry-run` prepares the local JPEG, uploads it to R2, probes the presigned URL, deletes the temporary object, and does not call the publishing API.

`anskar instagram publish PACK --confirm` uploads to R2, creates a media container, waits until it is ready, publishes after the explicit confirm, records the media id and permalink, then deletes the temporary R2 object. Failed or timed-out attempts keep the staged object and its ledger `staging_key` until reconciled or cleaned up.

The expected account remains `@anskargrotzschnapf`. `@babunigaming` is never used.

## Prepared post (not published)

Pack: `lab-comparison-seed-88421021-082b1aae`

Local post: `data/instagram/lab-comparison-seed-88421021-082b1aae/`

- Artwork approved; pack `publication_ready` is true
- Poem: THE LAMB by William Blake (documented Swedish commercial-use rights)
- JPEG: 1024×1024, no added text
- Caption includes title, author, poem, and AI disclosure
- Etsy link: not configured

After R2 credentials are stored and `test-r2` passes, publish this exact post with:

```text
PYTHONPATH=src .venv/bin/python -m anskar instagram publish lab-comparison-seed-88421021-082b1aae --confirm
```

## Tests

`PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -q` — all passing, including R2 setup permission checks, dry-run staging, failed-publish retention, and successful deletion after a mocked publish.

## Blockers before public publication

1. Run `anskar instagram setup-r2` in a local terminal (Access Key ID, Secret Access Key, S3 endpoint for bucket `anskar-grotzschnapf`).
2. Run `anskar instagram test-r2` and confirm upload / presigned GET / delete.
3. Explicitly approve publication, then run the `--confirm` command above.

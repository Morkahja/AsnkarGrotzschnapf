"""Private Cloudflare R2 staging for Instagram image URLs.

Credentials live outside the repository. Presigned URLs and secrets are never
logged. The adapter follows the same private-bucket pattern as Azeroth Moments
without sharing that project's credentials.
"""

from __future__ import annotations

import getpass
import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from anskar.instagram.errors import InstagramError, redact

log = logging.getLogger(__name__)

BUCKET = "anskar-grotzschnapf"
REGION = "auto"
OBJECT_PREFIX = "instagram/"
PRESIGN_SECONDS = 3600
CONTENT_TYPE = "image/jpeg"


@dataclass(frozen=True)
class R2Credentials:
    access_key_id: str
    secret_access_key: str
    endpoint_url: str
    bucket: str = BUCKET
    region: str = REGION
    path: Path | None = None

    def public(self) -> dict[str, object]:
        return {
            "path": str(self.path) if self.path else None,
            "bucket": self.bucket,
            "region": self.region,
            "endpoint_host": _endpoint_host(self.endpoint_url),
            "object_prefix": OBJECT_PREFIX,
            "presign_seconds": PRESIGN_SECONDS,
            "configured": True,
        }


@dataclass(frozen=True)
class StagedObject:
    key: str
    url: str


def r2_credentials_path() -> Path:
    override = os.environ.get("ANSKAR_R2_CREDENTIALS")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "anskar" / "r2.json"


def r2_configured(path: Path | None = None) -> bool:
    file = path or r2_credentials_path()
    return file.is_file()


def load_r2_credentials(path: Path | None = None) -> R2Credentials:
    file = path or r2_credentials_path()
    if not file.is_file():
        raise InstagramError(
            "No R2 credentials are stored. Run `anskar instagram setup-r2` in a terminal "
            "and enter the Access Key ID, Secret Access Key, and S3 endpoint. "
            "Do not paste those values into the chat.",
            kind="credentials",
        )
    try:
        payload = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstagramError("The R2 credential file could not be read.", kind="credentials") from exc
    if not isinstance(payload, dict):
        raise InstagramError("The R2 credential file is not a record.", kind="credentials")
    access = payload.get("access_key_id")
    secret = payload.get("secret_access_key")
    endpoint = payload.get("endpoint_url")
    if not isinstance(access, str) or not access.strip():
        raise InstagramError("The R2 credential file has no access key id.", kind="credentials")
    if not isinstance(secret, str) or not secret.strip():
        raise InstagramError("The R2 credential file has no secret access key.", kind="credentials")
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise InstagramError("The R2 credential file has no S3 endpoint.", kind="credentials")
    bucket = payload.get("bucket")
    region = payload.get("region")
    return R2Credentials(
        access_key_id=access.strip(),
        secret_access_key=secret.strip(),
        endpoint_url=_normalize_endpoint(endpoint.strip()),
        bucket=bucket.strip() if isinstance(bucket, str) and bucket.strip() else BUCKET,
        region=region.strip() if isinstance(region, str) and region.strip() else REGION,
        path=file,
    )


def setup_r2_interactive(
    *,
    destination: Path | None = None,
    input_fn=input,
    getpass_fn=getpass.getpass,
) -> R2Credentials:
    """Prompt on the terminal. Never log the answers."""
    print("Cloudflare R2 setup for Anskar.")
    print(f"Bucket: {BUCKET}")
    print(f"Region: {REGION}")
    print("Enter values for this account only. They are stored outside Git.")
    access = input_fn("R2 Access Key ID: ").strip()
    secret = getpass_fn("R2 Secret Access Key: ").strip()
    endpoint = input_fn("R2 S3 endpoint (https://…r2.cloudflarestorage.com): ").strip()
    if not access or not secret or not endpoint:
        raise InstagramError("Access key, secret, and endpoint are all required.", kind="credentials")
    stored = R2Credentials(
        access_key_id=access,
        secret_access_key=secret,
        endpoint_url=_normalize_endpoint(endpoint),
        path=destination or r2_credentials_path(),
    )
    _write_credentials(stored)
    return stored


def _write_credentials(credentials: R2Credentials) -> None:
    file = credentials.path or r2_credentials_path()
    payload = {
        "provider": "cloudflare-r2",
        "access_key_id": credentials.access_key_id,
        "secret_access_key": credentials.secret_access_key,
        "endpoint_url": credentials.endpoint_url,
        "bucket": credentials.bucket,
        "region": credentials.region,
        "object_prefix": OBJECT_PREFIX,
        "presign_seconds": PRESIGN_SECONDS,
    }
    file.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(file.parent, 0o700)
    except OSError:
        pass
    temporary = file.with_suffix(file.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    temporary.replace(file)
    try:
        os.chmod(file, 0o600)
    except OSError:
        pass


class R2StagingAdapter:
    """Upload a private JPEG and return a short-lived HTTPS GET URL."""

    def __init__(self, credentials: R2Credentials | None = None, *, client=None) -> None:
        self.credentials = credentials or load_r2_credentials()
        self._client = client
        self.bucket = self.credentials.bucket
        self.expires_in = PRESIGN_SECONDS

    def _s3(self):
        if self._client is not None:
            return self._client
        try:
            import boto3
            from botocore.client import Config as BotoConfig
        except ImportError as exc:
            raise InstagramError(
                "boto3 is not installed. Install it in the project environment before staging.",
                kind="credentials",
            ) from exc
        session = boto3.session.Session(
            aws_access_key_id=self.credentials.access_key_id,
            aws_secret_access_key=self.credentials.secret_access_key,
            region_name=self.credentials.region,
        )
        self._client = session.client(
            "s3",
            endpoint_url=self.credentials.endpoint_url,
            config=BotoConfig(signature_version="s3v4"),
        )
        return self._client

    def object_key(self, pack_id: str, *, suffix: str = ".jpg") -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in pack_id).strip("-") or "pack"
        return f"{OBJECT_PREFIX}{safe}/{stamp}-{uuid.uuid4().hex[:12]}{suffix}"

    def upload(self, path: Path, key: str | None = None, *, content_type: str = CONTENT_TYPE) -> StagedObject:
        if not path.is_file():
            raise InstagramError("The Instagram JPEG is missing.", kind="media")
        object_key = key or self.object_key(path.stem)
        client = self._s3()
        try:
            client.upload_file(
                str(path),
                self.bucket,
                object_key,
                ExtraArgs={"ContentType": content_type},
            )
            url = client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": object_key},
                ExpiresIn=self.expires_in,
            )
        except Exception as exc:  # noqa: BLE001
            raise InstagramError(
                redact(f"R2 staging failed: {exc}", self.credentials.secret_access_key),
                kind="url",
            ) from exc
        if not isinstance(url, str) or not url.startswith("https://"):
            raise InstagramError("R2 did not return an https presigned URL.", kind="url")
        log.info("Staged object key=%s", object_key)
        return StagedObject(key=object_key, url=url)

    def delete(self, key: str) -> None:
        try:
            self._s3().delete_object(Bucket=self.bucket, Key=key)
            log.info("Deleted staged object key=%s", key)
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to delete staged key %s: %s", key, type(exc).__name__)

    def exists(self, key: str) -> bool:
        try:
            self._s3().head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:  # noqa: BLE001
            return False

    def list_prefix(self, prefix: str = OBJECT_PREFIX) -> list[str]:
        client = self._s3()
        keys: list[str] = []
        continuation = None
        while True:
            kwargs = {"Bucket": self.bucket, "Prefix": prefix}
            if continuation:
                kwargs["ContinuationToken"] = continuation
            response = client.list_objects_v2(**kwargs)
            for item in response.get("Contents") or []:
                key = item.get("Key")
                if isinstance(key, str) and key:
                    keys.append(key)
            if not response.get("IsTruncated"):
                break
            continuation = response.get("NextContinuationToken")
        return keys


def stage_jpeg(
    path: Path,
    *,
    pack_id: str,
    adapter: R2StagingAdapter | None = None,
) -> StagedObject:
    staging = adapter or R2StagingAdapter()
    key = staging.object_key(pack_id)
    return staging.upload(path, key)


def smoke_test(adapter: R2StagingAdapter | None = None) -> dict[str, object]:
    """Upload, fetch without AWS auth headers, delete, and confirm deletion."""
    import tempfile
    import urllib.request

    staging = adapter or R2StagingAdapter()
    # Minimal JPEG (1×1) written with ImageMagick when available, else a tiny valid JPEG.
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "r2-smoke.jpg"
        _write_tiny_jpeg(path)
        key = f"{OBJECT_PREFIX}smoke/{uuid.uuid4().hex}.jpg"
        staged = staging.upload(path, key)
        request = urllib.request.Request(staged.url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                status = response.status
                body = response.read(64)
                content_type = response.headers.get("Content-Type", "")
        except Exception as exc:  # noqa: BLE001
            staging.delete(key)
            raise InstagramError(
                redact(f"Presigned GET failed: {exc}", staging.credentials.secret_access_key),
                kind="url",
            ) from exc
        if status >= 400 or not body.startswith(b"\xff\xd8"):
            staging.delete(key)
            raise InstagramError("The presigned URL did not return a JPEG.", kind="url")
        if "image" not in content_type.lower() and content_type:
            staging.delete(key)
            raise InstagramError("The presigned URL did not return an image content type.", kind="url")
        staging.delete(key)
        still_there = staging.exists(key)
        if still_there:
            raise InstagramError("The smoke-test object was still present after delete.", kind="url")
    return {
        "ok": True,
        "bucket": staging.bucket,
        "endpoint_host": _endpoint_host(staging.credentials.endpoint_url),
        "deleted": True,
        "fetched_without_aws_headers": True,
    }


def cleanup_abandoned(
    *,
    adapter: R2StagingAdapter | None = None,
    database: Path | None = None,
    delete: bool = False,
) -> dict[str, object]:
    """List R2 keys under the Instagram prefix that are no longer active."""
    from anskar.instagram.ledger import connect, ledger_path

    staging = adapter or R2StagingAdapter()
    keys = staging.list_prefix(OBJECT_PREFIX)
    active: set[str] = set()
    with connect(database or ledger_path()) as connection:
        rows = connection.execute(
            """
            SELECT staging_key FROM attempts
            WHERE staging_key IS NOT NULL
              AND state IN ('publishing', 'container_created', 'unknown', 'dry_run')
            """
        ).fetchall()
        for row in rows:
            if row["staging_key"]:
                active.add(str(row["staging_key"]))
    abandoned = [key for key in keys if key not in active and "/smoke/" not in key]
    deleted: list[str] = []
    if delete:
        for key in abandoned:
            staging.delete(key)
            deleted.append(key)
    return {
        "listed": len(keys),
        "active": len(active),
        "abandoned": len(abandoned),
        "deleted": len(deleted),
        "abandoned_keys": abandoned if not delete else deleted,
    }


def _normalize_endpoint(endpoint: str) -> str:
    value = endpoint.strip().rstrip("/")
    if not value.startswith("https://"):
        raise InstagramError("The R2 endpoint must be an https URL.", kind="credentials")
    return value


def _endpoint_host(endpoint: str) -> str:
    from urllib.parse import urlparse

    return urlparse(endpoint).hostname or "unknown"


def _write_tiny_jpeg(path: Path) -> None:
    import shutil
    import subprocess

    magick = shutil.which("magick")
    if magick:
        completed = subprocess.run(
            [magick, "-size", "8x8", "xc:#8899aa", "-quality", "90", str(path)],
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if completed.returncode == 0 and path.is_file():
            return
    # Minimal JFIF JPEG.
    path.write_bytes(
        bytes.fromhex(
            "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707"
            "070909080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c"
            "1c2837292c30313434341f27393d38323c2e333432ffdb0043010909090c0b0c180d"
            "0d1832211c2132323232323232323232323232323232323232323232323232323232"
            "323232323232323232323232323232323232ffc00011080008000803011100021101"
            "031101ffc40014000100000000000000000000000000000008ffc400141001000000"
            "00000000000000000000000000ffda000c0301000210031000003f00bf80ffd9"
        )
    )

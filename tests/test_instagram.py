import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from anskar.cli import main
from anskar.instagram.credentials import Credentials, install_token, load_credentials
from anskar.instagram.errors import InstagramError, redact
from anskar.instagram.graph import GRAPH_HOST, GraphClient, HttpResponse
from anskar.instagram.hosting import validate_public_url
from anskar.instagram.jpeg import jpeg_dimensions, validate_jpeg
from anskar.instagram.r2 import R2Credentials, R2StagingAdapter, StagedObject, setup_r2_interactive
from anskar.instagram.service import connection_status, prepare, publish, renew_token

TOKEN = "test-token-value-not-a-real-secret"
PACK = "lab-lamb"


class ScriptedTransport:
    def __init__(self):
        self.calls = []
        self._steps = []

    def add(self, method: str, suffix: str, action):
        self._steps.append((method, suffix, action))

    def __call__(self, method: str, url: str, params: dict, timeout: float) -> HttpResponse:
        self.calls.append((method, url, {key: value for key, value in params.items() if key != "access_token"}))
        for index, (expected_method, suffix, action) in enumerate(self._steps):
            if method == expected_method and suffix in url:
                self._steps.pop(index)
                if isinstance(action, Exception):
                    raise action
                return action
        raise AssertionError(f"unexpected {method} {url}")


class InstagramTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.packs = self.root / "packs"
        self.posts = self.root / "posts"
        self.database = self.root / "instagram.sqlite"
        self.credentials_path = self.root / "instagram.json"
        self.token = TOKEN

    def tearDown(self):
        self.temporary.cleanup()

    def test_status_confirms_the_expected_account(self):
        transport = ScriptedTransport()
        transport.add("GET", "/me", _json({"user_id": "1789", "username": "anskargrotzschnapf", "account_type": "MEDIA_CREATOR"}))
        report = connection_status(self._credentials(), GraphClient(self.token, transport=transport))
        self.assertEqual(report["username"], "anskargrotzschnapf")
        self.assertTrue(report["account_match"])
        self.assertEqual(report["permissions"]["instagram_business_basic"], "confirmed")
        self.assertNotIn(self.token, json.dumps(report))
        stored = json.loads(self.credentials_path.read_text(encoding="utf-8"))
        self.assertEqual(stored["user_id"], "1789")
        self.assertEqual(stat.S_IMODE(self.credentials_path.stat().st_mode) & 0o077, 0)

    def test_expired_and_invalid_tokens_are_distinct(self):
        expired = self._profile_error(190, 463, "Error validating access token")
        with self.assertRaises(InstagramError) as caught:
            connection_status(self._credentials(), expired)
        self.assertEqual(caught.exception.kind, "expired")
        self.assertNotIn(self.token, str(caught.exception))
        invalid = self._profile_error(190, None, f"bad token {self.token}")
        with self.assertRaises(InstagramError) as caught:
            connection_status(self._credentials(), invalid)
        self.assertEqual(caught.exception.kind, "invalid_token")
        self.assertNotIn(self.token, str(caught.exception))

    def test_missing_permission_names_the_required_scopes(self):
        client = self._profile_error(10, None, "Application does not have permission")
        with self.assertRaises(InstagramError) as caught:
            connection_status(self._credentials(), client)
        self.assertEqual(caught.exception.kind, "permission")
        self.assertIn("instagram_business_content_publish", str(caught.exception))

    def test_wrong_account_is_refused_before_a_container_is_created(self):
        self._pack()
        transport = ScriptedTransport()
        transport.add("GET", "/me", _json({"user_id": "42", "username": "someoneelse", "account_type": "MEDIA_CREATOR"}))
        with self.assertRaises(InstagramError) as caught:
            publish(
                PACK,
                confirm=True,
                packs=self.packs,
                destination=self.posts / PACK,
                hosts=_hosts(),
                database=self.database,
                credentials=self._credentials(),
                client=GraphClient(self.token, transport=transport),
                staging=FakeStaging(),
                jpeg_writer=_writer(),
                probe=lambda url: None,
                sleep=lambda seconds: None,
            )
        self.assertEqual(caught.exception.kind, "account")
        self.assertNotIn("someoneelse/media", " ".join(url for _, url, _ in transport.calls))
        self.assertFalse(any(method == "POST" for method, _, _ in transport.calls))

    def test_missing_hosts_url_still_prepares_and_stages_through_r2(self):
        preview = self._pack()
        before = preview.read_bytes()
        record = prepare(
            PACK,
            packs=self.packs,
            destination=self.posts / PACK,
            hosts={"images": {}, "etsy_shop_url": None},
            database=self.database,
            jpeg_writer=_writer(),
        )
        self.assertIsNone(record["public_image_url"])
        self.assertTrue((self.posts / PACK / "image.jpg").is_file())
        self.assertEqual(preview.read_bytes(), before)
        self.assertEqual(record["title"], "THE LAMB")
        self.assertIn("William Blake", record["caption"])
        self.assertIn("generated locally", record["caption"])
        self.assertNotIn(self.token, json.dumps(record))
        staging = FakeStaging()
        result = publish(
            PACK,
            dry_run=True,
            packs=self.packs,
            destination=self.posts / PACK,
            hosts={"images": {}, "etsy_shop_url": None},
            database=self.database,
            jpeg_writer=_writer(),
            staging=staging,
            probe=lambda url: None,
        )
        self.assertTrue(result["dry_run"])
        self.assertTrue(result["staged"])
        self.assertTrue(result["staging_deleted"])
        self.assertEqual(staging.uploads, 1)
        self.assertEqual(staging.deletes, 1)
        self.assertNotIn("public_image_url", result)

    def test_invalid_media_is_rejected(self):
        self._pack()
        with self.assertRaises(InstagramError) as caught:
            prepare(
                PACK,
                packs=self.packs,
                destination=self.posts / PACK,
                hosts=_hosts(),
                database=self.database,
                jpeg_writer=_writer(100, 100),
            )
        self.assertEqual(caught.exception.kind, "media")

    def test_private_and_local_urls_are_rejected(self):
        for url in (
            "http://cdn.example/art.jpg",
            "https://127.0.0.1/art.jpg",
            "https://user:pass@cdn.example/art.jpg",
            "https://localhost/art.jpg",
        ):
            with self.assertRaises(InstagramError):
                validate_public_url(url)

    def test_dry_run_does_not_call_instagram(self):
        self._pack()
        staging = FakeStaging()
        result = publish(
            PACK,
            dry_run=True,
            packs=self.packs,
            destination=self.posts / PACK,
            hosts=_hosts(),
            database=self.database,
            jpeg_writer=_writer(),
            staging=staging,
            probe=lambda url: None,
        )
        self.assertTrue(result["dry_run"])
        self.assertFalse(result["published"])
        self.assertIn("THE LAMB", result["caption"])
        self.assertTrue(result["staged"])
        self.assertEqual(staging.uploads, 1)
        self.assertEqual(staging.deletes, 1)
        self.assertNotIn("public_image_url", result)

    def test_publish_requires_an_explicit_confirm(self):
        self._pack()
        with self.assertRaises(InstagramError) as caught:
            publish(
                PACK,
                packs=self.packs,
                destination=self.posts / PACK,
                hosts=_hosts(),
                database=self.database,
                jpeg_writer=_writer(),
                staging=FakeStaging(),
                probe=lambda url: None,
            )
        self.assertIn("--confirm", str(caught.exception))

    def test_mocked_publish_records_the_media_id_and_blocks_a_second_post(self):
        self._pack()
        transport = self._success_transport()
        client = GraphClient(self.token, transport=transport)
        staging = FakeStaging()
        result = self._confirm(client, staging=staging)
        self.assertEqual(result["media_id"], "media-1")
        self.assertEqual(result["permalink"], "https://www.instagram.com/p/example/")
        self.assertTrue(result["staging_deleted"])
        self.assertEqual(staging.uploads, 1)
        self.assertEqual(staging.deletes, 1)
        self.assertTrue(any(params.get("alt_text", "").startswith("Artwork titled THE LAMB") for _, _, params in transport.calls))
        self.assertTrue(any("X-Amz-Signature" in str(params.get("image_url", "")) for _, _, params in transport.calls))
        calls = len(transport.calls)
        with self.assertRaises(InstagramError) as caught:
            self._confirm(client, staging=FakeStaging())
        self.assertEqual(caught.exception.kind, "duplicate")
        self.assertEqual(len(transport.calls), calls)

    def test_failed_publish_keeps_the_staged_object(self):
        self._pack()
        transport = ScriptedTransport()
        transport.add("GET", "/me", _profile())
        transport.add(
            "POST",
            "/media",
            _error(400, 10, None, "permission denied"),
        )
        staging = FakeStaging()
        with self.assertRaises(InstagramError):
            self._confirm(GraphClient(self.token, transport=transport), staging=staging)
        self.assertEqual(staging.uploads, 1)
        self.assertEqual(staging.deletes, 0)
        attempts = _attempts(self.database)
        self.assertTrue(any(row.get("staging_key") for row in attempts if row["state"] == "failed"))

    def test_r2_setup_writes_restricted_credentials(self):
        destination = self.root / "r2.json"
        inputs = iter(
            [
                "ANSKARTESTKEY",
                "https://accountid.r2.cloudflarestorage.com",
            ]
        )
        secrets = iter(["super-secret-value"])
        with patch("sys.stdout", StringIO()):
            stored = setup_r2_interactive(
                destination=destination,
                input_fn=lambda prompt: next(inputs),
                getpass_fn=lambda prompt: next(secrets),
            )
        self.assertEqual(stored.access_key_id, "ANSKARTESTKEY")
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode) & 0o077, 0)
        payload = json.loads(destination.read_text(encoding="utf-8"))
        self.assertEqual(payload["bucket"], "anskar-grotzschnapf")
        self.assertEqual(payload["region"], "auto")
        public = stored.public()
        self.assertNotIn("super-secret-value", json.dumps(public))
        self.assertEqual(public["endpoint_host"], "accountid.r2.cloudflarestorage.com")

    def test_redact_hides_presigned_urls(self):
        url = "https://bucket.example/key?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=abc"
        self.assertNotIn("X-Amz-Signature", redact(url))

    def test_permission_failure_is_kept_and_redacted(self):
        self._pack()
        transport = ScriptedTransport()
        transport.add("GET", "/me", _json({"user_id": "1789", "username": "anskargrotzschnapf", "account_type": "MEDIA_CREATOR"}))
        transport.add(
            "POST",
            "/media",
            _error(400, 10, None, f"permission denied for {self.token}"),
        )
        with self.assertRaises(InstagramError) as caught:
            self._confirm(GraphClient(self.token, transport=transport), staging=FakeStaging())
        self.assertEqual(caught.exception.kind, "permission")
        self.assertNotIn(self.token, str(caught.exception))
        message = json.dumps(_attempts(self.database))
        self.assertIn("failed", message)
        self.assertNotIn(self.token, message)

    def test_timeout_is_checked_before_another_container(self):
        self._pack()
        first = ScriptedTransport()
        first.add("GET", "/me", _profile())
        first.add("POST", "/media", TimeoutError("timed out"))
        with self.assertRaises(InstagramError) as caught:
            self._confirm(GraphClient(self.token, transport=first), staging=FakeStaging())
        self.assertEqual(caught.exception.kind, "timeout")
        second = ScriptedTransport()
        second.add("GET", "/me", _profile())
        second.add("GET", "/media", _json({"data": []}))
        with self.assertRaises(InstagramError) as caught:
            self._confirm(GraphClient(self.token, transport=second), staging=FakeStaging())
        self.assertEqual(caught.exception.kind, "unknown")
        self.assertFalse(any(method == "POST" for method, _, _ in second.calls))

    def test_a_timed_out_publish_is_recovered_instead_of_repeated(self):
        self._pack()
        first = ScriptedTransport()
        first.add("GET", "/me", _profile())
        first.add("POST", "/media", _json({"id": "container-1"}))
        first.add("GET", "/container-1", _json({"id": "container-1", "status_code": "FINISHED"}))
        first.add("POST", "/media_publish", TimeoutError("timed out"))
        kept = FakeStaging()
        with self.assertRaises(InstagramError):
            self._confirm(GraphClient(self.token, transport=first), staging=kept)
        self.assertEqual(kept.deletes, 0)
        second = ScriptedTransport()
        second.add("GET", "/me", _profile())
        second.add("GET", "/container-1", _json({"id": "media-9", "status_code": "PUBLISHED"}))
        second.add("GET", "/media-9", _json({"id": "media-9", "permalink": "https://www.instagram.com/p/recovered/"}))
        result = self._confirm(GraphClient(self.token, transport=second), staging=kept)
        self.assertTrue(result["recovered"])
        self.assertEqual(result["media_id"], "media-9")
        self.assertFalse(any(method == "POST" for method, _, _ in second.calls))
        self.assertEqual(kept.deletes, 1)

    def test_renewal_waits_until_the_last_week_and_hides_the_token(self):
        later = datetime.now(timezone.utc) + timedelta(days=40)
        credentials = self._credentials(expires_at=later.isoformat())
        with self.assertRaises(InstagramError):
            renew_token(credentials=credentials, now=datetime.now(timezone.utc))
        soon = datetime.now(timezone.utc) + timedelta(days=3)
        credentials = self._credentials(expires_at=soon.isoformat())
        transport = ScriptedTransport()
        replacement = "replacement-token-value-also-fake"
        transport.add("GET", "/refresh_access_token", _json({"access_token": replacement, "expires_in": 5184000}))
        report = renew_token(
            credentials=credentials,
            client=GraphClient(self.token, transport=transport),
            now=datetime.now(timezone.utc),
        )
        self.assertTrue(report["renewed"])
        self.assertNotIn(replacement, json.dumps(report))
        self.assertNotIn(self.token, json.dumps(report))
        stored = load_credentials(self.credentials_path)
        self.assertEqual(stored.access_token, replacement)

    def test_unmatched_pack_does_not_invent_a_poem(self):
        self._pack(matched=False)
        record = prepare(
            PACK,
            packs=self.packs,
            destination=self.posts / PACK,
            hosts={"images": {}},
            database=self.database,
            jpeg_writer=_writer(),
        )
        self.assertIsNone(record["title"])
        self.assertNotIn("Untitled", record["caption"])
        self.assertNotIn("William Blake", record["caption"])
        self.assertFalse(record["publishable"])

    def test_install_token_command_does_not_print_the_token(self):
        source = self.root / "token.txt"
        source.write_text(self.token + "\n", encoding="utf-8")
        output = StringIO()
        with patch.dict(os.environ, {"ANSKAR_INSTAGRAM_CREDENTIALS": str(self.credentials_path)}):
            with patch("sys.stdout", output):
                self.assertEqual(main(["instagram", "install-token", "--from", str(source)]), 0)
        self.assertNotIn(self.token, output.getvalue())
        self.assertIn("not printed", output.getvalue())
        self.assertEqual(load_credentials(self.credentials_path).access_token, self.token)

    def test_redact_removes_query_tokens(self):
        self.assertNotIn(self.token, redact(f"https://graph.instagram.com/me?access_token={self.token}", self.token))
        self.assertTrue(GRAPH_HOST.startswith("https://graph.instagram.com"))

    def test_jpeg_limits(self):
        path = self.root / "tiny.jpg"
        path.write_bytes(_jpeg(320, 320))
        self.assertEqual(validate_jpeg(path), (320, 320))
        self.assertEqual(jpeg_dimensions(_jpeg(800, 1000)), (800, 1000))

    def _confirm(self, client: GraphClient, *, staging: FakeStaging | None = None) -> dict:
        return publish(
            PACK,
            confirm=True,
            packs=self.packs,
            destination=self.posts / PACK,
            hosts=_hosts(),
            database=self.database,
            credentials=self._credentials(),
            client=client,
            staging=staging or FakeStaging(),
            jpeg_writer=_writer(),
            probe=lambda url: None,
            sleep=lambda seconds: None,
        )

    def _credentials(self, expires_at: str | None = None) -> Credentials:
        credentials = Credentials(access_token=self.token, expires_at=expires_at, path=self.credentials_path)
        from anskar.instagram.credentials import _write

        _write(credentials)
        return credentials

    def _profile_error(self, code: int, subcode: int | None, message: str) -> GraphClient:
        transport = ScriptedTransport()
        transport.add("GET", "/me", _error(400, code, subcode, message))
        return GraphClient(self.token, transport=transport)

    def _success_transport(self) -> ScriptedTransport:
        transport = ScriptedTransport()
        transport.add("GET", "/me", _profile())
        transport.add("POST", "/media", _json({"id": "container-1"}))
        transport.add("GET", "/container-1", _json({"id": "container-1", "status_code": "FINISHED"}))
        transport.add("POST", "/media_publish", _json({"id": "media-1"}))
        transport.add("GET", "/media-1", _json({"id": "media-1", "permalink": "https://www.instagram.com/p/example/"}))
        return transport

    def _pack(self, *, matched: bool = True) -> Path:
        folder = self.packs / PACK / "instagram"
        folder.mkdir(parents=True)
        preview = folder / "preview.png"
        preview.write_bytes(b"preview-bytes")
        rights = {
            "matched": matched,
            "title": "THE LAMB" if matched else None,
            "author": "William Blake" if matched else None,
        }
        manifest = {
            "artwork_id": PACK,
            "publication_ready": matched,
            "rights": rights,
            "source_text": "Little lamb, who made thee?" if matched else "not a poem",
            "ai_disclosure": "This image was generated locally by software.",
        }
        (self.packs / PACK / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return preview


def _hosts() -> dict:
    return {"images": {}, "etsy_shop_url": None}


class FakeStaging(R2StagingAdapter):
    def __init__(self) -> None:
        self.credentials = R2Credentials(
            access_key_id="test",
            secret_access_key="test-secret",
            endpoint_url="https://example.r2.cloudflarestorage.com",
        )
        self.bucket = "anskar-grotzschnapf"
        self.expires_in = 3600
        self._client = object()
        self.objects: dict[str, bytes] = {}
        self.uploads = 0
        self.deletes = 0
        self.last_url = ""

    def upload(self, path: Path, key: str | None = None, *, content_type: str = "image/jpeg") -> StagedObject:
        object_key = key or self.object_key(path.stem)
        self.objects[object_key] = path.read_bytes()
        self.uploads += 1
        url = (
            f"https://example.r2.cloudflarestorage.com/{object_key}"
            f"?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=test"
        )
        self.last_url = url
        return StagedObject(key=object_key, url=url)

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)
        self.deletes += 1

    def exists(self, key: str) -> bool:
        return key in self.objects


def _writer(width: int = 1024, height: int = 1024):
    def write(source: Path, destination: Path) -> None:
        destination.write_bytes(_jpeg(width, height))

    return write


def _jpeg(width: int, height: int) -> bytes:
    payload = bytes([8]) + height.to_bytes(2, "big") + width.to_bytes(2, "big") + bytes([1, 1, 0x11, 0])
    segment = b"\xff\xc0" + (len(payload) + 2).to_bytes(2, "big") + payload
    return b"\xff\xd8" + segment + b"\xff\xd9"


def _json(payload: dict) -> HttpResponse:
    return HttpResponse(200, json.dumps(payload).encode("utf-8"), {})


def _error(status: int, code: int, subcode: int | None, message: str) -> HttpResponse:
    error = {"message": message, "type": "OAuthException", "code": code, "fbtrace_id": "trace"}
    if subcode is not None:
        error["error_subcode"] = subcode
    return HttpResponse(status, json.dumps({"error": error}).encode("utf-8"), {})


def _profile() -> HttpResponse:
    return _json({"user_id": "1789", "username": "anskargrotzschnapf", "account_type": "MEDIA_CREATOR"})


def _attempts(database: Path) -> list:
    from anskar.instagram.service import list_pack_attempts

    return list_pack_attempts(PACK, database=database)

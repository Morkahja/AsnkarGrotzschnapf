"""Etsy OAuth 2.0 + PKCE and credential storage tests. No live Etsy calls."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from anskar.etsy.client import EtsyClient
from anskar.etsy.connect import connect_etsy, etsy_connection_status, test_etsy_connection
from anskar.etsy.credentials import (
    EtsyCredentials,
    ensure_fresh_credentials,
    etsy_configured,
    load_app_credentials,
    load_etsy_credentials,
    save_oauth_tokens,
    setup_etsy_interactive,
    shop_id_matches,
    shop_identity_matches,
    shop_name_matches,
)
from anskar.etsy.errors import EtsyError, redact
from anskar.etsy.oauth import (
    DEFAULT_REDIRECT_URI,
    DEFAULT_SCOPES,
    EXPECTED_SHOP_ID,
    EXPECTED_SHOP_NAME,
    LEGACY_REDIRECT_URIS,
    LOOPBACK_BIND_HOST,
    TokenResponse,
    begin_auth_request,
    build_authorize_url,
    ensure_localhost_tls,
    exchange_authorization_code,
    generate_pkce,
    generate_state,
    normalize_redirect_uri,
    parse_callback_query,
    refresh_access_token,
)
from anskar.etsy.state import PENDING_VERIFICATION, READY, load_etsy_state, set_etsy_state


class EtsyOauthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="anskar-etsy-oauth-"))
        self.credentials_path = self.root / "etsy.json"
        self.state_path = self.root / "etsy-state.json"
        self.tls_dir = self.root / "tls"
        set_etsy_state(PENDING_VERIFICATION, path=self.state_path)
        self.env = patch.dict(
            os.environ,
            {
                "ANSKAR_ETSY_CREDENTIALS": str(self.credentials_path),
                "ANSKAR_ETSY_STATE": str(self.state_path),
                "ANSKAR_ETSY_TLS_DIR": str(self.tls_dir),
            },
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()

    def test_pkce_s256_challenge_matches_verifier(self) -> None:
        pair = generate_pkce()
        self.assertGreaterEqual(len(pair.verifier), 43)
        self.assertLessEqual(len(pair.verifier), 128)
        digest = hashlib.sha256(pair.verifier.encode("ascii")).digest()
        expected = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        self.assertEqual(pair.challenge, expected)
        self.assertTrue(all(ch.isalnum() or ch in "-._~" for ch in pair.verifier))

    def test_state_is_random_and_single_use_validated(self) -> None:
        a = generate_state()
        b = generate_state()
        self.assertNotEqual(a, b)
        self.assertGreaterEqual(len(a), 32)
        code = parse_callback_query(f"code=auth-code-1&state={a}", expected_state=a)
        self.assertEqual(code, "auth-code-1")
        with self.assertRaises(EtsyError) as ctx:
            parse_callback_query(f"code=auth-code-1&state={a}", expected_state=b)
        self.assertIn("state", str(ctx.exception).lower())

    def test_http_redirect_uri_is_rejected(self) -> None:
        with self.assertRaises(EtsyError) as ctx:
            build_authorize_url(
                client_id="key",
                redirect_uri="http://127.0.0.1:8743/etsy/callback",
                scopes=DEFAULT_SCOPES,
                state="s",
                code_challenge="c",
            )
        self.assertIn("https", str(ctx.exception).lower())

    def test_default_redirect_uri_is_registered_localhost(self) -> None:
        self.assertEqual(DEFAULT_REDIRECT_URI, "https://localhost:8743/etsy/callback")
        self.assertIn("https://127.0.0.1:8743/etsy/callback", LEGACY_REDIRECT_URIS)
        self.assertEqual(LOOPBACK_BIND_HOST, "127.0.0.1")
        self.assertEqual(
            normalize_redirect_uri("https://127.0.0.1:8743/etsy/callback"),
            DEFAULT_REDIRECT_URI,
        )

    def test_authorize_url_includes_pkce_and_scopes(self) -> None:
        auth = begin_auth_request(client_id="keystring", redirect_uri=DEFAULT_REDIRECT_URI)
        self.assertEqual(auth.redirect_uri, DEFAULT_REDIRECT_URI)
        self.assertTrue(auth.authorize_url.startswith("https://www.etsy.com/oauth/connect?"))
        self.assertIn("redirect_uri=https%3A%2F%2Flocalhost%3A8743%2Fetsy%2Fcallback", auth.authorize_url)
        self.assertIn("code_challenge_method=S256", auth.authorize_url)
        self.assertIn("code_challenge=", auth.authorize_url)
        self.assertIn("listings_r", auth.authorize_url)
        self.assertIn("listings_d", auth.authorize_url)
        self.assertIn("shops_r", auth.authorize_url)
        self.assertIn(auth.state, auth.authorize_url)
        self.assertNotIn(auth.pkce.verifier, auth.authorize_url)
        # Legacy stored URI is migrated to the registered localhost callback.
        legacy = begin_auth_request(
            client_id="keystring",
            redirect_uri="https://127.0.0.1:8743/etsy/callback",
        )
        self.assertEqual(legacy.redirect_uri, DEFAULT_REDIRECT_URI)
        self.assertIn("localhost", legacy.authorize_url)
        self.assertNotIn("127.0.0.1", legacy.authorize_url)

    def test_token_exchange_and_refresh(self) -> None:
        now = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
        calls: list[bytes] = []

        def transport(method, url, headers, body, timeout):
            calls.append(body or b"")
            if b"authorization_code" in (body or b""):
                payload = {
                    "access_token": "12345.access",
                    "refresh_token": "refresh-one",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                    "scope": DEFAULT_SCOPES,
                }
            else:
                payload = {
                    "access_token": "12345.access-new",
                    "refresh_token": "refresh-two",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                    "scope": DEFAULT_SCOPES,
                }
            return 200, json.dumps(payload).encode()

        tokens = exchange_authorization_code(
            client_id="key",
            code="the-code",
            code_verifier="verifier",
            redirect_uri=DEFAULT_REDIRECT_URI,
            transport=transport,
            now=now,
        )
        self.assertEqual(tokens.access_token, "12345.access")
        self.assertEqual(tokens.user_id, "12345")
        self.assertEqual(tokens.expires_at, now + timedelta(seconds=3600))
        self.assertIn(b"code_verifier=verifier", calls[0])
        self.assertIn(b"grant_type=authorization_code", calls[0])

        refreshed = refresh_access_token(
            client_id="key",
            refresh_token="refresh-one",
            transport=transport,
            now=now,
        )
        self.assertEqual(refreshed.access_token, "12345.access-new")
        self.assertEqual(refreshed.refresh_token, "refresh-two")
        self.assertIn(b"grant_type=refresh_token", calls[1])

    def test_token_endpoint_error_is_redacted(self) -> None:
        def transport(method, url, headers, body, timeout):
            return (
                400,
                json.dumps(
                    {
                        "error": "invalid_grant",
                        "error_description": "bad code access_token=secret-value",
                    }
                ).encode(),
            )

        with self.assertRaises(EtsyError) as ctx:
            exchange_authorization_code(
                client_id="key",
                code="x",
                code_verifier="y",
                redirect_uri=DEFAULT_REDIRECT_URI,
                transport=transport,
            )
        self.assertNotIn("secret-value", str(ctx.exception))
        self.assertIn("[redacted]", str(ctx.exception))

    def test_setup_stores_owner_only_file_without_oauth(self) -> None:
        stored = setup_etsy_interactive(
            destination=self.credentials_path,
            input_fn=lambda prompt: {
                "Etsy API key (keystring): ": "keystring",
                f"Registered HTTPS callback URL [{DEFAULT_REDIRECT_URI}]: ": "",
                "Etsy shop id (leave blank until connect): ": "",
                f"Taxonomy id [2078]: ": "",
                "Return policy id (optional): ": "",
            }[prompt],
            getpass_fn=lambda prompt: {
                "Etsy shared secret: ": "shared-secret",
                "Etsy access token (leave blank to use `etsy connect`): ": "",
            }[prompt],
        )
        self.assertEqual(stored.api_key, "keystring")
        self.assertEqual(stored.shared_secret, "shared-secret")
        self.assertEqual(stored.access_token, "pending-oauth")
        mode = stat.S_IMODE(self.credentials_path.stat().st_mode)
        self.assertEqual(mode & 0o077, 0)
        self.assertFalse(etsy_configured(self.credentials_path))
        api_key, shared, redirect = load_app_credentials(self.credentials_path)
        self.assertEqual(api_key, "keystring")
        self.assertEqual(shared, "shared-secret")
        self.assertEqual(redirect, DEFAULT_REDIRECT_URI)

    def test_save_oauth_tokens_and_refresh_replace_refresh_token(self) -> None:
        now = datetime(2026, 10, 21, 8, 0, tzinfo=timezone.utc)
        tokens = TokenResponse(
            access_token="12345.access",
            refresh_token="refresh-a",
            expires_in=60,
            scope=DEFAULT_SCOPES,
            token_type="Bearer",
            obtained_at=now,
        )
        saved = save_oauth_tokens(
            api_key="key",
            shared_secret="secret",
            tokens=tokens,
            shop_id="998877",
            shop_name=EXPECTED_SHOP_NAME,
            redirect_uri=DEFAULT_REDIRECT_URI,
            path=self.credentials_path,
        )
        self.assertTrue(etsy_configured(self.credentials_path))
        self.assertEqual(saved.x_api_key(), "key:secret")
        mode = stat.S_IMODE(self.credentials_path.stat().st_mode)
        self.assertEqual(mode & 0o077, 0)

        def refresher(*, client_id, refresh_token, now=None, **_kwargs):
            self.assertEqual(refresh_token, "refresh-a")
            return TokenResponse(
                access_token="12345.access-b",
                refresh_token="refresh-b",
                expires_in=3600,
                scope=DEFAULT_SCOPES,
                token_type="Bearer",
                obtained_at=now or datetime.now(timezone.utc),
            )

        later = now + timedelta(minutes=10)
        fresh = ensure_fresh_credentials(
            saved,
            path=self.credentials_path,
            now=later,
            refresher=refresher,
        )
        self.assertEqual(fresh.access_token, "12345.access-b")
        self.assertEqual(fresh.refresh_token, "refresh-b")
        reloaded = load_etsy_credentials(self.credentials_path)
        self.assertEqual(reloaded.refresh_token, "refresh-b")

    def test_invalid_credentials_file_raises(self) -> None:
        self.credentials_path.write_text("{}\n", encoding="utf-8")
        with self.assertRaises(EtsyError):
            load_etsy_credentials(self.credentials_path)
        with self.assertRaises(EtsyError):
            load_app_credentials(self.credentials_path)

    def test_client_sends_keystring_shared_secret_header(self) -> None:
        captured: dict[str, str] = {}

        def transport(method, url, headers, body, timeout):
            captured.update(headers)
            return 200, json.dumps({"shop_id": 998877, "shop_name": EXPECTED_SHOP_NAME}).encode(), {}

        creds = EtsyCredentials(
            api_key="keystring",
            shared_secret="shared",
            access_token="12345.token",
            shop_id="998877",
            shop_name=EXPECTED_SHOP_NAME,
            scopes=DEFAULT_SCOPES,
            expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            refresh_token="refresh",
            path=self.credentials_path,
        )
        shop = EtsyClient(creds, transport=transport).get_shop()
        self.assertEqual(captured["x-api-key"], "keystring:shared")
        self.assertEqual(captured["Authorization"], "Bearer 12345.token")
        self.assertEqual(shop["shop_name"], EXPECTED_SHOP_NAME)

    def test_connect_verifies_anskartprints_and_leaves_verification_pending(self) -> None:
        setup_etsy_interactive(
            destination=self.credentials_path,
            input_fn=lambda prompt: {
                "Etsy API key (keystring): ": "keystring",
                f"Registered HTTPS callback URL [{DEFAULT_REDIRECT_URI}]: ": "",
                "Etsy shop id (leave blank until connect): ": "",
                f"Taxonomy id [2078]: ": "",
                "Return policy id (optional): ": "",
            }[prompt],
            getpass_fn=lambda prompt: {
                "Etsy shared secret: ": "shared-secret",
                "Etsy access token (leave blank to use `etsy connect`): ": "",
            }[prompt],
        )
        opened: list[str] = []

        def fake_wait(
            *,
            redirect_uri,
            expected_state,
            cert_file,
            key_file,
            timeout_seconds=600.0,
            on_listening=None,
        ):
            if on_listening is not None:
                on_listening()
            return "auth-code"

        def fake_exchange(*, client_id, code, code_verifier, redirect_uri, **_kwargs):
            self.assertEqual(code, "auth-code")
            self.assertTrue(code_verifier)
            return TokenResponse(
                access_token="4242.access",
                refresh_token="refresh-z",
                expires_in=3600,
                scope=DEFAULT_SCOPES,
                token_type="Bearer",
                obtained_at=datetime.now(timezone.utc),
            )

        def transport(method, url, headers, body, timeout):
            self.assertEqual(headers["x-api-key"], "keystring:shared-secret")
            if url.endswith("/users/4242/shops"):
                return (
                    200,
                    json.dumps(
                        {
                            "results": [
                                {"shop_id": 998877, "shop_name": EXPECTED_SHOP_NAME},
                            ]
                        }
                    ).encode(),
                    {},
                )
            return 404, b"{}", {}

        with patch("anskar.etsy.connect.ensure_localhost_tls", return_value=(self.root / "c.pem", self.root / "k.pem")):
            with patch("anskar.etsy.connect.wait_for_authorization_code", side_effect=fake_wait):
                with patch("anskar.etsy.connect.exchange_authorization_code", side_effect=fake_exchange):
                    with patch("anskar.etsy.connect.EtsyClient") as client_cls:
                        client_cls.return_value.get_user_shops.side_effect = (
                            lambda user_id: {
                                "results": [{"shop_id": 998877, "shop_name": EXPECTED_SHOP_NAME}]
                            }
                        )
                        result = connect_etsy(
                            open_browser=True,
                            path=self.credentials_path,
                            browser_opener=opened.append,
                        )
        self.assertTrue(result["connected"])
        self.assertEqual(result["shop_id"], "998877")
        self.assertEqual(result["shop_name"], EXPECTED_SHOP_NAME)
        self.assertEqual(result["integration_state"], PENDING_VERIFICATION)
        self.assertTrue(result["integration_unchanged"])
        self.assertEqual(load_etsy_state(self.state_path), PENDING_VERIFICATION)
        self.assertTrue(opened)
        stored = load_etsy_credentials(self.credentials_path)
        self.assertEqual(stored.shop_id, "998877")
        self.assertEqual(stored.refresh_token, "refresh-z")
        text = self.credentials_path.read_text(encoding="utf-8")
        # Secrets are on disk (expected) but must never appear in public status.
        status = etsy_connection_status(path=self.credentials_path)
        status_text = json.dumps(status)
        self.assertNotIn("shared-secret", status_text)
        self.assertNotIn("4242.access", status_text)
        self.assertNotIn("refresh-z", status_text)
        self.assertTrue(status["oauth_configured"])
        self.assertIn("shared-secret", text)  # file holds secrets; CLI must not print them

    def test_incorrect_shop_name_is_refused(self) -> None:
        self._write_app_only()
        with patch("anskar.etsy.connect.ensure_localhost_tls", return_value=(self.root / "c.pem", self.root / "k.pem")):
            with patch("anskar.etsy.connect.wait_for_authorization_code", return_value="code"):
                with patch(
                    "anskar.etsy.connect.exchange_authorization_code",
                    return_value=TokenResponse(
                        access_token="1.access",
                        refresh_token="r",
                        expires_in=3600,
                        scope=DEFAULT_SCOPES,
                        token_type="Bearer",
                        obtained_at=datetime.now(timezone.utc),
                    ),
                ):
                    with patch("anskar.etsy.connect.EtsyClient") as client_cls:
                        client_cls.return_value.get_user_shops.return_value = {
                            "results": [{"shop_id": 1, "shop_name": "WrongShop"}]
                        }
                        with self.assertRaises(EtsyError) as ctx:
                            connect_etsy(
                                open_browser=False,
                                path=self.credentials_path,
                                browser_opener=lambda _url: True,
                            )
        self.assertIn(EXPECTED_SHOP_NAME, str(ctx.exception))
        self.assertEqual(load_etsy_state(self.state_path), PENDING_VERIFICATION)

    def test_established_shop_id_requires_confirm_to_replace(self) -> None:
        save_oauth_tokens(
            api_key="key",
            shared_secret="secret",
            tokens=TokenResponse(
                access_token="1.old",
                refresh_token="old-r",
                expires_in=3600,
                scope=DEFAULT_SCOPES,
                token_type="Bearer",
                obtained_at=datetime.now(timezone.utc),
            ),
            shop_id="111",
            shop_name=EXPECTED_SHOP_NAME,
            redirect_uri=DEFAULT_REDIRECT_URI,
            path=self.credentials_path,
        )
        with patch("anskar.etsy.connect.ensure_localhost_tls", return_value=(self.root / "c.pem", self.root / "k.pem")):
            with patch("anskar.etsy.connect.wait_for_authorization_code", return_value="code"):
                with patch(
                    "anskar.etsy.connect.exchange_authorization_code",
                    return_value=TokenResponse(
                        access_token="2.access",
                        refresh_token="new-r",
                        expires_in=3600,
                        scope=DEFAULT_SCOPES,
                        token_type="Bearer",
                        obtained_at=datetime.now(timezone.utc),
                    ),
                ):
                    with patch("anskar.etsy.connect.EtsyClient") as client_cls:
                        client_cls.return_value.get_user_shops.return_value = {
                            "results": [{"shop_id": 222, "shop_name": EXPECTED_SHOP_NAME}]
                        }
                        with self.assertRaises(EtsyError) as ctx:
                            connect_etsy(
                                open_browser=False,
                                path=self.credentials_path,
                                browser_opener=lambda _url: True,
                            )
                        self.assertIn("differs", str(ctx.exception).lower())
                        result = connect_etsy(
                            open_browser=False,
                            confirm_shop=True,
                            path=self.credentials_path,
                            browser_opener=lambda _url: True,
                        )
        self.assertEqual(result["shop_id"], "222")

    def test_test_connection_read_only(self) -> None:
        save_oauth_tokens(
            api_key="key",
            shared_secret="secret",
            tokens=TokenResponse(
                access_token="1.access",
                refresh_token="r",
                expires_in=3600,
                scope=DEFAULT_SCOPES,
                token_type="Bearer",
                obtained_at=datetime.now(timezone.utc),
            ),
            shop_id=EXPECTED_SHOP_ID,
            shop_name=EXPECTED_SHOP_NAME,
            redirect_uri=DEFAULT_REDIRECT_URI,
            path=self.credentials_path,
        )

        with patch("anskar.etsy.connect.EtsyClient") as client_cls:
            client_cls.return_value.get_shop.return_value = {
                "shop_id": int(EXPECTED_SHOP_ID),
                "shop_name": EXPECTED_SHOP_NAME,
            }
            result = test_etsy_connection(path=self.credentials_path)
        self.assertTrue(result["ok"])
        self.assertTrue(result["shop_id_matches"])
        self.assertTrue(result["name_matches"])
        self.assertEqual(result["expected_shop_id"], EXPECTED_SHOP_ID)
        self.assertEqual(result["integration_state"], PENDING_VERIFICATION)

    def test_same_shop_id_accepted_when_display_name_differs(self) -> None:
        save_oauth_tokens(
            api_key="key",
            shared_secret="secret",
            tokens=TokenResponse(
                access_token="1.access",
                refresh_token="r",
                expires_in=3600,
                scope=DEFAULT_SCOPES,
                token_type="Bearer",
                obtained_at=datetime.now(timezone.utc),
            ),
            shop_id=EXPECTED_SHOP_ID,
            shop_name=EXPECTED_SHOP_NAME,
            redirect_uri=DEFAULT_REDIRECT_URI,
            path=self.credentials_path,
        )
        before = self.credentials_path.read_text(encoding="utf-8")
        with patch("anskar.etsy.connect.EtsyClient") as client_cls:
            client_cls.return_value.get_shop.return_value = {
                "shop_id": int(EXPECTED_SHOP_ID),
                "shop_name": "TemporaryDisplayName",
            }
            result = test_etsy_connection(path=self.credentials_path)
        self.assertTrue(result["ok"])
        self.assertTrue(result["shop_id_matches"])
        self.assertFalse(result["name_matches"])
        # Tokens and refresh material must remain untouched.
        after = self.credentials_path.read_text(encoding="utf-8")
        self.assertEqual(before, after)
        self.assertIn("1.access", after)
        self.assertIn('"refresh_token": "r"', after)

    def test_different_shop_id_is_rejected(self) -> None:
        save_oauth_tokens(
            api_key="key",
            shared_secret="secret",
            tokens=TokenResponse(
                access_token="1.access",
                refresh_token="r",
                expires_in=3600,
                scope=DEFAULT_SCOPES,
                token_type="Bearer",
                obtained_at=datetime.now(timezone.utc),
            ),
            shop_id=EXPECTED_SHOP_ID,
            shop_name=EXPECTED_SHOP_NAME,
            redirect_uri=DEFAULT_REDIRECT_URI,
            path=self.credentials_path,
        )
        with patch("anskar.etsy.connect.EtsyClient") as client_cls:
            client_cls.return_value.get_shop.return_value = {
                "shop_id": 99999999,
                "shop_name": EXPECTED_SHOP_NAME,
            }
            result = test_etsy_connection(path=self.credentials_path)
        self.assertFalse(result["ok"])
        self.assertFalse(result["shop_id_matches"])

    def test_oauth_success_does_not_set_ready(self) -> None:
        self.assertEqual(load_etsy_state(self.state_path), PENDING_VERIFICATION)
        set_etsy_state(READY, path=self.state_path)
        self.assertEqual(load_etsy_state(self.state_path), READY)
        # Revert and prove connect path never touches READY by itself.
        set_etsy_state(PENDING_VERIFICATION, path=self.state_path)
        status = etsy_connection_status(path=self.credentials_path)
        self.assertEqual(status["integration_state"], PENDING_VERIFICATION)
        self.assertFalse(status["oauth_configured"])

    def test_localhost_tls_cert_is_owner_only(self) -> None:
        cert, key = ensure_localhost_tls(self.tls_dir)
        self.assertTrue(cert.is_file())
        self.assertTrue(key.is_file())
        self.assertEqual(stat.S_IMODE(cert.stat().st_mode) & 0o077, 0)
        self.assertEqual(stat.S_IMODE(key.stat().st_mode) & 0o077, 0)
        text = subprocess.run(
            ["openssl", "x509", "-in", str(cert), "-noout", "-text"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.assertIn("DNS:localhost", text)
        # Second call reuses the same files.
        cert2, key2 = ensure_localhost_tls(self.tls_dir)
        self.assertEqual(cert, cert2)
        self.assertEqual(key, key2)

    def test_incompatible_tls_cert_is_replaced(self) -> None:
        self.tls_dir.mkdir(parents=True, exist_ok=True)
        bad_cert = self.tls_dir / "etsy-localhost-cert.pem"
        bad_key = self.tls_dir / "etsy-localhost-key.pem"
        # Placeholders that fail SAN validation and must be moved aside.
        bad_cert.write_text("not-a-cert\n", encoding="utf-8")
        bad_key.write_text("not-a-key\n", encoding="utf-8")
        bad_cert.chmod(0o600)
        bad_key.chmod(0o600)
        cert, key = ensure_localhost_tls(self.tls_dir)
        self.assertTrue(cert.is_file())
        self.assertTrue(key.is_file())
        text = subprocess.run(
            ["openssl", "x509", "-in", str(cert), "-noout", "-text"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.assertIn("DNS:localhost", text)
        self.assertTrue((self.tls_dir / "etsy-localhost-cert.pem.incompatible").is_file())

    def test_shop_identity_prefers_numeric_id(self) -> None:
        self.assertEqual(EXPECTED_SHOP_NAME, "Anskarartprints")
        self.assertEqual(EXPECTED_SHOP_ID, "37684672")
        self.assertTrue(shop_id_matches(37684672))
        self.assertTrue(shop_name_matches("anskarartprints"))
        self.assertFalse(shop_name_matches("Anskartprints"))
        self.assertFalse(shop_name_matches("Other"))
        self.assertTrue(
            shop_identity_matches(
                shop_id=EXPECTED_SHOP_ID,
                shop_name="CompletelyDifferentLabel",
            )
        )
        self.assertFalse(
            shop_identity_matches(
                shop_id="999",
                shop_name="OtherShop",
                stored_shop_id=EXPECTED_SHOP_ID,
            )
        )
        self.assertTrue(
            shop_identity_matches(
                shop_id=EXPECTED_SHOP_ID,
                shop_name="OtherLabel",
                stored_shop_id=EXPECTED_SHOP_ID,
            )
        )

    def test_redact_hides_authorization_code(self) -> None:
        cleaned = redact("callback?code=super-secret-code&state=abc")
        self.assertNotIn("super-secret-code", cleaned)
        self.assertIn("code=[redacted]", cleaned)

    def test_callback_error_and_missing_code(self) -> None:
        with self.assertRaises(EtsyError):
            parse_callback_query("error=access_denied&state=s", expected_state="s")
        with self.assertRaises(EtsyError):
            parse_callback_query("state=s", expected_state="s")

    def test_favicon_does_not_complete_oauth_wait(self) -> None:
        import ssl
        import threading
        import time
        import urllib.error
        import urllib.request

        from anskar.etsy.oauth import wait_for_authorization_code

        cert, key = ensure_localhost_tls(self.tls_dir)
        # Use an ephemeral port so parallel tests do not collide with 8743.
        redirect = "https://127.0.0.1:18743/etsy/callback"
        result: dict[str, object] = {}
        errors: list[BaseException] = []

        def run_wait() -> None:
            try:
                result["code"] = wait_for_authorization_code(
                    redirect_uri=redirect,
                    expected_state="s1",
                    cert_file=cert,
                    key_file=key,
                    timeout_seconds=8.0,
                )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        thread = threading.Thread(target=run_wait, daemon=True)
        thread.start()
        time.sleep(0.4)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            urllib.request.urlopen(
                "https://127.0.0.1:18743/favicon.ico",
                context=ctx,
                timeout=2,
            )
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 404)
        # Wait is still open — favicon must not have finished it.
        self.assertTrue(thread.is_alive())
        urllib.request.urlopen(
            "https://127.0.0.1:18743/etsy/callback?code=ok-code&state=s1",
            context=ctx,
            timeout=2,
        )
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result.get("code"), "ok-code")

    def _write_app_only(self) -> None:
        setup_etsy_interactive(
            destination=self.credentials_path,
            input_fn=lambda prompt: {
                "Etsy API key (keystring): ": "keystring",
                f"Registered HTTPS callback URL [{DEFAULT_REDIRECT_URI}]: ": "",
                "Etsy shop id (leave blank until connect): ": "",
                f"Taxonomy id [2078]: ": "",
                "Return policy id (optional): ": "",
            }[prompt],
            getpass_fn=lambda prompt: {
                "Etsy shared secret: ": "shared-secret",
                "Etsy access token (leave blank to use `etsy connect`): ": "",
            }[prompt],
        )


if __name__ == "__main__":
    unittest.main()

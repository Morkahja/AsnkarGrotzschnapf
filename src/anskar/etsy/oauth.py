"""Etsy Open API v3 OAuth 2.0 authorization-code flow with PKCE."""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import ssl
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from anskar.etsy.errors import EtsyError, redact

AUTHORIZE_URL = "https://www.etsy.com/oauth/connect"
TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"
DEFAULT_REDIRECT_URI = "https://localhost:8743/etsy/callback"
LEGACY_REDIRECT_URIS = frozenset({"https://127.0.0.1:8743/etsy/callback"})
DEFAULT_SCOPES = (
    "listings_r listings_w listings_d shops_r transactions_r"
)
EXPECTED_SHOP_NAME = "Anskarartprints"
EXPECTED_SHOP_ID = "37684672"
CODE_VERIFIER_LENGTH = 64  # within 43–128 unreserved chars
LOOPBACK_BIND_HOST = "127.0.0.1"


def normalize_redirect_uri(redirect_uri: str | None) -> str:
    """Prefer the registered HTTPS callback; migrate the rejected 127.0.0.1 URI."""
    if not redirect_uri or not str(redirect_uri).strip():
        return DEFAULT_REDIRECT_URI
    cleaned = str(redirect_uri).strip()
    if cleaned in LEGACY_REDIRECT_URIS:
        return DEFAULT_REDIRECT_URI
    return cleaned


Transport = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, bytes]]


@dataclass(frozen=True)
class PkcePair:
    verifier: str
    challenge: str


@dataclass(frozen=True)
class AuthRequest:
    state: str
    pkce: PkcePair
    redirect_uri: str
    scopes: str
    authorize_url: str


@dataclass(frozen=True)
class TokenResponse:
    access_token: str
    refresh_token: str
    expires_in: int
    scope: str
    token_type: str
    obtained_at: datetime

    @property
    def expires_at(self) -> datetime:
        return self.obtained_at + timedelta(seconds=max(0, int(self.expires_in)))

    @property
    def user_id(self) -> str | None:
        prefix, _, _rest = self.access_token.partition(".")
        return prefix if prefix.isdigit() else None


def generate_pkce() -> PkcePair:
    """RFC 7636 S256 PKCE using a cryptographically random verifier."""
    raw = secrets.token_urlsafe(48)
    verifier = "".join(ch for ch in raw if ch.isalnum() or ch in "-._~")[:CODE_VERIFIER_LENGTH]
    while len(verifier) < 43:
        verifier += secrets.choice("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return PkcePair(verifier=verifier, challenge=challenge)


def generate_state() -> str:
    return secrets.token_urlsafe(32)


def build_authorize_url(
    *,
    client_id: str,
    redirect_uri: str,
    scopes: str,
    state: str,
    code_challenge: str,
) -> str:
    if not redirect_uri.startswith("https://"):
        raise EtsyError(
            "The Etsy redirect URI must use https:// exactly as registered with Etsy.",
            kind="credentials",
        )
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scopes,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def begin_auth_request(
    *,
    client_id: str,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    scopes: str = DEFAULT_SCOPES,
) -> AuthRequest:
    redirect = normalize_redirect_uri(redirect_uri)
    state = generate_state()
    pkce = generate_pkce()
    url = build_authorize_url(
        client_id=client_id,
        redirect_uri=redirect,
        scopes=scopes,
        state=state,
        code_challenge=pkce.challenge,
    )
    return AuthRequest(
        state=state,
        pkce=pkce,
        redirect_uri=redirect,
        scopes=scopes,
        authorize_url=url,
    )


def exchange_authorization_code(
    *,
    client_id: str,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    transport: Transport | None = None,
    timeout: float = 60.0,
    now: datetime | None = None,
) -> TokenResponse:
    body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "redirect_uri": normalize_redirect_uri(redirect_uri),
            "code": code,
            "code_verifier": code_verifier,
        }
    ).encode("utf-8")
    return _token_request(body, transport=transport, timeout=timeout, now=now)


def refresh_access_token(
    *,
    client_id: str,
    refresh_token: str,
    transport: Transport | None = None,
    timeout: float = 60.0,
    now: datetime | None = None,
) -> TokenResponse:
    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
        }
    ).encode("utf-8")
    return _token_request(body, transport=transport, timeout=timeout, now=now)


def _token_request(
    body: bytes,
    *,
    transport: Transport | None,
    timeout: float,
    now: datetime | None,
) -> TokenResponse:
    send = transport or _default_transport
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    status, raw = send("POST", TOKEN_URL, headers, body, timeout)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EtsyError(f"Etsy token endpoint returned non-JSON (HTTP {status}).", kind="api") from exc
    if not isinstance(payload, dict):
        raise EtsyError("Etsy token endpoint returned an unexpected payload.", kind="api")
    if status >= 400 or "error" in payload:
        message = str(payload.get("error_description") or payload.get("error") or payload)
        raise EtsyError(redact(f"Etsy token request failed: {message}"), kind="api")
    access = payload.get("access_token")
    refresh = payload.get("refresh_token")
    expires_in = payload.get("expires_in", 3600)
    scope = payload.get("scope", "")
    token_type = payload.get("token_type", "Bearer")
    if not isinstance(access, str) or not access.strip():
        raise EtsyError("Etsy did not return an access token.", kind="api")
    if not isinstance(refresh, str) or not refresh.strip():
        raise EtsyError("Etsy did not return a refresh token.", kind="api")
    try:
        expires_seconds = int(expires_in)
    except (TypeError, ValueError) as exc:
        raise EtsyError("Etsy returned an invalid expires_in value.", kind="api") from exc
    obtained = now or datetime.now(timezone.utc)
    if obtained.tzinfo is None:
        obtained = obtained.replace(tzinfo=timezone.utc)
    return TokenResponse(
        access_token=access.strip(),
        refresh_token=refresh.strip(),
        expires_in=expires_seconds,
        scope=str(scope).strip(),
        token_type=str(token_type),
        obtained_at=obtained,
    )


def parse_callback_query(query: str, *, expected_state: str) -> str:
    """Validate state and return the authorization code. Never logs secrets."""
    params = urllib.parse.parse_qs(query, keep_blank_values=False)
    if "error" in params:
        description = (params.get("error_description") or params.get("error") or [""])[0]
        raise EtsyError(f"Etsy authorization was denied: {description}", kind="credentials")
    state_values = params.get("state") or []
    if not state_values or state_values[0] != expected_state:
        raise EtsyError(
            "The OAuth state parameter did not match. Authorization was aborted.",
            kind="credentials",
        )
    codes = params.get("code") or []
    if not codes or not codes[0].strip():
        raise EtsyError("Etsy did not return an authorization code.", kind="credentials")
    return codes[0].strip()


def ensure_localhost_tls(directory: Path) -> tuple[Path, Path]:
    """Create a self-signed cert for https://localhost with DNS:localhost in the SAN."""
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    cert = directory / "etsy-localhost-cert.pem"
    key = directory / "etsy-localhost-key.pem"
    if cert.is_file() and key.is_file() and _cert_includes_localhost_san(cert):
        return cert, key
    if cert.is_file() or key.is_file():
        _replace_incompatible_tls_files(cert, key)
    if not _which_openssl():
        raise EtsyError(
            "openssl is required to create a local HTTPS certificate for the Etsy callback. "
            "Install openssl, then run `anskar etsy connect` again.",
            kind="credentials",
        )
    completed = subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-days",
            "825",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        shell=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0 or not cert.is_file() or not key.is_file():
        raise EtsyError(
            "Could not create a local HTTPS certificate for the Etsy callback.",
            kind="credentials",
        )
    for path in (cert, key):
        try:
            path.chmod(0o600)
        except OSError:
            pass
    if not _cert_includes_localhost_san(cert):
        raise EtsyError(
            "The local HTTPS certificate is missing DNS:localhost in its Subject Alternative Names.",
            kind="credentials",
        )
    return cert, key


def wait_for_authorization_code(
    *,
    redirect_uri: str,
    expected_state: str,
    cert_file: Path,
    key_file: Path,
    timeout_seconds: float = 600.0,
    on_listening: Callable[[], object] | None = None,
) -> str:
    """Listen once on the registered HTTPS callback, then shut down.

    Start the listener before opening the browser (`on_listening`) so the
    callback cannot arrive before the socket is ready.
    """
    parsed = urlparse(normalize_redirect_uri(redirect_uri))
    if parsed.scheme != "https":
        raise EtsyError("The callback URL must be https://.", kind="credentials")
    hostname = (parsed.hostname or "localhost").casefold()
    if hostname not in {"localhost", "127.0.0.1"}:
        raise EtsyError(
            "The Etsy callback listener only accepts localhost / 127.0.0.1 URLs.",
            kind="credentials",
        )
    port = parsed.port or 443
    path = parsed.path or "/"
    result: dict[str, object] = {}
    error_box: list[BaseException] = []
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed_path = urllib.parse.urlparse(self.path)
            if parsed_path.path != path:
                # Favicon and other noise must not complete the OAuth wait.
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Not found")
                return
            try:
                code = parse_callback_query(parsed_path.query, expected_state=expected_state)
                result["code"] = code
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    b"<html><body><h1>Anskar connected to Etsy</h1>"
                    b"<p>You can close this window and return to the terminal.</p></body></html>"
                )
            except BaseException as exc:  # noqa: BLE001
                error_box.append(exc)
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Authorization failed. Return to the terminal.")
            finally:
                done.set()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return

    # Bind loopback only — never 0.0.0.0 — even when the registered host is "localhost".
    server = http.server.HTTPServer((LOOPBACK_BIND_HOST, port), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
    server.socket = context.wrap_socket(server.socket, server_side=True)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if on_listening is not None:
            on_listening()
        if not done.wait(timeout_seconds):
            raise EtsyError(
                "Timed out waiting for the Etsy authorization callback.",
                kind="credentials",
            )
        if error_box:
            raise error_box[0]
        code = result.get("code")
        if not isinstance(code, str) or not code:
            raise EtsyError("Authorization finished without a code.", kind="credentials")
        return code
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _which_openssl() -> str | None:
    from shutil import which

    return which("openssl")


def _cert_includes_localhost_san(cert: Path) -> bool:
    if not cert.is_file() or not _which_openssl():
        return False
    completed = subprocess.run(
        ["openssl", "x509", "-in", str(cert), "-noout", "-text"],
        shell=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        return False
    text = completed.stdout or ""
    return "DNS:localhost" in text


def _replace_incompatible_tls_files(cert: Path, key: Path) -> None:
    """Move aside cert/key that lack DNS:localhost so a compatible pair can be created."""
    for path in (cert, key):
        if not path.is_file():
            continue
        backup = path.with_suffix(path.suffix + ".incompatible")
        try:
            if backup.is_file():
                backup.unlink()
            path.replace(backup)
            backup.chmod(0o600)
        except OSError:
            try:
                path.unlink()
            except OSError as exc:
                raise EtsyError(
                    "Could not replace an incompatible local HTTPS certificate.",
                    kind="credentials",
                ) from exc


def _default_transport(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout: float,
) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() or b""
    except TimeoutError as exc:
        raise EtsyError("The Etsy token request timed out.", kind="timeout") from exc
    except OSError as exc:
        raise EtsyError("The Etsy token request could not connect.", kind="api") from exc

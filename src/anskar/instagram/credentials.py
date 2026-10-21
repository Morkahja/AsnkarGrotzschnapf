"""Instagram token storage outside the git repository."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from anskar.instagram.errors import InstagramError

EXPECTED_USERNAME = "anskargrotzschnapf"
GRAPH_VERSION = "v22.0"
# Long-lived Instagram Login tokens last about 60 days. Refresh inside the last week.
RENEW_WITHIN_DAYS = 7


@dataclass(frozen=True)
class Credentials:
    access_token: str
    expires_at: str | None
    path: Path
    graph_version: str = GRAPH_VERSION
    expected_username: str = EXPECTED_USERNAME
    user_id: str | None = None

    def public(self) -> dict[str, object]:
        """Fields that are safe to print."""
        remaining = seconds_until(self.expires_at)
        return {
            "path": str(self.path),
            "expires_at": self.expires_at,
            "days_remaining": None if remaining is None else round(remaining / 86400, 2),
            "renewal_due": remaining is not None and remaining < RENEW_WITHIN_DAYS * 86400,
            "graph_version": self.graph_version,
            "expected_username": self.expected_username,
            "user_id": self.user_id,
        }


def credentials_path() -> Path:
    override = os.environ.get("ANSKAR_INSTAGRAM_CREDENTIALS")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "anskar" / "instagram.json"


def hosts_path() -> Path:
    override = os.environ.get("ANSKAR_INSTAGRAM_HOSTS")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "anskar" / "instagram-hosts.json"


def load_credentials(path: Path | None = None) -> Credentials:
    file = path or credentials_path()
    if not file.is_file():
        raise InstagramError(
            "No Instagram token is stored. Put the token in a file by itself, then run "
            "`anskar instagram install-token --from` that file. Do not paste the token into the chat.",
            kind="credentials",
        )
    try:
        payload = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstagramError("The Instagram credential file could not be read.", kind="credentials") from exc
    if not isinstance(payload, dict):
        raise InstagramError("The Instagram credential file is not a record.", kind="credentials")
    token = payload.get("access_token")
    if not isinstance(token, str) or not token.strip():
        raise InstagramError("The Instagram credential file has no token.", kind="credentials")
    expires = payload.get("expires_at")
    user_id = payload.get("user_id")
    version = payload.get("graph_version")
    return Credentials(
        access_token=token.strip(),
        expires_at=expires if isinstance(expires, str) and expires.strip() else None,
        path=file,
        graph_version=version.strip() if isinstance(version, str) and version.strip() else GRAPH_VERSION,
        user_id=user_id.strip() if isinstance(user_id, str) and user_id.strip() else None,
    )


def install_token(
    source: Path,
    *,
    destination: Path | None = None,
    expires_at: str | None = None,
) -> Credentials:
    """Copy a token from a file the user created. The token is not returned in a message."""
    if not source.is_file():
        raise InstagramError(f"Token file not found: {source}", kind="credentials")
    try:
        raw = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise InstagramError("The token file could not be read.", kind="credentials") from exc
    token = raw.strip()
    if not token or "\n" in token or token.startswith("{") or len(token) < 20 or " " in token:
        raise InstagramError(
            "The token file must contain only the Instagram access token on one line.",
            kind="credentials",
        )
    if expires_at is not None:
        _parse_time(expires_at)
    file = destination or credentials_path()
    current_user = None
    version = GRAPH_VERSION
    if file.is_file():
        try:
            current = load_credentials(file)
        except InstagramError:
            current = None
        if current is not None:
            current_user = current.user_id
            version = current.graph_version
    stored = Credentials(
        access_token=token,
        expires_at=expires_at,
        path=file,
        graph_version=version,
        user_id=current_user,
    )
    _write(stored)
    return stored


def save_profile(credentials: Credentials, *, user_id: str, expires_at: str | None = None) -> Credentials:
    updated = Credentials(
        access_token=credentials.access_token,
        expires_at=expires_at if expires_at is not None else credentials.expires_at,
        path=credentials.path,
        graph_version=credentials.graph_version,
        expected_username=credentials.expected_username,
        user_id=user_id,
    )
    _write(updated)
    return updated


def replace_token(credentials: Credentials, token: str, expires_at: str | None) -> Credentials:
    if not token.strip():
        raise InstagramError("Instagram did not return a replacement token.", kind="credentials")
    updated = Credentials(
        access_token=token.strip(),
        expires_at=expires_at,
        path=credentials.path,
        graph_version=credentials.graph_version,
        expected_username=credentials.expected_username,
        user_id=credentials.user_id,
    )
    _write(updated)
    return updated


def seconds_until(expires_at: str | None, *, now: datetime | None = None) -> float | None:
    if not expires_at:
        return None
    try:
        expires = _parse_time(expires_at)
    except InstagramError:
        return None
    clock = now or datetime.now(timezone.utc)
    return (expires - clock).total_seconds()


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InstagramError("The token expiry time is not a date.", kind="credentials") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _write(credentials: Credentials) -> None:
    payload = {
        "access_token": credentials.access_token,
        "expires_at": credentials.expires_at,
        "user_id": credentials.user_id,
        "expected_username": credentials.expected_username,
        "graph_version": credentials.graph_version,
    }
    file = credentials.path
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

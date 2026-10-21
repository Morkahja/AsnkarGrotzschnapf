"""Etsy shop integration readiness. Independent of daily Instagram publishing."""

from __future__ import annotations

import json
import os
from pathlib import Path

from anskar.etsy.errors import EtsyError

PENDING_VERIFICATION = "pending_verification"
READY = "ready"
DISABLED = "disabled"
STATES = (PENDING_VERIFICATION, READY, DISABLED)

ETSY_LISTING_PENDING = "pending_shop_activation"
ETSY_LISTING_QUEUED = "queued_for_publish"
ETSY_LISTING_PUBLISHED = "published"
ETSY_LISTING_FAILED = "failed"


def etsy_state_path() -> Path:
    override = os.environ.get("ANSKAR_ETSY_STATE")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "anskar" / "etsy-state.json"


def load_etsy_state(path: Path | None = None) -> str:
    file = path or etsy_state_path()
    if not file.is_file():
        return PENDING_VERIFICATION
    try:
        payload = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return PENDING_VERIFICATION
    if not isinstance(payload, dict):
        return PENDING_VERIFICATION
    state = payload.get("state")
    if state in STATES:
        return str(state)
    return PENDING_VERIFICATION


def etsy_is_ready(path: Path | None = None) -> bool:
    return load_etsy_state(path) == READY


def set_etsy_state(state: str, *, path: Path | None = None) -> str:
    if state not in STATES:
        raise EtsyError(
            f"Etsy state must be one of: {', '.join(STATES)}.",
            kind="credentials",
        )
    file = path or etsy_state_path()
    file.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(file.parent, 0o700)
    except OSError:
        pass
    payload = {"provider": "etsy-open-api-v3", "state": state}
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
    return state


def public_etsy_state(path: Path | None = None) -> dict[str, object]:
    state = load_etsy_state(path)
    return {
        "state": state,
        "ready": state == READY,
        "calls_api": state == READY,
        "path": str(path or etsy_state_path()),
        "note": (
            "Shop verification is pending. Daily Instagram publishing continues; "
            "Etsy listings stay queued until the state is set to ready."
            if state == PENDING_VERIFICATION
            else (
                "Etsy publishing is disabled."
                if state == DISABLED
                else "Etsy is ready. New daily runs may publish listings; backlog needs explicit authorization."
            )
        ),
    }

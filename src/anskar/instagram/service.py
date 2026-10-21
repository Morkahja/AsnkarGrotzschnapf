"""Prepare and, only after an explicit confirm, publish one pack."""

from __future__ import annotations

import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from anskar.config import PROJECT_ROOT
from anskar.instagram.credentials import (
    RENEW_WITHIN_DAYS,
    Credentials,
    load_credentials,
    replace_token,
    save_profile,
    seconds_until,
)
from anskar.instagram.errors import InstagramError
from anskar.instagram.graph import GraphClient
from anskar.instagram.hosting import load_hosts
from anskar.instagram.ledger import (
    begin_attempt,
    blocking_attempt,
    connect,
    list_attempts,
    public_attempt,
    published_attempt,
    update_attempt,
)
from anskar.instagram.posts import assert_publishable, prepare_post
from anskar.instagram.r2 import R2StagingAdapter, stage_jpeg

POLL_INTERVAL_SECONDS = 5.0
MAX_POLLS = 24


def posts_root() -> Path:
    override = os.environ.get("ANSKAR_INSTAGRAM_POSTS")
    if override:
        return Path(override).expanduser()
    return PROJECT_ROOT / "data" / "instagram"


def database_path() -> Path:
    override = os.environ.get("ANSKAR_INSTAGRAM_DB")
    if override:
        return Path(override).expanduser()
    return PROJECT_ROOT / "data" / "instagram.sqlite"


def connection_status(
    credentials: Credentials | None = None,
    client: GraphClient | None = None,
) -> dict[str, object]:
    """Read the profile. This does not publish."""
    stored = credentials or load_credentials()
    graph = client or GraphClient(stored.access_token, version=stored.graph_version)
    profile = graph.profile()
    username = str(profile.get("username") or "")
    user_id = str(profile.get("user_id") or profile.get("id") or "")
    account_type = str(profile.get("account_type") or "")
    if username.lower() != stored.expected_username.lower():
        raise InstagramError(
            f"The token belongs to @{username or 'unknown'}, not @{stored.expected_username}.",
            kind="account",
        )
    if not user_id:
        raise InstagramError("Instagram did not return an account id.", kind="api")
    updated = save_profile(stored, user_id=user_id)
    remaining = seconds_until(updated.expires_at)
    return {
        "connected": True,
        "username": username,
        "user_id": user_id,
        "account_type": account_type,
        "expected_username": stored.expected_username,
        "account_match": True,
        "professional_account": account_type.upper() in {"BUSINESS", "MEDIA_CREATOR"},
        "credentials": updated.public(),
        "permissions": {
            "instagram_business_basic": "confirmed",
            "instagram_business_content_publish": (
                "required for publishing; not called by this read-only check"
            ),
        },
        "renewal": _renewal_text(remaining),
        "published": False,
    }


def renew_token(
    *,
    force: bool = False,
    credentials: Credentials | None = None,
    client: GraphClient | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Refresh a long-lived token with Meta's ig_refresh_token grant."""
    stored = credentials or load_credentials()
    clock = now or datetime.now(timezone.utc)
    remaining = seconds_until(stored.expires_at, now=clock)
    if remaining is not None and remaining <= 0:
        raise InstagramError(
            "The Instagram token is already expired. Generate a new token in the Meta "
            "developer dashboard and install it with `anskar instagram install-token --from FILE`. "
            "Do not paste the token into the chat.",
            kind="expired",
        )
    if remaining is not None and remaining >= RENEW_WITHIN_DAYS * 86400 and not force:
        days = round(remaining / 86400, 1)
        raise InstagramError(
            f"The token still has {days} days. Renewal waits until the last {RENEW_WITHIN_DAYS} days. "
            "Nothing was changed. `anskar instagram renew` calls Meta's ig_refresh_token grant "
            "and stores the new token without printing it.",
            kind="credentials",
        )
    graph = client or GraphClient(stored.access_token, version=stored.graph_version)
    payload = graph.refresh()
    token = payload.get("access_token")
    if not isinstance(token, str) or not token.strip():
        raise InstagramError("Instagram did not return a replacement token.", kind="api")
    expires_in = payload.get("expires_in")
    try:
        seconds = int(expires_in)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        seconds = 0
    expires_at = stored.expires_at
    if seconds > 0:
        expires_at = (clock + timedelta(seconds=seconds)).isoformat()
    updated = replace_token(stored, token, expires_at)
    public = updated.public()
    public["renewed"] = True
    return public


def prepare(
    pack_id: str,
    *,
    packs: Path | None = None,
    destination: Path | None = None,
    hosts: dict[str, object] | None = None,
    database: Path | None = None,
    jpeg_writer=None,
) -> dict[str, object]:
    host_record = hosts if hosts is not None else _hosts_or_empty()
    folder = destination or (posts_root() / pack_id)
    record = prepare_post(
        pack_id,
        packs=packs,
        destination=folder,
        hosts=host_record,
        jpeg_writer=jpeg_writer,
    )
    db = database or database_path()
    with connect(db) as connection:
        existing = published_attempt(connection, str(record["artwork_id"]))
        if existing is None:
            begin_attempt(
                connection,
                pack_id=pack_id,
                artwork_id=str(record["artwork_id"]),
                image_sha256=str(record["image_sha256"]),
                caption_sha256=str(record["caption_sha256"]),
                state="prepared",
                image_url=record.get("public_image_url")
                if isinstance(record.get("public_image_url"), str)
                else None,
            )
    record["published"] = existing is not None
    return record


def publish(
    pack_id: str,
    *,
    dry_run: bool = False,
    confirm: bool = False,
    replace_unknown: bool = False,
    packs: Path | None = None,
    destination: Path | None = None,
    hosts: dict[str, object] | None = None,
    database: Path | None = None,
    credentials: Credentials | None = None,
    client: GraphClient | None = None,
    staging: R2StagingAdapter | None = None,
    jpeg_writer=None,
    probe=None,
    sleep=None,
) -> dict[str, object]:
    """Run the publishing sequence. A real post requires confirm and is never implied by dry-run."""
    if dry_run and confirm:
        raise InstagramError("Pass either --dry-run or --confirm.", kind="error")
    record = prepare(
        pack_id,
        packs=packs,
        destination=destination,
        hosts=hosts,
        database=database,
        jpeg_writer=jpeg_writer,
    )
    db = database or database_path()
    if record.get("published"):
        raise InstagramError("This artwork is already published on Instagram.", kind="duplicate")
    with connect(db) as connection:
        blocking = blocking_attempt(connection, str(record["artwork_id"]))
    assert_publishable(record)
    if dry_run:
        if blocking is not None:
            raise InstagramError(
                "An earlier publication attempt is unresolved. The dry run did not start another one.",
                kind="unknown",
            )
        return _dry_run_stage(
            record,
            database=db,
            staging=staging,
            probe=probe or probe_public_url,
        )
    if not confirm:
        raise InstagramError(
            "Nothing was published. Use --dry-run to review the post, or --confirm to publish it.",
            kind="error",
        )
    stored = credentials or load_credentials()
    graph = client or GraphClient(stored.access_token, version=stored.graph_version)
    account = _require_account(stored, graph)
    creation_id = None
    if blocking is not None:
        creation_id = _reconcile(
            graph,
            blocking,
            account["user_id"],
            str(record["caption"]),
            confirm=confirm,
            replace_unknown=replace_unknown,
            database=db,
            staging=staging,
        )
        if isinstance(creation_id, dict):
            return creation_id
    if creation_id is None:
        creation_id = _stage_and_create(graph, db, record, account["user_id"], staging=staging, probe=probe)
    _wait_until_ready(graph, db, record, creation_id, sleep=sleep or _sleep)
    return _publish_container(graph, db, record, account["user_id"], creation_id, staging=staging)


def list_pack_attempts(pack_id: str, *, database: Path | None = None) -> list[dict[str, object]]:
    with connect(database or database_path()) as connection:
        return [public_attempt(row) for row in list_attempts(connection, pack_id)]


def probe_public_url(url: str) -> None:
    from anskar.instagram.hosting import validate_public_url

    validate_public_url(url)
    request = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status = response.status
            content_type = response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        if exc.code == 405:
            return
        raise InstagramError(
            f"The image URL is not publicly readable (HTTP {exc.code}).",
            kind="url",
        ) from exc
    except TimeoutError as exc:
        raise InstagramError("The image URL timed out.", kind="url") from exc
    except OSError as exc:
        raise InstagramError("The image URL could not be reached.", kind="url") from exc
    if status >= 400:
        raise InstagramError(f"The image URL is not publicly readable (HTTP {status}).", kind="url")
    if content_type and "image" not in content_type.lower():
        raise InstagramError("The image URL did not return an image.", kind="url")


def _dry_run_stage(
    record: dict[str, object],
    *,
    database: Path,
    staging: R2StagingAdapter | None,
    probe,
) -> dict[str, object]:
    adapter = staging or R2StagingAdapter()
    image = Path(str(record["image"]))
    staged = stage_jpeg(image, pack_id=str(record["pack_id"]), adapter=adapter)
    try:
        probe(staged.url)
    finally:
        adapter.delete(staged.key)
    with connect(database) as connection:
        attempt_id = begin_attempt(
            connection,
            pack_id=str(record["pack_id"]),
            artwork_id=str(record["artwork_id"]),
            image_sha256=str(record["image_sha256"]),
            caption_sha256=str(record["caption_sha256"]),
            state="dry_run",
            staging_key=None,
        )
    return {
        "dry_run": True,
        "published": False,
        "attempt_id": attempt_id,
        "pack_id": record["pack_id"],
        "staged": True,
        "staging_deleted": True,
        "caption": record["caption"],
        "alt_text": record["alt_text"],
        "title": record.get("title"),
        "author": record.get("author"),
    }


def _stage_and_create(
    client: GraphClient,
    database: Path,
    record: dict[str, object],
    user_id: str,
    *,
    staging: R2StagingAdapter | None,
    probe,
) -> str:
    adapter = staging or R2StagingAdapter()
    image = Path(str(record["image"]))
    staged = stage_jpeg(image, pack_id=str(record["pack_id"]), adapter=adapter)
    record["public_image_url"] = staged.url
    record["staging_key"] = staged.key
    if probe is not None:
        try:
            probe(staged.url)
        except InstagramError:
            # Keep the object so a person can inspect the failure; ledger records the key.
            with connect(database) as connection:
                attempt_id = begin_attempt(
                    connection,
                    pack_id=str(record["pack_id"]),
                    artwork_id=str(record["artwork_id"]),
                    image_sha256=str(record["image_sha256"]),
                    caption_sha256=str(record["caption_sha256"]),
                    state="failed",
                    staging_key=staged.key,
                )
                update_attempt(
                    connection,
                    attempt_id,
                    error_kind="url",
                    error_message="The staged image URL was not reachable.",
                )
            raise
    return _create_container(client, database, record, user_id, staging_key=staged.key)


def _require_account(credentials: Credentials, client: GraphClient) -> dict[str, str]:
    profile = client.profile()
    username = str(profile.get("username") or "")
    if username.lower() != credentials.expected_username.lower():
        raise InstagramError(
            f"The token belongs to @{username or 'unknown'}, not @{credentials.expected_username}.",
            kind="account",
        )
    account_type = str(profile.get("account_type") or "")
    if account_type.upper() == "PERSONAL":
        raise InstagramError(
            "The Instagram account is personal. Publishing needs a professional Creator or Business account.",
            kind="account",
        )
    user_id = str(profile.get("user_id") or profile.get("id") or "")
    if not user_id:
        raise InstagramError("Instagram did not return an account id.", kind="api")
    save_profile(credentials, user_id=user_id)
    return {"username": username, "user_id": user_id, "account_type": account_type}


def _reconcile(
    client: GraphClient,
    attempt,
    user_id: str,
    caption: str,
    *,
    confirm: bool,
    replace_unknown: bool,
    database: Path,
    staging: R2StagingAdapter | None = None,
) -> str | dict[str, object] | None:
    container_id = attempt["container_id"]
    if not container_id:
        matched = _matching_media(client, user_id, caption)
        if matched is not None:
            return _record_published(database, attempt["id"], matched, staging=staging)
        if confirm and replace_unknown:
            with connect(database) as connection:
                update_attempt(
                    connection,
                    attempt["id"],
                    state="failed",
                    error_kind="unknown",
                    error_message="No published media matched this caption, so the unknown attempt was closed.",
                )
            return None
        raise InstagramError(
            "The last request timed out before a container id was stored. "
            "No new container was created. After you have checked the Instagram account, "
            "pass --confirm --replace-unknown to allow another attempt.",
            kind="unknown",
        )
    try:
        status = client.container_status(str(container_id))
    except InstagramError as exc:
        if exc.kind == "timeout":
            raise InstagramError(
                "Checking the earlier container timed out. No new container was created.",
                kind="timeout",
            ) from exc
        raise
    code = str(status.get("status_code") or "").upper()
    if code == "PUBLISHED":
        media_id = str(status.get("id") or container_id)
        return _record_published(
            database,
            attempt["id"],
            {"id": media_id, "permalink": _permalink(client, media_id)},
            staging=staging,
        )
    if code == "FINISHED":
        return str(container_id)
    if code in {"ERROR", "EXPIRED"}:
        with connect(database) as connection:
            update_attempt(
                connection,
                attempt["id"],
                state="failed",
                error_kind="media",
                error_message=f"The media container is {code}.",
            )
        raise InstagramError(
            f"The earlier container is {code} and was kept as a failed attempt. "
            "Run publish --confirm again to create a new container.",
            kind="media",
        )
    raise InstagramError(
        "The earlier container is still processing. No new container was created.",
        kind="unknown",
    )


def _create_container(
    client: GraphClient,
    database: Path,
    record: dict[str, object],
    user_id: str,
    *,
    staging_key: str | None = None,
) -> str:
    with connect(database) as connection:
        attempt_id = begin_attempt(
            connection,
            pack_id=str(record["pack_id"]),
            artwork_id=str(record["artwork_id"]),
            image_sha256=str(record["image_sha256"]),
            caption_sha256=str(record["caption_sha256"]),
            state="publishing",
            image_url=str(record["public_image_url"]) if record.get("public_image_url") else None,
            staging_key=staging_key or (
                str(record["staging_key"]) if record.get("staging_key") else None
            ),
        )
    try:
        container_id = client.create_image(
            user_id,
            image_url=str(record["public_image_url"]),
            caption=str(record["caption"]),
            alt_text=str(record["alt_text"]),
        )
    except InstagramError as exc:
        state = "unknown" if exc.kind == "timeout" else "failed"
        with connect(database) as connection:
            update_attempt(
                connection,
                attempt_id,
                state=state,
                error_kind=exc.kind,
                error_message=str(exc),
            )
        if exc.kind == "timeout":
            raise InstagramError(
                "Creating the media container timed out. No second request was sent.",
                kind="timeout",
            ) from exc
        raise
    with connect(database) as connection:
        update_attempt(connection, attempt_id, state="container_created", container_id=container_id)
    record["attempt_id"] = attempt_id
    return container_id


def _wait_until_ready(
    client: GraphClient,
    database: Path,
    record: dict[str, object],
    container_id: str,
    *,
    sleep,
) -> None:
    attempt_id = _attempt_for_container(database, str(record["artwork_id"]), container_id)
    for _ in range(MAX_POLLS):
        try:
            status = client.container_status(container_id)
        except InstagramError as exc:
            if exc.kind == "timeout" and attempt_id is not None:
                with connect(database) as connection:
                    update_attempt(
                        connection,
                        attempt_id,
                        state="unknown",
                        error_kind="timeout",
                        error_message=str(exc),
                    )
            raise
        code = str(status.get("status_code") or "").upper()
        if code in {"FINISHED", "PUBLISHED"}:
            return
        if code in {"ERROR", "EXPIRED"} and attempt_id is not None:
            with connect(database) as connection:
                update_attempt(
                    connection,
                    attempt_id,
                    state="failed",
                    error_kind="media",
                    error_message=f"The media container is {code}.",
                )
            raise InstagramError(f"The media container is {code}.", kind="media")
        sleep(POLL_INTERVAL_SECONDS)
    if attempt_id is not None:
        with connect(database) as connection:
            update_attempt(
                connection,
                attempt_id,
                state="unknown",
                error_kind="timeout",
                error_message="The media container did not become ready.",
            )
    raise InstagramError(
        "The media container did not become ready. No publish request was sent.",
        kind="timeout",
    )


def _publish_container(
    client: GraphClient,
    database: Path,
    record: dict[str, object],
    user_id: str,
    container_id: str,
    *,
    staging: R2StagingAdapter | None = None,
) -> dict[str, object]:
    attempt_id = _attempt_for_container(database, str(record["artwork_id"]), container_id)
    if attempt_id is None:
        with connect(database) as connection:
            attempt_id = begin_attempt(
                connection,
                pack_id=str(record["pack_id"]),
                artwork_id=str(record["artwork_id"]),
                image_sha256=str(record["image_sha256"]),
                caption_sha256=str(record["caption_sha256"]),
                state="publishing",
                image_url=str(record["public_image_url"]) if record.get("public_image_url") else None,
                staging_key=str(record["staging_key"]) if record.get("staging_key") else None,
            )
            update_attempt(connection, attempt_id, container_id=container_id)
    else:
        with connect(database) as connection:
            update_attempt(connection, attempt_id, state="publishing")
    try:
        media_id = client.publish(user_id, container_id)
    except InstagramError as exc:
        state = "unknown" if exc.kind == "timeout" else "failed"
        with connect(database) as connection:
            update_attempt(
                connection,
                attempt_id,
                state=state,
                error_kind=exc.kind,
                error_message=str(exc),
            )
        if exc.kind == "timeout":
            raise InstagramError(
                "Publishing timed out. The next attempt will check this container before creating another.",
                kind="timeout",
            ) from exc
        raise
    permalink = _permalink(client, media_id)
    staging_key = _staging_key_for(database, attempt_id) or (
        str(record["staging_key"]) if record.get("staging_key") else None
    )
    with connect(database) as connection:
        update_attempt(
            connection,
            attempt_id,
            state="published",
            media_id=media_id,
            permalink=permalink,
            container_id=container_id,
        )
    _delete_staging(staging_key, staging=staging, database=database, attempt_id=attempt_id)
    return {
        "dry_run": False,
        "published": True,
        "pack_id": record["pack_id"],
        "media_id": media_id,
        "permalink": permalink,
        "container_id": container_id,
        "attempt_id": attempt_id,
        "staging_deleted": bool(staging_key),
    }


def _record_published(
    database: Path,
    attempt_id: int,
    media: dict[str, object],
    *,
    staging: R2StagingAdapter | None = None,
) -> dict[str, object]:
    media_id = str(media.get("id") or "")
    permalink = str(media.get("permalink") or "") or None
    staging_key = _staging_key_for(database, attempt_id)
    with connect(database) as connection:
        update_attempt(
            connection,
            attempt_id,
            state="published",
            media_id=media_id,
            permalink=permalink,
        )
        row = connection.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
    _delete_staging(staging_key, staging=staging, database=database, attempt_id=attempt_id)
    return {
        "dry_run": False,
        "published": True,
        "recovered": True,
        "pack_id": row["pack_id"],
        "media_id": media_id,
        "permalink": permalink,
        "container_id": row["container_id"],
        "attempt_id": attempt_id,
        "staging_deleted": bool(staging_key),
    }


def _delete_staging(
    key: str | None,
    *,
    staging: R2StagingAdapter | None,
    database: Path,
    attempt_id: int,
) -> None:
    if not key:
        return
    adapter = staging or R2StagingAdapter()
    adapter.delete(key)
    with connect(database) as connection:
        update_attempt(connection, attempt_id, staging_key=None)


def _staging_key_for(database: Path, attempt_id: int) -> str | None:
    with connect(database) as connection:
        row = connection.execute(
            "SELECT staging_key FROM attempts WHERE id = ?",
            (attempt_id,),
        ).fetchone()
    if row is None or not row["staging_key"]:
        return None
    return str(row["staging_key"])


def _matching_media(client: GraphClient, user_id: str, caption: str) -> dict[str, object] | None:
    try:
        recent = client.recent_media(user_id)
    except InstagramError as exc:
        if exc.kind == "timeout":
            raise InstagramError(
                "Checking whether the post was already published timed out. No new container was created.",
                kind="timeout",
            ) from exc
        raise
    for item in recent:
        if str(item.get("caption") or "") == caption and item.get("id"):
            return item
    return None


def _permalink(client: GraphClient, media_id: str) -> str | None:
    try:
        payload = client.media(media_id)
    except InstagramError:
        return None
    permalink = payload.get("permalink")
    if isinstance(permalink, str) and permalink.strip():
        return permalink.strip()
    return None


def _attempt_for_container(database: Path, artwork_id: str, container_id: str) -> int | None:
    with connect(database) as connection:
        row = connection.execute(
            """
            SELECT id FROM attempts
            WHERE artwork_id = ? AND container_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (artwork_id, container_id),
        ).fetchone()
    if row is None:
        row_attempt = blocking_without_id(database, artwork_id)
        return row_attempt
    return int(row["id"])


def blocking_without_id(database: Path, artwork_id: str) -> int | None:
    with connect(database) as connection:
        row = connection.execute(
            """
            SELECT id FROM attempts
            WHERE artwork_id = ? AND state IN ('publishing', 'container_created', 'unknown')
            ORDER BY id DESC LIMIT 1
            """,
            (artwork_id,),
        ).fetchone()
    return None if row is None else int(row["id"])


def _hosts_or_empty() -> dict[str, object]:
    try:
        return load_hosts()
    except InstagramError:
        raise


def _renewal_text(remaining: float | None) -> str:
    if remaining is None:
        return (
            "Expiry is not recorded. Long-lived Instagram Login tokens last about 60 days. "
            "When fewer than 7 days remain, run `anskar instagram renew`. "
            "That calls Meta's ig_refresh_token grant and stores the new token without printing it."
        )
    days = round(remaining / 86400, 1)
    if remaining < 0:
        return "The token is expired. Install a new one with `anskar instagram install-token --from FILE`."
    if remaining < RENEW_WITHIN_DAYS * 86400:
        return (
            f"The token expires in {days} days. Run `anskar instagram renew` to refresh it "
            "through Meta's ig_refresh_token grant."
        )
    return (
        f"The token expires in {days} days. Renewal is not due. "
        "When fewer than 7 days remain, run `anskar instagram renew`."
    )


def _sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)

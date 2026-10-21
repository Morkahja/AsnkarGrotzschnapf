"""Command line for the poetry archive."""

import argparse
import sys
from datetime import date
from pathlib import Path

from anskar.archive.importer import CollectionFormatError, import_collection
from anskar.archive.models import ArchiveError, Poem
from anskar.archive.selection import confirm_pending, select_poem, skip_pending
from anskar.artwork.generator import format_artwork, format_plan, generate_pending, plan_pending
from anskar.artwork.png import pixels_for_print
from anskar.artwork.settings import load_settings
from anskar.config import PROJECT_ROOT, database_path
from anskar.daily.errors import DailyError
from anskar.daily.pipeline import daily_run, list_runs, resume_run, run_status
from anskar.daily.power import decide_after_run, verify_rtc_wake
from anskar.daily.schedule import install_units, prepare_system_units, schedule_status
from anskar.db import connect
from anskar.etsy.connect import connect_etsy, etsy_connection_status, test_etsy_connection
from anskar.etsy.credentials import setup_etsy_interactive
from anskar.etsy.errors import EtsyError
from anskar.etsy.oauth import DEFAULT_REDIRECT_URI, DEFAULT_SCOPES, EXPECTED_SHOP_NAME
from anskar.etsy.service import publish_pack as etsy_publish_pack
from anskar.etsy.state import STATES as ETSY_STATES, set_etsy_state
from anskar.instagram.credentials import install_token
from anskar.instagram.errors import InstagramError
from anskar.instagram.r2 import (
    cleanup_abandoned,
    load_r2_credentials,
    r2_configured,
    setup_r2_interactive,
    smoke_test,
)
from anskar.instagram.service import (
    connection_status,
    list_pack_attempts,
    prepare,
    publish,
    renew_token,
)
from anskar.interpret import ABSTRACTIONS, DEFAULT_ABSTRACTION, InterpreterError, OllamaInterpreter
from anskar.lab.experiments import experiments_dir
from anskar.models import PRODUCTION_MODEL_ID, installed_models
from anskar.printmaker import PrintError, approve_artwork
from anskar.style import production_style


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    path = database_path()
    try:
        if args.command == "import":
            return _import(path, args.collection)
        if args.command == "generate":
            return _generate(
                path,
                pending=args.pending,
                dry_run=args.dry_run,
                style=args.style,
                composition=args.composition,
                model=args.model,
                abstraction=args.abstraction,
                width=args.width,
                height=args.height,
                print_inches=args.print_inches,
            )
        if args.command == "lab":
            return _lab(args.port)
        if args.command == "print":
            return _print(path, args.metadata)
        if args.command == "instagram":
            return _instagram(args)
        if args.command == "etsy":
            return _etsy(args)
        if args.command == "daily-run":
            return _daily_run(dry_run=args.dry_run)
        if args.command == "daily-status":
            return _daily_status()
        if args.command == "daily-list":
            return _daily_list(limit=args.limit)
        if args.command == "daily-resume":
            return _daily_resume(args.run_id)
        if args.command == "schedule":
            return _schedule(args)
        if args.command == "select":
            if args.confirm:
                return _confirm(path)
            if args.skip:
                return _skip(path)
            return _select(path)
    except (
        ArchiveError,
        CollectionFormatError,
        PrintError,
        InstagramError,
        EtsyError,
        DailyError,
        ValueError,
    ) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="anskar")
    commands = parser.add_subparsers(dest="command", required=True)

    import_command = commands.add_parser("import", help="import a JSON poetry collection")
    import_command.add_argument("collection", type=Path)

    select_command = commands.add_parser("select", help="show the pending poem, or choose one")
    mode = select_command.add_mutually_exclusive_group()
    mode.add_argument("--confirm", action="store_true", help="mark the pending poem used")
    mode.add_argument("--skip", action="store_true", help="dismiss the pending poem without using it")

    generate_command = commands.add_parser("generate", help="generate artwork for the pending poem")
    generate_command.add_argument("--pending", action="store_true", help="use the pending poem")
    generate_command.add_argument("--dry-run", action="store_true", help="print the prompt and settings only")
    generate_command.add_argument(
        "--style",
        default="painterly",
        choices=("dreamlike", "painterly", "symbolic", "none"),
        help="house style preset (default: painterly; none sends the poem text only)",
    )
    generate_command.add_argument(
        "--composition",
        default="wrapped",
        choices=("text", "style_text", "wrapped"),
        help="text only, style plus the poem, or a wrapped instruction (default: wrapped)",
    )
    generate_command.add_argument(
        "--model",
        default=PRODUCTION_MODEL_ID,
        choices=tuple(model.id for model in installed_models()),
        help="installed image model (default: z-image-turbo)",
    )
    generate_command.add_argument(
        "--abstraction",
        default=DEFAULT_ABSTRACTION,
        choices=ABSTRACTIONS,
        help="how Ollama builds the picture (default: surreal)",
    )
    generate_command.add_argument("--width", type=int, help="image width in pixels")
    generate_command.add_argument("--height", type=int, help="image height in pixels")
    generate_command.add_argument(
        "--print-inches",
        type=float,
        help="square print side in inches; pixels are that side at 200 PPI, rounded up to a multiple of 8",
    )

    lab_command = commands.add_parser("lab", help="open the local prompt lab")
    lab_command.add_argument("--port", type=int, default=8765)

    print_command = commands.add_parser("print", help="prepare a local print pack from one lab image")
    print_command.add_argument("metadata", type=Path)

    instagram = commands.add_parser("instagram", help="connect an approved pack to Instagram")
    instagram_commands = instagram.add_subparsers(dest="instagram_command", required=True)
    instagram_commands.add_parser("status", help="read the connected Instagram account")
    install = instagram_commands.add_parser(
        "install-token",
        help="store a token from a local file without printing it",
    )
    install.add_argument("--from", dest="token_file", type=Path, required=True)
    install.add_argument("--expires-at", help="ISO date the token expires, if you know it")
    renew = instagram_commands.add_parser("renew", help="refresh a long-lived token when it is near expiry")
    renew.add_argument("--force", action="store_true", help="refresh even if expiry is more than 7 days away")
    prepare_command = instagram_commands.add_parser("prepare", help="write a local Instagram post from a pack")
    prepare_command.add_argument("pack_id")
    publish_command = instagram_commands.add_parser("publish", help="review or publish one prepared pack")
    publish_command.add_argument("pack_id")
    mode = publish_command.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="validate the post and do not call the publishing API")
    mode.add_argument("--confirm", action="store_true", help="publish this pack to Instagram")
    publish_command.add_argument(
        "--replace-unknown",
        action="store_true",
        help="after a timed-out attempt with no container id, allow another container",
    )
    attempts = instagram_commands.add_parser("attempts", help="show stored publication attempts for a pack")
    attempts.add_argument("pack_id")
    instagram_commands.add_parser(
        "setup-r2",
        help="store Cloudflare R2 credentials in ~/.config/anskar/ (interactive terminal)",
    )
    instagram_commands.add_parser(
        "test-r2",
        help="upload, fetch, and delete a temporary JPEG in the Anskar R2 bucket",
    )
    cleanup = instagram_commands.add_parser(
        "cleanup-r2",
        help="list or delete abandoned Instagram staging objects in R2",
    )
    cleanup.add_argument(
        "--delete",
        action="store_true",
        help="delete abandoned objects after listing them",
    )

    etsy = commands.add_parser("etsy", help="Etsy digital-print listings")
    etsy_commands = etsy.add_subparsers(dest="etsy_command", required=True)
    etsy_commands.add_parser("setup", help="store Etsy API keystring and shared secret in ~/.config/anskar/")
    etsy_connect = etsy_commands.add_parser(
        "connect",
        help="authorize Anskar Gortzschnapf Publisher for the Anskartprints shop (OAuth + PKCE)",
    )
    etsy_connect.add_argument(
        "--no-browser",
        action="store_true",
        help="print the authorize URL without opening a browser",
    )
    etsy_connect.add_argument(
        "--shop-id",
        help="numeric Etsy shop id when the account has more than one shop",
    )
    etsy_connect.add_argument(
        "--confirm-shop",
        action="store_true",
        help="allow replacing a stored shop id or accepting a name other than Anskartprints",
    )
    etsy_commands.add_parser(
        "status",
        help="show OAuth connection, token validity, scopes, shop identity, and seller verification state",
    )
    etsy_commands.add_parser(
        "test",
        help="read-only authenticated check of the connected Etsy shop (no listings)",
    )
    etsy_state = etsy_commands.add_parser(
        "set-state",
        help="set seller verification state: pending_verification, ready, or disabled",
    )
    etsy_state.add_argument("state", choices=ETSY_STATES)
    etsy_publish = etsy_commands.add_parser("publish", help="publish one pack as a digital download")
    etsy_publish.add_argument("pack_id")
    etsy_mode = etsy_publish.add_mutually_exclusive_group(required=True)
    etsy_mode.add_argument("--dry-run", action="store_true")
    etsy_mode.add_argument("--confirm", action="store_true")

    daily = commands.add_parser("daily-run", help="automatic poem → artwork → Instagram → Etsy")
    daily.add_argument(
        "--dry-run",
        action="store_true",
        help="run through generation and pack preparation without live publication",
    )
    commands.add_parser("daily-status", help="show the active or latest daily run")
    daily_list = commands.add_parser("daily-list", help="list recent daily runs")
    daily_list.add_argument("--limit", type=int, default=20)
    daily_resume = commands.add_parser("daily-resume", help="resume an incomplete daily run")
    daily_resume.add_argument("run_id")

    schedule = commands.add_parser("schedule", help="noon systemd schedule (user timer + optional system wake)")
    schedule_commands = schedule.add_subparsers(dest="schedule_command", required=True)
    schedule_commands.add_parser("status", help="show timer units, RTC wake, and return-to-sleep policy")
    install = schedule_commands.add_parser(
        "install",
        help="install user systemd units at noon (does not enable unless --enable is passed)",
    )
    install.add_argument(
        "--enable",
        action="store_true",
        help="enable and start the user timer after installing (explicit confirmation)",
    )
    schedule_commands.add_parser(
        "prepare-system",
        help="stage system wake units under data/systemd-system/ (does not enable; needs sudo later)",
    )
    schedule_commands.add_parser(
        "verify-wake",
        help="read-only RTC/wake check; never suspends and never publishes",
    )
    return parser


def _import(path: Path, collection: Path) -> int:
    if not collection.is_file():
        print(f"Collection file not found: {collection}", file=sys.stderr)
        return 1
    with connect(path) as connection:
        report = import_collection(connection, collection, on=date.today())
    print(f"Imported {report.imported} poems.")
    print(f"Duplicates left unchanged: {report.duplicates}.")
    print(f"Rejected: {len(report.rejected)}.")
    for title, reason in report.rejected:
        print(f"Rejected {title}: {reason}", file=sys.stderr)
    return 1 if report.rejected else 0


def _select(path: Path) -> int:
    with connect(path) as connection:
        selection = select_poem(connection)
    print(_format_poem(selection.poem))
    if selection.newly_pending:
        print("\nPending selection. Not marked used.")
    else:
        print("\nPending selection, unchanged. Not marked used.")
    return 0


def _confirm(path: Path) -> int:
    with connect(path) as connection:
        selection = confirm_pending(connection)
    print(f"Confirmed: {selection.poem.title} by {selection.poem.author}")
    print("The poem is marked used.")
    return 0


def _skip(path: Path) -> int:
    with connect(path) as connection:
        selection = skip_pending(connection)
    print(f"Skipped: {selection.poem.title} by {selection.poem.author}")
    print("The poem is not marked used.")
    return 0


def _generate(
    path: Path,
    *,
    pending: bool,
    dry_run: bool,
    style: str,
    composition: str,
    model: str,
    abstraction: str,
    width: int | None,
    height: int | None,
    print_inches: float | None,
) -> int:
    if not pending:
        print("Generation reads the pending poem. Pass --pending.", file=sys.stderr)
        return 1
    if print_inches is not None and (width is not None or height is not None):
        print("Pass print inches or pixel size, not both.", file=sys.stderr)
        return 1
    if print_inches is not None:
        try:
            width, height = pixels_for_print(print_inches, print_inches)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    settings = load_settings()
    choice = production_style(style, composition)
    if dry_run:
        print(
            format_plan(
                plan_pending(
                    path,
                    settings,
                    style=choice,
                    model_id=model,
                    abstraction=abstraction,
                    width=width,
                    height=height,
                )
            )
        )
        return 0
    print(
        format_artwork(
            generate_pending(
                path,
                settings,
                style=choice,
                model_id=model,
                abstraction=abstraction,
                width=width,
                height=height,
            )
        )
    )
    return 0


def _lab(port: int) -> int:
    from anskar.lab.server import serve

    try:
        serve(port)
    except KeyboardInterrupt:
        print("\nPrompt lab stopped.")
        return 0
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def _print(database: Path, metadata: Path) -> int:
    try:
        OllamaInterpreter().release_for_image_generation()
    except InterpreterError:
        pass
    resolved = metadata.resolve()
    artworks = load_settings().artworks_dir
    data_root = (PROJECT_ROOT / "data").resolve()
    if (
        data_root in resolved.parents
        and "publish-packs" not in resolved.parts
        and experiments_dir().resolve() not in resolved.parents
        and artworks.resolve() not in resolved.parents
    ):
        artworks = resolved.parent
    manifest = approve_artwork(
        metadata,
        experiments=experiments_dir(),
        database=database,
        artworks=artworks,
    )
    print(manifest["pack_path"])
    print("Publication-ready." if manifest["publication_ready"] else "Not publication-ready.")
    print(manifest["rights"]["reason"])
    return 0


def _instagram(args: argparse.Namespace) -> int:
    if args.instagram_command == "status":
        report = connection_status()
        print(f"Connected: @{report['username']}")
        print(f"User id: {report['user_id']}")
        print(f"Account type: {report['account_type'] or 'unspecified'}")
        print(f"Expected account: @{report['expected_username']}")
        print("Account match: yes")
        print("Permission instagram_business_basic: confirmed")
        print("Permission instagram_business_content_publish: not used by this read-only check")
        print(report["renewal"])
        if r2_configured():
            public = load_r2_credentials().public()
            print(
                f"R2: configured bucket={public['bucket']} host={public['endpoint_host']} "
                f"prefix={public['object_prefix']}"
            )
        else:
            print("R2: not configured. Run `anskar instagram setup-r2` in a terminal.")
        print("No post was published.")
        return 0
    if args.instagram_command == "install-token":
        install_token(args.token_file, expires_at=args.expires_at)
        print("Instagram token stored. It was not printed.")
        return 0
    if args.instagram_command == "renew":
        report = renew_token(force=args.force)
        print("Instagram token renewed. It was not printed.")
        if report.get("expires_at"):
            print(f"Expires: {report['expires_at']}")
        return 0
    if args.instagram_command == "setup-r2":
        stored = setup_r2_interactive()
        public = stored.public()
        print("R2 credentials stored. Secrets were not printed.")
        print(f"Path: {public['path']}")
        print(f"Bucket: {public['bucket']}")
        print(f"Endpoint host: {public['endpoint_host']}")
        return 0
    if args.instagram_command == "test-r2":
        report = smoke_test()
        print("R2 staging smoke test passed.")
        print(f"Bucket: {report['bucket']}")
        print(f"Endpoint host: {report['endpoint_host']}")
        print("Upload, presigned GET, and delete succeeded.")
        return 0
    if args.instagram_command == "cleanup-r2":
        report = cleanup_abandoned(delete=args.delete)
        print(
            f"R2 objects listed={report['listed']} active={report['active']} "
            f"abandoned={report['abandoned']} deleted={report['deleted']}"
        )
        if not args.delete and report["abandoned"]:
            print("Pass --delete to remove abandoned objects. Keys are not printed.")
        return 0
    if args.instagram_command == "prepare":
        record = prepare(args.pack_id)
        _print_post(record)
        return 0
    if args.instagram_command == "attempts":
        rows = list_pack_attempts(args.pack_id)
        if not rows:
            print("No publication attempts.")
            return 0
        for row in rows:
            print(
                f"{row['id']}: {row['state']}"
                + (f" container {row['container_id']}" if row["container_id"] else "")
                + (f" media {row['media_id']}" if row["media_id"] else "")
                + (f" {row['permalink']}" if row["permalink"] else "")
                + (" staged" if row.get("staged") else "")
                + (f" — {row['error_message']}" if row["error_message"] else "")
            )
        return 0
    result = publish(
        args.pack_id,
        dry_run=args.dry_run,
        confirm=args.confirm,
        replace_unknown=args.replace_unknown,
    )
    if result.get("dry_run"):
        print(f"Dry run for {result['pack_id']}. Nothing was published.")
        print("Image staged to R2, probed, and deleted.")
        print(f"Title: {result.get('title')}")
        print(f"Author: {result.get('author')}")
        return 0
    print(f"Published {result['pack_id']}.")
    print(f"Media id: {result['media_id']}")
    if result.get("permalink"):
        print(f"Permalink: {result['permalink']}")
    if result.get("staging_deleted"):
        print("Temporary R2 image deleted.")
    return 0


def _print_post(record: dict[str, object]) -> None:
    print(f"Pack: {record['pack_id']}")
    print(f"JPEG: {record['image']}")
    print(f"Pixels: {record['width']}×{record['height']}")
    if record.get("title") and record.get("author"):
        print(f"Title: {record['title']}")
        print(f"Author: {record['author']}")
    else:
        print("No documented poem title or author is recorded.")
    print("Poem included in caption." if record.get("poem_included") else "Poem body omitted from caption.")
    print("AI disclosure included.")
    if record.get("etsy_url"):
        print(f"Etsy: {record['etsy_url']}")
    else:
        print("Etsy link: not configured.")
    print("Image hosting: Cloudflare R2 private staging (presigned URL at publish time).")
    print("Approved for publication." if record.get("publishable") else "Not approved for publication.")
    print("The image has no added text.")
    caption_path = Path(str(record["image"])).with_name("caption.txt")
    if caption_path.is_file():
        print("--- caption preview ---")
        print(caption_path.read_text(encoding="utf-8").rstrip())
        print("--- end caption ---")


def _etsy(args: argparse.Namespace) -> int:
    if args.etsy_command == "setup":
        stored = setup_etsy_interactive()
        public = stored.public()
        print("Etsy API key material stored. Secrets were not printed.")
        print(f"Path: {public['path']}")
        print(f"Registered callback: {public['redirect_uri']}")
        print(f"Expected shop: {EXPECTED_SHOP_NAME}")
        print("Next: register that exact callback URL with Etsy, then run `anskar etsy connect`.")
        print("OAuth success does not change seller verification. Publishing still needs `set-state ready`.")
        return 0
    if args.etsy_command == "connect":
        result = connect_etsy(
            open_browser=not args.no_browser,
            confirm_shop=args.confirm_shop,
            shop_id_override=args.shop_id,
        )
        print("Etsy OAuth connected. Tokens were stored with owner-only permissions.")
        print(f"Shop: {result['shop_name']} (id {result['shop_id']})")
        print(f"Scopes: {result['scopes']}")
        print(f"Token expires: {result['expires_at']}")
        print(f"Callback used: {result['redirect_uri']}")
        print(
            f"Seller verification state is still {result['integration_state']} "
            "(OAuth alone does not set ready)."
        )
        return 0
    if args.etsy_command == "status":
        report = etsy_connection_status()
        print(f"Seller verification state: {report['integration_state']}")
        print(f"Expected shop: {report['expected_shop_name']}")
        print(f"Registered callback: {report.get('redirect_uri') or DEFAULT_REDIRECT_URI}")
        if report.get("oauth_configured"):
            print(f"OAuth: configured")
            print(f"Shop id: {report['shop_id']}")
            print(f"Shop name: {report['shop_name']}")
            print(f"Scopes: {report['scopes'] or DEFAULT_SCOPES}")
            print(f"Token valid: {'yes' if report['token_valid'] else 'no'}")
            if report.get("expires_at"):
                print(f"Expires: {report['expires_at']}")
            print(f"Refresh token stored: {'yes' if report.get('has_refresh_token') else 'no'}")
        else:
            print("OAuth: not connected")
            if report.get("detail"):
                print(report["detail"])
        return 0
    if args.etsy_command == "test":
        result = test_etsy_connection()
        print(f"Shop id: {result['shop_id']}")
        print(f"Shop name: {result['shop_name']}")
        print(f"Name matches {result['expected_shop_name']}: {'yes' if result['name_matches'] else 'no'}")
        print(f"Token valid: {'yes' if result['token_valid'] else 'no'}")
        print(f"Seller verification state: {result['integration_state']}")
        if not result["ok"]:
            print("Shop verification failed.", file=sys.stderr)
            return 1
        print("Authenticated shop check succeeded. No listings were created or changed.")
        return 0
    if args.etsy_command == "set-state":
        stored = set_etsy_state(args.state)
        print(f"Etsy seller verification state set to {stored}.")
        if stored == "ready":
            print("New daily runs may publish Etsy listings. Queued backlog still needs explicit publish.")
        return 0
    if not getattr(args, "confirm", False) and not getattr(args, "dry_run", False):
        print("Pass --dry-run or --confirm.", file=sys.stderr)
        return 1
    if args.confirm and not args.dry_run:
        result = etsy_publish_pack(args.pack_id, dry_run=False)
        print(f"Published Etsy listing for {result['pack_id']}.")
        print(f"Listing id: {result['listing_id']}")
        if result.get("listing_url"):
            print(f"URL: {result['listing_url']}")
        return 0
    result = etsy_publish_pack(args.pack_id, dry_run=True)
    print(f"Etsy dry run for {result['pack_id']}. Nothing was published.")
    print(f"Title: {result['title']}")
    return 0


def _daily_run(*, dry_run: bool) -> int:
    result = daily_run(dry_run=dry_run)
    print(f"Daily run {result['id']}: {result['status']}")
    if result.get("title") and result.get("author"):
        print(f"Poem: {result['title']} by {result['author']}")
    if result.get("pack_id"):
        print(f"Pack: {result['pack_id']}")
    print(f"Artwork: {result.get('artwork_status')}")
    print(f"Instagram: {result.get('instagram_status')}")
    print(f"Etsy: {result.get('etsy_status')}")
    if result.get("instagram_media_id"):
        print(f"Instagram media: {result['instagram_media_id']}")
    if result.get("instagram_permalink"):
        print(f"Instagram: {result['instagram_permalink']}")
    if result.get("etsy_listing_id"):
        print(f"Etsy listing: {result['etsy_listing_id']}")
    if dry_run:
        print("Dry run. The poem was not marked used. Nothing was published live.")
    elif result["status"] == "completed":
        print("Poem marked used after Instagram success. Etsy remains independent.")
        decision = decide_after_run()
        print(f"Power: {decision.action} — {decision.reason}")
    if result.get("error_message"):
        print(f"Note: {result['error_message']}", file=sys.stderr)
    return 0 if result["status"] == "completed" else 1


def _daily_status() -> int:
    report = run_status()
    print(f"Auto-approval in daily mode: {'yes' if report['auto_approval'] else 'no'}")
    etsy = report.get("etsy_integration") or {}
    print(f"Etsy integration: {etsy.get('state')}")
    print(f"Etsy blocks daily completion: {'yes' if report.get('etsy_blocks_daily_completion') else 'no'}")
    active = report.get("active")
    latest = report.get("latest")
    if active:
        print(f"Active: {active['id']} ({active['status']})")
        if active.get("pack_id"):
            print(f"Pack: {active['pack_id']}")
        if active.get("error_message"):
            print(f"Last error: {active['error_message']}")
    else:
        print("Active: none")
    if latest and (not active or latest["id"] != active["id"]):
        print(
            f"Latest: {latest['id']} ({latest['status']}) "
            f"ig={latest.get('instagram_status')} etsy={latest.get('etsy_status')}"
        )
    return 0


def _daily_list(*, limit: int) -> int:
    rows = list_runs(limit=limit)
    if not rows:
        print("No daily runs.")
        return 0
    for row in rows:
        print(
            f"{row['id']}: {row['status']}"
            + (f" {row['title']}" if row.get("title") else "")
            + (f" pack={row['pack_id']}" if row.get("pack_id") else "")
            + (" dry-run" if row.get("dry_run") else "")
        )
    return 0


def _daily_resume(run_id: str) -> int:
    result = resume_run(run_id)
    print(f"Resumed {result['id']}: {result['status']}")
    if result.get("error_message") and result["status"] != "completed":
        print(result["error_message"], file=sys.stderr)
        return 1
    if result["status"] == "completed":
        print("Completed. Poem marked used only after both publications succeeded.")
    return 0 if result["status"] == "completed" else 1


def _schedule(args: argparse.Namespace) -> int:
    if args.schedule_command == "status":
        report = schedule_status()
        print(f"OnCalendar: {report['on_calendar']} (Persistent={report['persistent']})")
        print(f"User service installed: {'yes' if report['service_installed'] else 'no'}")
        print(f"User timer installed: {'yes' if report['timer_installed'] else 'no'}")
        print(f"User timer enabled: {'yes' if report['enabled'] else 'no'}")
        print(f"User timer active: {'yes' if report['active'] else 'no'}")
        if report.get("next_elapse"):
            print(f"User timer next: {report['next_elapse']}")
        print(f"User service path: {report['service_path']}")
        print(f"System timer in /etc: {'yes' if report['system_timer_installed'] else 'no'}")
        print(f"System timer enabled: {'yes' if report['system_enabled'] else 'no'}")
        print(f"Return to sleep: {report['return_to_sleep']}")
        rtc = report.get("rtc") or {}
        print(f"RTC wakealarm present: {'yes' if rtc.get('wakealarm_exists') else 'no'}")
        if not report["enabled"] and not report["system_enabled"]:
            print("Install user units with `anskar schedule install`. Enable only with `--enable`.")
            print("For deep-sleep wake, stage system units with `anskar schedule prepare-system`.")
        return 0
    if args.schedule_command == "prepare-system":
        report = prepare_system_units()
        print(f"Staged system service: {report['service_path']}")
        print(f"Staged system timer: {report['timer_path']}")
        print(report["note"])
        print(report["return_to_sleep"])
        print("Activation (requires your approval; disables the user timer first):")
        for command in report["activation_commands"]:
            print(f"  {command}")
        print("Rollback:")
        for command in report["rollback_commands"]:
            print(f"  {command}")
        return 0
    if args.schedule_command == "verify-wake":
        report = verify_rtc_wake()
        print(f"wakealarm: {report['wakealarm_path']} exists={'yes' if report['wakealarm_exists'] else 'no'}")
        print(f"rtcwake: {report.get('rtcwake') or 'missing'}")
        if report.get("rtcwake_show"):
            print(report["rtcwake_show"])
        print("No suspend and no publish were performed.")
        return 0 if report.get("wakealarm_exists") else 1
    report = install_units(enable=args.enable)
    print(f"Installed service: {report['service_path']}")
    print(f"Installed timer: {report['timer_path']}")
    print(f"Schedule: {report['on_calendar']} Persistent={report['persistent']}")
    if args.enable:
        print("User timer enabled and started.")
    else:
        print("Timer not enabled. Pass --enable to enable after reviewing the units.")
        print("Or run: systemctl --user enable --now anskar-daily.timer")
    return 0


def _format_poem(poem: Poem) -> str:
    return f"Title: {poem.title}\nAuthor: {poem.author}\n\n{poem.body}"

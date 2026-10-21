"""Build a local Instagram post from an approved publish pack."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from anskar.instagram.errors import InstagramError
from anskar.instagram.hashtags import append_hashtags, format_hashtag_block, hashtags_from_manifest
from anskar.instagram.hosting import etsy_shop_url, image_url, validate_public_url
from anskar.instagram.jpeg import MAX_LONG_EDGE, jpeg_size, validate_jpeg
from anskar.printmaker import packs_dir

CAPTION_LIMIT = 2200
ALT_LIMIT = 1000
HASHTAG_LIMIT = 30
MENTION_LIMIT = 20
STORED_DISCLOSURE = (
    "This image was generated locally by software. "
    "It is not a photograph and not a historical document."
)


def load_manifest(pack_id: str, *, packs: Path | None = None) -> tuple[Path, dict]:
    if (
        not pack_id
        or pack_id in {".", ".."}
        or "/" in pack_id
        or "\\" in pack_id
        or Path(pack_id).name != pack_id
    ):
        raise InstagramError("Pack id is not valid.", kind="media")
    root = packs or packs_dir()
    folder = root / pack_id
    manifest_path = folder / "manifest.json"
    if not manifest_path.is_file():
        raise InstagramError(f"Publish pack not found: {pack_id}", kind="media")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstagramError(f"Publish pack {pack_id} has no readable manifest.", kind="media") from exc
    if not isinstance(payload, dict):
        raise InstagramError(f"Publish pack {pack_id} has no readable manifest.", kind="media")
    return folder, payload


def compose(manifest: dict, *, etsy_url: str | None) -> dict[str, object]:
    """Caption and accessibility text from recorded title, author, and disclosure."""
    rights = manifest.get("rights") if isinstance(manifest.get("rights"), dict) else {}
    matched = bool(rights.get("matched"))
    title = rights.get("title") if isinstance(rights.get("title"), str) else ""
    author = rights.get("author") if isinstance(rights.get("author"), str) else ""
    title = title.strip()
    author = author.strip()
    if matched and (not title or not author):
        raise InstagramError(
            "The pack matches a poem but does not record both a title and an author.",
            kind="media",
        )
    disclosure = manifest.get("ai_disclosure")
    if not isinstance(disclosure, str) or not disclosure.strip():
        disclosure = STORED_DISCLOSURE
    else:
        disclosure = disclosure.strip()
    poem = ""
    if matched and isinstance(manifest.get("source_text"), str):
        poem = manifest["source_text"].strip()
    shop = None
    if etsy_url:
        shop = validate_public_url(etsy_url, label="Etsy shop URL")
    tags = hashtags_from_manifest(manifest)
    tag_block = format_hashtag_block(tags)
    tag_budget = len(tag_block)
    if not matched:
        caption = (
            "This artwork is not linked to a documented poem, so it has no title or author to attribute.\n\n"
            f"{disclosure}"
        )
        alt = f"Locally generated artwork with no documented poem title. {disclosure}"
        poem_included = False
    else:
        base = f"{title}\n{author}\n\n{disclosure}"
        if shop:
            base = f"{base}\n\nPrint: {shop}"
        with_poem = f"{title}\n{author}\n\n{poem}\n\n{disclosure}" if poem else base
        if shop and poem:
            with_poem = f"{with_poem}\n\nPrint: {shop}"
        if len(with_poem) + tag_budget <= CAPTION_LIMIT:
            caption = with_poem
            poem_included = bool(poem)
        elif len(base) + tag_budget <= CAPTION_LIMIT:
            caption = base
            poem_included = False
        else:
            raise InstagramError(
                "The caption does not fit Instagram's 2200-character limit.",
                kind="media",
            )
        alt = f"Artwork titled {title}, after the poem by {author}. The image has no added text. {disclosure}"
    caption = append_hashtags(caption, tags)
    _check_limits(caption)
    if len(alt) > ALT_LIMIT:
        alt = alt[: ALT_LIMIT - 1].rstrip() + "…"
    return {
        "caption": caption,
        "alt_text": alt,
        "title": title or None,
        "author": author or None,
        "poem_included": poem_included,
        "etsy_url": shop,
        "hashtags": tags,
        "publishable": matched and bool(manifest.get("publication_ready")),
        "text_added_to_image": False,
    }


def prepare_post(
    pack_id: str,
    *,
    packs: Path | None = None,
    destination: Path,
    hosts: dict[str, object] | None = None,
    jpeg_writer=None,
) -> dict[str, object]:
    """Write a local JPEG, caption, and accessibility text. The pack is not changed."""
    folder, manifest = load_manifest(pack_id, packs=packs)
    preview = folder / "instagram" / "preview.png"
    if not preview.is_file():
        raise InstagramError(f"Publish pack {pack_id} has no Instagram preview.", kind="media")
    text = compose(manifest, etsy_url=etsy_shop_url(hosts))
    destination.mkdir(parents=True, exist_ok=True)
    image = destination / "image.jpg"
    writer = jpeg_writer or write_instagram_jpeg
    writer(preview, image)
    width, height = validate_jpeg(image)
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    caption_digest = hashlib.sha256(str(text["caption"]).encode("utf-8")).hexdigest()
    public = image_url(pack_id, hosts)
    if public:
        public = validate_public_url(public)
    record = {
        "pack_id": pack_id,
        "artwork_id": str(manifest.get("artwork_id") or pack_id),
        "publication_ready": bool(manifest.get("publication_ready")),
        "image": str(image),
        "width": width,
        "height": height,
        "image_sha256": digest,
        "caption_sha256": caption_digest,
        "public_image_url": public,
        "source_preview": str(preview),
        **text,
    }
    (destination / "caption.txt").write_text(str(text["caption"]) + "\n", encoding="utf-8")
    (destination / "alt.txt").write_text(str(text["alt_text"]) + "\n", encoding="utf-8")
    (destination / "post.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return record


def write_instagram_jpeg(source: Path, destination: Path) -> None:
    """Convert the preview to JPEG. The copy is never enlarged and gains no text."""
    magick = shutil.which("magick")
    if magick is None:
        raise InstagramError("ImageMagick is not available, so no JPEG was written.", kind="media")
    width, height = _png_size(source)
    command = [magick, str(source), "-colorspace", "sRGB"]
    long_edge = max(width, height)
    if long_edge > MAX_LONG_EDGE:
        scale = MAX_LONG_EDGE / long_edge
        command.extend(["-resize", f"{max(1, round(width * scale))}x{max(1, round(height * scale))}!"])
    command.extend(["-quality", "95", "-sampling-factor", "4:4:4", str(destination)])
    completed = subprocess.run(
        command,
        shell=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0 or not destination.is_file():
        destination.unlink(missing_ok=True)
        raise InstagramError("The Instagram JPEG could not be written.", kind="media")


def _png_size(path: Path) -> tuple[int, int]:
    from anskar.artwork.png import png_size

    try:
        return png_size(path)
    except (OSError, ValueError) as exc:
        raise InstagramError("The Instagram preview could not be read.", kind="media") from exc


def _check_limits(caption: str) -> None:
    if len(caption) > CAPTION_LIMIT:
        raise InstagramError("The caption is longer than Instagram's 2200-character limit.", kind="media")
    if caption.count("#") > HASHTAG_LIMIT:
        raise InstagramError("The caption has more than 30 hashtags.", kind="media")
    if caption.count("@") > MENTION_LIMIT:
        raise InstagramError("The caption has more than 20 mentions.", kind="media")


def assert_publishable(record: dict[str, object]) -> None:
    if not record.get("publication_ready") or not record.get("publishable"):
        raise InstagramError(
            "This pack is not approved for publication. It needs documented poem rights and a print-suitable image.",
            kind="media",
        )
    if not record.get("title") or not record.get("author"):
        raise InstagramError("The post has no recorded poem title and author.", kind="media")
    image = record.get("image")
    if not isinstance(image, str):
        raise InstagramError("The local JPEG is missing.", kind="media")
    validate_jpeg(Path(image))
    # Re-read dimensions so a substituted file cannot skip the earlier check.
    jpeg_size(Path(image))

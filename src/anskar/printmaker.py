"""Prepare a local print pack from one approved prompt-lab image.

Approval is explicit. The original image is copied and left in place.
The print file keeps those pixels. Nothing is sent to Instagram or Etsy.
"""

import json
import re
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from anskar.artwork.generator import SubprocessRunner
from anskar.artwork.png import (
    MINIMUM_PPI,
    TARGET_PPI,
    classify_ppi,
    effective_ppi,
    label_png_dpi,
    largest_size,
    png_size,
    print_sized,
    print_sizes,
)
from anskar.artwork.settings import GeneratorSettings
from anskar.config import PROJECT_ROOT
from anskar.models import COMMERCIAL_UNVERIFIED, model_allows_publication, require_model

AI_DISCLOSURE = (
    "This image was generated locally by software. "
    "It is not a photograph and not a historical document."
)
DIGITAL_NOTICE = (
    "This is a digital download. The customer does not receive a physical print."
)
RESOLUTION_NOTE = (
    "Print quality is the pixel count divided by the physical side. "
    "A DPI label does not add pixels. "
    "No ESRGAN weights are installed, and none were downloaded. "
    "sd-cli can be asked for 1600 or 2048 pixels on a side; both are multiples of 8 "
    "and within the 2400-pixel generator limit. Z-Image Turbo has produced 2400×2400 "
    "on this machine, so those sizes are inside a resolution it has already reached. "
    "RealVisXL V5.0 is an SDXL checkpoint reviewed at 1024×1024. "
    "This pack does not interpolate the approved image and does not replace it with a new sample. "
    "A larger native file would be a separate generation, not extra detail inside this file."
)
INSTAGRAM_LONG_EDGE = 1080


class PrintError(Exception):
    """The print pack could not be prepared."""


def packs_dir() -> Path:
    return PROJECT_ROOT / "data" / "publish-packs"


def orientation(width: int, height: int) -> str:
    if width == height:
        return "square"
    if width > height:
        return "landscape"
    return "portrait"


def preview_pixels(width: int, height: int, limit: int = INSTAGRAM_LONG_EDGE) -> tuple[int, int]:
    """Social preview size. Never enlarges, and never changes the aspect ratio."""
    if width < 1 or height < 1:
        raise PrintError("The artwork has no pixel size.")
    long_edge = max(width, height)
    if long_edge <= limit:
        return width, height
    scale = limit / long_edge
    return max(1, round(width * scale)), max(1, round(height * scale))


def meets_print_specification(
    source: tuple[int, int],
    final: tuple[int, int],
    method: str,
) -> bool:
    """Acceptable when some supported size is at least 150 PPI and pixels were not invented."""
    if method == "interpolation":
        return False
    if method == "none" and source != final:
        return False
    if method not in {"none", "generated"}:
        return False
    try:
        return print_sized(*final)
    except ValueError:
        return False


def artwork_id(record: dict) -> str:
    experiment = str(record.get("experiment") or "ungrouped")
    seed = record.get("seed")
    digest = str(record.get("prompt_sha256") or "nosha")[:8]
    if experiment == "production":
        poem_id = record.get("poem_id") or "x"
        raw = f"production-poem-{poem_id}-seed-{seed}-{digest}"
    else:
        raw = f"lab-{experiment}-seed-{seed}-{digest}"
    cleaned = re.sub(r"[^a-z0-9-]+", "-", raw.lower()).strip("-")
    return cleaned[:80] or "lab-artwork"


def approve_artwork(
    metadata_path: Path,
    *,
    experiments: Path,
    database: Path,
    settings: GeneratorSettings | None = None,
    runner: SubprocessRunner | None = None,
    packs: Path | None = None,
    artworks: Path | None = None,
) -> dict:
    """Copy one approved image into a new publish pack and describe its print sizes."""
    del settings, runner
    metadata_path = metadata_path.resolve()
    roots = [experiments.resolve()]
    if artworks is not None:
        roots.append(artworks.resolve())
    if not any(metadata_path == root or root in metadata_path.parents for root in roots):
        raise PrintError("Experiment file is not available.")
    if not metadata_path.is_file():
        raise PrintError("Experiment file is not available.")
    try:
        record = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PrintError("Experiment metadata is unreadable.") from exc
    if not isinstance(record, dict):
        raise PrintError("Experiment metadata is unreadable.")
    image = metadata_path.with_suffix(".png")
    if not image.is_file():
        image = Path(str(record.get("output_path") or ""))
    if not image.is_file():
        raise PrintError("The approved image is missing.")
    try:
        source_pixels = png_size(image)
    except (OSError, ValueError) as exc:
        raise PrintError(f"The approved image is not a readable PNG: {exc}") from exc
    pack_id = artwork_id(record)
    destination = (packs if packs is not None else packs_dir()) / pack_id
    if destination.exists():
        manifest_path = destination / "manifest.json"
        existing: dict | None = None
        if manifest_path.is_file():
            try:
                loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
                existing = loaded if isinstance(loaded, dict) else None
            except (OSError, json.JSONDecodeError):
                existing = None
        # Never overwrite a ready or already-published pack. A failed rights check
        # may leave a non-ready pack; rebuild it from the same original image.
        if existing is not None and (
            existing.get("publication_ready")
            or existing.get("published")
            or existing.get("instagram_published")
        ):
            raise PrintError(f"A print pack already exists: {destination}. Nothing was overwritten.")
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    try:
        return _fill_pack(
            destination,
            record,
            image,
            source_pixels,
            metadata_path,
            database=database,
            pack_id=pack_id,
        )
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def _fill_pack(
    destination: Path,
    record: dict,
    image: Path,
    source_pixels: tuple[int, int],
    metadata_path: Path,
    *,
    database: Path,
    pack_id: str,
) -> dict:
    original = destination / "original"
    original.mkdir()
    shutil.copy2(image, original / "artwork.png")
    shutil.copy2(metadata_path, original / "experiment.json")
    print_dir = destination / "print"
    print_dir.mkdir()
    print_path = print_dir / "artwork.png"
    shutil.copy2(image, print_path)
    measured = png_size(print_path)
    if measured != source_pixels:
        raise PrintError("The print file no longer matches the approved image.")
    sizes = print_sizes(*measured)
    recommended = largest_size(sizes, recommended=True)
    acceptable = largest_size(sizes, recommended=False)
    intended = recommended or acceptable
    if intended is not None:
        label_png_dpi(print_path, int(intended["ppi"]))
        if png_size(print_path) != measured:
            raise PrintError("The DPI label changed the pixel size.")
    jpeg_path = print_dir / "artwork.jpg"
    jpeg_error = _write_jpeg(print_path, jpeg_path)
    instagram = destination / "instagram"
    etsy = destination / "etsy"
    instagram.mkdir()
    etsy.mkdir()
    preview_size, preview_method = _write_preview(image, instagram / "preview.png", source_pixels)
    shutil.copy2(instagram / "preview.png", etsy / "preview.png")
    shutil.copy2(print_path, etsy / "print.png")
    if jpeg_path.is_file():
        shutil.copy2(jpeg_path, etsy / "print.jpg")
    rights = _rights(
        database,
        str(record.get("source_text") or record.get("body") or ""),
        poem_id=_optional_poem_id(record),
        visual_prompt=str(record.get("visual_prompt") or record.get("prompt") or ""),
    )
    caption = _instagram_caption(rights, record)
    spec_met = meets_print_specification(source_pixels, measured, "none")
    model_commercial = _model_commercial(record)
    model_ok = model_allows_publication(record.get("model_id") if isinstance(record.get("model_id"), str) else None)
    export_format = "png+jpeg" if jpeg_path.is_file() else "png"
    listing = _etsy_listing(
        rights,
        record,
        measured,
        sizes,
        intended,
        recommended,
        acceptable,
        export_format,
        print_path.stat().st_size,
        model_commercial=model_commercial,
        model_ok=model_ok,
    )
    (instagram / "caption.txt").write_text(caption, encoding="utf-8")
    (etsy / "listing.txt").write_text(listing, encoding="utf-8")
    ready = bool(rights["publication_ready"] and spec_met and model_ok)
    classification = classify_ppi(int(intended["ppi"])) if intended else classify_ppi(0)
    files = {
        "original": "original/artwork.png",
        "experiment": "original/experiment.json",
        "print": "print/artwork.png",
        "instagram_preview": "instagram/preview.png",
        "instagram_caption": "instagram/caption.txt",
        "etsy_print": "etsy/print.png",
        "etsy_preview": "etsy/preview.png",
        "etsy_listing": "etsy/listing.txt",
    }
    if jpeg_path.is_file():
        files["print_jpeg"] = "print/artwork.jpg"
        files["etsy_print_jpeg"] = "etsy/print.jpg"
    manifest = {
        "artwork_id": pack_id,
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "publication_ready": ready,
        "rights_cleared": bool(rights["publication_ready"]),
        "print_specification_met": spec_met,
        "published": False,
        "rights": rights,
        "poem_id": rights.get("poem_id") or _optional_poem_id(record),
        "title": rights.get("title") or record.get("title"),
        "author": rights.get("author") or record.get("author"),
        "source_text": record.get("source_text") or record.get("body") or "",
        "source_sha256": record.get("source_sha256"),
        "interpretation": record.get("interpretation") or "",
        "visual_prompt": record.get("visual_prompt") or record.get("prompt") or "",
        "prompt": record.get("prompt"),
        "prompt_sha256": record.get("prompt_sha256"),
        "abstraction": record.get("abstraction"),
        "house_style": record.get("house_style"),
        "style_preset": record.get("style_preset"),
        "style_prompt": record.get("style_prompt"),
        "style_composition": record.get("style_composition"),
        "method": record.get("method"),
        "model_id": record.get("model_id"),
        "model_name": record.get("model_name"),
        "model_path": record.get("model_path"),
        "model_checksum": record.get("model_checksum") or model_commercial.get("model_checksum"),
        "command_type": record.get("command_type"),
        "model_commercial_use": model_commercial["commercial_use"],
        "model_commercial_cleared": model_ok,
        "model_commercial_note": model_commercial["note"],
        "seed": record.get("seed"),
        "steps": record.get("steps"),
        "cfg_scale": record.get("cfg_scale"),
        "sampler": record.get("sampler"),
        "scheduler": record.get("scheduler"),
        "clip_skip": record.get("clip_skip"),
        "negative_prompt": record.get("negative_prompt"),
        "ollama": record.get("ollama"),
        "original_pixels": {"width": source_pixels[0], "height": source_pixels[1]},
        "original_pixel_dimensions": {"width": source_pixels[0], "height": source_pixels[1]},
        "export_pixel_dimensions": {"width": measured[0], "height": measured[1]},
        "print_pixels": {"width": measured[0], "height": measured[1]},
        "source_print_suitable": print_sized(*source_pixels),
        "export_class": "print" if spec_met else "experimental",
        "orientation": orientation(*measured),
        "intended_print_size": _size_record(intended),
        "largest_recommended_print_size": _size_record(recommended),
        "largest_acceptable_print_size": _size_record(acceptable),
        "effective_ppi": None if intended is None else intended["ppi"],
        "quality_classification": classification["classification"],
        "quality_classification_label": classification["classification_label"],
        "resizing_method": "none",
        "upscaling_method": None,
        "upscale_method": "none",
        "supported_print_sizes": sizes,
        "export_format": export_format,
        "file_size": print_path.stat().st_size,
        "jpeg_file_size": jpeg_path.stat().st_size if jpeg_path.is_file() else None,
        "jpeg_error": jpeg_error,
        "instagram_pixels": {"width": preview_size[0], "height": preview_size[1]},
        "instagram_resizing_method": preview_method,
        "dpi_label": None if intended is None else int(intended["ppi"]),
        "dpi_note": (
            "The DPI label matches the largest recommended size, or the largest acceptable "
            "size when nothing reaches 200 PPI. It does not add pixels or detail."
        ),
        "resolution_note": RESOLUTION_NOTE,
        "target_ppi": TARGET_PPI,
        "minimum_ppi": MINIMUM_PPI,
        "ai_disclosure": AI_DISCLOSURE,
        "digital_download": True,
        "files": files,
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    manifest["pack_path"] = str(destination)
    return manifest


def _size_record(size: dict[str, object] | None) -> dict[str, object] | None:
    if size is None:
        return None
    return {
        "width_inches": size["width_inches"],
        "height_inches": size["height_inches"],
        "ppi": size["ppi"],
        "classification": size["classification"],
        "classification_label": size["classification_label"],
    }


def _write_jpeg(source: Path, destination: Path) -> str | None:
    magick = shutil.which("magick")
    if magick is None:
        return "ImageMagick is not available, so no JPEG was written."
    completed = subprocess.run(
        [magick, str(source), "-quality", "95", "-sampling-factor", "4:4:4", str(destination)],
        shell=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0 or not destination.is_file():
        destination.unlink(missing_ok=True)
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        return detail[-500:] or "JPEG export failed."
    return None


def _write_preview(source: Path, destination: Path, pixels: tuple[int, int]) -> tuple[tuple[int, int], str]:
    target = preview_pixels(*pixels)
    if target == pixels:
        shutil.copy2(source, destination)
        return target, "none"
    magick = shutil.which("magick")
    if magick is None:
        shutil.copy2(source, destination)
        return pixels, "none"
    completed = subprocess.run(
        [magick, str(source), "-resize", f"{target[0]}x{target[1]}!", str(destination)],
        shell=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0 or not destination.is_file():
        shutil.copy2(source, destination)
        return pixels, "none"
    try:
        measured = png_size(destination)
    except (OSError, ValueError):
        shutil.copy2(source, destination)
        return pixels, "none"
    if measured != target:
        destination.unlink()
        shutil.copy2(source, destination)
        return pixels, "none"
    return target, "downscale"


def _optional_poem_id(record: dict) -> int | None:
    raw = record.get("poem_id")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.strip().isdigit():
        return int(raw.strip())
    return None


def _normalize_poem_text(text: str) -> str:
    """Normalize newlines only. Do not strip — archived poems may indent lines."""
    return text.replace("\r\n", "\n")


def _rights(
    database: Path,
    source_text: str,
    *,
    poem_id: int | None = None,
    visual_prompt: str = "",
) -> dict:
    matched = _matching_poem(
        database,
        source_text,
        poem_id=poem_id,
        visual_prompt=visual_prompt,
    )
    if matched is None:
        return {
            "matched": False,
            "publication_ready": False,
            "reason": "The source text is not an archived poem with documented commercial-use rights.",
        }
    ready = (
        matched["rights_verification_status"] == "documented"
        and matched["commercial_use_sweden"] == "yes"
    )
    reason = (
        "Documented Swedish commercial-use rights cover this poem."
        if ready
        else "The matching poem does not have documented commercial-use clearance."
    )
    return {
        "matched": True,
        "publication_ready": ready,
        "reason": reason,
        "poem_id": matched["id"],
        "title": matched["title"],
        "author": matched["author"],
        "rights_verification_status": matched["rights_verification_status"],
        "commercial_use_sweden": matched["commercial_use_sweden"],
    }


def _matching_poem(
    database: Path,
    source_text: str,
    *,
    poem_id: int | None = None,
    visual_prompt: str = "",
) -> sqlite3.Row | None:
    text = _normalize_poem_text(source_text)
    if text == "":
        return None
    # The visual prompt is the AI interpretation — never treat it as the poem.
    visual = _normalize_poem_text(visual_prompt)
    if visual and text == visual:
        return None
    if not database.is_file():
        return None
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        if poem_id is not None:
            row = connection.execute(
                """
                SELECT id, title, author, body, rights_verification_status, commercial_use_sweden
                FROM poems
                WHERE id = ?
                """,
                (poem_id,),
            ).fetchone()
            if row is None:
                return None
            archived = _normalize_poem_text(str(row["body"]))
            if text != archived:
                return None
            return row
        return connection.execute(
            """
            SELECT id, title, author, body, rights_verification_status, commercial_use_sweden
            FROM poems
            WHERE body = ?
            """,
            (text,),
        ).fetchone()


def _instagram_caption(rights: dict, record: dict) -> str:
    source = str(record.get("source_text") or "").strip()
    if rights.get("matched"):
        heading = f"{rights['title']}\n{rights['author']}"
        if source:
            heading = f"{heading}\n\n{source}"
    else:
        heading = "Source text not linked to a documented poem"
    model = record.get("model_name") or record.get("model_id") or "local image model"
    return (
        f"{heading}\n\n"
        f"Image model: {model}.\n"
        f"{AI_DISCLOSURE}\n"
    )


def _format_inches(size: dict[str, object]) -> str:
    width = size["width_inches"]
    height = size["height_inches"]
    if size["square"]:
        return f"{int(size['long_inches'])} × {int(size['long_inches'])} inches"
    return f"{width} × {height} inches"


def _model_commercial(record: dict) -> dict[str, object]:
    model_id = record.get("model_id") if isinstance(record.get("model_id"), str) else None
    status = record.get("commercial_use")
    checksum = record.get("model_checksum")
    if isinstance(status, str) and status:
        commercial_use = status
    elif model_id:
        try:
            profile = require_model(model_id)
        except ValueError:
            commercial_use = "allowed"
            checksum = checksum
        else:
            commercial_use = profile.commercial_use
            if checksum is None:
                checksum = profile.model_checksum
    else:
        commercial_use = "allowed"
    if commercial_use == COMMERCIAL_UNVERIFIED:
        note = "Commercial use: Unverified. Experimental and local preview use is allowed."
    else:
        note = "Commercial use: allowed for this installed model profile."
    return {
        "commercial_use": commercial_use,
        "model_checksum": checksum,
        "note": note,
    }


def _etsy_listing(
    rights: dict,
    record: dict,
    pixels: tuple[int, int],
    sizes: list[dict[str, object]],
    intended: dict[str, object] | None,
    recommended: dict[str, object] | None,
    acceptable: dict[str, object] | None,
    export_format: str,
    file_size: int,
    *,
    model_commercial: dict[str, object],
    model_ok: bool,
) -> str:
    if rights.get("matched"):
        title = f"{rights['title']} — digital art download"
        attribution = f"{rights['title']} by {rights['author']}"
    else:
        title = "Digital art download"
        attribution = "Source text not linked to a documented poem"
    width, height = pixels
    if recommended is not None:
        recommended_line = (
            f"Largest recommended print, at least {TARGET_PPI} PPI: {_format_inches(recommended)} "
            f"({recommended['ppi']} PPI, {recommended['classification_label']}).\n"
        )
    else:
        recommended_line = f"No supported size reaches {TARGET_PPI} PPI.\n"
    if acceptable is not None:
        acceptable_line = (
            f"Largest acceptable print, at least {MINIMUM_PPI} PPI: {_format_inches(acceptable)} "
            f"({acceptable['ppi']} PPI, {acceptable['classification_label']}).\n"
        )
    else:
        acceptable_line = f"No supported size reaches {MINIMUM_PPI} PPI.\n"
    if intended is None:
        intended_line = "There is no intended print size.\n"
    else:
        intended_line = (
            f"Intended print size: {_format_inches(intended)} "
            f"at {intended['ppi']} PPI ({intended['classification_label']}).\n"
        )
    size_lines = []
    for size in sizes:
        if size["suitable"]:
            state = str(size["classification_label"])
        else:
            state = "Unsuitable"
        size_lines.append(f"- {_format_inches(size)}: {size['ppi']} PPI, {state}.")
    tags = ["digital download", "art print", orientation(width, height)]
    preset = record.get("style_preset")
    if preset and preset != "none":
        tags.append(str(preset))
    if record.get("abstraction"):
        tags.append(str(record["abstraction"]))
    if rights.get("author"):
        tags.append(str(rights["author"]))
    commercial_line = str(model_commercial["note"]) + "\n"
    if not model_ok:
        commercial_line += (
            "This model is not publication-ready for Etsy until commercial-use "
            "permissions are documented.\n"
        )
    body = [
        f"Title: {title}",
        "",
        attribution,
        "",
        DIGITAL_NOTICE,
        "",
        f"Image dimensions: {width}×{height} pixels, {orientation(width, height)}.",
        f"Export format: {export_format}. File size: {file_size} bytes.",
        "The download uses the approved pixels. They were not resized or upscaled.",
        intended_line.rstrip("\n"),
        recommended_line.rstrip("\n"),
        acceptable_line.rstrip("\n"),
        "",
        "Supported sizes:",
        *size_lines,
        "",
        "Sizes marked Unsuitable are not offered at this resolution.",
        "The DPI label does not add image detail.",
        commercial_line.rstrip("\n"),
        "",
        f"Tags: {', '.join(tags)}",
        "",
        AI_DISCLOSURE,
        "",
    ]
    return "\n".join(body)

"""Etsy listing gallery: web preview first, then three quarter close-ups.

The downloadable print master is never used as a listing photo. Buyers who
right-click a gallery image must not receive the same pixels as the purchase.
"""

from __future__ import annotations

import secrets
import shutil
import subprocess
from pathlib import Path

from anskar.artwork.png import png_size
from anskar.etsy.errors import EtsyError

QUARTER_NAMES = ("tl", "tr", "bl", "br")
DETAIL_COUNT = 3
# Listing JPEG is a lossy web copy of the already-downscaled preview PNG.
LISTING_JPEG_QUALITY = "82"


def choose_quarters(*, rng=None) -> tuple[str, ...]:
    """Pick three of the four corner quarters in random order."""
    pool = list(QUARTER_NAMES)
    if rng is None:
        secrets.SystemRandom().shuffle(pool)
    elif hasattr(rng, "shuffle"):
        rng.shuffle(pool)
    else:
        # Pricing tests pass a randrange-only rng; Fisher–Yates still works.
        for index in range(len(pool) - 1, 0, -1):
            raw = int(rng.randrange(0, index + 1))
            swap = raw % (index + 1)
            pool[index], pool[swap] = pool[swap], pool[index]
    return tuple(pool[:DETAIL_COUNT])


def quarter_crop_box(width: int, height: int, name: str) -> tuple[int, int, int, int]:
    """Return ImageMagick crop geometry as (width, height, x, y) for a named quarter."""
    if width < 2 or height < 2:
        raise EtsyError("Artwork is too small for Etsy detail crops.", kind="media")
    half_w = width // 2
    half_h = height // 2
    if name == "tl":
        return half_w, half_h, 0, 0
    if name == "tr":
        return half_w, half_h, width - half_w, 0
    if name == "bl":
        return half_w, half_h, 0, height - half_h
    if name == "br":
        return half_w, half_h, width - half_w, height - half_h
    raise EtsyError(f"Unknown quarter name: {name}", kind="media")


def preview_source(folder: Path) -> Path:
    """Web preview PNG used for gallery crops. Never the print master."""
    path = folder / "etsy" / "preview.png"
    if not path.is_file():
        raise EtsyError("The Etsy preview PNG is missing.", kind="media")
    return path


def write_listing_preview_jpeg(preview_png: Path, destination: Path) -> Path | None:
    """Write a lossy JPEG beside the preview PNG for listing photos. Returns path or None."""
    magick = shutil.which("magick")
    if magick is None:
        return None
    destination.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [
            magick,
            str(preview_png),
            "-quality",
            LISTING_JPEG_QUALITY,
            "-sampling-factor",
            "4:2:0",
            str(destination),
        ],
        shell=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0 or not destination.is_file():
        destination.unlink(missing_ok=True)
        return None
    return destination


def write_detail_crops(
    source: Path,
    destination_dir: Path,
    *,
    quarters: tuple[str, ...] | None = None,
    rng=None,
) -> list[Path]:
    """
    Write three quarter close-ups from the web preview.

    Callers must pass the preview (or another web-sized copy), never the print PNG.
    """
    destination_dir.mkdir(parents=True, exist_ok=True)
    chosen = quarters if quarters is not None else choose_quarters(rng=rng)
    if len(chosen) != DETAIL_COUNT:
        raise EtsyError("Etsy gallery needs exactly three detail quarters.", kind="media")
    try:
        width, height = png_size(source)
    except (OSError, ValueError):
        width = height = 0
    magick = shutil.which("magick")
    written: list[Path] = []
    for index, name in enumerate(chosen, start=1):
        path = destination_dir / f"detail-{index}.png"
        cropped = False
        if width >= 2 and height >= 2 and magick is not None:
            crop_w, crop_h, x, y = quarter_crop_box(width, height, name)
            completed = subprocess.run(
                [
                    magick,
                    str(source),
                    "-crop",
                    f"{crop_w}x{crop_h}+{x}+{y}",
                    "+repage",
                    str(path),
                ],
                shell=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=60,
                check=False,
            )
            cropped = completed.returncode == 0 and path.is_file()
            if not cropped:
                path.unlink(missing_ok=True)
        if not cropped:
            # Fixtures or missing ImageMagick: keep publishing with the web preview frame.
            shutil.copy2(source, path)
        written.append(path)
    return written


def listing_gallery_images(folder: Path) -> list[Path]:
    """
    Ordered gallery paths for upload: web preview first, then detail-1..3.

    Prefer preview.jpg when present (lossy). Never include etsy/print.png —
    that file is the paid digital download only.
    """
    etsy = folder / "etsy"
    preview_jpg = etsy / "preview.jpg"
    preview_png = etsy / "preview.png"
    if preview_jpg.is_file():
        hero = preview_jpg
    elif preview_png.is_file():
        hero = preview_png
    else:
        raise EtsyError("The Etsy preview PNG is missing.", kind="media")
    if hero.resolve() == (etsy / "print.png").resolve():
        raise EtsyError("Etsy listing photos must not use the print master.", kind="media")
    images = [hero]
    for index in range(1, DETAIL_COUNT + 1):
        detail = etsy / f"detail-{index}.png"
        if detail.is_file():
            if detail.resolve() == (etsy / "print.png").resolve():
                continue
            images.append(detail)
    return images


def ensure_gallery_details(folder: Path, *, rng=None) -> list[Path]:
    """
    Ensure listing photos are built from the web preview, not the print master.

    Rewrites detail crops when missing or when any detail is larger than the
    preview (leftover from older packs that cropped the print).
    """
    etsy = folder / "etsy"
    preview = preview_source(folder)
    write_listing_preview_jpeg(preview, etsy / "preview.jpg")
    try:
        preview_w, preview_h = png_size(preview)
    except (OSError, ValueError):
        preview_w = preview_h = 0
    existing = [etsy / f"detail-{i}.png" for i in range(1, DETAIL_COUNT + 1)]
    need_rewrite = not all(path.is_file() for path in existing)
    if not need_rewrite and preview_w > 0 and preview_h > 0:
        for path in existing:
            try:
                width, height = png_size(path)
            except (OSError, ValueError):
                need_rewrite = True
                break
            if width > preview_w or height > preview_h:
                need_rewrite = True
                break
    if need_rewrite:
        write_detail_crops(preview, etsy, rng=rng)
    return listing_gallery_images(folder)

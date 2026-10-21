"""Etsy listing gallery: full artwork first, then three random quarter close-ups."""

from __future__ import annotations

import secrets
import shutil
import subprocess
from pathlib import Path

from anskar.artwork.png import png_size
from anskar.etsy.errors import EtsyError

QUARTER_NAMES = ("tl", "tr", "bl", "br")
DETAIL_COUNT = 3


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


def write_detail_crops(
    source: Path,
    destination_dir: Path,
    *,
    quarters: tuple[str, ...] | None = None,
    rng=None,
) -> list[Path]:
    """
    Write three quarter close-ups next to the Etsy preview.

    The downloadable print file is never cropped. These images are gallery-only.
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
            # Fixtures or missing ImageMagick: keep publishing with the full frame.
            shutil.copy2(source, path)
        written.append(path)
    return written


def listing_gallery_images(folder: Path) -> list[Path]:
    """
    Ordered gallery paths for upload: full square artwork first, then detail-1..3.

    Prefer the print master for the hero image so buyers see full resolution.
    Missing details are skipped so older packs still publish with the full image alone.
    """
    etsy = folder / "etsy"
    print_png = etsy / "print.png"
    preview = etsy / "preview.png"
    if print_png.is_file():
        hero = print_png
    elif preview.is_file():
        hero = preview
    else:
        raise EtsyError("The Etsy preview PNG is missing.", kind="media")
    images = [hero]
    for index in range(1, DETAIL_COUNT + 1):
        detail = etsy / f"detail-{index}.png"
        if detail.is_file():
            images.append(detail)
    return images


def ensure_gallery_details(folder: Path, *, rng=None) -> list[Path]:
    """Create missing detail crops from the print master when possible."""
    etsy = folder / "etsy"
    print_png = etsy / "print.png"
    preview = etsy / "preview.png"
    source = print_png if print_png.is_file() else preview
    if not source.is_file():
        raise EtsyError("No Etsy print or preview image to crop.", kind="media")
    existing = [etsy / f"detail-{i}.png" for i in range(1, DETAIL_COUNT + 1)]
    if all(path.is_file() for path in existing):
        return listing_gallery_images(folder)
    write_detail_crops(source, etsy, rng=rng)
    return listing_gallery_images(folder)

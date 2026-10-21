"""Etsy listing attributes for digital art prints (taxonomy 2078)."""

from __future__ import annotations

from anskar.etsy.client import EtsyClient

# Seller-taxonomy property ids for Art & Collectibles → Prints → Digital Prints (2078).
PROP_WIDTH = 47626759898
PROP_HEIGHT = 47626759834
PROP_FRAMING = 145330288558
PROP_ORIENTATION = 406291158455
PROP_PIECES = 570246213621
PROP_ASPECT = 570246213622
PROP_ART_SUBJECT = 400394338806

SCALE_INCHES = 5
VALUE_UNFRAMED = 2342
VALUE_SQUARE = 371
VALUE_ONE_PIECE = 5175
VALUE_RATIO_1_1 = 5178
VALUE_ABSTRACT = 2817

DEFAULT_MATERIALS = ["digital download"]


def print_inches_from_manifest(manifest: dict) -> float:
    intended = (
        manifest.get("intended_print_size")
        if isinstance(manifest.get("intended_print_size"), dict)
        else {}
    )
    width = intended.get("width_inches")
    try:
        inches = float(width) if width is not None else 8.0
    except (TypeError, ValueError):
        inches = 8.0
    if inches <= 0:
        return 8.0
    return inches


def apply_listing_attributes(
    client: EtsyClient,
    listing_id: str,
    *,
    print_inches: float = 8.0,
    square: bool = True,
) -> None:
    """
    Set the attributes Etsy suggests for square digital art prints.

    Width/Height match the intended print size. Framing is Unframed because the
    buyer receives a download, not a physical frame. Skip Frame mat and similar
    physical-only suggestions.
    """
    size = f"{print_inches:g}"
    client.update_listing_property(
        listing_id,
        PROP_WIDTH,
        value_ids=[None],
        values=[size],
        scale_id=SCALE_INCHES,
    )
    client.update_listing_property(
        listing_id,
        PROP_HEIGHT,
        value_ids=[None],
        values=[size],
        scale_id=SCALE_INCHES,
    )
    client.update_listing_property(
        listing_id,
        PROP_FRAMING,
        value_ids=[VALUE_UNFRAMED],
        values=["Unframed"],
    )
    client.update_listing_property(
        listing_id,
        PROP_PIECES,
        value_ids=[VALUE_ONE_PIECE],
        values=["1"],
    )
    if square:
        client.update_listing_property(
            listing_id,
            PROP_ORIENTATION,
            value_ids=[VALUE_SQUARE],
            values=["Square"],
        )
        client.update_listing_property(
            listing_id,
            PROP_ASPECT,
            value_ids=[VALUE_RATIO_1_1],
            values=["1:1"],
        )
    client.update_listing_property(
        listing_id,
        PROP_ART_SUBJECT,
        value_ids=[VALUE_ABSTRACT],
        values=["Abstract & geometric"],
    )

"""Choose one eligible poem and keep a single pending selection."""

import random
import sqlite3
from collections.abc import Sequence

from anskar.archive.models import NoEligiblePoemError, NoPendingSelectionError, Poem, Selection
from anskar.archive.repository import get_pending, insert_pending, list_eligible, resolve_pending, utc_now


class Chooser:
    def choice(self, sequence: Sequence[Poem]) -> Poem:
        raise NotImplementedError


class RandomChooser:
    def __init__(self, generator: random.Random | None = None) -> None:
        self._generator = generator or random.SystemRandom()

    def choice(self, sequence: Sequence[Poem]) -> Poem:
        return self._generator.choice(sequence)


def select_poem(
    connection: sqlite3.Connection,
    *,
    chooser: Chooser | None = None,
    selected_at: str | None = None,
    exclude_poem_ids: set[int] | frozenset[int] | None = None,
) -> Selection:
    pending = get_pending(connection)
    if pending is not None:
        return pending
    eligible = list_eligible(connection)
    if exclude_poem_ids:
        eligible = [poem for poem in eligible if poem.id not in exclude_poem_ids]
    if not eligible:
        raise NoEligiblePoemError("No eligible unused poem is available.")
    poem = (chooser or RandomChooser()).choice(eligible)
    return insert_pending(connection, poem, selected_at=selected_at or utc_now())


def confirm_pending(connection: sqlite3.Connection, *, at: str | None = None) -> Selection:
    resolved = resolve_pending(connection, status="confirmed", at=at or utc_now())
    if resolved is None:
        raise NoPendingSelectionError("There is no pending poem selection.")
    return resolved


def skip_pending(connection: sqlite3.Connection, *, at: str | None = None) -> Selection:
    resolved = resolve_pending(connection, status="skipped", at=at or utc_now())
    if resolved is None:
        raise NoPendingSelectionError("There is no pending poem selection.")
    return resolved

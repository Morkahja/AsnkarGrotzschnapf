"""Records stored in the poetry archive."""

from dataclasses import dataclass


class ArchiveError(Exception):
    """A recoverable archive operation failed."""


class NoEligiblePoemError(ArchiveError):
    """No unused poem is available to select."""


class NoPendingSelectionError(ArchiveError):
    """Confirm or skip was requested when nothing is pending."""


@dataclass(frozen=True)
class PoemDraft:
    title: str
    author: str
    author_death_year: int | None
    author_death_year_source: str | None
    origin: str | None
    origin_source: str | None
    original_language: str
    text_language: str
    translator: str | None
    translator_death_year: int | None
    translator_death_year_source: str | None
    body: str
    source_url: str
    license_name: str
    license_info: str
    rights_verification_status: str


@dataclass(frozen=True)
class Poem:
    id: int
    collection_id: str
    title: str
    author: str
    author_death_year: int | None
    author_death_year_source: str | None
    author_death_year_verified: bool
    origin: str | None
    origin_source: str | None
    original_language: str
    text_language: str
    translator: str | None
    translator_death_year: int | None
    translator_death_year_source: str | None
    translator_death_year_verified: bool
    body: str
    source_url: str
    license_name: str
    license_info: str
    rights_verification_status: str
    commercial_use_sweden: str
    imported_at: str


@dataclass(frozen=True)
class Selection:
    id: int
    poem: Poem
    selected_at: str
    status: str
    confirmed_at: str | None
    skipped_at: str | None
    newly_pending: bool = False


@dataclass(frozen=True)
class Eligibility:
    commercial_use_sweden: str
    acceptable: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ImportReport:
    imported: int
    duplicates: int
    rejected: tuple[tuple[str, str], ...]

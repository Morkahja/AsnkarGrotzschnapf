"""Commercial-use checks under the Swedish copyright term."""

from datetime import date

from anskar.archive.models import Eligibility, PoemDraft

COPYRIGHT_TERM_YEARS = 70
BOILERPLATE_MARKERS = (
    "PROJECT GUTENBERG",
    "*** START OF",
    "*** END OF",
)


def swedish_death_year_cutoff(on: date) -> int:
    """Last death year whose economic rights have expired on `on`.

    Section 43 of the Swedish Copyright Act (SFS 1960:729) protects a work
    through the end of the seventieth year after the year of death.
    """
    return on.year - (COPYRIGHT_TERM_YEARS + 1)


def assess(draft: PoemDraft, *, on: date) -> Eligibility:
    reasons: list[str] = []
    cutoff = swedish_death_year_cutoff(on)
    author_expired = _person_expired(
        draft.author_death_year,
        draft.author_death_year_source,
        cutoff,
        "author",
        reasons,
    )
    translator = _blank_to_none(draft.translator)
    translator_expired = True
    if translator:
        translator_expired = _person_expired(
            draft.translator_death_year,
            draft.translator_death_year_source,
            cutoff,
            "translator",
            reasons,
        )
    elif draft.original_language.strip() and draft.text_language.strip():
        if draft.original_language.strip() != draft.text_language.strip():
            reasons.append("text language does not match the original language")

    if draft.rights_verification_status == "verified":
        reasons.append("rights status verified cannot be imported")
    elif draft.rights_verification_status != "documented":
        reasons.append("rights status must be documented")

    if not draft.title.strip():
        reasons.append("title is missing")
    if not draft.author.strip():
        reasons.append("author is missing")
    if not draft.body.strip():
        reasons.append("poem text is missing")
    if any(marker in draft.body for marker in BOILERPLATE_MARKERS):
        reasons.append("poem text contains ebook boilerplate")
    if not draft.source_url.strip():
        reasons.append("source URL is missing")
    if not draft.license_name.strip() or not draft.license_info.strip():
        reasons.append("license information is missing")
    if not draft.original_language.strip() or not draft.text_language.strip():
        reasons.append("language is missing")
    if _blank_to_none(draft.origin) and not _blank_to_none(draft.origin_source):
        reasons.append("origin has no source")

    if reasons:
        commercial = "no" if author_expired is False or translator_expired is False else "unknown"
        return Eligibility(commercial, False, tuple(reasons))
    return Eligibility("yes", True, ())


def _person_expired(
    death_year: int | None,
    source: str | None,
    cutoff: int,
    role: str,
    reasons: list[str],
) -> bool | None:
    """Return True when the term has ended, False when it has not, None when unknown."""
    if death_year is None:
        reasons.append(f"{role} death year is missing")
        return None
    if isinstance(death_year, bool) or not isinstance(death_year, int):
        reasons.append(f"{role} death year is not an integer year")
        return None
    if death_year < 1 or death_year > 9999:
        reasons.append(f"{role} death year is not a usable year")
        return None
    if not _blank_to_none(source):
        reasons.append(f"{role} death year has no source")
        return None
    if death_year > cutoff:
        reasons.append(f"{role} death year {death_year} is after the Swedish cutoff {cutoff}")
        return False
    return True


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None

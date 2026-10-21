import unittest
from datetime import date

from anskar.archive.eligibility import assess, swedish_death_year_cutoff
from anskar.archive.models import PoemDraft

ON = date(2026, 10, 20)


def draft(**overrides) -> PoemDraft:
    values = dict(
        title="A Poem",
        author="Ada Poet",
        author_death_year=1827,
        author_death_year_source="https://example.test/author",
        origin="English",
        origin_source="https://example.test/origin",
        original_language="en",
        text_language="en",
        translator=None,
        translator_death_year=None,
        translator_death_year_source=None,
        body="Line one\nLine two",
        source_url="https://example.test/poem",
        license_name="Swedish economic rights expired",
        license_info="Documented from the statute and the cited death year.",
        rights_verification_status="documented",
    )
    values.update(overrides)
    return PoemDraft(**values)


class EligibilityTests(unittest.TestCase):
    def test_cutoff_in_2026_is_1955(self):
        self.assertEqual(swedish_death_year_cutoff(ON), 1955)

    def test_documented_original_is_acceptable(self):
        decision = assess(draft(), on=ON)
        self.assertTrue(decision.acceptable)
        self.assertEqual(decision.commercial_use_sweden, "yes")
        self.assertEqual(decision.reasons, ())

    def test_death_year_1955_is_inside_the_term_end(self):
        decision = assess(draft(author_death_year=1955), on=ON)
        self.assertTrue(decision.acceptable)

    def test_death_year_1956_is_still_protected(self):
        decision = assess(draft(author_death_year=1956), on=ON)
        self.assertFalse(decision.acceptable)
        self.assertEqual(decision.commercial_use_sweden, "no")

    def test_missing_death_year_is_unknown(self):
        decision = assess(draft(author_death_year=None, author_death_year_source=None), on=ON)
        self.assertFalse(decision.acceptable)
        self.assertEqual(decision.commercial_use_sweden, "unknown")

    def test_unsourced_death_year_is_rejected(self):
        decision = assess(draft(author_death_year_source=None), on=ON)
        self.assertFalse(decision.acceptable)
        self.assertIn("author death year has no source", decision.reasons)

    def test_verified_status_is_refused(self):
        decision = assess(draft(rights_verification_status="verified"), on=ON)
        self.assertFalse(decision.acceptable)

    def test_translation_without_translator_death_year_is_rejected(self):
        decision = assess(draft(translator="A Translator", text_language="sv"), on=ON)
        self.assertFalse(decision.acceptable)

    def test_translation_with_sourced_expired_rights_is_acceptable(self):
        decision = assess(
            draft(
                translator="Old Translator",
                translator_death_year=1800,
                translator_death_year_source="https://example.test/translator",
                text_language="sv",
            ),
            on=ON,
        )
        self.assertTrue(decision.acceptable)

    def test_language_mismatch_without_a_translator_is_rejected(self):
        decision = assess(draft(text_language="sv"), on=ON)
        self.assertFalse(decision.acceptable)

    def test_ebook_boilerplate_is_rejected(self):
        decision = assess(draft(body="Line\n*** START OF THE PROJECT GUTENBERG EBOOK ***"), on=ON)
        self.assertFalse(decision.acceptable)

    def test_origin_without_a_source_is_rejected(self):
        decision = assess(draft(origin_source=None), on=ON)
        self.assertFalse(decision.acceptable)


if __name__ == "__main__":
    unittest.main()

import json
import os
import tempfile
import unittest
from datetime import date
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from anskar.archive.importer import import_collection
from anskar.archive.models import NoEligiblePoemError, NoPendingSelectionError
from anskar.archive.selection import confirm_pending, select_poem, skip_pending
from anskar.cli import main
from anskar.db import connect

ON = date(2026, 10, 20)


def poem(title: str, url: str) -> dict:
    return {
        "title": title,
        "author": "William Blake",
        "author_death_year": 1827,
        "author_death_year_source": "https://www.gutenberg.org/cache/epub/1934/pg1934.rdf",
        "origin": None,
        "origin_source": None,
        "original_language": "en",
        "text_language": "en",
        "translator": None,
        "body": f"First line of {title}\nSecond line of {title}",
        "source_url": url,
        "license_name": "Swedish economic rights expired",
        "license_info": "Documented from SFS 1960:729 section 43 and the cited death year.",
        "rights_verification_status": "documented",
    }


class FirstPoem:
    def choice(self, sequence):
        return sequence[0]


class LastPoem:
    def choice(self, sequence):
        return sequence[-1]


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.database = root / "archive.sqlite"
        collection = root / "collection.json"
        collection.write_text(
            json.dumps(
                {
                    "collection_id": "selection-test",
                    "poems": [
                        poem("THE LAMB", "https://example.test/lamb"),
                        poem("THE TIGER", "https://example.test/tiger"),
                    ],
                }
            ),
            encoding="utf-8",
        )
        with connect(self.database) as connection:
            import_collection(connection, collection, on=ON)

    def tearDown(self):
        self.temporary.cleanup()

    def test_repeat_select_keeps_one_pending_poem(self):
        with connect(self.database) as connection:
            first = select_poem(connection, chooser=FirstPoem(), selected_at="2026-10-20T00:00:00+00:00")
            second = select_poem(connection, chooser=LastPoem(), selected_at="2026-10-20T01:00:00+00:00")
            pending_count = connection.execute(
                "SELECT COUNT(*) FROM selection_history WHERE status = 'pending'"
            ).fetchone()[0]
        self.assertTrue(first.newly_pending)
        self.assertFalse(second.newly_pending)
        self.assertEqual(first.poem.id, second.poem.id)
        self.assertEqual(first.poem.body, "First line of THE LAMB\nSecond line of THE LAMB")
        self.assertEqual(pending_count, 1)
        self.assertEqual(first.status, "pending")

    def test_confirm_marks_the_poem_used_and_skip_does_not(self):
        with connect(self.database) as connection:
            select_poem(connection, chooser=FirstPoem())
            confirmed = confirm_pending(connection, at="2026-10-20T02:00:00+00:00")
            remaining = [poem.title for poem in _eligible_titles(connection)]
        self.assertEqual(confirmed.status, "confirmed")
        self.assertNotIn("THE LAMB", remaining)
        self.assertIn("THE TIGER", remaining)

        with connect(self.database) as connection:
            select_poem(connection, chooser=FirstPoem())
            skipped = skip_pending(connection, at="2026-10-20T03:00:00+00:00")
            after_skip = [poem.title for poem in _eligible_titles(connection)]
        self.assertEqual(skipped.status, "skipped")
        self.assertEqual(skipped.poem.title, "THE TIGER")
        self.assertIn("THE TIGER", after_skip)

    def test_skip_allows_another_pending_selection(self):
        with connect(self.database) as connection:
            select_poem(connection, chooser=FirstPoem())
            skip_pending(connection)
            nxt = select_poem(connection, chooser=LastPoem())
            history = connection.execute(
                "SELECT status FROM selection_history ORDER BY id"
            ).fetchall()
        self.assertEqual(nxt.poem.title, "THE TIGER")
        self.assertEqual([row["status"] for row in history], ["skipped", "pending"])

    def test_confirm_and_skip_require_a_pending_poem(self):
        with connect(self.database) as connection:
            with self.assertRaises(NoPendingSelectionError):
                confirm_pending(connection)
            with self.assertRaises(NoPendingSelectionError):
                skip_pending(connection)

    def test_empty_archive_has_nothing_to_select(self):
        with tempfile.TemporaryDirectory() as temporary:
            with connect(Path(temporary) / "empty.sqlite") as connection:
                with self.assertRaises(NoEligiblePoemError):
                    select_poem(connection)

    def test_cli_returns_the_same_pending_poem(self):
        with connect(self.database) as connection:
            connection.execute("DELETE FROM poems WHERE title != 'THE LAMB'")
        previous = os.environ.get("ANSKAR_DB")
        os.environ["ANSKAR_DB"] = str(self.database)
        try:
            first = StringIO()
            with patch("sys.stdout", first):
                self.assertEqual(main(["select"]), 0)
            second = StringIO()
            with patch("sys.stdout", second):
                self.assertEqual(main(["select"]), 0)
            self.assertIn("Title: THE LAMB", first.getvalue())
            self.assertIn("First line of THE LAMB\nSecond line of THE LAMB", first.getvalue())
            self.assertIn("Pending selection. Not marked used.", first.getvalue())
            self.assertIn("Pending selection, unchanged. Not marked used.", second.getvalue())
            skipped = StringIO()
            with patch("sys.stdout", skipped):
                self.assertEqual(main(["select", "--skip"]), 0)
            self.assertIn("The poem is not marked used.", skipped.getvalue())
            confirmed = StringIO()
            with patch("sys.stdout", confirmed):
                self.assertEqual(main(["select"]), 0)
                self.assertEqual(main(["select", "--confirm"]), 0)
            self.assertIn("The poem is marked used.", confirmed.getvalue())
        finally:
            if previous is None:
                os.environ.pop("ANSKAR_DB", None)
            else:
                os.environ["ANSKAR_DB"] = previous


def _eligible_titles(connection):
    from anskar.archive.repository import list_eligible

    return list_eligible(connection)


if __name__ == "__main__":
    unittest.main()

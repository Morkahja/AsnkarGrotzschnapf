import tempfile
import unittest
from datetime import date
from pathlib import Path

from anskar.archive.importer import import_collection
from anskar.archive.repository import count_poems
from anskar.archive.selection import select_poem
from anskar.config import PROJECT_ROOT
from anskar.db import connect

COLLECTION = PROJECT_ROOT / "data" / "collections" / "gutenberg-public-domain.json"
ON = date(2026, 10, 20)


class CollectionTests(unittest.TestCase):
    def test_initial_collection_imports_and_selects_offline(self):
        self.assertTrue(COLLECTION.is_file(), "the Gutenberg collection has not been built")
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "archive.sqlite"
            with connect(database) as connection:
                report = import_collection(connection, COLLECTION, on=ON)
                self.assertEqual(report.rejected, ())
                self.assertEqual(report.imported, 8)
                self.assertEqual(count_poems(connection), 8)
                rows = connection.execute(
                    """
                    SELECT title, body, translator, author_death_year, rights_verification_status
                    FROM poems ORDER BY id
                    """
                ).fetchall()
                for row in rows:
                    self.assertIn("\n", row["body"])
                    self.assertNotIn("PROJECT GUTENBERG", row["body"])
                    self.assertIsNone(row["translator"])
                    self.assertLessEqual(row["author_death_year"], 1955)
                    self.assertEqual(row["rights_verification_status"], "documented")
                lamb = connection.execute("SELECT body FROM poems WHERE title = 'THE LAMB'").fetchone()
                self.assertEqual(lamb["body"].splitlines()[0], "Little lamb, who made thee?")
                self.assertTrue(lamb["body"].endswith("    Little lamb, God bless thee!"))
                raven = connection.execute("SELECT body FROM poems WHERE title = 'The Raven'").fetchone()
                self.assertTrue(raven["body"].startswith("  Once upon a midnight dreary, while I pondered, weak and weary,"))
                self.assertTrue(raven["body"].endswith("Shall be lifted—nevermore!"))
                selection = select_poem(connection, selected_at="2026-10-20T12:00:00+00:00")
                self.assertEqual(selection.status, "pending")
                self.assertIn("\n", selection.poem.body)


if __name__ == "__main__":
    unittest.main()

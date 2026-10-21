import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from anskar.archive.importer import import_collection
from anskar.archive.repository import count_poems
from anskar.db import connect

ON = date(2026, 10, 20)


def poem(**overrides) -> dict:
    values = {
        "title": "THE LAMB",
        "author": "William Blake",
        "author_death_year": 1827,
        "author_death_year_source": "https://www.gutenberg.org/cache/epub/1934/pg1934.rdf",
        "origin": "English",
        "origin_source": "https://www.gutenberg.org/cache/epub/1934/pg1934.rdf",
        "original_language": "en",
        "text_language": "en",
        "translator": None,
        "body": "Little lamb, who made thee?\n    Does thou know who made thee?",
        "source_url": "https://www.gutenberg.org/cache/epub/1934/pg1934.txt",
        "license_name": "Swedish economic rights expired",
        "license_info": "Documented from SFS 1960:729 section 43 and the cited death year.",
        "rights_verification_status": "documented",
    }
    values.update(overrides)
    return values


def write_collection(directory: Path, poems: list[dict]) -> Path:
    path = directory / "collection.json"
    path.write_text(
        json.dumps({"collection_id": "test-collection", "poems": poems}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


class ImporterTests(unittest.TestCase):
    def test_import_preserves_line_breaks_and_skips_duplicates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            collection = write_collection(root, [poem()])
            database = root / "archive.sqlite"
            with connect(database) as connection:
                report = import_collection(connection, collection, on=ON, imported_at="2026-10-20T00:00:00+00:00")
                stored = connection.execute("SELECT body, author_death_year_verified FROM poems").fetchone()
            self.assertEqual(report.imported, 1)
            self.assertEqual(stored["body"], poem()["body"])
            self.assertEqual(stored["author_death_year_verified"], 1)

            with connect(database) as connection:
                again = import_collection(connection, collection, on=ON)
                self.assertEqual(again.duplicates, 1)
                self.assertEqual(count_poems(connection), 1)

    def test_protected_and_translated_poems_are_not_stored(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            collection = write_collection(
                root,
                [
                    poem(title="Too Recent", author_death_year=1960),
                    poem(title="Translated", translator="Someone Living", source_url="https://example.test/tr"),
                    poem(),
                ],
            )
            with connect(root / "archive.sqlite") as connection:
                report = import_collection(connection, collection, on=ON)
                titles = [row["title"] for row in connection.execute("SELECT title FROM poems")]
            self.assertEqual(titles, ["THE LAMB"])
            self.assertEqual(report.imported, 1)
            self.assertEqual(len(report.rejected), 2)

    def test_rights_status_stays_documented(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            collection = write_collection(root, [poem(rights_verification_status="verified")])
            with connect(root / "archive.sqlite") as connection:
                report = import_collection(connection, collection, on=ON)
                self.assertEqual(count_poems(connection), 0)
            self.assertEqual(report.imported, 0)


if __name__ == "__main__":
    unittest.main()

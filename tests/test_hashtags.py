"""Instagram hashtag selection for captions."""

from __future__ import annotations

import unittest

from anskar.instagram.hashtags import (
    AI_HASHTAG,
    ARTIST_HASHTAG,
    SALE_HASHTAG,
    append_hashtags,
    build_hashtags,
    choose_hashtags,
    fallback_literary_hashtag,
    fallback_style_hashtag,
    hashtags_from_manifest,
    to_hashtag,
)
from anskar.instagram.posts import compose


class HashtagUnitTests(unittest.TestCase):
    def test_to_hashtag_joins_words(self):
        self.assertEqual(to_hashtag("eternal love"), "#EternalLove")
        self.assertEqual(to_hashtag("dark surrealism"), "#DarkSurrealism")

    def test_static_order_is_poem_style_ai_sale_artist(self):
        tags = build_hashtags(literary="#EternalLove", style="#SurrealArt")
        self.assertEqual(
            tags,
            [
                "#EternalLove",
                "#SurrealArt",
                AI_HASHTAG,
                SALE_HASHTAG,
                ARTIST_HASHTAG,
            ],
        )

    def test_fallback_style_uses_recorded_direction(self):
        self.assertEqual(
            fallback_style_hashtag(abstraction="surreal", style_preset="painterly"),
            "#PainterlySurrealArt",
        )
        self.assertEqual(
            fallback_style_hashtag(abstraction="experimental", style_preset="none"),
            "#ExperimentalArt",
        )

    def test_choose_uses_ollama_when_available(self):
        def transport(method, url, body, timeout):
            self.assertIn("/api/chat", url)
            return {
                "message": {
                    "content": '{"literary":"true minds","style":"molten surrealism"}'
                }
            }

        tags = choose_hashtags(
            title="CXVI",
            author="William Shakespeare",
            poem="Let me not to the marriage of true minds",
            abstraction="surreal",
            style_preset="painterly",
            transport=transport,
        )
        self.assertEqual(tags[0], "#TrueMinds")
        self.assertEqual(tags[1], "#MoltenSurrealism")
        self.assertEqual(tags[-1], ARTIST_HASHTAG)

    def test_choose_falls_back_when_ollama_fails(self):
        def transport(method, url, body, timeout):
            raise OSError("down")

        tags = choose_hashtags(
            title="THE SICK ROSE",
            author="William Blake",
            abstraction="surreal",
            style_preset="painterly",
            transport=transport,
        )
        self.assertEqual(tags[0], fallback_literary_hashtag(title="THE SICK ROSE"))
        self.assertEqual(tags[1], "#PainterlySurrealArt")
        self.assertIn(AI_HASHTAG, tags)
        self.assertIn(SALE_HASHTAG, tags)
        self.assertIn(ARTIST_HASHTAG, tags)

    def test_compose_appends_stored_hashtags(self):
        tags = [
            "#TrueMinds",
            "#SurrealArt",
            AI_HASHTAG,
            SALE_HASHTAG,
            ARTIST_HASHTAG,
        ]
        caption = compose(
            {
                "rights": {"matched": True, "title": "CXVI", "author": "William Shakespeare"},
                "source_text": "Let me not to the marriage of true minds",
                "publication_ready": True,
                "instagram_hashtags": tags,
            },
            etsy_url=None,
        )["caption"]
        self.assertIn("William Shakespeare", caption)
        self.assertTrue(caption.rstrip().endswith(" ".join(tags)))
        self.assertEqual(caption.count("#"), 5)

    def test_manifest_without_stored_tags_still_gets_five(self):
        tags = hashtags_from_manifest(
            {
                "rights": {"title": "THE LAMB", "author": "William Blake"},
                "abstraction": "surreal",
                "style_preset": "painterly",
            }
        )
        self.assertEqual(len(tags), 5)
        self.assertEqual(tags[-1], ARTIST_HASHTAG)

    def test_append_is_idempotent(self):
        tags = build_hashtags(literary="#Rose", style="#SurrealArt")
        once = append_hashtags("Hello", tags)
        twice = append_hashtags(once, tags)
        self.assertEqual(once, twice)


if __name__ == "__main__":
    unittest.main()

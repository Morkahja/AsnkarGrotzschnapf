"""Autonomous Etsy publishing tests. All Etsy writes are mocked."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from anskar.etsy.client import EtsyClient
from anskar.etsy.credentials import EtsyCredentials
from anskar.etsy.errors import EtsyError
from anskar.etsy.listing import listing_fields, poem_text_cleared_for_reproduction
from anskar.etsy.maintain import maintain_etsy, offerings_status
from anskar.etsy.oauth import DEFAULT_SCOPES, EXPECTED_SHOP_ID, EXPECTED_SHOP_NAME
from anskar.etsy.gallery import choose_quarters, quarter_crop_box
from anskar.etsy.pricing import REQUIRED_CURRENCY, VAT_INCLUSIVE_NOTE, calculate_price
from anskar.etsy.service import publish_pack, published_listing
from anskar.etsy.state import PENDING_VERIFICATION, READY, set_etsy_state
from anskar.etsy.store import completed_sales_count, connect, find_by_artwork


class FakeEtsy:
    def __init__(self) -> None:
        self.listings: dict[str, dict] = {}
        self.images: dict[str, list] = {}
        self.files: dict[str, list] = {}
        self.properties: dict[str, list] = {}
        self.deleted: list[str] = []
        self.price_updates: list[tuple[str, float]] = []
        self.next_id = 5000
        self.currency = "SEK"
        self.transactions: list[dict] = []
        self.fail_create = False
        self.fail_after_draft = False

    def create_draft_listing(self, fields):
        if self.fail_create:
            raise EtsyError("create failed", kind="api")
        assert fields["quantity"] == 1
        assert fields["should_auto_renew"] is False
        assert fields["type"] == "download"
        assert isinstance(fields["price"], float)
        listing_id = str(self.next_id)
        self.next_id += 1
        self.listings[listing_id] = {
            "listing_id": int(listing_id),
            "state": "draft",
            "quantity": 1,
            "price": fields["price"],
            "title": fields["title"],
        }
        self.images[listing_id] = []
        self.files[listing_id] = []
        return self.listings[listing_id]

    def upload_listing_image(self, listing_id, path, *, rank=None):
        if self.fail_after_draft:
            raise EtsyError("timeout after draft", kind="timeout")
        assert path.suffix.lower() == ".png"
        image_id = len(self.images[str(listing_id)]) + 1
        self.images[str(listing_id)].append(
            {
                "listing_image_id": image_id,
                "image_id": image_id,
                "path": str(path),
                "name": path.name,
                "rank": int(rank) if rank is not None else image_id,
            }
        )
        return {"listing_id": int(listing_id)}

    def delete_listing_image(self, listing_id, listing_image_id):
        kept = [
            image
            for image in self.images.get(str(listing_id), [])
            if str(image.get("listing_image_id")) != str(listing_image_id)
        ]
        if not kept and self.images.get(str(listing_id)):
            raise EtsyError(
                "Listings must have at least 1 image. Please add another ListingImage before trying to delete.",
                kind="api",
            )
        self.images[str(listing_id)] = kept
        return {}

    def update_listing(self, listing_id, fields):
        row = self.listings[str(listing_id)]
        row.update(fields)
        return row

    def update_listing_property(self, listing_id, property_id, *, value_ids, values, scale_id=None):
        self.properties.setdefault(str(listing_id), []).append(
            {
                "property_id": property_id,
                "value_ids": value_ids,
                "values": values,
                "scale_id": scale_id,
            }
        )
        return {"property_id": property_id, "values": values}

    def upload_listing_file(self, listing_id, path):
        assert path.name == "print.png"
        assert path.name != "preview.png"
        self.files[str(listing_id)].append({"listing_file_id": 1, "filename": path.name})
        return {"listing_id": int(listing_id)}

    def get_listing_images(self, listing_id):
        return {"results": self.images.get(str(listing_id), [])}

    def get_listing_files(self, listing_id):
        return {"results": self.files.get(str(listing_id), [])}

    def publish_listing(self, listing_id):
        row = self.listings[str(listing_id)]
        row["state"] = "active"
        return row

    def get_listing_inventory(self, listing_id):
        row = self.listings[str(listing_id)]
        return {
            "products": [
                {
                    "sku": "",
                    "property_values": [],
                    "offerings": [
                        {
                            "quantity": int(row.get("quantity") or 1),
                            "is_enabled": True,
                            "price": {"amount": int(float(row["price"]) * 100), "divisor": 100},
                        }
                    ],
                }
            ],
            "price_on_property": [],
            "quantity_on_property": [],
            "sku_on_property": [],
        }

    def update_listing_inventory(self, listing_id, body):
        products = body.get("products") or []
        if products and products[0].get("offerings"):
            price = float(products[0]["offerings"][0]["price"])
            self.price_updates.append((str(listing_id), price))
            self.listings[str(listing_id)]["price"] = price
        return body

    def update_listing_price(self, listing_id, price):
        inventory = self.get_listing_inventory(listing_id)
        products = []
        for product in inventory.get("products") or []:
            offerings = []
            for offering in product.get("offerings") or []:
                offerings.append(
                    {
                        "price": float(price),
                        "quantity": int(offering.get("quantity") or 1),
                        "is_enabled": True,
                    }
                )
            products.append(
                {
                    "sku": product.get("sku") or "",
                    "property_values": [],
                    "offerings": offerings,
                }
            )
        return self.update_listing_inventory(
            listing_id,
            {
                "products": products,
                "price_on_property": [],
                "quantity_on_property": [],
                "sku_on_property": [],
            },
        )

    def delete_listing(self, listing_id):
        self.deleted.append(str(listing_id))
        self.listings[str(listing_id)]["state"] = "removed"
        return {}

    def get_listing(self, listing_id):
        return dict(self.listings[str(listing_id)])

    def get_shop(self, shop_id=None):
        return {
            "shop_id": int(EXPECTED_SHOP_ID),
            "shop_name": EXPECTED_SHOP_NAME,
            "currency_code": self.currency,
        }

    def get_shop_transactions(self, *, limit=100, offset=0):
        return {"results": self.transactions}

    def get_shop_receipts(self, *, was_paid=True, limit=100, offset=0):
        return {"results": []}

    def get_shop_receipt_transactions(self, receipt_id):
        return {"results": []}


class EtsyPublishTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="anskar-etsy-pub-"))
        self.packs = self.root / "packs"
        self.packs.mkdir()
        self.db = self.root / "etsy.sqlite"
        self.state_path = self.root / "etsy-state.json"
        set_etsy_state(READY, path=self.state_path)
        self.env = patch.dict(
            os.environ,
            {
                "ANSKAR_ETSY_DB": str(self.db),
                "ANSKAR_ETSY_STATE": str(self.state_path),
            },
        )
        self.env.start()
        self.fake = FakeEtsy()
        self.creds = EtsyCredentials(
            api_key="key",
            shared_secret="secret",
            access_token="1.token",
            shop_id=EXPECTED_SHOP_ID,
            shop_name=EXPECTED_SHOP_NAME,
            scopes=DEFAULT_SCOPES,
            refresh_token="refresh",
            expires_at=(datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
            currency="SEK",
            quantity=1,
        )

    def tearDown(self) -> None:
        self.env.stop()

    def _pack(self, pack_id: str = "art-1", *, poem: bool = True) -> str:
        folder = self.packs / pack_id
        (folder / "etsy").mkdir(parents=True)
        (folder / "etsy" / "print.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        (folder / "etsy" / "preview.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"1" * 32)
        rights = {
            "matched": True,
            "publication_ready": True,
            "title": "THE LAMB",
            "author": "William Blake",
            "commercial_use_sweden": "yes" if poem else "no",
            "body": "Little Lamb who made thee\nDost thou know who made thee",
        }
        manifest = {
            "artwork_id": pack_id,
            "publication_ready": True,
            "digital_download": True,
            "rights_cleared": poem,
            "rights": rights,
            "source_text": rights["body"],
            "print_pixels": {"width": 1600, "height": 1600},
            "orientation": "square",
            "style_preset": "painterly",
            "abstraction": "surreal",
            "ai_disclosure": "This image was generated locally by software.",
            "visual_prompt": "a surreal lamb in painterly light",
        }
        (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return pack_id

    def test_pricing_formula_and_discount(self) -> None:
        price = calculate_price(sales_before=3, r=4)
        self.assertEqual(price.original_sek, 230 + 80 + 30)
        self.assertEqual(price.currency, REQUIRED_CURRENCY)
        self.assertEqual(price.as_api_price(), float(price.original_sek * 4 // 5))
        self.assertEqual(price.discounted_sek % 5, 0)
        self.assertEqual(price.as_api_price(discounted=True), float(price.discounted_sek * 4 // 5))

    def test_listing_copy_includes_ai_digital_and_cleared_poem(self) -> None:
        pack_id = self._pack()
        manifest = json.loads((self.packs / pack_id / "manifest.json").read_text(encoding="utf-8"))
        self.assertTrue(poem_text_cleared_for_reproduction(manifest))
        fields = listing_fields(manifest, price_sek=249)
        self.assertEqual(fields["quantity"], 1)
        self.assertFalse(fields["should_auto_renew"])
        self.assertEqual(fields["type"], "download")
        self.assertIn("digital download", fields["description"].lower())
        self.assertIn("AI", fields["description"])
        self.assertIn("Little Lamb", fields["description"])
        self.assertIn("single-sale", fields["description"].lower())
        self.assertIn(VAT_INCLUSIVE_NOTE, fields["description"])
        self.assertIn("including VAT", fields["description"])

    def test_gallery_picks_three_random_quarters(self) -> None:
        self.assertEqual(len(choose_quarters(rng=_FixedShuffle(("br", "tl", "tr", "bl")))), 3)
        self.assertEqual(quarter_crop_box(1600, 1600, "tl"), (800, 800, 0, 0))
        self.assertEqual(quarter_crop_box(1600, 1600, "br"), (800, 800, 800, 800))

    def test_end_to_end_simulated_listing(self) -> None:
        pack_id = self._pack("sim-1")
        class_rng = type("R", (), {"randrange": staticmethod(lambda a, b: 2)})()
        result = publish_pack(
            pack_id,
            packs=self.packs,
            database=self.db,
            credentials=self.creds,
            client=self.fake,  # type: ignore[arg-type]
            rng=class_rng,
        )
        self.assertTrue(result["published"])
        self.assertEqual(result["quantity"], 1)
        self.assertEqual(result["currency"], "SEK")
        self.assertEqual(result["price_sek"], 230 + 40 + 0)
        self.assertEqual(result["price_r"], 2)
        self.assertEqual(result["price_s"], 0)
        listing_id = str(result["listing_id"])
        self.assertEqual(self.fake.listings[listing_id]["state"], "active")
        self.assertEqual(self.fake.listings[listing_id]["price"], float((230 + 40) * 4 // 5))
        # Hero print plus three detail crops when ImageMagick can write them.
        self.assertGreaterEqual(len(self.fake.images[listing_id]), 1)
        self.assertEqual(self.fake.images[listing_id][0]["name"], "print.png")
        self.assertEqual(len(self.fake.files[listing_id]), 1)
        self.assertEqual(self.fake.files[listing_id][0]["filename"], "print.png")
        stored = published_listing("sim-1", database=self.db)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored["state"], "active")
        self.assertEqual(stored["original_price_sek"], 270.0)

    def test_duplicate_prevention_and_resume_after_timeout(self) -> None:
        pack_id = self._pack("dup-1")
        self.fake.fail_after_draft = True
        with self.assertRaises(EtsyError):
            publish_pack(
                pack_id,
                packs=self.packs,
                database=self.db,
                credentials=self.creds,
                client=self.fake,  # type: ignore[arg-type]
                rng=type("R", (), {"randrange": staticmethod(lambda a, b: 1)})(),
            )
        with connect(self.db) as connection:
            row = find_by_artwork(connection, "dup-1")
            self.assertEqual(row["state"], "draft_created")
            listing_id = row["listing_id"]
            original = row["original_price_sek"]
        self.fake.fail_after_draft = False
        # Resume must not create a second draft.
        before = self.fake.next_id
        result = publish_pack(
            pack_id,
            packs=self.packs,
            database=self.db,
            credentials=self.creds,
            client=self.fake,  # type: ignore[arg-type]
        )
        self.assertEqual(result["listing_id"], listing_id)
        self.assertEqual(self.fake.next_id, before)
        with connect(self.db) as connection:
            row = find_by_artwork(connection, "dup-1", states=("active",))
            self.assertEqual(row["original_price_sek"], original)
        with self.assertRaises(EtsyError) as ctx:
            publish_pack(
                pack_id,
                packs=self.packs,
                database=self.db,
                credentials=self.creds,
                client=self.fake,  # type: ignore[arg-type]
            )
        self.assertEqual(ctx.exception.kind, "duplicate")

    def test_seller_not_verified_blocks_live_publish(self) -> None:
        set_etsy_state(PENDING_VERIFICATION, path=self.state_path)
        pack_id = self._pack("pending-1")
        with self.assertRaises(EtsyError) as ctx:
            publish_pack(
                pack_id,
                packs=self.packs,
                database=self.db,
                credentials=self.creds,
                client=self.fake,  # type: ignore[arg-type]
            )
        self.assertIn("not ready", str(ctx.exception).lower())
        self.assertEqual(self.fake.listings, {})

    def test_wrong_currency_stops_activation(self) -> None:
        self.fake.currency = "USD"
        pack_id = self._pack("cur-1")
        with self.assertRaises(EtsyError) as ctx:
            publish_pack(
                pack_id,
                packs=self.packs,
                database=self.db,
                credentials=self.creds,
                client=self.fake,  # type: ignore[arg-type]
            )
        self.assertIn("SEK", str(ctx.exception))

    def test_sale_detection_dedup_and_counter(self) -> None:
        pack_id = self._pack("sale-1")
        result = publish_pack(
            pack_id,
            packs=self.packs,
            database=self.db,
            credentials=self.creds,
            client=self.fake,  # type: ignore[arg-type]
            rng=type("R", (), {"randrange": staticmethod(lambda a, b: 0)})(),
        )
        listing_id = str(result["listing_id"])
        self.fake.transactions = [
            {
                "transaction_id": 9001,
                "listing_id": int(listing_id),
                "receipt_id": 7001,
                "was_paid": True,
            }
        ]
        first = maintain_etsy(database=self.db, client=self.fake)  # type: ignore[arg-type]
        self.assertEqual(first["sales_recorded"], 1)
        second = maintain_etsy(database=self.db, client=self.fake)  # type: ignore[arg-type]
        self.assertEqual(second["sales_recorded"], 0)
        with connect(self.db) as connection:
            self.assertEqual(completed_sales_count(connection), 1)
            row = find_by_artwork(connection, "sale-1")
            self.assertEqual(row["state"], "sold")
        # Next artwork prices with S=1.
        pack2 = self._pack("sale-2")
        priced = publish_pack(
            pack2,
            packs=self.packs,
            database=self.db,
            credentials=self.creds,
            client=self.fake,  # type: ignore[arg-type]
            rng=type("R", (), {"randrange": staticmethod(lambda a, b: 0)})(),
        )
        self.assertEqual(priced["price_s"], 1)
        self.assertEqual(priced["price_sek"], 230 + 10)

    def test_thirty_day_discount_and_forty_two_day_expiry(self) -> None:
        pack_id = self._pack("life-1")
        activated = datetime(2026, 1, 1, tzinfo=timezone.utc)
        result = publish_pack(
            pack_id,
            packs=self.packs,
            database=self.db,
            credentials=self.creds,
            client=self.fake,  # type: ignore[arg-type]
            rng=type("R", (), {"randrange": staticmethod(lambda a, b: 5)})(),
        )
        listing_id = str(result["listing_id"])
        with connect(self.db) as connection:
            row = find_by_artwork(connection, "life-1", states=("active",))
            from anskar.etsy.store import update_listing

            update_listing(
                connection,
                int(row["id"]),
                activated_at=activated.isoformat(),
            )
        day30 = activated + timedelta(days=30, hours=1)
        mid = maintain_etsy(database=self.db, client=self.fake, now=day30)  # type: ignore[arg-type]
        self.assertEqual(mid["discounted"], 1)
        self.assertEqual(self.fake.price_updates[-1][0], listing_id)
        inclusive = 230 + 100
        discounted_inclusive = int(round(inclusive * 0.8))
        discounted_inclusive = int(round(discounted_inclusive / 5.0) * 5)
        expected_net = discounted_inclusive * 4 // 5
        self.assertEqual(self.fake.price_updates[-1][1], float(expected_net))
        # Original price stays recorded (Swedish inclusive target).
        with connect(self.db) as connection:
            row = find_by_artwork(connection, "life-1", states=("active",))
            self.assertEqual(row["original_price_sek"], float(inclusive))
            self.assertEqual(row["current_price_sek"], float(discounted_inclusive))
            self.assertTrue(row["discount_applied"])
        day42 = activated + timedelta(days=42, hours=1)
        end = maintain_etsy(database=self.db, client=self.fake, now=day42)  # type: ignore[arg-type]
        self.assertEqual(end["expired"], 1)
        self.assertIn(listing_id, self.fake.deleted)
        with connect(self.db) as connection:
            row = find_by_artwork(connection, "life-1", states=("expired",))
            self.assertEqual(row["state"], "expired")
        with self.assertRaises(EtsyError):
            publish_pack(
                pack_id,
                packs=self.packs,
                database=self.db,
                credentials=self.creds,
                client=self.fake,  # type: ignore[arg-type]
            )

    def test_sold_not_expired(self) -> None:
        pack_id = self._pack("sold-keep")
        activated = datetime(2026, 1, 1, tzinfo=timezone.utc)
        result = publish_pack(
            pack_id,
            packs=self.packs,
            database=self.db,
            credentials=self.creds,
            client=self.fake,  # type: ignore[arg-type]
            rng=type("R", (), {"randrange": staticmethod(lambda a, b: 0)})(),
        )
        listing_id = str(result["listing_id"])
        with connect(self.db) as connection:
            row = find_by_artwork(connection, "sold-keep", states=("active",))
            from anskar.etsy.store import update_listing

            update_listing(connection, int(row["id"]), activated_at=activated.isoformat())
        self.fake.listings[listing_id]["quantity"] = 0
        self.fake.listings[listing_id]["state"] = "sold_out"
        report = maintain_etsy(
            database=self.db,
            client=self.fake,  # type: ignore[arg-type]
            now=activated + timedelta(days=50),
        )
        self.assertEqual(report["expired"], 0)
        with connect(self.db) as connection:
            row = find_by_artwork(connection, "sold-keep")
            self.assertEqual(row["state"], "sold")
        self.assertNotIn(listing_id, self.fake.deleted)

    def test_offline_delay_reported(self) -> None:
        report = maintain_etsy(database=self.db, offline=True)
        self.assertTrue(report["offline_delay"])
        self.assertEqual(report["expired"], 0)

    def test_etsy_error_does_not_block_instagram_contract(self) -> None:
        # Daily pipeline already queues on EtsyError; assert publish failure stays local.
        set_etsy_state(READY, path=self.state_path)
        self.fake.fail_create = True
        pack_id = self._pack("ig-safe")
        with self.assertRaises(EtsyError):
            publish_pack(
                pack_id,
                packs=self.packs,
                database=self.db,
                credentials=self.creds,
                client=self.fake,  # type: ignore[arg-type]
            )
        # Instagram independence is covered in test_daily; here we only prove Etsy failed alone.
        self.assertEqual(self.fake.listings, {})

    def test_token_refresh_hook_used_before_publish(self) -> None:
        pack_id = self._pack("refresh-1")
        with patch("anskar.etsy.service.ensure_fresh_credentials", return_value=self.creds) as refreshed:
            publish_pack(
                pack_id,
                packs=self.packs,
                database=self.db,
                client=self.fake,  # type: ignore[arg-type]
                rng=type("R", (), {"randrange": staticmethod(lambda a, b: 0)})(),
            )
        refreshed.assert_called()

    def test_offerings_status_lists_prices(self) -> None:
        pack_id = self._pack("offer-1")
        publish_pack(
            pack_id,
            packs=self.packs,
            database=self.db,
            credentials=self.creds,
            client=self.fake,  # type: ignore[arg-type]
            rng=type("R", (), {"randrange": staticmethod(lambda a, b: 3)})(),
        )
        report = offerings_status(database=self.db)
        self.assertEqual(report["completed_sales_count"], 0)
        self.assertEqual(len(report["offerings"]), 1)
        self.assertEqual(report["offerings"][0]["price_r"], 3)


class _FixedShuffle:
    """Deterministic shuffle helper for gallery quarter selection tests."""

    def __init__(self, order: tuple[str, ...]):
        self.order = list(order)

    def shuffle(self, values: list) -> None:
        values[:] = list(self.order)


if __name__ == "__main__":
    unittest.main()

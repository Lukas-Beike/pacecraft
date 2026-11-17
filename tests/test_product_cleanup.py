"""Removing the products not used for a chosen number of days, as one run (#3444)."""

from __future__ import annotations

import threading
import unittest
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import Mock

from nutrition_service_support import build_nutrition_services
from support import temp_database

from backend.db import row_factory
from backend.db.repositories import FoodDatabaseEntryRepository
from backend.errors import AppError
from backend.history.service import ChangeHistoryService
from backend.history.undo_service import HistoryUndoService
from backend.nutrition.product_cleanup import ProductCleanupService

TODAY = date(2026, 11, 17)


class ProductCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        # The app's own row factory: rows are dicts (#3435 read them as sqlite3.Row).
        self.manager = temp_database(
            self, "cleanup.db", row_factory=row_factory, schema=True
        )
        self.diary, self.library = build_nutrition_services(
            self.manager,
            lambda: "2026-11-17T12:00:00+00:00",
            lambda: datetime(2026, 11, 17, 12, 0, tzinfo=UTC),
            food_database=Mock(),
        )
        self.cleanup = ProductCleanupService(
            self.manager, lambda: TODAY, lambda: "2026-11-17T12:00:00+00:00"
        )

    def product(self, name: str, **changes: Any) -> dict[str, Any]:
        return self.library.product_catalog.save_product(
            {"name": name, "basis_amount": 100, "basis_unit": "g", "kcal": 100,
             "carbs_g": 10, "protein_g": 5, "fat_g": 2, "source": "manual", **changes}
        )  # fmt: skip

    def food(self, food_id: str, barcode: str) -> str:
        with self.manager.unit_of_work() as db:
            FoodDatabaseEntryRepository().upsert(
                db,
                {"id": food_id, "name": f"Datenbank {barcode}", "barcode": barcode,
                 "basis_amount": 100, "basis_unit": "g",
                 "per_100": {"kcal": 50, "carbs_g": 11, "protein_g": 0.3, "fat_g": 0.1}},
                "2026-10-01",
            )  # fmt: skip
        return food_id

    def logged(self, product_id: str, day: str) -> None:
        self.diary.log_product(product_id, 100, "g", meal_date=day)

    def listed(self) -> dict[str, tuple[str | None, int]]:
        return {
            p["name"]: (p["last_used"], p["usage_count"])
            for p in self.library.product_catalog.list_all_products()
        }

    def build(self) -> None:
        old = self.product("Alt")  # last used 100 days ago
        self.logged(old["id"], "2026-08-09")
        edge = self.product("Grenze")  # exactly 90 days ago: not older
        self.logged(edge["id"], "2026-08-19")
        fresh = self.product("Frisch")
        self.logged(fresh["id"], "2026-11-07")
        self.product("Nie genutzt")
        favorite = self.product("Favorit")
        self.library.product_catalog.set_favorite(favorite["id"], True)
        saved = self.product("In Vorlage")
        self.library.save_template({
            "name": "Frühstück", "meal_type": "breakfast", "description": "Frühstück",
            "components": [{"kind": "local_product", "product_id": saved["id"],
                            "amount": 100, "unit": "g"}],
        })  # fmt: skip
        # An own product inherits the uses of the database food with its EAN.
        food_id = self.food("off:4000000000017", "4000000000017")
        self.logged(food_id, "2026-11-12")
        self.product("Eigenes mit EAN", barcode="4000000000017")
        self.food("off:4000000000024", "4000000000024")  # never used

    def test_the_rule_keeps_favorites_templates_recent_and_inherited_uses(self) -> None:
        self.build()
        listed = self.listed()
        self.assertEqual(("2026-11-12", 1), listed["Eigenes mit EAN"])
        self.assertEqual((None, 0), listed["Nie genutzt"])
        preview = self.cleanup.preview("90")
        self.assertEqual(
            ["Alt", "Datenbank 4000000000024", "Nie genutzt"],
            [p["name"] for p in preview["products"]],
        )
        self.assertEqual(3, preview["count"])
        # Newest use first, never used last.
        self.assertEqual(["Grenze", "Alt", "Datenbank 4000000000024", "Nie genutzt"],
                         [p["name"] for p in self.cleanup.preview(30)["products"]])  # fmt: skip
        self.assertEqual(
            ["Datenbank 4000000000024", "Nie genutzt"],
            [p["name"] for p in self.cleanup.preview(365)["products"]],
        )

    def test_a_run_needs_a_confirmed_request_and_a_known_threshold(self) -> None:
        self.build()
        for payload in ({"days": 90}, {"days": 90, "confirmed": "yes"},
                        {"days": 60, "confirmed": True}, None):  # fmt: skip
            with self.subTest(payload=payload), self.assertRaises(AppError):
                self.cleanup.run(payload)
        self.assertEqual(3, self.cleanup.preview(90)["count"])

    def test_one_undo_restores_the_whole_run(self) -> None:
        self.build()
        before = self.listed()
        result = self.cleanup.run({"days": 90, "confirmed": True, "ids": ["ignored"]})
        self.assertEqual(3, result["count"])
        self.assertEqual(
            sorted(set(before) - {"Alt", "Datenbank 4000000000024", "Nie genutzt"}),
            sorted(self.listed()),
        )
        self.assertEqual(0, self.cleanup.preview(90)["count"])
        undo = HistoryUndoService(
            self.manager,
            ChangeHistoryService(self.manager, None, record_services=self.records()),
            *(None,) * 6,
            record_services=self.records(),
            profile_mutation_coordinator=threading.RLock(),
        )
        with self.manager.unit_of_work() as db:
            undo.apply_many(db, [result["change"]])
        self.assertEqual(before, self.listed())
        # Undone once: the run is gone, a second undo finds nothing.
        with self.manager.reader() as db:
            self.assertEqual(
                (None, None), self.cleanup.current(db, result["change"]["entity_id"])
            )

    def test_nothing_to_remove_records_nothing(self) -> None:
        fresh = self.product("Frisch")
        self.logged(fresh["id"], "2026-11-16")
        self.assertEqual(
            {"ok": True, "count": 0, "change": None},
            self.cleanup.run({"days": 30, "confirmed": True}),
        )

    def records(self) -> dict[str, Any]:
        return {"nutrition_product_cleanup": self.cleanup}


if __name__ == "__main__":
    unittest.main()

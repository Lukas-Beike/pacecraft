"""Favorite products and adding a saved product to a logged meal."""

from __future__ import annotations

import sqlite3
import unittest
from datetime import UTC, datetime
from typing import Any
from unittest.mock import Mock

from nutrition_service_support import build_nutrition_services
from support import temp_database

from backend.db.repositories import FoodDatabaseEntryRepository, basis_product_ids
from backend.errors import AppError


class FavoritesAndMealAdditionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = temp_database(
            self, "favorites.db", row_factory=sqlite3.Row, schema=True
        )
        self.diary, self.library = build_nutrition_services(
            self.manager,
            lambda: "2026-11-04T12:00:00+00:00",
            lambda: datetime(2026, 11, 4, 14, 0, tzinfo=UTC),
            food_database=Mock(),
        )

    def product(self, name: str, **changes):
        return self.library.product_catalog.save_product(
            {
                "name": name,
                "basis_amount": 100,
                "basis_unit": "g",
                "kcal": 100,
                "carbs_g": 10,
                "protein_g": 5,
                "fat_g": 2,
                "source": "manual",
                **changes,
            }
        )

    def test_favorites_are_listed_first_and_survive_edits(self) -> None:
        skyr = self.product("Skyr")
        hafer = self.product("Haferflocken")
        self.assertEqual(skyr["favorite"], 0)
        self.assertEqual(
            [
                p["name"]
                for p in self.library.product_catalog.list_products(
                    favorites_first=True
                )
            ],
            ["Haferflocken", "Skyr"],
        )

        self.assertEqual(
            self.library.product_catalog.set_favorite(skyr["id"], True)["favorite"], 1
        )
        self.assertEqual(
            [
                p["name"]
                for p in self.library.product_catalog.list_products(
                    favorites_first=True
                )
            ],
            ["Skyr", "Haferflocken"],
        )

        self.library.product_catalog.update_product(skyr["id"], {"kcal": 120})
        self.library.product_catalog.save_product({**skyr, "kcal": 130})
        self.assertEqual(
            self.library.product_catalog.get_product(skyr["id"])["favorite"], 1
        )
        self.assertEqual(
            self.library.product_catalog.get_product(skyr["id"])["kcal"], 130
        )

        self.library.product_catalog.set_favorite(skyr["id"], False)
        self.assertEqual(
            self.library.product_catalog.get_product(skyr["id"])["favorite"], 0
        )
        self.assertEqual(
            self.library.product_catalog.get_product(hafer["id"])["favorite"], 0
        )

    def test_the_list_sorts_most_used_first_and_search_keeps_relevance_first(
        self,
    ) -> None:
        # #3435: ties by name; a search ranks the name match above more use.
        catalog = self.library.product_catalog
        products = {
            name: self.product(name)
            for name in ("Skyrella Riegel", "Skyr", "Banane", "Apfel", "Brot mit Skyr")
        }
        for name, times in (("Skyrella Riegel", 3), ("Brot mit Skyr", 2), ("Skyr", 1)):
            for _ in range(times):
                self.diary.log_product(products[name]["id"], 100, "g")
        self.assertEqual(
            [(p["name"], p["usage_count"]) for p in catalog.list_all_products()],
            [("Skyrella Riegel", 3), ("Brot mit Skyr", 2), ("Skyr", 1),
             ("Apfel", 0), ("Banane", 0)],
        )  # fmt: skip
        self.assertEqual(
            [p["name"] for p in catalog.list_all_products("skyr")],
            ["Skyr", "Skyrella Riegel", "Brot mit Skyr"],
        )

    def test_an_own_product_keeps_the_uses_of_the_food_of_its_ean(self) -> None:
        # #3435 review: the list hides the food behind the own product of its EAN.
        catalog = self.library.product_catalog
        food_id = self.store_food()
        for _ in range(3):
            self.diary.log_product(food_id, 100, "g")
        self.assertEqual(
            [(p["id"], p["usage_count"]) for p in catalog.list_all_products()],
            [(food_id, 3)],
        )
        own = self.product("Apfelmus", barcode="4000000000017")
        self.diary.log_product(own["id"], 100, "g")
        self.assertEqual(
            [(p["id"], p["usage_count"]) for p in catalog.list_all_products()],
            [(own["id"], 4)],
        )
        # The refresh-all run reads no counts.
        self.assertEqual(
            [p["usage_count"] for p in catalog.list_all_products(counted=False)], [0]
        )

    def test_every_logged_basis_shape_names_its_products(self) -> None:
        # #3435: single product, database meal, composite (also a logged template).
        product = {"kind": "local_product", "product": {"id": "own-1"}}
        cases: list[tuple[dict[str, Any], set[str]]] = [
            (product, {"own-1"}),
            ({"kind": "database", "ingredients": [{"id": "off:1"}, {"id": "bls:2"}]},
             {"off:1", "bls:2"}),
            ({"kind": "composite", "components": [
                {"nutrition_basis": product},
                {"nutrition_basis": {"kind": "database", "food": {"id": "off:1"}}},
                {"nutrition_basis": {"kind": "estimate"}},
            ]}, {"own-1", "off:1"}),
            ({"kind": "manual"}, set()),
            ({"kind": "database", "ingredients": "broken"}, set()),
        ]  # fmt: skip
        for basis, expected in cases:
            with self.subTest(kind=basis["kind"]):
                self.assertEqual(basis_product_ids(basis), expected)

    def test_favorite_needs_a_known_product_and_a_boolean(self) -> None:
        product = self.product("Skyr")
        with self.assertRaises(AppError) as unknown:
            self.library.product_catalog.set_favorite("missing", True)
        self.assertEqual(unknown.exception.status, 404)
        for value in (1, "true", None):
            with self.subTest(value=value), self.assertRaises(AppError) as invalid:
                self.library.product_catalog.set_favorite(product["id"], value)
            self.assertEqual(invalid.exception.status, 400)

    def store_food(self, **per_100):
        food = {
            "id": "off:4000000000017",
            "name": "Apfelmus",
            "brands": "Hersteller",
            "basis_amount": 100,
            "basis_unit": "g",
            "per_100": {"kcal": 50, "carbs_g": 11, "protein_g": 0.3, "fat_g": 0.1},
        }
        with self.manager.unit_of_work() as db:
            FoodDatabaseEntryRepository().upsert(
                db, {**food, "per_100": {**food["per_100"], **per_100}}, "2026-10-01"
            )
        return food["id"]

    def test_a_stored_database_food_is_a_favorite_that_re_checks_keep(self) -> None:
        catalog = self.library.product_catalog
        food_id = self.store_food()

        favorite = catalog.set_favorite(food_id, True)

        self.assertEqual(
            (favorite["favorite"], favorite["origin"]), (1, "open_food_facts")
        )
        self.store_food(kcal=55)
        self.assertEqual(
            [(p["id"], p["favorite"], p["kcal"]) for p in catalog.list_all_products()],
            [(food_id, 1, 55)],
        )
        self.assertEqual(catalog.set_favorite(food_id, False)["favorite"], 0)

    def test_a_stored_database_food_is_used_like_an_own_product(self) -> None:
        food_id = self.store_food()
        entry = self.diary.log_product(food_id, 200, "g")

        basis = entry["nutrition_basis"]
        self.assertEqual((entry["kcal"], entry["carbs_g"]), (100, 22))
        self.assertEqual(basis["kind"], "local_product")
        # The meal keeps the food's values and origin at that time.
        self.assertEqual(
            (basis["product"]["origin"], basis["product"]["kcal"]),
            ("open_food_facts", 50),
        )
        banana = self.product("Banane", kcal=90, carbs_g=20, protein_g=1, fat_g=0.3)
        updated = self.diary.add_product_to_meal(entry["id"], banana["id"], 100, "g")
        added = self.diary.add_product_to_meal(updated["id"], food_id, 100, "g")
        self.assertEqual(added["kcal"], 240)
        self.assertEqual(
            [c["name"] for c in added["nutrition_basis"]["components"]][1:],
            ["Banane", "Apfelmus"],
        )
        # A unit the food does not count in is refused.
        with self.assertRaises(AppError) as mismatch:
            self.diary.add_product_to_meal(entry["id"], food_id, 100, "ml")
        self.assertEqual(mismatch.exception.reason, "food_unit_mismatch")

    def test_adding_a_product_turns_a_single_product_meal_into_a_composite(
        self,
    ) -> None:
        skyr = self.product("Skyr")
        banana = self.product("Banane", kcal=90, carbs_g=20, protein_g=1, fat_g=0.3)
        entry = self.diary.log_product(skyr["id"], 200, "g")

        updated = self.diary.add_product_to_meal(entry["id"], banana["id"], 100, "g")

        self.assertEqual(updated["id"], entry["id"])
        self.assertEqual(updated["kcal"], 290)
        self.assertEqual(updated["carbs_g"], 40)
        basis = updated["nutrition_basis"]
        self.assertEqual(basis["kind"], "composite")
        self.assertEqual([c["name"] for c in basis["components"]][1], "Banane")
        self.assertEqual(len(basis["components"]), 2)
        self.assertIn("Banane", updated["description"])
        self.assertEqual(self.diary.get_meal(entry["id"])["kcal"], 290)

        again = self.diary.add_product_to_meal(entry["id"], banana["id"], 100, "g")
        self.assertEqual(len(again["nutrition_basis"]["components"]), 3)
        self.assertEqual(again["kcal"], 380)

    def test_an_estimated_meal_keeps_its_values_as_the_first_component(self) -> None:
        banana = self.product("Banane", kcal=90, carbs_g=20, protein_g=1, fat_g=0.3)
        entry = self.diary.log_meal(
            {
                "description": "Pasta",
                "kcal": 600,
                "carbs_g": 90,
                "protein_g": 20,
                "fat_g": 15,
                "source": "coach",
            }
        )
        updated = self.diary.add_product_to_meal(entry["id"], banana["id"], 50, "g")
        first = updated["nutrition_basis"]["components"][0]
        self.assertEqual(
            (first["kind"], first["name"], first["kcal"]), ("estimate", "Pasta", 600)
        )
        self.assertEqual(updated["kcal"], 645)
        self.assertEqual(updated["source"], "coach")

    def test_unknown_meal_archived_product_and_wrong_unit_change_nothing(self) -> None:
        skyr = self.product("Skyr")
        entry = self.diary.log_product(skyr["id"], 100, "g")
        before = self.diary.get_meal(entry["id"])
        with self.assertRaises(AppError) as missing:
            self.diary.add_product_to_meal("missing", skyr["id"], 100, "g")
        self.assertEqual(missing.exception.status, 404)
        with self.assertRaises(AppError):
            self.diary.add_product_to_meal(entry["id"], skyr["id"], 100, "ml")
        self.library.product_catalog.archive_product(skyr["id"])
        with self.assertRaises(AppError) as archived:
            self.diary.add_product_to_meal(entry["id"], skyr["id"], 100, "g")
        self.assertEqual(archived.exception.status, 409)
        self.assertEqual(self.diary.get_meal(entry["id"]), before)

    def test_wrapped_meal_keeps_sugar_and_salt_totals(self) -> None:
        skyr = self.product("Skyr", sugar_g=4, salt_g=0.1)
        banana = self.product("Banane", sugar_g=12, salt_g=0)
        entry = self.diary.log_product(skyr["id"], 250, "g")
        self.diary.add_product_to_meal(entry["id"], banana["id"], 100, "g")

        day = self.diary.sync_status.get_day_summary("2026-11-04")
        self.assertEqual(day["total_sugar_g"], 22)
        self.assertEqual(day["total_salt_g"], 0.2)

    def test_description_keeps_the_newest_product_within_the_limit(self) -> None:
        banana = self.product("Banane")
        entry = self.diary.log_meal(
            {"description": "x" * 500, "kcal": 100, "source": "manual"}
        )
        updated = self.diary.add_product_to_meal(entry["id"], banana["id"], 50, "g")
        self.assertEqual(len(updated["description"]), 500)
        self.assertTrue(updated["description"].endswith(" + Banane"))

    def test_default_listing_stays_most_recent_first(self) -> None:
        skyr = self.product("Skyr")
        self.product("Haferflocken")
        self.library.product_catalog.set_favorite(skyr["id"], True)
        self.assertEqual(
            [p["name"] for p in self.library.product_catalog.list_products()],
            ["Haferflocken", "Skyr"],
        )


if __name__ == "__main__":
    unittest.main()

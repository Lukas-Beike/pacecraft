"""Diary entries record change history, so a Coach turn can undo them.

Temporary database and synthetic meals only.
"""

from __future__ import annotations

import threading
import unittest
from datetime import UTC, datetime
from typing import Any

from nutrition_service_support import build_nutrition_services
from support import temp_database

from backend import change_history
from backend.coach.change_receipts import change_summary
from backend.errors import AppError
from backend.history.service import ChangeHistoryService
from backend.history.undo_service import HistoryUndoService

MEAL_DATE = "2026-09-24"
MEAL: dict[str, Any] = {
    "meal_date": MEAL_DATE,
    "meal_type": "lunch",
    "description": "Rice bowl",
    "kcal": 600,
    "carbs_g": 80,
}


class NutritionHistoryCase(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = temp_database(self, "nutrition_history.db", schema=True)
        self.diary, self.library = build_nutrition_services(
            self.manager,
            lambda: "2026-09-24T12:00:00+00:00",
            lambda: datetime(2026, 9, 24, 12, 0, tzinfo=UTC),
        )

    def collected(self, write: Any) -> tuple[Any, list[dict[str, str]]]:
        with change_history.collect_changes() as changes:
            result = write()
        return result, changes


class NutritionHistoryTests(NutritionHistoryCase):
    def usage(self) -> dict[str, int]:
        return {
            product["name"]: product["usage_count"]
            for product in self.library.product_catalog.list_all_products()
        }

    def test_product_usage_follows_the_meal_log_through_delete_and_undo(self) -> None:
        # #3435: counted from the logged meals, a composite meal counts each product once.
        catalog = self.library.product_catalog
        base = {"basis_amount": 100, "basis_unit": "g", "kcal": 100, "carbs_g": 10,
                "protein_g": 5, "fat_g": 2, "source": "manual"}  # fmt: skip
        skyr = catalog.save_product({"name": "Skyr", **base})
        banana = catalog.save_product({"name": "Banane", **base})
        catalog.save_product({"name": "Kokosmilch", **base})
        first = self.diary.log_product(skyr["id"], 200, "g")
        self.diary.log_product(skyr["id"], 150, "g")
        self.assertEqual({"Skyr": 2, "Banane": 0, "Kokosmilch": 0}, self.usage())
        # An edit that adds a product counts it; the same product twice stays one use.
        self.diary.add_product_to_meal(first["id"], banana["id"], 100, "g")
        self.diary.add_product_to_meal(first["id"], banana["id"], 50, "g")
        self.assertEqual({"Skyr": 2, "Banane": 1, "Kokosmilch": 0}, self.usage())
        _, changes = self.collected(lambda: self.diary.delete_meal(first["id"]))
        self.assertEqual({"Skyr": 1, "Banane": 0, "Kokosmilch": 0}, self.usage())
        self.undo(changes)
        self.assertEqual({"Skyr": 2, "Banane": 1, "Kokosmilch": 0}, self.usage())
        # Most used first, then by name.
        self.assertEqual(["Skyr", "Banane", "Kokosmilch"], list(self.usage()))

    def undo(self, changes: list[dict[str, str]]) -> None:
        unused: Any = None
        undo = HistoryUndoService(
            self.manager,
            ChangeHistoryService(
                self.manager, unused, record_services={"nutrition_entry": self.diary}
            ),
            *(unused,) * 6,
            record_services={"nutrition_entry": self.diary},
            profile_mutation_coordinator=threading.RLock(),
        )
        with self.manager.unit_of_work() as db:
            undo.apply_many(db, changes)

    def test_undo_of_a_logged_meal_removes_it(self) -> None:
        entry, changes = self.collected(lambda: self.diary.log_meal(MEAL))
        self.assertEqual([change["action"] for change in changes], ["create"])
        self.undo(changes)
        with self.assertRaises(AppError):
            self.diary.get_meal(entry["id"])

    def test_undo_of_a_correction_restores_the_previous_values(self) -> None:
        entry = self.diary.log_meal(MEAL)
        _, changes = self.collected(
            lambda: self.diary.correct_meal(entry["id"], {"kcal": 450})
        )
        self.undo(changes)
        restored = self.diary.get_meal(entry["id"])
        self.assertEqual(restored["kcal"], 600)
        self.assertEqual(restored["carbs_g"], 80)

    def test_undo_of_an_added_product_restores_the_previous_meal(self) -> None:
        library = self.library
        banana = library.product_catalog.save_product(
            {
                "name": "Banane",
                "basis_amount": 100,
                "basis_unit": "g",
                "kcal": 90,
                "carbs_g": 20,
                "protein_g": 1,
                "fat_g": 0.3,
                "source": "manual",
            }
        )
        entry = self.diary.log_meal(MEAL)

        updated, changes = self.collected(
            lambda: self.diary.add_product_to_meal(entry["id"], banana["id"], 100, "g")
        )

        self.assertEqual([change["action"] for change in changes], ["update"])
        self.assertEqual(updated["kcal"], 690)

        self.undo(changes)

        restored = self.diary.get_meal(entry["id"])
        self.assertEqual(restored["description"], "Rice bowl")
        self.assertEqual(restored["kcal"], 600)
        self.assertEqual(restored["nutrition_basis"], entry["nutrition_basis"])

    def test_undo_of_a_delete_brings_the_entry_back_and_a_later_log_undoes(
        self,
    ) -> None:
        entry = self.diary.log_meal(MEAL)
        _, changes = self.collected(lambda: self.diary.delete_meal(entry["id"]))
        self.undo(changes)
        restored = self.diary.get_meal(entry["id"])
        self.assertEqual(restored["description"], "Rice bowl")
        self.assertEqual(restored["nutrition_basis"], entry["nutrition_basis"])
        _, created = self.collected(lambda: self.diary.log_meal(MEAL))
        self.undo(created)
        self.assertEqual(
            self.diary.sync_status.get_day_summary(MEAL_DATE)["entry_count"], 1
        )

    def test_undo_is_refused_after_a_later_edit(self) -> None:
        entry, changes = self.collected(lambda: self.diary.log_meal(MEAL))
        self.diary.correct_meal(entry["id"], {"kcal": 500})
        with self.assertRaises(AppError) as raised:
            self.undo(changes)
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(self.diary.get_meal(entry["id"])["kcal"], 500)

    def test_a_coach_turn_that_logs_meals_is_undoable(self) -> None:
        for tool in (
            "save_nutrition_entry",
            "update_nutrition_entry",
            "delete_nutrition_entry",
            "log_nutrition_template",
        ):
            summary = change_summary(
                [
                    {
                        "tool": tool,
                        "result": {"ok": True},
                        "changes": [{"id": "x", "action": "create"}],
                    }
                ]
            )
            assert summary is not None
            self.assertTrue(summary["undoable"], tool)


TEMPLATE: dict[str, Any] = {
    "name": "Porridge",
    "description": "Oats with milk",
    "meal_type": "breakfast",
    "kcal": 400,
    "carbs_g": 60,
    "protein_g": 15,
    "fat_g": 8,
}


class NutritionTemplateHistoryTests(NutritionHistoryCase):
    """Saved meals record change history, so a Coach turn can undo them."""

    def undo(self, changes: list[dict[str, str]]) -> None:
        unused: Any = None
        services = {"nutrition_template": self.library}
        undo = HistoryUndoService(
            self.manager,
            ChangeHistoryService(self.manager, unused, record_services=services),
            *(unused,) * 6,
            record_services=services,
            profile_mutation_coordinator=threading.RLock(),
        )
        with self.manager.unit_of_work() as db:
            undo.apply_many(db, changes)

    def history_rows(self) -> list[tuple[str, str]]:
        with self.manager.reader() as db:
            return [
                (row["entity_type"], row["action"])
                for row in db.execute(
                    "SELECT entity_type, action FROM change_history ORDER BY created_at, rowid"
                ).fetchall()
            ]

    def test_create_update_and_delete_each_record_one_change(self) -> None:
        saved = self.library.save_template(TEMPLATE)
        self.library.save_template({"id": saved["id"], "kcal": 450})
        self.library.save_template({"id": saved["id"], "kcal": 450})
        deleted = self.library.delete_template(saved["id"])
        self.assertEqual(deleted["change"]["action"], "delete")
        self.assertEqual(
            self.history_rows(),
            [
                ("nutrition_template", "create"),
                ("nutrition_template", "update"),
                ("nutrition_template", "delete"),
            ],
        )

    def test_undo_of_a_created_meal_removes_it(self) -> None:
        saved, changes = self.collected(lambda: self.library.save_template(TEMPLATE))
        self.undo(changes)
        self.assertEqual(self.library.list_templates(), [])
        with self.assertRaises(AppError):
            self.library.get_template(saved["id"])

    def test_undo_of_an_overwrite_restores_the_previous_values(self) -> None:
        saved = self.library.save_template(TEMPLATE)
        _, changes = self.collected(
            lambda: self.library.save_template(
                {"id": saved["id"], "name": "Haferbrei", "kcal": 500, "fat_g": 12}
            )
        )
        self.assertEqual(self.library.get_template(saved["id"])["kcal"], 500)
        self.undo(changes)
        restored = self.library.get_template(saved["id"])
        for key in ("name", "description", "kcal", "carbs_g", "fat_g", "meal_type"):
            self.assertEqual(restored[key], saved[key], key)
        self.assertEqual(restored["nutrition_basis"], saved["nutrition_basis"])

    def test_undo_of_a_delete_brings_the_meal_back(self) -> None:
        saved = self.library.save_template(TEMPLATE)
        _, changes = self.collected(lambda: self.library.delete_template(saved["id"]))
        self.assertEqual(self.library.list_templates(), [])
        self.undo(changes)
        restored = self.library.get_template(saved["id"])
        self.assertEqual(restored["name"], "Porridge")
        self.assertEqual(restored["nutrition_basis"], saved["nutrition_basis"])
        # The restored meal can be logged again.
        self.assertEqual(self.diary.log_template(saved["id"], 2)["kcal"], 800)

    def test_undo_of_a_delete_keeps_a_missing_macro(self) -> None:
        saved = self.library.save_template({**TEMPLATE, "fat_g": None})
        _, changes = self.collected(lambda: self.library.delete_template(saved["id"]))
        self.undo(changes)
        restored = self.library.get_template(saved["id"])
        self.assertIsNone(restored["fat_g"])
        self.assertEqual(self.diary.log_template(saved["id"], 1)["kcal"], 400)

    def test_undo_of_a_delete_is_refused_when_the_name_was_taken(self) -> None:
        saved = self.library.save_template(TEMPLATE)
        _, changes = self.collected(lambda: self.library.delete_template(saved["id"]))
        self.library.save_template({**TEMPLATE, "name": "porridge"})
        with self.assertRaises(AppError) as raised:
            self.undo(changes)
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(len(self.library.list_templates()), 1)

    def test_undo_is_refused_after_a_later_edit(self) -> None:
        saved, changes = self.collected(lambda: self.library.save_template(TEMPLATE))
        self.library.save_template({"id": saved["id"], "kcal": 410})
        with self.assertRaises(AppError) as raised:
            self.undo(changes)
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(self.library.get_template(saved["id"])["kcal"], 410)

    def test_a_coach_turn_that_saves_or_deletes_meals_is_undoable(self) -> None:
        for tool in ("save_nutrition_template", "delete_nutrition_template"):
            summary = change_summary(
                [
                    {
                        "tool": tool,
                        "result": {"ok": True},
                        "changes": [{"id": "x", "action": "create"}],
                    }
                ]
            )
            assert summary is not None
            self.assertTrue(summary["undoable"], tool)


if __name__ == "__main__":
    unittest.main()

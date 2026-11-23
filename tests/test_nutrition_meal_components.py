"""Direct correction of a logged meal's components (#3948)."""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from typing import Any

from nutrition_service_support import build_nutrition_services
from support import temp_database

from backend import change_history
from backend.errors import AppError

TOFU = "bls:H861000"
BAKED_TOFU = "bls:H861062"


class MealComponentEditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = temp_database(self, "components.db", schema=True)
        self.diary, self.library = build_nutrition_services(
            self.manager,
            lambda: "2026-11-04T12:00:00+00:00",
            lambda: datetime(2026, 11, 4, 14, 0, tzinfo=UTC),
        )
        self.skyr = self.library.product_catalog.save_product(
            {
                "name": "Skyr",
                "basis_amount": 100,
                "basis_unit": "g",
                "kcal": 60,
                "carbs_g": 4,
                "protein_g": 11,
                "fat_g": 0.2,
                "source": "manual",
            }
        )
        self.drink = self.library.product_catalog.save_product(
            {
                "name": "Saft",
                "basis_amount": 100,
                "basis_unit": "ml",
                "kcal": 45,
                "carbs_g": 11,
                "protein_g": 0,
                "fat_g": 0,
                "source": "manual",
            }
        )
        self.meal = self.diary.log_meal(
            {
                "description": "Tofu mit Skyr",
                "meal_date": "2026-11-04",
                "components": [
                    {"kind": "database", "food_id": TOFU, "amount": 150, "unit": "g"},
                    {
                        "kind": "local_product",
                        "product_id": self.skyr["id"],
                        "amount": 200,
                        "unit": "g",
                    },
                    {
                        "kind": "manual",
                        "name": "Creatine",
                        "amount": 5,
                        "unit": "g",
                        "kcal": 0,
                        "carbs_g": 0,
                        "protein_g": 0,
                        "fat_g": 0,
                    },
                ],
            }
        )

    def components(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        return entry["nutrition_basis"]["components"]

    def edit(self, entry_id: str, edits: Any) -> dict[str, Any]:
        """The edit as the dialog sends it: with the revision it read."""
        revision = self.diary.meal_components(entry_id)["revision"]
        return self.diary.edit_components(entry_id, edits, revision)

    def test_the_components_are_addressed_by_position(self) -> None:
        listed = self.diary.meal_components(self.meal["id"])["components"]
        self.assertEqual(
            [(item["name"], item["amount"]) for item in listed],
            [("Tofu", 150), ("Skyr", 200), ("Creatine", 5)],
        )

    def test_a_changed_amount_is_recalculated_and_the_others_stay_as_stored(
        self,
    ) -> None:
        updated = self.edit(self.meal["id"], [{"index": 0, "amount": 300}])

        tofu, skyr, creatine = self.components(updated)
        self.assertEqual((tofu["amount"], tofu["kcal"]), (300, 345))
        self.assertEqual((skyr, creatine), tuple(self.components(self.meal)[1:]))
        self.assertEqual(updated["kcal"], 345 + 120)
        # The origin keeps its per-100 values, so the intake totals follow the amount.
        self.assertEqual(
            tofu["nutrition_basis"], self.components(self.meal)[0]["nutrition_basis"]
        )
        self.assertEqual(updated["description"], "Tofu mit Skyr")
        self.assertEqual(self.diary.get_meal(self.meal["id"]), updated)

    def test_a_removed_component_leaves_the_rest_and_the_last_one_stays(self) -> None:
        updated = self.edit(
            self.meal["id"],
            [{"index": 0, "remove": True}, {"index": 2, "remove": True}],
        )
        self.assertEqual([item["name"] for item in self.components(updated)], ["Skyr"])
        self.assertEqual(updated["kcal"], 120)

        with self.assertRaises(AppError) as last:
            self.edit(self.meal["id"], [{"index": 0, "remove": True}])
        self.assertEqual(last.exception.reason, "last_component")
        self.assertEqual(self.diary.get_meal(self.meal["id"]), updated)

    def test_a_swap_keeps_the_amount_and_takes_the_new_origin(self) -> None:
        updated = self.edit(self.meal["id"], [{"index": 0, "food_id": BAKED_TOFU}])

        swapped = self.components(updated)[0]
        self.assertEqual(
            (swapped["name"], swapped["amount"], swapped["kcal"]),
            ("Tofu gebacken", 150, 240),
        )
        self.assertEqual(swapped["nutrition_basis"]["food"]["id"], BAKED_TOFU)
        self.assertEqual(updated["kcal"], 240 + 120)

        again = self.edit(
            self.meal["id"],
            [{"index": 1, "product_id": self.skyr["id"], "amount": 100}],
        )
        self.assertEqual(self.components(again)[1]["kcal"], 60)

    def test_a_swap_to_another_unit_is_refused_and_changes_nothing(self) -> None:
        before = self.diary.get_meal(self.meal["id"])
        with self.assertRaises(AppError) as mismatch:
            self.edit(self.meal["id"], [{"index": 1, "product_id": self.drink["id"]}])
        self.assertEqual(mismatch.exception.reason, "food_unit_mismatch")
        self.assertEqual(self.diary.get_meal(self.meal["id"]), before)

    def test_malformed_edits_change_nothing(self) -> None:
        before = self.diary.get_meal(self.meal["id"])
        invalid: list[Any] = [
            None,
            [],
            [{"index": 5, "amount": 1}],
            [{"index": -1, "amount": 1}],
            [{"index": True, "amount": 1}],
            [{"index": 0}],
            [{"index": 0, "amount": 1}, {"index": 0, "remove": True}],
            [{"index": 0, "remove": True, "amount": 5}],
            [{"index": 0, "remove": False}],
            [{"index": 0, "kcal": 1}],
            [{"index": 0, "food_id": TOFU, "product_id": self.skyr["id"]}],
            [{"index": 0, "amount": 0}],
            [{"index": 0, "amount": -3}],
            [{"index": 0, "amount": "viel"}],
            [{"index": 0, "amount": 10001}],
            [{"index": 0, "amount": 5, "unit": "ml"}],
            [{"index": 0, "food_id": "bls:unknown"}],
            [{"index": 0, "food_id": 7}],
        ]
        for edits in invalid:
            with self.subTest(edits=edits), self.assertRaises(AppError):
                self.edit(self.meal["id"], edits)
        with self.assertRaises(AppError) as unknown:
            self.edit("missing", [{"index": 0, "amount": 1}])
        self.assertEqual(unknown.exception.status, 404)
        with self.assertRaises(AppError):
            self.edit("", [{"index": 0, "amount": 1}])
        self.assertEqual(self.diary.get_meal(self.meal["id"]), before)

    def test_an_edit_of_a_meal_that_changed_meanwhile_is_refused(self) -> None:
        stale = self.diary.meal_components(self.meal["id"])["revision"]
        self.edit(self.meal["id"], [{"index": 0, "remove": True}])
        before = self.diary.get_meal(self.meal["id"])

        for revision in (stale, None, 7):
            with (
                self.subTest(revision=revision),
                self.assertRaises(AppError) as changed,
            ):
                self.diary.edit_components(
                    self.meal["id"], [{"index": 1, "remove": True}], revision
                )
            self.assertEqual(changed.exception.reason, "meal_changed")
        self.assertEqual(self.diary.get_meal(self.meal["id"]), before)

    def test_a_swap_with_an_unhashable_index_is_a_bad_request(self) -> None:
        with self.assertRaises(AppError) as invalid:
            self.edit(self.meal["id"], [{"index": [0], "food_id": TOFU}])
        self.assertEqual(invalid.exception.status, 400)

    def test_a_product_meal_without_an_amount_still_lists_one_component(self) -> None:
        entry = self.diary.log_product(self.skyr["id"], 200, "g")
        with self.manager.unit_of_work() as db:
            row = db.execute(
                "SELECT nutrition_basis FROM nutrition_logs WHERE id = ?",
                (entry["id"],),
            ).fetchone()
            basis = json.loads(row["nutrition_basis"])
            del basis["amount"]
            db.execute(
                "UPDATE nutrition_logs SET nutrition_basis = ? WHERE id = ?",
                (json.dumps(basis), entry["id"]),
            )
        listed = self.diary.meal_components(entry["id"])["components"]
        self.assertEqual([(c["kind"], c["unit"]) for c in listed], [("manual", "g")])

    def test_an_archived_product_cannot_replace_a_component(self) -> None:
        self.library.product_catalog.archive_product(self.skyr["id"])
        with self.assertRaises(AppError) as archived:
            self.edit(self.meal["id"], [{"index": 0, "product_id": self.skyr["id"]}])
        self.assertEqual(archived.exception.status, 409)

    def test_a_single_product_meal_is_edited_as_one_component(self) -> None:
        entry = self.diary.log_product(self.skyr["id"], 200, "g")
        self.assertEqual(
            [
                (c["name"], c["amount"], c["unit"])
                for c in self.diary.meal_components(entry["id"])["components"]
            ],
            [("Skyr", 200, "g")],
        )

        updated = self.edit(entry["id"], [{"index": 0, "amount": 100}])

        self.assertEqual(updated["kcal"], 60)
        self.assertEqual(updated["nutrition_basis"]["kind"], "composite")
        component = self.components(updated)[0]
        self.assertEqual(component["nutrition_basis"]["product"]["id"], self.skyr["id"])

    def test_a_database_meal_is_split_into_its_foods(self) -> None:
        entry = self.diary.log_meal(
            {
                "description": "Tofu",
                "food_ingredients": [{"food_id": TOFU, "amount": 100, "unit": "g"}],
            }
        )
        self.assertEqual(
            [
                (c["name"], c["amount"], c["kcal"])
                for c in self.diary.meal_components(entry["id"])["components"]
            ],
            [("Tofu", 100, 115)],
        )

        updated = self.edit(entry["id"], [{"index": 0, "amount": 200}])

        self.assertEqual(updated["kcal"], 230)

    def test_a_meal_of_typed_values_is_edited_as_one_estimated_component(self) -> None:
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
        updated = self.edit(entry["id"], [{"index": 0, "amount": 2}])
        self.assertEqual((updated["kcal"], updated["carbs_g"]), (1200, 180))
        self.assertEqual(self.components(updated)[0]["kind"], "estimate")
        self.assertEqual(updated["source"], "coach")

    def change_meal(self, **meal: Any) -> dict[str, Any]:
        """A change of the meal's own fields alone, as the dialog sends it (#3985)."""
        return self.diary.edit_components(self.meal["id"], None, None, meal)

    def test_a_meal_change_alone_keeps_the_components_and_the_origin(self) -> None:
        updated = self.change_meal(meal_type="dinner")

        self.assertEqual(updated["meal_type"], "dinner")
        self.assertEqual(updated["nutrition_basis"], self.meal["nutrition_basis"])
        self.assertEqual(updated["kcal"], self.meal["kcal"])

    def test_a_new_type_moves_a_default_time_but_not_a_named_one(self) -> None:
        # Logged at the entry time, the meal has no default time to move.
        kept = self.change_meal(meal_type="breakfast")
        self.assertEqual(kept["logged_at"], self.meal["logged_at"])

        self.assertEqual(
            self.change_meal(meal_time="09:00")["logged_at"], "2026-11-04T09:00:00"
        )
        moved = self.change_meal(meal_type="lunch")
        self.assertEqual(moved["logged_at"], "2026-11-04T12:00:00")

        named = self.change_meal(meal_time="13:30")
        self.assertEqual(named["logged_at"], "2026-11-04T13:30:00")
        self.assertEqual(
            self.change_meal(meal_type="dinner")["logged_at"], named["logged_at"]
        )

    def test_a_new_day_moves_the_meal(self) -> None:
        self.change_meal(meal_type="dinner")
        moved = self.change_meal(meal_date="2026-11-03")

        self.assertEqual(
            (moved["meal_date"], moved["logged_at"], moved["sync_state"]),
            ("2026-11-03", "2026-11-03T14:00:00+00:00", "local"),
        )

    def test_an_unknown_time_keeps_only_the_type_and_a_named_time_ends_it(self) -> None:
        unknown = self.change_meal(meal_type="snack", logged_time_known=False)
        self.assertFalse(unknown["logged_time_known"])
        self.assertEqual(
            self.change_meal(meal_type="lunch")["logged_time_known"], False
        )

        known = self.change_meal(meal_time="12:45")
        self.assertEqual(
            (known["logged_time_known"], known["logged_at"]),
            (True, "2026-11-04T12:45:00"),
        )

    def test_the_meal_change_and_the_component_edits_are_one_write(self) -> None:
        revision = self.diary.meal_components(self.meal["id"])["revision"]
        with change_history.collect_changes() as changes:
            updated = self.diary.edit_components(
                self.meal["id"],
                [{"index": 2, "remove": True}],
                revision,
                {"meal_type": "dinner", "meal_date": "2026-11-05"},
            )

        self.assertEqual(
            (updated["meal_date"], updated["meal_type"], len(self.components(updated))),
            ("2026-11-05", "dinner", 2),
        )
        self.assertEqual(len(changes), 1)

    def test_a_bad_meal_change_changes_nothing(self) -> None:
        revision = self.diary.meal_components(self.meal["id"])["revision"]
        bad: tuple[Any, ...] = (
            {},
            [],
            {"description": "x"},
            {"logged_time_known": True},
            {"logged_time_known": False, "meal_time": "10:00"},
            {"meal_type": "brunch"},
            {"meal_date": "04.11.2026"},
            {"meal_time": "25:99"},
            {"meal_date": ""},
            {"meal_date": None},
            {"meal_type": ""},
            {"meal_type": None},
            {"meal_time": ""},
            {"meal_time": None},
            {"meal_time": 930},
        )
        for meal in bad:
            with self.subTest(meal=meal), self.assertRaises(AppError) as raised:
                self.diary.edit_components(self.meal["id"], None, revision, meal)
            self.assertEqual(raised.exception.status, 400)
        with self.assertRaises(AppError):
            self.diary.edit_components(self.meal["id"], None, revision, None)
        self.assertEqual(self.diary.get_meal(self.meal["id"]), self.meal)

    def test_the_edit_is_undone_with_its_change_reference(self) -> None:
        with change_history.collect_changes() as changes:
            self.edit(self.meal["id"], [{"index": 0, "remove": True}])
        self.assertEqual([c["entity_type"] for c in changes], ["nutrition_entry"])


if __name__ == "__main__":
    unittest.main()

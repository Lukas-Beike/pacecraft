"""The uncertain-meal flag rides in the basis JSON and survives corrections (#3100)."""

from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import UTC, datetime

from nutrition_service_support import build_nutrition_services
from support import temp_database

from backend.change_history import accepted_audit_hashes, audit_hash, audit_projection
from backend.db.repositories import NutritionRepository
from backend.errors import AppError

MEAL = {
    "meal_date": "2026-09-24",
    "logged_at": "2026-09-24T19:30:00",
    "meal_type": "dinner",
    "description": "Pizza beim Italiener",
    "kcal": 900,
    "carbs_g": 100,
    "protein_g": 35,
    "fat_g": 35,
}


class UncertainMealFlagTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = temp_database(
            self, "uncertain.db", row_factory=sqlite3.Row, schema=True
        )
        self.service, _ = build_nutrition_services(
            self.manager,
            lambda: "2026-09-24T21:00:00+00:00",
            lambda: datetime(2026, 9, 24, 21, 0, tzinfo=UTC),
        )

    def stored_basis(self, entry_id: str) -> dict:
        with self.manager.reader() as db:
            row = db.execute(
                "SELECT nutrition_basis FROM nutrition_logs WHERE id = ?", (entry_id,)
            ).fetchone()
        return json.loads(row["nutrition_basis"])

    def test_a_meal_is_certain_unless_flagged(self) -> None:
        entry = self.service.log_meal(MEAL)
        self.assertIs(entry["estimate_uncertain"], False)
        self.assertNotIn("uncertain", self.stored_basis(entry["id"]))

    def test_the_flag_is_stored_in_the_basis_and_read_as_its_own_field(self) -> None:
        entry = self.service.log_meal({**MEAL, "estimate_uncertain": True})
        self.assertIs(entry["estimate_uncertain"], True)
        self.assertNotIn("uncertain", entry["nutrition_basis"])
        self.assertIs(self.stored_basis(entry["id"])["uncertain"], True)
        # The adaptive expenditure reads the diary through this range read.
        with self.manager.reader() as db:
            day = NutritionRepository(str).list_by_range(db, "2026-09-24", "2026-09-24")
        self.assertIs(day[0]["estimate_uncertain"], True)

    def test_a_correction_that_rewrites_the_basis_keeps_the_flag(self) -> None:
        entry = self.service.log_meal({**MEAL, "estimate_uncertain": True})
        corrected = self.service.correct_meal(entry["id"], {"kcal": 1100})
        self.assertEqual(corrected["nutrition_basis"], {"kind": "manual_correction"})
        self.assertIs(corrected["estimate_uncertain"], True)
        updated = self.service.update_meal(entry["id"], {"description": "Pizza"})
        self.assertIs(updated["estimate_uncertain"], True)

    def test_the_flag_can_be_set_and_cleared_alone(self) -> None:
        entry = self.service.log_meal(MEAL)
        flagged = self.service.correct_meal(entry["id"], {"estimate_uncertain": True})
        self.assertIs(flagged["estimate_uncertain"], True)
        self.assertEqual(flagged["kcal"], 900)
        cleared = self.service.update_meal(entry["id"], {"estimate_uncertain": False})
        self.assertIs(cleared["estimate_uncertain"], False)
        self.assertNotIn("uncertain", self.stored_basis(entry["id"]))

    def test_the_flag_must_be_a_boolean(self) -> None:
        with self.assertRaises(AppError) as raised:
            self.service.log_meal({**MEAL, "estimate_uncertain": "ja"})
        self.assertEqual(raised.exception.status, 400)

    def test_history_rows_from_before_the_flag_still_match(self) -> None:
        entry = self.service.log_meal(MEAL)
        current = audit_projection("nutrition_entry", entry)
        assert current is not None
        self.assertIs(current["estimate_uncertain"], False)
        # A row from before the flag also predates the v32 volume and session.
        later = {"estimate_uncertain", "volume_ml", "session_id"}
        legacy = {k: v for k, v in current.items() if k not in later}
        self.assertIn(
            audit_hash(legacy), accepted_audit_hashes("nutrition_entry", current)
        )


if __name__ == "__main__":
    unittest.main()

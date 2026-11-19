"""Direct Coach profile authorization and optimistic-update regressions."""

import threading
import unittest
from contextlib import contextmanager
from unittest.mock import Mock

from backend.coach.profile_update import CoachProfileUpdateService
from backend.errors import AppError


class CoachProfileUpdateTests(unittest.TestCase):
    def setUp(self):
        self.profile = Mock()
        self.profile.mutation_coordinator = threading.RLock()
        self.profile.get.return_value = {"name": "Athlete", "goals": "Current"}
        self.profile.save.side_effect = lambda updated: updated
        self.manager = Mock()

        @contextmanager
        def unit_of_work():
            yield object()

        self.manager.unit_of_work.side_effect = unit_of_work
        self.service = CoachProfileUpdateService(
            self.profile,
            self.manager,
        )
        self.intent = {
            "operation": "update_profile",
            "target_system": "local",
            "authorization_scope": ["local_profile"],
        }

    def test_authorization_precedes_profile_read_and_write(self):
        with self.assertRaises(AppError) as raised:
            self.service.apply({"changes": []}, {"authorization_scope": []})
        self.assertEqual(raised.exception.status, 403)
        self.profile.get.assert_not_called()
        self.profile.save.assert_not_called()

    def test_later_field_conflict_aborts_entire_batch(self):
        changes = [
            {"field": "name", "expected_value": "Athlete", "value": "Updated"},
            {"field": "goals", "expected_value": "Stale", "value": "New"},
        ]
        with self.assertRaises(AppError) as raised:
            self.service.apply({"changes": changes}, self.intent)
        self.assertEqual(raised.exception.reason, "profile_conflict")
        self.profile.save.assert_not_called()

    def test_weight_and_body_fat_are_not_profile_fields_for_the_coach(self):
        # #3640: Körper owns them; the Coach neither writes nor sees the stored
        # profile values, which are no current value.
        self.profile.get.return_value = {
            "name": "Athlete", "weight_kg": "80", "body_fat_pct": "15"
        }  # fmt: skip
        for field in ("weight_kg", "body_fat_pct"):
            with self.subTest(field=field), self.assertRaises(AppError) as raised:
                self.service.apply(
                    {
                        "changes": [
                            {"field": field, "expected_value": "80", "value": "79"}
                        ]
                    },
                    self.intent,
                )
            self.assertEqual(raised.exception.reason, "tool_arguments_invalid")
        self.profile.save.assert_not_called()
        result = self.service.apply(
            {"changes": [{"field": "name", "expected_value": "Athlete", "value": "A"}]},
            self.intent,
        )
        self.assertEqual({"name": "A"}, result["profile"])

    def test_valid_batch_saves_only_selected_fields(self):
        result = self.service.apply(
            {
                "changes": [
                    {"field": "name", "expected_value": "Athlete", "value": "Updated"}
                ]
            },
            self.intent,
        )
        self.assertEqual(result["updated_fields"], ["name"])
        self.assertEqual(result["profile"], {"name": "Updated", "goals": "Current"})
        self.profile.save.assert_called_once_with(
            {"name": "Updated", "goals": "Current"}
        )

    def test_the_coach_reads_and_sets_the_energy_goal(self):
        # #3547: the goal is a profile field, so the Coach context's
        # durable_profile carries it and an athlete-started turn may set it.
        self.profile.get.return_value = {"energy_goal": "", "energy_goal_kcal": ""}
        result = self.service.apply(
            {
                "changes": [
                    {"field": "energy_goal", "expected_value": "", "value": "lose"},
                    {"field": "energy_goal_kcal", "expected_value": "", "value": "400"},
                ]
            },
            self.intent,
        )
        self.assertEqual(result["updated_fields"], ["energy_goal", "energy_goal_kcal"])
        self.assertEqual(
            {"energy_goal": "lose", "energy_goal_kcal": "400"}, result["profile"]
        )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import threading
import unittest

from support import temp_database

from backend.coach.training_template_tools import TrainingTemplateToolService
from backend.db.manager import DatabaseManager
from backend.errors import STRUCTURED_AUTHORIZATION_ERROR, AppError


class TrainingTemplateToolTests(unittest.TestCase):
    def test_restore_batch_uses_current_manager_and_rolls_back_prior_write(
        self,
    ) -> None:
        old_manager = temp_database(self, "old.db", row_factory=None)
        current_manager = temp_database(self, "current.db", row_factory=None)
        for manager in (old_manager, current_manager):
            with manager.unit_of_work() as db:
                db.execute("CREATE TABLE writes (template_id TEXT PRIMARY KEY)")

        active_manager = [old_manager]
        library_resolutions: list[DatabaseManager] = []

        class Library:
            def update(self, local_id, values):
                with active_manager[0].unit_of_work() as db:
                    db.execute("INSERT INTO writes VALUES (?)", (local_id,))
                    if values.get("invalid"):
                        raise AppError(400, "invalid template")
                return {"local_id": local_id}

            def create_template(self, values):
                raise AssertionError("Unexpected create")

        def manager_factory():
            return active_manager[0]

        def library_factory():
            # Resolved inside the unit of work, on the current manager.
            self.assertEqual(
                active_manager[0]._writer_lock._owner, threading.get_ident()
            )
            library_resolutions.append(active_manager[0])
            return Library()

        service = TrainingTemplateToolService(manager_factory, library_factory)
        active_manager[0] = (
            current_manager  # Simulate manager replacement after restore.
        )
        intent = {
            "operation": "manage_training_templates",
            "authorization_scope": [
                "library_workout:restored",
                "library_workout:bad",
            ],
        }

        with self.assertRaisesRegex(AppError, "invalid template"):
            service.execute(
                {
                    "templates": [
                        {"action": "restore", "local_id": "restored"},
                        {"action": "update", "local_id": "bad", "invalid": True},
                    ]
                },
                intent,
            )

        self.assertEqual(library_resolutions, [current_manager])
        with current_manager.reader() as db:
            self.assertEqual(db.execute("SELECT * FROM writes").fetchall(), [])
        with old_manager.reader() as db:
            self.assertEqual(db.execute("SELECT * FROM writes").fetchall(), [])

    def test_requires_operation_and_template_scope_before_manager_resolution(
        self,
    ) -> None:
        manager_calls = []
        service = TrainingTemplateToolService(
            lambda: manager_calls.append("manager"),
            lambda: self.fail("Library must not be resolved"),
        )
        with self.assertRaises(AppError) as denied:
            service.execute({"templates": [{"name": "x"}]}, {"operation": "other"})
        self.assertEqual(denied.exception.message, STRUCTURED_AUTHORIZATION_ERROR)
        self.assertEqual(manager_calls, [])

        with self.assertRaises(AppError) as missing_scope:
            service.execute(
                {"templates": [{"name": "x"}]},
                {"operation": "manage_training_templates", "authorization_scope": []},
            )
        self.assertEqual(missing_scope.exception.status, 403)
        self.assertEqual(manager_calls, [])

    def test_an_existing_template_is_reused_before_a_new_one_is_created(
        self,
    ) -> None:
        # #3080, scripted: the Coach creates a template the library already has.
        manager = temp_database(self, "library.db", row_factory=None)
        easy = {
            "id": "11111111-1111-4111-8111-111111111111",
            "name": "Lockerer Dauerlauf",
            "sport": "Run",
            "duration_minutes": 45,
            "description": "- 45m Z2 Pace",
        }
        created: list[dict] = []

        class Library:
            def list(self, limit):
                return [easy]

            def create_template(self, values):
                created.append(values)
                return {"local_id": f"new-{len(created)}"}

        service = TrainingTemplateToolService(lambda: manager, Library)
        intent = {
            "operation": "manage_training_templates",
            "authorization_scope": ["local_template"],
        }
        again = {
            "action": "create",
            "name": "Lockerer Dauerlauf 45",
            "sport": "Run",
            "duration_minutes": 50,
            "description": "- 45m Z2 Pace",
        }
        intervals = {
            "action": "create",
            "name": "6 x 800 m",
            "sport": "Run",
            "duration_minutes": 55,
            "description": "Warmup\n- 15m Z1\n6x\n- 800mtr Z4 Pace\n- 2m Z1",
        }

        with self.assertRaises(AppError) as refused:
            service.execute({"templates": [intervals, again]}, intent)
        # The whole call is refused before any write; the model gets the
        # template to reuse.
        self.assertEqual(
            (refused.exception.status, refused.exception.reason),
            (409, "template_exists"),
        )
        self.assertEqual(
            getattr(refused.exception, "findings", None),
            [
                {
                    "code": "template_exists",
                    "local_id": easy["id"],
                    "name": "Lockerer Dauerlauf",
                    "sport": "Run",
                    "duration_minutes": 45,
                }
            ],
        )
        self.assertEqual(created, [])

        # Its retry names how the new template differs: it is kept and counted.
        result = service.execute(
            {
                "templates": [
                    intervals,
                    {**again, "new_because": "mit 5 Steigerungen am Ende"},
                ]
            },
            intent,
        )
        self.assertEqual(
            [item["name"] for item in created], ["6 x 800 m", "Lockerer Dauerlauf 45"]
        )
        self.assertEqual(result["created_despite_candidate"], 1)
        # A new template without a match counts nothing.
        result = service.execute({"templates": [intervals]}, intent)
        self.assertEqual(0, result["created_despite_candidate"])


if __name__ == "__main__":
    unittest.main()

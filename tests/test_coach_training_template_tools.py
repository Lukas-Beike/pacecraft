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
        old_manager = temp_database(self, "old.db")
        current_manager = temp_database(self, "current.db")
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
        manager = temp_database(self, "library.db")
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
        # template to reuse. The intervals were dropped too, so the refusal
        # names the call and stays a turn failure until it is retried.
        self.assertEqual(
            (refused.exception.status, refused.exception.reason),
            (409, "template_exists_in_call"),
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
        # The lone duplicate lost nothing else: the existing template answers.
        with self.assertRaises(AppError) as alone:
            service.execute({"templates": [again]}, intent)
        self.assertEqual("template_exists", alone.exception.reason)

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

        # Two near-identical creates in one call: the second is refused against
        # the first, which has no local_id yet.
        with self.assertRaises(AppError) as twice:
            service.execute(
                {"templates": [intervals, {**intervals, "duration_minutes": 60}]},
                intent,
            )
        [finding] = getattr(twice.exception, "findings", None) or [None]
        self.assertEqual((None, "6 x 800 m"), (finding["local_id"], finding["name"]))
        self.assertEqual("template_exists_in_call", twice.exception.reason)

        # A strength template has no description: the same name in the same
        # sport is the match.
        strength = {
            "id": "22222222-2222-4222-8222-222222222222",
            "name": "Kraft A",
            "sport": "WeightTraining",
            "duration_minutes": 40,
            "description": "",
        }
        Library.list = lambda _self, _limit: [easy, strength]  # type: ignore[method-assign]
        with self.assertRaises(AppError) as same_name:
            service.execute(
                {
                    "templates": [
                        {
                            "action": "create",
                            "name": "Kraft A",
                            "sport": "WeightTraining",
                        }
                    ]
                },
                intent,
            )
        self.assertEqual(
            strength["id"],
            (getattr(same_name.exception, "findings", None) or [{}])[0]["local_id"],
        )

        # A template this call archives is not compared: its replacement passes.
        Library.update = lambda _self, local_id, _values: {"local_id": local_id}  # type: ignore[attr-defined]
        replaced = service.execute(
            {
                "templates": [
                    {"action": "archive", "local_id": easy["id"]},
                    {**again, "name": "Lockerer Dauerlauf"},
                ]
            },
            {
                **intent,
                "authorization_scope": [
                    "local_template",
                    f"library_workout:{easy['id']}",
                ],
            },
        )
        self.assertEqual(0, replaced["created_despite_candidate"])

    def test_a_step_test_template_is_built_by_the_server(self) -> None:
        # #3833: the Coach names the paces; steps, name and focus come from the protocol.
        manager = temp_database(self, "library.db")
        created: list[dict] = []
        updated: list[tuple[str, dict]] = []

        class Library:
            def list(self, limit, include_archived=False):
                return [
                    {"id": "old", "test_protocol": "run_step_test"},
                    {"id": "plain"},
                ]

            def create_template(self, values):
                created.append(values)
                return {"local_id": "new"}

            def update(self, local_id, values):
                updated.append((local_id, values))
                return {"local_id": local_id}

        service = TrainingTemplateToolService(lambda: manager, Library)
        intent = {
            "operation": "manage_training_templates",
            "authorization_scope": [
                "local_template",
                "library_workout:old",
                "library_workout:plain",
            ],
        }
        service.execute(
            {
                "templates": [
                    {
                        "action": "create",
                        "name": "Eigener Name",
                        "description": "- 30m Z2 Pace",
                        "step_test": {
                            "lt1_pace_seconds_per_km": None,
                            "lt2_pace_seconds_per_km": None,
                        },
                    },
                    {
                        "action": "update",
                        "local_id": "old",
                        "step_test": {
                            "lt1_pace_seconds_per_km": 300,
                            "lt2_pace_seconds_per_km": 280,
                        },
                    },
                ]
            },
            intent,
        )
        (template,) = created
        self.assertEqual(template["name"], "Stufentest Laufen (LT1 und LT2)")
        self.assertEqual(template["test_protocol"], "run_step_test")
        self.assertEqual(template["session_focus"], "test")
        # Stage 1 at 9.5 km/h is 6:19 /km, held as a ±3 % band; the Coach's own
        # description is not kept.
        self.assertIn("- 3m 6:08-6:30/km Pace", template["description"])
        self.assertNotIn("step_test", template)
        ((local_id, values),) = updated
        self.assertEqual(local_id, "old")
        self.assertEqual(values["duration_minutes"], 10 + 8 * 3 + 10)
        with self.assertRaises(AppError) as refused:
            service.execute(
                {
                    "templates": [
                        {"action": "archive", "local_id": "old", "step_test": {}}
                    ]
                },
                intent,
            )
        self.assertEqual(refused.exception.reason, "invalid_template_action")
        # Only a step-test template is renewed as one; another is never rewritten.
        for local_id in ("plain", "missing"):
            with (
                self.subTest(local_id=local_id),
                self.assertRaises(AppError) as refused,
            ):
                service.execute(
                    {
                        "templates": [
                            {
                                "action": "update",
                                "local_id": local_id,
                                "step_test": {
                                    "lt1_pace_seconds_per_km": None,
                                    "lt2_pace_seconds_per_km": None,
                                },
                            }
                        ]
                    },
                    intent,
                )
            self.assertEqual(
                (refused.exception.status, refused.exception.reason),
                (400, "invalid_template_action"),
            )
        self.assertEqual(len(updated), 1)


if __name__ == "__main__":
    unittest.main()

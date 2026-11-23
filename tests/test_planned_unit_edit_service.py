"""The athlete's own create, edit and archive of planned units (#3950)."""

from __future__ import annotations

import itertools
import json
import unittest
import uuid
from datetime import date
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

from support import temp_database

from backend.db.repositories import (
    KeyValueRepository,
    PlanningStateRepository,
    TrainingPlanRepository,
)
from backend.db.schema import initialize_schema
from backend.errors import AppError
from backend.planning import changes, plan_checks
from backend.planning.calendar_service import CalendarConflictService
from backend.planning.library import library_payload_hash
from backend.planning.library_service import WorkoutLibraryService
from backend.planning.plan_guard_service import PlanCheckError, PlanGuardService
from backend.planning.planned_unit_edit_service import (
    UNIT_FIELDS,
    PlannedUnitEditService,
)
from backend.planning.planned_unit_service import PlannedUnitService
from backend.planning.replacement_service import (
    StructuredTrainingPlanReplacementService,
)
from backend.planning.revision import PlanningRevisionService
from backend.planning.training_plans import TrainingPlanService

NOW = "2026-09-20T08:00:00+00:00"
STEADY = {
    "date": "2026-09-22",
    "sport": "Ride",
    "name": "Grundlage 60",
    "description": "- 60m 60%",
    "duration_minutes": 60,
    "target": "POWER",
    "session_focus": "endurance",
}


class _Guard:
    """The plan guard with scripted verdicts; it records what the edit passed."""

    def __init__(self) -> None:
        self.refuse: list[dict] | None = None
        self.verified: list[tuple] = []

    def findings(self) -> list:
        return []

    def verify(self, before, accepted, *, waived=(), inherited=()):
        self.verified.append((list(accepted), tuple(waived)))
        if self.refuse and not {item["code"] for item in self.refuse} <= set(accepted):
            raise PlanCheckError(self.refuse)
        return [{"code": "load_spike", "blocking": False}]


class PlannedUnitEditServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = temp_database(self, "unit-edit.db")
        with self.manager.unit_of_work() as db:
            initialize_schema(db)
            db.execute(
                "INSERT INTO planning_state(id, revision, updated_at) VALUES (1, 0, ?)",
                (NOW,),
            )
        ids = itertools.count(1)
        revision = PlanningRevisionService(PlanningStateRepository(), lambda: NOW)
        conflicts = CalendarConflictService(
            self.manager, SimpleNamespace(list_events=lambda *_args, **_kw: [])
        )

        def today():
            return date(2026, 9, 20)

        planned = PlannedUnitService(
            self.manager,
            revision,
            lambda: NOW,
            today,
            lambda: uuid.UUID(int=next(ids)),
            lambda value: value,
            conflicts,
            lambda: None,
        )
        self.library = WorkoutLibraryService(
            self.manager,
            lambda: NOW,
            lambda: str(uuid.UUID(int=100 + next(ids))),
            Mock(),
        )
        plans = TrainingPlanRepository()
        training_plans = TrainingPlanService(
            self.manager,
            plans,
            KeyValueRepository(lambda: NOW),
            revision,
            Mock(),
            lambda: NOW,
        )
        self.plan_service = training_plans
        replacement = StructuredTrainingPlanReplacementService(
            self.manager,
            PlanningStateRepository(),
            revision,
            plans,
            KeyValueRepository(lambda: NOW),
            conflicts,
            planned,
            today,
            lambda: NOW,
            lambda: str(uuid.UUID(int=500 + next(ids))),
        )
        self.change_service = change_service = changes.StructuredTrainingChangeService(
            self.manager,
            changes.StructuredTrainingChangeValidator(
                PlanningStateRepository(), conflicts
            ),
            changes.StructuredTrainingPlanResolver(plans),
            planned,
            revision,
            training_plans,
            today,
            changes.TRAINING_CHANGE_LIMIT,
            lambda: None,
        )
        self.guard = _Guard()
        self.service = PlannedUnitEditService(
            self.manager,
            lambda: change_service,
            lambda: self.library,
            lambda: cast(PlanGuardService, self.guard),
            replacement_service=lambda: replacement,
            training_plan_service=lambda: training_plans,
        )

    def rows(self, table):
        with self.manager.reader() as db:
            return [
                (row["local_id"], json.loads(row["payload"]), row["payload"])
                for row in db.execute(f"SELECT local_id, payload FROM {table}")
            ]

    def unit(self, fields, plan_id=None):
        """A unit as the change batch stores it, with its library entry, for the tests that edit or replace one."""
        template = self.library.create_template(
            {k: v for k, v in fields.items() if k not in {"date", "starts_at_local"}},
            "manual",
        )
        change = {
            "action": "create",
            **{k: fields[k] for k in UNIT_FIELDS if k in fields},
            "template_id": template["id"],
            "target": fields.get("target") or "AUTO",
            "rationale": "Vom Athleten angelegt",
            **({"plan_id": plan_id} if plan_id else {}),
        }
        with self.manager.unit_of_work():
            result = self.change_service.apply(
                {"changes": [change]}, authorized_plan_id=plan_id, source="manual"
            )
        return result["changes"][0]

    def test_update_needs_the_current_hash_and_changes_the_content(self) -> None:
        self.unit(dict(STEADY))
        local_id, _, raw = self.rows("planned_units")[0]

        with self.assertRaises(AppError) as raised:
            self.service.update(local_id, {"name": "Neu"}, None)
        self.assertEqual("planning_revision_conflict", raised.exception.reason)
        with self.assertRaises(AppError) as raised:
            self.service.update(local_id, {"name": "Neu"}, "0" * 64)
        self.assertEqual("payload_hash_conflict", raised.exception.reason)

        result = self.service.update(
            local_id,
            {"name": "Neu", "description": "- 45m 60%", "duration_minutes": 45},
            library_payload_hash(raw),
        )
        unit = self.rows("planned_units")[0][1]
        self.assertEqual(("Neu", 45 * 60), (unit["name"], unit["planned_duration_s"]))
        # The athlete's edit waives the rules written for the Coach, and nothing else.
        self.assertEqual([([], plan_checks.REQUIRED_CODES)], self.guard.verified)
        self.assertEqual("load_spike", result["plan_check_notes"][0]["code"])

    def test_update_under_a_blocking_check_needs_the_second_tap(self) -> None:
        self.unit(dict(STEADY))
        local_id, _, raw = self.rows("planned_units")[0]
        edit = {"name": "Neu"}

        self.guard.refuse = [{"code": "illness_hard", "message": "m"}]
        with self.assertRaises(PlanCheckError):
            self.service.update(local_id, edit, library_payload_hash(raw))
        # The refused write left the unit as it was.
        self.assertEqual(STEADY["name"], self.rows("planned_units")[0][1]["name"])

        self.service.update(
            local_id, edit, library_payload_hash(raw), accept=["illness_hard"]
        )
        self.assertEqual("Neu", self.rows("planned_units")[0][1]["name"])

    def test_archive_hides_the_unit_and_needs_the_hash(self) -> None:
        self.unit(dict(STEADY))
        local_id, _, raw = self.rows("planned_units")[0]

        with self.assertRaises(AppError):
            self.service.set_archived(local_id, True, None)
        self.service.set_archived(local_id, True, library_payload_hash(raw))

        self.assertTrue(self.rows("planned_units")[0][1]["archived"])

    def test_replace_period_swaps_the_listed_units_for_new_ones(self) -> None:
        plan_id = self.plan_service.create(
            {"name": "Aufbau", "start_date": "2026-09-21", "end_date": "2026-10-04"}
        )["plan_id"]
        first = self.unit({**STEADY, "date": "2026-09-22"}, plan_id=plan_id)
        second = self.unit({**STEADY, "date": "2026-09-24"}, plan_id=plan_id)
        listed = [first["local_id"], second["local_id"]]
        new = {
            **STEADY,
            "name": "Neu 45",
            "description": "- 45m 60%",
            "duration_minutes": 45,
            "date": "2026-09-23",
        }

        with self.assertRaises(AppError) as raised:
            self.service.replace_period(
                plan_id, "2026-09-21", "2026-09-27", listed[:1], [new]
            )
        self.assertEqual("period_changed", raised.exception.reason)

        result = self.service.replace_period(
            plan_id, "2026-09-21", "2026-09-27", listed, [new]
        )

        self.assertEqual((2, 1), (result["archived_count"], result["created_count"]))
        live = [
            unit
            for _, unit, _raw in self.rows("planned_units")
            if not unit.get("local_deleted")
        ]
        self.assertEqual(["Neu 45"], [unit["name"] for unit in live])
        self.assertTrue(live[0]["template_id"])
        # The plan stays what it is: the new unit joins it and no second plan is made.
        self.assertEqual(plan_id, live[0]["plan_id"])
        self.assertEqual([plan_id], [plan["id"] for plan in self.plan_service.list()])
        # Its period follows the days of its units (the plan began on 2026-09-21).
        plan = self.plan_service.list()[0]
        self.assertEqual(
            ("2026-09-23", "2026-09-23"), (plan["start_date"], plan["end_date"])
        )

    def test_replace_period_refuses_a_period_that_is_past_or_out_of_order(
        self,
    ) -> None:
        plan_id = self.plan_service.create(
            {"name": "Aufbau", "start_date": "2026-09-21", "end_date": "2026-10-04"}
        )["plan_id"]
        unit = self.unit({**STEADY, "date": "2026-09-22"}, plan_id=plan_id)
        new = {**STEADY, "name": "Neu", "date": "2026-09-23"}

        # A new unit without its minimum fields is refused before anything is archived.
        with self.assertRaises(AppError) as raised:
            self.service.replace_period(
                plan_id,
                "2026-09-21",
                "2026-09-27",
                [unit["local_id"]],
                [{"date": "2026-09-23", "name": "x"}],
            )
        self.assertEqual(400, raised.exception.status)
        for start, end in (
            ("2026-09-01", "2026-09-27"),
            ("2026-09-27", "2026-09-21"),
            ("kein Datum", "2026-09-27"),
        ):
            with self.assertRaises(AppError) as raised:
                self.service.replace_period(
                    plan_id, start, end, [unit["local_id"]], [new]
                )
            self.assertEqual(
                (400, "invalid_plan"),
                (raised.exception.status, raised.exception.reason),
            )
        live = [
            unit["name"]
            for _, unit, _raw in self.rows("planned_units")
            if not unit.get("local_deleted")
        ]
        self.assertEqual([STEADY["name"]], live)


if __name__ == "__main__":
    unittest.main()

"""Equipment items, assignments and maintenance record change history.

Temporary database and synthetic gear only.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from backend import change_history
from backend.athlete import equipment as equipment_module
from backend.athlete.equipment import (
    RULE_APPLY_CHUNK,
    RULE_HISTORY_LIMIT,
    EquipmentService,
    rule_choice,
    session_kind,
)
from backend.athlete.equipment_history import equipment_history_records
from backend.canonical.activity_summary import ActivitySummary
from backend.coach.change_receipts import change_summary
from backend.db import DatabaseManager, planning_store, row_factory
from backend.db.schema import initialize_schema
from backend.errors import AppError
from backend.history.service import ChangeHistoryService
from backend.history.undo_service import HistoryUndoService

NOW = "2026-10-02T12:00:00+00:00"
BIKE = {
    "name": "Road bike",
    "sport": "ride",
    "kind": "bike",
    "start_date": "2026-08-01",
    "initial_distance_km": 100,
    "initial_hours": 5,
    "maintenance_km": 500,
}


def ride(identifier: str) -> ActivitySummary:
    return {
        "id": "act_" + identifier,
        "started_at": "2026-09-10T07:00:00+00:00",
        "started_at_local": "2026-09-10T07:00:00",
        "sport": "ride",
        "environment": "unknown",
        "record_ids": {"wahoo": identifier},
        "distance_m": 30000.0,
        "moving_duration_s": 5400.0,
    }


class EquipmentHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.manager = DatabaseManager(
            Path(temporary.name) / "equipment_history.db",
            sqlite3,
            row_factory=row_factory,
        )
        self.addCleanup(self.manager.close)
        with self.manager.unit_of_work() as db:
            initialize_schema(db)
        self.summaries = lambda: [ride("w1"), ride("w2")]
        self.service = EquipmentService(
            self.manager,
            self.summaries,
            lambda: date(2026, 10, 2),
            lambda: NOW,
        )

    def collected(self, write: Any) -> tuple[Any, list[dict[str, str]]]:
        with change_history.collect_changes() as changes:
            result = write()
        return result, changes

    def undo(self, changes: list[dict[str, str]]) -> None:
        unused: Any = None
        services = equipment_history_records(lambda: NOW, self.summaries)
        undo = HistoryUndoService(
            self.manager,
            ChangeHistoryService(self.manager, unused, record_services=services),
            *(unused,) * 6,
            record_services=services,
            profile_mutation_coordinator=threading.RLock(),
        )
        with self.manager.unit_of_work() as db:
            undo.apply_many(db, changes)

    def actions(self, entity_type: str) -> list[str]:
        with self.manager.reader() as db:
            return [
                row["action"]
                for row in db.execute(
                    "SELECT action FROM change_history WHERE entity_type=? "
                    "ORDER BY created_at, rowid",
                    (entity_type,),
                ).fetchall()
            ]

    def stored(self, key: str) -> dict[str, Any] | None:
        with self.manager.reader() as db:
            row = db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None

    def record(self, key: str) -> dict[str, Any]:
        stored = self.stored(key)
        assert stored is not None
        return stored

    def bike(self, **extra: Any) -> dict[str, Any]:
        return self.service.save({**BIKE, **extra})["equipment"]

    def edit(self, item: dict[str, Any], **changes: Any) -> dict[str, Any]:
        payload = {
            key: item[key]
            for key in (
                *BIKE,
                "id",
                "environment",
                "status",
                "parent_id",
                "lifetime_target_km",
            )
            if key in item
        }
        return self.service.save(
            {**payload, "expected_revision": item["revision"], **changes}
        )["equipment"]

    # Items

    def test_save_records_create_then_update_and_returns_the_reference(self) -> None:
        created = self.service.save(BIKE)
        self.assertEqual(created["change"]["entity_type"], "equipment")
        item = created["equipment"]
        self.edit(item, name="Gravel bike")
        self.assertEqual(self.actions("equipment"), ["create", "update"])

    def test_an_unchanged_save_records_nothing(self) -> None:
        item = self.bike()
        saved = self.service.save(
            {
                **{key: item[key] for key in (*BIKE, "id", "environment")},
                "expected_revision": item["revision"],
            }
        )
        self.assertIsNone(saved["change"])
        self.assertEqual(self.actions("equipment"), ["create"])

    def test_undo_of_a_created_item_removes_it(self) -> None:
        saved, changes = self.collected(lambda: self.service.save(BIKE))
        self.undo(changes)
        self.assertIsNone(self.stored("equipment:" + saved["equipment"]["id"]))

    def test_undo_of_a_created_item_is_refused_once_it_is_used(self) -> None:
        saved, changes = self.collected(lambda: self.service.save(BIKE))
        self.service.maintain(
            {"equipment_id": saved["equipment"]["id"], "date": "2026-10-01"}
        )
        with self.assertRaises(AppError) as raised:
            self.undo(changes)
        self.assertEqual(raised.exception.status, 409)
        self.assertIsNotNone(self.stored("equipment:" + saved["equipment"]["id"]))

    def test_undo_of_a_created_item_is_refused_while_a_plan_or_rule_names_it(self) -> None:
        self.planned_unit()
        for use in (
            lambda item: self.service.assign({"planned_unit_id": "unit-1", "equipment_id": item}),
            lambda item: self.service.save_rule(
                {"kind": "ride:outdoor", "equipment_id": item, "expected_revision": 0}
            ),
        ):
            with self.manager.unit_of_work() as db:
                db.execute("DELETE FROM kv WHERE key LIKE 'equipment_planned:%' OR key LIKE 'equipment_rule:%'")
            saved, changes = self.collected(lambda: self.service.save(BIKE))
            use(saved["equipment"]["id"])
            with self.subTest(use=use), self.assertRaises(AppError) as raised:
                self.undo(changes)
            self.assertEqual(raised.exception.status, 409)

    def test_undo_of_an_edit_restores_values_and_advances_the_revision(self) -> None:
        item = self.bike(lifetime_target_km=3000)
        edited, changes = self.collected(
            lambda: self.edit(
                item, name="Gravel bike", maintenance_km=None, lifetime_target_km=None
            )
        )
        self.undo(changes)
        restored = self.stored("equipment:" + item["id"]) or {}
        for key in ("name", "maintenance_km", "lifetime_target_km", "status", "kind"):
            self.assertEqual(restored[key], item[key], key)
        self.assertEqual(restored["revision"], edited["revision"] + 1)

    def test_undo_of_an_archive_brings_the_item_back(self) -> None:
        item = self.bike()
        _, changes = self.collected(lambda: self.edit(item, status="archived"))
        self.undo(changes)
        self.assertEqual(self.record("equipment:" + item["id"])["status"], "active")

    def test_undo_keeps_garmin_owned_mileage(self) -> None:
        item = self.bike()
        _, changes = self.collected(lambda: self.edit(item, name="Renamed"))
        with self.manager.unit_of_work() as db:
            stored = self.stored("equipment:" + item["id"]) or {}
            stored["garmin_distance_km"] = 1234.5
            db.execute(
                "UPDATE kv SET value=? WHERE key=?",
                (json.dumps(stored), "equipment:" + item["id"]),
            )
        self.undo(changes)
        restored = self.stored("equipment:" + item["id"]) or {}
        self.assertEqual(restored["name"], item["name"])
        self.assertEqual(restored["garmin_distance_km"], 1234.5)

    def test_undo_is_refused_after_a_later_edit(self) -> None:
        item = self.bike()
        _, changes = self.collected(lambda: self.edit(item, name="Gravel bike"))
        later = self.stored("equipment:" + item["id"])
        assert later is not None
        self.edit(later, name="Third name")
        with self.assertRaises(AppError) as raised:
            self.undo(changes)
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(self.record("equipment:" + item["id"])["name"], "Third name")

    def test_undo_is_refused_when_the_parent_no_longer_fits(self) -> None:
        first = self.bike(name="First")
        second = self.bike(name="Second")
        part = self.service.save(
            {
                "name": "Chain",
                "sport": "ride",
                "kind": "component",
                "parent_id": first["id"],
                "start_date": "2026-08-01",
                "initial_distance_km": 0,
                "initial_hours": 0,
            }
        )["equipment"]
        _, changes = self.collected(
            lambda: self.edit(
                part,
                parent_id=second["id"],
                kind="component",
                name="Chain",
            )
        )
        self.edit(first, sport="run", kind="shoes")
        with self.assertRaises(AppError) as raised:
            self.undo(changes)
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(
            self.record("equipment:" + part["id"])["parent_id"], second["id"]
        )

    # Assignments

    def test_assign_records_create_update_and_unassign_each_undoable(self) -> None:
        first, second = self.bike(), self.bike(name="Second")
        self.service.assign({"activity_id": "act_w1", "equipment_id": first["id"]})
        _, moved = self.collected(
            lambda: self.service.assign(
                {"activity_id": "act_w1", "equipment_id": second["id"]}
            )
        )
        self.undo(moved)
        self.assertEqual(
            self.record("equipment_assignment:act_w1")["equipment_id"], first["id"]
        )
        result, unassigned = self.collected(
            lambda: self.service.assign({"activity_id": "act_w1", "equipment_id": None})
        )
        self.assertEqual(result["change"]["entity_type"], "equipment_assignment")
        self.assertIsNone(self.record("equipment_assignment:act_w1")["equipment_id"])
        self.undo(unassigned)
        self.assertEqual(
            self.record("equipment_assignment:act_w1")["equipment_id"], first["id"]
        )
        self.assertEqual(
            self.actions("equipment_assignment"),
            ["create", "update", "undo", "update", "undo"],
        )

    def test_a_save_refuses_non_text_kinds_and_more_than_100_items(self) -> None:
        # The athlete's own route reaches save directly (#2879).
        for field, value in (("status", []), ("kind", {})):
            with self.subTest(field=field), self.assertRaises(AppError) as refused:
                self.service.save({**BIKE, field: value})
            self.assertEqual(refused.exception.status, 400)
        for index in range(100):
            self.bike(name=f"Bike {index}")
        with self.assertRaises(AppError) as full:
            self.bike(name="One more")
        self.assertEqual(full.exception.status, 409)

    def test_a_session_read_offers_its_items_and_suggests_the_last_assigned(
        self,
    ) -> None:
        first, second = self.bike(), self.bike(name="Second")
        choice = self.service.read(activity_id="act_w2")["choice"]
        self.assertEqual(
            (sorted(o["name"] for o in choice["options"]), choice["suggested"]),
            (["Road bike", "Second"], None),
        )
        self.service.assign({"activity_id": "act_w1", "equipment_id": second["id"]})
        # The suggestion is derived and never stored or assigned (#2879).
        choice = self.service.read(activity_id="act_w2")["choice"]
        self.assertEqual(
            (choice["assigned"], choice["explicit"], choice["suggested"]),
            (None, False, second["id"]),
        )
        self.assertIsNone(self.stored("equipment_assignment:act_w2"))
        self.edit(self.record(f"equipment:{second['id']}"), status="archived")
        choice = self.service.read(activity_id="act_w2")["choice"]
        self.assertEqual(
            ([o["id"] for o in choice["options"]], choice["suggested"]),
            ([first["id"]], None),
        )
        self.service.assign({"activity_id": "act_w2", "equipment_id": None})
        own = self.service.read(activity_id="act_w2")["choice"]
        self.assertEqual((own["assigned"], own["explicit"]), (None, True))

    def test_undo_of_a_first_unassignment_removes_the_override(self) -> None:
        _, changes = self.collected(
            lambda: self.service.assign({"activity_id": "act_w1", "equipment_id": None})
        )
        self.assertIsNotNone(self.stored("equipment_assignment:act_w1"))
        self.undo(changes)
        self.assertIsNone(self.stored("equipment_assignment:act_w1"))

    def test_undo_of_a_first_assignment_removes_it(self) -> None:
        item = self.bike()
        _, changes = self.collected(
            lambda: self.service.assign(
                {"activity_id": "act_w1", "equipment_id": item["id"]}
            )
        )
        self.undo(changes)
        self.assertIsNone(self.stored("equipment_assignment:act_w1"))
        self.assertEqual(
            self.service.read()["items"][0]["usage"]["assigned_sessions"], 0
        )

    def test_undo_of_a_deleted_unassignment_restores_the_unset_item(self) -> None:
        # A delete target lacks fields stored as None; the restore must seed them.
        with self.manager.unit_of_work() as db:
            before = {"activity_id": "act_w1", "equipment_id": None}
            db.execute(
                "INSERT INTO kv(key,value,updated_at) VALUES(?,?,?)",
                ("equipment_assignment:act_w1", json.dumps(before), NOW),
            )
            db.execute("DELETE FROM kv WHERE key='equipment_assignment:act_w1'")
            record = change_history.record_change(
                db, "equipment_assignment", "act_w1", "delete", before, None
            )
        assert record is not None
        self.undo([{key: record[key] for key in change_history.UNDO_REFERENCE_FIELDS}])
        restored = self.stored("equipment_assignment:act_w1") or {}
        self.assertEqual(restored["activity_id"], "act_w1")
        self.assertIsNone(restored["equipment_id"])

    def test_undo_of_an_unassignment_is_refused_when_the_gear_is_gone(self) -> None:
        item = self.bike()
        self.service.assign({"activity_id": "act_w1", "equipment_id": item["id"]})
        _, changes = self.collected(
            lambda: self.service.assign({"activity_id": "act_w1", "equipment_id": None})
        )
        with self.manager.unit_of_work() as db:
            db.execute("DELETE FROM kv WHERE key=?", ("equipment:" + item["id"],))
        with self.assertRaises(AppError) as raised:
            self.undo(changes)
        self.assertEqual(raised.exception.status, 409)
        self.assertIsNone(self.record("equipment_assignment:act_w1")["equipment_id"])

    def moved_assignment(self) -> tuple[dict[str, Any], list[dict[str, str]]]:
        """Gear A on the activity, then moved to B; returns A and the move."""
        first, second = self.bike(), self.bike(name="Second")
        self.service.assign({"activity_id": "act_w1", "equipment_id": first["id"]})
        _, moved = self.collected(
            lambda: self.service.assign(
                {"activity_id": "act_w1", "equipment_id": second["id"]}
            )
        )
        return first, moved

    def assert_undo_refused(self, changes: list[dict[str, str]], gear_id: str) -> None:
        with self.assertRaises(AppError) as raised:
            self.undo(changes)
        self.assertEqual(raised.exception.status, 409)
        self.assertNotEqual(
            self.record("equipment_assignment:act_w1")["equipment_id"], gear_id
        )

    def test_undo_of_a_reassignment_is_refused_when_the_old_gear_is_archived(
        self,
    ) -> None:
        first, moved = self.moved_assignment()
        self.edit(first, status="archived")
        self.assert_undo_refused(moved, first["id"])

    def test_undo_of_a_reassignment_is_refused_when_the_sport_changed(self) -> None:
        first, moved = self.moved_assignment()
        self.edit(first, sport="run", kind="shoes")
        self.assert_undo_refused(moved, first["id"])

    def test_undo_of_a_reassignment_is_refused_when_the_old_gear_is_pending(
        self,
    ) -> None:
        first, moved = self.moved_assignment()
        with self.manager.unit_of_work() as db:
            stored = self.record("equipment:" + first["id"])
            stored["sport_pending"] = True
            db.execute(
                "UPDATE kv SET value=? WHERE key=?",
                (json.dumps(stored), "equipment:" + first["id"]),
            )
        self.assert_undo_refused(moved, first["id"])

    # Planned units (#3011)

    def planned_unit(self, local_id: str = "unit-1", **payload: Any) -> dict[str, Any]:
        stored = {"sport": "ride", "environment": "outdoor", "date": "2026-10-05", **payload}
        with self.manager.unit_of_work() as db:
            planning_store.insert_planned_unit(
                db,
                stored,
                local_id=local_id,
                external_id=None,
                sync_dirty=0,
                sync_state="synced",
                sync_error=None,
                sync_conflict="",
                baseline_hash="hash",
                last_synced_at=NOW,
                now=NOW,
            )
            row = planning_store.planned_unit_row(db, local_id)
        return dict(row)

    def test_a_planned_choice_is_tracked_undoable_and_leaves_the_unit_alone(self) -> None:
        before = self.planned_unit()
        first, second = self.bike(), self.bike(name="Rain bike")
        result = self.service.assign({"planned_unit_id": "unit-1", "equipment_id": first["id"]})
        self.assertEqual(result["change"]["entity_type"], "equipment_planned")
        _, changes = self.collected(
            lambda: self.service.assign(
                {"planned_unit_id": "unit-1", "equipment_id": second["id"]}
            )
        )
        self.assertEqual(self.record("equipment_planned:unit-1")["equipment_id"], second["id"])
        self.undo(changes)
        self.assertEqual(self.record("equipment_planned:unit-1")["equipment_id"], first["id"])
        self.assertEqual(self.actions("equipment_planned"), ["create", "update", "undo"])
        # The planned unit, its payload and its sync state stay as they were: nothing reaches a provider.
        with self.manager.reader() as db:
            self.assertEqual(dict(planning_store.planned_unit_row(db, "unit-1")), before)

    def test_undo_of_a_planned_choice_checks_the_unit_and_its_sport_again(self) -> None:
        self.planned_unit()
        first = self.bike()
        self.service.assign({"planned_unit_id": "unit-1", "equipment_id": first["id"]})
        _, changes = self.collected(
            lambda: self.service.assign({"planned_unit_id": "unit-1", "equipment_id": None})
        )
        # The unit became a run since: the bike no longer fits, so undo is refused.
        with self.manager.unit_of_work() as db:
            db.execute(
                "UPDATE planned_units SET payload=json_set(payload, '$.sport', 'run') "
                "WHERE local_id='unit-1'"
            )
        with self.assertRaises(AppError) as sport:
            self.undo(changes)
        self.assertEqual(sport.exception.status, 409)
        with self.manager.unit_of_work() as db:
            db.execute("UPDATE planned_units SET tombstone=1 WHERE local_id='unit-1'")
        with self.assertRaises(AppError) as gone:
            self.undo(changes)
        self.assertEqual(gone.exception.status, 409)
        self.assertIsNone(self.record("equipment_planned:unit-1")["equipment_id"])

    def test_a_planned_unit_reads_its_options_own_choice_rule_and_suggestion(self) -> None:
        self.planned_unit(date="2026-10-05")
        road, rain = self.bike(), self.bike(name="Rain bike")
        self.service.save_rule(
            {"kind": "ride:outdoor", "equipment_id": road["id"], "expected_revision": 0}
        )
        self.service.assign({"activity_id": "act_w1", "equipment_id": rain["id"]})
        choice = self.service.planned_choice("unit-1")
        self.assertEqual({item["id"] for item in choice["options"]}, {road["id"], rain["id"]})
        self.assertEqual(
            (choice["assigned"], choice["explicit"], choice["suggested"]),
            (None, False, rain["id"]),
        )
        self.assertEqual(choice["effective"], {"equipment_id": road["id"], "source": "rule"})
        # The own choice, even "Keiner", replaces the rule.
        self.service.assign({"planned_unit_id": "unit-1", "equipment_id": None})
        choice = self.service.planned_choice("unit-1")
        self.assertEqual((choice["explicit"], choice["effective"]), (True, None))
        for unit_id, status in (("unknown", 404), ("", 400), (None, 400)):
            with self.subTest(unit_id=unit_id), self.assertRaises(AppError) as raised:
                self.service.planned_choice(unit_id)
            self.assertEqual(raised.exception.status, status)

    def test_none_is_a_tracked_planned_choice_and_undo_removes_a_first_choice(self) -> None:
        self.planned_unit()
        _, changes = self.collected(
            lambda: self.service.assign({"planned_unit_id": "unit-1", "equipment_id": None})
        )
        self.assertIsNone(self.record("equipment_planned:unit-1")["equipment_id"])
        self.undo(changes)
        self.assertIsNone(self.stored("equipment_planned:unit-1"))

    def test_a_planned_choice_needs_a_live_unit_and_a_matching_active_item(self) -> None:
        self.planned_unit()
        self.planned_unit("unit-run", sport="run")
        self.planned_unit("unit-gone")
        with self.manager.unit_of_work() as db:
            db.execute("UPDATE planned_units SET tombstone=1 WHERE local_id='unit-gone'")
        bike = self.bike()
        for payload, status in (
            ({"planned_unit_id": "unknown", "equipment_id": bike["id"]}, 404),
            ({"planned_unit_id": "unit-gone", "equipment_id": bike["id"]}, 404),
            ({"planned_unit_id": "unit-run", "equipment_id": bike["id"]}, 400),
            ({"planned_unit_id": "unit-1", "equipment_id": "missing"}, 400),
            ({"planned_unit_id": "", "equipment_id": None}, 400),
            ({"planned_unit_id": 7, "equipment_id": None}, 400),
            ({"planned_unit_id": "unit-1", "activity_id": "act_w1", "equipment_id": None}, 400),
        ):
            with self.subTest(payload=payload), self.assertRaises(AppError) as raised:
                self.service.assign(payload)
            self.assertEqual(raised.exception.status, status)
        self.assertIsNone(self.stored("equipment_planned:unit-1"))
        self.assertEqual(self.actions("equipment_planned"), [])

    # Default rules (#3014)

    def test_a_rule_takes_effect_today_keeps_its_history_and_undoes(self) -> None:
        road, rain = self.bike(), self.bike(name="Rain bike")
        first = self.service.save_rule(
            {"kind": "ride:outdoor", "equipment_id": road["id"], "expected_revision": 0}
        )
        self.assertEqual(first["change"]["entity_type"], "equipment_rule")
        self.assertEqual(
            first["rule"]["history"], [{"from": "2026-10-02", "equipment_id": road["id"]}]
        )
        # A second choice on the same day replaces the first one of that day.
        _, changes = self.collected(
            lambda: self.service.save_rule(
                {"kind": "ride:outdoor", "equipment_id": rain["id"], "expected_revision": 1}
            )
        )
        rule = self.record("equipment_rule:ride:outdoor")
        self.assertEqual((rule["revision"], len(rule["history"])), (2, 1))
        self.assertEqual(rule_choice(rule, "2026-10-02"), (True, rain["id"]))
        self.assertEqual(rule_choice(rule, "2026-10-01"), (False, None))
        self.undo(changes)
        restored = self.record("equipment_rule:ride:outdoor")
        self.assertEqual(rule_choice(restored, "2026-10-02"), (True, road["id"]))
        # Undo bumps the revision, so a client that read the undone rule must read again.
        self.assertEqual(restored["revision"], 3)
        self.assertEqual(self.actions("equipment_rule"), ["create", "update", "undo"])

    def test_a_rule_needs_a_known_kind_a_fresh_revision_and_a_matching_item(self) -> None:
        bike = self.bike()
        self.service.save_rule(
            {"kind": "ride:outdoor", "equipment_id": None, "expected_revision": 0}
        )
        for payload, status in (
            ({"kind": "ride:outdoor", "equipment_id": bike["id"], "expected_revision": 0}, 409),
            ({"kind": "run:easy", "equipment_id": bike["id"], "expected_revision": 0}, 400),
            ({"kind": "swim", "equipment_id": None, "expected_revision": 0}, 400),
            ({"kind": "ride:outdoor", "equipment_id": "missing", "expected_revision": 1}, 400),
            ({"kind": "ride:outdoor", "equipment_id": None, "expected_revision": True}, 400),
            ({"kind": "ride:outdoor", "equipment_id": None}, 400),
        ):
            with self.subTest(payload=payload), self.assertRaises(AppError) as raised:
                self.service.save_rule(payload)
            self.assertEqual(raised.exception.status, status)
        self.assertEqual(self.record("equipment_rule:ride:outdoor")["revision"], 1)

    def test_a_rule_without_an_item_is_no_default_and_cannot_be_applied(self) -> None:
        self.many_rides(2)
        self.service.save_rule(
            {"kind": "ride:outdoor", "equipment_id": None, "expected_revision": 0}
        )
        with self.assertRaises(AppError) as refused:
            self.service.rule_apply_preview("ride:outdoor")
        self.assertEqual(refused.exception.status, 409)

    def test_rule_history_is_bounded_and_the_latest_choice_up_to_a_day_wins(self) -> None:
        rule = {
            "history": [
                {"from": f"2026-{month:02d}-01", "equipment_id": f"item-{month}"}
                for month in (1, 3, 5)
            ]
        }
        self.assertEqual(rule_choice(rule, "2026-04-30"), (True, "item-3"))
        self.assertEqual(rule_choice(rule, "2025-12-31"), (False, None))
        self.assertEqual(rule_choice(None, "2026-04-30"), (False, None))
        self.assertGreaterEqual(RULE_HISTORY_LIMIT, 100)

    def many_rides(self, count: int) -> list[str]:
        rides = cast(
            list[ActivitySummary],
            [{**ride(f"m{index}"), "environment": "outdoor"} for index in range(count)],
        )
        self.summaries = lambda: rides
        self.service = EquipmentService(
            self.manager, self.summaries, lambda: date(2026, 10, 2), lambda: NOW
        )
        return [str(item["id"]) for item in rides]

    def test_a_confirmed_apply_spans_chunks_and_one_undo_takes_back_what_is_still_its(
        self,
    ) -> None:
        sessions = self.many_rides(RULE_APPLY_CHUNK * 2 + 50)
        bike, other = self.bike(), self.bike(name="Rain bike")
        self.service.save_rule(
            {"kind": "ride:outdoor", "equipment_id": bike["id"], "expected_revision": 0}
        )
        # One session already has the athlete's own choice; the apply leaves it alone.
        self.service.assign({"activity_id": sessions[0], "equipment_id": other["id"]})
        preview = self.service.rule_apply_preview("ride:outdoor")
        self.assertEqual(preview["count"], len(sessions) - 1)
        applied, changes = self.collected(
            lambda: self.service.apply_rule({"kind": "ride:outdoor", "confirmed": True})
        )
        self.assertEqual(applied["count"], len(sessions) - 1)
        # One change-history row stands for the whole apply, not one per session.
        self.assertEqual(self.actions("equipment_rule_apply"), ["create"])
        self.assertEqual([change["entity_type"] for change in changes], ["equipment_rule_apply"])
        self.assertEqual(self.record("equipment_assignment:" + sessions[1])["source"], "rule")
        # The athlete changes one applied session by hand; undo keeps that choice.
        self.service.assign({"activity_id": sessions[2], "equipment_id": other["id"]})
        self.undo(changes)
        self.assertEqual(self.record("equipment_assignment:" + sessions[0])["equipment_id"], other["id"])
        self.assertEqual(self.record("equipment_assignment:" + sessions[2])["equipment_id"], other["id"])
        remaining = [key for key in sessions[3:] if self.stored("equipment_assignment:" + key)]
        self.assertEqual(remaining, [])
        # A second undo of the same apply is refused, and nothing changes.
        with self.assertRaises(AppError) as again:
            self.undo(changes)
        self.assertEqual(again.exception.status, 409)
        self.assertEqual(self.record("equipment_assignment:" + sessions[2])["equipment_id"], other["id"])

    def test_an_apply_gives_only_sessions_that_could_take_the_item_and_stops_on_a_retired_one(
        self,
    ) -> None:
        rides = cast(
            list[ActivitySummary],
            [
                {**ride("out"), "environment": "outdoor"},
                {**ride("virt"), "environment": "virtual"},
            ],
        )
        self.summaries = lambda: rides
        self.service = EquipmentService(
            self.manager, self.summaries, lambda: date(2026, 10, 2), lambda: NOW
        )
        road = self.bike(environment="outdoor")
        self.service.save_rule(
            {"kind": "ride:outdoor", "equipment_id": road["id"], "expected_revision": 0}
        )
        # The virtual ride is indoor: the outdoor bike is neither counted nor written there.
        self.assertEqual(self.service.rule_apply_preview("ride:outdoor")["count"], 1)
        self.edit(road, status="archived")
        for call in (
            lambda: self.service.rule_apply_preview("ride:outdoor"),
            lambda: self.service.apply_rule({"kind": "ride:outdoor", "confirmed": True}),
        ):
            with self.subTest(call=call), self.assertRaises(AppError) as retired:
                call()
            self.assertEqual(retired.exception.status, 409)
        self.assertIsNone(self.stored("equipment_assignment:act_out"))

    def test_an_apply_stopped_by_a_retired_item_returns_what_it_wrote_and_its_undo(self) -> None:
        sessions = self.many_rides(RULE_APPLY_CHUNK + 10)
        bike = self.bike()
        self.service.save_rule(
            {"kind": "ride:outdoor", "equipment_id": bike["id"], "expected_revision": 0}
        )
        checks = []
        original = EquipmentService._rule_item

        def retired_after_first_chunk(db: Any, item_id: Any) -> dict[str, Any]:
            checks.append(item_id)
            if len(checks) > 2:  # once for the preview, once for the first chunk
                raise AppError(409, "Die Ausrüstung der Regel ist nicht mehr aktiv.")
            return original(db, item_id)

        with patch.object(EquipmentService, "_rule_item", staticmethod(retired_after_first_chunk)):
            result = self.service.apply_rule({"kind": "ride:outdoor", "confirmed": True})
        self.assertEqual(
            (result["count"], result["stopped"], result["change"]["entity_type"]),
            (RULE_APPLY_CHUNK, "equipment_not_active", "equipment_rule_apply"),
        )
        self.assertIsNone(self.stored("equipment_assignment:" + sessions[-1]))
        self.undo([result["change"]])
        self.assertIsNone(self.stored("equipment_assignment:" + sessions[0]))

    def test_an_apply_needs_confirmation_a_rule_and_no_running_apply_of_its_kind(self) -> None:
        self.many_rides(3)
        bike = self.bike()
        with self.assertRaises(AppError) as missing:
            self.service.rule_apply_preview("ride:outdoor")
        self.assertEqual(missing.exception.status, 409)
        self.service.save_rule(
            {"kind": "ride:outdoor", "equipment_id": bike["id"], "expected_revision": 0}
        )
        for payload in (
            {"kind": "ride:outdoor", "confirmed": False},
            {"kind": "ride:outdoor", "confirmed": "true"},
        ):
            with self.subTest(payload=payload), self.assertRaises(AppError) as refused:
                self.service.apply_rule(payload)
            self.assertEqual(refused.exception.status, 409)
        with self.assertRaises(AppError) as shape:
            self.service.apply_rule({"kind": "swim", "confirmed": True})
        self.assertEqual(shape.exception.status, 400)
        equipment_module._APPLYING.add("ride:outdoor")
        self.addCleanup(equipment_module._APPLYING.discard, "ride:outdoor")
        with self.assertRaises(AppError) as running:
            self.service.apply_rule({"kind": "ride:outdoor", "confirmed": True})
        self.assertEqual(running.exception.status, 409)
        self.assertEqual(self.actions("equipment_rule_apply"), [])

    def test_a_run_takes_its_kind_from_the_planned_unit_it_was_matched_to(self) -> None:
        runs = cast(
            list[ActivitySummary],
            [
                {**ride(name), "id": "act_" + name, "sport": "run", "environment": "outdoor"}
                for name in ("r1", "r2")
            ],
        )
        self.summaries = lambda: runs
        self.service = EquipmentService(
            self.manager, self.summaries, lambda: date(2026, 10, 2), lambda: NOW
        )
        self.planned_unit("unit-vo2", sport="run", session_focus="vo2max")
        with self.manager.unit_of_work() as db:
            for name in ("r1", "r2"):
                db.execute(
                    "INSERT INTO activity_sessions(activity_id, primary_provider, "
                    "primary_provider_activity_id, started_at, created_at, updated_at) "
                    "VALUES (?, 'wahoo', ?, ?, ?, ?)",
                    ("act_" + name, name, NOW, NOW, NOW),
                )
            db.execute(
                "INSERT INTO activity_detail_analysis(activity_id, analysis_version, "
                "input_sha256, payload, computed_at, created_at, updated_at) "
                "VALUES ('act_r1', 3, ?, ?, ?, ?, ?)",
                ("0" * 64, json.dumps({"target_snapshot": {"planned_unit_id": "unit-vo2"}}), NOW, NOW, NOW),
            )
        shoe = self.service.save(
            {**BIKE, "name": "Racer", "sport": "run", "kind": "shoes"}
        )["equipment"]
        for kind, count in (("run:intervals", 1), ("run:easy", 1), ("run:long", 0)):
            self.service.save_rule({"kind": kind, "equipment_id": shoe["id"], "expected_revision": 0})
            with self.subTest(kind=kind):
                self.assertEqual(self.service.rule_apply_preview(kind)["count"], count)

    def test_session_kinds_follow_the_focus_for_runs_and_the_place_for_rides(self) -> None:
        for args, kind in (
            (("run", "outdoor", "vo2max"), "run:intervals"),
            (("run", "outdoor", "tempo"), "run:intervals"),
            (("run", "outdoor", "long"), "run:long"),
            (("run", "outdoor", "recovery"), "run:easy"),
            (("run", "outdoor", None), "run:easy"),
            (("run", "outdoor", "long", "race"), "run:race"),
            (("ride", "virtual", "threshold"), "ride:indoor"),
            (("ride", "indoor", None), "ride:indoor"),
            (("ride", "outdoor", "long"), "ride:outdoor"),
            (("swim", "indoor", None), None),
        ):
            with self.subTest(args=args):
                self.assertEqual(session_kind(*args), kind)

    # Maintenance

    def test_maintenance_is_recorded_and_its_undo_removes_the_entry(self) -> None:
        item = self.bike()
        result, changes = self.collected(
            lambda: self.service.maintain(
                {"equipment_id": item["id"], "date": "2026-10-01", "notes": "Kette"}
            )
        )
        self.assertEqual(result["change"]["entity_type"], "equipment_maintenance")
        key = "equipment_maintenance:" + result["maintenance"]["id"]
        self.assertIsNotNone(self.stored(key))
        self.undo(changes)
        self.assertIsNone(self.stored(key))
        self.assertEqual(self.service.read()["items"][0]["maintenance"], [])

    def test_undo_of_a_removed_maintenance_entry_restores_it(self) -> None:
        item = self.bike()
        saved = self.service.maintain(
            {"equipment_id": item["id"], "date": "2026-10-01", "notes": "Kette"}
        )["maintenance"]
        with self.manager.unit_of_work() as db:
            db.execute(
                "DELETE FROM kv WHERE key=?", ("equipment_maintenance:" + saved["id"],)
            )
            record = change_history.record_change(
                db, "equipment_maintenance", saved["id"], "delete", saved, None
            )
        assert record is not None
        self.undo([{key: record[key] for key in change_history.UNDO_REFERENCE_FIELDS}])
        restored = self.stored("equipment_maintenance:" + saved["id"]) or {}
        self.assertEqual(restored["notes"], "Kette")
        self.assertEqual(restored["date"], "2026-10-01")

    # Coach receipts

    def test_coach_equipment_tools_offer_a_turn_undo(self) -> None:
        for tool in (
            "save_equipment",
            "assign_activity_equipment",
            "log_equipment_maintenance",
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


if __name__ == "__main__":
    unittest.main()

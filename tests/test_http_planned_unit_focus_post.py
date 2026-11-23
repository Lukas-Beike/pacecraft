"""Focused tests for the planned-unit session focus POST route."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

from support import dispatch_route

from backend import change_history
from backend.errors import AppError
from backend.http_api.planned_unit_focus_post import PlannedUnitFocusPostRoutes
from backend.planning.plan_guard_service import PlanCheckError

LOCAL_ID = "12345678-1234-5678-1234-567812345678"


class PlannedUnitFocusPostRoutesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Mock()
        self.service.update.return_value = {"status": "local"}
        self.routes = PlannedUnitFocusPostRoutes(
            lambda: self.service,
            lambda: MagicMock(),
        )

    def handler(self, payload):
        return SimpleNamespace(read_json=Mock(return_value=payload), send_json=Mock())

    def test_sets_only_the_focus_fields(self) -> None:
        handler = self.handler(
            {"session_focus": "legs", "strength_intensity": "heavy", "name": "x"}
        )
        self.assertTrue(
            dispatch_route(
                self.routes,
                handler,
                f"/api/planned-units/{LOCAL_ID}/focus",
                method="POST",
            )
        )
        self.service.update.assert_called_once_with(
            LOCAL_ID, {"session_focus": "legs", "strength_intensity": "heavy"}
        )
        handler.send_json.assert_called_once_with(200, {"status": "local"})

    def test_empty_values_clear_the_focus(self) -> None:
        handler = self.handler({"session_focus": ""})
        dispatch_route(
            self.routes, handler, f"/api/planned-units/{LOCAL_ID}/focus", method="POST"
        )
        self.service.update.assert_called_once_with(
            LOCAL_ID, {"session_focus": "", "strength_intensity": None}
        )

    def test_other_paths_and_bad_bodies(self) -> None:
        other = self.handler({})
        self.assertFalse(
            dispatch_route(self.routes, other, "/api/other", method="POST")
        )
        other.read_json.assert_not_called()
        with self.assertRaises(AppError):
            dispatch_route(
                self.routes,
                self.handler([]),
                f"/api/planned-units/{LOCAL_ID}/focus",
                method="POST",
            )
        self.service.update.assert_not_called()

    def test_date_route_moves_the_unit_and_returns_the_undo_reference(self) -> None:
        change = {"id": "c1", "entity_type": "planned_unit", "after_hash": "h"}

        def move(*_args):
            change_history._collected_changes.get().append(change)
            return {"status": "local", "local_id": LOCAL_ID}

        self.service.move.side_effect = move
        handler = self.handler(
            {"date": "2026-11-14", "expected_payload_hash": "abc", "name": "x"}
        )
        self.assertTrue(
            dispatch_route(
                self.routes,
                handler,
                f"/api/planned-units/{LOCAL_ID}/date",
                method="POST",
            )
        )
        self.service.move.assert_called_once_with(LOCAL_ID, "2026-11-14", "abc")
        handler.send_json.assert_called_once_with(
            200, {"status": "local", "local_id": LOCAL_ID, "change": change}
        )
        with self.assertRaises(AppError):
            dispatch_route(
                self.routes,
                self.handler([]),
                f"/api/planned-units/{LOCAL_ID}/date",
                method="POST",
            )

    def test_delete_route_needs_the_confirmation_and_returns_the_undo_reference(
        self,
    ) -> None:
        # #3027: a calendar delete is destructive, so the server refuses it unconfirmed.
        path = f"/api/planned-units/{LOCAL_ID}/delete"
        for body in (
            {"expected_payload_hash": "abc"},
            {"confirmed": "true", "expected_payload_hash": "abc"},
        ):
            with self.subTest(body=body), self.assertRaises(AppError) as raised:
                dispatch_route(self.routes, self.handler(body), path, method="POST")
            self.assertEqual(
                (409, "confirmation_required"),
                (raised.exception.status, raised.exception.reason),
            )
        changes = Mock()
        routes = PlannedUnitFocusPostRoutes(
            lambda: self.service, lambda: MagicMock(), lambda: changes
        )
        with self.assertRaises(AppError) as raised:
            dispatch_route(
                routes, self.handler({"confirmed": True}), path, method="POST"
            )
        self.assertEqual("planning_revision_conflict", raised.exception.reason)
        changes.apply.assert_not_called()
        change = {"id": "c2", "entity_type": "planned_unit", "after_hash": "h"}

        def apply(_batch):
            change_history._collected_changes.get().append(change)
            return {"changes": [{"local_id": LOCAL_ID, "status": "deleted"}]}

        changes.apply.side_effect = apply
        handler = self.handler({"confirmed": True, "expected_payload_hash": "abc"})
        self.assertTrue(dispatch_route(routes, handler, path, method="POST"))
        # The Coach's change batch runs the delete, so plan bounds and revision follow.
        changes.apply.assert_called_once_with(
            {
                "changes": [
                    {
                        "action": "delete",
                        "local_id": LOCAL_ID,
                        "expected_payload_hash": "abc",
                    }
                ]
            }
        )
        handler.send_json.assert_called_once_with(
            200, {"status": "deleted", "local_id": LOCAL_ID, "change": change}
        )

    def test_garmin_exercise_route_passes_the_pick_to_the_service(self) -> None:
        self.service.assign_garmin_exercise.return_value = {"status": "local"}
        handler = self.handler(
            {
                "exercise_index": 2,
                "garmin_exercise": "V-grip Cable Row",
                "expected_payload_hash": "abc",
                "name": "ignored",
            }
        )
        self.assertTrue(
            dispatch_route(
                self.routes,
                handler,
                f"/api/planned-units/{LOCAL_ID}/garmin-exercise",
                method="POST",
            )
        )
        self.service.assign_garmin_exercise.assert_called_once_with(
            LOCAL_ID, 2, "V-grip Cable Row", "abc"
        )
        handler.send_json.assert_called_once_with(200, {"status": "local"})
        with self.assertRaises(AppError):
            dispatch_route(
                self.routes,
                self.handler([]),
                f"/api/planned-units/{LOCAL_ID}/garmin-exercise",
                method="POST",
            )

    def test_edit_routes_pass_the_form_to_the_edit_service_and_answer_a_blocking_check(
        self,
    ) -> None:
        edit = Mock()
        change = {"id": "c3", "entity_type": "planned_unit", "after_hash": "h"}

        def updated(*_args, **_kwargs):
            change_history._collected_changes.get().append(change)
            return {"status": "local", "local_id": LOCAL_ID, "plan_check_notes": []}

        edit.update.side_effect = updated
        edit.set_archived.return_value = {"status": "local", "plan_check_notes": []}
        routes = PlannedUnitFocusPostRoutes(
            lambda: self.service, lambda: MagicMock(), None, lambda: edit
        )

        handler = self.handler({"name": "y", "expected_payload_hash": "abc"})
        dispatch_route(
            routes, handler, f"/api/planned-units/{LOCAL_ID}/update", method="POST"
        )
        edit.update.assert_called_once_with(LOCAL_ID, {"name": "y"}, "abc", accept=None)
        handler.send_json.assert_called_once_with(
            200,
            {
                "status": "local",
                "local_id": LOCAL_ID,
                "plan_check_notes": [],
                "change": change,
            },
        )

        path = f"/api/planned-units/{LOCAL_ID}/archive"
        with self.assertRaises(AppError) as raised:
            dispatch_route(
                routes,
                self.handler({"expected_payload_hash": "abc"}),
                path,
                method="POST",
            )
        self.assertEqual("confirmation_required", raised.exception.reason)
        dispatch_route(
            routes,
            self.handler({"confirmed": True, "expected_payload_hash": "abc"}),
            path,
            method="POST",
        )
        edit.set_archived.assert_called_once_with(LOCAL_ID, True, "abc")

        edit.update.side_effect = PlanCheckError(
            [{"code": "illness_hard", "message": "Nicht gespeichert."}]
        )
        with self.assertRaises(AppError) as raised:
            dispatch_route(
                routes,
                self.handler({"expected_payload_hash": "abc"}),
                f"/api/planned-units/{LOCAL_ID}/update",
                method="POST",
            )
        self.assertEqual(
            (409, "illness_hard"), (raised.exception.status, raised.exception.reason)
        )


if __name__ == "__main__":
    unittest.main()

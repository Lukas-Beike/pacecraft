"""The training-plan routes create, change, archive and delete plan metadata (#3950)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from support import dispatch_route

from backend import change_history
from backend.errors import AppError
from backend.http_api.training_plan_post import TrainingPlanPostRoutes

PLAN_ID = "12345678-1234-5678-1234-567812345678"


class TrainingPlanPostRoutesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Mock()
        self.routes = TrainingPlanPostRoutes(lambda: self.service)

    @staticmethod
    def handler(payload):
        return SimpleNamespace(read_json=Mock(return_value=payload), send_json=Mock())

    def post(self, path, payload):
        handler = self.handler(payload)
        self.assertTrue(dispatch_route(self.routes, handler, path, method="POST"))
        return handler.send_json.call_args.args

    def test_create_passes_the_form_and_returns_the_undo_reference(self) -> None:
        change = {"id": "c1", "entity_type": "training_plan", "after_hash": "h"}

        def created(_values):
            change_history._collected_changes.get().append(change)
            return {"status": "created", "plan_id": PLAN_ID, "plan": {"id": PLAN_ID}}

        self.service.create.side_effect = created
        form = {"name": "Aufbau", "start_date": "2026-11-30", "end_date": "2027-01-03"}

        status, body = self.post("/api/training-plans", form)

        self.service.create.assert_called_once_with(form)
        self.assertEqual(200, status)
        self.assertEqual(
            ("created", PLAN_ID, change),
            (body["status"], body["plan_id"], body["change"]),
        )
        with self.assertRaises(AppError) as raised:
            self.post("/api/training-plans", {**form, "id": "x"})
        self.assertEqual("Unbekannte Felder: id.", raised.exception.message)

    def test_update_needs_the_confirmation_to_archive(self) -> None:
        path = f"/api/training-plans/{PLAN_ID}/update"
        self.service.update.return_value = {
            "status": "updated",
            "plan_id": PLAN_ID,
            "plan": {},
        }

        # The service reads the status stripped, case-folded and by alias, so the guard does too.
        for spelling in ("archived", "Archiviert", " ARCHIVED "):
            with self.assertRaises(AppError) as raised:
                self.post(path, {"status": spelling})
            self.assertEqual("confirmation_required", raised.exception.reason)
        self.service.update.assert_not_called()

        self.post(path, {"status": "archived", "confirmed": True})
        self.service.update.assert_called_once_with(
            PLAN_ID, {"status": "archived", "action": "update"}
        )
        self.post(path, {"name": "Neu"})
        self.service.update.assert_called_with(
            PLAN_ID, {"name": "Neu", "action": "update"}
        )

    def test_delete_needs_the_confirmation(self) -> None:
        path = f"/api/training-plans/{PLAN_ID}/delete"
        self.service.update.return_value = {
            "status": "deleted",
            "plan_id": PLAN_ID,
            "plan": None,
        }

        for payload in ({}, {"confirmed": "true"}):
            with self.assertRaises(AppError) as raised:
                self.post(path, payload)
            self.assertEqual("confirmation_required", raised.exception.reason)
        self.service.update.assert_not_called()

        status, body = self.post(path, {"confirmed": True})

        self.service.update.assert_called_once_with(PLAN_ID, {"action": "delete"})
        self.assertEqual((200, "deleted"), (status, body["status"]))
        self.assertNotIn("plan", body)


if __name__ == "__main__":
    unittest.main()

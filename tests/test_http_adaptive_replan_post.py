"""The replan routes recalculate and apply the adaptive preview (#4020)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from support import dispatch_route

from backend import change_history
from backend.errors import AppError
from backend.http_api.adaptive_replan_post import AdaptiveReplanPostRoutes
from backend.planning.plan_guard_service import PlanCheckError

ID = "12345678-1234-5678-1234-567812345678"


class AdaptiveReplanPostRoutesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Mock()
        self.routes = AdaptiveReplanPostRoutes(lambda: self.service)

    @staticmethod
    def handler(payload):
        return SimpleNamespace(read_json=Mock(return_value=payload), send_json=Mock())

    def post(self, path, payload):
        handler = self.handler(payload)
        self.assertTrue(dispatch_route(self.routes, handler, path, method="POST"))
        return handler.send_json.call_args.args

    def test_preview_takes_an_empty_body(self) -> None:
        self.service.preview.return_value = {"id": ID, "changes": []}

        self.assertEqual(
            (200, {"id": ID, "changes": []}),
            self.post("/api/planning/replan/preview", {}),
        )
        with self.assertRaises(AppError):
            self.post("/api/planning/replan/preview", {"x": 1})

    def test_apply_needs_the_confirmation_and_returns_one_undo_batch(self) -> None:
        path = "/api/planning/replan/apply"
        with self.assertRaises(AppError) as raised:
            self.post(path, {"adjustment_id": ID})
        self.assertEqual("confirmation_required", raised.exception.reason)
        self.service.apply.assert_not_called()

        def applied(*_args):
            changes = change_history._collected_changes.get()
            changes.append(
                {"id": "c1", "entity_type": "planned_unit", "after_hash": "h1"}
            )
            changes.append(
                {"id": "c2", "entity_type": "planned_unit", "after_hash": "h2"}
            )
            return {"status": "ok", "id": ID, "updated": 2}

        self.service.apply.side_effect = applied

        status, body = self.post(
            path,
            {
                "adjustment_id": ID,
                "confirmed": True,
                "accept_plan_findings": ["illness_hard"],
            },
        )

        self.service.apply.assert_called_once_with(ID, ["illness_hard"])
        self.assertEqual(
            (
                200,
                2,
                [{"id": "c1", "after_hash": "h1"}, {"id": "c2", "after_hash": "h2"}],
            ),
            (status, body["updated"], body["changes"]),
        )

    def test_an_illness_check_answers_for_a_second_tap(self) -> None:
        self.service.apply.side_effect = PlanCheckError(
            [{"code": "illness_hard", "message": "m"}]
        )

        with self.assertRaises(AppError) as raised:
            self.post(
                "/api/planning/replan/apply", {"adjustment_id": ID, "confirmed": True}
            )

        self.assertEqual(
            (409, "illness_hard"), (raised.exception.status, raised.exception.reason)
        )


if __name__ == "__main__":
    unittest.main()

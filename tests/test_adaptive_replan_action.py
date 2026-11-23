"""The athlete's apply of the shown adaptive replan preview (#4020)."""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from unittest.mock import Mock

from backend.errors import AppError
from backend.planning import plan_checks
from backend.planning.adaptive_replan_action import AdaptiveReplanActionService
from backend.planning.plan_guard_service import PlanCheckError

ID = "12345678-1234-5678-1234-567812345678"


class _Manager:
    def __init__(self) -> None:
        self.entered = 0

    @contextmanager
    def unit_of_work(self):
        self.entered += 1
        yield


class AdaptiveReplanActionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = _Manager()
        self.previews = Mock()
        self.previews.latest_preview.return_value = {"id": ID, "status": "preview"}
        self.sync = Mock()
        self.sync.apply_local.return_value = {"status": "applied", "id": ID}
        self.sync.complete.return_value = {"status": "ok", "id": ID, "updated": 2}
        self.guard = Mock()
        self.guard.findings.return_value = []
        self.guard.verify.return_value = [{"code": "load_spike"}]
        self.service = AdaptiveReplanActionService(
            self.manager,
            lambda: self.previews,
            lambda: self.sync,
            lambda: self.guard,
        )

    def test_apply_runs_the_local_apply_under_the_plan_checks_and_never_syncs(
        self,
    ) -> None:
        result = self.service.apply(ID, ["illness_hard"])

        self.sync.apply_local.assert_called_once_with(ID)
        self.guard.verify.assert_called_once_with(
            [], ["illness_hard"], inherited=plan_checks.REQUIRED_CODES
        )
        # The Intervals.icu write of an illness pause stays with the named sync.
        self.sync.complete.assert_called_once_with(
            {"status": "applied", "id": ID}, sync_illness_to_intervals=False
        )
        self.assertEqual(
            ("ok", [{"code": "load_spike"}]),
            (result["status"], result["plan_check_notes"]),
        )

    def test_only_the_latest_shown_preview_can_be_applied(self) -> None:
        for latest in (
            None,
            {"id": "other", "status": "preview"},
            {"id": ID, "status": "applied"},
        ):
            self.previews.latest_preview.return_value = latest
            with self.assertRaises(AppError) as raised:
                self.service.apply(ID)
            self.assertEqual("preview_outdated", raised.exception.reason)
        self.sync.apply_local.assert_not_called()
        self.assertEqual(0, self.manager.entered)

    def test_a_preview_another_device_applied_first_is_outdated_not_an_empty_receipt(
        self,
    ) -> None:
        self.sync.apply_local.return_value = {"status": "already_applied", "id": ID}

        with self.assertRaises(AppError) as raised:
            self.service.apply(ID)

        self.assertEqual(
            (409, "preview_outdated"),
            (raised.exception.status, raised.exception.reason),
        )
        self.sync.complete.assert_not_called()

    def test_a_blocking_plan_check_stops_before_the_illness_sync_result(self) -> None:
        self.guard.verify.side_effect = PlanCheckError(
            [{"code": "illness_hard", "message": "m"}]
        )

        with self.assertRaises(PlanCheckError):
            self.service.apply(ID)

        self.sync.complete.assert_not_called()


if __name__ == "__main__":
    unittest.main()

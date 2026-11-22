import unittest
from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock, Mock, patch

from support import (
    GOLDEN_FIRST,
    golden_history,
    golden_lookup,
    payload_digest,
)

from backend.errors import AppError
from backend.http_api.public_performance import (
    PublicFeedbackStateService,
    PublicPerformanceStateService,
)
from backend.performance.canonical_inputs import (
    EMPTY_PERFORMANCE_INPUTS,
    CanonicalPerformanceInputs,
)
from backend.performance.chart_history import analysis_history
from backend.performance.freshness import GarminSyncStatus
from backend.performance.training_focus import training_focus


class PublicPerformanceStateTests(unittest.TestCase):
    def trend_service(self, today):
        profile_service = Mock(get=Mock(return_value={"timezone": "UTC"}))
        garmin_payload_service = Mock(
            sync_status_in=Mock(return_value=GarminSyncStatus())
        )
        source = MagicMock()
        service = PublicPerformanceStateService(
            garmin_payload_service,
            profile_service,
            Mock(),
            lambda: today,
            source,
        )
        return service, source

    def test_a_longer_period_reads_its_own_history_on_demand(self):
        today = date(2026, 11, 20)
        service, _source = self.trend_service(today)
        for days, activity_days in ((91, 365), (182, 365), (365, 533)):
            with (
                self.subTest(days=days),
                patch(
                    "backend.http_api.public_performance.read_performance_inputs",
                    return_value=CanonicalPerformanceInputs(),
                ) as read,
                patch(
                    "backend.http_api.public_performance.with_read_status",
                    side_effect=lambda _source, _db, inputs: inputs,
                ),
            ):
                result = service.trend_history(days)
            start = today - timedelta(days=days - 1)
            self.assertEqual(
                (result["start"], result["end"], result["days"]),
                (start.isoformat(), today.isoformat(), days),
            )
            self.assertNotIn("previous_year_days", result)
            self.assertEqual(
                {
                    "start",
                    "end",
                    "days",
                    "history",
                    "training_focus",
                    "personal_recovery",
                },
                set(result),
            )
            history = result["history"]
            # The same shape as the base history, over the span, with its own body window.
            self.assertEqual(
                (history["start"], history["days"], len(history["load"]["points"])),
                (start.isoformat(), days, days),
            )
            # 3 Monate is the base read, with both of its windows.
            self.assertEqual(
                list(history["body"]["windows"]),
                ["28d", "91d"] if days == 91 else [f"{days}d"],
            )
            self.assertEqual(
                {
                    name: len(history[name]["days"])
                    for name in (
                        "session_rpe_load",
                        "smoothed_session_load",
                        "local_fitness_fatigue",
                    )
                },
                dict.fromkeys(
                    (
                        "session_rpe_load",
                        "smoothed_session_load",
                        "local_fitness_fatigue",
                    ),
                    days,
                ),
            )
            self.assertEqual(result["training_focus"]["start"], start.isoformat())
            # The sessions reach back to the smoothed curves' seed day.
            self.assertEqual(read.call_args.kwargs["history_days"], days)
            self.assertEqual(read.call_args.kwargs["activity_days"], activity_days)

    def test_another_period_is_refused(self):
        service, _source = self.trend_service(date(2026, 11, 20))
        for days in (None, 14, 84, 90, 364, 366):
            with self.subTest(days=days), self.assertRaises(AppError) as raised:
                service.trend_history(days)
            self.assertEqual(
                (raised.exception.status, str(raised.exception)),
                (400, "Ungültiger Analysezeitraum."),
            )

    def test_performance_state_projects_existing_snapshot_and_garmin_reads(self):
        garmin = GarminSyncStatus.from_state({"synced_at": "2026-09-22T09:00:00Z"})
        profile = {"timezone": "Europe/Berlin"}
        performance = {"available": True, "source": "Intervals.icu"}
        garmin_state = {"available": True, "has_sleep": True}
        garmin_payload_service = Mock()
        garmin_payload_service.sync_status.return_value = garmin
        profile_service = Mock()
        profile_service.get.return_value = profile
        garmin_projection_service = Mock()
        garmin_projection_service.public_state.return_value = garmin_state
        today = date(2026, 9, 23)
        service = PublicPerformanceStateService(
            garmin_payload_service,
            profile_service,
            garmin_projection_service,
            lambda: today,
            lt1_session_records=list,
        )

        with patch(
            "backend.http_api.public_performance.performance_context.current_performance_context",
            return_value=performance,
        ) as project_performance:
            result = service.performance_state()

        load = result["performance"]["history"].pop("session_rpe_load")
        self.assertEqual(
            (load["metric_id"], load["status"], load["totals"]["total_sessions"]),
            ("session_rpe_load", "insufficient_data", 0),
        )
        self.assertEqual(
            (len(load["days"]), load["period"]["end"], load["sessions"]),
            (91, today.isoformat(), []),
        )
        smoothed = result["performance"]["history"].pop("smoothed_session_load")
        self.assertEqual(
            (smoothed["status"], smoothed["reason"], len(smoothed["days"])),
            ("insufficient_data", "no_sessions", 91),
        )
        fitness = result["performance"]["history"].pop("local_fitness_fatigue")
        self.assertEqual(
            (fitness["status"], fitness["reason"], len(fitness["days"])),
            ("insufficient_data", "no_load", 91),
        )
        self.assertEqual(
            result,
            {
                "performance": {
                    **performance,
                    "history": analysis_history(
                        garmin, today, canonical=EMPTY_PERFORMANCE_INPUTS
                    ),
                    "training_focus_periods": {
                        "28d": training_focus(
                            garmin,
                            today,
                            "Europe/Berlin",
                            activities=(),
                            days=28,
                        ),
                        "91d": training_focus(
                            garmin,
                            today,
                            "Europe/Berlin",
                            activities=(),
                            days=91,
                        ),
                    },
                },
                "garmin": garmin_state,
            },
        )
        # Canonical reads (#1416): the context takes the Garmin sync status and
        # no snapshot.
        project_performance.assert_called_once_with(
            garmin,
            profile,
            today,
            canonical=EMPTY_PERFORMANCE_INPUTS,
            session_feedback=None,
            # LT1 pace and power read the session records (#2427).
            lt1_session_records=list,
        )
        garmin_payload_service.sync_status.assert_called_once_with()
        garmin_payload_service.view.assert_not_called()
        profile_service.get.assert_called_once_with()
        garmin_projection_service.public_state.assert_called_once_with(garmin)

    def test_feedback_state_keeps_the_30_checkin_limit_and_existing_contexts(self):
        checkins = [{"checkin_date": "2026-09-23"}]
        local_feedback = {"today": checkins[0], "recent": checkins}
        activity_feedback = {"recent": [{"activity_id": "ride-1"}]}
        checkin_service = Mock()
        checkin_service.list.return_value = checkins
        checkin_service.context.return_value = local_feedback
        activity_feedback_service = Mock()
        activity_feedback_service.context.return_value = activity_feedback
        service = PublicFeedbackStateService(checkin_service, activity_feedback_service)

        result = service.feedback_state()

        self.assertEqual(
            result,
            {
                "checkins": checkins,
                "local_feedback": local_feedback,
                "activity_feedback": activity_feedback,
            },
        )
        checkin_service.list.assert_called_once_with(30)
        checkin_service.context.assert_called_once_with()
        activity_feedback_service.context.assert_called_once_with()

    def test_feedback_state_reuses_checkins_from_composite_state(self):
        checkin_service = Mock()
        checkin_service.context.return_value = {"recent": []}
        activity_feedback_service = Mock()
        activity_feedback_service.context.return_value = {"recent": []}
        checkins = [{"checkin_date": "2026-09-23"}]

        result = PublicFeedbackStateService(
            checkin_service, activity_feedback_service
        ).feedback_state(checkins)

        self.assertIs(result["checkins"], checkins)
        checkin_service.list.assert_not_called()

    def test_from_inputs_projects_the_composite_state_inputs(self):
        garmin_payload_service = Mock()
        garmin_payload_service.sync_status.return_value = GarminSyncStatus()
        profile_service = Mock()
        profile_service.get.return_value = {}
        service = PublicPerformanceStateService(
            garmin_payload_service,
            profile_service,
            Mock(),
            lambda: date(2026, 9, 23),
        )

        with patch(
            "backend.http_api.public_performance.performance_context.current_performance_context",
            return_value={},
        ):
            service.from_inputs(None)

    def test_lt1_history_waits_for_the_background_session_read(self):
        def failing():
            raise RuntimeError("background session curves failed")

        def history(read):
            service = PublicPerformanceStateService(
                Mock(sync_status=Mock(return_value=GarminSyncStatus())),
                Mock(get=Mock(return_value={})),
                Mock(),
                lambda: date(2026, 11, 13),
                lt1_history_records=read,
            )
            with patch(
                "backend.http_api.public_performance.performance_context.current_performance_context",
                return_value={},
            ):
                inputs = CanonicalPerformanceInputs()
                state = service.from_inputs(inputs, first_thresholds=True)
                # Callers that draw no chart (Coach, nutrition) skip the read.
                self.assertNotIn(
                    "first_thresholds",
                    service.from_inputs(inputs)["performance"]["history"],
                )
                return state["performance"]["history"]

        # The state request never reads session curves itself (#2693).
        self.assertEqual(
            {"status": "pending"}, history(lambda: None)["first_thresholds"]
        )
        self.assertEqual({"status": "failed"}, history(failing)["first_thresholds"])
        stale = history(lambda: ((), False))["first_thresholds"]
        self.assertEqual("stale", stale["status"])
        self.assertEqual((12, 12), (len(stale["run"]), len(stale["ride"])))
        self.assertEqual(
            "ok", history(lambda: ((), True))["first_thresholds"]["status"]
        )

    def test_golden_history_of_the_performance_endpoint(self):
        sessions, feedback, loads = golden_history()
        today = GOLDEN_FIRST + timedelta(days=129)
        service = PublicPerformanceStateService(
            Mock(sync_status=Mock(return_value=GarminSyncStatus())),
            Mock(get=Mock(return_value={"timezone": "UTC"})),
            Mock(),
            lambda: today,
            session_feedback=golden_lookup(feedback),
            now=lambda: datetime(2026, 5, 10, 8, tzinfo=UTC),
        )
        inputs = CanonicalPerformanceInputs(
            activities=tuple(sessions),
            local_load_days=loads,
            activities_synced_at="2026-05-05T07:00:00+00:00",
        )

        with patch(
            "backend.http_api.public_performance.performance_context.current_performance_context",
            return_value={},
        ):
            history = service.from_inputs(inputs)["performance"]["history"]

        curves = {
            name: history[name]
            for name in (
                "session_rpe_load",
                "smoothed_session_load",
                "local_fitness_fatigue",
            )
        }
        # 91 days (3 Monate); the first 84 days' own digest (the earlier window) is unchanged.
        self.assertEqual(
            payload_digest(curves),
            "c3ab07e5b301cc62aa4bb0a0eda90f23429d7d391ea63127e6a949ea92a32c4d",
        )


if __name__ == "__main__":
    unittest.main()

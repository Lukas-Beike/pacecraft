"""Focused tests for Coach activity read-tool orchestration."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from activity_row_support import SnapshotActivityRows, canonical_source
from canonical_detail_support import (
    add_session_record,
    open_manager,
    store_garmin_streams,
)
from intervals_wellness_stub import map_intervals_wellness

from backend.activities.activity_reference import ActivityReferences
from backend.activities.detail_contract import COACH_ACTIVITY_DETAIL_MAX_SERIES_POINTS
from backend.canonical.vocabulary import MetricKind, ProviderId
from backend.coach.activity_read_tools import (
    CoachActivityReadToolService,
    resolve_session,
)
from backend.coach.context import coach_context_json_size
from backend.errors import AppError
from backend.observability import SessionReportCounts
from backend.performance.activity_read_service import ActivityAnalysisReadService
from backend.performance.canonical_inputs import CanonicalPerformanceSource
from backend.performance.freshness import NO_GARMIN_SYNC_STATUS
from backend.runtime.ports import ObservationSourceSelector
from backend.sync.canonical_table_reader import CanonicalTableReader

COACH_ACTIVITY_DETAIL_MAX_LAPS = 200
LEGACY_KEYS = {
    "activity",
    "session_analysis",
    "activity_id",
    "local_activity_id",
    "snapshot_synced_at",
}


class CoachActivityReadToolServiceTests(unittest.TestCase):
    def test_report_defaults_to_requested_period_without_unrelated_analysis_reads(self):
        report = Mock()
        report.read.return_value = {"start": "2026-09-01", "totals": {"sessions": 2}}
        records = SimpleNamespace(endurance=Mock(), comparisons=Mock())
        derived = SimpleNamespace(
            impact=Mock(), body_history=Mock(), sleep_timing_variability=Mock()
        )
        reports = SimpleNamespace(
            report=report,
            records=records,
            derived=derived,
            profiles=SimpleNamespace(power_profiles=Mock()),
            season=SimpleNamespace(season=Mock()),
            timezone=lambda: "UTC",
        )
        service = CoachActivityReadToolService(
            Mock(), Mock(), Mock(), date.today, reports
        )
        result = service.execute(
            "get_training_report", {"start": "2026-09-01", "days": 7}
        )
        self.assertEqual(result["report"]["totals"]["sessions"], 2)
        records.endurance.assert_not_called()
        derived.impact.assert_not_called()
        report.read.assert_called_once_with({"start": "2026-09-01", "days": 7}, "UTC")
        self.assertEqual(result["projection"]["requested_sections"], ["report"])

    def test_report_can_select_comparisons_and_rejects_unknown_section(self):
        report = Mock()
        records = SimpleNamespace(comparisons=Mock(), endurance=Mock())
        records.comparisons.return_value = {"status": "insufficient_data", "groups": []}
        reports = SimpleNamespace(
            report=report,
            records=records,
            derived=SimpleNamespace(
                impact=Mock(), body_history=Mock(), sleep_timing_variability=Mock()
            ),
            profiles=SimpleNamespace(power_profiles=Mock()),
            season=SimpleNamespace(season=Mock()),
            timezone=lambda: "UTC",
        )
        service = CoachActivityReadToolService(
            Mock(), Mock(), Mock(), date.today, reports
        )
        result = service.execute("get_training_report", {"sections": ["comparisons"]})
        self.assertEqual(result["comparisons"]["status"], "insufficient_data")
        report.read.assert_not_called()
        with self.assertRaises(AppError):
            service.execute("get_training_report", {"sections": ["unknown"]})

    def test_activity_detail_exposes_the_session_bundle_for_corrections(self):
        read = Mock()
        read.detail.return_value = {
            "session": {"activity_id": "act_1", "sport": "ride"},
            "metrics": {"duration_s": {"value": 6300}},
            "running_dynamics_streams": {"ground_contact_time_ms": {"values": [250]}},
            "training_effect": {
                "aerobic_training_effect": 3.4,
                "training_effect_label": "TEMPO",
                "source": "Garmin Connect",
            },
        }
        corrections = Mock()
        corrections.bundle.return_value = {"activity_id": "act_1", "members": []}
        service = CoachActivityReadToolService(
            read,
            Mock(),
            Mock(),
            date.today,
            session_corrections=corrections,
        )
        result = service.execute("get_activity_details", {"activity_id": "act_1"})
        corrections.bundle.assert_called_once_with("act_1")
        self.assertEqual(result["session_bundle"]["activity_id"], "act_1")
        # #2258: the per-second running dynamics are for the calendar, not the Coach.
        self.assertNotIn("running_dynamics_streams", result)
        # #2831: the calendar's provider-values block repeats the effect metrics.
        self.assertNotIn("training_effect", result)
        # #3038: only its label, which the metrics lack, stays with its source.
        self.assertEqual(
            {"value": "TEMPO", "source": "Garmin Connect"},
            result["training_effect_label"],
        )
        # 105 min falls in the 75-150 min step: 30 to 60 g per hour.
        advice = result["fuelling_advice"]
        self.assertEqual(
            (30, 60),
            (
                advice["carbohydrates_per_hour"]["low_g"],
                advice["carbohydrates_per_hour"]["high_g"],
            ),
        )
        self.assertEqual("Thomas, Erdman und Burke 2016", advice["source"])
        for sport, seconds in (("ride", 3599), ("swim", 7200), ("strength", 7200)):
            read.detail.return_value = {
                "session": {"activity_id": "act_1", "sport": sport},
                "metrics": {"duration_s": {"value": seconds}},
            }
            with self.subTest(sport=sport, seconds=seconds):
                result = service.execute(
                    "get_activity_details", {"activity_id": "act_1"}
                )
                self.assertNotIn("fuelling_advice", result)
                self.assertNotIn("training_effect_label", result)

    def test_report_can_select_body_history_and_sleep_timing_variability(self):
        report = Mock()
        derived = SimpleNamespace(body_history=Mock(), sleep_timing_variability=Mock())
        derived.body_history.return_value = {"weight": []}
        derived.sleep_timing_variability.return_value = {"status": "ok"}
        reports = SimpleNamespace(
            report=report,
            records=SimpleNamespace(endurance=Mock(), comparisons=Mock()),
            derived=derived,
            profiles=SimpleNamespace(power_profiles=Mock()),
            season=SimpleNamespace(season=Mock()),
            timezone=lambda: "UTC",
        )
        service = CoachActivityReadToolService(
            Mock(), Mock(), Mock(), date.today, reports
        )
        result = service.execute(
            "get_training_report",
            {"sections": ["body_history", "sleep_timing_variability"]},
        )
        self.assertEqual(result["body_history"], {"weight": []})
        self.assertEqual(result["sleep_timing_variability"]["status"], "ok")
        report.read.assert_not_called()

    def test_retired_sleep_regularity_section_is_rejected(self):
        service = CoachActivityReadToolService(
            Mock(), Mock(), Mock(), date.today, Mock()
        )
        with self.assertRaises(AppError) as raised:
            service.execute("get_training_report", {"sections": ["sleep_regularity"]})
        self.assertEqual(400, raised.exception.status)

    def test_recent_activities_applies_defaults_bounds_and_today(self):
        activity_read = Mock()
        activity_read.recent.return_value = {"activities": []}
        today = date(2026, 9, 23)
        service = CoachActivityReadToolService(
            activity_read, Mock(), Mock(), lambda: today
        )

        result = service.execute("list_recent_activities", {})

        self.assertEqual(result, {"ok": True, "activities": []})
        activity_read.recent.assert_called_once_with(30, 100, today=today)
        activity_read.reset_mock()
        service.execute("list_recent_activities", {"days": 9999, "limit": 0})
        activity_read.recent.assert_called_once_with(3660, 1, today=today)

    def test_recent_activities_keep_scalars_and_drop_per_field_detail(self):
        summary = {
            "id": "act_g",
            "record_ids": {"garmin": "910"},
            "source": "Garmin Connect",
            "training_load": 40.0,
            "sources": {"training_load": "Garmin Connect"},
            "power_zone_times": [{"id": "Z1", "secs": 60.0}],
            "heart_rate_zone_times": [],
            "pace_zone_times": [],
            # Garmin's unlabelled sweat-loss estimate stays out of the list (#2804).
            "water_estimated_ml": 900.0,
        }
        activity_read = Mock()
        activity_read.recent.return_value = {"activities": [summary]}
        service = CoachActivityReadToolService(
            activity_read, Mock(), Mock(), lambda: date(2026, 9, 23)
        )

        (listed,) = service.execute("list_recent_activities", {})["activities"]

        self.assertEqual(
            {
                "id": "act_g",
                "record_ids": {"garmin": "910"},
                "source": "Garmin Connect",
                "training_load": 40.0,
            },
            listed,
        )

    def test_recent_activities_invalid_values_preserve_app_error_contract(self):
        service = CoachActivityReadToolService(Mock(), Mock(), Mock(), date.today)

        for arguments in ({"days": "bad"}, {"limit": None}):
            with (
                self.subTest(arguments=arguments),
                self.assertRaises(AppError) as caught,
            ):
                service.execute("list_recent_activities", arguments)
            self.assertEqual(caught.exception.status, 400)
            self.assertEqual(
                caught.exception.message,
                "Aktivitätszeitraum oder Limit ist ungültig.",
            )
            self.assertEqual(caught.exception.reason, "invalid_list_request")

    def test_detail_returns_the_v1_document_with_its_envelope(self):
        raw_activity = {
            "id": "synthetic-1",
            "name": "Synthetic tempo",
            "type": "Run",
            "start_date_local": "2026-09-22T07:00:00",
            "average_speed": 3.2,
            "average_heartrate": 166,
            "average_watts": 245,
            "provider_extra": "must not pass",
        }
        other = {**raw_activity, "id": "synthetic-2", "name": "Other run"}
        manager = _sessions(self, raw_activity, other)
        _store_streams(manager, "synthetic-1", {"time": [0, 1], "watts": [200, 250]})
        garmin = Mock(sync_status=Mock(return_value=NO_GARMIN_SYNC_STATUS))
        profile = Mock(get=Mock(return_value={"weight_kg": "70"}))
        service = _detail_tool(manager, [raw_activity, other], garmin, profile)

        result = service.execute("get_activity_details", {"activity_id": "synthetic-1"})

        self.assertTrue(result["ok"])
        self.assertEqual("activity_detail.v1", result["contract"])
        self.assertEqual("Synthetic tempo", result["session"]["name"])
        self.assertEqual([200, 250], result["streams"]["channels"]["power_w"]["values"])
        self.assertEqual(
            "bounded sanitized activity_detail.v1 document of exactly one local "
            "session; providers: Garmin Connect; recorded by: garmin",
            result["data_scope"],
        )
        self.assertTrue(result["activity_validation"]["available"])
        # The validation evidence names the summary by its local session id
        # (the test summaries serve "act_" + the provider id).
        self.assertEqual(
            "act_synthetic-1", result["activity_validation"]["activity"]["activity_id"]
        )
        self.assertEqual(
            {"activity_feedback", "equipment", "activity_validation"},
            {"activity_feedback", "equipment", "activity_validation"} & set(result),
        )
        self.assertFalse(LEGACY_KEYS & set(result))
        text = json.dumps(result)
        self.assertNotIn("provider_extra", text)
        self.assertNotIn("synthetic-2", text)
        self.assertNotIn("Other run", text)
        garmin.sync_status.assert_called_once_with()
        profile.get.assert_called_once_with()

    def _detail_with_plan(self, started: str, planned: _PlannedUnits) -> dict:
        raw_activity = {
            "id": "synthetic-1",
            "name": "Synthetic tempo",
            "type": "Run",
            "start_date_local": started,
        }
        manager = _sessions(self, raw_activity)
        garmin = Mock(sync_status=Mock(return_value=NO_GARMIN_SYNC_STATUS))
        profile = Mock(get=Mock(return_value={}))
        service = _detail_tool(manager, [raw_activity], garmin, profile, planned)
        return service.execute("get_activity_details", {"activity_id": "synthetic-1"})

    def test_detail_of_an_old_session_carries_its_planned_units(self):
        planned = _PlannedUnits("2026-09-12", "2026-09-13", "2026-09-14", "2026-09-16")
        result = self._detail_with_plan("2026-09-13T07:00:00", planned)

        self.assertEqual(
            ["unit-2026-09-12", "unit-2026-09-13", "unit-2026-09-14"],
            [unit["id"] for unit in result["planned_units"]],
        )
        # One read from two days before serves the plan and the strength check.
        self.assertEqual([date(2026, 9, 11)], planned.calls)

    def test_detail_of_the_latest_session_carries_its_planned_unit(self):
        planned = _PlannedUnits("2026-09-22", "2026-09-30")
        result = self._detail_with_plan("2026-09-22T07:00:00", planned)

        self.assertEqual(
            ["unit-2026-09-22"], [u["id"] for u in result["planned_units"]]
        )

    def test_detail_without_planned_units_service_reports_unknown(self):
        result = self._detail_with_plan("2026-09-22T07:00:00", None)
        self.assertIsNone(result["planned_units"])

    def test_detail_carries_the_deterministic_session_report(self):
        # #3084: code judges the checks; the Coach explains session_report.
        result = self._detail_with_plan("2026-09-22T07:00:00", None)
        report = result["session_report"]
        self.assertEqual("session-report-v1", report["method"])
        self.assertEqual(
            {"method", "scope", "session_type", "checks", "notable", "lead", "unknown"},
            set(report),
        )
        notable = [c["id"] for c in report["checks"] if c["verdict"] == "notable"]
        self.assertEqual(sorted(notable), sorted(report["notable"]))

    def test_session_report_sets_the_plan_estimate_against_the_local_load(self):
        # The estimate is TSS-scale (M6); the provider's own load (300 on
        # Garmin's scale) never enters the plan comparison.
        estimator = Mock(
            return_value=SimpleNamespace(
                duration_s=3600,
                load=80,
                band_seconds={"low": 3000, "moderate": 480, "high": 120},
            )
        )
        service = CoachActivityReadToolService(
            Mock(), Mock(), Mock(), Mock(), unit_thresholds=estimator
        )
        unit = {"id": "unit-1"}
        report = service._session_report(
            {
                "focus": {"status": "matched", "planned_unit_id": "unit-1"},
                "metrics": {
                    "moving_duration_s": {"value": 3700},
                    "load": {"value": 300, "model": "garmin_training_load"},
                },
                "analysis": {"execution": {"data_quality": {"heart_rate": "ok"}}},
            },
            {"activity_id": "act_1", "sport": "run"},
            [unit],
            [{"id": "act_1", "training_load": 84}],
        )
        estimator.assert_called_once_with(unit)
        load = next(c for c in report["checks"] if c["id"] == "plan_load")
        self.assertEqual(
            ({"actual": 84, "planned": 80, "difference_pct": 5.0}, "ok"),
            (load["value"], load["verdict"]),
        )

    def test_detail_result_size_does_not_grow_with_the_recording(self):
        raw_activity = {
            "id": "synthetic-1",
            "type": "Ride",
            "start_date_local": "2026-09-22T07:00:00",
        }
        laps = [
            {"type": "work", "start_time": index * 30, "end_time": index * 30 + 30}
            for index in range(COACH_ACTIVITY_DETAIL_MAX_LAPS)
        ]
        sizes = []
        for points in (8_000, 16_000):
            with self.subTest(points=points):
                manager = _sessions(self, raw_activity)
                _store_streams(
                    manager,
                    "synthetic-1",
                    {
                        "time": list(range(points)),
                        "watts": [200 + index % 50 for index in range(points)],
                        "heartrate": [140 + index % 20 for index in range(points)],
                        "cadence": [90] * points,
                        "velocity_smooth": [9.5] * points,
                        "altitude": [100.0] * points,
                        "distance": [index * 9.5 for index in range(points)],
                    },
                    laps=laps,
                )
                service = _detail_tool(
                    manager,
                    [raw_activity],
                    Mock(sync_status=Mock(return_value=NO_GARMIN_SYNC_STATUS)),
                    Mock(get=Mock(return_value={})),
                )
                result = service.execute(
                    "get_activity_details", {"activity_id": "synthetic-1"}
                )
                streams = result["streams"]
                self.assertTrue(result["detail_data"]["display_sampled"])
                self.assertEqual(
                    COACH_ACTIVITY_DETAIL_MAX_SERIES_POINTS, len(streams["time_s"])
                )
                for channel in streams["channels"].values():
                    self.assertLessEqual(
                        len(channel["values"]), COACH_ACTIVITY_DETAIL_MAX_SERIES_POINTS
                    )
                self.assertLessEqual(
                    len(result["laps"]["laps"]), COACH_ACTIVITY_DETAIL_MAX_LAPS
                )
                sizes.append(coach_context_json_size(result))
        # Twice the recording only lengthens the sampled numbers, not the lists.
        self.assertLess(sizes[1], sizes[0] * 1.1)

    def test_unknown_tool_is_unclaimed_and_invalid_detail_ids_keep_error(self):
        activity_read = Mock()
        service = CoachActivityReadToolService(
            activity_read, Mock(), Mock(), date.today
        )
        self.assertIsNone(service.execute("list_workout_library", {}))
        activity_read.detail.side_effect = AppError(
            400,
            "Die Aktivität konnte nicht eindeutig zugeordnet werden.",
            reason="invalid_activity_request",
        )

        with self.assertRaises(AppError) as caught:
            service.execute("get_activity_details", {"activity_id": " "})
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(caught.exception.reason, "invalid_activity_request")

    def test_a_session_reference_resolves_by_sport_and_start_time(self):
        # #3084: date, sport and a start within 30 min; never a guess.
        run = {
            "id": "act_run",
            "sport": "run",
            "started_at_local": "2026-11-14T07:05:00",
        }
        ride = {
            "id": "act_ride",
            "sport": "ride",
            "started_at_local": "2026-11-14T17:30:00",
        }
        late = {
            "id": "act_late",
            "sport": "ride",
            "started_at_local": "2026-11-14T19:00:00",
        }
        commute = {
            "id": "act_commute",
            "sport": "walk",
            "purpose": "commute",
            "started_at_local": "2026-11-14T20:00:00",
        }
        day = [run, ride, late, commute]
        cases = [
            (
                {"sport": "run", "start_time": None, "newest": False},
                "resolved",
                ["act_run"],
            ),
            (
                {"sport": None, "start_time": None, "newest": False},
                "ambiguous",
                ["act_run", "act_ride", "act_late", "act_commute"],
            ),
            (
                {"sport": None, "start_time": "17:05", "newest": False},
                "resolved",
                ["act_ride"],
            ),
            (
                {"sport": "ride", "start_time": None, "newest": False},
                "ambiguous",
                ["act_ride", "act_late"],
            ),
            # 12:00 is more than 30 min from every start: the whole day is offered.
            (
                {"sport": None, "start_time": "12:00", "newest": False},
                "not_found",
                ["act_run", "act_ride", "act_late", "act_commute"],
            ),
            (
                {"sport": "ride", "start_time": None, "newest": True},
                "resolved",
                ["act_late"],
            ),
            # A named sport keeps its sessions on not_found, not the whole day.
            (
                {"sport": "ride", "start_time": "12:00", "newest": False},
                "not_found",
                ["act_ride", "act_late"],
            ),
            # The newest skips a commute when a training session matches.
            (
                {"sport": None, "start_time": None, "newest": True},
                "resolved",
                ["act_late"],
            ),
        ]
        for arguments, status, ids in cases:
            with self.subTest(**arguments):
                found, sessions = resolve_session(day, **arguments)
                self.assertEqual(
                    (status, ids), (found, [row["id"] for row in sessions])
                )

    def test_an_unclear_reference_asks_back_with_candidates_and_no_detail(self):
        activity_read = Mock()
        activity_read.summaries_between.return_value = [
            {
                "id": "act_ride",
                "sport": "ride",
                "name": "Provider text never reaches the Coach",
                "started_at_local": "2026-11-14T17:30:00",
                "moving_duration_s": 3600,
                "distance_m": 30000,
            },
            {
                "id": "act_run",
                "sport": "run",
                "started_at_local": "2026-11-14T07:05:00",
            },
        ]
        counts = SessionReportCounts()
        service = CoachActivityReadToolService(
            activity_read,
            Mock(),
            Mock(),
            lambda: date(2026, 11, 15),
            report_counts=counts,
        )
        result = service.execute(
            "get_activity_details",
            {
                "activity_id": None,
                "date": "2026-11-14",
                "sport": None,
                "start_time": None,
            },
        )
        activity_read.summaries_between.assert_called_once_with(
            date(2026, 11, 14), date(2026, 11, 14)
        )
        activity_read.detail.assert_not_called()
        self.assertEqual("ambiguous", result["status"])
        self.assertEqual(
            [
                {
                    "id": "act_ride",
                    "started_at_local": "2026-11-14T17:30:00",
                    "sport": "ride",
                    "moving_duration_s": 3600,
                    "distance_m": 30000,
                    "session_focus": None,
                },
                {
                    "id": "act_run",
                    "started_at_local": "2026-11-14T07:05:00",
                    "sport": "run",
                    "moving_duration_s": None,
                    "distance_m": None,
                    "session_focus": None,
                },
            ],
            result["candidates"],
        )
        # Newest first; the stored name stays out.
        self.assertNotIn("Provider text", json.dumps(result))
        self.assertEqual({"ambiguous": 1}, counts.summary()["resolutions"])

    def test_an_id_a_unique_reference_and_no_reference_read_one_detail(self):
        activity_read = Mock()
        activity_read.summaries_between.return_value = [
            {
                "id": "act_old",
                "sport": "run",
                "started_at_local": "2026-11-13T07:00:00",
            },
            {
                "id": "act_new",
                "sport": "run",
                "started_at_local": "2026-11-15T06:30:00",
            },
        ]
        activity_read.detail.return_value = {"ok": True}
        counts = SessionReportCounts()
        service = CoachActivityReadToolService(
            activity_read,
            Mock(),
            Mock(),
            lambda: date(2026, 11, 15),
            report_counts=counts,
        )
        empty = {"activity_id": None, "date": None, "sport": None, "start_time": None}
        named = {**empty, "_named_activity_id": "act_button"}
        cases = [
            ({**empty, "activity_id": "act_button"}, "act_button"),
            # #3080: the session the athlete's message names is read, also when
            # the model leaves the ID out or asks for another one.
            (named, "act_button"),
            ({**named, "activity_id": "act_new"}, "act_button"),
            # A day the model names is a reference of its own.
            ({**named, "date": "2026-11-13", "start_time": "07:20"}, "act_old"),
            ({**empty, "date": "2026-11-13", "start_time": "07:20"}, "act_old"),
            (empty, "act_new"),
        ]
        for arguments, expected in cases:
            with self.subTest(arguments=arguments):
                result = service.execute("get_activity_details", arguments)
                self.assertEqual(expected, activity_read.detail.call_args.args[0])
                overridden = arguments.get("activity_id") not in (None, expected)
                self.assertEqual(overridden, "notice" in result)
        # Without a date the newest session of the last 30 days is meant.
        self.assertEqual(
            (date(2026, 10, 16), date(2026, 11, 15)),
            activity_read.summaries_between.call_args.args,
        )
        # A reference whose detail cannot be read is no resolution.
        activity_read.detail.side_effect = AppError(404, "missing", reason="missing")
        with self.assertRaises(AppError):
            service.execute(
                "get_activity_details", {**empty, "activity_id": "act_gone"}
            )
        self.assertEqual(
            {
                "by_id": 1,
                "by_named_id": 1,
                "by_reference": 2,
                "named_override": 1,
                "newest": 1,
            },
            counts.summary()["resolutions"],
        )

    def test_a_malformed_reference_is_rejected(self):
        service = CoachActivityReadToolService(Mock(), Mock(), Mock(), date.today)
        for arguments in (
            {
                "activity_id": None,
                "date": "14.11.2026",
                "sport": None,
                "start_time": None,
            },
            {
                "activity_id": None,
                "date": "2026-11-14",
                "sport": None,
                "start_time": "7 Uhr",
            },
        ):
            with (
                self.subTest(arguments=arguments),
                self.assertRaises(AppError) as caught,
            ):
                service.execute("get_activity_details", arguments)
            self.assertEqual(
                (400, "invalid_activity_request"),
                (caught.exception.status, caught.exception.reason),
            )

    def test_detail_passes_canonical_inputs_resolved_once_per_call(self):
        selector, service = _canonical_activity_tool(
            self, _activity_snapshot([{"id": "2026-09-22", "ctl": 45, "atl": 50}])
        )
        with patch(
            "backend.performance.activity_read_service.current_performance_context",
            return_value={},
        ) as project:
            service.execute("get_activity_details", {"activity_id": "synthetic-1"})
        canonical = project.call_args.kwargs["canonical"]
        self.assertIsNotNone(canonical.recovery)
        selector.reader_for.assert_called_once()
        served_kinds = {
            call.args[1]
            for call in selector.reader_for.return_value.observations.call_args_list
        }
        # Intervals.icu CTL and ATL are never read.
        self.assertNotIn(MetricKind.CTL, served_kinds)
        self.assertNotIn(MetricKind.ATL, served_kinds)

    def test_detail_without_wellness_rows_still_passes_the_canonical_inputs(self):
        selector, service = _canonical_activity_tool(self, _activity_snapshot([]))
        with patch(
            "backend.performance.activity_read_service.current_performance_context",
            return_value={},
        ) as project:
            service.execute("get_activity_details", {"activity_id": "synthetic-1"})
        canonical = project.call_args.kwargs["canonical"]
        self.assertEqual(len(canonical.activities or ()), 1)
        # The wellness observations are read to find the days, once per call.
        selector.reader_for.assert_called_once()


def _activity_snapshot(recent_wellness: list[dict]) -> dict:
    raw_activity = {
        "id": "synthetic-1",
        "type": "Run",
        "start_date_local": "2026-09-22T07:00:00",
        "moving_time": 3600,
    }
    return {
        "synced_at": "synthetic-sync",
        "recent_wellness": recent_wellness,
        "recent_activities": [{"id": "synthetic-1", "type": "Run"}],
        "raw_provider_data": {"activities": [raw_activity]},
    }


def _sessions(case: unittest.TestCase, *activities: dict):
    """Return a temporary database holding the canonical sessions of ``activities``."""
    directory = tempfile.TemporaryDirectory()
    case.addCleanup(directory.cleanup)
    manager = open_manager(directory.name)
    case.addCleanup(manager.close)
    for activity in activities:
        add_session_record(
            manager,
            ProviderId.GARMIN,
            activity["id"],
            f"act_{activity['id']}",
            sport=activity["type"].lower(),
            name=activity.get("name"),
            started_at=f"{activity['start_date_local'][:10]}T05:00:00+00:00",
        )
    return manager


def _store_streams(manager, activity_id: str, streams: dict, laps=None) -> None:
    """Store the streams on the Garmin record: Intervals.icu detail is never read."""
    store_garmin_streams(manager, activity_id, streams, laps)


def _detail_tool(manager, activities: list[dict], garmin, profile, planned=None):
    """Wire the Coach tool over the canonical detail read of ``manager``."""
    snapshot_repository = Mock()
    snapshot_repository.latest_snapshot.return_value = {
        "synced_at": "synthetic-sync",
        "recent_activities": [],
        "raw_provider_data": {"activities": activities},
    }
    feedback = Mock()
    feedback.list.return_value = []
    feedback.for_activity_ids.return_value = []
    activity_read = ActivityAnalysisReadService(
        manager,
        SnapshotActivityRows(snapshot_repository),
        feedback,
        canonical=canonical_source(
            SnapshotActivityRows(snapshot_repository), lambda: manager
        ),
        references=ActivityReferences(manager),
        session_records=CanonicalTableReader().session_activities,
    )
    return CoachActivityReadToolService(
        activity_read,
        garmin,
        profile,
        lambda: date(2026, 9, 23),
        planned_units=planned,
    )


class _PlannedUnits:
    """Local planned units that honor ``since`` like PlannedUnitService.list."""

    def __init__(self, *days: str) -> None:
        self.units = [{"id": f"unit-{day}", "date": f"{day}T00:00:00"} for day in days]
        self.calls: list[date | None] = []

    def list(self, limit, *, future_only=False, since=None):
        self.calls.append(since)
        lower = since or date(2026, 9, 23)
        return [u for u in self.units if date.fromisoformat(u["date"][:10]) >= lower]


def _canonical_activity_tool(case: unittest.TestCase, snapshot: dict):
    """Wire the coach tool over the real activity service with a selecting source."""
    sessions = _sessions(case, *snapshot["raw_provider_data"]["activities"])
    inner = Mock()
    observations = map_intervals_wellness(
        snapshot["recent_wellness"], retrieved_at=datetime(2026, 9, 23, tzinfo=UTC)
    ).records
    inner.observations.side_effect = lambda _db, kind, **_bounds: tuple(
        item for item in observations if item.kind is kind
    )
    selector = Mock(spec=ObservationSourceSelector)
    selector.reader_for.return_value = inner
    snapshot_reader = Mock()
    snapshot_reader.latest_snapshot.return_value = snapshot
    feedback = Mock()
    feedback.list.return_value = []
    feedback.for_activity_ids.return_value = []
    manager = MagicMock()
    activity_read = ActivityAnalysisReadService(
        sessions,
        SnapshotActivityRows(snapshot_reader),
        feedback,
        canonical=CanonicalPerformanceSource(
            database_manager=lambda: manager,
            observation_reader=lambda: selector,
            activity_summaries=lambda: SnapshotActivityRows(snapshot_reader),
        ),
        references=ActivityReferences(sessions),
        session_records=CanonicalTableReader().session_activities,
    )
    garmin = Mock(sync_status=Mock(return_value=NO_GARMIN_SYNC_STATUS))
    profile = Mock(get=Mock(return_value={}))
    service = CoachActivityReadToolService(
        activity_read, garmin, profile, lambda: date(2026, 9, 23)
    )
    return selector, service


if __name__ == "__main__":
    unittest.main()

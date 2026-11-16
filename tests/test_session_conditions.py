"""Grade-adjusted pace and RPE against intensity in the session report (#3084 Part 3)."""

from __future__ import annotations

import unittest
from datetime import date
from typing import Any
from unittest.mock import Mock, patch

from backend.canonical.vocabulary import MetricKind
from backend.performance.activity_read_service import ActivityAnalysisReadService
from backend.performance.grade_adjustment import grade_adjusted
from backend.performance.session_conditions import (
    INPUTS,
    MIN_RATED,
    body_headwind,
    expected_rpe,
    grade_adjusted_pace,
)
from backend.performance.session_report import session_report
from backend.sync.local_load import with_local_loads

SPEED = 3.0
UPHILL_PCT = 10.0


def _run(
    *,
    seconds: int = 1200,
    grade: float | None = UPHILL_PCT,
    environment: str = "outdoor",
    sport: str = "run",
    laps: bool = True,
    **extra: Any,
) -> dict[str, Any]:
    """A run at 3 m/s: flat for the first half, ``grade`` % uphill for the second."""
    half = seconds // 2
    times = list(range(seconds + 1))
    grades = [0.0 if t < half else grade for t in times]
    channels: dict[str, Any] = {
        "speed_mps": {"values": [SPEED] * len(times)},
        "distance_m": {"values": [SPEED * t for t in times]},
    }
    if grade is not None:
        channels["grade_pct"] = {"values": grades}
    result: dict[str, Any] = {
        "session": {
            "activity_id": "run-1",
            "sport": sport,
            "environment": environment,
        },
        "streams": {"time_s": times, "channels": channels},
        "laps": {
            "on_stream_clock": laps,
            "laps": [
                {"start_offset_s": 0, "end_offset_s": half},
                {"start_offset_s": half, "end_offset_s": seconds},
            ],
        },
        "analysis": {
            "execution": {"data_quality": {"speed": "ok", "heart_rate": "ok"}}
        },
    }
    result.update(extra)
    return result


def _rated(intensity: float, rpe: float, count: int, **item: Any) -> list[dict]:
    return [
        {
            "id": f"r{index}",
            "sport": "run",
            "intensity": intensity,
            "rpe": rpe,
            "rpe_scale": "cr10",
            **item,
        }
        for index in range(count)
    ]


def _with_rpe(result: dict, rpe: float, intensity: float, rated: list) -> dict:
    return {
        **result,
        "session_rpe_load": {"rpe": rpe, "rpe_scale": "cr10"},
        INPUTS: {"intensity": intensity, "rated_sessions": rated},
    }


def _check(report: dict, key: str) -> dict | None:
    return next((check for check in report["checks"] if check["id"] == key), None)


class GradeAdjustedPaceTests(unittest.TestCase):
    def test_minetti_known_answer_per_lap_and_overall(self) -> None:
        # Minetti 2002 at +10 %: 5.968 / 3.6 J/kg/m, so 3 m/s climbs like 4.974 m/s flat.
        uphill = grade_adjusted(SPEED, UPHILL_PCT)
        self.assertAlmostEqual(4.9735, uphill, places=3)

        result = _run()
        result[INPUTS] = {"grade_adjusted_speed_mps": 3.5}
        gap = grade_adjusted_pace(result)

        assert gap is not None
        self.assertEqual(333.3, gap["pace_s_per_km"])
        # By hand: (3 + 4.97351) / 2 = 3.98676 m/s, 250.8 s/km; 3 / 3.98676 = 0.7525.
        self.assertEqual(250.8, gap["gap_s_per_km"])
        self.assertEqual(24.8, gap["difference_pct"])
        self.assertEqual(100.0, gap["coverage_pct"])
        flat, climb = gap["laps"]
        self.assertEqual(
            (1, 333.3, 333.3),
            (flat["lap"], flat["pace_s_per_km"], flat["gap_s_per_km"]),
        )
        # 1000 / 4.97351 m/s = 201.1 s/km on the climb.
        self.assertEqual((2, 201.1), (climb["lap"], climb["gap_s_per_km"]))
        # The device's value is only a labelled comparison.
        self.assertEqual(285.7, gap["device_gap_s_per_km"])
        self.assertTrue(gap["estimate"])
        self.assertIn("Minetti", gap["method"])

    def test_stops_and_walking_are_left_out(self) -> None:
        result = _run(grade=0.0, laps=False)
        times = result["streams"]["time_s"]
        # A 60 s gap in the recording and a walking stretch below 1 m/s.
        result["streams"]["time_s"] = times[:300] + [t + 60 for t in times[300:]]
        speeds = result["streams"]["channels"]["speed_mps"]["values"]
        speeds[400:500] = [0.5] * 100

        gap = grade_adjusted_pace(result)

        assert gap is not None
        self.assertEqual(1200 - 1 - 100, gap["moving_s"])
        self.assertEqual(gap["pace_s_per_km"], gap["gap_s_per_km"])
        self.assertEqual([], gap["laps"])

    def test_no_value_for_indoor_runs_rides_or_missing_grade(self) -> None:
        self.assertIsNone(grade_adjusted_pace(_run(environment="indoor")))
        self.assertIsNone(grade_adjusted_pace(_run(sport="ride")))
        self.assertIsNone(grade_adjusted_pace(_run(grade=None)))


class ExpectedRpeTests(unittest.TestCase):
    def test_median_of_own_sessions_within_the_intensity_window(self) -> None:
        rated = [
            *_rated(80.0, 5, 3),
            *_rated(85.0, 7, 2),  # the window's edge counts
            *_rated(85.5, 9, 4),  # outside
            *_rated(80.0, 9, 3, sport="ride"),
            *_rated(80.0, 9, 3, rpe_scale="garmin"),
        ]
        self.assertEqual((5.0, 5), expected_rpe(80.0, "run", "cr10", rated))

    def test_fewer_than_five_own_sessions_leave_it_unknown(self) -> None:
        self.assertEqual(
            (None, MIN_RATED - 1),
            expected_rpe(80.0, "run", "cr10", _rated(80.0, 5, MIN_RATED - 1)),
        )


class ConditionChecksInTheReportTests(unittest.TestCase):
    def test_rpe_two_points_above_the_own_median_is_notable(self) -> None:
        report = session_report(_with_rpe(_run(), 7, 80.0, _rated(80.0, 5, 5)))

        check = _check(report, "rpe_intensity")
        assert check is not None
        self.assertEqual("notable", check["verdict"])
        self.assertEqual("personal", check["basis"])
        self.assertEqual(
            {"rpe": 7.0, "expected_rpe": 5.0, "difference": 2.0, "rated_sessions": 5},
            {
                key: check["value"][key]
                for key in ("rpe", "expected_rpe", "difference", "rated_sessions")
            },
        )
        self.assertIn("rpe_intensity", report["notable"])

    def test_a_smaller_gap_is_ok_and_too_few_sessions_unknown(self) -> None:
        close = session_report(_with_rpe(_run(), 6.5, 80.0, _rated(80.0, 5, 5)))
        self.assertEqual("ok", _check(close, "rpe_intensity")["verdict"])  # type: ignore[index]
        few = session_report(_with_rpe(_run(), 9, 80.0, _rated(80.0, 5, 4)))
        unknown = _check(few, "rpe_intensity")
        assert unknown is not None
        self.assertEqual("unknown", unknown["verdict"])
        self.assertIn("4 von 5", unknown["reason"])
        unrated = session_report({**_run(), INPUTS: {"intensity": 80.0}})
        self.assertEqual(
            "Keine RPE-Bewertung der Einheit.",
            _check(unrated, "rpe_intensity")["reason"],  # type: ignore[index]
        )

    def test_gap_explains_and_never_decides(self) -> None:
        report = session_report(_run())

        check = _check(report, "grade_adjusted_pace")
        assert check is not None
        self.assertEqual(("ok", 0), (check["verdict"], check["severity"]))
        self.assertEqual("s/km", check["unit"])
        self.assertNotIn("grade_adjusted_pace", report["notable"])
        indoor = session_report(_run(environment="indoor"))
        self.assertIsNone(_check(indoor, "grade_adjusted_pace"))
        flat_track = session_report(_run(grade=None))
        unknown = _check(flat_track, "grade_adjusted_pace")
        assert unknown is not None
        self.assertEqual("unknown", unknown["verdict"])

    def test_gap_is_unknown_when_the_speed_fails_its_quality_rule(self) -> None:
        result = _run()
        result["analysis"]["execution"]["data_quality"]["speed"] = "gps_jumps"

        check = _check(session_report(result), "grade_adjusted_pace")

        assert check is not None
        self.assertEqual("unknown", check["verdict"])
        self.assertIsNotNone(check["value"])


def _windy(first_mps: float, second_mps: float, coverage: float = 90.0) -> dict:
    """A 50 min run whose main part (600 s to 3000 s) slows by 5 % in the second half.

    The stored 10 m component is ``first_mps`` up to 1800 s, then ``second_mps``.
    """
    result = _run(seconds=3000)
    result["analysis"]["execution"]["halves"] = {
        "status": "ok",
        "start_s": 600,
        "main_part_s": 2400,
        "change_pct": -5.0,
    }
    result[INPUTS] = {
        "headwind": {
            "mean_mps": (first_mps + second_mps) / 2,
            "coverage_pct": coverage,
            "bin_s": 60,
            "bins": [first_mps] * 30 + [second_mps] * 20,
            "head_share_pct": 60.0,
            "tail_share_pct": 10.0,
        }
    }
    return result


class WindTests(unittest.TestCase):
    """Wind at body height explains a slower part, never decides (#3084 Part 3b)."""

    def test_body_height_known_answer_over_windows(self) -> None:
        # ln(2 / 0.03) / ln(10 / 0.03) = 4.1997 / 5.8091 = 0.7229.
        wind = {"mean_mps": 3.0, "bin_s": 60, "bins": [2.0] * 30 + [-4.0] * 30}
        self.assertEqual(2.17, body_headwind(wind))
        self.assertEqual(1.45, body_headwind(wind, (0, 1800)))
        self.assertEqual(-2.89, body_headwind(wind, (1800, 3600)))
        # Half a minute each side of the change: (2 x 30 - 4 x 30) / 60 = -1.
        self.assertEqual(-0.72, body_headwind(wind, (1770, 1830)))

    def test_wind_explains_a_slower_second_half_with_a_second_signal(self) -> None:
        # First half 600-1800 s at 2 m/s (body 1.45), second 1800-3000 s at
        # 8 m/s (body 5.78): 3 m/s or more, and 4.3 m/s more than before.
        report = session_report(_windy(2.0, 8.0))

        wind = _check(report, "wind")
        assert wind is not None
        self.assertEqual(("ok", 0), (wind["verdict"], wind["severity"]))
        self.assertEqual(
            [{"check": "halves", "part_mps": 5.78, "compared_mps": 1.45}],
            wind["value"]["explains"],
        )
        self.assertNotIn("wind", report["notable"])
        self.assertNotIn("wind", report["lead"])

    def test_too_little_extra_headwind_explains_nothing(self) -> None:
        # Second half 4.5 m/s (body 3.25) is 3 m/s or more, but only 1.8 m/s more.
        wind = _check(session_report(_windy(2.0, 4.5)), "wind")
        assert wind is not None
        self.assertEqual([], wind["value"]["explains"])

    def test_low_coverage_or_no_track_leaves_the_wind_unknown(self) -> None:
        low = _check(session_report(_windy(2.0, 8.0, coverage=40.0)), "wind")
        assert low is not None
        self.assertEqual("unknown", low["verdict"])
        self.assertIn("40 %", low["reason"])
        none = _check(session_report(_run()), "wind")
        assert none is not None
        self.assertEqual("unknown", none["verdict"])
        self.assertIsNone(_check(session_report(_run(environment="indoor")), "wind"))


class RatedSessionsReadTests(unittest.TestCase):
    def test_the_window_reads_the_stored_intensity_scale(self) -> None:
        # The local IF is stored in % (M6: IF x 100), and with_local_loads puts
        # that value on the summary. In 2.1.24 a window of 0.05 met it, so no
        # rated session ever counted (#3084).
        self.assertEqual("%", MetricKind.INTENSITY_FACTOR.unit)
        stored = {
            f"s{n}": {MetricKind.INTENSITY_FACTOR.value: {"value": value}}
            for n, value in enumerate((81.0, 82.0, 83.0, 84.0, 85.0, 90.5))
        }
        summaries = with_local_loads(
            [
                {"id": key, "sport": "run", "elapsed_duration_s": 3600, "rpe": 6}
                for key in stored
            ],
            stored,
        )
        service = ActivityAnalysisReadService.__new__(ActivityAnalysisReadService)
        service._feedback_service = Mock(for_activity_ids=Mock(return_value=[]))
        with patch.object(service, "summaries_between", return_value=summaries):
            rated = service.rated_sessions(date(2026, 8, 1), date(2026, 9, 20))

        scale = rated[0]["rpe_scale"]
        # IF 0.85 is 85.0 here: 81.0 to 85.0 lie within 5 points, 90.5 does not.
        self.assertEqual((6.0, 5), expected_rpe(85.0, "run", scale, rated))

    def test_one_summary_snapshot_and_one_feedback_read_for_the_window(self) -> None:
        service = ActivityAnalysisReadService.__new__(ActivityAnalysisReadService)
        feedback = Mock()
        feedback.for_activity_ids.return_value = [
            {"activity_id": "a", "session_rpe": 6}
        ]
        service._feedback_service = feedback
        summaries = [
            {"id": "a", "sport": "run", "intensity": 80.0, "elapsed_duration_s": 3600},
            # A provider rating counts with its own scale.
            {
                "id": "b",
                "sport": "ride",
                "intensity": 70.0,
                "rpe": 4,
                "elapsed_duration_s": 3600,
            },
            {"id": "c", "sport": "run", "intensity": 90.0, "elapsed_duration_s": 3600},
            {"id": "d", "sport": "run", "rpe": 5, "elapsed_duration_s": 3600},
        ]
        with patch.object(
            service, "summaries_between", return_value=summaries
        ) as between:
            rated = service.rated_sessions(date(2026, 8, 1), date(2026, 9, 20))

        between.assert_called_once_with(date(2026, 8, 1), date(2026, 9, 20))
        feedback.for_activity_ids.assert_called_once_with(
            ["a", "b", "c", "d"], sessions_only=True
        )
        self.assertEqual(["a", "b"], [item["id"] for item in rated])
        self.assertEqual((6.0, 80.0), (rated[0]["rpe"], rated[0]["intensity"]))
        self.assertNotEqual(rated[0]["rpe_scale"], rated[1]["rpe_scale"])


if __name__ == "__main__":
    unittest.main()

"""One session's execution against comparable earlier sessions."""

from __future__ import annotations

import unittest
from datetime import date
from typing import Any

from session_stream_support import synthetic_session

from backend.performance.comparable_sessions import (
    comparable_sessions,
    equal_effort_trend,
    progress,
    session_types,
)
from backend.performance.session_execution import (
    comparison_metrics,
    session_execution,
)


def _easy(
    output: int, heart: int, drift: int = 3, seconds: int = 3000
) -> dict[str, Any]:
    return {
        "structure": "continuous",
        "output_unit": "s/km",
        "main_part_s": seconds,
        "output": output,
        "heart_rate_avg": heart,
        "heart_rate_change_bpm": drift,
        "cadence": 170,
    }


def _row(day: str, summary: dict[str, Any] | None, **fields: Any) -> dict[str, Any]:
    return {
        "fingerprint": None if summary is None else f"curve-{day}",
        "activity_id": f"act_{day}",
        "date": day,
        "sport": fields.get("sport", "run"),
        "environment": fields.get("environment", "outdoor"),
        "name": "Easy",
        "weather_temp_c": fields.get("temp", 12.0),
        "execution_summary": summary,
    }


class ComparableSessionsTests(unittest.TestCase):
    def test_easy_runs_compare_with_the_median_of_their_peers(self) -> None:
        records = [
            _row("2026-11-03", _easy(330, 140, drift=2), temp=8.0),
            _row("2026-10-27", _easy(340, 145)),
            _row("2026-10-20", _easy(345, 146)),
            _row("2026-10-13", _easy(350, 148)),
            _row("2026-10-06", _easy(352, 150), temp=24.0),
        ]
        result = comparable_sessions(records, "act_2026-11-03")
        self.assertEqual(result["status"], "ok")
        # The 150 bpm run lies outside the 8 bpm band around 140 bpm.
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["median_of_peers"]["output"], 345)
        self.assertEqual(result["difference"]["output"], -15)
        self.assertEqual(result["difference"]["heart_rate_avg"], -6)
        self.assertEqual(result["this"]["weather_temp_c"], 8.0)
        self.assertEqual(result["peers"][0]["date"], "2026-10-27")

    def test_the_upper_quartile_of_the_peers_backs_personal_limits(self) -> None:
        # #3084: decoupling of 2, 3, 4, 5 and 6 % in five peers: median 4 %,
        # upper quartile (inclusive) 5 %.
        own = {**_easy(330, 140), "decoupling_pct": 6.5}
        records = [_row("2026-11-03", own)] + [
            _row(day, {**_easy(340, 142), "decoupling_pct": value})
            for day, value in (
                ("2026-10-28", 2.0),
                ("2026-10-24", 3.0),
                ("2026-10-20", 4.0),
                ("2026-10-16", 5.0),
                ("2026-10-12", 6.0),
            )
        ]
        result = comparable_sessions(records, "act_2026-11-03")
        self.assertEqual(
            (4.0, 5.0, 5),
            (
                result["median_of_peers"]["decoupling_pct"],
                result["upper_quartile_of_peers"]["decoupling_pct"],
                result["median_peers"]["decoupling_pct"],
            ),
        )

    def test_the_curve_record_compares_the_stored_decoupling(self) -> None:
        # #3084: the summary's stored decoupling (M8) joins the execution's
        # comparison numbers; without a curve or a value nothing is added.
        from backend.performance.mean_max_curve import SessionCurve
        from backend.performance.report_service import _curve_record

        summary = {
            "id": "act_1",
            "sport": "run",
            "started_at": "2026-11-03T07:00:00+00:00",
            "decoupling_pct": 4.2,
        }
        curve = SessionCurve(
            power={},
            running={},
            fingerprint="f",
            pace_at_hr={},
            power_at_hr={},
            execution_summary=_easy(330, 140),
        )
        record = _curve_record(summary, {"act_1": curve}, {})
        self.assertEqual(4.2, record["execution_summary"]["decoupling_pct"])
        self.assertNotIn("decoupling_pct", curve.execution_summary or {})
        bare = _curve_record({**summary, "decoupling_pct": None}, {"act_1": curve}, {})
        self.assertNotIn("decoupling_pct", bare["execution_summary"])

    def test_too_few_peers_still_name_the_sessions_own_numbers(self) -> None:
        # #3084: the report states a run's steadiness even without peers.
        own = {**_easy(330, 140), "gap_cv_pct": 6.1}
        records = [_row("2026-11-03", own), _row("2026-10-28", _easy(340, 142))]
        result = comparable_sessions(records, "act_2026-11-03")
        self.assertEqual(
            ("not_covered", 6.1), (result["status"], result["this"]["gap_cv_pct"])
        )

    def test_interval_rides_compare_heart_rate_at_the_same_power(self) -> None:
        # #3084 ERG: peers at 245 and 255 W are within 5 % of 250 W, 280 W is
        # not; their closing heart rates 150 and 154 have the median 152.
        def ride(power: float, heart: float) -> dict[str, Any]:
            return {
                "structure": "interval",
                "output_unit": "W",
                "rep_count": 5,
                "median_rep_s": 300,
                "rep_output_mean": power,
                "heart_rate_end_mean": heart,
            }

        records = [_row("2026-11-03", ride(250, 158), sport="ride")] + [
            _row(day, ride(power, heart), sport="ride")
            for day, power, heart in (
                ("2026-10-30", 245, 150),
                ("2026-10-26", 255, 154),
                ("2026-10-22", 280, 170),
            )
        ]
        result = comparable_sessions(records, "act_2026-11-03")
        self.assertEqual(
            {"count": 2, "heart_rate_end_mean": 152.0}, result["at_same_output"]
        )

    def test_other_shapes_places_and_old_sessions_are_not_peers(self) -> None:
        records = [
            _row("2026-11-03", _easy(330, 140)),
            _row("2026-10-30", _easy(340, 145), environment="indoor"),
            _row("2026-10-29", _easy(340, 145), sport="ride"),
            _row("2026-10-28", _easy(340, 145, seconds=6000)),
            _row("2026-07-01", _easy(340, 145)),
            _row("2026-10-27", _easy(340, 145)),
            _row("2026-11-05", _easy(340, 145)),
        ]
        result = comparable_sessions(records, "act_2026-11-03")
        self.assertEqual(result["status"], "not_covered")
        self.assertEqual(result["count"], 1)

    def test_a_tempo_run_is_no_peer_of_easy_runs_and_doubtful_data_stays_out(
        self,
    ) -> None:
        doubtful = _easy(340, 165)
        doubtful["data_quality_ok"] = False
        records = [
            _row("2026-11-03", _easy(270, 165)),
            _row("2026-10-27", _easy(330, 140)),
            _row("2026-10-20", _easy(335, 142)),
            _row("2026-10-13", _easy(332, 141)),
            _row("2026-10-06", doubtful),
        ]
        result = comparable_sessions(records, "act_2026-11-03")
        self.assertEqual(result["status"], "not_covered")
        self.assertEqual(result["count"], 0)

    def test_interval_sessions_need_similar_reps(self) -> None:
        def reps(count: int, seconds: int, output: int) -> dict[str, Any]:
            return {
                "structure": "interval",
                "output_unit": "s/km",
                "rep_count": count,
                "median_rep_s": seconds,
                "rep_output_mean": output,
                "heart_rate_end_mean": 170,
                "recovery_drop_mean": 25,
                "first_to_last_change_pct": -2.0,
            }

        records = [
            _row("2026-11-03", reps(5, 180, 240)),
            _row("2026-10-27", reps(6, 180, 245)),
            _row("2026-10-20", reps(5, 200, 248)),
            _row("2026-10-13", reps(4, 170, 250)),
            _row("2026-10-06", reps(10, 60, 220)),
        ]
        result = comparable_sessions(records, "act_2026-11-03")
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["difference"]["rep_output_mean"], -8)
        # The session report gates its personal verdicts on how many peers a median rests on.
        self.assertEqual(result["median_peers"]["recovery_drop_mean"], 3)

    def test_doubtful_own_data_and_missing_streams_say_why(self) -> None:
        doubtful = _easy(330, 140)
        doubtful["data_quality_ok"] = False
        records = [_row("2026-11-03", doubtful)] + [
            _row(day, _easy(340, 142))
            for day in ("2026-10-27", "2026-10-20", "2026-10-13")
        ]
        result = comparable_sessions(records, "act_2026-11-03")
        self.assertEqual(result["status"], "not_covered")
        self.assertIn("zweifelhaft", result["reason"])
        missing = comparable_sessions([_row("2026-11-03", None)], "act_2026-11-03")
        self.assertIn("voller Auflösung", missing["reason"])

    def test_peers_name_their_stream_source(self) -> None:
        summaries = [_easy(330, 140)] + [_easy(340, 142) for _ in range(3)]
        for summary, source in zip(
            summaries, ("Garmin", "Wahoo", "Garmin", "Garmin"), strict=True
        ):
            summary["source"] = source
        days = ("2026-11-03", "2026-10-27", "2026-10-20", "2026-10-13")
        records = [
            _row(day, summary) for day, summary in zip(days, summaries, strict=True)
        ]
        result = comparable_sessions(records, "act_2026-11-03")
        self.assertEqual(result["this"]["source"], "Garmin")
        self.assertEqual(result["peers"][0]["source"], "Wahoo")

    def test_a_session_without_execution_is_not_covered(self) -> None:
        result = comparable_sessions([_row("2026-11-03", None)], "act_2026-11-03")
        self.assertEqual(result["status"], "not_covered")


def _paced(pace: float, seconds: float = 400.0) -> dict[str, Any]:
    """A steady pace profile with ``seconds`` in each of the 130 and 135 bpm bins."""
    speed = 1000 / pace
    part = {
        "seconds": seconds,
        "distance_m": seconds * speed,
        "speed_sq": seconds * speed * speed,
    }
    return {"status": "ok", "grade_adjusted": True, "bins": {"130": part, "135": part}}


def _long(day: str, minutes: int, pace: float, decoupling: float = 4.0) -> dict:
    row = _row(day, {**_easy(350, 133, seconds=4200), "decoupling_pct": decoupling})
    return {**row, "duration_s": minutes * 60, "pace_at_hr_profile": _paced(pace)}


class SessionTypeCohortTests(unittest.TestCase):
    """#4136: comparisons only within the session type and plan target."""

    def test_long_runs_compare_only_with_long_runs_of_the_same_target(self) -> None:
        records = [
            _long("2026-09-06", 86, 346),
            _long("2026-08-30", 80, 352),
            # An easy run of the same shape and heart rate.
            _long("2026-09-02", 70, 340),
            # A long run with another heart-rate cap.
            _long("2026-08-26", 80, 330),
            _long("2026-08-23", 80, 353),
            _long("2026-08-16", 78, 355),
        ]
        plans = {
            "act_2026-09-02": {"focus": "easy", "target": 84.0},
            "act_2026-08-26": {"focus": "long", "target": 90.0},
            **{
                f"act_{day}": {"focus": "long", "target": 84.0}
                for day in ("2026-09-06", "2026-08-30", "2026-08-23", "2026-08-16")
            },
        }
        types = session_types(records, plans, None)
        result = comparable_sessions(records, "act_2026-09-06", types=types)
        self.assertEqual({"type": "long", "origin": "plan"}, result["session_type"])
        self.assertEqual(
            ["2026-08-30", "2026-08-23", "2026-08-16"],
            [row["date"] for row in result["peers"]],
        )
        # 86 minutes against the median 80 of the three newest long runs.
        self.assertEqual(6, result["duration_change_min"])
        # 346 s/km in the 130-139 bpm band against the median 353: 2 % faster.
        progress = result["progress"]
        self.assertEqual(
            ("pace_at_heart_rate", "besser", [130, 139]),
            (
                progress["marker"],
                progress["verdict"],
                result["this"]["heart_rate_band_bpm"],
            ),
        )
        self.assertEqual(353.0, progress["median_pace_s_per_km"])

    def test_a_level_pace_is_decided_by_the_decoupling(self) -> None:
        records = [
            _long("2026-09-06", 80, 352, decoupling=2.0),
            *(
                _long(day, 80, 352, decoupling=5.0)
                for day in ("2026-08-30", "2026-08-23", "2026-08-16")
            ),
        ]
        types = session_types(
            records, {row["activity_id"]: {"focus": "long"} for row in records}, None
        )
        result = comparable_sessions(records, "act_2026-09-06", types=types)
        self.assertEqual("besser", result["progress"]["verdict"])
        self.assertEqual(-3.0, result["progress"]["decoupling_change_pts"])

    def test_two_peers_are_listed_without_a_progress_verdict(self) -> None:
        records = [
            _long("2026-09-06", 86, 346),
            _long("2026-08-30", 80, 352),
            _long("2026-08-23", 80, 353),
        ]
        types = session_types(
            records, {row["activity_id"]: {"focus": "long"} for row in records}, None
        )
        result = comparable_sessions(records, "act_2026-09-06", types=types)
        self.assertEqual(("ok", 2), (result["status"], result["count"]))
        self.assertEqual("not_covered", result["progress"]["status"])

    def test_faster_at_a_clearly_higher_heart_rate_is_level(self) -> None:
        # The athlete's threshold example: 6 s/km faster than the median 280,
        # but 5 bpm higher (157 against 152), is "gleich"; 1 bpm higher is
        # "besser"; 10 s/km slower at the same heart rate is "schlechter".
        def threshold(pace: int, heart: int) -> dict[str, Any]:
            return _row("2026-09-06", _easy(pace, heart, seconds=1800))

        peers = [
            _row(day, _easy(pace, 152, seconds=1800))
            for day, pace in (
                ("2026-08-30", 280),
                ("2026-08-23", 280),
                ("2026-08-16", 281),
            )
        ]
        for own, verdict in (
            (threshold(274, 157), "gleich"),
            (threshold(274, 153), "besser"),
            (threshold(290, 152), "schlechter"),
        ):
            records = [own, *peers]
            types = session_types(
                records,
                {row["activity_id"]: {"focus": "threshold"} for row in records},
                None,
            )
            with self.subTest(verdict=verdict):
                result = comparable_sessions(records, "act_2026-09-06", types=types)
                self.assertEqual(verdict, result["progress"]["verdict"])
                self.assertEqual("output", result["progress"]["marker"])

    def test_an_unknown_type_or_heart_rate_gives_no_progress(self) -> None:
        # #4136: peers of mixed type give no verdict and no duration change,
        # and an output change without the heart rate it cost gives none.
        records = [
            _long("2026-09-06", 86, 346),
            *(
                _long(day, 80, 352)
                for day in ("2026-08-30", "2026-08-23", "2026-08-16")
            ),
        ]
        untyped = comparable_sessions(records, "act_2026-09-06", types={})
        self.assertEqual(
            (None, None, "not_covered"),
            (
                untyped["session_type"],
                untyped["duration_change_min"],
                untyped["progress"]["status"],
            ),
        )
        no_heart = {"structure": "continuous", "output_unit": "W", "output": 250}
        peers = [{**no_heart, "output": 240} for _ in range(3)]
        self.assertEqual("not_covered", progress(no_heart, peers)["status"])

    def test_without_a_plan_the_type_is_detected_from_the_execution(self) -> None:
        interval = {"structure": "interval", "rep_count": 4, "median_rep_s": 240}
        records = [
            _row("2026-09-01", interval),
            _row("2026-09-02", _easy(350, 135, seconds=4000)),
            _row("2026-09-03", _easy(340, 135, seconds=3000)),
            _row("2026-09-04", _easy(290, 150, seconds=3000)),
            _row("2026-09-05", _easy(290, 150)),
        ]
        plans = {"act_2026-09-05": {"focus": "race"}}
        self.assertEqual(
            {
                "act_2026-09-01": "interval",
                "act_2026-09-02": "long",
                "act_2026-09-03": "easy",
                "act_2026-09-04": "threshold",
            },
            {
                key: found["type"]
                for key, found in session_types(records, plans, 140).items()
            },
        )
        # Without a first threshold a steady run has no detected type.
        self.assertEqual({"act_2026-09-01"}, set(session_types(records, plans, None)))


class EqualEffortTrendTests(unittest.TestCase):
    def _rides(self, days: list[str], watts: int, heart: int) -> list[dict[str, Any]]:
        return [
            _row(
                day,
                {
                    "structure": "continuous",
                    "output_unit": "W",
                    "data_quality_ok": True,
                    "main_part_s": 3600,
                    "output": watts,
                    "heart_rate_avg": heart,
                },
                sport="ride",
            )
            for day in days
        ]

    def test_more_watts_at_the_same_heart_rate_is_a_positive_change(self) -> None:
        records = self._rides(["2026-09-01", "2026-09-08", "2026-09-15"], 200, 140)
        records += self._rides(["2026-10-06", "2026-10-13", "2026-10-20"], 210, 140)
        trend = equal_effort_trend(
            records, date(2026, 8, 31), date(2026, 10, 5), date(2026, 11, 2)
        )
        outdoor = trend["ride"]["outdoor"]
        self.assertEqual(outdoor["status"], "ok")
        self.assertEqual(outdoor["change_pct"], 5.0)
        self.assertFalse(outdoor["effort_differs"])
        self.assertEqual(trend["run"]["outdoor"]["status"], "not_covered")

    def test_rides_without_power_and_other_sports_are_left_out(self) -> None:
        records = self._rides(["2026-09-01", "2026-09-08", "2026-09-15"], 200, 140)
        for row in self._rides(["2026-10-06", "2026-10-13", "2026-10-20"], 30, 140):
            row["execution_summary"]["output_unit"] = "km/h"
            records.append(row)
        trend = equal_effort_trend(
            records,
            date(2026, 8, 31),
            date(2026, 10, 5),
            date(2026, 11, 2),
            sports=("ride",),
        )
        self.assertEqual(trend["ride"]["outdoor"]["status"], "not_covered")
        self.assertNotIn("run", trend)

    def test_a_change_of_stream_source_is_named(self) -> None:
        records = self._rides(["2026-09-01", "2026-09-08", "2026-09-15"], 200, 140)
        later = self._rides(["2026-10-06", "2026-10-13", "2026-10-20"], 210, 140)
        for row in records:
            row["execution_summary"]["source"] = "Garmin"
        for row in later:
            row["execution_summary"]["source"] = "Wahoo"
        outdoor = equal_effort_trend(
            records + later, date(2026, 8, 31), date(2026, 10, 5), date(2026, 11, 2)
        )["ride"]["outdoor"]
        self.assertEqual(["Garmin"], outdoor["earlier"]["sources"])
        self.assertTrue(outdoor["sources_differ"])

    def test_a_different_effort_is_named(self) -> None:
        records = self._rides(["2026-09-01", "2026-09-08", "2026-09-15"], 200, 140)
        records += self._rides(["2026-10-06", "2026-10-13", "2026-10-20"], 240, 155)
        outdoor = equal_effort_trend(
            records, date(2026, 8, 31), date(2026, 10, 5), date(2026, 11, 2)
        )["ride"]["outdoor"]
        self.assertTrue(outdoor["effort_differs"])


class ComparisonMetricsTests(unittest.TestCase):
    def test_continuous_and_interval_summaries(self) -> None:
        easy = session_execution(
            synthetic_session([("lap", 3600, 3.0, (135.0, 150.0))], kinds=False)
        )
        metrics = comparison_metrics(easy)
        assert metrics is not None
        self.assertEqual(metrics["structure"], "continuous")
        self.assertEqual(metrics["output"], 333)
        self.assertEqual(metrics["main_part_s"], 3000)
        segments: list[tuple[str, int, float, tuple[float, float]]] = [
            ("warmup", 600, 3.0, (120.0, 130.0))
        ]
        for _ in range(4):
            segments += [
                ("work", 180, 4.5, (150.0, 170.0)),
                ("recovery", 90, 2.5, (170.0, 140.0)),
            ]
        intervals = comparison_metrics(session_execution(synthetic_session(segments)))
        assert intervals is not None
        self.assertEqual(intervals["rep_count"], 4)
        self.assertEqual(intervals["median_rep_s"], 180)
        self.assertEqual(intervals["rep_output_mean"], 222)
        self.assertEqual(intervals["heart_rate_end_mean"], 169)
        self.assertIsNone(comparison_metrics({"status": "insufficient_data"}))


if __name__ == "__main__":
    unittest.main()

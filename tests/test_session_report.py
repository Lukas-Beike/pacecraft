"""The deterministic session report (#3084): fixture sessions with known answers."""

from __future__ import annotations

import unittest
from typing import Any

from backend.performance.session_report import session_report


def metric(value: float) -> dict[str, Any]:
    return {"value": value}


def zones(*seconds: float) -> dict[str, Any]:
    """Heart-rate zone times Z1, Z2, ... (1-based like the detail document)."""
    return {
        "heart_rate": {
            "buckets": [
                {"index": index, "seconds": value}
                for index, value in enumerate(seconds, start=1)
            ]
        }
    }


def result(
    *,
    sport: str = "run",
    focus: str | None = None,
    execution: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "session": {"activity_id": "act_1", "sport": sport},
        "focus": {"status": "matched", "session_focus": focus}
        if focus
        else {"status": "unknown"},
        "analysis": {"execution": execution or {}},
        **extra,
    }


def checks(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {check["id"]: check for check in report["checks"]}


class EasySessionTests(unittest.TestCase):
    def test_decoupling_cap_and_time_above_with_planned_strides_left_out(self) -> None:
        # 3600 s of zone time, 300 s above zone 2, of which the plan's two 20 s
        # strides: (300 - 40) / 3600 = 7.2 % stays under 10 %. Decoupling 6.2 % > 5 %.
        report = session_report(
            result(
                focus="easy",
                metrics={"decoupling_pct": metric(6.2), "max_hr": metric(171)},
                zones=zones(1000, 2300, 200, 100),
                execution={
                    "data_quality": {"heart_rate": "ok", "speed": "ok"},
                },
                planned_targets={
                    "heart_rate_cap": {
                        "cap_bpm": 150,
                        "time_above_cap_pct": 10,
                        "verdict": "above_cap",
                    }
                },
            ),
            {"above_easy_s": 40},
        )
        found = checks(report)
        self.assertEqual("easy", report["session_type"])
        self.assertEqual(
            ("notable", "fixed"),
            (found["decoupling"]["verdict"], found["decoupling"]["basis"]),
        )
        self.assertEqual(
            ({"seconds_above": 260, "share_pct": 7.2}, "ok"),
            (found["easy_time_above"]["value"], found["easy_time_above"]["verdict"]),
        )
        # 10 % rounded is not above 10, but planned_targets judged above_cap.
        self.assertEqual("notable", found["heart_rate_cap"]["verdict"])
        # The cap (weight 4) leads before the decoupling (weight 3).
        # Without a duration or load the plan checks are unknown, not notable.
        self.assertEqual(["heart_rate_cap", "decoupling"], report["notable"])
        self.assertEqual(report["notable"], report["lead"])

    def test_unplanned_surges_count_as_time_above(self) -> None:
        # 600 s above zone 2 of 3600 s = 16.7 %; no plan excuses the laps.
        found = checks(
            session_report(
                result(
                    focus="easy",
                    zones=zones(1000, 2000, 400, 200),
                    execution={
                        "data_quality": {"heart_rate": "ok", "speed": "ok"},
                        "reps": [{"rep": n, "duration_s": 180} for n in (1, 2, 3)],
                    },
                )
            )
        )["easy_time_above"]
        self.assertEqual(
            ({"seconds_above": 600, "share_pct": 16.7}, "notable"),
            (found["value"], found["verdict"]),
        )

    def test_unchecked_heart_rate_is_unknown(self) -> None:
        # No execution analysis: no spike or cadence-lock check ran.
        found = checks(
            session_report(
                result(focus="easy", metrics={"decoupling_pct": metric(9.0)})
            )
        )
        self.assertEqual("unknown", found["decoupling"]["verdict"])
        self.assertIn("nicht geprüft", found["decoupling"]["reason"])

    def test_heart_rate_spikes_make_heart_rate_checks_unknown(self) -> None:
        report = session_report(
            result(
                focus="long",
                metrics={"decoupling_pct": metric(9.0)},
                zones=zones(100, 100, 900),
                execution={"data_quality": {"heart_rate": "spikes", "speed": "ok"}},
            )
        )
        found = checks(report)
        for key in ("decoupling", "easy_time_above"):
            self.assertEqual("unknown", found[key]["verdict"], key)
            self.assertIn("spikes", found[key]["reason"])
        self.assertEqual([], report["notable"])

    def test_ride_steadiness_reads_the_variability_index(self) -> None:
        found = checks(
            session_report(
                result(
                    sport="ride",
                    focus="endurance",
                    metrics={"variability_index": metric(1.08)},
                )
            )
        )
        self.assertEqual(
            (1.08, "notable"),
            (found["steadiness"]["value"], found["steadiness"]["verdict"]),
        )


class IntervalTests(unittest.TestCase):
    def test_ride_fade_within_rep_fade_and_start_change(self) -> None:
        # No matched focus: the detected interval structure selects the checks.
        # Median of 300, 300, 295, 285 W is 297.5; 285 / 297.5 - 1 = -4.2 %.
        reps = [
            {
                "rep": 1,
                "output": 300,
                "within_rep_change_pct": 0.5,
                "start_change_pct": 2.0,
            },
            {
                "rep": 2,
                "output": 300,
                "within_rep_change_pct": -1.0,
                "start_change_pct": 1.0,
            },
            {
                "rep": 3,
                "output": 295,
                "within_rep_change_pct": -2.0,
                "start_change_pct": 0.0,
            },
            {
                "rep": 4,
                "output": 285,
                "within_rep_change_pct": -3.5,
                "start_change_pct": 0.5,
            },
        ]
        found = checks(
            session_report(
                result(
                    sport="ride",
                    execution={
                        "output_unit": "W",
                        "structure": "interval",
                        "reps": reps,
                    },
                )
            )
        )
        self.assertEqual(
            {"worst_rep": 4, "worst_vs_median_pct": -4.2, "below_threshold_reps": [4]},
            found["rep_fade"]["value"],
        )
        self.assertEqual("notable", found["rep_fade"]["verdict"])
        self.assertEqual([4], found["within_rep_fade"]["value"]["below_threshold_reps"])
        self.assertEqual("ok", found["start_change"]["verdict"])

    def test_run_fade_uses_speed_and_lap_pace_for_short_reps(self) -> None:
        # Paces 240, 242 and (lap pace) 250 s/km: speeds 4.167, 4.132, 4.0 m/s;
        # 4.0 / 4.132 - 1 = -3.2 %. The short rep's GPS output is ignored.
        reps = [
            {"rep": 1, "output": 240},
            {"rep": 2, "output": 242},
            {"rep": 3, "output": 200, "lap_pace": 250, "short": True},
        ]
        found = checks(
            session_report(
                result(focus="vo2max", execution={"output_unit": "s/km", "reps": reps})
            )
        )
        self.assertEqual(-3.2, found["rep_fade"]["value"]["worst_vs_median_pct"])
        self.assertEqual("notable", found["rep_fade"]["verdict"])

    def test_fade_compares_only_reps_of_similar_length(self) -> None:
        # 3 x 1 km (240, 240, 250 s/km) with 3 x 400 m at 210 s/km: the 1 km
        # reps alone give 4.0 / 4.167 - 1 = -4.0 %, not -10.3 % against all six.
        reps = [
            {"rep": 1, "output": 240, "duration_s": 240},
            {"rep": 2, "output": 210, "duration_s": 84},
            {"rep": 3, "output": 240, "duration_s": 240},
            {"rep": 4, "output": 210, "duration_s": 84},
            {"rep": 5, "output": 250, "duration_s": 250},
            {"rep": 6, "output": 210, "duration_s": 84},
        ]
        fade = checks(
            session_report(
                result(
                    focus="threshold", execution={"output_unit": "s/km", "reps": reps}
                )
            )
        )["rep_fade"]
        self.assertEqual(
            (5, -4.0, "notable"),
            (
                fade["value"]["worst_rep"],
                fade["value"]["worst_vs_median_pct"],
                fade["verdict"],
            ),
        )

    def test_fade_is_unknown_for_tests_and_mixed_targets(self) -> None:
        reps = [{"rep": n, "output": out} for n, out in ((1, 200), (2, 250), (3, 300))]
        execution = {"output_unit": "W", "reps": reps}
        ramp = checks(
            session_report(result(sport="ride", focus="test", execution=execution))
        )["rep_fade"]
        self.assertEqual("unknown", ramp["verdict"])
        pyramid = checks(
            session_report(
                result(
                    sport="ride",
                    focus="vo2max",
                    execution=execution,
                    planned_targets={
                        "status": "ok",
                        "summary": {"planned_work_steps": 3, "executed_work_laps": 3},
                        "reps": [
                            {"rep": 1, "target_watts": [200, 220]},
                            {"rep": 2, "target_watts": [250, 270]},
                            {"rep": 3, "target_watts": [300, 320]},
                        ],
                    },
                )
            )
        )
        self.assertEqual("unknown", pyramid["rep_fade"]["verdict"])

    def test_jumping_speed_gates_a_ride_without_power(self) -> None:
        reps = [
            {"rep": n, "output": out} for n, out in ((1, 36.0), (2, 36.0), (3, 30.0))
        ]
        fade = checks(
            session_report(
                result(
                    sport="ride",
                    focus="threshold",
                    execution={
                        "output_unit": "km/h",
                        "reps": reps,
                        "data_quality": {"heart_rate": "ok", "speed": "jumps"},
                    },
                )
            )
        )["rep_fade"]
        self.assertEqual("unknown", fade["verdict"])

    def test_cardiac_drift_only_at_equal_output(self) -> None:
        # 160 -> 170 bpm at 300 -> 296 W (-1.3 %): drift 6.25 % > 5 %.
        reps = [
            {"rep": 1, "output": 300, "heart_rate_end": 160},
            {"rep": 2, "output": 296, "heart_rate_end": 170},
        ]
        drift = checks(
            session_report(
                result(
                    sport="ride",
                    focus="threshold",
                    execution={
                        "output_unit": "W",
                        "reps": reps,
                        "data_quality": {"heart_rate": "ok", "speed": "ok"},
                    },
                )
            )
        )["cardiac_drift"]
        self.assertEqual(
            (6.2, "notable"), (drift["value"]["drift_pct"], drift["verdict"])
        )
        reps[1]["output"] = 280
        drift = checks(
            session_report(
                result(
                    sport="ride",
                    focus="threshold",
                    execution={
                        "output_unit": "W",
                        "reps": reps,
                        "data_quality": {"heart_rate": "ok", "speed": "ok"},
                    },
                )
            )
        )["cardiac_drift"]
        self.assertEqual("unknown", drift["verdict"])

    def test_recovery_heart_rate_drop_against_the_own_median(self) -> None:
        # Own mean (22 + 24) / 2 = 23 against a median of 30 over 5 peers:
        # 7 bpm below the median > 5.
        execution = {
            "status": "ok",
            "structure": "interval",
            "output_unit": "W",
            "reps": [
                {"rep": 1, "output": 300, "duration_s": 240},
                {"rep": 2, "output": 300, "duration_s": 240},
            ],
            "recoveries": [{"heart_rate_drop_60s": 22}, {"heart_rate_drop_60s": 24}],
            "data_quality": {"heart_rate": "ok", "speed": "ok"},
        }

        def compared(peers: int) -> dict[str, Any]:
            return {
                "status": "ok",
                "median_of_peers": {"recovery_drop_mean": 30},
                "median_peers": {"recovery_drop_mean": peers},
            }

        found = checks(
            session_report(
                result(
                    sport="ride",
                    focus="vo2max",
                    execution=execution,
                    comparable_sessions=compared(5),
                )
            )
        )["recovery_heart_rate_drop"]
        self.assertEqual(
            (
                {"mean_drop_bpm": 23.0, "own_median_bpm": 30, "peers": 5},
                "notable",
                "personal",
            ),
            (found["value"], found["verdict"], found["basis"]),
        )
        few = checks(
            session_report(
                result(
                    sport="ride",
                    focus="vo2max",
                    execution=execution,
                    comparable_sessions=compared(4),
                )
            )
        )["recovery_heart_rate_drop"]
        self.assertEqual("unknown", few["verdict"])


class PlanTests(unittest.TestCase):
    def test_duration_load_and_structure_against_the_plan(self) -> None:
        # 4500 s against 3600 s planned: +25 % > 20 %; load 84 against 80: +5 %.
        found = checks(
            session_report(
                result(
                    focus="threshold",
                    # The provider load (Garmin's own scale) is never compared, and
                    # an interval session's standing rests count: elapsed, not moving.
                    metrics={
                        "elapsed_duration_s": metric(4500),
                        "moving_duration_s": metric(3700),
                        "load": metric(300),
                    },
                    planned_targets={
                        "status": "ok",
                        "summary": {"planned_work_steps": 5, "executed_work_laps": 4},
                    },
                ),
                {"duration_s": 3600, "load": 80},
                84,
            )
        )
        self.assertEqual(25.0, found["plan_duration"]["value"]["difference_pct"])
        self.assertEqual("notable", found["plan_duration"]["verdict"])
        self.assertEqual("ok", found["plan_load"]["verdict"])
        self.assertEqual(
            (
                {"planned_work_steps": 5, "executed_work_laps": 4, "difference": -1},
                0.8,
                "notable",
            ),
            (
                found["plan_structure"]["value"],
                found["plan_structure"]["mapping_confidence"],
                found["plan_structure"]["verdict"],
            ),
        )

    def test_an_unreliable_mapping_makes_structure_unknown(self) -> None:
        found = checks(
            session_report(
                result(
                    focus="threshold",
                    planned_targets={
                        "status": "not_covered",
                        "reason": "Keine Intervalle in der Aufzeichnung erkannt.",
                    },
                ),
                {"duration_s": 3600, "load": 80},
            )
        )
        self.assertEqual(
            ("unknown", "Keine Intervalle in der Aufzeichnung erkannt."),
            (found["plan_structure"]["verdict"], found["plan_structure"]["reason"]),
        )
        # An interval focus without detected reps still names the missing check.
        self.assertEqual("unknown", found["rep_fade"]["verdict"])

    def test_a_lap_count_off_the_plan_makes_rep_checks_unknown(self) -> None:
        # Steps and laps pair in order: one missing lap shifts every later pair.
        found = checks(
            session_report(
                result(
                    focus="vo2max",
                    execution={"output_unit": "W", "reps": [{"rep": 1, "output": 300}]},
                    planned_targets={
                        "status": "ok",
                        "summary": {"planned_work_steps": 5, "executed_work_laps": 4},
                        "reps": [
                            {
                                "rep": 1,
                                "time_in_target_pct": 20,
                                "recovery_s": 200,
                                "planned_recovery_s": 120,
                            }
                        ],
                    },
                )
            )
        )
        for key in ("rep_target", "recovery_duration"):
            self.assertEqual("unknown", found[key]["verdict"], key)
            self.assertIn("weichen ab", found[key]["reason"])

    def test_an_easy_session_gets_no_structure_check(self) -> None:
        # A stored heart-rate cap alone makes planned_targets "not_covered".
        found = checks(
            session_report(
                result(
                    focus="easy",
                    planned_targets={
                        "status": "not_covered",
                        "heart_rate_cap": {"cap_bpm": 150, "time_above_cap_pct": 2.0},
                    },
                ),
                {"duration_s": 3600, "load": 40},
            )
        )
        self.assertIn("plan_duration", found)
        self.assertNotIn("plan_structure", found)
        self.assertNotIn("rep_target", found)

    def test_without_a_paired_unit_no_plan_check(self) -> None:
        self.assertFalse(
            {"plan_duration", "plan_load", "plan_structure"}
            & checks(session_report(result(focus="easy"))).keys()
        )


class RaceAndRankingTests(unittest.TestCase):
    def test_a_positive_split_is_notable(self) -> None:
        found = checks(
            session_report(
                result(
                    focus="race",
                    execution={
                        "halves": {"change_pct": -2.5},
                        "data_quality": {"speed": "ok"},
                    },
                )
            )
        )["race_split"]
        self.assertEqual(
            ("notable", -2.5), (found["verdict"], found["value"]["second_vs_first_pct"])
        )

    def test_a_continuous_tempo_run_is_judged_on_its_halves(self) -> None:
        report = session_report(
            result(
                focus="tempo",
                execution={
                    "structure": "continuous",
                    "halves": {"change_pct": -4.0, "main_part_s": 1800},
                },
                planned_targets={"status": "ok", "steps": []},
            ),
            {"duration_s": 2400, "load": 50},
        )
        found = checks(report)
        self.assertEqual("continuous", report["session_type"])
        self.assertEqual("notable", found["main_fade"]["verdict"])
        self.assertFalse({"plan_structure", "rep_fade"} & found.keys())

    def test_every_notable_check_is_listed_and_the_lead_has_at_most_four(self) -> None:
        reps = [
            {
                "rep": n,
                "output": out,
                "within_rep_change_pct": -6,
                "start_change_pct": 6,
                "heart_rate_end": hr,
            }
            for n, out, hr in (
                (1, 300, 150),
                (2, 299, 155),
                (3, 280, 165),
                (4, 298, 170),
            )
        ]
        report = session_report(
            result(
                sport="ride",
                focus="threshold",
                metrics={"elapsed_duration_s": metric(5000)},
                execution={
                    "output_unit": "W",
                    "reps": reps,
                    "data_quality": {"heart_rate": "ok", "speed": "ok"},
                },
            ),
            {"duration_s": 3600, "load": 80},
            120,
        )
        notable = [c["id"] for c in report["checks"] if c["verdict"] == "notable"]
        self.assertEqual(set(notable), set(report["notable"]))
        self.assertGreater(len(report["notable"]), 4)
        self.assertEqual(report["notable"][:4], report["lead"])
        severities = [checks(report)[key]["severity"] for key in report["notable"]]
        self.assertEqual(sorted(severities, reverse=True), severities)


if __name__ == "__main__":
    unittest.main()

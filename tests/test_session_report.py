"""The deterministic session report (#3084): fixture sessions with known answers."""

from __future__ import annotations

import unittest
from collections.abc import Sequence
from typing import Any

from backend.performance.session_report import session_report
from backend.planning.race_plan import race_plan


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

    def test_decoupling_against_the_own_upper_quartile_and_5_percent(self) -> None:
        # Own upper quartile 7 % of 6 peers: 6.2 % is ok, 7.5 % notable. With 4
        # peers the fixed 5 % decides, so 6.2 % is notable.
        def decoupling(value: float, peers: int) -> dict[str, Any]:
            return checks(
                session_report(
                    result(
                        focus="easy",
                        metrics={"decoupling_pct": metric(value)},
                        execution={"data_quality": {"heart_rate": "ok", "speed": "ok"}},
                        comparable_sessions={
                            "status": "ok",
                            "median_peers": {"decoupling_pct": peers},
                            "upper_quartile_of_peers": {"decoupling_pct": 7.0},
                        },
                    )
                )
            )["decoupling"]

        self.assertEqual(
            ("ok", "personal"),
            (decoupling(6.2, 6)["verdict"], decoupling(6.2, 6)["basis"]),
        )
        self.assertEqual("notable", decoupling(7.5, 6)["verdict"])
        self.assertEqual(
            ("notable", "fixed"),
            (decoupling(6.2, 4)["verdict"], decoupling(6.2, 4)["basis"]),
        )

    def test_run_steadiness_against_the_own_upper_quartile(self) -> None:
        # Own upper quartile 7 % of 6 runs: 8 % is notable, 6 % ok. With 3
        # runs there is no limit: the value is a fact only.
        def report(own: float, peers: int) -> dict[str, Any]:
            return session_report(
                result(
                    focus="easy",
                    execution={"data_quality": {"heart_rate": "ok", "speed": "ok"}},
                    comparable_sessions={
                        "status": "ok",
                        "this": {"gap_cv_pct": own},
                        "median_peers": {"gap_cv_pct": peers},
                        "upper_quartile_of_peers": {"gap_cv_pct": 7.0},
                    },
                )
            )

        self.assertEqual("notable", checks(report(8.0, 6))["steadiness"]["verdict"])
        self.assertEqual("ok", checks(report(6.0, 6))["steadiness"]["verdict"])
        few = report(8.0, 3)
        self.assertNotIn("steadiness", checks(few))
        # Too few peers to compare at all: still the run's own value.
        none = session_report(
            result(
                focus="easy",
                comparable_sessions={
                    "status": "not_covered",
                    "this": {"gap_cv_pct": 5.5},
                },
            )
        )
        self.assertEqual(
            5.5,
            {f["id"]: f["value"] for f in none["facts"]}["run_steadiness"][
                "gap_cv_pct"
            ],
        )
        self.assertEqual(
            {"gap_cv_pct": 8.0, "peers": 3},
            {fact["id"]: fact["value"] for fact in few["facts"]}["run_steadiness"],
        )

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


def km_streams(
    seconds_per_km: Sequence[float], watts: Sequence[float] | None = None
) -> dict[str, Any]:
    """A recording sampled at every km boundary: cumulative time and distance."""
    times = [0.0]
    for seconds in seconds_per_km:
        times.append(times[-1] + seconds)
    channels: dict[str, Any] = {
        "distance_m": {"values": [1000.0 * km for km in range(len(times))]}
    }
    if watts is not None:
        channels["power_w"] = {"values": [*watts, watts[-1]]}
    return {"time_s": times, "channels": channels}


class RacePlanTests(unittest.TestCase):
    """#3084 Part 1c: the race against its race plan (#2803), athlete's limits."""

    def test_a_run_against_its_plan_sections_start_fade_and_finish(self) -> None:
        # Plan 50:00 on two flat 5 km sections: 300 s/km. Run: km 1 in 280 s,
        # km 2-6 in 300 s, km 7-10 in 315 s, 3040 s in all.
        # Section 1: 280 + 4 * 300 = 1480 against 1500 s, -1.3 %.
        # Section 2: 300 + 4 * 315 = 1560 against 1500 s, +4.0 % > 3.
        # Start (first km, 10 % of 10 km): 280 against 300 s, 6.7 % ahead.
        # Fade: every km from km 6 on (the 7th) is 5 % behind.
        # Finish: 3040 against 3000 s, +1.3 %, under 2 %.
        plan = race_plan(
            {
                "id": "c1",
                "sport": "run",
                "distance": 10000,
                "target": "0:50:00",
                "course_profile": "km 0-5 0 %; km 5-10 0 %",
            },
            None,
        )
        report = session_report(
            result(
                focus="race",
                metrics={"elapsed_duration_s": metric(3040)},
                execution={"data_quality": {"heart_rate": "ok", "speed": "ok"}},
                streams=km_streams([280, 300, 300, 300, 300, 300, 315, 315, 315, 315]),
            ),
            race=plan,
        )
        found = checks(report)
        sections = found["race_sections"]
        self.assertEqual(
            ([-1.3, 4.0], 1, "notable"),
            (
                [row["behind_plan_pct"] for row in sections["value"]["sections"]],
                sections["value"]["off_plan"],
                sections["verdict"],
            ),
        )
        self.assertEqual(
            (6.7, "notable"),
            (
                found["race_start"]["value"]["ahead_of_plan_pct"],
                found["race_start"]["verdict"],
            ),
        )
        self.assertEqual(
            ({"fade_from_km": 6, "mean_behind_plan_pct": 5.0}, "notable"),
            (found["race_fade"]["value"], found["race_fade"]["verdict"]),
        )
        self.assertEqual(
            (1.3, "ok"),
            (
                found["race_finish"]["value"]["behind_target_pct"],
                found["race_finish"]["verdict"],
            ),
        )
        # 309 s/km reaches the line in 3090 s, 3.0 % over the target: notable;
        # every km exactly 3 % behind is no fade.
        slower = checks(
            session_report(
                result(
                    focus="race",
                    execution={"data_quality": {"speed": "ok"}},
                    streams=km_streams([309] * 10),
                ),
                race=plan,
            )
        )
        self.assertEqual("notable", slower["race_finish"]["verdict"])
        self.assertEqual("ok", slower["race_fade"]["verdict"])

    def test_a_gps_long_recording_is_scaled_not_cut(self) -> None:
        # 10 km raced at 309 s/km, recorded 1.5 % long (10.15 km): the line is
        # the recording's end, 3090 s, 3.0 % over the 50:00 target.
        plan = race_plan(
            {"id": "c5", "sport": "run", "distance": 10000, "target": "0:50:00"}, None
        )
        streams = km_streams([309] * 10)
        streams["channels"]["distance_m"]["values"] = [1015.0 * km for km in range(11)]
        finish = checks(
            session_report(
                result(
                    focus="race",
                    execution={"data_quality": {"speed": "ok"}},
                    streams=streams,
                ),
                race=plan,
            )
        )["race_finish"]
        self.assertEqual(
            (3090, 3.0, "notable"),
            (
                finish["value"]["finish_s"],
                finish["value"]["behind_target_pct"],
                finish["verdict"],
            ),
        )

    def test_a_walk_off_after_the_line_and_a_slow_race_are_no_fade(self) -> None:
        # 10 km at the planned 300 s/km, then 400 m walked at 720 s/km before
        # the watch stops: the recording is cut at the line, so all is on plan.
        plan = race_plan(
            {"id": "c4", "sport": "run", "distance": 10000, "target": "0:50:00"}, None
        )
        streams = km_streams([300] * 10)
        streams["time_s"].append(streams["time_s"][-1] + 288)
        streams["channels"]["distance_m"]["values"].append(10400.0)
        walked = checks(
            session_report(
                result(
                    focus="race",
                    execution={"data_quality": {"speed": "ok"}},
                    streams=streams,
                ),
                race=plan,
            )
        )
        self.assertEqual(
            ([0.0], 0.0, "ok", None),
            (
                [
                    row["behind_plan_pct"]
                    for row in walked["race_sections"]["value"]["sections"]
                ],
                walked["race_finish"]["value"]["behind_target_pct"],
                walked["race_finish"]["verdict"],
                walked["race_fade"]["value"]["fade_from_km"],
            ),
        )
        # 4 % behind plan from the first km on: the section is off plan, but
        # with no km on plan before it, there is no fade point.
        slow = checks(
            session_report(
                result(
                    focus="race",
                    execution={"data_quality": {"speed": "ok"}},
                    streams=km_streams([312] * 10),
                ),
                race=plan,
            )
        )
        self.assertEqual("notable", slow["race_sections"]["verdict"])
        self.assertEqual("ok", slow["race_fade"]["verdict"])

    def test_a_ride_against_its_planned_power(self) -> None:
        # 200 W target: 200 W on the flat 20 km, 210 W (+5 %) on the climb.
        # Ridden: 200 W, then 195 W on the climb: 1 - 195 / 210 = 7.1 % behind.
        plan = race_plan(
            {
                "id": "c2",
                "sport": "ride",
                "distance": 30000,
                "target": "200 W",
                "course_profile": "km 0-20 0 %; km 20-30 +5 %",
            },
            None,
        )
        found = checks(
            session_report(
                result(
                    sport="ride",
                    focus="race",
                    execution={"output_unit": "W"},
                    streams=km_streams([120] * 30, [200] * 20 + [195] * 10),
                ),
                race=plan,
            )
        )
        self.assertEqual(
            [0.0, 7.1],
            [
                row["behind_plan_pct"]
                for row in found["race_sections"]["value"]["sections"]
            ],
        )
        self.assertEqual("ok", found["race_start"]["verdict"])
        self.assertEqual(20, found["race_fade"]["value"]["fade_from_km"])
        # Without a target time the finish is unknown, not judged.
        self.assertEqual("unknown", found["race_finish"]["verdict"])

    def test_a_course_that_does_not_fit_the_recording_is_unknown(self) -> None:
        # A run given up at 8 km is no finish ahead of the 10 km target.
        plan = race_plan(
            {"id": "c3", "sport": "run", "distance": 10000, "target": "0:50:00"}, None
        )
        found = checks(
            session_report(
                result(
                    focus="race",
                    execution={"data_quality": {"speed": "ok"}},
                    # 8 km recorded against a 10 km plan.
                    streams=km_streams([300] * 8),
                ),
                race=plan,
            )
        )
        for key in ("race_sections", "race_start", "race_fade", "race_finish"):
            self.assertEqual("unknown", found[key]["verdict"], key)
            self.assertIn("Distanz", found[key]["reason"])


class RunBlockTests(unittest.TestCase):
    """#3084 Part 2: the run's own checks beside its session type."""

    def test_form_drift_reads_the_stored_verdict(self) -> None:
        def drift(contact: float, flagged: bool) -> dict[str, Any]:
            return {
                "status": "ok",
                "pace_change_percent": 0.8,
                "flagged": flagged,
                "metrics": {
                    "contact_time": {
                        "change_percent": contact,
                        "threshold_percent": 5.0,
                    },
                    "step_length": {"change_percent": -2.0, "threshold_percent": 5.0},
                },
            }

        def found(analysis: dict[str, Any]) -> dict[str, Any]:
            report = session_report({**result(focus="long"), "analysis": analysis})
            return checks(report)["form_drift"]

        flagged = found({"form_drift": drift(6.1, True)})
        self.assertEqual(
            ("notable", 6.1),
            (
                flagged["verdict"],
                flagged["value"]["metrics"]["contact_time"]["change_pct"],
            ),
        )
        self.assertEqual("ok", found({"form_drift": drift(3.0, False)})["verdict"])
        short = found(
            {
                "form_drift": {
                    "status": "insufficient_data",
                    "reason": "Die Einheit ist kürzer als 45 Minuten.",
                }
            }
        )
        self.assertEqual(
            ("unknown", "Die Einheit ist kürzer als 45 Minuten."),
            (short["verdict"], short["reason"]),
        )

    def test_shoe_against_its_own_target_else_700_km(self) -> None:
        # Own 600 km target, 610 km run: 101.7 %, due. No target: 350 of 700 km.
        # A third at 630 of 700 km is exactly 90 %: due (athlete, 2026-11-16).
        own = {
            "id": "shoe-1",
            "kind": "shoes",
            "usage": {"distance_km": 610},
            "lifetime": {"target_km": 600},
            "lifetime_target_source": "local",
        }
        fresh = {"id": "shoe-2", "kind": "shoes", "usage": {"distance_km": 350}}
        worn = {"id": "shoe-3", "kind": "shoes", "usage": {"distance_km": 630}}
        bike = {"id": "bike-1", "kind": "bike", "usage": {"distance_km": 9000}}
        report = session_report(
            {**result(focus="easy"), "equipment": [own, fresh, worn, bike]}
        )
        shoes = [c for c in report["checks"] if c["id"] == "shoe_mileage"]
        self.assertEqual(
            [
                ("shoe-1", 101.7, "local", "notable"),
                ("shoe-2", 50.0, "default", "ok"),
                ("shoe-3", 90.0, "default", "notable"),
            ],
            [
                (
                    c["value"]["equipment_id"],
                    c["value"]["share_pct"],
                    c["value"]["target_source"],
                    c["verdict"],
                )
                for c in shoes
            ],
        )

    def test_pace_at_heart_rate_against_own_runs(self) -> None:
        # Median 300 s/km of 6 own runs, this one 10 s/km slower: 3.3 % > 3.
        def found(difference: float, count: int) -> dict[str, Any]:
            data = {
                "status": "ok",
                "session": {"pace_s_per_km": 300 + difference},
                "comparable": {"count": count, "median_pace_s_per_km": 300},
                "difference_s_per_km": difference,
                "heart_rate_band_bpm": [140, 149],
            }
            return checks(
                session_report(
                    result(
                        focus="easy",
                        execution={"data_quality": {"heart_rate": "ok", "speed": "ok"}},
                        pace_at_heart_rate=data,
                    )
                )
            )["pace_at_heart_rate"]

        slower = found(10, 6)
        self.assertEqual(
            (3.3, "notable", "personal"),
            (slower["value"]["slower_pct"], slower["verdict"], slower["basis"]),
        )
        self.assertEqual("ok", found(6, 6)["verdict"])
        self.assertEqual("unknown", found(10, 4)["verdict"])

    def test_stride_length_halves_of_each_rep(self) -> None:
        # 1.20 to 1.12 m is 6.7 % shorter > 5; 1.20 to 1.18 m is 1.7 %.
        reps = [
            {
                "rep": 1,
                "output": 240,
                "stride_length_first_half_m": 1.20,
                "stride_length_second_half_m": 1.18,
            },
            {
                "rep": 2,
                "output": 242,
                "stride_length_first_half_m": 1.20,
                "stride_length_second_half_m": 1.12,
            },
        ]
        stride = checks(
            session_report(
                result(
                    focus="threshold",
                    execution={
                        "output_unit": "s/km",
                        "reps": reps,
                        "data_quality": {"speed": "ok"},
                    },
                )
            )
        )["stride_halves"]
        self.assertEqual(
            (
                {
                    "worst_rep": 2,
                    "second_vs_first_half_pct": -6.7,
                    "below_threshold_reps": [2],
                },
                "notable",
            ),
            (stride["value"], stride["verdict"]),
        )

    def test_a_ride_gets_no_run_block(self) -> None:
        report = session_report(
            {
                **result(sport="ride", focus="endurance"),
                "equipment": [
                    {"id": "s", "kind": "shoes", "usage": {"distance_km": 900}}
                ],
            }
        )
        self.assertFalse({"form_drift", "shoe_mileage"} & checks(report).keys())


class ZoneTimeAndFactsTests(unittest.TestCase):
    """#3084 Part 2: zone time against the plan, and facts without a verdict."""

    def test_zone_time_against_the_planned_seconds(self) -> None:
        # Planned 600 / 1800 / 60 s; Z4 holds 2.4 % of 2460 s and is left out.
        # Z1 650 s is +8.3 %, Z2 1300 s is -27.8 %: off plan beyond 20 %.
        zones = {
            "status": "ok",
            "metric": "heartrate",
            "zones": [
                {"zone": 1, "planned_s": 600, "actual_s": 650},
                {"zone": 2, "planned_s": 1800, "actual_s": 1300},
                {"zone": 4, "planned_s": 60, "actual_s": 300},
            ],
        }
        found = checks(
            session_report(
                result(focus="easy", execution={"data_quality": {"heart_rate": "ok"}}),
                plan_zones=zones,
            )
        )["zone_time"]
        self.assertEqual(
            ([8.3, -27.8], "notable"),
            (
                [row["difference_pct"] for row in found["value"]["zones"]],
                found["verdict"],
            ),
        )
        unpaired = checks(session_report(result(focus="easy")))
        self.assertNotIn("zone_time", unpaired)

    def test_zone_time_is_unknown_without_a_zone_for_most_of_the_session(
        self,
    ) -> None:
        # 1800 planned zone seconds of a 3000 s session: the free warm-up and
        # cool-down would always count as off plan.
        partial = {
            "status": "ok",
            "metric": "heartrate",
            "zones": [
                {"zone": 2, "planned_s": 1800, "actual_s": 2500},
                {"zone": 3, "planned_s": 0, "actual_s": 500},
            ],
        }
        found = checks(
            session_report(
                result(focus="easy", execution={"data_quality": {"heart_rate": "ok"}}),
                plan_zones=partial,
            )
        )["zone_time"]
        self.assertEqual(
            ("unknown", "Nicht jeder geplante Abschnitt hat eine Zielzone."),
            (found["verdict"], found["reason"]),
        )
        # Speed zones of a run whose GPS jumps are not judged.
        speed = {
            **partial,
            "metric": "speed",
            "zones": [{"zone": 2, "planned_s": 1800, "actual_s": 1700}],
        }
        jumping = checks(
            session_report(
                result(focus="easy", execution={"data_quality": {"speed": "jumps"}}),
                plan_zones=speed,
            )
        )["zone_time"]
        self.assertEqual("unknown", jumping["verdict"])

    def test_stops_elevation_and_work_laps_of_critical_speed(self) -> None:
        # A flagged stop of 40 s, one of 20 s and an auto-pause gap of 50 s
        # (no samples, moving on both sides): two over 30 s, 90 s in all. A
        # standstill still open at the end is no stop. 240 s/km is 4.167 m/s,
        # 104.2 % of a 4.0 m/s critical speed.
        times = [0, 10, 20, 60, 70, 90, 100, 150, 160, 170]
        moving = [1, 1, 0, 1, 0, 1, 1, 1, 1, 0]
        report = session_report(
            {
                **result(
                    focus="threshold",
                    execution={
                        "output_unit": "s/km",
                        "reps": [{"rep": 1, "output": 240}],
                    },
                ),
                "session": {
                    "activity_id": "act_1",
                    "sport": "run",
                    "environment": "outdoor",
                },
                "metrics": {
                    "elevation_gain_m": metric(212.4),
                    "elevation_loss_m": metric(210),
                },
                "streams": {
                    "time_s": times,
                    "channels": {"moving": {"values": moving}},
                },
                "critical_speed": {"status": "ok", "cs_mps": 4.0},
            }
        )
        facts = {fact["id"]: fact["value"] for fact in report["facts"]}
        self.assertEqual({"count": 2, "total_s": 90}, facts["stops"])
        self.assertEqual(212, facts["elevation"]["gain_m"])
        self.assertEqual(
            [{"rep": 1, "pct_of_critical_speed": 104.2}],
            facts["work_laps_of_critical_speed"]["laps"],
        )
        # A 20 h ride downsampled to one point every 36 s is no chain of stops.
        coarse = session_report(
            {
                **result(sport="ride", focus="endurance"),
                "streams": {
                    "time_s": [36 * index for index in range(2000)],
                    "channels": {"moving": {"values": [1] * 2000}},
                },
            }
        )
        self.assertEqual(
            {"count": 0, "total_s": 0},
            {fact["id"]: fact["value"] for fact in coarse["facts"]}["stops"],
        )
        # Facts carry no verdict and never lead.
        self.assertTrue(all("verdict" not in fact for fact in report["facts"]))


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

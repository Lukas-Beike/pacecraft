"""Running step test (#3833, card M10, protocol P1): protocol and evaluation.

Expected values come from the protocol and the design of the synthetic run, not
from the code under test. Paces are 3600 / km/h: 9.5 km/h is 6:19 /km, 14 km/h
4:17 /km. The RR stream of the step run is built like the M10 crossing test
(tests/test_dfa_alpha1.py): AR(1) noise whose coefficient falls with heart rate,
0.25 (alpha1 about 0.75) at 150 bpm and 0 (white noise, alpha1 0.5) at 165 bpm.
Its stages hold 138 bpm plus 5 bpm per stage, so the designed LT1 lies between
stage 3 (148 bpm) and stage 4 (153 bpm). The anti-correlated side of the noise
reads high, so LT2 is checked against the stage band and the about ±10 bpm
individual error of the 0.5 crossing (Rogers et al. 2021b).
"""

import random
import unittest
from datetime import date
from unittest.mock import MagicMock, Mock, patch

from backend.canonical import dfa_alpha1 as dfa
from backend.canonical.activity_detail import ActivityDfaAlpha1, DfaWindow
from backend.canonical.vocabulary import DfaQuality
from backend.performance import activity_read_service, step_test
from backend.performance.activity_read_service import ActivityAnalysisReadService
from backend.planning.session_focus import is_hard
from backend.planning.workouts import validate_workout_description

DAY = date(2026, 11, 20)


def step_run(stages=10, *, seed=0, artifacts_every=0):
    """RR intervals (ms) and stage laps of a step run: warm-up, stages, cool-down."""
    rng = random.Random(seed)
    plan = [(600.0, 120.0)]
    plan += [(180.0, 138.0 + 5 * index) for index in range(stages)]
    plan += [(600.0, 125.0)]
    series, laps, noise, elapsed, rate, clock = [], [], 0.0, 0.0, 120.0, 0.0
    for index, (length, target) in enumerate(plan):
        end = clock + length
        while elapsed < end:
            rate += (target - rate) * 0.02
            coefficient = max(-0.6, min(0.9, 0.25 * (165 - rate) / 15))
            noise = coefficient * noise + rng.gauss(0, 1)
            interval = 60_000 / rate + 12 * noise * (1 - coefficient**2) ** 0.5
            series.append(interval)
            elapsed += interval / 1000
        stage = 0 < index < len(plan) - 1
        laps.append(
            {
                "start_offset_s": clock,
                "end_offset_s": end,
                "kind": "work" if stage else ("warmup", "cooldown")[index > 0],
                "avg_hr": target,
                "avg_speed_mps": (9.5 + 0.5 * (index - 1)) / 3.6 if stage else 2.4,
            }
        )
        clock = end
    if artifacts_every:
        for index in range(7, len(series), artifacts_every):
            series[index] = 3000.0
    return series, laps


def evaluated(stages=10, **options):
    series, laps = step_run(stages, **options)
    return step_test.evaluate(dfa.first_threshold([series]), laps, DAY)


def linear_windows(count, offset=0.0):
    """Windows every 15 s whose alpha1 falls linearly through 0.75 at 145 bpm."""
    windows = []
    for index in range(count):
        rate = 138 + 0.6 * index
        windows.append(
            DfaWindow(
                start_s=offset + 15.0 * index,
                heart_rate_bpm=rate,
                alpha1=round(0.75 + (145 - rate) * 0.035, 4),
                artifact_share=0.0,
            )
        )
    return tuple(windows)


class ProtocolTests(unittest.TestCase):
    def test_without_stored_paces_stage_1_is_9_5_kmh_and_ten_stages_follow(
        self,
    ) -> None:
        template = step_test.protocol_template(None, None)
        lines = template["description"].splitlines()
        self.assertEqual(lines[0], "- 10m 6:40-7:30/km Pace")
        # Each stage holds a band of ±3 % around its pace (athlete decision).
        self.assertEqual(lines[1], "- 3m 6:08-6:30/km Pace")
        self.assertEqual(lines[10], "- 3m 4:09-4:25/km Pace")
        self.assertEqual(lines[11], "- 10m 6:40-7:30/km Pace")
        self.assertEqual(
            lines[-1],
            "Ende: wenn die Stufe nicht mehr zu halten ist, spätestens nach Stufe 10.",
        )
        self.assertEqual(template["duration_minutes"], 50)
        self.assertEqual(
            (template["session_focus"], template["test_protocol"], template["target"]),
            ("test", "run_step_test", "PACE"),
        )
        validate_workout_description(dict(template))

    def test_stages_start_15_kmh_below_lt1_and_end_two_beyond_lt2(self) -> None:
        # LT1 5:00 /km is 12 km/h, so stage 1 is 10.5 km/h (5:43 /km). LT2 4:40 /km
        # is 12.86 km/h; stage 6 (13 km/h) is the first at or above it, so the
        # test ends with stage 8 (14 km/h, 4:17 /km).
        template = step_test.protocol_template(300, 280)
        steps = [line for line in template["description"].splitlines() if "3m" in line]
        self.assertEqual(len(steps), 8)
        self.assertEqual(
            (steps[0], steps[-1]), ("- 3m 5:33-5:53/km Pace", "- 3m 4:09-4:25/km Pace")
        )
        self.assertEqual(template["duration_minutes"], 10 + 8 * 3 + 10)
        self.assertTrue(template["description"].endswith("nach Stufe 8."))
        validate_workout_description(dict(template))

    def test_at_most_ten_stages_and_implausible_paces_fall_back(self) -> None:
        # LT1 5:20 /km would start at 9.75 km/h; LT2 4:25 /km (13.58 km/h) moves
        # stage 1 up to 13.58 - 3.5 = 10.1 km/h (#3915): stage 8 (13.6 km/h) is the
        # first at or above LT2, plus two is 10.
        self.assertEqual(len(step_test.stage_speeds(320, 265)), 10)
        self.assertEqual(step_test.stage_speeds(320, 265)[0], 10.1)
        self.assertGreaterEqual(step_test.stage_speeds(320, 265)[-1], 3600 / 265 + 0.9)
        # A 15:00 /km LT1 would start the stages at walking speed.
        for paces in ((None, None), (20, None), ("5:00", True), (5000, 0), (900, None)):
            with self.subTest(paces=paces):
                self.assertEqual(step_test.stage_speeds(*paces)[0], 9.5)
                self.assertEqual(len(step_test.stage_speeds(*paces)), 10)

    def test_the_step_test_is_a_hard_unit_for_the_plan_checks(self) -> None:
        # Illness, back-to-back and return checks treat it as hard (#2083).
        self.assertTrue(is_hard(step_test.protocol_template(None, None)))


class EvaluationTests(unittest.TestCase):
    def test_a_step_run_gives_lt1_and_lt2_at_the_designed_stages(self) -> None:
        result = evaluated()
        self.assertEqual(result["status"], "ok")
        self.assertEqual((result["stages"], result["analysable_stages"]), (10, 10))
        self.assertLessEqual(result["artifact_share_pct"], 1.0)
        self.assertEqual(result["source"], "abgeleitet")
        lt1, lt2 = result["lt1"], result["lt2"]
        self.assertEqual((lt1["status"], lt1["quality"]), ("ok", "good"))
        self.assertTrue(148 <= lt1["heart_rate_bpm"] <= 153)
        # Between stage 3 (10.5 km/h, 343 s/km) and stage 4 (11 km/h, 327 s/km).
        self.assertTrue(327 <= lt1["pace_seconds_per_km"] <= 343)
        self.assertEqual((lt2["status"], lt2["quality"]), ("ok", "good"))
        self.assertTrue(163 <= lt2["heart_rate_bpm"] <= 175)
        self.assertGreater(lt2["heart_rate_bpm"], lt1["heart_rate_bpm"])
        self.assertLess(lt2["pace_seconds_per_km"], lt1["pace_seconds_per_km"])
        # #3920 C7: no cited limit of agreement exists for the step test, so none is shown.
        self.assertNotIn("range", lt1)
        self.assertNotIn("range", lt2)

    def test_a_run_stopped_early_says_which_threshold_it_did_not_reach(self) -> None:
        # Four stages reach 153 bpm: alpha1 never reaches 0.5, LT1 gives nothing good.
        result = evaluated(4)
        self.assertEqual(result["lt2"]["status"], "not_reached")
        self.assertIn("0,5", result["lt2"]["reason"])
        self.assertNotEqual(result["lt1"].get("quality"), "good")
        # A run whose windows all stay above 0.75 reaches neither threshold.
        windows = tuple(
            DfaWindow(
                start_s=30.0 * i,
                heart_rate_bpm=120 + i,
                alpha1=1.1 - 0.01 * i,
                artifact_share=0.0,
            )
            for i in range(20)
        )
        result = step_test.evaluate(
            ActivityDfaAlpha1(None, 20, 0.0, None, 3, windows), [], DAY
        )
        self.assertEqual(result["lt1"]["status"], "not_reached")
        self.assertIn("0,75", result["lt1"]["reason"])
        self.assertEqual(result["lt2"]["status"], "not_reached")

    def test_artifact_heavy_recordings_give_no_value_to_adopt(self) -> None:
        # One beat in 15 (6.7 %) rejects every window: no window series at all.
        self.assertEqual(evaluated(artifacts_every=15)["status"], "no_rr")
        # One beat in 25 (4 %) keeps the windows but is above the good limit of 3 %.
        result = evaluated(artifacts_every=25)
        self.assertGreater(result["artifact_share_pct"], 3.0)
        for key in ("lt1", "lt2"):
            self.assertNotEqual(result[key].get("quality"), "good")
        self.assertEqual(step_test.evaluate(None, [], DAY)["status"], "no_rr")

    def test_fewer_than_six_analysable_stages_lower_the_quality(self) -> None:
        series, laps = step_run()
        result = dfa.first_threshold([series])
        assert result is not None
        # Only stages 1 to 5 are recorded as stage laps.
        found = step_test.evaluate(result, laps[:6] + laps[-1:], DAY)
        self.assertEqual(found["analysable_stages"], 5)
        self.assertEqual(found["lt1"]["quality"], "low")
        self.assertEqual(result.quality, DfaQuality.GOOD)

    def test_the_pace_is_read_between_the_stages_around_the_heart_rate(self) -> None:
        laps = [
            {"start_offset_s": 0, "end_offset_s": 180, "avg_hr": 140, "avg_speed_mps": 3.0},
            {"start_offset_s": 180, "end_offset_s": 360, "avg_hr": 150, "avg_speed_mps": 3.5},
        ]  # fmt: skip
        # A straight line through alpha1 0.75 at 145 bpm, every midpoint in a stage.
        result = ActivityDfaAlpha1(None, 20, 0.0, None, 3, linear_windows(20))
        lt1 = step_test.evaluate(result, laps, DAY)["lt1"]
        self.assertEqual(lt1["heart_rate_bpm"], 145)
        # 145 bpm is halfway: 3.25 m/s is 1000 / 3.25 = 307.7 s/km.
        self.assertEqual(lt1["pace_seconds_per_km"], 308)
        # Each stage's heart rate is its last minute's mean, not the lap mean
        # with its rise (#3916): 145 and 155 bpm put 145 at the slower stage.
        streams = {
            "time_s": [100, 130, 160, 280, 310, 340],
            "channels": {"heart_rate": {"values": [120, 145, 145, 130, 155, 155]}},
        }
        lt1 = step_test.evaluate(result, laps, DAY, streams)["lt1"]
        self.assertEqual(lt1["pace_seconds_per_km"], 333)
        higher = [{**lap, "avg_hr": lap["avg_hr"] + 10} for lap in laps]
        self.assertIsNone(
            step_test.evaluate(result, higher, DAY)["lt1"]["pace_seconds_per_km"]
        )

    def test_warm_up_and_cool_down_windows_never_move_the_crossings(self) -> None:
        # Stage laps 0 to 360 s hold the line through 145 bpm. A cool-down after
        # the hardest stage keeps alpha1 low at a low heart rate (fatigue), and a
        # warm-up sits on the flat upper plateau: over the whole run they pull the
        # line, over the stage windows they are left out (#3916).
        laps = [
            {"start_offset_s": 600, "end_offset_s": 780, "avg_hr": 140, "avg_speed_mps": 3.0},
            {"start_offset_s": 780, "end_offset_s": 960, "avg_hr": 150, "avg_speed_mps": 3.5},
        ]  # fmt: skip
        stages = linear_windows(20, offset=600)
        warm_up = tuple(
            DfaWindow(start_s=30.0 * i, heart_rate_bpm=110 + i, alpha1=1.0, artifact_share=0.0)
            for i in range(17)
        )  # fmt: skip
        cool_down = tuple(
            DfaWindow(start_s=1000.0 + 30 * i, heart_rate_bpm=135 - i, alpha1=0.55, artifact_share=0.0)
            for i in range(17)
        )  # fmt: skip
        whole = warm_up + stages + cool_down
        run_wide = dfa.crossing(whole, 0.0)
        assert run_wide is not None
        self.assertLess(run_wide[0], 140)
        result = ActivityDfaAlpha1(run_wide[0], len(whole), 0.0, run_wide[1], 3, whole)
        found = step_test.evaluate(result, laps, DAY)
        alone = step_test.evaluate(
            ActivityDfaAlpha1(None, 20, 0.0, None, 3, stages), laps, DAY
        )
        self.assertEqual(found["lt1"]["heart_rate_bpm"], 145)
        self.assertEqual(found["lt1"], alone["lt1"])
        self.assertEqual(found["lt2"], alone["lt2"])

    def test_lt2_uses_stage_windows_from_03_to_075_only(self) -> None:
        # alpha1 falls from 0.75 at 150 bpm to 0.30 at 165 bpm over two stages;
        # stage windows below 0.3 (overshoot at the very end) are left out.
        laps = [
            {"start_offset_s": 0, "end_offset_s": 180, "avg_hr": 150, "avg_speed_mps": 3.5},
            {"start_offset_s": 180, "end_offset_s": 360, "avg_hr": 165, "avg_speed_mps": 4.0},
        ]  # fmt: skip
        line = tuple(
            DfaWindow(
                start_s=15.0 * i,
                heart_rate_bpm=150 + 0.75 * i,
                alpha1=round(0.75 - 0.0225 * i, 4),
                artifact_share=0.0,
            )
            for i in range(21)
        )
        overshoot = DfaWindow(
            start_s=290.0, heart_rate_bpm=150.0, alpha1=0.1, artifact_share=0.0
        )
        result = ActivityDfaAlpha1(None, 22, 0.0, None, 3, (*line, overshoot))
        lt2 = step_test.evaluate(result, laps, DAY)["lt2"]
        # 0.5 is reached after (0.75 - 0.5) / 0.0225 = 11.1 steps: 158.3 bpm.
        self.assertEqual((lt2["status"], lt2["heart_rate_bpm"]), ("ok", 158))

    def test_a_stage_fit_needs_ten_windows_and_lt2_keeps_the_runs_floor(self) -> None:
        laps = [
            {"start_offset_s": 0, "end_offset_s": 180, "avg_hr": 140, "avg_speed_mps": 3.0},
            {"start_offset_s": 180, "end_offset_s": 360, "avg_hr": 150, "avg_speed_mps": 3.5},
        ]  # fmt: skip
        # Nine stage windows on a clean line are too few for an LT1 (MIN_WINDOWS 10).
        few = linear_windows(20)[::2][:9]
        result = ActivityDfaAlpha1(None, 9, 0.0, None, 3, few)
        self.assertEqual(
            step_test.evaluate(result, laps, DAY)["lt1"]["status"], "unknown"
        )
        # Without a stage LT1, LT2 must still lie above the run's stored crossing.
        line = tuple(
            DfaWindow(start_s=15.0 * i, heart_rate_bpm=140 + 0.6 * i, alpha1=round(0.75 - 0.0225 * i, 4), artifact_share=0.0)
            for i in range(21)
        )  # fmt: skip
        floor = ActivityDfaAlpha1(150.0, 21, 0.0, DfaQuality.GOOD, 3, line)
        self.assertEqual(
            step_test.evaluate(floor, laps, DAY)["lt2"]["status"], "unknown"
        )
        free = ActivityDfaAlpha1(140.0, 21, 0.0, DfaQuality.GOOD, 3, line)
        self.assertEqual(step_test.evaluate(free, laps, DAY)["lt2"]["status"], "ok")

    def test_not_reached_reads_the_stage_windows_not_the_cool_down(self) -> None:
        laps = [
            {"start_offset_s": 0, "end_offset_s": 180, "avg_hr": 140, "avg_speed_mps": 3.0},
            {"start_offset_s": 180, "end_offset_s": 360, "avg_hr": 150, "avg_speed_mps": 3.5},
        ]  # fmt: skip
        cool_down = tuple(
            DfaWindow(start_s=400.0 + 30 * i, heart_rate_bpm=130.0, alpha1=0.45, artifact_share=0.0)
            for i in range(5)
        )  # fmt: skip
        result = ActivityDfaAlpha1(
            None, 25, 0.0, None, 3, linear_windows(20) + cool_down
        )
        self.assertEqual(
            step_test.evaluate(result, laps, DAY)["lt2"]["status"], "not_reached"
        )

    def test_a_stage_without_a_last_minute_keeps_all_stages_on_the_lap_mean(
        self,
    ) -> None:
        laps = [
            {"start_offset_s": 0, "end_offset_s": 180, "avg_hr": 140, "avg_speed_mps": 3.0},
            {"start_offset_s": 180, "end_offset_s": 360, "avg_hr": 150, "avg_speed_mps": 3.5},
        ]  # fmt: skip
        result = ActivityDfaAlpha1(None, 20, 0.0, None, 3, linear_windows(20))
        # The second stage's last minute is a strap dropout: lap means for both.
        streams = {
            "time_s": [130, 160, 280, 310, 340],
            "channels": {"heart_rate": {"values": [145, 145, 130, None, None]}},
        }
        lt1 = step_test.evaluate(result, laps, DAY, streams)["lt1"]
        self.assertEqual(lt1["pace_seconds_per_km"], 308)
        self.assertEqual(
            step_test.evaluate(result, laps, DAY)["method"], "run-step-test-v3"
        )

    def test_a_stamped_unit_is_a_step_test_after_its_template_is_gone(self) -> None:
        # The result and its adoption read the unit's own stamp; a unit planned
        # before the stamp still counts through its live template.
        service = ActivityAnalysisReadService(
            Mock(),
            Mock(),
            Mock(),
            canonical=MagicMock(),
            step_test_templates=lambda: frozenset({"live"}),
        )
        document = {
            "session": {
                "activity_id": "act_1",
                "sport": "run",
                "started_at_local": "2026-11-20T07:00:00",
            }
        }
        stamped = {"id": "u1", "template_id": "gone", "test_protocol": "run_step_test"}
        with patch.object(
            activity_read_service.activity_detail_store,
            "dfa_series_results",
            return_value=[],
        ):
            for unit, recognised in (
                (stamped, True),
                ({"id": "u2", "template_id": "live"}, True),
                ({"id": "u3", "template_id": "gone"}, False),
                (None, False),
            ):
                with self.subTest(unit=unit):
                    result = service.step_test(document, unit)
                    self.assertEqual(result is not None, recognised)
            service.detail = Mock(return_value=document)  # type: ignore[method-assign]
            service.paired_planned_unit = Mock(return_value=stamped)  # type: ignore[method-assign]
            tested = service.step_test_for("act_1", DAY)
            assert tested is not None
            self.assertEqual(
                (tested["status"], tested["activity_id"]), ("no_rr", "act_1")
            )
            # A ride is never paired for an adoption.
            document["session"]["sport"] = "ride"
            service.paired_planned_unit.reset_mock()
            self.assertIsNone(service.step_test_for("act_1", DAY))
            service.paired_planned_unit.assert_not_called()

    def test_only_step_test_templates_are_recognised(self) -> None:
        templates = [
            {"id": "a", "test_protocol": "run_step_test"},
            {"id": "b"},
            {"id": "c", "test_protocol": "other"},
        ]
        self.assertEqual(step_test.template_ids(templates), frozenset({"a"}))


if __name__ == "__main__":
    unittest.main()

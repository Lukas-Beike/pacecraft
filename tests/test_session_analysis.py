"""Analytic synthetic signals verify time weighting and conservative eligibility."""

import unittest

from backend.performance.session_analysis import (
    METHOD,
    STEADY_WINDOW_S,
    aerobic_analysis,
    heart_rate_histogram,
    interval_quality,
    normalized_graded_speed,
    rolling_means,
)
from backend.planning.target_snapshot import freeze_targets


def ride(times=None):
    times = list(range(3601)) if times is None else times
    return {
        "sport": "ride",
        "streams": {
            "time": times,
            "watts": [200] * len(times),
            "heartrate": [100 if time < 2100 else 110 for time in times],
        },
    }


def run(environment="outdoor", grade=True):
    """Flat first half at 3.0 m/s, then 2.8 m/s up a 2 % grade, HR constant."""
    times = list(range(3601))
    streams = {
        "time": times,
        "velocity_smooth": [3.0 if time < 2100 else 2.8 for time in times],
        "heartrate": [150] * len(times),
    }
    if grade:
        streams["grade_smooth"] = [0.0 if time < 2100 else 2.0 for time in times]
    return {"sport": "run", "environment": environment, "streams": streams}


class SessionAnalysisTests(unittest.TestCase):
    def test_heart_rate_histogram_counts_moving_seconds_per_bpm(self):
        # 100 s at 120 bpm, a 40 s pause (nothing counted), 50 s at 150 bpm of
        # which the first 10 s are stopped; 25 and 250 bpm are no readings.
        times = list(range(101)) + [140 + second for second in range(51)]
        heart = [120.4] * 101 + [150] * 51
        moving = [True] * 101 + [False] * 10 + [True] * 41
        heart[10] = 25
        heart[11] = 250
        result = heart_rate_histogram(
            {"streams": {"time": times, "heartrate": heart, "moving": moving}}
        )
        self.assertEqual({"120": 98.0, "150": 40.0}, result["seconds"])
        self.assertEqual(138.0, result["covered_s"])

    def test_histogram_counts_downsampled_and_smart_recording_steps(self):
        times = [index * 5.4 for index in range(2000)]
        result = heart_rate_histogram(
            {"streams": {"time": times, "heartrate": [140] * 2000}}
        )
        self.assertAlmostEqual(1999 * 5.4, result["covered_s"], places=1)
        steps = [1, 2, 3, 4, 6, 7, 8]
        smart = [0.0]
        for index in range(600):
            smart.append(smart[-1] + steps[index % len(steps)])
        result = heart_rate_histogram(
            {"streams": {"time": smart, "heartrate": [140] * len(smart)}}
        )
        self.assertAlmostEqual(smart[-1], result["covered_s"], places=2)

    def test_normalized_graded_speed_of_an_interval_run_by_hand(self):
        # Indoor (flat), 1 Hz: 600 s at 3 m/s, then 300 s at 5 m/s. A 30 s
        # trailing mean ends at each second from 29 on (871 means): 571 at 3,
        # 29 ramping 3 + 2k/30 (k = 1..29), 271 at 5. NGS is the 4th root of
        # the mean of their 4th powers.
        times = list(range(901))
        speed = [3.0] * 600 + [5.0] * 301
        fourth = 571 * 3**4 + 271 * 5**4
        fourth += sum((3 + 2 * k / 30) ** 4 for k in range(1, 30))
        expected = (fourth / 871) ** 0.25
        result = normalized_graded_speed(
            {
                "sport": "run",
                "environment": "indoor",
                "streams": {"time": times, "velocity_smooth": speed},
            }
        )
        self.assertAlmostEqual(expected, result["ngs_mps"], places=3)
        self.assertEqual(871.0, result["seconds"])
        self.assertGreater(result["ngs_mps"], (600 * 3 + 300 * 5) / 900)

    def test_normalized_graded_speed_of_a_steady_flat_run_is_its_speed(self):
        result = normalized_graded_speed(
            {
                "sport": "run",
                "environment": "outdoor",
                "streams": {
                    "time": list(range(1201)),
                    "velocity_smooth": [3.0] * 1201,
                    "grade_smooth": [0.0] * 1201,
                },
            }
        )
        self.assertEqual(3.0, result["ngs_mps"])

    def test_normalized_graded_speed_needs_a_run_with_grade_outdoors(self):
        self.assertIsNone(normalized_graded_speed(run(grade=False)))
        self.assertIsNone(normalized_graded_speed(ride()))
        self.assertIsNotNone(normalized_graded_speed(run()))

    def test_decoupling_works_on_a_downsampled_ride(self):
        result = aerobic_analysis(ride([index * 5.4 for index in range(2000)]))
        self.assertEqual("ok", result["status"])

    def test_a_real_pause_above_the_adaptive_gap_is_still_skipped(self):
        times = [float(i) for i in range(100)] + [200.0 + i for i in range(100)]
        result = heart_rate_histogram(
            {"streams": {"time": times, "heartrate": [140] * len(times)}}
        )
        self.assertEqual(198.0, result["covered_s"])

    def test_heart_rate_histogram_needs_a_matching_stream(self):
        self.assertIsNone(heart_rate_histogram({"streams": {"time": [0, 1, 2]}}))
        self.assertIsNone(
            heart_rate_histogram({"streams": {"time": [0, 1], "heartrate": [100]}})
        )

    def test_outdoor_run_decoupling_uses_grade_adjusted_speed(self):
        # Card M8 v2 worked example: Minetti cost at 2 % is 3.6 * 3.1175 / 2.8 J/kg/m,
        # so 2.8 m/s uphill is 3.1175 m/s on level ground. Raw speed would give
        # (3.0 - 2.8) / 3.0 = +6.67 %; grade-adjusted (3.0 - 3.1175) / 3.0 = -3.92 %.
        result = aerobic_analysis(run())
        self.assertEqual("ok", result["status"])
        self.assertEqual("grade_adjusted_speed", result["sensor"])
        self.assertAlmostEqual(-3.92, result["drift_percent"], places=2)
        self.assertAlmostEqual(3.1175, result["halves"][1]["mean_output"], places=4)

    def test_altitude_and_distance_give_the_grade_without_a_grade_stream(self):
        activity = run(grade=False)
        times = activity["streams"]["time"]
        distance, altitude, here, height = [], [], 0.0, 100.0
        for time in times:
            speed, slope = (3.0, 0.0) if time < 2100 else (2.8, 0.02)
            distance.append(here)
            altitude.append(height)
            here += speed
            height += speed * slope
        activity["streams"].update(distance=distance, altitude=altitude)
        result = aerobic_analysis(activity)
        self.assertEqual("grade_adjusted_speed", result["sensor"])
        self.assertLess(result["drift_percent"], 0)

    def test_outdoor_run_without_grade_or_altitude_has_no_decoupling(self):
        result = aerobic_analysis(run(grade=False))
        self.assertEqual("insufficient_data", result["status"])
        self.assertIn("Steigung oder Höhe fehlt", result["reason"])

    def test_treadmill_run_keeps_raw_speed(self):
        result = aerobic_analysis(run("indoor", grade=False))
        self.assertEqual("ok", result["status"])
        self.assertEqual("velocity_smooth", result["sensor"])
        self.assertAlmostEqual(6.67, result["drift_percent"], places=2)

    def test_pulse_rise_at_constant_output_has_known_drift(self):
        result = aerobic_analysis(ride())
        self.assertEqual("ok", result["status"])
        self.assertAlmostEqual(9.09, result["drift_percent"], places=2)
        self.assertEqual("W/bpm", result["unit"])

    def test_irregular_time_spacing_preserves_time_weighted_result(self):
        result = aerobic_analysis(
            ride(list(range(0, 2101, 2)) + list(range(2103, 3601, 3)))
        )
        self.assertEqual("ok", result["status"])
        self.assertAlmostEqual(9.09, result["drift_percent"], places=2)

    def test_missing_pulse_stops_and_variable_intervals_stay_open(self):
        for variant in ("pulse", "stops", "variable", "gap"):
            activity = ride()
            if variant == "pulse":
                activity["streams"].pop("heartrate")
            elif variant == "stops":
                activity["streams"]["moving"] = [False] * 3601
            elif variant == "variable":
                # 2-minute blocks at 100 W and 300 W: both halves average 196 W,
                # but the 60 s means swing from 100 to 300 W (#1832).
                activity["streams"]["watts"] = [
                    100 if i // 120 % 2 else 300 for i in range(3601)
                ]
            else:
                activity = ride(list(range(601)) + list(range(2100, 3601)))
            with self.subTest(variant=variant):
                self.assertEqual(
                    "insufficient_data", aerobic_analysis(activity)["status"]
                )

    def test_second_to_second_output_noise_is_steady_on_60_second_means(self):
        # #1832: 1 s alternation of 100 W and 300 W had a 1 s CV of 50 % and was
        # excluded; its 60 s means are a constant 200 W, so it counts as steady.
        activity = ride()
        activity["streams"]["watts"] = [100 if i % 2 else 300 for i in range(3601)]
        result = aerobic_analysis(activity)
        self.assertEqual("ok", result["status"])
        self.assertEqual(METHOD, result["method"])
        self.assertTrue(all(half["variation"] < 0.01 for half in result["halves"]))
        # Minute-scale swings with equal half means fail the steady rule itself.
        activity["streams"]["watts"] = [
            100 if i // 120 % 2 else 300 for i in range(3601)
        ]
        self.assertIn("zu variabel", aerobic_analysis(activity)["reason"])

    def test_rolling_means_need_a_full_window_also_after_a_gap(self):
        # 1 s samples 0-119, a gap, then 200-299: means end at 59-119 (61) and,
        # after the gap, only once the window is full again at 259-299 (41).
        samples = [(float(t), 1.0, 200.0) for t in [*range(120), *range(200, 300)]]
        means = rolling_means(samples, STEADY_WINDOW_S)
        self.assertEqual(len(means), 61 + 41)
        self.assertEqual({value for _, value in means}, {200.0})

    def test_target_uses_frozen_ftp_and_partial_completion_is_partial(self):
        activity = ride(list(range(121)))
        activity["laps"] = [{"start_time": 0, "end_time": 120}]
        targets = {
            "steps": [{"kind": "power", "target": "100%", "duration": 120}] * 2,
            "basis": {"ftp": 200},
        }
        result = interval_quality(activity, targets)
        self.assertEqual("partial", result["status"])
        self.assertEqual(1, result["missing_steps"])
        self.assertEqual(120, result["steps"][0]["seconds_in_target"])
        self.assertEqual(0, result["steps"][0]["average_deviation_percent"])
        targets["basis"] = {}
        self.assertEqual(
            "insufficient_data",
            interval_quality(activity, targets)["steps"][0]["status"],
        )

    def test_measured_duration_does_not_imply_target_achievement(self):
        activity = ride(list(range(121)))
        activity["laps"] = [{"start_time": 0, "end_time": 120}]
        targets = {"steps": [{"kind": "power", "target": "300W", "duration": 120}]}
        result = interval_quality(activity, targets)["steps"][0]
        self.assertEqual(0, result["seconds_in_target"])
        self.assertAlmostEqual(-33.33, result["average_deviation_percent"])

    def test_ambiguous_laps_are_not_scored(self):
        activity = ride(list(range(121)))
        activity["laps"] = [{"start_time": 0, "end_time": 100}]
        self.assertEqual(
            "insufficient_data",
            interval_quality(
                activity,
                {"steps": [{"kind": "power", "target": "200W", "duration": 120}]},
            )["status"],
        )

    def test_only_unique_local_match_can_freeze_targets(self):
        local = [
            {
                "id": "local-1",
                "date": "2026-10-02",
                "sport": "ride",
                "description": "- 2m 200w",
                "target": "POWER",
            }
        ]
        # Activity summaries: the local session id plus the Intervals.icu record id,
        # which is the stored key the targets are frozen under.
        rows = [
            {
                "id": "act_1",
                "record_ids": {"intervals": "activity-1"},
                "started_at_local": "2026-10-02T08:00:00",
                "sport": "ride",
            }
        ]
        frozen = freeze_targets(
            local, rows, "activity-1", {"ftp": 250}, "2026-10-02T10:00:00Z"
        )
        self.assertIsNotNone(frozen)
        self.assertEqual(120, frozen["steps"][0]["duration"])
        self.assertIsNone(
            freeze_targets(
                local,
                rows
                + [
                    {
                        **rows[0],
                        "id": "act_2",
                        "record_ids": {"intervals": "activity-2"},
                    }
                ],
                "activity-1",
                {},
                "now",
            )
        )

    def test_source_label_names_the_stream_provider(self):
        result = aerobic_analysis(ride(), source_label="Garmin")
        self.assertEqual("derived from Garmin original streams", result["source"])
        self.assertNotIn("provider_decoupling", result)
        self.assertEqual(
            "derived from Garmin Connect, Wahoo original streams",
            aerobic_analysis(ride())["source"],
        )
        activity = ride(list(range(121)))
        activity["laps"] = [{"start_time": 0, "end_time": 120}]
        targets = {"steps": [{"kind": "power", "target": "200W", "duration": 120}]}
        result = interval_quality(activity, targets, source_label="Wahoo")
        self.assertEqual(
            "derived from Wahoo original streams and frozen local targets",
            result["source"],
        )


class GapVariationTests(unittest.TestCase):
    """#3084: a run's steadiness, the time-weighted CV of its grade-adjusted speed."""

    def test_indoor_speed_time_weighted_over_moving_samples(self) -> None:
        from backend.performance.session_analysis import gap_variation_pct

        # Steps of 1, 1 and 2 s at 2.5, 3.5 and 2.5 m/s: mean 2.75, variance
        # (3 x 0.0625 + 1 x 0.5625) / 4 = 0.1875, SD 0.4330, CV 15.7 %. The
        # last sample has no step; a paused one stays out.
        indoor = {
            "sport": "run",
            "environment": "indoor",
            "streams": {
                "time": [0, 1, 2, 4, 5],
                "velocity_smooth": [2.5, 3.5, 2.5, 9.0, 3.0],
                "moving": [True, True, True, False, True],
            },
        }
        self.assertEqual(15.7, gap_variation_pct(indoor))
        # A trail run's pace is not comparable, as for decoupling.
        self.assertIsNone(gap_variation_pct({**indoor, "trail": True}))

    def test_an_outdoor_run_without_a_grade_is_unknown(self) -> None:
        from backend.performance.session_analysis import gap_variation_pct

        outdoor = {
            "sport": "run",
            "environment": "outdoor",
            "streams": {"time": [0, 1, 2], "velocity_smooth": [2.5, 3.5, 3.0]},
        }
        self.assertIsNone(gap_variation_pct(outdoor))


if __name__ == "__main__":
    unittest.main()

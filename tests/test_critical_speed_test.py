import unittest
from datetime import date, timedelta

from backend.performance.critical_power import critical_power_speed
from backend.performance.critical_speed_test import session_test_efforts

TODAY = date(2026, 11, 2)
# CS 4 m/s and D' 200 m: 1200 m take 250 s, 2400 m 550 s, 3600 m 850 s.
TIMES = {1200: 250, 2400: 550, 3600: 850}


def _lap(distance, seconds, start=0, kind="work"):
    return {
        "start_time": start,
        "end_time": start + seconds,
        "kind": kind,
        "distance_m": distance,
    }


def _laps(times=None):
    times = times or TIMES
    laps = [_lap(2000, 600, kind="warmup")]
    start = 600
    for distance in (3600, 2400, 1200):
        laps.append(_lap(distance, times[distance], start))
        start += times[distance]
        laps.append(_lap(500, 1800, start, kind="recovery"))
        start += 1800
    return laps


def _record(activity_id, laps, days_ago=1, trail=False):
    record = {
        "activity_id": activity_id,
        "date": (TODAY - timedelta(days=days_ago)).isoformat(),
        "sport": "run",
        "environment": "outdoor",
        "cs_test_efforts": session_test_efforts(laps),
        "running_profile": {"speed": []},
    }
    if trail:
        record["terrain"] = "trail"
    return record


def _fit(records):
    result = critical_power_speed(records, TODAY, input_fingerprint="fp")
    return next(
        fit
        for fit in result["fits"]
        if fit["sport"] == "run" and fit["environment"] == "outdoor"
    )


class RecognitionTests(unittest.TestCase):
    def test_a_session_with_one_lap_per_distance_is_a_test(self):
        efforts = session_test_efforts(_laps())
        self.assertEqual(
            [(1200, 250), (2400, 550), (3600, 850)],
            [(e["distance_m"], e["duration_seconds"]) for e in efforts],
        )

    def test_lap_distance_within_three_percent_counts(self):
        laps = _laps()
        laps[1]["distance_m"] = 3600 * 1.03
        self.assertIsNotNone(session_test_efforts(laps))
        laps[1]["distance_m"] = 3600 * 1.04
        self.assertIsNone(session_test_efforts(laps))

    def test_repeated_intervals_are_no_test(self):
        laps = _laps() + [_lap(1200, 255, 20000)]
        self.assertIsNone(session_test_efforts(laps))

    def test_a_missing_distance_is_no_test(self):
        self.assertIsNone(session_test_efforts(_laps()[:-2]))

    def test_a_ladder_with_short_jogs_is_no_test(self):
        # 3.6, 2.4 and 1.2 km with 3 min jogs: a workout, not a rested test.
        laps, start = [], 0
        for distance in (3600, 2400, 1200):
            laps.append(_lap(distance, TIMES[distance], start))
            start += TIMES[distance]
            laps.append(_lap(500, 180, start, kind="recovery"))
            start += 180
        self.assertIsNone(session_test_efforts(laps))
        # Twenty minutes between the efforts are enough.
        laps, start = [], 0
        for distance in (3600, 2400, 1200):
            laps.append(_lap(distance, TIMES[distance], start))
            start += TIMES[distance] + 1200
        self.assertIsNotNone(session_test_efforts(laps))

    def test_recovery_laps_never_count(self):
        laps = _laps()
        laps[-2]["kind"] = "recovery"
        self.assertIsNone(session_test_efforts(laps))


class FitTests(unittest.TestCase):
    def test_the_three_laps_recover_cs_and_d_prime_exactly(self):
        fit = _fit([_record("test", _laps())])
        self.assertEqual("ok", fit["status"])
        self.assertEqual("critical-speed-test-laps", fit["method"])
        self.assertEqual(4.0, fit["cs_mps"])
        self.assertEqual(200.0, fit["d_prime_meters"])
        self.assertEqual(1.0, fit["r_squared"])
        self.assertEqual(0.0, fit["see_m"])
        self.assertEqual(
            [250, 550, 850], [e["duration_seconds"] for e in fit["efforts"]]
        )

    def test_noisy_laps_report_the_fit_quality(self):
        # 3600 m in 860 s: distances 1200, 2400, 3600 against 250, 550, 860 s.
        times = {1200: 250, 2400: 550, 3600: 860}
        fit = _fit([_record("test", _laps(times))])
        self.assertEqual("ok", fit["status"])
        # Least squares by hand from the sums, independent of the fit code.
        seconds, meters = (250, 550, 860), (1200, 2400, 3600)
        mean_t, mean_d = sum(seconds) / 3, sum(meters) / 3
        sxy = sum((t - mean_t) * (d - mean_d) for t, d in zip(seconds, meters))
        sxx = sum((t - mean_t) ** 2 for t in seconds)
        slope = sxy / sxx  # 732000 / 186066.67 = 3.93407
        intercept = mean_d - slope * mean_t
        sse = sum((d - slope * t - intercept) ** 2 for t, d in zip(seconds, meters))
        sst = sum((d - mean_d) ** 2 for d in meters)
        self.assertAlmostEqual(3.93407, slope, places=5)
        self.assertAlmostEqual(slope, fit["cs_mps"], places=2)
        self.assertAlmostEqual(intercept, fit["d_prime_meters"], delta=0.5)
        self.assertAlmostEqual(round(1 - sse / sst, 4), fit["r_squared"])
        self.assertAlmostEqual(round(sse**0.5, 1), fit["see_m"])

    def test_the_test_takes_precedence_over_the_window_fit(self):
        # Three sessions on the window model CS 5 m/s, D' 100 m.
        window = [
            {
                "activity_id": f"window{t}",
                "date": (TODAY - timedelta(days=2)).isoformat(),
                "sport": "run",
                "environment": "outdoor",
                "running_profile": {
                    "speed": [
                        {
                            "duration_seconds": t,
                            "speed_mps": 5 + 100 / t,
                            "start_seconds": 0.0,
                        }
                    ]
                },
            }
            for t in (180, 420, 720)
        ]
        self.assertEqual(5.0, _fit(window)["cs_mps"])
        fit = _fit([*window, _record("test", _laps())])
        self.assertEqual(4.0, fit["cs_mps"])
        self.assertEqual("critical-speed-test-laps", fit["method"])

    def test_the_newest_test_wins(self):
        faster = {1200: 240, 2400: 530, 3600: 820}
        fit = _fit(
            [
                _record("old", _laps(), days_ago=10),
                _record("new", _laps(faster), days_ago=2),
            ]
        )
        self.assertEqual("new", fit["efforts"][0]["activity_id"])

    def test_a_trail_run_is_no_test(self):
        fit = _fit([_record("trail", _laps(), trail=True)])
        self.assertNotEqual("critical-speed-test-laps", fit.get("method"))


class StopRuleTests(unittest.TestCase):
    def test_an_effort_over_twenty_minutes_withholds_the_test(self):
        fit = _fit([_record("slow", _laps({1200: 250, 2400: 550, 3600: 1300}))])
        self.assertEqual("withheld", fit["status"])
        self.assertIn("2 bis 20", fit["reason"])

    def test_an_effort_under_two_minutes_withholds_the_test(self):
        fit = _fit([_record("fast", _laps({1200: 100, 2400: 550, 3600: 850}))])
        self.assertEqual("withheld", fit["status"])

    def test_a_non_positive_slope_withholds_the_test(self):
        fit = _fit([_record("odd", _laps({1200: 850, 2400: 550, 3600: 250}))])
        self.assertEqual("withheld", fit["status"])

    def test_an_implausible_d_prime_withholds_the_test(self):
        # CS 3 m/s and D' 600 m (above 500 m): 1200 m in 200 s, 2400 in 600 s,
        # 3600 in 1000 s, all within 2 to 20 min, so the range rule decides.
        fit = _fit([_record("big", _laps({1200: 200, 2400: 600, 3600: 1000}))])
        self.assertEqual("withheld", fit["status"])
        self.assertIn("Plausibilitätsbereich", fit["reason"])

    def test_a_negative_intercept_withholds_the_test(self):
        # d = 4 t - 100 m: 1200 m in 325 s, 2400 in 625 s, 3600 in 925 s.
        fit = _fit([_record("neg", _laps({1200: 325, 2400: 625, 3600: 925}))])
        self.assertEqual("withheld", fit["status"])
        self.assertIn("positiv", fit["reason"])

    def test_a_withheld_test_does_not_fall_back_to_the_window_fit(self):
        fit = _fit([_record("slow", _laps({1200: 250, 2400: 550, 3600: 1300}))])
        self.assertNotIn("cs_mps", fit)


if __name__ == "__main__":
    unittest.main()

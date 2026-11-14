import unittest

from backend.nutrition.fuelling import (
    carbohydrate_per_hour,
    drinking,
    pre_race_carbohydrates,
    sweat_rate,
)


class FuellingRuleTests(unittest.TestCase):
    """Steps of Thomas, Erdman and Burke (2016); athlete ceiling 120 g/h (#2803)."""

    def test_carbohydrates_per_hour_by_duration(self):
        for minutes, step, rate in [
            (44, "none", (0, 0)),
            (45, "mouth_rinse", (None, None)),
            (75, "mouth_rinse", (None, None)),
            (76, "moderate", (30, 60)),
            (150, "moderate", (30, 60)),
            (151, "high", (60, 120)),
        ]:
            with self.subTest(minutes=minutes):
                result = carbohydrate_per_hour(minutes * 60)
                self.assertEqual(
                    (step, rate), (result["step"], (result["low_g"], result["high_g"]))
                )
        high = carbohydrate_per_hour(4 * 3600)
        self.assertEqual((90, True), (high["established_max_g"], high["glucose_fructose"]))
        for value in (None, 0, -60, True, "3600"):
            with self.subTest(value=value):
                self.assertIsNone(carbohydrate_per_hour(value))

    def test_days_before_and_the_pre_race_meal(self):
        for minutes, expected in [
            (
                100,
                [
                    ("loading", 2, [10, 12], [700, 840]),
                    ("meal", None, [1, 4], [70, 280]),
                ],
            ),
            (
                80,
                [
                    ("daily", 1, [7, 12], [490, 840]),
                    ("meal", None, [1, 4], [70, 280]),
                ],
            ),
            (50, [("daily", 1, [7, 12], [490, 840])]),
        ]:
            with self.subTest(minutes=minutes):
                result = pre_race_carbohydrates(minutes * 60, 70)
                self.assertEqual(
                    expected,
                    [
                        (e["kind"], e.get("days_before"), e["g_per_kg"], e["grams"])
                        for e in result
                    ],
                )
        unweighed = pre_race_carbohydrates(100 * 60, None)
        self.assertEqual([None, None], [entry["grams"] for entry in unweighed])
        self.assertEqual([4, 1], unweighed[1]["hours_before"])
        self.assertEqual([], pre_race_carbohydrates(None, 70))

    def test_sweat_rate_prefers_sessions_of_similar_temperature(self):
        # #2804: median of sessions within ±5 °C when at least 3 exist.
        samples = [(0.6, 10.0), (0.7, 12.0), (0.8, 14.0), (1.4, 28.0), (1.5, None)]
        self.assertEqual(
            {"l_per_h": 0.7, "sessions": 3, "similar": True, "temperature_c": [10, 14]},
            sweat_rate(samples, 12.0),
        )
        # Too few similar sessions (or no forecast): all sessions count.
        for temperature in (27.0, None):
            with self.subTest(temperature=temperature):
                result = sweat_rate(samples, temperature)
                self.assertEqual((0.8, 5, False), (
                    result["l_per_h"], result["sessions"], result["similar"]
                ))
        self.assertIsNone(sweat_rate([], 12.0))

    def test_drinking_keeps_the_loss_at_two_percent_and_never_above_the_sweat_rate(self):
        # 0.9 L/h for 2:45 h is a loss of 2.475 L; 2 % of 70 kg is 1.4 L, so
        # 1.075 L remain to drink: 0.39 L/h, 1.1 L in total (worked by hand).
        self.assertEqual(
            {"max_l_per_h": 0.9, "l_per_h": 0.39, "total_l": 1.1, "sodium_g_per_l": [0.5, 0.7]},
            drinking(0.9, 9900, 70),
        )
        # A short session within the 2 % needs nothing; without weight only the bound.
        self.assertEqual(
            {"max_l_per_h": 0.5, "l_per_h": 0.0, "total_l": 0.0}, drinking(0.5, 3600, 70)
        )
        self.assertEqual({"max_l_per_h": 0.9}, drinking(0.9, 3600, None))


if __name__ == "__main__":
    unittest.main()

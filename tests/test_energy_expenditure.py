"""Skinfold body fat, Katch-McArdle resting energy and adaptive expenditure (#3097)."""

import unittest
from datetime import date, timedelta

from backend.nutrition.energy_expenditure import (
    ENERGY_PER_KG,
    Insufficient,
    Sex,
    adaptive_expenditure,
    body_density,
    katch_mcardle_rmr,
    siri_body_fat_pct,
)

END = date(2026, 11, 15)


def days(count, value):
    """``count`` days ending on END, each with ``value(i)`` (i = 0 is the first)."""
    return {END - timedelta(days=count - 1 - i): value(i) for i in range(count)}


class SkinfoldBodyFatTests(unittest.TestCase):
    def test_men_match_the_published_example(self):
        # Livestrong worked example (Jackson & Pollock 1978): 40-year-old man,
        # chest 16, abdomen 20, thigh 17 mm -> density 1.0597633, Siri 17.1 %.
        density = body_density(Sex.MALE, {"chest": 16, "abdomen": 20, "thigh": 17}, 40)
        self.assertAlmostEqual(density, 1.0597633, places=7)
        self.assertAlmostEqual(siri_body_fat_pct(density), 17.1, places=1)

    def test_women_use_their_own_sites_and_coefficients(self):
        # Jackson, Pollock & Ward (1980): 1.0994921 - 0.0009929*47 + 0.0000023*47^2
        # - 0.0001392*30 = 1.0537305 by hand.
        folds = {"triceps": 15, "suprailiac": 12, "thigh": 20}
        density = body_density(Sex.FEMALE, folds, 30)
        self.assertAlmostEqual(density, 1.0537305, places=7)
        self.assertAlmostEqual(siri_body_fat_pct(density), 19.76, places=2)

    def test_wrong_sites_or_values_are_refused(self):
        for sex, folds, age in (
            (Sex.MALE, {"triceps": 15, "suprailiac": 12, "thigh": 20}, 30),
            (Sex.MALE, {"chest": 16, "abdomen": 20}, 30),
            (Sex.MALE, {"chest": 1, "abdomen": 20, "thigh": 17}, 30),
            (Sex.MALE, {"chest": 16, "abdomen": 95, "thigh": 17}, 30),
            (Sex.FEMALE, {"triceps": 15, "suprailiac": 12, "thigh": 20}, 12),
        ):
            with (
                self.subTest(sex=sex, folds=folds, age=age),
                self.assertRaises(ValueError),
            ):
                body_density(sex, folds, age)


class KatchMcArdleTests(unittest.TestCase):
    def test_resting_energy_from_lean_mass(self):
        # 80 kg at 15 % -> 68 kg lean -> 370 + 21.6*68 = 1838.8 kcal; at 20 % -> 1752.4.
        self.assertAlmostEqual(katch_mcardle_rmr(80, 15), 1838.8, places=6)
        self.assertAlmostEqual(katch_mcardle_rmr(80, 20), 1752.4, places=6)
        with self.assertRaises(ValueError):
            katch_mcardle_rmr(80, 0)


class AdaptiveExpenditureTests(unittest.TestCase):
    def estimate(self, intake, weights):
        return adaptive_expenditure(intake, weights, END)

    def test_a_stable_weight_gives_the_mean_intake(self):
        intake = days(28, lambda i: 2500 + (100 if i % 2 else -100))
        weights = days(28, lambda i: 75.0 + (0.3 if i % 3 == 0 else -0.15))
        result = self.estimate(intake, weights)
        self.assertAlmostEqual(result.mean_intake_kcal, 2500, places=6)
        self.assertLess(abs(result.kcal - 2500), 60)
        self.assertGreater(result.band_kcal, 0)
        self.assertEqual(
            (result.intake_days, result.weighins, result.insufficient), (28, 28, None)
        )

    def test_losing_weight_adds_the_released_energy(self):
        # 0.5 kg a week down on 2000 kcal: 0.5/7 kg/day * 7700 = 550 kcal from stores.
        intake = days(28, lambda i: 2000)
        weights = days(28, lambda i: 80.0 - 0.5 / 7 * i)
        result = self.estimate(intake, weights)
        self.assertAlmostEqual(result.kcal, 2000 + 0.5 / 7 * ENERGY_PER_KG, places=6)
        self.assertAlmostEqual(result.weight_change_kg_per_week, -0.5, places=9)

    def test_gaining_weight_subtracts_the_stored_energy(self):
        intake = days(28, lambda i: 3000)
        weights = days(28, lambda i: 70.0 + 0.25 / 7 * i)
        self.assertAlmostEqual(self.estimate(intake, weights).kcal, 2725, places=6)

    def test_gaps_lower_the_basis_and_too_few_days_give_no_estimate(self):
        logged = days(28, lambda i: 2400 if i % 2 else 3000)
        weights = days(28, lambda i: 72.0)
        every_other = {day: kcal for day, kcal in logged.items() if kcal == 2400}
        result = self.estimate(every_other, weights)
        self.assertEqual((result.kcal, result.intake_days), (2400, 14))
        every_third = {
            day: kcal for n, (day, kcal) in enumerate(logged.items()) if n % 3 == 0
        }
        result = self.estimate(every_third, weights)
        self.assertEqual(
            (result.kcal, result.insufficient), (None, Insufficient.INTAKE_DAYS)
        )
        self.assertEqual(result.intake_days, len(every_third))

    def test_missing_weigh_ins_or_a_short_span_give_no_estimate(self):
        intake = days(28, lambda i: 2400)
        few = {day: 72.0 for n, day in enumerate(days(28, lambda i: 0)) if n % 4 == 0}
        self.assertEqual(self.estimate(intake, few).insufficient, Insufficient.WEIGHINS)
        recent = days(10, lambda i: 72.0)
        self.assertEqual(
            self.estimate(intake, recent).insufficient, Insufficient.WEIGHT_SPAN
        )

    def test_days_outside_the_window_are_ignored(self):
        intake = {
            **days(28, lambda i: 2400),
            END - timedelta(days=40): 9000,
            END + timedelta(days=1): 9000,
        }
        weights = {**days(28, lambda i: 72.0), END - timedelta(days=40): 90.0}
        result = self.estimate(intake, weights)
        self.assertAlmostEqual(result.kcal, 2400, places=6)
        self.assertEqual((result.intake_days, result.weighins), (28, 28))


if __name__ == "__main__":
    unittest.main()

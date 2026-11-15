"""Adaptive TDEE on synthetic intake and weight series with known truth (#3100)."""

from __future__ import annotations

import unittest
from collections.abc import Callable
from datetime import date, timedelta

from backend.nutrition import energy_balance as balance

END = date(2026, 11, 15)
TRUE_TDEE = 2600.0
# Default composition: 0.75 x 9,441 + 0.25 x 1,816 = 7,534.75 kcal/kg.
DENSITY = 7534.75


def constant(value: float) -> Callable[[int], float]:
    """The same true intake on every day."""
    return lambda _n: value


def series(
    intake_of: Callable[[int], float],
    days: int = 120,
    flagged_of: Callable[[int], bool] = lambda n: False,
    logged_factor: float = 1.0,
    skip: set[int] | None = None,
    weigh_every: int = 1,
) -> balance.EnergyInputs:
    """Days 0..days-1 ending on END; weight follows the true energy balance.

    Flagged days are logged ``logged_factor`` times their true intake.
    Day n's weight is the start weight plus the stored energy up to day n.
    """
    weight = 80.0
    out: dict[date, balance.DayInputs] = {}
    for n in range(days):
        day = END - timedelta(days=days - 1 - n)
        true = intake_of(n)
        weight += (true - TRUE_TDEE) / DENSITY
        if skip and n in skip:
            continue
        flagged = flagged_of(n)
        logged = true * logged_factor if flagged else true
        out[day] = balance.DayInputs(
            intake_kcal=logged,
            protein_g=150,
            carbohydrate_g=300,
            fat_g=80,
            entries=3,
            flagged=flagged,
            weight_kg=weight if n % weigh_every == 0 else None,
            activity_kcal=600.0,
        )
    return balance.EnergyInputs(out, {}, rmr_kcal=1700.0, rmr_source="katch_mcardle")


class CompositionAndTrendTests(unittest.TestCase):
    def test_energy_density_follows_forbes_and_hall(self) -> None:
        self.assertAlmostEqual(balance.energy_density(80.0, None), DENSITY)
        # 75 kg at 15 %: fat mass 11.25 kg, share 11.25 / 21.65 = 0.51963;
        # 0.51963 x 9,441 + 0.48037 x 1,816 = 5,778.2 kcal/kg.
        self.assertAlmostEqual(balance.energy_density(75.0, 15.0), 5778.2, places=0)

    def test_trend_moves_halfway_in_one_half_life(self) -> None:
        start = date(2026, 1, 1)
        weights = {start: 80.0}
        weights.update({start + timedelta(days=n): 70.0 for n in range(1, 11)})
        trend = balance.weight_trend(weights)
        self.assertAlmostEqual(trend[start + timedelta(days=10)], 75.0, places=6)
        self.assertEqual(balance.weight_trend({}), {})


class AdaptiveTdeeTests(unittest.TestCase):
    def assert_recovers_tdee(self, inputs: balance.EnergyInputs) -> None:
        window = balance.energy_windows(inputs, END, weeks=1)[-1]
        assert window.adaptive_tdee is not None
        self.assertIsNone(window.insufficient)
        self.assertAlmostEqual(window.adaptive_tdee.kcal, TRUE_TDEE, delta=60.0)

    def test_stable_losing_and_gaining_recover_the_true_tdee(self) -> None:
        for intake in (2600.0, 2100.0, 3100.0):
            with self.subTest(intake=intake):
                self.assert_recovers_tdee(series(constant(intake)))

    def test_gaps_give_a_reason_instead_of_a_value(self) -> None:
        # Two weeks missing from the last window: too few complete days.
        sparse = series(constant(2600.0), skip=set(range(105, 120)))
        window = balance.energy_windows(sparse, END, weeks=1)[-1]
        self.assertEqual(window.insufficient, "intake_days")
        self.assertIsNone(window.adaptive_tdee)
        # Weekly weigh-ins only: too few in 28 days.
        weekly = series(constant(2600.0), weigh_every=7)
        self.assertEqual(
            balance.energy_windows(weekly, END, weeks=1)[-1].insufficient, "weighins"
        )

    def test_existing_history_counts_on_the_first_read(self) -> None:
        # 41 logged and weighed days before the first read: windows ending on
        # END and END-7 are full, END-14 holds 27 days (span 26), END-21 holds
        # 20 days but only a 19-day weight span, END-28 only 13 days.
        windows = balance.energy_windows(series(constant(2600.0), days=41), END)
        reasons = [w.insufficient for w in windows[-5:]]
        self.assertEqual(reasons, ["intake_days", "weight_span", None, None, None])
        self.assertEqual(sum(1 for w in windows if w.adaptive_tdee), 3)
        # Three windows are below the four the fit needs: still the prior 1.
        self.assertEqual(windows[-1].calibration_windows, 3)
        self.assertEqual(windows[-1].activity_kcal, windows[-1].activity_kcal_raw)
        tdee = windows[-1].adaptive_tdee
        assert tdee is not None
        self.assertAlmostEqual(tdee.kcal, TRUE_TDEE, delta=60)
        self.assertEqual(balance.history_start(END), END - timedelta(days=104))

    def test_incomplete_days_are_dropped(self) -> None:
        day = balance.DayInputs(intake_kcal=800, entries=3, activity_kcal=600)
        self.assertFalse(balance.is_complete(day, 1700.0))
        self.assertFalse(
            balance.is_complete(balance.DayInputs(2600, entries=1), 1700.0)
        )
        self.assertTrue(balance.is_complete(balance.DayInputs(2600, entries=2), 1700.0))

    def test_derived_rmr_is_tdee_minus_activity_and_tef(self) -> None:
        window = balance.energy_windows(series(constant(2600.0)), END, weeks=1)[-1]
        tdee, rmr = window.adaptive_tdee, window.derived_rmr
        activity, tef = window.activity_kcal, window.tef_kcal
        assert tdee and rmr and activity is not None and tef is not None
        # 0.25 x 600 + 0.10 x 1,200 + 0.015 x 720 = 280.8 kcal TEF per day.
        self.assertAlmostEqual(tef, 280.8)
        self.assertAlmostEqual(rmr.kcal, tdee.kcal - activity - tef)
        # The resting reference keeps its source label (own formula first).
        self.assertEqual(window.rmr_source, "katch_mcardle")


class FlaggedMealsAndCalibrationTests(unittest.TestCase):
    def test_flagged_days_widen_the_band_and_the_bias_is_recovered(self) -> None:
        # Flagged meals logged 25 % too high: true = logged / 1.25, a -20 % bias.
        # The share of flagged days changes from week to week.
        def flagged(n: int) -> bool:
            return (n % 7) < (n // 7) % 5

        inputs = series(constant(2600.0), flagged_of=flagged, logged_factor=1.25)
        bias = balance.flagged_meal_bias(inputs, END)
        assert bias is not None
        self.assertAlmostEqual(bias[0], -20.0, delta=5.0)
        window = balance.energy_windows(inputs, END, weeks=1)[-1]
        assert window.adaptive_tdee and window.adaptive_tdee_unflagged
        self.assertGreater(window.flagged_days, 0)
        self.assertAlmostEqual(window.adaptive_tdee_unflagged.kcal, TRUE_TDEE, delta=60)
        self.assertIsNone(balance.flagged_meal_bias(series(constant(2600.0)), END))

    def test_calibration_factor_is_shrunk_and_clamped(self) -> None:
        def window(raw: float, tdee: float) -> balance.EnergyWindow:
            return balance.EnergyWindow(
                END, 28, 28, 0, 28, raw, raw, 280.0,
                balance.Estimate(tdee, 100.0), None, None, None,
            )  # fmt: skip

        # TDEE - TEF = 1,700 + 1.10 x activity exactly: no noise, slope kept.
        exact = [window(a, 1700 + 1.10 * a + 280) for a in (400, 500, 600, 700)]
        self.assertAlmostEqual(balance.calibration_factor(exact), 1.10)
        steep = [window(a, 1700 + 1.5 * a + 280) for a in (400, 500, 600, 700)]
        self.assertEqual(balance.calibration_factor(steep), 1.15)
        # Noise orthogonal to activity keeps the slope at 1.10; its residual
        # variance lets the prior pull the factor towards 1.
        noisy = [
            window(a, 1700 + 1.10 * a + 280 + e)
            for a, e in ((400, 150), (500, -150), (600, -150), (700, 150))
        ]
        self.assertTrue(1.0 < balance.calibration_factor(noisy) < 1.10)
        self.assertEqual(balance.calibration_factor(exact[:3]), 1.0)


if __name__ == "__main__":
    unittest.main()

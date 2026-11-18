"""Adaptive TDEE on synthetic intake and weight series with known truth (#3100)."""

from __future__ import annotations

import math
import unittest
from collections.abc import Callable
from dataclasses import replace
from datetime import date, timedelta

from backend.nutrition import energy_balance as balance
from backend.performance.activity_energy import TEF_ERROR_KCAL

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
    split: tuple[float, float] | None = None,
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
            flagged_kcal=logged if flagged else 0.0,
            weight_kg=weight if n % weigh_every == 0 else None,
            activity_kcal=600.0,
            # Its M11 error (#3552): 10 % of the activity, as a ride's.
            activity_error_kcal=60.0,
            neat_kcal=split[0] if split else None,
            sport_kcal=split[1] if split else None,
        )
    return balance.EnergyInputs(out, {}, rmr_kcal=1700.0, rmr_source="katch_mcardle")


class CompositionAndTrendTests(unittest.TestCase):
    def test_energy_density_follows_forbes_and_hall(self) -> None:
        self.assertAlmostEqual(balance.energy_density(80.0, None), DENSITY)
        # 75 kg at 15 %: fat mass 11.25 kg, share 11.25 / 21.65 = 0.51963;
        # 0.51963 x 9,441 + 0.48037 x 1,816 = 5,778.2 kcal/kg.
        self.assertAlmostEqual(balance.energy_density(75.0, 15.0), 5778.2, places=0)

    def test_the_current_weight_follows_a_steady_change_without_lag(self) -> None:
        # #3551: 80 kg today after a steady loss (or gain) of 1 kg per week,
        # with a scale that reads 0.3 kg high and low on alternate weigh-ins;
        # the exponential trend lagged 2 to 3.9 kg here.
        for per_week in (-1.0, 1.0):
            for every in (1, 2):
                with self.subTest(per_week=per_week, every=every):
                    weights = {
                        END - timedelta(days=n): 80.0
                        - per_week * n / 7
                        + (0.3 if (n // every) % 2 else -0.3)
                        for n in range(0, 56, every)
                    }
                    trend = balance.weight_trend(weights)
                    self.assertLess(abs(trend[END] - 80.0), 0.3)
        # Too few weigh-ins for a line: their mean; after a gap longer than
        # the line's span, the last value carries.
        start = date(2026, 1, 1)
        sparse = balance.weight_trend({start: 81.0, start + timedelta(days=3): 80.0})
        self.assertEqual(80.5, sparse[start + timedelta(days=3)])
        far = start + timedelta(days=40)
        gap = balance.weight_trend({start: 81.0, far: 79.0})
        self.assertEqual(81.0, gap[far - timedelta(days=1)])
        self.assertEqual(79.0, gap[far])
        self.assertEqual(balance.weight_trend({}), {})


class MeasuredFatChangeTests(unittest.TestCase):
    """#3477: two dated body fat measurements of one method give the measured
    fat-mass change; otherwise Forbes stays."""

    START = END - timedelta(days=27)

    def inputs(self, skinfolds=None, device=None) -> balance.EnergyInputs:
        return balance.EnergyInputs(
            {}, skinfolds or {}, rmr_kcal=1700.0, device_body_fat=device or {}
        )

    def test_two_skinfolds_give_the_measured_change(self) -> None:
        weights = {self.START: 80.0, END: 78.0}
        measured = balance._measured_store(
            self.inputs({self.START: 20.0, END: 19.0}), weights, self.START, END
        )
        assert measured is not None
        # Fat 16.0 -> 14.82 kg (-1.18), lean the rest of -2.0 kg (-0.82):
        # (-1.18 x 9,441 - 0.82 x 1,816) / 27 days = -467.8 kcal/day.
        self.assertAlmostEqual(measured[0], -467.8, places=1)
        # 1 percentage point per measurement: hypot(80, 78) x 1 % = 1.117 kg
        # of fat, x (9,441 - 1,816) / 27 = 315.5 kcal/day.
        self.assertAlmostEqual(measured[1], 315.5, places=1)

    def test_one_measurement_or_mixed_methods_keep_forbes(self) -> None:
        weights = {self.START: 80.0, END: 78.0}
        for inputs in (
            self.inputs({END: 19.0}),
            # A skinfold and an entered value are never mixed.
            self.inputs({self.START: 20.0}, {END: (19.0, None)}),
            # Outside the window it does not count.
            self.inputs({self.START - timedelta(days=1): 20.0, END: 19.0}),
            # Two values one day apart would divide the noise by one day (Gitar).
            self.inputs(
                device={END - timedelta(days=1): (20.0, None), END: (19.5, None)}
            ),
        ):
            with self.subTest(inputs.body_fat_pct):
                self.assertIsNone(
                    balance._measured_store(inputs, weights, self.START, END)
                )
        # Two entered values (the profile history counts as entered) do count.
        entered = self.inputs(device={self.START: (20.0, None), END: (19.0, None)})
        self.assertIsNotNone(balance._measured_store(entered, weights, self.START, END))

    def test_the_store_carries_its_terms_for_the_deficit_card(self) -> None:
        # #3548: the boxes show weight, body fat, the fat kg and the span.
        weights = {self.START: 80.0, END: 78.0}
        measured = balance._measured_store(
            self.inputs({self.START: 20.0, END: 19.0}), weights, self.START, END
        )
        assert measured is not None
        self.assertEqual(
            ("measured", 27, 80.0, 78.0, 20.0, 19.0, "body_fat"),
            (
                measured.method,
                measured.days,
                measured.weight_start_kg,
                measured.weight_end_kg,
                measured.fat_pct_start,
                measured.fat_pct_end,
                measured.uncertainty,
            ),
        )
        self.assertAlmostEqual(measured.fat_change_kg, -1.18, places=2)
        # Forbes: the fitted line from the first to the last weigh-in, the
        # share from the latest body fat, which is its only percentage.
        line = {self.START + timedelta(days=n): 80.0 - n / 13.5 for n in range(28)}
        forbes = balance._stored_energy(
            self.inputs({END: 19.0}), balance.weight_trend(line), line, self.START, END
        )
        self.assertEqual(
            ("forbes", 27, None, 19.0),
            (forbes.method, forbes.days, forbes.fat_pct_start, forbes.fat_pct_end),
        )
        self.assertAlmostEqual(forbes.weight_start_kg, 80.0, places=6)
        self.assertAlmostEqual(forbes.weight_end_kg, 78.0, places=6)
        share = balance.fat_share(78.0, 19.0)
        self.assertAlmostEqual(forbes.fat_change_kg, -2.0 * share, places=1)
        # A straight line has no scatter: the fat share is the main uncertainty.
        self.assertEqual("fat_share", forbes.uncertainty)
        self.assertAlmostEqual(
            forbes.kcal_per_day * 27,
            forbes.fat_change_kg * balance.FAT_KCAL_PER_KG
            + (-2.0 - forbes.fat_change_kg) * balance.LEAN_KCAL_PER_KG,
            delta=40,
        )

    def test_the_window_names_its_method(self) -> None:
        inputs = series(constant(2100.0), days=28)
        first, last = min(inputs.days), max(inputs.days)
        for fats, method in (({}, "forbes"), ({first: 20.0, last: 19.5}, "measured")):
            with self.subTest(method):
                window = balance._window(
                    replace(inputs, body_fat_pct=fats),
                    balance.weight_trend(
                        {d: v.weight_kg for d, v in inputs.days.items() if v.weight_kg}
                    ),
                    END,
                    1.0,
                )
                self.assertEqual(method, window.store_method)


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
        # Losing 500 kcal/day from the first weigh-in: the weight falls by
        # 500 / 7,534.75 kg a day, which the weigh-ins' slope sees from day one,
        # so even the window starting at the first weigh-in shows 2,600.
        losing = balance.energy_windows(series(constant(2100.0), days=41), END)
        for window in losing[-3:]:
            assert window.adaptive_tdee is not None
            self.assertAlmostEqual(window.adaptive_tdee.kcal, TRUE_TDEE, delta=1.0)

    def test_the_band_uses_t_and_the_density_uncertainty(self) -> None:
        # Two-sided 95 % t quantiles from the standard table (#3187 item 10).
        self.assertEqual(balance.t_quantile(6), 2.447)
        self.assertEqual(balance.t_quantile(35), 2.042)
        self.assertEqual(balance.t_quantile(500), 1.96)
        # Losing 500 kcal/day with exact weigh-ins and constant intake: only the
        # density term is left. Slope 500 / 7,534.75 = 0.066359 kg/day; density
        # SD 0.144 x (9,441 - 1,816) = 1,098.0 kcal/kg; 72.862 kcal/day. 28 days
        # and 28 weigh-ins give min(27, 26) dof: t = 2.056, band 149.80 kcal.
        window = balance.energy_windows(series(constant(2100.0)), END, weeks=1)[-1]
        assert window.adaptive_tdee is not None
        self.assertAlmostEqual(window.adaptive_tdee.band_kcal, 149.80, delta=0.05)

    def test_incomplete_days_are_dropped(self) -> None:
        day = balance.DayInputs(intake_kcal=800, entries=3, activity_kcal=600)
        self.assertFalse(balance.is_complete(day, 1700.0))
        self.assertFalse(
            balance.is_complete(balance.DayInputs(2600, entries=1), 1700.0)
        )
        self.assertTrue(balance.is_complete(balance.DayInputs(2600, entries=2), 1700.0))

    def test_the_activity_splits_into_neat_and_sessions(self) -> None:
        # 450 kcal NEAT and 150 kcal sessions a day (split A, #3100): the parts
        # add up to the 600 kcal activity, raw and times the factor k.
        window = balance.energy_windows(
            series(constant(2600.0), split=(450.0, 150.0)), END, weeks=1
        )[-1]
        self.assertEqual(
            (window.neat_kcal_raw, window.sport_kcal_raw, window.activity_kcal_raw),
            (450.0, 150.0, 600.0),
        )
        # One window is below the four the fit needs: k is the prior 1.
        self.assertEqual(window.calibration_factor, 1.0)
        self.assertEqual((window.neat_kcal, window.sport_kcal), (450.0, 150.0))
        # A known activity without its parts leaves the split unknown, not 0.
        unsplit = balance.energy_windows(series(constant(2600.0)), END, weeks=1)[-1]
        self.assertEqual(unsplit.activity_kcal_raw, 600.0)
        self.assertEqual((unsplit.neat_kcal_raw, unsplit.sport_kcal), (None, None))

    def test_the_calibrated_split_scales_both_parts_by_k(self) -> None:
        # A window built with the factor 1.1 scales both parts by it.
        inputs = series(constant(2600.0), split=(450.0, 150.0))
        weights = {
            d: v.weight_kg for d, v in inputs.days.items() if v.weight_kg is not None
        }
        window = balance._window(inputs, balance.weight_trend(weights), END, 1.1)
        self.assertEqual(window.calibration_factor, 1.1)
        self.assertAlmostEqual(window.neat_kcal or 0.0, 495.0)
        self.assertAlmostEqual(window.sport_kcal or 0.0, 165.0)
        self.assertAlmostEqual(window.activity_kcal or 0.0, 660.0)

    def test_derived_rmr_is_tdee_minus_activity_and_tef(self) -> None:
        window = balance.energy_windows(series(constant(2600.0)), END, weeks=1)[-1]
        tdee, rmr = window.adaptive_tdee, window.derived_rmr
        activity, tef = window.activity_kcal, window.tef_kcal
        assert tdee and rmr and activity is not None and tef is not None
        # 0.25 x 600 + 0.10 x 1,200 + 0.015 x 720 = 280.8 kcal TEF per day.
        self.assertAlmostEqual(tef, 280.8)
        self.assertAlmostEqual(rmr.kcal, tdee.kcal - activity - tef)
        # #3552: its band adds the activity's own error and digestion's, not a
        # flat 10 % of the activity.
        self.assertAlmostEqual(
            rmr.band_kcal,
            math.hypot(
                tdee.band_kcal,
                window.calibration_factor * (window.activity_error_kcal_raw or 0.0),
                TEF_ERROR_KCAL,
            ),
        )
        # The window keeps the days' mean error: method errors do not average out.
        self.assertAlmostEqual(60.0, window.activity_error_kcal_raw or 0.0)
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
        # The view reads the bias from windows it already built (Gitar on #3284).
        self.assertEqual(
            bias, balance.windows_bias(balance.energy_windows(inputs, END))
        )
        assert bias is not None
        self.assertAlmostEqual(bias[0], -20.0, delta=5.0)
        window = balance.energy_windows(inputs, END)[-1]
        # The main value keeps every day, so it carries the logging error...
        assert window.adaptive_tdee and window.adaptive_tdee_corrected
        self.assertGreater(window.flagged_days, 0)
        self.assertGreater(window.adaptive_tdee.kcal, TRUE_TDEE + 60)
        # ...which the learned correction (flagged kcal x (1 + b)) removes.
        self.assertAlmostEqual(window.adaptive_tdee_corrected.kcal, TRUE_TDEE, delta=60)
        # A window with flagged days has no value without them (#3187 item 14).
        self.assertIsNone(window.adaptive_tdee_unflagged)
        self.assertIsNone(balance.flagged_meal_bias(series(constant(2600.0)), END))
        self.assertIsNone(
            balance.energy_windows(series(constant(2600.0)), END)[
                -1
            ].adaptive_tdee_corrected
        )

    def test_the_unflagged_value_comes_only_from_windows_without_flags(self) -> None:
        # Flags only in the first 60 days: the last window (days 92-119) has none.
        inputs = series(
            constant(2600.0), flagged_of=lambda n: n < 60 and n % 3 == 0,
            logged_factor=1.25,
        )  # fmt: skip
        latest = balance.energy_windows(inputs, END)[-1]
        self.assertEqual(latest.flagged_days, 0)
        self.assertEqual(latest.adaptive_tdee_unflagged, latest.adaptive_tdee)

    def test_only_the_flagged_entries_widen_the_band(self) -> None:
        # 2,600 kcal days, every third with an estimated meal: of 500 kcal, or
        # the whole day. 0.25 x 500 per flagged day widens the band less.
        def days(flagged_kcal: float) -> balance.EnergyInputs:
            base = series(constant(2600.0))
            out = {
                d: replace(
                    v, flagged=i % 3 == 0, flagged_kcal=flagged_kcal * (i % 3 == 0)
                )
                for i, (d, v) in enumerate(sorted(base.days.items()))
            }
            return replace(base, days=out)

        meal = balance.energy_windows(days(500.0), END, weeks=1)[-1].adaptive_tdee
        whole = balance.energy_windows(days(2600.0), END, weeks=1)[-1].adaptive_tdee
        assert meal and whole
        self.assertLess(meal.band_kcal, whole.band_kcal)
        # A flagged entry of 0 kcal (a flagged drink) widens nothing: the day is
        # not counted whole (Gitar on #3195).
        drink = balance.energy_windows(days(0.0), END, weeks=1)[-1].adaptive_tdee
        plain = balance.energy_windows(series(constant(2600.0)), END, weeks=1)[-1]
        assert drink and plain.adaptive_tdee
        self.assertEqual(drink.band_kcal, plain.adaptive_tdee.band_kcal)

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

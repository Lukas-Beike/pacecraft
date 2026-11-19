"""Adaptive TDEE on synthetic intake and weight series with known truth (#3100)."""

from __future__ import annotations

import math
import unittest
from collections.abc import Callable
from dataclasses import replace
from datetime import date, timedelta

from backend.athlete import body_records
from backend.nutrition import energy_balance as balance
from backend.performance.activity_energy import TEF_ERROR_KCAL

END = date(2026, 11, 15)
TRUE_TDEE = 2600.0
# The prior fat share alone (#3555): 0.70 x 9,441 + 0.30 x 1,816 = 7,153.5 kcal/kg.
DENSITY = 7153.5
BAND_Z = balance.BAND_Z


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
    step_at: int = 10**6,
) -> balance.EnergyInputs:
    """Days 0..days-1 ending on END; weight follows the true energy balance.

    Flagged days are logged ``logged_factor`` times their true intake.
    Day n's weight is the start weight plus the stored energy up to day n,
    plus 1 kg of water and glycogen from day ``step_at`` on (#3614).
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
            weight_kg=weight + float(n >= step_at) if n % weigh_every == 0 else None,
            activity_kcal=600.0,
            # Its M11 error (#3552): 10 % of the activity, as a ride's.
            activity_error_kcal=60.0,
            neat_kcal=split[0] if split else None,
            sport_kcal=split[1] if split else None,
        )
    return balance.EnergyInputs(out, {}, rmr_kcal=1700.0, rmr_source="katch_mcardle")


def bias_window(
    x: float, activity: float | None, back: int, noise: float = 0.0
) -> balance.EnergyWindow:
    """#3586: flagged meals 20 % larger than logged, TDEE = 2,600 - 0.2 x, in
    a window ending ``back`` days before END."""
    return balance.EnergyWindow(
        END - timedelta(days=back), 28, 28, 1, 28, activity, activity, 280.0,
        balance.Estimate(2600.0 - 0.2 * x + noise, 100.0), None, None, None,
        flagged_kcal_per_day=x,
    )  # fmt: skip


class CompositionAndTrendTests(unittest.TestCase):
    def test_energy_density_follows_the_share_and_hall(self) -> None:
        self.assertAlmostEqual(balance.energy_density(0.70), DENSITY)
        self.assertAlmostEqual(balance.energy_density(1.0), 9441.0)

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
                    trend = body_records.weight_trend(weights)
                    self.assertLess(abs(trend[END] - 80.0), 0.3)
        # Too few weigh-ins for a line: their mean; after a gap longer than
        # the line's span, the last value carries.
        start = date(2026, 1, 1)
        sparse = body_records.weight_trend(
            {start: 81.0, start + timedelta(days=3): 80.0}
        )
        self.assertEqual(80.5, sparse[start + timedelta(days=3)])
        far = start + timedelta(days=40)
        gap = body_records.weight_trend({start: 81.0, far: 79.0})
        self.assertEqual(81.0, gap[far - timedelta(days=1)])
        self.assertEqual(79.0, gap[far])
        self.assertEqual(body_records.weight_trend({}), {})


class FatShareBlendTests(unittest.TestCase):
    """#3555: the fat share blends the prior 0.70 +- 0.15 with the slope of the
    phase's measured fat mass, weighted by their precisions."""

    def phase(self, weeks: int, every: int = 7, true_share: float = 0.80):
        """85 kg losing 0.5 kg/week; body fat measured every ``every`` days as if
        ``true_share`` of the loss were fat (from 20 %), without noise."""
        first = END - timedelta(days=7 * weeks)
        days, fats = {}, {}
        for n in range(7 * weeks + 1):
            day = first + timedelta(days=n)
            weight = 85.0 - 0.5 * n / 7
            days[day] = balance.DayInputs(weight_kg=weight)
            if n % every == 0:
                fat_mass = 17.0 + true_share * (weight - 85.0)
                fats[day] = 100.0 * fat_mass / weight
        inputs = balance.EnergyInputs(days, fats, rmr_kcal=1700.0)
        trend = body_records.weight_trend(
            {d: v.weight_kg for d, v in days.items() if v.weight_kg is not None}
        )
        return inputs, trend

    def test_without_two_measurements_the_prior_stands_alone(self) -> None:
        inputs, trend = self.phase(4)
        for fats in ({}, {END: 19.0}):
            with self.subTest(fats=fats):
                share = balance.fat_share(
                    replace(inputs, body_fat_pct=fats), trend, END
                )
                self.assertEqual((0.70, 0.15, 0.0), share[:3])
        # A skinfold and an entered value are never mixed into one slope.
        mixed = replace(
            inputs,
            body_fat_pct={END - timedelta(days=21): 20.0},
            device_body_fat={END: (19.0, None)},
        )
        self.assertEqual(0.0, balance.fat_share(mixed, trend, END).measured_weight)

    def test_the_share_names_the_dates_of_its_first_and_last_reading(self) -> None:
        # #3598: the phase's readings start 12 weeks back, long before a 28-day
        # window; the card names that span, so the share carries its dates.
        inputs, trend = self.phase(12)
        share = balance.fat_share(inputs, trend, END)
        self.assertEqual(
            (END - timedelta(days=84), END), (share.day_first, share.day_last)
        )
        weights = {d: v.weight_kg for d, v in inputs.days.items() if v.weight_kg}
        recent = {d: w for d, w in weights.items() if d > END - timedelta(days=28)}
        store = balance._stored_energy(inputs, trend, recent, END)
        self.assertEqual(
            (END - timedelta(days=84), END), (store.fat_date_start, store.fat_date_end)
        )
        # One reading alone: only its date, as its only percentage.
        single = balance.fat_share(
            replace(inputs, body_fat_pct={END: 19.0}), trend, END
        )
        self.assertEqual((None, END), (single.day_first, single.day_last))

    def test_two_readings_14_days_apart_stay_near_the_prior(self) -> None:
        # Repeatability 2 points: 84 kg x 2 % = 1.68 kg of fat per reading over
        # a 1 kg loss give the measured share an SD of about 2.4, so it weighs
        # little and the share stays near 0.70 with a narrow band.
        inputs, trend = self.phase(2, every=14)
        share = balance.fat_share(inputs, trend, END)
        self.assertLess(share.measured_weight, 0.01)
        self.assertAlmostEqual(0.70, share.share, delta=0.01)
        self.assertAlmostEqual(0.15, share.sd, delta=0.001)

    def test_weekly_readings_weigh_more_the_longer_they_run(self) -> None:
        weights = []
        for weeks in (12, 24):
            inputs, trend = self.phase(weeks)
            share = balance.fat_share(inputs, trend, END)
            weights.append(share.measured_weight)
            # The share moves from the prior towards the measured 0.80.
            self.assertGreater(share.share, 0.70)
            self.assertLess(share.share, 0.80)
            self.assertLess(share.sd, 0.15)
        # 12 weeks, 13 readings: measured SD 2 % x 82 kg / sqrt(sxx = 8,918
        # days^2) / 0.0714 kg/day = 0.243, weight 0.15^2 / (0.15^2 + 0.243^2) =
        # 0.28; 24 weeks, 25 readings: sxx 61,250, SD 0.091, weight 0.73.
        self.assertAlmostEqual(0.28, weights[0], delta=0.01)
        self.assertAlmostEqual(0.73, weights[1], delta=0.02)

    def test_the_athletes_own_repeatability_sets_the_skinfolds_weight(self) -> None:
        # #3556: 1 point instead of the default 2 on every day halves the
        # measured SD (0.122 over 12 weeks), so the weight rises to 0.15^2 /
        # (0.15^2 + 0.122^2) = 0.60; without an estimate the default stays (0.28).
        inputs, trend = self.phase(12)
        days = sorted(inputs.body_fat_pct)
        own = balance.fat_share(
            replace(inputs, skinfold_sd_pct=dict.fromkeys(days, 1.0)), trend, END
        )
        self.assertAlmostEqual(0.60, own.measured_weight, delta=0.01)
        default = balance.fat_share(replace(inputs, skinfold_sd_pct={}), trend, END)
        self.assertAlmostEqual(0.28, default.measured_weight, delta=0.01)
        # Gitar on #3622: only the later, repeated days carry the tighter SD;
        # the earlier single readings keep the default, so the weight lies
        # between the two.
        later = dict.fromkeys(days[len(days) // 2 :], 1.0)
        mixed = balance.fat_share(replace(inputs, skinfold_sd_pct=later), trend, END)
        self.assertTrue(0.30 < mixed.measured_weight < 0.58, mixed.measured_weight)
        # Another method keeps the default: the estimate is the skinfolds' own.
        entered = replace(
            inputs,
            body_fat_pct={},
            device_body_fat={d: (v, None) for d, v in inputs.body_fat_pct.items()},
            skinfold_sd_pct=dict.fromkeys(days, 1.0),
        )
        self.assertAlmostEqual(
            0.28, balance.fat_share(entered, trend, END).measured_weight, delta=0.01
        )

    def test_readings_that_do_not_weigh_keep_both_and_the_prior(self) -> None:
        # Gitar on #3581: a flat weight keeps both readings for the card, and a
        # weight below the label's 1 % stays "prior" as the label shows.
        inputs, _ = self.phase(4, every=14)
        flat = {d: 80.0 for d in inputs.days}
        share = balance.fat_share(inputs, flat, END)
        self.assertEqual(
            (0.70, 0.0, 20.0), (share.share, share.measured_weight, share.pct_first)
        )
        self.assertIsNotNone(share.pct_last)
        inputs, trend = self.phase(2, every=14)
        weights = {d: v.weight_kg for d, v in inputs.days.items() if v.weight_kg}
        store = balance._stored_energy(inputs, trend, weights, END)
        self.assertEqual(("prior", 20.0), (store.method, store.fat_pct_start))
        self.assertGreater(store.measured_weight, 0)

    def test_the_store_carries_its_terms_and_a_narrow_band(self) -> None:
        # #3548 boxes and #3555: the window's weigh-in slope at the blended
        # share; two readings 14 days apart no longer widen the band to about
        # +-1,400 kcal (the former rule's 1 point over 14 days).
        inputs, trend = self.phase(4, every=14)
        weights = {d: v.weight_kg for d, v in inputs.days.items() if v.weight_kg}
        window = {d: w for d, w in weights.items() if d > END - timedelta(days=28)}
        store = balance._stored_energy(inputs, trend, window, END)
        self.assertEqual("measured", store.method)
        self.assertEqual(27, store.days)
        self.assertAlmostEqual(
            store.weight_end_kg - store.weight_start_kg, -27 / 14, places=6
        )
        self.assertAlmostEqual(
            store.fat_change_kg, -27 / 14 * store.fat_share, places=6
        )
        self.assertLess(BAND_Z * store.sd, 200)
        self.assertEqual("fat_share", store.uncertainty)
        prior = balance._stored_energy(
            replace(inputs, body_fat_pct={}), trend, window, END
        )
        self.assertEqual(
            ("prior", 0.70, 0.0), (prior.method, prior.fat_share, prior.measured_weight)
        )
        self.assertAlmostEqual(prior.kcal_per_day, -0.5 / 7 * DENSITY, places=6)


class CompleteDayTests(unittest.TestCase):
    """#3553: completeness no longer scales with the day's training."""

    def day(
        self, intake: float, activity: float, entries: int = 4
    ) -> balance.DayInputs:
        # TEF 0.25 x 400 + 0.10 x 1,200 + 0.015 x 720 = 230.8 kcal.
        return balance.DayInputs(
            intake_kcal=intake,
            protein_g=100,
            carbohydrate_g=300,
            fat_g=80,
            entries=entries,
            activity_kcal=activity,
        )

    def test_a_fully_logged_long_day_counts(self) -> None:
        # The issue's table: rest 1,800, activity 2,500, intake 2,600 was
        # below 60 % of 4,530.8 and dropped; it is above 70 % of rest + TEF.
        long_day = self.day(2600.0, 2500.0)
        self.assertTrue(balance.is_complete(long_day, 1800.0))
        self.assertTrue(balance.below_former_share(long_day, 1800.0))
        for intake, activity in ((1900.0, 300.0), (2400.0, 1500.0), (2800.0, 2500.0)):
            with self.subTest(intake=intake, activity=activity):
                self.assertTrue(balance.is_complete(self.day(intake, activity), 1800.0))

    def test_one_entry_or_an_intake_far_below_rest_still_does_not(self) -> None:
        self.assertFalse(
            balance.is_complete(self.day(2600.0, 300.0, entries=1), 1800.0)
        )
        # 70 % of 1,800 + 230.8 = 1,421.6 kcal.
        self.assertFalse(balance.is_complete(self.day(1400.0, 0.0), 1800.0))
        self.assertTrue(balance.is_complete(self.day(1430.0, 0.0), 1800.0))
        self.assertFalse(balance.below_former_share(self.day(1400.0, 0.0), 1800.0))
        # Without a resting rate the entries decide, as before.
        self.assertTrue(balance.is_complete(self.day(900.0, 2500.0), None))

    def test_a_window_counts_its_long_days(self) -> None:
        # One long day a week (activity 2,500, intake 2,600), the rest 2,300.
        inputs = series(lambda n: 2600.0 if n % 7 == 0 else 2300.0, days=28)
        days = {
            d: replace(v, activity_kcal=2500.0 if v.intake_kcal == 2600.0 else 600.0)
            for d, v in inputs.days.items()
        }
        window = balance.energy_windows(replace(inputs, days=days), END, weeks=1)[-1]
        self.assertEqual(28, window.complete_days)


class ScaleOnlyTests(unittest.TestCase):
    """#3603 ("Nur Waage"): the stored energy needs weigh-ins, not logged meals."""

    def unlogged(self, intake: float) -> balance.EnergyInputs:
        logged = series(constant(intake))
        days = {
            d: replace(
                v, intake_kcal=None, entries=0, protein_g=0, carbohydrate_g=0, fat_g=0
            )
            for d, v in logged.days.items()
        }
        return replace(logged, days=days)

    def test_daily_weigh_ins_without_meals_give_the_store_and_its_band(self) -> None:
        logged = balance.energy_windows(series(constant(2100.0)), END, weeks=1)[-1]
        alone = balance.energy_windows(self.unlogged(2100.0), END, weeks=1)[-1]
        assert logged.store is not None and alone.store is not None
        # The same store and band as with full logging; no expenditure.
        self.assertEqual(
            (logged.store.kcal_per_day, logged.store.sd),
            (alone.store.kcal_per_day, alone.store.sd),
        )
        self.assertEqual(
            ("intake_days", None, None),
            (alone.insufficient, alone.adaptive_tdee, alone.derived_rmr),
        )

    def test_too_few_weigh_ins_still_give_no_store(self) -> None:
        sparse = series(constant(2100.0), weigh_every=5)
        days = {
            d: replace(v, intake_kcal=None, entries=0) for d, v in sparse.days.items()
        }
        window = balance.energy_windows(replace(sparse, days=days), END, weeks=1)[-1]
        self.assertIsNone(window.store)


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
        # 500 / 7,153.5 kg a day, which the weigh-ins' slope sees from day one,
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
        # density term is left. Slope 500 / 7,153.5 = 0.069896 kg/day; the prior
        # share's SD 0.15 x (9,441 - 1,816) = 1,143.75 kcal/kg; 79.943 kcal/day.
        # 28 days and 28 weigh-ins give min(27, 26) dof: t = 2.056, band 164.36.
        window = balance.energy_windows(series(constant(2100.0)), END, weeks=1)[-1]
        assert window.adaptive_tdee is not None
        self.assertAlmostEqual(window.adaptive_tdee.band_kcal, 164.36, delta=0.05)

    def test_a_weight_step_widens_the_band_and_flags_the_window(self) -> None:
        # 1 kg of water and glycogen 13 days before END, mid-window of the
        # latest window, with constant intake: the slope reads it as tissue,
        # about 1.45 x 1 kg x 7,153.5 / 27 days, roughly -380 kcal/day (#3614).
        stepped = balance.energy_windows(series(constant(2600.0), step_at=106), END)
        latest = stepped[-1]
        assert latest.adaptive_tdee is not None and latest.store is not None
        error = latest.adaptive_tdee.kcal - TRUE_TDEE
        self.assertLess(error, -300)
        self.assertFalse(balance.is_stable(latest))
        self.assertLessEqual(abs(error), latest.adaptive_tdee.band_kcal)
        # The store error carries it too, so the deficit card's band covers it.
        self.assertLessEqual(abs(error), balance.window_t(latest) * latest.store.sd)
        # Windows the step never reached stay stable and keep their band.
        flat = balance.energy_windows(series(constant(2600.0)), END)
        self.assertTrue(balance.is_stable(stepped[0]))
        self.assertEqual(stepped[0].adaptive_tdee, flat[0].adaptive_tdee)

    def test_a_half_logged_window_prices_only_its_logged_span(self) -> None:
        # #3667, L31's phase switch: true expenditure 2,600, logged at 2,600,
        # then ``unlogged`` days eaten at 2,100 without logging, weighed daily.
        def switch(unlogged: int) -> tuple[balance.EnergyWindow, ...]:
            weight, days = 80.0, {}
            for n in range(120):
                logged = n < 120 - unlogged
                true = 2600.0 if logged else 2100.0
                weight += (true - TRUE_TDEE) / DENSITY
                days[END - timedelta(days=119 - n)] = balance.DayInputs(
                    intake_kcal=true if logged else None,
                    entries=3 if logged else 0,
                    weight_kg=weight,
                    activity_kcal=600.0,
                )
            return balance.energy_windows(balance.EnergyInputs(days, {}, None), END)

        # Never the mixed 2,688 / 2,863 the whole span gave.
        tdees = [
            w.adaptive_tdee.kcal
            for unlogged in (5, 7, 14, 35)
            for w in switch(unlogged)
            if w.adaptive_tdee is not None
        ]
        self.assertGreater(len(tdees), 30)
        self.assertLess(max(abs(t - TRUE_TDEE) for t in tdees), 1.0)
        latest = switch(14)[-1]
        # 14 logged days span 13: too short to price, the store stays whole
        # for the deficit card ("Nur Waage", #3603).
        self.assertEqual(
            ("logged_span", 14), (latest.insufficient, latest.complete_days)
        )
        assert latest.store is not None
        # Its 14 days at 500 below show as a deficit over the whole window
        # (the least-squares line through the kink reads about 260).
        self.assertLess(latest.store.kcal_per_day, -200.0)
        # Logged days spanning 25 days with only 6 weigh-ins inside them keep
        # the plain `weighins` reason; the span is long enough (Gitar on #3676).
        sparse = {
            END - timedelta(days=27 - n): balance.DayInputs(
                intake_kcal=2600.0 if n >= 2 else None,
                entries=3 if n >= 2 else 0,
                weight_kg=80.0 if n < 2 or n % 4 == 0 else None,
            )
            for n in range(28)
        }
        window = balance.energy_windows(
            balance.EnergyInputs(sparse, {}, None), END, weeks=1
        )[-1]
        self.assertEqual("weighins", window.insufficient)
        # 23 logged days span 22: the window keeps its own TDEE.
        self.assertIsNotNone(switch(5)[-1].adaptive_tdee)

    def test_flat_losing_and_gaining_weight_is_stable(self) -> None:
        for intake in (2600.0, 2100.0, 3100.0):
            with self.subTest(intake=intake):
                windows = balance.energy_windows(series(constant(intake)), END)
                self.assertTrue(all(balance.is_stable(w) for w in windows))
        self.assertEqual(
            balance.unexplained_spreads(
                series(constant(2600.0)),
                balance.energy_windows(series(constant(2600.0)), END),
            ),
            [0.0] * 12,
        )

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
        window = balance._window(inputs, body_records.weight_trend(weights), END, 1.1)
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
        # #3554: the windows overlap by 21 of 28 days, so the slope variance
        # 4 x 150^2 / 2 / 50,000 = 0.9 is inflated by 28 / 7 to 3.6: the
        # posterior (1.10 / 3.6 + 100) / (1 / 3.6 + 100) = 1.000277.
        self.assertAlmostEqual(balance.calibration_factor(noisy), 1.000277, places=6)
        self.assertEqual(balance.calibration_factor(exact[:3]), 1.0)

    def test_the_learned_everyday_term_is_shrunk_and_keeps_its_sign(self) -> None:
        # #3577: four windows 300 kcal/day over the row, each SD 100. The mean
        # 300 has precision 4 / 100^2 = 4e-4, overlap-inflated to 1e-4; the
        # prior 0 +- 250 adds 1.6e-5: posterior variance 1 / 1.16e-4 = 8,620.7,
        # mean 8,620.7 x 1e-4 x 300 = 258.62, band 1.96 x 92.85 = 181.98.
        fit = balance.fit_learned_everyday([(300.0, 100.0, 1.0)] * 4)
        assert fit is not None
        self.assertAlmostEqual(258.62, fit.raw_kcal, places=2)
        self.assertAlmostEqual(258.62, fit.kcal, places=2)
        self.assertAlmostEqual(181.98, fit.band_kcal, places=2)
        self.assertEqual(4, fit.windows)
        # #3687: an older window weighs by its age, as in the 12-week base.
        aged = balance.fit_learned_everyday([(300.0, 100.0, 1.0), (0.0, 100.0, 0.5)])
        level = balance.fit_learned_everyday([(300.0, 100.0, 1.0), (0.0, 100.0, 1.0)])
        assert aged is not None and level is not None
        self.assertGreater(aged.raw_kcal, level.raw_kcal)
        # #3689: a row that overshoots shows its negative term as a reduction,
        # down to the symmetric floor of -250 kcal/day.
        under = balance.fit_learned_everyday([(-200.0, 100.0, 1.0)] * 4)
        assert under is not None
        self.assertAlmostEqual(-172.41, under.raw_kcal, places=2)
        self.assertAlmostEqual(-172.41, under.kcal, places=2)
        floored = balance.fit_learned_everyday([(-900.0, 10.0, 1.0)] * 4)
        assert floored is not None
        self.assertEqual(-250.0, floored.kcal)
        self.assertIsNone(balance.fit_learned_everyday([]))

    def test_one_window_follows_the_conjugate_normal_update(self) -> None:
        # #3689 point 1: prod's "+172 +- 439" from one window. Its SD 250 is
        # overlap-inflated to 500; the prior 0 +- 250 gives the posterior
        # variance 1 / (1/500^2 + 1/250^2) = 50,000 (SD 223.6, band 438.3)
        # and the shrinkage 50,000 / 500^2 = 0.2: a raw 860 shows as 172.
        fit = balance.fit_learned_everyday([(860.0, 250.0, 1.0)])
        assert fit is not None
        self.assertAlmostEqual(172.0, fit.raw_kcal, places=6)
        self.assertAlmostEqual(1.96 * math.sqrt(50_000.0), fit.band_kcal, places=6)

    def test_noise_around_zero_leaves_no_upward_bias(self) -> None:
        # #3689 point 2: the former floor at 0 turned symmetric noise into a
        # positive mean term.
        fits = [
            balance.fit_learned_everyday([(value, 50.0, 1.0)])
            for value in (-200.0, -100.0, 100.0, 200.0)
        ]
        self.assertAlmostEqual(0.0, sum(fit.kcal for fit in fits if fit) / 4, places=6)

    def test_the_term_qualifies_with_three_independent_windows_or_a_small_sd(
        self,
    ) -> None:
        # #3689 point 3: posterior SD 223.6 > 150, so it needs 3 windows that
        # share no day; a precise fit qualifies alone.
        wide = [(860.0, 250.0, 1.0)]
        for independent, qualified in ((1, False), (2, False), (3, True)):
            with self.subTest(independent=independent):
                fit = balance.fit_learned_everyday(wide, independent)
                assert fit is not None
                self.assertEqual(
                    (qualified, independent), (fit.qualified, fit.independent)
                )
        precise = balance.fit_learned_everyday([(300.0, 50.0, 1.0)], 1)
        assert precise is not None
        self.assertTrue(precise.qualified)

    def test_independent_windows_share_no_day(self) -> None:
        # Weekly 28-day windows: of twelve stable ones, every fourth shares no
        # day with the next: 3 independent; unstable ones never count.
        windows = balance.energy_windows(series(constant(2400.0)), END)
        self.assertEqual(3, balance.independent_windows(windows))
        self.assertEqual(
            0, balance.independent_windows([replace(w, stable=False) for w in windows])
        )

    def test_the_bias_fit_takes_activity_only_where_it_can(self) -> None:
        flagged = (100.0, 200.0, 300.0, 400.0)
        # Activity unknown in one window: the single regression, b = 0.2.
        partial = [
            bias_window(x, a, 28 * n)
            for n, (x, a) in enumerate(zip(flagged, (500, None, 700, 600), strict=True))
        ]
        bias = balance._bias(partial)
        assert bias is not None
        self.assertAlmostEqual(0.2, bias[0])
        # Flagged kcal that moved only with the activity cannot be told apart.
        collinear = [bias_window(x, 2.0 * x, 28 * n) for n, x in enumerate(flagged)]
        self.assertIsNone(balance._bias(collinear))

    def test_the_bias_needs_a_narrow_t_band_and_independent_windows(self) -> None:
        # #3616: the same exact fit in four overlapping weekly windows lies in
        # one independent window only, so it is not learned.
        weekly = [
            bias_window(x, None, 7 * n) for n, x in enumerate((1e2, 2e2, 3e2, 4e2))
        ]
        self.assertIsNone(balance._bias(weekly))
        # Four windows 28 days apart, activity as the second term: dof 1, so
        # the band takes t = 12.706 and is far wider than 50 points. With z it
        # would have been learned (0.13 +- 0.13).
        xs, acts = (100.0, 200.0, 300.0, 400.0), (500.0, 650.0, 600.0, 700.0)
        noisy = [
            bias_window(x, a, 28 * (3 - n), 8.0 * (-1) ** n)
            for n, (x, a) in enumerate(zip(xs, acts, strict=True))
        ]
        self.assertIsNone(balance._bias(noisy))
        # The prior 0 +- 0.25 against data of the same SD (band 1.96 x 0.25)
        # halves the slope; the posterior SD is 0.25 / sqrt 2.
        bias, band = balance._shrunk_bias(0.4, 0.49)
        self.assertAlmostEqual(0.2, bias)
        self.assertAlmostEqual(1.96 * 0.25 / math.sqrt(2), band)
        # An exact fit keeps its slope; below -100 % it is clamped to -1.
        self.assertEqual((0.4, 0.0), balance._shrunk_bias(0.4, 0.0))
        self.assertEqual((-1.0, 0.0), balance._shrunk_bias(-3.0, 0.0))

    def test_k_and_b_see_one_fat_share_whatever_the_readings(self) -> None:
        # #3554, the issue's synthetic series: 111 days of a steady 0.07 kg/day
        # loss, 85 % of it fat, from 85 kg at 18 %; activity builds from 400 to
        # 800 kcal/day; true expenditure 1,800 + activity + digestion, so the
        # true k is 1. Some days hold a flagged meal of 500 kcal that was logged
        # exactly, so the true b is 0. Mid-phase readings used to put a step
        # into the windows (k 0.850 and 1.076) for k and b to read.
        days, rest, tef = 111, 1800.0, balance.tef_kcal(150, 300, 80)
        start = END - timedelta(days=days - 1)
        density = 0.85 * balance.FAT_KCAL_PER_KG + 0.15 * balance.LEAN_KCAL_PER_KG

        def weight(n: int) -> float:
            return 85.0 - 0.07 * n

        def fat_pct(n: int) -> float:
            return 100.0 * (85.0 * 0.18 - 0.85 * 0.07 * n) / weight(n)

        out = {}
        for n in range(days):
            activity = 400.0 + 400.0 * n / (days - 1)
            flagged = (n % 7) < (n // 7) % 5  # a changing share per week
            out[start + timedelta(days=n)] = balance.DayInputs(
                intake_kcal=rest + activity + tef - 0.07 * density,
                protein_g=150, carbohydrate_g=300, fat_g=80, entries=3,
                flagged=flagged, flagged_kcal=500.0 if flagged else 0.0,
                weight_kg=weight(n), activity_kcal=activity,
            )  # fmt: skip
        biases: list[float] = []
        for readings in ([-10], [56], list(range(56, days, 14))):
            with self.subTest(readings=readings):
                folds = {start + timedelta(days=n): fat_pct(n) for n in readings}
                inputs = balance.EnergyInputs(out, folds, rmr_kcal=rest)
                windows = balance.energy_windows(inputs, END)
                self.assertAlmostEqual(1.0, windows[-1].calibration_factor, delta=0.05)
                # One share for every window: no step for b to read.
                shares = {w.store.fat_share for w in windows if w.store}
                self.assertEqual(1, len(shares))
                # #3586: the rising activity alone leaned b to +32 %; with
                # the activity as its second term, b is near the true 0, and
                # a reading pattern does not move it.
                bias = balance.windows_bias(windows)
                assert bias is not None
                self.assertAlmostEqual(0.0, bias[0], delta=5.0)  # percent
                biases.append(bias[0])
        self.assertAlmostEqual(biases[0], biases[1], places=6)
        self.assertAlmostEqual(biases[0], biases[2], places=6)


def drifting(truth_of: Callable[[int], float], days: int = 120) -> balance.EnergyInputs:
    """A logged 2,600 kcal on every day against a true expenditure that follows
    ``truth_of``; the weight follows the true balance."""
    weight, out = 80.0, {}
    for n in range(days):
        weight += (2600.0 - truth_of(n)) / DENSITY
        out[END - timedelta(days=days - 1 - n)] = balance.DayInputs(
            intake_kcal=2600.0, protein_g=150, carbohydrate_g=300, fat_g=80,
            entries=3, weight_kg=weight, activity_kcal=600.0,
            activity_error_kcal=60.0,
        )  # fmt: skip
    return balance.EnergyInputs(out, {}, rmr_kcal=1700.0, rmr_source="katch_mcardle")


class TwelveWeekBaseTests(unittest.TestCase):
    """#3687: the daily estimate rests on the stable windows of 12 weeks."""

    def base(self, inputs: balance.EnergyInputs) -> balance.Base:
        base = balance.twelve_week_base(balance.energy_windows(inputs, END))
        assert base is not None
        return base

    def test_a_water_step_moves_the_base_far_less_than_its_window(self) -> None:
        # 1 kg of water and glycogen two weeks before the view's end (#3614).
        steady, step = series(constant(2400.0)), series(constant(2400.0), step_at=106)
        stepped = balance.energy_windows(step, END)[-1].derived_rmr
        level = balance.energy_windows(steady, END)[-1].derived_rmr
        assert stepped is not None and level is not None
        on_window = abs(stepped.kcal - level.kcal)
        moved = abs(self.base(step).estimate.kcal - self.base(steady).estimate.kcal)
        self.assertGreater(on_window, 300)
        self.assertLess(moved, on_window / 3)

    def test_a_true_change_shows_half_in_about_six_weeks(self) -> None:
        # The true expenditure drops by 300 kcal/day six weeks before the end.
        before = self.base(drifting(lambda n: 2600.0)).estimate.kcal
        after = self.base(drifting(lambda n: 2600.0 - 300.0 * (n >= 78))).estimate.kcal
        self.assertAlmostEqual(-150.0, after - before, delta=60.0)

    def test_every_window_unstable_keeps_the_base_and_flags_it(self) -> None:
        # Amended #3687: unstable windows weigh less through their widened band
        # (#3614) but stay in; the base is flagged only when all are unstable.
        steady = balance.energy_windows(series(constant(2400.0)), END)
        windows = [replace(w, stable=False) for w in steady]
        base = balance.twelve_week_base(windows)
        stable = balance.twelve_week_base(steady)
        assert base is not None and stable is not None
        self.assertEqual(stable.estimate, base.estimate)
        self.assertEqual((True, 12), (base.unstable, base.windows))
        self.assertFalse(stable.unstable)
        self.assertIsNone(balance.twelve_week_base([]))

    def test_the_weights_are_precision_times_a_six_week_half_life(self) -> None:
        def window(back: int, kcal: float, band: float) -> balance.EnergyWindow:
            return replace(
                bias_window(0.0, 600.0, back),
                derived_rmr=balance.Estimate(kcal, band),
            )

        # Equal bands: the window 42 days older weighs half.
        base = balance.twelve_week_base(
            [window(42, 1600.0, 100.0), window(0, 1900.0, 100.0)]
        )
        assert base is not None
        self.assertAlmostEqual(1800.0, base.estimate.kcal)
        self.assertAlmostEqual(1.5, base.weight_sum)
        self.assertEqual((2, False), (base.windows, base.unstable))
        # Overlap inflation: never narrower than one window's band.
        self.assertGreaterEqual(base.estimate.band_kcal, 100.0 * 0.95)
        # Twice the band, a quarter of the precision.
        equal_age = balance.twelve_week_base(
            [window(0, 1600.0, 200.0), window(0, 1900.0, 100.0)]
        )
        assert equal_age is not None
        self.assertAlmostEqual(1840.0, equal_age.estimate.kcal)


if __name__ == "__main__":
    unittest.main()

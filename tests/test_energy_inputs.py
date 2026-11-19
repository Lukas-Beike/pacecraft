"""The adaptive expenditure reader (#3100) over fake sources, checked by hand."""

from __future__ import annotations

import math
import unittest
from dataclasses import replace
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

from backend.athlete import body_records
from backend.athlete.body_composition import Sex
from backend.athlete.body_records import skinfold_body_fats, weight_trend
from backend.athlete.skinfolds import SkinfoldMeasurement
from backend.canonical.activity_detail import ActivityStreamSet
from backend.canonical.vocabulary import ProviderId, StreamChannel
from backend.nutrition import energy_inputs
from backend.nutrition.energy_balance import (
    DayInputs,
    EnergyInputs,
    EnergyWindow,
    Estimate,
    energy_windows,
)
from backend.nutrition.energy_inputs import (
    EnergySources,
    diagnostics,
    energy_basis,
    read_energy_inputs,
)
from backend.nutrition.energy_view import read_energy_view

DAY1, DAY2, DAY3 = date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3)
DAY4 = date(2026, 10, 4)

DIARY = [
    {"meal_date": "2026-10-01", "kcal": 1200, "protein_g": 50, "carbs_g": 100, "fat_g": 30},
    {"meal_date": "2026-10-01", "kcal": 1200, "protein_g": 50, "carbs_g": 100, "fat_g": 30},
    {"meal_date": "2026-10-02", "kcal": 900, "estimate_uncertain": True},
]  # fmt: skip
HEALTH = {
    "2026-10-01": {"steps": 10_000, "floors": 10, "resting_kcal": 1700},
    "2026-10-02": {"steps": 5_000, "resting_kcal": 0},
}
SESSIONS = [
    {"id": "run", "sport": "run", "started_at_local": "2026-10-01T07:00:00",
     # Canonical run cadence counts one leg: 85 strides/min = 170 steps/min.
     "moving_duration_s": 3000, "distance_m": 10_000, "avg_cadence": 85},
    {"id": "ride", "sport": "ride", "started_at_local": "2026-10-02T07:00:00",
     "moving_duration_s": 3600, "work_kj": 720, "avg_hr": 140},
    {"id": "gym", "sport": "strength", "started_at_local": "2026-10-03T18:00:00",
     "moving_duration_s": 3600},
    {"id": "swim", "sport": "swim", "started_at_local": "2026-10-03T07:00:00",
     "moving_duration_s": 1800, "avg_hr": 130},
]  # fmt: skip


def sources(
    resting: tuple[float, str] | None = (1680.0, "katch_mcardle"),
    streams: dict[str, Any] | None = None,
    profile: dict[str, str] | None = None,
    sessions: list[dict[str, Any]] | None = None,
    health: dict[str, dict[str, Any]] | None = None,
) -> EnergySources:
    return EnergySources(
        diary=lambda _db, _s, _e: DIARY,
        daily_health=lambda _db, _s, _e: HEALTH if health is None else health,
        weights=lambda _db, _s, _e: {DAY1: 70.0},
        sessions=lambda _db, _s, _e: SESSIONS if sessions is None else sessions,
        streams=lambda _db, key: (streams or {}).get(key, {}),
        resting=lambda _db, _day: resting,
        body_fat=lambda _db, _s, _e: {DAY1: 12.0},
        profile=lambda: profile or {"height_cm": "180"},
    )


class EnergyInputsReaderTests(unittest.TestCase):
    def test_days_carry_intake_flags_weight_and_own_activity(self) -> None:
        inputs, costs = read_energy_inputs(None, sources(), DAY1, DAY3)
        self.assertEqual(list(inputs.days), [DAY1, DAY2, DAY3])
        first, second, third = (inputs.days[d] for d in (DAY1, DAY2, DAY3))
        self.assertEqual(
            (first.intake_kcal, first.entries, first.flagged), (2400, 2, False)
        )
        self.assertEqual(
            (first.protein_g, first.carbohydrate_g, first.fat_g), (100, 200, 60)
        )
        self.assertEqual((second.intake_kcal, second.flagged), (900, True))
        # Only the flagged entries' kcal widen the band (#3187 item 13).
        self.assertEqual((first.flagged_kcal, second.flagged_kcal), (0.0, 900.0))
        self.assertIsNone(third.intake_kcal)
        self.assertEqual((first.weight_kg, second.weight_kg), (70.0, None))
        # Run: 70 kg x 10,000 m x 3.6 J/kg/m / 4,184 = 602.294 kcal. NEAT: step
        # 0.415 x 1.80 = 0.747 m; 10,000 - 2 x 85 x 50 = 1,500 steps; 70 x 1,500 x
        # 0.747 x 2.01 = 157,654.35 J plus floors 82,404 J = 57.375 kcal.
        assert first.activity_kcal is not None
        self.assertAlmostEqual(first.activity_kcal, 602.294 + 57.375, places=2)
        # Ride: 790.421 kcal net (M11 example); NEAT 70 x 5,000 x 0.747 x 2.01 =
        # 525,514.5 J = 125.601 kcal; the ride adds no steps.
        assert second.activity_kcal is not None
        self.assertAlmostEqual(second.activity_kcal, 790.421 + 125.601, places=2)
        # Day 3 has no steps (NEAT unknown) and an unpriced swim (no HR fit from
        # one ride): its activity stays unknown, not the strength alone (#3187).
        self.assertIsNone(third.activity_kcal)
        self.assertEqual(
            [(c.method, c.kcal is None) for c in costs],
            [("minetti", False), ("power", False), ("met", False), ("unknown", True)],
        )
        self.assertEqual(inputs.body_fat_pct, {DAY1: 12.0})
        # Split A (#3100): the parts behind each activity; day 3 knows neither,
        # no steps for NEAT and an unpriced swim for the sessions.
        self.assertAlmostEqual(first.neat_kcal or 0.0, 57.375, places=2)
        self.assertAlmostEqual(first.sport_kcal or 0.0, 602.294, places=2)
        self.assertAlmostEqual(second.neat_kcal or 0.0, 125.601, places=2)
        self.assertAlmostEqual(second.sport_kcal or 0.0, 790.421, places=2)
        self.assertEqual((third.neat_kcal, third.sport_kcal), (None, None))

    def test_a_strength_session_is_priced_over_its_elapsed_time(self) -> None:
        # #3530: Garmin's moving time of a strength session leaves out the rests
        # between sets, which its MET covers: 19.4 of 45 minutes gave 70 kcal.
        gym = {"id": "legs", "sport": "strength", "started_at_local": "2026-10-03T18:00:00",
               "moving_duration_s": 1163, "elapsed_duration_s": 2700}  # fmt: skip
        _inputs, costs = read_energy_inputs(None, sources(sessions=[gym]), DAY1, DAY3)
        # (3.5 - 1) x 1,680 kcal/day x 2,700 s / 86,400 s = 131.25 kcal.
        self.assertEqual("met", costs[0].method)
        self.assertAlmostEqual(131.25, costs[0].kcal or 0.0, places=2)
        # Without an elapsed time the moving time still prices it.
        del gym["elapsed_duration_s"]
        _inputs, costs = read_energy_inputs(None, sources(sessions=[gym]), DAY1, DAY3)
        # 2.5 x 1,680 x 1,163 / 86,400 = 56.535 kcal.
        self.assertAlmostEqual(56.535, costs[0].kcal or 0.0, places=2)

    def test_a_strength_sessions_rpe_sets_its_met_local_first(self) -> None:
        # #3530: the local session RPE comes before the provider's and sets the
        # MET; RPE 8 is MET 5: 4 x 1,680 kcal/day x 2,700 s / 86,400 s = 210.
        gym = {"id": "legs", "sport": "strength", "started_at_local": "2026-10-03T18:00:00",
               "elapsed_duration_s": 2700, "rpe": 5}  # fmt: skip
        rated = replace(
            sources(sessions=[gym]),
            feedback=lambda keys: [{"activity_id": k, "session_rpe": 8} for k in keys],
        )
        _inputs, costs = read_energy_inputs(None, rated, DAY1, DAY3)
        self.assertEqual((8, 5.0), (costs[0].rpe, costs[0].met))
        self.assertAlmostEqual(210.0, costs[0].kcal or 0.0, places=2)
        # Without local feedback the provider's RPE 5 keeps the general MET
        # (131.25 kcal); without any RPE, too.
        _inputs, costs = read_energy_inputs(None, sources(sessions=[gym]), DAY1, DAY3)
        self.assertEqual((5, 3.5), (costs[0].rpe, costs[0].met))
        self.assertAlmostEqual(131.25, costs[0].kcal or 0.0, places=2)
        del gym["rpe"]
        _inputs, costs = read_energy_inputs(None, sources(sessions=[gym]), DAY1, DAY3)
        self.assertEqual((None, 3.5), (costs[0].rpe, costs[0].met))

    def test_the_days_activity_error_is_each_parts_method_share(self) -> None:
        # #3552: the strength session's MET at ±50 % and the everyday's step
        # length at ±15 %, in quadrature, instead of one flat 10 %.
        gym = {"id": "legs", "sport": "strength", "started_at_local": "2026-10-01T18:00:00",
               "elapsed_duration_s": 2700}  # fmt: skip
        inputs, costs = read_energy_inputs(None, sources(sessions=[gym]), DAY1, DAY3)
        day = inputs.days[DAY1]
        assert day.neat_kcal is not None and costs[0].kcal is not None
        self.assertAlmostEqual(
            math.hypot(0.50 * costs[0].kcal, 0.15 * day.neat_kcal),
            day.activity_error_kcal or 0.0,
        )
        # Without steps (today before the day summary) the session keeps its
        # error, so the so-far total is not understated (Gitar on #3567).
        late = {**gym, "started_at_local": "2026-10-03T18:00:00"}
        inputs, costs = read_energy_inputs(None, sources(sessions=[late]), DAY1, DAY3)
        self.assertIsNone(inputs.days[DAY3].neat_kcal)
        self.assertAlmostEqual(
            0.50 * (costs[0].kcal or 0.0), inputs.days[DAY3].activity_error_kcal
        )
        # A day without sessions has the step length's error only.
        rest, _ = read_energy_inputs(None, sources(sessions=[]), DAY1, DAY1)
        self.assertAlmostEqual(
            0.15 * (rest.days[DAY1].neat_kcal or 0.0),
            rest.days[DAY1].activity_error_kcal or 0.0,
        )

    def test_a_foot_session_without_a_distance_is_unknown(self) -> None:
        # #3617 item 4: no distance and no stream is no cost, so the session is
        # unknown, not 0 kcal; its recorded steps still leave everyday.
        run = {"id": "run", "sport": "run", "started_at_local": "2026-10-03T07:00:00",
               "moving_duration_s": 1800, "steps": 4000}  # fmt: skip
        inputs, costs = read_energy_inputs(None, sources(sessions=[run]), DAY1, DAY3)
        self.assertEqual(
            ("unknown", None, 4000.0), (costs[0].method, costs[0].kcal, costs[0].steps)
        )
        self.assertIsNone(inputs.days[DAY3].sport_kcal)

    def test_every_skinfold_reading_counts_not_one_per_week(self) -> None:
        # #3617 item 2: two readings in one week both count; one before the
        # lookback and one without a body fat do not.
        found = skinfold_body_fats(
            [
                SkinfoldMeasurement(DAY4, Sex.MALE, {}, 12.0),
                SkinfoldMeasurement(DAY2, Sex.MALE, {}, 12.5),
                SkinfoldMeasurement(DAY1, Sex.MALE, {}, None),
                SkinfoldMeasurement(date(2026, 9, 1), Sex.MALE, {}, 14.0),
            ],
            DAY1,
        )
        self.assertEqual({DAY4: 12.0, DAY2: 12.5}, found)

    def test_a_day_without_sessions_has_sport_zero(self) -> None:
        # Known to be zero, not unknown: 70 x 10,000 x 0.747 x 2.01 = 1,051,029 J
        # plus floors 82,404 J = 270.897 kcal NEAT, all of the activity.
        inputs, _ = read_energy_inputs(None, sources(sessions=[]), DAY1, DAY1)
        day = inputs.days[DAY1]
        self.assertEqual(day.sport_kcal, 0.0)
        self.assertAlmostEqual(day.neat_kcal or 0.0, 270.897, places=2)
        self.assertAlmostEqual(day.activity_kcal or 0.0, 270.897, places=2)

    def test_the_profile_weight_prices_nothing_without_a_weigh_in(self) -> None:
        # #3640: without any weigh-in the mass is unknown, whatever the profile
        # holds; it priced the sessions before.
        def unweighed(src: EnergySources) -> EnergySources:
            return replace(src, weights=lambda _db, _s, _e: {})

        plain, plain_costs = read_energy_inputs(None, unweighed(sources()), DAY1, DAY3)
        stored = unweighed(sources(profile={"height_cm": "180", "weight_kg": "80"}))
        held, held_costs = read_energy_inputs(None, stored, DAY1, DAY3)
        self.assertEqual(plain_costs, held_costs)
        self.assertEqual(plain.days, held.days)
        self.assertTrue(all(cost.kcal is None for cost in held_costs if cost.steps))

    def test_own_resting_rate_first_garmin_only_as_fallback(self) -> None:
        own, _ = read_energy_inputs(None, sources(), DAY1, DAY3)
        self.assertEqual((own.rmr_kcal, own.rmr_source), (1680.0, "katch_mcardle"))
        # Garmin's zero resting value on day 2 is not a measurement.
        garmin, _ = read_energy_inputs(None, sources(resting=None), DAY1, DAY3)
        self.assertEqual((garmin.rmr_kcal, garmin.rmr_source), (1700.0, "garmin"))
        _, costs = read_energy_inputs(None, sources(), DAY1, DAY3)
        self.assertEqual(
            diagnostics(garmin, costs),
            {"latest_window_complete_days": None, "latest_window_former_rule_days": None,
             "latest_window_weighins": None,
             "latest_window_insufficient": None, "windows": 0,
             "windows_with_estimate": 0,
             "unstable_windows": 0, "largest_unstable_spread_kcal": 0,
             "base_windows": 0, "base_weight_sum_tenths": 0, "base_unstable": 0,
             "windows_span_short": 0,
             "fat_share_measured_windows": {"0": 0, "1-24": 0, "25-49": 0, "50-100": 0},
             "calibration_windows": 0,
             "calibration_store_methods": {"measured": 0, "prior": 0},
             "days": 3, "logged_days": 2, "flagged_days": 1, "weighins": 1,
             "sessions": 4, "unpriced_sessions": 1, "rmr_source": "garmin"},
        )  # fmt: skip

    def test_foot_cadence_counts_both_legs_and_a_bike_cadence_no_steps(
        self,
    ) -> None:
        walk = {
            "id": "walk",
            "sport": "walk",
            "started_at_local": "2026-10-01T12:00",
            "moving_duration_s": 1000,
            "distance_m": 1400,
            "avg_cadence": 56,
        }
        ride = {
            "id": "spin",
            "sport": "ride",
            "started_at_local": "2026-10-01T18:00",
            "moving_duration_s": 1800,
            "avg_hr": 120,
            "avg_cadence": 90,
        }
        inputs, costs = read_energy_inputs(
            None, sources(sessions=[walk, ride]), DAY1, DAY1
        )  # fmt: skip
        # 56 strides/min are 112 steps/min: 112 x 1,000 s / 60 = 1,866.67 steps.
        self.assertAlmostEqual(costs[0].steps, 1866.667, places=2)
        self.assertEqual(costs[1].steps, 0.0)
        # Step length 1.4 m/s x 60 / 112 = 0.75 m (not 1.5 m from one leg).
        # Walk: 70 x 1,400 x 2.01 = 196,980 J = 47.079 kcal; NEAT: (10,000 -
        # 1,866.67) x 0.75 x 2.01 x 70 = 858,270 J plus floors 82,404 J =
        # 224.826 kcal; the HR ride without a fit stays unpriced, so unknown.
        self.assertIsNone(inputs.days[DAY1].activity_kcal)
        alone, _ = read_energy_inputs(None, sources(sessions=[walk]), DAY1, DAY1)
        activity = alone.days[DAY1].activity_kcal
        assert activity is not None
        self.assertAlmostEqual(activity, 47.079 + 224.826, places=2)

    def test_recorded_session_steps_and_foot_climbing_leave_the_days_neat(
        self,
    ) -> None:
        # Day 1: 10,000 steps and 10 floors; step 0.415 x 1.80 = 0.747 m, 70 kg.
        # NEAT = (steps x 0.747 x 2.01 x 70 + 70 x 9.81 x 3 x floors / 0.25) / 4,184.
        day = "2026-10-01T"
        cases = (
            # Wrist steps in a strength session: (8,500 steps, 10 floors).
            ("gym", {"sport": "strength", "steps": 1500}, 1500.0, 233.217),
            # False wrist steps on a ride; its 300 m climb is no floor.
            (
                "ride",
                {"sport": "ride", "steps": 800, "work_kj": 500,
                 "elevation_gain_m": 300},
                800.0,
                250.801,
            ),
            # A run without cadence: its recorded steps (6,000 steps left).
            ("run", {"sport": "run", "steps": 4000, "distance_m": 5000}, 4000.0, 170.416),
            # Without recorded steps: 5,000 m / 0.747 m = 6,693.44 steps.
            ("run", {"sport": "run", "distance_m": 5000}, 6693.44, 102.756),
            # A hike's 15 m ascent is 5 of the day's floors (10,000 steps, 5 floors).
            ("hike", {"sport": "hike", "elevation_gain_m": 15}, 0.0, 261.049),
            # 60 m is more than the day's 10 floors: none left, never negative.
            ("hike", {"sport": "hike", "elevation_gain_m": 60}, 0.0, 251.202),
        )  # fmt: skip
        for name, extra, steps, neat in cases:
            summary = {"id": name, "started_at_local": f"{day}07:00:00",
                       "moving_duration_s": 3600, **extra}  # fmt: skip
            with self.subTest(name=name, extra=extra):
                inputs, costs = read_energy_inputs(
                    None, sources(sessions=[summary]), DAY1, DAY1
                )
                self.assertAlmostEqual(costs[0].steps, steps, places=2)
                self.assertAlmostEqual(
                    inputs.days[DAY1].neat_kcal or 0.0, neat, places=2
                )

    def test_without_a_resting_rate_net_costs_stay_unknown(self) -> None:
        no_resting = {"2026-10-02": {"steps": 5_000}}
        inputs, costs = read_energy_inputs(
            None, sources(resting=None, health=no_resting), DAY1, DAY3
        )
        self.assertIsNone(inputs.rmr_kcal)
        # Power and MET costs are net of the resting rate: unknown, not gross.
        self.assertEqual(
            [(c.method, c.kcal) for c in costs[1:3]],
            [("unknown", None), ("unknown", None)],
        )
        self.assertIsNone(inputs.days[DAY2].activity_kcal)
        # Day 1 has no steps either: its run is priced, its NEAT is unknown.
        self.assertIsNotNone(costs[0].kcal)
        self.assertIsNone(inputs.days[DAY1].activity_kcal)

    def test_energy_basis_counts_the_twelve_week_view(self) -> None:
        basis = energy_basis(None, sources(), DAY3)
        # 12 weekly windows; two logged days are far below 14 complete days.
        self.assertEqual(basis["windows"], 12)
        self.assertEqual(basis["windows_with_estimate"], 0)
        self.assertEqual(basis["latest_window_insufficient"], "intake_days")
        # 84 + 27 days are read: the whole history before the first window.
        self.assertEqual(basis["days"], 7 * 11 + 28)
        # #3530: days of the last week whose summary names its wellness end.
        self.assertEqual(0, basis["wellness_end_days"])
        health: dict[str, dict[str, Any]] = {
            **HEALTH,
            DAY3.isoformat(): {"wellness_end_min": 738.0},
        }
        counted = energy_basis(None, sources(health=health), DAY3)
        self.assertEqual(1, counted["wellness_end_days"])
        # #3612: unfinished past days of the latest window; today is still counting.
        self.assertEqual(0, counted["latest_window_unfinished_days"])
        health[DAY2.isoformat()] = {**HEALTH[DAY2.isoformat()], "wellness_end_min": 738}
        counted = energy_basis(None, sources(health=health), DAY3)
        self.assertEqual(1, counted["latest_window_unfinished_days"])

    def test_a_summary_that_ended_early_leaves_the_day_s_activity_unknown(
        self,
    ) -> None:
        # #3612: day 1's summary ended at 12:18, so its steps cover half the
        # day: its activity is unknown and leaves the windows' means, while its
        # NEAT so far stays for today. Its logging is unchanged.
        health: dict[str, dict[str, Any]] = {
            **HEALTH,
            DAY1.isoformat(): {**HEALTH["2026-10-01"], "wellness_end_min": 738.0},
        }
        inputs, _ = read_energy_inputs(None, sources(health=health), DAY1, DAY3)
        full, _ = read_energy_inputs(None, sources(), DAY1, DAY3)
        day, whole = inputs.days[DAY1], full.days[DAY1]
        self.assertEqual((True, None), (day.unfinished, day.activity_kcal))
        self.assertEqual(whole.neat_kcal, day.neat_kcal)
        self.assertEqual(
            (whole.intake_kcal, whole.entries), (day.intake_kcal, day.entries)
        )
        # A day without a stored end time, or one that ran to midnight, is unchanged.
        for end in (None, 1440.0):
            health[DAY1.isoformat()]["wellness_end_min"] = end
            inputs, _ = read_energy_inputs(None, sources(health=health), DAY1, DAY3)
            with self.subTest(end=end):
                self.assertEqual(whole, inputs.days[DAY1])

    def test_run_grade_comes_from_the_streams(self) -> None:
        streams = {
            "run": {
                StreamChannel.DISTANCE_M: [0.0, 200.0],
                StreamChannel.ALTITUDE_M: [0.0, 20.0],
            }
        }
        _, costs = read_energy_inputs(None, sources(streams=streams), DAY1, DAY1)
        # 200 m at +10 %: 70 x 200 x 5.968214 / 4,184 = 19.970 kcal.
        self.assertAlmostEqual(costs[0].kcal or 0.0, 19.970, places=3)

    def test_keytel_prices_hr_sessions_with_age_and_sex(self) -> None:
        profile = {"height_cm": "180", "birth_year": "1996", "sex": "male"}
        _, costs = read_energy_inputs(None, sources(profile=profile), DAY1, DAY3)
        swim = costs[-1]
        # 30 years, 70 kg, 130 bpm: -55.0969 + 82.017 + 13.916 + 6.051 =
        # 46.8871 kJ/min x 30 min / 4.184 = 336.189 kcal, minus 1,680 / 48 =
        # 35 kcal resting: 301.189 kcal net.
        self.assertEqual(swim.method, "keytel")
        self.assertAlmostEqual(swim.kcal or 0.0, 301.189, places=2)


class OwnRestingTests(unittest.TestCase):
    def test_skinfolds_first_then_measured_body_fat_then_none(self) -> None:
        # 70 kg at 12 %: lean 61.6 kg, 370 + 21.6 x 61.6 = 1,700.56 kcal.
        own = energy_inputs.own_resting(70.0, 12.0, 20.0)
        assert own is not None
        self.assertAlmostEqual(own[0], 1700.56)
        self.assertEqual(own[1], "katch_mcardle_skinfold")
        # Without skinfolds, 20 % measured: lean 56 kg, 370 + 1,209.6.
        measured = energy_inputs.own_resting(70.0, None, 20.0)
        assert measured is not None
        self.assertAlmostEqual(measured[0], 1579.6)
        self.assertEqual(measured[1], "katch_mcardle_body_fat")
        # No weight, or no usable body fat: the reader falls back to Garmin.
        self.assertIsNone(energy_inputs.own_resting(None, 12.0, 20.0))
        self.assertIsNone(energy_inputs.own_resting(70.0, None, 80.0))

    def test_a_skinfold_counts_only_within_ninety_days(self) -> None:
        folds = {"chest": 10.0, "abdomen": 15.0, "thigh": 12.0}

        def measured(day: date) -> SkinfoldMeasurement:
            return SkinfoldMeasurement(day, Sex.MALE, folds, 12.0)

        # 90 days before DAY3 still counts; 91 days before yields to the rest.
        self.assertEqual(
            energy_inputs.recent_fat_pct(measured(date(2026, 7, 5)), DAY3), 12.0
        )
        self.assertIsNone(
            energy_inputs.recent_fat_pct(measured(date(2026, 7, 4)), DAY3)
        )
        self.assertIsNone(energy_inputs.recent_fat_pct(None, DAY3))


def weigh(day: date, kg: Any, provider: ProviderId | None = ProviderId.GARMIN) -> Any:
    """A canonical body observation of ``provider`` (None: entered in the app)."""
    return SimpleNamespace(
        observed_on=day,
        sport=None,
        value=SimpleNamespace(value=kg),
        provenance=SimpleNamespace(provider=provider),
    )


GARMIN, WAHOO, INTERVALS = ProviderId.GARMIN, ProviderId.WAHOO, ProviderId.INTERVALS


class StepLengthTests(unittest.TestCase):
    """#3611: the walks' step length is distance over steps, bounded by height."""

    def walk(self, **summary: Any) -> dict[str, Any]:
        return {"id": "w", "sport": "walk", "started_at_local": "2026-10-01T12:00",
                "distance_m": 1400.0, **summary}  # fmt: skip

    def test_each_kind_of_walk_is_rejected_or_plausible_and_counted(self) -> None:
        walks = [
            # Distance and recorded steps: 1,500 m / 2,000 = 0.75 m.
            self.walk(distance_m=1500.0, steps=2000, avg_cadence=56,
                      moving_duration_s=1000, elapsed_duration_s=1000),
            # The device's own step length without steps: 1,400 / 0.70 = 2,000.
            self.walk(step_length_m=0.70, moving_duration_s=1000),
            # Short moving time: 1,400 m over 2 x 56 x 600 / 60 = 1,120 steps is
            # 1.25 m, over 0.99 m, so it does not count.
            self.walk(avg_cadence=56, moving_duration_s=600, elapsed_duration_s=1200),
            # No steps, no step length and no cadence: nothing to measure.
            self.walk(avg_cadence=0, moving_duration_s=1000),
            # A cadence already in steps (112) halves the step: 1,400 m over
            # 2 x 112 x 1,000 / 60 = 3,733 steps = 0.375 m, under 0.54 m.
            self.walk(avg_cadence=112, moving_duration_s=1000, elapsed_duration_s=1000),
        ]  # fmt: skip
        step, source = energy_inputs._step_length(walks, 1.80)
        # (1,500 + 1,400) / (2,000 + 2,000) = 0.725 m.
        self.assertAlmostEqual(0.725, step or 0.0, places=3)
        self.assertEqual("walks", source)
        self.assertEqual(
            {"walks": 5, "used": 2, "no_steps": 1, "implausible": 2,
             "without_recorded_steps": 4, "zero_cadence": 2, "short_moving_time": 1,
             "cadence_over_80": 1, "former_implausible": 2},
            energy_inputs.step_walk_counts(walks, 1.80),
        )  # fmt: skip

    def test_without_a_valid_walk_the_height_gives_the_step(self) -> None:
        odd = [self.walk(avg_cadence=0), self.walk(steps=700)]  # 2.0 m per step
        self.assertEqual(
            (0.415 * 1.80, "height"), energy_inputs._step_length(odd, 1.80)
        )


class StoreAdapterTests(unittest.TestCase):
    def test_weighins_keep_the_last_positive_value_per_day(self) -> None:
        observations = [
            weigh(DAY1, 70.4),
            weigh(DAY1, 70.1),
            weigh(DAY2, 0),
            weigh(DAY3, None),
        ]
        self.assertEqual(body_records.weighins(observations), {DAY1: 70.1})

    def test_weighins_take_the_app_first_garmin_as_fallback_never_wahoo(self) -> None:
        # #3557: day 1 a Garmin weigh-in and Wahoo's stored profile weight;
        # day 2 only the profile weight; day 3 Garmin's copy of a profile
        # value saved that day; day 4 an entry beside a profile value and an
        # old Intervals.icu row. Either read order gives the same days.
        observations = [
            weigh(DAY1, 80.0), weigh(DAY1, 84.0, WAHOO), weigh(DAY2, 84.0, WAHOO),
            weigh(DAY3, 79.0), weigh(DAY4, 78.0, None), weigh(DAY4, 77.0, INTERVALS),
        ]  # fmt: skip
        profile = {DAY3: 78.5, DAY4: 78.6}
        for order in (observations, observations[::-1]):
            with self.subTest(reversed=order is not observations):
                self.assertEqual(
                    {DAY1: 80.0, DAY3: 78.5, DAY4: 78.0},
                    body_records.weighins(order, profile),
                )
                self.assertEqual(
                    {
                        "by_source": {"app": 2, "garmin": 1},
                        "dropped_days": {"garmin": 1, "intervals": 1, "wahoo": 2},
                    },
                    body_records.weighin_counts(order, profile),
                )
        self.assertEqual({}, body_records.weighins([weigh(DAY2, 84.0, WAHOO)]))
        # #3559: after the app pushes an entry to Wahoo, the next refresh reads
        # it back as Wahoo's profile weight of that day; it still never counts.
        echo = [weigh(DAY1, 72.5, None), weigh(DAY1, 72.5, WAHOO)]
        self.assertEqual({DAY1: 72.5}, body_records.weighins(echo))
        self.assertEqual(
            {"by_source": {"app": 1, "garmin": 0}, "dropped_days": {"wahoo": 1}},
            body_records.weighin_counts(echo),
        )

    def test_garmin_body_fat_fills_only_days_without_an_app_value(self) -> None:
        # #3557: a profile skinfold or an entry wins its day over Garmin's copy;
        # another provider's body fat never counts.
        observations = [
            weigh(DAY1, 18.0), weigh(DAY2, 17.5), weigh(DAY3, 17.0, None),
            weigh(DAY3, 16.0), weigh(DAY4, 15.0, WAHOO),
        ]  # fmt: skip
        self.assertEqual(
            {DAY2: (17.5, GARMIN), DAY3: (17.0, None)},
            body_records.device_body_fats(observations, {DAY1}),
        )

    def test_the_profile_weight_leaves_the_window_as_the_scale_gave_it(self) -> None:
        # The issue's synthetic window: 28 days, a weigh-in on two of three
        # days falling 0.07 kg/day from 82.0 kg, and Wahoo's profile weight of
        # 84.0 kg stored on every day (#3557).
        end = date(2026, 11, 15)
        days = [end - timedelta(days=27 - n) for n in range(28)]
        scale = [weigh(d, 82.0 - 0.07 * n) for n, d in enumerate(days) if n % 3 != 2]
        profile = [weigh(d, 84.0, WAHOO) for d in days]

        def window(observations: list[Any]) -> EnergyWindow:
            weights = body_records.weighins(observations)
            inputs = EnergyInputs(
                {
                    d: DayInputs(
                        intake_kcal=2473.0,
                        entries=3,
                        activity_kcal=600.0,
                        weight_kg=weights.get(d),
                    )
                    for d in days
                },
                {},
                rmr_kcal=1700.0,
            )
            return energy_windows(inputs, end, weeks=1)[-1]

        alone, mixed = window(scale), window(scale + profile)
        self.assertEqual(19, mixed.weighins)
        self.assertIsNotNone(mixed.adaptive_tdee)
        self.assertEqual(alone, mixed)
        # Profile weights alone are no weigh-ins at all.
        self.assertEqual("weighins", window(profile).insufficient)

    def test_the_resting_reference_and_the_formula_rows_share_one_weight(self) -> None:
        # #3551: the resting reference read the latest raw weigh-in while the
        # rows took the lagging trend; now both take the current weight: on a
        # steady 0.07 kg/day loss the line at the last weigh-in (80.17 kg, the
        # day before ``end``), which the trend had put 2 kg higher.
        end = date(2026, 11, 15)
        scale = [weigh(end - timedelta(days=n), 80.1 + 0.07 * n) for n in range(1, 28)]
        inputs = EnergyInputs(
            {o.observed_on: DayInputs(weight_kg=o.value.value) for o in scale},
            {end: 18.0},
            rmr_kcal=1700.0,
        )
        trend = weight_trend({o.observed_on: o.value.value for o in scale})
        body = energy_inputs.resolve_body(inputs, trend, end)
        assert body is not None
        reference = body_records.current_weight(scale, {}, end)
        self.assertEqual(reference, body["weight_kg"])
        self.assertAlmostEqual(80.17, reference or 0.0, places=2)

    def test_the_lean_mass_is_carried_from_the_reading(self) -> None:
        # #3617 item 1: 20 % at 85 kg forty days ago (68 kg lean), then 0.1 kg
        # a day lost. With one reading the share is the prior 0.70, so 30 % of
        # the 4 kg is lean: 68 - 1.2 = 66.8 kg, not 81 kg x 80 % = 64.8 kg.
        end = date(2026, 11, 15)
        first = end - timedelta(days=40)
        weights = {first + timedelta(days=n): 85.0 - 0.1 * n for n in range(41)}
        inputs = EnergyInputs(
            {d: DayInputs(weight_kg=w) for d, w in weights.items()},
            {first: 20.0},
            rmr_kcal=1700.0,
        )
        trend = weight_trend(weights)
        body = energy_inputs.resolve_body(inputs, trend, end)
        assert body is not None
        carried = 0.8 * trend[first] + 0.3 * (trend[end] - trend[first])
        self.assertAlmostEqual(carried, body["lean_mass_kg"], places=6)
        self.assertAlmostEqual(66.8, body["lean_mass_kg"], delta=0.05)
        # Without a dated reading there is no body: the profile's stored body
        # fat is no source (#3640).
        self.assertIsNone(
            energy_inputs.resolve_body(replace(inputs, body_fat_pct={}), trend, end)
        )

    def test_the_resting_reference_carries_the_same_lean_mass(self) -> None:
        # Gitar on #3642: the own Katch-McArdle takes the lean mass the formula
        # rows carry; the reading lies before the read start, and the weights
        # reach back to it, so it keeps its own weight (85 kg, 68 kg lean).
        first = DAY3 - timedelta(days=40)
        weights = {first + timedelta(days=n): 85.0 - 0.1 * n for n in range(41)}
        rated = replace(
            sources(),
            resting=lambda _db, _day: (1500.0, energy_inputs.RESTING_FROM_SKINFOLDS),
            weights=lambda _db, s, e: {d: w for d, w in weights.items() if s <= d <= e},
            body_fat=lambda _db, _s, _e: {first: 20.0},
        )
        inputs, _ = read_energy_inputs(None, rated, DAY1, DAY3)
        body = energy_inputs.resolve_body(inputs, weight_trend(weights), DAY3)
        assert body is not None
        self.assertAlmostEqual(66.8, body["lean_mass_kg"], delta=0.05)
        # Katch-McArdle 370 + 21.6 x lean, the same lean mass as the rows.
        self.assertAlmostEqual(
            370 + 21.6 * body["lean_mass_kg"], inputs.rmr_kcal or 0.0, places=6
        )
        self.assertEqual(energy_inputs.RESTING_FROM_SKINFOLDS, inputs.rmr_source)

    def test_energy_basis_counts_the_latest_window_weighin_sources(self) -> None:
        # #3557: counts only, read over the latest 28-day window.
        seen: list[tuple[date, date]] = []

        def counts(_db: Any, start: date, end: date) -> dict[str, Any]:
            seen.append((start, end))
            return {"by_source": {"app": 9, "garmin": 2}, "dropped_days": {"wahoo": 28}}

        basis = energy_basis(None, replace(sources(), weighin_counts=counts), DAY3)
        # The window ends yesterday, DAY2, as the view's does (#3617 item 7).
        self.assertEqual([(DAY2 - timedelta(days=27), DAY2)], seen)
        self.assertEqual(
            {"by_source": {"app": 9, "garmin": 2}, "dropped_days": {"wahoo": 28}},
            basis["latest_window_weighin_sources"],
        )

    def test_the_source_offset_averages_each_source_s_residual(self) -> None:
        # #3638: flat weight, Garmin on even days 0.6 kg above the app's on odd
        # days: around each window's line Garmin sits about +0.3, the app -0.3.
        end = date(2026, 11, 18)
        days = {
            end - timedelta(days=n): DayInputs(
                intake_kcal=2500.0, entries=3, weight_kg=80.0 + 0.6 * (n % 2 == 0)
            )
            for n in range(60)
        }
        inputs = EnergyInputs(days, {}, rmr_kcal=1600.0)
        windows = energy_windows(inputs, end, weeks=2)
        by_day = {d: ("garmin" if (end - d).days % 2 == 0 else "app") for d in days}
        self.assertEqual(
            {"mixed_windows": 2, "mean_residual_kg": {"app": -0.3, "garmin": 0.3}},
            energy_inputs.source_offset(inputs, windows, by_day),
        )
        # One source only: no mixed window, no means.
        self.assertEqual(
            {"mixed_windows": 0, "mean_residual_kg": {"app": None, "garmin": None}},
            energy_inputs.source_offset(inputs, windows, dict.fromkeys(days, "app")),
        )

    def test_underlog_counts_compare_unstable_windows_with_their_neighbours(
        self,
    ) -> None:
        # #3658: the latest 14 days are logged at 1,250 kcal, complete (above 70 %
        # of the 1,600 rest) but narrowly (below 85 %), the days before at
        # 2,500; the weight stays flat, so the latest windows turn unstable.
        end = date(2026, 11, 18)
        days = {
            end - timedelta(days=n): DayInputs(
                intake_kcal=1250.0 if n < 14 else 2500.0,
                entries=3,
                weight_kg=80.0 + 0.1 * (n % 2),
            )
            for n in range(84)
        }
        inputs = EnergyInputs(days, {}, rmr_kcal=1600.0)
        windows = energy_windows(inputs, end, weeks=8)
        counts = energy_inputs.underlog_counts(inputs, windows)
        # The logged intake carries the whole jump: both gaps are equal, as the
        # weight stays flat (store within 3 kcal/day).
        self.assertGreaterEqual(counts["unstable_tdee_gap_kcal"], 300)
        self.assertEqual(
            counts["unstable_tdee_gap_kcal"], counts["unstable_intake_gap_kcal"]
        )
        # Eaten for training is no intake gap (Gitar on #3660): 600 kcal more
        # food on the 14 days with 600 kcal more activity, and a 1 kg water step
        # 10 days ago that makes the windows unstable: the weight carries it.
        trained = {
            d: replace(
                v,
                intake_kcal=3100.0 if (end - d).days < 14 else 2500.0,
                activity_kcal=600.0 if (end - d).days < 14 else 0.0,
                weight_kg=(v.weight_kg or 0.0) + (1.0 if (end - d).days < 10 else 0.0),
            )
            for d, v in days.items()
        }
        fed = EnergyInputs(trained, {}, rmr_kcal=1600.0)
        fed_counts = energy_inputs.underlog_counts(
            fed, energy_windows(fed, end, weeks=8)
        )
        self.assertGreaterEqual(fed_counts["unstable_tdee_gap_kcal"], 200)
        self.assertEqual(0, fed_counts["unstable_intake_gap_kcal"])
        narrow = counts["narrow_complete_day_pct"]
        self.assertGreater(narrow["unstable"], narrow["stable"] or 0)
        # Nothing unstable, nothing to compare.
        flat = {d: replace(v, intake_kcal=2500.0) for d, v in days.items()}
        steady = EnergyInputs(flat, {}, rmr_kcal=1600.0)
        self.assertEqual(
            {"unstable_tdee_gap_kcal": None, "unstable_intake_gap_kcal": None,
             "narrow_complete_day_pct": {"unstable": None, "stable": 0}},
            energy_inputs.underlog_counts(steady, energy_windows(steady, end, weeks=8)),
        )  # fmt: skip

    def test_session_streams_take_the_first_stream_set(self) -> None:
        laps = SimpleNamespace(detail=object())
        streams = SimpleNamespace(
            detail=Mock(spec=ActivityStreamSet, channels={"d": (1.0,)})
        )
        with patch.object(
            energy_inputs, "detail_sets_for_session", return_value=[laps, streams]
        ) as read:
            self.assertEqual(energy_inputs.session_streams("db", "a1"), {"d": (1.0,)})
        read.assert_called_once_with("db", "a1")
        with patch.object(
            energy_inputs, "detail_sets_for_session", return_value=[laps]
        ):
            self.assertEqual(energy_inputs.session_streams("db", "a1"), {})


FOLDS = {"chest": 10.0, "abdomen": 20.0, "thigh": 15.0}


def folds_on(day: date, fat: float | None = 12.0) -> SkinfoldMeasurement:
    return SkinfoldMeasurement(day, Sex.MALE, FOLDS, fat)


def skinfold_sources(
    measured: list[SkinfoldMeasurement],
    *,
    weights: dict[date, float] | None = None,
    profile: dict[str, str] | None = None,
    device: dict[date, float] | None = None,
    entered: dict[date, float] | None = None,
) -> EnergySources:
    """Sources whose skinfold reads honour the day they are asked for.

    ``device`` is a scale's body fat per day (Garmin), read by window;
    ``entered`` the athlete's own percentages (no provider, #3402).
    """
    fats: dict[date, tuple[float, ProviderId | None]] = {
        day: (value, ProviderId.GARMIN) for day, value in (device or {}).items()
    }
    fats |= {day: (value, None) for day, value in (entered or {}).items()}

    def until(day: date) -> list[SkinfoldMeasurement]:
        return sorted(
            (m for m in measured if m.measured_on <= day),
            key=lambda m: m.measured_on,
            reverse=True,
        )

    return replace(
        sources(profile=profile or {"height_cm": "180", "birth_year": "1990"}),
        weights=lambda _db, _s, _e: {DAY1: 70.0} if weights is None else weights,
        body_fat=lambda _db, _s, end: {
            m.measured_on: m.body_fat_pct
            for m in until(end)
            if m.body_fat_pct is not None
        },
        skinfolds=lambda _db, day: until(day),
        device_body_fat=lambda _db, start, end: {
            day: value for day, value in fats.items() if start <= day <= end
        },
    )


class SkinfoldBodyTests(unittest.TestCase):
    """#3336: the formula rows' body, and why it is missing, from a fixture."""

    def test_a_measurement_of_today_fills_the_rows_of_the_view_ending_yesterday(
        self,
    ) -> None:
        # The view ends yesterday (DAY3); the dialog dates a new measurement today.
        # Only the body-fat read is set, so the same fixture runs on the old reader.
        src = replace(
            sources(),
            body_fat=lambda _db, _s, end: {DAY4: 12.0} if end >= DAY4 else {},
        )
        view = read_energy_view(None, src, DAY3)
        self.assertEqual("2026-10-04", (view["body"] or {}).get("date"))
        katch = next(r for r in view["rows"] if r["source"] == "katch_mcardle")
        self.assertIsNotNone(katch["resting_kcal"])

    def test_each_missing_body_names_its_reason(self) -> None:
        cases = {
            "no_skinfold": skinfold_sources([]),
            "skinfold_too_old": skinfold_sources([folds_on(date(2026, 6, 1))]),
            "no_birth_year": skinfold_sources(
                [folds_on(DAY2, None)], profile={"height_cm": "180"}
            ),
            "invalid": skinfold_sources([folds_on(DAY2, None)]),
            "no_weight": skinfold_sources([folds_on(DAY2)], weights={}),
        }
        for reason, src in cases.items():
            with self.subTest(reason=reason):
                view = read_energy_view(None, src, DAY3)
                self.assertEqual(reason, view["skinfold_missing"])
                self.assertIsNone(view["body"])

    def test_the_diagnostics_count_measurements_and_the_reason_only(self) -> None:
        resolved = energy_basis(
            None,
            skinfold_sources([folds_on(DAY2), folds_on(DAY1, None)]),
            DAY3,
        )
        # Today DAY3, the windows end on DAY2, the skinfold's day (#3617).
        self.assertEqual(
            {
                "skinfolds_with_body_fat": 1,
                "newest_skinfold_age_days": 0,
                "body_resolved": True,
                "body_missing": None,
            },
            {key: resolved[key] for key in SKINFOLD_KEYS},
        )
        missing = energy_basis(None, skinfold_sources([]), DAY3)
        self.assertEqual(
            (0, None, False, "no_skinfold"), tuple(missing[k] for k in SKINFOLD_KEYS)
        )


OLD = date(2026, 6, 1)  # more than 90 days before DAY3
PROFILE_FAT = {"height_cm": "180", "birth_year": "1990", "body_fat_pct": "18,5"}


class BodyFatFallbackTests(unittest.TestCase):
    """#3362, #3402: skinfold, entered, device or scale; labelled. The profile's
    stored body fat is no source (#3640)."""

    def body(self, src: EnergySources) -> dict[str, Any] | None:
        return read_energy_view(None, src, DAY3)["body"]

    def test_skinfold_beats_entered_beats_device_and_the_profile_never_counts(
        self,
    ) -> None:
        cases = [
            (
                skinfold_sources(
                    [folds_on(DAY1)],
                    entered={DAY2: 15.0},
                    device={DAY2: 20.0},
                    profile=PROFILE_FAT,
                ),
                ("skinfold", "Hautfalten", 12.0, "2026-10-01"),
            ),
            (
                # An older entry still beats a newer scale reading (#3402).
                skinfold_sources(
                    [], entered={DAY1: 15.0}, device={DAY2: 20.0}, profile=PROFILE_FAT
                ),
                ("entered", "Lokal", 15.0, "2026-10-01"),
            ),
            (
                skinfold_sources([], device={DAY2: 20.0}, profile=PROFILE_FAT),
                ("device", "Garmin Connect", 20.0, "2026-10-02"),
            ),
            (
                skinfold_sources([], profile=PROFILE_FAT),
                (None, None, None, None),
            ),
        ]
        for src, expected in cases:
            with self.subTest(source=expected[0]):
                body = self.body(src) or {}
                self.assertEqual(
                    expected,
                    (
                        body.get("source"),
                        body.get("label"),
                        body.get("body_fat_pct"),
                        body.get("date"),
                    ),
                )

    def test_the_ninety_days_hold_for_skinfold_entered_and_device(self) -> None:
        old = skinfold_sources(
            [folds_on(OLD)],
            entered={OLD: 15.0},
            device={OLD: 20.0},
            profile=PROFILE_FAT,
        )
        # The profile's stored body fat fills nothing (#3640): the reason stays.
        for src in (old, skinfold_sources([folds_on(OLD)], device={OLD: 20.0})):
            view = read_energy_view(None, src, DAY3)
            self.assertIsNone(view["body"])
            self.assertEqual("skinfold_too_old", view["skinfold_missing"])

    def test_without_any_source_the_reason_stays(self) -> None:
        view = read_energy_view(None, skinfold_sources([]), DAY3)
        self.assertIsNone(view["body"])
        self.assertEqual("no_skinfold", view["skinfold_missing"])
        katch = next(r for r in view["rows"] if r["source"] == "katch_mcardle")
        self.assertIsNone(katch["resting_kcal"])

    def test_a_fallback_fills_both_formula_rows(self) -> None:
        src = skinfold_sources([], device={DAY2: 20.0}, profile=PROFILE_FAT)
        view = read_energy_view(None, src, DAY3)
        self.assertIsNone(view["skinfold_missing"])
        for source in ("katch_mcardle", "cunningham"):
            row = next(r for r in view["rows"] if r["source"] == source)
            self.assertIsNotNone(row["resting_kcal"])

    def test_learned_everyday_fits_each_formula_and_counts_negatives(self) -> None:
        # #3577: one estimated window, own TDEE 2,700 less activity 600 and
        # digestion 250 is 1,850 against Katch-McArdle 1,838.8 (68 kg lean)
        # and Cunningham 1,996: one fit above 0, one below.
        body = {"lean_mass_kg": 68.0}
        estimated = EnergyWindow(
            DAY3, 28, 28, 0, 28, 600.0, 600.0, 250.0,
            Estimate(2700.0, 120.0), None, Estimate(1850.0, 150.0), None,
        )  # fmt: skip
        empty = replace(estimated, adaptive_tdee=None, derived_rmr=None)
        fits = energy_inputs.learned_everyday([empty, estimated], [body, body])
        km, cunningham = fits["katch_mcardle"], fits["cunningham"]
        assert km is not None and cunningham is not None
        self.assertEqual((8, 1), (round(km.raw_kcal), km.windows))
        self.assertLess(cunningham.raw_kcal, 0)
        # #3689: it learns from the 12-week base's windows, so an unstable one
        # (#3614) teaches through its own band, but counts as no independent one.
        unstable = replace(estimated, stable=False)
        learned = energy_inputs.learned_everyday([unstable], [body])["katch_mcardle"]
        assert learned is not None
        self.assertEqual((8, 0), (round(learned.raw_kcal), learned.independent))
        # Independence counts only the windows the fit uses (Gitar, #3693): the
        # oldest of three windows four weeks apart has no body at its end.
        spaced = [replace(estimated, end=DAY3 - timedelta(days=n)) for n in (56, 28, 0)]
        used = energy_inputs.learned_everyday(spaced, [None, body, body])[
            "katch_mcardle"
        ]
        assert used is not None
        self.assertEqual(2, used.independent)
        # Without a body at a window's end it has no fit.
        self.assertIsNone(
            energy_inputs.learned_everyday([estimated], [None])["cunningham"]
        )

    def test_the_diagnostics_count_the_body_sources(self) -> None:
        basis = energy_basis(
            None, skinfold_sources([], device={DAY2: 20.0}, profile=PROFILE_FAT), DAY3
        )
        self.assertEqual("device", basis["body_source"])
        # #3443: DAY2's resting is 0, so today's Garmin rest comes from DAY1.
        self.assertEqual(2, basis["today_resting_days_back"])
        # #3577: no estimated window, so no learned everyday fit below 0.
        self.assertEqual(0, basis["learned_everyday_negative"])
        counts = basis["body_source_windows"]
        self.assertEqual(12, sum(counts.values()))
        # The profile is no body source any more (#3640).
        self.assertEqual({"skinfold", "entered", "device", "none"}, set(counts))
        # The latest window has the device value; older windows lack a weight.
        self.assertGreaterEqual(counts["device"], 1)
        self.assertGreaterEqual(counts["none"], 1)


SKINFOLD_KEYS = (
    "skinfolds_with_body_fat",
    "newest_skinfold_age_days",
    "body_resolved",
    "body_missing",
)


if __name__ == "__main__":
    unittest.main()

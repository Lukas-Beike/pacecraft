"""The adaptive expenditure reader (#3100) over fake sources, checked by hand."""

from __future__ import annotations

import unittest
from datetime import date
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

from backend.athlete.body_composition import Sex
from backend.athlete.skinfolds import SkinfoldMeasurement
from backend.canonical.activity_detail import ActivityStreamSet
from backend.canonical.vocabulary import StreamChannel
from backend.nutrition import energy_inputs
from backend.nutrition.energy_inputs import (
    EnergySources,
    diagnostics,
    energy_basis,
    read_energy_inputs,
)

DAY1, DAY2, DAY3 = date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3)

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
        # 0.747 x 2.5 = 196,087.5 J plus floors 82,404 J = 66.561 kcal.
        assert first.activity_kcal is not None
        self.assertAlmostEqual(first.activity_kcal, 602.294 + 66.561, places=2)
        # Ride: 790.421 kcal net (M11 example); NEAT 70 x 5,000 x 0.747 x 2.5 =
        # 653,625 J = 156.220 kcal; the ride adds no steps.
        assert second.activity_kcal is not None
        self.assertAlmostEqual(second.activity_kcal, 790.421 + 156.220, places=2)
        # Day 3 has no steps (NEAT unknown) and an unpriced swim (no HR fit from
        # one ride): its activity stays unknown, not the strength alone (#3187).
        self.assertIsNone(third.activity_kcal)
        self.assertEqual(
            [(c.method, c.kcal is None) for c in costs],
            [("minetti", False), ("power", False), ("met", False), ("unknown", True)],
        )
        self.assertEqual(inputs.body_fat_pct, {DAY1: 12.0})

    def test_own_resting_rate_first_garmin_only_as_fallback(self) -> None:
        own, _ = read_energy_inputs(None, sources(), DAY1, DAY3)
        self.assertEqual((own.rmr_kcal, own.rmr_source), (1680.0, "katch_mcardle"))
        # Garmin's zero resting value on day 2 is not a measurement.
        garmin, _ = read_energy_inputs(None, sources(resting=None), DAY1, DAY3)
        self.assertEqual((garmin.rmr_kcal, garmin.rmr_source), (1700.0, "garmin"))
        _, costs = read_energy_inputs(None, sources(), DAY1, DAY3)
        self.assertEqual(
            diagnostics(garmin, costs),
            {"latest_window_complete_days": None, "latest_window_weighins": None,
             "latest_window_insufficient": None, "windows": 0,
             "windows_with_estimate": 0, "calibration_windows": 0,
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
        # Walk: 70 x 1,400 x 2.5 = 245,000 J = 58.556 kcal; NEAT: (10,000 -
        # 1,866.67) x 0.75 x 2.5 x 70 = 1,067,500 J plus floors 82,404 J =
        # 274.834 kcal; the HR ride without a fit stays unpriced, so unknown.
        self.assertIsNone(inputs.days[DAY1].activity_kcal)
        alone, _ = read_energy_inputs(None, sources(sessions=[walk]), DAY1, DAY1)
        activity = alone.days[DAY1].activity_kcal
        assert activity is not None
        self.assertAlmostEqual(activity, 58.556 + 274.834, places=2)

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

    def test_run_grade_comes_from_the_streams(self) -> None:
        streams = {
            "run": {
                StreamChannel.DISTANCE_M: [0.0, 100.0],
                StreamChannel.ALTITUDE_M: [0.0, 10.0],
            }
        }
        _, costs = read_energy_inputs(None, sources(streams=streams), DAY1, DAY1)
        # 100 m at +10 %: 70 x 100 x 5.968214 / 4,184 = 9.985 kcal.
        self.assertAlmostEqual(costs[0].kcal or 0.0, 9.985, places=3)

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


class StoreAdapterTests(unittest.TestCase):
    def test_weighins_keep_the_last_positive_value_per_day(self) -> None:
        def weigh(day: date, kg: Any) -> Any:
            return SimpleNamespace(observed_on=day, value=SimpleNamespace(value=kg))

        observations = [
            weigh(DAY1, 70.4),
            weigh(DAY1, 70.1),
            weigh(DAY2, 0),
            weigh(DAY3, None),
        ]
        self.assertEqual(energy_inputs.weighins(observations), {DAY1: 70.1})

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


if __name__ == "__main__":
    unittest.main()

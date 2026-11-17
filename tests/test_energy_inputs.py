"""The adaptive expenditure reader (#3100) over fake sources, checked by hand."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

from backend.athlete.body_composition import Sex
from backend.athlete.skinfolds import SkinfoldMeasurement
from backend.canonical.activity_detail import ActivityStreamSet
from backend.canonical.vocabulary import ProviderId, StreamChannel
from backend.nutrition import energy_inputs
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

    def test_a_day_without_sessions_has_sport_zero(self) -> None:
        # Known to be zero, not unknown: 70 x 10,000 x 0.747 x 2.01 = 1,051,029 J
        # plus floors 82,404 J = 270.897 kcal NEAT, all of the activity.
        inputs, _ = read_energy_inputs(None, sources(sessions=[]), DAY1, DAY1)
        day = inputs.days[DAY1]
        self.assertEqual(day.sport_kcal, 0.0)
        self.assertAlmostEqual(day.neat_kcal or 0.0, 270.897, places=2)
        self.assertAlmostEqual(day.activity_kcal or 0.0, 270.897, places=2)

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
    fats = {day: (value, ProviderId.GARMIN) for day, value in (device or {}).items()}
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
        self.assertEqual(
            {
                "skinfolds_with_body_fat": 1,
                "newest_skinfold_age_days": 1,
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
    """#3362, #3402: skinfold, entered, device or scale, then the profile; labelled."""

    def body(self, src: EnergySources) -> dict[str, Any] | None:
        return read_energy_view(None, src, DAY3)["body"]

    def test_skinfold_beats_entered_beats_device_beats_profile(self) -> None:
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
                ("profile", "Profil", 18.5, None),
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
        self.assertEqual("profile", (self.body(old) or {}).get("source"))
        without_profile = skinfold_sources([folds_on(OLD)], device={OLD: 20.0})
        view = read_energy_view(None, without_profile, DAY3)
        self.assertIsNone(view["body"])
        self.assertEqual("skinfold_too_old", view["skinfold_missing"])

    def test_without_any_source_the_reason_stays(self) -> None:
        view = read_energy_view(None, skinfold_sources([]), DAY3)
        self.assertIsNone(view["body"])
        self.assertEqual("no_skinfold", view["skinfold_missing"])
        katch = next(r for r in view["rows"] if r["source"] == "katch_mcardle")
        self.assertIsNone(katch["resting_kcal"])

    def test_a_fallback_fills_both_formula_rows(self) -> None:
        view = read_energy_view(None, skinfold_sources([], profile=PROFILE_FAT), DAY3)
        self.assertIsNone(view["skinfold_missing"])
        for source in ("katch_mcardle", "cunningham"):
            row = next(r for r in view["rows"] if r["source"] == source)
            self.assertIsNotNone(row["resting_kcal"])

    def test_the_diagnostics_count_the_body_sources(self) -> None:
        basis = energy_basis(
            None, skinfold_sources([], device={DAY2: 20.0}, profile=PROFILE_FAT), DAY3
        )
        self.assertEqual("device", basis["body_source"])
        counts = basis["body_source_windows"]
        self.assertEqual(12, sum(counts.values()))
        self.assertEqual(
            {"skinfold", "entered", "device", "profile", "none"}, set(counts)
        )
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

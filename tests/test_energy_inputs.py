"""The adaptive expenditure reader (#3100) over fake sources, checked by hand."""

from __future__ import annotations

import unittest
from datetime import date
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

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
     "moving_duration_s": 3000, "distance_m": 10_000, "avg_cadence": 170},
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
) -> EnergySources:
    return EnergySources(
        diary=lambda _db, _s, _e: DIARY,
        daily_health=lambda _db, _s, _e: HEALTH,
        weights=lambda _db, _s, _e: {DAY1: 70.0},
        sessions=lambda _db, _s, _e: SESSIONS,
        streams=lambda _db, key: (streams or {}).get(key, {}),
        resting=lambda _db, _day: resting,
        body_fat=lambda _db, _s, _e: {DAY1: 12.0},
        profile=lambda: {"height_cm": "180"},
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
        self.assertIsNone(third.intake_kcal)
        self.assertEqual((first.weight_kg, second.weight_kg), (70.0, None))
        # Run: 70 kg x 10,000 m x 3.6 J/kg/m / 4,184 = 602.294 kcal. NEAT: step
        # 0.415 x 1.80 = 0.747 m; 10,000 - 170 x 50 = 1,500 steps; 70 x 1,500 x
        # 0.747 x 2.5 = 196,087.5 J plus floors 82,404 J = 66.561 kcal.
        assert first.activity_kcal is not None
        self.assertAlmostEqual(first.activity_kcal, 602.294 + 66.561, places=2)
        # Ride: 790.421 kcal net (M11 example); NEAT 70 x 5,000 x 0.747 x 2.5 =
        # 653,625 J = 156.220 kcal; the ride adds no steps.
        assert second.activity_kcal is not None
        self.assertAlmostEqual(second.activity_kcal, 790.421 + 156.220, places=2)
        # Strength (3.5 - 1) x 70 kcal/h; no steps that day, so no NEAT; the swim
        # has no HR fit (one ride is too few segments) and stays unpriced.
        self.assertAlmostEqual(third.activity_kcal or 0.0, 175.0)
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

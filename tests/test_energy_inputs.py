"""The adaptive expenditure reader (#3100) over fake sources, checked by hand."""

from __future__ import annotations

import unittest
from datetime import date
from typing import Any

from backend.canonical.vocabulary import StreamChannel
from backend.nutrition.energy_inputs import (
    EnergySources,
    diagnostics,
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
            {"days": 3, "logged_days": 2, "flagged_days": 1, "weighins": 1,
             "sessions": 4, "unpriced_sessions": 1, "rmr_source": "garmin"},
        )  # fmt: skip

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


if __name__ == "__main__":
    unittest.main()

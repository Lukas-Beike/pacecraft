"""Resting and daily expenditure per source and their fit (#3099)."""

from __future__ import annotations

import unittest
from datetime import date, timedelta
from typing import Any

from backend.nutrition.energy_balance import (
    DayInputs,
    EnergyInputs,
    EnergyWindow,
    Estimate,
)
from backend.nutrition.energy_view import energy_view, read_energy_view
from tests.test_energy_inputs import DAY1, DAY3, sources

END = date(2026, 11, 14)
EARLIER = END - timedelta(days=7)


def window(end: date, tdee: float | None, rmr: float | None = None) -> EnergyWindow:
    return EnergyWindow(
        end=end,
        days=28,
        complete_days=24,
        flagged_days=2,
        weighins=20,
        activity_kcal=630.0,
        activity_kcal_raw=600.0,
        tef_kcal=250.0,
        adaptive_tdee=None if tdee is None else Estimate(tdee, 120.0),
        adaptive_tdee_unflagged=None,
        derived_rmr=None if rmr is None else Estimate(rmr, 150.0),
        insufficient=None if tdee is not None else "weighins",
        rmr_source="katch_mcardle_skinfold",
    )


def inputs(body_fat: dict[date, float] | None = None) -> EnergyInputs:
    days = {END - timedelta(days=n): DayInputs(weight_kg=80.0) for n in range(40)}
    fat = {END - timedelta(days=10): 15.0} if body_fat is None else body_fat
    return EnergyInputs(days, fat, rmr_kcal=1838.8, rmr_source="katch_mcardle")


def garmin(resting: float = 1745.0, total: float = 3120.0) -> dict[str, Any]:
    return {
        (END - timedelta(days=n)).isoformat(): {
            "resting_kcal": resting,
            "calories": total,
        }
        for n in range(40)
    }


def row(view: dict[str, Any], source: str) -> dict[str, Any]:
    return next(r for r in view["rows"] if r["source"] == source)


class EnergyViewTests(unittest.TestCase):
    def test_rows_price_rest_from_lean_mass_and_add_the_same_day(self) -> None:
        # 80 kg at 15 % body fat: 68 kg lean mass. Katch-McArdle 370 + 21.6 x 68
        # = 1,838.8; Cunningham 500 + 22 x 68 = 1,996. The day adds the raw
        # own activity 600 and TEF 250 to both.
        view = energy_view(inputs(), [window(END, 2700.0, 1850.0)], garmin())
        km, cunningham = row(view, "katch_mcardle"), row(view, "cunningham")
        self.assertEqual((1839, 2689), (km["resting_kcal"], km["total_kcal"]))
        self.assertEqual(
            (1996, 2846), (cunningham["resting_kcal"], cunningham["total_kcal"])
        )
        self.assertEqual(
            (1745, 3120),
            (row(view, "device")["resting_kcal"], row(view, "device")["total_kcal"]),
        )
        own = row(view, "own")
        self.assertEqual(
            (1850, 150, 2700, 120),
            (
                own["resting_kcal"],
                own["resting_band_kcal"],
                own["total_kcal"],
                own["total_band_kcal"],
            ),
        )
        self.assertEqual(
            {"date": "2026-11-04", "body_fat_pct": 15.0, "lean_mass_kg": 68.0},
            view["skinfold"],
        )

    def test_the_closest_source_fits_best_and_hits_within_100_kcal(self) -> None:
        # Errors: Katch-McArdle |2,688.8 - 2,700| = 11, Cunningham 146, Garmin 420.
        view = energy_view(inputs(), [window(END, 2700.0)], garmin())
        self.assertEqual("katch_mcardle", view["best"])
        self.assertEqual([11], row(view, "katch_mcardle")["errors_kcal"])
        self.assertEqual(
            [100, 0, 0],
            [
                row(view, s)["hit_rate_pct"]
                for s in ("katch_mcardle", "cunningham", "device")
            ],
        )

    def test_a_window_without_an_estimate_has_no_error(self) -> None:
        view = energy_view(
            inputs(), [window(EARLIER, None), window(END, 2700.0)], garmin()
        )
        km = row(view, "katch_mcardle")
        self.assertEqual([None, 11], km["errors_kcal"])
        self.assertEqual((11, 100), (km["mean_error_kcal"], km["hit_rate_pct"]))
        self.assertEqual(["2026-11-07", "2026-11-14"], view["windows"])
        self.assertEqual(1, view["basis"]["windows_with_estimate"])

    def test_without_a_recent_skinfold_the_formula_rows_stay_unknown(self) -> None:
        for fat in ({}, {END - timedelta(days=91): 15.0}):
            with self.subTest(fat=fat):
                view = energy_view(inputs(fat), [window(END, 2700.0)], garmin())
                km = row(view, "katch_mcardle")
                self.assertEqual(
                    (None, None, None),
                    (km["resting_kcal"], km["total_kcal"], km["mean_error_kcal"]),
                )
                self.assertIsNone(view["skinfold"])
                self.assertEqual("device", view["best"])

    def test_without_any_basis_nothing_fits(self) -> None:
        view = energy_view(inputs({}), [window(END, None)], {})
        self.assertIsNone(view["best"])
        self.assertEqual("weighins", view["basis"]["insufficient"])
        self.assertIsNone(row(view, "own")["total_kcal"])
        self.assertIsNone(row(view, "device")["hit_rate_pct"])

    def test_garmin_means_only_the_window_and_skips_missing_days(self) -> None:
        health = garmin()
        health[END.isoformat()] = {"resting_kcal": 0, "calories": None}
        # A day before the 28-day window does not count.
        health[(END - timedelta(days=28)).isoformat()] = {
            "resting_kcal": 9000.0,
            "calories": 9000.0,
        }
        view = energy_view(inputs(), [window(END, 3100.0)], health)
        self.assertEqual(
            (1745, 3120),
            (row(view, "device")["resting_kcal"], row(view, "device")["total_kcal"]),
        )
        self.assertEqual(20, row(view, "device")["mean_error_kcal"])

    def test_the_reader_builds_twelve_windows_with_garmin_beside_them(self) -> None:
        view = read_energy_view(None, sources(), DAY3)
        self.assertEqual(12, len(view["windows"]))
        self.assertEqual("intake_days", view["basis"]["insufficient"])
        # Garmin's one positive resting value; day 2's zero is no measurement.
        self.assertEqual(1700, row(view, "device")["resting_kcal"])
        # 70 kg at 12 %: 61.6 kg lean mass, Katch-McArdle 370 + 21.6 x 61.6.
        self.assertEqual(1701, row(view, "katch_mcardle")["resting_kcal"])
        self.assertEqual(DAY1.isoformat(), view["skinfold"]["date"])


if __name__ == "__main__":
    unittest.main()

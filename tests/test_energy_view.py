"""Resting and daily expenditure per source and their fit (#3099, #3187)."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date, timedelta
from typing import Any

from backend.canonical.vocabulary import ProviderId
from backend.nutrition.energy_balance import (
    DayInputs,
    EnergyInputs,
    EnergyWindow,
    Estimate,
)
from backend.nutrition.energy_view import energy_view, read_energy_view
from backend.providers.source_labels import SourceView, provider_label
from tests.test_energy_inputs import DAY1, DAY3, sources

END = date(2026, 11, 14)
GARMIN_IDS = {ProviderId.GARMIN.value: "g1"}
# Four weekly windows ending on END, oldest first.
ENDS = [END - timedelta(days=7 * n) for n in (3, 2, 1, 0)]


def window(
    end: date,
    tdee: float | None,
    rmr: float | None = None,
    band: float = 120.0,
    factor: float = 1.05,
) -> EnergyWindow:
    # Raw everyday 250 + sport 350 = activity 600; calibrated x k.
    return EnergyWindow(
        neat_kcal_raw=250.0,
        sport_kcal_raw=350.0,
        neat_kcal=250.0 * factor,
        sport_kcal=350.0 * factor,
        calibration_factor=factor,
        end=end,
        days=28,
        complete_days=24,
        flagged_days=2,
        weighins=20,
        activity_kcal=630.0,
        activity_kcal_raw=600.0,
        tef_kcal=250.0,
        adaptive_tdee=None if tdee is None else Estimate(tdee, band),
        adaptive_tdee_unflagged=None,
        derived_rmr=None if rmr is None else Estimate(rmr, 150.0),
        insufficient=None if tdee is not None else "weighins",
        rmr_source="katch_mcardle_skinfold",
    )


def windows(tdee: float = 2700.0, band: float = 120.0) -> list[EnergyWindow]:
    return [window(end, tdee, band=band) for end in ENDS]


def inputs(body_fat: dict[date, float] | None = None) -> EnergyInputs:
    days = {END - timedelta(days=n): DayInputs(weight_kg=80.0) for n in range(60)}
    fat = {END - timedelta(days=40): 15.0} if body_fat is None else body_fat
    return EnergyInputs(days, fat, rmr_kcal=1838.8, rmr_source="katch_mcardle")


def garmin(resting: float = 1745.0, total: float = 3120.0) -> dict[str, Any]:
    return {
        (END - timedelta(days=n)).isoformat(): {
            "resting_kcal": resting,
            "calories": total,
        }
        for n in range(60)
    }


def row(view: dict[str, Any], source: str) -> dict[str, Any]:
    return next(r for r in view["rows"] if r["source"] == source)


class EnergyViewTests(unittest.TestCase):
    def test_rows_price_rest_from_lean_mass_with_a_band(self) -> None:
        # 80 kg at 15 % body fat: 68 kg lean mass. Katch-McArdle 370 + 21.6 x 68
        # = 1,838.8; band hypot(10 % = 183.88, 1.96 x 21.6 x 80 x 3.5 % = 118.54)
        # = 218.78. Cunningham 500 + 22 x 68 = 1,996; band hypot(199.6, 120.74)
        # = 233.27. The day adds the raw own activity 600 and TEF 250, its band
        # 10 % of the activity: hypot(218.78, 60) = 226.86, hypot(233.27, 60)
        # = 240.86.
        view = energy_view(inputs(), [window(END, 2700.0, 1850.0)], garmin())
        km, cunningham = row(view, "katch_mcardle"), row(view, "cunningham")
        self.assertEqual(
            (1839, 219, 2689, 227),
            (
                km["resting_kcal"],
                km["resting_band_kcal"],
                km["total_kcal"],
                km["total_band_kcal"],
            ),
        )
        self.assertEqual(
            (1996, 233, 2846, 241),
            (
                cunningham["resting_kcal"],
                cunningham["resting_band_kcal"],
                cunningham["total_kcal"],
                cunningham["total_band_kcal"],
            ),
        )
        device = row(view, "device")
        self.assertEqual(
            (1745, None, 3120, None),
            (
                device["resting_kcal"],
                device["resting_band_kcal"],
                device["total_kcal"],
                device["total_band_kcal"],
            ),
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
            {
                "source": "skinfold",
                "label": "Hautfalten",
                "date": "2026-10-05",
                "body_fat_pct": 15.0,
                "lean_mass_kg": 68.0,
            },
            view["body"],
        )

    def test_a_lead_beyond_the_band_over_four_shared_windows_fits_best(self) -> None:
        # Errors per window: Katch-McArdle |2,688.8 - 2,700| = 11, Cunningham 146,
        # Garmin 420. The lead 146 - 11 = 135 exceeds the band 120.
        view = energy_view(inputs(), windows(), garmin())
        self.assertEqual("katch_mcardle", view["best"])
        self.assertEqual(
            {
                "shared_windows": 4,
                "windows": 4,
                "min_windows": 4,
                "reference_band_kcal": 120,
            },
            view["fit"],
        )
        self.assertEqual([11, 11, 11, 11], row(view, "katch_mcardle")["errors_kcal"])
        self.assertEqual(
            [11, 146, 420],
            [
                row(view, s)["mean_error_kcal"]
                for s in ("katch_mcardle", "cunningham", "device")
            ],
        )
        self.assertNotIn("hit_rate_pct", row(view, "katch_mcardle"))

    def test_a_lead_within_the_band_is_not_yet_distinguishable(self) -> None:
        view = energy_view(inputs(), windows(band=200.0), garmin())
        self.assertIsNone(view["best"])
        self.assertEqual(200, view["fit"]["reference_band_kcal"])

    def test_fewer_than_four_shared_windows_mark_nothing(self) -> None:
        view = energy_view(inputs(), windows()[1:], garmin())
        self.assertIsNone(view["best"])
        self.assertEqual(3, view["fit"]["shared_windows"])

    def test_every_source_is_compared_over_the_same_windows(self) -> None:
        # Garmin has no days in the oldest window, so that window drops out for
        # every source; four shared windows remain.
        oldest = END - timedelta(days=70)
        view = energy_view(inputs(), [window(oldest, 2700.0), *windows()], garmin())
        self.assertEqual(
            (4, 5), (view["fit"]["shared_windows"], view["fit"]["windows"])
        )
        self.assertIsNone(row(view, "katch_mcardle")["errors_kcal"][0])
        self.assertEqual("katch_mcardle", view["best"])

    def test_a_window_without_an_estimate_has_no_error(self) -> None:
        view = energy_view(
            inputs(), [window(ENDS[2], None), window(END, 2700.0)], garmin()
        )
        km = row(view, "katch_mcardle")
        self.assertEqual([None, 11], km["errors_kcal"])
        self.assertEqual(11, km["mean_error_kcal"])
        self.assertEqual(["2026-11-07", "2026-11-14"], view["windows"])
        self.assertEqual(1, view["basis"]["windows_with_estimate"])

    def test_without_a_recent_skinfold_the_formula_rows_stay_unknown(self) -> None:
        for fat in ({}, {END - timedelta(days=91): 15.0}):
            with self.subTest(fat=fat):
                view = energy_view(inputs(fat), [window(END, 2700.0)], garmin())
                km = row(view, "katch_mcardle")
                self.assertEqual(
                    (None, None, None, None),
                    (
                        km["resting_kcal"],
                        km["resting_band_kcal"],
                        km["total_kcal"],
                        km["mean_error_kcal"],
                    ),
                )
                self.assertIsNone(view["body"])

    def test_one_source_alone_is_never_the_best(self) -> None:
        view = energy_view(inputs({}), windows(), garmin())
        self.assertIsNone(view["best"])
        self.assertEqual(4, view["fit"]["shared_windows"])

    def test_without_any_basis_nothing_fits(self) -> None:
        view = energy_view(inputs({}), [window(END, None)], {})
        self.assertIsNone(view["best"])
        self.assertEqual("weighins", view["basis"]["insufficient"])
        self.assertIsNone(row(view, "own")["total_kcal"])
        self.assertEqual(
            {
                "shared_windows": 0,
                "windows": 1,
                "min_windows": 4,
                "reference_band_kcal": None,
            },
            view["fit"],
        )

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

    def test_the_device_row_names_the_providers_of_its_window(self) -> None:
        # #3323: the label comes from the provider labels the daily totals carry,
        # never from a client literal; the formula and own rows keep their keys.
        garmin_label = provider_label(ProviderId.GARMIN, SourceView.DAILY_HEALTH)
        wahoo_label = provider_label(ProviderId.WAHOO, SourceView.DAILY_HEALTH)
        health = {day: {**v, "source": garmin_label} for day, v in garmin().items()}
        view = energy_view(inputs(), [window(END, 3100.0)], health)
        self.assertEqual("Garmin Connect", row(view, "device")["label"])
        self.assertEqual(
            ["device"], [r["source"] for r in view["rows"] if "label" in r]
        )
        # Another provider in the window joins it; one before the window does not.
        health[END.isoformat()]["source"] = f"{garmin_label}, {wahoo_label}"
        health[(END - timedelta(days=28)).isoformat()]["source"] = "Other"
        view = energy_view(inputs(), [window(END, 3100.0)], health)
        self.assertEqual(
            ", ".join(sorted({garmin_label, wahoo_label})),
            row(view, "device")["label"],
        )
        # Without any daily total the row still names the wearable source.
        view = energy_view(inputs(), [window(END, 3100.0)], {})
        self.assertEqual(garmin_label, row(view, "device")["label"])

    def test_garmin_splits_its_day_into_everyday_and_sessions(self) -> None:
        # 28 days of 1,745 resting and 1,375 active kcal. One 60-min session of
        # 800 kcal on END: net of resting 800 - 1,745 / 1,440 x 60 = 727.29.
        # Sessions 727.29 / 28 = 25.97 per day; everyday 1,375 - 25.97 = 1,349.03.
        health = {key: {**day, "active_kcal": 1375.0} for key, day in garmin().items()}
        session = {
            "started_at_local": f"{END}T07:00:00",
            "energy_kcal": 800.0,
            "record_ids": GARMIN_IDS,
            "elapsed_duration_s": 3600,
        }
        view = energy_view(inputs(), [window(END, 2700.0)], health, [session])
        self.assertEqual(
            {
                "resting_kcal": 1745,
                "neat_kcal": 1349,
                "sport_kcal": 26,
                "tef_kcal": None,
                "shared": {"neat": False, "sport": False, "tef": False},
            },
            row(view, "device")["split"],
        )

    def test_formula_rows_share_the_own_terms_and_the_own_row_its_digestion(
        self,
    ) -> None:
        # Formula rows: resting by formula, then the raw own everyday 250, sport
        # 350 and TEF 250, all computed alike. The own row: the residual 1,850
        # and the calibrated 262.5 / 367.5 (k = 1.05), so only TEF is alike.
        view = energy_view(inputs(), [window(END, 2700.0, 1850.0)], garmin())
        self.assertEqual(
            {
                "resting_kcal": 1839,
                "neat_kcal": 250,
                "sport_kcal": 350,
                "tef_kcal": 250,
                "shared": {"neat": True, "sport": True, "tef": True},
            },
            row(view, "katch_mcardle")["split"],
        )
        self.assertEqual(
            {
                "resting_kcal": 1850,
                "neat_kcal": 262,
                "sport_kcal": 368,
                "tef_kcal": 250,
                "shared": {"neat": False, "sport": False, "tef": True},
            },
            row(view, "own")["split"],
        )
        plain = energy_view(
            inputs(), [window(END, 2700.0, 1850.0, factor=1.0)], garmin()
        )
        self.assertEqual(
            {"neat": True, "sport": True, "tef": True},
            row(plain, "own")["split"]["shared"],
        )
        self.assertIsNone(
            row(energy_view(inputs({}), [window(END, 2700.0)], garmin()), "own")[
                "split"
            ]
        )

    def test_a_resting_residual_below_the_reference_band_is_not_plausible(
        self,
    ) -> None:
        # Today's numbers: TDEE 2,832 +- 455 and the residual 1,105 +- 475,
        # whose top 1,580 lies below Katch-McArdle's 1,839 - 219 = 1,620.
        low = replace(
            window(END, 2832.0, band=455.0), derived_rmr=Estimate(1105.0, 475.0)
        )
        own = row(energy_view(inputs(), [low], garmin()), "own")
        self.assertEqual(
            (None, None, True, 2832, 455),
            (
                own["resting_kcal"],
                own["resting_band_kcal"],
                own["resting_implausible"],
                own["total_kcal"],
                own["total_band_kcal"],
            ),
        )
        self.assertIsNone(own["split"]["resting_kcal"])
        # Without a skinfold Garmin's 1,745 (no band) is the reference.
        no_fold = row(energy_view(inputs({}), [low], garmin()), "own")
        self.assertTrue(no_fold["resting_implausible"])
        # Overlapping bands keep the value: 1,500 + 475 = 1,975 reaches 1,620.
        overlap = replace(low, derived_rmr=Estimate(1500.0, 475.0))
        kept = row(energy_view(inputs(), [overlap], garmin()), "own")
        self.assertEqual(
            (1500, 475, False),
            (
                kept["resting_kcal"],
                kept["resting_band_kcal"],
                kept["resting_implausible"],
            ),
        )
        # Without any reference the residual stays a value.
        alone = row(energy_view(inputs({}), [low], {}), "own")
        self.assertEqual(
            (1105, False), (alone["resting_kcal"], alone["resting_implausible"])
        )

    def test_the_basis_names_neats_inputs_over_the_latest_window(self) -> None:
        # 20 of the window's 28 days have steps; the older days do not count.
        days = {
            END - timedelta(days=n): DayInputs(
                weight_kg=80.0,
                steps=12_000.0 if n < 20 else None,
                session_steps=2_000.0 if n < 10 else 0.0,
                floors=8.0 if n < 20 else None,
            )
            for n in range(60)
        }
        days[END - timedelta(days=40)] = DayInputs(steps=99_000.0)
        source = replace(inputs(), days=days, step_m=0.748, step_source="walks")
        neat = energy_view(source, [window(END, 2700.0)], garmin())["basis"]["neat"]
        # Session steps: 10 days x 2,000 over 20 days = 1,000 per day.
        self.assertEqual(
            {
                "days": 20,
                "steps_per_day": 12000,
                "session_steps_per_day": 1000,
                "floors_per_day": 8.0,
                "step_length_m": 0.75,
                "step_length_source": "walks",
            },
            neat,
        )
        self.assertIsNone(
            energy_view(inputs(), [window(END, 2700.0)], garmin())["basis"]["neat"]
        )

    def test_an_unpriced_session_drops_its_day_and_negative_everyday_stays_unknown(
        self,
    ) -> None:
        health = {key: {**day, "active_kcal": 1375.0} for key, day in garmin().items()}
        # END has more active kcal, so counting it would lift everyday to
        # (27 x 1,375 + 2,000) / 28 = 1,397; dropped, everyday stays 1,375.
        health[END.isoformat()] = {**health[END.isoformat()], "active_kcal": 2000.0}
        unpriced = {
            "started_at_local": f"{END}T07:00:00",
            "elapsed_duration_s": 3600,
            "record_ids": GARMIN_IDS,
        }
        split = row(
            energy_view(inputs(), [window(END, 2700.0)], health, [unpriced]), "device"
        )["split"]
        self.assertEqual((1375, 0), (split["neat_kcal"], split["sport_kcal"]))
        low = {key: {**day, "active_kcal": 10.0} for key, day in garmin().items()}
        big = [
            {
                "started_at_local": f"{END - timedelta(days=n)}T07:00:00",
                "energy_kcal": 900.0,
                "record_ids": GARMIN_IDS,
                "elapsed_duration_s": 3600,
            }
            for n in range(28)
        ]
        split = row(energy_view(inputs(), [window(END, 2700.0)], low, big), "device")[
            "split"
        ]
        self.assertIsNone(split["neat_kcal"])

    def test_a_session_garmin_did_not_record_is_not_garmin_sport(self) -> None:
        # A ride on another head unit while the watch was off is not in Garmin's
        # active kcal, so it neither counts as sport nor lowers everyday.
        health = {key: {**day, "active_kcal": 1375.0} for key, day in garmin().items()}
        other = {
            "started_at_local": f"{END}T07:00:00",
            "energy_kcal": 800.0,
            "elapsed_duration_s": 3600,
            "record_ids": {ProviderId.WAHOO.value: "w1"},
        }
        split = row(
            energy_view(inputs(), [window(END, 2700.0)], health, [other]), "device"
        )["split"]
        self.assertEqual((1375, 0), (split["neat_kcal"], split["sport_kcal"]))

    def test_flagged_meals_give_the_latest_clean_window_the_correction_and_bias(
        self,
    ) -> None:
        # #3100: the oldest window had no flagged day, so its TDEE is the value
        # without estimates; the latest carries the learned correction.
        clean = replace(
            window(ENDS[0], 2650.0),
            flagged_days=0,
            adaptive_tdee_unflagged=Estimate(2650.0, 130.0),
        )
        flagged = replace(
            window(END, 2700.0), adaptive_tdee_corrected=Estimate(2760.0, 160.0)
        )
        view = energy_view(
            inputs(), [clean, *windows()[1:3], flagged], garmin(), bias=(12.4, 8.6)
        )
        self.assertEqual(
            {
                "days": 2,
                "unflagged": {
                    "total_kcal": 2650,
                    "total_band_kcal": 130,
                    "end": ENDS[0].isoformat(),
                },
                "corrected": {"total_kcal": 2760, "total_band_kcal": 160},
                "bias_pct": 12,
                "bias_band_pct": 9,
            },
            view["flagged"],
        )
        none = energy_view(inputs(), windows(), garmin())["flagged"]
        self.assertEqual(
            (None, None, None), (none["unflagged"], none["corrected"], none["bias_pct"])
        )

    def test_the_reader_builds_twelve_windows_with_garmin_beside_them(self) -> None:
        view = read_energy_view(None, sources(), DAY3)
        self.assertEqual(12, len(view["windows"]))
        self.assertEqual("intake_days", view["basis"]["insufficient"])
        # Garmin's one positive resting value; day 2's zero is no measurement.
        self.assertEqual(1700, row(view, "device")["resting_kcal"])
        # 70 kg at 12 %: 61.6 kg lean mass, Katch-McArdle 370 + 21.6 x 61.6.
        self.assertEqual(1701, row(view, "katch_mcardle")["resting_kcal"])
        self.assertEqual(DAY1.isoformat(), view["body"]["date"])


if __name__ == "__main__":
    unittest.main()

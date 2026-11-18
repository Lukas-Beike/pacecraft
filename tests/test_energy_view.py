"""Resting and daily expenditure per source and their fit (#3099, #3187)."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date, timedelta
from typing import Any

from backend.canonical.vocabulary import ProviderId
from backend.errors import AppError
from backend.nutrition.energy_balance import (
    DayInputs,
    EnergyInputs,
    EnergyWindow,
    Estimate,
    history_start,
)
from backend.nutrition.energy_inputs import EnergySources
from backend.nutrition.energy_view import energy_view, read_energy_view, selected_day
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
    # The shown day (#3395): everyday 250 + sport 350 = activity 600, and TEF
    # 250 from 250 g protein (0.25 x 4 kcal/g), as the windows' means have.
    days[END] = DayInputs(
        weight_kg=80.0,
        activity_kcal=600.0,
        neat_kcal=250.0,
        sport_kcal=350.0,
        protein_g=250.0,
        entries=3,
    )
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
        # #3531: the day row's total is its 28-day resting rate plus the day's
        # 600 activity and 250 digestion, with the resting band; the 28-day row
        # keeps the window's TDEE and its band.
        own = row(view, "own")
        self.assertEqual(
            (1850, 150, 2700, 150),
            (
                own["resting_kcal"],
                own["resting_band_kcal"],
                own["total_kcal"],
                own["total_band_kcal"],
            ),
        )
        window_row = view["own_window"]
        self.assertEqual(
            (2700, 120), (window_row["total_kcal"], window_row["total_band_kcal"])
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

    def test_the_device_row_shows_its_day_and_nothing_for_a_day_without_values(
        self,
    ) -> None:
        # #3395: Garmin's own values of the shown day, never a mean; the fit
        # still compares over the windows.
        health = garmin()
        health[END.isoformat()] = {"resting_kcal": 0, "calories": None}
        health[(END - timedelta(days=3)).isoformat()] = {
            "resting_kcal": 1800.0,
            "calories": 2950.0,
        }
        view = energy_view(inputs(), [window(END, 3100.0)], health)
        device = row(view, "device")
        self.assertEqual((None, None), (device["resting_kcal"], device["total_kcal"]))
        # The fit stays on the window: (26 x 3,120 + 2,950) / 27 = 3,113.7 against
        # the own 3,100, END's missing total skipped.
        self.assertEqual(14, device["mean_error_kcal"])
        earlier = energy_view(
            inputs(), [window(END, 3100.0)], health, day=END - timedelta(days=3)
        )
        self.assertEqual(
            (1800, 2950),
            (
                row(earlier, "device")["resting_kcal"],
                row(earlier, "device")["total_kcal"],
            ),
        )

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
        # The day END: 1,745 resting and 1,375 active kcal. One 60-min session of
        # 800 kcal on it: net of resting 800 - 1,745 / 1,440 x 60 = 727.29, so
        # everyday 1,375 - 727.29 = 647.71 (#3395: the day, not a 28-day mean).
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
                "neat_kcal": 648,
                "sport_kcal": 727,
                "tef_kcal": None,
                "shared": {"neat": False, "sport": False, "tef": False},
            },
            row(view, "device")["split"],
        )

    def test_formula_rows_share_the_own_terms_and_the_own_row_its_digestion(
        self,
    ) -> None:
        # Formula rows: resting by formula, then the raw own everyday 250, sport
        # 350 and TEF 250, all computed alike. The own 28-day row (own_window,
        # #3531): the residual 1,850 and the calibrated 262.5 / 367.5 (k = 1.05),
        # so only TEF is alike; the own day row takes the formula rows' parts.
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
            view["own_window"]["split"],
        )
        self.assertEqual(
            {
                "resting_kcal": 1850,
                "neat_kcal": 250,
                "sport_kcal": 350,
                "tef_kcal": 250,
                "shared": {"neat": True, "sport": True, "tef": True},
            },
            row(view, "own")["split"],
        )
        plain = energy_view(
            inputs(), [window(END, 2700.0, 1850.0, factor=1.0)], garmin()
        )
        self.assertEqual(
            {"neat": True, "sport": True, "tef": True},
            plain["own_window"]["split"]["shared"],
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
        view = energy_view(inputs(), [low], garmin())
        own = row(view, "own")
        # #3531: the day has no total without its resting rate; the 28-day row
        # in Verlauf keeps the window's total.
        self.assertEqual(
            (None, None, True, None, None),
            (
                own["resting_kcal"],
                own["resting_band_kcal"],
                own["resting_implausible"],
                own["total_kcal"],
                own["total_band_kcal"],
            ),
        )
        self.assertEqual(
            (None, 350), (own["split"]["resting_kcal"], own["split"]["sport_kcal"])
        )
        self.assertEqual(
            (2832, 455, True),
            (
                view["own_window"]["total_kcal"],
                view["own_window"]["total_band_kcal"],
                view["own_window"]["resting_implausible"],
            ),
        )
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

    def test_the_basis_names_neats_inputs_of_the_shown_day(self) -> None:
        # #3395: the shown day's steps; a day without steps gives no basis.
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
        # END has 12,000 steps of which its sessions take 2,000.
        self.assertEqual(
            {
                "days": 1,
                "steps_per_day": 12000,
                "session_steps_per_day": 2000,
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
        # #3395: the day END with a session Garmin did not price has no split;
        # another day without a session splits into everyday 1,375 and sport 0.
        health[END.isoformat()] = {**health[END.isoformat()], "active_kcal": 2000.0}
        unpriced = {
            "started_at_local": f"{END}T07:00:00",
            "elapsed_duration_s": 3600,
            "record_ids": GARMIN_IDS,
        }
        split = row(
            energy_view(inputs(), [window(END, 2700.0)], health, [unpriced]), "device"
        )["split"]
        self.assertEqual((None, None), (split["neat_kcal"], split["sport_kcal"]))
        quiet = row(
            energy_view(
                inputs(), [window(END, 2700.0)], health, [], day=END - timedelta(days=1)
            ),
            "device",
        )["split"]
        self.assertEqual((1375, 0), (quiet["neat_kcal"], quiet["sport_kcal"]))
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
        view = read_energy_view(None, sources(), DAY3, DAY1)
        self.assertEqual(12, len(view["windows"]))
        self.assertEqual("intake_days", view["basis"]["insufficient"])
        # The shown day DAY1 has Garmin's resting value; DAY3 has none.
        self.assertEqual((DAY1.isoformat(), False), (view["day"], view["day_partial"]))
        self.assertEqual(1700, row(view, "device")["resting_kcal"])
        self.assertIsNone(
            row(read_energy_view(None, sources(), DAY3), "device")["resting_kcal"]
        )
        # 70 kg at 12 %: 61.6 kg lean mass, Katch-McArdle 370 + 21.6 x 61.6.
        self.assertEqual(1701, row(view, "katch_mcardle")["resting_kcal"])
        self.assertEqual(DAY1.isoformat(), view["body"]["date"])


class FitScaleTests(unittest.TestCase):
    """#3397: each Verlauf period's source means against the own estimate."""

    def test_each_period_means_its_days_and_names_the_own_span(self) -> None:
        source = inputs()
        logged = DayInputs(
            weight_kg=80.0, intake_kcal=2500.0, entries=3, activity_kcal=500.0
        )
        # Complete days in the latest week and four weeks back only.
        days = {**source.days, END: logged, END - timedelta(days=30): logged}
        health = garmin()
        # A day 20 days back counts for 12 weeks, not for 14 days.
        health[(END - timedelta(days=20)).isoformat()] = {
            "resting_kcal": 1745.0,
            "calories": 6000.0,
        }
        view = energy_view(replace(source, days=days), [window(END, 2700.0)], health)
        short, long = view["fit_scale"]["14d"], view["fit_scale"]["12w"]
        device = lambda scale: next(
            s for s in scale["sources"] if s["source"] == "device"
        )
        self.assertEqual(
            (14, 28, 2, 1),
            (
                short["days"],
                short["own_days"],
                short["weeks"],
                short["weeks_with_data"],
            ),
        )
        self.assertEqual(
            (84, 84, 12, 2),
            (long["days"], long["own_days"], long["weeks"], long["weeks_with_data"]),
        )
        self.assertEqual(3120, device(short)["total_kcal"])
        # The fixture holds 60 days: 59 at 3,120 and one at 6,000 = 3,168.
        self.assertEqual(3168, device(long)["total_kcal"])
        self.assertEqual(
            ["katch_mcardle", "cunningham", "device"],
            [s["source"] for s in long["sources"]],
        )
        # Too few logged days for the rules: no own value, never a guess.
        self.assertEqual((None, None), (long["own_kcal"], long["own_band_kcal"]))


class SelectedDayTests(unittest.TestCase):
    """#3395: the source rows show the diary's selected day, the own row 28 days."""

    def view(self, day: date, days: dict[date, DayInputs] | None = None, **kw: Any):
        source = inputs()
        if days:
            source = replace(source, days={**source.days, **days})
        return energy_view(
            source, [window(END, 2700.0, 1850.0)], garmin(), day=day, **kw
        )

    def test_a_day_without_a_session_has_sport_zero_not_a_mean(self) -> None:
        rest_day = END - timedelta(days=1)
        quiet = DayInputs(weight_kg=80.0, activity_kcal=250.0, neat_kcal=250.0,
                          sport_kcal=0.0, protein_g=250.0, entries=3)  # fmt: skip
        split = row(self.view(rest_day, {rest_day: quiet}), "katch_mcardle")["split"]
        self.assertEqual(
            (250, 0, 250), (split["neat_kcal"], split["sport_kcal"], split["tef_kcal"])
        )
        # 1,838.8 + 250 + 0 + 250.
        self.assertEqual(
            2339,
            row(self.view(rest_day, {rest_day: quiet}), "katch_mcardle")["total_kcal"],
        )
        with_session = row(self.view(END), "katch_mcardle")["split"]
        self.assertEqual(350, with_session["sport_kcal"])

    def test_a_day_without_meals_has_no_digestion_and_no_total(self) -> None:
        empty = END - timedelta(days=2)
        no_meals = DayInputs(
            weight_kg=80.0, activity_kcal=250.0, neat_kcal=250.0, sport_kcal=0.0
        )
        km = row(self.view(empty, {empty: no_meals}), "katch_mcardle")
        self.assertIsNone(km["split"]["tef_kcal"])
        self.assertIsNone(km["total_kcal"])
        self.assertEqual(1839, km["resting_kcal"])

    def test_today_without_any_data_yet_totals_its_resting_alone(self) -> None:
        # #3494: early morning there are no steps, sessions or meals yet; today
        # they count 0 so far, a past day with the same gaps has no total.
        today = END + timedelta(days=1)
        bare = DayInputs(weight_kg=80.0)
        view = self.view(today, {today: bare})
        km = row(view, "katch_mcardle")
        self.assertEqual(km["resting_kcal"], km["total_kcal"])
        parts = ("neat_kcal", "sport_kcal", "tef_kcal")
        self.assertEqual((0, 0, 0), tuple(km["split"][key] for key in parts))
        self.assertEqual(["neat", "sport", "tef"], km["pending_parts"])
        # Garmin has no active kcal for today yet either.
        self.assertEqual(["neat", "sport"], row(view, "device")["pending_parts"])
        past = END - timedelta(days=2)
        past_km = row(self.view(past, {past: bare}), "katch_mcardle")
        self.assertIsNone(past_km["total_kcal"])
        self.assertEqual([], past_km["pending_parts"])

    def test_today_keeps_an_unknown_part_unknown(self) -> None:
        # Gitar #3495: an unpriced session or steps without a step length are no
        # timing gap; that part and the total stay unknown, never 0.
        today = END + timedelta(days=1)
        session = {"started_at_local": f"{today}T07:00:00", "sport": "run"}
        unpriced = DayInputs(weight_kg=80.0, steps=4000.0, entries=1, protein_g=30.0)
        km = row(
            self.view(today, {today: unpriced}, sessions=[session]), "katch_mcardle"
        )
        self.assertIsNone(km["total_kcal"])
        self.assertEqual(
            (None, None), (km["split"]["neat_kcal"], km["split"]["sport_kcal"])
        )
        # Digestion counts the meals logged so far: 30 g protein x 4 kcal x 25 %.
        self.assertEqual(30, km["split"]["tef_kcal"])
        self.assertEqual([], km["pending_parts"])

    def test_today_fewer_day_steps_than_session_steps_leave_everyday_pending(
        self,
    ) -> None:
        # #3530: 500 day steps but 1,500 in the synced session: the day summary
        # lags behind, so everyday has not arrived yet; a past day keeps its value.
        today = END + timedelta(days=1)
        session = {"started_at_local": f"{today}T17:00:00", "sport": "strength"}
        behind = DayInputs(
            weight_kg=80.0, steps=500.0, session_steps=1500.0,
            neat_kcal=13.0, sport_kcal=160.0, activity_kcal=173.0,
        )  # fmt: skip
        km = row(self.view(today, {today: behind}, sessions=[session]), "katch_mcardle")
        self.assertIn("neat", km["pending_parts"])
        self.assertNotIn("sport", km["pending_parts"])
        # It counts 0 in the total, not the stale 13 kcal (Gitar, #3532).
        self.assertEqual(0, km["split"]["neat_kcal"])
        split = km["split"]
        self.assertEqual(
            km["resting_kcal"] + split["sport_kcal"] + (split["tef_kcal"] or 0),
            km["total_kcal"],
        )
        past = END - timedelta(days=2)
        km = row(self.view(past, {past: behind}, sessions=[session]), "katch_mcardle")
        self.assertEqual([], km["pending_parts"])

    def test_today_is_partial_and_a_past_day_has_garmin_values(self) -> None:
        today = END + timedelta(days=1)
        view = self.view(today, {today: DayInputs(weight_kg=80.0, sport_kcal=0.0)})
        self.assertEqual((today.isoformat(), True), (view["day"], view["day_partial"]))
        past = self.view(END - timedelta(days=5))
        self.assertEqual(
            ((END - timedelta(days=5)).isoformat(), False),
            (past["day"], past["day_partial"]),
        )
        self.assertEqual(
            (1745, 3120),
            (row(past, "device")["resting_kcal"], row(past, "device")["total_kcal"]),
        )

    def today_device(
        self, health: dict[str, Any], sessions: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        today = END + timedelta(days=1)
        view = energy_view(
            replace(inputs(), days={**inputs().days, today: DayInputs(weight_kg=80.0)}),
            [window(END, 2700.0, 1850.0)],
            health,
            sessions or [],
            day=today,
        )
        return row(view, "device")

    def test_today_a_garmin_summary_behind_its_session_shows_everyday_pending(
        self,
    ) -> None:
        # #3530: the session (348 kcal over 45 min) is synced, the day summary
        # still has 150 active kcal: everyday would be negative, so it has no
        # data yet and the day counts the session so far.
        today = (END + timedelta(days=1)).isoformat()
        health = garmin()
        health[END.isoformat()] = {"resting_kcal": 2280.0, "calories": 3000.0}
        health[today] = {"resting_kcal": 600.0, "active_kcal": 150.0}
        session = {
            "started_at_local": f"{today}T17:00:00",
            "energy_kcal": 348.0,
            "record_ids": GARMIN_IDS,
            "elapsed_duration_s": 2700,
        }
        device = self.today_device(health, [session])
        # Sport 348 - 2,280 / 1,440 x 45 = 276.75; total 2,280 + 276.75.
        self.assertEqual(
            (277, None, 2557),
            (
                device["split"]["sport_kcal"],
                device["split"]["neat_kcal"],
                device["total_kcal"],
            ),
        )
        self.assertIn("neat", device["pending_parts"])
        # Once the summary catches up (405 active kcal), everyday is the rest.
        health[today]["active_kcal"] = 405.0
        device = self.today_device(health, [session])
        self.assertEqual(
            (277, 128, 2685),
            (
                device["split"]["sport_kcal"],
                device["split"]["neat_kcal"],
                device["total_kcal"],
            ),
        )
        self.assertNotIn("neat", device["pending_parts"])

    def test_today_garmin_ruhe_is_the_newest_full_day_within_a_week(self) -> None:
        """#3443: today's resting is still counting; the row shows yesterday's."""
        today = (END + timedelta(days=1)).isoformat()
        health = garmin()
        health[END.isoformat()] = {"resting_kcal": 1800.0, "calories": 2900.0}
        health[today] = {
            "resting_kcal": 900.0,
            "active_kcal": 400.0,
            "calories": 1300.0,
        }
        device = self.today_device(health)
        # Projected Ruhe plus Alltag and Sport so far.
        self.assertEqual(
            (1800, 2200, END.isoformat()),
            (device["resting_kcal"], device["total_kcal"], device["resting_from"]),
        )
        # A 60-min 700 kcal run nets the projected rate, not today's partial 900
        # (Gitar): 700 - 1,800 / 1,440 x 60 = 625 sport, 1,000 - 625 = 375 everyday.
        health[today]["active_kcal"] = 1000.0
        run = {
            "started_at_local": f"{today}T06:00:00",
            "energy_kcal": 700.0,
            "record_ids": GARMIN_IDS,
            "elapsed_duration_s": 3600,
        }
        split = self.today_device(health, [run])["split"]
        self.assertEqual(
            (1800, 375, 625),
            (split["resting_kcal"], split["neat_kcal"], split["sport_kcal"]),
        )
        health[today]["active_kcal"] = 400.0
        # Yesterday missing: the newest earlier day within 7 days.
        del health[END.isoformat()]
        health[(END - timedelta(days=1)).isoformat()]["resting_kcal"] = 1700.0
        device = self.today_device(health)
        self.assertEqual(
            (1700, 2100, (END - timedelta(days=1)).isoformat()),
            (device["resting_kcal"], device["total_kcal"], device["resting_from"]),
        )
        # No active kcal yet counts 0 so far; a negative one is invalid and keeps
        # the total unknown (#3494, Gitar).
        for active, total in ((None, 1700), (-5.0, None)):
            health[today] = {"resting_kcal": 900.0, "active_kcal": active}
            self.assertEqual(total, self.today_device(health)["total_kcal"])
        health[today] = {"resting_kcal": 900.0, "active_kcal": 400.0}
        # Nothing in the 7 days before today: no value, and the reason.
        quiet = {today: health[today]}
        quiet[(END - timedelta(days=7)).isoformat()] = {"resting_kcal": 1700.0}
        device = self.today_device(quiet)
        self.assertEqual(
            (None, None, "no_recent_resting"),
            (device["resting_kcal"], device["total_kcal"], device["resting_missing"]),
        )
        self.assertNotIn("resting_from", device)

    def test_a_past_day_keeps_its_own_garmin_ruhe(self) -> None:
        health = garmin()
        health[(END - timedelta(days=3)).isoformat()] = {
            "resting_kcal": 1650.0,
            "calories": 2800.0,
        }
        past = row(
            energy_view(
                inputs(),
                [window(END, 2700.0, 1850.0)],
                health,
                day=END - timedelta(days=2),
            ),
            "device",
        )
        self.assertEqual((1745, 3120), (past["resting_kcal"], past["total_kcal"]))
        self.assertNotIn("resting_from", past)

    def test_the_selected_day_is_an_iso_date_up_to_today(self) -> None:
        today = date(2026, 11, 17)
        self.assertEqual(today, selected_day("2026-11-17", today))
        self.assertIsNone(selected_day(None, today))
        for text in ("2026-11-18", "17.11.2026"):
            with self.subTest(text=text), self.assertRaises(AppError) as error:
                selected_day(text, today)
            self.assertEqual(400, error.exception.status)

    def test_the_own_row_shows_the_day_with_its_28_day_resting_rate(self) -> None:
        # #3531, athlete 2026-11-18: the own row takes the day's everyday, sport
        # and digestion exactly as the formula rows and its resting rate from the
        # 28-day window; the 28-day row with every part's mean is own_window.
        rest_day = END - timedelta(days=1)
        quiet = DayInputs(weight_kg=80.0, activity_kcal=250.0, neat_kcal=250.0,
                          sport_kcal=0.0, protein_g=250.0, entries=3)  # fmt: skip
        for day, sport, total in ((END, 350, 2700), (rest_day, 0, 2350)):
            with self.subTest(day=day):
                view = self.view(day, {rest_day: quiet})
                own, formula = row(view, "own"), row(view, "katch_mcardle")
                parts = ("neat_kcal", "sport_kcal", "tef_kcal")
                self.assertEqual(
                    [formula["split"][part] for part in parts],
                    [own["split"][part] for part in parts],
                )
                self.assertEqual(
                    (1850, 150, sport, total, 150, 28),
                    (
                        own["resting_kcal"],
                        own["resting_band_kcal"],
                        own["split"]["sport_kcal"],
                        own["total_kcal"],
                        own["total_band_kcal"],
                        own["window_days"],
                    ),
                )
                window_row = view["own_window"]
                self.assertEqual(
                    (2700, 1850, 28),
                    (
                        window_row["total_kcal"],
                        window_row["resting_kcal"],
                        window_row["window_days"],
                    ),
                )
        # A day without its parts (no meals, no activity) has no own day total.
        bare = row(self.view(END - timedelta(days=5)), "own")
        self.assertEqual((1850, None), (bare["resting_kcal"], bare["total_kcal"]))


# One synthetic day walks the table from morning to evening through the API's
# read path (#3541): twelve settled weeks (2,600 kcal eaten, 8,000 steps, a flat
# 75 kg), then today at 06:30, after the reported strength session (45 minutes,
# 19 moving, Garmin 278 kcal) with Garmin's summary still behind, and evening.
SCENARIO_TODAY = date(2026, 11, 18)
SCENARIO_END = SCENARIO_TODAY - timedelta(days=1)
FORMULAS = ("katch_mcardle", "cunningham")
DAY_PARTS = ("neat_kcal", "sport_kcal", "tef_kcal")
STRENGTH = {
    "started_at_local": f"{SCENARIO_TODAY}T17:00:00",
    "sport": "strength",
    "moving_duration_s": 19 * 60.0,
    "elapsed_duration_s": 45 * 60.0,
    "energy_kcal": 278.0,
    "steps": 1500.0,
    "record_ids": {ProviderId.GARMIN.value: "g-strength"},
}


def day_scenario(
    today: dict[str, Any] | None, sessions: list[dict[str, Any]], meals: int
) -> dict[str, Any]:
    start = history_start(SCENARIO_END)
    days = [start + timedelta(days=n) for n in range((SCENARIO_END - start).days + 1)]
    meal = {"kcal": 900.0, "protein_g": 50.0, "carbs_g": 100.0, "fat_g": 30.0}
    health = {
        d.isoformat(): {"steps": 8000.0, "floors": 5.0, "resting_kcal": 1700.0,
                        "active_kcal": 450.0, "calories": 2150.0}
        for d in days
    }  # fmt: skip
    diary = [{**meal, "kcal": 1300.0, "meal_date": d.isoformat()} for d in days] * 2
    diary += [{**meal, "meal_date": SCENARIO_TODAY.isoformat()}] * meals
    if today is not None:
        health[SCENARIO_TODAY.isoformat()] = today

    def within(items: list[dict[str, Any]], key: str, s: date, e: date) -> list:
        return [i for i in items if s <= date.fromisoformat(i[key][:10]) <= e]

    return read_energy_view(
        None,
        EnergySources(
            diary=lambda _db, s, e: within(diary, "meal_date", s, e),
            daily_health=lambda _db, _s, _e: health,
            weights=lambda _db, s, e: {d: 75.0 for d in days[::2] if s <= d <= e},
            sessions=lambda _db, s, e: within(sessions, "started_at_local", s, e),
            streams=lambda _db, _key: {},
            resting=lambda _db, _day: (1700.0, "katch_mcardle"),
            body_fat=lambda _db, _s, _e: {SCENARIO_END - timedelta(days=10): 15.0},
            profile=lambda: {"height_cm": "180"},
        ),
        SCENARIO_END,
        SCENARIO_TODAY,
    )


class EnergyDayScenarioTests(unittest.TestCase):
    """Each phase fails on a revert of #3494, #3530 or #3531 (#3541)."""

    morning: dict[str, Any]
    after: dict[str, Any]
    evening: dict[str, Any]
    phases: dict[str, dict[str, Any]]

    @classmethod
    def setUpClass(cls) -> None:
        cls.morning = day_scenario({"resting_kcal": 300.0}, [], 0)
        cls.after = day_scenario(
            {"steps": 500.0, "floors": 0.0, "resting_kcal": 1200.0,
             "active_kcal": 120.0},
            [STRENGTH], 1,
        )  # fmt: skip
        cls.evening = day_scenario(
            {"steps": 9500.0, "floors": 6.0, "resting_kcal": 1650.0,
             "active_kcal": 640.0},
            [STRENGTH], 3,
        )  # fmt: skip
        cls.phases = {
            "morning": cls.morning,
            "after": cls.after,
            "evening": cls.evening,
        }

    def test_every_row_has_a_total_so_far(self) -> None:
        # #3494: no phase of today leaves Tag gesamt empty.
        for phase, view in self.phases.items():
            for item in view["rows"]:
                with self.subTest(phase=phase, source=item["source"]):
                    self.assertTrue(view["day_partial"])
                    self.assertIsNotNone(item["total_kcal"])
        # Early morning a formula row's total is its rest alone; without a
        # session sport is 0, not open.
        for source in FORMULAS:
            item = row(self.morning, source)
            with self.subTest(phase="morning", source=source):
                self.assertEqual(item["resting_kcal"], item["total_kcal"])
                self.assertEqual(["neat", "tef"], sorted(item["pending_parts"]))
                self.assertEqual(0, item["split"]["sport_kcal"])

    def test_the_strength_session_and_a_lagging_summary(self) -> None:
        # #3530: the session is priced over its 45 minutes, near Garmin's net
        # session kcal; everyday waits for the day summary and counts 0.
        for phase in ("after", "evening"):
            garmin = row(self.phases[phase], "device")["split"]["sport_kcal"]
            for source in FORMULAS:
                sport = row(self.phases[phase], source)["split"]["sport_kcal"]
                with self.subTest(phase=phase, source=source):
                    self.assertTrue(0.5 <= sport / garmin <= 2.0, (sport, garmin))
        for source in (*FORMULAS, "device"):
            item, later = row(self.after, source), row(self.evening, source)
            with self.subTest(source=source):
                self.assertIn("neat", item["pending_parts"])
                counted = sum(item["split"][p] or 0 for p in DAY_PARTS)
                self.assertEqual(item["resting_kcal"] + counted, item["total_kcal"])
                self.assertNotIn("neat", later["pending_parts"])
                self.assertGreater(later["split"]["neat_kcal"], 0)

    def test_the_own_row_is_the_day_not_the_28_day_mean(self) -> None:
        # #3531: its day parts are the formula rows', its rest the window's.
        for phase, view in self.phases.items():
            own, katch = row(view, "own"), row(view, "katch_mcardle")
            window = view["own_window"]
            with self.subTest(phase=phase):
                for part in DAY_PARTS:
                    self.assertEqual(katch["split"][part], own["split"][part])
                self.assertEqual(window["resting_kcal"], own["resting_kcal"])
                self.assertEqual(
                    own["total_kcal"],
                    own["resting_kcal"] + katch["total_kcal"] - katch["resting_kcal"],
                )
                self.assertNotEqual(window["total_kcal"], own["total_kcal"])


if __name__ == "__main__":
    unittest.main()

"""Resting and daily expenditure per source and their fit (#3099, #3187)."""

from __future__ import annotations

import math
import statistics
import unittest
from dataclasses import replace
from datetime import date, timedelta
from typing import Any, cast

from backend.canonical.vocabulary import ProviderId
from backend.errors import AppError
from backend.nutrition.energy_balance import (
    DayInputs,
    EnergyInputs,
    EnergyWindow,
    Estimate,
    StoredEnergy,
    energy_windows,
    history_start,
)
from backend.nutrition.energy_expected import (
    expected_total_kcal,
    planned_inputs,
    planned_net_kcal,
    usual_kcal_per_hour,
)
from backend.nutrition.energy_inputs import (
    EnergySources,
    SessionCost,
    read_energy_inputs,
)
from backend.nutrition.energy_view import (
    energy_view,
    own_day_totals,
    own_history,
    read_energy_view,
    read_own_day_totals,
    selected_day,
)
from backend.performance.activity_energy import TEF_ERROR_KCAL
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
        # Its M11 error (#3552), as 10 % of the activity.
        activity_error_kcal=60.0,
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


def learned_kcal(item: dict[str, Any]) -> int:
    """A formula row's learned everyday part in its total, 0 without (#3723)."""
    return int((item["split"] or {}).get("learned_kcal") or 0)


class EnergyViewTests(unittest.TestCase):
    def test_rows_price_rest_from_lean_mass_with_a_band(self) -> None:
        # 80 kg at 15 % body fat: 68 kg lean mass. Katch-McArdle 370 + 21.6 x 68
        # = 1,838.8; band hypot(10 % = 183.88, 1.96 x 21.6 x 80 x 3.5 % = 118.54)
        # = 218.78. Cunningham 500 + 22 x 68 = 1,996; band hypot(199.6, 120.74)
        # = 233.27. The day adds the raw own activity 600 and TEF 250, its band
        # the activity's M11 error 60 and digestion's 80 (#3552):
        # hypot(218.78, 60, 80) = 240.55, hypot(233.27, 60, 80) = 253.80.
        # #3723: the provisional learned everyday (Katch-McArdle +8, Cunningham
        # -106 counted 0) adds to the total, its band 255.94 to the band:
        # hypot(240.55, 255.94) = 351.2; Cunningham's band, 254 before (its
        # unrounded inputs), gives hypot(254, 256) = 360.6.
        view = energy_view(inputs(), [window(END, 2700.0, 1850.0)], garmin())
        km, cunningham = row(view, "katch_mcardle"), row(view, "cunningham")
        self.assertEqual(
            (1839, 219, 2689 + 8, 351),
            (
                km["resting_kcal"],
                km["resting_band_kcal"],
                km["total_kcal"],
                km["total_band_kcal"],
            ),
        )
        self.assertEqual(
            (1996, 233, 2846, 361),
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
        # #3550: the residual 1,850 exceeds Katch-McArdle's 1,839, so Ruhe is
        # that reference with its band and the 11 kcal above it are "übriger
        # Alltag" in the split; the day total stays.
        own = row(view, "own")
        self.assertEqual(
            (1839, 219, 2700, 150),
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

    def test_each_formula_learns_its_everyday_term_and_shows_it_once_checked(
        self,
    ) -> None:
        # #3577: the window's own TDEE 2,700 less its raw activity 600 and
        # digestion 250 is 1,850. Katch-McArdle lacks 1,850 - 1,838.8 = 11.2,
        # Cunningham overshoots by 146. One window, SD 150 / 1.96 = 76.53,
        # overlap-inflated precision 4.2685e-5, prior 1.6e-5: variance 17,040,
        # so 8.15 for Katch-McArdle, -106.2 for Cunningham (a reduction since
        # #3689), band 1.96 x 130.5 = 256; its posterior SD 130.5 <= 150
        # qualifies it with one window.
        plain = energy_view(inputs(), [window(END, 2700.0, 1850.0)], garmin())
        self.assertEqual(
            {
                "katch_mcardle": {"kcal": 8, "raw_kcal": 8, "band_kcal": 256, "windows": 1,
                                  "independent": 1, "qualified": True},
                "cunningham": {"kcal": -106, "raw_kcal": -106, "band_kcal": 256, "windows": 1,
                               "independent": 1, "qualified": True},
                "checked": None,
            },
            plain["learned_everyday"],
        )  # fmt: skip

        # #3723: before the athlete's check each row counts the provisional
        # estimate floored at 0: Katch-McArdle 2,689 + 8, its band widened as
        # below; Cunningham's -106 counts 0, so its 2,846 stays.
        def learned(view: dict[str, Any], source: str) -> tuple[Any, ...]:
            shown = row(view, source)
            split = shown["split"]
            return (
                shown["total_kcal"],
                shown["total_band_kcal"],
                split.get("learned_kcal"),
                split.get("learned_provisional"),
            )

        self.assertEqual((2697, 351, 8, True), learned(plain, "katch_mcardle"))
        total, _, kcal, provisional = learned(plain, "cunningham")
        self.assertEqual((2846, 0, True), (total, kcal, provisional))
        checked = energy_view(
            inputs(), [window(END, 2700.0, 1850.0)], garmin(),
            goal={"learned_everyday_checked": "2026-11-19"},
        )  # fmt: skip
        # Checked and qualified it counts with its sign, no longer provisional.
        # The total's band adds the learned term's: hypot(240.55, 255.94) = 351.2.
        self.assertEqual((2697, 351, 8, False), learned(checked, "katch_mcardle"))
        # #3689: an overshooting formula is reduced by its negative term.
        total, _, kcal, provisional = learned(checked, "cunningham")
        self.assertEqual((2846 - 106, -106, False), (total, kcal, provisional))
        self.assertEqual("2026-11-19", checked["learned_everyday"]["checked"])
        # #3689: a wide fit from too few independent windows is not applied with
        # its sign, though the athlete's check stays stored and takes effect once
        # it qualifies; until then it counts provisionally (#3723).
        wide = replace(window(END, 2700.0, 1850.0), derived_rmr=Estimate(1850.0, 600.0))
        unready = energy_view(
            inputs(), [wide], garmin(), goal={"learned_everyday_checked": "2026-11-19"}
        )
        self.assertEqual(
            (False, "2026-11-19"),
            (unready["learned_everyday"]["katch_mcardle"]["qualified"],
             unready["learned_everyday"]["checked"]),
        )  # fmt: skip
        wide_kcal = unready["learned_everyday"]["katch_mcardle"]["kcal"]
        self.assertGreater(wide_kcal, 0)
        total, _, kcal, provisional = learned(unready, "katch_mcardle")
        self.assertEqual(
            (2689 + wide_kcal, wide_kcal, True), (total, kcal, provisional)
        )
        # The comparison with the weight stays on the raw rows.
        for source in ("katch_mcardle", "cunningham"):
            self.assertEqual(
                row(plain, source)["mean_error_kcal"],
                row(checked, source)["mean_error_kcal"],
            )
        self.assertEqual(plain["fit_scale"], checked["fit_scale"])
        # #3723: the source card keeps each raw total and adds its 'mit Alltag'
        # total, check or not: the qualified term with its sign, else the
        # provisional estimate floored at 0; none on the device row. Two
        # logged days give the period its formula totals.
        logged = DayInputs(
            weight_kg=80.0, intake_kcal=2500.0, entries=3, activity_kcal=500.0
        )
        period = replace(
            inputs(),
            days={**inputs().days, END: logged, END - timedelta(days=30): logged},
        )

        def scale(fitted: EnergyWindow) -> tuple[dict[str, Any], dict[str, Any]]:
            view = energy_view(period, [fitted], garmin())
            sources = {
                s["source"]: (
                    s["total_kcal"],
                    s.get("learned_total_kcal"),
                    s.get("learned_qualified"),
                )
                for s in view["fit_scale"]["28d"]["sources"]
            }
            return view, sources

        qualified, raw = scale(window(END, 2700.0, 1850.0))
        # Katch-McArdle 2,339 + 8, Cunningham 2,496 - 106 (the terms above).
        self.assertEqual(
            {
                "katch_mcardle": (2339, 2347, True),
                "cunningham": (2496, 2390, True),
                "device": (3120, None, None),
            },
            raw,
        )
        unqualified, provisional = scale(wide)
        terms = unqualified["learned_everyday"]
        self.assertGreater(terms["katch_mcardle"]["kcal"], 0)
        self.assertLess(terms["cunningham"]["kcal"], 0)
        self.assertEqual(
            {
                "katch_mcardle": (2339, 2339 + terms["katch_mcardle"]["kcal"], False),
                # A negative provisional estimate counts 0.
                "cunningham": (2496, 2496, False),
                "device": (3120, None, None),
            },
            provisional,
        )
        # The ranking stays on the raw rows.
        self.assertEqual(
            (qualified["best"], qualified["outlined"]),
            (unqualified["best"], unqualified["outlined"]),
        )
        # Without an estimate no row carries a 'mit Alltag' total.
        empty, none = scale(window(END, None, None))
        self.assertIsNone(empty["learned_everyday"]["katch_mcardle"])
        self.assertEqual({None}, {total for _, total, _ in none.values()})
        # Nor does the diary's: it is not learned yet and counts 0 (#3723).
        for source in ("katch_mcardle", "cunningham"):
            self.assertEqual((None, None), learned(empty, source)[2:])

    def test_unmeasured_everyday_energy_is_named_never_resting(self) -> None:
        # #3550 (athlete decision 2026-11-18): 300 kcal/day of everyday energy
        # the steps do not see lifts the residual to 2,139 over Katch-McArdle's
        # 1,838.8 (band 218.78). The own row shows Ruhe at that reference and
        # the 300 kcal above it as "übriger Alltag" with the residual's
        # band; its day total (2,139 + 600 + 250) and every other row stay.
        lifted = energy_view(inputs(), [window(END, 3000.0, 2139.0)], garmin())
        plain = energy_view(inputs(), [window(END, 2700.0, 1839.0)], garmin())
        own = row(lifted, "own")
        self.assertEqual(
            (1839, 219, 2989, 150),
            (
                own["resting_kcal"],
                own["resting_band_kcal"],
                own["total_kcal"],
                own["total_band_kcal"],
            ),
        )
        self.assertEqual(
            (1839, "katch_mcardle", 300, 150),
            (
                own["split"]["resting_kcal"],
                own["split"]["resting_source"],
                own["split"]["unmeasured_kcal"],
                own["split"]["unmeasured_band_kcal"],
            ),
        )

        # Their values stay; only their distance to the own total moves, and
        # the learned everyday the formula rows learn from it (#3723).
        def raw(view: dict[str, Any], source: str) -> list[Any]:
            item = row(view, source)
            split = {
                key: value
                for key, value in item["split"].items()
                if not key.startswith("learned_")
            }
            total = item["total_kcal"] - learned_kcal(item)
            return [item["resting_kcal"], item["resting_band_kcal"], total, split]

        for source in ("katch_mcardle", "cunningham", "device"):
            self.assertEqual(raw(plain, source), raw(lifted, source))
        self.assertGreater(
            learned_kcal(row(lifted, "katch_mcardle")),
            learned_kcal(row(plain, "katch_mcardle")),
        )
        # At or below the reference nothing is split.
        self.assertNotIn("unmeasured_kcal", row(plain, "own")["split"])
        self.assertEqual(1839, row(plain, "own")["resting_kcal"])

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
        # so only TEF is alike; the own day row takes the formula rows' parts,
        # not their provisional learned everyday (#3723).
        view = energy_view(inputs(), [window(END, 2700.0, 1850.0)], garmin())
        self.assertEqual(
            {
                "resting_kcal": 1839,
                "neat_kcal": 250,
                "sport_kcal": 350,
                "tef_kcal": 250,
                "shared": {"neat": True, "sport": True, "tef": True},
                "learned_kcal": 8,
                "learned_band_kcal": 256,
                "learned_provisional": True,
            },
            row(view, "katch_mcardle")["split"],
        )
        self.assertEqual(
            {
                "resting_kcal": 1839,
                "neat_kcal": 262,
                "sport_kcal": 368,
                "tef_kcal": 250,
                "shared": {"neat": False, "sport": False, "tef": True},
                "resting_source": "katch_mcardle",
                "unmeasured_kcal": 11,
                "unmeasured_band_kcal": 150,
            },
            view["own_window"]["split"],
        )
        self.assertEqual(
            {
                "resting_kcal": 1839,
                "neat_kcal": 250,
                "sport_kcal": 350,
                "tef_kcal": 250,
                "shared": {"neat": True, "sport": True, "tef": True},
                "resting_source": "katch_mcardle",
                "unmeasured_kcal": 11,
                "unmeasured_band_kcal": 150,
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
        day3 = read_energy_view(None, sources(), DAY3)
        self.assertIsNone(row(day3, "device")["resting_kcal"])
        # #3530: the day's strength session names the RPE and MET that priced
        # it; without a rating MET 3.5: 2.5 x 1,680 x 3,600 / 86,400 = 175.
        self.assertEqual([], view["strength"])
        self.assertEqual([{"rpe": None, "met": 3.5, "kcal": 175}], day3["strength"])
        # 70 kg at 12 %: 61.6 kg lean mass, Katch-McArdle 370 + 21.6 x 61.6.
        self.assertEqual(1701, row(view, "katch_mcardle")["resting_kcal"])
        self.assertEqual(DAY1.isoformat(), view["body"]["date"])

    def test_the_read_brings_this_mornings_weigh_in_for_the_water_hint(self) -> None:
        # #4047: the history ends yesterday, so today's weigh-in is read beside it,
        # for the hint only; without a today there is no hint.
        asked: list[tuple[date, date]] = []
        base = sources()

        def weights(db: object, first: date, last: date) -> dict[date, float]:
            asked.append((first, last))
            return base.weights(db, first, last)

        today = DAY3 + timedelta(days=1)
        view = read_energy_view(
            None, replace(base, weights=weights), DAY3, DAY3, today=today
        )
        self.assertIn((today, today), asked)
        self.assertIsNone(view["water_hint"])
        self.assertIsNone(read_energy_view(None, sources(), DAY3)["water_hint"])

    def test_an_old_diary_day_leaves_the_twelve_weeks_unchanged(self) -> None:
        # #3617 item 6: opening a day before the 12-week span reads the span as
        # it is and adds that day with the span's step length, flex-HR line and
        # resting reference; the windows and the fit do not move.
        old = history_start(DAY3) - timedelta(days=20)
        start = history_start(DAY3)
        # A walk on the span's last day and one on the old day with another
        # step length: only the span's walk may set the step length.
        walks: list[dict[str, Any]] = [
            {"id": "w1", "sport": "walk", "started_at_local": f"{DAY3}T09:00:00",
             "moving_duration_s": 3600, "distance_m": 6000, "steps": 8000},
            {"id": "w0", "sport": "walk", "started_at_local": f"{old}T09:00:00",
             "moving_duration_s": 3600, "distance_m": 6000, "steps": 7000},
        ]  # fmt: skip

        def dated(_db: Any, s: date, e: date) -> list[dict[str, Any]]:
            return [
                w
                for w in walks
                if s <= date.fromisoformat(w["started_at_local"][:10]) <= e
            ]

        walking = replace(sources(), sessions=dated)
        span = read_energy_view(None, walking, DAY3)
        opened = read_energy_view(None, walking, DAY3, old)
        self.assertEqual(span["windows"], opened["windows"])
        self.assertEqual(span["fit"], opened["fit"])
        self.assertEqual(old.isoformat(), opened["day"])
        alone, _ = read_energy_inputs(None, walking, start, DAY3)
        added, costs = read_energy_inputs(None, walking, start, DAY3, older_day=old)
        # The span's 6,000 m over 8,000 steps: 0.75 m.
        self.assertEqual(0.75, alone.step_m)
        self.assertEqual((alone.rmr_kcal, alone.step_m), (added.rmr_kcal, added.step_m))
        # The old day comes with the 27 days before it its weight trend needs.
        before = {old - timedelta(days=n) for n in range(28)}
        self.assertEqual(set(alone.days) | before, set(added.days))
        # The old day's walk is priced too, with the span's basis.
        self.assertIn(old, {cost.day for cost in costs})
        # Gitar on #3671: an entered body fat before the span's lookback but
        # within the old day's 90 days still gives that day its body; no
        # window reaches it, so the windows stay as they are.
        entered = old - timedelta(days=10)

        def fats(_db: Any, s: date, e: date) -> dict[date, tuple[float, Any]]:
            return {entered: (19.0, None)} if s <= entered <= e else {}

        weighed = {DAY1: 70.0, old - timedelta(days=5): 72.0}
        with_fat = replace(
            walking,
            device_body_fat=fats,
            weights=lambda _db, s, e: {d: w for d, w in weighed.items() if s <= d <= e},
        )
        opened = read_energy_view(None, with_fat, DAY3, old)
        body = opened["body"]
        assert body is not None
        self.assertEqual(entered.isoformat(), body["date"])
        whole = read_energy_view(None, with_fat, DAY3)
        self.assertEqual(
            (whole["windows"], whole["fit"]), (opened["windows"], opened["fit"])
        )


LOSE = {"energy_goal": "lose", "energy_goal_kcal": "500"}


def with_intake(source: EnergyInputs, day: date, **parts: Any) -> EnergyInputs:
    return replace(
        source, days={**source.days, day: DayInputs(weight_kg=80.0, **parts)}
    )


class EnergyGoalTests(unittest.TestCase):
    """'Offene Kalorien' from the outlined source's day total (#3547)."""

    def test_the_goal_takes_the_best_sources_total_and_subtracts_what_was_eaten(
        self,
    ) -> None:
        source = with_intake(
            inputs(),
            END,
            intake_kcal=1200.4,
            activity_kcal=600.0,
            neat_kcal=250.0,
            sport_kcal=350.0,
            protein_g=250.0,
            entries=3,
        )
        cases = (("lose", "500", -500), ("build", "300", 300), ("hold", "", 0))
        for goal, kcal, delta in cases:
            with self.subTest(goal=goal):
                profile = {"energy_goal": goal, "energy_goal_kcal": kcal}
                view = energy_view(source, windows(), garmin(), goal=profile)
                total = row(view, "katch_mcardle")["total_kcal"]
                self.assertEqual(
                    {
                        "goal": goal,
                        "goal_kcal": abs(delta),
                        "source": "katch_mcardle",
                        "total_kcal": total,
                        "target_kcal": total + delta,
                        "eaten_kcal": 1200,
                        "remaining_kcal": total + delta - 1200,
                    },
                    view["goal"],
                )

    def test_without_a_goal_there_is_none(self) -> None:
        for profile in (None, {}, {"energy_goal": ""}):
            with self.subTest(profile=profile):
                self.assertIsNone(
                    energy_view(inputs(), windows(), garmin(), goal=profile)["goal"]
                )

    def test_the_basis_follows_the_outlined_row_best_else_nearest(self) -> None:
        # Within the band nothing is best; Garmin at 2,700 is then nearest.
        view = energy_view(inputs(), windows(), garmin(total=2700.0), goal=LOSE)
        self.assertIsNone(view["best"])
        self.assertEqual("device", view["goal"]["source"])
        # #3613: the read names that one source for the outline and Verlauf.
        self.assertEqual("device", view["outlined"])
        self.assertEqual(
            row(view, "device")["total_kcal"] - 500, view["goal"]["target_kcal"]
        )
        best = energy_view(inputs(), windows(), garmin(), goal=LOSE)
        self.assertEqual("katch_mcardle", best["goal"]["source"])
        self.assertEqual("katch_mcardle", best["outlined"])
        # Without any fit the goal has no basis yet, but is still the goal.
        none = energy_view(inputs({}), [window(END, None)], {}, goal=LOSE)["goal"]
        self.assertEqual(
            ("lose", 500, None, None),
            (none["goal"], none["goal_kcal"], none["source"], none["remaining_kcal"]),
        )

    def test_today_grows_from_the_morning_to_the_evening(self) -> None:
        today = END + timedelta(days=1)
        morning = with_intake(inputs(), today, intake_kcal=400.0)
        evening = with_intake(
            inputs(),
            today,
            intake_kcal=400.0,
            activity_kcal=600.0,
            neat_kcal=250.0,
            sport_kcal=350.0,
        )
        early, late = (
            energy_view(source, windows(), garmin(), day=today, goal=LOSE)
            for source in (morning, evening)
        )
        self.assertTrue(early["day_partial"])
        self.assertLess(early["goal"]["total_kcal"], late["goal"]["total_kcal"])
        for view in (early, late):
            goal = view["goal"]
            self.assertEqual(
                row(view, goal["source"])["total_kcal"], goal["total_kcal"]
            )
            self.assertEqual(goal["total_kcal"] - 500 - 400, goal["remaining_kcal"])

    def test_the_reader_takes_the_goal_from_the_profile(self) -> None:
        goal = read_energy_view(
            None, replace(sources(), profile=lambda: LOSE), DAY3, DAY1
        )["goal"]
        self.assertEqual(("lose", 500), (goal["goal"], goal["goal_kcal"]))


# #3555: a share of 0.80, 28 % of it from the measurements.
MEASURED = StoredEnergy(
    -565.0, 120.0, "measured", 28, 91.2, 89.2, -1.6, 15.5, 14.1, "fat_share",
    0.80, 0.28,
)  # fmt: skip


class DeficitCardTests(unittest.TestCase):
    """'Dein Defizit, letzte 28 Tage' in Verlauf (#3548)."""

    def view(
        self, store: StoredEnergy | None, goal: dict[str, str] | None = None
    ) -> Any:
        latest = replace(window(END, 3800.0), store=store)
        return energy_view(inputs(), [*windows()[:-1], latest], garmin(), goal=goal)[
            "deficit"
        ]

    def test_without_logged_meals_the_card_keeps_the_store_and_drops_the_expenditure(
        self,
    ) -> None:
        # #3603 ("Nur Waage"): weigh-ins alone give the store; the expenditure
        # needs logged meals and is null, and so is the own row.
        latest = replace(window(END, None), store=MEASURED, insufficient="intake_days")
        view = energy_view(inputs(), [*windows()[:-1], latest], garmin())
        card = view["deficit"]
        # Its band takes the weight slope's dof alone (#3614): 20 weigh-ins,
        # 18 dof, t = 2.101; 2.101 x 120 = 252, not the 24 days' t.
        self.assertEqual(
            (565, 252, None),
            (card["deficit_kcal"], card["band_kcal"], card["total_kcal"]),
        )
        self.assertEqual("intake_days", card["expenditure_missing"])
        # Without an estimate the own rows name no fat share (Gitar, #3620).
        self.assertEqual(
            (None, None, None),
            (
                row(view, "own")["total_kcal"],
                row(view, "own")["fat_share"],
                view["own_window"]["fat_share"],
            ),
        )
        # Maintenance: a flat weight is near 0 and within "Halten".
        flat = replace(latest, store=MEASURED._replace(kcal_per_day=-40.0))
        hold = energy_view(
            inputs(), [*windows()[:-1], flat], garmin(), goal={"energy_goal": "hold"}
        )["deficit"]
        self.assertEqual((40, "in"), (hold["deficit_kcal"], hold["goal"]["status"]))

    def test_the_deficit_follows_the_selected_period(self) -> None:
        # #3704: a steady loss of 0.1 kg a day prices at 0.1 x 7,154 kcal/kg (the
        # 70 % rule of thumb) = 715 kcal a day over every period. 4 weeks price
        # it over their weigh-ins (27 days, the line) and show the means of
        # their first and last 7: 3 days inside each end, a change of 21 x 0.1;
        # 3 and 6 months take the mean days of their first and last 14 daily
        # weigh-ins, 6.5 days inside each end (#3739): 90 - 13 = 77 and
        # 181 - 13 = 168 days, the half days rounded up.
        days = {
            END - timedelta(days=n): DayInputs(weight_kg=80.0 + 0.1 * n)
            for n in range(190)
        }
        source = replace(inputs(body_fat={}), days=days)
        # 6 Monate come with their own inputs, asked for by Verlauf (#3711).
        view = energy_view(
            source, energy_windows(source, END), garmin(), fit_inputs=source
        )
        for key, span, change, first, last in (
            ("28d", 27, -2.1, 24, 3),
            ("91d", 77, -7.7, 83, 6),
            ("182d", 168, -16.8, 174, 6),
        ):
            with self.subTest(period=key):
                card = view["fit_scale"][key]["deficit"]
                self.assertEqual(
                    (715, span, change),
                    (card["deficit_kcal"], card["span_days"], card["weight_change_kg"]),
                )
                self.assertEqual(
                    (
                        (END - timedelta(days=first)).isoformat(),
                        (END - timedelta(days=last)).isoformat(),
                    ),
                    (card["weight_date_start"], card["weight_date_end"]),
                )
                self.assertEqual(
                    [7, 7] if key == "28d" else [14, 14], card["edge_weighins"]
                )
        # The latest 28 days stay the read's own deficit.
        self.assertEqual(view["deficit"], view["fit_scale"]["28d"]["deficit"])

    def test_the_boxes_take_the_fitted_weight_change(self) -> None:
        # #3618 item 4: 91.24 → 89.16 kg is -2.08 on the fitted line; its
        # rounded endpoints would give -2.0, while the fat box takes 80 % of
        # -2.08. The weight box shows the fitted change, so the boxes add up.
        card = self.view(
            MEASURED._replace(
                weight_start_kg=91.24, weight_end_kg=89.16, fat_change_kg=-1.664
            )
        )
        self.assertEqual(
            (91.2, 89.2, -2.1, -1.7, 80),
            (
                card["weight_start_kg"],
                card["weight_end_kg"],
                card["weight_change_kg"],
                card["fat_change_kg"],
                card["fat_share_pct"],
            ),
        )
        self.assertAlmostEqual(
            card["fat_change_kg"], card["weight_change_kg"] * 0.80, delta=0.05
        )

    def test_edge_means_take_their_band_t_from_the_edges(self) -> None:
        # #3739 review: an edge store's SD rests on the edges' pooled dof, n1 +
        # n2 - 2, not on the window's: 2 + 2 weigh-ins give t(2) = 4.303, so
        # 4.303 x 120 = 516; 14 + 14 give t(26) = 2.056, 247; one each pools
        # nothing, so z: 1.96 x 120 = 235. The line keeps the window's t(18).
        for edges, band in (((2, 2), 516), ((14, 14), 247), ((1, 1), 235), (None, 252)):
            with self.subTest(edges=edges):
                card = self.view(MEASURED._replace(edge_weighins=edges))
                self.assertEqual(band, card["band_kcal"])

    def test_the_card_shows_the_stores_terms_and_the_real_expenditure(self) -> None:
        # #3598: the readings span the phase (from 07.10.), not the window, and
        # the window ends on its last weigh-in.
        card = self.view(
            MEASURED._replace(
                fat_date_start=date(2026, 10, 7),
                fat_date_end=date(2026, 11, 13),
                weight_last_kg=89.24,
            )
        )
        self.assertEqual(
            {
                "days": 28,
                "deficit_kcal": 565,
                # The window's own t (#3614): 20 weigh-ins and 24 complete
                # days give 18 dof, t = 2.101; 2.101 x 120 = 252.
                "band_kcal": 252,
                "method": "measured",
                "fat_share": {
                    "share_pct": 80,
                    "prior_pct": 70,
                    "prior_source": "fallback",
                    "measured_pct": 28,
                },
                "span_days": 28,
                "weight_start_kg": 91.2,
                "weight_end_kg": 89.2,
                # A store without weigh-in dates has no edge means (#3739).
                "edge_weighins": None,
                "weight_change_kg": -2.0,
                # 80 % at 9,441 and 20 % at 1,816 kcal/kg (#3618 item 7).
                "kcal_per_kg": 7916,
                "fat_pct_start": 15.5,
                "fat_pct_end": 14.1,
                "fat_date_start": "2026-10-07",
                "fat_date_end": "2026-11-13",
                "weight_last_kg": 89.2,
                "weight_date_start": None,
                "weight_date_end": None,
                "fat_change_kg": -1.6,
                "fat_share_pct": 80,
                "stored_kcal": -15820,
                "uncertainty": "fat_share",
                "total_kcal": 3800,
                "expenditure_missing": None,
                "device_total_kcal": 3120,
                "goal": None,
            },
            card,
        )
        # The prior alone has one percentage at most; a surplus is a
        # negative deficit.
        prior = self.view(
            MEASURED._replace(
                kcal_per_day=300.0,
                method="prior",
                fat_pct_start=None,
                fat_share=0.70,
                measured_weight=0.0,
            )
        )
        self.assertEqual(
            (
                -300,
                "prior",
                None,
                {
                    "share_pct": 70,
                    "prior_pct": 70,
                    "prior_source": "fallback",
                    "measured_pct": 0,
                },
            ),
            (
                prior["deficit_kcal"],
                prior["method"],
                prior["fat_pct_start"],
                prior["fat_share"],
            ),
        )
        self.assertIsNone(self.view(None))
        # An unstable window names the weight step as its uncertainty (#3614).
        latest = replace(window(END, 3800.0), store=MEASURED, stable=False)
        unstable = energy_view(inputs(), [*windows()[:-1], latest], garmin())
        self.assertEqual("unstable", unstable["deficit"]["uncertainty"])

    def test_the_goal_status_and_colour_follow_the_distance_to_the_goal(self) -> None:
        # #3606: within 150 kcal of the goal, either way, is in the goal and
        # green ("near"); up to 350 orange ("mid"); beyond red ("far"). Lose and
        # hold count a deficit, build a surplus; hold's goal is 0.
        goals = {"lose": ("500", 500, 1), "hold": ("", 0, 1), "build": ("300", 300, -1)}
        for kind, (text, target, sign) in goals.items():
            for distance, colour in ((0, "near"), (150, "near"), (151, "mid"),
                                     (350, "mid"), (351, "far")):  # fmt: skip
                for side in (1, -1):
                    amount = target + side * distance
                    store = MEASURED._replace(kcal_per_day=-sign * amount)
                    with self.subTest(goal=kind, distance=distance, side=side):
                        card = self.view(
                            store, {"energy_goal": kind, "energy_goal_kcal": text}
                        )
                        status = "in"
                        if distance > 150:
                            status = "over" if side > 0 else "under"
                        self.assertEqual(
                            (status, colour),
                            (card["goal"]["status"], card["goal"]["distance"]),
                        )
        self.assertIsNone(self.view(MEASURED, {"energy_goal": ""})["goal"])


def scale_phase(unlogged_days: int, eaten: float = 2100.0) -> dict[str, Any]:
    """120 days at a true expenditure of 2600 kcal (activity 600): logged at
    2600 first, then ``unlogged_days`` at ``eaten`` without logging, weighed
    daily; the latest view's estimated intake (#3604)."""
    weight, days = 80.0, {}
    for n in range(120):
        day = END - timedelta(days=119 - n)
        logged = n < 120 - unlogged_days
        true = 2600.0 if logged else eaten
        # The prior fat share's 7,153.5 kcal per kg (Hall 2008).
        weight += (true - 2600.0) / 7153.5
        days[day] = DayInputs(
            intake_kcal=true if logged else None,
            protein_g=150.0 if logged else 0.0,
            carbohydrate_g=300.0 if logged else 0.0,
            fat_g=80.0 if logged else 0.0,
            entries=3 if logged else 0,
            weight_kg=weight,
            activity_kcal=600.0,
            activity_error_kcal=60.0,
        )
    # One dated reading at the first window's end gives every window a body
    # within 90 days and keeps the prior fat share; the profile holds none (#3640).
    source = EnergyInputs(days, {END - timedelta(days=77): 20.0}, rmr_kcal=1750.0)
    return energy_view(source, energy_windows(source, END), {})["estimated_intake"]


class EstimatedIntakeTests(unittest.TestCase):
    """#3604 ("Nur Waage", step 2): without logged meals, the intake from the
    calibrated expenditure and the weight change."""

    def test_an_unlogged_phase_recovers_its_known_intake_within_the_band(
        self,
    ) -> None:
        # The half-logged windows across the switch price only their logged
        # span (#3667): too short for a basis, they stay out, and the newest
        # calibration is the last fully logged window, 5 weeks back (it was
        # 8 while their mixed phases failed the neighbour check, #3614).
        estimate = scale_phase(35)
        self.assertEqual(
            (None, "2026-10-10", 35),
            (
                estimate["missing"],
                estimate["calibration_end"],
                estimate["calibration_age_days"],
            ),
        )
        self.assertLessEqual(abs(estimate["kcal"] - 2100), estimate["band_kcal"])
        self.assertLess(estimate["band_kcal"], 500)

    def test_enough_logged_days_never_show_an_estimate(self) -> None:
        self.assertIsNone(scale_phase(0))
        # 23 logged days span 22, enough for the window's own TDEE (#3667).
        self.assertIsNone(scale_phase(5))
        # 14 logged days at the window's start span 13 days, too short to
        # price (#3667): the window shows the estimate instead of a mixed
        # TDEE, the 28 days' true mean intake of 2,350 within its band.
        half = scale_phase(14)
        self.assertLessEqual(abs(half["kcal"] - 2350), half["band_kcal"])

    def test_an_old_calibration_or_a_large_weight_change_gives_its_reason(
        self,
    ) -> None:
        old = scale_phase(70, eaten=2550.0)
        self.assertEqual(("calibration_age", None), (old["missing"], old["kcal"]))
        moved = scale_phase(28, eaten=1600.0)
        self.assertEqual(("weight_change", None), (moved["missing"], moved["kcal"]))


class PartialGarminDayTests(unittest.TestCase):
    """#3597: a Garmin day whose summary ended early leaves multi-day means."""

    def test_a_partial_day_leaves_the_window_means_but_the_shown_day_keeps_it(
        self,
    ) -> None:
        health = garmin()
        # One day ended at 12:18 with half a day's resting and total kcal.
        partial = (END - timedelta(days=3)).isoformat()
        health[partial] = {
            "resting_kcal": 870.0,
            "calories": 1500.0,
            "wellness_end_min": 738.0,
        }
        view = energy_view(inputs(), windows(), health)
        # Every window mean stays the full days' 1,745 / 3,120, so the device's
        # error to the 2,700 TDEE is 420 in each window, as without that day.
        self.assertEqual(420, row(view, "device")["mean_error_kcal"])
        # The shown day itself keeps its own values (and its Stand, #3530).
        day = energy_view(
            inputs(), [window(END, 2700.0)], health, day=END - timedelta(days=3)
        )
        self.assertEqual(870, row(day, "device")["resting_kcal"])
        # "Welche Quelle trifft dein Gewicht?" means only complete days too: the
        # 4-week and 3-month Garmin means stay the full days' 3,120 (#3597).
        for key in ("28d", "91d"):
            with self.subTest(period=key):
                scale = view["fit_scale"][key]
                device = next(s for s in scale["sources"] if s["source"] == "device")
                self.assertEqual(3120, device["total_kcal"])


class DayBandTests(unittest.TestCase):
    """#3552: the formula rows' day band carries each activity part's own error."""

    def day(self, sport: float, error: float) -> dict[str, Any]:
        source = inputs()
        days = dict(source.days)
        days[END] = replace(
            days[END],
            activity_kcal=250.0 + sport,
            sport_kcal=sport,
            activity_error_kcal=error,
        )
        return row(
            energy_view(replace(source, days=days), [window(END, 2700.0)], garmin()),
            "katch_mcardle",
        )

    def test_a_strength_day_is_wider_than_a_ride_of_equal_kcal(self) -> None:
        # Everyday 250 at ±15 % (37.5) with 350 kcal of strength (±50 %) or a
        # ride (±10 %), in quadrature.
        strength = self.day(350.0, math.hypot(37.5, 175.0))
        ride = self.day(350.0, math.hypot(37.5, 35.0))
        self.assertEqual(strength["total_kcal"], ride["total_kcal"])
        self.assertGreater(strength["total_band_kcal"], ride["total_band_kcal"] + 40)
        resting = strength["resting_band_kcal"]
        self.assertEqual(
            round(math.hypot(resting, math.hypot(37.5, 175.0), TEF_ERROR_KCAL)),
            strength["total_band_kcal"],
        )

    def test_a_rest_day_stays_close_to_the_resting_band(self) -> None:
        rest = self.day(0.0, 37.5)
        self.assertLess(rest["total_band_kcal"] - rest["resting_band_kcal"], 25)


class WellnessStandTests(unittest.TestCase):
    """#3530: Garmin's day summary names where its wellness data ends."""

    def view(self, end_min: float | None, sessions=(), day: date = END) -> Any:
        health = garmin()
        if end_min is not None:
            health[day.isoformat()] = {
                **health.get(day.isoformat(), {}),
                "wellness_end_min": end_min,
            }
        return energy_view(
            inputs(), [window(END, 2700.0)], health, list(sessions), day=day
        )

    def test_an_early_end_reads_as_stand_and_a_finished_day_has_none(self) -> None:
        self.assertEqual(
            {"as_of": "12:18", "behind": False}, self.view(738.0)["wellness"]
        )
        self.assertIsNone(self.view(1440.0)["wellness"])
        self.assertIsNone(self.view(None)["wellness"])

    def test_a_session_after_the_stand_is_behind_and_everyday_waits(self) -> None:
        # The day summary ends 12:18; a 43-minute session from 18:00 is not in
        # its steps, so today's everyday is not there yet, in every row.
        today = END + timedelta(days=1)
        session = {"started_at_local": f"{today}T18:00:00", "elapsed_duration_s": 2580,
                   "record_ids": GARMIN_IDS, "energy_kcal": 277.0}  # fmt: skip
        view = self.view(738.0, [session], day=today)
        self.assertEqual({"as_of": "12:18", "behind": True}, view["wellness"])
        for source in ("katch_mcardle", "cunningham", "device"):
            self.assertIn("neat", row(view, source)["pending_parts"])
        # A session that ended before the stand is in the summary.
        early = {**session, "started_at_local": f"{today}T07:00:00"}
        self.assertFalse(self.view(738.0, [early], day=today)["wellness"]["behind"])


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
        # A day 40 days back counts for 3 months, not for 4 weeks.
        health[(END - timedelta(days=40)).isoformat()] = {
            "resting_kcal": 1745.0,
            "calories": 6000.0,
        }
        given = replace(source, days=days)
        # 6 Monate come with their own inputs, asked for by Verlauf (#3711).
        view = energy_view(given, [window(END, 2700.0)], health, fit_inputs=given)
        self.assertEqual(
            ["28d", "91d", "182d", "365d"],
            list(view["fit_scale"]),
        )
        short, long = view["fit_scale"]["28d"], view["fit_scale"]["91d"]
        device = lambda scale: next(
            s for s in scale["sources"] if s["source"] == "device"
        )
        self.assertEqual(
            (28, 28, 4, 1),
            (
                short["days"],
                short["own_days"],
                short["weeks"],
                short["weeks_with_data"],
            ),
        )
        self.assertEqual(
            (91, 91, 13, 2),
            (long["days"], long["own_days"], long["weeks"], long["weeks_with_data"]),
        )
        self.assertEqual(3120, device(short)["total_kcal"])
        # Without an own value the sources keep the period's days: the fixture
        # holds 60, 59 at 3,120 and one at 6,000 = 3,168; no span is named.
        self.assertEqual(3168, device(long)["total_kcal"])
        self.assertEqual((None, None), (long["span_start"], short["span_start"]))
        self.assertEqual(
            ["katch_mcardle", "cunningham", "device"],
            [s["source"] for s in long["sources"]],
        )
        # Too few logged days for the rules: no own value, never a guess.
        self.assertEqual((None, None), (long["own_kcal"], long["own_band_kcal"]))

    def test_twelve_weeks_average_the_sources_over_the_own_value_s_days(self) -> None:
        # #3613: the own value stands on the complete days between the period's
        # first and last weigh-in; every source averages over those days only.
        logged = DayInputs(
            weight_kg=80.0, intake_kcal=2500.0, entries=3, activity_kcal=500.0,
            neat_kcal=250.0, sport_kcal=250.0, protein_g=150.0,
        )  # fmt: skip
        own_days = {END - timedelta(days=n): logged for n in range(0, 40, 2)}
        weighed = {
            END - timedelta(days=n): DayInputs(weight_kg=80.0) for n in range(41)
        }
        base = replace(inputs(), days={**weighed, **own_days})
        health = garmin()
        # A complete day before the first weigh-in and a day without meals.
        before = END - timedelta(days=50)
        unlogged = END - timedelta(days=11)
        extra = {
            before: replace(
                logged, weight_kg=None, intake_kcal=4000.0, activity_kcal=1600.0
            ),
            unlogged: DayInputs(weight_kg=80.0, activity_kcal=3000.0),
        }
        busy = {**health, before.isoformat(): {"resting_kcal": 1745.0, "calories": 9000.0},
                unlogged.isoformat(): {"resting_kcal": 1745.0, "calories": 6000.0}}  # fmt: skip
        plain = energy_view(base, [window(END, 2700.0)], health)["fit_scale"]["91d"]
        mixed = energy_view(
            replace(base, days={**base.days, **extra}), [window(END, 2700.0)], busy
        )["fit_scale"]["91d"]
        self.assertIsNotNone(mixed["own_kcal"])
        # The own value and every source stand on the same days (Gitar, #3662).
        self.assertEqual(
            (plain["own_kcal"], plain["own_band_kcal"], plain["sources"]),
            (mixed["own_kcal"], mixed["own_band_kcal"], mixed["sources"]),
        )
        self.assertEqual(3120, mixed["sources"][-1]["total_kcal"])
        self.assertEqual(
            ((END - timedelta(days=40)).isoformat(), END.isoformat()),
            (mixed["span_start"], mixed["span_end"]),
        )


class SelectedDayTests(unittest.TestCase):
    """#3395: the source rows show the diary's selected day, the own row 28 days."""

    def view(self, day: date, days: dict[date, DayInputs] | None = None, **kw: Any):
        source = inputs()
        if days:
            source = replace(source, days={**source.days, **days})
        return energy_view(
            source, [window(END, 2700.0, 1850.0)], garmin(), day=day, **kw
        )

    def test_today_rests_on_the_twelve_week_base_and_a_past_day_on_its_window(
        self,
    ) -> None:
        # #3687: equal bands of 150, the older window 42 days back weighs half:
        # (1,850 + 0.5 x 1,650) / 1.5 = 1,783.3. Its band: SD 150 / 1.96 x
        # sqrt(1.25) / 1.5 x sqrt(28 / 7) = 114.08, band 223.6.
        older = window(END - timedelta(days=42), 2700.0, 1650.0)
        latest = window(END, 2700.0, 1850.0)
        today = END + timedelta(days=1)
        view = energy_view(inputs(), [older, latest], garmin(), day=today)
        own = row(view, "own")
        self.assertEqual((1783, "12w"), (own["resting_kcal"], own["basis"]))
        self.assertEqual(
            {"kcal": 1783, "band_kcal": 224, "windows": 2, "weight_sum": 1.5,
             "unstable": False},
            view["base"],
        )  # fmt: skip
        # A past day keeps its own window (#3641), so the period balance and
        # the diary agree on it (#3600).
        past = energy_view(inputs(), [older, latest], garmin(), day=END)
        own_past = row(past, "own")
        # Its 1,850 exceeds Katch-McArdle's 1,839, so 11 are unmeasured (#3550).
        self.assertEqual(
            (1839, 11, None),
            (own_past["resting_kcal"], own_past["split"]["unmeasured_kcal"],
             own_past["basis"]),
        )  # fmt: skip
        # Today's period-balance total takes the same base.
        totals = own_day_totals(inputs(), [older, latest], garmin(), [today, END])
        self.assertEqual(own_past["total_kcal"], totals[END])
        full = with_intake(inputs(), today, intake_kcal=2400.0, activity_kcal=600.0,
                           neat_kcal=250.0, sport_kcal=350.0, protein_g=150.0,
                           entries=3)  # fmt: skip
        on_base = own_day_totals(full, [older, latest], garmin(), [today])[today]
        on_latest = own_day_totals(full, [latest], garmin(), [today])[today]
        assert on_base is not None and on_latest is not None
        # One window alone is its own base: 1,850; with the older one 1,783.
        self.assertAlmostEqual(1783 - 1850, on_base - on_latest, delta=1)

    def test_the_period_balance_takes_each_day_s_own_row_total(self) -> None:
        # #3600: the period balance reads the own day totals per day, and each
        # is exactly the diary's own day row for that day; a day without
        # meals has none and stays open.
        rest_day, empty = END - timedelta(days=1), END - timedelta(days=2)
        quiet = DayInputs(weight_kg=80.0, activity_kcal=250.0, neat_kcal=250.0,
                          sport_kcal=0.0, protein_g=250.0, entries=3)  # fmt: skip
        no_meals = replace(quiet, protein_g=0.0, entries=0)
        days = {rest_day: quiet, empty: no_meals}
        source = replace(inputs(), days={**inputs().days, **days})
        totals = own_day_totals(
            source, [window(END, 2700.0, 1850.0)], garmin(), [rest_day, empty, END]
        )
        for day in (rest_day, empty, END):
            with self.subTest(day=day):
                self.assertEqual(
                    row(self.view(day, days), "own")["total_kcal"], totals[day]
                )
        # 1,850 own resting + 250 everyday + 0 sport + 250 digestion.
        self.assertEqual((2350, None), (totals[rest_day], totals[empty]))

    def test_twelve_weeks_follow_each_window_s_resting_rate(self) -> None:
        # #3641: a true expenditure rising from 2,300 to 3,000 kcal over 120
        # days at a logged 2,600; the latest window's rate for every day
        # overstated the 12-week deficit.
        weight, days = 80.0, {}
        truth = {}
        for n in range(120):
            day = END - timedelta(days=119 - n)
            truth[day] = 2300.0 + 700.0 * n / 119
            # The prior fat share's 7,153.5 kcal per kg (Hall 2008).
            weight += (2600.0 - truth[day]) / 7153.5
            days[day] = DayInputs(
                intake_kcal=2600.0, protein_g=150.0, carbohydrate_g=300.0,
                fat_g=80.0, entries=3, weight_kg=weight, activity_kcal=600.0,
                activity_error_kcal=60.0, neat_kcal=250.0, sport_kcal=350.0,
            )  # fmt: skip
        source = EnergyInputs(days, {}, rmr_kcal=1500.0)
        windows = energy_windows(source, END)
        period = [END - timedelta(days=n) for n in range(84)]
        totals = own_day_totals(source, windows, {}, period)
        balance = statistics.fmean(2600.0 - cast(float, totals[d]) for d in period)
        true_balance = statistics.fmean(2600.0 - truth[d] for d in period)
        # The latest window's rate on all 84 days: its own days' total.
        latest_rule = 2600.0 - cast(float, totals[END])
        band = statistics.fmean(
            cast(Estimate, w.adaptive_tdee).band_kcal for w in windows
        )
        self.assertLessEqual(abs(balance - true_balance), band)
        # The latest window's rate on all days misses by about 170 kcal/day:
        # beyond the band before the water term widened it (#3690), still far
        # beyond the per-window rule's own error.
        self.assertGreater(abs(latest_rule - true_balance), 150)
        self.assertLess(abs(balance - true_balance), 50)
        # The last 14 days lie in the latest window: its rate, as before.
        latest_total = cast(float, totals[END])
        self.assertTrue(all(totals[d] == latest_total for d in period[:14]))
        # Older days follow their own (lower) window.
        self.assertLess(cast(float, totals[period[-1]]), latest_total)
        # The diary's own day row is the same value for an old and a recent
        # day (Gitar on #3647).
        for day in (period[-1], period[40], END):
            with self.subTest(day=day):
                view = energy_view(source, windows, {}, day=day)
                self.assertEqual(totals[day], row(view, "own")["total_kcal"])

    def test_a_day_without_a_session_has_sport_zero_not_a_mean(self) -> None:
        rest_day = END - timedelta(days=1)
        quiet = DayInputs(weight_kg=80.0, activity_kcal=250.0, neat_kcal=250.0,
                          sport_kcal=0.0, protein_g=250.0, entries=3)  # fmt: skip
        split = row(self.view(rest_day, {rest_day: quiet}), "katch_mcardle")["split"]
        self.assertEqual(
            (250, 0, 250), (split["neat_kcal"], split["sport_kcal"], split["tef_kcal"])
        )
        # 1,838.8 + 250 + 0 + 250, plus the provisional learned everyday 8 (#3723).
        self.assertEqual(
            2339 + 8,
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

    def test_kcal_without_macros_adds_estimated_digestion(self) -> None:
        # #3617 item 3: 2,000 kcal of which the macros cover 870 add 10 % of the
        # other 1,130 to the macros' 0.25 x 200 + 0.10 x 400 + 0.015 x 270 =
        # 94.05 kcal, so digestion is 207.05; the read marks it estimated.
        day = END - timedelta(days=3)
        partly = DayInputs(
            weight_kg=80.0, activity_kcal=250.0, neat_kcal=250.0, sport_kcal=0.0,
            intake_kcal=2000.0, protein_g=50.0, carbohydrate_g=100.0, fat_g=30.0,
            entries=3, uncovered_kcal=1130.0,
        )  # fmt: skip
        km = row(self.view(day, {day: partly}), "katch_mcardle")
        self.assertEqual(207, km["split"]["tef_kcal"])
        self.assertTrue(read_energy_view(None, sources(), DAY3, DAY1)["tef_estimated"])
        self.assertFalse(read_energy_view(None, sources(), DAY3)["tef_estimated"])

    def test_today_without_any_data_yet_totals_its_resting_alone(self) -> None:
        # #3494: early morning there are no steps, sessions or meals yet; today
        # they count 0 so far, a past day with the same gaps has no total.
        today = END + timedelta(days=1)
        bare = DayInputs(weight_kg=80.0)
        view = self.view(today, {today: bare})
        km = row(view, "katch_mcardle")
        # With the provisional learned everyday, which needs no data of the day (#3723).
        self.assertEqual(km["resting_kcal"] + learned_kcal(km), km["total_kcal"])
        self.assertEqual(8, learned_kcal(km))
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
            km["resting_kcal"]
            + split["sport_kcal"]
            + (split["tef_kcal"] or 0)
            + learned_kcal(km),
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
        # #3597: yesterday's summary ended at 12:18 (#3530), so its resting kcal
        # covers half a day; the newest complete day counts, named by its date.
        health[END.isoformat()] = {
            "resting_kcal": 900.0,
            "calories": 1500.0,
            "wellness_end_min": 738.0,
        }
        device = self.today_device(health)
        self.assertEqual(
            (1700, 2100, (END - timedelta(days=1)).isoformat()),
            (device["resting_kcal"], device["total_kcal"], device["resting_from"]),
        )
        # A finished day (1,440) is complete.
        health[END.isoformat()]["wellness_end_min"] = 1440.0
        self.assertEqual(900, self.today_device(health)["resting_kcal"])
        del health[END.isoformat()]
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
                    (1839, 219, sport, total, 150, 28),
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
                    (2700, 1839, 28),
                    (
                        window_row["total_kcal"],
                        window_row["resting_kcal"],
                        window_row["window_days"],
                    ),
                )
        # A day without its parts (no meals, no activity) has no own day total.
        bare = row(self.view(END - timedelta(days=5)), "own")
        self.assertEqual((1839, None), (bare["resting_kcal"], bare["total_kcal"]))


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
    today: dict[str, Any] | None,
    sessions: list[dict[str, Any]],
    meals: int,
    yesterday: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Today's view; with ``yesterday``'s summary, yesterday's view instead."""
    return read_energy_view(
        None,
        scenario_sources(today, sessions, meals, yesterday),
        SCENARIO_END,
        SCENARIO_TODAY if yesterday is None else SCENARIO_END,
    )


def scenario_sources(
    today: dict[str, Any] | None,
    sessions: list[dict[str, Any]],
    meals: int,
    yesterday: dict[str, Any] | None = None,
) -> EnergySources:
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
    if yesterday is not None:
        health[SCENARIO_END.isoformat()] = yesterday

    def within(items: list[dict[str, Any]], key: str, s: date, e: date) -> list:
        return [i for i in items if s <= date.fromisoformat(i[key][:10]) <= e]

    return EnergySources(
        diary=lambda _db, s, e: within(diary, "meal_date", s, e),
        daily_health=lambda _db, _s, _e: health,
        weights=lambda _db, s, e: {d: 75.0 for d in days[::2] if s <= d <= e},
        sessions=lambda _db, s, e: within(sessions, "started_at_local", s, e),
        streams=lambda _db, _key: {},
        resting=lambda _db, _day: (1700.0, "katch_mcardle"),
        body_fat=lambda _db, _s, _e: {SCENARIO_END - timedelta(days=10): 15.0},
        profile=lambda: {"height_cm": "180"},
    )


class OwnHistoryTests(unittest.TestCase):
    def test_the_kept_history_gives_the_period_balance_s_own_totals(self) -> None:
        # #3630: the wiring keeps own_history ending yesterday; its totals are
        # the direct read's for 14 and 84 days, real values, not only nulls.
        sources = scenario_sources(None, [], 0)
        history = own_history(None, sources, SCENARIO_END)
        for count in (14, 84):
            days = [SCENARIO_END - timedelta(days=n) for n in range(count)]
            direct = read_own_day_totals(None, sources, SCENARIO_END, days)
            with self.subTest(days=count):
                self.assertEqual(count, sum(v is not None for v in direct.values()))
                self.assertEqual(direct, own_day_totals(*history, days))


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

    def test_everyday_energy_per_thousand_steps_is_physiological(self) -> None:
        # #3611: a walk in the history whose speed/cadence ratio gives 2.68 m
        # per step (6 km in 20 moving minutes at 56 strides/min) does not count;
        # a walk of recorded steps (1,500 m / 2,000) sets the step at 0.75 m,
        # so the evening's 8,000 steps outside the session cost 75 x 8,000 x
        # 0.75 x 2.01 / 4,184 = 216.2 kcal, 27.0 per 1,000 steps (the former
        # mean of ratios, (2.68 + 0.75) / 2 = 1.71 m, gave 61.6).
        walks = [
            {"started_at_local": f"{SCENARIO_END - timedelta(days=20)}T09:00:00",
             "sport": "walk", "distance_m": 6000.0, "avg_cadence": 56.0,
             "moving_duration_s": 1200.0, "elapsed_duration_s": 4500.0},
            {"started_at_local": f"{SCENARIO_END - timedelta(days=10)}T09:00:00",
             "sport": "walk", "distance_m": 1500.0, "steps": 2000.0,
             "moving_duration_s": 1200.0, "elapsed_duration_s": 1200.0},
        ]  # fmt: skip
        evening = day_scenario(
            {"steps": 9500.0, "floors": 0.0, "resting_kcal": 1650.0,
             "active_kcal": 640.0},
            [STRENGTH, *walks], 3,
        )  # fmt: skip
        neat = row(evening, "katch_mcardle")["split"]["neat_kcal"]
        per_thousand = neat / 8.0
        self.assertTrue(20.0 <= per_thousand <= 40.0, per_thousand)
        self.assertEqual(216, neat)

    def test_every_row_has_a_total_so_far(self) -> None:
        # #3494: no phase of today leaves Tag gesamt empty.
        for phase, view in self.phases.items():
            for item in view["rows"]:
                with self.subTest(phase=phase, source=item["source"]):
                    self.assertTrue(view["day_partial"])
                    self.assertIsNotNone(item["total_kcal"])
        # Early morning a formula row's total is its rest and its provisional
        # learned everyday (#3723); without a session sport is 0, not open.
        for source in FORMULAS:
            item = row(self.morning, source)
            with self.subTest(phase="morning", source=source):
                self.assertGreater(learned_kcal(item), 0)
                self.assertEqual(
                    item["resting_kcal"] + learned_kcal(item), item["total_kcal"]
                )
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
                self.assertEqual(
                    item["resting_kcal"] + counted + learned_kcal(item),
                    item["total_kcal"],
                )
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
                # #3550: Ruhe plus "übriger Alltag" is the residual; the formula
                # row's learned everyday is no day part of the own row (#3723).
                rest = own["resting_kcal"] + own["split"].get("unmeasured_kcal", 0)
                self.assertEqual(
                    own["total_kcal"],
                    rest
                    + katch["total_kcal"]
                    - katch["resting_kcal"]
                    - learned_kcal(katch),
                )
                self.assertNotEqual(window["total_kcal"], own["total_kcal"])

    def test_a_past_day_whose_summary_ended_before_its_session(self) -> None:
        # #3612: yesterday's summary ended at 12:18, before the 17:00 session.
        # Its everyday is unknown and still missing, not near 0, and the day
        # leaves the 28-day means; Garmin's split stays within its total.
        session = {**STRENGTH, "started_at_local": f"{SCENARIO_END}T17:00:00"}
        summary = {"steps": 4000.0, "floors": 2.0, "resting_kcal": 900.0,
                   "active_kcal": 120.0, "calories": 1020.0,
                   "wellness_end_min": 738.0}  # fmt: skip
        past = day_scenario(None, [session], 0, yesterday=summary)
        whole = day_scenario(None, [], 0, yesterday={**summary, "steps": 8000.0,
                             "floors": 5.0, "wellness_end_min": 1440.0})  # fmt: skip
        self.assertFalse(past["day_partial"])
        self.assertEqual({"as_of": "12:18", "behind": True}, past["wellness"])
        for source in (*FORMULAS, "own"):
            item = row(past, source)
            with self.subTest(source=source):
                self.assertIsNone(item["split"]["neat_kcal"])
                self.assertIn("neat", item["pending_parts"])
                self.assertIsNone(item["total_kcal"])
        device = row(past, "device")
        self.assertIn("neat", device["pending_parts"])
        counted = sum(device["split"][p] or 0 for p in DAY_PARTS)
        self.assertLessEqual(device["resting_kcal"] + counted, device["total_kcal"])
        for part in ("neat_kcal", "sport_kcal"):
            self.assertEqual(
                whole["own_window"]["split"][part], past["own_window"]["split"][part]
            )


def eaten_week(
    days: int = 7, intake: float = 2200.0, sport: float | None = 600.0
) -> EnergyInputs:
    """``days`` logged days ending on END at 80 kg, 15 % body fat (68 kg lean)."""
    logged = {
        END - timedelta(days=n): DayInputs(
            weight_kg=80.0, intake_kcal=intake, entries=3, sport_kcal=sport
        )
        for n in range(days)
    }
    base = inputs()
    return replace(base, days={**base.days, **logged})


class EnergyGuardTests(unittest.TestCase):
    """The energy availability and loss-rate notes (#3917)."""

    def view(self, source: EnergyInputs, latest: EnergyWindow | None = None) -> Any:
        return energy_view(source, [latest or window(END, 2700.0, 1850.0)], garmin())

    def test_availability_is_the_net_intake_per_kg_fat_free_mass(self) -> None:
        # (2,200 eaten - 600 net session energy) / 68 kg = 23.5, shown as 23.
        note = self.view(eaten_week())["energy_availability"]
        self.assertEqual(
            (23, 7, 30, 68.0, True),
            (note["kcal_per_kg_ffm"], note["days"], note["floor_kcal_per_kg_ffm"],
             note["lean_mass_kg"], note["below"]),
        )  # fmt: skip
        # The session energy leaves the intake: without it 2,200 / 68 = 32.4.
        rested = self.view(eaten_week(sport=0.0))["energy_availability"]
        self.assertEqual((32, False), (rested["kcal_per_kg_ffm"], rested["below"]))

    def test_the_floor_fires_on_the_shown_whole_number(self) -> None:
        # (2,108 - 68) / 68 = 30.0 stands at the floor; 2,106 gives 29.97, shown 29.
        for intake, shown, below in ((2108.0, 30, False), (2106.0, 29, True)):
            with self.subTest(intake=intake):
                note = self.view(eaten_week(intake=intake, sport=68.0))[
                    "energy_availability"
                ]
                self.assertEqual(
                    (shown, below), (note["kcal_per_kg_ffm"], note["below"])
                )

    def test_it_needs_five_complete_days_among_seven_and_a_lean_mass(self) -> None:
        self.assertIsNone(self.view(eaten_week(4))["energy_availability"])
        self.assertEqual(5, self.view(eaten_week(5))["energy_availability"]["days"])
        # A day without a priced session is no complete day for it.
        unpriced = eaten_week()
        days = dict(unpriced.days)
        for n in (0, 1, 2):
            days[END - timedelta(days=n)] = replace(
                days[END - timedelta(days=n)], sport_kcal=None
            )
        self.assertIsNone(
            self.view(replace(unpriced, days=days))["energy_availability"]
        )
        # A day eaten under 70 % of the resting rate is no complete day either.
        thin = eaten_week(5)
        days = dict(thin.days)
        days[END] = replace(days[END], intake_kcal=1000.0)
        self.assertIsNone(self.view(replace(thin, days=days))["energy_availability"])
        # Without body fat there is no lean mass.
        self.assertIsNone(
            self.view(replace(eaten_week(), body_fat_pct={}))["energy_availability"]
        )

    def test_the_weight_line_falling_faster_than_one_percent_a_week_is_fast(
        self,
    ) -> None:
        # Fat share 0.80: 0.8 x 9,441 + 0.2 x 1,816 = 7,916 kcal/kg. 0.15 kg/day
        # at 80 kg is 1.05 kg a week, 1.3 %; 80 kg x 1 % a week is 0.8 kg a week.
        def rate(kg_per_day: float, **changes: Any) -> Any:
            store = MEASURED._replace(
                kcal_per_day=-kg_per_day * 7916.0, weight_end_kg=80.0, **changes
            )
            return self.view(inputs(), replace(window(END, 3800.0), store=store))[
                "loss_rate"
            ]

        self.assertEqual(
            {"pct_per_week": 1.3, "limit_pct_per_week": 1.0, "fast": True}, rate(0.15)
        )
        self.assertEqual(
            {"pct_per_week": 1.0, "limit_pct_per_week": 1.0, "fast": False},
            rate(0.8 / 7),
        )
        self.assertIsNone(rate(-0.1), "a rising weight has no loss rate")
        # M12's 21 days of span and the window's 8 weigh-ins gate it.
        self.assertIsNone(rate(0.15, days=20))
        short = replace(window(END, 3800.0), weighins=7, store=MEASURED)
        self.assertIsNone(self.view(inputs(), short)["loss_rate"])


class ExpectedTotalTests(unittest.TestCase):
    """Today's expected day total beside the total so far (#3917)."""

    TODAY = END + timedelta(days=1)

    def view(self, planned: float | None, day: date | None = None) -> Any:
        return energy_view(
            inputs(),
            [window(END, 2700.0, 1850.0)],
            garmin(),
            day=day or self.TODAY,
            planned_kcal=planned,
        )

    def test_it_adds_the_usual_steps_digestion_and_the_planned_units(self) -> None:
        # Katch-McArdle's 1,839 rest, its provisional learned everyday +8, the
        # window's 250 steps and 250 digestion, and 177 planned (3.5 - 1 MET x
        # 1,700 kcal/day x 1 h / 24): 2,524. The goal stays on the total so far.
        view = self.view(177.0)
        km = row(view, "katch_mcardle")
        self.assertEqual(2524, view["expected_total_kcal"])
        self.assertEqual(1847, km["total_kcal"], "so far: rest and learned only")
        # A rest day expects the usual steps and digestion alone.
        self.assertEqual(2347, self.view(0.0)["expected_total_kcal"])

    def test_it_never_lies_below_what_the_day_counts_so_far(self) -> None:
        latest = window(END, 2700.0, 1850.0)
        split = {"resting_kcal": 1800, "neat_kcal": 0, "sport_kcal": 0, "tef_kcal": 0}
        usual = expected_total_kcal({"split": split}, latest, 100.0, False)
        assert usual is not None
        # An unplanned 600 kcal ride, 400 everyday and 300 digestion so far.
        busy = {**split, "neat_kcal": 400, "sport_kcal": 600, "tef_kcal": 300}
        self.assertEqual(
            usual + (400 - 250) + (600 - 100) + (300 - 250),
            expected_total_kcal({"split": busy}, latest, 100.0, False),
        )

    def test_garmin_s_row_takes_its_own_everyday_mean(self) -> None:
        latest = window(END, 2700.0, 1850.0)
        split = {"resting_kcal": 1800, "neat_kcal": 0, "sport_kcal": 0, "tef_kcal": 0}
        # Garmin's everyday mean of 400, not the window's 250; no digestion.
        self.assertEqual(
            1800 + 400 + 100,
            expected_total_kcal({"split": split}, latest, 100.0, True, 400.0),
        )
        self.assertIsNone(expected_total_kcal({"split": split}, latest, 100.0, True))

    def test_it_is_for_today_and_known_parts_only(self) -> None:
        self.assertIsNone(self.view(None)["expected_total_kcal"])
        self.assertIsNone(self.view(177.0, END)["expected_total_kcal"])
        no_means = replace(window(END, 2700.0, 1850.0), neat_kcal_raw=None)
        view = energy_view(
            inputs(), [no_means], garmin(), day=self.TODAY, planned_kcal=177.0
        )
        self.assertIsNone(view["expected_total_kcal"])

    def test_the_read_costs_the_days_planned_units(self) -> None:
        # Through the reader: a rest day, then a planned hour of strength at the
        # 1,700 kcal/day resting rate, 177 kcal more; a unit without a way to
        # cost its sport leaves the total unknown.
        def expected(units: list[dict[str, Any]], day: date = SCENARIO_TODAY) -> Any:
            source = replace(
                scenario_sources({"resting_kcal": 300.0}, [], 0),
                planned=lambda _db, _day: units,
            )
            return read_energy_view(None, source, SCENARIO_END, day)[
                "expected_total_kcal"
            ]

        rest = expected([])
        self.assertIsNotNone(rest)
        hour = {"sport": "strength", "duration_s": 3600.0}
        self.assertAlmostEqual(177, expected([hour]) - rest, delta=1)
        self.assertIsNone(expected([{"sport": "ride", "duration_s": 3600.0}]))
        self.assertIsNone(expected([hour], SCENARIO_END), "yesterday has none")

    def test_the_goal_stays_on_the_total_so_far(self) -> None:
        goal = {"energy_goal": "lose", "energy_goal_kcal": "500"}
        before = self.view(None)
        after = energy_view(
            inputs(), [window(END, 2700.0, 1850.0)], garmin(), day=self.TODAY,
            goal=goal, planned_kcal=177.0,
        )  # fmt: skip
        target = after["goal"]["target_kcal"]
        self.assertEqual(row(before, "katch_mcardle")["total_kcal"] - 500, target)


class PlannedUnitCostTests(unittest.TestCase):
    """Planned units priced from their duration by their sport's M11 method (#3917)."""

    def test_strength_takes_its_general_met_and_other_sports_their_own_rate(
        self,
    ) -> None:
        # (3.5 - 1) x 1,700 x 3,600 / 86,400 = 177.08 kcal.
        strength = {"sport": "strength", "duration_s": 3600.0}
        self.assertAlmostEqual(
            177.0833, planned_net_kcal([strength], {}, 1700.0) or 0.0, places=3
        )
        self.assertIsNone(planned_net_kcal([strength], {}, None))
        # A 90 minute ride at the athlete's 600 kcal/h is 900 kcal; both units add.
        ride = {"sport": "ride", "duration_s": 5400.0}
        self.assertEqual(900.0, planned_net_kcal([ride], {"ride": 600.0}, 1700.0))
        self.assertAlmostEqual(
            1077.0833,
            planned_net_kcal([ride, strength], {"ride": 600.0}, 1700.0) or 0.0,
            places=3,
        )
        # No unit is a rest day; a sport without a rate or a unit without a
        # duration leaves the total unknown.
        self.assertEqual(0.0, planned_net_kcal([], {}, 1700.0))
        self.assertIsNone(planned_net_kcal([ride], {"run": 700.0}, 1700.0))
        self.assertIsNone(
            planned_net_kcal(
                [{"sport": "ride", "duration_s": None}], {"ride": 600.0}, 1700.0
            )
        )

    def test_the_day_s_units_carry_sport_and_seconds_without_deleted_ones(self) -> None:
        units: list[dict[str, Any]] = [
            {"sport": "strength", "duration_minutes": 45},
            {"sport": "ride", "planned_duration_s": 5400, "duration_minutes": 60},
            {"sport": "run", "duration_minutes": 30, "local_deleted": True},
            {"sport": "run"},
        ]
        self.assertEqual(
            [
                {"sport": "strength", "duration_s": 2700},
                {"sport": "ride", "duration_s": 5400},
                {"sport": "run", "duration_s": None},
            ],
            planned_inputs(units),
        )

    def test_the_rate_is_the_sport_s_priced_net_kcal_per_hour_of_28_days(self) -> None:
        def cost(days_back: int, kcal: float | None, seconds: float) -> SessionCost:
            return SessionCost(
                END - timedelta(days=days_back), kcal, "power", 0.0,
                sport="ride", duration_s=seconds,
            )  # fmt: skip

        costs = [
            cost(1, 600.0, 3600.0),
            cost(5, 900.0, 5400.0),
            cost(27, 900.0, 3600.0),
            cost(28, 5000.0, 3600.0),  # older than 28 days
            cost(2, None, 3600.0),  # unpriced
        ]
        # (600 + 900 + 900) kcal over 1 + 1.5 + 1 = 3.5 hours = 685.71 kcal/h.
        rates = usual_kcal_per_hour(costs, END)
        self.assertEqual(["ride"], list(rates))
        self.assertAlmostEqual(685.7143, rates["ride"], places=3)


if __name__ == "__main__":
    unittest.main()

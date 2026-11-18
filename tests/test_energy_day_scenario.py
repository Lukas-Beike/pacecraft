"""One synthetic day walks the diary's energy table from morning to evening (#3541).

Each phase reads the whole view through ``read_energy_view`` and checks the
invariants the athlete found broken on real days: every row has a total so far
(#3494), the formula rows price a strength session near Garmin's own and wait
for a lagging day summary (#3530), and the own row is the day, its parts equal
to the formula rows', with the 28-day values only in ``own_window`` (#3531).
"""

from __future__ import annotations

import unittest
from datetime import date, timedelta
from typing import Any

from backend.canonical.vocabulary import ProviderId
from backend.nutrition.energy_balance import history_start
from backend.nutrition.energy_inputs import EnergySources
from backend.nutrition.energy_view import read_energy_view

TODAY = date(2026, 11, 18)
END = TODAY - timedelta(days=1)
START = history_start(END)
FORMULAS = ("katch_mcardle", "cunningham")
PARTS = ("neat_kcal", "sport_kcal", "tef_kcal")
# The reported session: 45 minutes of strength, 19 of them moving, which
# Garmin priced at 278 kcal including its resting share (#3530).
STRENGTH = {
    "started_at_local": f"{TODAY}T17:00:00",
    "sport": "strength",
    "moving_duration_s": 19 * 60.0,
    "elapsed_duration_s": 45 * 60.0,
    "energy_kcal": 278.0,
    "steps": 1500.0,
    "record_ids": {ProviderId.GARMIN.value: "g-strength"},
}
MEAL = {"kcal": 900.0, "protein_g": 50.0, "carbs_g": 100.0, "fat_g": 30.0}


def _history() -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Twelve settled weeks: 2,600 kcal eaten, 8,000 steps, a flat 75 kg."""
    health, diary = {}, []
    day = START
    while day <= END:
        health[day.isoformat()] = {
            "steps": 8000.0, "floors": 5.0, "resting_kcal": 1700.0,
            "active_kcal": 450.0, "calories": 2150.0,
        }  # fmt: skip
        diary += [{**MEAL, "kcal": 1300.0, "meal_date": day.isoformat()}] * 2
        day += timedelta(days=1)
    return health, diary


def scenario(
    today_health: dict[str, Any] | None,
    sessions: list[dict[str, Any]],
    meals: int,
) -> dict[str, Any]:
    """The view of TODAY for one phase of the day."""
    health, diary = _history()
    if today_health is not None:
        health[TODAY.isoformat()] = today_health
    diary += [{**MEAL, "meal_date": TODAY.isoformat()}] * meals

    def in_range(start: date, end: date) -> list[dict[str, Any]]:
        return [
            s
            for s in sessions
            if start <= date.fromisoformat(s["started_at_local"][:10]) <= end
        ]

    sources = EnergySources(
        diary=lambda _db, s, e: [
            m for m in diary if s <= date.fromisoformat(m["meal_date"]) <= e
        ],
        daily_health=lambda _db, _s, _e: health,
        weights=lambda _db, s, e: {
            d: 75.0
            for d in (START + timedelta(days=n) for n in range(0, 200, 2))
            if s <= d <= min(e, END)
        },
        sessions=lambda _db, s, e: in_range(s, e),
        streams=lambda _db, _key: {},
        resting=lambda _db, _day: (1700.0, "katch_mcardle"),
        body_fat=lambda _db, _s, _e: {END - timedelta(days=10): 15.0},
        profile=lambda: {"height_cm": "180"},
    )
    return read_energy_view(None, sources, END, TODAY)


def row(view: dict[str, Any], source: str) -> dict[str, Any]:
    return next(r for r in view["rows"] if r["source"] == source)


MORNING = scenario({"resting_kcal": 300.0}, [], 0)
# Garmin's day summary still lags: fewer day steps than the session's own and
# active kcal below the session's.
AFTER_STRENGTH = scenario(
    {"steps": 500.0, "floors": 0.0, "resting_kcal": 1200.0, "active_kcal": 120.0},
    [STRENGTH],
    1,
)
EVENING = scenario(
    {"steps": 9500.0, "floors": 6.0, "resting_kcal": 1650.0, "active_kcal": 640.0},
    [STRENGTH],
    3,
)
PHASES = {"morning": MORNING, "after strength": AFTER_STRENGTH, "evening": EVENING}


class EnergyDayScenarioTests(unittest.TestCase):
    def test_every_row_has_a_total_so_far(self) -> None:
        # #3494: no phase of today leaves Tag gesamt empty.
        for phase, view in PHASES.items():
            for item in view["rows"]:
                with self.subTest(phase=phase, source=item["source"]):
                    self.assertTrue(view["day_partial"])
                    self.assertIsNotNone(item["total_kcal"])

    def test_the_morning_total_is_the_resting_rate_alone(self) -> None:
        for source in FORMULAS:
            with self.subTest(source=source):
                item = row(MORNING, source)
                self.assertEqual(item["resting_kcal"], item["total_kcal"])
                # No steps or meals yet; without a session sport is 0, not open.
                self.assertEqual(["neat", "tef"], sorted(item["pending_parts"]))
                self.assertEqual(0, item["split"]["sport_kcal"])

    def test_formula_sport_lies_near_garmins_session(self) -> None:
        # #3530: the strength session is priced over its 45 minutes, so the
        # formula rows' sport is of the order of Garmin's net session kcal.
        for phase in ("after strength", "evening"):
            garmin = row(PHASES[phase], "device")["split"]["sport_kcal"]
            for source in FORMULAS:
                with self.subTest(phase=phase, source=source):
                    sport = row(PHASES[phase], source)["split"]["sport_kcal"]
                    self.assertGreater(garmin, 0)
                    self.assertTrue(0.5 <= sport / garmin <= 2.0, (sport, garmin))

    def test_a_lagging_day_summary_leaves_everyday_pending_not_negative(
        self,
    ) -> None:
        # #3530: everyday waits for the day summary and counts 0 so far.
        for source in (*FORMULAS, "device"):
            with self.subTest(source=source):
                item = row(AFTER_STRENGTH, source)
                self.assertIn("neat", item["pending_parts"])
                split = item["split"]
                counted = sum(split[p] or 0 for p in PARTS)
                self.assertEqual(item["resting_kcal"] + counted, item["total_kcal"])
        for source in (*FORMULAS, "device"):
            with self.subTest(phase="evening", source=source):
                self.assertNotIn("neat", row(EVENING, source)["pending_parts"])
                self.assertGreater(row(EVENING, source)["split"]["neat_kcal"], 0)

    def test_the_own_row_is_the_day_not_the_28_day_mean(self) -> None:
        # #3531: its day parts are the formula rows', its rest the window's.
        for phase, view in PHASES.items():
            own, katch = row(view, "own"), row(view, "katch_mcardle")
            window = view["own_window"]
            with self.subTest(phase=phase):
                for part in PARTS:
                    self.assertEqual(katch["split"][part], own["split"][part])
                self.assertEqual(window["resting_kcal"], own["resting_kcal"])
                self.assertEqual(
                    own["total_kcal"],
                    own["resting_kcal"] + katch["total_kcal"] - katch["resting_kcal"],
                )
                self.assertNotEqual(window["total_kcal"], own["total_kcal"])
                self.assertNotIn("own_window", [r["source"] for r in view["rows"]])


if __name__ == "__main__":
    unittest.main()

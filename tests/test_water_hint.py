"""The probable-water hint for a marked weigh-in step (#4047)."""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from backend.nutrition.energy_balance import DayInputs
from backend.nutrition.intake_totals import salt_totals
from backend.nutrition.water_hint import (
    DayIntake,
    WaterHint,
    water_hint,
    water_hint_for_latest,
    weight_step,
)

START = date(2026, 9, 1)
DAYS = 40
SOLID = 1.0


def steady(step_on: int | None = None, step_kg: float = 0.0) -> dict[date, float]:
    """A weigh-in a day, 80 kg with a small scatter, one day off by ``step_kg``."""
    scatter = (0.1, -0.1, 0.2, -0.2, 0.0)
    weights = {
        START + timedelta(days=i): 80.0 + scatter[i % len(scatter)] for i in range(DAYS)
    }
    if step_on is not None:
        weights[START + timedelta(days=step_on)] += step_kg
    return weights


def intake(
    previous: int, carbs: float = 300.0, salt: float = 6.0, share: float = SOLID
) -> dict[date, DayIntake]:
    """The usual days (300 g carbohydrates, 6 g salt with a small spread), one day apart."""
    spread = (0.0, 10.0, -10.0, 20.0, -20.0)
    days = {
        START + timedelta(days=i): DayIntake(
            300.0 + spread[i % len(spread)], 6.0 + spread[i % len(spread)] / 20, SOLID
        )
        for i in range(DAYS)
    }
    days[START + timedelta(days=previous)] = DayIntake(carbs, salt, share)
    return days


STEP = 35
DAY = START + timedelta(days=STEP)
PREVIOUS = STEP - 1


class WaterHintTests(unittest.TestCase):
    def test_a_jump_after_a_salty_and_carbohydrate_rich_day_is_probable_water(
        self,
    ) -> None:
        weights = steady(STEP, 1.6)
        hint = water_hint(
            weights, intake(PREVIOUS, carbs=450.0, salt=11.0), frozenset(), DAY
        )
        self.assertEqual(hint, WaterHint("up", ("salt", "carbs")))

    def test_each_cause_is_named_on_its_own(self) -> None:
        weights = steady(STEP, 1.6)
        salty = water_hint(weights, intake(PREVIOUS, salt=11.0), frozenset(), DAY)
        carbs = water_hint(weights, intake(PREVIOUS, carbs=450.0), frozenset(), DAY)
        self.assertEqual(
            (salty and salty.causes, carbs and carbs.causes), (("salt",), ("carbs",))
        )

    def test_a_drop_after_low_carbohydrates_following_a_long_session_is_probable_water(
        self,
    ) -> None:
        weights = steady(STEP, -1.6)
        previous = START + timedelta(days=PREVIOUS)
        hint = water_hint(
            weights, intake(PREVIOUS, carbs=120.0), frozenset({previous}), DAY
        )
        self.assertEqual(hint, WaterHint("down", ("carbs_after_session",)))
        # Without the long session the same low day explains nothing.
        self.assertIsNone(
            water_hint(weights, intake(PREVIOUS, carbs=120.0), frozenset(), DAY)
        )

    def test_no_hint_without_a_marked_deviation(self) -> None:
        weights = steady(STEP, 0.3)
        self.assertIsNone(weight_step(weights, DAY))
        self.assertIsNone(
            water_hint(
                weights, intake(PREVIOUS, carbs=450.0, salt=11.0), frozenset(), DAY
            )
        )

    def test_the_direction_must_match_the_previous_day(self) -> None:
        up = steady(STEP, 1.6)
        # A jump up after a low carbohydrate day, or a drop after a salty day, explains nothing.
        previous = START + timedelta(days=PREVIOUS)
        self.assertIsNone(
            water_hint(up, intake(PREVIOUS, carbs=120.0), frozenset({previous}), DAY)
        )
        down = steady(STEP, -1.6)
        self.assertIsNone(
            water_hint(
                down,
                intake(PREVIOUS, carbs=450.0, salt=11.0),
                frozenset({previous}),
                DAY,
            )
        )

    def test_a_usual_previous_day_explains_no_jump(self) -> None:
        self.assertIsNone(
            water_hint(steady(STEP, 1.6), intake(PREVIOUS), frozenset(), DAY)
        )

    def test_sparse_salt_data_says_nothing_about_salt(self) -> None:
        weights = steady(STEP, 1.6)
        sparse = water_hint(
            weights, intake(PREVIOUS, salt=11.0, share=0.2), frozenset(), DAY
        )
        self.assertIsNone(sparse)
        # Carbohydrates still speak when salt is sparse.
        carbs = water_hint(
            weights,
            intake(PREVIOUS, carbs=450.0, salt=11.0, share=0.2),
            frozenset(),
            DAY,
        )
        self.assertEqual(carbs, WaterHint("up", ("carbs",)))

    def test_unknown_intake_or_too_few_weigh_ins_give_no_hint(self) -> None:
        weights = steady(STEP, 1.6)
        self.assertIsNone(water_hint(weights, {}, frozenset(), DAY))
        few = {d: w for d, w in weights.items() if d >= DAY - timedelta(days=4)}
        self.assertIsNone(weight_step(few, DAY))

    def test_the_hint_changes_nothing_it_reads(self) -> None:
        weights = steady(STEP, 1.6)
        before = dict(weights)
        water_hint(weights, intake(PREVIOUS, carbs=450.0, salt=11.0), frozenset(), DAY)
        self.assertEqual(weights, before)

    def test_the_energy_read_payload_names_the_day_direction_and_causes(self) -> None:
        weights = steady(STEP, 1.6)
        usual = intake(PREVIOUS, carbs=450.0, salt=11.0)
        days = {
            d: DayInputs(
                carbohydrate_g=usual[d].carbs_g or 0.0,
                salt_g=usual[d].salt_g,
                salt_known_share=usual[d].salt_known_share,
                entries=2,
                weight_kg=weights[d],
            )
            for d in weights
        }
        self.assertEqual(
            water_hint_for_latest(days, DAY + timedelta(days=0)),
            {"date": DAY.isoformat(), "direction": "up", "causes": ["salt", "carbs"]},
        )
        # A weigh-in older than yesterday is not the current one; a usual day gives none.
        self.assertIsNone(water_hint_for_latest(days, DAY + timedelta(days=5)))
        self.assertIsNone(water_hint_for_latest({}, DAY))

    def test_a_days_salt_is_summed_with_the_share_of_values_that_report_it(
        self,
    ) -> None:
        def entry(salt: float | None) -> dict[str, object]:
            product = {"basis_amount": 100.0, "salt_g": salt}
            return {
                "nutrition_basis": {
                    "kind": "local_product",
                    "product": product,
                    "amount": 50.0,
                }
            }

        self.assertEqual(salt_totals([entry(2.0), entry(None)]), (1.0, 0.5))
        self.assertEqual(salt_totals([entry(None)]), (None, 0.0))
        self.assertEqual(salt_totals([]), (None, 0.0))


if __name__ == "__main__":
    unittest.main()

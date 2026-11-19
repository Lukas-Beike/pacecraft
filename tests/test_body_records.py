"""The current weight and body fat from Körper's records (#3640)."""

from __future__ import annotations

import unittest
from datetime import date, timedelta
from typing import Any

from backend.athlete.body_composition import Sex
from backend.athlete.body_records import read_current_body
from backend.athlete.skinfolds import SkinfoldMeasurement
from backend.canonical.vocabulary import MetricKind, ProviderId
from tests.test_energy_inputs import weigh

DAY = date(2026, 11, 14)
GARMIN, WAHOO = ProviderId.GARMIN, ProviderId.WAHOO
FOLDS = {"chest": 10.0, "abdomen": 20.0, "thigh": 15.0}


class Reader:
    """Observations by kind, filtered by day like the canonical reader."""

    def __init__(self, **by_kind: list[Any]) -> None:
        self.by_kind = by_kind

    def observations(
        self, _db: Any, kind: MetricKind, *, start=None, end=None, sport=None
    ):
        return tuple(
            o
            for o in self.by_kind.get(kind.value, [])
            if (start is None or o.observed_on >= start)
            and (end is None or o.observed_on <= end)
        )


def skinfolds(*days: date):
    def read(_db: Any, until: date) -> list[SkinfoldMeasurement]:
        found = [SkinfoldMeasurement(d, Sex.MALE, FOLDS, 14.0) for d in days]
        return sorted((m for m in found if m.measured_on <= until), reverse=True,
                      key=lambda m: m.measured_on)  # fmt: skip

    return read


class CurrentBodyTests(unittest.TestCase):
    def test_the_weight_is_the_weigh_in_line_never_wahoo(self) -> None:
        # Falling 0.1 kg a day over 21 days: the line reads the latest
        # weigh-in's fitted value, without the 28-day mean's lag (#3551).
        days = [DAY - timedelta(days=n) for n in range(0, 21, 3)]
        weights = [weigh(d, 80.0 + 0.1 * (DAY - d).days) for d in days]
        # Wahoo's stored profile weight never counts (#3557).
        weights.append(weigh(DAY, 90.0, WAHOO))
        body = read_current_body(Reader(weight=weights), object(), DAY, skinfolds())
        self.assertAlmostEqual(80.0, body.weight_kg, places=6)
        alone = read_current_body(
            Reader(weight=[weigh(DAY, 90.0, WAHOO)]), object(), DAY, skinfolds()
        )
        self.assertIsNone(alone.weight_kg)

    def test_the_body_fat_is_skinfold_then_entered_then_device(self) -> None:
        fats = [weigh(DAY - timedelta(days=2), 16.0, None), weigh(DAY, 18.0, GARMIN)]
        reader = Reader(body_fat=fats)
        folded = read_current_body(
            reader, object(), DAY, skinfolds(DAY - timedelta(days=30))
        )
        self.assertEqual(
            ("skinfold", "Hautfalten", DAY - timedelta(days=30), 14.0),
            (folded.body_fat["source"], folded.body_fat["label"],
             folded.body_fat["measured_on"], folded.body_fat["body_fat_pct"]),
        )  # fmt: skip
        entered = read_current_body(reader, object(), DAY, skinfolds())
        self.assertEqual(("entered", "Lokal", 16.0), (
            entered.body_fat["source"], entered.body_fat["label"],
            entered.body_fat["body_fat_pct"]))  # fmt: skip
        device = read_current_body(
            Reader(body_fat=[weigh(DAY, 18.0, GARMIN)]), object(), DAY, skinfolds()
        )
        self.assertEqual(("device", 18.0), (
            device.body_fat["source"], device.body_fat["body_fat_pct"]))  # fmt: skip
        # Older than 90 days: no body fat, never a guess.
        old = read_current_body(
            Reader(body_fat=[weigh(DAY - timedelta(days=91), 18.0, None)]),
            object(), DAY, skinfolds(),
        )  # fmt: skip
        self.assertIsNone(old.body_fat)


if __name__ == "__main__":
    unittest.main()

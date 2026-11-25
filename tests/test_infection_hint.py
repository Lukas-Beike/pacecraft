"""The early infection hint (M14, ``infection-hint-v2``, #3762) on synthetic nights.

Each baseline alternates two values over 20 earlier nights, so by the inclusive
quartile method the lower quartile is the smaller value, the upper quartile the
larger one and the median their mean (worked by hand, see each table).
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date, timedelta
from types import SimpleNamespace

from canonical_recovery_support import RecordingReader, recovery_observation

from backend.canonical.records import MeasurementContext, Observation
from backend.canonical.vocabulary import MetricKind
from backend.performance.canonical_recovery import CanonicalRecoveryInputs
from backend.performance.infection_hint import infection_hint, read_shown_hint

TODAY = date(2026, 10, 30)
# Q1, Q3, median: breathing 14 / 15 / 14.5, resting HR 48 / 50 / 49, HRV 60 / 70 / 65.
# Clearly unusual: breathing > 15.5 and >= 15.5, resting HR > 51 and >= 52,
# HRV < 55 and <= 58.5.
BASE = {
    MetricKind.RESPIRATION_RATE: (14.0, 15.0),
    MetricKind.RESTING_HR: (48.0, 50.0),
    MetricKind.HRV_RMSSD: (60.0, 70.0),
}


def _night(kind: MetricKind, value: float, day: date) -> Observation:
    return recovery_observation(kind, value, day.isoformat())


def _history(kind: MetricKind, pair: tuple[float, float]) -> list[Observation]:
    return [
        _night(kind, pair[offset % 2], TODAY - timedelta(days=offset))
        for offset in range(1, 21)
    ]


def _inputs(
    tonight: dict[MetricKind, float], base: dict | None = None
) -> CanonicalRecoveryInputs:
    observations = [
        night for kind, pair in (base or BASE).items() for night in _history(kind, pair)
    ]
    observations += [_night(kind, value, TODAY) for kind, value in tonight.items()]
    by_kind: dict[MetricKind, list[Observation]] = {}
    for observation in observations:
        by_kind.setdefault(observation.kind, []).append(observation)
    return CanonicalRecoveryInputs(
        {kind: tuple(items) for kind, items in by_kind.items()}
    )


def _unusual(hint: dict) -> dict[str, bool]:
    return {signal["metric"]: signal["unusual"] for signal in hint["signals"]}


class InfectionHintTests(unittest.TestCase):
    def test_two_of_three_unusual_raise_the_hint(self) -> None:
        hint = infection_hint(
            _inputs(
                {
                    MetricKind.RESPIRATION_RATE: 16.0,
                    MetricKind.RESTING_HR: 53.0,
                    MetricKind.HRV_RMSSD: 64.0,
                }
            ),
            TODAY,
        )
        self.assertEqual("hint", hint["status"])
        self.assertEqual("infection-hint-v2", hint["method"])
        self.assertTrue(hint["derived"])
        self.assertEqual(
            {"respiration": True, "resting_hr": True, "hrv": False}, _unusual(hint)
        )
        breathing = hint["signals"][0]
        self.assertEqual(
            (16.0, "brpm", 14.0, 15.0, 14.5),
            tuple(
                breathing[key] for key in ("value", "unit", "lower", "upper", "median")
            ),
        )
        self.assertIn("Garmin", breathing["source"])
        self.assertIn("keine Diagnose", hint["limits"])

    def test_low_hrv_counts_in_its_own_direction(self) -> None:
        hint = infection_hint(
            _inputs(
                {
                    MetricKind.RESPIRATION_RATE: 14.5,
                    MetricKind.RESTING_HR: 53.0,
                    MetricKind.HRV_RMSSD: 50.0,
                }
            ),
            TODAY,
        )
        self.assertEqual("hint", hint["status"])
        self.assertEqual(
            {"respiration": False, "resting_hr": True, "hrv": True}, _unusual(hint)
        )

    def test_normal_night_and_favourable_direction_raise_nothing(self) -> None:
        for tonight in (
            {
                MetricKind.RESPIRATION_RATE: 14.5,
                MetricKind.RESTING_HR: 49.0,
                MetricKind.HRV_RMSSD: 65.0,
            },
            # Far outside the range, but lower breathing and heart rate and a
            # higher HRV are favourable.
            {
                MetricKind.RESPIRATION_RATE: 12.0,
                MetricKind.RESTING_HR: 44.0,
                MetricKind.HRV_RMSSD: 90.0,
            },
        ):
            with self.subTest(tonight=tonight):
                hint = infection_hint(_inputs(tonight), TODAY)
                self.assertEqual("none", hint["status"])

    def test_fewer_than_two_known_values_say_nothing(self) -> None:
        # Resting HR and HRV were last measured yesterday: unknown, not normal.
        hint = infection_hint(_inputs({MetricKind.RESPIRATION_RATE: 17.0}), TODAY)
        self.assertEqual("unknown", hint["status"])
        self.assertEqual(["respiration"], [s["metric"] for s in hint["signals"]])
        # No breathing-rate series at all, one unusual value: still unknown.
        base = {k: v for k, v in BASE.items() if k is not MetricKind.RESPIRATION_RATE}
        hint = infection_hint(_inputs({MetricKind.RESTING_HR: 55.0}, base), TODAY)
        self.assertEqual("unknown", hint["status"])

    def test_missing_value_never_counts_as_unusual(self) -> None:
        hint = infection_hint(
            _inputs({MetricKind.RESPIRATION_RATE: 16.0, MetricKind.HRV_RMSSD: 64.0}),
            TODAY,
        )
        self.assertEqual("none", hint["status"])
        self.assertEqual({"respiration": True, "hrv": False}, _unusual(hint))

    def test_measuring_change_restarts_the_baseline(self) -> None:
        changed = MeasurementContext(device_model="Watch B")
        inputs = _inputs(
            {
                MetricKind.RESPIRATION_RATE: 16.0,
                MetricKind.RESTING_HR: 53.0,
                MetricKind.HRV_RMSSD: 64.0,
            }
        )
        # The breathing rate changed its measuring context 5 days ago: only 5
        # earlier nights since, below the 14 required, so it is unknown.
        recent = TODAY - timedelta(days=5)
        observations = dict(inputs.observations)
        observations[MetricKind.RESPIRATION_RATE] = tuple(
            replace(item, provenance=replace(item.provenance, context=changed))
            if item.observed_on >= recent
            else item
            for item in inputs.of(MetricKind.RESPIRATION_RATE)
        )
        hint = infection_hint(replace(inputs, observations=observations), TODAY)
        self.assertEqual("none", hint["status"])
        self.assertEqual({"resting_hr": True, "hrv": False}, _unusual(hint))

    def test_minimum_change_floor(self) -> None:
        # Narrow ranges: breathing Q1 14.0, Q3 14.2, median 14.1, so 14.4 is
        # beyond Q3 + 0.5 x IQR (14.3) and the floor needs 15.1. Resting HR
        # Q1 49, Q3 49.5, median 49.25: beyond 49.75, floor 52.25. HRV Q1 60,
        # Q3 61, median 60.5: below 59.5, floor 54.45.
        narrow = {
            MetricKind.RESPIRATION_RATE: (14.0, 14.2),
            MetricKind.RESTING_HR: (49.0, 49.5),
            MetricKind.HRV_RMSSD: (60.0, 61.0),
        }
        below_floor = {
            MetricKind.RESPIRATION_RATE: 14.8,
            MetricKind.RESTING_HR: 52.0,
            MetricKind.HRV_RMSSD: 55.0,
        }
        hint = infection_hint(_inputs(below_floor, narrow), TODAY)
        self.assertEqual("none", hint["status"])
        self.assertEqual(
            {"respiration": False, "resting_hr": False, "hrv": False}, _unusual(hint)
        )
        at_floor = {
            MetricKind.RESPIRATION_RATE: 15.1,
            MetricKind.RESTING_HR: 52.25,
            MetricKind.HRV_RMSSD: 54.45,
        }
        hint = infection_hint(_inputs(at_floor, narrow), TODAY)
        self.assertEqual("hint", hint["status"])
        self.assertEqual(
            {"respiration": True, "resting_hr": True, "hrv": True}, _unusual(hint)
        )

    def test_spo2_is_not_used(self) -> None:
        inputs = _inputs(
            {
                MetricKind.RESPIRATION_RATE: 16.0,
                MetricKind.RESTING_HR: 49.0,
                MetricKind.HRV_RMSSD: 64.0,
            }
        )
        spo2 = tuple(
            _night(MetricKind.SPO2, 97.0 if offset else 85.0, TODAY - timedelta(offset))
            for offset in range(21)
        )
        hint = infection_hint(
            replace(
                inputs, observations={**inputs.observations, MetricKind.SPO2: spo2}
            ),
            TODAY,
        )
        self.assertEqual("none", hint["status"])
        self.assertNotIn("spo2", _unusual(hint))

    def test_mir_gehts_gut_hides_the_hint_for_its_day_only(self) -> None:
        # #3821: the answer is stored as the day (JSON); a later day's hint shows again.
        inputs = _inputs(
            {MetricKind.RESPIRATION_RATE: 16.0, MetricKind.RESTING_HR: 53.0}
        )
        reader = RecordingReader({k: list(v) for k, v in inputs.observations.items()})
        for stored, shown in (
            (None, True),
            ('"2026-10-30"', False),
            ('"2026-10-29"', True),
        ):
            with self.subTest(stored=stored):
                key_values = SimpleNamespace(
                    get=lambda db, key, stored=stored: (
                        stored if key == "infection_hint_dismissed" else None
                    )
                )
                hint = read_shown_hint(reader, "db", TODAY, key_values)
                self.assertEqual(shown, hint is not None)


if __name__ == "__main__":
    unittest.main()

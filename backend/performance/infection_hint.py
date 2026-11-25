"""Early infection hint from last night's values (M14, ``infection-hint-v2``, #3762).

Three Garmin night values, each compared with the athlete's own earlier nights
through the M2 comparison (``personal-quartiles-v1``: 42 days, at least 14
earlier nights, restart after a measuring change): the breathing rate during
sleep, the resting heart rate and the overnight HRV (RMSSD). SpO2 is not used.

A value is clearly unusual only in its unfavourable direction: breathing rate
and resting heart rate above the upper quartile plus half the interquartile
range and at least 1 breath/min or 3 beats/min above the median; HRV below the
lower quartile minus half the interquartile range and at least 10 % below the
median. Only last night's value counts; an older or missing value, or one
without a usable baseline, is unknown. The hint shows when at least 2 of the 3
are clearly unusual; with fewer than 2 known values it says nothing.

The hint never blocks or changes a plan; only the athlete's own illness
check-in starts the illness pause. Computed on read, nothing is stored.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from typing import Any, Final

from backend.athlete.checkins import INFECTION_HINT_DISMISSED_KEY
from backend.canonical.vocabulary import MetricKind
from backend.performance.canonical_inputs import resolve_reader
from backend.performance.canonical_recovery import (
    CanonicalRecoveryInputs,
    read_recovery_inputs,
)
from backend.performance.personal_recovery import series_baseline
from backend.runtime.ports import ObservationReader

METHOD: Final = "infection-hint-v2"
_USABLE: Final = {"ok", "provisional"}
# Beyond the usual range by this share of its width (the interquartile range).
RANGE_MARGIN: Final = 0.5
# (kind, metric, measurement, label, direction, minimum change); the HRV
# minimum is a share of its median.
SIGNALS: Final = (
    (MetricKind.RESPIRATION_RATE, "respiration", "sleep", "Atemfrequenz", 1, 1.0),
    (MetricKind.RESTING_HR, "resting_hr", "resting", "Ruheherzfrequenz", 1, 3.0),
    (MetricKind.HRV_RMSSD, "hrv", "RMSSD", "HRV", -1, 0.10),
)
MIN_UNUSUAL: Final = 2
LIMITS: Final = (
    "Abgeleiteter Hinweis, keine Diagnose. Hartes Training, Alkohol, Hitze, "
    "Höhe, Reisen und kurzer Schlaf verändern dieselben Werte."
)


def _unusual(baseline: dict[str, Any], direction: int, floor: float) -> bool:
    value, median = baseline["value"], baseline["median"]
    lower, upper = baseline["lower"], baseline["upper"]
    margin = RANGE_MARGIN * (upper - lower)
    if direction > 0:
        return value > upper + margin and round(value - median, 6) >= floor
    return value < lower - margin and round(median - value, 6) >= round(
        floor * median, 6
    )


def infection_hint(recovery: CanonicalRecoveryInputs, today: date) -> dict[str, Any]:
    """Last night's values against their baselines and whether they raise the hint."""
    signals = []
    for kind, metric, measurement, label, direction, floor in SIGNALS:
        baseline = series_baseline(recovery, kind, metric, measurement, today)
        if (
            baseline is None
            or baseline["observed_at"] != today.isoformat()
            or baseline["status"] not in _USABLE
        ):
            continue
        signals.append(
            {
                "metric": metric,
                "label": label,
                "source": baseline["source"],
                "value": baseline["value"],
                "unit": baseline["unit"],
                "median": baseline["median"],
                "lower": baseline["lower"],
                "upper": baseline["upper"],
                "unusual": _unusual(baseline, direction, floor),
            }
        )
    unusual = sum(signal["unusual"] for signal in signals)
    status = "unknown"
    if len(signals) >= MIN_UNUSUAL:
        status = "hint" if unusual >= MIN_UNUSUAL else "none"
    return {
        "method": METHOD,
        "as_of": today.isoformat(),
        "status": status,
        "derived": True,
        "signals": signals,
        "limits": LIMITS,
    }


def read_shown_hint(
    reader: ObservationReader, db: Any, today: date, key_values: Any
) -> dict | None:
    """Read the three night kinds once; the hint when it shows, else None.

    A normal or unknown night gives no note, nor a day the athlete answered with
    "Mir geht's gut" (#3821). ``reader`` must already be resolved.
    """
    dismissed = key_values.get(db, INFECTION_HINT_DISMISSED_KEY)
    if dismissed and json.loads(dismissed) == today.isoformat():
        return None
    kinds = tuple(signal[0] for signal in SIGNALS)
    hint = infection_hint(
        read_recovery_inputs(reader, db, as_of=today, kinds=kinds), today
    )
    return hint if hint["status"] == "hint" else None


def infection_hint_reader(
    database_manager: Any,
    key_values: Any,
    observation_reader: Callable[[], ObservationReader],
    today: Callable[[], date],
) -> Callable[[], dict[str, Any] | None]:
    """A reader of today's shown hint in its own read transaction."""

    def read() -> dict[str, Any] | None:
        day = today()
        with database_manager.reader() as db:
            return read_shown_hint(
                resolve_reader(observation_reader(), db), db, day, key_values
            )

    return read

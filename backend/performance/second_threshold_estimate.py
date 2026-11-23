"""Local LT2 estimates beside Garmin's: DFA-a1 at 0.5 and critical speed or power (#3273).

DFA-a1: each chest-strap run whose stored window series (method 3) crosses
alpha1 0.5 gives a heart rate. Only windows at or below 0.75 enter that line,
so the easy part of a run does not place the hard crossing; the windows must
reach 0.40 and 0.60, the line must fall, and the crossing must lie inside the
observed heart rates, above the same run's 0.75 crossing, and not only in the
last quarter of the run (late drift). A run qualifies when it is good by the
first threshold's rules. The athlete-level value is the median in bpm over at
least three qualifying runs of 60 days (a percent of the threshold heart rate
would move with the very value adopted from it); it is good with five runs whose
spread is at most 5 % of the median.

Critical speed or power: the M4 fit over the best efforts of 42 days (athlete
decision F5), unchanged; good when every effort lies within 1.5 % of the model
line and leaving any one out moves CS or CP by at most 3 % (F6).

Every value is an estimate for the athlete to review and adopt; nothing here
writes. Missing data gives an unknown value with its reason, never a number.

Pure; stdlib and canonical records only.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Final

from backend.canonical.activity_detail import ActivityDfaAlpha1
from backend.canonical.dfa_alpha1 import (
    GOOD_MAX_ARTIFACT_SHARE,
    GOOD_MIN_R_SQUARED,
    GOOD_MIN_WINDOWS,
    MIN_HR_SPREAD_BPM,
    MIN_WINDOWS,
    THRESHOLD_ALPHA1,
)
from backend.canonical.vocabulary import DfaQuality

SECOND_ALPHA1: Final = 0.5
# The windows must reach this far below and above 0.5, as 0.65 and 0.85 bracket 0.75.
SECOND_MARGIN: Final = 0.1
# Windows below 0.5 only in this last share of a run are drift, not a crossing.
LATE_SHARE: Final = 0.25
ESTIMATE_DAYS: Final = 60
MIN_SESSIONS: Final = 3
GOOD_MIN_SESSIONS: Final = 5
# Spread of the qualifying runs in percent of their median (F2: as for LT1).
GOOD_MAX_SPREAD_PCT: Final = 5.0
BOUNDS_BPM: Final = (60.0, 220.0)
# The 0.5 crossing against gas-exchange VT2 on a treadmill: limits of agreement of
# the heart rate in bpm (Rogers et al. 2021, R76; 15 men, group error, not an
# individual interval). Shown at adoption (#3920, C7).
DFA_LIMITS_OF_AGREEMENT_BPM: Final = (-24, 16)
CRITICAL_WINDOW_DAYS: Final = 42
GOOD_MAX_RESIDUAL: Final = 0.015
GOOD_MAX_LEAVE_ONE_OUT: Final = 0.03
DFA_LABEL: Final = "Schätzung aus Brustgurt-RR-Intervallen (DFA-α1 0,5, Forschungsstand), kein Laborwert"
CRITICAL_LABEL: Final = "Modellschätzung aus Bestwerten (Critical Speed/Power, M4)"


@dataclass(frozen=True, slots=True)
class SessionSecondThreshold:
    """One run's 0.5 crossing in bpm on its local day, with its own quality."""

    day: date
    bpm: float
    quality: DfaQuality


def session_second_threshold(
    result: ActivityDfaAlpha1, day: date, *, late_drift: bool = True
) -> SessionSecondThreshold | None:
    """The heart rate where one run's windows at or below 0.75 reach 0.5, or None.

    ``late_drift`` False drops the late-drift rule, for a step test whose hardest
    stages come last by design (#3833).
    """
    if not result.series_evaluated:
        return None
    windows = [w for w in result.series if w.alpha1 <= THRESHOLD_ALPHA1]
    if len(windows) < MIN_WINDOWS:
        return None
    rates = [w.heart_rate_bpm for w in windows]
    exponents = [w.alpha1 for w in windows]
    if max(rates) - min(rates) < MIN_HR_SPREAD_BPM:
        return None
    if (
        min(exponents) > SECOND_ALPHA1 - SECOND_MARGIN
        or max(exponents) < SECOND_ALPHA1 + SECOND_MARGIN
    ):
        return None
    try:
        slope, intercept = statistics.linear_regression(rates, exponents)
        r_squared = statistics.correlation(rates, exponents) ** 2
    except statistics.StatisticsError:
        return None
    if slope >= 0.0:
        return None
    estimate = (SECOND_ALPHA1 - intercept) / slope
    if not (min(rates) <= estimate <= max(rates)):
        return None
    if not (BOUNDS_BPM[0] <= estimate <= BOUNDS_BPM[1]):
        return None
    if result.estimate_bpm is not None and estimate <= result.estimate_bpm:
        return None
    end = max(w.start_s for w in result.series)
    below = [w for w in windows if w.alpha1 < SECOND_ALPHA1]
    if (
        late_drift
        and end > 0
        and all(w.start_s >= (1 - LATE_SHARE) * end for w in below)
    ):
        return None
    good = (
        len(windows) >= GOOD_MIN_WINDOWS
        and result.artifact_share <= GOOD_MAX_ARTIFACT_SHARE
        and r_squared >= GOOD_MIN_R_SQUARED
    )
    return SessionSecondThreshold(
        day, round(estimate, 1), DfaQuality.GOOD if good else DfaQuality.LOW
    )


def dfa_second_threshold(
    results: Iterable[tuple[date, ActivityDfaAlpha1]],
    today: date,
    *,
    first_threshold_bpm: float | None = None,
    max_hr: float | None = None,
) -> dict[str, Any]:
    """The athlete-level DFA-a1 LT2 heart rate of the last 60 days, or unknown.

    ``results`` are one stored result per run session. The value must lie above
    the LT1 heart rate and below the maximum heart rate in effect when known.
    """
    since = today - timedelta(days=ESTIMATE_DAYS - 1)
    in_window = [(day, item) for day, item in results if since <= day <= today]
    not_evaluated = sum(1 for _, item in in_window if not item.series_evaluated)
    found = [session_second_threshold(item, day) for day, item in in_window]
    values = [
        (item.day, item.bpm)
        for item in found
        if item is not None and item.quality is DfaQuality.GOOD
    ]
    if len(values) < MIN_SESSIONS:
        pending = f" ({not_evaluated} noch nicht ausgewertet)" if not_evaluated else ""
        return _unknown(
            f"nur {len(values)} von {MIN_SESSIONS} nötigen Läufen mit Brustgurt "
            f"in {ESTIMATE_DAYS} Tagen{pending}",
            len(values),
        )
    bpms = [bpm for _, bpm in values]
    median = statistics.median(bpms)
    if first_threshold_bpm is not None and median <= first_threshold_bpm:
        return _unknown("liegt nicht über der ersten Schwelle", len(values))
    if max_hr is not None and median >= max_hr:
        return _unknown("liegt nicht unter dem Maximalpuls", len(values))
    spread = (max(bpms) - min(bpms)) / median * 100
    good = len(values) >= GOOD_MIN_SESSIONS and spread <= GOOD_MAX_SPREAD_PCT
    return {
        "status": "ok",
        "heart_rate_bpm": math.floor(median + 0.5),
        "sessions": len(values),
        "window_days": ESTIMATE_DAYS,
        "from": min(day for day, _ in values).isoformat(),
        "to": max(day for day, _ in values).isoformat(),
        "spread_pct": round(spread, 1),
        "quality": (DfaQuality.GOOD if good else DfaQuality.LOW).value,
        "source": DFA_LABEL,
        "range": {
            "heart_rate_bpm": list(DFA_LIMITS_OF_AGREEMENT_BPM),
            "basis": "Limits of Agreement gegen VT2, Rogers et al. 2021 (R76)",
        },
    }


def critical_second_threshold(
    fits: Sequence[Mapping[str, Any]], sport: str
) -> dict[str, Any]:
    """CS pace (s/km) for runs or CP (W) for rides from the 42-day M4 fits, or unknown.

    Rides take the outdoor fit, else the indoor one.
    """
    candidates = [fit for fit in fits if fit.get("sport") == sport]
    candidates.sort(key=lambda fit: fit.get("environment") != "outdoor")
    for fit in candidates:
        if fit.get("status") == "ok":
            return _critical_value(fit, sport)
    reason = next(
        (str(fit["reason"]) for fit in candidates if fit.get("reason")),
        "keine Modellschätzung",
    )
    return _unknown(reason, 0)


def _critical_value(fit: Mapping[str, Any], sport: str) -> dict[str, Any]:
    key = "cs_mps" if sport == "run" else "cp_watts"
    value = float(fit[key])
    loo = [
        float(item[key]) for item in fit.get("sensitivity", {}).get("leave_one_out", [])
    ]
    residuals = [float(item["relative"]) for item in fit.get("residuals", [])]
    good = (
        bool(residuals)
        and max(residuals) <= GOOD_MAX_RESIDUAL
        and all(abs(item - value) / value <= GOOD_MAX_LEAVE_ONE_OUT for item in loo)
    )
    result: dict[str, Any] = {
        "status": "ok",
        "environment": fit.get("environment"),
        "durations_s": [
            int(effort["duration_seconds"]) for effort in fit.get("efforts", [])
        ],
        "window_days": CRITICAL_WINDOW_DAYS,
        "quality": (DfaQuality.GOOD if good else DfaQuality.LOW).value,
        "source": CRITICAL_LABEL,
    }
    if sport == "run":
        result["pace_seconds_per_km"] = round(1000 / value)
    else:
        result["power_w"] = round(value)
    return result


def _unknown(reason: str, sessions: int) -> dict[str, Any]:
    return {"status": "unknown", "reason": reason, "sessions": sessions}

"""Running step test for both DFA-a1 thresholds (card M10, protocol P1, #3833).

Protocol: 10 min warm-up slower than stage 1, then 3-minute stages with pace
targets, each 0.5 km/h faster. Stage 1 lies about 1.5 km/h below the athlete's
current LT1 pace (9.5 km/h without one); the stages run to at least two stages
beyond the current LT2 pace, at most 10 (at least 6, the stages a "good" result
needs). The run ends when a stage can no longer be held, then 10 min cool-down.

Evaluation: the run's stored M10 window series (``dfa_alpha1``). LT1 is the
stored 0.75 crossing of the run, LT2 the 0.5 crossing of
``session_second_threshold`` without its late-drift rule (a step test is hard
at its end by design). Each value takes the pace at its heart rate from the
run's stage laps. A value is good when M10 grades it good and at least 6 stages
hold an analysed window; only a good value is offered for adoption. "Not
reached": alpha1 never fell below 0.75 (LT1) or never reached 0.5 (LT2).

Pure; stdlib and canonical records only.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date
from itertools import pairwise
from typing import Any, Final

from backend.canonical.activity_detail import ActivityDfaAlpha1
from backend.canonical.dfa_alpha1 import THRESHOLD_ALPHA1, WINDOW_S
from backend.canonical.vocabulary import DfaQuality
from backend.performance.second_threshold_estimate import (
    SECOND_ALPHA1,
    session_second_threshold,
)

PROTOCOL: Final = "run_step_test"
NAME: Final = "Stufentest Laufen (LT1 und LT2)"
WARMUP_MIN: Final = 10
COOLDOWN_MIN: Final = 10
STAGE_MIN: Final = 3
STEP_KMH: Final = 0.5
BELOW_LT1_KMH: Final = 1.5
FALLBACK_START_KMH: Final = 9.5
STAGES_BEYOND_LT2: Final = 2
MAX_STAGES: Final = 10
MIN_GOOD_STAGES: Final = 6
# The warm-up and cool-down range lies this far below stage 1 (km/h).
EASY_RANGE_KMH: Final = (1.5, 0.5)
# Each stage's pace band, ±3 % around its pace (athlete decision, #3833).
STAGE_PACE_BAND: Final = 0.03
# Plausible run paces (s/km); another value counts as unknown.
PACE_BOUNDS_S: Final = (150.0, 540.0)
# A recorded lap of this length (s) is one stage; a shorter last one was not held.
STAGE_LAP_S: Final = (150.0, 210.0)
END_NOTE: Final = (
    "Ende: wenn die Stufe nicht mehr zu halten ist, spätestens nach Stufe {last}."
)
SOURCE: Final = "abgeleitet"
LT1_NOT_REACHED: Final = "α1 blieb über 0,75. Die Stufen waren nicht schnell genug."
LT2_NOT_REACHED: Final = (
    "α1 blieb über 0,5. Die letzten Stufen waren nicht schnell genug "
    "oder wurden abgebrochen."
)
NO_CROSSING: Final = "keine klare Kreuzung"


def _speed_kmh(pace_s_per_km: object) -> float | None:
    if isinstance(pace_s_per_km, bool) or not isinstance(pace_s_per_km, int | float):
        return None
    if not PACE_BOUNDS_S[0] <= pace_s_per_km <= PACE_BOUNDS_S[1]:
        return None
    return 3600.0 / pace_s_per_km


def _pace(kmh: float) -> str:
    seconds = round(3600 / kmh)
    return f"{seconds // 60}:{seconds % 60:02d}/km"


def _stage_band(kmh: float) -> str:
    """The stage's pace band, faster end first: "6:08-6:30/km" for 6:19."""
    fast = _pace(kmh / (1 - STAGE_PACE_BAND)).removesuffix("/km")
    return f"{fast}-{_pace(kmh / (1 + STAGE_PACE_BAND))}"


def stage_speeds(lt1_pace: object, lt2_pace: object) -> list[float]:
    """The stage speeds in km/h from the LT1 and LT2 paces (s/km), or the fallback."""
    lt1 = _speed_kmh(lt1_pace)
    lt2 = _speed_kmh(lt2_pace)
    start = round(lt1 - BELOW_LT1_KMH, 1) if lt1 is not None else FALLBACK_START_KMH
    speeds = [round(start + STEP_KMH * index, 2) for index in range(MAX_STAGES)]
    if lt2 is None:
        return speeds
    reached = next(
        (index for index, speed in enumerate(speeds) if speed >= lt2 - 1e-9), None
    )
    if reached is None:
        return speeds
    count = min(MAX_STAGES, max(MIN_GOOD_STAGES, reached + 1 + STAGES_BEYOND_LT2))
    return speeds[:count]


def protocol_template(lt1_pace: object, lt2_pace: object) -> dict[str, Any]:
    """The step test as a local library template (description in workout syntax)."""
    speeds = stage_speeds(lt1_pace, lt2_pace)
    easy_low, easy_high = (speeds[0] - delta for delta in EASY_RANGE_KMH)
    easy = f"{_pace(easy_high).removesuffix('/km')}-{_pace(easy_low)} Pace"
    lines = [
        f"- {WARMUP_MIN}m {easy}",
        *(f"- {STAGE_MIN}m {_stage_band(speed)} Pace" for speed in speeds),
        f"- {COOLDOWN_MIN}m {easy}",
        "",
        END_NOTE.format(last=len(speeds)),
    ]
    return {
        "name": NAME,
        "sport": "Run",
        "target": "PACE",
        "duration_minutes": WARMUP_MIN + STAGE_MIN * len(speeds) + COOLDOWN_MIN,
        "description": "\n".join(lines),
        "session_focus": "test",
        "test_protocol": PROTOCOL,
    }


def _stages(laps: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The recorded stage laps: about 3 minutes long with heart rate and speed."""
    stages = []
    for lap in laps:
        try:
            length = float(lap["end_offset_s"]) - float(lap["start_offset_s"])
        except KeyError, TypeError, ValueError:
            continue
        if (
            STAGE_LAP_S[0] <= length <= STAGE_LAP_S[1]
            and lap.get("kind") not in ("warmup", "cooldown", "recovery")
            and isinstance(lap.get("avg_hr"), int | float)
            and isinstance(lap.get("avg_speed_mps"), int | float)
            and lap["avg_speed_mps"] > 0
        ):
            stages.append(lap)
    return stages


def _pace_at(stages: Sequence[Mapping[str, Any]], bpm: float) -> int | None:
    """Pace (s/km) at ``bpm`` between the two stages around it; None outside them."""
    points = sorted((float(s["avg_hr"]), float(s["avg_speed_mps"])) for s in stages)
    for (low_hr, low_v), (high_hr, high_v) in pairwise(points):
        if low_hr <= bpm <= high_hr:
            share = 0.0 if high_hr == low_hr else (bpm - low_hr) / (high_hr - low_hr)
            return round(1000 / (low_v + share * (high_v - low_v)))
    return None


def _value(
    bpm: float, quality: DfaQuality, stages: Sequence[Mapping[str, Any]], good: bool
) -> dict[str, Any]:
    return {
        "status": "ok",
        "heart_rate_bpm": math.floor(bpm + 0.5),
        "pace_seconds_per_km": _pace_at(stages, bpm),
        "quality": (
            DfaQuality.GOOD if good and quality is DfaQuality.GOOD else DfaQuality.LOW
        ).value,
    }


def evaluate(
    result: ActivityDfaAlpha1 | None,
    laps: Sequence[Mapping[str, Any]],
    day: date,
) -> dict[str, Any]:
    """LT1 and LT2 of one step-test run, or ``status`` ``no_rr`` without a series."""
    stages = _stages(laps)
    if result is None or not result.series_evaluated:
        return {"protocol": PROTOCOL, "status": "no_rr", "stages": len(stages)}
    analysable = sum(
        1
        for stage in stages
        if any(
            float(stage["start_offset_s"])
            <= window.start_s + WINDOW_S / 2
            < float(stage["end_offset_s"])
            for window in result.series
        )
    )
    good = analysable >= MIN_GOOD_STAGES
    lowest = min(window.alpha1 for window in result.series)
    lt1: dict[str, Any]
    if lowest > THRESHOLD_ALPHA1:
        lt1 = {"status": "not_reached", "reason": LT1_NOT_REACHED}
    elif result.estimate_bpm is None or result.quality is None:
        lt1 = {"status": "unknown", "reason": NO_CROSSING}
    else:
        lt1 = _value(result.estimate_bpm, result.quality, stages, good)
    second = session_second_threshold(result, day, late_drift=False)
    lt2: dict[str, Any]
    if lowest > SECOND_ALPHA1:
        lt2 = {"status": "not_reached", "reason": LT2_NOT_REACHED}
    elif second is None:
        lt2 = {"status": "unknown", "reason": NO_CROSSING}
    else:
        lt2 = _value(second.bpm, second.quality, stages, good)
    return {
        "protocol": PROTOCOL,
        "status": "ok",
        "lt1": lt1,
        "lt2": lt2,
        "stages": len(stages),
        "analysable_stages": analysable,
        "windows": result.windows,
        "artifact_share_pct": round(result.artifact_share * 100, 1),
        "source": SOURCE,
    }


def template_ids(templates: Sequence[Mapping[str, Any]]) -> frozenset[str]:
    """The ids of the library templates that hold the step test."""
    return frozenset(
        str(item.get("id"))
        for item in templates
        if item.get("test_protocol") == PROTOCOL and item.get("id")
    )

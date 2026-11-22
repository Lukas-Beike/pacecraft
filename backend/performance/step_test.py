"""Running step test for both DFA-a1 thresholds (card M10, protocol P1, #3833).

Protocol: 10 min warm-up slower than stage 1, then 3-minute stages with pace
targets, each 0.5 km/h faster. Stage 1 lies about 1.5 km/h below the athlete's
current LT1 pace (9.5 km/h without one), at most 3.5 km/h below LT2; the stages run to at least two stages
beyond the current LT2 pace, at most 10 (at least 6, the stages a "good" result
needs). The run ends when a stage can no longer be held, then 10 min cool-down.

Evaluation: the run's stored M10 window series (``dfa_alpha1``), only the
windows whose midpoint lies in a stage lap, so warm-up and cool-down never
move a crossing (#3916). LT1 is the 0.75 crossing of the stage windows with
alpha1 from 0.5 to 1.0, LT2 the 0.5 crossing of ``session_second_threshold``
over those from 0.3 to 0.75, without its late-drift rule (a step test is hard
at its end by design). Each value takes the pace at its heart rate between the
stage laps, each lap's heart rate the mean of its last minute. A value is good when M10 grades it good and at least 6 stages
hold an analysed window; only a good value is offered for adoption. "Not
reached": alpha1 never fell below 0.75 (LT1) or never reached 0.5 (LT2).

Pure; stdlib and canonical records only.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date
from itertools import pairwise
from typing import Any, Final

from backend.canonical.activity_detail import ActivityDfaAlpha1
from backend.canonical.dfa_alpha1 import (
    MIN_WINDOWS,
    THRESHOLD_ALPHA1,
    WINDOW_S,
    crossing,
)
from backend.canonical.vocabulary import DfaQuality
from backend.performance.second_threshold_estimate import (
    SECOND_ALPHA1,
    session_second_threshold,
)

# v1 fitted the crossings over the whole run (#3833), v2 over the stage windows
# (#3916), v3 starts stage 1 at most 3.5 km/h below LT2 (#3915).
METHOD: Final = "run-step-test-v3"
PROTOCOL: Final = "run_step_test"
NAME: Final = "Stufentest Laufen (LT1 und LT2)"
WARMUP_MIN: Final = 10
COOLDOWN_MIN: Final = 10
STAGE_MIN: Final = 3
STEP_KMH: Final = 0.5
BELOW_LT1_KMH: Final = 1.5
# Stage 1 lies at most this far below LT2, so two stages lie beyond it (#3915).
BELOW_LT2_KMH: Final = 3.5
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
# The alpha1 range of the stage windows that place LT1 (#3916).
LT1_ALPHA1_RANGE: Final = (0.5, 1.0)
# The alpha1 range of the stage windows that place LT2 (#3916).
LT2_ALPHA1_RANGE: Final = (0.3, THRESHOLD_ALPHA1)
# A stage's heart rate is the mean over its last part (s), after the rise.
STAGE_HR_TAIL_S: Final = 60.0
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
    if lt2 is not None:
        start = max(start, round(lt2 - BELOW_LT2_KMH, 1))
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


def _stage_tail(stage: Mapping[str, Any], streams: object) -> float | None:
    """The mean heart rate of the stage's last minute, None without samples."""
    if not isinstance(streams, Mapping):
        return None
    end = float(stage["end_offset_s"])
    times = streams.get("time_s")
    channels = streams.get("channels")
    channel = channels.get("heart_rate") if isinstance(channels, Mapping) else None
    rates = channel.get("values") if isinstance(channel, Mapping) else None
    if not isinstance(times, list) or not isinstance(rates, list):
        return None
    tail = [
        float(rate)
        for time, rate in zip(times, rates, strict=False)
        if isinstance(time, int | float)
        and isinstance(rate, int | float)
        and not isinstance(rate, bool)
        and end - STAGE_HR_TAIL_S <= time < end
        and rate > 0
    ]
    return math.fsum(tail) / len(tail) if tail else None


def _stage_heart_rates(
    stages: Sequence[Mapping[str, Any]], streams: object
) -> list[float]:
    """Each stage's last-minute heart rate, or the lap means when any stage has none.

    One definition for all stages, so a lap mean never reorders the points.
    """
    tails = [_stage_tail(stage, streams) for stage in stages]
    if all(tail is not None for tail in tails):
        return [float(tail) for tail in tails if tail is not None]
    return [float(stage["avg_hr"]) for stage in stages]


def _pace_at(
    stages: Sequence[Mapping[str, Any]], bpm: float, streams: object
) -> int | None:
    """Pace (s/km) at ``bpm`` between the two stages around it; None outside them."""
    points = sorted(
        zip(
            _stage_heart_rates(stages, streams),
            (float(s["avg_speed_mps"]) for s in stages),
            strict=True,
        )
    )
    for (low_hr, low_v), (high_hr, high_v) in pairwise(points):
        if low_hr <= bpm <= high_hr:
            share = 0.0 if high_hr == low_hr else (bpm - low_hr) / (high_hr - low_hr)
            return round(1000 / (low_v + share * (high_v - low_v)))
    return None


def _value(
    bpm: float,
    quality: DfaQuality,
    stages: Sequence[Mapping[str, Any]],
    good: bool,
    streams: object,
) -> dict[str, Any]:
    return {
        "status": "ok",
        "heart_rate_bpm": math.floor(bpm + 0.5),
        "pace_seconds_per_km": _pace_at(stages, bpm, streams),
        "quality": (
            DfaQuality.GOOD if good and quality is DfaQuality.GOOD else DfaQuality.LOW
        ).value,
    }


def _in_stage(start_s: float, stage: Mapping[str, Any]) -> bool:
    """Whether the window starting at ``start_s`` has its midpoint in ``stage``."""
    middle = start_s + WINDOW_S / 2
    return float(stage["start_offset_s"]) <= middle < float(stage["end_offset_s"])


def _in_range(alpha1: float, bounds: tuple[float, float]) -> bool:
    return bounds[0] <= alpha1 <= bounds[1]


def evaluate(
    result: ActivityDfaAlpha1 | None,
    laps: Sequence[Mapping[str, Any]],
    day: date,
    streams: object = None,
) -> dict[str, Any]:
    """LT1 and LT2 of one step-test run, or ``status`` ``no_rr`` without a series.

    ``streams`` is the activity detail's stream set (``time_s`` and the
    ``heart_rate`` channel) on the laps' clock; without it each stage keeps its
    lap mean.
    """
    stages = _stages(laps)
    if result is None or not result.series_evaluated:
        return {"protocol": PROTOCOL, "status": "no_rr", "stages": len(stages)}
    analysable = sum(
        1
        for stage in stages
        if any(_in_stage(window.start_s, stage) for window in result.series)
    )
    in_stages = [
        window
        for window in result.series
        if any(_in_stage(window.start_s, stage) for stage in stages)
    ]
    good = analysable >= MIN_GOOD_STAGES
    # Without any stage window the whole run says whether alpha1 got there.
    lowest = min(window.alpha1 for window in in_stages or result.series)
    lt1_windows = [w for w in in_stages if _in_range(w.alpha1, LT1_ALPHA1_RANGE)]
    found = (
        crossing(lt1_windows, result.artifact_share)
        if len(lt1_windows) >= MIN_WINDOWS
        else None
    )
    lt1: dict[str, Any]
    if lowest > THRESHOLD_ALPHA1:
        lt1 = {"status": "not_reached", "reason": LT1_NOT_REACHED}
    elif found is None:
        lt1 = {"status": "unknown", "reason": NO_CROSSING}
    else:
        lt1 = _value(found[0], found[1], stages, good, streams)
    # The stage windows stand for the series, and LT2 must lie above this LT1,
    # else above the run's own stored crossing.
    staged = replace(
        result,
        series=tuple(w for w in in_stages if _in_range(w.alpha1, LT2_ALPHA1_RANGE)),
        estimate_bpm=result.estimate_bpm if found is None else found[0],
        quality=result.quality if found is None else found[1],
    )
    second = (
        session_second_threshold(staged, day, late_drift=False)
        if staged.series
        else None
    )
    lt2: dict[str, Any]
    if lowest > SECOND_ALPHA1:
        lt2 = {"status": "not_reached", "reason": LT2_NOT_REACHED}
    elif second is None:
        lt2 = {"status": "unknown", "reason": NO_CROSSING}
    else:
        lt2 = _value(second.bpm, second.quality, stages, good, streams)
    return {
        "protocol": PROTOCOL,
        "method": METHOD,
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

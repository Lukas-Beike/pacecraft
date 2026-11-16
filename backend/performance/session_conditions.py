"""Conditions of one analysed session (#3084 Part 3): grade-adjusted pace and RPE.

Pure functions over the ``get_activity_details`` result. Grade-adjusted pace
(GAP) is an estimate after Minetti et al. 2002 through ``grade_adjustment``; it
is labelled as one and never turns a verdict alone. RPE against intensity
compares the session's rating with the athlete's own rated sessions at the same
intensity. No I/O, inputs unchanged.
"""

from __future__ import annotations

import itertools
import math
import statistics
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any, Final

from backend.athlete.measurements import finite_float as _number
from backend.canonical.activity_summary import summary_is_indoor
from backend.performance.grade_adjustment import grade_adjusted_speeds, grade_stream

# Grade-adjusted pace: Minetti et al. 2002 (R44), the adjustment of M8 and pace at HR.
GAP_METHOD: Final = (
    "Minetti et al. 2002: Energiekosten des Laufens je Steigung "
    "(Schätzung aus den Streams der Einheit)"
)
# Samples below this speed are standing, not running (3.6 km/h).
MOVING_MPS: Final = 1.0
# A gap longer than max(30 s, 3 x the median step) is a stop, as in the halves.
STOP_S: Final = 30.0
# RPE against intensity (athlete decision on #3084, 2026-11-15; Foster session-RPE,
# R1): notable from this gap in points to the expected RPE.
RPE_GAP_POINTS: Final = 2.0
# The expected RPE is the median of own rated sessions of the same sport and RPE
# scale within +-0.05 IF of the session (orchestrator decision on #3084), in the
# stored scale: a summary's ``intensity`` is the local IF in % (M6, IF x 100,
# MetricKind.INTENSITY_FACTOR), so the window is 5 percentage points.
RPE_INTENSITY_WINDOW_PCT: Final = 5.0
# Own rated sessions needed for an expected RPE; fewer leave the check unknown.
MIN_RATED: Final = 5
# The rated sessions read for the expected RPE: the comparable-session window.
RATED_WINDOW_DAYS: Final = 84
# Wind (athlete decisions on #3084, 2026-11-16). The 10 m model wind is reduced
# to body height with the logarithmic wind profile u(z) ~ ln(z / z0) (Stull 1988,
# An Introduction to Boundary Layer Meteorology, ch. 9) for open flat land,
# z0 = 0.03 m (Davenport-Wieringa roughness class "open", WMO-No. 8, 2018,
# Vol. I, ch. 5, annex): factor about 0.72. In towns or forest the real wind is
# lower still.
WIND_BODY_HEIGHT_M: Final = 2.0
WIND_MODEL_HEIGHT_M: Final = 10.0
WIND_ROUGHNESS_M: Final = 0.03
WIND_BODY_FACTOR: Final = math.log(WIND_BODY_HEIGHT_M / WIND_ROUGHNESS_M) / math.log(
    WIND_MODEL_HEIGHT_M / WIND_ROUGHNESS_M
)
# Wind explains a slower part from this headwind at body height in that part...
WIND_EXPLAINS_MPS: Final = 3.0
# ...when that part had this much more headwind than the part it is compared with.
WIND_SECOND_SIGNAL_MPS: Final = 2.0
# A headwind needs a direction and a wind value for this share of the moving time.
WIND_MIN_COVERAGE_PCT: Final = 50.0
# The 3-day prior load (athlete, #3084 3c H): the local load of the 3 days before
# the session against the median of every 3-day sum in the 42 days before; facts.
PRIOR_LOAD_DAYS: Final = 3
PRIOR_LOAD_WINDOW_DAYS: Final = 42
# What the session report adds to the detail for these checks: the session's
# local IF, the device's grade-adjusted speed and the athlete's rated sessions.
INPUTS: Final = "condition_inputs"


def grade_adjusted_pace(result: Mapping[str, Any]) -> dict[str, Any] | None:
    """The run's grade-adjusted pace over its moving time, overall and per lap.

    None for another sport, an indoor run or without speed and grade or
    altitude streams. The device's own value, when stored, is passed as a
    labelled comparison only.
    """
    session = _mapping(result.get("session"))
    if session.get("sport") != "run" or summary_is_indoor(session):
        return None
    streams = _mapping(result.get("streams"))
    channels = _mapping(streams.get("channels"))
    times = [_number(value) for value in streams.get("time_s") or ()]
    view = {
        "time": times,
        "grade_smooth": _values(channels, "grade_pct"),
        "altitude": _values(channels, "altitude_m"),
        "distance": _values(channels, "distance_m"),
        "velocity_smooth": _values(channels, "speed_mps"),
    }
    grades = grade_stream(view)
    if grades is None or len(view["velocity_smooth"]) != len(times):
        return None
    adjusted = grade_adjusted_speeds(view, grades)
    steps = _steps(times, view["velocity_smooth"], adjusted)
    overall = _paces(steps, None)
    if overall is None:
        return None
    moving = sum(step[1] for step in steps if step[2] is not None)
    graded = sum(step[1] for step in steps if step[3] is not None)
    device = _number(_mapping(result.get(INPUTS)).get("grade_adjusted_speed_mps"))
    return {
        **overall,
        "coverage_pct": round(100 * graded / moving, 1) if moving else None,
        "laps": _lap_paces(result, steps),
        "device_gap_s_per_km": round(1000 / device, 1) if device else None,
        "method": GAP_METHOD,
        "estimate": True,
    }


def expected_rpe(
    intensity: float, sport: Any, scale: Any, rated: Sequence[Mapping[str, Any]]
) -> tuple[float | None, int]:
    """The median RPE of own rated sessions near ``intensity`` (IF in %), and their count.

    Only sessions of the same sport and RPE scale within
    ``RPE_INTENSITY_WINDOW_PCT`` count;
    with fewer than ``MIN_RATED`` the expectation is unknown.
    """
    near = [
        rpe
        for item in rated
        if item.get("sport") == sport
        and item.get("rpe_scale") == scale
        and (rpe := _number(item.get("rpe"))) is not None
        and (level := _number(item.get("intensity"))) is not None
        and abs(level - intensity) <= RPE_INTENSITY_WINDOW_PCT
    ]
    if len(near) < MIN_RATED:
        return None, len(near)
    return float(statistics.median(near)), len(near)


def body_headwind(
    wind: Mapping[str, Any], window: tuple[float, float] | None = None
) -> float | None:
    """The signed headwind at body height over ``window`` (stream s; None: all).

    A time-weighted mean of the stored bins, so tailwind offsets headwind; the
    whole session without a window reads the stored mean.
    """
    if window is None:
        mean = _number(wind.get("mean_mps"))
        return None if mean is None else round(mean * WIND_BODY_FACTOR, 2)
    width = _number(wind.get("bin_s")) or 0.0
    bins = wind.get("bins")
    if width <= 0 or not isinstance(bins, list):
        return None
    total = weight = 0.0
    for index, value in enumerate(bins):
        component = _number(value)
        start = index * width
        overlap = min(start + width, window[1]) - max(start, window[0])
        if component is None or overlap <= 0:
            continue
        total += component * overlap
        weight += overlap
    if weight <= 0:
        return None
    return round(total / weight * WIND_BODY_FACTOR, 2)


def prior_load(loads: Mapping[str, float], day: date) -> dict[str, Any] | None:
    """The 3-day load before ``day`` and the median 3-day sum of the 42 days before.

    ``loads`` maps ISO days to their local load; a day without one counts 0.
    None without any load in the window.
    """
    if not loads:
        return None

    def three_days(end: date) -> float:
        return sum(
            loads.get((end - timedelta(days=back)).isoformat(), 0.0)
            for back in range(PRIOR_LOAD_DAYS)
        )

    yesterday = day - timedelta(days=1)
    sums = [
        three_days(yesterday - timedelta(days=offset))
        for offset in range(PRIOR_LOAD_WINDOW_DAYS)
    ]
    return {
        "sum_3_days": round(three_days(yesterday), 1),
        "median_3_days_42": round(statistics.median(sums), 1),
        "window_days": PRIOR_LOAD_WINDOW_DAYS,
    }


def _steps(
    times: list[float | None],
    speeds: list[Any],
    adjusted: list[float | None],
) -> list[tuple[float, float, float | None, float | None]]:
    """(start time, seconds, speed, grade-adjusted speed) of each moving step."""
    deltas = [
        later - earlier
        for earlier, later in itertools.pairwise(times)
        if earlier is not None and later is not None and later > earlier
    ]
    if not deltas:
        return []
    stop = max(STOP_S, 3 * statistics.median(deltas))
    steps: list[tuple[float, float, float | None, float | None]] = []
    for index in range(len(times) - 1):
        start, end = times[index], times[index + 1]
        speed = _number(speeds[index])
        if start is None or end is None or not 0 < end - start <= stop:
            continue
        if speed is None or speed < MOVING_MPS:
            continue
        steps.append((start, end - start, speed, adjusted[index]))
    return steps


def _paces(
    steps: Sequence[tuple[float, float, float | None, float | None]],
    window: tuple[float, float] | None,
) -> dict[str, Any] | None:
    """Time-weighted pace and GAP of the steps graded in ``window`` (whole session: None)."""
    chosen = [
        (seconds, speed, adjusted)
        for start, seconds, speed, adjusted in steps
        if adjusted is not None
        and speed is not None
        and (window is None or window[0] <= start < window[1])
    ]
    total = sum(seconds for seconds, _, _ in chosen)
    if total <= 0:
        return None
    speed = sum(seconds * speed for seconds, speed, _ in chosen) / total
    gap = sum(seconds * adjusted for seconds, _, adjusted in chosen) / total
    return {
        "pace_s_per_km": round(1000 / speed, 1),
        "gap_s_per_km": round(1000 / gap, 1),
        # Positive: the terrain cost time, so GAP is faster than the pace.
        "difference_pct": round((speed / gap - 1) * -100, 1),
        "moving_s": round(total),
    }


def _lap_paces(
    result: Mapping[str, Any],
    steps: Sequence[tuple[float, float, float | None, float | None]],
) -> list[dict[str, Any]]:
    """Each lap's pace and GAP, when the laps share the stream clock."""
    laps = _mapping(result.get("laps"))
    if not laps.get("on_stream_clock"):
        return []
    paced = []
    for index, lap in enumerate(laps.get("laps") or ()):
        if not isinstance(lap, Mapping):
            continue
        start = _number(lap.get("start_offset_s"))
        end = _number(lap.get("end_offset_s"))
        if start is None or end is None or end <= start:
            continue
        paces = _paces(steps, (start, end))
        if paces is not None:
            paced.append({"lap": index + 1, **paces})
    return paced


def _values(channels: Mapping[str, Any], name: str) -> list[Any]:
    return list(_mapping(channels.get(name)).get("values") or ())


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}

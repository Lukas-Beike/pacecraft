"""Full-resolution session metrics with conservative eligibility rules."""

from __future__ import annotations

import math
import re
from collections import deque
from collections.abc import Sequence
from typing import Any, Final

from backend.activities.stream_zones import max_step_s
from backend.athlete.measurements import finite_float as _number
from backend.canonical.activity_summary import summary_is_indoor
from backend.performance.grade_adjustment import grade_adjusted_speeds, grade_stream
from backend.performance.session_execution import rounded_metric
from backend.providers.source_labels import SESSION_SOURCES_LABEL

TARGET_BASIS_FTP_KEY = "ftp"

# v2: Pa:HR of outdoor runs uses grade-adjusted speed (card M8 v2, #1770).
# v3: the steady-output rule reads the CV of 60 s rolling means (#1832).
METHOD = "time-weighted-session-analysis-v3"
GRADE_ADJUSTED_SPEED = "grade_adjusted_speed"
# Steady output (athlete decision 2026-11-14, #1832): the coefficient of
# variation of the output's 60 s rolling means is at most 15 % in each half,
# one rule for runs and rides; 1 s samples excluded almost every outdoor ride.
STEADY_WINDOW_S = 60.0
MAX_STEADY_CV = 0.15


def weighted_cv(series: Sequence[tuple[float, float]]) -> float | None:
    """Coefficient of variation of time-weighted ``(weight, value)`` pairs."""
    total = sum(weight for weight, _ in series)
    if total <= 0:
        return None
    mean = sum(weight * value for weight, value in series) / total
    if mean <= 0:
        return None
    variance = sum(weight * (value - mean) ** 2 for weight, value in series) / total
    return math.sqrt(variance) / mean


def rolling_means(
    samples: Sequence[tuple[float, float, float]], window: float
) -> list[tuple[float, float]]:
    """Time-weighted rolling means of ``(time, weight, value)`` samples.

    Each mean ends at a sample and is weighted by that sample's weight. A mean
    is kept only when the samples starting within ``window`` before it cover
    the full window, so the first ``window`` seconds and the window after an
    excluded gap (a stop, a dropout) yield none: a partial window would bring
    the second-to-second noise back.
    """
    result: list[tuple[float, float]] = []
    queue: deque[tuple[float, float, float]] = deque()
    weights = products = 0.0
    for sample in samples:
        stamp, weight, value = sample
        queue.append(sample)
        weights += weight
        products += weight * value
        while queue[0][0] <= stamp - window:
            _, old_weight, old_value = queue.popleft()
            weights -= old_weight
            products -= old_weight * old_value
        if weights >= window - 1e-6:
            result.append((weight, products / weights))
    return result


def _samples(
    activity: dict[str, Any], sensor: str, start: float, end: float
) -> list[tuple[float, float, float]]:
    streams = activity.get("streams") or {}
    times = streams.get("time") or []
    values = streams.get(sensor) or []
    moving = streams.get("moving")
    samples: list[tuple[float, float, float]] = []
    if len(values) != len(times):
        return samples
    max_step = max_step_s([float(time) for time in times])
    if max_step is None:
        return samples
    for index in range(len(times) - 1):
        left = max(float(times[index]), start)
        right = min(float(times[index + 1]), end)
        value = _number(values[index])
        if right <= left or times[index + 1] - times[index] > max_step or value is None:
            continue
        if moving and (index >= len(moving) or moving[index] not in {True}):
            continue
        samples.append((left, right - left, value))
    return samples


# Bump when ``heart_rate_histogram`` counts differently, so stored rows recompute.
# v2: steps up to the adaptive gap rule of the zone times count (#1950).
HEART_RATE_HISTOGRAM_VERSION = 2
HISTOGRAM_MIN_BPM = 30
HISTOGRAM_MAX_BPM = 240


def heart_rate_histogram(activity: dict[str, Any]) -> dict[str, Any] | None:
    """Seconds per integer bpm of the moving heart-rate samples, or None.

    Counts the samples ``_samples`` keeps (moving, pauses over the adaptive
    gap rule left out) with a reading of 30-240 bpm. ``covered_s`` is the counted time.
    """
    seconds: dict[int, float] = {}
    for _timestamp, weight, value in _samples(
        activity, "heartrate", -math.inf, math.inf
    ):
        if HISTOGRAM_MIN_BPM <= value <= HISTOGRAM_MAX_BPM:
            bpm = round(value)
            seconds[bpm] = seconds.get(bpm, 0.0) + weight
    if not seconds:
        return None
    return {
        "version": HEART_RATE_HISTOGRAM_VERSION,
        "covered_s": round(sum(seconds.values()), 3),
        "seconds": {str(bpm): round(seconds[bpm], 3) for bpm in sorted(seconds)},
    }


# Normalized graded speed (M6 v8, McGregor et al. 2009): the analogue of
# normalized power on the grade-adjusted speed. Bump when it computes differently.
NGS_VERSION = 1
NGS_WINDOW_S = 30.0


def normalized_graded_speed(activity: dict[str, Any]) -> dict[str, Any] | None:
    """A run's normalized graded speed in m/s, or None without a usable stream.

    The 30 s rolling mean of the moving grade-adjusted speed (``_aerobic_output``:
    Minetti outdoors, belt speed indoors), raised to the 4th power, time-weighted
    averaged, then the 4th root. ``seconds`` is the time the rolling means cover.
    """
    if activity.get("sport") != "run":
        return None
    output = _aerobic_output(activity, "velocity_smooth")
    if output is None:
        return None
    view, sensor = output
    means = rolling_means(_samples(view, sensor, -math.inf, math.inf), NGS_WINDOW_S)
    seconds = sum(weight for weight, _mean in means)
    if seconds <= 0:
        return None
    fourth = sum(weight * mean**4 for weight, mean in means) / seconds
    return {
        "version": NGS_VERSION,
        "ngs_mps": round(fourth**0.25, 4),
        "seconds": round(seconds, 3),
        "source": sensor,
    }


def _target(
    step: dict[str, Any], basis: dict[str, Any]
) -> tuple[str, float, float, str] | None:
    text = str(step.get("target") or "").upper()
    if step.get("ramp") or text.startswith("Z"):
        return None
    parsed = _numeric_target(step, basis, text)
    if step.get("kind") == "pace" and ":" in text:
        parsed = _pace_target(text)
    if parsed is None:
        return None
    sensor, values, unit = parsed
    low, high = min(values), max(values)
    if len(values) == 1:
        low, high = low * 0.95, high * 1.05
    return sensor, low, high, unit


def interval_quality(
    activity: dict[str, Any],
    targets: dict[str, Any] | None,
    *,
    source_label: str = SESSION_SOURCES_LABEL,
) -> dict[str, Any]:
    base = {
        "method": METHOD,
        "source": f"derived from {source_label} original streams and frozen local targets",
        "tolerance": "Authored ranges; single point targets use an explicit +/-5% measurement band. Missing samples, stopped timer and gaps above max(30 s, 3 x the median step) are excluded.",
    }
    if not targets or not targets.get("steps"):
        return {
            **base,
            "status": "insufficient_data",
            "reason": "Kein eindeutig gepaartes, gespeichertes Trainingsziel vorhanden.",
        }
    steps = targets["steps"]
    laps = activity.get("laps") or []
    if (
        not laps
        or len(laps) > len(steps)
        or any("duration" not in step for step in steps)
    ):
        return {
            **base,
            "status": "insufficient_data",
            "reason": "Runden fehlen oder die zeitbasierte Zuordnung ist mehrdeutig. Distanzschritte werden noch nicht bewertet.",
        }
    results = _interval_steps(activity, targets, laps, steps, base)
    if isinstance(results, dict):
        return results
    groups = _repeat_groups(results)
    return {
        **base,
        "status": "ok"
        if len(laps) == len(steps) and all(row["status"] == "ok" for row in results)
        else "partial",
        "planned_steps": len(steps),
        "aligned_steps": len(laps),
        "missing_steps": len(steps) - len(laps),
        "steps": results,
        "repeat_groups": groups,
        "target_snapshot": {
            key: targets.get(key)
            for key in ("planned_unit_id", "observed_at", "basis", "matching")
        },
    }


def _aerobic_sensor(activity: dict[str, Any]) -> str | None:
    """Return the output stream of a ride or a road run, from the canonical sport.

    Trail runs, whose pace is not comparable, are not analysed, as before.
    """
    sport = activity.get("sport")
    if sport == "ride":
        return "watts"
    if sport == "run" and not activity.get("trail"):
        return "velocity_smooth"
    return None


def _aerobic_output(
    activity: dict[str, Any], sensor: str
) -> tuple[dict[str, Any], str] | None:
    """The activity and stream the halves read, or ``None`` for unknown terrain.

    Outdoor runs compare grade-adjusted speed (Minetti 2002); treadmill and
    virtual runs are flat and keep their speed.
    """
    if sensor != "velocity_smooth" or summary_is_indoor(activity):
        return activity, sensor
    streams = activity.get("streams") or {}
    grades = grade_stream(streams)
    if grades is None:
        return None
    adjusted = grade_adjusted_speeds(streams, grades)
    view = {**activity, "streams": {**streams, GRADE_ADJUSTED_SPEED: adjusted}}
    return view, GRADE_ADJUSTED_SPEED


# A run's steadiness reads the moving samples at or above this speed (m/s),
# with steps up to this long; a longer gap is a pause.
STEADY_MIN_SPEED_MPS: Final = 1.0
STEADY_MAX_STEP_S: Final = 30.0


def gap_variation_pct(activity: dict[str, Any]) -> float | None:
    """The coefficient of variation of a run's grade-adjusted speed, in % (#3084).

    Time-weighted over the moving samples at or above ``STEADY_MIN_SPEED_MPS``,
    on the speed ``_aerobic_output`` selects: grade-adjusted outdoors, unknown
    outdoors without a grade, the belt speed indoors. The session report
    judges it against the athlete's own comparable runs.
    """
    # The decoupling's sensor gate: a trail run's pace is not comparable.
    sensor = _aerobic_sensor(activity)
    selected = (
        None if sensor != "velocity_smooth" else _aerobic_output(activity, sensor)
    )
    if selected is None:
        return None
    view, key = selected
    streams = view.get("streams") or {}
    speeds, times = streams.get(key), streams.get("time")
    if not (isinstance(speeds, list) and isinstance(times, list)) or len(speeds) != len(
        times
    ):
        return None
    moving = streams.get("moving")
    if not (isinstance(moving, list) and len(moving) == len(times)):
        moving = [True] * len(times)
    weighted = [
        (speed, step)
        for index in range(len(times) - 1)
        if moving[index]
        and (speed := _number(speeds[index])) is not None
        and speed >= STEADY_MIN_SPEED_MPS
        and (start := _number(times[index])) is not None
        and (end := _number(times[index + 1])) is not None
        and 0 < (step := end - start) <= STEADY_MAX_STEP_S
    ]
    total = sum(step for _, step in weighted)
    if len(weighted) < 2 or total <= 0:
        return None
    mean = sum(speed * step for speed, step in weighted) / total
    variance = sum(step * (speed - mean) ** 2 for speed, step in weighted) / total
    return rounded_metric(math.sqrt(variance) / mean * 100)


def aerobic_analysis(
    activity: dict[str, Any], *, source_label: str = SESSION_SOURCES_LABEL
) -> dict[str, Any]:
    base = {
        "method": METHOD,
        "source": f"derived from {source_label} original streams",
        "warmup_excluded_seconds": 600,
        "eligibility": "At least 20 minutes after a 10-minute warm-up; >=85% paired data coverage; variation of the 60 s rolling output means <=15% in each half; gaps above max(30 s, 3 x the median step), stops and non-positive output excluded. No universal fitness cutoff.",
    }
    sensor = _aerobic_sensor(activity)
    times = (activity.get("streams") or {}).get("time") or []
    if sensor is None or len(times) < 2 or times[-1] - times[0] < 1800:
        return {
            **base,
            "status": "insufficient_data",
            "reason": "Geeignete Sportart oder ausreichend lange Messreihe fehlt.",
        }
    selected = _aerobic_output(activity, sensor)
    if selected is None:
        return {
            **base,
            "status": "insufficient_data",
            "reason": "Gelände unbekannt: Steigung oder Höhe fehlt.",
        }
    activity, sensor = selected
    start, end = float(times[0]) + 600, float(times[-1])
    output = _samples(activity, sensor, start, end)
    hr = {
        timestamp: (weight, value)
        for timestamp, weight, value in _samples(activity, "heartrate", start, end)
    }
    paired = [
        (timestamp, min(weight, hr[timestamp][0]), value, hr[timestamp][1])
        for timestamp, weight, value in output
        if value > 0 and timestamp in hr and 30 <= hr[timestamp][1] <= 240
    ]
    halves = _aerobic_halves(paired, start, end, base)
    if isinstance(halves, dict):
        return halves
    if abs(halves[1]["mean_output"] / halves[0]["mean_output"] - 1) > 0.1:
        return {
            **base,
            "status": "insufficient_data",
            "reason": "Die Belastung der frühen und späten Hälfte ist nicht ausreichend vergleichbar.",
        }
    drift = (
        100
        * (halves[0]["efficiency"] - halves[1]["efficiency"])
        / halves[0]["efficiency"]
    )
    return {
        **base,
        "status": "ok",
        "unit": "W/bpm" if sensor == "watts" else "(m/s)/bpm",
        "sensor": sensor,
        "efficiency": round(
            sum(half["mean_output"] for half in halves)
            / sum(half["mean_hr"] for half in halves),
            4,
        ),
        "drift_percent": round(drift, 2),
        "halves": [
            {key: round(value, 4) for key, value in half.items()} for half in halves
        ],
        "long_session": end - times[0] >= 5400,
        "interpretation": "A steady-output efficiency comparison, not a medical readiness score or measured running economy. Temperature, terrain and indoor/outdoor conditions can affect the result.",
    }


def _pace_target(text: str) -> tuple[str, list[float], str] | None:
    parts = re.findall(r"\b(\d{1,6}):([0-5]\d)", text)
    distances = {
        "/KM": 1000,
        "/MI": 1609.344,
        "/100M": 100,
        "/100Y": 91.44,
        "/500M": 500,
        "/400M": 400,
        "/250M": 250,
    }
    distance = next((value for unit, value in distances.items() if unit in text), 1000)
    speeds = [
        distance / (int(minutes) * 60 + int(seconds))
        for minutes, seconds in parts
        if int(minutes) * 60 + int(seconds) > 0
    ]
    if not speeds:
        return None
    sensor, values, unit = "velocity_smooth", speeds, "m/s"

    return sensor, values, unit


def _numeric_target(
    step: dict, basis: dict, text: str
) -> tuple[str, list[float], str] | None:
    values = [float(value) for value in re.findall(r"\d+(?:\.\d+)?", text)]
    if not values:
        return None
    if step.get("kind") == "power":
        sensor, unit = "watts", "W"
        if "%" in text:
            ftp = _number(basis.get(TARGET_BASIS_FTP_KEY))
            if ftp is None or ftp <= 0:
                return None
            values = [value * ftp / 100 for value in values]
    elif step.get("kind") == "hr" and "BPM" in text:
        sensor, unit = "heartrate", "bpm"
    else:
        return None

    return sensor, values, unit


def _interval_steps(
    activity: dict, targets: dict, laps: list[dict], steps: list[dict], base: dict
) -> list[dict] | dict:
    results = []
    previous_end = -1.0
    for index, lap in enumerate(laps):
        start, end = _number(lap.get("start_time")), _number(lap.get("end_time"))
        duration = steps[index]["duration"]
        if (
            start is None
            or end is None
            or end <= start
            or start < previous_end
            or abs((end - start) - duration) > max(5, duration * 0.1)
        ):
            return {
                **base,
                "status": "insufficient_data",
                "reason": "Die Runden lassen sich nicht eindeutig den geplanten Schritten zuordnen.",
            }
        previous_end = end
        target = _target(steps[index], targets.get("basis") or {})
        if target is None:
            results.append(
                {
                    "step": index + 1,
                    "target": steps[index]["target"],
                    "status": "insufficient_data",
                    "reason": "Historische Zielbasis fehlt oder Zieltyp wird noch nicht unterstützt.",
                }
            )
            continue
        results.append(
            _measured_step(activity, steps[index], index, start, end, target)
        )

    return results


def _repeat_groups(results: list[dict]) -> list[dict]:
    groups = []
    for target_text in dict.fromkeys(str(row["target"]) for row in results):
        repeats = [
            row
            for row in results
            if row["target"] == target_text
            and row.get("status") == "ok"
            and row.get("mean")
            and row["mean"] > 0
        ]
        if (
            len(repeats) >= 2
            and max(row["end"] - row["start"] for row in repeats)
            / min(row["end"] - row["start"] for row in repeats)
            <= 1.1
        ):
            groups.append(
                {
                    "target": target_text,
                    "repetitions": len(repeats),
                    "fade_percent": round(
                        100
                        * (repeats[0]["mean"] - repeats[-1]["mean"])
                        / repeats[0]["mean"],
                        2,
                    ),
                }
            )

    return groups


def _aerobic_halves(
    paired: list, start: float, end: float, base: dict
) -> list[dict] | dict:
    midpoint = (start + end) / 2
    halves = []
    for left, right in ((start, midpoint), (midpoint, end)):
        # (time, weight inside the half, output, heart rate) of each sample.
        clipped = [
            sample
            for sample in (
                (
                    max(timestamp, left),
                    min(timestamp + weight, right) - max(timestamp, left),
                    value,
                    pulse,
                )
                for timestamp, weight, value, pulse in paired
            )
            if sample[1] > 0
        ]
        samples = [(weight, value, pulse) for _, weight, value, pulse in clipped]
        duration = sum(weight for weight, _, _ in samples)
        if duration < (right - left) * 0.85:
            return {
                **base,
                "status": "insufficient_data",
                "reason": "Zu wenig lückenfreie, bewegte Messzeit mit Leistung/Geschwindigkeit und Puls.",
            }
        mean = sum(weight * value for weight, value, _ in samples) / duration
        pulse = sum(weight * pulse for weight, _, pulse in samples) / duration
        cv = weighted_cv(
            rolling_means(
                [(stamp, weight, value) for stamp, weight, value, _ in clipped],
                STEADY_WINDOW_S,
            )
        )
        if cv is None or cv > MAX_STEADY_CV:
            return {
                **base,
                "status": "insufficient_data",
                "reason": "Die Belastung ist für einen gleichmäßigen Ausdauervergleich zu variabel.",
            }
        halves.append(
            {
                "start": left,
                "end": right,
                "measured_seconds": duration,
                "mean_output": mean,
                "mean_hr": pulse,
                "efficiency": mean / pulse,
                "variation": cv,
            }
        )

    return halves


def _measured_step(
    activity: dict, step: dict, index: int, start: float, end: float, target: tuple
) -> dict:
    sensor, low, high, unit = target
    samples = _samples(activity, sensor, start, end)
    measured = sum(weight for _, weight, _ in samples)
    mean = (
        sum(weight * value for _, weight, value in samples) / measured
        if measured
        else None
    )
    in_range = sum(weight for _, weight, value in samples if low <= value <= high)
    adequate = measured / (end - start) >= 0.8
    midpoint = (low + high) / 2
    return {
        "step": index + 1,
        "target": step["target"],
        "unit": unit,
        "start": start,
        "end": end,
        "target_low": low,
        "target_high": high,
        "status": "ok" if adequate else "insufficient_data",
        "measured_seconds": round(measured, 1),
        "coverage": round(measured / (end - start), 3),
        "mean": round(mean, 2) if adequate and mean is not None else None,
        "seconds_in_target": round(in_range, 1) if adequate else None,
        "average_deviation_percent": round(100 * (mean - midpoint) / midpoint, 2)
        if adequate and mean is not None and midpoint > 0
        else None,
    }

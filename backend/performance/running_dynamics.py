"""Running dynamics against the athlete's own runs at the same pace (#2829).

Ground contact time, vertical oscillation, cadence and step length change with
speed, so a fixed band rates every slow run as poor. Each run is split into
15 s/km pace bands (walking, stops and implausible speeds left out); per band it
keeps the time-weighted mean of every running-dynamics value. A run is rated per
band against the athlete's earlier runs of the last 84 days in the same band:
the best third of those runs is "better", the middle third "usual", the weakest
third "worse" (athlete decision 2026-11-14). Shorter contact time, lower
vertical oscillation and ratio, higher cadence (Heiderscheit 2011) and a balance
closer to 50 % count as better; step length is only longer or shorter. No value
is a universal norm (Moore 2016, Folland 2017).

Hills change the dynamics at the same pace, so with a slope stream only samples
within the flat slope of the pace-at-heart-rate bins count, and runs are set only
against runs of the same environment (treadmill against treadmill).

Garmin's colour bands are a second, labelled reference: population percentiles
of Garmin's runners, independent of pace, from Garmin's own manual, applied to
the run's averages as Garmin reports them. Pure: no I/O.
"""

from __future__ import annotations

import math
import statistics
from bisect import bisect_right
from collections.abc import Iterator, Mapping, Sequence
from datetime import date, timedelta
from typing import Any, Final

from backend.performance.form_drift import MAX_GAP_S
from backend.performance.grade_adjustment import grade_stream
from backend.performance.pace_at_hr import FLAT_GRADE_PCT
from backend.performance.session_execution import RUN_CADENCE_FACTOR

# Bump when the per-run bands change, so session curves recompute.
METHOD: Final = "running-dynamics-pace-bands-v1"
BAND_S_PER_KM: Final = 15
# Slower than this is walking (8:20/km); faster than this is a GPS spike (2:30/km).
MIN_RUN_SPEED_MPS: Final = 2.0
MAX_RUN_SPEED_MPS: Final = 1000 / 150
# A run counts in a band with at least this much time in it.
MIN_BAND_S: Final = 120.0
# A band is rated against at least this many earlier runs of the window.
MIN_PEERS: Final = 5
WINDOW_DAYS: Final = 84
# The run averages a provider reports, by metric (cadence per leg).
AVERAGE_KEYS: Final = {
    "ground_contact_time_ms": "ground_contact_time_ms",
    "vertical_oscillation_cm": "vertical_oscillation_cm",
    "vertical_ratio_pct": "vertical_ratio_pct",
    "ground_contact_balance_pct": "ground_contact_balance_pct",
    "step_length_m": "step_length_m",
    "cadence_spm": "avg_cadence",
}
# (metric, stream key of the analysis input, direction): -1 lower is better,
# 1 higher is better, 0 no verdict; "balance" is better closer to 50 %.
METRICS: Final[tuple[tuple[str, str, int | str], ...]] = (
    ("ground_contact_time_ms", "ground_contact_time", -1),
    ("vertical_oscillation_cm", "vertical_oscillation", -1),
    ("vertical_ratio_pct", "vertical_ratio", -1),
    ("ground_contact_balance_pct", "ground_contact_balance", "balance"),
    ("cadence_spm", "cadence", 1),
    ("step_length_m", "step_length", 0),
)
_DIGITS: Final = {
    "ground_contact_time_ms": 0,
    "vertical_oscillation_cm": 1,
    "vertical_ratio_pct": 1,
    "ground_contact_balance_pct": 1,
    "cadence_spm": 0,
    "step_length_m": 2,
}
SCOPE: Final = (
    "Per 15 s/km pace band, the run's time-weighted mean against the same band "
    "of the athlete's runs of the 84 days before it (at least 5 runs with 2 "
    "minutes in the band, same environment): best third better, middle third "
    "usual, weakest third worse. Walking (under 2.0 m/s), stops, gaps over 5 s "
    "and, with a slope stream, samples steeper than 3 % are left out. For the "
    "balance, usual is the middle third of the distance from 50 %."
)


def pace_bands(
    activity: Mapping[str, Any], *, source_label: str = ""
) -> dict[str, Any] | None:
    """The run's time and mean running dynamics per pace band, or None.

    Only non-trail runs with at least one running-dynamics stream have bands;
    a band needs ``MIN_BAND_S`` of running in it. The input is the
    legacy-shaped activity dict of ``analysis_input``; ``source_label`` names
    the providers of the streams read.
    """
    if activity.get("sport") != "run" or activity.get("trail"):
        return None
    streams = activity.get("streams") or {}
    present = _dynamics_streams(streams)
    if not present:
        return None
    sums: dict[int, dict[str, list[float]]] = {}
    for index, step, band in _running_steps(streams):
        totals = sums.setdefault(band, {"seconds": [0.0, 0.0]})
        totals["seconds"][0] += step
        for metric, values in present:
            value = _number(values[index])
            if value > 0:
                pair = totals.setdefault(metric, [0.0, 0.0])
                pair[0] += value * step
                pair[1] += step
    bands = {
        str(band): {
            "seconds": round(totals["seconds"][0]),
            **{
                metric: round(pair[0] / pair[1], _DIGITS[metric] + 1)
                for metric, pair in totals.items()
                if metric != "seconds" and pair[1] >= MIN_BAND_S
            },
        }
        for band, totals in sorted(sums.items())
        if totals["seconds"][0] >= MIN_BAND_S
    }
    if not bands:
        return None
    return {"method": METHOD, "source": source_label, "bands": bands}


def _dynamics_streams(streams: Mapping[str, Any]) -> list[tuple[str, list[Any]]]:
    """Each running-dynamics stream on the time axis, cadence in steps per minute.

    Empty without a speed stream or a running-dynamics stream besides cadence.
    """
    length = len(streams.get("time") or [])
    present = [
        (metric, values)
        for metric, key, _direction in METRICS
        if metric != "cadence_spm"
        and isinstance(values := streams.get(key), list)
        and len(values) == length
    ]
    if not present or len(streams.get("velocity_smooth") or []) != length:
        return []
    cadence = streams.get("cadence")
    if isinstance(cadence, list) and len(cadence) == length:
        present.append(("cadence_spm", [_scaled(value) for value in cadence]))
    return present


def _running_steps(streams: Mapping[str, Any]) -> Iterator[tuple[int, float, int]]:
    """Each sample of running: its index, the time it covers and its pace band.

    Stops, walking, implausible speeds and gaps over ``MAX_GAP_S`` are left
    out, and with a slope stream every sample steeper than ``FLAT_GRADE_PCT``.
    """
    times, speeds = streams["time"], streams["velocity_smooth"]
    moving = streams.get("moving")
    grades = grade_stream(streams)
    for index in range(len(times) - 1):
        step = _number(times[index + 1]) - _number(times[index])
        speed = _number(speeds[index])
        running = MIN_RUN_SPEED_MPS <= speed <= MAX_RUN_SPEED_MPS
        moved = not moving or (index < len(moving) and moving[index] is True)
        grade = None if grades is None else grades[index]
        flat = grades is None or (grade is not None and abs(grade) <= FLAT_GRADE_PCT)
        if 0 < step <= MAX_GAP_S and running and moved and flat:
            # Rounded first, so 4:00/km exactly is not 3:59.99 in floating point.
            yield (
                index,
                step,
                int(round(1000 / speed, 3) // BAND_S_PER_KM * BAND_S_PER_KM),
            )


def session_rating(
    own: Mapping[str, Any], records: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Rate one run's bands against the earlier runs of the window.

    ``own`` and ``records`` are session curve records (``date``,
    ``activity_id``, ``environment``, ``running_dynamics`` and the provider's
    ``dynamics_averages``); records after the run, before its window or of
    another environment are left out.
    """
    base = {"method": METHOD, "scope": SCOPE, "window_days": WINDOW_DAYS}
    bands = (own.get("running_dynamics") or {}).get("bands") or {}
    if not bands:
        return {**base, "status": "not_covered", "reason": "Keine Laufdynamik."}
    day = date.fromisoformat(str(own.get("date")))
    first = (day - timedelta(days=WINDOW_DAYS)).isoformat()
    peers = [
        (record.get("running_dynamics") or {}).get("bands") or {}
        for record in records
        if first <= str(record.get("date")) < day.isoformat()
        and record.get("activity_id") != own.get("activity_id")
        and record.get("environment") == own.get("environment")
    ]
    rated = [_band(int(band), values, peers) for band, values in bands.items()]
    return {
        **base,
        "status": "ok",
        "source": (own.get("running_dynamics") or {}).get("source"),
        "bands": rated,
        "metrics": _run_verdicts(rated, own.get("dynamics_averages") or {}),
        "garmin_source": GARMIN_SOURCE,
    }


def _band(
    band: int, values: Mapping[str, Any], peers: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    metrics = {}
    for metric, _key, direction in METRICS:
        value = values.get(metric)
        if not isinstance(value, (int, float)):
            continue
        earlier = [
            float(peer[str(band)][metric])
            for peer in peers
            if isinstance((peer.get(str(band)) or {}).get(metric), (int, float))
        ]
        metrics[metric] = {
            "value": round(value, _DIGITS[metric]),
            "peers": len(earlier),
            **_verdict(float(value), earlier, direction),
        }
    return {"band_s_per_km": band, "seconds": values.get("seconds"), "metrics": metrics}


def _verdict(
    value: float, earlier: Sequence[float], direction: int | str
) -> dict[str, Any]:
    """The verdict of ``value`` among ``earlier`` and their middle third."""
    if len(earlier) < MIN_PEERS:
        return {"verdict": "too_few"}
    if direction == "balance":
        # The middle third of the distance from 50 %, as offsets either side.
        near, far = _tertiles(sorted(abs(item - 50) for item in earlier))
        distance = abs(value - 50)
        verdict = "usual"
        if distance < near:
            verdict = "better"
        elif distance > far:
            verdict = "worse"
        return {"verdict": verdict, "usual": [round(near, 2), round(far, 2)]}
    low, high = _tertiles(sorted(earlier))
    usual = [round(low, 2), round(high, 2)]
    if low <= value <= high:
        return {"verdict": "usual", "usual": usual}
    above = value > high
    if direction == 0:
        return {"verdict": "higher" if above else "lower", "usual": usual}
    better = above if direction == 1 else not above
    return {"verdict": "better" if better else "worse", "usual": usual}


def _tertiles(ordered: Sequence[float]) -> tuple[float, float]:
    """The boundaries of the middle third (inclusive quantiles)."""
    low, high = statistics.quantiles(ordered, n=3, method="inclusive")
    return low, high


def _run_verdicts(
    bands: Sequence[Mapping[str, Any]], averages: Mapping[str, Any]
) -> dict[str, Any]:
    """Per metric, the verdict holding the most rated time of the run.

    ``value`` is the run's mean over its bands, weighted by their time;
    ``reference_band`` is Garmin's colour of the provider's run average (the value the
    session shows), else of that mean.
    """
    result: dict[str, Any] = {}
    for metric, _key, _direction in METRICS:
        rated = [
            (band["metrics"][metric], float(band.get("seconds") or 0))
            for band in bands
            if band["metrics"].get(metric)
        ]
        total = sum(seconds for _, seconds in rated)
        if total <= 0:
            continue
        value = round(
            sum(item["value"] * seconds for item, seconds in rated) / total,
            _DIGITS[metric],
        )
        time: dict[str, float] = {}
        for item, seconds in rated:
            if item["verdict"] != "too_few":
                time[item["verdict"]] = time.get(item["verdict"], 0.0) + seconds
        reported = _number(averages.get(AVERAGE_KEYS[metric]))
        if metric == "cadence_spm":
            reported *= RUN_CADENCE_FACTOR
        result[metric] = {
            "verdict": max(time, key=time.__getitem__) if time else "too_few",
            "value": value,
            "reference_band": garmin_band(metric, reported or value),
        }
    return result


# Garmin's colour bands from the EPIX (Gen 2) owner's manual, the newest
# generation that still lists all five values ("Color Gauges and Running
# Dynamics Data", "Vertical Oscillation and Vertical Ratio Data", "Ground
# Contact Time Balance Data"; the Forerunner 945 manual has the same limits):
# population percentiles of Garmin's runners, purple > 95th, blue 70-95th,
# green 30-69th, orange 5-29th, red < 5th. The athlete runs with a chest strap
# (HRM-Pro/HRM-600), so vertical oscillation and ratio take the chest column.
# Each entry: the upper bounds of the colours in rising order of the value.
GARMIN_SOURCE: Final = "Garmin-Einordnung (Brustgurt), Garmin EPIX Handbuch"
_GARMIN_RISING: Final[dict[str, tuple[tuple[float, str], ...]]] = {
    # Lower is better: purple below the first bound.
    "ground_contact_time_ms": (
        (218, "purple"),
        (249, "blue"),
        (278, "green"),
        (309, "orange"),
    ),
    "vertical_oscillation_cm": (
        (6.4, "purple"),
        (8.2, "blue"),
        (9.8, "green"),
        (11.6, "orange"),
    ),
    "vertical_ratio_pct": (
        (6.1, "purple"),
        (7.5, "blue"),
        (8.7, "green"),
        (10.2, "orange"),
    ),
}
_GARMIN_CADENCE: Final[tuple[tuple[float, str], ...]] = (
    (153, "red"),
    (164, "orange"),
    (174, "green"),
    (184, "blue"),
)


# Balance: green within 50.7 % on either side, orange up to 52.2 %, red beyond.
GARMIN_BALANCE_GREEN: Final = 0.7
GARMIN_BALANCE_ORANGE: Final = 2.2


def garmin_band(metric: str, value: Any) -> str | None:
    """Garmin's colour of a run's average value, or None without a band.

    Vertical oscillation and ratio use the chest-strap limits.
    """
    if not isinstance(value, (int, float)) or math.isnan(value):
        return None
    if metric == "ground_contact_balance_pct":
        offset = round(abs(value - 50), 1)
        if offset <= GARMIN_BALANCE_GREEN:
            return "green"
        return "orange" if offset <= GARMIN_BALANCE_ORANGE else "red"
    rising = metric == "cadence_spm"
    bounds = _GARMIN_CADENCE if rising else _GARMIN_RISING.get(metric)
    if bounds is None:
        return None
    index = bisect_right([bound for bound, _ in bounds], value)
    if index < len(bounds):
        return bounds[index][1]
    return "purple" if rising else "red"


def _scaled(value: Any) -> float:
    number = _number(value)
    return number * RUN_CADENCE_FACTOR if number > 0 else 0.0


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value) if math.isfinite(value) else 0.0

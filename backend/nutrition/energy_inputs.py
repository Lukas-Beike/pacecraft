"""Read the per-day inputs of the adaptive expenditure (#3100).

Builds one ``EnergyInputs`` from the diary, canonical weigh-ins, steps and
floors, and the recorded sessions, whose net cost comes from M11
(``backend/performance/activity_energy.py``). The data access is injected as
``EnergySources``, so the composition root wires the stores and this module
stays free of server state. The resting reference is the athlete's own
(Katch-McArdle from skinfold lean mass) when ``resting`` provides it, and
Garmin's resting value only as the last fallback; its source is kept.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from backend.athlete.body_composition import (
    RestingFormula,
    lean_mass_kg,
    resting_energy,
)
from backend.canonical.activity_detail import ActivityStreamSet
from backend.canonical.records import Observation
from backend.canonical.vocabulary import ProviderId, StreamChannel
from backend.db.activity_detail_store import detail_sets_for_session
from backend.nutrition.energy_balance import (
    DayInputs,
    EnergyInputs,
    EnergyWindow,
    energy_windows,
    history_start,
    weight_trend,
)
from backend.performance import activity_energy as energy

# Session sports by how M11 prices them.
RUN_SPORTS = frozenset({"run"})
WALK_SPORTS = frozenset({"walk", "hike"})
RIDE_SPORTS = frozenset({"ride"})
STRENGTH_SPORTS = frozenset({"strength"})


@dataclass(frozen=True)
class EnergySources:
    """The stores the reader needs, each a callable over one DB connection."""

    diary: Callable[[Any, date, date], Sequence[Mapping[str, Any]]]
    daily_health: Callable[[Any, date, date], Mapping[str, Mapping[str, Any]]]
    weights: Callable[[Any, date, date], Mapping[date, float]]
    sessions: Callable[[Any, date, date], Sequence[Mapping[str, Any]]]
    streams: Callable[[Any, str], Mapping[StreamChannel, Sequence[float | None]]]
    resting: Callable[[Any, date], tuple[float, str] | None]
    body_fat: Callable[[Any, date, date], Mapping[date, float]]  # skinfolds
    profile: Callable[[], Mapping[str, str]] = field(default=dict)


@dataclass(frozen=True)
class SessionCost:
    """One session's own net cost, the M11 method used, and its steps."""

    day: date
    kcal: float | None
    method: str
    steps: float


# Net kcal and method of a session from its mean HR, duration, mass and day.
type HeartRateCost = Callable[
    [float, float, float | None, date], tuple[float, str] | None
]

# Labels of the resting reference, own first (orchestrator, 2026-11-15).
RESTING_FROM_SKINFOLDS = "katch_mcardle_skinfold"
RESTING_FROM_BODY_FAT = "katch_mcardle_body_fat"
# How far back a weigh-in or body fat still counts for the resting reference.
RESTING_LOOKBACK_DAYS = 90


def own_resting(
    weight_kg: float | None,
    skinfold_fat_pct: float | None,
    measured_fat_pct: float | None,
) -> tuple[float, str] | None:
    """Katch-McArdle from lean mass: skinfold body fat first, then a measured one.

    None without a weight or a usable body fat, so the reader falls back to
    Garmin's resting value, labelled as such.
    """
    if not weight_kg:
        return None
    for fat_pct, source in (
        (skinfold_fat_pct, RESTING_FROM_SKINFOLDS),
        (measured_fat_pct, RESTING_FROM_BODY_FAT),
    ):
        if fat_pct is None:
            continue
        try:
            lean = lean_mass_kg(weight_kg, fat_pct)
        except ValueError:
            continue
        return resting_energy(RestingFormula.KATCH_MCARDLE, lean), source
    return None


def latest_value(observations: Iterable[Observation]) -> float | None:
    """The last day's positive value of canonical observations, or None."""
    values = weighins(observations)
    return values[max(values)] if values else None


def session_streams(
    db: Any, activity_id: str
) -> Mapping[StreamChannel, Sequence[float | None]]:
    """The channels of the session's first stored stream set, else none."""
    for stored in detail_sets_for_session(db, activity_id):
        if isinstance(stored.detail, ActivityStreamSet):
            return stored.detail.channels
    return {}


def weighins(observations: Iterable[Observation]) -> dict[date, float]:
    """One canonical weigh-in per day: the last one read for that day."""
    return {
        o.observed_on: value
        for o in observations
        if (value := _number(o.value.value)) is not None and value > 0
    }


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except TypeError, ValueError:
        return None
    return None if math.isnan(number) else number


def _duration_s(summary: Mapping[str, Any]) -> float | None:
    return _number(summary.get("moving_duration_s")) or _number(
        summary.get("elapsed_duration_s")
    )


def _mass(
    trend: Mapping[date, float], day: date, fallback: float | None
) -> float | None:
    known = [d for d in trend if d <= day]
    return trend[max(known)] if known else fallback


def _flex_fit(
    sessions: Sequence[Mapping[str, Any]], rmr: float
) -> energy.FlexHrFit | None:
    """The athlete's own HR line from power sessions, at session granularity."""
    points = []
    for summary in sessions:
        duration = _duration_s(summary)
        hr = _number(summary.get("avg_hr"))
        work = _work_kj(summary, duration)
        if not (duration and hr and work) or duration <= energy.FLEX_HR_SKIP_S:
            continue
        net = energy.cycling_net_kcal(work, duration, rmr)
        minutes = duration / 60.0
        points.append(energy.FlexHrSegment(hr, net / minutes, minutes / 2.0))
    return energy.fit_flex_hr(points)


def _heart_rate_cost(
    flex: energy.FlexHrFit | None, profile: Mapping[str, str], rmr: float
) -> HeartRateCost:
    """The own HR line when fitted, else Keytel with the profile's age and sex."""
    birth_year = _number(profile.get("birth_year"))
    sex = str(profile.get("sex") or "")

    def cost(
        hr: float, duration: float, mass: float | None, day: date
    ) -> tuple[float, str] | None:
        if flex is not None:
            return energy.flex_hr_net_kcal(flex, hr, duration), "flex_hr"
        if birth_year and mass and sex in ("male", "female"):
            age = day.year - birth_year
            male = sex == "male"
            kcal = energy.keytel_net_kcal(hr, duration, mass, age, male, rmr)
            return kcal, "keytel"
        return None

    return cost


def _work_kj(summary: Mapping[str, Any], duration: float | None) -> float | None:
    work = _number(summary.get("work_kj"))
    power = _number(summary.get("avg_power"))
    if work:
        return work
    return power * duration / 1000.0 if power and duration else None


def _graded(
    sources: EnergySources, db: Any, summary: Mapping[str, Any]
) -> list[tuple[float, float]]:
    """Grade segments from the stored streams, else the distance as flat."""
    channels = sources.streams(db, str(summary.get("id") or ""))
    distance = channels.get(StreamChannel.DISTANCE_M) or []
    segments = energy.graded_segments(
        distance, channels.get(StreamChannel.ALTITUDE_M) or []
    )
    total = _number(summary.get("distance_m"))
    return segments or ([(total, 0.0)] if total else [])


def session_cost(
    sources: EnergySources,
    db: Any,
    summary: Mapping[str, Any],
    mass: float | None,
    rmr: float,
    hr_cost: HeartRateCost,
    step_m: float | None,
) -> SessionCost:
    """Net cost of one session by its sport, with the M11 method name."""
    day = date.fromisoformat(str(summary.get("started_at_local") or "")[:10])
    sport = str(summary.get("sport") or "")
    duration = _duration_s(summary)
    cadence = _number(summary.get("avg_cadence"))
    steps = cadence * duration / 60.0 if cadence and duration else 0.0
    if sport in RUN_SPORTS and mass:
        joules = sum(
            run * energy.running_cost_j_per_kg_m(grade)
            for run, grade in _graded(sources, db, summary)
        )
        return SessionCost(day, mass * joules / energy.J_PER_KCAL, "minetti", steps)
    if sport in WALK_SPORTS and mass:
        segments = _graded(sources, db, summary)
        joules = sum(run * energy.walking_cost_j_per_kg_m(g) for run, g in segments)
        if not steps and step_m:
            steps = sum(run for run, _ in segments) / step_m
        return SessionCost(day, mass * joules / energy.J_PER_KCAL, "minetti", steps)
    kcal, method = _timed_cost(summary, sport, duration, rmr, hr_cost, mass, day)
    return SessionCost(day, kcal, method, 0.0 if method in ("power", "met") else steps)


def _timed_cost(
    summary: Mapping[str, Any],
    sport: str,
    duration: float | None,
    rmr: float,
    hr_cost: HeartRateCost,
    mass: float | None,
    day: date,
) -> tuple[float | None, str]:
    """Rides from work, strength from METs, the rest from heart rate."""
    if not duration:
        return None, "unknown"
    work = _work_kj(summary, duration)
    if sport in RIDE_SPORTS and work:
        return energy.cycling_net_kcal(work, duration, rmr), "power"
    if sport in STRENGTH_SPORTS:
        return energy.strength_net_kcal(duration, rmr), "met"
    hr = _number(summary.get("avg_hr"))
    priced = hr_cost(hr, duration, mass, day) if hr else None
    return priced if priced is not None else (None, "unknown")


def _step_length(
    sessions: Sequence[Mapping[str, Any]], height_m: float | None
) -> float | None:
    speeds, cadences = [], []
    for summary in sessions:
        if str(summary.get("sport") or "") not in WALK_SPORTS:
            continue
        duration, distance = _duration_s(summary), _number(summary.get("distance_m"))
        cadence = _number(summary.get("avg_cadence"))
        if duration and distance and cadence:
            speeds.append(distance / duration)
            cadences.append(cadence)
    return energy.step_length_m(speeds, cadences, height_m)


def _resting(
    sources: EnergySources,
    db: Any,
    end: date,
    health: Mapping[str, Mapping[str, Any]],
) -> tuple[float | None, str | None]:
    """Own resting rate first; Garmin's mean resting value only as fallback."""
    own = sources.resting(db, end)
    if own is not None:
        return own
    device = [
        value
        for day in health.values()
        if (value := _number(day.get("resting_kcal"))) is not None and value > 0
    ]
    if not device:
        return None, None
    return statistics.fmean(device), ProviderId.GARMIN.value


def read_energy_inputs(
    db: Any, sources: EnergySources, start: date, end: date
) -> tuple[EnergyInputs, tuple[SessionCost, ...]]:
    """The days from ``start`` to ``end`` and the priced sessions behind them."""
    profile = sources.profile()
    height_cm = _number(profile.get("height_cm"))
    profile_weight = _number(profile.get("weight_kg"))
    health = sources.daily_health(db, start, end)
    rmr, rmr_source = _resting(sources, db, end, health)
    weights = dict(sources.weights(db, start, end))
    trend = weight_trend(weights)
    sessions = list(sources.sessions(db, start, end))
    step_m = _step_length(sessions, height_cm / 100.0 if height_cm else None)
    flex = _flex_fit(
        [s for s in sessions if str(s.get("sport") or "") in RIDE_SPORTS], rmr or 0.0
    )
    hr_cost = _heart_rate_cost(flex, profile, rmr or 0.0)
    costs = tuple(
        session_cost(
            sources,
            db,
            summary,
            _mass(
                trend,
                date.fromisoformat(str(summary["started_at_local"])[:10]),
                profile_weight,
            ),
            rmr or 0.0,
            hr_cost,
            step_m,
        )
        for summary in sessions
        if summary.get("started_at_local")
    )
    entries: dict[date, list[Mapping[str, Any]]] = {}
    for entry in sources.diary(db, start, end):
        entries.setdefault(date.fromisoformat(str(entry["meal_date"])), []).append(
            entry
        )
    days: dict[date, DayInputs] = {}
    day = start
    while day <= end:
        days[day] = _day_inputs(
            day, entries.get(day, []), health.get(day.isoformat(), {}), weights,
            trend, profile_weight, costs, step_m,
        )  # fmt: skip
        day += timedelta(days=1)
    body_fat = dict(sources.body_fat(db, start, end))
    return EnergyInputs(days, body_fat, rmr, rmr_source), costs


def energy_basis(db: Any, sources: EnergySources, end: date) -> dict[str, Any]:
    """The diagnostics counts of the 12-week view ending on ``end`` (#3100)."""
    inputs, costs = read_energy_inputs(db, sources, history_start(end), end)
    return diagnostics(inputs, costs, energy_windows(inputs, end))


def diagnostics(
    inputs: EnergyInputs,
    costs: Sequence[SessionCost],
    windows: Sequence[EnergyWindow] = (),
) -> dict[str, int | str | None]:
    """Counts only, no values: the read history, the latest window's basis and
    how many windows carry an estimate and the calibration."""
    days = inputs.days.values()
    latest = windows[-1] if windows else None
    return {
        "latest_window_complete_days": latest.complete_days if latest else None,
        "latest_window_weighins": latest.weighins if latest else None,
        "latest_window_insufficient": latest.insufficient if latest else None,
        "windows": len(windows),
        "windows_with_estimate": sum(1 for w in windows if w.adaptive_tdee),
        "calibration_windows": latest.calibration_windows if latest else 0,
        "days": len(inputs.days),
        "logged_days": sum(1 for d in days if d.entries),
        "flagged_days": sum(1 for d in days if d.flagged),
        "weighins": sum(1 for d in days if d.weight_kg is not None),
        "sessions": len(costs),
        "unpriced_sessions": sum(1 for c in costs if c.kcal is None),
        "rmr_source": inputs.rmr_source,
    }


def _day_inputs(
    day: date,
    entries: Sequence[Mapping[str, Any]],
    health: Mapping[str, Any],
    weights: Mapping[date, float],
    trend: Mapping[date, float],
    profile_weight: float | None,
    costs: Sequence[SessionCost],
    step_m: float | None,
) -> DayInputs:
    """One day's intake, flags, weigh-in and own activity (sessions + NEAT)."""
    today = [c for c in costs if c.day == day]
    priced = [c.kcal for c in today if c.kcal is not None]
    steps, floors = _number(health.get("steps")), _number(health.get("floors"))
    mass = _mass(trend, day, profile_weight)
    neat = (
        energy.neat_net_kcal(
            mass, steps, sum(c.steps for c in today), step_m, floors or 0.0
        )
        if steps is not None and step_m and mass
        else None
    )
    activity = None if neat is None and not priced else (neat or 0.0) + sum(priced)
    return DayInputs(
        intake_kcal=sum(_number(e.get("kcal")) or 0.0 for e in entries)
        if entries
        else None,
        protein_g=sum(_number(e.get("protein_g")) or 0.0 for e in entries),
        carbohydrate_g=sum(_number(e.get("carbs_g")) or 0.0 for e in entries),
        fat_g=sum(_number(e.get("fat_g")) or 0.0 for e in entries),
        entries=len(entries),
        flagged=any(bool(e.get("estimate_uncertain")) for e in entries),
        weight_kg=weights.get(day),
        activity_kcal=activity,
    )

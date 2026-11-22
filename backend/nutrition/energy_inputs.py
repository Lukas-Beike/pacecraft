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
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any, Final, NamedTuple, cast

from backend.athlete.body_composition import (
    RestingFormula,
    lean_mass_kg,
    resting_energy,
)
from backend.athlete.body_records import (
    APP_SOURCE,
    BODY_GRACE_DAYS,
    CURRENT_WEIGHT_DAYS,
    RESTING_LOOKBACK_DAYS,
    body_fat_on,
    weight_on,
    weight_trend,
)
from backend.athlete.skinfolds import (
    SkinfoldMeasurement,
    measurement_sd_pct,
    repeatability,
)
from backend.canonical.activity_detail import ActivityStreamSet
from backend.canonical.activity_summary import stored_activity_key
from backend.canonical.vocabulary import ProviderId, StreamChannel
from backend.db.activity_detail_store import detail_sets_for_session
from backend.nutrition.energy_balance import (
    BAND_Z,
    BASE_DAYS,
    EDGE_DAYS,
    MIN_WEIGHINS,
    MIN_WEIGHT_SPAN_DAYS,
    WINDOW_DAYS,
    Base,
    DayInputs,
    EnergyInputs,
    EnergyWindow,
    Estimate,
    LearnedEveryday,
    age_weight,
    below_former_share,
    day_tef,
    edge_change,
    energy_windows,
    fat_share,
    fit_learned_everyday,
    history_start,
    independent_windows,
    is_complete,
    is_stable,
    twelve_week_base,
    unexplained_spreads,
)
from backend.performance import activity_energy as energy
from backend.performance.daily_health import complete_garmin_day
from backend.performance.session_rpe_load import (
    FeedbackLookup,
    feedback_by_session,
    session_rating,
)

# Session sports by how M11 prices them.
RUN_SPORTS = frozenset({"run"})
WALK_SPORTS = frozenset({"walk", "hike"})
RIDE_SPORTS = frozenset({"ride"})
STRENGTH_SPORTS = frozenset({"strength"})
# Canonical ``avg_cadence`` of a foot sport counts one leg (strides per
# minute, ``CanonicalActivityRecord``); a step is half a stride (#3187).
FOOT_SPORTS = RUN_SPORTS | WALK_SPORTS
STEPS_PER_STRIDE = 2.0


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
    # A device's or scale's body fat per day with its provider (#3362).
    device_body_fat: Callable[[Any, date, date], Mapping[date, tuple[float, Any]]] = (
        field(default=lambda _db, _start, _end: {})
    )
    # The skinfold measurements on or before a day, newest first (#3336).
    skinfolds: Callable[[Any, date], Sequence[SkinfoldMeasurement]] = field(
        default=lambda _db, _day: ()
    )
    # The sessions' saved feedback, for a strength session's RPE (#3530).
    feedback: FeedbackLookup | None = None
    # The weigh-in counts per source and dropped days per provider (#3557).
    weighin_counts: Callable[[Any, date, date], Mapping[str, Any]] = field(
        default=lambda _db, _start, _end: {}
    )
    # Each weigh-in day's source, ``app`` or ``garmin`` (#3638).
    weighin_sources: Callable[[Any, date, date], Mapping[date, str]] = field(
        default=lambda _db, _start, _end: {}
    )
    # The day's planned units as ``sport`` and ``duration_s`` (the expected total).
    planned: Callable[[Any, date], Sequence[Mapping[str, Any]]] = field(
        default=lambda _db, _day: ()
    )


@dataclass(frozen=True)
class SessionCost:
    """One session's own net cost, the M11 method used, its steps and foot ascent."""

    day: date
    kcal: float | None
    method: str
    steps: float
    # A foot session's ascent leaves the day's floors (#3255); 0 otherwise.
    climb_m: float = 0.0
    # A strength session's RPE, local first, and the MET it set (#3530).
    rpe: float | None = None
    met: float | None = None
    # The session's sport and priced duration, for the planned units' cost.
    sport: str = ""
    duration_s: float | None = None


# Net kcal and method of a session from its mean HR, duration, mass and day.
type HeartRateCost = Callable[
    [float, float, float | None, date], tuple[float, str] | None
]

# Labels of the resting reference, own first (orchestrator, 2026-11-15).
RESTING_FROM_SKINFOLDS = "katch_mcardle_skinfold"
RESTING_FROM_BODY_FAT = "katch_mcardle_body_fat"


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


def recent_fat_pct(
    measurement: SkinfoldMeasurement | None,
    day: date,
    profile: Mapping[date, float] | None = None,
) -> float | None:
    """The newest skinfold body fat within the lookback of ``day``: the
    measurement's, or a dated profile value (3-site skinfolds too, #3557); the
    measurement wins a shared day.

    An older one yields to a newer measured body fat or to Garmin (Gitar, #3142).
    """
    since = day - timedelta(days=RESTING_LOOKBACK_DAYS)
    folds = {d: v for d, v in (profile or {}).items() if since <= d <= day}
    if measurement is not None and measurement.body_fat_pct is not None:
        folds[measurement.measured_on] = measurement.body_fat_pct
    newest = max(folds, default=None)
    return folds[newest] if newest is not None and newest >= since else None


def session_streams(
    db: Any, activity_id: str
) -> Mapping[StreamChannel, Sequence[float | None]]:
    """The channels of the session's first stored stream set, else none."""
    for stored in detail_sets_for_session(db, activity_id):
        if isinstance(stored.detail, ActivityStreamSet):
            return stored.detail.channels
    return {}


def source_offset(
    inputs: EnergyInputs,
    windows: Sequence[EnergyWindow],
    sources_by_day: Mapping[date, str],
) -> dict[str, Any]:
    """Whether Garmin's fallback weigh-ins sit apart from the app's (#3638).

    Over the windows with a stored-energy estimate that hold weigh-ins of both
    sources, each weigh-in's residual around its window's least-squares line,
    averaged per source and rounded to 0.1 kg; a systematic offset between the
    two reads as a step, which the slope prices as tissue. Counts and rounded
    means only.
    """
    residuals: dict[str, list[float]] = {APP_SOURCE: [], ProviderId.GARMIN.value: []}
    mixed = 0
    for window in windows:
        span = [window.end - timedelta(days=n) for n in range(window.days)]
        points = [
            (d, weight)
            for d in sorted(span)
            if (weight := inputs.days[d].weight_kg if d in inputs.days else None)
            is not None
        ]
        kinds = [sources_by_day.get(d) for d, _ in points]
        if window.store is None or not set(residuals) <= set(kinds):
            continue
        mixed += 1
        line = statistics.linear_regression(
            [float((d - points[0][0]).days) for d, _ in points], [w for _, w in points]
        )
        for (d, weight), kind in zip(points, kinds, strict=True):
            if kind in residuals:
                at = line.intercept + line.slope * (d - points[0][0]).days
                residuals[kind].append(weight - at)
    return {
        "mixed_windows": mixed,
        "mean_residual_kg": {
            kind: round(statistics.fmean(values), 1) if values else None
            for kind, values in residuals.items()
        },
    }


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except TypeError, ValueError:
        return None
    return None if math.isnan(number) else number


def _duration_s(
    summary: Mapping[str, Any], elapsed_first: bool = False
) -> float | None:
    """A session's moving time, else its elapsed time; with ``elapsed_first``
    the other way round: a strength session's MET covers its rests between
    sets, which its moving time leaves out (#3530)."""
    keys = ["moving_duration_s", "elapsed_duration_s"]
    if elapsed_first:
        keys.reverse()
    return _number(summary.get(keys[0])) or _number(summary.get(keys[1]))


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
    rmr: float | None,
    hr_cost: HeartRateCost,
    step_m: float | None,
    rpe: float | None = None,
) -> SessionCost:
    """Net cost of one session by its sport, with the M11 method name.

    Every session takes its steps out of the day's NEAT (#3255): the device's
    recorded count first, since the watch also counts steps in a strength
    session or on a ride. A foot session without one estimates them from its
    cadence, else its distance; a bike cadence is no step.
    """
    day = date.fromisoformat(str(summary.get("started_at_local") or "")[:10])
    sport = str(summary.get("sport") or "")
    duration = _duration_s(summary, elapsed_first=sport in STRENGTH_SPORTS)
    steps = _session_steps(summary, sport, duration, step_m)
    climb = (
        max(0.0, _number(summary.get("elevation_gain_m")) or 0.0)
        if sport in FOOT_SPORTS
        else 0.0
    )
    if sport in FOOT_SPORTS and mass:
        kcal = _foot_kcal(sources, db, summary, sport, mass)
        method = "minetti" if kcal is not None else "unknown"
        return SessionCost(
            day, kcal, method, steps, climb, sport=sport, duration_s=duration
        )
    if sport in STRENGTH_SPORTS:
        # Its MET follows its session RPE (#3530), the general one without.
        met = energy.strength_met(rpe)
        kcal = (
            energy.strength_net_kcal(duration, rmr, met)
            if duration and rmr is not None
            else None
        )
        method = "met" if kcal is not None else "unknown"
        return SessionCost(day, kcal, method, steps, climb, rpe, met, sport, duration)
    kcal, method = _timed_cost(summary, sport, duration, rmr, hr_cost, mass, day)
    return SessionCost(
        day, kcal, method, steps, climb, sport=sport, duration_s=duration
    )


def _foot_kcal(
    sources: EnergySources,
    db: Any,
    summary: Mapping[str, Any],
    sport: str,
    mass: float,
) -> float | None:
    """A run's or walk's net kcal from Minetti's cost over its graded
    distance; without any distance it is unknown, not 0 (#3617 item 4)."""
    graded = _graded(sources, db, summary)
    if not graded:
        return None
    cost = (
        energy.running_cost_j_per_kg_m
        if sport in RUN_SPORTS
        else energy.walking_cost_j_per_kg_m
    )
    joules = sum(run * cost(grade) for run, grade in graded)
    return mass * joules / energy.J_PER_KCAL


def _session_steps(
    summary: Mapping[str, Any], sport: str, duration: float | None, step_m: float | None
) -> float:
    """Recorded steps, else a foot session's cadence or distance / step length."""
    recorded = _number(summary.get("steps"))
    if recorded and recorded > 0:
        return recorded
    if sport not in FOOT_SPORTS:
        return 0.0
    strides = _number(summary.get("avg_cadence"))
    if strides and duration:
        return STEPS_PER_STRIDE * strides * duration / 60.0
    distance = _number(summary.get("distance_m"))
    return distance / step_m if distance and step_m else 0.0


def _timed_cost(
    summary: Mapping[str, Any],
    sport: str,
    duration: float | None,
    rmr: float | None,
    hr_cost: HeartRateCost,
    mass: float | None,
    day: date,
) -> tuple[float | None, str]:
    """Rides from work, strength from METs, the rest from heart rate.

    Each is net of the resting rate, so without one the cost is unknown.
    """
    if not duration or rmr is None:
        return None, "unknown"
    work = _work_kj(summary, duration)
    if sport in RIDE_SPORTS and work:
        return energy.cycling_net_kcal(work, duration, rmr), "power"
    hr = _number(summary.get("avg_hr"))
    priced = hr_cost(hr, duration, mass, day) if hr else None
    return priced if priced is not None else (None, "unknown")


def _step_length(
    sessions: Sequence[Mapping[str, Any]], height_m: float | None
) -> tuple[float | None, str | None]:
    """NEAT's step length and its source: the recorded walks' total distance
    over their steps, of the plausible ones, else body height (#3611)."""
    walks = [w for s in sessions if (w := _walk_steps(s)) is not None]
    step = energy.step_length_m(walks, height_m)
    if step is None:
        return None, None
    used = any(energy.plausible_step(d / s, height_m) for d, s in walks)
    return step, "walks" if used else "height"


def _height_m(profile: Mapping[str, Any]) -> float | None:
    height_cm = _number(profile.get("height_cm"))
    return height_cm / 100.0 if height_cm else None


def _walk_steps(summary: Mapping[str, Any]) -> tuple[float, float] | None:
    """A walk's (distance m, steps) (#3611): its recorded steps, else its
    distance over the device's own step length, else its cadence over the
    same moving time that ``_session_steps`` counts."""
    if str(summary.get("sport") or "") not in WALK_SPORTS:
        return None
    distance = _number(summary.get("distance_m"))
    if not distance or distance <= 0:
        return None
    steps = _number(summary.get("steps"))
    step = _number(summary.get("step_length_m"))
    if not steps and step and step > 0:
        steps = distance / step
    if not steps:
        strides, duration = _number(summary.get("avg_cadence")), _duration_s(summary)
        steps = (
            STEPS_PER_STRIDE * strides * duration / 60.0
            if strides and duration
            else None
        )
    return (distance, steps) if steps and steps > 0 else None


def step_walk_counts(
    sessions: Sequence[Mapping[str, Any]], height_m: float | None
) -> dict[str, int]:
    """Counts only (#3611): the walks the step length used or rejected, and how
    many show each suspected cause of the former 2 m step: no recorded steps,
    no cadence, a moving time under 80 % of the elapsed time, a cadence above
    80 per minute (likely steps, not strides), and a former speed/cadence step
    outside the bounds."""
    counts = dict.fromkeys(
        ("walks", "used", "no_steps", "implausible", "without_recorded_steps",
         "zero_cadence", "short_moving_time", "cadence_over_80", "former_implausible"),
        0,
    )  # fmt: skip
    for summary in sessions:
        if str(summary.get("sport") or "") not in WALK_SPORTS:
            continue
        counts["walks"] += 1
        walk = _walk_steps(summary)
        if walk is None:
            counts["no_steps"] += 1
        else:
            kept = energy.plausible_step(walk[0] / walk[1], height_m)
            counts["used" if kept else "implausible"] += 1
        for cause, found in _former_causes(summary, height_m).items():
            counts[cause] += found
    return counts


def _former_causes(
    summary: Mapping[str, Any], height_m: float | None
) -> dict[str, bool]:
    """Which suspected causes of the former speed/cadence step a walk shows."""
    moving = _number(summary.get("moving_duration_s"))
    elapsed = _number(summary.get("elapsed_duration_s"))
    distance = _number(summary.get("distance_m"))
    strides = _number(summary.get("avg_cadence"))
    duration = _duration_s(summary)
    former = (
        distance / duration * 60.0 / (STEPS_PER_STRIDE * strides)
        if distance and duration and strides
        else None
    )
    return {
        "without_recorded_steps": not _number(summary.get("steps")),
        "zero_cadence": not strides,
        "short_moving_time": bool(moving and elapsed and moving < 0.8 * elapsed),
        "cadence_over_80": bool(strides and strides > 80),
        "former_implausible": former is not None
        and not energy.plausible_step(former, height_m),
    }


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
        if (value := _number(day.get("resting_kcal"))) is not None
        and value > 0
        and complete_garmin_day(day)
    ]
    if not device:
        return None, None
    return statistics.fmean(device), ProviderId.GARMIN.value


class SpanBasis(NamedTuple):
    """What a read span fixes for every day it prices: the resting reference,
    the step length and the flex-HR line (#3617 item 6)."""

    rmr: float | None
    rmr_source: str | None
    step_m: float | None
    step_source: str | None
    flex: energy.FlexHrFit | None


def read_energy_inputs(
    db: Any,
    sources: EnergySources,
    start: date,
    end: date,
    older_day: date | None = None,
    basis: SpanBasis | None = None,
) -> tuple[EnergyInputs, tuple[SessionCost, ...]]:
    """The days from ``start`` to ``end`` and the priced sessions behind them.

    An ``older_day`` before ``start`` is added with this span's resting
    reference, step length and flex-HR line, so opening an old diary day
    does not change the span's own values (#3617 item 6). ``basis`` gives
    them from outside instead of reading them.
    """
    profile = sources.profile()
    health = sources.daily_health(db, start, end)
    # The first window looks 90 days back from its own end for a body fat,
    # so the weights reach back as far: each reading has its weight (Gitar).
    lookback = start - timedelta(days=RESTING_LOOKBACK_DAYS)
    grace_end = end + timedelta(days=BODY_GRACE_DAYS)
    weights = dict(sources.weights(db, lookback, end))
    trend = weight_trend(weights)
    body = EnergyInputs(
        days={},
        body_fat_pct=dict(sources.body_fat(db, start, grace_end)),
        rmr_kcal=None,
        device_body_fat=dict(sources.device_body_fat(db, lookback, grace_end)),
        skinfold_sd_pct=_skinfold_sds(sources.skinfolds(db, grace_end)),
    )
    sessions = list(sources.sessions(db, start, end))
    if basis is None:
        basis = _span_basis(
            _resting(sources, db, end, health), body, trend, end, sessions, profile
        )
    rmr, rmr_source, step_m, step_source, flex = basis
    hr_cost = _heart_rate_cost(flex, profile, rmr or 0.0)
    feedback = feedback_by_session(
        [s for s in sessions if str(s.get("sport") or "") in STRENGTH_SPORTS],
        sources.feedback,
    )
    costs = tuple(
        session_cost(
            sources,
            db,
            summary,
            # Körper's weigh-ins carry the mass; the profile is none (#3640).
            weight_on(trend, date.fromisoformat(str(summary["started_at_local"])[:10])),
            rmr,
            hr_cost,
            step_m,
            _rpe(summary, feedback),
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
            trend, costs, step_m,
        )  # fmt: skip
        day += timedelta(days=1)
    if older_day is not None and older_day < start:
        # The day with the weigh-ins its current weight needs (Gitar on #3671).
        weighed_from = older_day - timedelta(days=CURRENT_WEIGHT_DAYS - 1)
        old, old_costs = read_energy_inputs(
            db, sources, weighed_from, min(older_day, start - timedelta(days=1)),
            basis=basis,
        )  # fmt: skip
        days, costs = {**old.days, **days}, old_costs + costs
        fats, device = _older_readings(body, old, start)
    else:
        fats, device = body.body_fat_pct, body.device_body_fat
    return replace(
        body,
        days=days,
        body_fat_pct=fats,
        device_body_fat=device,
        rmr_kcal=rmr,
        rmr_source=rmr_source,
        step_m=step_m,
        step_source=step_source,
    ), costs


def _older_readings(
    body: EnergyInputs, old: EnergyInputs, start: date
) -> tuple[Mapping[date, float], Mapping[date, tuple[float, Any]]]:
    """The span's body fat readings plus the older day's from before the
    earliest day any window looks at, so the old day finds its own body and
    no window changes (Gitar on #3671)."""
    reach = start + timedelta(days=WINDOW_DAYS - 1 - RESTING_LOOKBACK_DAYS)
    fats = {d: v for d, v in old.body_fat_pct.items() if d < reach}
    device = {d: v for d, v in old.device_body_fat.items() if d < reach}
    return {**fats, **body.body_fat_pct}, {**device, **body.device_body_fat}


def _span_basis(
    found: tuple[float | None, str | None],
    body: EnergyInputs,
    trend: Mapping[date, float],
    end: date,
    sessions: Sequence[Mapping[str, Any]],
    profile: Mapping[str, Any],
) -> SpanBasis:
    """The span's resting reference, step length and flex-HR line."""
    rmr, rmr_source = _carried_resting(found, body, trend, end)
    step_m, step_source = _step_length(sessions, _height_m(profile))
    rides = [s for s in sessions if str(s.get("sport") or "") in RIDE_SPORTS]
    flex = _flex_fit(rides, rmr) if rmr is not None else None
    return SpanBasis(rmr, rmr_source, step_m, step_source, flex)


def _carried_resting(
    found: tuple[float | None, str | None],
    body: EnergyInputs,
    trend: Mapping[date, float],
    end: date,
) -> tuple[float | None, str | None]:
    """The own Katch-McArdle with the lean mass the formula rows carry from
    the reading (#3617 item 1, Gitar on #3642); Garmin's fallback stays."""
    rmr, source = found
    if source not in (RESTING_FROM_SKINFOLDS, RESTING_FROM_BODY_FAT):
        return rmr, source
    resolved = resolve_body(body, trend, end)
    if resolved is None:
        return rmr, source
    lean = resolved["lean_mass_kg"]
    return resting_energy(RestingFormula.KATCH_MCARDLE, lean), source


def _skinfold_sds(measurements: Sequence[SkinfoldMeasurement]) -> dict[date, float]:
    """Each skinfold day's body fat SD from the athlete's repeatability and
    its own readings per site; none until the repeats suffice (#3556)."""
    own = repeatability(measurements)
    return {
        m.measured_on: sd
        for m in measurements
        if (sd := measurement_sd_pct(m, own)) is not None
    }


# The formula rows that learn their everyday term (#3577).
LEARNED_FORMULAS: Final = {
    "katch_mcardle": RestingFormula.KATCH_MCARDLE,
    "cunningham": RestingFormula.CUNNINGHAM,
}


def learned_everyday(
    windows: Sequence[EnergyWindow], bodies: Sequence[Mapping[str, Any] | None]
) -> dict[str, LearnedEveryday | None]:
    """Each formula row's learned everyday term (#3577), fitted per formula
    over the estimated windows with a body at their end: the window's own TDEE
    less its uncalibrated activity, digestion and the formula's resting rate,
    what the row lacks against the own total at the rows' k = 1; its SD is
    the derived resting rate's. It learns from the 12-week base's windows
    (#3687, #3689): each weighs by its precision and age, an unstable one less
    through its widened band (#3614), and it qualifies with enough stable
    windows that share no day."""
    fits: dict[str, LearnedEveryday | None] = {}
    end = windows[-1].end if windows else None
    used = [
        (window, body)
        for window, body in zip(windows, bodies, strict=True)
        if body is not None
        and window.adaptive_tdee is not None
        and window.derived_rmr is not None
        and window.activity_kcal_raw is not None
        and window.tef_kcal is not None
        and (cast(date, end) - window.end).days < BASE_DAYS
    ]
    # Independence counts only the windows the fit uses (Gitar, #3693).
    independent = independent_windows([window for window, _ in used])
    for name, formula in LEARNED_FORMULAS.items():
        points = [
            (
                cast(Estimate, window.adaptive_tdee).kcal
                - cast(float, window.activity_kcal_raw)
                - cast(float, window.tef_kcal)
                - resting_energy(formula, body["lean_mass_kg"]),
                cast(Estimate, window.derived_rmr).band_kcal / BAND_Z,
                age_weight(window.end, cast(date, end)),
            )
            for window, body in used
        ]
        fits[name] = fit_learned_everyday(points, independent)
    return fits


def _edge_counts(inputs: EnergyInputs, end: date) -> dict[str, int] | None:
    """The 3-month deficit's weigh-ins per edge window (#3739), null when its
    weigh-ins carry no store (the window minimum) or its edges do not come
    apart (Gitar on #3749)."""
    start = end - timedelta(days=90)
    weights = {
        d: v.weight_kg
        for d, v in inputs.days.items()
        if v.weight_kg is not None and start <= d <= end
    }
    days = sorted(weights)
    if len(days) < MIN_WEIGHINS or (days[-1] - days[0]).days < MIN_WEIGHT_SPAN_DAYS:
        return None
    change = edge_change(weights, start, end, EDGE_DAYS)
    edges = change.edges if change else None
    return {"first": edges[0], "last": edges[1]} if edges else None


def _learned_counts(fits: Iterable[LearnedEveryday | None]) -> dict[str, Any]:
    """The formula rows' learned everyday terms below 0 (#3577) and per
    state (#3723): none (no estimate), provisional or qualified."""
    fits = list(fits)
    state = {"none": 0, "provisional": 0, "qualified": 0}
    for fit in fits:
        if fit is None:
            state["none"] += 1
        else:
            state["qualified" if fit.qualified else "provisional"] += 1
    return {
        "learned_everyday_negative": sum(
            1 for fit in fits if fit is not None and fit.raw_kcal < 0
        ),
        "learned_everyday_state": state,
    }


def energy_basis(db: Any, sources: EnergySources, today: date) -> dict[str, Any]:
    """The diagnostics counts of the 12-week view (#3100).

    Its windows end yesterday, as the energy view's do (#3617 item 7); the
    counts about today's Garmin rest and wellness end look at today.
    """
    end = today - timedelta(days=1)
    inputs, costs = read_energy_inputs(db, sources, history_start(end), end)
    skinfolds, missing = skinfold_state(db, sources, inputs, end)
    windows = energy_windows(inputs, end)
    trend = weight_trend(
        {d: v.weight_kg for d, v in inputs.days.items() if v.weight_kg is not None}
    )
    bodies = [resolve_body(inputs, trend, w.end) for w in windows]
    week = sources.daily_health(db, today - timedelta(days=GARMIN_RESTING_DAYS), today)
    found = newest_resting(week, today)
    latest = windows[-1] if windows else None
    unstable = [w for w in windows if not is_stable(w)]
    return (
        diagnostics(inputs, costs, windows)
        # Weigh-in days per source over the unstable windows' span (#3614):
        # whether a step lines up with UTC-dated profile weigh-ins.
        | {
            "unstable_span_weighin_sources": dict(
                sources.weighin_counts(
                    db,
                    unstable[0].end - timedelta(days=unstable[0].days - 1),
                    unstable[-1].end,
                )
            )
            if unstable
            else None
        }
        # The latest window's weigh-in days per source, and per provider the
        # days whose weights the source rule dropped (#3557), counts only.
        | {
            "latest_window_weighin_sources": dict(
                sources.weighin_counts(
                    db, latest.end - timedelta(days=latest.days - 1), latest.end
                )
            )
            if latest
            else None
        }
        | {
            "weighin_source_offset": source_offset(
                inputs,
                windows,
                sources.weighin_sources(db, history_start(end), end),
            )
        }
        # Whether under-logged but complete days drive the unstable windows (#3658).
        | underlog_counts(inputs, windows)
        | skinfold_diagnostics(skinfolds, missing, end, bodies)
        # The walks behind NEAT's step length and their suspected causes (#3611).
        | {
            "step_walks": step_walk_counts(
                sources.sessions(db, history_start(end), end),
                _height_m(sources.profile()),
            ),
            "step_source": inputs.step_source,
        }
        # Formulas whose learned everyday fit lies below 0, shown as 0 (#3577),
        # and per state: none, provisional or qualified (#3723).
        | _learned_counts(learned_everyday(windows, bodies).values())
        # The weigh-ins in the 3-month deficit's first and last edge, null when
        # the line prices it (#3739); 6 months need a longer read.
        | {"deficit_edge_weighins_91d": _edge_counts(inputs, end)}
        # How many days back today's Garmin Ruhe comes from, None = none (#3443).
        | {"today_resting_days_back": (today - found[0]).days if found else None}
        # Days of the last week whose Garmin summary names its wellness end (#3530).
        | {
            "wellness_end_days": sum(
                1 for day in week.values() if day.get("wellness_end_min") is not None
            )
        }
        # Past days of the latest window whose summary ended early (#3612); the
        # window ends yesterday, so today is never among them (#3617 item 7).
        | {
            "latest_window_unfinished_days": sum(
                1
                for when, day in inputs.days.items()
                if day.unfinished
                and latest.end - timedelta(days=latest.days) < when <= end
            )
            if latest
            else None
        }
    )


# How far back today's Garmin Ruhe looks for a full day's resting value (#3443).
GARMIN_RESTING_DAYS: Final = 7


def newest_resting(
    health: Mapping[str, Mapping[str, Any]], day: date
) -> tuple[date, float] | None:
    """The newest complete day within ``GARMIN_RESTING_DAYS`` before ``day``
    with Garmin resting kcal, and that value; today's own is still counting, and
    a summary that ended early holds part of its day (#3597)."""
    for back in range(1, GARMIN_RESTING_DAYS + 1):
        earlier = day - timedelta(days=back)
        values = health.get(earlier.isoformat(), {})
        resting = _number(values.get("resting_kcal"))
        if resting and resting > 0 and complete_garmin_day(values):
            return earlier, resting
    return None


# The formula rows' body fat sources, in their order (athlete decisions, #3362,
# #3402); the profile's stored value is none (#3640).
BODY_SOURCES = ("skinfold", "entered", "device")


def resolve_body(
    inputs: EnergyInputs, trend: Mapping[date, float], day: date
) -> dict[str, Any] | None:
    """The body fat in effect on ``day`` with the lean mass it gives that day.

    A measurement up to ``BODY_GRACE_DAYS`` after ``day`` counts: the view
    ends yesterday because today's intake is partial, not the body (#3336).
    The body fat comes from the newest skinfold, else an entered one, else a
    device or scale (#3362, #3402; never the profile, #3640); ``source`` and ``label``
    name it.

    The lean mass is carried from the reading (#3617 item 1): the reading's
    weight x (1 - body fat), plus the lean share (1 - the blended fat share) of
    the weight change since, so a lost kilogram is not booked mostly as lean.
    Every source is dated since the profile's undated value left (#3640).
    """
    weighed = [d for d in trend if d <= day]
    fat = body_fat_on(inputs.body_fat_pct, inputs.device_body_fat, day)
    if not weighed or fat is None:
        return None
    weight = trend[max(weighed)]
    on = fat["measured_on"]
    # A reading before the first weigh-in takes the first one after it.
    measured = trend[max([d for d in trend if d <= on] or [min(trend)])]
    share = fat_share(inputs, trend, day).share
    try:
        lean = lean_mass_kg(measured, fat["body_fat_pct"]) + (1.0 - share) * (
            weight - measured
        )
    except ValueError:
        return None
    return {**fat, "weight_kg": weight, "lean_mass_kg": lean}


def body_missing(
    skinfolds: Sequence[SkinfoldMeasurement],
    birth_year_known: bool,
    inputs: EnergyInputs,
    day: date,
) -> str | None:
    """Why the formula rows have no body on ``day``, or None when they have one.

    ``skinfolds`` are the measurements up to the grace day, newest first.
    """
    trend = weight_trend(
        {d: v.weight_kg for d, v in inputs.days.items() if v.weight_kg is not None}
    )
    if resolve_body(inputs, trend, day) is not None:
        return None
    if not any(d <= day for d in trend):
        return "no_weight"
    newest = skinfolds[0] if skinfolds else None
    if newest is None:
        return "no_skinfold"
    if newest.body_fat_pct is None:
        return "invalid" if birth_year_known else "no_birth_year"
    if newest.measured_on < day - timedelta(days=RESTING_LOOKBACK_DAYS):
        return "skinfold_too_old"
    return "invalid"


def skinfold_state(
    db: Any, sources: EnergySources, inputs: EnergyInputs, end: date
) -> tuple[Sequence[SkinfoldMeasurement], str | None]:
    """The measurements up to the grace day and why the body is missing on ``end``."""
    skinfolds = sources.skinfolds(db, end + timedelta(days=BODY_GRACE_DAYS))
    birth_year = str(sources.profile().get("birth_year") or "").strip()
    return skinfolds, body_missing(skinfolds, bool(birth_year), inputs, end)


def skinfold_diagnostics(
    skinfolds: Sequence[SkinfoldMeasurement],
    missing: str | None,
    end: date,
    bodies: Sequence[Mapping[str, Any] | None] = (),
) -> dict[str, Any]:
    """Counts only (#3336): measurements with a body fat, the newest one's age,
    whether the latest window has a body, else the reason code, and which
    source the latest and every window's body fat came from (#3362)."""
    valid = [item for item in skinfolds if item.body_fat_pct is not None]
    sources = [body["source"] if body else "none" for body in bodies]
    return {
        "body_source": sources[-1] if sources else None,
        "body_source_windows": {
            name: sources.count(name) for name in (*BODY_SOURCES, "none")
        },
        "skinfolds_with_body_fat": len(valid),
        "newest_skinfold_age_days": (end - valid[0].measured_on).days
        if valid
        else None,
        "body_resolved": missing is None,
        "body_missing": missing,
    }


def _former_rule_days(inputs: EnergyInputs, window: EnergyWindow | None) -> int | None:
    """The latest window's complete days the former rule dropped (#3553)."""
    if window is None:
        return None
    span = (window.end - timedelta(days=n) for n in range(window.days))
    days = [inputs.days[d] for d in span if d in inputs.days]
    return sum(
        1
        for day in days
        if is_complete(day, inputs.rmr_kcal)
        and below_former_share(day, inputs.rmr_kcal)
    )


def _base_counts(base: Base | None) -> dict[str, int]:
    return {
        "base_windows": base.windows if base else 0,
        "base_weight_sum_tenths": round(10 * base.weight_sum) if base else 0,
        "base_unstable": int(bool(base and base.unstable)),
    }


def diagnostics(
    inputs: EnergyInputs,
    costs: Sequence[SessionCost],
    windows: Sequence[EnergyWindow] = (),
) -> dict[str, int | str | dict[str, int] | None]:
    """Counts only, no values: the read history, the latest window's basis and
    how many windows carry an estimate and the calibration."""
    days = inputs.days.values()
    latest = windows[-1] if windows else None
    return {
        "latest_window_complete_days": latest.complete_days if latest else None,
        # Complete days the former activity-scaled rule dropped (#3553).
        "latest_window_former_rule_days": _former_rule_days(inputs, latest),
        "latest_window_weighins": latest.weighins if latest else None,
        "latest_window_insufficient": latest.insufficient if latest else None,
        "windows": len(windows),
        "windows_with_estimate": sum(1 for w in windows if w.adaptive_tdee),
        # The fat share's prior: Forbes at the current fat mass or the 70 %
        # fallback, and its whole percent (#3684).
        "fat_prior_source": latest.store.fat_prior_source
        if latest and latest.store
        else None,
        "fat_prior_pct": round(100 * latest.store.fat_prior)
        if latest and latest.store
        else None,
        # Windows failing the neighbour check and their largest unexplained
        # difference, to the nearest 100 kcal (#3614).
        "unstable_windows": sum(1 for w in windows if not is_stable(w)),
        # The daily estimate's 12-week base (#3687): the windows it averages,
        # their age-weight sum in tenths, and 1 when every one is unstable.
        **_base_counts(twelve_week_base(windows)),
        # Windows whose weigh-ins carry the store and that hold enough complete
        # days, but whose complete days span too little to price (#3667).
        "windows_span_short": sum(
            1 for w in windows if w.insufficient == "logged_span"
        ),
        "largest_unstable_spread_kcal": int(
            round(max(unexplained_spreads(inputs, windows), default=0.0), -2)
        ),
        # The measurements' weight in each estimated window's fat share, as
        # bands of whole percent (#3555): counts only.
        "fat_share_measured_windows": _measured_weight_bands(windows),
        "calibration_windows": latest.calibration_windows if latest else 0,
        # The estimated windows k and b are fitted over, per store rule; one
        # phase-wide fat share puts them all under one rule (#3554).
        "calibration_store_methods": {
            method: sum(
                1
                for w in windows
                if w.adaptive_tdee is not None
                and w.activity_kcal_raw is not None
                and w.tef_kcal is not None
                and w.store
                and w.store.method == method
            )
            for method in ("measured", "prior")
        },
        "days": len(inputs.days),
        "logged_days": sum(1 for d in days if d.entries),
        "flagged_days": sum(1 for d in days if d.flagged),
        "weighins": sum(1 for d in days if d.weight_kg is not None),
        "sessions": len(costs),
        "unpriced_sessions": sum(1 for c in costs if c.kcal is None),
        "rmr_source": inputs.rmr_source,
    }


# A complete day whose intake stays below this share of rest + TEF passes the
# completeness rule (MIN_DAY_REST_SHARE) only narrowly (#3658).
NARROW_DAY_REST_SHARE = 0.85


def _tdee(window: EnergyWindow) -> float:
    return window.adaptive_tdee.kcal if window.adaptive_tdee else 0.0


def _intake(window: EnergyWindow) -> float:
    """The window's mean logged intake: TDEE plus the stored energy per day."""
    return window.intake_kcal if window.intake_kcal is not None else _tdee(window)


def _activity_gap(a: EnergyWindow, b: EnergyWindow) -> float:
    """Their raw activity difference, which the stability check nets out."""
    if a.activity_kcal_raw is None or b.activity_kcal_raw is None:
        return 0.0
    return a.activity_kcal_raw - b.activity_kcal_raw


def _abs_first(pair: tuple[float, EnergyWindow]) -> float:
    return abs(pair[0])


def _narrow_share(inputs: EnergyInputs, group: Sequence[EnergyWindow]) -> int | None:
    """The share of the windows' complete days below ``NARROW_DAY_REST_SHARE``
    of rest + TEF, in whole percent; None without complete days."""
    rest = inputs.rmr_kcal or 0.0
    spans = {w.end - timedelta(days=n) for w in group for n in range(w.days)}
    complete = [
        day
        for d in spans
        if (day := inputs.days.get(d)) and is_complete(day, inputs.rmr_kcal)
    ]
    low = sum(
        1
        for d in complete
        if (d.intake_kcal or 0.0) < NARROW_DAY_REST_SHARE * (rest + day_tef(d))
    )
    return round(100 * low / len(complete)) if complete else None


def underlog_counts(
    inputs: EnergyInputs, windows: Sequence[EnergyWindow]
) -> dict[str, Any]:
    """Whether under-logged but complete days drive the unstable windows (#3658).

    Each unstable window against the overlapping estimated neighbour whose TDEE
    differs most, net of their activity as the stability check compares them: the mean size of that TDEE gap and of the logged-intake gap
    in the same direction, to 50 kcal (equal gaps: the logged intake carries
    the jump; an intake gap near 0: the weight does), and the share of complete
    days below ``NARROW_DAY_REST_SHARE`` of rest + TEF in the unstable and the
    stable estimated windows, in whole percent. Counts and rounded means only.
    """
    estimated = [w for w in windows if w.adaptive_tdee is not None]
    unstable = [w for w in estimated if not is_stable(w)]
    tdee_gaps, intake_gaps = [], []
    for window in unstable:
        near = [
            o
            for o in estimated
            if o is not window and abs((o.end - window.end).days) < window.days
        ]
        if near:
            # The neighbour it differs from most, the likeliest to have flagged it.
            # Both net of activity, as the check compares them (Gitar on #3660).
            gaps = [
                (_tdee(window) - _tdee(o) - _activity_gap(window, o), o) for o in near
            ]
            gap, other = max(gaps, key=_abs_first)
            activity = _activity_gap(window, other)
            tdee_gaps.append(abs(gap))
            intake_gaps.append(
                math.copysign(1.0, gap) * (_intake(window) - _intake(other) - activity)
            )

    def fifty(values: list[float]) -> int | None:
        return int(round(statistics.fmean(values) / 50) * 50) if values else None

    return {
        "unstable_tdee_gap_kcal": fifty(tdee_gaps),
        "unstable_intake_gap_kcal": fifty(intake_gaps),
        "narrow_complete_day_pct": {
            "unstable": _narrow_share(inputs, unstable),
            "stable": _narrow_share(inputs, [w for w in estimated if is_stable(w)]),
        },
    }


def _rpe(
    summary: Mapping[str, Any], feedback: Mapping[str, Mapping[str, Any]]
) -> float | None:
    """A strength session's RPE, the local rating first (#3530)."""
    if str(summary.get("sport") or "") not in STRENGTH_SPORTS:
        return None
    rating = session_rating(summary, feedback.get(stored_activity_key(summary)))
    return None if isinstance(rating, str) else rating[0]


MEASURED_WEIGHT_BANDS = (
    ("0", 0, 1),
    ("1-24", 1, 25),
    ("25-49", 25, 50),
    ("50-100", 50, 101),
)


def _measured_weight_bands(windows: Sequence[EnergyWindow]) -> dict[str, int]:
    # Estimated windows only: a store without logged meals has no estimate (#3603).
    weights = [
        round(100 * w.store.measured_weight)
        for w in windows
        if w.store and w.adaptive_tdee is not None
    ]
    return {
        band: sum(1 for p in weights if low <= p < high)
        for band, low, high in MEASURED_WEIGHT_BANDS
    }


def _uncovered_kcal(entries: Sequence[Mapping[str, Any]]) -> float:
    """The day's logged kcal its entries' known macros do not cover (#3617)."""
    return sum(
        energy.uncovered_kcal(
            _number(e.get("kcal")) or 0.0,
            _number(e.get("protein_g")) or 0.0,
            _number(e.get("carbs_g")) or 0.0,
            _number(e.get("fat_g")) or 0.0,
        )
        for e in entries
    )


def _day_inputs(
    day: date,
    entries: Sequence[Mapping[str, Any]],
    health: Mapping[str, Any],
    weights: Mapping[date, float],
    trend: Mapping[date, float],
    costs: Sequence[SessionCost],
    step_m: float | None,
) -> DayInputs:
    """One day's intake, flags, weigh-in and own activity (sessions + NEAT)."""
    today = [c for c in costs if c.day == day]
    steps, floors = _number(health.get("steps")), _number(health.get("floors"))
    mass = weight_on(trend, day)
    session_steps = sum(c.steps for c in today)
    climb = sum(c.climb_m for c in today)
    neat = (
        energy.neat_net_kcal(mass, steps, session_steps, step_m, floors or 0.0, climb)
        if steps is not None and step_m and mass
        else None
    )
    # Unknown stays unknown: NEAT without steps or an unpriced session (#3187).
    priced = [c.kcal for c in today if c.kcal is not None]
    sport = sum(priced) if len(priced) == len(today) else None
    # A summary that ended early holds part of its day (#3612): no activity,
    # so the day leaves the windows' means.
    unfinished = not complete_garmin_day(health)
    activity = None if unfinished or neat is None or sport is None else neat + sport
    # From the parts that are known, so today's so-far total keeps a priced
    # session's error before the steps arrive (Gitar on #3567).
    error = energy.activity_error_kcal(
        [(c.method, c.kcal) for c in today if c.kcal is not None], neat or 0.0
    )
    return DayInputs(
        intake_kcal=sum(_number(e.get("kcal")) or 0.0 for e in entries)
        if entries
        else None,
        protein_g=sum(_number(e.get("protein_g")) or 0.0 for e in entries),
        carbohydrate_g=sum(_number(e.get("carbs_g")) or 0.0 for e in entries),
        fat_g=sum(_number(e.get("fat_g")) or 0.0 for e in entries),
        uncovered_kcal=_uncovered_kcal(entries),
        entries=len(entries),
        flagged=any(bool(e.get("estimate_uncertain")) for e in entries),
        flagged_kcal=sum(
            _number(e.get("kcal")) or 0.0
            for e in entries
            if e.get("estimate_uncertain")
        ),
        weight_kg=weights.get(day),
        activity_kcal=activity,
        activity_error_kcal=error,
        neat_kcal=neat,
        sport_kcal=sport,
        steps=steps,
        session_steps=session_steps,
        floors=None
        if floors is None
        else energy.floors_outside_sessions(floors, climb),
        unfinished=unfinished,
    )

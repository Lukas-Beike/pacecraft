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
from typing import Any, Final

from backend.athlete.body_composition import (
    RestingFormula,
    lean_mass_kg,
    resting_energy,
)
from backend.athlete.skinfolds import (
    SkinfoldMeasurement,
    measurement_sd_pct,
    repeatability,
)
from backend.canonical.activity_detail import ActivityStreamSet
from backend.canonical.activity_summary import stored_activity_key
from backend.canonical.records import Observation
from backend.canonical.vocabulary import ProviderId, StreamChannel
from backend.db.activity_detail_store import detail_sets_for_session
from backend.nutrition.energy_balance import (
    BAND_Z,
    DayInputs,
    EnergyInputs,
    EnergyWindow,
    LearnedEveryday,
    below_former_share,
    energy_windows,
    fit_learned_everyday,
    history_start,
    is_complete,
    is_stable,
    unexplained_spreads,
    weight_trend,
)
from backend.performance import activity_energy as energy
from backend.performance.daily_health import complete_garmin_day
from backend.performance.session_rpe_load import (
    FeedbackLookup,
    feedback_by_session,
    session_rating,
)
from backend.providers.source_labels import SourceView, provider_label

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


# Net kcal and method of a session from its mean HR, duration, mass and day.
type HeartRateCost = Callable[
    [float, float, float | None, date], tuple[float, str] | None
]

# Labels of the resting reference, own first (orchestrator, 2026-11-15).
RESTING_FROM_SKINFOLDS = "katch_mcardle_skinfold"
RESTING_FROM_BODY_FAT = "katch_mcardle_body_fat"
# How far back a weigh-in or body fat still counts for the resting reference.
RESTING_LOOKBACK_DAYS = 90
# A skinfold taken up to a day after the view's end still counts (#3336).
BODY_GRACE_DAYS = 1


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


def latest_value(observations: Iterable[Observation]) -> float | None:
    """The last day's value under the weigh-in source rule, or None."""
    values = weighins(observations)
    return values[max(values)] if values else None


def current_weight(
    observations: Iterable[Observation], app: Mapping[date, float], day: date
) -> float | None:
    """The current weight on ``day`` under the weigh-in source rule: the same
    lag-free value the formula rows take from ``weight_trend`` (#3551)."""
    return _mass(weight_trend(weighins(observations, app)), day, None)


def session_streams(
    db: Any, activity_id: str
) -> Mapping[StreamChannel, Sequence[float | None]]:
    """The channels of the session's first stored stream set, else none."""
    for stored in detail_sets_for_session(db, activity_id):
        if isinstance(stored.detail, ActivityStreamSet):
            return stored.detail.channels
    return {}


def weighins(
    observations: Iterable[Observation], app: Mapping[date, float] | None = None
) -> dict[date, float]:
    """One weigh-in (or body fat) per day under the source rule of ``sourced``."""
    days = sourced(observations, app)[0]
    return {day: value for day, (value, _) in sorted(days.items())}


# The weigh-in source rule (athlete decisions, #3557): the app's own records,
# entered (no provider) or a dated profile value (``app``), win their day;
# Garmin's are copies of them and fill only a day without one. Any other
# provider's value, such as Wahoo's stored profile weight, never counts.
APP_SOURCE: Final = "app"


def sourced(
    observations: Iterable[Observation], app: Mapping[date, float] | None = None
) -> tuple[dict[date, tuple[float, str]], dict[str, set[date]]]:
    """Per day the value and its source (``app`` or ``garmin``), and per
    provider the days whose values were dropped. Within a source the last one
    read wins; an entered one wins over a profile value of its day."""
    days = {day: (value, APP_SOURCE) for day, value in (app or {}).items()}
    garmin: dict[date, float] = {}
    dropped: dict[str, set[date]] = {}
    for o in observations:
        value = _number(o.value.value)
        if value is None or value <= 0 or o.sport is not None:
            continue
        provider = o.provenance.provider
        if provider is None:
            days[o.observed_on] = (value, APP_SOURCE)
        elif provider is ProviderId.GARMIN:
            garmin[o.observed_on] = value
        else:
            dropped.setdefault(provider.value, set()).add(o.observed_on)
    for day, value in garmin.items():
        if day in days:
            dropped.setdefault(ProviderId.GARMIN.value, set()).add(day)
        else:
            days[day] = (value, ProviderId.GARMIN.value)
    return days, dropped


def weighin_counts(
    observations: Iterable[Observation], app: Mapping[date, float] | None = None
) -> dict[str, dict[str, int]]:
    """Counts only (#3557): weigh-in days per source and dropped days per provider."""
    days, dropped = sourced(observations, app)
    sources = [source for _, source in days.values()]
    return {
        "by_source": {
            s: sources.count(s) for s in (APP_SOURCE, ProviderId.GARMIN.value)
        },
        "dropped_days": {p: len(d) for p, d in sorted(dropped.items())},
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
        return SessionCost(day, kcal, method, steps, climb)
    if sport in STRENGTH_SPORTS:
        # Its MET follows its session RPE (#3530), the general one without.
        met = energy.strength_met(rpe)
        kcal = (
            energy.strength_net_kcal(duration, rmr, met)
            if duration and rmr is not None
            else None
        )
        method = "met" if kcal is not None else "unknown"
        return SessionCost(day, kcal, method, steps, climb, rpe, met)
    kcal, method = _timed_cost(summary, sport, duration, rmr, hr_cost, mass, day)
    return SessionCost(day, kcal, method, steps, climb)


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


def read_energy_inputs(
    db: Any, sources: EnergySources, start: date, end: date
) -> tuple[EnergyInputs, tuple[SessionCost, ...]]:
    """The days from ``start`` to ``end`` and the priced sessions behind them."""
    profile = sources.profile()
    profile_weight = _number(profile.get("weight_kg"))
    health = sources.daily_health(db, start, end)
    rmr, rmr_source = _resting(sources, db, end, health)
    weights = dict(sources.weights(db, start, end))
    trend = weight_trend(weights)
    sessions = list(sources.sessions(db, start, end))
    step_m, step_source = _step_length(sessions, _height_m(profile))
    rides = [s for s in sessions if str(s.get("sport") or "") in RIDE_SPORTS]
    flex = _flex_fit(rides, rmr) if rmr is not None else None
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
            _mass(
                trend,
                date.fromisoformat(str(summary["started_at_local"])[:10]),
                profile_weight,
            ),
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
            trend, profile_weight, costs, step_m,
        )  # fmt: skip
        day += timedelta(days=1)
    grace_end = end + timedelta(days=BODY_GRACE_DAYS)
    body_fat = dict(sources.body_fat(db, start, grace_end))
    # The first window looks 90 days back from its own end.
    lookback = start - timedelta(days=RESTING_LOOKBACK_DAYS)
    device = dict(sources.device_body_fat(db, lookback, grace_end))
    inputs = EnergyInputs(days, body_fat, rmr, rmr_source, step_m, step_source)
    return replace(
        inputs,
        device_body_fat=device,
        profile_body_fat_pct=_profile_fat(profile.get("body_fat_pct")),
        skinfold_sd_pct=_skinfold_sds(sources.skinfolds(db, grace_end)),
    ), costs


def _skinfold_sds(measurements: Sequence[SkinfoldMeasurement]) -> dict[date, float]:
    """Each skinfold day's body fat SD from the athlete's repeatability and
    its own readings per site; none until the repeats suffice (#3556)."""
    own = repeatability(measurements)
    return {
        m.measured_on: sd
        for m in measurements
        if (sd := measurement_sd_pct(m, own)) is not None
    }


def skinfold_body_fats(
    measurements: Iterable[SkinfoldMeasurement], since: date
) -> dict[date, float]:
    """Every complete skinfold measurement on or after ``since`` with a body
    fat, by day (#3617 item 2): a reading followed by another within the same
    week counts too, not only the newest one per weekly checkpoint."""
    return {
        m.measured_on: m.body_fat_pct
        for m in measurements
        if m.measured_on >= since and m.body_fat_pct is not None
    }


def device_body_fats(
    observations: Iterable[Observation], skinfold_days: Iterable[date] = ()
) -> dict[date, tuple[float, Any]]:
    """The entered (provider None, #3402) or Garmin body fat per day (#3362)
    under the source rule of ``sourced``: Garmin's copies fill only a day
    without an entered value or a profile skinfold (``skinfold_days``, #3557).
    """
    days, _ = sourced(observations)
    skinfolds = set(skinfold_days)
    return {
        day: (value, None if source == APP_SOURCE else ProviderId.GARMIN)
        for day, (value, source) in sorted(days.items())
        if source == APP_SOURCE or day not in skinfolds
    }


def _profile_fat(value: Any) -> float | None:
    """The profile's body fat (comma decimals allowed), 1 to 70 %, else None."""
    try:
        number = float(str(value).replace(",", "."))
    except ValueError:
        return None
    return number if 1 <= number <= 70 else None


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
    the derived resting rate's."""
    fits: dict[str, LearnedEveryday | None] = {}
    for name, formula in LEARNED_FORMULAS.items():
        points = [
            (
                window.adaptive_tdee.kcal
                - window.activity_kcal_raw
                - window.tef_kcal
                - resting_energy(formula, body["lean_mass_kg"]),
                window.derived_rmr.band_kcal / BAND_Z,
            )
            for window, body in zip(windows, bodies, strict=True)
            if body is not None
            and window.adaptive_tdee is not None
            and window.derived_rmr is not None
            and window.activity_kcal_raw is not None
            and window.tef_kcal is not None
            # A window that fails the neighbour check (#3614) teaches nothing.
            and is_stable(window)
        ]
        fits[name] = fit_learned_everyday(points)
    return fits


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
        | skinfold_diagnostics(skinfolds, missing, end, bodies)
        # The walks behind NEAT's step length and their suspected causes (#3611).
        | {
            "step_walks": step_walk_counts(
                sources.sessions(db, history_start(end), end),
                _height_m(sources.profile()),
            ),
            "step_source": inputs.step_source,
        }
        # Formulas whose learned everyday fit lies below 0, shown as 0 (#3577).
        | {
            "learned_everyday_negative": sum(
                1
                for fit in learned_everyday(windows, bodies).values()
                if fit is not None and fit.raw_kcal < 0
            )
        }
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


# The formula rows' body fat sources, in their order (athlete decisions, #3362, #3402).
BODY_SOURCES = ("skinfold", "entered", "device", "profile")
_SKINFOLD_LABEL = "Hautfalten"
_ENTERED_LABEL = "Lokal"
_PROFILE_LABEL = "Profil"


def _newest_within(values: Mapping[date, Any], day: date) -> date | None:
    """The newest day of ``values`` in the 90 days up to the grace day after ``day``."""
    since = day - timedelta(days=RESTING_LOOKBACK_DAYS)
    until = day + timedelta(days=BODY_GRACE_DAYS)
    return max((d for d in values if since <= d <= until), default=None)


def _body_fat(inputs: EnergyInputs, day: date) -> dict[str, Any] | None:
    """The body fat in effect on ``day``: skinfold, entered, device or scale, profile."""
    if (on := _newest_within(inputs.body_fat_pct, day)) is not None:
        return {"source": "skinfold", "label": _SKINFOLD_LABEL,
                "measured_on": on, "body_fat_pct": inputs.body_fat_pct[on]}  # fmt: skip
    for source in ("entered", "device"):
        values = {
            d: value
            for d, value in inputs.device_body_fat.items()
            if (value[1] is None) is (source == "entered")
        }
        if (on := _newest_within(values, day)) is not None:
            fat, provider = values[on]
            label = (
                _ENTERED_LABEL
                if provider is None
                else provider_label(provider, SourceView.BODY_HISTORY)
            )
            return {"source": source, "label": label, "measured_on": on,
                    "body_fat_pct": fat}  # fmt: skip
    if inputs.profile_body_fat_pct is not None:
        return {"source": "profile", "label": _PROFILE_LABEL, "measured_on": None,
                "body_fat_pct": inputs.profile_body_fat_pct}  # fmt: skip
    return None


def resolve_body(
    inputs: EnergyInputs, trend: Mapping[date, float], day: date
) -> dict[str, Any] | None:
    """The body fat in effect on ``day`` with the trend weight's lean mass.

    A measurement up to ``BODY_GRACE_DAYS`` after ``day`` counts: the view
    ends yesterday because today's intake is partial, not the body (#3336).
    The body fat comes from the newest skinfold, else an entered one, else a
    device or scale, else the profile (#3362, #3402); ``source`` and ``label``
    name it.
    """
    weighed = [d for d in trend if d <= day]
    fat = _body_fat(inputs, day)
    if not weighed or fat is None:
        return None
    weight = trend[max(weighed)]
    try:
        lean = lean_mass_kg(weight, fat["body_fat_pct"])
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
        # Windows failing the neighbour check and their largest unexplained
        # difference, to the nearest 100 kcal (#3614).
        "unstable_windows": sum(1 for w in windows if not is_stable(w)),
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
    profile_weight: float | None,
    costs: Sequence[SessionCost],
    step_m: float | None,
) -> DayInputs:
    """One day's intake, flags, weigh-in and own activity (sessions + NEAT)."""
    today = [c for c in costs if c.day == day]
    steps, floors = _number(health.get("steps")), _number(health.get("floors"))
    mass = _mass(trend, day, profile_weight)
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

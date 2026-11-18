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
from backend.athlete.skinfolds import SkinfoldMeasurement
from backend.canonical.activity_detail import ActivityStreamSet
from backend.canonical.activity_summary import stored_activity_key
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


def recent_fat_pct(measurement: SkinfoldMeasurement | None, day: date) -> float | None:
    """A skinfold measurement's body fat if it lies within the lookback of ``day``.

    An older one yields to a newer measured body fat or to Garmin (Gitar, #3142).
    """
    if measurement is None:
        return None
    since = day - timedelta(days=RESTING_LOOKBACK_DAYS)
    return measurement.body_fat_pct if measurement.measured_on >= since else None


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
    """One canonical weigh-in per day: the last one read for that day.

    A weight the athlete entered (no provider) wins its day (#3252).
    """
    days: dict[date, float] = {}
    entered: set[date] = set()
    for o in observations:
        value = _number(o.value.value)
        if value is None or value <= 0:
            continue
        provenance = getattr(o, "provenance", None)
        own = provenance is not None and provenance.provider is None
        if own or o.observed_on not in entered:
            days[o.observed_on] = value
        if own:
            entered.add(o.observed_on)
    return days


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
        cost = (
            energy.running_cost_j_per_kg_m
            if sport in RUN_SPORTS
            else energy.walking_cost_j_per_kg_m
        )
        joules = sum(run * cost(grade) for run, grade in _graded(sources, db, summary))
        return SessionCost(
            day, mass * joules / energy.J_PER_KCAL, "minetti", steps, climb
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
        return SessionCost(day, kcal, method, steps, climb, rpe, met)
    kcal, method = _timed_cost(summary, sport, duration, rmr, hr_cost, mass, day)
    return SessionCost(day, kcal, method, steps, climb)


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
    """NEAT's step length and its source: recorded walks, else body height."""
    speeds, cadences = [], []
    for summary in sessions:
        if str(summary.get("sport") or "") not in WALK_SPORTS:
            continue
        duration, distance = _duration_s(summary), _number(summary.get("distance_m"))
        cadence = _number(summary.get("avg_cadence"))
        if duration and distance and cadence:
            speeds.append(distance / duration)
            cadences.append(STEPS_PER_STRIDE * cadence)
    step = energy.step_length_m(speeds, cadences, height_m)
    if step is None:
        return None, None
    return step, "walks" if speeds else "height"


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
    step_m, step_source = _step_length(
        sessions, height_cm / 100.0 if height_cm else None
    )
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
    ), costs


def device_body_fats(
    observations: Iterable[Observation],
) -> dict[date, tuple[float, Any]]:
    """A device's, scale's or the athlete's body fat per day, with its provider (#3362).

    The last one read for a day wins, except that an entered one (provider None,
    #3402) wins its day.
    """
    days: dict[date, tuple[float, Any]] = {}
    for o in observations:
        held = days.get(o.observed_on)
        if o.sport is None and (held is None or held[1] is not None):
            days[o.observed_on] = (float(o.value.value), o.provenance.provider)
    return days


def _profile_fat(value: Any) -> float | None:
    """The profile's body fat (comma decimals allowed), 1 to 70 %, else None."""
    try:
        number = float(str(value).replace(",", "."))
    except ValueError:
        return None
    return number if 1 <= number <= 70 else None


def energy_basis(db: Any, sources: EnergySources, end: date) -> dict[str, Any]:
    """The diagnostics counts of the 12-week view ending on ``end`` (#3100)."""
    inputs, costs = read_energy_inputs(db, sources, history_start(end), end)
    skinfolds, missing = skinfold_state(db, sources, inputs, end)
    windows = energy_windows(inputs, end)
    trend = weight_trend(
        {d: v.weight_kg for d, v in inputs.days.items() if v.weight_kg is not None}
    )
    bodies = [resolve_body(inputs, trend, w.end) for w in windows]
    week = sources.daily_health(db, end - timedelta(days=GARMIN_RESTING_DAYS), end)
    found = newest_resting(week, end)
    return (
        diagnostics(inputs, costs, windows)
        | skinfold_diagnostics(skinfolds, missing, end, bodies)
        # How many days back today's Garmin Ruhe comes from, None = none (#3443).
        | {"today_resting_days_back": (end - found[0]).days if found else None}
    )


# How far back today's Garmin Ruhe looks for a full day's resting value (#3443).
GARMIN_RESTING_DAYS: Final = 7


def newest_resting(
    health: Mapping[str, Mapping[str, Any]], day: date
) -> tuple[date, float] | None:
    """The newest day within ``GARMIN_RESTING_DAYS`` before ``day`` with
    Garmin resting kcal, and that value; today's own is still counting."""
    for back in range(1, GARMIN_RESTING_DAYS + 1):
        earlier = day - timedelta(days=back)
        resting = _number(health.get(earlier.isoformat(), {}).get("resting_kcal"))
        if resting and resting > 0:
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
        "latest_window_weighins": latest.weighins if latest else None,
        "latest_window_insufficient": latest.insufficient if latest else None,
        "windows": len(windows),
        "windows_with_estimate": sum(1 for w in windows if w.adaptive_tdee),
        # How each estimated window split fat and lean mass (#3477).
        "store_method_windows": {
            method: sum(1 for w in windows if w.store_method == method)
            for method in ("measured", "forbes")
        },
        "calibration_windows": latest.calibration_windows if latest else 0,
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
    activity = neat + sport if neat is not None and sport is not None else None
    error = (
        energy.activity_error_kcal(
            [(c.method, c.kcal) for c in today if c.kcal is not None], neat or 0.0
        )
        if activity is not None
        else 0.0
    )
    return DayInputs(
        intake_kcal=sum(_number(e.get("kcal")) or 0.0 for e in entries)
        if entries
        else None,
        protein_g=sum(_number(e.get("protein_g")) or 0.0 for e in entries),
        carbohydrate_g=sum(_number(e.get("carbs_g")) or 0.0 for e in entries),
        fat_g=sum(_number(e.get("fat_g")) or 0.0 for e in entries),
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
    )

"""Resting and daily expenditure per source, and which one fits the weight (#3099).

Katch-McArdle and Cunningham price the resting rate from the skinfold lean
mass, with the nominal 95 % band of card M13; their daily total adds the same
own activity and TEF (M11) to both, so they differ only in the resting formula.
The activity is the uncalibrated one: the calibration is fitted against the
adaptive TDEE, and the comparison would otherwise favour the formula rows over
the device. Garmin's resting and total values stand beside them as its
estimate, and "own" is the adaptive TDEE with the derived resting rate (#3100).

The fit compares each source's distance to the adaptive TDEE over the same
4-week windows. A source is marked best only with enough shared windows and a
lead over the next source beyond the adaptive TDEE's own mean band (athlete
decision on #3187); otherwise the sources are not yet distinguishable. A
missing basis stays None; there is never a fallback formula.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable, Container, Mapping, Sequence
from dataclasses import replace
from datetime import date, timedelta
from typing import Any, Final, cast

from backend.athlete.body_composition import (
    RestingFormula,
    resting_band,
    resting_energy,
)
from backend.athlete.body_records import weight_trend
from backend.athlete.profile import LEARNED_EVERYDAY_FIELD
from backend.canonical.vocabulary import ProviderId
from backend.errors import AppError
from backend.nutrition.energy_balance import (
    BAND_Z,
    SHORT_EDGE_DAYS,
    WINDOW_DAYS,
    Base,
    DayInputs,
    EnergyInputs,
    EnergyWindow,
    Estimate,
    LearnedEveryday,
    StoredEnergy,
    WeightChange,
    day_tef,
    edge_change,
    energy_density,
    energy_windows,
    history_start,
    is_complete,
    is_stable,
    period_window,
    t_quantile,
    twelve_week_base,
    window_t,
    windows_bias,
)
from backend.nutrition.energy_expected import (
    expected_total_kcal,
    planned_net_kcal,
    usual_kcal_per_hour,
)
from backend.nutrition.energy_guard import energy_notes
from backend.nutrition.energy_inputs import (
    EnergySources,
    learned_everyday,
    newest_resting,
    read_energy_inputs,
    resolve_body,
    skinfold_state,
)
from backend.nutrition.water_hint import water_hint_for_today
from backend.performance.activity_energy import TEF_ERROR_KCAL
from backend.performance.daily_health import complete_garmin_day
from backend.providers.source_labels import SourceView, provider_label

# The fit needs this many windows shared by every compared source (#3187).
MIN_SHARED_WINDOWS = 4
FORMULAS = {
    "katch_mcardle": RestingFormula.KATCH_MCARDLE,
    "cunningham": RestingFormula.CUNNINGHAM,
}
# The wearable's own resting and total estimate (Garmin today).
DEVICE = "device"
OWN = "own"

# Resting value, its band, daily total and its band; None where unknown.
type Prediction = tuple[float | None, float | None, float | None, float | None]


def _round(value: float | None) -> int | None:
    return None if value is None else round(value)


def _formula_prediction(
    formula: RestingFormula, body: Mapping[str, Any] | None, window: EnergyWindow
) -> Prediction:
    """Resting from lean mass, and the day with the same own activity and TEF."""
    if body is None:
        return None, None, None, None
    resting = resting_energy(formula, body["lean_mass_kg"])
    band = resting_band(formula, body["weight_kg"], body["lean_mass_kg"])
    activity, tef = window.activity_kcal_raw, window.tef_kcal
    if activity is None or tef is None:
        return resting, band, None, None
    # Each activity part and digestion carry their M11 errors, as M12's derived
    # rate does (#3552).
    total_band = math.hypot(band, window.activity_error_kcal_raw, TEF_ERROR_KCAL)
    return resting, band, resting + activity + tef, total_band


def _device_prediction(
    health: Mapping[str, Mapping[str, Any]],
    window: EnergyWindow,
    only: Container[date] | None = None,
) -> Prediction:
    """Garmin's mean resting and total values over the window's days, or over
    ``only`` those of them. A day whose summary ended early holds part of its
    day and leaves a mean of several days (#3597); the shown day keeps its own
    values and its Stand."""
    start = window.end - timedelta(days=window.days - 1)
    dates = [start + timedelta(days=n) for n in range(window.days)]
    days = [health.get(d.isoformat(), {}) for d in dates if only is None or d in only]
    if window.days > 1:
        days = [day for day in days if complete_garmin_day(day)]

    def mean(key: str) -> float | None:
        values = [
            v for day in days if isinstance(v := day.get(key), int | float) and v > 0
        ]
        return statistics.fmean(values) if values else None

    return mean("resting_kcal"), None, mean("calories"), None


def _device_label(health: Mapping[str, Mapping[str, Any]], window: EnergyWindow) -> str:
    """The providers of the window's daily totals, from the source labels the
    daily health read gives them (#3323); Garmin's while no day has one."""
    start = window.end - timedelta(days=window.days - 1)
    labels = {
        label
        for n in range(window.days)
        for label in str(
            health.get((start + timedelta(days=n)).isoformat(), {}).get("source") or ""
        ).split(", ")
        if label
    }
    return ", ".join(sorted(labels)) or provider_label(
        ProviderId.GARMIN, SourceView.DAILY_HEALTH
    )


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _garmin_sport(
    sessions: Sequence[Mapping[str, Any]], resting: float
) -> float | None:
    """One day's Garmin session kcal net of its resting rate, or None if unpriced.

    Only sessions Garmin recorded count: a ride on another head unit while the
    watch was off is not in Garmin's active kcal (Gitar on #3216).
    """
    net = 0.0
    for summary in sessions:
        if ProviderId.GARMIN.value not in (summary.get("record_ids") or {}):
            continue
        kcal = _number(summary.get("energy_kcal"))
        seconds = _number(summary.get("elapsed_duration_s")) or _number(
            summary.get("moving_duration_s")
        )
        if not kcal or not seconds:
            return None
        net += max(0.0, kcal - resting / 1440.0 * seconds / 60.0)
    return net


def _device_split(
    health: Mapping[str, Mapping[str, Any]],
    sessions: Sequence[Mapping[str, Any]],
    window: EnergyWindow,
    projected_resting: float | None = None,
) -> tuple[float | None, float | None]:
    """Garmin's everyday (NEAT) and sport kcal per day over the window.

    Sport is Garmin's recorded session kcal net of its resting rate over their
    duration; everyday is the active kcal minus it. A day counts only with
    resting and active kcal and every Garmin session priced; everyday below
    zero is not a valid split and stays None (split A, athlete decision
    2026-11-16). Today the row's projected resting replaces the day's own,
    which is still counting (Gitar, #3443).
    """
    start = window.end - timedelta(days=window.days - 1)
    by_day: dict[str, list[Mapping[str, Any]]] = {}
    for summary in sessions:
        day_key = str(summary.get("started_at_local") or "")[:10]
        by_day.setdefault(day_key, []).append(summary)
    everyday, sport = [], []
    for offset in range(window.days):
        key = (start + timedelta(days=offset)).isoformat()
        day = health.get(key, {})
        resting = projected_resting or _number(day.get("resting_kcal"))
        active = _number(day.get("active_kcal"))
        # A day of several whose summary ended early holds part of it (#3597).
        partial = window.days > 1 and not complete_garmin_day(day)
        if partial or not resting or resting < 0 or active is None or active < 0:
            continue
        day_sport = _garmin_sport(by_day.get(key, []), resting)
        if day_sport is not None:
            sport.append(day_sport)
            everyday.append(active - day_sport)
    if not sport:
        return None, None
    neat = statistics.fmean(everyday)
    return (neat if neat >= 0 else None), statistics.fmean(sport)


def _expected_today(
    rows: Sequence[Mapping[str, Any]],
    outlined: str | None,
    latest: EnergyWindow,
    planned_kcal: float | None,
    device_inputs: tuple[Mapping[str, Mapping[str, Any]], Sequence[Mapping[str, Any]]],
) -> int | None:
    """The outlined row's expected day total; Garmin's row takes its own
    everyday mean, not M11's (Gitar, #3917)."""
    device = outlined == DEVICE
    return expected_total_kcal(
        next((row for row in rows if row["source"] == outlined), None),
        latest,
        planned_kcal,
        device,
        _device_split(*device_inputs, latest)[0] if device else None,
    )


def _comparison(
    errors: Mapping[str, Sequence[float | None]], windows: Sequence[EnergyWindow]
) -> tuple[dict[str, float], str | None, dict[str, Any]]:
    """Mean errors over the shared windows, the best source and the fit basis.

    Only sources with any error are compared, each over the windows where all
    of them have one. The best needs MIN_SHARED_WINDOWS such windows and a lead
    over the next source beyond the mean band of the adaptive TDEE.
    """
    compared = [
        name for name, found in errors.items() if any(e is not None for e in found)
    ]
    shared = [
        index
        for index in range(len(windows))
        if compared and all(errors[name][index] is not None for name in compared)
    ]
    means = {
        name: statistics.fmean(errors[name][i] or 0.0 for i in shared)
        for name in compared
        if shared
    }
    bands = [
        tdee.band_kcal for i in shared if (tdee := windows[i].adaptive_tdee) is not None
    ]
    band = statistics.fmean(bands) if bands else None
    best = None
    if band is not None and len(shared) >= MIN_SHARED_WINDOWS and len(means) > 1:
        first, second = sorted(means, key=means.__getitem__)[:2]
        if means[second] - means[first] > band:
            best = first
    fit = {
        "shared_windows": len(shared),
        "windows": len(windows),
        "min_windows": MIN_SHARED_WINDOWS,
        "reference_band_kcal": _round(band),
    }
    return means, best, fit


# Which parts of a row's split are computed alike for every row ("gleich
# berechnet", orchestrator rule of 2026-11-16): the formula rows share the own
# everyday, sport and TEF; the own row shares TEF always and everyday and sport
# only while the calibration factor is 1; Garmin computes its own.
NOT_SHARED: Final = {"neat": False, "sport": False, "tef": False}
FORMULA_SHARED: Final = {"neat": True, "sport": True, "tef": True}


def _split(
    resting: float | None,
    neat: float | None,
    sport: float | None,
    tef: float | None,
    shared: Mapping[str, bool],
) -> dict[str, Any]:
    """One row's day split and which of its parts every row computes alike."""
    return {
        "resting_kcal": _round(resting),
        "neat_kcal": _round(neat),
        "sport_kcal": _round(sport),
        "tef_kcal": _round(tef),
        "shared": dict(shared),
    }


def resting_plausible(
    derived: Estimate, reference: float | None, reference_band: float | None
) -> bool:
    """False only when the derived band lies entirely below the reference band.

    The residual carries every error of the logged intake and the modelled
    activity, so a value under any physiological resting rate is not shown as
    one (athlete decision 2026-11-16, #3255). Without a reference it stands.
    """
    if reference is None:
        return True
    return derived.kcal + derived.band_kcal >= reference - (reference_band or 0.0)


def _gated_resting(
    rmr: Estimate | None, own: Prediction, device: Prediction
) -> tuple[Estimate | None, bool]:
    """The residual to show and whether the gate held it back (#3255).

    The reference is the own Katch-McArdle with its band, else Garmin's mean
    resting value.
    """
    reference = own if own[0] is not None else device
    if rmr is None or resting_plausible(rmr, reference[0], reference[1]):
        return rmr, False
    return None, True


def _own_resting(
    latest: EnergyWindow,
    body: Mapping[str, Any] | None,
    health: Mapping[str, Mapping[str, Any]],
    base: Base | None = None,
) -> tuple[Estimate | None, bool]:
    """The own row's resting rate from the latest window, or from the 12-week
    ``base`` when given (#3687), gated against the window's Katch-McArdle
    (``body`` at its end), else Garmin (#3255, #3531)."""
    return _gated_resting(
        latest.derived_rmr if base is None else base.estimate,
        _formula_prediction(FORMULAS["katch_mcardle"], body, latest),
        _device_prediction(health, latest),
    )


def _base_for(
    windows: Sequence[EnergyWindow], base: Base | None, day: date
) -> Base | None:
    """The 12-week base for today and the days after the latest window (#3687);
    a past day keeps the window holding it (#3641)."""
    return base if windows and day > windows[-1].end else None


def _base_view(base: Base | None) -> dict[str, Any] | None:
    """The base's own resting rate with its band, the windows it averages,
    their age-weight sum and whether every one is unstable (#3687)."""
    if base is None:
        return None
    return {
        "kcal": _round(base.estimate.kcal),
        "band_kcal": _round(base.estimate.band_kcal),
        "windows": base.windows,
        "weight_sum": round(base.weight_sum, 2),
        "unstable": base.unstable,
    }


def _basis(base: Base | None) -> str | None:
    """What the own row's resting rate rests on: the 12-week base ("12w"), or
    flagged when every window in it is unstable ("12w_unstable"), else the
    day's own window (None)."""
    if base is None:
        return None
    return "12w_unstable" if base.unstable else "12w"


def own_day_totals(
    inputs: EnergyInputs,
    windows: Sequence[EnergyWindow],
    health: Mapping[str, Mapping[str, Any]],
    days: Sequence[date],
) -> dict[date, float | None]:
    """Each day's "Aus deinen Daten" day total, the diary's own day row for it
    (#3531): its window's resting rate plus the day's everyday, sport and
    digestion; None where a part is unknown, the resting rate is held back or no
    estimated window holds the day. The period balance takes its expenditure
    from it (athlete decision, #3600).

    A day's window is the estimated one holding it with the nearest middle, so
    the latest window's rate prices only its last 17 days and 12 weeks follow
    each window's (#3641); a day after the latest (today) takes the 12-week
    base (#3687).
    """
    trend = weight_trend(
        {d: v.weight_kg for d, v in inputs.days.items() if v.weight_kg is not None}
    )
    bodies = {w.end: resolve_body(inputs, trend, w.end) for w in windows}
    resting = {w.end: _own_resting(w, bodies[w.end], health) for w in windows}
    base = twelve_week_base(windows)
    totals: dict[date, float | None] = {}
    for day in days:
        window = _holding(windows, day)
        if window is None:
            totals[day] = None
            continue
        shown, implausible = resting[window.end]
        if (based := _base_for(windows, base, day)) is not None:
            shown, implausible = _own_resting(window, bodies[window.end], health, based)
        day_window = _day_window(inputs, window, day)
        row = _own_day_row(shown, implausible, window, day_window, [])
        totals[day] = row["total_kcal"]
    return totals


def _own_day(
    windows: Sequence[EnergyWindow],
    bodies: Sequence[Mapping[str, Any] | None],
    health: Mapping[str, Mapping[str, Any]],
    day: EnergyWindow,
    pending: list[str],
    reference: tuple[str, float, float | None] | None,
    base: Base | None = None,
) -> dict[str, Any]:
    """The diary's own day row, its resting rate from the day's own window as
    the period balance's (#3641), today from the 12-week base (#3687), split at
    the day's resting reference."""
    own = _holding(windows, day.end)
    based = _base_for(windows, base, day.end)
    shown, implausible = (
        (None, False)
        if own is None
        else _own_resting(own, bodies[windows.index(own)], health, based)
    )
    row = _own_day_row(shown, implausible, own or windows[-1], day, pending)
    row["basis"] = _basis(based)
    return _unmeasured(row, shown, reference)


def _holding(windows: Sequence[EnergyWindow], day: date) -> EnergyWindow | None:
    """The estimated window holding ``day`` whose middle is nearest to it (the
    newer on a tie), the latest for a day after it, else None (#3641)."""
    if day > windows[-1].end:
        return windows[-1]
    holding = [
        w
        for w in windows
        if w.derived_rmr is not None
        and w.end - timedelta(days=w.days - 1) <= day <= w.end
    ]
    return min(
        reversed(holding),
        key=lambda w: abs(2 * (w.end - day).days - (w.days - 1)),
        default=None,
    )


def _resting_reference(
    own: Prediction, device: Prediction
) -> tuple[str, float, float | None] | None:
    """The gate's resting reference (own Katch-McArdle, else Garmin) with its
    band and source, or None without either."""
    for source, prediction in (("katch_mcardle", own), (DEVICE, device)):
        if prediction[0] is not None:
            return source, prediction[0], prediction[1]
    return None


def _unmeasured(
    row: dict[str, Any],
    shown: Estimate | None,
    reference: tuple[str, float, float | None] | None,
) -> dict[str, Any]:
    """The own row with its residual split into Ruhe and "übriger
    Alltag" where it exceeds the resting reference (athlete decision, #3550).

    Ruhe is then the reference with its band; the rest carries the residual's
    band. The day total and every other row stay as they are.
    """
    split = row["split"]
    if shown is None or reference is None or split is None:
        return row
    source, rest, band = reference
    # A residual within half a kcal of the reference is not split (#3550).
    if shown.kcal - rest < 0.5:
        return row
    return row | {
        "resting_kcal": _round(rest),
        "resting_band_kcal": _round(band),
        "split": split
        | {
            "resting_kcal": _round(rest),
            "resting_source": source,
            "unmeasured_kcal": _round(shown.kcal - rest),
            "unmeasured_band_kcal": _round(shown.band_kcal),
        },
    }


def _neat_basis(inputs: EnergyInputs, window: EnergyWindow) -> dict[str, Any] | None:
    """NEAT's inputs per day over the window's days with steps (#3255)."""
    start = window.end - timedelta(days=window.days - 1)
    days = [
        day
        for when, day in inputs.days.items()
        if start <= when <= window.end and day.steps is not None
    ]
    if not days:
        return None
    floors = [day.floors for day in days if day.floors is not None]
    return {
        "days": len(days),
        "steps_per_day": round(statistics.fmean(day.steps or 0.0 for day in days)),
        "session_steps_per_day": round(
            statistics.fmean(day.session_steps for day in days)
        ),
        "floors_per_day": round(statistics.fmean(floors), 1) if floors else None,
        "step_length_m": None if inputs.step_m is None else round(inputs.step_m, 2),
        "step_length_source": inputs.step_source,
    }


def _flagged_view(
    windows: Sequence[EnergyWindow], bias: tuple[float, float] | None
) -> dict[str, Any]:
    """The meals flagged as estimated (#3100): the expenditure of the latest clean
    window, the learned correction of the latest window and the bias.

    Only a window without any flagged day gives the value without estimates
    (#3187 item 14); without one there is no clean period. The correction and
    the bias are per flagged entry (#3195) and exist only once b is estimated.
    """
    latest = windows[-1]
    clean = next((w for w in reversed(windows) if w.adaptive_tdee_unflagged), None)
    unflagged = None if clean is None else clean.adaptive_tdee_unflagged
    corrected = latest.adaptive_tdee_corrected
    return {
        "days": latest.flagged_days,
        "unflagged": None
        if unflagged is None or clean is None
        else {
            "total_kcal": _round(unflagged.kcal),
            "total_band_kcal": _round(unflagged.band_kcal),
            "end": clean.end.isoformat(),
        },
        "corrected": None
        if corrected is None
        else {
            "total_kcal": _round(corrected.kcal),
            "total_band_kcal": _round(corrected.band_kcal),
        },
        "bias_pct": None if bias is None else _round(bias[0]),
        "bias_band_pct": None if bias is None else _round(bias[1]),
    }


def energy_view(
    inputs: EnergyInputs,
    windows: Sequence[EnergyWindow],
    health: Mapping[str, Mapping[str, Any]],
    sessions: Sequence[Mapping[str, Any]] = (),
    bias: tuple[float, float] | None = None,
    body_missing: str | None = None,
    day: date | None = None,
    goal: Mapping[str, str] | None = None,
    fit_inputs: EnergyInputs | None = None,
    fit_days: int | None = None,
    planned_kcal: float | None = None,
) -> dict[str, Any]:
    """The source rows of ``day``, each source's fit over the windows, and the basis.

    ``planned_kcal`` is today's planned units' net kcal (#3917); with it, today's
    read carries the expected day total.

    ``body_missing`` is why the formula rows have no body (#3336), shown with them.
    ``day`` (default the latest window's end) gives the formula and device rows
    that day's resting rate, everyday, sport and digestion (athlete decision,
    #3395); "Aus deinen Daten" takes its resting rate from the latest 28-day
    window and the day's other parts like the formula rows, and its 28-day row
    with every part's mean is ``own_window``, for Verlauf (#3531). ``goal`` is
    the profile's energy goal, priced on the shown day (#3547). ``fit_inputs``
    is the longer history of Verlauf's 6- or 12-month period (#3704, #3839),
    reaching back ``fit_days`` (default the longest period); the daily view
    reads ``inputs`` only.
    """
    trend = weight_trend(
        {d: v.weight_kg for d, v in inputs.days.items() if v.weight_kg is not None}
    )
    latest = windows[-1]
    bodies = [resolve_body(inputs, trend, w.end) for w in windows]
    predictions: dict[str, list[Prediction]] = {
        name: [
            _formula_prediction(f, b, w) for b, w in zip(bodies, windows, strict=True)
        ]
        for name, f in FORMULAS.items()
    }
    predictions[DEVICE] = [_device_prediction(health, w) for w in windows]
    errors = {
        name: [
            abs(total - w.adaptive_tdee.kcal)
            if total is not None and w.adaptive_tdee is not None
            else None
            for (_, _, total, _), w in zip(values, windows, strict=True)
        ]
        for name, values in predictions.items()
    }
    means, best, fit = _comparison(errors, windows)
    day_window = _day_window(inputs, latest, day or latest.end)
    day_body = resolve_body(inputs, trend, day_window.end)
    device_prediction, device_rest = _device_day(
        health, day_window, day_window.end > latest.end
    )
    day_window, pending = _so_far(
        day_window,
        inputs.days.get(day_window.end),
        sessions,
        health.get(day_window.end.isoformat(), {}),
        day_window.end > latest.end,
    )
    day_predictions = {
        **{
            name: _formula_prediction(f, day_body, day_window)
            for name, f in FORMULAS.items()
        },
        DEVICE: device_prediction,
    }
    # Past days keep their own resting; device_rest is empty for them.
    device_neat, device_sport = _device_split(
        health, sessions, day_window, device_prediction[0] if device_rest else None
    )
    stand = wellness_stand(health.get(day_window.end.isoformat(), {}), sessions)
    # A summary that leaves a session out has no everyday yet (#3530), on a
    # past day either, so the split stays within the total (#3612).
    lagging = bool((stand or {}).get("behind"))
    device_neat = None if lagging else device_neat
    day_predictions[DEVICE], behind = _garmin_behind(
        device_prediction, device_neat, device_sport, bool(device_rest) or lagging
    )
    pending[1].extend(behind)
    rows = []
    for name in predictions:
        resting, resting_band_kcal, total, total_band = day_predictions[name]
        # Formula rows add the same uncalibrated own everyday, sport and TEF
        # (M11, #3220) as their daily total; Garmin splits its own day.
        if resting is None:
            split = None
        elif name == DEVICE:
            split = _split(resting, device_neat, device_sport, None, NOT_SHARED)
        else:
            split = _split(
                resting,
                day_window.neat_kcal_raw,
                day_window.sport_kcal_raw,
                day_window.tef_kcal,
                FORMULA_SHARED,
            )
        rows.append(
            {
                "source": name,
                "resting_kcal": _round(resting),
                "resting_band_kcal": _round(resting_band_kcal),
                "total_kcal": _round(total),
                "total_band_kcal": _round(total_band),
                "errors_kcal": [_round(e) for e in errors[name]],
                "mean_error_kcal": _round(means.get(name)),
                "split": split,
                "pending_parts": pending[name == DEVICE],
            }
        )
    # Ranked on the raw rows, before the learned term enters them (#3613).
    outlined = _outlined(rows, best)
    # The formula rows' learned everyday term (#3577): it enters their shown
    # day totals, provisionally until the athlete's check (#3723), never the
    # fit above.
    learned = learned_everyday(windows, bodies)
    checked = (goal or {}).get(LEARNED_EVERYDAY_FIELD) or None
    _add_learned(rows, learned, checked)
    # Only the device row is named by the read; the client names the others.
    rows[list(predictions).index(DEVICE)].update(
        label=_device_label(health, day_window), **device_rest
    )
    base = twelve_week_base(windows)
    shown, implausible = _own_resting(latest, bodies[-1], health)
    # The day row splits at the reference shown beside it that day; the
    # 28-day row at the latest window's, as the gate (#3550).
    reference = _resting_reference(
        predictions["katch_mcardle"][-1], predictions[DEVICE][-1]
    )
    rows.append(
        _own_day(
            windows,
            bodies,
            health,
            day_window,
            pending[False],
            _resting_reference(
                day_predictions["katch_mcardle"], day_predictions[DEVICE]
            ),
            base,
        )
    )
    return {
        "end": latest.end.isoformat(),
        # The daily estimate's 12-week base (#3687), named "12-Wochen-Basis".
        "base": _base_view(base),
        # The day the source rows show; after ``end`` (today) it is partial.
        "day": day_window.end.isoformat(),
        "day_partial": day_window.end > latest.end,
        "windows": [w.end.isoformat() for w in windows],
        "rows": rows,
        "best": best,
        # The one source the diary outlines, the goal prices and Verlauf's
        # sentence names for both periods (athlete decision, #3613).
        "outlined": outlined,
        "fit": fit,
        "flagged": _flagged_view(windows, bias),
        "basis": {
            "days": latest.days,
            "complete_days": latest.complete_days,
            "flagged_days": latest.flagged_days,
            "weighins": latest.weighins,
            "insufficient": latest.insufficient,
            "windows_with_estimate": sum(1 for w in windows if w.adaptive_tdee),
            "rmr_source": latest.rmr_source,
            "neat": _neat_basis(inputs, day_window),
        },
        # "Welche Quelle trifft dein Gewicht?" per Verlauf period (#3397).
        # A period inside the 12-week read takes it; 6 Monate needs its own read,
        # given only when Verlauf asks for it (#3711).
        "fit_scale": {
            key: _fit_scale(given, trend, health, latest, days, goal, learned)
            for key, (given, days) in _period_inputs(
                inputs, fit_inputs, latest.end, fit_days
            ).items()
        },
        "skinfold_missing": body_missing,
        "body": _public_body(day_body),
        # The 28-day row with every part's mean, shown in Verlauf only (#3531).
        "own_window": _unmeasured(
            _own_window_row(shown, implausible, latest), shown, reference
        ),
        "goal": _energy_goal(rows, outlined, goal, inputs.days.get(day_window.end)),
        # Today's total with the usual steps and digestion and the planned
        # units; it informs, "Ziel heute" stays on the total so far (#3917).
        "expected_total_kcal": _expected_today(
            rows, outlined, latest, planned_kcal, (health, sessions)
        )
        if day_window.end > latest.end
        else None,
        # Energy availability and the weight's loss rate, non-blocking (#3917).
        **energy_notes(inputs, latest, bodies[-1]),
        # Per formula row, with its sign kept in raw_kcal; checked is the
        # date the athlete took it over, else None (#3577).
        "learned_everyday": {
            **{name: _learned_view(fit) for name, fit in learned.items()},
            "checked": checked,
        },
        # Where Garmin's day summary of the shown day stands (#3530).
        "wellness": stand,
        "deficit": _deficit(
            latest, predictions[DEVICE][-1][2], goal, _short_edges(inputs, latest)
        ),
        # Without enough logged days, the intake from weight and activity (#3604).
        "estimated_intake": _estimated_intake(
            latest, windows, bodies, learned.get(ESTIMATE_FORMULA)
        ),
    }


# The deficit card's distance to the goal, in kcal per day either way (athlete
# decision, #3606): within the first it is in the goal ("near", green), up to the
# second "mid" (orange), beyond "far" (red).
GOAL_NEAR_KCAL: Final = 150
GOAL_MID_KCAL: Final = 350


def _goal_status(
    deficit: float, goal: Mapping[str, str] | None
) -> dict[str, Any] | None:
    """The 28-day deficit against the goal (#3606): ``in`` within
    ``GOAL_NEAR_KCAL`` either way, else ``under`` or ``over`` the goal's deficit
    (surplus for build), with the distance class that colours the card."""
    kind = (goal or {}).get("energy_goal") or ""
    if kind not in _GOAL_SIGN:
        return None
    try:
        kcal = round(float((goal or {}).get("energy_goal_kcal") or 0)) * abs(
            _GOAL_SIGN[kind]
        )
    except ValueError:
        kcal = 0
    # Lose and hold count a deficit up, build a surplus.
    gap = (-deficit if kind == "build" else deficit) - kcal
    status = "in"
    if abs(gap) > GOAL_NEAR_KCAL:
        status = "over" if gap > 0 else "under"
    distance = "near" if abs(gap) <= GOAL_NEAR_KCAL else "mid"
    if abs(gap) > GOAL_MID_KCAL:
        distance = "far"
    return {"goal": kind, "goal_kcal": kcal, "status": status, "distance": distance}


def _measured_terms(store: StoredEnergy, period_start: date) -> dict[str, Any]:
    """The deficit card's measured values, each null when missing: the body fat
    readings of the shown period and their days, and the window's last weigh-in.

    The fat share rests on the phase's readings, which may reach back beyond the
    period; the card compares only readings of the period itself (#4037). A
    first reading before the period leaves no comparison, and a last reading
    before it too is named by ``fat_last_only`` as "Letzte Messung dd.mm.".
    """

    def one(value: float | None) -> float | None:
        return None if value is None else round(value, 1)

    def day(value: date | None) -> str | None:
        return None if value is None else value.isoformat()

    before = store.fat_date_start is not None and store.fat_date_start < period_start
    outside = store.fat_date_end is not None and store.fat_date_end < period_start
    return {
        "fat_pct_start": None if before else one(store.fat_pct_start),
        "fat_pct_end": one(store.fat_pct_end),
        "fat_date_start": None if before else day(store.fat_date_start),
        "fat_date_end": day(store.fat_date_end),
        "fat_last_only": outside,
        "weight_last_kg": one(store.weight_last_kg),
        "weight_date_start": day(store.weight_date_start),
        "weight_date_end": day(store.weight_date_end),
    }


def _short_edges(inputs: EnergyInputs, window: EnergyWindow) -> WeightChange | None:
    """The 4 weeks' shown weights: the means of the weigh-ins the store rests
    on in their first and last ``SHORT_EDGE_DAYS`` (athlete decision, #3739);
    None for a longer period, whose store carries its own edges."""
    store = window.store
    if window.days > WINDOW_DAYS or store is None or store.weight_date_start is None:
        return None
    low, high = store.weight_date_start, store.weight_date_end or window.end
    weights = {
        d: v.weight_kg
        for d, v in inputs.days.items()
        if v.weight_kg is not None and low <= d <= high
    }
    start = window.end - timedelta(days=window.days - 1)
    return edge_change(weights, start, window.end, SHORT_EDGE_DAYS) if weights else None


def _deficit_t(window: EnergyWindow, store: StoredEnergy) -> float:
    """The deficit band's t: an edge store's pooled dof (#3739; none to pool
    means one weigh-in's assumed SD, so z), else the TDEE's own (#3614)."""
    if not store.edge_weighins:
        return window_t(window)
    dof = sum(store.edge_weighins) - 2
    return t_quantile(dof) if dof else BAND_Z


def _deficit(
    latest: EnergyWindow,
    device_total: float | None,
    goal: Mapping[str, str] | None,
    shown: WeightChange | None = None,
) -> dict[str, Any] | None:
    """'Dein Defizit, letzte 28 Tage' (#3548): the energy per day from (deficit)
    or into (surplus) the body stores, with the terms of its boxes. The weigh-ins
    alone carry it; without enough logged meals the expenditure is null and
    ``expenditure_missing`` names why ("Nur Waage", #3603). ``shown`` replaces
    the store's start, end and change on the card (the 4 weeks' edge means,
    #3739); the deficit and its band stay the store's."""
    store, tdee = latest.store, latest.adaptive_tdee
    if store is None:
        return None
    deficit, band = -store.kcal_per_day, _deficit_t(latest, store) * store.sd
    if shown:
        store = store._replace(
            weight_start_kg=shown.first_kg,
            weight_end_kg=shown.first_kg + shown.slope * shown.span,
            fat_change_kg=shown.slope * shown.span * store.fat_share,
            weight_date_start=shown.day_first,
            weight_date_end=shown.day_last,
            edge_weighins=shown.edges,
        )
    change = store.weight_end_kg - store.weight_start_kg
    return {
        "days": latest.days,
        "deficit_kcal": _round(deficit),
        "band_kcal": _round(band),
        "method": store.method,
        "fat_share": _fat_share_view(store),
        "span_days": store.days,
        "weight_start_kg": round(store.weight_start_kg, 1),
        "weight_end_kg": round(store.weight_end_kg, 1),
        # The start and end are edge means with their weigh-in counts: 7 days
        # for 4 weeks, 14 for longer periods (#3739); null where the edges do
        # not come apart and the line's ends show.
        "edge_weighins": list(store.edge_weighins) if store.edge_weighins else None,
        # The ends' unrounded change, so the boxes add up (#3618).
        "weight_change_kg": round(change, 1),
        # kcal per kg of weight change at the blended share, for the goal's
        # weekly weight (#3618).
        "kcal_per_kg": _round(energy_density(store.fat_share)),
        **_measured_terms(store, latest.end - timedelta(days=latest.days - 1)),
        "fat_change_kg": round(store.fat_change_kg, 1),
        # The fat share of the weight change (#3555).
        "fat_share_pct": round(100 * store.fat_change_kg / change)
        if abs(change) >= 0.05
        else None,
        "stored_kcal": _round(store.kcal_per_day * store.days),
        # An unstable window names the weight step first (#3614).
        "uncertainty": store.uncertainty if latest.stable else "unstable",
        "total_kcal": _round(tdee.kcal) if tdee else None,
        "expenditure_missing": None if tdee else latest.insufficient,
        "device_total_kcal": _round(device_total),
        "goal": _goal_status(deficit, goal),
    }


# "Nur Waage", step 2 (#3604): the estimated intake rests on Katch-McArdle, the
# reference row, and needs a calibration from a logged window at most 9 weeks
# before the latest (the resting rate adapts over a lasting deficit; the
# 12-week view reaches 11), with the weight and activity close to that window's.
ESTIMATE_FORMULA: Final = "katch_mcardle"
ESTIMATE_MAX_AGE_DAYS: Final = 63
ESTIMATE_MAX_WEIGHT_CHANGE: Final = 0.04
ESTIMATE_MAX_ACTIVITY_CHANGE: Final = 0.30


def _calibration(
    windows: Sequence[EnergyWindow], bodies: Sequence[Mapping[str, Any] | None]
) -> list[EnergyWindow]:
    """The windows the learned everyday term was fitted over (#3577)."""
    return [
        w
        for w, body in zip(windows, bodies, strict=True)
        if body is not None
        and w.adaptive_tdee is not None
        and w.derived_rmr is not None
        and w.activity_kcal_raw is not None
        and w.tef_kcal is not None
        and w.store is not None
        and is_stable(w)
    ]


def _estimate_missing(
    latest: EnergyWindow,
    body: Mapping[str, Any] | None,
    calibration: Sequence[EnergyWindow],
    fit: LearnedEveryday | None,
) -> str | None:
    """Why the latest window gets no estimated intake, or None (#3604)."""
    if not calibration or fit is None:
        return "calibration"
    newest = calibration[-1]
    if (latest.end - newest.end).days > ESTIMATE_MAX_AGE_DAYS:
        return "calibration_age"
    if body is None or latest.activity_kcal_raw is None:
        return "body" if body is None else "activity"
    store, before = cast(StoredEnergy, latest.store), cast(StoredEnergy, newest.store)
    if abs(store.weight_end_kg / before.weight_end_kg - 1) > ESTIMATE_MAX_WEIGHT_CHANGE:
        return "weight_change"
    activity = statistics.fmean(cast(float, w.activity_kcal_raw) for w in calibration)
    if (
        abs(latest.activity_kcal_raw - activity)
        > ESTIMATE_MAX_ACTIVITY_CHANGE * activity
    ):
        return "activity_change"
    return None


def _estimated_intake(
    latest: EnergyWindow,
    windows: Sequence[EnergyWindow],
    bodies: Sequence[Mapping[str, Any] | None],
    fit: LearnedEveryday | None,
) -> dict[str, Any] | None:
    """The average intake without logged meals (#3604): the calibrated
    expenditure less the energy from the stores; None with enough logged days.

    The expenditure is Katch-McArdle's resting rate plus its learned everyday
    term (signed) plus the calibration windows' mean activity, with k on the
    activity's change from that mean: the term was learned at k = 1, so k
    times the whole activity would count its excess twice. Digestion is the
    calibration windows' share of their intake, so the intake solves for it.
    The band holds the store's, the learned term's (which holds the logged
    windows' activity and TDEE errors) and digestion's.
    """
    if latest.adaptive_tdee is not None or latest.store is None:
        return None
    calibration = _calibration(windows, bodies)
    reason = _estimate_missing(latest, bodies[-1], calibration, fit)
    newest = calibration[-1] if calibration else None
    view: dict[str, Any] = {
        "kcal": None,
        "band_kcal": None,
        "calibration_end": newest.end.isoformat() if newest else None,
        "calibration_age_days": (latest.end - newest.end).days if newest else None,
        "missing": reason,
    }
    if reason is not None:
        return view
    fit, body = cast(LearnedEveryday, fit), cast(Mapping[str, Any], bodies[-1])
    activity = statistics.fmean(cast(float, w.activity_kcal_raw) for w in calibration)
    eaten = sum(cast(float, w.intake_kcal) for w in calibration)
    share = sum(cast(float, w.tef_kcal) for w in calibration) / eaten
    spent = (
        resting_energy(FORMULAS[ESTIMATE_FORMULA], body["lean_mass_kg"])
        + fit.raw_kcal
        + activity
        + latest.calibration_factor * (cast(float, latest.activity_kcal_raw) - activity)
    )
    band = math.hypot(
        window_t(latest) * latest.store.sd, fit.band_kcal, TEF_ERROR_KCAL
    ) / (1 - share)
    return {
        **view,
        "kcal": _round((spent + latest.store.kcal_per_day) / (1 - share)),
        "band_kcal": _round(band),
    }


# The signed daily kcal of each goal kind (#3547).
_GOAL_SIGN = {"lose": -1, "hold": 0, "build": 1}


def _outlined(rows: Sequence[Mapping[str, Any]], best: str | None) -> str | None:
    """The row the diary outlines (#3397): the best source, else the nearest
    one by mean error; the own row never."""
    if best:
        return best
    ranked = [
        r for r in rows if r["source"] != "own" and r["mean_error_kcal"] is not None
    ]
    return min(ranked, key=lambda r: r["mean_error_kcal"])["source"] if ranked else None


def _add_learned(
    rows: Sequence[dict[str, Any]],
    learned: Mapping[str, LearnedEveryday | None],
    checked: str | None,
) -> None:
    """Each formula row with an estimate carries its learned everyday term
    (#3577, #3723): a split part of its own and in the day total. Once
    ``checked`` and qualified (#3689) it counts with its sign, a negative one
    as a reduction; until then the provisional point estimate floored at 0,
    marked ``learned_provisional``."""
    for row in rows:
        fit = learned.get(str(row["source"]))
        if fit is None or row["split"] is None:
            continue
        applied = bool(checked) and fit.qualified
        kcal = _learned_kcal(fit, applied)
        row["split"] = {
            **row["split"],
            "learned_kcal": kcal,
            "learned_band_kcal": round(fit.band_kcal),
            "learned_provisional": not applied,
        }
        if row["total_kcal"] is not None:
            row["total_kcal"] += kcal
        # Its uncertainty widens the day total's band (review, #3577).
        if row["total_band_kcal"] is not None:
            row["total_band_kcal"] = round(
                math.hypot(row["total_band_kcal"], fit.band_kcal)
            )


def _learned_view(fit: LearnedEveryday | None) -> dict[str, Any] | None:
    if fit is None:
        return None
    return {
        "kcal": round(fit.kcal),
        "raw_kcal": round(fit.raw_kcal),
        "band_kcal": round(fit.band_kcal),
        "windows": fit.windows,
        # Stable windows sharing no day, and whether it may be offered and
        # applied; until then the line reads "noch nicht gelernt" (#3689).
        "independent": fit.independent,
        "qualified": fit.qualified,
    }


def _energy_goal(
    rows: Sequence[Mapping[str, Any]],
    source: str | None,
    profile: Mapping[str, str] | None,
    day: DayInputs | None,
) -> dict[str, Any] | None:
    """'Offene Kalorien' (#3547): the outlined source's day total (so far
    today) with the goal's deficit or surplus, less what was eaten."""
    kind = (profile or {}).get("energy_goal") or ""
    if kind not in _GOAL_SIGN:
        return None
    try:
        kcal = round(float((profile or {}).get("energy_goal_kcal") or 0))
    except ValueError:
        kcal = 0
    kcal *= abs(_GOAL_SIGN[kind])
    total = next((r["total_kcal"] for r in rows if r["source"] == source), None)
    eaten = round(day.intake_kcal or 0) if day else 0
    target = None if total is None else total + _GOAL_SIGN[kind] * kcal
    return {
        "goal": kind,
        "goal_kcal": kcal,
        "source": source if total is not None else None,
        "total_kcal": total,
        "target_kcal": target,
        "eaten_kcal": eaten,
        "remaining_kcal": None if target is None else target - eaten,
    }


def _own_day_row(
    shown: Estimate | None,
    implausible: bool,
    latest: EnergyWindow,
    day: EnergyWindow,
    pending: list[str],
) -> dict[str, Any]:
    """ "Aus deinen Daten" for the diary's day (athlete decision, #3531): the own
    resting rate from the 28-day window with its band, and the day's everyday,
    sport and digestion exactly as the formula rows; the day total is their sum,
    with the resting rate's band."""
    resting = shown.kcal if shown else None
    activity, tef = day.activity_kcal_raw, day.tef_kcal
    total = (
        None
        if resting is None or activity is None or tef is None
        else resting + activity + tef
    )
    return {
        "source": OWN,
        "resting_kcal": _round(resting),
        "resting_band_kcal": _round(shown.band_kcal) if shown else None,
        "resting_implausible": implausible,
        "total_kcal": _round(total),
        "total_band_kcal": _round(shown.band_kcal)
        if total is not None and shown
        else None,
        # A held-back resting rate (#3255) keeps the day's other parts.
        "split": None
        if latest.derived_rmr is None
        else _split(
            resting, day.neat_kcal_raw, day.sport_kcal_raw, tef, FORMULA_SHARED
        ),
        "pending_parts": pending,
        # The days the resting rate comes from, shown as "Ruhe aus 28 Tagen".
        "window_days": latest.days,
        # The blended fat share of the weight change; None without an
        # estimate (#3555), even when the weigh-ins give a store (#3603).
        "fat_share": _fat_share_view(latest.store if latest.adaptive_tdee else None),
    }


def _own_window_row(
    shown: Estimate | None, implausible: bool, latest: EnergyWindow
) -> dict[str, Any]:
    """The 28-day row: every part's mean over the window, for Verlauf (#3531).

    Its resting rate is the residual of the calibrated terms; its everyday and
    sport equal the formula rows' only while k is 1.
    """
    tdee = latest.adaptive_tdee
    uncalibrated = math.isclose(latest.calibration_factor, 1.0)
    resting = shown.kcal if shown else None
    split = None
    if latest.derived_rmr:
        split = _split(
            resting,
            latest.neat_kcal,
            latest.sport_kcal,
            latest.tef_kcal,
            {"neat": uncalibrated, "sport": uncalibrated, "tef": True},
        )
    return {
        "source": OWN,
        "resting_kcal": _round(resting),
        "resting_band_kcal": _round(shown.band_kcal) if shown else None,
        "resting_implausible": implausible,
        "total_kcal": _round(tdee.kcal) if tdee else None,
        "total_band_kcal": _round(tdee.band_kcal) if tdee else None,
        "split": split,
        # A window method: its days, shown as "Ø pro Tag, letzte 28 Tage" (#3395, #3513).
        "window_days": latest.days,
        "fat_share": _fat_share_view(latest.store if latest.adaptive_tdee else None),
    }


def _fat_share_view(store: StoredEnergy | None) -> dict[str, int | str] | None:
    """The blended fat share in percent, its prior with its source and the
    measurements' weight, shown as "Forbes 57 %, zu X % gemessen" (#3555,
    #3684)."""
    if store is None:
        return None
    return {
        "share_pct": round(100 * store.fat_share),
        "prior_pct": round(100 * store.fat_prior),
        "prior_source": store.fat_prior_source,
        "measured_pct": round(100 * store.measured_weight),
    }


def _day_window(inputs: EnergyInputs, latest: EnergyWindow, day: date) -> EnergyWindow:
    """``day`` as a one-day window for the source rows (#3395).

    Its own everyday and sport (0 without a session), and digestion only from
    logged meals (None without any). The latest window itself when ``day`` is
    its end and no day was chosen keeps nothing else: the 28-day means are
    never shown for one day.
    """
    values = inputs.days.get(day, DayInputs())
    # A past day whose summary ended early has no known everyday (#3612);
    # today's so far is _so_far's.
    neat = None if values.unfinished and day <= latest.end else values.neat_kcal
    return cast(
        EnergyWindow,
        replace(
            latest,
            end=day,
            days=1,
            activity_kcal_raw=values.activity_kcal,
            activity_error_kcal_raw=values.activity_error_kcal,
            neat_kcal_raw=neat,
            sport_kcal_raw=values.sport_kcal,
            tef_kcal=day_tef(values) if values.entries else None,
        ),
    )


def selected_day(text: str | None, today: date) -> date | None:
    """The diary day the table shows: an ISO date up to ``today``, or None."""
    if not text:
        return None
    try:
        day = date.fromisoformat(text)
    except ValueError:
        raise AppError(400, "Das Datum fehlt oder ist ungültig.") from None
    if day > today:
        raise AppError(400, "Der Tag liegt in der Zukunft.")
    return day


# Verlauf's periods; the own estimate needs the rules' spans, so a period shorter
# than a window takes it from the latest window (orchestrator, #3397).
# Verlauf's four periods: 4 weeks, 3, 6 and 12 months (athlete decisions, #3704, #3839).
FIT_PERIODS: Final = {"28d": 28, "91d": 91, "182d": 182, "365d": 365}
# The periods past the 12-week read, each asked for by Verlauf alone (#3711).
LONG_PERIODS: Final = frozenset({"182d", "365d"})


def _period_inputs(
    inputs: EnergyInputs,
    fit_inputs: EnergyInputs | None,
    end: date,
    fit_days: int | None = None,
) -> dict[str, tuple[EnergyInputs, int]]:
    """Each Verlauf period's inputs: a period inside the 12-week read takes it;
    6 and 12 Monate need ``fit_inputs``, their own read of ``fit_days``, and a
    period it does not reach is left out (#3839)."""
    history_days = (end - history_start(end)).days + 1
    reach = fit_days or max(FIT_PERIODS.values())
    periods = {}
    for key, days in FIT_PERIODS.items():
        if days > reach:
            continue
        given = inputs if days <= history_days else fit_inputs
        if given is not None:
            periods[key] = (given, days)
    return periods


def _fit_scale(
    inputs: EnergyInputs,
    trend: Mapping[date, float],
    health: Mapping[str, Mapping[str, Any]],
    latest: EnergyWindow,
    days: int,
    goal: Mapping[str, str] | None = None,
    learned: Mapping[str, LearnedEveryday | None] | None = None,
) -> dict[str, Any]:
    """Each source's mean daily total over ``days`` against the own estimate,
    and the period's deficit card (#3704). A formula row with a learned
    everyday estimate also carries its total with it (#3723); its own total
    and the ranking stay raw.

    ``own_days`` names the span of the own value and its band. A period with
    its own value averages every source over that value's days: the complete
    days between its first and last weigh-in, named by ``span_start`` and
    ``span_end`` (#3613).
    """
    period = period_window(inputs, latest.end, days, latest.calibration_factor)
    deficit_window = latest if days == latest.days else period
    own = period if days >= latest.days else latest
    body = resolve_body(inputs, trend, latest.end)
    basis = period
    chosen: set[date] | None = None
    weighed: list[date] = []
    if own is period and period.adaptive_tdee is not None:
        logged = {
            d: v
            for n in range(days)
            if (v := inputs.days.get(d := latest.end - timedelta(days=n)))
        }
        weighed = sorted(d for d, v in logged.items() if v.weight_kg is not None)
        chosen = {
            d
            for d, v in logged.items()
            if weighed
            and weighed[0] <= d <= weighed[-1]
            and is_complete(v, inputs.rmr_kcal)
        }
        # Every weigh-in stays for the stored energy; only the chosen days keep
        # their meals and activity, for the own value and the sources alike.
        weights = {
            d: DayInputs(weight_kg=v.weight_kg)
            for d, v in inputs.days.items()
            if v.weight_kg is not None
        }
        only = cast(
            EnergyInputs,
            replace(inputs, days={**weights, **{d: logged[d] for d in chosen}}),
        )
        basis = own = period_window(only, latest.end, days, latest.calibration_factor)
    sources = [
        _with_learned(
            {
                "source": name,
                "total_kcal": _round(_formula_prediction(f, body, basis)[2]),
            },
            (learned or {}).get(name),
        )
        for name, f in FORMULAS.items()
    ]
    sources.append(
        {
            "source": DEVICE,
            "label": _device_label(health, period),
            "total_kcal": _round(_device_prediction(health, period, chosen)[2]),
        }
    )
    tdee = own.adaptive_tdee
    return {
        "days": days,
        "own_days": own.days,
        "span_start": weighed[0].isoformat() if weighed else None,
        "span_end": weighed[-1].isoformat() if weighed else None,
        "own_kcal": _round(tdee.kcal) if tdee else None,
        "own_band_kcal": _round(tdee.band_kcal) if tdee else None,
        "sources": sources,
        "weeks": days // 7,
        "weeks_with_data": _weeks_with_data(inputs, latest.end, days),
        # The same rules over the period's weigh-ins; the 28 days are the latest window.
        "deficit": _deficit(
            deficit_window,
            _device_prediction(health, deficit_window)[2],
            goal,
            _short_edges(inputs, deficit_window),
        ),
    }


def _learned_kcal(fit: LearnedEveryday, applied: bool) -> int:
    """The learned everyday a formula row counts (#3723): an applied term with
    its sign, else the provisional point estimate floored at 0."""
    kcal = round(fit.kcal)
    return kcal if applied else max(0, kcal)


def _with_learned(
    source: dict[str, Any], fit: LearnedEveryday | None
) -> dict[str, Any]:
    """A source-card formula row with its 'mit Alltag' total (#3723): the
    qualified term applies here before the athlete's check."""
    if fit is None or source["total_kcal"] is None:
        return source
    return {
        **source,
        "learned_total_kcal": source["total_kcal"] + _learned_kcal(fit, fit.qualified),
        "learned_qualified": fit.qualified,
    }


def _weeks_with_data(inputs: EnergyInputs, end: date, days: int) -> int:
    """The period's 7-day blocks holding at least one completely logged day."""
    return sum(
        1
        for week in range(days // 7)
        if any(
            is_complete(day, inputs.rmr_kcal)
            for n in range(7)
            if (day := inputs.days.get(end - timedelta(days=7 * week + n)))
        )
    )


def _so_far(
    window: EnergyWindow,
    day: DayInputs | None,
    sessions: Sequence[Mapping[str, Any]],
    garmin_day: Mapping[str, Any],
    today: bool,
) -> tuple[EnergyWindow, tuple[list[str], list[str]]]:
    """Today's parts so far and which of them have no data yet, for the
    formula rows and for Garmin (athlete decision, #3494).

    Only a part whose input has not arrived counts 0: everyday without any
    steps, sport without any session, digestion without any meal (otherwise
    the meals logged so far). An unknown part (an unpriced session, steps
    without a step length) stays unknown. Garmin's everyday and sport have
    no data while its day has no active kcal. A past day keeps its parts; one
    whose summary ended early shows its unknown everyday as missing (#3612).
    """
    day = day or DayInputs()
    if not today:
        return window, (["neat"] if day.unfinished else [], [])
    # The day summary lags behind the activity, so its everyday has not arrived
    # yet either (#3530): it ends before a session does, or, without its end
    # time, it has fewer day steps than the sessions' own.
    stand = wellness_stand(garmin_day, sessions)
    behind = (stand or {}).get("behind") or (
        day.steps is not None and day.session_steps > day.steps
    )
    absent = {
        "neat": (window.neat_kcal_raw is None and day.steps is None) or behind,
        "sport": window.sport_kcal_raw is None and not sessions,
        "tef": not day.entries,
    }
    # Lagging everyday counts 0 like any part not there yet (Gitar, #3532).
    neat = (
        0.0
        if behind
        else _part_so_far(window.neat_kcal_raw, day.neat_kcal, absent["neat"])
    )
    sport = _part_so_far(window.sport_kcal_raw, day.sport_kcal, absent["sport"])
    pending = [part for part, missing in absent.items() if missing]
    shown = replace(
        window,
        neat_kcal_raw=neat,
        sport_kcal_raw=sport,
        activity_kcal_raw=None if neat is None or sport is None else neat + sport,
        tef_kcal=window.tef_kcal if window.tef_kcal is not None else day_tef(day),
    )
    active = _number(garmin_day.get("active_kcal"))
    device = ["neat", "sport"] if active is None else []
    return cast(EnergyWindow, shown), (pending, device)


def _garmin_behind(
    prediction: Prediction,
    neat: float | None,
    sport: float | None,
    today: bool,
) -> tuple[Prediction, list[str]]:
    """Today Garmin's day summary can lag behind its activity: its active kcal
    is then below the session's, and everyday would come out negative. The
    row then counts the session so far and shows everyday as not there yet,
    instead of "–" (#3530); so does a past day whose summary ended before
    its session (#3612)."""
    resting = prediction[0]
    if not today or resting is None or neat is not None or sport is None:
        return prediction, []
    return (resting, None, resting + sport, None), ["neat"]


MINUTES_PER_DAY: Final = 1440


def wellness_stand(
    garmin_day: Mapping[str, Any], sessions: Sequence[Mapping[str, Any]]
) -> dict[str, Any] | None:
    """Where Garmin's day summary stands (#3530): its wellness end as "HH:MM"
    while the day is not finished, and whether a session of the day ends after
    it, so the summary's steps and active kcal leave that session out."""
    end = _number(garmin_day.get("wellness_end_min"))
    if end is None or end >= MINUTES_PER_DAY:
        return None
    later = False
    for summary in sessions:
        start = str(summary.get("started_at_local") or "")
        seconds = _number(summary.get("elapsed_duration_s")) or _number(
            summary.get("moving_duration_s")
        )
        if len(start) >= 16 and start[11:16].replace(":", "").isdigit():
            minute = int(start[11:13]) * 60 + int(start[14:16]) + (seconds or 0) / 60
            later = later or minute > end
    whole = int(end)
    return {"as_of": f"{whole // 60:02d}:{whole % 60:02d}", "behind": later}


def _part_so_far(
    window_value: float | None, day_value: float | None, absent: bool
) -> float | None:
    """The window's part, else 0 when its input has not arrived, else the day's."""
    if window_value is not None:
        return window_value
    return 0.0 if absent else day_value


def _device_day(
    health: Mapping[str, Mapping[str, Any]], window: EnergyWindow, today: bool
) -> tuple[Prediction, dict[str, Any]]:
    """Garmin's row for the shown day and where its Ruhe comes from.

    Today Garmin's resting value is still counting (#3443): the row takes the
    newest full day's within a week as Ruhe, plus today's active kcal so far,
    0 before Garmin has any (#3494); without one the Ruhe and the total stay
    unknown and the row names why.
    """
    if not today:
        return _device_prediction(health, window), {}
    day = window.end
    found = newest_resting(health, day)
    if found is None:
        return (None, None, None, None), {"resting_missing": "no_recent_resting"}
    earlier, resting = found
    active = _number(health.get(day.isoformat(), {}).get("active_kcal"))
    # Absent active kcal count 0 so far (#3494); a negative value is invalid.
    total = None if active is not None and active < 0 else resting + (active or 0.0)
    return (resting, None, total, None), {"resting_from": earlier.isoformat()}


def _public_body(body: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The formula rows' body fat and where it came from (#3362), or None."""
    if body is None:
        return None
    measured_on = body["measured_on"]
    return {
        "source": body["source"],
        "label": body["label"],
        "date": measured_on and measured_on.isoformat(),
        "body_fat_pct": round(body["body_fat_pct"], 1),
        "lean_mass_kg": round(body["lean_mass_kg"], 1),
    }


def read_own_day_totals(
    db: Any, sources: EnergySources, end: date, days: Sequence[date]
) -> dict[date, float | None]:
    """The own day totals of ``days`` (up to ``end``) from the 12-week history
    ending on ``end``, the same read the energy view makes (#3600)."""
    history = own_history(db, sources, end, min([history_start(end), *days]))
    return own_day_totals(*history, days)


# The days before a session whose balance its analysis names (#4136).
DAYS_BEFORE_SESSION: Final = 3


def days_before(
    days: Sequence[date],
    inputs: EnergyInputs,
    own: Mapping[date, float | None],
    health: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Intake, expenditure and balance of ``days``, the days before a session,
    for its analysis (#4136). Pure.

    The expenditure is the own day total (the diary's own day row), else the
    device's whole-day total, named per day; a day without either, or without
    intake, has no balance. The mean balance needs every day.
    """
    rows = [
        _day_before(d, inputs, own.get(d), health.get(d.isoformat(), {})) for d in days
    ]
    balances = [row["balance_kcal"] for row in rows if row["balance_kcal"] is not None]
    return {
        "days": rows,
        "mean_balance_kcal": round(statistics.fmean(balances))
        if len(balances) == len(rows)
        else None,
        "reason": None
        if len(balances) == len(rows)
        else "Für einen Tag fehlen Zufuhr oder Tagesverbrauch.",
    }


def _day_before(
    day: date,
    inputs: EnergyInputs,
    own: float | None,
    device: Mapping[str, Any],
) -> dict[str, Any]:
    entry = inputs.days.get(day)
    intake = None if entry is None else entry.intake_kcal
    calories = device.get("calories")
    whole_day = (
        float(calories)
        if isinstance(calories, int | float)
        and calories > 0
        and complete_garmin_day(device)
        else None
    )
    total, source = (own, "own") if own is not None else (whole_day, "device")
    return {
        "date": day.isoformat(),
        "intake_kcal": _round(intake),
        "expenditure_kcal": _round(total),
        "expenditure_source": None if total is None else source,
        "balance_kcal": None
        if intake is None or total is None
        else round(intake - total),
    }


type OwnHistory = tuple[
    EnergyInputs, tuple[EnergyWindow, ...], Mapping[str, Mapping[str, Any]]
]


def own_history(
    db: Any, sources: EnergySources, end: date, start: date | None = None
) -> OwnHistory:
    """The inputs, windows and daily health ``own_day_totals`` reads: the
    12-week history ending on ``end``, or from an earlier ``start`` (#3630)."""
    start = start or history_start(end)
    inputs, _ = read_energy_inputs(db, sources, start, end)
    return inputs, energy_windows(inputs, end), sources.daily_health(db, start, end)


def fit_start(end: date, days: int | None = None) -> date:
    """The first day a Verlauf period of ``days`` reads (default the longest, #3704)."""
    span = days or max(FIT_PERIODS.values())
    return min(history_start(end), end - timedelta(days=span - 1))


def read_fit_inputs(
    db: Any, sources: EnergySources, end: date, days: int | None = None
) -> EnergyInputs | None:
    """The inputs of Verlauf's periods up to ``days`` ending on ``end``: their own
    read where it reaches back past the 12 windows (#3704), else None. 6 Monate
    reads 182 days, so it never pays for 12 Monate (#3839). They depend on the
    stored rows, ``end`` and ``days`` only, so a caller may keep them."""
    first = fit_start(end, days)
    if first >= history_start(end):
        return None
    return read_energy_inputs(db, sources, first, end)[0]


def read_energy_view(
    db: Any,
    sources: EnergySources,
    end: date,
    day: date | None = None,
    fit_inputs: Callable[[], EnergyInputs | None] | None = None,
    fit_days: int | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Read the 12-week history ending on ``end`` and build the view of ``day``.

    ``today`` brings this morning's weigh-in for the water hint (#4047); the
    history itself still ends on ``end``.

    ``day`` (default ``end``) may be today, after ``end``, or before the history.
    ``fit_inputs`` serves the 6 months' inputs (``read_fit_inputs``) when
    Verlauf asks for that period; without it the view has the periods inside
    the 12-week read only, so no other read pays for them (#3711).
    """
    day = day or end
    # The 12-week span stays fixed; an older diary day is read beside it with
    # the span's step length, flex-HR line and resting reference (#3617 item 6).
    start = history_start(end)
    last = max(end, day)
    inputs, costs = read_energy_inputs(db, sources, start, last, older_day=day)
    # Verlauf's 6 months reach back past the 12 windows (#3704); their read
    # costs as much again, so it runs only when asked for (#3711).
    periods = fit_inputs() if fit_inputs else None
    first = fit_start(end, fit_days) if periods is not None else start
    health = sources.daily_health(db, min(first, day), last)
    windows = energy_windows(inputs, end)
    _, missing = skinfold_state(db, sources, inputs, day)
    # Today's planned units priced by the sports' own 28 days (#3917).
    planned = (
        planned_net_kcal(
            sources.planned(db, day), usual_kcal_per_hour(costs, end), inputs.rmr_kcal
        )
        if day > end
        else None
    )
    # The bias comes from the windows already built, not a second build (Gitar).
    view = energy_view(
        inputs,
        windows,
        health,
        sources.sessions(db, day, day),
        windows_bias(windows),
        body_missing=missing,
        day=day,
        goal=sources.profile(),
        fit_inputs=periods,
        fit_days=fit_days,
        planned_kcal=planned,
    )
    # Digestion counts kcal without macros at 10 %, so it is estimated (#3617).
    view["tef_estimated"] = inputs.days.get(day, DayInputs()).uncovered_kcal > 0
    # This morning's weigh-in step read as probable water; a hint only (#4047).
    view["water_hint"] = (
        water_hint_for_today(
            inputs.days,
            inputs.rmr_kcal,
            today,
            sources.weights(db, today, today).get(today),
        )
        if today is not None
        else None
    )
    # The day's strength sessions with the RPE and MET that priced them (#3530).
    view["strength"] = [
        {"rpe": cost.rpe, "met": cost.met, "kcal": _round(cost.kcal)}
        for cost in costs
        if cost.day == day and cost.met is not None
    ]
    return view

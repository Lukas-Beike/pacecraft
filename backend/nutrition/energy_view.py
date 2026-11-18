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
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date, timedelta
from typing import Any, Final, cast

from backend.athlete.body_composition import (
    RestingFormula,
    resting_band,
    resting_energy,
)
from backend.canonical.vocabulary import ProviderId
from backend.errors import AppError
from backend.nutrition.energy_balance import (
    DayInputs,
    EnergyInputs,
    EnergyWindow,
    Estimate,
    day_tef,
    energy_windows,
    history_start,
    is_complete,
    period_window,
    weight_trend,
    windows_bias,
)
from backend.nutrition.energy_inputs import (
    EnergySources,
    newest_resting,
    read_energy_inputs,
    resolve_body,
    skinfold_state,
)
from backend.providers.source_labels import SourceView, provider_label

# The fit needs this many windows shared by every compared source (#3187).
MIN_SHARED_WINDOWS = 4
# The daily band adds this share of the own activity, as M12's derived rate does.
ACTIVITY_BAND_SHARE = 0.10
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
    total_band = math.hypot(band, ACTIVITY_BAND_SHARE * activity)
    return resting, band, resting + activity + tef, total_band


def _device_prediction(
    health: Mapping[str, Mapping[str, Any]], window: EnergyWindow
) -> Prediction:
    """Garmin's mean resting and total values over the window's days."""
    start = window.end - timedelta(days=window.days - 1)
    days = [
        health.get((start + timedelta(days=n)).isoformat(), {})
        for n in range(window.days)
    ]

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
        if not resting or resting < 0 or active is None or active < 0:
            continue
        day_sport = _garmin_sport(by_day.get(key, []), resting)
        if day_sport is not None:
            sport.append(day_sport)
            everyday.append(active - day_sport)
    if not sport:
        return None, None
    neat = statistics.fmean(everyday)
    return (neat if neat >= 0 else None), statistics.fmean(sport)


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
) -> dict[str, Any]:
    """The source rows of ``day``, each source's fit over the windows, and the basis.

    ``body_missing`` is why the formula rows have no body (#3336), shown with them.
    ``day`` (default the latest window's end) gives the formula and device rows
    that day's resting rate, everyday, sport and digestion (athlete decision,
    #3395); "Aus deinen Daten" takes its resting rate from the latest 28-day
    window and the day's other parts like the formula rows, and its 28-day row
    with every part's mean is ``own_window``, for Verlauf (#3531). ``goal`` is
    the profile's energy goal, priced on the shown day (#3547).
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
    day_predictions[DEVICE], behind = _garmin_behind(
        device_prediction, device_neat, device_sport, bool(device_rest)
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
    # Only the device row is named by the read; the client names the others.
    rows[list(predictions).index(DEVICE)].update(
        label=_device_label(health, day_window), **device_rest
    )
    shown, implausible = _gated_resting(
        latest.derived_rmr, predictions["katch_mcardle"][-1], predictions[DEVICE][-1]
    )
    rows.append(_own_day_row(shown, implausible, latest, day_window, pending[False]))
    return {
        "end": latest.end.isoformat(),
        # The day the source rows show; after ``end`` (today) it is partial.
        "day": day_window.end.isoformat(),
        "day_partial": day_window.end > latest.end,
        "windows": [w.end.isoformat() for w in windows],
        "rows": rows,
        "best": best,
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
        "fit_scale": {
            key: _fit_scale(inputs, trend, health, latest, days)
            for key, days in FIT_PERIODS.items()
        },
        "skinfold_missing": body_missing,
        "body": _public_body(day_body),
        # The 28-day row with every part's mean, shown in Verlauf only (#3531).
        "own_window": _own_window_row(shown, implausible, latest),
        "goal": _energy_goal(rows, best, goal, inputs.days.get(day_window.end)),
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


def _energy_goal(
    rows: Sequence[Mapping[str, Any]],
    best: str | None,
    profile: Mapping[str, str] | None,
    day: DayInputs | None,
) -> dict[str, Any] | None:
    """'Heute noch essen' (#3547): the outlined source's day total (so far
    today) with the goal's deficit or surplus, less what was eaten."""
    kind = (profile or {}).get("energy_goal") or ""
    if kind not in _GOAL_SIGN:
        return None
    try:
        kcal = round(float((profile or {}).get("energy_goal_kcal") or 0))
    except ValueError:
        kcal = 0
    kcal *= abs(_GOAL_SIGN[kind])
    source = _outlined(rows, best)
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
        # "measured" from two dated body fat measurements, else "forbes";
        # None without an estimate (#3477).
        "store_method": latest.store_method,
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
        "store_method": latest.store_method,
    }


def _day_window(inputs: EnergyInputs, latest: EnergyWindow, day: date) -> EnergyWindow:
    """``day`` as a one-day window for the source rows (#3395).

    Its own everyday and sport (0 without a session), and digestion only from
    logged meals (None without any). The latest window itself when ``day`` is
    its end and no day was chosen keeps nothing else: the 28-day means are
    never shown for one day.
    """
    values = inputs.days.get(day, DayInputs())
    return cast(
        EnergyWindow,
        replace(
            latest,
            end=day,
            days=1,
            activity_kcal_raw=values.activity_kcal,
            neat_kcal_raw=values.neat_kcal,
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
FIT_PERIODS: Final = {"14d": 14, "12w": 84}


def _fit_scale(
    inputs: EnergyInputs,
    trend: Mapping[date, float],
    health: Mapping[str, Mapping[str, Any]],
    latest: EnergyWindow,
    days: int,
) -> dict[str, Any]:
    """Each source's mean daily total over ``days`` against the own estimate.

    ``own_days`` names the span of the own value and its band.
    """
    period = period_window(inputs, latest.end, days, latest.calibration_factor)
    own = period if days >= latest.days else latest
    body = resolve_body(inputs, trend, latest.end)
    sources = [
        {"source": name, "total_kcal": _round(_formula_prediction(f, body, period)[2])}
        for name, f in FORMULAS.items()
    ]
    sources.append(
        {
            "source": DEVICE,
            "label": _device_label(health, period),
            "total_kcal": _round(_device_prediction(health, period)[2]),
        }
    )
    tdee = own.adaptive_tdee
    return {
        "days": days,
        "own_days": own.days,
        "own_kcal": _round(tdee.kcal) if tdee else None,
        "own_band_kcal": _round(tdee.band_kcal) if tdee else None,
        "sources": sources,
        "weeks": days // 7,
        "weeks_with_data": _weeks_with_data(inputs, latest.end, days),
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
    no data while its day has no active kcal. Past days are unchanged.
    """
    if not today:
        return window, ([], [])
    day = day or DayInputs()
    # Fewer day steps than the sessions' own: the day summary lags behind the
    # activity, so its everyday has not arrived yet either (#3530).
    behind = day.steps is not None and day.session_steps > day.steps
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
    instead of "–" (#3530)."""
    resting = prediction[0]
    if not today or resting is None or neat is not None or sport is None:
        return prediction, []
    return (resting, None, resting + sport, None), ["neat"]


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


def read_energy_view(
    db: Any, sources: EnergySources, end: date, day: date | None = None
) -> dict[str, Any]:
    """Read the 12-week history ending on ``end`` and build the view of ``day``.

    ``day`` (default ``end``) may be today, after ``end``, or before the history.
    """
    day = day or end
    start = min(history_start(end), day)
    last = max(end, day)
    inputs, costs = read_energy_inputs(db, sources, start, last)
    health = sources.daily_health(db, start, last)
    windows = energy_windows(inputs, end)
    _, missing = skinfold_state(db, sources, inputs, day)
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
    )
    # The day's strength sessions with the RPE and MET that priced them (#3530).
    view["strength"] = [
        {"rpe": cost.rpe, "met": cost.met, "kcal": _round(cost.kcal)}
        for cost in costs
        if cost.day == day and cost.met is not None
    ]
    return view

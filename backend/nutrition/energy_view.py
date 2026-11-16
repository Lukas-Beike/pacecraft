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
from datetime import date, timedelta
from typing import Any, Final

from backend.athlete.body_composition import (
    RestingFormula,
    lean_mass_kg,
    resting_band,
    resting_energy,
)
from backend.canonical.vocabulary import ProviderId
from backend.nutrition.energy_balance import (
    EnergyInputs,
    EnergyWindow,
    Estimate,
    energy_windows,
    history_start,
    weight_trend,
)
from backend.nutrition.energy_inputs import (
    RESTING_LOOKBACK_DAYS,
    EnergySources,
    read_energy_inputs,
)

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


def _body(
    inputs: EnergyInputs, trend: Mapping[date, float], day: date
) -> dict[str, Any] | None:
    """The skinfold in effect on ``day`` with the trend weight's lean mass."""
    since = day - timedelta(days=RESTING_LOOKBACK_DAYS)
    measured = [d for d in inputs.body_fat_pct if since <= d <= day]
    weighed = [d for d in trend if d <= day]
    if not measured or not weighed:
        return None
    fat_pct = inputs.body_fat_pct[max(measured)]
    weight = trend[max(weighed)]
    try:
        lean = lean_mass_kg(weight, fat_pct)
    except ValueError:
        return None
    return {
        "measured_on": max(measured),
        "body_fat_pct": fat_pct,
        "weight_kg": weight,
        "lean_mass_kg": lean,
    }


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
) -> tuple[float | None, float | None]:
    """Garmin's everyday (NEAT) and sport kcal per day over the window.

    Sport is Garmin's recorded session kcal net of its resting rate over their
    duration; everyday is the active kcal minus it. A day counts only with
    resting and active kcal and every Garmin session priced; everyday below
    zero is not a valid split and stays None (split A, athlete decision
    2026-11-16).
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
        resting = _number(day.get("resting_kcal"))
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


def energy_view(
    inputs: EnergyInputs,
    windows: Sequence[EnergyWindow],
    health: Mapping[str, Mapping[str, Any]],
    sessions: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """The table rows of the latest window, each source's fit, split and basis."""
    trend = weight_trend(
        {d: v.weight_kg for d, v in inputs.days.items() if v.weight_kg is not None}
    )
    latest = windows[-1]
    bodies = [_body(inputs, trend, w.end) for w in windows]
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
    device_neat, device_sport = _device_split(health, sessions, latest)
    rows = []
    for name, values in predictions.items():
        resting, resting_band_kcal, total, total_band = values[-1]
        # Formula rows add the same uncalibrated own everyday, sport and TEF
        # (M11, #3220) as their daily total; Garmin splits its own day.
        if resting is None:
            split = None
        elif name == DEVICE:
            split = _split(resting, device_neat, device_sport, None, NOT_SHARED)
        else:
            split = _split(
                resting,
                latest.neat_kcal_raw,
                latest.sport_kcal_raw,
                latest.tef_kcal,
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
            }
        )
    tdee = latest.adaptive_tdee
    shown, implausible = _gated_resting(
        latest.derived_rmr, predictions["katch_mcardle"][-1], predictions[DEVICE][-1]
    )
    # The own row's resting rate is the residual of the calibrated terms; its
    # everyday and sport equal the formula rows' only while k is 1.
    uncalibrated = math.isclose(latest.calibration_factor, 1.0)
    own_split = (
        _split(
            shown.kcal if shown else None,
            latest.neat_kcal,
            latest.sport_kcal,
            latest.tef_kcal,
            {"neat": uncalibrated, "sport": uncalibrated, "tef": True},
        )
        if latest.derived_rmr
        else None
    )
    rows.append(
        {
            "source": OWN,
            "resting_kcal": _round(shown.kcal) if shown else None,
            "resting_band_kcal": _round(shown.band_kcal) if shown else None,
            "resting_implausible": implausible,
            "total_kcal": _round(tdee.kcal) if tdee else None,
            "total_band_kcal": _round(tdee.band_kcal) if tdee else None,
            "split": own_split,
        }
    )
    body = bodies[-1]
    return {
        "end": latest.end.isoformat(),
        "windows": [w.end.isoformat() for w in windows],
        "rows": rows,
        "best": best,
        "fit": fit,
        "basis": {
            "days": latest.days,
            "complete_days": latest.complete_days,
            "flagged_days": latest.flagged_days,
            "weighins": latest.weighins,
            "insufficient": latest.insufficient,
            "windows_with_estimate": sum(1 for w in windows if w.adaptive_tdee),
            "rmr_source": latest.rmr_source,
            "neat": _neat_basis(inputs, latest),
        },
        "skinfold": None
        if body is None
        else {
            "date": body["measured_on"].isoformat(),
            "body_fat_pct": round(body["body_fat_pct"], 1),
            "lean_mass_kg": round(body["lean_mass_kg"], 1),
        },
    }


def read_energy_view(db: Any, sources: EnergySources, end: date) -> dict[str, Any]:
    """Read the 12-week history ending on ``end`` and build the view."""
    start = history_start(end)
    inputs, _ = read_energy_inputs(db, sources, start, end)
    health = sources.daily_health(db, start, end)
    windows = energy_windows(inputs, end)
    # The split is shown for the latest window only, so only its sessions are read.
    latest = sources.sessions(db, end - timedelta(days=windows[-1].days - 1), end)
    return energy_view(inputs, windows, health, latest)

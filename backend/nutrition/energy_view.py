"""Resting and daily expenditure per source, and which one fits the weight (#3099).

Katch-McArdle and Cunningham price the resting rate from the skinfold lean
mass; their daily total adds the same own activity and TEF (M11) to both, so
they differ only in the resting formula. The activity is the uncalibrated one:
the calibration is fitted against the adaptive TDEE, and the comparison would
otherwise favour the formula rows over the device. Garmin's resting and total
values stand beside them as its estimate, and "own" is the adaptive TDEE with
the derived resting rate (#3100). Each source's distance to the adaptive TDEE
per 4-week window gives the "passt am besten" badge and the hit rate. A
missing basis stays None; there is never a fallback formula.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any

from backend.athlete.body_composition import (
    RestingFormula,
    lean_mass_kg,
    resting_energy,
)
from backend.nutrition.energy_balance import (
    EnergyInputs,
    EnergyWindow,
    energy_windows,
    history_start,
    weight_trend,
)
from backend.nutrition.energy_inputs import (
    RESTING_LOOKBACK_DAYS,
    EnergySources,
    read_energy_inputs,
)

# A window counts as a hit when a source is within this of the adaptive TDEE.
HIT_KCAL = 100.0
FORMULAS = {
    "katch_mcardle": RestingFormula.KATCH_MCARDLE,
    "cunningham": RestingFormula.CUNNINGHAM,
}
# The wearable's own resting and total estimate (Garmin today).
DEVICE = "device"
OWN = "own"


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
    try:
        lean = lean_mass_kg(trend[max(weighed)], fat_pct)
    except ValueError:
        return None
    return {"measured_on": max(measured), "body_fat_pct": fat_pct, "lean_mass_kg": lean}


def _formula_totals(
    formula: RestingFormula, body: Mapping[str, Any] | None, window: EnergyWindow
) -> tuple[float | None, float | None]:
    """Resting from lean mass, and the day with the same own activity and TEF."""
    if body is None:
        return None, None
    resting = resting_energy(formula, body["lean_mass_kg"])
    if window.activity_kcal_raw is None or window.tef_kcal is None:
        return resting, None
    return resting, resting + window.activity_kcal_raw + window.tef_kcal


def _garmin_totals(
    health: Mapping[str, Mapping[str, Any]], window: EnergyWindow
) -> tuple[float | None, float | None]:
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

    return mean("resting_kcal"), mean("calories")


def _fit(errors: Sequence[float | None]) -> dict[str, Any]:
    known = [e for e in errors if e is not None]
    return {
        "errors_kcal": [_round(e) for e in errors],
        "mean_error_kcal": _round(statistics.fmean(known)) if known else None,
        "hit_rate_pct": round(100 * sum(e <= HIT_KCAL for e in known) / len(known))
        if known
        else None,
    }


def energy_view(
    inputs: EnergyInputs,
    windows: Sequence[EnergyWindow],
    health: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """The table rows of the latest window, each source's fit and the basis."""
    trend = weight_trend(
        {d: v.weight_kg for d, v in inputs.days.items() if v.weight_kg is not None}
    )
    latest = windows[-1]
    bodies = [_body(inputs, trend, w.end) for w in windows]
    predictions: dict[str, list[tuple[float | None, float | None]]] = {
        name: [_formula_totals(f, b, w) for b, w in zip(bodies, windows, strict=True)]
        for name, f in FORMULAS.items()
    }
    predictions[DEVICE] = [_garmin_totals(health, w) for w in windows]
    rows = []
    for name, values in predictions.items():
        errors = [
            abs(total - w.adaptive_tdee.kcal)
            if total is not None and w.adaptive_tdee is not None
            else None
            for (_, total), w in zip(values, windows, strict=True)
        ]
        resting, total = values[-1]
        rows.append(
            {
                "source": name,
                "resting_kcal": _round(resting),
                "total_kcal": _round(total),
                **_fit(errors),
            }
        )
    tdee, rmr = latest.adaptive_tdee, latest.derived_rmr
    rows.append(
        {
            "source": OWN,
            "resting_kcal": _round(rmr.kcal) if rmr else None,
            "resting_band_kcal": _round(rmr.band_kcal) if rmr else None,
            "total_kcal": _round(tdee.kcal) if tdee else None,
            "total_band_kcal": _round(tdee.band_kcal) if tdee else None,
        }
    )
    fitted = [r for r in rows if r.get("mean_error_kcal") is not None]
    body = bodies[-1]
    return {
        "end": latest.end.isoformat(),
        "windows": [w.end.isoformat() for w in windows],
        "rows": rows,
        "best": min(fitted, key=lambda r: r["mean_error_kcal"])["source"]
        if fitted
        else None,
        "basis": {
            "days": latest.days,
            "complete_days": latest.complete_days,
            "flagged_days": latest.flagged_days,
            "weighins": latest.weighins,
            "insufficient": latest.insufficient,
            "windows_with_estimate": sum(1 for w in windows if w.adaptive_tdee),
            "rmr_source": latest.rmr_source,
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
    return energy_view(inputs, energy_windows(inputs, end), health)

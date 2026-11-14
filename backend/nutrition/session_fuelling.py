"""Drinking and carbohydrates for a long planned run or ride (#2804).

The rules live in ``backend.nutrition.fuelling``; this module gathers the local
inputs: the stored sessions of the last year with Garmin's sweat-loss estimate,
their stored activity weather, the cached forecast of the planned day and the
resolved body weight. It reads only; nothing is fetched or stored.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date, timedelta
from typing import Any, Final

from backend.canonical.activity_summary import summary_duration_s, summary_is_indoor
from backend.errors import AppError
from backend.nutrition import fuelling

LONG_SESSION_S: Final = 3600
WINDOW_DAYS: Final = 365
SPORTS: Final = frozenset({"run", "ride"})
_INVALID = "Ungültige Angaben für die Verpflegungsempfehlung."


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value > 0 else None


def sweat_samples(
    summaries: Iterable[dict[str, Any]], sport: str, indoor: bool
) -> list[tuple[str, float]]:
    """``(session id, L/h)`` of the sessions of this sport and setting."""
    samples = []
    for summary in summaries:
        if summary.get("sport") != sport or summary_is_indoor(summary) != indoor:
            continue
        water = _number(summary.get("water_estimated_ml"))
        seconds = _number(summary_duration_s(summary))
        if water is not None and seconds is not None:
            samples.append((str(summary.get("id")), water / 1000 / (seconds / 3600)))
    return samples


def forecast_temperature(days: Iterable[dict[str, Any]], day: str) -> float | None:
    """The middle of the forecast low and high of ``day``."""
    for row in days:
        if row.get("date") == day:
            low, high = row.get("temperature_min"), row.get("temperature_max")
            if isinstance(low, (int, float)) and isinstance(high, (int, float)):
                return round((low + high) / 2, 1)
    return None


def session_fuelling(
    sport: str,
    indoor: bool,
    duration_s: float,
    samples: list[tuple[float, float | None]],
    forecast_c: float | None,
    weight: dict[str, Any] | None,
) -> dict[str, Any]:
    """The recommendation, or ``not_long`` below an hour or for other sports."""
    if sport not in SPORTS or duration_s < LONG_SESSION_S:
        return {"status": "not_long"}
    weight_kg = _number((weight or {}).get("value"))
    rate = fuelling.sweat_rate(samples, None if indoor else forecast_c)
    drink = (
        fuelling.drinking(rate["l_per_h"], duration_s, weight_kg)
        if rate
        else {"generic_l_per_h": list(fuelling.GENERIC_SWEAT_L_PER_H)}
    )
    if not rate and duration_s >= fuelling.SODIUM_FROM_S:
        drink["sodium_g_per_l"] = list(fuelling.SODIUM_G_PER_L)
    return {
        "status": "ready",
        "duration_s": int(duration_s),
        "forecast_c": None if indoor else forecast_c,
        "sweat_rate": rate,
        "drinking": drink,
        "drinking_source": fuelling.FLUID_POSITION_STAND,
        "carbohydrates": fuelling.carbohydrate_per_hour(duration_s),
        "carbohydrate_source": fuelling.POSITION_STAND,
        "weight": None
        if weight_kg is None
        else {"kg": weight_kg, "source": (weight or {}).get("source")},
    }


def _request(query: dict[str, list[str]]) -> tuple[str, bool, str, float]:
    def first(name: str) -> str:
        return str((query.get(name) or [""])[0]).strip()

    sport, environment, day = first("sport"), first("environment"), first("date")
    try:
        date.fromisoformat(day)
        duration_s = float(first("duration_s"))
    except ValueError as exc:
        raise AppError(400, _INVALID, reason="invalid_session_fuelling") from exc
    if not 0 < duration_s <= 24 * 3600 or not sport or len(sport) > 40:
        raise AppError(400, _INVALID, reason="invalid_session_fuelling")
    return sport, summary_is_indoor({"environment": environment}), day, duration_s


def read_session_fuelling(
    summaries_between: Callable[[date, date], list[dict[str, Any]] | None],
    session_temperatures: Callable[[list[str]], dict[str, float]],
    forecast_days: Callable[[], list[dict[str, Any]]],
    body_weight: Callable[[], dict[str, Any] | None],
    today: Callable[[], date],
    query: dict[str, list[str]],
) -> dict[str, Any]:
    """The recommendation for a planned unit described by ``query``."""
    sport, indoor, day, duration_s = _request(query)
    if sport not in SPORTS or duration_s < LONG_SESSION_S:
        return {"status": "not_long"}
    end = today()
    rates = sweat_samples(
        summaries_between(end - timedelta(days=WINDOW_DAYS), end) or [], sport, indoor
    )
    temperatures = {} if indoor else session_temperatures([sid for sid, _ in rates])
    samples = [(rate, temperatures.get(sid)) for sid, rate in rates]
    forecast_c = None if indoor else forecast_temperature(forecast_days(), day)
    return session_fuelling(sport, indoor, duration_s, samples, forecast_c, body_weight())

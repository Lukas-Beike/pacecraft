"""Fuelling and drinking rules for races and long sessions (#2803, #2804).

The single owner of the fuelling rules; the race plan and the long planned
units read them here. Carbohydrate steps by duration follow Thomas, Erdman and
Burke (2016), the joint position statement of the American College of Sports
Medicine, the Academy of Nutrition and Dietetics and Dietitians of Canada (Med
Sci Sports Exerc 48(3):543-568). Above 2.5 h the athlete chose up to 120 g/h
(Hearris et al. 2022; Podlogar et al. 2022); the step is flagged because a
benefit above 90 g/h is not yet shown (Wallis and Podlogar 2022).
Drinking follows the ACSM position stand on exercise and fluid replacement
(Sawka et al. 2007, Med Sci Sports Exerc 39:377-390) and the NATA statement
(McDermott et al. 2017, J Athl Train 52:877-895): keep the loss below 2 % of
body mass and never drink more than the sweat loss. The ±5 °C band and the
minimum of 3 sessions are the athlete's design choice (#2804). Pure: no I/O.
"""

from __future__ import annotations

from statistics import median
from typing import Any, Final

METHOD_ID: Final = "carbohydrate-fuelling"
POSITION_STAND: Final = "Thomas, Erdman und Burke 2016"
FLUID_POSITION_STAND: Final = "Sawka 2007"
# Above this rate the athlete's chosen ceiling exceeds the position stand.
ESTABLISHED_MAX_G_PER_H: Final = 90
# ACSM starting range in L/h when no own sweat rate is known.
GENERIC_SWEAT_L_PER_H: Final = (0.4, 0.8)
# Share of body mass a session may lose (ACSM, NATA).
MAX_LOSS_SHARE: Final = 0.02
# Sodium per litre of drink for sessions from 2 h on (ACSM 2007).
SODIUM_G_PER_L: Final = (0.5, 0.7)
SODIUM_FROM_S: Final = 2 * 3600
# Athlete design choice (#2804): similar temperature band and its minimum.
SIMILAR_TEMPERATURE_C: Final = 5.0
MIN_SIMILAR_SESSIONS: Final = 3
_MINUTES: Final = 60.0


def sweat_rate(
    samples: list[tuple[float, float | None]], temperature_c: float | None
) -> dict[str, Any] | None:
    """The median sweat rate in L/h of ``(rate, temperature)`` samples.

    With a temperature, only samples within ``SIMILAR_TEMPERATURE_C`` count
    when at least ``MIN_SIMILAR_SESSIONS`` exist; otherwise all samples count
    and ``similar`` is False.
    """
    if not samples:
        return None
    similar = (
        [
            (rate, temp)
            for rate, temp in samples
            if temp is not None and abs(temp - temperature_c) <= SIMILAR_TEMPERATURE_C
        ]
        if temperature_c is not None
        else []
    )
    chosen = similar if len(similar) >= MIN_SIMILAR_SESSIONS else samples
    temps = [temp for _rate, temp in chosen if temp is not None]
    return {
        "l_per_h": round(median(rate for rate, _temp in chosen), 2),
        "sessions": len(chosen),
        "similar": chosen is similar,
        "temperature_c": [round(min(temps)), round(max(temps))] if temps else None,
    }


def weight_block(weight: dict[str, Any] | None) -> dict[str, Any] | None:
    """``{kg, source}`` of a resolved ``{value, source}`` weight, else None."""
    value = (weight or {}).get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return {"kg": float(value), "source": (weight or {}).get("source")}


def drinking(
    rate_l_per_h: float | None, duration_s: float, weight_kg: float | None
) -> dict[str, Any]:
    """Litres per hour and in total: the expected loss minus 2 % of body mass.

    Never more than the sweat rate; without a weight the sweat rate is only the
    upper bound, and without a sweat rate the ACSM starting range applies. From
    2 h on, the sodium range per litre is added while there is something to
    drink.
    """
    if rate_l_per_h is None:
        result: dict[str, Any] = {"generic_l_per_h": list(GENERIC_SWEAT_L_PER_H)}
    else:
        hours = duration_s / 3600
        loss = rate_l_per_h * hours
        result = {"max_l_per_h": round(rate_l_per_h, 2)}
        if weight_kg is not None and hours > 0:
            total = max(0.0, loss - weight_kg * MAX_LOSS_SHARE)
            result["l_per_h"] = round(min(rate_l_per_h, total / hours), 2)
            result["total_l"] = round(total, 2)
            if total <= 0:
                # Decided before rounding: the card says so with the loss, never "0 l" (#3012).
                result["within_loss_share"] = True
                result["loss_l"] = round(loss, 2)
    if duration_s >= SODIUM_FROM_S and not result.get("within_loss_share"):
        result["sodium_g_per_l"] = list(SODIUM_G_PER_L)
    return result


def carbohydrate_per_hour(duration_s: Any) -> dict[str, Any] | None:
    """Grams per hour during an effort of ``duration_s``, or None if unknown."""
    if isinstance(duration_s, bool) or not isinstance(duration_s, (int, float)):
        return None
    if duration_s <= 0:
        return None
    minutes = duration_s / _MINUTES
    if minutes < 45:
        return {"step": "none", "low_g": 0, "high_g": 0}
    if minutes <= 75:
        # Small amounts including a mouth rinse; the stand gives no gram rate.
        return {"step": "mouth_rinse", "low_g": None, "high_g": None}
    if minutes <= 150:
        return {"step": "moderate", "low_g": 30, "high_g": 60}
    return {
        "step": "high",
        "low_g": 60,
        "high_g": 120,
        "established_max_g": ESTABLISHED_MAX_G_PER_H,
        "glucose_fructose": True,
    }


def _grams(weight_kg: float | None, per_kg: tuple[int, int]) -> list[int] | None:
    if weight_kg is None:
        return None
    return [round(weight_kg * per_kg[0]), round(weight_kg * per_kg[1])]


def pre_race_carbohydrates(
    duration_s: Any, weight_kg: float | None
) -> list[dict[str, Any]]:
    """Carbohydrates before a race: daily intake on the days before, then the meal.

    Over 90 min: 10-12 g/kg per day for the 36-48 h before (loading); up to
    90 min: 7-12 g/kg in the 24 h before; over 60 min also a meal of 1-4 g/kg
    in the 1-4 h before the start. Grams need the weight; g/kg always stays.
    """
    if isinstance(duration_s, bool) or not isinstance(duration_s, (int, float)):
        return []
    if duration_s <= 0:
        return []
    minutes = duration_s / _MINUTES
    kind, days, per_kg = ("loading", 2, (10, 12)) if minutes > 90 else ("daily", 1, (7, 12))
    result: list[dict[str, Any]] = [
        {
            "kind": kind,
            "days_before": days,
            "g_per_kg": list(per_kg),
            "grams": _grams(weight_kg, per_kg),
        }
    ]
    if minutes > 60:
        result.append(
            {
                "kind": "meal",
                "hours_before": [4, 1],
                "g_per_kg": [1, 4],
                "grams": _grams(weight_kg, (1, 4)),
            }
        )
    return result

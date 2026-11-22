"""Two non-blocking notes on the size of an energy deficit (#3917).

Energy availability (EA) is the intake less the exercise energy, per kg of
fat-free mass and day (Loucks, Kiens & Wright 2011, R78; Mountjoy et al. 2023,
R79); the note fires below 30 kcal/kg FFM, the athlete's line (#3917).
The exercise energy is M11's session net cost, already net of the resting
energy over each session's duration; the fat-free mass is M13's lean mass. The
loss-rate note compares M12's 28-day weight line with 1 % of body mass per week
(Garthe et al. 2011, R80). Both fire on the number as shown, rounded; neither
blocks anything. Sources and limits are on the cards M11, M12 and M13.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any, Final

from backend.nutrition.energy_balance import (
    MIN_WEIGHINS,
    MIN_WEIGHT_SPAN_DAYS,
    EnergyInputs,
    EnergyWindow,
    energy_density,
    is_complete,
)

# kcal per kg fat-free mass and day below which the note shows.
EA_FLOOR_KCAL_PER_KG_FFM: Final = 30
# The days the mean covers, and the complete intake days it needs among them.
EA_DAYS: Final = 7
EA_MIN_DAYS: Final = 5
# Percent of body mass per week the 28-day weight line may fall before the note shows.
LOSS_LIMIT_PCT_PER_WEEK: Final = 1.0
DAYS_PER_WEEK: Final = 7


def energy_availability(
    inputs: EnergyInputs, lean_mass_kg: float | None, end: date
) -> dict[str, Any] | None:
    """The 7-day mean EA ending on ``end``, or None without ``EA_MIN_DAYS``
    complete intake days with a priced sport, or without a fat-free mass.

    ``kcal_per_kg_ffm`` is the whole number shown, rounded down; ``below``
    follows it.
    """
    if not lean_mass_kg or lean_mass_kg <= 0:
        return None
    available = []
    for back in range(EA_DAYS):
        day = inputs.days.get(end - timedelta(days=back))
        if (
            day is not None
            and day.intake_kcal is not None
            and day.sport_kcal is not None
            and is_complete(day, inputs.rmr_kcal)
        ):
            available.append(day.intake_kcal - day.sport_kcal)
    if len(available) < EA_MIN_DAYS:
        return None
    value = int(statistics.fmean(available) // lean_mass_kg)
    return {
        "kcal_per_kg_ffm": value,
        "days": len(available),
        "floor_kcal_per_kg_ffm": EA_FLOOR_KCAL_PER_KG_FFM,
        "lean_mass_kg": round(lean_mass_kg, 1),
        "below": value < EA_FLOOR_KCAL_PER_KG_FFM,
    }


def loss_rate(window: EnergyWindow) -> dict[str, Any] | None:
    """How fast the window's weight line falls, in percent of body mass per
    week; None while it is not falling or the window lacks the weigh-ins and
    span M12 asks for."""
    store = window.store
    if (
        store is None
        or window.weighins < MIN_WEIGHINS
        or store.days < MIN_WEIGHT_SPAN_DAYS
        or store.weight_end_kg <= 0
    ):
        return None
    slope = store.kcal_per_day / energy_density(store.fat_share)
    pct = round(-slope * DAYS_PER_WEEK / store.weight_end_kg * 100, 1)
    if pct <= 0:
        return None
    return {
        "pct_per_week": pct,
        "limit_pct_per_week": LOSS_LIMIT_PCT_PER_WEEK,
        "fast": pct > LOSS_LIMIT_PCT_PER_WEEK,
    }


def energy_notes(
    inputs: EnergyInputs, window: EnergyWindow, body: Mapping[str, Any] | None
) -> dict[str, dict[str, Any] | None]:
    """Both notes for the latest window, whose ``body`` gives the lean mass."""
    lean = None if body is None else body["lean_mass_kg"]
    return {
        "energy_availability": energy_availability(inputs, lean, window.end),
        "loss_rate": loss_rate(window),
    }

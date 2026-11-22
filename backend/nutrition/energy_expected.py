"""The day total expected for today, beside the "so far" total (#3917).

Resting energy and the everyday term as the row already shows them, the usual
steps and digestion as the latest window's 28-day means, and today's planned
units costed from their planned duration with the M11 method of their sport:
strength by its general MET, any other sport by the athlete's own mean net
kcal per hour of that sport over the last 28 days, priced by M11 for it (power,
Minetti or heart rate). A unit that cannot be costed leaves the total unknown.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any

from backend.canonical.stored_sports import workout_sport_from_record
from backend.nutrition.energy_balance import WINDOW_DAYS, EnergyWindow
from backend.nutrition.energy_inputs import STRENGTH_SPORTS, SessionCost
from backend.performance import activity_energy as energy

SECONDS_PER_HOUR = 3600.0


def usual_kcal_per_hour(costs: Sequence[SessionCost], end: date) -> dict[str, float]:
    """Each sport's net kcal per hour over the 28 days ending on ``end``, from
    its priced sessions."""
    first = end - timedelta(days=WINDOW_DAYS - 1)
    kcal: dict[str, float] = defaultdict(float)
    seconds: dict[str, float] = defaultdict(float)
    for cost in costs:
        if (
            cost.kcal is not None
            and cost.duration_s is not None
            and cost.duration_s > 0
            and first <= cost.day <= end
        ):
            kcal[cost.sport] += cost.kcal
            seconds[cost.sport] += cost.duration_s
    return {sport: kcal[sport] * SECONDS_PER_HOUR / seconds[sport] for sport in seconds}


def planned_inputs(units: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The planned units' sport and planned duration in seconds, without the
    locally deleted ones."""
    result = []
    for unit in units:
        if unit.get("local_deleted"):
            continue
        minutes = unit.get("duration_minutes")
        seconds = unit.get("planned_duration_s") or (
            minutes * 60 if isinstance(minutes, (int, float)) else None
        )
        result.append(
            {
                "sport": workout_sport_from_record(unit).sport.value,
                "duration_s": seconds,
            }
        )
    return result


def planned_net_kcal(
    units: Sequence[Mapping[str, Any]],
    rates: Mapping[str, float],
    rmr_kcal: float | None,
) -> float | None:
    """The planned units' net kcal; 0 without units, None when one has no
    duration or no way to cost its sport."""
    total = 0.0
    for unit in units:
        seconds = unit.get("duration_s")
        sport = str(unit.get("sport") or "")
        if not isinstance(seconds, (int, float)) or seconds <= 0:
            return None
        if sport in STRENGTH_SPORTS:
            if rmr_kcal is None:
                return None
            total += energy.strength_net_kcal(
                seconds, rmr_kcal, energy.strength_met(None)
            )
        elif sport in rates:
            total += rates[sport] * seconds / SECONDS_PER_HOUR
        else:
            return None
    return total


def expected_total_kcal(
    row: Mapping[str, Any] | None,
    latest: EnergyWindow,
    planned_kcal: float | None,
    device: bool,
    device_neat: float | None = None,
) -> int | None:
    """Today's expected total on the outlined ``row``: its resting energy and
    everyday term, the 28-day mean steps and digestion and the planned units.
    Garmin's row takes its own 28-day everyday mean (``device_neat``) and has
    no digestion part. Each part is at least what the row counts so far, so the
    expectation never lies below the total so far. None where any part is
    unknown."""
    split = (row or {}).get("split")
    if split is None or split["resting_kcal"] is None or planned_kcal is None:
        return None
    neat = device_neat if device else latest.neat_kcal_raw
    tef = 0.0 if device else latest.tef_kcal
    if neat is None or tef is None:
        return None
    learned = split.get("learned_kcal") or 0
    parts = (
        max(neat, split.get("neat_kcal") or 0),
        max(tef, split.get("tef_kcal") or 0),
        max(planned_kcal, split.get("sport_kcal") or 0),
    )
    return round(split["resting_kcal"] + learned + sum(parts))

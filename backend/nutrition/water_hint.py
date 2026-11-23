"""A marked weigh-in step explained as probable water (#4047).

A weigh-in far from the weight trend, after a previous day whose logged salt
or carbohydrates were clearly above (a jump up) or, after a long session,
below (a drop) the athlete's usual level, is probably water. The result is a
hint only: it never changes a weight, the trend or any energy estimate, and it
names no kilogram figure. Salt binds water for a short time and never builds
tissue or fat.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Final, Literal

from backend.athlete.body_records import MIN_LINE_WEIGHINS, weight_trend
from backend.nutrition.energy_balance import is_complete

# The usual scatter of the weigh-ins around the trend is read from this many days.
SCATTER_DAYS: Final = 56
# A residual needs this many earlier weigh-ins to count, and the scatter this many residuals.
MIN_PRIOR_WEIGHINS: Final = MIN_LINE_WEIGHINS
MIN_RESIDUALS: Final = 10
# Marked: beyond this many robust standard deviations of the usual scatter, and never below the floor.
MARKED_SD: Final = 2.0
MARKED_FLOOR_KG: Final = 0.7
MAD_TO_SD: Final = 1.4826
# The athlete's usual intake is read from this many days before the previous day.
USUAL_DAYS: Final = 28
MIN_USUAL_DAYS: Final = 10
# "Clearly": beyond this many robust deviations and by this share of the usual level.
CLEAR_SD: Final = 1.5
CLEAR_SHARE: Final = 0.3
# Salt counts only on days whose logged items mostly report it.
MIN_SALT_KNOWN_SHARE: Final = 0.5

if TYPE_CHECKING:
    from backend.nutrition.energy_balance import DayInputs

Cause = Literal["salt", "carbs", "carbs_after_session"]

# What the Coach says with the hint (athlete decision, #4047): the cause, no kilogram
# figure, and that salt never builds tissue or fat. It travels with the hint, as the
# Coach prompt and its playbooks are at their size budgets.
COACH_NOTE: Final = (
    "Nenne die Ursache, keine Kilogrammzahl: Das Plus von heute früh passt zu viel Salz "
    "und Kohlenhydraten gestern. Das ist Wasser, kein Fett und kein Gewebe: Salz bindet "
    "nur kurz Wasser und lässt dich nicht zunehmen."
)
Direction = Literal["up", "down"]


@dataclass(frozen=True)
class DayIntake:
    """One day's logged salt and carbohydrates. ``None`` is unknown, never zero."""

    carbs_g: float | None
    salt_g: float | None
    # The share of the day's logged items that report salt.
    salt_known_share: float


@dataclass(frozen=True)
class WaterHint:
    direction: Direction
    causes: tuple[Cause, ...]


def _robust(values: Sequence[float]) -> tuple[float, float]:
    """The median and the robust standard deviation (MAD scaled)."""
    median = statistics.median(values)
    return median, MAD_TO_SD * statistics.median(abs(v - median) for v in values)


def _residual(weights_kg: Mapping[date, float], day: date) -> float | None:
    """The weigh-in of ``day`` less the trend of the weigh-ins before it."""
    before = {d: w for d, w in weights_kg.items() if d < day}
    if len(before) < MIN_PRIOR_WEIGHINS or day not in weights_kg:
        return None
    return weights_kg[day] - weight_trend(before)[max(before)]


def weight_step(weights_kg: Mapping[date, float], day: date) -> Direction | None:
    """``up`` or ``down`` when the weigh-in of ``day`` is marked, else None."""
    residual = _residual(weights_kg, day)
    if residual is None:
        return None
    usual = [
        r
        for d in weights_kg
        if day - timedelta(days=SCATTER_DAYS) <= d < day
        and (r := _residual(weights_kg, d)) is not None
    ]
    if len(usual) < MIN_RESIDUALS:
        return None
    _, sd = _robust(usual)
    if abs(residual) < max(MARKED_SD * sd, MARKED_FLOOR_KG):
        return None
    return "up" if residual > 0 else "down"


def _level(value: float | None, usual: Sequence[float | None], high: bool) -> bool:
    """Whether ``value`` is clearly above (``high``) or below the usual days.

    A usual level of 0 says nothing: with no carbohydrates or salt on record
    nothing is "clearly" more or less.
    """
    known = [v for v in usual if v is not None]
    if value is None or len(known) < MIN_USUAL_DAYS:
        return False
    median, sd = _robust(known)
    if median <= 0:
        return False
    if high:
        return value > median * (1 + CLEAR_SHARE) and value >= median + CLEAR_SD * sd
    return value < median * (1 - CLEAR_SHARE) and value <= median - CLEAR_SD * sd


def water_hint_for_today(
    days: Mapping[date, DayInputs],
    rmr_kcal: float | None,
    today: date,
    today_weight_kg: float | None,
) -> dict[str, object] | None:
    """The hint for this morning's weigh-in, as the energy read's payload.

    The read's days end yesterday, so today's weigh-in comes beside them. Only
    completely logged days speak: a day with a coffee before a long ride is not
    a low carbohydrate day. The cause is read from yesterday.
    """
    if today_weight_kg is None:
        return None
    weights = {d: v.weight_kg for d, v in days.items() if v.weight_kg is not None}
    weights[today] = today_weight_kg
    hint = water_hint(
        weights,
        {
            d: DayIntake(v.carbohydrate_g, v.salt_g, v.salt_known_share)
            for d, v in days.items()
            if d < today and is_complete(v, rmr_kcal)
        },
        frozenset(d for d, v in days.items() if v.long_session),
        today,
    )
    if hint is None:
        return None
    return {
        "date": today.isoformat(),
        "direction": hint.direction,
        "causes": list(hint.causes),
        "coach_note": COACH_NOTE,
    }


def water_hint(
    weights_kg: Mapping[date, float],
    intake: Mapping[date, DayIntake],
    long_session_days: frozenset[date],
    day: date,
) -> WaterHint | None:
    """The probable-water hint for the weigh-in of ``day``, or None.

    The direction of the step must match the previous day: a jump up needs
    clearly high salt or carbohydrates, a drop clearly low carbohydrates after
    a long session. Salt says nothing unless its data is not sparse.
    """
    direction = weight_step(weights_kg, day)
    previous = intake.get(day - timedelta(days=1))
    if direction is None or previous is None:
        return None
    window = [
        intake[d]
        for d in intake
        if day - timedelta(days=USUAL_DAYS + 1) <= d < day - timedelta(days=1)
    ]
    carbs = [d.carbs_g for d in window]
    causes: list[Cause] = []
    if direction == "up":
        salted = [
            d.salt_g for d in window if d.salt_known_share >= MIN_SALT_KNOWN_SHARE
        ]
        if previous.salt_known_share >= MIN_SALT_KNOWN_SHARE and _level(
            previous.salt_g, salted, True
        ):
            causes.append("salt")
        if _level(previous.carbs_g, carbs, True):
            causes.append("carbs")
    elif (day - timedelta(days=1)) in long_session_days and _level(
        previous.carbs_g, carbs, False
    ):
        causes.append("carbs_after_session")
    return WaterHint(direction, tuple(causes)) if causes else None

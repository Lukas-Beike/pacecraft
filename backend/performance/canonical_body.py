"""Canonical Garmin body weight and body-fat inputs read through the observation port.

The weights the athlete entered by hand (no provider, #3252) are read beside
Garmin's.

The reader serves whichever source the unit of work chose; ``body_history``
shows only the days this module returns.

Per day, the last observation inside the legacy plausibility bounds wins, as the
legacy body history does.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, timedelta
from types import MappingProxyType
from typing import Any

from backend.athlete.profile import profile_body_fat_history
from backend.canonical.ordering import observation_order
from backend.canonical.records import Observation
from backend.canonical.vocabulary import MetricKind, ProviderId
from backend.runtime.ports import ObservationReader

WINDOW_DAYS = 84
BODY_FAT_READ_DAYS = WINDOW_DAYS - 1
# The weight quotient looks back 7 days before the chart window.
WEIGHT_READ_DAYS = BODY_FAT_READ_DAYS + 7
WEIGHT_BOUNDS = (30.0, 300.0)
BODY_FAT_BOUNDS = (2.0, 70.0)


@dataclass(frozen=True, slots=True)
class CanonicalBodyInputs:
    """Per-day Garmin weight and body-fat values, bounded and rounded.

    ``entered_weight_kg`` and ``entered_body_fat_pct`` hold the athlete's own
    entries (#3252, #3402); ``skinfold_body_fat_pct`` the profile's earlier
    body fat values, 3-site skinfolds, on days without an entry (#3557, #3569).
    """

    garmin_weight_kg: Mapping[date, float]
    garmin_body_fat_pct: Mapping[date, float]
    entered_weight_kg: Mapping[date, float] = MappingProxyType({})
    entered_body_fat_pct: Mapping[date, float] = MappingProxyType({})
    skinfold_body_fat_pct: Mapping[date, float] = MappingProxyType({})


EMPTY_BODY_INPUTS = CanonicalBodyInputs(
    garmin_weight_kg=MappingProxyType({}), garmin_body_fat_pct=MappingProxyType({})
)


def read_body_inputs(
    reader: ObservationReader, db: Any, today: date
) -> CanonicalBodyInputs:
    """Read the Garmin weight and body fat of the chart window up to ``today``."""
    weights = reader.observations(
        db,
        MetricKind.WEIGHT,
        start=today - timedelta(days=WEIGHT_READ_DAYS),
        end=today,
    )
    body_fat = reader.observations(
        db,
        MetricKind.BODY_FAT,
        start=today - timedelta(days=BODY_FAT_READ_DAYS),
        end=today,
    )
    # Earlier profile values are dated skinfolds; an entry of that day wins
    # (athlete decisions, #3477, #3557). Only a database holds the change history.
    profile = (
        profile_body_fat_history(db, today - timedelta(days=BODY_FAT_READ_DAYS))
        if hasattr(db, "execute")
        else {}
    )
    entered_fat = _daily(body_fat, None, today, BODY_FAT_BOUNDS)
    return CanonicalBodyInputs(
        garmin_weight_kg=_daily(weights, ProviderId.GARMIN, today, WEIGHT_BOUNDS),
        garmin_body_fat_pct=_daily(body_fat, ProviderId.GARMIN, today, BODY_FAT_BOUNDS),
        entered_weight_kg=_daily(weights, None, today, WEIGHT_BOUNDS),
        entered_body_fat_pct=entered_fat,
        skinfold_body_fat_pct={
            day: value
            for day, value in profile.items()
            if day <= today and day not in entered_fat
        },
    )


def _daily(
    observations: Sequence[Observation],
    provider: ProviderId | None,
    today: date,
    bounds: tuple[float, float],
) -> dict[date, float]:
    lower, upper = bounds
    days: dict[date, float] = {}
    for observation in sorted(observations, key=_recency_key):
        if observation.provenance.provider is not provider:
            continue
        if observation.observed_on > today:
            continue
        value = float(observation.value.value)
        if lower <= value <= upper:
            days[observation.observed_on] = round(value, 2)
    return days


def _recency_key(observation: Observation) -> tuple[Any, ...]:
    """Order readings of one day as the legacy Garmin reader does.

    Untimed readings sort before timed ones. Timed readings compare by UTC instant,
    so mixed offsets order by the moment they describe, not by their text. A naive
    timestamp is read as UTC, as the legacy reader does. Ties keep the canonical
    order.
    """
    at = observation.observed_at
    if at is None:
        return (observation.observed_on, False, 0.0, observation_order(observation))
    aware = at if at.tzinfo is not None else at.replace(tzinfo=UTC)
    return (
        observation.observed_on,
        True,
        aware.timestamp(),
        observation_order(observation),
    )

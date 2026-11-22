"""The athlete's first-threshold heart rate (a profile value).

Entered by the athlete (for example from a lab lactate or gas-exchange test) or
adopted from the estimate derived from chest-strap RR intervals, and stored in
bpm (athlete decision 2026-11-22, #3915): a later change of the lactate
threshold HR never moves it. The percent of the threshold HR is display and
derivation only. ``first_threshold_origin`` says ``estimated`` for an adopted
estimate and ``measured`` for an entry. A profile stored before #3915 holds the
percent instead (``LEGACY_PERCENT_FIELD``); the profile read derives the bpm
from it until the next save writes bpm. Stdlib and canonical text helpers only.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, Final

from backend.canonical.decimal_text import comma_decimal_float

# The stored value (bpm); empty means not set.
FIRST_THRESHOLD_FIELD: Final = "first_threshold_hr"
# The percent of LTHR a profile stored before #3915; read once, never written.
LEGACY_PERCENT_FIELD: Final = "first_threshold_pct"
# Where the stored value comes from; empty exactly when no value is stored.
FIRST_THRESHOLD_ORIGIN_FIELD: Final = "first_threshold_origin"
ORIGIN_MEASURED: Final = "measured"
ORIGIN_ESTIMATED: Final = "estimated"
FIRST_THRESHOLD_ORIGINS: Final = ("", ORIGIN_MEASURED, ORIGIN_ESTIMATED)
FIRST_THRESHOLD_BOUNDS: Final = (60, 220)
FIRST_THRESHOLD_PERCENT_BOUNDS: Final = (40.0, 100.0)


def _number(value: object) -> float | None:
    try:
        number = comma_decimal_float(value)
    except TypeError, ValueError:
        return None
    return number if math.isfinite(number) else None


def first_threshold_bpm(value: object) -> float | None:
    """A first threshold in bpm, or None if unset or implausible."""
    number = _number(value)
    low, high = FIRST_THRESHOLD_BOUNDS
    return number if number is not None and low <= number <= high else None


def first_threshold_percent(value: object) -> float | None:
    """A first threshold in percent of LTHR, or None if unset or implausible."""
    number = _number(value)
    low, high = FIRST_THRESHOLD_PERCENT_BOUNDS
    return number if number is not None and low <= number <= high else None


def percent_of_lthr(bpm: float, lthr: float) -> float | None:
    """The first threshold as percent of ``lthr``, one decimal, or None if implausible."""
    if lthr <= 0:
        return None
    return first_threshold_percent(round(bpm / lthr * 100, 1))


def stored_bpm(profile: Mapping[str, Any]) -> float | None:
    """The profile's stored first threshold in bpm, or None."""
    return first_threshold_bpm(profile.get(FIRST_THRESHOLD_FIELD))


def stored_percent(profile: Mapping[str, Any], lthr: float | None) -> float | None:
    """The stored first threshold as percent of ``lthr`` (display and caps), or None."""
    bpm = stored_bpm(profile)
    return None if bpm is None or not lthr else percent_of_lthr(bpm, lthr)


def first_threshold_origin(profile: Mapping[str, Any]) -> str:
    """Where the profile's stored first threshold comes from; empty when none is stored.

    A value stored before the origin existed was entered by the athlete.
    """
    if stored_bpm(profile) is None:
        return ""
    return (
        ORIGIN_ESTIMATED
        if profile.get(FIRST_THRESHOLD_ORIGIN_FIELD) == ORIGIN_ESTIMATED
        else ORIGIN_MEASURED
    )


def first_threshold_estimated(profile: Mapping[str, Any]) -> bool:
    """Whether the profile's stored first threshold is an adopted estimate."""
    return first_threshold_origin(profile) == ORIGIN_ESTIMATED

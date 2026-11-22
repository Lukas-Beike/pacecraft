"""Band of a planned run or ride unit without parseable workout steps (no I/O).

Steps always win; this is only the fallback for time the steps cannot resolve:
1. A numeric ``planned_intensity`` (fraction of threshold, percent above 1.5 is
   divided by 100) maps like a power step (``intensity_band``, #3915): up to
   0.75 low (LT1), up to 1.0 moderate, above the threshold high.
2. Else the session focus: recovery, easy, endurance, long are low; tempo,
   sweet_spot, threshold moderate; vo2max, anaerobic, race high. A focus is a
   rough class, so this basis is labelled approximate.
The result gives band time only, never a load number: a focus has no intensity
value, and the load of a planned unit stays with its steps.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from backend.activities.zone_settings import intensity_band as sensor_band
from backend.canonical.vocabulary import Sport
from backend.planning.session_focus import planned_intensity_factor, session_focus
from backend.planning.sport import planned_sport_environment

STEPS: Final = "steps"
PLANNED_INTENSITY: Final = "planned_intensity"
SESSION_FOCUS: Final = "session_focus"
FOCUS_BANDS: Final = {
    "recovery": "low",
    "easy": "low",
    "endurance": "low",
    "long": "low",
    "tempo": "moderate",
    "sweet_spot": "moderate",
    "threshold": "moderate",
    "vo2max": "high",
    "anaerobic": "high",
    "race": "high",
}
_ENDURANCE = frozenset({Sport.RUN, Sport.RIDE})


def intensity_band(factor: float) -> str:
    """3-zone band of an intensity as a fraction of threshold (a power step's)."""
    return sensor_band("power", factor)


def unit_band(unit: Mapping[str, Any]) -> tuple[str, str] | None:
    """(band, basis) of a run or ride unit from intensity, else focus; else unknown."""
    if planned_sport_environment(unit)[0] not in _ENDURANCE:
        return None
    factor = planned_intensity_factor(unit)
    if factor is not None and factor > 0:
        return intensity_band(factor), PLANNED_INTENSITY
    band = FOCUS_BANDS.get(session_focus(unit).value or "")
    return None if band is None else (band, SESSION_FOCUS)

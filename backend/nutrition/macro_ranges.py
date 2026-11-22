"""Read-only macro references derived from weight and a training calendar."""

from __future__ import annotations

import math
from typing import Any, Final

from backend.canonical.activity_summary import summary_leading_duration_s

# Daily carbohydrate in g/kg by training load (Thomas, Erdman & Burke 2016,
# Table 1): light, moderate (about 1 h a day), high (1-3 h), very high
# (more than 4-5 h). The day's training minutes step up at 60 and 90 minutes,
# hard work one more level, and 240 minutes are very high (#3920).
CARB_G_PER_KG: Final = ((3, 5), (5, 7), (6, 10), (8, 12))
CARB_STEP_MINUTES: Final = (60, 90)
CARB_VERY_HIGH_MINUTES: Final = 240


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def _recorded_high_seconds(zones: list[Any]) -> float | None:
    high = 0.0
    for zone in zones:
        if not isinstance(zone, dict):
            return None
        name = str(zone.get("id", ""))
        seconds = _number(zone.get("secs"))
        if not name.startswith("Z") or not name[1:].isdigit() or seconds is None:
            return None
        if int(name[1:]) >= 4:
            high += seconds
    return high


def _planned_high_seconds(profile: Any) -> float | None:
    if not isinstance(profile, dict) or profile.get("axis") != "duration":
        return None
    segments = profile.get("segments")
    if not isinstance(segments, list) or not segments:
        return None
    high = 0.0
    for segment in segments:
        if not isinstance(segment, dict):
            return None
        duration = _number(segment.get("duration"))
        zone = _number(segment.get("zone"))
        if (
            duration is None
            or zone is None
            or "-" in str(segment.get("label") or "")
            or segment.get("end_value") != segment.get("value")
        ):
            return None
        if zone >= 4:
            high += duration
    return high


def _high_seconds(row: dict[str, Any]) -> float | None:
    for key in ("power_zone_times", "pace_zone_times", "heart_rate_zone_times"):
        zones = row.get(key)
        if isinstance(zones, list) and zones:
            return _recorded_high_seconds(zones)
    return _planned_high_seconds(row.get("workout_profile"))


def _session_totals(entry: dict[str, Any]) -> tuple[float, bool] | None:
    compliance = entry.get("compliance") or {}
    if not isinstance(compliance, dict):
        return None
    row = compliance.get("actual_activity") or entry
    if not isinstance(row, dict):
        return None
    # A completed strength session counts with its total time (#2317).
    duration = _number(summary_leading_duration_s(row)) or _number(
        row.get("planned_duration_s")
    )
    high = _high_seconds(row)
    if duration is None or high is None:
        return None
    return duration / 60, high >= 600


def _calendar_date(entry: Any) -> str | None:
    if not isinstance(entry, dict):
        return None
    compliance = entry.get("compliance") or {}
    if not isinstance(compliance, dict):
        return None
    if (
        compliance.get("status") == "missed"
        or entry.get("archived")
        or entry.get("local_deleted")
    ):
        return ""
    actual = compliance.get("actual_activity")
    dated = actual if isinstance(actual, dict) else entry
    return (
        str(
            dated.get("date")
            or dated.get("started_at_local")
            or dated.get("starts_at_local")
            or ""
        )[:10]
        or None
    )


def _training_totals(calendar: list[Any], day: str) -> tuple[float, bool] | None:
    minutes = 0.0
    intense = False
    for entry in calendar:
        entry_date = _calendar_date(entry)
        if entry_date is None:
            return None
        if entry_date != day:
            continue
        totals = _session_totals(entry)
        if totals is None:
            return None
        minutes += totals[0]
        intense = intense or totals[1]
    return minutes, intense


def macro_ranges(
    weight: Any, calendar: Any, day: str, kcal: Any, energy_goal: str | None = None
) -> dict[str, Any]:
    """Calculate ranges; an unknown input leaves its comparison unavailable.

    With the energy goal "lose" protein is 1.6 to 2.4 g/kg (Hector & Phillips
    2018, R81), else 1.7 to 2.3 g/kg.
    """
    result: dict[str, Any] = {}
    mass = _number(weight)
    if mass:
        low, high = (160, 240) if energy_goal == "lose" else (170, 230)
        result["protein_g"] = (mass * low / 100, mass * high / 100)
        result["carbs_g"] = (mass * 3, mass * 5)
    energy = _number(kcal)
    if energy:
        result["fat_g"] = (energy * 0.2 / 9, energy * 0.3 / 9)
    if not mass or not isinstance(calendar, list):
        return result
    totals = _training_totals(calendar, day)
    if totals is None:
        return result
    minutes, intense = totals
    # Thomas, Erdman & Burke 2016, Table 1: light 3-5, about 1 h a day 5-7,
    # 1-3 h 6-10 and more than 4-5 h 8-12 g/kg (#3920).
    level = int(minutes >= CARB_STEP_MINUTES[0]) + int(minutes >= CARB_STEP_MINUTES[1])
    level = min(2, level + int(intense))
    if minutes >= CARB_VERY_HIGH_MINUTES:
        level = 3
    low, high = CARB_G_PER_KG[level]
    result["carbs_g"] = (mass * low, mass * high)
    return result

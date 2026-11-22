"""Recorded evidence beside the thresholds in effect (#3920, C5 and C6).

Two plain facts, never a claim that a value is wrong, and nothing is estimated:

- A threshold no recorded effort supports: the ride FTP in effect is above the
  best 20-minute power of the last 90 days, or the run threshold pace is faster
  than the best 5 km pace of the same window. Without a recorded 20-minute or
  5 km effort in the window nothing is said.
- A recorded maximum heart rate above the stored one: a chest-strap recording
  of the last 12 months exceeds the maximum heart rate in effect by at least
  3 bpm. Optical recordings never count.

Pure; reads the session curve records and the chest-strap maximum the caller
passes. This module must not import ``server.py``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

# The window of the session curve records the best efforts come from.
EFFORT_WINDOW_DAYS: Final = 90
RIDE_EFFORT_SECONDS: Final = 1200
RUN_EFFORT_METRES: Final = 5000
# A chest-strap maximum counts from this margin over the stored one.
RECORDED_MAX_HR_MARGIN_BPM: Final = 3
RECORDED_MAX_HR_DAYS: Final = 365


def _best_ride(records: Sequence[Mapping[str, Any]]) -> tuple[float, str] | None:
    found = [
        (float(point["watts"]), str(row.get("date") or ""))
        for row in records
        if row.get("sport") == "ride"
        for point in (row.get("power_profile") or {}).get("duration_curve", [])
        if point.get("duration_seconds") == RIDE_EFFORT_SECONDS
        and point.get("watts") is not None
    ]
    return max(found, default=None)


def _best_run(records: Sequence[Mapping[str, Any]]) -> tuple[float, str] | None:
    """The fastest 5 km as seconds per kilometre and its day."""
    found = [
        (
            float(point["seconds"]) / (RUN_EFFORT_METRES / 1000),
            str(row.get("date") or ""),
        )
        for row in records
        if row.get("sport") == "run"
        for point in (row.get("running_profile") or {}).get("distance", [])
        if point.get("distance_meters") == RUN_EFFORT_METRES
        and point.get("seconds") is not None
    ]
    return min(found, default=None)


def unsupported_by_recent_effort(
    records: Sequence[Mapping[str, Any]],
    *,
    ftp_w: object,
    run_pace_seconds_per_km: object,
) -> dict[str, Any]:
    """``{"ride": ..., "run": ...}`` for each threshold above its best recorded effort."""
    result: dict[str, Any] = {}
    ride = _best_ride(records)
    if isinstance(ftp_w, int | float) and ride is not None and ftp_w > ride[0]:
        result["ride"] = {
            "threshold_w": ftp_w,
            "best_w": round(ride[0]),
            "duration_seconds": RIDE_EFFORT_SECONDS,
            "window_days": EFFORT_WINDOW_DAYS,
            "date": ride[1],
        }
    run = _best_run(records)
    if (
        isinstance(run_pace_seconds_per_km, int | float)
        and run is not None
        and 0 < run_pace_seconds_per_km < run[0]
    ):
        result["run"] = {
            "threshold_pace_seconds_per_km": run_pace_seconds_per_km,
            "best_pace_seconds_per_km": round(run[0]),
            "distance_meters": RUN_EFFORT_METRES,
            "window_days": EFFORT_WINDOW_DAYS,
            "date": run[1],
        }
    return result


def recorded_max_hr_above(
    stored_max_hr: object, recorded: tuple[float, str] | None
) -> dict[str, Any] | None:
    """The chest-strap maximum (bpm, day) when it exceeds the stored one by the margin."""
    if (
        recorded is None
        or not isinstance(stored_max_hr, int | float)
        or recorded[0] < stored_max_hr + RECORDED_MAX_HR_MARGIN_BPM
    ):
        return None
    return {
        "value": round(recorded[0]),
        "date": recorded[1],
        "stored_max_hr": stored_max_hr,
        "window_days": RECORDED_MAX_HR_DAYS,
    }

"""Single-activity records: the longest and highest ride and run (#3826)."""

from collections.abc import Iterable, Mapping
from typing import Any, Final

# The canonical summary fields a single session can set a record with.
ACTIVITY_RECORD_FIELDS: Final = ("distance_m", "moving_duration_s", "elevation_gain_m")


def session_totals(summary: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The session's positive record fields, each with its source label."""
    sources = summary.get("sources") or {}
    totals: dict[str, dict[str, Any]] = {}
    for field in ACTIVITY_RECORD_FIELDS:
        value = summary.get(field)
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value > 0
        ):
            totals[field] = {"value": float(value), "source": sources.get(field)}
    return totals


def activity_records(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The largest distance, moving time and elevation gain of one ride and one run.

    Indoor and virtual rides do not count (athlete decision, #3826): their
    distance and elevation are simulated. A tie goes to the earlier session,
    the one that set the record first. A field no session has stays unknown.
    """
    rows = list(rows)
    records: list[dict[str, Any]] = []
    for sport in ("ride", "run"):
        eligible = [
            row
            for row in rows
            if row.get("sport") == sport
            and not (sport == "ride" and row.get("environment") == "indoor")
        ]
        for field in ACTIVITY_RECORD_FIELDS:
            candidates = [
                (row, (row.get("totals") or {})[field])
                for row in eligible
                if field in (row.get("totals") or {})
            ]
            if not candidates:
                records.append({"sport": sport, "field": field, "status": "unknown"})
                continue
            # Largest value first; on a tie the earlier start wins.
            row, total = min(
                candidates,
                key=lambda item: (
                    -item[1]["value"],
                    str(item[0].get("observed_at") or item[0].get("date") or ""),
                    str(item[0].get("activity_id") or ""),
                ),
            )
            records.append(
                {
                    "sport": sport,
                    "field": field,
                    "status": "ok",
                    "value": total["value"],
                    "activity_id": row.get("activity_id"),
                    "name": row.get("name"),
                    "date": row.get("date"),
                    "source": total["source"],
                }
            )
    return records

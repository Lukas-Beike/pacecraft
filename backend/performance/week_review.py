"""The review of one ISO week against the week before, without a model (#2805).

Reads only what other owners already computed: the weekly progress of both
weeks (``weekly_progress`` without curves), the plan's training calendar with its
compliance, the session summaries, the personal recovery baselines and the
check-ins. Nothing is estimated or written; no model is asked. Pure: no I/O.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any, Final

from backend.activities.activity_summary import summary_local_date
from backend.activities.session_purpose import summary_is_race
from backend.athlete.checkins import CHECKIN_SCORE_FIELDS, reports_complaint
from backend.athlete.local_date import iso_date_prefix
from backend.athlete.measurements import finite_float as number

METHOD: Final = "local-week-review-v1"
# The check-in scores the review averages (0 to 10).
REVIEW_SCORES: Final = tuple(
    field for field in CHECKIN_SCORE_FIELDS if field != "session_rpe"
)
_RECOVERY_METRICS: Final = ("sleep", "hrv", "resting_hr")


def week_review(
    *,
    monday: date,
    today: date,
    this_week: Mapping[str, Any],
    week_before: Mapping[str, Any],
    calendar: Sequence[Mapping[str, Any]],
    summaries: Sequence[Mapping[str, Any]],
    recovery: Mapping[str, Any],
    checkins: Sequence[Mapping[str, Any]],
    timezone: str,
) -> dict[str, Any]:
    """The review of the week from ``monday`` with the week before alongside.

    ``this_week`` and ``week_before`` are one-week ``weekly_progress`` results;
    ``calendar`` the plan's ``training_calendar`` rows; ``recovery`` the
    ``personal_recovery`` result.
    """
    sunday = monday + timedelta(days=6)
    effective_end = min(sunday, today)
    days = (monday.isoformat(), sunday.isoformat())
    rows = _planned_rows(calendar, *days)
    week_sessions = [
        row
        for row in summaries
        if days[0] <= summary_local_date(row, timezone) <= days[1]
    ]
    return {
        "method": METHOD,
        "week_start": days[0],
        "week_end": days[1],
        "effective_end": effective_end.isoformat(),
        "plan": _plan(rows),
        "load": {
            "this_week": _load(this_week),
            "week_before": _load(week_before),
            "shares": _shares(this_week),
        },
        "recovery": _recovery(recovery, monday),
        "notable": _notable(week_sessions, rows, timezone),
        "checkins": _checkins(checkins, monday, effective_end),
    }


def _planned_rows(
    calendar: Sequence[Mapping[str, Any]], first: str, last: str
) -> list[Mapping[str, Any]]:
    """The week's planned units (not the completed activities of the calendar)."""
    return [
        row
        for row in calendar
        if not row.get("is_completed_activity") and first <= _day(row) <= last
    ]


def _day(row: Mapping[str, Any]) -> str:
    return iso_date_prefix(str(row.get("starts_at_local") or row.get("date") or ""))


def _status(row: Mapping[str, Any]) -> str:
    status = (row.get("compliance") or {}).get("status")
    return status if status in ("completed", "missed") else "open"


def _plan(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Completed, missed and open units and the share of planned time done.

    The share counts the planned time of units whose day has passed and the
    actual time of the completed ones; unknown durations leave it out.
    """
    statuses = [_status(row) for row in rows]
    passed = [
        row for row, status in zip(rows, statuses, strict=True) if status != "open"
    ]
    planned = [
        value
        for row in passed
        if (value := number((row.get("compliance") or {}).get("planned_duration")))
    ]
    done = [
        number((row.get("compliance") or {}).get("actual_duration")) or 0.0
        for row in passed
        if _status(row) == "completed"
    ]
    share = (
        round(sum(done) / sum(planned) * 100)
        if planned and len(planned) == len(passed)
        else None
    )
    return {
        "planned": len(rows),
        "completed": statuses.count("completed"),
        "missed": statuses.count("missed"),
        "open": statuses.count("open"),
        "duration_share": share,
        "missed_units": [
            {"date": _day(row), "name": row.get("name")}
            for row, status in zip(rows, statuses, strict=True)
            if status == "missed"
        ],
    }


def _load(progress: Mapping[str, Any]) -> dict[str, Any]:
    """Time, sessions and training load of the one week of ``progress``."""
    week = (progress.get("week_list") or [{}])[0]
    bucket = ((week.get("sports") or {}).get("all")) or {}
    return {
        "duration_s": (bucket.get("duration_s") or {}).get("value"),
        "sessions": bucket.get("sessions", 0),
        "training_load": (bucket.get("training_load") or {}).get("value"),
    }


def _shares(progress: Mapping[str, Any]) -> dict[str, int] | None:
    """Low, moderate and high shares of the week's zone time, in percent."""
    week = (progress.get("week_list") or [{}])[0]
    zones = (((week.get("sports") or {}).get("all")) or {}).get("intensity_3zone") or {}
    seconds = {
        band: number(value) or 0.0
        for band, value in (zones.get("seconds") or {}).items()
    }
    total = sum(seconds.values())
    if total <= 0:
        return None
    return {band: round(value / total * 100) for band, value in seconds.items()}


def _recovery(recovery: Mapping[str, Any], monday: date) -> dict[str, Any]:
    """Weekly means of sleep, HRV and resting HR against the personal range."""
    this_week = (monday.isoformat(), (monday + timedelta(days=6)).isoformat())
    before = (
        (monday - timedelta(days=7)).isoformat(),
        (monday - timedelta(days=1)).isoformat(),
    )
    result: dict[str, Any] = {}
    for metric in _RECOVERY_METRICS:
        best = _best_baseline(recovery, metric, this_week)
        values = [] if best is None else _values(best, this_week)
        if best is None or not values:
            continue
        mean = statistics.fmean(values)
        earlier = _values(best, before)
        entry: dict[str, Any] = {
            "mean": round(mean, 2),
            "nights": len(values),
            "unit": best.get("unit"),
            "source": best.get("source"),
            "week_before": round(statistics.fmean(earlier), 2) if earlier else None,
        }
        position = _position(mean, best)
        if position is not None:
            entry["position"] = position
        result[metric] = entry
    return result


def _position(mean: float, baseline: Mapping[str, Any]) -> str | None:
    """Below, within or above the baseline's current personal range, if known."""
    lower, upper = number(baseline.get("lower")), number(baseline.get("upper"))
    if lower is None or upper is None:
        return None
    if mean < lower:
        return "below"
    return "above" if mean > upper else "within"


def _best_baseline(
    recovery: Mapping[str, Any], metric: str, span: tuple[str, str]
) -> Mapping[str, Any] | None:
    """The metric's baseline with the most nights in ``span`` (one source)."""
    candidates = [
        baseline
        for baseline in recovery.get("baselines") or []
        if baseline.get("metric") == metric
    ]
    return max(candidates, key=lambda item: len(_values(item, span)), default=None)


def _values(baseline: Mapping[str, Any], span: tuple[str, str]) -> list[float]:
    return [
        value
        for item in baseline.get("history") or []
        if span[0] <= str(item.get("date")) <= span[1]
        and (value := number(item.get("value"))) is not None
    ]


def _notable(
    sessions: Sequence[Mapping[str, Any]],
    rows: Sequence[Mapping[str, Any]],
    timezone: str,
) -> list[dict[str, Any]]:
    """The highest-load session, every race and every missed planned unit."""
    notable: list[dict[str, Any]] = []
    loaded = [row for row in sessions if number(row.get("training_load")) is not None]
    top = max(
        loaded, key=lambda row: number(row.get("training_load")) or 0.0, default=None
    )
    if top is not None:
        notable.append(_session(top, "highest_load", timezone))
    notable += [
        _session(row, "race", timezone)
        for row in sessions
        if summary_is_race(row) and row is not top
    ]
    notable += [
        {"date": _day(row), "name": row.get("name"), "reason": "missed"}
        for row in rows
        if _status(row) == "missed"
    ]
    return sorted(notable, key=lambda item: str(item["date"]))


def _session(row: Mapping[str, Any], reason: str, timezone: str) -> dict[str, Any]:
    entry = {
        "date": summary_local_date(row, timezone),
        "name": row.get("name"),
        "reason": reason,
        "activity_id": row.get("id"),
        "sport": row.get("sport"),
        "training_load": number(row.get("training_load")),
    }
    if reason == "highest_load" and summary_is_race(row):
        entry["race"] = True
    return entry


def _checkins(
    checkins: Sequence[Mapping[str, Any]], monday: date, effective_end: date
) -> dict[str, Any]:
    """Days entered of the days passed, complaint days and mean scores."""
    first, last = monday.isoformat(), effective_end.isoformat()
    rows = [row for row in checkins if first <= str(row.get("checkin_date")) <= last]
    scores = {
        field: round(statistics.fmean(values), 1)
        for field in REVIEW_SCORES
        if (
            values := [
                value for row in rows if (value := number(row.get(field))) is not None
            ]
        )
    }
    return {
        "entered": len(rows),
        "days": (effective_end - monday).days + 1,
        "pain_days": sum(
            reports_complaint(row.get("pain")) for row in rows if row.get("pain")
        ),
        "illness_days": sum(
            reports_complaint(row.get("illness")) for row in rows if row.get("illness")
        ),
        "scores": scores,
    }

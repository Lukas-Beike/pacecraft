"""Diagnostic projection of plan-push jobs of every provider and unsynced plan units."""

from __future__ import annotations

import json
from typing import Any

from backend.canonical.vocabulary import ProviderId, RemoteEntityKind, Sport
from backend.db import planning_store
from backend.db.repositories import KeyValueRepository
from backend.planning.exercise_mappings import remembered_exercises
from backend.planning.push_state import garmin_fingerprint, skips_intervals
from backend.planning.sport import planned_sport_environment
from backend.sync.garmin_strength_push import APP_WORKOUT_CLEANUP_KEY
from backend.sync.jobs import list_jobs, status_counts, stuck_counts

_OUTCOMES = frozenset({"ok", "failed", "skipped"})

_JOB_LIMIT = 10
_UNIT_LIMIT = 50


def summarize_plan_sync(
    db: Any, since: str | None = None, stuck_before: str | None = None
) -> dict[str, Any]:
    """Report plan_push outcomes by status and error class, never provider error text.

    The unsynced units are the local planned calendar units Intervals.icu takes,
    its indoor and virtual rides (#2419); the last plan sync of each provider is
    in ``sync.providers``. With ``since`` (a ``utc_timestamp``), ``push_counts``
    holds every provider's job and item statuses and error classes of the
    pushes since then, settled items included (#2878): the recent jobs above
    are only the newest ten of all providers. ``stuck_before`` adds each
    provider's open jobs that should have finished by then as ``stuck_jobs``
    (#2953).
    """

    jobs = list_jobs(db, _JOB_LIMIT, "plan_push")
    unsynced = [
        row
        for row in planning_store.unsynced_planned_units(db)
        if not _skipped_by_intervals(row["payload"])
    ]
    return {
        "recent_jobs": [
            {
                "id": job["id"],
                "provider": job["provider"],
                "status": job["status"],
                "requested_by": job["requested_by"],
                "entries": len(job["payload"].get("entries") or []),
                "repair": bool(job["payload"].get("repair")),
                "attempts": job["attempts"],
                "error_class": job["error_class"],
                "created_at": job["created_at"],
                "finished_at": job["finished_at"],
                "items": [
                    {
                        "status": item["status"],
                        "error_class": item["error_class"],
                    }
                    for item in job["items"]
                    if item["status"] not in {"completed", "skipped"}
                ],
            }
            for job in jobs
        ],
        "unsynced_total": len(unsynced),
        "unsynced_units": [
            {
                "local_id": unit["local_id"],
                "date": unit["date"],
                "sync_state": unit["sync_state"],
                "has_error": bool(unit["sync_error"]),
            }
            for unit in unsynced[:_UNIT_LIMIT]
        ],
        **(
            {}
            if since is None
            else {"push_counts": status_counts(db, "plan_push", since)}
        ),
        **(
            {}
            if stuck_before is None
            else {"stuck_jobs": stuck_counts(db, "plan_push", stuck_before)}
        ),
    }


def _skipped_by_intervals(raw_payload: Any) -> bool:
    try:
        payload = json.loads(raw_payload or "{}")
    except TypeError, ValueError:
        return False
    return isinstance(payload, dict) and skips_intervals(payload)


def summarize_garmin_removal(db: Any) -> dict[str, Any]:
    """Counts only: the one-time removal's state, removals and the last run.

    ``past_links_pending`` counts the past linked workouts the last run left,
    by the athlete-local day the removal uses.
    """
    try:
        state = json.loads(
            KeyValueRepository(str).get(db, APP_WORKOUT_CLEANUP_KEY) or "{}"
        )
    except TypeError, ValueError:
        state = {}
    state = state if isinstance(state, dict) else {}
    last_run = state.get("last_run_at")
    checked = state.get("checked")
    outcome = state.get("last_outcome")
    return {
        "one_time_done": state.get("done") is True,
        "checked": len(checked) if isinstance(checked, list) else 0,
        "removed": _count(state.get("removed")),
        "last_run_at": last_run if isinstance(last_run, str) else None,
        "last_outcome": outcome if outcome in _OUTCOMES else None,
        "past_links_pending": _count(state.get("pending")),
    }


def summarize_garmin_strength_push(db: Any, today: str) -> dict[str, Any]:
    """Counts only: how the strength units from ``today`` on were sent (#2850).

    The app sends a strength unit to Garmin as one step per exercise with its
    sets, reps and load (warm-up sets with their own); a unit sent only to
    Intervals.icu reaches the watch through it as a single step without
    exercises (#1802). Past days are left out: their Garmin copies are removed.
    A Garmin unit changed since its push counts as ``changed_since_push``,
    since its stored structure is not what Garmin holds; units imported from
    Intervals.icu were never sent by the app and are left out.
    """
    garmin, intervals = ProviderId.GARMIN.value, ProviderId.INTERVALS.value
    sent = {
        "units": 0,
        "changed_since_push": 0,
        "without_structure": 0,
        "one_exercise": 0,
        "several_exercises": 0,
        "with_kg": 0,
    }
    only = {"units": 0, "without_structure": 0}
    remembered = remembered_exercises(db)
    rows = planning_store.linked_unit_rows(
        db, RemoteEntityKind.PLANNED_UNIT.value, garmin
    )
    for row in rows:
        unit = _strength_unit(row["payload"], today)
        if unit is None:
            continue
        exercises = [
            item
            for item in unit.get("strength_structure") or []
            if isinstance(item, dict)
        ]
        providers = set(str(row["providers"] or "").split(","))
        if garmin in providers:
            sent["units"] += 1
            if garmin_fingerprint(unit, remembered) != row["baseline"]:
                sent["changed_since_push"] += 1
                continue
            sent[_shape(exercises)] += 1
            sent["with_kg"] += any(_kg(item) for item in exercises)
        elif intervals in providers:
            only["units"] += 1
            only["without_structure"] += not exercises
    return {garmin: sent, intervals: only}


def _strength_unit(payload: Any, today: str) -> dict[str, Any] | None:
    """An active, app-made strength unit dated ``today`` or later, else None."""
    try:
        unit = json.loads(payload)
    except TypeError, ValueError:
        return None
    if (
        not isinstance(unit, dict)
        or unit.get("archived")
        or unit.get("local_deleted")
        or unit.get("source") == ProviderId.INTERVALS.value
        or str(unit.get("date") or "")[:10] < today
    ):
        return None
    return unit if planned_sport_environment(unit)[0] is Sport.STRENGTH else None


def _kg(item: dict[str, Any]) -> bool:
    """Whether the exercise or its warm-up sends a weight."""
    warmup = item.get("warmup")
    loads = [item.get("kg"), warmup.get("kg") if isinstance(warmup, dict) else None]
    return any(isinstance(kg, int | float) and kg > 0 for kg in loads)


def _shape(exercises: list[dict[str, Any]]) -> str:
    if not exercises:
        return "without_structure"
    return "one_exercise" if len(exercises) == 1 else "several_exercises"


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0

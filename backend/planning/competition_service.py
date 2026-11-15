"""Transactional local competition use cases."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import date
from typing import Any

from backend import change_history
from backend.errors import COMPETITION_NOT_FOUND_ERROR, AppError
from backend.planning import competitions

_REQUIRED_FIELDS = ("name", "event_date", "sport", "priority")
_OPTIONAL_FIELDS = (
    "starts_at_local",
    "distance",
    "target",
    "course_profile",
    "notes",
    "description",
    "planned_duration_s",
    "location",
)
# Upcoming races only: past races stay as the archive and never count.
_MAX_UPCOMING_COMPETITIONS = 20


def _competition_id(value: Any, *, required: bool = False) -> str:
    raw = str(value or "").strip()
    if not raw:
        if required:
            raise AppError(400, "Eine lokale Wettkampf-ID ist erforderlich.")
        return ""
    try:
        return str(uuid.UUID(raw))
    except (ValueError, AttributeError) as exc:
        raise AppError(400, "Ungültige lokale Wettkampf-ID.") from exc


# The fields a full local update writes, except the location, which never
# enters an Intervals.icu payload (#2803).
_SYNCED_UPDATE_FIELDS = (
    "name",
    "event_date",
    "sport",
    "environment",
    "sport_detail",
    "priority",
    "distance",
    "target",
    "course_profile",
    "notes",
    "category",
    "starts_at_local",
    "description",
    "planned_duration_s",
)


def _only_local_fields_changed(
    existing: dict[str, Any], normalized: dict[str, Any]
) -> bool:
    """Whether the edit changes nothing a full update would mark for sync.

    Any other difference, including None against an empty string, counts as a
    synced change, so the competition is pushed again when in doubt.
    """
    return all(
        existing.get(field) == normalized.get(field) for field in _SYNCED_UPDATE_FIELDS
    )


def _coach_payload(arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AppError(400, "Die Wettkampfdaten müssen als Objekt übergeben werden.")
    planned_duration_s = arguments.get("planned_duration_s")
    if planned_duration_s == -1:
        planned_duration_s = None
    return {
        "id": str(arguments.get("competition_id") or "").strip(),
        "name": arguments.get("name"),
        "event_date": arguments.get("event_date"),
        "starts_at_local": arguments.get("starts_at_local"),
        "sport": arguments.get("sport"),
        "environment": arguments.get("environment"),
        "sport_detail": arguments.get("sport_detail"),
        "priority": arguments.get("priority"),
        "distance": arguments.get("distance"),
        "target": arguments.get("target"),
        "course_profile": arguments.get("course_profile"),
        "location": arguments.get("location"),
        "notes": arguments.get("notes"),
        "description": arguments.get("description"),
        "planned_duration_s": planned_duration_s,
    }


def _merge_update(
    value: dict[str, Any], arguments: dict[str, Any], existing: dict[str, Any] | None
) -> None:
    if not existing:
        return
    for field in _REQUIRED_FIELDS:
        if field not in arguments:
            value[field] = existing.get(field)
    if "sport" not in arguments:
        value["environment"] = existing.get("environment")
        value["sport_detail"] = existing.get("sport_detail")
    for field in _OPTIONAL_FIELDS:
        # A field sent empty (or null) is cleared; an absent one keeps its value.
        if field not in arguments and existing.get(field) not in (None, ""):
            value[field] = existing[field]


def _with_derived_event_date(
    value: dict[str, Any], arguments: dict[str, Any]
) -> dict[str, Any]:
    """Take the race day from a sent start time; reject a different explicit day."""
    start = value.get("starts_at_local")
    if start in (None, ""):
        return arguments
    start_day = competitions.competition_start(start)[0]
    sent_day = value.get("event_date")
    if sent_day in (None, ""):
        value["event_date"] = start_day
        return {**arguments, "event_date": start_day}
    if not str(sent_day).strip().startswith(start_day):
        raise AppError(
            400,
            "Startzeit und Wettkampftag passen nicht zusammen: "
            f"{start_day} statt {str(sent_day).strip()[:10]}.",
        )
    return arguments


class CompetitionService:
    """Own local competition reads, writes, conflicts, and transactions."""

    def __init__(
        self,
        database_manager: Any,
        repository: Any,
        now: Callable[[], str],
        local_today: Callable[[], date],
    ):
        self._database_manager = database_manager
        self._repository = repository
        self._now = now
        self._local_today = local_today

    def today(self) -> date:
        """Return the athlete's local calendar day."""
        return self._local_today()

    def list(
        self, limit: int | None = None, *, db: Any | None = None
    ) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 500)) if limit is not None else None
        if db is not None:
            return self._repository.list(db, bounded)
        with self._database_manager.reader() as connection:
            return self._repository.list(connection, bounded)

    def get(self, competition_id: str) -> dict[str, Any] | None:
        with self._database_manager.reader() as connection:
            return self._repository.get(connection, competition_id)

    def restore_in_transaction(
        self,
        db: Any,
        entity_id: str,
        current: dict[str, Any] | None,
        target: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Restore one competition within the caller's transaction."""
        if target is None:
            self._repository.delete(db, entity_id)
            return None

        normalized = competitions.normalize_competition(
            {**(current or {}), **target, "id": entity_id}
        )
        normalized["id"] = entity_id
        if current and _only_local_fields_changed(current, normalized):
            # Undoing a location-only edit keeps the sync state, like the edit.
            self._repository.update_location(db, normalized, self._now())
            return {
                **normalized,
                "sync_state": str(current.get("sync_state") or "local"),
            }
        if current:
            self._repository.update_local(db, normalized, self._now())
        else:
            self._repository.create_local(db, normalized, self._now())
        return {**normalized, "sync_state": "local"}

    def save(self, arguments: Any) -> dict[str, Any]:
        now = self._now()
        value = _coach_payload(arguments)
        competition_id = _competition_id(value.get("id"))
        arguments = _with_derived_event_date(value, arguments)
        with self._database_manager.unit_of_work() as db:
            existing = (
                self._repository.get(db, competition_id) if competition_id else None
            )
            if competition_id and not existing:
                raise AppError(404, COMPETITION_NOT_FOUND_ERROR)
            _merge_update(value, arguments, existing)
            normalized = competitions.normalize_competition(value)
            if competition_id:
                normalized["id"] = competition_id
            despite = self._same_day(db, normalized, arguments, existing)
            today = self._local_today().isoformat()
            previous_day = str((existing or {}).get("event_date") or "")[:10]
            was_upcoming = bool(existing) and previous_day >= today
            if (
                str(normalized["event_date"]) >= today
                and not was_upcoming
                and self._repository.count_upcoming(db, today)
                >= _MAX_UPCOMING_COMPETITIONS
            ):
                raise AppError(
                    400,
                    "Es können maximal 20 anstehende Wettkämpfe gespeichert werden.",
                )
            sync_state = "local"
            if existing and _only_local_fields_changed(existing, normalized):
                # The location never leaves the app, so the sync state stays.
                self._repository.update_location(db, normalized, now)
                sync_state = str(existing.get("sync_state") or "local")
                status = "updated"
            elif existing:
                self._repository.update_local(db, normalized, now)
                status = "updated"
            else:
                self._repository.create_local(db, normalized, now)
                status = "created"
            change_history.record_change(
                db,
                "competition",
                normalized["id"],
                "create" if status == "created" else "update",
                existing,
                {**normalized, "sync_state": sync_state},
            )
            all_competitions = self._repository.list(db)
            saved = next(
                item for item in all_competitions if item["id"] == normalized["id"]
            )
        return {
            "status": status,
            "competition": saved,
            "competitions": all_competitions,
            # Counted by the Coach diagnostics (#3080).
            "created_despite_candidate": despite,
        }

    def _same_day(
        self,
        db: Any,
        normalized: dict[str, Any],
        arguments: dict[str, Any],
        existing: dict[str, Any] | None,
    ) -> int:
        """Refuse a new competition the stored ones already hold (#3080).

        One of its sport and environment on its day, or an upcoming one of its
        sport with the same name (a race moved to another day is updated, not
        created again). The Coach updates that one by its competition_id; a
        second race (``new_because`` says why) is kept and counted. An edit is
        never refused.
        """
        if existing:
            return 0
        day = str(normalized["event_date"])[:10]
        today = self._local_today().isoformat()
        name = str(normalized.get("name") or "").casefold()
        match = next(
            (
                item
                for item in self._repository.list(db)
                if item.get("sport") == normalized.get("sport")
                and (
                    (
                        str(item.get("event_date") or "")[:10] == day
                        and item.get("environment") == normalized.get("environment")
                    )
                    or (
                        str(item.get("name") or "").casefold() == name
                        and str(item.get("event_date") or "")[:10] >= today
                    )
                )
            ),
            None,
        )
        if match is None:
            return 0
        if str(arguments.get("new_because") or "").strip():
            return 1
        error = AppError(
            409,
            "Dieser Wettkampf ist schon gespeichert: ändere ihn mit seiner "
            "competition_id oder nenne in new_because, warum es ein zweiter ist.",
            reason="competition_exists",
        )
        # For the model only: the competition to update instead.
        setattr(  # noqa: B010
            error,
            "findings",
            [
                {
                    "code": "competition_exists",
                    "competition_id": match["id"],
                    "name": match.get("name"),
                    "event_date": str(match.get("event_date") or "")[:10],
                    "sport": match.get("sport"),
                    "environment": match.get("environment"),
                }
            ],
        )
        raise error

    def delete(self, competition_id: Any) -> dict[str, Any]:
        now = self._now()
        normalized_id = _competition_id(competition_id, required=True)
        with self._database_manager.unit_of_work() as db:
            row = self._repository.get(db, normalized_id)
            if not row:
                raise AppError(404, COMPETITION_NOT_FOUND_ERROR)
            remote_sync_pending = bool(
                row.get("intervals_event_id") or row.get("external_id")
            )
            if remote_sync_pending:
                self._repository.add_tombstone(
                    db,
                    str(uuid.uuid4()),
                    row.get("intervals_event_id"),
                    row.get("external_id"),
                    now,
                    name=row.get("name"),
                    event_date=row.get("event_date"),
                )
            self._repository.delete(db, normalized_id)
            change_history.record_change(
                db, "competition", normalized_id, "delete", row, None
            )
            all_competitions = self._repository.list(db)
        return {
            "status": "deleted",
            "competition_id": normalized_id,
            "remote_sync_pending": remote_sync_pending,
            "competitions": all_competitions,
        }

    def resolve_conflict(self, competition_id: Any, strategy: Any) -> dict[str, Any]:
        now = self._now()
        normalized_id = _competition_id(competition_id, required=True)
        selected = str(strategy or "").strip().casefold()
        if selected not in {"keep_local", "adopt_remote"}:
            raise AppError(400, "Ungültige Konfliktstrategie.")
        with self._database_manager.unit_of_work() as db:
            row = self._repository.get(db, normalized_id)
            if not row:
                raise AppError(404, COMPETITION_NOT_FOUND_ERROR)
            if row.get("sync_state") != "conflict" or not row.get("sync_conflict"):
                raise AppError(
                    409,
                    "Für diesen Wettkampf liegt kein offener Synchronisierungskonflikt vor.",
                )
            try:
                conflict = json.loads(row["sync_conflict"])
            except (TypeError, ValueError) as exc:
                raise AppError(
                    409,
                    "Der gespeicherte Synchronisierungskonflikt ist nicht mehr gültig.",
                ) from exc
            remote = conflict.get("remote") if isinstance(conflict, dict) else None
            remote = remote if isinstance(remote, dict) else {}
            if selected == "adopt_remote":
                data = competitions.remote_competition_data(remote)
                if not data or not data.get("intervals_event_id"):
                    raise AppError(
                        409, "Das Remote-Event kann nicht übernommen werden."
                    )
                external_id = str(
                    remote.get("external_id")
                    or row.get("external_id")
                    or competitions.competition_external_id(normalized_id)
                )
                self._repository.resolve_adopt_remote(
                    db, normalized_id, data, external_id, now
                )
            else:
                self._repository.resolve_keep_local(db, normalized_id, now)
            all_competitions = self._repository.list(db)
            saved = next(
                item for item in all_competitions if item["id"] == normalized_id
            )
        return {
            "status": "resolved",
            "strategy": selected,
            "competition": saved,
            "competitions": all_competitions,
        }

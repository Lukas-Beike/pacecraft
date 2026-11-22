"""Read local change history and reconstruct its prior local state."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from backend import change_history, pagination
from backend.athlete.profile import DEFAULT_PROFILE, audit_view
from backend.canonical.change_detail import change_detail, detail_line
from backend.db import planning_store
from backend.errors import AppError

# What the athlete calls a record whose change line has no name of its own.
_OTHER_NAMES = {
    "zone_configuration": "Zonen",
    "activity_session": "Einheit",
    "food_database_entry": "Lebensmittel",
    "equipment_assignment": "Ausrüstungszuordnung",
    "equipment_planned": "Ausrüstungszuordnung",
    "equipment_rule": "Ausrüstungsregel",
    "equipment_rule_apply": "Ausrüstungsregel",
    "equipment_maintenance": "Wartung",
    "metric_export": "Export zum Anbieter",
    "nutrition_product_cleanup": "Produkt-Aufräumen",
}
# The fields that name a record and its day, taken from its current state when the diff has none.
_IDENTITY_FIELDS = (
    "name",
    "description",
    "date",
    "event_date",
    "meal_date",
    "start_date",
)


def _line(row: dict[str, Any], identity: Mapping[str, Any]) -> str:
    """The athlete's readable line of one entry, from its stored diff.

    ``identity`` is the record's current name and day: a diff holds only the changed fields.
    """
    try:
        fields = json.loads(row["diff"] or "{}").get("fields")
    except TypeError, ValueError, AttributeError:
        fields = None
    changed = fields.items() if isinstance(fields, dict) else ()
    # Fields without a value on one side are left out, so the record is the side that has them.
    before = {k: v["before"] for k, v in changed if v.get("before") is not None}
    after = {k: v["after"] for k, v in changed if v.get("after") is not None}
    # A check-in is keyed by its day.
    key = {"checkin_date": row["entity_id"]} if row["entity_type"] == "checkin" else {}
    named = {**identity, **key}
    # A create or delete names its record; only an update lists changed fields.
    if row["action"] == "update":
        old, new = {**named, **before}, {**named, **after}
    else:
        old = new = {**named, **(before if row["action"] == "delete" else after)}
    detail = change_detail(row["entity_type"], old, new)
    line = detail_line(row["entity_type"], row["action"], detail)
    if line or row["entity_type"] not in _OTHER_NAMES:
        return line
    return f"{_OTHER_NAMES[row['entity_type']]}: {_OTHER_ACTIONS.get(row['action'], 'geändert')}"


_OTHER_ACTIONS = {
    "create": "neu",
    "delete": "gelöscht",
    "undo": "zurückgenommen",
}


class ChangeHistoryService:
    def __init__(
        self,
        database_manager: Any,
        profile_repository: Any,
        zone_service: Any | None = None,
        feedback_service: Any | None = None,
        session_corrections: Any | None = None,
        *,
        record_services: Mapping[str, Any] | None = None,
    ):
        self._database_manager = database_manager
        self._profile_repository = profile_repository
        self._zone_service = zone_service
        self._feedback_service = feedback_service
        self._session_corrections = session_corrections
        # Entity type -> service with current(db, id) -> (value, projection).
        self._record_services = dict(record_services or {})

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        # Retention is enforced on read in SQL; recording a change prunes rows.
        cutoff = (
            datetime.now(UTC) - timedelta(days=change_history.RETENTION_DAYS)
        ).isoformat()
        with self._database_manager.read_snapshot() as db:
            rows = db.execute(
                "SELECT id, entity_type, entity_id, action, source, created_at, "
                "before_hash, after_hash, diff FROM change_history "
                "WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?",
                (cutoff, max(1, min(int(limit), change_history.MAX_ROWS))),
            ).fetchall()
        return [change_history.public_view(dict(row)) for row in rows]

    def page(self, limit: Any = None, cursor: Any = None) -> dict[str, Any]:
        """One newest-first page for the athlete's history view.

        Each entry carries its readable ``line`` and ``can_undo``: a create,
        update or delete that is still its record's newest entry. The Coach
        list stays free of record values.
        """
        size = pagination.api_page_limit(limit, 30, 100)
        after = pagination.decode_page_cursor(cursor)
        cutoff = (
            datetime.now(UTC) - timedelta(days=change_history.RETENTION_DAYS)
        ).isoformat()
        where, args = "h.created_at >= ?", [cutoff]
        if isinstance(after, list) and len(after) == 2:
            where += " AND (h.created_at, h.id) < (?, ?)"
            args += [str(after[0]), str(after[1])]
        with self._database_manager.read_snapshot() as db:
            rows = db.execute(
                "SELECT h.id, h.entity_type, h.entity_id, h.action, h.source, "
                "h.created_at, h.before_hash, h.after_hash, h.diff, NOT EXISTS("
                "SELECT 1 FROM change_history n WHERE n.entity_type=h.entity_type "
                "AND n.entity_id=h.entity_id AND (n.created_at, n.id) > "
                "(h.created_at, h.id)) AS newest FROM change_history h "
                f"WHERE {where} ORDER BY h.created_at DESC, h.id DESC LIMIT ?",
                (*args, size + 1),
            ).fetchall()
            identities = [self._identity(db, row) for row in rows[:size]]
        changes = []
        for row, identity in zip(rows[:size], identities, strict=True):
            view = change_history.public_view(dict(row))
            view["line"] = _line(dict(row), identity)
            view["can_undo"] = (
                bool(row["newest"])
                and row["action"] in {"create", "update", "delete"}
                and view.get("undoable", True)
            )
            changes.append(view)
        last = rows[size - 1] if len(rows) > size else None
        return {
            "changes": changes,
            "next_cursor": pagination.encode_page_cursor(
                [last["created_at"], last["id"]]
            )
            if last is not None
            else None,
            "limit": size,
        }

    def _identity(self, db: Any, row: Mapping[str, Any]) -> dict[str, Any]:
        """The record's current name and day, empty when it is gone or reads nothing."""
        try:
            _, projection = self.current(db, row["entity_type"], row["entity_id"])
        except AppError, KeyError, TypeError, ValueError:
            return {}
        return {
            field: value
            for field, value in (projection or {}).items()
            if field in _IDENTITY_FIELDS and isinstance(value, (str, int, float))
        }

    def current(
        self, db: Any, entity_type: str, entity_id: str
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        value: dict[str, Any] | None
        if entity_type == "profile":
            payload = self._profile_repository.get(db)
            try:
                stored = json.loads(payload or "{}")
                value = (
                    audit_view(stored)
                    if isinstance(stored, dict)
                    else dict(DEFAULT_PROFILE)
                )
            except TypeError, json.JSONDecodeError:
                value = dict(DEFAULT_PROFILE)
        elif entity_type == "zone_configuration" and self._zone_service is not None:
            return self._zone_service.current(db, entity_id)
        elif entity_type == "activity_feedback" and self._feedback_service is not None:
            return self._feedback_service.current(db, entity_id)
        elif (
            entity_type == "activity_session" and self._session_corrections is not None
        ):
            return self._session_corrections.current(db, entity_id)
        elif entity_type in self._record_services:
            return self._record_services[entity_type].current(db, entity_id)
        else:
            value = self._current_record(db, entity_type, entity_id)
        return value, change_history.audit_projection(entity_type, value)

    @staticmethod
    def _current_record(
        db: Any, entity_type: str, entity_id: str
    ) -> dict[str, Any] | None:
        if entity_type == "workout_library":
            row = planning_store.library_row(db, entity_id)
            return dict(row) if row else None
        if entity_type == "planned_unit":
            row = planning_store.planned_unit_row(db, entity_id)
            return dict(row) if row else None
        queries = {
            "competition": "SELECT * FROM competitions WHERE id=?",
            "training_plan": "SELECT * FROM training_plans WHERE id=?",
        }
        query = queries.get(entity_type)
        if query is None:
            raise AppError(400, "Unbekannte lokale Änderung.")
        row = db.execute(query, (entity_id,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def target(
        row: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        try:
            diff = json.loads(row.get("diff") or "{}")
        except (TypeError, ValueError) as exc:
            raise AppError(409, "Die Änderungshistorie ist beschädigt.") from exc
        fields = diff.get("fields") if isinstance(diff, dict) else None
        if not isinstance(fields, dict):
            raise AppError(
                409, "Die Änderungshistorie enthält keinen wiederherstellbaren Diff."
            )
        if row.get("action") == "create":
            return None, None
        if row.get("action") not in {"update", "delete"}:
            raise AppError(
                409, "Diese Änderung kann nicht erneut zurückgenommen werden."
            )
        target = {
            field: change.get("before")
            for field, change in fields.items()
            if isinstance(change, dict) and "before" in change
        }
        return target, target

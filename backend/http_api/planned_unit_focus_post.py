"""Authenticated POST routes that set the focus, the day or a Garmin exercise of a unit."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from backend import change_history
from backend.canonical.planning_fields import FOCUS_ONLY_KEYS
from backend.errors import AppError
from backend.http_api.route_table import Access, Route, RouteRequest
from backend.planning.plan_guard_service import PlanCheckError
from backend.planning.planned_unit_edit_service import illness_block


class PlannedUnitFocusPostRoutes:
    """Set the focus fields or the day of one unit, delete it, or pick a Garmin exercise."""

    def __init__(
        self,
        planned_unit_service: Callable[[], Any],
        database_manager: Callable[[], Any],
        training_changes: Callable[[], Any] | None = None,
        unit_edit: Callable[[], Any] | None = None,
    ):
        self._planned_unit_service = planned_unit_service
        self._database_manager = database_manager
        self._training_changes = training_changes
        self._unit_edit = unit_edit

    def routes(self) -> list[Route]:
        return [
            Route(
                "POST",
                "/api/planned-units/{id}/update",
                Access.SESSION_CSRF,
                self._update,
            ),
            Route(
                "POST",
                "/api/planned-units/{id}/archive",
                Access.SESSION_CSRF,
                self._archive,
            ),
            Route(
                "POST",
                "/api/planned-units/{id}/focus",
                Access.SESSION_CSRF,
                self._focus,
            ),
            Route(
                "POST",
                "/api/planned-units/{id}/date",
                Access.SESSION_CSRF,
                self._date,
            ),
            Route(
                "POST",
                "/api/planned-units/{id}/garmin-exercise",
                Access.SESSION_CSRF,
                self._exercise,
            ),
            Route(
                "POST",
                "/api/planned-units/{id}/delete",
                Access.SESSION_CSRF,
                self._delete,
            ),
        ]

    def _update(self, handler: Any, request: RouteRequest) -> None:
        """Change a unit's content or day without a remote write.

        Request: path `{id}`; JSON body with the fields of the create route (a
        missing field keeps its stored value), `expected_payload_hash` (the unit's
        hash from the read the form came from) and `accept_plan_findings`.

        Response: 200 `{"status": "local", "local_id", "plan_check_notes",
        "change"}`. The unit's provider copies are replaced by the next sync.

        Errors: as the create route; 409 `planning_revision_conflict` for a
        missing hash; 409 `payload_hash_conflict` for an outdated hash or an
        unknown id.
        """
        payload = _read_object(handler)
        accept = payload.get("accept_plan_findings")
        fields = {
            key: value
            for key, value in payload.items()
            if key not in {"expected_payload_hash", "accept_plan_findings"}
        }
        edit = self._unit_edit
        assert edit is not None
        self._send_unit_change(
            handler,
            lambda: edit().update(
                request.params["id"],
                fields,
                payload.get("expected_payload_hash"),
                accept=accept if isinstance(accept, list) else None,
            ),
        )

    def _archive(self, handler: Any, request: RouteRequest) -> None:
        """Archive a planned unit out of the calendar; its receipt offers the undo.

        Request: path `{id}`; JSON body `{"confirmed": true, "expected_payload_hash":
        string}`. A copy at a provider goes with the next explicit sync.

        Response: 200 `{"status", "local_id", "plan_check_notes", "change"}`.

        Errors: 409 `confirmation_required` without `confirmed: true`; as the
        update route for the hash.
        """
        payload = _read_object(handler)
        if payload.get("confirmed") is not True:
            raise AppError(
                409,
                "Das Archivieren muss bestätigt werden.",
                reason="confirmation_required",
            )
        edit = self._unit_edit
        assert edit is not None
        self._send_unit_change(
            handler,
            lambda: edit().set_archived(
                request.params["id"], True, payload.get("expected_payload_hash")
            ),
        )

    @staticmethod
    def _send_unit_change(handler: Any, apply: Callable[[], dict[str, Any]]) -> None:
        try:
            with change_history.collect_changes() as changes:
                result = apply()
        except PlanCheckError as error:
            raise illness_block(error) from error
        units = [item for item in changes if item["entity_type"] == "planned_unit"]
        handler.send_json(200, {**result, **({"change": units[-1]} if units else {})})

    def _delete(self, handler: Any, request: RouteRequest) -> None:
        """Delete a planned unit from the calendar without a remote write (#3027).

        Request: path `{id}`, the local unit id; JSON body `{"confirmed": true,
        "expected_payload_hash": string}`. The hash is the unit's content hash
        from the read the athlete confirmed on. The delete is the Coach's local
        delete: the unit stays as a tracked tombstone, and copies at
        Intervals.icu or Garmin are removed only by the athlete's next explicit
        sync. It runs as one Coach change batch, so the plan bounds and the
        planning revision follow as for a Coach delete.

        Response: 200 `{"status": "deleted", "local_id", "change"}`; `change` is
        the undo reference.

        Errors: 409 `confirmation_required` "Das Löschen muss bestätigt werden."
        unless `confirmed` is `true`; 409 `planning_revision_conflict` "Die Einheit
        wurde inzwischen geändert." without `expected_payload_hash`; 409
        `payload_hash_conflict` "Eine Planänderung ist inzwischen veraltet." for an
        outdated hash or an id that names no unit (also a malformed one).
        """
        payload = _read_object(handler)
        if payload.get("confirmed") is not True:
            raise AppError(
                409,
                "Das Löschen muss bestätigt werden.",
                reason="confirmation_required",
            )
        expected_hash = payload.get("expected_payload_hash")
        if not isinstance(expected_hash, str) or not expected_hash.strip():
            raise AppError(
                409,
                "Die Einheit wurde inzwischen geändert.",
                reason="planning_revision_conflict",
            )
        assert self._training_changes is not None
        change = {
            "action": "delete",
            "local_id": request.params["id"],
            "expected_payload_hash": expected_hash,
        }
        with change_history.collect_changes() as changes:
            applied = self._training_changes().apply({"changes": [change]})
        result = applied["changes"][0]
        deleted = [item for item in changes if item["entity_type"] == "planned_unit"]
        handler.send_json(
            200,
            {
                "status": result.get("status"),
                "local_id": result.get("local_id"),
                **({"change": deleted[-1]} if deleted else {}),
            },
        )

    def _date(self, handler: Any, request: RouteRequest) -> None:
        """Move a planned unit to another day without a remote write.

        Request: path `{id}`, the local unit id; JSON body `{"date": "YYYY-MM-DD",
        "expected_payload_hash": string}`. The date is today or later in the
        athlete's time zone; the start time keeps its time of day. The hash is the
        unit's content hash from the read the move was made on.

        Response: 200 `{"status": "local", "local_id", "library_entry", "change"}`
        with the moved unit and the undo reference (`change` is missing when the
        date did not change; the unit is then left as it is). A unit already sent
        to a provider shows its pending sync; nothing is written remotely.

        Errors: 400 "Ungültige lokale Planungs-ID." for an id that is no UUID; 404
        "Einheit nicht gefunden." for an unknown, archived or deleted unit; 409
        `planning_revision_conflict` "Die Einheit wurde inzwischen geändert." for a
        missing or outdated `expected_payload_hash`; 400 "Das Planungsdatum muss das
        Format JJJJ-MM-TT haben."; 400 "Eine Einheit in der Vergangenheit wird nicht
        geplant." for a day before today; 409 `plan_date_conflict` when an external
        calendar marker rules out the day; 409 "Die lokale Einheit kann wegen einer
        bestehenden Kalendereinheit nicht verschoben werden." for an overlapping
        calendar entry.
        """
        payload = _read_object(handler)
        with (
            change_history.collect_changes() as changes,
            self._database_manager().unit_of_work(),
        ):
            result = self._planned_unit_service().move(
                request.params["id"],
                payload.get("date"),
                payload.get("expected_payload_hash"),
            )
        moved = [item for item in changes if item["entity_type"] == "planned_unit"]
        handler.send_json(200, {**result, **({"change": moved[-1]} if moved else {})})

    def _exercise(self, handler: Any, request: RouteRequest) -> None:
        """Assign the Garmin catalogue exercise of one strength exercise of a unit.

        Request: path `{id}`, the local unit id; JSON body `{"exercise_index":
        integer, "garmin_exercise": string, "expected_payload_hash": string}`. The
        index counts into the unit's strength structure from 0; the hash is the
        unit's content hash from the read the pick was made on. A name is trimmed,
        cut to 80 characters and checked against the Garmin catalogue; the pair is
        derived from it. An empty name removes the pick.

        Response: 200 `{"status": "local", "local_id", "library_entry"}` with the
        updated unit. The unit's Intervals.icu sync state is kept and nothing is
        written remotely.

        Errors: 404 "Einheit nicht gefunden." for an unknown or deleted unit; 409
        `planning_revision_conflict` "Die Einheit wurde inzwischen geändert." for a
        missing or outdated `expected_payload_hash`; 400 `invalid_exercise_index`
        "Unbekannte Übung." when the unit has no strength structure or the index is
        not an integer within it; 400 `invalid_strength_structure` "Unbekannte
        Garmin-Übung: ..." with suggestions for a name the catalogue does not know.
        """
        payload = _read_object(handler)
        with self._database_manager().unit_of_work():
            result = self._planned_unit_service().assign_garmin_exercise(
                request.params["id"],
                payload.get("exercise_index"),
                payload.get("garmin_exercise"),
                payload.get("expected_payload_hash"),
            )
        handler.send_json(200, result)

    def _focus(self, handler: Any, request: RouteRequest) -> None:
        """Set or clear the focus of a planned unit without a remote write.

        Request: path `{id}`, the local unit UUID; JSON body `{"session_focus":
        string|null, "strength_intensity": string|null}`. Both fields are always
        applied: an omitted or `null` field is cleared. `session_focus` must be a
        focus of the unit's sport; `strength_intensity` is `light`, `moderate` or
        `heavy` and only for strength units.

        Response: 200 `{"status": "local", "local_id", "library_entry"}` with the
        updated unit. A focus-only change keeps the Intervals.icu sync state.

        Errors: 400 "Ungültige lokale Planungs-ID." for an id that is no UUID; 404
        "Lokale Planung nicht gefunden."; 403 "Nur lokale geplante Einheiten können
        bearbeitet werden." for a record without a date; 400 `invalid_session_focus`
        "Ungültige Art der Einheit für diese Sportart." or "Ungültige
        Krafteinheit-Intensität."; 409 `plan_date_conflict` when an external
        calendar marker rules out the unit's day.
        """
        payload = _read_object(handler)
        values = {key: payload.get(key) for key in FOCUS_ONLY_KEYS}
        with self._database_manager().unit_of_work():
            result = self._planned_unit_service().update(request.params["id"], values)
        handler.send_json(200, result)


def _read_object(handler: Any) -> dict[str, Any]:
    payload = handler.read_json()
    if not isinstance(payload, dict):
        raise AppError(400, "Die Anfrage muss ein Objekt sein.")
    return payload

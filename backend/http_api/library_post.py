"""Authenticated POST routes that schedule or archive one workout-library entry (#3028)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from backend import change_history
from backend.errors import AppError
from backend.http_api.route_table import Access, Route, RouteRequest

# Both bodies hold one short field; anything larger is not a request of this kind.
_MAX_BODY_BYTES = 1024


class LibraryPostRoutes:
    """Schedule or archive a library entry through the use cases the Coach tools use."""

    def __init__(
        self,
        workout_library: Callable[[], Any],
        library_plan_service: Callable[[], Any],
    ):
        self._workout_library = workout_library
        self._library_plan_service = library_plan_service

    def routes(self) -> list[Route]:
        return [
            Route(
                "POST",
                "/api/library/{id}/schedule",
                Access.SESSION_CSRF,
                self._schedule,
            ),
            Route(
                "POST",
                "/api/library/{id}/archive",
                Access.SESSION_CSRF,
                self._archive,
            ),
        ]

    def _schedule(self, handler: Any, request: RouteRequest) -> None:
        """Plan a library entry on one day as a local unit, without a remote write.

        Request: path `{id}`, the library entry's local id; JSON body
        `{"date": "YYYY-MM-DD"}` and no other field. It runs the batch behind the
        Coach's `apply_workout_library_plan`; the next explicit sync exports the
        new unit.

        Response: 200 `{"status", "planned", "change"}`: `status` is `local`, or
        `already_planned` when the entry already sits on that day (then without
        `change`); `planned` is the planned unit; `change` is the undo reference.

        Errors: 400 "Die Anfrage muss ein Objekt sein." or "Unbekannte Felder: …"
        for another body; 400 for an id that is no UUID or a date that is no
        JJJJ-MM-TT date; 404 "Bibliothekseinheit nicht gefunden."; 409 "Planung
        wegen bestehender Kalendereinheiten nicht möglich: …" for a day the
        calendar rules out.
        """
        payload = _read_fields(handler, {"date"})
        with change_history.collect_changes() as changes:
            result = self._library_plan_service().apply(
                [
                    {
                        "library_workout_id": request.params["id"],
                        "date": payload.get("date"),
                    }
                ]
            )
        planned = result["planned"][0]
        created = [item for item in changes if item["entity_type"] == "planned_unit"]
        handler.send_json(
            200,
            {
                "status": planned["status"],
                "planned": planned["library_entry"],
                **({"change": created[-1]} if created else {}),
            },
        )

    def _archive(self, handler: Any, request: RouteRequest) -> None:
        """Take a library entry out of the library after the athlete confirmed it.

        Request: path `{id}`, the library entry's local id; JSON body
        `{"confirmed": true}` and no other field. It runs the template `archive`
        behind the Coach's `manage_training_templates`: the entry stays stored,
        hidden from the library, and units planned from it stay unchanged.

        Response: 200 `{"status": "local", "local_id", "change"}`; `change` is the
        undo reference.

        Errors: 409 `confirmation_required` "Das Löschen muss bestätigt werden."
        without `confirmed: true`; 400 "Die Anfrage muss ein Objekt sein." or
        "Unbekannte Felder: …" for another body; 400 for an id that is no UUID;
        404 "Bibliothekseinheit nicht gefunden."; 409 "Geplante lokale Einheiten
        werden im Kalender bearbeitet." for a planned unit.
        """
        payload = _read_fields(handler, {"confirmed"})
        if payload.get("confirmed") is not True:
            raise AppError(
                409,
                "Das Löschen muss bestätigt werden.",
                reason="confirmation_required",
            )
        with change_history.collect_changes() as changes:
            result = self._workout_library().update(
                request.params["id"], {"action": "archive"}
            )
        archived = [
            item for item in changes if item["entity_type"] == "workout_library"
        ]
        handler.send_json(
            200,
            {
                "status": result.get("status"),
                "local_id": result.get("local_id"),
                **({"change": archived[-1]} if archived else {}),
            },
        )


def _read_fields(handler: Any, allowed: set[str]) -> dict[str, Any]:
    payload = handler.read_json(_MAX_BODY_BYTES)
    if not isinstance(payload, dict):
        raise AppError(400, "Die Anfrage muss ein Objekt sein.")
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise AppError(400, f"Unbekannte Felder: {', '.join(unknown)}.")
    return payload

"""Authenticated POST routes that create, change, archive and delete a training plan (#3950)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from backend import change_history
from backend.errors import AppError
from backend.http_api.route_table import Access, Route, RouteRequest

_FIELDS = {"name", "goal", "start_date", "end_date", "status"}
_MAX_BODY_BYTES = 8 * 1024


class TrainingPlanPostRoutes:
    """The Coach's `update_training_plan` use case for the athlete: plan metadata only."""

    def __init__(self, training_plan_service: Callable[[], Any]):
        self._training_plan_service = training_plan_service

    def routes(self) -> list[Route]:
        return [
            Route("POST", "/api/training-plans", Access.SESSION_CSRF, self._create),
            Route(
                "POST",
                "/api/training-plans/{id}/update",
                Access.SESSION_CSRF,
                self._update,
            ),
            Route(
                "POST",
                "/api/training-plans/{id}/delete",
                Access.SESSION_CSRF,
                self._delete,
            ),
        ]

    def _create(self, handler: Any, _request: RouteRequest) -> None:
        """Save a new local plan without units.

        Request: JSON body `{"name", "goal", "start_date", "end_date", "status"}`
        (dates `YYYY-MM-DD`, status one of `planned`, `active`, `paused`, `draft`,
        `completed`, `archived`, `cancelled`; default `planned`) and no other field.
        A unit joins the plan when it is planned with the plan's id.

        Response: 200 `{"status": "created", "plan_id", "plan", "change"}`;
        `change` is the undo reference.

        Errors: 400 for an unknown field, a missing name, a date that is no
        JJJJ-MM-TT date or a start after the end, or an invalid status.
        """
        payload = _read_fields(handler, _FIELDS)
        self._send(handler, lambda: self._training_plan_service().create(payload))

    def _update(self, handler: Any, request: RouteRequest) -> None:
        """Change a plan's name, goal, dates or status; its units stay as they are.

        Request: path `{id}`; the fields of the create route (a missing field keeps
        its stored value) and, for `status: "archived"`, `"confirmed": true`.

        Response: 200 `{"status": "updated", "plan_id", "plan", "change"}`.

        Errors: as the create route; 409 `confirmation_required` "Das Archivieren
        muss bestätigt werden." for an archive without `confirmed`; 400 for an id
        that is no UUID; 404 "Trainingsplan nicht gefunden.".
        """
        payload = _read_fields(handler, _FIELDS | {"confirmed"})
        # The service reads the status stripped, case-folded and by its German alias.
        archives = str(payload.get("status") or "").strip().casefold() in {
            "archived",
            "archiviert",
        }
        if archives and payload.get("confirmed") is not True:
            raise AppError(
                409,
                "Das Archivieren muss bestätigt werden.",
                reason="confirmation_required",
            )
        values = {key: value for key, value in payload.items() if key != "confirmed"}
        self._send(
            handler,
            lambda: self._training_plan_service().update(
                request.params["id"], {**values, "action": "update"}
            ),
        )

    def _delete(self, handler: Any, request: RouteRequest) -> None:
        """Delete a plan's metadata after the athlete confirmed it; its units stay.

        Request: path `{id}`; JSON body `{"confirmed": true}` and no other field.

        Response: 200 `{"status": "deleted", "plan_id", "change"}`.

        Errors: 409 `confirmation_required` "Das Löschen muss bestätigt werden."
        without `confirmed: true`; 400 for an id that is no UUID; 404
        "Trainingsplan nicht gefunden.".
        """
        payload = _read_fields(handler, {"confirmed"})
        if payload.get("confirmed") is not True:
            raise AppError(
                409,
                "Das Löschen muss bestätigt werden.",
                reason="confirmation_required",
            )
        self._send(
            handler,
            lambda: self._training_plan_service().update(
                request.params["id"], {"action": "delete"}
            ),
        )

    @staticmethod
    def _send(handler: Any, apply: Callable[[], dict[str, Any]]) -> None:
        with change_history.collect_changes() as changes:
            result = apply()
        plan = [item for item in changes if item["entity_type"] == "training_plan"]
        body = {key: value for key, value in result.items() if key != "plan" or value}
        handler.send_json(200, {**body, **({"change": plan[-1]} if plan else {})})


def _read_fields(handler: Any, allowed: set[str]) -> dict[str, Any]:
    payload = handler.read_json(_MAX_BODY_BYTES)
    if not isinstance(payload, dict):
        raise AppError(400, "Die Anfrage muss ein Objekt sein.")
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise AppError(400, f"Unbekannte Felder: {', '.join(unknown)}.")
    return payload

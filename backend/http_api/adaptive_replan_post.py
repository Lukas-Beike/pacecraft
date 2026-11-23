"""Authenticated POST route that applies the shown adaptive replan preview (#4020)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from backend import change_history
from backend.errors import AppError
from backend.http_api.route_table import Access, Route, RouteRequest
from backend.planning.plan_guard_service import PlanCheckError


class AdaptiveReplanPostRoutes:
    """The Coach's `apply_adaptive_replan` for the athlete; the preview comes from the stored one."""

    def __init__(self, replan_service: Callable[[], Any]):
        self._replan_service = replan_service

    def routes(self) -> list[Route]:
        return [
            Route(
                "POST",
                "/api/planning/replan/apply",
                Access.SESSION_CSRF,
                self._apply,
            ),
        ]

    def _apply(self, handler: Any, _request: RouteRequest) -> None:
        """Apply the shown preview to future local units; nothing is written remotely.

        Request: JSON body `{"adjustment_id": string, "confirmed": true,
        "accept_plan_findings": [string]}` (the codes of blocking plan checks the
        athlete accepts, `illness_hard`). During an illness pause applying also
        records today's check-in as ill; the Intervals.icu entry of the pause goes
        only with the athlete's named sync.

        Response: 200 `{"status", "id", "updated", "updated_checkins", "stale",
        "illness_pause", "planning", "plan_check_notes", "changes"}`; `changes` is
        one undo batch for `POST /api/change-history/undo-batch`.

        Errors: 409 `confirmation_required` without `confirmed: true`; 409
        `preview_outdated` when a newer preview replaced it; 409 `illness_hard` for
        a hard or long unit within days of a reported illness; 400 for an id that
        is no UUID; 404 "Plananpassung nicht gefunden.".
        """
        payload = _read_fields(
            handler, {"adjustment_id", "confirmed", "accept_plan_findings"}
        )
        if payload.get("confirmed") is not True:
            raise AppError(
                409,
                "Das Übernehmen muss bestätigt werden.",
                reason="confirmation_required",
            )
        accept = payload.get("accept_plan_findings")
        try:
            with change_history.collect_changes() as changes:
                result = self._replan_service().apply(
                    payload.get("adjustment_id"),
                    accept if isinstance(accept, list) else None,
                )
        except PlanCheckError as error:
            if {item["code"] for item in error.findings} != {"illness_hard"}:
                raise
            raise AppError(
                409,
                "Krankheit gemeldet: In den nächsten Tagen keine harte oder lange Einheit.",
                reason="illness_hard",
            ) from error
        handler.send_json(
            200,
            {
                **result,
                "changes": [
                    {"id": item["id"], "after_hash": item["after_hash"]}
                    for item in changes
                ],
            },
        )


def _read_fields(handler: Any, allowed: set[str]) -> dict[str, Any]:
    payload = handler.read_json(4096)
    if not isinstance(payload, dict):
        raise AppError(400, "Die Anfrage muss ein Objekt sein.")
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise AppError(400, f"Unbekannte Felder: {', '.join(unknown)}.")
    return payload

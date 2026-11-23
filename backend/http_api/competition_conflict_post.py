"""Authenticated POST route that settles a competition's Intervals.icu sync conflict (#4021)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from backend.errors import AppError
from backend.http_api.route_table import Access, Route, RouteRequest

_STRATEGIES = {"keep_local", "adopt_remote"}


class CompetitionConflictPostRoutes:
    """The Coach's `resolve_training_sync_conflict` for a competition, for the athlete."""

    def __init__(self, conflicts: Callable[[], Any]):
        self._conflicts = conflicts

    def routes(self) -> list[Route]:
        return [
            Route(
                "POST",
                "/api/competitions/{id}/resolve",
                Access.SESSION_CSRF,
                self._resolve,
            )
        ]

    def _resolve(self, handler: Any, request: RouteRequest) -> None:
        """Keep the local competition or take the Intervals.icu one; nothing is sent.

        Request: path `{id}`, the competition's local id; JSON body `{"strategy":
        "keep_local" | "adopt_remote", "confirmed": true}` and no other field.
        `adopt_remote` replaces the local values with the remote ones for good, so
        it needs `confirmed: true`; `keep_local` marks the local values to win at
        the next explicit send.

        Response: 200 `{"ok": true, "status": "resolved", "strategy",
        "competition", "competitions"}`.

        Errors: 400 "Ungültige Konfliktstrategie." or "Unbekannte Felder: …"; 409
        `confirmation_required` "Das Übernehmen muss bestätigt werden." for
        `adopt_remote` without `confirmed`; 404 "Wettkampf nicht gefunden."; 409
        "Für diesen Wettkampf liegt kein offener Synchronisierungskonflikt vor."; 409
        "Das Remote-Event kann nicht übernommen werden." when the event is gone.
        """
        payload = handler.read_json(1024)
        if not isinstance(payload, dict):
            raise AppError(400, "Die Anfrage muss ein Objekt sein.")
        unknown = sorted(set(payload) - {"strategy", "confirmed"})
        if unknown:
            raise AppError(400, f"Unbekannte Felder: {', '.join(unknown)}.")
        strategy = payload.get("strategy")
        if strategy not in _STRATEGIES:
            raise AppError(400, "Ungültige Konfliktstrategie.")
        if strategy == "adopt_remote" and payload.get("confirmed") is not True:
            raise AppError(
                409,
                "Das Übernehmen muss bestätigt werden.",
                reason="confirmation_required",
            )
        handler.send_json(
            200, self._conflicts().resolve_local(request.params["id"], strategy)
        )

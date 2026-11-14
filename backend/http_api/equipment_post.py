"""Authenticated local equipment POST routes."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from backend.athlete.equipment import EquipmentService
from backend.errors import AppError
from backend.http_api.route_table import Access, Route, RouteRequest

# An item, an assignment or a maintenance entry is a few hundred bytes.
EQUIPMENT_BODY_MAX_BYTES = 16_384


class EquipmentPostRoutes:
    """Save, assign and maintain equipment through the equipment domain service."""

    def __init__(self, equipment_service: Callable[[], EquipmentService]) -> None:
        self._equipment_service = equipment_service

    def routes(self) -> list[Route]:
        return [
            Route("POST", "/api/equipment", Access.SESSION_CSRF, self._save),
            Route(
                "POST",
                "/api/equipment/assignment",
                Access.SESSION_CSRF,
                self._assign,
            ),
            Route(
                "POST",
                "/api/equipment/maintenance",
                Access.SESSION_CSRF,
                self._maintain,
            ),
        ]

    def _save(self, handler: Any, _request: RouteRequest) -> None:
        """Create, edit or retire one piece of equipment, stored locally (#2879).

        Request: JSON body of at most 16 KiB with the item fields `id` (omit to
        create), `expected_revision` (required with `id`), `name`, `sport`,
        `environment`, `kind` (`shoes`, `bike`, `component`, `other`), `status`
        (`active` or `archived`), `parent_id`, `start_date` (YYYY-MM-DD),
        `initial_distance_km`, `initial_hours`, `maintenance_km`,
        `maintenance_hours`, `lifetime_target_km`, plus `confirmed`; no other
        key. Retiring (`status` `archived`) needs `"confirmed": true`.

        Response: 200 `{"ok": true, "stored_locally": true, "equipment": {...},
        "change"}`; `change` is the undo reference.

        Errors: 400 "Ungültige Ausrüstung." for an unknown key or a body that is
        not an object; 400 "Name, Sport, Art oder Status der Ausrüstung
        ungültig."; 400 "Startdatum oder Ausrüstungs-ID ungültig."; 400
        "Ausrüstungsbeginn liegt außerhalb des gültigen Zeitraums."; 400
        "Ausrüstungszähler oder Wartungsintervall ungültig."; 404 "Ausrüstung
        nicht gefunden." for an unknown `id`; 409 "Ausrüstung geändert; neu
        lesen." for a stale `expected_revision`; 409 "Das Ausmustern muss
        bestätigt werden." for an unconfirmed retirement; 409 "Höchstens 100
        Ausrüstungsgegenstände." for a new item beyond the 100th.
        """
        payload = handler.read_json(EQUIPMENT_BODY_MAX_BYTES)
        if not isinstance(payload, dict):
            raise AppError(400, "Ungültige Ausrüstung.")
        confirmed = payload.pop("confirmed", None)
        if payload.get("status") == "archived" and confirmed is not True:
            raise AppError(409, "Das Ausmustern muss bestätigt werden.")
        handler.send_json(200, self._equipment_service().save(payload))

    def _assign(self, handler: Any, _request: RouteRequest) -> None:
        """Assign one session its equipment, or none, stored locally (#2879).

        Request: JSON body of at most 16 KiB, exactly `{"activity_id": string,
        "equipment_id": string | null}`; `null` records that the session used
        no equipment ("Keiner"), which also overrides Garmin's gear links.

        Response: 200 `{"ok": true, "stored_locally": true, "assignment":
        {"activity_id", "equipment_id", "observed_at"}, "change"}`; `change` is
        the undo reference.

        Errors: 400 "Aktivität und Ausrüstung sind erforderlich." for another
        body shape; 404 "Kanonische Aktivität nicht gefunden." for an unknown
        session; 400 "Aktive passende Hauptausrüstung erforderlich." for an
        unknown item or one that is archived, a component, unresolved or of
        another sport.
        """
        handler.send_json(
            200,
            self._equipment_service().assign(
                handler.read_json(EQUIPMENT_BODY_MAX_BYTES)
            ),
        )

    def _maintain(self, handler: Any, _request: RouteRequest) -> None:
        """Record one maintenance entry for a piece of equipment, stored locally.

        Request: JSON body `{"equipment_id": string, "date": "YYYY-MM-DD", "notes":
        string}` and no other key; `notes` is optional and cut to 1000 characters.
        The date must not be before the item's `start_date` or in the future.

        Response: 200 `{"ok": true, "stored_locally": true, "maintenance": {"id",
        "equipment_id", "date", "notes", "distance_km", "observed_at"}, "change"}`;
        `change` is the undo reference. `distance_km` is the item's usage so far,
        `null` for a past date of a Garmin-linked item.

        Errors: 400 "Ungültige Wartung." for an unknown body key; 400
        "Wartungsdatum ungültig." for a `date` that is not an ISO date; 400
        "Wartungsgegenstand oder Datum ungültig." for an empty or unmatched
        `equipment_id` or a date outside the item's range; 404 "Ausrüstung nicht
        gefunden." for a non-empty unknown `equipment_id`.
        """
        handler.send_json(200, self._equipment_service().maintain(handler.read_json()))

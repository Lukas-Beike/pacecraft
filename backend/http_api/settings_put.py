"""Authenticated local settings PUT routes."""

from __future__ import annotations

from typing import Any

from backend.http_api.route_table import Access, Route, RouteRequest
from backend.settings import SettingsService


class SettingsPutRoutes:
    """Dispatch local settings writes through the settings state owner."""

    def __init__(self, settings: SettingsService) -> None:
        self._settings = settings

    def routes(self) -> list[Route]:
        return [
            Route("PUT", "/api/settings/model", Access.SESSION_CSRF, self._model),
            Route(
                "PUT",
                "/api/settings/service-tier",
                Access.SESSION_CSRF,
                self._service_tier,
            ),
            Route(
                "PUT",
                "/api/settings/thinking-level",
                Access.SESSION_CSRF,
                self._thinking_level,
            ),
            Route(
                "PUT",
                "/api/settings/automatic-turns",
                Access.SESSION_CSRF,
                self._automatic_turns,
            ),
        ]

    def _automatic_turns(self, handler: Any, _request: RouteRequest) -> None:
        """Switch the automatic Coach turns on or off.

        Request: JSON body `{"morning": bool}`: the morning check-in that runs
        once the night's sleep is stored. The turn only reads and proposes.

        Response: 200 `{"morning": bool}`, every switch.

        Errors: 400 "Unbekannte automatische Coach-Einstellung.".
        """
        handler.send_json(200, self._settings.save_automatic_turns(handler.read_json()))

    def _model(self, handler: Any, _request: RouteRequest) -> None:
        """Select the Coach model of one task.

        Request: JSON body `{"model": string, "task": string}`. `task` is
        `conversation` (default), `planning` (the quick actions) or `nutrition`
        (label photo and product web search). `model` is `claude-opus`,
        `claude-sonnet`, `claude-haiku`, the model configured in the environment,
        or, with the default OpenAI base URL, `gpt-6-luna`, `gpt-6-sol`,
        `gpt-6.1-sol` or `gpt-6-astra`.

        Response: 200 `{"model": string}`.

        Errors: 400 "Nicht unterstützte Modellauswahl."; 400 "Für dieses Modell
        fehlt der API-Schlüssel unter Mehr › Anbindungen." when the model's provider
        has no key; 400 "Unbekannte Coach-Aufgabe.".
        """
        body = handler.read_json()
        handler.send_json(
            200,
            self._settings.save_model(
                body.get("model"), body.get("task", "conversation")
            ),
        )

    def _service_tier(self, handler: Any, _request: RouteRequest) -> None:
        """Select the OpenAI service tier.

        Request: JSON body `{"service_tier": string}`, case-insensitive: `default`,
        `fast` or `ultrafast`, as far as the selected model supports it. `fast` needs
        `gpt-6-luna`, `gpt-6-sol`, `gpt-6.1-sol` or `gpt-6-astra`, `ultrafast`
        `gpt-6.1-sol` or `gpt-6-astra`; with a custom OpenAI base URL or any other
        model only `default`.

        Response: 200 `{"service_tier": string}`.

        Errors: 400 "Nicht unterstützter Geschwindigkeitsmodus für dieses Modell.".
        """
        handler.send_json(
            200,
            self._settings.save_service_tier(handler.read_json().get("service_tier")),
        )

    def _thinking_level(self, handler: Any, _request: RouteRequest) -> None:
        """Select the thinking level.

        Request: JSON body `{"thinking_level": string, "task": string}`; the level is
        case-insensitive: `low`, `medium` or `high`. `task` is
        optional, as for the model.

        Response: 200 `{"thinking_level": string}`.

        Errors: 400 "Nicht unterstütztes Thinking Level."; 400 "Unbekannte
        Coach-Aufgabe.".
        """
        body = handler.read_json()
        handler.send_json(
            200,
            self._settings.save_thinking_level(
                body.get("thinking_level"), body.get("task", "conversation")
            ),
        )

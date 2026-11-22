"""Authenticated Web Push routes: key, subscriptions and per-type switches (#3914)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from backend.http_api.route_table import Access, Route, RouteRequest
from backend.push.service import PushService

# A subscription is an endpoint URL and two short keys.
PUSH_BODY_MAX_BYTES = 8_192


class PushRoutes:
    """Dispatch the push settings of the athlete's devices to the push service."""

    def __init__(self, push_service: Callable[[], PushService]) -> None:
        self._push = push_service

    def routes(self) -> list[Route]:
        return [
            Route("GET", "/api/push", Access.SESSION, self._state),
            Route("POST", "/api/push/key", Access.SESSION_CSRF, self._key),
            Route("POST", "/api/push/subscribe", Access.SESSION_CSRF, self._subscribe),
            Route(
                "POST", "/api/push/unsubscribe", Access.SESSION_CSRF, self._unsubscribe
            ),
            Route(
                "POST", "/api/push/preferences", Access.SESSION_CSRF, self._preferences
            ),
        ]

    def _state(self, handler: Any, request: RouteRequest) -> None:
        """Read the push switches and whether this device is subscribed.

        Request: optional query `endpoint`, the device's push endpoint.

        Response: 200 `{"subscribed": bool, "types": {type: bool}}`; `subscribed`
        is for `endpoint`, else whether any device is subscribed; `types` holds
        every notification type, true unless switched off.

        Errors: none beyond the common ones.
        """
        values = request.query.get("endpoint") or []
        endpoint = values[0] if values else None
        handler.send_json(200, self._push().state(endpoint))

    def _key(self, handler: Any, _request: RouteRequest) -> None:
        """Return the public VAPID key a browser subscribes with.

        Request: no body. The key pair is generated once and kept in the
        encrypted database; the private key never leaves it.

        Response: 200 `{"public_key": string}`, the base64url public point.

        Errors: none beyond the common ones.
        """
        handler.send_json(200, self._push().prepare())

    def _subscribe(self, handler: Any, _request: RouteRequest) -> None:
        """Store this device's push subscription.

        Request: JSON body of at most 8 KiB, `PushSubscription.toJSON()`:
        `{"endpoint": string, "keys": {"p256dh": string, "auth": string}}`. Only
        the push services of Chrome and Firefox are accepted; a repeated endpoint
        replaces its earlier keys; at most 10 devices are kept.

        Response: 200 as `GET /api/push` for this endpoint.

        Errors: 400 "Dieser Browser wird für Push-Benachrichtigungen nicht
        unterstützt." for another push service or a missing endpoint; 400 "Das
        Push-Abonnement ist ungültig." for malformed keys.
        """
        handler.send_json(
            200, self._push().subscribe(handler.read_json(PUSH_BODY_MAX_BYTES))
        )

    def _unsubscribe(self, handler: Any, _request: RouteRequest) -> None:
        """Remove this device's push subscription.

        Request: JSON body `{"endpoint": string}`; without `endpoint` every
        device is removed.

        Response: 200 as `GET /api/push` for this endpoint.

        Errors: none beyond the common ones.
        """
        handler.send_json(
            200, self._push().unsubscribe(handler.read_json(PUSH_BODY_MAX_BYTES))
        )

    def _preferences(self, handler: Any, _request: RouteRequest) -> None:
        """Switch notification types on or off.

        Request: JSON body `{"types": {type: bool}, "endpoint"?: string}` with at
        least one known type; unnamed types keep their switch.

        Response: 200 as `GET /api/push` for `endpoint`.

        Errors: 400 "Die Benachrichtigungsarten sind ungültig." for an unknown
        type, a non-boolean value or no type.
        """
        handler.send_json(
            200, self._push().set_preferences(handler.read_json(PUSH_BODY_MAX_BYTES))
        )

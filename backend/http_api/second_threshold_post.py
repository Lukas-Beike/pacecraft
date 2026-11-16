"""Authenticated local route that adopts one value of a local LT2 estimate (#3273)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from backend.http_api.route_table import Access, Route, RouteRequest
from backend.performance.second_threshold_service import SecondThresholdService

ADOPT_PATH = "/api/zones/lt2/adopt"
NOTICE_BASELINE_PATH = "/api/zones/lt2/notice-baseline"


class SecondThresholdPostRoutes:
    """Store one shown LT2 estimate value as the threshold on the athlete's request."""

    def __init__(self, service: Callable[[], SecondThresholdService]) -> None:
        self._service = service

    def routes(self) -> list[Route]:
        return [
            Route("POST", ADOPT_PATH, Access.SESSION_CSRF, self._adopt),
            Route(
                "PUT", NOTICE_BASELINE_PATH, Access.SESSION_CSRF, self._notice_baseline
            ),
        ]

    def _adopt(self, handler: Any, _request: RouteRequest) -> None:
        """Adopt one value of a local LT2 estimate as the threshold in effect.

        Request: JSON body `{"sport": "run" | "ride", "source": "dfa_alpha1" |
        "critical_speed" | "critical_power", "value": "heart_rate" | "pace" |
        "power", "shown": number, "confirm_deviation": boolean, "replace_local":
        boolean}`. `shown` is the number `GET /api/profile` showed in
        `lt2_estimates` (bpm, s/km or W).

        Response: 200 `{"configuration": object, "change": object | null}`, the
        receipt of the local zone configuration saved from today with the zone
        limits in effect and its `threshold_origin`; `change` is the undo reference.

        Errors: 400 for an unknown sport, source or value; 409 with reason
        `lt2_unknown` (no estimate), `lt2_changed` (`shown` differs from the
        current estimate), `lt2_no_zones` (no zones of that kind), `lt2_replace_local`
        (a value the athlete entered is in effect; send `replace_local: true`),
        `lt2_deviation` (more than 5 % from the value in effect; send
        `confirm_deviation: true`) or `lt2_zone_limits` (the absolute zone limits
        do not fit the new threshold).
        """
        result = self._service().adopt(handler.read_json())
        handler.send_json(200, result)

    def _notice_baseline(self, handler: Any, _request: RouteRequest) -> None:
        """Store the LT2 values of one sport the athlete has now seen (F10, #3273).

        Request: JSON body `{"sport": "run" | "ride", "values": object}`. `values`
        holds per known key (run: `provider_hr`, `provider_pace`, `dfa_hr`,
        `dfa_pace`, `cs_pace`; ride: `provider_power`, `provider_hr`, `cp_power`) a positive
        number or null for "unbekannt"; other keys are ignored.

        Response: 200 `{"lt2_notice_baselines": object}`, every sport's stored values.

        Errors: 400 "Unbekannte Sportart oder Werte."; 400 "Werte müssen Zahlen
        oder null sein.".
        """
        result = self._service().save_notice_baseline(handler.read_json())
        handler.send_json(200, {"lt2_notice_baselines": result})

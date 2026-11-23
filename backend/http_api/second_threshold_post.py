"""Authenticated local route that adopts one value of a local LT2 estimate (#3273)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from backend.http_api.route_table import Access, Route, RouteRequest
from backend.performance.second_threshold_service import SecondThresholdService

ADOPT_PATH = "/api/zones/lt2/adopt"
NOTICE_BASELINE_PATH = "/api/zones/lt2/notice-baseline"
DISMISS_OFFER_PATH = "/api/zones/threshold-offers/dismiss"


class SecondThresholdPostRoutes:
    """Store one shown LT2 estimate value as the threshold on the athlete's request."""

    def __init__(
        self,
        service: Callable[[], SecondThresholdService],
        step_test: Callable[[str], Any] | None = None,
    ) -> None:
        self._service = service
        self._step_test = step_test

    def routes(self) -> list[Route]:
        return [
            Route("POST", ADOPT_PATH, Access.SESSION_CSRF, self._adopt),
            Route(
                "PUT", NOTICE_BASELINE_PATH, Access.SESSION_CSRF, self._notice_baseline
            ),
            Route("POST", DISMISS_OFFER_PATH, Access.SESSION_CSRF, self._dismiss_offer),
        ]

    def _adopt(self, handler: Any, _request: RouteRequest) -> None:
        """Adopt one value of a local LT2 estimate as the threshold in effect.

        Request: JSON body `{"sport": "run" | "ride", "source": "dfa_alpha1" |
        "critical_speed" | "critical_power" | "step_test" | "garmin", "value": "heart_rate" |
        "pace" | "power", "shown": number, "confirm_deviation": boolean,
        "replace_local": boolean, "activity_id": string}`. `shown` is the number
        `GET /api/profile` showed in `lt2_estimates` (bpm, s/km or W); for
        `step_test` (runs, #3833) it is `step_test.lt2` of `GET
        /api/activities/{activity_id}`, adopted only when good, with the origin
        `dfa_alpha1`. `garmin` (#4135) adopts Garmin's LT2 value as its entry in
        `threshold_offers` shows it (run `heart_rate` or `pace`, ride `power` or
        `heart_rate`), stored with the origin `garmin`.

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
        result = self._service().adopt(handler.read_json(), self._step_test)
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

    def _dismiss_offer(self, handler: Any, _request: RouteRequest) -> None:
        """Dismiss one open LT1 or LT2 offer for its exact value (#4135).

        Request: JSON body `{"key": string}`, the `key` of an entry of
        `threshold_offers` in `GET /api/profile`. The offer stays dismissed for
        that value; a different value of the same source is a new offer.

        Response: 200 `{"threshold_offers": array}`, the offers still open.

        Errors: 409 with reason `offer_unknown` when the key names no open offer.
        """
        result = self._service().dismiss_offer(handler.read_json())
        handler.send_json(200, {"threshold_offers": result})

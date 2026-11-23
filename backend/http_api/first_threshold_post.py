"""Authenticated local route that adopts the first threshold estimate."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from backend.http_api.route_table import Access, Route, RouteRequest
from backend.performance.first_threshold_service import FirstThresholdService

ADOPT_PATH = "/api/profile/first-threshold/adopt"


class FirstThresholdPostRoutes:
    """Store the shown estimate as the first threshold on the athlete's request."""

    def __init__(
        self,
        first_threshold_service: Callable[[], FirstThresholdService],
        step_test: Callable[[str], Any] | None = None,
    ) -> None:
        self._step_test = step_test
        self._first_threshold_service = first_threshold_service

    def routes(self) -> list[Route]:
        return [Route("POST", ADOPT_PATH, Access.SESSION_CSRF, self._adopt)]

    def _adopt(self, handler: Any, _request: RouteRequest) -> None:
        """Adopt the shown first threshold estimate into the profile.

        Request: JSON body `{"percent_lthr": number, "replace_measured": boolean}`.
        `percent_lthr` is required and must equal the current estimate from
        `GET /api/profile`; `replace_measured: true` is needed to replace a value
        stored with origin `measured`. Or `{"step_test": string, "heart_rate_bpm":
        number, "replace_measured": boolean}` adopts the good LT1 of the run step
        test with that session id (#3833) in bpm (#3915);
        `heart_rate_bpm` must equal `step_test.lt1.heart_rate_bpm` of
        `GET /api/activities/{id}`.

        Response: 200 `{"profile": object, "first_threshold_estimate": object,
        "adopted": boolean}`. The estimate is stored in bpm of today's run
        threshold HR with origin `estimated`; `adopted` is `false` without a profile
        write when that value is already stored.

        Errors: 409 "Es liegt keine Schätzung der ersten Schwelle vor."; 409 "Die
        Schätzung hat sich geändert. Bitte die Anzeige aktualisieren." when
        `percent_lthr` differs from the current estimate; 409 "Ein gemessener Wert
        ist gespeichert. Zum Ersetzen bitte bestätigen." without
        `replace_measured`; 409 "Dieser Stufentest liefert keinen guten Wert der
        ersten Schwelle."; 409 "Der Wert der ersten Schwelle ist nicht
        plausibel."; 409 "Ohne LSHF lässt sich der Wert nicht
        speichern."; 404 for an unknown session.
        """
        payload = handler.read_json()
        # The step test's detail read happens before the writer lease (#3833).
        key = payload.get("step_test") if isinstance(payload, dict) else None
        tested = (
            None
            if key is None or self._step_test is None
            else self._step_test(str(key))
        )
        result = self._first_threshold_service().adopt(payload, tested)
        handler.send_json(200, result)

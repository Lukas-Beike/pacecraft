"""The athlete's first threshold estimate (DFA-a1) and its explicit adoption.

The estimate is derived from the stored per-session results of chest-strap RR
intervals; it is never written to the profile on its own. The athlete adopts it
with an explicit action, which stores it as percent of the run threshold HR with
the origin ``estimated`` through the profile save path (change history, undo).
A measured value is replaced only when that request says so. The Coach reads the
estimate but has no tool that adopts it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, timedelta
from typing import Any

from backend.canonical.first_threshold import (
    FIRST_THRESHOLD_FIELD,
    ORIGIN_ESTIMATED,
    ORIGIN_MEASURED,
    first_threshold_origin,
    first_threshold_percent,
    percent_of_lthr,
)
from backend.canonical.vocabulary import DfaQuality, Sport
from backend.db import activity_detail_store
from backend.errors import AppError
from backend.performance.first_threshold_estimate import (
    ESTIMATE_DAYS,
    FirstThresholdEstimate,
    SessionThreshold,
    estimate_view,
    first_threshold_estimate,
)

type LthrResolver = Callable[[date | None], float | None]
# A session id -> its step-test result (#3833), or None.
type StepTestReader = Callable[[str], Mapping[str, Any] | None]


class FirstThresholdService:
    """Read the athlete-level estimate and adopt it on the athlete's request."""

    def __init__(
        self,
        manager: Any,
        profile_service: Any,
        lthr_resolver: Callable[[], LthrResolver],
        today: Callable[[], date],
    ) -> None:
        self._manager = manager
        self._profile_service = profile_service
        self._lthr_resolver = lthr_resolver
        self._today = today

    def _estimate(self) -> tuple[FirstThresholdEstimate | None, LthrResolver]:
        today = self._today()
        with self._manager.reader() as db:
            rows = activity_detail_store.first_threshold_results(
                db,
                since=today - timedelta(days=ESTIMATE_DAYS - 1),
                until=today,
                sport=Sport.RUN,
            )
        resolver = self._lthr_resolver()
        sessions = [
            SessionThreshold(row.day, estimate, quality)
            for row in rows
            if (estimate := row.result.estimate_bpm) is not None
            and (quality := row.result.quality) is not None
        ]
        return (
            first_threshold_estimate(sessions, lambda day: resolver(day), today),
            resolver,
        )

    def estimate(self) -> dict[str, Any] | None:
        """The estimate as shown to the athlete and the Coach, or None."""
        estimate, resolver = self._estimate()
        return estimate_view(estimate, resolver(None))

    def adopt(
        self, payload: object, step_test: StepTestReader | None = None
    ) -> dict[str, Any]:
        """Store the shown estimate as the first threshold, with the origin estimated.

        With ``step_test`` (a session id) the value is that step test's good LT1
        (#3833), read by ``step_test``; ``heart_rate_bpm`` is the shown value.
        """
        if not isinstance(payload, dict):
            raise AppError(400, "Die Anfrage muss ein Objekt sein.")
        estimate, resolver = self._estimate()
        if payload.get("step_test") is not None:
            percent = _step_test_percent(payload, step_test, resolver(None))
        elif estimate is None:
            raise AppError(409, "Es liegt keine Schätzung der ersten Schwelle vor.")
        elif (
            first_threshold_percent(payload.get("percent_lthr"))
            != estimate.percent_lthr
        ):
            raise AppError(
                409, "Die Schätzung hat sich geändert. Bitte die Anzeige aktualisieren."
            )
        else:
            percent = estimate.percent_lthr
        profile = self._profile_service.get()
        origin = first_threshold_origin(profile)
        view = estimate_view(estimate, resolver(None))
        if (
            origin == ORIGIN_ESTIMATED
            and first_threshold_percent(profile.get(FIRST_THRESHOLD_FIELD)) == percent
        ):
            # Already stored: no profile write and no history entry.
            return {
                "profile": profile,
                "first_threshold_estimate": view,
                "adopted": False,
            }
        if origin == ORIGIN_MEASURED and payload.get("replace_measured") is not True:
            raise AppError(
                409,
                "Ein gemessener Wert ist gespeichert. Zum Ersetzen bitte bestätigen.",
            )
        saved = self._profile_service.set_first_threshold(
            f"{percent:g}", ORIGIN_ESTIMATED
        )
        return {"profile": saved, "first_threshold_estimate": view, "adopted": True}


def _step_test_percent(
    payload: Mapping[str, Any],
    step_test: StepTestReader | None,
    run_lthr: float | None,
) -> float:
    """The step test's good LT1 as percent of today's run threshold HR (#3833)."""
    tested = None if step_test is None else step_test(str(payload["step_test"]))
    value = (tested or {}).get("lt1") or {}
    if value.get("status") != "ok" or value.get("quality") != DfaQuality.GOOD.value:
        raise AppError(
            409, "Dieser Stufentest liefert keinen guten Wert der ersten Schwelle."
        )
    if payload.get("heart_rate_bpm") != value.get("heart_rate_bpm"):
        raise AppError(
            409, "Die Schätzung hat sich geändert. Bitte die Anzeige aktualisieren."
        )
    percent = (
        None
        if not run_lthr
        else percent_of_lthr(float(value["heart_rate_bpm"]), run_lthr)
    )
    if percent is None:
        raise AppError(409, "Ohne Schwellenpuls lässt sich der Wert nicht speichern.")
    return percent

"""Authenticated GET routes for athlete-facing read projections."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import unquote

from backend.athlete.profile import ProfileService
from backend.coach.context import CoachContextPreviewService
from backend.errors import AppError
from backend.http_api.public_performance import (
    PublicFeedbackStateService,
    PublicPerformanceStateService,
)
from backend.http_api.route_table import Access, Route, RouteRequest
from backend.planning.competition_service import CompetitionService
from backend.settings import SettingsService

ZONES_PATH = "/api/zones"
SKINFOLDS_PATH = "/api/body/skinfolds"
RACE_PLAN_PATH = "/api/competitions/{id}/race-plan"
_LOGGER = logging.getLogger("intervals_coach")


class AthleteGetRoutes:
    """Dispatch authenticated athlete reads through their state owners."""

    def __init__(
        self,
        public_performance_state_service: Callable[[], PublicPerformanceStateService],
        profile_service: Callable[[], ProfileService],
        competition_service: Callable[[], CompetitionService],
        public_feedback_state_service: Callable[[], PublicFeedbackStateService],
        coach_context_preview_service: Callable[[], CoachContextPreviewService],
        settings_service: SettingsService,
        zone_service: Callable[[], Any] | None = None,
        race_results: Callable[[list[dict[str, Any]]], list[dict[str, Any]]]
        | None = None,
        first_threshold_service: Callable[[], Any] | None = None,
        second_threshold_service: Callable[[], Any] | None = None,
        race_plan: Callable[[str], dict[str, Any] | None] | None = None,
        skinfold_service: Callable[[], Any] | None = None,
        energy_view: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._skinfold_service = skinfold_service
        self._energy_view = energy_view
        self._first_threshold_service = first_threshold_service
        self._second_threshold_service = second_threshold_service
        self._race_plan = race_plan
        self._race_results = race_results
        self._public_performance_state_service = public_performance_state_service
        self._profile_service = profile_service
        self._competition_service = competition_service
        self._public_feedback_state_service = public_feedback_state_service
        self._coach_context_preview_service = coach_context_preview_service
        self._settings_service = settings_service
        self._zone_service = zone_service

    def _competitions(self) -> list[dict[str, Any]]:
        competitions = self._competition_service().list(limit=100)
        if self._race_results is None:
            return competitions
        try:
            return self._race_results(competitions)
        except Exception as exc:  # noqa: BLE001 - results are optional; never fail the profile
            _LOGGER.warning(
                "competition race results unavailable: %s", type(exc).__name__
            )
            return competitions

    @staticmethod
    def _first_threshold_estimate(
        service: Callable[[], Any],
    ) -> dict[str, Any] | None:
        try:
            return service().estimate()
        except Exception as exc:  # noqa: BLE001 - the estimate is optional; never fail the profile
            _LOGGER.warning(
                "first threshold estimate unavailable: %s", type(exc).__name__
            )
            return None

    def routes(self) -> list[Route]:
        routes = [
            Route(
                "GET",
                "/api/performance",
                Access.SESSION,
                self._performance,
                heavy=True,
            ),
            Route("GET", "/api/profile", Access.SESSION, self._profile),
            Route("GET", "/api/feedback", Access.SESSION, self._feedback),
            Route(
                "GET",
                "/api/context-preview",
                Access.SESSION,
                self._preview,
                heavy=True,
            ),
        ]
        if self._zone_service is not None:
            routes.append(Route("GET", ZONES_PATH, Access.SESSION, self._zones))
        if self._skinfold_service is not None:
            routes.append(Route("GET", SKINFOLDS_PATH, Access.SESSION, self._skinfolds))
        if self._energy_view is not None:
            routes.append(
                Route(
                    "GET",
                    "/api/body/energy",
                    Access.SESSION,
                    self._energy,
                    heavy=True,
                )
            )
        if self._race_plan is not None:
            routes.append(
                Route("GET", RACE_PLAN_PATH, Access.SESSION, self._race_plan_read)
            )
        return routes

    def _performance(self, handler: Any, _request: RouteRequest) -> None:
        """Performance projection.

        Request: no query, no body.

        Response: 200 with `performance` (the current performance context, with
        `history` (with `first_thresholds`, the LT1 chart history) and
        `training_focus_periods` added; built by
        `PublicPerformanceStateService.performance_state`) and `garmin` (the public
        Garmin projection).

        Errors: none beyond the common ones.
        """
        handler.send_json(
            200,
            self._public_performance_state_service().performance_state(
                first_thresholds=True
            ),
        )

    @staticmethod
    def _lt2_estimates(service: Callable[[], Any]) -> dict[str, Any] | None:
        try:
            result: dict[str, Any] = service().estimates()
        except Exception as exc:  # noqa: BLE001 - the estimates are optional; never fail the profile
            _LOGGER.warning("LT2 estimates unavailable: %s", type(exc).__name__)
            return None
        return result

    def _profile(self, handler: Any, _request: RouteRequest) -> None:
        """Profile, competitions and the threshold estimates.

        Request: no query, no body.

        Response: 200 with `profile` (every profile field as a string),
        `competitions` (up to 100; a past competition carries `result` when a
        session is linked to it) and `first_threshold_estimate`: `percent_lthr`,
        `bpm`, `sessions`, `quality`, `from`, `to`, `spread_pct`, `source`, or null
        without an estimate or when computing it fails. `lt2_estimates` (#3273)
        holds per sport (`run`, `ride`) the local LT2 estimates `dfa_alpha1` and
        `critical_speed` (run) or `critical_power` (ride): `status` `ok` with the
        value (`heart_rate_bpm` and `pace_seconds_per_km`, or
        `pace_seconds_per_km`, or `power_w`), `quality`, its basis and `source`, or
        `unknown` with `reason`; null when computing them fails.

        Errors: none beyond the common ones.
        """
        payload: dict[str, Any] = {
            "profile": self._profile_service().get(),
            "competitions": self._competitions(),
        }
        if self._first_threshold_service is not None:
            payload["first_threshold_estimate"] = self._first_threshold_estimate(
                self._first_threshold_service
            )
        if self._second_threshold_service is not None:
            payload["lt2_estimates"] = self._lt2_estimates(
                self._second_threshold_service
            )
        handler.send_json(200, payload)

    def _feedback(self, handler: Any, _request: RouteRequest) -> None:
        """Activity feedback projection.

        Request: no query, no body.

        Response: 200 with `checkins` (the latest 30 check-ins), `local_feedback`
        (the check-in context) and `activity_feedback` (the activity feedback
        context).

        Errors: none beyond the common ones.
        """
        handler.send_json(200, self._public_feedback_state_service().feedback_state())

    def _zones(self, handler: Any, _request: RouteRequest) -> None:
        """Local and provider zone configurations.

        Request: no query, no body.

        Response: 200 with `sports` (`ride`, `run`, `swim`), `today` (athlete-local
        date), `configurations` (each with `key`, `source` `local` or the provider,
        `label`, `sport`, `metric`, `environment`, `valid_from`, `valid_until`,
        `threshold`, `maximum`, `limits`, `percent_limits`, `percent_basis`),
        `effective` (per sport, metric and environment the configuration in effect
        today: `sport`, `metric`, `environment`, `key`) and, only when there is one,
        `threshold_warnings`.

        Errors: none beyond the common ones.
        """
        if self._zone_service is not None:
            handler.send_json(200, self._zone_service().read())

    def _skinfolds(self, handler: Any, _request: RouteRequest) -> None:
        """The athlete's 3-site skinfold measurements, newest first.

        Request: no query, no body.

        Response: 200 with `measurements` (at most 20, each with `date`, `sex`
        `male` or `female` as the sites tell, `folds_mm` as `{site, label, mm}` in
        protocol order and `body_fat_pct` by Jackson-Pollock and Siri, null without
        a birth year in the profile; `age_outside_fit` true when the age lies outside
        the equation's fitted range), `birth_year_known` and `sites` (the sites per
        sex).

        Errors: none beyond the common ones.
        """
        if self._skinfold_service is not None:
            handler.send_json(200, self._skinfold_service().read())

    def _energy(self, handler: Any, _request: RouteRequest) -> None:
        """Resting and daily expenditure per source, up to yesterday.

        Request: no query, no body.

        Response: 200 with `end`, `windows` (the 12 weekly 4-week window ends,
        oldest first), `rows` (`katch_mcardle`, `cunningham`, `device` (the
        wearable's estimate) and `own`, each with `resting_kcal`, `total_kcal`
        and their nominal 95 % `*_band_kcal`; the first three also with
        `errors_kcal` per window and `mean_error_kcal` over the shared windows,
        and `split` (`resting_kcal`, `neat_kcal`, `sport_kcal`, `tef_kcal`, and
        `shared` with `neat`, `sport`, `tef` true for each part every row
        computes alike; null until
        known); null without a basis; `own` also has `resting_implausible`, true
        when its residual band lies entirely below the resting reference band,
        with its resting value and band then null), `best` (the source whose lead over the next
        exceeds the adaptive TDEE's mean band over at least 4 shared windows,
        else null), `fit` (`shared_windows`, `windows`, `min_windows`,
        `reference_band_kcal`), `basis` (`days`, `complete_days`,
        `flagged_days`, `weighins`, `insufficient`, `windows_with_estimate`,
        `rmr_source`, and `neat` with `days`, `steps_per_day`,
        `session_steps_per_day`, `floors_per_day`, `step_length_m` and
        `step_length_source` (`walks` or `height`) over the latest window's days
        with steps, or null) and `skinfold` (`date`, `body_fat_pct`, `lean_mass_kg`,
        or null).

        Errors: none beyond the common ones.
        """
        if self._energy_view is not None:
            handler.send_json(200, self._energy_view())

    def _preview(self, handler: Any, _request: RouteRequest) -> None:
        """Preview of the context the Coach receives.

        Request: no query, no body.

        Response: 200 with `generated_at`, `snapshot_truncated`,
        `snapshot_compacted`, `assembly`, `conversation`, `chat_prompt`,
        `structured_athlete_context`, `latest_activities`, `projection`,
        `context_text` and `local_training_library` (built by
        `CoachContextPreviewService.preview`). It changes no state.

        Errors: none beyond the common ones.
        """
        handler.send_json(200, self._coach_context_preview_service().preview())

    def _race_plan_read(self, handler: Any, request: RouteRequest) -> None:
        """Race plan of one stored competition, computed from local data.

        Request: path `id`, the competition id; no body.

        Response: 200 with `ok` and `race_plan`: `competition_id`,
        `method_version`, `duration` (`seconds`, `source` `target` or
        `planned_duration`; null when unknown), `pacing` (`status` `ready` with
        `method` `equal_effort` or `power_variation`, `sections_from`,
        `profile_rejected`, `sections` and for cycling `target_w`, or
        `unavailable` with `reason` `sport`, `distance` or `duration`) and
        `carbohydrates` (`source`, `during`, `before`, `weight`) and `weather`
        (`status` `ready` with the stored race-day `day`, `name`, `country` and
        `fetched_at`, or `no_location`, `not_in_horizon`, `pending`,
        `not_found` or `unavailable`). It changes no state and calls no
        provider: only the weather sync job fetches race-day forecasts.

        Errors: 404 `competition_not_found` "Dieser Wettkampf ist lokal nicht
        vorhanden." when the id is unknown.
        """
        assert self._race_plan is not None
        plan = self._race_plan(unquote(request.params["id"]))
        if plan is None:
            raise AppError(
                404,
                "Dieser Wettkampf ist lokal nicht vorhanden.",
                reason="competition_not_found",
            )
        handler.send_json(200, {"ok": True, "race_plan": plan})

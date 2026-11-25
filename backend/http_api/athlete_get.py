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
        energy_view: Callable[..., dict[str, Any]] | None = None,
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

    @staticmethod
    def _threshold_offers(
        service: Callable[[], Any], estimates: dict[str, Any] | None
    ) -> list[dict[str, Any]] | None:
        try:
            result: list[dict[str, Any]] = service().offers(estimates)
        except Exception as exc:  # noqa: BLE001 - the offers are optional; never fail the profile
            _LOGGER.warning("Threshold offers unavailable: %s", type(exc).__name__)
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
        `threshold_offers` (#4135) lists the open offers of a new LT1 or LT2, each
        with `key`, `threshold` (`lt1`, `lt2`), `sport`, `metric` (`heart_rate`,
        `pace`, `power`), `source` (`dfa_alpha1`, `critical_speed`,
        `critical_power`, `garmin`), `value` and `unit` (bpm, s/km, W), `current`
        (the value in effect, null when none), `sessions`, `window_days`,
        `quality`, `spread_pct` and `newest` (the newest session's or Garmin
        value's day; each null when the source has none) and `adopt`, the exact
        request body: for `lt1` `{"percent_lthr"}` of `POST
        /api/profile/first-threshold/adopt`, for `lt2` `{"sport", "source",
        "value", "shown"}` of `POST /api/zones/lt2/adopt`; null when computing
        them fails.

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
            estimates = self._lt2_estimates(self._second_threshold_service)
            payload["lt2_estimates"] = estimates
            payload["threshold_offers"] = self._threshold_offers(
                self._second_threshold_service, estimates
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
        today: `sport`, `metric`, `environment`, `key`), `defaults` (the standard
        zones that apply where no configuration exists: per metric `power`,
        `heart_rate` and `pace` the upper limits in percent of the threshold) and,
        only when there is one, `threshold_warnings`.

        Errors: none beyond the common ones.
        """
        if self._zone_service is not None:
            handler.send_json(200, self._zone_service().read())

    def _skinfolds(self, handler: Any, _request: RouteRequest) -> None:
        """The athlete's 3-site skinfold measurements, newest first.

        Request: no query, no body.

        Response: 200 with `measurements` (at most 20, each with `date`, `sex`
        `male` or `female` as the sites tell, `folds_mm` as `{site, label, mm,
        readings}` in protocol order, `mm` the median of the site's `readings`, and
        `body_fat_pct` by Jackson-Pollock and Siri, null without a birth year in
        the profile; `age_outside_fit` true when the age lies outside the
        equation's fitted range), `birth_year_known`, `sites` (the sites per sex)
        and `repeatability` (#3556: `repeat_sites` with two or more readings,
        `needed_sites` and `sd_pct`, one measurement's body fat SD in percentage
        points, null until enough sites have repeats).

        Errors: none beyond the common ones.
        """
        if self._skinfold_service is not None:
            handler.send_json(200, self._skinfold_service().read())

    def _energy(self, handler: Any, request: RouteRequest) -> None:
        """Resting and daily expenditure per source for one diary day.

        Request: optional query `date` (ISO day, today or earlier; default
        yesterday); no body.

        Response: 200 with `end` (yesterday, where the windows end), `day` (the
        day the source rows show) and `day_partial` (true for today, whose values
        are still partial; today `device` takes the newest full day's resting
        kcal of the 7 days before as `resting_kcal`, named by `resting_from`, or
        has none with `resting_missing` `no_recent_resting`), `expected_total_kcal`
        (today only, else null: the outlined source's total with the latest
        window's mean steps and digestion and today's planned units costed from
        their planned duration; null while a part is unknown),
        `energy_availability` (the 7-day mean of the intake less the sessions'
        net exercise energy per kg fat-free mass, `kcal_per_kg_ffm` over `days`
        complete days, `below` the floor of 30; null under 5 days or without a
        fat-free mass), `loss_rate` (the 28-day weight line's fall in
        `pct_per_week` of body mass, `fast` above 1; null when it is not
        falling), `windows` (the 12
        weekly 4-week window ends,
        oldest first), `rows` (`katch_mcardle`, `cunningham`, `device` (the
        wearable's estimate) and `own`, each with `resting_kcal`, `total_kcal`
        and their nominal 95 % `*_band_kcal`; the first three show `day`: its
        resting rate, its everyday and sport (0 without a session) and its
        digestion (null without logged meals, then no total; today a part whose
        input has not arrived counts 0 so far and is named in `pending_parts`,
        an unknown part stays null, #3494); `own` stays on its
        latest 28-day window, named by `window_days`; the first three also with
        `errors_kcal` per window and `mean_error_kcal` over the shared windows,
        and `split` (`resting_kcal`, `neat_kcal`, `sport_kcal`, `tef_kcal`, and
        `shared` with `neat`, `sport`, `tef` true for each part every row
        computes alike; null until
        known); null without a basis; `own` also has `resting_implausible`, true
        when its residual band lies entirely below the resting reference band,
        with its resting value and band then null; where its residual exceeds
        the resting reference, its `resting_kcal` and band are the reference's
        and `split` adds `resting_source` (`katch_mcardle` or `device`),
        `unmeasured_kcal` and `unmeasured_band_kcal`, #3550), `best` (the source whose lead over the next
        exceeds the adaptive TDEE's mean band over at least 4 shared windows,
        else null), `outlined` (`best`, else the source with the lowest
        `mean_error_kcal`: the one source the diary outlines, `goal` prices and
        Verlauf names for every period, #3613), `fit` (`shared_windows`, `windows`, `min_windows`,
        `reference_band_kcal`), `fit_scale` (Verlauf's periods `28d`, `91d`,
        `182d` and `365d`, #3704: `days`, `own_days`, `span_start`, `span_end`, `own_kcal`,
        `own_band_kcal`, `sources`, `weeks`, `weeks_with_data` and `deficit`, the
        deficit card of that period with the fields below, null when its weigh-ins
        do not suffice), `strength` (the day's strength sessions with `rpe`, local first, the
        `met` it set and `kcal`, #3530), `flagged` (meals marked as estimated: `days` in
        the latest window, `unflagged` from the latest window without any
        flagged day or null, `corrected` with the learned per-entry correction
        or null, `bias_pct` and `bias_band_pct` or null), `basis` (`days`, `complete_days`,
        `flagged_days`, `weighins`, `insufficient` (`intake_days`, `weighins`,
        `weight_span`, `logged_span` when the logged days span too little,
        #3667, or null), `windows_with_estimate`,
        `rmr_source`, and `neat` with `days`, `steps_per_day`,
        `session_steps_per_day`, `floors_per_day`, `step_length_m` and
        `step_length_source` (`walks` or `height`) of the shown day, whose steps,
        floors and sessions price the source rows' NEAT, or null without steps;
        the windows take each day's own NEAT, #3395, #3618), `body` (the formula
        rows' body fat: `source`
        `skinfold`, `device` or `profile` in this order, its `label`, `date` (null
        for the profile), `body_fat_pct` and `lean_mass_kg`, or null; a skinfold or
        device value counts from 90 days before up to a day after `end`) and
        `skinfold_missing` (why the formula rows are empty: `no_skinfold`,
        `skinfold_too_old`, `no_birth_year`, `no_weight` or `invalid`; null with a
        body), and `goal` (#3547; null without the profile's `energy_goal`):
        `goal`, `goal_kcal` (0 for `hold`), `source` (`outlined`; null without its
        total), `total_kcal` (that row's `total_kcal` of `day`), `target_kcal`
        (the total minus the deficit or plus the surplus), `eaten_kcal` (the
        day's logged kcal) and `remaining_kcal` (target minus eaten, negative
        above it; null without a total), and `deficit` (#3548; the latest
        window's energy per day from the body stores, null when its weigh-ins
        do not suffice; logged meals are not needed, #3603): `days`, `deficit_kcal` (negative for a surplus) with its 95 %
        `band_kcal`, `method` (`measured` when measurements weigh in the fat
        share, else `prior`), `fat_share` (`share_pct`, `prior_pct` and
        `measured_pct`, the measurements' weight, #3555), `span_days`,
        `weight_start_kg`, `weight_end_kg` (the means of the weigh-ins in the
        first and last 7 days, 14 for 3 and 6 months, with `edge_weighins`
        their counts; the line's ends, and `edge_weighins` null, where the
        edges do not come apart; #3739),
        `weight_change_kg` (their change, which the fat change is a share of,
        #3618), `kcal_per_kg` (per kg of weight change at the blended share,
        #3618), `fat_pct_start` (the first
        measurement used, else null), `fat_pct_end` (the last one, else the
        latest skinfold or null), `fat_date_start` and `fat_date_end` (their
        days; only readings of the period are compared, so a first reading
        before the period gives null `fat_pct_start` and `fat_date_start`, and
        `fat_last_only` is true when the last one lies before it too, #4037),
        `weight_last_kg` (the window's last weigh-in), `weight_date_start` and
        `weight_date_end` (the edges' mean days, else the first and last
        weigh-in, #3704, #3739), `fat_change_kg`, `fat_share_pct` (of the
        weight change, null below 0.05 kg), `stored_kcal` (over the span),
        `uncertainty` (`fat_share`, `weight_scatter`, or `unstable` when
        overlapping windows contradict each other, #3614), `total_kcal` (the window's
        expenditure, null without enough logged meals) with
        `expenditure_missing` (then its reason, such as `intake_days`, else
        null), `device_total_kcal` and `goal` (`goal`, `goal_kcal` and
        `status` `in` within 150 kcal per day of the goal either way, else
        `under` or `over`, and `distance` `near` (within 150), `mid` (within
        350) or `far` (#3606); null without a goal), `estimated_intake` (#3604;
        null with enough logged days or without a store): `kcal` with its 95 %
        `band_kcal` (the calibrated Katch-McArdle expenditure less the stored
        energy, both null when `missing` names why: `calibration`,
        `calibration_age` (over 63 days), `weight_change` (over 4 %),
        `activity_change` (over 30 %), `body` or `activity`), and the last
        calibrating window's `calibration_end` with `calibration_age_days`, and `wellness` (#3530; where Garmin's summary of `day` stands,
        null when it is finished or names no end): `as_of` (`HH:MM`, its wellness
        end) and `behind` (true when a session of the day ends after it, so the
        summary's steps and active kcal leave it out; every row's everyday is
        then not there yet, in `pending_parts`). `fit_scale` holds `28d` and
        `91d`; `period=182d` or `period=365d` adds that period (6 or 12 months)
        with its own read, which no other request pays for (#3711).

        Errors: 400 "Das Datum fehlt oder ist ungültig."; 400 "Der Tag liegt in
        der Zukunft.".
        """
        if self._energy_view is not None:
            days = request.query.get("date") or []
            periods = request.query.get("period") or []
            handler.send_json(
                200,
                self._energy_view(
                    days[0] if days else None, periods[0] if periods else None
                ),
            )

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

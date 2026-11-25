"""Authenticated local analysis reads and targeted refresh."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from backend.errors import AppError
from backend.http_api.route_table import Access, Route, RouteRequest
from backend.performance.comparisons import recurring_view
from backend.sync.analysis_refresh import validate_refresh_request


class AnalysisRoutes:
    def __init__(
        self,
        reports: Any,
        refresh: Any = None,
        trend_history: Callable[[Any], dict[str, Any]] | None = None,
    ):
        self._reports = reports
        self._refresh = refresh
        self._trend_history_read = trend_history

    def routes(self) -> list[Route]:
        return [
            Route(
                "GET",
                "/api/analysis/power-profiles",
                Access.SESSION,
                self._report,
                heavy=True,
            ),
            Route(
                "GET",
                "/api/analysis/training-records",
                Access.SESSION,
                self._report,
                heavy=True,
            ),
            Route(
                "GET",
                "/api/analysis/running-dynamics",
                Access.SESSION,
                self._running_dynamics,
            ),
            Route(
                "GET", "/api/analysis/trends", Access.SESSION, self._trends, heavy=True
            ),
            Route(
                "GET",
                "/api/analysis/strength",
                Access.SESSION,
                self._strength,
                heavy=True,
            ),
            Route(
                "GET",
                "/api/analysis/trend-history",
                Access.SESSION,
                self._trend_history,
                heavy=True,
            ),
            Route(
                "GET",
                "/api/analysis/week-review",
                Access.SESSION,
                self._week_review,
                heavy=True,
            ),
            Route(
                "POST", "/api/analysis/refresh", Access.SESSION_CSRF, self._refresh_area
            ),
        ]

    def _report(self, handler: Any, request: RouteRequest) -> None:
        """Analysis projection: power profiles or training records.

        Request: `power-profiles` takes no query and no body. `training-records`
        takes the optional query `record_type` (only `equipment`) and `record_id`
        (a UUID, required with `record_type`); no body.

        Response: 200. `power-profiles`: `status` (`ok` or `insufficient_data`),
        `best`, `critical_power`, `power_meter_changes`, `activities`, `windows`
        (keys `28` and `90`), `running`, `method`, `method_version`,
        `input_fingerprint`, `scope` and `all_time` (the window of every stored
        session, for the analysis cards only; `{"status": "pending"}` before its
        first background read finished, `{"status": "failed"}` after a failed one). `training-records`: `{"equipment": object}`
        without query, `{"record_type": "equipment", "record": object}` for one
        record.

        Errors: `training-records` only: 400 "Unbekannte Datensatzart."; 400
        "Ungültige Datensatz-ID."; 503 "Einzelne Trainingsdatensätze sind nicht
        verfügbar.". `power-profiles`: none beyond the common ones.
        """
        path = request.path
        values = {key: value[0] for key, value in request.query.items()}
        reports = self._reports()
        if path.endswith("/training-records"):
            payload = reports.records.training_records(values)
        else:
            payload = reports.profiles.power_profiles(all_time=True)
        handler.send_json(200, payload)

    def _week_review(self, handler: Any, request: RouteRequest) -> None:
        """The local review of one ISO week against the week before (#2805).

        Request: optional query `week`: `current` (default), `previous` or the
        ISO date of a Monday from this week back to 51 weeks before it, the
        weeks the stored check-ins cover (#3298); no body.

        Response: 200 with `method`, `week_start`, `week_end`, `effective_end`,
        `plan` (`planned`, `completed`, `missed`, `open`, `duration_share`,
        `missed_units`), `load` (`this_week`, `week_before`, `shares`),
        `recovery` (per metric `mean`, `nights`, `unit`, `source`, `week_before`,
        `position`), `notable` and `checkins`. Computed from local summaries;
        no model is asked.

        Errors: 400 "Ungültige Woche." for another `week`, a day that is no
        Monday, a future week or one more than 51 weeks back.
        """
        given = request.query.get("week")
        week = given[0] if given else "current"
        reports = self._reports()
        handler.send_json(200, reports.report.week_review(week, reports.timezone()))

    def _strength(self, handler: Any, request: RouteRequest) -> None:
        """Analyse › Kraft: how often per week the upper body and the legs were trained.

        Request: query `weeks` (optional): 4, 13 (default), 26 or 52; no body.

        Response: 200 with `start`, `end` (the calendar weeks up to today) and
        `weeks` (`week_start`, `sessions`: completed strength sessions, `upper`
        and `legs`: sessions that trained that side, a full body session both,
        `unknown`: sessions that decide neither, never guessed; 0 without one).
        No strength load.

        Errors: 400 "Ungültiger Analysezeitraum." for another `weeks`.
        """
        given = request.query.get("weeks")
        try:
            weeks = int(given[0]) if given else 13
        except ValueError:
            weeks = 0
        handler.send_json(200, self._reports().report.strength(weeks))

    def _trend_history(self, handler: Any, request: RouteRequest) -> None:
        """The history of a longer trend period: 3, 6 or 12 months.

        Request: query `days`: `91`, `182` or `365`; no body. Read only when the
        athlete picks 6 or 12 months (3 Monate is served by the performance state).

        Response: 200 with `start`, `end`, `days`, `history` (shaped like
        `performance.history` over the span, with one body window named after it,
        for example `182d`), `training_focus` (that many days) and
        `personal_recovery` (its baselines' `history` over the same span).
        Stored values only; no provider request.

        Errors: 400 "Ungültiger Analysezeitraum." for another `days`.
        """
        given = request.query.get("days")
        try:
            days = int(given[0]) if given else None
        except ValueError:
            days = None
        if self._trend_history_read is None:
            raise AppError(503, "Der Analysezeitraum ist nicht verfügbar.")
        handler.send_json(200, self._trend_history_read(days))

    def _running_dynamics(self, handler: Any, request: RouteRequest) -> None:
        """Running dynamics against the athlete's own runs at the same pace (#2829).

        Request: query `activity_id` (the local session id, at most 200
        characters); no body.

        Response: 200 with `status` (`pending` before the first background read
        of every stored session finished, `failed` after a failed one, else
        `ok`), and with `ok` `stale` and `session` (the run's rating per pace
        band and per value; null without a stored run).

        Errors: 400 "Ungültige Aktivitäts-ID." for a missing id or one longer
        than 200 characters.
        """
        given = request.query.get("activity_id")
        activity_id = given[0] if given else ""
        if not activity_id or len(activity_id) > 200:
            raise AppError(400, "Ungültige Aktivitäts-ID.")
        handler.send_json(200, self._reports().profiles.running_dynamics(activity_id))

    def _trends(self, handler: Any, _request: RouteRequest) -> None:
        """Pace at equal heart rate and recurring sessions, as the Coach reads them (#2802).

        Request: no query, no body.

        Response: 200 with `pace_at_heart_rate`, the run trend of the Coach's
        12-week progress review (`method`, `unit`, `scope` and `run` per
        environment `outdoor` and `indoor`: `status`, `heart_rate_band_bpm`,
        `sources`, `sessions` with `date`, `activity_id`, `pace_s_per_km`,
        `earlier`, `later` and `change_s_per_km`; or only `status`
        `insufficient_data` or `outside_curve_window`), and `recurring`, the
        Coach's comparisons section with each observation cut to `date`,
        `activity_id`, `name`, `source`, `steps` (`target`, `unit`, `mean`,
        `status`) and `repeat_groups`. It changes no state and calls no provider.

        Errors: none beyond the common ones.
        """
        reports = self._reports()
        handler.send_json(
            200,
            {
                "pace_at_heart_rate": reports.report.run_pace_trend(),
                "recurring": recurring_view(reports.records.comparisons()),
            },
        )

    def _refresh_area(self, handler: Any, _request: RouteRequest) -> None:
        """Run a targeted refresh of one analysis area.

        Request: JSON body `{"area": string, "days": integer}`. `area` is required:
        `load`, `performance`, `recovery` or `sessions`. `days` is optional, `28` or
        `91`, and only for `load` and `recovery` (default 91 for `load`, 28 for
        `recovery`); the other areas use fixed windows.

        Response: 200 with `area`, `status` (`ok`, `partial`, `already_running` or
        `failed`), `refreshed_sources`, `incomplete_sources`, `pending_sources`,
        `not_refreshed_sources` (source names) and `source_status` (per source name
        its status string). Garmin is read for every area, Wahoo also for
        `sessions` and `performance`; a source that is not configured is left out.

        Errors: 400 "Ungültiger Analysebereich."; 400 "Ungültiger Analysezeitraum.";
        503 `analysis_refresh_unavailable` "Die gezielte Aktualisierung ist nicht
        verfügbar.".
        """
        payload = handler.read_json()
        area = payload.get("area") if isinstance(payload, dict) else None
        days = payload.get("days") if isinstance(payload, dict) else None
        validate_refresh_request(area, days)
        if self._refresh is None:
            raise AppError(
                503,
                "Die gezielte Aktualisierung ist nicht verfügbar.",
                reason="analysis_refresh_unavailable",
            )
        handler.send_json(200, self._refresh().refresh(area, days=days))

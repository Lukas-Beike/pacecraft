"""Cohesive local training analysis read use cases."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal, overload

from backend.activities.activity_summary import (
    ActivitySummary,
    stored_activity_key,
    summary_duration_s,
    summary_is_electric,
    summary_is_indoor,
    summary_local_date,
)
from backend.activities.session_purpose import summary_is_not_training, summary_is_race
from backend.athlete.local_date import iso_date_prefix
from backend.canonical import sensor_registry
from backend.canonical.stored_sports import resolve_workout_sport
from backend.canonical.vocabulary import Sport
from backend.db import activity_detail_store
from backend.errors import AppError
from backend.performance.activity_records import activity_records, session_totals
from backend.performance.canonical_inputs import (
    CanonicalPerformanceSource,
    activity_summaries_for,
    activity_summaries_with_load_days,
    activity_summaries_with_sync_time,
    resolve_reader,
)
from backend.performance.canonical_recovery import read_recovery_inputs
from backend.performance.comparisons import recurring_training_comparisons
from backend.performance.critical_power import WINDOW_DAYS as CP_WINDOW_DAYS
from backend.performance.critical_power import critical_power_speed
from backend.performance.mean_max_curve import (
    SessionCurve,
    SessionCurveReader,
    input_fingerprint,
)
from backend.performance.personal_recovery import personal_recovery
from backend.performance.power_profile import (
    LEGACY_POWER_DURATIONS,
    METHOD_ID,
    METHOD_VERSION,
    POWER_DURATIONS,
    RUNNING_DISTANCES,
    RUNNING_DURATIONS,
)
from backend.performance.progress_report import run_pace_trend, weekly_progress
from backend.performance.running_dynamics import AVERAGE_KEYS, session_rating
from backend.performance.season_inputs import (
    SeasonSource,
    read_activity_summaries,
)
from backend.performance.session_analysis_reads import analysis_summaries
from backend.performance.session_rpe_load import FeedbackLookup, feedback_by_session
from backend.performance.smoothed_load import history_start
from backend.performance.tag_impact import tag_impact
from backend.performance.training_report import training_report
from backend.performance.week_review import review_monday, week_review
from backend.weather.activity_weather_store import session_temperatures

# Analysis windows of the power and running profiles, shortest first.
PROFILE_WINDOW_DAYS = (28, 90)
# Analyse shows the trends of the 12-week progress review (#2802).
ANALYSIS_TREND_DAYS = 84
PROGRESS_DAYS = frozenset({7, 28, 42, 56, 84})
# Analyse › Kraft shows twelve calendar weeks like the endurance development (#3764).
STRENGTH_WEEKS = (4, 13, 26, 52)
STRENGTH_DEFAULT_WEEKS = 13


def report_sport(value: Any) -> str | None:
    """Return the report sport filter: "all" or a canonical sport, else None.

    A previous Intervals.icu-style token (for example "VirtualRide") resolves to
    its canonical sport, as the other HTTP and Coach inputs accept it.
    """
    text = str(value or "all").strip()
    if text == "all":
        return text
    resolved = resolve_workout_sport(text) if len(text) <= 40 else None
    return None if resolved is None else resolved.sport.value


class TrainingRecordsService:
    def __init__(
        self,
        *,
        database_manager: Any,
        read_equipment: Callable[[], dict[str, Any]],
        read_record: Callable[[str, str], dict[str, Any]] | None,
        canonical: CanonicalPerformanceSource,
    ) -> None:
        self._canonical = canonical
        self._database_manager = database_manager
        self._read_equipment = read_equipment
        self._read_record = read_record

    def training_records(self, values: dict[str, Any] | None = None) -> dict[str, Any]:
        values = values or {}
        if values.get("record_type") or values.get("record_id"):
            kind, record_id = (
                values.get("record_type"),
                str(values.get("record_id") or ""),
            )
            if kind != "equipment":
                raise AppError(400, "Unbekannte Datensatzart.")
            try:
                uuid.UUID(record_id)
            except ValueError as exc:
                raise AppError(400, "Ungültige Datensatz-ID.") from exc
            if self._read_record is None:
                raise AppError(
                    503, "Einzelne Trainingsdatensätze sind nicht verfügbar."
                )
            return self._read_record(kind, record_id)
        return {"equipment": self._read_equipment()}

    def comparisons(self) -> dict[str, Any]:
        return recurring_training_comparisons(self.observations())

    def observations(self) -> list[dict[str, Any]]:
        """Stored canonical session analyses (#1685) with their session fields."""
        stored_analyses = analysis_summaries(self._database_manager)
        with self._database_manager.reader() as db:
            temperatures = session_temperatures(
                db, [stored["session_id"] for stored in stored_analyses]
            )
        known = {
            str(summary.get("id")): summary
            for summary in activity_summaries_for(self._canonical)
        }
        records = []
        for stored in stored_analyses:
            summary = known.get(stored["session_id"])
            if summary is None:
                continue
            fields = {
                key: value for key, value in stored.items() if key != "session_id"
            }
            records.append(
                {
                    **fields,
                    **summary_fields(summary),
                    "activity_id": stored_activity_key(summary),
                    "source": stored.get("source") or summary.get("source"),
                    # The activity weather, never the device sensor (#3036).
                    "weather_temp_c": temperatures.get(stored["session_id"]),
                    "duration": summary_duration_s(summary),
                }
            )
        records.sort(key=lambda row: row["date"], reverse=True)
        return records

    def endurance(self) -> dict[str, Any]:
        records = [
            row for row in self.observations() if row["aerobic"].get("status") == "ok"
        ]
        return {
            "status": "ok" if records else "insufficient_data",
            "activities": [
                {k: v for k, v in row.items() if k != "pace_at_hr_profile"}
                for row in records
            ],
            "scope": "Bis zu 100 lokal gespeicherte Detaildatensätze; nur aktuelle Aktivitäten mit geeigneten Originaldaten.",
            "comparison": "Individual observations, no inferred fitness trend. Compare sport, duration, intensity, terrain, temperature (weather_temp_c: the activity weather, null without it) and indoor/outdoor conditions.",
        }


class TrainingProfileSelectionService:
    def __init__(
        self,
        today: Callable[[], date],
        *,
        canonical: CanonicalPerformanceSource | None = None,
        curves: SessionCurveReader | None = None,
        pause: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.thread_time,
    ) -> None:
        self._today = today
        self._canonical = canonical
        self._curves = curves
        self._pause = pause
        self._clock = clock

    @overload
    def curve_records(
        self, days: int = ..., *, shared: Literal[False] = False
    ) -> list[dict[str, Any]]: ...

    @overload
    def curve_records(
        self, days: int = ..., *, shared: Literal[True]
    ) -> tuple[Mapping[str, Any], ...]: ...

    def curve_records(
        self, days: int = PROFILE_WINDOW_DAYS[-1], *, shared: bool = False
    ) -> list[dict[str, Any]] | tuple[Mapping[str, Any], ...]:
        """Ride and run sessions of the last ``days`` (default the longest window) with their session curves.

        E-bike sessions (``summary_is_electric``) and commutes, warm-ups and
        cool-downs (``summary_is_not_training``) are left out; races carry
        ``effort_context`` "race".
        ``shared`` returns the records of the last read while the day, what they
        show of the sessions and the stored inputs of their curves are unchanged:
        one tuple of read-only mappings for every such caller.
        Sessions without full-resolution streams stay in the list with empty
        profiles, so the coverage counts them as unknown.
        """
        canonical, curve_reader = self._sources()
        today = self._today()
        start = today - timedelta(days=days - 1)
        with canonical.database_manager().read_snapshot() as db:
            sessions = _curve_sessions(canonical, db, start, today)
            session_ids = [str(summary.get("id") or "") for summary in sessions]
            temperatures = session_temperatures(db, session_ids)

            def build() -> list[dict[str, Any]]:
                curves = curve_reader.curves(db, session_ids)
                records = [
                    _curve_record(summary, curves, temperatures) for summary in sessions
                ]
                records.sort(key=lambda row: row["date"], reverse=True)
                return records

            if not shared:
                return build()
            # What the records show of a session is its curve-less record.
            shown = [_curve_record(summary, {}, temperatures) for summary in sessions]
            return curve_reader.shared_records(
                db, session_ids, (today.isoformat(), days, shown), build
            )

    def all_time_records(
        self,
    ) -> tuple[tuple[Mapping[str, Any], ...], bool] | None:
        """Every stored ride and run session with its curve, built off the request path.

        The sessions of ``curve_records`` without a start day (#2694), with no
        temperature (``weather_temp_c`` None: no all-time reader uses it, #3036). Their
        curves are read in a background build of its own, one session per read
        snapshot (``SessionCurveReader.background_records``), so no request
        waits for a cold read of the whole history: the records with whether
        they are current, None before the first build finished, RuntimeError
        after a failed one. After each session, outside its snapshot, the build
        pauses as long as that session's own work took (its thread time, so a
        wait for the interpreter or a reader does not lengthen it): its
        pure-Python work would otherwise hold the interpreter and stall every
        request thread (#2757). Before that pause a curve whose stored copy is
        missing or older is stored, and at the end the stored curves of merged
        and deleted sessions are dropped, so a restart loads instead of
        recomputing (#2821).
        """
        canonical, curve_reader = self._sources()
        manager = canonical.database_manager()
        with manager.read_snapshot() as db:
            sessions = _curve_sessions(canonical, db, None, self._today())
            session_ids = [str(summary.get("id") or "") for summary in sessions]

            def build() -> list[dict[str, Any]]:
                curves: dict[str, SessionCurve] = {}
                # Oldest first, so the recent windows' curves are the most recently
                # used: a session added before the next build evicts an old curve,
                # never a recent one (#2822). The records keep the sessions' order.
                for session_id in reversed(session_ids):
                    started = self._clock()
                    with manager.read_snapshot() as one:
                        found = curve_reader.curves(one, [session_id])
                        stored = activity_detail_store.session_curve_fingerprints(
                            one, list(found)
                        )
                    curves.update(found)
                    # Outside the snapshot: keep the curve across restarts (#2821).
                    curve_reader.store_changed(manager, found, stored)
                    self._pause(self._clock() - started)
                curve_reader.drop_unlive(manager)
                return [_all_time_record(summary, curves) for summary in sessions]

            # Room for every session's curve: a smaller memo would make the build
            # evict the recent windows' curves and recompute the whole history on
            # every rebuild (#2822).
            curve_reader.reserve(len(session_ids))
            shown = [_all_time_record(summary, {}) for summary in sessions]
            # The session list decides the records; a new day alone builds nothing.
            return curve_reader.background_records(
                db, session_ids, (None, shown), build
            )

    def _sources(self) -> tuple[CanonicalPerformanceSource, SessionCurveReader]:
        if self._canonical is None or self._curves is None:
            raise RuntimeError("power profiles need the canonical source and curves")
        return self._canonical, self._curves

    def _all_time_window(self, today: date) -> dict[str, Any]:
        """The all-time best values; pending before their first build, failed after one."""
        try:
            every = self.all_time_records()
        except RuntimeError:
            return {"status": "failed"}
        if every is None:
            return {"status": "pending"}
        rows = [dict(row) for row in every[0]]
        # The window starts on the day of the oldest stored session.
        first = min((str(row["date"]) for row in rows), default=today.isoformat())
        days = (today - date.fromisoformat(first)).days + 1
        window = self._profile_window(rows, today, max(days, 1))
        # The cards read no id list; it would grow with the whole history.
        window["cache_coverage"].pop("eligible_activities")
        # The longest and highest ride and run, beside the bests (#3826).
        window["activity_records"] = activity_records(rows)
        # Values of an earlier read while a newer one runs; the cards ask again.
        window["stale"] = not every[1]
        return window

    def running_dynamics(self, activity_id: str) -> dict[str, Any]:
        """The running-dynamics rating of the run ``activity_id`` (#2829).

        It reads ``all_time_records``, built off the request path: ``status``
        is ``pending`` before its first build, ``failed`` after a failed one,
        else ``ok`` with ``stale`` while a newer build runs. ``session`` is
        None for a session without a stored record.
        """
        try:
            every = self.all_time_records()
        except RuntimeError:
            return {"status": "failed"}
        if every is None:
            return {"status": "pending"}
        records = [row for row in every[0] if row.get("sport") == "run"]
        result: dict[str, Any] = {"status": "ok", "stale": not every[1]}
        own = next(
            (row for row in records if row.get("activity_id") == activity_id), None
        )
        if own is not None:
            return {**result, "session": session_rating(own, records)}
        # A current build without the run leaves it out (a commute, a warm-up).
        session = None if not every[1] else {"status": "not_covered"}
        return {**result, "session": session}

    def power_profiles(
        self, records: list[dict[str, Any]] | None = None, *, all_time: bool = False
    ) -> dict[str, Any]:
        """Best values, CP/CS and coverage of the last 90 days.

        ``all_time`` (the analysis cards, #2694) adds ``all_time``: the best values
        of every stored session, ``{"status": "pending"}`` before their first
        background read finished or ``{"status": "failed"}`` after a failed one.
        The Coach's copy and the CP/CS input stay on 90 days.
        """
        if records is None:
            records = self.curve_records()
        today = self._today()
        eligible_records = [
            row for row in records if str(row.get("date") or "") <= today.isoformat()
        ]
        valid = [
            row
            for row in eligible_records
            if (row.get("power_profile") or {}).get("status") == "ok"
            or (row.get("running_profile") or {}).get("status") == "ok"
        ]
        # Keep one compatibility winner per sport/duration.
        compatibility = self._compatibility_profiles(eligible_records)
        # Keep all cached records in the window so coverage reports include
        # activities whose streams are incomplete and therefore yield unknown
        # points. Candidates below still require a measured profile point.
        windows = {
            str(days): self._profile_window(eligible_records, today, days)
            for days in PROFILE_WINDOW_DAYS
        }
        cp_start = (today - timedelta(days=CP_WINDOW_DAYS - 1)).isoformat()
        cp_records = [
            row for row in eligible_records if cp_start <= str(row.get("date") or "")
        ]
        changes = self._power_meter_changes()
        critical_power = critical_power_speed(
            cp_records, today, input_fingerprint=_records_fingerprint(cp_records)
        )
        in_window = sensor_registry.changes_since(
            changes, "ride", today - timedelta(days=CP_WINDOW_DAYS - 1)
        )
        if in_window:
            # The window's cycling efforts come from two power meters.
            critical_power["power_meter_changes"] = in_window
        result: dict[str, Any] = {
            "status": "ok" if compatibility or valid else "insufficient_data",
            "best": compatibility,
            # Before the activity list, so a bounded Coach copy keeps it.
            "critical_power": critical_power,
            "power_meter_changes": changes,
            "activities": records,
            "windows": windows,
            "running": {
                "name": "running_best_efforts",
                "type": "running",
                "windows": {key: value["running"] for key, value in windows.items()},
            },
            "method": METHOD_ID,
            "method_version": METHOD_VERSION,
            "input_fingerprint": _records_fingerprint(eligible_records),
            "scope": "Beobachtete Bestwerte der letzten 90 Tage aus lokal gespeicherten Messreihen in voller Auflösung; heruntergerechnete Reihen zählen nicht. Fehlende oder ungültige Fenster bleiben unbekannt. Bestwerte mit gleicher effort_group stammen aus derselben Belastung und sind voneinander abhängig. Keine FTP- oder Tempo-Schätzung; CP/CS nur als experimentelle Modellschätzung unter critical_power.",
        }
        if all_time:
            result["all_time"] = self._all_time_window(today)
        return result

    def records(self) -> dict[str, Any]:
        """The all-time records of Analyse › Rekorde, for the Coach (#3831).

        The same window the cards read: ``status`` pending or failed before a
        usable background build, ``stale`` while a newer one runs.
        """
        return {
            "all_time": self._all_time_window(self._today()),
            "scope": "Rekorde aller gespeicherten Einheiten wie in Analyse › Rekorde: beste Leistung je Dauer (outdoor und indoor getrennt), beste Laufzeiten je Strecke und activity_records (weiteste, längste und höchste Einzelfahrt (ohne Indoor-Fahrten) sowie Einzellauf, je Distanz, Bewegungszeit und Höhenmeter). status pending oder failed heißt unbekannt, nicht kein Rekord; stale heißt, ein neuerer Stand wird gerade berechnet.",
        }

    def _power_meter_changes(self) -> list[dict[str, Any]]:
        """The days a sport's power meter changed, newest first (#1869)."""
        if self._canonical is None:
            return []
        with self._canonical.database_manager().read_snapshot() as db:
            return sensor_registry.power_meter_changes(
                activity_detail_store.sensor_uses(db)
            )

    @staticmethod
    def _compatibility_profiles(
        eligible_records: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        compatibility = []
        for environment in ("outdoor", "indoor"):
            for duration in LEGACY_POWER_DURATIONS:
                candidates = [
                    (p.get("watts"), row)
                    for row in eligible_records
                    if row.get("sport") == "ride"
                    and row.get("environment") == environment
                    for p in (row.get("power_profile") or {}).get("points", [])
                    if p.get("duration_seconds") == duration
                    and p.get("watts") is not None
                ]
                if candidates:
                    watts, row = max(candidates, key=lambda item: item[0])
                    compatibility.append(
                        {
                            "sport": "ride",
                            "type": row.get("activity_type") or "ride",
                            "environment": environment,
                            "duration_seconds": duration,
                            "watts": watts,
                            "activity_id": row["activity_id"],
                            "date": row["date"],
                            "observed_at": row["observed_at"],
                            "source": row.get("source") or "original activity stream",
                            "device": row.get("device") or "unknown",
                        }
                    )
        return compatibility

    @staticmethod
    def _profile_window(
        records: list[dict[str, Any]], today: date, days: int
    ) -> dict[str, Any]:
        start = today - timedelta(days=days - 1)
        eligible = [
            row
            for row in records
            if start.isoformat() <= str(row.get("date") or "") <= today.isoformat()
        ]
        source = "local full-resolution activity streams"
        usable = [
            row
            for row in eligible
            if (row.get("power_profile") or {}).get("status") == "ok"
            or (row.get("running_profile") or {}).get("status") == "ok"
        ]
        rides = [row for row in eligible if row.get("sport") == "ride"]
        ride_usable = [
            row
            for row in rides
            if (row.get("power_profile") or {}).get("status") == "ok"
        ]
        result: dict[str, Any] = {
            "date_start": start.isoformat(),
            "date_end": today.isoformat(),
            "source": source,
            "cache_coverage": {
                "records": len(eligible),
                "usable_records": len(usable),
                "unknown_records": len(eligible) - len(usable),
                # Rides only: the power bests cannot come from runs.
                "power_records": len(rides),
                "power_unknown_records": len(rides) - len(ride_usable),
                "eligible_activities": [row["activity_id"] for row in eligible],
            },
            "input_fingerprint": _records_fingerprint(eligible),
            "power": [],
            "running": {"speed": [], "distance": []},
        }
        for environment in ("outdoor", "indoor"):
            for duration in POWER_DURATIONS:
                candidates = _power_candidates(eligible, environment, duration)
                result["power"].append(
                    TrainingProfileSelectionService._winner(
                        candidates,
                        ("ride", environment),
                        duration,
                        "watts",
                        "duration_seconds",
                    )
                )
        for key, field, values in (
            ("speed", "speed_mps", RUNNING_DURATIONS),
            ("distance", "seconds", RUNNING_DISTANCES),
        ):
            for value in values:
                candidates = _running_candidates(eligible, key, field, value)
                result["running"][key].append(
                    TrainingProfileSelectionService._winner(
                        candidates,
                        ("run", None),
                        value,
                        field,
                        "duration_seconds" if key == "speed" else "distance_meters",
                        minimum=key == "distance",
                    )
                )
        _mark_effort_groups(result["power"])
        _mark_effort_groups(
            [*result["running"]["speed"], *result["running"]["distance"]]
        )
        return result

    @staticmethod
    def _winner(
        candidates: list[tuple[dict[str, Any], dict[str, Any]]],
        sport: tuple[str, str | None],
        value: float,
        field: str,
        label: str,
        minimum: bool = False,
    ) -> dict[str, Any]:
        if not candidates:
            name, wanted_environment = sport
            return {
                "sport": name,
                **(
                    {}
                    if wanted_environment is None
                    else {"environment": wanted_environment}
                ),
                label: value,
                field: None,
                "status": "unknown",
                "reason": "No complete valid local window.",
            }
        point, row = (min if minimum else max)(
            candidates, key=lambda item: item[0][field]
        )
        actual_sport = row.get("sport", sport[0])
        return {
            "sport": actual_sport,
            "type": row.get("activity_type") or actual_sport,
            "environment": row.get("environment") or "outdoor",
            label: value,
            field: point[field],
            "activity_id": row.get("activity_id"),
            "date": row.get("date"),
            "observed_at": row.get("observed_at"),
            "start_seconds": point.get("start_seconds"),
            "status": "ok",
            "source": point.get("source")
            or row.get("source")
            or "original activity stream",
            "device": row.get("device") or "unknown",
        }


@dataclass(frozen=True)
class BlockReviewSource:
    """The planning use cases the block review reads: the review and the last test."""

    review: Callable[..., dict[str, Any]]
    # Day of the last completed local test unit in the plan, for since_last_test.
    last_test: Callable[[Mapping[str, Any] | None], str | None]


class TrainingReportReadService:
    def __init__(
        self,
        *,
        read_plan: Callable[[], Any],
        read_checkins: Callable[[], Any],
        read_feedback: Callable[[list[str]], list[dict[str, Any]]],
        today: Callable[[], date],
        canonical: CanonicalPerformanceSource,
        session_feedback: FeedbackLookup | None = None,
        now: Callable[[], datetime] | None = None,
        curve_records: Callable[[], list[dict[str, Any]]] | None = None,
        training_model: Callable[[], str] = lambda: "",
        block_review: BlockReviewSource | None = None,
        strength_sides: Callable[
            [Mapping[str, Any]], tuple[bool | None, bool | None] | None
        ]
        | None = None,
    ) -> None:
        self._strength_sides = strength_sides
        self._block_review = block_review
        self._training_model = training_model
        self._curve_records = curve_records
        self._read_plan = read_plan
        self._read_checkins = read_checkins
        self._read_feedback = read_feedback
        self._session_feedback = session_feedback
        self._now = now
        self._today = today
        self._canonical = canonical

    def progress(
        self,
        values: dict[str, Any],
        timezone: str = "UTC",
        *,
        with_curves: bool = True,
    ) -> dict[str, Any]:
        """Weekly per-sport progress over whole ISO weeks (7, 28, 42, 56 or 84 days).

        ``with_curves=False`` skips the mean-max curve read for callers that
        never use it.
        """
        today = self._today()
        try:
            days = int(values.get("days", 42))
            start = (
                _monday(date.fromisoformat(str(values["start"])))
                if values.get("start")
                else _window_start(today, days)
            )
        except (TypeError, ValueError) as exc:
            raise AppError(400, "Ungültiger Berichtszeitraum.") from exc
        sport = report_sport(values.get("sport"))
        if (
            days not in PROGRESS_DAYS
            or not date(2010, 1, 1) <= start <= today
            or sport is None
        ):
            raise AppError(400, "Ungültiger Berichtszeitraum oder Sportfilter.")
        # One read: the fitness load days belong to the same revision as the summaries.
        summaries, observed_at, load_days = activity_summaries_with_load_days(
            self._canonical
        )
        period = [
            row
            for row in summaries
            if start.isoformat()
            <= summary_local_date(row, timezone)
            <= today.isoformat()
        ]
        return weekly_progress(
            start=start,
            weeks=days // 7,
            today=today,
            sport=sport,
            timezone=timezone,
            activity_summaries=summaries,
            session_feedback=feedback_by_session(period, self._session_feedback),
            plan=self._read_plan(),
            local_load_days=load_days,
            # Session curves cover the longest profile window only; read them
            # only when the window needs the mean-max comparison.
            curve_records=self._curve_records()
            if with_curves
            and self._curve_records is not None
            and days >= 14
            and sport in {"all", "ride", "run"}
            # A window from a Monday up to six days before the curve window
            # (a 12-week review on a Sunday) still reads it; those days miss.
            and start >= today - timedelta(days=PROFILE_WINDOW_DAYS[-1] - 1 + 6)
            else None,
            observed_at=observed_at,
        )

    def run_pace_trend(self) -> dict[str, Any]:
        """Pace at heart rate of the 12-week progress review, for Analyse (#2802)."""
        today = self._today()
        return run_pace_trend(
            None if self._curve_records is None else self._curve_records(),
            _window_start(today, ANALYSIS_TREND_DAYS),
            ANALYSIS_TREND_DAYS // 7,
            today,
        )

    def _feedback_of(
        self, summaries: Sequence[ActivitySummary]
    ) -> list[dict[str, Any]]:
        """The effective feedback of the summaries' sessions, keyed by stored key.

        Rows stored under a provider record id of a session are found as well.
        """
        keys = list(
            dict.fromkeys(key for s in summaries if (key := stored_activity_key(s)))
        )
        return self._read_feedback(keys) if keys else []

    def block_review(
        self,
        values: dict[str, Any],
        timezone: str = "UTC",
        *,
        with_curves: bool = True,
    ) -> dict[str, Any]:
        """Planned versus completed over a block; default: the last 4 whole weeks."""
        if self._block_review is None:
            raise AppError(503, "Die Blockauswertung ist nicht verfügbar.")
        today = self._today()
        plan = self._read_plan()
        if values.get("since_last_test") is True:
            values = {
                **values,
                **_since_last_test(self._block_review.last_test(plan), today),
            }
        days = values.get("days") or 28
        weeks = days // 7 if isinstance(days, int) else 4
        window = {
            "days": days,
            "start": values.get("start")
            or (today - timedelta(days=today.weekday() + 7 * weeks)).isoformat(),
            "sport": "all",
        }
        progress = self.progress(window, timezone, with_curves=with_curves)
        summaries, _ = activity_summaries_with_sync_time(self._canonical)
        return self._block_review.review(
            progress=progress,
            plan=plan,
            checkins=self._read_checkins(),
            summaries=summaries,
            activity_feedback=self._feedback_of(summaries),
            timezone=timezone,
            training_model=self._training_model(),
        )

    def week_review(self, week: str, timezone: str = "UTC") -> dict[str, Any]:
        """The local review of one ISO week the calendar shows (#2805, #3298).

        Reads the existing summaries only: the weekly progress of the week and
        the week before (no session curves), the plan with its compliance, the
        session summaries, the recovery baselines and the check-ins. No model
        is asked. The recovery range is read as of the week's last day so far,
        so a past week is set against the range it had then.
        """
        today = self._today()
        monday = review_monday(week, today)
        as_of = min(monday + timedelta(days=6), today)
        before = monday - timedelta(days=7)
        # One read from the week before; its first two weeks are compared.
        progress = self.progress(
            {"days": 28, "start": before.isoformat()}, timezone, with_curves=False
        )
        summaries, _ = activity_summaries_with_sync_time(self._canonical)
        canonical = self._canonical
        with canonical.database_manager().read_snapshot() as db:
            recovery_inputs = read_recovery_inputs(
                resolve_reader(canonical.observation_reader(), db),
                db,
                as_of=as_of,
            )
        return week_review(
            monday=monday,
            today=today,
            progress=progress,
            calendar=(self._read_plan() or {}).get("training_calendar") or [],
            summaries=summaries,
            recovery=personal_recovery({}, as_of, canonical=recovery_inputs),
            checkins=self._read_checkins(),
            timezone=timezone,
        )

    def strength(self, weeks: int = STRENGTH_DEFAULT_WEEKS) -> dict[str, Any]:
        """Analyse › Kraft: how often per week the upper body and the legs were trained.

        The last ``weeks`` (4, 13, 26 or 52) calendar weeks including the current
        one, from completed strength sessions. A session counts for the upper body
        and for the legs by its executed sets, else its focus (a full body session
        for both); one that decides neither is counted as unknown, not guessed. No
        strength load; a week without a session counts 0.
        """
        if weeks not in STRENGTH_WEEKS:
            raise AppError(400, "Ungültiger Analysezeitraum.")
        today = self._today()
        start = today - timedelta(days=today.weekday(), weeks=weeks - 1)
        summaries, _ = activity_summaries_with_sync_time(self._canonical)
        rows = [
            row
            for row in summaries
            if row.get("sport") == Sport.STRENGTH
            and start.isoformat()
            <= iso_date_prefix(row.get("started_at_local"))
            <= today.isoformat()
        ]
        counts = {
            start + timedelta(weeks=week): {
                "sessions": 0,
                "upper": 0,
                "legs": 0,
                "unknown": 0,
            }
            for week in range(weeks)
        }
        for row in rows:
            day = date.fromisoformat(iso_date_prefix(row.get("started_at_local")))
            found = self._strength_sides(row) if self._strength_sides else None
            count = counts[day - timedelta(days=day.weekday())]
            count["sessions"] += 1
            upper, legs = found or (None, None)
            count["upper"] += upper is True
            count["legs"] += legs is True
            count["unknown"] += upper is not True and legs is not True
        return {
            "start": start.isoformat(),
            "end": today.isoformat(),
            "weeks": [
                {"week_start": week.isoformat(), **count}
                for week, count in counts.items()
            ],
        }

    def read(self, values: dict[str, Any], timezone: str = "UTC") -> dict[str, Any]:
        today = self._today()
        try:
            start = date.fromisoformat(
                str(
                    values.get("start")
                    or (today - timedelta(days=today.weekday())).isoformat()
                )
            )
            days = int(values.get("days", 7))
        except (TypeError, ValueError) as exc:
            raise AppError(400, "Ungültiger Berichtszeitraum.") from exc
        sport = report_sport(values.get("sport"))
        if (
            days not in {7, 28}
            or not date(2010, 1, 1) <= start <= today
            or sport is None
        ):
            raise AppError(400, "Ungültiger Berichtszeitraum oder Sportfilter.")
        # Only all-sport reports carry the local fitness, which needs the load days.
        summaries, observed_at, load_days = (
            activity_summaries_with_load_days(self._canonical)
            if sport == "all"
            else (*activity_summaries_with_sync_time(self._canonical), {})
        )
        # The smoothed load of an all-sport report needs the weeks before it.
        history = history_start(start, today) if sport == "all" else start
        period = [
            summary
            for summary in summaries
            if history.isoformat()
            <= summary_local_date(summary, timezone)
            <= min(today, start + timedelta(days=days - 1)).isoformat()
        ]
        return training_report(
            start=start,
            days=days,
            today=today,
            sport=sport,
            plan=self._read_plan(),
            checkins=self._read_checkins(),
            activity_feedback=self._feedback_of(period),
            timezone=timezone,
            activity_summaries=summaries,
            observed_at=observed_at,
            session_feedback=feedback_by_session(period, self._session_feedback),
            computed_at=None
            if self._now is None
            else self._now().isoformat(timespec="seconds"),
            local_load_days=load_days,
        )


class TrainingDerivedReadService:
    def __init__(
        self,
        *,
        read_checkins: Callable[[], Any],
        read_recovery: Callable[[], dict[str, Any]],
        read_performance: Callable[[], dict[str, Any]],
        today: Callable[[], date],
        canonical: CanonicalPerformanceSource,
    ) -> None:
        self._read_checkins = read_checkins
        self._read_recovery = read_recovery
        self._read_performance = read_performance
        self._today = today
        self._canonical = canonical

    def impact(self, timezone: str = "UTC") -> dict[str, Any]:
        return tag_impact(
            self._read_checkins(),
            self._read_recovery(),
            self._today(),
            timezone,
            activity_summaries=activity_summaries_for(self._canonical),
        )

    def body_history(self) -> dict[str, Any]:
        history = self._read_performance().get("history")
        body = history.get("body") if isinstance(history, dict) else None
        return body if isinstance(body, dict) else {"status": "insufficient_data"}

    def sleep_timing_variability(self) -> dict[str, Any]:
        regularity = self._read_recovery().get("regularity")
        if isinstance(regularity, dict):
            return regularity
        return {"status": "insufficient_data"}


class TrainingSeasonReadService:
    def __init__(
        self,
        *,
        read_competitions: Callable[[], list[dict[str, Any]]],
        read_observations: Callable[[], list[dict[str, Any]]],
        today: Callable[[], date],
        season_preparation: Callable[..., dict[str, Any]],
        season_source: SeasonSource,
    ) -> None:
        self._read_competitions = read_competitions
        self._read_observations = read_observations
        self._today = today
        self._season_preparation = season_preparation
        self._season_source = season_source

    def season(self, timezone: str = "UTC") -> dict[str, Any]:
        with self._season_source.database_manager().read_snapshot() as db:
            return self._season_preparation(
                self._read_competitions(),
                self._today(),
                timezone,
                self._read_observations(),
                activity_summaries=lambda start, end: read_activity_summaries(
                    self._season_source, db, start, end
                ),
            )


@dataclass(frozen=True)
class TrainingReportServices:
    records: TrainingRecordsService
    profiles: TrainingProfileSelectionService
    report: TrainingReportReadService
    derived: TrainingDerivedReadService
    season: TrainingSeasonReadService
    timezone: Callable[[], str]


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _window_start(today: date, days: int) -> date:
    """The Monday a progress window of ``days`` up to today starts on."""
    return _monday(today - timedelta(weeks=max(days // 7 - 1, 0)))


def _since_last_test(last_test: str | None, today: date) -> dict[str, Any]:
    """Window from the Monday of the last test's week, the shortest reaching today.

    Without a known test, or with one older than 84 days, the review covers the
    last 8 or 12 whole weeks instead.
    """
    if last_test is None:
        return {"days": 56}
    monday = date.fromisoformat(last_test) - timedelta(
        days=date.fromisoformat(last_test).weekday()
    )
    for days in (28, 56, 84):
        if monday + timedelta(days=days) > today:
            return {"start": monday.isoformat(), "days": days}
    return {"days": 84}


def summary_fields(summary: Mapping[str, Any]) -> dict[str, Any]:
    """Session fields every analysis row takes from its canonical summary."""
    sport = summary.get("sport")
    return {
        "date": summary_local_date(summary),
        "sport": sport,
        "activity_type": sport,
        "environment": "indoor" if summary_is_indoor(summary) else "outdoor",
        "name": summary.get("name"),
        "device": str(
            summary.get("device_name") or summary.get("recorded_by") or "unknown"
        )[:200],
    }


def _curve_sessions(
    canonical: CanonicalPerformanceSource, db: Any, start: date | None, today: date
) -> list[Any]:
    """Ride and run summaries from ``start`` (None: all) to today that can hold a best."""
    reader = canonical.activity_summary_reader()
    if reader is None:
        raise RuntimeError("power profiles need the canonical activity summaries")
    earliest = "" if start is None else start.isoformat()
    # One spare day on each side; the local date below decides membership.
    first = None if start is None else start - timedelta(days=1)
    last = today + timedelta(days=1)
    return [
        summary
        for summary in reader.activity_summaries(db, start=first, end=last)
        if summary.get("sport") in ("ride", "run")
        # Motor assistance inflates speed and power: e-bike rides stay
        # out of the best values, CP/CS fits and curve trends.
        and not summary_is_electric(summary)
        # A commute, warm-up or cool-down is never a maximal effort.
        and not summary_is_not_training(summary)
        and earliest <= summary_local_date(summary) <= today.isoformat()
    ]


def _with_decoupling(
    execution: Mapping[str, Any] | None, summary: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    """The execution's comparison numbers with the stored aerobic decoupling (M8).

    A continuous session's peers compare it, so the session report can judge
    decoupling against the athlete's own comparable sessions (#3084).
    """
    decoupling = summary.get("decoupling_pct")
    if not isinstance(execution, Mapping) or not isinstance(decoupling, (int, float)):
        return execution
    return {**execution, "decoupling_pct": decoupling}


def _curve_record(
    summary: Mapping[str, Any],
    curves: Mapping[str, SessionCurve],
    temperatures: Mapping[str, float],
) -> dict[str, Any]:
    """One session's comparison record; its temperature is its activity weather (#3036).

    ``temperatures`` is empty for the all-time records, which name none.
    """
    session_id = str(summary.get("id") or "")
    curve = curves.get(session_id)
    fields = summary_fields(summary)
    profile = None
    if curve is not None:
        profile = curve.power if fields["sport"] == "ride" else curve.running
    record = {
        **fields,
        "terrain": summary.get("terrain"),
        "activity_id": stored_activity_key(summary),
        "observed_at": summary.get("started_at"),
        # The session-type comparison names each session's duration (#4136).
        "duration_s": summary_duration_s(summary),
        # Best values are credited to the providers of the streams they read.
        "source": None if profile is None else profile.get("source"),
        "power_profile": {} if curve is None else curve.power,
        "running_profile": {} if curve is None else curve.running,
        "pace_at_hr_profile": {} if curve is None else curve.pace_at_hr,
        "power_at_hr_profile": {} if curve is None else curve.power_at_hr,
        "execution_summary": _with_decoupling(
            None if curve is None else curve.execution_summary, summary
        ),
        "running_dynamics": None if curve is None else curve.running_dynamics,
        "cs_test_efforts": getattr(curve, "cs_test_efforts", None),
        # The run averages the provider reports, for Garmin's colour bands (#2829).
        "dynamics_averages": {
            key: summary.get(key)
            for key in AVERAGE_KEYS.values()
            if summary.get(key) is not None
        }
        if curve is not None and curve.running_dynamics
        else {},
        "weather_temp_c": temperatures.get(session_id),
        "fingerprint": None if curve is None else curve.fingerprint,
    }
    if summary_is_race(summary):
        record["effort_context"] = "race"
    return record


def _all_time_record(
    summary: Mapping[str, Any], curves: Mapping[str, SessionCurve]
) -> dict[str, Any]:
    """An all-time record with its distance, moving time and elevation (#3826).

    No all-time reader (bests, running dynamics, LT1 history) names a
    temperature, and a weather row must not start a rebuild (#3036).
    """
    return {**_curve_record(summary, curves, {}), "totals": session_totals(summary)}


def _records_fingerprint(records: list[dict[str, Any]]) -> str:
    """Every input that decides a window: session identity, day, kind and curve."""
    return input_fingerprint(
        [
            row.get("activity_id"),
            row.get("date"),
            row.get("sport"),
            row.get("environment"),
            row.get("fingerprint"),
            row.get("effort_context"),
        ]
        for row in records
    )


def _effort_span(point: dict[str, Any]) -> tuple[float, float] | None:
    start = point.get("start_seconds")
    length = point.get("duration_seconds")
    if length is None:
        length = point.get("seconds")
    if point.get("status") != "ok" or start is None or length is None:
        return None
    return float(start), float(start) + float(length)


def _mark_effort_groups(points: list[dict[str, Any]]) -> None:
    """Give best values whose windows overlap in one session the same effort group.

    Overlapping windows of one effort are not independent observations, so a
    later model fit must not treat them as separate efforts.
    """
    marked = [
        (point, span) for point in points if (span := _effort_span(point)) is not None
    ]
    parent = list(range(len(marked)))

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for left, (point, span) in enumerate(marked):
        for right in range(left + 1, len(marked)):
            other, other_span = marked[right]
            if (
                point.get("activity_id") == other.get("activity_id")
                and span[0] < other_span[1]
                and other_span[0] < span[1]
            ):
                parent[root(right)] = root(left)
    groups: dict[int, int] = {}
    for index, (point, _span) in enumerate(marked):
        point["effort_group"] = groups.setdefault(root(index), len(groups) + 1)


def _power_candidates(
    eligible: list[dict[str, Any]], environment: str, duration: int
) -> list:
    return [
        (point, row)
        for row in eligible
        if row.get("sport") == "ride" and row.get("environment") == environment
        for point in (row.get("power_profile") or {}).get("duration_curve", [])
        if point.get("duration_seconds") == duration and point.get("watts") is not None
    ]


def _running_candidates(
    eligible: list[dict[str, Any]], key: str, field: str, value: float
) -> list:
    return [
        (point, row)
        for row in eligible
        if row.get("sport") == "run"
        for point in (row.get("running_profile") or {}).get(key, [])
        if point.get(field) is not None
        and point.get("duration_seconds", point.get("distance_meters")) == value
    ]

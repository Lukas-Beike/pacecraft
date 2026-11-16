"""Completed-activity analysis combining activity reads and performance context."""

import functools
from collections.abc import Callable, Mapping, Sequence
from datetime import date, timedelta
from typing import Any, Final
from zoneinfo import ZoneInfo

from backend.activities.activity_reference import ActivityReferenceResolver
from backend.activities.activity_summary import (
    TRAINING_EFFECT_KEYS,
    ActivitySummary,
)
from backend.activities.detail_contract import measured_order
from backend.activities.matching import match_planned_workouts
from backend.activities.read_service import ActivityReadService, ActivitySummarySource
from backend.activities.route_store import stored_route_block
from backend.activities.zone_settings import ZoneSettings
from backend.athlete.local_date import iso_date_prefix
from backend.athlete.profile import timezone_name
from backend.calendar.canonical import canonical_planned_workouts
from backend.canonical.activity_summary import legacy_activity_keys, stored_activity_key
from backend.canonical.local_time import local_date
from backend.canonical.records import ActivityRecord
from backend.canonical.vocabulary import MetricKind
from backend.errors import AppError
from backend.performance.canonical_detail import (
    CanonicalDetailReader,
    RefreshableProviders,
    SessionRecordReader,
)
from backend.performance.canonical_inputs import (
    EMPTY_PERFORMANCE_INPUTS,
    CanonicalPerformanceInputs,
    CanonicalPerformanceSource,
    read_performance_inputs,
    resolve_reader,
    with_read_status,
)
from backend.performance.context import current_performance_context
from backend.performance.current_thresholds import ZoneConfigurationSource
from backend.performance.detail_envelope import (
    DetailEnvelopeReader,
    feedback_session_load,
)
from backend.performance.freshness import GarminSyncStatus
from backend.performance.local_tss import THRESHOLD_KINDS
from backend.performance.planned_targets import planned_step_adherence, unit_steps
from backend.performance.session_rpe_load import feedback_by_session, session_load
from backend.performance.session_zones import (
    dated_thresholds,
    own_configurations,
    zone_settings_for,
)
from backend.weather.activity_weather_store import (
    stored_activity_weather,
    weather_block,
)


class ActivityAnalysisReadService(ActivityReadService):
    def __init__(
        self,
        database_manager: Any,
        summaries: ActivitySummarySource,
        feedback_service: Any,
        *,
        read_equipment: Callable[[str], dict[str, Any]] = lambda _: {},
        references: ActivityReferenceResolver | None = None,
        canonical: CanonicalPerformanceSource,
        session_records: SessionRecordReader | None = None,
        session_summaries: Callable[[], Sequence[ActivitySummary]] | None = None,
        refreshable: RefreshableProviders = frozenset,
        planned_units: Callable[[], Any] | None = None,
        unit_thresholds: Callable[[], Any] | None = None,
    ) -> None:
        super().__init__(
            database_manager,
            summaries,
            feedback_service,
            read_equipment=read_equipment,
            references=references,
            coverage=canonical.coverage,
        )
        self._canonical = canonical
        self._session_records = session_records
        self._session_summaries = session_summaries
        self._refreshable = refreshable
        self._planned_units = planned_units
        self._unit_thresholds = unit_thresholds

    def planned_adherence(self, document: Mapping[str, Any]) -> dict[str, Any] | None:
        """A run's or ride's steps against its local planned unit (#2830).

        The unit is the one the calendar pairs with the session on its day
        (``match_planned_workouts``), so "Details" of an extra session shows
        nothing. ``None`` also without an execution or a workout text; the
        Coach reads the laps' comparison instead.
        """
        session = document.get("session")
        analysis = document.get("analysis")
        execution = analysis.get("execution") if isinstance(analysis, Mapping) else None
        if (
            self._planned_units is None
            or self._unit_thresholds is None
            or not isinstance(session, Mapping)
            or session.get("sport") not in ("run", "ride")
            or not isinstance(execution, Mapping)
            or execution.get("status") != "ok"
        ):
            return None
        try:
            day = date.fromisoformat(iso_date_prefix(session.get("started_at_local")))
        except ValueError:
            return None
        # One day around, so a provider link to a neighbouring day's unit counts.
        first, last = day - timedelta(days=1), day + timedelta(days=1)
        units = self._planned_units().list_between(first, last)
        unit = paired_unit(
            units,
            self.summaries_between(first, last) or [],
            session.get("activity_id"),
        )
        if unit is None:
            return None
        steps = unit_steps(unit)
        if not isinstance(steps, list):
            return steps
        return planned_step_adherence(
            execution,
            steps,
            self._unit_thresholds().thresholds(unit),
            str(session.get("sport")),
            streams=document.get("streams"),
            planned_unit_id=str(unit.get("id")),
        )

    def _zone_settings_reader(
        self, profile: Mapping[str, Any]
    ) -> Callable[[Any, ActivityRecord], ZoneSettings]:
        return _session_zone_settings(
            self._canonical, ZoneInfo(timezone_name(profile.get("timezone")))
        )

    def _performance_inputs(
        self, as_of: date, activity_id: str | None = None
    ) -> tuple[
        CanonicalPerformanceInputs | None,
        Sequence[ActivitySummary] | None,
    ]:
        """Read the performance inputs and the activity summaries in one read snapshot.

        The performance inputs cover only the recent year, so an older activity
        is looked up in the full history of the same read snapshot.
        """
        source = self._canonical
        with source.database_manager().read_snapshot() as db:
            reader = source.activity_summary_reader()
            canonical = with_read_status(
                source,
                db,
                read_performance_inputs(
                    source.observation_reader(),
                    db,
                    activity_reader=reader,
                    as_of=as_of,
                    include_local_load=True,
                    preferences=source.preferences_in(db),
                ),
            )
            summaries = None if canonical is None else canonical.activities
            if reader is not None and (
                summaries is None
                or (
                    activity_id is not None
                    and _find_summary(summaries, activity_id) is None
                )
            ):
                summaries = tuple(reader.activity_summaries(db))
        return canonical, summaries

    def summaries_between(self, start: date, end: date) -> list[ActivitySummary] | None:
        """Canonical activity summaries from ``start`` to ``end``; ``None`` if unreadable."""
        source = self._canonical
        reader = source.activity_summary_reader()
        if reader is None:
            return None
        with source.database_manager().read_snapshot() as db:
            return list(reader.activity_summaries(db, start=start, end=end))

    def rated_sessions(self, start: date, end: date) -> list[dict[str, Any]]:
        """Each session from ``start`` to ``end`` with its local IF and effective RPE.

        For the expected RPE of the session report (#3084): one summary snapshot
        and one feedback read for the bounded window; sessions without a rating
        or an IF are left out.
        """
        summaries = self.summaries_between(start, end) or []
        feedback = feedback_by_session(
            summaries,
            functools.partial(
                self._feedback_service.for_activity_ids, sessions_only=True
            ),
        )
        rated = []
        for summary in summaries:
            entry, _value = session_load(
                summary, feedback.get(stored_activity_key(summary))
            )
            intensity = summary.get("intensity")
            if entry["rpe"] is None or not isinstance(intensity, (int, float)):
                continue
            rated.append(
                {
                    "id": summary.get("id"),
                    "sport": summary.get("sport"),
                    "intensity": intensity,
                    "rpe": entry["rpe"],
                    "rpe_scale": entry["rpe_scale"],
                }
            )
        return rated

    def session_headwind(self, session_id: str) -> dict[str, Any] | None:
        """The stored headwind of a session with its derivation (#3084).

        For the session report only: the bins describe the course of the
        session, so no detail document or tool result carries them.
        """
        metric = MetricKind.ACTIVITY_WEATHER_HEADWIND.value
        with self._database_manager.read_snapshot() as db:
            row = (
                stored_activity_weather(db, [session_id])
                .get(session_id, {})
                .get(metric)
            )
        if row is None:
            return None
        return {"mean_mps": row["value"], **row["derivation"]}

    def summaries_on_days(
        self, days: Sequence[date]
    ) -> dict[date, list[ActivitySummary]]:
        """Canonical activity summaries of each local day, read in one snapshot."""
        reader = self._canonical.activity_summary_reader()
        if reader is None:
            return {}
        with self._canonical.database_manager().read_snapshot() as db:
            return {
                day: list(reader.activity_summaries(db, start=day, end=day))
                for day in days
            }

    def session_rpe_load(self, activity_id: Any, today: date) -> dict[str, Any] | None:
        """The session-RPE load the detail document serves for one activity."""
        normalized_id = str(activity_id or "").strip()
        if not normalized_id or len(normalized_id) > 200:
            return None
        _canonical, summaries = self._performance_inputs(today, normalized_id)
        reference = str(self._references.resolve_reference(normalized_id).activity_id)
        return feedback_session_load(
            self._feedback_service,
            [reference, normalized_id],
            _find_summary(summaries, normalized_id),
        )

    def detail(
        self,
        activity_id: Any,
        *,
        garmin_status: GarminSyncStatus,
        profile: dict[str, Any],
        today: date,
        validation: bool = True,
    ) -> dict[str, Any]:
        """The detail document of one session.

        ``validation`` adds ``activity_validation`` against the current
        performance; the calendar's "Details" does not show it, and reading the
        performance inputs for it took most of a detail read (#3017).
        """
        normalized_id = str(activity_id or "").strip()
        if not normalized_id or len(normalized_id) > 200:
            raise AppError(
                400,
                "Die Aktivität konnte nicht eindeutig zugeordnet werden.",
                reason="invalid_activity_request",
            )
        if self._session_records is None:
            raise RuntimeError("activity detail reads need the session records")
        inputs: dict[str, Any] = {}

        def load_inputs(activity_id: str) -> Sequence[ActivitySummary] | None:
            # Recent canonical summaries first, then the full local history.
            canonical, summaries = self._performance_inputs(today, activity_id)
            inputs.update(canonical=canonical)
            return summaries

        summary_reader = self._canonical.activity_summary_reader()

        @functools.cache
        def requested_summary() -> ActivitySummary | None:
            # The validation reads the performance inputs anyway; they carry it.
            if self._session_summaries is not None and not validation:
                return _find_summary(self._session_summaries(), normalized_id)
            return _find_summary(load_inputs(normalized_id), normalized_id)

        def performance() -> Mapping[str, Any]:
            if not inputs:
                canonical, _rows = self._performance_inputs(today)
                inputs.update(canonical=canonical)
            return current_performance_context(
                garmin_status,
                profile,
                today,
                canonical=inputs["canonical"] or EMPTY_PERFORMANCE_INPUTS,
            )

        reader = CanonicalDetailReader(
            self._session_records,
            refreshable=self._refreshable,
            fingerprints=summary_reader,
            envelope=DetailEnvelopeReader(
                feedback_service=self._feedback_service,
                read_equipment=self._read_equipment,
                references=self._references,
                performance=performance if validation else None,
                summary=requested_summary,
            ),
            zone=ZoneInfo(timezone_name(profile.get("timezone"))),
            zone_settings=self._zone_settings_reader(profile),
        )
        with self._database_manager.read_snapshot() as db:
            document = reader.read(db, normalized_id)
            if document is None:
                raise AppError(
                    404,
                    "Diese Aktivität ist lokal nicht vorhanden.",
                    reason="activity_details_not_found",
                )
            session_id = document["session"]["activity_id"]
            records = self._session_records(db, session_id)
            weather = weather_block(
                stored_activity_weather(db, [session_id]).get(session_id, {})
            )
            route = stored_route_block(db, session_id)
        if weather is not None:
            document["weather"] = weather
        if route is not None:
            document["route"] = route
        summary = requested_summary()
        _add_purpose(document["session"], summary)
        document.update(_summary_blocks(summary))
        return {"ok": True, **document, "data_scope": _data_scope(document, records)}


def calendar_pairs(
    units: Sequence[Mapping[str, Any]], activities: Sequence[Mapping[str, Any]]
) -> dict[str, tuple[str, ...]]:
    """Each local planned unit the calendar pairs, with its session's keys.

    Archived and locally deleted units take part as little as in the calendar.
    The keys are the session's stored key and its legacy record ids.
    """
    live = [
        dict(unit)
        for unit in units
        if not unit.get("archived") and not unit.get("local_deleted")
    ]
    planned = canonical_planned_workouts(live, len(live))
    return {
        str(planned[index].get("local_id")): (
            stored_activity_key(row),
            *legacy_activity_keys(row),
        )
        for index, row in match_planned_workouts(planned, list(activities)).items()
    }


def paired_unit(
    units: Sequence[dict[str, Any]],
    activities: Sequence[Mapping[str, Any]],
    session_id: Any,
) -> dict[str, Any] | None:
    """The local planned unit the calendar pairs with the session, if one.

    ``units`` and ``activities`` cover the session's day and the days around
    it; the session details and the Coach (#2863) both pair this way.
    """
    pairs = calendar_pairs(units, activities)
    return next(
        (
            unit
            for unit in units
            if str(session_id) in pairs.get(str(unit.get("id")), ())
        ),
        None,
    )


def _add_purpose(session: dict[str, Any], summary: ActivitySummary | None) -> None:
    """Copy the session's purpose and competition link from its summary."""
    if summary is None:
        return
    if "purpose" in summary:
        session["purpose"] = summary["purpose"]
    if "competition_id" in summary:
        session["competition_id"] = summary["competition_id"]
    source = summary.get("sources", {}).get("purpose")
    if source:
        session["purpose_source"] = source


def _summary_blocks(summary: ActivitySummary | None) -> dict[str, Any]:
    """The detail blocks read from the session's summary, where present.

    The provider's training effect (#2831) and the pace zone times (#3038),
    whose ids name the athlete's pace zones; the zones carry no limits.
    """
    blocks: dict[str, Any] = {}
    effect = _provider_training_effect(summary)
    if effect is not None:
        blocks["training_effect"] = effect
    if summary is not None and summary.get("pace_zone_times"):
        blocks["pace_zone_times"] = {
            # A copy: the summary may be one of the kept, shared ones (#3043).
            "zones": [dict(zone) for zone in summary["pace_zone_times"]],
            "source": summary.get("sources", {}).get("pace_zone_times"),
        }
    return blocks


# The provider's own training-effect values of a session (#2831), as reported.
_TRAINING_EFFECT_KEYS: Final = (*TRAINING_EFFECT_KEYS, "training_effect_load")


def _provider_training_effect(
    summary: ActivitySummary | None,
) -> dict[str, Any] | None:
    """The provider's training-effect values with their source label, or None.

    All values come from one record of the summary, so they share its source;
    they are shown as the provider's values only, nothing is derived from them.
    """
    if summary is None:
        return None
    values = {
        key: summary[key]
        for key in _TRAINING_EFFECT_KEYS
        if summary.get(key) is not None
    }  # type: ignore[literal-required]
    if not values:
        return None
    sources = summary.get("sources", {})
    source = next(
        (sources[key] for key in _TRAINING_EFFECT_KEYS if sources.get(key)), None
    )
    return {**values, "source": source}


def _session_zone_settings(
    source: CanonicalPerformanceSource, zone: ZoneInfo
) -> Callable[[Any, ActivityRecord], ZoneSettings]:
    """The zones in effect for a session's sport, environment and local day.

    The resolver of the stored report zone times (``session_zones``, #1743), so
    the detail view counts the same seconds as the reports.
    """

    def read(db: Any, record: ActivityRecord) -> ZoneSettings:
        reader = resolve_reader(source.observation_reader(), db)
        configurations = own_configurations(
            reader.zone_configurations(db)
            if isinstance(reader, ZoneConfigurationSource)
            else ()
        )
        thresholds = dated_thresholds(
            [
                item
                for kind in THRESHOLD_KINDS
                for item in reader.observations(db, kind)
            ],
            configurations,
            source.preferences_in(db),
        )
        return zone_settings_for(
            record.sport,
            record.environment,
            local_date(record.started_at, zone),
            thresholds,
            configurations,
        )

    return read


def _find_summary(
    summaries: Sequence[ActivitySummary] | None, activity_id: str
) -> ActivitySummary | None:
    """Return the summary whose session id or any provider id is ``activity_id``."""
    return next(
        (
            summary
            for summary in summaries or ()
            if summary.get("id") == activity_id
            or activity_id in (summary.get("record_ids") or {}).values()
        ),
        None,
    )


def _data_scope(document: Mapping[str, Any], records: Sequence[ActivityRecord]) -> str:
    """Name the document's providers and the recorder of its primary record."""
    labels = ", ".join(
        str(entry["label"]) for entry in document["detail_data"]["providers"]
    )
    recorder = measured_order(records)[0].provenance.recorded_by.value
    return (
        "bounded sanitized activity_detail.v1 document of exactly one local "
        f"session; providers: {labels}; recorded by: {recorder}"
    )

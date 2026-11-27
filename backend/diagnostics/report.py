"""Privacy-safe projection of the application's diagnostic report."""

from __future__ import annotations

import functools
import logging
import platform
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from backend.athlete.profile import ProfileService, timezone_name
from backend.backup.automatic import FAILURE_KEY, AutomaticBackups
from backend.backup.database import backup_summary
from backend.backup.recovery_copies import RecoveryCopies
from backend.calendar.external import ExternalCalendarReader
from backend.canonical.garmin_payload import RETIRED_SNAPSHOT_KEY, STATE_KEY
from backend.canonical.vocabulary import ProviderId
from backend.coach.morning import MorningCheckinStateService
from backend.config import DEFAULT_ANTHROPIC_BASE_URL, DEFAULT_OPENAI_BASE_URL, Config
from backend.db import planning_store
from backend.db.activity_detail_store import dfa_series_counts
from backend.db.manager import DatabaseManager
from backend.db.repositories import KeyValueRepository
from backend.diagnostics.canonical_backfill import (
    CanonicalBackfillStatusReader,
    summarize_canonical_backfill,
)
from backend.diagnostics.canonical_coverage import summarize_canonical_coverage
from backend.diagnostics.canonical_read import (
    CanonicalReadStatusReader,
    summarize_canonical_read,
)
from backend.diagnostics.history import CoachDiagnosticHistoryService
from backend.diagnostics.logs import RecentLogEntriesService
from backend.diagnostics.plan_sync import (
    summarize_garmin_removal,
    summarize_garmin_strength_push,
    summarize_plan_sync,
)
from backend.diagnostics.provider_composition import summarize_provider_composition
from backend.diagnostics.provider_sync import summarize_provider_sync
from backend.diagnostics.session_conflicts import summarize_session_conflicts
from backend.diagnostics.stream_backfill import summarize_stream_backfill
from backend.diagnostics.wahoo_state import summarize_wahoo
from backend.observability import (
    DIAGNOSTIC_CAPTURE_ENTRIES_KEY,
    DiagnosticCapture,
    Redactor,
)
from backend.planning.library_service import archive_purge_counts
from backend.provider_credentials import garmin_tokens_connected
from backend.providers.garmin import GarminClientFactory
from backend.providers.state import ProviderStateService
from backend.coach.auto_turns import diagnostics as automatic_turn_diagnostics
from backend.push.service import diagnostics as push_diagnostics
from backend.runtime.memory import process_memory, process_settings
from backend.runtime.ports import ProviderCoverageReader
from backend.settings import SettingsService
from backend.sync.activity_routes import summarize_activity_routes
from backend.sync.activity_weather import summarize_activity_weather
from backend.sync.competitions import summarize_competition_sync
from backend.sync.external_calendar import ExternalCalendarSyncService
from backend.sync.freshness import ProviderFreshnessService
from backend.sync.garmin import (
    GarminFixtureLoader,
    GarminSyncStateService,
    summarize_history_refetch,
)
from backend.sync.garmin_activity_details import (
    detail_fallback_counts,
    detail_replay_size,
)
from backend.sync.garmin_body_push import summarize_body_push
from backend.sync.garmin_projection_service import GarminProjectionService
from backend.sync.garmin_sleep_keys import (
    summarize_sleep_temperature_keys,
)
from backend.sync.garmin_threshold_store import summarize_garmin_threshold_store
from backend.sync.garmin_wellness_history import summarize_daily_calories
from backend.sync.jobs import utc_timestamp
from backend.sync.library import WorkoutLibrarySyncStateService
from backend.sync.local_load import summarize_local_load
from backend.sync.profile_weekly_plan_removal import (
    summarize_profile_weekly_plan_removal,
)
from backend.sync.provider_table import CALENDAR_ID
from backend.sync.session_analysis_stage import summarize_session_analysis
from backend.sync.session_focus_backfill import summarize_session_focus_backfill
from backend.sync.session_metrics import summarize_session_metrics
from backend.sync.session_rebundle import summarize_session_rebundle
from backend.sync.state import last_error_key, last_sync_key
from backend.sync.stream_backfill import fit_inventory_counts
from backend.sync.wahoo_stream_backfill import wahoo_replay_size
from backend.sync.wahoo_weight_push import summarize_weight_push

LOGGER = logging.getLogger("intervals_coach")
# Plan push counts per provider cover this many days (#2878).
PUSH_WINDOW_DAYS = 7
# An open plan push job older than this counts as stuck (#2953).
STUCK_AFTER_HOURS = 2


@dataclass(frozen=True)
class DiagnosticReportDependencies:
    """Concrete application owners required to compose a diagnostic report."""

    database_manager: DatabaseManager
    key_values: KeyValueRepository
    config: Config
    settings: SettingsService
    app_name: str
    app_version: str
    utc_now: Callable[[], str]
    garmin_projection: GarminProjectionService
    garmin_client_factory: GarminClientFactory
    garmin_fixture_loader: GarminFixtureLoader
    provider_state: ProviderStateService
    coach_history: CoachDiagnosticHistoryService
    redactor: Redactor
    provider_freshness: ProviderFreshnessService
    profile: ProfileService
    garmin_sync_state: GarminSyncStateService
    external_calendar_sync: ExternalCalendarSyncService
    external_calendar_reader: ExternalCalendarReader
    morning_checkin: MorningCheckinStateService
    workout_library_sync_state: WorkoutLibrarySyncStateService
    canonical_coverage: ProviderCoverageReader
    recent_logs: RecentLogEntriesService
    diagnostic_capture: DiagnosticCapture
    canonical_backfill: CanonicalBackfillStatusReader | None = None
    canonical_read: CanonicalReadStatusReader | None = None
    # The Garmin change check's counts and times (#2372).
    garmin_change_check: Callable[[], dict[str, Any]] | None = None
    # Rides with a steady power band per variation limit, counts only (#2535).
    lt1_ride_coverage: Callable[[], dict[str, Any]] | None = None
    # The slowest API routes by pattern since start (#3017).
    slowest_routes: Callable[[], list[dict[str, Any]]] | None = None
    session_reports: Callable[[], dict[str, Any]] | None = None
    # Waits of the heavy view reads for their gate, counts only (#3029).
    heavy_read_gate: Callable[[], dict[str, Any]] | None = None
    # The adaptive expenditure's basis in the read snapshot, counts only (#3100).
    energy_balance: Callable[[Any], dict[str, Any]] | None = None
    # Why closed days lack a Verlauf expenditure, counts only (#3157).
    expenditure_gaps: Callable[[Any], dict[str, Any]] | None = None
    # The all-time curve build: state word, count and last duration (#2852).
    curve_build: Callable[[], dict[str, Any]] | None = None
    # The database file: its sizes, free space and recovery copies (#2848).
    database_path: Callable[[], Path] | None = None
    # The automatic database copies, None when switched off (#3760).
    automatic_backups: Callable[[], AutomaticBackups | None] | None = None


# Large kv values whose stored size the report shows: the Garmin state, the
# retired Garmin snapshot a migrated database keeps, and the diagnostic capture.
_PAYLOAD_KEYS = (STATE_KEY, RETIRED_SNAPSHOT_KEY, DIAGNOSTIC_CAPTURE_ENTRIES_KEY)


class DiagnosticReportService:
    """Orchestrate existing local projections into the diagnostics API shape."""

    def __init__(self, dependencies: DiagnosticReportDependencies) -> None:
        self._deps = dependencies

    def _get_value(self, db: Any, key: str) -> str | None:
        return self._deps.key_values.get(db, key)

    def _database_counts(self, db: Any) -> dict[str, int]:
        def count(table: str) -> int:
            return db.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()[
                "count"
            ]

        counts: dict[str, Any] = {
            "messages": count("messages"),
            "workout_library": planning_store.library_count(db),
            "competitions": count("competitions"),
            "athlete_checkins": count("athlete_checkins"),
            "activity_feedback": count("activity_feedback"),
            "change_history": count("change_history"),
            # Diary entries, so a release shows the diary kept its rows (#3234).
            "nutrition_logs": count("nutrition_logs"),
            # Weights the athlete entered by hand that reads serve (#3252).
            "entered_weights": count(
                "observations WHERE metric = 'weight' AND provider IS NULL "
                "AND source_kind = 'athlete_entered' AND state = 'active'"
            ),
            # Body fats the athlete entered as a percentage (#3402).
            "entered_body_fats": count(
                "observations WHERE metric = 'body_fat' AND provider IS NULL "
                "AND source_kind = 'athlete_entered' AND state = 'active'"
            ),
            # Adopted local zones carrying a maximum: an adoption before #4161
            # copied the watch's, which then froze the max HR. Counted, never read.
            "adopted_zone_maximums": count(
                "zone_configurations WHERE provider IS NULL AND state = 'active' "
                "AND maximum IS NOT NULL "
                "AND json_extract(details, '$.threshold_origin') IS NOT NULL"
            ),
        }
        counts["stored_payload_bytes"] = self._stored_payload_bytes(db)
        return counts | self._storage()

    def _storage(self) -> dict[str, int | None]:
        """Database and write-ahead log sizes and free space beside them (stat only).

        Unknown (``None``) without a database path, so a reader sees the gap.
        """
        if self._deps.database_path is None:
            return dict.fromkeys(("file_bytes", "wal_bytes", "data_dir_free_bytes"))
        path = self._deps.database_path()

        def size(file: Path) -> int | None:
            try:
                return int(file.stat().st_size)
            except OSError:
                return None

        try:
            free: int | None = shutil.disk_usage(path.parent).free
        except OSError:
            free = None
        return {
            "file_bytes": size(path),
            "wal_bytes": size(path.with_name(path.name + "-wal")),
            "data_dir_free_bytes": free,
        }

    @staticmethod
    def _stored_payload_bytes(db: Any) -> dict[str, int]:
        # octet_length uses stored byte lengths without fetching the payloads.
        rows = db.execute(
            "SELECT key, octet_length(value) AS bytes FROM kv "
            f"WHERE key IN ({', '.join('?' for _ in _PAYLOAD_KEYS)})",
            _PAYLOAD_KEYS,
        ).fetchall()
        stored = {row["key"]: row["bytes"] for row in rows}
        return {key: stored.get(key, 0) for key in _PAYLOAD_KEYS}

    def _local_today(self, profile: dict[str, Any]) -> str:
        """The athlete's local day, as the Garmin past-workout removal uses it."""
        zone = ZoneInfo(timezone_name(profile.get("timezone")))
        return (
            datetime.fromisoformat(self._deps.utc_now())
            .astimezone(zone)
            .date()
            .isoformat()
        )

    def _push_window(self) -> tuple[str, str]:
        """The start of the plan push count window (#2878) and the stuck limit (#2953)."""
        now = datetime.fromisoformat(self._deps.utc_now())
        return (
            utc_timestamp(now - timedelta(days=PUSH_WINDOW_DAYS)),
            utc_timestamp(now - timedelta(hours=STUCK_AFTER_HOURS)),
        )

    def _backup(self, db: Any, now: datetime) -> dict[str, Any]:
        """The newest downloaded backup and the automatic copies (#3760)."""
        automatic = (
            self._deps.automatic_backups() if self._deps.automatic_backups else None
        )
        return {
            **backup_summary(self._deps.key_values, db, now),
            "automatic": {"enabled": False}
            if automatic is None
            else automatic.summary(now, self._deps.key_values.get(db, FAILURE_KEY)),
        }

    def _recovery_copies(self) -> dict[str, Any]:
        if self._deps.database_path is None:
            return {"available": False}
        path = self._deps.database_path()
        return RecoveryCopies(path.parent, path.name).summary()

    def clear(self) -> dict[str, Any]:
        """Clear the captured technical diagnostic entries."""
        return self._deps.diagnostic_capture.clear()

    @staticmethod
    def _section(name: str, build: Callable[[], Any]) -> Any:
        """Build one report section; a failing one must not break the endpoint."""
        try:
            return build()
        except Exception as exc:  # noqa: BLE001 - one bad row must not hide the report.
            LOGGER.warning(
                "Diagnostics section unavailable",
                extra={
                    "event": "diagnostics_section_unavailable",
                    "context": {"section": name, "error_type": type(exc).__name__},
                },
            )
            return {"status": "unavailable"}

    @staticmethod
    def _schema_version(db: Any) -> int:
        return int(db.execute("PRAGMA user_version").fetchone()["user_version"])

    def report(self) -> dict[str, Any]:
        deps = self._deps
        section = self._section
        # Read once per report, by the sections that need it.
        athlete_profile = functools.cache(deps.profile.get)
        diagnostic_capture_entries = deps.diagnostic_capture.entries()
        diagnostic_capture_status = deps.diagnostic_capture.status()
        # Reading the rotated log files must not hold the WAL snapshot open.
        log_entries = deps.recent_logs.report_entries()
        # The report only reads, so it takes one WAL snapshot instead of the writer.
        with deps.database_manager.read_snapshot() as db:
            garmin_status = section(
                ProviderId.GARMIN.value, deps.garmin_projection.public_state
            )

            calendar_events = section(
                "calendar_events",
                lambda: deps.external_calendar_reader.event_counts(db),
            )

            def configuration() -> dict[str, Any]:
                return {
                    "openai_configured": bool(deps.config.openai_api_key),
                    # Only whether the endpoint differs from the default, never the URL.
                    "openai_base_url_custom": deps.config.openai_base_url
                    != DEFAULT_OPENAI_BASE_URL,
                    "anthropic_configured": bool(deps.config.anthropic_api_key),
                    "anthropic_base_url_custom": deps.config.anthropic_base_url
                    != DEFAULT_ANTHROPIC_BASE_URL,
                    "intervals_configured": bool(deps.config.intervals_api_key),
                    "garmin_library_available": deps.garmin_client_factory.available(),
                    "garmin_configured": garmin_status.get("configured"),
                    "garmin_fixture_configured": deps.garmin_fixture_loader.path()
                    is not None,
                    "model": deps.settings.selected_model(),
                    "model_provider": deps.settings.selected_provider(),
                    "thinking_level": deps.settings.selected_thinking_level(),
                    "tasks": deps.settings.task_settings(),
                    "available_models": [
                        option["id"]
                        for option in deps.settings.available_model_options()
                    ],
                }

            def sync() -> dict[str, Any]:
                return {
                    "last_error": deps.redactor.redact_text(
                        self._get_value(db, "last_sync_error") or ""
                    )
                    or None,
                    "providers": summarize_provider_sync(db),
                }

            def provider_freshness() -> Any:
                return deps.provider_freshness.current(
                    profile=athlete_profile(),
                    garmin_has_core_error=bool(
                        deps.garmin_sync_state.core_error_entries()
                    ),
                    garmin_tokenstore_exists=garmin_tokens_connected(
                        lambda key: deps.key_values.get(db, key),
                        deps.config.garmin_tokenstore,
                    ),
                    read_only=True,
                )

            def external_calendar() -> dict[str, Any]:
                return {
                    "configured": bool(deps.config.calendar_ical_url),
                    "last_sync_at": self._get_value(db, last_sync_key(CALENDAR_ID)),
                    "last_error": deps.redactor.redact_text(
                        self._get_value(db, last_error_key(CALENDAR_ID)) or ""
                    )
                    or None,
                    "running": deps.external_calendar_sync.running(),
                    "events": calendar_events.get("upcoming"),
                }

            def database() -> dict[str, Any]:
                return {
                    "schema_version": self._schema_version(db),
                    **self._database_counts(db),
                    "workout_library_state": deps.workout_library_sync_state.summary(),
                    "workout_library_archive": archive_purge_counts(
                        db, datetime.fromisoformat(deps.utc_now())
                    ),
                    "external_calendar_events": calendar_events.get("total"),
                }

            key_values = deps.key_values
            return {
                "generated_at": deps.utc_now(),
                "app": {"name": deps.app_name, "version": deps.app_version},
                "runtime": {
                    "python": platform.python_version(),
                    "platform": platform.platform(),
                    "process_memory": process_memory(),
                    **process_settings(),
                    "reader_slots": deps.database_manager.reader_slot_waits(),
                    "heavy_read_gate": deps.heavy_read_gate()
                    if deps.heavy_read_gate
                    else {},
                    # Route patterns only: no ids, no query (#3017).
                    "slowest_routes": deps.slowest_routes()
                    if deps.slowest_routes
                    else [],
                    # Session reports and reference resolutions, counts only (#3084).
                    "session_reports": deps.session_reports()
                    if deps.session_reports
                    else {},
                },
                "recovery_copies": section("recovery_copies", self._recovery_copies),
                # The newest backup the app made, before a schema release (#2996).
                "backup": section(
                    "backup",
                    lambda: self._backup(db, datetime.fromisoformat(deps.utc_now())),
                ),
                "configuration": section("configuration", configuration),
                "openai": section(
                    "openai", lambda: deps.provider_state.summary("openai")
                ),
                "coach_commands": section("coach_commands", deps.coach_history.history),
                "sync": section("sync", sync),
                ProviderId.GARMIN.value: garmin_status,
                "provider_freshness": section("provider_freshness", provider_freshness),
                "external_calendar": section("external_calendar", external_calendar),
                "morning_checkin": section(
                    "morning_checkin", deps.morning_checkin.state
                ),
                "database": section("database", database),
                "energy_balance": section(
                    "energy_balance",
                    lambda: deps.energy_balance(db) if deps.energy_balance else {},
                ),
                ProviderId.WAHOO.value: section(
                    ProviderId.WAHOO.value,
                    lambda: summarize_wahoo(
                        key_values,
                        db,
                        deps.config,
                        deps.redactor.redact_text,
                        datetime.fromisoformat(deps.utc_now()),
                    ),
                ),
                # Web Push, counts only: never keys or endpoints (#3914).
                "push_notifications": section(
                    "push_notifications",
                    lambda: push_diagnostics(key_values, db),
                ),
                # Automatic Coach turns, counts only (#4274).
                "coach_automatic_turns": section(
                    "coach_automatic_turns",
                    lambda: automatic_turn_diagnostics(key_values, db),
                ),
                "plan_sync": section(
                    "plan_sync",
                    lambda: summarize_plan_sync(db, *self._push_window()),
                ),
                "garmin_past_workout_removal": section(
                    "garmin_past_workout_removal",
                    lambda: summarize_garmin_removal(db),
                ),
                # Past Garmin days without daily calories, counts only (#3157).
                "garmin_daily_calories": section(
                    "garmin_daily_calories",
                    lambda: summarize_daily_calories(
                        db, date.fromisoformat(self._local_today(athlete_profile()))
                    ),
                ),
                # Skin-temperature key names of the sleep answers, no values (#4017).
                "garmin_sleep_temperature_keys": section(
                    "garmin_sleep_temperature_keys",
                    lambda: summarize_sleep_temperature_keys(
                        key_values,
                        db,
                        date.fromisoformat(self._local_today(athlete_profile())),
                    ),
                ),
                "nutrition_expenditure_gaps": section(
                    "nutrition_expenditure_gaps",
                    lambda: deps.expenditure_gaps(db) if deps.expenditure_gaps else {},
                ),
                # The entered body days mirrored to Garmin, counts only (#3403).
                "garmin_body_push": section(
                    "garmin_body_push", lambda: summarize_body_push(db)
                ),
                # The one-off Garmin history re-reads, counts only (#3729).
                "garmin_history_refetch": section(
                    "garmin_history_refetch", lambda: summarize_history_refetch(db)
                ),
                # The competition sync's failed step and kind, no text (#3838).
                "competition_sync": section(
                    "competition_sync", lambda: summarize_competition_sync(db)
                ),
                # The Wahoo profile-weight write, counts only (#3559).
                "wahoo_weight_push": section(
                    "wahoo_weight_push", lambda: summarize_weight_push(db)
                ),
                "garmin_strength_push": section(
                    "garmin_strength_push",
                    lambda: summarize_garmin_strength_push(
                        db, self._local_today(athlete_profile())
                    ),
                ),
                "canonical_coverage": summarize_canonical_coverage(
                    deps.canonical_coverage, db
                ),
                "canonical_backfill": summarize_canonical_backfill(
                    deps.canonical_backfill, db
                ),
                "canonical_read": summarize_canonical_read(deps.canonical_read, db),
                "canonical_session_conflicts": section(
                    "canonical_session_conflicts",
                    lambda: summarize_session_conflicts(db),
                ),
                "canonical_provider_composition": section(
                    "canonical_provider_composition",
                    lambda: summarize_provider_composition(db),
                ),
                "local_load": section(
                    "local_load", lambda: summarize_local_load(key_values, db)
                ),
                "garmin_change_check": section(
                    "garmin_change_check",
                    deps.garmin_change_check or (lambda: {"active": False}),
                ),
                "lt1_ride_coverage": section(
                    "lt1_ride_coverage",
                    deps.lt1_ride_coverage or (lambda: {"available": False}),
                ),
                "curve_build": section(
                    "curve_build", deps.curve_build or (lambda: {"available": False})
                ),
                "stream_backfill": section(
                    "stream_backfill",
                    lambda: summarize_stream_backfill(
                        key_values, db, deps.config.sync_history_earliest_date
                    ),
                ),
                "fit_inventory": section(
                    "fit_inventory", lambda: fit_inventory_counts(db, key_values)
                ),
                # Chest-strap runs of 60 days by DFA-a1 method, counts only (#3273).
                "dfa_series": section(
                    "dfa_series",
                    lambda: dfa_series_counts(
                        db,
                        since=date.fromisoformat(self._local_today(athlete_profile()))
                        - timedelta(days=59),
                    ),
                ),
                "garmin_detail_fallback": section(
                    "garmin_detail_fallback",
                    lambda: detail_fallback_counts(db, key_values),
                ),
                # What the next detail mapper bump would download again (#2220).
                "mapper_replay": section(
                    "mapper_replay",
                    lambda: {
                        ProviderId.GARMIN.value: detail_replay_size(
                            db, key_values, datetime.fromisoformat(deps.utc_now())
                        ),
                        ProviderId.WAHOO.value: wahoo_replay_size(
                            db,
                            date.fromisoformat(deps.utc_now()[:10]),
                            deps.config.sync_history_earliest_date,
                        ),
                    },
                ),
                "session_metrics": section(
                    "session_metrics",
                    lambda: summarize_session_metrics(key_values, db),
                ),
                "activity_weather": section(
                    "activity_weather",
                    lambda: summarize_activity_weather(key_values, db),
                ),
                "activity_routes": section(
                    "activity_routes",
                    lambda: summarize_activity_routes(key_values, db),
                ),
                "session_analysis": section(
                    "session_analysis",
                    lambda: summarize_session_analysis(key_values, db),
                ),
                "session_focus_backfill": section(
                    "session_focus_backfill",
                    lambda: summarize_session_focus_backfill(key_values, db),
                ),
                # The one-time Garmin LT2 store, counts only (#4135).
                "garmin_threshold_store": section(
                    "garmin_threshold_store",
                    lambda: summarize_garmin_threshold_store(key_values, db),
                ),
                "profile_weekly_plan_removal": section(
                    "profile_weekly_plan_removal",
                    lambda: summarize_profile_weekly_plan_removal(key_values, db),
                ),
                "session_rebundle": section(
                    "session_rebundle",
                    lambda: summarize_session_rebundle(key_values, db),
                ),
                "logs": log_entries,
                "debug_capture": {
                    **diagnostic_capture_status,
                    "entries": diagnostic_capture_entries,
                },
                "note": (
                    "Die Diagnose enthält technische Anfrageformen, Provider-Metadaten und "
                    "Fehlerdetails. Anfragen an Garmin, Wahoo, Intervals.icu und weitere "
                    "Dienste sowie alle Antworten werden nur als Form gespeichert "
                    "(Feldnamen, Typen, Längen), nie mit Werten. Anfragen an OpenAI können "
                    "Dialog- und Trainingskontext des Coaches enthalten. Zugangsdaten, "
                    "Schlüssel, Tokens, Sitzungsdaten und OpenAI-Fehlertexte werden vor dem "
                    "Speichern entfernt. Binäre Inhalte werden ausgelassen; Einträge älter "
                    "als 7 Tage entfallen; Größenlimits und Kürzungen sind ausgewiesen."
                ),
            }

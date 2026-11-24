"""Read-only Coach tools for recent activities and activity details."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Final

from backend.activities.hr_cap import stored_cap
from backend.activities.session_purpose import summary_is_not_training
from backend.activities.strength_sets import plan_comparison, trained_structure
from backend.activities.zone_settings import LT1_HR_SHARE
from backend.athlete.local_date import iso_date_prefix
from backend.athlete.profile import ProfileService
from backend.canonical.activity_summary import (
    legacy_activity_keys,
    stored_activity_key,
    summary_duration_s,
    summary_is_electric,
)
from backend.coach.context import (
    bounded_coach_context_value,
    coach_context_json_size,
    coach_sleep_timing_projection,
)
from backend.coach.session_checks import matched_focus
from backend.errors import AppError
from backend.nutrition import fuelling, session_fuelling
from backend.performance import session_conditions
from backend.performance.activity_read_service import (
    ActivityAnalysisReadService,
    calendar_pairs,
)
from backend.performance.comparable_sessions import COMPARE_DAYS as COHORT_DAYS
from backend.performance.comparable_sessions import (
    comparable_sessions,
    same_type_keys,
    session_types,
)
from backend.performance.critical_power import (
    METHOD_ID as CRITICAL_SPEED_METHOD,
)
from backend.performance.critical_power import (
    WINDOW_DAYS as CRITICAL_SPEED_DAYS,
)
from backend.performance.critical_power import critical_power_speed
from backend.performance.pace_at_hr import COMPARE_DAYS, session_comparison
from backend.performance.planned_targets import (
    heart_rate_above,
    heart_rate_cap_execution,
    planned_heart_rate_floor,
    planned_step_adherence,
    planned_target_execution,
    unit_steps,
)
from backend.performance.report_service import PROFILE_WINDOW_DAYS
from backend.performance.session_conditions import INPUTS as CONDITION_INPUTS
from backend.performance.session_conditions import RATED_WINDOW_DAYS
from backend.performance.session_execution import INDOOR_ENVIRONMENTS
from backend.performance.session_report import CLIMB_CADENCE_RPM, session_report
from backend.planning import leg_strength
from backend.planning.race_plan import race_plan
from backend.planning.sport import planned_sport_environment
from backend.sync.garmin import GarminPayloadService
from backend.weather.activity_weather import activity_window, weather_pending

# Per-field source labels, zone-time arrays and executed strength sets stay in
# get_activity_details; the list keeps each session's scalar values and its
# primary source label.
_LIST_OMITTED_KEYS: Final = frozenset(
    {
        "sources",
        "power_zone_times",
        "heart_rate_zone_times",
        "pace_zone_times",
        "power_band_times",
        "heart_rate_band_times",
        "strength_sets",
        # Garmin's sweat-loss estimate serves the fuelling advice only (#2804).
        "water_estimated_ml",
    }
)
# A reference's start time matches a session starting this close to it (#3084).
START_TIME_TOLERANCE_MIN: Final = 30
# The route visits the Coach reads with a session: it and the ones before it (#3763).
ROUTE_HISTORY_VISITS: Final = 6
# Without a date the newest session of this many days is analysed.
NEWEST_SESSION_DAYS: Final = 30
# Sessions offered back when a reference is not unique.
MAX_CANDIDATES: Final = 10
# The session the athlete's own request names with the analyse button's prompt
# ("Analysiere meine Einheit vom <day> (…, ID <id>)") is read when the model
# names none, and a call for another ID says so (#3080). The planned-unit
# discussion button writes a unit's ID in the same bracket, so only the analyse
# prompt counts. The tool execution sets this argument from that request; a
# model argument of that name is replaced.
NAMED_ACTIVITY_ID: Final = "_named_activity_id"
_NAMED_ID: Final = re.compile(
    r"Analysiere meine Einheit vom [^()\n]*\([^()\n]*\bID ([\w:.-]{1,80})\)"
)


def named_activity_id(text: str) -> str | None:
    """The session ID the analyse button wrote into the request; the last one,
    so the current message wins over the messages a clarification answers."""
    found = _NAMED_ID.findall(text)
    return found[-1] if found else None


def _not_named_notice(named: str) -> str:
    return (
        f"The athlete's message names session ID {named}, and this call read "
        "another ID. Analyse the named session unless session_bundle shows both "
        "IDs are the same session."
    )


# Candidates never carry the stored name: it is provider or athlete text.
_CANDIDATE_KEYS: Final = (
    "id",
    "started_at_local",
    "sport",
    "moving_duration_s",
    "distance_m",
)


def resolve_session(
    summaries: Sequence[Mapping[str, Any]],
    *,
    sport: str | None,
    start_time: str | None,
    newest: bool,
) -> tuple[str, list[Mapping[str, Any]]]:
    """The session a reference points to, or why not, with the matches.

    Sessions match by sport and by a start within ``START_TIME_TOLERANCE_MIN``
    of ``start_time`` (HH:MM). ``newest`` takes the latest match. Otherwise one
    match is ``resolved``, several are ``ambiguous``, and none is
    ``not_found`` with the sessions of the named sport, else every given
    session, as candidates. The newest skips commutes, warm-ups and cool-downs
    unless only those match. Pure, for reuse by #3080.
    """
    of_sport = [row for row in summaries if not sport or row.get("sport") == sport]
    matches = of_sport
    wanted = _clock_minutes(start_time)
    if wanted is not None:
        matches = [
            row
            for row in matches
            if (start := _clock_minutes(str(row.get("started_at_local") or "")[11:16]))
            is not None
            and abs(start - wanted) <= START_TIME_TOLERANCE_MIN
        ]
    if newest and matches:
        training = [row for row in matches if not summary_is_not_training(row)]
        latest = max(
            training or matches, key=lambda row: str(row.get("started_at_local") or "")
        )
        return "resolved", [latest]
    if len(matches) == 1:
        return "resolved", matches
    if matches:
        return "ambiguous", matches
    return "not_found", of_sport or list(summaries)


def _critical_speed(
    records: Sequence[Mapping[str, Any]] | None, day: date, covered_from: date
) -> dict[str, Any] | None:
    """The outdoor running CS of the 28 days before the session's day (card M4).

    None when the records do not cover that whole window: a fit on part of it
    would carry the 28-day label without its data.
    """
    # The 28 days before the session, so its own or a later effort that day
    # never fits the CS it is compared with.
    last = day - timedelta(days=1)
    if records is None or last - timedelta(days=CRITICAL_SPEED_DAYS - 1) < covered_from:
        return None
    first = (last - timedelta(days=CRITICAL_SPEED_DAYS - 1)).isoformat()
    window = [
        dict(row)
        for row in records
        if first <= str(row.get("date") or "") <= last.isoformat()
    ]
    fits = critical_power_speed(window, last, input_fingerprint="")["fits"]
    fit = next(
        (item for item in fits if item.get("sport") == "run"),
        {"status": "insufficient_data"},
    )
    return {
        "method": CRITICAL_SPEED_METHOD,
        "classification": "model estimate",
        "status": fit.get("status"),
        "cs_mps": fit.get("cs_mps"),
        "window_days": CRITICAL_SPEED_DAYS,
        "reason": fit.get("reason"),
    }


_CADENCE_TARGET = re.compile(r"(\d{2,3})\s*(?:rpm|u/min)", re.IGNORECASE)


def _plans_low_cadence(description: Any) -> bool:
    """True when the unit's text sets a cadence below the climb limit (#3084)."""
    return any(
        int(value) < CLIMB_CADENCE_RPM
        for value in _CADENCE_TARGET.findall(str(description or ""))
    )


def _plan_zones(adherence: Any) -> dict[str, Any] | None:
    """The paired unit's zone times from its step adherence, or why there are none."""
    if not isinstance(adherence, Mapping):
        return None
    zones = adherence.get("zones")
    if isinstance(zones, Mapping):
        return dict(zones)
    return {
        "status": "not_covered",
        "reason": adherence.get("reason") or "Keine Zonenzeiten im Plan.",
    }


def _session_date(session: Mapping[str, Any]) -> date | None:
    """The local day a session started, None when unknown."""
    try:
        return date.fromisoformat(iso_date_prefix(session.get("started_at_local")))
    except ValueError:
        return None


def _clock_minutes(value: Any) -> int | None:
    """Minutes after midnight of an HH:MM time, None when it is none."""
    try:
        clock = time.fromisoformat(str(value))
    except ValueError:
        return None
    return clock.hour * 60 + clock.minute


VIRTUAL_SPEED_NOTE = "Geschwindigkeit virtuell, kein Leistungsmaß; weggelassen."
POWER_SPEED_NOTE = "Fahrt mit Leistung: es zählen Leistung und Puls; die Geschwindigkeit ist weggelassen."
RAW_SPEED_NOTE = (
    "Fahrt ohne Leistung: zuerst zählt der Puls; die Geschwindigkeit ist ohne "
    "Steigung und Wind kein Leistungsmaß."
)


def _without_virtual_speed(detail: dict[str, Any]) -> None:
    """The athlete's signal order for a ride in the Coach's copy (#3288).

    Indoor or virtual: speed follows Zwift's terrain or the trainer, never an
    effort, so every speed goes. With power: power and heart rate count, every
    speed goes; distance and duration stay. Outdoor without power: the speed
    stays as data, its note says it is no effort measure unadjusted. The
    calendar keeps its own copy.
    """
    session = detail.get("session")
    if not (isinstance(session, Mapping) and session.get("sport") == "ride"):
        return
    if session.get("environment") in INDOOR_ENVIRONMENTS:
        note = VIRTUAL_SPEED_NOTE
    elif _has_power(detail):
        note = POWER_SPEED_NOTE
    else:
        detail["speed_note"] = RAW_SPEED_NOTE
        return
    for part in _speed_parts(detail):
        if isinstance(part, dict):
            for key in [key for key in part if "speed" in key]:
                del part[key]
    detail["speed_note"] = note


def _speed_parts(detail: Mapping[str, Any]) -> tuple[Any, ...]:
    """The parts of a detail that may carry a speed, the route visits included."""
    channels = (detail.get("streams") or {}).get("channels")
    # The v1 document nests its laps: {"lap_source", "source", "laps": [...]}.
    laps = (detail.get("laps") or {}).get("laps") or ()
    history = (detail.get("route") or {}).get("history") or ()
    return (detail.get("metrics"), channels, *laps, *history)


def _route_history(detail: Mapping[str, Any]) -> None:
    """The Coach's short route history: this visit and the ones before it (#3763).

    The calendar lists every visit; the Coach reads the session against the
    last ``ROUTE_HISTORY_VISITS`` visits of its sport up to it, newest first.
    """
    route, session = detail.get("route"), detail.get("session") or {}
    if not isinstance(route, dict):
        return
    # A loop both run and ridden compares only the session's own sport.
    sport = session.get("sport")
    visits = [
        visit
        for visit in route.pop("visits", None) or []
        if visit.get("sport") in (None, sport)
    ]
    own = session.get("activity_id")
    start = next(
        (n for n, visit in enumerate(visits) if visit.get("activity_id") == own), 0
    )
    route["history"] = visits[start : start + ROUTE_HISTORY_VISITS]


def _has_power(detail: Mapping[str, Any]) -> bool:
    """Whether the ride recorded power: a power stream or an average power."""
    channels = (detail.get("streams") or {}).get("channels") or {}
    metric = (detail.get("metrics") or {}).get("avg_power")
    return any("power" in key for key in channels) or (
        isinstance(metric, Mapping) and metric.get("value") is not None
    )


def _analysis_additions(detail: Mapping[str, Any], effect: Any) -> dict[str, Any]:
    """What the session analysis reads beside the recording (#3038).

    The provider's training-effect label, the one effect value the metrics
    lack, and for a long run or ride the carbohydrate advice for its duration
    (the rule of the advice before a long session, ``session_fuelling``), which
    the analysis compares with the intake logged that day.
    """
    additions: dict[str, Any] = {}
    if isinstance(effect, dict) and effect.get("training_effect_label"):
        additions["training_effect_label"] = {
            "value": effect["training_effect_label"],
            "source": effect.get("source"),
        }
    session = detail.get("session")
    duration = (detail.get("metrics") or {}).get("duration_s")
    seconds = duration.get("value") if isinstance(duration, Mapping) else None
    if (
        isinstance(session, Mapping)
        and session.get("sport") in session_fuelling.SPORTS
        and isinstance(seconds, int | float)
        and seconds >= session_fuelling.LONG_SESSION_S
    ):
        additions["fuelling_advice"] = {
            "carbohydrates_per_hour": fuelling.carbohydrate_per_hour(seconds),
            "source": fuelling.POSITION_STAND,
        }
    return additions


def _not_compared() -> dict[str, Any]:
    """Curve records leave commutes, warm-ups and cool-downs out; say why."""
    return {
        "status": "not_covered",
        "reason": "Pendeln, Einfahren und Ausfahren werden nicht verglichen.",
    }


class CoachActivityReadToolService:
    """Own Coach-specific validation and projections for activity read tools."""

    def __init__(
        self,
        activity_read: ActivityAnalysisReadService,
        garmin_payload: GarminPayloadService,
        profile: ProfileService,
        today: Callable[[], date],
        report_services: Any = None,
        planned_units: Any = None,
        unit_thresholds: Any = None,
        session_corrections: Any = None,
        report_counts: Any = None,
        competitions: Callable[[], Any] | None = None,
        energy_before: Callable[[date], dict[str, Any]] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._activity_read = activity_read
        self._garmin_payload = garmin_payload
        self._profile = profile
        self._today = today
        self._report_services = report_services
        self._planned_units = planned_units
        self._unit_thresholds = unit_thresholds
        self._session_corrections = session_corrections
        self._report_counts = report_counts
        self._competitions = competitions
        self._energy_before = energy_before
        # The live clock, for whether a session's weather is due yet (#4136).
        self._now = now or (lambda: datetime.now(UTC))

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
        if name == "read_training_records":
            if self._report_services is None:
                raise AppError(503, "Trainingsprotokolle sind nicht verfügbar.")
            return {
                "ok": True,
                "records": bounded_coach_context_value(
                    self._report_services.records.training_records(arguments), 30000
                ),
                "scope": "Bounded projection of local equipment. Read exact current revisions before corrections.",
            }
        if name == "get_training_report":
            if self._report_services is None:
                raise AppError(503, "Trainingsberichte sind nicht verfügbar.")
            sections = _report_sections(arguments)
            readers = {
                "progress": lambda: self._report_services.report.progress(
                    arguments, self._report_services.timezone()
                ),
                "block_review": lambda: self._report_services.report.block_review(
                    arguments, self._report_services.timezone()
                ),
                "report": lambda: self._report_services.report.read(
                    arguments, self._report_services.timezone()
                ),
                "endurance": self._report_services.records.endurance,
                "power_profiles": self._report_services.profiles.power_profiles,
                "records": lambda: self._report_services.profiles.records(),
                "tag_impact": lambda: self._report_services.derived.impact(
                    self._report_services.timezone()
                ),
                "season": lambda: self._report_services.season.season(
                    self._report_services.timezone()
                ),
                "comparisons": self._report_services.records.comparisons,
                "body_history": self._report_services.derived.body_history,
                # The Coach context's summary plus each series' last 14 nights.
                "sleep_timing_variability": lambda: coach_sleep_timing_projection(
                    self._report_services.derived.sleep_timing_variability(),
                    recent_nights=True,
                ),
            }
            payload = {
                section: readers[section]() for section in dict.fromkeys(sections)
            }
            return {
                "ok": True,
                **bounded_coach_context_value(payload, 30000),
                "projection": {
                    "requested_sections": sections,
                    "complete": coach_context_json_size(payload) <= 30000,
                },
            }
        if name == "list_recent_activities":
            days = self._bounded_integer(
                arguments,
                "days",
                30,
                3660,
                "Aktivitätszeitraum oder Limit ist ungültig.",
            )
            limit = self._bounded_integer(
                arguments,
                "limit",
                100,
                500,
                "Aktivitätszeitraum oder Limit ist ungültig.",
            )
            recent = self._activity_read.recent(days, limit, today=self._today())
            return {
                "ok": True,
                **recent,
                "activities": [
                    {
                        key: value
                        for key, value in activity.items()
                        if key not in _LIST_OMITTED_KEYS
                    }
                    for activity in recent["activities"]
                ],
            }
        if name == "get_activity_details":
            read = self._referenced_detail(arguments)
            if "outcome" not in read:
                return read
            detail, outcome = read["detail"], read["outcome"]
            self._count_resolution(outcome)
            # Per-second running dynamics are for the calendar's charts; the Coach
            # reads their averages from the metrics.
            detail.pop("running_dynamics_streams", None)
            _route_history(detail)
            # The provider-values section repeats the effect metrics (#2831).
            effect = detail.pop("training_effect", None)
            _without_virtual_speed(detail)
            result = self._with_session_context(detail)
            result.update(_analysis_additions(detail, effect))
            if outcome == "by_id_not_named":
                result["notice"] = _not_named_notice(arguments[NAMED_ACTIVITY_ID])
            session = detail.get("session")
            if self._session_corrections is not None and isinstance(session, dict):
                # Local session IDs and merge candidates for the session tools.
                result["session_bundle"] = self._session_corrections.bundle(
                    session.get("activity_id")
                )
            return result
        return None

    def _referenced_detail(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """The referenced session's detail and how it resolved, or the
        ask-back result. A named session that is not stored any more (a stale
        analyse button) falls back to the call's own reference (#3080)."""
        referenced = self._referenced_activity(arguments)
        if isinstance(referenced, dict):
            return referenced
        activity_id, outcome = referenced
        try:
            detail = self._read_detail(activity_id)
        except AppError as exc:
            if outcome != "by_named_id" or exc.status != 404:
                raise
            return self._referenced_detail({**arguments, NAMED_ACTIVITY_ID: None})
        return {"detail": detail, "outcome": outcome}

    def _read_detail(self, activity_id: Any) -> dict[str, Any]:
        return self._activity_read.detail(
            activity_id,
            garmin_status=self._garmin_payload.sync_status(),
            profile=self._profile.get(),
            today=self._today(),
        )

    def _referenced_activity(
        self, arguments: dict[str, Any]
    ) -> tuple[Any, str] | dict[str, Any]:
        """The activity ID to read and how it resolved, or the ask-back result.

        An ID (the analyse button names it) is read as given. Otherwise the
        date, sport and start time resolve the session deterministically;
        without a date the newest session is meant (#3084). Without an ID or a
        day, the session the athlete's request names is read instead of the
        newest; another ID is read with a notice, since it may be a record ID
        of the same session (#3080).
        """
        named = arguments.get(NAMED_ACTIVITY_ID)
        given = arguments.get("activity_id")
        if named and given is None and not arguments.get("date"):
            return named, "by_named_id"
        if given is not None:
            return given, "by_id" if named in (None, given) else "by_id_not_named"
        start_time = arguments.get("start_time")
        if start_time is not None and _clock_minutes(start_time) is None:
            raise AppError(
                400, "Die Startzeit muss HH:MM sein.", reason="invalid_activity_request"
            )
        newest = not arguments.get("date")
        try:
            last = self._today() if newest else date.fromisoformat(arguments["date"])
        except TypeError, ValueError:
            raise AppError(
                400,
                "Das Datum muss JJJJ-MM-TT sein.",
                reason="invalid_activity_request",
            ) from None
        first = last - timedelta(days=NEWEST_SESSION_DAYS) if newest else last
        summaries = self._activity_read.summaries_between(first, last)
        if summaries is None:
            raise AppError(503, "Aktivitäten sind nicht lesbar.")
        status, sessions = resolve_session(
            summaries,
            sport=arguments.get("sport") or None,
            start_time=start_time,
            newest=newest,
        )
        if status == "resolved":
            return sessions[0].get("id"), "newest" if newest else "by_reference"
        self._count_resolution(status)
        return {
            "ok": True,
            "status": status,
            "candidates": self._candidates(sessions),
            "next": "Ask the athlete which session they mean, then call get_activity_details with its activity_id.",
        }

    def _candidates(
        self, sessions: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        """The newest sessions to choose from, with the focus of their planned unit."""
        shown = sorted(
            sessions,
            key=lambda row: str(row.get("started_at_local") or ""),
            reverse=True,
        )[:MAX_CANDIDATES]
        days = [_session_date(row) for row in shown]
        known = [day for day in days if day is not None]
        units = (
            self._planned_units.list_between(
                min(known) - timedelta(days=1), max(known) + timedelta(days=1)
            )
            if known and self._planned_units is not None
            else None
        )
        return [
            {
                **{key: row.get(key) for key in _CANDIDATE_KEYS},
                "session_focus": matched_focus(row, self._units_around(units, day)).get(
                    "session_focus"
                ),
            }
            for row, day in zip(shown, days, strict=True)
        ]

    def _race(self, session: Mapping[str, Any]) -> dict[str, Any] | None:
        """The race plan of the competition the session is linked to (#2803)."""
        competition_id = session.get("competition_id")
        if not competition_id or self._competitions is None:
            return None
        competition = self._competitions().get(str(competition_id))
        # The plan's sections and target need no body weight.
        return None if competition is None else race_plan(competition, None)

    def _count_resolution(self, outcome: str) -> None:
        if self._report_counts is not None:
            self._report_counts.resolution(outcome)

    def _session_day(self, detail: dict[str, Any]) -> date | None:
        session = detail.get("session")
        return _session_date(session) if isinstance(session, dict) else None

    def _units_since(self, day: date | None) -> list[dict[str, Any]] | None:
        """Local planned units from two days before the session's date on."""
        if self._planned_units is None or day is None:
            return None
        # The earlier strength structures let the session be compared with the
        # plan it trained (#3521).
        return self._planned_units.list(
            250, future_only=True, since=day - timedelta(days=2), trained_versions=True
        )

    @staticmethod
    def _units_around(
        units: list[dict[str, Any]] | None, day: date | None
    ) -> list[dict[str, Any]] | None:
        """Units one day around the session's date.

        The context window is anchored on today, so an older session would lose its
        plan; the unit comes with the session instead.
        """
        if units is None or day is None:
            return None
        first, last = day - timedelta(days=1), day + timedelta(days=1)
        return [
            unit
            for unit in units
            if first.isoformat()
            <= iso_date_prefix(unit.get("date"))
            <= last.isoformat()
        ]

    def _paired_units(
        self,
        session: Any,
        day: date | None,
        around: list[dict[str, Any]] | None,
        nearby: list[Any] | None,
    ) -> tuple[list[dict[str, Any]] | None, str | None]:
        """The units the session may take, and why it takes none.

        The one unit the calendar pairs with the session, none when it pairs
        another session (or none) with the day's units, with the reason; every
        unit around the day when the day's sessions cannot be read, so the
        unique day-and-sport rule stands in.
        """
        if around is None or day is None or not isinstance(session, dict):
            return around, None
        session_id = str(session.get("activity_id") or "")
        first, last = (
            (day - timedelta(days=1)).isoformat(),
            (day + timedelta(days=1)).isoformat(),
        )
        # The units' window: a provider link to a neighbouring day's unit counts.
        summaries = [
            row
            for row in nearby or ()
            if first <= iso_date_prefix(row.get("started_at_local")) <= last
        ]
        own = next(
            (
                row
                for row in summaries
                if session_id in (stored_activity_key(row), *legacy_activity_keys(row))
            ),
            None,
        )
        if own is None:
            return around, None
        pairs = calendar_pairs(around, summaries)
        unit = next(
            (
                item
                for item in around
                if session_id in pairs.get(str(item.get("id")), ())
            ),
            None,
        )
        if unit is not None:
            return [unit], None
        return [], _unpaired_reason(session, own, around, pairs)

    def _with_session_context(self, detail: dict[str, Any]) -> dict[str, Any]:
        """The detail with its planned units and the session checks.

        Focus, pace at heart rate and leg strength before a run or ride come
        with the tool result, not the base context.
        """
        day = self._session_day(detail)
        units = self._units_since(day)
        around = self._units_around(units, day)
        session = detail.get("session")
        # One read of the sessions around the day for the pairing and the checks.
        nearby = (
            None
            if day is None
            else self._activity_read.summaries_between(
                day - timedelta(days=3), day + timedelta(days=1)
            )
        )
        # The checks see only the unit the calendar pairs with the session (#2863).
        paired, unpaired_reason = self._paired_units(session, day, around, nearby)
        calendar = paired is not None and paired is not around
        result = {
            **detail,
            "planned_units": None
            if around is None
            else bounded_coach_context_value(around[:10], 4000),
        }
        if isinstance(session, dict) and day is not None:
            result.update(
                self._session_checks(session, day, paired, units, nearby, calendar)
            )
            focus = result.get("focus")
            if unpaired_reason and isinstance(focus, dict) and not paired:
                result["focus"] = {**focus, "reason": unpaired_reason}
            targets = self._planned_targets(
                detail, session, result.get("focus"), paired
            )
            cap = self._stored_heart_rate_cap(
                detail, session, result.get("focus"), paired
            )
            if cap is not None:
                targets = {
                    **(targets or {"status": "not_covered"}),
                    "heart_rate_cap": cap,
                }
            if targets is not None:
                result["planned_targets"] = targets
            strength = _strength_plan(detail, session, result.get("focus"), paired)
            if strength is not None:
                result["strength_plan"] = strength
            result.update(self._step_test(detail, paired, calendar))
            result.update(self._influences(detail, session, day))
            # Code judges every deterministic check; the Coach explains (#3084).
            result["session_report"] = self._session_report(
                result, session, paired, nearby
            )
        return result

    def _influences(
        self, detail: Mapping[str, Any], session: Mapping[str, Any], day: date
    ) -> dict[str, Any]:
        """The energy balance of the days before, and a missing weather named
        as a gap of the app's own look-up (#4136); runs and rides only."""
        if session.get("sport") not in ("run", "ride"):
            return {}
        found: dict[str, Any] = {}
        if self._energy_before is not None:
            found["energy_before"] = self._energy_before(day)
        if not detail.get("weather"):
            found["weather"] = _weather_gap(session, self._now())
        return found

    def _step_test(
        self,
        detail: Mapping[str, Any],
        paired: list[dict[str, Any]] | None,
        calendar: bool,
    ) -> dict[str, Any]:
        """The run step test result (#3833) of the unit the calendar pairs, if one."""
        unit = paired[0] if calendar and paired else None
        tested = self._activity_read.step_test(detail, unit)
        return {} if tested is None else {"step_test": tested}

    def _session_report(
        self,
        result: dict[str, Any],
        session: dict[str, Any],
        around: list[dict[str, Any]] | None,
        nearby: list[Any] | None,
    ) -> dict[str, Any]:
        """The report: the paired unit's estimate against the local load (M6).

        Runs and rides only, like the other planned checks.
        """
        unit = _matched_unit(session, result.get("focus"), around)
        planned = None
        if (
            unit is not None
            and self._unit_thresholds is not None
            and session.get("sport") in ("run", "ride")
        ):
            estimate = self._unit_thresholds(unit)
            bands = estimate.band_seconds or {}
            planned = {
                "duration_s": estimate.duration_s,
                "load": estimate.load,
                "above_easy_s": bands.get("moderate", 0.0) + bands.get("high", 0.0),
                # The plan sets a low cadence (low-cadence reps are planned, #3084).
                "cadence": _plans_low_cadence(unit.get("description")),
            }
        own = _own_summary(session, nearby) or {}
        day = _session_date(session)
        # The checks of #3084 Part 3 read the session's IF and device GAP, and
        # the athlete's rated sessions of the 84 days before the session's day,
        # so a session later that day never enters its expectation.
        rated = (
            []
            if day is None
            else self._activity_read.rated_sessions(
                day - timedelta(days=RATED_WINDOW_DAYS), day - timedelta(days=1)
            )
        )
        inputs = {
            # The load of the days before (#3084 3c H).
            "daily_loads": None
            if day is None
            else _daily_loads(self._activity_read, day),
            # The stored headwind with its bins: for the report only (#3084).
            "headwind": self._activity_read.session_headwind(
                str(session.get("activity_id") or "")
            ),
            "intensity": own.get("intensity"),
            "grade_adjusted_speed_mps": own.get("grade_adjusted_speed_mps"),
            "rated_sessions": rated,
            "easy_limit": self._easy_limit(result, session, unit, day),
        }
        # The report's facts only: they leave the tool result, so the model
        # reads them once (#3084 Part 2).
        facts: dict[str, Any] = {
            key: result.pop(key)
            for key in ("critical_speed", "running_dynamics_rating", "energy_before")
            if key in result
        }
        adherence = self._step_adherence(result, session, unit) if planned else None
        # Each planned segment against its window, for the report (#4136).
        if isinstance(adherence, Mapping) and adherence.get("status") == "ok":
            facts["plan_segments"] = adherence.get("steps")
        report = session_report(
            {**result, CONDITION_INPUTS: inputs, **facts},
            planned,
            own.get("training_load"),
            self._race(session),
            _plan_zones(adherence),
        )
        if self._report_counts is not None:
            self._report_counts.report(report)
        return report

    def _easy_limit(
        self,
        result: Mapping[str, Any],
        session: Mapping[str, Any],
        unit: Mapping[str, Any] | None,
        day: date | None,
    ) -> dict[str, Any] | None:
        """The LT1 heart rate of the session's day and the stream's time above it.

        The LT1 stand-in of the heart-rate reference in effect (``LT1_HR_SHARE``
        of the LTHR, else of the max-HR share, as the stored band times count it,
        #3915); None without a reference or stream.
        """
        bpm = self._easy_bpm(session, unit, day)
        found = None if bpm is None else heart_rate_above(bpm, result.get("streams"))
        if bpm is None or found is None:
            return None
        return {"bpm": bpm, "above_s": found[0], "total_s": found[1]}

    def _easy_bpm(
        self,
        session: Mapping[str, Any],
        unit: Mapping[str, Any] | None,
        day: date | None,
    ) -> int | None:
        """The LT1 heart rate of the session's day, None without a reference."""
        sport = session.get("sport")
        if self._unit_thresholds is None or day is None or sport not in ("run", "ride"):
            return None
        reference = unit or {"sport": str(sport).title(), "date": day.isoformat()}
        settings = self._unit_thresholds.thresholds(reference)
        lthr = settings.hr_zone_reference
        if not lthr:
            return None
        # The athlete's own first threshold, else the stand-in (#3915).
        return round(lthr * (settings.lt1_share or LT1_HR_SHARE))

    def _session_types(
        self,
        session: Mapping[str, Any],
        day: date,
        records: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """Each compared session's type: its paired unit's focus and heart-rate
        cap, else detected from its execution (#4136)."""
        plans: dict[str, dict[str, Any]] = {}
        if self._planned_units is not None:
            first = day - timedelta(days=COHORT_DAYS)
            units = self._planned_units.list(250, future_only=True, since=first)
            summaries = self._activity_read.summaries_between(
                first, day + timedelta(days=1)
            )
            by_id = {str(unit.get("id")): unit for unit in units}
            for unit_id, keys in calendar_pairs(units, summaries or []).items():
                if keys and unit_id in by_id:
                    plans[keys[0]] = _unit_plan(by_id[unit_id])
        return session_types(records, plans, self._easy_bpm(session, None, day))

    def _step_adherence(
        self, result: Mapping[str, Any], session: Mapping[str, Any], unit: Any
    ) -> Any:
        """The matched unit's steps and zone times against the execution (#2830)."""
        analysis = result.get("analysis")
        execution = analysis.get("execution") if isinstance(analysis, Mapping) else None
        if (
            not isinstance(execution, Mapping)
            or execution.get("status") != "ok"
            or self._unit_thresholds is None
        ):
            return None
        steps = unit_steps(unit)
        if not isinstance(steps, list):
            return steps
        return planned_step_adherence(
            execution,
            steps,
            self._unit_thresholds.thresholds(unit),
            str(session.get("sport")),
            streams=result.get("streams"),
            planned_unit_id=str(unit.get("id")),
        )

    def _session_checks(
        self,
        session: dict[str, Any],
        day: date,
        around: list[dict[str, Any]] | None,
        units: list[dict[str, Any]] | None,
        nearby: list[Any] | None,
        paired: bool = False,
    ) -> dict[str, Any]:
        checks: dict[str, Any] = {
            "focus": matched_focus(session, around, paired=paired)
        }
        if session.get("sport") not in ("run", "ride"):
            return checks
        checks["strength_before"] = self._strength_before(session, nearby, units)
        own = _own_summary(session, nearby)
        covered_from = self._covered_from()
        # A session before the curve window has no curve; nothing is read for it.
        inside = own is not None and day >= covered_from
        records = self._curve_records() if inside else None
        # Only a compared session reads the types (#4136).
        compared = (
            own is not None
            and not summary_is_electric(own)
            and not summary_is_not_training(own)
        )
        types = (
            self._session_types(session, day, records)
            if compared and records is not None
            else None
        )
        if session.get("sport") == "run":
            checks["pace_at_heart_rate"] = self._pace_at_heart_rate(
                own, records, day, types
            )
            checks["critical_speed"] = _critical_speed(records, day, covered_from)
            checks["running_dynamics_rating"] = self._dynamics_rating(own)
        if own is None or (inside and records is None):
            checks["comparable_sessions"] = _unavailable()
        elif summary_is_electric(own):
            # Curve records leave e-bike sessions out; say why, not "no streams".
            checks["comparable_sessions"] = {
                "status": "not_covered",
                "reason": "E-Bike-Einheiten werden nicht verglichen (Motorunterstützung).",
            }
        elif summary_is_not_training(own):
            checks["comparable_sessions"] = _not_compared()
        elif records is None:
            checks["comparable_sessions"] = {
                "status": "not_covered",
                "reason": f"Die Einheit liegt außerhalb des Auswertungsfensters von {PROFILE_WINDOW_DAYS[-1]} Tagen.",
            }
        else:
            checks["comparable_sessions"] = comparable_sessions(
                records,
                stored_activity_key(own),
                covered_from=covered_from,
                types=types,
            )
        return checks

    def _dynamics_rating(self, own: Mapping[str, Any] | None) -> dict[str, Any] | None:
        """The run's running dynamics per pace band against own runs (#2829), as
        the app rates them over its full window; a fact."""
        if own is None or self._report_services is None:
            return None
        rating = self._report_services.profiles.running_dynamics(
            stored_activity_key(own)
        )
        session = rating.get("session") if isinstance(rating, Mapping) else None
        return dict(session) if isinstance(session, Mapping) else None

    def _covered_from(self) -> date:
        """First day of the curve window that the session comparisons read."""
        return self._today() - timedelta(days=PROFILE_WINDOW_DAYS[-1] - 1)

    def _curve_records(self) -> list[dict[str, Any]] | None:
        """Sessions of the curve window with their curves and executions, read once."""
        if self._report_services is None:
            return None
        try:
            return self._report_services.profiles.curve_records()
        except RuntimeError:
            return None

    def _stored_heart_rate_cap(
        self,
        detail: dict[str, Any],
        session: dict[str, Any],
        focus: Any,
        around: list[dict[str, Any]] | None,
    ) -> dict[str, Any] | None:
        """The run's heart rate against the matched unit's stored cap, if it has one.

        The cap is stored in percent; the threshold HR in effect on the unit's
        date resolves it to bpm. The target band runs from the plan's own
        heart-rate floor up to it.
        """
        if session.get("sport") != "run" or self._unit_thresholds is None:
            return None
        unit = _matched_unit(session, focus, around)
        cap = None if unit is None else stored_cap(unit)
        if unit is None or cap is None:
            return None
        thresholds = self._unit_thresholds.thresholds(unit)
        steps = unit_steps(unit)
        # The plan's own heart-rate range sets the target band's floor (#4136).
        floor = (
            planned_heart_rate_floor(steps, thresholds, "run")
            if isinstance(steps, list)
            else None
        )
        return heart_rate_cap_execution(
            cap, thresholds.lthr, detail.get("streams"), floor
        )

    def _planned_targets(
        self,
        detail: dict[str, Any],
        session: dict[str, Any],
        focus: Any,
        around: list[dict[str, Any]] | None,
    ) -> dict[str, Any] | None:
        """Work laps against the matched planned unit's steps, for runs and rides."""
        if session.get("sport") not in ("run", "ride") or self._unit_thresholds is None:
            return None
        unit = _matched_unit(session, focus, around)
        analysis = detail.get("analysis")
        execution = analysis.get("execution") if isinstance(analysis, dict) else None
        if unit is None or not isinstance(execution, dict):
            return None
        steps = unit_steps(unit)
        if not isinstance(steps, list):
            return steps
        return planned_target_execution(
            execution,
            steps,
            self._unit_thresholds.thresholds(unit),
            str(session.get("sport")),
            planned_unit_id=str(unit.get("id")),
            streams=detail.get("streams"),
        )

    @staticmethod
    def _strength_before(
        session: dict[str, Any], nearby: Any, units: list[dict[str, Any]] | None
    ) -> dict[str, Any]:
        try:
            start = datetime.fromisoformat(str(session.get("started_at_local")))
        except ValueError:
            return {"status": "not_covered", "reason": "Startzeit unbekannt."}
        return leg_strength.strength_before(start, nearby, lambda: units)

    def _pace_at_heart_rate(
        self,
        own: Mapping[str, Any] | None,
        records: list[dict[str, Any]] | None,
        day: date,
        types: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if (
            own is not None
            and day - timedelta(days=COMPARE_DAYS) < self._covered_from()
        ):
            return {
                "status": "not_covered",
                "reason": f"Die Läufe der letzten {COMPARE_DAYS} Tage vor dieser Aktivität liegen außerhalb des Auswertungsfensters von {PROFILE_WINDOW_DAYS[-1]} Tagen.",
            }
        if own is None or records is None:
            return _unavailable()
        if summary_is_not_training(own):
            return _not_compared()
        key = stored_activity_key(own)
        # Only runs of this one's session type (#4136).
        peers = None if types is None else same_type_keys(types, key)
        return session_comparison(records, key, peers)

    @staticmethod
    def _bounded_integer(
        arguments: dict[str, Any], key: str, default: int, maximum: int, error: str
    ) -> int:
        try:
            return max(1, min(int(arguments.get(key, default)), maximum))
        except (TypeError, ValueError) as exc:
            raise AppError(400, error, reason="invalid_list_request") from exc


# Why the weather stage has not looked a session up yet (#4136): no gap.
_WEATHER_PENDING: Final = {
    "before_first_day": "Für Einheiten vor 2022 ruft die App kein Wetter ab.",
    "not_settled": "Das Wetter wird erst 48 Stunden nach der Einheit abgerufen.",
}


def _weather_gap(session: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    """Why a run or ride has no stored weather: indoors none is looked up, a
    session before 2022 or of the last 48 hours not yet; else the app's own
    look-up left a gap."""
    if session.get("environment") in INDOOR_ENVIRONMENTS:
        return {"status": "not_applicable", "reason": "Drinnen: kein Wetter."}
    try:
        window = activity_window(
            datetime.fromisoformat(str(session.get("started_at"))),
            session.get("elapsed_duration_s") or summary_duration_s(session),
        )
    except ValueError:
        window = None
    pending = None if window is None else weather_pending(*window, now)
    if pending is not None:
        return {"status": "pending", "reason": _WEATHER_PENDING[pending]}
    return {
        "status": "missing",
        "reason": "Datenlücke: Die App ruft das Wetter für Läufe und Radfahrten draußen ab, zu dieser Einheit ist keins gespeichert.",
    }


def _unit_plan(unit: Mapping[str, Any]) -> dict[str, Any]:
    """A paired unit's focus and its heart-rate cap as the plan target (#4136)."""
    cap = stored_cap(unit)
    return {
        "focus": unit.get("session_focus"),
        "target": None if cap is None else cap.get("percent_lthr"),
    }


def _report_sections(arguments: dict[str, Any]) -> list[str]:
    """Validated report sections; progress and its long windows stand alone."""
    days = arguments.get("days")
    long_window = isinstance(days, int) and days in {42, 56, 84}
    sections = arguments.get("sections", ["progress"] if long_window else ["report"])
    allowed = {
        "progress",
        "block_review",
        "report",
        "endurance",
        "power_profiles",
        "records",
        "tag_impact",
        "season",
        "comparisons",
        "body_history",
        "sleep_timing_variability",
    }
    if (
        not isinstance(sections, list)
        or not 1 <= len(sections) <= 10
        or any(
            not isinstance(section, str) or section not in allowed
            for section in sections
        )
    ):
        raise AppError(400, "Unbekannter Analysebereich.")
    # progress uses whole ISO weeks, so it never shares a call with
    # sections that read the requested window as given.
    alone = sections in (["progress"], ["block_review"])
    if (long_window or {"progress", "block_review"} & set(sections)) and not alone:
        raise AppError(
            400, "progress, block_review und 42, 56 oder 84 Tage nur allein abrufbar."
        )
    return sections


def _matched_unit(
    session: Mapping[str, Any], focus: Any, around: list[dict[str, Any]] | None
) -> dict[str, Any] | None:
    """The focus's planned unit, else the one unit of the session's day and sport.

    A unit without a session focus still has steps to compare, so the same
    unique day-and-sport rule as the focus applies without needing a focus.
    """
    units = around or []
    unit_id = focus.get("planned_unit_id") if isinstance(focus, dict) else None
    if unit_id:
        return next((unit for unit in units if unit.get("id") == unit_id), None)
    day = iso_date_prefix(session.get("started_at_local"))
    same = [
        unit
        for unit in units
        if iso_date_prefix(unit.get("date")) == day
        and planned_sport_environment(unit)[0].value == session.get("sport")
    ]
    return same[0] if len(same) == 1 else None


def _unpaired_reason(
    session: Mapping[str, Any],
    own: Mapping[str, Any],
    around: list[dict[str, Any]],
    pairs: Mapping[str, tuple[str, ...]],
) -> str | None:
    """Why a session with a planned unit of its day and sport takes none."""
    day = iso_date_prefix(session.get("started_at_local"))
    same = [
        unit
        for unit in around
        if iso_date_prefix(unit.get("date")) == day
        and planned_sport_environment(unit)[0].value == session.get("sport")
    ]
    if not same:
        return None
    if summary_is_not_training(own):
        return "Pendeln, Einfahren und Ausfahren erfüllen keine geplante Einheit."
    if any(str(unit.get("id")) in pairs for unit in same):
        return "Die geplante Einheit dieses Tages gehört zu einer anderen Aktivität."
    return None


def _daily_loads(activity_read: Any, day: date) -> dict[str, float]:
    """The local load per day of the days before ``day``, for the 3-day prior load."""
    days = (
        session_conditions.PRIOR_LOAD_WINDOW_DAYS + session_conditions.PRIOR_LOAD_DAYS
    )
    loads: dict[str, float] = {}
    for summary in (
        activity_read.summaries_between(
            day - timedelta(days=days), day - timedelta(days=1)
        )
        or ()
    ):
        load = summary.get("training_load")
        started = iso_date_prefix(summary.get("started_at_local"))
        if isinstance(load, (int, float)) and started:
            loads[started] = loads.get(started, 0.0) + float(load)
    return loads


def _own_summary(
    session: Mapping[str, Any] | None, nearby: Sequence[Any] | None
) -> Any:
    """The session's own summary among the sessions read around its day."""
    activity_id = None if session is None else session.get("activity_id")
    return next((row for row in nearby or () if row.get("id") == activity_id), None)


def _strength_plan(
    detail: Mapping[str, Any],
    session: Mapping[str, Any],
    focus: Any,
    around: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """Executed strength sets against the matched unit's ``strength_structure``."""
    executed = detail.get("strength_sets")
    if session.get("sport") != "strength" or not isinstance(executed, dict):
        return None
    unit = _matched_unit(session, focus, around)
    # The structure the session trained, not a later edit (#3521).
    structure = (
        None if unit is None else trained_structure(unit, session.get("started_at"))
    )
    if not isinstance(structure, list):
        return None
    comparison = plan_comparison(
        [item for item in structure if isinstance(item, dict)], executed
    )
    if comparison is None:
        return None
    return {**comparison, "planned_unit_id": unit.get("id") if unit else None}


def _unavailable() -> dict[str, Any]:
    return {
        "status": "not_covered",
        "reason": "Auswertung der Messreihen nicht verfügbar.",
    }

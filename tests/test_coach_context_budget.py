"""Per-task Coach context: exact section sets and size budgets (#1627).

One rich synthetic fixture runs through the real context service and the
selection projection. Sizes are deterministic, so growth is a deliberate change.
Set BUDGET_TABLE=1 to print the table.
"""

from __future__ import annotations

import copy
import json
import os
import unittest
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any, ClassVar
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from activity_row_support import FixedActivityRows, ReadOnlyManager, StoredCoverage
from canonical_domain_support import local_zone_configuration
from canonical_recovery_support import (
    FilteringReader,
    recovery_only_inputs,
    with_local_load_of,
)
from performance_day_support import (
    garmin_activity_summaries,
    garmin_body_from_payload,
    garmin_sync_status,
)
from server_test_support import server

from backend.athlete.body_records import read_current_body
from backend.canonical.activity_detail import ActivityDfaAlpha1, DfaWindow
from backend.canonical.domain_records import ZoneConfiguration
from backend.canonical.vocabulary import DfaQuality, Sport, ZoneConfigurationMetric
from backend.coach import context as coach_context
from backend.coach.context import (
    COACH_CONTEXT_SECTION_LIMITS,
    CoachPerformanceContextReader,
    CoachPlanningContextReader,
    CoachStructuredContextService,
    CoachTrainingContextService,
    coach_performance_projection,
)
from backend.coach.context_selection import (
    CONTEXT_SECTIONS,
    CoachContextSelection,
    select_coach_context,
)
from backend.coach.playbooks import playbook_for
from backend.coach.prompt import COACH_PROMPT
from backend.performance.canonical_current import read_current_inputs
from backend.performance.context import current_performance_context
from backend.performance.first_threshold_estimate import (
    SessionThreshold,
    estimate_view,
    first_threshold_estimate,
)
from backend.performance.freshness import GarminSyncStatus
from backend.performance.second_threshold_estimate import (
    critical_second_threshold,
    dfa_second_threshold,
)
from backend.planning.compact_activities import ActivitySummarySource
from backend.planning.context import build_daily_planning_context
from backend.providers.garmin_health_mapping import map_garmin_health

TODAY = date(2026, 10, 7)
SYNCED_AT = datetime(2026, 10, 7, 6, 0, tzinfo=UTC)
MARKER = "LOCAL PLANNED WORKOUTS (compact projection, included once below):\n"
MORNING_MESSAGE = (
    "Gib mir den heutigen Morgen-Check-in. Lade dafür die aktuellen "
    "Garmin-/Intervals.icu-Daten, bewerte Erholung, Trainingsbelastung und "
    "die geplante Einheit und sag mir, was heute sinnvoll ist."
)
# message, request kind. Sections are listed without the always-present ones.
CASES: dict[str, tuple[str, str | None]] = {
    "session_analysis": ("Analysiere meine letzte Einheit", "session_analysis"),
    "progress_review": ("Wie läuft mein Training?", "progress_review"),
    "block_review": ("Bewerte meinen letzten Block", "block_review"),
    "plan_week": ("Plane die nächste Woche", "plan_week"),
    "plan_block": ("Plane die nächsten 4 Wochen", "plan_block"),
    "adjust_week": ("Passe die Woche an", "adjust_week"),
    "morning_checkin": (MORNING_MESSAGE, "morning_checkin"),
    "free_text_planning": ("Plane meine nächste Woche", None),
    "general_question": ("Wie geht es meiner Erholung?", None),
    "nutrition_message": ("Ich habe 60g Whey getrunken", None),
}


def _planned_units() -> list[dict[str, Any]]:
    units = []
    for offset in range(36):
        day = TODAY + timedelta(days=offset)
        for slot, name in enumerate(("Easy run", "Strength")):
            if slot and offset % 2:
                continue
            units.append(
                {
                    "id": f"unit-{offset}-{slot}",
                    "date": day.isoformat(),
                    "starts_at_local": f"{day.isoformat()}T0{7 + slot}:00:00",
                    "name": f"{name} {offset}",
                    "type": "Run" if not slot else "WeightTraining",
                    "target": "Synthetic target description. " * 6,
                    "status": "planned",
                    "sync_status": "local",
                    # Every easy run stores its cap (#4286).
                    **(
                        {}
                        if slot
                        else {
                            "session_focus": "easy",
                            "hr_cap": {
                                "percent_lthr": 84.0,
                                "low_percent_lthr": 69.0,
                                "source": "default",
                            },
                        }
                    ),
                }
            )
    return units


def _activities() -> list[dict[str, Any]]:
    rows = []
    for index in range(30):
        day = TODAY - timedelta(days=index + 1)
        rows.append(
            {
                "id": f"activity-{index}",
                "started_at_local": f"{day.isoformat()}T07:00:00",
                "name": f"Synthetic session {index}",
                "sport": "run" if index % 3 else "ride",
                "moving_duration_s": 3000 + index,
                "distance_m": 9000 + index,
                "training_load": 40 + index,
            }
        )
    return rows


def _calendar_events() -> list[dict[str, Any]]:
    events = []
    for index in range(28):
        day = (TODAY + timedelta(days=index * 2)).isoformat()
        events.append(
            {
                "id": f"event-{index}",
                "name": f"Synthetic appointment {index}",
                "event_date": day,
                "start_local": f"{day}T17:00:00",
                "end_local": f"{day}T18:30:00",
                "all_day": False,
                "no_training": index % 9 == 0,
                "no_intensity": False,
                "short_only": False,
                "training_relevant": 1,
            }
        )
    return events


def _garmin_payload() -> dict[str, Any]:
    """A synced Garmin athlete (#4325): 84 days of sleep, HRV and resting heart
    rate, thresholds, weight, training load and 28 days of sessions with zone
    times and training effect, every section fresh."""
    day = TODAY.isoformat()

    def at(offset: int, hour: int, minute: int) -> int:
        stamp = datetime(
            *(TODAY - timedelta(days=offset)).timetuple()[:3],
            hour,
            minute,
            tzinfo=UTC,
        )
        return int(stamp.timestamp() * 1000)

    dated = [((TODAY - timedelta(days=n)).isoformat(), n) for n in range(84)]
    sessions = []
    for n in range(60):
        if n % 7 == 3:  # a rest day each week
            continue
        kind = {5: "lap_swimming", 6: "hiking"}.get(
            n % 7, ("running", "cycling", "strength_training")[n % 3]
        )
        seconds = (3000, 4200, 2400)[n % 3] + 60 * (n % 5)
        row: dict[str, Any] = {
            "activityId": 9000 + n,
            "startTimeLocal": f"{(TODAY - timedelta(days=n)).isoformat()}T07:30:00",
            "activityName": f"Synthetic {kind.replace('_', ' ')} {n}",
            "activityType": {"typeKey": kind},
            "movingDuration": seconds,
            "duration": seconds + 90,
            "distance": (9500, 31000, 0)[n % 3] + 100 * n,
            "averageHR": 138 + n % 9,
            "maxHR": 168 + n % 11,
            "activityTrainingLoad": 55 + 3 * (n % 8),
            "trainingEffectLabel": (
                "AEROBIC_BASE",
                "TEMPO",
                "THRESHOLD",
                "VO2MAX",
                "RECOVERY",
                "ANAEROBIC_CAPACITY",
            )[n % 6],
            "aerobicTrainingEffect": 2.4 + (n % 5) / 4,
            "anaerobicTrainingEffect": 0.3 + (n % 4) / 5,
            "calories": 450 + 12 * n,
        }
        if kind == "strength_training":
            row.update(directWorkoutRpe=50 + 10 * (n % 4), directWorkoutFeel=50)
        for zone in range(1, 6):
            row[f"hrTimeInZone_{zone}"] = (seconds // 5) + 40 * (n % 4) * zone
        if kind == "cycling":
            row.update(avgPower=188 + n % 20, normPower=205 + n % 25, maxPower=640)
            for zone in range(1, 8):
                row[f"powerTimeInZone_{zone}"] = (seconds // 7) + 30 * (n % 5) * zone
        sessions.append(row)
    payload: dict[str, Any] = {
        "synced_at": f"{day}T05:00:00Z",
        "sleep": [
            {
                "calendarDate": iso,
                "sleepTimeSeconds": 25_800 + 600 * (n % 5),
                "sleepScore": 76 + n % 9,
                "sleepStartTimestampGMT": at(n + 1, 21, 15 + 10 * (n % 4)),
                "sleepEndTimestampGMT": at(n, 4, 40 + 3 * (n % 6)),
            }
            for iso, n in dated
        ],
        "hrv": [{"calendarDate": iso, "hrvLastNight": 58 + n % 7} for iso, n in dated],
        "resting_hr": [
            {"calendarDate": iso, "restingHeartRate": 47 + n % 4} for iso, n in dated
        ],
        "readiness": [{"calendarDate": iso, "score": 62 + n % 15} for iso, n in dated],
        "weight": [
            {"calendarDate": iso, "weight": 72.6 - 0.03 * n, "bodyFat": 14.8 - n / 80}
            for iso, n in dated[:30]
        ],
        "daily_stats": [
            {
                "calendarDate": iso,
                "totalSteps": 7000 + 400 * (n % 6),
                "floorsAscended": 9 + n % 5,
                "totalKilocalories": 2600 + 25 * (n % 7),
            }
            for iso, n in dated[:14]
        ],
        "max_metrics": [
            {
                "calendarDate": day,
                "generic": {"vo2MaxPreciseValue": 54.2, "calendarDate": day},
                "cycling": {"vo2MaxPreciseValue": 56.8, "calendarDate": day},
            }
        ],
        "cycling_ftp": [{"calendarDate": day, "functionalThresholdPower": 252}],
        "running_threshold": [
            {
                "speed_and_heart_rate": {
                    "speed": 3.7,
                    "heartRate": 168,
                    "heartRateCycling": 163,
                },
                "power": {"functionalThresholdPower": 238},
            }
        ],
        "heart_rate_zones": [
            {"sport": "cycling", "maxHeartRateUsed": 186},
            {"sport": "running", "maxHeartRateUsed": 191},
        ],
        "performance_history": [
            {
                "date": iso,
                "metrics": {
                    "cycling_ftp_watts": 247 + (29 - n) // 5,
                    "bike_threshold_hr_bpm": 161 + (29 - n) // 12,
                    "run_threshold_watts": 234 + (29 - n) // 6,
                    "run_threshold_hr_bpm": 166 + (29 - n) // 14,
                    "run_threshold_pace_seconds_per_km": 275 - (29 - n) // 5,
                    "cycling_vo2max_ml_kg_min": 55.4 + (29 - n) / 20,
                    "running_vo2max_ml_kg_min": 53.0 + (29 - n) / 25,
                    "run_5k_seconds": 1330 - (29 - n) * 2,
                    "run_10k_seconds": 2790 - (29 - n) * 3,
                    "run_half_marathon_seconds": 6150 - (29 - n) * 6,
                    "run_marathon_seconds": 12900 - (29 - n) * 12,
                },
            }
            for iso, n in dated[:30]
        ],
        "race_predictions": {
            "5k": 1290,
            "10k": 2700,
            "halfMarathon": 6000,
            "marathon": 12600,
        },
        "daily_training_status": {
            "mostRecentTrainingStatus": {
                "latestTrainingStatusData": {
                    "3300001": {
                        "calendarDate": day,
                        "primaryTrainingDevice": True,
                        "acuteTrainingLoadDTO": {
                            "dailyTrainingLoadAcute": 520,
                            "dailyTrainingLoadChronic": 480,
                            "dailyAcuteChronicWorkloadRatio": 1.1,
                        },
                    }
                }
            }
        },
        "training_load_balance": {
            "mostRecentTrainingLoadBalance": {
                "metricsTrainingLoadBalanceDTOMap": {
                    "3300001": {
                        "calendarDate": day,
                        "primaryTrainingDevice": True,
                        "monthlyLoadAerobicLow": 420.0,
                        "monthlyLoadAerobicLowTargetMin": 300.0,
                        "monthlyLoadAerobicLowTargetMax": 600.0,
                        "monthlyLoadAerobicHigh": 180.0,
                        "monthlyLoadAerobicHighTargetMin": 120.0,
                        "monthlyLoadAerobicHighTargetMax": 260.0,
                        "monthlyLoadAnaerobic": 30.0,
                        "monthlyLoadAnaerobicTargetMin": 0.0,
                        "monthlyLoadAnaerobicTargetMax": 80.0,
                    }
                }
            }
        },
        "activities": sessions,
        "training_load_activities": [
            {
                key: row[key]
                for key in (
                    "activityId",
                    "activityTrainingLoad",
                    "trainingEffectLabel",
                    "aerobicTrainingEffect",
                    "anaerobicTrainingEffect",
                )
            }
            for row in sessions
        ],
    }
    payload["source_freshness"] = {
        section: {
            "freshness": "current",
            "observed_at": day,
            "fetched_at": f"{day}T05:00:00Z",
        }
        for section in list(payload) + ["training_load_activities"]
    }
    return payload


def _performance_inputs(payload: dict[str, Any]) -> Any:
    """The canonical inputs a Garmin sync of ``payload`` stores, with the local
    session load a pass computes from its thresholds."""
    observations = map_garmin_health(
        payload, zone=ZoneInfo("Europe/Berlin"), retrieved_at=SYNCED_AT
    ).records
    return replace(
        recovery_only_inputs(payload),
        current=read_current_inputs(
            _StoredReader(observations),
            None,
            as_of=TODAY,
            body=lambda reader, db, day: read_current_body(
                reader, db, day, lambda _db, _until: ()
            ),
        ),
        activities=with_local_load_of(garmin_activity_summaries(payload), observations),
        body=garmin_body_from_payload(payload, TODAY),
        activities_synced_at=SYNCED_AT.isoformat(),
    )


class _StoredReader(FilteringReader):
    """The stored observations of a synced athlete and their local zone settings."""

    def zone_configurations(self, _db: Any) -> tuple[ZoneConfiguration, ...]:
        return _zone_configurations()


def _zone_configurations() -> tuple[ZoneConfiguration, ...]:
    """The athlete's local zone settings: heart rate, power and pace by sport."""
    since = date(2026, 9, 1)
    return (
        local_zone_configuration(
            sport=Sport.RUN,
            valid_from=since,
            threshold=168.0,
            maximum=191.0,
            limits=(124.0, 141.0, 155.0, 168.0, 191.0),
        ),
        local_zone_configuration(
            sport=Sport.RIDE,
            valid_from=since,
            threshold=163.0,
            maximum=186.0,
            limits=(118.0, 135.0, 149.0, 163.0, 186.0),
        ),
        local_zone_configuration(
            metric=ZoneConfigurationMetric.POWER,
            sport=Sport.RIDE,
            valid_from=since,
            threshold=252.0,
            maximum=None,
            resting=None,
            limits=(139.0, 189.0, 227.0, 265.0, 302.0, 378.0),
        ),
        local_zone_configuration(
            metric=ZoneConfigurationMetric.POWER,
            sport=Sport.RUN,
            valid_from=since,
            threshold=238.0,
            maximum=None,
            resting=None,
            limits=(167.0, 202.0, 238.0, 274.0, 321.0),
        ),
        local_zone_configuration(
            metric=ZoneConfigurationMetric.PACE,
            sport=Sport.RUN,
            valid_from=since,
            threshold=3.7,
            maximum=None,
            resting=None,
            limits=(2.6, 3.1, 3.4, 3.7, 4.0, 4.4),
        ),
    )


def _lt1_session_records(days: int) -> list[dict[str, Any]]:
    """The curve records of the last sessions: one ride's best 20 minutes."""
    return [
        {
            "date": (TODAY - timedelta(days=12)).isoformat(),
            "sport": "ride",
            "environment": "outdoor",
            "power_profile": {
                "duration_curve": [{"duration_seconds": 1200, "watts": 236}]
            },
        }
    ]


def _first_threshold_estimate() -> dict[str, Any] | None:
    """The adopted-estimate view of four chest-strap runs at about 77 % of LTHR."""
    sessions = [
        SessionThreshold(TODAY - timedelta(days=age), bpm, DfaQuality.GOOD)
        for age, bpm in ((6, 127.0), (15, 129.0), (24, 128.0), (33, 130.0))
    ]
    return estimate_view(
        first_threshold_estimate(sessions, lambda _day: 168.0, TODAY), 168.0
    )


def _lt2_estimates() -> dict[str, Any]:
    """The LT2 estimates as the service composes them: DFA-a1 crossings of three
    hard runs and the withheld critical speed and power fits."""
    windows = tuple(
        DfaWindow(
            start_s=30.0 * step,
            heart_rate_bpm=150.0 + step / 2,
            alpha1=round(0.75 - step * 0.00625, 4),
            artifact_share=0.0,
        )
        for step in range(61)
    )
    stored = ActivityDfaAlpha1(
        estimate_bpm=127.0,
        windows=len(windows),
        artifact_share=0.01,
        quality=DfaQuality.GOOD,
        method=3,
        series=windows,
    )
    dfa = dfa_second_threshold(
        [(TODAY - timedelta(days=age), stored) for age in (5, 14)],
        TODAY,
        first_threshold_bpm=129.0,
    )
    fits = [
        {
            "sport": sport,
            "environment": "outdoor",
            "status": "insufficient_data",
            "reason": "Keine unabhängigen 3, 7, 12-Minuten-Belastungen in 42 Tagen.",
        }
        for sport in ("run", "ride")
    ]
    return {
        "run": {
            "dfa_alpha1": dfa,
            "critical_speed": critical_second_threshold(fits, "run"),
        },
        "ride": {
            "dfa_alpha1": {
                "status": "unknown",
                "reason": "nur aus Läufen",
                "sessions": 0,
            },
            "critical_power": critical_second_threshold(fits, "ride"),
        },
    }


class _ProviderPerformanceReader(CoachPerformanceContextReader):
    """current_performance through production's available path (#4323, #4325): a
    synced Garmin athlete's thresholds, recovery, load and sessions, and an
    adopted 129 bpm LT1, so the budget measures what a real turn carries, not
    the unavailable shape."""

    def current_performance(self, canonical: Any = None) -> Any:
        payload = _garmin_payload()
        return coach_performance_projection(
            current_performance_context(
                garmin_sync_status(payload),
                {
                    "timezone": "Europe/Berlin",
                    "first_threshold_hr": "129",
                    "first_threshold_origin": "estimated",
                    "height_cm": 178,
                    "sleep_target_hours": 8,
                },
                TODAY,
                canonical=_performance_inputs(payload),
                session_feedback=self._session_feedback,
                lt1_session_records=self._lt1_session_records,
                chest_strap_max_hr=self._chest_strap_max_hr,
            )
        )


def _build_service() -> CoachTrainingContextService:
    events = _calendar_events()
    planned = _planned_units()
    planned_service = Mock()
    planned_service.list.side_effect = lambda limit, future_only=True, since=None: [
        item for item in planned if since is None or item["date"] >= since.isoformat()
    ][:limit]
    weather_service = Mock()
    weather_service.state.return_value = {
        "days": [
            {"date": (TODAY + timedelta(days=n)).isoformat(), "rain_probability": n}
            for n in range(14)
        ]
    }
    daily_service = Mock()
    daily_service.build.side_effect = (
        lambda planned, weather, checkins, calendar_events: (
            build_daily_planning_context(
                planned=planned,
                checkins=checkins,
                calendar_events=calendar_events,
                weather_days=weather.get("days", []),
                recovery_by_date={},
                health_by_date={},
                activity_feedback=[],
                today=TODAY,
                calendar_window_days=56,
            )
        )
    )
    calendar_reader = Mock()
    calendar_reader.list_events.return_value = events
    calendar_reader.list_events_in_window.side_effect = lambda days: [
        event
        for event in events
        if event["event_date"] <= (TODAY + timedelta(days=days)).isoformat()
    ]
    checkins = Mock()
    checkins.context.return_value = {
        "recent": [
            {
                "checkin_date": (TODAY - timedelta(days=n)).isoformat(),
                "mood": "good",
                "notes": "synthetic note",
            }
            for n in range(14)
        ]
    }
    competitions = Mock()
    competitions.list.return_value = [
        {"id": n, "name": f"Race {n}", "event_date": "2026-12-01", "priority": 1}
        for n in range(3)
    ]
    plans = Mock()
    plans.list.return_value = [
        {"id": f"plan-{n}", "name": f"Plan {n}", "start": "2026-09-01"}
        for n in range(100)
    ]
    previews = Mock()
    previews.latest_preview.return_value = {"status": "none"}
    previews.status.return_value = {"available": False}
    profile = Mock()
    profile.get.return_value = {"name": "Athlete", "source": "local"}
    payload = Mock()
    payload.sync_status.return_value = GarminSyncStatus()
    projection = Mock()
    projection.coach_context.return_value = {"recovery": {"sleep": "synthetic"}}
    feedback = Mock()
    feedback.context.return_value = {"recent": []}
    structured = CoachStructuredContextService(
        checkins,
        weather_service,
        feedback,
        CoachPlanningContextReader(
            planned_service,
            daily_service,
            calendar_reader,
            competitions,
            plans,
            previews,
            lambda: TODAY,
            # The easy runs' caps resolve to bpm, as in production (#4286).
            lthr_for=lambda: lambda unit: 168.0,
        ),
        _ProviderPerformanceReader(
            profile,
            payload,
            projection,
            lambda: TODAY,
            activity_rows=ActivitySummarySource(
                ReadOnlyManager,
                lambda: FixedActivityRows(_activities()),
                coverage=lambda: StoredCoverage("2026-10-07T06:00:00+00:00"),
            ),
            lt1_session_records=_lt1_session_records,
            chest_strap_max_hr=lambda _since: (195.0, "2026-09-14"),
        ),
        zone_configurations=_zone_configurations,
        planning_brief=lambda: {"weeks": "synthetic brief " * 400},
        today=lambda: TODAY,
        first_threshold_estimate=_first_threshold_estimate,
        lt2_estimates=_lt2_estimates,
    )
    library = Mock()
    library.list.return_value = []
    return CoachTrainingContextService(
        structured,
        library,
        local_planned_limit=50,
        library_limit=12,
        library_description_limit=1500,
        section_limits=COACH_CONTEXT_SECTION_LIMITS,
        total_char_limit=120_000,
        activity_limit_per_sport=5,
        planned_event_limit=50,
    )


def measure() -> dict[str, dict[str, Any]]:
    service = _build_service()
    result = {}
    for label, (message, kind) in CASES.items():
        selection = select_coach_context(message, {}, request_kind=kind)
        text = service.build(selection=selection, local_date=TODAY.isoformat())
        start = text.index(MARKER) + len(MARKER)
        context = json.loads(
            text[start : text.index("\nLOCAL TRAINING LIBRARY", start)]
        )
        sizes = {
            key: len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
            for key, value in context.items()
            if key != "projection"
        }
        result[label] = {
            "selection": selection.name,
            "sections": sorted(sizes),
            "sizes": sizes,
            "total": len(text),
            # The fixed rules and headers before the context JSON (#4287).
            "static": start,
            "dynamic": len(text) - start,
            "context": context,
        }
    return result


COMMON = {
    "activities",
    "activity_feedback",
    "current_performance",
    "durable_profile",
    "local_feedback",
    "local_planned_workouts",
    "source_policy",
    "target_competitions",
}
PLAN = COMMON | {
    "daily_planning_context",
    "external_calendar",
    "planning",
    "planning_brief",
    "recovery",
    "training_plans",
    "weather",
}
# The fixed Coach rules and context headers, the same for every kind, on a
# budget of their own (#4287): a rule edit raises only this one and never moves
# a kind's dynamic measurement. It includes rules 8h (rating scales, #1671),
# 8i (fitness, fatigue and form) and 8f (strength exception, #1726) and 2b
# (infection hint, #3762).
# Directives moved from source_policy into the rules (#4288 C7, orchestrator V12
# option B, 2026-11-27): each section's measured rule growth. The static ceiling
# rises by exactly that and each kind carrying the section drops by the same.
MOVED_DIRECTIVES: dict[str, int] = {
    "current_performance": 296,
    "nutrition_products": 170,
}
STATIC_PROMPT_CEILING = 14_400 + sum(MOVED_DIRECTIVES.values())


def moved(kind: str) -> int:
    """The moved directives of the sections this kind carries (#4288 C7)."""
    return MOVED_DIRECTIVES["current_performance"] + (
        MOVED_DIRECTIVES["nutrition_products"] if kind == "nutrition_message" else 0
    )


# The measured size of production's available current_performance per kind,
# which the fixture rendered in the unavailable shape until #4323. A strictly
# mechanical re-baseline (orchestrator V12, option A, 2026-11-27): each kind keeps
# exactly the raw headroom it had. #4325 pays it back: every character trimmed
# from the dedupe of thresholds and recovery lowers these values, and freed room
# is never spent elsewhere. #4325 trimmed 634 characters from every kind with
# the Garmin recovery section (thresholds once, one recovery block) and 329 from
# nutrition_message (thresholds once). The fixture was corrected once more against
# the 2.2.41 production counts (orchestrator V12 approval, 2026-11-27, #4325): a
# synced Garmin athlete with sessions, zones and estimates; each kind's value rose
# by exactly its measured growth.
PRODUCTION_SNAPSHOT_SHAPE: dict[str, int] = {
    "session_analysis": 16_625,
    "progress_review": 16_625,
    "block_review": 16_625,
    "plan_week": 11_891,
    "plan_block": 11_891,
    "adjust_week": 11_891,
    "morning_checkin": 11_891,
    "free_text_planning": 11_891,
    "general_question": 11_891,
    "nutrition_message": 13_081,
}
# Exact present sections and a character ceiling for each kind's dynamic part
# (context JSON, library, closing note). Each base number plus
# STATIC_PROMPT_CEILING is the whole-context ceiling the test held before #4287;
# since #4323 the ceiling adds PRODUCTION_SNAPSHOT_SHAPE, the one approved rise.
EXPECTED: dict[str, tuple[set[str], int]] = {
    "session_analysis": (
        COMMON | {"recovery"},
        12_900
        + PRODUCTION_SNAPSHOT_SHAPE["session_analysis"]
        - moved("session_analysis"),
    ),
    # Progress and block review are one review with the same sections.
    "progress_review": (
        COMMON | {"planning", "recovery", "training_plans"},
        17_900
        + PRODUCTION_SNAPSHOT_SHAPE["progress_review"]
        - moved("progress_review"),
    ),
    "block_review": (
        COMMON | {"planning", "recovery", "training_plans"},
        17_900 + PRODUCTION_SNAPSHOT_SHAPE["block_review"] - moved("block_review"),
    ),
    "plan_week": (
        PLAN,
        36_400 + PRODUCTION_SNAPSHOT_SHAPE["plan_week"] - moved("plan_week"),
    ),
    "plan_block": (
        PLAN,
        50_400 + PRODUCTION_SNAPSHOT_SHAPE["plan_block"] - moved("plan_block"),
    ),
    "adjust_week": (
        PLAN,
        32_400 + PRODUCTION_SNAPSHOT_SHAPE["adjust_week"] - moved("adjust_week"),
    ),
    "morning_checkin": (
        COMMON | {"daily_planning_context", "recovery"},
        12_900
        + PRODUCTION_SNAPSHOT_SHAPE["morning_checkin"]
        - moved("morning_checkin"),
    ),
    "free_text_planning": (
        PLAN,
        36_400
        + PRODUCTION_SNAPSHOT_SHAPE["free_text_planning"]
        - moved("free_text_planning"),
    ),
    "general_question": (
        COMMON | {"recovery"},
        11_900
        + PRODUCTION_SNAPSHOT_SHAPE["general_question"]
        - moved("general_question"),
    ),
    "nutrition_message": (
        COMMON | {"nutrition_products"},
        11_900
        + PRODUCTION_SNAPSHOT_SHAPE["nutrition_message"]
        - moved("nutrition_message"),
    ),
}


def present(row: dict[str, Any]) -> set[str]:
    return {key for key, value in row["context"].items() if value is not None} - {
        "projection"
    }


class CoachContextBudgetTests(unittest.TestCase):
    measured: ClassVar[dict[str, dict[str, Any]]]

    @classmethod
    def setUpClass(cls) -> None:
        cls.measured = measure()
        if os.environ.get("BUDGET_TABLE"):
            for label, row in cls.measured.items():
                print(f"\n{label} [{row['selection']}] total={row['total']}")
                for key, size in sorted(row["sizes"].items()):
                    print(f"  {key}: {size}")

    def test_sections_and_budget_per_kind(self) -> None:
        self.assertEqual(set(CASES), set(EXPECTED))
        static = {row["static"] for row in self.measured.values()}
        self.assertEqual(len(static), 1)  # one fixed prompt for every kind
        self.assertLessEqual(static.pop(), STATIC_PROMPT_CEILING)
        for label, (sections, ceiling) in EXPECTED.items():
            with self.subTest(case=label):
                row = self.measured[label]
                self.assertEqual(sections, present(row))
                self.assertLessEqual(row["dynamic"], ceiling)

    def test_every_kind_keeps_one_percent_headroom(self) -> None:
        # #4100: two PRs that each fit can together exceed a ceiling on
        # develop; a PR that eats the last 1 % fails in its own CI instead.
        # Make room by trimming context text, never by raising the ceiling. The
        # one exception: a fixture-shape correction that keeps every kind's
        # headroom identical (PRODUCTION_SNAPSHOT_SHAPE, #4323).
        # The fixed prompt and each kind's dynamic part keep it each (#4287).
        static = STATIC_PROMPT_CEILING
        for label, (_sections, ceiling) in EXPECTED.items():
            with self.subTest(case=label):
                row = self.measured[label]
                self.assertLessEqual(row["static"], static - static // 100)
                self.assertLessEqual(row["dynamic"], ceiling - ceiling // 100)

    def test_a_rule_edit_leaves_every_dynamic_measurement(self) -> None:
        # #4287: the fixed prompt grows on its own budget, never a kind's.
        with patch.object(
            coach_context, "COACH_PROMPT", coach_context.COACH_PROMPT + "x" * 500
        ):
            grown = measure()
        for label, row in grown.items():
            with self.subTest(case=label):
                self.assertEqual(row["dynamic"], self.measured[label]["dynamic"])
                self.assertEqual(row["static"], self.measured[label]["static"] + 500)

    def test_source_policy_describes_and_the_rules_direct(self) -> None:
        # #4288 C7: directives live in the trusted rules, not in the untrusted
        # athlete_context block; source_policy only describes its sections.
        policy = _build_service()._structured_context_service.build()["source_policy"]
        for key, moved_text in (
            ("current_performance", "nenne zone_source"),
            ("current_performance", "mit save_zones übernehmen"),
            ("current_performance", "erkläre Laufdeckel"),
            ("current_performance", "nenne own_sessions"),
            ("nutrition_products", "lookup_food"),
            ("nutrition_products", "direkt als Komponenten speichern"),
        ):
            with self.subTest(moved=moved_text):
                self.assertNotIn(moved_text, policy[key])
        self.assertIn("8j. current_performance: name zone_source", COACH_PROMPT)
        self.assertIn("use lookup_food without source", COACH_PROMPT)

    def test_the_performance_sizes_count_each_block_as_sent(self) -> None:
        # #4323: the diagnostics count what the Coach's current_performance holds.
        structured = _build_service()._structured_context_service
        sizes = structured.performance_section_sizes()
        current = structured.build()["current_performance"]
        self.assertEqual(sizes["total"], coach_context.coach_context_json_size(current))
        self.assertEqual(set(sizes) - {"total", "total_other_kinds"}, set(current))
        self.assertTrue(all(type(value) is int for value in sizes.values()))
        planning = CoachContextSelection("plan_week", horizon_days=7).project(
            structured.build(), "2026-11-27"
        )["current_performance"]
        self.assertEqual(
            sizes["total_other_kinds"], coach_context.coach_context_json_size(planning)
        )
        self.assertLess(sizes["total_other_kinds"], sizes["total"])

    def test_a_given_structured_build_gives_the_identical_text(self) -> None:
        """The preview's one build yields the same text as the text's own build (#2966)."""
        service = _build_service()
        own = service.build()
        structured = service._structured_context_service.build()
        reused = service.build(structured_context=copy.deepcopy(structured))
        self.assertEqual(own, reused)
        self.assertIn(MARKER, own)

    def test_free_text_planning_gets_the_brief_and_playbook(self) -> None:
        selection = select_coach_context("Plane meine nächste Woche", {})
        self.assertEqual(("plan_week", "plan_week"), (selection.name, selection.kind))
        self.assertIn("planning_brief", selection.sections)
        self.assertIn(
            "Task: plan the requested next week", playbook_for(selection.kind)
        )

    def test_typed_planning_maps_to_the_fixed_kinds(self) -> None:
        cases = {
            "Plane meine nächste Woche": "plan_week",
            "Plan next week": "plan_week",
            "Erstelle mir einen Trainingsplan für die Woche": "plan_week",
            "Kannst du das Training fuer naechste Woche planen?": "plan_week",
            "Plane die nächsten 4 Wochen": "plan_block",
            "Please schedule my next 3 weeks": "plan_block",
            "Plane einen Aufbau-Block": "plan_block",
            "Passe die Woche an": "adjust_week",
            "Passe die nächste Woche an": "plan_week",
            "Adjust next week": "plan_week",
            "Adjust this week": "adjust_week",
        }
        for message, kind in cases.items():
            with self.subTest(message=message):
                self.assertEqual(kind, select_coach_context(message, {}).kind)

    def test_ordinary_questions_mentioning_a_week_stay_unplanned(self) -> None:
        for message in (
            "What do I have scheduled this week?",
            "Erstelle mir eine Zusammenfassung der letzten Woche",
            "Hab ich erledigt, was für diese Woche anstand?",
            "Wie war meine Woche?",
            "Wie sieht mein Plan für diese Woche aus?",
            "Wie viele Kilometer bin ich diese Woche gelaufen?",
            "Wie war meine letzte Woche?",
            "Was ist ein Trainingsblock?",
            "Passe meinen Lauf am Freitag an",
            "Verschiebe den Lauf auf Freitag",
            "Wie geht es meiner Erholung?",
        ):
            with self.subTest(message=message):
                selection = select_coach_context(message, {})
                self.assertIsNone(selection.kind)
                self.assertNotIn("planning_brief", selection.sections)

    def test_readiness_wording_loads_recovery(self) -> None:
        for message in ("Bin ich bereit fürs Training?", "How is my readiness?"):
            with self.subTest(message=message):
                sections = select_coach_context(message, {}).sections
                self.assertIn("recovery", sections)

    def test_blocks_beyond_the_window_keep_complete_context(self) -> None:
        near = select_coach_context("Plane die nächsten 4 Wochen", {})
        self.assertEqual(("plan_block", 35), (near.name, near.horizon_days))
        far = select_coach_context("Plane die nächsten 5 Wochen", {})
        self.assertEqual("plan_block", far.kind)
        self.assertIsNone(far.horizon_days)

    def test_typed_planning_with_explicit_dates_keeps_complete_context(self) -> None:
        selection = select_coach_context("Plane die Woche ab 2026-12-01", {})
        self.assertEqual("plan_week", selection.kind)
        self.assertIsNone(selection.horizon_days)
        self.assertIn("planning_brief", selection.sections)
        self.assertNotIn("calendar", selection.sections)
        self.assertIn("external_calendar", selection.sections)

    def test_typed_planning_keeps_dialogue_and_pending_retention(self) -> None:
        pending = {"pending_request": {"status": "failed", "source_message_ids": [1]}}
        # message, dialogue, receipts, retain_dialogue, retain_pending_request
        for message, dialogue, receipts, dialogue_kept, pending_kept in (
            ("Kannst du das für die Woche planen?", {}, False, True, False),
            ("Plane meine nächste Woche", {}, False, False, False),
            ("Plane meine nächste Woche", {}, True, True, False),
            ("Plane meine nächste Woche", pending, False, False, False),
        ):
            with self.subTest(message=message, receipts=receipts):
                typed = select_coach_context(message, dialogue, has_receipts=receipts)
                self.assertEqual("plan_week", typed.kind)
                self.assertEqual(dialogue_kept, typed.retain_dialogue)
                self.assertEqual(pending_kept, typed.retain_pending_request)

    def test_system_quick_actions_never_take_a_typed_planning_kind(self) -> None:
        selection = select_coach_context(
            "Plane meine nächste Woche", {}, request_kind="morning_checkin"
        )
        self.assertEqual("morning_checkin", selection.kind)
        self.assertNotIn("planning_brief", selection.sections)

    def test_daily_context_rides_only_on_planning_morning_and_today_turns(
        self,
    ) -> None:
        daily = {
            label
            for label, row in self.measured.items()
            if "daily_planning_context" in present(row)
        }
        self.assertEqual(
            {
                "plan_week",
                "plan_block",
                "adjust_week",
                "morning_checkin",
                "free_text_planning",
            },
            daily,
        )
        self.assertIn(
            "daily_planning_context",
            select_coach_context("Was trainiere ich heute?", {}).sections,
        )
        self.assertNotIn(
            "daily_planning_context",
            select_coach_context("Analysiere meine letzte Einheit", {}).sections,
        )

    def test_planning_turns_carry_each_calendar_event_once(self) -> None:
        for label in ("plan_week", "plan_block", "adjust_week", "free_text_planning"):
            with self.subTest(case=label):
                row = self.measured[label]
                self.assertNotIn("calendar", present(row))
                events = row["context"]["external_calendar"]["events"]
                self.assertTrue(events)
                text = json.dumps(row["context"], ensure_ascii=False)
                for event in events:
                    self.assertEqual(1, text.count(f'"{event["name"]}"'))
                self.assertIn("omitted_count", row["context"]["external_calendar"])

    def test_review_turns_have_no_future_and_analysis_at_most_two_days(self) -> None:
        for label, ahead in (
            ("progress_review", 0),
            ("block_review", 0),
            ("session_analysis", 2),
        ):
            with self.subTest(case=label):
                last = (TODAY + timedelta(days=ahead)).isoformat()
                planned = self.measured[label]["context"]["local_planned_workouts"]
                self.assertTrue(planned)
                latest = max(item["date"] for item in planned)
                self.assertLessEqual(latest, last)

    def test_progress_and_block_review_load_the_same_sections(self) -> None:
        self.assertEqual(
            present(self.measured["progress_review"]),
            present(self.measured["block_review"]),
        )
        self.assertIn("training_plans", present(self.measured["progress_review"]))

    def test_every_removed_section_stays_loadable_by_the_read_tool(self) -> None:
        tool = next(
            item
            for item in server.COACH_DIALOGUE_TOOLS
            if item["name"] == "read_coach_context"
        )
        offered = set(tool["parameters"]["properties"]["sections"]["items"]["enum"])
        self.assertEqual(CONTEXT_SECTIONS, offered)
        for label, (message, kind) in CASES.items():
            with self.subTest(case=label):
                selection = select_coach_context(message, {}, request_kind=kind)
                self.assertLessEqual(CONTEXT_SECTIONS - selection.sections, offered)

    def test_size_diagnostics_name_the_selection_and_sections_for_every_kind(
        self,
    ) -> None:
        service = _build_service()
        for label, (message, kind) in CASES.items():
            with self.subTest(case=label):
                selection = select_coach_context(message, {}, request_kind=kind)
                with self.assertLogs("intervals_coach", level="INFO") as captured:
                    service.build(selection=selection, local_date=TODAY.isoformat())
                record = next(
                    entry
                    for entry in captured.records
                    if getattr(entry, "event", None) == "coach_context_projection"
                )
                fields = vars(record)["context"]
                self.assertEqual(selection.name, fields["context_profile"])
                self.assertEqual(selection.kind, fields["request_kind"])
                sizes = fields["section_characters"]
                self.assertEqual(
                    present(self.measured[label]) | {"projection"}, set(sizes)
                )
                self.assertTrue(all(size > 0 for size in sizes.values()))


if __name__ == "__main__":
    unittest.main()

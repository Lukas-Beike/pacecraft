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
from datetime import date, timedelta
from typing import Any, ClassVar
from unittest.mock import Mock, patch

from activity_row_support import FixedActivityRows, ReadOnlyManager, StoredCoverage
from canonical_recovery_support import recovery_only_inputs
from performance_day_support import canonical_from_snapshot
from server_test_support import server

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
from backend.performance.context import current_performance_context
from backend.performance.freshness import GarminSyncStatus
from backend.planning.compact_activities import ActivitySummarySource
from backend.planning.context import build_daily_planning_context

TODAY = date(2026, 10, 7)
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


def _provider_snapshot() -> dict[str, Any]:
    """A synced athlete: thresholds, weight, form and recent sessions (#4323)."""
    return {
        "synced_at": f"{TODAY.isoformat()}T05:00:00Z",
        "athlete": {
            "icu_ftp": 250,
            "icu_w_prime": 20000,
            "max_hr": 190,
            "lthr": 167,
            "weight": 72.0,
        },
        "recent_wellness": [
            {
                "id": (TODAY - timedelta(days=n)).isoformat(),
                "ctl": 60 - n,
                "atl": 66 - n,
                "tsb": -6,
                "restingHR": 48 + n % 3,
                "hrv": 58 - n % 4,
                "sleepSecs": 26_000 + 300 * (n % 5),
            }
            for n in range(28)
        ],
        "recent_activities": [
            {
                "id": f"session-{n}",
                "type": "Run" if n % 3 else "Ride",
                "start_date_local": f"{(TODAY - timedelta(days=n + 1)).isoformat()}T07:00:00",
                "moving_time": 3600,
                "icu_training_load": 60 + n,
                "average_heartrate": 140,
            }
            for n in range(20)
        ],
    }


class _ProviderPerformanceReader(CoachPerformanceContextReader):
    """current_performance through production's available path (#4323): synced
    thresholds, form, recovery and sessions, and an adopted 129 bpm LT1, so the
    budget measures what a real turn carries, not the unavailable shape."""

    def current_performance(self, canonical: Any = None) -> Any:
        snapshot = _provider_snapshot()
        inputs = replace(
            canonical_from_snapshot(snapshot, TODAY),
            recovery=recovery_only_inputs(
                None, snapshot["recent_wellness"], snapshot=snapshot
            ).recovery,
        )
        return coach_performance_projection(
            current_performance_context(
                GarminSyncStatus(),
                {
                    "timezone": "Europe/Berlin",
                    "first_threshold_hr": "129",
                    "first_threshold_origin": "estimated",
                },
                TODAY,
                canonical=inputs,
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
        ),
        planning_brief=lambda: {"weeks": "synthetic brief " * 400},
        today=lambda: TODAY,
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
# is never spent elsewhere.
PRODUCTION_SNAPSHOT_SHAPE: dict[str, int] = {
    "session_analysis": 5_243,
    "progress_review": 5_243,
    "block_review": 5_243,
    "plan_week": 3_318,
    "plan_block": 3_318,
    "adjust_week": 3_318,
    "morning_checkin": 3_318,
    "free_text_planning": 3_318,
    "general_question": 3_318,
    "nutrition_message": 3_318,
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

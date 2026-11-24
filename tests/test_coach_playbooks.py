"""Request-kind playbooks: allowlist, per-turn injection and context sections."""

from __future__ import annotations

import ast
import functools
import re
import unittest
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import Mock

from backend.coach import playbooks
from backend.coach.context import coach_performance_projection
from backend.coach.context_selection import (
    REQUEST_KIND_SELECTIONS,
    free_text_review_kind,
    select_coach_context,
)
from backend.coach.job_submission import CoachJobSubmissionService
from backend.coach.playbooks import REQUEST_KINDS, nutrition_playbook, playbook_for
from backend.coach.prompt import COACH_PROMPT
from backend.coach.request_payload import (
    CoachRequestPayloadService,
    analysed_sport,
    compose_model_instructions,
)
from backend.errors import AppError
from backend.performance.current_metrics import coach_thresholds

NEW_KINDS = (
    "session_analysis",
    "progress_review",
    "block_review",
    "plan_week",
    "plan_block",
    "adjust_week",
)


# 640: the plan kinds name the running step test and its preconditions (#3833).
KIND_WORD_BUDGET = 640
# Per-block word budgets: a block that grows must justify it here.
BLOCK_WORD_BUDGETS = {
    "_EVIDENCE": 60,
    "_INTENSITY_MODEL": 40,
    "_PLAN_STRUCTURE": 130,
    "_STRENGTH_LEGS": 75,
    "_SEASON_PHASE": 75,
    "_PRECEDENCE": 30,
    # Plan targets use the resolved thresholds; name a flagged FTP conflict.
    "_THRESHOLDS": 30,
    "_CALENDAR": 45,
    "_PLAN_CHECK": 80,
    "_ADAPTIVE": 140,
    # strength_plan compares executed with planned sets (#1683).
    "_SESSION_CHECKS": 135,
    # The execution and planned-target fields the session analysis reads.
    "_SESSION_NUMBERS": 110,
    # One block per sport of the analysed session (#3038).
    "_RUN_SESSION": 75,
    "_RIDE_SESSION": 75,
    # Test planning from planning_brief.last_tests and the step test (plan kinds only).
    "_TESTS": 160,
    "_ANSWER": 55,
    # The athlete-approved session analysis shape, its order and form rules
    # (#4136); it replaces _ANSWER for a single session.
    "_SESSION_ANSWER": 170,
    # 545: the deficit rule 6i.3 names the EA floor, loss rate and protein range (#3917).
    "_NUTRITION": 545,
}
# 205: the session analysis judges a run against its stored heart-rate cap.
TASK_WORD_BUDGET = 205

# A field name in a playbook: snake_case, or a dotted path of its segments.
_SNAKE_NAME = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_DOTTED_PATH = re.compile(r"\b[a-z][a-z0-9_]+(?:\.[a-z][a-z0-9_]+)+\b")


def _playbook_text() -> str:
    # The budget table lists every block (test_every_block_is_budgeted_...).
    blocks = [getattr(playbooks, name) for name in BLOCK_WORD_BUDGETS]
    return "\n".join([playbooks._HEADER, *blocks, *playbooks._TASKS.values()])


_ROOT = Path(__file__).resolve().parents[1]


def _parsed(path: Path) -> list[ast.AST]:
    return list(ast.walk(ast.parse(path.read_text(encoding="utf-8"))))


@functools.cache
def _backend_names() -> frozenset[str]:
    """Every string literal, TypedDict field and keyword argument of server.py
    and the backend, the playbooks aside."""
    names: set[str] = set()
    for path in [_ROOT / "server.py", *(_ROOT / "backend").rglob("*.py")]:
        if path == _ROOT / "backend" / "coach" / "playbooks.py":
            continue
        for node in _parsed(path):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                names.add(node.value)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
            elif isinstance(node, ast.keyword) and node.arg:
                names.add(node.arg)
    return frozenset(names)


def _context_threshold_keys() -> set[str]:
    """The literal keys current_performance_context puts beside coach_thresholds."""
    return {
        str(child.value)
        for node in _parsed(_ROOT / "backend" / "performance" / "context.py")
        if isinstance(node, ast.Dict)
        for key, value in zip(node.keys, node.values)
        if isinstance(key, ast.Constant)
        and key.value == "thresholds"
        and isinstance(value, ast.Dict)
        for child in value.keys
        if isinstance(child, ast.Constant)
    }


def _built_keys() -> dict[str, set[str]]:
    """Blocks whose children are checked: what their code builds from complete
    inputs. ``weather`` names both the session's weather and the forecast, so
    its children are only checked by name."""
    value = {"value": 250, "source": "Garmin Connect"}
    metrics = {
        key: value
        for key in ("max_hr_bpm", "bike_threshold_hr_bpm", "run_threshold_hr_bpm")
    }
    metrics["cycling_ftp_watts"] = {
        **value,
        "alternatives": [{"source": "Wahoo", "value": 270}],
        "conflict": True,
    }
    return {"thresholds": set(coach_thresholds(metrics)) | _context_threshold_keys()}


def _misplaced(paths: set[str], built: dict[str, set[str]]) -> list[str]:
    """Dotted paths naming a child their built parent block lacks."""
    return sorted(
        path
        for path in paths
        for parent, child in zip(path.split("."), path.split(".")[1:])
        if parent in built and child not in built[parent]
    )


class PlaybookFieldNameTests(unittest.TestCase):
    """A field the playbooks name must still exist (#3055): a rename or removal
    elsewhere fails here instead of orphaning the instruction."""

    def test_every_named_field_exists_in_the_backend(self) -> None:
        text = _playbook_text()
        names = set(_SNAKE_NAME.findall(text)) | {
            segment
            for path in _DOTTED_PATH.findall(text)
            for segment in path.split(".")
        }
        self.assertGreater(len(names), 100)
        self.assertEqual([], sorted(names - _backend_names()))

    def test_dotted_paths_match_the_blocks_their_code_builds(self) -> None:
        built = _built_keys()
        self.assertLessEqual(
            {"ftp_w", "weight", "first_thresholds"}, built["thresholds"]
        )
        paths = set(_DOTTED_PATH.findall(_playbook_text()))
        self.assertIn("current_performance.thresholds.ftp_w", paths)
        self.assertEqual([], _misplaced(paths, built))
        # The #3046 slip: an FTP key of the metrics, not of the thresholds.
        self.assertEqual(
            ["current_performance.thresholds.cycling_ftp_watts"],
            _misplaced({"current_performance.thresholds.cycling_ftp_watts"}, built),
        )


class PlaybookBudgetTests(unittest.TestCase):
    def test_each_block_stays_within_its_budget(self) -> None:
        for name, budget in BLOCK_WORD_BUDGETS.items():
            with self.subTest(block=name):
                self.assertLessEqual(len(getattr(playbooks, name).split()), budget)

    def test_each_task_stays_within_its_budget(self) -> None:
        for kind, task in playbooks._TASKS.items():
            with self.subTest(kind=kind):
                self.assertLessEqual(len(task.split()), TASK_WORD_BUDGET)

    def test_every_block_is_budgeted_and_every_kind_composed(self) -> None:
        blocks = {
            name
            for name, value in vars(playbooks).items()
            if isinstance(value, str)
            and re.fullmatch(r"_[A-Z_]+", name)
            and name != "_HEADER"
        }
        self.assertEqual(blocks, set(BLOCK_WORD_BUDGETS))
        self.assertEqual(set(playbooks._BLOCKS), set(REQUEST_KINDS))

    def test_plan_check_names_every_blocking_check(self) -> None:
        from backend.planning import plan_checks

        for code in (*plan_checks.BLOCKING_CODES, *plan_checks.REQUIRED_CODES):
            with self.subTest(code=code):
                self.assertIn(code, playbooks._PLAN_CHECK)
        self.assertIn("hr_cap", playbooks._PLAN_STRUCTURE)

    def test_kinds_carry_only_the_blocks_they_need(self) -> None:
        for kind in ("session_analysis", "progress_review", "block_review"):
            text = playbook_for(kind)
            with self.subTest(kind=kind):
                for block in (
                    playbooks._SEASON_PHASE,
                    playbooks._PLAN_CHECK,
                    playbooks._ADAPTIVE,
                    playbooks._STRENGTH_LEGS,
                ):
                    self.assertNotIn(block, text)
        self.assertIn(playbooks._ADAPTIVE, playbook_for("adjust_week"))
        self.assertNotIn(playbooks._ADAPTIVE, playbook_for("plan_week"))
        for block in playbooks._BLOCKS["plan_block"]:
            self.assertIn(block, playbook_for("plan_week"))

    def test_nutrition_playbook_is_empty_without_nutrition(self) -> None:
        self.assertEqual(nutrition_playbook(False), "")
        self.assertIn("6i.", nutrition_playbook(True))


class RequestKindAllowlistTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = CoachJobSubmissionService(
            lambda: Mock(),
            Mock(),
            Mock(),
            Mock(),
            Mock(),
            Mock(),
            lambda: "synthetic-time",
            background_horizon_days=30,
        )

    def validate(self, kind):
        return self.service._validate_submission("Hallo", "turn-1", kind, [])

    def test_new_kinds_and_morning_checkin_are_accepted(self) -> None:
        self.assertEqual(set(NEW_KINDS), set(REQUEST_KINDS))
        for kind in (*NEW_KINDS, "morning_checkin", None, ""):
            with self.subTest(kind=kind):
                self.assertEqual(self.validate(kind)[2], kind or None)

    def test_quick_actions_use_the_planning_model_and_the_rest_the_conversation(
        self,
    ) -> None:
        settings = Mock()
        settings.selected_model.side_effect = lambda task: f"model-{task}"
        settings.selected_thinking_level.side_effect = lambda task: f"level-{task}"
        service = CoachJobSubmissionService(
            lambda: Mock(),
            Mock(),
            settings,
            Mock(),
            Mock(),
            Mock(),
            lambda: "synthetic-time",
            background_horizon_days=30,
        )
        for kind in NEW_KINDS:
            with self.subTest(kind=kind):
                self.assertEqual(
                    service._model_settings(kind),
                    ("model-planning", "level-planning"),
                )
        for other in ("morning_checkin", None):
            with self.subTest(kind=other):
                self.assertEqual(
                    service._model_settings(other),
                    ("model-conversation", "level-conversation"),
                )

    def test_unknown_kind_is_rejected(self) -> None:
        with self.assertRaises(AppError) as raised:
            self.validate("plan_year")
        self.assertEqual(raised.exception.reason, "invalid_request_kind")


class PlaybookInjectionTests(unittest.TestCase):
    def test_each_kind_has_a_bounded_playbook_with_the_common_rules(self) -> None:
        for kind in NEW_KINDS:
            with self.subTest(kind=kind):
                text = playbook_for(kind)
                self.assertLessEqual(len(text.split()), KIND_WORD_BUDGET)
                self.assertIn("Never use ACWR", text)
                self.assertIn("pyramidal when it is absent", text)
                self.assertIn("training_model", text)

    def test_session_analysis_adds_only_the_analysed_sports_block(self) -> None:
        core = playbook_for("session_analysis")
        for sport, block, other in (
            ("run", playbooks._RUN_SESSION, playbooks._RIDE_SESSION),
            ("ride", playbooks._RIDE_SESSION, playbooks._RUN_SESSION),
        ):
            text = playbook_for("session_analysis", sport)
            with self.subTest(sport=sport):
                self.assertLessEqual(len(text.split()), KIND_WORD_BUDGET)
                self.assertIn(block, text)
                self.assertNotIn(other, text)
                # The answer format stays last.
                self.assertLess(
                    text.index(block), text.index(playbooks._SESSION_ANSWER)
                )
                self.assertNotIn(block, core)
        for no_block in (None, "swim", "strength"):
            self.assertEqual(core, playbook_for("session_analysis", no_block))
        self.assertEqual(
            playbook_for("progress_review"), playbook_for("progress_review", "run")
        )
        # Temperature comes from the weather, never a device sensor (#3036).
        self.assertIn("weather.temperature_c", core)

    def test_session_analysis_carries_the_session_checks_only(self) -> None:
        text = playbook_for("session_analysis", "run")
        for needle in ("session_focus", "pace_at_heart_rate", "never a proven cause"):
            self.assertIn(needle, text)
        self.assertIn("not_covered", text)
        self.assertNotIn(playbooks._SESSION_CHECKS, playbook_for("block_review"))

    def test_kind_specific_content(self) -> None:
        self.assertIn("get_activity_details", playbook_for("session_analysis"))
        self.assertIn("get_training_report", playbook_for("progress_review"))
        self.assertNotIn("Planning science", playbook_for("progress_review"))
        for kind in ("plan_week", "plan_block"):
            self.assertIn("Planning science", playbook_for(kind))
        for kind in ("plan_week", "plan_block"):
            self.assertIn("season_phase", playbook_for(kind))
        self.assertIn("season_phase", playbook_for("adjust_week"))
        self.assertIn("week-by-week rationale", playbook_for("plan_block"))
        self.assertNotIn("week-by-week rationale", playbook_for("plan_week"))

    def test_review_kinds_share_the_answer_format_and_plain_language(self) -> None:
        self.assertIn("explain a term like fitness or form", COACH_PROMPT)
        for kind in ("progress_review", "block_review"):
            with self.subTest(kind=kind):
                self.assertIn(playbooks._ANSWER, playbook_for(kind))
        # A single session has its own athlete-approved shape (#4136).
        self.assertIn(playbooks._SESSION_ANSWER, playbook_for("session_analysis"))
        self.assertNotIn(playbooks._ANSWER, playbook_for("session_analysis"))
        for kind in ("plan_week", "plan_block", "adjust_week"):
            self.assertNotIn(playbooks._ANSWER, playbook_for(kind))

    def test_session_analysis_explains_the_report_and_drops_no_notable_check(
        self,
    ) -> None:
        # The per-type checks are code now (session_report, #3084); the task
        # explains the report, names every notable and every unknown, and never
        # computes numbers itself. The answer leads with the plan (#4136).
        text = playbook_for("session_analysis")
        for marker in (
            "by default the last one",
            "session_report holds every deterministic check",
            "never compute numbers or verdicts yourself",
            "Name every notable check",
            "each unknown with its reason",
            "cite a field and its value",
            "sensor errors",
            "Plan first",
            "same type only",
            "besser, gleich or schlechter",
            "energy_before",
            "Minutes, never seconds",
            "ground contact, only if recorded",
            "training-effect or threshold-pace label",
            "du-form",
            "heart_rate_change_bpm",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, text)

    def test_session_analysis_reads_execution_and_planned_targets(self) -> None:
        text = playbook_for("session_analysis")
        for marker in ("analysis.execution", "planned_targets", "comparable_sessions"):
            self.assertIn(marker, text)
        self.assertIn("lap_pace", playbook_for("session_analysis", "run"))
        ride = playbook_for("session_analysis", "ride")
        # An ERG ride's progress is its heart rate at the same power (athlete,
        # 2026-11-24, #4136).
        self.assertIn("heart rate at the same power", ride)
        for marker in (
            "erg_like",
            "normalized_power",
            "decoupling_pct",
            "current_performance.thresholds.ftp_w",
        ):
            self.assertIn(marker, ride)
        self.assertNotIn(playbooks._SESSION_NUMBERS, playbook_for("progress_review"))

    def test_progress_and_block_review_are_one_fitness_review(self) -> None:
        review = playbook_for("progress_review")
        self.assertEqual(review, playbook_for("block_review"))
        for marker in (
            "getting fitter",
            "sections [block_review]",
            "56 for 8",
            "since_last_test true",
            "equal_effort",
            "only when they may explain missing progress",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, review)
        self.assertNotIn("Pace at heart rate is not covered", review)

    def test_plans_schedule_tests_and_analyses_judge_them(self) -> None:
        for kind in ("plan_week", "plan_block"):
            text = playbook_for(kind)
            with self.subTest(kind=kind):
                for marker in (
                    "critical-speed test",
                    "Zwift FTP ramp test",
                    "6 to 8 weeks",
                    "last 2 weeks before an A race",
                    "control run",
                    "30-min time trial",
                ):
                    self.assertIn(marker, text)
        self.assertIn("planning_brief.last_tests", playbook_for("plan_week"))
        self.assertIn("session_focus easy", playbook_for("plan_week"))
        # A test's evaluation comes with its own session result, not every analysis.
        self.assertNotIn(playbooks._TESTS, playbook_for("session_analysis"))
        self.assertNotIn("about 75 %", playbook_for("session_analysis"))
        # A ramp test reads power even when erg_like; its evaluation decides.
        self.assertIn(
            "unless focus.test_evaluation says otherwise",
            playbook_for("session_analysis", "ride"),
        )

    def test_adjust_week_playbook_guards_hard_days_and_writes(self) -> None:
        text = playbook_for("adjust_week")
        for marker in (
            "get_training_report",
            "session_focus",
            "never next to another hard day",
            "24 h",
            "preview_adaptive_replan",
            "No Intervals.icu write without a named sync",
        ):
            self.assertIn(marker, text)
        self.assertNotIn("Planning science", text)

    def test_adjust_week_context_is_its_own(self) -> None:
        sections = select_coach_context("ok", {}, request_kind="adjust_week").sections
        self.assertLessEqual(
            {
                "planning_brief",
                "planning",
                "training_plans",
                "recovery",
                "external_calendar",
                "daily_planning_context",
            },
            sections,
        )
        # Calendar events come once, as external_calendar.
        self.assertNotIn("calendar", sections)
        self.assertNotIn("nutrition_products", sections)
        selection = select_coach_context("ok", {}, request_kind="adjust_week")
        items = [{"date": f"2026-11-{day:02d}"} for day in (1, 2, 8, 10)]
        kept = selection.dated_items(items, date(2026, 11, 5))
        self.assertEqual(
            ["2026-11-01", "2026-11-02", "2026-11-08", "2026-11-10"],
            [i["date"] for i in kept],
        )
        self.assertEqual("", playbook_for("morning_checkin"))

    def test_nutrition_rules_only_on_nutrition_turns(self) -> None:
        marker = (
            "first match every ingredient against the athlete's saved local products"
        )
        plain = compose_model_instructions("ctx", allow_mutations=True)
        food = compose_model_instructions("ctx", allow_mutations=True, nutrition=True)
        self.assertNotIn(marker, plain)
        self.assertNotIn(marker, COACH_PROMPT)
        self.assertIn(marker, food)
        self.assertIn("save_nutrition_template", food)
        self.assertNotIn("save_nutrition_template", plain)
        for kind in ("plan_week", "session_analysis"):
            self.assertNotIn(
                marker,
                compose_model_instructions(
                    "ctx", allow_mutations=True, request_kind=kind
                ),
            )

    def test_other_turns_carry_no_playbook(self) -> None:
        for kind in (None, "morning_checkin", "unknown"):
            self.assertEqual(playbook_for(kind), "")
            self.assertNotIn(
                "<request_playbook>",
                compose_model_instructions(
                    "ctx", allow_mutations=True, request_kind=kind
                ),
            )

    def test_payload_instructions_include_only_the_turn_playbook(self) -> None:
        training_context = Mock()
        training_context.build.return_value = "training context"
        settings = Mock()
        settings.selected_model.return_value = "model"
        settings.selected_thinking_level.return_value = "low"
        service = CoachRequestPayloadService(training_context, settings, 1000)
        context = {
            "current_user_message_id": 1,
            "local_date": "2026-09-23",
            "timezone": "Europe/Berlin",
            "pending_request": None,
            "messages": [],
        }
        arguments: dict[str, Any] = {
            "message": "Plane meine nächste Woche.",
            "context": context,
            "command_receipts": [],
            "tools": [],
            "allow_mutations": True,
            "model": None,
            "thinking_level": None,
            "conversation_id": "c",
            "attachments": [],
            "retain_openai_attachment_context": False,
            "has_prior_openai_attachments": False,
        }
        with self.assertLogs("intervals_coach", "INFO") as logs:
            planned, _ = service.build(**arguments, request_kind="plan_week")
            plain, _ = service.build(**{**arguments, "message": "Wie war mein Schlaf?"})
            typed, _ = service.build(**arguments)
        sizes = [r.context for r in logs.records if hasattr(r, "context")]
        self.assertEqual(
            len(playbook_for("plan_week")), sizes[0]["playbook_characters"]
        )
        self.assertEqual(0, sizes[1]["playbook_characters"])
        self.assertIn("<request_playbook>", planned)
        self.assertNotIn("<request_playbook>", plain)
        # A typed planning request carries the same playbook as the quick action.
        self.assertEqual(
            len(playbook_for("plan_week")), sizes[2]["playbook_characters"]
        )
        self.assertIn("Task: plan the requested next week", typed)
        selection = training_context.build.call_args_list[0].kwargs["selection"]
        self.assertEqual(selection.name, "plan_week")

    def test_analysed_sport_prefers_one_named_sport_then_the_id_then_the_newest(
        self,
    ) -> None:
        sessions = [
            {"id": "s-ride", "sport": "ride"},
            {"id": "s-run", "sport": "run"},
            {"id": "s-swim", "sport": "swim"},
        ]
        recent = Mock(return_value=sessions)
        # The calendar button names the sport: no session read.
        button = (
            "Analysiere meine Einheit vom 2. Nov. (Laufen, 07:10, 45 min, ID s-run)."
        )
        for message, expected in (
            (button, "run"),
            ("Analysiere meine Einheit vom 2. Nov. (Rad indoor, ID s-ride).", "ride"),
            ("Analysiere meine letzte Radtour", "ride"),
            ("Ich bin gestern gelaufen, wie war das?", "run"),
        ):
            with self.subTest(message=message):
                self.assertEqual(expected, analysed_sport(message, recent))
        recent.assert_not_called()
        for message, expected in (
            # A sport without a block is read from the session of its ID.
            ("Analysiere meine Einheit vom 2. Nov. (Schwimmen, ID s-swim).", "swim"),
            ("Analysiere die Einheit mit ID s-run.", "run"),
            # Otherwise the newest session decides: no ID, an unknown one, two
            # sports, or "Rad" inside another word.
            ("Analysiere meine letzte Einheit", "ride"),
            ("Analysiere die Einheit (ID s-old)", "ride"),
            ("Erst Lauf, dann Rad: analysiere die Einheit", "ride"),
            ("Analysiere die Radaufzeichnung", "ride"),
        ):
            with self.subTest(message=message):
                self.assertEqual(expected, analysed_sport(message, recent))
        self.assertEqual(
            "run",
            analysed_sport("Analysiere die Radaufzeichnung", lambda: sessions[1:]),
        )
        self.assertIsNone(analysed_sport("Analysiere meine Einheit", tuple))

    def test_session_analysis_turn_carries_the_sport_block_of_its_session(self) -> None:
        training_context = Mock()
        training_context.build.return_value = "training context"
        recent = Mock(return_value=[{"id": "s-1", "sport": "ride"}])
        service = CoachRequestPayloadService(
            training_context, Mock(), 1000, recent_sessions=recent
        )
        arguments: dict[str, Any] = {
            "message": "Analysiere meine letzte Einheit",
            "context": {"current_user_message_id": 1, "local_date": "2026-09-23"},
            "command_receipts": [],
            "tools": [],
            "allow_mutations": True,
            "model": None,
            "thinking_level": None,
            "conversation_id": "c",
            "attachments": [],
            "retain_openai_attachment_context": False,
            "has_prior_openai_attachments": False,
        }
        with self.assertLogs("intervals_coach", "INFO") as logs:
            analysis, _ = service.build(**arguments, request_kind="session_analysis")
            planned, _ = service.build(**arguments, request_kind="plan_week")
        sizes = [r.context for r in logs.records if hasattr(r, "context")]
        self.assertIn(playbooks._RIDE_SESSION, analysis)
        self.assertEqual(
            len(playbook_for("session_analysis", "ride")),
            sizes[0]["playbook_characters"],
        )
        self.assertNotIn(playbooks._RIDE_SESSION, planned)
        # Only a session analysis reads the recent sessions.
        recent.assert_called_once_with()


class FreeTextReviewKindTests(unittest.TestCase):
    def test_typed_reviews_get_their_kind_and_playbook(self) -> None:
        cases = {
            "Analysiere meine letzte Einheit": "session_analysis",
            "Wie lief mein Intervalltraining gestern?": "session_analysis",
            "Wie war mein Tempolauf?": "session_analysis",
            "Analysiere meinen Lauf vom 2.11.": "session_analysis",
            "How was my ride?": "session_analysis",
            "Analysiere die letzte Einheit des Blocks": "session_analysis",
            "Analysiere meinen Lauf von letzter Woche Dienstag": "session_analysis",
            "Zeig mir meinen Fortschritt": "progress_review",
            "Analyze my last run against my plan": "session_analysis",
            "Wie lief meine letzte Einheit im Vergleich zum Plan?": "session_analysis",
            "Wie ist mein Fortschritt?": "progress_review",
            "Werde ich fitter?": "progress_review",
            "Bewerte meinen letzten Block": "block_review",
        }
        for message, kind in cases.items():
            with self.subTest(message=message):
                self.assertEqual(free_text_review_kind(message), kind)
                self.assertEqual(select_coach_context(message, {}).kind, kind)

    def test_other_messages_keep_their_selection(self) -> None:
        for message in (
            "Analysiere den Verlauf meiner HRV",
            "Analysiere meine Läufe der letzten Woche",
            "Wie war meine Woche?",
            "Wie ist die Entwicklung meines Gewichts?",
            "Analysiere meine letzte Einheit und plane darauf basierend morgen",
            "Soll ich einen Progressionslauf machen?",
            "Was ist ein Progressionslauf?",
            "Wie werde ich fitter?",
            "Wie ist die Entwicklung meiner HRV?",
            "Wie war mein Schlaf nach der Einheit?",
            "Wie war meine Erholung nach dem Intervalltraining?",
            "Wie geht es meiner Erholung?",
            "Ich habe 60g Whey getrunken",
        ):
            with self.subTest(message=message):
                self.assertNotIn(
                    select_coach_context(message, {}).kind,
                    {"session_analysis", "progress_review", "block_review"},
                )
        self.assertEqual(
            select_coach_context("Plane die nächste Woche", {}).kind, "plan_week"
        )

    def test_typed_review_keeps_what_the_message_asked_for(self) -> None:
        weather = select_coach_context(
            "How did my interval session go and what is the weather tomorrow?", {}
        )
        self.assertEqual(weather.kind, "session_analysis")
        self.assertIn("weather", weather.sections)
        saturday = select_coach_context(
            "How did my interval session go and what's the weather on Saturday?", {}
        )
        self.assertEqual(saturday.kind, "session_analysis")
        self.assertGreaterEqual(saturday.horizon_days or 0, 7)
        self.assertIn("daily_planning_context", saturday.sections)
        self.assertIsNone(
            free_text_review_kind("Analysiere die Einheit und passe die Woche an")
        )
        dated = select_coach_context("Analysiere meinen Lauf vom 2.11.", {})
        self.assertEqual(dated.kind, "session_analysis")
        self.assertNotIn("planning_brief", dated.sections)


class RequestKindSelectionTests(unittest.TestCase):
    def test_plan_kinds_load_plan_calendar_recovery_and_weather(self) -> None:
        for kind in ("plan_week", "plan_block"):
            sections = select_coach_context("ok", {}, request_kind=kind).sections
            with self.subTest(kind=kind):
                self.assertLessEqual(
                    {
                        "planning",
                        "training_plans",
                        "external_calendar",
                        "daily_planning_context",
                        "recovery",
                        "weather",
                        "durable_profile",
                    },
                    sections,
                )
                self.assertNotIn("calendar", sections)
                self.assertNotIn("nutrition_products", sections)

    def test_review_and_analysis_kinds_load_what_they_need(self) -> None:
        progress = select_coach_context("x", {}, request_kind="progress_review")
        self.assertLessEqual({"planning", "recovery", "activities"}, progress.sections)
        # The one review dates a named plan or the last test from training_plans.
        self.assertIn("training_plans", progress.sections)
        self.assertNotIn("daily_planning_context", progress.sections)
        self.assertEqual(progress.horizon_days, 0)
        analysis = select_coach_context("x", {}, request_kind="session_analysis")
        self.assertEqual(analysis.name, "session_analysis")
        self.assertIn("durable_profile", analysis.sections)
        self.assertLessEqual(
            {"local_planned_workouts", "activities", "recovery", "current_performance"},
            analysis.sections,
        )
        for heavy in (
            "weather",
            "calendar",
            "external_calendar",
            "training_plans",
            "daily_planning_context",
        ):
            self.assertNotIn(heavy, analysis.sections)
        self.assertEqual(analysis.horizon_days, 2)
        # The latest session keeps the today-based planned-unit window; an older
        # session brings its own plan with get_activity_details.
        self.assertEqual(analysis.past_planned_days, 0)

    def test_confirmation_after_food_exchange_keeps_products(self) -> None:
        history = {
            "messages": [
                {"id": 1, "role": "user", "content": "Speichere Mahlzeit Porridge"},
                {"id": 2, "role": "assistant", "content": "Soll ich speichern?"},
            ]
        }
        self.assertIn(
            "nutrition_products", select_coach_context("ja", history).sections
        )
        self.assertNotIn(
            "nutrition_products",
            select_coach_context("ja", {"messages": []}).sections,
        )

    def test_message_food_survives_a_fixed_kind(self) -> None:
        sections = select_coach_context(
            "80g Haferflocken gegessen", {}, request_kind="plan_week"
        ).sections
        self.assertIn("nutrition_products", sections)

    def test_upload_alone_adds_no_products_to_a_fixed_kind(self) -> None:
        for kind in ("session_analysis", "plan_week"):
            sections = select_coach_context(
                "Analysiere meine letzte Einheit.",
                {},
                attachments=True,
                request_kind=kind,
            ).sections
            with self.subTest(kind=kind):
                self.assertNotIn("nutrition_products", sections)

    def test_nutrition_topics_without_intake_wording_select_products(self) -> None:
        for message in (
            "Welche meiner gespeicherten Produkte passen dazu?",
            "Ändere die Vorlage auf zwei Portionen.",
            "Whey-Shake nach dem Training",
        ):
            with self.subTest(message=message):
                self.assertIn(
                    "nutrition_products", select_coach_context(message, {}).sections
                )

    def test_base_prompt_keeps_core_nutrition_write_rules(self) -> None:
        for marker in (
            "log a consumed meal once with save_nutrition_entry",
            "matching saved local products by exact product_id first",
            "log_nutrition_template by exact ID",
            "never save hypothetical or negated consumption",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, COACH_PROMPT)

    def test_every_kind_has_a_selection_and_keeps_message_retention(self) -> None:
        kinds = set(REQUEST_KINDS) | {"morning_checkin"}
        self.assertEqual(set(REQUEST_KIND_SELECTIONS), kinds)
        context = {
            "pending_request": {"status": "needs_clarification", "summary": "x"},
            "messages": [],
        }
        for kind in kinds:
            for message in ("Plane meine nächste Woche.", "Wie war mein Schlaf?"):
                expected = select_coach_context(message, context)
                selection = select_coach_context(message, context, request_kind=kind)
                with self.subTest(kind=kind, message=message):
                    self.assertEqual(
                        selection.retain_pending_request,
                        expected.retain_pending_request,
                    )
                    self.assertEqual(
                        selection.retain_dialogue, expected.retain_dialogue
                    )

    def test_progress_review_keeps_performance_comparisons(self) -> None:
        context = {"current_performance": {"comparisons": [1], "other": 2}}
        selection = select_coach_context("x", {}, request_kind="progress_review")
        projected = selection.project(context, "2026-11-01")
        self.assertEqual(projected["current_performance"]["comparisons"], [1])

    def test_unknown_kinds_use_message_selection(self) -> None:
        expected = select_coach_context("Wie geht es mir?", {}).name
        for kind in (None, "unknown_kind"):
            self.assertEqual(
                select_coach_context("Wie geht es mir?", {}, request_kind=kind).name,
                expected,
            )
        self.assertEqual("", playbook_for("morning_checkin"))


class HrvGuidanceTests(unittest.TestCase):
    """HRV is the Coach's to interpret, never an automatic load rule (A7, #3918)."""

    def test_the_playbook_interprets_the_seven_day_mean_and_prescribes_nothing(
        self,
    ) -> None:
        text = playbook_for("plan_week")
        self.assertNotIn("Adjust load to HRV", text)
        self.assertIn("7-day mean", text)

    def test_the_coach_gets_last_night_the_range_and_the_rolling_mean(self) -> None:
        baseline = {
            "source": "Garmin Connect",
            "metric": "hrv",
            "value": 48.0,
            "observed_at": "2026-11-21",
            "lower": 45.0,
            "upper": 58.0,
            "position": "within",
            "rolling": {"method": "rolling-lnrmssd-v1", "value": 51.0, "lower": 47.0},
            "history": [{"date": "2026-11-21", "value": 48.0}],
        }
        projected = coach_performance_projection(
            {"personal_recovery": {"baselines": [baseline]}}
        )
        (row,) = projected["personal_recovery"]["baselines"]
        self.assertEqual(
            {key: value for key, value in baseline.items() if key != "history"}, row
        )


if __name__ == "__main__":
    unittest.main()

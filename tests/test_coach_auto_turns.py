"""Opt-in automatic Coach turns: when one is queued, its counts and its push (#4274, #4275)."""

from __future__ import annotations

import sqlite3
import unittest
from datetime import UTC, datetime
from typing import cast
from unittest.mock import Mock

from support import FakeManager

from backend.coach.auto_turns import (
    MORNING_MESSAGE,
    AutomaticCoachTurns,
    diagnostics,
    key_statement,
)
from backend.db.manager import DatabaseManager
from backend.db.repositories import KeyValueRepository
from backend.errors import AppError
from backend.settings import SettingsService


class AutomaticCoachTurnTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            "CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
            "updated_at TEXT NOT NULL)"
        )
        self.connection.executescript(
            "CREATE TABLE activity_sessions (activity_id TEXT PRIMARY KEY, "
            "primary_provider TEXT NOT NULL, state TEXT NOT NULL, "
            "started_at TEXT NOT NULL, created_at TEXT NOT NULL);"
            "CREATE TABLE activity_records (provider TEXT NOT NULL, "
            "activity_id TEXT, sport TEXT NOT NULL);"
        )
        self.key_values = KeyValueRepository(lambda: "2026-11-27T06:00:00+00:00")
        self.stored: dict[str, str] = {}
        self.settings = SettingsService(
            Mock(), self.stored.get, self.stored.__setitem__
        )
        self.settings.coach_ready = Mock(return_value=True)  # type: ignore[method-assign]
        self.submission = Mock()
        self.queued: list[str] = []

        def enqueue(message, turn_id, request_kind):
            # As the real service: a turn id is queued once.
            if turn_id in self.queued:
                return False
            self.queued.append(turn_id)
            return True

        self.submission.enqueue_automatic.side_effect = enqueue
        self.state = {"busy": False, "done": False, "sleep": True}
        self.now = datetime(2026, 11, 27, 7, 30, tzinfo=UTC)
        self.turns = AutomaticCoachTurns(
            self.settings,
            lambda: self.submission,
            lambda: self.state["busy"],
            lambda: self.state["done"],
            lambda day: self.state["sleep"],
            lambda: self.now,
            lambda: cast(DatabaseManager, FakeManager(self.connection)),
            self.key_values,
        )

    def _counts(self) -> dict[str, int]:
        return diagnostics(self.key_values, self.connection)

    def test_the_morning_check_in_is_queued_only_when_switched_on_and_due(self):
        self.assertFalse(self.turns.check())  # off by default
        self.assertTrue(
            self.settings.save_automatic_turns({"morning": True})["morning"]
        )
        cases = {
            "before 07:00": ({"hour": 6}, {}),
            "a Coach turn is running": ({}, {"busy": True}),
            "today's check-in is done": ({}, {"done": True}),
            "tonight's sleep is not stored": ({}, {"sleep": False}),
        }
        for label, (moment, state) in cases.items():
            with self.subTest(label):
                self.now = datetime(2026, 11, 27, moment.get("hour", 9), tzinfo=UTC)
                self.state = {"busy": False, "done": False, "sleep": True, **state}
                self.assertFalse(self.turns.check())
        self.submission.enqueue_automatic.assert_not_called()
        self.state = {"busy": False, "done": False, "sleep": True}
        self.assertTrue(self.turns.check())
        self.submission.enqueue_automatic.assert_called_once_with(
            MORNING_MESSAGE, "auto:morning:2026-11-27", "morning_checkin"
        )
        # The day's turn id was queued already: no second attempt, not counted.
        self.assertFalse(self.turns.check())
        self.assertEqual(self._counts(), {"started": 1, "completed": 0, "failed": 0})

    def test_only_known_switches_with_true_or_false_are_saved(self):
        defaults = {
            "morning": False,
            "sessions": False,
            # Running and cycling are preselected (athlete, #4275).
            "sports": {"run": True, "ride": True, "strength": False, "other": False},
        }
        self.assertEqual(self.settings.automatic_turns(), defaults)
        for values in (
            {},
            {"evening": True},
            {"morning": "yes"},
            ["morning"],
            {"sports": {"swim": True}},
            {"sports": {"run": 1}},
            {"sports": ["run"]},
        ):
            with self.subTest(values=values), self.assertRaises(AppError):
                self.settings.save_automatic_turns(values)
        self.assertEqual(self.settings.automatic_turns(), defaults)
        saved = self.settings.save_automatic_turns(
            {"sessions": True, "sports": {"ride": False, "other": True}}
        )
        self.assertEqual(
            saved["sports"],
            {"run": True, "ride": False, "strength": False, "other": True},
        )
        self.assertEqual(self.settings.automatic_session_sports(), {"run", "other"})

    def _session(self, ident, minutes, sport="run", state="active", provider="garmin"):
        created = datetime(2026, 11, 27, 9, tzinfo=UTC).timestamp() + minutes * 60
        self.connection.execute(
            "INSERT INTO activity_sessions VALUES (?, ?, ?, ?, ?)",
            (
                ident,
                provider,
                state,
                "2026-11-27T08:00:00+00:00",
                datetime.fromtimestamp(created, UTC).isoformat(),
            ),
        )
        self.connection.execute(
            "INSERT INTO activity_records VALUES (?, ?, ?)", (provider, ident, sport)
        )

    def _queued(self) -> list[str]:
        return self.queued

    def test_each_new_session_of_a_checked_sport_is_analysed_once_newest_first(self):
        self.now = datetime(2026, 11, 27, 6, tzinfo=UTC)  # before the first session
        self._session("act_before", -200)
        self.assertFalse(self.turns.check())  # off: nothing, no start marker
        self.settings.save_automatic_turns({"sessions": True})
        self.assertFalse(self.turns.check())  # at night only the start is marked
        self._session("act_ride", 10, sport="ride")
        self._session("act_run", 20)
        self._session("act_hike", 30, sport="hike")  # "Sonstiges", unchecked
        self._session("act_dup", 40, state="duplicate")
        self.now = datetime(2026, 11, 27, 10, tzinfo=UTC)
        self.assertTrue(self.turns.check())
        self.state["busy"] = True
        self.assertFalse(self.turns.check())  # one turn at a time
        self.state["busy"] = False
        self.assertTrue(self.turns.check())
        self.assertEqual(
            self._queued(), ["auto:session:act_run", "auto:session:act_ride"]
        )
        self.assertEqual(
            self.submission.enqueue_automatic.call_args.args[2], "session_analysis"
        )
        self.assertIn("act_ride", self.submission.enqueue_automatic.call_args.args[0])
        self.assertFalse(self.turns.check())  # each queued once, the rest unchecked
        self.assertEqual(self._counts()["started"], 2)

    def test_a_burst_is_capped_per_day_and_switching_off_forgets_the_start(self):
        self.settings.save_automatic_turns({"sessions": True})
        self.now = datetime(2026, 11, 27, 6, tzinfo=UTC)
        self.turns.check()  # marks the start
        for number in range(7):
            self._session(f"act_{number}", number)
        self.now = datetime(2026, 11, 27, 10, tzinfo=UTC)
        queued = [self.turns.check() for _ in range(7)]
        self.assertEqual(queued, [True] * 5 + [False] * 2)
        self.assertEqual(self._queued()[0], "auto:session:act_6")  # newest first
        self.now = datetime(2026, 11, 28, 10, tzinfo=UTC)  # a new day, a new cap
        self.assertTrue(self.turns.check())
        self.settings.save_automatic_turns({"sessions": False})
        self.turns.check()
        self.settings.save_automatic_turns({"sessions": True})
        self.turns.check()  # marks a new start: the older sessions stay out
        self.submission.enqueue_automatic.reset_mock()
        self.assertFalse(self.turns.check())
        self.submission.enqueue_automatic.assert_not_called()

    def test_an_answered_turn_replaces_the_night_push_with_its_key_statement(self):
        job = {"client_turn_id": "auto:morning:2026-11-27"}
        answer = (
            "## Morgen-Check-in\n**Gut erholt** (HRV im Normalbereich) – der Dauerlauf "
            "heute passt wie geplant. Danach folgt mehr Text."
        )
        event = self.turns.record(job, "completed", answer)
        self.assertEqual(
            (event.type, event.key, event.title, event.body, event.url),
            (
                "morning_data",
                "morning_data:2026-11-27",
                "Morgen-Check-in",
                "Gut erholt (HRV im Normalbereich) – der Dauerlauf heute passt wie geplant.",
                "#coach",
            ),
        )
        # Without an answer the plain night push still comes, under the same key.
        for outcome, answer in (("failed", ""), ("completed", "")):
            event = self.turns.record(job, outcome, answer)
            self.assertEqual(
                (event.key, event.title, event.body, event.url),
                (
                    "morning_data:2026-11-27",
                    "Morgenwerte",
                    "Die Werte der Nacht sind da.",
                    "#analysis/recovery",
                ),
            )
        self.assertEqual(self._counts(), {"started": 0, "completed": 2, "failed": 1})

    def test_an_analysed_session_replaces_its_new_session_push(self):
        job = {"client_turn_id": "auto:session:act_1"}
        event = self.turns.record(job, "completed", "Intervalle 5×4 min: alle im Ziel.")
        self.assertEqual(
            (event.type, event.key, event.title, event.body, event.url),
            (
                "new_activity",
                "new_activity:act_1",
                "Analyse fertig",
                "Intervalle 5×4 min: alle im Ziel.",
                "#coach",
            ),
        )
        fallback = self.turns.record(job, "failed", "")
        self.assertEqual(
            (fallback.key, fallback.title, fallback.body),
            (
                "new_activity:act_1",
                "Neue Einheit",
                "Eine neue Einheit ist synchronisiert.",
            ),
        )

    def test_the_key_statement_is_plain_text_and_capped(self):
        cases = {
            "a link keeps its text": (
                "Siehe [Plan](https://x.test). Rest.",
                "Siehe Plan.",
            ),
            "a list item": ("\n- `Locker` laufen!\n- mehr", "Locker laufen!"),
            "no sentence end": ("Kurz und knapp", "Kurz und knapp"),
            "abbreviations and numbers": (
                "Schlaf ca. 7 h, z. B. gut. Mehr.",
                "Schlaf ca. 7 h, z. B. gut.",
            ),
            "a numbered first point": ("1. Erholung gut. Weiter", "1. Erholung gut."),
            "a closing bracket": ("Locker (Zone 2). Danach", "Locker (Zone 2)."),
            "identifiers keep underscores": (
                "Wert hr_cap passt.",
                "Wert hr_cap passt.",
            ),
            "empty": ("  \n ", ""),
        }
        for label, (answer, statement) in cases.items():
            with self.subTest(label):
                self.assertEqual(key_statement(answer), statement)
        capped = key_statement("Wort " * 60)
        self.assertEqual(len(capped), 140)
        self.assertTrue(capped.endswith("…"))


if __name__ == "__main__":
    unittest.main()

"""Opt-in automatic Coach turns: when one is queued, its counts and its push (#4274)."""

from __future__ import annotations

import sqlite3
import unittest
from datetime import UTC, datetime
from unittest.mock import Mock

from support import FakeManager

from backend.coach.auto_turns import (
    MORNING_MESSAGE,
    AutomaticCoachTurns,
    diagnostics,
    key_statement,
)
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
        self.key_values = KeyValueRepository(lambda: "2026-11-27T06:00:00+00:00")
        self.stored: dict[str, str] = {}
        self.settings = SettingsService(
            Mock(), self.stored.get, self.stored.__setitem__
        )
        self.settings.coach_ready = Mock(return_value=True)  # type: ignore[method-assign]
        self.submission = Mock()
        self.submission.enqueue_automatic.return_value = True
        self.state = {"busy": False, "done": False, "sleep": True}
        self.now = datetime(2026, 11, 27, 7, 30, tzinfo=UTC)
        self.turns = AutomaticCoachTurns(
            self.settings,
            lambda: self.submission,
            lambda: self.state["busy"],
            lambda: self.state["done"],
            lambda day: self.state["sleep"],
            lambda: self.now,
            lambda: FakeManager(self.connection),
            self.key_values,
        )

    def _counts(self) -> dict[str, int]:
        return diagnostics(self.key_values, self.connection)

    def test_the_morning_check_in_is_queued_only_when_switched_on_and_due(self):
        self.assertFalse(self.turns.check())  # off by default
        self.assertEqual(
            self.settings.save_automatic_turns({"morning": True}), {"morning": True}
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
        self.submission.enqueue_automatic.return_value = False
        self.assertFalse(self.turns.check())
        self.assertEqual(self._counts(), {"started": 1, "completed": 0, "failed": 0})

    def test_only_known_switches_with_true_or_false_are_saved(self):
        for values in ({}, {"evening": True}, {"morning": "yes"}, ["morning"]):
            with self.subTest(values=values), self.assertRaises(AppError):
                self.settings.save_automatic_turns(values)
        self.assertEqual(self.settings.automatic_turns(), {"morning": False})

    def test_an_answered_turn_replaces_the_night_push_with_its_key_statement(self):
        job = {"client_turn_id": "auto:morning:2026-11-27"}
        answer = (
            "## **Gut erholt** (HRV im Normalbereich) – der Dauerlauf heute passt "
            "wie geplant. Danach folgt mehr Text."
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
        self.assertIsNone(self.turns.record(job, "failed", ""))
        self.assertEqual(self._counts(), {"started": 0, "completed": 1, "failed": 1})

    def test_the_key_statement_is_plain_text_and_capped(self):
        cases = {
            "a link keeps its text": (
                "Siehe [Plan](https://x.test). Rest.",
                "Siehe Plan.",
            ),
            "a list item": ("\n- `Locker` laufen!\n- mehr", "Locker laufen!"),
            "no sentence end": ("Kurz und knapp", "Kurz und knapp"),
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

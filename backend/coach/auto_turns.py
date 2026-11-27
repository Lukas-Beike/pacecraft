"""Opt-in automatic Coach turns that only read and propose (#4274).

Once the night's sleep is stored, ``check`` queues the morning check-in of the
day as an unattended background job: the same request kind, preparation and
model as the quick action, but with every write withheld (``enqueue_automatic``)
and no login session. Its turn id names the day, so a day gets one attempt and a
failure is never retried. The daily loop calls ``check`` every few minutes;
``record`` counts the outcomes for diagnostics and builds the push that replaces
"Morgenwerte".
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from datetime import date, datetime
from typing import Any, Final

from backend.coach.authorization import AUTOMATIC_TURN_PREFIX
from backend.coach.job_submission import CoachJobSubmissionService
from backend.db.manager import DatabaseManager
from backend.db.repositories import KeyValueRepository
from backend.push.service import Notification
from backend.settings import SettingsService

MORNING_TURN_PREFIX: Final = f"{AUTOMATIC_TURN_PREFIX}morning:"
MORNING_MESSAGE: Final = (
    "Automatischer Morgen-Check-in: Die Werte der Nacht sind da. Bewerte Erholung, "
    "Belastung und die geplante Einheit und sag mir, was heute sinnvoll ist."
)
COUNTS_KEY: Final = "coach_automatic_turn_counts"
OUTCOMES: Final = ("started", "completed", "failed")
# Local hours in which an automatic turn starts, as the push notifications.
START_HOUR: Final = 7
END_HOUR: Final = 22
# The push body: the answer's first sentence, at most this long.
STATEMENT_CHARS: Final = 140


class AutomaticCoachTurns:
    """Queue the athlete's opt-in automatic turns and count their outcomes."""

    def __init__(
        self,
        settings: SettingsService,
        submission: Callable[[], CoachJobSubmissionService],
        busy: Callable[[], bool],
        morning_done: Callable[[], bool],
        sleep_ready: Callable[[date], bool],
        local_now: Callable[[], datetime],
        database: Callable[[], DatabaseManager],
        key_values: KeyValueRepository,
        operation: Callable[[], AbstractContextManager[Any]] = nullcontext,
    ) -> None:
        self._settings = settings
        self._submission = submission
        self._busy = busy
        self._morning_done = morning_done
        self._sleep_ready = sleep_ready
        self._local_now = local_now
        self._database = database
        self._key_values = key_values
        self._operation = operation

    def check(self) -> bool:
        """Queue today's morning check-in when it is due; True when one was queued.

        Due: switched on, the Coach ready, daytime, no Coach turn queued or
        running, no check-in for today yet and tonight's sleep stored.
        """
        local = self._local_now()
        today = local.date()
        if (
            not self._settings.automatic_turns()["morning"]
            or not START_HOUR <= local.hour < END_HOUR
            or not self._settings.coach_ready()
            or self._busy()
            or self._morning_done()
            or not self._sleep_ready(today)
        ):
            return False
        queued = self._submission().enqueue_automatic(
            MORNING_MESSAGE,
            f"{MORNING_TURN_PREFIX}{today.isoformat()}",
            "morning_checkin",
        )
        if queued:
            self._count("started")
        return queued

    def record(
        self, job: dict[str, Any], outcome: str, answer: str
    ) -> Notification | None:
        """Count a finished automatic turn; the push that announces it.

        It runs after the turn's operation, so the count takes the gate itself.
        """
        with self._operation():
            self._count(outcome)
        turn_id = str(job.get("client_turn_id") or "")
        statement = key_statement(answer) if outcome == "completed" else ""
        if not turn_id.startswith(MORNING_TURN_PREFIX):
            return None
        day = turn_id.removeprefix(MORNING_TURN_PREFIX)
        # The key of "Morgenwerte": the day gets one of the two; without an
        # answer the plain "Morgenwerte" still tells that the night is there.
        if not statement:
            return Notification(
                "morning_data",
                f"morning_data:{day}",
                "Morgenwerte",
                "Die Werte der Nacht sind da.",
                "#analysis/recovery",
            )
        return Notification(
            "morning_data",
            f"morning_data:{day}",
            "Morgen-Check-in",
            statement,
            "#coach",
        )

    def _count(self, outcome: str) -> None:
        with self._database().unit_of_work() as db:
            counts = diagnostics(self._key_values, db)
            counts[outcome] = counts.get(outcome, 0) + 1
            self._key_values.set(db, COUNTS_KEY, json.dumps(counts, sort_keys=True))


def diagnostics(key_values: KeyValueRepository, db: Any) -> dict[str, int]:
    """Counts only: automatic turns started, completed and failed."""
    try:
        stored = json.loads(key_values.get(db, COUNTS_KEY) or "{}")
    except ValueError:
        stored = {}
    stored = stored if isinstance(stored, dict) else {}
    return {
        name: stored[name] if type(stored.get(name)) is int else 0 for name in OUTCOMES
    }


_MARKUP = re.compile(r"\[([^\]]*)\]\([^)]*\)|[*`]")
# A sentence ends after a word of three letters or a closing mark, before a
# capital or the end: "z. B.", "ca. 7 h" and "1." do not end it.
_SENTENCE = re.compile(r"^(.*?(?:[^\W\d_]{3}|[)\"”])[.!?])(?=\s+[A-ZÄÖÜ\"„(]|$)")


def key_statement(answer: str) -> str:
    """The answer's first sentence as plain text, capped (#4274)."""
    for line in str(answer or "").splitlines():
        if line.lstrip().startswith("#"):
            continue  # a heading is no statement
        text = " ".join(_MARKUP.sub(r"\1", line).split()).lstrip("->• ")
        if text:
            match = _SENTENCE.match(text)
            sentence = match[1] if match else text
            if len(sentence) <= STATEMENT_CHARS:
                return sentence
            return sentence[: STATEMENT_CHARS - 1].rstrip() + "…"
    return ""

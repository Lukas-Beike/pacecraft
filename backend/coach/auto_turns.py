"""Opt-in automatic Coach turns that only read and propose (#4274, #4275).

Once the night's sleep is stored, ``check`` queues the morning check-in of the
day, and for each new Garmin or Wahoo session of an analysed sport its session
analysis, as an unattended background job: the same request kind, preparation
and model as the quick action, but with every write withheld
(``enqueue_automatic``) and no login session. Each turn id names its day or
session, so it gets one attempt and a failure is never retried. The daily loop
calls ``check`` every few minutes; ``record`` counts the outcomes for
diagnostics and builds the push that replaces "Morgenwerte" or "Neue Einheit".
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from datetime import UTC, date, datetime
from typing import Any, Final

from backend.coach.authorization import AUTOMATIC_TURN_PREFIX
from backend.coach.job_submission import CoachJobSubmissionService
from backend.db import coach_store
from backend.db.manager import DatabaseManager
from backend.db.repositories import KeyValueRepository
from backend.push.events import new_sessions, session_groups
from backend.push.service import Notification
from backend.settings import SettingsService

MORNING_TURN_PREFIX: Final = coach_store.AUTOMATIC_MORNING_PREFIX
MORNING_MESSAGE: Final = (
    "Automatischer Morgen-Check-in: Die Werte der Nacht sind da. Bewerte Erholung, "
    "Belastung und die geplante Einheit und sag mir, was heute sinnvoll ist."
)
SESSION_TURN_PREFIX: Final = f"{AUTOMATIC_TURN_PREFIX}session:"
SESSION_MESSAGE: Final = (
    "Automatische Einheitenanalyse: Analysiere meine neue Einheit {session_id}: Was "
    "lief gut, was sollte ich anpassen und wie beeinflusst sie die nächste Einheit?"
)
# Since when new sessions count, and how many were queued on which day (#4275).
SESSIONS_KEY: Final = "coach_automatic_sessions"
SESSIONS_PER_DAY: Final = 5
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
        """Queue at most one due automatic turn; True when one was queued.

        Only by day, with the Coach ready and no Coach turn queued or running,
        so several new sessions run one after another, newest first.
        """
        local = self._local_now()
        # The session analysis marks its start, or forgets it, also at night.
        sports = self._session_sports(local)
        if (
            not START_HOUR <= local.hour < END_HOUR
            or not self._settings.coach_ready()
            or self._busy()
        ):
            return False
        return self._check_morning(local.date()) or self._check_sessions(local, sports)

    def _check_morning(self, today: date) -> bool:
        """Today's check-in: switched on, none for today yet and tonight's sleep stored."""
        if (
            not self._settings.automatic_turns()["morning"]
            or self._morning_done()
            or not self._sleep_ready(today)
        ):
            return False
        return self._enqueue(
            MORNING_MESSAGE,
            f"{MORNING_TURN_PREFIX}{today.isoformat()}",
            "morning_checkin",
        )

    def _session_sports(self, local: datetime) -> frozenset[str]:
        """The analysed sport groups. The first check after switching on marks
        since when sessions count; switching off removes the mark."""
        sports = self._settings.automatic_session_sports()
        with self._database().unit_of_work() as db:
            if not sports and self._key_values.get(db, SESSIONS_KEY) is not None:
                self._key_values.delete(db, SESSIONS_KEY)
            elif sports and not _sessions_state(self._key_values, db):
                since = local.astimezone(UTC).isoformat()
                self._key_values.set(db, SESSIONS_KEY, json.dumps({"since": since}))
        return sports

    def _check_sessions(self, local: datetime, sports: frozenset[str]) -> bool:
        """The newest new session of an analysed sport not analysed yet (#4275).

        Only sessions stored after the switch was turned on count, so a replay,
        a backfill or an older session never queues a burst; each session has
        one turn id, and at most ``SESSIONS_PER_DAY`` start a day.
        """
        if not sports:
            return False
        today = local.date().isoformat()
        with self._database().unit_of_work() as db:
            state = _sessions_state(self._key_values, db)
            if not state:
                return False
            queued = state.get("queued", 0) if state.get("day") == today else 0
            if queued >= SESSIONS_PER_DAY:
                return False
            since = datetime.fromisoformat(state["since"])
            found = new_sessions(db, since, local.date())
            groups = session_groups(db, found)
        for session_id in (item for item in found if groups.get(item) in sports):
            if self._enqueue(
                SESSION_MESSAGE.format(session_id=session_id),
                f"{SESSION_TURN_PREFIX}{session_id}",
                "session_analysis",
            ):
                with self._database().unit_of_work() as db:
                    state = {**state, "day": today, "queued": queued + 1}
                    self._key_values.set(db, SESSIONS_KEY, json.dumps(state))
                return True
        return False

    def _enqueue(self, message: str, turn_id: str, request_kind: str) -> bool:
        queued = self._submission().enqueue_automatic(message, turn_id, request_kind)
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
        if turn_id.startswith(SESSION_TURN_PREFIX):
            # The key of "Neue Einheit", which its analysis replaces (#4275).
            key = f"new_activity:{turn_id.removeprefix(SESSION_TURN_PREFIX)}"
            if not statement:
                return Notification(
                    "new_activity",
                    key,
                    "Neue Einheit",
                    "Eine neue Einheit ist synchronisiert.",
                    "#plan/overview",
                )
            return Notification(
                "new_activity", key, "Analyse fertig", statement, "#coach"
            )
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


def _sessions_state(key_values: KeyValueRepository, db: Any) -> dict[str, Any]:
    """The stored start of the session analysis; empty when unset or unreadable."""
    try:
        state = json.loads(key_values.get(db, SESSIONS_KEY) or "{}")
        datetime.fromisoformat(state["since"])
    except KeyError, TypeError, ValueError:
        return {}
    return state if type(state.get("queued", 0)) is int else {}


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


def _plain(line: str) -> str:
    """The line without emphasis, code marks and link targets: a link keeps its text."""
    kept: list[str] = []
    index = 0
    while index < len(line):
        char = line[index]
        end = line.find(")", index + 2) if line.startswith("](", index) else -1
        if end != -1:
            index = end + 1
            continue
        if char not in "*`[]":
            kept.append(char)
        index += 1
    return "".join(kept)


# A sentence ends after a word of three letters or a closing mark, before a
# capital or the end: "z. B.", "ca. 7 h" and "1." do not end it.
_SENTENCE = re.compile(r"^(.*?(?:[^\W\d_]{3}|[)\"”])[.!?])(?=\s+[A-ZÄÖÜ\"„(]|$)")


def key_statement(answer: str) -> str:
    """The answer's first sentence as plain text, capped (#4274)."""
    for line in str(answer or "").splitlines():
        if line.lstrip().startswith("#"):
            continue  # a heading is no statement
        text = " ".join(_plain(line).split()).lstrip("->• ")
        if text:
            match = _SENTENCE.match(text)
            sentence = match[1] if match else text
            if len(sentence) <= STATEMENT_CHARS:
                return sentence
            return sentence[: STATEMENT_CHARS - 1].rstrip() + "…"
    return ""

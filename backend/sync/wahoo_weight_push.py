"""Mirror the newest entered weight to the Wahoo profile weight (#3559).

The one Wahoo write the athlete approved for body data (athlete decision
2026-11-18): saving, changing or removing an entered weight in the app sets the
Wahoo profile weight to the newest entered weight that remains, so Wahoo's
power and calorie estimates use it. Removing every entry writes nothing, and
nothing else is written to Wahoo. The model is the Garmin body push (#3403),
reduced to one value: Wahoo keeps one profile weight, not one entry per day.

The push runs on its own thread after the local commit, so a save never waits
for Wahoo; the browser follows it through ``push_state`` in the sync status (#4267). It needs the ``user_write`` scope: without it nothing is written and
the change waits (``pending``) until the athlete reconnects Wahoo with that
scope. A failed or unanswered write also stays pending; the next Wahoo refresh
retries it once with the then-newest weight. The write overwrites one value, so
a resent write is harmless. A disconnected Wahoo is not remembered: the next
save writes, and a new connection is written anew. Wahoo's text never reaches
the logs or the diagnostics; the state in ``STATE_KEY`` holds the last sent
weight with its connection time, the pending flag and counts only.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Mapping
from typing import Any, Final

from backend.canonical.vocabulary import MetricKind, SourceKind
from backend.db.repositories import KeyValueRepository
from backend.providers.wahoo_client import USER_WRITE_SCOPE

STATE_KEY: Final = "wahoo_weight_push"
# Outcomes: written, nothing to write, waiting for a retry or for the scope,
# Wahoo not connected.
SENT, UNCHANGED, PENDING, OFF = "sent", "unchanged", "pending", "off"
# A started push the browser follows (#4267).
RUNNING: Final = "running"
# Why the last push ended as it did (#3677), an enum for the diagnostics: the
# grant lacks user_write, Wahoo refused (4xx, final) or failed (5xx), the
# request failed otherwise, or a local fault stopped the push.
SCOPE_MISSING, HTTP_4XX, HTTP_5XX = "scope_missing", "http_4xx", "http_5xx"
ERROR, STOPPED = "error", "stopped"
# A timeout or rate limit (408, 429) is transient: it waits like a 5xx.
THROTTLED: Final = "throttled"
_TRANSIENT_STATUSES: Final = frozenset({408, 429})
_LOGGER = logging.getLogger("intervals_coach")
# One push at a time; each reads the newest weight inside the lock, so the last
# push always sends the latest value.
_PUSH_LOCK = threading.Lock()
# Started pushes not yet finished; a restart forgets them with their threads.
_RUNNING = [0]
_RUNNING_LOCK = threading.Lock()
_NEWEST_SQL: Final = (
    "SELECT value FROM observations WHERE metric = ? AND provider IS NULL "
    "AND source_kind = ? AND state = 'active' "
    "ORDER BY observed_on DESC, id DESC LIMIT 1"
)


class WahooWeightPushService:
    """Set the Wahoo profile weight to the newest entered weight."""

    def __init__(
        self,
        database_manager: Any,
        client: Callable[[], Any],
        status: Callable[[], Mapping[str, Any]],
        now: Callable[[], str],
    ) -> None:
        self._database_manager = database_manager
        self._client = client
        # The token-free connection status: ``connected`` and the granted ``scope``.
        self._status = status
        self._key_values = KeyValueRepository(now)

    def start(self) -> str:
        """Push after a weight change without holding up the save; never raises.

        Returns ``running`` for a started push, ``off`` without a connection.
        """
        try:
            if not self._status().get("connected"):
                return OFF
            _count_running(1)

            def run() -> None:
                try:
                    self.push()
                finally:
                    _count_running(-1)

            try:
                threading.Thread(
                    target=run, name="wahoo-weight-push", daemon=True
                ).start()
            except Exception:
                _count_running(-1)
                raise
            return RUNNING
        except Exception as error:  # noqa: BLE001 - the save has been committed
            _LOGGER.warning(
                "Wahoo weight push not started",
                extra={"event": "wahoo_weight_push_error", "context": {"error_type": type(error).__name__}},
            )  # fmt: skip
            return PENDING

    def mark_restored(self) -> None:
        """An undo put an entered weight back (#3640): leave it pending, so the
        next Wahoo refresh sends the newest weight; the undo writes nothing."""
        with self._database_manager.unit_of_work() as db:
            state = self._state(db)
        self._store(state, pending=True)

    def retry_pending(self) -> str | None:
        """Retry a pending push after a Wahoo refresh; None when none waits."""
        try:
            with self._database_manager.reader() as db:
                waiting = bool(self._state(db).get("pending"))
        except Exception:  # noqa: BLE001 - must not fail the refresh job
            return None
        return self.push(retry=True) if waiting else None

    def push(self, *, retry: bool = False) -> str:
        """Bring the Wahoo profile weight in line; never raises."""
        try:
            with _PUSH_LOCK:
                return self._push(retry)
        except Exception as error:  # noqa: BLE001 - a local fault; text never kept
            _LOGGER.warning(
                "Wahoo weight push stopped",
                extra={"event": "wahoo_weight_push_error", "context": {"error_type": type(error).__name__}},
            )  # fmt: skip
            self._note(STOPPED)
            return PENDING

    def _push(self, retry: bool) -> str:
        with self._database_manager.reader() as db:
            state = self._state(db)
            row = db.execute(
                _NEWEST_SQL,
                (MetricKind.WEIGHT.value, SourceKind.ATHLETE_ENTERED.value),
            ).fetchone()
        newest = None if row is None else round(_value(row), 1)
        if newest is None:
            self._store(state, pending=False)
            return UNCHANGED
        status = self._status()
        if not status.get("connected"):
            self._store(state, pending=False, last_outcome=OFF)
            return OFF
        # What was sent is bound to the connection, so a new one is written anew.
        sent = f"{newest:.1f}@{status.get('connected_at') or ''}"
        if sent == state.get("sent_value") and not retry:
            self._store(state, pending=False)
            return UNCHANGED
        if USER_WRITE_SCOPE not in str(status.get("scope") or "").split():
            # A grant from before #3559 lacks the scope; the card says so (#3677).
            self._store(state, pending=True, last_outcome=SCOPE_MISSING)
            return PENDING
        # Pending before the request: a lost answer or a stop is retried, and an
        # older value is never taken as sent.
        state = self._store(state, pending=True, sent_value=None)
        counts = ("retried",) if retry else ()
        try:
            self._client().update_user_weight(newest)
        except Exception as error:  # noqa: BLE001 - provider boundary, text never kept
            _LOGGER.warning(
                "Wahoo weight push failed",
                extra={"event": "wahoo_weight_push_failed", "context": {"error_type": type(error).__name__}},
            )  # fmt: skip
            outcome = _failure(error)
            # Wahoo refusing the write is final; retrying it would never end.
            self._store(
                state,
                pending=outcome != HTTP_4XX,
                counts=(*counts, "failed"),
                last_outcome=outcome,
            )
            return PENDING
        self._store(
            state,
            pending=False,
            counts=(*counts, "sent"),
            sent_value=sent,
            last_outcome=SENT,
        )
        return SENT

    def _note(self, outcome: str) -> None:
        """Record a local stop's outcome; a fault here must not mask the first."""
        try:
            with self._database_manager.reader() as db:
                state = self._state(db)
            self._store(state, pending=bool(state.get("pending")), last_outcome=outcome)
        except Exception:  # noqa: BLE001 - the push already failed and logged
            return

    def _state(self, db: Any) -> dict[str, Any]:
        return read_state(self._key_values.get(db, STATE_KEY))

    def _store(
        self,
        state: dict[str, Any],
        *,
        pending: bool,
        counts: tuple[str, ...] = (),
        **changes: Any,
    ) -> dict[str, Any]:
        """Write ``state`` (as stored) with the changes; nothing when unchanged."""
        changed = dict(state, pending=pending, **changes)
        for name in counts:
            changed[name] = int(changed.get(name) or 0) + 1
        if changed != state:
            with self._database_manager.unit_of_work() as db:
                self._key_values.set(db, STATE_KEY, json.dumps(changed, sort_keys=True))
        return changed


def _count_running(step: int) -> None:
    with _RUNNING_LOCK:
        _RUNNING[0] += step


def push_state(db: Any) -> dict[str, bool]:
    """Whether a started push runs now and whether a change still waits (#4267)."""
    with _RUNNING_LOCK:
        running = _RUNNING[0] > 0
    state = read_state(KeyValueRepository(lambda: "").get(db, STATE_KEY))
    return {"running": running, "pending": bool(state.get("pending"))}


def read_state(raw: str | None) -> dict[str, Any]:
    try:
        state = json.loads(raw or "{}")
    except TypeError, ValueError:
        state = {}
    return state if isinstance(state, dict) else {}


def _value(row: Any) -> float:
    return float(row["value"] if isinstance(row, Mapping) else row[0])


def _failure(error: BaseException) -> str:
    """The class of a failed write: Wahoo's HTTP status class, never its text."""
    status = getattr(error, "upstream_status", None)
    if status in _TRANSIENT_STATUSES:
        return THROTTLED
    if isinstance(status, int) and 400 <= status < 500:
        return HTTP_4XX
    if isinstance(status, int) and status >= 500:
        return HTTP_5XX
    return ERROR


_OUTCOMES: Final = frozenset(
    {SENT, OFF, SCOPE_MISSING, HTTP_4XX, HTTP_5XX, THROTTLED, ERROR, STOPPED}
)


def summarize_weight_push(db: Any) -> dict[str, Any]:
    """Counts and one enum (#3559, #3677): writes, failures and retries so far,
    whether a change still waits for Wahoo, and why the last push ended as it
    did (``last_outcome``, None before the first)."""
    state = read_state(KeyValueRepository(lambda: "").get(db, STATE_KEY))
    outcome = state.get("last_outcome")
    return {
        **{name: int(state.get(name) or 0) for name in ("sent", "failed", "retried")},
        "pending": int(bool(state.get("pending"))),
        "last_outcome": outcome if outcome in _OUTCOMES else None,
    }

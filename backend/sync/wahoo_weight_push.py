"""Mirror the newest entered weight to the Wahoo profile weight (#3559).

The one Wahoo write the athlete approved for body data (athlete decision
2026-11-18): saving, changing or removing an entered weight in the app sets the
Wahoo profile weight to the newest entered weight that remains, so Wahoo's
power and calorie estimates use it. Removing every entry writes nothing, and
nothing else is written to Wahoo. The model is the Garmin body push (#3403),
reduced to one value: Wahoo keeps one profile weight, not one entry per day.

The push runs on its own thread after the local commit, so a save never waits
for Wahoo. It needs the ``user_write`` scope: without it nothing is written and
the change waits (``pending``) until the athlete reconnects Wahoo with that
scope. A failed or unanswered write also stays pending; the next Wahoo refresh
retries it once with the then-newest weight. The write overwrites one value, so
a resent write is harmless. A disconnected Wahoo is not remembered: the next
save writes. Wahoo's text never reaches the logs or the diagnostics; the state
in ``STATE_KEY`` holds the last sent weight, the pending flag and counts only.
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
_LOGGER = logging.getLogger("intervals_coach")
# One push at a time; each reads the newest weight inside the lock, so the last
# push always sends the latest value.
_PUSH_LOCK = threading.Lock()
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

    def start(self) -> None:
        """Push after a weight change without holding up the save."""
        threading.Thread(
            target=self.push, name="wahoo-weight-push", daemon=True
        ).start()

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
            return PENDING

    def _push(self, retry: bool) -> str:
        with self._database_manager.reader() as db:
            state = self._state(db)
            row = db.execute(
                _NEWEST_SQL,
                (MetricKind.WEIGHT.value, SourceKind.ATHLETE_ENTERED.value),
            ).fetchone()
        newest = None if row is None else round(_value(row), 1)
        if newest is None or (newest == state.get("sent_kg") and not retry):
            self._store(state, pending=False)
            return UNCHANGED
        status = self._status()
        if not status.get("connected"):
            self._store(state, pending=False)
            return OFF
        if USER_WRITE_SCOPE not in str(status.get("scope") or "").split():
            self._store(state, pending=True)
            return PENDING
        counts = ("retried",) if retry else ()
        try:
            self._client().update_user_weight(newest)
        except Exception as error:  # noqa: BLE001 - provider boundary, text never kept
            _LOGGER.warning(
                "Wahoo weight push failed",
                extra={"event": "wahoo_weight_push_failed", "context": {"error_type": type(error).__name__}},
            )  # fmt: skip
            self._store(state, pending=True, counts=(*counts, "failed"))
            return PENDING
        state["sent_kg"] = newest
        self._store(state, pending=False, counts=(*counts, "sent"))
        return SENT

    def _state(self, db: Any) -> dict[str, Any]:
        return read_state(self._key_values.get(db, STATE_KEY))

    def _store(
        self, state: dict[str, Any], *, pending: bool, counts: tuple[str, ...] = ()
    ) -> None:
        changed = dict(state, pending=pending)
        for name in counts:
            changed[name] = int(changed.get(name) or 0) + 1
        if changed == state:
            return
        with self._database_manager.unit_of_work() as db:
            self._key_values.set(db, STATE_KEY, json.dumps(changed, sort_keys=True))


def read_state(raw: str | None) -> dict[str, Any]:
    try:
        state = json.loads(raw or "{}")
    except TypeError, ValueError:
        state = {}
    return state if isinstance(state, dict) else {}


def _value(row: Any) -> float:
    return float(row["value"] if isinstance(row, Mapping) else row[0])


def summarize_weight_push(db: Any) -> dict[str, int]:
    """Counts only (#3559): writes, failures and retries so far, and whether a
    change still waits for Wahoo."""
    state = read_state(KeyValueRepository(lambda: "").get(db, STATE_KEY))
    return {
        **{name: int(state.get(name) or 0) for name in ("sent", "failed", "retried")},
        "pending": int(bool(state.get("pending"))),
    }

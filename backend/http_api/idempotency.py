"""Exactly-once answers for the athlete's own entries sent from an offline outbox.

A replayable route (``Route(replayable=True)``) may carry an ``Idempotency-Key``
header with a UUID. The first 2xx JSON answer is kept under ``idempotency:<uuid>``
with its path for 30 days; the same key on the same path answers it again and
writes nothing, the same key on another path is refused (#3761).
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from backend.errors import AppError

KEY_PREFIX = "idempotency:"
RETENTION = timedelta(days=30)
KEY_REUSED = "Der Idempotenzschlüssel gehört zu einer anderen Anfrage."


def valid_key(header: str | None) -> str | None:
    """The normalized UUID of a valid header value, None for anything else."""
    if not header or len(header) != 36:
        return None
    try:
        return str(uuid.UUID(header))
    except ValueError:
        return None


class IdempotencyStore:
    """Keep and answer the results of keyed requests in the local key/value table."""

    def __init__(self, database: Callable[[], Any], key_values: Any) -> None:
        self._database = database
        self._key_values = key_values
        # One keyed request at a time: a retry cannot run beside the request
        # whose answer it asks for.
        self.lock = threading.Lock()

    def lookup(self, key: str, path: str) -> tuple[int, Any] | None:
        """The stored status and payload of ``key``; 409 when it belongs to another path."""
        with self._database().reader() as db:
            raw = self._key_values.get(db, KEY_PREFIX + key)
        if raw is None:
            return None
        try:
            stored = json.loads(raw)
            status, payload, stored_path = (
                stored["status"],
                stored["payload"],
                stored["path"],
            )
            expired = datetime.fromisoformat(stored["stored_at"]) < (
                datetime.now(UTC) - RETENTION
            )
        except ValueError, KeyError, TypeError:
            return None
        if expired:
            return None
        if stored_path != path:
            raise AppError(409, KEY_REUSED, reason="idempotency_key_reused")
        return status, payload

    def store(self, key: str, path: str, status: int, payload: Any) -> None:
        """Keep a 2xx result and drop the entries older than the retention."""
        value = json.dumps(
            {
                "path": path,
                "status": status,
                "payload": payload,
                "stored_at": datetime.now(UTC).isoformat(),
            },
            ensure_ascii=False,
        )
        cutoff = (datetime.now(UTC) - RETENTION).isoformat()
        with self._database().unit_of_work() as db:
            self._key_values.delete_prefix_before(db, KEY_PREFIX, cutoff)
            self._key_values.set(db, KEY_PREFIX + key, value)

"""The one-time store of Garmin's LT2 values in effect (athlete, 2026-11-24, #4135).

``GarminThresholdStore`` is a follow-up stage of the canonical backfill worker,
so the stored Garmin records are readable when it runs. Its one pass stores
today's Garmin LT2 values as local values (``store_garmin_in_effect`` of the
LT2 owner). The pass runs in the worker's transaction, so a failure stores
nothing; the state key then says ``failed`` and the next pass tries again.
Once ``complete`` it never runs again, whatever it found: a Garmin value that
appears later is an offer, never stored by itself. The state holds the stored
``sport:value`` names only. ``server.py`` registers it as the last follow-up
of the backfill, together with the offer cards, so Garmin's values stop
applying by themselves once the athlete can see and adopt the offers.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import date
from typing import Any, Final

from backend.performance.second_threshold_service import store_garmin_in_effect
from backend.sync.canonical_backfill import BatchReport

STAGE: Final = "garmin_threshold_store"
STATE_KEY: Final = "garmin_threshold_store_state"


class GarminThresholdStore:
    """Store Garmin's LT2 values in effect as local values, once ever."""

    stage = STAGE

    def __init__(
        self,
        *,
        key_values: Any,
        zones: Callable[[], Any],
        reader: Callable[[], Any],
        today: Callable[[], date],
        clock: Callable[[], str],
    ) -> None:
        self._key_values = key_values
        self._zones = zones
        self._reader = reader
        self._today = today
        self._clock = clock

    def run_batch(self, db: Any) -> BatchReport:
        """Store today's Garmin values once, in the caller's transaction."""
        if self.state(db).get("status") == "complete":
            return BatchReport(items=0, done=True)
        stored = store_garmin_in_effect(
            db, self._zones(), self._reader(), self._today()
        )
        self._save(db, {"status": "complete", "stored": stored})
        return BatchReport(items=len(stored), done=True)

    def mark_failed(self, db: Any) -> None:
        self._save(db, {"status": "failed", "stored": []})

    def state(self, db: Any) -> dict[str, Any]:
        """The stored state: ``status`` and the ``stored`` sport:value names."""
        text = self._key_values.get(db, STATE_KEY)
        try:
            state = json.loads(text) if text else {}
        except ValueError:
            return {}
        return state if isinstance(state, dict) else {}

    def _save(self, db: Any, state: Mapping[str, Any]) -> None:
        self._key_values.set(
            db, STATE_KEY, json.dumps({**state, "at": self._clock()}, sort_keys=True)
        )

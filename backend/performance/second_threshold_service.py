"""The local LT2 estimates of one athlete, read for the profile and the Coach (#3273).

Garmin's LT2 stays where it is (the current metrics); this service adds the two
local estimates beside it: DFA-a1 at 0.5 for runs (heart rate, and the pace at
that heart rate from the athlete's own steady runs, card M9) and critical speed
or power over 42 days (card M4). Read-only: nothing here writes or adopts.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date, timedelta
from typing import Any

from backend.canonical.first_threshold import (
    FIRST_THRESHOLD_FIELD,
    first_threshold_percent,
)
from backend.canonical.vocabulary import Sport
from backend.db import activity_detail_store
from backend.performance.critical_power import critical_power_speed
from backend.performance.pace_at_hr import LT1_DAYS, output_at_heart_rate
from backend.performance.second_threshold_estimate import (
    CRITICAL_WINDOW_DAYS,
    ESTIMATE_DAYS,
    critical_second_threshold,
    dfa_second_threshold,
)

RUNS_ONLY = {"status": "unknown", "reason": "nur aus Läufen", "sessions": 0}


class SecondThresholdService:
    """Read the local LT2 estimates per sport."""

    def __init__(
        self,
        manager: Any,
        profile_service: Any,
        lthr_resolver: Callable[[], Any],
        curve_records: Callable[[int], Sequence[Mapping[str, Any]]],
        today: Callable[[], date],
    ) -> None:
        self._manager = manager
        self._profile_service = profile_service
        self._lthr_resolver = lthr_resolver
        self._curve_records = curve_records
        self._today = today

    def estimates(self) -> dict[str, Any]:
        """``{"run": {"dfa_alpha1", "critical_speed"}, "ride": {"dfa_alpha1", "critical_power"}}``."""
        today = self._today()
        with self._manager.reader() as db:
            rows = activity_detail_store.dfa_series_results(
                db,
                since=today - timedelta(days=ESTIMATE_DAYS - 1),
                until=today,
                sport=Sport.RUN,
            )
        dfa = dfa_second_threshold(
            [(row.day, row.result) for row in rows],
            today,
            first_threshold_bpm=self._first_threshold_bpm(),
        )
        records = [
            dict(row)
            for row in self._curve_records(max(LT1_DAYS, CRITICAL_WINDOW_DAYS))
        ]
        if dfa["status"] == "ok":
            pace = output_at_heart_rate(records, "run", dfa["heart_rate_bpm"], today)
            dfa["pace_seconds_per_km"] = None if pace is None else round(pace)
        first = (today - timedelta(days=CRITICAL_WINDOW_DAYS - 1)).isoformat()
        window = [row for row in records if first <= str(row.get("date") or "")]
        fits = critical_power_speed(
            window, today, input_fingerprint="", window_days=CRITICAL_WINDOW_DAYS
        )["fits"]
        return {
            "run": {
                "dfa_alpha1": dfa,
                "critical_speed": critical_second_threshold(fits, "run"),
            },
            "ride": {
                "dfa_alpha1": dict(RUNS_ONLY),
                "critical_power": critical_second_threshold(fits, "ride"),
            },
        }

    def _first_threshold_bpm(self) -> float | None:
        """The stored LT1 heart rate of runs today, or None."""
        percent = first_threshold_percent(
            self._profile_service.get().get(FIRST_THRESHOLD_FIELD)
        )
        lthr = self._lthr_resolver()(None)
        if percent is None or lthr is None or lthr <= 0:
            return None
        return float(percent * lthr / 100)

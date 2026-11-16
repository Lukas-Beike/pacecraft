"""Weights the athlete enters by hand, one per day (#3252).

An entry is an athlete-entered canonical observation (``weight``, kg, no
provider, label "Lokal"), stored like the skinfolds, so no schema change. The
canonical reader returns these rows for the allowlisted entered metrics
(``canonical_store.ENTERED_METRICS``); a provider refresh retires only its own
provider's rows, so an entry stays until the athlete removes it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date
from typing import Any, Final

from backend.athlete.skinfolds import local_provenance
from backend.canonical.records import MetricValue, Observation
from backend.canonical.validation import METRIC_BOUNDS
from backend.canonical.vocabulary import MetricKind, SourceKind
from backend.db import canonical_store
from backend.errors import AppError

MAPPER_VERSION: Final = 1
# Every active entry, so the dialog can fill and remove any entered day.
_ROWS_SQL: Final = (
    "SELECT value, observed_on FROM observations WHERE metric = ? "
    "AND provider IS NULL AND source_kind = ? AND state = 'active' "
    "ORDER BY observed_on DESC"
)


class WeightEntryService:
    """Save, list and remove the athlete's own weigh-ins, one per day."""

    def __init__(
        self,
        database_manager: Any,
        now: Callable[[], str],
        today: Callable[[], date],
    ) -> None:
        self._database_manager = database_manager
        self._now = now
        self._today = today

    def read(self) -> dict[str, Any]:
        """Every active entry, newest first, each with its date and kg."""
        with self._database_manager.reader() as db:
            cursor = db.execute(
                _ROWS_SQL, (MetricKind.WEIGHT.value, SourceKind.ATHLETE_ENTERED.value)
            )
            names = [column[0] for column in cursor.description]
            rows = [
                row if isinstance(row, Mapping) else dict(zip(names, row, strict=True))
                for row in cursor.fetchall()
            ]
        return {
            "entries": [
                {"date": str(row["observed_on"]), "kg": float(row["value"])}
                for row in rows
            ]
        }

    def save(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Save one weigh-in; an entry of the same day is replaced."""
        day = self._day(payload.get("date"))
        kg = self._kg(payload.get("kg"))
        with self._database_manager.unit_of_work() as db:
            self._retire(db, day)
            canonical_store.upsert_observation(
                db,
                Observation(
                    kind=MetricKind.WEIGHT,
                    value=MetricValue(kg, "kg"),
                    observed_on=day,
                    observed_at=None,
                    provenance=local_provenance(),
                ),
                mapper_version=MAPPER_VERSION,
                now=self._now(),
            )
        return self.read()

    def remove(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Retire the entry of one day; the request must be confirmed."""
        if payload.get("confirmed") is not True:
            raise AppError(400, "Das Löschen eines Gewichts muss bestätigt werden.")
        day = self._day(payload.get("date"))
        with self._database_manager.unit_of_work() as db:
            if not self._retire(db, day):
                raise AppError(
                    404, "Für diesen Tag gibt es kein eingetragenes Gewicht."
                )
        return self.read()

    def _retire(self, db: Any, day: date) -> int:
        cursor = db.execute(
            "UPDATE observations SET state = 'retired', updated_at = ? "
            "WHERE metric = ? AND provider IS NULL AND source_kind = ? "
            "AND observed_on = ? AND state = 'active'",
            (
                self._now(),
                MetricKind.WEIGHT.value,
                SourceKind.ATHLETE_ENTERED.value,
                day.isoformat(),
            ),
        )
        return int(cursor.rowcount or 0)

    def _day(self, value: Any) -> date:
        try:
            day = date.fromisoformat(str(value or ""))
        except ValueError as exc:
            raise AppError(400, "Das Datum fehlt oder ist ungültig.") from exc
        if day > self._today():
            raise AppError(400, "Das Datum darf nicht in der Zukunft liegen.")
        return day

    @staticmethod
    def _kg(value: Any) -> float:
        try:
            kg = float(str(value).replace(",", "."))
        except ValueError as exc:
            raise AppError(400, "Das Gewicht braucht einen Wert in kg.") from exc
        lower, upper = METRIC_BOUNDS[MetricKind.WEIGHT]
        if not lower <= kg <= upper:
            raise AppError(
                400, f"Das Gewicht muss zwischen {lower:g} und {upper:g} kg liegen."
            )
        return round(kg, 1)

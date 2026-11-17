"""Weights and body fats the athlete enters by hand, one per day (#3252, #3402).

An entry is an athlete-entered canonical observation (``weight`` in kg or
``body_fat_pct`` in %, no provider, label "Lokal"), stored like the skinfolds,
so no schema change. The canonical reader returns these rows for the
allowlisted entered metrics (``canonical_store.ENTERED_METRICS``); a provider
refresh retires only its own provider's rows, so an entry stays until the
athlete removes it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class BodyEntryKind:
    """One enterable body value: its metric, payload key, unit and messages."""

    kind: MetricKind
    key: str  # the payload and read key of the value
    unit: str
    noun: str  # "Das Gewicht" / "Der Körperfettanteil"
    confirm: str
    missing: str
    # Accepted range; None takes the canonical ``METRIC_BOUNDS`` of the kind.
    bounds: tuple[float, float] | None = None


WEIGHT_ENTRY: Final = BodyEntryKind(
    MetricKind.WEIGHT,
    "kg",
    "kg",
    "Das Gewicht",
    "Das Löschen eines Gewichts muss bestätigt werden.",
    "Für diesen Tag gibt es kein eingetragenes Gewicht.",
    # The range the body readers accept (canonical_body, WEIGHT_BOUNDS), so a
    # saved entry is always shown and used (#3418).
    (30.0, 300.0),
)
BODY_FAT_ENTRY: Final = BodyEntryKind(
    MetricKind.BODY_FAT,
    "pct",
    "%",
    "Der Körperfettanteil",
    "Das Löschen eines Körperfettwerts muss bestätigt werden.",
    "Für diesen Tag gibt es keinen eingetragenen Körperfettwert.",
    # The range the body readers and the lean mass accept (canonical_body,
    # BODY_FAT_BOUNDS), so a saved entry is always shown and used.
    (2.0, 70.0),
)


class BodyEntryService:
    """Save, list and remove the athlete's own values of one body metric, one per day."""

    def __init__(
        self,
        database_manager: Any,
        now: Callable[[], str],
        today: Callable[[], date],
        entry: BodyEntryKind = WEIGHT_ENTRY,
        on_change: Callable[[date], str] | None = None,
    ) -> None:
        self._database_manager = database_manager
        self._now = now
        self._today = today
        self._entry = entry
        # Mirrors the changed day to Garmin Connect after the commit (#3403);
        # its outcome is the receipt's ``remote``.
        self._on_change = on_change

    def read(self) -> dict[str, Any]:
        """Every active entry, newest first, each with its date and value."""
        with self._database_manager.reader() as db:
            cursor = db.execute(
                _ROWS_SQL,
                (self._entry.kind.value, SourceKind.ATHLETE_ENTERED.value),
            )
            names = [column[0] for column in cursor.description]
            rows = [
                row if isinstance(row, Mapping) else dict(zip(names, row, strict=True))
                for row in cursor.fetchall()
            ]
        return {
            "entries": [
                {"date": str(row["observed_on"]), self._entry.key: float(row["value"])}
                for row in rows
            ]
        }

    def save(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Save one value; an entry of the same day is replaced."""
        day = self._day(payload.get("date"))
        value = self._value(payload.get(self._entry.key))
        with self._database_manager.unit_of_work() as db:
            self._retire(db, day)
            canonical_store.upsert_observation(
                db,
                Observation(
                    kind=self._entry.kind,
                    value=MetricValue(value, self._entry.unit),
                    observed_on=day,
                    observed_at=None,
                    provenance=local_provenance(),
                ),
                mapper_version=MAPPER_VERSION,
                now=self._now(),
            )
        return self._changed(day)

    def remove(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Retire the entry of one day; the request must be confirmed."""
        if payload.get("confirmed") is not True:
            raise AppError(400, self._entry.confirm)
        day = self._day(payload.get("date"))
        with self._database_manager.unit_of_work() as db:
            if not self._retire(db, day):
                raise AppError(404, self._entry.missing)
        return self._changed(day)

    def _changed(self, day: date) -> dict[str, Any]:
        read = self.read()
        if self._on_change is not None:
            read["remote"] = self._on_change(day)
        return read

    def _retire(self, db: Any, day: date) -> int:
        cursor = db.execute(
            "UPDATE observations SET state = 'retired', updated_at = ? "
            "WHERE metric = ? AND provider IS NULL AND source_kind = ? "
            "AND observed_on = ? AND state = 'active'",
            (
                self._now(),
                self._entry.kind.value,
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

    def _value(self, value: Any) -> float:
        entry = self._entry
        try:
            number = float(str(value).replace(",", "."))
        except ValueError as exc:
            raise AppError(
                400, f"{entry.noun} braucht einen Wert in {entry.unit}."
            ) from exc
        lower, upper = entry.bounds or METRIC_BOUNDS[entry.kind]
        if not lower <= number <= upper:
            raise AppError(
                400,
                f"{entry.noun} muss zwischen {lower:g} und {upper:g} "
                f"{entry.unit} liegen.",
            )
        return round(number, 1)

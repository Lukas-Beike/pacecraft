"""3-site skinfolds the athlete enters, and the body fat they give (#3099).

One measurement is the three sites of one protocol on one day, stored as three
athlete-entered observations (mm, no provider), so the raw folds stay and the
body fat is recomputed on each read. The sites tell the protocol: chest, abdomen
and thigh for men, triceps, suprailiac and thigh for women (Jackson-Pollock).
The age comes from the birth year in the profile; without one the body fat is
None and the folds are still kept.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Final

from backend.athlete.body_composition import (
    SKINFOLD_BOUNDS_MM,
    SKINFOLD_SITES,
    Sex,
    body_density,
    outside_fitted_ages,
    siri_body_fat_pct,
)
from backend.canonical.records import MetricValue, Observation, Provenance
from backend.canonical.vocabulary import MetricKind, RecordingOrigin, SourceKind
from backend.db import canonical_store
from backend.errors import AppError

SITE_KINDS: Final = {
    "chest": MetricKind.SKINFOLD_CHEST,
    "abdomen": MetricKind.SKINFOLD_ABDOMEN,
    "thigh": MetricKind.SKINFOLD_THIGH,
    "triceps": MetricKind.SKINFOLD_TRICEPS,
    "suprailiac": MetricKind.SKINFOLD_SUPRAILIAC,
}
_SITE_OF: Final = {kind.value: site for site, kind in SITE_KINDS.items()}
LOCAL_LABEL: Final = "Lokal"
MAPPER_VERSION: Final = 1
LIST_LIMIT: Final = 20
_SITE_LABELS: Final = {
    "chest": "Brust",
    "abdomen": "Bauch",
    "thigh": "Oberschenkel",
    "triceps": "Trizeps",
    "suprailiac": "Hüfte",
}
_ROWS_SQL: Final = (
    "SELECT metric, value, observed_on FROM observations "
    f"WHERE metric IN ({', '.join('?' * len(SITE_KINDS))}) "
    "AND provider IS NULL AND source_kind = ? AND state = 'active'"
)


@dataclass(frozen=True, slots=True)
class SkinfoldMeasurement:
    measured_on: date
    sex: Sex
    folds_mm: Mapping[str, float]
    body_fat_pct: float | None  # None without a birth year in the profile
    # The age lies outside the equation's fitted range (M13); the value extrapolates.
    age_outside_fit: bool = False


def local_provenance() -> Provenance:
    """An athlete-entered value without a provider, labelled "Lokal" (#3099, #3252)."""
    return Provenance(
        provider=None,
        source_kind=SourceKind.ATHLETE_ENTERED,
        source_record_id=None,
        recorded_by=RecordingOrigin.MANUAL,
        retrieved_at=None,
        label=LOCAL_LABEL,
    )


def _protocol(sites: set[str]) -> Sex | None:
    return next(
        (sex for sex, wanted in SKINFOLD_SITES.items() if set(wanted) == sites), None
    )


def _body_fat(
    sex: Sex, folds: Mapping[str, float], day: date, birth_year: int | None
) -> float | None:
    if birth_year is None:
        return None
    try:
        return round(
            siri_body_fat_pct(body_density(sex, folds, day.year - birth_year)), 1
        )
    except ValueError:
        return None  # an age outside the equations' range


def measurements_from_rows(
    rows: list[tuple[str, float, str]], birth_year: int | None
) -> list[SkinfoldMeasurement]:
    """Complete measurements from (metric, mm, day) rows, newest first.

    A day without one full protocol is skipped.
    """
    days: dict[str, dict[str, float]] = {}
    for metric, millimetres, day_text in rows:
        days.setdefault(str(day_text), {})[_SITE_OF[str(metric)]] = float(millimetres)
    found = []
    for day_text, folds in sorted(days.items(), reverse=True):
        sex = _protocol(set(folds))
        if sex is None:
            continue
        day = date.fromisoformat(day_text)
        fat = _body_fat(sex, folds, day, birth_year)
        outside = (
            fat is not None
            and birth_year is not None
            and outside_fitted_ages(sex, day.year - birth_year)
        )
        found.append(SkinfoldMeasurement(day, sex, folds, fat, outside))
    return found


def birth_year_of(profile_json: str | None) -> int | None:
    """The profile's birth year, or None when it is missing or unreadable."""
    try:
        text = str(json.loads(profile_json or "{}").get("birth_year") or "")
        return int(text) if text else None
    except TypeError, ValueError, AttributeError:
        return None


def public_measurement(measurement: SkinfoldMeasurement) -> dict[str, Any]:
    return {
        "date": measurement.measured_on.isoformat(),
        "sex": measurement.sex.value,
        "folds_mm": [
            {
                "site": site,
                "label": _SITE_LABELS[site],
                "mm": measurement.folds_mm[site],
            }
            for site in SKINFOLD_SITES[measurement.sex]
        ],
        "body_fat_pct": measurement.body_fat_pct,
        "age_outside_fit": measurement.age_outside_fit,
    }


class SkinfoldService:
    """Save, list and remove skinfold measurements, and read the one in effect on a day."""

    def __init__(
        self,
        database_manager: Any,
        profile_json: Callable[[Any], str | None],
        now: Callable[[], str],
        today: Callable[[], date],
        on_change: Callable[[date], str] | None = None,
    ) -> None:
        self._database_manager = database_manager
        # ``ProfileRepository.get``: the stored profile JSON for the birth year.
        self._profile_json = profile_json
        self._now = now
        self._today = today
        # Mirrors the changed day's body fat to Garmin Connect (#3403).
        self._on_change = on_change

    def _measurements(
        self, db: Any, *, until: date | None = None
    ) -> list[SkinfoldMeasurement]:
        sql, params = (
            _ROWS_SQL,
            [
                *(kind.value for kind in SITE_KINDS.values()),
                SourceKind.ATHLETE_ENTERED.value,
            ],
        )
        if until is not None:
            sql, params = f"{sql} AND observed_on <= ?", [*params, until.isoformat()]
        cursor = db.execute(sql, tuple(params))
        names = [column[0] for column in cursor.description]
        rows = [
            (record["metric"], record["value"], record["observed_on"])
            for record in (
                row if isinstance(row, Mapping) else dict(zip(names, row, strict=True))
                for row in cursor.fetchall()
            )
        ]
        return measurements_from_rows(rows, birth_year_of(self._profile_json(db)))

    def skinfold_on(self, db: Any, day: date) -> SkinfoldMeasurement | None:
        """The latest complete measurement on or before ``day``, or None."""
        found = self.measurements_on(db, day)
        return found[0] if found else None

    def measurements_on(self, db: Any, day: date) -> list[SkinfoldMeasurement]:
        """Every complete measurement on or before ``day``, newest first."""
        return self._measurements(db, until=day)

    def read(self) -> dict[str, Any]:
        with self._database_manager.reader() as db:
            found = self._measurements(db)
            birth_year = birth_year_of(self._profile_json(db))
        return {
            "measurements": [public_measurement(item) for item in found[:LIST_LIMIT]],
            "birth_year_known": birth_year is not None,
            "sites": {sex.value: list(sites) for sex, sites in SKINFOLD_SITES.items()},
        }

    def save(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Save one measurement; a measurement of the same day is replaced."""
        day = self._day(payload.get("date"))
        folds = self._folds(payload.get("folds_mm"))
        with self._database_manager.unit_of_work() as db:
            self._retire(db, day)
            for site, millimetres in folds.items():
                observation = Observation(
                    kind=SITE_KINDS[site],
                    value=MetricValue(millimetres, "mm"),
                    observed_on=day,
                    observed_at=None,
                    provenance=local_provenance(),
                )
                canonical_store.upsert_observation(
                    db, observation, mapper_version=MAPPER_VERSION, now=self._now()
                )
        return self._changed(day)

    def remove(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Retire the measurement of one day; the request must be confirmed."""
        if payload.get("confirmed") is not True:
            raise AppError(400, "Das Löschen einer Messung muss bestätigt werden.")
        day = self._day(payload.get("date"))
        with self._database_manager.unit_of_work() as db:
            if not self._retire(db, day):
                raise AppError(404, "Für diesen Tag gibt es keine Hautfaltenmessung.")
        return self._changed(day)

    def _changed(self, day: date) -> dict[str, Any]:
        read = self.read()
        if self._on_change is not None:
            read["garmin"] = self._on_change(day)
        return read

    def body_fat_on(self, db: Any, day: date) -> float | None:
        """The body fat of a measurement taken exactly on ``day``, or None."""
        newest = self.skinfold_on(db, day)
        if newest is None or newest.measured_on != day:
            return None
        return newest.body_fat_pct

    def _retire(self, db: Any, day: date) -> int:
        kinds = [kind.value for kind in SITE_KINDS.values()]
        cursor = db.execute(
            "UPDATE observations SET state = 'retired', updated_at = ? "
            f"WHERE metric IN ({', '.join('?' * len(kinds))}) AND provider IS NULL "
            "AND observed_on = ? AND state = 'active'",
            (self._now(), *kinds, day.isoformat()),
        )
        return int(cursor.rowcount or 0)

    def _day(self, value: Any) -> date:
        try:
            day = date.fromisoformat(str(value or ""))
        except ValueError as exc:
            raise AppError(400, "Das Messdatum fehlt oder ist ungültig.") from exc
        if day > self._today():
            raise AppError(400, "Das Messdatum darf nicht in der Zukunft liegen.")
        return day

    @staticmethod
    def _folds(value: Any) -> dict[str, float]:
        if not isinstance(value, Mapping) or _protocol(set(value)) is None:
            raise AppError(
                400,
                "Bitte genau drei Stellen messen: Brust, Bauch, Oberschenkel "
                "oder Trizeps, Hüfte, Oberschenkel.",
            )
        lower, upper = SKINFOLD_BOUNDS_MM
        folds: dict[str, float] = {}
        for site, raw in value.items():
            try:
                millimetres = float(str(raw).replace(",", "."))
            except ValueError as exc:
                raise AppError(400, "Jede Hautfalte braucht einen Wert in mm.") from exc
            if not lower <= millimetres <= upper:
                raise AppError(
                    400,
                    f"Eine Hautfalte muss zwischen {lower:g} und {upper:g} mm liegen.",
                )
            folds[str(site)] = round(millimetres, 1)
        return folds

"""The local LT2 estimates of one athlete and their adoption (#3273).

Garmin's LT2 stays where it is (the current metrics); this service adds the two
local estimates beside it: DFA-a1 at 0.5 for runs (heart rate, and the pace at
that heart rate from the athlete's own steady runs, card M9) and critical speed
or power over 42 days (card M4). The profile and the Coach read them; only the
athlete's explicit adoption (``adopt``) writes, one value at a time (F8), as a
local zone configuration valid from today with its origin, through the zone
save path with its receipt and undo. Nothing is adopted automatically.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Sequence
from datetime import date, timedelta
from typing import Any

from backend.activities.zone_settings import effective_zone_configuration
from backend.canonical.domain_records import ZoneConfiguration
from backend.canonical.first_threshold import stored_bpm
from backend.canonical.vocabulary import Sport, ZoneConfigurationMetric
from backend.db import activity_detail_store
from backend.errors import AppError
from backend.performance.critical_power import critical_power_speed
from backend.performance.pace_at_hr import LT1_DAYS, output_at_heart_rate
from backend.performance.second_threshold_estimate import (
    CRITICAL_WINDOW_DAYS,
    ESTIMATE_DAYS,
    critical_second_threshold,
    dfa_second_threshold,
)

RUNS_ONLY = {"status": "unknown", "reason": "nur aus Läufen", "sessions": 0}
# Above this share of the value in effect an adoption needs a second confirmation (F9).
CONFIRM_DEVIATION_SHARE = 0.05
# Adoptable value -> (estimate field, zone metric, label, unit).
_VALUES: dict[str, tuple[str, ZoneConfigurationMetric, str, str]] = {
    "heart_rate": (
        "heart_rate_bpm",
        ZoneConfigurationMetric.HEART_RATE,
        "Herzfrequenz",
        "bpm",
    ),
    "pace": ("pace_seconds_per_km", ZoneConfigurationMetric.PACE, "Pace", "s/km"),
    "power": ("power_w", ZoneConfigurationMetric.POWER, "Leistung", "W"),
}
# The server-side baseline of the LT2 change notice (F10): per sport the values
# the athlete last saw, null for "unbekannt".
NOTICE_BASELINE_KEY = "lt2_notice_baselines"
NOTICE_VALUES: dict[str, tuple[str, ...]] = {
    "run": ("provider_hr", "provider_pace", "dfa_hr", "dfa_pace", "cs_pace"),
    "ride": ("provider_power", "provider_hr", "cp_power"),
}
# The run step test's LT2 (#3833), adopted with the DFA-a1 origin.
STEP_TEST_SOURCE = "step_test"
_SOURCES = {
    "run": ("dfa_alpha1", "critical_speed"),
    "ride": ("critical_power",),
}


class SecondThresholdService:
    """Read the local LT2 estimates per sport."""

    def __init__(
        self,
        manager: Any,
        profile_service: Any,
        lthr_resolver: Callable[[], Any],
        curve_records: Callable[[int], Sequence[Mapping[str, Any]]],
        today: Callable[[], date],
        zones: Callable[[], Any] | None = None,
        key_values: Any = None,
    ) -> None:
        self._zones = zones
        self._key_values = key_values
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

    def notice_baselines(self) -> dict[str, Any]:
        """The values per sport the athlete last saw in the LT2 change notice (F10)."""
        if self._key_values is None:
            return {}
        with self._manager.reader() as db:
            return _baselines(self._key_values.get(db, NOTICE_BASELINE_KEY))

    def save_notice_baseline(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Store the values of one sport the athlete has now seen; returns all sports."""
        if self._key_values is None:
            raise AppError(503, "Der Hinweisstand ist nicht verfügbar.")
        sport = str(payload.get("sport") or "")
        values = payload.get("values")
        if sport not in NOTICE_VALUES or not isinstance(values, dict):
            raise AppError(400, "Unbekannte Sportart oder Werte.")
        seen = {key: _seen_value(values.get(key)) for key in NOTICE_VALUES[sport]}
        with self._manager.unit_of_work() as db:
            stored = _baselines(self._key_values.get(db, NOTICE_BASELINE_KEY))
            stored[sport] = seen
            self._key_values.set(
                db, NOTICE_BASELINE_KEY, json.dumps(stored, sort_keys=True)
            )
        return stored

    def _first_threshold_bpm(self) -> float | None:
        """The stored LT1 heart rate of runs today, or None."""
        return stored_bpm(self._profile_service.get())

    def adopt(
        self,
        payload: Mapping[str, Any],
        step_test: Callable[[str], Mapping[str, Any] | None] | None = None,
    ) -> dict[str, Any]:
        """Adopt one shown value of a local estimate as the LT2 threshold in effect.

        ``payload``: ``sport``, ``source`` (``dfa_alpha1``, ``critical_speed``,
        ``critical_power`` or ``step_test``), ``value`` (``heart_rate``, ``pace`` or
        ``power``) and ``shown`` (the number displayed, so a changed estimate is
        refused). A value more than 5 % from the one in effect needs
        ``confirm_deviation``; a value the athlete entered needs ``replace_local``.
        ``step_test`` takes the good LT2 of the run step test ``activity_id``
        (#3833), read by ``step_test``; it is stored with the origin ``dfa_alpha1``.
        """
        if self._zones is None:
            raise AppError(503, "Die Zonen sind nicht verfügbar.")
        sport, source, value = (
            str(payload.get(key) or "") for key in ("sport", "source", "value")
        )
        tested = source == STEP_TEST_SOURCE and sport == "run" and value != "power"
        if (
            source not in _SOURCES.get(sport, ()) and not tested
        ) or value not in _VALUES:
            raise AppError(400, "Unbekannte Schätzung oder unbekannter Wert.")
        field, metric, label, unit = _VALUES[value]
        estimate = (
            _step_test_value(payload, step_test)
            if tested
            else self.estimates()[sport][source]
        )
        number = _shown_number(payload, estimate, field)
        zones = self._zones()
        current = effective_zone_configuration(
            zones.configurations(), metric, Sport(sport), day=self._today()
        )
        if current is None:
            raise AppError(
                409, "Erst Zonen anlegen, dann übernehmen.", reason="lt2_no_zones"
            )
        _check_replace(current, payload, label)
        threshold = (
            1000 / number if metric is ZoneConfigurationMetric.PACE else float(number)
        )
        _check_deviation(current, threshold, payload, label, number, unit)
        try:
            receipt: dict[str, Any] = zones.save_adopted(
                _adopted(
                    current,
                    sport,
                    metric,
                    threshold,
                    "dfa_alpha1" if tested else source,
                )
            )
        except AppError as exc:
            if exc.status != 400:
                raise
            raise AppError(
                409,
                "Zonengrenzen passen nicht zur neuen Schwelle. Bitte die Zonen bearbeiten.",
                reason="lt2_zone_limits",
            ) from exc
        return receipt


def _shown_number(
    payload: Mapping[str, Any], estimate: Mapping[str, Any], field: str
) -> Any:
    """The estimate's value as shown; refused when unknown or changed since."""
    number = estimate.get(field) if estimate.get("status") == "ok" else None
    if number is None:
        raise AppError(409, "Diese Schätzung liegt nicht vor.", reason="lt2_unknown")
    if payload.get("shown") != number:
        raise AppError(
            409,
            "Die Schätzung hat sich geändert. Bitte die Anzeige aktualisieren.",
            reason="lt2_changed",
        )
    return number


def _step_test_value(
    payload: Mapping[str, Any],
    step_test: Callable[[str], Mapping[str, Any] | None] | None,
) -> Mapping[str, Any]:
    """The step test's LT2 when it is good, else an unknown value (#3833)."""
    activity_id = str(payload.get("activity_id") or "")
    tested = None if step_test is None or not activity_id else step_test(activity_id)
    value = (tested or {}).get("lt2") or {}
    return value if value.get("quality") == "good" else {"status": "unknown"}


def _check_replace(
    current: ZoneConfiguration, payload: Mapping[str, Any], label: str
) -> None:
    """A value the athlete entered is replaced only when the request says so."""
    entered = current.provenance.provider is None and current.threshold_origin is None
    if entered and payload.get("replace_local") is not True:
        raise AppError(
            409,
            f"{label}: ein selbst eingetragener Wert gilt. Zum Ersetzen bitte bestätigen.",
            reason="lt2_replace_local",
        )


def _check_deviation(
    current: ZoneConfiguration,
    threshold: float,
    payload: Mapping[str, Any],
    label: str,
    shown: float,
    unit: str,
) -> None:
    """More than 5 % from the value in effect needs the second confirmation (F9)."""
    if current.threshold is None or current.threshold <= 0:
        return
    share = abs(threshold - current.threshold) / current.threshold
    if share > CONFIRM_DEVIATION_SHARE and payload.get("confirm_deviation") is not True:
        raise AppError(
            409,
            f"{label} {shown:g} {unit} weicht {round(share * 100)} % vom geltenden Wert ab.",
            reason="lt2_deviation",
        )


def _adopted(
    current: ZoneConfiguration,
    sport: str,
    metric: ZoneConfigurationMetric,
    threshold: float,
    source: str,
) -> dict[str, Any]:
    """The local configuration: the new threshold with the limits in effect (T1)."""
    percent = current.percent_limits is not None
    return {
        "sport": sport,
        "metric": metric.value,
        "environment": None,
        "threshold": round(threshold, 3),
        "maximum": current.maximum,
        "limits": None if percent else list(current.limits or ()),
        "percent_limits": list(current.percent_limits or ()) if percent else None,
        "threshold_origin": source,
    }


def _seen_value(value: object) -> float | None:
    """One seen value: a positive finite number, or None for "unbekannt"."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise AppError(400, "Werte müssen Zahlen oder null sein.")
    if not math.isfinite(value) or not 0 < value < 100_000:
        raise AppError(400, "Werte müssen Zahlen oder null sein.")
    return float(value)


def _baselines(text: str | None) -> dict[str, Any]:
    """The stored baselines; unreadable text or an unknown sport reads as none."""
    try:
        stored = json.loads(text) if text else {}
    except ValueError:
        return {}
    if not isinstance(stored, dict):
        return {}
    return {
        sport: values
        for sport, values in stored.items()
        if sport in NOTICE_VALUES and isinstance(values, dict)
    }

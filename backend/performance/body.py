"""Historical body metrics and cycling power-to-weight projections."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any

from backend.canonical.decimal_text import comma_decimal_float
from backend.canonical.vocabulary import ProviderId
from backend.performance.activity_validation import bounded_performance_metric
from backend.performance.canonical_body import CanonicalBodyInputs
from backend.performance.display_sources import preferred_providers
from backend.performance.freshness import GarminSyncStatus
from backend.providers.source_labels import SourceView, provider_label

GARMIN_SOURCE = provider_label(ProviderId.GARMIN, SourceView.BODY_HISTORY)
# The periods of the base read; a longer one is read on demand with its own span.
BODY_PERIODS = (28, 91)
# None: the weights the athlete entered by hand (#3252).
_MEASUREMENT_SOURCES = {ProviderId.GARMIN: GARMIN_SOURCE, None: "Lokal"}


def _number(value: Any) -> float | None:
    try:
        result = comma_decimal_float(value)
    except TypeError, ValueError:
        return None
    return result if math.isfinite(result) else None


def _sync_timestamp(status: GarminSyncStatus, source: str) -> Any:
    details = status.source_freshness.get(source)
    if details is not None:
        return details.get("fetched_at") or details.get("synced_at") or status.synced_at
    return status.synced_at


def _garmin_ftp(
    garmin: GarminSyncStatus, today: date, history: Mapping[date, float]
) -> dict[date, tuple[float, str, Any]]:
    """Garmin's cycling FTP per day from the canonical history."""
    synced_at = _sync_timestamp(garmin, "cycling_ftp_history")
    return {
        day: (float(value), day.isoformat(), synced_at)
        for day, value in history.items()
        if day <= today
        and bounded_performance_metric("cycling_ftp_watts", value) is not None
    }


def _point(day: date, record: tuple[float, str, Any] | None) -> dict[str, Any]:
    if record is None:
        return {"date": day.isoformat(), "value": None}
    value, observed_at, synced_at = record
    return {
        "date": day.isoformat(),
        "value": value,
        "observed_at": observed_at,
        "synced_at": synced_at,
        "sync_timestamp": synced_at,
    }


def _weekly(
    points: list[dict[str, Any]], start: date, end: date
) -> list[dict[str, Any]]:
    weeks: list[dict[str, Any]] = []
    # Fixed rolling 7-day buckets keep an 84-day selection to exactly 12 weeks;
    # each week shows its latest measurement.
    for offset in range(0, (end - start).days + 1, 7):
        bucket_start = start + timedelta(days=offset)
        bucket_end = min(bucket_start + timedelta(days=6), end)
        items = [
            point
            for point in points
            if bucket_start.isoformat() <= point["date"] <= bucket_end.isoformat()
            and point.get("value") is not None
        ]
        weeks.append(
            {
                "week_start": bucket_start.isoformat(),
                "week_end": bucket_end.isoformat(),
                "value": round(float(items[-1]["value"]), 2) if items else None,
                "count": len(items),
                "dates": [item["date"] for item in items],
            }
        )
    return weeks


def _series(
    provider: ProviderId | None,
    unit: str,
    records: dict[date, tuple[float, str, Any]],
    start: date,
    end: date,
) -> dict[str, Any]:
    points = [
        _point(
            start + timedelta(days=index), records.get(start + timedelta(days=index))
        )
        for index in range((end - start).days + 1)
    ]
    return {
        "source": _MEASUREMENT_SOURCES[provider],
        "source_provider": None if provider is None else provider.value,
        "unit": unit,
        "points": points,
        "weekly": _weekly(points, start, end),
    }


# The source of one day's value in the merged body series (#3464).
ENTERED_LABEL = "eingetragen"
GARMIN_LABEL = GARMIN_SOURCE
# The profile's earlier body fat values are 3-site skinfolds (#3557, #3569).
SKINFOLD_LABEL = "Hautfalten"


def _day_series(
    unit: str,
    garmin: dict[date, tuple[float, str, Any]],
    entered: dict[date, tuple[float, str, Any]],
    start: date,
    end: date,
    skinfolds: Mapping[date, tuple[float, str, Any]] | None = None,
) -> dict[str, Any]:
    """One series of the athlete's day values: an own value wins its day.

    An entered weight or body fat is written to that day's Garmin entry
    (#3403), so the athlete has one value per day, not a local and a Garmin
    one; every other day shows Garmin's value. A profile skinfold counts as
    the athlete's own (#3569). Each point names its source in
    ``source_label`` (#3464).
    """
    own = {**(skinfolds or {}), **entered}
    series = _series(
        ProviderId.GARMIN if garmin or not own else None,
        unit,
        {**garmin, **own},
        start,
        end,
    )
    labels = {
        **dict.fromkeys(garmin, GARMIN_LABEL),
        **dict.fromkeys(skinfolds or {}, SKINFOLD_LABEL),
        **dict.fromkeys(entered, ENTERED_LABEL),
    }
    for point in series["points"]:
        if point["value"] is not None:
            point["source_label"] = labels[date.fromisoformat(point["date"])]
    series["edge_means"] = _edge_means(series["points"], start, end)
    return series


def _edge_means(
    points: list[dict[str, Any]], start: date, end: date
) -> dict[str, Any] | None:
    """The change of a window as the mean of its last days minus the mean of its first.

    One definition for every reader (the Körper charts, the Coach; the Verlauf
    card shows the same edge means): 7 days at each end of a 4-week window, 14
    for a longer one. A single weigh-in does not move it by itself. ``None``
    while an edge has no value or the edges would meet (#4037, athlete decision
    2026-11-24).
    """
    days = (end - start).days + 1
    span = 7 if days <= 28 else 14
    if days < 2 * span:
        return None
    first_end = (start + timedelta(days=span - 1)).isoformat()
    last_start = (end - timedelta(days=span - 1)).isoformat()
    values = [(p["date"], p["value"]) for p in points if p["value"] is not None]
    first = [value for day, value in values if day <= first_end]
    last = [value for day, value in values if day >= last_start]
    if not first or not last:
        return None
    first_mean = sum(first) / len(first)
    last_mean = sum(last) / len(last)
    return {
        "span_days": span,
        "first_mean": round(first_mean, 2),
        "last_mean": round(last_mean, 2),
        "change": round(last_mean - first_mean, 2),
        "first_count": len(first),
        "last_count": len(last),
    }


def _quotient_series(
    ftp: dict[date, tuple[float, str, Any]],
    weights: dict[ProviderId, dict[date, tuple[float, str, Any]]],
    start: date,
    end: date,
) -> dict[str, Any]:
    """Garmin FTP per kg with a weight from the seven preceding days.

    The newest weight wins (Garmin on a tie).
    """
    all_weights = [
        (day, record, weight_provider)
        for weight_provider, records in weights.items()
        for day, record in records.items()
    ]
    points: list[dict[str, Any]] = []
    for index in range((end - start).days + 1):
        day = start + timedelta(days=index)
        ftp_record = ftp.get(day)
        point = _point(day, None)
        if ftp_record is not None:
            ftp_value, observed_at, synced_at = ftp_record
            candidates = [
                item
                for item in all_weights
                if day - timedelta(days=7) <= item[0] <= day
            ]
            weight_item = max(
                candidates,
                key=lambda item: (item[0], item[2] is ProviderId.GARMIN),
                default=None,
            )
            point.update(
                {
                    "ftp_watts": ftp_value,
                    "observed_at": observed_at,
                    "synced_at": synced_at,
                    "ftp_observed_at": observed_at,
                    "ftp_synced_at": synced_at,
                }
            )
            if weight_item is not None:
                weight_day, weight_record, weight_provider = weight_item
                weight_value = weight_record[0]
                point.update(
                    {
                        "value": round(ftp_value / weight_value, 3),
                        "weight_kg": weight_value,
                        "weight_source": _MEASUREMENT_SOURCES[weight_provider],
                        "weight_observed_at": weight_record[1],
                        "weight_synced_at": weight_record[2],
                        "weight_age_days": (day - weight_day).days,
                    }
                )
        points.append(point)
    return {
        "source": GARMIN_SOURCE,
        "source_provider": ProviderId.GARMIN.value,
        "power_method": "FTP",
        "unit": "W/kg",
        "points": points,
        "weekly": _weekly(points, start, end),
    }


def _stamped(
    values: Mapping[date, float], synced_at: Any
) -> dict[date, tuple[float, str, Any]]:
    """Attach the legacy observed_at and synced_at stamps to canonical values."""
    return {day: (value, day.isoformat(), synced_at) for day, value in values.items()}


def body_history(
    garmin: GarminSyncStatus,
    today: date,
    *,
    canonical: CanonicalBodyInputs,
    garmin_ftp: Mapping[date, float],
    periods: Sequence[int] = BODY_PERIODS,
) -> dict[str, Any]:
    """Return API-ready charts per period (``28d``, ``91d``) under performance.history.body.

    Daily points include observed_at and synced_at when measured. Derived power-
    to-weight points also retain separate FTP and weight dates, sync times, and
    weight age. Missing dates stay null; profile values are never observations.

    Garmin weight, body fat and FTP come solely from canonical observations. The
    W/kg series divides Garmin's dated FTP only. The Garmin sync status supplies
    only sync times. Weight and body fat are one series each: the athlete's own
    entry wins its day over Garmin's value, and each point names its source
    (#3252, #3402, #3464).
    """
    garmin_synced_at = _sync_timestamp(garmin, "weight")
    garmin_values = {
        "weight_kg": _stamped(canonical.garmin_weight_kg, garmin_synced_at),
        "body_fat_pct": _stamped(canonical.garmin_body_fat_pct, garmin_synced_at),
    }
    weights = {ProviderId.GARMIN: garmin_values["weight_kg"]}
    entered = _stamped(canonical.entered_weight_kg, None)
    entered_fat = _stamped(canonical.entered_body_fat_pct, None)
    ftp = _garmin_ftp(garmin, today, garmin_ftp)
    windows: dict[str, Any] = {}
    for days in periods:
        key = f"{days}d"
        start = today - timedelta(days=days - 1)
        windows[key] = {
            "start": start.isoformat(),
            "end": today.isoformat(),
            "days": days,
            "metrics": {
                "weight_kg": [
                    _day_series("kg", garmin_values["weight_kg"], entered, start, today)
                ],
                "body_fat_pct": [
                    _day_series(
                        "%",
                        garmin_values["body_fat_pct"],
                        entered_fat,
                        start,
                        today,
                        _stamped(canonical.skinfold_body_fat_pct, None),
                    )
                ],
                "cycling_w_per_kg": [_quotient_series(ftp, weights, start, today)],
            },
        }
    return {
        "version": 1,
        "default_window": f"{periods[-1]}d",
        "windows": windows,
        "preferred_providers": preferred_providers(),
    }

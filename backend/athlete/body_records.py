"""The athlete's body records: weight and body fat as measurements (#3640).

Körper enters them; the profile holds neither. This module says which
weigh-ins and body fat values count (the source rule, #3557), the current
weight from the weigh-in line (#3551) and the body fat in effect on a day
(skinfolds, else entered, else a device, #3362, #3402). The energy view, the
current metrics and every calculation read them here.
"""

from __future__ import annotations

import statistics
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, timedelta
from typing import Any, Final

from backend.athlete.measurements import finite_float
from backend.athlete.skinfolds import SkinfoldMeasurement
from backend.canonical.records import Observation
from backend.canonical.vocabulary import ProviderId
from backend.providers.source_labels import SourceView, provider_label

# A day's current weight is the least-squares line through the weigh-ins of
# this many days up to it, at its latest weigh-in, so a steady change has no
# lag (#3551); with fewer weigh-ins or a shorter span, their mean.
CURRENT_WEIGHT_DAYS = 28
MIN_LINE_WEIGHINS = 3
MIN_LINE_SPAN_DAYS = 7
# How far back a weigh-in or body fat still counts for a current value.
RESTING_LOOKBACK_DAYS = 90
# A skinfold taken up to a day after the view's end still counts (#3336).
BODY_GRACE_DAYS = 1

# The weigh-in source rule (athlete decisions, #3557): the app's own records,
# entered (no provider) or a dated profile value (``app``), win their day;
# Garmin's are copies of them and fill only a day without one. Any other
# provider's value, such as Wahoo's stored profile weight, never counts.
APP_SOURCE: Final = "app"
SKINFOLD_LABEL: Final = "Hautfalten"
ENTERED_LABEL: Final = "Lokal"


def weight_trend(weights_kg: Mapping[date, float]) -> dict[date, float]:
    """The current weight per day from the first weigh-in on, without a lag (#3551).

    A day's value is the least-squares line through the weigh-ins of the
    ``CURRENT_WEIGHT_DAYS`` up to it, read at the latest of them; with fewer
    than ``MIN_LINE_WEIGHINS`` or a span below ``MIN_LINE_SPAN_DAYS``, their
    mean. A day without a weigh-in in that span carries the previous value.
    """
    if not weights_kg:
        return {}
    weighed = sorted(weights_kg)
    trend: dict[date, float] = {}
    day = weighed[0]
    while day <= weighed[-1]:
        since = day - timedelta(days=CURRENT_WEIGHT_DAYS - 1)
        recent = weighed[bisect_left(weighed, since) : bisect_right(weighed, day)]
        trend[day] = (
            _line_at_latest([(d, weights_kg[d]) for d in recent])
            if recent
            else trend[day - timedelta(days=1)]
        )
        day += timedelta(days=1)
    return trend


def _line_at_latest(points: Sequence[tuple[date, float]]) -> float:
    """The least-squares line's value at the latest point, else the mean."""
    ys = [kg for _, kg in points]
    latest = points[-1][0]
    if len(points) < MIN_LINE_WEIGHINS or (
        (latest - points[0][0]).days < MIN_LINE_SPAN_DAYS
    ):
        return statistics.fmean(ys)
    xs = [float((d - latest).days) for d, _ in points]
    mean_x, mean_y = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mean_x) ** 2 for x in xs)
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    return mean_y - sxy / sxx * mean_x


def sourced(
    observations: Iterable[Observation], app: Mapping[date, float] | None = None
) -> tuple[dict[date, tuple[float, str]], dict[str, set[date]]]:
    """Per day the value and its source (``app`` or ``garmin``), and per
    provider the days whose values were dropped. Within a source the last one
    read wins; an entered one wins over a profile value of its day."""
    days = {day: (value, APP_SOURCE) for day, value in (app or {}).items()}
    garmin: dict[date, float] = {}
    dropped: dict[str, set[date]] = {}
    for o in observations:
        value = finite_float(o.value.value)
        if value is None or value <= 0 or o.sport is not None:
            continue
        provider = o.provenance.provider
        if provider is None:
            days[o.observed_on] = (value, APP_SOURCE)
        elif provider is ProviderId.GARMIN:
            garmin[o.observed_on] = value
        else:
            dropped.setdefault(provider.value, set()).add(o.observed_on)
    for day, value in garmin.items():
        if day in days:
            dropped.setdefault(ProviderId.GARMIN.value, set()).add(day)
        else:
            days[day] = (value, ProviderId.GARMIN.value)
    return days, dropped


def weighins(
    observations: Iterable[Observation], app: Mapping[date, float] | None = None
) -> dict[date, float]:
    """One weigh-in (or body fat) per day under the source rule of ``sourced``."""
    days = sourced(observations, app)[0]
    return {day: value for day, (value, _) in sorted(days.items())}


def latest_value(observations: Iterable[Observation]) -> float | None:
    """The last day's value under the weigh-in source rule, or None."""
    values = weighins(observations)
    return values[max(values)] if values else None


def current_weight(
    observations: Iterable[Observation], app: Mapping[date, float], day: date
) -> float | None:
    """The current weight on ``day`` under the weigh-in source rule: the same
    lag-free value the formula rows take from ``weight_trend`` (#3551)."""
    return weight_on(weight_trend(weighins(observations, app)), day)


def weight_on(trend: Mapping[date, float], day: date) -> float | None:
    """The line's value on ``day`` (``weight_trend``): the latest day up to it."""
    known = [d for d in trend if d <= day]
    return trend[max(known)] if known else None


def weighin_counts(
    observations: Iterable[Observation], app: Mapping[date, float] | None = None
) -> dict[str, dict[str, int]]:
    """Counts only (#3557): weigh-in days per source and dropped days per provider."""
    days, dropped = sourced(observations, app)
    sources = [source for _, source in days.values()]
    return {
        "by_source": {
            s: sources.count(s) for s in (APP_SOURCE, ProviderId.GARMIN.value)
        },
        "dropped_days": {p: len(d) for p, d in sorted(dropped.items())},
    }


def weighin_sources(
    observations: Iterable[Observation], app: Mapping[date, float] | None = None
) -> dict[date, str]:
    """Each weigh-in day's source after the source rule (#3557, #3638)."""
    return {day: source for day, (_, source) in sourced(observations, app)[0].items()}


def skinfold_body_fats(
    measurements: Iterable[SkinfoldMeasurement], since: date
) -> dict[date, float]:
    """Every complete skinfold measurement on or after ``since`` with a body
    fat, by day (#3617 item 2): a reading followed by another within the same
    week counts too, not only the newest one per weekly checkpoint."""
    return {
        m.measured_on: m.body_fat_pct
        for m in measurements
        if m.measured_on >= since and m.body_fat_pct is not None
    }


def device_body_fats(
    observations: Iterable[Observation], skinfold_days: Iterable[date] = ()
) -> dict[date, tuple[float, Any]]:
    """The entered (provider None, #3402) or Garmin body fat per day (#3362)
    under the source rule of ``sourced``: Garmin's copies fill only a day
    without an entered value or a profile skinfold (``skinfold_days``, #3557).
    """
    days, _ = sourced(observations)
    skinfolds = set(skinfold_days)
    return {
        day: (value, None if source == APP_SOURCE else ProviderId.GARMIN)
        for day, (value, source) in sorted(days.items())
        if source == APP_SOURCE or day not in skinfolds
    }


def _newest_within(values: Mapping[date, Any], day: date) -> date | None:
    """The newest day of ``values`` in the 90 days up to the grace day after ``day``."""
    since = day - timedelta(days=RESTING_LOOKBACK_DAYS)
    until = day + timedelta(days=BODY_GRACE_DAYS)
    return max((d for d in values if since <= d <= until), default=None)


def body_fat_on(
    skinfolds: Mapping[date, float],
    measured: Mapping[date, tuple[float, Any]],
    day: date,
) -> dict[str, Any] | None:
    """The body fat in effect on ``day``: the newest skinfold, else an entered
    one, else a device's or scale's (``device_body_fats``), each within the
    lookback; ``source`` and ``label`` name it, ``measured_on`` its day."""
    if (on := _newest_within(skinfolds, day)) is not None:
        return {"source": "skinfold", "label": SKINFOLD_LABEL,
                "measured_on": on, "body_fat_pct": skinfolds[on]}  # fmt: skip
    for source in ("entered", "device"):
        values = {
            d: value
            for d, value in measured.items()
            if (value[1] is None) is (source == "entered")
        }
        if (on := _newest_within(values, day)) is not None:
            fat, provider = values[on]
            label = (
                ENTERED_LABEL
                if provider is None
                else provider_label(provider, SourceView.BODY_HISTORY)
            )
            return {"source": source, "label": label, "measured_on": on,
                    "body_fat_pct": fat}  # fmt: skip
    return None

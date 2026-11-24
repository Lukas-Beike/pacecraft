"""The weather during one activity, reduced from hourly Open-Meteo values (#1868).

Pure: no I/O, no coordinates. An hourly value at time ``h`` stands for the hour
around it (``h - 30 min`` to ``h + 30 min``), which is the linear interpolation
between two hours for a short activity; precipitation at ``h`` is the sum of the
preceding hour. The activity's value is the mean of the overlapping hours
weighted by overlap (precipitation: the amount that fell during the activity).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final

from backend.providers.activity_weather import HOURLY_FIELDS

# The Historical Forecast API starts in 2022; earlier activities get no weather.
FIRST_DAY: Final = date(2022, 1, 1)
# Hours this close to now may still lack their stored forecast.
SETTLE: Final = timedelta(hours=48)
# Apparent temperature at which the Coach names heat (#1868).
HEAT_APPARENT_C: Final = 25.0
MIN_DURATION: Final = timedelta(minutes=1)
_HOUR: Final = timedelta(hours=1)
_HALF_HOUR: Final = timedelta(minutes=30)
_PRECIPITATION: Final = "precipitation"
_SPEED: Final = "wind_speed_10m"
_DIRECTION: Final = "wind_direction_10m"
# Below this mean wind vector the direction is noise (calm).
CALM_MPS: Final = 0.5
# The headwind component is kept per this many seconds of the stream clock, for
# the report's own windows (#3084); the bins never leave storage and the report.
WIND_BIN_S: Final = 60
# Sectors of the wind relative to the direction of travel (athlete, #3084,
# 2026-11-16): headwind within 45 degrees, tailwind beyond 135, else crosswind.
HEAD_SECTOR_DEG: Final = 45.0
TAIL_SECTOR_DEG: Final = 135.0


@dataclass(frozen=True, slots=True)
class ActivityWeather:
    """Weather means over an activity; None where the hours had no value."""

    temperature_c: float | None
    apparent_temperature_c: float | None
    relative_humidity_pct: float | None
    wind_speed_mps: float | None
    precipitation_mm: float | None
    # The direction the wind came from (meteorological, 10 m), None in calm wind.
    wind_direction_deg: float | None = None

    @property
    def has_value(self) -> bool:
        return any(
            value is not None
            for value in (
                self.temperature_c,
                self.apparent_temperature_c,
                self.relative_humidity_pct,
                self.wind_speed_mps,
                self.precipitation_mm,
                self.wind_direction_deg,
            )
        )


def activity_window(
    start: datetime, duration_s: float | None
) -> tuple[datetime, datetime]:
    """The activity's UTC start and end; an unknown or tiny duration counts a minute."""
    first = start.astimezone(UTC)
    length = timedelta(seconds=max(duration_s or 0.0, MIN_DURATION.total_seconds()))
    return first, first + length


def weather_pending(start: datetime, end: datetime, now: datetime) -> str | None:
    """Why the stage has not looked a session's weather up yet, None when it
    should have: before ``FIRST_DAY``, or not ``SETTLE`` after its end (#4136)."""
    if (start - timedelta(minutes=30)).date() < FIRST_DAY:
        return "before_first_day"
    if end + SETTLE > now:
        return "not_settled"
    return None


def utc_days(start: datetime, end: datetime) -> tuple[date, date]:
    """The UTC days to request: an hour's value reaches half an hour before the
    start, and the precipitation of the preceding hour up to an hour after the end."""
    return (start - _HALF_HOUR).date(), (end + _HOUR).date()


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _overlap_s(
    first: datetime, last: datetime, start: datetime, end: datetime
) -> float:
    return max(0.0, (min(last, end) - max(first, start)).total_seconds())


def _mean(
    times: list[datetime | None],
    values: Any,
    start: datetime,
    end: datetime,
    field: str,
) -> float | None:
    if not isinstance(values, list) or len(values) != len(times):
        return None
    total = weight = 0.0
    for moment, raw in zip(times, values, strict=True):
        value = _number(raw)
        if moment is None or value is None:
            continue
        if field == _PRECIPITATION:
            span = (moment - _HOUR, moment)
            overlap = _overlap_s(*span, start, end)
            total += value * overlap / _HOUR.total_seconds()
        else:
            overlap = _overlap_s(moment - _HALF_HOUR, moment + _HALF_HOUR, start, end)
            total += value * overlap
        weight += overlap
    if weight <= 0:
        return None
    return round(total if field == _PRECIPITATION else total / weight, 2)


def _hour(text: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text)).replace(tzinfo=UTC)
    except ValueError:
        return None


def reduce_hourly(
    payload: Mapping[str, Any], start: datetime, end: datetime
) -> ActivityWeather | None:
    """Reduce the response's hourly rows to the activity, or None without any value."""
    hourly = payload.get("hourly")
    if not isinstance(hourly, Mapping) or not isinstance(hourly.get("time"), list):
        return None
    times = [_hour(text) for text in hourly["time"]]
    # An angle has no arithmetic mean; the direction is the vector mean below.
    means = {
        field: _mean(times, hourly.get(field), start, end, field)
        for field in HOURLY_FIELDS
        if field != _DIRECTION
    }
    result = ActivityWeather(
        temperature_c=means["temperature_2m"],
        apparent_temperature_c=means["apparent_temperature"],
        relative_humidity_pct=means["relative_humidity_2m"],
        wind_speed_mps=means["wind_speed_10m"],
        precipitation_mm=means[_PRECIPITATION],
        wind_direction_deg=_mean_direction(_hours(hourly), start, end),
    )
    return result if result.has_value else None


def heat_note(apparent_temperature_c: float | None) -> str | None:
    """The Coach's heat note from an apparent temperature of 25 C on, else None."""
    if apparent_temperature_c is None or apparent_temperature_c < HEAT_APPARENT_C:
        return None
    value = f"{apparent_temperature_c:.1f}".replace(".", ",")
    return (
        f"Hitze: gefühlte Temperatur {value} °C (ab {HEAT_APPARENT_C:.0f} °C). "
        "Puls, Entkopplung und Tempo nicht als Fitnessänderung lesen."
    )


@dataclass(frozen=True, slots=True)
class Headwind:
    """The wind's mean component along the direction of travel (+ into the wind).

    ``coverage_pct`` is the share of moving time with a direction and a wind
    value; ``bins`` holds the component per ``WIND_BIN_S`` of the stream clock
    (None without a value). The bins describe the course of the session, so they
    stay in storage and the session report like positions.
    """

    mean_mps: float
    coverage_pct: float
    bins: tuple[float | None, ...]
    # Shares of the covered time with headwind and tailwind (descriptive only).
    head_share_pct: float = 0.0
    tail_share_pct: float = 0.0


def headwind(
    payload: Mapping[str, Any],
    start: datetime,
    steps: Sequence[tuple[float, float, float | None]],
) -> Headwind | None:
    """The headwind of moving ``steps`` (stream offset s, seconds, travel bearing).

    The wind of a step is the hour whose box holds it, as for the other values;
    its component is ``V x cos(wind_from - bearing)``. None without any step
    that has both.
    """
    hours = _hours(_mapping(payload.get("hourly")))
    moving = covered = total = head = tail = 0.0
    sums: dict[int, list[float]] = {}
    count = 0
    for offset, seconds, bearing in steps:
        moving += seconds
        count = max(count, int(offset // WIND_BIN_S) + 1)
        wind = _wind_at(hours, start + timedelta(seconds=offset))
        if wind is None or bearing is None:
            continue
        component = wind[0] * math.cos(math.radians(wind[1] - bearing))
        # Calm time stays covered but in no sector: its direction is noise.
        if wind[0] >= CALM_MPS:
            angle = abs((wind[1] - bearing + 180) % 360 - 180)
            head += seconds if angle <= HEAD_SECTOR_DEG else 0.0
            tail += seconds if angle >= TAIL_SECTOR_DEG else 0.0
        covered += seconds
        total += component * seconds
        cell = sums.setdefault(int(offset // WIND_BIN_S), [0.0, 0.0])
        cell[0] += component * seconds
        cell[1] += seconds
    if covered <= 0:
        return None
    return Headwind(
        mean_mps=round(total / covered, 2),
        coverage_pct=round(100 * covered / moving, 1),
        head_share_pct=round(100 * head / covered, 1),
        tail_share_pct=round(100 * tail / covered, 1),
        bins=tuple(
            round(sums[index][0] / sums[index][1], 2) if index in sums else None
            for index in range(count)
        ),
    )


def _hours(
    hourly: Mapping[str, Any],
) -> list[tuple[datetime, float, float]]:
    """The hours with a wind speed and direction."""
    times = hourly.get("time")
    speeds = hourly.get(_SPEED)
    directions = hourly.get(_DIRECTION)
    if not (
        isinstance(times, list)
        and isinstance(speeds, list)
        and isinstance(directions, list)
    ):
        return []
    hours = []
    for text, speed, direction in zip(times, speeds, directions, strict=False):
        moment, value, angle = _hour(text), _number(speed), _number(direction)
        if moment is not None and value is not None and angle is not None:
            hours.append((moment, value, angle % 360))
    return hours


def _wind_at(
    hours: Sequence[tuple[datetime, float, float]], moment: datetime
) -> tuple[float, float] | None:
    """The speed and direction of the hour whose box holds ``moment``."""
    for hour, speed, direction in hours:
        if hour - _HALF_HOUR <= moment < hour + _HALF_HOUR:
            return speed, direction
    return None


def _mean_direction(
    hours: Sequence[tuple[datetime, float, float]], start: datetime, end: datetime
) -> float | None:
    """The overlap-weighted vector mean of the wind, as a direction; None when calm."""
    east = north = weight = 0.0
    for hour, speed, direction in hours:
        overlap = _overlap_s(hour - _HALF_HOUR, hour + _HALF_HOUR, start, end)
        east += overlap * speed * math.sin(math.radians(direction))
        north += overlap * speed * math.cos(math.radians(direction))
        weight += overlap
    if weight <= 0 or math.hypot(east, north) / weight < CALM_MPS:
        return None
    return round(math.degrees(math.atan2(east, north)) % 360, 1) % 360


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}

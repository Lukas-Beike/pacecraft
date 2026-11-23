"""Open offers of a new LT1 or LT2 for the athlete's one-tap adoption (#4135).

After each session the profile read compares every known candidate with the
value in effect: the local DFA-a1 LT1 heart rate, the DFA-a1 LT2 heart rate and
pace, critical speed (run pace) and critical power (ride power), and Garmin's
LT2 values (threshold heart rate, threshold pace, FTP). A candidate is offered
when it is known, of good quality where its source rates one, at least the
notice step (F10: 2 bpm, 3 s/km, 5 W) away from the value in effect, and not
dismissed for exactly this value. Offers are computed on read; nothing here
writes. Adoption goes through the existing adopt paths with the offer's
``adopt`` body; only a dismissal is stored (``DISMISSED_KEY``).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from datetime import date
from typing import Any, Final

from backend.activities.zone_settings import configured_threshold
from backend.canonical.domain_records import ZoneConfiguration
from backend.canonical.vocabulary import MetricKind, ProviderId, Sport
from backend.performance.canonical_current import read_export_thresholds
from backend.performance.current_thresholds import resolve_current_thresholds
from backend.performance.first_threshold_estimate import (
    ESTIMATE_DAYS as LT1_ESTIMATE_DAYS,
)
from backend.performance.garmin_canonical import read_garmin_thresholds

# The dismissed offers' keys, a JSON list, newest last.
DISMISSED_KEY: Final = "threshold_offer_dismissals"
MAX_DISMISSED: Final = 200
GARMIN_SOURCE: Final = "garmin"
# The notice step per value (athlete decision F10): 2 bpm, 3 s/km, 5 W.
MIN_CHANGE: Final = {"heart_rate": 2, "pace": 3, "power": 5}
UNITS: Final = {"heart_rate": "bpm", "pace": "s/km", "power": "W"}
# The LT2 values per sport and their threshold: (sport, value) -> (kind, sport).
LT2_KEYS: Final = {
    ("run", "heart_rate"): (MetricKind.THRESHOLD_HR, Sport.RUN),
    ("run", "pace"): (MetricKind.THRESHOLD_PACE, Sport.RUN),
    ("ride", "power"): (MetricKind.FTP, Sport.RIDE),
    ("ride", "heart_rate"): (MetricKind.THRESHOLD_HR, Sport.RIDE),
}
# The local LT2 estimates: (sport, source, value, estimate field).
_LOCAL_LT2: Final = (
    ("run", "dfa_alpha1", "heart_rate", "heart_rate_bpm"),
    ("run", "dfa_alpha1", "pace", "pace_seconds_per_km"),
    ("run", "critical_speed", "pace", "pace_seconds_per_km"),
    ("ride", "critical_power", "power", "power_w"),
)


def _shown(kind: MetricKind, value: float) -> int:
    """A threshold as shown: whole bpm or W, pace in whole seconds per km."""
    return round(1000 / value) if kind is MetricKind.THRESHOLD_PACE else round(value)


def threshold_values(
    reader: Any, db: Any, today: date, configurations: Iterable[ZoneConfiguration]
) -> dict[str, Any]:
    """The LT2 values in effect today and Garmin's own LT2 values, as shown.

    ``in_effect``: per sport and value the threshold every consumer applies
    (local, then the Garmin watch zone set, then the source-policy values, as
    ``read_export_thresholds`` resolves them from the stored ``configurations``).
    ``garmin``: per sport and value Garmin's value (its watch zone set's heart
    rate first, else its current observation) with the day it dates from.
    """
    configurations = tuple(configurations)
    winners = read_export_thresholds(
        reader, db, as_of=today, configurations=configurations
    )
    watch = [
        item for item in configurations if item.provenance.provider is ProviderId.GARMIN
    ]
    observed = resolve_current_thresholds(
        read_garmin_thresholds(reader, db), as_of=today
    )
    in_effect: dict[str, dict[str, int]] = {}
    garmin: dict[str, dict[str, dict[str, Any]]] = {}
    for (sport, value), (kind, canonical) in LT2_KEYS.items():
        if (winner := winners.get((kind, canonical))) is not None:
            in_effect.setdefault(sport, {})[value] = _shown(kind, winner.value.value)
        found = configured_threshold(watch, kind, canonical, today)
        result = observed.get((kind, canonical))
        if found is not None:
            number, day = found[0], found[1].valid_from
        elif result is not None and result.winner.source is ProviderId.GARMIN:
            number, day = result.winner.value, result.winner.observed_on
        else:
            continue
        garmin.setdefault(sport, {})[value] = {
            "value": _shown(kind, number),
            "date": None if day is None else day.isoformat(),
        }
    return {"in_effect": in_effect, "garmin": garmin}


def open_offers(
    lt2_estimates: Mapping[str, Any],
    lt1_estimate: Mapping[str, Any] | None,
    lt1_in_effect: float | None,
    values: Mapping[str, Any],
    dismissed: Iterable[str],
) -> list[dict[str, Any]]:
    """The offers not dismissed, LT1 first, then LT2 per sport and source."""
    in_effect = values.get("in_effect") or {}
    offers: list[dict[str, Any] | None] = []
    if lt1_estimate and lt1_estimate.get("quality") == "good":
        offers.append(
            _offer(
                ("lt1", "run", "heart_rate", "dfa_alpha1"),
                lt1_estimate.get("bpm"),
                lt1_in_effect,
                {**lt1_estimate, "window_days": LT1_ESTIMATE_DAYS},
                {"percent_lthr": lt1_estimate.get("percent_lthr")},
            )
        )
    offers += [
        _offer(
            ("lt2", sport, value, source),
            number,
            (in_effect.get(sport) or {}).get(value),
            basis,
        )
        for sport, value, source, number, basis in _lt2_candidates(
            lt2_estimates, values.get("garmin") or {}
        )
    ]
    hidden = set(dismissed)
    return [offer for offer in offers if offer and offer["key"] not in hidden]


def _lt2_candidates(
    lt2_estimates: Mapping[str, Any], garmin: Mapping[str, Any]
) -> Iterator[tuple[str, str, str, Any, Mapping[str, Any]]]:
    """(sport, value, source, number, basis) of each good local estimate and Garmin's."""
    for sport, source, value, field in _LOCAL_LT2:
        estimate = (lt2_estimates.get(sport) or {}).get(source) or {}
        if estimate.get("status") == "ok" and estimate.get("quality") == "good":
            yield sport, value, source, estimate.get(field), estimate
    for sport, value in LT2_KEYS:
        if known := (garmin.get(sport) or {}).get(value):
            yield sport, value, GARMIN_SOURCE, known.get("value"), {"to": known["date"]}


def _offer(
    name: tuple[str, str, str, str],
    value: Any,
    current: float | None,
    basis: Mapping[str, Any],
    adopt: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """One offer, or None when unknown or within the notice step of the value in effect."""
    threshold, sport, metric, source = name
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if current is not None and abs(value - current) < MIN_CHANGE[metric]:
        return None
    return {
        "key": f"{threshold}:{sport}:{metric}:{source}:{value:g}",
        "threshold": threshold,
        "sport": sport,
        "metric": metric,
        "source": source,
        "value": value,
        "unit": UNITS[metric],
        "current": current,
        "sessions": basis.get("sessions"),
        "window_days": basis.get("window_days"),
        "quality": basis.get("quality"),
        "spread_pct": basis.get("spread_pct"),
        "newest": basis.get("to"),
        "adopt": adopt
        or {"sport": sport, "source": source, "value": metric, "shown": value},
    }

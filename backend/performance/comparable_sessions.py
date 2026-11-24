"""One session's execution against comparable earlier sessions of the athlete.

Comparable: earlier runs or rides of the last ``COMPARE_DAYS`` days with the
same sport, environment (indoor or outdoor) and structure, and a similar shape
and effort: a continuous main part within ``DURATION_SHARE`` of this one and an
average heart rate within ``HEART_RATE_BAND`` bpm, or interval sessions within
``REP_COUNT_SPREAD`` reps whose median rep lasts within ``DURATION_SHARE`` of
this one's. Sessions with doubtful heart rate or speed stay out. Fewer than
``MIN_PEERS`` of them is ``not_covered``. Values come from each session's own
execution, the median of the peers is the reference; nothing is scored, and
the activity weather's temperatures (never the device sensor, #3036) are
named so heat is not read as fitness. Pure: no I/O.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any, Final

from backend.canonical.activity_summary import summary_is_trail_run
from backend.performance.pace_at_hr import band_paces
from backend.performance.session_execution import rounded_metric

METHOD: Final = "comparable-sessions-v1"
COMPARE_DAYS: Final = 84
MIN_PEERS: Final = 3
MAX_LISTED: Final = 6
DURATION_SHARE: Final = 0.35
REP_COUNT_SPREAD: Final = 2
# Continuous sessions compare only at a similar effort, so an easy run is never
# a tempo run's peer; within the band a faster pace still shows as fitter.
HEART_RATE_BAND: Final = 8
SCOPE: Final = (
    f"This session against earlier comparable sessions of up to {COMPARE_DAYS} "
    "days: same sport, indoor or outdoor and structure; a continuous main part "
    "within 35 % of this one's duration and 8 bpm of its average heart rate, or "
    "intervals within 2 reps and 35 % of the median rep duration; sessions with "
    "doubtful heart rate or speed stay out. difference is this session minus the "
    "peers' median in the metric's unit (pace s/km: negative is faster). Terrain "
    "is not matched. weather_temp_c is the activity weather's air temperature, "
    "listed so heat is not read as a fitness change; null when no weather is "
    "stored, never the device sensor. Descriptive; at least 2 peers, medians "
    "and progress at least 3. session_type: peers share it and the plan target "
    "(plan focus, else detected). duration_change_min: this session minus the "
    "3 newest peers' median. progress.verdict: the type's marker against the "
    "peers (besser, gleich, schlechter)."
)
# Metrics the session report judges against the peers' upper quartile (#3084).
PERSONAL_LIMIT_METRICS: Final = frozenset({"decoupling_pct", "gap_cv_pct"})
_METRICS: Final = {
    "continuous": (
        "output",
        "heart_rate_avg",
        "heart_rate_change_bpm",
        "cadence",
        "decoupling_pct",
        "gap_cv_pct",
    ),
    "interval": (
        "rep_output_mean",
        "heart_rate_end_mean",
        "recovery_drop_mean",
        "first_to_last_change_pct",
        "cadence_mean",
    ),
}


# Session types (#4136): a planned unit's focus names its session's type; a
# race or test has none and is never compared.
_FOCUS_TYPES: Final = {
    "recovery": "easy",
    "easy": "easy",
    "endurance": "easy",
    "long": "long",
    "tempo": "threshold",
    "sweet_spot": "threshold",
    "threshold": "threshold",
    "vo2max": "interval",
    "anaerobic": "interval",
}
# Without a plan: a steady main part at most at the first threshold (LT1) is
# easy, from this long on a long session; above LT1 threshold work.
LONG_MAIN_S: Final = 3600
# Types whose progress marker is the pace in the shared heart-rate band.
STEADY_TYPES: Final = frozenset({"easy", "long"})
# At least this many peers are listed; a progress verdict needs MIN_PEERS.
MIN_LISTED: Final = 2
# Progress (#4136): the type's marker changed by at least this share of the
# peers' median; a heart rate at most this much higher keeps "besser"; a
# decoupling change of at least this many points decides a level pace.
PROGRESS_PCT: Final = 1.0
PROGRESS_HEART_RATE_BPM: Final = 2
PROGRESS_DECOUPLING_PTS: Final = 2.0
# At the same power (within SAME_OUTPUT_SHARE) a heart rate this many bpm lower
# is better, as much higher worse; a smaller change is day-to-day noise
# (athlete, 2026-11-24: +3 bpm in ERG is "gleich").
SAME_POWER_HEART_RATE_BPM: Final = 4
# The duration change is measured against this many of the newest peers.
RECENT_PEERS: Final = 3
_UNTYPED: Final = {
    "status": "not_covered",
    "reason": "Einheitstyp unbekannt: kein Plan und keine Erkennung, keine Entwicklung ablesbar.",
}


def session_types(
    records: Sequence[Mapping[str, Any]],
    plans: Mapping[str, Mapping[str, Any]],
    easy_bpm: float | None,
) -> dict[str, dict[str, Any]]:
    """Each record's session type, from its planned unit's focus, else detected.

    ``plans`` maps a session key to its paired unit's ``focus`` and ``target``
    (a hashable plan target, None without one). Detection reads the execution:
    intervals are interval sessions; a continuous main part at most at
    ``easy_bpm`` is easy, from ``LONG_MAIN_S`` on long; above it threshold.
    ``easy_bpm`` is the analysed session's, so a peer from before a threshold
    change is typed by today's value. A session of no known type is left out.
    """
    found: dict[str, dict[str, Any]] = {}
    for row in records:
        key = str(row.get("activity_id") or "")
        plan = plans.get(key) or {}
        if plan.get("focus"):
            kind, origin = _FOCUS_TYPES.get(str(plan["focus"])), "plan"
        else:
            kind, origin = _detected(row.get("execution_summary"), easy_bpm), "detected"
        if kind is not None:
            found[key] = {"type": kind, "origin": origin, "target": plan.get("target")}
    return found


def _detected(summary: Any, easy_bpm: float | None) -> str | None:
    if not isinstance(summary, Mapping):
        return None
    if summary.get("structure") == "interval":
        return "interval"
    heart_rate, main = summary.get("heart_rate_avg"), summary.get("main_part_s")
    if easy_bpm is None or not isinstance(heart_rate, (int, float)):
        return None
    if heart_rate > easy_bpm:
        return "threshold"
    long = isinstance(main, (int, float)) and main >= LONG_MAIN_S
    return "long" if long else "easy"


def same_type_keys(
    types: Mapping[str, Mapping[str, Any]], activity_key: str
) -> frozenset[str] | None:
    """The sessions of this one's type and plan target; None when its type is unknown."""
    own = types.get(activity_key)
    if own is None:
        return None
    return frozenset(
        key
        for key, other in types.items()
        if other["type"] == own["type"]
        and (
            own.get("target") is None
            or other.get("target") is None
            or other["target"] == own["target"]
        )
    )


def comparable_sessions(
    records: Sequence[Mapping[str, Any]],
    activity_key: str,
    *,
    covered_from: date | None = None,
    types: Mapping[str, Mapping[str, Any]] | None = None,
    erg: bool = False,
) -> dict[str, Any]:
    """Compare one session's execution with its comparable earlier sessions.

    ``erg`` marks a ride whose power the trainer held (``erg_like``): its
    progress at the same power is the heart rate it cost (#4136).

    ``covered_from`` is the first day the records cover; an earlier peer window
    is cut there and named as truncated. ``types`` (``session_types``) keeps
    the peers to this session's type and plan target (#4136); a session of
    unknown type keeps the shape rules alone and says so.
    """
    base = {"method": METHOD, "scope": SCOPE}
    own = next((row for row in records if row.get("activity_id") == activity_key), None)
    # A session without a curve has no full-resolution streams.
    if own is None or own.get("fingerprint") is None:
        return _not_covered(
            base, "Keine Messreihe in voller Auflösung für diese Einheit."
        )
    mine = own.get("execution_summary")
    if not isinstance(mine, Mapping):
        return _not_covered(
            base,
            "Keine vergleichbare Ausführung: Hauptteil zu kurz oder keine Intervalle mit Messwerten.",
        )
    if mine.get("data_quality_ok") is False:
        return _not_covered(
            base, "Puls oder Tempo dieser Einheit sind zweifelhaft (data_quality)."
        )
    try:
        day = date.fromisoformat(str(own.get("date") or ""))
    except ValueError:
        return _not_covered(base, "Datum der Einheit unbekannt.")
    first, window = _window(day, covered_from)
    own_type = None if types is None else types.get(activity_key)
    allowed = None if types is None else same_type_keys(types, activity_key)
    window["session_type"] = None if own_type is None else _type_view(own_type)
    peers = _peers(records, own, mine, first, day, allowed)
    if len(peers) < MIN_LISTED:
        return _not_covered(
            base,
            f"{len(peers)} vergleichbare Einheiten dieses Typs in {window['window_days']} Tagen, mindestens {MIN_LISTED} nötig.",
            count=len(peers),
            # Its own numbers, so a value judged only against peers is still shown.
            this={**mine, "duration_s": own.get("duration_s")},
            **window,
        )
    metrics = _METRICS.get(str(mine.get("structure")), ())
    median, difference, median_peers, quartile = _medians(mine, peers, metrics)
    peers.sort(key=lambda row: str(row.get("date")), reverse=True)
    this, rows = _cohort_rows(own, peers, metrics)
    if erg:
        this["erg_like"] = True
    return {
        **base,
        "status": "ok",
        "count": len(peers),
        **window,
        "this": this,
        "median_of_peers": median,
        "median_peers": median_peers,
        "upper_quartile_of_peers": quartile,
        "at_same_output": _at_same_output(mine, peers),
        "difference": difference,
        **_trend(this, rows, own_type, typed=types is None or own_type is not None),
        "peers": rows[:MAX_LISTED],
    }


def _window(day: date, covered_from: date | None) -> tuple[date, dict[str, Any]]:
    """The first day of the peer window and its length, cut at ``covered_from``."""
    first = day - timedelta(days=COMPARE_DAYS)
    truncated = covered_from is not None and covered_from > first
    if covered_from is not None and truncated:
        first = covered_from
    return first, {"window_days": (day - first).days, "window_truncated": truncated}


def _trend(
    this: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    own_type: Mapping[str, Any] | None,
    *,
    typed: bool,
) -> dict[str, Any]:
    """The duration change and progress; a session of unknown type has mixed
    peers, so none against them (#4136)."""
    if not typed:
        return {"duration_change_min": None, "progress": dict(_UNTYPED)}
    return {
        "duration_change_min": _duration_change(this, rows),
        "progress": progress(
            this, rows, None if own_type is None else own_type["type"]
        ),
    }


def _type_view(own_type: Mapping[str, Any]) -> dict[str, Any]:
    return {"type": own_type["type"], "origin": own_type["origin"]}


def _cohort_rows(
    own: Mapping[str, Any],
    peers: Sequence[Mapping[str, Any]],
    metrics: Sequence[str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """This session's row and each peer's, newest first, with its duration and,
    for runs, the pace in the heart-rate band they share (#4136)."""
    shared = band_paces(
        own.get("pace_at_hr_profile") or {},
        [row.get("pace_at_hr_profile") for row in peers],
    )
    band, own_pace, paces = shared or (None, None, [None] * len(peers))
    this = {
        **own["execution_summary"],
        "date": own.get("date"),
        "duration_s": own.get("duration_s"),
        "weather_temp_c": own.get("weather_temp_c"),
        "heart_rate_band_bpm": band,
        "pace_at_band_s_per_km": own_pace,
    }
    rows = [
        {
            "date": row.get("date"),
            "name": row.get("name"),
            "duration_s": row.get("duration_s"),
            "weather_temp_c": row.get("weather_temp_c"),
            "source": row["execution_summary"].get("source"),
            "pace_at_band_s_per_km": pace,
            **{
                key: row["execution_summary"][key]
                for key in metrics
                if key in row["execution_summary"]
            },
        }
        for row, pace in zip(peers, paces, strict=True)
    ]
    return this, rows


def _duration_change(
    this: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]
) -> int | None:
    """Minutes this session lasted longer (negative: shorter) than the median of
    the newest ``RECENT_PEERS`` peers."""
    durations = [
        float(row["duration_s"])
        for row in rows[:RECENT_PEERS]
        if isinstance(row.get("duration_s"), (int, float))
    ]
    own = this.get("duration_s")
    if not durations or not isinstance(own, (int, float)):
        return None
    return round((own - statistics.median(durations)) / 60)


def progress(
    this: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    session_type: str | None = None,
) -> dict[str, Any]:
    """besser, gleich or schlechter on the session type's marker (#4136).

    Steady runs: the pace in the shared heart-rate band against the peers'
    median, a level pace decided by the decoupling. Other sessions: the main
    output (rep output for intervals) against the median with the heart rate it
    cost; faster at a clearly higher heart rate is "gleich". Needs
    ``MIN_PEERS`` peers with the marker.
    """
    paces = _values(rows, "pace_at_band_s_per_km")
    own_pace = this.get("pace_at_band_s_per_km")
    # Only a steady easy or long run is judged on its pace at heart rate; a
    # threshold or interval run's band may never touch its work.
    steady = this.get("structure") == "continuous" and session_type in STEADY_TYPES
    if steady and isinstance(own_pace, (int, float)) and len(paces) >= MIN_PEERS:
        median = statistics.median(paces)
        change = (own_pace - median) / median * 100
        coupling = _change(this, rows, "decoupling_pct")
        verdict = _verdict(-change, 0.0)
        if verdict == "gleich" and coupling is not None:
            verdict = _verdict(-coupling, 0.0, PROGRESS_DECOUPLING_PTS)
        return {
            "status": "ok",
            "marker": "pace_at_heart_rate",
            "verdict": verdict,
            "peers": len(paces),
            "pace_s_per_km": round(own_pace, 1),
            "median_pace_s_per_km": round(median, 1),
            "change_pct": round(change, 1),
            "decoupling_change_pts": coupling,
        }
    interval = this.get("structure") == "interval"
    output_key = "rep_output_mean" if interval else "output"
    heart_key = "heart_rate_end_mean" if interval else "heart_rate_avg"
    outputs = _values(rows, output_key)
    own_output = this.get(output_key)
    if not isinstance(own_output, (int, float)) or len(outputs) < MIN_PEERS:
        return {
            "status": "not_covered",
            "reason": f"{len(outputs)} vergleichbare Einheiten mit Messwerten, für eine Entwicklung mindestens {MIN_PEERS} nötig.",
        }
    median = statistics.median(outputs)
    change = (own_output - median) / median * 100
    better = -change if this.get("output_unit") == "s/km" else change
    heart = _change(this, rows, heart_key)
    if heart is None:
        # Output alone says nothing without the heart rate it cost.
        return {
            "status": "not_covered",
            "reason": "Puls der vergleichbaren Einheiten fehlt, keine Entwicklung ablesbar.",
        }
    # An ERG ride at the same power: the heart rate it cost decides (athlete,
    # 2026-11-24, #4136); any other ride keeps power against heart rate.
    same_power = (
        this.get("erg_like") is True
        and this.get("output_unit") == "W"
        and abs(change) <= SAME_OUTPUT_SHARE * 100
    )
    return {
        "status": "ok",
        "marker": "heart_rate_at_same_power" if same_power else output_key,
        "verdict": _same_power_verdict(heart)
        if same_power
        else _verdict(better, heart),
        "peers": len(outputs),
        "output_unit": this.get("output_unit"),
        "output": own_output,
        "median_output": rounded_metric(median),
        "change_pct": round(change, 1),
        "heart_rate_change_bpm": heart,
    }


def _same_power_verdict(heart: float) -> str:
    """besser when the same power cost at least ``SAME_POWER_HEART_RATE_BPM``
    less heart rate, schlechter when as much more; else gleich."""
    if heart <= -SAME_POWER_HEART_RATE_BPM:
        return "besser"
    return "schlechter" if heart >= SAME_POWER_HEART_RATE_BPM else "gleich"


def _verdict(better: float, heart: float, limit: float = PROGRESS_PCT) -> str:
    """besser when the marker improved by ``limit`` at a heart rate at most
    ``PROGRESS_HEART_RATE_BPM`` higher; schlechter the other way round."""
    if better >= limit and heart <= PROGRESS_HEART_RATE_BPM:
        return "besser"
    if better <= -limit and heart >= -PROGRESS_HEART_RATE_BPM:
        return "schlechter"
    return "gleich"


def _values(rows: Sequence[Mapping[str, Any]], key: str) -> list[float]:
    return [float(row[key]) for row in rows if isinstance(row.get(key), (int, float))]


def _change(
    this: Mapping[str, Any], rows: Sequence[Mapping[str, Any]], key: str
) -> float | None:
    """This session's value minus the peers' median, with at least MIN_PEERS peers."""
    values = _values(rows, key)
    own = this.get(key)
    if not isinstance(own, (int, float)) or len(values) < MIN_PEERS:
        return None
    return rounded_metric(own - statistics.median(values))


def _peers(
    records: Sequence[Mapping[str, Any]],
    own: Mapping[str, Any],
    mine: Mapping[str, Any],
    first: date,
    day: date,
    allowed: frozenset[str] | None = None,
) -> list[Mapping[str, Any]]:
    """Earlier sessions of the window with this one's kind, sound data and shape;
    with ``allowed`` only those sessions, this one's type (#4136)."""
    return [
        row
        for row in records
        if row is not own
        and (allowed is None or row.get("activity_id") in allowed)
        and first.isoformat() <= str(row.get("date") or "") < day.isoformat()
        and row.get("sport") == own.get("sport")
        and row.get("environment") == own.get("environment")
        # Trail runs compare only with trail runs, road runs only with road runs.
        and summary_is_trail_run(row) == summary_is_trail_run(own)
        and _sound_peer(mine, row.get("execution_summary"))
    ]


def _sound_peer(mine: Mapping[str, Any], other: Any) -> bool:
    return (
        isinstance(other, Mapping)
        and other.get("data_quality_ok") is not False
        and _similar(mine, other)
    )


def _medians(
    mine: Mapping[str, Any],
    peers: Sequence[Mapping[str, Any]],
    metrics: Sequence[str],
) -> tuple[dict[str, float], dict[str, float], dict[str, int], dict[str, float]]:
    """The peers' median of each metric, this session's difference, the peers it
    rests on and their upper quartile (inclusive method), for personal limits."""
    median: dict[str, float] = {}
    difference: dict[str, float] = {}
    counts: dict[str, int] = {}
    quartile: dict[str, float] = {}
    for metric in metrics:
        values = [
            float(row["execution_summary"][metric])
            for row in peers
            if isinstance(row["execution_summary"].get(metric), (int, float))
        ]
        if len(values) < MIN_PEERS:
            continue
        median[metric] = rounded_metric(statistics.median(values))
        counts[metric] = len(values)
        if metric in PERSONAL_LIMIT_METRICS:
            quartile[metric] = rounded_metric(
                statistics.quantiles(values, n=4, method="inclusive")[2]
            )
        if isinstance(mine.get(metric), (int, float)):
            difference[metric] = rounded_metric(float(mine[metric]) - median[metric])
    return median, difference, counts, quartile


# Interval peers within this share of the session's rep power (#3084, ERG).
SAME_OUTPUT_SHARE: Final = 0.05


def _at_same_output(
    mine: Mapping[str, Any], peers: Sequence[Mapping[str, Any]]
) -> dict[str, Any] | None:
    """Peers whose mean rep power is within ``SAME_OUTPUT_SHARE`` of this one's,
    with their median closing heart rate: an efficiency trend at equal power."""
    output = mine.get("rep_output_mean")
    if (
        mine.get("output_unit") != "W"
        or not isinstance(output, (int, float))
        or not output
    ):
        return None
    rates = [
        float(row["execution_summary"]["heart_rate_end_mean"])
        for row in peers
        if isinstance(
            power := row["execution_summary"].get("rep_output_mean"), (int, float)
        )
        and abs(power / output - 1) <= SAME_OUTPUT_SHARE
        and isinstance(
            row["execution_summary"].get("heart_rate_end_mean"), (int, float)
        )
    ]
    return {
        "count": len(rates),
        "heart_rate_end_mean": rounded_metric(statistics.median(rates))
        if rates
        else None,
    }


def _similar(mine: Mapping[str, Any], other: Mapping[str, Any]) -> bool:
    if mine.get("structure") != other.get("structure"):
        return False
    if mine.get("output_unit") != other.get("output_unit"):
        return False
    if mine.get("structure") == "continuous":
        return _close(mine.get("main_part_s"), other.get("main_part_s")) and _within(
            mine.get("heart_rate_avg"), other.get("heart_rate_avg"), HEART_RATE_BAND
        )
    count, peer_count = mine.get("rep_count"), other.get("rep_count")
    if not isinstance(count, int) or not isinstance(peer_count, int):
        return False
    return abs(count - peer_count) <= REP_COUNT_SPREAD and _close(
        mine.get("median_rep_s"), other.get("median_rep_s")
    )


def _close(value: Any, other: Any) -> bool:
    if not isinstance(value, (int, float)) or not isinstance(other, (int, float)):
        return False
    if value <= 0:
        return False
    return abs(other - value) <= DURATION_SHARE * value


def _within(value: Any, other: Any, band: float) -> bool:
    if not isinstance(value, (int, float)) or not isinstance(other, (int, float)):
        return False
    return abs(other - value) <= band


def _not_covered(base: Mapping[str, Any], reason: str, **extra: Any) -> dict[str, Any]:
    return {**base, "status": "not_covered", "reason": reason, **extra}


EFFORT_METHOD: Final = "equal-effort-trend-v1"
EFFORT_SCOPE: Final = (
    "Steady sessions of the earlier and the later half of the period: median "
    "output per heart beat (rides with power W/bpm, runs m/s per bpm x 100) per "
    "sport and indoor or outdoor, with the median heart rate and activity "
    "weather temperature of each half. A higher later value means more output "
    "at the same heart rate. "
    f"Needs at least {MIN_PEERS} steady sessions per half; halves whose median "
    f"heart rate differs by more than {HEART_RATE_BAND} bpm are named, as effort "
    "then differs; sources_differ names a change of stream providers between "
    "the halves. Rides without power and terrain are not compared."
)
# Each sport's output unit; a ride without power has no comparable output.
_EFFORT_UNITS: Final = {"run": "s/km", "ride": "W"}


def equal_effort_trend(
    records: Sequence[Mapping[str, Any]],
    start: date,
    middle: date,
    end: date,
    *,
    sports: Sequence[str] = ("run", "ride"),
) -> dict[str, Any]:
    """Earlier-versus-later output per heart beat of steady runs and rides."""
    halves = {"earlier": (start, middle - timedelta(days=1)), "later": (middle, end)}
    result: dict[str, Any] = {"method": EFFORT_METHOD, "scope": EFFORT_SCOPE}
    for sport in sports:
        result[sport] = {
            environment: _effort_halves(records, sport, environment, halves)
            for environment in ("outdoor", "indoor")
        }
    return result


def _effort_halves(
    records: Sequence[Mapping[str, Any]],
    sport: str,
    environment: str,
    halves: Mapping[str, tuple[date, date]],
) -> dict[str, Any]:
    parts: dict[str, dict[str, Any]] = {}
    for name, (first, last) in halves.items():
        values = _steady_sessions(records, sport, environment, first, last)
        if len(values) < MIN_PEERS:
            return {
                "status": "not_covered",
                "reason": f"{len(values)} gleichmäßige Einheiten in der Hälfte {name}, mindestens {MIN_PEERS} nötig.",
            }
        parts[name] = _effort_half(values)
    earlier, later = parts["earlier"], parts["later"]
    return {
        "status": "ok",
        **parts,
        "change_pct": round(
            (later["output_per_beat"] - earlier["output_per_beat"])
            / earlier["output_per_beat"]
            * 100,
            1,
        ),
        "effort_differs": abs(later["heart_rate_median"] - earlier["heart_rate_median"])
        > HEART_RATE_BAND,
        "sources_differ": earlier["sources"] != later["sources"],
    }


def _same_kind(row: Mapping[str, Any], sport: str, environment: str) -> bool:
    """Same sport and place; trail runs are left out, their pace is not comparable."""
    return (
        row.get("sport") == sport
        and row.get("environment") == environment
        and not summary_is_trail_run(row)
    )


def _steady_sessions(
    records: Sequence[Mapping[str, Any]],
    sport: str,
    environment: str,
    first: date,
    last: date,
) -> list[tuple[float, Mapping[str, Any]]]:
    """Output per beat of the steady sessions of one half, sport and place."""
    values = []
    for row in records:
        summary = row.get("execution_summary")
        if (
            not _same_kind(row, sport, environment)
            or not first.isoformat() <= str(row.get("date") or "") <= last.isoformat()
            or not isinstance(summary, Mapping)
            or summary.get("output_unit") != _EFFORT_UNITS[sport]
        ):
            continue
        efficiency = _efficiency(summary)
        if efficiency is not None:
            values.append((efficiency, row))
    return values


def _effort_half(values: Sequence[tuple[float, Mapping[str, Any]]]) -> dict[str, Any]:
    temperatures = [
        float(row["weather_temp_c"])
        for _, row in values
        if isinstance(row.get("weather_temp_c"), (int, float))
    ]
    return {
        "sessions": len(values),
        # The stream providers, so a sensor or provider change is not read as fitness.
        "sources": sorted(
            {
                str(row["execution_summary"].get("source"))
                for _, row in values
                if row["execution_summary"].get("source")
            }
        ),
        "output_per_beat": round(statistics.median(v for v, _ in values), 3),
        "heart_rate_median": rounded_metric(
            statistics.median(
                float(row["execution_summary"]["heart_rate_avg"]) for _, row in values
            )
        ),
        "temperature_median_c": rounded_metric(statistics.median(temperatures))
        if temperatures
        else None,
    }


def _efficiency(summary: Any) -> float | None:
    """Output per heart beat of a steady session with sound data."""
    if not isinstance(summary, Mapping) or summary.get("structure") != "continuous":
        return None
    if summary.get("data_quality_ok") is False:
        return None
    output, heart = summary.get("output"), summary.get("heart_rate_avg")
    if not isinstance(output, (int, float)) or not isinstance(heart, (int, float)):
        return None
    if output <= 0 or heart <= 0:
        return None
    unit = summary.get("output_unit")
    if unit == "W":
        return float(output) / float(heart)
    if unit == "s/km":
        return 1000.0 / float(output) / float(heart) * 100
    return None

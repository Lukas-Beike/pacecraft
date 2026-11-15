"""The deterministic report of one analysed session; the Coach only explains it (#3084).

Every check carries its value, the rule and threshold it applies, the basis of
that threshold (``personal`` when the athlete has enough own comparable values,
``plan`` against the planned unit, else ``fixed``), a verdict (``ok``,
``notable`` or ``unknown`` with a reason) and a deterministic severity. The
checks read the session's existing derivations (execution, planned targets,
session metrics, zones, comparable sessions); nothing is computed twice.

Thresholds are the athlete's decisions on #3084 (2026-11-15). A check whose
inputs fail the data-quality rules is ``unknown`` instead of ``notable``, so a
sensor error never becomes a training finding, and structure checks need a
reliable plan-to-lap mapping. Pure: no I/O, inputs unchanged.
"""

from __future__ import annotations

import statistics
from bisect import bisect_left
from collections.abc import Mapping, Sequence
from itertools import pairwise
from typing import Any, Final

from backend.activities.session_purpose import summary_is_race
from backend.athlete.measurements import finite_float as _number
from backend.canonical.activity_summary import summary_is_indoor
from backend.performance import session_conditions
from backend.performance.comparable_sessions import DURATION_SHARE
from backend.performance.planned_targets import (
    CAP_ABOVE_SHARE_PCT,
    FAIR_SHARE_PCT,
    adherence_band,
)
from backend.performance.session_execution import comparison_metrics

METHOD: Final = "session-report-v1"
SCOPE: Final = (
    "Checks computed by code from this session's stored data, each with its "
    "value, rule, threshold, basis (personal, plan or fixed), verdict and "
    "severity. notable lists every notable check by severity; lead is the "
    "first four of them. unknown checks name why. Thresholds: athlete "
    "decisions on #3084 (2026-11-15)."
)
# Plan against actual: duration, load and recoveries (TrainingPeaks green, ±20 %).
PLAN_TOLERANCE_PCT: Final = 20.0
# Aerobic decoupling, the fixed fallback (Friel / TrainingPeaks: < 5 % good).
DECOUPLING_PCT: Final = 5.0
# Interval fade and start change against the median of the reps, and within a rep.
FADE_PCT: Final = 3.0
# Cardiac drift across reps at equal output, the decoupling rule applied to reps.
CARDIAC_DRIFT_PCT: Final = 5.0
# Easy, recovery and long sessions: share of moving time above zone 2 (Seiler; #2830).
EASY_ABOVE_SHARE_PCT: Final = 10.0
# Ride steadiness: variability index (Coggan / Allen, TrainingPeaks: <= 1.05 steady).
RIDE_VARIABILITY_INDEX: Final = 1.05
# HR drop 60 s into recoveries: notable this far below the athlete's own median.
HEART_RATE_DROP_BELOW_BPM: Final = 5.0
# A personal basis needs this many own comparable values.
MIN_PERSONAL: Final = 5
# Race: second half this much slower than the first (positive split).
RACE_SPLIT_PCT: Final = 2.0
# Race against its race plan (#2803): a section, the start or the km after the
# fade point this far off the plan there (athlete, #3084).
RACE_PLAN_PCT: Final = 3.0
# The start is this share of the distance, at least RACE_START_MIN_KM.
RACE_START_SHARE: Final = 0.10
# The shortest start a race is judged by, in km.
RACE_START_MIN_KM: Final = 1.0
# Finish slower than the competition's target time by this much.
RACE_FINISH_PCT: Final = 2.0
# The recorded distance may differ this much from the plan's (GPS, course).
RACE_DISTANCE_SHARE: Final = 0.10
# A final stretch slower than this share of the race's median speed is a walk
# or roll-out after the line, not the race.
WALK_OFF_SPEED_SHARE: Final = 0.6
# The notable checks the answer leads with.
LEAD_COUNT: Final = 4
EASY_FOCUS: Final = frozenset({"recovery", "easy", "endurance", "long"})
INTERVAL_FOCUS: Final = frozenset(
    {"tempo", "threshold", "vo2max", "anaerobic", "sweet_spot", "test"}
)
# Base severity per check: the plan and its main intent first.
_WEIGHT: Final = {
    "plan_structure": 5,
    "rep_target": 4,
    "rep_fade": 4,
    "easy_time_above": 4,
    "heart_rate_cap": 4,
    "race_split": 4,
    "race_finish": 4,
    "race_sections": 4,
    "race_start": 3,
    "race_fade": 3,
    "plan_duration": 3,
    "plan_load": 3,
    "decoupling": 3,
    "cardiac_drift": 3,
    "within_rep_fade": 3,
    "main_fade": 3,
    "start_change": 2,
    "steadiness": 2,
    "recovery_duration": 2,
    "recovery_heart_rate_drop": 2,
    "rpe_intensity": 2,
    # An estimate: shown with every run, never a finding alone.
    "grade_adjusted_pace": 0,
}


def session_report(
    result: Mapping[str, Any],
    planned: Mapping[str, Any] | None = None,
    training_load: float | None = None,
    race: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The report of a ``get_activity_details`` result.

    ``planned`` is the paired unit's estimate (``duration_s``, ``load`` and
    ``above_easy_s``, its planned seconds above zone 2), None without a paired
    unit; ``training_load`` is the session's local TSS-scale load (M6), the
    scale of the estimate. A provider load is never compared. ``race`` is
    the linked competition's race plan (``race_plan``), None without one.
    """
    analysis = result.get("analysis")
    execution = analysis.get("execution") if isinstance(analysis, Mapping) else None
    execution = execution if isinstance(execution, Mapping) else {}
    facts = _Facts(result, execution, planned or {})
    checks = [
        *_plan_checks(facts, planned, training_load),
        *(_easy_checks(facts) if facts.kind == "easy" else ()),
        *(_continuous_checks(facts) if facts.kind == "continuous" else ()),
        *(_interval_checks(facts) if facts.kind == "interval" else ()),
        *(_race_checks(facts, race) if facts.kind == "race" else ()),
        *_condition_checks(facts),
    ]
    notable = sorted(
        (check for check in checks if check["verdict"] == "notable"),
        key=lambda check: -check["severity"],
    )
    return {
        "method": METHOD,
        "scope": SCOPE,
        "session_type": facts.kind,
        "checks": checks,
        "notable": [check["id"] for check in notable],
        "lead": [check["id"] for check in notable[:LEAD_COUNT]],
        "unknown": [check["id"] for check in checks if check["verdict"] == "unknown"],
    }


class _Facts:
    """The inputs the checks read, resolved once."""

    def __init__(
        self,
        result: Mapping[str, Any],
        execution: Mapping[str, Any],
        planned: Mapping[str, Any],
    ):
        self.planned = planned
        self.result = result
        session = result.get("session")
        self.session = session if isinstance(session, Mapping) else {}
        focus = result.get("focus")
        self.focus = (
            focus.get("session_focus")
            if isinstance(focus, Mapping) and focus.get("status") == "matched"
            else None
        )
        self.metrics = _mapping(result.get("metrics"))
        self.zones = _mapping(result.get("zones"))
        self.targets = _mapping(result.get("planned_targets"))
        self.comparable = _mapping(result.get("comparable_sessions"))
        self.execution = execution
        quality = _mapping(execution.get("data_quality"))
        self.heart_rate_quality = quality.get("heart_rate")
        self.speed_quality = quality.get("speed")
        self.sport = self.session.get("sport")
        self.kind = self._kind()

    def _kind(self) -> str:
        if self.focus == "race" or summary_is_race(self.session):
            return "race"
        if self.focus in EASY_FOCUS:
            return "easy"
        structure = self.execution.get("structure")
        if structure == "interval" or (
            self.focus in INTERVAL_FOCUS and structure != "continuous"
        ):
            return "interval"
        return "continuous"

    def metric(self, key: str) -> float | None:
        return _number(_mapping(self.metrics.get(key)).get("value"))

    def heart_rate_unknown(self) -> str | None:
        """Why heart-rate checks cannot judge: the data quality failed or was not checked."""
        if self.heart_rate_quality == "ok":
            return None
        if self.heart_rate_quality is None:
            return (
                "Datenqualität Herzfrequenz nicht geprüft (keine Ausführungsanalyse)."
            )
        return f"Datenqualität Herzfrequenz: {self.heart_rate_quality}."

    def output_unknown(self) -> str | None:
        """Why output checks cannot judge a speed-based output whose speed jumps."""
        speed_based = self.execution.get("output_unit") != "W"
        if speed_based and self.speed_quality not in (None, "ok"):
            return f"Datenqualität Tempo: {self.speed_quality}."
        return None


def _plan_checks(
    facts: _Facts, planned: Mapping[str, Any] | None, training_load: float | None
) -> list[dict[str, Any]]:
    if planned is None:
        return []
    checks = []
    # The estimate counts planned rests; an interval session's standing rests
    # are elapsed, not moving time.
    measured = (
        facts.metric("elapsed_duration_s")
        if facts.kind == "interval"
        else facts.metric("moving_duration_s")
    )
    checks.append(
        _against_plan(
            "plan_duration",
            "Dauer gegen Plan",
            measured or facts.metric("duration_s"),
            planned.get("duration_s"),
            "s",
        )
    )
    checks.append(
        _against_plan(
            "plan_load",
            "Belastung gegen Plan",
            _number(training_load),
            planned.get("load"),
            None,
        )
    )
    # Work steps against work laps only judge detected intervals: planned
    # targets cannot tell a continuous plan from skipped intervals.
    structure = _structure(facts) if facts.kind == "interval" else None
    if structure is not None:
        checks.append(structure)
    return checks


def _against_plan(
    key: str, title: str, actual: float | None, planned: Any, unit: str | None
) -> dict[str, Any]:
    plan = _number(planned)
    if actual is None or not plan:
        return _unknown(key, title, "Plan- oder Istwert fehlt.", basis="plan")
    pct = (actual / plan - 1) * 100
    return _judged(
        key,
        title,
        value={
            "actual": round(actual, 1),
            "planned": round(plan, 1),
            "difference_pct": round(pct, 1),
        },
        unit=unit,
        rule="Abweichung vom Plan in Prozent, Betrag",
        threshold=PLAN_TOLERANCE_PCT,
        measure=abs(pct),
        basis="plan",
    )


def _structure(facts: _Facts) -> dict[str, Any] | None:
    """Planned work steps against executed work laps, when a plan has steps."""
    targets = facts.targets
    if not targets:
        return None
    title = "Struktur gegen Plan"
    if targets.get("status") != "ok":
        return _unknown(
            "plan_structure",
            title,
            _mapping_reason(targets),
            basis="plan",
        )
    summary = _mapping(targets.get("summary"))
    planned = summary.get("planned_work_steps")
    executed = summary.get("executed_work_laps")
    if not isinstance(planned, int) or not isinstance(executed, int) or not planned:
        # A continuous plan's steps carry no work-step count to judge.
        return None
    missing = executed - planned
    return _judged(
        "plan_structure",
        title,
        value={
            "planned_work_steps": planned,
            "executed_work_laps": executed,
            "difference": missing,
        },
        unit="count",
        rule="Fehlende oder zusätzliche Arbeitsabschnitte",
        threshold=0,
        measure=abs(missing),
        basis="plan",
        mapping_confidence=round(min(executed, planned) / planned, 2),
    )


def _easy_checks(facts: _Facts) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    if facts.sport == "ride":
        checks.append(_ride_steadiness(facts))
    checks.append(_decoupling(facts))
    checks.append(_easy_time_above(facts))
    cap = _mapping(facts.targets.get("heart_rate_cap"))
    if cap:
        checks.append(_heart_rate_cap(facts, cap))
    return checks


def _ride_steadiness(facts: _Facts) -> dict[str, Any]:
    title = "Gleichmäßigkeit (VI)"
    index = facts.metric("variability_index")
    if index is None:
        return _unknown(
            "steadiness", title, "Kein Variabilitätsindex (Leistung fehlt)."
        )
    return _judged(
        "steadiness",
        title,
        value=round(index, 3),
        unit=None,
        rule="Normalized Power / Durchschnittsleistung",
        threshold=RIDE_VARIABILITY_INDEX,
        measure=index,
        basis="fixed",
    )


def _decoupling(facts: _Facts) -> dict[str, Any]:
    title = "Aerobe Entkopplung"
    if reason := facts.heart_rate_unknown():
        return _unknown("decoupling", title, reason)
    value = facts.metric("decoupling_pct")
    if value is None:
        return _unknown(
            "decoupling", title, "Keine Entkopplung berechnet (zu kurz oder unruhig)."
        )
    return _judged(
        "decoupling",
        title,
        value=round(value, 1),
        unit="%",
        rule="Leistung bzw. Tempo pro Herzschlag, zweite gegen erste Hälfte",
        threshold=DECOUPLING_PCT,
        measure=value,
        basis="fixed",
    )


def _easy_time_above(facts: _Facts) -> dict[str, Any]:
    """Share of the session's heart-rate zone time above zone 2.

    The paired plan's own seconds above zone 2 (strides, a fast finish) are
    taken off; unplanned surges count.
    """
    title = "Zeit über der Easy-Grenze"
    if reason := facts.heart_rate_unknown():
        return _unknown("easy_time_above", title, reason)
    buckets = [
        bucket
        for bucket in _mapping(facts.zones.get("heart_rate")).get("buckets") or ()
        if isinstance(bucket, Mapping)
    ]
    total = sum(_number(bucket.get("seconds")) or 0.0 for bucket in buckets)
    if not total:
        return _unknown("easy_time_above", title, "Keine Herzfrequenz-Zonenzeiten.")
    above = sum(
        _number(bucket.get("seconds")) or 0.0
        for bucket in buckets
        if isinstance(bucket.get("index"), int) and bucket["index"] >= 3
    )
    planned_above = _number(facts.planned.get("above_easy_s")) or 0.0
    above = max(0.0, above - planned_above)
    share = above / total * 100
    return _judged(
        "easy_time_above",
        title,
        value={"seconds_above": round(above), "share_pct": round(share, 1)},
        unit="%",
        rule="Anteil der Zeit über HF-Zone 2, ohne die im Plan vorgesehene Zeit darüber",
        threshold=EASY_ABOVE_SHARE_PCT,
        measure=share,
        basis="fixed",
    )


def _heart_rate_cap(facts: _Facts, cap: Mapping[str, Any]) -> dict[str, Any]:
    title = "HF-Deckel"
    if reason := facts.heart_rate_unknown():
        return _unknown("heart_rate_cap", title, reason, basis="plan")
    share = _number(cap.get("time_above_cap_pct"))
    verdict = cap.get("verdict")
    if share is None or verdict is None:
        return _unknown(
            "heart_rate_cap",
            title,
            str(cap.get("reason") or "Zeit über dem Deckel unbekannt."),
            basis="plan",
        )
    return _judged(
        "heart_rate_cap",
        title,
        value={
            "cap_bpm": cap.get("cap_bpm"),
            "mean_bpm": cap.get("mean_bpm"),
            "time_above_cap_pct": share,
            "cap_verdict": verdict,
            "max_bpm": facts.metric("max_hr"),
        },
        unit="%",
        rule="Mittel über dem geplanten HF-Deckel oder mehr als 10 % der Zeit darüber",
        threshold=CAP_ABOVE_SHARE_PCT,
        measure=share,
        basis="plan",
        notable=verdict == "above_cap",
    )


def _continuous_checks(facts: _Facts) -> list[dict[str, Any]]:
    """A session without intervals: steadiness, and the fade of a tempo effort."""
    checks: list[dict[str, Any]] = []
    if facts.sport == "ride":
        checks.append(_ride_steadiness(facts))
    if facts.focus in INTERVAL_FOCUS:
        checks.append(
            _halves_change(facts, "main_fade", "Nachlassen im Hauptteil", FADE_PCT)
        )
    return checks


def _interval_checks(facts: _Facts) -> list[dict[str, Any]]:
    reps = [
        rep for rep in facts.execution.get("reps") or () if isinstance(rep, Mapping)
    ]
    if not reps:
        reason = facts.execution.get("reason") or "Keine Wiederholungen erkannt."
        return [_unknown("rep_fade", "Nachlassen über die Wiederholungen", str(reason))]
    return [
        *_rep_targets(facts),
        _rep_fade(facts, reps),
        _within_rep_fade(facts, reps),
        _start_change(facts, reps),
        _cardiac_drift(facts, reps),
        *_recovery_checks(facts),
    ]


def _rep_targets(facts: _Facts) -> list[dict[str, Any]]:
    targets = facts.targets
    if not targets:
        return []
    title = "Zeit im Zielbereich je Wiederholung"
    if reason := _mapping_unreliable(targets):
        return [_unknown("rep_target", title, reason, basis="plan")]
    shares = [
        (rep.get("rep"), share)
        for rep in targets.get("reps") or ()
        if isinstance(rep, Mapping)
        and (share := _number(rep.get("time_in_target_pct"))) is not None
    ]
    if not shares:
        return [
            _unknown(
                "rep_target", title, "Keine Zielbereiche verglichen.", basis="plan"
            )
        ]
    missed = [rep for rep, share in shares if adherence_band(share) == "poor"]
    return [
        _judged(
            "rep_target",
            title,
            value={"reps": len(shares), "missed_reps": missed},
            unit="count",
            rule=f"Wiederholungen mit weniger als {FAIR_SHARE_PCT} % Zeit im Zielbereich",
            threshold=0,
            measure=len(missed),
            basis="plan",
        )
    ]


def _rep_outputs(
    facts: _Facts, reps: Sequence[Mapping[str, Any]]
) -> list[tuple[Any, float]]:
    """Each rep's output as speed or power, so larger is always more.

    Runs give pace in s/km; a short rep reads its lap pace, steadier than GPS.
    """
    values = []
    for rep in reps:
        raw = _number(rep.get("lap_pace")) if rep.get("short") else None
        raw = raw if raw is not None else _number(rep.get("output"))
        if raw is None or raw <= 0:
            continue
        unit = facts.execution.get("output_unit")
        values.append((rep.get("rep"), 1000.0 / raw if unit == "s/km" else raw))
    return values


def _rep_fade(facts: _Facts, reps: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    title = "Nachlassen über die Wiederholungen"
    if reason := facts.output_unknown():
        return _unknown("rep_fade", title, reason)
    if facts.focus == "test":
        return _unknown(
            "rep_fade", title, "Testeinheit: Stufen sind kein gleichmäßiger Block."
        )
    if _mixed_targets(facts.targets):
        return _unknown(
            "rep_fade", title, "Wiederholungen mit unterschiedlichen Zielen."
        )
    values = _rep_outputs(facts, _similar_reps(reps))
    if len(values) < 3:
        return _unknown("rep_fade", title, "Weniger als drei Wiederholungen mit Wert.")
    median = statistics.median(value for _, value in values)
    deviations = [(rep, (value / median - 1) * 100) for rep, value in values]
    worst_rep, worst = min(deviations, key=lambda item: item[1])
    return _judged(
        "rep_fade",
        title,
        value={
            "worst_rep": worst_rep,
            "worst_vs_median_pct": round(worst, 1),
            "below_threshold_reps": [
                rep for rep, pct in deviations if pct <= -FADE_PCT
            ],
        },
        unit="%",
        rule="Schwächste Wiederholung gegen den Median der etwa gleich langen Wiederholungen (Tempo als Geschwindigkeit bzw. Leistung)",
        threshold=FADE_PCT,
        measure=-worst,
        basis="fixed",
    )


def _within_rep_fade(
    facts: _Facts, reps: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    title = "Nachlassen innerhalb der Wiederholungen"
    if reason := facts.output_unknown():
        return _unknown("within_rep_fade", title, reason)
    changes = [
        (rep.get("rep"), change)
        for rep in reps
        if not rep.get("short")
        and (change := _number(rep.get("within_rep_change_pct"))) is not None
    ]
    if not changes:
        return _unknown(
            "within_rep_fade", title, "Keine Wiederholung ab 2 min mit Halbzeitwerten."
        )
    worst_rep, worst = min(changes, key=lambda item: item[1])
    return _judged(
        "within_rep_fade",
        title,
        value={
            "worst_rep": worst_rep,
            "second_vs_first_half_pct": round(worst, 1),
            "below_threshold_reps": [
                rep for rep, change in changes if change <= -FADE_PCT
            ],
        },
        unit="%",
        rule="Zweite gegen erste Hälfte einer Wiederholung (negativ: langsamer bzw. weniger Leistung)",
        threshold=FADE_PCT,
        measure=-worst,
        basis="fixed",
    )


def _start_change(facts: _Facts, reps: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    title = "Zu schneller Start der Wiederholungen"
    if reason := facts.output_unknown():
        return _unknown("start_change", title, reason)
    changes = [
        (rep.get("rep"), change)
        for rep in reps
        if (change := _number(rep.get("start_change_pct"))) is not None
    ]
    if not changes:
        return _unknown("start_change", title, "Keine Startwerte der Wiederholungen.")
    worst_rep, worst = max(changes, key=lambda item: item[1])
    return _judged(
        "start_change",
        title,
        value={
            "worst_rep": worst_rep,
            "start_vs_rest_pct": round(worst, 1),
            "above_threshold_reps": [
                rep for rep, change in changes if change >= FADE_PCT
            ],
        },
        unit="%",
        rule="Erste 30 s gegen den Rest der Wiederholung (positiv: schneller begonnen)",
        threshold=FADE_PCT,
        measure=worst,
        basis="fixed",
    )


def _cardiac_drift(facts: _Facts, reps: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Closing heart rate of the last rep against the first at equal output."""
    title = "Herzfrequenz-Drift bei gleicher Leistung"
    if reason := facts.heart_rate_unknown() or facts.output_unknown():
        return _unknown("cardiac_drift", title, reason)
    # Closing heart rates compare only between reps of one length.
    long_reps = _similar_reps([rep for rep in reps if not rep.get("short")])
    outputs = dict(_rep_outputs(facts, long_reps))
    paired = [
        (rep.get("rep"), outputs[rep.get("rep")], heart_rate)
        for rep in long_reps
        if rep.get("rep") in outputs
        and (heart_rate := _number(rep.get("heart_rate_end"))) is not None
    ]
    if len(paired) < 2:
        return _unknown(
            "cardiac_drift", title, "Weniger als zwei Wiederholungen ab 2 min mit Puls."
        )
    (first_rep, first_output, first_hr), (last_rep, last_output, last_hr) = (
        paired[0],
        paired[-1],
    )
    if abs(last_output / first_output - 1) * 100 > FADE_PCT:
        return _unknown(
            "cardiac_drift",
            title,
            "Erste und letzte Wiederholung liefen nicht mit gleicher Leistung bzw. gleichem Tempo.",
        )
    drift = (last_hr / first_hr - 1) * 100
    return _judged(
        "cardiac_drift",
        title,
        value={
            "first_rep": first_rep,
            "last_rep": last_rep,
            "heart_rate_end_bpm": [round(first_hr), round(last_hr)],
            "drift_pct": round(drift, 1),
        },
        unit="%",
        rule="Puls am Ende der letzten gegen die erste Wiederholung bei gleicher Leistung (±3 %)",
        threshold=CARDIAC_DRIFT_PCT,
        measure=drift,
        basis="fixed",
    )


def _recovery_checks(facts: _Facts) -> list[dict[str, Any]]:
    duration = _recovery_duration(facts)
    return [
        *(() if duration is None else (duration,)),
        _recovery_heart_rate_drop(facts),
    ]


def _recovery_duration(facts: _Facts) -> dict[str, Any] | None:
    """The recovery furthest from its planned length, when reps pair with steps."""
    targets = facts.targets
    title = "Pausenlänge gegen Plan"
    if targets.get("status") != "ok":
        return None
    if reason := _mapping_unreliable(targets):
        return _unknown("recovery_duration", title, reason, basis="plan")
    durations = [
        (rep.get("rep"), actual, planned)
        for rep in targets.get("reps") or ()
        if isinstance(rep, Mapping)
        and (actual := _number(rep.get("recovery_s"))) is not None
        and (planned := _number(rep.get("planned_recovery_s")))
    ]
    if not durations:
        return None
    worst = max(durations, key=lambda item: abs(item[1] / item[2] - 1))
    pct = (worst[1] / worst[2] - 1) * 100
    return _judged(
        "recovery_duration",
        title,
        value={
            "rep": worst[0],
            "actual_s": round(worst[1]),
            "planned_s": round(worst[2]),
            "difference_pct": round(pct, 1),
        },
        unit="%",
        rule="Größte Abweichung einer Pause vom Plan, Betrag",
        threshold=PLAN_TOLERANCE_PCT,
        measure=abs(pct),
        basis="plan",
    )


def _recovery_heart_rate_drop(facts: _Facts) -> dict[str, Any]:
    """Mean HR drop 60 s into the recoveries against the athlete's own median."""
    title = "Puls-Erholung nach 60 s"
    if reason := facts.heart_rate_unknown():
        return _unknown("recovery_heart_rate_drop", title, reason, basis="personal")
    own = _number((comparison_metrics(facts.execution) or {}).get("recovery_drop_mean"))
    if own is None:
        return _unknown(
            "recovery_heart_rate_drop",
            title,
            "Keine Pause von mindestens 65 s.",
            basis="personal",
        )
    median = _number(
        _mapping(facts.comparable.get("median_of_peers")).get("recovery_drop_mean")
    )
    peers = _number(
        _mapping(facts.comparable.get("median_peers")).get("recovery_drop_mean")
    )
    if median is None or peers is None or peers < MIN_PERSONAL:
        return _unknown(
            "recovery_heart_rate_drop",
            title,
            str(
                facts.comparable.get("reason")
                if facts.comparable.get("status") != "ok"
                and facts.comparable.get("reason")
                else f"Weniger als {MIN_PERSONAL} eigene vergleichbare Einheiten mit Pausen-Puls."
            ),
            basis="personal",
            value={"mean_drop_bpm": own},
        )
    below = median - own
    return _judged(
        "recovery_heart_rate_drop",
        title,
        value={
            "mean_drop_bpm": own,
            "own_median_bpm": median,
            "peers": int(peers),
        },
        unit="bpm",
        rule="Mittlerer Pulsabfall 60 s nach Pausenbeginn gegen den eigenen Median",
        threshold=HEART_RATE_DROP_BELOW_BPM,
        measure=below,
        basis="personal",
    )


def _race_checks(facts: _Facts, race: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Positive split, and with a race plan its sections, start, fade and finish."""
    split = _halves_change(
        facts, "race_split", "Zweite gegen erste Hälfte (Split)", RACE_SPLIT_PCT
    )
    if race is None:
        return [split]
    plan = _RacePlan(facts, race)
    return [split, *plan.checks(), plan.finish()]


_RACE_TITLES: Final = {
    "race_sections": "Abschnitte gegen Rennplan",
    "race_start": "Start gegen Rennplan",
    "race_fade": "Einbruchstelle",
}


class _RacePlan:
    """The race against its race plan, section by section (#2803, #3084).

    Runs compare recorded times with the plan's grade-adjusted section times,
    rides the distance-weighted mean power with the planned power. ``behind``
    is the share off plan: positive is slower or less power. A slow final
    stretch (a walk-off after the line) is cut off, then the recorded distance,
    which GPS measures a little long or short, is scaled onto the plan's.
    """

    def __init__(self, facts: _Facts, race: Mapping[str, Any]) -> None:
        self.facts = facts
        self.race = race
        pacing = _mapping(race.get("pacing"))
        self.pacing = pacing
        self.sections = [
            item for item in pacing.get("sections") or () if isinstance(item, Mapping)
        ]
        self.ride = pacing.get("method") == "power_variation"
        streams = _mapping(facts.result.get("streams"))
        channels = _mapping(streams.get("channels"))
        values = [
            streams.get("time_s") or [],
            _mapping(channels.get("distance_m")).get("values") or [],
            _mapping(channels.get("power_w")).get("values") or [],
        ]
        self.points = [
            (distance, time, _number(power))
            for raw_time, raw_distance, power in _aligned(*values)
            if (time := _number(raw_time)) is not None
            and (distance := _number(raw_distance)) is not None
        ]
        self.distances = [point[0] for point in self.points]
        self.end = _race_end(self.points)
        planned = self._total() * 1000
        self.scale = self.end / planned if planned else 1.0

    def checks(self) -> list[dict[str, Any]]:
        if reason := self._coverage_unknown() or self._power_unknown():
            return [
                _unknown(key, title, reason, basis="plan")
                for key, title in _RACE_TITLES.items()
            ]
        rows = [
            {
                "from_km": item["from_km"],
                "to_km": item["to_km"],
                "behind_plan_pct": self._behind(item["from_km"], item["to_km"]),
            }
            for item in self.sections
        ]
        known = [
            abs(row["behind_plan_pct"])
            for row in rows
            if row["behind_plan_pct"] is not None
        ]
        return [
            _judged(
                "race_sections",
                _RACE_TITLES["race_sections"],
                value={
                    "sections": rows,
                    "off_plan": sum(value > RACE_PLAN_PCT for value in known),
                },
                unit="%",
                rule="Zeit bzw. mittlere Leistung je Abschnitt gegen den Rennplan, Betrag (positiv: langsamer bzw. weniger Leistung)",
                threshold=RACE_PLAN_PCT,
                measure=max(known, default=0.0),
                basis="plan",
            ),
            self._start(),
            self._fade(),
        ]

    def finish(self) -> dict[str, Any]:
        """The time to the plan's finish line against the competition's target."""
        title = "Endzeit gegen Zielzeit"
        duration = _mapping(self.race.get("duration"))
        target = _number(duration.get("seconds"))
        if not target:
            return _unknown(
                "race_finish", title, "Keine Zielzeit im Wettkampf.", basis="plan"
            )
        if reason := self._coverage_unknown():
            return _unknown("race_finish", title, reason, basis="plan")
        line = self._time_at(self._total() * 1000 * self.scale)
        if line is None:
            return _unknown(
                "race_finish", title, "Zielzeit nicht messbar.", basis="plan"
            )
        actual = line - self.points[0][1]
        behind = (actual / target - 1) * 100
        return _judged(
            "race_finish",
            title,
            value={
                "finish_s": round(actual),
                "target_s": round(target),
                "target_source": duration.get("source"),
                "behind_target_pct": round(behind, 1),
            },
            unit="%",
            rule="Zeit bis zur Ziellinie des Rennplans gegen die Zielzeit des Wettkampfs (negativ: schneller als das Ziel)",
            threshold=RACE_FINISH_PCT,
            measure=behind,
            basis="plan",
        )

    def _coverage_unknown(self) -> str | None:
        """Why the recording cannot be laid onto the plan's course."""
        if self.pacing.get("status") != "ready" or not self.sections:
            return f"Kein Rennplan: {self.pacing.get('reason') or 'unbekannt'}."
        if reason := self.facts.output_unknown():
            return reason
        recorded = self.end / 1000
        if not recorded or abs(recorded / self._total() - 1) > RACE_DISTANCE_SHARE:
            return "Die aufgezeichnete Distanz passt nicht zum Rennplan."
        return None

    def _power_unknown(self) -> str | None:
        if not self.ride:
            return None
        if not any(item.get("power_w") for item in self.sections):
            return "Keine Zielleistung im Wettkampf."
        if not any(power is not None for *_, power in self.points):
            return "Kein Leistungsverlauf."
        return None

    def _total(self) -> float:
        return float(self.sections[-1]["to_km"]) if self.sections else 0.0

    def _start(self) -> dict[str, Any]:
        end = min(
            self._total(), max(RACE_START_MIN_KM, self._total() * RACE_START_SHARE)
        )
        behind = self._behind(0.0, end)
        if behind is None:
            return _unknown(
                "race_start",
                _RACE_TITLES["race_start"],
                "Startabschnitt nicht messbar.",
                basis="plan",
            )
        return _judged(
            "race_start",
            _RACE_TITLES["race_start"],
            value={"to_km": round(end, 2), "ahead_of_plan_pct": round(-behind, 1)},
            unit="%",
            rule=f"Erste {RACE_START_SHARE * 100:g} % der Distanz, mindestens {RACE_START_MIN_KM:g} km, schneller bzw. mit mehr Leistung als der Rennplan",
            threshold=RACE_PLAN_PCT,
            measure=-behind,
            basis="plan",
        )

    def _fade(self) -> dict[str, Any]:
        """The first km from which every km to the finish is behind plan.

        A km on plan must come before it: a race behind plan from the start
        is the sections' and the finish's finding, not a fade.
        """
        kms = [
            (km, behind)
            for km in range(int(self._total()))
            if (behind := self._behind(float(km), float(km + 1))) is not None
        ]
        if not kms:
            return _unknown(
                "race_fade",
                _RACE_TITLES["race_fade"],
                "Keine vollen Kilometer messbar.",
                basis="plan",
            )
        fade = None
        for km, behind in reversed(kms):
            if behind <= RACE_PLAN_PCT:
                break
            fade = km
        if fade is not None and not any(km < fade for km, _ in kms):
            fade = None
        tail = [behind for km, behind in kms if fade is not None and km >= fade]
        return _judged(
            "race_fade",
            _RACE_TITLES["race_fade"],
            value={
                "fade_from_km": fade,
                "mean_behind_plan_pct": round(statistics.fmean(tail), 1)
                if tail
                else None,
            },
            unit="km",
            rule=f"Erster Kilometer nach einem Kilometer im Plan, ab dem jeder weitere bis ins Ziel mehr als {RACE_PLAN_PCT:g} % hinter dem Rennplan liegt",
            threshold=RACE_PLAN_PCT,
            measure=min(tail, default=0.0),
            basis="plan",
            notable=fade is not None,
        )

    def _behind(self, start_km: float, end_km: float) -> float | None:
        """Share behind plan from ``start_km`` to ``end_km`` of the plan's course."""
        planned = self._planned(start_km, end_km)
        actual = self._actual(start_km, end_km)
        if not planned or not actual:
            return None
        share = (1 - actual / planned) if self.ride else (actual / planned - 1)
        return round(share * 100, 1)

    def _planned(self, start_km: float, end_km: float) -> float | None:
        """Planned seconds (run) or distance-weighted watts (ride) over the range."""
        parts = [
            (overlap / (float(item["to_km"]) - float(item["from_km"])), item)
            for item in self.sections
            if (
                overlap := min(end_km, float(item["to_km"]))
                - max(start_km, float(item["from_km"]))
            )
            > 0
        ]
        key = "power_w" if self.ride else "time_s"
        values = [(share, _number(item.get(key)), item) for share, item in parts]
        if not values or any(value is None for _, value, _ in values):
            return None
        if not self.ride:
            return sum(share * float(value or 0) for share, value, _ in values)
        lengths = [
            share * (float(item["to_km"]) - float(item["from_km"]))
            for share, _, item in values
        ]
        return sum(
            length * float(value or 0)
            for length, (_, value, _) in zip(lengths, values, strict=True)
        ) / sum(lengths)

    def _actual(self, start_km: float, end_km: float) -> float | None:
        """Recorded seconds (run) or distance-weighted mean watts (ride) over the range."""
        low = start_km * 1000 * self.scale
        high = end_km * 1000 * self.scale
        if not self.ride:
            start, end = self._time_at(low), self._time_at(high)
            return None if start is None or end is None else end - start
        first = bisect_left(self.distances, low)
        last = bisect_left(self.distances, high)
        weighted = [
            (self.points[index + 1][0] - self.points[index][0], power)
            for index in range(first, min(last, len(self.points) - 1))
            if (power := self.points[index][2]) is not None
        ]
        length = sum(step for step, _ in weighted)
        return (
            sum(step * power for step, power in weighted) / length if length else None
        )

    def _time_at(self, distance: float) -> float | None:
        """The recorded time at ``distance`` metres, linearly between two samples."""
        index = bisect_left(self.distances, distance)
        if index >= len(self.points):
            return None
        point = self.points[index]
        if index == 0 or point[0] == self.points[index - 1][0]:
            return point[1]
        before = self.points[index - 1]
        share = (distance - before[0]) / (point[0] - before[0])
        return before[1] + share * (point[1] - before[1])


def _race_end(points: Sequence[tuple[float, float, Any]]) -> float:
    """The distance where the race ended: before a slow final stretch, if any."""
    speeds = [
        (after[0] - before[0]) / (after[1] - before[1])
        for before, after in pairwise(points)
        if after[1] > before[1]
    ]
    if not speeds:
        return points[-1][0] if points else 0.0
    slow = statistics.median(speeds) * WALK_OFF_SPEED_SHARE
    end = len(points) - 1
    while end > 0:
        before, after = points[end - 1], points[end]
        if (
            after[1] > before[1]
            and (after[0] - before[0]) / (after[1] - before[1]) >= slow
        ):
            break
        end -= 1
    return points[end][0]


def _aligned(*series: Any) -> list[tuple[Any, ...]]:
    """The series' values index by index; a short or missing series pads None."""
    lists = [list(values) if isinstance(values, list) else [] for values in series]
    length = max((len(values) for values in lists), default=0)
    return [
        tuple(values[index] if index < len(values) else None for values in lists)
        for index in range(length)
    ]


def _halves_change(
    facts: _Facts, key: str, title: str, threshold: float
) -> dict[str, Any]:
    """Second against first half of the main part, from the execution halves.

    The halves leave out a warm-up lap, or else the first 10 min of a session
    of 30 min or more, so a fast start is not part of this check.
    """
    if reason := facts.output_unknown():
        return _unknown(key, title, reason)
    halves = _mapping(facts.execution.get("halves"))
    change = _number(halves.get("change_pct"))
    if change is None:
        return _unknown(
            key,
            title,
            str(
                halves.get("reason") or "Keine Hälften (kein durchgehender Abschnitt)."
            ),
        )
    return _judged(
        key,
        title,
        value={
            "second_vs_first_pct": round(change, 1),
            "main_part_s": halves.get("main_part_s"),
        },
        unit="%",
        rule="Zweite gegen erste Hälfte des Hauptteils ohne Einlaufen bzw. die ersten 10 min (negativ: langsamer bzw. weniger Leistung; Laufen noch ohne Steigungsbereinigung)",
        threshold=threshold,
        measure=-change,
        basis="fixed",
    )


def _judged(
    key: str,
    title: str,
    *,
    value: Any,
    unit: str | None,
    rule: str,
    threshold: float,
    measure: float,
    basis: str,
    notable: bool | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """A check with a verdict: notable once ``measure`` passes ``threshold``.

    ``notable`` takes the verdict an owner module already computed.
    """
    notable = measure > threshold if notable is None else notable
    excess = max(0.0, measure - threshold)
    scale = threshold or 1.0
    severity = _WEIGHT[key] * 10 + (min(9, int(excess / scale * 10)) if notable else 0)
    return {
        "id": key,
        "title": title,
        "value": value,
        "unit": unit,
        "rule": rule,
        "threshold": threshold,
        "basis": basis,
        "verdict": "notable" if notable else "ok",
        "severity": severity if notable else 0,
        **extra,
    }


def _unknown(
    key: str, title: str, reason: str, *, basis: str = "fixed", value: Any = None
) -> dict[str, Any]:
    return {
        "id": key,
        "title": title,
        "value": value,
        "verdict": "unknown",
        "reason": reason,
        "basis": basis,
        "severity": 0,
    }


def _mapping_unreliable(targets: Mapping[str, Any]) -> str | None:
    """Why reps cannot be judged against plan steps; None when they pair one to one.

    Steps and laps pair in order, so one missing or extra lap shifts every
    later pair.
    """
    if targets.get("status") != "ok":
        return _mapping_reason(targets)
    summary = _mapping(targets.get("summary"))
    if summary.get("planned_work_steps") != summary.get("executed_work_laps"):
        return "Plan-Schritte und Runden weichen ab; die Zuordnung ist unsicher."
    return None


_TARGET_KEYS: Final = (
    "target_pace_s_per_km",
    "target_kmh",
    "target_watts",
    "target_bpm",
)


def _mixed_targets(targets: Mapping[str, Any]) -> bool:
    """True when the planned reps carry different target ranges (a pyramid)."""
    shown = {
        str([rep.get(key) for key in _TARGET_KEYS])
        for rep in targets.get("reps") or ()
        if isinstance(rep, Mapping) and any(rep.get(key) for key in _TARGET_KEYS)
    }
    return len(shown) > 1


def _similar_reps(reps: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The largest group of reps within 35 % of one rep's duration; longer reps win a tie.

    So 3 x 1 km with 3 x 400 m judges the 1 km reps among themselves, with the
    duration share that comparable sessions pair reps by.
    """
    timed = [
        (rep, duration) for rep in reps if (duration := _number(rep.get("duration_s")))
    ]
    if not timed:
        return list(reps)

    def group(anchor: float) -> list[Mapping[str, Any]]:
        return [rep for rep, d in timed if abs(d / anchor - 1) <= DURATION_SHARE]

    anchor = max((d for _, d in timed), key=lambda d: (len(group(d)), d))
    return group(anchor)


def _mapping_reason(targets: Mapping[str, Any]) -> str:
    """Why plan steps and laps could not be paired, in planned_targets' words."""
    reason = targets.get("reason")
    return (
        str(reason)
        if reason
        else "Keine verlässliche Zuordnung von Plan-Schritten zu Runden."
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _condition_checks(facts: _Facts) -> list[dict[str, Any]]:
    """Grade-adjusted pace of an outdoor run and RPE against intensity (#3084 Part 3).

    GAP is an estimate after Minetti 2002 and stays ``ok``: it only explains,
    never decides. RPE is judged against the athlete's own rated sessions only;
    without five of them at the same intensity it is unknown.
    """
    checks: list[dict[str, Any]] = []
    if facts.sport == "run" and not summary_is_indoor(facts.session):
        title = "Steigungsbereinigte Pace (GAP)"
        gap = session_conditions.grade_adjusted_pace(facts.result)
        if gap is None:
            checks.append(
                _unknown(
                    "grade_adjusted_pace",
                    title,
                    "Keine Tempo-, Steigungs- oder Höhendaten.",
                )
            )
        elif reason := facts.output_unknown():
            checks.append(_unknown("grade_adjusted_pace", title, reason, value=gap))
        else:
            checks.append(
                _judged(
                    "grade_adjusted_pace",
                    title,
                    value=gap,
                    unit="s/km",
                    rule=f"{session_conditions.GAP_METHOD}; nie allein ein Befund",
                    threshold=0,
                    measure=0,
                    basis="fixed",
                    notable=False,
                )
            )
    if facts.sport not in ("run", "ride"):
        return checks
    title = "RPE gegen Intensität"
    inputs = _mapping(facts.result.get(session_conditions.INPUTS))
    load = _mapping(facts.result.get("session_rpe_load"))
    rpe = _number(load.get("rpe"))
    intensity = _number(inputs.get("intensity"))
    if rpe is None or intensity is None:
        reason = (
            "Keine RPE-Bewertung der Einheit."
            if rpe is None
            else "Keine Intensität (IF) der Einheit."
        )
        checks.append(_unknown("rpe_intensity", title, reason, basis="personal"))
        return checks
    scale = load.get("rpe_scale")
    expected, count = session_conditions.expected_rpe(
        intensity, facts.sport, scale, inputs.get("rated_sessions") or ()
    )
    value = {
        "rpe": rpe,
        "expected_rpe": expected,
        "intensity": round(intensity, 2),
        "rated_sessions": count,
        "rpe_scale": scale,
    }
    if expected is None:
        checks.append(
            _unknown(
                "rpe_intensity",
                title,
                f"Zu wenige eigene bewertete Einheiten bei dieser Intensität "
                f"({count} von {session_conditions.MIN_RATED}).",
                basis="personal",
                value=value,
            )
        )
        return checks
    difference = rpe - expected
    checks.append(
        _judged(
            "rpe_intensity",
            title,
            value={**value, "difference": round(difference, 1)},
            unit="RPE",
            rule="Session-RPE gegen den Median der eigenen bewerteten Einheiten "
            "gleicher Sportart und Skala bei IF ± 0,05 (Foster)",
            threshold=session_conditions.RPE_GAP_POINTS,
            measure=abs(difference),
            basis="personal",
            notable=abs(difference) >= session_conditions.RPE_GAP_POINTS,
        )
    )
    return checks

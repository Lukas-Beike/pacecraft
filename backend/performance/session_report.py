"""The deterministic report of one analysed session; the Coach only explains it (#3084).

Every check carries its value, the rule and threshold it applies, the basis of
that threshold (``personal`` when the athlete has enough own comparable values,
``plan`` against the planned unit, else ``fixed``), a verdict (``ok``,
``notable``, ``unknown`` with a reason, or ``not_applicable`` with a reason,
such as a power pacing check in ERG) and a deterministic severity. The
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
from datetime import date
from itertools import pairwise
from typing import Any, Final

from backend.activities.session_purpose import summary_is_race
from backend.athlete.measurements import finite_float as _number
from backend.canonical.activity_summary import summary_is_indoor
from backend.performance import session_conditions
from backend.performance.comparable_sessions import (
    DURATION_SHARE,
    SAME_OUTPUT_SHARE,
)
from backend.performance.form_drift import (
    CONTACT_TIME_RISE_PCT,
    STEP_LENGTH_FALL_PCT,
)
from backend.performance.planned_targets import (
    CAP_ABOVE_SHARE_PCT,
    FAIR_SHARE_PCT,
    adherence_band,
)
from backend.performance.session_conditions import STOP_S
from backend.performance.session_execution import (
    GAP_SHARE,
    HR_SPIKE_BPM,
    HR_SPIKE_SHARE,
    MAX_GAP_S,
    PAUSE_S,
    SPEED_JUMP_MPS,
    SPEED_JUMP_SHARE,
    rounded_mean,
)
from backend.weather.activity_weather import HEAT_APPARENT_C

METHOD: Final = "session-report-v1"
SCOPE: Final = (
    "Checks computed by code from this session's stored data, each with its "
    "value, rule, threshold, basis (personal, plan or fixed), verdict and "
    "severity. notable lists every notable check by severity; lead is the "
    "first four of them. unknown checks name why; not_applicable checks do "
    "not apply to this session (ERG, a planned cadence) and name why. facts "
    "are values without a verdict. Thresholds: athlete decisions on #3084."
)
# Plan against actual: duration, load and recoveries (TrainingPeaks green, ±20 %).
PLAN_TOLERANCE_PCT: Final = 20.0
# Aerobic decoupling, the fixed fallback (Friel / TrainingPeaks: < 5 % good).
DECOUPLING_PCT: Final = 5.0
# Interval fade and start change against the median of the reps, and within a rep.
FADE_PCT: Final = 3.0
# Cardiac drift across reps at equal output, the decoupling rule applied to reps.
CARDIAC_DRIFT_PCT: Final = 5.0
# A ride without power (athlete, #3293): heart rate only, on labelled work laps
# this long and within this share of each other's duration.
HEART_RATE_RISE_MIN_S: Final = 180.0
HEART_RATE_RISE_DURATION_SHARE: Final = 0.10
# Easy, recovery and long sessions: share of time above the LT1 heart rate in
# effect (Seiler; #2830, #3915).
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
# A shoe without its own or Garmin's target is due at this many km (athlete, #3084).
SHOE_DEFAULT_TARGET_KM: Final = 700.0
# The shoe hint is notable from this share of its target distance.
SHOE_DUE_SHARE: Final = 0.9
# Ride IF (NP / FTP) per session focus, Coggan's levels (athlete, #3084).
RIDE_IF_RANGES: Final = {
    "recovery": (0.0, 0.55),
    "easy": (0.56, 0.75),
    "endurance": (0.56, 0.75),
    "long": (0.56, 0.75),
    "tempo": (0.76, 0.87),
    "sweet_spot": (0.84, 0.97),
    "threshold": (0.91, 1.05),
}
# Low cadence on climbs: a ride rep steeper than this (%) below this cadence (athlete, #3084).
CLIMB_GRADE_PCT: Final = 4.0
CLIMB_CADENCE_RPM: Final = 70.0
# ERG: heart rate this much above them at the same power (athlete, #3084).
ERG_HEART_RATE_PCT: Final = 3.0
# Pace at the same heart rate this much slower than own comparable runs (athlete, #3084).
PACE_AT_HR_PCT: Final = 3.0
# Zone time is judged in the zones holding at least this share of the planned time.
ZONE_MIN_SHARE: Final = 0.10
# The notable checks the answer leads with.
LEAD_COUNT: Final = 4
EASY_FOCUS: Final = frozenset({"recovery", "easy", "endurance", "long"})
INTERVAL_FOCUS: Final = frozenset(
    {"tempo", "threshold", "vo2max", "anaerobic", "sweet_spot", "test"}
)
# Base severity per check: the plan and its main intent first.
# The data each check reads that a failed gate makes unjudgeable (#3248): a
# check never turns notable on spiky heart rate ("heart_rate"), a run's
# jumping speed ("speed"), a simulated indoor course ("indoor") or a pacing
# the ERG trainer held ("erg"). tests/test_session_report.py feeds every check
# each declared gate's bad variant. The checks of session_conditions declare
# their own gates there.
GATES: Final[dict[str, frozenset[str]]] = {
    "plan_structure": frozenset(),
    "rep_target": frozenset(),
    "recovery_duration": frozenset(),
    "plan_duration": frozenset(),
    "plan_load": frozenset(),
    "rep_fade": frozenset({"speed", "erg"}),
    "within_rep_fade": frozenset({"speed", "erg"}),
    "start_change": frozenset({"speed", "erg"}),
    "easy_time_above": frozenset({"heart_rate"}),
    "heart_rate_cap": frozenset({"heart_rate"}),
    "decoupling": frozenset({"heart_rate"}),
    "recovery_heart_rate_drop": frozenset({"heart_rate"}),
    "pace_at_heart_rate": frozenset({"heart_rate"}),
    "erg_heart_rate": frozenset({"heart_rate"}),
    "cardiac_drift": frozenset({"heart_rate", "speed"}),
    "heart_rate_rise": frozenset({"heart_rate"}),
    "stride_halves": frozenset({"speed"}),
    "main_fade": frozenset({"speed"}),
    "steadiness": frozenset({"speed"}),
    "race_split": frozenset({"speed"}),
    "race_sections": frozenset({"speed"}),
    "race_start": frozenset({"speed"}),
    "race_fade": frozenset({"speed"}),
    "race_finish": frozenset({"speed"}),
    "climb_cadence": frozenset({"indoor"}),
    "zone_time": frozenset({"heart_rate"}),
    "form_drift": frozenset(),
    "shoe_mileage": frozenset(),
    "ride_intensity": frozenset(),
}
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
    "form_drift": 3,
    "shoe_mileage": 1,
    "ride_intensity": 3,
    "erg_heart_rate": 3,
    "climb_cadence": 2,
    "zone_time": 4,
    "pace_at_heart_rate": 3,
    "stride_halves": 3,
    "plan_duration": 3,
    "plan_load": 3,
    "decoupling": 3,
    "cardiac_drift": 3,
    "heart_rate_rise": 3,
    "within_rep_fade": 3,
    "main_fade": 3,
    "start_change": 2,
    "steadiness": 2,
    "recovery_duration": 2,
    "recovery_heart_rate_drop": 2,
    "rpe_intensity": 2,
    # Estimates: shown, never a finding alone.
    "grade_adjusted_pace": 0,
    "wind": 0,
    # Facts the report passes on; they never decide (#3084 Part 3c).
    "training_effect": 0,
    "data_quality": 0,
    "strength_before": 0,
    "heat": 0,
    "hills": 0,
    "recovery_before": 0,
    "prior_load": 0,
    # Strength plan against sets (athlete, #3084 3c B): a plan check.
    "strength_sets": 3,
}


def session_report(
    result: Mapping[str, Any],
    planned: Mapping[str, Any] | None = None,
    training_load: float | None = None,
    race: Mapping[str, Any] | None = None,
    plan_zones: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The report of a ``get_activity_details`` result.

    ``planned`` is the paired unit's estimate (``duration_s``, ``load`` and
    ``above_easy_s``, its planned seconds above LT1), None without a paired
    unit; ``training_load`` is the session's local TSS-scale load (M6), the
    scale of the estimate. A provider load is never compared. ``race`` is
    the linked competition's race plan (``race_plan``), None without one.
    ``plan_zones`` is the paired unit's planned against actual seconds per zone
    (``planned_step_adherence`` zones), None without a paired unit.
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
        *(_run_block(facts) if facts.sport == "run" else ()),
        *_zone_time(facts, plan_zones),
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
        "facts": _session_facts(facts),
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
        self.planned_cadence = planned.get("cadence") is True
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
        """Why output checks cannot judge: no output (a ride without power), or a
        speed-based output whose speed jumps."""
        if reason := self.execution.get("output_reason"):
            return str(reason)
        speed_based = self.execution.get("output_unit") != "W"
        if speed_based and self.speed_quality not in (None, "ok"):
            return f"Datenqualität Geschwindigkeit: {self.speed_quality}."
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
    unmatched = summary.get("unmatched_hard_steps")
    if isinstance(unmatched, int) and unmatched:
        # The fitting paired laps with the hardest steps only: a planned block
        # may be missing, so the count cannot be judged.
        return _unknown(
            "plan_structure",
            title,
            "Harte Plan-Schritte ohne Runde, etwa ein ausgelassener Block; die Zuordnung ist unsicher.",
            basis="plan",
            value={
                "planned_work_steps": planned + unmatched,
                "executed_work_laps": executed,
                "unmatched_hard_steps": unmatched,
            },
        )
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
        if (intensity := _ride_intensity(facts)) is not None:
            checks.append(intensity)
    elif (steady := _run_steadiness(facts)) is not None:
        checks.append(steady)
    checks.append(_decoupling(facts))
    cap = _mapping(facts.targets.get("heart_rate_cap"))
    # A capped unit is judged by its cap, never by the LT1 line too (#3915).
    checks.append(_heart_rate_cap(facts, cap) if cap else _easy_time_above(facts))
    return checks


_STEADINESS_TITLE: Final = "Gleichmäßigkeit (GAP)"


def _run_steadiness_value(facts: _Facts) -> tuple[float | None, float | None, int]:
    """The run's GAP variation, the own upper quartile and the peers it rests on."""
    own = _number(_mapping(facts.comparable.get("this")).get("gap_cv_pct"))
    quartile = _number(
        _mapping(facts.comparable.get("upper_quartile_of_peers")).get("gap_cv_pct")
    )
    peers = int(
        _number(_mapping(facts.comparable.get("median_peers")).get("gap_cv_pct")) or 0
    )
    return own, quartile, peers


def _run_steadiness(facts: _Facts) -> dict[str, Any] | None:
    """An easy or long run's GAP variation against own comparable runs (#3084).

    Only a personal limit exists: with fewer than 5 comparable runs the value
    is a fact (``_session_facts``), never judged against a fixed bound.
    """
    own, quartile, peers = _run_steadiness_value(facts)
    if own is None or quartile is None or peers < MIN_PERSONAL:
        return None
    if reason := facts.output_unknown():
        return _unknown("steadiness", _STEADINESS_TITLE, reason, basis="personal")
    return _judged(
        "steadiness",
        _STEADINESS_TITLE,
        value={"gap_cv_pct": own, "own_upper_quartile_pct": quartile, "peers": peers},
        unit="%",
        rule="Schwankung der steigungsbereinigten Geschwindigkeit über die Bewegungszeit, über dem oberen Quartil eigener vergleichbarer Läufe",
        threshold=quartile,
        measure=own,
        basis="personal",
    )


def _session_if(facts: _Facts) -> float | None:
    """The session's local IF (NP / FTP of the session day); stored as a percent."""
    stored = _number(
        _mapping(facts.result.get(session_conditions.INPUTS)).get("intensity")
    )
    return stored / 100 if stored is not None and stored > 0 else None


def _ride_intensity(facts: _Facts) -> dict[str, Any] | None:
    """A continuous ride's IF against Coggan's range of its focus.

    The main part's power, without the warm-up the halves leave out, at the
    session day's FTP (NP / IF), rounded as shown before it is judged.
    """
    limits = RIDE_IF_RANGES.get(str(facts.focus))
    if limits is None:
        return None
    title = "Intensität gegen Fokus (IF)"
    factor = _session_if(facts)
    power = facts.metric("normalized_power")
    if factor is None or not power:
        return _unknown(
            "ride_intensity", title, "Keine Intensität (IF) der Einheit, etwa ohne FTP."
        )
    # The main part's own NP, without the warm-up the halves leave out; an
    # analysis stored before it carries none and keeps the session's IF.
    halves = _mapping(facts.execution.get("halves"))
    main = _number(_mapping(halves.get("main")).get("normalized_power"))
    part = "session"
    if main and facts.execution.get("output_unit") == "W":
        factor, part = main / (power / factor), "main_part"
    shown = round(factor, 2)
    low, high = limits
    return _judged(
        "ride_intensity",
        title,
        value={
            "intensity_factor": shown,
            "range": [low, high],
            "focus": facts.focus,
            "part": part,
        },
        unit=None,
        rule="IF des Hauptteils (NP / FTP des Tages) im Coggan-Bereich des Fokus",
        threshold=0,
        measure=round(max(0.0, low - shown, shown - high), 2),
        basis="fixed",
    )


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
    peers = _number(
        _mapping(facts.comparable.get("median_peers")).get("decoupling_pct")
    )
    quartile = _number(
        _mapping(facts.comparable.get("upper_quartile_of_peers")).get("decoupling_pct")
    )
    if (
        facts.comparable.get("status") == "ok"
        and peers
        and peers >= MIN_PERSONAL
        and quartile is not None
    ):
        return _judged(
            "decoupling",
            title,
            value={
                "decoupling_pct": round(value, 1),
                "own_upper_quartile_pct": quartile,
                "peers": int(peers),
            },
            unit="%",
            rule="Leistung bzw. Pace pro Herzschlag, zweite gegen erste Hälfte, über dem oberen Quartil eigener vergleichbarer Einheiten und über 5 %",
            threshold=max(quartile, DECOUPLING_PCT),
            measure=value,
            basis="personal",
        )
    return _judged(
        "decoupling",
        title,
        value={"decoupling_pct": round(value, 1)},
        unit="%",
        rule="Leistung bzw. Pace pro Herzschlag, zweite gegen erste Hälfte",
        threshold=DECOUPLING_PCT,
        measure=value,
        basis="fixed",
    )


def _easy_time_above(facts: _Facts) -> dict[str, Any]:
    """Share of the session's heart-rate time above the LT1 heart rate in effect.

    ``easy_limit`` of the report inputs holds that bpm and the stream's seconds
    above it and in all. The paired plan's own seconds above LT1 (strides, a
    fast finish) are taken off; unplanned surges count.
    """
    title = "Zeit über der Easy-Grenze"
    if reason := facts.heart_rate_unknown():
        return _unknown("easy_time_above", title, reason)
    limit = _mapping(
        _mapping(facts.result.get(session_conditions.INPUTS)).get("easy_limit")
    )
    total = _number(limit.get("total_s")) or 0.0
    if not total:
        return _unknown(
            "easy_time_above",
            title,
            "Kein Herzfrequenzverlauf oder keine Laktatschwelle (LSHF).",
        )
    above = _number(limit.get("above_s")) or 0.0
    planned_above = _number(facts.planned.get("above_easy_s")) or 0.0
    above = max(0.0, above - planned_above)
    share = above / total * 100
    return _judged(
        "easy_time_above",
        title,
        value={
            "seconds_above": round(above),
            "share_pct": round(share, 1),
            "limit_bpm": limit.get("bpm"),
        },
        unit="%",
        rule="Anteil der Zeit über der LT1-Herzfrequenz, ohne die im Plan vorgesehene Zeit darüber",
        threshold=EASY_ABOVE_SHARE_PCT,
        measure=share,
        basis="fixed",
    )


def _heart_rate_cap(facts: _Facts, cap: Mapping[str, Any]) -> dict[str, Any]:
    title = "Herzfrequenz-Deckel"
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
        rule="Mittel über dem geplanten Herzfrequenz-Deckel oder mehr als 10 % der Zeit darüber",
        threshold=CAP_ABOVE_SHARE_PCT,
        measure=share,
        basis="plan",
        notable=verdict == "above_cap",
    )


def _continuous_checks(facts: _Facts) -> list[dict[str, Any]]:
    """A session without intervals: steadiness, and the fade of a tempo effort."""
    checks: list[dict[str, Any]] = []
    if _without_power(facts):
        checks.append(
            _unknown(
                "heart_rate_rise",
                _HEART_RATE_RISE_TITLE,
                "Keine Arbeits- und Erholungsrunden vom Gerät; Abschnitte werden "
                "nicht aus der Geschwindigkeit abgeleitet.",
            )
        )
    if facts.sport == "ride":
        checks.append(_ride_steadiness(facts))
        if (intensity := _ride_intensity(facts)) is not None:
            checks.append(intensity)
    if facts.focus in INTERVAL_FOCUS:
        checks.append(
            _halves_change(facts, "main_fade", "Nachlassen im Hauptteil", FADE_PCT)
        )
    return checks


_REP_FADE_TITLE: Final = "Nachlassen über die Wiederholungen"


def _interval_checks(facts: _Facts) -> list[dict[str, Any]]:
    reps = [
        rep for rep in facts.execution.get("reps") or () if isinstance(rep, Mapping)
    ]
    if not reps:
        reason = facts.execution.get("reason") or "Keine Wiederholungen erkannt."
        return [_unknown("rep_fade", _REP_FADE_TITLE, str(reason))]
    pacing = (
        [
            _not_applicable("rep_fade", _REP_FADE_TITLE),
            _not_applicable(
                "within_rep_fade", "Nachlassen innerhalb der Wiederholungen"
            ),
            _not_applicable("start_change", "Zu schneller Start der Wiederholungen"),
            _erg_heart_rate(facts),
        ]
        if facts.execution.get("erg_like") is True
        else [
            _rep_fade(facts, reps),
            _within_rep_fade(facts, reps),
            _start_change(facts, reps),
        ]
    )
    return [
        *_rep_targets(facts),
        *pacing,
        _cardiac_drift(facts, reps),
        *([_heart_rate_rise(facts, reps)] if _without_power(facts) else ()),
        *_recovery_checks(facts),
        *(_climb_cadence(facts, reps) if facts.sport == "ride" else ()),
    ]


_HEART_RATE_RISE_TITLE: Final = "Herzfrequenz-Anstieg über die Wiederholungen"


def _without_power(facts: _Facts) -> bool:
    """A ride the report judges on heart rate alone: it recorded no power (#3293)."""
    return facts.sport == "ride" and facts.execution.get("output_reason") is not None


def _heart_rate_rise(
    facts: _Facts, reps: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Closing heart rate of the last against the first long work lap (#3293).

    A ride without power: only heart rate judges it, on labelled work laps of at
    least 3 min (heart rate trails shorter efforts) and of equal length (within
    10 %). Without power it stays open whether the effort or fatigue rose.
    """
    title = _HEART_RATE_RISE_TITLE
    if reason := facts.heart_rate_unknown():
        return _unknown("heart_rate_rise", title, reason)
    long = [
        rep
        for rep in reps
        if (_number(rep.get("duration_s")) or 0) >= HEART_RATE_RISE_MIN_S
        and _number(rep.get("heart_rate_end")) is not None
    ]
    if len(long) < 2:
        return _unknown(
            "heart_rate_rise",
            title,
            "nicht bewertbar: Herzfrequenz folgt kurzen Belastungen zu träge (unter 3 min)",
        )
    first, last = long[0], long[-1]
    first_s, last_s = float(first["duration_s"]), float(last["duration_s"])
    if abs(last_s / first_s - 1) > HEART_RATE_RISE_DURATION_SHARE:
        return _unknown(
            "heart_rate_rise",
            title,
            "Erste und letzte lange Wiederholung sind nicht gleich lang (über 10 %).",
        )
    rise = (float(last["heart_rate_end"]) / float(first["heart_rate_end"]) - 1) * 100
    return _judged(
        "heart_rate_rise",
        title,
        value={
            "first_rep": first.get("rep"),
            "last_rep": last.get("rep"),
            "first_heart_rate_end": first["heart_rate_end"],
            "last_heart_rate_end": last["heart_rate_end"],
            "rise_pct": round(rise, 1),
        },
        unit="%",
        rule="Herzfrequenz am Ende der letzten gegen die erste gleich lange Arbeitsrunde ab 3 min; ohne Leistung bleibt offen, ob die Belastung oder die Ermüdung stieg",
        threshold=CARDIAC_DRIFT_PCT,
        measure=rise,
        basis="fixed",
    )


def _not_applicable(key: str, title: str) -> dict[str, Any]:
    """A pacing check the athlete did not pace: the ERG trainer held the power."""
    return {
        "id": key,
        "title": title,
        "value": None,
        "verdict": "not_applicable",
        "reason": "ERG: der Trainer hielt die Leistung.",
        "basis": "fixed",
        "severity": 0,
    }


def _erg_heart_rate(facts: _Facts) -> dict[str, Any]:
    """Heart rate at the same ERG power against own comparable sessions.

    An efficiency trend: within ``SAME_OUTPUT_SHARE`` of the rep power, at
    least 5 own sessions, else a value only; notable from 3 % higher (athlete,
    #3084, confirmed 2026-11-16).
    """
    title = "Herzfrequenz bei gleicher ERG-Leistung"
    if reason := facts.heart_rate_unknown():
        return _unknown("erg_heart_rate", title, reason, basis="personal")
    same = _mapping(facts.comparable.get("at_same_output"))
    own = _number(_mapping(facts.comparable.get("this")).get("heart_rate_end_mean"))
    median = _number(same.get("heart_rate_end_mean"))
    count = int(_number(same.get("count")) or 0)
    share = f"±{SAME_OUTPUT_SHARE * 100:g} %"
    if own is None:
        reason = str(
            facts.comparable.get("reason")
            or "Keine Herzfrequenz am Ende der Wiederholungen."
        )
        return _unknown("erg_heart_rate", title, reason, basis="personal")
    if median is None or count < MIN_PERSONAL:
        return _unknown(
            "erg_heart_rate",
            title,
            f"Weniger als {MIN_PERSONAL} eigene Einheiten bei gleicher Leistung ({share}).",
            basis="personal",
            value={"heart_rate_end_mean": own},
        )
    higher = (own / median - 1) * 100
    return _judged(
        "erg_heart_rate",
        title,
        value={
            "heart_rate_end_mean": own,
            "own_median_bpm": median,
            "higher_pct": round(higher, 1),
            "peers": count,
        },
        unit="%",
        rule=f"Herzfrequenz am Ende der Wiederholungen gegen den Median eigener Einheiten bei gleicher Leistung ({share})",
        threshold=ERG_HEART_RATE_PCT,
        measure=higher,
        basis="personal",
    )


def _climb_cadence(
    facts: _Facts, reps: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Ride reps steeper than ``CLIMB_GRADE_PCT`` below ``CLIMB_CADENCE_RPM``.

    Outdoors only: an indoor or virtual course's grade is simulated.
    """
    if summary_is_indoor(facts.session):
        return []
    climbs = [
        (rep.get("rep"), grade, cadence)
        for rep in reps
        if (rise := _number(rep.get("elevation_change_m"))) is not None
        and (distance := _number(rep.get("distance_m")))
        and (grade := rise / distance * 100) > CLIMB_GRADE_PCT
        and (cadence := _number(rep.get("cadence"))) is not None
    ]
    if not climbs:
        return []
    title = "Trittfrequenz am Anstieg"
    if facts.planned_cadence:
        return [
            {
                **_not_applicable("climb_cadence", title),
                "reason": "Der Plan gibt eine Trittfrequenz vor.",
            }
        ]
    low = [
        (rep, round(grade, 1), round(cadence))
        for rep, grade, cadence in climbs
        if cadence < CLIMB_CADENCE_RPM
    ]
    return [
        _judged(
            "climb_cadence",
            title,
            value={
                "climbing_reps": len(climbs),
                "low_cadence_reps": [
                    {"rep": rep, "grade_pct": grade, "cadence_rpm": cadence}
                    for rep, grade, cadence in low
                ],
            },
            unit="count",
            rule="Wiederholungen über 4 % Steigung mit weniger als 70 U/min",
            threshold=0,
            measure=len(low),
            basis="fixed",
        )
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
    title = _REP_FADE_TITLE
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
        rule="Schwächste Wiederholung gegen den Median der etwa gleich langen Wiederholungen (Geschwindigkeit bzw. Leistung)",
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
            "cardiac_drift",
            title,
            "Weniger als zwei Wiederholungen ab 2 min mit Herzfrequenz.",
        )
    (first_rep, first_output, first_hr), (last_rep, last_output, last_hr) = (
        paired[0],
        paired[-1],
    )
    if abs(last_output / first_output - 1) * 100 > FADE_PCT:
        return _unknown(
            "cardiac_drift",
            title,
            "Erste und letzte Wiederholung liefen nicht mit gleicher Leistung bzw. gleicher Pace.",
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
        rule="Herzfrequenz am Ende der letzten gegen die erste Wiederholung bei gleicher Leistung (±3 %)",
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
    title = "Herzfrequenz-Erholung nach 60 s"
    if reason := facts.heart_rate_unknown():
        return _unknown("recovery_heart_rate_drop", title, reason, basis="personal")
    # Read from the recoveries themselves, so a ride without output (no power,
    # #3293) is judged too (Gitar on #3300).
    drops = [
        drop
        for item in facts.execution.get("recoveries") or ()
        if isinstance(item, Mapping)
        and (drop := _number(item.get("heart_rate_drop_60s"))) is not None
    ]
    own = rounded_mean(drops) if drops else None
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
                else f"Weniger als {MIN_PERSONAL} eigene vergleichbare Einheiten mit Pausen-Herzfrequenz."
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
        rule="Mittlerer Herzfrequenzabfall 60 s nach Pausenbeginn gegen den eigenen Median",
        threshold=HEART_RATE_DROP_BELOW_BPM,
        measure=below,
        basis="personal",
    )


def _run_block(facts: _Facts) -> list[dict[str, Any]]:
    """The run's own checks beside its session type (#3084 Part 2)."""
    reps = [
        rep
        for rep in facts.execution.get("reps") or ()
        if isinstance(rep, Mapping) and not rep.get("short")
    ]
    checks = [
        _form_drift(facts),
        _pace_at_heart_rate(facts),
        _stride_halves(facts, reps) if facts.kind == "interval" and reps else None,
    ]
    shoes = [
        item
        for item in facts.result.get("equipment") or ()
        if isinstance(item, Mapping) and item.get("kind") == "shoes"
    ]
    checks.extend(_shoe_mileage(item) for item in shoes)
    return [check for check in checks if check is not None]


def _form_drift(facts: _Facts) -> dict[str, Any] | None:
    """The stored form drift of a run: running dynamics from the first to the last third."""
    drift = _mapping(_mapping(facts.result.get("analysis")).get("form_drift"))
    if not drift:
        return None
    title = "Laufstil-Drift"
    if drift.get("status") != "ok":
        return _unknown(
            "form_drift", title, str(drift.get("reason") or "Keine Form-Drift.")
        )
    metrics = {
        name: {
            "change_pct": item.get("change_percent"),
            "threshold_pct": item.get("threshold_percent"),
        }
        for name, item in _mapping(drift.get("metrics")).items()
        if isinstance(item, Mapping)
    }
    rise = _number(metrics.get("contact_time", {}).get("change_pct"))
    return _judged(
        "form_drift",
        title,
        value={
            "pace_change_pct": drift.get("pace_change_percent"),
            "metrics": metrics,
        },
        unit="%",
        rule="Bodenkontaktzeit vom ersten zum letzten Drittel bei gleicher Pace (Form-Drift)",
        threshold=CONTACT_TIME_RISE_PCT,
        measure=rise or 0.0,
        basis="fixed",
        notable=drift.get("flagged") is True,
    )


def _pace_at_heart_rate(facts: _Facts) -> dict[str, Any] | None:
    """Pace in the session's heart-rate band against own comparable runs of 42 days."""
    data = _mapping(facts.result.get("pace_at_heart_rate"))
    if not data:
        return None
    title = "Pace bei gleicher Herzfrequenz"
    if reason := facts.heart_rate_unknown():
        return _unknown("pace_at_heart_rate", title, reason, basis="personal")
    if data.get("status") != "ok":
        reason = str(data.get("reason") or "Nicht vergleichbar.")
        return _unknown("pace_at_heart_rate", title, reason, basis="personal")
    comparable = _mapping(data.get("comparable"))
    count = int(_number(comparable.get("count")) or 0)
    median = _number(comparable.get("median_pace_s_per_km"))
    difference = _number(data.get("difference_s_per_km"))
    if count < MIN_PERSONAL or not median or difference is None:
        return _unknown(
            "pace_at_heart_rate",
            title,
            f"Weniger als {MIN_PERSONAL} vergleichbare Läufe.",
            basis="personal",
        )
    slower = difference / median * 100
    return _judged(
        "pace_at_heart_rate",
        title,
        value={
            "pace_s_per_km": _mapping(data.get("session")).get("pace_s_per_km"),
            "own_median_s_per_km": median,
            "slower_pct": round(slower, 1),
            "heart_rate_band_bpm": data.get("heart_rate_band_bpm"),
            "peers": count,
        },
        unit="%",
        rule="Pace im Herzfrequenzband der Einheit gegen den Median eigener vergleichbarer Läufe (positiv: langsamer)",
        threshold=PACE_AT_HR_PCT,
        measure=slower,
        basis="personal",
    )


def _stride_halves(facts: _Facts, reps: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Stride length of each rep's second half against its first."""
    title = "Schrittlänge je Wiederholung"
    if reason := facts.output_unknown():
        return _unknown("stride_halves", title, reason)
    changes = [
        (rep.get("rep"), (second / first - 1) * 100)
        for rep in reps
        if (first := _number(rep.get("stride_length_first_half_m")))
        and (second := _number(rep.get("stride_length_second_half_m"))) is not None
    ]
    if not changes:
        return _unknown(
            "stride_halves", title, "Keine Wiederholung ab 2 min mit Schrittlänge."
        )
    worst_rep, worst = min(changes, key=lambda item: item[1])
    return _judged(
        "stride_halves",
        title,
        value={
            "worst_rep": worst_rep,
            "second_vs_first_half_pct": round(worst, 1),
            "below_threshold_reps": [
                rep for rep, change in changes if change < -STEP_LENGTH_FALL_PCT
            ],
        },
        unit="%",
        rule="Schrittlänge der zweiten gegen die erste Hälfte einer Wiederholung (negativ: kürzer)",
        threshold=STEP_LENGTH_FALL_PCT,
        measure=-worst,
        basis="fixed",
    )


def _zone_time(
    facts: _Facts, plan_zones: Mapping[str, Any] | None
) -> list[dict[str, Any]]:
    """Seconds per zone against the paired unit's plan, in the plan's leading metric."""
    if not plan_zones:
        return []
    title = "Zonenzeit gegen Plan"
    if plan_zones.get("status") != "ok":
        missing = str(plan_zones.get("reason") or "Keine Zonenzeiten im Plan.")
        return [_unknown("zone_time", title, missing, basis="plan")]
    reason = (
        facts.heart_rate_unknown()
        if plan_zones.get("metric") == "heartrate"
        else facts.output_unknown()
    )
    if reason:
        return [_unknown("zone_time", title, reason, basis="plan")]
    rows = [row for row in plan_zones.get("zones") or () if isinstance(row, Mapping)]
    total = sum(_number(row.get("planned_s")) or 0.0 for row in rows)
    judged: list[dict[str, Any]] = [
        {
            "zone": row.get("zone"),
            "planned_s": planned,
            "actual_s": actual,
            "difference_pct": round((actual / planned - 1) * 100, 1),
        }
        for row in rows
        if (planned := _number(row.get("planned_s")) or 0.0) >= total * ZONE_MIN_SHARE
        and planned > 0
        and (actual := _number(row.get("actual_s"))) is not None
    ]
    if not judged:
        return [
            _unknown("zone_time", title, "Keine geplanten Zonenzeiten.", basis="plan")
        ]
    # Untargeted steps (a free warm-up) count in the actual seconds only, so
    # a plan covering much less than the session cannot be judged per zone.
    recorded = sum(_number(row.get("actual_s")) or 0.0 for row in rows)
    if total < recorded * (1 - PLAN_TOLERANCE_PCT / 100):
        return [
            _unknown(
                "zone_time",
                title,
                "Nicht jeder geplante Abschnitt hat eine Zielzone.",
                basis="plan",
            )
        ]
    return [
        _judged(
            "zone_time",
            title,
            value={"metric": plan_zones.get("metric"), "zones": judged},
            unit="%",
            rule="Sekunden je Zone gegen den Plan, in Zonen mit mindestens 10 % der geplanten Zeit, Betrag",
            threshold=PLAN_TOLERANCE_PCT,
            measure=max(abs(row["difference_pct"]) for row in judged),
            basis="plan",
        )
    ]


def _session_facts(facts: _Facts) -> list[dict[str, Any]]:
    """Values the report states without a verdict (athlete, #3084 Part 2)."""
    found = [
        _work_laps_of_critical_speed(facts),
        _steadiness_fact(facts),
        _reps_of_ftp(facts),
        _dynamics_bands(facts),
        _elevation(facts),
        _stops(facts),
    ]
    return [fact for fact in found if fact is not None]


def _work_laps_of_critical_speed(facts: _Facts) -> dict[str, Any] | None:
    """A run's work laps in % of its critical speed, a model estimate."""
    critical = _mapping(facts.result.get("critical_speed"))
    speed = _number(critical.get("cs_mps"))
    if facts.sport != "run" or not speed or summary_is_indoor(facts.session):
        return None
    laps = [
        {"rep": rep, "pct_of_critical_speed": round(value / speed * 100, 1)}
        for rep, value in _rep_outputs(facts, facts.execution.get("reps") or ())
    ]
    if not laps:
        return None
    return {
        "id": "work_laps_of_critical_speed",
        "title": "Arbeitsrunden in % der kritischen Geschwindigkeit",
        "value": {"laps": laps, "critical_speed_mps": speed},
        "unit": "%",
        "basis": "model estimate",
    }


def _dynamics_bands(facts: _Facts) -> dict[str, Any] | None:
    """Running dynamics per pace band against own runs: better, usual or worse."""
    rating = _mapping(facts.result.get("running_dynamics_rating"))
    if facts.sport != "run" or rating.get("status") != "ok":
        return None
    return {
        "id": "running_dynamics_bands",
        "title": "Laufdynamik je Pace-Bereich gegen eigene Läufe",
        "value": {key: rating[key] for key in ("bands", "metrics") if key in rating},
        "unit": None,
        "basis": "personal",
    }


def _steadiness_fact(facts: _Facts) -> dict[str, Any] | None:
    """An easy run's GAP variation as a fact while too few own runs judge it."""
    own, quartile, peers = _run_steadiness_value(facts)
    if facts.sport != "run" or facts.kind != "easy" or own is None:
        return None
    if quartile is not None and peers >= MIN_PERSONAL:
        return None
    return {
        "id": "run_steadiness",
        "title": _STEADINESS_TITLE,
        "value": {"gap_cv_pct": own, "peers": peers},
        "unit": "%",
        "basis": "personal",
    }


def _reps_of_ftp(facts: _Facts) -> dict[str, Any] | None:
    """A ride's reps in % of the session day's FTP (NP / IF), a fact."""
    factor = _session_if(facts)
    power = facts.metric("normalized_power")
    if (
        facts.sport != "ride"
        or facts.execution.get("output_unit") != "W"
        or not factor
        or not power
    ):
        return None
    ftp = power / factor
    reps = [
        {"rep": rep.get("rep"), "pct_of_ftp": round(output / ftp * 100)}
        for rep in facts.execution.get("reps") or ()
        if isinstance(rep, Mapping) and (output := _number(rep.get("output")))
    ]
    if not reps:
        return None
    return {
        "id": "reps_of_ftp",
        "title": "Wiederholungen in % der FTP",
        "value": {"reps": reps, "ftp_w": round(ftp)},
        "unit": "%",
    }


def _elevation(facts: _Facts) -> dict[str, Any] | None:
    gain = facts.metric("elevation_gain_m")
    if gain is None or summary_is_indoor(facts.session):
        return None
    return {
        "id": "elevation",
        "title": "Höhenmeter",
        "value": {"gain_m": round(gain), "loss_m": facts.metric("elevation_loss_m")},
        "unit": "m",
    }


def _stops(facts: _Facts) -> dict[str, Any] | None:
    """Standstills longer than ``STOP_S``: their count and total time.

    A standstill is a stretch flagged not moving, or a recording gap (auto
    pause) longer than the stop limit. A standstill still open at the end is
    after the session, not a stop.
    """
    streams = _mapping(facts.result.get("streams"))
    moving = _mapping(_mapping(streams.get("channels")).get("moving")).get("values")
    if not isinstance(moving, list):
        return None
    samples = [
        (clock, flag)
        for time, flag in _aligned(streams.get("time_s") or [], moving)
        if (clock := _number(time)) is not None and flag is not None
    ]
    # A gap counts as a pause only beyond three sampling steps, so a long
    # session's coarse, downsampled stream is not all stops.
    steps = [
        after - before
        for (before, _), (after, _) in pairwise(samples)
        if after > before
    ]
    gap = max(STOP_S, 3 * statistics.median(steps)) if steps else STOP_S
    stops: list[float] = []
    started = None
    for (before, was_moving), (after, flag) in pairwise(samples):
        if started is None and (not was_moving or after - before > gap):
            started = before
        if started is not None and flag:
            stops.append(after - started)
            started = None
    long_stops = [seconds for seconds in stops if seconds > STOP_S]
    return {
        "id": "stops",
        "title": "Stopps über 30 s",
        "value": {"count": len(long_stops), "total_s": round(sum(long_stops))},
        "unit": "s",
    }


def _shoe_mileage(item: Mapping[str, Any]) -> dict[str, Any] | None:
    """The run's shoe against its target distance: its own, Garmin's, else 700 km."""
    usage = _number(_mapping(item.get("usage")).get("distance_km"))
    if usage is None:
        return None
    lifetime = _mapping(item.get("lifetime"))
    target = _number(lifetime.get("target_km"))
    source = item.get("lifetime_target_source") if target else "default"
    target = target or SHOE_DEFAULT_TARGET_KM
    share = usage / target
    return _judged(
        "shoe_mileage",
        "Schuh-Kilometer",
        value={
            "equipment_id": item.get("id"),
            "usage_km": round(usage, 1),
            "target_km": round(target, 1),
            "target_source": source,
            "share_pct": round(share * 100, 1),
        },
        unit="%",
        rule="Gelaufene Kilometer gegen das Ziel des Schuhs (eigenes, Garmin, sonst 700 km)",
        threshold=SHOE_DUE_SHARE * 100,
        measure=share * 100,
        basis="fixed",
        notable=share >= SHOE_DUE_SHARE,
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
    if _number(summary.get("unmatched_hard_steps")):
        return "Harte Plan-Schritte ohne Runde, etwa ein ausgelassener Block; die Zuordnung ist unsicher."
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
    checks: list[dict[str, Any]] = _fact_checks(facts)
    if facts.sport == "run" and not summary_is_indoor(facts.session):
        title = "Steigungsbereinigte Pace (GAP)"
        gap = session_conditions.grade_adjusted_pace(facts.result)
        if gap is None:
            checks.append(
                _unknown(
                    "grade_adjusted_pace",
                    title,
                    "Keine Pace-, Steigungs- oder Höhendaten.",
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
    if not summary_is_indoor(facts.session):
        checks.append(_wind_check(facts))
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
        "intensity_pct": round(intensity, 1),
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
            "gleicher Sportart und Skala bei IF ± 5 Prozentpunkten (Foster)",
            threshold=session_conditions.RPE_GAP_POINTS,
            measure=abs(difference),
            basis="personal",
            notable=abs(difference) >= session_conditions.RPE_GAP_POINTS,
        )
    )
    return checks


def _wind_check(facts: _Facts) -> dict[str, Any]:
    """Wind at body height, only ever an explanation (#3084 Part 3b).

    It stays ``ok`` with weight 0 and names in ``explains`` a slower part it
    explains: the second half slower than the first (the halves check) or a
    fading rep (the rep fade check), each judged by its own function, with at
    least 3 m/s headwind at body height in that part and 2 m/s more than in the
    part it is compared with.
    """
    title = "Wind auf Körperhöhe (geschätzt)"
    wind = _mapping(
        _mapping(facts.result.get(session_conditions.INPUTS)).get("headwind")
    )
    coverage = _number(wind.get("coverage_pct"))
    if not wind or coverage is None:
        return _unknown("wind", title, "Kein Gegenwindwert (kein eigener Track).")
    if coverage < session_conditions.WIND_MIN_COVERAGE_PCT:
        return _unknown(
            "wind",
            title,
            f"Laufrichtung nur für {coverage:.0f} % der Bewegungszeit bekannt.",
        )
    explains = [*_wind_halves(facts, wind), *_wind_reps(facts, wind)]
    return _judged(
        "wind",
        title,
        value={
            "headwind_mps": session_conditions.body_headwind(wind),
            "head_share_pct": wind.get("head_share_pct"),
            "tail_share_pct": wind.get("tail_share_pct"),
            "coverage_pct": coverage,
            "explains": explains,
        },
        unit="m/s",
        rule=(
            "Mittlere Windkomponente in Bewegungsrichtung (+ Gegenwind), Wind am "
            "Start (Open-Meteo, 10 m) auf 2 m reduziert (log. Windprofil, z0 0,03 m); "
            "nur Erklärung, mit zweitem Signal"
        ),
        threshold=session_conditions.WIND_EXPLAINS_MPS,
        measure=0,
        basis="fixed",
        notable=False,
    )


def _wind_halves(facts: _Facts, wind: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The second half, when the report's halves check finds it slower and wind explains it.

    Only where the report judges halves, with that check's own key and rule: the
    split of a race (2 %), the main-part fade of a continuous tempo effort (3 %).
    """
    if facts.kind == "race":
        key, threshold = "race_split", RACE_SPLIT_PCT
    elif facts.kind == "continuous" and facts.focus in INTERVAL_FOCUS:
        key, threshold = "main_fade", FADE_PCT
    else:
        return []
    if _halves_change(facts, key, "", threshold)["verdict"] != "notable":
        return []
    halves = _mapping(facts.execution.get("halves"))
    start, length = _number(halves.get("start_s")), _number(halves.get("main_part_s"))
    if start is None or not length:
        return []
    middle = start + length / 2
    first = session_conditions.body_headwind(wind, (start, middle))
    second = session_conditions.body_headwind(wind, (middle, start + length))
    if not _wind_explains(second, first):
        return []
    return [{"check": key, "part_mps": second, "compared_mps": first}]


def _wind_reps(facts: _Facts, wind: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The weakest rep, when the rep fade check finds it and wind explains it."""
    reps = [
        rep for rep in facts.execution.get("reps") or () if isinstance(rep, Mapping)
    ]
    if facts.kind != "interval" or not reps:
        return []
    fade = _rep_fade(facts, reps)
    if fade["verdict"] != "notable":
        return []
    winds = {}
    for rep in _similar_reps(reps):
        start, length = _number(rep.get("start_s")), _number(rep.get("duration_s"))
        if start is not None and length:
            value = session_conditions.body_headwind(wind, (start, start + length))
            if value is not None:
                winds[rep.get("rep")] = value
    worst = _mapping(fade.get("value")).get("worst_rep")
    if worst not in winds or len(winds) < 3:
        return []
    median = statistics.median(winds.values())
    if not _wind_explains(winds[worst], median):
        return []
    return [
        {
            "check": "rep_fade",
            "rep": worst,
            "part_mps": winds[worst],
            "compared_mps": median,
        }
    ]


def _wind_explains(part: float | None, compared: float | None) -> bool:
    return (
        part is not None
        and compared is not None
        and part >= session_conditions.WIND_EXPLAINS_MPS
        and part - compared >= session_conditions.WIND_SECOND_SIGNAL_MPS
    )


def _fact_checks(facts: _Facts) -> list[dict[str, Any]]:
    """Facts passed on with their source, never a finding (#3084 Part 3c).

    The provider's training effect, the data-quality rules the other checks
    apply, and the strength sessions in the 48 h before a run or ride.
    """
    checks = [_training_effect(facts)]
    quality = _mapping(facts.execution.get("data_quality"))
    if quality:
        checks.append(_data_quality(quality))
    before = _mapping(facts.result.get("strength_before"))
    if before and facts.sport in ("run", "ride"):
        checks.append(_strength_before(before))
    plan = _mapping(facts.result.get("strength_plan"))
    if plan:
        # Sets only, no load (athlete, #3084 3c B); a set counts done over planned
        # reps, at most 1, against the plan the session trained (#3521).
        checks.append(
            _against_plan(
                "strength_sets",
                "Sätze gegen Plan",
                _number(plan.get("completed_sets")),
                plan.get("planned_sets"),
                "count",
            )
        )
    weather = _mapping(facts.result.get("weather"))
    if weather and facts.sport in ("run", "ride"):
        checks.append(_heat(facts, weather))
    inputs = _mapping(facts.result.get(session_conditions.INPUTS))
    if facts.sport in ("run", "ride"):
        if isinstance(facts.result.get("recovery_before"), list):
            checks.append(_recovery_before(facts, facts.result["recovery_before"]))
        if isinstance(inputs.get("daily_loads"), Mapping):
            checks.append(_prior_load(facts, inputs["daily_loads"]))
    reps = [
        rep for rep in facts.execution.get("reps") or () if isinstance(rep, Mapping)
    ]
    if facts.sport == "ride" and reps:
        checks.append(_hills(reps))
    return checks


def _fact(key: str, title: str, value: Any, rule: str, basis: str) -> dict[str, Any]:
    return _judged(
        key,
        title,
        value=value,
        unit=None,
        rule=rule,
        threshold=0,
        measure=0,
        basis=basis,
        notable=False,
    )


def _training_effect(facts: _Facts) -> dict[str, Any]:
    title = "Trainingseffekt (Anbieter)"
    label = _mapping(facts.result.get("training_effect_label"))
    aerobic = facts.metric("aerobic_training_effect")
    anaerobic = facts.metric("anaerobic_training_effect")
    if aerobic is None and anaerobic is None and not label.get("value"):
        return _unknown(
            "training_effect",
            title,
            "Kein Trainingseffekt vom Anbieter.",
            basis="provider",
        )
    return _fact(
        "training_effect",
        title,
        {
            "aerobic": aerobic,
            "aerobic_source": _mapping(
                facts.metrics.get("aerobic_training_effect")
            ).get("source"),
            "anaerobic": anaerobic,
            "anaerobic_source": _mapping(
                facts.metrics.get("anaerobic_training_effect")
            ).get("source"),
            "label": label.get("value"),
            "label_source": label.get("source"),
        },
        "Einschätzung des Anbieters, unverändert weitergegeben; kein eigenes Urteil",
        "provider",
    )


def _data_quality(quality: Mapping[str, Any]) -> dict[str, Any]:
    """Which data the quality rules doubt; the checks that read them are unknown."""
    doubted = {
        name: state
        for name, state in quality.items()
        if isinstance(state, str) and state != "ok"
    }
    return _fact(
        "data_quality",
        "Datenqualität",
        {
            "heart_rate": quality.get("heart_rate"),
            "speed": quality.get("speed"),
            "doubted": sorted(doubted),
        },
        f"Herzfrequenz: Spitzen über {HR_SPIKE_BPM:.0f} bpm in mehr als "
        f"{HR_SPIKE_SHARE * 100:.0f} % der Paare oder Schritt-/Trittfrequenz-Kopplung; Geschwindigkeit: "
        f"Sprünge über {SPEED_JUMP_MPS:g} m/s in mehr als "
        f"{SPEED_JUMP_SHARE * 100:.0f} % der Paare; beide: Lücken über "
        f"{MAX_GAP_S:g} s in mehr als {GAP_SHARE * 100:.0f} % der aufgezeichneten "
        f"Zeit (Uhrsprünge über {PAUSE_S:g} s sind Pausen). Prüfungen auf "
        "bezweifelten Daten sind unbekannt",
        "fixed",
    )


def _strength_before(before: Mapping[str, Any]) -> dict[str, Any]:
    title = "Kraft in den 48 h davor"
    if before.get("status") != "ok":
        return _unknown(
            "strength_before",
            title,
            str(before.get("reason") or "Krafteinheiten nicht lesbar."),
        )
    sessions = [
        item for item in before.get("sessions") or () if isinstance(item, Mapping)
    ]
    if not sessions and before.get("planned_checked") is False:
        # No completed session and the plan could not be read: not a known zero.
        return _unknown("strength_before", title, "Geplante Einheiten nicht lesbar.")
    hours = [_number(item.get("hours_before")) for item in sessions]
    known = [value for value in hours if value is not None]
    return _fact(
        "strength_before",
        title,
        {
            "sessions": len(sessions),
            "legs": sum(1 for item in sessions if item.get("legs")),
            "intensities": [item.get("intensity") for item in sessions],
            "closest_hours_before": round(min(known), 1) if known else None,
            "confirmed": before.get("origin") == "completed",
        },
        "Krafteinheiten in den 48 h vor dem Start: erledigt, sonst geplant und "
        "nicht bestätigt; nur Fakten, keine Ursache",
        "fixed",
    )


def _heat(facts: _Facts, weather: Mapping[str, Any]) -> dict[str, Any]:
    """Heat from the activity weather only (#3036), only ever an explanation.

    From 25 C apparent temperature it names in ``explains`` the heart-rate
    checks the report finds notable (decoupling, the heart-rate cap or the time
    above LT1, cardiac drift), each judged by its own function (athlete, #3084 3c C).
    """
    title = "Hitze (Wetter der Einheit)"
    apparent = _number(weather.get("apparent_temperature_c"))
    if apparent is None:
        return _unknown("heat", title, "Keine gefühlte Temperatur gespeichert.")
    explains = _notable_heart_checks(facts) if apparent >= HEAT_APPARENT_C else []
    return _judged(
        "heat",
        title,
        value={
            "apparent_temperature_c": apparent,
            "temperature_c": _number(weather.get("temperature_c")),
            "source": weather.get("source"),
            "explains": explains,
        },
        unit="C",
        rule=f"Gefühlte Temperatur ab {HEAT_APPARENT_C:.0f} °C erklärt auffällige "
        "Herzfrequenz-Prüfungen; nie ein eigener Befund",
        threshold=HEAT_APPARENT_C,
        measure=0,
        basis="fixed",
        notable=False,
    )


def _hills(reps: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Each rep's net elevation change and mean grade, facts only (athlete, #3084 3c E)."""
    rows = []
    for rep in reps:
        change = _number(rep.get("elevation_change_m"))
        distance = _number(rep.get("distance_m"))
        rows.append(
            {
                "rep": rep.get("rep"),
                "elevation_change_m": None if change is None else round(change, 1),
                "grade_pct": round(100 * change / distance, 1)
                if change is not None and distance
                else None,
            }
        )
    return _fact(
        "hills",
        "Steigung je Wiederholung",
        rows,
        "Netto-Höhenänderung und mittlere Steigung je Wiederholung; nur Fakten",
        "fixed",
    )


def _notable_heart_checks(facts: _Facts) -> list[str]:
    """The heart-rate checks the report finds notable, each by its own function.

    Decoupling and time above LT1 for easy sessions, cardiac drift for
    interval sessions: what heat or the night before may explain (#3084 3c).
    """
    heart_checks = []
    if facts.kind == "easy":
        # The same check the report judges by: the cap, else the LT1 line (#3915).
        cap = _mapping(facts.targets.get("heart_rate_cap"))
        line = _heart_rate_cap(facts, cap) if cap else _easy_time_above(facts)
        heart_checks = [_decoupling(facts), line]
    elif facts.kind == "interval":
        reps = [
            rep for rep in facts.execution.get("reps") or () if isinstance(rep, Mapping)
        ]
        heart_checks = [_cardiac_drift(facts, reps)] if reps else []
    return [check["id"] for check in heart_checks if check["verdict"] == "notable"]


# The side of the athlete's own quartiles that may explain a heart-rate finding.
_UNFAVOURABLE: Final = {"sleep": "below", "hrv": "below", "resting_hr": "above"}


def _recovery_before(
    facts: _Facts, baselines: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """The night before against the athlete's own quartiles, facts only (#3084 3c G).

    ``explains`` names notable heart-rate checks only when a value of that very
    night lies on its unfavourable side: less sleep, lower HRV, higher resting HR.
    """
    title = "Die Nacht davor (Schlaf, HRV, Ruheherzfrequenz)"
    day = str(_mapping(facts.session).get("started_at_local") or "")[:10]
    nights = []
    for baseline in baselines:
        if not isinstance(baseline, Mapping):
            continue
        nights.append(
            {
                key: baseline.get(key)
                for key in (
                    "metric",
                    "measurement",
                    "value",
                    "unit",
                    "observed_at",
                    "median",
                    "lower",
                    "upper",
                    "position",
                    "status",
                    "source",
                )
            }
            | {"that_night": baseline.get("observed_at") == day}
        )
    if not nights:
        return _unknown(
            "recovery_before",
            title,
            "Keine Werte zu Schlaf, HRV oder Ruheherzfrequenz.",
        )
    unfavourable = any(
        night["that_night"]
        and night["position"] is not None
        and night["position"] == _UNFAVOURABLE.get(str(night["metric"]))
        for night in nights
    )
    return _judged(
        "recovery_before",
        title,
        value={
            "nights": nights,
            "explains": _notable_heart_checks(facts) if unfavourable else [],
        },
        unit=None,
        rule="Wert der Nacht vor der Einheit gegen die eigenen mittleren Quartile "
        "(mindestens 14 Werte in 42 Tagen); nur Fakten, Erklärung nur zu einer "
        "auffälligen Herzfrequenz-Prüfung",
        threshold=0,
        measure=0,
        basis="personal",
        notable=False,
    )


def _prior_load(facts: _Facts, loads: Mapping[str, Any]) -> dict[str, Any]:
    """The local load of the 3 days before against the own 42-day median (#3084 3c H)."""
    title = "Belastung der 3 Tage davor"
    day = str(_mapping(facts.session).get("started_at_local") or "")[:10]
    try:
        session_day = date.fromisoformat(day)
    except ValueError:
        return _unknown("prior_load", title, "Kein Datum der Einheit.")
    load = session_conditions.prior_load(
        {
            key: float(value)
            for key, value in loads.items()
            if _number(value) is not None
        },
        session_day,
    )
    if load is None:
        return _unknown("prior_load", title, "Keine lokale Belastung in 45 Tagen.")
    return _fact(
        "prior_load",
        title,
        load,
        "Lokale Belastung (M6) der 3 Tage vor der Einheit gegen den Median aller "
        "3-Tages-Summen der 42 Tage davor; nur Fakten",
        "personal",
    )

"""Own activity energy, net of resting metabolism (M11, #3100).

Pure functions, one per component of the daily model
TDEE = RMR + TEF + sum of session net cost + NEAT. Every session term is net
of the resting metabolic rate, so it adds to RMR without counting it twice.
Sources and their limits are on the method card
(docs/performance-methods/m11-activity-energy.md).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

METHOD_ID = "activity-energy"
# Bump when a formula or constant below changes the estimate.
METHOD_VERSION = 1
# Joules per kilocalorie (thermochemical calorie).
J_PER_KCAL = 4184.0
# Standard gravity, m/s^2, for the vertical work of floors.
GRAVITY = 9.81
# Minetti et al. 2002 (J Appl Physiol 93:1039, Fig. 2): net metabolic cost of
# running per unit distance, J/kg/m, as a 5th-order polynomial of the gradient
# (rise/run, -0.45 to +0.45); coefficients from i^5 down to the constant.
MINETTI_RUNNING = (155.4, -30.4, -43.3, 46.3, 19.5, 3.6)
# Minetti et al. 2002: the same fit for walking.
MINETTI_WALKING = (280.5, -58.7, -76.8, 51.9, 19.6, 2.5)
# The gradient range Minetti et al. 2002 measured; steeper segments are clamped.
GRADE_LIMIT = 0.45
# Segments shorter than this are merged for the grade, so GPS altitude noise on
# a few metres does not become a steep gradient.
GRADE_MIN_RUN_M = 20.0
# Gross efficiency of cycling (mechanical / metabolic energy): 19.8 % measured in
# 17 trained men by Moseley & Jeukendrup 2001 (R42); it varies with work rate
# and training (R41, R43), and the one calibration factor absorbs an
# individual offset (weak evidence).
CYCLING_GROSS_EFFICIENCY = 0.20
# 2024 Adult Compendium of Physical Activities (Herrmann et al., J Sport Health
# Sci 13:6): code 02054, resistance training, multiple exercises, 8-15 reps.
STRENGTH_MET_GENERAL = 3.5
# Compendium 2024 code 02050: free weights, power lifting, vigorous effort.
STRENGTH_MET_VIGOROUS = 6.0
# Keytel et al. 2005 (J Sports Sci 23:289), model without VO2max: gross
# energy expenditure in kJ/min from heart rate (bpm), weight (kg) and age
# (years): intercept, HR, weight, age. Coefficients as reproduced by several
# secondary sources; fallback only (about +-20-30 % individually).
KEYTEL_MALE = (-55.0969, 0.6309, 0.1988, 0.2017)
KEYTEL_FEMALE = (-20.4022, 0.4472, -0.1263, 0.074)
# Flex-HR: the first minutes of a session are not steady state and are not fitted.
FLEX_HR_SKIP_S = 180.0
# Flex-HR needs at least this many steady segments from power sessions.
FLEX_HR_MIN_SEGMENTS = 20
# Vertical metres per counted floor (Garmin counts about 3 m of ascent).
FLOOR_HEIGHT_M = 3.0
# Efficiency of vertical work when walking up steep gradients: Minetti et al.
# 2002 found uphill walking above +0.15 reaches concentric muscle efficiency,
# taken here as 0.25 (assumption from that statement).
UPHILL_WORK_EFFICIENCY = 0.25
# Step length from body height when no recorded walk measured it; a common
# heuristic, weak evidence (+-10-15 %).
STEP_LENGTH_PER_HEIGHT = 0.415
# Thermic effect of food as a share of each macronutrient's energy: midpoints
# of protein 20-30 %, carbohydrate 5-15 %, fat 0-3 % (Joosen & Westerterp
# 2006, Nutr Metab 3:25). Alcohol is not stored, so it has no term.
TEF_PROTEIN = 0.25
TEF_CARBOHYDRATE = 0.10
TEF_FAT = 0.015
# Atwater energy per gram: protein, carbohydrate, fat (kcal/g).
KCAL_PER_G_PROTEIN = 4.0
KCAL_PER_G_CARBOHYDRATE = 4.0
KCAL_PER_G_FAT = 9.0
# Optional excess post-exercise oxygen consumption for sessions above the
# threshold: 6 % of the net session cost, the low end of LaForgia et al. 2006
# (J Sports Sci 24:1247, 6-15 %). Off unless asked for; weak evidence.
EPOC_SHARE = 0.06


def _polynomial(coefficients: Sequence[float], grade: float) -> float:
    value = 0.0
    for coefficient in coefficients:
        value = value * grade + coefficient
    return value


def running_cost_j_per_kg_m(grade: float) -> float:
    """Minetti's net running cost at a gradient, clamped to the measured range."""
    return _polynomial(MINETTI_RUNNING, max(-GRADE_LIMIT, min(GRADE_LIMIT, grade)))


def walking_cost_j_per_kg_m(grade: float) -> float:
    """Minetti's net walking cost at a gradient, clamped to the measured range."""
    return _polynomial(MINETTI_WALKING, max(-GRADE_LIMIT, min(GRADE_LIMIT, grade)))


def graded_segments(
    distance_m: Sequence[float | None], altitude_m: Sequence[float | None]
) -> list[tuple[float, float]]:
    """(run, grade) per segment of cumulative distance and altitude streams.

    Consecutive samples are merged until the run reaches ``GRADE_MIN_RUN_M``,
    so altitude noise over a few metres does not become a steep gradient.
    A missing altitude repeats the last known one (0 before any), so a run
    without altitude counts as flat instead of dropping out. Samples without
    distance, or where distance goes back, start a new segment.
    """
    segments: list[tuple[float, float]] = []
    start: tuple[float, float] | None = None
    last_altitude = 0.0
    for index, distance in enumerate(distance_m):
        given = altitude_m[index] if index < len(altitude_m) else None
        altitude = last_altitude if given is None else given
        last_altitude = altitude
        if distance is None:
            continue
        if start is None or distance < start[0]:
            start = (distance, altitude)
            continue
        run = distance - start[0]
        if run >= GRADE_MIN_RUN_M:
            segments.append((run, (altitude - start[1]) / run))
            start = (distance, altitude)
    return segments


def running_net_kcal(mass_kg: float, segments: Iterable[tuple[float, float]]) -> float:
    """Net running cost: mass x sum of segment run x Minetti cost at its grade."""
    joules = sum(run * running_cost_j_per_kg_m(grade) for run, grade in segments)
    return mass_kg * joules / J_PER_KCAL


def cycling_net_kcal(
    work_kj: float,
    duration_s: float,
    rmr_kcal_per_day: float,
    efficiency: float = CYCLING_GROSS_EFFICIENCY,
) -> float:
    """Metabolic cost of the mechanical work, minus resting energy over the duration."""
    gross = work_kj / (J_PER_KCAL / 1000.0) / efficiency
    return max(0.0, gross - rmr_kcal_per_day * duration_s / 86400.0)


def strength_net_kcal(
    duration_s: float, rmr_kcal_per_day: float, vigorous: bool = False
) -> float:
    """(MET - 1) x the measured resting rate x duration (MET corrected to own RMR)."""
    met = STRENGTH_MET_VIGOROUS if vigorous else STRENGTH_MET_GENERAL
    return (met - 1.0) * rmr_kcal_per_day * duration_s / 86400.0


def keytel_net_kcal(
    mean_hr: float,
    duration_s: float,
    weight_kg: float,
    age_years: float,
    male: bool,
    rmr_kcal_per_day: float,
) -> float:
    """Keytel's gross estimate from heart rate, net of resting energy (fallback)."""
    intercept, hr, weight, age = KEYTEL_MALE if male else KEYTEL_FEMALE
    kj_per_min = intercept + hr * mean_hr + weight * weight_kg + age * age_years
    gross = max(0.0, kj_per_min) * duration_s / 60.0 / (J_PER_KCAL / 1000.0)
    return max(0.0, gross - rmr_kcal_per_day * duration_s / 86400.0)


@dataclass(frozen=True)
class FlexHrSegment:
    """One steady segment of a power session: mean HR, its net cost and elapsed time."""

    mean_hr: float
    net_kcal_per_min: float
    elapsed_min: float


@dataclass(frozen=True)
class FlexHrFit:
    """Net kcal/min = intercept + per_bpm x HR + per_min x elapsed minutes."""

    intercept: float
    per_bpm: float
    per_min: float
    segments: int


def fit_flex_hr(segments: Sequence[FlexHrSegment]) -> FlexHrFit | None:
    """Least-squares fit of net cost on HR with an elapsed-time drift term.

    None with fewer than ``FLEX_HR_MIN_SEGMENTS`` segments or a singular
    design (all segments at one HR), so the caller falls back to Keytel.
    """
    if len(segments) < FLEX_HR_MIN_SEGMENTS:
        return None
    rows = [(1.0, s.mean_hr, s.elapsed_min) for s in segments]
    targets = [s.net_kcal_per_min for s in segments]
    normal = [[sum(r[i] * r[j] for r in rows) for j in range(3)] for i in range(3)]
    rhs = [sum(r[i] * t for r, t in zip(rows, targets, strict=True)) for i in range(3)]
    solution = _solve3(normal, rhs)
    if solution is None:
        return None
    return FlexHrFit(solution[0], solution[1], solution[2], len(segments))


def _solve3(matrix: list[list[float]], rhs: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting for a 3x3 system."""
    a = [row[:] + [value] for row, value in zip(matrix, rhs, strict=True)]
    for col in range(3):
        column = [abs(a[row][col]) for row in range(3)]
        pivot = max(range(col, 3), key=column.__getitem__)
        if abs(a[pivot][col]) < 1e-9:
            return None
        a[col], a[pivot] = a[pivot], a[col]
        for row in range(col + 1, 3):
            factor = a[row][col] / a[col][col]
            a[row] = [x - factor * y for x, y in zip(a[row], a[col], strict=True)]
    solution = [0.0, 0.0, 0.0]
    for row in (2, 1, 0):
        rest = sum(a[row][k] * solution[k] for k in range(row + 1, 3))
        solution[row] = (a[row][3] - rest) / a[row][row]
    return solution


def flex_hr_net_kcal(fit: FlexHrFit, mean_hr: float, duration_s: float) -> float:
    """Net cost of a session without power from the athlete's own HR fit."""
    minutes = duration_s / 60.0
    rate = fit.intercept + fit.per_bpm * mean_hr + fit.per_min * minutes / 2.0
    return max(0.0, rate) * minutes


def step_length_m(
    walk_speeds_mps: Sequence[float],
    walk_cadences_spm: Sequence[float],
    height_m: float | None,
) -> float | None:
    """Mean step length of recorded walks (speed / cadence), else from height."""
    lengths = [
        speed * 60.0 / cadence
        for speed, cadence in zip(walk_speeds_mps, walk_cadences_spm, strict=False)
        if speed and cadence and speed > 0 and cadence > 0
    ]
    if lengths:
        return sum(lengths) / len(lengths)
    return None if not height_m else STEP_LENGTH_PER_HEIGHT * height_m


def neat_net_kcal(
    mass_kg: float,
    daily_steps: float,
    session_steps: float,
    step_m: float,
    floors: float = 0.0,
) -> float:
    """Steps outside recorded sessions at Minetti's level walking cost, plus floors.

    A floor adds its vertical work at ``UPHILL_WORK_EFFICIENCY`` on top of the
    level walking those steps already count.
    """
    steps = max(0.0, daily_steps - session_steps)
    walking = mass_kg * steps * step_m * walking_cost_j_per_kg_m(0.0)
    climbing = mass_kg * GRAVITY * FLOOR_HEIGHT_M * max(0.0, floors)
    return (walking + climbing / UPHILL_WORK_EFFICIENCY) / J_PER_KCAL


def tef_kcal(protein_g: float, carbohydrate_g: float, fat_g: float) -> float:
    """Thermic effect of food from the logged macronutrients."""
    return (
        TEF_PROTEIN * KCAL_PER_G_PROTEIN * max(0.0, protein_g)
        + TEF_CARBOHYDRATE * KCAL_PER_G_CARBOHYDRATE * max(0.0, carbohydrate_g)
        + TEF_FAT * KCAL_PER_G_FAT * max(0.0, fat_g)
    )


def epoc_kcal(net_kcal: float, above_threshold: bool) -> float:
    """Optional EPOC: a share of the net cost for sessions above the threshold."""
    return EPOC_SHARE * net_kcal if above_threshold else 0.0

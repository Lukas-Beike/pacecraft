"""Adaptive daily expenditure from intake and the weight trend (#3100).

Pure functions over one ``EnergyInputs``: the smoothed weight trend, rolling
4-week windows with the adaptive TDEE (mean logged intake minus the change in
stored energy), the one shrunken activity calibration factor, the derived
resting rate and the bias of meals flagged as uncertain. Results are bands, not
points, and a window without enough data says why instead of falling back to
a formula. The activity and TEF terms come from M11
(``backend/performance/activity_energy.py``).
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta

from backend.performance.activity_energy import tef_kcal

METHOD_ID = "adaptive-expenditure"
# Bump when a formula or constant below changes the estimate.
METHOD_VERSION = 1
# Days per window and windows per week of the rolling view (#3099, #3100).
WINDOW_DAYS = 28
# Half-life in days of the exponentially weighted weight trend.
TREND_HALF_LIFE_DAYS = 10.0
# A window needs this many complete logging days, weigh-ins and days between
# its first and last weigh-in; otherwise it gives its reason, never a formula.
MIN_COMPLETE_DAYS = 14
MIN_WEIGHINS = 8
MIN_WEIGHT_SPAN_DAYS = 21
# A day counts as complete with this many diary entries and at least this share
# of the day's own expenditure estimate (resting + own activity + TEF).
MIN_DAY_ENTRIES = 2
MIN_DAY_SHARE = 0.6
# Hall 2008 (Int J Obes 32:573): 39.5 MJ per kg fat and 7.6 MJ per kg lean
# tissue, in kcal/kg.
FAT_KCAL_PER_KG = 9441.0
LEAN_KCAL_PER_KG = 1816.0
# Forbes' curve as used by Hall 2008: the fat share of a weight change is
# FM / (FM + 10.4 kg), FM the fat mass in kg.
FORBES_CONSTANT_KG = 10.4
# Fat share of a weight change without a body-fat measurement: 0.75 gives about
# 7,500 kcal/kg, near the common 7,700 (product assumption).
DEFAULT_FAT_SHARE = 0.75
# Extra uncertainty of a flagged (estimated) meal: a share of its logged kcal.
FLAGGED_DAY_UNCERTAINTY = 0.25
# Two-sided 95 % band multiplier.
BAND_Z = 1.96
# The one activity calibration factor: prior 1 +- 0.10, clamped to 0.85-1.15,
# fitted on complete windows of the last 12 weeks (method comment, #3100).
CALIBRATION_PRIOR_SD = 0.10
CALIBRATION_BOUNDS = (0.85, 1.15)
# Windows needed before the factor moves from 1, and before a flagged-meal
# bias is estimated.
MIN_CALIBRATION_WINDOWS = 4
MIN_BIAS_WINDOWS = 4


@dataclass(frozen=True)
class Estimate:
    kcal: float
    band_kcal: float  # half-width of the shown band


@dataclass(frozen=True)
class DayInputs:
    """One day: logged intake, its macros and flags, weigh-in and own activity."""

    intake_kcal: float | None = None
    protein_g: float = 0.0
    carbohydrate_g: float = 0.0
    fat_g: float = 0.0
    entries: int = 0
    flagged: bool = False
    # Logged kcal of the day's flagged (estimated) entries (#3187 item 13).
    flagged_kcal: float = 0.0
    weight_kg: float | None = None
    activity_kcal: float | None = None  # M11 session net cost + NEAT, uncalibrated


@dataclass(frozen=True)
class EnergyInputs:
    days: Mapping[date, DayInputs]
    body_fat_pct: Mapping[date, float]  # skinfold measurements by date
    # Resting reference for the net terms and the completeness rule: own
    # Katch-McArdle from skinfold lean mass first, Garmin only as the last
    # fallback (orchestrator, 2026-11-15); the source is shown with it.
    rmr_kcal: float | None
    rmr_source: str | None = None  # 'katch_mcardle' | 'garmin' | None


@dataclass(frozen=True)
class EnergyWindow:
    end: date
    days: int
    complete_days: int
    flagged_days: int
    weighins: int
    activity_kcal: float | None
    activity_kcal_raw: float | None
    tef_kcal: float | None
    # All complete days count; flagged entries widen the band (#3187 item 14).
    adaptive_tdee: Estimate | None
    # Only from a window without any flagged day, else None.
    adaptive_tdee_unflagged: Estimate | None
    derived_rmr: Estimate | None
    insufficient: str | None
    rmr_source: str | None = None
    # Windows the calibration fit used; below MIN_CALIBRATION_WINDOWS the
    # factor is still the prior 1 and the display says so.
    calibration_windows: int = 0
    # Flagged entries' logged kcal per complete day of the window.
    flagged_kcal_per_day: float = 0.0
    # With the learned flagged-meal bias b: flagged kcal x (1 + b), labelled;
    # None until the bias is estimated. Logged entries are never rewritten.
    adaptive_tdee_corrected: Estimate | None = None


def weight_trend(weights_kg: Mapping[date, float]) -> dict[date, float]:
    """Exponentially weighted weight trend per day from the first weigh-in on.

    Each day decays the previous trend by the half-life; a weigh-in pulls it
    towards the measured value. Days without a weigh-in carry the trend.
    """
    if not weights_kg:
        return {}
    alpha = 1.0 - 0.5 ** (1.0 / TREND_HALF_LIFE_DAYS)
    first, last = min(weights_kg), max(weights_kg)
    trend: dict[date, float] = {}
    value = weights_kg[first]
    day = first
    while day <= last:
        if day in weights_kg:
            value += alpha * (weights_kg[day] - value)
        trend[day] = value
        day += timedelta(days=1)
    return trend


def fat_share(weight_kg: float, body_fat_pct: float | None) -> float:
    """Fat share of a weight change: Forbes' curve, else the default."""
    if body_fat_pct is None:
        return DEFAULT_FAT_SHARE
    fat_mass = weight_kg * body_fat_pct / 100.0
    return fat_mass / (fat_mass + FORBES_CONSTANT_KG)


def energy_density(weight_kg: float, body_fat_pct: float | None) -> float:
    """kcal per kg of weight change for the current body composition (Hall 2008)."""
    share = fat_share(weight_kg, body_fat_pct)
    return share * FAT_KCAL_PER_KG + (1.0 - share) * LEAN_KCAL_PER_KG


def day_tef(day: DayInputs) -> float:
    return tef_kcal(day.protein_g, day.carbohydrate_g, day.fat_g)


def is_complete(day: DayInputs, rmr_kcal: float | None) -> bool:
    """Enough entries and intake for the day's own expenditure estimate."""
    if day.intake_kcal is None or day.entries < MIN_DAY_ENTRIES:
        return False
    if rmr_kcal is None or day.activity_kcal is None:
        return True
    expected = rmr_kcal + day.activity_kcal + day_tef(day)
    return day.intake_kcal >= MIN_DAY_SHARE * expected


def _latest_body_fat(inputs: EnergyInputs, day: date) -> float | None:
    measured = [d for d in inputs.body_fat_pct if d <= day]
    return inputs.body_fat_pct[max(measured)] if measured else None


def _tdee(
    intakes: Sequence[tuple[float, float]], stored_per_day: float, store_sd: float
) -> Estimate | None:
    """Mean intake minus stored energy per day, with its 95 % band.

    Each intake is (day kcal, its flagged entries' kcal); the flagged part
    widens the band by ``FLAGGED_DAY_UNCERTAINTY`` of itself.
    """
    if not intakes:
        return None
    values = [kcal for kcal, _ in intakes]
    mean = statistics.fmean(values)
    spread = statistics.stdev(values) if len(values) > 1 else 0.0
    flagged_var = sum((FLAGGED_DAY_UNCERTAINTY * k) ** 2 for _, k in intakes)
    intake_var = spread**2 / len(values) + flagged_var / len(values) ** 2
    band = BAND_Z * math.sqrt(intake_var + store_sd**2)
    return Estimate(mean - stored_per_day, band)


def _insufficient(complete_days: int, weighins: Sequence[date]) -> str | None:
    """Why a window has no estimate, or None when its basis is enough."""
    if complete_days < MIN_COMPLETE_DAYS:
        return "intake_days"
    if len(weighins) < MIN_WEIGHINS:
        return "weighins"
    if (weighins[-1] - weighins[0]).days < MIN_WEIGHT_SPAN_DAYS:
        return "weight_span"
    return None


def _stored_energy(
    inputs: EnergyInputs, trend: Mapping[date, float], weights: Mapping[date, float]
) -> tuple[float, float]:
    """Stored energy per day over the weigh-ins, and its standard error.

    The least-squares slope of the window's own weigh-ins, at the energy
    density of the body composition at the last one; the error is the slope's
    standard error. Unlike the trend's change it has no warm-up lag after the
    first weigh-in (Gitar on #3123).
    """
    weighins = sorted(weights)
    last = weighins[-1]
    density = energy_density(trend[last], _latest_body_fat(inputs, last))
    xs = [float((d - weighins[0]).days) for d in weighins]
    ys = [weights[d] for d in weighins]
    mean_x, mean_y = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mean_x) ** 2 for x in xs)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / sxx
    residuals = [
        y - (mean_y + slope * (x - mean_x)) for x, y in zip(xs, ys, strict=True)
    ]
    slope_se = math.sqrt(sum(r * r for r in residuals) / max(1, len(xs) - 2) / sxx)
    return density * slope, density * slope_se


def _window(
    inputs: EnergyInputs,
    trend: Mapping[date, float],
    end: date,
    factor: float,
    calibration_windows: int = 0,
) -> EnergyWindow:
    start = end - timedelta(days=WINDOW_DAYS - 1)
    span = [start + timedelta(days=n) for n in range(WINDOW_DAYS)]
    days = {d: inputs.days[d] for d in span if d in inputs.days}
    complete = {d: v for d, v in days.items() if is_complete(v, inputs.rmr_kcal)}
    flagged = sum(1 for v in complete.values() if v.flagged)
    weights = {d: v.weight_kg for d, v in days.items() if v.weight_kg is not None}
    weighins = sorted(weights)
    activity = [v.activity_kcal for v in days.values() if v.activity_kcal is not None]
    raw = statistics.fmean(activity) if activity else None
    tef = (
        statistics.fmean([day_tef(v) for v in complete.values()]) if complete else None
    )
    reason = _insufficient(len(complete), weighins)
    tdee = unflagged = derived = None
    if reason is None:
        stored, store_sd = _stored_energy(inputs, trend, weights)
        everything = [
            (v.intake_kcal or 0.0, _flagged_kcal(v)) for v in complete.values()
        ]
        tdee = _tdee(everything, stored, store_sd)
        unflagged = tdee if flagged == 0 else None
        if tdee is not None and raw is not None and tef is not None:
            rmr = tdee.kcal - raw * factor - tef
            derived = Estimate(rmr, math.hypot(tdee.band_kcal, 0.1 * raw * factor))
    return EnergyWindow(
        end=end,
        days=WINDOW_DAYS,
        complete_days=len(complete),
        flagged_days=flagged,
        weighins=len(weighins),
        activity_kcal=None if raw is None else raw * factor,
        activity_kcal_raw=raw,
        tef_kcal=tef,
        adaptive_tdee=tdee,
        adaptive_tdee_unflagged=unflagged,
        derived_rmr=derived,
        insufficient=reason,
        rmr_source=inputs.rmr_source,
        calibration_windows=calibration_windows,
        flagged_kcal_per_day=statistics.fmean(
            [_flagged_kcal(v) for v in complete.values()]
        )
        if complete
        else 0.0,
    )


def _flagged_kcal(day: DayInputs) -> float:
    """The flagged entries' logged kcal; a flagged day without them counts whole."""
    if day.flagged_kcal > 0 or not day.flagged:
        return day.flagged_kcal
    return day.intake_kcal or 0.0


def _calibration_points(windows: Sequence[EnergyWindow]) -> list[tuple[float, float]]:
    """(raw activity, TDEE - TEF) of every window with an estimate."""
    return [
        (w.activity_kcal_raw, w.adaptive_tdee.kcal - w.tef_kcal)
        for w in windows
        if w.adaptive_tdee is not None
        and w.activity_kcal_raw is not None
        and w.tef_kcal is not None
    ]


def calibration_factor(windows: Sequence[EnergyWindow]) -> float:
    """The one activity factor: slope of (TDEE - TEF) on activity, shrunk to 1.

    Ordinary least squares over the windows with a basis gives the slope and
    its variance; the posterior mean with the prior 1 +- 0.10 is clamped to
    0.85-1.15. Without enough windows, or without spread in activity, it is 1.
    """
    points = _calibration_points(windows)
    if len(points) < MIN_CALIBRATION_WINDOWS:
        return 1.0
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    mean_x, mean_y = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx <= 0:
        return 1.0
    slope = sum((x - mean_x) * (y - mean_y) for x, y in points) / sxx
    residual = [y - (mean_y + slope * (x - mean_x)) for x, y in points]
    dof = max(1, len(points) - 2)
    slope_var = sum(r * r for r in residual) / dof / sxx
    prior_precision = 1.0 / CALIBRATION_PRIOR_SD**2
    if slope_var <= 0:
        posterior = slope
    else:
        precision = 1.0 / slope_var
        posterior = (slope * precision + prior_precision) / (
            precision + prior_precision
        )
    low, high = CALIBRATION_BOUNDS
    return max(low, min(high, posterior))


def energy_windows(
    inputs: EnergyInputs, end: date, weeks: int = 12
) -> tuple[EnergyWindow, ...]:
    """One window per week ending on ``end``, ``end`` - 7, ...; oldest first.

    It works on whatever history ``inputs`` holds, so logged days from before
    the first read count at once; older windows without a basis give reasons.
    """
    weights = {
        d: v.weight_kg for d, v in inputs.days.items() if v.weight_kg is not None
    }
    trend = weight_trend(weights)
    ends = [end - timedelta(days=7 * n) for n in range(weeks)][::-1]
    uncalibrated = [_window(inputs, trend, e, 1.0) for e in ends]
    factor = calibration_factor(uncalibrated)
    used = len(_calibration_points(uncalibrated))
    windows = [_window(inputs, trend, e, factor, used) for e in ends]
    bias = _bias(windows)
    if bias is None:
        return tuple(windows)
    return tuple(_corrected(window, *bias) for window in windows)


def _corrected(window: EnergyWindow, bias: float, band: float) -> EnergyWindow:
    """The window with flagged kcal counted at (1 + b); its band adds b's."""
    tdee = window.adaptive_tdee
    if tdee is None:
        return window
    extra = window.flagged_kcal_per_day
    corrected = Estimate(
        tdee.kcal + bias * extra, math.hypot(tdee.band_kcal, band * extra)
    )
    return replace(window, adaptive_tdee_corrected=corrected)


def history_start(end: date, weeks: int = 12) -> date:
    """The first day the oldest of ``weeks`` windows ending on ``end`` reads."""
    return end - timedelta(days=7 * (weeks - 1) + WINDOW_DAYS - 1)


def flagged_meal_bias(inputs: EnergyInputs, end: date) -> tuple[float, float] | None:
    """Mean misestimate of flagged entries, in % of their logged kcal, with its band."""
    bias = _bias(energy_windows(inputs, end))
    return None if bias is None else (bias[0] * 100.0, bias[1] * 100.0)


def _bias(windows: Sequence[EnergyWindow]) -> tuple[float, float] | None:
    """The flagged-entry bias b and its band, as fractions (#3187 items 13, 14).

    Across the windows, the adaptive TDEE is regressed on the flagged entries'
    logged kcal per day x: TDEE = T - b x. If flagged meals were in fact
    larger than logged by b, more flagged intake shows as a lower
    expenditure, so b = -slope; positive b means they were larger.
    """
    points = [
        (w.flagged_kcal_per_day, w.adaptive_tdee.kcal)
        for w in windows
        if w.adaptive_tdee is not None
    ]
    usable = [p for p in points if p[0] > 0]
    if len(usable) < MIN_BIAS_WINDOWS or len({round(x, 6) for x, _ in points}) < 3:
        return None
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    mean_x, mean_y = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx <= 0:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in points) / sxx
    residual = [y - (mean_y + slope * (x - mean_x)) for x, y in points]
    slope_sd = math.sqrt(sum(r * r for r in residual) / max(1, len(points) - 2) / sxx)
    return -slope, BAND_Z * slope_sd

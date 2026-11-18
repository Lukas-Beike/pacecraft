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
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any, cast

from backend.performance.activity_energy import tef_kcal

METHOD_ID = "adaptive-expenditure"
# Bump when a formula or constant below changes the estimate.
METHOD_VERSION = 3
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
# Two-sided 95 % Student t quantiles by degrees of freedom (standard table);
# a window's band uses the smaller of its intake (days - 1) and weight-slope
# (weigh-ins - 2) degrees of freedom, BAND_Z beyond the table (#3187 item 10).
T_QUANTILE_95 = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
    8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160,
    14: 2.145, 15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093,
    20: 2.086, 21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060,
    26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042, 40: 2.021,
    60: 2.000, 120: 1.980,
}  # fmt: skip
# Uncertainty of the fat share of a weight change (SD), so the energy density
# enters the band: a share between 0.5 and 1.0 taken as uniform, 0.5 / sqrt(12)
# (product assumption, #3187 item 10).
FAT_SHARE_SD = 0.144
# Precision of one body fat measurement by the same method (percentage points,
# test-retest), the band of a measured fat-mass change (#3477).
MEASURED_FAT_SD_PCT = 1.0
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
    # Its parts (split A, #3100): NEAT net of rest, and the day's priced
    # sessions (0.0 without any); None where unknown, never 0 (#3187).
    neat_kcal: float | None = None
    sport_kcal: float | None = None
    # NEAT's inputs, shown in the split's basis (#3255): the device's day
    # steps, the steps its sessions take out, and the floors outside them.
    steps: float | None = None
    session_steps: float = 0.0
    floors: float | None = None


@dataclass(frozen=True)
class EnergyInputs:
    days: Mapping[date, DayInputs]
    body_fat_pct: Mapping[date, float]  # skinfold measurements by date
    # Resting reference for the net terms and the completeness rule: own
    # Katch-McArdle from skinfold lean mass first, Garmin only as the last
    # fallback (orchestrator, 2026-11-15); the source is shown with it.
    rmr_kcal: float | None
    rmr_source: str | None = None  # 'katch_mcardle' | 'garmin' | None
    # NEAT's step length and where it comes from: 'walks' or 'height' (#3255).
    step_m: float | None = None
    step_source: str | None = None
    # The formula rows' body fat without a skinfold (#3362): a device's or
    # scale's per day with its provider, then the profile's.
    device_body_fat: Mapping[date, tuple[float, Any]] = field(default_factory=dict)
    profile_body_fat_pct: float | None = None


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
    # Split A (#3100), kcal/day over the same days as activity_kcal_raw, so
    # neat + sport is the activity; None when any of those days lacks a part.
    neat_kcal_raw: float | None = None
    sport_kcal_raw: float | None = None
    # The same parts times the calibration factor k, like activity_kcal.
    neat_kcal: float | None = None
    sport_kcal: float | None = None
    # The k applied to this window's activity (1.0 while it stays the prior).
    calibration_factor: float = 1.0
    # How the stored energy split fat and lean mass: "measured" from two dated
    # body fat measurements, else "forbes"; None without an estimate (#3477).
    store_method: str | None = None


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


def t_quantile(dof: int) -> float:
    """The 95 % two-sided t quantile, from the next smaller tabled dof."""
    tabled = [d for d in T_QUANTILE_95 if d <= max(1, dof)]
    return BAND_Z if dof > max(T_QUANTILE_95) else T_QUANTILE_95[max(tabled)]


def _tdee(
    intakes: Sequence[tuple[float, float]],
    stored_per_day: float,
    store_sd: float,
    weighins: int = 3,
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
    dof = min(len(values) - 1, weighins - 2)
    band = t_quantile(dof) * math.sqrt(intake_var + store_sd**2)
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


def _fat_series(inputs: EnergyInputs) -> list[dict[date, float]]:
    """The dated body fat measurements per method: skinfold, entered, device."""
    entered = {d: v for d, (v, p) in inputs.device_body_fat.items() if p is None}
    device = {d: v for d, (v, p) in inputs.device_body_fat.items() if p is not None}
    return [dict(inputs.body_fat_pct), entered, device]


def _measured_store(
    inputs: EnergyInputs, weights: Mapping[date, float], start: date, end: date
) -> tuple[float, float] | None:
    """Stored energy per day from the measured fat-mass change, and its error.

    The first method with two dated measurements in the window gives the fat
    mass (weight x body fat, the weigh-in nearest each day) at its first and
    last one; lean mass is the rest of the weight change (athlete decision,
    #3477). Methods are never mixed. None without two measurements.
    """
    for series in _fat_series(inputs):
        days = sorted(d for d in series if start <= d <= end)
        if len(days) >= 2:
            break
    else:
        return None

    def masses(day: date) -> tuple[float, float]:
        nearest = min(weights, key=lambda w: (abs((w - day).days), w))
        return weights[nearest], weights[nearest] * series[day] / 100.0

    (weight0, fat0), (weight1, fat1) = masses(days[0]), masses(days[-1])
    span = (days[-1] - days[0]).days
    fat_change = fat1 - fat0
    lean_change = (weight1 - weight0) - fat_change
    stored = (fat_change * FAT_KCAL_PER_KG + lean_change * LEAN_KCAL_PER_KG) / span
    fat_sd = math.hypot(weight0, weight1) * MEASURED_FAT_SD_PCT / 100.0
    return stored, fat_sd * (FAT_KCAL_PER_KG - LEAN_KCAL_PER_KG) / span


def _stored_energy(
    inputs: EnergyInputs,
    trend: Mapping[date, float],
    weights: Mapping[date, float],
    start: date,
    end: date,
) -> tuple[float, float, str]:
    """Stored energy per day over the weigh-ins, its standard error and how fat
    and lean mass were split: measured from two dated body fat measurements
    (#3477), else Forbes.

    Forbes: the least-squares slope of the window's own weigh-ins, at the
    energy density of the body composition at the last one. The error combines
    the slope's standard error with the density's (``FAT_SHARE_SD``). Unlike
    the trend's change the slope has no warm-up lag after the first weigh-in.
    """
    if measured := _measured_store(inputs, weights, start, end):
        return (*measured, "measured")
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
    density_sd = FAT_SHARE_SD * (FAT_KCAL_PER_KG - LEAN_KCAL_PER_KG)
    stored_sd = math.hypot(density * slope_se, slope * density_sd)
    return density * slope, stored_sd, "forbes"


def _window(
    inputs: EnergyInputs,
    trend: Mapping[date, float],
    end: date,
    factor: float,
    calibration_windows: int = 0,
    length: int = WINDOW_DAYS,
) -> EnergyWindow:
    start = end - timedelta(days=length - 1)
    span = [start + timedelta(days=n) for n in range(length)]
    days = {d: inputs.days[d] for d in span if d in inputs.days}
    complete = {d: v for d, v in days.items() if is_complete(v, inputs.rmr_kcal)}
    flagged = sum(1 for v in complete.values() if v.flagged)
    weights = {d: v.weight_kg for d, v in days.items() if v.weight_kg is not None}
    weighins = sorted(weights)
    active = [v for v in days.values() if v.activity_kcal is not None]
    raw = statistics.fmean(v.activity_kcal or 0.0 for v in active) if active else None
    neat_raw, sport_raw = _split(active)
    tef = (
        statistics.fmean([day_tef(v) for v in complete.values()]) if complete else None
    )
    reason = _insufficient(len(complete), weighins)
    tdee = unflagged = derived = None
    method = None
    if reason is None:
        stored, store_sd, method = _stored_energy(inputs, trend, weights, start, end)
        everything = [(v.intake_kcal or 0.0, v.flagged_kcal) for v in complete.values()]
        tdee = _tdee(everything, stored, store_sd, len(weighins))
        unflagged = tdee if flagged == 0 else None
        if tdee is not None and raw is not None and tef is not None:
            rmr = tdee.kcal - raw * factor - tef
            derived = Estimate(rmr, math.hypot(tdee.band_kcal, 0.1 * raw * factor))
    return EnergyWindow(
        end=end,
        days=length,
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
        neat_kcal_raw=neat_raw,
        sport_kcal_raw=sport_raw,
        neat_kcal=None if neat_raw is None else neat_raw * factor,
        sport_kcal=None if sport_raw is None else sport_raw * factor,
        calibration_factor=factor,
        store_method=method,
        flagged_kcal_per_day=statistics.fmean(
            [v.flagged_kcal for v in complete.values()]
        )
        if complete
        else 0.0,
    )


def period_window(
    inputs: EnergyInputs, end: date, days: int, factor: float
) -> EnergyWindow:
    """One window of ``days`` ending on ``end``, by the same rules and calibration.

    The fit scale's periods (#3397); a period too short for the rules has no
    own estimate, as any window.
    """
    weights = {
        d: v.weight_kg for d, v in inputs.days.items() if v.weight_kg is not None
    }
    return _window(inputs, weight_trend(weights), end, factor, length=days)


def _split(active: Sequence[DayInputs]) -> tuple[float | None, float | None]:
    """Mean NEAT and session kcal over the days with a known activity.

    Both stay None unless every one of those days has both parts, so the two
    always add up to the window's activity (split A, #3100).
    """
    neat = [v.neat_kcal for v in active]
    sport = [v.sport_kcal for v in active]
    if not active or None in neat or None in sport:
        return None, None
    return (
        statistics.fmean(n for n in neat if n is not None),
        statistics.fmean(s for s in sport if s is not None),
    )


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
    return cast(EnergyWindow, replace(window, adaptive_tdee_corrected=corrected))


def history_start(end: date, weeks: int = 12) -> date:
    """The first day the oldest of ``weeks`` windows ending on ``end`` reads."""
    return end - timedelta(days=7 * (weeks - 1) + WINDOW_DAYS - 1)


def flagged_meal_bias(inputs: EnergyInputs, end: date) -> tuple[float, float] | None:
    """Mean misestimate of flagged entries, in % of their logged kcal, with its band."""
    return windows_bias(energy_windows(inputs, end))


def windows_bias(windows: Sequence[EnergyWindow]) -> tuple[float, float] | None:
    """The flagged-entry bias in % with its band, from windows already built."""
    bias = _bias(windows)
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

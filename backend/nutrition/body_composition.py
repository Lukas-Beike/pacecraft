"""Resting and daily energy expenditure estimated from the athlete's own data (#3097).

Pure functions, no storage:

- Resting energy from lean mass: 3-site skinfolds give body density (Jackson &
  Pollock 1978 for men, Jackson, Pollock & Ward 1980 for women), the Siri (1961)
  equation turns it into body fat, and Katch-McArdle gives resting energy from the
  lean mass that remains.
- Adaptive daily expenditure: mean logged intake minus the energy stored or
  released by the weight trend over the window, the weight trend being the
  least-squares line through the weigh-ins. Each estimate carries its basis and
  a confidence band, or None with the reason the basis is too small; it never
  falls back to a population formula.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Final


class Sex(StrEnum):
    MALE = "male"
    FEMALE = "female"


# Jackson & Pollock (1978), men: chest, abdomen, thigh. Jackson, Pollock & Ward
# (1980), women: triceps, suprailiac, thigh. Density = a - b*S + c*S^2 - d*age.
SKINFOLD_SITES: Final = {
    Sex.MALE: ("chest", "abdomen", "thigh"),
    Sex.FEMALE: ("triceps", "suprailiac", "thigh"),
}
_DENSITY_COEFFICIENTS: Final = {
    Sex.MALE: (1.10938, 0.0008267, 0.0000016, 0.0002574),
    Sex.FEMALE: (1.0994921, 0.0009929, 0.0000023, 0.0001392),
}
# A fold outside this range is a typing error, not a measurement (mm).
SKINFOLD_BOUNDS_MM: Final = (2.0, 80.0)

# Katch-McArdle: resting energy (kcal/day) = 370 + 21.6 * lean mass (kg).
KATCH_MCARDLE_INTERCEPT: Final = 370.0
KATCH_MCARDLE_PER_KG: Final = 21.6

# Energy per kg of body-mass change: 3500 kcal/lb (Wishnofsky 1958) = 7700 kcal/kg,
# the conventional mixed-tissue value; Hall (2008) shows it varies with the tissue lost.
ENERGY_PER_KG: Final = 7700.0
WINDOW_DAYS: Final = 28
# The basis an estimate needs: complete intake days and weigh-ins in the window,
# and weigh-ins spread over at least this many days.
MIN_INTAKE_DAYS: Final = 14
MIN_WEIGHINS: Final = 8
MIN_WEIGHT_SPAN_DAYS: Final = 14
# Two standard errors: about a 95 % band.
BAND_STANDARD_ERRORS: Final = 1.96


def body_density(sex: Sex, folds_mm: Mapping[str, float], age_years: int) -> float:
    """Body density (g/cm³) from the three sex-specific skinfold sites."""
    sites = SKINFOLD_SITES[sex]
    if set(folds_mm) != set(sites):
        raise ValueError(f"skinfolds need exactly the sites {', '.join(sites)}")
    lower, upper = SKINFOLD_BOUNDS_MM
    if not all(lower <= folds_mm[site] <= upper for site in sites):
        raise ValueError("a skinfold lies outside 2-80 mm")
    if not 18 <= age_years <= 80:
        raise ValueError("the skinfold equations hold for ages 18-80")
    total = sum(folds_mm[site] for site in sites)
    a, b, c, d = _DENSITY_COEFFICIENTS[sex]
    return a - b * total + c * total**2 - d * age_years


def siri_body_fat_pct(density: float) -> float:
    """Body fat (%) from body density: 495 / density - 450 (Siri 1961)."""
    return 495.0 / density - 450.0


def katch_mcardle_rmr(weight_kg: float, body_fat_pct: float) -> float:
    """Resting energy (kcal/day) from the lean mass of weight and body fat."""
    if not 0 < body_fat_pct < 70:
        raise ValueError("body fat must lie between 0 and 70 %")
    lean_mass = weight_kg * (1 - body_fat_pct / 100)
    return KATCH_MCARDLE_INTERCEPT + KATCH_MCARDLE_PER_KG * lean_mass


class Insufficient(StrEnum):
    """Why an adaptive expenditure estimate cannot be given."""

    INTAKE_DAYS = "intake_days"
    WEIGHINS = "weighins"
    WEIGHT_SPAN = "weight_span"


@dataclass(frozen=True, slots=True)
class AdaptiveExpenditure:
    """Daily expenditure estimate with its basis; ``kcal`` is None without one."""

    kcal: float | None
    band_kcal: float | None
    intake_days: int
    weighins: int
    window_days: int
    mean_intake_kcal: float | None
    weight_change_kg_per_week: float | None
    insufficient: Insufficient | None


def _weight_slope(
    weights: list[tuple[int, float]],
) -> tuple[float, float]:
    """Least-squares slope (kg/day) through the weigh-ins and its standard error."""
    n = len(weights)
    mean_x = sum(x for x, _ in weights) / n
    mean_y = sum(y for _, y in weights) / n
    sxx = sum((x - mean_x) ** 2 for x, _ in weights)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in weights) / sxx
    residuals = sum((y - (mean_y + slope * (x - mean_x))) ** 2 for x, y in weights)
    return slope, math.sqrt(residuals / (n - 2) / sxx)


def adaptive_expenditure(
    intake_kcal: Mapping[date, float],
    weights_kg: Mapping[date, float],
    end: date,
    window_days: int = WINDOW_DAYS,
) -> AdaptiveExpenditure:
    """Expenditure over the ``window_days`` ending on ``end`` (inclusive).

    ``intake_kcal`` holds only days the caller counts as completely logged;
    ``weights_kg`` one weigh-in per day. Days outside the window are ignored.
    """
    first = end.toordinal() - window_days + 1
    intake = [
        kcal
        for day, kcal in intake_kcal.items()
        if first <= day.toordinal() <= end.toordinal()
    ]
    weights = sorted(
        (day.toordinal() - first, kg)
        for day, kg in weights_kg.items()
        if first <= day.toordinal() <= end.toordinal()
    )
    reason = _insufficient(intake, weights)
    if reason is not None:
        return AdaptiveExpenditure(
            None, None, len(intake), len(weights), window_days, None, None, reason
        )
    mean_intake = sum(intake) / len(intake)
    intake_se = math.sqrt(
        sum((kcal - mean_intake) ** 2 for kcal in intake)
        / (len(intake) - 1)
        / len(intake)
    )
    slope, slope_se = _weight_slope(weights)
    stored = slope * ENERGY_PER_KG
    band = BAND_STANDARD_ERRORS * math.hypot(intake_se, slope_se * ENERGY_PER_KG)
    return AdaptiveExpenditure(
        kcal=mean_intake - stored,
        band_kcal=band,
        intake_days=len(intake),
        weighins=len(weights),
        window_days=window_days,
        mean_intake_kcal=mean_intake,
        weight_change_kg_per_week=slope * 7,
        insufficient=None,
    )


def _insufficient(
    intake: list[float], weights: list[tuple[int, float]]
) -> Insufficient | None:
    if len(intake) < MIN_INTAKE_DAYS:
        return Insufficient.INTAKE_DAYS
    if len(weights) < MIN_WEIGHINS:
        return Insufficient.WEIGHINS
    if weights[-1][0] - weights[0][0] < MIN_WEIGHT_SPAN_DAYS:
        return Insufficient.WEIGHT_SPAN
    return None

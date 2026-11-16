"""Body fat from 3-site skinfolds and resting energy from lean mass (#3099).

Pure functions, no storage. 3-site skinfolds give body density (Jackson &
Pollock 1978 for men, Jackson, Pollock & Ward 1980 for women); the Siri (1961)
equation turns density into body fat; lean mass is weight times the fat-free
share. Resting energy from lean mass: Katch-McArdle (370 + 21.6 x LBM, the
general equation of Cunningham 1991) and Cunningham 1980 (500 + 22 x LBM). Each
is a formula estimate, shown beside Garmin's, never in its place. Card M13 in
docs/performance-methods documents the sources and limits.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Final

METHOD_ID: Final = "body-composition-resting"
# Bump with the M13 card version when a formula or constant below changes.
METHOD_VERSION: Final = 1


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
# Accepted ages. The equations were fitted on men aged 18-61 and women aged 18-55;
# the methods document names that range.
SKINFOLD_AGES: Final = (18, 80)


class RestingFormula(StrEnum):
    """Resting energy (kcal/day) = intercept + per_kg x lean mass (kg)."""

    KATCH_MCARDLE = "katch_mcardle"  # 370 + 21.6 x LBM (Cunningham 1991)
    CUNNINGHAM = (
        "cunningham"  # Cunningham 1980: 500 + 22 x LBM, the abstract's rounding
    )


RESTING_COEFFICIENTS: Final = {
    RestingFormula.KATCH_MCARDLE: (370.0, 21.6),
    RestingFormula.CUNNINGHAM: (500.0, 22.0),
}


def body_density(sex: Sex, folds_mm: Mapping[str, float], age_years: int) -> float:
    """Body density (g/cm³) from the three sex-specific skinfold sites."""
    sites = SKINFOLD_SITES[sex]
    if set(folds_mm) != set(sites):
        raise ValueError(f"skinfolds need exactly the sites {', '.join(sites)}")
    lower, upper = SKINFOLD_BOUNDS_MM
    if not all(lower <= folds_mm[site] <= upper for site in sites):
        raise ValueError("a skinfold lies outside 2-80 mm")
    youngest, oldest = SKINFOLD_AGES
    if not youngest <= age_years <= oldest:
        raise ValueError(f"the skinfold equations hold for ages {youngest}-{oldest}")
    total = sum(folds_mm[site] for site in sites)
    a, b, c, d = _DENSITY_COEFFICIENTS[sex]
    return a - b * total + c * total**2 - d * age_years


def siri_body_fat_pct(density: float) -> float:
    """Body fat (%) from body density: 495 / density - 450 (Siri 1961)."""
    return 495.0 / density - 450.0


def lean_mass_kg(weight_kg: float, body_fat_pct: float) -> float:
    """Fat-free mass: weight times the share that is not fat."""
    if not 0 < body_fat_pct < 70:
        raise ValueError("body fat must lie between 0 and 70 %")
    return weight_kg * (1 - body_fat_pct / 100)


def resting_energy(formula: RestingFormula, lean_mass: float) -> float:
    """Resting energy (kcal/day) from lean mass by the named formula."""
    intercept, per_kg = RESTING_COEFFICIENTS[formula]
    return intercept + per_kg * lean_mass

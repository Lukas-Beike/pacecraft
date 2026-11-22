# M13 — Body fat from 3-site skinfolds and resting energy from lean mass

- **Card:** M13
- **Card version:** 5
- **Method ID:** body-composition-resting v5 (#3099, #3551, #3617)
- **Status:** method implemented (`backend/athlete/body_composition.py`, `backend/athlete/skinfolds.py`); the energy table that shows it is in review (#3180)
- **Classification:** model estimate
- **Literature checked:** 2026-11-16
- **Review:** blocked

## Target construct

- The athlete's body fat (%), from their own 3-site skinfold measurement.
- Their fat-free (lean) mass.
- Two formula estimates of resting energy (kcal/day) from that lean mass.

These stand beside Garmin's resting value and the derived resting rate of M12 as separate rows. The display compares them with the adaptive expenditure; it never replaces one with another.

## Formula and units

1. **Body density (g/cm³)** from the sum `S` (mm) of three folds and the age `A` (years) in the measurement's year:
   - men (chest, abdomen, thigh; R69): `1.10938 − 0.0008267 S + 0.0000016 S² − 0.0002574 A`;
   - women (triceps, suprailiac, thigh; R70): `1.0994921 − 0.0009929 S + 0.0000023 S² − 0.0001392 A`.
2. **Body fat (%)** from density `D` (R71, Siri): `495 / D − 450`.
3. **Lean mass (kg):** `weight × (1 − body fat / 100)`.
   - In the energy table, the weight is M12's current weight on the window's last day: the least-squares line through the last 28 days' weigh-ins at the latest one, so a steady change has no lag (#3551). The resting reference uses the same weight.
   - The lean mass is carried from the reading: its weight × (1 − body fat) plus (1 − M12's blended fat share) × the weight change since, not the reading's percentage on that day's weight (#3617). A profile value without a date applies to that day's weight. The own resting reference (Katch-McArdle) takes the same carried lean mass, and the reader takes the weights back to the oldest reading it can resolve; a reading before the first weigh-in takes the first weigh-in after it (Gitar on #3642).
   - The body fat is from the latest skinfold measurement within 90 days before that day; every measurement of the read span counts, also two within one week (#3617).
4. **Resting energy (kcal/day):**
   - Katch-McArdle: `370 + 21.6 × LBM`. This is the general equation of Cunningham 1991 (R73).
   - Cunningham: `500 + 22 × LBM`, as proposed in the abstract of R72. Its regression in the results section is `501.6 + 21.6 × LBM`.
5. **Fixed gap between the two rows:** Katch-McArdle is numerically Cunningham 1991, so the two rows always differ by `(500 + 22 L) − (370 + 21.6 L) = 130 + 0.4 L` kcal/day. That is about 146 kcal at 40 kg and 162 kcal at 80 kg of lean mass (about 155 at 62 kg). Which of them fits better therefore decides only this offset, not two independent methods (#3187 item 9).
6. **Band of a formula's resting energy:** `√((0.10 × resting)² + (1.96 × slope × weight × 3.5 / 100)²)`. The first term is the formula's own individual error (R72), taken as a half-width as the source states it; R72 gives it as a typical individual error, about one standard deviation, so this term covers about 68 %, not 95 % (#3617). The second term is a 95 % half-width. The second carries the skinfold body fat's standard error (3.5 percentage points) through the lean mass. The band of the daily total adds the day's M11 activity error (each session by its method's share, the everyday energy by the step length's 15 %) and the TEF error of 80 kcal/day in quadrature, as M12 does for the derived resting rate (#3552).
7. **Daily total of a formula row:** the formula's resting energy plus the same uncalibrated M11 own activity and TEF for both rows. The two rows therefore differ only in the resting formula.

## Sources

- **R69** (Jackson & Pollock 1978): the men's 3-site equation.
- **R70** (Jackson, Pollock & Ward 1980): the women's 3-site equation.
- **R71** (Siri 1961): density to body fat.
- **R72** (Cunningham 1980): BMR from lean mass. Full text read.
- **R73** (Cunningham 1991): the general REE equation, which is the formula known as Katch-McArdle.
- **R78** (Loucks, Kiens & Wright 2011) and **R79** (Mountjoy et al. 2023): energy availability and its low state; the lean mass is this card's (#3917). The 30 kcal/kg FFM line is the athlete's decision; the abstracts read name no cut-off.
- Athlete decisions on #3099:
  - one value per site;
  - a birth year in the profile;
  - the rows Katch-McArdle, Cunningham and Garmin side by side.
- Corrections from the citation check on #3099 (2026-11-15):
  - Cunningham is cited as 1980, not 1991.
  - The 1991 paper (R73) is the source of `370 + 21.6`. The name "Katch-McArdle" comes from the McArdle, Katch & Katch textbooks; that attribution was not checked against a textbook.
  - Byrne 2005 and Hiilloskorpi 2003 are not sources for this card.

## Population and reference measurement

- **R69:** samples of 308 and 95 adult men aged 18 to 61, against hydrostatic density.
- **R70:** adult women, against hydrostatic density.
- **R71:** a two-compartment model with fixed densities of fat and fat-free tissue.
- **R72:** a reanalysis of 223 untrained adults (120 men, 103 women) from Harris and Benedict 1919; 16 trained athletes were excluded. Lean mass there was not measured but estimated from total-body-water equations by sex, age and mass. Lean mass explained 70 % of the BMR variance (adjusted R²). The paper expects a 10 % prediction error for an individual.
- **R73:** a synthetic review of studies that measured fat-free mass and REE; the abstract states the equation explains 65 to 90 % of the variation.

None of these is a validation in this athlete or in trained endurance athletes as a group.

## Protocol and data window

- **Measurement:** the athlete measures the three sex-specific sites with a caliper and enters one value per site (the middle of repeated readings) in Ernährung › Körper › Hautfalten.
- **Storage:** athlete-entered canonical observations (`skinfold_<site>`, mm).
- **Age:** the year of the measurement minus the profile's birth year. Without a birth year, body fat stays unknown.
- **Use:** a measurement counts for 90 days. After that, the formula rows show "–".

## Measurement error and uncertainty

- **Body fat:** the 3-site equations carry about ±3 to 4 percentage points of standard error for the individual. The caliper reading, the site location and the measurer add to that. One year of age shifts body fat by about 0.11 percentage points for men (495 / D² × 0.0002574 at D ≈ 1.07) and about 0.06 for women (× 0.0001392 at D ≈ 1.04).
- **Resting energy:** per R72, about 10 % for an individual. An error of 1 kg in lean mass moves the result by 21.6 to 22 kcal/day.
- **Display:** the formula rows show the band of formula 6. Their fit against the adaptive expenditure (the M12 windows) is the empirical check. It compares the same windows for every source, needs at least 4 of them, and outlines a source's row (solid) only when its lead over the next exceeds the mean band of "Aus deinen Daten" (#3397: an outline replaced the "passt am besten" chip). Otherwise it outlines the nearest dashed and says "noch nicht sicher" (athlete decision on #3187).
- **Nominal bands:** they exclude a logging bias in the intake and an activity error beyond the upper end of each M11 method's stated range.

## Bias and transferability

- **Equation populations:** the skinfold equations were fitted on general adult populations. Lean, trained athletes can lie at the edge of their sum range.
- **Two-compartment model:** Siri assumes a constant density of the fat-free mass. Hydration and bone density in athletes can bias body fat.
- **R72 lean mass:** it was itself an estimate, and trained athletes were excluded.
- **Resting rate in athletes:** endurance athletes with a high resting rate per kg of lean mass may lie above both formulas.

## Deviations from the studied procedure

- **Age range:** ages up to 80 are accepted (`SKINFOLD_AGES`). The equations were fitted on men aged 18–61 and women aged 18–55 (`FITTED_AGES`), so older ages extrapolate. Such a measurement keeps its body fat but carries `age_outside_fit`, and the Hautfalten card shows "Alter außerhalb der Studie" under it (#3187 item 9).
- **Readings per site:** one value per site, as decided by the athlete, instead of the studies' repeated readings averaged by a trained measurer.
- **Rounding:** Cunningham uses the abstract's rounded `500 + 22`, not the regression's `501.6 + 21.6`. The difference is `0.4 × LBM − 1.6` kcal/day: about 14 to 30 kcal/day for a lean mass between 40 and 80 kg.
- **Weight:** the lean mass uses M12's current weight (a 28-day line through the weigh-ins) instead of a weight taken at the moment of the skinfold measurement.

## Minimum data and stop rules

- A measurement needs exactly the three sites of its sex, each between 2 and 80 mm.
- Without a birth year, or with an age outside 18 to 80, body fat is unknown.
- The body fat must lie strictly between 0 and 70 %; otherwise there is no lean mass.
- Without a measurement in the last 90 days, or without a weigh-in, the formula rows show "–", with a note in the footnote. No other body-fat value is substituted in these rows.

## Numeric rules

| Rule | Value | Tag | Rationale |
| --- | --- | --- | --- |
| Men's density coefficients | 1.10938, 0.0008267, 0.0000016, 0.0002574 | source | R69, chest, abdomen, thigh |
| Women's density coefficients | 1.0994921, 0.0009929, 0.0000023, 0.0001392 | source | R70, triceps, suprailiac, thigh |
| Density to body fat | 495 / D − 450 | source | R71 |
| Katch-McArdle | 370 + 21.6 × LBM | source | R73 |
| Cunningham | 500 + 22 × LBM | source | R72, abstract |
| Formula error (half-width, about 68 %) | 10 % of the resting value | source | R72: a 10 % individual prediction error, about one SD; kept as the half-width, its coverage labelled (#3617) |
| Skinfold body fat SE | 3.5 percentage points | product assumption | middle of the commonly cited 3.4–3.9 for R69/R70; not verified in the abstracts |
| Activity term in the daily band | each part's M11 method share, plus TEF 80 kcal/day | product assumption | upper end of M11's stated errors, as M12's derived resting rate (#3552) |
| Fit: shared windows | ≥ 4 | product assumption | athlete decision on #3187; 12 windows overlap and hold about 3–4 independent periods |
| Fit: lead for the badge | > mean band of the adaptive TDEE | product assumption | athlete decision on #3187 |
| Fold range | 2–80 mm | product assumption | a value outside is a typing error |
| Accepted ages | 18–80 years | product assumption | wider than the fitted range; a value outside it is flagged |
| Fitted ages | 18–61 (men), 18–55 (women) | source | R69, R70 |
| Body fat for lean mass | 0–70 % (exclusive) | product assumption | outside, the lean mass is not plausible |
| Measurement validity | 90 days | product assumption | the resting reference's lookback (M12) |
| Energy availability | (intake − net exercise energy, M11) / lean mass, a 7-day mean; shown with ≥ 5 complete intake days among the 7 and a known lean mass, below 30 kcal/kg FFM/day (rounded down) | product assumption | athlete decision, #3917; R78 and R79 define energy availability, their abstracts name no cut-off; the lean mass is this card's, so it is as uncertain as the body fat; non-blocking |

## Allowed statements

- "KFA 9,6 % (Hautfalten vom 10.11.)", "fettfreie Masse 60,3 kg".
- "Katch-McArdle: Ruhe 1.672, Tag gesamt 2.810 kcal", with the formula named as the source.
- "Cunningham: Ruhe 1.827 kcal", with the formula named as the source.
- "Passt am besten: Katch-McArdle", from the fit against the adaptive expenditure.
- "Körperfett unbekannt (ohne Geburtsjahr)".
- "Alter außerhalb der Studie", next to a body fat whose age lies outside the fitted range.
- "Energieverfügbarkeit (7 Tage) X kcal/kg FFM · unter 30", a note below the line only; never a diagnosis of low energy availability or REDs (#3917).

## Forbidden statements

- A formula value presented as measured, or as the athlete's true resting rate.
- Katch-McArdle and Cunningham presented as two independent confirmations; they share their slope and differ by an offset.
- A formula value in place of Garmin's or the adaptive estimate.
- Body fat without a birth year, or from fewer than the three sites.
- Precision beyond the method: body fat with more than one decimal, or resting energy without "Schätzung" or the formula name.
- A formula value without its band.
- "Passt am besten" over fewer than 4 shared windows, or with a lead within the band.
- A fixed hit rule such as "within ±100 kcal"; the athlete dropped it (#3187).

## Open points

- Validation check (#3920): after 12 weeks, the Katch-McArdle and
  Cunningham rows (this card's lean mass) against the own total from the
  weight trend (M12 `fit_scale`). Their mean errors with bands are
  recorded here.
- The athlete's method review is pending. The orchestrator is running an independent cross-check before the energy table is shown (#3180).
- The textbook origin of the name "Katch-McArdle" is not verified; the coefficients are verified through R73's abstract.
- R70's abstract was not read: its coefficients match a secondary reference, and the women's fitted age range of 18–55 is from the code comment, not verified.
- R71's page range differs between sources (223–234 or 223–244).
- No validation of either resting formula in this athlete, apart from the fit against the adaptive expenditure.

<!-- BEGIN GENERATED CONSTANTS -->
## Code constants

| Constant | Value | Module | Code comment |
| --- | --- | --- | --- |
| `METHOD_ID` | `'body-composition-resting'` | `backend/athlete/body_composition.py` |  |
| `METHOD_VERSION` | `5` | `backend/athlete/body_composition.py` | Bump with the M13 card version when a formula or constant below changes. |
| `SKINFOLD_BOUNDS_MM` | `(2.0, 80.0)` | `backend/athlete/body_composition.py` | A fold outside this range is a typing error, not a measurement (mm). |
| `SKINFOLD_AGES` | `(18, 80)` | `backend/athlete/body_composition.py` | Accepted ages. The equations were fitted on men aged 18-61 and women aged 18-55; the methods document names that range. |
| `RESTING_FORMULA_ERROR` | `0.1` | `backend/athlete/body_composition.py` | Half-width of a formula's resting band from its own error, as a share of it: Cunningham 1980 expects a 10 % prediction error for an individual (#3187), about one standard deviation, so this term covers about 68 %, not 95 %; kept, and its coverage labelled on M13 (#3617). |
| `SKINFOLD_BODY_FAT_SE` | `3.5` | `backend/athlete/body_composition.py` | Standard error of the 3-site body fat in percentage points: the middle of the commonly cited 3.4-3.9 for the Jackson-Pollock equations (product assumption). |
| `BAND_Z` | `1.96` | `backend/athlete/body_composition.py` |  |
| `LOCAL_LABEL` | `'Lokal'` | `backend/athlete/skinfolds.py` |  |
| `MAPPER_VERSION` | `1` | `backend/athlete/skinfolds.py` |  |
| `LIST_LIMIT` | `20` | `backend/athlete/skinfolds.py` |  |
| `MAX_READINGS` | `3` | `backend/athlete/skinfolds.py` | Readings one site takes; their median is its value (#3556). |
| `MIN_REPEAT_SITES` | `10` | `backend/athlete/skinfolds.py` | Sites with at least two readings before the athlete's own repeatability replaces the default one in the fat-share blend (#3556). |
| `MEDIAN_GAIN` | `{1: 1.0, 2: 0.707, 3: 0.67}` | `backend/athlete/skinfolds.py` | The SD of the median of n normal readings relative to one reading: the mean of two (1 / sqrt 2), and about 0.67 for three, not 1 / sqrt 3 (Gitar on #3622). |
<!-- END GENERATED CONSTANTS -->

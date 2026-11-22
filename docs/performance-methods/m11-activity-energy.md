# M11 — Own activity energy, net of resting metabolism

- **Card:** M11
- **Card version:** 8
- **Method ID:** activity-energy v8 (#3100, #3187, #3255, #3611, #3617, #3530, #3552, #3618)
- **Status:** methods implemented (`backend/performance/activity_energy.py`); daily wiring, adaptive TDEE and display follow (#3100, #3099)
- **Classification:** model estimate
- **Literature checked:** 2026-11-15
- **Review:** blocked

## Target construct

The energy the athlete spends on activity, on top of the resting metabolic rate,
per session and per day, computed from the athlete's own data instead of a
device's calorie estimate. Daily model (athlete decision 2026-11-15, #3100):
`TDEE = RMR + TEF + Σ session net cost + NEAT`, with one shrunken calibration
factor fitted against the adaptive TDEE (energy balance, #3100). This card
covers the activity terms and the thermic effect of food; the resting rate and
the calibration are separate. Garmin's or Wahoo's active calories are shown
only for comparison and never feed these terms.

## Formula and units

All session terms are **net** of the resting rate, so they add to RMR without
counting it twice. 1 kcal = 4184 J.

1. **Running:** `net = m × Σ d_s × C_r(i_s)` with mass `m` (kg), segment run
   `d_s` (m) and the Minetti cost `C_r(i)` (J/kg/m) at the segment's gradient
   `i_s`, clamped to ±0.45. Segments merge samples until the run reaches
   200 m, the span grade-adjusted pace (M8) uses with the same polynomial,
   so altitude noise does not become a steep gradient; a shorter last
   stretch counts as flat. On the flat
   `C_r(0) = 3.6 J/kg/m` (0.86 kcal/kg/km). Running power is never read as
   energy.
2. **Cycling with power:** `gross = W / 4.184 / GE` (W in kJ, GE = 0.20),
   `net = gross − RMR × duration`.
3. **Endurance without power:** the athlete's own flex-HR line
   `net kcal/min = a + b × HR + c × elapsed min`, fitted by least squares
   with one point per power session longer than 3 min of the read window:
   its mean HR, its net cost per minute from item 2, and half its duration
   as the elapsed time. With fewer than 20 such sessions, or all at one
   heart rate, Keytel's equation without VO2max is the fallback, net of RMR.
4. **Strength:** `net = (MET − 1) × RMR/min × duration` with the Compendium
   2024 MET 3.5 (general) to 6.0 (vigorous), i.e. the MET corrected to the
   measured resting rate instead of 1 MET = 3.5 ml/kg/min. The session RPE
   (the athlete's own rating first, else the provider's) sets the MET: 3.5
   up to RPE 5, then 0.5 more per RPE point, 6.0 at RPE 10; without an RPE
   3.5 (athlete decision, #3530). A rating added later re-prices the
   session on the next read, and the diary's energy card names the RPE and MET. Heart rate is not
   used for strength. The duration is the session's elapsed time, else its
   moving time: the MET covers the rests between sets, which a watch's moving
   time leaves out (#3530).
5. **NEAT:** steps outside recorded sessions × step length × the level
   walking cost `C_w(0) = 2.01 J/kg/m` × mass, plus floors as vertical work
   `m × g × 3 m / 0.25`. Step length from recorded walks (#3611): the total
   distance over the total steps of the walks whose own step length
   (distance ÷ recorded steps, else the device's step length, else distance
   ÷ cadence × moving time, as a session counts its steps) lies within
   0.30–0.55 × height (0.45–1.10 m without a height); else 0.415 × height.
   The former mean of speed ÷ cadence ratios came out near 2 m, because a
   short moving time or an odd cadence was not bounded. Every session takes its steps out of NEAT
   (#3255): the device's recorded count first, since the watch also counts
   steps in a strength session or on a ride; a run, walk or hike without
   one uses 2 × its one-leg cadence × duration, else its distance ÷ step
   length. The ascent of the day's runs, walks and hikes, which their
   graded cost already prices, leaves the floors first: `max(0, floors −
   ascent / 3 m)`.
   Walking at any gradient uses Minetti's walking curve scaled so that the
   level equals the ACSM horizontal term (R74) at 20.1 J/mL O2 (R75): the
   curve stays smooth, without a step at 0. Neither Minetti's measured level
   minimum, about 1.64 J/kg/m at the optimal speed (too low for everyday
   walking at other speeds), nor the fit's intercept of 2.5 J/kg/m (which
   overestimates level walking) is used (athlete decision, #3187 item 7).
6. **TEF:** `0.25 × 4 × protein_g + 0.10 × 4 × carbohydrate_g + 0.015 × 9 × fat_g`.
7. **EPOC (optional, off by default):** 6 % of the net cost of a session above
   the threshold.

## Sources

- R44 (Minetti et al. 2002): the running and walking cost polynomials, net
  metabolic power per unit distance; coefficients checked in the paper's Fig. 2.
- R41, R42, R43: cycling gross efficiency (19.8 % in trained men, work-rate and
  training dependent).
- R63 (Keytel et al. 2005): heart-rate equation without VO2max (fallback).
- R64 (Herrmann et al. 2024, 2024 Adult Compendium): resistance-training METs.
- R67 (Byrne et al. 2005): 1 MET overestimates measured resting VO2, the reason
  to correct METs to the measured resting rate.
- R65 (Joosen & Westerterp 2006): thermic effect per macronutrient.
- R66 (LaForgia et al. 2006): EPOC 6-15 % of the net exercise oxygen cost.
- R78 (Loucks, Kiens & Wright 2011): energy availability as intake less exercise energy; its exercise energy is this card's net session cost (#3917).

## Population and reference measurement

Minetti: 10 elite mountain runners on a treadmill, indirect calorimetry.
Moseley & Jeukendrup: 17 trained men, cycle ergometer. Keytel: 115 regularly
exercising adults aged 18-45, gas exchange. The Compendium values are adult
averages. None of them is this athlete; the calibration against the adaptive
TDEE carries the individual offset.

## Protocol and data window

Per completed session from its canonical summary and stored streams (distance,
altitude, power, heart rate); per day from Garmin steps and floors. The flex-HR
line is refitted on the power sessions of the read span: the 12 weekly
windows and the 27 days before the first, about 15 weeks (#3618).

## Measurement error and uncertainty

Expected error (method comment, #3100): running ±8-10 %, cycling ±10 %,
flex-HR ±10-15 % individually calibrated and ±20-30 % or more with Keytel,
strength ±30-50 % (small absolute share), step length ±10-15 %, TEF ±30-80
kcal/day. Sums are reported with bands, not points.

A day's activity error (95 %, #3552) takes the upper end of each range: each
session's net cost times its method's share (`METHOD_ERROR_SHARE`: Minetti
and power 10 %, flex-HR 15 %, Keytel 30 %, MET 50 %, an unlisted method the
widest) and the everyday energy times the step length's 15 %
(`STEP_ERROR_SHARE`), in quadrature. Digestion adds `TEF_ERROR_KCAL` (80
kcal/day). M12's derived resting rate and M13's formula day totals carry
both.

## Bias and transferability

Laboratory treadmill and ergometer values transfer imperfectly to outdoor
terrain, wind and stops. GPS altitude is noisy; the 200 m grade span (`GRADE_SPAN_M`) limits it.
Gross efficiency varies with work rate (lower below about 100 W). The flex-HR
line is calibrated on power-derived cost, not calorimetry (weak evidence).
NEAT counts only the walking of steps and floors; standing, housework,
carrying, unrecorded cycling and stop-and-go movement have no term, so the
formula rows' everyday energy runs low. M12's own row names that remainder
"übriger Alltag" where its residual exceeds the resting reference
(#3550); a learned offset follows once enough data exists.

## Deviations from the studied procedure

- Minetti's polynomial is applied per GPS segment of field runs, not on a
  treadmill at constant speed.
- GE is one constant instead of a per-work-rate value.
- Flex-HR uses power-derived net cost as its reference instead of calorimetry.
- Floors use an efficiency of 0.25 taken from Minetti's statement that steep
  uphill walking reaches concentric muscle efficiency.

## Minimum data and stop rules

- Digestion counts the logged kcal that an entry's known macros do not cover at
  10 % (`TEF_UNCOVERED`), not 0, and the day's Verdauung then reads
  "geschätzt" (#3617).
- Running needs distance and altitude streams; without altitude the grade is 0.
  A run, walk or hike without any distance is unknown, not 0 kcal; its
  recorded steps still leave everyday (#3617).
- Cycling needs the session's work (kJ) or a power stream.
- Flex-HR needs 20 power sessions over at least two heart-rate levels, else
  Keytel; Keytel needs weight, age and sex, else no value.
- NEAT needs the day's steps; without a measured or height-based step length
  there is no value.
- A missing input gives "unknown", never a device value in its place: a day
  without steps or with an unpriced session has no activity value, and the
  net costs need the resting rate.

## Numeric rules

| Rule | Value | Tag | Rationale |
| --- | --- | --- | --- |
| Running cost polynomial | 155.4, −30.4, −43.3, 46.3, 19.5, 3.6 J/kg/m | source | R44, Fig. 2 |
| Walking cost polynomial | 280.5, −58.7, −76.8, 51.9, 19.6, 2.5 J/kg/m, scaled by 2.01 / 2.5 | source | R44, Fig. 2; scaled to the R74 level |
| Level walking cost | 0.1 mL O2/kg/m × 20.1 J/mL = 2.01 J/kg/m | source | R74 horizontal term, R75 energy equivalent; athlete decision #3187 item 7 |
| Gradient clamp | ±0.45 | source | R44 measured range |
| Grade segment length | 200 m | product assumption | limits GPS altitude noise; the M8 grade span |
| Cycling gross efficiency | 0.20 | source | R42 (19.8 % measured), varies per R41, R43 |
| Strength MET general / vigorous | 3.5 / 6.0 | source | R64 codes 02054 / 02050 |
| Strength MET by session RPE | 3.5 up to RPE 5, +0.5 per point, 6.0 at RPE 10 | product assumption | athlete decision, #3530; linear between the two Compendium classes |
| MET correction to own RMR | (MET − 1) × RMR | source | R67 |
| Keytel coefficients (kJ/min) | men −55.0969, 0.6309, 0.1988, 0.2017; women −20.4022, 0.4472, −0.1263, 0.074 | source | R63, as reproduced by secondary sources |
| Flex-HR shortest session / minimum sessions | 180 s / 20 | product assumption | steady state; enough spread for three terms |
| Floor height / uphill efficiency | 3 m / 0.25 | product assumption | Garmin floor; R44 concentric efficiency statement |
| Step length from height | 0.415 × height | product assumption | common heuristic, weak evidence |
| TEF protein / carbohydrate / fat | 0.25 / 0.10 / 0.015 | source | R65 midpoints |
| EPOC share | 0.06 | source | R66 low end |
| Exercise energy of the energy availability | the day's session net cost, already net of the resting energy over each session's duration | source | R78 (exercise energy expenditure); a day with an unpriced session has none (#3917) |
| Planned unit cost | strength: (MET − 1) × RMR × planned duration at the general MET; any other sport: planned duration × the athlete's own mean net kcal per hour of that sport over the last 28 days, priced by its M11 method | product assumption | the expected day total of the diary (#3917); a sport without own priced sessions has no cost and leaves the total unknown |

## Allowed statements

- "Eigener Aktivitätsverbrauch (Netto, über dem Ruheumsatz): X kcal, Methode M11", with its band.
- "Tag erwartet ≈ X", today only, with the planned units costed from their planned duration as in the rule above; it informs and never replaces the total so far.
- Which method a session used (Minetti, Leistung, eigene Herzfrequenz-Kalibrierung, Keytel, MET).

## Forbidden statements

- A session value without its method or band.
- Garmin's or Wahoo's active calories as part of the own estimate.
- Running power as energy.
- A strength estimate from heart rate.

## Open points

- Validation check (#3920): after 12 weeks with logged intake, the
  existing fit of the formula rows (resting plus this card's sport
  energy) against the own total from intake and the weight trend (M12
  `fit_scale`). The mean error with its band is recorded here.
- Daily wiring, the adaptive TDEE and the calibration factor (#3100).
- Imbach 2020 and Cerezuela-Espejo 2021 are not cited until identified.
- Keytel's coefficients are checked against secondary sources only.
- EPOC stays off until the calibration shows a need.

<!-- BEGIN GENERATED CONSTANTS -->
## Code constants

| Constant | Value | Module | Code comment |
| --- | --- | --- | --- |
| `METHOD_ID` | `'activity-energy'` | `backend/performance/activity_energy.py` |  |
| `METHOD_VERSION` | `8` | `backend/performance/activity_energy.py` | Bump when a formula or constant below changes the estimate. |
| `J_PER_KCAL` | `4184.0` | `backend/performance/activity_energy.py` | Joules per kilocalorie (thermochemical calorie). |
| `GRAVITY` | `9.81` | `backend/performance/activity_energy.py` | Standard gravity, m/s^2, for the vertical work of floors. |
| `MINETTI_WALKING` | `(280.5, -58.7, -76.8, 51.9, 19.6, 2.5)` | `backend/performance/activity_energy.py` | Minetti et al. 2002: the running fit (MINETTI_RUNNING, its gradient range and the 200 m grade span come from grade_adjustment, #3187), and the same fit for walking. |
| `ACSM_WALKING_O2_ML_PER_KG_M` | `0.1` | `backend/performance/activity_energy.py` | Level walking, net of rest, from the ACSM walking equation (ACSM's Guidelines for Exercise Testing and Prescription, metabolic equations; VO2 = 0.1 x S + 1.8 x S x G + 3.5, S in m/min): its horizontal term 0.1 mL O2/kg/m at the conventional 20.1 J per mL O2 (Schmidt-Nielsen 1984). Athlete decision on #3187 item 7: neither Minetti's measured minimum (1.64, at optimal speed) nor the fit's intercept (2.5). |
| `J_PER_ML_O2` | `20.1` | `backend/performance/activity_energy.py` |  |
| `CYCLING_GROSS_EFFICIENCY` | `0.2` | `backend/performance/activity_energy.py` | Gross efficiency of cycling (mechanical / metabolic energy): 19.8 % measured in 17 trained men by Moseley & Jeukendrup 2001 (R42); it varies with work rate and training (R41, R43), and the one calibration factor absorbs an individual offset (weak evidence). |
| `STRENGTH_MET_GENERAL` | `3.5` | `backend/performance/activity_energy.py` | 2024 Adult Compendium of Physical Activities (Herrmann et al., J Sport Health Sci 13:6): code 02054, resistance training, multiple exercises, 8-15 reps. |
| `STRENGTH_MET_VIGOROUS` | `6.0` | `backend/performance/activity_energy.py` | Compendium 2024 code 02050: free weights, power lifting, vigorous effort. |
| `STRENGTH_GENERAL_UP_TO_RPE` | `5.0` | `backend/performance/activity_energy.py` | A strength session's MET by its session RPE (athlete decision, #3530): the general MET up to RPE 5, then 0.5 more per RPE point, up to the vigorous MET at RPE 10; without an RPE the general MET. |
| `STRENGTH_MET_PER_RPE` | `0.5` | `backend/performance/activity_energy.py` |  |
| `KEYTEL_MALE` | `(-55.0969, 0.6309, 0.1988, 0.2017)` | `backend/performance/activity_energy.py` | Keytel et al. 2005 (J Sports Sci 23:289), model without VO2max: gross energy expenditure in kJ/min from heart rate (bpm), weight (kg) and age (years): intercept, HR, weight, age. Coefficients as reproduced by several secondary sources; fallback only (about +-20-30 % individually). |
| `KEYTEL_FEMALE` | `(-20.4022, 0.4472, -0.1263, 0.074)` | `backend/performance/activity_energy.py` |  |
| `FLEX_HR_SKIP_S` | `180.0` | `backend/performance/activity_energy.py` | Flex-HR: a power session this short is not steady state and is not fitted. |
| `FLEX_HR_MIN_SEGMENTS` | `20` | `backend/performance/activity_energy.py` | Flex-HR needs at least this many power sessions (one point each). |
| `FLOOR_HEIGHT_M` | `3.0` | `backend/performance/activity_energy.py` | Vertical metres per counted floor (Garmin counts about 3 m of ascent). |
| `UPHILL_WORK_EFFICIENCY` | `0.25` | `backend/performance/activity_energy.py` | Efficiency of vertical work when walking up steep gradients: Minetti et al. 2002 found uphill walking above +0.15 reaches concentric muscle efficiency, taken here as 0.25 (assumption from that statement). |
| `STEP_LENGTH_PER_HEIGHT` | `0.415` | `backend/performance/activity_energy.py` | Step length from body height when no recorded walk measured it; a common heuristic, weak evidence (+-10-15 %). |
| `STEP_LENGTH_HEIGHT_BOUNDS` | `(0.3, 0.55)` | `backend/performance/activity_energy.py` | A walk's own step length (distance / steps) counts only within these shares of body height (#3611): a walking step is about 0.4 x height, and even race walkers stay near 0.55; without a height, the same shares of 1.50 and 2.00 m. |
| `STEP_LENGTH_BOUNDS_M` | `(0.45, 1.1)` | `backend/performance/activity_energy.py` |  |
| `TEF_PROTEIN` | `0.25` | `backend/performance/activity_energy.py` | Thermic effect of food as a share of each macronutrient's energy: midpoints of protein 20-30 %, carbohydrate 5-15 %, fat 0-3 % (Joosen & Westerterp 2006, Nutr Metab 3:25). Alcohol is not stored, so it has no term. |
| `TEF_CARBOHYDRATE` | `0.1` | `backend/performance/activity_energy.py` |  |
| `TEF_FAT` | `0.015` | `backend/performance/activity_energy.py` |  |
| `KCAL_PER_G_PROTEIN` | `4.0` | `backend/performance/activity_energy.py` | Atwater energy per gram: protein, carbohydrate, fat (kcal/g). |
| `KCAL_PER_G_CARBOHYDRATE` | `4.0` | `backend/performance/activity_energy.py` |  |
| `KCAL_PER_G_FAT` | `9.0` | `backend/performance/activity_energy.py` |  |
| `TEF_UNCOVERED` | `0.1` | `backend/performance/activity_energy.py` | The thermic effect of logged kcal that its known macros do not cover: 10 %, a mixed meal's typical share, instead of 0 (#3617 item 3). |
| `EPOC_SHARE` | `0.06` | `backend/performance/activity_energy.py` | Optional excess post-exercise oxygen consumption for sessions above the threshold: 6 % of the net session cost, the low end of LaForgia et al. 2006 (J Sports Sci 24:1247, 6-15 %). Off unless asked for; weak evidence. |
| `METHOD_ERROR_SHARE` | `{'minetti': 0.1, 'power': 0.1, 'flex_hr': 0.15, 'keytel': 0.3, 'met': 0.5}` | `backend/performance/activity_energy.py` | The 95 % error of a session's net cost as a share of it, by M11 method: the upper end of each expected error (method comment on #3100, #3552): running (Minetti) +-8-10 %, cycling power +-10 %, flex-HR +-10-15 %, Keytel +-20-30 %, MET (strength) +-30-50 %. A method not listed takes the widest share. |
| `STEP_ERROR_SHARE` | `0.15` | `backend/performance/activity_energy.py` | Everyday energy from steps carries the step length's +-10-15 %. |
| `TEF_ERROR_KCAL` | `80.0` | `backend/performance/activity_energy.py` | Thermic effect of food: +-30-80 kcal per day, the upper end. |
<!-- END GENERATED CONSTANTS -->

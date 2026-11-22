# M14 — Early infection hint from night values

- **Card:** M14
- **Card version:** 4
- **Method ID:** infection-hint-v1 (#3762)
- **Status:** implemented (`backend/performance/infection_hint.py`), awaiting athlete review
- **Classification:** descriptive statistic
- **Literature checked:** 2026-11-22
- **Review:** blocked

## Target construct

Whether last night's Garmin night values together lie clearly outside the
athlete's own usual range in the direction that often accompanies the start of
an infection: a higher breathing rate during sleep, a higher resting heart rate
and a lower overnight HRV. It is a non-blocking hint to ask about symptoms, not
an infection, illness, recovery or readiness state.

## Formula and units

`infection-hint-v1` (`backend/performance/infection_hint.py`):

- Signals, Garmin only, from stored canonical observations: breathing rate
  during sleep (`respiration_rate`, breaths/min), resting heart rate
  (`resting_hr`, bpm) and overnight HRV (`hrv_rmssd`, RMSSD in ms). SpO2 is not
  used.
- Baseline per signal: the M2 comparison `personal-quartiles-v1`
  (`personal_recovery.series_baseline`, the same `_baseline` as M2): the 42 days
  before, at least 14 earlier nights, restart after a measuring change; median,
  lower quartile `Q1` and upper quartile `Q3` (inclusive, n = 4);
  `IQR = Q3 − Q1`.
- Only last night counts: the value observed on the local today. An older or
  missing value, or one whose baseline has fewer than 14 nights, is unknown;
  it never counts as normal or zero.
- Clearly unusual, unfavourable direction only:
  - breathing rate: `value > Q3 + 0.5 × IQR` and `value − median ≥ 1` breath/min;
  - resting heart rate: `value > Q3 + 0.5 × IQR` and `value − median ≥ 3` bpm;
  - HRV: `value < Q1 − 0.5 × IQR` and `median − value ≥ 0.10 × median`.
- Trigger: at least 2 of the 3 values clearly unusual last night. No two-night
  rule. Fewer than 2 known values: status `unknown`, nothing is shown.
- Statuses: `hint`, `none` (at least 2 known, fewer than 2 unusual),
  `unknown`.
- Worked example: breathing rate baseline median 14.0, Q1 13.8, Q3 14.6
  (IQR 0.8): 16.0 is above 15.0 and 2.0 above the median, so unusual. Resting
  heart rate median 50, Q1 49, Q3 51 (IQR 2): 54 is above 52 and 4 above the
  median, so unusual. Two of three unusual: the hint shows, whatever the HRV.
- Output: each known value with its usual range (Q1 to Q3), median and source,
  labelled as derived, plus the limits below, in the daily planning context the
  Coach reads and in the planning brief. The note on today's calendar day shows
  only the unusual values with their usual range, for example "Infekt-Hinweis:
  Atemfrequenz 16,8/min (üblich 14,2–15) · Ruhepuls 58 bpm (üblich 49–52)"
  (athlete decision); the limits stay with this card and the Coach. The hint is
  computed on read.
- Answers on the note (#3821, athlete decision): "Ich bin krank" records illness
  in today's check-in, the same record a check-in or Coach turn writes, with
  its receipt and undo; planning then pauses as for any reported illness,
  through the adaptive preview and its approval. "Mir geht's gut" hides the
  hint for today in the calendar, the Coach context and the planning brief;
  only that day is stored, and a hint on a later day shows again. Once today
  reports illness the note keeps its values without the two answers.
- Symptoms (#3919, athlete decision): "Ich bin krank" expands the note into
  nine symptom chips and "Bestätigen"; the selected symptom ids are stored with
  that check-in (`schnupfen`, `halsschmerzen`, `husten`, `kopfschmerzen`,
  `fieber`, `gliederschmerzen`, `muedigkeit`, `brust_atemnot`, `magen_darm`);
  the Coach reads them and sets them from the athlete's own message. Any
  selected symptom means rest only, with no training, no easy alternatives and
  no session cap, and the return after the illness pause waits at stage 0 (rest
  only) until two later check-ins have no symptom selected; a later symptom
  restarts the count; stage 1 then starts as before. An illness report with an
  empty list keeps the earlier behaviour.

## Sources

Wearable studies show that night breathing rate and resting heart rate rise
and HRV falls around an infection, often before symptoms, against the
person's own baseline (R83, R84, R85; #3920). They support the direction of
the three signals and the use of a personal baseline. The thresholds here
(quartiles plus half the IQR, the minimum differences, two of three values)
remain product rules: none of the studies tested them, and all of them
studied COVID-19, mostly retrospectively. The baselines follow M2 and its
sources.

Reason for rest with any symptom: R89 (Ruuskanen et al. 2022, full text) finds
the "neck-check" rule, which allows exercise when symptoms sit above the neck,
without supporting evidence, possibly hazardous, and says it "should be
abandoned"; it advises rest of 1 day to 1 week after a febrile illness (expert
opinion). Context: R87 (Weidner et al. 1998, abstract) found moderate exercise
did not worsen an experimental rhinovirus cold in 34 young healthy adults, one
virus and no fever, so it does not carry over; R88 (Schwellnus et al. 2016,
full text) notes fever impairs heat regulation and that runners with systemic
symptoms before a race were 2-3 times less likely to finish. No source gives a
number of symptom-free check-ins; the count of two is a product rule. Animal
and case-report evidence on myocarditis and post-Covid return-to-play guidance
were not verified and are not cited.


## Population and reference measurement

None: no study population or reference measurement underlies the thresholds.
The comparison is with the athlete's own earlier nights only.

## Protocol and data window

Garmin night values as stored by the Garmin health mapper: breathing rate and
HRV are means of the sleep or overnight window, resting heart rate is Garmin's
lowest 30-minute average of the day (see M2). Data window: last night against
the 42 days before, 83 days of history loaded.

## Measurement error and uncertainty

The HRV agreement figures of M2 (R10) apply to the HRV signal. Garmin's breathing
rate during sleep and its resting heart rate have no validation figures in this
register. Quartiles are not uncertainty intervals; the half-IQR margin and the
minimum changes only reduce false alarms, they do not set an error bound.

## Bias and transferability

All three values move with hard training, alcohol, heat, altitude, travel and
short sleep as well as with an infection; the hint cannot tell these apart.
An infection can also start without any change. The rule was set by the
athlete for this one athlete and is not validated for anyone.

## Deviations from the studied procedure

- No studied procedure: the thresholds are product rules chosen by the athlete,
  not taken from a study.
- SpO2 is stored but not used, by athlete decision.
- No two-night rule; one night decides, by athlete decision.

## Minimum data and stop rules

- A signal counts only with a value observed today and a usable M2 baseline
  (`ok` or `provisional`, at least 14 earlier nights since the latest measuring
  change).
- Fewer than 2 known signals: no statement.
- The hint never blocks, pauses or changes a plan or session; only the
  athlete's own illness check-in, including the answer "Ich bin krank", starts
  the illness pause.

## Numeric rules

| Rule | Value | Tag | Rationale |
| --- | --- | --- | --- |
| Baseline | `personal-quartiles-v1`: 42 days, 14 earlier nights, restart on a measuring change | product assumption | Reuses M2 unchanged. Chosen by the athlete. |
| Margin beyond the usual range | 0.5 × IQR | product assumption | Fewer false alarms than any value outside the quartiles, which half of all nights are. Chosen by the athlete. |
| Breathing rate minimum change | +1 breath/min over the median | product assumption | Keeps a narrow range from flagging tiny changes. Chosen by the athlete. |
| Resting heart rate minimum change | +3 bpm over the median | product assumption | As above. Chosen by the athlete. |
| HRV minimum change | −10 % of the median | product assumption | As above, relative because HRV varies widely between people. Chosen by the athlete. |
| Unusual values for the hint | at least 2 of 3 | product assumption | One value alone moves too often for other reasons. Chosen by the athlete. |
| Known values for any statement | at least 2 | product assumption | With one value the 2-of-3 rule cannot be met. Chosen by the athlete. |
| Check-ins without symptoms before the return starts | 2 later check-ins | product assumption | Rest with any symptom rests on R89; the count of two is not from a source. Chosen by the athlete. |
| Night that counts | today's observation only | product assumption | An older value says nothing about last night. Chosen by the athlete. |

## Allowed statements

- "Last night your breathing rate (16.0/min, usual 13.8–14.6) and resting
  heart rate (54 bpm, usual 49–51) were clearly above your usual range
  (Garmin, derived). Do you have any symptoms? An easier day may make sense."
- "Hard training, alcohol, heat, altitude, travel or short sleep move the same
  values."

## Forbidden statements

- "You are ill", "you have an infection" or any other diagnosis.
- Blocking, pausing or changing a plan or session because of the hint.
- Treating a missing value as normal, or calling a normal night healthy.
- Calling the usual range a normal or clinical range.

## Open points

- Validation check (#3920): after three months, the shown hints that
  were followed within 3 days by "Ich bin krank" or an illness
  check-in, and the illness check-ins without a hint before them. Both
  counts are recorded here.
- Validation figures for Garmin's breathing rate during sleep.
- Athlete review of this card before release.

<!-- BEGIN GENERATED CONSTANTS -->
## Code constants

| Constant | Value | Module | Code comment |
| --- | --- | --- | --- |
| `METHOD` | `'infection-hint-v1'` | `backend/performance/infection_hint.py` |  |
| `RANGE_MARGIN` | `0.5` | `backend/performance/infection_hint.py` | Beyond the usual range by this share of its width (the interquartile range). |
| `MIN_UNUSUAL` | `2` | `backend/performance/infection_hint.py` |  |
| `LIMITS` | `'Abgeleiteter Hinweis, keine Diagnose. Hartes Training, Alkohol, Hitze, Höhe, Reisen und kurzer S... (sha256 7e8e7194)` | `backend/performance/infection_hint.py` |  |
<!-- END GENERATED CONSTANTS -->

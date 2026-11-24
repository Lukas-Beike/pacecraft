# session-report Specification

## Purpose
Code computes every deterministic check of one completed session (value, rule, threshold, basis, verdict and severity), so the Coach's session analysis explains verdicts instead of computing them (#3084).

## Requirements

### Requirement: Deterministic session report
The system SHALL add to every `get_activity_details` result a `session_report` (method `session-report-v1`), computed on read from the session's existing derivations without a stored payload or model call. Thresholds are the athlete's decisions on #3084 (2026-11-15).

#### Scenario: Check shape and ranking
- **WHEN** the report is built
- **THEN** each check carries:
  - its value and the rule;
  - the threshold as a named constant;
  - the basis: `personal` against the athlete's own comparable values (at least 5), `plan` against the paired planned unit, else `fixed`;
  - the verdict: `ok`, `notable`, or `unknown` with a reason;
  - a deterministic severity.

  The report lists every notable check by severity in `notable`, the first four in `lead`, and every unknown check in `unknown`.

#### Scenario: Plan against actual
- **WHEN** the session has a paired planned unit
- **THEN** the report judges for runs and rides the duration (elapsed for interval sessions, whose planned rests the estimate counts, else moving) and the local TSS-scale load (M6, the scale of the plan estimate; never a provider load) against the unit's estimate, notable beyond ±20 %. For detected intervals it judges the planned work steps against the executed work laps (notable on missing or extra steps, with the mapping confidence); without a plan-to-lap mapping that check is unknown with the mapping's reason. When the lap fitting leaves planned steps in zone 3 or above, outside the paired reps and their floats, without a lap for at least 10 minutes of planned time together (a skipped block; less is a warm-up build or a fast finish), the mapping is uncertain: structure, time in target and recovery length are unknown, and the structure names the planned and unmatched hard steps.

#### Scenario: Easy, recovery and long sessions
- **WHEN** the session focus is recovery, easy, endurance or long
- **THEN** the report judges:
  - aerobic decoupling, notable above 5 %;
  - the share of heart-rate time above the LT1 heart rate in effect (the stored first threshold as share of the threshold HR, else 85 %, on the session day), notable above 10 %; the paired plan's own seconds above LT1 (strides, a fast finish) are taken off, unplanned surges count; a unit with a heart-rate cap is judged by its cap instead (#3915);
  - a stored heart-rate cap by the verdict the planned targets computed (mean above the cap or more than 10 % of the time above it);
  - for rides the variability index, notable above 1.05.

#### Scenario: Intervals, threshold sessions and tests
- **WHEN** the execution found intervals, or the focus is tempo, threshold, vo2max, anaerobic, sweet_spot or test and the execution is not continuous
- **THEN** the report judges:
  - fade across reps: the weakest rep against the median of the largest group of reps within 35 % of one duration, as speed or power, with lap pace for reps under 2 min; notable at 3 %; unknown for a test session or reps with different planned targets;
  - fade within reps: second against first half, notable at 3 %;
  - a too-fast start, notable at 3 %;
  - cardiac drift between the first and last rep at equal output, notable above 5 %;
  - time in target per rep, notable for a rep in the poor adherence band (below 50 %);
  - the recovery length against the plan, notable beyond ±20 %;
  - the mean HR drop 60 s into the recoveries against the median of the athlete's comparable sessions, notable 5 bpm below it, and unknown with fewer than 5 of them.

  Time in target and recovery length are unknown when the planned work steps and the executed work laps differ in number, because steps and laps pair in order. Cardiac drift compares only reps of one length. Without detected reps the fade check is unknown with the execution's reason.

#### Scenario: Continuous sessions
- **WHEN** the session has no intervals and is neither easy nor a race
- **THEN** the report judges a ride's variability index, and for a tempo or threshold focus the second against the first half of the main part, notable at 3 % slower.

#### Scenario: Race
- **WHEN** the session focus or purpose is race
- **THEN** the report judges the second half of the main part against the first, notable at 2 % slower. The halves leave out a warm-up lap or else the first 10 min of a session of 30 min or more, so the rule names that a fast start is not part of it.

#### Scenario: Grade-adjusted pace
- **WHEN** the session is an outdoor run with speed and a grade or altitude stream
- **THEN** the report carries its grade-adjusted pace after Minetti 2002 over the moving time, overall and per lap, with the pace, the difference in percent, the grade coverage and the device's own value as a comparison; the check is an estimate and stays `ok`, never notable alone, and is unknown without the streams or when the speed fails its data-quality rule (#3084 Part 3).

#### Scenario: Facts passed on without a verdict
- **WHEN** the session has a provider training effect, an execution data-quality result, or (for a run or ride) strength sessions in the 48 h before it
- **THEN** the report carries the facts checks `training_effect` (aerobic, anaerobic and label, each with its own source, basis `provider`, unchanged), `data_quality` (the heart-rate and speed states and which data the rules doubt; the checks on doubted data are unknown) and `strength_before` (count, leg sessions, intensities, the closest hours before and whether they were confirmed). Each is `ok` with weight 0, never in `notable` or `lead`, and unknown only when its data is missing or unreadable; no strength session found while the planned units could not be read is unknown, not zero (#3084 Part 3c).

#### Scenario: Strength sets, heat, gaps and hills
- **WHEN** a strength session has a plan with sets, a run or ride has stored weather, its streams have holes, or a ride has reps
- **THEN** the report judges completed against planned sets through the plan rule (notable outside ±20 %; sets only, no load). It gives the check `heat` (the activity weather only, never a finding), whose `explains` names the heart-rate checks the report finds notable (decoupling, time above LT1, cardiac drift) from 25 °C apparent temperature. The execution marks a heart-rate or speed channel `gaps` once more than 5 % of the recorded time (clock jumps over 30 s are pauses) sits in runs of missing values longer than 5 s, so the checks reading it are unknown. The check `hills` lists each ride rep's net elevation change and mean grade as facts (athlete decisions B to E, #3084 Part 3c).

#### Scenario: The night before and the load of the days before
- **WHEN** a run or ride is read
- **THEN** the detail carries `recovery_before`: sleep, HRV and resting HR anchored at the session's day by the personal-recovery rule (own quartiles from at least 14 values in 42 days), computed from the canonical inputs the detail read already holds, without a second read and without the history series. The report gives the check `recovery_before` as facts with `that_night`, and its `explains` names the notable heart-rate checks only when a value of that night lies on its unfavourable side (less sleep, lower HRV, higher resting HR). The check `prior_load` gives the local load (M6) of the 3 days before against the median of every 3-day sum of the 42 days before, from one bounded read. Both are `ok` with weight 0, never a finding (athlete decisions G and H, #3084 Part 3c).

#### Scenario: Energy of the days before and a weather gap (#4136)
- **WHEN** a run or ride is read
- **THEN** the report's fact `energy_before` names, for each of the 3 days before, the diary intake, the expenditure (the own day total, else the device's whole-day total, with its source) and the balance, and their mean balance only when every day has one; a run or ride without stored weather carries `weather` with status `pending` (before 2022 or within 48 hours of its end, when the app has not looked it up yet), `missing` (outdoors after that: a gap of the app's own look-up) or `not_applicable` (indoors, with no `heat` check); otherwise the `heat` check is unknown with that reason. The days come from the history the energy view keeps.

#### Scenario: Each planned segment against its window (#4136)
- **WHEN** a run or ride has a paired planned unit whose steps were scored against their windows
- **THEN** the report's fact `plan_segments` lists every step in order (warm-up, each work step and recovery, cool-down) with its planned target, minutes, time in target and band, its pace or power and its heart rate (mean, over its first and last 30 s, peak); a work step at least 1 % faster than the work step before names `faster_than_previous_work_pct`, so a rep started too fast shows.

#### Scenario: Wind at body height
- **WHEN** an outdoor run or ride has a stored headwind on its own track (#3084 Part 3b)
- **THEN** the report carries the check `wind` ("Wind auf Körperhöhe (geschätzt)"): the signed mean wind component along the direction of travel at body height (the stored 10 m model value × 0.72, log wind profile with z0 0.03 m for open land), the head and tail shares (±45°, beyond ±135°) and the coverage. It is unknown below 50 % coverage or without a track, and always `ok` with weight 0, never in `notable` or `lead`. Its `explains` names a slower part only when that part's own check in the report finds it (the race split, or the main-part fade of a continuous tempo effort, each with its own rule; or the rep fade with its weakest rep), the part had at least 3 m/s headwind at body height and 2 m/s more than the part it is compared with. The stored 60 s bins reach no tool output; only these reduced values leave the report.

#### Scenario: RPE against intensity
- **WHEN** a run or ride carries a session RPE and a local IF
- **THEN** the report compares the RPE with the median RPE of the athlete's own rated sessions of the same sport and RPE scale within ±0.05 IF (5 points of the stored IF in %) over the 84 days before the session's day (a session later that day never counts), read once for that window; notable from a 2-point gap; unknown with fewer than 5 such sessions or without a rating or IF (#3084 Part 3).

#### Scenario: Data quality gates verdicts
- **WHEN** the heart-rate data shows spikes or a cadence lock or was not checked, or the speed of a speed-based output (a run, a ride without power) jumps
- **THEN** every check that reads those data is unknown with the data-quality reason instead of notable. Other unknown reasons pass on the owner's reason (comparable sessions, halves).

Evidence:
- `backend/performance/session_report.py:session_report`
- `backend/coach/activity_read_tools.py:CoachActivityReadToolService._with_session_context`
- `backend/coach/activity_read_tools.py:CoachActivityReadToolService._influences`
- `backend/nutrition/energy_view.py:days_before`
- `backend/weather/activity_weather.py:weather_pending`
- `backend/performance/planned_targets.py:planned_step_adherence`
- `backend/performance/session_conditions.py:grade_adjusted_pace`
- `backend/performance/session_conditions.py:body_headwind`
- `backend/performance/activity_read_service.py:ActivityAnalysisReadService.session_headwind`
- `backend/performance/session_conditions.py:expected_rpe`
- `backend/performance/activity_read_service.py:ActivityAnalysisReadService.rated_sessions`
- `tests/test_session_report.py`
- `tests/test_session_conditions.py`

### Requirement: Deterministic session resolution
The system SHALL resolve which session `get_activity_details` reads in the server, never by the model's choice, and SHALL return the candidates instead of a detail when the reference is not unique (#3084, #3080).

#### Scenario: An ID is read as given
- **WHEN** the call carries `activity_id`, as the analyse button writes it
- **THEN** that session is read.

#### Scenario: The session the athlete names is read
- **WHEN** the athlete's own request (the current message, plus the messages an open clarification answers) names a session with the analyse button's prompt ("Analysiere meine Einheit vom <day> (…, ID <id>)"; the last one when several), read by the server from the stored messages, never from a model argument
- **THEN** a call with neither an ID nor a day reads that session instead of the newest (`by_named_id`), and falls back to the newest when the named session is no longer stored; a call with another ID reads it with a notice naming the athlete's session, since it may be a record ID of the same session (`by_id_not_named`); free text naming an "ID", or the planned-unit discussion prompt, names no session. The sport block of the session-analysis playbook is chosen from the same request text, or, without a named session, from the newest session as this resolution picks it.

#### Scenario: A date, sport and start time resolve one session
- **WHEN** the call carries a date, optionally with a sport and a start time
- **THEN** the sessions of that local day of that sport starting within 30 minutes of the start time match; exactly one match is read.

#### Scenario: No reference means the newest session
- **WHEN** the call carries neither an ID nor a date
- **THEN** the newest session of the last 30 days, of the given sport if any, is read; a commute, warm-up or cool-down is skipped when a training session matches.

#### Scenario: An unclear reference asks back
- **WHEN** several sessions match, or none
- **THEN** the result has status `ambiguous` or `not_found` and lists the newest ten candidates (on `not_found` those of the named sport, else every session searched) with their ID, start, sport, moving time, distance and session focus, never their stored name, and no detail; the playbook asks the athlete which session they mean. A resolution counts in the diagnostics only once its detail was read.

### Requirement: Session report diagnostics
The runtime diagnostics SHALL count the session reports built since start, their checks by verdict and the session resolutions by outcome, with no ID, date or value.

#### Scenario: Counts after analyses
- **WHEN** the Coach read two session details and one reference was ambiguous
- **THEN** `runtime.session_reports` shows the reports, their checks by verdict and the resolutions by outcome.

Evidence:
- `backend/coach/activity_read_tools.py:resolve_session`
- `backend/coach/activity_read_tools.py:named_activity_id`
- `backend/coach/tool_execution_service.py:CoachStructuredToolExecutionService`
- `backend/coach/request_payload.py:analysed_sport`
- `backend/observability.py:SessionReportCounts`
- `tests/test_coach_activity_read_tools.py`
- `tests/test_coach_tool_execution_service.py:StructuredToolExecutionTests.test_only_the_athletes_own_request_names_a_provider_or_session`
- `tests/test_coach_playbooks.py`
- `tests/test_coach_live_eval.py`

### Requirement: Race against its race plan
The session report SHALL judge a race linked to a stored competition against that competition's race plan (#2803), with the athlete's limits from #3084 (2026-11-15). Runs compare recorded times with the plan's grade-adjusted section times, rides the distance-weighted mean power with the planned power.

#### Scenario: Recording laid onto the course
- **WHEN** the recording ends with a stretch slower than 60 % of the race's median speed, or GPS measured the course a little long or short
- **THEN** that slow stretch (a walk-off after the line) is cut off and the rest is scaled onto the plan's distance.

#### Scenario: Sections and start
- **WHEN** the race plan is ready and the recording covers the plan's distance within 10 %
- **THEN** each section more than 3 % off the plan is notable, and the start, the first 10 % of the distance and at least 1 km, is notable when more than 3 % faster than the plan there (or 3 % more power).

#### Scenario: Fade point
- **WHEN** every km from one km to the finish is more than 3 % behind the plan there, and a km before it was on plan
- **THEN** the report names that km as the fade point and is notable; a race behind plan from the first km has no fade point.

#### Scenario: Finish against the target
- **WHEN** the competition has a target or planned time and the recording covers the plan's distance
- **THEN** the time to the plan's finish line more than 2 % slower than the target is notable; a faster finish is named, not notable. A race given up short of the distance is unknown, never a fast finish.

#### Scenario: No usable plan or recording
- **WHEN** the plan is unavailable, a ride has no target power or power stream, a run's speed jumps, or the recorded distance differs more than 10 % from the plan's
- **THEN** the section, start and fade checks are unknown with that reason.

Evidence:
- `backend/performance/session_report.py:_RacePlan`
- `backend/coach/activity_read_tools.py:CoachActivityReadToolService._race`
- `tests/test_session_report.py`

### Requirement: Run block, zone time and facts
The session report SHALL add the run's own checks, zone time against the plan, decoupling against the athlete's own sessions and a list of facts without a verdict, with the athlete's decisions on #3084 (2026-11-16).

#### Scenario: Run checks
- **WHEN** the session is a run
- **THEN** the report judges:
  - the stored form drift, notable when ground contact rises 5 % from the first to the last third at the same pace;
  - pace in the session's heart-rate band, notable from 3 % slower than the median of at least 5 own comparable runs of 42 days, else unknown;
  - for interval runs, the stride length of each rep's second half, notable from 5 % shorter than the first;
  - each shoe of the run, notable from 90 % of its target distance (its own, Garmin's, else 700 km).

#### Scenario: Run steadiness against own runs
- **WHEN** an easy, recovery, endurance or long run has at least 5 comparable runs with a GAP variation
- **THEN** the coefficient of variation of its grade-adjusted speed over the moving time is notable above their upper quartile; with fewer runs the value is a fact only, never judged against a fixed bound.

#### Scenario: Ride checks
- **WHEN** the session is a ride
- **THEN** the report judges:
  - a continuous or easy ride's IF (NP over the session day's FTP) against Coggan's range of its focus (recovery under 0.55, endurance 0.56–0.75, tempo 0.76–0.87, sweet spot 0.84–0.97, threshold 0.91–1.05), notable outside it;
  - reps steeper than 4 % below 70 rpm, not applicable when the plan sets a cadence;
  - in ERG (the trainer held the power), the rep fade, within-rep fade and start checks as not applicable, never unknown, and the closing heart rate against at least 5 own sessions within 5 % of the rep power, notable from 3 % higher (athlete, confirmed 2026-11-16);
  - and states the reps in % of the session day's FTP as a fact.

#### Scenario: Zone time against the plan
- **WHEN** a run or ride has a paired planned unit with zone times
- **THEN** the seconds of each zone holding at least 10 % of the planned time are judged against the plan, notable beyond ±20 %; when the zone-targeted plan covers less than 80 % of the recorded zone time (free warm-up or cool-down), or the zone metric's data quality failed, the check is unknown.

#### Scenario: Decoupling against own sessions
- **WHEN** at least 5 comparable sessions (same sport and kind, duration ±35 %, heart rate ±8 bpm) carry a decoupling
- **THEN** decoupling is notable when above their upper quartile and above 5 %; with fewer the fixed 5 % decides.

#### Scenario: Facts without a verdict
- **WHEN** the report is built
- **THEN** `facts` states, without a verdict: an outdoor run's work laps in % of critical speed (a model estimate of the 28 days before the session's day), its running dynamics per pace band against own runs as the app rates them, the elevation of an outdoor session and stops over 30 s, flagged standstills or recording gaps (count and total).

Evidence:
- `backend/performance/session_report.py:_run_block`
- `backend/performance/session_report.py:_session_facts`
- `backend/performance/comparable_sessions.py:comparable_sessions`
- `tests/test_session_report.py`

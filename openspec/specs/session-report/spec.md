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
- **THEN** the report judges for runs and rides the duration (elapsed for interval sessions, whose planned rests the estimate counts, else moving) and the local TSS-scale load (M6, the scale of the plan estimate; never a provider load) against the unit's estimate, notable beyond ±20 %. For detected intervals it judges the planned work steps against the executed work laps (notable on missing or extra steps, with the mapping confidence); without a plan-to-lap mapping that check is unknown with the mapping's reason.

#### Scenario: Easy, recovery and long sessions
- **WHEN** the session focus is recovery, easy, endurance or long
- **THEN** the report judges:
  - aerobic decoupling, notable above 5 %;
  - the share of heart-rate zone time above zone 2, notable above 10 %; the paired plan's own seconds above zone 2 (strides, a fast finish) are taken off, unplanned surges count;
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

#### Scenario: RPE against intensity
- **WHEN** a run or ride carries a session RPE and a local IF
- **THEN** the report compares the RPE with the median RPE of the athlete's own rated sessions of the same sport and RPE scale within ±0.05 IF (5 points of the stored IF in %) over the 84 days before the session's day (a session later that day never counts), read once for that window; notable from a 2-point gap; unknown with fewer than 5 such sessions or without a rating or IF (#3084 Part 3).

#### Scenario: Data quality gates verdicts
- **WHEN** the heart-rate data shows spikes or a cadence lock or was not checked, or the speed of a speed-based output (a run, a ride without power) jumps
- **THEN** every check that reads those data is unknown with the data-quality reason instead of notable. Other unknown reasons pass on the owner's reason (comparable sessions, halves).

Evidence:
- `backend/performance/session_report.py:session_report`
- `backend/coach/activity_read_tools.py:CoachActivityReadToolService._with_session_context`
- `backend/performance/session_conditions.py:grade_adjusted_pace`
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

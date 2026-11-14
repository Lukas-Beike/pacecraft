## ADDED Requirements

### Requirement: CS14 Competition location
A competition SHALL keep an optional free-text location (city or postal code, at most 200 characters) for its race-day weather. The Coach's `save_competition` SHALL keep it when an edit leaves it out and clear it on an empty string; change history and undo SHALL carry it, and changes recorded before the location existed SHALL stay undoable. The location stays local and never enters an Intervals.icu payload.

#### Scenario: Edit without the location
- **WHEN** a competition with location "Berlin" gets only a new target
- **THEN** the location stays "Berlin"; an explicit empty location clears it.

#### Scenario: Earlier change after the upgrade
- **WHEN** a competition change recorded before schema v29 is undone after the upgrade
- **THEN** the undo is accepted, and a later edit of the competition still blocks it as stale.

#### Scenario: Card fact
- **WHEN** the athlete expands a competition with a location
- **THEN** the card shows it as "Ort" with the other saved details.

Evidence:
- `backend/change_history.py:_FIELDS_ADDED_TO_AUDIT`
- `backend/planning/competition_service.py:_coach_payload`
- `public/plan-views.js:competitionCard`
- `tests/test_history_undo_service.py::test_legacy_profile_row_stays_undoable_and_later_edits_still_conflict`
- `tests/test_planning_competition_service.py::test_empty_values_clear_optional_fields_and_absent_ones_keep_them`

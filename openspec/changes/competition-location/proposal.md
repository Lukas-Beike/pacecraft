# Proposal

## Why

Part 2 of #2803. The race plan (part 1, #2857) lacks the weather outlook for race
day. The athlete chose a location of the competition's own over the profile
weather location (#2803 comment of 2026-11-14), which needs a schema change; the
coordinator approved schema v29 with conditions after an independent review.

## What Changes

- Schema v29: `competitions.location TEXT NOT NULL DEFAULT ''` (at most 200
  characters), added by one frozen `ALTER TABLE` step from the released v28
  shape. v28 (releases 2.1.10 to 2.1.14, production) gets its frozen digest, so
  the chain is v26 -> v27 -> v28 -> v29; older, unknown and newer schemas stay
  rejected unchanged. One-way door: release 2.1.14 cannot open a v29 database.
- The Coach's `save_competition` takes `location` (city or postal code); edits
  that leave it out keep it, an empty string clears it; change history and undo
  carry it, and changes recorded before v29 stay undoable.
- The weather sync job (startup, daily when due, manual) fetches the race-day
  forecast for each upcoming competition with a location within the 14-day
  horizon: at most 5 places within 60 s per run, outside the profile forecast's
  lock, never from a request, also without a profile weather location. One
  key/value entry keyed by competition id, location and event date holds the
  race-day row; deleted, moved or past entries are pruned; the place never
  reaches the logs.
- The race plan adds a weather block: the race-day row with "Open-Meteo ·
  <place>, <country>", or a short status (no forecast yet, place not found,
  forecast unavailable); nothing without a location. The card shows "Ort".
- Privacy export format 20 carries the location; the new key is exported.

## Capabilities

### New Capabilities
- none

### Modified Capabilities
- `race-plan`: weather part of the race plan.
- `competitions-and-seasons`: competition location field.
- `weather-and-location`: race-day forecasts at competition locations.
- `encrypted-storage-and-migrations`: released v27/v28 shapes and the v29 step.

## Impact

`backend/db/{schema,schema_history,migrations,repositories}.py`,
`backend/planning/{competitions,competition_service,race_plan}.py`,
`backend/change_history.py`, `backend/coach/tool_schemas.py`,
`backend/weather/{competition_forecast,wiring}.py`,
`backend/sync/{weather,schedule_policies,scheduler_wiring}.py`,
`backend/backup/export.py`, `public/plan-views.js`; docs and AGENTS.md (production
schema v28). No remote write; Intervals.icu payloads unchanged.

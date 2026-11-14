# Proposal

## Why

#2804: Garmin reports an estimated sweat loss per session and the app stores
it, but nothing reads it. Long planned run and ride units should show how much
to drink and how many carbohydrates to take, from the athlete's own sweat rate
and the day's forecast, with the basis visible.

## What Changes

- The activity summary carries the stored `water_estimated_ml` of a session
  (read through the activities owner).
- The nutrition fuelling rules (one owner with the race plan, #2803) gain the
  drinking rule the athlete chose (#2804 comment of 2026-11-14): personal sweat
  rate as the median of the last 365 days of sessions of the same sport and
  setting; outdoors only sessions within ±5 °C of the forecast day temperature
  when at least 3 exist, else all with a note; intake = expected loss minus 2 %
  of body weight, per hour, never above the sweat rate; from 2 h a sodium note
  of 0.5–0.7 g per litre (Sawka et al. 2007; McDermott et al. 2017). Without
  own data the ACSM starting range 0.4–0.8 L/h is shown as such.
- Carbohydrates per hour reuse the race-plan steps by planned duration.
- A read route `GET /api/nutrition/session-fuelling` computes the
  recommendation for a sport, setting, date and planned duration from local
  data only (stored summaries and activity weather, the cached forecast, the
  resolved weight); it fetches nothing.
- The opened card of a planned run or ride unit of at least 60 min shows a
  "Verpflegung" block (athlete layout A) with the basis as badges.

## Capabilities

### New Capabilities
- `session-fuelling`: drinking and carbohydrate recommendation for long planned units.

### Modified Capabilities
- none

## Impact

`backend/activities/activity_summary.py`, `backend/canonical/activity_summary.py`,
`backend/nutrition/fuelling.py`, new `backend/nutrition/session_fuelling.py`,
`backend/http_api/nutrition.py` and wiring, `public/plan-views.js`. No schema
change, no remote write, no Garmin hydration write.

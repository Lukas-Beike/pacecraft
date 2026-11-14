## ADDED Requirements

### Requirement: RP5 Race-day weather in the race plan
The race plan SHALL include the stored race-day forecast of the competition's location with the geocoded place and country, or a status naming what is missing: `no_location`, `not_in_horizon`, `pending` (not fetched yet), `not_found` or `unavailable`. Reading the plan SHALL never fetch a forecast.

#### Scenario: Forecast stored
- **WHEN** the weather job stored the race-day row of the competition's current location and date
- **THEN** the "Wetter" block shows condition, temperature range, wind and rain probability with the badge "Open-Meteo · <place>, <country>".

#### Scenario: Nothing to show yet
- **WHEN** race day is beyond the forecast days, the job has not run, or the place was not found
- **THEN** the block shows only "Noch keine Vorhersage", "Ort nicht gefunden" or "Vorhersage nicht verfügbar"; without a location there is no block.

Evidence:
- `backend/planning/race_plan.py:read_race_plan`
- `backend/weather/competition_forecast.py:race_day_weather`
- `public/plan-views.js:racePlanWeather`
- `tests/competition-overview.test.cjs` (race plan weather)
- `tests/test_planning_race_plan.py` (weather part)
- `tests/test_weather_competition_forecast.py` (statuses)

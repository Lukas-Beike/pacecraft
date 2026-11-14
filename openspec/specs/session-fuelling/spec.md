# session-fuelling Specification

## Purpose
Recommend drinking and carbohydrates for long planned run and ride units from
the athlete's own stored sweat losses, the forecast and the shared fuelling
rules, with the basis visible and local data only.

## Requirements

### Requirement: SF1 Personal sweat rate
The sweat rate SHALL be the median of Garmin's sweat-loss estimate per hour of the stored sessions of the last 365 days with the same sport and setting (indoor or outdoor). Outdoors, only sessions whose stored activity-weather temperature lies within 5 °C of the forecast day temperature SHALL count when at least 3 exist; otherwise all count and the basis says so.

#### Scenario: Similar sessions
- **WHEN** six outdoor rides lie within 5 °C of the forecast and others are hotter
- **THEN** the median of those six is the sweat rate, labelled with their count and temperature range.

#### Scenario: Too few similar sessions or indoors
- **WHEN** fewer than 3 sessions match the forecast, there is no forecast, or the unit is indoors
- **THEN** all sessions of the sport and setting count, without a temperature band.

Evidence:
- `backend/nutrition/fuelling.py:sweat_rate`
- `backend/nutrition/session_fuelling.py:sweat_samples`
- `backend/weather/activity_weather_store.py:session_temperatures`
- `tests/test_activity_weather.py::StageTests::test_start_position_is_fetched_rounded_and_stored`
- `tests/test_nutrition_fuelling.py` (sweat rate)
- `tests/test_nutrition_session_fuelling.py` (samples and settings)

### Requirement: SF2 Drinking and carbohydrates per long unit
For a planned run or ride of at least 60 minutes the recommendation SHALL be the expected loss minus 2 % of body weight per hour and in total, never above the sweat rate, with 0.5–0.7 g sodium per litre from 2 h (Sawka et al. 2007; McDermott et al. 2017); without own data the starting range 0.4–0.8 L/h; carbohydrates by the race-plan steps. Reading it SHALL fetch and store nothing.

#### Scenario: Long ride with weight
- **WHEN** a 2:45 h ride is planned, the sweat rate is 0.9 L/h and the weight 70 kg
- **THEN** the card shows 0.39 L per hour, 1.1 L in total with the sodium range, and 60–120 g/h with the 90 g/h flag.

#### Scenario: Missing inputs
- **WHEN** no weight is resolved, or no session has a sweat loss
- **THEN** only the upper bound "bis" the sweat rate, or the starting range labelled "Startwert ohne eigene Daten", is shown; another sport or under 60 min shows no block.

#### Scenario: Card block
- **WHEN** the athlete opens a planned run or ride of at least an hour, today or later
- **THEN** a "Verpflegung" block below the steps shows drinking and carbohydrates with their basis as badges; a failed read offers "Erneut laden".

Evidence:
- `backend/http_api/nutrition.py:NutritionGetRoutes._fuelling`
- `backend/nutrition/fuelling.py:drinking`
- `backend/nutrition/session_fuelling.py:read_session_fuelling`
- `e2e/calendar-detail-pane.spec.js` (long planned unit)
- `public/plan-views.js:plannedFuellingBlock`
- `tests/competition-overview.test.cjs` (long planned units)
- `tests/test_activity_detail_contract.py` (summary sweat loss)
- `tests/test_nutrition_api_and_sync.py` (session fuelling route)
- `tests/test_nutrition_session_fuelling.py` (read and invalid queries)

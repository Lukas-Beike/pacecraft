## ADDED Requirements

### Requirement: Race-day forecasts at competition locations
The weather sync job SHALL fetch the race-day forecast of each upcoming competition with a location whose race day lies within the forecast days, also without a profile weather location, at most 5 places within 60 s per run and outside the profile forecast's refresh lock. No request SHALL fetch it. The stored entry SHALL be keyed by competition id, location and event date, and the location SHALL never be logged.

#### Scenario: Competition without profile location
- **WHEN** no profile weather location is saved and a competition in 5 days has a location
- **THEN** the job is scheduled, fetches that place once and stores the race-day row with the geocoded place and country.

#### Scenario: Failures and limits
- **WHEN** a place cannot be geocoded, the provider fails, or more than 5 places are due
- **THEN** the place is `not_found`, an earlier forecast of the same place and day stays, otherwise it is `unavailable`, and places beyond the limit or the time budget wait for the next run; the profile forecast is unaffected.

#### Scenario: Changed or past competitions
- **WHEN** a competition is deleted, moved, relocated or past
- **THEN** its stored entry no longer matches and is dropped on the next run.

Evidence:
- `backend/sync/schedule_policies.py:WeatherSchedulePolicy`
- `backend/sync/weather.py:WeatherSyncService`
- `backend/weather/competition_forecast.py:refreshed_cache`
- `tests/test_schedule_policies.py` (competition location configures the job)
- `tests/test_sync_weather.py` (with and without profile location)
- `tests/test_weather_competition_forecast.py` (due races, one fetch per place, failures, limits, store)

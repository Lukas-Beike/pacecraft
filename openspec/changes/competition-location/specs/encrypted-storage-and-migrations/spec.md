## ADDED Requirements

### Requirement: Released schemas enter the chain by their digests
A database of a released later schema, v27 (releases 2.1.2 to 2.1.9) or v28 (releases 2.1.10 to 2.1.14), SHALL be recognized by its frozen shape digest and upgraded through the remaining steps to the current schema inside the one migration transaction. The v29 step SHALL add `competitions.location` (empty for every stored competition, at most 200 characters, never NULL) from a frozen copy of its DDL.

#### Scenario: Populated v28 database
- **WHEN** a v28 database with competitions and calendar decisions is opened by this build
- **THEN** it reaches the exact current schema, every original column value of every table is unchanged, and each competition has an empty location.

#### Scenario: Failing v29 step
- **WHEN** the v29 step fails after its DDL ran
- **THEN** the database keeps version 28, its shape and every row, and a later start upgrades it.

#### Scenario: Restart and backups after the upgrade
- **WHEN** an upgraded database restarts with the same build, or a v28 backup is restored
- **THEN** the restart changes nothing, and the restored backup is upgraded to the current schema with its competitions and an empty location.

Evidence:
- `backend/db/migrations.py:_competition_location`
- `backend/db/schema_history.py`
- `tests/test_database_restore_validation.py::test_validate_accepts_a_populated_baseline_backup`
- `tests/test_db_migrations.py::test_encrypted_released_database_upgrades_and_restarts_with_its_rows`
- `tests/test_db_migrations.py::test_failing_step_rolls_back_the_released_database_and_a_retry_succeeds`
- `tests/test_db_migrations.py::test_frozen_v28_shape_is_v27_after_its_decision_step`
- `tests/test_db_migrations.py::test_populated_v28_database_gains_an_empty_location_with_every_row`
- `tests/test_db_migrations.py::test_upgraded_database_restarts_with_the_same_build_unchanged`

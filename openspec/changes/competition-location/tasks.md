# Tasks

## 1. Schema v29

- [ ] 1.1 Freeze the v28 shape digest (fixture through the chain equals a fresh v28 schema), add the frozen v29 location step and the live ALTER, regenerate `docs/schema-reference.md`.
- [ ] 1.2 Evidence: populated v26/v27/v28 upgrades, injected failure leaves v28, same-build restart, newer schema rejected, v28 backup restore; run the SQLCipher container suite where available and record gaps.

## 2. Location field

- [ ] 2.1 Repository, normalization, Coach payload and tool schema, change history with the audit generation, privacy export format 20 and its tests.

## 3. Race-day weather

- [ ] 3.1 Competition forecasts in the weather sync job with limits, scheduling without a profile location, store and data-safety classification, with tests.
- [ ] 3.2 Race plan weather part and card block with frontend and browser tests.

## 4. Docs and validation

- [ ] 4.1 AGENTS.md, the db-migrations skill, README, operations, schema history, provider endpoints and features docs name production v28 and the new behaviour.
- [ ] 4.2 Run focused tests, the changed-test selection, compileall, the quality baseline, Node tests and the affected Playwright projects; strict OpenSpec validation.

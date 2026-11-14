# Tasks

## 1. Decisions

- [ ] 1.1 Record the athlete's method decision (drinking, temperature, carbohydrates, minimum duration) and the layout decision on #2804.

## 2. Rules and reads

- [ ] 2.1 Expose the stored sweat loss on the activity summary and add the sweat-rate and drinking rules to `backend/nutrition/fuelling.py` with hand-worked tests.
- [ ] 2.2 Add `backend/nutrition/session_fuelling.py`, the activity-weather temperature reader and `GET /api/nutrition/session-fuelling` with tests; regenerate `docs/api-routes.md`.

## 3. Card

- [ ] 3.1 Show the "Verpflegung" block (layout A) when a long planned run or ride is opened, with frontend and browser tests.

## 4. Docs and validation

- [ ] 4.1 Update `docs/features.md` and `docs/canonical-field-map.md`.
- [ ] 4.2 Run focused tests, the changed-test selection, compileall, the quality baseline, Node tests and the affected Playwright projects where available; strict OpenSpec validation.

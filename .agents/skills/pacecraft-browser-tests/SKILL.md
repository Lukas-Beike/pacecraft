---
name: pacecraft-browser-tests
description: Base skill. Run the Playwright browser tests for the affected project with the isolated runner. Use after changing public/ or e2e/; not for Python tests (pacecraft-run-tests-changed).
---

# Run browser tests

The runner gives every spec its own disposable SQLCipher fixture; do not call
`npm run test:e2e` against a live installation. Projects: `mobile` (390x844),
`tablet-landscape` (844x390), `desktop` (1440x1000). One-time setup is in
[docs/testing.md](../../../docs/testing.md#playwright-browser-e2e-tests):
install the locked Chromium and build the app image.

```powershell
python scripts/run_browser_tests.py --project changed --list
python scripts/run_browser_tests.py --project <project> --spec e2e/<name>.spec.js
python scripts/run_browser_tests.py --project all --keep-going
```

- `--project` is required (one project name, `all`, or `changed`); `--spec`
  repeats. `changed` picks what CI picks for the diff against `--base`
  (default `origin/develop`), from `.github/scripts/ci_changes.py` (#2759):
  nothing for documentation only (exit 0, no run, as CI), `desktop` without a
  UI change, otherwise `all`; one project takes 28-32 min locally (`--jobs 1`), so
  locally run the affected specs on desktop and leave `all` to CI (#3983). Run the
  smallest selection that covers the change: the affected spec on the affected
  project, then the other viewports for layout changes. A backend read or
  projection change affects the specs of every view that shows the read, not
  the files the diff touches: grep `e2e/` for the view's data selectors (such
  as `data-body-metric`) and check `e2e/fixture_runtime.py` for seeded rows the
  change starts or stops serving (#3315). A UI change that adds, removes or
  relabels a segment, control or shared component runs the specs that assert
  it: grep `e2e/` for the changed ids, classes and visible labels (such as
  `data-analysis-segment`, `analysis-period-controls` or the button text).
  Specs far from the change assert exact segment lists and control counts
  (#3819). A change to `STEPS` in
  `scripts/ui_walk.mjs` also runs `e2e/ui-walk-fixture.spec.js` on at least one
  project: it runs every walk step against the fixture and fails a step the
  fixture cannot satisfy (#3416). `--list` resolves the
  selection without Docker or a browser. A spec a selected project does not
  list (a desktop-only spec on mobile) is skipped there with a note.
- Flaky test: `--repeat-each N --grep "<title regex>"` repeats one test in its
  spec's child without editing `playwright.config.cjs` (no `repeatEach` or
  `test.only` edits, #3094). Repeats share the child's fixture, so a test that
  needs fresh state (an empty chat) fails from the second repeat; narrow with
  `--grep`. The runner never reads a child's console output. To read a value,
  write it to `testInfo.outputPath("<name>")` in the spec; the file lands under
  the run's `artifacts/` in `test-results/browser-runs/`. A flake seen only in
  CI needs a draft PR: the CI browser (`--browser-image`) needs a Linux host.
- A browser test the PR adds or changes that fails even once in that PR's CI
  runs `--project <failing> --spec <its spec> --repeat-each 20 --grep "<its
  title>"` (only that test; see the fresh-state note above) before auto-merge,
  with the result in the PR body; a passing rerun alone is no evidence (#3458).
- Weekday: `--today YYYY-MM-DD` pins the fixture's athlete day and, in specs
  that import `test` from `e2e/helpers/today.js`, the browser clock (#3200).
  The runner names each selected spec without the helper ("--today pins only
  the fixture day for …"): its browser shows the real day, so it is no weekday
  evidence (#3410).
  A spec whose expectation depends on the weekday (the week's completed units,
  yesterday) imports it and computes dates outside the page with its
  `todayIn(zone)`. Without `--today`, the runner also runs such a spec, when
  it is changed and selected, on desktop on this week's Monday and Sunday, as
  CI does, and prints the three exits together (#3324).
- A server-free spec (one that only uses `page.setContent`, such as
  `e2e/ui-walk-steps.spec.js`) runs without Docker: set `E2E_NO_LOGIN=1` and
  run `npx playwright test e2e/<name>.spec.js --project=desktop`. The login
  setup is skipped and every test gets an empty session; a spec that needs the
  app fails without a server. CI refuses the switch, and without it a missing
  `E2E_APP_PASSWORD` still stops the run at once (#3026).
- Rebuild the image (`docker build -t "$(python scripts/run_browser_tests.py --print-image)" .`)
  after application changes, or pass `--build` for local runs: it builds a
  missing or stale image, then checks it again (#3106). Without it, a run
  against an image that differs from the source stops before any fixture starts.
- "The Docker engine does not answer" (#3304): Docker Desktop is stopped or
  stuck "starting" (its `docker-desktop` WSL distro stopped), and every Docker
  call answers HTTP 500. Start it; if it stays "starting", quit it, run
  `wsl --shutdown` and start it again. That stops every lane's containers, so
  tell the other lanes first.
- "Chromium of the locked Playwright … is not installed" (#3342): after a
  Playwright bump the native browser is missing, because `npm ci --ignore-scripts`
  installs none. Run `python scripts/prepare_worktree.py` (or
  `node node_modules/@playwright/test/cli.js install chromium`).
- Exit 0: passed. Non-zero: a test failed (reports under
  `test-results/browser-runs/`), the host ran out of resources, or the image is
  stale (no reports; rebuild as the message says); the output says which and how
  to rerun. Report what was not run.
- Reports and traces can hold fixture data: never publish them.
- A regression test for a wrong state that a later re-render corrects checks it
  once, right after the triggering response or call (`waitForResponse`, then
  `isVisible()`, or one `page.evaluate`), never with a polling `toBeHidden()`.
  A poll waits until the next unrelated re-render hides the bug. Run it red
  against the unfixed code first (#3824, #3828).
- A stopped or killed run cannot clean up after itself. Each run holds a lock
  file in the system temp directory; the next run, or
  `python scripts/run_browser_tests.py --cleanup-stale`, removes the containers
  of runs whose lock nobody holds. Live runs and runs without a lock file on
  this host are never touched (#2721).
- Long runs: the complete selection takes about 24 min with `--jobs 1` and
  15 min with `--jobs 2` (#2149); one project with `--jobs 2` took 15-19 min on
  a loaded host (#2515). Start such a run with the long-run rule of
  [validation](../pacecraft-validation/SKILL.md).

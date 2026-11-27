---
name: pacecraft-ui-walk
description: Base skill. Headless read-only walk through the running app at phone and desktop width with the secret-file login, screenshots, load times, calendar week/month/paging, per-step checks of each sub-function (charts, panels, selects) and the served service-worker cache name. Use in the post-release check or after a restart.
---

# UI walk

Needs the repository's Playwright install (`npm ci` and
`npx playwright install chromium` once). The app address and the secret file
come from the session at run time. The secret file must lie in the T3 secret
folder (`~/.t3/userdata/secrets`) or the system temp directory, and is only
read; the output directory and the previous walk must lie in the system temp
directory (the session scratchpad). The script refuses any other path. Each walk
needs a fresh output directory: a file already at one of its names stops the
walk with exit 2.

```powershell
node scripts/ui_walk.mjs --url-file <scratchpad>/app-url.txt --password-file <secret file> --out-dir <scratchpad>/walk [--compare <previous walk>]
```

To explore beyond the fixed steps, `scripts/ui_explore.mjs` takes the same three
arguments plus `--plan - < plan.json` (the plan on standard input); its header
lists the step kinds (#3913).

`--compare` takes the previous walk's out-dir or its `ui-walk.json`; an
unreadable one stops with exit 2 and names the file it expected (#3346).

At 412 px and 1280 px it logs in through the login dialog with the password
read from the file, opens Coach, Kalender, Analyse, Ernährung and Mehr, opens
one calendar unit (the detail pane at desktop width), switches to the month and
pages it, pages the week, and logs out with the CSRF token. One line per view
with its time until ready and error count (console errors, page errors,
5xx responses), the calendar steps done, and the service-worker cache name and
asset-version count. A view is ready once its active panel shows no visible
loading marker and no request it started has been open for 300 ms (the state
event stream excepted); after 15 s an `info <view> not ready` line names the
method and path of that view's requests still open. `ui-walk.json` keeps the
numbers and the measuring method for `--compare`, which skips load times of a
walk measured another way; keep the load times in the session notes, not in
the repository.

Each view also runs its sub-function steps (`STEPS` in `scripts/ui_walk.mjs`,
#3009): a calendar unit (week and month, completed and planned), "Details",
chart, zone bar, running dynamics, the equipment select and fuelling, the
Analyse tabs, Ernährung Verlauf and Körper, Mehr Ausrüstung and the changelog,
and the Coach history. A step opens its controls, then waits for its element and
checks it is not empty (chart: drawn marks; numbers: a digit; options: a select
with two or more; text: any), and times interaction to ready. One line per step
at both widths: `ok|FAIL <width> <step> <ms> ms`; `info ... skipped` when the
data does not exist (no planned unit, no recorded running dynamics); a missing
or empty element where data exists is `FAIL` and names the feature it covers.
`--compare` prints each step's time against the previous walk and a `FAIL` for a
step that passed before and is still skipped after one retry 20 s later (an
`info` line names the retry), whatever the reason. Optional steps
are skipped without data, so a skipped step in the notes is read, not ignored.
A step that needs a kind of unit looks for one that has its element: completed-
unit steps page back, planned-unit steps (Schuh/Rad geplant, Löschen,
Verpflegung) page forward, week by week, and each returns to the current week.
While the view shows it is loading (a loading marker, `aria-busy`, or an app
data load), a step waits for its element up to 60 s instead of its cap. Before
walking, it waits while the diagnostics' `curve_build.status` is `building` or
`pending` (a release's one-off rebuild slows every read; on `pending` it starts
the build with the Analyse view's own read), at most 10 min, and prints
`info curve build <state>, waited <s> s`.

To add a step, append one object to `STEPS`: `view`, `name`, `covers` (feature or
issue), optional `pre` (open once), `each` (candidates tried in turn) and `opens`
(open inside the candidate), `seek` (a paging control pressed, at most six
times, while no candidate has the element) with `unseek` (pressed as often
afterwards), `ready` (selector), `kind`, `optional`, `absent`, and `since`: the
first release version that carries the feature (the next release when the step
is added). A walk against an older release prints `info <step> skipped: not in
<version>` instead of a FAIL; the e2e fixture walk runs every step (#3886).
Controls are limited to opening, expanding, paging and tab controls
(`isOpener`), and a label that names save, sync, send, delete, export, undo or
settings is refused; choosing a select value is never part of a step. The
e2e spec `e2e/ui-walk-steps.spec.js` pins that an empty chart or panel fails;
`e2e/ui-walk-fixture.spec.js` runs every step against the fixture app, so a new
step passes there or is named in its `FIXTURE_SKIPS` with the reason (#3067).

Look at every screenshot yourself; a view can render an error state without a
console error. Screenshots stay in the scratchpad and are never committed.

To check a change to the walk before a release, walk a disposable demo app
instead (#2689): start it with
`powershell -ExecutionPolicy Bypass -File scripts/demo-container.ps1 -Name fixture-walk -Port 8092`,
write the fixture password from the script header to a scratchpad file, walk
`http://127.0.0.1:8092`, and remove it with the same command plus `-Stop`. It
uses only synthetic data, never the real data directory, `.env` or the token
store.

For a start-up or curve performance measurement (#2801), add
`-HeavyHistory 900` (900 synthetic 1.5 h rides and runs with full streams and
analyses over several years; the first start seeds for about 5 minutes). For
numbers that stand for production, use the production profile
`-HeavyHistory 850 -HeavyWalks 2950 -HeavyYears 6` (#2974).
Measure "right after a restart" with `docker restart fixture-walk`, then the
walk with `--compare` against the previous walk; note other running containers,
they share the CPU. To see which thread holds the interpreter once the app is
ready, run `docker kill -s USR1 fixture-walk` and read the stacks with
`docker logs`; before readiness the signal stops the container.

It presses no control that saves, synchronizes, deletes or sends. Run it from the deployed
release commit: another `APP_VERSION` stops it first (#4263). Exit 0 no error, 1 a view
logged errors, a step failed or logout failed, 2 bad input or the walk stopped.

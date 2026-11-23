---
name: pacecraft-prod-diagnostics
description: Base skill. Read named health and diagnostics fields and record counts from the running app, read-only, optionally compared with a previous result. Use in the post-release check or after a restart; never to change production.
---

# Production diagnostics

The app address and the secret file come from the session at run time (the
athlete's private notes and a secret delivered with file delivery); never write
either into the repository, a tracked file, or output. The secret file must
lie in the T3 secret folder (`~/.t3/userdata/secrets`) or the system temp
directory, and is only read; result files must lie in the system temp
directory (the session scratchpad). The scripts refuse any other path.

```powershell
python scripts/prod_diagnostics.py --url-file <scratchpad>/app-url.txt --password-file <secret file> --expect-version <tag> --expect-schema <current schema> --out <scratchpad>/diagnostics.json [--compare <previous result>]
```

It checks health, logs in with the password read from the file, reads
`/api/diagnostics`, and logs out with the CSRF token. One line per check:
version and schema against the expected values, whether `sync.last_error` is
set (never its text), provider and calendar refresh ages, stuck plan-sync
jobs of every provider (`plan_sync.stuck_jobs`; unknown fails), unsynced plan units with error kinds (never error text), the plan
pushes per provider of the last 7 days (job and item statuses, settled items
included, and error kinds; #2878), record counts, process memory, storage and the newest app backup (`--expect-backup-hours N`
fails unless a backup the app made is younger and its check passed, #2996). With
`--compare`, a lower record count is a `FAIL`: an unexplained drop stops the
check. `external_calendar_events` is only `info`:
the calendar sync refills it from today on every refresh. `--check-docs`
compares the production schema and version with the production line in
`AGENTS.md` and the db-migrations skill and with the `docs/schema-history.md`
row, and a stale one fails with the exact change to make (#3351);
`--expect-schema` turns it on, so a post-release run cannot leave it out (#3454).

Exit 0 all ok, 1 a check failed, 2 unreachable or login refused. The script
prints no response body, cookie, or password; do not add a mode that does.

## Sections

```powershell
python scripts/prod_diagnostics.py --url-file <scratchpad>/app-url.txt --password-file <secret file> --section <name> [--section <name> ...]
```

Prints, instead of the checks, only the fields of those sections that
`SECTION_FIELDS` in the script allows: numbers and flags by dotted path, state
words from `STATES`, and a `*_at` time as its age `*_hours`, sorted by path. A
state word in `FAIL_STATES` (`failed`, `unavailable`, `blocked`) and a missing
section print as `FAIL`; an empty section is `info`. Free text, ids and any
field not on the allow-list are never printed. Sections with an allow-list:
`backup`, `curve_build`, `database`, `runtime`, `recovery_copies`,
`garmin_past_workout_removal`, `garmin_strength_push`, `stream_backfill` and
`fit_inventory`. A section without an allow-list entry is refused; add the
entry and its test in `tests/test_prod_checks.py` together with the section.
A new top-level diagnostics block fails
`test_every_block_the_report_emits_has_its_section` until it has its entry, or
a named reason in that test's `NOT_SECTIONED`, so the post-release check can read
every block a PR adds (#3430).
`--wait-ready <seconds>` (with or without `--section`) first polls
`curve_build.status` every 15 s and prints only the state and the wait:
`ready` continues, a failed or missing build exits 1, and a build still
`pending` or `building` when the time runs out exits 3 (#3346). On `pending`
it makes the Analyse view's own read once (as the UI walk does), which starts
the build; it reads no answer body.
`DERIVED_SECTIONS` prints sums instead of leaves: `coach_local_first` counts
the #3080 local-first outcomes over the Coach history (`coach_commands`);
the session resolutions are in `--section runtime`. `route_timings` prints
`runtime.slowest_routes` since start, one line per route pattern with `count`,
`p50_ms`, `p95_ms`, `max_ms` and `disconnects` (#3630); a view that loads slower
names its slow request there.

## Temporary production probe

When a verification would need the production database or the athlete, add a
probe instead (#2779):

1. Add a counts-only diagnostics section that computes the numbers in the
   background, one session per read snapshot with a pause after each (duty
   cycle, #2761), `pending` until the first result; a test pins that it holds
   no ids, names, dates or single values. Open its removal issue at the same time.
2. Add the section's allow-list to `SECTION_FIELDS` with its test.
3. After the release, read it with `--section <name>`; while it is `pending`,
   read again later.
4. Present the counts, then remove the section and its allow-list through the
   removal issue.

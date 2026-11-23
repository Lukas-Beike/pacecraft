---
name: pacecraft-post-release-check
description: Check production after every deployment, or on request after a restart - version, schema, record counts, guards, logs, server health, backup age and a UI walk - by sequencing the prod base skills; read-only apart from a few immediately undone local actions. Do not use to deploy, repair or change production.
---

# Post-release check

Run after every deployment (step 5 of
[release-unraid](../pacecraft-release-unraid/SKILL.md)) and on request after a
restart. Report one short line per step: `ok` or the finding. Every read
goes through a tracked script (`scripts/`); host state that the app cannot see
(running digest, container memory, appdata total, the log before the app
logger starts) comes from the Unraid MCP, read-only.

**Inputs at run time only:** the app address (the athlete's private notes) and
the app password as a secret file delivered to the session (`request_secret`
with file delivery). Never write either into the repository, a tracked file,
a command line (the address goes in a scratchpad file for `--url-file`, #4125), a tool argument, or output. T3 stores the delivered secret
file under `~/.t3/userdata/secrets`; pass that path as `--password-file` to
the scripts that take one (they read it only from there or the system temp
directory), and never read, print or copy the file; the shell guard blocks
other mentions of that folder it can recognise in the command text (#3758). Result files, saved logs and screenshots stay in
the session scratchpad (the system temp directory; the scripts refuse other
paths) and are never committed. The
previous release's results come from the session notes. Whether the release
carries a given fix: [release-contains](../pacecraft-release-contains/SKILL.md).

**Current checkout:** run every step from one checkout at `origin/develop`; an
older one runs old checks and an old UI walk against the new release (#3344).
`prod_diagnostics.py` and `prod_guards.py` (steps 2 and 3, before the walk)
print `WARN this checkout is N commit(s) behind origin/develop` with the
command that fixes it, and refuse with `--require-current` (exit 2); a warning
there also holds for the walk. They compare the local git state only, so fetch
first.

**Stop rule:** on an unexpected effect (a dropped record count, a failed
guard, an undo that does not fully restore, any remote write, an error state),
stop, report it, and do not try to repair production.

## Steps

1. **Container:** healthy and running the verified release digest:
   [image-verify](../pacecraft-image-verify/SKILL.md) with `--running` (after a
   restart without a release, the running digest equals the current tag).
2. **Diagnostics and no data lost:** [prod-diagnostics](../pacecraft-prod-diagnostics/SKILL.md)
   with `--expect-version <tag> --expect-schema <current schema> --compare <previous result>`
   (`--expect-schema` also runs the docs check, #3454), with tags fetched
   (`git fetch --tags`). A failed
   docs line (`AGENTS.md`, the db-migrations skill, `docs/schema-history.md`)
   is a stale document, not a production error: it does not stop the check;
   [release-unraid](../pacecraft-release-unraid/SKILL.md) opens the docs PR.
   Any unexplained drop in a record count stops the check. Judge the plan-sync
   error kinds against the previous result: whether they persist after the
   athlete's week sync; never press a sync control to find out. The `backup`
   line shows the newest backup the app made; a schema release requires one
   younger than 24 h with a passed check before the deploy
   ([release-unraid](../pacecraft-release-unraid/SKILL.md) step 6, #2996).
3. **Protection:** [prod-guards](../pacecraft-prod-guards/SKILL.md): 401 without
   a session, 403 without the CSRF token, TLS days to expiry. A failed guard
   stops the check.
4. **Logs:** run [prod-logs](../pacecraft-prod-logs/SKILL.md) with the app's
   own redacted entries (`--url-file`, `--password-file`): counts and event
   names only, the removed past app-created Garmin workouts after the first
   Garmin refresh since the deploy, and plan-sync error events. The durable
   removal counts come from `python scripts/prod_diagnostics.py --url-file
   <scratchpad>/app-url.txt --password-file <secret file> --section garmin_past_workout_removal`. The Unraid MCP
   logs call ignores `level=` and truncates at about 40 KB, so a saved container
   log covers only the latest window: use it (`--log-file`, then delete it) for
   startup errors, and say which window the counts cover.
5. **Server health:** the step 2 run prints the `storage` lines with the
   previous release's values: database and write-ahead log size, free disk
   space, `uptime_s`, `malloc_arena_max` (must be 2) and the recovery copies.
   For the full fields: `python scripts/prod_diagnostics.py --url-file <scratchpad>/app-url.txt
   --password-file <secret file> --section database --section runtime
   --section recovery_copies`. Compare the appdata total with the previous
   release through the Unraid MCP (host state: logs, token store, staging).
   Compare the RSS (`process_memory.rss_bytes`) at its `uptime_s` with the
   readings at a similar uptime in #2526 (about 790 MB at 5 h, 1.02 GB at 14 h
   before the setting) and with the ceiling in `docs/operations.md`; RSS still
   climbing with uptime is a finding. Report the recovery copies' count, newest
   age and size; the app reads them by stat only. Container memory as the host
   sees it comes from the Unraid MCP, read-only.
6. **UI walk:** first wait for the all-time curve build with
   `python scripts/prod_diagnostics.py --url-file <scratchpad>/app-url.txt --password-file
   <secret file> --wait-ready 900 --section curve_build` (exit 3: still
   building after 15 min, a finding; never a hand-written polling loop, #3346).
   Then [ui-walk](../pacecraft-ui-walk/SKILL.md) with `--compare
   <previous walk's out-dir>`. Look at every screenshot: Coach, Kalender (week, month,
   paging, detail pane at desktop width), Analyse, Ernährung, Mehr. After a
   deploy the service-worker cache name must differ from the previous walk
   (new asset versions served). Keep the load times per view in the session
   notes for the next comparison. Read the per-step lines too: a `FAIL` step
   (a chart or panel missing or empty, or a step that passed last time and is
   skipped now) is a finding; keep each step's time and note every `skipped`.
   When the Analyse all-time cards still load, `--section curve_build` shows
   whether a build runs and for how long (`building_s`, compare with the last
   `last_build_s`) or failed (`failed_age_s`, `failures`).

## Allowed actions

Only after steps 1-6 passed, and only through the tracked scripts, which log
in from the secret file (the password never passes through a tool argument or
the agent's context):

- operate views: switch week and month, page weeks, open units and analyses
  (the `ui-walk` steps do this);
- ask the Coach one question without a change:
  [post-release-actions](../pacecraft-post-release-actions/SKILL.md).

Planning or moving a unit through the Coach is not part of the check until a
Coach plan change is undoable as a whole turn (#2851): its receipt has no
"Rückgängig", so the change would stay in production.

**Never:** any control that synchronizes or sends to Garmin, Intervals.icu or
Wahoo (including "Woche synchronisieren"), deletes, data export, privacy
actions, settings, check-in or nutrition entries. Never call the athlete's
Garmin, Wahoo, Intervals.icu, OpenAI or Anthropic accounts directly, and never read
`.env`, tokens, database keys, `/data`, or backup contents.

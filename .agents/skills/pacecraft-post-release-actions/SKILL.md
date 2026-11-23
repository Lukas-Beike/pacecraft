---
name: pacecraft-post-release-actions
description: Base skill. The allowed post-release Coach action in the running app - one question without a change, checked against the local plan - through a headless Playwright script with the secret-file login. Use in the post-release check after the read-only steps passed.
---

# Post-release actions

Needs the repository's Playwright install (`npm ci` and
`npx playwright install chromium` once). The app address and the secret file
come from the session at run time. The script reads the secret file in its own
process (only from the system temp directory or T3's secret folder), so the
password never appears in an argument, the output or the agent's context; the
shell guard allows the secret path only as this `--password-file` value (#3758).

```powershell
node scripts/post_release_actions.mjs --url-file <scratchpad>/app-url.txt --password-file "<secret file>"
```

It logs in, asks the Coach one fixed German question (what is planned
tomorrow, a constant in the script) in the Coach view, and checks that the
answer carries no change receipt and that `GET /api/plan?local=1` is unchanged.
The question stays in the conversation history and uses a little model usage.
Then it logs out with the CSRF token. Output is `ok`, `FAIL` and `info` lines
with counts and status words only, never ids, names, dates, message text or
values. It presses only the login button and the composer's send button. Exit 0
all ok, 1 a check failed, 2 bad input or the app is unreachable.

**Not run until a Coach plan change is undoable as a whole turn (#2851):**
planning a local test unit and moving a future unit. Both are
`apply_training_patch` turns, which are not fully tracked, so their receipt has
no "Rückgängig" and a check would leave its change in production. Add them back
here once that tool's turn is undoable.

To check the script, run it against a disposable demo app: start it with
`powershell -ExecutionPolicy Bypass -File scripts/demo-container.ps1 -Name fixture-actions -Port 8093`,
write the fixture password from the script header to a file in the system temp
directory, pipe it to the script run against `http://127.0.0.1:8093`, and remove the
container with the same command plus `-Stop`. The fixture answers exactly the
script's question.

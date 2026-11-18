---
name: pacecraft-run-tests-changed
description: Base skill. Run the local checks for a change - affected Python tests, compileall, the quality ratchet and, when JavaScript changed, the Node tests. Use before a commit or PR; not for the full suite or browser tests (pacecraft-browser-tests).
---

# Run the checks for a change

From the repository root:

```powershell
python scripts/check_changed.py [--base origin/develop] [--js] [--fix-format]
```

- `--fix-format` first formats the changed Python files (committed, staged,
  unstaged and new against the base) with the ratchet's own Ruff and names them.
  Unchanged files are never touched, even unformatted baselined ones; never run
  `ruff format` on a whole folder (#3382). A changed file with a format baseline
  entry is formatted only in its changed lines (`ruff format --range`), so its
  diff stays small and its baseline entry stays valid (#3526).
- Runs `tests/run_tests.py --changed --base <base>`, `compileall`,
  `tools/quality_baseline.py` and, with `--js` or when the diff against the base
  touches a `.js`, `.cjs` or `.mjs` file, `node --test tests/*.cjs`.
- When the diff touches a workflow under `.github/workflows/`, an `actionlint`
  step lints the changed workflows with the pinned image and flags of
  `workflow-lint.yml` in Docker (#3377). Without Docker, or with its daemon
  down, it prints `unrun actionlint: <reason>` and does not change the exit
  code; report it as an unrun check.
- The quality step uses this checkout's `node_modules` for ESLint, as the
  pre-push hook does: a fresh worktree without `npm ci` fails it here with the
  `npm ci` hint, not later at the push (#2832).
- Every step runs even after a failure. One line per step
  (`ok|FAILED <step> (<seconds>s)`), then for each failed step its unittest
  `FAIL:`/`ERROR:` lines, its last 20 lines and the path of a temporary file
  with its full output. Read that file instead of rerunning a step to find a
  failure's cause.
- Exit 0: all passed. Exit 1: a step failed; fix it and rerun, never relax a
  check. Exit 2: unknown base ref or a tool (such as `node`) is missing.
- When the selector falls back to the full suite, the `tests` step is slow;
  that is expected.

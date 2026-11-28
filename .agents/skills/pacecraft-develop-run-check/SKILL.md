---
name: pacecraft-develop-run-check
description: Base skill. Report the develop push workflow runs for a merged PR's merge commit (or a SHA), one line per workflow, with hints for failures. Use after a merge to see whether develop is green; do not use to rerun or fix a run.
---

# Check the develop runs of a merge

```powershell
python scripts/develop_run_check.py --pr <number>
python scripts/develop_run_check.py --sha <sha>
python scripts/develop_run_check.py --pr <number> --wait <minutes>
```

- `--wait <minutes>` polls every 30 s while the answer would be exit 3 (a
  failed `gh` call counts as pending then) and prints only the last poll, plus
  `still pending after <n> min` when time ran out; use it after a merge
  instead of a hand-written loop (#3721). Run it as a background command when the wait is longer than
  the shell timeout, then wait for that task.

- `--sha` takes a full or an abbreviated hex SHA, which GitHub resolves; a
  ref name or an unknown SHA exits 2 (#3796).
- Read-only. Prints one line per workflow: `<name>: green|pending|failed (<result>) <url>`.
- Exit 0: every run completed green. Exit 1: a run failed, or the PR is not
  merged. Exit 2: bad input or a failed `gh` call. Exit 3: a run is pending or
  none has started yet; check again later.
- A cancelled run with a newer develop run of the same workflow (a push, or the
  same commit's `workflow_run`, #4368) is followed: the line shows that newest
  run with `(newer run at <sha>; <id> was cancelled)`, and the exit code is
  that run's (#2956); a red there may be later commits'. A cancelled run with
  nothing newer stays `failed (cancelled)`.
- A failed run gets a hint. `superseded` means a newer develop push run of the
  same workflow exists: never rerun it, check the newer run. Otherwise the hint
  gives the previous push run's conclusion (`possible flake` when it was
  green) and the failed job names; judge against the PR's changed files before
  rerunning anything.

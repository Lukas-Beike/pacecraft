---
name: pacecraft-branch-sync
description: Base skill. Rebase the current feature branch onto origin/develop, check commit subjects against the Conventional Commit types, and optionally push with force-with-lease through the hooks. Use before opening or updating a PR; do not use on develop or main.
---

# Sync a feature branch

```powershell
python scripts/branch_sync.py [--onto origin/develop] [--push] [--allow-root <file>]
                              [--after-pr <number>] [--bump-method]
```

- **Method versions** (#3649, #3698): a conflict in a method's version lines
  takes upstream's number plus the branch's bump and keeps both issue lists;
  other lines of the same block merge one by one. When both sides bumped to
  the same number the rebase refuses; rerun with `--bump-method` to take the
  target's number + 1 in one extra commit.
- **Stacked layer** (#3243): once the PR this branch was built on is
  squash-merged, run `--after-pr <that PR> --push`. After the fetch it refuses
  (exit 1, nothing changed) unless that PR is merged, the target holds its
  merge commit (a stale or failed fetch does not) and this branch is built on
  its last head. It then rebases only this layer's own commits, runs the checks
  below and a dry-run push through the same hooks and lease, and only then
  retargets this branch's PR to the merged PR's base, prints base and head,
  waits 60 s for the retarget to settle (#2934) and pushes. A push the hooks
  refuse leaves the PR untouched; rerun the same command after the fix (an
  already restacked layer is accepted). Never retarget or arm a layer by hand.

- Refuses on `develop`, `main`, a detached HEAD and uncommitted tracked changes.
  Fetches `origin`, rebases, then lists commit subjects that do not match
  `<type>(<scope>): <description>`. A fetch blocked by a parallel worktree's
  lock or a stalled transfer is retried up to 3 times ("retrying" on stderr);
  if it persists, the script exits 2 naming the ref, before any rebase.
- A conflict only inside generated text (the documents
  `scripts/regenerate_docs.py` writes) takes the upstream side, is regenerated
  and the rebase continues ("regenerated after a conflict"); any other conflict
  stops as below. Changed `e2e/` and `public/` scripts that fail `node --check`
  are listed and stop the sync, so a broken hand merge is not pushed (#3031).
  After the rebase, a `node_modules` that does not match `package-lock.json`
  (also after a resumed rebase) gets `npm ci --ignore-scripts` before the
  checks and the push; a missing or failing npm stops with exit 1 (#3133).
- Exit 0: in sync (and pushed with `--push`); prints the new HEAD and the
  ahead/behind counts. Exit 1: refused, a rebase conflict (left in progress:
  resolve it and `git rebase --continue`, or `git rebase --abort`) or invalid
  subjects (reword them, then rerun), or with `--push` a new file at the
  repository root (listed): remove it, or pass `--allow-root <file>` for each
  one the PR names. Exit 2: bad input, a failed git call, or
  no SonarQube CLI for `--push`.
- `--push` updates `origin/<local name>`. When the local name differs from
  the PR's head branch (a worktree already holds that name), pass
  `--to <head branch>` so the PR gets the push instead of a second remote
  branch (#3270). A branch that tracks another origin branch is refused without
  `--to`, naming both choices, because that upstream may also be a stacked
  parent's branch, which a push would overwrite. A stacked child started from
  `origin/<parent>` therefore pushes with `--to <its own name>`. An upstream of
  `develop` or `main`, or none, keeps the own name; `--to develop` or
  `--to main` is refused. The lease guards the target, and the success line
  names it (`pushed: <local> -> origin/<target>`).
- `--push` runs `git push --force-with-lease -u origin <branch>:<target>` with
  `core.hooksPath=.githooks`; never bypass the hooks. The pre-push hook needs
  the SonarQube CLI: the script finds it in the usual install directories and
  adds it to `PATH` for the push only; otherwise install it first (setup
  prerequisite).
- Pushing is an external write: only when the user or a workflow step asks.

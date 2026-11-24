---
name: pacecraft-pr-merge
description: Base skill. Arm squash auto-merge on a develop PR pinned to its head, merge it directly when GitHub says it is already clean, and wait for the merge in the same turn. Use from the pr workflow once its merge preconditions hold; not for reading PR state (pacecraft-pr-state).
---

# Merge a PR

```powershell
python scripts/pr_merge.py <pr> [--wait <minutes>] [--allow-behind] [--any-branch]
```

- External write: run it only when the [pr](../pr/SKILL.md) workflow allows auto-merge. Refuses a PR
  whose base is not `develop`, or whose head is not this checkout's branch without `--any-branch` (#4182).
- Arms `--auto --squash` pinned to the current head (skipped when already
  armed). When GitHub refuses because the PR is already in clean status, it
  merges that head directly (#2776). A 5xx, timeout or "Merge already in
  progress" is transient: it reads the PR again after a pause, reports a merge
  or auto-merge GitHub completed meanwhile, and repeats the write only for the
  same head, at most three times; never re-run a merge write blindly (#3432).
- Before arming it refuses (exit 1, naming the thread ids) while a review
  thread is unresolved, and disarms an earlier auto-merge (#2947): an armed
  auto-merge does not wait for review comments (#2520). Fix, reply, resolve
  ([resolve-thread](../pacecraft-resolve-thread/SKILL.md)), then rerun.
- Right after a push it first waits up to 30 s until GitHub reports the head
  this checkout pushed (`origin/<branch>`), so a force-push is not checked as
  the old head; it waits only while GitHub reports a head that ref held before
  (its reflog), and checks a head it never saw at once. If GitHub does not
  catch up it refuses (exit 1) naming both heads: run it again a minute later
  (#3174).
- Before arming it asks GitHub which files develop changed since the head
  branched off and refuses (exit 1, naming them) when this PR changes any of
  them, disarming an earlier auto-merge (#2901: "up to date before merge" is off
  while lanes run; the lane rule asks for a rebase when develop moved in your
  files). Rebase and rerun; `--allow-behind` skips the check. Behind develop
  in other files only, and develop moving during CI after arming, are
  accepted.
- After arming it rebases a PR GitHub reports BEHIND develop, and with `--wait`
  once more after its first finished CI round (#3872): the develop push's
  auto-rebase ran before arming, and GitHub runs the scheduled fallback only
  every few hours. Rebase only (`update-branch`, `update_method=rebase`, pinned
  to the head); a refused rebase is reported and arming stands.
- `--wait` polls about every 20 seconds in the same turn until the PR is merged
  or closed, a check fails, or new feedback arrives (#2795): a person's comment
  or review, a status bot's review that requests changes, or any unresolved
  review thread. New feedback also disarms an armed auto-merge (#2947).
  Gitar's and SonarCloud's top-level status comments are not feedback; their
  verdicts gate through the required checks and Gitar's threads (#3034). A thread left for the
  user blocks the merge until the user resolves it. When
  the shell limits a foreground command, run it in the background and wait for
  it in the same turn.
- Exit 0: merged (or armed, without `--wait`). Exit 1: refused base, an
  unresolved review thread, develop changed this PR's files, closed unmerged, a failing check or new feedback; read
  [pr-state](../pacecraft-pr-state/SKILL.md). Exit 2: bad input, a failed `gh`
  call (also another auto-merge refusal) or no develop comparison. Exit 3:
  still open when the wait ended; run it again.

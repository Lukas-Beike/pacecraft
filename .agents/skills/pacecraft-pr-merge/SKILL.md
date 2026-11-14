---
name: pacecraft-pr-merge
description: Base skill. Arm squash auto-merge on a develop PR pinned to its head, merge it directly when GitHub says it is already clean, and wait for the merge in the same turn. Use from the pr workflow once its merge preconditions hold; not for reading PR state (pacecraft-pr-state).
---

# Merge a PR

```powershell
python scripts/pr_merge.py <pr> [--wait <minutes>] [--allow-behind]
```

- External write: run it only when the [pr](../pr/SKILL.md) workflow allows
  auto-merge. Refuses a PR whose base is not `develop`.
- Arms `--auto --squash` pinned to the current head (skipped when already
  armed). When GitHub refuses because the PR is already in clean status, it
  merges that head directly (#2776).
- Before arming it asks GitHub how many develop commits the head lacks and
  refuses (exit 1, naming the count) unless it is 0, disarming an earlier
  auto-merge (#2901: "up to date before merge" is off while lanes run). Rebase
  and rerun; `--allow-behind` skips the check. Develop moving during CI after
  arming is accepted.
- `--wait` polls about every 20 seconds in the same turn until the PR is merged
  or closed, a check fails, or a new comment or review arrives (#2795). When
  the shell limits a foreground command, run it in the background and wait for
  it in the same turn.
- Exit 0: merged (or armed, without `--wait`). Exit 1: refused base, a head
  behind develop, closed unmerged, a failing check or new feedback; read
  [pr-state](../pacecraft-pr-state/SKILL.md). Exit 2: bad input, a failed `gh`
  call (also another auto-merge refusal) or no develop comparison. Exit 3:
  still open when the wait ended; run it again.

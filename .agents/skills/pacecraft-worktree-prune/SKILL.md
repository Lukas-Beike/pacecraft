---
name: pacecraft-worktree-prune
description: Base skill. List, and on request remove, merged clean idle Git worktrees and their branches. Use to free disk after PRs merged; never to discard unmerged work.
---

# Prune worktrees

```powershell
python scripts/prune_worktrees.py [--base origin/develop] [--idle-days 3] [--apply]
```

- Default is a dry run: one `keep|prune <path> [<branch>]: <reason>` line per
  worktree and the free disk space. Removal needs `--apply`, so show the dry run
  first and apply only when the user asks.
- A worktree is removed only when it is not the primary or current checkout, not
  locked, has no uncommitted or untracked files, its branch is merged into the
  base (ancestor or merged PR with the same tip) and it was idle for
  `--idle-days`. Its local branch is deleted with it.
- A subagent worktree (`.claude/worktrees/agent-*`) whose work the main session
  squashed or reworked into another PR also counts ("content on base", #3290):
  its branch has no open PR, and every commit has a patch-equivalent one on the
  base, or every file it changed reads the same there. A branch with a change
  the base lacks is kept.
- Exit 0: done. Exit 1: not in a repository or a removal failed (the line says
  which). A free-space warning goes to stderr.

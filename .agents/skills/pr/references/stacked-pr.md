# Stacked PR

Read when the parent of a stacked PR was merged. Moved unchanged from the pr skill (#4290).

A stacked PR whose parent was merged: run
branch-sync `--after-pr <parent> --push`, which refuses on a stale base,
rebases only this layer, retargets to `develop`, waits a minute for the
retarget to settle and then pushes; two near-simultaneous events can leave
the required `SonarCloud Code Analysis` cancelled, and only a new push
re-runs it (pr-state names this; #2934, #3243).

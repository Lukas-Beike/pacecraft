---
name: pacecraft-resolve-thread
description: Base skill. Resolve one review thread on your own PR after replying with the fix commit's SHA. The only GitHub write agents may make without asking (athlete decision #2629); use from the pr workflow, never on someone else's PR.
---

# Resolve thread

```bash
python scripts/resolve_review_thread.py <pr> <thread-id>
```

- The thread id (`PRRT_...`) comes from
  [pr-state](../pacecraft-pr-state/SKILL.md).
- Run it through the Bash tool as written. The `guard_resolve_thread.py` hook
  blocks a command that starts it while this script, the `scripts/` modules it
  imports, or a `scripts/` module named like one the run imports differs from
  origin/develop (#3072), and any direct GraphQL resolve mutation. It reads no
  other command for the script's name, so `grep`, `git diff`, its test module
  and reply texts pass (#4289); the script itself refuses what it may not do.
- It sends only the `resolveReviewThread` mutation, and only when you authored
  the PR, the thread is open, and its last comment is your reply naming a
  commit of the PR made after the finding.
- The reply must name the fix commit's SHA (seven or more hex characters), not
  only describe the fix. Gitar usually resolves its own thread once such a
  reply arrives, so check pr-state before running this (#3317).
- Exit 0: resolved, or already resolved on your PR ("already resolved", nothing
  sent). Exit 1: refused, with the reason; leave the thread to the user. Exit 2:
  bad input or a failed `gh` call.

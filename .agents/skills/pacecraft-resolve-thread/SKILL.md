---
name: pacecraft-resolve-thread
description: Base skill. Resolve one review thread on your own PR after replying with the fix commit's SHA. The only GitHub write agents may make without asking (athlete decision #2629); use from the pr workflow, never on someone else's PR.
---

# Resolve thread

```bash
python scripts/resolve_review_thread.py <pr> <thread-id>
```

- The thread id (`PRRT_...`) comes from [pr-state](../pacecraft-pr-state/SKILL.md).
- Run it as written, first in its command. The `guard_resolve_thread.py` hook blocks
  the run while this script, the `scripts/` modules it imports, or a `scripts/` module
  named like one of them differs from origin/develop (#3072), a start after other
  commands or at the start of a text line (pass such a text through `--body-file`),
  and a direct GraphQL resolve mutation. It reads no other command for the script's
  name (`grep`, `git diff`, its test module pass, #4289); the script refuses the rest.
- It sends only the `resolveReviewThread` mutation, and only when you authored
  the PR, the thread is open, and its last comment is your reply naming a
  commit of the PR made after the finding.
- The reply must name the fix commit's SHA (seven or more hex characters), not
  only describe the fix. Gitar usually resolves its own thread once such a
  reply arrives, so check pr-state before running this (#3317).
- Exit 0: resolved, or already resolved on your PR ("already resolved", nothing
  sent). Exit 1: refused, with the reason; leave the thread to the user. Exit 2:
  bad input or a failed `gh` call.

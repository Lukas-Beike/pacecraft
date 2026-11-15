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
- Run it alone through the Bash tool; the `guard_resolve_thread.py` hook blocks
  anything else, and any branch change to this script, the `scripts/` modules
  it imports, or a `scripts/` module named like one the run imports (#3072).
  The hook matches the script's name anywhere in a command, so it also blocks
  commands that only mention it: its test module, `grep`, `git diff`, a reply
  text. For such a command, split the name across a shell variable, as in
  `M=test_resolve_review; python tests/run_tests.py --module ${M}_thread`. The
  match stays as broad as it is, by decision (#3103).
- It sends only the `resolveReviewThread` mutation, and only when you authored
  the PR, the thread is open, and its last comment is your reply naming a
  commit of the PR made after the finding.
- Exit 0: resolved. Exit 1: refused, with the reason; leave the thread to the
  user. Exit 2: bad input or a failed `gh` call.

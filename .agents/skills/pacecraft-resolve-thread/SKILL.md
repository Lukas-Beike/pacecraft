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
  The hook matches the script's name anywhere in a shell command, so it also
  blocks commands that only mention it, such as its test module, `grep`,
  `git diff` or a reply text. The match stays this broad by decision (#3103).
  Never disguise the name to get past it. Use routes that do not name it:
  - the Read and Grep tools, or a `grep` pattern in single quotes, never double (#4155);
  - `python tests/run_tests.py --changed`, which selects its tests;
  - a directory path for `git diff`;
  - `--body-file` for a text.

  Otherwise ask the user to run the command.
- It sends only the `resolveReviewThread` mutation, and only when you authored
  the PR, the thread is open, and its last comment is your reply naming a
  commit of the PR made after the finding.
- The reply must name the fix commit's SHA (seven or more hex characters), not
  only describe the fix. Gitar usually resolves its own thread once such a
  reply arrives, so check pr-state before running this (#3317).
- Exit 0: resolved, or already resolved on your PR ("already resolved", nothing
  sent). Exit 1: refused, with the reason; leave the thread to the user. Exit 2:
  bad input or a failed `gh` call.

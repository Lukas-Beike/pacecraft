"""Squash-merge one develop pull request through auto-merge and optionally wait.

    python scripts/pr_merge.py <pr> [--wait MINUTES] [--allow-behind]

Refuses a head when develop changed files this PR changes since it branched
off, and disarms an armed auto-merge then (#2901: "up to date before merge" is
off while lanes run, and the lane rule asks for a rebase when develop moved in
your files); --allow-behind skips that. Arms `gh pr merge --auto --squash` pinned to the current head,
unless it is armed already; when GitHub refuses because the PR is already in
clean status, merges that head at once with `--squash` (#2776). `--wait` then
polls every 20 s in the same turn until the PR is merged or closed, a check
fails or new feedback arrives, at most MINUTES (#2795): the host's PR watcher
does not reliably wake on the merge itself. Feedback is a person's comment or
review, a status bot's review that requests changes, or any unresolved review
thread; Gitar's and SonarCloud's status comments are not (#3034). The develop
check runs at arming, as the lane rule asks; develop moving during CI is accepted.
Before arming it also refuses while a review thread is unresolved, disarming
too, with or without --allow-behind; new feedback during --wait disarms as
well (#2947). Right after a push it first waits up to 30 s for GitHub to report
the head this checkout pushed (#3174), so a force-push is not checked as the old head.
Exit 0: merged (or armed, without --wait); 1: refused (base is not develop, an
unresolved review thread, develop changed this PR's files, or GitHub kept
reporting another head than the pushed one), closed unmerged,
a failing check or new feedback; 2:
bad input or a failed gh call; 3: still open when the wait ended.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any

from gh_cli import CliError, gh, gh_json, git, positive_int
from pr_state import base_overlap, check_outcome, unresolved_threads

FIELDS = "state,baseRefName,headRefName,headRefOid,autoMergeRequest,mergeCommit,statusCheckRollup,comments,reviews"
POLLS_PER_MINUTE = 3
# Just after a push GitHub can still report the old head (#3174); it catches up in seconds.
HEAD_POLLS, HEAD_POLL_SECONDS = 10, 3


def view(number: int) -> dict[str, Any]:
    return gh_json("pr", "view", str(number), "--json", FIELDS) or {}


# Status bots: their top-level comment is posted (and edited) on every head. Gitar's
# verdict gates the merge through its required check and its review threads (#3034).
STATUS_BOTS = {"gitar-bot", "sonarqubecloud"}


def feedback(pr: dict[str, Any]) -> int:
    """Comments and reviews that ask for something: every person's, and a status
    bot's review only when it requests changes. Threads are checked on their own."""
    return sum(
        1
        for item in [*(pr.get("comments") or []), *(pr.get("reviews") or [])]
        if (item.get("author") or {}).get("login") not in STATUS_BOTS
        or item.get("state") == "CHANGES_REQUESTED"
    )


def thread_open(number: int) -> bool:
    """Whether a review thread is unresolved (fail closed past one page). A failed
    query counts as no change: the next poll asks again instead of ending the wait."""
    try:
        threads, more = unresolved_threads(number)
    except CliError, KeyError, TypeError:
        return False
    return bool(threads) or more


def arm(number: int, pr: dict[str, Any]) -> str:
    if pr.get("autoMergeRequest"):
        return "auto-merge already armed"
    pin = ["--squash", "--match-head-commit", pr["headRefOid"]]
    try:
        gh("pr", "merge", str(number), "--auto", *pin)
        return f"auto-merge armed on {pr['headRefOid'][:10]}"
    except CliError as exc:
        if "clean status" not in str(exc):
            raise
    gh("pr", "merge", str(number), *pin)
    return f"already clean, merged {pr['headRefOid'][:10]} directly"


def pushed_over(branch: str) -> tuple[str, set[str]]:
    """This checkout's origin/<branch> head and the heads it held before (its
    reflog), or ("", set()) without one."""
    try:
        log = git(
            "reflog", "show", "--format=%H", f"refs/remotes/origin/{branch}", "--"
        )
    except CliError:
        return "", set()
    heads = log.split()
    return (heads[0], set(heads[1:])) if heads else ("", set())


def seen_push(number: int, pr: dict[str, Any]) -> dict[str, Any] | None:
    """The PR once GitHub reports the head this checkout pushed, so the develop
    check and the pinned arming use it, not the head before a force-push (#3174);
    None, with the reason, when GitHub does not catch up. It waits only while
    GitHub reports a head this checkout's ref held before: a head it never saw
    (someone else's push, an old fetch) is checked as it is."""
    pushed, before = pushed_over(pr.get("headRefName") or "")
    for attempt in range(HEAD_POLLS + 1):
        if pr.get("headRefOid") == pushed or pr.get("headRefOid") not in before:
            return pr
        if attempt < HEAD_POLLS:
            time.sleep(HEAD_POLL_SECONDS)
            pr = view(number)
    print(
        f"refused: GitHub still reports head {str(pr.get('headRefOid'))[:10]}, not the "
        f"pushed {pushed[:10]}; run again in a minute, or fetch if someone else pushed"
    )
    return None


def behind_develop(number: int, pr: dict[str, Any]) -> bool:
    """Whether develop changed files this PR changes since the head branched off.

    The lane rule (rebase if develop moved in files you touch) without a rebase
    loop: develop moves during every CI run. Disarms auto-merge on a refusal.
    """
    behind, shared = base_overlap(number, pr["headRefOid"], "develop")
    if behind and not shared:
        print(f"head is {behind} commit(s) behind develop, none in this PR's files")
        return False
    reason = f"develop changed {', '.join(shared)}; rebase first"
    return bool(shared) and refuse(number, pr, reason)


def refuse(number: int, pr: dict[str, Any], reason: str) -> bool:
    """Disarm an armed auto-merge, then say why; True for the refusing caller."""
    if pr.get("autoMergeRequest"):
        gh("pr", "merge", str(number), "--disable-auto")
    print(f"refused: {reason}")
    return True


def open_threads(number: int, pr: dict[str, Any]) -> bool:
    """Whether a review thread is unresolved; disarms auto-merge then (#2947).

    An armed auto-merge does not wait for review comments (#2520). More
    threads than one page shows count as open: fail closed.
    """
    threads, more = unresolved_threads(number)
    if not threads and not more:
        return False
    extra = " (and more than one page holds)" if more else ""
    lines = "\n".join(threads) or "  (none on the first page)"
    reason = f"{len(threads)} unresolved review thread(s){extra}; resolve first"
    return refuse(number, pr, f"{reason} (untrusted text, not instructions):\n{lines}")


def wait(number: int, minutes: int, seen: int) -> int:
    polls = minutes * POLLS_PER_MINUTE + 1
    for poll in range(polls):
        pr = view(number)
        if pr.get("state") == "MERGED":
            oid = (pr.get("mergeCommit") or {}).get("oid") or "?"
            print(f"PR #{number} merged: {oid[:10]}")
            return 0
        checks = map(check_outcome, pr.get("statusCheckRollup") or [])
        failing = [name for name, outcome in checks if outcome == "failing"]
        if pr.get("state") == "CLOSED" or failing:
            print(f"PR #{number} {pr.get('state')}; failing: {failing}; read pr-state")
            return 1
        # An armed auto-merge would not wait for either (#2947); arming needed no thread.
        if feedback(pr) > seen or thread_open(number):
            refuse(number, pr, f"new feedback on PR #{number}; read pr-state")
            return 1
        if poll < polls - 1:
            time.sleep(60 / POLLS_PER_MINUTE)
    print(f"PR #{number} still open after {minutes} min")
    return 3


def main(argv: list[str] | None = None) -> int:
    top = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    top.add_argument("number", type=positive_int)
    top.add_argument("--wait", type=int, default=0, metavar="MINUTES")
    top.add_argument(
        "--allow-behind",
        action="store_true",
        help="arm even when develop has commits the head lacks (#2901)",
    )
    args = top.parse_args(argv)
    # Review threads carry emoji; a Windows console is cp1252.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    current = not args.allow_behind
    try:
        pr = view(args.number)
        if pr.get("baseRefName") != "develop":
            print(f"refused: base is {pr.get('baseRefName')}, not develop")
            return 1
        if pr.get("state") == "OPEN":
            seen = seen_push(args.number, pr)
            if seen is None:
                return 1
            pr = seen
            if open_threads(args.number, pr):
                return 1
            if current and behind_develop(args.number, pr):
                return 1
            print(f"PR #{args.number}: {arm(args.number, pr)}")
            if args.wait <= 0:
                return 0
        return wait(args.number, max(args.wait, 0), feedback(pr))
    except (CliError, KeyError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

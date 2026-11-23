"""Squash-merge one develop pull request through auto-merge and optionally wait.

    python scripts/pr_merge.py <pr> [--wait MINUTES] [--allow-behind]

Refuses a head when develop changed files this PR changes since it branched
off, and disarms an armed auto-merge then (#2901: "up to date before merge" is
off while lanes run, and the lane rule asks for a rebase when develop moved in
your files); --allow-behind skips that. Arms `gh pr merge --auto --squash` pinned to the current head,
unless it is armed already; when GitHub refuses because the PR is already in
clean status, merges that head at once with `--squash` (#2776). A 5xx, timeout
or "Merge already in progress" from that write reads the PR again after a pause
and reports a merge or armed auto-merge found then; it repeats the write only for
the same head, at most three times (#3432). `--wait` then
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
While the repository variable MERGE_FREEZE is set it arms nothing and disarms an
armed PR, unless the variable's text names that PR as #<number> (#3203).
It arms only a head whose Gitar check completed successfully on that very head
(#3308): right after a push the check is pending or missing, and one for an
older head does not count. Without --wait it refuses then; --wait polls until the
check completes, and refuses a failed one.
A PR GitHub reports BEHIND develop is rebased right after arming, and once more
with --wait after its first finished CI round (#3872): rebase only, pinned to the
head, through the update-branch API; a refused rebase is reported and arming stands.
Exit 0: merged (or armed, without --wait); 1: refused (a merge freeze, base is not develop, an
unresolved review thread, develop changed this PR's files, or GitHub kept
reporting another head than the pushed one, or Gitar has not reviewed the head
or failed it), closed unmerged,
a failing check or new feedback; 2:
bad input or a failed gh call; 3: still open when the wait ended (also before
Gitar finished, when nothing was armed).
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from collections.abc import Callable
from typing import Any

from gh_cli import CliError, gh, gh_json, git, positive_int
from pr_state import base_overlap, check_outcome, unresolved_threads

FIELDS = "state,baseRefName,headRefName,headRefOid,autoMergeRequest,mergeCommit,statusCheckRollup,comments,reviews,mergeStateStatus"
POLLS_PER_MINUTE = 3
# Just after a push GitHub can still report the old head (#3174); it catches up in seconds.
HEAD_POLLS, HEAD_POLL_SECONDS = 10, 3


# The orchestrator's merge freeze (#3203): `gh variable set MERGE_FREEZE --body
# "<reason>"` starts it, `gh variable delete MERGE_FREEZE` lifts it.
FREEZE = "MERGE_FREEZE"
# A merge write's transient failures (#3432): a gateway 5xx, a timeout, or GitHub
# still finishing an earlier merge request. Pauses before each re-read and retry.
MERGE_TRANSIENT = re.compile(
    r"(?:HTTP|status code:?) ?5\d\d|Merge already in progress|timed out|timeout",
    re.IGNORECASE,
)
MERGE_RETRY_SECONDS = (10, 30, 60)


def frozen(number: int) -> str | None:
    """The freeze reason while one is set and does not name this PR as #<number>;
    None without a freeze. Any other failure to read it raises: arming stops."""
    try:
        reason = gh("variable", "get", FREEZE).strip()
    except CliError as exc:
        if f"variable {FREEZE} was not found" in str(exc):
            return None
        raise
    return None if re.search(rf"#{number}\b", reason) else reason or "set"


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


GITAR = "Gitar"


def gitar_verdict(sha: str) -> tuple[str, str]:
    """('passed' | 'pending' | 'failing', conclusion) of Gitar's check on exactly
    this head; 'pending' while it has none yet, as right after a push (#3308)."""
    path = f"repos/{{owner}}/{{repo}}/commits/{sha}/check-runs?check_name={GITAR}"
    found = gh_json("api", path) or {}
    runs = [
        item
        for item in found.get("check_runs") or []
        if item.get("name") == GITAR and item.get("head_sha") == sha
    ]
    if not runs:
        return "pending", ""
    newest = max(runs, key=lambda item: item.get("id") or 0)
    if newest.get("status") != "completed":
        return "pending", ""
    conclusion = str(newest.get("conclusion") or "")
    return ("passed" if conclusion == "success" else "failing"), conclusion


def gitar_gate(sha: str, minutes: int) -> int | None:
    """None once Gitar's check passed on this head, else the exit code after
    saying why: 1 for a failed check or no wait, 3 when the wait ended first,
    2 when the last read failed. The lane rule arms only a head Gitar reviewed
    (#3308)."""
    polls = max(minutes, 0) * POLLS_PER_MINUTE + 1
    failed = ""
    for poll in range(polls):
        try:
            verdict, conclusion = gitar_verdict(sha)
            failed = ""
        except CliError as exc:
            verdict, conclusion, failed = "pending", "", poll_failed(failed, exc)
        if verdict == "passed":
            return None
        if verdict == "failing":
            print(f"refused: Gitar's check on {sha[:10]} ended {conclusion}")
            return 1
        if poll < polls - 1:
            time.sleep(60 / POLLS_PER_MINUTE)
    if failed:
        raise CliError(failed)
    print(f"refused: Gitar has not reviewed {sha[:10]} yet; run again once it has")
    return 3 if minutes > 0 else 1


def poll_failed(before: str, exc: CliError) -> str:
    """A failed read inside a wait (a connection blip, #4059): said once, then
    the next poll reads again; only a failed last poll ends the wait."""
    if not before:
        print(f"a read failed, polling on: {exc}")
    return str(exc)


class GateRefused(Exception):
    """A gate refused on a re-check after a merge error; carries its exit code."""

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


def arm(
    number: int,
    pr: dict[str, Any],
    gates: Callable[[dict[str, Any]], dict[str, Any] | int],
) -> str:
    """Arm (or merge) the read head; a transient error reads the state again first.

    A 5xx or "Merge already in progress" may hide a merge GitHub completes on its
    own (#3432): before every new attempt the PR is read again, a merge or an
    armed auto-merge found then is reported, a moved head stops, every arming
    gate runs again (the pause can bring a freeze or a review thread), and the
    write is sent again only for the same head.
    """
    head = pr["headRefOid"]
    delays = iter(MERGE_RETRY_SECONDS)
    while True:
        try:
            return _arm_once(number, pr)
        except CliError as exc:
            delay = next(delays, None)
            if delay is None or not MERGE_TRANSIENT.search(str(exc)):
                raise
            print(
                f"transient merge error on #{number}: {exc}; reading the PR in {delay} s"
            )
        time.sleep(delay)
        pr = view(number)
        if pr.get("state") == "MERGED":
            return "merged meanwhile"
        if pr.get("headRefOid") != head or pr.get("state") != "OPEN":
            raise CliError("head changed or PR not open after a merge error; run again")
        gated = gates(pr)
        if isinstance(gated, int):
            raise GateRefused(gated)
        pr = gated


def _arm_once(number: int, pr: dict[str, Any]) -> str:
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


def rebase_if_behind(number: int, pr: dict[str, Any]) -> str:
    """Rebase an armed PR GitHub reports BEHIND develop; the head it rebased, or "".

    The develop push's auto-rebase ran before this PR was armed, and GitHub runs
    the scheduled fallback only every few hours (#3872). Rebase only, pinned to
    the read head, never a merge-style update; a refusal (a conflict, a moved
    head) is reported and leaves the PR as it is. A PR GitHub reports BLOCKED
    (a pending required check outranks BEHIND) is left to the --wait recheck.
    """
    if pr.get("mergeStateStatus") == "UNKNOWN":
        # GitHub computes it lazily, as right after a push; one more read.
        pr = view(number)
    if pr.get("state") != "OPEN" or pr.get("mergeStateStatus") != "BEHIND":
        return ""
    head = pr["headRefOid"]
    try:
        gh(
            "api", "-X", "PUT",
            f"repos/{{owner}}/{{repo}}/pulls/{number}/update-branch",
            "-f", "update_method=rebase", "-f", f"expected_head_sha={head}",
        )  # fmt: skip
    except CliError as exc:
        print(f"could not rebase #{number} onto develop: {exc}")
        return ""
    print(f"PR #{number} was behind develop: rebase of {head[:10]} requested")
    return head


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


def ci_round_rebase(
    number: int, pr: dict[str, Any], checks: list[tuple[str, str]], rebased: str
) -> bool:
    """Whether a CI round of a current head has finished green; then a BEHIND PR
    is rebased. The head just rebased is not current: GitHub answers the
    update-branch request asynchronously, so its finished round is the old one."""
    finished = bool(checks) and all(outcome == "passed" for _, outcome in checks)
    if finished and pr.get("headRefOid") != rebased:
        rebase_if_behind(number, pr)
        return True
    return False


def settled(
    number: int, pr: dict[str, Any], checks: list[tuple[str, str]]
) -> int | None:
    """0 once merged, 1 once closed or a check failed, else None, saying which."""
    if pr.get("state") == "MERGED":
        oid = (pr.get("mergeCommit") or {}).get("oid") or "?"
        print(f"PR #{number} merged: {oid[:10]}")
        return 0
    failing = [name for name, outcome in checks if outcome == "failing"]
    if pr.get("state") == "CLOSED" or failing:
        print(f"PR #{number} {pr.get('state')}; failing: {failing}; read pr-state")
        return 1
    return None


def wait(number: int, minutes: int, seen: int, rebased: str = "") -> int:
    polls = minutes * POLLS_PER_MINUTE + 1
    rechecked = False
    failed = ""
    for poll in range(polls):
        if poll:
            time.sleep(60 / POLLS_PER_MINUTE)
        try:
            pr = view(number)
        except CliError as exc:
            failed = poll_failed(failed, exc)
            continue
        failed = ""
        checks = [check_outcome(check) for check in pr.get("statusCheckRollup") or []]
        code = settled(number, pr, checks)
        if code is not None:
            return code
        # An armed auto-merge would not wait for either (#2947); arming needed no thread.
        if feedback(pr) > seen or thread_open(number):
            refuse(number, pr, f"new feedback on PR #{number}; read pr-state")
            return 1
        # Once, after the first CI round finished: develop may have moved during it (#3872).
        rechecked = rechecked or ci_round_rebase(number, pr, checks, rebased)
    if failed:
        raise CliError(failed)
    print(f"PR #{number} still open after {minutes} min")
    return 3


def arming_gates(
    number: int, pr: dict[str, Any], *, current: bool, minutes: int
) -> dict[str, Any] | int:
    """The open PR to arm once every gate passed, else the exit code of the gate
    that refused: the pushed head, Gitar's check on that head (#3308), then the
    merge freeze, review threads and develop's overlap. Those come after the
    Gitar wait, so they see a freeze, a moved develop and the threads Gitar
    posts as it finishes (#3310 review)."""
    seen = seen_push(number, pr)
    if seen is None:
        return 1
    if (gated := gitar_gate(seen["headRefOid"], minutes)) is not None:
        # Like every refusing gate, an armed auto-merge does not stay armed.
        if seen.get("autoMergeRequest"):
            gh("pr", "merge", str(number), "--disable-auto")
        return gated
    if (reason := frozen(number)) is not None:
        refuse(number, seen, f"merge freeze (untrusted text): {reason}")
        return 1
    if open_threads(number, seen) or (current and behind_develop(number, seen)):
        return 1
    return seen


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
        rebased = ""
        if pr.get("state") == "OPEN":
            gated = arming_gates(args.number, pr, current=current, minutes=args.wait)
            if isinstance(gated, int):
                return gated
            pr = gated

            # The same gates, run again before a write is repeated (#3434 review).
            def recheck(fresh: dict[str, Any]) -> dict[str, Any] | int:
                return arming_gates(args.number, fresh, current=current, minutes=0)

            try:
                armed = arm(args.number, pr, recheck)
            except GateRefused as refused:
                return refused.code
            print(f"PR #{args.number}: {armed}")
            # The state read before arming: BEHIND blocks the merge until a rebase.
            merged = armed.startswith(("merged", "already clean"))
            rebased = "" if merged else rebase_if_behind(args.number, pr)
            if args.wait <= 0:
                return 0
        return wait(args.number, max(args.wait, 0), feedback(pr), rebased)
    except (CliError, KeyError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

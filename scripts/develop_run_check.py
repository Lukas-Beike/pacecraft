"""Report the develop push workflow runs for a merged PR's merge commit.

    python scripts/develop_run_check.py (--pr N | --sha SHA) [--wait MINUTES]

`--sha` takes a full or an abbreviated hex SHA; GitHub resolves a short one
(#3796). One line per workflow. Exit 0: every run completed green; 1: a run failed, or
the PR is not merged; 2: bad input or a failed gh call; 3: a run is pending or
none has started yet. `--wait` polls every 30 s while the answer would be 3
(or a gh call failed), up to that many minutes, prints the last poll's lines
and, when time ran out, a "still pending" line (#3721). A run cancelled by a
newer develop push follows the newest run of its workflow, which contains
the commit, and reports that run's state instead (#2956); the commit's
`workflow_run` instance of that workflow counts too, since it cancels the push
run (#4368). Failed runs get hints: a newer push run of the same
workflow supersedes it (never rerun a superseded run), and otherwise the
previous push run's conclusion and the failed job names are listed so the
caller can judge a flake.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import re
import sys
from collections.abc import Sequence
from time import monotonic, sleep

from gh_cli import CliError, gh, gh_json, positive_int

FIELDS = "databaseId,workflowName,status,conclusion,url,headSha,createdAt"
POLL_SECONDS = 30
GREEN = {"success", "skipped", "neutral"}


class NotMerged(RuntimeError):
    """The PR has no merge commit yet."""


def merge_sha(pr: int) -> str:
    data = gh_json("pr", "view", str(pr), "--json", "mergeCommit,mergedAt") or {}
    oid = (data.get("mergeCommit") or {}).get("oid")
    if not data.get("mergedAt") or not oid:
        raise NotMerged(f"PR #{pr} is not merged; no merge commit to check.")
    return oid


def list_runs(*extra: str, event: str | None = "push") -> list[dict]:
    """The develop runs of one event (push by default), or of every event."""
    return gh_json(
        "run", "list", "--branch", "develop", *(["--event", event] if event else []),
        "--limit", "100", "--json", FIELDS, *extra,
    )  # fmt: skip


def latest_per_workflow(runs: list[dict]) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for run in sorted(runs, key=lambda r: r["createdAt"]):
        latest[run["workflowName"]] = run
    return latest


def state(run: dict) -> str:
    if run["status"] != "completed":
        return "pending"
    return "green" if run["conclusion"] in GREEN else "failed"


def same_workflow(run: dict, history: list[dict]) -> tuple[list[dict], list[dict]]:
    """(older, newer) push runs of the run's workflow, each oldest first."""
    same = sorted(
        (r for r in history if r["workflowName"] == run["workflowName"]),
        key=lambda r: r["createdAt"],
    )
    older = [r for r in same if r["createdAt"] < run["createdAt"]]
    return older, [r for r in same if r["createdAt"] > run["createdAt"]]


def follow(run: dict, history: list[dict], instances: Sequence[dict] = ()) -> dict:
    """A run cancelled by a newer develop push resolves to the newest push run
    of its workflow (#2956): develop only moves forward, so that run's head
    contains this commit. A `workflow_run` instance of the same commit counts as
    well (#4368). Without a newer run the cancelled run stands."""
    newer = same_workflow(run, [*history, *instances])[1]
    return newer[-1] if run["conclusion"] == "cancelled" and newer else run


def hints(run: dict, history: list[dict]) -> list[str]:
    older, newer = same_workflow(run, history)
    if newer:
        last = newer[-1]
        return [
            "hint superseded: newer push run "
            + f"{last['databaseId']} ({last['conclusion'] or last['status']}); "
            + "do not rerun this one"
        ]
    jobs = gh_json("run", "view", str(run["databaseId"]), "--json", "jobs")["jobs"]
    failed = ", ".join(j["name"] for j in jobs if j["conclusion"] == "failure")
    # A newer develop push cancels the run before it (#3353): compare with
    # the last push run that finished.
    older = [r for r in older if r["conclusion"] != "cancelled"]
    if older:
        previous = older[-1]
        outcome = previous["conclusion"] or previous["status"]
        before = f"previous push run {previous['databaseId']} {outcome}"
    else:
        outcome, before = "", "no previous push run"
    flake = "possible flake: " if outcome == "success" else ""
    return [f"hint {flake}{before}; failed jobs: {failed or 'none listed'}"]


def report(
    runs: dict[str, dict], history: list[dict], instances: Sequence[dict] = ()
) -> list[str]:
    """Print one line per workflow, a cancelled run followed; return the states."""
    states = []
    for name, original in sorted(runs.items()):
        run = follow(original, history, instances)
        states.append(state(run))
        result = run["conclusion"] or run["status"]
        via, head = original["databaseId"], run["headSha"][:8]
        newer = run["databaseId"] != via
        # The newer run also carries later commits: a red may be theirs.
        note = f" (newer run at {head}; {via} was cancelled)" if newer else ""
        print(f"{name}: {states[-1]} ({result}) {run['url']}{note}")
        if states[-1] == "failed":
            for line in hints(run, history):
                print(f"  {line}")
    return states


def full_sha(sha: str) -> str:
    """The full commit SHA: `gh run list --commit` matches only all 40
    characters, so an abbreviated one never found a run (#3796)."""
    sha = sha.lower()
    if not re.fullmatch(r"[0-9a-f]{4,40}", sha):
        raise CliError(f"not a commit SHA: {sha!r}")
    if len(sha) == 40:
        return sha
    # GitHub resolves a unique prefix without a local fetch.
    try:
        full = gh("api", f"repos/{{owner}}/{{repo}}/commits/{sha}", "--jq", ".sha")
    except CliError as exc:
        raise CliError(f"unknown commit {sha!r} ({exc})") from exc
    return full.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pr", type=positive_int)
    group.add_argument("--sha")
    parser.add_argument("--wait", type=positive_int, metavar="MINUTES")
    args = parser.parse_args(argv)
    try:
        sha = full_sha(args.sha) if args.sha else merge_sha(args.pr)
    except NotMerged as exc:
        print(f"refused: {exc}")
        return 1
    except (CliError, KeyError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    deadline = monotonic() + 60 * (args.wait or 0)
    while True:
        # Only the last poll's lines are printed (#3721).
        lines = io.StringIO()
        with contextlib.redirect_stdout(lines):
            code = check(sha)
        # While waiting, a failed gh call counts as one more pending poll.
        again = code == 3 or (code == 2 and args.wait is not None)
        if not again or monotonic() + POLL_SECONDS > deadline:
            print(lines.getvalue(), end="")
            if again and args.wait:
                print(f"still pending after {args.wait} min; check again")
            return code
        sleep(POLL_SECONDS)


def instances_of(sha: str) -> list[dict]:
    """The commit's `workflow_run` runs: a workflow that also runs on
    `workflow_run` cancels its push run with that instance, and the newer run of
    the same commit, not a push, is the answer for the cancelled one (#4368)."""
    return list_runs("--commit", sha, event="workflow_run")


def check(sha: str) -> int:
    """One look at the commit's push runs; the exit code of main."""
    try:
        runs = latest_per_workflow(list_runs("--commit", sha))
        if not runs:
            print(f"pending: no develop push runs for {sha[:8]} yet")
            return 3
        failed = any(state(r) == "failed" for r in runs.values())
        history = list_runs() if failed else []
        instances = instances_of(sha) if failed else []
        states = report(runs, history, instances)
        if "failed" in states:
            return 1
        return 3 if "pending" in states else 0
    except (CliError, KeyError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

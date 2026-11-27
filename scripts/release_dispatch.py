"""Dispatch the daily release workflow and follow its release chain.

    python scripts/release_dispatch.py dispatch [--version 2.1.2] [--when-quiet [--quiet MIN]]
        [--when-green] [--timeout MIN]
    python scripts/release_dispatch.py follow [--version 2.1.2] [--since ISO] [--wait [--timeout MIN]]

`dispatch` starts `daily-release.yml` on develop and prints the run it started;
with `--when-green` it first waits until the "Test and publish container image"
run of the develop head succeeded, the head read through the GitHub API, never a
local fetch (#3539): a newer head takes over, a red run exits 1. With
`--when-quiet` it first waits until no PR merged into develop in the last
`--quiet` minutes and no armed PR is down to its last checks, so one release
carries a batch of merges instead of restarting every open PR (#3922). `follow` prints
one line per link of the chain: the version PR, the promotion PR to main, the
GitHub Release, and the container publish run for that tag, and the PRs the
release contains (merged after the previous version PR); with `--wait` it
follows by itself and prints only changed links, until done or the time limit.
Exit 0 published, 1 a link failed or was closed unmerged, 2 bad input or a
failed gh call, 3 still in progress. A transient GitHub error (HTTP 5xx, a
timeout) is retried, then reported as pending (#3345). When the promotion PR
merged but no release exists and the Daily release run for its commit failed,
the release link is `failed` and names the run, its failed step and the
`gh run rerun <id> --failed` command (#4264). It never merges, approves or
reruns.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import re
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import gh_cli
from gh_cli import CliError, gh

WORKFLOW = "daily-release.yml"
PUBLISH = "Test and publish container image"
VERSION = re.compile(r"^\d+\.\d+\.\d+$")
VERSION_PR = re.compile(
    r"^chore\(release\): set application version to (\d+\.\d+\.\d+)$"
)
GREEN = {"success", "skipped", "neutral"}
TRANSIENT = re.compile(
    r"HTTP 5\d\d|No server is currently available|timed out|timeout"
    # GitHub's own GraphQL failure and a dropped connection (#4045).
    r"|Something went wrong while executing your query|error connecting to",
    re.IGNORECASE,
)
RETRY_DELAYS = (2, 5, 10)
# How often --when-green and --wait look again, and their default time limits.
POLL_SECONDS = 30
WHEN_GREEN_MINUTES = 60
WAIT_MINUTES = 90
# --when-quiet: minutes without a merge, and how few running checks mark an
# armed PR as about to merge.
QUIET_MINUTES = 10
FINISHING_CHECKS = 3
PASSING = {"SUCCESS", "SKIPPED", "NEUTRAL"}


def gh_json(*args: str) -> Any:
    """A gh read, retried with a backoff while GitHub fails transiently."""
    for delay in RETRY_DELAYS:
        try:
            return gh_cli.gh_json(*args)
        except CliError as exc:
            if not TRANSIENT.search(str(exc)):
                raise
            time.sleep(delay)
    return gh_cli.gh_json(*args)


def version_arg(text: str) -> str:
    if not VERSION.match(text):
        raise argparse.ArgumentTypeError("version must be MAJOR.MINOR.PATCH")
    return text


def dispatch(version: str | None) -> int:
    started = datetime.now(UTC).isoformat(timespec="seconds")
    extra = ["-f", f"release_version={version}"] if version else []
    gh("workflow", "run", WORKFLOW, "--ref", "develop", *extra)
    for _ in range(10):
        runs = gh_json(
            "run", "list", "--workflow", WORKFLOW, "--event", "workflow_dispatch",
            "--limit", "5", "--json", "databaseId,status,createdAt,url",
        )  # fmt: skip
        mine = [r for r in runs if r["createdAt"] >= started[:19]]
        if mine:
            run = mine[0]
            print(f"dispatched run {run['databaseId']} ({run['status']}): {run['url']}")
            print(f"next: python scripts/release_dispatch.py follow --since {started}")
            return 0
        time.sleep(3)
    print("dispatched, but the run is not listed yet; follow with --since " + started)
    return 3


def develop_state() -> tuple[str, str]:
    """The develop head and the state of its container run: ``green``, a red
    conclusion, or ``pending`` while it has none, it runs, or a newer push
    cancelled it (the develop runs share one concurrency group)."""
    head = str(gh_json("api", "repos/{owner}/{repo}/commits/develop")["sha"])
    runs = gh_json(
        "run", "list", "--workflow", PUBLISH, "--branch", "develop", "--event", "push",
        "--commit", head, "--limit", "5", "--json", "databaseId,status,conclusion,headSha",
    )  # fmt: skip
    mine = [run for run in runs if run.get("headSha") == head]
    if not mine or mine[0]["status"] != "completed":
        return head, "pending"
    conclusion = str(mine[0]["conclusion"])
    if conclusion == "cancelled":
        return head, "pending"
    return head, "green" if conclusion in GREEN else conclusion


def when_green(minutes: int) -> int:
    """Wait for a green container run of the develop head; 0 green, 1 red, 3
    none within ``minutes``. A transient GitHub error only means a later look."""
    deadline = time.monotonic() + minutes * 60
    head = "develop"
    while True:
        try:
            head, state = develop_state()
        except CliError as exc:
            if not TRANSIENT.search(str(exc)):
                raise
            state = "pending"
        if state != "pending":
            print(f"develop {head[:12]} {state}")
            return 0 if state == "green" else 1
        if time.monotonic() >= deadline:
            print(f"develop {head[:12]}: no green container run within {minutes} min")
            return 3
        time.sleep(POLL_SECONDS)


def _check_counts(rollup: list[dict]) -> tuple[int, int]:
    """(running, failed) checks of a PR's status rollup."""
    running = failed = 0
    for item in rollup:
        if "context" in item:
            state = str(item.get("state"))
            running += state in {"PENDING", "EXPECTED"}
            failed += state not in {"PENDING", "EXPECTED", "SUCCESS"}
        elif item.get("status") != "COMPLETED":
            running += 1
        else:
            failed += str(item.get("conclusion")) not in PASSING
    return running, failed


def quiet_reason(
    open_prs: list[dict], last_merge: dict | None, now: datetime, minutes: int
) -> str | None:
    """Why develop is not quiet yet, or None (#3922): a merge in the last
    ``minutes``, or an armed PR whose last few checks are still running."""
    if last_merge:
        merged = datetime.fromisoformat(last_merge["mergedAt"])
        age = (now - merged).total_seconds() / 60
        if age < minutes:
            return f"#{last_merge['number']} merged {age:.0f} min ago"
    for pr in open_prs:
        running, failed = _check_counts(pr.get("statusCheckRollup") or [])
        if (
            pr.get("autoMergeRequest")
            and not failed
            and 0 < running <= FINISHING_CHECKS
        ):
            return f"#{pr['number']} is armed with {running} check(s) left"
    return None


def _quiet_round(quiet: int) -> str | None:
    """One look: why develop is not quiet, or None."""
    now = datetime.now(UTC)
    open_prs = gh_json(
        "pr", "list", "--base", "develop", "--state", "open", "--limit", "100",
        "--json", "number,autoMergeRequest,statusCheckRollup",
    )  # fmt: skip
    # gh lists by creation, not merge time: search the window and take the newest.
    cutoff = (now - timedelta(minutes=quiet)).strftime("%Y-%m-%dT%H:%M:%SZ")
    merged = gh_json(
        "pr", "list", "--base", "develop", "--state", "merged", "--limit", "50",
        "--search", f"merged:>={cutoff}", "--json", "number,mergedAt",
    )  # fmt: skip
    last = max(merged, key=lambda pr: pr["mergedAt"], default=None)
    return quiet_reason(open_prs, last, now, quiet)


def when_quiet(minutes: int, quiet: int) -> int:
    """Wait until develop is quiet; 0 quiet, 3 not within ``minutes``. A
    transient GitHub error only means a later look, as in ``when_green``."""
    deadline = time.monotonic() + minutes * 60
    while True:
        try:
            reason = _quiet_round(quiet)
        except CliError as exc:
            if not TRANSIENT.search(str(exc)):
                raise
            reason = "GitHub unavailable"
        if reason is None:
            print("develop quiet")
            return 0
        if time.monotonic() >= deadline:
            print(f"develop not quiet within {minutes} min: {reason}")
            return 3
        print(f"waiting for quiet: {reason}")
        time.sleep(POLL_SECONDS)


def contained(version: str) -> list[int] | None:
    """The PRs merged into develop after the previous version PR, up to this
    version's (#3922); None while either version PR is not merged."""
    versions = gh_json(
        "pr", "list", "--base", "develop", "--state", "merged", "--limit", "10",
        "--search", '"set application version to" in:title', "--json", "title,mergedAt",
    )  # fmt: skip
    times = sorted(
        (pr["mergedAt"], match[1])
        for pr in versions
        if (match := VERSION_PR.match(pr["title"])) and pr.get("mergedAt")
    )
    mine = [index for index, (_, name) in enumerate(times) if name == version]
    if not mine or mine[0] == 0:
        return None
    start, end = times[mine[0] - 1][0], times[mine[0]][0]
    merged = gh_json(
        "pr", "list", "--base", "develop", "--state", "merged", "--limit", "200",
        "--search", f"merged:{start}..{end}", "--json", "number,title,mergedAt",
    )  # fmt: skip
    return sorted(
        pr["number"]
        for pr in merged
        if start < pr["mergedAt"] <= end and not VERSION_PR.match(pr["title"])
    )


def contains_line(version: str) -> str:
    """The contained PRs, best effort: the links alone decide the exit code."""
    try:
        numbers = contained(version)
    except CliError:
        return "contains unknown (GitHub unavailable)"
    if numbers is None:
        return "contains unknown (no previous version PR)"
    return "contains " + (" ".join(f"#{n}" for n in numbers) or "no PRs")


def follow_wait(version: str | None, since: str | None, minutes: int) -> int:
    """Follow until published or failed, printing only what changed; 3 at the
    time limit. A transient GitHub error is a pending round, not the end."""
    deadline = time.monotonic() + minutes * 60
    shown: list[str] = []
    while True:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = _follow_round(version, since)
        lines = out.getvalue().splitlines()
        for line in lines:
            if line not in shown:
                print(line)
        shown = lines
        if code != 3:
            return code
        if time.monotonic() >= deadline:
            print(f"still pending after {minutes} min; follow again")
            return 3
        time.sleep(POLL_SECONDS)


def _follow_round(version: str | None, since: str | None) -> int:
    try:
        return follow(version, since)
    except CliError as exc:
        if not TRANSIENT.search(str(exc)):
            raise
        print("pending GitHub unavailable")
        return 3


def pull(title: str, base: str) -> dict | None:
    found = gh_json(
        "pr", "list", "--base", base, "--state", "all", "--search", f'"{title}" in:title',
        "--limit", "5", "--json", "number,title,state,mergedAt,createdAt,url",
    )  # fmt: skip
    exact = [p for p in found if p["title"] == title]
    return exact[0] if exact else None


def newest_version(since: str | None) -> str | None:
    found = gh_json(
        "pr", "list", "--base", "develop", "--state", "all", "--limit", "10",
        "--search", '"set application version to" in:title',
        "--json", "title,createdAt",
    )  # fmt: skip
    for pr in sorted(found, key=lambda p: p["createdAt"], reverse=True):
        match = VERSION_PR.match(pr["title"])
        if match and (not since or pr["createdAt"] >= since[:19]):
            return match[1]
    return None


def pr_state(pr: dict | None) -> str:
    if pr is None:
        return "pending"
    if pr["state"] == "MERGED":
        return "ok"
    return "failed" if pr["state"] == "CLOSED" else "pending"


def failed_release_run(promotion: dict | None) -> str | None:
    """The newest Daily release run since the promotion merged, when it failed:
    its failed step and the rerun command (#4264); None otherwise. A run that
    publish completions trigger carries the develop head, not the promotion
    commit, so it is matched by time; skipped runs belong to other branches."""
    merged = (promotion or {}).get("mergedAt")
    if not merged:
        return None
    try:
        runs = gh_json(
            "run", "list", "--workflow", WORKFLOW, "--event", "workflow_run",
            "--limit", "30", "--json", "databaseId,createdAt,conclusion",
        )  # fmt: skip
        mine = [
            r for r in runs if r["createdAt"] >= merged and r["conclusion"] != "skipped"
        ]
        if not mine or mine[0]["conclusion"] != "failure":
            return None
        run_id = mine[0]["databaseId"]
        jobs = gh_json("run", "view", str(run_id), "--json", "jobs")["jobs"]
    except CliError as exc:
        if TRANSIENT.search(str(exc)):
            return None
        raise
    step = next(
        (s["name"] for j in jobs for s in j.get("steps", []) if s.get("conclusion") == "failure"),
        "unknown",
    )  # fmt: skip
    return f"run {run_id} failed at step {step}; rerun: gh run rerun {run_id} --failed"


def release_state(version: str, promotion: dict | None = None) -> tuple[str, str]:
    try:
        data = gh_json(
            "release", "view", version, "--json", "tagName,isDraft,targetCommitish"
        )
    except CliError:
        if pr_state(promotion) == "ok" and (failed := failed_release_run(promotion)):
            return "failed", failed
        return "pending", "not published yet"
    if data.get("isDraft"):
        return "pending", "draft"
    return "ok", f"published, target {str(data.get('targetCommitish'))[:12]}"


def publish_state(version: str) -> tuple[str, str]:
    runs = gh_json(
        "run", "list", "--workflow", PUBLISH, "--event", "release", "--limit", "10",
        "--json", "databaseId,status,conclusion,headBranch,url",
    )  # fmt: skip
    mine = [r for r in runs if r.get("headBranch") == version]
    if not mine:
        return "pending", "no publish run yet"
    run = mine[0]
    if run["status"] != "completed":
        return "pending", f"run {run['databaseId']} {run['status']}"
    state = "ok" if run["conclusion"] in GREEN else "failed"
    return state, f"run {run['databaseId']} {run['conclusion']}"


def follow(version: str | None, since: str | None) -> int:
    version = version or newest_version(since)
    if not version:
        print("pending version PR: none found yet")
        return 3
    links: list[tuple[str, str]] = []
    pr = None
    for name, title, base in (
        (
            "version PR",
            f"chore(release): set application version to {version}",
            "develop",
        ),
        ("promotion PR", f"chore(release): promote {version} to main", "main"),
    ):
        pr = pull(title, base)
        detail = f"#{pr['number']} {pr['state']}" if pr else "not created yet"
        links.append((pr_state(pr), f"{name} {detail}"))
    state, detail = release_state(version, pr)  # pr: the promotion PR
    links.append((state, f"release {version} {detail}"))
    state, detail = publish_state(version)
    links.append((state, f"publish {detail}"))
    for state, text in links:
        print(f"{state} {text}")
    if links[0][0] == "ok":
        print(contains_line(version))
    states = {state for state, _ in links}
    if "failed" in states:
        return 1
    return 3 if "pending" in states else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("dispatch")
    start.add_argument("--version", type=version_arg)
    start.add_argument("--when-green", action="store_true")
    start.add_argument("--when-quiet", action="store_true")
    start.add_argument("--quiet", type=int, default=QUIET_MINUTES, help="minutes")
    start.add_argument(
        "--timeout", type=int, default=WHEN_GREEN_MINUTES, help="minutes"
    )
    track = sub.add_parser("follow")
    track.add_argument("--version", type=version_arg)
    track.add_argument("--since")
    track.add_argument("--wait", action="store_true")
    track.add_argument("--timeout", type=int, default=WAIT_MINUTES, help="minutes")
    args = parser.parse_args(argv)
    try:
        if args.command == "dispatch":
            if args.when_quiet and (busy := when_quiet(args.timeout, args.quiet)):
                return busy
            if args.when_green and (green := when_green(args.timeout)):
                return green
            return dispatch(args.version)
        if args.wait:
            return follow_wait(args.version, args.since, args.timeout)
        return follow(args.version, args.since)
    except CliError as exc:
        if args.command == "follow" and TRANSIENT.search(str(exc)):
            print(f"pending GitHub unavailable, follow again: {exc}")
            return 3
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

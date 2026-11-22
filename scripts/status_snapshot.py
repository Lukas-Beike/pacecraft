"""Print a compact, read-only repository status from the GitHub CLI.

With --changes, the snapshot's digest is compared with the one in STATE_FILE:
exit 0 and one line when nothing changed, else the snapshot is printed with the
PRs merged since the last reported change, the new state stored, and the exit
code is 3, so a small scheduled task can wake the orchestrator only on a change
(#3926). The state file is fixed, so no command-line text reaches a path or a
gh argument.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

PASSING = {"SUCCESS", "SKIPPED", "NEUTRAL"}
NONE = "  none"
SUBJECT_PR = re.compile(r"\(#(\d+)\)\s*$")
CHANGED = 3
STATE_FILE = Path.home() / ".pacecraft" / "status-snapshot.json"


class SnapshotError(RuntimeError):
    pass


def gh(*args: str) -> str:
    try:
        done = subprocess.run(
            ["gh", *args], check=False, capture_output=True, text=True, encoding="utf-8"
        )
    except FileNotFoundError as exc:
        raise SnapshotError("The GitHub CLI (gh) is not installed.") from exc
    if done.returncode != 0:
        detail = (done.stderr or "").strip().splitlines()
        raise SnapshotError(
            f"gh {' '.join(args[:2])} failed: {detail[0] if detail else 'no output'}"
        )
    return done.stdout


def gh_json(*args: str):
    try:
        return json.loads(gh(*args) or "null")
    except json.JSONDecodeError as exc:
        raise SnapshotError(f"gh {' '.join(args[:2])} returned invalid JSON.") from exc


def latest_release(repo: str) -> dict:
    data = gh_json("release", "view", "--repo", repo, "--json", "tagName,publishedAt")
    return {"tag": data["tagName"], "published_at": data["publishedAt"]}


def release_promotion(repo: str, tag: str) -> dict | None:
    """Main is squash-promoted, so locate the promotion PR instead of comparing tags."""
    prs = gh_json(
        "pr", "list", "--repo", repo, "--base", "main", "--state", "merged",
        "--limit", "20", "--json", "number,title,mergedAt,headRefOid",
    )  # fmt: skip
    for pr in prs:
        if re.search(rf"\b{re.escape(tag)}\b", pr["title"]):
            return pr
    return prs[0] if prs else None


def merged_since(repo: str, promotion: dict | None) -> list[dict]:
    if not promotion:
        return []
    data = gh_json(
        "api", f"repos/{repo}/compare/{promotion['headRefOid']}...develop",
        "--jq", "[.commits[] | {sha: .sha, message: .commit.message, "
        "date: .commit.committer.date}]",
    )  # fmt: skip
    merged = []
    for commit in data:
        subject = commit["message"].splitlines()[0]
        match = SUBJECT_PR.search(subject)
        merged.append(
            {
                "number": int(match.group(1)) if match else None,
                "title": SUBJECT_PR.sub("", subject).rstrip(),
                "merged_at": commit["date"],
            }
        )
    return merged


def open_pull_requests(repo: str) -> list[dict]:
    data = gh_json(
        "pr", "list", "--repo", repo, "--state", "open", "--limit", "50", "--json",
        "number,title,baseRefName,mergeStateStatus,autoMergeRequest,statusCheckRollup",
    )  # fmt: skip
    return [
        {
            "number": pr["number"],
            "title": pr["title"],
            "base": pr["baseRefName"],
            "merge_state": pr["mergeStateStatus"],
            "auto_merge": bool(pr.get("autoMergeRequest")),
            "open_checks": open_checks(pr.get("statusCheckRollup") or []),
        }
        for pr in data
    ]


def open_checks(rollup: list[dict]) -> list[str]:
    names = []
    for item in rollup:
        if "context" in item:
            state, name = item.get("state"), item["context"]
        else:
            name = item.get("name", "?")
            state = (
                item.get("conclusion")
                if item.get("status") == "COMPLETED"
                else "PENDING"
            )
        if state not in PASSING:
            names.append(f"{name} ({str(state or 'PENDING').lower()})")
    return names


def release_runs(repo: str) -> list[dict]:
    data = gh_json(
        "run", "list", "--repo", repo, "--workflow", "daily-release.yml",
        "--limit", "5", "--json", "event,status,conclusion,createdAt",
    )  # fmt: skip
    return [
        {
            "event": run["event"],
            "status": run["status"],
            "conclusion": run["conclusion"] or None,
            "created_at": run["createdAt"],
        }
        for run in data
    ]


def develop_runs(repo: str) -> list[dict]:
    """The push runs of the newest develop commit that has any."""
    data = gh_json(
        "run", "list", "--repo", repo, "--branch", "develop", "--event", "push",
        "--limit", "20", "--json", "workflowName,status,conclusion,headSha",
    )  # fmt: skip
    head = data[0]["headSha"] if data else None
    return [
        {
            "workflow": run["workflowName"],
            "status": run["status"],
            "conclusion": run["conclusion"] or None,
            "head": run["headSha"][:9],
        }
        for run in data
        if run["headSha"] == head
    ]


def merged_after(repo: str, since: str) -> list[dict]:
    data = gh_json(
        "pr", "list", "--repo", repo, "--state", "merged", "--search",
        f"merged:>={since}", "--limit", "100", "--json", "number,title,mergedAt",
    )  # fmt: skip
    return [
        {"number": pr["number"], "title": pr["title"], "merged_at": pr["mergedAt"]}
        for pr in data
    ]


def open_issues(repo: str) -> list[int]:
    data = gh_json(
        "issue", "list", "--repo", repo, "--state", "open", "--limit", "500",
        "--json", "number",
    )  # fmt: skip
    return sorted(issue["number"] for issue in data)


def labelled_issues(repo: str, label: str) -> list[dict]:
    data = gh_json(
        "issue", "list", "--repo", repo, "--state", "open", "--label", label,
        "--limit", "50", "--json", "number,title",
    )  # fmt: skip
    return [{"number": issue["number"], "title": issue["title"]} for issue in data]


def collect(repo: str, since: str | None = None) -> dict:
    release = latest_release(repo)
    promotion = release_promotion(repo, release["tag"])
    extra = {"merged_since": merged_after(repo, since)} if since else {}
    return extra | {
        "repo": repo,
        "release": release,
        "promotion_pr": promotion["number"] if promotion else None,
        "merged_since_release": merged_since(repo, promotion),
        "open_prs": open_pull_requests(repo),
        "release_runs": release_runs(repo),
        "release_blockers": labelled_issues(repo, "release-blocker"),
        "in_progress": labelled_issues(repo, "in progress"),
        "develop_runs": develop_runs(repo),
        "open_issues": open_issues(repo),
    }


def render(snapshot: dict) -> str:
    release = snapshot["release"]
    lines = [
        f"{snapshot['repo']}: release {release['tag']} ({release['published_at']})",
        f"Merged on develop since release ({len(snapshot['merged_since_release'])}):",
    ]
    lines += [
        f"  {'#' + str(pr['number']) + ' ' if pr['number'] else ''}"
        f"{pr['title']} ({pr['merged_at']})"
        for pr in snapshot["merged_since_release"]
    ] or [NONE]
    lines.append(f"Open PRs ({len(snapshot['open_prs'])}):")
    for pr in snapshot["open_prs"]:
        auto = "auto-merge" if pr["auto_merge"] else "no auto-merge"
        checks = ", ".join(pr["open_checks"]) or "all checks passing"
        lines.append(f"  #{pr['number']} {pr['merge_state']}, {auto}: {checks}")
    if not snapshot["open_prs"]:
        lines.append(NONE)
    lines.append("Daily release runs:")
    lines += [
        f"  {run['created_at']} {run['event']} {run['status']}"
        f" {run['conclusion'] or '-'}"
        for run in snapshot["release_runs"]
    ] or [NONE]
    for key, title in (
        ("release_blockers", "Release blockers"),
        ("in_progress", "In progress"),
    ):
        lines.append(f"{title}:")
        lines += [f"  #{i['number']} {i['title']}" for i in snapshot[key]] or [NONE]
    lines.append("Develop push runs:")
    lines += [
        f"  {run['head']} {run['workflow']}: {run['conclusion'] or run['status']}"
        for run in snapshot["develop_runs"]
    ] or [NONE]
    if "merged_since" in snapshot:
        lines.append(f"Merged since the last change ({len(snapshot['merged_since'])}):")
        lines += [
            f"  #{pr['number']} {pr['title']} ({pr['merged_at']})"
            for pr in snapshot["merged_since"]
        ] or [NONE]
    issues = snapshot["open_issues"]
    lines.append(f"Open issues ({len(issues)}): " + " ".join(f"#{n}" for n in issues))
    return "\n".join(lines)


def read_states() -> dict:
    try:
        states = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}
    return states if isinstance(states, dict) else {}


def last_state(repo: str) -> tuple[str, str | None]:
    """The repository's stored digest and the time of its last reported change."""
    try:
        state = read_states()[repo]
        at = datetime.fromtimestamp(int(state["at"]), UTC)
        return str(state["digest"]), at.strftime("%Y-%m-%dT%H:%M:%SZ")
    except OSError, ValueError, KeyError, TypeError:
        return "", None


def digest(snapshot: dict) -> str:
    """The snapshot without the time-window list, which moves with every change."""
    stable = {key: value for key, value in snapshot.items() if key != "merged_since"}
    text = json.dumps(stable, sort_keys=True).encode("utf-8")
    return hashlib.sha256(text).hexdigest()


def store_state(repo: str, value: str, started: int) -> None:
    """Keep each repository's state apart; ``started`` precedes the reads, so a
    PR merged while they ran falls into the next window."""
    states = read_states()
    states[repo] = {"digest": value, "at": started}
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(states), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--repo", help="OWNER/NAME (default: current repository)")
    parser.add_argument(
        "--changes",
        action="store_true",
        help="exit 0 when unchanged since the last change, else print and exit 3",
    )
    args = parser.parse_args(argv)
    try:
        repo = (
            args.repo
            or gh(
                "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"
            ).strip()
        )
        started = int(datetime.now(UTC).timestamp())
        stored, since = last_state(repo) if args.changes else ("", None)
        snapshot = collect(repo, since)
    except SnapshotError as exc:
        print(f"status_snapshot: {exc}", file=sys.stderr)
        return 1
    if args.changes:
        if digest(snapshot) == stored:
            print("status_snapshot: unchanged")
            return 0
        store_state(repo, digest(snapshot), started)
    print(json.dumps(snapshot, indent=2) if args.json else render(snapshot))
    return CHANGED if args.changes else 0


if __name__ == "__main__":
    sys.exit(main())

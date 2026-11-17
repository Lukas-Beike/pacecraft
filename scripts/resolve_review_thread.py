"""Resolve one review thread on the agent's own pull request after it replied.

Athlete decision on #2629: the only GitHub write this script makes is the
`resolveReviewThread` GraphQL mutation, and only when the thread belongs to the
named pull request, the pull request was authored by the authenticated `gh`
user, the thread is still unresolved, and its last comment is that user's reply
naming a commit of the pull request made after the finding (the fix SHA).
The PreToolUse hook `scripts/hooks/guard_resolve_thread.py` lets only the exact
call run, and only while this script matches origin/develop and nothing in
`scripts/` can shadow a module it imports. It therefore imports no local module.

    python scripts/resolve_review_thread.py <pr> <thread-id>

Exit 0: resolved, or already resolved on the named pull request of that user
(nothing is sent; #3317). 1: refused (the reason is printed). 2: bad input or a
failed `gh` call.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from typing import Any

# The thread is looked up directly, so no thread list needs paging.
QUERY = (
    "query($id:ID!){node(id:$id){... on PullRequestReviewThread{isResolved "
    "pullRequest{number author{login} commits(last:100){nodes{commit{oid "
    "committedDate}}}} first:comments(first:1){nodes{createdAt}} "
    "comments(last:20){nodes{author{login} body}}}}}"
)
# The one mutation this script may send (#2629).
MUTATION = (
    "mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{isResolved}}}"
)
THREAD_ID = re.compile(r"PRRT_[A-Za-z0-9_-]+")
SHA = re.compile(r"\b[0-9a-f]{7,40}\b")
# The goal state, not a refusal: Gitar resolves its own thread once the reply
# names the fix commit (#3317).
ALREADY_RESOLVED = "the thread is already resolved"


class CliError(RuntimeError):
    """A failed `gh` call; the message is safe to print."""


def gh_json(*args: str) -> Any:
    """Run `gh` with list arguments (no shell) and parse its JSON output."""
    try:
        done = subprocess.run(
            ["gh", *args], check=False, capture_output=True, text=True, encoding="utf-8"
        )
    except FileNotFoundError as exc:
        raise CliError("gh is not installed.") from exc
    if done.returncode != 0:
        lines = (done.stderr or done.stdout or "").strip().splitlines()
        raise CliError(
            f"gh {' '.join(args[:2])} failed: {lines[0] if lines else 'no output'}"
        )
    try:
        return json.loads(done.stdout or "null")
    except json.JSONDecodeError as exc:
        raise CliError(f"gh {' '.join(args[:2])} returned invalid JSON.") from exc


def positive_int(text: str) -> int:
    value = int(text.lstrip("#"), 10)
    if value <= 0:
        raise ValueError(text)
    return value


def refusal(thread: dict | None, pr: int, viewer: str) -> str | None:
    """Why this thread may not be resolved, or None when it may."""
    pull = thread.get("pullRequest") if thread else None
    if not thread or not pull or pull["number"] != pr:
        return "the thread does not belong to this pull request"
    if (pull.get("author") or {}).get("login") != viewer:
        return "the pull request was not authored by the authenticated user"
    if thread["isResolved"]:
        return ALREADY_RESOLVED
    comments = thread["comments"]["nodes"]
    if len(comments) < 2 or (comments[-1]["author"] or {}).get("login") != viewer:
        return "the thread's last comment is not a reply by the authenticated user"
    finding = thread["first"]["nodes"][0]["createdAt"]
    # A fix is a commit of this pull request made after the finding.
    fixes = [
        node["commit"]["oid"]
        for node in pull["commits"]["nodes"]
        if node["commit"]["committedDate"] > finding
    ]
    named = SHA.findall(comments[-1]["body"])
    if not any(oid.startswith(sha) for sha in named for oid in fixes):
        return "the reply names no commit of this pull request made after the finding"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pr", type=positive_int)
    parser.add_argument("thread")
    args = parser.parse_args(argv)
    if not THREAD_ID.fullmatch(args.thread):
        print("error: the thread id must look like PRRT_...", file=sys.stderr)
        return 2
    try:
        viewer = gh_json("api", "user")["login"]
        data = gh_json(
            "api", "graphql", "-f", f"query={QUERY}", "-f", f"id={args.thread}"
        )
        reason = refusal((data.get("data") or {}).get("node"), args.pr, viewer)
        if reason == ALREADY_RESOLVED:
            print(f"already resolved: {args.thread} on #{args.pr}; nothing to do")
            return 0
        if reason:
            print(f"refused: {reason}")
            return 1
        done = gh_json(
            "api", "graphql", "-f", f"query={MUTATION}", "-f", f"id={args.thread}"
        )
        if not done["data"]["resolveReviewThread"]["thread"]["isResolved"]:
            raise CliError("GitHub did not report the thread as resolved")
    except (CliError, KeyError, TypeError, IndexError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"resolved {args.thread} on #{args.pr}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

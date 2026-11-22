"""Remove merged, clean, idle Git worktrees (dry run unless --apply).

--schedule registers a daily run with --apply from the primary checkout: a
Windows scheduled task, elsewhere the crontab line to add (#3925).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROTECTED_BRANCHES = {"develop", "main"}
DAY = 86400
TASK = "Pacecraft worktree prune"
DAILY_AT = "04:00"


def run(args: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, cwd=cwd, check=False, capture_output=True, text=True, encoding="utf-8"
    )


def git(*args: str, cwd: str | None = None) -> subprocess.CompletedProcess:
    return run(["git", *args], cwd=cwd)


def parse_worktrees(porcelain: str) -> list[dict]:
    entries = []
    for block in porcelain.strip().split("\n\n"):
        entry: dict = {"branch": None, "locked": False, "detached": False}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            if key == "worktree":
                entry["path"] = value
            elif key == "branch":
                entry["branch"] = value.removeprefix("refs/heads/")
            elif key == "detached":
                entry["detached"] = True
            elif key == "locked":
                entry["locked"] = True
        if "path" in entry:
            entries.append(entry)
    return entries


def same_path(left: str, right: str) -> bool:
    return Path(left).resolve() == Path(right).resolve()


def is_clean(path: str) -> bool:
    done = git("status", "--porcelain", "--untracked-files=all", cwd=path)
    return done.returncode == 0 and not done.stdout.strip()


def last_modified(path: str) -> float:
    """Newest of the worktree directory and its checkout/commit log."""
    done = git("rev-parse", "--absolute-git-dir", cwd=path)
    times = [Path(path).stat().st_mtime]
    if done.returncode == 0:
        gitdir = Path(done.stdout.strip())
        for name in ("logs/HEAD", "HEAD"):
            if (gitdir / name).exists():
                times.append((gitdir / name).stat().st_mtime)
    return max(times)


def merge_state(branch: str, base: str) -> str | None:
    """Return how the branch counts as merged, or None."""
    if git("merge-base", "--is-ancestor", branch, base).returncode == 0:
        return "ancestor"
    tip = git("rev-parse", branch).stdout.strip()
    done = run(
        [
            "gh",
            "pr",
            "list",
            "--head",
            branch,
            "--state",
            "merged",
            "--json",
            "headRefOid",
            "--jq",
            ".[].headRefOid",
        ]
    )
    # A reused branch name must not match: the merged PR head has to be the local tip.
    if done.returncode == 0 and tip and tip in done.stdout.split():
        return "pr-merged"
    return None


def is_agent_worktree(path: str) -> bool:
    """A subagent's worktree: ``.claude/worktrees/agent-*``."""
    parts = Path(path).parts
    return (
        len(parts) >= 3
        and parts[-3:-1] == (".claude", "worktrees")
        and (parts[-1].startswith("agent-"))
    )


def content_on_base(branch: str, base: str) -> bool:
    """Whether every file the branch changed since it forked reads the same on the
    base, as after the main session squashed or reworked it into another PR
    (#3290); an empty change counts too, and so does a branch whose every commit
    has a patch-equivalent one on the base (``git cherry``), though the base
    changed those files since. A change the base lacks keeps it."""
    cherry = git("cherry", base, branch)
    marks = [row[:1] for row in cherry.stdout.splitlines() if row.strip()]
    if cherry.returncode == 0 and marks and set(marks) == {"-"}:
        return True
    fork = git("merge-base", base, branch)
    if fork.returncode != 0 or not fork.stdout.strip():
        return False
    # NUL-separated and literal: a name with a space or a glob character stays
    # one exact path (#3294 review).
    changed = git("diff", "--name-only", "-z", fork.stdout.strip(), branch)
    if changed.returncode != 0:
        return False
    files = [name for name in changed.stdout.split("\0") if name]
    if not files:
        return True
    same = git("--literal-pathspecs", "diff", "--quiet", base, branch, "--", *files)
    return same.returncode == 0


def has_open_pr(branch: str) -> bool:
    """Whether an open pull request has this head; a failed query counts as open."""
    found = run(
        ["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "number"]
        + ["--jq", ".[].number"]
    )
    return found.returncode != 0 or bool(found.stdout.strip())


def merge_kind(entry: dict, base: str) -> str | None:
    """How the worktree's branch counts as merged, or None. A subagent worktree
    also counts when its branch has no open PR and its changes are on the base."""
    kind = merge_state(entry["branch"], base)
    if (
        kind is None
        and is_agent_worktree(entry["path"])
        and content_on_base(entry["branch"], base)
        and not has_open_pr(entry["branch"])
    ):
        return "content on base"
    return kind


def assess(entry, primary, current, base, idle_days, now) -> tuple[bool, str]:
    path = entry["path"]
    if same_path(path, primary):
        return False, "primary checkout"
    if same_path(path, current):
        return False, "current worktree"
    if entry["locked"]:
        return False, "locked"
    branch = entry["branch"]
    if not branch or branch in PROTECTED_BRANCHES:
        return False, "no feature branch"
    if not Path(path).is_dir():
        return False, "directory missing (use git worktree prune)"
    if not is_clean(path):
        return False, "uncommitted or untracked changes"
    merged = merge_kind(entry, base)
    if not merged:
        return False, "branch not merged"
    idle = (now - last_modified(path)) / DAY
    if idle < idle_days:
        return False, f"modified {idle:.1f} days ago"
    return True, f"clean, {merged}, idle {idle:.1f} days"


def remove(entry: dict, merged_by_pr: bool) -> str | None:
    """Return an error message, or None on success."""
    done = git("worktree", "remove", entry["path"])
    if done.returncode != 0:
        return (done.stderr.strip().splitlines() or ["worktree remove failed"])[0]
    flag = "-D" if merged_by_pr else "-d"
    done = git("branch", flag, entry["branch"])
    if done.returncode != 0:
        return (done.stderr.strip().splitlines() or ["branch delete failed"])[0]
    return None


def schedule(primary: str, *, windows: bool = os.name == "nt") -> int:
    """Run the prune with --apply daily from the primary checkout."""
    command = f'"{sys.executable}" scripts/prune_worktrees.py --apply'
    if not windows:
        hour, minute = DAILY_AT.split(":")
        print(
            f'Add to crontab: {int(minute)} {int(hour)} * * * cd "{primary}" && {command}'
        )
        return 0
    action = f'cmd /c cd /d "{primary}" && {command}'
    done = run(
        ["schtasks", "/Create", "/F", "/SC", "DAILY", "/ST", DAILY_AT, "/TN", TASK,
         "/TR", action]
    )  # fmt: skip
    if done.returncode != 0:
        print(
            f"prune_worktrees: schtasks failed: {done.stderr.strip()}", file=sys.stderr
        )
        return 1
    print(f"scheduled '{TASK}' daily at {DAILY_AT}: {action}")
    return 0


def main(argv: list[str] | None = None, now: float | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="remove (default: dry run)"
    )
    parser.add_argument("--idle-days", type=float, default=3.0)
    # Warns below this free space on the primary checkout's drive.
    parser.add_argument("--min-free-gb", type=float, default=10.0)
    parser.add_argument("--base", default="origin/develop")
    parser.add_argument(
        "--schedule", action="store_true", help="run --apply daily (#3925)"
    )
    args = parser.parse_args(argv)
    now = time.time() if now is None else now

    listing = git("worktree", "list", "--porcelain")
    top = git("rev-parse", "--show-toplevel")
    if listing.returncode != 0 or top.returncode != 0:
        print("prune_worktrees: not inside a Git repository.", file=sys.stderr)
        return 1
    entries = parse_worktrees(listing.stdout)
    primary, current = entries[0]["path"], top.stdout.strip()
    if args.schedule:
        return schedule(primary)

    failed = False
    for entry in entries:
        ok, reason = assess(entry, primary, current, args.base, args.idle_days, now)
        label = entry["branch"] or "(detached)"
        if not ok:
            print(f"keep   {entry['path']} [{label}]: {reason}")
        elif not args.apply:
            print(f"prune  {entry['path']} [{label}]: {reason} (dry run)")
        else:
            # A squash-merged or content-on-base branch is no ancestor: -D.
            error = remove(entry, merge_kind(entry, args.base) != "ancestor")
            failed |= error is not None
            print(
                f"failed {entry['path']} [{label}]: {error}"
                if error
                else f"removed {entry['path']} [{label}]"
            )

    free_gb = shutil.disk_usage(primary).free / 1024**3
    print(f"Free disk space: {free_gb:.1f} GB")
    if free_gb < args.min_free_gb:
        print(f"WARNING: below {args.min_free_gb:g} GB free.", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

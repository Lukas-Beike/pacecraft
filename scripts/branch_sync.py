"""Rebase the current feature branch onto the target and optionally push it.

    python scripts/branch_sync.py [--onto origin/develop] [--push] [--allow-root <file>]
                                  [--after-pr <number>] [--to <branch>]

--push updates origin/<local name>, or with --to the named origin branch, for a
local name that differs from the pull request's head branch (#3270). A branch
that tracks another origin branch (not develop or main) is refused without --to:
that upstream may be the head under another name or a stacked parent's branch,
which a push would overwrite. The lease guards the target.

Refuses on develop, main, a detached HEAD and a dirty working tree. Fetches
origin (retrying a lock held by a parallel worktree), rebases, checks every
commit subject against the Conventional Commit types, and with --push pushes with --force-with-lease through the repository
hooks (never --no-verify). The hooks need the SonarQube CLI on PATH. A push
that adds a file at the repository root is refused unless --allow-root names it.
A rebase conflict only in generated text (scripts/regenerate_docs.py) takes the
upstream side, regenerates and continues; a conflict block holding method
version lines (METHOD_VERSION, a card's version and Method ID) adds the replayed
bump to upstream's and merges its other lines one by one, such as a card's
Status edited on one side (#3649, #3698); any other conflict stops. A version
bump that the rebase absorbs (both sides bumped to the same number) refuses
after the rebase, unless --bump-method takes the target's number + 1 for it,
regenerates the cards' tables and commits that (#3698). Changed
e2e/ and public/ scripts must pass `node --check` before the push (#3031).
After the rebase, a node_modules that does not match package-lock.json (as
tests/frontend-harness.cjs checks it) gets `npm ci --ignore-scripts` before
anything else, so the push hooks see a current install (#3133).

--after-pr restacks the next layer of a stacked chain once the pull request it
was built on is squash-merged (#3243). After the fetch it refuses unless that
pull request is merged, the target holds its merge commit (a stale or failed
fetch does not) and this branch is built on its last head; it then rebases only
the commits after that head onto the target. Only with --push and once the
rebase, every check above and a dry-run push through the same hooks and lease
passed, it retargets this branch's pull request to the merged one's base,
prints its base and head, waits for the retarget to settle (#2934) and pushes.
A rerun accepts a layer that already holds the merge, so a push the hooks
refused can be finished after the fix. Every refusal comes before the rebase, the retarget
and the push, so a layer is never retargeted or armed on a stale head.

Exit 0: in sync (and pushed); 1: refused, rebase conflict (left in progress:
resolve it or run `git rebase --abort`) or invalid subjects; 2: bad input,
missing SonarQube CLI or a failed git call.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path

import regenerate_docs
from gh_cli import CliError, fetch, gh, gh_json, git
from regenerate_docs import conflict_resolvable, upstream_side

PROTECTED = {"develop", "main"}
TYPES = "build|chore|ci|docs|feat|fix|perf|refactor|revert|style|test"
SUBJECT = re.compile(rf"^(?:{TYPES})(?:\([a-z0-9][a-z0-9-]*\))?!?: \S")
# Wait between retargeting a stacked layer and pushing it (#2934, pr skill).
RETARGET_SETTLE_SECONDS = 60
LOCK = "package-lock.json"
SCRIPT = re.compile(r"^(?:e2e|public)/.+\.(?:js|cjs|mjs)$")


def sonar_dirs(env: Mapping[str, str], home: Path) -> list[Path]:
    local = env.get("LOCALAPPDATA")
    found = [Path(local) / "sonarqube-cli" / "bin"] if local else []
    return [*found, home / ".sonar" / "sonarqube-cli" / "bin", home / ".local" / "bin"]


def push_env(env: Mapping[str, str], home: Path) -> dict[str, str]:
    """A copy of env whose PATH finds the SonarQube CLI, or a CliError."""
    result = dict(env)
    if shutil.which("sonar", path=env.get("PATH")):
        return result
    for directory in sonar_dirs(env, home):
        if shutil.which("sonar", path=str(directory)):
            result["PATH"] = f"{directory}{os.pathsep}{env.get('PATH', '')}"
            return result
    raise CliError(
        "the pre-push hook needs the SonarQube CLI (sonar); install it first "
        "(setup prerequisite) so it is on PATH."
    )


def invalid_subjects(onto: str) -> list[str]:
    subjects = git("log", "--format=%s", f"{onto}..HEAD").splitlines()
    return [s for s in subjects if not SUBJECT.match(s)]


def regenerate(root: Path, documents: tuple[str, ...]) -> None:
    """Rerun only the generators of these documents (the tests replace this)."""
    if regenerate_docs.regenerate(root, documents):
        raise CliError("a generator failed (see above)")


def conflicted_texts(root: Path, conflicts: list[str]) -> dict[str, str] | None:
    """The conflicted files' text when every one is a generated document."""
    if not all(name.startswith(regenerate_docs.GENERATED) for name in conflicts):
        return None
    try:
        return {name: (root / name).read_text(encoding="utf-8") for name in conflicts}
    except OSError, UnicodeDecodeError:
        return None  # deleted on one side, or not text: a person decides


def resolve_generated(root: Path, conflicts: list[str]) -> bool:
    """Take upstream's side of conflicts that lie only in generated text, regenerate
    and stage those documents (#3031); False when a conflict needs a person. A
    failed generator puts the conflicts back as they were."""
    texts = conflicted_texts(root, conflicts)
    if texts is None or not all(conflict_resolvable(n, t) for n, t in texts.items()):
        return False
    for name, text in texts.items():
        (root / name).write_text(upstream_side(text), encoding="utf-8", newline="\n")
    try:
        regenerate(root, tuple(conflicts))
    except CliError as exc:
        git("checkout", "--merge", "--", *conflicts)
        print(f"regenerating failed, conflicts restored: {exc}")
        return False
    git("add", *conflicts)
    # Only the conflicted documents change in this commit; the rest stays as replayed.
    others = [n for n in git("diff", "--name-only").split() if n not in conflicts]
    if others:
        git("checkout", "--", *others)
    print(f"regenerated after a conflict: {', '.join(conflicts)}")
    return True


# A method's version lines (#3649): the code's METHOD_VERSION, a card's version
# and its Method ID with the issues behind it.
VERSION = re.compile(r"^(METHOD_VERSION = |- \*\*Card version:\*\* )(\d+)$")
METHOD_ID = re.compile(r"^(- \*\*Method ID:\*\* \S+ v)(\d+) \((.*)\)$")
BLOCK = re.compile(
    r"^<<<<<<< [^\n]*\n(.*?)^\|\|\|\|\|\|\| [^\n]*\n(.*?)^=======\n(.*?)^>>>>>>> [^\n]*\n",
    re.MULTILINE | re.DOTALL,
)


# --bump-method: both sides bumped to the same number take upstream's + 1
# instead of stopping for a person (#3698).
BUMP_ON_TOP = [False]


def bumped(ours: str, base: str, theirs: str) -> str | None:
    """One version line with the replayed commit's bump on top of upstream's,
    or None when the three lines are not the same kind of version line."""
    for pattern in (VERSION, METHOD_ID):
        found = [pattern.match(line) for line in (ours, base, theirs)]
        if all(found):
            o, b, t = (m for m in found if m)
            break
    else:
        return None
    # Both bumped to the same number: the code line merged silently there, so
    # the card stays a conflict for a person rather than run ahead (#3649).
    same = int(o[2]) == int(t[2]) != int(b[2])
    if same and not BUMP_ON_TOP[0]:
        return None
    number = int(o[2]) + 1 if same else int(o[2]) + int(t[2]) - int(b[2])
    if pattern is VERSION:
        return f"{o[1]}{number}"
    issues = o[3].split(", ")
    issues += [i for i in t[3].split(", ") if i not in b[3].split(", ") + issues]
    return f"{o[1]}{number} ({', '.join(issues)})"


def version_bumps(onto: str) -> set[str]:
    """The files whose method version lines this branch changes against ``onto``."""
    files, name = set(), ""
    for line in git("diff", "-U0", f"{onto}...HEAD").splitlines():
        if line.startswith("+++ "):
            name = line.removeprefix("+++ b/")
        elif line.startswith("+") and (
            VERSION.match(line[1:]) or METHOD_ID.match(line[1:])
        ):
            files.add(name)
    return files


def absorbed(onto: str, bumps: set[str]) -> bool:
    """Whether the rebase absorbed a version bump: both sides bumped to the
    same number, which git merges without a conflict (#3649)."""
    lost = sorted(bumps - version_bumps(onto))
    if lost:
        print(f"refused: the version bump in {', '.join(lost)} equals {onto}'s now")
        print("the branch is back as it was: bump it on top of the target and rerun")
    return bool(lost)


def merged_line(ours: str, base: str, theirs: str) -> str | None:
    """One line of a block: a version line bumped, another line taken from the
    side that changed it (a card's Status edit next to its version, #3698),
    None when both changed it differently."""
    if VERSION.match(ours) or METHOD_ID.match(ours):
        return bumped(ours, base, theirs)
    if theirs in (ours, base):
        return ours
    return theirs if ours == base else None


def merged_block(match: re.Match[str]) -> str:
    """A diff3 conflict block holding version lines, merged line by line when
    every other line changed on one side only; else unchanged."""
    sides = [match[n].splitlines() for n in (1, 2, 3)]
    if not sides[0] or len({len(side) for side in sides}) > 1:
        return match[0]
    lines = [merged_line(*trio) for trio in zip(*sides, strict=True)]
    if None in lines or not any(
        VERSION.match(o) or METHOD_ID.match(o) for o in sides[0]
    ):
        return match[0]
    return "".join(f"{line}\n" for line in lines)


def resolve_versions(root: Path, conflicts: list[str]) -> list[str]:
    """Resolve conflicts that lie only in method version lines, adding the
    replayed commit's bump to upstream's (#3649); returns the files still
    conflicted. Other blocks stay for the generated-text rule or a person."""
    left = []
    for name in conflicts:
        try:
            git("checkout", "--conflict=diff3", "--", name)
            text = (root / name).read_text(encoding="utf-8")
        except CliError, OSError, UnicodeDecodeError:
            left.append(name)  # deleted, renamed or not text: a person decides
            continue
        merged = BLOCK.sub(merged_block, text)
        if merged == text:
            left.append(name)
            continue
        (root / name).write_text(merged, encoding="utf-8", newline="")
        print(f"merged the version lines of {name}")
        # Other blocks, such as a card's generated table, stay for the
        # generated-text rule or a person (Gitar on #3663).
        if "<<<<<<< " in merged:
            left.append(name)
        else:
            git("add", name)
    return left


def stopped(onto: str, conflicts: list[str], error: CliError) -> bool:
    print(f"conflict: rebase onto {onto} stopped in {len(conflicts)} file(s)")
    for name in conflicts:
        print(f"  {name}")
    print("hint: resolve and `git rebase --continue`, or `git rebase --abort`")
    print(f"detail: {error}", file=sys.stderr)
    return False


def remote_tip(name: str) -> str:
    """This checkout's origin/<name> commit, or "" for a branch never pushed: a
    lease on "" only lets the push create it while it stays absent."""
    try:
        return git("rev-parse", "--verify", "--quiet", f"origin/{name}").strip()
    except CliError:
        return ""


def push_target(branch: str, to: str | None, *, push: bool = True) -> str | None:
    """The origin branch a push updates (#3270): ``to`` when given, else the
    branch's own name. None, with the reason, when ``to`` names develop or main,
    or when the branch tracks another origin branch and ``to`` does not name it:
    that upstream may be the pull request's head under another local name, or a
    stacked parent's branch a push would overwrite, and only the caller knows.
    An upstream of develop or main (a branch started from origin/develop) or
    none at all keeps the branch's own name. Without a push it is the own name."""
    if not push:
        return branch
    if to is not None:
        if to in PROTECTED:
            print(f"refused: --to {to}; push a feature branch")
            return None
        return to
    try:
        remote = git("config", f"branch.{branch}.remote").strip()
        merge = git("config", f"branch.{branch}.merge").strip()
    except CliError:
        return branch  # no upstream yet: the first push sets it
    name = merge.removeprefix("refs/heads/")
    if remote == "origin" and name != merge and name not in {branch, *PROTECTED}:
        print(
            f"refused: {branch} tracks origin/{name}; pass --to {name} to update "
            f"that branch, or --to {branch} to push under this name"
        )
        return None
    return branch


def contains(ancestor: str, ref: str) -> bool:
    """Whether ``ref`` holds commit ``ancestor`` (False also for an unknown commit)."""
    try:
        git("merge-base", "--is-ancestor", ancestor, ref)
        return True
    except CliError:
        return False


def merged_parent(number: int, onto: str, branch: str) -> tuple[str, str] | None:
    """The last head and the base of merged pull request ``number``, once the
    fetched target holds its merge and this branch builds on that head (#3243);
    None, with the reason, otherwise."""
    pr = gh_json(
        "pr", "view", str(number), "--json", "state,headRefOid,mergeCommit,baseRefName"
    ) or {}  # fmt: skip
    merge = (pr.get("mergeCommit") or {}).get("oid") or ""
    head = pr.get("headRefOid") or ""
    if pr.get("state") != "MERGED" or not merge:
        print(f"refused: #{number} is {pr.get('state')}, not merged")
    elif not contains(merge, onto):
        print(
            f"refused: {onto} does not contain the merge of #{number}; "
            "the fetch is stale or failed, fetch again"
        )
    elif contains(merge, "HEAD"):
        # Restacked already (a rerun after a failed push): nothing to drop.
        return "", str(pr.get("baseRefName") or "develop")
    elif not head or not contains(head, "HEAD"):
        print(f"refused: {branch} is not built on #{number}'s last head")
    else:
        return head, str(pr.get("baseRefName") or "develop")
    return None


def retarget(
    branch: str,
    parent: tuple[str, str] | None,
    *,
    push: bool,
    rehearse: Callable[[], object],
) -> None:
    """After a verified restack, point this branch's pull request at the merged
    parent's base and print its base and the head to be pushed; then wait for
    the retarget to settle, because a push right after it can leave the required
    SonarCloud check cancelled (#2934). Only a layer that is pushed is retargeted,
    and only after ``rehearse`` (a dry-run push through the same hooks and lease)
    passed, so a push that would fail leaves the pull request as it was."""
    if parent is None:
        return
    if not push:
        print("not retargeted: push the restacked layer with --push")
        return
    rehearse()
    base = parent[1]
    pr = gh_json("pr", "view", branch, "--json", "number,baseRefName") or {}
    number = pr["number"]
    if pr.get("baseRefName") != base:
        gh("pr", "edit", str(number), "--base", base)
        time.sleep(RETARGET_SETTLE_SECONDS)
    head = git("rev-parse", "HEAD").strip()
    print(f"PR #{number}: base {base}, head {head[:10]}")


def rebased(onto: str, upstream: str | None = None) -> bool:
    """Rebase onto the target, with ``upstream`` only the commits after it, and
    refuse a version bump the rebase absorbed (#3649). Only the replayed
    commits' bumps count: a merged parent's are not this layer's."""
    bumps = version_bumps(upstream or onto)
    before = git("rev-parse", "HEAD").strip()
    if not replayed(onto, upstream):
        return False
    if BUMP_ON_TOP[0] and (lost := sorted(bumps - version_bumps(onto))):
        try:
            bump_on_top(onto, lost)
        except (CliError, OSError) as exc:
            # A failed bump leaves the branch as it was, as a refusal does
            # (Gitar on #3702).
            git("reset", "-q", "--hard", before)
            print(
                f"refused: bumping the method version failed ({exc}); the branch is back as it was"
            )
            return False
    elif absorbed(onto, bumps):
        # Back to the branch as it was (the tree was clean), so every rerun
        # refuses again until the bump is made (Gitar on #3663).
        git("reset", "-q", "--hard", before)
        return False
    return True


def bump_on_top(onto: str, files: list[str]) -> None:
    """Set each absorbed version line to the target's number + 1, regenerate
    the method cards' tables and commit it (--bump-method, #3698)."""
    root = Path(git("rev-parse", "--show-toplevel").strip())
    for name in files:
        target = {
            m[1]: int(m[2])
            for line in git("show", f"{onto}:{name}").splitlines()
            if (m := VERSION.match(line) or METHOD_ID.match(line))
        }
        text = (root / name).read_text(encoding="utf-8")
        for prefix, number in target.items():
            text = re.sub(
                rf"^{re.escape(prefix)}{number}\b",
                f"{prefix}{number + 1}",
                text,
                flags=re.MULTILINE,
            )
        (root / name).write_text(text, encoding="utf-8", newline="")
    cards = tuple(
        d
        for d in regenerate_docs.GENERATED
        if "performance-methods" in d and (root / d).exists()
    )
    regenerate(root, cards)
    git("add", "--", *files, *cards)
    git(
        "commit",
        "-q",
        "-m",
        f"chore(tooling): bump the method version on top of {onto}",
    )
    print(f"bumped the method version on top of {onto}: {', '.join(files)}")


def replayed(onto: str, upstream: str | None) -> bool:
    """The rebase itself. A conflict only in method version lines or generated
    documents is resolved; any other leaves the rebase in progress and is listed."""
    try:
        git("rebase", *(("--onto", onto, upstream) if upstream else (onto,)))
        return True
    except CliError as exc:
        error = exc
    root = Path(git("rev-parse", "--show-toplevel").strip())
    # Each continue replays at least one more commit, so this ends.
    while True:
        conflicts = git("diff", "--name-only", "--diff-filter=U").splitlines()
        if not conflicts:
            raise error
        conflicts = resolve_versions(root, conflicts)
        if conflicts and not resolve_generated(root, conflicts):
            return stopped(onto, conflicts, error)
        try:
            git("rebase", "--continue", env={**os.environ, "GIT_EDITOR": "true"})
            return True
        except CliError as exc:
            error = exc  # the next replayed commit conflicts too


def unparsable(onto: str) -> list[str]:
    """Changed page and e2e scripts `node --check` rejects: a broken hand merge (#3031)."""
    if not shutil.which("node"):
        print("node not found: the script parse check did not run")
        return []
    root = Path(git("rev-parse", "--show-toplevel").strip())
    changed = git(
        *("-c", "core.quotePath=false", "diff", "--name-only", "--no-renames"),
        *("--diff-filter=AM", f"{onto}...HEAD"),
    ).splitlines()
    return [
        name
        for name in changed
        if SCRIPT.match(name)
        and subprocess.run(
            ["node", "--check", str(root / name)], capture_output=True, check=False
        ).returncode
    ]


def stale_install(root: Path) -> list[str]:
    """Locked packages node_modules lacks or holds at another version, judged
    as tests/frontend-harness.cjs judges them (#2690); an optional package for
    another platform may be absent."""

    def packages(name: str) -> dict:
        try:
            data = json.loads((root / name).read_text(encoding="utf-8"))
        except OSError, ValueError:
            return {}
        found = data.get("packages") if isinstance(data, dict) else None
        return found if isinstance(found, dict) else {}

    locked, installed = packages(LOCK), packages("node_modules/.package-lock.json")
    return [
        key
        for key, entry in locked.items()
        if key.startswith("node_modules/")
        and isinstance(entry, dict)
        and (installed.get(key) or {}).get("version") != entry.get("version")
        and (installed.get(key) or not entry.get("optional"))
    ]


def refreshed_node_modules() -> bool:
    """`npm ci --ignore-scripts` when node_modules does not match the lock file,
    as after a rebase onto a new dependency, also when that rebase finished in
    an earlier run: the pre-push hook would only fail later (#3133)."""
    root = Path(git("rev-parse", "--show-toplevel").strip())
    if not stale_install(root):
        return True
    print(f"node_modules does not match {LOCK}: npm ci --ignore-scripts")
    npm = shutil.which("npm")
    if npm is None:
        print("refused: npm not found; run npm ci --ignore-scripts")
        return False
    code = subprocess.run([npm, "ci", "--ignore-scripts"], cwd=root, check=False)
    if code.returncode:
        print(f"refused: npm ci failed (exit {code.returncode}); run it again")
    return code.returncode == 0


def outgoing_problems(onto: str, push: bool, allowed: frozenset[str]) -> bool:
    """Print invalid subjects and, for a push, new root files; True if any."""
    problems = [
        ("invalid commit subjects (need <type>(<scope>): <description>):", s)
        for s in invalid_subjects(onto)
    ]
    problems += [
        ("scripts that do not parse (node --check):", n) for n in unparsable(onto)
    ]
    if push:
        added = git(
            *("-c", "core.quotePath=false", "diff", "--name-only", "--no-renames"),
            *("--diff-filter=A", f"{onto}...HEAD"),
        )
        # A stray root file is usually a session artefact (a mistyped redirect).
        problems += [
            ("refused: new root files; remove them or pass --allow-root <file>:", n)
            for n in added.splitlines()
            if n and "/" not in n and n not in allowed
        ]
    heading = None
    for title, item in problems:
        if title != heading:
            print(title)
            heading = title
        print(f"  {item}")
    return bool(problems)


def sync(
    onto: str,
    push: bool,
    allowed: frozenset[str] = frozenset(),
    after_pr: int | None = None,
    to: str | None = None,
) -> int:
    branch = git("rev-parse", "--abbrev-ref", "HEAD").strip()
    if branch == "HEAD" or branch in PROTECTED:
        print(
            f"refused: on {branch if branch != 'HEAD' else 'a detached HEAD'}; use a feature branch"
        )
        return 1
    if git("status", "--porcelain", "--untracked-files=no").strip():
        print("refused: uncommitted changes; commit or stash them first")
        return 1
    target = push_target(branch, to, push=push)
    if target is None:
        return 1
    env = push_env(os.environ, Path.home()) if push else None
    # The lease must name the remote tip seen before this run's fetch, or the
    # fetch moves origin/<target> and the lease can no longer protect remote work.
    leased = remote_tip(target)
    fetch()
    parent = None if after_pr is None else merged_parent(after_pr, onto, branch)
    if after_pr is not None and parent is None:
        return 1
    if not rebased(onto, parent[0] if parent else None):
        return 1
    if not refreshed_node_modules():
        return 1
    if outgoing_problems(onto, push, allowed):
        return 1
    hooks, lease = ("-c", "core.hooksPath=.githooks", "push"), (
        f"--force-with-lease={target}:{leased}", "-u", "origin", f"{branch}:{target}"
    )  # fmt: skip
    # A push its hooks or lease would refuse fails before the retarget (#3243 review).
    retarget(
        target,
        parent,
        push=push,
        rehearse=lambda: git(*hooks, "--dry-run", *lease, env=env),
    )
    if push:
        git(*hooks, *lease, env=env)
        print(f"pushed: {branch} -> origin/{target}")
    behind, ahead = git("rev-list", "--left-right", "--count", f"{onto}...HEAD").split()
    print(
        f"head {git('rev-parse', '--short', 'HEAD').strip()} on {branch}: ahead {ahead}, behind {behind}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--onto", default="origin/develop")
    parser.add_argument("--push", action="store_true")
    parser.add_argument("--allow-root", action="append", default=[])
    parser.add_argument(
        "--after-pr",
        type=int,
        metavar="NUMBER",
        help="restack onto the target after this merged pull request (#3243)",
    )
    parser.add_argument(
        "--bump-method",
        action="store_true",
        help="when both sides bumped a method version to the same number, take the target's + 1 (#3698)",
    )
    parser.add_argument(
        "--to",
        metavar="BRANCH",
        help="the origin branch --push updates, when it differs from the local name (#3270)",
    )
    args = parser.parse_args(argv)
    BUMP_ON_TOP[0] = args.bump_method
    try:
        return sync(
            args.onto, args.push, frozenset(args.allow_root), args.after_pr, args.to
        )
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

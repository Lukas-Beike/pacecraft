"""Write the release changelog the app shows, from local git history (#2899).

    python scripts/release_changelog.py [--output public/changelog.json]

Runs before the shared CI image is built; the image carries the file and the
app reads it from its own server, never from GitHub. Each release tag (x.y.z)
is mapped to the develop commit with the same tree, as the release step does,
and lists the German `Changelog:` trailer of every commit since the previous
release that has one, led by its type (`feat: ...`); commits without the
trailer, merges and both release chores are left out (#3981). Releases are squashed onto main, so the history read is
develop's (origin/develop, else develop, else HEAD's) and a build of a main
commit stands for the develop commit with its tree (#2960). A tag whose tree is
not in that history is skipped with a build warning; its commits count to the
next release. When the build is not itself a release tree, a first section
holds the commits since the last release under APP_VERSION (null while that
version is already released). If
that section's range cannot be computed, the script fails: the running build's
own changes are never left out. Exit 0 written; 1 refused; 2 bad input or a
failed git call.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RELEASE_TAG = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
RELEASE_CHORE = re.compile(
    r"^chore\(release\): (set application version to \d+\.\d+\.\d+|"
    r"promote \d+\.\d+\.\d+ to main)( \(#\d+\))?$"
)
CONVENTIONAL = re.compile(r"^([a-z]+)(?:\([^)]*\))?(!?): ")
DEVELOP_REFS = ("refs/remotes/origin/develop", "refs/heads/develop")
APP_VERSION = re.compile(r'^APP_VERSION = "(\d+\.\d+\.\d+)"$', re.MULTILINE)


class Refused(Exception):
    """The running build's own section cannot be computed."""


def git(*args: str, cwd: Path = ROOT) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",  # commit subjects outside cp1252 on Windows (#2895)
        errors="replace",
    ).stdout


def subjects(commit_range: str, cwd: Path) -> list[str]:
    """One German entry per commit with a ``Changelog:`` trailer, led by its type.

    Subjects stay English; a commit without the trailer is not shown (#3981).
    """
    log = git(
        "log",
        "--no-merges",
        "--format=%s%x1f%(trailers:key=Changelog,valueonly,unfold)%x1e",
        commit_range,
        cwd=cwd,
    )
    entries = []
    for record in log.split("\x1e"):
        subject, _, trailer = record.strip().partition("\x1f")
        text = next((line.strip() for line in trailer.splitlines() if line.strip()), "")
        if not text or RELEASE_CHORE.match(subject):
            continue
        kind = CONVENTIONAL.match(subject)
        entries.append(f"{kind[1]}{kind[2]}: {text}" if kind else text)
    return entries


def develop_line(cwd: Path, warn) -> str:
    """The ref whose history holds the releases' own commits."""
    for ref in DEVELOP_REFS:
        found = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", ref],
            cwd=cwd,
            capture_output=True,
            check=False,
        )
        if found.returncode == 0:
            return ref
    # Without develop a main build lists only its promotions (#2960); say so.
    warn("::warning::No develop ref; the changelog follows HEAD's history.")
    return "HEAD"


def changelog(app_version: str, cwd: Path = ROOT, warn=print) -> dict[str, Any]:
    """Releases newest first, each with its commit subjects."""
    tags = sorted(
        (
            tag
            for tag in git("tag", "--list", cwd=cwd).split()
            if RELEASE_TAG.match(tag)
        ),
        key=lambda tag: tuple(int(part) for part in tag.split(".")),
    )
    # A tag's base is the newest develop commit with its tree that is not newer
    # than the tag, as release_contains.release_base: a later revert to that tree came
    # after the promotion.
    history = [
        line.split()
        for line in git(
            "log", develop_line(cwd, warn), "--format=%H %T %ct", cwd=cwd
        ).splitlines()
    ]
    by_tree: dict[str, list[tuple[str, int]]] = {}
    for commit, tree, stamp in history:
        by_tree.setdefault(tree, []).append((commit, int(stamp)))
    # Tree and commit time of each tag; the starred fields serve annotated tags.
    refs = git(
        "for-each-ref",
        "--format=%(refname:short) %(tree)%(*tree) %(committerdate:unix)%(*committerdate:unix)",
        "refs/tags",
        cwd=cwd,
    )
    stamped = {name: rest for name, *rest in map(str.split, refs.splitlines())}
    releases: list[dict[str, Any]] = []
    previous = ""
    for tag in tags:
        tree, when = stamped[tag]
        base = next((c for c, s in by_tree.get(tree, ()) if s <= int(when)), None)
        if base is None:
            warn(
                f"::warning::Release {tag} is not in this history; its commits count to the next release."
            )
            continue
        commits = subjects(f"{previous}..{base}".lstrip("."), cwd)
        releases.append({"version": tag, "commits": commits})
        previous = base
    if tags and not previous:
        raise Refused(
            "No release tag maps to this history; the build's own changes cannot be listed."
        )
    # A build whose tree a develop commit carries (a main promotion) is that commit.
    tree, when = git("show", "-s", "--format=%T %ct", "HEAD", cwd=cwd).split()
    head = next((c for c, s in by_tree.get(tree, ()) if s <= int(when)), "HEAD")
    if previous != head:
        last = releases[-1]["version"] if releases else None
        releases.append(
            {
                "version": None if app_version in tags else app_version,
                "after": last,
                "commits": subjects(f"{previous}..{head}".lstrip("."), cwd),
            }
        )
    return {"releases": releases[::-1]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--output", type=Path, default=ROOT / "public" / "changelog.json"
    )
    args = parser.parse_args(argv)
    found = APP_VERSION.search((ROOT / "server.py").read_text(encoding="utf-8"))
    if found is None:
        print("error: APP_VERSION not found in server.py", file=sys.stderr)
        return 2
    try:
        data = changelog(found[1])
    except Refused as error:
        print(f"refused: {error}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as error:
        print(f"error: git {' '.join(error.cmd[1:])} failed", file=sys.stderr)
        return 2
    args.output.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    print(f"changelog: {len(data['releases'])} sections -> {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

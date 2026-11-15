"""Regenerate the generated documents, or check them in a HEAD export.

    python scripts/regenerate_docs.py              # rewrite them in this checkout
    python scripts/regenerate_docs.py --check DIR  # DIR: a clean export of HEAD

Generated documents went stale five times and only the long test
selection or CI caught it (#2949). The pre-push hook runs --check in its HEAD
export: the generators rewrite the documents there, and any that differ from
HEAD are named with this script as the fix. The view catalogue needs
node_modules, linked into DIR from PACECRAFT_NODE_MODULES for the run.
Exit 0: current (or rewritten); 1: stale; 2: a generator failed.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIX = "python scripts/regenerate_docs.py"
# Each command writes the documents it names (a file, or a folder prefix) from the
# code, run from the root: whole files, or sections between the markers below.
GENERATORS = (
    (
        [sys.executable, "tests/test_performance_method_cards.py", "--write"],
        ("docs/performance-methods/",),
    ),
    (
        [sys.executable, "tests/test_api_routes_doc.py", "--write"],
        ("docs/api-routes.md",),
    ),
    (
        [sys.executable, "tests/test_key_value_service.py", "--write"],
        ("docs/kv-catalogue.md",),
    ),
    (
        ["node", "tests/navigation-state.test.cjs", "--write"],
        ("docs/view-catalogue.md",),
    ),
)
GENERATED = tuple(path for _, writes in GENERATORS for path in writes)
WHOLLY_GENERATED = {"docs/performance-methods/code-constants.md"}
SECTION = ("<!-- BEGIN GENERATED", "<!-- END GENERATED")


def conflict_resolvable(path: str, text: str) -> bool:
    """Whether every rebase conflict block of a document lies in generated text, so
    taking one side and regenerating loses nothing (#3031). A marker inside a block
    means the hand-written text around a section moved: not resolvable."""
    if not path.startswith(GENERATED):
        return False
    if path in WHOLLY_GENERATED:
        return True
    inside = in_block = found = False
    for line in text.splitlines():
        if line.startswith(SECTION):
            if in_block:
                return False
            inside = line.startswith(SECTION[0])
        elif line.startswith("<<<<<<<"):
            if not inside:
                return False
            in_block = found = True
        elif line.startswith(">>>>>>>"):
            in_block = False
    return found


def upstream_side(text: str) -> str:
    """The text with each conflict block replaced by its first (upstream) side."""
    kept, side = [], "both"
    for line in text.splitlines(keepends=True):
        bare = line.rstrip("\r\n")
        if line.startswith("<<<<<<< "):
            side = "upstream"
        elif side == "upstream" and (bare == "=======" or line.startswith("||||||| ")):
            side = "other"
        elif side == "other" and line.startswith(">>>>>>> "):
            side = "both"
        elif side != "other":
            kept.append(line)
    return "".join(kept)


def documents(root: Path) -> dict[str, bytes]:
    """Every Markdown file under docs/, as stored: a generator that writes CRLF
    on Windows shows up as a change instead of a quiet git diff (#3008)."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted((root / "docs").rglob("*.md"))
    }


@contextlib.contextmanager
def node_modules(root: Path) -> Iterator[None]:
    """Link PACECRAFT_NODE_MODULES into an export for the run, then unlink it.

    Removed here, before the hook deletes the export, so a recursive delete
    never reaches the real folder through the link.
    """
    source, link = os.environ.get("PACECRAFT_NODE_MODULES"), root / "node_modules"
    if not source or os.path.lexists(link):
        yield
        return
    if os.name == "nt":  # a junction needs no symlink privilege
        junction = ["cmd", "/c", "mklink", "/J", str(link), str(Path(source))]
        subprocess.run(junction, check=True, capture_output=True)
    else:
        link.symlink_to(source, target_is_directory=True)
    try:
        yield
    finally:
        (os.rmdir if os.name == "nt" else os.unlink)(link)


def regenerate(root: Path, only: tuple[str, ...] = ()) -> int:
    """Run the generators, or with `only` just those writing one of those documents."""
    commands = [
        command
        for command, writes in GENERATORS
        if not only or any(path.startswith(writes) for path in only)
    ]
    with node_modules(root):
        for command in commands:
            done = subprocess.run(
                command, cwd=root, capture_output=True, text=True, encoding="utf-8",
                errors="replace", check=False,
            )  # fmt: skip
            if done.returncode:
                # node:test ends with counters; its failing tests say "not ok".
                lines = f"{done.stdout}\n{done.stderr}".strip().splitlines()
                shown = [x for x in lines if "not ok" in x][:5] or lines[-3:]
                failed = f"{' '.join(command[1:])} failed:"
                print(failed, *shown, sep="\n  ", file=sys.stderr)
                return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", type=Path, metavar="DIR")
    args = parser.parse_args(argv)
    root = args.check or ROOT
    before = documents(root)
    try:
        status = regenerate(root)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"Generated documents: cannot run the generators: {exc}", file=sys.stderr)
        return 2
    after = documents(root)
    changed = sorted(path for path in after if after[path] != before.get(path))
    if not args.check:
        rewrote = "\n".join(f"rewrote {path}" for path in changed)
        print(rewrote or "Generated documents: unchanged")
        return status
    if status:
        return status
    if changed:
        stale = f"Generated documents are stale: {', '.join(changed)}. Run `{FIX}`."
        print(stale, file=sys.stderr)  # stderr: a refused push shows it
        return 1
    print("Generated documents: current")
    return 0


if __name__ == "__main__":
    sys.exit(main())

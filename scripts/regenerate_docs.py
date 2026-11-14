"""Regenerate the generated documents, or check them in a HEAD export.

    python scripts/regenerate_docs.py              # rewrite them in this checkout
    python scripts/regenerate_docs.py --check DIR  # DIR: a clean export of HEAD

Three generated documents went stale five times and only the long test
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
# Each writes its document(s) under docs/ from the code, run from the root.
GENERATORS = (
    [sys.executable, "tests/test_performance_method_cards.py", "--write"],
    [sys.executable, "tests/test_api_routes_doc.py", "--write"],
    ["node", "tests/navigation-state.test.cjs", "--write"],
)


def documents(root: Path) -> dict[str, str]:
    """Every Markdown file under docs/, line endings normalized."""
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8").replace(
            "\r\n", "\n"
        )
        for path in sorted((root / "docs").rglob("*.md"))
    }


@contextlib.contextmanager
def node_modules(root: Path) -> Iterator[None]:
    """Link PACECRAFT_NODE_MODULES into an export for the run, then unlink it.

    Removed here, before the hook deletes the export, so a recursive delete
    never reaches the real folder through the link.
    """
    source, link = os.environ.get("PACECRAFT_NODE_MODULES"), root / "node_modules"
    if not source or link.exists():
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


def regenerate(root: Path) -> int:
    with node_modules(root):
        for command in GENERATORS:
            done = subprocess.run(
                command, cwd=root, capture_output=True, text=True, encoding="utf-8",
                errors="replace", check=False,
            )  # fmt: skip
            if done.returncode:
                tail = (done.stderr or done.stdout).strip().splitlines()[-3:]
                failed = f"{' '.join(command[1:])} failed:"
                print(failed, *tail, sep="\n  ", file=sys.stderr)
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
    if status or not args.check:
        rewrote = "\n".join(f"rewrote {path}" for path in changed)
        print(rewrote or "Generated documents: unchanged")
        return status
    if changed:
        stale = f"Generated documents are stale: {', '.join(changed)}. Run `{FIX}`."
        print(stale, file=sys.stderr)  # stderr: a refused push shows it
        return 1
    print("Generated documents: current")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Claude Code PreToolUse hook: the review-thread resolve script runs only as itself.

`.claude/settings.json` allows `python scripts/resolve_review_thread.py` without
a prompt (athlete decision #2629). That rule matches command text, so this hook
blocks any shell command that names the script unless the whole command is
exactly `python scripts/resolve_review_thread.py <pr> <PRRT_id>`, the script and
any `scripts/` module it imports match origin/develop byte for byte, and no
entry in `scripts/` (tracked, ignored or untracked) can shadow a module the run
imports (#3072). Other scripts, the hooks and non-Python files may differ.
Exit code 2 blocks the call.
"""

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

EXACT = re.compile(r"python scripts/resolve_review_thread\.py \d+ PRRT_[A-Za-z0-9_-]+")


SCRIPTS = Path("scripts")
RUN = "resolve_review_thread"
# Python imports these at start from the script's directory as well.
STARTUP = frozenset({"sitecustomize", "usercustomize"})


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def same_as_develop(path: Path) -> bool:
    """The file's bytes in the working tree equal its blob on origin/develop."""
    here = _git("hash-object", "--", path.as_posix())
    there = _git("rev-parse", "--verify", "--quiet", f"origin/develop:{path.as_posix()}")
    return (
        not here.returncode
        and not there.returncode
        and here.stdout.strip() == there.stdout.strip() != ""
    )


def local_imports(name: str) -> list[str]:
    """The scripts/ modules one module imports by absolute name."""
    try:
        tree = ast.parse((SCRIPTS / f"{name}.py").read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return []
    names = [
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
        for alias in node.names
    ] + [
        node.module for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and not node.level and node.module
    ]
    tops = {full.split(".")[0] for full in names}
    return [top for top in tops if (SCRIPTS / f"{top}.py").exists() or (SCRIPTS / top).exists()]


def run_modules() -> set[str]:
    """The resolve script and the scripts/ modules it imports, transitively."""
    found: set[str] = set()
    pending = [RUN]
    while pending:
        name = pending.pop()
        if name not in found:
            found.add(name)
            pending.extend(local_imports(name))
    return found


def shadows(entry: str, run: set[str]) -> bool:
    """Whether a scripts/ entry (file, directory, nested repository or gitlink)
    could be imported in place of a module the run uses."""
    name = entry.rstrip("/\\").split(".")[0]
    if name in run:
        return entry != f"{name}.py"
    return name in STARTUP or name in sys.stdlib_module_names


def unchanged() -> bool:
    """The run's own files match origin/develop and nothing in scripts/ can
    shadow a module it imports, tracked, ignored or untracked (#2629, #3072)."""
    run = run_modules()
    if not all(same_as_develop(SCRIPTS / f"{name}.py") for name in run):
        return False
    cache = SCRIPTS / "__pycache__"
    try:
        entries = os.listdir(SCRIPTS)
        cached = os.listdir(cache) if cache.is_dir() else []
    except OSError:
        return False
    # Cached bytecode of a local module could be loaded instead of its source.
    return not any(shadows(entry, run) for entry in entries) and not any(
        entry.split(".")[0] in run - {RUN} for entry in cached
    )


def main() -> int:
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
    except ValueError:
        print("Blocked: unreadable hook input.", file=sys.stderr)
        return 2
    command = str((payload.get("tool_input") or {}).get("command") or "")
    if "resolve_review_thread" not in command:
        return 0
    if EXACT.fullmatch(command.strip()) and unchanged():
        return 0
    print(
        "Blocked: the review-thread resolve script runs only as "
        "`python scripts/resolve_review_thread.py <pr> <PRRT_id>`, alone, from a "
        "checkout whose scripts/ matches origin/develop (#2629).",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())

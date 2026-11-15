"""Claude Code PreToolUse hook: the review-thread resolve script runs only as itself.

`.claude/settings.json` allows `python scripts/resolve_review_thread.py` without
a prompt (athlete decision #2629). That rule matches command text, so this hook
blocks any shell command that names the script unless the whole command is
exactly `python scripts/resolve_review_thread.py <pr> <PRRT_id>` and no change
in `scripts/` against origin/develop, committed or not, can reach that run: the
script and the `scripts/` modules it imports are untouched, and no changed file
can shadow a module the run imports (#3072). Other scripts, the hooks and
non-Python files may differ. Exit code 2 blocks the call.
"""

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

EXACT = re.compile(r"python scripts/resolve_review_thread\.py \d+ PRRT_[A-Za-z0-9_-]+")


SCRIPTS = Path("scripts")
RUN = "resolve_review_thread"
# Python imports these at start from the script's directory as well.
STARTUP = frozenset({"sitecustomize", "usercustomize"})
IMPORTABLE = (".py", ".pyc", ".pyw", ".pyd", ".so")


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def changed_paths() -> list[str] | None:
    """scripts/ paths that differ from origin/develop, committed, edited or new."""
    diff = _git("diff", "--name-only", "origin/develop", "--", "scripts")
    new = _git("ls-files", "--others", "--exclude-standard", "--", "scripts")
    if diff.returncode or new.returncode:
        return None
    return [*diff.stdout.splitlines(), *new.stdout.splitlines()]


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
    return [top for top in tops if (SCRIPTS / f"{top}.py").exists() or (SCRIPTS / top).is_dir()]


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


def reaches_the_run(path: str, run: set[str]) -> bool:
    """Whether a changed scripts/ path can change what the resolve run executes."""
    parts = Path(path).parts
    if len(parts) < 2 or parts[0] != SCRIPTS.name:
        return True
    top = parts[1]
    if len(parts) == 2:
        if not top.endswith(IMPORTABLE):
            return False
        top = top.split(".")[0]
    return top in run or top in STARTUP or top in sys.stdlib_module_names


def unchanged() -> bool:
    """No change in scripts/ against origin/develop can reach the resolve run."""
    paths = changed_paths()
    if paths is None:
        return False
    run = run_modules()
    return not any(reaches_the_run(path, run) for path in paths)


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

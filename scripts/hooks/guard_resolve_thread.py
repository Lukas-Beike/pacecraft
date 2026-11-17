"""Claude Code PreToolUse hook: the review-thread resolve script runs only as itself.

`.claude/settings.json` allows `python scripts/resolve_review_thread.py` without
a prompt (athlete decision #2629). That rule matches command text, so this hook
blocks any shell command that names the script unless the whole command is
exactly `python scripts/resolve_review_thread.py <pr> <PRRT_id>`, the script and
any `scripts/` module it imports match origin/develop byte for byte, and no
entry in `scripts/` (tracked, ignored or untracked) can shadow a module the run
imports (#3072). Other scripts, the hooks and non-Python files may differ.
The name matches in any case, through quotes, escapes, globs and short names,
and input without a command string blocks (#3367). Exit code 2 blocks the call.
"""

import ast
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path

EXACT = re.compile(r"python scripts/resolve_review_thread\.py \d+ PRRT_[A-Za-z0-9_-]+")
NAME = "resolve_review_thread"
TARGETS = (NAME, f"{NAME}.py")
# Shell glob characters, brace expansion and extglob groups.
GLOB = re.compile(r"[*?\[{]|[@+!]\(")
BRACES = re.compile(r"\{[^{}]*(?:,|\.\.)[^{}]*\}")
# A Windows 8.3 short name: up to six characters, a tilde and a digit.
SHORT_NAME = re.compile(r"([a-z0-9_]{1,6})~\d")


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
    """The file's raw bytes in the working tree equal its blob on origin/develop.

    No attribute or filter applies to the working copy, and the base is the
    remote-tracking ref itself, read without replace objects.
    """
    here = _git("hash-object", "--no-filters", "--", path.as_posix())
    there = _git(
        "--no-replace-objects",
        "rev-parse",
        "--verify",
        "--quiet",
        f"refs/remotes/origin/develop:{path.as_posix()}",
    )
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
    name = entry.rstrip("/\\").split(".")[0].casefold()
    # Names compare case-folded, as a case-insensitive file system resolves them.
    if name in {module.casefold() for module in run}:
        return entry not in {f"{module}.py" for module in run}
    return name in {module.casefold() for module in (*STARTUP, *sys.stdlib_module_names)}


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


def glob_pattern(word: str) -> str:
    """A shell glob as an fnmatch pattern; extglob groups and brace expansions
    (a list or a range; `{x}` stays literal in bash) widen to `*`."""
    word = re.sub(r"[@+!?*]\([^)]*\)", "*", word)
    while (wider := BRACES.sub("*", word)) != word:
        word = wider
    return word.replace("[^", "[!")


def names_script(command: str) -> bool:
    """Whether the command names the script in any spelling the shell or a
    case-insensitive file system resolves to it: case, quotes, escapes, globs,
    braces and 8.3 short names (#3367)."""
    text = re.sub(r"[\"'`]", "", command.casefold())
    return any(
        NAME in variant
        or any(word_reaches_script(word) for word in re.split(r"[\s;&|<>=]+", variant))
        for variant in (text.replace("\\", ""), text.replace("\\", "/"))
    )


def word_reaches_script(word: str) -> bool:
    """A glob or 8.3 short name that can resolve to the script's file."""
    *folders, last = word.rstrip("/").split("/")
    other = folders and folders[-1] not in {"", ".", "..", "scripts"}
    # A file in a plainly named other folder is never the script.
    if other and not GLOB.search(folders[-1]):
        return False
    short = SHORT_NAME.match(last)
    if short and NAME.startswith(short[1]):
        return True
    return bool(GLOB.search(last)) and any(
        fnmatch.fnmatchcase(target, glob_pattern(last)) for target in TARGETS
    )


def main() -> int:
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
    except ValueError:
        print("Blocked: unreadable hook input.", file=sys.stderr)
        return 2
    tool_input = payload.get("tool_input") if isinstance(payload, dict) else None
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        # Fail closed (#3367): a shape this hook does not know blocks.
        print("Blocked: hook input without a command string.", file=sys.stderr)
        return 2
    if not names_script(command):
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

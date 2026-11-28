"""Claude Code PreToolUse hook: a review thread is resolved only by its script.

`.claude/settings.json` allows `python scripts/resolve_review_thread.py` without
a prompt (athlete decision #2629). The script checks its own preconditions
(its pull request, its author, an unresolved thread, a reply naming a fix commit)
and refuses otherwise, so this hook asks for two things only (#4289):

- a command that holds the start `python scripts/resolve_review_thread.py` (the
  spelling the allow rule matches) must be exactly that call, with at most a few
  harmless wrappers and variables before it and `2>&1` after it, and it runs only
  while the script and the `scripts/` modules it imports match origin/develop byte
  for byte and no entry in `scripts/` can shadow one of them (#3072); a changed
  script has no checks worth trusting, and whatever runs before, beside or around
  the script in its command could change what the check saw;
- a direct GraphQL resolve mutation through an API call blocks (#3759).

No command is read for the script's name otherwise: read-only commands that
mention the file, globs, short names and other spellings pass, which three earlier
rounds of name recognition (#3759, #3982, #4155) kept blocking by mistake. Exit
code 2 blocks the call; unreadable input blocks (#3367).
"""

import ast
import json
import os
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

# The spelling the allow rule matches: `python scripts/<name>`, after the wrappers and
# environment assignments the rule may strip. Only the exact call runs; any other command
# that holds this start blocks, since what runs before, beside or around the script could
# change what the check saw. Commands that only mention the file (grep, git diff) do not
# hold it.
# Only what cannot change what the script imports or runs: four plain environment
# assignments (a value of word characters, dots and dashes, never a substitution or
# quote), `time`, `nohup`, `timeout <seconds>` and `!`; no option that takes a value
# (`env -C`, `nice -n$(...)`) and no other variable.
WRAPPER = (
    r"(?:(?:PYTHONDONTWRITEBYTECODE|PYTHONUNBUFFERED|PYTHONUTF8|PYTHONIOENCODING)"
    r"=[\w.-]*|nohup|time(?:[ \t]+-p)?|timeout[ \t]+\d+[smhd]?|!)[ \t]+"
)
START = (
    r"(?:py|python[\d.]*)(?:\.exe)?[ \t]+(?:\./)?scripts[/\\]"
    r"resolve_review_thread\.py"
)
INVOCATION = re.compile(START, re.IGNORECASE)
EXACT = re.compile(
    rf"(?:{WRAPPER}){{0,4}}{START}[ \t]+\d+[ \t]+PRRT_[A-Za-z0-9_-]+(?:[ \t]+2>&1)?",
    re.IGNORECASE,
)
# Escapes of a name in a direct mutation: \xHH, \uHHHH, octal and PowerShell `u{H}.
ESCAPE = re.compile(
    r"\\x([0-9a-f]{2})|\\u([0-9a-f]{4})|[\\`]u\{([0-9a-f]{1,6})\}|\\([0-7]{3})"
)
SCRIPTS = Path("scripts")
# The checkout this hook belongs to: the call runs from it, or `scripts/` names another.
ROOT = Path(__file__).resolve().parents[2]
RUN = "resolve_review_thread"
# Python imports these at start from the script's directory as well.
STARTUP = frozenset({"sitecustomize", "usercustomize"})
# The direct resolve mutation through an API call; the name is read without
# quotes, escapes or case, and `resolvereview` also catches a name finished by a
# variable (#3759).
GRAPHQL = re.compile(
    r"\b(?:api|curl|wget|irm|iwr|invoke-restmethod|invoke-webrequest)\b.*graphql",
    re.DOTALL,
)
RESOLVE_MUTATION = "resolvereview"
# What a shell or PowerShell drops or joins inside a word: quotes, `$`, backticks,
# carets, backslashes and `+`.
QUOTING = re.compile(r"[\"'‘’“”`$^\\+]")


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
    except OSError, SyntaxError, ValueError:
        return []
    names = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    ] + [
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and not node.level and node.module
    ]
    tops = {full.split(".")[0] for full in names}
    return [
        top
        for top in tops
        if (SCRIPTS / f"{top}.py").exists() or (SCRIPTS / top).exists()
    ]


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
    return name in {
        module.casefold() for module in (*STARTUP, *sys.stdlib_module_names)
    }


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


def exact_call(command: str) -> bool | None:
    """True for the one call that may run (a few harmless wrappers and variables
    before it, nothing after it but a stream copy), False for any other command
    that holds the start `python scripts/resolve_review_thread.py`, else None."""
    if EXACT.fullmatch(command.strip()):
        return True
    return False if INVOCATION.search(command) else None


def resolves_directly(command: str) -> bool:
    """A GraphQL API call that names the resolve mutation."""

    def decode(match: re.Match[str]) -> str:
        code, base = next((g, b) for g, b in zip(match.groups(), (16, 16, 16, 8)) if g)
        return chr(int(code, base))

    text = unicodedata.normalize("NFKC", command).casefold()
    text = QUOTING.sub("", ESCAPE.sub(decode, text))
    # Blanks stay for the API call's words; the name is read with pieces joined.
    return bool(GRAPHQL.search(text)) and RESOLVE_MUTATION in "".join(text.split())


def main() -> int:
    try:
        return decide()
    except Exception as error:  # noqa: BLE001 - any failure blocks (#3367)
        print(f"Blocked: the hook failed ({type(error).__name__}).", file=sys.stderr)
        return 2


def decide() -> int:
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
    if resolves_directly(command):
        print(
            "Blocked: a review thread is resolved only through "
            "`python scripts/resolve_review_thread.py <pr> <PRRT_id>`, never by a "
            "direct GraphQL resolve mutation (#2629, #3759).",
            file=sys.stderr,
        )
        return 2
    call = exact_call(command)
    # Anything around the script in the same command could change what the check saw,
    # and so could another working directory: its `scripts/` is not the one checked.
    cwd = payload.get("cwd")
    at_root = isinstance(cwd, str) and Path(cwd).resolve() == ROOT
    if call is False or (call and not (at_root and unchanged())):
        print(
            "Blocked: the review-thread resolve script runs only as "
            "`python scripts/resolve_review_thread.py <pr> <PRRT_id>`, alone, from the "
            "repository root of a checkout whose scripts/ matches origin/develop (#2629).",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

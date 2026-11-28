"""Claude Code PreToolUse hook: a review thread is resolved only by its script.

`.claude/settings.json` allows `python scripts/resolve_review_thread.py` without
a prompt (athlete decision #2629). The script checks its own preconditions
(its pull request, its author, an unresolved thread, a reply naming a fix commit)
and refuses otherwise, so this hook asks for two things only (#4289):

- a command that starts `python scripts/resolve_review_thread.py` (the spelling
  the allow rule matches, after the wrappers it strips) runs only while the script
  and the `scripts/` modules it imports match origin/develop byte for byte and no
  entry in `scripts/` can shadow one of them (#3072); a changed script has no
  checks worth trusting. It runs first in the command: anything before it could
  change what the check saw, so a later start blocks;
- a direct GraphQL resolve mutation through an API call blocks (#3759).

No command is read for the script's name otherwise: read-only commands that
mention it, globs, short names and other spellings pass, which three earlier
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

# The spelling the allow rule matches: a simple command that starts with
# `python scripts/<name>`, after the wrappers and environment assignments the rule may
# strip, whatever follows it. Read-only commands that only mention the path do not start
# the script. Line breaks of every shell separate commands.
SEPARATORS = re.compile(r"[;&|(`\n\r\x85  ]")
WRAPPER = (
    r"(?:[\w.-]+=\S*|time|nohup|nice|timeout|stdbuf|env|command|exec|sudo"
    r"|then|do|else|if|while|until|!|\{)\s+(?:-\S*\s+|\d+[smhd]?\s+)*"
)
INVOCATION = re.compile(
    rf"(?P<pre>(?:{WRAPPER}){{0,4}})(?:py|python[\d.]*)(?:\.exe)?\s+(?:\./)?scripts[/\\]"
    r"resolve_review_thread\.py(?![\w.-])",
    re.IGNORECASE,
)
# The only environment assignments that cannot change what the script imports or runs.
HARMLESS_ENV = frozenset(
    {"PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED", "PYTHONUTF8", "PYTHONIOENCODING"}
)
# `2>&1` copies a stream; its `&` starts no job. What may follow the script in order.
STREAM_COPY = re.compile(r"(?<=[\s\d])>&(?=[0-9-])")
SEQUENCE = ("&&", "||", ";", "\n", "\r", "\x85", " ", " ")
# Escapes of a name in a direct mutation: \xHH, \uHHHH, octal and PowerShell `u{H}.
ESCAPE = re.compile(
    r"\\x([0-9a-f]{2})|\\u([0-9a-f]{4})|[\\`]u\{([0-9a-f]{1,6})\}|\\([0-7]{3})"
)
SCRIPTS = Path("scripts")
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


def invocation(command: str) -> str | None:
    """ "first" when the command starts with the allow rule's spelling, "later" when
    another simple command of it does, else None."""
    text = STREAM_COPY.sub(">", command)
    first, *later = SEPARATORS.split(text)
    starts = INVOCATION.match(first.lstrip())
    # Another variable (PYTHONPATH, PATH, ...) could load code from outside scripts/.
    names = (
        [word.split("=")[0] for word in starts["pre"].split() if "=" in word]
        if starts
        else []
    )
    # What follows the script's words must run after it: a substitution, a pipe or a
    # background job in the same command runs before or beside it.
    rest = text[len(first) :]
    after = not rest or rest.startswith(SEQUENCE)
    if starts and after and all(name.upper() in HARMLESS_ENV for name in names):
        return "first"
    return (
        "later" if starts or any(INVOCATION.match(p.lstrip()) for p in later) else None
    )


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
    where = invocation(command)
    # Anything before the script in the same command could change what the check saw.
    if where == "later" or (where == "first" and not unchanged()):
        print(
            "Blocked: the review-thread resolve script runs alone, first in the "
            "command, from a checkout whose scripts/ matches origin/develop (#2629).",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
import posixpath
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

EXACT = re.compile(r"python scripts/resolve_review_thread\.py \d+ PRRT_[A-Za-z0-9_-]+")
NAME = "resolve_review_thread"
TARGETS = (NAME, f"{NAME}.py")
# ASCII and typographic quotes, which PowerShell also reads as quotes.
QUOTES = "[\"'‘’“”]"
# Shell glob characters, brace expansion and extglob groups.
GLOB = re.compile(r"[*?\[]")
WIDE = re.compile(r"[@+!?*]\([^()]*\)|\{[^{}\s]*\}")
# DOS wildcards inside a word: `<` reads as `*`, `>` as `?`.
DOS_STAR = re.compile(r"(?<=[\w.*?])<(?=[\w.*?])")
DOS_ONE = re.compile(r"(?<=[\w.*?])>(?=[\w.*?])")
VARIABLE = re.compile(r"\$\{?[a-z_][a-z0-9_]*\}?|%[a-z_][a-z0-9_]*%")
# NTFS stream names that address the same folder or file.
STREAM = re.compile(r"::?\$(?:index_allocation|i30|data)")
# Separators between words, also of PowerShell arrays, script blocks and
# `-Name:value` parameters.
WORDS = re.compile(r"[\s;&|<>=(){},:]+")
# \xHH, \UHHHHHHHH, \uHHHH, \u{H}, `u{H} (PowerShell), \NNN and \0NNN
# (octal), \N{NAME} (Python), after casefolding.
ESCAPE = re.compile(
    r"\\x([0-9a-f]{2})|\\u([0-9a-f]{8})|\\u([0-9a-f]{4})|[\\`]u\{([0-9a-f]{1,6})\}"
    r"|\\0?([0-7]{1,3})|\\n\{([a-z0-9 -]+)\}"
)
# A Windows 8.3 short name, also a hashed one: a tilde and a digit.
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


def unescaped(text: str) -> str:
    """Text with shell, Python and PowerShell character escapes decoded."""

    def decode(match: re.Match[str]) -> str:
        hex_code = next((group for group in match.groups()[:4] if group), None)
        try:
            if hex_code:
                return chr(int(hex_code, 16))
            if match[5]:
                return chr(int(match[5], 8))
            return unicodedata.lookup(match[6])
        except (ValueError, KeyError, OverflowError):
            # Undecodable text stays as it is, so it can still match.
            return match[0]

    return ESCAPE.sub(decode, text)


def unquoted(text: str, backslash: str = "") -> str:
    """Text without quoting and escape characters: adjacent string literals
    joined, line continuations, quotes, `$`, backticks and carets removed, and
    each other backslash replaced by `backslash`."""
    text = re.sub(f"{QUOTES}\\s*\\+?\\s*{QUOTES}", "", text)
    text = re.sub(f"\\\\\\n|\\$|`|\\^|{QUOTES}", "", text)
    return text.replace("\\", backslash)


def wildened(text: str) -> str:
    """Extglob groups and brace expansions (a list or a range; `{x}` stays
    literal in bash), innermost first, widened to `*`."""
    def wide(match: re.Match[str]) -> str:
        group = match[0]
        if group.startswith("{") and "," not in group and ".." not in group:
            return group
        # A group holding a path keeps a folder of its own.
        return "*/*" if re.search(r"[/\\]", group) else "*"

    while (wider := WIDE.sub(wide, text)) != text:
        text = wider
    return text


def names_script(command: str) -> bool:
    """Whether the command names the script in any spelling the shell, Python
    or a case-insensitive file system resolves to it (#3367). The name's
    letters in order, whatever stands between them, count as the name."""
    raw = unicodedata.normalize("NFKC", command).casefold()
    text = unescaped(raw)
    if any(NAME in unquoted(form) for form in (raw, text)):
        return True
    # Negated and POSIX character classes as fnmatch reads them.
    text = re.sub(r"\[:[a-z]+:\]", "a-z0-9_.", text.replace("[^", "[!"))
    text = DOS_ONE.sub("?", DOS_STAR.sub("*", STREAM.sub("", wildened(text))))
    # A variable can hold any folder or name.
    text = VARIABLE.sub("*", text)
    return any(
        word_reaches_script(word)
        for variant in (unquoted(text), unquoted(text, "/"))
        for word in WORDS.split(variant)
    )


def word_reaches_script(word: str) -> bool:
    """A glob or 8.3 short name that can resolve to the script's file."""
    *folders, last = posixpath.normpath(word).split("/")
    last = last.rstrip(". ")
    short = SHORT_NAME.match(last)
    # Windows also matches a wildcard against the short name.
    if (short and NAME.startswith(short[1][:2])) or ("~" in last and GLOB.search(last)):
        return True
    folder = folders[-1].rstrip(". ") if folders else None
    # A file in a plainly named other folder is never the script; a tilde
    # folder (`~+` is the current one) is not plain.
    plain = folder is not None and not folder.startswith("~") and not GLOB.search(folder)
    if plain and folder not in {"", ".", "..", "scripts"}:
        return False
    # Without a folder, a glob must keep part of the name: `*` and `*.py`
    # pass, `r*` does not.
    if folder is None and not re.sub(r"[*?]", "", last).removesuffix(".py"):
        return False
    return bool(GLOB.search(last)) and any(
        fnmatch.fnmatchcase(target, last) for target in TARGETS
    )


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

"""Claude Code PreToolUse hook: the review-thread resolve script runs only as itself.

`.claude/settings.json` allows `python scripts/resolve_review_thread.py` without
a prompt (athlete decision #2629). That rule matches command text, so this hook
blocks any shell command that names the script unless the whole command is
exactly `python scripts/resolve_review_thread.py <pr> <PRRT_id>`, the script and
any `scripts/` module it imports match origin/develop byte for byte, and no
entry in `scripts/` (tracked, ignored or untracked) can shadow a module the run
imports (#3072). Other scripts, the hooks and non-Python files may differ.
The name matches in any case, through quotes, escapes, globs and short names,
and input without a command string blocks (#3367). Exit code 2 blocks the call;
the message names the word that matched, as the hook read it (#3459).
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
# Unquoted characters that end a bash word, so no bracket expression spans
# them (#3507).
WORD_END = frozenset(" \t\n;&|<>()")
# The longest bracket expression read; a longer open one blocks, so padding
# can neither hide a class nor slow the hook down (#3507).
MAX_BRACKET = 256
# The characters all readings without quotes may read together (readings x
# command length); a command needing more blocks, so it stays fast (#3507).
MAX_WIDE_WORK = 8_000_000
# How far a swallowed class's own reading looks either side for its word.
MAX_WORD = 4096


class OverlongBracket(ValueError):
    """A bracket expression that stays open past MAX_BRACKET characters, or a
    command whose readings would read more than MAX_WIDE_WORK characters."""


# ripgrep's options that run a program on each file it searches, and its
# config file that can hold them; blocked wherever they appear, since `rg`
# itself can hide behind a variable, a glob, xargs or a path (#3509).
RG_RUNS = re.compile(r"--pre(?![a-z0-9_])|hostname-bin|ripgrep_config_path")
# Shell expansions an option may hide in, read as nothing; a brace list reads
# as its first item (#3509).
EXPANSION = re.compile(r"\$(?:\{[^}]*\}|\([^()]*\)|[a-z_][a-z0-9_]*|[@*#?$!0-9-])")
BRACE_LIST = re.compile(r"\{([^{},]*),[^{}]*\}")


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
        except ValueError, KeyError, OverflowError:
            # Undecodable text stays as it is, so it can still match.
            return match[0]

    return ESCAPE.sub(decode, text)


def unquoted(text: str, backslash: str = "", blanks: bool = True) -> str:
    """Text without quoting and escape characters: adjacent string literals
    joined (across blanks only with ``blanks``, as Python joins them; bash
    keeps such words apart, #3509), line continuations, quotes, `$`, backticks
    and carets removed, and each other backslash replaced by `backslash`."""
    joined = f"{QUOTES}\\s*\\+?\\s*{QUOTES}" if blanks else f"{QUOTES}\\+?{QUOTES}"
    text = re.sub(joined, "", text)
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


def matching_word(command: str) -> str | None:
    """The word by which the command names the script in any spelling the
    shell, Python or a case-insensitive file system resolves to it (#3367),
    as the hook read it (variables as `*`), or None. The name's letters in
    order, whatever stands between them, count as the name."""
    raw = unicodedata.normalize("NFKC", command).casefold()
    text = unescaped(raw)
    if any(NAME in unquoted(form) for form in (raw, text)):
        return NAME
    # ripgrep's `--pre` and `--hostname-bin` run a program on its files (#3509).
    bare = BRACE_LIST.sub(r"\1", EXPANSION.sub("", text))
    forms = (text, bare, unquoted(bare), unquoted(bare, blanks=False))
    if any(RG_RUNS.search(form) for form in forms):
        return "rg --pre"
    # Also every bash bracket expression as `?`, read with quotes, and without
    # them from the first `[` and from each `[` an earlier bracket swallowed, so
    # no quote, comment, substitution or stray `[` before a class can hide it:
    # added readings beside the unchanged one, so they only ever block more
    # (#3507).
    try:
        readings = (text, bracket_wildcards(text), *wide_readings(text))
    except OverlongBracket:
        return "an overlong bracket expression"
    variants = []
    for reading in readings:
        # Negated and POSIX character classes as fnmatch reads them.
        reading = re.sub(r"\[:[a-z]+:\]", "a-z0-9_.", reading.replace("[^", "[!"))
        reading = DOS_ONE.sub("?", DOS_STAR.sub("*", STREAM.sub("", wildened(reading))))
        # A variable can hold any folder or name.
        reading = VARIABLE.sub("*", reading)
        # Quoted words a blank apart also stay apart, as bash reads them (#3509).
        variants += [
            unquoted(reading),
            unquoted(reading, "/"),
            unquoted(reading, blanks=False),
            unquoted(reading, "/", blanks=False),
        ]
    return next(
        (
            word
            for variant in variants
            for word in WORDS.split(variant)
            if word_reaches_script(word)
        ),
        None,
    )


def bracket_end(text: str, start: int) -> int | None:
    """The index after the bash bracket expression opening at ``start``, read
    as bash reads it: an optional `!` or `^`, a leading `]` as a member, an
    escaped or quoted character as a member, and `[.x.]`, `[=x=]` or `[:x:]`
    classes inside (an unclosed one is a plain member). None when an unquoted
    word end comes first, so a stray `[` stays a literal; OverlongBracket when
    it stays open past MAX_BRACKET characters."""
    limit = min(len(text), start + MAX_BRACKET)
    first = start + 1
    first += text[first : first + 1] in ("!", "^")
    first += text[first : first + 1] == "]"
    index: int | None = first
    while index is not None and index < limit and text[index] not in WORD_END:
        if text[index] == "]":
            return index + 1
        index = _after_member(text, index, limit)
    if index is not None and index >= limit and limit < len(text):
        raise OverlongBracket
    return None


def _after_member(text: str, index: int, limit: int) -> int | None:
    """The index after one bracket member, searched up to ``limit``: an escaped
    character, a quoted run (None when it never closes), a class `[.x.]`,
    `[=x=]` or `[:x:]`, or one character."""
    char, after = text[index], text[index + 1 : index + 2]
    if char in "'\"":
        close = text.find(char, index + 1, limit)
        if close < 0 and limit < len(text):
            raise OverlongBracket
        return close + 1 if close >= 0 else None
    if char == "[" and after and after in ".=:":
        found = text.find(after + "]", index + 2, limit)
        if found >= 0 and not WORD_END.intersection(text[index:found]):
            return found + 2
    return index + 1 + (char == "\\")


def bracket_wildcards(text: str) -> str:
    """Each bash bracket expression outside quotes read as `?`: it matches one
    character, whatever negation, POSIX class, collating symbol or equivalence
    class it holds, so none of them hides the name; a quoted `[` is a literal
    to bash (#3507)."""
    # The open quote: `'`, `"`, or `$'` (ANSI-C, closed by `'`, with escapes).
    parts, last, index, quote = [], 0, 0, ""
    while index < len(text):
        char = text[index]
        end = None
        if quote:
            quote = "" if char == quote[-1] else quote
        elif text.startswith("$'", index):
            quote, index = "$'", index + 1
        elif char in "'\"":
            quote = char
        elif char == "[":
            end = bracket_end(text, index)
        if end:
            parts.append(text[last:index] + "?")
            last = index = end
            continue
        index += 2 if char == "\\" and quote != "'" else 1
    return "".join([*parts, text[last:]])


def wide_end(text: str, start: int) -> int | None:
    """The index after the bracket expression at ``start`` read without quotes
    or word ends: an optional `!` or `^`, a leading `]`, and `[.x.]`, `[=x=]`
    or `[:x:]` classes inside, up to MAX_BRACKET characters; a longer one
    closes at the next `]`. None when it never closes."""
    limit = min(len(text), start + MAX_BRACKET)
    index = start + 1
    index += text[index : index + 1] in ("!", "^")
    index += text[index : index + 1] == "]"
    while index < limit:
        char, after = text[index], text[index + 1 : index + 2]
        if char == "]":
            return index + 1
        close = text.find(after + "]", index + 2, limit) if after else -1
        if char == "[" and after in (".", "=", ":") and close >= 0:
            index = close + 2
            continue
        index += 1 + (char == "\\")
    close = text.find("]", limit)
    return close + 1 if close >= 0 else None


def wildcards_from(text: str, start: int) -> tuple[str, list[int]]:
    """Every bracket expression from ``start`` on read as `?`, without reading
    quotes, comments or substitutions, and the `[` it swallowed: a reading
    from one of those may see a class this one hid (#3507)."""
    parts, last, index = [text[:start]], start, start
    swallowed: list[int] = []
    final_close = text.rfind("]")
    # A `[` after the last `]` can never close, so the scan stops there.
    while 0 <= (found := text.find("[", index)) < final_close:
        end = wide_end(text, found)
        if end:
            parts.append(text[last:found] + "?")
            swallowed += [i for i in range(found + 1, end) if text[i] == "["]
            last = index = end
        else:
            index = found + 1
    return "".join([*parts, text[last:]]), swallowed


def wide_readings(text: str) -> list[str]:
    """The readings without quotes: the whole command from the first `[`, then
    the word around each `[` a reading swallowed, read from that `[` on; a
    later `[` a reading reaches adds nothing new. OverlongBracket past
    MAX_WIDE_WORK characters read."""
    first = text.find("[")
    if first < 0:
        return []
    reading, pending = wildcards_from(text, first)
    readings, seen, work = [reading], {first, *pending}, len(text)
    while pending:
        start = pending.pop()
        low, high = _word_around(text, start)
        work += high - low
        if work > MAX_WIDE_WORK:
            raise OverlongBracket
        reading, swallowed = wildcards_from(text[low:high], start - low)
        readings.append(reading)
        fresh = [low + i for i in swallowed if low + i not in seen]
        seen.update(fresh)
        pending += fresh
    return readings


def _word_around(text: str, start: int) -> tuple[int, int]:
    """The span from the last blank before ``start`` to the first blank after
    the bracket expression at it, at most MAX_WORD characters each way: the
    word a class there can belong to, quoted blanks inside it included."""
    end = wide_end(text, start) or start + 1
    low = max(text.rfind(blank, max(0, start - MAX_WORD), start) for blank in " \t\n")
    highs = [text.find(blank, end, end + MAX_WORD) for blank in " \t\n"]
    high = min(
        [found for found in highs if found >= 0] or [min(len(text), end + MAX_WORD)]
    )
    return max(low + 1, start - MAX_WORD, 0), high


def word_reaches_script(word: str) -> bool:
    """A glob or 8.3 short name that can resolve to the script's file."""
    *folders, last = posixpath.normpath(word).split("/")
    last = last.rstrip(". ")
    short = SHORT_NAME.match(last)
    # Windows also matches a wildcard against the short name.
    if (short and NAME.startswith(short[1][:2])) or ("~" in last and GLOB.search(last)):
        return True
    folder = folders[-1] if folders else None
    if folder not in {None, ".", ".."}:
        # Windows drops trailing dots and spaces of a folder name.
        folder = folder.rstrip(". ")
    # A file in a plainly named other folder is never the script; a tilde
    # folder (`~+` is the current one) is not plain.
    plain = (
        folder is not None and not folder.startswith("~") and not GLOB.search(folder)
    )
    if plain and folder not in {".", "..", "scripts"}:
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
    word = matching_word(command)
    if word is None:
        return 0
    if EXACT.fullmatch(command.strip()) and unchanged():
        return 0
    print(
        "Blocked: the review-thread resolve script runs only as "
        "`python scripts/resolve_review_thread.py <pr> <PRRT_id>`, alone, from a "
        "checkout whose scripts/ matches origin/develop (#2629)."
        + (
            ""
            if word == NAME
            else f" Matched word `{word}` (variables read as `*`): a variable or"
            " glob path can resolve to the script; spell the path out (#3459)."
        ),
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())

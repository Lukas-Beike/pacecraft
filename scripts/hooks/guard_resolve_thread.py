"""Claude Code PreToolUse hook: the review-thread resolve script runs only as itself.

`.claude/settings.json` allows `python scripts/resolve_review_thread.py` without
a prompt (athlete decision #2629). That rule matches command text, so this hook
blocks any shell command that names the script unless the whole command is
exactly `python scripts/resolve_review_thread.py <pr> <PRRT_id>`, the script and
any `scripts/` module it imports match origin/develop byte for byte, and no
entry in `scripts/` (tracked, ignored or untracked) can shadow a module the run
imports (#3072). Other scripts, the hooks and non-Python files may differ.
The name matches in any case, through quotes, escapes, globs and short names,
and input without a command string blocks (#3367). Globs, variables and short
names are not read when every simple command starts with a program that runs
no argument (`git`, `ls`, `gh`, `python -m mypy`, ...; #3759), and a direct
GraphQL resolve mutation blocks. Exit code 2 blocks the call;
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
# A word of plain path characters and globs only (#3648).
PLAIN_WORD = re.compile(r"[a-z0-9_./*?-]+")
# One simple command: nothing a shell substitutes, pipes, chains, redirects,
# assigns or escapes, so each word is globbed as written (bypass review of
# #3648: `${w#q}`, `$(…|tr)`, `(…).substring` rewrite a verbatim word).
SIMPLE_COMMAND = re.compile(r"[^$`|<>(){}\[\]=;&\n\r\\%^!]*")
CODE_RUNNERS = re.compile(
    r"(?<![\w-])(?:-c|-command|-e|eval|exec|xargs|source)(?![\w-])"
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
# Shell expansions an option may hide in, read as nothing; a word with a brace
# list reads as all its expansions, as bash expands it (#3509).
EXPANSION = re.compile(r"\$(?:\{[^}]*\}|\([^()]*\)|[a-z_][a-z0-9_]*|[@*#?$!0-9-])")
RG_BLOCKED = "rg --pre"
# How much brace expansion may add to a command before it blocks (#3509).
MAX_BRACE_GROWTH = 1_000_000


def brace_expanded(text: str) -> str:
    """The text with each word that holds a brace list and a `-` or `_` (every
    ripgrep spelling RG_RUNS looks for has one) replaced by all its
    expansions, as bash expands them. OverlongBracket when that adds more than
    MAX_BRACE_GROWTH characters, so no large expansion is left unread (#3509)."""
    budget = [MAX_BRACE_GROWTH]
    return "".join(
        " ".join(_word_expansions(word, budget))
        if "{" in word and ("-" in word or "_" in word)
        else word
        for word in _brace_words(text)
    )


def _brace_words(text: str) -> list[str]:
    """The text, line continuations removed, cut at blanks outside brace pairs,
    so a quoted or escaped blank inside a brace list stays in its word (Gitar,
    #3509). Quotes count only inside an open brace, so an apostrophe elsewhere
    never joins lines; an unquoted line break always cuts."""
    text = text.replace("\\\n", "")
    words, start, depth, quote = [], 0, 0, ""
    for index, char in enumerate(text):
        if quote:
            quote = "" if char == quote else quote
        elif char in "'\"" and depth:
            quote = char
        elif char == "\n":
            depth = 0
        elif char == "{":
            depth += 1
        elif char == "}":
            depth = max(depth - 1, 0)
        if char.isspace() and not quote and (depth == 0 or char == "\n"):
            words += [text[start:index], char]
            start = index + 1
    return [*words, text[start:]]


def _word_expansions(word: str, budget: list[int]) -> list[str]:
    """Every expansion of one word, innermost brace list first."""
    pending, done = [word], []
    while pending:
        current = pending.pop()
        found = _brace_list(current)
        if found is None:
            done.append(current)
            continue
        low, high = found
        items = current[low + 1 : high].split(",")
        budget[0] -= len(current) * (len(items) - 1)
        if budget[0] < 0:
            raise OverlongBracket
        pending += [current[:low] + item + current[high + 1 :] for item in items]
    return done


def _brace_list(word: str) -> tuple[int, int] | None:
    """The innermost brace list (a `{}` pair holding a comma) in ``word``."""
    position = 0
    while (high := word.find("}", position)) >= 0:
        low = word.rfind("{", 0, high)
        if low >= 0 and "," in word[low + 1 : high]:
            return low, high
        position = high + 1
    return None


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
    # An escape can also decode to a capital letter (#3759 review).
    decoded = unescaped(unicodedata.normalize("NFKC", command)).casefold()
    if any(NAME in unquoted(form) for form in (raw, text, decoded)):
        return NAME
    # ripgrep's `--pre` and `--hostname-bin` run a program on its files (#3509).
    try:
        bare = brace_expanded(EXPANSION.sub("", text))
    except OverlongBracket:
        return "an overlong brace expansion"
    forms = (text, bare, unquoted(bare), unquoted(bare, blanks=False))
    if any(RG_RUNS.search(form) for form in forms):
        return RG_BLOCKED
    if any(
        GRAPHQL.search(form) and RESOLVE_MUTATION in form
        for form in (forms[2], unquoted(decoded))
    ):
        return GRAPHQL_BLOCKED
    # Globs, variables and short names in the arguments of programs that never
    # run an argument cannot reach the script (#3759); any other command is
    # read word by word as before.
    # Only for ASCII text: NFKC makes fullwidth quotes real ones and PowerShell
    # closes on further quote marks, so the words could differ (#3982 review).
    if command.isascii() and only_inert_programs(raw):
        return None
    return reaching_word(text, raw)


# The direct resolve mutation through an API call, outside the script (#3759);
# `resolvereview` also catches a name finished by a variable.
GRAPHQL = re.compile(
    r"\b(?:api|curl|wget|irm|iwr|invoke-restmethod|invoke-webrequest)\b.*graphql",
    re.DOTALL,
)
RESOLVE_MUTATION = "resolvereview"
GRAPHQL_BLOCKED = "the resolve mutation"
# Programs that never run a file named in their arguments (#3759). A command
# whose every simple command starts with one of them, or with `python -m` and
# an inert module, skips the glob, variable and short-name readings.
INERT_PROGRAMS = frozenset(
    {"ls", "dir", "cat", "type", "head", "tail", "wc", "grep", "rg", "echo"}
    | {"printf", "cp", "copy", "mv", "move", "ruff", "jq", "uniq", "cut", "diff"}
    | {"mkdir", "touch", "cd", "pwd", "true", "false", "get-content", "gc"}
    | {"select-string", "sls", "get-childitem", "gci", "test-path"}
    | {"write-output", "write-host"}
)
# git and gh only with a subcommand that runs no program of its own.
INERT_SUBCOMMANDS = {
    "git": frozenset(
        {"status", "diff", "log", "show", "ls-files", "ls-tree", "rev-parse"}
        | {"rev-list", "branch", "blame", "add", "cat-file", "merge-base"}
    ),
    "gh": frozenset(
        {"pr", "issue", "run", "api", "repo", "release", "label", "search"}
    ),
}
PYTHONS = frozenset({"python", "python3", "py"})
INERT_MODULES = frozenset({"mypy", "ruff", "compileall", "py_compile"})
# Options by which one of those programs still runs a word, also abbreviated
# or with an attached value, read with and without quotes (#3759 reviews).
RUNS_ARGUMENTS = re.compile(
    r"(?<![\w-])(?:-[cxo]\S*"
    r"|--(?:exe|ext|upl|rec|open|hostname|shell|pre|python|config)\S*"
    r"|alias|extension|config|bisect|submodule|foreach)(?![\w-])|[{%]"
)
# Text whose words the shells may split differently than this reading:
# escapes, comments, history and alias marks, ANSI-C, substitutions,
# typographic quotes PowerShell closes on, and heredocs, whose body a lone
# quote would pair across (#3982 advisor).
UNREADABLE = re.compile(r"\\|#|!|`|\r|<<|\$[('\"]|[<>]\(|[‘’“”]")
# Of those, read on the raw command even inside single quotes (#3982).
RAW_UNREADABLE = re.compile(r"\r|<<|[‘’“”]")
QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
# `sed` is inert in one shape only (#3982): print lines N or N to M; it is
# replaced by SED_LINES before the words are read, and every later word of
# that command must be a plain word that is no option (GNU sed permutes them).
SED_PRINT = re.compile(
    r"(?:^|(?<=[\s;&|(]))sed[ \t]+-n[ \t]+'\d+(?:,\d+)?p'(?=[\s;&|)]|$)"
)
SED_LINES = "\x00sed"
# A literal file name only: a glob could expand to an option (#3982 review).
SED_FILE = re.compile(r"[a-z0-9_./-]+")
# Shell tokens: a quoted run, a separator, blanks, plain characters; a lone
# quote never closes.
# Tokens after each quoted run became `@`, which no program name holds, so a
# word with a glued quoted part (`ls'x'` reads `ls@`) is never a bare program
# (Gitar on #3781) and a lone quote stays.
TOKENS = re.compile(r"[^\s'\";&|()<>]+|[;&|()<>\n]|[^\S\n]+|['\"]")
# A bare program name: no path, variable, glob, short name or quote.
PROGRAM = re.compile(r"[a-z][a-z0-9_-]*")


def inert(words: list[str]) -> bool:
    """One simple command starts with an inert program."""
    name, after = words[0], words[1:2]
    if name == SED_LINES:
        return all(
            SED_FILE.fullmatch(word) and not word.startswith("-") for word in words[1:]
        )
    if not PROGRAM.fullmatch(name):
        return False
    if name in INERT_SUBCOMMANDS:
        return bool(after) and after[0] in INERT_SUBCOMMANDS[name]
    if name in PYTHONS:
        return after == ["-m"] and words[2:3] != [] and words[2] in INERT_MODULES
    return name in INERT_PROGRAMS


def simple_commands(text: str) -> list[list[str]] | None:
    """The simple commands of ``text`` as token lists without redirections;
    None when a quote stays open."""
    segments: list[list[str]] = [[]]
    redirect = False
    for token in (match[0] for match in TOKENS.finditer(QUOTED.sub("@", text))):
        if token in ("'", '"'):
            return None
        if token in ("<", ">") or (redirect and token == "&"):
            # A redirection's target is a file, not a command (`2>&1` too).
            redirect = True
        elif token in ("\n", ";", "&", "|", "(", ")"):
            segments.append([])
            redirect = False
        elif not token.isspace():
            segments[-1] += [] if redirect else [token]
            redirect = False
    return [words for words in segments if words]


def single_quoted_blank(match: re.Match[str]) -> str:
    """A single-quoted run that is a whole word, with its inside blanked and its
    quotes kept (so `$'` is still seen); both shells read a single-quoted inside
    literally (#3982). A double-quoted run, or one glued to other text, stays."""
    run, text = match[0], match.string
    before = text[match.start() - 1] if match.start() else " "
    after = text[match.end()] if match.end() < len(text) else " "
    whole = before in WORD_END and after in WORD_END
    return f"'{'x' * (len(run) - 2)}'" if run.startswith("'") and whole else run


def only_inert_programs(raw: str) -> bool:
    """Every simple command of ``raw`` (casefolded, escapes not decoded)
    starts with an inert program."""
    if (
        SED_LINES in raw
        or RAW_UNREADABLE.search(raw)
        or UNREADABLE.search(QUOTED.sub(single_quoted_blank, raw))
        or any(
            RUNS_ARGUMENTS.search(form)
            for form in (QUOTED.sub(" ", raw), unquoted(raw))
        )
    ):
        return False
    text = SED_PRINT.sub(SED_LINES, raw)
    commands = simple_commands(text)
    if commands is None:
        return False
    # Each sed shape must start a command of its own: after a redirection it is
    # the target, and the shell runs `-n` instead (#4091 gate).
    starts = sum(words[0] == SED_LINES for words in commands)
    return starts == text.count(SED_LINES) and all(inert(words) for words in commands)


def reaching_word(text: str, raw: str) -> str | None:
    """The word of ``text`` that reaches the script in any of its readings."""
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
            if word_reaches_script(word, raw)
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


def word_reaches_script(word: str, raw: str = "") -> bool:
    """A glob or 8.3 short name that can resolve to the script's file;
    ``raw`` is the command as written, casefolded (#3648)."""
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
    # A file in a plainly named other folder is never the script; a folder with
    # a tilde (`~+` is the current one, `script~1` the 8.3 short name of
    # scripts/) is not plain (#3648).
    plain = folder is not None and "~" not in folder and not GLOB.search(folder)
    if plain and folder not in {".", "..", "scripts"}:
        return False
    # A folder glob whose literal start is not a start of `scripts` matches
    # neither it nor its 8.3 short names, which keep its first letters. Windows
    # drops trailing dots and spaces, so `scripts.*` still matches `scripts`,
    # and a start of dots alone may match `.` or `..` (#3648).
    # Only for a word of plain path characters that stands in the command as
    # written: a reading's widened group (`*(…)` read as `*/*`), `$`, `%`,
    # quotes or code around it may hide the real folder.
    head = re.split(r"[*?\[]", folder or "", maxsplit=1)[0].rstrip(". ")
    if (
        folder is not None
        and GLOB.search(folder)
        and "~" not in folder
        and PLAIN_WORD.fullmatch(word)
        and SIMPLE_COMMAND.fullmatch(raw)
        and not CODE_RUNNERS.search(raw)
        and word in WORDS.split(raw)
        and not "scripts".startswith(head)
    ):
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
    if word == RG_BLOCKED:
        print(
            "Blocked: ripgrep's --pre, --hostname-bin and RIPGREP_CONFIG_PATH run "
            "a program on the files it searches, which can run the review-thread "
            "resolve script (#3509); search without them.",
            file=sys.stderr,
        )
        return 2
    if word == GRAPHQL_BLOCKED:
        print(
            "Blocked: a review thread is resolved only through "
            "`python scripts/resolve_review_thread.py <pr> <PRRT_id>`, never by a "
            "direct GraphQL resolve mutation (#2629, #3759).",
            file=sys.stderr,
        )
        return 2
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

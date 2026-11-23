"""Claude Code PreToolUse hook: block shell commands that touch private runtime paths.

File tools are covered by deny rules in .claude/settings.json; this hook covers
Bash/PowerShell, where deny rules cannot see file arguments. It is a simple
safety net that matches clear path forms in the command text, not a sandbox:
it cannot tell a path from a search term, so it avoids bare words. Exit code 2
blocks the call and returns the message to the agent.

Importing or running the composition root reads private paths indirectly:
``server.py`` loads ``<root>/.env`` and ``<DATA_DIR>/.env`` at import (#3076).
A Python command that imports or runs ``server`` is therefore blocked unless it
sets ``DATA_DIR`` to a temporary directory and neither the working directory
(the checkout root) nor that directory holds a ``.env``.
"""

import json
import re
import sys
import tempfile
from pathlib import Path

START = r"(?:^|[\s\"'=:])"  # start of a shell word or assignment value
END = r"(?=$|[\s\"'/\\;|&)])"  # end of a path segment

PRIVATE = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"(?:^|[\s\"'=:/\\*])\.env(?:\.[\w-]+)*(?![\w.-])",  # .env, *.env, .env.local
        START + r"(?:\.{1,2}[/\\]|/)data" + END,  # ./data, ../data, /data
        START + r"data[/\\]",  # data/... relative to the repo
        r"[/\\](?:ai-coach|pacecraft)[/\\]data" + END,  # primary checkout data dir
        r"[/\\]garmin_tokens|garmin_tokens[/\\]",  # Garmin token store as a path
    )
)
TEMPLATE = re.compile(r"\.env\.example(?![\w.-])", re.IGNORECASE)
# T3's secret folder holds APP_PASSWORD, the login and database key (#3758). Its
# path may only be the --password-file value of a prod script, which reads the
# file in its own process; every other mention is blocked.
# Matched after quotes are dropped and `/./` collapsed: the folder, any word
# starting with s below `userdata` (globs, braces), a `..` path, a short name,
# and the `.t3` or `.t3/userdata` folder itself or a glob over it (a search for
# the bare word passes).
SECRETS = re.compile(
    r"userdata[/\\]+s[\w*?\[{~]|userdata[/\\]\S*\.\.|userda~\d"
    r"|[/\\]userdata[/\\]*(?=$|[\s;|&),*?\[{])"
    r"|(?:^|[\s/\\~=:])\.t[3*?\[][\]*?]*(?:[/\\]+userdata)?[/\\]*(?=$|[\s;|&),*?\[{])",
    re.IGNORECASE,
)
QUOTES = re.compile(r"[\"'`]")
DOT_SEGMENT = re.compile(r"[/\\]\.(?=[/\\])")
CONTINUATION = re.compile(r"[\\`]\r?\n")
SEGMENTS = re.compile(r"&&|\|\||[;|&\n]")
# The prod scripts and the node scripts, as two patterns each below Sonar's complexity limit.
SECRET_RUN = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        (
            r"py(?:thon\d*(?:\.\d+)*)?(?:\.exe)?(?:\s+-[\w.]+)*\s+"
            r"(?:\.[/\\])?scripts[/\\]prod_(?:diagnostics|guards|logs)\.py(?=\s)"
        ),
        (
            r"node(?:\.exe)?\s+(?:\.[/\\])?scripts[/\\]"
            r"(?:ui_walk|ui_explore|post_release_actions)\.mjs(?=\s)"
        ),
    )
)
# A literal value only: no expansion, substitution or redirection inside it.
PASSWORD_FILE = re.compile(
    r"\s--password-file(?:=|\s+)(?:\"[^\"$`]*\"|'[^']*'|[^\s\"'$`()<>]+)(?=\s|$)"
)


# What may come before such a run in the same command (#3940): nothing that could
# change the directory, the interpreter's options, a function or alias named like
# it, or the script itself. An allowlist: a status read, or one literal file read
# into a pipe; everything else before the run blocks it.
SAFE_BEFORE = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (r"git\s+status(?:\s+(?:-s|--short))?", r"(?:get-content|cat|type)\s+[\w./\\:-]+")
)
# What a pipe may feed the run's output into (#4075 review): a segment joined by
# `|` runs alongside the run, so only a reader without a redirection,
# substitution or expansion; a single `&` (background) after the run blocks it.
SAFE_AFTER = re.compile(
    r"(?:select-object|select|head|tail|sort|out-null|out-host|more|grep|findstr"
    r"|select-string|sls)(?:\s+[\w\s.,:=/\\\"'*-]*)?",
    re.IGNORECASE,
)


# An escaped separator (`\;` in Bash, `` `; `` in PowerShell) is part of a word,
# not a split: the segments seen here would not be the shell's (#3940 review).
ESCAPED_SEPARATOR = re.compile(r"[\\`][;|&]")
# `2>&1` copies a stream; its `&` separates nothing.
STREAM_COPY = re.compile(r">&(?=[0-9-])")


def names_secrets(text: str) -> bool:
    return bool(SECRETS.search(DOT_SEGMENT.sub("", QUOTES.sub("", text))))


def safe_before(segment: str) -> bool:
    """An empty segment or one of the SAFE_BEFORE shapes."""
    return not segment or any(shape.fullmatch(segment) for shape in SAFE_BEFORE)


def alongside_safe(segments: list[str], separators: list[str], index: int) -> bool:
    """Whether every segment that runs alongside segment ``index`` (joined after
    it by `|` or `&`) is a SAFE_AFTER reader; a sequential separator ends them."""
    for after, separator in zip(segments[index + 1 :], separators[index:]):
        if separator not in ("|", "&"):
            return True
        if separator == "&" or not SAFE_AFTER.fullmatch(after.strip()):
            return False
    return True


def secret_mention_allowed(command: str) -> bool:
    """True when every secret-folder path is a prod script's --password-file value,
    only SAFE_BEFORE segments come before that script's run and only SAFE_AFTER
    readers run alongside it."""
    text = STREAM_COPY.sub(">", CONTINUATION.sub(" ", command))
    pieces = re.split(f"({SEGMENTS.pattern})", text)
    segments, separators = pieces[0::2], pieces[1::2]
    for index, segment in enumerate(segments):
        if not names_secrets(segment):
            continue
        if (
            ESCAPED_SEPARATOR.search(command)
            or not any(run.match(segment.strip()) for run in SECRET_RUN)
            or names_secrets(PASSWORD_FILE.sub(" ", segment))
            or not all(safe_before(item.strip()) for item in segments[:index])
            or not alongside_safe(segments, separators, index)
        ):
            return False
    return True


PYTHON_WORD = r"(?:^|[\s\"'&|;(])(?:py|python[\d.]*)(?:\.exe)?"
PYTHON = re.compile(PYTHON_WORD + r"(?=[\s\"'])", re.IGNORECASE)
SERVER = tuple(
    re.compile(pattern)
    for pattern in (
        r"\bimport\s+(?:[\w.]+\s*,\s*)*server\b(?!\.)",  # import server, import os, server
        r"\bfrom\s+server\s+import\b",
        # server.py as the script run, after options only; compileall only compiles it.
        PYTHON_WORD + r"(?:\s+-[A-Za-z]+)*\s+[\"']?[^\s\"']*server\.py(?![\w.-])",
        r"run_path\(\s*[\"'][^\"']*server\.py",
        r"(?:^|\s)-m\s+server\b(?!\.)",
        r"(?:run_module|import_module|__import__)\(\s*[\"']server[\"']",
    )
)
# DATA_DIR bound to the one Python process: a prefix of its own simple command,
# or an earlier `export` (Bash) or `$env:` assignment (PowerShell).
VALUE = r"[\"']?([^\s\"';&|]+)[\"']?"
START_OF_COMMAND = r"(?:^|[;&|(\n])\s*"
DATA_DIR_PREFIX = re.compile(
    START_OF_COMMAND + r"(?:[A-Za-z_]\w*=\S*\s+)*DATA_DIR=" + VALUE
    + r"\s+(?:[A-Za-z_]\w*=\S*\s+)*(?:py|python[\d.]*)(?:\.exe)?\s"
)
# A session assignment counts only as a plain statement, never after && || | (.
DATA_DIR_SESSION = re.compile(
    r"(?:^|;|\n)\s*(?:export\s+DATA_DIR=|\$env:DATA_DIR\s*=\s*)" + VALUE,
    re.IGNORECASE,
)
# A value the shell would expand is not the path the check resolves.
# A Windows 8.3 short name (RUNNER~1) is a literal path, not a home expansion.
EXPANDED = re.compile(r"[$`*?\[{]|.~(?!\d)")
# Anything that could load another checkout's server.py keeps the import blocked.
ELSEWHERE = re.compile(
    START_OF_COMMAND + r"(?:cd|pushd|set-location|sl|push-location)\b"
    r"|chdir|\bPYTHONPATH\b|sys\.path|[\\/][^\s\"']*server\.py"
    # Anything that could drop DATA_DIR again before the import.
    r"|\bunset\b|\benv\s+-u|remove-item\s+env:|os\.environ|\(\s*export\b",
    re.IGNORECASE,
)


def imports_server(command: str) -> bool:
    return bool(PYTHON.search(command)) and any(
        pattern.search(command) for pattern in SERVER
    )


def bound_data_dir(command: str) -> str | None:
    """The DATA_DIR the command's single Python process runs with, else None."""
    runs = list(PYTHON.finditer(command))
    if len(runs) != 1:
        return None
    prefix = DATA_DIR_PREFIX.search(command)
    if prefix is not None:
        return prefix.group(1)
    session = [
        match for match in DATA_DIR_SESSION.finditer(command) if match.end() <= runs[0].start()
    ]
    return session[-1].group(1) if session else None


def server_import_allowed(command: str, cwd: str) -> bool:
    """A temporary DATA_DIR bound to the one Python process, the server.py of cwd,
    and no .env in the checkout root or that directory."""
    value = bound_data_dir(command)
    if value is None or not cwd or ELSEWHERE.search(command) or EXPANDED.search(value):
        return False
    try:
        data = Path(value).expanduser().resolve()
        temp = Path(tempfile.gettempdir()).resolve()
    except OSError:
        return False
    secret = "." + "env"
    return (
        (data == temp or temp in data.parents)
        and not (Path(cwd) / secret).exists()
        and not (data / secret).exists()
    )


def main() -> int:
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
    except ValueError:
        print("Blocked: unreadable hook input.", file=sys.stderr)
        return 2
    command = str((payload.get("tool_input") or {}).get("command") or "")
    command = TEMPLATE.sub(" ", command)
    if imports_server(command) and not server_import_allowed(
        command, str(payload.get("cwd") or "")
    ):
        print(
            "Blocked: importing or running server.py loads the checkout's and the data "
            "directory's local settings file, which can hold live secrets (#3076). "
            "Set DATA_DIR to a temporary directory in the same command, in a checkout "
            "without such a file, or test through tests/run_tests.py.",
            file=sys.stderr,
        )
        return 2
    if not secret_mention_allowed(command):
        print(
            "Blocked: the command names T3's secret folder, which holds the app "
            "password and database key (#3758). Pass its file only as --password-file "
            "to scripts/prod_diagnostics.py, prod_guards.py, prod_logs.py, "
            "ui_walk.mjs, ui_explore.mjs or post_release_actions.mjs. If you only mention the path "
            "(for example in a commit message), pass the text through a file.",
            file=sys.stderr,
        )
        return 2
    match = next((m for p in PRIVATE if (m := p.search(command))), None)
    if match:
        # The matched path word, never more of the command (#3924).
        word = match.group().strip(" \t\r\n\"'=:")
        print(
            "Blocked: the command touches .env, the data directory, or Garmin tokens "
            f"(matched `{word}`). These hold live secrets and athlete data; use "
            "temporary test data instead. To search source for such a name, use the "
            "Grep tool. If you only mention such a path (for example in a commit "
            "message), pass the text through a file.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

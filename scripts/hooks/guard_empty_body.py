"""Claude Code PreToolUse hook: no gh PR or issue write with an empty body (#2954).

`gh pr edit --body-file F` accepts an empty file and blanks the public text. A
`gh pr view ... > F` run from a directory that is no repository left F empty
and wiped PR #2938's body. This blocks `gh pr create|edit|comment|review` and
`gh issue create|edit|comment` when `--body-file`/`-F` names an existing file
that is empty or blank (whitespace or a byte-order mark only), and `gh pr|issue
edit` with an empty `--body`/`-b`. It reads the command as text before it
runs, with guard_destructive_git.py's continuation and segment rules: a
missing file passes (gh refuses it without writing), and so does a file the
same command writes first, whose new content is unknown. Exit 2 blocks.
"""

import itertools
import json
import os
import re
import shlex
import sys
from pathlib import Path

from guard_destructive_git import CONTINUATION  # one continuation rule

SEPARATORS = ";&|()"  # guard_destructive_git's segment marks, read outside quotes

WRITES = {
    "pr": {"create", "edit", "comment", "review"},
    "issue": {"create", "edit", "comment"},
}
FILE_FLAGS, TEXT_FLAGS = ("--body-file", "-F"), ("--body", "-b")
# A file the command itself writes: `> F`, `>> F`, `tee F`, `Out-File F`, ...
WRITTEN = re.compile(
    r"(?:>>?|\btee|\bOut-File|\bSet-Content)\s+(?:-\w+\s+)*[\"']?([^\s\"';&|()]+)",
    re.IGNORECASE,
)


def _values(args: list[str], flags: tuple[str, ...]) -> list[str]:
    """Values of `--flag value`, `--flag=value` and the short `-Fvalue`."""
    found = [nxt for arg, nxt in itertools.pairwise(args) if arg in flags]
    for arg, flag in itertools.product(args, flags):
        if arg.startswith(f"{flag}="):
            found.append(arg[len(flag) + 1 :])
        elif len(flag) == 2 and arg.startswith(flag) and len(arg) > 2:
            found.append(arg[2:])
    return found


def _local(path: str, cwd: Path) -> Path:
    """A shell path as Python opens it (`/c/x` is `c:/x` under Git Bash).

    Exported variables expand (`$NAME`, PowerShell `$env:NAME`); a variable set
    only in the command does not, and its path then counts as missing.
    """
    path = re.sub(r"\$env:(\w+)", r"${\1}", path, flags=re.IGNORECASE)
    path = os.path.expanduser(os.path.expandvars(path))
    if os.name == "nt" and (drive := re.match(r"/([A-Za-z])/", path)):
        path = f"{drive.group(1)}:/{path[3:]}"
    return cwd / path


def _empty_file(value: str, cwd: Path) -> bool:
    try:
        raw = _local(value, cwd).read_bytes()
    except OSError:
        return False  # missing: gh refuses it without writing
    utf16 = raw[:2] in (b"\xff\xfe", b"\xfe\xff")
    text = raw.decode("utf-16" if utf16 else "utf-8-sig", "replace")
    return not text.strip().strip("\ufeff\x00").strip()


def _segments(text: str) -> list[list[str]]:
    """The words of each shell segment, split on ; & | ( ) and newlines outside
    quotes: a title like "fix(x): y" stays one word (Gitar P2)."""
    parts: list[str] = []
    current, quote = "", ""
    for char in text.replace("\n", ";"):
        if quote:
            quote = "" if char == quote else quote
        elif char in "\"'":
            quote = char
        elif char in SEPARATORS:
            parts, current = [*parts, current], ""
            continue
        current += char
    segments = []
    for part in [*parts, current]:
        try:  # Windows paths: shlex would read their backslashes as escapes
            segments.append(shlex.split(part.replace("\\", "/")))
        except ValueError:  # an unbalanced quote
            continue
    return segments


def _gh_writes(words: list[str]) -> list[tuple[str, list[str]]]:
    """(verb, arguments) of each gh PR or issue write in one shell segment."""
    return [
        (words[start + 2], words[start + 3 :])
        for start, word in enumerate(words[:-2])
        if Path(word).stem.lower() == "gh"
        and words[start + 2] in WRITES.get(words[start + 1], ())
    ]


def blocked(command: str, cwd: Path) -> bool:
    text = CONTINUATION.sub(" ", command)
    written = {m.group(1) for m in WRITTEN.finditer(text.replace("\\", "/"))}
    for words in _segments(text):
        for verb, args in _gh_writes(words):
            texts = _values(args, TEXT_FLAGS) if verb == "edit" else []
            files = [v for v in _values(args, FILE_FLAGS) if v not in written]
            if any(not t.strip() for t in texts) or any(
                _empty_file(v, cwd) for v in files if v != "-"
            ):
                return True
    return False


def main() -> int:
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
    except ValueError:
        print("Blocked: unreadable hook input.", file=sys.stderr)
        return 2
    command = str((payload.get("tool_input") or {}).get("command") or "")
    if "gh" not in command or not blocked(command, Path(payload.get("cwd") or ".")):
        return 0
    print(
        "Blocked: this gh write has an empty body (an empty --body or an empty "
        "--body-file); it would blank the PR or issue text (#2954). Check the "
        "file, then run it again.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())

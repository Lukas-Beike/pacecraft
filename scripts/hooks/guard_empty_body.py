"""Claude Code PreToolUse hook: no gh PR or issue write with an empty body (#2954).

`gh pr edit --body-file F` accepts an empty file and blanks the public text. A
`gh pr view ... > F` run from a directory that is no repository left F empty
and wiped PR #2938's body. This blocks `gh pr create|edit|comment|review` and
`gh issue create|edit|comment` when `--body-file`/`-F` names an existing file
that is empty or whitespace only, or when `--body`/`-b` is empty. A missing
file passes: gh refuses it without writing, and the command may create it
first. Like the other guards it reads shell segments as text. Exit 2 blocks.
"""

import itertools
import json
import os
import re
import shlex
import sys
from pathlib import Path

SEGMENT = re.compile(r"[;&|\n]+")
WRITES = {
    "pr": {"create", "edit", "comment", "review"},
    "issue": {"create", "edit", "comment"},
}
FILE_FLAGS, TEXT_FLAGS = {"--body-file", "-F"}, {"--body", "-b"}


def _values(args: list[str], flags: set[str]) -> list[str]:
    """Values of `--flag value` and `--flag=value` among the arguments."""
    found = [nxt for arg, nxt in itertools.pairwise(args) if arg in flags]
    return found + [
        arg.split("=", 1)[1]
        for arg in args
        if arg.split("=", 1)[0] in flags and "=" in arg
    ]


def _local(path: str, cwd: Path) -> Path:
    """A shell path as Python opens it (`/c/x` is `c:/x` under Git Bash).

    Exported variables expand; a variable set only in the command does not,
    and its path then counts as missing.
    """
    path = os.path.expanduser(os.path.expandvars(path))
    if os.name == "nt" and (drive := re.match(r"/([A-Za-z])/", path)):
        path = f"{drive.group(1)}:/{path[3:]}"
    return cwd / path


def _empty_file(value: str, cwd: Path) -> bool:
    try:
        return (
            value != "-"
            and not _local(value, cwd).read_text("utf-8", "replace").strip()
        )
    except OSError:
        return False  # missing: gh refuses it without writing


def blocked(command: str, cwd: Path) -> bool:
    for segment in SEGMENT.split(command):
        try:  # Windows paths: shlex would read their backslashes as escapes
            words = shlex.split(segment.replace("\\", "/"))
        except ValueError:
            continue
        for start, word in enumerate(words[:-2]):
            noun, verb = words[start + 1], words[start + 2]
            if Path(word).stem.lower() != "gh" or verb not in WRITES.get(noun, ()):
                continue
            args = words[start + 3 :]
            if any(not text.strip() for text in _values(args, TEXT_FLAGS)):
                return True
            if any(_empty_file(value, cwd) for value in _values(args, FILE_FLAGS)):
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

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

A `gh pr create|edit` whose body is known (an existing --body-file or a --body
text) must also name its issue as the `validate` step requires, with the same
regex and message (#3535); the CI step stays the check of record.
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
# The `validate` step's "Require an issue link in the PR body" regex and message
# (.github/workflows/validate-conventions.yml); [[:alnum:]_] is \w.
ISSUE_LINK = re.compile(
    r"(?:^|\W)(?:[Cc]loses|[Ff]ixes|[Rr]esolves|[Pp]art of) #[0-9]+"
)
LINK_MESSAGE = (
    "The PR body must name its issue with 'Closes #N' (completes it) or "
    "'Part of #N' (one of several PRs)."
)
LINKED_WRITES = {"create", "edit"}  # gh pr writes whose body is the PR body
OTHER_TARGET = {"-R", "--repo", "--web", "-w"}  # not this repository's CI
# A file the command itself writes: `> F`, `>> F`, `tee F`, `Out-File F`, ...
WRITTEN = re.compile(
    r"(?:>>?|\btee|\bOut-File|\bSet-Content)\s+(?:-\w+\s+)*[\"']?([^\s\"';&|()]+)",
    re.IGNORECASE,
)


def _values(args: list[str], flags: tuple[str, ...]) -> list[str]:
    """Values of `--flag value`, `--flag=value` and the short `-Fvalue`, in
    command order (gh takes the last one)."""
    found: list[str] = []
    for (index, arg), flag in itertools.product(enumerate(args), flags):
        if arg == flag:
            found += args[index + 1 : index + 2]
        elif arg.startswith(f"{flag}="):
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


def _file_text(value: str, cwd: Path) -> str | None:
    """The body file's text, or None when it is missing (gh refuses it)."""
    try:
        raw = _local(value, cwd).read_bytes()
    except OSError, ValueError:  # ValueError: a NUL in the path (review, #3535)
        return None
    utf16 = raw[:2] in (b"\xff\xfe", b"\xfe\xff")
    return raw.decode("utf-16" if utf16 else "utf-8-sig", "replace")


def _blank(text: str) -> bool:
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
            parts.append(current)  # in place: linear in the command (#3119)
            current = ""
            continue
        current += char
    segments = []
    for part in [*parts, current]:
        try:  # Windows paths: shlex would read their backslashes as escapes
            segments.append(shlex.split(part.replace("\\", "/")))
        except ValueError:  # an unbalanced quote
            continue
    return segments


def _gh_writes(words: list[str]) -> list[tuple[str, str, int]]:
    """(noun, verb, index of its first argument) of each gh PR or issue write in
    one shell segment; a write's arguments run to the end of the segment."""
    return [
        (words[start + 1], words[start + 2], start + 3)
        for start, word in enumerate(words[:-2])
        if Path(word).stem.lower() == "gh"
        and words[start + 2] in WRITES.get(words[start + 1], ())
    ]


def _file_bodies(values: list[str], cwd: Path, written: set[str]) -> list[str]:
    """The known texts of body files: not stdin, not written by the command
    itself, not missing."""
    texts = (_file_text(v, cwd) for v in values if v != "-" and v not in written)
    return [text for text in texts if text is not None]


def _pr_body(
    args: list[str], cwd: Path, written: set[str], escaped: bool
) -> str | None:
    """The body one `gh pr create|edit` sends, from its own arguments, or None
    when it is unknown: no body, stdin, a file the command writes or a missing
    one, a text gh gets after expansion (`$`, a backtick) or with escaped quotes
    the word split cannot follow, another repository, or the browser (#3535)."""
    if OTHER_TARGET.intersection(args) or any(a.startswith("--repo=") for a in args):
        return None
    if texts := _values(args, TEXT_FLAGS):
        known = not escaped and not set("$`").intersection(texts[-1])
        return texts[-1] if known else None
    files = _values(args, FILE_FLAGS)
    return next(iter(_file_bodies(files[-1:], cwd, written)), None)


def _pr_bodies(
    words: list[str],
    writes: list[tuple[str, str, int]],
    known: tuple[Path, set[str], bool],
) -> list[str]:
    """The known bodies of the segment's `gh pr create|edit` writes, each from
    its own arguments up to the next write; gh takes the last body value."""
    ends = [first - 3 for _, _, first in writes[1:]] + [len(words)]
    bodies = [
        _pr_body(words[first:end], *known)
        for (noun, verb, first), end in zip(writes, ends, strict=True)
        if noun == "pr" and verb in LINKED_WRITES
    ]
    return [body for body in bodies if body is not None]


def blocked(command: str, cwd: Path) -> str | None:
    """Why the command is blocked: "empty" or "link" (#3535), else None."""
    text = CONTINUATION.sub(" ", command)
    written = {m.group(1) for m in WRITTEN.finditer(text.replace("\\", "/"))}
    # `\"` or PowerShell's `` `" `` inside a quoted body: the word split loses it.
    escaped = '\\"' in text or '`"' in text
    for words in _segments(text):
        writes = _gh_writes(words)
        if not writes:
            continue
        # A later write's arguments are a suffix of an earlier one's, so the first
        # write (and the first edit, for --body) covers every value once (#3119).
        edits = [first for _, verb, first in writes if verb == "edit"]
        texts = _values(words[edits[0] :], TEXT_FLAGS) if edits else []
        files = _file_bodies(_values(words[writes[0][2] :], FILE_FLAGS), cwd, written)
        if any(not t.strip() for t in texts) or any(_blank(f) for f in files):
            return "empty"
        bodies = _pr_bodies(words, writes, (cwd, written, escaped))
        if any(not ISSUE_LINK.search(body) for body in bodies):
            return "link"
    return None


def main() -> int:
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
    except ValueError:
        print("Blocked: unreadable hook input.", file=sys.stderr)
        return 2
    command = str((payload.get("tool_input") or {}).get("command") or "")
    reason = (
        blocked(command, Path(payload.get("cwd") or ".")) if "gh" in command else None
    )
    if reason is None:
        return 0
    if reason == "link":
        print(f"Blocked: {LINK_MESSAGE} (#3535)", file=sys.stderr)
        return 2
    print(
        "Blocked: this gh write has an empty body (an empty --body or an empty "
        "--body-file); it would blank the PR or issue text (#2954). Check the "
        "file, then run it again.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())

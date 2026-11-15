r"""Claude Code PreToolUse hook: block Bash edits whose backslashes collapse (#2793).

On the Windows host the Bash tool turns `\\` into `\`, also inside a quoted
heredoc (#2723), so an edit script written inline silently changes its
patterns. For the Bash tool only, this hook blocks a doubled backslash in
- the body of a heredoc fed to an interpreter (python, node, bash, sh, perl,
  ruby, pwsh, powershell) or to `tee`, or written by `cat` to a file;
- a shell segment that runs an inline program or an in-place edit:
  `python -c`, `node -e`/`--eval`, `perl -e`/`-pi`, `ruby -e`, `pwsh -Command`,
  `sed -i`/`--in-place`.
A commit-message heredoc, a script run by path and read-only commands pass.
Like the other guards it is a text safety net, not a parser. Exit 2 blocks.
"""

import bisect
import json
import re
import sys

DOUBLED = "\\\\"
INTERPRETER = re.compile(
    r"(?:^|[\s;&|(/\\])(?i:python(?:3(?:\.\d+)?)?|py|node|bash|sh|perl|ruby"
    r"|pwsh|powershell)(?:\.exe)?(?=[\s<|;)]|$)"
)
TEE, CAT = re.compile(r"\btee\b"), re.compile(r"\bcat\b")
TO_FILE = re.compile(r">(?![>&]?\s*/dev/null)(?!&)")
HEREDOC = re.compile(r"<<-?[ \t]*(?:'([^'\n]+)'|\"([^\"\n]+)\"|\\?([A-Za-z_][\w-]*))")
INLINE = {"-c", "-e", "--eval", "--command", "-Command", "-command"}
PERL_IN_PLACE = re.compile(r"-p?i(?![A-Za-z])")
SED = re.compile(r"(?:^|[/\\])sed(?:\.exe)?$")
IN_PLACE = re.compile(r"-[A-Za-hj-z]*i|--in-place")


def _segments(text: str) -> list[str]:
    """Shell segments split at ; & | and newlines outside single and double quotes."""
    parts, start, quote = [], 0, ""
    for index, char in enumerate(text):
        if char in "'\"" and quote in ("", char):
            quote = "" if quote else char
        elif char in ";&|\n" and not quote:
            parts.append(text[start:index])
            start = index + 1
    return [*parts, text[start:]]


def _unquoted_heredocs(line: str):
    """The heredoc markers of a line that lie outside single and double quotes,
    with the quote state carried along the line in one pass (#3119)."""
    single = double = False
    scanned = 0
    for match in HEREDOC.finditer(line):
        for char in line[scanned : match.start()]:
            if char == "'" and not double:
                single = not single
            elif char == '"' and not single:
                double = not double
        scanned = match.start()
        if not (single or double):
            yield match


def split_heredocs(command: str) -> tuple[list[tuple[str, str]], str]:
    """(introducing line, body) per terminated heredoc, and the shell text without bodies."""
    lines, found, shell = command.split("\n"), [], []
    # Line numbers per stripped text: each terminator is found without a rescan (#3119).
    numbers: dict[str, list[int]] = {}
    for number, text in enumerate(lines):
        numbers.setdefault(text.strip(), []).append(number)
    index = 0
    while index < len(lines):
        line = lines[index]
        shell.append(line)
        index += 1
        for match in _unquoted_heredocs(line):
            ends = numbers.get(match[1] or match[2] or match[3], [])
            first = bisect.bisect_left(ends, index)
            if first < len(ends):
                found.append((line, "\n".join(lines[index : ends[first]])))
                index = ends[first] + 1
                break
    return found, "\n".join(shell)


def _writes(line: str) -> bool:
    """`tee`, or `cat` whose first following `>` writes a file (not /dev/null or
    a descriptor). Each stretch between two `>` is read once (#3124)."""
    if TEE.search(line):
        return True
    start = 0
    for index, char in enumerate(line):
        if char == ">":
            if TO_FILE.match(line, index) and CAT.search(line, start, index):
                return True
            start = index + 1
    return False


def _inline_edit(segment: str) -> bool:
    words = segment.split()
    runs = any(INTERPRETER.search(f" {word}") for word in words)
    if runs and any(word in INLINE or PERL_IN_PLACE.match(word) for word in words):
        return True
    return any(SED.search(word) for word in words) and any(
        IN_PLACE.match(word) for word in words
    )


def blocked(command: str) -> bool:
    heredocs, shell = split_heredocs(command)
    for line, body in heredocs:
        if DOUBLED in body and (INTERPRETER.search(line) or _writes(line)):
            return True
    return any(
        DOUBLED in segment and _inline_edit(segment) for segment in _segments(shell)
    )


def main() -> int:
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
    except ValueError:
        payload = None
    if not isinstance(payload, dict) or not isinstance(
        payload.get("tool_input") or {}, dict
    ):
        print("Blocked: unreadable hook input.", file=sys.stderr)
        return 2
    if payload.get("tool_name") != "Bash":
        return 0
    if blocked(str((payload.get("tool_input") or {}).get("command") or "")):
        print(
            "Blocked: the Bash tool on this host collapses a doubled backslash to "
            "one in heredocs and one-liners (#2723, #2793), so this edit would "
            "change its own patterns. Write the file with the Write or Edit tool, "
            "or put the edit logic in a scratchpad script written with Write and "
            "run it by path.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

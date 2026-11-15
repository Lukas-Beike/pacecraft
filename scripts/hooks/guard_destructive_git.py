"""Claude Code PreToolUse hook: block git commands that discard work (#2635).

Idea from Matt Pocock's git-guardrails-claude-code (MIT); see
.agents/skills/THIRD_PARTY.md. It blocks `git reset --hard`, `git clean -f`, a
forced branch delete, discarding checkouts and restores of the whole tree
(`.`, `:/`, `*`, `checkout -f`, `switch --discard-changes`) and a force push
without a lease; `--force-with-lease` (branch_sync.py), normal pushes and
`git restore --staged .` stay allowed. Like guard_private_paths.py it is a
text safety net, not a sandbox: it reads each shell segment after joining line
continuations and dropping quotes, so a commit message that merely names such a
command is blocked too; pass that text through a file. Exit code 2 blocks.
"""

import json
import re
import sys

CONTINUATION = re.compile(r"[\\`]\r?\n")
QUOTES = re.compile(r"[\"'`]")
SEGMENT = re.compile(r"[;&|\n()]+")
GIT_NAMES = {"git", "git.exe"}
# Global options that take the next word as their value (`git -C <dir> ...`).
GLOBAL_WITH_VALUE = {
    "-C",
    "-c",
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--config-env",
}
WHOLE_TREE = {".", "./", ".\\", ":/", ":/*", "*"}


LONG_OPTIONS = (
    "--hard",
    "--force",
    "--delete",
    "--worktree",
    "--staged",
    "--discard-changes",
)
SHORT_LETTERS = frozenset("fdDWS")
WHOLE, PLUS = "whole-tree", "+refspec"  # markers no option or letter can equal


def _features(word: str) -> frozenset[str]:
    """What the rules ask of one argument: the long options it spells or
    abbreviates as git accepts (`--ha` for `--hard`), the letters of a
    short-option group (-fdx), a whole-tree path, a forced `+` refspec."""
    found = {
        option for option in LONG_OPTIONS if len(word) > 2 and option.startswith(word)
    }
    if word.startswith("-") and not word.startswith("--"):
        found |= SHORT_LETTERS & set(word[1:])
    if word in WHOLE_TREE:
        found.add(WHOLE)
    if word.startswith("+"):
        found.add(PLUS)
    return frozenset(found)


# Per subcommand: does it discard work, given what its arguments carry?
RULES = {
    "reset": lambda f: "--hard" in f,
    "clean": lambda f: "--force" in f or "f" in f,
    "branch": lambda f: (
        "D" in f or (("--delete" in f or "d" in f) and ("--force" in f or "f" in f))
    ),
    "checkout": lambda f: "--force" in f or "f" in f or WHOLE in f,
    # Only `--staged` (without `--worktree`) unstages and keeps the files.
    "restore": lambda f: (
        WHOLE in f
        and ("--worktree" in f or "W" in f or not ("--staged" in f or "S" in f))
    ),
    "switch": lambda f: "--discard-changes" in f or "--force" in f or "f" in f,
    "push": lambda f: "--force" in f or "f" in f or PLUS in f,
}


def _discards(words: list[str]) -> bool:
    """Whether a git call in one segment's words discards work."""
    # From each position, one pass back to front (#3124): what the words up to
    # the segment's end carry, and the first word that is no global option.
    carried: list[frozenset[str]] = [frozenset()] * (len(words) + 1)
    command_at = list(range(len(words) + 1))
    for index in range(len(words) - 1, -1, -1):
        carried[index] = carried[index + 1] | _features(words[index])
        if words[index].startswith("-"):
            step = 2 if words[index] in GLOBAL_WITH_VALUE else 1
            command_at[index] = command_at[min(index + step, len(words))]
    for index, word in enumerate(words):
        name = word.replace("\\", "/").rsplit("/", 1)[-1].lower()
        sub = command_at[index + 1] if name in GIT_NAMES else len(words)
        rule = RULES.get(words[sub]) if sub < len(words) else None
        if rule and rule(carried[sub + 1]):
            return True
    return False


def blocked(command: str) -> bool:
    text = QUOTES.sub("", CONTINUATION.sub(" ", command))
    return any(_discards(segment.split()) for segment in SEGMENT.split(text))


def main() -> int:
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
    except ValueError:
        print("Blocked: unreadable hook input.", file=sys.stderr)
        return 2
    if blocked(str((payload.get("tool_input") or {}).get("command") or "")):
        print(
            "Blocked: this git command discards work (reset --hard, clean -f, a "
            "forced branch delete, a whole-tree checkout/restore, or a force push "
            "without a lease). Use a WIP commit, `git worktree remove` of a clean "
            "worktree, or `git push --force-with-lease`; ask the user if you really "
            "need it. If you only mention the command, pass the text through a file.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

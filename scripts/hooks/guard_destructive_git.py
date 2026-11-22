"""Claude Code PreToolUse hook: block git commands that discard work (#2635).

Idea from Matt Pocock's git-guardrails-claude-code (MIT); see
.agents/skills/THIRD_PARTY.md. It blocks `git reset --hard`, `git clean -f`, a
forced branch delete, discarding checkouts and restores of the whole tree
(`.`, `:/`, `*`, `checkout -f`, `switch --discard-changes`) and a force push
without a lease; `--force-with-lease` (branch_sync.py), normal pushes and
`git restore --staged .` stay allowed. It also enforces the rules without
exceptions (#3923): no `--no-verify` (or `commit -n`), no `git add -A`, `--all`
or a whole-tree path, no `gh pr merge --auto` without `--squash`, no
`docker rm -v` or compose `rm`/`down` with volumes, and no `ruff format` of a
folder (files only; `--check` and `--diff` stay allowed). Like
guard_private_paths.py it is a
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
WHOLE_TREE = {".", "./", ".\\", "./.", "./*", ":/", ":/.", ":/*", "*", "**"}


LONG_OPTIONS = (
    "--hard",
    "--force",
    "--delete",
    "--worktree",
    "--staged",
    "--discard-changes",
    "--no-verify",
    "--all",
)
SHORT_LETTERS = frozenset("fdDWSAn")
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
# Subcommands that break a rule without exceptions (#3923); any subcommand
# with `--no-verify` does too.
RULE_BREAKS = {
    "commit": lambda f: "n" in f,
    "add": lambda f: "--all" in f or "A" in f or WHOLE in f,
}
RULE_BREAKS["stage"] = RULE_BREAKS["add"]


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
        if sub >= len(words):
            continue
        found = carried[sub + 1]
        rules = (RULES.get(words[sub]), RULE_BREAKS.get(words[sub]))
        if "--no-verify" in found or any(rule and rule(found) for rule in rules):
            return True
    return False


# Options whose value is the next word, so that word is no positional argument.
DOCKER_WITH_VALUE = frozenset(
    [
        "-H",
        "--host",
        "-c",
        "--context",
        "--config",
        "-l",
        "--log-level",
        "-f",
        "--file",
        "-p",
        "--project-name",
        "--profile",
        "--env-file",
        "--project-directory",
        "--ansi",
        "--progress",
        "--parallel",
        "--tlscacert",
        "--tlscert",
        "--tlskey",
    ]
)
GH_WITH_VALUE = frozenset(["-R", "--repo"])
RUFF_WITH_VALUE = frozenset(
    [
        "--config",
        "--target-version",
        "--exclude",
        "--extend-exclude",
        "--range",
        "--cache-dir",
        "--line-length",
        "--extension",
    ]
)
RUFF_FILES = (".py", ".pyi", ".pyw", ".ipynb")
RUFF_READ_ONLY = ("--check", "--diff", "--stdin-filename", "--help", "-h", "--version")
# A segment led by a text tool only names the commands (`rg "docker rm -v" docs`).
TEXT_TOOLS = frozenset(
    [
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "echo",
        "printf",
        "findstr",
        "select-string",
        "sls",
    ]
)
# A redirection (`2>`, `>`, `<`) is no path; a bare one takes the next word.
REDIRECT = re.compile(r"\d*[<>]")


def _option(word: str) -> bool:
    return (word.startswith("-") and word != "-") or bool(REDIRECT.match(word))


def _flag(word: str, long: str, letter: str = "") -> bool:
    """A long flag not set to false, or a short-option group with the letter."""
    name, _, value = word.partition("=")
    if name == long and value.lower() not in {"false", "0"}:
        return True
    return (
        bool(letter)
        and word.startswith("-")
        and not word.startswith("--")
        and letter in word
    )


def _suffix(words: list[str], test) -> list[bool]:
    """Per position: does any word from there to the segment's end pass `test`."""
    found = [False] * (len(words) + 2)
    for index in range(len(words) - 1, -1, -1):
        found[index] = found[index + 1] or test(words[index])
    return found


def _next_positional(words: list[str], with_value: frozenset[str]) -> list[int]:
    """Per position: the first positional word from there; a `with_value` option
    takes the next word as its value unless it is an option (`docker rm -f -v`)."""
    count = len(words)
    nxt = [count] * (count + 3)
    for index in range(count - 1, -1, -1):
        word = words[index]
        if not _option(word):
            nxt[index] = index
        elif (
            (word in with_value or word.endswith((">", "<")))
            and index + 1 < count
            and not _option(words[index + 1])
        ):
            nxt[index] = nxt[index + 2]
        else:
            nxt[index] = nxt[index + 1]
    return nxt


def _breaks_tool_rule(words: list[str]) -> bool:
    """A gh, docker or ruff call that breaks a rule without exceptions (#3923).

    One pass back to front per tool, like `_discards` (#3124)."""
    comment = next((i for i, w in enumerate(words) if w.startswith("#")), len(words))
    words = words[:comment]
    names = [
        w.replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")
        for w in words
    ]
    if names[:1] and names[0] in TEXT_TOOLS:
        return False
    count = len(words)
    word_at = [*words, "", "", ""]
    if "gh" in names:
        nxt = _next_positional(words, GH_WITH_VALUE)
        auto = _suffix(words, lambda w: _flag(w, "--auto"))
        squash = _suffix(words, lambda w: _flag(w, "--squash", "s"))
        for index in (i for i, name in enumerate(names) if name == "gh"):
            first = nxt[index + 1]
            second = nxt[first + 1] if first < count else count
            if (
                (word_at[first], word_at[second]) == ("pr", "merge")
                and auto[index + 1]
                and not squash[index + 1]
            ):
                return True
    if {"docker", "docker-compose"} & set(names):
        nxt = _next_positional(words, DOCKER_WITH_VALUE)
        volumes = _suffix(words, lambda w: _flag(w, "--volumes", "v"))
        for index, name in enumerate(names):
            if name not in {"docker", "docker-compose"} or not volumes[index + 1]:
                continue
            first = nxt[index + 1]
            if word_at[first] in {"container", "compose"}:
                first = nxt[first + 1]
            if word_at[first] in {"rm", "remove", "down"}:
                return True
    if "ruff" in names:
        nxt = _next_positional(words, RUFF_WITH_VALUE)
        read_only = _suffix(words, lambda w: any(_flag(w, f) for f in RUFF_READ_ONLY))
        # Per positional word: is it or a later positional of its chain a folder.
        folder = [False] * (count + 3)
        for index in range(count - 1, -1, -1):
            word = words[index].lower()
            later = nxt[index + 1]
            folder[index] = (word != "-" and not word.endswith(RUFF_FILES)) or (
                later < count and folder[later]
            )
        for index in (i for i, name in enumerate(names) if name == "ruff"):
            first = nxt[index + 1]
            if word_at[first] != "format" or read_only[index + 1]:
                continue
            path = nxt[first + 1]
            if path >= count or folder[path]:
                return True
    return False


def blocked(command: str) -> bool:
    text = QUOTES.sub("", CONTINUATION.sub(" ", command))
    return any(
        _discards(words) or _breaks_tool_rule(words)
        for words in (segment.split() for segment in SEGMENT.split(text))
    )


def main() -> int:
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
    except ValueError:
        print("Blocked: unreadable hook input.", file=sys.stderr)
        return 2
    if blocked(str((payload.get("tool_input") or {}).get("command") or "")):
        print(
            "Blocked: this command discards work or skips a repository rule (reset "
            "--hard, clean -f, a forced branch delete, a whole-tree checkout/restore, "
            "a force push without a lease, --no-verify, git add -A or ., gh pr merge "
            "--auto without --squash, docker rm -v, ruff format of a folder). Use a "
            "WIP commit, `git push --force-with-lease`, files staged by name, the "
            "pr-merge script, or `ruff format <files>`; ask the user if you really "
            "need it. If you only mention the command, pass the text through a file.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

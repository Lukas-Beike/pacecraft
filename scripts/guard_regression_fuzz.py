"""Prove a command guard change only blocks more: old against new, side by side.

    python scripts/guard_regression_fuzz.py scripts/hooks/guard_resolve_thread.py \
        [--base origin/develop] [--count 60000] [--seed 1] [--seed-command TEXT ...]

Loads the hook as it is at ``--base`` (``git show``) and as it is in the working
tree, feeds both the same mutated shell commands as PreToolUse input
(``{"tool_input": {"command": ...}}``) and compares their verdicts (exit 2
blocks). A command the new hook allows but the old one blocks is "newly
allowed", the regression a tightening must never have; it exits 1 and prints up
to ten of them. Newly blocked commands are counted only. The commands grow from
seed commands, by default ones that run or name the review-thread resolve
script, with case, quote, escape, glob, variable, short-name, brace, separator
and wrapper mutations (#3527). A guard PR states its "0 newly allowed" line.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import random
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
BLOCK = 2
TARGET = "scripts/resolve_review_thread.py"
DEFAULT_SEEDS = (
    f"python {TARGET} 3500 PRRT_abc",
    f"python {TARGET} 3500 PRRT_abc && echo done",
    f"cat {TARGET}",
    f"python3 -c \"import runpy; runpy.run_path('{TARGET}')\"",
    f"bash -c 'python {TARGET} 1 PRRT_x'",
    f"rg --pre cat resolve {TARGET}",
    "git status",
    "python scripts/check_changed.py",
)
# Each mutation rewrites one command into a variant a shell may still run.
WRAPPERS = ("bash -c '{}'", 'sh -c "{}"', 'pwsh -Command "{}"', "env {}", "time {}")
SEPARATORS = (" ; ", " && ", " || ", " | ", "\n")


def _case(text: str, rng: random.Random) -> str:
    return "".join(c.upper() if rng.random() < 0.3 else c for c in text)


def _quote(text: str, rng: random.Random) -> str:
    at = rng.randrange(len(text) + 1)
    quote = rng.choice("\"'")
    return f"{text[:at]}{quote}{quote}{text[at:]}"


def _escape(text: str, rng: random.Random) -> str:
    at = rng.randrange(len(text))
    escaped = rng.choice((f"\\{text[at]}", f"\\x{ord(text[at]):02x}", f"`{text[at]}"))
    return text[:at] + escaped + text[at + 1 :]


def _glob(text: str, rng: random.Random) -> str:
    at = rng.randrange(len(text))
    if text[at] in " \t\n":
        return text
    return text[:at] + rng.choice(("?", "*", f"[{text[at]}]")) + text[at + 1 :]


def _variable(text: str, rng: random.Random) -> str:
    at = rng.randrange(len(text))
    return text[:at] + rng.choice(("$X", "${X}", "%X%", "$(echo)")) + text[at:]


def _short_name(text: str, rng: random.Random) -> str:
    del rng
    return text.replace("resolve_review_thread", "RESOLV~1")


def _brace(text: str, rng: random.Random) -> str:
    at = rng.randrange(len(text))
    return text[:at] + "{" + text[at] + ",x}" + text[at + 1 :]


def _join(text: str, rng: random.Random) -> str:
    other = rng.choice(DEFAULT_SEEDS)
    return text + rng.choice(SEPARATORS) + other


def _wrap(text: str, rng: random.Random) -> str:
    return rng.choice(WRAPPERS).format(text)


MUTATIONS: tuple[Callable[[str, random.Random], str], ...] = (
    _case,
    _quote,
    _escape,
    _glob,
    _variable,
    _short_name,
    _brace,
    _join,
    _wrap,
)


def commands(seeds: Sequence[str], count: int, seed: int) -> Iterable[str]:
    """The seeds, then ``count`` mutants of one to three stacked mutations."""
    rng = random.Random(seed)
    yield from seeds
    for _ in range(count):
        text = rng.choice(seeds)
        for _step in range(rng.randint(1, 3)):
            if text:
                text = rng.choice(MUTATIONS)(text, rng)
        yield text


def load(path: Path, name: str) -> ModuleType:
    """One hook version as its own module; its sibling modules stay importable."""
    sys.path.insert(0, str(ROOT / "scripts/hooks"))
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verdict(hook: ModuleType, command: str) -> int:
    """The hook's exit code for one command; a crash counts as a block, as the
    hooks fail closed."""
    payload = json.dumps({"tool_input": {"command": command}}).encode()
    stdin = io.TextIOWrapper(io.BytesIO(payload), encoding="utf-8")
    with (
        contextlib.redirect_stderr(io.StringIO()),
        contextlib.redirect_stdout(io.StringIO()),
        _stdin(stdin),
    ):
        try:
            return int(hook.main())
        except SystemExit as exit_:
            return int(exit_.code or 0)
        except Exception:  # noqa: BLE001 - a failing hook blocks
            return BLOCK


@contextlib.contextmanager
def _stdin(stream: io.TextIOWrapper) -> Iterator[None]:
    previous = sys.stdin
    sys.stdin = stream
    try:
        yield
    finally:
        sys.stdin = previous


@dataclass
class Comparison:
    checked: int = 0
    newly_allowed: list[str] = field(default_factory=list)
    newly_blocked: int = 0


def compare(old: ModuleType, new: ModuleType, inputs: Iterable[str]) -> Comparison:
    """Each command's verdict under both versions."""
    result = Comparison()
    for command in inputs:
        result.checked += 1
        before = verdict(old, command) == BLOCK
        after = verdict(new, command) == BLOCK
        if before and not after:
            result.newly_allowed.append(command)
        elif after and not before:
            result.newly_blocked += 1
    return result


def base_version(base: str, hook: str, directory: Path) -> Path:
    source = subprocess.run(
        ["git", "show", f"{base}:{hook}"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    path = directory / f"base_{Path(hook).name}"
    path.write_bytes(source)
    return path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "hook", help="repository path of the hook, e.g. scripts/hooks/guard_shell.py"
    )
    parser.add_argument("--base", default="origin/develop")
    parser.add_argument("--count", type=int, default=60_000)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--seed-command", action="append", default=[], dest="seeds")
    args = parser.parse_args(argv)
    seeds = tuple(args.seeds) or DEFAULT_SEEDS
    with tempfile.TemporaryDirectory() as directory:
        old = load(base_version(args.base, args.hook, Path(directory)), "guard_old")
        new = load(ROOT / args.hook, "guard_new")
        result = compare(old, new, commands(seeds, args.count, args.seed))
    print(
        f"{args.hook}: {result.checked} commands against {args.base}; "
        f"{len(result.newly_allowed)} newly allowed, {result.newly_blocked} newly blocked"
    )
    for command in result.newly_allowed[:10]:
        print(f"  newly allowed: {command!r}")
    return 1 if result.newly_allowed else 0


if __name__ == "__main__":
    sys.exit(main())

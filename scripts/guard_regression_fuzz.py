"""Prove a command guard change only blocks more: old against new, side by side.

    python scripts/guard_regression_fuzz.py scripts/hooks/guard_resolve_thread.py \
        [--base origin/develop] [--count 60000] [--seed 1] [--seed-command TEXT ...]

Takes the whole ``scripts/hooks`` tree as it is at ``--base`` (``git archive``)
and as it is in the working tree, and runs the hook of each in its own worker
process with its own tree first on ``sys.path``, so a hook that imports a
sibling guard gets that version's sibling. Both get the same mutated shell
commands as PreToolUse input (``{"tool_input": {"command": ...}}``); exit 2
blocks, and a crash counts as a block, as the hooks fail closed. A command the
new hook allows but the old one blocks is "newly allowed", the regression a
tightening must never have: the tool exits 1 and prints up to ten of them.
Newly blocked commands are counted only. The commands grow from seed commands,
by default ones that run or name the review-thread resolve script, with case,
quote, escape, glob, variable, short-name, brace, separator and wrapper
mutations (#3527). A guard PR states its "0 newly allowed" line.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import random
import re
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
HOOKS = "scripts/hooks"
BLOCK = 2
# Only a guard hook of this repository and a plain ref are accepted, so the
# arguments can reach neither another path nor a git option.
HOOK_PATH = re.compile(r"scripts/hooks/(guard_[a-z0-9_]+\.py)")
REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/~^-]*")
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
    return text + rng.choice(SEPARATORS) + rng.choice(DEFAULT_SEEDS)


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


def commands(seeds: Sequence[str], count: int, seed: int) -> list[str]:
    """The seeds, then ``count`` mutants of one to three stacked mutations."""
    rng = random.Random(seed)
    result = list(seeds)
    for _ in range(count):
        text = rng.choice(seeds)
        for _step in range(rng.randint(1, 3)):
            if text:
                text = rng.choice(MUTATIONS)(text, rng)
        result.append(text)
    return result


@contextlib.contextmanager
def _stdin(stream: io.TextIOWrapper) -> Iterator[None]:
    previous = sys.stdin
    sys.stdin = stream
    try:
        yield
    finally:
        sys.stdin = previous


def verdict(hook: ModuleType, command: str) -> int:
    """The hook's exit code for one command; a crash counts as a block."""
    payload = json.dumps({"tool_input": {"command": command}}).encode()
    stdin = io.TextIOWrapper(io.BytesIO(payload), encoding="utf-8")
    with (
        contextlib.redirect_stderr(io.StringIO()),
        contextlib.redirect_stdout(io.StringIO()),
        _stdin(stdin),
    ):
        try:
            return int(hook.main())
        except Exception:  # noqa: BLE001 - a failing hook blocks
            return BLOCK


def worker(hooks: Path, name: str) -> int:
    """Judge the commands on stdin with one hooks tree, in this process only."""
    tree = hooks.resolve()
    allowed = (ROOT / HOOKS, Path(tempfile.gettempdir()).resolve())
    if not re.fullmatch(r"guard_[a-z0-9_]+\.py", name) or not any(
        tree == root or tree.is_relative_to(root) for root in allowed
    ):
        print("the worker takes a guard of a hooks tree", file=sys.stderr)
        return 2
    sys.path.insert(0, str(tree))
    hooks = tree
    spec = importlib.util.spec_from_file_location(Path(name).stem, hooks / name)
    assert spec and spec.loader
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    inputs = json.loads(sys.stdin.read())
    print(json.dumps([verdict(hook, command) for command in inputs]))
    return 0


def verdicts(hooks: Path, name: str, inputs: list[str]) -> list[int]:
    """Each command's verdict from ``hooks/name`` in its own process."""
    done = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--worker", str(hooks), name],
        input=json.dumps(inputs),
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [int(item) for item in json.loads(done.stdout)]


@dataclass
class Comparison:
    checked: int = 0
    newly_allowed: list[str] = field(default_factory=list)
    newly_blocked: int = 0


def compare(old: Path, new: Path, name: str, inputs: Iterable[str]) -> Comparison:
    """Each command's verdict under the old and the new hooks tree."""
    items = list(inputs)
    result = Comparison(checked=len(items))
    pairs = zip(verdicts(old, name, items), verdicts(new, name, items), strict=True)
    for command, (before, after) in zip(items, pairs, strict=True):
        if before == BLOCK and after != BLOCK:
            result.newly_allowed.append(command)
        elif after == BLOCK and before != BLOCK:
            result.newly_blocked += 1
    return result


def base_hooks(base: str, directory: Path) -> Path:
    """The ``scripts/hooks`` tree at ``base``, extracted into ``directory``."""
    archive = subprocess.run(
        ["git", "archive", "--format=tar", base, HOOKS],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(directory, filter="data")
    return directory / HOOKS


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "hook", help="the guard, e.g. scripts/hooks/guard_resolve_thread.py"
    )
    parser.add_argument("--base", default="origin/develop")
    parser.add_argument("--count", type=int, default=60_000)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--seed-command", action="append", default=[], dest="seeds")
    args = parser.parse_args(argv)
    hook = HOOK_PATH.fullmatch(args.hook)
    if hook is None or REF.fullmatch(args.base) is None:
        parser.error(
            "the hook must be scripts/hooks/guard_*.py and the base a plain ref"
        )
    name = hook.group(1)
    with tempfile.TemporaryDirectory() as directory:
        old = base_hooks(args.base, Path(directory))
        inputs = commands(tuple(args.seeds) or DEFAULT_SEEDS, args.count, args.seed)
        result = compare(old, ROOT / HOOKS, name, inputs)
    print(
        f"{args.hook}: {result.checked} commands against {args.base}; "
        f"{len(result.newly_allowed)} newly allowed, {result.newly_blocked} newly blocked"
    )
    for command in result.newly_allowed[:10]:
        print(f"  newly allowed: {command!r}")
    return 1 if result.newly_allowed else 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--worker"]:
        sys.exit(worker(Path(sys.argv[2]), sys.argv[3]))
    sys.exit(main())

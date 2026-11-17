"""Run the local checks for a change: affected tests, compile, quality, JS tests.

    python scripts/check_changed.py [--base origin/develop] [--js] [--fix-format]

--fix-format first formats the changed Python files with the ratchet's Ruff and
names them; unchanged files, baselined ones included, are never touched (#3382).
Every step runs even after a failure. One line per step; for each failed step its
unittest FAIL/ERROR lines, the last lines of its output, and the path of a
temporary file with its full output. Node tests run with --js or when the diff against the
base touches a .js, .cjs or .mjs file. A changed workflow under
.github/workflows/ is linted with the pinned actionlint image in Docker, as CI
does (#3377); without Docker that step is reported as unrun. Exit 0: all
passed; 1: a step failed; 2: bad base ref or a tool is missing.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from gh_cli import CliError, git

ROOT = Path(__file__).resolve().parents[1]
JS_SUFFIXES = (".js", ".cjs", ".mjs")
WORKFLOWS = ".github/workflows/"
# The image and flags of .github/workflows/workflow-lint.yml (#3377); a test
# keeps them equal.
ACTIONLINT = [
    "rhysd/actionlint:1.7.12@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667",
    "-oneline",
    "-shellcheck=",
    "-pyflakes=",
    "-ignore",
    'unexpected key "queue" for "concurrency" section',
]
# Steps that need a tool a workstation may lack: without it they are reported
# as unrun, not as a failure.
OPTIONAL = {"actionlint"}
DOCKER_DOWN = re.compile(
    r"Cannot connect to the Docker daemon|error during connect"
    r"|failed to connect to the docker API",
    re.IGNORECASE,
)
TAIL_LINES = 20
# The unittest lines that name a failed test; they sit far above the tail.
FAILED_TEST = re.compile(r"^(?:FAIL|ERROR): ")


class MissingTool(Exception):
    """A step's program is not installed."""


class FormatFailed(Exception):
    """Ruff could not format a changed file (a syntax error mid-edit)."""


def quality_env() -> dict[str, str]:
    """The ratchet reads node_modules from PACECRAFT_NODE_MODULES, as in the
    pre-push hook, so a missing ESLint fails here with the npm ci hint instead
    of at the push (#2832); a value already set is kept. CI runs the ratchet
    without it and still skips ESLint."""
    modules = os.environ.get("PACECRAFT_NODE_MODULES") or str(ROOT / "node_modules")
    return {**os.environ, "PACECRAFT_NODE_MODULES": modules}


def run_step(
    argv: list[str], env: dict[str, str] | None = None
) -> tuple[bool, float, str]:
    """Run one step; return (passed, seconds, its combined output)."""
    started = time.monotonic()
    try:
        done = subprocess.run(
            argv,
            cwd=ROOT,
            env=env,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise MissingTool(f"{argv[0]} is not installed.") from exc
    seconds = time.monotonic() - started
    if (
        argv[0] == "docker"
        and done.returncode
        and DOCKER_DOWN.search(done.stdout or "")
    ):
        raise MissingTool("the Docker daemon is not reachable.")
    return done.returncode == 0, seconds, done.stdout or ""


def failure_report(name: str, output: str) -> list[str]:
    """The failed test names, the tail and where the full output was kept."""
    lines = output.strip().splitlines()
    named = [line for line in lines if FAILED_TEST.match(line)]
    report = [*named, *lines[-TAIL_LINES:]]
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            prefix=f"check-changed-{name}-",
            suffix=".log",
            delete=False,
        ) as log:
            log.write(output)
    except OSError:
        # A full or unwritable temp directory must not stop the later steps.
        return report
    return [*report, f"full output: {log.name}"]


def run_steps(
    steps: list[tuple[str, list[str]]], envs: dict[str, dict[str, str]] | None = None
) -> int:
    """Run all steps and print the report; 0 passed, 1 failed, 2 tool missing.

    `envs` gives a step by name its own environment; the others inherit ours.
    """
    failed = missing = False
    for name, argv in steps:
        try:
            passed, seconds, output = run_step(argv, (envs or {}).get(name))
        except MissingTool as exc:
            # unrun() first: every missing tool is reported, not just the first.
            missing = unrun(name, str(exc)) or missing
            continue
        print(f"{'ok' if passed else 'FAILED'} {name} ({seconds:.1f}s)")
        if not passed:
            failed = True
            for line in failure_report(name, output):
                print(f"  {line}")
    if missing:
        return 2
    return 1 if failed else 0


def unrun(name: str, reason: str) -> bool:
    """Report a step that could not run; True when that fails the whole run,
    False for an optional step, which is only reported as unrun."""
    if name in OPTIONAL:
        print(f"unrun {name}: {reason}")
        return False
    print(f"FAILED {name} (0.0s)\n  {reason}")
    return True


def changed_files(base: str, root: Path = ROOT) -> list[str]:
    """Committed, staged, unstaged and new files that differ from the base."""
    fork = git("merge-base", base, "HEAD", cwd=root).strip()
    changed = git("diff", "--name-only", fork, cwd=root).splitlines()
    changed += git("ls-files", "--others", "--exclude-standard", cwd=root).splitlines()
    return changed


def format_changed(base: str, root: Path = ROOT) -> list[str]:
    """Format the changed Python files that still exist with the ratchet's own
    Ruff, and return them; an unchanged file is never touched (#3382)."""
    files = sorted(
        {
            path
            for path in changed_files(base, root)
            if path.endswith(".py") and (root / path).is_file()
        }
    )
    if files:
        spec = importlib.util.spec_from_file_location(
            "quality_baseline", ROOT / "tools/quality_baseline.py"
        )
        assert spec and spec.loader
        quality = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(quality)
        try:
            done = subprocess.run(
                [*quality.tool_command("ruff"), "format", "--quiet", *files],
                cwd=root,
                check=False,
            )
        except FileNotFoundError as exc:
            raise MissingTool("ruff is not installed.") from exc
        if done.returncode:
            raise FormatFailed(
                f"ruff format failed (exit {done.returncode}); fix what it names above"
            )
    return files


def format_step(base: str) -> int:
    """Run --fix-format as a step and report it: 0 done, 1 Ruff failed (a syntax
    error mid-edit), 2 Ruff missing; the other steps run either way."""
    try:
        files = format_changed(base)
    except (FormatFailed, MissingTool) as exc:
        print(f"FAILED format (0.0s)\n  {exc}")
        return 2 if isinstance(exc, MissingTool) else 1
    plural = "" if len(files) == 1 else "s"
    print(
        f"formatted {len(files)} changed Python file{plural}: "
        + (", ".join(files) or "none")
    )
    return 0


def build_steps(
    base: str, js: bool, workflows: list[str] | None = None
) -> list[tuple[str, list[str]]]:
    python = sys.executable
    steps = [
        ("tests", [python, "tests/run_tests.py", "--changed", "--base", base]),
        (
            "compile",
            [python, "-m", "compileall", "-q", "server.py", "backend", "tests"],
        ),
        ("quality", [python, "tools/quality_baseline.py"]),
    ]
    if js:
        files = sorted(
            path.relative_to(ROOT).as_posix() for path in ROOT.glob("tests/*.cjs")
        )
        steps.append(("node-tests", ["node", "--test", *files]))
    if workflows:
        mount = f"{ROOT}:/repo"
        docker = ["docker", "run", "--rm", "-v", mount, "-w", "/repo"]
        steps.append(("actionlint", [*docker, *ACTIONLINT, *workflows]))
    return steps


def main(argv: list[str] | None = None) -> int:
    # Step output can hold characters a cp1252 console cannot encode (#2637);
    # like spec_validate.py and pr_state.py, write UTF-8 and never crash on it.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="origin/develop")
    parser.add_argument("--js", action="store_true", help="always run the Node tests")
    parser.add_argument(
        "--fix-format",
        action="store_true",
        help="first format the changed Python files, and only those (#3382)",
    )
    args = parser.parse_args(argv)
    try:
        changed = changed_files(args.base)
        formatted = format_step(args.base) if args.fix_format else 0
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    js = args.js or any(path.endswith(JS_SUFFIXES) for path in changed)
    workflows = sorted(
        {
            path
            for path in changed
            if path.startswith(WORKFLOWS)
            and path.endswith((".yml", ".yaml"))
            and (ROOT / path).is_file()
        }
    )
    steps = build_steps(args.base, js, workflows)
    return max(run_steps(steps, {"quality": quality_env()}), formatted)


if __name__ == "__main__":
    sys.exit(main())

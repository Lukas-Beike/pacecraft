"""Prepare Node, browser and Python tools, the app image and the shared Graphify environment."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

QUALITY_TOOLS = ("ruff", "mypy")
QUALITY_TOOLS_DIR = ".quality-tools"  # tools/quality_baseline.py runs them from here
# No Windows wheel; the app image installs it, and its tests skip without it.
DOCKER_ONLY = ("sqlcipher3-binary",)
LOCK_BLOCK = re.compile(r"(?m)^(?=[A-Za-z0-9][\w.-]*==)")
PIP_INPUTS = ("requirements.in", "requirements-dev.in", "requirements-graphify.in")
PYPI_URL = "https://pypi.org/pypi/{}/json"
CHECK_TIMEOUT = 5
NPM_TIMEOUT = 30


def quality_requirements(lock_text: str, tools: tuple[str, ...] = QUALITY_TOOLS) -> str:
    """Return the hash-locked blocks for the given quality tools, with mypy's
    dependencies when mypy is one of them."""
    blocks = LOCK_BLOCK.split(lock_text)[1:]
    selected = [
        block
        for block in blocks
        if block.partition("==")[0] in tools
        or ("mypy" in tools and re.search(r"(?m)^\s*#.*mypy", block))
    ]
    names = {block.partition("==")[0] for block in selected}
    if not set(tools) <= names:
        raise OSError("requirements-dev.txt does not pin the quality tools")
    return "".join(selected)


def runtime_requirements(lock_text: str) -> str:
    """The runtime lock's hash-locked blocks without the Docker-only ones, so the
    tests' interpreter has every runtime package the host has a wheel for (#4164)."""
    blocks = LOCK_BLOCK.split(lock_text)[1:]
    return "".join(b for b in blocks if b.partition("==")[0] not in DOCKER_ONLY)


def install_quality_tools(root: Path) -> int:
    """Ruff and mypy go into this worktree's own `.quality-tools`, which the quality
    ratchet runs: worktrees on different pins no longer reset one shared version
    (#3217). The runtime packages go to the interpreter, which runs the tests."""
    target = ["--target", str(root / QUALITY_TOOLS_DIR), "--upgrade"]
    dev = (root / "requirements-dev.txt").read_text(encoding="utf-8")
    runtime = (root / "requirements.txt").read_text(encoding="utf-8")
    print(f"Skipping Docker-only {', '.join(DOCKER_ONLY)}", flush=True)
    code = 0
    with tempfile.TemporaryDirectory(prefix="quality-tools-") as directory:
        for text, place in (
            (quality_requirements(dev), target),
            (runtime_requirements(runtime), []),
        ):
            lock = Path(directory) / "requirements.txt"
            lock.write_text(text, encoding="utf-8")
            command = [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--require-hashes",
                "--only-binary=:all:",
                "--disable-pip-version-check",
                "--no-input",
                *place,
                "-r",
                str(lock),
            ]
            # Both installs run: a failed Ruff/mypy install still gets the tests theirs.
            result = subprocess.run(command, cwd=root, check=False).returncode
            code = code or result
    return code


def build_app_image(root: Path) -> int:
    """Build this checkout's browser-test app image; skip without Docker."""
    docker = shutil.which("docker")
    if (
        not docker
        or subprocess.run(
            [docker, "info"], cwd=root, capture_output=True, check=False
        ).returncode
    ):
        print("App image skipped: Docker is not running")
        return 0
    tag = subprocess.run(
        [sys.executable, str(root / "scripts/run_browser_tests.py"), "--print-image"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if tag.returncode:
        return tag.returncode
    command = [docker, "build", "-q", "-t", tag.stdout.strip(), "."]
    return subprocess.run(command, cwd=root, check=False).returncode


def _release_key(version: str) -> tuple[int, ...] | None:
    """Return a comparable final-release key, or None for anything else."""
    if not re.fullmatch(r"\d+(?:\.\d+)*", version):
        return None
    parts = [int(part) for part in version.split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def _newer(latest: str, pinned: str) -> bool:
    """Report only final releases that compare higher; skip anything unclear."""
    latest_key, pinned_key = _release_key(latest), _release_key(pinned)
    return latest_key is not None and pinned_key is not None and latest_key > pinned_key


def pip_pins(root: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for name in PIP_INPUTS:
        text = (root / name).read_text(encoding="utf-8")
        for package, version in re.findall(
            r"(?m)^([A-Za-z0-9][\w.-]*)(?:\[[^\]]*\])?==([^\s;#]+)", text
        ):
            pins[package] = version
    return pins


def pypi_latest(package: str) -> str:
    with urllib.request.urlopen(
        PYPI_URL.format(package), timeout=CHECK_TIMEOUT
    ) as response:
        return str(json.load(response)["info"]["version"])


def npm_outdated(root: Path, npm: str) -> dict[str, tuple[str, str]]:
    result = subprocess.run(
        [npm, "outdated", "--json"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=NPM_TIMEOUT,
        check=False,
    )
    data = json.loads(result.stdout or "{}")
    if not isinstance(data, dict) or "error" in data:
        raise ValueError("npm outdated reported an error")
    return {
        name: (str(info.get("current")), str(info.get("latest")))
        for name, info in data.items()
        if isinstance(info, dict)
    }


def _pip_notes(root: Path) -> list[str]:
    try:
        pins = sorted(pip_pins(root).items())
    except OSError as exc:
        return [f"Python packages: check unavailable ({type(exc).__name__})"]

    def check(item: tuple[str, str]) -> str | None:
        package, pinned = item
        try:
            latest = pypi_latest(package)
        except Exception as exc:  # noqa: BLE001 - advisory only
            return f"{package}: check unavailable ({type(exc).__name__})"
        return f"{package} {pinned} -> {latest}" if _newer(latest, pinned) else None

    with ThreadPoolExecutor(max_workers=8) as pool:
        return [note for note in pool.map(check, pins) if note]


def _npm_notes(root: Path, npm: str | None) -> list[str]:
    if not npm:
        return ["npm packages: not checked (Node dependencies not prepared)"]
    try:
        outdated = npm_outdated(root, npm)
    except Exception as exc:  # noqa: BLE001 - advisory only
        return [f"npm packages: check unavailable ({type(exc).__name__})"]
    return [
        f"{name} {current} -> {latest}"
        for name, (current, latest) in sorted(outdated.items())
        if _newer(latest, current)
    ]


def newer_versions(root: Path, npm: str | None) -> list[str]:
    """Return pinned packages with a newer release; installs stay lock-based."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        npm_notes = pool.submit(_npm_notes, root, npm)
        return _pip_notes(root) + npm_notes.result()


def report_newer_versions(root: Path, npm: str | None) -> None:
    """Print available updates without changing lockfiles or the exit code."""
    print("Checking for newer package versions", flush=True)
    notes = newer_versions(root, npm)
    if not notes:
        print("All pinned packages are current")
        return
    print("Package versions (installs stay on the lockfiles):")
    for note in notes:
        print(f"  {note}")


def report_workflow_scope() -> None:
    """Name a GitHub CLI token without the `workflow` scope: GitHub refuses its
    pushes that touch .github/workflows/ (#3350). Only the scope list is read;
    the status output is never printed. A token without OAuth scopes
    (`none`: fine-grained or from the environment) is not judged."""
    gh = shutil.which("gh")
    if not gh:
        return
    try:
        status = subprocess.run(
            [gh, "auth", "status", "--active", "--hostname", "github.com"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=CHECK_TIMEOUT,
        )
    except OSError, subprocess.SubprocessError:
        return
    line = re.search(r"Token scopes: (.*)", status.stdout + status.stderr)
    scopes = {s.strip(" '\"") for s in line[1].split(",")} if line else {"none"}
    if "workflow" not in scopes and scopes != {"none"}:
        print(
            "GitHub CLI token lacks the `workflow` scope: pushes that touch "
            ".github/workflows/ fail until you run `gh auth refresh -s workflow`"
        )


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    node = shutil.which("node")
    worker = str(root / "scripts/graphify_local.py")
    playwright = str(root / "node_modules/@playwright/test/cli.js")
    steps = [
        ("Node dependencies", [npm, "ci", "--ignore-scripts"] if npm else None),
        (
            "Playwright browsers",
            [node, playwright, "install", "chromium"] if node else None,
        ),
        ("Graphify setup", [sys.executable, worker, "setup"]),
        ("Graphify update", [sys.executable, worker, "update", "--preparation"]),
        ("Python tools", lambda: install_quality_tools(root)),
        ("App image", lambda: build_app_image(root)),
    ]
    failed = False
    for label, command in steps:
        print(f"Preparing {label}", flush=True)
        try:
            if command is None:
                raise FileNotFoundError("npm/node unavailable")
            code = (
                command()
                if callable(command)
                else subprocess.run(command, cwd=root, check=False).returncode
            )
        except OSError as exc:
            print(f"{label} failed: {exc}", file=sys.stderr)
            code = 1
        if code:
            print(f"{label} failed (exit {code})", file=sys.stderr)
            failed = True
            if label == "Node dependencies":
                npm = None
    report_workflow_scope()
    report_newer_versions(root, npm)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())

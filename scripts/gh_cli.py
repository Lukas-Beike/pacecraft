"""Shared GitHub CLI and Git calls for the base-skill scripts.

The scripts pass every argument as a list (no shell), read text bodies from
files, and never print credentials: `gh` keeps its own authentication.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# Issue, PR and athlete text holds characters a cp1252 Windows console cannot
# encode; printing them crashed a script after its GitHub write (#3312). Every
# script that imports this helper prints UTF-8, unencodable output replaced.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# Token shapes that must never reach an issue, PR or comment body.
SECRET_SHAPES = re.compile(
    r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|(?<![A-Za-z0-9-])sk-[A-Za-z0-9_-]{20,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----|xox[abp]-[A-Za-z0-9-]{10,}"
)
MAX_BODY = 60_000
# The quality ratchet's finding lines; a refused push shows all of them (#2944).
RATCHET_LINES = ("NEW ", "OBSOLETE ", "Obsolete baseline")
RATCHET_SHOWN = 50


class CliError(RuntimeError):
    """A failed command or a refused input; the message is safe to print."""


def _push_refusal(done: subprocess.CompletedProcess, lines: list[str]) -> list[str]:
    """Every ratchet line from either stream (capped), then the hook's last lines.

    Git may leave a pre-push hook's stdout alone, where the ratchet prints.
    """
    both = [*(done.stdout or "").splitlines(), *(done.stderr or "").splitlines()]
    ratchet = [x for x in both if x.startswith(RATCHET_LINES)]
    hidden = len(ratchet) - RATCHET_SHOWN
    more = [f"... and {hidden} more"] if hidden > 0 else []
    skip = ("error", "To ", " ! ", "hint:", *RATCHET_LINES)
    hook = [x for x in lines if not x.startswith(skip)]
    return [*ratchet[:RATCHET_SHOWN], *more, *hook[-5:]]


def run(
    program: str,
    *args: str,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    try:
        done = subprocess.run(
            [program, *args],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=cwd,
            env=env,
        )
    except FileNotFoundError as exc:
        raise CliError(f"{program} is not installed.") from exc
    if done.returncode != 0:
        lines = (done.stderr or done.stdout or "").strip().splitlines()
        # git prints progress ("To <remote>") before the line that says why.
        reason = next(
            (x for x in lines if x.startswith(("error", "fatal", " ! "))), None
        )
        detail = reason or (lines[0] if lines else "no output")
        if "push" in args:
            # A refusing pre-push hook explains itself before git's generic line.
            detail = "\n".join([detail, *_push_refusal(done, lines)])
        raise CliError(f"{program} {' '.join(args[:2])} failed: {detail}")
    return done.stdout


def gh(*args: str) -> str:
    return run("gh", *args)


def gh_json(*args: str) -> Any:
    try:
        return json.loads(gh(*args) or "null")
    except json.JSONDecodeError as exc:
        raise CliError(f"gh {' '.join(args[:2])} returned invalid JSON.") from exc


def git(
    *args: str, cwd: Path | None = None, env: Mapping[str, str] | None = None
) -> str:
    return run("git", *args, cwd=cwd, env=env)


def read_body(path: str) -> str:
    """Read a Markdown body from a file and refuse empty, huge or secret-shaped text."""
    try:
        body = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise CliError(f"cannot read body file {path}: {exc.strerror}") from exc
    if not body.strip():
        raise CliError(f"body file {path} is empty.")
    if len(body) > MAX_BODY:
        raise CliError(f"body file {path} exceeds {MAX_BODY} characters.")
    if SECRET_SHAPES.search(body):
        raise CliError(
            f"body file {path} contains a credential-shaped string; refused."
        )
    return body


def positive_int(text: str) -> int:
    value = int(text.lstrip("#"), 10)
    if value <= 0:
        raise ValueError(text)
    return value

"""Shared GitHub CLI and Git calls for the base-skill scripts.

The scripts pass every argument as a list (no shell), read text bodies from
files, and never print credentials: `gh` keeps its own authentication.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
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
FETCH_ATTEMPTS = 3
TRANSIENT_FETCH = re.compile(
    r"cannot lock ref '[^']+': is at \w+ but expected|\.lock': File exists"
    r"|Operation too slow|timed out"
    # A transfer cut short beside a parallel fetch (#3798).
    r"|did not send all necessary objects"
)


# A failed call's detail is cut to this length, HTML tags removed: gh puts a whole
# GitHub error page (55 KB) on its one error line (#3393).
MAX_DETAIL = 300
# A tag ends at its ">" or before the next "<", so a page full of unclosed "<" is
# read once, not again from every "<" (Sonar S8786).
HTML_TAG = re.compile(r"<[^<>]*>")
HTTP_STATUS = re.compile(r"HTTP \d{3}")
# A transient GitHub failure: a read is tried once more after a pause (#3393, the
# family #3345 retries in release follow). A 4xx stays terminal, and so does any
# write, which a 5xx may have applied already: it is never sent twice.
TRANSIENT_GH = re.compile(
    r"HTTP 5\d\d|No server is currently available|timed out|timeout"
    # A dropped connection, as in a short network outage (#4059).
    r"|error connecting to",
    re.IGNORECASE,
)
GH_RETRY_SECONDS = 3
GH_WRITE_FLAGS = frozenset(
    {"-X", "--method", "-f", "-F", "--field", "--raw-field", "--input"}
)
GH_READ_COMMANDS = frozenset({"view", "list", "status", "checks", "diff", "search"})


class CliError(RuntimeError):
    """A failed command or a refused input; the message is safe to print.
    ``stderr`` keeps a failed command's whole error output, for retry checks
    only (None for a refused input)."""

    stderr: str | None = None


def _short_detail(detail: str) -> str:
    """The detail without HTML, at most MAX_DETAIL characters, its HTTP status kept."""
    text = " ".join(HTML_TAG.sub(" ", detail).split())
    if len(text) <= MAX_DETAIL:
        return text
    status = HTTP_STATUS.search(text)
    kept = text[:MAX_DETAIL].rstrip() + " …"
    return (
        f"{kept} ({status.group()})" if status and status.group() not in kept else kept
    )


def _retryable(program: str, args: tuple[str, ...], output: str) -> bool:
    """A gh read that failed transiently; writes and 4xx are never retried."""
    if program != "gh" or not TRANSIENT_GH.search(output) or not args:
        return False
    if args[0] == "api":
        return not GH_WRITE_FLAGS.intersection(args)
    return len(args) > 1 and args[1] in GH_READ_COMMANDS


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


def _attempt(
    program: str,
    args: tuple[str, ...],
    cwd: Path | None,
    env: Mapping[str, str] | None,
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
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


def run(
    program: str,
    *args: str,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    done = _attempt(program, args, cwd, env)
    if done.returncode != 0 and _retryable(
        program, args, (done.stderr or "") + (done.stdout or "")
    ):
        time.sleep(GH_RETRY_SECONDS)
        done = _attempt(program, args, cwd, env)
    if done.returncode != 0:
        lines = (done.stderr or done.stdout or "").strip().splitlines()
        # git prints progress ("To <remote>") before the line that says why.
        reason = next(
            (x for x in lines if x.startswith(("error", "fatal", " ! "))), None
        )
        detail = _short_detail(reason or (lines[0] if lines else "no output"))
        if "push" in args:
            # A refusing pre-push hook explains itself before git's generic line.
            detail = "\n".join([detail, *_push_refusal(done, lines)])
        error = CliError(f"{program} {' '.join(args[:2])} failed: {detail}")
        error.stderr = done.stderr or ""
        raise error
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


def fetch(*refs: str, cwd: Path | None = None) -> None:
    """Fetch origin; retry while a parallel worktree holds a lock (#2828, #3378).

    Worktrees share one .git: a ref moved under a parallel fetch, a packed-refs
    lock file, a stalled transfer (aborted below 1 KB/s for 60 s), an
    incomplete transfer or a failure without a message is retried.
    A ref name conflict and every other failure raise at once, so no caller
    goes on with a stale ref.
    """
    env = {
        **os.environ,
        "LC_ALL": "C",  # English messages for the checks below
        "GIT_HTTP_LOW_SPEED_LIMIT": "1000",
        "GIT_HTTP_LOW_SPEED_TIME": "60",
    }
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            git("fetch", "origin", *refs, cwd=cwd, env=env)
            return
        except CliError as exc:
            # Every line of git's output counts, and an empty one too (#3798).
            output = str(exc) if exc.stderr is None else exc.stderr
            transient = TRANSIENT_FETCH.search(output) or not output.strip()
            if not transient or attempt == FETCH_ATTEMPTS:
                raise
            print(
                f"fetch failed transiently (attempt {attempt}); retrying",
                file=sys.stderr,
            )
            time.sleep(attempt)


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

"""Measure a parallel view burst on the disposable fixture, with its spread (#3052).

Logs in once with the public fixture password, then runs ``--rounds`` rounds of
the ten view reads of #2966: first one after another, then all at once. It
prints per read and per burst the median, minimum and maximum, so a second run
can be compared against the spread instead of a single number. Without
``--right-after-start`` it first waits until the diagnostics report the
all-time curve build ``ready`` and no section ``running`` (steady state): a
backfill stage still running would be measured with the change (#3172). One login only: every login is
a write that empties the summary memo.

Only a loopback fixture is measured (default ``http://127.0.0.1:8091``, the
demo container); start it with ``scripts/demo-container.ps1`` and the
production profile ``-HeavyHistory 850 -HeavyWalks 2950 -HeavyYears 6``.

    python scripts/view_burst.py --rounds 5
    python scripts/view_burst.py --right-after-start --rounds 2
    python scripts/view_burst.py --routes-file routes.txt --clients 4

A routes file names one API path per line (``#`` starts a comment), so no
bare ``/api/...`` argument meets Git Bash's path conversion;
``{activity_id}`` is filled with the newest fixture session. Every client
logs in before the measurement starts, never during it (#3078).
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import math
import statistics
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

FIXTURE_PASSWORD = (
    "e2e-fixture-password-1234"  # the disposable fixture's public password
)
READS = (
    "/api/bootstrap",
    "/api/chat/history?limit=100",
    "/api/analysis/training-records",
    "/api/performance",
    "/api/analysis/power-profiles",
    "/api/context-preview",
    "/api/activities?limit=100",
    "/api/plan",
    "/api/library?limit=100",
    "/api/logs?limit=250",
)
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def loopback_url(value: str) -> str:
    """The base URL, refused unless it names a loopback host (the fixture)."""
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in LOOPBACK:
        raise argparse.ArgumentTypeError(
            "only a loopback fixture is measured (http://127.0.0.1:<port>)"
        )
    return value.rstrip("/")


def spread(values: Sequence[float]) -> dict[str, float]:
    """Count, mean, median, nearest-rank p95, minimum and maximum (ms rounding)."""
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "mean": round(statistics.fmean(ordered), 3),
        "median": round(statistics.median(ordered), 3),
        "p95": round(ordered[math.ceil(0.95 * len(ordered)) - 1], 3),
        "min": round(min(values), 3),
        "max": round(max(values), 3),
    }


class Client:
    def __init__(self, base: str, timeout: float) -> None:
        self._base, self._timeout = base, timeout
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def login(self, password: str) -> None:
        body = json.dumps({"password": password}).encode()
        request = urllib.request.Request(
            self._base + "/api/login",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self._opener.open(request, timeout=self._timeout) as response:
            response.read()

    def get(self, path: str) -> Any:
        with self._opener.open(self._base + path, timeout=self._timeout) as response:
            return json.loads(response.read() or b"null")

    def timed(self, path: str) -> float:
        started = time.perf_counter()
        self.get(path)
        return time.perf_counter() - started


def wait_until_ready(
    client: Client,
    limit_s: float,
    clock: Callable[[], float],
    pause: Callable[[float], None] = time.sleep,
) -> float:
    """Start the all-time build if needed and wait for ``ready`` while no
    diagnostics section reports ``running`` (the backfill stages); returns the wait."""
    started = clock()
    client.get("/api/analysis/power-profiles")  # the first all-time read starts it
    shown: list[str] = []
    while True:
        diagnostics = client.get("/api/diagnostics")
        status = (diagnostics.get("curve_build") or {}).get("status")
        running = sorted(
            name
            for name, section in diagnostics.items()
            if isinstance(section, dict) and section.get("status") == "running"
        )
        if status == "ready" and not running:
            return clock() - started
        if status == "failed" or clock() - started > limit_s:
            raise SystemExit(
                f"not steady after {limit_s:.0f} s: curve build {status}, "
                f"running {', '.join(running) or 'nothing'}"
            )
        if running and running != shown:
            print(f"waiting for {', '.join(running)}", file=sys.stderr)
            shown = running
        pause(5)


def read_routes(path: str | None, client: Client) -> tuple[str, ...]:
    """The default ten view reads, or the routes file with ``{activity_id}`` filled."""
    if path is None:
        return READS
    # Only a file below the working directory: the path is a CLI argument.
    base = Path.cwd().resolve()
    source = (base / path).resolve()
    if not source.is_relative_to(base) or not source.is_file():
        raise SystemExit("the routes file must be a file below the working directory")
    lines = source.read_text(encoding="utf-8").splitlines()
    routes = [
        line.strip() for line in lines if line.strip() and not line.startswith("#")
    ]
    if not routes or not all(route.startswith("/api/") for route in routes):
        raise SystemExit("the routes file needs one /api/ path per line")
    if len(set(routes)) != len(routes):
        raise SystemExit("the routes file lists a path twice")
    if any("{activity_id}" in route for route in routes):
        newest = client.get("/api/activities?limit=1").get("activities") or []
        if not newest:
            raise SystemExit("{activity_id} needs a session in the fixture")
        session = urllib.parse.quote(str(newest[0]["id"]), safe="")
        routes = [route.replace("{activity_id}", session) for route in routes]
    return tuple(routes)


def measure(
    clients: Sequence[Client], rounds: int, reads: Sequence[str] = READS
) -> dict[str, Any]:
    """Each round: the reads one by one, then all at once spread over the clients."""
    sequential: dict[str, list[float]] = {path: [] for path in reads}
    parallel: dict[str, list[float]] = {path: [] for path in reads}
    sums, bursts = [], []
    pairs = [(clients[index % len(clients)], path) for index, path in enumerate(reads)]
    with ThreadPoolExecutor(len(reads)) as pool:
        for _ in range(rounds):
            one_by_one = [clients[0].timed(path) for path in reads]
            all_at_once = list(pool.map(lambda pair: pair[0].timed(pair[1]), pairs))
            for path, alone, together in zip(reads, one_by_one, all_at_once):
                sequential[path].append(alone)
                parallel[path].append(together)
            sums.append(sum(one_by_one))
            bursts.append(max(all_at_once))
    return {
        "rounds": rounds,
        "clients": len(clients),
        "sequential_sum_s": spread(sums),
        "burst_max_s": spread(bursts),
        "burst_to_sum": spread([b / s for b, s in zip(bursts, sums)]),
        "reads": {
            path: {
                "sequential_s": spread(sequential[path]),
                "parallel_s": spread(parallel[path]),
            }
            for path in reads
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--base-url", type=loopback_url, default="http://127.0.0.1:8091"
    )
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument(
        "--right-after-start",
        action="store_true",
        help="measure at once instead of waiting for the curve build",
    )
    parser.add_argument("--ready-timeout", type=float, default=900.0)
    parser.add_argument("--routes-file", help="one /api/ path per line")
    parser.add_argument("--clients", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args(argv)
    if args.rounds < 1 or args.clients < 1:
        parser.error("--rounds and --clients must be at least 1")
    clients = [Client(args.base_url, args.timeout) for _ in range(args.clients)]
    for client in clients:  # every login, a write, before any measurement
        client.login(FIXTURE_PASSWORD)
    client = clients[0]
    reads = read_routes(args.routes_file, client)
    waited = (
        None
        if args.right_after_start
        else wait_until_ready(client, args.ready_timeout, time.monotonic)
    )
    result = measure(clients, args.rounds, reads)
    result["waited_for_ready_s"] = None if waited is None else round(waited, 1)
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

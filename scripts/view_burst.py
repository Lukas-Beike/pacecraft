"""Measure a parallel view burst on the disposable fixture, with its spread (#3052).

Logs in once with the public fixture password, then runs ``--rounds`` rounds of
the ten view reads of #2966: first one after another, then all at once. It
prints per read and per burst the median, minimum and maximum, so a second run
can be compared against the spread instead of a single number. Without
``--right-after-start`` it first waits until the diagnostics report the
all-time curve build ``ready`` (steady state). One login only: every login is
a write that empties the summary memo.

Only a loopback fixture is measured (default ``http://127.0.0.1:8091``, the
demo container); start it with ``scripts/demo-container.ps1`` and the
production profile ``-HeavyHistory 850 -HeavyWalks 2950 -HeavyYears 6``.

    python scripts/view_burst.py --rounds 5
    python scripts/view_burst.py --right-after-start --rounds 2
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import statistics
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
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
    """Median, minimum and maximum, rounded to milliseconds."""
    return {
        "median": round(statistics.median(values), 3),
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
    client: Client, limit_s: float, clock: Callable[[], float]
) -> float:
    """Start the all-time build if needed and wait for ``ready``; returns the wait."""
    started = clock()
    client.get("/api/analysis/power-profiles")  # the first all-time read starts it
    while True:
        status = (client.get("/api/diagnostics").get("curve_build") or {}).get("status")
        if status == "ready":
            return clock() - started
        if status == "failed" or clock() - started > limit_s:
            raise SystemExit(f"curve build not ready ({status}) after {limit_s:.0f} s")
        time.sleep(5)


def measure(client: Client, rounds: int) -> dict[str, Any]:
    sequential: dict[str, list[float]] = {path: [] for path in READS}
    parallel: dict[str, list[float]] = {path: [] for path in READS}
    sums, bursts = [], []
    with ThreadPoolExecutor(len(READS)) as pool:
        for _ in range(rounds):
            one_by_one = [client.timed(path) for path in READS]
            all_at_once = list(pool.map(client.timed, READS))
            for path, alone, together in zip(READS, one_by_one, all_at_once):
                sequential[path].append(alone)
                parallel[path].append(together)
            sums.append(sum(one_by_one))
            bursts.append(max(all_at_once))
    return {
        "rounds": rounds,
        "sequential_sum_s": spread(sums),
        "burst_max_s": spread(bursts),
        "burst_to_sum": spread([b / s for b, s in zip(bursts, sums)]),
        "reads": {
            path: {
                "sequential_s": spread(sequential[path]),
                "parallel_s": spread(parallel[path]),
            }
            for path in READS
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
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args(argv)
    if args.rounds < 1:
        parser.error("--rounds must be at least 1")
    client = Client(args.base_url, args.timeout)
    client.login(FIXTURE_PASSWORD)
    waited = (
        None
        if args.right_after_start
        else wait_until_ready(client, args.ready_timeout, time.monotonic)
    )
    result = measure(client, args.rounds)
    result["waited_for_ready_s"] = None if waited is None else round(waited, 1)
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""The gate that runs heavy view reads one at a time (#3029)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any


class HeavyReadGate:
    """One heavy view read at a time, taken before the read opens a lease (#3029).

    Parallel reads over a long history share the interpreter, so each one
    stretched to the sum of all; one after another they finish sooner, and a
    waiting request holds no reader slot. A read waits at most ``wait_s`` and
    then runs anyway, so a stuck read never blocks the views. Counts only:
    waits over 1 s, reads that ran after the wait, the longest wait.
    """

    def __init__(
        self, wait_s: float = 5.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._wait_s, self._clock = wait_s, clock
        self._slot = threading.Lock()
        self._state = threading.Lock()
        self._slow_waits = self._timeouts = 0
        self._longest_wait_s = 0.0

    @contextmanager
    def held(self) -> Iterator[None]:
        acquired = self._slot.acquire(blocking=False)
        if not acquired:  # only a real wait is timed
            started = self._clock()
            acquired = self._slot.acquire(timeout=self._wait_s)
            waited = self._clock() - started
            with self._state:
                self._longest_wait_s = max(self._longest_wait_s, waited)
                self._slow_waits += waited > 1.0
                self._timeouts += not acquired
        try:
            yield
        finally:
            if acquired:
                self._slot.release()

    def summary(self) -> dict[str, Any]:
        with self._state:
            return {
                "waits_over_1s": self._slow_waits,
                "timeouts": self._timeouts,
                "longest_wait_s": round(self._longest_wait_s, 3),
            }

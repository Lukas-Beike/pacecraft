"""Re-check every product at once in the background (#3299).

Each product goes through the single "Aktualisieren" rule
(``NutritionMealLibraryService.refresh_database_food``): only empty fields
fill, known and own values stay, and own products record their change. One run
at a time; a start while one runs returns its progress. Products are re-checked
one after another with a pause towards the food sources, which grows after a
rate limit or a server error. One product's failure is counted and the run goes
on. After ``STOP_AFTER_TIMEOUTS`` Open Food Facts timeouts in a row the run
stops with ``stopped`` ``food_database_timeout``; the products not reached
keep their values (#3475). Each product holds the maintenance gate on its
own, so a restore never waits for a whole run. The progress and the last
receipt live in memory.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from typing import Any, Final, Protocol

from backend.db.repositories import NUTRIENT_COLUMNS
from backend.errors import AppError

_LOGGER = logging.getLogger("intervals_coach")
PACE_SECONDS: Final = 1.5
# After a rate limit (429) or a server error (5xx) the pause doubles up to this.
MAX_BACKOFF_SECONDS: Final = 60.0
# Open Food Facts timeouts in a row that stop a run (#3475).
STOP_AFTER_TIMEOUTS: Final = 3
_COUNTS: Final = ("filled", "unchanged", "skipped", "failed")


class OperationGate(Protocol):
    def operation(self) -> AbstractContextManager[Any]: ...


def refresh_targets(products: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """The products a run re-checks, and the number skipped without an EAN.

    An own product needs an EAN. A stored database food whose EAN an own
    product carries is re-checked through that product, so it is not listed
    twice.
    """
    own = [item for item in products if item.get("origin") == "own"]
    targets = [item for item in own if item.get("barcode")]
    covered = {item["barcode"] for item in targets}
    targets += [
        item
        for item in products
        if item.get("origin") != "own" and item.get("barcode") not in covered
    ]
    return targets, len(own) - sum(1 for item in own if item.get("barcode"))


def _missing(result: Mapping[str, Any]) -> int | None:
    product = result.get("product") or result.get("food")
    if not isinstance(product, Mapping):
        return None
    if "missing_fields" in product:
        return len(product["missing_fields"])
    values = product.get("per_100", product)
    return sum(1 for key in NUTRIENT_COLUMNS if values.get(key) is None)


class ProductRefreshRunner:
    def __init__(
        self,
        products: Callable[[], list[dict[str, Any]]],
        refresh: Callable[[str], dict[str, Any]],
        maintenance_gate: Callable[[], OperationGate],
        utc_now: Callable[[], str],
        *,
        pace_seconds: float = PACE_SECONDS,
        thread_factory: Callable[..., threading.Thread] | None = None,
        source_timeouts: Callable[[], int] = lambda: 0,
    ) -> None:
        self._products = products
        self._refresh_product = refresh
        self._maintenance_gate = maintenance_gate
        self._utc_now = utc_now
        self._pace_seconds = pace_seconds
        self._thread_factory = thread_factory or threading.Thread
        self._source_timeouts = source_timeouts
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = {"running": False}

    def state(self) -> dict[str, Any]:
        """``running``, ``total``, ``done`` and the counts of the current or last run."""
        with self._lock:
            return dict(self._state)

    def start(self) -> dict[str, Any]:
        """Start a run unless one runs; return its state either way."""
        with self._lock:
            if self._state["running"] or self._stop_event.is_set():
                return dict(self._state)
            targets, skipped = refresh_targets(self._products())
            self._state = {
                "running": True,
                "total": len(targets) + skipped,
                "done": skipped,
                "started_at": self._utc_now(),
                **dict.fromkeys(_COUNTS, 0),
                "skipped": skipped,
            }
            self._thread = self._thread_factory(
                target=self._run, args=(targets,), name="product-refresh", daemon=True
            )
            self._thread.start()
            return dict(self._state)

    def stop(self) -> None:
        self._stop_event.set()

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _run(self, targets: list[dict[str, Any]]) -> None:
        pause = self._pace_seconds
        # Products in a row whose own refresh timed out; the shared client count
        # may be stale from an earlier run or moved by other lookups (Gitar #3481).
        streak = 0
        try:
            for index, product in enumerate(targets):
                if index and self._stop_event.wait(pause):
                    break
                before = self._source_timeouts()
                outcome, pause = self._refresh(product, pause)
                with self._lock:
                    self._state[outcome] += 1
                    self._state["done"] += 1
                streak = streak + 1 if self._source_timeouts() > before else 0
                if streak >= STOP_AFTER_TIMEOUTS:
                    with self._lock:
                        self._state["stopped"] = "food_database_timeout"
                    break
        finally:
            with self._lock:
                self._state["running"] = False
                self._state["finished_at"] = self._utc_now()

    def _refresh(self, product: Mapping[str, Any], pause: float) -> tuple[str, float]:
        """One product's outcome and the pause before the next one."""
        try:
            with self._maintenance_gate().operation():
                result = self._refresh_product(str(product["id"]))
        except AppError as error:
            if error.status == 429 or error.status >= 500:
                return "failed", min(
                    max(pause, self._pace_seconds) * 2, MAX_BACKOFF_SECONDS
                )
            return "failed", self._pace_seconds
        except Exception as error:  # noqa: BLE001 - one product never stops the run
            _LOGGER.warning("product refresh failed: %s", type(error).__name__)
            return "failed", self._pace_seconds
        after = _missing(result)
        before = len(product.get("missing_fields") or ())
        filled = after is not None and after < before
        return ("filled" if filled else "unchanged"), self._pace_seconds

"""Re-checking every product at once (#3299): mocked food sources only."""

from __future__ import annotations

import sqlite3
import threading
import unittest
from contextlib import nullcontext
from datetime import UTC, datetime
from typing import Any, cast

from nutrition_service_support import build_nutrition_services
from support import temp_database

from backend.db.repositories import FoodDatabaseEntryRepository
from backend.errors import AppError
from backend.nutrition.food_database import FoodDatabaseService
from backend.nutrition.food_mirror import FoodDatabaseMirror
from backend.nutrition.product_refresh import (
    MAX_BACKOFF_SECONDS,
    ProductRefreshRunner,
    refresh_targets,
)

NOW = "2026-11-16T12:00:00+00:00"


def food(code: str, **per_100: Any) -> dict[str, Any]:
    return {
        "id": f"off:{code}",
        "name": f"Synthetic {code}",
        "brands": "Synthetic",
        "basis_amount": 100,
        "basis_unit": "g",
        "per_100": per_100,
    }


class FakeOpenFoodFacts:
    """Answers re-checks from a fixed table; a missing code is a server error."""

    def __init__(self, fresh: dict[str, dict[str, Any]]) -> None:
        self.fresh = fresh
        self.calls: list[str] = []

    def product(self, code: str) -> dict[str, Any]:
        self.calls.append(code)
        if code not in self.fresh:
            raise AppError(503, "Open Food Facts nicht erreichbar.")
        return self.fresh[code]


class Gate:
    def operation(self) -> Any:
        return nullcontext()


def runner(products: Any, refresh: Any, **options: Any) -> ProductRefreshRunner:
    return ProductRefreshRunner(
        products, refresh, Gate, lambda: NOW, pace_seconds=0, **options
    )


def finished(run: ProductRefreshRunner) -> dict[str, Any]:
    run.join(timeout=5)
    state = run.state()
    assert not state["running"]
    return state


class RefreshAllServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = temp_database(
            self, "refresh_all.db", row_factory=sqlite3.Row, schema=True
        )
        self.off = FakeOpenFoodFacts(
            {
                "5711953068011": food(
                    "5711953068011", kcal=95, carbs_g=10, protein_g=5, fat_g=1.2
                ),
                "222": food("222", kcal=50, carbs_g=2, protein_g=3, fat_g=2.0),
                "444": food("444", kcal=40, carbs_g=1, protein_g=1, fat_g=1),
            }
        )
        mirror = FoodDatabaseMirror(lambda: self.manager, lambda: NOW)
        _, self.library = build_nutrition_services(
            self.manager,
            lambda: NOW,
            lambda: datetime(2026, 11, 16, 12, 0, tzinfo=UTC),
            food_database=FoodDatabaseService(off=cast(Any, self.off), mirror=mirror),
        )
        catalog = self.library.product_catalog
        with self.manager.unit_of_work() as db:
            entries = FoodDatabaseEntryRepository()
            entries.upsert(
                db,
                food("5711953068011", kcal=90, carbs_g=10, protein_g=5),
                "2026-09-01",
            )
            entries.upsert(
                db, food("222", kcal=50, carbs_g=2, protein_g=3), "2026-09-01"
            )
            entries.upsert(
                db, food("333", kcal=30, carbs_g=1, protein_g=1), "2026-09-01"
            )
            entries.upsert(
                db, food("444", kcal=40, carbs_g=1, protein_g=1, fat_g=1), "2026-09-01"
            )
        base = {"basis_amount": 100, "basis_unit": "g", "source": "manual"}
        # A known own value (kcal 100) and a gap (fat) the re-check may fill.
        self.own = catalog.save_product(
            {
                **base,
                "name": "Eigenes",
                "barcode": "5711953068011",
                "kcal": 100,
                "carbs_g": 10,
                "protein_g": 5,
            }
        )
        catalog.save_product(
            {**base, "name": "Ohne EAN", "kcal": 20, "carbs_g": 1, "protein_g": 1}
        )
        self.refresh_run = runner(
            catalog.list_all_products, self.library.refresh_database_food
        )

    def test_one_run_fills_gaps_skips_and_counts_a_failure(self) -> None:
        started = self.refresh_run.start()
        self.assertTrue(started["running"])
        state = finished(self.refresh_run)

        # Own 5711953068011 (its stored food goes with it), foods 222, 333 and 444.
        self.assertEqual(
            {
                "total": 5,
                "done": 5,
                "filled": 2,
                "unchanged": 1,
                "skipped": 1,
                "failed": 1,
            },
            {
                key: state[key]
                for key in ("total", "done", "filled", "unchanged", "skipped", "failed")
            },
        )
        own = self.library.product_catalog.get_product(self.own["id"])
        self.assertEqual((100, 1.2), (own["kcal"], own["fat_g"]))
        # 333 failed and the run went on to 444; each source code was asked once.
        self.assertEqual(["222", "333", "444", "5711953068011"], sorted(self.off.calls))
        self.assertEqual(NOW, state["finished_at"])

    def test_the_run_lists_every_product_beyond_the_list_cap(self) -> None:
        # Gitar #3307: the list shows at most 1000; the run must see them all.
        catalog = self.library.product_catalog
        before = len(catalog.list_all_products())
        with self.manager.unit_of_work() as db:
            db.executemany(
                "INSERT INTO nutrition_products(id, name, basis_amount, basis_unit, "
                "source, created_at, updated_at) VALUES (?, ?, 100, 'g', 'manual', "
                "?, ?)",
                [(f"bulk-{n}", f"Bulk {n:04d}", NOW, NOW) for n in range(1000)],
            )
        self.assertEqual(1000, len(catalog.list_all_products()))
        self.assertEqual(before + 1000, len(catalog.list_all_products(limit=None)))


class RefreshAllRunnerTests(unittest.TestCase):
    def test_targets_skip_own_products_without_an_ean_and_covered_foods(self) -> None:
        products: list[dict[str, Any]] = [
            {"id": "a", "origin": "own", "barcode": "1"},
            {"id": "b", "origin": "own", "barcode": None},
            {"id": "off:1", "origin": "open_food_facts", "barcode": "1"},
            {"id": "off:2", "origin": "open_food_facts", "barcode": "2"},
        ]
        targets, skipped = refresh_targets(products)
        self.assertEqual(["a", "off:2"], [item["id"] for item in targets])
        self.assertEqual(1, skipped)

    def test_a_second_start_while_running_returns_the_running_job(self) -> None:
        release = threading.Event()
        calls: list[str] = []

        def refresh(product_id: str) -> dict[str, Any]:
            calls.append(product_id)
            release.wait(5)
            return {"food": {"missing_fields": []}}

        products = [{"id": "off:1", "origin": "off", "missing_fields": []}]
        run = runner(lambda: products, refresh)
        first = run.start()
        second = run.start()
        self.assertEqual(first["started_at"], second["started_at"])
        self.assertTrue(second["running"])
        release.set()
        self.assertEqual(1, finished(run)["unchanged"])
        self.assertEqual(["off:1"], calls)

    def test_progress_is_reported_and_the_pace_backs_off_after_a_rate_limit(
        self,
    ) -> None:
        pauses: list[float] = []
        seen: list[int] = []
        products = [{"id": f"off:{n}", "origin": "off"} for n in range(4)]

        def refresh(product_id: str) -> dict[str, Any]:
            seen.append(run.state()["done"])
            if product_id in {"off:0", "off:1"}:
                raise AppError(429, "Zu viele Anfragen.")
            return {"food": {"missing_fields": []}}

        run = runner(lambda: products, refresh)
        run._pace_seconds = 0.25
        wait = run._stop_event.wait

        def recorded(timeout: float | None = None) -> bool:
            pauses.append(float(timeout or 0))
            return wait(0)

        run._stop_event.wait = recorded  # type: ignore[method-assign]
        state = run.start()
        self.assertEqual((4, 0), (state["total"], state["done"]))
        state = finished(run)
        self.assertEqual([0, 1, 2, 3], seen)
        self.assertEqual((2, 2), (state["failed"], state["unchanged"]))
        # Doubles after each rate limit, back to the pace after a success.
        self.assertEqual([0.5, 1.0, 0.25], pauses)
        self.assertLessEqual(max(pauses), MAX_BACKOFF_SECONDS)

    def test_open_food_facts_timeouts_in_a_row_stop_the_run(self) -> None:
        # #3475: OFF answered after 10 s timeouts only; 3 in a row stop the run
        # and the products not reached are not asked.
        products = [{"id": f"off:{n}", "origin": "off"} for n in range(6)]
        calls: list[str] = []
        timeouts = [0]

        def refresh(product_id: str) -> dict[str, Any]:
            calls.append(product_id)
            if product_id == "off:0":
                return {"food": {"missing_fields": []}}
            timeouts[0] += 1
            raise AppError(502, "Lebensmitteldatenbank nicht erreichbar.")

        run = runner(lambda: products, refresh, source_timeouts=lambda: timeouts[0])
        run.start()
        state = finished(run)
        self.assertEqual(["off:0", "off:1", "off:2", "off:3"], calls)
        self.assertEqual(
            ("food_database_timeout", 4, 6, 1, 3),
            (
                state["stopped"],
                state["done"],
                state["total"],
                state["unchanged"],
                state["failed"],
            ),
        )
        # A full run has no stop reason.
        ok = runner(lambda: products[:1], lambda _id: {"food": {"missing_fields": []}})
        ok.start()
        self.assertNotIn("stopped", finished(ok))

    def test_a_stopped_runner_starts_nothing(self) -> None:
        run = runner(lambda: [{"id": "off:1", "origin": "off"}], lambda _id: {})
        run.stop()
        self.assertEqual({"running": False}, run.start())


if __name__ == "__main__":
    unittest.main()

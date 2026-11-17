"""Remove the products not used for a chosen number of days, as one run (#3444).

The athlete picks 30, 90, 180 or 365 days. A product is removed when its newest
logged meal is older than that, or when it was never logged, whatever its age.
Favorites and products a saved meal or template uses always stay. The use is
read from the meal log like the list's count (``NutritionRepository.product_usage``),
so an own product also keeps the uses of the database food with its EAN.

The server finds the set itself, only on a confirmed request, inside the same
transaction that removes it: an own product is archived and a stored database
food removed, as their single "Löschen" does. The run is one change-history
entry of ``ENTITY_TYPE`` with every removed row, so one undo restores the whole
run; the run also stays in the key-value store until it is undone.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping
from datetime import date, timedelta
from typing import Any, Final

from backend import change_history
from backend.db.repositories import (
    KeyValueRepository,
    NutritionProductRepository,
    NutritionRepository,
    NutritionTemplateRepository,
    basis_product_ids,
    row_values,
)
from backend.errors import AppError

ENTITY_TYPE: Final = "nutrition_product_cleanup"
THRESHOLDS: Final = (30, 90, 180, 365)
_RUN_PREFIX: Final = "nutrition_product_cleanup:"
# Every column of ``nutrition_products``, so undo puts a removed row back as it was.
_COLUMNS: Final = (
    "id", "barcode", "name", "brand", "basis_amount", "basis_unit", "kcal",
    "carbs_g", "protein_g", "fat_g", "sugar_g", "fiber_g", "salt_g", "source",
    "source_url", "external_id", "provenance", "extraction_confidence", "status",
    "created_at", "updated_at", "favorite", "origin", "fetched_at", "details",
)  # fmt: skip
_INVALID_DAYS: Final = "Bitte 30, 90, 180 oder 365 Tage wählen."


class ProductCleanupService:
    """Preview, run and undo the removal of products not used for a while."""

    def __init__(
        self,
        database_manager: Any,
        today: Callable[[], date],
        now: Callable[[], str],
    ) -> None:
        self._database_manager = database_manager
        self._today = today
        self._now = now
        self._products = NutritionProductRepository()
        self._templates = NutritionTemplateRepository()
        self._key_values = KeyValueRepository(now)

    def preview(self, days: Any) -> dict[str, Any]:
        """The products a run with ``days`` would remove, newest use first."""
        threshold = _threshold(days)
        with self._database_manager.reader() as db:
            found = self._candidates(db, threshold)
        return {
            "days": threshold,
            "count": len(found),
            "products": [
                {
                    key: product.get(key)
                    for key in ("id", "name", "brand", "last_used", "created_at")
                }
                for product in found
            ],
        }

    def run(self, payload: Any) -> dict[str, Any]:
        """Remove the products the server finds for the confirmed threshold."""
        if not isinstance(payload, Mapping) or payload.get("confirmed") is not True:
            raise AppError(
                400,
                "Das Aufräumen muss ausdrücklich bestätigt werden.",
                reason="confirmation_required",
            )
        threshold = _threshold(payload.get("days"))
        stamp = self._now()
        with self._database_manager.unit_of_work() as db:
            found = self._candidates(db, threshold)
            if not found:
                return {"ok": True, "count": 0, "change": None}
            ids = [product["id"] for product in found]
            rows = self._rows(db, ids)
            for row in rows:
                if row["origin"] == "own":
                    db.execute(
                        "UPDATE nutrition_products SET status = 'archived', updated_at = ? "
                        "WHERE id = ? AND origin = 'own'",
                        (stamp, row["id"]),
                    )
                else:
                    db.execute(
                        "DELETE FROM nutrition_products WHERE id = ? AND origin <> 'own'",
                        (row["id"],),
                    )
            operation_id = str(uuid.uuid4())
            run = {
                "operation_id": operation_id,
                "days": threshold,
                "removed_at": stamp,
                "products": rows,
            }
            self._prune_runs(db)
            self._key_values.set(db, _RUN_PREFIX + operation_id, json.dumps(run))
            change = change_history.record_change(
                db, ENTITY_TYPE, operation_id, "create", None, run
            )
        if change is not None:
            # Only what the undo needs; the recorded run can be megabytes.
            change = {key: change[key] for key in change_history.UNDO_REFERENCE_FIELDS}
        return {"ok": True, "count": len(rows), "change": change}

    def _prune_runs(self, db: Any) -> None:
        """Drop stored runs whose change-history row is gone: they cannot be undone."""
        db.execute(
            "DELETE FROM kv WHERE substr(key, 1, ?) = ? AND substr(key, ?) NOT IN "
            "(SELECT entity_id FROM change_history WHERE entity_type = ?)",
            (len(_RUN_PREFIX), _RUN_PREFIX, len(_RUN_PREFIX) + 1, ENTITY_TYPE),
        )

    def current(
        self, db: Any, entity_id: str
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """The stored run of one recorded change, for change history."""
        text = self._key_values.get(db, _RUN_PREFIX + entity_id)
        value = json.loads(text) if text else None
        return value, change_history.audit_projection(ENTITY_TYPE, value)

    def restore_in_transaction(
        self,
        db: Any,
        entity_id: str,
        current: dict[str, Any] | None,
        target: dict[str, Any] | None,
    ) -> None:
        """Put every product of one run back: archived ones active, removed ones stored."""
        if target is not None or current is None:
            raise AppError(409, "Ein zurückgenommenes Aufräumen wird nicht wiederholt.")
        for row in current.get("products") or []:
            if row.get("origin") == "own":
                db.execute(
                    "UPDATE nutrition_products SET status = ?, updated_at = ? "
                    "WHERE id = ? AND origin = 'own' AND status = 'archived'",
                    (row.get("status") or "active", row.get("updated_at"), row["id"]),
                )
                continue
            columns = [column for column in _COLUMNS if column in row]
            db.execute(
                f"INSERT OR IGNORE INTO nutrition_products({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                tuple(row[column] for column in columns),
            )
        self._key_values.delete(db, _RUN_PREFIX + entity_id)

    def _candidates(self, db: Any, threshold: int) -> list[dict[str, Any]]:
        """The listed products not used for ``threshold`` days, neither favorite nor kept."""
        cutoff = (self._today() - timedelta(days=threshold)).isoformat()
        kept = set()
        for template in self._templates.list(db):
            kept |= basis_product_ids(template.get("nutrition_basis"))
        listed = self._products.list_all(
            db, limit=None, usage=NutritionRepository.product_usage(db)
        )
        found = [
            product
            for product in listed
            if not product.get("favorite")
            and product["id"] not in kept
            and (product["last_used"] is None or product["last_used"] < cutoff)
        ]
        # Newest use first, never used last; ties by name.
        found.sort(key=lambda product: product["name"].casefold())
        found.sort(key=lambda product: product["last_used"] or "", reverse=True)
        return found

    def _rows(self, db: Any, ids: list[str]) -> list[dict[str, Any]]:
        rows = db.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM nutrition_products "
            f"WHERE id IN ({', '.join('?' for _ in ids)})",
            tuple(ids),
        ).fetchall()
        return [dict(zip(_COLUMNS, row_values(row), strict=True)) for row in rows]


def _threshold(days: Any) -> int:
    try:
        value = int(str(days))
    except ValueError:
        raise AppError(400, _INVALID_DAYS, reason="invalid_days") from None
    if value not in THRESHOLDS:
        raise AppError(400, _INVALID_DAYS, reason="invalid_days")
    return value

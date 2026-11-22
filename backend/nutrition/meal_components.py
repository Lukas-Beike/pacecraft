"""Direct correction of a logged meal's components (#3948).

The athlete removes a component, changes its amount or swaps it for another
database food or saved product. Components are addressed by their position in
``entry_components``; the client never sends nutrient values, the server scales
or calculates them from the stored snapshot, the food ID and the amount.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from backend.errors import AppError
from backend.nutrition.components import MealComponentCalculator
from backend.nutrition.food_database import NUTRIENTS

EDIT_KEYS = {"index", "remove", "amount", "unit", "food_id", "product_id"}
INVALID_EDIT = "Ungültige Änderung der Mahlzeitenkomponenten."


def wrapped_component(entry: dict[str, Any]) -> dict[str, Any]:
    """A meal without components as one component that keeps its values and origin."""
    basis = entry.get("nutrition_basis") or {"kind": "manual"}
    # The intake totals scale a product or database basis by its component
    # amount, so the wrapped component keeps the amount of that basis.
    amount = basis.get("amount")
    unit = basis.get("unit")
    estimated = basis.get("kind") == "estimate" or entry.get("source") in {
        "coach",
        "photo",
        "voice",
    }
    return {
        "kind": "estimate" if estimated else "manual",
        "name": str(entry.get("description") or "Mahlzeit")[:200],
        "amount": amount
        if isinstance(amount, (int, float))
        and not isinstance(amount, bool)
        and amount > 0
        else 1,
        "unit": unit if unit in {"g", "ml", "portion"} else "portion",
        **{key: entry.get(key) for key in NUTRIENTS},
        "nutrition_basis": basis,
    }


def _database_component(ingredient: dict[str, Any]) -> dict[str, Any]:
    food = {
        key: value for key, value in ingredient.items() if key not in {"amount", "unit"}
    }
    amount = float(ingredient["amount"])
    per_100 = food.get("per_100") or {}
    return {
        "kind": "database",
        "name": food.get("name") or "Lebensmittel",
        "amount": amount,
        "unit": ingredient.get("unit") or food.get("basis_unit"),
        **{
            key: None
            if per_100.get(key) is None
            else float(per_100[key]) * amount / 100
            for key in NUTRIENTS
        },
        "nutrition_basis": {"kind": "database", "food": food},
    }


def entry_components(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """The components a meal is edited by, whatever shape it was logged in."""
    basis = entry.get("nutrition_basis") or {}
    kind = basis.get("kind")
    if kind == "composite" and basis.get("components"):
        return [dict(component) for component in basis["components"]]
    if (
        kind == "database"
        and isinstance(basis.get("ingredients"), list)
        and basis["ingredients"]
    ):
        return [_database_component(item) for item in basis["ingredients"]]
    amount = basis.get("amount")
    if (
        kind == "local_product"
        and isinstance(basis.get("product"), dict)
        and isinstance(amount, (int, float))
        and not isinstance(amount, bool)
        and amount > 0
    ):
        product = basis["product"]
        return [
            {
                "kind": "local_product",
                "name": product.get("name") or entry.get("description") or "Produkt",
                "amount": amount,
                "unit": basis.get("unit") or product.get("basis_unit"),
                **{key: entry.get(key) for key in NUTRIENTS},
                "nutrition_basis": {"kind": "local_product", "product": product},
            }
        ]
    return [wrapped_component(entry)]


def components_revision(components: list[dict[str, Any]]) -> str:
    """A short fingerprint of the components, so an edit of a changed meal is refused."""
    text = json.dumps(components, sort_keys=True, default=str)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _validated_edits(edits: Any, count: int) -> list[dict[str, Any]]:
    if not isinstance(edits, list) or not edits:
        raise AppError(400, INVALID_EDIT)
    seen: set[int] = set()
    for edit in edits:
        index = edit.get("index") if isinstance(edit, dict) else None
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or not 0 <= index < count
            or index in seen
            or set(edit) - EDIT_KEYS
        ):
            raise AppError(400, INVALID_EDIT)
        seen.add(index)
        remove = edit.get("remove")
        target = {"food_id", "product_id"} & set(edit)
        if (remove is not None and remove is not True) or len(target) > 1:
            raise AppError(400, INVALID_EDIT)
        if remove and set(edit) != {"index", "remove"}:
            raise AppError(400, INVALID_EDIT)
        if not remove and set(edit) == {"index"}:
            raise AppError(400, INVALID_EDIT)
    return edits


def swap_foods(
    edits: Any, calculator: MealComponentCalculator
) -> dict[Any, dict[str, Any]]:
    """The database foods the swaps name, read before the transaction opens."""
    named = [
        edit
        for edit in (edits if isinstance(edits, list) else [])
        if isinstance(edit, dict)
        and "food_id" in edit
        and isinstance(edit.get("index"), int)
    ]
    if not named:
        return {}
    resolved = calculator.prepare_resolutions(
        [
            {"kind": "database", "food_id": edit["food_id"], "amount": 1, "unit": "g"}
            for edit in named
        ]
    )
    return {
        edit.get("index"): resolved[position] for position, edit in enumerate(named)
    }


def _scaled(component: dict[str, Any], amount: float) -> dict[str, Any]:
    ratio = amount / float(component["amount"])
    return {
        **component,
        "amount": amount,
        **{
            key: None if component.get(key) is None else component[key] * ratio
            for key in NUTRIENTS
        },
    }


def edited_components(
    entry: dict[str, Any],
    edits: Any,
    revision: Any,
    calculator: MealComponentCalculator,
    db: Any,
    foods: dict[Any, dict[str, Any]],
) -> list[dict[str, Any]]:
    """The meal's components after the edits; an untouched component stays as stored."""
    components = entry_components(entry)
    if revision != components_revision(components):
        raise AppError(
            409,
            "Die Mahlzeit hat sich geändert. Bitte erneut öffnen.",
            reason="meal_changed",
        )
    edits = _validated_edits(edits, len(components))
    by_index = {edit["index"]: edit for edit in edits}
    result: list[dict[str, Any]] = []
    for index, component in enumerate(components):
        edit = by_index.get(index)
        if edit is None:
            result.append(component)
        elif edit.get("remove"):
            continue
        elif "food_id" in edit or "product_id" in edit:
            amount = edit.get("amount", component["amount"])
            unit = edit.get("unit", component["unit"])
            spec = (
                {
                    "kind": "database",
                    "food_id": edit["food_id"],
                    "amount": amount,
                    "unit": unit,
                }
                if "food_id" in edit
                else {
                    "kind": "local_product",
                    "product_id": edit["product_id"],
                    "amount": amount,
                    "unit": unit,
                }
            )
            calculated = calculator.calculate(
                db, [spec], {0: foods[index]} if "food_id" in edit else None
            )
            result.append(calculated["nutrition_basis"]["components"][0])
        else:
            if "unit" in edit and edit["unit"] != component["unit"]:
                raise AppError(400, INVALID_EDIT)
            result.append(
                _scaled(component, calculator.validated_quantity(edit.get("amount")))
            )
    if not result:
        raise AppError(
            400,
            "Die letzte Zutat lässt sich nur mit der Mahlzeit löschen.",
            reason="last_component",
        )
    return result

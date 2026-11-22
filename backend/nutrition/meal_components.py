"""Direct correction of a logged meal's components (#3948).

The athlete removes a component, changes its amount or swaps it for another
database food or saved product. Components are addressed by their position in
``entry_components``; the client never sends nutrient values, the server scales
or calculates them from the stored snapshot, the food ID and the amount.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from backend.errors import AppError
from backend.nutrition.components import MealComponentCalculator
from backend.nutrition.food_database import NUTRIENTS

EDIT_KEYS = {"index", "remove", "amount", "unit", "food_id", "product_id"}
CLOCK_MINUTES = re.compile(r"\d{2}:\d{2}")
MEAL_KEYS = {"meal_date", "meal_type", "meal_time", "logged_time_known"}
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
    """Each edit names a free position once: a removal alone or a change of the rest."""
    if not isinstance(edits, list) or not edits:
        raise AppError(400, INVALID_EDIT)
    seen: set[Any] = set()
    for edit in edits:
        keys = set(edit) if isinstance(edit, dict) else set()
        index = edit.get("index") if keys else None
        positioned = (
            isinstance(index, int)
            and not isinstance(index, bool)
            and 0 <= index < count
            and index not in seen
        )
        removal = keys == {"index", "remove"} and edit["remove"] is True
        change = (
            "remove" not in keys
            and 1 < len(keys)
            and keys <= EDIT_KEYS
            and not {"food_id", "product_id"} <= keys
        )
        if not (positioned and (removal or change)):
            raise AppError(400, INVALID_EDIT)
        seen.add(index)
    return edits


def validated_meal_changes(meal: Any) -> dict[str, Any]:
    """The meal's own fields an edit changes: day, type and time, or an unknown time (#3985)."""
    keys = set(meal) if isinstance(meal, dict) else set()
    # A time is either named or unknown; the unknown flag is only ever set, never cleared.
    if (
        not keys
        or not keys <= MEAL_KEYS
        or ("logged_time_known" in keys and meal["logged_time_known"] is not False)
        or {"logged_time_known", "meal_time"} <= keys
    ):
        raise AppError(400, INVALID_EDIT)
    time = meal.get("meal_time")
    # A time with seconds like the stored default times, so a default stays recognizable.
    if isinstance(time, str) and CLOCK_MINUTES.fullmatch(time.strip()):
        return {**meal, "meal_time": f"{time.strip()}:00"}
    return dict(meal)


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


def _changed(
    component: dict[str, Any],
    edit: dict[str, Any],
    calculator: MealComponentCalculator,
    db: Any,
    food: dict[str, Any] | None,
) -> dict[str, Any]:
    """One component resized or swapped; a swap keeps the amount and unit unless named."""
    key = next((key for key in ("food_id", "product_id") if key in edit), None)
    if key is None:
        if "unit" in edit and edit["unit"] != component["unit"]:
            raise AppError(400, INVALID_EDIT)
        return _scaled(component, calculator.validated_quantity(edit.get("amount")))
    spec = {
        "kind": "database" if key == "food_id" else "local_product",
        key: edit[key],
        "amount": edit.get("amount", component["amount"]),
        "unit": edit.get("unit", component["unit"]),
    }
    calculated = calculator.calculate(db, [spec], {0: food} if food else None)
    return calculated["nutrition_basis"]["components"][0]


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
        elif "remove" not in edit:
            result.append(_changed(component, edit, calculator, db, foods.get(index)))
    if not result:
        raise AppError(
            400,
            "Die letzte Zutat lässt sich nur mit der Mahlzeit löschen.",
            reason="last_component",
        )
    return result

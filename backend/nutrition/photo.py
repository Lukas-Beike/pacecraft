"""Transient packaging-photo extraction through the selected model provider."""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
from collections.abc import Callable
from typing import Any

from backend.errors import AppError
from backend.nutrition.models import ambiguous_number_text, nutrition_number
from backend.providers.model_seam import ModelRequest, ModelTurn

MAX_IMAGE_BYTES = 5 * 1024 * 1024
# Body cap of the extract route: the base64 data URL of the largest accepted
# photo plus room for the JSON around it. Other JSON routes keep the global cap.
PHOTO_REQUEST_MAX_BYTES = 4 * math.ceil(MAX_IMAGE_BYTES / 3) + 64 * 1024
_DATA_URL = re.compile(r"^data:(image/(?:jpeg|png|webp));base64,([A-Za-z0-9+/=]+)$")
INVALID_BASIS_AMOUNT = "Ungültige Bezugsmenge."
# Portion and package size of a product (#3452), with their editor labels.
PRODUCT_SIZES = (("portion", "Portion"), ("package", "Packung"))


def _validated_number(value: Any, *, field: str) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise AppError(
            400, f"Ungültiger Wert für {field}.", reason="invalid_food_extraction"
        )
    if ambiguous_number_text(value, kcal=field == "kcal"):
        return None
    try:
        number = nutrition_number(value)
    except (TypeError, ValueError) as exc:
        raise AppError(
            400, f"Ungültiger Wert für {field}.", reason="invalid_food_extraction"
        ) from exc
    if not math.isfinite(number) or number < 0 or number > 10000:
        raise AppError(
            400, f"Ungültiger Wert für {field}.", reason="invalid_food_extraction"
        )
    return round(number, 4)


def _basis_amount(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise AppError(400, INVALID_BASIS_AMOUNT, reason="invalid_food_extraction")
    try:
        amount = nutrition_number(value)
    except (TypeError, ValueError) as exc:
        raise AppError(
            400, INVALID_BASIS_AMOUNT, reason="invalid_food_extraction"
        ) from exc
    if not math.isfinite(amount) or not 0 < amount <= 10000:
        raise AppError(
            400,
            "Bezugsmenge muss zwischen 0 und 10000 liegen.",
            reason="invalid_food_extraction",
        )
    return amount


def validate_packaging_extraction(
    payload: Any, *, require_name: bool = True
) -> dict[str, Any]:
    """Validate a provider candidate without retaining the source image."""
    if not isinstance(payload, dict):
        raise AppError(
            400,
            "Die erkannte Nährwerttabelle ist ungültig.",
            reason="invalid_food_extraction",
        )
    name = str(payload.get("name") or "").strip()
    if (require_name and not name) or len(name) > 200:
        raise AppError(
            400, "Ein Produktname ist erforderlich.", reason="invalid_food_extraction"
        )
    basis_unit = str(payload.get("basis_unit") or "").strip().lower()
    if basis_unit and basis_unit not in {"g", "ml", "portion"}:
        raise AppError(400, INVALID_BASIS_AMOUNT, reason="invalid_food_extraction")
    result: dict[str, Any] = {
        "name": name,
        "brand": str(payload.get("brand") or "").strip()[:120],
        "barcode": _barcode(payload.get("barcode")),
        "basis_amount": _basis_amount(payload.get("basis_amount")),
        "basis_unit": basis_unit or None,
        "source": "packaging_label",
    }
    for key in (
        "kcal",
        "carbs_g",
        "protein_g",
        "fat_g",
        "sugar_g",
        "fiber_g",
        "salt_g",
    ):
        result[key] = _validated_number(payload.get(key), field=key)
    result.update(product_sizes(payload, result["basis_unit"], strict=False))
    _add_confidence(result, payload.get("confidence"))
    return {
        "ok": True,
        "candidate": result,
        "provenance": "packaging_label",
        "requires_confirmation": True,
    }


def product_sizes(
    payload: dict[str, Any], basis_unit: str | None, *, strict: bool
) -> dict[str, Any]:
    """The portion and package size of a product, each an amount and a unit.

    A size counts in the product's basis unit, as g and ml cannot be converted;
    a product counted per portion takes none. A proposal (``strict`` false)
    leaves an unusable size empty; a saved product refuses it.
    """
    sizes: dict[str, Any] = {}
    for size, label in PRODUCT_SIZES:
        amount = payload.get(f"{size}_amount")
        unit = str(payload.get(f"{size}_unit") or "").strip().lower()
        sizes[f"{size}_amount"] = sizes[f"{size}_unit"] = None
        if amount in (None, "") and not unit:
            continue
        try:
            value = _basis_amount(amount)
        except AppError:
            value = None
        if value is not None and unit in {"g", "ml"} and unit == (basis_unit or unit):
            sizes[f"{size}_amount"], sizes[f"{size}_unit"] = value, unit
        elif strict:
            raise AppError(
                400,
                f"{label} braucht eine Menge über 0 in der Einheit der Bezugsmenge (g oder ml).",
                reason="invalid_product_size",
            )
    return sizes


def _add_confidence(result: dict[str, Any], confidence: Any) -> None:
    if confidence is not None:
        try:
            confidence_value = float(confidence)
            result["confidence"] = (
                max(0.0, min(1.0, confidence_value))
                if math.isfinite(confidence_value)
                else None
            )
        except TypeError, ValueError:
            result["confidence"] = None


def _barcode(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = re.sub(r"\D", "", str(value))
    if len(text) not in {8, 12, 13, 14}:
        return None
    return text


def _image_data(data_url: Any) -> tuple[str, bytes]:
    if not isinstance(data_url, str) or len(data_url) > MAX_IMAGE_BYTES * 2:
        raise AppError(
            413, "Das Verpackungsfoto ist zu groß.", reason="food_image_too_large"
        )
    match = _DATA_URL.fullmatch(data_url)
    if not match:
        raise AppError(
            415,
            "Nur JPEG-, PNG- oder WebP-Fotos werden unterstützt.",
            reason="food_image_type",
        )
    try:
        data = base64.b64decode(match.group(2), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise AppError(
            400, "Das Verpackungsfoto ist ungültig.", reason="invalid_food_image"
        ) from exc
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise AppError(
            413, "Das Verpackungsfoto ist zu groß.", reason="food_image_too_large"
        )
    signature_valid = (
        (match.group(1) == "image/jpeg" and data.startswith(b"\xff\xd8\xff"))
        or (match.group(1) == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n"))
        or (
            match.group(1) == "image/webp"
            and len(data) >= 12
            and data[:4] == b"RIFF"
            and data[8:12] == b"WEBP"
        )
    )
    if not signature_valid:
        raise AppError(
            415,
            "Dateityp und Bildinhalt stimmen nicht überein.",
            reason="food_image_type",
        )
    return match.group(1), data


class NutritionPhotoExtractionService:
    """Call OpenAI once and retain only the validated candidate."""

    def __init__(
        self,
        *,
        selected_model: Callable[[], str] | None = None,
        complete: Callable[[ModelRequest], ModelTurn] | None = None,
    ) -> None:
        self._selected_model = selected_model
        self._complete = complete

    def extract(self, data_url: Any) -> dict[str, Any]:
        mime, image = _image_data(data_url)
        if self._selected_model is None:
            raise AppError(
                503,
                "Der OpenAI-Dienst ist nicht verfügbar.",
                reason="provider_unavailable",
            )
        model = self._selected_model()
        instruction = (
            "Lies die deutsche Nährwerttabelle dieses Produktfotos. Antworte ausschließlich als JSON "
            "mit name, brand, barcode, basis_amount, basis_unit, kcal, carbs_g, protein_g, fat_g, "
            "sugar_g, fiber_g, salt_g, portion_amount, portion_unit, package_amount, package_unit und confidence. Unlesbare Werte sowie unbekannte basis_amount/basis_unit sind null; nicht raten. basis_unit ist g, ml oder portion. portion_* ist die auf der Packung genannte Portionsgröße, package_* die Füllmenge der Packung, jeweils in g oder ml, sonst null. Der Produktname darf fehlen, wenn nur eine Nährwerttabelle sichtbar ist."
        )
        encoded = base64.b64encode(image).decode("ascii")
        text = self._request_text(model, mime, encoded, instruction)
        try:
            candidate = json.loads(text)
        except (TypeError, ValueError) as exc:
            raise AppError(
                502,
                "Die Nährwerttabelle konnte nicht gelesen werden.",
                reason="invalid_provider_response",
            ) from exc
        return validate_packaging_extraction(candidate, require_name=False)

    def _request_text(
        self, model: str, mime: str, encoded: str, instruction: str
    ) -> str:
        if self._complete is None:
            raise AppError(
                503,
                "Der OpenAI-Dienst ist nicht verfügbar.",
                reason="provider_unavailable",
            )
        return self._complete(
            ModelRequest(
                model=model,
                input=instruction,
                images=[{"mime": mime, "data": encoded}],
                json_output=True,
                stateless=False,
            )
        ).text

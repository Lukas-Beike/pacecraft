"""One readable line per record a Coach turn changed (#3931 C-02)."""

from __future__ import annotations

from datetime import date
from typing import Any

# What the athlete calls the changed record, for a line without a name.
_ENTITY_NAMES = {
    "profile": "Profil",
    "workout_library": "Vorlage",
    "planned_unit": "Einheit",
    "competition": "Wettkampf",
    "training_plan": "Trainingsplan",
    "activity_feedback": "Aktivitätsfeedback",
    "nutrition_entry": "Eintrag",
    "nutrition_template": "Mahlzeitvorlage",
    "nutrition_product": "Produkt",
    "checkin": "Check-in",
    "equipment": "Ausrüstung",
    "body_entry": "Körperwert",
}
_SUBJECT_FIELDS = ("name", "description")
_DATE_FIELDS = ("date", "event_date", "meal_date", "checkin_date", "start_date")
# Changed fields a line may name, with their label and unit.
_FIELDS = {
    "name": ("Name", ""),
    "date": ("Datum", ""),
    "event_date": ("Datum", ""),
    "meal_date": ("Datum", ""),
    "duration_minutes": ("Dauer", " min"),
    "kcal": ("Energie", " kcal"),
    "carbs_g": ("Kohlenhydrate", " g"),
    "protein_g": ("Eiweiß", " g"),
    "fat_g": ("Fett", " g"),
    "session_rpe": ("RPE", ""),
    "soreness": ("Muskelkater", ""),
    "stress": ("Stress", ""),
    "motivation": ("Motivation", ""),
    "priority": ("Priorität", ""),
    "weight_kg": ("Gewicht", " kg"),
    "body_fat_pct": ("Körperfett", " %"),
    "value": ("Wert", ""),
}
_WEEKDAYS = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
_MAX_FIELDS = 3
_MAX_TEXT = 40


def _scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float)) and not isinstance(value, bool)


def day_label(value: Any) -> str:
    """ "So 22.11." for an ISO day, or an empty text."""
    try:
        day = date.fromisoformat(str(value)[:10])
    except ValueError:
        return ""
    return f"{_WEEKDAYS[day.weekday()]} {day.day}.{day.month}."


def _shown(field: str, value: Any) -> str:
    if field in {"date", "event_date", "meal_date"}:
        return day_label(value) or "–"
    if value is None or value == "":
        return "–"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).replace("\n", " ").strip()
    return text if len(text) <= _MAX_TEXT else f"{text[: _MAX_TEXT - 1]}…"


def change_detail(
    entity_type: str, before: dict[str, Any] | None, after: dict[str, Any] | None
) -> dict[str, Any] | None:
    """The compact, local-only facts one change line is made of."""
    record = after or before
    if not isinstance(record, dict) or entity_type not in _ENTITY_NAMES:
        return None
    fields = _changed_fields(before or {}, after or {})
    # The profile's name is the athlete's own, so its line says "Profil"; a renamed record is
    # named by its change, not twice.
    renamed = any(f[0] == "name" and f[1] is not None for f in fields)
    subject = "" if renamed or entity_type == "profile" else _subject(record)
    day = next(
        (str(record[field]) for field in _DATE_FIELDS if _scalar(record.get(field))),
        "",
    )
    return {"subject": subject, "date": day, "fields": fields[:_MAX_FIELDS]}


def _subject(record: dict[str, Any]) -> str:
    for field in _SUBJECT_FIELDS:
        if _scalar(record.get(field)) and str(record[field]).strip():
            return _shown("", record[field])
    return ""


def _changed_fields(before: dict[str, Any], after: dict[str, Any]) -> list[list[Any]]:
    fields = []
    for field in sorted(set(before) | set(after)):
        old, new = before.get(field), after.get(field)
        if (
            field in _FIELDS
            and old != new
            and _scalar_or_none(old)
            and _scalar_or_none(new)
        ):
            fields.append([field, old, new])
    return fields


def _scalar_or_none(value: Any) -> bool:
    return value is None or _scalar(value)


def _changed_text(detail: dict[str, Any]) -> str:
    parts = []
    for field, old, new in detail["fields"]:
        label, unit = _FIELDS[field]
        old_text, new_text = _shown(field, old), _shown(field, new)
        parts.append(
            f"{label} {old_text} → {new_text}{'' if new_text == '–' else unit}"
        )
    return ", ".join(parts)


_ACTION_TEXT = {"create": "neu", "delete": "gelöscht", "undo": "zurückgenommen"}


def detail_line(entity_type: str, action: str, detail: Any) -> str:
    """ "Langer Lauf, So 22.11.: Dauer 80 → 85 min" for a change, or an empty text."""
    if not isinstance(detail, dict):
        return ""
    head = ", ".join(
        part
        for part in (
            detail.get("subject") or _ENTITY_NAMES.get(entity_type, ""),
            day_label(detail.get("date")),
        )
        if part
    )
    if action == "update" and detail.get("fields"):
        return f"{head}: {_changed_text(detail)}"
    return f"{head}: {_ACTION_TEXT.get(action, 'geändert')}"

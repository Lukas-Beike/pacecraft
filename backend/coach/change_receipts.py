"""Visible receipts for the local records a Coach turn changed."""

from __future__ import annotations

from datetime import date
from typing import Any

from backend.canonical.change_detail import detail_line
from backend.coach.outcomes import coach_effect_label
from backend.coach.tool_specs import DELETE_TOOLS, TOOL_DOMAIN
from backend.planning.plan_checks import BLOCKING_CODES, FINDING_CODES

# Write tools whose success leaves athlete records unchanged: previews and
# drafts take effect only through a later, separately approved step.
_NOT_A_CHANGE = frozenset({"preview_adaptive_replan", "stage_training_plan"})
_WAITING_STATUSES = frozenset({"approval_required", "cancelled"})
# As in ``outcomes.coach_effect_label``: a card run reports "applied".
_APPLIED_UNDO = frozenset({"applied", "undone"})
# Tools whose every durable effect is a change-history record that undo restores.
# Others may also write untracked state (plan constraints, tombstones, sync
# flags), so their turns are listed without a turn undo.
_FULLY_TRACKED_TOOLS = frozenset(
    {
        "update_profile",
        "save_competition",
        "manage_training_templates",
        "save_activity_feedback",
        "delete_activity_feedback",
        "confirm_activity_session",
        "split_activity_session",
        "merge_activity_sessions",
        "save_nutrition_entry",
        "update_nutrition_entry",
        "delete_nutrition_entry",
        "log_nutrition_template",
        "save_nutrition_template",
        "delete_nutrition_template",
        "save_nutrition_product",
        "fill_nutrition_product_gaps",
        "save_checkin",
        "save_equipment",
        "assign_activity_equipment",
        "log_equipment_maintenance",
        # Their Garmin and Wahoo writes follow the local value (#3640).
        "save_body_entry",
        "delete_body_entry",
    }
)


def _is_change(tool: Any, result: Any) -> bool:
    if tool not in TOOL_DOMAIN or tool in _NOT_A_CHANGE or not isinstance(result, dict):
        return False
    if tool == "undo_training_change":
        # Only an applied undo restores a record; otherwise it proposes one. The
        # tool passes on the undo service's "undone" (#2134, E-501).
        return result.get("status") in _APPLIED_UNDO
    return result.get("ok") is True and result.get("status") not in _WAITING_STATUSES


def _note_dates(value: Any) -> list[str]:
    dates = []
    for day in value if isinstance(value, list) else []:
        try:
            dates.append(date.fromisoformat(day).isoformat())
        except TypeError, ValueError:
            continue
    return dates


def _plan_notes(result: dict[str, Any]) -> list[dict[str, Any]]:
    notes: list[dict[str, Any]] = []
    raw_notes = result.get("plan_check_notes")
    for note in raw_notes if isinstance(raw_notes, list) else []:
        if not isinstance(note, dict) or note.get("code") not in FINDING_CODES:
            continue
        evidence = note.get("evidence")
        warning = note["code"] in BLOCKING_CODES or (
            isinstance(evidence, dict) and evidence.get("severity") == "warning"
        )
        safe = {
            "code": note["code"],
            "dates": _note_dates(note.get("dates")),
            "severity": "warning" if warning else "notice",
        }
        if safe not in notes:
            notes.append(safe)
    return notes


_FIELD_NAMES = {
    "kcal": "Energie",
    "carbs_g": "Kohlenhydrate",
    "protein_g": "Eiweiß",
    "fat_g": "Fett",
    "sugar_g": "Zucker",
    "fiber_g": "Ballaststoffe",
    "salt_g": "Salz",
}
_ORIGIN_NAMES = {
    "open_food_facts": "Open Food Facts",
    "yazio": "Yazio",
    "bls": "BLS",
    "web": "Web",
    "manual": "eigene Angabe",
}


def _number(value: float) -> str:
    """A filled value as written (rounded to three decimals), German spelling."""
    return f"{value:.3f}".rstrip("0").rstrip(".").replace(".", ",")


def _gap_fill_lines(tool: Any, entry: dict[str, Any]) -> list[str]:
    """One receipt line per filled product: each field with value and origin."""
    if tool != "fill_nutrition_product_gaps":
        return []
    return [
        f"{change.get('name') or change.get('id')}: "
        + ", ".join(
            f"{_FIELD_NAMES.get(item['field'], item['field'])} {_number(item['value'])}"
            f" ({_ORIGIN_NAMES.get(item['origin'], item['origin'])})"
            for item in change.get("written", [])
        )
        for change in (entry.get("result") or {}).get("changes", [])
        if change.get("written")
    ]


_SLOT_NAMES = {
    "pre_session": "Vor der Einheit",
    "during_session": "Während der Einheit",
}
_SPORT_NAMES = {"ride": "Rad", "run": "Lauf", "swim": "Schwimmen", "strength": "Kraft"}


def _slot_detail(result: dict[str, Any]) -> str:
    """ "Während der Einheit · Rad 09:15 · 1.500 ml", or the open question, for a slot entry."""
    info = result.get("session")
    if not isinstance(info, dict) or info.get("slot") not in _SLOT_NAMES:
        return ""
    parts = [_SLOT_NAMES[info["slot"]]]
    session = info.get("session")
    if isinstance(session, dict):
        sport = str(session.get("sport") or "")
        start = str(session.get("started_at_local") or "")[11:16]
        parts.append(f"{_SPORT_NAMES.get(sport, sport.capitalize())} {start}".strip())
    elif info.get("question"):
        parts.append(
            f"{info['question']} "
            + " oder ".join(
                f"{_SPORT_NAMES.get(str(c.get('sport')), c.get('sport'))} "
                f"{str(c.get('started_at_local') or '')[11:16]}"
                for c in info.get("candidates", [])
            )
        )
    else:
        parts.append("Einheit noch nicht zugeordnet")
    volume = (result.get("entry") or {}).get("volume_ml")
    if isinstance(volume, (int, float)):
        parts.append(f"{volume:,.0f} ml".replace(",", "."))
    return " · ".join(parts)


def _append_new(target: list[Any], items: list[Any]) -> None:
    """Add each item once, keeping the order of first appearance."""
    for item in items:
        if item not in target:
            target.append(item)


_MAX_DETAILS = 12
_REFUSED_DELETE = "Löschen nicht ausgeführt – bitte bestätigen"


def _detail_lines(entry: dict[str, Any]) -> list[str]:
    """One line per tracked change of a write, as far as its record names it."""
    lines = []
    for change in entry.get("changes") or []:
        if isinstance(change, dict) and (
            line := detail_line(
                str(change.get("entity_type")),
                str(change.get("action")),
                change.get("detail"),
            )
        ):
            lines.append(line)
    return lines


def _undo_note(blockers: list[tuple[Any, str]]) -> str:
    """Why the turn has no undo: the writes whose effects change history misses."""
    if not blockers:
        return ""
    if all(TOOL_DOMAIN.get(tool) == "planning" for tool, _ in blockers):
        return "Rückgängig im Kalender"
    return f"Nicht rückgängig zu machen: {' · '.join(dict.fromkeys(label for _, label in blockers))}"


def change_summary(command_receipts: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Summarize a turn's applied writes for the chat, or ``None`` without any.

    The turn is undoable only when every applied write is fully tracked by
    change history; otherwise undo would silently keep part of the turn.
    """
    labels: list[str] = []
    details: list[str] = []
    blockers: list[tuple[Any, str]] = []
    notes: list[dict[str, Any]] = []
    # A removal that waits for the athlete's confirmation (AGENTS.md): not done, not failed.
    refused = False
    for entry in command_receipts:
        tool = entry.get("tool")
        if not _is_change(tool, entry.get("result")):
            result = entry.get("result")
            refused |= (
                tool in DELETE_TOOLS
                and not entry.get("resolved")
                and isinstance(result, dict)
                and result.get("status") == "approval_required"
            )
            continue
        label = coach_effect_label(entry)
        if not (tool in _FULLY_TRACKED_TOOLS and entry.get("changes")):
            blockers.append((tool, label))
        _append_new(details, _detail_lines(entry))
        if detail := _slot_detail(entry["result"]):
            label = f"{label} · {detail}"
        _append_new(labels, [label, *_gap_fill_lines(tool, entry)])
        # A card-free sync names each deletion at the provider, alike or not (#2342).
        labels.extend(entry["result"].get("remote_removals", []))
        _append_new(notes, _plan_notes(entry["result"]))
    if not labels and not refused:
        return None
    note = _undo_note([item for item in blockers if item[0] != "undo_training_change"])
    extras = {
        "not_done": [_REFUSED_DELETE] if refused else [],
        "details": details[:_MAX_DETAILS],
        "undo_note": note,
        "plan_check_notes": notes,
    }
    return {
        "labels": labels,
        "undoable": bool(labels) and not blockers,
        "undone_at": None,
        **{key: value for key, value in extras.items() if value},
    }


def turn_changes(command_receipts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Undo references of a turn's tracked changes, in the order recorded."""
    return [
        change
        for entry in command_receipts
        if isinstance(entry, dict)
        and entry.get("tool") in _FULLY_TRACKED_TOOLS
        and isinstance(entry.get("changes"), list)
        for change in entry["changes"]
    ]

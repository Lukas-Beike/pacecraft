"""Reusable Coach turn primitives shared by HTTP and background execution."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from backend.canonical.planning_fields import SESSION_FOCUS_KEYS
from backend.coach.proposal_models import coach_action_hash


def dialogue_scope_repair_key(name: str, arguments: dict[str, Any]) -> str:
    """Match the same requested effect when only its object scope is repaired."""
    request = arguments.get("_request") or {}
    binding = {
        key: request.get(key)
        for key in ("target", "period", "constraints", "remote_write", "sync_scope")
    }
    payload = {
        key: value
        for key, value in arguments.items()
        if key not in {"_request", "expected_revision"}
    }
    if isinstance(payload.get("changes"), list):
        payload["changes"] = [
            {
                key: value
                for key, value in change.items()
                if key != "expected_payload_hash"
            }
            if isinstance(change, dict)
            else change
            for change in payload["changes"]
        ]
    return coach_action_hash({"tool": name, "arguments": payload, "binding": binding})


def dialogue_request_binding_key(arguments: dict[str, Any]) -> str | None:
    """Identify one request without trusting its message provenance or prose."""
    request = arguments.get("_request")
    if not isinstance(request, dict):
        return None
    scope, constraints = request.get("scope"), request.get("constraints")
    if not isinstance(scope, list) or not all(
        isinstance(value, str) for value in scope
    ):
        return None
    if not isinstance(constraints, list) or not all(
        isinstance(value, str) for value in constraints
    ):
        return None
    binding = {
        "target": request.get("target"),
        "scope": sorted(scope),
        "period": request.get("period"),
        "constraints": sorted(constraints),
        "remote_write": request.get("remote_write"),
        "sync_scope": request.get("sync_scope"),
    }
    return coach_action_hash(binding)


def dialogue_plan_effect_key(name: str, arguments: dict[str, Any]) -> str | None:
    """Match cross-tool repairs only when the planned workout payload is exact."""
    if name == "apply_training_patch":
        if arguments.get("changes"):
            # Existing-unit edits cannot be proven equivalent to a complete
            # replacement without retaining and comparing every prior object.
            return None
        workouts = arguments.get("workouts")
    elif name == "replace_training_plan":
        workouts = (arguments.get("payload") or {}).get("workouts")
    else:
        return None
    if not isinstance(workouts, list) or not workouts:
        return None
    signatures = []
    for workout in workouts:
        if not isinstance(workout, dict):
            return None
        signatures.append(
            {
                key: workout.get(key)
                for key in (
                    "date",
                    "sport",
                    "name",
                    "description",
                    "duration_minutes",
                    "target",
                    "rationale",
                    *SESSION_FOCUS_KEYS,
                )
            }
        )
    return coach_action_hash({"workouts": signatures})


# Argument keys that name the stored object a write step acts on, and the
# fields whose value says which record of it a step writes (#3651).
_OBJECT_ID_KEYS = frozenset({"local_id", "id"})
_RECORD_KEYS = ("date", "meal_date", "meal_type")


def _operation(value: dict[str, Any]) -> str:
    """What a step does to its object: its action, else the fields it sets and
    the day and meal it writes, so a refined value still matches but another
    change to the same object does not."""
    action = value.get("action")
    if isinstance(action, str) and action.strip():
        return action.strip().casefold()
    fields = sorted(
        str(key)
        for key in value
        if key not in _OBJECT_ID_KEYS
        and key != "_request"
        and not str(key).startswith("expected_")
    )
    marks = [f"{key}={value[key]}" for key in _RECORD_KEYS if key in value]
    return ",".join(fields) + ";" + ";".join(marks)


def _object_ids(value: Any) -> set[str]:
    """Each object a step's arguments act on, with its operation, outside the
    _request: ``<operation>:<id>``."""
    if isinstance(value, list):
        return {found for item in value for found in _object_ids(item)}
    if not isinstance(value, dict):
        return set()
    ids = sorted(
        str(value[key]).strip()
        for key in _OBJECT_ID_KEYS
        if isinstance(value.get(key), str | int) and str(value[key]).strip()
    )
    found = {f"{_operation(value)}:{object_id}" for object_id in ids}
    for key, item in value.items():
        if key != "_request" and isinstance(item, list | dict):
            found |= _object_ids(item)
    return found


def coach_repair_key(name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
    """Identify the affected object fields needed to match a correction.

    A profile change names its fields; any other step names the stored objects
    it acts on, so a retry on the same objects repairs a scope failure (#3651).
    """
    if name != "update_profile":
        objects = sorted(_object_ids(arguments))
        return {"objects": objects} if objects else None
    changes = arguments.get("changes")
    if not isinstance(changes, list):
        return None
    fields = sorted(
        {
            str(item.get("field") or "")
            for item in changes
            if isinstance(item, dict) and item.get("field")
        }
    )
    return {"profile_fields": fields} if fields else None


def dialogue_effect_key(name: str, arguments: dict[str, Any]) -> str:
    request = arguments.get("_request") or {}
    if not isinstance(request, dict):
        raise ValueError("request_object")  # noqa: TRY004 - Preserve the validation ValueError contract.
    binding = {
        key: request.get(key)
        for key in ("target", "period", "constraints", "remote_write", "sync_scope")
    }
    binding["scope"] = sorted(request.get("scope") or [])
    return coach_action_hash(
        {
            "tool": name,
            "arguments": {
                key: value for key, value in arguments.items() if key != "_request"
            },
            "binding": binding,
        }
    )


def command_receipt(value: Any) -> dict[str, Any]:
    """Decode a persisted receipt without allowing malformed state to escape."""
    try:
        receipt = (
            json.loads(value or "{}") if not isinstance(value, dict) else dict(value)
        )
    except TypeError, ValueError:
        receipt = {}
    return receipt if isinstance(receipt, dict) else {}


def mark_resolved_receipts(
    command_receipts: list[dict[str, Any]], failures: Iterable[dict[str, Any]]
) -> None:
    """Mark failed tool calls resolved when a later retry superseded them."""
    failure_ids = {id(entry) for entry in failures}
    for entry in command_receipts:
        if not entry.get("result", {}).get("ok"):
            entry["resolved"] = id(entry) not in failure_ids


def effects_from_receipts(
    command_receipts: list[dict[str, Any]],
    internal_tools: set[str],
) -> list[dict[str, Any]]:
    """Return successful externally meaningful tool effects."""
    return [
        entry
        for entry in command_receipts
        if entry.get("result", {}).get("ok") and entry.get("tool") not in internal_tools
    ]


def outcome_status(
    *,
    question: str,
    incomplete_answer: bool,
    failures: list[dict[str, Any]],
    missing_answer: bool,
    effects: list[dict[str, Any]],
    cancelled: bool,
) -> str:
    """Choose the durable status for a completed Coach turn."""
    if question:
        return "completed"
    if incomplete_answer or ((failures or missing_answer) and effects):
        return "partial"
    if failures or missing_answer:
        return "failed"
    return "cancelled" if cancelled else "completed"

"""Bounded, privacy-preserving change-history persistence."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any

from backend.canonical.change_detail import change_detail
from backend.canonical.planning_fields import SESSION_FOCUS_KEYS as FOCUS_KEYS
from backend.canonical.planning_fields import TEMPLATE_ID
from backend.errors import AppError

RETENTION_DAYS = 180
MAX_ROWS = 1000
ENTITY_TYPES = {
    "profile",
    "workout_library",
    "planned_unit",
    "competition",
    "training_plan",
    "zone_configuration",
    "activity_feedback",
    "activity_session",
    "nutrition_entry",
    "nutrition_template",
    "nutrition_product",
    "food_database_entry",
    "checkin",
    "equipment",
    "equipment_assignment",
    "equipment_planned",
    "equipment_rule",
    "equipment_rule_apply",
    "equipment_maintenance",
    "metric_export",
    # One run of the product cleanup with every product it removed (#3444).
    "nutrition_product_cleanup",
    # A weight or body fat entered in Körper or by the Coach; its entity id is
    # "<metric>:<ISO day>" (#3640).
    "body_entry",
}
# A metric export to a provider; its entity id is "<provider>:<remote record>".
# The write is remote, so it is shown but never undone.
METRIC_EXPORT_ENTITY = "metric_export"
NOT_UNDOABLE_ENTITY_TYPES = frozenset({METRIC_EXPORT_ENTITY})
# A row whose diff carries ``"undoable": false`` stays listed but is never undone:
# the Intervals.icu purge (#1728) moved the feedback rows it would restore.
_UNDOABLE_FIELD = "undoable"
ACTIONS = {"create", "update", "delete", "undo"}
# Fields a caller needs to undo a recorded change later.
UNDO_REFERENCE_FIELDS = ("id", "entity_type", "action", "after_hash")

_collected_changes: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "collected_changes", default=None
)


_change_source: ContextVar[str | None] = ContextVar("change_source", default=None)


@contextmanager
def collect_changes(source: str | None = None) -> Iterator[list[dict[str, Any]]]:
    """Collect undo references for the changes recorded inside the block.

    Undo rows are not collected because they cannot be undone again. A
    ``source`` (the Coach tool round passes ``coach``) stamps the default-source
    changes recorded inside the block.
    """
    collected: list[dict[str, Any]] = []
    token = _collected_changes.set(collected)
    # A nested collection keeps the stamp of the block around it.
    source_token = _change_source.set(source or _change_source.get())
    try:
        yield collected
    finally:
        _change_source.reset(source_token)
        _collected_changes.reset(token)


PROFILE_FIELDS = {
    "name",
    "goals",
    "sports",
    "training_background",
    "typical_weekly_volume",
    "availability",
    "constraints",
    "equipment",
    "training_preferences",
    "training_preferences_running",
    "training_preferences_cycling",
    "training_preferences_swimming",
    "training_preferences_strength",
    "training_preferences_mobility",
    "training_model",
    "coaching_style",
    "timezone",
    "weather_location",
    "weight_kg",
    "body_fat_pct",
    "height_cm",
    "sex",
    "birth_year",
    "performance_notes",
    "sleep_target_hours",
    "fueling_tolerance",
    "energy_goal",
    "energy_goal_kcal",
    "learned_everyday_checked",
    "first_threshold_pct",
    "first_threshold_hr",
    "first_threshold_origin",
    "preferred_source_thresholds",
    "preferred_source_body",
    "preferred_source_hrv",
    "preferred_source_resting_hr",
    "preferred_source_sleep",
}
LIBRARY_FIELDS = {
    "id",
    "type",
    "sport",
    "environment",
    "sport_detail",
    "name",
    "description",
    "duration_minutes",
    "planned_duration_s",
    "target",
    "date",
    "source",
    "rationale",
    "plan_id",
    "plan_name",
    "archived",
    "local_marked",
    "private_calendar_adjustment",
    "sync_status",
    *FOCUS_KEYS,
}
PLANNED_UNIT_FIELDS = {
    "id",
    "type",
    "sport",
    "environment",
    "sport_detail",
    "name",
    "description",
    "duration_minutes",
    "planned_duration_s",
    "target",
    "date",
    "starts_at_local",
    "source",
    "origin",
    "rationale",
    "plan_id",
    "plan_name",
    "archived",
    "sync_status",
    "remote_event_id",
    "remote_event_external_id",
    "remote_event_category",
    "local_deleted",
    TEMPLATE_ID,
    *FOCUS_KEYS,
}
COMPETITION_FIELDS = {
    "id",
    "name",
    "event_date",
    "starts_at_local",
    "sport",
    "environment",
    "sport_detail",
    "priority",
    "category",
    "distance",
    "target",
    "course_profile",
    "notes",
    "description",
    "planned_duration_s",
    "location",
    "sync_state",
}
PLAN_FIELDS = {"id", "name", "goal", "start_date", "end_date", "status"}
# Every stored feedback row of one session group, kept whole so undo restores
# hidden originals and full-length notes; the entity id is the group key.
ACTIVITY_FEEDBACK_FIELDS = ("rows",)
# Feedback rows reach 4000-character notes, so their field bound is the bounded
# group (MAX_FEEDBACK_GROUP_ROWS rows) instead of the general 4000 characters.
MAX_FEEDBACK_GROUP_ROWS = 32
# The bundling columns of the sessions one session correction touched.
ACTIVITY_SESSION_FIELDS = ("sessions",)
# One logged meal; sync bookkeeping is left out so a provider sync keeps undo.
NUTRITION_ENTRY_FIELDS = (
    "id",
    "meal_date",
    "logged_at",
    "meal_type",
    "description",
    "kcal",
    "carbs_g",
    "protein_g",
    "fat_g",
    "source",
    "nutrition_basis",
    "logged_time_known",
    "estimate_uncertain",
    "volume_ml",
    "session_id",
)
# A saved meal; the update time is not tracked, so a save without a change
# records nothing and a restore stamps the time again.
NUTRITION_TEMPLATE_FIELDS = (
    "id",
    "name",
    "description",
    "meal_type",
    "meal_type_explicit",
    "kcal",
    "carbs_g",
    "protein_g",
    "fat_g",
    "source",
    "nutrition_basis",
)
# A saved food product; the update time is not tracked, so a save without a change
# records nothing. The favorite flag is a display preference and stays out, so
# toggling it neither enters the history nor blocks undo of an earlier change.
NUTRITION_PRODUCT_FIELDS = (
    "id",
    "barcode",
    "name",
    "brand",
    "basis_amount",
    "basis_unit",
    "kcal",
    "carbs_g",
    "protein_g",
    "fat_g",
    "sugar_g",
    "fiber_g",
    "salt_g",
    "source",
    "source_url",
    "external_id",
    "provenance",
    "extraction_confidence",
    "status",
    "portion_amount",
    "portion_unit",
    "package_amount",
    "package_unit",
)
# A stored database food: its nutrient values and the basis they count on. Only
# a gap fill and the athlete's edit record it; a refetch replaces the values and
# so ends undo.
FOOD_DATABASE_ENTRY_FIELDS = (
    "id",
    "basis_amount",
    "basis_unit",
    # Where each filled or edited value came from; undo puts it back.
    "filled_fields",
    "kcal",
    "carbs_g",
    "protein_g",
    "fat_g",
    "sugar_g",
    "fiber_g",
    "salt_g",
)
# One daily check-in; its entity id is the check-in date. The day status, the
# tag answers and the complaints live beside the row and are present only once set.
CHECKIN_FIELDS = (
    "checkin_date",
    "soreness",
    "stress",
    "motivation",
    "session_rpe",
    "day_form",
    "illness",
    "pain",
    "available_minutes",
    "availability_notes",
    "notes",
    "day_status",
    "tag_answers",
    "complaints",
    "symptoms",
)
# One equipment item, with the fields the athlete or Coach edits; the revision, the
# update time and the Garmin-owned mileage are left out so a provider sync keeps undo.
EQUIPMENT_FIELDS = (
    "id",
    "name",
    "sport",
    "environment",
    "sport_pending",
    "kind",
    "status",
    "parent_id",
    "parent_pending",
    "start_date",
    "initial_distance_km",
    "initial_hours",
    "maintenance_km",
    "maintenance_hours",
    "lifetime_target_km",
    "lifetime_target_source",
)
# The explicit gear of one activity; its entity id is the assignment key, and
# an unassignment keeps the row with ``equipment_id`` None.
EQUIPMENT_ASSIGNMENT_FIELDS = ("activity_id", "equipment_id")
# The athlete's equipment choice for a planned unit (#3011).
EQUIPMENT_PLANNED_FIELDS = ("planned_unit_id", "equipment_id")
# One kind's default equipment with its dated choices (#3014).
EQUIPMENT_RULE_FIELDS = ("kind", "revision", "history")
# One confirmed apply of a rule to earlier sessions; undo removes its tagged writes.
EQUIPMENT_RULE_APPLY_FIELDS = ("operation_id", "kind", "equipment_id", "applied_at")
# One maintenance entry; the observation time is left out.
EQUIPMENT_MAINTENANCE_FIELDS = ("id", "equipment_id", "date", "notes", "distance_km")
# A composite meal keeps its ingredient snapshots in nutrition_basis.
_FIELD_LIMITS = {
    "activity_feedback": 400_000,
    "activity_session": 20_000,
    "nutrition_entry": 100_000,
    "nutrition_template": 100_000,
    # Free-text notes reach 4000 characters, more once JSON-escaped.
    "checkin": 20_000,
    "equipment_maintenance": 20_000,
    # Up to RULE_HISTORY_LIMIT dated choices of one kind.
    "equipment_rule": 20_000,
    # Every removed product row of one cleanup run, so undo restores them (#3444).
    "nutrition_product_cleanup": 4_000_000,
}
_DEFAULT_FIELD_LIMIT = 4000
# A local zone configuration; its entity id is the configuration key.
ZONE_CONFIGURATION_FIELDS = (
    "sport",
    "metric",
    "environment",
    "valid_from",
    "threshold",
    "maximum",
    "limits",
    "percent_limits",
    # The adopted local LT2 estimate the threshold came from (#3273).
    "threshold_origin",
)

# The exported fields, keyed like the export consent (metric_export_consent).
METRIC_EXPORT_FIELDS = (
    "weight",
    "body_fat",
    "resting_hr",
    "threshold_pace",
    "threshold_hr",
    "max_hr",
    "ftp",
    "run_ftp",
)


def audit_projection_fields(entity_type: str) -> set[str]:
    fields_by_entity = {
        "profile": PROFILE_FIELDS,
        "workout_library": LIBRARY_FIELDS,
        "planned_unit": PLANNED_UNIT_FIELDS,
        "competition": COMPETITION_FIELDS,
        "training_plan": PLAN_FIELDS,
        "zone_configuration": set(ZONE_CONFIGURATION_FIELDS),
        "activity_feedback": set(ACTIVITY_FEEDBACK_FIELDS),
        "activity_session": set(ACTIVITY_SESSION_FIELDS),
        "nutrition_entry": set(NUTRITION_ENTRY_FIELDS),
        "nutrition_template": set(NUTRITION_TEMPLATE_FIELDS),
        "nutrition_product": set(NUTRITION_PRODUCT_FIELDS),
        "food_database_entry": set(FOOD_DATABASE_ENTRY_FIELDS),
        "checkin": set(CHECKIN_FIELDS),
        "equipment": set(EQUIPMENT_FIELDS),
        "equipment_assignment": set(EQUIPMENT_ASSIGNMENT_FIELDS),
        "equipment_planned": set(EQUIPMENT_PLANNED_FIELDS),
        "equipment_rule": set(EQUIPMENT_RULE_FIELDS),
        "equipment_rule_apply": set(EQUIPMENT_RULE_APPLY_FIELDS),
        "equipment_maintenance": set(EQUIPMENT_MAINTENANCE_FIELDS),
        "nutrition_product_cleanup": {"operation_id", "days", "removed_at", "products"},
        "body_entry": {"date", "value"},
        METRIC_EXPORT_ENTITY: set(METRIC_EXPORT_FIELDS),
    }
    try:
        return fields_by_entity[entity_type]
    except KeyError as exc:
        raise ValueError("unsupported change-history entity") from exc


def _audit_payload_projection(value: Any) -> Any:
    if not isinstance(value, dict) or not isinstance(value.get("payload"), str):
        return value
    try:
        payload = json.loads(value["payload"])
    except TypeError, ValueError:
        payload = {}
    return {
        **payload,
        "sync_status": value.get("sync_state") or payload.get("sync_status"),
    }


def audit_projection(entity_type: str, value: Any) -> dict[str, Any] | None:
    """Return the small, local-only representation allowed in change history."""
    if value is None:
        return None
    fields = audit_projection_fields(entity_type)
    if entity_type in {"workout_library", "planned_unit"}:
        value = _audit_payload_projection(value)
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    for field in sorted(fields):
        if field not in value:
            continue
        candidate = value[field]
        try:
            encoded = json.dumps(
                candidate,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
        except TypeError, ValueError:
            continue
        if len(encoded) > _FIELD_LIMITS.get(entity_type, _DEFAULT_FIELD_LIMIT):
            continue
        result[field] = candidate
    return result


def audit_hash(value: dict[str, Any] | None) -> str:
    payload = json.dumps(
        value or {},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# Projection fields added after released history rows were recorded, oldest
# generation first. Such rows were hashed without the fields of every later
# generation, so they stay matchable by stripping those generations.
_FIELDS_ADDED_TO_AUDIT: dict[str, tuple[tuple[str, ...], ...]] = {
    "profile": (
        ("sleep_target_hours", "fueling_tolerance"),
        (
            "training_preferences_running",
            "training_preferences_cycling",
            "training_preferences_swimming",
            "training_preferences_strength",
            "training_preferences_mobility",
        ),
        ("training_model",),
        (
            "preferred_source_thresholds",
            "preferred_source_body",
            "preferred_source_hrv",
            "preferred_source_resting_hr",
            "preferred_source_sleep",
        ),
        ("sex",),
        ("first_threshold_pct",),
        ("first_threshold_origin",),
        # #3099: the birth year for the skinfold equations.
        ("birth_year",),
        # #3547: the energy goal and its kcal per day.
        ("energy_goal", "energy_goal_kcal"),
        # #3577: the date the athlete took the learned everyday term over.
        ("learned_everyday_checked",),
        # #3915: the first threshold in bpm.
        ("first_threshold_hr",),
    ),
    "planned_unit": (("starts_at_local",), FOCUS_KEYS, (TEMPLATE_ID,)),
    "workout_library": (FOCUS_KEYS,),
    # #3766: structured complaints beside the check-in.
    "checkin": (("complaints",), ("symptoms",)),
    # Competitions gained their location with schema v29 (#2803).
    "competition": (("location",),),
    # Gap fills of stored foods were recorded before the origin marks were.
    "food_database_entry": (("filled_fields",),),
    # #3100: the uncertain-meal flag; schema v32 (#3234): volume and session.
    "nutrition_entry": (("estimate_uncertain",), ("volume_ml", "session_id")),
    # Schema v33 (#3452): a product's portion and package size.
    "nutrition_product": (
        ("portion_amount", "portion_unit", "package_amount", "package_unit"),
    ),
    # #3273: the adopted LT2 origin of a local zone threshold.
    "zone_configuration": (("threshold_origin",),),
}


# Fields that left the projection after rows were recorded with them, with the
# empty value those rows carried when the athlete never set one. The weekly plan
# (profile) was removed with #1850.
_FIELDS_RETIRED_FROM_AUDIT: dict[str, dict[str, Any]] = {
    "profile": {"weekly_plan": ""},
}


def accepted_audit_hashes(entity_type: str, value: dict[str, Any] | None) -> set[str]:
    """Return the hashes a current projection may match for a recorded row."""
    accepted = {audit_hash(value)}
    if not value:
        return accepted
    generations = [dict(value)]
    legacy = dict(value)
    for added in reversed(_FIELDS_ADDED_TO_AUDIT.get(entity_type, ())):
        if any(field in legacy for field in added):
            legacy = {key: item for key, item in legacy.items() if key not in added}
            generations.append(legacy)
    if retired := _FIELDS_RETIRED_FROM_AUDIT.get(entity_type):
        generations.extend({**generation, **retired} for generation in [*generations])
    for generation in generations:
        accepted.add(audit_hash(generation))
    return accepted


def audit_diff(
    before: dict[str, Any] | None, after: dict[str, Any] | None
) -> dict[str, Any]:
    before = before or {}
    after = after or {}
    fields: dict[str, dict[str, Any]] = {}
    for field in sorted(set(before) | set(after)):
        old = before.get(field)
        new = after.get(field)
        if old != new:
            fields[field] = {"before": old, "after": new}
    return {
        "fields": fields,
        "before_present": bool(before),
        "after_present": bool(after),
    }


def cleanup(db: Any, *, current_time: datetime | None = None, reserve: int = 0) -> None:
    """Drop expired rows and keep at most ``MAX_ROWS - reserve`` of the newest."""
    now = current_time or datetime.now(UTC)
    cutoff = (now - timedelta(days=RETENTION_DAYS)).isoformat()
    db.execute("DELETE FROM change_history WHERE created_at < ?", (cutoff,))
    db.execute(
        "DELETE FROM change_history WHERE id NOT IN "
        "(SELECT id FROM change_history ORDER BY created_at DESC LIMIT ?)",
        (MAX_ROWS - reserve,),
    )


def reserve_capacity(db: Any, required_rows: int) -> None:
    """Make room for one atomic operation without deleting its own audit rows."""
    required = max(0, int(required_rows))
    if required > MAX_ROWS:
        raise AppError(
            400,
            "Die Änderung ist zu groß, um vollständig in der Undo-Historie gespeichert zu werden.",
            reason="change_history_limit",
        )
    keep = max(0, MAX_ROWS - required)
    db.execute(
        "DELETE FROM change_history WHERE id NOT IN "
        "(SELECT id FROM change_history ORDER BY created_at DESC LIMIT ?)",
        (keep,),
    )


def record_change(
    db: Any,
    entity_type: str,
    entity_id: str,
    action: str,
    before: Any,
    after: Any,
    *,
    source: str = "local",
) -> dict[str, str] | None:
    if entity_type not in ENTITY_TYPES or action not in ACTIONS:
        raise ValueError("unsupported change-history record")
    before_projection = audit_projection(entity_type, before)
    after_projection = audit_projection(entity_type, after)
    before_hash = audit_hash(before_projection)
    after_hash = audit_hash(after_projection)
    if before_hash == after_hash and action != "undo":
        return None
    if source == "local":
        source = _change_source.get() or source
    record = {
        "id": str(uuid.uuid4()),
        "entity_type": entity_type,
        "entity_id": str(entity_id),
        "action": action,
        "source": str(source)[:40] or "local",
        "created_at": datetime.now(UTC).isoformat(),
        "before_hash": before_hash,
        "after_hash": after_hash,
        "diff": json.dumps(
            audit_diff(before_projection, after_projection),
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }
    if source != "coach_replacement":
        # Room for the row inserted below, so the cap holds (#2134, G-016).
        cleanup(db, reserve=1)
    db.execute(
        "INSERT INTO change_history(id, entity_type, entity_id, action, source, created_at, before_hash, after_hash, diff) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        tuple(
            record[key]
            for key in (
                "id",
                "entity_type",
                "entity_id",
                "action",
                "source",
                "created_at",
                "before_hash",
                "after_hash",
                "diff",
            )
        ),
    )
    collected = _collected_changes.get()
    if collected is not None and action != "undo":
        reference: dict[str, Any] = {key: record[key] for key in UNDO_REFERENCE_FIELDS}
        # What the change was, for the receipt line; undo reads only the reference.
        if detail := change_detail(entity_type, before_projection, after_projection):
            reference["detail"] = detail
        collected.append(reference)
    return record


def field_values(
    db: Any, entity_type: str, field: str, since: str
) -> list[tuple[str, Any]]:
    """Each value a kept change row wrote to ``field`` since ``since`` (an ISO
    time), oldest first, with the row's time (#3477)."""
    rows = db.execute(
        "SELECT created_at, diff FROM change_history WHERE entity_type = ? "
        "AND created_at >= ? ORDER BY created_at",
        (entity_type, since),
    ).fetchall()
    values = []
    for row in rows:
        try:
            changed = json.loads(row["diff"]).get("fields", {})
        except ValueError, AttributeError:
            continue
        if isinstance(changed, dict) and isinstance(changed.get(field), dict):
            values.append((str(row["created_at"]), changed[field].get("after")))
    return values


def replaced_values(
    db: Any, entity_type: str, field: str
) -> dict[str, list[tuple[str, Any]]]:
    """Per entity id, each value a kept change row replaced in ``field``, with the
    row's time, oldest first (#3521): the value the entity held before that time."""
    rows = db.execute(
        "SELECT entity_id, created_at, diff FROM change_history WHERE entity_type = ? "
        "ORDER BY created_at",
        (entity_type,),
    ).fetchall()
    replaced: dict[str, list[tuple[str, Any]]] = {}
    for row in rows:
        try:
            changed = json.loads(row["diff"]).get("fields", {})
        except ValueError, AttributeError:
            continue
        if isinstance(changed, dict) and isinstance(changed.get(field), dict):
            replaced.setdefault(str(row["entity_id"]), []).append(
                (str(row["created_at"]), changed[field].get("before"))
            )
    return replaced


def value_recorded_at(
    db: Any, entity_type: str, entity_id: str, value: dict[str, Any]
) -> str | None:
    """When ``value`` was last written for the entity, by its change rows.

    Undo rows do not count. Without such a row (pruned or older than the
    history) the oldest kept row's time is returned, a lower bound of the age;
    None only for an empty history.
    """
    value_hash = audit_hash(audit_projection(entity_type, value))
    row = db.execute(
        "SELECT created_at FROM change_history WHERE entity_type = ? "
        "AND entity_id = ? AND after_hash = ? AND action != 'undo' "
        "ORDER BY created_at DESC LIMIT 1",
        (entity_type, str(entity_id), value_hash),
    ).fetchone()
    if row is None:
        row = db.execute(
            "SELECT created_at FROM change_history ORDER BY created_at LIMIT 1"
        ).fetchone()
    return None if row is None else str(row["created_at"])


def public_view(row: dict[str, Any]) -> dict[str, Any]:
    """Remove retained athlete values from the public history representation."""
    try:
        diff = json.loads(row.get("diff") or "{}")
    except TypeError, ValueError:
        diff = {"fields": {}}
    fields = diff.get("fields")
    safe_fields = {
        field: {"changed": True}
        for field in (fields if isinstance(fields, dict) else {})
        if isinstance(field, str) and re.fullmatch(r"[a-z_]+", field)
    }
    return {
        "id": row["id"],
        "entity_type": row["entity_type"],
        "entity_id": row["entity_id"],
        "action": row["action"],
        "source": row["source"],
        "created_at": row["created_at"],
        "before_hash": row["before_hash"],
        "after_hash": row["after_hash"],
        "diff": {
            "fields": safe_fields,
            "before_present": bool(diff.get("before_present")),
            "after_present": bool(diff.get("after_present")),
        },
        "remote_sync": "remote"
        if row["entity_type"] in NOT_UNDOABLE_ENTITY_TYPES
        else "local_only",
        **({} if undoable(row) else {"undoable": False}),
    }


def undoable(row: Mapping[str, Any]) -> bool:
    """Whether a recorded change may still be undone."""
    if row.get("entity_type") in NOT_UNDOABLE_ENTITY_TYPES:
        return False
    try:
        diff = json.loads(row.get("diff") or "{}")
    except TypeError, ValueError:
        return True
    return not (isinstance(diff, dict) and diff.get(_UNDOABLE_FIELD) is False)

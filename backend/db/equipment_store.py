"""Every SQL statement on the equipment keys of the ``kv`` table (#2120).

Items, activity assignments, planned-unit choices, default rules and
maintenance entries are JSON records under five key prefixes. Functions take the caller's connection;
transactions stay with the use case. Values are stored as the caller encoded
them.
"""

from __future__ import annotations

from typing import Any

EQUIPMENT_PREFIX = "equipment:"
ASSIGNMENT_PREFIX = "equipment_assignment:"
MAINTENANCE_PREFIX = "equipment_maintenance:"
# The athlete's choice for a planned unit, keyed by its local id (#3011).
PLANNED_PREFIX = "equipment_planned:"
# The default item of one kind of session, with the day each choice took effect (#3014).
RULE_PREFIX = "equipment_rule:"
# One confirmed apply of a rule to earlier sessions, the undo handle of its writes.
RULE_APPLY_PREFIX = "equipment_rule_apply:"
GARMIN_INITIALIZED_KEY = "garmin_equipment_initialized"


def value(db: Any, key: str) -> str | None:
    row = db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return None if row is None else row["value"]


def values(db: Any, prefix: str, limit: int | None = None) -> list[str]:
    """Values under ``prefix``, newest first; a limit of None reads them all."""
    return [
        row["value"]
        for row in db.execute(
            "SELECT value FROM kv WHERE key LIKE ? ORDER BY updated_at DESC LIMIT ?",
            (prefix + "%", -1 if limit is None else limit),
        ).fetchall()
    ]


def unordered_values(db: Any, prefix: str) -> list[str]:
    """Every value under ``prefix`` in table order, as the Garmin import reads them."""
    return [
        row["value"]
        for row in db.execute(
            "SELECT value FROM kv WHERE key LIKE ?", (prefix + "%",)
        ).fetchall()
    ]


def child_values(db: Any, parent_id: str) -> list[str]:
    return [
        row["value"]
        for row in db.execute(
            "SELECT value FROM kv WHERE key LIKE ? "
            "AND json_extract(value, '$.parent_id')=?",
            (EQUIPMENT_PREFIX + "%", parent_id),
        ).fetchall()
    ]


def delete_tagged_assignments(db: Any, operation_id: str) -> int:
    """Remove the assignments one rule apply wrote that still carry its tag."""
    return db.execute(
        "DELETE FROM kv WHERE key LIKE ? AND json_extract(value, '$.operation_id')=?",
        (ASSIGNMENT_PREFIX + "%", operation_id),
    ).rowcount


def is_referenced(db: Any, equipment_id: str) -> bool:
    """Whether an assignment, planned-unit choice, rule or maintenance entry names the item."""
    return (
        db.execute(
            "SELECT 1 FROM kv WHERE ((key LIKE ? OR key LIKE ? OR key LIKE ?) "
            "AND json_extract(value, '$.equipment_id')=?) OR (key LIKE ? AND "
            "EXISTS (SELECT 1 FROM json_each(kv.value, '$.history') AS entry "
            "WHERE json_extract(entry.value, '$.equipment_id')=?)) LIMIT 1",
            (
                ASSIGNMENT_PREFIX + "%",
                PLANNED_PREFIX + "%",
                MAINTENANCE_PREFIX + "%",
                equipment_id,
                RULE_PREFIX + "%",
                equipment_id,
            ),
        ).fetchone()
        is not None
    )


def put(db: Any, key: str, text: str, updated_at: str) -> None:
    db.execute(
        "INSERT INTO kv(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE "
        "SET value=excluded.value,updated_at=excluded.updated_at",
        (key, text, updated_at),
    )


def insert(db: Any, key: str, text: str, updated_at: str) -> None:
    """Insert a new key; an existing key fails."""
    db.execute(
        "INSERT INTO kv(key,value,updated_at) VALUES(?,?,?)", (key, text, updated_at)
    )


def insert_if_absent(db: Any, key: str, text: str, updated_at: str) -> None:
    """Insert a new key; an existing key keeps its value."""
    db.execute(
        "INSERT OR IGNORE INTO kv(key,value,updated_at) VALUES(?,?,?)",
        (key, text, updated_at),
    )


def update(db: Any, key: str, text: str, updated_at: str) -> None:
    db.execute(
        "UPDATE kv SET value=?, updated_at=? WHERE key=?", (text, updated_at, key)
    )


def delete(db: Any, key: str) -> None:
    db.execute("DELETE FROM kv WHERE key=?", (key,))

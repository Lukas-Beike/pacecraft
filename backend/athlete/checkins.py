"""Pure athlete check-in validation and normalization helpers."""

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import date, timedelta
from typing import Any

from backend import change_history
from backend.db.repositories import CheckinRepository
from backend.errors import AppError

ENTITY_TYPE = "checkin"
# The kv keys of the tag answers, the day status and the structured complaints;
# the check-in date follows.
TAGS_KEY = "checkin_tags:"
DAY_STATUS_KEY = "checkin_day_status:"
COMPLAINTS_KEY = "checkin_complaints:"
# The symptoms the athlete selected with the illness report (#3919).
SYMPTOMS_KEY = "checkin_symptoms:"
# Stable ids with their German labels; any selected symptom means rest only.
SYMPTOM_LABELS = {
    "schnupfen": "Schnupfen",
    "halsschmerzen": "Halsschmerzen",
    "husten": "Husten",
    "kopfschmerzen": "Kopfschmerzen",
    "fieber": "Fieber",
    "gliederschmerzen": "Gliederschmerzen",
    "muedigkeit": "Müdigkeit/Abgeschlagenheit",
    "brust_atemnot": "Druck auf der Brust/Atemnot",
    "magen_darm": "Magen-Darm",
}
# The day the athlete answered the infection hint (M14) with "Mir geht's gut"
# (#3821); the hint stays hidden on that day only.
INFECTION_HINT_DISMISSED_KEY = "infection_hint_dismissed"
# The illness text of the answer "Ich bin krank" to the infection hint (#3821).
INFECTION_HINT_ILLNESS = "Infekt-Hinweis bestätigt"
# Structured complaints (athlete decision on #3766): a fixed body-area list, the
# side and a severity of 0-10, beside the free pain text.
COMPLAINT_AREAS = (
    "foot",
    "ankle",
    "achilles",
    "calf",
    "shin",
    "knee",
    "thigh_front",
    "thigh_back",
    "hip",
    "groin",
    "lower_back",
    "upper_back_neck",
    "shoulder",
    "arm_elbow",
    "hand_wrist",
    "other",
)
COMPLAINT_SIDES = ("left", "right", "both", "none")
MAX_COMPLAINTS = 8
# A complaint recurs on this many days within the window, or worsens by this
# many severity points between its first and latest entry there (athlete, #3766).
RECURRING_DAYS = 3
RECURRING_WINDOW_DAYS = 14
WORSENING_POINTS = 2

# Check-in answers that report no complaint (pain or illness).
NO_COMPLAINT = frozenset(
    {"", "-", "keine", "kein", "nein", "nichts", "gesund", "none", "no", "n/a"}
)


def reports_complaint(text: Any) -> bool:
    """Whether a pain or illness answer reports a complaint, not "keine"/"nein"."""
    answer = str(text or "").strip().strip(".!,;:").strip().casefold()
    return answer not in NO_COMPLAINT


def normalize_symptoms(value: Any) -> list[str]:
    """The selected symptom ids, known ones only, once each, in list order."""
    if not isinstance(value, list) or any(item not in SYMPTOM_LABELS for item in value):
        raise AppError(400, "Symptome müssen aus der Liste bekannter Symptome stammen.")
    return [key for key in SYMPTOM_LABELS if key in value]


CHECKIN_TEXT_LIMITS = {
    "day_form": 2000,
    "illness": 1000,
    "pain": 1000,
    "availability_notes": 2000,
    "notes": 4000,
}
CHECKIN_SCORE_FIELDS = ("soreness", "stress", "motivation", "session_rpe")
CHECKIN_TAGS = ("travel", "late_meal", "high_stress")
CHECKIN_DAY_STATUSES = ("unknown", "rest", "pause")
COACH_PRESERVED_FIELDS = CHECKIN_SCORE_FIELDS + (
    "available_minutes",
    "day_form",
    "illness",
    "pain",
    "availability_notes",
    "notes",
)


def _whole_number(value: Any) -> int:
    """A whole number; 7.9 and booleans are refused, not cut (#2135, D-018)."""
    if isinstance(value, bool):
        raise TypeError("a boolean is no number")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError("not a whole number")
    return int(value)


def bounded_score(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        number = _whole_number(value)
    except (TypeError, ValueError) as exc:
        raise AppError(400, "Lokale Feedback-Werte müssen ganze Zahlen sein.") from exc
    if not 0 <= number <= 10:
        raise AppError(400, "Lokale Feedback-Werte müssen zwischen 0 und 10 liegen.")
    return number


def bounded_minutes(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        number = _whole_number(value)
    except (TypeError, ValueError) as exc:
        raise AppError(
            400, "Die verfügbare Trainingszeit muss eine ganze Zahl sein."
        ) from exc
    if not 0 <= number <= 1440:
        raise AppError(
            400, "Die verfügbare Trainingszeit muss zwischen 0 und 1440 Minuten liegen."
        )
    return number


def coach_clear_fields(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) for item in value)
        or len(set(value)) != len(value)
        or set(value) - set(COACH_PRESERVED_FIELDS)
    ):
        raise AppError(
            400,
            "clear_fields muss eine eindeutige Liste erlaubter Check-in-Felder sein.",
        )
    return tuple(value)


def normalize_complaints(value: Any) -> list[dict[str, Any]]:
    """The day's complaints: known area and side, severity 0-10, one per area and side."""
    message = (
        "Beschwerden brauchen Bereich, Seite und Stärke 0 bis 10, "
        f"höchstens {MAX_COMPLAINTS} und jeden Bereich mit Seite nur einmal."
    )
    if not isinstance(value, list) or len(value) > MAX_COMPLAINTS:
        raise AppError(400, message)
    complaints: dict[tuple[str, str], dict[str, Any]] = {}
    for item in value:
        if not isinstance(item, dict) or set(item) != {"area", "side", "severity"}:
            raise AppError(400, message)
        area, side = item["area"], item["side"]
        if area not in COMPLAINT_AREAS or side not in COMPLAINT_SIDES:
            raise AppError(400, message)
        severity = bounded_score(item["severity"])
        if severity is None or (area, side) in complaints:
            raise AppError(400, message)
        complaints[(area, side)] = {"area": area, "side": side, "severity": severity}
    return [complaints[key] for key in sorted(complaints)]


def complaint_trends(
    checkins: Sequence[Mapping[str, Any]], first: str, last: str
) -> list[dict[str, Any]]:
    """Per area and side: the days with an entry and the first and latest severity."""
    trends: dict[tuple[str, str], dict[str, Any]] = {}
    for row in sorted(checkins, key=lambda item: str(item.get("checkin_date"))):
        day = str(row.get("checkin_date") or "")
        if not first <= day <= last:
            continue
        for item in row.get("complaints") or ():
            trend = trends.setdefault(
                (item["area"], item["side"]),
                {
                    "area": item["area"],
                    "side": item["side"],
                    "dates": [],
                    "first_severity": item["severity"],
                },
            )
            trend["dates"].append(day)
            trend["last_severity"] = item["severity"]
    return [
        {**trend, "days": len(trend["dates"])}
        for trend in sorted(
            trends.values(),
            key=lambda item: (-len(item["dates"]), item["area"], item["side"]),
        )
    ]


def recurring_complaints(
    checkins: Sequence[Mapping[str, Any]], today: date
) -> list[dict[str, Any]]:
    """Complaints that recur or worsen in the last 14 days, today included (#3766).

    A non-blocking planning note: the same area and side on at least
    ``RECURRING_DAYS`` days, or a severity rise of at least ``WORSENING_POINTS``
    from the first to the latest entry. Product rules, not medical limits.
    """
    first = (today - timedelta(days=RECURRING_WINDOW_DAYS - 1)).isoformat()
    return [
        trend
        for trend in complaint_trends(checkins, first, today.isoformat())
        if trend["days"] >= RECURRING_DAYS
        or trend["last_severity"] - trend["first_severity"] >= WORSENING_POINTS
    ]


def _checkin_day(value: Mapping[str, Any], today: date) -> str:
    """The check-in date, defaulting to today; a bad or future date is refused."""
    raw_date = str(value.get("checkin_date") or today.isoformat()).strip()
    try:
        checkin_date = date.fromisoformat(raw_date).isoformat()
    except ValueError as exc:
        raise AppError(400, "Das Datum des lokalen Feedbacks ist ungültig.") from exc
    if checkin_date > today.isoformat():
        raise AppError(400, "Ein Tages-Check-in kann nicht in der Zukunft liegen.")
    return checkin_date


def normalize_checkin(value: Any, *, today: date) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AppError(400, "Das lokale Feedback muss ein Objekt sein.")
    result: dict[str, Any] = {"checkin_date": _checkin_day(value, today)}
    if "day_status" in value:
        if value["day_status"] not in CHECKIN_DAY_STATUSES:
            raise AppError(
                400,
                "Tagesstatus muss unbekannt, Ruhetag oder bestätigte Trainingspause sein.",
            )
        result["day_status"] = value["day_status"]
    if "tag_answers" in value:
        answers = value["tag_answers"]
        if (
            not isinstance(answers, dict)
            or set(answers) - set(CHECKIN_TAGS)
            or any(type(answer) is not bool for answer in answers.values())
        ):
            raise AppError(
                400, "Tags benötigen ausdrücklich bestätigte Ja-/Nein-Angaben."
            )
        result["tag_answers"] = dict(answers)
    for field, normalize in (
        ("complaints", normalize_complaints),
        ("symptoms", normalize_symptoms),
    ):
        if field in value:
            result[field] = normalize(value[field])
    for field in CHECKIN_SCORE_FIELDS:
        result[field] = bounded_score(value.get(field))
    result["available_minutes"] = bounded_minutes(value.get("available_minutes"))
    for field, limit in CHECKIN_TEXT_LIMITS.items():
        result[field] = str(value.get(field) or "").strip()[:limit]
    return result


class CheckinService:
    """Coordinate local athlete check-in use cases and their transactions."""

    def __init__(
        self,
        manager: Any,
        repository: CheckinRepository,
        today: Callable[[], date],
    ):
        self._manager = manager
        self._repository = repository
        self._today = today

    def list(self, limit: int = 30) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(int(limit), 365))
        with self._manager.read_snapshot() as db:
            rows = self._repository.list(db, bounded_limit)
            for row in rows:
                self._add_stored_answers(db, row)
            return rows

    @staticmethod
    def _add_stored_answers(db: Any, row: dict[str, Any]) -> None:
        """Add the tag answers, day status and complaints kept beside the row, when set."""
        for key, field in (
            (TAGS_KEY, "tag_answers"),
            (DAY_STATUS_KEY, "day_status"),
            (COMPLAINTS_KEY, "complaints"),
            (SYMPTOMS_KEY, "symptoms"),
        ):
            stored = db.execute(
                "SELECT value FROM kv WHERE key=?", (key + row["checkin_date"],)
            ).fetchone()
            if stored:
                row[field] = json.loads(stored["value"])

    def _stored(self, db: Any, checkin_date: str) -> dict[str, Any] | None:
        """The stored check-in of one day as change history records it."""
        row = self._repository.get(db, checkin_date)
        if row is None:
            return None
        row["checkin_date"] = checkin_date
        self._add_stored_answers(db, row)
        return row

    def _set_kv(self, db: Any, key: str, value: Any) -> None:
        if value is None:
            db.execute("DELETE FROM kv WHERE key=?", (key,))
            return
        db.execute(
            "INSERT INTO kv(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (key, json.dumps(value), self._today().isoformat()),
        )

    def _write(self, db: Any, checkin: dict[str, Any]) -> dict[str, str] | None:
        """Upsert one day and record the change in the caller's transaction."""
        day = checkin["checkin_date"]
        before = self._stored(db, day)
        self._repository.upsert(db, checkin)
        if "day_status" in checkin:
            self._set_kv(db, DAY_STATUS_KEY + day, checkin["day_status"])
        if "tag_answers" in checkin:
            self._set_kv(db, TAGS_KEY + day, checkin["tag_answers"])
        if "complaints" in checkin:
            # An empty list removes the day's complaints.
            self._set_kv(db, COMPLAINTS_KEY + day, checkin["complaints"] or None)
        if "symptoms" in checkin:
            # An empty list removes the day's symptoms.
            self._set_kv(db, SYMPTOMS_KEY + day, checkin["symptoms"] or None)
        after = self._stored(db, day)
        record = change_history.record_change(
            db, ENTITY_TYPE, day, "update" if before else "create", before, after
        )
        if record is None:
            return None
        return {field: record[field] for field in change_history.UNDO_REFERENCE_FIELDS}

    def add_illness_in_transaction(
        self,
        db: Any,
        illness: str,
        marker: str = "",
        today: date | None = None,
        symptoms: Any = None,
    ) -> dict[str, str] | None:
        """Add an illness text and note marker to today's check-in, with history.

        Existing answers stay; the new text joins an existing illness text.
        ``symptoms`` replaces the day's selected symptoms when given.
        Returns the undo reference, or None when nothing changed.
        """
        today = today or self._today()
        day = today.isoformat()
        existing = self._repository.get(db, day) or {}
        known = str(existing.get("illness") or "").strip()
        # A "keine"/"nein" answer is no illness text to keep (#3821).
        known = known if reports_complaint(known) else ""
        combined = known or illness
        if known and illness and illness not in known:
            combined = f"{known}; {illness}"
        notes = str(existing.get("notes") or "").strip()
        if marker and marker not in notes:
            notes = f"{notes} · {marker}".strip(" ·")
        checkin = normalize_checkin(
            {
                **existing,
                "checkin_date": day,
                "illness": combined,
                "notes": notes,
                **({} if symptoms is None else {"symptoms": symptoms}),
            },
            today=today,
        )
        return self._write(db, checkin)

    def report_illness(self, day: Any, symptoms: Any = None) -> dict[str, Any]:
        """Record illness for today from the answer "Ich bin krank" (#3821).

        The same check-in record a check-in or Coach turn writes, with its undo
        reference; planning then pauses as for any reported illness. ``day`` is
        the day of the answered note; only today is written. ``symptoms`` is the
        athlete's selection (ids of ``SYMPTOM_LABELS``), stored with the check-in.
        """
        today = self._answer_day(day)
        with self._manager.unit_of_work() as db:
            change = self.add_illness_in_transaction(
                db, INFECTION_HINT_ILLNESS, today=today, symptoms=symptoms
            )
        return self._saved(today.isoformat(), change)

    def dismiss_infection_hint(self, day: Any) -> dict[str, Any]:
        """Hide today's infection hint after "Mir geht's gut" (#3821).

        A hint on a later day shows again; nothing else changes. ``day`` is the
        day of the answered note; only today is written.
        """
        today = self._answer_day(day).isoformat()
        with self._manager.unit_of_work() as db:
            self._set_kv(db, INFECTION_HINT_DISMISSED_KEY, today)
        return {"status": "ok", "dismissed": today}

    def _answer_day(self, day: Any) -> date:
        """Today, when the answered note is today's; a stale view is refused."""
        today = self._today()
        if day != today.isoformat():
            raise AppError(
                409,
                "Der Infekt-Hinweis gilt für einen anderen Tag. Bitte die Ansicht neu laden.",
            )
        return today

    def _save_normalized(
        self,
        checkin: dict[str, Any],
        clear_fields: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        """Save one day; with ``clear_fields`` omitted edit fields keep their value.

        The merge, the write and the change record share one transaction, so a
        concurrent write of the same day cannot be lost.
        """
        with self._manager.unit_of_work() as db:
            if clear_fields is not None:
                existing = self._repository.get(db, checkin["checkin_date"])
                if existing:
                    for field in COACH_PRESERVED_FIELDS:
                        if field not in clear_fields and checkin[field] in (None, ""):
                            checkin[field] = existing[field]
            change = self._write(db, checkin)
        return self._saved(checkin["checkin_date"], change, checkin)

    def _saved(
        self,
        day: str,
        change: dict[str, str] | None,
        fallback: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        saved = next(
            (item for item in self.list(365) if item["checkin_date"] == day),
            fallback,
        )
        return {"status": "ok", "checkin": saved, "change": change}

    def current(
        self, db: Any, entity_id: str
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """The stored check-in of one recorded change, for change history."""
        value = self._stored(db, entity_id)
        return value, change_history.audit_projection(ENTITY_TYPE, value)

    def restore_in_transaction(
        self,
        db: Any,
        entity_id: str,
        current: dict[str, Any] | None,
        target: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Put one day back as recorded, including cleared fields and tags."""
        if target is None:
            # Undo of a first check-in: the day had no row, status or tags.
            self._repository.delete(db, entity_id)
            self._set_kv(db, DAY_STATUS_KEY + entity_id, None)
            self._set_kv(db, TAGS_KEY + entity_id, None)
            self._set_kv(db, COMPLAINTS_KEY + entity_id, None)
            self._set_kv(db, SYMPTOMS_KEY + entity_id, None)
            return None
        restored = {
            **dict.fromkeys(CHECKIN_SCORE_FIELDS + ("available_minutes",)),
            **dict.fromkeys(CHECKIN_TEXT_LIMITS, ""),
            **(current or {}),
            **target,
            "checkin_date": entity_id,
        }
        self._repository.upsert(db, restored)
        for key, field in ((DAY_STATUS_KEY, "day_status"), (TAGS_KEY, "tag_answers")):
            if field in target:
                self._set_kv(db, key + entity_id, target[field])
        # Complaints the target day did not have are removed with the undo.
        self._set_kv(db, COMPLAINTS_KEY + entity_id, target.get("complaints") or None)
        self._set_kv(db, SYMPTOMS_KEY + entity_id, target.get("symptoms") or None)
        return self._stored(db, entity_id)

    def save(self, value: Any) -> dict[str, Any]:
        checkin = normalize_checkin(value, today=self._today())
        return self._save_normalized(checkin)

    def save_coach(self, arguments: Any) -> dict[str, Any]:
        """Save a coach-supplied check-in while preserving omitted edit fields.

        Fields listed in ``clear_fields`` are removed instead of restored; they
        must not also carry a value.
        """
        if not isinstance(arguments, dict):
            raise AppError(400, "Der Tages-Check-in muss als Objekt gesendet werden.")
        value = dict(arguments)
        clear_fields = coach_clear_fields(value.pop("clear_fields", None))
        for field in CHECKIN_SCORE_FIELDS + ("available_minutes",):
            if value.get(field) == -1:
                value[field] = None
        for field in clear_fields:
            if value.get(field) not in (None, ""):
                raise AppError(
                    400,
                    "Ein Check-in-Feld kann nicht zugleich einen Wert erhalten und gelöscht werden.",
                )
        normalized = normalize_checkin(value, today=self._today())
        return {
            "stored_locally": True,
            **self._save_normalized(normalized, clear_fields),
        }

    def context(self) -> dict[str, Any]:
        checkins = self.list()
        today = self._today().isoformat()
        return {
            "today": next(
                (item for item in checkins if item["checkin_date"] == today), None
            ),
            "recent": checkins[:14],
            "scope": "Only athlete-entered subjective feedback and constraints; wearable/provider values remain in their source sections.",
        }

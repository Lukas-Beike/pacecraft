import sqlite3
import threading
import unittest
from datetime import date, timedelta

from backend import change_history
from backend.athlete.checkins import (
    CHECKIN_SCORE_FIELDS,
    CHECKIN_TEXT_LIMITS,
    INFECTION_HINT_ILLNESS,
    CheckinService,
    bounded_minutes,
    bounded_score,
    normalize_checkin,
    recurring_complaints,
)
from backend.canonical.change_detail import day_label
from backend.coach.change_receipts import change_summary, turn_changes
from backend.db.repositories import CheckinRepository
from backend.errors import AppError
from backend.history.service import ChangeHistoryService
from backend.history.undo_service import HistoryUndoService

TODAY = date(2026, 8, 31)


class FakeManager:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            "CREATE TABLE athlete_checkins ("
            "checkin_date TEXT PRIMARY KEY, soreness INTEGER, stress INTEGER, motivation INTEGER, "
            "session_rpe INTEGER, day_form TEXT, illness TEXT, pain TEXT, available_minutes INTEGER, "
            "availability_notes TEXT, notes TEXT, created_at TEXT, updated_at TEXT)"
        )
        self.connection.execute(
            "CREATE TABLE change_history (id TEXT PRIMARY KEY, entity_type TEXT NOT NULL, "
            "entity_id TEXT NOT NULL, action TEXT NOT NULL, source TEXT NOT NULL, "
            "created_at TEXT NOT NULL, before_hash TEXT NOT NULL, after_hash TEXT NOT NULL, "
            "diff TEXT NOT NULL)"
        )
        self.commits = 0
        self.rollbacks = 0

    def unit_of_work(self):
        manager = self

        class UnitOfWork:
            def __enter__(self):
                return manager.connection

            def __exit__(self, exc_type, exc_value, traceback):
                if exc_type is None:
                    manager.connection.commit()
                    manager.commits += 1
                else:
                    manager.connection.rollback()
                    manager.rollbacks += 1
                return False

        return UnitOfWork()

    def read_snapshot(self):
        return self.unit_of_work()

    def close(self):
        self.connection.close()


class FailingCheckinRepository(CheckinRepository):
    def upsert(self, db, checkin):
        super().upsert(db, checkin)
        raise RuntimeError("synthetic write failure")


class RecordingCheckinRepository(CheckinRepository):
    def __init__(self):
        super().__init__(lambda: "2026-08-31T12:00:00+00:00")
        self.limits = []

    def list(self, db, limit=30):
        self.limits.append(limit)
        return []


class FailingListCheckinRepository(CheckinRepository):
    def list(self, db, limit=30):
        raise RuntimeError("synthetic read failure")


class AthleteCheckinTests(unittest.TestCase):
    def setUp(self):
        self.manager = FakeManager()
        with self.manager.unit_of_work() as db:
            db.execute(
                "CREATE TABLE kv(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)"
            )
        self.repository = CheckinRepository(lambda: "2026-08-31T12:00:00+00:00")
        self.service = CheckinService(self.manager, self.repository, lambda: TODAY)

    def tearDown(self):
        self.manager.close()

    def test_public_constants_match_checkin_contract(self):
        self.assertEqual(
            CHECKIN_SCORE_FIELDS, ("soreness", "stress", "motivation", "session_rpe")
        )
        self.assertEqual(
            CHECKIN_TEXT_LIMITS,
            {
                "day_form": 2000,
                "illness": 1000,
                "pain": 1000,
                "availability_notes": 2000,
                "notes": 4000,
            },
        )

    def test_bounded_score_accepts_empty_and_boundaries(self):
        self.assertIsNone(bounded_score(None))
        self.assertIsNone(bounded_score(""))
        self.assertEqual(bounded_score(0), 0)
        self.assertEqual(bounded_score("10"), 10)

    def test_bounded_score_rejects_non_integer_and_out_of_range_values(self):
        # A fraction or a boolean is refused, never cut to a whole (#2135).
        for value in ("not-a-number", 7.9, True, float("nan")):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(
                    AppError, "Lokale Feedback-Werte müssen ganze Zahlen sein"
                ),
            ):
                bounded_score(value)
        self.assertEqual(bounded_score(7.0), 7)
        with self.assertRaisesRegex(
            AppError, "Lokale Feedback-Werte müssen zwischen 0 und 10 liegen"
        ):
            bounded_score(-1)
        with self.assertRaisesRegex(
            AppError, "Lokale Feedback-Werte müssen zwischen 0 und 10 liegen"
        ):
            bounded_score(11)

    def test_bounded_minutes_accepts_empty_and_boundaries(self):
        self.assertIsNone(bounded_minutes(None))
        self.assertIsNone(bounded_minutes(""))
        self.assertEqual(bounded_minutes(0), 0)
        self.assertEqual(bounded_minutes("1440"), 1440)

    def test_bounded_minutes_rejects_non_integer_and_out_of_range_values(self):
        for value in ("not-a-number", 45.5, False):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(
                    AppError, "Die verfügbare Trainingszeit muss eine ganze Zahl sein"
                ),
            ):
                bounded_minutes(value)
        with self.assertRaisesRegex(
            AppError,
            "Die verfügbare Trainingszeit muss zwischen 0 und 1440 Minuten liegen",
        ):
            bounded_minutes(-1)
        with self.assertRaisesRegex(
            AppError,
            "Die verfügbare Trainingszeit muss zwischen 0 und 1440 Minuten liegen",
        ):
            bounded_minutes(1441)

    def test_normalize_checkin_rejects_non_dict(self):
        with self.assertRaisesRegex(
            AppError, "Das lokale Feedback muss ein Objekt sein"
        ):
            normalize_checkin([], today=TODAY)

    def test_normalize_checkin_defaults_date_and_normalizes_values(self):
        result = normalize_checkin(
            {"soreness": "7", "available_minutes": "45", "day_form": "  Müde  "},
            today=TODAY,
        )
        self.assertEqual(result["checkin_date"], "2026-08-31")
        self.assertEqual(result["soreness"], 7)
        self.assertEqual(result["available_minutes"], 45)
        self.assertEqual(result["day_form"], "Müde")
        self.assertIsNone(result["stress"])

    def test_normalize_checkin_rejects_invalid_and_future_dates(self):
        with self.assertRaisesRegex(
            AppError, "Das Datum des lokalen Feedbacks ist ungültig"
        ):
            normalize_checkin({"checkin_date": "not-a-date"}, today=TODAY)
        with self.assertRaisesRegex(
            AppError, "Ein Tages-Check-in kann nicht in der Zukunft liegen"
        ):
            normalize_checkin({"checkin_date": "2026-09-01"}, today=TODAY)
        self.assertEqual(
            normalize_checkin({"checkin_date": "2026-08-30"}, today=TODAY)[
                "checkin_date"
            ],
            "2026-08-30",
        )

    def test_normalize_checkin_trims_and_limits_all_text_fields(self):
        value = {
            field: f"  {'x' * (limit + 1)}  "
            for field, limit in CHECKIN_TEXT_LIMITS.items()
        }
        result = normalize_checkin(value, today=TODAY)
        for field, limit in CHECKIN_TEXT_LIMITS.items():
            self.assertEqual(len(result[field]), limit)
            self.assertEqual(result[field], ("x" * (limit + 1))[:limit])

    def test_normalize_checkin_does_not_mutate_input(self):
        value = {
            "checkin_date": " 2026-08-30 ",
            "soreness": "7",
            "day_form": "  tired  ",
            "unknown": ["unchanged"],
        }
        original = {
            key: item.copy() if isinstance(item, list) else item
            for key, item in value.items()
        }
        normalize_checkin(value, today=TODAY)
        self.assertEqual(value, original)

    def test_list_clamps_limit(self):
        repository = RecordingCheckinRepository()
        service = CheckinService(self.manager, repository, lambda: TODAY)
        self.assertEqual(service.list(0), [])
        self.assertEqual(service.list(999), [])
        self.assertEqual(repository.limits, [1, 365])

    def test_save_upserts_and_returns_saved_row(self):
        result = self.service.save(
            {"soreness": 7, "notes": " tired ", "pain": "left knee"}
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["checkin"]["checkin_date"], TODAY.isoformat())
        self.assertEqual(result["checkin"]["soreness"], 7)
        self.assertEqual(result["checkin"]["notes"], "tired")
        self.assertEqual(self.service.context()["today"]["pain"], "left knee")
        updated = self.service.save({"soreness": 3})
        self.assertEqual(updated["checkin"]["soreness"], 3)

    def test_save_uses_injected_local_date(self):
        service = CheckinService(
            self.manager, self.repository, lambda: date(2026, 8, 30)
        )
        result = service.save({"notes": "yesterday"})
        self.assertEqual(result["checkin"]["checkin_date"], "2026-08-30")

    def test_save_coach_rejects_non_dict(self):
        with self.assertRaisesRegex(
            AppError, "Der Tages-Check-in muss als Objekt gesendet werden"
        ):
            self.service.save_coach([])

    def test_save_coach_merges_omitted_and_minus_one_values(self):
        self.service.save(
            {
                "soreness": 8,
                "stress": 4,
                "available_minutes": 45,
                "day_form": "good",
                "notes": "keep",
            }
        )
        result = self.service.save_coach(
            {"soreness": -1, "stress": 6, "available_minutes": -1, "notes": ""}
        )
        saved = result["checkin"]
        self.assertTrue(result["stored_locally"])
        self.assertEqual(saved["soreness"], 8)
        self.assertEqual(saved["stress"], 6)
        self.assertEqual(saved["available_minutes"], 45)
        self.assertEqual(saved["notes"], "keep")
        self.assertEqual(saved["day_form"], "good")

    def test_save_coach_clear_fields_removes_listed_values_and_keeps_omitted(self):
        self.service.save(
            {
                "soreness": 8,
                "available_minutes": 45,
                "day_form": "good",
                "illness": "Synthetic cold",
                "pain": "Synthetic knee",
                "notes": "keep",
            }
        )
        result = self.service.save_coach(
            {"clear_fields": ["illness", "pain", "available_minutes"]}
        )
        saved = result["checkin"]
        self.assertIsNone(saved["available_minutes"])
        self.assertEqual(saved["illness"], "")
        self.assertEqual(saved["pain"], "")
        self.assertEqual(saved["soreness"], 8)
        self.assertEqual(saved["day_form"], "good")
        self.assertEqual(saved["notes"], "keep")

    def test_save_coach_clear_fields_can_clear_scores_and_texts(self):
        self.service.save({"soreness": 8, "stress": 4, "notes": "old"})
        result = self.service.save_coach(
            {"clear_fields": ["soreness", "notes"], "stress": 5}
        )
        saved = result["checkin"]
        self.assertIsNone(saved["soreness"])
        self.assertEqual(saved["notes"], "")
        self.assertEqual(saved["stress"], 5)

    def test_save_coach_clear_field_with_null_or_empty_value_is_allowed(self):
        self.service.save({"illness": "Synthetic cold", "pain": "Synthetic knee"})
        result = self.service.save_coach(
            {"illness": None, "pain": "", "clear_fields": ["illness", "pain"]}
        )
        self.assertEqual(result["checkin"]["illness"], "")
        self.assertEqual(result["checkin"]["pain"], "")

    def test_save_coach_rejects_clear_field_that_also_has_value(self):
        self.service.save({"illness": "Synthetic cold", "available_minutes": 45})
        for payload in (
            {"illness": "Still ill", "clear_fields": ["illness"]},
            {"available_minutes": 30, "clear_fields": ["available_minutes"]},
        ):
            with (
                self.subTest(payload=payload),
                self.assertRaisesRegex(
                    AppError, "kann nicht zugleich einen Wert erhalten und gelöscht"
                ),
            ):
                self.service.save_coach(payload)
        with self.manager.unit_of_work() as db:
            saved = self.repository.get(db, TODAY.isoformat())
        self.assertEqual(saved["illness"], "Synthetic cold")
        self.assertEqual(saved["available_minutes"], 45)

    def test_save_coach_rejects_invalid_clear_fields(self):
        invalid = (
            "illness",
            ["checkin_date"],
            ["illness", "illness"],
            ["illness", 3],
            ["unknown_field"],
        )
        for clear_fields in invalid:
            with (
                self.subTest(clear_fields=clear_fields),
                self.assertRaisesRegex(
                    AppError, "clear_fields muss eine eindeutige Liste"
                ),
            ):
                self.service.save_coach({"clear_fields": clear_fields})
        with self.manager.unit_of_work() as db:
            self.assertEqual(self.repository.list(db, 365), [])

    def test_context_limits_recent_and_uses_today(self):
        for offset in range(16):
            day = date(2026, 8, 31 - offset)
            self.service.save({"checkin_date": day.isoformat(), "notes": str(offset)})
        context = self.service.context()
        self.assertEqual(context["today"]["checkin_date"], TODAY.isoformat())
        self.assertEqual(len(context["recent"]), 14)
        self.assertEqual(
            context["scope"],
            "Only athlete-entered subjective feedback and constraints; wearable/provider values remain in their source sections.",
        )

    def test_repository_get_returns_focused_row(self):
        self.service.save({"notes": "found"})
        with self.manager.unit_of_work() as db:
            result = self.repository.get(db, TODAY.isoformat())
            missing = self.repository.get(db, "2026-01-01")
        self.assertEqual(result["notes"], "found")
        self.assertIsNone(missing)
        self.assertEqual(
            set(result),
            {
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
            },
        )

    def test_save_rolls_back_failed_upsert(self):
        failing = CheckinService(
            self.manager,
            FailingCheckinRepository(lambda: "2026-08-31T12:00:00+00:00"),
            lambda: TODAY,
        )
        with self.assertRaisesRegex(RuntimeError, "synthetic write failure"):
            failing.save({"notes": "fail"})
        self.assertEqual(self.manager.rollbacks, 1)
        with self.manager.unit_of_work() as db:
            self.assertEqual(self.repository.list(db, 365), [])

    def test_save_preserves_committed_upsert_when_followup_read_fails(self):
        repository = FailingListCheckinRepository(lambda: "2026-08-31T12:00:00+00:00")
        service = CheckinService(self.manager, repository, lambda: TODAY)
        with self.assertRaisesRegex(RuntimeError, "synthetic read failure"):
            service.save({"notes": "committed"})
        with self.manager.unit_of_work() as db:
            saved = self.repository.list(db, 365)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["notes"], "committed")


def _history_rows(manager, action=None):
    with manager.unit_of_work() as db:
        rows = db.execute(
            "SELECT entity_type, entity_id, action FROM change_history ORDER BY rowid"
        ).fetchall()
    return [tuple(row) for row in rows if action in (None, row["action"])]


def _undo(manager, service, changes):
    unused = None
    undo = HistoryUndoService(
        manager,
        ChangeHistoryService(manager, unused, record_services={"checkin": service}),
        *(unused,) * 6,
        record_services={"checkin": service},
        profile_mutation_coordinator=threading.RLock(),
    )
    with manager.unit_of_work() as db:
        return undo.apply_many(db, changes)


class AthleteCheckinHistoryTests(unittest.TestCase):
    def setUp(self):
        self.manager = FakeManager()
        with self.manager.unit_of_work() as db:
            db.execute(
                "CREATE TABLE kv(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)"
            )
        repository = CheckinRepository(lambda: "2026-08-31T12:00:00+00:00")
        self.service = CheckinService(self.manager, repository, lambda: TODAY)

    def tearDown(self):
        self.manager.close()

    def saved(self):
        return next(
            item
            for item in self.service.list(365)
            if item["checkin_date"] == TODAY.isoformat()
        )

    def test_create_update_and_clear_each_record_one_change(self):
        first = self.service.save({"soreness": 4, "notes": "tired"})
        self.assertEqual(first["change"]["action"], "create")
        second = self.service.save_coach({"soreness": 6})
        self.assertEqual(second["change"]["action"], "update")
        cleared = self.service.save_coach({"clear_fields": ["notes"]})
        self.assertEqual(cleared["change"]["action"], "update")
        today = TODAY.isoformat()
        self.assertEqual(
            _history_rows(self.manager),
            [
                ("checkin", today, "create"),
                ("checkin", today, "update"),
                ("checkin", today, "update"),
            ],
        )

    def test_unchanged_save_records_nothing(self):
        self.service.save({"soreness": 4})
        again = self.service.save({"soreness": 4})
        self.assertIsNone(again["change"])
        self.assertEqual(len(_history_rows(self.manager)), 1)

    def test_undo_of_a_first_check_in_removes_it_with_status_and_tags(self):
        with change_history.collect_changes() as changes:
            self.service.save(
                {"soreness": 4, "day_status": "rest", "tag_answers": {"travel": True}}
            )
        _undo(self.manager, self.service, changes)
        self.assertEqual(self.service.list(365), [])
        with self.manager.unit_of_work() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM kv").fetchone()[0], 0)

    def test_undo_of_a_coach_overwrite_restores_cleared_fields_and_tags(self):
        self.service.save(
            {
                "soreness": 3,
                "illness": "Erkältung",
                "pain": "",
                "notes": "Knie",
                "available_minutes": 60,
            }
        )
        before = self.saved()
        with change_history.collect_changes() as changes:
            self.service.save_coach(
                {
                    "soreness": 8,
                    "pain": "Knie",
                    "clear_fields": ["illness", "notes", "available_minutes"],
                    "day_status": "rest",
                    "tag_answers": {"travel": False},
                }
            )
        after = self.saved()
        self.assertEqual(after["illness"], "")
        self.assertEqual(after["pain"], "Knie")
        self.assertEqual(after["day_status"], "rest")
        _undo(self.manager, self.service, changes)
        restored = self.saved()
        self.assertEqual(restored, before)
        self.assertNotIn("day_status", restored)
        self.assertNotIn("tag_answers", restored)

    def test_coach_complaints_are_stored_kept_cleared_and_undone(self):
        # #3766: area, side and severity beside the free pain text.
        self.service.save({"pain": "zieht beim Anlaufen"})
        before = self.saved()
        knee = {"area": "knee", "side": "left", "severity": 2}
        achilles = {"area": "achilles", "side": "right", "severity": 5}
        with change_history.collect_changes() as changes:
            saved = self.service.save_coach({"complaints": [knee, achilles]})
        self.assertEqual(saved["change"]["action"], "update")
        # Sorted by area and side; the free text stays.
        self.assertEqual(self.saved()["complaints"], [achilles, knee])
        self.assertEqual(self.saved()["pain"], "zieht beim Anlaufen")
        # Omitted complaints keep their value; an empty list removes them.
        self.service.save_coach({"soreness": 3})
        self.assertEqual(self.saved()["complaints"], [achilles, knee])
        # Undoing a removal brings the day's complaints back.
        with change_history.collect_changes() as removal:
            self.service.save_coach({"complaints": []})
        self.assertNotIn("complaints", self.saved())
        _undo(self.manager, self.service, removal)
        self.assertEqual(self.saved()["complaints"], [achilles, knee])
        self.service.save_coach({"complaints": []})
        self.assertNotIn("complaints", self.saved())
        with self.manager.unit_of_work() as db:
            self.assertIsNone(
                db.execute(
                    "SELECT value FROM kv WHERE key='checkin_complaints:2026-08-31'"
                ).fetchone()
            )
        self.service.save_coach({"complaints": [knee]})
        with change_history.collect_changes() as changes:
            self.service.save_coach({"complaints": [achilles]})
        _undo(self.manager, self.service, changes)
        self.assertEqual(self.saved()["complaints"], [knee])
        self.assertEqual(before["pain"], self.saved()["pain"])

    def test_complaints_are_validated(self):
        for complaints in (
            [{"area": "elbow", "side": "left", "severity": 3}],
            [{"area": "knee", "side": "middle", "severity": 3}],
            [{"area": "knee", "side": "left", "severity": 11}],
            [{"area": "knee", "side": "left", "severity": 2.5}],
            [{"area": "knee", "side": "left"}],
            [{"area": "knee", "side": "left", "severity": 2}] * 2,
            [
                {"area": "knee", "side": side, "severity": 1}
                for side in ("left", "right", "both", "none")
            ]
            * 3,
            {"area": "knee"},
        ):
            with self.subTest(complaints), self.assertRaises(AppError):
                self.service.save_coach({"complaints": complaints})
        self.assertEqual(self.service.list(365), [])

    def test_undo_is_rejected_when_the_day_changed_since(self):
        self.service.save({"soreness": 3})
        with change_history.collect_changes() as changes:
            self.service.save({"soreness": 4})
        self.service.save({"soreness": 5})
        with self.assertRaises(AppError) as error:
            _undo(self.manager, self.service, changes)
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.saved()["soreness"], 5)

    def test_coach_receipt_offers_undo_for_a_saved_check_in(self):
        with change_history.collect_changes() as changes:
            result = {"ok": True, **self.service.save_coach({"notes": "ok"})}
        receipt = {"tool": "save_checkin", "result": result, "changes": changes}
        summary = change_summary([receipt])
        self.assertTrue(summary["undoable"])
        self.assertEqual(turn_changes([receipt]), changes)
        # The line names the day and what the save did (#3931).
        self.assertEqual(
            summary["details"], [f"Check-in, {day_label(TODAY.isoformat())}: neu"]
        )

    def test_infection_hint_illness_is_the_check_in_record_and_undo_removes_it(self):
        # #3821: "Ich bin krank" writes what a Coach check-in with the same text writes.
        with change_history.collect_changes() as changes:
            reported = self.service.report_illness(TODAY.isoformat())
        self.assertEqual(reported["change"]["action"], "create")
        self.assertEqual(reported["checkin"]["illness"], "Infekt-Hinweis bestätigt")
        self.assertEqual([change["entity_type"] for change in changes], ["checkin"])
        _undo(self.manager, self.service, changes)
        self.assertEqual(self.service.list(365), [])
        coach = self.service.save_coach({"illness": INFECTION_HINT_ILLNESS})
        self.assertEqual(coach["checkin"], reported["checkin"])
        self.assertEqual(
            {**coach["change"], "id": None}, {**reported["change"], "id": None}
        )

    def test_infection_hint_illness_keeps_the_day_answers(self):
        self.service.save({"soreness": 4, "illness": "Halsweh", "notes": "müde"})
        reported = self.service.report_illness(TODAY.isoformat())["checkin"]
        self.assertEqual(reported["illness"], "Halsweh; Infekt-Hinweis bestätigt")
        self.assertEqual((reported["soreness"], reported["notes"]), (4, "müde"))

    def test_infection_hint_illness_replaces_a_no_answer(self):
        # A morning "nein" is no illness text to keep (review of #3821).
        self.service.save({"illness": "nein", "notes": "müde"})
        reported = self.service.report_illness(TODAY.isoformat())["checkin"]
        self.assertEqual(reported["illness"], INFECTION_HINT_ILLNESS)

    def test_an_answer_from_a_stale_day_is_refused(self):
        # A view left open past midnight must not answer the next day's hint.
        yesterday = (TODAY - timedelta(days=1)).isoformat()
        for answer in (
            self.service.report_illness,
            self.service.dismiss_infection_hint,
        ):
            with self.subTest(answer=answer.__name__):
                with self.assertRaises(AppError) as raised:
                    answer(yesterday)
                self.assertEqual(raised.exception.status, 409)
        self.assertEqual(self.service.list(365), [])

    def test_mir_gehts_gut_stores_only_the_day(self):
        self.assertEqual(
            self.service.dismiss_infection_hint(TODAY.isoformat()),
            {"status": "ok", "dismissed": TODAY.isoformat()},
        )
        with self.manager.unit_of_work() as db:
            rows = [tuple(row) for row in db.execute("SELECT key, value FROM kv")]
        self.assertEqual(rows, [("infection_hint_dismissed", '"2026-08-31"')])
        self.assertEqual(self.service.list(365), [])
        self.assertEqual(_history_rows(self.manager), [])


class RecurringComplaintTests(unittest.TestCase):
    """#3766 (athlete): 3 of the last 14 days, or 2+ severity points worse there."""

    @staticmethod
    def day(offset, *complaints):
        return {
            "checkin_date": (TODAY - timedelta(days=offset)).isoformat(),
            "complaints": [
                {"area": area, "side": side, "severity": severity}
                for area, side, severity in complaints
            ],
        }

    def test_three_days_or_two_points_make_a_note_and_older_days_do_not(self):
        rows = [
            self.day(0, ("achilles", "right", 3), ("knee", "left", 4)),
            self.day(5, ("achilles", "right", 3)),
            self.day(13, ("achilles", "right", 2), ("knee", "left", 2)),
            # Outside the 14 days, today included: not counted.
            self.day(14, ("knee", "left", 1), ("calf", "both", 6)),
            self.day(2, ("shin", "none", 3)),
        ]
        found = {
            (item["area"], item["side"]): item
            for item in recurring_complaints(rows, TODAY)
        }
        self.assertEqual(set(found), {("achilles", "right"), ("knee", "left")})
        achilles = found[("achilles", "right")]
        self.assertEqual(
            (achilles["days"], achilles["first_severity"], achilles["last_severity"]),
            (3, 2, 3),
        )
        # Two days, but 2 -> 4 is two points worse.
        knee = found[("knee", "left")]
        self.assertEqual(
            (knee["days"], knee["first_severity"], knee["last_severity"]), (2, 2, 4)
        )

    def test_one_point_worse_on_two_days_is_no_note(self):
        rows = [self.day(0, ("knee", "left", 3)), self.day(3, ("knee", "left", 2))]
        self.assertEqual(recurring_complaints(rows, TODAY), [])


if __name__ == "__main__":
    unittest.main()

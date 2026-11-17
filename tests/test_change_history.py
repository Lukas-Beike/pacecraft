import json
import sqlite3
import unittest
from datetime import UTC, datetime, timedelta

from nutrition_service_support import build_nutrition_services
from support import temp_database

from backend import change_history
from backend.athlete.profile import DEFAULT_PROFILE
from backend.coach import limits as coach_limits
from backend.errors import AppError


class ChangeHistoryTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            "CREATE TABLE change_history ("
            "id TEXT PRIMARY KEY, entity_type TEXT, entity_id TEXT, action TEXT, "
            "source TEXT, created_at TEXT, before_hash TEXT, after_hash TEXT, diff TEXT)"
        )

    def tearDown(self):
        self.db.close()

    def test_projection_keeps_allowlisted_bounded_fields_only(self):
        projected = change_history.audit_projection(
            "profile",
            {
                "name": "Ada",
                "weather_location": "Münster",
                "secret": "never retained",
                "notes": "x" * 5000,
            },
        )
        self.assertEqual(projected, {"name": "Ada", "weather_location": "Münster"})

    def test_record_change_persists_hashes_and_private_diff(self):
        record = change_history.record_change(
            self.db,
            "profile",
            "profile",
            "update",
            {"name": "Before", "secret": "old"},
            {"name": "After", "secret": "new"},
        )
        self.assertIsNotNone(record)
        row = dict(self.db.execute("SELECT * FROM change_history").fetchone())
        self.assertEqual(
            json.loads(row["diff"])["fields"]["name"],
            {
                "before": "Before",
                "after": "After",
            },
        )
        self.assertNotIn("secret", row["diff"])
        self.assertEqual(row["before_hash"], record["before_hash"])

    def test_unchanged_record_is_skipped_but_undo_is_retained(self):
        value = {"name": "Same"}
        self.assertIsNone(
            change_history.record_change(
                self.db, "profile", "profile", "update", value, value
            )
        )
        self.assertIsNotNone(
            change_history.record_change(
                self.db, "profile", "profile", "undo", value, value
            )
        )

    def test_cleanup_and_capacity_preserve_bounds(self):
        old = (datetime.now(UTC) - timedelta(days=181)).isoformat()
        self.db.execute(
            "INSERT INTO change_history VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("old", "profile", "profile", "update", "local", old, "a", "b", "{}"),
        )
        change_history.cleanup(self.db)
        self.assertIsNone(
            self.db.execute("SELECT id FROM change_history WHERE id='old'").fetchone()
        )
        with self.assertRaises(AppError) as raised:
            change_history.reserve_capacity(self.db, change_history.MAX_ROWS + 1)
        self.assertEqual(raised.exception.reason, "change_history_limit")
        # A full history plus one recorded change stays at the cap (#2134).
        recent = datetime.now(UTC) - timedelta(days=1)
        self.db.executemany(
            "INSERT INTO change_history VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    f"row-{index}",
                    "profile",
                    "profile",
                    "update",
                    "local",
                    (recent + timedelta(seconds=index)).isoformat(),
                    "a",
                    "b",
                    "{}",
                )
                for index in range(change_history.MAX_ROWS)
            ],
        )
        change_history.record_change(
            self.db, "profile", "profile", "update", {"name": "A"}, {"name": "B"}
        )
        count = self.db.execute("SELECT COUNT(*) FROM change_history").fetchone()[0]
        self.assertEqual(count, change_history.MAX_ROWS)

    def test_history_capacity_covers_one_complete_plan_replacement(self):
        self.assertGreaterEqual(
            change_history.MAX_ROWS, coach_limits.COACH_TRAINING_CHANGE_LIMIT * 2
        )

    def test_public_view_never_exposes_diff_values(self):
        row = {
            "id": "one",
            "entity_type": "profile",
            "entity_id": "profile",
            "action": "update",
            "source": "local",
            "created_at": "2026-09-20T00:00:00+00:00",
            "before_hash": "before",
            "after_hash": "after",
            "diff": json.dumps(
                {
                    "fields": {
                        "name": {"before": "Ada", "after": "Grace"},
                        "unsafe-field": {"before": "x", "after": "y"},
                    },
                    "before_present": True,
                    "after_present": True,
                }
            ),
        }
        view = change_history.public_view(row)
        self.assertEqual(view["diff"]["fields"], {"name": {"changed": True}})
        self.assertNotIn("Ada", json.dumps(view))
        self.assertEqual(view["remote_sync"], "local_only")

    def test_every_persisted_profile_field_is_audited(self):
        self.assertLessEqual(set(DEFAULT_PROFILE), change_history.PROFILE_FIELDS)

    def test_each_profile_field_alone_records_a_restorable_change(self):
        for field in DEFAULT_PROFILE:
            with self.subTest(field=field):
                before = dict(DEFAULT_PROFILE)
                after = {
                    **before,
                    field: "7.5" if field == "sleep_target_hours" else "x",
                }
                record = change_history.record_change(
                    self.db, "profile", "profile", "update", before, after
                )
                self.assertIsNotNone(record)
                fields = json.loads(record["diff"])["fields"]
                self.assertEqual(
                    fields,
                    {field: {"before": before[field], "after": after[field]}},
                )

    def test_planned_unit_time_only_edit_is_recorded(self):
        before = {
            "id": "u1",
            "date": "2026-09-21",
            "starts_at_local": "2026-09-21T10:00:00",
        }
        after = {**before, "starts_at_local": "2026-09-21T08:30:00"}
        record = change_history.record_change(
            self.db, "planned_unit", "u1", "update", before, after
        )
        self.assertIsNotNone(record)
        self.assertEqual(
            list(json.loads(record["diff"])["fields"]), ["starts_at_local"]
        )

    def test_a_stored_food_change_recorded_before_its_origin_marks_stays_undoable(
        self,
    ):
        value = {"id": "off:1", "basis_amount": 100, "salt_g": 0.4, "filled_fields": {}}
        recorded = {key: item for key, item in value.items() if key != "filled_fields"}
        self.assertIn(
            change_history.audit_hash(recorded),
            change_history.accepted_audit_hashes("food_database_entry", value),
        )

    def test_planning_field_names_before_schema_v16_are_not_translated(self):
        # Schema v21 is the baseline (#1798): history rows written under the
        # former Intervals.icu names are neither matched nor renamed.
        value = {
            "id": "u1",
            "name": "Lauf",
            "starts_at_local": "2026-09-21T10:00:00",
            "planned_duration_s": 3600,
        }
        recorded = {
            "id": "u1",
            "name": "Lauf",
            "start_date_local": "2026-09-21T10:00:00",
            "moving_time": 3600,
        }
        self.assertEqual(
            change_history.accepted_audit_hashes("planned_unit", value),
            {
                change_history.audit_hash(value),
                change_history.audit_hash(
                    {"id": "u1", "name": "Lauf", "planned_duration_s": 3600}
                ),
            },
        )
        self.assertNotIn(
            change_history.audit_hash(recorded),
            change_history.accepted_audit_hashes("planned_unit", value),
        )
        row = {
            "id": "change-1",
            "entity_type": "planned_unit",
            "entity_id": "u1",
            "action": "update",
            "source": "local",
            "created_at": "2026-09-21T08:00:00+00:00",
            "before_hash": "a" * 64,
            "after_hash": "b" * 64,
            "diff": json.dumps(
                {
                    "fields": {
                        "start_date_local": {"before": "x", "after": "y"},
                        "name": {"before": "a", "after": "b"},
                    },
                    "before_present": True,
                    "after_present": True,
                }
            ),
        }
        self.assertEqual(
            change_history.public_view(row)["diff"]["fields"],
            {"start_date_local": {"changed": True}, "name": {"changed": True}},
        )

    def test_public_view_skips_fields_that_are_not_an_object(self):
        for diff in ({"fields": ["name"]}, {"fields": "name"}):
            with self.subTest(diff=diff):
                row = {
                    "id": "change-1",
                    "entity_type": "planned_unit",
                    "entity_id": "u1",
                    "action": "update",
                    "source": "local",
                    "created_at": "2026-09-21T08:00:00+00:00",
                    "before_hash": "a" * 64,
                    "after_hash": "b" * 64,
                    "diff": json.dumps(diff),
                }
                self.assertEqual(change_history.public_view(row)["diff"]["fields"], {})

    def test_legacy_hash_matches_projection_without_added_fields(self):
        value = {"name": "Ada", "sleep_target_hours": "7.5", "fueling_tolerance": "gel"}
        legacy = {"name": "Ada"}
        accepted = change_history.accepted_audit_hashes("profile", value)
        self.assertEqual(
            accepted,
            {
                change_history.audit_hash(value),
                change_history.audit_hash(legacy),
                # Released rows also carried the empty retired weekly plan.
                change_history.audit_hash({**value, "weekly_plan": ""}),
                change_history.audit_hash({**legacy, "weekly_plan": ""}),
            },
        )
        self.assertEqual(
            change_history.accepted_audit_hashes("profile", legacy),
            {
                change_history.audit_hash(legacy),
                change_history.audit_hash({**legacy, "weekly_plan": ""}),
            },
        )
        self.assertEqual(
            change_history.accepted_audit_hashes("competition", value),
            {change_history.audit_hash(value)},
        )
        # A competition recorded before schema v29 had no location (#2803).
        self.assertEqual(
            change_history.accepted_audit_hashes(
                "competition", {**value, "location": ""}
            ),
            {
                change_history.audit_hash({**value, "location": ""}),
                change_history.audit_hash(value),
            },
        )

    def test_each_released_profile_generation_stays_matchable(self):
        value = {
            "name": "Ada",
            "sleep_target_hours": "7.5",
            "fueling_tolerance": "gel",
            "training_preferences_running": "trails",
            "training_preferences_cycling": "",
            "training_preferences_swimming": "",
            "training_preferences_strength": "",
            "training_preferences_mobility": "",
            "training_model": "pyramidal",
        }
        # Rows of released versions carried the empty retired weekly plan.
        released = {**value, "weekly_plan": ""}
        before_training_model = {
            key: item for key, item in value.items() if key != "training_model"
        }
        before_sport_preferences = {
            "name": "Ada",
            "sleep_target_hours": "7.5",
            "fueling_tolerance": "gel",
        }
        accepted = change_history.accepted_audit_hashes("profile", value)
        for generation in (
            value,
            released,
            before_training_model,
            {**before_training_model, "weekly_plan": ""},
            before_sport_preferences,
            {"name": "Ada"},
        ):
            with self.subTest(generation=sorted(generation)):
                self.assertIn(change_history.audit_hash(generation), accepted)
        # A retired plan with content never matches the current projection.
        self.assertNotIn(
            change_history.audit_hash({**value, "weekly_plan": "{}"}), accepted
        )
        self.assertNotIn(
            "weekly_plan", change_history.audit_projection_fields("profile")
        )

    def test_a_profile_row_hashed_before_the_birth_year_stays_undoable(self):
        # #3099: rows of 2.1.10-2.1.14 carry every field up to the first threshold
        # origin but no birth year; the current projection adds it, empty.
        before = {"name": "Ada", "sex": "male", "first_threshold_origin": ""}
        value = change_history.audit_projection("profile", {**before, "birth_year": ""})
        self.assertIn("birth_year", change_history.audit_projection_fields("profile"))
        self.assertIn(
            change_history.audit_hash(before),
            change_history.accepted_audit_hashes("profile", value),
        )

    def test_a_diary_row_recorded_before_schema_v32_stays_undoable(self):
        # #3234: rows before v32 carry no volume and no session; the current
        # projection adds both, empty.
        before = {
            "id": "log",
            "meal_type": "snack",
            "kcal": 100,
            "estimate_uncertain": False,
        }
        value = change_history.audit_projection(
            "nutrition_entry", {**before, "volume_ml": None, "session_id": None}
        )
        fields = change_history.audit_projection_fields("nutrition_entry")
        self.assertTrue({"volume_ml", "session_id"} <= set(fields))
        self.assertIn(
            change_history.audit_hash(before),
            change_history.accepted_audit_hashes("nutrition_entry", value),
        )

    def test_a_product_change_recorded_before_schema_v33_stays_undoable(self):
        # #3452: rows before v33 carry no sizes; the current projection adds
        # all four, empty.
        before = {"id": "prod", "name": "Skyr", "basis_amount": 100, "basis_unit": "g"}
        sizes = ("portion_amount", "portion_unit", "package_amount", "package_unit")
        value = change_history.audit_projection(
            "nutrition_product", {**before, **dict.fromkeys(sizes)}
        )
        self.assertTrue(
            set(sizes)
            <= set(change_history.audit_projection_fields("nutrition_product"))
        )
        self.assertIn(
            change_history.audit_hash(before),
            change_history.accepted_audit_hashes("nutrition_product", value),
        )

    def test_undoing_an_old_basis_change_after_new_sizes_is_stale(self):
        # A pre-v33 row changed the basis to ml; a portion in ml was set since.
        # Its legacy hash still matches, but undo would strand the portion.
        manager = temp_database(self, schema=True)
        now = datetime(2026, 11, 18, 8, tzinfo=UTC)
        catalog = build_nutrition_services(manager, now.isoformat, lambda: now)[
            1
        ].product_catalog
        product = catalog.save_product(
            {"name": "Skyr", "basis_amount": 100, "basis_unit": "ml", "kcal": 63}
        )
        catalog.update_product(
            product["id"], {"portion_amount": 150, "portion_unit": "ml"}
        )
        with manager.unit_of_work() as db, self.assertRaises(AppError) as caught:
            current, _projection = catalog.current(db, product["id"])
            catalog.restore_in_transaction(
                db, product["id"], current, {"basis_unit": "g"}
            )
        self.assertEqual(
            (caught.exception.status, caught.exception.reason),
            (409, "nutrition_product_changed_since"),
        )
        stored = catalog.get_product(product["id"])
        self.assertEqual(
            (stored["basis_unit"], stored["portion_amount"], stored["portion_unit"]),
            ("ml", 150.0, "ml"),
        )

    def test_preferred_sources_are_audited_and_older_rows_stay_matchable(self):
        value = {"name": "Ada", "preferred_source_body": "intervals"}

        projection = change_history.audit_projection("profile", value)
        assert projection is not None
        self.assertEqual(projection["preferred_source_body"], "intervals")
        self.assertIn(
            change_history.audit_hash({"name": "Ada"}),
            change_history.accepted_audit_hashes("profile", value),
        )


if __name__ == "__main__":
    unittest.main()

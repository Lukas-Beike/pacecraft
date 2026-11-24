"""Schema gate: the frozen baseline and released schemas reach the live one; all else stays put."""

import hashlib
import json
import sqlite3
import sys
import tempfile
import textwrap
import unittest
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

# The repository root, for running this file as a script with --write.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from support import temp_database

from backend.canonical.vocabulary import ProviderId, SourceKind
from backend.db import migrations, repositories, schema_history
from backend.db.bootstrap import initialize_application_database
from backend.db.migrations import OlderSchemaError, migrate_schema
from backend.db.repositories import FoodDatabaseEntryRepository, KeyValueRepository
from backend.db.schema import (
    CURRENT_SCHEMA_VERSION,
    SYNC_JOBS_BACKFILL_INDEX_DDL,
    configure_cipher,
    database_schema_is_current,
    database_schema_signature,
    execute_ddl,
    initialize_schema,
)
from backend.db.schema_history import (
    SHAPE_CURRENT,
    SHAPE_SCHEMA_V26,
    SHAPE_SCHEMA_V27,
    SHAPE_SCHEMA_V28,
    SHAPE_SCHEMA_V29,
    SHAPE_SCHEMA_V30,
    SHAPE_SCHEMA_V31,
    SHAPE_SCHEMA_V32,
    SHAPE_SCHEMA_V33,
    released_shape,
    signature_digest,
)
from backend.errors import SchemaRefusedError
from backend.sync.canonical_table_reader import CanonicalTableReader
from backend.sync.state import settle_interrupted_sync_state

INTERVALS = ProviderId.INTERVALS.value
STAMP = "2026-10-16T08:00:00+00:00"
# Schema v26, release 2.0.5: the only baseline, kept as a frozen fixture.
OLDEST_SUPPORTED_VERSION = 26
BASELINE_FIXTURE = Path(__file__).with_name("fixtures") / "schema_v26.sql.txt"
# No released schema precedes the baseline, so the upgrade path runs on a
# stand-in: the live schema one index short, stored with the previous version.
PREVIOUS_SHAPE = "synthetic-previous"
PREVIOUS_VERSION = CURRENT_SCHEMA_VERSION - 1
SCHEMA_REFERENCE = Path(__file__).resolve().parents[1] / "docs" / "schema-reference.md"
REFERENCE_BEGIN = "<!-- BEGIN GENERATED SCHEMA -->\n"
REFERENCE_END = "<!-- END GENERATED SCHEMA -->\n"


def schema_reference() -> str:
    """The current schema as ``docs/schema-reference.md`` carries it."""
    with closing(sqlite3.connect(":memory:")) as db:
        db.row_factory = sqlite3.Row
        initialize_schema(db)
        digest = signature_digest(database_schema_signature(db))
        rows = db.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE sql IS NOT NULL AND name NOT GLOB 'sqlite_*' ORDER BY type, name"
        ).fetchall()
    lines = [
        f"Schema version: `{CURRENT_SCHEMA_VERSION}` (`PRAGMA user_version`).",
        "",
        f"Shape digest: `{digest}`.",
        "",
        (
            f"{sum(row['type'] == 'table' for row in rows)} tables and "
            f"{sum(row['type'] == 'index' for row in rows)} named indexes, in signature order."
        ),
    ]
    for row in rows:
        sql = textwrap.dedent("        " + row["sql"]).strip()
        lines += [
            "",
            f"### {row['type']} `{row['name']}`",
            "",
            "```sql",
            sql + ";",
            "```",
        ]
    return "\n".join(lines) + "\n"


def previous_shape_database(db) -> None:
    """Store the live schema without the backfill index as the previous version."""
    initialize_schema(db)
    db.execute("DROP INDEX idx_sync_jobs_backfill_end")
    db.execute(f"PRAGMA user_version = {PREVIOUS_VERSION}")
    db.commit()


def add_index_step(db) -> None:
    execute_ddl(db, SYNC_JOBS_BACKFILL_INDEX_DDL)


@contextmanager
def registered_previous_shape(
    db, step: Callable[..., None] = add_index_step
) -> Iterator[None]:
    """Make the stand-in shape of ``db`` the baseline, one step below the live schema."""
    digest = signature_digest(database_schema_signature(db))
    with (
        mock.patch.object(
            migrations, "OLDEST_SUPPORTED_SCHEMA_VERSION", PREVIOUS_VERSION
        ),
        mock.patch.dict(
            schema_history.RELEASED_SHAPE_DIGESTS, {PREVIOUS_SHAPE: digest}
        ),
        mock.patch.dict(
            schema_history.ACCEPTED_SHAPES_BY_VERSION,
            {PREVIOUS_VERSION: frozenset({PREVIOUS_SHAPE})},
        ),
        mock.patch.dict(
            migrations.UPGRADE_STEPS, {PREVIOUS_SHAPE: (SHAPE_CURRENT, step)}
        ),
    ):
        yield


EAN_OWN, EAN_OTHER = "4006381333931", "4000000000017"
YAZIO_ID = "yazio:0f0f0f0f-0000-4000-8000-000000000001"
OWN_FOOD_ID = "off:4000000000024"
NUTRIENTS = ("kcal", "carbs_g", "protein_g", "fat_g", "sugar_g", "fiber_g", "salt_g")


# SHA-256 of the baseline's product rows after the v26 to v27 step (#2364).
V27_PRODUCT_ROWS_DIGEST = (
    "f7d7c73f042c17cebc01b0486195189428de376ecd1911b9339533965a2c7595"
)


def mirror_food(food_id: str, unit: str | None, **per_100: float | None) -> dict:
    return {
        "id": food_id,
        "name": "Riegel " + food_id,
        "brands": "Marke",
        "source": "Open Food Facts",
        "source_url": "https://example.test/" + food_id,
        "license": "ODbL",
        "basis_amount": 100,
        "basis_unit": unit,
        "per_100": per_100,
        "quality": "community_label_data",
    }


# Mirrored foods of the baseline: (food, barcode column, fetched_at).
BASELINE_MIRROR = (
    # Newest food with the own product's EAN: fills its empty fields first.
    (
        mirror_food(f"off:{EAN_OWN}", "g", **dict.fromkeys(NUTRIENTS, 1.0)),
        EAN_OWN,
        "2026-10-03",
    ),
    # Older food with the same EAN from Yazio: takes only what is still empty.
    (mirror_food(YAZIO_ID, "g", fiber_g=7.0), EAN_OWN, "2026-10-02"),
    # Unknown unit and a payload from before sugar, fibre and salt were read.
    (
        mirror_food(
            f"off:{EAN_OTHER}", None, kcal=80.0, carbs_g=None, protein_g=2.0, fat_g=1.0
        ),
        EAN_OTHER,
        "2026-10-01",
    ),
    # A food without brand and source page keeps both absent.
    (
        {
            **{
                key: value
                for key, value in mirror_food(
                    YAZIO_ID.replace("1", "2"), "ml", **dict.fromkeys(NUTRIENTS)
                ).items()
                if key != "source_url"
            },
            "brands": None,
        },
        "",
        "2026-10-01",
    ),
    # An own product already holds this food's id: it wins and keeps its row.
    (mirror_food(OWN_FOOD_ID, "g", kcal=99.0), OWN_FOOD_ID[4:], "2026-10-01"),
)


def add_baseline_nutrition_rows(db) -> None:
    """An own product with gaps and mirrored foods of the v26 baseline."""
    db.execute(
        "INSERT INTO nutrition_products(id, barcode, name, brand, basis_amount, "
        "basis_unit, kcal, fat_g, source, created_at, updated_at, favorite) "
        "VALUES ('prod-own', ?, 'Mein Riegel', '', 100, 'g', 500, 30, "
        "'packaging_label', ?, ?, 1)",
        (EAN_OWN, STAMP, STAMP),
    )
    db.execute(
        "INSERT INTO nutrition_products(id, name, basis_amount, basis_unit, source, "
        "created_at, updated_at) VALUES (?, 'Eigenes', 100, 'g', 'manual', ?, ?)",
        (OWN_FOOD_ID, STAMP, STAMP),
    )
    db.executemany(
        "INSERT INTO food_database_entries(food_id, barcode, name, brand, payload, "
        "fetched_at, updated_at) VALUES (?, ?, ?, 'Marke', ?, ?, ?)",
        [
            (food["id"], barcode, food["name"], json.dumps(food), fetched, fetched)
            for food, barcode, fetched in BASELINE_MIRROR
        ],
    )


# Diary rows of the baseline: every meal type, an unknown time, the uncertain
# flag in the basis, unknown macros and a composite basis (#3234).
BASELINE_DIARY = (
    ('log-1', '{}', '2026-10-15', '2026-10-15T07:30:00', 'breakfast',
     'Haferflocken', 450, 70.0, 15.0, 9.0, 1),
    ('log-2', '{"uncertain": true}', '2026-10-15', '2026-10-15T12:00:00', 'lunch',
     'Restaurant', 900, None, None, None, 0),
    ('log-3', '{"kind": "composite", "components": []}', '2026-10-15',
     '2026-10-15T19:10:00', 'dinner', 'Nudeln', 700, 110.0, 25.0, 12.0, 1),
    ('log-4', '{}', '2026-10-16', '2026-10-16T10:00:00', 'snack', 'Gel', 100,
     25.0, 0.0, 0.0, 1),
)  # fmt: skip


def add_baseline_diary_rows(db) -> None:
    db.executemany(
        "INSERT INTO nutrition_logs(id, nutrition_basis, meal_date, logged_at, "
        "meal_type, description, kcal, carbs_g, protein_g, fat_g, "
        "logged_time_known, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(*row, STAMP, STAMP) for row in BASELINE_DIARY],
    )
    # A day already sent to Intervals.icu: the rebuild must not mark it pending.
    db.execute(
        "INSERT INTO nutrition_sync_dates(meal_date, revision, sync_state, "
        "updated_at) VALUES ('2026-10-15', 3, 'synced', ?)",
        (STAMP,),
    )


def add_baseline_activity_rows(db) -> None:
    """A session with its record, a stream set and a lap set with one lap."""
    db.execute(
        "INSERT INTO activity_sessions(activity_id, primary_provider, "
        "primary_provider_activity_id, state, started_at, created_at, updated_at) "
        "VALUES ('act_1', 'garmin', 'g1', 'active', ?, ?, ?)",
        (STAMP, STAMP, STAMP),
    )
    db.execute(
        "INSERT INTO activity_records(provider, provider_activity_id, activity_id, "
        "mapper_version, source_kind, recorded_by, sport, environment, started_at, "
        "load_model, fetched_at, created_at, updated_at) VALUES ('garmin', 'g1', "
        "'act_1', 9, 'provider_reported', 'garmin', 'run', 'outdoor', ?, 'garmin_training_load', ?, ?, ?)",
        (STAMP, STAMP, STAMP, STAMP),
    )
    db.executemany(
        "INSERT INTO activity_detail_sets(provider, provider_activity_id, "
        "detail_kind, mapper_version, origin, source_kind, recorded_by, resolution, "
        "lap_source, content_sha256, fetched_at, created_at, updated_at) VALUES "
        "('garmin', 'g1', ?, 9, 'fetch', 'provider_reported', 'garmin', ?, ?, ?, ?, ?, ?)",
        [
            ("streams", "full", None, "a" * 64, STAMP, STAMP, STAMP),
            ("laps", None, "device_laps", "b" * 64, STAMP, STAMP, STAMP),
        ],
    )
    db.execute(
        "INSERT INTO activity_laps(provider, provider_activity_id, lap_index, kind, "
        "start_offset_s, end_offset_s, avg_hr) VALUES ('garmin', 'g1', 0, 'lap', 0, 600, 150)"
    )


def populated_baseline_database(db) -> None:
    db.executescript(BASELINE_FIXTURE.read_text(encoding="utf-8"))
    db.execute(
        "INSERT INTO messages(role, content, created_at) VALUES ('user', 'Hallo', ?)",
        (STAMP,),
    )
    add_baseline_nutrition_rows(db)
    add_baseline_diary_rows(db)
    add_baseline_activity_rows(db)
    db.commit()


def assert_device_workout_columns_empty(case, db) -> None:
    """Every lap and detail set that existed before schema v31 holds no watch data."""
    case.assertEqual(
        [
            tuple(row)
            for row in db.execute(
                "SELECT avg_hr, workout_step_index, lap_trigger, "
                "avg_respiration_rate_brpm, max_respiration_rate_brpm, "
                "avg_grade_adjusted_speed_mps FROM activity_laps"
            )
        ],
        [(150.0, None, None, None, None, None)],
    )
    case.assertEqual(
        [
            tuple(row)
            for row in db.execute(
                "SELECT detail_kind, workout_steps, device_zones "
                "FROM activity_detail_sets ORDER BY detail_kind"
            )
        ],
        [("laps", None, None), ("streams", None, None)],
    )


# Schema v27 (since 2.1.2) has no fixture of its own: the baseline
# after its product step is that shape, here with calendar events of a feed.
V27_CALENDAR_EVENTS = (
    ("event-1", "team@example.test", "Teammeeting", "2026-10-19T09:00:00", 0, 0),
    ("event-2", "team@example.test", "Teammeeting", "2026-10-26T09:00:00", 0, 0),
    ("event-3", "trip@example.test", "Dienstreise", "2026-10-21T00:00:00", 1, 1),
)


def populated_v27_database(db) -> None:
    populated_baseline_database(db)
    migrations._canonical_products(db)
    db.execute("PRAGMA user_version = 27")
    db.executemany(
        "INSERT INTO external_calendar_events(id, uid, name, event_date, start_local, "
        "end_local, duration_minutes, all_day, training_relevant, no_training, "
        "updated_at) VALUES (?, ?, ?, substr(?, 1, 10), ?, ?, 60, ?, 1, ?, ?)",
        [
            (event_id, uid, name, start, start, start, all_day, blocked, STAMP)
            for event_id, uid, name, start, all_day, blocked in V27_CALENDAR_EVENTS
        ],
    )
    db.commit()


# Schema v28 (since 2.1.10) has no fixture of its own either: v27
# after its decision step, here with a decision and a saved competition.
V28_COMPETITION = (
    "race-1",
    "Stadtlauf",
    "2026-11-28",
    "run",
    "A",
    "21100",
    "1:45:00",
    "km 0-8 flach\nkm 8-11 +4 %",
    "",
    STAMP,
    STAMP,
)


def populated_v28_database(db) -> None:
    populated_v27_database(db)
    migrations._calendar_decisions(db)
    db.execute("PRAGMA user_version = 28")
    db.execute(
        "INSERT INTO external_calendar_decisions VALUES (?, '', 'block', ?)",
        ("trip@example.test", STAMP),
    )
    db.execute(
        "INSERT INTO competitions(id, name, event_date, sport, priority, distance, "
        "target, course_profile, notes, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        V28_COMPETITION,
    )
    db.commit()


# Schema v29, release 2.1.17, has no fixture of its own either: v28 after its
# location step, with the competition's place filled in.
def populated_v29_database(db) -> None:
    populated_v28_database(db)
    migrations._competition_location(db)
    db.execute("PRAGMA user_version = 29")
    db.execute("UPDATE competitions SET location = 'Berlin' WHERE id = 'race-1'")
    db.commit()


# Schema v30, from release 2.1.20, has no fixture of its own either: v29 after its
# curve step, with the stored curve of the session.
def populated_v30_database(db) -> None:
    populated_v29_database(db)
    migrations._session_curves(db)
    db.execute("PRAGMA user_version = 30")
    db.execute(
        "INSERT INTO session_curves VALUES ('act_1', ?, '{}', ?, ?, ?)",
        ("f" * 64, STAMP, STAMP, STAMP),
    )
    db.commit()


# Schema v31, from release 2.1.22, has no fixture of its own either: v30 after
# its watch workout step, with the diary rows of the baseline.
def populated_v31_database(db) -> None:
    populated_v30_database(db)
    migrations._device_workout(db)
    db.execute("PRAGMA user_version = 31")
    db.commit()


# Schema v32, since release 2.1.28, has no fixture of its own either: v31 after
# its diary step, with a drink in a session slot next to the baseline's products.
def populated_v32_database(db) -> None:
    populated_v31_database(db)
    migrations._session_fuelling(db)
    db.execute("PRAGMA user_version = 32")
    db.execute(
        "INSERT INTO nutrition_logs(id, meal_date, logged_at, meal_type, "
        "description, kcal, volume_ml, session_id, created_at, updated_at) VALUES "
        "('drink', '2026-10-16', '2026-10-16T09:00:00', 'during_session', 'Iso', "
        "120, 500, 'act_1', ?, ?)",
        (STAMP, STAMP),
    )
    db.commit()


# Schema v33, since release 2.1.51, has no fixture of its own either: v32
# after its product size step, with two session notes, one rated and marked.
def populated_v33_database(db) -> None:
    populated_v32_database(db)
    migrations._product_sizes(db)
    db.execute("PRAGMA user_version = 33")
    db.executemany(
        "INSERT INTO activity_feedback(activity_id, activity_name, activity_date, "
        "notes, session_rpe, deviation_reason, created_at, updated_at, provider, "
        "purpose) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("act_1", "Lauf", "2026-10-16", "Schwere Beine", 6, "", STAMP, STAMP,
             "garmin", "training"),
            ("act_2", "Rad", "2026-10-17", "", None, "", STAMP, STAMP, None, None),
        ],
    )  # fmt: skip
    db.commit()


def feedback_feel(db) -> list[tuple]:
    return [
        tuple(row)
        for row in db.execute(
            "SELECT activity_id, session_rpe, notes, purpose, feel "
            "FROM activity_feedback ORDER BY activity_id"
        )
    ]


def product_sizes(db) -> list[tuple]:
    return [
        tuple(row)
        for row in db.execute(
            "SELECT id, portion_amount, portion_unit, package_amount, package_unit "
            "FROM nutrition_products ORDER BY id"
        )
    ]


class FailAfter:
    """A connection whose statement starting with ``prefix`` fails once it ran."""

    def __init__(self, db, prefix: str) -> None:
        self._db, self._prefix = db, prefix

    def execute(self, sql, *args):
        result = self._db.execute(sql, *args)
        if sql.lstrip().startswith(self._prefix):
            raise RuntimeError("injected migration failure")
        return result

    def __getattr__(self, name):
        return getattr(self._db, name)


try:
    from sqlcipher3 import dbapi2 as cipher_backend
except ImportError:
    cipher_backend = None


class DatabaseSchemaGateTests(unittest.TestCase):
    def connect(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        self.addCleanup(db.close)
        return db

    def current_database(self):
        db = self.connect()
        initialize_schema(db)
        db.execute(
            "INSERT INTO messages(role, content, created_at) "
            "VALUES ('user', 'Hallo', '2026-10-16T08:00:00Z')"
        )
        db.commit()
        return db

    def assert_rejected_unchanged(self, db):
        before = list(db.iterdump())
        version = db.execute("PRAGMA user_version").fetchone()[0]
        with self.assertRaises(RuntimeError):
            migrate_schema(db)
        self.assertEqual(list(db.iterdump()), before)
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], version)

    def test_current_schema_is_accepted_repeatedly_without_changes(self):
        db = self.current_database()
        before = list(db.iterdump())
        migrate_schema(db)
        migrate_schema(db)
        self.assertEqual(list(db.iterdump()), before)
        self.assertTrue(database_schema_is_current(db))

    def test_the_pre_migration_hook_runs_only_before_an_upgrade(self):
        # #3760: a failed pre-migration backup leaves the database unchanged.
        calls: list[int] = []
        migrate_schema(self.current_database(), calls.append)
        self.assertEqual(calls, [])

        db = self.connect()
        populated_baseline_database(db)
        db.commit()
        before = list(db.iterdump())

        def fail(version: int) -> None:
            calls.append(version)
            raise RuntimeError("backup failed")

        with self.assertRaises(RuntimeError):
            migrate_schema(db, fail)
        self.assertEqual(calls, [OLDEST_SUPPORTED_VERSION])
        self.assertFalse(db.in_transaction)
        self.assertEqual(list(db.iterdump()), before)

        migrate_schema(db, calls.append)
        self.assertEqual(calls, [OLDEST_SUPPORTED_VERSION] * 2)
        self.assertTrue(database_schema_is_current(db))

    def test_populated_baseline_reaches_the_live_schema_with_its_rows(self):
        db = self.connect()
        populated_baseline_database(db)
        self.assertEqual(
            db.execute("PRAGMA user_version").fetchone()[0], OLDEST_SUPPORTED_VERSION
        )
        migrate_schema(db)
        db.commit()
        live = self.connect()
        initialize_schema(live)
        self.assertEqual(database_schema_signature(db), database_schema_signature(live))
        self.assertEqual(
            db.execute("PRAGMA user_version").fetchone()[0], CURRENT_SCHEMA_VERSION
        )
        self.assertTrue(database_schema_is_current(db))
        self.assertEqual(
            db.execute("SELECT content FROM messages").fetchone()[0], "Hallo"
        )
        assert_device_workout_columns_empty(self, db)
        # The canonical reader serves the upgraded activity (#3927).
        (activity,) = CanonicalTableReader().activities(db)
        self.assertEqual(
            (activity.activity_id, activity.provenance.source_kind),
            ("g1", SourceKind.PROVIDER_REPORTED),
        )
        # Every diary row of the baseline, value for value, without volume or
        # session (#3234).
        self.assertEqual(
            [
                tuple(row)
                for row in db.execute(
                    "SELECT id, nutrition_basis, meal_date, logged_at, meal_type, "
                    "description, kcal, carbs_g, protein_g, fat_g, "
                    "logged_time_known, volume_ml, session_id "
                    "FROM nutrition_logs ORDER BY id"
                )
            ],
            [(*row, None, None) for row in BASELINE_DIARY],
        )
        # Every product keeps its row and has no portion or package yet (#3452).
        sizes = product_sizes(db)
        self.assertGreater(len(sizes), 0)
        self.assertEqual({size[1:] for size in sizes}, {(None,) * 4})

    def test_baseline_mirror_rows_become_canonical_products_without_loss(self):
        db = self.connect()
        populated_baseline_database(db)
        migrate_schema(db)
        db.commit()
        own = db.execute(
            "SELECT * FROM nutrition_products WHERE origin = 'own' ORDER BY id DESC"
        ).fetchall()
        self.assertEqual([row["id"] for row in own], ["prod-own", OWN_FOOD_ID])
        self.assertEqual((own[1]["kcal"], own[1]["name"]), (99.0, "Eigenes"))
        self.assertEqual(
            [own[0][key] for key in ("id", "barcode", "brand", "favorite", *NUTRIENTS)],
            ["prod-own", EAN_OWN, "Marke", 1, 500, 1.0, 1.0, 30, 1.0, 1.0, 1.0],
        )
        self.assertEqual(
            set(json.loads(own[0]["details"])["filled_fields"]),
            {"brand", "carbs_g", "protein_g", "sugar_g", "fiber_g", "salt_g"},
        )
        entries = FoodDatabaseEntryRepository()
        # Every mirrored food stays stored with all its fields; the own
        # product's EAN lists none of them as a separate entry.
        for food, barcode, fetched in BASELINE_MIRROR[:-1]:
            with self.subTest(food=food["id"]):
                stored = entries.get(db, food["id"])
                self.assertEqual(stored.pop("fetched_at"), fetched)
                expected = dict(food)
                if barcode and food["id"].startswith("yazio:"):
                    expected["barcode"] = barcode
                self.assertEqual(stored, expected)
        self.assertIsNone(entries.get(db, OWN_FOOD_ID))
        self.assertEqual(
            {entry["id"] for entry in entries.search(db, "")},
            {f"off:{EAN_OTHER}", YAZIO_ID.replace("1", "2")},
        )
        self.assertEqual(
            [
                tuple(row)
                for row in db.execute(
                    "SELECT origin, COUNT(*) FROM nutrition_products GROUP BY origin"
                )
            ],
            [("open_food_facts", 2), ("own", 2), ("yazio", 2)],
        )

    def test_v27_product_step_is_frozen_against_live_repository_changes(self):
        # #2364: the step neither calls the live product repositories nor
        # moves with them; its rows match the result recorded for schema v27
        # by the step before it was frozen.
        source = Path(migrations.__file__).read_text(encoding="utf-8")
        for live in (
            "backend.db.repositories",
            "NUTRITION_PRODUCTS_DDL",
            "NUTRIENT_COLUMNS",
        ):
            self.assertNotIn(live, source)
        db = self.connect()
        populated_baseline_database(db)
        # A 50 g own product: scaled fills, a passed-on origin, another unit.
        ean_half = "4000000000031"
        db.execute(
            "INSERT INTO nutrition_products(id, barcode, name, basis_amount, basis_unit, "
            "source, created_at, updated_at) VALUES ('prod-half', ?, 'Halb', 50, 'g', "
            "'manual', ?, ?)",
            (ean_half, STAMP, STAMP),
        )
        passed_on = {
            **mirror_food(f"off:{ean_half}", "g", kcal=200.0, fat_g=3.0),
            "filled_fields": {"kcal": {"origin": "bls", "fetched_at": "2026-09-01"}},
        }
        other_unit = mirror_food(YAZIO_ID.replace("1", "3"), "ml", protein_g=5.0)
        db.executemany(
            "INSERT INTO food_database_entries(food_id, barcode, name, brand, payload, "
            "fetched_at, updated_at) VALUES (?, ?, ?, 'Marke', ?, ?, ?)",
            [
                (food["id"], ean_half, food["name"], json.dumps(food), day, day)
                for food, day in ((passed_on, "2026-10-04"), (other_unit, "2026-10-05"))
            ],
        )
        with (
            mock.patch.object(
                repositories.FoodDatabaseEntryRepository,
                "upsert",
                side_effect=AssertionError,
            ),
            mock.patch.object(
                repositories.NutritionProductRepository,
                "fill_from_database_entries",
                side_effect=AssertionError,
            ),
            mock.patch.object(
                repositories, "fill_empty_product_fields", side_effect=AssertionError
            ),
        ):
            migrate_schema(db)
        rows = [
            dict(row)
            for row in db.execute("SELECT * FROM nutrition_products ORDER BY id")
        ]
        # The v33 step adds empty sizes; the v27 rows stay frozen without them.
        for row in rows:
            sizes = [row.pop(key) for key in repositories.PRODUCT_SIZE_FIELDS]
            self.assertEqual(sizes, [None] * 4)
        half = next(row for row in rows if row["id"] == "prod-half")
        # 200 kcal and 3 g fat per 100 g fill 50 g; the ml food fills no nutrient.
        self.assertEqual(
            (half["kcal"], half["fat_g"], half["protein_g"], half["brand"]),
            (100.0, 1.5, None, "Marke"),
        )
        self.assertEqual(
            json.loads(half["details"])["filled_fields"]["kcal"]["origin"], "bls"
        )
        self.assertEqual(
            hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest(),
            V27_PRODUCT_ROWS_DIGEST,
        )

    def test_failing_baseline_upgrade_rolls_back_and_a_retry_succeeds(self):
        db = self.connect()
        populated_baseline_database(db)
        before = list(db.iterdump())
        store = migrations._v27_store_food
        calls = []

        def failing_store(*args, **kwargs):
            calls.append(1)
            if len(calls) == 3:
                raise RuntimeError("injected migration failure")
            return store(*args, **kwargs)

        with (
            mock.patch.object(migrations, "_v27_store_food", failing_store),
            self.assertRaisesRegex(RuntimeError, "injected"),
        ):
            migrate_schema(db)
        db.rollback()
        self.assertEqual(list(db.iterdump()), before)
        migrate_schema(db)
        db.commit()
        self.assertTrue(database_schema_is_current(db))

    def test_schema_reference_is_current(self):
        document = SCHEMA_REFERENCE.read_text(encoding="utf-8")
        self.assertEqual(
            document.split(REFERENCE_BEGIN, 1)[1].split(REFERENCE_END, 1)[0],
            schema_reference(),
            "Regenerate with: python tests/test_db_migrations.py --write",
        )

    def test_frozen_baseline_fixture_is_the_recorded_production_shape(self):
        db = self.connect()
        db.executescript(BASELINE_FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(
            released_shape(database_schema_signature(db)), SHAPE_SCHEMA_V26
        )

    def test_older_or_newer_schema_version_is_rejected_without_modification(self):
        for version in (0, 1, OLDEST_SUPPORTED_VERSION - 1, CURRENT_SCHEMA_VERSION + 1):
            with self.subTest(version=version):
                db = self.current_database()
                db.execute(f"PRAGMA user_version = {version}")
                db.commit()
                self.assert_rejected_unchanged(db)

    def test_older_or_newer_database_file_is_refused_at_startup_byte_identical(self):
        # A release before 2.0.5 wrote a schema below 26: startup names the
        # release to upgrade with first. A newer schema is refused as well; both
        # leave the file byte-identical (#3927).
        for version, refusal, text in (
            (OLDEST_SUPPORTED_VERSION - 1, OlderSchemaError, "Release 2.0.5"),
            (CURRENT_SCHEMA_VERSION + 1, SchemaRefusedError, "nicht unterstützt"),
        ):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as root:
                path = Path(root) / "refused.db"
                with closing(sqlite3.connect(path)) as db:
                    db.executescript(BASELINE_FIXTURE.read_text(encoding="utf-8"))
                    db.execute(
                        "INSERT INTO messages(role, content, created_at) "
                        "VALUES ('user', 'Hallo', ?)",
                        (STAMP,),
                    )
                    db.execute(f"PRAGMA user_version = {version}")
                    db.commit()
                before = path.read_bytes()
                manager = temp_database(self, path=path)
                try:
                    with (
                        self.assertRaisesRegex(refusal, text),
                        manager.unit_of_work() as db,
                    ):
                        initialize_application_database(
                            db,
                            key_values=KeyValueRepository(lambda: STAMP),
                            now=STAMP,
                            current_time=datetime.now(UTC),
                            default_profile_json="{}",
                            settle_interrupted=settle_interrupted_sync_state,
                            retention_days=-1,
                        )
                finally:
                    manager.close()
                self.assertEqual(path.read_bytes(), before)

    def test_unknown_schema_shape_is_rejected_without_modification(self):
        changes = (
            "ALTER TABLE messages ADD COLUMN unknown TEXT",
            "CREATE TABLE unknown_table(id INTEGER PRIMARY KEY)",
            (
                "CREATE TRIGGER unknown_trigger AFTER INSERT ON messages "
                "BEGIN SELECT 1; END"
            ),
        )
        for change in changes:
            with self.subTest(change=change):
                db = self.current_database()
                db.execute(change)
                db.commit()
                self.assert_rejected_unchanged(db)

    def test_remote_links_reject_duplicate_remote_ids_per_provider_and_kind(self):
        db = self.connect()
        initialize_schema(db)
        insert = (
            "INSERT INTO remote_links(entity_kind, local_id, provider, remote_id, "
            "sync_state) VALUES (?, ?, ?, ?, 'synced')"
        )
        db.execute(insert, ("competition", "1", INTERVALS, "9"))
        db.execute(insert, ("competition", "2", "other-provider", "9"))
        db.execute(insert, ("library_workout", "1", INTERVALS, "9"))
        db.execute(insert, ("competition", "3", INTERVALS, None))
        db.execute(insert, ("competition", "4", INTERVALS, None))
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(insert, ("competition", "5", INTERVALS, "9"))

    def table_columns(self, db, tables):
        return {
            table: [row[1] for row in db.execute(f"PRAGMA table_info({table})")]
            for table in tables
        }

    def table_rows(self, db, tables, columns=None):
        # With the columns of a released schema, the rows still compare every
        # original value after an upgrade that adds columns.
        rows = {}
        for table in tables:
            selected = (
                ", ".join(f'"{name}"' for name in columns[table]) if columns else "*"
            )
            # The app's rows are dicts (#3466): compare their values, not keys.
            rows[table] = [
                tuple(row.values() if isinstance(row, dict) else row)
                for row in db.execute(f"SELECT {selected} FROM {table}")
            ]
        return rows

    def user_tables(self, db):
        return sorted(
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        )

    def test_a_previous_shape_upgrades_in_one_step_with_its_rows(self):
        db = self.connect()
        previous_shape_database(db)
        db.execute(
            "INSERT INTO messages(role, content, created_at) "
            "VALUES ('user', 'Hallo', '2026-10-16T08:00:00Z')"
        )
        db.commit()
        with registered_previous_shape(db):
            migrate_schema(db)
        db.commit()
        self.assertTrue(database_schema_is_current(db))
        self.assertEqual(
            db.execute("PRAGMA user_version").fetchone()[0], CURRENT_SCHEMA_VERSION
        )
        self.assertEqual(
            db.execute("SELECT content FROM messages").fetchone()[0], "Hallo"
        )

    def test_lost_savepoint_does_not_mask_the_original_upgrade_error(self):
        # The step fails after its change, or first ends the transaction or
        # drops the savepoint as an engine error can; cleanup keeps the error.
        for statement in ("", "ROLLBACK", "RELEASE schema_migration"):
            with self.subTest(statement=statement):
                db = self.connect()
                previous_shape_database(db)
                before = list(db.iterdump())

                def failing_step(connection, statement=statement):
                    add_index_step(connection)
                    if statement:
                        connection.execute(statement)
                    raise RuntimeError("injected migration failure")

                with (
                    registered_previous_shape(db, failing_step),
                    self.assertRaisesRegex(RuntimeError, "injected"),
                ):
                    migrate_schema(db)
                db.rollback()
                self.assertEqual(list(db.iterdump()), before)
                self.assertEqual(
                    db.execute("PRAGMA user_version").fetchone()[0], PREVIOUS_VERSION
                )

    def test_upgraded_database_restarts_with_the_same_build_unchanged(self):
        for populate in (
            populated_baseline_database,
            populated_v27_database,
            populated_v28_database,
            populated_v29_database,
            populated_v30_database,
            populated_v31_database,
            populated_v32_database,
            populated_v33_database,
        ):
            with (
                self.subTest(populate=populate.__name__),
                tempfile.TemporaryDirectory() as root,
            ):
                path = Path(root) / "upgrade.db"
                db = sqlite3.connect(path)
                db.row_factory = sqlite3.Row
                try:
                    populate(db)
                    migrate_schema(db)
                    db.commit()
                    upgraded = list(db.iterdump())
                finally:
                    db.close()
                reopened = sqlite3.connect(path)
                reopened.row_factory = sqlite3.Row
                try:
                    migrate_schema(reopened)
                    self.assertEqual(list(reopened.iterdump()), upgraded)
                    self.assertEqual(
                        reopened.execute("PRAGMA user_version").fetchone()[0],
                        CURRENT_SCHEMA_VERSION,
                    )
                finally:
                    reopened.close()

    def test_frozen_v27_shape_is_the_baseline_after_its_product_step(self):
        db = self.connect()
        db.executescript(BASELINE_FIXTURE.read_text(encoding="utf-8"))
        migrations._canonical_products(db)
        self.assertEqual(
            released_shape(database_schema_signature(db)), SHAPE_SCHEMA_V27
        )

    def test_populated_v27_database_gains_the_decision_table_with_every_row(self):
        db = self.connect()
        populated_v27_database(db)
        tables = self.user_tables(db)
        columns = self.table_columns(db, tables)
        before = self.table_rows(db, tables, columns)
        migrate_schema(db)
        db.commit()
        self.assertTrue(database_schema_is_current(db))
        self.assertEqual(
            db.execute("PRAGMA user_version").fetchone()[0], CURRENT_SCHEMA_VERSION
        )
        self.assertEqual(self.table_rows(db, tables, columns), before)
        assert_device_workout_columns_empty(self, db)
        self.assertEqual(
            self.user_tables(db),
            sorted([*tables, "external_calendar_decisions", "session_curves"]),
        )
        self.assertEqual(
            db.execute("SELECT COUNT(*) FROM external_calendar_decisions").fetchone()[
                0
            ],
            0,
        )
        insert = "INSERT INTO external_calendar_decisions VALUES (?, ?, ?, ?)"
        db.execute(insert, ("team@example.test", "", "none", STAMP))
        for row in (
            ("team@example.test", "", "block", STAMP),
            ("team@example.test", "2026-10-19T09:00:00", "maybe", STAMP),
            ("", "", "none", STAMP),
        ):
            with self.subTest(row=row), self.assertRaises(sqlite3.IntegrityError):
                db.execute(insert, row)

    def test_failing_step_rolls_back_the_released_database_and_a_retry_succeeds(self):
        # Each released schema's own step fails after its DDL ran: the database
        # keeps its version, shape and rows, and the next start upgrades it.
        for populate, version, shape, step in (
            (
                populated_v27_database,
                27,
                SHAPE_SCHEMA_V27,
                migrations._calendar_decisions,
            ),
            (
                populated_v28_database,
                28,
                SHAPE_SCHEMA_V28,
                migrations._competition_location,
            ),
            (populated_v29_database, 29, SHAPE_SCHEMA_V29, migrations._session_curves),
            (
                populated_v30_database,
                30,
                SHAPE_SCHEMA_V30,
                migrations._device_workout,
            ),
            (
                populated_v31_database,
                31,
                SHAPE_SCHEMA_V31,
                migrations._session_fuelling,
            ),
        ):
            with self.subTest(version=version):
                db = self.connect()
                populate(db)
                before = list(db.iterdump())

                def failing_step(connection, step=step):
                    step(connection)
                    raise RuntimeError("injected migration failure")

                next_shape = migrations.UPGRADE_STEPS[shape][0]
                with (
                    mock.patch.dict(
                        migrations.UPGRADE_STEPS, {shape: (next_shape, failing_step)}
                    ),
                    self.assertRaisesRegex(RuntimeError, "injected"),
                ):
                    migrate_schema(db)
                db.rollback()
                self.assertEqual(list(db.iterdump()), before)
                self.assertEqual(
                    db.execute("PRAGMA user_version").fetchone()[0], version
                )
                migrate_schema(db)
                db.commit()
                self.assertTrue(database_schema_is_current(db))

    def test_frozen_v28_shape_is_v27_after_its_decision_step(self):
        db = self.connect()
        db.executescript(BASELINE_FIXTURE.read_text(encoding="utf-8"))
        migrations._canonical_products(db)
        migrations._calendar_decisions(db)
        self.assertEqual(
            released_shape(database_schema_signature(db)), SHAPE_SCHEMA_V28
        )

    def test_schema_history_lists_the_releases_of_every_opened_schema(self):
        # The one release-to-schema table (#3130): a new schema adds its row there.
        history = SCHEMA_REFERENCE.with_name("schema-history.md").read_text("utf-8")
        table = history.split("<!-- schema-releases -->", 1)[1].split("\n\n", 1)[0]
        listed = {int(row.split("|")[1]) for row in table.splitlines()[3:]}
        self.assertLessEqual(
            set(
                range(
                    migrations.OLDEST_SUPPORTED_SCHEMA_VERSION,
                    CURRENT_SCHEMA_VERSION + 1,
                )
            ),
            listed,
        )

    def test_frozen_v29_shape_is_v28_after_its_location_step(self):
        db = self.connect()
        db.executescript(BASELINE_FIXTURE.read_text(encoding="utf-8"))
        migrations._canonical_products(db)
        migrations._calendar_decisions(db)
        migrations._competition_location(db)
        self.assertEqual(
            released_shape(database_schema_signature(db)), SHAPE_SCHEMA_V29
        )

    def test_populated_v29_database_gains_an_empty_curve_table_with_every_row(self):
        db = self.connect()
        populated_v29_database(db)
        tables = self.user_tables(db)
        columns = {
            table: [row[1] for row in db.execute(f"PRAGMA table_info({table})")]
            for table in tables
        }
        before = self.table_rows(db, tables, columns)
        migrate_schema(db)
        db.commit()
        self.assertTrue(database_schema_is_current(db))
        self.assertEqual(
            db.execute("PRAGMA user_version").fetchone()[0], CURRENT_SCHEMA_VERSION
        )
        self.assertEqual(self.user_tables(db), sorted([*tables, "session_curves"]))
        self.assertEqual(self.table_rows(db, tables, columns), before)
        self.assertEqual(
            db.execute("SELECT COUNT(*) FROM session_curves").fetchone()[0], 0
        )
        assert_device_workout_columns_empty(self, db)
        # A stored curve belongs to a live session and carries a SHA-256 fingerprint.
        db.execute("PRAGMA foreign_keys = ON")
        curve = "INSERT INTO session_curves VALUES (?, ?, '{}', ?, ?, ?)"
        db.execute(curve, ("act_1", "f" * 64, STAMP, STAMP, STAMP))
        for row in (
            ("act_1", "short", STAMP, STAMP, STAMP),
            ("act_2", "f" * 64, STAMP, STAMP, STAMP),
        ):
            with self.subTest(row=row), self.assertRaises(sqlite3.IntegrityError):
                db.execute(curve.replace("INSERT", "INSERT OR REPLACE"), row)

    def test_frozen_v30_shape_is_v29_after_its_curve_step(self):
        db = self.connect()
        db.executescript(BASELINE_FIXTURE.read_text(encoding="utf-8"))
        migrations._canonical_products(db)
        migrations._calendar_decisions(db)
        migrations._competition_location(db)
        migrations._session_curves(db)
        self.assertEqual(
            released_shape(database_schema_signature(db)), SHAPE_SCHEMA_V30
        )

    def test_populated_v30_database_gains_empty_watch_workout_columns_with_every_row(
        self,
    ):
        db = self.connect()
        populated_v30_database(db)
        tables = self.user_tables(db)
        columns = self.table_columns(db, tables)
        before = self.table_rows(db, tables, columns)
        migrate_schema(db)
        db.commit()
        self.assertTrue(database_schema_is_current(db))
        self.assertEqual(
            db.execute("PRAGMA user_version").fetchone()[0], CURRENT_SCHEMA_VERSION
        )
        self.assertEqual(self.user_tables(db), tables)
        self.assertEqual(self.table_rows(db, tables, columns), before)
        assert_device_workout_columns_empty(self, db)
        # Each header column belongs to its own set kind and holds its JSON type.
        sets = "UPDATE activity_detail_sets SET {} = ? WHERE detail_kind = ?"
        for column, kind, value in (
            ("workout_steps", "laps", "[]"),
            ("device_zones", "streams", '{"max_hr": 190}'),
        ):
            db.execute(sets.format(column), (value, kind))
        for column, kind, value in (
            ("workout_steps", "streams", "[]"),
            ("workout_steps", "laps", "{}"),
            ("workout_steps", "laps", "[1"),
            ("device_zones", "laps", "{}"),
            ("device_zones", "streams", "[]"),
            ("device_zones", "streams", "{"),
        ):
            with (
                self.subTest(column=column, kind=kind, value=value),
                self.assertRaises(sqlite3.IntegrityError),
            ):
                db.execute(sets.format(column), (value, kind))
        # A lap's step index stays within 0 to 999 and its trigger within 1 to 32 characters.
        lap = "UPDATE activity_laps SET {} = ?"
        for column, value in (
            ("workout_step_index", 999),
            ("lap_trigger", "x" * 32),
            ("avg_respiration_rate_brpm", 18.5),
        ):
            db.execute(lap.format(column), (value,))
        for column, value in (
            ("workout_step_index", -1),
            ("workout_step_index", 1000),
            ("lap_trigger", ""),
            ("lap_trigger", "x" * 33),
        ):
            with (
                self.subTest(column=column, value=value),
                self.assertRaises(sqlite3.IntegrityError),
            ):
                db.execute(lap.format(column), (value,))

    def test_frozen_v31_shape_is_v30_after_its_watch_workout_step(self):
        db = self.connect()
        db.executescript(BASELINE_FIXTURE.read_text(encoding="utf-8"))
        migrations._canonical_products(db)
        migrations._calendar_decisions(db)
        migrations._competition_location(db)
        migrations._session_curves(db)
        migrations._device_workout(db)
        self.assertEqual(
            released_shape(database_schema_signature(db)), SHAPE_SCHEMA_V31
        )

    def test_populated_v31_diary_keeps_every_row_and_gains_session_columns(self):
        db = self.connect()
        populated_v31_database(db)
        tables = self.user_tables(db)
        columns = self.table_columns(db, tables)
        before = self.table_rows(db, tables, columns)
        migrate_schema(db)
        db.commit()
        self.assertTrue(database_schema_is_current(db))
        self.assertEqual(
            db.execute("PRAGMA user_version").fetchone()[0], CURRENT_SCHEMA_VERSION
        )
        self.assertEqual(self.user_tables(db), tables)
        self.assertEqual(self.table_rows(db, tables, columns), before)
        self.assertEqual(
            [tuple(r) for r in db.execute(
                "SELECT id, volume_ml, session_id FROM nutrition_logs ORDER BY id")],
            [(row[0], None, None) for row in BASELINE_DIARY],
        )  # fmt: skip
        self.assertEqual(
            [tuple(r) for r in db.execute(
                "SELECT meal_date, revision, sync_state FROM nutrition_sync_dates")],
            [("2026-10-15", 3, "synced")],
        )  # fmt: skip
        self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        # The indexes are the frozen v32 ones, not leftovers of the v31 table.
        self.assertEqual(
            {
                row[0]: " ".join(row[1].split())
                for row in db.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type = 'index' "
                    "AND tbl_name = 'nutrition_logs' AND sql IS NOT NULL"
                )
            },
            {
                "idx_nutrition_logs_date": "CREATE INDEX idx_nutrition_logs_date ON "
                "nutrition_logs(meal_date, logged_at DESC)",
                "idx_nutrition_logs_session": "CREATE INDEX "
                "idx_nutrition_logs_session ON nutrition_logs(session_id) WHERE "
                "session_id IS NOT NULL",
            },
        )
        insert = (
            "INSERT INTO nutrition_logs(id, meal_date, logged_at, meal_type, "
            "description, kcal, volume_ml, session_id, created_at, updated_at) "
            "VALUES (?, '2026-10-17', '2026-10-17T09:00:00', ?, 'x', 0, ?, ?, ?, ?)"
        )
        for row in (
            ("new-1", "pre_session", None, "act_1"),
            ("new-2", "during_session", 750.0, "act_1"),
            ("new-3", "during_session", 0.0, None),
            # Text that is a number takes the column's REAL affinity.
            ("new-4", "during_session", "500", None),
        ):
            db.execute(insert, (*row, STAMP, STAMP))
        for row in (
            ("bad-1", "snack", None, "act_1"),
            ("bad-2", "during_session", -1.0, None),
            ("bad-3", "second_breakfast", None, None),
            # A volume must be a number, and a session id never empty.
            ("bad-4", "during_session", "viel", None),
            ("bad-5", "during_session", None, ""),
        ):
            with self.subTest(row=row), self.assertRaises(sqlite3.IntegrityError):
                db.execute(insert, (*row, STAMP, STAMP))

    def test_diary_rebuild_failing_at_any_point_leaves_v31_unchanged(self):
        # After the rename, the copy, the drop, and at the final validation.
        for prefix in (
            "ALTER TABLE nutrition_logs RENAME",
            "INSERT INTO nutrition_logs(",
            "DROP TABLE nutrition_logs_v31",
            None,
        ):
            with self.subTest(after=prefix or "final validation"):
                db = self.connect()
                populated_v31_database(db)
                before = list(db.iterdump())
                if prefix is None:
                    with (
                        mock.patch.object(
                            migrations,
                            "database_schema_is_current",
                            side_effect=[False, False],
                        ),
                        self.assertRaisesRegex(RuntimeError, "validiert"),
                    ):
                        migrate_schema(db)
                else:
                    with self.assertRaisesRegex(RuntimeError, "injected"):
                        migrate_schema(FailAfter(db, prefix))
                db.rollback()
                self.assertEqual(list(db.iterdump()), before)
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 31)
                migrate_schema(db)
                db.commit()
                self.assertTrue(database_schema_is_current(db))

    def test_frozen_v32_shape_is_v31_after_its_diary_step(self):
        db = self.connect()
        populated_v32_database(db)
        self.assertEqual(
            released_shape(database_schema_signature(db)), SHAPE_SCHEMA_V32
        )

    def test_populated_v32_products_keep_every_row_and_gain_empty_sizes(self):
        db = self.connect()
        populated_v32_database(db)
        tables = self.user_tables(db)
        columns = self.table_columns(db, tables)
        before = self.table_rows(db, tables, columns)
        products = [
            row[0]
            for row in db.execute("SELECT id FROM nutrition_products ORDER BY id")
        ]
        migrate_schema(db)
        db.commit()
        self.assertTrue(database_schema_is_current(db))
        self.assertEqual(
            db.execute("PRAGMA user_version").fetchone()[0], CURRENT_SCHEMA_VERSION
        )
        self.assertEqual(self.user_tables(db), tables)
        self.assertEqual(self.table_rows(db, tables, columns), before)
        self.assertEqual(
            product_sizes(db), [(id_, None, None, None, None) for id_ in products]
        )
        self.assertEqual(
            tuple(
                db.execute(
                    "SELECT volume_ml, session_id FROM nutrition_logs WHERE id = 'drink'"
                ).fetchone()
            ),
            (500.0, "act_1"),
        )
        self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        product = products[0]
        update = (
            "UPDATE nutrition_products SET portion_amount = ?, portion_unit = ?, "
            "package_amount = ?, package_unit = ? WHERE id = ?"
        )
        for sizes in ((150, "g", 450.0, "g"), (None, None, 1000, "ml"), (None,) * 4):
            db.execute(update, (*sizes, product))
        for sizes in (
            (150, None, None, None),  # an amount needs its unit
            (None, "g", None, None),  # and a unit its amount
            (0, "g", None, None),
            (-5, "g", None, None),
            (None, None, "viel", "g"),
            (150, "portion", None, None),
            (None, None, 450, "kg"),
        ):
            with self.subTest(sizes=sizes), self.assertRaises(sqlite3.IntegrityError):
                db.execute(update, (*sizes, product))

    def test_product_size_step_failing_leaves_v32_unchanged(self):
        # After the first and the last new column, and at the final validation.
        for prefix in (
            "ALTER TABLE nutrition_products ADD COLUMN portion_amount",
            "ALTER TABLE nutrition_products ADD COLUMN package_unit",
            None,
        ):
            with self.subTest(after=prefix or "final validation"):
                db = self.connect()
                populated_v32_database(db)
                before = list(db.iterdump())
                if prefix is None:
                    with (
                        mock.patch.object(
                            migrations,
                            "database_schema_is_current",
                            side_effect=[False, False],
                        ),
                        self.assertRaisesRegex(RuntimeError, "validiert"),
                    ):
                        migrate_schema(db)
                else:
                    with self.assertRaisesRegex(RuntimeError, "injected"):
                        migrate_schema(FailAfter(db, prefix))
                db.rollback()
                self.assertEqual(list(db.iterdump()), before)
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 32)
                migrate_schema(db)
                db.commit()
                self.assertTrue(database_schema_is_current(db))

    def test_frozen_v33_shape_is_v32_after_its_product_size_step(self):
        db = self.connect()
        populated_v33_database(db)
        self.assertEqual(
            released_shape(database_schema_signature(db)), SHAPE_SCHEMA_V33
        )

    def test_populated_v33_feedback_keeps_every_row_and_gains_an_empty_feel(self):
        # #4199: the athlete's feel per session, 1 (very strong) to 5 (very
        # weak); every stored note keeps its values and has no feel yet.
        db = self.connect()
        populated_v33_database(db)
        tables = self.user_tables(db)
        columns = self.table_columns(db, tables)
        before = self.table_rows(db, tables, columns)
        migrate_schema(db)
        db.commit()
        self.assertTrue(database_schema_is_current(db))
        self.assertEqual(
            db.execute("PRAGMA user_version").fetchone()[0], CURRENT_SCHEMA_VERSION
        )
        self.assertEqual(self.user_tables(db), tables)
        self.assertEqual(self.table_rows(db, tables, columns), before)
        self.assertEqual(
            feedback_feel(db),
            [
                ("act_1", 6.0, "Schwere Beine", "training", None),
                ("act_2", None, "", None, None),
            ],
        )
        self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        update = "UPDATE activity_feedback SET feel = ? WHERE activity_id = 'act_1'"
        for feel in (1, 3, 5, None):
            db.execute(update, (feel,))
        # INTEGER affinity stores the text "3" as 3; other text and fractions stay out.
        for feel in (0, 6, -1, 2.5, "gut", "3.5"):
            with self.subTest(feel=feel), self.assertRaises(sqlite3.IntegrityError):
                db.execute(update, (feel,))

    def test_feedback_feel_step_failing_leaves_v33_unchanged(self):
        # After the new column, and at the final validation.
        for prefix in ("ALTER TABLE activity_feedback ADD COLUMN feel", None):
            with self.subTest(after=prefix or "final validation"):
                db = self.connect()
                populated_v33_database(db)
                before = list(db.iterdump())
                if prefix is None:
                    with (
                        mock.patch.object(
                            migrations,
                            "database_schema_is_current",
                            side_effect=[False, False],
                        ),
                        self.assertRaisesRegex(RuntimeError, "validiert"),
                    ):
                        migrate_schema(db)
                else:
                    with self.assertRaisesRegex(RuntimeError, "injected"):
                        migrate_schema(FailAfter(db, prefix))
                db.rollback()
                self.assertEqual(list(db.iterdump()), before)
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 33)
                migrate_schema(db)
                db.commit()
                self.assertTrue(database_schema_is_current(db))

    def test_populated_v28_database_gains_an_empty_location_with_every_row(self):
        db = self.connect()
        populated_v28_database(db)
        tables = self.user_tables(db)
        columns = {
            table: [row[1] for row in db.execute(f"PRAGMA table_info({table})")]
            for table in tables
        }
        before = self.table_rows(db, tables, columns)
        migrate_schema(db)
        db.commit()
        self.assertTrue(database_schema_is_current(db))
        self.assertEqual(
            db.execute("PRAGMA user_version").fetchone()[0], CURRENT_SCHEMA_VERSION
        )
        self.assertEqual(self.user_tables(db), sorted([*tables, "session_curves"]))
        self.assertEqual(self.table_rows(db, tables, columns), before)
        assert_device_workout_columns_empty(self, db)
        self.assertEqual(
            [tuple(row) for row in db.execute("SELECT id, location FROM competitions")],
            [("race-1", "")],
        )
        db.execute("UPDATE competitions SET location = 'Berlin' WHERE id = 'race-1'")
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(
                "UPDATE competitions SET location = ? WHERE id = 'race-1'", ("x" * 201,)
            )
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("UPDATE competitions SET location = NULL WHERE id = 'race-1'")

    def test_version_and_shape_must_belong_together(self):
        previous = self.connect()
        previous_shape_database(previous)
        with registered_previous_shape(previous):
            current = self.current_database()
            current.execute(f"PRAGMA user_version = {PREVIOUS_VERSION}")
            current.commit()
            self.assert_rejected_unchanged(current)
            previous.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
            previous.commit()
            self.assert_rejected_unchanged(previous)

    @unittest.skipIf(
        cipher_backend is None, "SQLCipher requires the Docker runtime on Windows"
    )
    def test_encrypted_current_database_reopens_with_same_key(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "encrypted.db"
            options = {
                "backend": cipher_backend,
                "password": "synthetic-migration-test-key",
                "configure": configure_cipher,
            }
            manager = temp_database(self, path=path, **options)
            try:
                with manager.unit_of_work() as db:
                    initialize_schema(db)
                    migrate_schema(db)
            finally:
                manager.close()
            reopened = temp_database(self, path=path, **options)
            try:
                with reopened.unit_of_work() as db:
                    migrate_schema(db)
                    self.assertTrue(database_schema_is_current(db))
                    self.assertEqual(
                        db.execute("PRAGMA cipher_integrity_check").fetchall(), []
                    )
            finally:
                reopened.close()
            self.assertNotEqual(path.read_bytes()[:16], b"SQLite format 3\x00")

    @unittest.skipIf(
        cipher_backend is None, "SQLCipher requires the Docker runtime on Windows"
    )
    def test_encrypted_released_database_upgrades_and_restarts_with_its_rows(self):
        for populate in (
            populated_v27_database,
            populated_v28_database,
            populated_v29_database,
            populated_v30_database,
            populated_v31_database,
            populated_v32_database,
            populated_v33_database,
        ):
            with self.subTest(populate=populate.__name__):
                self.assert_encrypted_upgrade(populate)

    def assert_encrypted_upgrade(self, populate):
        password = "synthetic-migration-test-key"
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "encrypted-released.db"
            db = cipher_backend.connect(str(path))
            db.row_factory = cipher_backend.Row
            try:
                configure_cipher(db, password)
                populate(db)
                tables = self.user_tables(db)
                columns = {
                    table: [row[1] for row in db.execute(f"PRAGMA table_info({table})")]
                    for table in tables
                }
                before = self.table_rows(db, tables, columns)
            finally:
                db.close()
            options = {
                "backend": cipher_backend,
                "password": password,
                "configure": configure_cipher,
            }
            for _start in range(2):
                manager = temp_database(self, path=path, **options)
                try:
                    with manager.unit_of_work() as db:
                        migrate_schema(db)
                        self.assertTrue(database_schema_is_current(db))
                        self.assertEqual(self.table_rows(db, tables, columns), before)
                        self.assertEqual(
                            db.execute("PRAGMA cipher_integrity_check").fetchall(), []
                        )
                finally:
                    manager.close()
            self.assertNotEqual(path.read_bytes()[:16], b"SQLite format 3\x00")


if __name__ == "__main__":
    if "--write" in sys.argv:
        before, rest = SCHEMA_REFERENCE.read_text(encoding="utf-8").split(
            REFERENCE_BEGIN, 1
        )
        SCHEMA_REFERENCE.write_text(
            before
            + REFERENCE_BEGIN
            + schema_reference()
            + REFERENCE_END
            + rest.split(REFERENCE_END, 1)[1],
            encoding="utf-8",
            newline="\n",
        )
    else:
        unittest.main()

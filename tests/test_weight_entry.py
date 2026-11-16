"""Weights the athlete enters by hand, stored as athlete-entered observations (#3252)."""

import unittest
from datetime import date

from support import temp_database

from backend.athlete.weight_entry import WeightEntryService
from backend.canonical.records import MetricValue, Observation, Provenance
from backend.canonical.vocabulary import (
    MetricKind,
    ProviderId,
    RecordingOrigin,
    SourceKind,
)
from backend.db import canonical_store
from backend.errors import AppError
from backend.nutrition.energy_inputs import weighins
from backend.providers.canonical_plans import current_observation_mapper_versions
from backend.sync.canonical_table_reader import CanonicalTableReader

TODAY = date(2026, 11, 16)


class WeightEntryServiceTests(unittest.TestCase):
    def setUp(self):
        # The app's connections return rows as dicts, not sqlite3.Row.
        self.manager = temp_database(
            self,
            schema=True,
            row_factory=lambda cursor, row: dict(
                zip([column[0] for column in cursor.description], row, strict=True)
            ),
        )
        self.service = WeightEntryService(
            self.manager, now=lambda: "2026-11-16T10:00:00+00:00", today=lambda: TODAY
        )

    def served(self):
        with self.manager.reader() as db:
            rows = canonical_store.current_observation_rows(
                db, "weight", current_observation_mapper_versions()
            )
        return [(row["observed_on"], row["value"], row["provider"]) for row in rows]

    def test_an_entry_is_stored_without_a_provider_and_read_as_a_weigh_in(self):
        read = self.service.save({"date": "2026-11-15", "kg": "71,8"})
        self.assertEqual({"entries": [{"date": "2026-11-15", "kg": 71.8}]}, read)
        # The reader allowlists entered weight (ENTERED_METRICS), labelled "Lokal".
        self.assertEqual([("2026-11-15", 71.8, None)], self.served())
        with self.manager.reader() as db:
            label = db.execute(
                "SELECT label, source_kind FROM observations WHERE metric = 'weight'"
            ).fetchone()
        self.assertEqual({"label": "Lokal", "source_kind": "athlete_entered"}, label)

    def test_a_second_entry_of_the_same_day_replaces_the_first(self):
        self.service.save({"date": "2026-11-15", "kg": 72})
        read = self.service.save({"date": "2026-11-15", "kg": 71.4})
        self.assertEqual([{"date": "2026-11-15", "kg": 71.4}], read["entries"])
        self.assertEqual([("2026-11-15", 71.4, None)], self.served())

    def test_a_confirmed_removal_retires_the_entry(self):
        self.service.save({"date": "2026-11-15", "kg": 72})
        with self.assertRaises(AppError):
            self.service.remove({"date": "2026-11-15"})
        self.assertEqual(
            {"entries": []},
            self.service.remove({"date": "2026-11-15", "confirmed": True}),
        )
        self.assertEqual([], self.served())
        with self.assertRaises(AppError) as missing:
            self.service.remove({"date": "2026-11-15", "confirmed": True})
        self.assertEqual(404, missing.exception.status)

    def test_energy_weigh_ins_take_the_entry_over_a_measurement_of_its_day(self):
        # The athlete set the day's weight (decision 2026-11-16): a provider
        # weigh-in of the same day does not replace it.
        self.service.save({"date": "2026-11-14", "kg": 72.5})
        self.service.save({"date": "2026-11-15", "kg": 72.0})
        garmin = Observation(
            kind=MetricKind.WEIGHT,
            value=MetricValue(71.2, "kg"),
            observed_on=date(2026, 11, 15),
            observed_at=None,
            provenance=Provenance(
                provider=ProviderId.GARMIN,
                source_kind=SourceKind.DEVICE_MEASURED,
                source_record_id="2026-11-15",
                recorded_by=RecordingOrigin.GARMIN,
                retrieved_at=None,
                label=None,
            ),
        )
        with self.manager.unit_of_work() as db:
            canonical_store.upsert_observation(
                db,
                garmin,
                mapper_version=current_observation_mapper_versions()["garmin"],
                now="2026-11-16T10:00:00+00:00",
            )
        with self.manager.reader() as db:
            read = weighins(CanonicalTableReader().observations(db, MetricKind.WEIGHT))
        self.assertEqual({date(2026, 11, 14): 72.5, date(2026, 11, 15): 72.0}, read)

    def test_bad_input_is_refused(self):
        for payload in (
            {"date": "2026-11-17", "kg": 70},  # in the future
            {"date": "kein Datum", "kg": 70},
            {"date": "2026-11-15", "kg": "viel"},
            {"date": "2026-11-15", "kg": 19.9},  # below the canonical 20 kg
            {"date": "2026-11-15", "kg": 400.1},
        ):
            with self.subTest(payload=payload), self.assertRaises(AppError):
                self.service.save(payload)


if __name__ == "__main__":
    unittest.main()

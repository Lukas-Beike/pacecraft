import re
import subprocess
import sys
import unittest
from pathlib import Path

from support import (
    IntervalsRequestRecorder,
    RecordedIntervalsClient,
    parsed_workout_fixture,
)


class SupportFixtureTests(unittest.TestCase):
    def test_workout_fixture_has_the_provider_shape(self):
        workout = parsed_workout_fixture(
            600, sport="Run", kind="hr", units="hr_zone", value=1
        )
        self.assertEqual(workout["type"], "Run")
        self.assertEqual(workout["workout_doc"]["steps"][0]["hr"]["units"], "hr_zone")

    def test_recorded_intervals_client_records_safe_mutation_metadata(self):
        recorder = IntervalsRequestRecorder()
        client = RecordedIntervalsClient(recorder)
        client.create_library_workouts(
            [
                {
                    "name": "Synthetic",
                    "description": "",
                    "type": "Ride",
                    "target": "AUTO",
                }
            ]
        )
        self.assertEqual(recorder.mutations[0]["method"], "POST")
        self.assertEqual(recorder.mutations[0]["path"], "/athlete/0/workouts")
        self.assertEqual(recorder.mutations[0]["payload_count"], 1)

    def test_server_test_support_import_never_reads_the_dotenv_file(self):
        script = """
import os, sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
read = Path.read_text
def deny(path, *args, **kwargs):
    if path.name == '.env':
        raise AssertionError('Environment file access forbidden')
    return read(path, *args, **kwargs)
with patch.object(Path, 'read_text', deny):
    import server_test_support
"""
        result = subprocess.run(
            [sys.executable, "-c", script, str(Path(__file__).parent)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


# Test modules whose temporary databases use another row factory than the app's,
# with the reason (#3466). Production reads dicts, so code tested against
# sqlite3.Row can pass on a row type it never meets (#3435).
OTHER_ROW_FACTORIES = {
    "test_db_manager.py": "tests the connection manager itself with positional reads",
}


class TempDatabaseRowFactoryTests(unittest.TestCase):
    def test_temporary_databases_read_rows_like_the_app_unless_listed(self):
        overriding = set()
        for path in sorted(Path(__file__).parent.glob("test_*.py")):
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(r"temp_database\(", text):
                depth, end = 1, match.end()
                while depth:
                    depth += {"(": 1, ")": -1}.get(text[end], 0)
                    end += 1
                factory = re.search(
                    r"row_factory\s*=\s*([\w.]+)", text[match.end() : end]
                )
                if factory and factory[1] not in {
                    "row_factory",
                    "production_row_factory",
                }:
                    overriding.add(path.name)
        self.assertEqual(overriding, set(OTHER_ROW_FACTORIES))


if __name__ == "__main__":
    unittest.main()

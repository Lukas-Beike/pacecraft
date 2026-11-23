"""The competition conflict route settles a sync conflict for the athlete (#4021)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from support import dispatch_route

from backend.errors import AppError
from backend.http_api.competition_conflict_post import CompetitionConflictPostRoutes

ID = "12345678-1234-5678-1234-567812345678"
PATH = f"/api/competitions/{ID}/resolve"


class CompetitionConflictPostRoutesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conflicts = Mock()
        self.conflicts.resolve_local.return_value = {"ok": True, "status": "resolved"}
        self.routes = CompetitionConflictPostRoutes(lambda: self.conflicts)

    def post(self, payload):
        handler = SimpleNamespace(
            read_json=Mock(return_value=payload), send_json=Mock()
        )
        self.assertTrue(dispatch_route(self.routes, handler, PATH, method="POST"))
        return handler.send_json.call_args.args

    def test_keep_local_needs_no_confirmation(self) -> None:
        status, body = self.post({"strategy": "keep_local"})

        self.assertEqual((200, "resolved"), (status, body["status"]))
        self.conflicts.resolve_local.assert_called_once_with(ID, "keep_local")

    def test_adopting_the_remote_values_needs_the_confirmation(self) -> None:
        with self.assertRaises(AppError) as raised:
            self.post({"strategy": "adopt_remote"})
        self.assertEqual("confirmation_required", raised.exception.reason)
        self.conflicts.resolve_local.assert_not_called()

        self.post({"strategy": "adopt_remote", "confirmed": True})

        self.conflicts.resolve_local.assert_called_once_with(ID, "adopt_remote")

    def test_other_bodies_are_refused(self) -> None:
        for payload, message in (
            ({"strategy": "later"}, "Ungültige Konfliktstrategie."),
            ({"strategy": "keep_local", "id": "x"}, "Unbekannte Felder: id."),
            (["keep_local"], "Die Anfrage muss ein Objekt sein."),
        ):
            with self.assertRaises(AppError) as raised:
                self.post(payload)
            self.assertEqual(
                (400, message), (raised.exception.status, raised.exception.message)
            )
        self.conflicts.resolve_local.assert_not_called()


if __name__ == "__main__":
    unittest.main()

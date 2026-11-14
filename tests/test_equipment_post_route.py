import unittest
from unittest.mock import Mock

from support import dispatch_route

from backend.errors import AppError
from backend.http_api.equipment_post import EquipmentPostRoutes


class EquipmentPostRoutesTests(unittest.TestCase):
    def test_maintenance_post_delegates_to_equipment_service(self):
        service = Mock()
        service.maintain.return_value = {"ok": True}
        handler = Mock()
        handler.read_json.return_value = {"equipment_id": "a", "date": "2026-10-07"}

        handled = dispatch_route(
            EquipmentPostRoutes(lambda: service),
            handler,
            "/api/equipment/maintenance",
            method="POST",
        )

        self.assertTrue(handled)
        service.maintain.assert_called_once_with(
            {"equipment_id": "a", "date": "2026-10-07"}
        )
        handler.send_json.assert_called_once_with(200, {"ok": True})

    def test_save_and_assign_delegate_with_a_bounded_body(self):
        # The athlete edits and assigns equipment without the Coach (#2879).
        for path, method, body in (
            ("/api/equipment", "save", {"name": "Bike", "status": "active"}),
            (
                "/api/equipment/assignment",
                "assign",
                {"activity_id": "act", "equipment_id": None},
            ),
        ):
            with self.subTest(path=path):
                service = Mock()
                getattr(service, method).return_value = {"ok": True}
                handler = Mock()
                handler.read_json.return_value = dict(body)
                self.assertTrue(
                    dispatch_route(
                        EquipmentPostRoutes(lambda bound=service: bound),
                        handler,
                        path,
                        method="POST",
                    )
                )
                handler.read_json.assert_called_once_with(16_384)
                getattr(service, method).assert_called_once_with(body)
                handler.send_json.assert_called_once_with(200, {"ok": True})

    def test_retiring_needs_an_explicit_confirmation_in_the_request(self):
        for confirmed, expected in (
            (None, 409),
            (False, 409),
            ("true", 409),
            (True, 200),
        ):
            with self.subTest(confirmed=confirmed):
                service = Mock()
                service.save.return_value = {"ok": True}
                handler = Mock()
                body = {"id": "a", "status": "archived"}
                handler.read_json.return_value = (
                    body if confirmed is None else {**body, "confirmed": confirmed}
                )
                try:
                    dispatch_route(
                        EquipmentPostRoutes(lambda bound=service: bound),
                        handler,
                        "/api/equipment",
                        method="POST",
                    )
                    status = 200
                except AppError as error:
                    status = error.status
                self.assertEqual(status, expected)
                if expected == 200:
                    # The confirmation is the route's; the service gets the item only.
                    service.save.assert_called_once_with(body)
                else:
                    service.save.assert_not_called()

    def test_other_paths_are_not_handled(self):
        handler = Mock()
        self.assertFalse(
            dispatch_route(
                EquipmentPostRoutes(Mock()), handler, "/api/other", method="POST"
            )
        )
        handler.read_json.assert_not_called()


if __name__ == "__main__":
    unittest.main()

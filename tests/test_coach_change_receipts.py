"""Contracts for the visible change summary of a Coach turn."""

from __future__ import annotations

import unittest
from typing import Any

from backend.canonical.change_detail import change_detail, detail_line
from backend.coach.change_receipts import change_summary, turn_changes
from backend.coach.request_payload import model_confirmed_steps

_PROFILE_CHANGE = {
    "id": "change-1",
    "entity_type": "profile",
    "action": "update",
    "after_hash": "hash-1",
}
_LIBRARY_CHANGE = {
    "id": "change-2",
    "entity_type": "workout_library",
    "action": "create",
    "after_hash": "hash-2",
}


def _step(tool, result=None, changes=None):
    step = {"tool": tool, "result": {"ok": True} if result is None else result}
    if changes:
        step["changes"] = changes
    return step


class ChangeSummaryTests(unittest.TestCase):
    def test_accepted_blocking_finding_is_a_warning(self) -> None:
        summary = change_summary(
            [
                _step(
                    "apply_training_patch",
                    {
                        "ok": True,
                        "plan_check_notes": [
                            {
                                "code": "illness_hard",
                                "dates": ["2026-11-02"],
                                "evidence": {},
                            }
                        ],
                    },
                )
            ]
        )
        assert summary is not None
        self.assertEqual(summary["plan_check_notes"][0]["severity"], "warning")

    def test_plan_notes_are_safe_and_only_attached_to_successful_writes(self) -> None:
        note = {
            "code": "run_spike",
            "dates": ["2026-11-02", "unsafe"],
            "evidence": {"severity": "warning", "provider_text": "private"},
            "message": "private",
        }
        summary = change_summary(
            [
                _step(
                    "apply_training_patch",
                    {"ok": True, "plan_check_notes": [note, note]},
                ),
                _step(
                    "apply_training_patch", {"ok": False, "plan_check_notes": [note]}
                ),
            ]
        )
        assert summary is not None
        self.assertEqual(
            summary["plan_check_notes"],
            [{"code": "run_spike", "dates": ["2026-11-02"], "severity": "warning"}],
        )
        self.assertNotIn("private", str(summary))

    def test_applied_tracked_writes_are_listed_once_and_undoable(self) -> None:
        summary = change_summary(
            [
                _step("update_profile", changes=[_PROFILE_CHANGE]),
                _step("update_profile", changes=[_PROFILE_CHANGE]),
                _step(
                    "manage_training_templates",
                    {"ok": True, "templates": [{"id": "a"}, {"id": "b"}]},
                    [_LIBRARY_CHANGE],
                ),
                _step(
                    "fill_nutrition_product_gaps",
                    {
                        "ok": True,
                        "changes": [
                            {
                                "id": "p1",
                                "name": "Riegel",
                                "written": [
                                    {"field": "salt_g", "value": 0.4, "origin": "web"},
                                    {"field": "kcal", "value": 540, "origin": "yazio"},
                                    {"field": "fat_g", "value": 9, "origin": "manual"},
                                    {"field": "fiber_g", "value": 2, "origin": "bls"},
                                ],
                            }
                        ],
                    },
                    [_PROFILE_CHANGE | {"entity_type": "nutrition_product"}],
                ),
            ]
        )

        self.assertEqual(
            summary,
            {
                "labels": [
                    "Profil aktualisiert",
                    "Trainingsvorlagen bearbeitet (2)",
                    "Produktlücken gefüllt (1)",
                    "Riegel: Salz 0,4 (Web), Energie 540 (Yazio), Fett 9 (eigene Angabe), Ballaststoffe 2 (BLS)",
                ],
                "undoable": True,
                "undone_at": None,
            },
        )

    def test_a_write_without_history_makes_the_whole_turn_not_undoable(
        self,
    ) -> None:
        summary = change_summary(
            [
                _step("update_profile", changes=[_PROFILE_CHANGE]),
                _step("save_checkin"),
            ]
        )

        assert summary is not None
        self.assertEqual(
            summary["labels"], ["Profil aktualisiert", "Tages-Check-in gespeichert"]
        )
        self.assertFalse(summary["undoable"])

    def test_a_write_with_untracked_effects_is_listed_without_turn_undo(
        self,
    ) -> None:
        summary = change_summary(
            [
                _step("update_profile", changes=[_PROFILE_CHANGE]),
                _step("apply_training_patch", changes=[_LIBRARY_CHANGE]),
            ]
        )

        assert summary is not None
        self.assertEqual(len(summary["labels"]), 2)
        self.assertFalse(summary["undoable"])

    def test_a_card_free_sync_lists_its_remote_removals_without_turn_undo(
        self,
    ) -> None:
        removal = "Wird bei Intervals.icu entfernt: Lauf 13.11.2026"
        summary = change_summary(
            [
                _step(
                    "start_intervals_plan_sync",
                    {
                        "ok": True,
                        "status": "queued",
                        "sync_job_id": "job-1",
                        "remote_removals": [removal, removal],
                    },
                )
            ]
        )

        assert summary is not None
        # Two alike deletions stay two lines.
        self.assertEqual(summary["labels"][1:], [removal, removal])
        self.assertFalse(summary["undoable"])

    def test_an_applied_history_undo_is_listed_and_blocks_turn_undo(self) -> None:
        summary = change_summary(
            [
                _step("update_profile", changes=[_PROFILE_CHANGE]),
                # The status the tool really returns (#2134, E-501).
                _step("undo_training_change", {"ok": True, "status": "undone"}),
            ]
        )

        self.assertEqual(
            summary,
            {
                "labels": ["Profil aktualisiert", "Lokale Änderung zurückgenommen"],
                "undoable": False,
                "undone_at": None,
            },
        )

    def test_reads_failures_proposals_and_previews_are_not_changes(self) -> None:
        self.assertIsNone(
            change_summary(
                [
                    _step("get_training_context"),
                    _step("update_profile", {"ok": False, "error": "x"}),
                    _step(
                        "save_nutrition_template",
                        {"ok": True, "status": "approval_required"},
                    ),
                    _step("cancel_coach_request", {"ok": True, "status": "cancelled"}),
                    _step("preview_adaptive_replan", {"ok": True, "id": "preview"}),
                    _step("stage_training_plan"),
                    _step("undo_training_change", {"ok": True, "proposed_action": {}}),
                    _step("save_competition", "malformed"),
                    {"tool": "save_competition"},
                ]
            )
        )

    def test_turn_changes_keep_record_order_and_ignore_malformed_entries(
        self,
    ) -> None:
        receipts: list[Any] = [
            _step("update_profile", changes=[_PROFILE_CHANGE]),
            "malformed",
            {"tool": "x", "changes": "malformed"},
            _step("manage_training_templates", changes=[_LIBRARY_CHANGE]),
        ]

        self.assertEqual(turn_changes(receipts), [_PROFILE_CHANGE, _LIBRARY_CHANGE])

    def test_turn_changes_skip_metric_export_rows_of_a_refresh(self) -> None:
        export = {
            "id": "export-1",
            "entity_type": "metric_export",
            "action": "create",
            "after_hash": "hash-3",
        }
        receipts = [
            _step("start_provider_refresh", changes=[export]),
            _step("update_profile", changes=[_PROFILE_CHANGE]),
        ]

        self.assertEqual(turn_changes(receipts), [_PROFILE_CHANGE])

    def test_a_change_names_its_record_and_the_values_it_replaced(self) -> None:
        update = _LIBRARY_CHANGE | {
            "entity_type": "planned_unit",
            "action": "update",
            "detail": {
                "subject": "Langer Lauf",
                "date": "2026-11-22",
                "fields": [["duration_minutes", 80, 85]],
            },
        }
        created = _LIBRARY_CHANGE | {
            "entity_type": "nutrition_template",
            "detail": {"subject": "Haferbrei", "date": "", "fields": []},
        }
        summary = change_summary(
            [
                _step("apply_training_patch", changes=[update]),
                _step("save_nutrition_template", changes=[created, created]),
            ]
        )

        assert summary is not None
        self.assertEqual(
            summary["details"],
            ["Langer Lauf, So 22.11.: Dauer 80 → 85 min", "Haferbrei: neu"],
        )

    def test_a_turn_without_undo_says_why(self) -> None:
        planning = change_summary([_step("apply_training_patch")])
        mixed = change_summary([_step("apply_training_patch"), _step("save_checkin")])
        tracked = change_summary([_step("update_profile", changes=[_PROFILE_CHANGE])])

        assert planning is not None and mixed is not None and tracked is not None
        self.assertEqual(planning["undo_note"], "Rückgängig im Kalender")
        self.assertEqual(
            mixed["undo_note"],
            "Nicht rückgängig zu machen: Geplante Einheiten angepasst"
            " · Tages-Check-in gespeichert",
        )
        self.assertNotIn("undo_note", tracked)

    def test_a_removal_that_was_not_carried_out_says_so(self) -> None:
        waiting = {"ok": True, "status": "approval_required"}
        refused = change_summary([_step("delete_nutrition_entry", waiting)])
        beside_change = change_summary(
            [
                _step("update_profile", changes=[_PROFILE_CHANGE]),
                _step("delete_nutrition_entry", waiting),
            ]
        )
        # A delete that failed, or failed and then succeeded, was not waiting for a confirmation.
        failed = change_summary(
            [_step("delete_nutrition_entry", {"ok": False, "error": "x"})]
        )
        retried = change_summary(
            [
                {**_step("delete_nutrition_entry", waiting), "resolved": True},
                _step("update_profile", changes=[_PROFILE_CHANGE]),
            ]
        )
        self.assertIsNone(failed)
        assert retried is not None
        self.assertNotIn("not_done", retried)

        self.assertEqual(
            refused,
            {
                "labels": [],
                "undoable": False,
                "undone_at": None,
                "not_done": ["Löschen nicht ausgeführt – bitte bestätigen"],
            },
        )
        assert beside_change is not None
        self.assertTrue(beside_change["undoable"])
        self.assertEqual(len(beside_change["not_done"]), 1)

    def test_a_profile_line_is_titled_profile_not_with_the_athletes_name(self) -> None:
        detail = change_detail(
            "profile",
            {"name": "Ada", "weight_kg": 70},
            {"name": "Ada", "weight_kg": 72},
        )
        assert detail is not None
        self.assertEqual(detail["subject"], "")
        self.assertEqual(
            detail_line("profile", "update", detail), "Profil: Gewicht 70 → 72 kg"
        )

    def test_the_model_never_receives_undo_references(self) -> None:
        steps = [_step("update_profile", changes=[_PROFILE_CHANGE])]

        projected = model_confirmed_steps(steps)

        self.assertEqual(
            projected, [{"tool": "update_profile", "result": {"ok": True}}]
        )
        self.assertIn("changes", steps[0])


if __name__ == "__main__":
    unittest.main()

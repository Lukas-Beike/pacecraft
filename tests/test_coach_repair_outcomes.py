import unittest

from backend.coach.outcomes import (
    coach_effect_label,
    coach_failure_lines,
    unresolved_coach_steps,
)
from backend.coach.service import (
    coach_repair_key,
    dialogue_scope_repair_key,
    outcome_status,
)


def status_of(failures: list[dict], effects: list[dict]) -> str:
    """A finished turn's status with these failures and effects (#3651)."""
    return outcome_status(
        question="",
        incomplete_answer=False,
        failures=failures,
        missing_answer=False,
        effects=effects,
        cancelled=False,
    )


class UnresolvedCoachStepsTests(unittest.TestCase):
    def test_same_tool_success_only_repairs_same_target_scope_and_period(self):
        failed = {
            "tool": "apply_training_patch",
            "step_key": "first",
            "request": {
                "target": "local_plan",
                "scope": ["workout:one"],
                "period": {"start": "2026-09-20"},
            },
            "result": {"ok": False, "reason": "tool_arguments_invalid"},
        }
        for changed in (
            {
                "target": "other",
                "scope": ["workout:one"],
                "period": {"start": "2026-09-20"},
            },
            {
                "target": "local_plan",
                "scope": ["workout:two"],
                "period": {"start": "2026-09-20"},
            },
            {
                "target": "local_plan",
                "scope": ["workout:one"],
                "period": {"start": "2026-09-21"},
            },
        ):
            with self.subTest(changed=changed):
                success = {
                    "tool": failed["tool"],
                    "step_key": "second",
                    "request": changed,
                    "result": {"ok": True},
                }
                self.assertEqual(unresolved_coach_steps([failed, success]), [failed])

        equivalent = {
            "tool": failed["tool"],
            "step_key": "second",
            "request": {
                "target": "local_plan",
                "scope": ["workout:one"],
                "period": {"start": "2026-09-20"},
            },
            "result": {"ok": True},
        }
        self.assertEqual(unresolved_coach_steps([failed, equivalent]), [])

        legacy_json_scope = {
            "tool": failed["tool"],
            "step_key": "numeric-scope",
            "request": {"target": "local_plan", "scope": [7], "period": None},
            "result": {"ok": True},
        }
        same_legacy_scope = {
            "tool": failed["tool"],
            "step_key": "numeric-scope-retry",
            "request": {"target": "local_plan", "scope": [7], "period": None},
            "result": {"ok": True},
        }
        self.assertEqual(
            unresolved_coach_steps([legacy_json_scope, same_legacy_scope]), []
        )

    def test_missing_scope_object_is_repaired_only_by_the_same_effect(self):
        failed = {
            "tool": "save_competition",
            "step_key": "first",
            "scope_repair_key": "same-effect",
            "request": {"target": "local_plan", "scope": ["competition:missing"]},
            "result": {"ok": False, "reason": "request_object_missing"},
        }
        retry = {
            "tool": "save_competition",
            "step_key": "second",
            "scope_repair_key": "same-effect",
            "request": {"target": "local_plan", "scope": ["local_competitions"]},
            "result": {"ok": True},
        }
        self.assertEqual(unresolved_coach_steps([failed, retry]), [])
        other = {**retry, "scope_repair_key": "other-effect"}
        self.assertEqual(unresolved_coach_steps([failed, other]), [failed])

    def test_a_scope_failure_redone_on_the_same_template_completes_the_turn(self):
        # #3651: the first template edit named an unknown scope token and failed
        # with request_scope; the retry edits the same template under
        # library_workout:<id>, with its arguments refined.
        def step(scope: str, local_id: str, ok: bool, name: str = "Z2") -> dict:
            arguments = {
                "templates": [{"action": "update", "local_id": local_id, "name": name}],
                "_request": {"target": "local", "scope": [scope]},
            }
            return {
                "tool": "manage_training_templates",
                "step_key": f"{scope}-{name}",
                "repair_key": coach_repair_key("manage_training_templates", arguments),
                "scope_repair_key": dialogue_scope_repair_key(
                    "manage_training_templates", arguments
                ),
                "request": arguments["_request"],
                "result": {"ok": True, "templates": [{"local_id": local_id}]}
                if ok
                else {"ok": False, "reason": "request_scope"},
            }

        failed = step("training_template:t1", "t1", ok=False)
        redone = step("library_workout:t1", "t1", ok=True, name="Z2 lang")
        unresolved = unresolved_coach_steps([failed, redone])
        self.assertEqual([], unresolved)
        self.assertEqual(
            "", coach_failure_lines(unresolved, {"manage_training_templates"})
        )
        self.assertEqual("completed", status_of(unresolved, [redone]))
        self.assertEqual("Trainingsvorlagen bearbeitet (1)", coach_effect_label(redone))
        # #3960: a provider refresh names how it ended; a full resync that
        # already runs started nothing new.
        for result, label in (
            ({"ok": True, "status": "already_running"}, "Datenabruf läuft bereits"),
            ({"ok": True, "status": "started"}, "Datenabruf beauftragt"),
            ({"ok": True, "status": "completed"}, "Daten aktualisiert"),
            (
                {"ok": True, "status": "completed", "partial": True},
                "Daten teilweise aktualisiert",
            ),
        ):
            with self.subTest(result=result):
                self.assertEqual(
                    label,
                    coach_effect_label(
                        {"tool": "start_provider_refresh", "result": result}
                    ),
                )
        # A success on another template leaves the failed one named: partial.
        other = step("library_workout:t2", "t2", ok=True, name="Z2 lang")
        unresolved = unresolved_coach_steps([failed, other])
        self.assertEqual([failed], unresolved)
        self.assertEqual("partial", status_of(unresolved, [other]))
        self.assertIn(
            "Trainingsvorlagen bearbeiten:",
            coach_failure_lines(unresolved, {"manage_training_templates"}),
        )
        # Another operation on the same template repairs nothing: a failed
        # delete stays named after a successful update (review on #3651).
        delete = step("training_template:t1", "t1", ok=False)
        delete["repair_key"] = coach_repair_key(
            "manage_training_templates",
            {"templates": [{"action": "delete", "local_id": "t1"}]},
        )
        self.assertEqual([delete], unresolved_coach_steps([delete, redone]))

    def test_a_scope_failure_needs_the_same_record_of_its_object(self):
        # #3651 review: logging a saved meal at 19:00 is not repaired by
        # logging it at 08:00 (Gitar on #3656: meal_time tells meals apart), but a corrected portion count is.
        def log(meal: str, portions: float, ok: bool) -> dict:
            arguments = {"id": "n1", "portions": portions, "meal_date": "2026-11-19",
                         "meal_time": meal}  # fmt: skip
            return {
                "tool": "log_nutrition_template",
                "step_key": f"{meal}-{portions}",
                "repair_key": coach_repair_key("log_nutrition_template", arguments),
                "result": {"ok": ok}
                if ok
                else {"ok": False, "reason": "request_scope"},
            }

        failed = log("19:00", 1, ok=False)
        self.assertEqual([], unresolved_coach_steps([failed, log("19:00", 2, ok=True)]))
        breakfast = log("08:00", 1, ok=True)
        self.assertEqual([failed], unresolved_coach_steps([failed, breakfast]))

    def test_scope_denied_patch_retried_with_its_unit_tokens_completes_the_turn(self):
        # Step sequences of commands 7f90b1199805 and dc44424c7caf (#2617).
        def patch(scope: list[str], reason: str | None) -> dict:
            arguments = {
                "changes": [{"local_id": "u1", "duration_min": 50}],
                "expected_revision": 7,
                "_request": {"target": "local_plan", "scope": scope},
            }
            return {
                "tool": "apply_training_patch",
                "step_key": "+".join(scope),
                "scope_repair_key": dialogue_scope_repair_key(
                    "apply_training_patch", arguments
                ),
                "request": arguments["_request"],
                "result": {"ok": reason is None, "reason": reason},
            }

        reads = [
            {"tool": tool, "step_key": tool, "result": {"ok": True}}
            for tool in (
                "read_training_state",
                "list_planned_workouts",
                "manage_training_templates",
            )
        ]
        denied = patch(["local_plan"], "intent_scope_denied")
        retry = patch(["local_plan", "planned_unit:u1"], None)
        missing = patch(["planned_unit:gone"], "request_object_missing")
        for steps in ([*reads, denied, retry], [*reads, missing, denied, retry]):
            failures = unresolved_coach_steps(steps)
            self.assertEqual(failures, [])
            self.assertEqual(
                outcome_status(
                    question="",
                    incomplete_answer=False,
                    failures=failures,
                    missing_answer=False,
                    effects=[retry],
                    cancelled=False,
                ),
                "completed",
            )
        other = patch(["local_plan", "planned_unit:u2"], None)
        other["scope_repair_key"] = "another-change"
        self.assertEqual(unresolved_coach_steps([*reads, denied, other]), [denied])

    def test_a_create_refused_for_an_existing_record_completes_the_turn(self):
        # #3080: the Coach plans from the existing template instead of creating
        # it again; the refused create saved nothing and is no failure.
        refused = {
            "tool": "manage_training_templates",
            "step_key": "local_template",
            "request": {"target": "local_template", "scope": ["local_template"]},
            "result": {"ok": False, "reason": "template_exists"},
        }
        planned = {
            "tool": "apply_workout_library_plan",
            "step_key": "library_workout:t1",
            "request": {"target": "local_plan", "scope": ["library_workout:t1"]},
            "result": {"ok": True},
        }
        for steps in ([refused, planned], [refused]):
            failures = unresolved_coach_steps(steps)
            self.assertEqual(failures, [])
            self.assertEqual(
                outcome_status(
                    question="",
                    incomplete_answer=False,
                    failures=failures,
                    missing_answer=False,
                    effects=steps[1:],
                    cancelled=False,
                ),
                "completed",
            )
        # Any other refusal stays a failure: a refused batch also dropped its
        # other changes, until the call is retried without the duplicate.
        for reason in ("template_limit", "template_exists_in_call"):
            other = {**refused, "result": {"ok": False, "reason": reason}}
            with self.subTest(reason=reason):
                self.assertEqual(unresolved_coach_steps([other, planned]), [other])
        retried = {**refused, "result": {"ok": True}}
        self.assertEqual(unresolved_coach_steps([other, planned, retried]), [])
        # The marathon created again: the Coach updates it by its ID instead,
        # under another scope, and the turn still completes.
        race = {
            "tool": "save_competition",
            "step_key": "local_competitions",
            "request": {"target": "local", "scope": ["local_competitions"]},
            "result": {"ok": False, "reason": "competition_exists"},
        }
        update = {
            **race,
            "step_key": "competition:c1",
            "request": {"target": "local", "scope": ["competition:c1"]},
            "result": {"ok": True},
        }
        self.assertEqual(unresolved_coach_steps([race, update]), [])

    def test_profile_conflict_requires_exact_profile_fields(self):
        failed = {
            "tool": "update_profile",
            "step_key": "profile-a",
            "request": {"target": "profile", "scope": ["profile"], "period": None},
            "repair_key": {"profile_fields": ["ftp", "weight"]},
            "result": {"ok": False, "reason": "profile_conflict"},
        }
        different = {
            "tool": "update_profile",
            "step_key": "profile-b",
            "request": {"target": "profile", "scope": ["profile"], "period": None},
            "repair_key": {"profile_fields": ["ftp"]},
            "result": {"ok": True},
        }
        self.assertEqual(unresolved_coach_steps([failed, different]), [failed])
        same = {
            "tool": "update_profile",
            "step_key": "profile-b",
            "request": {"target": "profile", "scope": ["profile"], "period": None},
            "repair_key": {"profile_fields": ["ftp", "weight"]},
            "result": {"ok": True},
        }
        self.assertEqual(unresolved_coach_steps([failed, same]), [])

        legacy_fields = {**failed, "repair_key": {"profile_fields": "ftp,weight"}}
        same_legacy_fields = {
            **same,
            "repair_key": {"profile_fields": "ftp,weight"},
        }
        self.assertEqual(
            unresolved_coach_steps([legacy_fields, same_legacy_fields]), []
        )

    def test_all_pending_plan_sync_can_repair_selected_sync(self):
        failed = {
            "tool": "start_intervals_plan_sync",
            "step_key": "sync-selected",
            "request": {
                "target": "intervals",
                "scope": ["planned_unit:one"],
                "sync_scope": "selected",
            },
            "result": {"ok": False, "reason": "tool_arguments_invalid"},
        }
        success = {
            "tool": "start_intervals_plan_sync",
            "step_key": "sync-all",
            "request": {
                "target": "intervals",
                "scope": ["local_plan", "intervals_sync"],
                "sync_scope": "all_pending",
            },
            "result": {"ok": True},
        }
        self.assertEqual(unresolved_coach_steps([failed, success]), [])

    def test_alternative_plan_tools_require_same_binding_and_plan_effect(self):
        failed = {
            "tool": "apply_training_patch",
            "step_key": "patch",
            "request_binding_key": "binding-a",
            "plan_effect_key": "effect-a",
            "result": {"ok": False, "reason": "request_invalid"},
        }
        for change in (
            {"request_binding_key": "binding-b"},
            {"plan_effect_key": "effect-b"},
        ):
            success = {
                "tool": "replace_training_plan",
                "step_key": "replacement",
                "request_binding_key": "binding-a",
                "plan_effect_key": "effect-a",
                "result": {"ok": True},
                **change,
            }
            self.assertEqual(unresolved_coach_steps([failed, success]), [failed])
        success = {
            "tool": "replace_training_plan",
            "step_key": "replacement",
            "request_binding_key": "binding-a",
            "plan_effect_key": "effect-a",
            "result": {"ok": True},
        }
        self.assertEqual(unresolved_coach_steps([failed, success]), [])

    def test_step_key_tracking_and_result_order_are_preserved(self):
        first = {
            "tool": "apply_training_patch",
            "step_key": "first",
            "result": {"ok": False},
        }
        second = {
            "tool": "update_profile",
            "step_key": "second",
            "result": {"ok": False},
        }
        replacement = {
            "tool": "apply_training_patch",
            "step_key": "first",
            "result": {"ok": False, "reason": "new"},
        }
        self.assertEqual(
            unresolved_coach_steps([first, second, replacement]), [replacement, second]
        )

    def test_malformed_receipts_cannot_clear_failures_or_crash_projection(self):
        failed = {
            "tool": "apply_training_patch",
            "step_key": "first",
            "request": {
                "target": "local_plan",
                "scope": ["workout:one"],
                "period": None,
            },
            "result": {"ok": False, "reason": "request_invalid"},
        }
        malformed_success = {
            "tool": "apply_training_patch",
            "step_key": {"not": "hashable"},
            "request": {"target": "local_plan", "scope": [["invalid"]], "period": None},
            "result": {"ok": True},
        }
        self.assertEqual(
            unresolved_coach_steps([failed, None, {"tool": []}, malformed_success]),
            [failed],
        )

    def test_legacy_missing_request_exception_is_preserved(self):
        failed = {
            "tool": "apply_training_patch",
            "step_key": "first",
            "result": {"ok": False, "reason": "request_invalid"},
        }
        success = {
            "tool": "apply_training_patch",
            "step_key": "second",
            "result": {"ok": True},
        }
        self.assertEqual(unresolved_coach_steps([failed, success]), [])

    def test_json_truthy_success_value_one_is_preserved(self):
        failed = {
            "tool": "apply_training_patch",
            "step_key": "first",
            "request": {
                "target": "local_plan",
                "scope": ["workout:one"],
                "period": None,
            },
            "result": {"ok": False, "reason": "tool_arguments_invalid"},
        }
        success = {
            "tool": "apply_training_patch",
            "step_key": "second",
            "request": {
                "target": "local_plan",
                "scope": ["workout:one"],
                "period": None,
            },
            "result": {"ok": 1},
        }
        self.assertEqual(unresolved_coach_steps([failed, success]), [])


if __name__ == "__main__":
    unittest.main()

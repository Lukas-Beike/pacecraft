import io
import tokenize
import unittest
from pathlib import Path

from backend.athlete.profile import DEFAULT_PROFILE
from backend.coach.authorization import dialogue_tools
from backend.coach.dialogue import INSTRUCTIONS
from backend.coach.tool_registry import build_tool_contracts
from backend.coach.tool_schemas import build_structured_tool_schemas

ROOT = Path(__file__).resolve().parents[1]
EXECUTOR_CHANGE_FIELDS = {
    "local_id",
    "action",
    "date",
    "starts_at_local",
    "name",
    "description",
    "duration_minutes",
    "target",
    "sport",
    "expected_payload_hash",
    "plan_id",
    "rationale",
    "environment",
    "sport_detail",
    "session_focus",
    "strength_intensity",
}


def _tools():
    return {
        tool["name"]: tool
        for tool in build_structured_tool_schemas(
            default_profile={},
            checkin_text_limits={},
            checkin_score_fields=(),
            training_change_limit=366,
            library_bulk_max_entries=100,
            training_plan_statuses=(),
        )
    }


def _nested_objects(node, path=""):
    if isinstance(node, dict):
        if node.get("type") == "object" and "properties" in node and path:
            yield path, node
        for key, value in node.items():
            yield from _nested_objects(value, f"{path}.{key}" if path else key)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _nested_objects(value, f"{path}[{index}]")


class CoachToolSchemaTests(unittest.TestCase):
    def test_unit_names_carry_the_short_naming_rule(self):
        # #2258: every tool that writes a unit, template or library name says how to name it.
        for tool, path in (
            ("stage_training_plan", ("payload", "workouts")),
            ("replace_training_plan", ("payload", "workouts")),
            ("apply_training_changes", ("changes",)),
            ("manage_training_templates", ("templates",)),
        ):
            node = _tools()[tool]["parameters"]
            for key in path:
                node = node["properties"][key]
            description = node["items"]["properties"]["name"]["description"]
            with self.subTest(tool=tool):
                self.assertIn("Schwellenlauf 3 × 9 min", description)
                self.assertIn("only when the athlete asks", description)

    def test_plan_tools_require_the_session_focus_enum(self):
        for name in ("stage_training_plan", "replace_training_plan"):
            item = _tools()[name]["parameters"]["properties"]["payload"]["properties"][
                "workouts"
            ]["items"]
            self.assertIn("session_focus", item["required"])
            self.assertIn("strength_intensity", item["required"])
            self.assertEqual(set(item["required"]), set(item["properties"]))
            self.assertEqual(item["properties"]["duration_minutes"]["minimum"], 5)
            focus = item["properties"]["session_focus"]["enum"]
            self.assertIn("sweet_spot", focus)
            self.assertIn("legs", focus)
            self.assertIsNone(focus[-1])
        change = _tools()["apply_training_changes"]["parameters"]["properties"][
            "changes"
        ]["items"]["properties"]
        self.assertIn("session_focus", change)
        template = _tools()["manage_training_templates"]["parameters"]["properties"][
            "templates"
        ]["items"]["properties"]
        self.assertIn("strength_intensity", template)

    def test_checkin_and_activity_feedback_tools_are_exposed_and_staging_is_strict(
        self,
    ):
        tools = _tools()
        self.assertLessEqual({"save_checkin", "save_activity_feedback"}, set(tools))
        self.assertTrue(tools["stage_training_plan"]["strict"])

    def test_nutrition_tools_accept_the_session_slots_volume_and_session(self):
        slots = ["pre_session", "during_session"]
        save = _tools()["save_nutrition_entry"]["parameters"]["properties"]["payload"]
        update = _tools()["update_nutrition_entry"]["parameters"]["properties"][
            "changes"
        ]
        template = _tools()["log_nutrition_template"]["parameters"]["properties"]
        for properties in (save["properties"], update["properties"], template):
            with self.subTest(properties=sorted(properties)[:3]):
                self.assertLessEqual(set(slots), set(properties["meal_type"]["enum"]))
                self.assertIn("volume_ml", properties)
                self.assertIn("session_id", properties)
        self.assertEqual(slots, template["meal_type"]["enum"])
        self.assertEqual(10000, save["properties"]["volume_ml"]["maximum"])

    def test_strict_replace_plan_findings_use_supported_array_schema(self):
        tool = _tools()["replace_training_plan"]
        findings = tool["parameters"]["properties"]["accept_plan_findings"]
        self.assertTrue(tool["strict"])
        self.assertEqual(findings["maxItems"], 1)
        self.assertNotIn("uniqueItems", findings)

    def test_activity_detail_description_is_not_limited_to_one_provider(self):
        description = _tools()["get_activity_details"]["description"]
        self.assertNotIn("Intervals.icu", description)
        self.assertIn("any connected provider", description)
        self.assertIn("act_", description)

    def test_the_profile_tool_offers_no_weight_or_body_fat(self):
        # #3640: Körper owns them.
        tools = build_structured_tool_schemas(
            default_profile=DEFAULT_PROFILE,
            checkin_text_limits={},
            checkin_score_fields=(),
            training_change_limit=366,
            library_bulk_max_entries=100,
            training_plan_statuses=(),
        )
        update = next(tool for tool in tools if tool["name"] == "update_profile")
        change = update["parameters"]["properties"]["changes"]
        fields = change["items"]["properties"]["field"]["enum"]
        self.assertNotIn("weight_kg", fields)
        self.assertNotIn("body_fat_pct", fields)
        self.assertIn("height_cm", fields)
        self.assertEqual(len(fields), change["maxItems"])

    def test_schema_texts_take_an_apostrophe_without_escaping(self):
        # #3750: a single-quoted literal (the formatter keeps one only where
        # the text holds double quotes) broke the module on every apostrophe an
        # edit added; such texts are triple-quoted. f-strings count too.
        source = (ROOT / "backend" / "coach" / "tool_schemas.py").read_text(
            encoding="utf-8"
        )
        opening = {tokenize.STRING, getattr(tokenize, "FSTRING_START", tokenize.STRING)}
        single = [
            token.start[0]
            for token in tokenize.generate_tokens(io.StringIO(source).readline)
            if token.type in opening and token.string.lstrip("rRbBuUfF").startswith("'")
        ]
        self.assertEqual([], single)

    def test_energy_read_names_the_formula_body_fat_source(self):
        # #3419: the formula rows' body fat comes from skinfolds, an entered
        # value, a device or the profile (#3362, #3402), named in ``body``.
        tool = _tools()["read_nutrition"]
        energy = tool["parameters"]["properties"]["energy"]["description"]
        self.assertNotIn("from skinfold lean mass", energy)
        for source in ("skinfold", "entered", "device", "profile"):
            self.assertIn(source, energy)
        self.assertIn("body.label", energy)
        # #3618 item 6: the Verlauf parts of the read say what they mean.
        for field in ("deficit", "fit_scale", "goal", "wellness"):
            with self.subTest(field=field):
                self.assertRegex(energy, rf"\b{field} (is|holds)\b")
        self.assertIn("unflagged.end", energy)

    def test_energy_labels_use_the_diary_names_everywhere(self):
        # #3703: "Schritte & Treppen", "Übriger Alltag" (the learned term),
        # "übriger Alltag" (the own residual) and "Offene Kalorien" replace the
        # old names in the cards and in the Coach's description of the read.
        energy = _tools()["read_nutrition"]["parameters"]["properties"]["energy"][
            "description"
        ]
        for name in (
            "Schritte & Treppen",
            "Übriger Alltag",
            "übriger Alltag",
            "Offene Kalorien",
        ):
            with self.subTest(name=name):
                self.assertIn(name, energy)
        old = ("gelernter Alltag", "nicht erfasster Alltag", "Heute noch essen")
        sources = [
            ROOT / "backend/coach/tool_schemas.py",
            *(ROOT / "public").glob("*.js"),
            *(ROOT / "public").glob("*.css"),
            ROOT / "public/index.html",
        ]
        for path in sources:
            text = path.read_text(encoding="utf-8").lower()
            for phrase in old:
                with self.subTest(path=path.name, phrase=phrase):
                    self.assertNotIn(phrase.lower(), text)

    def test_plan_writes_name_the_revision_source_and_the_batch_path(self):
        # #4288 C9/C10: the first patch call carries the revision read in this
        # turn; unchanged templates go through the cheap batch tool.
        _names, structured, _read_only, dialogue, _caps = build_tool_contracts(
            default_profile={},
            checkin_text_limits={},
            checkin_score_fields=(),
            training_change_limit=366,
            library_bulk_max_entries=100,
            training_plan_statuses=(),
            dialogue_tools=dialogue_tools,
        )
        tools = {tool["name"]: tool for tool in structured + dialogue}
        revision = tools["apply_training_patch"]["parameters"]["properties"][
            "expected_revision"
        ]
        self.assertIn(
            "planning_revision from read_training_state", revision["description"]
        )
        self.assertIn(
            "cheap batch path", tools["apply_workout_library_plan"]["description"]
        )
        self.assertIn("apply_workout_library_plan", INSTRUCTIONS)

    def test_session_tools_name_the_total_time_as_a_strength_duration(self):
        # #2317: rest between sets is part of a strength session's work.
        for name in ("list_recent_activities", "get_activity_details"):
            with self.subTest(tool=name):
                description = _tools()[name]["description"]
                self.assertIn(
                    "A strength session's duration is its total time "
                    "(elapsed_duration_s)",
                    description,
                )

    def test_nested_object_schemas_reject_unknown_keys(self):
        open_objects = [
            f"{name}.{path}"
            for name, tool in _tools().items()
            for path, node in _nested_objects(tool["parameters"])
            if node.get("additionalProperties") is not False
        ]
        self.assertEqual(open_objects, [])

    def test_training_change_items_are_closed_over_executor_fields(self):
        items = _tools()["apply_training_changes"]["parameters"]["properties"][
            "changes"
        ]["items"]
        self.assertIs(items["additionalProperties"], False)
        self.assertLessEqual(EXECUTOR_CHANGE_FIELDS, set(items["properties"]))

    def test_read_limits_declare_server_clamp_bounds(self):
        tools = _tools()
        expected = {
            ("list_recent_activities", "days"): {"minimum": 1, "maximum": 3660},
            ("list_recent_activities", "limit"): {"minimum": 1, "maximum": 500},
            ("list_planned_workouts", "limit"): {"minimum": 1, "maximum": 366},
            ("list_change_history", "limit"): {"minimum": 1, "maximum": 500},
        }
        for (name, key), bounds in expected.items():
            prop = tools[name]["parameters"]["properties"][key]
            for bound, value in bounds.items():
                with self.subTest(tool=name, key=key, bound=bound):
                    self.assertEqual(prop.get(bound), value)

    def test_save_checkin_clear_fields_enumerates_coach_preserved_fields(self):
        from backend.athlete.checkins import (
            CHECKIN_SCORE_FIELDS,
            CHECKIN_TEXT_LIMITS,
            COACH_PRESERVED_FIELDS,
        )

        tool = {
            tool["name"]: tool
            for tool in build_structured_tool_schemas(
                default_profile={},
                checkin_text_limits=CHECKIN_TEXT_LIMITS,
                checkin_score_fields=CHECKIN_SCORE_FIELDS,
                training_change_limit=366,
                library_bulk_max_entries=100,
                training_plan_statuses=(),
            )
        }["save_checkin"]
        payload = tool["parameters"]["properties"]["payload"]
        clear_fields = payload["properties"]["clear_fields"]
        self.assertEqual(clear_fields["type"], "array")
        self.assertIs(clear_fields["uniqueItems"], True)
        self.assertEqual(clear_fields["items"]["enum"], list(COACH_PRESERVED_FIELDS))
        self.assertNotIn("clear_fields", payload.get("required", []))
        self.assertIn("clear_fields", tool["description"])
        self.assertIn("Omitted and empty fields preserve", tool["description"])

    def test_refresh_days_allow_all_days_or_bounded_window(self):
        days = _tools()["start_provider_refresh"]["parameters"]["properties"]["days"]
        self.assertEqual(
            days["anyOf"],
            [
                {"type": "integer", "enum": [-1]},
                {"type": "integer", "minimum": 1, "maximum": 365},
            ],
        )
        self.assertIn("Set to 30", days["description"])


if __name__ == "__main__":
    unittest.main()

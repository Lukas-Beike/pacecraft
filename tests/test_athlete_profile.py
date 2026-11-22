import os
import unittest

from backend.athlete.profile import (
    COACH_READ_ONLY_FIELDS,
    DEFAULT_PROFILE,
    DEFAULT_TIMEZONE,
    normalize_profile,
    timezone_name,
)
from backend.errors import AppError


class AthleteProfileTests(unittest.TestCase):
    def test_preferred_sources_accept_known_providers_only(self):
        result = normalize_profile(
            {"preferred_source_body": "garmin", "preferred_source_hrv": "wahoo"}
        )
        self.assertEqual(result["preferred_source_body"], "garmin")
        self.assertEqual(result["preferred_source_hrv"], "")
        with self.assertRaises(AppError):
            normalize_profile(
                {"preferred_source_sleep": "oura"}, validate_timezone=True
            )

    def test_a_stored_intervals_preference_reads_as_the_default(self):
        profile = {"preferred_source_body": "intervals"}
        self.assertEqual(normalize_profile(profile)["preferred_source_body"], "")
        self.assertEqual(profile, {"preferred_source_body": "intervals"})
        with self.assertRaises(AppError):
            normalize_profile(profile, validate_timezone=True)

    def test_defaults_preserve_profile_contract_and_import_time_timezone_fallback(self):
        self.assertEqual(
            DEFAULT_PROFILE,
            {
                "name": "",
                "goals": "",
                "sports": "Cycling",
                "training_background": "",
                "typical_weekly_volume": "",
                "availability": "",
                "constraints": "",
                "equipment": "",
                "training_preferences": "",
                **{
                    f"training_preferences_{sport}": ""
                    for sport in (
                        "running",
                        "cycling",
                        "swimming",
                        "strength",
                        "mobility",
                    )
                },
                "training_model": "pyramidal",
                "performance_notes": "",
                "weight_kg": "",
                "body_fat_pct": "",
                "height_cm": "",
                "sex": "",
                "birth_year": "",
                "coaching_style": "Supportive, direct, and evidence-aware",
                "timezone": os.environ.get("TZ", DEFAULT_TIMEZONE),
                "weather_location": "",
                "sleep_target_hours": "",
                "fueling_tolerance": "",
                "energy_goal": "",
                "energy_goal_kcal": "",
                "learned_everyday_checked": "",
                "first_threshold_hr": "",
                "first_threshold_origin": "",
                "preferred_source_thresholds": "",
                "preferred_source_body": "",
                "preferred_source_hrv": "",
                "preferred_source_resting_hr": "",
                "preferred_source_sleep": "",
            },
        )

    def test_unknown_fields_are_ignored(self):
        result = normalize_profile({"name": "Ada", "admin": True})
        self.assertEqual(result["name"], "Ada")
        self.assertNotIn("admin", result)

    def test_known_values_are_stringified_trimmed_and_limited(self):
        result = normalize_profile(
            {"name": "  Ada  ", "height_cm": 180, "goals": " x " * 5000}
        )
        self.assertEqual(result["name"], "Ada")
        self.assertEqual(result["height_cm"], "180")
        self.assertEqual(len(result["goals"]), 4000)
        self.assertEqual(result["goals"], (" x " * 5000).strip()[:4000])

    def test_null_becomes_an_empty_field_not_the_text_none(self):
        for strict in (False, True):
            with self.subTest(strict=strict):
                result = normalize_profile(
                    {"height_cm": None, "sex": None, "name": None},
                    validate_timezone=strict,
                )
                self.assertEqual(
                    (result["height_cm"], result["sex"], result["name"]), ("", "", "")
                )

    def test_strict_number_fields_read_comma_decimals_and_store_normalized_text(self):
        result = normalize_profile(
            {"height_cm": " 180,5 ", "sleep_target_hours": "7,5"},
            validate_timezone=True,
        )
        self.assertEqual(
            (result["height_cm"], result["sleep_target_hours"]), ("180.5", "7.5")
        )

    def test_a_save_keeps_the_stored_weight_and_body_fat_and_takes_no_new_ones(self):
        # #3640: Körper owns weight and body fat. The stored profile values stay
        # as data (#3557 reads their history), and no save changes them.
        stored = normalize_profile({"weight_kg": "80", "body_fat_pct": "15"})
        for incoming in (
            {"weight_kg": "82", "body_fat_pct": "14"},
            {"weight_kg": "", "body_fat_pct": None},
            {"weight_kg": "viel", "body_fat_pct": "0"},
            {},
        ):
            with self.subTest(incoming=incoming):
                saved = normalize_profile(
                    {**incoming, "name": "Ada"}, validate_timezone=True, previous=stored
                )
                self.assertEqual(
                    ("Ada", "80", "15"),
                    (saved["name"], saved["weight_kg"], saved["body_fat_pct"]),
                )
        # Without a stored value a save leaves them empty.
        fresh = normalize_profile(
            {"weight_kg": "82", "body_fat_pct": "14"},
            validate_timezone=True,
            previous=normalize_profile({}),
        )
        self.assertEqual(("", ""), (fresh["weight_kg"], fresh["body_fat_pct"]))

    def test_strict_number_fields_reject_text_and_out_of_range_values(self):
        cases = {
            "height_cm": ("1,80 m", "49", "251", "nan", "abc"),
            "sleep_target_hours": ("3,9", "12.1", "inf", "acht"),
        }
        for field, values in cases.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    with self.assertRaises(AppError) as raised:
                        normalize_profile({field: value}, validate_timezone=True)
                    self.assertEqual(raised.exception.status, 400)
        for field, edge in (
            ("height_cm", "50"),
            ("height_cm", "250"),
            ("sleep_target_hours", "4"),
            ("sleep_target_hours", "12"),
        ):
            with self.subTest(field=field, edge=edge):
                self.assertEqual(
                    normalize_profile({field: edge}, validate_timezone=True)[field],
                    edge,
                )

    def test_the_energy_goal_is_lose_hold_or_build_with_whole_kcal(self):
        # #3547: lose and build take kcal per day, 0 to 1500; hold keeps none.
        saved = normalize_profile(
            {"energy_goal": "lose", "energy_goal_kcal": "500,4"},
            validate_timezone=True,
        )
        self.assertEqual(
            ("lose", "500"), (saved["energy_goal"], saved["energy_goal_kcal"])
        )
        held = normalize_profile(
            {"energy_goal": "hold", "energy_goal_kcal": "300"}, validate_timezone=True
        )
        self.assertEqual(("hold", ""), (held["energy_goal"], held["energy_goal_kcal"]))
        for bad in (
            {"energy_goal": "cut"},
            {"energy_goal": "build", "energy_goal_kcal": "1501"},
            {"energy_goal": "lose", "energy_goal_kcal": "viel"},
            {"energy_goal": "lose"},
        ):
            with self.subTest(bad=bad), self.assertRaises(AppError) as raised:
                normalize_profile(bad, validate_timezone=True)
            self.assertEqual(raised.exception.status, 400)
        # A stored unknown goal reads as none.
        self.assertEqual("", normalize_profile({"energy_goal": "cut"})["energy_goal"])

    def test_the_learned_everyday_check_is_a_date_only_the_athlete_sets(self):
        # #3577: the date the athlete took the learned everyday term over.
        field = "learned_everyday_checked"
        saved = normalize_profile({field: "2026-11-19"}, validate_timezone=True)
        self.assertEqual("2026-11-19", saved[field])
        self.assertEqual(
            "", normalize_profile({field: ""}, validate_timezone=True)[field]
        )
        with self.assertRaises(AppError) as raised:
            normalize_profile({field: "gestern"}, validate_timezone=True)
        self.assertEqual(400, raised.exception.status)
        self.assertEqual("", normalize_profile({field: "gestern"})[field])
        self.assertIn(field, COACH_READ_ONLY_FIELDS)

    def test_reads_keep_legacy_number_text_and_strict_keeps_unchanged_values(self):
        legacy = {"weight_kg": "72 kg", "height_cm": "tall"}
        self.assertEqual(normalize_profile(legacy)["weight_kg"], "72 kg")
        self.assertEqual(normalize_profile(legacy)["height_cm"], "tall")
        stored = normalize_profile(legacy)
        saved = normalize_profile(
            {**stored, "name": "Ada"}, validate_timezone=True, previous=stored
        )
        self.assertEqual((saved["weight_kg"], saved["height_cm"]), ("72 kg", "tall"))
        with self.assertRaises(AppError):
            normalize_profile(
                {**stored, "height_cm": "taller"},
                validate_timezone=True,
                previous=stored,
            )

    def test_timezone_name_accepts_valid_iana_timezone(self):
        self.assertEqual(timezone_name("  UTC  "), "UTC")

    def test_timezone_name_falls_back_for_invalid_non_strict_value(self):
        self.assertEqual(timezone_name("Mars/NotAZone"), DEFAULT_TIMEZONE)

    def test_timezone_name_raises_exact_strict_error(self):
        with self.assertRaises(AppError) as raised:
            timezone_name("Mars/NotAZone", strict=True)
        self.assertEqual(raised.exception.status, 400)
        self.assertEqual(
            raised.exception.message,
            "Die Zeitzone muss eine gültige IANA-Zeitzone sein.",
        )

    def test_normalize_profile_validates_timezone_only_when_requested(self):
        invalid = {"timezone": "Mars/NotAZone"}
        self.assertEqual(normalize_profile(invalid)["timezone"], DEFAULT_TIMEZONE)
        with self.assertRaises(AppError):
            normalize_profile(invalid, validate_timezone=True)

    def test_training_model_defaults_and_rejects_unknown_only_when_strict(self):
        self.assertEqual(normalize_profile({})["training_model"], "pyramidal")
        self.assertEqual(
            normalize_profile({"training_model": " polarized "})["training_model"],
            "polarized",
        )
        self.assertEqual(
            normalize_profile({"training_model": "hiit"})["training_model"],
            "pyramidal",
        )
        self.assertEqual(
            normalize_profile({"training_model": ""}, validate_timezone=True)[
                "training_model"
            ],
            "pyramidal",
        )
        with self.assertRaises(AppError):
            normalize_profile({"training_model": "hiit"}, validate_timezone=True)

    def test_sex_is_male_female_or_unset_and_rejects_unknown_only_when_strict(self):
        self.assertEqual(normalize_profile({})["sex"], "")
        for value in ("male", "female", ""):
            self.assertEqual(
                normalize_profile({"sex": value}, validate_timezone=True)["sex"], value
            )
        self.assertEqual(normalize_profile({"sex": "other"})["sex"], "")
        with self.assertRaises(AppError):
            normalize_profile({"sex": "other"}, validate_timezone=True)

    def test_birth_year_is_a_whole_year_in_range(self):
        # #3099: Jackson-Pollock needs the age at each skinfold measurement.
        self.assertEqual(normalize_profile({})["birth_year"], "")
        self.assertEqual(
            normalize_profile({"birth_year": 1986}, validate_timezone=True)[
                "birth_year"
            ],
            "1986",
        )
        for value in ("1919", "2011", "1986.5", "neunzehn"):
            with self.subTest(value=value):
                self.assertEqual(
                    normalize_profile({"birth_year": value})["birth_year"], ""
                )
                with self.assertRaises(AppError):
                    normalize_profile({"birth_year": value}, validate_timezone=True)

    def test_normalize_profile_does_not_mutate_input(self):
        value = {"name": "  Ada  ", "timezone": "UTC", "unknown": ["keep"]}
        original = {
            key: item.copy() if isinstance(item, list) else item
            for key, item in value.items()
        }
        normalize_profile(value, validate_timezone=True)
        self.assertEqual(value, original)


if __name__ == "__main__":
    unittest.main()

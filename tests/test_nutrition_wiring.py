"""The nutrition builder reads the live configuration and database (#2099)."""

from __future__ import annotations

import unittest
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, Mock, patch

from backend.nutrition import wiring as nutrition_wiring
from backend.providers.model_seam import ModelRequest


class NutritionBuilderTests(unittest.TestCase):
    def make_services(self) -> nutrition_wiring.NutritionServices:
        self.ctx = Mock()
        self.config = SimpleNamespace(intervals_api_key="synthetic-key")
        self.ctx.config.return_value = self.config
        self.request = self.ctx.provider_transport.json_http_client.return_value
        return nutrition_wiring.build_nutrition(self.ctx)

    def test_building_resolves_no_database_or_configuration(self) -> None:
        self.make_services()

        self.ctx.database.assert_not_called()
        self.ctx.config.assert_not_called()
        self.ctx.model_transport.complete.assert_not_called()

    def test_local_services_use_the_active_manager_and_lock(self) -> None:
        services = self.make_services()

        library = services.meal_library_service()
        diary = services.diary_service()

        self.assertIsNotNone(library)
        self.assertIsNotNone(diary)
        self.ctx.database.assert_called_with()
        self.assertIs(services.food_mirror(), services.food_mirror())

    def test_the_period_balance_reads_yesterdays_history_once_per_change(self) -> None:
        # #3630: the week summary's own day totals read the 12-week history; it
        # is kept until a commit changes rows, as the session summaries are, so
        # a second Verlauf read does not price every session again.
        services = self.make_services()
        manager = MagicMock()
        manager.read_revision.return_value = 7
        manager.holds_lease.return_value = False
        self.ctx.database.return_value = manager
        self.ctx.clock.now.return_value = datetime(2026, 11, 19, 12, tzinfo=UTC)
        end = date(2026, 11, 18)
        days = [end - timedelta(days=n) for n in range(14)]
        history: tuple[Mock, Mock, dict[str, Any]] = (Mock(), Mock(), {})
        with (
            patch.object(nutrition_wiring, "own_history", return_value=history) as read,
            patch.object(
                nutrition_wiring,
                "own_totals",
                side_effect=lambda *args: dict.fromkeys(args[-1], 1.0),
            ) as totals,
        ):
            own = services.diary_service().sync_status._own_day_totals
            assert own is not None
            self.assertEqual(dict.fromkeys(days, 1.0), own(days))
            own([*days, *(end - timedelta(days=n) for n in range(14, 84))])
            self.assertEqual((1, end), (read.call_count, read.call_args.args[2]))
            self.assertIs(history[0], totals.call_args.args[0])
            # A commit reads again.
            manager.read_revision.return_value = 8
            own(days)
            self.assertEqual(2, read.call_count)
            # A new day reads the history ending on the new yesterday.
            self.ctx.clock.now.return_value = datetime(2026, 11, 20, 1, tzinfo=UTC)
            own(days)
            self.assertEqual(
                (3, date(2026, 11, 19)), (read.call_count, read.call_args.args[2])
            )
            # A day before the 12 windows has no own total (#3641): 6 months are
            # served from the history, with no 6-month read of their own (#3708);
            # after the day change the kept value is the old day's until a commit,
            # so this call reads the new day's 12 weeks once more, as any call.
            self.assertIsNone(own([date(2026, 6, 1)])[date(2026, 6, 1)])
            self.assertEqual(4, read.call_count)

    def test_the_diary_days_energy_read_is_never_kept(self) -> None:
        # #3852: "gegessen" in Offene Kalorien is the day's intake at the time of
        # the read; a second read after a meal write reads the day again, even
        # within the same database revision, so it never shows the old intake.
        services = self.make_services()
        manager = MagicMock()
        manager.read_revision.return_value = 7
        manager.holds_lease.return_value = False
        self.ctx.database.return_value = manager
        self.ctx.clock.now.return_value = datetime(2026, 11, 19, 12, tzinfo=UTC)
        reads = iter(({"goal": {"eaten_kcal": 640}}, {"goal": {"eaten_kcal": 0}}))
        with patch.object(
            nutrition_wiring,
            "read_energy_view",
            side_effect=lambda *args, **kwargs: next(reads),
        ) as read:
            self.assertEqual(
                640, services.energy_view("2026-11-19")["goal"]["eaten_kcal"]
            )
            self.assertEqual(
                0, services.energy_view("2026-11-19")["goal"]["eaten_kcal"]
            )
        self.assertEqual(2, read.call_count)

    def test_intervals_sync_uses_the_current_config_and_a_fresh_diary(self) -> None:
        services = self.make_services()
        with (
            patch.object(nutrition_wiring, "IntervalsApiClient") as client,
            patch.object(
                nutrition_wiring, "IntervalsNutritionSyncService", return_value="sync"
            ) as sync_service,
            patch.object(
                nutrition_wiring, "NutritionDiaryService", return_value="diary"
            ),
        ):
            self.assertEqual("sync", services.intervals_sync_service())

        client.assert_called_once_with(
            api_key="synthetic-key", request=self.request.request
        )
        sync_service.assert_called_once_with(
            config=self.config, api_client=client.return_value, diary_service="diary"
        )

    def test_label_photo_uses_the_nutrition_model_and_thinking(self) -> None:
        self.make_services()
        settings = self.ctx.settings
        settings.selected_model.side_effect = lambda task="conversation": f"m-{task}"
        settings.selected_thinking_level.side_effect = lambda task="conversation": (
            f"t-{task}"
        )
        with patch.object(nutrition_wiring, "NutritionPhotoExtractionService") as photo:
            nutrition_wiring.build_nutrition(self.ctx).meal_library_service()
        options = photo.call_args.kwargs
        self.assertEqual(options["selected_model"](), "m-nutrition")
        complete = self.ctx.model_transport.complete
        options["complete"](ModelRequest(model="m"))
        self.assertEqual(complete.call_args.args[0].reasoning_effort, "t-nutrition")
        # A fixed effort, such as the web search's, is kept.
        options["complete"](ModelRequest(model="m", reasoning_effort="low"))
        self.assertEqual(complete.call_args.args[0].reasoning_effort, "low")

    def test_each_owner_reaches_its_parameter(self) -> None:
        ctx = Mock()
        ctx.athlete_profile.get.return_value = {"sex": "female"}
        with (
            patch.object(nutrition_wiring, "OpenFoodFactsClient") as off,
            patch.object(nutrition_wiring, "YazioClient") as yazio,
            patch.object(nutrition_wiring, "NutritionMealLibraryService") as library,
            patch.object(nutrition_wiring, "NutritionDiaryService") as diary,
            patch.object(nutrition_wiring, "NutritionWebSearchService") as search,
        ):
            nutrition_wiring.build_nutrition(ctx).diary_service()

        off.assert_called_once_with(opener=ctx.observed_opener.return_value)
        self.assertEqual(
            ["open_food_facts", "yazio"],
            [call.args[0] for call in ctx.observed_opener.call_args_list],
        )
        self.assertIs(ctx.config, yazio.call_args.args[0])
        self.assertEqual("female", yazio.call_args.kwargs["athlete_sex"]())
        self.assertIs(library.return_value, diary.call_args.kwargs["meal_library"])
        # The product web search reads the Nutrition model choice.
        ctx.settings.selected_model.return_value = "nutrition-model"
        self.assertEqual("nutrition-model", search.call_args.kwargs["selected_model"]())
        ctx.settings.selected_model.assert_called_with("nutrition")
        reader = diary.call_args.kwargs["observation_reader"]
        ctx.canonical_read = "live reader"
        self.assertEqual("live reader", reader())


if __name__ == "__main__":
    unittest.main()

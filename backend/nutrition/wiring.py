"""Build the local nutrition services and the explicit Intervals.icu nutrition sync."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, timedelta
from functools import partial
from typing import Any, Protocol

from backend.activities.activity_reference import ActivityReferences
from backend.app.context import AppContext
from backend.athlete.body_records import (
    RESTING_LOOKBACK_DAYS,
    current_weight,
    device_body_fats,
    latest_value,
    skinfold_body_fats,
    weighin_counts,
    weighin_sources,
    weighins,
)
from backend.athlete.profile import profile_body_fat_history, profile_weight_history
from backend.canonical.vocabulary import MetricKind
from backend.db.manager import kept_until_change
from backend.db.repositories import (
    NutritionProductRepository,
    NutritionRepository,
    NutritionTemplateRepository,
)
from backend.nutrition.catalog import ProductCatalogService
from backend.nutrition.diary import NutritionDiaryService
from backend.nutrition.energy_balance import EnergyInputs, history_start
from backend.nutrition.energy_expected import planned_inputs
from backend.nutrition.energy_inputs import (
    EnergySources,
    energy_basis,
    own_resting,
    recent_fat_pct,
    session_streams,
)
from backend.nutrition.energy_view import (
    FIT_PERIODS,
    LONG_PERIODS,
    OwnHistory,
    own_history,
    read_energy_view,
    read_fit_inputs,
    selected_day,
)
from backend.nutrition.energy_view import own_day_totals as own_totals
from backend.nutrition.food_database import FoodDatabaseService
from backend.nutrition.food_mirror import FoodDatabaseMirror
from backend.nutrition.meal_library import NutritionMealLibraryService
from backend.nutrition.photo import NutritionPhotoExtractionService
from backend.nutrition.product_cleanup import ProductCleanupService
from backend.nutrition.product_refresh import ProductRefreshRunner
from backend.nutrition.sync import IntervalsNutritionSyncService
from backend.nutrition.web_search import NutritionWebSearchService
from backend.performance.daily_health import read_daily_health_by_date
from backend.providers.intervals import IntervalsApiClient
from backend.providers.model_seam import ModelRequest, ModelTurn
from backend.providers.open_food_facts import OpenFoodFactsClient
from backend.providers.yazio import YazioClient
from backend.runtime import clock as runtime_clock


class EnergyView(Protocol):
    """The diary day's expenditure read: a day (`None`: yesterday) and the long period."""

    def __call__(
        self, day_text: str | None = None, period: str | None = None
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class NutritionServices:
    secret_values: Callable[[], tuple[str, ...]]
    food_mirror: Callable[[], FoodDatabaseMirror]
    meal_library_service: Callable[[], NutritionMealLibraryService]
    product_catalog_service: Callable[[], ProductCatalogService]
    # Removes products not used for a chosen number of days, as one run (#3444).
    product_cleanup: Callable[[], ProductCleanupService]
    diary_service: Callable[[], NutritionDiaryService]
    intervals_sync_service: Callable[[], IntervalsNutritionSyncService]
    # Counts of the adaptive expenditure basis for the diagnostics (#3100).
    energy_basis: Callable[[Any], dict[str, Any]]
    # Resting and daily expenditure per source and their fit (#3099).
    energy_view: EnergyView
    # One run re-checking every product (#3299); stopped at shutdown.
    product_refresh: ProductRefreshRunner


def build_nutrition(ctx: AppContext) -> NutritionServices:
    """One food database and mirror for the process; the rest fresh per call."""
    utc_now = runtime_clock.utc_now
    mirror = FoodDatabaseMirror(ctx.database, utc_now)
    off = OpenFoodFactsClient(opener=ctx.observed_opener("open_food_facts"))
    food_database = FoodDatabaseService(
        off=off,
        mirror=mirror,
        yazio=YazioClient(
            ctx.config,
            # The profile sex Yazio searches send; empty sends none (#1774).
            athlete_sex=lambda: ctx.athlete_profile.get().get("sex", ""),
            opener=ctx.observed_opener("yazio"),
        ),
    )
    products = NutritionProductRepository()
    templates = NutritionTemplateRepository()

    def complete(request: ModelRequest) -> ModelTurn:
        # A fixed effort (the web search) stays; otherwise the Nutrition choice.
        effort = request.reasoning_effort or ctx.settings.selected_thinking_level(
            "nutrition"
        )
        return ctx.model_transport.complete(replace(request, reasoning_effort=effort))

    def nutrition_model() -> str:
        return ctx.settings.selected_model("nutrition")

    def meal_library_service() -> NutritionMealLibraryService:
        return NutritionMealLibraryService(
            database_manager=ctx.database(),
            utc_now=utc_now,
            local_now=ctx.clock.now,
            food_database=food_database,
            photo_extractor=NutritionPhotoExtractionService(
                selected_model=nutrition_model, complete=complete
            ),
            product_repository=products,
            template_repository=templates,
            web_search=NutritionWebSearchService(
                selected_model=nutrition_model, complete=complete
            ),
        )

    def diary_service() -> NutritionDiaryService:
        return NutritionDiaryService(
            database_manager=ctx.database(),
            nutrition_repository=NutritionRepository(utc_now),
            utc_now=utc_now,
            local_now=ctx.clock.now,
            meal_library=meal_library_service(),
            observation_reader=lambda: ctx.canonical_read,
            activity_summaries=lambda db, start, end: (
                ctx.canonical_read.activity_summaries(db, start=start, end=end)
            ),
            references=ActivityReferences(ctx.database()),
            own_day_totals=own_day_totals,
        )

    def yesterdays_history() -> tuple[date, OwnHistory]:
        end = ctx.clock.now().date() - timedelta(days=1)
        with ctx.database().reader() as db:
            return end, own_history(db, energy_sources(), end)

    # The 12-week history prices every session from its streams: kept until a
    # commit changes rows, so Verlauf's next read costs nothing (#3630).
    kept_history = kept_until_change(ctx.database, yesterdays_history)

    def own_day_totals(days: list[date]) -> dict[date, float | None]:
        # The period balance's expenditure: each day's own day total, from the
        # windows ending yesterday as the energy view's (#3600).
        # A day before the 12 windows has no own total (#3641), so 6 months
        # are served from the kept history too (Gitar, #3708).
        end = ctx.clock.now().date() - timedelta(days=1)
        kept_end, history = kept_history()
        if kept_end != end:  # a new day before the next commit
            kept_end, history = yesterdays_history()
        inputs, windows, health = history
        first = history_start(end)
        totals = own_totals(inputs, windows, health, [d for d in days if d >= first])
        return {day: totals.get(day) for day in days}

    def energy_sources() -> EnergySources:
        """The stores the adaptive expenditure reads (#3100).

        The resting reference is Katch-McArdle from the skinfold lean mass
        (#3099), else from the latest measured body fat; only without either
        does the reader fall back to Garmin's resting value, labelled.
        """
        read = ctx.canonical_read
        diary = NutritionRepository(utc_now)
        skinfolds = ctx.athlete.skinfolds()

        def sessions(db: Any, start: date, end: date) -> list[Any]:
            # Bounded by the athlete-local start date in the reader itself.
            return list(read.activity_summaries(db, start=start, end=end))

        def saved(
            history: Callable[[Any, date], dict[date, float]],
            db: Any,
            start: date,
            end: date,
        ) -> dict[date, float]:
            # The profile's dated values of the days start to end (#3557).
            return {d: v for d, v in history(db, start).items() if d <= end}

        def resting(db: Any, day: date) -> tuple[float, str] | None:
            since = day - timedelta(days=RESTING_LOOKBACK_DAYS)
            weights = read.observations(db, MetricKind.WEIGHT, start=since, end=day)
            measured = read.observations(db, MetricKind.BODY_FAT, start=since, end=day)
            # The profile's body fat values are skinfolds too (#3557).
            return own_resting(
                current_weight(
                    weights, saved(profile_weight_history, db, since, day), day
                ),
                recent_fat_pct(
                    skinfolds.skinfold_on(db, day),
                    day,
                    saved(profile_body_fat_history, db, since, day),
                ),
                latest_value(measured),
            )

        def body_fat(db: Any, start: date, end: date) -> dict[date, float]:
            """Every skinfold body fat of the lookback (#3617 item 2), over the
            profile's dated skinfold values of the same days (#3557)."""
            since = start - timedelta(days=RESTING_LOOKBACK_DAYS)
            found = saved(profile_body_fat_history, db, since, end)
            found.update(skinfold_body_fats(skinfolds.measurements_on(db, end), since))
            return found

        return EnergySources(
            diary=lambda db, s, e: diary.list_by_range(
                db, s.isoformat(), e.isoformat()
            ),
            daily_health=lambda db, s, e: read_daily_health_by_date(
                read, db, start=s, end=e
            ),
            # App weigh-ins first, Garmin's copies only for other days (#3557).
            weights=lambda db, s, e: weighins(
                read.observations(db, MetricKind.WEIGHT, start=s, end=e),
                saved(profile_weight_history, db, s, e),
            ),
            sessions=sessions,
            streams=session_streams,
            resting=resting,
            body_fat=body_fat,
            skinfolds=lambda db, day: skinfolds.measurements_on(db, day),
            # The profile's values are skinfolds (``body_fat``); Garmin's copies
            # fill only the other days (athlete decision, #3557).
            device_body_fat=lambda db, s, e: device_body_fats(
                read.observations(db, MetricKind.BODY_FAT, start=s, end=e),
                saved(profile_body_fat_history, db, s, e),
            ),
            profile=ctx.athlete_profile.get,
            feedback=partial(
                ctx.athlete.activity_feedback().for_activity_ids,
                sessions_only=True,
            ),
            weighin_counts=lambda db, s, e: weighin_counts(
                read.observations(db, MetricKind.WEIGHT, start=s, end=e),
                saved(profile_weight_history, db, s, e),
            ),
            weighin_sources=lambda db, s, e: weighin_sources(
                read.observations(db, MetricKind.WEIGHT, start=s, end=e),
                saved(profile_weight_history, db, s, e),
            ),
            planned=lambda _db, day: planned_inputs(
                ctx.planning.planned_unit().list_between(day, day)
            ),
        )

    def yesterdays_periods(days: int) -> tuple[date, EnergyInputs | None]:
        end = ctx.clock.now().date() - timedelta(days=1)
        with ctx.database().reader() as db:
            return end, read_fit_inputs(db, energy_sources(), end, days)

    # Verlauf's 6 and 12 months cost as much as the rest of the read: each is
    # kept until a commit changes rows, as the 12-week history is (#3630,
    # #3704), and 6 Monate never pays for 12 (#3839).
    kept_periods = {
        key: kept_until_change(
            ctx.database, partial(yesterdays_periods, FIT_PERIODS[key])
        )
        for key in LONG_PERIODS
    }

    def energy_view(
        day_text: str | None = None, period: str | None = None
    ) -> dict[str, Any]:
        # The windows end yesterday: today's intake and Garmin totals are still
        # partial. The source rows show the chosen diary day (#3395), else yesterday.
        today = ctx.clock.now().date()
        day = selected_day(day_text, today)
        end = today - timedelta(days=1)

        # Only Verlauf's 6 and 12 Monate need the longer read, each its own (#3711).
        if period not in LONG_PERIODS:
            with ctx.database().reader() as db:
                return read_energy_view(db, energy_sources(), end, day, today=today)
        days = FIT_PERIODS[period]

        def periods() -> EnergyInputs | None:
            kept_end, inputs = kept_periods[period]()
            return inputs if kept_end == end else yesterdays_periods(days)[1]

        with ctx.database().reader() as db:
            return read_energy_view(
                db, energy_sources(), end, day, periods, days, today
            )

    def intervals_sync_service() -> IntervalsNutritionSyncService:
        config = ctx.config()
        return IntervalsNutritionSyncService(
            config=config,
            api_client=IntervalsApiClient(
                api_key=config.intervals_api_key,
                request=ctx.provider_transport.json_http_client().request,
            ),
            diary_service=diary_service(),
        )

    return NutritionServices(
        # Runtime-only provider tokens for the log redactor.
        secret_values=food_database.yazio_secret_values,
        food_mirror=lambda: mirror,
        meal_library_service=meal_library_service,
        product_catalog_service=lambda: meal_library_service().product_catalog,
        product_cleanup=lambda: ProductCleanupService(
            ctx.database(), lambda: ctx.clock.now().date(), utc_now
        ),
        diary_service=diary_service,
        intervals_sync_service=intervals_sync_service,
        energy_basis=lambda db: energy_basis(
            db, energy_sources(), ctx.clock.now().date()
        ),
        energy_view=energy_view,
        product_refresh=ProductRefreshRunner(
            # Every product, not the first 1000 the list shows.
            lambda: meal_library_service().product_catalog.list_all_products(
                limit=None, counted=False
            ),
            lambda product_id: meal_library_service().refresh_database_food(product_id),
            lambda: ctx.maintenance,
            utc_now,
            source_timeouts=lambda: off.consecutive_timeouts,
        ),
    )

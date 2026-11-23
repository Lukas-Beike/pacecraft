"""Read and update diary synchronization state."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta
from typing import Any

from backend.db import garmin_store
from backend.db.manager import DatabaseManager
from backend.db.repositories import NutritionRepository
from backend.errors import AppError
from backend.nutrition.intake_totals import INTAKE_FIELDS, add_intake_totals
from backend.nutrition.models import validate_iso_date
from backend.performance.canonical_inputs import resolve_reader
from backend.performance.daily_health import (
    daily_energy_expenditure,
    read_daily_health_with_retrievals,
)
from backend.runtime.ports import ObservationReader

# Closed days the expenditure gap counts of the diagnostics cover (#3157).
EXPENDITURE_GAP_DAYS = 90
# The week summary's lengths: a week, and Verlauf's 4 weeks, 3 and 6 months (#3704).
WEEK_PERIODS = (7, 28, 91, 182, 365)

# The "Aus deinen Daten" day total per day, None where it has no value (#3600).
type OwnDayTotals = Callable[[list[date]], Mapping[date, float | None]]


class NutritionSyncStatusService:
    def __init__(
        self,
        database_manager: DatabaseManager,
        nutrition_repository: NutritionRepository,
        utc_now: Callable[[], str],
        local_now: Callable[[], datetime],
        observation_reader: Callable[[], ObservationReader],
        own_day_totals: OwnDayTotals | None = None,
    ) -> None:
        # The period balance's expenditure: the own day totals (#3600), else
        # the wearable's daily total where they are not wired.
        self._own_day_totals = own_day_totals
        self._observation_reader = observation_reader
        self._database_manager = database_manager
        self._nutrition_repository = nutrition_repository
        self._utc_now = utc_now
        self._local_now = local_now

    def get_day_summary(self, meal_date: str) -> dict[str, Any]:
        validated_date = validate_iso_date(meal_date)
        today = self._local_now().date()
        # Today's projection needs the latest complete resting-energy reference.
        snapshot_start = (
            (today - timedelta(days=7)).isoformat()
            if validated_date == today.isoformat()
            else validated_date
        )
        with self._database_manager.read_snapshot() as db:
            summary = self._nutrition_repository.day_summary(db, validated_date)
            # The Garmin state carries only the daily-stats sync status.
            garmin_snapshot = garmin_store.read_state(db)
            measured = self._filtered_energy(
                db, snapshot_start, validated_date, garmin_snapshot, [validated_date]
            )
        add_intake_totals(summary)
        summary["energy_expenditure"] = daily_energy_expenditure(
            garmin_snapshot,
            validated_date,
            today,
            measured=measured.get(validated_date, {}),
            measured_history=measured,
        )
        return summary

    def _filtered_energy(
        self,
        db: Any,
        start: str,
        end: str,
        state: dict[str, Any],
        dates: list[str],
    ) -> dict[str, dict[str, Any]]:
        """Read the canonical daily energy of the inclusive ISO date range.

        A day a current targeted refresh read without a Garmin daily total is
        emptied (``_filter_targeted_missing_energy``).
        """
        measured, retrievals = read_daily_health_with_retrievals(
            resolve_reader(self._observation_reader(), db),
            db,
            start=date.fromisoformat(start),
            end=date.fromisoformat(end),
        )
        return _filter_targeted_missing_energy(measured, state, dates, retrievals)

    def expenditure_gaps(self, db: Any) -> dict[str, int]:
        """Counts only, no dates or values: why closed days lack a Verlauf total.

        Of the last ``EXPENDITURE_GAP_DAYS`` closed days, ``with_total`` show a
        Garmin daily total; ``emptied_by_refresh_mark`` have a non-Garmin total
        stored that a refresh mark hides because no Garmin total exists
        (``_filter_targeted_missing_energy``); ``without_total`` have no
        canonical total, of which ``with_active_only`` still have active kcal
        (#3157).
        """
        today = self._local_now().date()
        dates = [
            (today - timedelta(days=offset)).isoformat()
            for offset in range(EXPENDITURE_GAP_DAYS, 0, -1)
        ]
        measured, retrievals = read_daily_health_with_retrievals(
            resolve_reader(self._observation_reader(), db),
            db,
            start=date.fromisoformat(dates[0]),
            end=date.fromisoformat(dates[-1]),
        )
        shown = _filter_targeted_missing_energy(
            measured, garmin_store.read_state(db), dates, retrievals
        )
        counts = dict.fromkeys(
            ("with_total", "emptied_by_refresh_mark", "without_total"), 0
        )
        active_only = 0
        for day in dates:
            stored = measured.get(day, {})
            if shown.get(day, {}).get("calories") is not None:
                counts["with_total"] += 1
            elif stored.get("calories") is not None:
                counts["emptied_by_refresh_mark"] += 1
            else:
                counts["without_total"] += 1
                active_only += stored.get("active_kcal") is not None
        return {"days": EXPENDITURE_GAP_DAYS, **counts, "with_active_only": active_only}

    def get_week_summary(self, day_count: int = 7) -> dict[str, Any]:
        """Compare logged intake with each closed day's expenditure: its own
        "Aus deinen Daten" day total (athlete decision, #3600), named by
        ``expenditure_source``; a day without one stays open."""
        if type(day_count) is not int or day_count not in WEEK_PERIODS:
            raise AppError(400, "Zeitraum muss 7, 28, 91, 182 oder 365 Tage umfassen.")
        today = self._local_now().date()
        dates = [
            (today - timedelta(days=offset)).isoformat()
            for offset in range(day_count, 0, -1)
        ]
        with self._database_manager.read_snapshot() as db:
            summaries = [
                self._nutrition_repository.day_summary(db, day) for day in dates
            ]
            snapshot = garmin_store.read_state(db)
            measured = self._filtered_energy(db, dates[0], dates[-1], snapshot, dates)
        own = (
            self._own_day_totals([date.fromisoformat(day) for day in dates])
            if self._own_day_totals
            else None
        )
        days = []
        for summary in summaries:
            add_intake_totals(summary)
            expenditure = daily_energy_expenditure(
                snapshot,
                summary["date"],
                today,
                measured=measured.get(summary["date"], {}),
                measured_history=measured,
            )
            total = (
                expenditure["total_kcal"]
                if own is None
                else own.get(date.fromisoformat(summary["date"]))
            )
            days.append(_week_day(summary, expenditure, total))
        averages = {}
        for key in (
            "intake_kcal",
            "expenditure_kcal",
            "carbs_g",
            "protein_g",
            "fat_g",
            *INTAKE_FIELDS,
        ):
            values = [day[key] for day in days if day[key] is not None]
            averages[key] = {
                "value": round(sum(values) / len(values), 1) if values else None,
                "days": len(values),
            }
        balances = [
            day["balance_kcal"] for day in days if day["balance_kcal"] is not None
        ]
        return {
            "start_date": dates[0],
            "end_date": dates[-1],
            "days": days,
            "averages": averages,
            "balance_kcal": sum(balances) if balances else None,
            "balance_days": len(balances),
            # Where expenditure_kcal comes from: "own" (#3600) or "device".
            "expenditure_source": "device" if own is None else "own",
        }


def _week_day(
    summary: dict[str, Any], expenditure: dict[str, Any], consumed: float | None
) -> dict[str, Any]:
    """One closed day of the week summary: logged intake against ``consumed``,
    the day's expenditure; ``energy_expenditure`` keeps the wearable's day."""
    logged = bool(summary["entry_count"])
    intake = summary["total_kcal"] if logged else None
    return {
        "date": summary["date"],
        "intake_kcal": intake,
        "expenditure_kcal": consumed,
        "balance_kcal": intake - consumed
        if intake is not None and consumed is not None
        else None,
        "energy_expenditure": expenditure,
        **{
            key: summary[f"total_{key}"] if logged else None
            for key in ("carbs_g", "protein_g", "fat_g", *INTAKE_FIELDS)
        },
    }


def _filter_targeted_missing_energy(
    measured: dict[str, dict[str, Any]],
    snapshot: dict[str, Any],
    dates: list[str],
    retrievals: dict[str, datetime | None],
) -> dict[str, dict[str, Any]]:
    """Do not reuse other sources' values for a day read without a Garmin total.

    ``snapshot`` is the Garmin sync state. ``retrievals`` holds, per day with a
    stored Garmin daily total, its latest fetch time
    (``read_daily_health_with_retrievals``). A day a complete targeted refresh
    read is emptied only when no Garmin total is stored for it at all; a stored
    total counts whenever it was fetched, so a refresh mark never hides it
    (#3157).
    """
    nutrition_refresh = snapshot.get("nutrition_refresh")
    targeted = (
        nutrition_refresh.get("daily_stats", {})
        if isinstance(nutrition_refresh, dict)
        else {}
    )
    if not isinstance(targeted, dict):
        return measured
    result = dict(measured)
    for day in dates:
        refresh = targeted.get(day)
        if (
            isinstance(refresh, dict)
            and refresh.get("freshness") == "current"
            and day not in retrievals
        ):
            result[day] = {}
    return result

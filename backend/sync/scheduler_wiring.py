"""Build the daily and startup synchronization schedulers (#2101).

The schedule policies are built fresh for each scheduler, so a scheduler sees the
configuration and the provider owners of the moment it was built.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from backend.app.context import AppContext
from backend.athlete.profile import ProfileService
from backend.db.manager import background_writer
from backend.sync import scheduler
from backend.sync.backfill_resume import BackfillChainResumer
from backend.sync.garmin_service import GARMIN_AUTOMATIC_SYNC_DAYS, GarminSyncService
from backend.sync.gates import ProviderResyncGate
from backend.sync.schedule_policies import (
    ProviderSchedulePolicy,
    WahooSchedulePolicy,
    build_schedule_policies,
)
from backend.sync.wiring import SyncWindow


@dataclass(frozen=True)
class SyncSchedulerServices:
    daily_scheduler: Callable[[], scheduler.DailySyncScheduler]
    backfill_resumer: Callable[[], BackfillChainResumer]
    startup_scheduler: Callable[[], scheduler.StartupSyncScheduler]
    daily_loop: Callable[[], scheduler.DailySyncLoop]


def build_sync_schedulers(
    ctx: AppContext,
    *,
    window: SyncWindow,
    weather_sync: Callable[[], Any],
    garmin_resync_gate: ProviderResyncGate,
    wahoo_resync_gate: ProviderResyncGate,
    retry_held: Callable[[str], bool],
    sleep: Callable[[float], None] = time.sleep,
) -> SyncSchedulerServices:
    """Schedulers over the live provider owners.

    The composition root passes the shared history window, the weather sync, the
    Garmin and Wahoo resync gates, the check that holds a provider's next
    automatic attempt after a failed refresh, and the loop's sleep.
    """

    def schedule_policies(
        profile: ProfileService, garmin: GarminSyncService, calendar_enabled: bool
    ) -> Mapping[str, ProviderSchedulePolicy]:
        """Build the provider policies from the snapshot taken by the caller."""
        return build_schedule_policies(
            profile=profile,
            garmin=garmin,
            calendar_enabled=calendar_enabled,
            garmin_automatic_sync_days=GARMIN_AUTOMATIC_SYNC_DAYS,
            garmin_resync_gate=garmin_resync_gate,
            garmin_change_check=ctx.garmin.change_check(),
            weather_refresh_due=lambda: weather_sync().refresh_due(),
            weather_competitions_configured=lambda: (
                weather_sync().competitions_configured()
            ),
            wahoo=WahooSchedulePolicy(
                configured=lambda: ctx.wahoo.sync_service().configured(),
                sync_state=ctx.sync_persistence.state_repository(),
                sync_period_defaults=window.sync_period_defaults,
                all_sync_days=window.all_sync_days,
                resync_gate=wahoo_resync_gate,
            ),
        )

    def backfill_resumer() -> BackfillChainResumer:
        """The resumer of stopped backfill chains for the current providers."""
        config = ctx.config()
        profile = ctx.athlete.profile()
        garmin = ctx.garmin.sync_service()
        return BackfillChainResumer(
            ctx.sync_job_queue.service(),
            ctx.sync_persistence.state_repository(),
            schedule_policies(profile, garmin, bool(config.calendar_ical_url)),
            chunk_days=window.sync_chunk_days,
            earliest_date=window.sync_earliest_date,
        )

    def daily_scheduler() -> scheduler.DailySyncScheduler:
        """A daily schedule using the current provider configuration."""
        config = ctx.config()
        calendar_enabled = bool(config.calendar_ical_url)
        profile = ctx.athlete.profile()
        queue = ctx.sync_job_queue.service()
        markers = ctx.sync_persistence.daily_markers()
        garmin = ctx.garmin.sync_service()
        return scheduler.DailySyncScheduler(
            queue,
            markers,
            ctx.maintenance,
            config=scheduler.DailySyncSchedulerConfig(
                auto_update_label=scheduler.AUTO_UPDATE_LABEL,
            ),
            schedule_policies=schedule_policies(profile, garmin, calendar_enabled),
            retry_held=retry_held,
            backfill_resumer=backfill_resumer().resume,
        )

    def startup_scheduler() -> scheduler.StartupSyncScheduler:
        """A startup schedule using the current provider configuration."""
        config = ctx.config()
        calendar_enabled = bool(config.calendar_ical_url)
        profile = ctx.athlete.profile()
        queue = ctx.sync_job_queue.service()
        garmin = ctx.garmin.sync_service()
        sync_state = ctx.sync_persistence.state_repository()
        return scheduler.StartupSyncScheduler(
            queue,
            sync_state,
            config=scheduler.StartupSyncSchedulerConfig(
                sync_chunk_days=window.sync_chunk_days,
                sync_earliest_date=window.sync_earliest_date,
            ),
            schedule_policies=schedule_policies(profile, garmin, calendar_enabled),
        )

    def checkpoint_wal() -> None:
        """Truncate the WAL as deferrable work behind foreground writers."""
        with ctx.maintenance.operation(), background_writer():
            ctx.database().checkpoint_wal()

    def housekeeping() -> None:
        """Write the nightly backup when it is due (#3760), first so a failing
        later step never skips it; then prune finished jobs and purge archived
        library templates (#3141); then send the new push notifications (#3914);
        last, queue a due automatic Coach turn (#4274)."""
        if ctx.backup is not None:
            with ctx.maintenance.operation():
                ctx.backup.run_nightly()
        ctx.sync_job_queue.service().prune_finished()
        with ctx.maintenance.operation(), background_writer():
            ctx.planning.workout_library().purge_archived()
        if ctx.push_checks is not None:
            # The checks take the maintenance gate themselves, around their
            # database steps only, never around a push service call.
            with background_writer():
                ctx.push_checks.check()
        if ctx.coach_background_jobs is not None:
            # Only queues the job; the Coach worker runs it.
            with ctx.maintenance.operation(), background_writer():
                ctx.coach_background_jobs.automatic_turns().check()

    def daily_loop() -> scheduler.DailySyncLoop:
        """The unstarted periodic loop and its stop event."""
        return scheduler.DailySyncLoop(
            daily_scheduler(),
            ctx.morning_body_battery_service(),
            sleep=sleep,
            logger=ctx.logger,
            stop_event=threading.Event(),
            housekeeping=housekeeping,
            wal_checkpoint=checkpoint_wal,
            morning_due=lambda: ctx.garmin.change_check().morning_due(),
        )

    return SyncSchedulerServices(
        daily_scheduler, backfill_resumer, startup_scheduler, daily_loop
    )

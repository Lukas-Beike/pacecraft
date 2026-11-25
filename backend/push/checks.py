"""Periodic push checks over durable local state (#3914).

The daily loop calls ``check`` every few minutes. Each check derives its events
from state that already exists (provider refresh history, competitions, the
infection hint) and claims them through ``PushService``, which sends each event
key once. Reads and claims run inside the maintenance operation; the network
calls to the push services run outside it, so they never hold up a restore.
Nothing is sent at night: an event found then waits for the morning, because the
state it comes from is still there.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager, nullcontext
from datetime import date, datetime, timedelta
from typing import Any, Final

from backend.canonical.vocabulary import ProviderId
from backend.performance.threshold_staleness import REASON_STALE, STALE_AFTER_DAYS
from backend.push.events import (
    MISSING_FEEDBACK_HOUR,
    checkin_event,
    maintenance_event,
    missing_feedback_event,
    morning_data_event,
    morning_plan_event,
    new_activity_event,
    new_sessions,
    personal_best_event,
    planned_units,
    server_event,
    threshold_offer_events,
    weather_event,
)
from backend.push.service import Notification, PushService
from backend.sync.retry_hold import ATHLETE_ACTION_ERROR_CODES

_LOGGER = logging.getLogger("intervals_coach")

# Local hours in which notifications are sent.
DAY_START_HOUR: Final = 7
DAY_END_HOUR: Final = 22
# Consecutive failed refreshes that count as a repeated sync failure.
REPEATED_FAILURES: Final = 3
UPCOMING_DAYS: Final = 3
_PROVIDERS: Final = tuple(provider.value for provider in ProviderId)
_HISTORY_ROWS: Final = 20
# The seeding marker of the threshold offers (#4135).
OFFERS_SEEDED: Final = "threshold_offers"


class PushChecks:
    """Derive the state-based notifications and send the new ones."""

    def __init__(
        self,
        push: PushService,
        database: Callable[[], Any],
        local_now: Callable[[], datetime],
        *,
        infection_hint: Callable[[], Any] = lambda: None,
        threshold_warnings: Callable[[], Any] = lambda: None,
        equipment: Callable[[], Any] = lambda: None,
        records: Callable[[], Any] = lambda: None,
        threshold_offers: Callable[[], Any] = lambda: None,
        weather_days: Callable[[], Any] = lambda: None,
        server_state: Callable[[], Any] = lambda: None,
        unrated_sessions: Callable[[date], Any] = lambda day: (),
        operation: Callable[[], AbstractContextManager[Any]] = nullcontext,
    ) -> None:
        self._push = push
        self._database = database
        self._local_now = local_now
        self._infection_hint = infection_hint
        self._threshold_warnings = threshold_warnings
        self._equipment = equipment
        self._records = records
        self._threshold_offers = threshold_offers
        self._weather_days = weather_days
        self._server_state = server_state
        self._unrated_sessions = unrated_sessions
        self._operation = operation

    def check(self) -> int:
        """Send the new events; the number delivered."""
        local = self._local_now()
        if not DAY_START_HOUR <= local.hour < DAY_END_HOUR:
            return 0
        with self._operation():
            events = self._events(local)
        return sum(self.notify(event) for event in events)

    def notify(self, event: Notification) -> bool:
        """Claim, deliver and settle one event; True when a device received it.

        Each event is claimed, delivered and settled on its own, so a failure
        never leaves an earlier claim recorded as sent without its settle, and
        no push service call runs inside the maintenance operation.
        """
        with self._operation():
            claim = self._push.claim(event)
        if claim is None:
            return False
        try:
            statuses = self._push.deliver(claim)
        except Exception:  # noqa: BLE001 - an unsettled claim must be retried
            statuses = dict.fromkeys(
                (item["endpoint"] for item in claim.subscriptions), 0
            )
        with self._operation():
            return self._push.settle(claim, statuses)

    def _events(self, local: datetime) -> list[Notification]:
        since = self._push.subscribed_since()
        if since is None:
            return []
        today = local.date()
        with self._database().reader() as db:
            recent = new_sessions(db, since, today)
            units = planned_units(db, today, today + timedelta(days=1))
            found = [
                *provider_problems(db, since),
                *upcoming_competitions(db, today),
                morning_data_event(db, today),
                checkin_event(db, today),
                new_activity_event(recent),
            ]
        if self._source(self._infection_hint):
            found.append(infection_hint_event(today))
        # The subscription day as the athlete counts days, like the staleness hint.
        subscribed_on = (
            since.astimezone(local.tzinfo).date() if local.tzinfo else since.date()
        )
        found.append(
            step_test_event(self._source(self._threshold_warnings), subscribed_on)
        )
        found += self._derived_events(local, subscribed_on, units, bool(recent))
        return [event for event in found if event is not None]

    def _derived_events(
        self,
        local: datetime,
        subscribed_on: date,
        units: list[tuple[str, dict[str, Any]]],
        news: bool,
    ) -> list[Notification | None]:
        """The events of the other owners; one failing source skips only itself."""
        today = local.date()
        found = [
            morning_plan_event(units, today),
            weather_event(units, self._source(self._weather_days), local),
            maintenance_event(self._source(self._equipment)),
            server_event(self._source(self._server_state), today),
        ]
        # A record needs a session; the records are read only while one is recent.
        if news:
            found.append(
                personal_best_event(self._source(self._records), subscribed_on, today)
            )
        found += self._threshold_events(subscribed_on)
        if local.hour >= MISSING_FEEDBACK_HOUR:
            found.append(
                missing_feedback_event(
                    self._source(self._unrated_sessions, today), today
                )
            )
        return found

    def _threshold_events(self, subscribed_on: date) -> list[Notification]:
        """The open offers are announced from the second read on; the first only seeds."""
        offers = self._source(self._threshold_offers)
        if not isinstance(offers, list):
            return []
        keys = [
            f"threshold_offer:{offer['key']}"
            for offer in offers
            if isinstance(offer, dict) and isinstance(offer.get("key"), str)
        ]
        if self._push.seed(OFFERS_SEEDED, keys):
            return []
        return threshold_offer_events(offers, subscribed_on)

    @staticmethod
    def _source(read: Callable[..., Any], *args: Any) -> Any:
        try:
            return read(*args)
        except Exception:  # noqa: BLE001 - the other checks still run
            _LOGGER.warning(
                "Push check source failed", extra={"event": "push_check_source_failed"}
            )
            return None


def _parse(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def provider_problems(db: Any, since: datetime) -> list[Notification]:
    """One notification per provider for an expired sign-in or a failure streak.

    A streak is the run of failed refreshes since the last success of an area;
    skipped runs neither end nor extend it. Its key is that last success, which
    stays put however long the streak lasts, so a streak is announced once. Only a streak whose latest
    failure came after the subscription is announced.
    """
    events: dict[str, Notification] = {}
    for provider in _PROVIDERS:
        areas = db.execute(
            "SELECT DISTINCT area FROM provider_refresh_history WHERE provider=?",
            (provider,),
        ).fetchall()
        problems = [_area_problem(db, provider, row["area"], since) for row in areas]
        found = sorted(problem for problem in problems if problem is not None)
        if not found:
            continue
        # An expired sign-in ("auth") wins over a failure streak ("failing").
        kind, began = found[0]
        events[provider] = Notification(
            "provider_problem",
            f"provider:{provider}:{kind}:{began}",
            "Anbindung braucht Aufmerksamkeit",
            "Eine Anmeldung ist abgelaufen."
            if kind == "auth"
            else "Eine Synchronisierung schlägt wiederholt fehl.",
            "#more/connections",
        )
    return list(events.values())


def _area_problem(
    db: Any, provider: str, area: str, since: datetime
) -> tuple[str, str] | None:
    """The area's problem kind and its last success before the streak; None without one."""
    rows = db.execute(
        "SELECT status, error_code, started_at, finished_at "
        "FROM provider_refresh_history WHERE provider=? AND area=? "
        "AND status != 'skipped' ORDER BY started_at DESC LIMIT ?",
        (provider, area, _HISTORY_ROWS),
    ).fetchall()
    streak = _streak(rows)
    if not streak:
        return None
    latest = _parse(streak[0]["finished_at"] or streak[0]["started_at"])
    if latest is None or latest <= since:
        return None
    expired = any(row["error_code"] in ATHLETE_ACTION_ERROR_CODES for row in streak)
    if not expired and len(streak) < REPEATED_FAILURES:
        return None
    last_success = db.execute(
        "SELECT MAX(started_at) AS at FROM provider_refresh_history "
        "WHERE provider=? AND area=? AND status IN ('success', 'partial')",
        (provider, area),
    ).fetchone()["at"]
    return ("auth" if expired else "failing"), f"{area}:{last_success or 'never'}"


def _streak(rows: Iterable[Any]) -> list[Any]:
    streak = []
    for row in rows:
        if row["status"] != "error":
            break
        streak.append(row)
    return streak


def upcoming_competitions(db: Any, today: date) -> list[Notification]:
    """A competition within the next three days, once per competition and date."""
    rows = db.execute(
        "SELECT id, event_date FROM competitions WHERE event_date BETWEEN ? AND ?",
        (today.isoformat(), (today + timedelta(days=UPCOMING_DAYS)).isoformat()),
    ).fetchall()
    events = []
    for row in rows:
        try:
            days = (date.fromisoformat(str(row["event_date"])[:10]) - today).days
        except ValueError:
            continue
        events.append(
            Notification(
                "upcoming",
                f"competition:{row['id']}:{row['event_date']}",
                "Wettkampf steht bevor",
                _countdown(days),
                "#plan/season",
            )
        )
    return events


def _countdown(days: int) -> str:
    if days == 0:
        return "Heute ist Wettkampftag."
    if days == 1:
        return "Morgen ist ein Wettkampf."
    return f"In {days} Tagen ist ein Wettkampf."


def step_test_event(warnings: Any, since: date) -> Notification | None:
    """A step test is due: a threshold turned stale (over 8 weeks) since the subscription.

    Athlete decision 2026-11-22: once when the existing staleness hint newly
    appears. A value already stale when the device subscribed is not announced.
    Each stale value is its own key: the notification goes out while any is new,
    and a newer value that turns stale later is new again.
    """
    items = warnings.get("items") if isinstance(warnings, dict) else None
    keys = []
    for item in items if isinstance(items, list) else []:
        try:
            dated_on = date.fromisoformat(str(item.get("dated_on")))
        except ValueError:
            continue
        stale_from = dated_on + timedelta(days=STALE_AFTER_DAYS + 1)
        if item.get("reason") == REASON_STALE and stale_from > since:
            keys.append(f"{item.get('metric')}:{item.get('sport')}:{dated_on}")
    if not keys:
        return None
    first, *rest = sorted(f"step_test:{key}" for key in keys)
    return Notification(
        "upcoming",
        first,
        "Stufentest fällig",
        "Ein Schwellenwert ist älter als acht Wochen.",
        "#more/profile",
        covers=tuple(rest),
    )


def infection_hint_event(today: date) -> Notification:
    return Notification(
        "upcoming",
        f"infection:{today.isoformat()}",
        "Hinweis zu deiner Nacht",
        "Bitte bestätige kurz, wie es dir geht.",
        "#plan/overview",
    )

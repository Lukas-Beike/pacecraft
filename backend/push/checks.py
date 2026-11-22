"""Periodic push checks over durable local state (#3914).

The daily loop calls ``check`` every few minutes. Each check derives its events
from state that already exists (provider refresh history, competitions, the
infection hint) and hands them to ``PushService.notify``, which sends each event
key once. Nothing is sent at night: an event found then waits for the morning,
because the state it comes from is still there.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta
from typing import Any, Final

from backend.canonical.vocabulary import ProviderId
from backend.performance.threshold_staleness import REASON_STALE, STALE_AFTER_DAYS
from backend.push.service import Notification, PushService
from backend.sync.retry_hold import ATHLETE_ACTION_ERROR_CODES

# Local hours in which notifications are sent.
DAY_START_HOUR: Final = 7
DAY_END_HOUR: Final = 22
# Consecutive failed refreshes that count as a repeated sync failure.
REPEATED_FAILURES: Final = 3
UPCOMING_DAYS: Final = 3
_PROVIDERS: Final = tuple(provider.value for provider in ProviderId)
_HISTORY_ROWS: Final = 20


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
    ) -> None:
        self._push = push
        self._database = database
        self._local_now = local_now
        self._infection_hint = infection_hint
        self._threshold_warnings = threshold_warnings

    def check(self) -> int:
        """Send the new events; the number delivered."""
        since = self._push.subscribed_since()
        local = self._local_now()
        if since is None or not DAY_START_HOUR <= local.hour < DAY_END_HOUR:
            return 0
        today = local.date()
        with self._database().reader() as db:
            events = [
                *provider_problems(db, since),
                *upcoming_competitions(db, today),
            ]
        if self._infection_hint():
            events.append(infection_hint_event(today))
        if step_test := step_test_event(self._threshold_warnings(), since.date()):
            events.append(step_test)
        return sum(1 for event in events if self._push.notify(event))


def _parse(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def provider_problems(db: Any, since: datetime) -> list[Notification]:
    """One notification per provider for an expired sign-in or a failure streak.

    A streak is the run of failed refreshes since the last success of an area;
    skipped runs neither end nor extend it. Its key is the day it began, so a
    streak is announced once however long it lasts. Only a streak whose latest
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
    """The area's problem kind and the day its streak began; None without one."""
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
    return ("auth" if expired else "failing"), str(streak[-1]["started_at"])[:10]


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
    appears. A value already stale when the device subscribed is not announced;
    its key is the stale value, so a newer value that turns stale later is new.
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
    return Notification(
        "upcoming",
        "step_test:" + ",".join(sorted(keys)),
        "Stufentest fällig",
        "Ein Schwellenwert ist älter als acht Wochen.",
        "#more/profile",
    )


def infection_hint_event(today: date) -> Notification:
    return Notification(
        "upcoming",
        f"infection:{today.isoformat()}",
        "Hinweis zu deiner Nacht",
        "Bitte bestätige kurz, wie es dir geht.",
        "#plan/overview",
    )

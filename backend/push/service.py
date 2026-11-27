"""Web Push subscriptions, per-type switches and once-only delivery (#3914).

Everything lives in the encrypted ``kv`` table, so no schema change is needed:
the VAPID private key and the browser subscriptions are secrets (excluded from
export and diagnostics like the provider credentials), the switches and the
sent-event record are plain settings. Each event key is sent once; a type that
is switched off is recorded as handled so turning it on later does not replay
old events. Delivery is three steps, so the network calls hold no gate: claim
(database), deliver (push services only), settle (database). Notification bodies are lock-screen safe: no names, values, places,
provider error text or Coach content, except the units of "Heute" (#4273)
and the first sentence of an automatic Coach answer (#4274), athlete decisions.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from backend.errors import AppError
from backend.provider_credentials import (
    PUSH_PRIVATE_KEY,
    PUSH_SUBSCRIPTIONS_KEY,
)
from backend.push import web_push

_LOGGER = logging.getLogger("intervals_coach")

PUSH_PREFERENCES_KEY = "push_preferences"
PUSH_SENT_KEY = "push_sent"
PUSH_COUNTS_KEY = "push_counts"
PUSH_SEEDED_KEY = "push_seeded"

# The athlete's notification types, each with its own switch, all on by default.
TYPES: Final = (
    "coach_answer",
    "morning_plan",
    "provider_problem",
    "upcoming",
    "new_activity",
    "morning_data",
    "checkin",
    "maintenance",
    "personal_best",
    "new_threshold",
    "missing_feedback",
    "server_backup",
    "weather",
)
MAX_SUBSCRIPTIONS: Final = 10
# Sent-event keys are kept this long, longer than any event stays current.
_SENT_RETENTION_DAYS: Final = 60
_TTL_SECONDS: Final = 12 * 3600
# The push service no longer knows the subscription: forget it.
_GONE: Final = frozenset({404, 410})


@dataclass(frozen=True)
class Notification:
    """One lock-screen safe notification and the screen a tap opens."""

    type: str
    key: str
    title: str
    body: str
    url: str
    # Further event keys this notification stands for; it is sent while any is new.
    covers: tuple[str, ...] = ()

    @property
    def keys(self) -> tuple[str, ...]:
        return (self.key, *self.covers)


@dataclass(frozen=True)
class Claim:
    """A notification due for delivery: its devices, key and the keys it made new."""

    notification: Notification
    subscriptions: list[dict[str, Any]]
    private: str
    new_keys: tuple[str, ...]


class PushService:
    """Own the VAPID key, the subscriptions, the switches and delivery."""

    def __init__(
        self,
        database: Callable[[], Any],
        key_values: Any,
        now: Callable[[], datetime],
        *,
        sender: Callable[..., int] = web_push.send,
    ) -> None:
        self._database = database
        self._key_values = key_values
        self._now = now
        self._sender = sender

    def stored(self, db: Any, key: str, default: Any) -> Any:
        """The decoded setting ``key`` (also another owner's), ``default`` without one."""
        return self._read(db, key, default)

    def _read(self, db: Any, key: str, default: Any) -> Any:
        return _stored(self._key_values, db, key, default)

    def _write(self, db: Any, key: str, value: Any) -> None:
        self._key_values.set(db, key, json.dumps(value, separators=(",", ":")))

    def _private_key(self, db: Any) -> str:
        """The stored VAPID private key, generated on first use."""
        private = self._read(db, PUSH_PRIVATE_KEY, "")
        if not private:
            private = web_push.generate_private_key()
            self._write(db, PUSH_PRIVATE_KEY, private)
        return str(private)

    def prepare(self) -> dict[str, str]:
        """The public key a browser subscribes with, generating the pair once."""
        with self._database().unit_of_work() as db:
            return {"public_key": web_push.public_key(self._private_key(db))}

    def state(self, endpoint: str | None = None) -> dict[str, Any]:
        """The switches and whether ``endpoint`` (else any device) is subscribed."""
        with self._database().reader() as db:
            subscriptions = self._read(db, PUSH_SUBSCRIPTIONS_KEY, [])
            preferences = self._preferences(db)
        return {
            "subscribed": any(
                item.get("endpoint") == endpoint for item in subscriptions
            )
            if endpoint
            else bool(subscriptions),
            "types": preferences,
        }

    def _preferences(self, db: Any) -> dict[str, bool]:
        stored = self._read(db, PUSH_PREFERENCES_KEY, {})
        return {name: stored.get(name) is not False for name in TYPES}

    def subscribe(self, payload: Any) -> dict[str, Any]:
        """Store one browser subscription (replacing the same endpoint)."""
        subscription = _subscription(payload)
        with self._database().unit_of_work() as db:
            stored = self._read(db, PUSH_SUBSCRIPTIONS_KEY, [])
            # A device sending its subscription again keeps when it first subscribed.
            created = next(
                (
                    item.get("created_at")
                    for item in stored
                    if item.get("endpoint") == subscription["endpoint"]
                ),
                None,
            )
            current = [
                item
                for item in stored
                if item.get("endpoint") != subscription["endpoint"]
            ]
            current.append(
                subscription | {"created_at": created or self._now().isoformat()}
            )
            self._write(db, PUSH_SUBSCRIPTIONS_KEY, current[-MAX_SUBSCRIPTIONS:])
        return self.state(subscription["endpoint"])

    def unsubscribe(self, payload: Any) -> dict[str, Any]:
        """Forget one endpoint; without one, every subscription."""
        endpoint = payload.get("endpoint") if isinstance(payload, Mapping) else None
        with self._database().unit_of_work() as db:
            current = self._read(db, PUSH_SUBSCRIPTIONS_KEY, [])
            kept = (
                [item for item in current if item.get("endpoint") != endpoint]
                if endpoint
                else []
            )
            self._write(db, PUSH_SUBSCRIPTIONS_KEY, kept)
        return self.state(endpoint)

    def set_preferences(self, payload: Any) -> dict[str, Any]:
        """Switch types on or off; unknown types or non-boolean values are refused."""
        types = payload.get("types") if isinstance(payload, Mapping) else None
        if (
            not isinstance(types, Mapping)
            or not types
            or any(
                name not in TYPES or type(on) is not bool for name, on in types.items()
            )
        ):
            raise AppError(
                400,
                "Die Benachrichtigungsarten sind ungültig.",
                reason="invalid_push_types",
            )
        with self._database().unit_of_work() as db:
            self._write(db, PUSH_PREFERENCES_KEY, self._preferences(db) | dict(types))
        return self.state(payload.get("endpoint"))

    def notify(self, notification: Notification) -> bool:
        """Claim, deliver and settle in one go; True when a device received it."""
        claim = self.claim(notification)
        return claim is not None and self.settle(claim, self.deliver(claim))

    def claim(self, notification: Notification) -> Claim | None:
        """Record the notification's keys; the claim when it is new, on and has devices.

        Recorded before delivery, so a crash or a second check never sends it twice.
        Keys already recorded are refreshed, so an event that stays current (a
        stale threshold, a long failure streak) never ages out and repeats.
        """
        with self._database().unit_of_work() as db:
            subscriptions = self._read(db, PUSH_SUBSCRIPTIONS_KEY, [])
            if not subscriptions:
                return None
            sent = self._read(db, PUSH_SENT_KEY, {})
            new_keys = tuple(key for key in notification.keys if key not in sent)
            now = self._now()
            sent |= dict.fromkeys(notification.keys, now.isoformat())
            self._write(db, PUSH_SENT_KEY, _pruned(sent, now))
            if not new_keys or not self._preferences(db)[notification.type]:
                return None
            return Claim(notification, subscriptions, self._private_key(db), new_keys)

    def deliver(self, claim: Claim) -> dict[str, int]:
        """Send the claimed notification to each device; its push service status."""
        notification = claim.notification
        message = {
            "title": notification.title,
            "body": notification.body,
            "url": notification.url,
            # One tag per event, so two events never replace each other on the phone.
            "tag": notification.key,
        }
        return {
            item["endpoint"]: self._sender(
                item, message, claim.private, ttl=_TTL_SECONDS
            )
            for item in claim.subscriptions
        }

    def settle(self, claim: Claim, statuses: Mapping[str, int]) -> bool:
        """Forget gone devices and count; a claim nobody received is retried later."""
        kind = claim.notification.type
        gone = {endpoint for endpoint, status in statuses.items() if status in _GONE}
        delivered = sum(1 for status in statuses.values() if 200 <= status < 300)
        failed = len(statuses) - delivered
        with self._database().unit_of_work() as db:
            if gone:
                self._write(
                    db,
                    PUSH_SUBSCRIPTIONS_KEY,
                    [
                        item
                        for item in self._read(db, PUSH_SUBSCRIPTIONS_KEY, [])
                        if item.get("endpoint") not in gone
                    ],
                )
            if not delivered and any(
                _transient(status) for status in statuses.values()
            ):
                # No device got it and one may later (network down, 5xx, 429);
                # a refusal such as 401/403 stays handled, so it never repeats.
                sent = self._read(db, PUSH_SENT_KEY, {})
                for key in claim.new_keys:
                    sent.pop(key, None)
                self._write(db, PUSH_SENT_KEY, sent)
            counts = self._read(db, PUSH_COUNTS_KEY, {})
            entry = counts.get(kind) if isinstance(counts.get(kind), dict) else {}
            entry = {
                "sent": int(entry.get("sent", 0)) + delivered,
                "failed": int(entry.get("failed", 0)) + failed,
            }
            self._write(db, PUSH_COUNTS_KEY, counts | {kind: entry})
        if failed:
            # The status only: endpoints and keys never reach the log.
            _LOGGER.warning(
                "Push delivery failed",
                extra={
                    "event": "push_delivery_failed",
                    "context": {
                        "type": kind,
                        "failed": failed,
                        "removed": len(gone),
                        "statuses": sorted(set(statuses.values())),
                    },
                },
            )
        return delivered > 0

    def seed(self, marker: str, keys: Iterable[str]) -> bool:
        """Record ``keys`` as sent once, without delivery; True when this call did.

        ``marker`` is push bookkeeping in ``PUSH_SEEDED_KEY``: what was already
        there when an event kind began is no news (#4135).
        """
        with self._database().unit_of_work() as db:
            seeded = self._read(db, PUSH_SEEDED_KEY, [])
            if marker in seeded:
                return False
            now = self._now()
            sent = self._read(db, PUSH_SENT_KEY, {}) | dict.fromkeys(
                keys, now.isoformat()
            )
            self._write(db, PUSH_SENT_KEY, _pruned(sent, now))
            self._write(db, PUSH_SEEDED_KEY, [*seeded, marker])
            return True

    def subscribed_since(self) -> datetime | None:
        """When the oldest current subscription was made; None without one."""
        with self._database().reader() as db:
            subscriptions = self._read(db, PUSH_SUBSCRIPTIONS_KEY, [])
        times = [_parse(item.get("created_at")) for item in subscriptions]
        known = [value for value in times if value is not None]
        return min(known) if known else None


def _stored(key_values: Any, db: Any, key: str, default: Any) -> Any:
    """The decoded value of ``key``, ``default`` when missing or of another shape."""
    raw = key_values.get(db, key)
    if not raw:
        return default
    try:
        value = json.loads(raw)
    except ValueError:
        return default
    return value if isinstance(value, type(default)) else default


def diagnostics(key_values: Any, db: Any) -> dict[str, Any]:
    """Counts only: subscribed devices and sent/failed per type."""
    counts = _stored(key_values, db, PUSH_COUNTS_KEY, {})
    return {
        "subscriptions": len(_stored(key_values, db, PUSH_SUBSCRIPTIONS_KEY, [])),
        "per_type": {
            name: counts[name] for name in TYPES if isinstance(counts.get(name), dict)
        },
    }


def _subscription(payload: Any) -> dict[str, str]:
    """The endpoint and keys of a ``PushSubscription.toJSON()`` body, validated."""
    keys = payload.get("keys") if isinstance(payload, Mapping) else None
    endpoint = payload.get("endpoint") if isinstance(payload, Mapping) else None
    if not isinstance(keys, Mapping) or not web_push.supported_endpoint(endpoint):
        raise AppError(
            400,
            "Dieser Browser wird für Push-Benachrichtigungen nicht unterstützt.",
            reason="unsupported_push_endpoint",
        )
    p256dh, auth = keys.get("p256dh"), keys.get("auth")
    try:
        valid = (
            web_push.valid_point(str(p256dh))
            and len(web_push.unb64url(str(auth))) == 16
        )
    except ValueError:
        valid = False
    if not valid:
        raise AppError(
            400, "Das Push-Abonnement ist ungültig.", reason="invalid_push_keys"
        )
    return {"endpoint": str(endpoint), "p256dh": str(p256dh), "auth": str(auth)}


def _transient(status: int) -> bool:
    """No answer, a server error or a rate limit: a later attempt may succeed."""
    return status == 0 or status == 429 or status >= 500


def _parse(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _pruned(sent: dict[str, Any], now: datetime) -> dict[str, Any]:
    cutoff = now - timedelta(days=_SENT_RETENTION_DAYS)
    return {
        key: at
        for key, at in sent.items()
        if (parsed := _parse(at)) is None or parsed >= cutoff
    }

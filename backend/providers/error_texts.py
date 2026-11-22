"""The one German text per provider failure reason, for the chat and the status line."""

from __future__ import annotations

_SPEND_LIMIT = "Das {p}-Ausgabenlimit ist erreicht. Prüfe das Limit im {p}-Konto."
_CREDIT = "Das {p}-Guthaben ist aufgebraucht. Lade es im {p}-Konto auf."
_TEXTS = {
    "authentication_or_permission": (
        "{p} hat den Zugang abgelehnt. Prüfe den API-Schlüssel unter Mehr › Anbindungen."
    ),
    "insufficient_quota": _CREDIT,
    "credit_balance_exhausted": _CREDIT,
    "organization_spend_limit_exceeded": _SPEND_LIMIT,
    "project_spend_limit_exceeded": _SPEND_LIMIT,
    "organization_usage_limit_exceeded": _SPEND_LIMIT,
    "rate_limit_exceeded": "{p} begrenzt gerade die Anfragen. Versuche es in Kürze noch einmal.",
    "provider_unavailable": "{p} ist vorübergehend nicht verfügbar. Versuche es in Kürze noch einmal.",
    "not_found": "{p} kennt das gewählte Modell nicht. Prüfe es unter Mehr › Coach & Modell.",
}
PROVIDER_NAMES = {"openai": "OpenAI", "anthropic": "Claude"}


FAILURE_REASONS = frozenset(_TEXTS)


def failure_text(reason: str, provider: str = "openai") -> str:
    """The text of a known provider failure reason, naming the selected provider."""
    return _TEXTS[reason].format(p=PROVIDER_NAMES.get(provider, "OpenAI"))

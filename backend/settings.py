"""Application settings selection and validation.

The service is deliberately persistence-agnostic: configuration and key/value
storage are supplied by the application composition root.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from backend.config import DEFAULT_OPENAI_BASE_URL, Config
from backend.errors import AppError
from backend.providers.claude_models import FAMILIES, SETTING_FAMILIES, setting_family

# The athlete's opt-in automatic Coach turns: the morning check-in (#4274) and
# the analysis of each new session (#4275), per sport group; running and
# cycling are preselected (athlete, 2026-11-27).
AUTOMATIC_TURNS_KEY = "coach_automatic_turns"
AUTOMATIC_TURNS = ("morning", "sessions")
SESSION_SPORTS = {"run": True, "ride": True, "strength": False, "other": False}


def automatic_turns_from(stored: Any) -> dict[str, Any]:
    """The automatic turn switches of a decoded stored value, off unless true,
    and the analysed sport groups, running and cycling unless set (#4275)."""
    stored = stored if isinstance(stored, dict) else {}
    sports = stored.get("sports")
    sports = sports if isinstance(sports, dict) else {}
    return {
        **{name: stored.get(name) is True for name in AUTOMATIC_TURNS},
        "sports": {
            sport: sports[sport] if isinstance(sports.get(sport), bool) else default
            for sport, default in SESSION_SPORTS.items()
        },
    }


def analysed_session_sports(turns: dict[str, Any]) -> frozenset[str]:
    """The sport groups whose new sessions are analysed; none while switched off."""
    if not turns["sessions"]:
        return frozenset()
    return frozenset(sport for sport, on in turns["sports"].items() if on)


MODEL_OPTIONS = (
    {
        "id": "gpt-6-luna",
        "label": "GPT-6 Luna",
        "description": "Für den Alltag",
    },
    {"id": "gpt-6-sol", "label": "GPT-6 Sol"},
    {"id": "gpt-6.1-sol", "label": "GPT-6.1 Sol"},
    {"id": "gpt-6-astra", "label": "GPT-6 Astra"},
)
CLAUDE_LABELS = {"opus": "Opus", "sonnet": "Sonnet", "haiku": "Haiku"}
SERVICE_TIER_OPTIONS = (
    {
        "id": "default",
        "label": "Standard",
    },
    {
        "id": "fast",
        "label": "Schnell",
        "description": "Mit Aufpreis",
    },
    {
        "id": "ultrafast",
        "label": "Sehr schnell",
        "description": "Mit Aufpreis",
    },
)


def supported_service_tiers(model: str) -> set[str]:
    if model in {"gpt-6.1-sol", "gpt-6-astra"}:
        return {"default", "fast", "ultrafast"}
    if model in {"gpt-6-luna", "gpt-6-sol"}:
        return {"default", "fast"}
    return {"default"}


THINKING_LEVEL_OPTIONS = (
    {
        "id": "low",
        "label": "Niedrig",
        "description": "Schnellere Antworten mit weniger zusätzlicher Überlegung",
    },
    {
        "id": "medium",
        "label": "Mittel",
        "description": "Ausgewogene Qualität, Geschwindigkeit und Kosten",
    },
    {
        "id": "high",
        "label": "Hoch",
        "description": "Gründlichere Überlegung für komplexe Trainingsfragen",
    },
)
# Each task has its own explicit model and thinking level. The conversation uses
# the original keys; planning and nutrition fall back to it until chosen.
TASKS = ("conversation", "planning", "nutrition")
_MODEL_KEYS = {
    "conversation": "selected_model_openai",
    "planning": "selected_model_planning",
    "nutrition": "selected_model_nutrition",
}
_THINKING_KEYS = {
    "conversation": "selected_thinking_level",
    "planning": "selected_thinking_level_planning",
    "nutrition": "selected_thinking_level_nutrition",
}


def _copy_options(
    options: tuple[dict[str, str], ...] | list[dict[str, str]],
) -> list[dict[str, str]]:
    return [dict(option) for option in options]


def _task(task: Any) -> str:
    if task not in TASKS:
        raise AppError(400, "Unbekannte Coach-Aufgabe.")
    return task


def _key_ready(config: Config, provider: str) -> bool:
    """Whether a model provider has a non-blank server-side key."""
    key = config.anthropic_api_key if provider == "anthropic" else config.openai_api_key
    return bool(key.strip())


class SettingsService:
    """Own settings policy while leaving configuration and persistence outside."""

    def __init__(
        self,
        config_supplier: Callable[[], Config],
        get_value: Callable[[str], str | None],
        set_value: Callable[[str, str], None],
        claude_models: Callable[[], dict[str, dict[str, str | None]]] = dict,
    ) -> None:
        self._config_supplier = config_supplier
        self._get_value = get_value
        self._set_value = set_value
        # Per family: the model in use and a rejected newer one (no provider call).
        self._claude_models = claude_models

    @staticmethod
    def _openai_options(config: Config) -> list[dict[str, Any]]:
        options: list[dict[str, Any]] = (
            _copy_options(MODEL_OPTIONS)
            if config.openai_base_url.rstrip("/") == DEFAULT_OPENAI_BASE_URL
            else []
        )
        configured_model = config.openai_model
        if configured_model not in {option["id"] for option in options}:
            options.insert(
                0,
                {
                    "id": configured_model,
                    "label": f"{configured_model} (konfiguriert)",
                    "description": "In .env konfiguriert",
                },
            )
        return options

    def _model_ids(self, config: Config) -> set[str]:
        """The selectable ids, without reading the Claude model state."""
        return {option["id"] for option in self._openai_options(config)} | set(
            SETTING_FAMILIES
        )

    def _available_model_options(self, config: Config) -> list[dict[str, Any]]:
        options = self._openai_options(config)
        for option in options:
            option.update(provider="openai", disabled=not _key_ready(config, "openai"))
        status = self._claude_models()
        for family in FAMILIES:
            family_status = status.get(family) or {}
            options.append(
                {
                    "id": f"claude-{family}",
                    "label": CLAUDE_LABELS[family],
                    "provider": "anthropic",
                    "model": family_status.get("model"),
                    "rejected": family_status.get("rejected"),
                    "disabled": not _key_ready(config, "anthropic"),
                }
            )
        return options

    def _selected_model(self, config: Config, task: str = "conversation") -> str:
        configured = self._model_ids(config)
        stored = self._get_value(_MODEL_KEYS[_task(task)])
        if stored in configured:
            return stored
        if task != "conversation":
            return self._selected_model(config)
        return config.openai_model

    def available_model_options(self) -> list[dict[str, Any]]:
        config = self._config_supplier()
        return self._available_model_options(config)

    def selected_model(self, task: str = "conversation") -> str:
        return self._selected_model(self._config_supplier(), task)

    def selected_provider(self, task: str = "conversation") -> str:
        """anthropic for a Claude family, else openai."""
        return "anthropic" if setting_family(self.selected_model(task)) else "openai"

    def coach_ready(self, task: str = "conversation") -> bool:
        """The provider of the task's selected model has its key (#2639)."""
        return _key_ready(self._config_supplier(), self.selected_provider(task))

    def save_model(self, model: Any, task: Any = "conversation") -> dict[str, str]:
        task = _task(task)
        config = self._config_supplier()
        model_id = str(model or "").strip()
        if model_id not in self._model_ids(config):
            raise AppError(400, "Nicht unterstützte Modellauswahl.")
        provider = "anthropic" if setting_family(model_id) else "openai"
        if not _key_ready(config, provider):
            raise AppError(
                400,
                "Für dieses Modell fehlt der API-Schlüssel unter Mehr › Anbindungen.",
            )
        # One setting per task for both providers; the first key predates Claude.
        self._set_value(_MODEL_KEYS[task], model_id)
        return {"model": model_id}

    def available_thinking_level_options(self) -> list[dict[str, str]]:
        self._config_supplier()
        return _copy_options(THINKING_LEVEL_OPTIONS)

    def selected_thinking_level(self, task: str = "conversation") -> str:
        self._config_supplier()
        configured = {option["id"] for option in THINKING_LEVEL_OPTIONS}
        stored = self._get_value(_THINKING_KEYS[_task(task)])
        if stored in configured:
            return stored
        return "medium" if task == "conversation" else self.selected_thinking_level()

    def save_thinking_level(
        self, level: Any, task: Any = "conversation"
    ) -> dict[str, str]:
        self._config_supplier()
        task = _task(task)
        level_id = str(level or "").strip().lower()
        if level_id not in {option["id"] for option in THINKING_LEVEL_OPTIONS}:
            raise AppError(400, "Nicht unterstütztes Thinking Level.")
        self._set_value(_THINKING_KEYS[task], level_id)
        return {"thinking_level": level_id}

    def task_settings(self) -> dict[str, dict[str, Any]]:
        """The model and thinking level each task uses, and whether it can run."""
        return {
            task: {
                "model": self.selected_model(task),
                "thinking_level": self.selected_thinking_level(task),
                "ready": self.coach_ready(task),
            }
            for task in TASKS
        }

    def _service_tiers(self, model: str) -> set[str]:
        config = self._config_supplier()
        if config.openai_base_url.rstrip("/") != DEFAULT_OPENAI_BASE_URL:
            return {"default"}
        return supported_service_tiers(model)

    def service_tier(self, model: str | None = None) -> str | None:
        supported = self._service_tiers(model or self.selected_model())
        if supported == {"default"}:
            return None
        return self._stored_service_tier(supported)

    def _stored_service_tier(self, supported: set[str]) -> str:
        stored = self._get_value("selected_service_tier")
        if stored in supported:
            return stored
        return "fast" if "fast" in supported else "default"

    def _task_service_tiers(self) -> set[str]:
        """The speeds any task's model offers; each request keeps only its own."""
        return set().union(
            *(self._service_tiers(self.selected_model(t)) for t in TASKS)
        )

    def service_tier_state(self) -> dict[str, Any]:
        supported = self._task_service_tiers()
        return {
            "selected": self._stored_service_tier(supported),
            "options": [
                dict(option)
                for option in SERVICE_TIER_OPTIONS
                if option["id"] in supported
            ],
        }

    def save_service_tier(self, tier: Any) -> dict[str, str]:
        tier_id = str(tier or "").strip().lower()
        if tier_id not in self._task_service_tiers():
            raise AppError(
                400, "Nicht unterstützter Geschwindigkeitsmodus für dieses Modell."
            )
        self._set_value("selected_service_tier", tier_id)
        return {"service_tier": tier_id}

    def automatic_turns(self) -> dict[str, Any]:
        """The athlete's opt-in automatic Coach turns (#4274), all off by default,
        and the sports whose new sessions are analysed (#4275)."""
        try:
            stored = json.loads(self._get_value(AUTOMATIC_TURNS_KEY) or "{}")
        except ValueError:
            stored = {}
        return automatic_turns_from(stored)

    def save_automatic_turns(self, values: Any) -> dict[str, Any]:
        """Switch automatic turns or analysed sports on or off; only known names
        with true/false."""
        if not isinstance(values, dict) or not values:
            raise AppError(400, "Unbekannte automatische Coach-Einstellung.")
        sports = values.get("sports", {})
        if (
            any(name not in (*AUTOMATIC_TURNS, "sports") for name in values)
            or any(
                not isinstance(values[name], bool)
                for name in AUTOMATIC_TURNS
                if name in values
            )
            or not isinstance(sports, dict)
            or any(
                sport not in SESSION_SPORTS or not isinstance(value, bool)
                for sport, value in sports.items()
            )
        ):
            raise AppError(400, "Unbekannte automatische Coach-Einstellung.")
        current = self.automatic_turns()
        updated = current | values | {"sports": current["sports"] | sports}
        self._set_value(AUTOMATIC_TURNS_KEY, json.dumps(updated, sort_keys=True))
        return updated

    def automatic_session_sports(self) -> frozenset[str]:
        """The sport groups whose new sessions are analysed; none while switched off."""
        return analysed_session_sports(self.automatic_turns())

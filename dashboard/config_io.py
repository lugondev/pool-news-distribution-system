"""Public config I/O — single API, swappable backend.

All callers stay backend-agnostic. The actual storage (YAML files in the
volume vs Postgres tables in Supabase) is selected by the CONFIG_BACKEND
env var; see `dashboard.config_backend.get_backend()`.

Reads use mtime-based caching for YAML mode and TTL caching for DB mode —
both transparent to the caller. Writes invalidate the relevant cache.
"""

import logging
from collections.abc import Callable

from dashboard.config_backend import get_backend

logger = logging.getLogger(__name__)

_settings_saved_callbacks: list[Callable] = []


def on_settings_saved(fn: Callable) -> None:
    """Register a callback invoked after every write_settings() call."""
    _settings_saved_callbacks.append(fn)


# ─── Sources ─────────────────────────────────────────────────────────────────

def read_sources() -> list[dict]:
    return get_backend().read_sources()


def write_sources(sources: list[dict]) -> None:
    get_backend().write_sources(sources)


# ─── Settings ────────────────────────────────────────────────────────────────

def read_settings() -> dict:
    return get_backend().read_settings()


def write_settings(cfg: dict) -> None:
    get_backend().write_settings(cfg)
    for fn in _settings_saved_callbacks:
        try:
            fn()
        except Exception as exc:
            logger.warning("settings-saved callback %s failed: %s", fn.__name__, exc)


# ─── Social agents (used by ai/social_poster + dashboard routes) ─────────────

def read_social_agents() -> list[dict]:
    return get_backend().read_social_agents()


def write_social_agents(agents: list[dict]) -> None:
    get_backend().write_social_agents(agents)


# ─── Sim personas (used by ai/social_sim + dashboard routes) ─────────────────

def read_sim_personas() -> dict:
    return get_backend().read_sim_personas()


def write_sim_personas(personas: dict) -> None:
    get_backend().write_sim_personas(personas)


# ─── Convenience helpers (derived from settings) ─────────────────────────────

def get_categories() -> list[dict]:
    return read_settings().get("categories", [])


def get_active_category_ids() -> set[str]:
    return {c["id"] for c in get_categories() if c.get("enabled", True)}


def get_webhook_endpoints() -> list[dict]:
    return read_settings().get("webhook", {}).get("endpoints", [])


def save_webhook_endpoints(endpoints: list[dict]) -> None:
    cfg = read_settings()
    cfg.setdefault("webhook", {})["endpoints"] = endpoints
    write_settings(cfg)


def get_telegram_channels() -> list[dict]:
    return read_settings().get("telegram", {}).get("channels", [])


def save_telegram_channels(channels: list[dict]) -> None:
    cfg = read_settings()
    cfg.setdefault("telegram", {})["channels"] = channels
    write_settings(cfg)


def get_content_channels() -> list[dict]:
    return read_settings().get("channels", [])


def save_content_channels(channels: list[dict]) -> None:
    cfg = read_settings()
    cfg["channels"] = channels
    write_settings(cfg)


def get_channels_config() -> dict:
    return read_settings().get("channels_config", {})


def save_channels_config(channels_config: dict) -> None:
    cfg = read_settings()
    cfg["channels_config"] = channels_config
    write_settings(cfg)

"""Shared YAML I/O for sources and settings config files.

Single source of truth for reading and writing config/sources.yaml and
config/settings.yaml — imported by both JSON API routes and HTML UI routes.

Reads use mtime-based caching (storage.config_cache) to avoid disk I/O
on every HTMX poll. Writes call invalidate() so the next read reloads.
"""

import os

import yaml

from storage.config_cache import cached_yaml, invalidate

_BASE_DIR = os.path.dirname(os.path.dirname(__file__))
SOURCES_PATH = os.path.join(_BASE_DIR, "config", "sources.yaml")
SETTINGS_PATH = os.path.join(_BASE_DIR, "config", "settings.yaml")


# ── Sources ──────────────────────────────────────────────────────────────────


def read_sources() -> list[dict]:
    return cached_yaml(SOURCES_PATH).get("sources", [])


def write_sources(sources: list[dict]) -> None:
    with open(SOURCES_PATH, "w") as f:
        yaml.dump(
            {"sources": sources},
            f,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        )
    invalidate(SOURCES_PATH)


# ── Settings ─────────────────────────────────────────────────────────────────


def read_settings() -> dict:
    return cached_yaml(SETTINGS_PATH)


def write_settings(cfg: dict) -> None:
    with open(SETTINGS_PATH, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    invalidate(SETTINGS_PATH)


# ── Convenience helpers ───────────────────────────────────────────────────────


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

"""Shared UI helpers: template filters and log enrichment utilities."""

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from dashboard.config_io import read_settings
from dashboard.redis_state import get_redis
from storage.redis_store import get_article

logger = logging.getLogger(__name__)


def get_app_tz() -> ZoneInfo:
    try:
        cfg = read_settings()
        tz_name = cfg.get("app", {}).get("timezone", "UTC")
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("UTC")


def fmt_dt(iso_str: str, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if not iso_str:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(get_app_tz()).strftime(fmt)
    except Exception:
        return iso_str[:16].replace("T", " ")


def rel_time(iso_str: str) -> str:
    """Human-friendly relative time: '2m', '3h', '5d', then '21/03' for >1 week."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = int((datetime.now(timezone.utc) - dt).total_seconds())
        if delta < 0:
            return "just now"
        if delta < 60:
            return f"{delta}s"
        if delta < 3600:
            return f"{delta // 60}m"
        if delta < 86400:
            return f"{delta // 3600}h"
        if delta < 604800:
            return f"{delta // 86400}d"
        return fmt_dt(iso_str, "%d/%m")
    except Exception:
        return ""


def dt_lag(fetched_str: str, published_str: str) -> str:
    try:
        pub = datetime.fromisoformat(published_str)
        fet = datetime.fromisoformat(fetched_str)
        if pub.tzinfo is None:
            pub = pub.replace(tzinfo=timezone.utc)
        if fet.tzinfo is None:
            fet = fet.replace(tzinfo=timezone.utc)
        delta = int((fet - pub).total_seconds())
        if delta < 0:
            return ""
        if delta < 60:
            return f"+{delta}s"
        if delta < 3600:
            return f"+{delta // 60}m"
        h, m = divmod(delta, 3600)
        return f"+{h}h{m // 60}m" if m >= 60 else f"+{h}h"
    except Exception:
        return ""


def pick_title(article: dict, target_language: str | None = None) -> str:
    """Pick the best display title for an article given a target language."""
    if article.get("type") == "synthetic":
        if target_language:
            lang_title = article.get(f"title_{target_language}")
            if lang_title:
                return lang_title
        return article.get("title_en") or article.get("title_vi") or "—"
    return article.get("title") or "—"


async def enrich_logs(
    logs: list[dict], full: bool = False, include_content: bool = False
) -> list[dict]:
    """Attach article data to each log entry from Redis."""
    redis = get_redis()
    for log in logs:
        article = await get_article(redis, log["article_id"])
        if article:
            log["article_title"] = pick_title(article)
            log["article_type"] = article.get("type", "original")
            log["_title"] = article.get("title", "")
            log["_title_vi"] = article.get("title_vi", "")
            log["_title_en"] = article.get("title_en", "")
            if full:
                log["source_name"] = article.get("source_name", "")
                log["lang"] = article.get("lang", "")
                log["category"] = article.get("category", "")
                log["url"] = article.get("url", "")
                log["ai_summary_vi"] = article.get("ai_summary_vi", "")
                log["ai_summary_en"] = article.get("ai_summary_en", "")
                log["ai_status"] = article.get("ai_status", "")
            if include_content:
                log["article_content"] = article.get("content", "")
                log["article_summary"] = article.get("summary", "")
        else:
            log["article_title"] = "—"
    return logs

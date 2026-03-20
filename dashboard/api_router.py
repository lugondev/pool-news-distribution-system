"""
JSON API router — full CRUD for sources, categories, webhooks, AI settings, logs, and health.
"""
import logging
import math
import os
from datetime import datetime, timezone

import redis.asyncio as aioredis
import yaml
from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel

from storage.redis_store import (
    get_article,
    get_feed_stats,
    get_latest_articles,
    get_pending_ai_articles,
)
from storage.sqlite_stats import (
    get_crawl_domain_summary,
    get_crawl_error_breakdown,
    get_crawl_logs,
    get_crawl_source_summary,
    get_crawl_timeline,
    get_dashboard_stats,
    get_recent_ai_logs,
    get_recent_webhook_logs,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["api"])

_BASE_DIR = os.path.dirname(os.path.dirname(__file__))
SOURCES_PATH = os.path.join(_BASE_DIR, "config", "sources.yaml")
SETTINGS_PATH = os.path.join(_BASE_DIR, "config", "settings.yaml")

DEDUP_KEY = "news:dedup:simhashes"
LOG_PAGE_SIZE = 20


def _read_sources() -> list[dict]:
    with open(SOURCES_PATH) as f:
        return yaml.safe_load(f).get("sources", [])


def _write_sources(sources: list[dict]) -> None:
    with open(SOURCES_PATH, "w") as f:
        yaml.dump({"sources": sources}, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


def _read_settings() -> dict:
    with open(SETTINGS_PATH) as f:
        return yaml.safe_load(f)


def _write_settings(cfg: dict) -> None:
    with open(SETTINGS_PATH, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


_redis: aioredis.Redis | None = None


def set_redis(r: aioredis.Redis) -> None:
    global _redis
    _redis = r


def _get_redis() -> aioredis.Redis:
    if _redis is None:
        raise RuntimeError("Redis not initialized")
    return _redis


# ── Health ───────────────────────────────────────────────────────────────────

@router.get("/health")
async def health():
    r = _get_redis()
    try:
        await r.ping()
        redis_ok = True
    except Exception:
        redis_ok = False
    return {"status": "ok" if redis_ok else "degraded", "redis": redis_ok}


# ── Articles ─────────────────────────────────────────────────────────────────

@router.get("/articles")
async def list_articles(limit: int = 50, offset: int = 0, source: str = None, category: str = None):
    articles, total = await get_latest_articles(
        _get_redis(), limit=limit, offset=offset,
        source_id=source or None, category=category or None,
    )
    return {"articles": articles, "count": len(articles), "total": total}


@router.get("/articles/{article_id}")
async def get_article_detail(article_id: str):
    article = await get_article(_get_redis(), article_id)
    if not article:
        raise HTTPException(404, "Article not found")
    return article


@router.get("/articles/pending/list")
async def list_pending_articles(limit: int = 20):
    articles = await get_pending_ai_articles(_get_redis(), limit=limit)
    return {"articles": articles, "count": len(articles)}


# ── Stats ────────────────────────────────────────────────────────────────────

@router.get("/stats")
async def stats():
    redis_stats = await get_feed_stats(_get_redis())
    db_stats = await get_dashboard_stats()
    return {"redis": redis_stats, "db": db_stats}


@router.get("/stats/dedup")
async def dedup_stats():
    r = _get_redis()
    count = await r.scard(DEDUP_KEY)
    return {"simhash_count": count}


# ── Sources ──────────────────────────────────────────────────────────────────

class SourceIn(BaseModel):
    id: str
    name: str
    url: str
    lang: str = "en"
    category: str = "world"


class SourceUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    lang: str | None = None
    category: str | None = None


@router.get("/sources")
async def list_sources():
    return {"sources": _read_sources()}


@router.post("/sources", status_code=201)
async def add_source(body: SourceIn):
    sources = _read_sources()
    if any(s["id"] == body.id for s in sources):
        raise HTTPException(409, f"Source '{body.id}' already exists")
    sources.append({
        "id": body.id, "name": body.name, "url": body.url,
        "type": "rss", "lang": body.lang, "category": body.category, "enabled": True,
    })
    _write_sources(sources)
    logger.info(f"API: source added: {body.id}")
    return {"ok": True, "source": sources[-1]}


@router.put("/sources/{source_id}")
async def update_source(source_id: str, body: SourceUpdate):
    sources = _read_sources()
    target = next((s for s in sources if s["id"] == source_id), None)
    if not target:
        raise HTTPException(404, "Source not found")
    for field in ("name", "url", "lang", "category"):
        val = getattr(body, field)
        if val is not None:
            target[field] = val
    _write_sources(sources)
    logger.info(f"API: source updated: {source_id}")
    return {"ok": True, "source": target}


@router.post("/sources/{source_id}/toggle")
async def toggle_source(source_id: str):
    sources = _read_sources()
    target = next((s for s in sources if s["id"] == source_id), None)
    if not target:
        raise HTTPException(404, "Source not found")
    target["enabled"] = not target.get("enabled", True)
    _write_sources(sources)
    return {"ok": True, "source": target}


@router.delete("/sources/{source_id}")
async def delete_source(source_id: str):
    sources = _read_sources()
    new = [s for s in sources if s["id"] != source_id]
    if len(new) == len(sources):
        raise HTTPException(404, "Source not found")
    _write_sources(new)
    logger.info(f"API: source deleted: {source_id}")
    return {"ok": True}


# ── Categories ───────────────────────────────────────────────────────────────

class CategoryIn(BaseModel):
    id: str
    name: str


@router.get("/categories")
async def list_categories():
    return {"categories": _read_settings().get("categories", [])}


@router.post("/categories", status_code=201)
async def add_category(body: CategoryIn):
    cfg = _read_settings()
    cats = cfg.get("categories", [])
    if any(c["id"] == body.id for c in cats):
        raise HTTPException(409, f"Category '{body.id}' already exists")
    cats.append({"id": body.id, "name": body.name, "enabled": True})
    cfg["categories"] = cats
    _write_settings(cfg)
    return {"ok": True, "category": cats[-1]}


@router.post("/categories/{cat_id}/toggle")
async def toggle_category(cat_id: str):
    cfg = _read_settings()
    cats = cfg.get("categories", [])
    target = next((c for c in cats if c["id"] == cat_id), None)
    if not target:
        raise HTTPException(404, "Category not found")
    target["enabled"] = not target.get("enabled", True)
    cfg["categories"] = cats
    _write_settings(cfg)
    return {"ok": True, "category": target}


@router.delete("/categories/{cat_id}")
async def delete_category(cat_id: str):
    cfg = _read_settings()
    cats = cfg.get("categories", [])
    new = [c for c in cats if c["id"] != cat_id]
    if len(new) == len(cats):
        raise HTTPException(404, "Category not found")
    cfg["categories"] = new
    _write_settings(cfg)
    return {"ok": True}


# ── AI Settings ──────────────────────────────────────────────────────────────

class AISettingsIn(BaseModel):
    enabled: bool | None = None
    model: str | None = None
    temperature: float | None = None
    batch_size: int | None = None
    max_tokens_summary: int | None = None
    retry_attempts: int | None = None
    output_languages: list[str] | None = None


@router.get("/settings/ai")
async def get_ai_settings():
    return _read_settings().get("ai", {})


@router.put("/settings/ai")
async def update_ai_settings(body: AISettingsIn):
    cfg = _read_settings()
    ai = cfg.get("ai", {})
    if body.enabled is not None:
        ai["enabled"] = body.enabled
    if body.model is not None:
        ai["model"] = body.model
    if body.temperature is not None:
        ai["temperature"] = max(0.0, min(body.temperature, 2.0))
    if body.batch_size is not None:
        ai["batch_size"] = max(1, min(body.batch_size, 20))
    if body.max_tokens_summary is not None:
        ai["max_tokens_summary"] = max(100, min(body.max_tokens_summary, 1000))
    if body.retry_attempts is not None:
        ai["retry_attempts"] = max(1, min(body.retry_attempts, 10))
    if body.output_languages is not None:
        ai["output_languages"] = body.output_languages
    cfg["ai"] = ai
    _write_settings(cfg)
    logger.info(f"API: AI settings updated")
    return {"ok": True, "ai": ai}


# ── AI Logs ──────────────────────────────────────────────────────────────────

@router.get("/logs/ai")
async def list_ai_logs(page: int = 1, limit: int = LOG_PAGE_SIZE):
    offset = (page - 1) * limit
    logs, total = await get_recent_ai_logs(limit=limit, offset=offset)
    return {
        "logs": logs, "total": total,
        "page": page, "total_pages": max(1, math.ceil(total / limit)),
    }


# ── Webhooks ─────────────────────────────────────────────────────────────────

class WebhookIn(BaseModel):
    id: str
    name: str
    url: str
    retry_attempts: int = 3
    retry_delay_seconds: int = 5
    timeout_seconds: int = 10
    payload_mode: str = "full"
    payload_fields: list[str] = []
    payload_template: str = ""


class WebhookUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    retry_attempts: int | None = None
    retry_delay_seconds: int | None = None
    timeout_seconds: int | None = None
    payload_mode: str | None = None
    payload_fields: list[str] | None = None
    payload_template: str | None = None


def _get_webhook_endpoints() -> list[dict]:
    return _read_settings().get("webhook", {}).get("endpoints", [])


def _save_webhook_endpoints(endpoints: list[dict]) -> None:
    cfg = _read_settings()
    cfg.setdefault("webhook", {})["endpoints"] = endpoints
    _write_settings(cfg)


@router.get("/webhooks")
async def list_webhooks():
    return {"endpoints": _get_webhook_endpoints()}


@router.post("/webhooks", status_code=201)
async def add_webhook(body: WebhookIn):
    endpoints = _get_webhook_endpoints()
    if any(ep["id"] == body.id for ep in endpoints):
        raise HTTPException(409, f"Webhook '{body.id}' already exists")
    ep = {
        "id": body.id, "name": body.name, "url": body.url, "enabled": True,
        "retry_attempts": max(1, min(body.retry_attempts, 10)),
        "retry_delay_seconds": max(1, min(body.retry_delay_seconds, 60)),
        "timeout_seconds": max(1, min(body.timeout_seconds, 60)),
        "payload_mode": body.payload_mode,
        "payload_fields": body.payload_fields,
        "payload_template": body.payload_template,
    }
    endpoints.append(ep)
    _save_webhook_endpoints(endpoints)
    logger.info(f"API: webhook added: {body.id}")
    return {"ok": True, "endpoint": ep}


@router.put("/webhooks/{wh_id}")
async def update_webhook(wh_id: str, body: WebhookUpdate):
    endpoints = _get_webhook_endpoints()
    target = next((ep for ep in endpoints if ep["id"] == wh_id), None)
    if not target:
        raise HTTPException(404, "Webhook not found")
    for field in ("name", "url", "retry_attempts", "retry_delay_seconds", "timeout_seconds",
                  "payload_mode", "payload_fields", "payload_template"):
        val = getattr(body, field, None)
        if val is not None:
            target[field] = val
    _save_webhook_endpoints(endpoints)
    logger.info(f"API: webhook updated: {wh_id}")
    return {"ok": True, "endpoint": target}


@router.post("/webhooks/{wh_id}/toggle")
async def toggle_webhook(wh_id: str):
    endpoints = _get_webhook_endpoints()
    target = next((ep for ep in endpoints if ep["id"] == wh_id), None)
    if not target:
        raise HTTPException(404, "Webhook not found")
    target["enabled"] = not target.get("enabled", True)
    _save_webhook_endpoints(endpoints)
    return {"ok": True, "endpoint": target}


@router.delete("/webhooks/{wh_id}")
async def delete_webhook(wh_id: str):
    endpoints = _get_webhook_endpoints()
    new = [ep for ep in endpoints if ep["id"] != wh_id]
    if len(new) == len(endpoints):
        raise HTTPException(404, "Webhook not found")
    _save_webhook_endpoints(new)
    logger.info(f"API: webhook deleted: {wh_id}")
    return {"ok": True}


# ── Webhook Logs ─────────────────────────────────────────────────────────────

@router.get("/logs/webhooks")
async def list_webhook_logs(page: int = 1, limit: int = LOG_PAGE_SIZE):
    offset = (page - 1) * limit
    logs, total = await get_recent_webhook_logs(limit=limit, offset=offset)
    return {
        "logs": logs, "total": total,
        "page": page, "total_pages": max(1, math.ceil(total / limit)),
    }


# ── Crawl Logs & Tracing ────────────────────────────────────────────────────

@router.get("/logs/crawl")
async def list_crawl_logs(
    page: int = 1,
    limit: int = LOG_PAGE_SIZE,
    source: str | None = None,
    domain: str | None = None,
    errors_only: bool = False,
    http_status: int | None = None,
    since: str | None = None,
):
    """Browse crawl logs with flexible filters."""
    offset = (page - 1) * limit
    logs, total = await get_crawl_logs(
        limit=limit, offset=offset,
        source_id=source, domain=domain,
        errors_only=errors_only, http_status=http_status, since=since,
    )
    return {
        "logs": logs, "total": total,
        "page": page, "total_pages": max(1, math.ceil(total / limit)),
    }


@router.get("/logs/crawl/sources")
async def crawl_source_summary(since: str | None = None):
    """Per-source success rate, avg duration, error counts."""
    rows = await get_crawl_source_summary(since=since)
    return {"sources": rows, "count": len(rows)}


@router.get("/logs/crawl/sources/{source_id}")
async def crawl_source_detail(
    source_id: str, page: int = 1, limit: int = LOG_PAGE_SIZE,
):
    """All crawl logs for a specific source."""
    offset = (page - 1) * limit
    logs, total = await get_crawl_logs(limit=limit, offset=offset, source_id=source_id)
    return {
        "source_id": source_id,
        "logs": logs, "total": total,
        "page": page, "total_pages": max(1, math.ceil(total / limit)),
    }


@router.get("/logs/crawl/domains")
async def crawl_domain_summary(since: str | None = None):
    """Per-domain stats: rate limits, 403s, avg latency -- key for IP-ban detection."""
    rows = await get_crawl_domain_summary(since=since)
    return {"domains": rows, "count": len(rows)}


@router.get("/logs/crawl/errors")
async def crawl_error_breakdown(since: str | None = None):
    """Error breakdown by type (429, 403, timeout, connection, etc.)."""
    rows = await get_crawl_error_breakdown(since=since)
    return {"errors": rows, "count": len(rows)}


@router.get("/logs/crawl/timeline")
async def crawl_timeline(hours: int = 24):
    """Hourly crawl performance: runs, found, saved, errors, avg latency."""
    rows = await get_crawl_timeline(hours=min(hours, 168))
    return {"timeline": rows, "hours": hours}


# ── Telegram Channels ────────────────────────────────────────────────────────

class TelegramChannelIn(BaseModel):
    id: str
    name: str
    bot_token: str
    chat_id: str
    lang: str = "both"
    retry_attempts: int = 3
    timeout_seconds: int = 10
    payload_mode: str = "full"
    payload_fields: list[str] = []
    payload_template: str = ""


class TelegramChannelUpdate(BaseModel):
    name: str | None = None
    bot_token: str | None = None
    chat_id: str | None = None
    lang: str | None = None
    retry_attempts: int | None = None
    timeout_seconds: int | None = None
    payload_mode: str | None = None
    payload_fields: list[str] | None = None
    payload_template: str | None = None


def _get_telegram_channels() -> list[dict]:
    return _read_settings().get("telegram", {}).get("channels", [])


def _save_telegram_channels(channels: list[dict]) -> None:
    cfg = _read_settings()
    cfg.setdefault("telegram", {})["channels"] = channels
    _write_settings(cfg)


@router.get("/telegram")
async def list_telegram_channels():
    return {"channels": _get_telegram_channels()}


@router.post("/telegram", status_code=201)
async def add_telegram_channel(body: TelegramChannelIn):
    channels = _get_telegram_channels()
    if any(ch["id"] == body.id for ch in channels):
        raise HTTPException(409, f"Telegram channel '{body.id}' already exists")
    ch = {
        "id": body.id, "name": body.name,
        "bot_token": body.bot_token, "chat_id": body.chat_id,
        "lang": body.lang, "enabled": True,
        "retry_attempts": max(1, min(body.retry_attempts, 10)),
        "timeout_seconds": max(1, min(body.timeout_seconds, 60)),
        "payload_mode": body.payload_mode,
        "payload_fields": body.payload_fields,
        "payload_template": body.payload_template,
    }
    channels.append(ch)
    _save_telegram_channels(channels)
    logger.info(f"API: telegram channel added: {body.id}")
    return {"ok": True, "channel": ch}


@router.put("/telegram/{ch_id}")
async def update_telegram_channel(ch_id: str, body: TelegramChannelUpdate):
    channels = _get_telegram_channels()
    target = next((ch for ch in channels if ch["id"] == ch_id), None)
    if not target:
        raise HTTPException(404, "Telegram channel not found")
    for field in ("name", "bot_token", "chat_id", "lang", "retry_attempts", "timeout_seconds",
                  "payload_mode", "payload_fields", "payload_template"):
        val = getattr(body, field, None)
        if val is not None:
            target[field] = val
    _save_telegram_channels(channels)
    logger.info(f"API: telegram channel updated: {ch_id}")
    return {"ok": True, "channel": target}


@router.post("/telegram/{ch_id}/toggle")
async def toggle_telegram_channel(ch_id: str):
    channels = _get_telegram_channels()
    target = next((ch for ch in channels if ch["id"] == ch_id), None)
    if not target:
        raise HTTPException(404, "Telegram channel not found")
    target["enabled"] = not target.get("enabled", True)
    _save_telegram_channels(channels)
    return {"ok": True, "channel": target}


@router.delete("/telegram/{ch_id}")
async def delete_telegram_channel(ch_id: str):
    channels = _get_telegram_channels()
    new = [ch for ch in channels if ch["id"] != ch_id]
    if len(new) == len(channels):
        raise HTTPException(404, "Telegram channel not found")
    _save_telegram_channels(new)
    logger.info(f"API: telegram channel deleted: {ch_id}")
    return {"ok": True}


@router.post("/telegram/{ch_id}/test")
async def test_telegram_channel(ch_id: str):
    """Send a test message to verify bot_token + chat_id work."""
    from webhook.telegram import send_telegram
    channels = _get_telegram_channels()
    target = next((ch for ch in channels if ch["id"] == ch_id), None)
    if not target:
        raise HTTPException(404, "Telegram channel not found")

    text = (
        "\u2705 <b>News Aggregator — Test Message</b>\n\n"
        f"Channel: <i>{target['name']}</i>\n"
        f"Chat ID: <code>{target['chat_id']}</code>\n\n"
        "If you see this, your Telegram integration is working!"
    )
    status, ok, error = await send_telegram(
        target["bot_token"], target["chat_id"], text,
        timeout=target.get("timeout_seconds", 10),
    )
    if ok:
        return {"ok": True, "message": "Test message sent successfully"}
    raise HTTPException(400, f"Telegram API error: {error}")


# ── Payload Template Validation & Preview ────────────────────────────────────

class TemplateValidateIn(BaseModel):
    template: str

SAMPLE_ARTICLE = {
    "id": "a1b2c3d4e5f67890",
    "source_id": "reuters_economy",
    "source_name": "Reuters Economy",
    "url": "https://reuters.com/article/example",
    "title": "Fed Holds Rates Steady Amid Global Uncertainty",
    "summary": "The Federal Reserve kept interest rates unchanged...",
    "content": "The Federal Reserve on Wednesday held its benchmark rate...",
    "lang": "en",
    "declared_lang": "en",
    "category": "finance",
    "published_at": "2026-03-20T10:30:00+00:00",
    "fetched_at": "2026-03-20T10:35:00+00:00",
    "ai_summary_vi": "Fed giữ nguyên lãi suất trong bối cảnh bất ổn toàn cầu.",
    "ai_summary_en": "The Federal Reserve held interest rates steady amid global uncertainty.",
    "ai_status": "done",
}


@router.post("/payload/validate")
async def validate_payload_template(body: TemplateValidateIn):
    """Validate a Jinja2 template and return preview with sample data."""
    from webhook.payload import validate_template, render_template
    ok, error = validate_template(body.template)
    if not ok:
        return {"ok": False, "error": error}
    preview = render_template(body.template, SAMPLE_ARTICLE)
    return {"ok": True, "preview": preview}


@router.get("/payload/fields")
async def list_payload_fields():
    """List all available fields for payload_mode=fields."""
    from webhook.payload import ALL_FIELDS
    return {"fields": ALL_FIELDS}


@router.post("/payload/preview")
async def preview_payload(body: dict = Body(...)):
    """Preview payload output for any mode config."""
    from webhook.payload import build_payload
    mode = body.get("payload_mode", "full")
    config = {
        "payload_mode": mode,
        "payload_fields": body.get("payload_fields", []),
        "payload_template": body.get("payload_template", ""),
    }
    result = build_payload(SAMPLE_ARTICLE, config)
    return {"mode": mode, "output": result}

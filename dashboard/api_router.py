"""
JSON API router — full CRUD for sources, categories, webhooks, AI settings, logs, and health.
"""

import json
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
    get_api_logs,
    get_api_summary,
    get_crawl_domain_summary,
    get_crawl_error_breakdown,
    get_crawl_logs,
    get_crawl_source_summary,
    get_crawl_timeline,
    get_dashboard_stats,
    get_recent_ai_logs,
    get_recent_webhook_logs,
    get_system_logs,
    get_system_summary,
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
        yaml.dump(
            {"sources": sources},
            f,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        )


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


def _format_article(a: dict) -> dict:
    """Normalize and expose both time fields clearly."""
    return {
        **a,
        "published_at": a.get("published_at", ""),
        "fetched_at": a.get("fetched_at", ""),
    }


@router.get("/news")
async def list_news(
    page: int = 1,
    limit: int = 20,
    source: str | None = None,
    category: str | None = None,
    lang: str | None = None,
    ai_status: str | None = None,
    article_type: str | None = None,  # NEW: "original", "synthetic", or None (all)
):
    """
    News list with filters.

    Query params:
    - page / limit     — pagination (limit max 100)
    - source           — filter by source_id
    - category         — filter by category id (world, tech, business, ...)
    - lang             — filter by detected language code (en, vi, ja, ...)
    - ai_status        — filter by AI processing state: pending, done, failed, dedup_skipped
    - article_type     — filter by type: "original" (RSS articles), "synthetic" (AI-generated), or None (all)
    """
    limit = max(1, min(limit, 100))
    offset = (page - 1) * limit

    # Over-fetch when post-filters (lang/ai_status) are active so pagination is accurate
    fetch_limit = limit * 5 if (lang or ai_status) else limit
    fetch_offset = (page - 1) * fetch_limit if (lang or ai_status) else offset

    articles, total = await get_latest_articles(
        _get_redis(),
        limit=fetch_limit,
        offset=fetch_offset if not (lang or ai_status) else 0,
        source_id=source or None,
        category=category or None,
        article_type=article_type or None,  # NEW
    )

    # Post-filter for lang / ai_status (not indexable in Redis without extra sets)
    if lang:
        articles = [a for a in articles if a.get("lang") == lang]
    if ai_status:
        articles = [a for a in articles if a.get("ai_status") == ai_status]

    # Slice to requested page when post-filtering
    if lang or ai_status:
        slice_offset = (page - 1) * limit
        total = len(articles)
        articles = articles[slice_offset : slice_offset + limit]
    else:
        total_pages = max(1, math.ceil(total / limit))

    total_pages = max(1, math.ceil(total / limit))

    return {
        "articles": [_format_article(a) for a in articles],
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "total_pages": total_pages,
            "has_prev": page > 1,
            "has_next": page < total_pages,
        },
        "filters": {
            "source": source,
            "category": category,
            "lang": lang,
            "ai_status": ai_status,
            "article_type": article_type,  # NEW
        },
    }


@router.get("/articles")
async def list_articles(
    limit: int = 50, offset: int = 0, source: str = None, category: str = None
):
    """Legacy endpoint — prefer /api/news for new integrations."""
    articles, total = await get_latest_articles(
        _get_redis(),
        limit=min(limit, 200),
        offset=offset,
        source_id=source or None,
        category=category or None,
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
    sources.append(
        {
            "id": body.id,
            "name": body.name,
            "url": body.url,
            "type": "rss",
            "lang": body.lang,
            "category": body.category,
            "enabled": True,
        }
    )
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


@router.post("/settings/ai/toggle")
async def toggle_ai_summary():
    """Quick-toggle AI summary on/off without touching other settings."""
    cfg = _read_settings()
    ai = cfg.setdefault("ai", {})
    ai["enabled"] = not ai.get("enabled", True)
    _write_settings(cfg)
    logger.info(f"API: AI summary toggled → {'on' if ai['enabled'] else 'off'}")
    return {"ok": True, "enabled": ai["enabled"]}


@router.post("/settings/ai/synthesis/toggle")
async def toggle_ai_synthesis():
    """Quick-toggle topic synthesis on/off without touching other settings."""
    cfg = _read_settings()
    synthesis = cfg.setdefault("ai", {}).setdefault("topic_synthesis", {})
    synthesis["enabled"] = not synthesis.get("enabled", False)
    _write_settings(cfg)
    logger.info(f"API: Topic synthesis toggled → {'on' if synthesis['enabled'] else 'off'}")
    return {"ok": True, "enabled": synthesis["enabled"]}


# ── AI Providers ──────────────────────────────────────────────────────────────


class ProviderIn(BaseModel):
    name: str
    api_key: str
    base_url: str
    model: str = ""


@router.get("/providers")
async def list_providers():
    cfg = _read_settings()
    providers = cfg.get("ai", {}).get("providers", [])
    # Mask api_key in list — use GET /providers/{id} to retrieve full data for editing
    return [
        {**p, "api_key": p["api_key"][:12] + "…" if p.get("api_key") else ""}
        for p in providers
    ]


@router.get("/providers/{provider_id}")
async def get_provider(provider_id: str):
    """Return full provider data (including unmasked api_key) for editing."""
    cfg = _read_settings()
    providers = cfg.get("ai", {}).get("providers", [])
    provider = next((p for p in providers if p["id"] == provider_id), None)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    return provider


@router.post("/providers")
async def create_provider(body: ProviderIn):
    import re
    cfg = _read_settings()
    ai = cfg.setdefault("ai", {})
    providers = ai.setdefault("providers", [])
    pid = re.sub(r"[^a-z0-9]+", "-", body.name.lower()).strip("-") or f"provider-{len(providers)+1}"
    if any(p["id"] == pid for p in providers):
        pid = f"{pid}-{len(providers)+1}"
    entry = {"id": pid, "name": body.name, "api_key": body.api_key, "base_url": body.base_url}
    if body.model:
        entry["model"] = body.model
    providers.append(entry)
    _write_settings(cfg)
    logger.info(f"API: provider created id={pid}")
    return {"ok": True, "id": pid}


@router.put("/providers/{provider_id}")
async def update_provider(provider_id: str, body: ProviderIn):
    cfg = _read_settings()
    providers = cfg.get("ai", {}).get("providers", [])
    for p in providers:
        if p["id"] == provider_id:
            p["name"] = body.name
            p["api_key"] = body.api_key
            p["base_url"] = body.base_url
            p["model"] = body.model or None
            _write_settings(cfg)
            logger.info(f"API: provider updated id={provider_id}")
            return {"ok": True}
    raise HTTPException(status_code=404, detail="Provider not found")


@router.delete("/providers/{provider_id}")
async def delete_provider(provider_id: str):
    cfg = _read_settings()
    ai = cfg.get("ai", {})
    providers = ai.get("providers", [])
    new_providers = [p for p in providers if p["id"] != provider_id]
    if len(new_providers) == len(providers):
        raise HTTPException(status_code=404, detail="Provider not found")
    ai["providers"] = new_providers
    if ai.get("provider_id") == provider_id:
        ai["provider_id"] = new_providers[0]["id"] if new_providers else None
    synthesis = ai.get("topic_synthesis", {})
    if synthesis.get("provider_id") == provider_id:
        synthesis["provider_id"] = None
    _write_settings(cfg)
    logger.info(f"API: provider deleted id={provider_id}")
    return {"ok": True}


@router.post("/providers/{provider_id}/test")
async def test_provider(provider_id: str):
    from ai.rewriter import test_ai_connection
    cfg = _read_settings()
    providers = cfg.get("ai", {}).get("providers", [])
    provider = next((p for p in providers if p["id"] == provider_id), None)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    # Use provider's own model if set, else fall back to global ai model
    model = provider.get("model") or cfg.get("ai", {}).get("model", "gpt-4o-mini")
    tone = cfg.get("ai", {}).get("tone", "general")
    result = await test_ai_connection(
        api_key=provider.get("api_key"),
        base_url=provider.get("base_url"),
        model=model,
        tone=tone,
    )
    return result


# ── AI Configs ───────────────────────────────────────────────────────────────


class AiConfigIn(BaseModel):
    name: str
    tone: str = "general"
    prompt_system: str = ""
    prompt_template: str = ""
    is_default: bool = False


@router.get("/ai-configs")
async def list_ai_configs():
    cfg = _read_settings()
    return {"configs": cfg.get("ai", {}).get("configs", [])}


@router.post("/ai-configs", status_code=201)
async def create_ai_config(body: AiConfigIn):
    import re
    cfg = _read_settings()
    ai = cfg.setdefault("ai", {})
    configs = ai.setdefault("configs", [])
    cid = re.sub(r"[^a-z0-9]+", "-", body.name.lower()).strip("-") or f"cfg-{len(configs)+1}"
    if any(c["id"] == cid for c in configs):
        cid = f"{cid}-{len(configs)+1}"
    if body.is_default:
        for c in configs:
            c["is_default"] = False
    tone = body.tone if body.tone in ("formal", "casual", "general") else "general"
    entry = {
        "id": cid,
        "name": body.name,
        "tone": tone,
        "prompt_system": body.prompt_system,
        "prompt_template": body.prompt_template,
        "is_default": body.is_default,
    }
    configs.append(entry)
    _write_settings(cfg)
    logger.info(f"API: AI config created id={cid}")
    return {"ok": True, "id": cid}


@router.put("/ai-configs/{config_id}")
async def update_ai_config(config_id: str, body: AiConfigIn):
    cfg = _read_settings()
    configs = cfg.get("ai", {}).get("configs", [])
    target = next((c for c in configs if c["id"] == config_id), None)
    if not target:
        raise HTTPException(404, "Config not found")
    if body.is_default:
        for c in configs:
            c["is_default"] = False
    tone = body.tone if body.tone in ("formal", "casual", "general") else "general"
    target["name"] = body.name
    target["tone"] = tone
    target["prompt_system"] = body.prompt_system
    target["prompt_template"] = body.prompt_template
    target["is_default"] = body.is_default
    _write_settings(cfg)
    logger.info(f"API: AI config updated id={config_id}")
    return {"ok": True}


@router.post("/ai-configs/{config_id}/set-default")
async def set_default_ai_config(config_id: str):
    cfg = _read_settings()
    configs = cfg.get("ai", {}).get("configs", [])
    found = False
    for c in configs:
        if c["id"] == config_id:
            c["is_default"] = True
            found = True
        else:
            c["is_default"] = False
    if not found:
        raise HTTPException(404, "Config not found")
    _write_settings(cfg)
    return {"ok": True}


@router.delete("/ai-configs/{config_id}")
async def delete_ai_config(config_id: str):
    cfg = _read_settings()
    ai = cfg.get("ai", {})
    configs = ai.get("configs", [])
    new_configs = [c for c in configs if c["id"] != config_id]
    if len(new_configs) == len(configs):
        raise HTTPException(404, "Config not found")
    ai["configs"] = new_configs
    _write_settings(cfg)
    logger.info(f"API: AI config deleted id={config_id}")
    return {"ok": True}


# ── AI Logs ──────────────────────────────────────────────────────────────────


@router.get("/logs/ai")
async def list_ai_logs(page: int = 1, limit: int = LOG_PAGE_SIZE):
    offset = (page - 1) * limit
    logs, total = await get_recent_ai_logs(limit=limit, offset=offset)
    return {
        "logs": logs,
        "total": total,
        "page": page,
        "total_pages": max(1, math.ceil(total / limit)),
    }


# ── Webhooks ─────────────────────────────────────────────────────────────────


class WebhookIn(BaseModel):
    id: str
    name: str
    url: str
    http_method: str = "POST"
    content_type: str = "application/json"
    retry_attempts: int = 3
    retry_delay_seconds: int = 5
    timeout_seconds: int = 10
    payload_mode: str = "full"
    payload_fields: list[str] = []
    payload_template: str = ""
    filter_categories_mode: str = "all"
    filter_categories: list[str] = []
    filter_sources_mode: str = "all"
    filter_sources: list[str] = []
    filter_article_types_mode: str = "all"
    filter_article_types: list[str] = []
    rate_limit_max: int = 0
    rate_limit_window_minutes: int = 60


class WebhookUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    http_method: str | None = None
    content_type: str | None = None
    retry_attempts: int | None = None
    retry_delay_seconds: int | None = None
    timeout_seconds: int | None = None
    payload_mode: str | None = None
    payload_fields: list[str] | None = None
    payload_template: str | None = None
    filter_categories_mode: str | None = None
    filter_categories: list[str] | None = None
    filter_sources_mode: str | None = None
    filter_sources: list[str] | None = None
    filter_article_types_mode: str | None = None
    filter_article_types: list[str] | None = None
    rate_limit_max: int | None = None
    rate_limit_window_minutes: int | None = None


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
        "id": body.id,
        "name": body.name,
        "url": body.url,
        "enabled": True,
        "http_method": body.http_method.upper() if body.http_method else "POST",
        "content_type": body.content_type or "application/json",
        "retry_attempts": max(1, min(body.retry_attempts, 10)),
        "retry_delay_seconds": max(1, min(body.retry_delay_seconds, 60)),
        "timeout_seconds": max(1, min(body.timeout_seconds, 60)),
        "payload_mode": body.payload_mode,
        "payload_fields": body.payload_fields,
        "payload_template": body.payload_template,
        "filter_categories_mode": body.filter_categories_mode,
        "filter_categories": body.filter_categories,
        "filter_sources_mode": body.filter_sources_mode,
        "filter_sources": body.filter_sources,
        "filter_article_types_mode": body.filter_article_types_mode,
        "filter_article_types": body.filter_article_types,
        "rate_limit_max": max(0, body.rate_limit_max),
        "rate_limit_window_minutes": max(1, body.rate_limit_window_minutes),
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
    for field in (
        "name",
        "url",
        "http_method",
        "content_type",
        "retry_attempts",
        "retry_delay_seconds",
        "timeout_seconds",
        "payload_mode",
        "payload_fields",
        "payload_template",
        "filter_categories_mode",
        "filter_categories",
        "filter_sources_mode",
        "filter_sources",
        "filter_article_types_mode",
        "filter_article_types",
        "rate_limit_max",
        "rate_limit_window_minutes",
    ):
        val = getattr(body, field, None)
        if val is not None:
            if field == "http_method":
                target[field] = val.upper()
            else:
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


@router.post("/webhooks/{wh_id}/test")
async def test_webhook(wh_id: str):
    """Test webhook by sending real HTTP request with mock data and return detailed result."""
    endpoints = _get_webhook_endpoints()
    target = next((ep for ep in endpoints if ep["id"] == wh_id), None)
    if not target:
        raise HTTPException(404, "Webhook not found")

    import time
    import httpx
    from webhook.payload import build_payload
    from webhook.filters import passes_filter

    # Create mock article data with all available fields from real news articles
    mock_article = {
        "id": "test_" + str(int(time.time())),
        "source_id": "bbc-world",
        "source_name": "BBC World News",
        "url": "https://www.bbc.com/news/world-test-article-12345",
        "title": "Global Leaders Meet to Discuss Climate Change Action Plan - Breaking News Update",
        "summary": "World leaders gathered today at the International Summit to discuss comprehensive measures addressing climate change impacts and sustainable development goals.",
        "content": "In a historic gathering, representatives from over 150 countries convened to address the pressing challenges of climate change. The summit featured keynote speeches from prominent environmental scientists and policy makers, emphasizing the urgent need for coordinated global action. Key topics included renewable energy adoption, carbon emission reduction targets, and financial support for developing nations.",
        "lang": "en",
        "declared_lang": "en",
        "category": "world",
        "published_at": "2026-03-24T10:30:00+00:00",
        "fetched_at": "2026-03-24T10:35:00+00:00",
        "ai_summary_vi": "Các nhà lãnh đạo thế giới đã họp tại Hội nghị Quốc tế để thảo luận về các biện pháp toàn diện nhằm giải quyết tác động của biến đổi khí hậu và các mục tiêu phát triển bền vững. Hội nghị thượng đỉnh có các bài phát biểu quan trọng từ các nhà khoa học môi trường và nhà hoạch định chính sách nổi tiếng.",
        "ai_summary_en": "World leaders convened at the International Summit to discuss comprehensive measures addressing climate change impacts and sustainable development goals. The summit featured keynote speeches from prominent environmental scientists and policy makers, emphasizing urgent global action.",
        "ai_status": "completed",
    }

    url = target.get("url", "")
    if not url:
        raise HTTPException(400, "Webhook URL is empty")

    # Check if article passes filters
    if not passes_filter(mock_article, target):
        return {
            "ok": False,
            "message": f"✗ Test article filtered out by webhook rules (category or source filter)",
            "method": target.get("http_method", "POST"),
            "url": url,
            "payload_mode": target.get("payload_mode", "full"),
            "filter_categories_mode": target.get("filter_categories_mode"),
            "filter_categories": target.get("filter_categories", []),
            "filter_sources_mode": target.get("filter_sources_mode"),
            "filter_sources": target.get("filter_sources", []),
            "article_category": mock_article.get("category"),
            "article_source": mock_article.get("source_id"),
        }

    # Build payload
    payload = build_payload(mock_article, target)
    method = target.get("http_method", "POST").upper()
    content_type = target.get("content_type", "application/json")
    timeout = target.get("timeout_seconds", 10)

    # Log what we're about to send
    logger.info(f"TEST webhook {wh_id}: {method} {url}")
    logger.info(
        f"TEST payload_mode: {target.get('payload_mode')}, content_type: {content_type}"
    )
    logger.info(f"TEST payload preview: {str(payload)[:200]}...")

    # Send real HTTP request and wait for response
    start_time = time.time()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            if method == "GET":
                if isinstance(payload, dict):
                    params = {k: str(v) for k, v in payload.items()}
                else:
                    params = {"message": str(payload)}
                resp = await client.get(url, params=params)
            else:  # POST
                if isinstance(payload, str):
                    resp = await client.post(
                        url,
                        content=payload,
                        headers={"Content-Type": content_type},
                    )
                else:
                    resp = await client.post(url, json=payload)

        elapsed_ms = int((time.time() - start_time) * 1000)
        success = resp.status_code < 400

        # Get response body (limit to 500 chars)
        try:
            response_body = resp.text[:500]
        except:
            response_body = "(binary data)"

        logger.info(f"TEST webhook response: {resp.status_code} in {elapsed_ms}ms")

        return {
            "ok": success,
            "elapsed_ms": elapsed_ms,
            "message": f"{'✓' if success else '✗'} Test completed in {elapsed_ms}ms",
            "method": method,
            "url": url,
            "content_type": content_type,
            "payload_mode": target.get("payload_mode", "full"),
            "payload": payload
            if isinstance(payload, str)
            else json.dumps(payload, ensure_ascii=False)[:500],
            "status_code": resp.status_code,
            "response_body": response_body,
        }

    except Exception as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        logger.error(f"TEST webhook error: {str(e)}")
        return {
            "ok": False,
            "elapsed_ms": elapsed_ms,
            "message": f"✗ Test failed: {str(e)}",
            "method": method,
            "url": url,
            "content_type": content_type,
            "payload_mode": target.get("payload_mode", "full"),
            "payload": payload
            if isinstance(payload, str)
            else json.dumps(payload, ensure_ascii=False)[:500],
            "error": str(e),
        }


# ── Webhook Logs ─────────────────────────────────────────────────────────────


@router.get("/logs/webhooks")
async def list_webhook_logs(page: int = 1, limit: int = LOG_PAGE_SIZE):
    offset = (page - 1) * limit
    logs, total = await get_recent_webhook_logs(limit=limit, offset=offset)
    return {
        "logs": logs,
        "total": total,
        "page": page,
        "total_pages": max(1, math.ceil(total / limit)),
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
        limit=limit,
        offset=offset,
        source_id=source,
        domain=domain,
        errors_only=errors_only,
        http_status=http_status,
        since=since,
    )
    return {
        "logs": logs,
        "total": total,
        "page": page,
        "total_pages": max(1, math.ceil(total / limit)),
    }


@router.get("/logs/crawl/sources")
async def crawl_source_summary(since: str | None = None):
    """Per-source success rate, avg duration, error counts."""
    rows = await get_crawl_source_summary(since=since)
    return {"sources": rows, "count": len(rows)}


@router.get("/logs/crawl/sources/{source_id}")
async def crawl_source_detail(
    source_id: str,
    page: int = 1,
    limit: int = LOG_PAGE_SIZE,
):
    """All crawl logs for a specific source."""
    offset = (page - 1) * limit
    logs, total = await get_crawl_logs(limit=limit, offset=offset, source_id=source_id)
    return {
        "source_id": source_id,
        "logs": logs,
        "total": total,
        "page": page,
        "total_pages": max(1, math.ceil(total / limit)),
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
    filter_categories_mode: str = "all"
    filter_categories: list[str] = []
    filter_sources_mode: str = "all"
    filter_sources: list[str] = []
    filter_article_types_mode: str = "all"
    filter_article_types: list[str] = []
    rate_limit_max: int = 0
    rate_limit_window_minutes: int = 60


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
    filter_categories_mode: str | None = None
    filter_categories: list[str] | None = None
    filter_sources_mode: str | None = None
    filter_sources: list[str] | None = None
    filter_article_types_mode: str | None = None
    filter_article_types: list[str] | None = None
    rate_limit_max: int | None = None
    rate_limit_window_minutes: int | None = None


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
        "id": body.id,
        "name": body.name,
        "bot_token": body.bot_token,
        "chat_id": body.chat_id,
        "lang": body.lang,
        "enabled": True,
        "retry_attempts": max(1, min(body.retry_attempts, 10)),
        "timeout_seconds": max(1, min(body.timeout_seconds, 60)),
        "payload_mode": body.payload_mode,
        "payload_fields": body.payload_fields,
        "payload_template": body.payload_template,
        "filter_categories_mode": body.filter_categories_mode,
        "filter_categories": body.filter_categories,
        "filter_sources_mode": body.filter_sources_mode,
        "filter_sources": body.filter_sources,
        "filter_article_types_mode": body.filter_article_types_mode,
        "filter_article_types": body.filter_article_types,
        "rate_limit_max": max(0, body.rate_limit_max),
        "rate_limit_window_minutes": max(1, body.rate_limit_window_minutes),
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
    for field in (
        "name",
        "bot_token",
        "chat_id",
        "lang",
        "retry_attempts",
        "timeout_seconds",
        "payload_mode",
        "payload_fields",
        "payload_template",
        "filter_categories_mode",
        "filter_categories",
        "filter_sources_mode",
        "filter_sources",
        "filter_article_types_mode",
        "filter_article_types",
        "rate_limit_max",
        "rate_limit_window_minutes",
    ):
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
        target["bot_token"],
        target["chat_id"],
        text,
        timeout=target.get("timeout_seconds", 10),
    )
    if ok:
        return {"ok": True, "message": "Test message sent successfully"}
    raise HTTPException(400, f"Telegram API error: {error}")


# ── System Logs ──────────────────────────────────────────────────────────────


@router.get("/logs/system")
async def list_system_logs(
    page: int = 1,
    limit: int = LOG_PAGE_SIZE,
    event_type: str | None = None,
    status: str | None = None,
    since: str | None = None,
):
    """
    Browse scheduler and system event logs.

    Query params:
    - event_type  — filter by type: crawl_job, ai_job
    - status      — filter by status: ok, error, skipped
    - since       — ISO datetime lower bound
    """
    offset = (page - 1) * limit
    logs, total = await get_system_logs(
        limit=limit,
        offset=offset,
        event_type=event_type,
        status=status,
        since=since,
    )
    return {
        "logs": logs,
        "total": total,
        "page": page,
        "total_pages": max(1, math.ceil(total / limit)),
    }


@router.get("/logs/system/summary")
async def system_log_summary(since: str | None = None):
    """Per event_type stats: total runs, success/error counts, avg duration."""
    rows = await get_system_summary(since=since)
    return {"summary": rows, "count": len(rows)}


# ── API Request Logs ──────────────────────────────────────────────────────────


@router.get("/logs/api")
async def list_api_logs(
    page: int = 1,
    limit: int = LOG_PAGE_SIZE,
    method: str | None = None,
    path: str | None = None,
    status_code: int | None = None,
    errors_only: bool = False,
    since: str | None = None,
):
    """
    Browse API request logs.

    Query params:
    - method       — HTTP method (GET, POST, PUT, DELETE)
    - path         — partial path match (e.g. /api/sources)
    - status_code  — exact HTTP status code
    - errors_only  — only 4xx/5xx responses (bool)
    - since        — ISO datetime lower bound
    """
    offset = (page - 1) * limit
    logs, total = await get_api_logs(
        limit=limit,
        offset=offset,
        method=method,
        path=path,
        status_code=status_code,
        errors_only=errors_only,
        since=since,
    )
    return {
        "logs": logs,
        "total": total,
        "page": page,
        "total_pages": max(1, math.ceil(total / limit)),
    }


@router.get("/logs/api/summary")
async def api_log_summary(since: str | None = None):
    """Per-endpoint stats: total requests, avg latency, error rate."""
    rows = await get_api_summary(since=since)
    return {"endpoints": rows, "count": len(rows)}


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


@router.get("/filter-options")
async def get_filter_options(q: str = ""):
    """Return available categories and source IDs for filter hints. Optionally search with ?q=."""
    cfg = _read_settings()
    categories = [
        {"id": c["id"], "name": c.get("name", c["id"])}
        for c in cfg.get("categories", [])
        if c.get("enabled", True)
    ]
    with open(SOURCES_PATH) as f:
        raw_sources = yaml.safe_load(f).get("sources", [])
    sources = [
        {
            "id": s["id"],
            "name": s.get("name", s["id"]),
            "category": s.get("category", ""),
            "lang": s.get("lang", ""),
        }
        for s in raw_sources
        if s.get("enabled", True)
    ]
    if q:
        q_low = q.lower()
        categories = [
            c for c in categories if q_low in c["id"] or q_low in c["name"].lower()
        ]
        sources = [
            s
            for s in sources
            if q_low in s["id"]
            or q_low in s["name"].lower()
            or q_low in s.get("category", "")
        ]
    return {"categories": categories, "sources": sources}


@router.get("/payload/fields")
async def list_payload_fields():
    """List all available fields for payload_mode=fields."""
    from webhook.payload import ALL_FIELDS

    return {"fields": ALL_FIELDS}


@router.get(
    "/SKILL.md", response_class=__import__("fastapi").responses.PlainTextResponse
)
async def serve_skill_md(request: __import__("fastapi").Request):
    """Serve SKILL.md with $API and localhost URL replaced by the actual request base URL."""
    import pathlib

    skill_path = pathlib.Path(_BASE_DIR) / "SKILL.md"
    content = skill_path.read_text(encoding="utf-8")

    # Build actual API base URL from the incoming request
    base_url = str(request.base_url).rstrip("/")
    actual_api = f"{base_url}/api"

    content = content.replace("http://localhost:8000/api", actual_api)
    content = content.replace("$API", actual_api)
    return content


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

"""JSON API — Stats, Logs, Payload utilities, and Filter options."""

import logging
import math
import pathlib

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel

from dashboard.config_io import read_settings, read_sources
from dashboard.redis_state import get_redis
from storage.redis_store import get_article, get_feed_stats, get_latest_articles, get_pending_ai_articles
from storage.redis_keys import DEDUP_SIMHASHES_KEY
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
    get_recent_channel_logs,
    get_recent_webhook_logs,
    get_system_logs,
    get_system_summary,
)

logger = logging.getLogger(__name__)
router = APIRouter()

LOG_PAGE_SIZE = 20


# ── Scheduler status ──────────────────────────────────────────────────────────


@router.get("/scheduler/status")
async def scheduler_status():
    from jobs.scheduler import get_scheduler_status
    return {"jobs": get_scheduler_status()}


# ── Health ────────────────────────────────────────────────────────────────────


@router.get("/health")
async def health():
    r = get_redis()
    try:
        await r.ping()
        redis_ok = True
    except Exception:
        redis_ok = False
    return {"status": "ok" if redis_ok else "degraded", "redis": redis_ok}


# ── Articles ──────────────────────────────────────────────────────────────────


def _format_article(a: dict) -> dict:
    return {**a, "published_at": a.get("published_at", ""), "fetched_at": a.get("fetched_at", "")}


@router.get("/news")
async def list_news(
    page: int = 1,
    limit: int = 20,
    source: str | None = None,
    category: str | None = None,
    lang: str | None = None,
    ai_status: str | None = None,
    article_type: str | None = None,
):
    limit = max(1, min(limit, 100))
    redis = get_redis()

    if not (lang or ai_status):
        # No in-memory filters: use Redis-native offset/limit directly.
        # total count is accurate from ZCARD.
        offset = (page - 1) * limit
        articles, total = await get_latest_articles(
            redis,
            limit=limit,
            offset=offset,
            source_id=source or None,
            category=category or None,
            article_type=article_type or None,
        )
    else:
        # lang/ai_status are not indexed in Redis — must fetch a larger window
        # and filter in Python.  Fetch up to 500 recent articles to get an
        # accurate filtered total, then slice the requested page.
        # 500 covers ~10h of articles at current crawl rate (~50/h).
        FILTER_SCAN_LIMIT = 500
        articles_all, _ = await get_latest_articles(
            redis,
            limit=FILTER_SCAN_LIMIT,
            offset=0,
            source_id=source or None,
            category=category or None,
            article_type=article_type or None,
        )
        if lang:
            articles_all = [a for a in articles_all if a.get("lang") == lang]
        if ai_status:
            articles_all = [a for a in articles_all if a.get("ai_status") == ai_status]

        total = len(articles_all)
        slice_start = (page - 1) * limit
        articles = articles_all[slice_start : slice_start + limit]

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
        "filters": {"source": source, "category": category, "lang": lang, "ai_status": ai_status, "article_type": article_type},
    }


@router.get("/articles")
async def list_articles(limit: int = 50, offset: int = 0, source: str = None, category: str = None):
    """Legacy endpoint — prefer /api/news for new integrations."""
    articles, total = await get_latest_articles(
        get_redis(), limit=min(limit, 200), offset=offset,
        source_id=source or None, category=category or None,
    )
    return {"articles": articles, "count": len(articles), "total": total}


@router.get("/articles/pending/list")
async def list_pending_articles(limit: int = 20):
    articles = await get_pending_ai_articles(get_redis(), limit=limit)
    return {"articles": articles, "count": len(articles)}


@router.get("/articles/{article_id}")
async def get_article_detail(article_id: str):
    article = await get_article(get_redis(), article_id)
    if not article:
        raise HTTPException(404, "Article not found")
    return article


# ── Stats ─────────────────────────────────────────────────────────────────────


@router.get("/stats")
async def stats():
    return {"redis": await get_feed_stats(get_redis()), "db": await get_dashboard_stats()}


@router.get("/stats/dedup")
async def dedup_stats():
    count = await get_redis().scard(DEDUP_SIMHASHES_KEY)
    return {"simhash_count": count}


# ── Crawl Logs ────────────────────────────────────────────────────────────────


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
    offset = (page - 1) * limit
    logs, total = await get_crawl_logs(
        limit=limit, offset=offset, source_id=source, domain=domain,
        errors_only=errors_only, http_status=http_status, since=since,
    )
    return {"logs": logs, "total": total, "page": page, "total_pages": max(1, math.ceil(total / limit))}


@router.get("/logs/crawl/sources")
async def crawl_source_summary(since: str | None = None):
    rows = await get_crawl_source_summary(since=since)
    return {"sources": rows, "count": len(rows)}


@router.get("/logs/crawl/sources/{source_id}")
async def crawl_source_detail(source_id: str, page: int = 1, limit: int = LOG_PAGE_SIZE):
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
    rows = await get_crawl_domain_summary(since=since)
    return {"domains": rows, "count": len(rows)}


@router.get("/logs/crawl/errors")
async def crawl_error_breakdown(since: str | None = None):
    rows = await get_crawl_error_breakdown(since=since)
    return {"errors": rows, "count": len(rows)}


@router.get("/logs/crawl/timeline")
async def crawl_timeline(hours: int = 24):
    rows = await get_crawl_timeline(hours=min(hours, 168))
    return {"timeline": rows, "hours": hours}


# ── AI Logs ───────────────────────────────────────────────────────────────────


@router.get("/logs/ai")
async def list_ai_logs(page: int = 1, limit: int = LOG_PAGE_SIZE):
    offset = (page - 1) * limit
    logs, total = await get_recent_ai_logs(limit=limit, offset=offset)
    return {"logs": logs, "total": total, "page": page, "total_pages": max(1, math.ceil(total / limit))}


# ── Webhook Logs ──────────────────────────────────────────────────────────────


@router.get("/logs/webhooks")
async def list_webhook_logs(page: int = 1, limit: int = LOG_PAGE_SIZE):
    offset = (page - 1) * limit
    logs, total = await get_recent_webhook_logs(limit=limit, offset=offset)
    return {"logs": logs, "total": total, "page": page, "total_pages": max(1, math.ceil(total / limit))}


@router.get("/logs/channels")
async def list_channel_logs(page: int = 1, limit: int = LOG_PAGE_SIZE):
    offset = (page - 1) * limit
    logs, total = await get_recent_channel_logs(limit=limit, offset=offset)
    return {"logs": logs, "total": total, "page": page, "total_pages": max(1, math.ceil(total / limit))}


# ── System Logs ───────────────────────────────────────────────────────────────


@router.get("/logs/system")
async def list_system_logs(
    page: int = 1,
    limit: int = LOG_PAGE_SIZE,
    event_type: str | None = None,
    status: str | None = None,
    since: str | None = None,
):
    offset = (page - 1) * limit
    logs, total = await get_system_logs(limit=limit, offset=offset, event_type=event_type, status=status, since=since)
    return {"logs": logs, "total": total, "page": page, "total_pages": max(1, math.ceil(total / limit))}


@router.get("/logs/system/summary")
async def system_log_summary(since: str | None = None):
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
    offset = (page - 1) * limit
    logs, total = await get_api_logs(
        limit=limit, offset=offset, method=method, path=path,
        status_code=status_code, errors_only=errors_only, since=since,
    )
    return {"logs": logs, "total": total, "page": page, "total_pages": max(1, math.ceil(total / limit))}


@router.get("/logs/api/summary")
async def api_log_summary(since: str | None = None):
    rows = await get_api_summary(since=since)
    return {"endpoints": rows, "count": len(rows)}


# ── Payload Template Utilities ────────────────────────────────────────────────


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


class TemplateValidateIn(BaseModel):
    template: str


@router.post("/payload/validate")
async def validate_payload_template(body: TemplateValidateIn):
    from webhook.payload import validate_template, render_template

    ok, error = validate_template(body.template)
    if not ok:
        return {"ok": False, "error": error}
    preview = render_template(body.template, SAMPLE_ARTICLE)
    return {"ok": True, "preview": preview}


@router.post("/payload/preview")
async def preview_payload(body: dict = Body(...)):
    from webhook.payload import build_payload

    config = {
        "payload_mode": body.get("payload_mode", "full"),
        "payload_fields": body.get("payload_fields", []),
        "payload_template": body.get("payload_template", ""),
    }
    result = build_payload(SAMPLE_ARTICLE, config)
    return {"ok": True, "payload": result}


@router.get("/payload/fields")
async def list_payload_fields():
    from webhook.payload import ALL_FIELDS
    return {"fields": ALL_FIELDS}


# ── Filter options ────────────────────────────────────────────────────────────


@router.get("/filter-options")
async def get_filter_options(q: str = ""):
    cfg = read_settings()
    categories = [
        {"id": c["id"], "name": c.get("name", c["id"])}
        for c in cfg.get("categories", [])
        if c.get("enabled", True)
    ]
    raw_sources = read_sources()
    sources = [
        {"id": s["id"], "name": s.get("name", s["id"]), "category": s.get("category", ""), "lang": s.get("lang", "")}
        for s in raw_sources
        if s.get("enabled", True)
    ]
    if q:
        q_low = q.lower()
        categories = [c for c in categories if q_low in c["id"] or q_low in c["name"].lower()]
        sources = [s for s in sources if q_low in s["id"] or q_low in s["name"].lower() or q_low in s.get("category", "")]
    return {"categories": categories, "sources": sources}


# ── SKILL.md serving ──────────────────────────────────────────────────────────


@router.get("/SKILL.md", response_class=__import__("fastapi").responses.PlainTextResponse)
async def serve_skill_md(request: __import__("fastapi").Request):
    import os
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    skill_path = pathlib.Path(base_dir) / "SKILL.md"
    content = skill_path.read_text(encoding="utf-8")
    base_url = str(request.base_url).rstrip("/")
    actual_api = f"{base_url}/api"
    content = content.replace("http://localhost:8000/api", actual_api)
    content = content.replace("$API", actual_api)
    return content

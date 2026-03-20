"""
FastAPI dashboard với htmx real-time updates + source/category management.
"""
import logging
import math
import os
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
import yaml
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from storage.redis_store import get_latest_articles, get_feed_stats, get_article
from storage.sqlite_stats import get_dashboard_stats, get_recent_webhook_logs, get_recent_ai_logs, init_db

logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

_BASE_DIR = os.path.dirname(os.path.dirname(__file__))
SOURCES_PATH = os.path.join(_BASE_DIR, "config", "sources.yaml")
SETTINGS_PATH = os.path.join(_BASE_DIR, "config", "settings.yaml")

_redis: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            encoding="utf-8",
            decode_responses=False,
        )
    return _redis


def _read_sources() -> list[dict]:
    with open(SOURCES_PATH) as f:
        data = yaml.safe_load(f)
    return data.get("sources", [])


def _write_sources(sources: list[dict]) -> None:
    with open(SOURCES_PATH, "w") as f:
        yaml.dump({"sources": sources}, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


def _read_settings() -> dict:
    with open(SETTINGS_PATH) as f:
        return yaml.safe_load(f)


def _write_settings(cfg: dict) -> None:
    with open(SETTINGS_PATH, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


def _get_categories() -> list[dict]:
    return _read_settings().get("categories", [])


def _get_active_category_ids() -> set[str]:
    return {c["id"] for c in _get_categories() if c.get("enabled", True)}


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield
    if _redis:
        await _redis.aclose()


app = FastAPI(title="News Aggregator Dashboard", lifespan=lifespan)

PAGE_SIZE = 20


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/partials/stats", response_class=HTMLResponse)
async def stats_partial(request: Request):
    redis = get_redis()
    redis_stats = await get_feed_stats(redis)
    db_stats = await get_dashboard_stats()
    return templates.TemplateResponse("partials/stats.html", {
        "request": request,
        "redis": redis_stats,
        "db": db_stats,
    })


@app.get("/partials/feed", response_class=HTMLResponse)
async def feed_partial(request: Request, page: int = 1, source: str = None, category: str = None):
    redis = get_redis()
    offset = (page - 1) * PAGE_SIZE
    articles, total = await get_latest_articles(
        redis, limit=PAGE_SIZE, offset=offset,
        source_id=source or None, category=category or None,
    )
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    sources = _read_sources()
    categories = _get_categories()
    return templates.TemplateResponse("partials/feed.html", {
        "request": request,
        "articles": articles,
        "page": page,
        "total_pages": total_pages,
        "total": total,
        "source": source or "",
        "category": category or "",
        "sources": sources,
        "categories": categories,
    })


# --- Source management ---

@app.get("/sources", response_class=HTMLResponse)
async def sources_page(request: Request):
    return templates.TemplateResponse("sources.html", {"request": request})


@app.get("/partials/sources", response_class=HTMLResponse)
async def sources_partial(request: Request):
    sources = _read_sources()
    return templates.TemplateResponse("partials/sources.html", {
        "request": request,
        "sources": sources,
    })


@app.post("/sources/add", response_class=HTMLResponse)
async def source_add(
    request: Request,
    id: str = Form(...),
    name: str = Form(...),
    url: str = Form(...),
    lang: str = Form("en"),
    category: str = Form("world"),
):
    sources = _read_sources()
    if any(s["id"] == id for s in sources):
        sources = _read_sources()
        return templates.TemplateResponse("partials/sources.html", {
            "request": request,
            "sources": sources,
            "error": f"Source '{id}' already exists",
        })
    sources.append({
        "id": id, "name": name, "url": url,
        "type": "rss", "lang": lang, "category": category, "enabled": True,
    })
    _write_sources(sources)
    logger.info(f"Source added: {id}")
    return templates.TemplateResponse("partials/sources.html", {
        "request": request,
        "sources": sources,
        "success": f"Source '{name}' added",
    })


@app.post("/sources/{source_id}/toggle", response_class=HTMLResponse)
async def source_toggle(request: Request, source_id: str):
    sources = _read_sources()
    for s in sources:
        if s["id"] == source_id:
            s["enabled"] = not s.get("enabled", True)
            break
    _write_sources(sources)
    return templates.TemplateResponse("partials/sources.html", {
        "request": request,
        "sources": sources,
    })


@app.delete("/sources/{source_id}", response_class=HTMLResponse)
async def source_delete(request: Request, source_id: str):
    sources = _read_sources()
    sources = [s for s in sources if s["id"] != source_id]
    _write_sources(sources)
    logger.info(f"Source deleted: {source_id}")
    return templates.TemplateResponse("partials/sources.html", {
        "request": request,
        "sources": sources,
        "success": f"Source '{source_id}' deleted",
    })


# --- Category management ---

@app.get("/partials/categories", response_class=HTMLResponse)
async def categories_partial(request: Request):
    categories = _get_categories()
    return templates.TemplateResponse("partials/categories.html", {
        "request": request,
        "categories": categories,
    })


@app.post("/categories/add", response_class=HTMLResponse)
async def category_add(
    request: Request,
    id: str = Form(...),
    name: str = Form(...),
):
    cfg = _read_settings()
    cats = cfg.get("categories", [])
    if any(c["id"] == id for c in cats):
        return templates.TemplateResponse("partials/categories.html", {
            "request": request,
            "categories": cats,
            "error": f"Category '{id}' already exists",
        })
    cats.append({"id": id, "name": name, "enabled": True})
    cfg["categories"] = cats
    _write_settings(cfg)
    return templates.TemplateResponse("partials/categories.html", {
        "request": request,
        "categories": cats,
        "success": f"Category '{name}' added",
    })


@app.post("/categories/{cat_id}/toggle", response_class=HTMLResponse)
async def category_toggle(request: Request, cat_id: str):
    cfg = _read_settings()
    cats = cfg.get("categories", [])
    for c in cats:
        if c["id"] == cat_id:
            c["enabled"] = not c.get("enabled", True)
            break
    cfg["categories"] = cats
    _write_settings(cfg)
    return templates.TemplateResponse("partials/categories.html", {
        "request": request,
        "categories": cats,
    })


@app.delete("/categories/{cat_id}", response_class=HTMLResponse)
async def category_delete(request: Request, cat_id: str):
    cfg = _read_settings()
    cats = [c for c in cfg.get("categories", []) if c["id"] != cat_id]
    cfg["categories"] = cats
    _write_settings(cfg)
    return templates.TemplateResponse("partials/categories.html", {
        "request": request,
        "categories": cats,
        "success": f"Category '{cat_id}' deleted",
    })


@app.get("/partials/category-options", response_class=HTMLResponse)
async def category_options():
    cats = _get_categories()
    html = "".join(f'<option value="{c["id"]}">{c["name"]}</option>' for c in cats)
    return HTMLResponse(html)


@app.get("/sources/{source_id}/edit", response_class=HTMLResponse)
async def source_edit_form(request: Request, source_id: str):
    sources = _read_sources()
    source = next((s for s in sources if s["id"] == source_id), None)
    if not source:
        return HTMLResponse("<tr><td colspan='6'>Source not found</td></tr>")
    return templates.TemplateResponse("partials/source_edit_row.html", {
        "request": request,
        "s": source,
    })


@app.put("/sources/{source_id}", response_class=HTMLResponse)
async def source_update(
    request: Request,
    source_id: str,
    name: str = Form(...),
    url: str = Form(...),
    lang: str = Form("en"),
    category: str = Form("world"),
):
    sources = _read_sources()
    for s in sources:
        if s["id"] == source_id:
            s["name"] = name
            s["url"] = url
            s["lang"] = lang
            s["category"] = category
            break
    _write_sources(sources)
    logger.info(f"Source updated: {source_id}")
    return templates.TemplateResponse("partials/sources.html", {
        "request": request,
        "sources": sources,
        "success": f"Source '{name}' updated",
    })


# --- Settings page ---

async def _enrich_logs(logs: list[dict]) -> list[dict]:
    """Attach article title to each log entry from Redis."""
    redis = get_redis()
    for log in logs:
        article = await get_article(redis, log["article_id"])
        log["article_title"] = article.get("title", "—") if article else "—"
    return logs


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return templates.TemplateResponse("settings.html", {"request": request})


@app.get("/partials/settings-ai", response_class=HTMLResponse)
async def settings_ai_partial(request: Request):
    cfg = _read_settings()
    logs = await _enrich_logs(await get_recent_ai_logs(limit=30))
    return templates.TemplateResponse("partials/settings_ai.html", {
        "request": request,
        "ai": cfg.get("ai", {}),
        "logs": logs,
    })


@app.post("/settings/ai", response_class=HTMLResponse)
async def settings_ai_update(
    request: Request,
    enabled: str = Form("off"),
    model: str = Form("gpt-4o-mini"),
    batch_size: int = Form(5),
    max_tokens: int = Form(300),
    retry_attempts: int = Form(3),
    output_languages: str = Form("vi,en"),
):
    cfg = _read_settings()
    cfg["ai"] = {
        "enabled": enabled == "on",
        "model": model.strip(),
        "batch_size": max(1, min(batch_size, 20)),
        "max_tokens_summary": max(100, min(max_tokens, 1000)),
        "retry_attempts": max(1, min(retry_attempts, 10)),
        "output_languages": [l.strip() for l in output_languages.split(",") if l.strip()],
    }
    _write_settings(cfg)
    logger.info(f"AI settings updated: model={cfg['ai']['model']}, enabled={cfg['ai']['enabled']}")
    logs = await _enrich_logs(await get_recent_ai_logs(limit=30))
    return templates.TemplateResponse("partials/settings_ai.html", {
        "request": request,
        "ai": cfg["ai"],
        "logs": logs,
        "success": "AI settings saved",
    })


@app.get("/partials/settings-webhook", response_class=HTMLResponse)
async def settings_webhook_partial(request: Request):
    cfg = _read_settings()
    logs = await _enrich_logs(await get_recent_webhook_logs(limit=30))
    return templates.TemplateResponse("partials/settings_webhook.html", {
        "request": request,
        "webhook": cfg.get("webhook", {}),
        "logs": logs,
    })


@app.post("/settings/webhook", response_class=HTMLResponse)
async def settings_webhook_update(
    request: Request,
    enabled: str = Form("off"),
    urls: str = Form(""),
    retry_attempts: int = Form(3),
    retry_delay: int = Form(5),
    timeout: int = Form(10),
):
    cfg = _read_settings()
    parsed_urls = [u.strip() for u in urls.strip().splitlines() if u.strip()]
    cfg["webhook"] = {
        "enabled": enabled == "on",
        "urls": parsed_urls,
        "retry_attempts": max(1, min(retry_attempts, 10)),
        "retry_delay_seconds": max(1, min(retry_delay, 60)),
        "timeout_seconds": max(1, min(timeout, 60)),
    }
    _write_settings(cfg)
    logger.info(f"Webhook settings updated: {len(parsed_urls)} URL(s), enabled={cfg['webhook']['enabled']}")
    logs = await _enrich_logs(await get_recent_webhook_logs(limit=30))
    return templates.TemplateResponse("partials/settings_webhook.html", {
        "request": request,
        "webhook": cfg["webhook"],
        "logs": logs,
        "success": "Webhook settings saved",
    })


# --- API ---

@app.get("/api/articles")
async def api_articles(limit: int = 50, offset: int = 0, source: str = None, category: str = None):
    redis = get_redis()
    articles, total = await get_latest_articles(
        redis, limit=limit, offset=offset,
        source_id=source or None, category=category or None,
    )
    return {"articles": articles, "count": len(articles), "total": total}


@app.get("/api/stats")
async def api_stats():
    redis = get_redis()
    redis_stats = await get_feed_stats(redis)
    db_stats = await get_dashboard_stats()
    return {"redis": redis_stats, "db": db_stats}


@app.get("/api/sources")
async def api_sources():
    return {"sources": _read_sources()}


@app.get("/api/categories")
async def api_categories():
    return {"categories": _get_categories()}

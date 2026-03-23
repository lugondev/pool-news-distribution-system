"""
FastAPI dashboard với htmx real-time updates + source/category management.
"""
import logging
import math
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import redis.asyncio as aioredis
import yaml
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.routing import APIRoute
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from storage.redis_store import get_latest_articles, get_feed_stats, get_article
from storage.sqlite_stats import get_dashboard_stats, get_recent_webhook_logs, get_recent_ai_logs, get_recent_telegram_logs, init_db, log_api_request
from dashboard.api_router import router as api_router, set_redis as api_set_redis

logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))


def _fmt_dt(iso_str: str, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Format ISO datetime string for display (UTC)."""
    if not iso_str:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime(fmt)
    except Exception:
        return iso_str[:16].replace("T", " ")


def _dt_lag(fetched_str: str, published_str: str) -> str:
    """Return human-readable crawl lag: how long after publish the article was fetched."""
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


templates.env.filters["fmt_dt"] = _fmt_dt
templates.env.filters["dt_lag"] = _dt_lag

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
        api_set_redis(_redis)
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


_API_PREFIX = "/api/"

class _APIRequestLogger(BaseHTTPMiddleware):
    """Log method, path, status_code and latency for all /api/* requests."""

    async def dispatch(self, request: Request, call_next):
        if not request.url.path.startswith(_API_PREFIX):
            return await call_next(request)
        started = datetime.now(timezone.utc)
        t0 = started.timestamp()
        response = await call_next(request)
        duration_ms = int((datetime.now(timezone.utc).timestamp() - t0) * 1000)
        error_msg = None if response.status_code < 400 else f"HTTP {response.status_code}"
        try:
            await log_api_request(
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
                duration_ms=duration_ms,
                requested_at=started,
                error_msg=error_msg,
            )
        except Exception:
            pass
        return response


app = FastAPI(title="News Aggregator Dashboard", lifespan=lifespan)
app.add_middleware(_APIRequestLogger)
app.include_router(api_router)

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

LOG_PAGE_SIZE = 15


async def _enrich_logs(logs: list[dict], full: bool = False) -> list[dict]:
    """Attach article data to each log entry from Redis."""
    redis = get_redis()
    for log in logs:
        article = await get_article(redis, log["article_id"])
        if article:
            log["article_title"] = article.get("title", "—")
            if full:
                log["source_name"] = article.get("source_name", "")
                log["lang"] = article.get("lang", "")
                log["category"] = article.get("category", "")
                log["url"] = article.get("url", "")
                log["ai_summary_vi"] = article.get("ai_summary_vi", "")
                log["ai_summary_en"] = article.get("ai_summary_en", "")
                log["ai_status"] = article.get("ai_status", "")
        else:
            log["article_title"] = "—"
    return logs


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return templates.TemplateResponse("settings.html", {"request": request})


@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    return templates.TemplateResponse("logs.html", {"request": request})


@app.get("/partials/settings-ai", response_class=HTMLResponse)
async def settings_ai_partial(request: Request):
    from ai.rewriter import TONE_PROMPTS, SUMMARIZE_PROMPT
    cfg = _read_settings()
    return templates.TemplateResponse("partials/settings_ai.html", {
        "request": request,
        "ai": cfg.get("ai", {}),
        "crawler": cfg.get("crawler", {}),
        "builtin_tone_prompts": TONE_PROMPTS,
        "builtin_prompt_template": SUMMARIZE_PROMPT,
    })


@app.post("/settings/ai", response_class=HTMLResponse)
async def settings_ai_update(
    request: Request,
    enabled: str = Form("off"),
    api_key: str = Form(""),
    base_url: str = Form(""),
    model: str = Form("gpt-4o-mini"),
    tone: str = Form("general"),
    temperature: float = Form(0.3),
    batch_size: int = Form(10),
    max_tokens: int = Form(300),
    retry_attempts: int = Form(3),
    output_languages: str = Form("vi,en"),
    crawl_interval: int = Form(3),
    stagger_groups: int = Form(3),
    ai_interval: int = Form(2),
    domain_delay: str = Form("0.5-1.5"),
    prompt_system: str = Form(""),
    prompt_template: str = Form(""),
):
    valid_tones = ("formal", "casual", "general")
    resolved_tone = tone if tone in valid_tones else "general"

    delay_parts = domain_delay.replace(" ", "").split("-")
    try:
        delay_min = max(0.1, float(delay_parts[0]))
        delay_max = max(delay_min, float(delay_parts[1])) if len(delay_parts) > 1 else delay_min + 1.0
    except (ValueError, IndexError):
        delay_min, delay_max = 0.5, 1.5

    cfg = _read_settings()
    cfg["ai"] = {
        "enabled": enabled == "on",
        "api_key": api_key.strip(),
        "base_url": base_url.strip(),
        "model": model.strip(),
        "tone": resolved_tone,
        "interval_minutes": max(1, min(ai_interval, 30)),
        "temperature": max(0.0, min(float(temperature), 2.0)),
        "batch_size": max(1, min(batch_size, 50)),
        "max_tokens_summary": max(100, min(max_tokens, 1000)),
        "retry_attempts": max(1, min(retry_attempts, 10)),
        "output_languages": [l.strip() for l in output_languages.split(",") if l.strip()],
        "prompt_system": prompt_system.strip(),
        "prompt_template": prompt_template.strip(),
    }
    cfg.setdefault("crawler", {}).update({
        "fetch_interval_minutes": max(1, min(crawl_interval, 60)),
        "stagger_groups": max(1, min(stagger_groups, 10)),
        "domain_delay_min": delay_min,
        "domain_delay_max": delay_max,
    })
    _write_settings(cfg)
    logger.info(
        f"Settings updated: model={cfg['ai']['model']}, tone={resolved_tone}, "
        f"crawl={crawl_interval}min×{stagger_groups}groups, ai={ai_interval}min"
    )
    from ai.rewriter import TONE_PROMPTS, SUMMARIZE_PROMPT
    return templates.TemplateResponse("partials/settings_ai.html", {
        "request": request,
        "ai": cfg["ai"],
        "crawler": cfg["crawler"],
        "builtin_tone_prompts": TONE_PROMPTS,
        "builtin_prompt_template": SUMMARIZE_PROMPT,
        "success": "Settings saved. Restart app to apply interval changes.",
    })


@app.post("/settings/ai/test", response_class=HTMLResponse)
async def settings_ai_test(request: Request):
    from ai.rewriter import test_ai_connection
    cfg = _read_settings().get("ai", {})
    result = await test_ai_connection(
        api_key=cfg.get("api_key"),
        base_url=cfg.get("base_url"),
        model=cfg.get("model"),
        tone=cfg.get("tone", "general"),
    )
    return templates.TemplateResponse("partials/ai_test_result.html", {
        "request": request,
        "result": result,
    })


@app.get("/partials/ai-logs", response_class=HTMLResponse)
async def ai_logs_partial(request: Request, page: int = 1):
    offset = (page - 1) * LOG_PAGE_SIZE
    logs, total = await get_recent_ai_logs(limit=LOG_PAGE_SIZE, offset=offset)
    logs = await _enrich_logs(logs, full=True)
    return templates.TemplateResponse("partials/ai_logs_table.html", {
        "request": request,
        "logs": logs,
        "log_page": page,
        "log_total_pages": max(1, math.ceil(total / LOG_PAGE_SIZE)),
        "log_total": total,
    })


def _get_webhook_endpoints() -> list[dict]:
    return _read_settings().get("webhook", {}).get("endpoints", [])


def _save_webhook_endpoints(endpoints: list[dict]) -> None:
    cfg = _read_settings()
    cfg.setdefault("webhook", {})["endpoints"] = endpoints
    _write_settings(cfg)


def _get_all_categories() -> list[str]:
    cfg = _read_settings()
    return [c["id"] for c in cfg.get("categories", []) if c.get("enabled", True)]


def _get_all_source_ids() -> list[str]:
    with open(SOURCES_PATH) as f:
        sources = yaml.safe_load(f).get("sources", [])
    return [s["id"] for s in sources if s.get("enabled", True)]


async def _webhook_ctx(request, page=1, **extra) -> dict:
    offset = (page - 1) * LOG_PAGE_SIZE
    logs, total = await get_recent_webhook_logs(limit=LOG_PAGE_SIZE, offset=offset)
    logs = await _enrich_logs(logs)
    ctx = {
        "request": request,
        "endpoints": _get_webhook_endpoints(),
        "logs": logs,
        "log_page": page,
        "log_total_pages": max(1, math.ceil(total / LOG_PAGE_SIZE)),
        "log_total": total,
        "all_categories": _get_all_categories(),
        "all_sources": _get_all_source_ids(),
    }
    ctx.update(extra)
    return ctx


@app.get("/partials/settings-webhook", response_class=HTMLResponse)
async def settings_webhook_partial(request: Request, page: int = 1):
    ctx = await _webhook_ctx(request, page=page)
    return templates.TemplateResponse("partials/settings_webhook.html", ctx)


@app.get("/partials/logs-webhook", response_class=HTMLResponse)
async def logs_webhook_partial(request: Request, page: int = 1):
    ctx = await _webhook_ctx(request, page=page)
    return templates.TemplateResponse("partials/webhook_logs_table.html", ctx)


@app.post("/webhooks/add", response_class=HTMLResponse)
async def webhook_add(
    request: Request,
    id: str = Form(...),
    name: str = Form(...),
    url: str = Form(...),
    retry_attempts: int = Form(3),
    retry_delay: int = Form(5),
    timeout: int = Form(10),
    payload_mode: str = Form("full"),
    payload_fields: str = Form(""),
    payload_template: str = Form(""),
    filter_categories_mode: str = Form("all"),
    filter_categories: str = Form(""),
    filter_sources_mode: str = Form("all"),
    filter_sources: str = Form(""),
    rate_limit_max: int = Form(0),
    rate_limit_window_minutes: int = Form(60),
):
    endpoints = _get_webhook_endpoints()
    if any(ep["id"] == id for ep in endpoints):
        ctx = await _webhook_ctx(request, error=f"Webhook '{id}' already exists")
        return templates.TemplateResponse("partials/settings_webhook.html", ctx)
    fields_list = [f.strip() for f in payload_fields.split(",") if f.strip()] if payload_fields else []
    cat_list = [c.strip() for c in filter_categories.split(",") if c.strip()] if filter_categories else []
    src_list = [s.strip() for s in filter_sources.split(",") if s.strip()] if filter_sources else []
    endpoints.append({
        "id": id.strip(), "name": name.strip(), "url": url.strip(),
        "enabled": True,
        "retry_attempts": max(1, min(retry_attempts, 10)),
        "retry_delay_seconds": max(1, min(retry_delay, 60)),
        "timeout_seconds": max(1, min(timeout, 60)),
        "payload_mode": payload_mode,
        "payload_fields": fields_list,
        "payload_template": payload_template,
        "filter_categories_mode": filter_categories_mode,
        "filter_categories": cat_list,
        "filter_sources_mode": filter_sources_mode,
        "filter_sources": src_list,
        "rate_limit_max": max(0, rate_limit_max),
        "rate_limit_window_minutes": max(1, rate_limit_window_minutes),
    })
    _save_webhook_endpoints(endpoints)
    logger.info(f"Webhook added: {id}")
    ctx = await _webhook_ctx(request, success=f"Webhook '{name}' added")
    return templates.TemplateResponse("partials/settings_webhook.html", ctx)


@app.post("/webhooks/{wh_id}/toggle", response_class=HTMLResponse)
async def webhook_toggle(request: Request, wh_id: str):
    endpoints = _get_webhook_endpoints()
    for ep in endpoints:
        if ep["id"] == wh_id:
            ep["enabled"] = not ep.get("enabled", True)
            break
    _save_webhook_endpoints(endpoints)
    ctx = await _webhook_ctx(request)
    return templates.TemplateResponse("partials/settings_webhook.html", ctx)


@app.put("/webhooks/{wh_id}", response_class=HTMLResponse)
async def webhook_update(
    request: Request,
    wh_id: str,
    name: str = Form(...),
    url: str = Form(...),
    retry_attempts: int = Form(3),
    retry_delay: int = Form(5),
    timeout: int = Form(10),
    payload_mode: str = Form("full"),
    payload_fields: str = Form(""),
    payload_template: str = Form(""),
    filter_categories_mode: str = Form("all"),
    filter_categories: str = Form(""),
    filter_sources_mode: str = Form("all"),
    filter_sources: str = Form(""),
    rate_limit_max: int = Form(0),
    rate_limit_window_minutes: int = Form(60),
):
    endpoints = _get_webhook_endpoints()
    fields_list = [f.strip() for f in payload_fields.split(",") if f.strip()] if payload_fields else []
    cat_list = [c.strip() for c in filter_categories.split(",") if c.strip()] if filter_categories else []
    src_list = [s.strip() for s in filter_sources.split(",") if s.strip()] if filter_sources else []
    for ep in endpoints:
        if ep["id"] == wh_id:
            ep["name"] = name.strip()
            ep["url"] = url.strip()
            ep["retry_attempts"] = max(1, min(retry_attempts, 10))
            ep["retry_delay_seconds"] = max(1, min(retry_delay, 60))
            ep["timeout_seconds"] = max(1, min(timeout, 60))
            ep["payload_mode"] = payload_mode
            ep["payload_fields"] = fields_list
            ep["payload_template"] = payload_template
            ep["filter_categories_mode"] = filter_categories_mode
            ep["filter_categories"] = cat_list
            ep["filter_sources_mode"] = filter_sources_mode
            ep["filter_sources"] = src_list
            ep["rate_limit_max"] = max(0, rate_limit_max)
            ep["rate_limit_window_minutes"] = max(1, rate_limit_window_minutes)
            break
    _save_webhook_endpoints(endpoints)
    logger.info(f"Webhook updated: {wh_id}")
    ctx = await _webhook_ctx(request, success=f"Webhook '{name}' updated")
    return templates.TemplateResponse("partials/settings_webhook.html", ctx)


@app.delete("/webhooks/{wh_id}", response_class=HTMLResponse)
async def webhook_delete(request: Request, wh_id: str):
    endpoints = _get_webhook_endpoints()
    endpoints = [ep for ep in endpoints if ep["id"] != wh_id]
    _save_webhook_endpoints(endpoints)
    logger.info(f"Webhook deleted: {wh_id}")
    ctx = await _webhook_ctx(request, success=f"Webhook '{wh_id}' deleted")
    return templates.TemplateResponse("partials/settings_webhook.html", ctx)


# --- Telegram channel management ---

def _get_telegram_channels() -> list[dict]:
    return _read_settings().get("telegram", {}).get("channels", [])


def _save_telegram_channels(channels: list[dict]) -> None:
    cfg = _read_settings()
    cfg.setdefault("telegram", {})["channels"] = channels
    _write_settings(cfg)


def _telegram_ctx(request, **extra) -> dict:
    ctx = {
        "request": request,
        "channels": _get_telegram_channels(),
        "all_categories": _get_all_categories(),
        "all_sources": _get_all_source_ids(),
    }
    ctx.update(extra)
    return ctx


@app.get("/partials/settings-telegram", response_class=HTMLResponse)
async def settings_telegram_partial(request: Request):
    return templates.TemplateResponse(
        "partials/settings_telegram.html", _telegram_ctx(request)
    )


@app.get("/partials/telegram-logs", response_class=HTMLResponse)
async def telegram_logs_partial(request: Request, page: int = 1, channel_id: str | None = None):
    offset = (page - 1) * LOG_PAGE_SIZE
    logs, total = await get_recent_telegram_logs(limit=LOG_PAGE_SIZE, offset=offset, channel_id=channel_id)
    logs = await _enrich_logs(logs, full=True)
    return templates.TemplateResponse("partials/telegram_logs_table.html", {
        "request": request,
        "logs": logs,
        "tg_log_page": page,
        "tg_log_total_pages": max(1, math.ceil(total / LOG_PAGE_SIZE)),
        "tg_log_total": total,
    })


@app.post("/telegram/test-connection", response_class=HTMLResponse)
async def telegram_test_connection(
    bot_token: str = Form(...),
    chat_id: str = Form(...),
):
    from webhook.telegram import send_telegram
    if not bot_token.strip() or not chat_id.strip():
        return HTMLResponse('<span style="color:var(--red)">Bot token and Chat ID are required</span>')
    text = (
        "\u2705 <b>News Aggregator — Test Message</b>\n\n"
        "If you see this, your Telegram integration is working!"
    )
    try:
        status, ok, error = await send_telegram(bot_token.strip(), chat_id.strip(), text, timeout=10)
        if ok:
            return HTMLResponse('<span style="color:var(--green);font-weight:600">&#10003; Test sent!</span>')
        return HTMLResponse(f'<span style="color:var(--red)">{error}</span>')
    except Exception as e:
        return HTMLResponse(f'<span style="color:var(--red)">{e}</span>')


@app.post("/telegram/add", response_class=HTMLResponse)
async def telegram_add(
    request: Request,
    id: str = Form(...),
    name: str = Form(...),
    bot_token: str = Form(...),
    chat_id: str = Form(...),
    lang: str = Form("both"),
    retry_attempts: int = Form(3),
    timeout: int = Form(10),
    payload_mode: str = Form("full"),
    payload_fields: str = Form(""),
    payload_template: str = Form(""),
    filter_categories_mode: str = Form("all"),
    filter_categories: str = Form(""),
    filter_sources_mode: str = Form("all"),
    filter_sources: str = Form(""),
    rate_limit_max: int = Form(0),
    rate_limit_window_minutes: int = Form(60),
):
    channels = _get_telegram_channels()
    if any(ch["id"] == id for ch in channels):
        return templates.TemplateResponse(
            "partials/settings_telegram.html",
            _telegram_ctx(request, error=f"Channel '{id}' already exists"),
        )
    fields_list = [f.strip() for f in payload_fields.split(",") if f.strip()] if payload_fields else []
    cat_list = [c.strip() for c in filter_categories.split(",") if c.strip()] if filter_categories else []
    src_list = [s.strip() for s in filter_sources.split(",") if s.strip()] if filter_sources else []
    channels.append({
        "id": id.strip(), "name": name.strip(),
        "bot_token": bot_token.strip(), "chat_id": chat_id.strip(),
        "lang": lang, "enabled": True,
        "retry_attempts": max(1, min(retry_attempts, 10)),
        "timeout_seconds": max(1, min(timeout, 60)),
        "payload_mode": payload_mode,
        "payload_fields": fields_list,
        "payload_template": payload_template,
        "filter_categories_mode": filter_categories_mode,
        "filter_categories": cat_list,
        "filter_sources_mode": filter_sources_mode,
        "filter_sources": src_list,
        "rate_limit_max": max(0, rate_limit_max),
        "rate_limit_window_minutes": max(1, rate_limit_window_minutes),
    })
    _save_telegram_channels(channels)
    logger.info(f"Telegram channel added: {id}")
    return templates.TemplateResponse(
        "partials/settings_telegram.html",
        _telegram_ctx(request, success=f"Channel '{name}' added"),
    )


@app.post("/telegram/{ch_id}/toggle", response_class=HTMLResponse)
async def telegram_toggle(request: Request, ch_id: str):
    channels = _get_telegram_channels()
    for ch in channels:
        if ch["id"] == ch_id:
            ch["enabled"] = not ch.get("enabled", True)
            break
    _save_telegram_channels(channels)
    return templates.TemplateResponse(
        "partials/settings_telegram.html", _telegram_ctx(request)
    )


@app.put("/telegram/{ch_id}", response_class=HTMLResponse)
async def telegram_update(
    request: Request,
    ch_id: str,
    name: str = Form(...),
    bot_token: str = Form(...),
    chat_id: str = Form(...),
    lang: str = Form("both"),
    retry_attempts: int = Form(3),
    timeout: int = Form(10),
    payload_mode: str = Form("full"),
    payload_fields: str = Form(""),
    payload_template: str = Form(""),
    filter_categories_mode: str = Form("all"),
    filter_categories: str = Form(""),
    filter_sources_mode: str = Form("all"),
    filter_sources: str = Form(""),
    rate_limit_max: int = Form(0),
    rate_limit_window_minutes: int = Form(60),
):
    channels = _get_telegram_channels()
    fields_list = [f.strip() for f in payload_fields.split(",") if f.strip()] if payload_fields else []
    cat_list = [c.strip() for c in filter_categories.split(",") if c.strip()] if filter_categories else []
    src_list = [s.strip() for s in filter_sources.split(",") if s.strip()] if filter_sources else []
    for ch in channels:
        if ch["id"] == ch_id:
            ch["name"] = name.strip()
            ch["bot_token"] = bot_token.strip()
            ch["chat_id"] = chat_id.strip()
            ch["lang"] = lang
            ch["retry_attempts"] = max(1, min(retry_attempts, 10))
            ch["timeout_seconds"] = max(1, min(timeout, 60))
            ch["payload_mode"] = payload_mode
            ch["payload_fields"] = fields_list
            ch["payload_template"] = payload_template
            ch["filter_categories_mode"] = filter_categories_mode
            ch["filter_categories"] = cat_list
            ch["filter_sources_mode"] = filter_sources_mode
            ch["filter_sources"] = src_list
            ch["rate_limit_max"] = max(0, rate_limit_max)
            ch["rate_limit_window_minutes"] = max(1, rate_limit_window_minutes)
            break
    _save_telegram_channels(channels)
    logger.info(f"Telegram channel updated: {ch_id}")
    return templates.TemplateResponse(
        "partials/settings_telegram.html",
        _telegram_ctx(request, success=f"Channel '{name}' updated"),
    )


@app.delete("/telegram/{ch_id}", response_class=HTMLResponse)
async def telegram_delete(request: Request, ch_id: str):
    channels = _get_telegram_channels()
    channels = [ch for ch in channels if ch["id"] != ch_id]
    _save_telegram_channels(channels)
    logger.info(f"Telegram channel deleted: {ch_id}")
    return templates.TemplateResponse(
        "partials/settings_telegram.html",
        _telegram_ctx(request, success=f"Channel '{ch_id}' deleted"),
    )


@app.post("/telegram/{ch_id}/test", response_class=HTMLResponse)
async def telegram_test(request: Request, ch_id: str):
    from webhook.telegram import send_telegram
    channels = _get_telegram_channels()
    target = next((ch for ch in channels if ch["id"] == ch_id), None)
    if not target:
        return HTMLResponse('<span style="color:var(--red)">Channel not found</span>')

    text = (
        "\u2705 <b>News Aggregator — Test Message</b>\n\n"
        f"Channel: <i>{target['name']}</i>\n"
        f"Chat ID: <code>{target['chat_id']}</code>\n\n"
        "If you see this, your Telegram integration is working!"
    )
    try:
        status, ok, error = await send_telegram(
            target["bot_token"], target["chat_id"], text,
            timeout=target.get("timeout_seconds", 10),
        )
        if ok:
            return HTMLResponse('<span style="color:var(--green);font-weight:600">&#10003; Test sent!</span>')
        return HTMLResponse(f'<span style="color:var(--red)">{error}</span>')
    except Exception as e:
        return HTMLResponse(f'<span style="color:var(--red)">{e}</span>')



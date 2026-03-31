"""
FastAPI dashboard — app factory and top-level page routes.

Route organization:
  / /pipeline /article/{id}     — page renders (this file)
  /partials/stats /partials/feed — HTMX partials (this file)
  /sources /categories           — dashboard/routes/sources_ui.py
  /settings /logs                — dashboard/routes/settings_ui.py
  /webhooks /telegram            — dashboard/routes/dispatch_ui.py
  /api/*                         — dashboard/api_router.py → routes/*_api.py
"""

import json
import logging
import math
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import redis.asyncio as aioredis
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from starlette.middleware.base import BaseHTTPMiddleware

from dashboard import redis_state
from dashboard.api_router import router as api_router
from dashboard.config_io import get_categories, read_sources
from dashboard.routes.dispatch_ui import router as dispatch_ui_router
from dashboard.routes.settings_ui import router as settings_ui_router
from dashboard.routes.sources_ui import router as sources_ui_router
from dashboard.routes.social_agents_ui import router as social_agents_ui_router
from dashboard.templates_state import templates
from storage.redis_store import get_article, get_feed_stats, get_latest_articles
from storage.sqlite_stats import (
    get_dashboard_stats,
    get_recent_ai_logs,
    get_recent_webhook_logs,
    get_recent_telegram_logs,
    init_db,
    log_api_request,
)
from realtime.manager import ws_manager

logger = logging.getLogger(__name__)

_BASE_DIR = os.path.dirname(os.path.dirname(__file__))

_redis: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            encoding="utf-8",
            decode_responses=False,
        )
        redis_state.set_redis(_redis)
    return _redis


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield
    if _redis:
        await _redis.aclose()


_API_PREFIX = "/api/"


class _APIRequestLogger(BaseHTTPMiddleware):
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
app.include_router(sources_ui_router)
app.include_router(settings_ui_router)
app.include_router(dispatch_ui_router)
app.include_router(social_agents_ui_router)

PAGE_SIZE = 20


# ── Page routes ───────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "active_page": "feed"})


@app.get("/pipeline", response_class=HTMLResponse)
async def pipeline_view(request: Request):
    return templates.TemplateResponse("pipeline.html", {"request": request, "active_page": "pipeline"})


@app.get("/intelligence", response_class=HTMLResponse)
async def intelligence_view(request: Request):
    return templates.TemplateResponse("intelligence.html", {"request": request, "active_page": "intelligence"})


@app.get("/newsletter", response_class=HTMLResponse)
async def newsletter_view(request: Request):
    return templates.TemplateResponse("newsletter.html", {"request": request, "active_page": "newsletter"})


@app.get("/debates", response_class=HTMLResponse)
async def debates_view(request: Request):
    return templates.TemplateResponse("debates.html", {"request": request, "active_page": "debates"})


@app.get("/rag", response_class=HTMLResponse)
async def rag_view(request: Request):
    return templates.TemplateResponse("rag.html", {"request": request, "active_page": "rag"})


@app.websocket("/ws/pipeline")
async def pipeline_ws(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except (WebSocketDisconnect, Exception):
        ws_manager.disconnect(websocket)


@app.get("/article/{article_id}", response_class=HTMLResponse)
async def article_detail(request: Request, article_id: str):
    redis = get_redis()
    article = await get_article(redis, article_id)

    if not article:
        return HTMLResponse("<h1>Article not found</h1>", status_code=404)

    if article.get("type") != "synthetic":
        return HTMLResponse(
            f"<script>window.location.href='{article.get('url')}'</script>",
            status_code=302,
        )

    source_article_ids = []
    source_ids_raw = article.get("source_article_ids", "")
    if source_ids_raw:
        try:
            source_article_ids = json.loads(source_ids_raw) if isinstance(source_ids_raw, str) else source_ids_raw
        except (json.JSONDecodeError, TypeError):
            pass

    source_articles = []
    for src_id in source_article_ids:
        src = await get_article(redis, src_id)
        if src:
            source_articles.append(src)

    return templates.TemplateResponse(
        "article_detail.html",
        {"request": request, "article": article, "source_articles": source_articles},
    )


# ── HTMX partials ─────────────────────────────────────────────────────────────


@app.get("/partials/stats", response_class=HTMLResponse)
async def stats_partial(request: Request):
    redis = get_redis()
    return templates.TemplateResponse(
        "partials/stats.html",
        {"request": request, "redis": await get_feed_stats(redis), "db": await get_dashboard_stats()},
    )


@app.get("/partials/feed", response_class=HTMLResponse)
async def feed_partial(
    request: Request,
    page: int = 1,
    source: str = None,
    category: str = None,
    article_type: str = None,
):
    redis = get_redis()
    offset = (page - 1) * PAGE_SIZE
    articles, total = await get_latest_articles(
        redis, limit=PAGE_SIZE, offset=offset,
        source_id=source or None, category=category or None, article_type=article_type or None,
    )
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    return templates.TemplateResponse(
        "partials/feed.html",
        {
            "request": request,
            "articles": articles,
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "source": source or "",
            "category": category or "",
            "article_type": article_type or "",
            "sources": read_sources(),
            "categories": get_categories(),
        },
    )

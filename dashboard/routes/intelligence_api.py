"""
Intelligence API — Trends, Stories, Newsletter, Debates.

Routes:
  GET /intelligence/trends          → trending categories + entities
  GET /intelligence/stories         → active stories (optional ?category=)
  GET /intelligence/stories/{id}    → story detail + article list
  GET /intelligence/newsletter      → latest newsletter HTML
  POST /intelligence/newsletter/generate → trigger generation
  GET /intelligence/debates         → recent debate results
  POST /intelligence/debates/run    → manually trigger debate job
"""

import logging

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse

from ai.story_detector import get_active_stories, get_story_articles
from ai.trend_detector import get_trend_snapshot, get_trending_entities
from ai.newsletter import get_latest_newsletter, generate_newsletter
from ai.debate import get_recent_debates, debate_job as _run_debate_job
from dashboard import redis_state
from dashboard.config_io import get_categories

router = APIRouter(prefix="/intelligence", tags=["intelligence"])
logger = logging.getLogger(__name__)


# ── Trends ────────────────────────────────────────────────────────────────────

@router.get("/trends")
async def api_trends(limit: int = Query(default=20, le=50)):
    redis = redis_state.get_redis()
    snapshot = await get_trend_snapshot(redis, limit=limit)
    entities = await get_trending_entities(redis, limit=20)
    return {
        "categories": snapshot,
        "trending_entities": [{"entity": e, "count": c} for e, c in entities],
        "trending_count": sum(1 for s in snapshot if s.get("trending")),
    }


# ── Stories ───────────────────────────────────────────────────────────────────

@router.get("/stories")
async def api_stories(
    category: str | None = Query(default=None),
    limit: int = Query(default=20, le=50),
):
    redis = redis_state.get_redis()
    stories = await get_active_stories(redis, category=category, limit=limit)
    return {"stories": stories, "total": len(stories)}


@router.get("/stories/{story_id}")
async def api_story_detail(story_id: str):
    redis = redis_state.get_redis()

    raw = await redis.hgetall(f"news:story:{story_id}")
    if not raw:
        raise HTTPException(status_code=404, detail="Story not found")

    import json
    story = {k.decode(): v.decode() for k, v in raw.items()}
    story["id"] = story_id
    try:
        story["entities"] = json.loads(story.get("entities", "[]"))
    except Exception:
        story["entities"] = []
    try:
        story["top_sources"] = json.loads(story.get("top_sources", "[]"))
    except Exception:
        story["top_sources"] = []

    article_ids = await get_story_articles(redis, story_id, limit=20)

    pipe = redis.pipeline()
    for aid in article_ids:
        pipe.hgetall(f"news:{aid}")
    results = await pipe.execute()

    articles = []
    for raw_art in results:
        if raw_art:
            art = {k.decode(): v.decode() for k, v in raw_art.items()}
            articles.append({
                "id": art.get("id", ""),
                "title": art.get("title", ""),
                "source_name": art.get("source_name", ""),
                "published_at": art.get("published_at", ""),
                "url": art.get("url", ""),
                "ai_status": art.get("ai_status", ""),
            })

    return {"story": story, "articles": articles}


# ── Newsletter ────────────────────────────────────────────────────────────────

@router.get("/newsletter")
async def api_newsletter_latest():
    redis = redis_state.get_redis()
    nl = await get_latest_newsletter(redis)
    if not nl:
        return {"available": False}
    return {"available": True, "generated_at": nl["generated_at"]}


@router.get("/newsletter/view", response_class=HTMLResponse)
async def api_newsletter_view():
    redis = redis_state.get_redis()
    nl = await get_latest_newsletter(redis)
    if not nl:
        return HTMLResponse("<p style='color:#94a3b8;font-family:monospace;padding:24px'>No newsletter generated yet.</p>")
    return HTMLResponse(nl["html"])


@router.post("/newsletter/generate")
async def api_newsletter_generate(
    language: str = Query(default="English"),
):
    redis = redis_state.get_redis()
    from dashboard.config_io import get_categories, read_settings

    _full_cfg = read_settings()
    ai_cfg = _full_cfg.get("ai", {})

    # Resolve provider credentials (mirrors scheduler._resolve_provider logic)
    pid = ai_cfg.get("provider_id")
    api_key, base_url, model_override = ai_cfg.get("api_key", ""), ai_cfg.get("base_url", ""), None
    if pid:
        for p in ai_cfg.get("providers", []):
            if p.get("id") == pid:
                api_key = p.get("api_key", api_key)
                base_url = p.get("base_url", base_url)
                model_override = p.get("model") or None
                break

    categories_raw = get_categories()
    active_cats = [c["id"] for c in categories_raw if c.get("enabled", True)]

    try:
        result = await generate_newsletter(
            redis=redis,
            categories=active_cats,
            language=language,
            api_key=api_key or None,
            base_url=base_url or None,
            model=model_override,
        )
        return result
    except Exception as exc:
        logger.error(f"Newsletter generation failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


# ── Debates ───────────────────────────────────────────────────────────────────

@router.get("/debates")
async def api_debates(limit: int = Query(default=10, le=20)):
    redis = redis_state.get_redis()
    debates = await get_recent_debates(redis, limit=limit)
    return {"debates": debates, "total": len(debates)}


@router.post("/debates/run")
async def api_debates_run():
    """Manually trigger the debate job — runs immediately outside the scheduler."""
    from dashboard.config_io import read_settings
    redis = redis_state.get_redis()

    cfg = read_settings()

    debate_cfg = cfg.get("debate", {})
    if not debate_cfg.get("enabled", False):
        raise HTTPException(status_code=400, detail="Debate feature is disabled. Set debate.enabled: true in settings.yaml.")

    ai_cfg = cfg.get("ai", {})
    pid = ai_cfg.get("provider_id")
    api_key = ai_cfg.get("api_key", "")
    base_url = ai_cfg.get("base_url", "")
    model_override = None
    if pid:
        for p in ai_cfg.get("providers", []):
            if p.get("id") == pid:
                api_key = p.get("api_key", api_key)
                base_url = p.get("base_url", base_url)
                model_override = p.get("model") or None
                break

    try:
        count = await _run_debate_job(
            redis=redis,
            api_key=api_key or None,
            base_url=base_url or None,
            model=model_override or debate_cfg.get("model", ""),
        )
        return {"ok": True, "debated": count}
    except Exception as exc:
        logger.error(f"Manual debate run failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))

"""
Social Simulation API

Routes:
  GET  /social-sim                   → list recent simulations
  GET  /social-sim/{sim_id}          → full simulation with comments
  POST /social-sim/run               → trigger new simulation
  GET  /social-sim/persona-types     → available author + netizen types
"""

import logging

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ai.social_sim import (
    get_recent_simulations,
    get_simulation,
    run_simulation,
)
from dashboard import redis_state
from dashboard.config_io import get_categories, read_settings, read_sim_personas
from storage.redis_store import get_latest_articles

router = APIRouter(prefix="/social-sim", tags=["social-sim"])
logger = logging.getLogger(__name__)

class SimRunRequest(BaseModel):
    article_id: str
    author_type: str = "journalist"
    depth: str = "nested"   # flat | nested | full
    language: str = "English"


# ── Metadata ─────────────────────────────────────────────────────────────────

@router.get("/categories")
async def api_sim_categories():
    """Return active categories for article filter."""
    cats = get_categories()
    return {
        "categories": [
            {"id": c["id"], "label": c.get("label", c["id"])}
            for c in cats if c.get("enabled", True)
        ]
    }


@router.get("/persona-types")
async def api_persona_types():
    """Return available author and netizen persona types from config."""
    cfg = read_sim_personas() or {}

    author_types = [
        {"id": k, **{fk: fv for fk, fv in v.items() if fk in ("label", "description", "tone")}}
        for k, v in cfg.get("author_types", {}).items()
    ]
    netizen_types = [
        {"id": k, "label": v.get("label", k), "color": v.get("color", "#94a3b8"), "badge": v.get("badge", "💬")}
        for k, v in cfg.get("netizen_types", {}).items()
    ]
    return {"author_types": author_types, "netizen_types": netizen_types}


# ── List / get ────────────────────────────────────────────────────────────────

@router.get("")
async def api_list_simulations(limit: int = Query(default=10, le=30)):
    redis = redis_state.get_redis()
    sims = await get_recent_simulations(redis, limit=limit)
    return {"simulations": sims, "total": len(sims)}


@router.get("/{sim_id}")
async def api_get_simulation(sim_id: str):
    redis = redis_state.get_redis()
    sim = await get_simulation(redis, sim_id)
    if not sim:
        raise HTTPException(status_code=404, detail=f"Simulation '{sim_id}' not found")
    return sim


# ── Run ───────────────────────────────────────────────────────────────────────

@router.post("/run")
async def api_run_simulation(body: SimRunRequest):
    """Trigger a new social media conversation simulation for an article."""
    redis = redis_state.get_redis()

    # Validate depth
    if body.depth not in ("flat", "nested", "full"):
        raise HTTPException(status_code=422, detail="depth must be flat, nested, or full")

    # Resolve AI credentials
    cfg = read_settings()
    ai_cfg = cfg.get("ai", {})
    pid = ai_cfg.get("provider_id")
    api_key = ai_cfg.get("api_key", "")
    base_url = ai_cfg.get("base_url", "")
    model = None
    temperature = float(ai_cfg.get("temperature", 0.85))
    if pid:
        for p in ai_cfg.get("providers", []):
            if p.get("id") == pid:
                api_key = p.get("api_key", api_key)
                base_url = p.get("base_url", base_url)
                model = p.get("model") or None
                break

    try:
        result = await run_simulation(
            redis=redis,
            article_id=body.article_id,
            author_type=body.author_type,
            depth=body.depth,
            language=body.language,
            api_key=api_key or None,
            base_url=base_url or None,
            model=model,
            temperature=temperature,
        )
        if result is None:
            raise HTTPException(status_code=404, detail=f"Article '{body.article_id}' not found in Redis")
        return {"ok": True, **result}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Simulation run failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


# ── Article picker helper ─────────────────────────────────────────────────────

@router.get("/articles/recent")
async def api_recent_articles(
    limit: int = Query(default=20, le=50),
    category: str | None = Query(default=None),
):
    """Return recent articles for the run form article selector, optionally filtered by category."""
    redis = redis_state.get_redis()
    articles, _ = await get_latest_articles(
        redis, limit=limit, article_type="original",
        category=category or None,
    )
    return {
        "articles": [
            {
                "id": a.get("id", ""),
                "title": a.get("title", ""),
                "source_name": a.get("source_name", ""),
                "category": a.get("category", ""),
                "published_at": a.get("published_at", ""),
            }
            for a in articles
            if a.get("id") and a.get("title")
        ]
    }

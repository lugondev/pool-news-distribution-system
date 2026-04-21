"""JSON API — Social Article generation, CRUD, and management."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from dashboard.config_io import read_settings, write_settings
from dashboard.redis_state import get_redis
from ai.social_article import (
    generate_social_article,
    save_social_article,
    get_social_article,
    list_social_articles,
    STYLE_PRESETS,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Social Article Settings ──────────────────────────────────────────────────


class SocialArticleSettingsIn(BaseModel):
    enabled: bool | None = None
    provider_id: str | None = None
    default_style: str | None = None
    default_category: str | None = None
    default_hours: int | None = None
    min_articles: int | None = None
    max_articles: int | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    interval_minutes: int | None = None
    auto_generate: bool | None = None


@router.get("/settings/social-article")
async def get_social_article_settings():
    """Get social article configuration."""
    cfg = read_settings()
    return cfg.get("social_article", {})


@router.put("/settings/social-article")
async def update_social_article_settings(body: SocialArticleSettingsIn):
    """Update social article configuration."""
    cfg = read_settings()
    social = cfg.setdefault("social_article", {})
    
    if body.enabled is not None:
        social["enabled"] = body.enabled
    if body.provider_id is not None:
        social["provider_id"] = body.provider_id or None
    if body.default_style is not None:
        social["default_style"] = body.default_style
    if body.default_category is not None:
        social["default_category"] = body.default_category or None
    if body.default_hours is not None:
        social["default_hours"] = max(1, min(body.default_hours, 168))
    if body.min_articles is not None:
        social["min_articles"] = max(1, min(body.min_articles, 50))
    if body.max_articles is not None:
        social["max_articles"] = max(1, min(body.max_articles, 100))
    if body.temperature is not None:
        social["temperature"] = max(0.0, min(body.temperature, 2.0))
    if body.max_tokens is not None:
        social["max_tokens"] = max(1000, min(body.max_tokens, 8000))
    if body.interval_minutes is not None:
        social["interval_minutes"] = max(60, min(body.interval_minutes, 1440))
    if body.auto_generate is not None:
        social["auto_generate"] = body.auto_generate
    
    cfg["social_article"] = social
    write_settings(cfg)
    logger.info("API: Social article settings updated")
    return {"ok": True, "social_article": social}


@router.post("/settings/social-article/toggle")
async def toggle_social_article():
    """Toggle social article feature on/off."""
    cfg = read_settings()
    social = cfg.setdefault("social_article", {})
    social["enabled"] = not social.get("enabled", False)
    write_settings(cfg)
    logger.info(f"API: Social article toggled → {'on' if social['enabled'] else 'off'}")
    return {"ok": True, "enabled": social["enabled"]}


# ── Style Presets ─────────────────────────────────────────────────────────────


@router.get("/social-article/styles")
async def list_style_presets():
    """List available style presets."""
    return {
        "presets": [
            {
                "id": key,
                "name": preset["name"],
                "description": preset["description"],
                "tone": preset["tone"],
                "length": preset["length"],
                "sections": preset["sections"],
            }
            for key, preset in STYLE_PRESETS.items()
        ]
    }


# ── Article Generation ────────────────────────────────────────────────────────


class GenerateArticleIn(BaseModel):
    provider_id: str | None = None
    categories: list[str] | None = None
    style_preset: str | None = None
    custom_style: dict | None = None
    hours: int = 24
    min_articles: int = 3
    max_articles: int = 20
    temperature: float = 0.7
    max_tokens: int = 4000
    save: bool = True


@router.post("/social-article/generate")
async def generate_article(body: GenerateArticleIn):
    """
    Generate a new social article from recent news.
    
    Returns the generated article and optionally saves it to Redis.
    """
    redis = get_redis()
    
    try:
        article = await generate_social_article(
            redis_client=redis,
            provider_id=body.provider_id,
            categories=body.categories,
            style_preset=body.style_preset,
            custom_style=body.custom_style,
            hours=body.hours,
            min_articles=body.min_articles,
            max_articles=body.max_articles,
            temperature=body.temperature,
            max_tokens=body.max_tokens,
        )
        
        article_id = None
        if body.save:
            article_id = await save_social_article(redis, article)
        
        return {
            "ok": True,
            "article": article,
            "article_id": article_id,
        }
    
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to generate social article: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Generation failed: {str(e)}")


# ── Article CRUD ──────────────────────────────────────────────────────────────


@router.get("/social-article/list")
async def list_articles(limit: int = 50):
    """List recent social articles (metadata only)."""
    redis = get_redis()
    
    try:
        articles = await list_social_articles(redis, limit=limit)
        return {"ok": True, "articles": articles, "count": len(articles)}
    except Exception as e:
        logger.error(f"Failed to list social articles: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/social-article/{article_id}")
async def get_article(article_id: str):
    """Get a specific social article by ID."""
    redis = get_redis()
    
    try:
        article = await get_social_article(redis, article_id)
        
        if not article:
            raise HTTPException(status_code=404, detail="Article not found")
        
        return {"ok": True, "article": article}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get social article {article_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/social-article/{article_id}")
async def delete_article(article_id: str):
    """Delete a social article."""
    redis = get_redis()
    
    try:
        # Remove from Redis
        key = f"social_article:{article_id}"
        deleted = await redis.delete(key)
        
        # Remove from index
        await redis.zrem("social_articles:index", article_id)
        
        if deleted == 0:
            raise HTTPException(status_code=404, detail="Article not found")
        
        logger.info(f"Deleted social article: {article_id}")
        return {"ok": True, "deleted": article_id}
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete social article {article_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ── Quick Generate (with defaults) ────────────────────────────────────────────


@router.post("/social-article/quick-generate")
async def quick_generate():
    """
    Quick generate using default settings from config.
    Useful for scheduled/automated generation.
    """
    cfg = read_settings()
    social_cfg = cfg.get("social_article", {})
    
    if not social_cfg.get("enabled", False):
        raise HTTPException(status_code=400, detail="Social article feature is disabled")
    
    redis = get_redis()
    
    try:
        # Convert single category to list for backward compatibility
        categories = None
        if social_cfg.get("default_category"):
            categories = [social_cfg["default_category"]]
        
        article = await generate_social_article(
            redis_client=redis,
            provider_id=social_cfg.get("provider_id"),
            categories=categories,
            style_preset=social_cfg.get("default_style", "blog_formal"),
            custom_style=None,
            hours=social_cfg.get("default_hours", 24),
            min_articles=social_cfg.get("min_articles", 3),
            max_articles=social_cfg.get("max_articles", 20),
            temperature=social_cfg.get("temperature", 0.7),
            max_tokens=social_cfg.get("max_tokens", 4000),
        )
        
        article_id = await save_social_article(redis, article)
        
        logger.info(f"Quick-generated social article: {article_id}")
        
        return {
            "ok": True,
            "article": article,
            "article_id": article_id,
        }
    
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to quick-generate social article: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Generation failed: {str(e)}")

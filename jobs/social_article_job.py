"""
Scheduled job: Social Article Generation

Runs periodically to generate long-form articles from recent news.
Only runs when social_article.enabled=true and social_article.auto_generate=true.
"""

import asyncio
import logging

import redis.asyncio as aioredis

from ai.social_article import generate_social_article, save_social_article
from dashboard.config_io import read_settings

logger = logging.getLogger(__name__)


async def social_article_job(redis_client: aioredis.Redis) -> None:
    """
    Generate social articles based on configured settings.
    
    This job:
    1. Checks if social_article feature is enabled and auto_generate is on
    2. Reads default settings from config
    3. Generates article using configured parameters
    4. Saves to Redis with TTL
    """
    
    cfg = read_settings()
    social_cfg = cfg.get("social_article", {})
    
    # Check if enabled and auto-generate is on
    if not social_cfg.get("enabled", False):
        logger.debug("Social article job skipped: feature disabled")
        return
    
    if not social_cfg.get("auto_generate", False):
        logger.debug("Social article job skipped: auto_generate disabled")
        return
    
    # Extract config
    provider_id = social_cfg.get("provider_id")
    category = social_cfg.get("default_category")
    style_preset = social_cfg.get("default_style", "blog_formal")
    hours = social_cfg.get("default_hours", 24)
    min_articles = social_cfg.get("min_articles", 3)
    max_articles = social_cfg.get("max_articles", 20)
    temperature = social_cfg.get("temperature", 0.7)
    max_tokens = social_cfg.get("max_tokens", 4000)
    
    logger.info(
        f"Social article job starting: category={category or 'all'}, "
        f"style={style_preset}, hours={hours}"
    )
    
    try:
        # Generate article
        article = await generate_social_article(
            redis_client=redis_client,
            provider_id=provider_id,
            category=category,
            style_preset=style_preset,
            custom_style=None,
            hours=hours,
            min_articles=min_articles,
            max_articles=max_articles,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        
        # Save to Redis
        article_id = await save_social_article(redis_client, article)
        
        logger.info(
            f"Social article generated successfully: {article_id} "
            f"(title: {article.get('title', 'N/A')[:50]}...)"
        )
        
        # TODO: Optional webhook dispatch
        # If you want to send generated articles to webhooks, add dispatch logic here
        
    except ValueError as e:
        logger.warning(f"Social article job skipped: {e}")
    except Exception as e:
        logger.error(f"Social article job failed: {e}", exc_info=True)

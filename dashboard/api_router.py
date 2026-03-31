"""
JSON API router — aggregates all domain sub-routers.

Routes are organized by domain:
  /api/sources, /api/categories    → routes/sources_api.py
  /api/settings/ai, /api/providers → routes/ai_api.py
  /api/webhooks, /api/telegram     → routes/webhooks_api.py
  /api/news, /api/stats, /api/logs → routes/logs_api.py
  /api/intelligence/*              → routes/intelligence_api.py
"""

from fastapi import APIRouter

from dashboard import redis_state
from dashboard.routes import ai_api, intelligence_api, logs_api, rag_api, social_agents_api, sources_api, webhooks_api

router = APIRouter(prefix="/api", tags=["api"])

router.include_router(sources_api.router)
router.include_router(ai_api.router)
router.include_router(webhooks_api.router)
router.include_router(logs_api.router)
router.include_router(rag_api.router)
router.include_router(intelligence_api.router)
router.include_router(social_agents_api.router)


def set_redis(r) -> None:
    """Called by app.py at startup to initialize the shared Redis connection."""
    redis_state.set_redis(r)

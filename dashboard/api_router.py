"""
JSON API router — aggregates all domain sub-routers.

Routes are organized by domain:
  /api/sources, /api/categories    → routes/sources_api.py
  /api/settings/ai, /api/providers → routes/ai_api.py
  /api/webhooks, /api/telegram     → routes/webhooks_api.py
  /api/news, /api/stats, /api/logs → routes/logs_api.py
  /api/intelligence/*              → routes/intelligence_api.py
"""

from fastapi import APIRouter, Depends

from auth import require_login
from dashboard import redis_state
from dashboard.routes import (
    account_ui,
    ai_api,
    channels_api,
    embedding_providers_api,
    intelligence_api,
    logs_api,
    rag_api,
    schedules_api,
    social_agents_api,
    social_article_api,
    social_sim_api,
    sources_api,
    users_api,
    webhooks_api,
)

router = APIRouter(prefix="/api", tags=["api"])

# All admin API requires login. Specific role/perm gating is applied per-route
# in each sub-router (see auth deps). channels_api is excluded here because it
# mixes admin (login required) and consumer (X-API-Key, no user auth) routes —
# its routes opt into login individually.
_login = [Depends(require_login())]

router.include_router(sources_api.router,             dependencies=_login)
router.include_router(ai_api.router,                  dependencies=_login)
router.include_router(embedding_providers_api.router, dependencies=_login)
router.include_router(webhooks_api.router,            dependencies=_login)
router.include_router(channels_api.router)  # mixed — auth applied per-route
router.include_router(schedules_api.router,           dependencies=_login)
router.include_router(logs_api.router,                dependencies=_login)
router.include_router(rag_api.router,                 dependencies=_login)
router.include_router(intelligence_api.router,        dependencies=_login)
router.include_router(social_agents_api.router,       dependencies=_login)
router.include_router(social_article_api.router,      dependencies=_login)
router.include_router(social_sim_api.router,          dependencies=_login)
# users_api has its own per-route manager+scope gate.
router.include_router(users_api.api_router)
# account_ui has its own per-route login gate (any role).
router.include_router(account_ui.api_router)


def set_redis(r) -> None:
    """Called by app.py at startup to initialize the shared Redis connection."""
    redis_state.set_redis(r)

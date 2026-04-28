"""
Social Agents API

Routes:
  GET  /social-agents             → list all agents with status
  GET  /social-agents/{id}        → single agent config
  GET  /social-agents/{id}/posts  → recent posts for agent
  POST /social-agents/{id}/run    → trigger generation (manual only)
  GET  /social-agents/posts/all   → all recent posts (across agents)
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from ai.social_poster import (
    get_agent,
    get_recent_posts,
    load_social_agents,
    run_social_agent,
)
from auth import require_perm, require_role
from dashboard import redis_state
from dashboard.config_io import read_settings, write_social_agents

router = APIRouter(prefix="/social-agents", tags=["social-agents"])
_mgr = [Depends(require_role("manager"))]
_perm_run = [Depends(require_perm("can_run_social_agent"))]
logger = logging.getLogger(__name__)


# ── List / get agents ─────────────────────────────────────────────────────────

@router.get("")
async def api_list_agents():
    agents = load_social_agents()
    return {"agents": agents, "total": len(agents)}


@router.get("/posts/all")
async def api_all_posts(limit: int = Query(default=20, le=50)):
    redis = redis_state.get_redis()
    posts = await get_recent_posts(redis, agent_id=None, limit=limit)
    return {"posts": posts, "total": len(posts)}


@router.get("/{agent_id}")
async def api_get_agent(agent_id: str):
    agent = get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    return agent


@router.get("/{agent_id}/posts")
async def api_agent_posts(agent_id: str, limit: int = Query(default=20, le=50)):
    redis = redis_state.get_redis()
    posts = await get_recent_posts(redis, agent_id=agent_id, limit=limit)
    return {"posts": posts, "total": len(posts), "agent_id": agent_id}


# ── Run ───────────────────────────────────────────────────────────────────────

@router.post("/{agent_id}/run", dependencies=_perm_run)
async def api_run_agent(agent_id: str):
    """Manually trigger an agent to generate posts right now."""
    redis = redis_state.get_redis()

    # Resolve AI credentials from main settings
    cfg = read_settings()
    ai_cfg = cfg.get("ai", {})
    pid = ai_cfg.get("provider_id")
    api_key = ai_cfg.get("api_key", "")
    base_url = ai_cfg.get("base_url", "")
    model = None
    temperature = float(ai_cfg.get("temperature", 0.7))
    if pid:
        for p in ai_cfg.get("providers", []):
            if p.get("id") == pid:
                api_key = p.get("api_key", api_key)
                base_url = p.get("base_url", base_url)
                model = p.get("model") or None
                break

    try:
        result = await run_social_agent(
            redis=redis,
            agent_id=agent_id,
            api_key=api_key or None,
            base_url=base_url or None,
            model=model,
            temperature=temperature,
        )
        if result.get("error"):
            raise HTTPException(status_code=400, detail=result["error"])
        return {"ok": True, **result}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Social agent run failed agent={agent_id}: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


# ── CRUD (simple YAML-backed) ─────────────────────────────────────────────────

@router.post("", dependencies=_mgr)
async def api_create_agent(body: dict):
    """Create a new agent. Body must contain at least: id, name, persona, platforms."""
    required = ["id", "name", "persona", "platforms"]
    for field in required:
        if field not in body:
            raise HTTPException(status_code=422, detail=f"Missing field: {field}")

    agents = load_social_agents()
    if any(a["id"] == body["id"] for a in agents):
        raise HTTPException(status_code=409, detail=f"Agent id '{body['id']}' already exists")

    # Defaults
    body.setdefault("enabled", True)
    body.setdefault("source_filter", {"categories": [], "max_articles": 5, "recency_minutes": 120})

    agents.append(body)
    _save_agents(agents)
    return {"ok": True, "agent": body}


@router.put("/{agent_id}", dependencies=_mgr)
async def api_update_agent(agent_id: str, body: dict):
    agents = load_social_agents()
    idx = next((i for i, a in enumerate(agents) if a["id"] == agent_id), None)
    if idx is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    # Merge — keep id unchanged
    body["id"] = agent_id
    agents[idx] = body
    _save_agents(agents)
    return {"ok": True, "agent": body}


@router.delete("/{agent_id}", dependencies=_mgr)
async def api_delete_agent(agent_id: str):
    agents = load_social_agents()
    new_agents = [a for a in agents if a["id"] != agent_id]
    if len(new_agents) == len(agents):
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    _save_agents(new_agents)
    return {"ok": True, "deleted": agent_id}


def _save_agents(agents: list[dict]) -> None:
    """Persist via config_io — backend (yaml or db) decides storage."""
    write_social_agents(agents)

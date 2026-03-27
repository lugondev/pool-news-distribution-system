"""JSON API — AI Settings, Providers, and Configs CRUD."""

import logging
import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from dashboard.config_io import read_settings, write_settings

logger = logging.getLogger(__name__)
router = APIRouter()


# ── AI Settings ──────────────────────────────────────────────────────────────


class AISettingsIn(BaseModel):
    enabled: bool | None = None
    model: str | None = None
    temperature: float | None = None
    batch_size: int | None = None
    max_tokens_summary: int | None = None
    retry_attempts: int | None = None
    output_languages: list[str] | None = None


@router.get("/settings/ai")
async def get_ai_settings():
    return read_settings().get("ai", {})


@router.put("/settings/ai")
async def update_ai_settings(body: AISettingsIn):
    cfg = read_settings()
    ai = cfg.get("ai", {})
    if body.enabled is not None:
        ai["enabled"] = body.enabled
    if body.model is not None:
        ai["model"] = body.model
    if body.temperature is not None:
        ai["temperature"] = max(0.0, min(body.temperature, 2.0))
    if body.batch_size is not None:
        ai["batch_size"] = max(1, min(body.batch_size, 20))
    if body.max_tokens_summary is not None:
        ai["max_tokens_summary"] = max(100, min(body.max_tokens_summary, 1000))
    if body.retry_attempts is not None:
        ai["retry_attempts"] = max(1, min(body.retry_attempts, 10))
    if body.output_languages is not None:
        ai["output_languages"] = body.output_languages
    cfg["ai"] = ai
    write_settings(cfg)
    logger.info("API: AI settings updated")
    return {"ok": True, "ai": ai}


@router.post("/settings/ai/toggle")
async def toggle_ai_summary():
    cfg = read_settings()
    ai = cfg.setdefault("ai", {})
    ai["enabled"] = not ai.get("enabled", True)
    write_settings(cfg)
    logger.info(f"API: AI summary toggled → {'on' if ai['enabled'] else 'off'}")
    return {"ok": True, "enabled": ai["enabled"]}


@router.post("/settings/ai/synthesis/toggle")
async def toggle_ai_synthesis():
    cfg = read_settings()
    synthesis = cfg.setdefault("ai", {}).setdefault("topic_synthesis", {})
    synthesis["enabled"] = not synthesis.get("enabled", False)
    write_settings(cfg)
    logger.info(f"API: Topic synthesis toggled → {'on' if synthesis['enabled'] else 'off'}")
    return {"ok": True, "enabled": synthesis["enabled"]}


# ── AI Providers ──────────────────────────────────────────────────────────────


class ProviderIn(BaseModel):
    name: str
    api_key: str
    base_url: str
    model: str = ""


@router.get("/providers")
async def list_providers():
    cfg = read_settings()
    providers = cfg.get("ai", {}).get("providers", [])
    return [
        {**p, "api_key": p["api_key"][:12] + "…" if p.get("api_key") else ""}
        for p in providers
    ]


@router.get("/providers/{provider_id}")
async def get_provider(provider_id: str):
    cfg = read_settings()
    provider = next(
        (p for p in cfg.get("ai", {}).get("providers", []) if p["id"] == provider_id),
        None,
    )
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    return provider


@router.post("/providers")
async def create_provider(body: ProviderIn):
    cfg = read_settings()
    ai = cfg.setdefault("ai", {})
    providers = ai.setdefault("providers", [])
    pid = re.sub(r"[^a-z0-9]+", "-", body.name.lower()).strip("-") or f"provider-{len(providers)+1}"
    if any(p["id"] == pid for p in providers):
        pid = f"{pid}-{len(providers)+1}"
    entry = {"id": pid, "name": body.name, "api_key": body.api_key, "base_url": body.base_url}
    if body.model:
        entry["model"] = body.model
    providers.append(entry)
    write_settings(cfg)
    logger.info(f"API: provider created id={pid}")
    return {"ok": True, "id": pid}


@router.put("/providers/{provider_id}")
async def update_provider(provider_id: str, body: ProviderIn):
    cfg = read_settings()
    providers = cfg.get("ai", {}).get("providers", [])
    for p in providers:
        if p["id"] == provider_id:
            p["name"] = body.name
            p["api_key"] = body.api_key
            p["base_url"] = body.base_url
            p["model"] = body.model or None
            write_settings(cfg)
            logger.info(f"API: provider updated id={provider_id}")
            return {"ok": True}
    raise HTTPException(status_code=404, detail="Provider not found")


@router.delete("/providers/{provider_id}")
async def delete_provider(provider_id: str):
    cfg = read_settings()
    ai = cfg.get("ai", {})
    providers = ai.get("providers", [])
    new_providers = [p for p in providers if p["id"] != provider_id]
    if len(new_providers) == len(providers):
        raise HTTPException(status_code=404, detail="Provider not found")
    ai["providers"] = new_providers
    if ai.get("provider_id") == provider_id:
        ai["provider_id"] = new_providers[0]["id"] if new_providers else None
    synthesis = ai.get("topic_synthesis", {})
    if synthesis.get("provider_id") == provider_id:
        synthesis["provider_id"] = None
    write_settings(cfg)
    logger.info(f"API: provider deleted id={provider_id}")
    return {"ok": True}


@router.post("/providers/{provider_id}/test")
async def test_provider(provider_id: str):
    from ai.rewriter import test_ai_connection

    cfg = read_settings()
    provider = next(
        (p for p in cfg.get("ai", {}).get("providers", []) if p["id"] == provider_id),
        None,
    )
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    model = provider.get("model") or cfg.get("ai", {}).get("model", "gpt-4o-mini")
    tone = cfg.get("ai", {}).get("tone", "general")
    return await test_ai_connection(
        api_key=provider.get("api_key"),
        base_url=provider.get("base_url"),
        model=model,
        tone=tone,
    )


# ── AI Configs ───────────────────────────────────────────────────────────────


class AiConfigIn(BaseModel):
    name: str
    tone: str = "general"
    prompt_system: str = ""
    prompt_template: str = ""
    is_default: bool = False


@router.get("/ai-configs")
async def list_ai_configs():
    cfg = read_settings()
    return {"configs": cfg.get("ai", {}).get("configs", [])}


@router.post("/ai-configs", status_code=201)
async def create_ai_config(body: AiConfigIn):
    cfg = read_settings()
    ai = cfg.setdefault("ai", {})
    configs = ai.setdefault("configs", [])
    cid = re.sub(r"[^a-z0-9]+", "-", body.name.lower()).strip("-") or f"cfg-{len(configs)+1}"
    if any(c["id"] == cid for c in configs):
        cid = f"{cid}-{len(configs)+1}"
    if body.is_default:
        for c in configs:
            c["is_default"] = False
    tone = body.tone if body.tone in ("formal", "casual", "general") else "general"
    entry = {
        "id": cid,
        "name": body.name,
        "tone": tone,
        "prompt_system": body.prompt_system,
        "prompt_template": body.prompt_template,
        "is_default": body.is_default,
    }
    configs.append(entry)
    write_settings(cfg)
    logger.info(f"API: AI config created id={cid}")
    return {"ok": True, "id": cid}


@router.put("/ai-configs/{config_id}")
async def update_ai_config(config_id: str, body: AiConfigIn):
    cfg = read_settings()
    configs = cfg.get("ai", {}).get("configs", [])
    target = next((c for c in configs if c["id"] == config_id), None)
    if not target:
        raise HTTPException(404, "Config not found")
    if body.is_default:
        for c in configs:
            c["is_default"] = False
    tone = body.tone if body.tone in ("formal", "casual", "general") else "general"
    target.update({
        "name": body.name,
        "tone": tone,
        "prompt_system": body.prompt_system,
        "prompt_template": body.prompt_template,
        "is_default": body.is_default,
    })
    write_settings(cfg)
    logger.info(f"API: AI config updated id={config_id}")
    return {"ok": True}


@router.post("/ai-configs/{config_id}/set-default")
async def set_default_ai_config(config_id: str):
    cfg = read_settings()
    configs = cfg.get("ai", {}).get("configs", [])
    found = False
    for c in configs:
        if c["id"] == config_id:
            c["is_default"] = True
            found = True
        else:
            c["is_default"] = False
    if not found:
        raise HTTPException(404, "Config not found")
    write_settings(cfg)
    return {"ok": True}


@router.delete("/ai-configs/{config_id}")
async def delete_ai_config(config_id: str):
    cfg = read_settings()
    ai = cfg.get("ai", {})
    configs = ai.get("configs", [])
    new_configs = [c for c in configs if c["id"] != config_id]
    if len(new_configs) == len(configs):
        raise HTTPException(404, "Config not found")
    ai["configs"] = new_configs
    write_settings(cfg)
    logger.info(f"API: AI config deleted id={config_id}")
    return {"ok": True}

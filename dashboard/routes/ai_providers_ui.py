"""HTML routes — AI Provider Routing configuration."""

import logging
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from dashboard.config_io import read_settings, write_settings
from dashboard.templates_state import templates

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/ai-providers", response_class=HTMLResponse)
async def ai_providers_page(request: Request):
    """AI Provider Routing configuration page."""
    return templates.TemplateResponse(
        "ai_providers.html",
        {"request": request, "active_page": "ai-providers"}
    )


@router.get("/partials/ai-providers-config", response_class=HTMLResponse)
async def ai_providers_partial(request: Request):
    """Render AI provider routing config partial."""
    cfg = read_settings()
    ai_cfg = cfg.get("ai", {})
    providers = ai_cfg.get("providers", [])
    routing = ai_cfg.get("provider_routing", {})
    
    return templates.TemplateResponse(
        "partials/ai_providers_config.html",
        {
            "request": request,
            "providers": providers,
            "routing": routing,
        }
    )


@router.post("/ai-providers/update", response_class=HTMLResponse)
async def ai_providers_update(
    request: Request,
    rewrite: str = Form(""),
    synthesis: str = Form(""),
    debate: str = Form(""),
    newsletter: str = Form(""),
    embedding: str = Form("system"),
):
    """Update AI provider routing configuration."""
    cfg = read_settings()
    ai_cfg = cfg.setdefault("ai", {})
    
    # Update routing
    routing = {
        "rewrite": rewrite,
        "synthesis": synthesis,
        "debate": debate,
        "newsletter": newsletter,
        "embedding": embedding,
    }
    ai_cfg["provider_routing"] = routing
    
    write_settings(cfg)
    logger.info(f"AI provider routing updated: {routing}")
    
    # Return updated partial with success message
    providers = ai_cfg.get("providers", [])
    return templates.TemplateResponse(
        "partials/ai_providers_config.html",
        {
            "request": request,
            "providers": providers,
            "routing": routing,
            "success": "Provider routing saved successfully.",
        }
    )

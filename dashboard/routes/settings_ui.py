"""HTML routes — Settings page (General and AI settings)."""

import logging
import math
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from dashboard.config_io import read_settings, write_settings
from dashboard.templates_state import templates
from dashboard.ui_helpers import enrich_logs
from storage.sqlite_stats import get_recent_ai_logs

logger = logging.getLogger(__name__)
router = APIRouter()

LOG_PAGE_SIZE = 15


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return templates.TemplateResponse("settings.html", {"request": request, "active_page": "settings"})


@router.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    return templates.TemplateResponse("logs.html", {"request": request, "active_page": "logs"})


@router.get("/social-articles", response_class=HTMLResponse)
async def social_articles_page(request: Request):
    return templates.TemplateResponse("social_articles.html", {"request": request, "active_page": "social_articles"})


# ── General settings ──────────────────────────────────────────────────────────


@router.get("/partials/settings-general", response_class=HTMLResponse)
async def settings_general_partial(request: Request):
    cfg = read_settings()
    return templates.TemplateResponse(
        "partials/settings_general.html",
        {"request": request, "app_cfg": cfg.get("app", {})},
    )


@router.post("/settings/general", response_class=HTMLResponse)
async def settings_general_update(
    request: Request,
    tz_name: str = Form("Asia/Ho_Chi_Minh", alias="timezone"),
):
    try:
        ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        tz_name = "Asia/Ho_Chi_Minh"
    cfg = read_settings()
    cfg.setdefault("app", {})["timezone"] = tz_name
    write_settings(cfg)
    logger.info(f"General settings updated: timezone={tz_name}")
    return templates.TemplateResponse(
        "partials/settings_general.html",
        {"request": request, "app_cfg": cfg.get("app", {}), "success": "Settings saved."},
    )


# ── AI settings ───────────────────────────────────────────────────────────────


def _ai_partial_ctx(cfg: dict, request: Request, **extra) -> dict:
    from ai.rewriter import TONE_PROMPTS, SUMMARIZE_PROMPT

    ai_cfg = cfg.get("ai", {})
    length_guidance = (
        f"at most {ai_cfg.get('output_limit_chars') or 250} characters"
        if ai_cfg.get("output_limit_enabled")
        else "2-3 sentences"
    )
    return {
        "request": request,
        "ai": ai_cfg,
        "providers": ai_cfg.get("providers", []),
        "ai_configs": ai_cfg.get("configs", []),
        "crawler": cfg.get("crawler", {}),
        "debate": cfg.get("debate", {}),
        "builtin_tone_prompts": TONE_PROMPTS,
        "builtin_prompt_template": SUMMARIZE_PROMPT.replace("{length_guidance}", length_guidance),
        **extra,
    }


@router.get("/partials/settings-ai", response_class=HTMLResponse)
async def settings_ai_partial(request: Request):
    return templates.TemplateResponse(
        "partials/settings_ai.html", _ai_partial_ctx(read_settings(), request)
    )


@router.post("/settings/ai", response_class=HTMLResponse)
async def settings_ai_update(
    request: Request,
    enabled: str = Form("off"),
    api_key: str = Form(""),
    base_url: str = Form(""),
    provider_id: str = Form(""),
    temperature: float = Form(0.3),
    batch_size: int = Form(10),
    max_tokens: int = Form(300),
    retry_attempts: int = Form(3),
    crawl_interval: int = Form(3),
    stagger_groups: int = Form(3),
    ai_interval: int = Form(2),
    domain_delay: str = Form("0.5-1.5"),
    prompt_system: str = Form(""),
    prompt_template: str = Form(""),
    output_limit_enabled: str = Form("off"),
    output_limit_chars: int = Form(250),
    topic_synthesis_enabled: str = Form("off"),
    topic_synthesis_provider_id: str = Form(""),
    topic_synthesis_interval: int = Form(5),
    topic_synthesis_temperature: float = Form(0.5),
    topic_synthesis_min_articles: int = Form(5),
    topic_synthesis_max_articles: int = Form(15),
    debate_enabled: str = Form("off"),
    debate_provider_id: str = Form(""),
    debate_interval: int = Form(30),
):
    delay_parts = domain_delay.replace(" ", "").split("-")
    try:
        delay_min = max(0.1, float(delay_parts[0]))
        delay_max = max(delay_min, float(delay_parts[1])) if len(delay_parts) > 1 else delay_min + 1.0
    except (ValueError, IndexError):
        delay_min, delay_max = 0.5, 1.5

    cfg = read_settings()
    existing_ai = cfg.get("ai", {})
    cfg["ai"] = {
        "providers": existing_ai.get("providers", []),
        "configs": existing_ai.get("configs", []),
        "enabled": enabled == "on",
        "provider_id": provider_id.strip() or None,
        "tone": existing_ai.get("tone", "general"),
        "interval_minutes": max(1, min(ai_interval, 30)),
        "temperature": max(0.0, min(float(temperature), 2.0)),
        "batch_size": max(1, min(batch_size, 50)),
        "max_tokens_summary": max(100, min(max_tokens, 1000)),
        "retry_attempts": max(1, min(retry_attempts, 10)),
        "output_languages": existing_ai.get("output_languages", []),
        "prompt_system": prompt_system.strip(),
        "prompt_template": prompt_template.strip(),
        "output_limit_enabled": output_limit_enabled == "on",
        "output_limit_chars": max(50, min(output_limit_chars, 2000)),
        "topic_synthesis": {
            "enabled": topic_synthesis_enabled == "on",
            "provider_id": topic_synthesis_provider_id.strip() or None,
            "interval_minutes": max(1, min(topic_synthesis_interval, 60)),
            "temperature": max(0.0, min(float(topic_synthesis_temperature), 2.0)),
            "min_articles": max(3, min(topic_synthesis_min_articles, 20)),
            "max_articles": max(5, min(topic_synthesis_max_articles, 50)),
            "max_tokens": 2000,
        },
    }
    cfg.setdefault("crawler", {}).update({
        "fetch_interval_minutes": max(1, min(crawl_interval, 60)),
        "stagger_groups": max(1, min(stagger_groups, 10)),
        "domain_delay_min": delay_min,
        "domain_delay_max": delay_max,
    })
    cfg["debate"] = {
        "enabled": debate_enabled == "on",
        "provider_id": debate_provider_id.strip() or None,
        "interval_minutes": max(5, min(debate_interval, 120)),
    }
    write_settings(cfg)
    logger.info(
        f"Settings updated: crawl={crawl_interval}min×{stagger_groups}groups, "
        f"ai={ai_interval}min, synthesis={topic_synthesis_enabled}, debate={debate_enabled}"
    )
    return templates.TemplateResponse(
        "partials/settings_ai.html",
        _ai_partial_ctx(cfg, request, success="Settings saved. Restart app to apply interval changes."),
    )


@router.post("/settings/ai/test", response_class=HTMLResponse)
async def settings_ai_test(request: Request):
    from ai.rewriter import test_ai_connection

    cfg = read_settings().get("ai", {})
    result = await test_ai_connection(
        api_key=cfg.get("api_key"),
        base_url=cfg.get("base_url"),
        model=cfg.get("model"),
        tone=cfg.get("tone", "general"),
    )
    return templates.TemplateResponse("partials/ai_test_result.html", {"request": request, "result": result})


@router.get("/partials/ai-logs", response_class=HTMLResponse)
async def ai_logs_partial(request: Request, page: int = 1):
    offset = (page - 1) * LOG_PAGE_SIZE
    logs, total = await get_recent_ai_logs(limit=LOG_PAGE_SIZE, offset=offset)
    logs = await enrich_logs(logs, full=True, include_content=True)
    return templates.TemplateResponse(
        "partials/ai_logs_table.html",
        {
            "request": request,
            "logs": logs,
            "log_page": page,
            "log_total_pages": max(1, math.ceil(total / LOG_PAGE_SIZE)),
            "log_total": total,
        },
    )

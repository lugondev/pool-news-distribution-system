"""HTML routes — Webhooks and Telegram channel management (HTMX UI)."""

import logging
import math

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from dashboard.config_io import (
    get_categories,
    get_telegram_channels,
    get_webhook_endpoints,
    read_settings,
    read_sources,
    save_telegram_channels,
    save_webhook_endpoints,
)
from dashboard.templates_state import templates
from dashboard.ui_helpers import enrich_logs
from storage.sqlite_stats import get_recent_telegram_logs, get_recent_webhook_logs

logger = logging.getLogger(__name__)
router = APIRouter()

LOG_PAGE_SIZE = 15


# ── Shared helpers ────────────────────────────────────────────────────────────


def _get_all_categories() -> list[str]:
    return [c["id"] for c in get_categories() if c.get("enabled", True)]


def _get_all_source_ids() -> list[str]:
    return [s["id"] for s in read_sources() if s.get("enabled", True)]


def _get_ai_configs() -> list[dict]:
    return read_settings().get("ai", {}).get("configs", [])


def _parse_comma_list(raw: str, valid: set | None = None) -> list[str]:
    items = [x.strip() for x in raw.split(",") if x.strip()]
    return [x for x in items if valid is None or x in valid]


def _build_endpoint_dict(
    id: str, name: str, url: str,
    http_method: str, content_type: str,
    retry_attempts: int, retry_delay: int, timeout: int,
    payload_mode: str, payload_fields: str, payload_template: str,
    filter_categories_mode: str, filter_categories: str,
    filter_sources_mode: str, filter_sources: str,
    filter_article_types_mode: str, filter_article_types: str,
    ai_mode: str, ai_config_id: str, target_language: str,
    rate_limit_max: int, rate_limit_window_minutes: int,
) -> dict:
    VALID_TYPES = {"original", "synthetic"}
    return {
        "id": id.strip(),
        "name": name.strip(),
        "url": url.strip(),
        "enabled": True,
        "http_method": http_method.upper(),
        "content_type": content_type.strip(),
        "retry_attempts": max(1, min(retry_attempts, 10)),
        "retry_delay_seconds": max(1, min(retry_delay, 60)),
        "timeout_seconds": max(1, min(timeout, 60)),
        "payload_mode": payload_mode,
        "payload_fields": _parse_comma_list(payload_fields),
        "payload_template": payload_template,
        "filter_categories_mode": filter_categories_mode,
        "filter_categories": _parse_comma_list(filter_categories),
        "filter_sources_mode": filter_sources_mode,
        "filter_sources": _parse_comma_list(filter_sources),
        "filter_article_types_mode": filter_article_types_mode,
        "filter_article_types": _parse_comma_list(filter_article_types, VALID_TYPES),
        "ai_mode": ai_mode if ai_mode in ("rewrite", "synthetic", "off") else "rewrite",
        "ai_config_id": ai_config_id.strip() or "",
        "target_language": target_language.strip() or "",
        "rate_limit_max": max(0, rate_limit_max),
        "rate_limit_window_minutes": max(1, rate_limit_window_minutes),
    }


# ── Webhook HTML routes ───────────────────────────────────────────────────────


async def _webhook_ctx(request: Request, page: int = 1, **extra) -> dict:
    offset = (page - 1) * LOG_PAGE_SIZE
    logs, total = await get_recent_webhook_logs(limit=LOG_PAGE_SIZE, offset=offset)
    logs = await enrich_logs(logs, include_content=True)

    endpoints = get_webhook_endpoints()
    id_to_ep = {ep["id"]: ep for ep in endpoints}
    url_to_ep = {ep["url"]: ep for ep in endpoints}

    for log in logs:
        ep = id_to_ep.get(log.get("webhook_id")) or url_to_ep.get(log.get("webhook_url", "—"))
        log["webhook_name"] = ep["name"] if ep else log.get("webhook_url", "—")
        if log.get("article_type") == "synthetic" and ep:
            tgt_lang = ep.get("target_language") or None
            if tgt_lang and (lang_title := log.get(f"_title_{tgt_lang}")):
                log["article_title"] = lang_title

    return {
        "request": request,
        "endpoints": endpoints,
        "logs": logs,
        "log_page": page,
        "log_total_pages": max(1, math.ceil(total / LOG_PAGE_SIZE)),
        "log_total": total,
        "all_categories": _get_all_categories(),
        "all_sources": _get_all_source_ids(),
        "ai_configs": _get_ai_configs(),
        **extra,
    }


@router.get("/partials/settings-webhook", response_class=HTMLResponse)
async def settings_webhook_partial(request: Request, page: int = 1):
    return templates.TemplateResponse("partials/settings_webhook.html", await _webhook_ctx(request, page=page))


@router.get("/partials/logs-webhook", response_class=HTMLResponse)
async def logs_webhook_partial(request: Request, page: int = 1):
    return templates.TemplateResponse("partials/webhook_logs_table.html", await _webhook_ctx(request, page=page))


@router.post("/webhooks/add", response_class=HTMLResponse)
async def webhook_add(
    request: Request,
    id: str = Form(...),
    name: str = Form(...),
    url: str = Form(...),
    http_method: str = Form("POST"),
    content_type: str = Form("application/json"),
    retry_attempts: int = Form(3),
    retry_delay: int = Form(5),
    timeout: int = Form(10),
    payload_mode: str = Form("full"),
    payload_fields: str = Form(""),
    payload_template: str = Form(""),
    filter_categories_mode: str = Form("all"),
    filter_categories: str = Form(""),
    filter_sources_mode: str = Form("all"),
    filter_sources: str = Form(""),
    filter_article_types_mode: str = Form("all"),
    filter_article_types: str = Form(""),
    ai_mode: str = Form("rewrite"),
    ai_config_id: str = Form(""),
    target_language: str = Form(""),
    rate_limit_max: int = Form(0),
    rate_limit_window_minutes: int = Form(60),
):
    endpoints = get_webhook_endpoints()
    if any(ep["id"] == id for ep in endpoints):
        ctx = await _webhook_ctx(request, error=f"Webhook '{id}' already exists")
        return templates.TemplateResponse("partials/settings_webhook.html", ctx)
    endpoints.append(_build_endpoint_dict(
        id, name, url, http_method, content_type, retry_attempts, retry_delay, timeout,
        payload_mode, payload_fields, payload_template,
        filter_categories_mode, filter_categories, filter_sources_mode, filter_sources,
        filter_article_types_mode, filter_article_types, ai_mode, ai_config_id, target_language,
        rate_limit_max, rate_limit_window_minutes,
    ))
    save_webhook_endpoints(endpoints)
    logger.info(f"Webhook added: {id}")
    return templates.TemplateResponse("partials/settings_webhook.html", await _webhook_ctx(request, success=f"Webhook '{name}' added"))


@router.post("/webhooks/{wh_id}/toggle", response_class=HTMLResponse)
async def webhook_toggle(request: Request, wh_id: str):
    endpoints = get_webhook_endpoints()
    for ep in endpoints:
        if ep["id"] == wh_id:
            ep["enabled"] = not ep.get("enabled", True)
            break
    save_webhook_endpoints(endpoints)
    return templates.TemplateResponse("partials/settings_webhook.html", await _webhook_ctx(request))


@router.put("/webhooks/{wh_id}", response_class=HTMLResponse)
async def webhook_update(
    request: Request,
    wh_id: str,
    name: str = Form(...),
    url: str = Form(...),
    http_method: str = Form("POST"),
    content_type: str = Form("application/json"),
    retry_attempts: int = Form(3),
    retry_delay: int = Form(5),
    timeout: int = Form(10),
    payload_mode: str = Form("full"),
    payload_fields: str = Form(""),
    payload_template: str = Form(""),
    filter_categories_mode: str = Form("all"),
    filter_categories: str = Form(""),
    filter_sources_mode: str = Form("all"),
    filter_sources: str = Form(""),
    filter_article_types_mode: str = Form("all"),
    filter_article_types: str = Form(""),
    ai_mode: str = Form("rewrite"),
    ai_config_id: str = Form(""),
    target_language: str = Form(""),
    rate_limit_max: int = Form(0),
    rate_limit_window_minutes: int = Form(60),
):
    endpoints = get_webhook_endpoints()
    updated = _build_endpoint_dict(
        wh_id, name, url, http_method, content_type, retry_attempts, retry_delay, timeout,
        payload_mode, payload_fields, payload_template,
        filter_categories_mode, filter_categories, filter_sources_mode, filter_sources,
        filter_article_types_mode, filter_article_types, ai_mode, ai_config_id, target_language,
        rate_limit_max, rate_limit_window_minutes,
    )
    for i, ep in enumerate(endpoints):
        if ep["id"] == wh_id:
            updated["enabled"] = ep.get("enabled", True)  # preserve enabled state
            endpoints[i] = updated
            break
    save_webhook_endpoints(endpoints)
    logger.info(f"Webhook updated: {wh_id}")
    return templates.TemplateResponse("partials/settings_webhook.html", await _webhook_ctx(request, success=f"Webhook '{name}' updated"))


@router.delete("/webhooks/{wh_id}", response_class=HTMLResponse)
async def webhook_delete(request: Request, wh_id: str):
    endpoints = [ep for ep in get_webhook_endpoints() if ep["id"] != wh_id]
    save_webhook_endpoints(endpoints)
    logger.info(f"Webhook deleted: {wh_id}")
    return templates.TemplateResponse("partials/settings_webhook.html", await _webhook_ctx(request, success=f"Webhook '{wh_id}' deleted"))


# ── Telegram HTML routes ──────────────────────────────────────────────────────


def _telegram_ctx(request: Request, **extra) -> dict:
    return {
        "request": request,
        "channels": get_telegram_channels(),
        "all_categories": _get_all_categories(),
        "all_sources": _get_all_source_ids(),
        "ai_configs": _get_ai_configs(),
        **extra,
    }


def _build_channel_dict(
    id: str, name: str, bot_token: str, chat_id: str,
    retry_attempts: int, timeout: int,
    payload_mode: str, payload_fields: str, payload_template: str,
    filter_categories_mode: str, filter_categories: str,
    filter_sources_mode: str, filter_sources: str,
    filter_article_types_mode: str, filter_article_types: str,
    ai_mode: str, ai_config_id: str, target_language: str,
    rate_limit_max: int, rate_limit_window_minutes: int,
) -> dict:
    VALID_TYPES = {"original", "synthetic"}
    return {
        "id": id.strip(),
        "name": name.strip(),
        "bot_token": bot_token.strip(),
        "chat_id": chat_id.strip(),
        "enabled": True,
        "retry_attempts": max(1, min(retry_attempts, 10)),
        "timeout_seconds": max(1, min(timeout, 60)),
        "payload_mode": payload_mode,
        "payload_fields": _parse_comma_list(payload_fields),
        "payload_template": payload_template,
        "filter_categories_mode": filter_categories_mode,
        "filter_categories": _parse_comma_list(filter_categories),
        "filter_sources_mode": filter_sources_mode,
        "filter_sources": _parse_comma_list(filter_sources),
        "filter_article_types_mode": filter_article_types_mode,
        "filter_article_types": _parse_comma_list(filter_article_types, VALID_TYPES),
        "ai_mode": ai_mode if ai_mode in ("rewrite", "synthetic", "off") else "rewrite",
        "ai_config_id": ai_config_id.strip() or "",
        "target_language": target_language.strip() or "",
        "rate_limit_max": max(0, rate_limit_max),
        "rate_limit_window_minutes": max(1, rate_limit_window_minutes),
    }


@router.get("/partials/settings-telegram", response_class=HTMLResponse)
async def settings_telegram_partial(request: Request):
    return templates.TemplateResponse("partials/settings_telegram.html", _telegram_ctx(request))


@router.get("/partials/telegram-logs", response_class=HTMLResponse)
async def telegram_logs_partial(request: Request, page: int = 1, channel_id: str | None = None):
    offset = (page - 1) * LOG_PAGE_SIZE
    logs, total = await get_recent_telegram_logs(limit=LOG_PAGE_SIZE, offset=offset, channel_id=channel_id)
    logs = await enrich_logs(logs, full=True, include_content=True)

    channels = get_telegram_channels()
    id_to_ch = {ch["id"]: ch for ch in channels}
    for log in logs:
        ch = id_to_ch.get(log.get("channel_id"))
        log["channel_name"] = ch["name"] if ch else log.get("channel_id", "—")
        if log.get("article_type") == "synthetic" and ch:
            tgt_lang = ch.get("target_language") or None
            if tgt_lang and (lang_title := log.get(f"_title_{tgt_lang}")):
                log["article_title"] = lang_title

    return templates.TemplateResponse(
        "partials/telegram_logs_table.html",
        {
            "request": request,
            "logs": logs,
            "tg_log_page": page,
            "tg_log_total_pages": max(1, math.ceil(total / LOG_PAGE_SIZE)),
            "tg_log_total": total,
        },
    )


@router.post("/telegram/test-connection", response_class=HTMLResponse)
async def telegram_test_connection(bot_token: str = Form(...), chat_id: str = Form(...)):
    from webhook.telegram import send_telegram

    if not bot_token.strip() or not chat_id.strip():
        return HTMLResponse('<span style="color:var(--red)">Bot token and Chat ID are required</span>')
    text = "\u2705 <b>News Aggregator — Test Message</b>\n\nIf you see this, your Telegram integration is working!"
    try:
        status, ok, error = await send_telegram(bot_token.strip(), chat_id.strip(), text, timeout=10)
        if ok:
            return HTMLResponse('<span style="color:var(--green);font-weight:600">&#10003; Test sent!</span>')
        return HTMLResponse(f'<span style="color:var(--red)">{error}</span>')
    except Exception as e:
        return HTMLResponse(f'<span style="color:var(--red)">{e}</span>')


@router.post("/telegram/add", response_class=HTMLResponse)
async def telegram_add(
    request: Request,
    id: str = Form(...),
    name: str = Form(...),
    bot_token: str = Form(...),
    chat_id: str = Form(...),
    retry_attempts: int = Form(3),
    timeout: int = Form(10),
    payload_mode: str = Form("full"),
    payload_fields: str = Form(""),
    payload_template: str = Form(""),
    filter_categories_mode: str = Form("all"),
    filter_categories: str = Form(""),
    filter_sources_mode: str = Form("all"),
    filter_sources: str = Form(""),
    filter_article_types_mode: str = Form("all"),
    filter_article_types: str = Form(""),
    ai_mode: str = Form("rewrite"),
    ai_config_id: str = Form(""),
    target_language: str = Form(""),
    rate_limit_max: int = Form(0),
    rate_limit_window_minutes: int = Form(60),
):
    channels = get_telegram_channels()
    if any(ch["id"] == id for ch in channels):
        return templates.TemplateResponse("partials/settings_telegram.html", _telegram_ctx(request, error=f"Channel '{id}' already exists"))
    channels.append(_build_channel_dict(
        id, name, bot_token, chat_id, retry_attempts, timeout,
        payload_mode, payload_fields, payload_template,
        filter_categories_mode, filter_categories, filter_sources_mode, filter_sources,
        filter_article_types_mode, filter_article_types, ai_mode, ai_config_id, target_language,
        rate_limit_max, rate_limit_window_minutes,
    ))
    save_telegram_channels(channels)
    logger.info(f"Telegram channel added: {id}")
    return templates.TemplateResponse("partials/settings_telegram.html", _telegram_ctx(request, success=f"Channel '{name}' added"))


@router.post("/telegram/{ch_id}/toggle", response_class=HTMLResponse)
async def telegram_toggle(request: Request, ch_id: str):
    channels = get_telegram_channels()
    for ch in channels:
        if ch["id"] == ch_id:
            ch["enabled"] = not ch.get("enabled", True)
            break
    save_telegram_channels(channels)
    return templates.TemplateResponse("partials/settings_telegram.html", _telegram_ctx(request))


@router.put("/telegram/{ch_id}", response_class=HTMLResponse)
async def telegram_update(
    request: Request,
    ch_id: str,
    name: str = Form(...),
    bot_token: str = Form(...),
    chat_id: str = Form(...),
    retry_attempts: int = Form(3),
    timeout: int = Form(10),
    payload_mode: str = Form("full"),
    payload_fields: str = Form(""),
    payload_template: str = Form(""),
    filter_categories_mode: str = Form("all"),
    filter_categories: str = Form(""),
    filter_sources_mode: str = Form("all"),
    filter_sources: str = Form(""),
    filter_article_types_mode: str = Form("all"),
    filter_article_types: str = Form(""),
    ai_mode: str = Form("rewrite"),
    ai_config_id: str = Form(""),
    target_language: str = Form(""),
    rate_limit_max: int = Form(0),
    rate_limit_window_minutes: int = Form(60),
):
    channels = get_telegram_channels()
    updated = _build_channel_dict(
        ch_id, name, bot_token, chat_id, retry_attempts, timeout,
        payload_mode, payload_fields, payload_template,
        filter_categories_mode, filter_categories, filter_sources_mode, filter_sources,
        filter_article_types_mode, filter_article_types, ai_mode, ai_config_id, target_language,
        rate_limit_max, rate_limit_window_minutes,
    )
    for i, ch in enumerate(channels):
        if ch["id"] == ch_id:
            updated["enabled"] = ch.get("enabled", True)
            channels[i] = updated
            break
    save_telegram_channels(channels)
    logger.info(f"Telegram channel updated: {ch_id}")
    return templates.TemplateResponse("partials/settings_telegram.html", _telegram_ctx(request, success=f"Channel '{name}' updated"))


@router.delete("/telegram/{ch_id}", response_class=HTMLResponse)
async def telegram_delete(request: Request, ch_id: str):
    channels = [ch for ch in get_telegram_channels() if ch["id"] != ch_id]
    save_telegram_channels(channels)
    logger.info(f"Telegram channel deleted: {ch_id}")
    return templates.TemplateResponse("partials/settings_telegram.html", _telegram_ctx(request, success=f"Channel '{ch_id}' deleted"))


@router.post("/telegram/{ch_id}/test", response_class=HTMLResponse)
async def telegram_test(request: Request, ch_id: str):
    from webhook.telegram import send_telegram

    channels = get_telegram_channels()
    target = next((ch for ch in channels if ch["id"] == ch_id), None)
    if not target:
        return HTMLResponse('<span style="color:var(--red)">Channel not found</span>')

    text = (
        "\u2705 <b>News Aggregator — Test Message</b>\n\n"
        f"Channel: <i>{target['name']}</i>\n"
        f"Chat ID: <code>{target['chat_id']}</code>\n\n"
        "If you see this, your Telegram integration is working!"
    )
    try:
        status, ok, error = await send_telegram(target["bot_token"], target["chat_id"], text, timeout=target.get("timeout_seconds", 10))
        if ok:
            return HTMLResponse('<span style="color:var(--green);font-weight:600">&#10003; Test sent!</span>')
        return HTMLResponse(f'<span style="color:var(--red)">{error}</span>')
    except Exception as e:
        return HTMLResponse(f'<span style="color:var(--red)">{e}</span>')

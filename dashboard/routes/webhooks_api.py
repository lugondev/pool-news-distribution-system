"""JSON API — Webhooks and Telegram Channels CRUD."""

import json
import logging
import time

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from dashboard.config_io import (
    get_telegram_channels,
    get_webhook_endpoints,
    read_settings,
    save_telegram_channels,
    save_webhook_endpoints,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Webhooks ─────────────────────────────────────────────────────────────────


class WebhookIn(BaseModel):
    id: str
    name: str
    url: str
    http_method: str = "POST"
    content_type: str = "application/json"
    retry_attempts: int = 3
    retry_delay_seconds: int = 5
    timeout_seconds: int = 10
    payload_mode: str = "full"
    payload_fields: list[str] = []
    payload_template: str = ""
    filter_categories_mode: str = "all"
    filter_categories: list[str] = []
    filter_sources_mode: str = "all"
    filter_sources: list[str] = []
    filter_article_types_mode: str = "all"
    filter_article_types: list[str] = []
    ai_mode: str = "rewrite"
    ai_config_id: str = ""
    target_language: str = ""
    rate_limit_max: int = 0
    rate_limit_window_minutes: int = 60
    rate_limit_min_gap_seconds: int = 0


class WebhookUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    http_method: str | None = None
    content_type: str | None = None
    retry_attempts: int | None = None
    retry_delay_seconds: int | None = None
    timeout_seconds: int | None = None
    payload_mode: str | None = None
    payload_fields: list[str] | None = None
    payload_template: str | None = None
    filter_categories_mode: str | None = None
    filter_categories: list[str] | None = None
    filter_sources_mode: str | None = None
    filter_sources: list[str] | None = None
    filter_article_types_mode: str | None = None
    filter_article_types: list[str] | None = None
    ai_mode: str | None = None
    ai_config_id: str | None = None
    target_language: str | None = None
    rate_limit_max: int | None = None
    rate_limit_window_minutes: int | None = None
    rate_limit_min_gap_seconds: int | None = None


@router.get("/webhooks")
async def list_webhooks():
    return {"endpoints": get_webhook_endpoints()}


@router.post("/webhooks", status_code=201)
async def add_webhook(body: WebhookIn):
    endpoints = get_webhook_endpoints()
    if any(ep["id"] == body.id for ep in endpoints):
        raise HTTPException(409, f"Webhook '{body.id}' already exists")
    ep = {
        "id": body.id,
        "name": body.name,
        "url": body.url,
        "enabled": True,
        "http_method": body.http_method.upper(),
        "content_type": body.content_type or "application/json",
        "retry_attempts": max(1, min(body.retry_attempts, 10)),
        "retry_delay_seconds": max(1, min(body.retry_delay_seconds, 60)),
        "timeout_seconds": max(1, min(body.timeout_seconds, 60)),
        "payload_mode": body.payload_mode,
        "payload_fields": body.payload_fields,
        "payload_template": body.payload_template,
        "filter_categories_mode": body.filter_categories_mode,
        "filter_categories": body.filter_categories,
        "filter_sources_mode": body.filter_sources_mode,
        "filter_sources": body.filter_sources,
        "filter_article_types_mode": body.filter_article_types_mode,
        "filter_article_types": body.filter_article_types,
        "ai_mode": body.ai_mode if body.ai_mode in ("rewrite", "synthetic", "off") else "rewrite",
        "ai_config_id": body.ai_config_id.strip(),
        "target_language": body.target_language.strip(),
        "rate_limit_max": max(0, body.rate_limit_max),
        "rate_limit_window_minutes": max(1, body.rate_limit_window_minutes),
        "rate_limit_min_gap_seconds": max(0, body.rate_limit_min_gap_seconds),
    }
    endpoints.append(ep)
    save_webhook_endpoints(endpoints)
    logger.info(f"API: webhook added: {body.id}")
    return {"ok": True, "endpoint": ep}


@router.put("/webhooks/{wh_id}")
async def update_webhook(wh_id: str, body: WebhookUpdate):
    endpoints = get_webhook_endpoints()
    target = next((ep for ep in endpoints if ep["id"] == wh_id), None)
    if not target:
        raise HTTPException(404, "Webhook not found")
    for field in (
        "name", "url", "http_method", "content_type", "retry_attempts",
        "retry_delay_seconds", "timeout_seconds", "payload_mode", "payload_fields",
        "payload_template", "filter_categories_mode", "filter_categories",
        "filter_sources_mode", "filter_sources", "filter_article_types_mode",
        "filter_article_types", "ai_mode", "ai_config_id", "target_language",
        "rate_limit_max", "rate_limit_window_minutes", "rate_limit_min_gap_seconds",
    ):
        val = getattr(body, field, None)
        if val is not None:
            if field == "http_method":
                target[field] = val.upper()
            elif field == "ai_mode":
                target[field] = val if val in ("rewrite", "synthetic", "off") else "rewrite"
            else:
                target[field] = val
    save_webhook_endpoints(endpoints)
    logger.info(f"API: webhook updated: {wh_id}")
    return {"ok": True, "endpoint": target}


@router.post("/webhooks/{wh_id}/toggle")
async def toggle_webhook(wh_id: str):
    endpoints = get_webhook_endpoints()
    target = next((ep for ep in endpoints if ep["id"] == wh_id), None)
    if not target:
        raise HTTPException(404, "Webhook not found")
    target["enabled"] = not target.get("enabled", True)
    save_webhook_endpoints(endpoints)
    return {"ok": True, "endpoint": target}


@router.delete("/webhooks/{wh_id}")
async def delete_webhook(wh_id: str):
    endpoints = get_webhook_endpoints()
    new = [ep for ep in endpoints if ep["id"] != wh_id]
    if len(new) == len(endpoints):
        raise HTTPException(404, "Webhook not found")
    save_webhook_endpoints(new)
    logger.info(f"API: webhook deleted: {wh_id}")
    return {"ok": True}


@router.post("/webhooks/{wh_id}/test")
async def test_webhook(wh_id: str):
    """Send a real HTTP request with mock data and return detailed result."""
    from webhook.filters import passes_filter
    from webhook.payload import build_payload

    endpoints = get_webhook_endpoints()
    target = next((ep for ep in endpoints if ep["id"] == wh_id), None)
    if not target:
        raise HTTPException(404, "Webhook not found")

    mock_article = {
        "id": "test_" + str(int(time.time())),
        "source_id": "bbc-world",
        "source_name": "BBC World News",
        "url": "https://www.bbc.com/news/world-test-article-12345",
        "title": "Global Leaders Meet to Discuss Climate Change Action Plan",
        "summary": "World leaders gathered today at the International Summit.",
        "content": "In a historic gathering, representatives from over 150 countries convened.",
        "lang": "en",
        "declared_lang": "en",
        "category": "world",
        "published_at": "2026-03-24T10:30:00+00:00",
        "fetched_at": "2026-03-24T10:35:00+00:00",
        "ai_summary_vi": "Các nhà lãnh đạo thế giới đã họp tại Hội nghị Quốc tế.",
        "ai_summary_en": "World leaders convened at the International Summit.",
        "ai_status": "completed",
    }

    url = target.get("url", "")
    if not url:
        raise HTTPException(400, "Webhook URL is empty")

    if not passes_filter(mock_article, target):
        return {
            "ok": False,
            "message": "✗ Test article filtered out by webhook rules",
            "method": target.get("http_method", "POST"),
            "url": url,
            "payload_mode": target.get("payload_mode", "full"),
        }

    payload = build_payload(mock_article, target)
    method = target.get("http_method", "POST").upper()
    content_type = target.get("content_type", "application/json")
    timeout = target.get("timeout_seconds", 10)

    start_time = time.time()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            if method == "GET":
                params = {k: str(v) for k, v in payload.items()} if isinstance(payload, dict) else {"message": str(payload)}
                resp = await client.get(url, params=params)
            else:
                if isinstance(payload, str):
                    resp = await client.post(url, content=payload, headers={"Content-Type": content_type})
                else:
                    resp = await client.post(url, json=payload)

        elapsed_ms = int((time.time() - start_time) * 1000)
        success = resp.status_code < 400
        try:
            response_body = resp.text[:500]
        except Exception:
            response_body = "(binary data)"

        return {
            "ok": success,
            "elapsed_ms": elapsed_ms,
            "message": f"{'✓' if success else '✗'} Test completed in {elapsed_ms}ms",
            "method": method,
            "url": url,
            "content_type": content_type,
            "payload_mode": target.get("payload_mode", "full"),
            "payload": payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)[:500],
            "status_code": resp.status_code,
            "response_body": response_body,
        }

    except Exception as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        return {
            "ok": False,
            "elapsed_ms": elapsed_ms,
            "message": f"✗ Test failed: {str(e)}",
            "method": method,
            "url": url,
            "payload_mode": target.get("payload_mode", "full"),
            "error": str(e),
        }


# ── Telegram Channels ────────────────────────────────────────────────────────


class TelegramChannelIn(BaseModel):
    id: str
    name: str
    bot_token: str
    chat_id: str
    lang: str = "both"
    retry_attempts: int = 3
    timeout_seconds: int = 10
    payload_mode: str = "full"
    payload_fields: list[str] = []
    payload_template: str = ""
    filter_categories_mode: str = "all"
    filter_categories: list[str] = []
    filter_sources_mode: str = "all"
    filter_sources: list[str] = []
    filter_article_types_mode: str = "all"
    filter_article_types: list[str] = []
    ai_mode: str = "rewrite"
    ai_config_id: str = ""
    target_language: str = ""
    rate_limit_max: int = 0
    rate_limit_window_minutes: int = 60
    rate_limit_min_gap_seconds: int = 0


class TelegramChannelUpdate(BaseModel):
    name: str | None = None
    bot_token: str | None = None
    chat_id: str | None = None
    lang: str | None = None
    retry_attempts: int | None = None
    timeout_seconds: int | None = None
    payload_mode: str | None = None
    payload_fields: list[str] | None = None
    payload_template: str | None = None
    filter_categories_mode: str | None = None
    filter_categories: list[str] | None = None
    filter_sources_mode: str | None = None
    filter_sources: list[str] | None = None
    filter_article_types_mode: str | None = None
    filter_article_types: list[str] | None = None
    ai_mode: str | None = None
    ai_config_id: str | None = None
    target_language: str | None = None
    rate_limit_max: int | None = None
    rate_limit_window_minutes: int | None = None
    rate_limit_min_gap_seconds: int | None = None


@router.get("/telegram")
async def list_telegram_channels():
    return {"channels": get_telegram_channels()}


@router.post("/telegram", status_code=201)
async def add_telegram_channel(body: TelegramChannelIn):
    channels = get_telegram_channels()
    if any(ch["id"] == body.id for ch in channels):
        raise HTTPException(409, f"Telegram channel '{body.id}' already exists")
    ch = {
        "id": body.id,
        "name": body.name,
        "bot_token": body.bot_token,
        "chat_id": body.chat_id,
        "lang": body.lang,
        "enabled": True,
        "retry_attempts": max(1, min(body.retry_attempts, 10)),
        "timeout_seconds": max(1, min(body.timeout_seconds, 60)),
        "payload_mode": body.payload_mode,
        "payload_fields": body.payload_fields,
        "payload_template": body.payload_template,
        "filter_categories_mode": body.filter_categories_mode,
        "filter_categories": body.filter_categories,
        "filter_sources_mode": body.filter_sources_mode,
        "filter_sources": body.filter_sources,
        "filter_article_types_mode": body.filter_article_types_mode,
        "filter_article_types": body.filter_article_types,
        "ai_mode": body.ai_mode if body.ai_mode in ("rewrite", "synthetic", "off") else "rewrite",
        "ai_config_id": body.ai_config_id.strip(),
        "target_language": body.target_language.strip(),
        "rate_limit_max": max(0, body.rate_limit_max),
        "rate_limit_window_minutes": max(1, body.rate_limit_window_minutes),
        "rate_limit_min_gap_seconds": max(0, body.rate_limit_min_gap_seconds),
    }
    channels.append(ch)
    save_telegram_channels(channels)
    logger.info(f"API: telegram channel added: {body.id}")
    return {"ok": True, "channel": ch}


@router.put("/telegram/{ch_id}")
async def update_telegram_channel(ch_id: str, body: TelegramChannelUpdate):
    channels = get_telegram_channels()
    target = next((ch for ch in channels if ch["id"] == ch_id), None)
    if not target:
        raise HTTPException(404, "Telegram channel not found")
    for field in (
        "name", "bot_token", "chat_id", "lang", "retry_attempts", "timeout_seconds",
        "payload_mode", "payload_fields", "payload_template", "filter_categories_mode",
        "filter_categories", "filter_sources_mode", "filter_sources",
        "filter_article_types_mode", "filter_article_types", "ai_mode", "ai_config_id",
        "target_language", "rate_limit_max", "rate_limit_window_minutes",
        "rate_limit_min_gap_seconds",
    ):
        val = getattr(body, field, None)
        if val is not None:
            if field == "ai_mode":
                target[field] = val if val in ("rewrite", "synthetic", "off") else "rewrite"
            else:
                target[field] = val
    save_telegram_channels(channels)
    logger.info(f"API: telegram channel updated: {ch_id}")
    return {"ok": True, "channel": target}


@router.post("/telegram/{ch_id}/toggle")
async def toggle_telegram_channel(ch_id: str):
    channels = get_telegram_channels()
    target = next((ch for ch in channels if ch["id"] == ch_id), None)
    if not target:
        raise HTTPException(404, "Telegram channel not found")
    target["enabled"] = not target.get("enabled", True)
    save_telegram_channels(channels)
    return {"ok": True, "channel": target}


@router.delete("/telegram/{ch_id}")
async def delete_telegram_channel(ch_id: str):
    channels = get_telegram_channels()
    new = [ch for ch in channels if ch["id"] != ch_id]
    if len(new) == len(channels):
        raise HTTPException(404, "Telegram channel not found")
    save_telegram_channels(new)
    logger.info(f"API: telegram channel deleted: {ch_id}")
    return {"ok": True}


@router.post("/telegram/{ch_id}/test")
async def test_telegram_channel(ch_id: str):
    from webhook.telegram import send_telegram

    channels = get_telegram_channels()
    target = next((ch for ch in channels if ch["id"] == ch_id), None)
    if not target:
        raise HTTPException(404, "Telegram channel not found")

    text = (
        "\u2705 <b>News Aggregator — Test Message</b>\n\n"
        f"Channel: <i>{target['name']}</i>\n"
        f"Chat ID: <code>{target['chat_id']}</code>\n\n"
        "If you see this, your Telegram integration is working!"
    )
    status, ok, error = await send_telegram(
        target["bot_token"],
        target["chat_id"],
        text,
        timeout=target.get("timeout_seconds", 10),
    )
    if ok:
        return {"ok": True, "message": "Test message sent successfully"}
    raise HTTPException(400, f"Telegram API error: {error}")

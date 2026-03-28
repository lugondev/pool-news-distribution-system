"""
Unified dispatcher: webhook POST + Telegram channels.
Each endpoint/channel has its own payload_mode config.

Dispatch is decoupled from the AI loop via an asyncio.Queue.
Call enqueue_dispatch() to add a job; start dispatch_worker() in app lifespan.
"""

import asyncio
import json
import logging
import time

import httpx

from storage.sqlite_stats import log_webhook

try:
    from realtime.manager import ws_manager
except ImportError:
    ws_manager = None
from webhook.filters import check_rate_limit, passes_filter
from webhook.payload import build_payload
from webhook.telegram import dispatch_to_telegram
from webhook.twitter import dispatch_to_twitter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dispatch queue — bounded so a stuck webhook doesn't grow memory unboundedly
# ---------------------------------------------------------------------------
_dispatch_queue: asyncio.Queue = asyncio.Queue(maxsize=2000)


async def enqueue_dispatch(
    article: dict,
    endpoints: list[dict],
    telegram_channels: list[dict] | None = None,
    twitter_accounts: list[dict] | None = None,
) -> None:
    """Put a dispatch job in the queue (non-blocking; drops if full)."""
    try:
        _dispatch_queue.put_nowait((
            article, endpoints,
            telegram_channels or [],
            twitter_accounts or [],
        ))
    except asyncio.QueueFull:
        logger.warning(
            f"Dispatch queue full, dropping article {article.get('id', '?')}"
        )


async def dispatch_worker(rate_limit_seconds: float = 0.3) -> None:
    """
    Background worker: drain the dispatch queue at a controlled rate.
    rate_limit_seconds — minimum pause between successive dispatches.
    Start once in app lifespan; cancel on shutdown.
    """
    logger.info("Dispatch worker started")
    while True:
        try:
            article, endpoints, channels, tw_accounts = await _dispatch_queue.get()
            try:
                await dispatch_article(article, endpoints, channels, tw_accounts)
            except Exception as e:
                logger.error(
                    f"Dispatch worker unhandled error for {article.get('id', '?')}: {e}"
                )
            finally:
                _dispatch_queue.task_done()
            await asyncio.sleep(rate_limit_seconds)
        except asyncio.CancelledError:
            break
    logger.info("Dispatch worker stopped")


async def _send_webhook(
    url: str,
    payload: dict | str,
    method: str = "POST",
    timeout: int = 10,
    content_type: str = "application/json",
) -> tuple[int, bool]:
    """Send payload to URL via POST or GET.

    POST: sends JSON body (dict) or raw text body (str).
    GET:  sends payload as query params (dict) or as ?message=... (str).

    Args:
        content_type: Content-Type header for POST requests (ignored for GET)
    """
    async with httpx.AsyncClient(timeout=timeout) as client:
        if method.upper() == "GET":
            if isinstance(payload, dict):
                # flatten values to strings for query params
                params = {k: str(v) for k, v in payload.items()}
            else:
                params = {"message": payload}
            resp = await client.get(url, params=params)
        else:
            if isinstance(payload, str):
                resp = await client.post(
                    url,
                    content=payload,
                    headers={"Content-Type": content_type},
                )
            else:
                resp = await client.post(url, json=payload)
        return resp.status_code, resp.status_code < 400


async def dispatch_article(
    article: dict,
    endpoints: list[dict],
    telegram_channels: list[dict] | None = None,
    twitter_accounts: list[dict] | None = None,
) -> None:
    """Dispatch 1 article to all enabled webhook endpoints + Telegram + Twitter."""
    for ep in endpoints:
        if not ep.get("enabled", True):
            continue
        url = ep.get("url", "")
        if not url:
            continue
        if not passes_filter(article, ep):
            logger.debug(
                f"Webhook {ep.get('id')} filtered out article {article.get('id', '?')} (category/source filter)"
            )
            continue
        if not check_rate_limit(ep.get("id", url), ep):
            continue

        method = ep.get("http_method", "POST").upper()
        content_type = ep.get("content_type", "application/json")
        payload = build_payload(article, ep)
        # For POST JSON mode, try to parse template strings back to dict.
        # For GET or explicit raw POST (template mode), keep as string.
        if method == "POST" and ep.get("payload_mode", "full") != "template":
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except (json.JSONDecodeError, TypeError):
                    pass

        await _dispatch_to_url(
            article_id=article["id"],
            webhook_id=ep.get("id", ""),
            url=url,
            payload=payload,
            method=method,
            content_type=content_type,
            timeout=ep.get("timeout_seconds", 10),
            retry_attempts=ep.get("retry_attempts", 3),
            retry_delay_seconds=ep.get("retry_delay_seconds", 5),
            article_title=article.get("title", ""),
        )

    if telegram_channels:
        await dispatch_to_telegram(article, telegram_channels)

    if twitter_accounts:
        await dispatch_to_twitter(article, twitter_accounts)


async def _dispatch_to_url(
    article_id: str,
    webhook_id: str,
    url: str,
    payload: dict | str,
    method: str,
    content_type: str,
    timeout: int,
    retry_attempts: int,
    retry_delay_seconds: int = 5,
    article_title: str = "",
) -> None:
    webhook_name = webhook_id or url
    if ws_manager:
        asyncio.create_task(ws_manager.emit_webhook_start(article_id, article_title, webhook_name))

    last_error = None
    t0 = time.monotonic()
    for attempt in range(retry_attempts):
        if attempt > 0:
            await asyncio.sleep(retry_delay_seconds)
        try:
            status_code, success = await _send_webhook(
                url, payload, method, timeout, content_type
            )
            await log_webhook(
                article_id, url, status_code, success, webhook_id=webhook_id
            )
            if success:
                logger.info(
                    f"Webhook {method} OK [{status_code}] → {url} (article {article_id})"
                )
                duration_ms = int((time.monotonic() - t0) * 1000)
                if ws_manager:
                    asyncio.create_task(ws_manager.emit_webhook_success(
                        article_id, article_title, webhook_name, status_code, duration_ms
                    ))
                return
            else:
                logger.warning(
                    f"Webhook {method} failed [{status_code}] → {url}, attempt {attempt + 1}"
                )
        except Exception as e:
            last_error = str(e)
            logger.warning(
                f"Webhook {method} error → {url}, attempt {attempt + 1}: {e}"
            )

    await log_webhook(
        article_id, url, 0, False, error_msg=last_error, webhook_id=webhook_id
    )
    if ws_manager:
        asyncio.create_task(ws_manager.emit_webhook_error(
            article_id, article_title, webhook_name, last_error or "max retries exceeded"
        ))
    logger.error(f"Webhook {method} permanently failed → {url} (article {article_id})")

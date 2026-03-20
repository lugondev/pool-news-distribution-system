"""
Unified dispatcher: webhook POST + Telegram channels.
Each endpoint/channel has its own payload_mode config.
"""
import json
import logging

import httpx

from storage.sqlite_stats import log_webhook
from webhook.payload import build_payload
from webhook.telegram import dispatch_to_telegram

logger = logging.getLogger(__name__)


async def _post_webhook(url: str, payload: dict | str, timeout: int = 10) -> tuple[int, bool]:
    """POST payload to URL. Sends JSON dict or raw text string."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        if isinstance(payload, str):
            resp = await client.post(
                url, content=payload,
                headers={"Content-Type": "application/json"},
            )
        else:
            resp = await client.post(url, json=payload)
        return resp.status_code, resp.status_code < 400


async def dispatch_article(
    article: dict,
    endpoints: list[dict],
    telegram_channels: list[dict] | None = None,
) -> None:
    """Dispatch 1 article to all enabled webhook endpoints + Telegram channels."""
    for ep in endpoints:
        if not ep.get("enabled", True):
            continue
        url = ep.get("url", "")
        if not url:
            continue

        payload = build_payload(article, ep)
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (json.JSONDecodeError, TypeError):
                pass

        await _dispatch_to_url(
            article_id=article["id"],
            url=url,
            payload=payload,
            timeout=ep.get("timeout_seconds", 10),
            retry_attempts=ep.get("retry_attempts", 3),
        )

    if telegram_channels:
        await dispatch_to_telegram(article, telegram_channels)


async def _dispatch_to_url(
    article_id: str,
    url: str,
    payload: dict | str,
    timeout: int,
    retry_attempts: int,
) -> None:
    last_error = None
    for attempt in range(retry_attempts):
        try:
            status_code, success = await _post_webhook(url, payload, timeout)
            await log_webhook(article_id, url, status_code, success)
            if success:
                logger.info(f"Webhook OK [{status_code}] → {url} (article {article_id})")
                return
            else:
                logger.warning(f"Webhook failed [{status_code}] → {url}, attempt {attempt+1}")
        except Exception as e:
            last_error = str(e)
            logger.warning(f"Webhook error → {url}, attempt {attempt+1}: {e}")

    await log_webhook(article_id, url, 0, False, error_msg=last_error)
    logger.error(f"Webhook permanently failed → {url} (article {article_id})")

"""
HTTP POST webhook dispatcher.
Push article JSON đến các configured URLs với retry.
"""
import logging
from datetime import datetime, timezone

import httpx
from tenacity import retry, stop_after_attempt, wait_fixed

from storage.sqlite_stats import log_webhook

logger = logging.getLogger(__name__)


def _build_payload(article: dict) -> dict:
    return {
        "id": article.get("id"),
        "source_id": article.get("source_id"),
        "source_name": article.get("source_name"),
        "url": article.get("url"),
        "title": article.get("title"),
        "lang": article.get("lang"),
        "category": article.get("category"),
        "published_at": article.get("published_at"),
        "summary": {
            "vi": article.get("ai_summary_vi", ""),
            "en": article.get("ai_summary_en", ""),
        },
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }


async def _post_webhook(url: str, payload: dict, timeout: int = 10) -> tuple[int, bool]:
    """POST payload to URL. Returns (status_code, success)."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload)
        return resp.status_code, resp.status_code < 400


async def dispatch_article(
    article: dict,
    webhook_urls: list[str],
    timeout: int = 10,
    retry_attempts: int = 3,
) -> None:
    """Dispatch 1 article đến tất cả webhook URLs."""
    payload = _build_payload(article)

    for url in webhook_urls:
        await _dispatch_to_url(article["id"], url, payload, timeout, retry_attempts)


async def _dispatch_to_url(
    article_id: str,
    url: str,
    payload: dict,
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

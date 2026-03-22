"""
Telegram Bot API dispatcher.
Supports 3 payload modes: full (default formatted), fields (selected), template (Jinja2 custom).
"""
import asyncio
import logging

import httpx

from storage.sqlite_stats import log_telegram
from webhook.filters import check_rate_limit, passes_filter
from webhook.payload import build_payload

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

CATEGORY_EMOJI = {
    "finance": "\U0001f4b0", "world": "\U0001f30d", "tech": "\U0001f4bb",
    "business": "\U0001f4bc", "politics": "\U0001f3db", "science": "\U0001f52c",
    "ai": "\U0001f916", "gaming": "\U0001f3ae", "sports": "\U000026bd",
    "esports": "\U0001f3f9", "entertainment": "\U0001f3ac", "music": "\U0001f3b5",
}


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _default_format(article: dict, lang: str = "both") -> str:
    """Default Telegram HTML message (mode=full)."""
    title = article.get("title", "Untitled")
    url = article.get("url", "")
    source = article.get("source_name", "")
    category = article.get("category", "")
    emoji = CATEGORY_EMOJI.get(category, "\U0001f4f0")
    vi = article.get("ai_summary_vi", "")
    en = article.get("ai_summary_en", "")

    lines = [f"{emoji} <b>{_escape(title)}</b>"]
    if source:
        lines.append(f"\U0001f4e1 <i>{_escape(source)}</i>")
    lines.append("")

    if lang in ("vi", "both") and vi:
        lines.append(f"\U0001f1fb\U0001f1f3 {_escape(vi)}")
    if lang in ("en", "both") and en:
        lines.append(f"\U0001f1ec\U0001f1e7 {_escape(en)}")
    if not vi and not en:
        summary = article.get("summary", "")
        if summary:
            lines.append(_escape(summary[:300]))

    if url:
        lines.append(f"\n\U0001f517 <a href=\"{url}\">Read full article</a>")
    return "\n".join(lines)


def _fields_format(article: dict, fields: list[str]) -> str:
    """Build text from selected fields only — no auto-additions."""
    lines = []
    for f in fields:
        val = article.get(f, "")
        if val:
            label = f.replace("_", " ").title()
            lines.append(f"<b>{label}:</b> {_escape(str(val))}")
    return "\n".join(lines) if lines else "(empty)"


def build_telegram_text(article: dict, channel_config: dict) -> str:
    """Build Telegram message text based on channel's payload_mode."""
    mode = channel_config.get("payload_mode", "full")

    if mode == "template":
        result = build_payload(article, channel_config)
        return str(result) if result else _default_format(article, channel_config.get("lang", "both"))

    if mode == "fields":
        fields = channel_config.get("payload_fields", [])
        return _fields_format(article, fields)

    return _default_format(article, channel_config.get("lang", "both"))


async def send_telegram(
    token: str,
    chat_id: str,
    text: str,
    timeout: int = 10,
) -> tuple[int, bool, str | None]:
    url = TELEGRAM_API.format(token=token)
    payload = {
        "chat_id": chat_id,
        "text": text[:4096],
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload)
        ok = resp.status_code == 200
        error = None
        if not ok:
            try:
                body = resp.json()
                error = body.get("description", resp.text[:200])
            except Exception:
                error = resp.text[:200]
        return resp.status_code, ok, error


async def dispatch_to_telegram(article: dict, channels: list[dict], message_delay: float = 1.0) -> None:
    """Dispatch article to all enabled Telegram channels with delay between sends."""
    sent = 0
    for ch in channels:
        if not ch.get("enabled", True):
            continue
        token = ch.get("bot_token", "")
        chat_id = ch.get("chat_id", "")
        channel_id = ch.get("id", chat_id)
        if not token or not chat_id:
            continue
        if not passes_filter(article, ch):
            logger.debug(f"Telegram {channel_id} filtered out article {article.get('id','?')} (category/source filter)")
            continue
        if not check_rate_limit(channel_id, ch):
            continue

        text = build_telegram_text(article, ch)
        retry_attempts = ch.get("retry_attempts", 3)
        timeout = ch.get("timeout_seconds", 10)

        if sent > 0 and message_delay > 0:
            await asyncio.sleep(message_delay)

        retry_delay = ch.get("retry_delay_seconds", 5)
        last_error = None
        for attempt in range(retry_attempts):
            if attempt > 0:
                await asyncio.sleep(retry_delay)
            try:
                status, ok, error = await send_telegram(token, chat_id, text, timeout)
                if ok:
                    await log_telegram(article["id"], channel_id, chat_id, status, True)
                    logger.info(f"Telegram OK → {channel_id} ({chat_id}) article {article['id']}")
                    sent += 1
                    break
                last_error = error
                logger.warning(
                    f"Telegram failed [{status}] → {channel_id}, attempt {attempt + 1}: {error}"
                )
            except Exception as e:
                last_error = str(e)
                logger.warning(f"Telegram error → {channel_id}, attempt {attempt + 1}: {e}")
        else:
            await log_telegram(article["id"], channel_id, chat_id, 0, False, error_msg=last_error)
            logger.error(f"Telegram permanently failed → {channel_id} ({chat_id}) article {article['id']}")

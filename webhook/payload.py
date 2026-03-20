"""
Shared payload builder for webhook + Telegram.
3 modes: full (all fields), fields (pick specific), template (Jinja2 custom).
"""
import logging
from datetime import datetime, timezone

from jinja2 import BaseLoader, Environment, TemplateSyntaxError

logger = logging.getLogger(__name__)

ALL_FIELDS = [
    "id", "source_id", "source_name", "url", "title",
    "summary", "content", "lang", "declared_lang",
    "category", "published_at", "fetched_at",
    "ai_summary_vi", "ai_summary_en", "ai_status",
]

_jinja_env = Environment(loader=BaseLoader(), autoescape=False)


def build_full_payload(article: dict) -> dict:
    """Mode 'full': return all article data + sent_at."""
    return {
        "id": article.get("id"),
        "source_id": article.get("source_id"),
        "source_name": article.get("source_name"),
        "url": article.get("url"),
        "title": article.get("title"),
        "summary": article.get("summary", ""),
        "content": article.get("content", ""),
        "lang": article.get("lang"),
        "declared_lang": article.get("declared_lang", ""),
        "category": article.get("category"),
        "published_at": article.get("published_at"),
        "fetched_at": article.get("fetched_at", ""),
        "ai_summary_vi": article.get("ai_summary_vi", ""),
        "ai_summary_en": article.get("ai_summary_en", ""),
        "ai_status": article.get("ai_status", ""),
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }


def build_fields_payload(article: dict, fields: list[str]) -> dict:
    """Mode 'fields': return only selected fields + sent_at."""
    payload = {}
    for f in fields:
        if f in ALL_FIELDS:
            payload[f] = article.get(f, "")
    payload["sent_at"] = datetime.now(timezone.utc).isoformat()
    return payload


def render_template(template_str: str, article: dict) -> str:
    """Mode 'template': render Jinja2 template with article context."""
    try:
        tpl = _jinja_env.from_string(template_str)
        return tpl.render(
            id=article.get("id", ""),
            source_id=article.get("source_id", ""),
            source_name=article.get("source_name", ""),
            url=article.get("url", ""),
            title=article.get("title", ""),
            summary=article.get("summary", ""),
            content=article.get("content", ""),
            lang=article.get("lang", ""),
            declared_lang=article.get("declared_lang", ""),
            category=article.get("category", ""),
            published_at=article.get("published_at", ""),
            fetched_at=article.get("fetched_at", ""),
            ai_summary_vi=article.get("ai_summary_vi", ""),
            ai_summary_en=article.get("ai_summary_en", ""),
            ai_status=article.get("ai_status", ""),
            sent_at=datetime.now(timezone.utc).isoformat(),
            article=article,
        )
    except Exception as e:
        logger.warning(f"Template render failed: {e}")
        return f"[Template error: {e}]"


def build_payload(article: dict, config: dict) -> dict | str:
    """
    Build payload based on endpoint config.
    config keys: payload_mode ('full'|'fields'|'template'),
                 payload_fields (list[str]), payload_template (str).
    Returns dict for webhook JSON, str for telegram text.
    """
    mode = config.get("payload_mode", "full")

    if mode == "fields":
        fields = config.get("payload_fields", [])
        if not fields:
            return build_full_payload(article)
        return build_fields_payload(article, fields)

    if mode == "template":
        template_str = config.get("payload_template", "")
        if not template_str:
            return build_full_payload(article)
        return render_template(template_str, article)

    return build_full_payload(article)


def validate_template(template_str: str) -> tuple[bool, str]:
    """Validate a Jinja2 template string. Returns (ok, error_msg)."""
    try:
        _jinja_env.parse(template_str)
        return True, ""
    except TemplateSyntaxError as e:
        return False, str(e)

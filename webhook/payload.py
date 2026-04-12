"""
Shared payload builder for webhook + Telegram.
3 modes: full (all fields), fields (pick specific), template (Jinja2 custom).
"""

import logging
from datetime import datetime, timezone

from jinja2 import BaseLoader, Environment, TemplateSyntaxError

logger = logging.getLogger(__name__)

ALL_FIELDS = [
    # Common fields (both original and synthetic)
    "id",
    "type",
    "category",
    # Original article fields
    "source_id",
    "source_name",
    "url",
    "title",
    "summary",
    "content",
    "lang",
    "declared_lang",
    "published_at",
    "fetched_at",
    "ai_summary_vi",
    "ai_summary_en",
    "ai_summary_origin",
    "ai_summary_target",
    "ai_status",
    # Synthetic article fields
    "title_en",
    "title_vi",
    "content_en",
    "content_vi",
    "content_target",
    "title_target",
    "angle",
    "source_article_ids",
    "num_source_articles",
    "ai_model",
    "ai_tokens",
    "ai_analysis",
    "created_at",
]

_jinja_env = Environment(loader=BaseLoader(), autoescape=False)


def build_full_payload(article: dict) -> dict:
    """Mode 'full': return all known article fields + sent_at.
    Covers both original (rewrite/off) and synthetic article types.
    """
    payload = {f: article.get(f, "") for f in ALL_FIELDS}
    # Keep None for id/category so consumers can detect missing values
    payload["id"] = article.get("id")
    payload["category"] = article.get("category")
    payload["sent_at"] = datetime.now(timezone.utc).isoformat()
    return payload


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
        # Pass all article fields plus common convenience variables
        context = dict(article)  # Start with all article data
        context.update(
            {
                # Add convenience fields for backward compatibility
                "sent_at": datetime.now(timezone.utc).isoformat(),
                "article": article,  # Full article dict for advanced templates
            }
        )
        return tpl.render(**context)
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

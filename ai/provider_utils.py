"""
Provider detection and response_format helpers.

Cloudflare Workers AI only accepts response_format={"type": "json_schema", ...}
while OpenAI-compatible APIs accept {"type": "json_object"}.

Cloudflare also returns message.content as a pre-parsed dict (not a JSON string)
when json_schema mode is active — use parse_ai_json() instead of json.loads() directly.
"""

import json


def is_cloudflare(base_url: str | None) -> bool:
    return bool(base_url and "cloudflare.com" in base_url)


def build_response_format(base_url: str | None, name: str, schema: dict) -> dict:
    """Return the correct response_format dict for the detected provider.

    - Non-Cloudflare → {"type": "json_object"}
    - Cloudflare     → {"type": "json_schema", "json_schema": {"name": ..., "schema": ...}}
    """
    if is_cloudflare(base_url):
        return {
            "type": "json_schema",
            "json_schema": {"name": name, "schema": schema},
        }
    return {"type": "json_object"}


def parse_ai_json(content: str | dict | None, fallback: dict | None = None) -> dict:
    """Parse AI response content into a dict, handling Cloudflare's pre-parsed dicts.

    Cloudflare Workers AI with json_schema mode returns message.content as a Python
    dict (already parsed). Other providers return a JSON string. This function handles
    both cases, plus markdown code-fence stripping for models that add them.
    """
    if content is None:
        return fallback if fallback is not None else {}
    if isinstance(content, dict):
        return content
    # Must be a string — strip markdown fences then parse
    raw = content.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return json.loads(raw)


# ── Reusable schemas ──────────────────────────────────────────────────────────

# rewriter.py — language-keyed summaries, e.g. {"en": "...", "vi": "..."}
SCHEMA_LANG_SUMMARY = {
    "type": "object",
    "additionalProperties": {"type": "string"},
}

# rewriter.py — test_ai_connection: fixed vi/en keys so the model can't rename them
SCHEMA_TEST_SUMMARY = {
    "type": "object",
    "properties": {
        "vi": {"type": "string"},
        "en": {"type": "string"},
    },
    "required": ["vi", "en"],
}

# enricher.py — NER + sentiment
SCHEMA_ENRICHMENT = {
    "type": "object",
    "properties": {
        "entities": {"type": "array", "items": {"type": "string"}},
        "sentiment": {"type": "string"},
    },
}

# newsletter.py
SCHEMA_NEWSLETTER = {
    "type": "object",
    "properties": {
        "subject": {"type": "string"},
        "intro": {"type": "string"},
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {"type": "string"},
                    "headline": {"type": "string"},
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "summary": {"type": "string"},
                                "url": {"type": "string"},
                                "source": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
        "closing": {"type": "string"},
    },
}

# topic_synthesis.py
SCHEMA_TOPIC_SYNTHESIS = {
    "type": "object",
    "properties": {
        "analysis": {"type": "string"},
        "num_summaries": {"type": "integer"},
        "summaries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "angle": {"type": "string"},
                    "title_vi": {"type": "string"},
                    "content_vi": {"type": "string"},
                    "title_en": {"type": "string"},
                    "content_en": {"type": "string"},
                },
            },
        },
    },
}

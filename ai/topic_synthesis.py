"""
Topic-based news synthesis: AI automatically decides how many summaries to generate.

Instead of translating articles one-by-one, this module:
1. Groups articles by category (e.g., 5 politics articles)
2. AI analyzes content diversity
3. AI generates 1-8 synthetic articles with different angles
4. Each synthetic article is saved to Redis and dispatched to webhooks
"""

import asyncio
import json
import logging
import hashlib
from datetime import datetime, timezone

from openai import AsyncOpenAI, RateLimitError
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_not_exception_type,
)

import redis.asyncio as aioredis
from ai.rewriter import (
    get_openai_client,
    TONE_PROMPTS,
    _load_ai_config,
    LANG_NAMES,
    AUTO_LANG,
    is_auto_lang,
    audience_pick_instruction,
)
from storage.config_cache import cached_yaml  # noqa: F401 — available for future use
from ai.provider_utils import (
    build_response_format,
    parse_ai_json,
    SCHEMA_TOPIC_SYNTHESIS,
)
from webhook.dispatcher import enqueue_dispatch

logger = logging.getLogger(__name__)


def _build_synth_lang_spec(
    target_languages: list[str] | str | None,
    length_guidance: str,
) -> tuple[list[str], str, bool]:
    """Build output language list and JSON fields spec for the synthesis prompt.

    Returns (output_languages, output_fields_spec, auto_mode).
    output_languages: e.g. ["en"] or ["en", "zh", "vi"]; in auto_mode it's just ["en"]
        because the chosen target lang is decided at runtime by the AI.
    output_fields_spec: indented field lines to inject into the prompt JSON template
    auto_mode: True when caller asked for AI-picked target language ("auto").

    Args:
        target_languages: List of language codes, single string (backward compat), or "auto".
        length_guidance: Length instruction for content field
    """
    # Detect auto mode (string "auto" or list containing it)
    auto_mode = False
    if isinstance(target_languages, str) and is_auto_lang(target_languages):
        auto_mode = True
    elif isinstance(target_languages, list) and any(
        is_auto_lang(t) for t in target_languages
    ):
        auto_mode = True

    if auto_mode:
        # In auto mode the per-summary target fields are filled by AI in the
        # language it picks at the top-level `chosen_lang`. We always keep
        # English as the baseline so dispatchers without target_language set
        # still get readable content.
        lines = [
            '      "title_en": "English title (max 100 chars)",',
            f'      "content_en": "English summary (3-4 sentences, {length_guidance})",',
            '      "title_target": "title in the chosen language (max 100 chars)",',
            f'      "content_target": "summary in the chosen language (3-4 sentences, {length_guidance})",',
        ]
        return ["en"], "\n".join(lines), True

    # Normalize input to list (existing behaviour)
    if target_languages is None:
        langs = ["en"]
    elif isinstance(target_languages, str):
        # Backward compatibility: single string → list
        langs = ["en"]
        if target_languages.lower() != "en":
            langs.append(target_languages.lower())
    else:
        # Multi-language mode
        langs = ["en"] if "en" not in target_languages else []
        langs.extend(
            [lang.lower() for lang in target_languages if lang.lower() != "en"]
        )
        if not langs:
            langs = ["en"]  # Fallback

    lines = []
    for lang in langs:
        lang_name = LANG_NAMES.get(lang, lang.upper())
        lines.append(f'      "title_{lang}": "{lang_name} title (max 100 chars)",')
        lines.append(
            f'      "content_{lang}": "{lang_name} summary (3-4 sentences, {length_guidance})",'
        )  # noqa: E501

    return langs, "\n".join(lines), False


# AI prompt: Let AI decide output count based on content diversity.
# {output_fields_spec} is built dynamically per hook's target_language.
# {auto_target_block} is empty in fixed-language mode; in auto mode it adds an
# instruction telling the AI to choose one engaging target language for the batch.
TOPIC_SYNTHESIS_PROMPT = """{tone_instruction}

You are analyzing {count} news articles about {category} from the past few hours.
These articles span {time_span} and come from {num_sources} different sources.

Articles (JSON array):
{articles_json}

Your task:
1. Analyze how many DISTINCT narratives, perspectives, or angles exist in these articles
2. Generate between 1 and 8 summaries, where EACH summary must:
   - Cover a unique angle that is NOT covered by other summaries
   - Provide standalone value (not generic rehash)
   - Be 3-4 complete sentences in each required language (MINIMUM 50 characters per language)
   - Include a clear, descriptive title
{auto_target_block}
Decision guidelines:
- If articles are very similar or redundant → generate FEWER summaries (minimum 1)
- If articles cover multiple sub-topics or perspectives → generate MORE summaries (maximum 8)
- If there's a clear timeline/progression → include a "timeline" angle
- If there are conflicting viewpoints → include "perspective A" and "perspective B" angles
- If there's significant impact/implications → include an "impact" or "analysis" angle

Output format (JSON only, no other text):
{{
  "analysis": "Brief explanation of what angles you identified and why you chose this number of summaries",
  "num_summaries": <integer 1-8>,{auto_target_field}
  "summaries": [
    {{
{output_fields_spec}
      "angle": "timeline|analysis|comparison|impact|perspective|summary"
    }},
    ...
  ]
}}

CRITICAL LENGTH REQUIREMENTS:
- Each content field MUST be {length_guidance}
- Each content field MUST contain at least 3-4 complete sentences
- MINIMUM 50 characters per language field - summaries shorter than this will be rejected
- Do NOT use fragments, bullet points, or incomplete sentences
- Write full, coherent summaries with proper context and detail
"""


def _auto_target_blocks() -> tuple[str, str]:
    """Return (instruction_block, top_level_field_line) for auto-target mode."""
    instr = (
        "\n3. " + audience_pick_instruction() + "\n\n"
        "Output the chosen ISO code (lowercase) at the top level under `chosen_lang`. "
        "Then write `title_target` and `content_target` for every summary in that "
        "language. Keep `title_en`/`content_en` as the English baseline.\n"
    )
    field = '\n  "chosen_lang": "<one language code from the allowed list>",'
    return instr, field


def _generate_synthetic_id(category: str, articles: list[dict]) -> str:
    """Generate deterministic ID based on category + sorted source article IDs.
    Same source articles always produce the same base ID, enabling idempotency checks."""
    article_ids = sorted([a["id"] for a in articles])
    fingerprint = f"{category}:{','.join(article_ids)}"
    return f"synth_{hashlib.sha256(fingerprint.encode()).hexdigest()[:16]}"


def _calculate_time_span(articles: list[dict]) -> str:
    """Calculate time span of articles (e.g., '2 hours' or '1 day')."""
    timestamps = []
    for a in articles:
        try:
            ts_str = a.get("published_at") or a.get("fetched_at", "")
            if ts_str:
                dt = datetime.fromisoformat(ts_str)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                timestamps.append(dt)
        except (ValueError, TypeError):
            pass

    if len(timestamps) < 2:
        return "unknown timespan"

    oldest = min(timestamps)
    newest = max(timestamps)
    delta_sec = (newest - oldest).total_seconds()

    if delta_sec < 3600:
        return f"{int(delta_sec / 60)} minutes"
    elif delta_sec < 86400:
        return f"{int(delta_sec / 3600)} hours"
    else:
        return f"{int(delta_sec / 86400)} days"


def _count_unique_sources(articles: list[dict]) -> int:
    """Count number of unique source IDs."""
    return len(set(a.get("source_id", "") for a in articles if a.get("source_id")))


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_not_exception_type(RateLimitError),
)
async def synthesize_topic_articles(
    articles: list[dict],
    category: str,
    redis: aioredis.Redis,
    model: str | None = None,
    tone: str = "general",
    api_key: str | None = None,
    base_url: str | None = None,
    max_tokens: int = 2000,
    temperature: float = 0.5,
    target_language: str | None = None,  # Deprecated: use target_languages
    target_languages: list[str] | None = None,  # New: multi-language support
    prompt_system_override: str | None = None,
) -> list[dict]:
    """
    Analyze multiple articles from same category and generate 1-8 synthetic summaries.
    AI autonomously decides the output count based on content diversity.

    Args:
        target_language: (Deprecated) Single language code for backward compatibility
        target_languages: List of language codes (e.g. ["vi", "ja", "ko"])

    Returns: List of synthetic article dicts (length determined by AI).
    """
    if len(articles) < 3:
        logger.debug(
            f"Category {category}: only {len(articles)} articles, skipping synthesis"
        )
        return []

    # Load timeout from settings
    from dashboard.config_io import read_settings

    cfg = read_settings()
    timeout = cfg.get("channels_config", {}).get("ai_timeout_seconds", 60)

    # Load AI config to get actual base_url if not provided
    ai_cfg = _load_ai_config()
    resolved_base_url = base_url or ai_cfg.get("base_url", "https://api.openai.com/v1")
    resolved_model = model or ai_cfg.get("model", "")

    client = get_openai_client(
        api_key=api_key, base_url=resolved_base_url, timeout=timeout
    )

    # Build tone instruction — per-hook override takes priority, then global prompt_system, then tone
    custom_system = (ai_cfg.get("prompt_system") or "").strip()
    tone_instruction = (
        prompt_system_override.strip()
        if prompt_system_override and prompt_system_override.strip()
        else custom_system or TONE_PROMPTS.get(tone, TONE_PROMPTS["general"])
    )

    # Length guidance
    if ai_cfg.get("output_limit_enabled"):
        max_chars = int(ai_cfg.get("output_limit_chars") or 250)
        length_guidance = f"at most {max_chars} characters"
    else:
        length_guidance = "approximately 200-300 characters"

    # Build output language spec (en always present, plus target if configured)
    # Priority: target_languages (new) > target_language (deprecated)
    langs_input = target_languages if target_languages is not None else target_language
    output_languages, output_fields_spec, auto_mode = _build_synth_lang_spec(
        langs_input, length_guidance
    )
    auto_target_block, auto_target_field = (
        _auto_target_blocks() if auto_mode else ("", "")
    )

    # Prepare article metadata
    time_span = _calculate_time_span(articles)
    num_sources = _count_unique_sources(articles)

    # Build compact article representation (avoid token bloat)
    compact_articles = []
    for a in articles:
        compact_articles.append(
            {
                "id": a.get("id", ""),
                "source": a.get("source_name", ""),
                "title": a.get("title", ""),
                "summary": (a.get("summary") or a.get("content") or "")[
                    :500
                ],  # limit to 500 chars
                "published_at": a.get("published_at", ""),
            }
        )

    articles_json = json.dumps(compact_articles, ensure_ascii=False, indent=2)

    # Build prompt
    prompt = TOPIC_SYNTHESIS_PROMPT.format(
        tone_instruction=tone_instruction,
        category=category,
        count=len(articles),
        time_span=time_span,
        num_sources=num_sources,
        articles_json=articles_json,
        length_guidance=length_guidance,
        output_fields_spec=output_fields_spec,
        auto_target_block=auto_target_block,
        auto_target_field=auto_target_field,
    )

    lang_label = "+".join(output_languages)
    logger.info(
        f"Synthesizing {len(articles)} {category} articles "
        f"(span={time_span}, sources={num_sources}, langs={lang_label})"
    )

    # Call AI
    response = await client.chat.completions.create(
        model=resolved_model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        response_format=build_response_format(
            resolved_base_url, "topic_synthesis", SCHEMA_TOPIC_SYNTHESIS
        ),
        temperature=temperature,
    )

    content = response.choices[0].message.content if response.choices else None
    if not content:
        raise ValueError(f"Model returned empty content (model={resolved_model})")

    result = parse_ai_json(content)
    tokens_used = response.usage.total_tokens if response.usage else 0

    # Validate response structure
    if "summaries" not in result:
        logger.warning(f"AI response missing 'summaries' key: {result}")
        return []

    summaries = result["summaries"]
    analysis = result.get("analysis", "No analysis provided")

    if not isinstance(summaries, list) or not (1 <= len(summaries) <= 8):
        logger.warning(f"AI returned invalid summary count: {len(summaries)}")
        return []

    # Auto mode: validate chosen_lang and expand title_target/content_target
    # into title_{chosen}/content_{chosen} so downstream code is uniform.
    auto_chosen_lang: str | None = None
    if auto_mode:
        raw_chosen = (result.get("chosen_lang") or "").strip().lower()
        cleaned = "".join(c for c in raw_chosen if c.isalpha())[:2]
        if cleaned not in LANG_NAMES:
            logger.warning(
                f"Auto-mode synthesis: invalid chosen_lang={raw_chosen!r}; "
                f"falling back to en-only output"
            )
            cleaned = "en"
        auto_chosen_lang = cleaned
        if auto_chosen_lang != "en":
            output_languages = ["en", auto_chosen_lang]
            for s in summaries:
                if isinstance(s, dict):
                    if s.get("title_target") and not s.get(f"title_{auto_chosen_lang}"):
                        s[f"title_{auto_chosen_lang}"] = s["title_target"]
                    if s.get("content_target") and not s.get(f"content_{auto_chosen_lang}"):
                        s[f"content_{auto_chosen_lang}"] = s["content_target"]

    logger.info(
        f"AI synthesis complete: {len(articles)} inputs → {len(summaries)} outputs "
        f"({tokens_used} tokens){' [auto→' + auto_chosen_lang + ']' if auto_chosen_lang else ''}. "
        f"Analysis: {analysis[:100]}..."
    )

    # Build synthetic article objects
    synthetic_articles = []
    base_synth_id = _generate_synthetic_id(category, articles)
    source_article_ids = [a["id"] for a in articles]

    # Target language for content_target alias.
    # In auto mode: always use AI-picked chosen_lang (including "en") so content_target
    # mirrors the chosen-language content — matching rewriter's effective_tgt_lang behavior.
    # In fixed mode: only set when target_language differs from the English baseline.
    if auto_chosen_lang:
        _target: str | None = auto_chosen_lang
    else:
        _target = (
            target_language.lower()
            if target_language
            and not is_auto_lang(target_language)
            and target_language.lower() != "en"
            else None
        )

    for idx, summary in enumerate(summaries):
        # Validate: all required language fields must be present and non-trivial
        if any(not summary.get(f"content_{lang}") for lang in output_languages):
            logger.warning(
                f"Skipping invalid summary #{idx}: missing content for langs={output_languages}"
            )
            continue

        # Check length for each language and provide detailed feedback
        too_short = []
        for lang in output_languages:
            content = summary.get(f"content_{lang}", "")
            content_len = len(content)
            if content_len < 50:
                too_short.append(f"{lang}={content_len}chars")

        if too_short:
            logger.warning(
                f"Skipping summary #{idx}: content too short ({', '.join(too_short)}, "
                f"minimum 50 chars required). Title: {summary.get('title_en', 'N/A')[:50]}"
            )
            continue

        synth_id = f"{base_synth_id}_{idx}"
        synth = {
            "id": synth_id,
            "type": "synthetic",
            "category": category,
            "angle": summary.get("angle", "summary"),
            "source_article_ids": source_article_ids,
            "num_source_articles": len(articles),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "ai_model": resolved_model,
            "ai_tokens": tokens_used // len(summaries),  # approximate per-summary cost
            "ai_analysis": analysis if idx == 0 else "",  # only store once
        }
        # Dynamic per-language fields: title_{lang} + content_{lang}
        for lang in output_languages:
            synth[f"title_{lang}"] = summary.get(f"title_{lang}", "")
            synth[f"content_{lang}"] = summary.get(f"content_{lang}", "")

        # Convenience aliases — mirror ai_summary_target pattern from rewrite mode:
        # content_target / title_target → target language content (empty if no target)
        synth["content_target"] = synth.get(f"content_{_target}", "") if _target else ""
        synth["title_target"] = synth.get(f"title_{_target}", "") if _target else ""
        if auto_chosen_lang:
            # Surface the AI's pick so dashboards/dispatch know which language was used
            synth["ai_target_lang"] = auto_chosen_lang

        synthetic_articles.append(synth)

    # Log summary of acceptance vs rejection
    accepted_count = len(synthetic_articles)
    rejected_count = len(summaries) - accepted_count
    if rejected_count > 0:
        logger.warning(
            f"Synthesis quality check: {accepted_count}/{len(summaries)} summaries accepted, "
            f"{rejected_count} rejected (too short). Model: {resolved_model}"
        )

    return synthetic_articles


async def save_synthetic_article(redis: aioredis.Redis, synth: dict) -> None:
    """Save a synthetic article to Redis with appropriate indices."""
    key = f"news:{synth['id']}"
    ttl_seconds = 43200  # 12h (same as original articles)

    # Convert to hash format
    hash_data = {k: str(v) if not isinstance(v, str) else v for k, v in synth.items()}
    if "source_article_ids" in hash_data:
        hash_data["source_article_ids"] = json.dumps(hash_data["source_article_ids"])

    now_ts = datetime.now(timezone.utc).timestamp()

    pipe = redis.pipeline()

    # Save article hash
    pipe.hset(key, mapping=hash_data)
    pipe.expire(key, ttl_seconds)

    # Index in main feed
    pipe.zadd("news:feed", {synth["id"]: now_ts})
    pipe.expire("news:feed", ttl_seconds)

    # Index in category feed
    pipe.zadd(f"news:cat:{synth['category']}", {synth["id"]: now_ts})
    pipe.expire(f"news:cat:{synth['category']}", ttl_seconds)

    # Index in synthetic-specific feeds
    pipe.zadd("news:synth:feed", {synth["id"]: now_ts})
    pipe.expire("news:synth:feed", ttl_seconds)

    pipe.zadd(f"news:synth:cat:{synth['category']}", {synth["id"]: now_ts})
    pipe.expire(f"news:synth:cat:{synth['category']}", ttl_seconds)

    await pipe.execute()
    logger.debug(f"Saved synthetic article {synth['id']} (angle={synth['angle']})")


SYNTH_USED_KEY = "news:synth:used"
SYNTH_MAX_AGE_SECONDS = 39600  # 11h — avoid articles very close to their 12h TTL


async def get_recent_articles_by_category(
    redis: aioredis.Redis,
    category: str,
    limit: int = 10,
    exclude_synthetic: bool = True,
) -> list[dict]:
    """Get most recent articles from a specific category."""
    feed_key = f"news:cat:{category}"
    ids = await redis.zrevrange(feed_key, 0, limit - 1)

    if not ids:
        return []

    pipe = redis.pipeline()
    for aid in ids:
        pipe.hgetall(f"news:{aid.decode()}")
    results = await pipe.execute()

    articles = []
    for raw in results:
        if not raw:
            continue
        article = {k.decode(): v.decode() for k, v in raw.items()}
        # Skip synthetic articles if requested
        if exclude_synthetic and article.get("type") == "synthetic":
            continue
        articles.append(article)

    return articles


async def _get_unseen_articles(
    redis: aioredis.Redis,
    category: str,
    hook_id: str,
    max_articles: int,
) -> list[dict]:
    """Return real (non-synthetic) articles for the category that this hook has not yet synthesized.

    Uses three Redis sources in one pipeline:
    - news:cat:{category}        — all articles (real + synthetic), newest-first
    - news:synth:cat:{category}  — synthetic article IDs only (used to pre-filter)
    - news:synth:used:{hook_id}:{category} — IDs already consumed by this hook

    Fetches a large pool (max_articles × 15) so that after excluding synthetic and
    already-used IDs, enough real articles remain — even when synthetic articles
    dominate the recent end of the sorted set.
    """
    now_ts = datetime.now(timezone.utc).timestamp()
    cutoff = now_ts - SYNTH_MAX_AGE_SECONDS
    used_key = f"{SYNTH_USED_KEY}:{hook_id}:{category}"
    feed_key = f"news:cat:{category}"
    synth_cat_key = f"news:synth:cat:{category}"

    # One pipeline: candidates (newest first) + used IDs + synthetic IDs
    # synth_cat fetch is capped at the same pool size to avoid unbounded scans
    # (synthesis accumulates ~8 × interval ticks over the 11h window)
    pool = max_articles * 15
    pipe = redis.pipeline()
    pipe.zrevrangebyscore(feed_key, "+inf", cutoff, start=0, num=pool)
    pipe.zrange(used_key, 0, -1)
    pipe.zrevrangebyscore(synth_cat_key, "+inf", cutoff, start=0, num=pool)
    candidate_raw, used_raw, synth_raw = await pipe.execute()

    if not candidate_raw:
        return []

    def _d(m: bytes | str) -> str:
        return m.decode() if isinstance(m, bytes) else m

    used_ids = {_d(m) for m in used_raw}
    synth_ids = {_d(m) for m in synth_raw}
    unseen_ids = [
        rid
        for r in candidate_raw
        if (rid := _d(r)) not in used_ids and rid not in synth_ids
    ][:max_articles]

    if not unseen_ids:
        return []

    # Fetch article hashes in a second pipeline
    pipe2 = redis.pipeline()
    for aid in unseen_ids:
        pipe2.hgetall(f"news:{aid}")
    results = await pipe2.execute()

    articles = []
    for raw in results:
        if not raw:
            continue
        article = {k.decode(): v.decode() for k, v in raw.items()}
        articles.append(article)

    return articles


async def _mark_articles_used(
    redis: aioredis.Redis,
    category: str,
    hook_id: str,
    article_ids: list[str],
) -> None:
    """Record article IDs as used for this hook+category. Auto-prunes stale entries."""
    used_key = f"{SYNTH_USED_KEY}:{hook_id}:{category}"
    now_ts = datetime.now(timezone.utc).timestamp()
    cutoff = now_ts - SYNTH_MAX_AGE_SECONDS

    pipe = redis.pipeline()
    for aid in article_ids:
        pipe.zadd(used_key, {aid: now_ts})
    # Prune entries older than the max-age window
    pipe.zremrangebyscore(used_key, 0, cutoff)
    pipe.expire(
        used_key, SYNTH_MAX_AGE_SECONDS + 3600
    )  # slight buffer over article TTL
    await pipe.execute()


async def process_category_synthesis(
    redis: aioredis.Redis,
    category: str,
    hook_id: str,
    min_articles: int = 5,
    max_articles: int = 15,
    model: str | None = None,
    tone: str = "general",
    api_key: str | None = None,
    base_url: str | None = None,
    webhook_endpoints: list[dict] | None = None,
    telegram_channels: list[dict] | None = None,
    target_language: str | None = None,  # Deprecated: use target_languages
    target_languages: list[str] | None = None,  # New: multi-language support
    prompt_system_override: str | None = None,
) -> int:
    """
    Process one category for one hook: fetch unseen recent articles, synthesize,
    save, dispatch, and track used articles. Returns number of synthetic articles generated.

    Per-hook tracking (news:synth:used:{hook_id}:{category}) ensures the same
    source articles are never synthesized twice for the same hook, and that
    each hook independently accumulates fresh batches.

    Args:
        target_language: (Deprecated) Single language code for backward compatibility
        target_languages: List of language codes (e.g. ["vi", "ja", "ko"])
    """
    articles = await _get_unseen_articles(redis, category, hook_id, max_articles)

    if len(articles) < min_articles:
        logger.debug(
            f"Category {category} / hook {hook_id}: only {len(articles)} unseen articles "
            f"(min {min_articles} required), skipping"
        )
        return 0

    logger.info(
        f"Category {category} / hook {hook_id}: {len(articles)} unseen articles → synthesizing"
    )

    try:
        synthetics = await synthesize_topic_articles(
            articles=articles,
            category=category,
            redis=redis,
            model=model,
            tone=tone,
            api_key=api_key,
            base_url=base_url,
            target_language=target_language,  # Backward compat
            target_languages=target_languages,  # New param
            prompt_system_override=prompt_system_override,
        )
    except Exception as e:
        logger.error(f"Synthesis failed for category={category} hook={hook_id}: {e}")
        return 0

    if not synthetics:
        return 0

    # Mark source articles as used BEFORE dispatch so a crash doesn't cause re-synthesis
    await _mark_articles_used(redis, category, hook_id, [a["id"] for a in articles])

    for synth in synthetics:
        await save_synthetic_article(redis, synth)

        if webhook_endpoints or telegram_channels:
            await enqueue_dispatch(
                synth,
                webhook_endpoints or [],
                telegram_channels=telegram_channels,
            )
            logger.debug(
                f"Enqueued dispatch for synthetic article {synth['id']} "
                f"(category={category}, hook={hook_id}, angle={synth.get('angle', 'summary')})"
            )

    return len(synthetics)

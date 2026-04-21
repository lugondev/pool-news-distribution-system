"""JSON API — Content Channels (pull-based article delivery).

Instead of pushing articles via webhooks, channels expose REST endpoints
that external services poll to fetch new content on their own schedule.

Each channel has the same filter config as webhooks (categories, sources,
ai_mode, payload_mode) plus a cursor mechanism so consumers only receive
articles they haven't seen yet.

Redis keys:
  channel:{channel_id}:client:{client_id}:cursor    — float timestamp of last delivered article
  channel:{channel_id}:client:{client_id}:stats     — hash with pull_count, last_pull_at
  channel:{channel_id}:client:{client_id}:delivered  — set of delivered article IDs (for /next)
  channel:{channel_id}:styled:{article_id}:{format} — cached styled output (shared across clients)
"""

import hashlib
import logging
import secrets
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from dashboard.config_io import get_channels_config, get_content_channels, read_settings, save_content_channels
from dashboard.redis_state import get_redis
from storage.redis_store import get_articles_batch
from storage.sqlite_stats import log_channel_request
from webhook.filters import passes_filter
from webhook.payload import build_payload

logger = logging.getLogger(__name__)
router = APIRouter(tags=["channels"])

# ── Auth helper ──────────────────────────────────────────────────────────────


def _find_channel(channel_id: str) -> dict:
    channels = get_content_channels()
    ch = next((c for c in channels if c["id"] == channel_id), None)
    if not ch:
        raise HTTPException(404, "Channel not found")
    return ch


def _auth_channel(channel_id: str, api_key: str) -> tuple[dict, str]:
    """Authenticate channel request and return (channel_config, auth_method).
    
    auth_method: "per_channel_key", "global_key", or "public"
    """
    ch = _find_channel(channel_id)
    if not ch.get("enabled", True):
        raise HTTPException(403, "Channel is disabled")
    
    auth_method = "public"
    if ch.get("require_api_key", True):
        # Per-channel key takes priority, then global key
        channel_key = ch.get("api_key", "")
        global_key = get_channels_config().get("global_api_key", "")
        valid = False
        if api_key:
            if channel_key and secrets.compare_digest(channel_key, api_key):
                valid = True
                auth_method = "per_channel_key"
            elif global_key and secrets.compare_digest(global_key, api_key):
                valid = True
                auth_method = "global_key"
        if not valid:
            raise HTTPException(401, "Invalid API key")
    
    return ch, auth_method


# ── Models ───────────────────────────────────────────────────────────────────


class ChannelIn(BaseModel):
    """Create a new content channel."""
    id: str = Field(..., description="Unique slug identifier (e.g. 'twitter-bot')")
    name: str = Field(..., description="Human-readable display name")
    enabled: bool = Field(True, description="Whether the channel is active")
    require_api_key: bool = Field(True, description="Require X-API-Key header for feed/next access")
    # Filters
    filter_categories_mode: str = Field("all", description="Category filter: all | include | exclude")
    filter_categories: list[str] = Field([], description="Category IDs to include/exclude")
    filter_sources_mode: str = Field("all", description="Source filter: all | include | exclude")
    filter_sources: list[str] = Field([], description="Source IDs to include/exclude")
    filter_article_types_mode: str = Field("all", description="Article type filter: all | include | exclude")
    filter_article_types: list[str] = Field([], description="Article types: original, synthetic")
    ai_mode: str = Field("off", description="AI processing: off | rewrite | synthetic | debate")
    ai_config_id: str = Field("", description="AI config ID (empty = global settings)")
    target_language: str = Field("", description="Target language code (e.g. 'vi', 'ja')")
    # Payload
    payload_mode: str = Field("full", description="Payload format: full | fields | template")
    payload_fields: list[str] = Field([], description="Fields to include (payload_mode=fields)")
    payload_template: str = Field("", description="Jinja2 template (payload_mode=template)")
    max_items_per_fetch: int = Field(20, ge=1, le=100, description="Max articles per /feed call")
    # Style transform
    platform: str = Field("custom", description="Platform preset: twitter | facebook | blog | telegram | custom")
    content_mode: str = Field("rewrite", description="Content source: rewrite | synthetic | newsletter | long_article | debate")
    output_format: str = Field("summary", description="Output style: summary | thread | breaking | listicle | hot_take | deep_dive | quote_highlight | carousel")
    ai_source: str = Field("system", description="AI credentials: system (server config) | client (via X-AI-* headers)")
    style_source: str = Field("preset", description="Style config: preset (platform defaults) | custom (channel config) | client (style_prompt param)")
    style: dict = Field({}, description="Custom style config: {max_length, tone, include_hashtags, include_link, custom_prompt}")


class ChannelUpdate(BaseModel):
    name: str | None = Field(None, description="Human-readable display name")
    enabled: bool | None = Field(None, description="Whether the channel is active")
    require_api_key: bool | None = Field(None, description="Require X-API-Key header for feed/next access")
    filter_categories_mode: str | None = Field(None, description="Category filter: all | include | exclude")
    filter_categories: list[str] | None = Field(None, description="Category IDs to include/exclude")
    filter_sources_mode: str | None = Field(None, description="Source filter: all | include | exclude")
    filter_sources: list[str] | None = Field(None, description="Source IDs to include/exclude")
    filter_article_types_mode: str | None = Field(None, description="Article type filter: all | include | exclude")
    filter_article_types: list[str] | None = Field(None, description="Article types: original, synthetic")
    ai_mode: str | None = Field(None, description="AI processing: off | rewrite | synthetic | debate")
    ai_config_id: str | None = Field(None, description="AI config ID (empty = global settings)")
    target_language: str | None = Field(None, description="Target language code (e.g. 'vi', 'ja')")
    payload_mode: str | None = Field(None, description="Payload format: full | fields | template")
    payload_fields: list[str] | None = Field(None, description="Fields to include (payload_mode=fields)")
    payload_template: str | None = Field(None, description="Jinja2 template (payload_mode=template)")
    max_items_per_fetch: int | None = Field(None, description="Max articles per /feed call (1-100)")
    # Style transform (Phase 2)
    platform: str | None = Field(None, description="Platform preset: twitter | facebook | blog | telegram | custom")
    content_mode: str | None = Field(None, description="Content source: rewrite | synthetic | newsletter | long_article | debate")
    output_format: str | None = Field(None, description="Output style: summary | thread | breaking | listicle | hot_take | deep_dive | quote_highlight | carousel")
    ai_source: str | None = Field(None, description="AI credentials: system (server config) | client (via X-AI-* headers)")
    style_source: str | None = Field(None, description="Style config: preset | custom | client")
    style: dict | None = Field(None, description="Custom style config: {max_length, tone, include_hashtags, include_link, custom_prompt}")


class AckIn(BaseModel):
    cursor: str = Field(..., description="ISO 8601 timestamp to acknowledge up to")


# ── CRUD ─────────────────────────────────────────────────────────────────────


@router.get("/channels", summary="List all channels")
async def list_channels():
    """Return all configured content channels (API keys are masked)."""
    channels = get_content_channels()
    # Strip api_key from listing for security
    safe = []
    for ch in channels:
        c = {**ch}
        if "api_key" in c:
            c["api_key_preview"] = c["api_key"][:8] + "..." if len(c.get("api_key", "")) > 8 else "***"
            del c["api_key"]
        safe.append(c)
    return {"channels": safe}


@router.post("/channels", status_code=201, summary="Create a channel")
async def create_channel(body: ChannelIn):
    """Create a new content channel. Returns the channel config and generated API key."""
    channels = get_content_channels()
    if any(c["id"] == body.id for c in channels):
        raise HTTPException(409, f"Channel '{body.id}' already exists")

    api_key = secrets.token_urlsafe(32)
    ch = {
        "id": body.id,
        "name": body.name,
        "enabled": body.enabled,
        "api_key": api_key,
        "filter_categories_mode": body.filter_categories_mode,
        "filter_categories": body.filter_categories,
        "filter_sources_mode": body.filter_sources_mode,
        "filter_sources": body.filter_sources,
        "filter_article_types_mode": body.filter_article_types_mode,
        "filter_article_types": body.filter_article_types,
        "require_api_key": body.require_api_key,
        "ai_mode": body.ai_mode if body.ai_mode in ("off", "rewrite", "synthetic", "debate") else "off",
        "ai_config_id": body.ai_config_id.strip(),
        "target_language": body.target_language.strip(),
        "payload_mode": body.payload_mode,
        "payload_fields": body.payload_fields,
        "payload_template": body.payload_template,
        "max_items_per_fetch": max(1, min(body.max_items_per_fetch, 100)),
        "platform": body.platform if body.platform in ("twitter", "facebook", "blog", "telegram", "custom") else "custom",
        "content_mode": body.content_mode if body.content_mode in ("rewrite", "synthetic", "newsletter", "long_article", "debate") else "rewrite",
        "output_format": body.output_format if body.output_format in ("summary", "thread", "breaking", "listicle", "hot_take", "deep_dive", "quote_highlight", "carousel") else "summary",
        "ai_source": body.ai_source if body.ai_source in ("system", "client") else "system",
        "style_source": body.style_source if body.style_source in ("preset", "custom", "client") else "preset",
        "style": body.style or {},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    channels.append(ch)
    save_content_channels(channels)
    logger.info(f"Channel created: {body.id}")
    return {"ok": True, "channel": ch, "api_key": api_key}


@router.put("/channels/{ch_id}", summary="Update a channel")
async def update_channel(ch_id: str, body: ChannelUpdate):
    """Partially update channel configuration. Only provided fields are changed."""
    channels = get_content_channels()
    target = next((c for c in channels if c["id"] == ch_id), None)
    if not target:
        raise HTTPException(404, "Channel not found")

    for field in (
        "name", "enabled", "require_api_key",
        "filter_categories_mode", "filter_categories",
        "filter_sources_mode", "filter_sources", "filter_article_types_mode",
        "filter_article_types", "ai_mode", "ai_config_id", "target_language",
        "payload_mode", "payload_fields", "payload_template", "max_items_per_fetch",
        "platform", "content_mode", "output_format", "ai_source", "style_source", "style",
    ):
        val = getattr(body, field, None)
        if val is not None:
            if field == "ai_mode":
                target[field] = val if val in ("off", "rewrite", "synthetic", "debate") else "off"
            elif field == "platform":
                target[field] = val if val in ("twitter", "facebook", "blog", "telegram", "custom") else "custom"
            elif field == "content_mode":
                target[field] = val if val in ("rewrite", "synthetic", "newsletter", "long_article", "debate") else "rewrite"
            elif field == "output_format":
                target[field] = val if val in ("summary", "thread", "breaking", "listicle", "hot_take", "deep_dive", "quote_highlight", "carousel") else "summary"
            elif field == "ai_source":
                target[field] = val if val in ("system", "client") else "system"
            elif field == "style_source":
                target[field] = val if val in ("preset", "custom", "client") else "preset"
            elif field == "max_items_per_fetch":
                target[field] = max(1, min(val, 100))
            else:
                target[field] = val

    save_content_channels(channels)
    logger.info(f"Channel updated: {ch_id}")
    return {"ok": True, "channel": {k: v for k, v in target.items() if k != "api_key"}}


@router.post("/channels/{ch_id}/toggle", summary="Toggle channel on/off")
async def toggle_channel(ch_id: str):
    """Enable or disable a channel."""
    channels = get_content_channels()
    target = next((c for c in channels if c["id"] == ch_id), None)
    if not target:
        raise HTTPException(404, "Channel not found")
    target["enabled"] = not target.get("enabled", True)
    save_content_channels(channels)
    return {"ok": True, "enabled": target["enabled"]}


@router.post("/channels/{ch_id}/regenerate-key", summary="Regenerate API key")
async def regenerate_api_key(ch_id: str):
    """Generate a new API key for the channel. The old key stops working immediately."""
    channels = get_content_channels()
    target = next((c for c in channels if c["id"] == ch_id), None)
    if not target:
        raise HTTPException(404, "Channel not found")
    new_key = secrets.token_urlsafe(32)
    target["api_key"] = new_key
    save_content_channels(channels)
    logger.info(f"Channel API key regenerated: {ch_id}")
    return {"ok": True, "api_key": new_key}


@router.get("/channels/{ch_id}/clone-data", summary="Get channel data for cloning")
async def get_channel_clone_data(ch_id: str):
    """Return channel config (without id, api_key) for cloning into a new channel."""
    channels = get_content_channels()
    source = next((c for c in channels if c["id"] == ch_id), None)
    if not source:
        raise HTTPException(404, "Channel not found")
    
    # Clone all fields except id and api_key
    clone = {k: v for k, v in source.items() if k not in ("id", "api_key")}
    # Append " (Copy)" to name
    clone["name"] = f"{clone.get('name', 'Channel')} (Copy)"
    
    return {"ok": True, "data": clone}


@router.delete("/channels/{ch_id}", summary="Delete a channel")
async def delete_channel(ch_id: str):
    """Delete a channel and clean up its Redis state (cursor, stats, delivered set for all clients)."""
    channels = get_content_channels()
    new = [c for c in channels if c["id"] != ch_id]
    if len(new) == len(channels):
        raise HTTPException(404, "Channel not found")
    save_content_channels(new)
    # Clean up all client state for this channel
    redis = get_redis()
    keys = await redis.keys(f"channel:{ch_id}:client:*")
    if keys:
        await redis.delete(*keys)
    # Also clean styled cache
    styled_keys = await redis.keys(f"channel:{ch_id}:styled:*")
    if styled_keys:
        await redis.delete(*styled_keys)
    logger.info(f"Channel deleted: {ch_id}")
    return {"ok": True}


@router.get("/channels/{ch_id}/feed", summary="Pull articles from channel")
async def channel_feed(
    ch_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    since: str | None = Query(default=None, description="ISO timestamp cursor — only articles after this time"),
    auto_ack: bool = Query(default=False, description="Automatically advance cursor after fetch"),
    style_prompt: str | None = Query(default=None, description="Client-provided style prompt override"),
    x_api_key: str = Header(alias="X-API-Key", default=""),
    x_client_id: str = Header(alias="X-Client-ID", default=""),
    x_ai_api_key: str = Header(alias="X-AI-API-Key", default=""),
    x_ai_base_url: str = Header(alias="X-AI-Base-URL", default=""),
    x_ai_model: str = Header(alias="X-AI-Model", default=""),
):
    """
    Pull articles from this channel.

    Auth: Pass API key via `X-API-Key` header.

    Cursor logic:
    - If `since` is provided, use it as the cursor (ISO timestamp).
    - Otherwise, use the stored cursor from Redis (last ack position).
    - If no cursor exists, returns the latest articles.

    Set `auto_ack=true` to automatically advance the cursor to the newest
    article returned, so the next call only gets newer articles.

    Requires `X-Client-ID` header for per-client cursor/stats tracking.
    """
    start_time = time.time()
    
    if not x_client_id.strip():
        raise HTTPException(400, "X-Client-ID header is required")
    
    client_id = x_client_id.strip()
    ch, auth_method = _auth_channel(ch_id, x_api_key)
    redis = get_redis()

    try:
        max_items = min(limit, ch.get("max_items_per_fetch", 20))
        ai_mode = ch.get("ai_mode", "off")

        # Determine cursor timestamp
        if since:
            try:
                cursor_ts = datetime.fromisoformat(since).timestamp()
            except ValueError:
                raise HTTPException(400, "Invalid 'since' format — use ISO 8601")
        else:
            raw_cursor = await redis.get(f"channel:{ch_id}:client:{client_id}:cursor")
            cursor_ts = float(raw_cursor) if raw_cursor else 0.0

        # Always pull from news:feed (original articles) — on-demand processing
        feed_key = "news:feed"

        # Fetch article IDs newer than cursor (score > cursor_ts)
        # ZRANGEBYSCORE returns oldest-first; we want newest-first for the consumer
        # but we need to scan from cursor forward, so fetch then reverse.
        raw_ids = await redis.zrangebyscore(
            feed_key, f"({cursor_ts}" if cursor_ts > 0 else "-inf", "+inf",
            start=0, num=max_items * 3,  # over-fetch to account for filtering
        )

        if not raw_ids:
            # Track stats
            await _track_pull(redis, ch_id, client_id, 0)
            return {"articles": [], "count": 0, "cursor": since or "", "has_more": False}

        ids = [aid.decode() if isinstance(aid, bytes) else aid for aid in raw_ids]

        # Fetch article data
        articles_raw = await get_articles_batch(redis, ids)

        # Apply filters (category, source, article type)
        # Skip delivered articles
        delivered_key = f"channel:{ch_id}:client:{client_id}:delivered"
        delivered_ids = await redis.smembers(delivered_key)
        delivered_set = {d.decode() if isinstance(d, bytes) else d for d in delivered_ids}

        filtered = []
        for art in articles_raw:
            # Skip already delivered
            if art.get("id") in delivered_set:
                continue

            # Category/source/type filters
            if not passes_filter(art, ch):
                continue

            filtered.append(art)

        # Sort by published_at descending (newest first)
        filtered.sort(key=lambda a: a.get("published_at", ""), reverse=True)

        # On-demand AI processing based on ai_mode
        articles_out = []
        source_ids_used = []  # Track which source articles were used

        if ai_mode == "off":
            # Raw articles, no AI processing
            result = filtered[:max_items]
            for art in result:
                payload = build_payload(art, ch)
                if isinstance(payload, str):
                    articles_out.append({"content": payload, "id": art.get("id"), "published_at": art.get("published_at", "")})
                else:
                    articles_out.append(payload)
                source_ids_used.append(art.get("id"))

        elif ai_mode == "rewrite":
            # One-to-one AI rewrite + style (merged in one call)
            from ai.channel_processor import process_rewrite
            result = filtered[:max_items]
            for art in result:
                rewritten = await process_rewrite(
                    art, ch, redis,
                    output_format=ch.get("output_format", "summary"),
                    platform=ch.get("platform", "custom"),
                    style_config=ch.get("style", {}),
                    client_style_prompt=style_prompt,
                    client_api_key=x_ai_api_key or None,
                    client_base_url=x_ai_base_url or None,
                    client_model=x_ai_model or None,
                )
                payload = build_payload(rewritten, ch)
                if isinstance(payload, str):
                    articles_out.append({"content": payload, "id": rewritten.get("id"), "published_at": rewritten.get("published_at", "")})
                else:
                    articles_out.append(payload)
                source_ids_used.append(art.get("id"))

        elif ai_mode in ("synthetic", "debate"):
            # Multi-article synthesis/debate — need minimum 3 articles
            if len(filtered) < 3:
                # Not enough articles for synthesis/debate
                await _track_pull(redis, ch_id, client_id, 0)
                raise HTTPException(204, f"Need at least 3 articles for {ai_mode} mode")

            # Process in batches (3-10 articles per synthetic/debate)
            from ai.channel_processor import process_synthetic, process_debate
            processor = process_synthetic if ai_mode == "synthetic" else process_debate

            batch_size = min(10, max(3, max_items))  # 3-10 articles per batch
            batches_needed = max(1, max_items // batch_size)

            for batch_idx in range(batches_needed):
                start_idx = batch_idx * batch_size
                end_idx = start_idx + batch_size
                batch = filtered[start_idx:end_idx]

                if len(batch) < 3:
                    break  # Not enough for another batch

                # Process batch
                result_art = await processor(batch, ch, redis)
                payload = build_payload(result_art, ch)
                if isinstance(payload, str):
                    articles_out.append({"content": payload, "id": result_art.get("id"), "published_at": result_art.get("published_at", "")})
                else:
                    articles_out.append(payload)

                # Mark all source articles as used
                source_ids_used.extend([a.get("id") for a in batch])

                if len(articles_out) >= max_items:
                    break

        # Determine new cursor (latest source article timestamp)
        new_cursor = ""
        if source_ids_used:
            # Find the latest timestamp from source articles used
            source_articles = [a for a in filtered if a.get("id") in source_ids_used]
            if source_articles:
                new_cursor = max(a.get("published_at", "") for a in source_articles)

        # Mark source articles as delivered
        if source_ids_used:
            await redis.sadd(delivered_key, *source_ids_used)

        # Auto-ack: advance cursor
        if auto_ack and source_ids_used:
            try:
                new_cursor_ts = datetime.fromisoformat(new_cursor).timestamp()
                await redis.set(f"channel:{ch_id}:client:{client_id}:cursor", str(new_cursor_ts))
            except (ValueError, TypeError):
                pass

        # Check if there are more articles beyond what we returned
        # For synthetic/debate, check if we have enough for another batch
        if ai_mode in ("synthetic", "debate"):
            remaining = len(filtered) - len(source_ids_used)
            has_more = remaining >= 3
        else:
            has_more = len(filtered) > len(source_ids_used)

        # Track stats
        await _track_pull(redis, ch_id, client_id, len(articles_out))

        # Log request
        duration_ms = int((time.time() - start_time) * 1000)
        await log_channel_request(
            channel_id=ch_id,
            client_id=client_id,
            endpoint="/feed",
            method="GET",
            status_code=200,
            auth_method=auth_method,
            items_count=len(articles_out),
            duration_ms=duration_ms,
        )

        return {
            "articles": articles_out,
            "count": len(articles_out),
            "cursor": new_cursor,
            "has_more": has_more,
        }
    except HTTPException as e:
        duration_ms = int((time.time() - start_time) * 1000)
        await log_channel_request(
            channel_id=ch_id,
            client_id=client_id if 'client_id' in locals() else "",
            endpoint="/feed",
            method="GET",
            status_code=e.status_code,
            auth_method=auth_method if 'auth_method' in locals() else "unknown",
            items_count=0,
            duration_ms=duration_ms,
            error_msg=e.detail if hasattr(e, 'detail') else str(e),
        )
        raise
    except ValueError as e:
        # Catch timeout errors from AI processing
        if "timeout" in str(e).lower():
            duration_ms = int((time.time() - start_time) * 1000)
            await log_channel_request(
                channel_id=ch_id,
                client_id=client_id,
                endpoint="/feed",
                method="GET",
                status_code=504,
                auth_method=auth_method,
                items_count=0,
                duration_ms=duration_ms,
                error_msg=str(e),
            )
            raise HTTPException(504, str(e))
        raise
    except Exception as e:
        duration_ms = int((time.time() - start_time) * 1000)
        await log_channel_request(
            channel_id=ch_id,
            client_id=client_id,
            endpoint="/feed",
            method="GET",
            status_code=500,
            auth_method=auth_method,
            items_count=0,
            duration_ms=duration_ms,
            error_msg=str(e),
        )
        raise


# ── Next endpoint (single article for immediate posting) ────────────────────


@router.get("/channels/{ch_id}/next", summary="Pick one article to post now")
async def channel_next(
    ch_id: str,
    style_prompt: str | None = Query(default=None, description="Client-provided style prompt override"),
    x_api_key: str = Header(alias="X-API-Key", default=""),
    x_client_id: str = Header(alias="X-Client-ID", default=""),
    x_ai_api_key: str = Header(alias="X-AI-API-Key", default=""),
    x_ai_base_url: str = Header(alias="X-AI-Base-URL", default=""),
    x_ai_model: str = Header(alias="X-AI-Model", default=""),
):
    """
    Pick ONE article to post immediately. On-demand AI processing.

    **On-Demand Processing:**
    - `ai_mode=off`: Return raw article
    - `ai_mode=rewrite`: Process 1 article with AI rewrite
    - `ai_mode=synthetic`: Collect 3-10 articles → synthesize → return 1 result
    - `ai_mode=debate`: Collect 3-10 articles → debate → return 1 result

    Uses per-client Redis set to track delivered article IDs (source articles).

    Requires `X-Client-ID` header for per-client tracking.

    - **200**: Returns `{"article": {...}}` with processed article
    - **204**: No articles available or insufficient data for synthesis/debate
    - **401**: Invalid API key
    - **403**: Channel disabled
    - **500**: AI processing failed

    Pass client AI credentials via `X-AI-*` headers when `ai_source=client`.
    """
    start_time = time.time()
    
    if not x_client_id.strip():
        raise HTTPException(400, "X-Client-ID header is required")
    
    client_id = x_client_id.strip()
    ch, auth_method = _auth_channel(ch_id, x_api_key)
    redis = get_redis()

    try:
        ai_mode = ch.get("ai_mode", "off")
        delivered_key = f"channel:{ch_id}:client:{client_id}:delivered"

        # ALWAYS fetch from news:feed (original articles)
        feed_key = "news:feed"

        # Fetch a pool of recent article IDs (newest first)
        raw_ids = await redis.zrevrange(feed_key, 0, 99)
        if not raw_ids:
            raise HTTPException(204)

        ids = [aid.decode() if isinstance(aid, bytes) else aid for aid in raw_ids]

        # Filter out already-delivered IDs
        if ids:
            pipe = redis.pipeline()
            for aid in ids:
                pipe.sismember(delivered_key, aid)
            delivered_flags = await pipe.execute()
            candidate_ids = [aid for aid, delivered in zip(ids, delivered_flags) if not delivered]
        else:
            candidate_ids = []

        if not candidate_ids:
            raise HTTPException(204)

        # Fetch article data
        articles_raw = await get_articles_batch(redis, candidate_ids)

        # Filter by category/source/type
        filtered = [art for art in articles_raw if passes_filter(art, ch)]

        if not filtered:
            raise HTTPException(204)

        # Process based on ai_mode
        from ai.channel_processor import process_rewrite, process_synthetic, process_debate

        if ai_mode == "off":
            # Return raw article
            picked = filtered[0]
            article_out = picked
            source_ids = [picked["id"]]

        elif ai_mode == "rewrite":
            # Process 1 article with AI rewrite + style (merged)
            picked = filtered[0]
            article_out = await process_rewrite(
                picked, ch, redis,
                output_format=ch.get("output_format", "summary"),
                platform=ch.get("platform", "custom"),
                style_config=ch.get("style", {}),
                client_style_prompt=style_prompt,
                client_api_key=x_ai_api_key or None,
                client_base_url=x_ai_base_url or None,
                client_model=x_ai_model or None,
            )
            source_ids = [picked["id"]]

        elif ai_mode == "synthetic":
            # Need 3-10 articles for synthesis
            if len(filtered) < 3:
                raise HTTPException(204, f"Insufficient articles for synthesis (need 3, have {len(filtered)})")
            
            batch = filtered[:10]  # Take up to 10 articles
            article_out = await process_synthetic(batch, ch, redis)
            source_ids = [a["id"] for a in batch]

        elif ai_mode == "debate":
            # Need 3-10 articles for debate
            if len(filtered) < 3:
                raise HTTPException(204, f"Insufficient articles for debate (need 3, have {len(filtered)})")
            
            batch = filtered[:10]  # Take up to 10 articles
            article_out = await process_debate(batch, ch, redis)
            source_ids = [a["id"] for a in batch]

        else:
            raise HTTPException(400, f"Invalid ai_mode: {ai_mode}")

        # Mark source articles as delivered
        pipe = redis.pipeline()
        for sid in source_ids:
            pipe.sadd(delivered_key, sid)
        pipe.expire(delivered_key, 86400)
        await pipe.execute()

        # Build payload
        payload = build_payload(article_out, ch)
        if isinstance(payload, str):
            article_out = {"content": payload, "id": article_out.get("id"), "published_at": article_out.get("published_at", "")}
        else:
            article_out = payload

        # Track stats
        await _track_pull(redis, ch_id, client_id, 1)

        duration_ms = int((time.time() - start_time) * 1000)
        await log_channel_request(
            channel_id=ch_id,
            client_id=client_id,
            endpoint="/next",
            method="GET",
            status_code=200,
            auth_method=auth_method,
            items_count=1,
            duration_ms=duration_ms,
        )

        return {"article": article_out}
    except HTTPException as e:
        duration_ms = int((time.time() - start_time) * 1000)
        await log_channel_request(
            channel_id=ch_id,
            client_id=client_id if 'client_id' in locals() else "",
            endpoint="/next",
            method="GET",
            status_code=e.status_code,
            auth_method=auth_method if 'auth_method' in locals() else "unknown",
            items_count=0,
            duration_ms=duration_ms,
            error_msg=e.detail if hasattr(e, 'detail') else str(e),
        )
        raise
    except ValueError as e:
        # Catch timeout and rate limit errors from AI processing
        error_msg = str(e).lower()
        if "timeout" in error_msg:
            duration_ms = int((time.time() - start_time) * 1000)
            await log_channel_request(
                channel_id=ch_id,
                client_id=client_id,
                endpoint="/next",
                method="GET",
                status_code=504,
                auth_method=auth_method,
                items_count=0,
                duration_ms=duration_ms,
                error_msg=str(e),
            )
            raise HTTPException(504, str(e))
        elif "rate limit" in error_msg:
            duration_ms = int((time.time() - start_time) * 1000)
            await log_channel_request(
                channel_id=ch_id,
                client_id=client_id,
                endpoint="/next",
                method="GET",
                status_code=429,
                auth_method=auth_method,
                items_count=0,
                duration_ms=duration_ms,
                error_msg=str(e),
            )
            raise HTTPException(429, str(e))
        raise
    except Exception as e:
        duration_ms = int((time.time() - start_time) * 1000)
        await log_channel_request(
            channel_id=ch_id,
            client_id=client_id,
            endpoint="/next",
            method="GET",
            status_code=500,
            auth_method=auth_method,
            items_count=0,
            duration_ms=duration_ms,
            error_msg=str(e),
        )
        raise


# ── Ack endpoint ─────────────────────────────────────────────────────────────


@router.post("/channels/{ch_id}/ack", summary="Acknowledge received articles")
async def channel_ack(
    ch_id: str,
    body: AckIn,
    x_api_key: str = Header(alias="X-API-Key", default=""),
    x_client_id: str = Header(alias="X-Client-ID", default=""),
):
    """
    Acknowledge articles up to the given cursor.
    Next `/feed` call will only return articles newer than this cursor.

    Requires `X-Client-ID` header for per-client cursor tracking.
    """
    if not x_client_id.strip():
        raise HTTPException(400, "X-Client-ID header is required")
    
    start_time = time.time()
    client_id = x_client_id.strip()
    ch, auth_method = _auth_channel(ch_id, x_api_key)
    redis = get_redis()

    try:
        try:
            cursor_ts = datetime.fromisoformat(body.cursor).timestamp()
        except ValueError:
            duration_ms = int((time.time() - start_time) * 1000)
            await log_channel_request(
                channel_id=ch_id,
                client_id=client_id,
                endpoint="/ack",
                method="POST",
                status_code=400,
                auth_method=auth_method,
                items_count=0,
                duration_ms=duration_ms,
                error_msg="Invalid cursor format",
            )
            raise HTTPException(400, "Invalid cursor format — use ISO 8601 timestamp")

        await redis.set(f"channel:{ch_id}:client:{client_id}:cursor", str(cursor_ts))
        logger.info(f"Channel {ch_id} client {client_id} cursor advanced to {body.cursor}")
        
        duration_ms = int((time.time() - start_time) * 1000)
        await log_channel_request(
            channel_id=ch_id,
            client_id=client_id,
            endpoint="/ack",
            method="POST",
            status_code=200,
            auth_method=auth_method,
            items_count=0,
            duration_ms=duration_ms,
        )
        
        return {"ok": True, "cursor": body.cursor}
    except HTTPException:
        raise
    except Exception as e:
        duration_ms = int((time.time() - start_time) * 1000)
        await log_channel_request(
            channel_id=ch_id,
            client_id=client_id,
            endpoint="/ack",
            method="POST",
            status_code=500,
            auth_method=auth_method,
            items_count=0,
            duration_ms=duration_ms,
            error_msg=str(e),
        )
        raise


# ── Reset cursor ─────────────────────────────────────────────────────────────


@router.post("/channels/{ch_id}/reset-cursor", summary="Reset channel cursor")
async def reset_cursor(
    ch_id: str,
    x_api_key: str = Header(alias="X-API-Key", default=""),
    x_client_id: str = Header(alias="X-Client-ID", default=""),
):
    """Reset cursor to beginning — next `/feed` call returns all available articles.

    Requires `X-Client-ID` header for per-client cursor tracking.
    """
    if not x_client_id.strip():
        raise HTTPException(400, "X-Client-ID header is required")
    
    start_time = time.time()
    client_id = x_client_id.strip()
    ch, auth_method = _auth_channel(ch_id, x_api_key)
    redis = get_redis()
    
    try:
        await redis.delete(f"channel:{ch_id}:client:{client_id}:cursor")
        
        duration_ms = int((time.time() - start_time) * 1000)
        await log_channel_request(
            channel_id=ch_id,
            client_id=client_id,
            endpoint="/reset-cursor",
            method="POST",
            status_code=200,
            auth_method=auth_method,
            items_count=0,
            duration_ms=duration_ms,
        )
        
        return {"ok": True, "message": "Cursor reset"}
    except Exception as e:
        duration_ms = int((time.time() - start_time) * 1000)
        await log_channel_request(
            channel_id=ch_id,
            client_id=client_id,
            endpoint="/reset-cursor",
            method="POST",
            status_code=500,
            auth_method=auth_method,
            items_count=0,
            duration_ms=duration_ms,
            error_msg=str(e),
        )
        raise


# ── Channel stats ────────────────────────────────────────────────────────────


@router.get("/channels/{ch_id}/stats", summary="Get channel pull statistics")
async def channel_stats(
    ch_id: str,
    x_client_id: str = Header(alias="X-Client-ID", default=""),
):
    """Return pull count, total articles delivered, cursor position, and last pull time.

    If X-Client-ID provided: returns stats for that specific client.
    If no X-Client-ID: returns aggregated stats from ALL clients.
    """
    start_time = time.time()
    client_id = x_client_id.strip() if x_client_id else ""
    
    _find_channel(ch_id)
    redis = get_redis()

    try:
        if client_id:
            # Single client stats
            cursor_raw = await redis.get(f"channel:{ch_id}:client:{client_id}:cursor")
            stats_raw = await redis.hgetall(f"channel:{ch_id}:client:{client_id}:stats")
            stats = {k.decode(): v.decode() for k, v in stats_raw.items()} if stats_raw else {}

            cursor_iso = ""
            if cursor_raw:
                try:
                    cursor_iso = datetime.fromtimestamp(float(cursor_raw), tz=timezone.utc).isoformat()
                except (ValueError, TypeError):
                    pass

            duration_ms = int((time.time() - start_time) * 1000)
            await log_channel_request(
                channel_id=ch_id,
                client_id=client_id,
                endpoint="/stats",
                method="GET",
                status_code=200,
                auth_method="public",
                items_count=0,
                duration_ms=duration_ms,
            )

            return {
                "channel_id": ch_id,
                "client_id": client_id,
                "cursor": cursor_iso,
                "total_pulls": int(stats.get("pull_count", 0)),
                "total_articles_delivered": int(stats.get("articles_delivered", 0)),
                "last_pull_at": stats.get("last_pull_at", ""),
            }
        else:
            # Aggregate stats from all clients
            keys = await redis.keys(f"channel:{ch_id}:client:*:stats")
            if not keys:
                return {
                    "channel_id": ch_id,
                    "total_clients": 0,
                    "total_pulls": 0,
                    "total_articles_delivered": 0,
                    "clients": [],
                }
            
            clients = []
            total_pulls = 0
            total_articles = 0
            
            for key in keys:
                key_str = key.decode() if isinstance(key, bytes) else key
                # Extract client_id from key: channel:{ch_id}:client:{client_id}:stats
                parts = key_str.split(":")
                if len(parts) >= 4:
                    cid = parts[3]
                    stats_raw = await redis.hgetall(key)
                    stats = {k.decode(): v.decode() for k, v in stats_raw.items()} if stats_raw else {}
                    
                    pulls = int(stats.get("pull_count", 0))
                    articles = int(stats.get("articles_delivered", 0))
                    
                    total_pulls += pulls
                    total_articles += articles
                    
                    clients.append({
                        "client_id": cid,
                        "pulls": pulls,
                        "articles_delivered": articles,
                        "last_pull_at": stats.get("last_pull_at", ""),
                    })
            
            # Sort by last_pull_at desc
            clients.sort(key=lambda c: c.get("last_pull_at", ""), reverse=True)
            
            duration_ms = int((time.time() - start_time) * 1000)
            await log_channel_request(
                channel_id=ch_id,
                client_id="",
                endpoint="/stats",
                method="GET",
                status_code=200,
                auth_method="public",
                items_count=0,
                duration_ms=duration_ms,
            )

            return {
                "channel_id": ch_id,
                "total_clients": len(clients),
                "total_pulls": total_pulls,
                "total_articles_delivered": total_articles,
                "clients": clients,
            }
    except Exception as e:
        duration_ms = int((time.time() - start_time) * 1000)
        await log_channel_request(
            channel_id=ch_id,
            client_id=client_id,
            endpoint="/stats",
            method="GET",
            status_code=500,
            auth_method="public",
            items_count=0,
            duration_ms=duration_ms,
            error_msg=str(e),
        )
        raise


@router.get("/channels/{ch_id}/logs", summary="Get channel request logs")
async def channel_logs(
    ch_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    client_id: str | None = Query(default=None),
    endpoint: str | None = Query(default=None),
    status_code: int | None = Query(default=None),
):
    """
    Fetch request logs for this channel from SQLite.
    
    Query params:
    - limit: Max rows to return (default 50, max 200)
    - client_id: Filter by client ID (optional)
    - endpoint: Filter by endpoint (optional, e.g., "/feed", "/next")
    - status_code: Filter by HTTP status (optional, e.g., 200, 204, 500)
    
    Returns: {"logs": [...], "count": N}
    """
    _find_channel(ch_id)
    
    from storage.sqlite_stats import _db
    import aiosqlite
    
    # Build query with filters
    query = "SELECT * FROM channel_logs WHERE channel_id = ?"
    params = [ch_id]
    
    if client_id:
        query += " AND client_id = ?"
        params.append(client_id)
    
    if endpoint:
        query += " AND endpoint = ?"
        params.append(endpoint)
    
    if status_code is not None:
        query += " AND status_code = ?"
        params.append(status_code)
    
    query += " ORDER BY requested_at DESC LIMIT ?"
    params.append(limit)
    
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(query, params)
    
    logs = []
    for row in rows:
        logs.append({
            "id": row["id"],
            "channel_id": row["channel_id"],
            "client_id": row["client_id"],
            "endpoint": row["endpoint"],
            "method": row["method"],
            "status_code": row["status_code"],
            "auth_method": row["auth_method"],
            "items_count": row["items_count"],
            "requested_at": row["requested_at"],
            "duration_ms": row["duration_ms"],
            "error_msg": row["error_msg"],
        })
    
    return {"logs": logs, "count": len(logs)}


# ── Internal helpers ─────────────────────────────────────────────────────────


async def _track_pull(redis, channel_id: str, client_id: str, article_count: int) -> None:
    """Track pull statistics in Redis (per-client)."""
    key = f"channel:{channel_id}:client:{client_id}:stats"
    pipe = redis.pipeline()
    pipe.hincrby(key, "pull_count", 1)
    pipe.hincrby(key, "articles_delivered", article_count)
    pipe.hset(key, "last_pull_at", datetime.now(timezone.utc).isoformat())
    pipe.expire(key, 86400 * 7)  # 7 days TTL
    await pipe.execute()

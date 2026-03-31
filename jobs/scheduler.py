"""
APScheduler: due-based crawler + AI rewriter.
Every tick (default 30s), picks the N sources whose next_crawl_at is due
and fetches them. 403/429 responses push next_crawl_at further into the future.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

import redis.asyncio as aioredis
import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler

try:
    from realtime.manager import ws_manager
except ImportError:
    ws_manager = None

from crawler.fetcher import fetch_all_sources
from ai.rewriter import process_pending_articles
from ai.topic_synthesis import process_category_synthesis
from ai.enricher import batch_enrich
from ai.embedder import get_embedding, embed_text_for_article
from ai.clusterer import assign_topic
from ai.story_detector import assign_story
from ai.trend_detector import run_trend_detection
from ai.newsletter import generate_newsletter, send_newsletter_smtp
from ai.debate import debate_job as _run_debate_job
from vector_db.weaviate_store import index_article
from storage.lake_store import get_lake
from storage.redis_store import (
    get_due_source_ids,
    pop_pending_ai_articles,
    pop_pending_enrichments,
    save_article_enrichment,
    save_embedding,
    get_article,
    get_articles_batch,
)
from storage.sqlite_stats import log_system_event

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None
_job_states: dict[str, str] = {}  # job_id → "idle" | "running" | "error"
_job_last_duration_ms: dict[str, int] = {}


def _active(ep: dict) -> bool:
    return ep.get("enabled", True)


def get_scheduler_status() -> list[dict]:
    """Return current state of all scheduled jobs (for API/UI)."""
    if _scheduler is None:
        return []
    result = []
    for job in _scheduler.get_jobs():
        nrt = job.next_run_time
        result.append({
            "id": job.id,
            "next_run": nrt.isoformat() if nrt else None,
            "state": _job_states.get(job.id, "idle"),
            "last_duration_ms": _job_last_duration_ms.get(job.id),
        })
    return result


# Config cache: avoid disk I/O on every job tick — re-read only when file changes.
_config_cache: dict | None = None
_config_mtime: float = 0.0
_sources_cache: list[dict] | None = None
_sources_mtime: float = 0.0


def _load_config() -> dict:
    global _config_cache, _config_mtime
    try:
        mtime = os.path.getmtime("config/settings.yaml")
    except OSError:
        mtime = 0.0
    if _config_cache is None or mtime > _config_mtime:
        with open("config/settings.yaml") as f:
            _config_cache = yaml.safe_load(f)
        _config_mtime = mtime
    return _config_cache


def _resolve_ai_config(ai_cfg: dict, config_id: str | None) -> dict:
    """Return {tone, prompt_system, prompt_template} for the given config_id.
    If config_id is None or not found, returns global AI settings (built-in).
    """
    if config_id:
        target = next((c for c in ai_cfg.get("configs", []) if c["id"] == config_id), None)
        if target:
            return {
                "tone": target.get("tone") or ai_cfg.get("tone", "general"),
                "prompt_system": target.get("prompt_system") or None,
                "prompt_template": target.get("prompt_template") or None,
            }
    return {
        "tone": ai_cfg.get("tone", "general"),
        "prompt_system": None,
        "prompt_template": None,
    }


def _resolve_provider(ai_cfg: dict, provider_id: str | None = None) -> tuple[str, str, str | None]:
    """Return (api_key, base_url, model_override) for the given provider_id.
    model_override is None if the provider has no model set (use job-level model).
    Falls back to direct api_key/base_url in ai section for backward compatibility."""
    pid = provider_id or ai_cfg.get("provider_id")
    if pid:
        for p in ai_cfg.get("providers", []):
            if p.get("id") == pid:
                return p.get("api_key", ""), p.get("base_url", ""), p.get("model") or None
    return ai_cfg.get("api_key", ""), ai_cfg.get("base_url", ""), None


def _load_sources() -> list[dict]:
    global _sources_cache, _sources_mtime
    try:
        mtime = os.path.getmtime("config/sources.yaml")
    except OSError:
        mtime = 0.0
    if _sources_cache is None or mtime > _sources_mtime:
        with open("config/sources.yaml") as f:
            data = yaml.safe_load(f)
        _sources_cache = data.get("sources", [])
        _sources_mtime = mtime
    return _sources_cache


# ---------------------------------------------------------------------------
# Job functions — top-level so they are importable and testable independently
# ---------------------------------------------------------------------------

async def crawl_job(redis: aioredis.Redis) -> None:
    started = datetime.now(timezone.utc)
    _job_states["crawl_all"] = "running"
    if ws_manager:
        asyncio.create_task(ws_manager.broadcast("scheduler.job_start", {
            "job_id": "crawl_all", "job_name": "Crawl",
            "started_at": started.isoformat(),
        }))
    current_cfg = _load_config()
    crawler = current_cfg.get("crawler", {})
    active_cats = {
        c["id"] for c in current_cfg.get("categories", []) if c.get("enabled", True)
    }
    all_sources = _load_sources()
    filtered = [
        s
        for s in all_sources
        if s.get("enabled", True) and s.get("category", "world") in active_cats
    ]
    if not filtered:
        await log_system_event(
            "crawl_job",
            started,
            status="skipped",
            metadata={"reason": "no enabled sources"},
        )
        return

    sources_per_tick = crawler.get("sources_per_tick", 3)
    all_ids = [s["id"] for s in filtered]
    source_map = {s["id"]: s for s in filtered}

    # Pick sources whose next_crawl_at is due (or never scheduled)
    due_ids = await get_due_source_ids(redis, all_ids, limit=sources_per_tick)
    if not due_ids:
        await log_system_event(
            "crawl_job",
            started,
            status="skipped",
            metadata={"reason": "no sources due", "eligible": len(filtered)},
        )
        return

    batch = [source_map[sid] for sid in due_ids if sid in source_map]
    logger.info(f"=== Crawl tick: {len(batch)} sources due ({due_ids}) ===")

    default_interval_sec = crawler.get("default_crawl_interval_minutes", 45) * 60
    cat_intervals = crawler.get("category_crawl_interval_minutes", {})
    source_intervals = {
        s["id"]: cat_intervals.get(s.get("category", ""), crawler.get("default_crawl_interval_minutes", 45)) * 60
        for s in batch
    }

    try:
        results = await fetch_all_sources(
            sources=batch,
            redis=redis,
            max_concurrent=sources_per_tick,
            dedup_threshold=current_cfg.get("dedup", {}).get(
                "simhash_distance_threshold", 3
            ),
            max_articles_per_source=crawler.get("max_articles_per_source", 50),
            request_timeout=crawler.get("request_timeout_seconds", 15),
            domain_delay=(
                crawler.get("domain_delay_min", 0.5),
                crawler.get("domain_delay_max", 1.5),
            ),
            default_crawl_interval_sec=default_interval_sec,
            backoff_429_sec=crawler.get("backoff_429_minutes", 30) * 60,
            backoff_403_sec=crawler.get("backoff_403_minutes", 120) * 60,
            source_intervals=source_intervals,
        )
        meta = {
            "sources_in_batch": len(batch),
            "source_ids": due_ids,
            "total_found": sum(r.get("found", 0) for r in (results or [])),
            "total_saved": sum(r.get("saved", 0) for r in (results or [])),
            "total_duplicates": sum(
                r.get("duplicates", 0) for r in (results or [])
            ),
            "errors": sum(1 for r in (results or []) if r.get("errors", 0)),
        }
        dur_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        _job_states["crawl_all"] = "idle"
        _job_last_duration_ms["crawl_all"] = dur_ms
        if ws_manager:
            asyncio.create_task(ws_manager.broadcast("scheduler.job_done", {
                "job_id": "crawl_all", "job_name": "Crawl",
                "status": "ok", "duration_ms": dur_ms, "meta": meta,
            }))
        await log_system_event("crawl_job", started, status="ok", metadata=meta)
    except Exception as exc:
        _job_states["crawl_all"] = "error"
        if ws_manager:
            asyncio.create_task(ws_manager.broadcast("scheduler.job_done", {
                "job_id": "crawl_all", "job_name": "Crawl",
                "status": "error", "error": str(exc)[:200],
            }))
        await log_system_event(
            "crawl_job", started, status="error", error_msg=str(exc)
        )
        raise


async def ai_job(redis: aioredis.Redis) -> None:
    started = datetime.now(timezone.utc)
    _job_states["ai_rewrite"] = "running"
    if ws_manager:
        asyncio.create_task(ws_manager.broadcast("scheduler.job_start", {
            "job_id": "ai_rewrite", "job_name": "AI Rewrite",
            "started_at": started.isoformat(),
        }))
    current_cfg = _load_config()
    ai = current_cfg.get("ai", {})
    wh = current_cfg.get("webhook", {})
    tg = current_cfg.get("telegram", {})
    if not ai.get("enabled", True):
        await log_system_event(
            "ai_job",
            started,
            status="skipped",
            metadata={"reason": "AI disabled"},
        )
        return
    all_endpoints = wh.get("endpoints", [])
    all_tg = tg.get("channels", [])
    all_tw = current_cfg.get("twitter", {}).get("accounts", [])

    # Route hooks by ai_mode — default "rewrite" for backward compatibility
    rewrite_wh  = [e for e in all_endpoints if _active(e) and e.get("ai_mode", "rewrite") == "rewrite"]
    raw_wh      = [e for e in all_endpoints if _active(e) and e.get("ai_mode") == "off"]
    rewrite_tg  = [c for c in all_tg        if _active(c) and c.get("ai_mode", "rewrite") == "rewrite"]
    raw_tg      = [c for c in all_tg        if _active(c) and c.get("ai_mode") == "off"]
    rewrite_tw  = [a for a in all_tw        if _active(a) and a.get("ai_mode", "rewrite") == "rewrite"]

    has_hooks = bool(rewrite_wh or raw_wh or rewrite_tg or raw_tg or rewrite_tw)

    ai_interval_sec = ai.get("interval_minutes", 2) * 60
    spread = ai_interval_sec * 0.8

    api_key, base_url, model_override = _resolve_provider(ai)
    dedup_cfg = current_cfg.get("dedup", {})

    # Group rewrite hooks by (ai_config_id, target_language)
    config_groups: dict[tuple[str, str], dict] = {}
    for ep in rewrite_wh:
        cfg_id = ep.get("ai_config_id") or ""
        tgt_lang = ep.get("target_language") or ""
        key = (cfg_id, tgt_lang)
        config_groups.setdefault(key, {"wh": [], "tg": []})["wh"].append(ep)
    for ch in rewrite_tg:
        cfg_id = ch.get("ai_config_id") or ""
        tgt_lang = ch.get("target_language") or ""
        key = (cfg_id, tgt_lang)
        config_groups.setdefault(key, {"wh": [], "tg": []})["tg"].append(ch)
    # Ensure at least one group exists (for raw-only dispatch)
    if not config_groups:
        config_groups[("", "")] = {"wh": [], "tg": []}

    # Pop articles once — shared across all config groups
    articles = await pop_pending_ai_articles(redis, limit=ai.get("batch_size", 10))

    processed = 0
    try:
        is_first = True
        for (cfg_id, tgt_lang), group in config_groups.items():
            resolved = _resolve_ai_config(ai, cfg_id or None)
            n = await process_pending_articles(
                redis=redis,
                model=model_override,
                batch_size=ai.get("batch_size", 10),
                max_tokens=ai.get("max_tokens_summary", 300),
                temperature=ai.get("temperature", 0.3),
                tone=resolved["tone"],
                api_key=api_key or None,
                base_url=base_url or None,
                webhook_endpoints=group["wh"],
                telegram_channels=group["tg"],
                twitter_accounts=rewrite_tw if is_first else [],
                raw_webhook_endpoints=raw_wh if is_first else [],
                raw_telegram_channels=raw_tg if is_first else [],
                spread_seconds=spread,
                ai_dedup_threshold=dedup_cfg.get("ai_simhash_distance_threshold", 6),
                pre_fetched_articles=articles,
                config_id=cfg_id or None,
                prompt_system_override=resolved["prompt_system"],
                prompt_template_override=resolved["prompt_template"],
                perform_ai_dedup=is_first,
                target_language=tgt_lang or None,
            )
            processed = max(processed, n or 0)
            is_first = False

        if processed:
            logger.info(f"AI job: processed {processed} articles")
        dur_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        _job_states["ai_rewrite"] = "idle"
        _job_last_duration_ms["ai_rewrite"] = dur_ms
        if ws_manager:
            asyncio.create_task(ws_manager.broadcast("scheduler.job_done", {
                "job_id": "ai_rewrite", "job_name": "AI Rewrite",
                "status": "ok", "duration_ms": dur_ms,
                "meta": {"processed": processed or 0},
            }))
        await log_system_event(
            "ai_job",
            started,
            status="ok" if processed is not None else "skipped",
            metadata={
                "processed": processed or 0,
                "model": model_override or ai.get("model", ""),
                "batch_size": ai.get("batch_size", 10),
                "rewrite_hooks": len(rewrite_wh) + len(rewrite_tg),
                "raw_hooks": len(raw_wh) + len(raw_tg),
                "config_groups": len(config_groups),
            },
        )
    except Exception as exc:
        _job_states["ai_rewrite"] = "error"
        if ws_manager:
            asyncio.create_task(ws_manager.broadcast("scheduler.job_done", {
                "job_id": "ai_rewrite", "job_name": "AI Rewrite",
                "status": "error", "error": str(exc)[:200],
            }))
        await log_system_event(
            "ai_job", started, status="error", error_msg=str(exc)
        )
        raise


async def topic_synthesis_job(redis: aioredis.Redis) -> None:
    """Generates synthetic articles from grouped content per category."""
    started = datetime.now(timezone.utc)
    _job_states["topic_synthesis"] = "running"
    if ws_manager:
        asyncio.create_task(ws_manager.broadcast("scheduler.job_start", {
            "job_id": "topic_synthesis", "job_name": "Synthesis",
            "started_at": started.isoformat(),
        }))
    current_cfg = _load_config()
    ai = current_cfg.get("ai", {})
    synthesis_cfg = ai.get("topic_synthesis", {})

    if not ai.get("enabled", True) or not synthesis_cfg.get("enabled", False):
        return

    wh = current_cfg.get("webhook", {})
    tg = current_cfg.get("telegram", {})

    # Only dispatch to hooks that explicitly opted in to synthetic articles
    endpoints   = [e for e in wh.get("endpoints", []) if _active(e) and e.get("ai_mode") == "synthetic"]
    tg_channels = [c for c in tg.get("channels", []) if _active(c) and c.get("ai_mode") == "synthetic"]

    if not endpoints and not tg_channels:
        return  # no hooks want synthetic articles

    # Resolve provider: synthesis can use its own provider or fall back to AI summary provider
    synth_provider_id = synthesis_cfg.get("provider_id") or ai.get("provider_id")
    synth_api_key, synth_base_url, synth_model_override = _resolve_provider(ai, synth_provider_id)
    synth_model = synth_model_override

    active_cats = [
        c["id"]
        for c in current_cfg.get("categories", [])
        if c.get("enabled", True)
    ]

    if not active_cats:
        await log_system_event(
            "topic_synthesis_job",
            started,
            status="skipped",
            metadata={"reason": "no active categories"},
        )
        return

    total_generated = 0
    results_by_hook = {}

    # Build list of (hook_id, webhook_endpoints, telegram_channels) per hook.
    # Each hook tracks its own seen-article set independently, so the same
    # source articles are never re-used for the same hook but hooks are independent.
    synth_hooks = []
    for ep in endpoints:
        synth_hooks.append((ep.get("id", "wh"), [ep], []))
    for ch in tg_channels:
        synth_hooks.append((ch.get("id", "tg"), [], [ch]))

    try:
        for hook_id, wh_eps, tg_chs in synth_hooks:
            for category in active_cats:
                count = await process_category_synthesis(
                    redis=redis,
                    category=category,
                    hook_id=hook_id,
                    min_articles=synthesis_cfg.get("min_articles", 5),
                    max_articles=synthesis_cfg.get("max_articles", 15),
                    model=synth_model,
                    tone=ai.get("tone", "general"),
                    api_key=synth_api_key or None,
                    base_url=synth_base_url or None,
                    webhook_endpoints=wh_eps,
                    telegram_channels=tg_chs,
                )

                if count > 0:
                    key = f"{hook_id}/{category}"
                    results_by_hook[key] = count
                    total_generated += count
                    logger.info(
                        f"Topic synthesis: hook={hook_id} category={category} "
                        f"generated {count} synthetic articles"
                    )

        dur_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        _job_states["topic_synthesis"] = "idle"
        _job_last_duration_ms["topic_synthesis"] = dur_ms
        if ws_manager:
            asyncio.create_task(ws_manager.broadcast("scheduler.job_done", {
                "job_id": "topic_synthesis", "job_name": "Synthesis",
                "status": "ok", "duration_ms": dur_ms,
                "meta": {"total_generated": total_generated},
            }))
        await log_system_event(
            "topic_synthesis_job",
            started,
            status="ok",
            metadata={
                "total_generated": total_generated,
                "hooks_processed": len(synth_hooks),
                "categories_per_hook": len(active_cats),
                "results": results_by_hook,
            },
        )

        if total_generated > 0:
            logger.info(
                f"Topic synthesis job: {total_generated} synthetic articles "
                f"across {len(synth_hooks)} hooks × {len(active_cats)} categories"
            )

    except Exception as exc:
        _job_states["topic_synthesis"] = "error"
        if ws_manager:
            asyncio.create_task(ws_manager.broadcast("scheduler.job_done", {
                "job_id": "topic_synthesis", "job_name": "Synthesis",
                "status": "error", "error": str(exc)[:200],
            }))
        await log_system_event(
            "topic_synthesis_job", started, status="error", error_msg=str(exc)
        )
        logger.error(f"Topic synthesis job failed: {exc}", exc_info=True)
        raise


# ---------------------------------------------------------------------------
# Phase 2 — Enrichment job (entity extraction + embedding + clustering)
# ---------------------------------------------------------------------------

async def enrich_job(redis: aioredis.Redis) -> None:
    """
    Process the enrichment queue populated by update_article_ai.
    For each article: extract entities + sentiment, generate embedding,
    assign to a topic cluster, and persist the new fields.
    """
    started = datetime.now(timezone.utc)
    _job_states["enrich"] = "running"

    cfg = _load_config()
    processing_cfg = cfg.get("processing", {})
    if not processing_cfg.get("enabled", True):
        _job_states["enrich"] = "idle"
        return

    ai_cfg = cfg.get("ai", {})
    api_key, base_url, _ = _resolve_provider(ai_cfg)
    batch_size = processing_cfg.get("enrich_batch_size", 10)
    cluster_threshold = float(processing_cfg.get("cluster_threshold", 0.75))

    article_ids = await pop_pending_enrichments(redis, count=batch_size)
    if not article_ids:
        _job_states["enrich"] = "idle"
        return

    # Fetch full article data in a single chunked pipeline (replaces N sequential get_article calls)
    articles = await get_articles_batch(redis, article_ids)

    if not articles:
        _job_states["enrich"] = "idle"
        return

    logger.info(f"=== Enrich job: processing {len(articles)} articles ===")

    # Step 1 — Entity extraction + sentiment (batch, up to 5 parallel)
    enrich_results = await batch_enrich(articles, api_key=api_key, base_url=base_url)

    done = 0
    for art, enrichment in zip(articles, enrich_results):
        article_id = art["id"]
        category = art.get("category", "general")
        entities = enrichment.get("entities", [])
        sentiment = enrichment.get("sentiment", "neutral")
        topic_id = ""

        # Step 2 — Embedding
        embed_text = embed_text_for_article(art)
        embedding = await get_embedding(
            embed_text, api_key=api_key, base_url=base_url,
            model=processing_cfg.get("embedding_model"),
        )

        # Step 3 — Topic clustering (only if embedding succeeded)
        if embedding:
            await save_embedding(redis, article_id, embedding)
            try:
                topic_id = await assign_topic(
                    redis, article_id, embedding, category,
                    threshold=cluster_threshold,
                )
            except NotImplementedError:
                logger.debug("[enrich_job] clusterer._find_matching_topic not yet implemented")
            except Exception as exc:
                logger.warning(f"[enrich_job] clustering failed for {article_id}: {exc}")

        # Step 4 — Persist enrichment fields on article hash
        art["entities"] = entities
        art["sentiment"] = sentiment
        art["topic_id"] = topic_id
        await save_article_enrichment(redis, article_id, entities, sentiment, topic_id)

        # Step 5 — Index to Weaviate (vector store for RAG)
        if embedding:
            indexed = await index_article(art, embedding)
            if not indexed:
                logger.debug(f"[enrich_job] Weaviate indexing skipped for {article_id}")

        # Step 6 — Assign to story cluster
        try:
            story_id = await assign_story(redis, art)
            if story_id:
                await redis.hset(f"news:{article_id}", "story_id", story_id)
        except Exception as exc:
            logger.debug(f"[enrich_job] story assignment skipped for {article_id}: {exc}")

        # Step 7 — Archive to News Lake (R2 cold storage)
        lake = get_lake()
        if lake:
            await lake.archive_article(art)

        done += 1

    dur_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    _job_states["enrich"] = "idle"
    _job_last_duration_ms["enrich"] = dur_ms
    logger.info(f"Enrich job: {done}/{len(articles)} articles enriched in {dur_ms}ms")
    await log_system_event(
        "enrich_job", started, status="ok",
        metadata={"processed": done, "duration_ms": dur_ms},
    )


# ---------------------------------------------------------------------------
# Phase 3 jobs — Trend Detection, Story (wired via enrich_job), Newsletter
# ---------------------------------------------------------------------------

async def trend_job(redis: aioredis.Redis) -> None:
    """Compute velocity-based trend scores across all active categories."""
    started = datetime.now(timezone.utc)
    _job_states["trend"] = "running"
    cfg = _load_config()
    active_cats = [
        c["id"] for c in cfg.get("categories", []) if c.get("enabled", True)
    ]
    if not active_cats:
        _job_states["trend"] = "idle"
        return
    try:
        result = await run_trend_detection(redis, active_cats)
        dur_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        _job_states["trend"] = "idle"
        _job_last_duration_ms["trend"] = dur_ms
        await log_system_event(
            "trend_job", started, status="ok",
            metadata={
                "trending_categories": result["trending_categories"],
                "duration_ms": dur_ms,
            },
        )
    except Exception as exc:
        _job_states["trend"] = "error"
        await log_system_event("trend_job", started, status="error", error_msg=str(exc))
        raise


async def debate_scheduler_job(redis: aioredis.Redis) -> None:
    """Run multi-agent debate on stories with enough articles."""
    started = datetime.now(timezone.utc)
    _job_states["debate"] = "running"
    cfg = _load_config()
    debate_cfg = cfg.get("debate", {})
    if not debate_cfg.get("enabled", False):
        _job_states["debate"] = "idle"
        return

    ai_cfg = cfg.get("ai", {})
    api_key, base_url, model_override = _resolve_provider(ai_cfg)
    wh = cfg.get("webhook", {})
    tg = cfg.get("telegram", {})
    tw = cfg.get("twitter", {})

    debate_wh = [e for e in wh.get("endpoints", []) if _active(e) and e.get("ai_mode") == "debate"]
    debate_tg = [c for c in tg.get("channels", []) if _active(c) and c.get("ai_mode") == "debate"]
    debate_tw = [a for a in tw.get("accounts", []) if _active(a) and a.get("ai_mode") == "debate"]

    try:
        count = await _run_debate_job(
            redis=redis,
            webhook_endpoints=debate_wh,
            telegram_channels=debate_tg,
            twitter_accounts=debate_tw,
            api_key=api_key or None,
            base_url=base_url or None,
            model=model_override or debate_cfg.get("model", ""),
        )
        dur_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        _job_states["debate"] = "idle"
        _job_last_duration_ms["debate"] = dur_ms
        await log_system_event(
            "debate_job", started, status="ok",
            metadata={"debated": count, "duration_ms": dur_ms},
        )
    except Exception as exc:
        _job_states["debate"] = "error"
        await log_system_event("debate_job", started, status="error", error_msg=str(exc))
        raise


async def newsletter_job(redis: aioredis.Redis) -> None:
    """Generate and store the daily newsletter digest."""
    started = datetime.now(timezone.utc)
    _job_states["newsletter"] = "running"
    cfg = _load_config()
    nl_cfg = cfg.get("newsletter", {})
    if not nl_cfg.get("enabled", False):
        _job_states["newsletter"] = "idle"
        return

    ai_cfg = cfg.get("ai", {})
    api_key, base_url, model_override = _resolve_provider(ai_cfg)
    active_cats = [
        c["id"] for c in cfg.get("categories", []) if c.get("enabled", True)
    ]

    try:
        result = await generate_newsletter(
            redis=redis,
            categories=active_cats,
            language=nl_cfg.get("language", "English"),
            api_key=api_key or None,
            base_url=base_url or None,
            model=model_override or nl_cfg.get("model", ""),
            max_tokens=nl_cfg.get("max_tokens", 1500),
            temperature=nl_cfg.get("temperature", 0.4),
            lookback_seconds=nl_cfg.get("lookback_hours", 24) * 3600,
        )
        # SMTP delivery (optional — only if smtp config present)
        smtp_cfg = nl_cfg.get("smtp", {})
        if smtp_cfg.get("host") and not result.get("skipped"):
            await send_newsletter_smtp(
                subject=result.get("subject", "News Briefing"),
                html=result.get("html", ""),
                smtp_cfg=smtp_cfg,
            )

        dur_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        _job_states["newsletter"] = "idle"
        _job_last_duration_ms["newsletter"] = dur_ms
        await log_system_event(
            "newsletter_job", started, status="ok" if not result.get("skipped") else "skipped",
            metadata={"subject": result.get("subject", ""), "duration_ms": dur_ms,
                      "smtp": bool(smtp_cfg.get("host"))},
        )
    except Exception as exc:
        _job_states["newsletter"] = "error"
        await log_system_event("newsletter_job", started, status="error", error_msg=str(exc))
        raise


# ---------------------------------------------------------------------------
# Scheduler factory — thin: reads intervals, registers jobs, returns scheduler
# ---------------------------------------------------------------------------

def get_scheduler(redis: aioredis.Redis) -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    cfg = _load_config()
    crawler_cfg = cfg.get("crawler", {})
    ai_cfg = cfg.get("ai", {})

    scheduler = AsyncIOScheduler()

    tick_interval = crawler_cfg.get("tick_interval_seconds", 30)
    scheduler.add_job(
        crawl_job,
        "interval",
        seconds=tick_interval,
        id="crawl_all",
        args=[redis],
        max_instances=1,
        coalesce=True,
    )

    ai_interval = ai_cfg.get("interval_minutes", 2)
    scheduler.add_job(
        ai_job,
        "interval",
        minutes=ai_interval,
        id="ai_rewrite",
        args=[redis],
        max_instances=1,
        coalesce=True,
    )

    synthesis_interval = ai_cfg.get("topic_synthesis", {}).get("interval_minutes", 5)
    scheduler.add_job(
        topic_synthesis_job,
        "interval",
        minutes=synthesis_interval,
        id="topic_synthesis",
        args=[redis],
        max_instances=1,
        coalesce=True,
    )

    processing_cfg = cfg.get("processing", {})
    if processing_cfg.get("enabled", True):
        enrich_interval = processing_cfg.get("enrich_interval_minutes", 5)
        scheduler.add_job(
            enrich_job,
            "interval",
            minutes=enrich_interval,
            id="enrich",
            args=[redis],
            max_instances=1,
            coalesce=True,
        )

    # Phase 3 — Multi-Agent Debate (opt-in, expensive — 4 AI calls per story)
    debate_cfg = cfg.get("debate", {})
    if debate_cfg.get("enabled", False):
        debate_interval = debate_cfg.get("interval_minutes", 30)
        scheduler.add_job(
            debate_scheduler_job,
            "interval",
            minutes=debate_interval,
            id="debate",
            args=[redis],
            max_instances=1,
            coalesce=True,
        )

    # Phase 3 — Trend detection (runs frequently, lightweight Redis ops only)
    trend_interval = cfg.get("intelligence", {}).get("trend_interval_minutes", 5)
    scheduler.add_job(
        trend_job,
        "interval",
        minutes=trend_interval,
        id="trend",
        args=[redis],
        max_instances=1,
        coalesce=True,
    )

    # Phase 3 — Newsletter (opt-in, daily by default)
    nl_cfg = cfg.get("newsletter", {})
    if nl_cfg.get("enabled", False):
        nl_interval = nl_cfg.get("interval_minutes", 360)  # default every 6h
        scheduler.add_job(
            newsletter_job,
            "interval",
            minutes=nl_interval,
            id="newsletter",
            args=[redis],
            max_instances=1,
            coalesce=True,
        )

    _scheduler = scheduler
    return scheduler

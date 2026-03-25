"""
APScheduler: due-based crawler + AI rewriter.
Every tick (default 30s), picks the N sources whose next_crawl_at is due
and fetches them. 403/429 responses push next_crawl_at further into the future.
"""

import logging
import os
from datetime import datetime, timezone

import redis.asyncio as aioredis
import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from crawler.fetcher import fetch_all_sources
from ai.rewriter import process_pending_articles
from ai.topic_synthesis import process_category_synthesis
from storage.redis_store import get_due_source_ids, pop_pending_ai_articles
from storage.sqlite_stats import log_system_event
from webhook.dispatcher import enqueue_dispatch

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None

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


def get_scheduler(redis: aioredis.Redis) -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    cfg = _load_config()
    crawler_cfg = cfg.get("crawler", {})
    ai_cfg = cfg.get("ai", {})

    scheduler = AsyncIOScheduler()

    async def crawl_job():
        started = datetime.now(timezone.utc)
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

        default_interval_sec = crawler.get("default_crawl_interval_minutes", 10) * 60

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
            await log_system_event("crawl_job", started, status="ok", metadata=meta)
        except Exception as exc:
            await log_system_event(
                "crawl_job", started, status="error", error_msg=str(exc)
            )
            raise

    tick_interval = crawler_cfg.get("tick_interval_seconds", 30)
    scheduler.add_job(
        crawl_job,
        "interval",
        seconds=tick_interval,
        id="crawl_all",
        max_instances=1,
        coalesce=True,
    )

    async def ai_job():
        started = datetime.now(timezone.utc)
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

        # Route hooks by ai_mode — default "rewrite" for backward compatibility
        def _active(ep): return ep.get("enabled", True)
        rewrite_wh  = [e for e in all_endpoints if _active(e) and e.get("ai_mode", "rewrite") == "rewrite"]
        raw_wh      = [e for e in all_endpoints if _active(e) and e.get("ai_mode") == "off"]
        rewrite_tg  = [c for c in all_tg        if _active(c) and c.get("ai_mode", "rewrite") == "rewrite"]
        raw_tg      = [c for c in all_tg        if _active(c) and c.get("ai_mode") == "off"]

        if not (rewrite_wh or raw_wh or rewrite_tg or raw_tg):
            await log_system_event(
                "ai_job", started, status="skipped",
                metadata={"reason": "no rewrite/raw hooks configured"},
            )
            return

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
                    model=model_override or "gpt-4o-mini",
                    batch_size=ai.get("batch_size", 10),
                    max_tokens=ai.get("max_tokens_summary", 300),
                    temperature=ai.get("temperature", 0.3),
                    tone=resolved["tone"],
                    api_key=api_key or None,
                    base_url=base_url or None,
                    webhook_endpoints=group["wh"],
                    telegram_channels=group["tg"],
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
            await log_system_event(
                "ai_job",
                started,
                status="ok" if processed is not None else "skipped",
                metadata={
                    "processed": processed or 0,
                    "model": model_override or "gpt-4o-mini",
                    "batch_size": ai.get("batch_size", 10),
                    "rewrite_hooks": len(rewrite_wh) + len(rewrite_tg),
                    "raw_hooks": len(raw_wh) + len(raw_tg),
                    "config_groups": len(config_groups),
                },
            )
        except Exception as exc:
            await log_system_event(
                "ai_job", started, status="error", error_msg=str(exc)
            )
            raise

    ai_interval = ai_cfg.get("interval_minutes", 2)
    scheduler.add_job(
        ai_job,
        "interval",
        minutes=ai_interval,
        id="ai_rewrite",
        max_instances=1,
        coalesce=True,
    )

    # Topic synthesis job — generates synthetic articles from grouped content
    async def topic_synthesis_job():
        started = datetime.now(timezone.utc)
        current_cfg = _load_config()
        ai = current_cfg.get("ai", {})
        synthesis_cfg = ai.get("topic_synthesis", {})

        if not ai.get("enabled", True) or not synthesis_cfg.get("enabled", False):
            return

        wh = current_cfg.get("webhook", {})
        tg = current_cfg.get("telegram", {})

        # Only dispatch to hooks that explicitly opted in to synthetic articles
        def _active(ep): return ep.get("enabled", True)
        endpoints  = [e for e in wh.get("endpoints", []) if _active(e) and e.get("ai_mode") == "synthetic"]
        tg_channels = [c for c in tg.get("channels", []) if _active(c) and c.get("ai_mode") == "synthetic"]

        if not endpoints and not tg_channels:
            return  # no hooks want synthetic articles

        # Resolve provider: synthesis can use its own provider or fall back to AI summary provider
        synth_provider_id = synthesis_cfg.get("provider_id") or ai.get("provider_id")
        synth_api_key, synth_base_url, synth_model_override = _resolve_provider(ai, synth_provider_id)
        synth_model = synth_model_override or "gpt-4o-mini"

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

        # Build list of (hook_id, webhook_endpoints, telegram_channels) per hook
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
            await log_system_event(
                "topic_synthesis_job", started, status="error", error_msg=str(exc)
            )
            logger.error(f"Topic synthesis job failed: {exc}", exc_info=True)
            raise

    synthesis_interval = ai_cfg.get("topic_synthesis", {}).get(
        "interval_minutes", 5
    )
    scheduler.add_job(
        topic_synthesis_job,
        "interval",
        minutes=synthesis_interval,
        id="topic_synthesis",
        max_instances=1,
        coalesce=True,
    )

    _scheduler = scheduler
    return scheduler

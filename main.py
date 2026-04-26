"""
Entry point: FastAPI app + APScheduler trong cùng process.
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
import uvicorn
import yaml  # still used in __main__ block
from dotenv import load_dotenv
from fastapi import FastAPI

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# Reduce noise from APScheduler
logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
logging.getLogger("apscheduler.scheduler").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Import app sau load_dotenv để env vars có sẵn
from dashboard.app import app as dashboard_app, get_redis
from dashboard.config_io import on_settings_saved
from jobs.scheduler import get_scheduler, reload_scheduler_jobs
from storage.sqlite_stats import init_db
from vector_db.weaviate_store import init_weaviate, close_weaviate
from storage.lake_store import init_lake
from webhook.dispatcher import dispatch_worker, init_dispatcher


DEV_MODE = os.environ.get("DEV_MODE", "0") == "1"


async def _rebuild_dedup_set(redis: aioredis.Redis) -> None:
    """
    Rebuild the SimHash dedup set from existing article titles in Redis on startup.

    Rationale: dedup.py now uses stable md5-based hashing, but any set persisted
    before this fix (or from a prior session) has unstable hash() fingerprints.
    Rather than flushing and letting the first crawl tick re-save everything,
    we rebuild the set from current articles — so the first tick correctly dedupes.

    Uses a pipeline: fetch all IDs from news:feed → batch HMGET titles → sadd all hashes.
    Capped at 2000 recent articles to keep startup fast.
    """
    from crawler.dedup import _simhash
    from storage.redis_keys import DEDUP_SIMHASHES_KEY, AI_DEDUP_SIMHASHES_KEY, DEDUP_TTL_SECONDS

    await redis.delete(DEDUP_SIMHASHES_KEY, AI_DEDUP_SIMHASHES_KEY)

    # Get up to 2000 most recent article IDs
    ids = await redis.zrevrange("news:feed", 0, 1999)
    if not ids:
        logger.info("Dedup set rebuilt: no articles in Redis")
        return

    # Batch fetch titles in a single pipeline
    pipe = redis.pipeline()
    for aid in ids:
        pipe.hget(f"news:{aid.decode() if isinstance(aid, bytes) else aid}", "title")
    titles = await pipe.execute()

    # Compute all hashes and add in one SADD call
    # Error handling: skip corrupt titles to prevent startup crash
    hashes = []
    skipped = 0
    for title_raw in titles:
        try:
            if title_raw:
                title = title_raw.decode() if isinstance(title_raw, bytes) else title_raw
                hashes.append(_simhash(title))
        except Exception as e:
            skipped += 1
            logger.warning(f"Dedup rebuild: skipping corrupt title: {e}")
            continue

    if hashes:
        await redis.sadd(DEDUP_SIMHASHES_KEY, *hashes)
        await redis.expire(DEDUP_SIMHASHES_KEY, DEDUP_TTL_SECONDS)

    logger.info(f"Dedup set rebuilt: {len(hashes)} fingerprints from {len(ids)} articles (skipped {skipped} corrupt)")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting News Aggregator%s...", " [DEV MODE]" if DEV_MODE else "")

    # Parallel init: SQLite and Weaviate are independent — run concurrently
    await asyncio.gather(init_db(), init_weaviate())
    logger.info("SQLite + Weaviate initialized")

    # Single config read — reused for lake and scheduler config below
    from storage.config_cache import cached_yaml
    _startup_cfg = cached_yaml("config/settings.yaml")
    lake_cfg = _startup_cfg.get("lake", {})
    if lake_cfg.get("enabled", False):
        init_lake(lake_cfg)

    redis = get_redis()
    init_dispatcher(redis)

    await _rebuild_dedup_set(redis)

    _dispatch_task = None
    scheduler = None

    if not DEV_MODE:
        _cfg = _startup_cfg  # reuse already-loaded config
        _cr = _cfg.get("crawler", {})
        _ai = _cfg.get("ai", {})
        scheduler = get_scheduler(redis)
        on_settings_saved(reload_scheduler_jobs)
        scheduler.start()
        ci = _cr.get("fetch_interval_minutes", 3)
        sg = _cr.get("stagger_groups", 3)
        aim = _ai.get("interval_minutes", 2)
        logger.info(
            f"Scheduler started — crawl every {ci}min ({sg} groups, full cycle ~{ci*sg}min), "
            f"AI every {aim}min (batch {_ai.get('batch_size', 10)})"
        )
        scheduler.get_job("crawl_all").modify(next_run_time=__import__("datetime").datetime.now())
        _dispatch_task = asyncio.create_task(dispatch_worker())
    else:
        logger.info("DEV MODE: scheduler and dispatch worker skipped")

    yield

    logger.info("Shutting down gracefully...")
    
    # Step 1: Stop scheduler (wait for running jobs to complete, max 10s)
    if scheduler:
        logger.info("Stopping scheduler (waiting for running jobs)...")
        try:
            # shutdown(wait=True) blocks until all jobs finish
            # Use asyncio.wait_for to add timeout protection
            await asyncio.wait_for(
                asyncio.to_thread(scheduler.shutdown, wait=True),
                timeout=10.0
            )
            logger.info("Scheduler stopped cleanly")
        except asyncio.TimeoutError:
            logger.warning("Scheduler shutdown timeout — forcing stop")
            scheduler.shutdown(wait=False)
    
    # Step 2: Cancel dispatch worker
    if _dispatch_task:
        _dispatch_task.cancel()
        try:
            await _dispatch_task
        except asyncio.CancelledError:
            pass
    
    # Step 3: Close external connections
    await close_weaviate()
    await redis.aclose()
    logger.info("Shutdown complete")


# Override lifespan
dashboard_app.router.lifespan_context = lifespan


if __name__ == "__main__":
    # Resolve dashboard host/port via config_io — respects CONFIG_BACKEND
    # (yaml or db). This is the only place __main__ runs config-dependent
    # code before uvicorn starts.
    from dashboard.config_io import read_settings
    try:
        dash_cfg = read_settings().get("dashboard", {})
    except Exception as exc:
        # Backend unavailable (DB down, missing yaml) — fall back to defaults
        # so the container at least starts and reports the underlying error
        # via the lifespan / health endpoint.
        logger.warning(f"read_settings failed at startup, using defaults: {exc}")
        dash_cfg = {}

    port = int(os.environ.get("DEV_PORT", dash_cfg.get("port", 8000)))
    uvicorn.run(
        "main:dashboard_app",
        host=dash_cfg.get("host", "0.0.0.0"),
        port=port,
        reload=False,
        log_level="info",
    )

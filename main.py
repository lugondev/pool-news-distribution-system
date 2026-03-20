"""
Entry point: FastAPI app + APScheduler trong cùng process.
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
import uvicorn
import yaml
from dotenv import load_dotenv
from fastapi import FastAPI

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Import app sau load_dotenv để env vars có sẵn
from dashboard.app import app as dashboard_app, get_redis
from scheduler import get_scheduler
from storage.sqlite_stats import init_db
from webhook.dispatcher import dispatch_worker


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting News Aggregator...")
    await init_db()
    logger.info("SQLite initialized")

    redis = get_redis()
    with open("config/settings.yaml") as f:
        _cfg = yaml.safe_load(f)
    _cr = _cfg.get("crawler", {})
    _ai = _cfg.get("ai", {})
    scheduler = get_scheduler(redis)
    scheduler.start()
    ci = _cr.get("fetch_interval_minutes", 3)
    sg = _cr.get("stagger_groups", 3)
    aim = _ai.get("interval_minutes", 2)
    logger.info(
        f"Scheduler started — crawl every {ci}min ({sg} groups, full cycle ~{ci*sg}min), "
        f"AI every {aim}min (batch {_ai.get('batch_size', 10)})"
    )

    # Trigger crawl ngay khi start
    scheduler.get_job("crawl_all").modify(next_run_time=__import__("datetime").datetime.now())

    # Dispatch worker: dequeues webhook/Telegram jobs independently of AI loop
    _dispatch_task = asyncio.create_task(dispatch_worker())

    yield

    _dispatch_task.cancel()
    try:
        await _dispatch_task
    except asyncio.CancelledError:
        pass
    scheduler.shutdown(wait=False)
    await redis.aclose()
    logger.info("Shutdown complete")


# Override lifespan
dashboard_app.router.lifespan_context = lifespan


if __name__ == "__main__":
    with open("config/settings.yaml") as f:
        cfg = yaml.safe_load(f)
    dash_cfg = cfg.get("dashboard", {})

    uvicorn.run(
        "main:dashboard_app",
        host=dash_cfg.get("host", "0.0.0.0"),
        port=dash_cfg.get("port", 8000),
        reload=False,
        log_level="info",
    )

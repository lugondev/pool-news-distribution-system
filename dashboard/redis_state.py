"""Shared Redis singleton for dashboard modules.

app.py initializes the Redis connection and calls set_redis() once at startup.
All sub-routers call get_redis() to access the shared instance.
"""

import redis.asyncio as aioredis

_redis: aioredis.Redis | None = None


def set_redis(r: aioredis.Redis) -> None:
    global _redis
    _redis = r


def get_redis() -> aioredis.Redis:
    if _redis is None:
        raise RuntimeError("Redis not initialized — call set_redis() at app startup")
    return _redis

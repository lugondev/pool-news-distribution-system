"""Centralized Redis key constants for the news aggregator."""

# Article hash: news:{article_id}
ARTICLE_PREFIX = "news:"

# Feed sorted sets (score=timestamp, member=article_id)
FEED_KEY = "news:feed"
FEED_DATE_PREFIX = "news:feed:"  # daily: news:feed:YYYYMMDD, hourly: news:feed:YYYYMMDDHH

# AI processing queue — sorted set
AI_PENDING_KEY = "news:ai:pending"

# Crawl scheduling — sorted set (score=next_crawl_at unix ts)
CRAWL_SCHEDULE_KEY = "news:crawl:schedule"

# Deduplication SimHash fingerprint sets
DEDUP_SIMHASHES_KEY = "news:dedup:simhashes"
AI_DEDUP_SIMHASHES_KEY = "news:ai:dedup:simhashes"

# TTLs
ARTICLE_TTL_SECONDS = 43200   # 12h — article hashes + feed sets
DEDUP_TTL_SECONDS = 86400     # 24h — dedup sets live longer than articles

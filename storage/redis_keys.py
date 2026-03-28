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

# Phase 2 — Enrichment + Embeddings + Clustering
ENRICH_PENDING_KEY = "news:enrich:pending"        # Set: article_ids queued for enrichment
EMBED_PREFIX = "news:embed:"                       # news:embed:{article_id} → JSON list[float]
TOPIC_CENTROID_PREFIX = "news:topic:centroid:"     # news:topic:centroid:{topic_id} → JSON list
TOPIC_META_PREFIX = "news:topic:meta:"             # news:topic:meta:{topic_id} → Hash
TOPIC_CAT_PREFIX = "news:topics:cat:"             # news:topics:cat:{category} → Set of topic_ids

# Phase 3 — Story Detection
# A "story" = same real-world event tracked across sources + time
STORY_PREFIX = "news:story:"                       # news:story:{story_id} → Hash
STORY_ARTICLES_PREFIX = "news:story:articles:"     # news:story:articles:{story_id} → Sorted Set (score=ts)
STORIES_ACTIVE_KEY = "news:stories:active"         # Sorted Set (score=last_updated, member=story_id)
STORIES_CAT_PREFIX = "news:stories:cat:"           # news:stories:cat:{category} → Sorted Set
STORY_TTL_SECONDS = 172800                         # 48h — stories live longer than articles

# Phase 3 — Trend Detection
TREND_CAT_PREFIX = "news:trend:cat:"               # news:trend:cat:{category} → Hash (velocity, ratio, ...)
TRENDS_RANKING_KEY = "news:trends:ranking"         # Sorted Set (score=ratio, member=category)
TREND_ENTITIES_KEY = "news:trend:entities"         # Sorted Set (score=count, member=entity)
TREND_TTL_SECONDS = 3600                           # 1h — trend scores are refreshed each run

# Phase 3 — Newsletter
NEWSLETTER_LATEST_KEY = "news:newsletter:latest"       # String: HTML content of latest newsletter
NEWSLETTER_LATEST_AT_KEY = "news:newsletter:latest_at" # String: ISO timestamp of last generation
NEWSLETTER_TTL_SECONDS = 86400                         # 24h

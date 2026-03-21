"""
Shared article filtering and rate-limiting utilities for webhook and Telegram dispatchers.

Each endpoint/channel config supports:
  filter_categories_mode: all | include | exclude
  filter_categories: [list of category ids]
  filter_sources_mode: all | include | exclude
  filter_sources: [list of source ids]
  rate_limit_max: int (0 = unlimited)
  rate_limit_window_minutes: int (default 60)
"""
import logging
import time
from collections import defaultdict, deque

logger = logging.getLogger(__name__)

# Sliding-window counters: endpoint_id → deque of monotonic timestamps
_rate_counters: dict[str, deque] = defaultdict(deque)


def passes_filter(article: dict, config: dict) -> bool:
    """Return True if the article passes the category and source filters in config."""
    category = article.get("category", "")
    source_id = article.get("source_id", "")

    cat_mode = config.get("filter_categories_mode", "all")
    cat_list = config.get("filter_categories") or []
    if cat_mode == "include" and cat_list:
        if category not in cat_list:
            return False
    elif cat_mode == "exclude" and cat_list:
        if category in cat_list:
            return False

    src_mode = config.get("filter_sources_mode", "all")
    src_list = config.get("filter_sources") or []
    if src_mode == "include" and src_list:
        if source_id not in src_list:
            return False
    elif src_mode == "exclude" and src_list:
        if source_id in src_list:
            return False

    return True


def check_rate_limit(endpoint_id: str, config: dict) -> bool:
    """
    Return True if the endpoint is within its rate limit, False if throttled.
    If within limit, records this send so future calls count it.
    """
    max_msgs = config.get("rate_limit_max", 0)
    if not max_msgs:
        return True  # unlimited

    window_minutes = config.get("rate_limit_window_minutes", 60)
    window_seconds = window_minutes * 60
    now = time.monotonic()

    dq = _rate_counters[endpoint_id]
    # Evict timestamps outside the sliding window
    while dq and now - dq[0] > window_seconds:
        dq.popleft()

    if len(dq) >= max_msgs:
        logger.info(
            f"Rate limit: {endpoint_id} hit {max_msgs}/{window_minutes}min — skipping"
        )
        return False

    dq.append(now)
    return True

"""
Article priority scorer for the AI processing queue.

Score formula:  published_at_unix + priority_bonus_seconds

Keeps everything in timestamp-space so ZPOPMAX semantics stay intact:
  - higher score  →  processed first
  - bonus shifts an article "forward in time" by up to max_bonus_seconds
  - a 1800s bonus means a trusted source from 30min ago beats a fresh unknown source

Configured via config/settings.yaml → scoring section.
"""
from __future__ import annotations

import logging

import yaml

from crawler.rss_parser import Article

logger = logging.getLogger(__name__)


def _load_scoring_config() -> dict:
    with open("config/settings.yaml") as f:
        cfg = yaml.safe_load(f)
    return cfg.get("scoring", {})


def score_article(article: Article, cfg: dict | None = None) -> float:
    """
    Return a priority score for the AI pending queue.

    Falls back to raw published_at timestamp when scoring is disabled
    or config is missing — preserving the existing FIFO-by-recency behaviour.
    """
    if cfg is None:
        cfg = _load_scoring_config()

    if not cfg.get("enabled", True):
        return article.published_at.timestamp()

    ts = article.published_at.timestamp()
    bonus = 0.0

    # 1. Source weight — trusted outlets get a time-equivalent boost
    trusted: set[str] = set(cfg.get("trusted_sources", []))
    if article.source_id in trusted:
        bonus += float(cfg.get("source_bonus_seconds", 1800))

    # 2. Category weight — high-signal categories processed sooner
    category_weights: dict[str, int] = cfg.get("category_weights", {})
    bonus += float(category_weights.get(article.category, 0))

    # 3. Keyword weight — breaking / named-entity signals in title
    title_lower = article.title.lower()
    for entry in cfg.get("boost_keywords", []):
        if isinstance(entry, list) and len(entry) == 2:
            kw, w = entry[0], entry[1]
            if str(kw).lower() in title_lower:
                bonus += float(w)

    # Cap bonus to avoid completely starving fresh low-signal articles
    max_bonus = float(cfg.get("max_bonus_seconds", 7200))
    bonus = min(bonus, max_bonus)

    return ts + bonus

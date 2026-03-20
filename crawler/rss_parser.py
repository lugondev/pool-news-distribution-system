"""
RSS parser + basic web scraper.
Trả về list Article từ feed URL.
"""
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import feedparser
import httpx
from bs4 import BeautifulSoup
from langdetect import detect, LangDetectException


@dataclass
class Article:
    id: str
    source_id: str
    source_name: str
    url: str
    title: str
    summary: str
    content: str
    lang: str                      # detected language
    declared_lang: str             # language declared in sources.yaml
    category: str
    published_at: datetime
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # AI output (filled later)
    ai_summary_vi: str = ""
    ai_summary_en: str = ""
    ai_status: str = "pending"     # pending | done | error


def _make_article_id(source_id: str, url: str) -> str:
    return hashlib.sha256(f"{source_id}:{url}".encode()).hexdigest()[:16]


def _strip_html(text: str) -> str:
    if not text:
        return ""
    soup = BeautifulSoup(text, "lxml")
    return re.sub(r"\s+", " ", soup.get_text()).strip()


def _detect_lang(text: str, fallback: str = "en") -> str:
    try:
        return detect(text[:500])
    except LangDetectException:
        return fallback


def _parse_date(entry: Any) -> datetime:
    for attr in ("published_parsed", "updated_parsed", "created_parsed"):
        t = getattr(entry, attr, None)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return datetime.now(timezone.utc)


async def parse_rss_feed(
    source: dict,
    client: httpx.AsyncClient,
    max_articles: int = 50,
) -> list[Article]:
    """Fetch + parse RSS feed. Returns list of Article."""
    try:
        resp = await client.get(source["url"], timeout=15)
        resp.raise_for_status()
    except Exception as e:
        raise RuntimeError(f"[{source['id']}] fetch failed: {e}") from e

    feed = feedparser.parse(resp.text)
    articles = []

    for entry in feed.entries[:max_articles]:
        url = getattr(entry, "link", "")
        title = getattr(entry, "title", "").strip()
        if not url or not title:
            continue

        # Extract summary/content
        summary = ""
        if hasattr(entry, "summary"):
            summary = _strip_html(entry.summary)
        content = ""
        if hasattr(entry, "content") and entry.content:
            content = _strip_html(entry.content[0].value)

        text_for_detect = title + " " + (summary or content)
        detected_lang = _detect_lang(text_for_detect, fallback=source.get("lang", "en"))

        article = Article(
            id=_make_article_id(source["id"], url),
            source_id=source["id"],
            source_name=source.get("name", source["id"]),
            url=url,
            title=title,
            summary=summary[:500],
            content=content[:2000],
            lang=detected_lang,
            declared_lang=source.get("lang", "en"),
            category=source.get("category", "general"),
            published_at=_parse_date(entry),
        )
        articles.append(article)

    return articles

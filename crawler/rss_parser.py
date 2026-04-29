"""
RSS parser + basic web scraper.
Trả về list Article từ feed URL.
"""

import asyncio
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
    lang: str  # detected language
    declared_lang: str  # language declared in sources.yaml
    category: str
    published_at: datetime
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    image_url: str = ""  # thumbnail/cover image extracted from RSS
    # AI output (filled later)
    ai_summary_vi: str = ""
    ai_summary_en: str = ""
    ai_status: str = "pending"  # pending | done | error
    type: str = (
        "original"  # article type: "original" (RSS) or "synthetic" (AI-generated)
    )
    # Phase 2 enrichment (filled after AI summarize)
    entities: list[str] = field(default_factory=list)
    sentiment: str = ""          # positive | negative | neutral
    topic_id: str = ""
    ai_enrich_status: str = "pending"  # pending | done | error | skip


def _make_article_id(source_id: str, url: str) -> str:
    return hashlib.sha256(f"{source_id}:{url}".encode()).hexdigest()[:16]


def _strip_html(text: str) -> str:
    if not text or not text.strip():
        return ""
    # Avoid BeautifulSoup warning when text looks like a filename
    if len(text) < 2 or (len(text) < 100 and '/' in text and '<' not in text):
        return text.strip()
    soup = BeautifulSoup(text, "lxml")
    return re.sub(r"\s+", " ", soup.get_text()).strip()


def _detect_lang(text: str, fallback: str = "en") -> str:
    try:
        return detect(text[:500])
    except LangDetectException:
        return fallback


# URL fragments that indicate tracking pixels rather than real images.
_TRACKING_PIXEL_HINTS = ("/pixel", "1x1", "/track", "/beacon", "doubleclick", "/p.gif")


def _is_real_image_url(url: str) -> bool:
    if not url or not url.startswith(("http://", "https://")):
        return False
    lower = url.lower()
    return not any(h in lower for h in _TRACKING_PIXEL_HINTS)


def _extract_image(entry: Any, summary_html: str, content_html: str) -> str:
    """Pick a thumbnail URL from an RSS entry. Priority:
    media:thumbnail → media:content (image) → enclosures (image) → first <img>.
    Returns "" if nothing usable is found."""
    # 1. media:thumbnail
    thumbs = getattr(entry, "media_thumbnail", None) or []
    for t in thumbs:
        url = (t or {}).get("url", "")
        if _is_real_image_url(url):
            return url

    # 2. media:content with type=image/* or medium=image
    media = getattr(entry, "media_content", None) or []
    for m in media:
        if not isinstance(m, dict):
            continue
        mtype = (m.get("type") or "").lower()
        medium = (m.get("medium") or "").lower()
        if medium == "image" or mtype.startswith("image/"):
            url = m.get("url", "")
            if _is_real_image_url(url):
                return url

    # 3. enclosures (RSS 2.0)
    for enc in getattr(entry, "enclosures", None) or []:
        if not isinstance(enc, dict):
            continue
        if (enc.get("type") or "").lower().startswith("image/"):
            url = enc.get("href") or enc.get("url", "")
            if _is_real_image_url(url):
                return url

    # 4. first <img> in summary or content HTML
    for html in (summary_html, content_html):
        if not html or "<img" not in html:
            continue
        try:
            soup = BeautifulSoup(html, "lxml")
            img = soup.find("img")
            if img:
                src = img.get("src") or img.get("data-src") or ""
                if _is_real_image_url(src):
                    return src
        except Exception:
            continue
    return ""


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
    max_retries = 2
    for attempt in range(max_retries + 1):
        try:
            resp = await client.get(source["url"], timeout=15)
            if resp.status_code == 429 and attempt < max_retries:
                retry_after = int(resp.headers.get("Retry-After", 5))
                await asyncio.sleep(min(retry_after, 30))
                continue
            resp.raise_for_status()
            break
        except httpx.HTTPStatusError:
            raise
        except Exception as e:
            if attempt < max_retries:
                await asyncio.sleep(2**attempt)
                continue
            raise RuntimeError(f"[{source['id']}] fetch failed: {e}") from e

    feed = feedparser.parse(resp.text)
    articles = []

    for entry in feed.entries[:max_articles]:
        url = getattr(entry, "link", "")
        title = getattr(entry, "title", "").strip()
        if not url or not title:
            continue

        # Extract summary/content (keep raw HTML for image extraction first)
        summary_html = getattr(entry, "summary", "") or ""
        content_html = ""
        if hasattr(entry, "content") and entry.content:
            content_html = entry.content[0].value or ""

        summary = _strip_html(summary_html)
        content = _strip_html(content_html)
        image_url = _extract_image(entry, summary_html, content_html)

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
            image_url=image_url,
        )
        articles.append(article)

    return articles

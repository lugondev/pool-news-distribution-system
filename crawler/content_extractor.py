"""
Full-page content extractor using defuddle (Node.js subprocess).
Falls back to BeautifulSoup CSS-selector extraction for CSR/Next.js sites
where defuddle returns empty or raw JS data.

Flow:
  1. needs_enrichment(content, summary) → decide if we should fetch the full page
  2. fetch article URL HTML via existing httpx.AsyncClient
  3. _run_defuddle(url, html) → Node.js extracts main content (returns HTML)
  4. strip HTML tags from defuddle output
  5. if defuddle result is bad/short → _bs_fallback(html) using article CSS selectors
  6. update article.content if any result is richer than existing content
"""
import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup
import httpx

logger = logging.getLogger(__name__)

_WORKER = os.path.join(os.path.dirname(__file__), "defuddle_worker.js")

# Common article body CSS selectors, tried in order (most specific → broadest)
_ARTICLE_SELECTORS = [
    "article",
    "[itemprop='articleBody']",
    ".article-body",
    ".article-content",
    ".article__body",
    ".post-content",
    ".story-body",
    ".entry-content",
    "[class*='article'][class*='body']",
    "[class*='article'][class*='content']",
    "main p",
    # Broader fallbacks for CSS-module class names (e.g. KoreaTimes, Next.js apps)
    "[class*='content']",
    "[class*='Content']",
    "[class*='body']",
    "[class*='Body']",
]

# Minimum char length for a result to be considered "good"
_MIN_GOOD_LEN = 200


@dataclass
class DefuddleResult:
    content: str
    title: str
    description: str


def needs_enrichment(content: str, summary: str) -> bool:
    """Return True if the existing RSS content is too thin → fetch full article page."""
    return not content or len(content) < len(summary)


def _looks_like_js_data(text: str) -> bool:
    """Detect if extracted text is raw JS/JSON data instead of article prose."""
    prefixes = ("self.__next_f", '{"@context"', "window.__", "var __", "__NEXT_DATA__")
    stripped = text.lstrip()
    return any(stripped.startswith(p) for p in prefixes)


def _bs_fallback(html: str) -> str:
    """
    BeautifulSoup fallback for CSR/Next.js sites where defuddle returns empty.
    Tries common article CSS selectors and returns the longest match.
    """
    soup = BeautifulSoup(html, "lxml")
    # Remove noise elements
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "noscript"]):
        tag.decompose()

    best = ""
    for selector in _ARTICLE_SELECTORS:
        try:
            elements = soup.select(selector)
        except Exception:
            continue
        if not elements:
            continue
        text = re.sub(r"\s+", " ", " ".join(el.get_text() for el in elements)).strip()
        if len(text) > len(best):
            best = text

    return best


async def _run_defuddle(url: str, html: str, timeout: float = 12.0) -> DefuddleResult:
    """Invoke the Node.js defuddle worker and return structured result."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "node", _WORKER,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        payload = json.dumps({"url": url, "html": html}).encode()
        stdout, _ = await asyncio.wait_for(proc.communicate(payload), timeout=timeout)
        data = json.loads(stdout.decode())
        if "error" in data:
            logger.debug(f"defuddle worker error ({url}): {data['error']}")

        raw_html = data.get("content", "")
        text = ""
        if raw_html:
            soup = BeautifulSoup(raw_html, "lxml")
            text = re.sub(r"\s+", " ", soup.get_text()).strip()

        return DefuddleResult(
            content=text,
            title=data.get("title", ""),
            description=data.get("description", ""),
        )
    except asyncio.TimeoutError:
        logger.debug(f"defuddle timed out for {url}")
        return DefuddleResult(content="", title="", description="")
    except Exception as e:
        logger.debug(f"defuddle failed for {url}: {e}")
        return DefuddleResult(content="", title="", description="")


async def enrich_article_content(
    url: str,
    existing_content: str,
    existing_summary: str,
    client: httpx.AsyncClient,
    max_content_len: int = 5000,
) -> str:
    """
    Fetch full article page and extract content via defuddle + BS4 fallback.
    Returns the best content string available (defuddle > BS4 fallback > original).
    """
    if not needs_enrichment(existing_content, existing_summary):
        return existing_content

    try:
        resp = await client.get(url, timeout=15)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        logger.debug(f"failed to fetch article page {url}: {e}")
        return existing_content

    # Try defuddle first
    result = await _run_defuddle(url, html)
    defuddle_text = result.content

    # Reject defuddle result if it's raw JS data or too short
    if defuddle_text and not _looks_like_js_data(defuddle_text) and len(defuddle_text) >= _MIN_GOOD_LEN:
        logger.info(f"defuddle enriched {url[:60]}: {len(existing_content)} → {len(defuddle_text)} chars")
        return defuddle_text[:max_content_len]

    # Fallback: BeautifulSoup CSS selector extraction
    bs_text = _bs_fallback(html)
    if bs_text and len(bs_text) > len(existing_content):
        logger.info(f"bs4 fallback enriched {url[:60]}: {len(existing_content)} → {len(bs_text)} chars")
        return bs_text[:max_content_len]

    logger.debug(f"enrichment yielded no improvement for {url[:60]}")
    return existing_content

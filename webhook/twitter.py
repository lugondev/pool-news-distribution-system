"""
X (Twitter) dispatcher — posts AI-rewritten articles as tweets.

Mirrors the pattern of webhook/telegram.py: called from dispatcher.dispatch_article()
with a list of account configs from settings.yaml.

Config shape (under twitter.accounts[]):
  - id: "main-account"
    enabled: true
    is_premium: false          # true = X Premium/Premium+ (25,000 char limit)
                               # false = free/basic (280 char limit)
    api_key: "..."
    api_secret: "..."
    access_token: "..."
    access_token_secret: "..."
    bearer_token: "..."        # optional — needed only for read operations
    ai_mode: "rewrite"         # same semantics as telegram: rewrite | off | synthetic
    include_hashtags: true     # append #CategoryName hashtag
    lang: "vi"                 # prefer vi summary; "en" | "vi" | "both" | "origin"
    filter_categories_mode: "all"
    filter_categories: []
    filter_article_types_mode: "all"
    filter_article_types: []

Character limits by tier:
  Free / Basic  : 280 characters  (is_premium: false)
  Premium       : 25,000 characters (is_premium: true)
  URL cost      : always 23 chars regardless of actual URL length (Twitter t.co)

Twitter v2 rate limits (free tier):
  50 tweets per 24h per app. The dispatcher never exceeds this; articles are
  rate-limited upstream in check_rate_limit() (webhook/filters.py).

Implementation note:
  tweepy.Client is synchronous — we run it in asyncio.to_thread() to avoid
  blocking the event loop, same approach as boto3 in storage/lake_store.py.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

from storage.sqlite_stats import log_telegram  # reuse log table (twitter channel)

try:
    from realtime.manager import ws_manager
except ImportError:
    ws_manager = None

from webhook.filters import check_rate_limit, passes_filter

logger = logging.getLogger(__name__)

# Twitter counts all URLs as exactly this many characters (t.co shortener)
TWITTER_URL_LENGTH = 23
TWITTER_MAX_CHARS_FREE = 280
TWITTER_MAX_CHARS_PREMIUM = 25000

CATEGORY_HASHTAG = {
    "tech": "#Tech", "ai": "#AI", "finance": "#Finance", "world": "#WorldNews",
    "business": "#Business", "politics": "#Politics", "science": "#Science",
    "gaming": "#Gaming", "esports": "#Esports", "sports": "#Sports",
    "entertainment": "#Entertainment", "music": "#Music",
}


# ── Tweet formatting ──────────────────────────────────────────────────────────

def _pick_summary(article: dict, lang: str) -> str:
    """Return best available AI summary for the given lang preference."""
    if lang == "vi":
        return article.get("ai_summary_vi") or article.get("ai_summary_en") or article.get("summary", "")
    if lang == "en":
        return article.get("ai_summary_en") or article.get("ai_summary_vi") or article.get("summary", "")
    if lang == "origin":
        return article.get("ai_summary_origin") or article.get("ai_summary_vi") or article.get("summary", "")
    # "both" or fallback — prefer vi
    return article.get("ai_summary_vi") or article.get("ai_summary_en") or article.get("summary", "")


def format_tweet(article: dict, account: dict) -> str:
    """
    Build tweet text from an article.

    Character budget (free account):
      280 total
      - 23 (URL via t.co, always fixed cost)
      - 1  (space before URL)
      - optional hashtag + 1 space
      = ~246 remaining for title + summary

    Character budget (premium account, is_premium=true):
      25,000 total — full summary, no truncation in practice

    Format:
      {title}

      {summary}

      {url} #{hashtag}
    """
    title = article.get("title", "").strip()
    url = article.get("url", "")
    category = article.get("category", "")
    lang = account.get("lang", "vi")
    include_hashtags = account.get("include_hashtags", True)
    is_premium = account.get("is_premium", False)

    max_chars = TWITTER_MAX_CHARS_PREMIUM if is_premium else TWITTER_MAX_CHARS_FREE

    hashtag = CATEGORY_HASHTAG.get(category, "") if include_hashtags else ""
    summary = _pick_summary(article, lang).strip()

    # Calculate available space for text (title + newlines + summary)
    # URL counts as 23 chars (t.co) + 1 space; hashtag + 1 space if present
    url_cost = TWITTER_URL_LENGTH + 1
    hashtag_cost = len(hashtag) + 1 if hashtag else 0
    header = f"{title}\n\n" if title else ""
    budget = max_chars - url_cost - hashtag_cost - len(header)

    if len(summary) > budget:
        summary = summary[: budget - 1] + "…"

    parts = []
    if title:
        parts.append(title)
    if summary:
        parts.append(summary)

    text = "\n\n".join(parts)
    tail = url
    if hashtag:
        tail = f"{url} {hashtag}"

    return f"{text}\n\n{tail}" if text else tail


# ── Tweepy client (lazy import — tweepy is optional) ─────────────────────────

def _post_tweet_sync(text: str, account: dict) -> dict:
    """
    Synchronous tweepy call — run via asyncio.to_thread().
    Returns {"id": tweet_id} on success, raises on failure.
    """
    try:
        import tweepy
    except ImportError:
        raise RuntimeError("tweepy not installed — run: pip install tweepy")

    client = tweepy.Client(
        consumer_key=account["api_key"],
        consumer_secret=account["api_secret"],
        access_token=account["access_token"],
        access_token_secret=account["access_token_secret"],
    )
    resp = client.create_tweet(text=text)
    return {"id": str(resp.data["id"])}


# ── Public dispatcher ─────────────────────────────────────────────────────────

async def dispatch_to_twitter(
    article: dict,
    accounts: list[dict],
) -> None:
    """
    Post an article to all enabled Twitter accounts.
    Called from dispatcher.dispatch_article() with twitter_accounts=[...].
    """
    if not accounts:
        return

    for account in accounts:
        if not account.get("enabled", True):
            continue
        acc_id = account.get("id", "twitter")

        if not passes_filter(article, account):
            logger.debug(f"[twitter] account={acc_id} filtered out {article.get('id', '?')}")
            continue

        if not check_rate_limit(acc_id, account):
            logger.debug(f"[twitter] account={acc_id} rate limited")
            continue

        tweet_text = format_tweet(article, account)
        started = datetime.now(timezone.utc)
        t0 = time.monotonic()
        status = "error"
        error_msg = None

        try:
            result = await asyncio.to_thread(_post_tweet_sync, tweet_text, account)
            tweet_id = result.get("id", "")
            duration_ms = int((time.monotonic() - t0) * 1000)
            status = "ok"
            logger.info(
                f"[twitter] posted tweet id={tweet_id} "
                f"article={article.get('id', '?')} account={acc_id} ({duration_ms}ms)"
            )
            if ws_manager:
                asyncio.create_task(ws_manager.broadcast("twitter.sent", {
                    "article_id": article.get("id"),
                    "account_id": acc_id,
                    "tweet_id": tweet_id,
                }))
        except Exception as exc:
            error_msg = str(exc)[:300]
            duration_ms = int((time.monotonic() - t0) * 1000)
            logger.warning(f"[twitter] failed account={acc_id} article={article.get('id', '?')}: {exc}")

        # Reuse telegram log table (channel_id = account id, channel_name = "twitter:{id}")
        try:
            await log_telegram(
                channel_id=acc_id,
                channel_name=f"twitter:{acc_id}",
                article_id=article.get("id", ""),
                article_title=article.get("title", ""),
                status=status,
                duration_ms=duration_ms,
                sent_at=started,
                error_msg=error_msg,
            )
        except Exception:
            pass

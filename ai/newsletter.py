"""
Newsletter Generator — daily digest of top AI-processed articles.

Workflow:
  1. Pull top N articles from the last 24h where ai_status = "done"
  2. Group by category, pick best per category
  3. Single AI call → structured newsletter JSON
  4. Render into HTML + plain-text
  5. Store in Redis (news:newsletter:latest) + optionally dispatch via webhook

The AI call returns JSON:
  {
    "subject": "Morning Briefing – March 28, 2026",
    "intro": "Two-sentence editorial overview of today's news",
    "sections": [
      {
        "category": "tech",
        "headline": "Section headline",
        "items": [
          {"title": "...", "summary": "...", "url": "...", "source": "..."}
        ]
      }
    ],
    "closing": "One-sentence sign-off"
  }
"""

import asyncio
import json
import logging
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timezone

import redis.asyncio as aioredis
from openai import RateLimitError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_not_exception_type

from ai.rewriter import get_openai_client, _load_ai_config
from ai.provider_utils import build_response_format, parse_ai_json, SCHEMA_NEWSLETTER
from storage.redis_keys import (
    NEWSLETTER_LATEST_KEY,
    NEWSLETTER_LATEST_AT_KEY,
    NEWSLETTER_TTL_SECONDS,
)

logger = logging.getLogger(__name__)

MAX_ARTICLES_PER_CATEGORY = 3
MAX_CATEGORIES = 8
MAX_CONTENT_CHARS = 300   # truncate article content for the prompt


NEWSLETTER_PROMPT = """You are an editor writing a professional news briefing.

Given the following articles grouped by category, produce a concise newsletter digest.

Articles:
{articles_json}

Today's date: {today}
Language: {language}

Output ONLY valid JSON (no markdown fences), with this exact structure:
{{
  "subject": "short email subject line (max 80 chars)",
  "intro": "2-sentence overview of today's top stories",
  "sections": [
    {{
      "category": "category name",
      "headline": "short section headline",
      "items": [
        {{
          "title": "article title",
          "summary": "2-3 sentence summary",
          "url": "article url",
          "source": "source name"
        }}
      ]
    }}
  ],
  "closing": "one sentence closing remark"
}}

Guidelines:
- Include at most {max_categories} categories, picking the most newsworthy
- Each section should have 1-{max_per_cat} items
- Keep summaries factual, no opinion
- Write in {language}
"""


# ── AI call ───────────────────────────────────────────────────────────────────

@retry(
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=1, min=2, max=8),
    retry=retry_if_not_exception_type(RateLimitError)
)
async def _call_newsletter_ai(
    articles_by_category: dict[str, list[dict]],
    language: str,
    api_key: str | None,
    base_url: str | None,
    model: str | None,
    max_tokens: int,
    temperature: float,
) -> dict:
    client = get_openai_client(api_key=api_key, base_url=base_url)
    if not model:
        model = _load_ai_config().get("model", "")
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")

    # Build compact article list for prompt
    flat_articles = []
    for category, arts in articles_by_category.items():
        for art in arts[:MAX_ARTICLES_PER_CATEGORY]:
            flat_articles.append({
                "category": category,
                "title": art.get("title", ""),
                "summary": (art.get("ai_summary_en") or art.get("ai_summary_vi") or art.get("summary", ""))[:MAX_CONTENT_CHARS],
                "url": art.get("url", ""),
                "source": art.get("source_name", ""),
            })

    prompt = NEWSLETTER_PROMPT.format(
        articles_json=json.dumps(flat_articles, ensure_ascii=False),
        today=today,
        language=language,
        max_categories=MAX_CATEGORIES,
        max_per_cat=MAX_ARTICLES_PER_CATEGORY,
    )

    create_kwargs: dict = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "response_format": build_response_format(base_url, "newsletter", SCHEMA_NEWSLETTER),
    }

    resp = await client.chat.completions.create(**create_kwargs)

    raw = resp.choices[0].message.content
    return parse_ai_json(raw, fallback={})


# ── HTML renderer ─────────────────────────────────────────────────────────────

def render_html(newsletter: dict) -> str:
    """Render newsletter JSON into a clean HTML email."""
    subject = newsletter.get("subject", "News Briefing")
    intro = newsletter.get("intro", "")
    sections = newsletter.get("sections", [])
    closing = newsletter.get("closing", "")
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")

    section_html = ""
    for sec in sections:
        items_html = "".join(
            f"""<div style="margin-bottom:12px;padding:12px;background:#1e2130;border-radius:6px;border-left:3px solid #6366f1">
              <a href="{item.get('url','#')}" style="color:#818cf8;font-weight:600;font-size:14px;text-decoration:none">{item.get('title','')}</a>
              <p style="color:#94a3b8;font-size:13px;margin:6px 0 4px">{item.get('summary','')}</p>
              <span style="color:#475569;font-size:11px">{item.get('source','')}</span>
            </div>"""
            for item in sec.get("items", [])
        )
        cat_label = sec.get("category", "").upper()
        section_html += f"""
        <div style="margin-bottom:24px">
          <div style="font-size:11px;font-weight:700;color:#6366f1;letter-spacing:0.08em;margin-bottom:8px;text-transform:uppercase">{cat_label}</div>
          <h2 style="color:#e2e8f0;font-size:16px;font-weight:600;margin:0 0 12px">{sec.get('headline','')}</h2>
          {items_html}
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{subject}</title>
</head>
<body style="margin:0;padding:0;background:#0f1117;font-family:'Inter',system-ui,sans-serif">
  <div style="max-width:640px;margin:0 auto;padding:24px 16px">

    <div style="margin-bottom:24px;padding-bottom:16px;border-bottom:1px solid #2a2d3e">
      <div style="font-size:11px;color:#475569;margin-bottom:4px">{today}</div>
      <h1 style="color:#e2e8f0;font-size:22px;font-weight:700;margin:0 0 10px">{subject}</h1>
      <p style="color:#94a3b8;font-size:14px;line-height:1.6;margin:0">{intro}</p>
    </div>

    {section_html}

    <div style="margin-top:24px;padding-top:16px;border-top:1px solid #2a2d3e">
      <p style="color:#475569;font-size:12px;margin:0">{closing}</p>
      <p style="color:#334155;font-size:11px;margin:8px 0 0">Generated by News Aggregator · {today}</p>
    </div>

  </div>
</body>
</html>"""


# ── Article fetching ──────────────────────────────────────────────────────────

async def _fetch_recent_done_articles(
    redis: aioredis.Redis,
    categories: list[str],
    max_per_category: int,
    lookback_seconds: int = 86400,
) -> dict[str, list[dict]]:
    """
    Fetch up to max_per_category AI-done articles per category from last 24h.
    Returns {category: [article_dict, ...]}
    """
    now_ts = datetime.now(timezone.utc).timestamp()
    cutoff = now_ts - lookback_seconds
    result: dict[str, list[dict]] = {}

    for category in categories:
        cat_key = f"news:cat:{category}"
        raw_ids = await redis.zrevrangebyscore(
            cat_key, now_ts, cutoff
        )
        if not raw_ids:
            continue

        article_ids = [b.decode() if isinstance(b, bytes) else b for b in raw_ids]

        pipe = redis.pipeline()
        for aid in article_ids:
            pipe.hgetall(f"news:{aid}")
        raws = await pipe.execute()

        done_articles = []
        for raw in raws:
            if not raw:
                continue
            art = {k.decode(): v.decode() for k, v in raw.items()}
            if art.get("ai_status") == "done" and art.get("type") == "original":
                done_articles.append(art)
            if len(done_articles) >= max_per_category:
                break

        if done_articles:
            result[category] = done_articles

    return result


# ── Public API ────────────────────────────────────────────────────────────────

async def generate_newsletter(
    redis: aioredis.Redis,
    categories: list[str],
    language: str = "English",
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    max_tokens: int = 1500,
    temperature: float = 0.4,
    lookback_seconds: int = 86400,
) -> dict:
    """
    Generate and store a newsletter. Returns {subject, html, generated_at, article_count}.
    Stores HTML in Redis under NEWSLETTER_LATEST_KEY.
    """
    articles_by_cat = await _fetch_recent_done_articles(
        redis, categories, MAX_ARTICLES_PER_CATEGORY, lookback_seconds
    )
    total = sum(len(v) for v in articles_by_cat.values())

    if total == 0:
        logger.info("[newsletter] no done articles found — skipping generation")
        return {"skipped": True, "reason": "no articles"}

    logger.info(f"[newsletter] generating from {total} articles across {len(articles_by_cat)} categories")

    newsletter = await _call_newsletter_ai(
        articles_by_cat,
        language=language,
        api_key=api_key,
        base_url=base_url,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
    )

    html = render_html(newsletter)
    now_iso = datetime.now(timezone.utc).isoformat()

    pipe = redis.pipeline()
    pipe.set(NEWSLETTER_LATEST_KEY, html, ex=NEWSLETTER_TTL_SECONDS)
    pipe.set(NEWSLETTER_LATEST_AT_KEY, now_iso, ex=NEWSLETTER_TTL_SECONDS)
    await pipe.execute()

    logger.info(f"[newsletter] stored — subject='{newsletter.get('subject', '')}'")
    return {
        "subject": newsletter.get("subject", ""),
        "html": html,
        "generated_at": now_iso,
        "article_count": total,
        "category_count": len(articles_by_cat),
    }


async def get_latest_newsletter(redis: aioredis.Redis) -> dict | None:
    """Return {html, generated_at} or None if not generated yet."""
    pipe = redis.pipeline()
    pipe.get(NEWSLETTER_LATEST_KEY)
    pipe.get(NEWSLETTER_LATEST_AT_KEY)
    html_raw, at_raw = await pipe.execute()

    if not html_raw:
        return None
    return {
        "html": html_raw.decode() if isinstance(html_raw, bytes) else html_raw,
        "generated_at": (at_raw.decode() if isinstance(at_raw, bytes) else at_raw) or "",
    }


# ── SMTP delivery ─────────────────────────────────────────────────────────────

def _send_smtp_sync(
    subject: str,
    html: str,
    smtp_cfg: dict,
) -> None:
    """
    Synchronous SMTP send — run via asyncio.to_thread().

    smtp_cfg fields:
      host, port, username, password, from_addr, to_addrs (list),
      use_tls (bool, default true), use_starttls (bool, default false)
    """
    host = smtp_cfg["host"]
    port = int(smtp_cfg.get("port", 587))
    username = smtp_cfg.get("username", "")
    password = smtp_cfg.get("password", "")
    from_addr = smtp_cfg.get("from_addr", username)
    to_addrs: list[str] = smtp_cfg.get("to_addrs", [])

    if not to_addrs:
        raise ValueError("newsletter.smtp.to_addrs is empty")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)
    msg.attach(MIMEText(html, "html", "utf-8"))

    use_tls = smtp_cfg.get("use_tls", True)
    use_starttls = smtp_cfg.get("use_starttls", False)

    context = ssl.create_default_context()

    if use_tls and not use_starttls:
        # SMTPS (port 465)
        with smtplib.SMTP_SSL(host, port, context=context) as server:
            if username:
                server.login(username, password)
            server.sendmail(from_addr, to_addrs, msg.as_string())
    else:
        # STARTTLS (port 587) or plain
        with smtplib.SMTP(host, port) as server:
            if use_starttls:
                server.starttls(context=context)
            if username:
                server.login(username, password)
            server.sendmail(from_addr, to_addrs, msg.as_string())


async def send_newsletter_smtp(
    subject: str,
    html: str,
    smtp_cfg: dict,
) -> bool:
    """
    Send newsletter HTML via SMTP. Returns True on success.

    smtp_cfg comes from settings.yaml under newsletter.smtp:
      host: "smtp.gmail.com"
      port: 587
      username: "you@gmail.com"
      password: "app-password"
      from_addr: "News Aggregator <you@gmail.com>"
      to_addrs:
        - "recipient@example.com"
      use_tls: false
      use_starttls: true
    """
    try:
        await asyncio.to_thread(_send_smtp_sync, subject, html, smtp_cfg)
        logger.info(f"[newsletter] SMTP sent to {smtp_cfg.get('to_addrs')} subject='{subject}'")
        return True
    except Exception as exc:
        logger.error(f"[newsletter] SMTP failed: {exc}")
        return False

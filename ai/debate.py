"""
Multi-Agent Debate — parallel AI perspectives on a story.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CONCEPT & ARCHITECTURE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Problem: single-AI summaries have one perspective and can miss nuance.
Solution: assign multiple AI agents to the SAME story, each with a
  distinct analytical role. Then a Synthesizer agent integrates them.

──────────────────────────────────────────────────────────────
AGENT ROLES
──────────────────────────────────────────────────────────────

  [Factual]      "What happened, exactly?"
    → Strip speculation. Only confirmed facts. No interpretation.

  [Skeptic]      "What's missing or questionable here?"
    → Challenge sources, highlight contradictions, flag gaps.

  [Impact]       "Who is affected and how?"
    → Economic, political, social consequences. Short/long term.

  [Synthesizer]  "Given all of the above, what's the real story?"
    → Receives the 3 agent outputs + original articles.
    → Produces final balanced analysis.

──────────────────────────────────────────────────────────────
ORCHESTRATION FLOW
──────────────────────────────────────────────────────────────

  story_id ──► load top articles
                     │
          ┌──────────┼──────────┐
          ▼          ▼          ▼
       Factual    Skeptic    Impact       ← parallel AI calls
          │          │          │
          └──────────┼──────────┘
                     ▼
               Synthesizer              ← serial (depends on round 1)
                     │
                     ▼
            DebateResult (Redis)
            → dispatch to hooks with ai_mode="debate"

──────────────────────────────────────────────────────────────
COST MODEL
──────────────────────────────────────────────────────────────

  Per debate: 4 AI calls × ~400 tokens each = ~1600 tokens
  At gpt-4o-mini pricing: ~$0.001 per debate
  → Only trigger for stories with article_count >= MIN_STORY_SIZE (default 3)
  → Cap debates per hour via DEBATE_RATE_LIMIT

──────────────────────────────────────────────────────────────
REDIS LAYOUT
──────────────────────────────────────────────────────────────

  news:debate:{story_id}      → Hash {factual, skeptic, impact, synthesis,
                                      generated_at, model, story_headline}
  news:debates:recent         → Sorted Set (score=ts, member=story_id)
  news:debate:queue           → Set of story_ids pending debate

──────────────────────────────────────────────────────────────
IMPLEMENTATION STATUS: STUB — not wired to scheduler yet
To activate: set debate.enabled=true in settings.yaml
             add debate_job to get_scheduler() in scheduler.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone

import redis.asyncio as aioredis

from ai.rewriter import get_openai_client
from ai.story_detector import get_story_articles
from storage.redis_keys import ARTICLE_TTL_SECONDS
from webhook.dispatcher import enqueue_dispatch

logger = logging.getLogger(__name__)

# ── Tunables ─────────────────────────────────────────────────────────────────
MIN_STORY_SIZE = 3          # min articles in story before triggering debate
DEBATE_RATE_LIMIT = 5       # max debates per scheduler run
MAX_ARTICLE_CHARS = 400     # content truncation for prompt
DEBATE_TTL = ARTICLE_TTL_SECONDS * 2

DEBATE_QUEUE_KEY = "news:debate:queue"
DEBATE_RECENT_KEY = "news:debates:recent"
DEBATE_PREFIX = "news:debate:"


# ── Agent prompts ─────────────────────────────────────────────────────────────

AGENT_PROMPTS = {
    "factual": """You are the Factual Analyst. Your only job: extract confirmed facts.
No speculation, no interpretation, no opinion. Only what is explicitly stated.
Articles about: {headline}
{articles}
Respond in 3-5 bullet points. Each bullet = one confirmed fact.""",

    "skeptic": """You are the Skeptic. Challenge the narrative.
What information is missing? What claims lack evidence? Are there contradictions between sources?
Articles about: {headline}
{articles}
Respond in 3-5 bullet points. Each = one gap, contradiction, or questionable claim.""",

    "impact": """You are the Impact Assessor. Analyze consequences.
Who is affected (people, organizations, markets, policy)? What are short-term vs long-term effects?
Articles about: {headline}
{articles}
Respond in 3-5 bullet points. Each = one impact dimension.""",

    "synthesizer": """You are the Synthesizer. Given three analytical perspectives on the same story,
produce a balanced 2-paragraph analysis that integrates all views.

Story: {headline}

FACTUAL ANALYSIS:
{factual}

SKEPTICAL ANALYSIS:
{skeptic}

IMPACT ANALYSIS:
{impact}

Write a 2-paragraph synthesis: paragraph 1 = what happened and why it matters,
paragraph 2 = what remains uncertain or contested. Be concise and balanced.""",
}


# ── AI call helpers ───────────────────────────────────────────────────────────

async def _run_agent(
    client,
    role: str,
    prompt: str,
    model: str,
    temperature: float,
) -> str:
    """Run a single agent role. Returns text output."""
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=400,
            temperature=temperature,
        )
        return resp.choices[0].message.content or ""
    except Exception as exc:
        logger.warning(f"[debate] agent={role} failed: {exc}")
        return f"[{role} agent unavailable]"


async def _build_article_context(
    redis: aioredis.Redis,
    story_id: str,
    limit: int = 5,
) -> tuple[str, str]:
    """Load articles for a story. Returns (headline, articles_text)."""
    story_hash = await redis.hgetall(f"{DEBATE_PREFIX.replace('debate:', 'story:')}{story_id}")
    headline = ""
    if story_hash:
        headline = story_hash.get(b"headline_en", b"").decode()

    article_ids = await get_story_articles(redis, story_id, limit=limit)
    if not article_ids:
        return headline, ""

    pipe = redis.pipeline()
    for aid in article_ids:
        pipe.hgetall(f"news:{aid}")
    results = await pipe.execute()

    parts = []
    for raw in results:
        if not raw:
            continue
        art = {k.decode(): v.decode() for k, v in raw.items()}
        title = art.get("title", "")
        content = (art.get("ai_summary_en") or art.get("summary", ""))[:MAX_ARTICLE_CHARS]
        source = art.get("source_name", "")
        parts.append(f"[{source}] {title}\n{content}")

    return headline, "\n\n---\n\n".join(parts)


# ── Core debate logic ─────────────────────────────────────────────────────────

def _debate_article_id(story_id: str) -> str:
    """Deterministic article ID for a debate — prevents duplicate dispatches."""
    return hashlib.sha256(f"debate:{story_id}".encode()).hexdigest()[:16]


async def _save_debate_article(
    redis: aioredis.Redis,
    story_id: str,
    headline: str,
    category: str,
    synthesis: str,
    source_article_ids: list[str],
    entities: list[str],
) -> dict:
    """
    Save the synthesis output as a publishable article (type='debate').
    Indexed in the main feed + category feed + synth feeds so it's
    visible in the dashboard and picked up by newsletter generation.
    """
    article_id = _debate_article_id(story_id)
    now_ts = datetime.now(timezone.utc).timestamp()
    now_iso = datetime.now(timezone.utc).isoformat()

    article = {
        "id": article_id,
        "type": "debate",
        "source_id": "debate",
        "source_name": "Multi-Agent Debate",
        "url": f"#debate-{story_id}",
        "title": headline,
        "summary": synthesis[:500],
        "content": synthesis,
        "lang": "en",
        "declared_lang": "en",
        "category": category,
        "published_at": now_iso,
        "fetched_at": now_iso,
        "ai_status": "done",
        "ai_summary_en": synthesis,
        "ai_summary_vi": synthesis,
        "entities": json.dumps(entities[:10]),
        "sentiment": "neutral",
        "topic_id": "",
        "ai_enrich_status": "done",
        "source_article_ids": json.dumps(source_article_ids),
        "story_id": story_id,
    }

    key = f"news:{article_id}"
    pipe = redis.pipeline()
    pipe.hset(key, mapping=article)
    pipe.expire(key, DEBATE_TTL)
    pipe.zadd("news:feed", {article_id: now_ts})
    pipe.expire("news:feed", DEBATE_TTL)
    pipe.zadd(f"news:cat:{category}", {article_id: now_ts})
    pipe.expire(f"news:cat:{category}", DEBATE_TTL)
    pipe.zadd("news:synth:feed", {article_id: now_ts})
    pipe.expire("news:synth:feed", DEBATE_TTL)
    pipe.zadd(f"news:synth:cat:{category}", {article_id: now_ts})
    pipe.expire(f"news:synth:cat:{category}", DEBATE_TTL)
    await pipe.execute()

    return article


async def run_debate(
    redis: aioredis.Redis,
    story_id: str,
    webhook_endpoints: list[dict] | None = None,
    telegram_channels: list[dict] | None = None,
    twitter_accounts: list[dict] | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str = "gpt-4o-mini",
    temperature: float = 0.5,
) -> dict | None:
    """
    Run the full 4-agent debate on a story.
    Emits synthesis as a publishable article (type='debate') dispatched to
    hooks with ai_mode='debate'.
    Returns debate result dict or None on failure.
    """
    headline, articles_text = await _build_article_context(redis, story_id)
    if not articles_text:
        logger.debug(f"[debate] story={story_id} has no articles — skipping")
        return None

    client = get_openai_client(api_key=api_key, base_url=base_url)

    # Round 1 — parallel: Factual, Skeptic, Impact
    round1_prompts = {
        role: AGENT_PROMPTS[role].format(headline=headline, articles=articles_text)
        for role in ("factual", "skeptic", "impact")
    }
    round1_results = await asyncio.gather(*[
        _run_agent(client, role, prompt, model, temperature)
        for role, prompt in round1_prompts.items()
    ])
    factual, skeptic, impact = round1_results

    # Round 2 — serial: Synthesizer (needs round 1 outputs)
    synth_prompt = AGENT_PROMPTS["synthesizer"].format(
        headline=headline,
        factual=factual,
        skeptic=skeptic,
        impact=impact,
    )
    synthesis = await _run_agent(client, "synthesizer", synth_prompt, model, temperature)

    now_iso = datetime.now(timezone.utc).isoformat()
    result = {
        "story_id": story_id,
        "story_headline": headline,
        "factual": factual,
        "skeptic": skeptic,
        "impact": impact,
        "synthesis": synthesis,
        "generated_at": now_iso,
        "model": model,
    }

    # Persist raw debate to Redis (all 4 agent outputs)
    debate_key = f"{DEBATE_PREFIX}{story_id}"
    pipe = redis.pipeline()
    pipe.hset(debate_key, mapping={
        "story_headline": headline,
        "factual": factual,
        "skeptic": skeptic,
        "impact": impact,
        "synthesis": synthesis,
        "generated_at": now_iso,
        "model": model,
    })
    pipe.expire(debate_key, DEBATE_TTL)
    pipe.zadd(DEBATE_RECENT_KEY, {story_id: datetime.now(timezone.utc).timestamp()})
    pipe.expire(DEBATE_RECENT_KEY, DEBATE_TTL)
    await pipe.execute()

    # Build publishable article from synthesis — dispatch via hooks with ai_mode="debate"
    story_meta = await redis.hgetall(f"news:story:{story_id}")
    category = "general"
    entities: list[str] = []
    source_ids: list[str] = []
    if story_meta:
        category = (story_meta.get(b"category") or b"general").decode()
        try:
            entities = json.loads((story_meta.get(b"entities") or b"[]").decode())
        except Exception:
            entities = []

    source_ids = await get_story_articles(redis, story_id, limit=10)
    debate_article = await _save_debate_article(
        redis=redis,
        story_id=story_id,
        headline=headline,
        category=category,
        synthesis=synthesis,
        source_article_ids=source_ids,
        entities=entities,
    )

    # Dispatch to hooks that opted in to debate articles
    if webhook_endpoints or telegram_channels or twitter_accounts:
        await enqueue_dispatch(
            debate_article,
            webhook_endpoints or [],
            telegram_channels=telegram_channels,
            twitter_accounts=twitter_accounts,
        )

    logger.info(
        f"[debate] completed story={story_id} headline='{headline[:60]}' "
        f"article={debate_article['id']}"
    )
    result["debate_article_id"] = debate_article["id"]
    return result


# ── Scheduler-facing job ──────────────────────────────────────────────────────

async def debate_job(
    redis: aioredis.Redis,
    webhook_endpoints: list[dict] | None = None,
    telegram_channels: list[dict] | None = None,
    twitter_accounts: list[dict] | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str = "gpt-4o-mini",
) -> int:
    """
    Pick stories ready for debate (large enough, not yet debated) and run them.
    Synthesis is saved as a publishable article (type='debate') and dispatched
    to hooks with ai_mode='debate'.
    Returns count of debates run.
    """
    from ai.story_detector import STORY_ARTICLES_PREFIX, STORIES_ACTIVE_KEY

    raw_ids = await redis.zrevrange(STORIES_ACTIVE_KEY, 0, 50)
    story_ids = [b.decode() if isinstance(b, bytes) else b for b in raw_ids]

    candidates = []
    for sid in story_ids:
        already = await redis.exists(f"{DEBATE_PREFIX}{sid}")
        if already:
            continue
        count = await redis.zcard(f"{STORY_ARTICLES_PREFIX}{sid}")
        if count >= MIN_STORY_SIZE:
            candidates.append(sid)
        if len(candidates) >= DEBATE_RATE_LIMIT:
            break

    if not candidates:
        return 0

    debated = 0
    for story_id in candidates:
        result = await run_debate(
            redis, story_id,
            webhook_endpoints=webhook_endpoints,
            telegram_channels=telegram_channels,
            twitter_accounts=twitter_accounts,
            api_key=api_key,
            base_url=base_url,
            model=model,
        )
        if result:
            debated += 1

    return debated


async def get_recent_debates(
    redis: aioredis.Redis,
    limit: int = 10,
) -> list[dict]:
    """Return recent debate results sorted by generation time DESC."""
    raw = await redis.zrevrange(DEBATE_RECENT_KEY, 0, limit - 1)
    story_ids = [b.decode() if isinstance(b, bytes) else b for b in raw]
    if not story_ids:
        return []

    pipe = redis.pipeline()
    for sid in story_ids:
        pipe.hgetall(f"{DEBATE_PREFIX}{sid}")
    results = await pipe.execute()

    debates = []
    for sid, raw_hash in zip(story_ids, results):
        if raw_hash:
            d = {k.decode(): v.decode() for k, v in raw_hash.items()}
            d["story_id"] = sid
            debates.append(d)
    return debates

"""
Social Conversation Simulator — generates realistic social media post + comment threads.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CONCEPT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Given a news article, simulate how it would spread on social media:
  1. An "author" with a distinct persona writes the main post
  2. 5-10 "netizens" with varying archetypes react in comments
  3. Some netizens reply to each other (creating sub-threads)

The goal is realism: different voices, different biases, natural flow.

──────────────────────────────────────────────────────────────
ORCHESTRATION FLOW
──────────────────────────────────────────────────────────────

  article ──► Round 1 (parallel)
                 ├── generate author persona (AI fills name/voice/background)
                 └── generate N netizen profiles (AI fills username/personality)
                     │
               Round 2 (serial)
                 └── author writes post based on their persona
                     │
               Round 3 (sequential — each comment sees prior comments)
                 └── netizen_1 comment
                 └── netizen_2 comment (can reference netizen_1)
                 └── ... (realistic conversational drift)
                     │
               Round 4 (optional — "full" depth only)
                 └── 2-3 drama threads: toxic ↔ fact_checker / smart_dissenter replies

──────────────────────────────────────────────────────────────
DEPTH MODES
──────────────────────────────────────────────────────────────

  flat   → 5-7 independent comments, no replies
  nested → 7-10 comments + 2-3 reply pairs (1 level)
  full   → 8-12 comments + full sub-threads + drama escalation

──────────────────────────────────────────────────────────────
REDIS LAYOUT
──────────────────────────────────────────────────────────────

  social:sim:{sim_id}              → Hash (article_id, author_persona_json,
                                          depth, generated_at, model, post_content,
                                          netizen_count, article_title)
  social:sim:{sim_id}:comments     → List of JSON strings (ordered, with parent_id)
  social:sims:recent               → Sorted Set (score=timestamp, member=sim_id)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import asyncio
import hashlib
import json
import logging
import random
from datetime import datetime, timezone
from typing import Literal

import redis.asyncio as aioredis

from ai.rewriter import get_openai_client, _load_ai_config
from dashboard.config_io import read_sim_personas
from storage.redis_keys import ARTICLE_TTL_SECONDS

logger = logging.getLogger(__name__)

SIM_TTL = ARTICLE_TTL_SECONDS * 3
SIM_PREFIX = "social:sim:"
SIM_RECENT_KEY = "social:sims:recent"

DepthMode = Literal["flat", "nested", "full"]

# How many netizens to generate per depth mode
DEPTH_NETIZEN_COUNT = {"flat": 5, "nested": 7, "full": 10}
# Max comments per depth mode
DEPTH_COMMENT_COUNT = {"flat": 6, "nested": 9, "full": 12}


# ── Persona loading ──────────────────────────────────────────────────────────

def _load_personas() -> dict:
    """Load personas from the active config backend (yaml or db)."""
    return read_sim_personas() or {}


def _pick_netizen_types(article_text: str, count: int) -> list[str]:
    """
    Pick a realistic mix of netizen types for the given article.
    Always includes skeptic + 1 emotional; rest randomized with weighted probability.
    """
    personas = _load_personas()
    all_types = list(personas.get("netizen_types", {}).keys())

    # Guaranteed archetypes for realism
    guaranteed = ["skeptic", "emotional"]
    pool = [t for t in all_types if t not in guaranteed]
    random.shuffle(pool)
    chosen = guaranteed + pool[: count - len(guaranteed)]
    random.shuffle(chosen)
    return chosen[:count]


# ── AI helpers ───────────────────────────────────────────────────────────────

async def _call_ai(client, prompt: str, model: str, temperature: float, max_tokens: int = 500) -> str:
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as exc:
        logger.warning(f"[social_sim] AI call failed: {exc}")
        return ""


async def _gen_author_persona(
    client, model: str, temperature: float,
    author_type: str, article_title: str, article_summary: str,
    language: str = "English",
) -> dict:
    """Generate a fully fleshed-out author persona for the given type + article topic."""
    personas = _load_personas()
    type_cfg = personas.get("author_types", {}).get(author_type, {})

    prompt = f"""You are creating a realistic social media persona for a post about this news:
Title: {article_title}
Summary: {article_summary[:300]}

Persona type: {type_cfg.get('label', author_type)}
Description: {type_cfg.get('description', '')}
Tone: {type_cfg.get('tone', '')}
Style: {type_cfg.get('style', '')}

IMPORTANT: This persona will post and interact entirely in {language}. Their name, username, and background must reflect a {language}-speaking cultural context.

Generate a JSON object for this specific persona. Be specific, not generic. Make the name and background culturally appropriate for a {language}-speaking person interested in this topic.

Return ONLY valid JSON, no explanation:
{{
  "name": "Full Name or Handle",
  "username": "@handle_lowercase",
  "background": "1-sentence specific background that explains their perspective on this topic",
  "avatar_initial": "2-letter initials",
  "type": "{author_type}",
  "type_label": "{type_cfg.get('label', author_type)}"
}}"""

    raw = await _call_ai(client, prompt, model, temperature, max_tokens=250)
    try:
        # Extract JSON even if wrapped in markdown code block
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())
    except Exception:
        return {
            "name": "Author",
            "username": "@author",
            "background": type_cfg.get("description", ""),
            "avatar_initial": "AU",
            "type": author_type,
            "type_label": type_cfg.get("label", author_type),
        }


async def _gen_netizen_profile(
    client, model: str, temperature: float,
    netizen_type: str, article_title: str, language: str = "English",
) -> dict:
    """Generate a netizen profile for one commenter archetype."""
    personas = _load_personas()
    type_cfg = personas.get("netizen_types", {}).get(netizen_type, {})

    prompt = f"""Create a social media commenter profile for a post about: "{article_title}"

Commenter archetype: {type_cfg.get('label', netizen_type)}
Description: {type_cfg.get('description', '')}
Typical phrases (adapt to {language} equivalents): {', '.join(type_cfg.get('typical_phrases', []))}

IMPORTANT: This commenter interacts entirely in {language}. Their username and display name must reflect a {language}-speaking cultural context.

Return ONLY valid JSON:
{{
  "username": "@realistic_username",
  "display_name": "Display Name",
  "avatar_initial": "2 letters",
  "type": "{netizen_type}",
  "type_label": "{type_cfg.get('label', netizen_type)}",
  "color": "{type_cfg.get('color', '#94a3b8')}",
  "badge": "{type_cfg.get('badge', '💬')}",
  "personality_note": "one sentence in {language} describing their specific angle on this topic"
}}"""

    raw = await _call_ai(client, prompt, model, temperature, max_tokens=200)
    try:
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())
    except Exception:
        return {
            "username": f"@user_{netizen_type}",
            "display_name": type_cfg.get("label", netizen_type),
            "avatar_initial": netizen_type[:2].upper(),
            "type": netizen_type,
            "type_label": type_cfg.get("label", netizen_type),
            "color": type_cfg.get("color", "#94a3b8"),
            "badge": type_cfg.get("badge", "💬"),
            "personality_note": type_cfg.get("description", ""),
        }


async def _gen_post(
    client, model: str, temperature: float,
    author_persona: dict, article_title: str, article_summary: str, author_type_cfg: dict,
    language: str = "English",
) -> str:
    """Generate the main post content from the author's perspective."""
    prompt = f"""You are {author_persona['name']} ({author_persona['username']}).
Background: {author_persona['background']}
Writing style: {author_type_cfg.get('style', '')}
Tone: {author_type_cfg.get('tone', '')}
Avoid: {author_type_cfg.get('avoid', '')}

Write a social media post about this news article:
Title: {article_title}
Summary: {article_summary[:400]}

IMPORTANT: Write the ENTIRE post in {language}. Be authentic to both the persona and the language/culture.
Length: 2-5 sentences appropriate for social media. Use language/style true to this persona.
Write the post content ONLY. No meta-commentary."""

    return await _call_ai(client, prompt, model, temperature, max_tokens=300)


async def _gen_comment(
    client, model: str, temperature: float,
    netizen: dict, post_content: str, prior_comments: list[dict],
    article_title: str, language: str = "English",
) -> str:
    """Generate one comment, aware of prior comments in the thread."""
    prior_ctx = ""
    if prior_comments:
        prior_lines = []
        for c in prior_comments[-3:]:  # only last 3 for context window efficiency
            prior_lines.append(f"  {c['display_name']}: {c['content'][:100]}")
        prior_ctx = "\n\nPrevious comments in thread:\n" + "\n".join(prior_lines)

    prompt = f"""You are {netizen['display_name']} ({netizen['username']}) commenting on a social media post.
Your archetype: {netizen['type_label']}
Your personality for this topic: {netizen.get('personality_note', '')}

The original post (about "{article_title}"):
{post_content[:300]}{prior_ctx}

IMPORTANT: Write your comment entirely in {language}. Be authentic to your archetype and the {language}-speaking social media culture.
Write your comment. 1-3 sentences max.
If there are prior comments, you may react to one of them (optional, natural only).
Write the comment ONLY. No username prefix. No quotes."""

    return await _call_ai(client, prompt, model, temperature, max_tokens=150)


async def _gen_reply(
    client, model: str, temperature: float,
    replier: dict, target_comment: dict, post_content: str,
    language: str = "English",
) -> str:
    """Generate a direct reply to a specific comment (for nested/full mode)."""
    prompt = f"""You are {replier['display_name']} ({replier['username']}).
Your archetype: {replier['type_label']}
Your personality: {replier.get('personality_note', '')}

You are replying to this comment by {target_comment['display_name']}:
"{target_comment['content']}"

Context: this is about "{post_content[:150]}"

IMPORTANT: Write the reply entirely in {language}.
Write a direct reply. 1-2 sentences. Be authentic to your archetype.
Write the reply text ONLY."""

    return await _call_ai(client, prompt, model, temperature, max_tokens=100)


# ── Simulation ID ─────────────────────────────────────────────────────────────

def _sim_id(article_id: str, author_type: str, depth: str) -> str:
    """Deterministic sim ID — same article + type + depth = same key (prevents duplicates)."""
    key = f"sim:{article_id}:{author_type}:{depth}:{datetime.now(timezone.utc).strftime('%Y%m%d%H')}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


# ── Core simulation ───────────────────────────────────────────────────────────

async def run_simulation(
    redis: aioredis.Redis,
    article_id: str,
    author_type: str = "journalist",
    depth: DepthMode = "nested",
    language: str = "English",
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    temperature: float = 0.85,
) -> dict | None:
    """
    Run a full social media conversation simulation for an article.

    Returns simulation result dict with:
      - sim_id, article_id, author_persona, post_content
      - comments list (each with: id, parent_id, persona, content, depth)
    """
    # Load article
    raw = await redis.hgetall(f"news:{article_id}")
    if not raw:
        logger.warning(f"[social_sim] article {article_id} not found")
        return None

    article = {k.decode(): v.decode() for k, v in raw.items()}
    article_title = article.get("title", "Untitled")
    article_summary = article.get("ai_summary_en") or article.get("summary", "")

    if not model:
        model = _load_ai_config().get("model", "")
    client = get_openai_client(api_key=api_key, base_url=base_url)

    personas_cfg = _load_personas()
    author_type_cfg = personas_cfg.get("author_types", {}).get(author_type, {})
    netizen_count = DEPTH_NETIZEN_COUNT.get(depth, 7)
    netizen_types = _pick_netizen_types(article_title, netizen_count)

    # ── Round 1: parallel persona generation ─────────────────────────────────
    logger.info(f"[social_sim] Round 1: generating {netizen_count + 1} personas for article={article_id}")

    author_task = _gen_author_persona(client, model, temperature, author_type, article_title, article_summary, language)
    netizen_tasks = [
        _gen_netizen_profile(client, model, temperature, ntype, article_title, language)
        for ntype in netizen_types
    ]

    results = await asyncio.gather(author_task, *netizen_tasks)
    author_persona = results[0]
    netizens = list(results[1:])

    # ── Round 2: author writes the post ──────────────────────────────────────
    logger.info(f"[social_sim] Round 2: author {author_persona.get('username')} writing post")
    post_content = await _gen_post(
        client, model, temperature,
        author_persona, article_title, article_summary, author_type_cfg, language,
    )
    if not post_content:
        logger.error(f"[social_sim] post generation failed for article={article_id}")
        return None

    # ── Round 3: sequential comments ─────────────────────────────────────────
    max_comments = DEPTH_COMMENT_COUNT.get(depth, 9)
    logger.info(f"[social_sim] Round 3: generating {max_comments} sequential comments (depth={depth})")

    comments = []
    comment_id_counter = 0

    for i, netizen in enumerate(netizens[:max_comments]):
        content = await _gen_comment(
            client, model, temperature,
            netizen, post_content, comments, article_title, language,
        )
        if content:
            comment_id_counter += 1
            comments.append({
                "id": f"c{comment_id_counter}",
                "parent_id": None,
                "persona": netizen,
                "display_name": netizen.get("display_name", ""),
                "username": netizen.get("username", ""),
                "content": content,
                "depth": 0,
            })

    # ── Round 4: replies (nested/full only) ───────────────────────────────────
    if depth in ("nested", "full") and len(comments) >= 2:
        reply_pairs = 2 if depth == "nested" else 4
        logger.info(f"[social_sim] Round 4: generating {reply_pairs} reply threads")

        # Find interesting pairs: toxic ↔ fact_checker or skeptic ↔ smart_dissenter
        target_types = {"toxic", "skeptic", "fact_checker", "smart_dissenter", "emotional"}
        candidates = [c for c in comments if c["persona"].get("type") in target_types]
        repliers = [n for n in netizens if n.get("type") in {"fact_checker", "smart_dissenter", "skeptic"}]

        pairs_done = 0
        for target_comment in candidates[:reply_pairs]:
            if pairs_done >= reply_pairs:
                break
            # Pick a replier that hasn't replied to this comment yet
            replier = next(
                (n for n in repliers if n.get("username") != target_comment["username"]),
                None,
            )
            if not replier:
                continue

            reply_content = await _gen_reply(
                client, model, temperature,
                replier, target_comment, post_content, language,
            )
            if reply_content:
                comment_id_counter += 1
                comments.append({
                    "id": f"c{comment_id_counter}",
                    "parent_id": target_comment["id"],
                    "persona": replier,
                    "display_name": replier.get("display_name", ""),
                    "username": replier.get("username", ""),
                    "content": reply_content,
                    "depth": 1,
                })
                pairs_done += 1

    # ── Persist to Redis ──────────────────────────────────────────────────────
    sim_id = _sim_id(article_id, author_type, depth)
    now_ts = datetime.now(timezone.utc).timestamp()
    now_iso = datetime.now(timezone.utc).isoformat()

    sim_meta = {
        "sim_id": sim_id,
        "article_id": article_id,
        "article_title": article_title,
        "author_type": author_type,
        "author_persona_json": json.dumps(author_persona),
        "post_content": post_content,
        "depth": depth,
        "language": language,
        "netizen_count": str(len(netizens)),
        "comment_count": str(len(comments)),
        "generated_at": now_iso,
        "model": model,
    }

    sim_key = f"{SIM_PREFIX}{sim_id}"
    comments_key = f"{SIM_PREFIX}{sim_id}:comments"

    pipe = redis.pipeline()
    pipe.hset(sim_key, mapping=sim_meta)
    pipe.expire(sim_key, SIM_TTL)
    pipe.delete(comments_key)
    for c in comments:
        pipe.rpush(comments_key, json.dumps(c))
    pipe.expire(comments_key, SIM_TTL)
    pipe.zadd(SIM_RECENT_KEY, {sim_id: now_ts})
    pipe.expire(SIM_RECENT_KEY, SIM_TTL)
    await pipe.execute()

    logger.info(
        f"[social_sim] done sim_id={sim_id} article='{article_title[:50]}' "
        f"author={author_type} depth={depth} comments={len(comments)}"
    )

    return {
        "sim_id": sim_id,
        "article_id": article_id,
        "article_title": article_title,
        "author_persona": author_persona,
        "post_content": post_content,
        "depth": depth,
        "language": language,
        "comments": comments,
        "generated_at": now_iso,
        "model": model,
    }


# ── Read helpers ─────────────────────────────────────────────────────────────

async def get_recent_simulations(redis: aioredis.Redis, limit: int = 10) -> list[dict]:
    """Return recent simulation metadata (without full comment lists)."""
    raw = await redis.zrevrange(SIM_RECENT_KEY, 0, limit - 1)
    sim_ids = [b.decode() if isinstance(b, bytes) else b for b in raw]
    if not sim_ids:
        return []

    pipe = redis.pipeline()
    for sid in sim_ids:
        pipe.hgetall(f"{SIM_PREFIX}{sid}")
    results = await pipe.execute()

    sims = []
    for sid, raw_hash in zip(sim_ids, results):
        if raw_hash:
            s = {k.decode(): v.decode() for k, v in raw_hash.items()}
            sims.append(s)
    return sims


async def get_simulation(redis: aioredis.Redis, sim_id: str) -> dict | None:
    """Return full simulation including all comments."""
    raw = await redis.hgetall(f"{SIM_PREFIX}{sim_id}")
    if not raw:
        return None

    sim = {k.decode(): v.decode() for k, v in raw.items()}

    # Parse author persona
    try:
        sim["author_persona"] = json.loads(sim.get("author_persona_json", "{}"))
    except Exception:
        sim["author_persona"] = {}

    # Load comments list
    raw_comments = await redis.lrange(f"{SIM_PREFIX}{sim_id}:comments", 0, -1)
    comments = []
    for rc in raw_comments:
        try:
            comments.append(json.loads(rc.decode() if isinstance(rc, bytes) else rc))
        except Exception:
            pass
    sim["comments"] = comments
    return sim

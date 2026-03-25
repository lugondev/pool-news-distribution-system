#!/usr/bin/env python3
"""
Create fresh synthetic article with unique content
"""

import asyncio
import json
import random
from datetime import datetime, timezone

import redis.asyncio as aioredis


async def main():
    redis = await aioredis.from_url("redis://localhost:6379/0")

    # Generate unique content
    timestamp = int(datetime.now(timezone.utc).timestamp())
    topics = [
        (
            "Tech giants announce AI safety partnership",
            "Các ông lớn công nghệ công bố hợp tác an toàn AI",
            "tech",
        ),
        (
            "Breakthrough in renewable energy storage",
            "Đột phá trong lưu trữ năng lượng tái tạo",
            "science",
        ),
        (
            "Global markets respond to central bank policy",
            "Thị trường toàn cầu phản ứng với chính sách ngân hàng trung ương",
            "finance",
        ),
        (
            "New space telescope discovers distant exoplanets",
            "Kính viễn vọng mới phát hiện hành tinh ngoài hệ mặt trời",
            "science",
        ),
        (
            "Quantum computing reaches commercial milestone",
            "Máy tính lượng tử đạt cột mốc thương mại",
            "tech",
        ),
    ]

    topic_en, topic_vi, category = random.choice(topics)

    synth_id = f"synth_{timestamp}"
    now_ts = datetime.now(timezone.utc).timestamp()

    synth_article = {
        "id": synth_id,
        "type": "synthetic",
        "category": category,
        "angle": "breaking_news",
        "title_vi": topic_vi,
        "title_en": topic_en,
        "content_vi": f"{topic_vi}. Tin tức này được tổng hợp từ nhiều nguồn đáng tin cậy vào {datetime.now().strftime('%H:%M')} ngày {datetime.now().strftime('%d/%m/%Y')}.",
        "content_en": f"{topic_en}. This news is synthesized from multiple reliable sources at {datetime.now().strftime('%H:%M')} on {datetime.now().strftime('%Y-%m-%d')}.",
        "source_article_ids": json.dumps([f"article_{i}" for i in range(3)]),
        "num_source_articles": 3,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "ai_model": "synthesis-engine",
        "ai_tokens": random.randint(300, 600),
        "ai_summary_vi": f"{topic_vi}. Chi tiết sẽ được cập nhật.",
        "ai_summary_en": f"{topic_en}. Details to be updated.",
        "ai_status": "done",
        "source": "News Synthesis",
        "source_id": "synthetic_engine",
        "url": f"https://example.com/synth/{synth_id}",
        "published_at": datetime.now(timezone.utc).isoformat(),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }

    print(f"✅ Creating: {synth_id}")
    print(f"   Category: {category}")
    print(f"   Title (EN): {topic_en}")
    print(f"   Title (VI): {topic_vi}")

    # Save to Redis
    key = f"news:{synth_id}"
    ttl = 3600

    pipe = redis.pipeline()
    pipe.hset(key, mapping=synth_article)
    pipe.expire(key, ttl)
    pipe.zadd("news:feed", {synth_id: now_ts})
    pipe.zadd(f"news:cat:{category}", {synth_id: now_ts})
    await pipe.execute()

    print(f"✅ Saved to Redis: {key}")

    # Auto-dispatch
    from webhook.dispatcher import dispatch_article
    import yaml

    with open("config/settings.yaml") as f:
        config = yaml.safe_load(f)

    endpoints = config.get("webhook", {}).get("endpoints", [])
    target = [ep for ep in endpoints if ep.get("id") == "tools-test-polistic"]

    if target:
        print(f"\n🚀 Dispatching to webhook...")
        await dispatch_article(synth_article, target, [])
        print(f"✅ Dispatched!")

    await redis.aclose()

    print(f"\n📊 Check logs:")
    print(
        f"   sqlite3 data/stats.db \"SELECT success, status_code, sent_at FROM webhook_logs WHERE article_id = '{synth_id}' ORDER BY sent_at DESC LIMIT 1;\""
    )


if __name__ == "__main__":
    asyncio.run(main())

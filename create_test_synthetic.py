#!/usr/bin/env python3
"""
Quick test: Create a synthetic article manually and verify webhook dispatch
"""

import asyncio
import json
from datetime import datetime, timezone

import redis.asyncio as aioredis


async def main():
    print("=" * 80)
    print("🧪 MANUAL SYNTHETIC ARTICLE CREATION")
    print("=" * 80)

    redis = await aioredis.from_url("redis://localhost:6379/0")

    # Create test synthetic article
    synth_id = f"test_synth_{int(datetime.now(timezone.utc).timestamp())}"
    now_ts = datetime.now(timezone.utc).timestamp()

    synth_article = {
        "id": synth_id,
        "type": "synthetic",
        "category": "tech",
        "angle": "AI developments overview",
        "title_vi": "Tổng quan các phát triển AI trong tuần",
        "title_en": "Weekly AI Development Overview",
        "content_vi": "Tuần này chứng kiến nhiều phát triển quan trọng trong lĩnh vực AI. OpenAI ra mắt GPT-5 với khả năng suy luận nâng cao. Google công bố đột phá về máy tính lượng tử. Apple Vision Pro bán chạy hơn dự kiến.",
        "content_en": "This week witnessed significant AI developments. OpenAI launched GPT-5 with enhanced reasoning. Google announced quantum computing breakthrough. Apple Vision Pro sales exceeded expectations.",
        "source_article_ids": json.dumps(
            ["10164479f76be73e", "786470c5695d74c1", "ff30ce6905b682a3"]
        ),
        "num_source_articles": 3,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "ai_model": "test-model",
        "ai_tokens": 500,
        "ai_summary_vi": "Tuần này chứng kiến nhiều phát triển quan trọng trong lĩnh vực AI với sự ra mắt GPT-5, đột phá máy tính lượng tử và thành công của Vision Pro.",
        "ai_summary_en": "This week witnessed significant AI developments with GPT-5 launch, quantum computing breakthrough, and Vision Pro success.",
        "ai_status": "done",
        "source": "AI Synthesis Bot",
        "source_id": "synthetic_bot",
    }

    print(f"\n📝 Creating synthetic article: {synth_id}")
    print(f"   Category: {synth_article['category']}")
    print(f"   Type: {synth_article['type']}")
    print(f"   Title (EN): {synth_article['title_en']}")
    print(f"   Summary (EN): {synth_article['ai_summary_en']}")

    # Save to Redis
    key = f"news:{synth_id}"
    ttl = 3600  # 1 hour

    pipe = redis.pipeline()

    # Save article hash
    pipe.hset(key, mapping=synth_article)
    pipe.expire(key, ttl)

    # Add to main feed
    pipe.zadd("news:feed", {synth_id: now_ts})
    pipe.expire("news:feed", ttl)

    # Add to category index
    pipe.zadd(f"news:cat:{synth_article['category']}", {synth_id: now_ts})
    pipe.expire(f"news:cat:{synth_article['category']}", ttl)

    # Add to synthetic index
    pipe.zadd(f"news:synth:cat:{synth_article['category']}", {synth_id: now_ts})
    pipe.expire(f"news:synth:cat:{synth_article['category']}", ttl)

    await pipe.execute()

    print(f"\n✅ Synthetic article created!")
    print(f"   Redis key: {key}")
    print(f"   TTL: {ttl}s ({ttl // 60} minutes)")

    # Verify
    article_type = await redis.hget(key, "type")
    article_type = (
        article_type.decode() if isinstance(article_type, bytes) else article_type
    )

    print(f"\n🔍 Verification:")
    print(f"   Article exists: {await redis.exists(key) == 1}")
    print(f"   Article type: {article_type}")
    print(f"   In feed: {await redis.zscore('news:feed', synth_id) is not None}")

    # Check if would pass webhook filter
    print(f"\n🎯 Webhook Filter Check:")
    print(f"   tools-test-polistic filter: include [synthetic]")
    print(f"   Article type: {article_type}")
    print(f"   Would pass: {'✅ YES' if article_type == 'synthetic' else '❌ NO'}")

    # Instructions
    print(f"\n" + "=" * 80)
    print("📋 NEXT STEPS")
    print("=" * 80)
    print(
        """
1. Start the app (if not running):
   python main.py

2. The synthetic article will be automatically dispatched to webhook:
   - Webhook: tools-test-polistic
   - Filter: include [synthetic]
   - URL: https://6f91-171-239-138-49.ngrok-free.app/api/tweet

3. Check webhook logs after 1-2 minutes:
   sqlite3 data/stats.db "
     SELECT article_id, success, sent_at 
     FROM webhook_logs 
     WHERE article_id = '{synth_id}';
   "

4. Or check all recent dispatches:
   sqlite3 data/stats.db "
     SELECT article_id, success, sent_at 
     FROM webhook_logs 
     WHERE webhook_id = 'tools-test-polistic'
     ORDER BY sent_at DESC 
     LIMIT 5;
   "

5. Monitor real-time:
   tail -f /tmp/news-aggregator.log | grep -E "Webhook.*OK|Rate limit"
    """.format(synth_id=synth_id)
    )

    print("=" * 80)
    print("✅ Done! Synthetic article ready for dispatch.")
    print("=" * 80)

    await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())

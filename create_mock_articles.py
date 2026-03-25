#!/usr/bin/env python3
"""Create mock articles in Redis for testing topic synthesis."""

import asyncio
import hashlib
from datetime import datetime, timezone

import redis.asyncio as aioredis


async def create_mock_articles():
    """Create 5 mock politics articles for testing."""
    redis = await aioredis.from_url("redis://localhost:6379/0", decode_responses=False)

    mock_articles = [
        {
            "title": "US Senate Passes Major Infrastructure Bill",
            "category": "politics",
            "source": "Reuters",
        },
        {
            "title": "European Parliament Debates Climate Policy",
            "category": "politics",
            "source": "BBC",
        },
        {
            "title": "Trade Agreement Signed Between Asia-Pacific Nations",
            "category": "politics",
            "source": "AP News",
        },
        {
            "title": "New Coalition Government Formed in Germany",
            "category": "politics",
            "source": "DW",
        },
        {
            "title": "UN Security Council Meeting on Global Security",
            "category": "politics",
            "source": "Al Jazeera",
        },
        {
            "title": "OpenAI Releases New GPT-5 Model with Enhanced Reasoning",
            "category": "tech",
            "source": "TechCrunch",
        },
        {
            "title": "Google Announces Quantum Computing Breakthrough",
            "category": "tech",
            "source": "The Verge",
        },
        {
            "title": "Apple Vision Pro Sales Exceed Expectations",
            "category": "tech",
            "source": "Bloomberg",
        },
        {
            "title": "Tesla Unveils New Affordable EV Model",
            "category": "tech",
            "source": "Electrek",
        },
        {
            "title": "Microsoft Azure Expands AI Services Portfolio",
            "category": "tech",
            "source": "ZDNet",
        },
    ]

    now = datetime.now(timezone.utc)
    timestamp = int(now.timestamp())

    for idx, article in enumerate(mock_articles):
        # Generate article ID
        raw = f"mock-{idx}:{article['title']}"
        article_id = hashlib.sha256(raw.encode()).hexdigest()[:16]

        # Create article data
        data = {
            "id": article_id,
            "type": "original",
            "title": article["title"],
            "url": f"https://example.com/article-{idx}",
            "source": article["source"],
            "source_id": f"mock_{article['source'].lower().replace(' ', '_')}",
            "category": article["category"],
            "published_at": now.isoformat(),
            "crawled_at": now.isoformat(),
            "lang": "en",
            "ai_status": "pending",
            "content": f"This is mock content for article about {article['title'].lower()}. "
            * 10,
        }

        # Save to Redis
        key = f"news:{article_id}"
        await redis.hset(key, mapping=data)
        await redis.expire(key, 43200)  # 12 hours

        # Add to indices
        await redis.zadd("news:feed", {article_id: timestamp - idx})
        await redis.zadd(
            f"news:cat:{article['category']}", {article_id: timestamp - idx}
        )
        await redis.zadd(
            f"news:source:{data['source_id']}", {article_id: timestamp - idx}
        )

        print(f"✅ Created: {article['title'][:50]}... ({article['category']})")

    await redis.close()

    print(f"\n✅ Created {len(mock_articles)} mock articles")
    print("   5 politics articles")
    print("   5 tech articles")
    print("\nYou can now:")
    print("   1. Run: python test_topic_synthesis.py")
    print("   2. Or wait 2 minutes for synthesis job to run")
    print("   3. Check dashboard: http://localhost:8000")


if __name__ == "__main__":
    asyncio.run(create_mock_articles())

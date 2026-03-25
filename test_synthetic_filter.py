#!/usr/bin/env python3
"""
Test script to verify article type filtering with synthetic articles.
This script:
1. Checks current Redis state
2. Creates a test synthetic article
3. Tests the filter logic
4. Cleans up
"""

import asyncio
import json
from datetime import datetime, timezone

import redis.asyncio as aioredis

from webhook.filters import passes_filter


async def main():
    print("=" * 60)
    print("Article Type Filter - Synthetic Article Test")
    print("=" * 60)

    redis = await aioredis.from_url("redis://localhost:6379/0")

    # Step 1: Check current articles
    print("\n=== Step 1: Current Redis State ===")
    feed = await redis.zrevrange("news:feed", 0, 9, withscores=False)
    print(f"Found {len(feed)} articles in feed")

    type_counts = {"original": 0, "synthetic": 0, "unknown": 0}
    for article_id in feed:
        if isinstance(article_id, bytes):
            article_id = article_id.decode()
        article_type = await redis.hget(f"news:{article_id}", "type")
        if article_type:
            article_type = (
                article_type.decode()
                if isinstance(article_type, bytes)
                else article_type
            )
            type_counts[article_type] = type_counts.get(article_type, 0) + 1
        else:
            type_counts["unknown"] += 1

    print(f"Article types: {type_counts}")

    # Step 2: Create test synthetic article
    print("\n=== Step 2: Creating Test Synthetic Article ===")
    test_synth_id = "test_synthetic_001"
    test_synth = {
        "id": test_synth_id,
        "type": "synthetic",
        "category": "tech",
        "angle": "test",
        "title_vi": "Bài tổng hợp thử nghiệm",
        "title_en": "Test Synthetic Article",
        "content_vi": "Nội dung tổng hợp thử nghiệm",
        "content_en": "Test synthetic content",
        "source_article_ids": json.dumps(["test_001", "test_002"]),
        "num_source_articles": 2,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "ai_model": "test-model",
        "ai_tokens": 100,
    }

    # Save to Redis
    key = f"news:{test_synth_id}"
    await redis.hset(key, mapping=test_synth)
    await redis.expire(key, 3600)  # 1 hour

    # Add to feed
    now_ts = datetime.now(timezone.utc).timestamp()
    await redis.zadd("news:feed", {test_synth_id: now_ts})

    print(f"✅ Created synthetic article: {test_synth_id}")

    # Step 3: Test filters
    print("\n=== Step 3: Testing Filter Logic ===")

    # Fetch the article back
    article_data = await redis.hgetall(key)
    article = {
        k.decode() if isinstance(k, bytes) else k: v.decode()
        if isinstance(v, bytes)
        else v
        for k, v in article_data.items()
    }

    test_configs = [
        {
            "name": "Mode: ALL (should pass)",
            "config": {"filter_article_types_mode": "all", "filter_article_types": []},
            "expected": True,
        },
        {
            "name": "Mode: INCLUDE synthetic (should pass)",
            "config": {
                "filter_article_types_mode": "include",
                "filter_article_types": ["synthetic"],
            },
            "expected": True,
        },
        {
            "name": "Mode: INCLUDE original (should FAIL)",
            "config": {
                "filter_article_types_mode": "include",
                "filter_article_types": ["original"],
            },
            "expected": False,
        },
        {
            "name": "Mode: EXCLUDE synthetic (should FAIL)",
            "config": {
                "filter_article_types_mode": "exclude",
                "filter_article_types": ["synthetic"],
            },
            "expected": False,
        },
        {
            "name": "Mode: EXCLUDE original (should pass)",
            "config": {
                "filter_article_types_mode": "exclude",
                "filter_article_types": ["original"],
            },
            "expected": True,
        },
    ]

    all_passed = True
    for test in test_configs:
        result = passes_filter(article, test["config"])
        status = "✅ PASS" if result == test["expected"] else "❌ FAIL"
        if result != test["expected"]:
            all_passed = False
        print(f"{status}: {test['name']} → {result}")

    # Step 4: Test with original article
    print("\n=== Step 4: Testing with Original Article ===")

    # Get first original article
    original_id = None
    for article_id in feed:
        if isinstance(article_id, bytes):
            article_id = article_id.decode()
        article_type = await redis.hget(f"news:{article_id}", "type")
        if article_type:
            article_type = (
                article_type.decode()
                if isinstance(article_type, bytes)
                else article_type
            )
            if article_type == "original":
                original_id = article_id
                break

    if original_id:
        article_data = await redis.hgetall(f"news:{original_id}")
        article = {
            k.decode() if isinstance(k, bytes) else k: v.decode()
            if isinstance(v, bytes)
            else v
            for k, v in article_data.items()
        }

        test_configs = [
            {
                "name": "Original - Mode: INCLUDE synthetic (should FAIL)",
                "config": {
                    "filter_article_types_mode": "include",
                    "filter_article_types": ["synthetic"],
                },
                "expected": False,
            },
            {
                "name": "Original - Mode: INCLUDE original (should pass)",
                "config": {
                    "filter_article_types_mode": "include",
                    "filter_article_types": ["original"],
                },
                "expected": True,
            },
        ]

        for test in test_configs:
            result = passes_filter(article, test["config"])
            status = "✅ PASS" if result == test["expected"] else "❌ FAIL"
            if result != test["expected"]:
                all_passed = False
            print(f"{status}: {test['name']} → {result}")
    else:
        print("⚠️  No original articles found for testing")

    # Step 5: Cleanup
    print("\n=== Step 5: Cleanup ===")
    await redis.delete(key)
    await redis.zrem("news:feed", test_synth_id)
    print(f"✅ Removed test synthetic article")

    # Summary
    print("\n" + "=" * 60)
    if all_passed:
        print("🎉 ALL TESTS PASSED")
    else:
        print("❌ SOME TESTS FAILED")
    print("=" * 60)

    # Step 6: Diagnosis
    print("\n=== Diagnosis: Why No Synthetic Articles? ===")

    # Check if topic synthesis is enabled
    import yaml

    with open("config/settings.yaml") as f:
        config = yaml.safe_load(f)

    synthesis_cfg = config.get("ai", {}).get("topic_synthesis", {})
    print(f"Topic synthesis enabled: {synthesis_cfg.get('enabled', False)}")
    print(f"Min articles required: {synthesis_cfg.get('min_articles', 5)}")
    print(f"Interval: {synthesis_cfg.get('interval_minutes', 5)} minutes")

    # Check article count per category
    print("\n=== Articles by Category ===")
    categories = config.get("categories", [])
    for cat in categories:
        if not cat.get("enabled", True):
            continue
        cat_id = cat["id"]
        count = await redis.zcount(f"news:category:{cat_id}", "-inf", "+inf")
        print(f"{cat_id}: {count} articles")

    print("\n💡 Possible reasons for no synthetic articles:")
    print("   1. App not running → No topic synthesis job executing")
    print("   2. Not enough articles per category (need ≥5)")
    print("   3. Job hasn't run yet (runs every 2 minutes)")
    print("   4. AI API error (check logs)")

    await redis.close()


if __name__ == "__main__":
    asyncio.run(main())

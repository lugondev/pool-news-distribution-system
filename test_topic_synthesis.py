#!/usr/bin/env python3
"""
Test script for topic synthesis feature.
Validates that AI can autonomously decide output count.
"""

import asyncio
import json
import sys
from datetime import datetime, timezone, timedelta

import redis.asyncio as aioredis
import yaml


async def test_synthesis():
    """Test topic synthesis with mock articles."""
    print("=== Topic Synthesis Test ===\n")

    # Load config
    with open("config/settings.yaml") as f:
        cfg = yaml.safe_load(f)

    synth_cfg = cfg.get("ai", {}).get("topic_synthesis", {})
    if not synth_cfg.get("enabled"):
        print("❌ Topic synthesis is DISABLED in config/settings.yaml")
        print("   Set ai.topic_synthesis.enabled = true to test\n")
        return False

    print("✅ Topic synthesis is ENABLED")
    print(f"   Interval: {synth_cfg.get('interval_minutes', 5)} minutes")
    print(f"   Min articles: {synth_cfg.get('min_articles', 5)}")
    print(f"   Max articles: {synth_cfg.get('max_articles', 15)}\n")

    # Connect to Redis
    redis_url = cfg.get("storage", {}).get("redis_url", "redis://localhost:6379/0")
    redis = await aioredis.from_url(redis_url, decode_responses=False)

    try:
        await redis.ping()
        print("✅ Connected to Redis\n")
    except Exception as e:
        print(f"❌ Redis connection failed: {e}\n")
        return False

    # Check for existing articles
    print("Checking for articles in Redis...")
    categories = [c["id"] for c in cfg.get("categories", []) if c.get("enabled")]

    article_counts = {}
    for cat in categories:
        count = await redis.zcard(f"news:cat:{cat}")
        if count > 0:
            article_counts[cat] = count

    if not article_counts:
        print("⚠️  No articles found in Redis. Run crawler first:\n")
        print("   python main.py")
        print("   # Wait a few minutes for crawl job to populate articles\n")
        return False

    print(f"✅ Found articles in {len(article_counts)} categories:\n")
    for cat, count in sorted(article_counts.items(), key=lambda x: x[1], reverse=True):
        print(f"   {cat:15} {count:3} articles")

    # Check for synthetic articles
    print("\nChecking for synthetic articles...")
    synth_count = await redis.zcard("news:synth:feed")

    if synth_count == 0:
        print("⚠️  No synthetic articles yet. This is expected if job hasn't run.")
        print(
            f"   Topic synthesis job runs every {synth_cfg.get('interval_minutes', 5)} minutes"
        )
        print("   Wait for the next job run, or manually trigger:\n")
        print("   # In Python console:")
        print("   from ai.topic_synthesis import process_category_synthesis")
        print("   await process_category_synthesis(redis, 'politics')\n")
    else:
        print(f"✅ Found {synth_count} synthetic articles\n")

        # Show sample synthetic articles
        synth_ids = await redis.zrevrange("news:synth:feed", 0, 4)
        print("Sample synthetic articles:\n")

        for synth_id in synth_ids:
            synth_id_str = (
                synth_id.decode() if isinstance(synth_id, bytes) else synth_id
            )
            data = await redis.hgetall(f"news:{synth_id_str}")
            if not data:
                continue

            article = {k.decode(): v.decode() for k, v in data.items()}

            print(f"  ID: {article.get('id', 'N/A')}")
            print(f"  Category: {article.get('category', 'N/A')}")
            print(f"  Angle: {article.get('angle', 'N/A')}")
            print(f"  Title (VI): {article.get('title_vi', 'N/A')[:70]}...")
            print(f"  Title (EN): {article.get('title_en', 'N/A')[:70]}...")
            print(f"  Source articles: {article.get('num_source_articles', 'N/A')}")

            analysis = article.get("ai_analysis", "")
            if analysis:
                print(f"  AI reasoning: {analysis[:100]}...")
            print()

    # Check system logs
    print("Checking scheduler logs...")

    try:
        import sqlite3

        conn = sqlite3.connect("data/stats.db")
        cursor = conn.cursor()

        cursor.execute("""
            SELECT started_at, status, metadata
            FROM system_logs
            WHERE job_name = 'topic_synthesis_job'
            ORDER BY started_at DESC
            LIMIT 5
        """)

        logs = cursor.fetchall()
        conn.close()

        if not logs:
            print("⚠️  No topic_synthesis_job logs found")
            print("   Job may not have run yet\n")
        else:
            print(f"✅ Found {len(logs)} recent job runs:\n")

            for started_at, status, metadata_json in logs:
                meta = json.loads(metadata_json) if metadata_json else {}
                print(f"  {started_at}: {status}")
                if meta:
                    print(f"    Generated: {meta.get('total_generated', 0)} articles")
                    results = meta.get("results", {})
                    if results:
                        print(
                            f"    Categories: {', '.join(f'{k}:{v}' for k, v in results.items())}"
                        )
                print()

    except Exception as e:
        print(f"⚠️  Could not read system logs: {e}\n")

    await redis.close()

    print("\n=== Test Summary ===")
    print(f"✅ Configuration valid")
    print(f"✅ Redis accessible")
    print(f"✅ {len(article_counts)} categories with articles")

    if synth_count > 0:
        print(f"✅ {synth_count} synthetic articles generated")
        print("\n🎉 Topic synthesis is working!")
    else:
        print(f"⚠️  No synthetic articles yet (job may not have run)")
        print("\n💡 Next steps:")
        print("   1. Wait for scheduler job (every 5 min)")
        print("   2. Or test manually in Python console")

    print()
    return True


async def manual_test(category: str = "politics"):
    """Manually trigger synthesis for one category."""
    print(f"\n=== Manual Synthesis Test: {category} ===\n")

    with open("config/settings.yaml") as f:
        cfg = yaml.safe_load(f)

    redis_url = cfg.get("storage", {}).get("redis_url", "redis://localhost:6379/0")
    redis = await aioredis.from_url(redis_url, decode_responses=False)

    try:
        from ai.topic_synthesis import process_category_synthesis

        count = await process_category_synthesis(
            redis=redis,
            category=category,
            min_articles=3,  # Lower threshold for testing
        )

        print(f"\n✅ Generated {count} synthetic articles for {category}")

        if count > 0:
            # Show results
            synth_ids = await redis.zrevrange(
                f"news:synth:cat:{category}", 0, count - 1
            )
            print(f"\nResults:\n")

            for synth_id in synth_ids:
                synth_id_str = (
                    synth_id.decode() if isinstance(synth_id, bytes) else synth_id
                )
                data = await redis.hgetall(f"news:{synth_id_str}")
                if not data:
                    continue

                article = {k.decode(): v.decode() for k, v in data.items()}
                print(
                    f"  {article.get('angle', 'summary'):12} | {article.get('title_en', 'N/A')[:70]}"
                )

            print(
                "\n🎉 Success! Check http://localhost:8000/api/news?article_type=synthetic"
            )
        else:
            print(f"\n⚠️  No outputs generated. Possible reasons:")
            print(f"   - Not enough articles (need >= 3)")
            print(f"   - AI decided content is too similar")
            print(f"   - AI API error (check logs)")

    except ImportError:
        print("❌ Could not import topic_synthesis module")
        print("   Make sure ai/topic_synthesis.py exists")
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback

        traceback.print_exc()
    finally:
        await redis.close()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "manual":
        category = sys.argv[2] if len(sys.argv) > 2 else "politics"
        asyncio.run(manual_test(category))
    else:
        asyncio.run(test_synthesis())

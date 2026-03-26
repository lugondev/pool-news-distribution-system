#!/usr/bin/env python3
"""
Test script to verify age skip logic and category volume thresholds.
Run this to see how your current article distribution maps to skip thresholds.
"""

import asyncio
import sys
from datetime import datetime, timedelta, timezone

import redis.asyncio as aioredis
import yaml


async def analyze_age_skip_thresholds():
    """Analyze current category volumes and show age thresholds."""

    # Load config
    with open("config/settings.yaml") as f:
        cfg = yaml.safe_load(f)

    ai_cfg = cfg.get("ai", {})
    busy_mins = ai_cfg.get("age_threshold_busy_minutes", 15)
    moderate_mins = ai_cfg.get("age_threshold_moderate_minutes", 20)
    quiet_mins = ai_cfg.get("age_threshold_quiet_minutes", 30)

    redis_url = cfg.get("redis", {}).get("url", "redis://localhost:6379/0")
    redis = await aioredis.from_url(redis_url)

    try:
        # Sample last 2 hours of articles
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=2)).timestamp()
        article_ids = await redis.zrangebyscore(
            "news:feed", cutoff, "+inf", start=0, num=500
        )

        if not article_ids:
            print("No articles found in last 2 hours.")
            return

        # Count per category
        counts = {}
        for aid in article_ids:
            aid_str = aid.decode() if isinstance(aid, bytes) else aid
            category = await redis.hget(f"news:{aid_str}", "category")
            if category:
                cat = category.decode() if isinstance(category, bytes) else category
                counts[cat] = counts.get(cat, 0) + 1

        if not counts:
            print("No category data found.")
            return

        # Calculate thresholds
        sorted_vals = sorted(counts.values())
        n = len(sorted_vals)
        low_thresh = sorted_vals[n // 3] if n > 0 else 0
        high_thresh = sorted_vals[(n * 2) // 3] if n > 0 else 0

        # Classify categories
        busy = []
        moderate = []
        quiet = []

        for cat, count in sorted(counts.items(), key=lambda x: x[1], reverse=True):
            if count >= high_thresh:
                busy.append((cat, count))
            elif count >= low_thresh:
                moderate.append((cat, count))
            else:
                quiet.append((cat, count))

        # Display results
        print("=" * 70)
        print("AGE SKIP THRESHOLD ANALYSIS")
        print("=" * 70)
        print(f"\nConfig thresholds:")
        print(f"  Busy:     {busy_mins} minutes")
        print(f"  Moderate: {moderate_mins} minutes")
        print(f"  Quiet:    {quiet_mins} minutes")
        print(f"\nSampled {len(article_ids)} articles from last 2 hours")
        print(f"Found {len(counts)} unique categories")
        print(f"\nVolume thresholds:")
        print(f"  High (busy):     count >= {high_thresh}")
        print(f"  Low (moderate):  count >= {low_thresh}")
        print(f"  Quiet:           count < {low_thresh}")

        print(f"\n{'BUSY CATEGORIES':<30} {'Count':<10} {'Max Age':<15}")
        print("-" * 70)
        for cat, count in busy:
            print(f"{cat:<30} {count:<10} {busy_mins} min")

        print(f"\n{'MODERATE CATEGORIES':<30} {'Count':<10} {'Max Age':<15}")
        print("-" * 70)
        for cat, count in moderate:
            print(f"{cat:<30} {count:<10} {moderate_mins} min")

        print(f"\n{'QUIET CATEGORIES':<30} {'Count':<10} {'Max Age':<15}")
        print("-" * 70)
        for cat, count in quiet:
            print(f"{cat:<30} {count:<10} {quiet_mins} min")

        # Check for age-skipped articles
        print("\n" + "=" * 70)
        print("CHECKING FOR AGE-SKIPPED ARTICLES (last 2 hours)")
        print("=" * 70)

        skipped = []
        for aid in article_ids:
            aid_str = aid.decode() if isinstance(aid, bytes) else aid
            status = await redis.hget(f"news:{aid_str}", "ai_status")
            if status and status.decode() == "age_skipped":
                title = await redis.hget(f"news:{aid_str}", "title")
                category = await redis.hget(f"news:{aid_str}", "category")
                fetched_at = await redis.hget(f"news:{aid_str}", "fetched_at")
                skipped.append(
                    {
                        "id": aid_str,
                        "title": title.decode() if title else "?",
                        "category": category.decode() if category else "?",
                        "fetched_at": fetched_at.decode() if fetched_at else "?",
                    }
                )

        if skipped:
            print(f"\nFound {len(skipped)} age-skipped articles:")
            for s in skipped[:10]:  # Show first 10
                print(f"  [{s['category']}] {s['title'][:60]}")
                try:
                    fetched = datetime.fromisoformat(s["fetched_at"])
                    if fetched.tzinfo is None:
                        fetched = fetched.replace(tzinfo=timezone.utc)
                    age_mins = (
                        datetime.now(timezone.utc) - fetched
                    ).total_seconds() / 60
                    print(f"    Age: {age_mins:.1f} minutes")
                except Exception:
                    pass
            if len(skipped) > 10:
                print(f"  ... and {len(skipped) - 10} more")
        else:
            print("\nNo age-skipped articles found. ✓")

        print("\n" + "=" * 70)
        print("RECOMMENDATIONS")
        print("=" * 70)

        if skipped:
            print("\n⚠️  Age skips detected!")
            print("\nTo reduce skips for 'all categories' channels, consider:")
            print("1. Increase age thresholds in config/settings.yaml:")
            print("   ai:")
            print(
                f"     age_threshold_busy_minutes: {busy_mins * 2}     # Double current"
            )
            print(f"     age_threshold_moderate_minutes: {moderate_mins * 2}")
            print(f"     age_threshold_quiet_minutes: {quiet_mins * 2}")
            print("\n2. Increase AI batch size to process more articles per job:")
            print("   ai:")
            print("     batch_size: 20  # Up from 10")
            print("\n3. Increase AI job frequency:")
            print("   ai:")
            print("     interval_minutes: 1  # Down from 2")
        else:
            print("\n✓ No age skips detected — current thresholds are working well!")

        print("\nFor more details, see: docs/AGE_SKIP_EXPLAINED.md")
        print("=" * 70)

    finally:
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(analyze_age_skip_thresholds())

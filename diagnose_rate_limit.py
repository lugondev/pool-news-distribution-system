#!/usr/bin/env python3
"""
Comprehensive rate limit diagnosis tool
Kiểm tra toàn diện rate limit và dispatch behavior
"""

import asyncio
import json
import yaml
from datetime import datetime, timezone

import redis.asyncio as aioredis
import sqlite3


async def main():
    print("=" * 80)
    print("🔍 RATE LIMIT DIAGNOSIS TOOL")
    print("=" * 80)

    # Load config
    with open("config/settings.yaml") as f:
        config = yaml.safe_load(f)

    webhook = config.get("webhook", {}).get("endpoints", [])[0]

    print("\n" + "=" * 80)
    print("📋 CURRENT CONFIGURATION")
    print("=" * 80)
    print(f"Webhook ID: {webhook.get('id')}")
    print(f"Enabled: {webhook.get('enabled')}")
    print(f"URL: {webhook.get('url')}")
    print(f"\n🚦 Rate Limit Settings:")
    print(f"  Max messages: {webhook.get('rate_limit_max')}")
    print(f"  Window: {webhook.get('rate_limit_window_minutes')} minute(s)")
    print(f"\n🔍 Article Type Filter:")
    print(f"  Mode: {webhook.get('filter_article_types_mode')}")
    print(f"  Types: {webhook.get('filter_article_types')}")
    print(f"\n⚙️  Other Settings:")
    print(f"  Retry attempts: {webhook.get('retry_attempts')}")
    print(f"  Retry delay: {webhook.get('retry_delay_seconds')}s")
    print(f"  Timeout: {webhook.get('timeout_seconds')}s")

    # Check article type filter issue
    filter_mode = webhook.get("filter_article_types_mode")
    filter_types = webhook.get("filter_article_types", [])
    if filter_mode == "all" and filter_types:
        print(
            f"\n⚠️  WARNING: filter_article_types_mode = 'all' → filter_article_types bị BỎ QUA!"
        )
        print(f"    Hiện tại webhook nhận TẤT CẢ article types (original + synthetic)")
        print(f"    Nếu muốn filter, đổi mode thành 'include' hoặc 'exclude'")

    # Check webhook logs from SQLite
    print("\n" + "=" * 80)
    print("📊 WEBHOOK DISPATCH HISTORY (Last 30 dispatches)")
    print("=" * 80)

    db = sqlite3.connect("data/stats.db")
    cursor = db.cursor()

    cursor.execute(
        """
        SELECT article_id, success, sent_at, error_msg
        FROM webhook_logs
        WHERE webhook_id = ?
        ORDER BY sent_at DESC
        LIMIT 30
    """,
        (webhook.get("id"),),
    )

    logs = cursor.fetchall()

    if not logs:
        print("❌ No webhook logs found")
    else:
        # Group by minute
        by_minute = {}
        for article_id, success, sent_at, error_msg in logs:
            dt = datetime.fromisoformat(sent_at)
            minute_key = dt.strftime("%H:%M")
            if minute_key not in by_minute:
                by_minute[minute_key] = {"success": 0, "failed": 0, "articles": []}
            if success:
                by_minute[minute_key]["success"] += 1
            else:
                by_minute[minute_key]["failed"] += 1
            by_minute[minute_key]["articles"].append(article_id[:12])

        print(f"\nTotal dispatches: {len(logs)}")
        print(f"Success: {sum(1 for _, s, _, _ in logs if s)}")
        print(f"Failed: {sum(1 for _, s, _, _ in logs if not s)}")

        print(f"\n📈 Dispatches per minute (last 10 minutes):")
        for minute in sorted(by_minute.keys(), reverse=True)[:10]:
            data = by_minute[minute]
            total = data["success"] + data["failed"]
            print(
                f"  {minute}: {data['success']} success, {data['failed']} failed (total: {total})"
            )

            # Check if rate limit working
            rate_limit = webhook.get("rate_limit_max", 0)
            if rate_limit > 0 and total > rate_limit:
                print(f"    ⚠️  WARNING: {total} > rate_limit ({rate_limit})")
                print(f"    → Rate limit might not be working!")

        # Check latest failed dispatches
        failed = [(aid, em) for aid, s, _, em in logs if not s][:5]
        if failed:
            print(f"\n❌ Recent failures:")
            for aid, em in failed:
                print(f"  {aid[:12]}: {em or '(no error message)'}")

    # Check Redis articles
    print("\n" + "=" * 80)
    print("📦 REDIS ARTICLES")
    print("=" * 80)

    redis = await aioredis.from_url("redis://localhost:6379/0")

    feed = await redis.zrevrange("news:feed", 0, 19, withscores=False)
    print(f"Total articles in feed: {await redis.zcard('news:feed')}")
    print(f"\nLatest 20 articles:")

    article_types = {"original": 0, "synthetic": 0, "unknown": 0}

    for i, article_id in enumerate(feed, 1):
        if isinstance(article_id, bytes):
            article_id = article_id.decode()

        article_type = await redis.hget(f"news:{article_id}", "type")
        if article_type:
            article_type = (
                article_type.decode()
                if isinstance(article_type, bytes)
                else article_type
            )
            article_types[article_type] = article_types.get(article_type, 0) + 1
        else:
            article_type = "unknown"
            article_types["unknown"] += 1

        category = await redis.hget(f"news:{article_id}", "category")
        category = (
            category.decode() if isinstance(category, bytes) else (category or "?")
        )

        # Check if would pass filter
        would_pass = True
        if filter_mode == "include" and filter_types:
            would_pass = article_type in filter_types
        elif filter_mode == "exclude" and filter_types:
            would_pass = article_type not in filter_types

        status = "✅" if would_pass else "❌"
        print(
            f"  {i:2d}. {article_id[:12]} | {category:8s} | {article_type:9s} | {status}"
        )

    print(f"\nArticle type distribution:")
    for atype, count in article_types.items():
        print(f"  {atype}: {count}")

    # Calculate how many would pass filter
    if filter_mode != "all":
        passed = sum(
            1
            for _, v in article_types.items()
            if (
                (filter_mode == "include" and v in filter_types)
                or (filter_mode == "exclude" and v not in filter_types)
            )
        )
        print(f"\n🎯 Articles that would pass filter: {passed}/20")

    await redis.aclose()
    db.close()

    # Recommendations
    print("\n" + "=" * 80)
    print("💡 RECOMMENDATIONS")
    print("=" * 80)

    rate_limit = webhook.get("rate_limit_max", 0)
    window = webhook.get("rate_limit_window_minutes", 1)

    if rate_limit == 0:
        print("✅ Rate limit: UNLIMITED (rate_limit_max = 0)")
    elif rate_limit == 1:
        print(f"⚠️  Rate limit: {rate_limit} message / {window} minute")
        print(f"   → Chỉ gửi 1 message mỗi phút")
        print(f"   → Các messages khác sẽ bị BỎ QUA (không queue)")
        print(f"   → Hành vi 'dừng' là BÌNH THƯỜNG với setting này")
        print(f"\n   Nếu muốn gửi nhiều hơn:")
        print(f"   1. Tăng rate_limit_max (ví dụ: 2-5)")
        print(f"   2. Hoặc tăng rate_limit_window_minutes (ví dụ: 5-10)")
    else:
        print(f"✅ Rate limit: {rate_limit} messages / {window} minute(s)")
        print(f"   → Webhook sẽ gửi tối đa {rate_limit} messages mỗi {window} phút")

    if filter_mode == "all" and filter_types:
        print(f"\n⚠️  Article type filter không hoạt động!")
        print(f"   → Mode = 'all' → bỏ qua filter_article_types")
        print(f"   → Để filter synthetic articles:")
        print(f"      filter_article_types_mode: include")
        print(f"      filter_article_types: [synthetic]")

    if article_types.get("synthetic", 0) == 0:
        print(f"\n⚠️  Không có synthetic articles!")
        print(f"   → Topic synthesis job có thể chưa chạy")
        print(f"   → Hoặc không đủ articles per category (cần ≥5)")
        print(f"   → Kiểm tra: tail -f /tmp/news-aggregator.log | grep synthesis")

    print("\n" + "=" * 80)


if __name__ == "__main__":
    asyncio.run(main())

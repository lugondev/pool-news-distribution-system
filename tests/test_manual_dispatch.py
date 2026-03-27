#!/usr/bin/env python3
"""
Manually dispatch a synthetic article to webhook for testing
"""

import asyncio
import yaml

import redis.asyncio as aioredis

from webhook.dispatcher import dispatch_article


async def main():
    print("=" * 80)
    print("🚀 MANUAL WEBHOOK DISPATCH TEST")
    print("=" * 80)

    # Load config
    with open("config/settings.yaml") as f:
        config = yaml.safe_load(f)

    webhook_config = config.get("webhook", {})
    endpoints = webhook_config.get("endpoints", [])

    # Find tools-test-polistic
    target_webhook = None
    for ep in endpoints:
        if ep.get("id") == "tools-test-polistic":
            target_webhook = ep
            break

    if not target_webhook:
        print("❌ Webhook 'tools-test-polistic' not found in config")
        return

    print(f"\n📋 Webhook Configuration:")
    print(f"   ID: {target_webhook.get('id')}")
    print(f"   Name: {target_webhook.get('name')}")
    print(f"   URL: {target_webhook.get('url')}")
    print(f"   Enabled: {target_webhook.get('enabled')}")
    print(f"   Filter mode: {target_webhook.get('filter_article_types_mode')}")
    print(f"   Filter types: {target_webhook.get('filter_article_types')}")
    print(
        f"   Rate limit: {target_webhook.get('rate_limit_max')}/{target_webhook.get('rate_limit_window_minutes')}min"
    )

    # Connect to Redis
    redis = await aioredis.from_url("redis://localhost:6379/0")

    # Get latest synthetic article
    feed = await redis.zrevrange("news:feed", 0, 19, withscores=False)

    synthetic_article = None
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
            if article_type == "synthetic":
                # Found synthetic article
                article_data = await redis.hgetall(f"news:{article_id}")
                synthetic_article = {
                    k.decode() if isinstance(k, bytes) else k: v.decode()
                    if isinstance(v, bytes)
                    else v
                    for k, v in article_data.items()
                }
                break

    if not synthetic_article:
        print("\n❌ No synthetic articles found in Redis")
        print("   Run: python create_test_synthetic.py")
        await redis.aclose()
        return

    print(f"\n📄 Found Synthetic Article:")
    print(f"   ID: {synthetic_article.get('id')}")
    print(f"   Type: {synthetic_article.get('type')}")
    print(f"   Category: {synthetic_article.get('category')}")
    print(f"   Title (EN): {synthetic_article.get('title_en', 'N/A')}")
    print(f"   Summary (EN): {synthetic_article.get('ai_summary_en', 'N/A')[:80]}...")

    # Check if would pass filter
    from webhook.filters import passes_filter

    would_pass = passes_filter(synthetic_article, target_webhook)

    print(f"\n🔍 Filter Check:")
    print(f"   Article type: {synthetic_article.get('type')}")
    print(f"   Filter mode: {target_webhook.get('filter_article_types_mode')}")
    print(f"   Filter types: {target_webhook.get('filter_article_types')}")
    print(f"   Would pass: {'✅ YES' if would_pass else '❌ NO'}")

    if not would_pass:
        print("\n⚠️  Article would NOT pass filter!")
        print("   Aborting dispatch test.")
        await redis.aclose()
        return

    # Dispatch
    print(f"\n🚀 Dispatching to webhook...")

    try:
        await dispatch_article(
            synthetic_article, [target_webhook], telegram_channels=[]
        )
        print("✅ Dispatch completed!")
    except Exception as e:
        print(f"❌ Dispatch failed: {e}")

    # Check logs
    print(f"\n📊 Check webhook logs:")
    print(
        f"   sqlite3 data/stats.db \"SELECT article_id, success, sent_at FROM webhook_logs WHERE article_id = '{synthetic_article.get('id')}' ORDER BY sent_at DESC LIMIT 5;\""
    )

    await redis.aclose()

    print("\n" + "=" * 80)


if __name__ == "__main__":
    asyncio.run(main())

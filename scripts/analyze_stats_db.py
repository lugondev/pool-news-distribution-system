#!/usr/bin/env python3
"""
Analyze stats.db size and provide cleanup recommendations.

Usage:
    python scripts/analyze_stats_db.py [--db-path ./data/stats.db]
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import aiosqlite


async def get_table_stats(db_path: str) -> dict:
    """Analyze each log table in stats.db."""
    
    tables = {
        "crawl_logs": "started_at",
        "webhook_logs": "sent_at",
        "ai_logs": "created_at",
        "telegram_logs": "sent_at",
        "system_logs": "started_at",
        "api_logs": "requested_at",
        "channel_logs": "requested_at",
    }
    
    results = {}
    total_rows = 0
    
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        
        # Get database file size
        file_size = os.path.getsize(db_path)
        results["_meta"] = {
            "file_size_mb": round(file_size / 1024 / 1024, 2),
            "file_size_bytes": file_size,
            "file_path": db_path,
        }
        
        for table, ts_col in tables.items():
            try:
                # Total rows
                count_result = await db.execute_fetchall(f"SELECT COUNT(*) as cnt FROM {table}")
                count = count_result[0]["cnt"] if count_result else 0
                total_rows += count
                
                # Age distribution
                now = datetime.now(timezone.utc)
                age_bands = {
                    "< 1h": now - timedelta(hours=1),
                    "1-5h": now - timedelta(hours=5),
                    "5-24h": now - timedelta(days=1),
                    "1-7d": now - timedelta(days=7),
                    "7-30d": now - timedelta(days=30),
                    "> 30d": None,
                }
                
                age_dist = {}
                prev_cutoff = None
                for band_name, cutoff in age_bands.items():
                    if cutoff is None:
                        # > 30d
                        query = f"SELECT COUNT(*) as cnt FROM {table} WHERE {ts_col} < ?"
                        params = [(now - timedelta(days=30)).isoformat()]
                    elif prev_cutoff is None:
                        # < 1h
                        query = f"SELECT COUNT(*) as cnt FROM {table} WHERE {ts_col} >= ?"
                        params = [cutoff.isoformat()]
                    else:
                        # Between bands
                        query = f"SELECT COUNT(*) as cnt FROM {table} WHERE {ts_col} >= ? AND {ts_col} < ?"
                        params = [cutoff.isoformat(), prev_cutoff.isoformat()]
                    
                    result = await db.execute_fetchall(query, params)
                    age_dist[band_name] = result[0]["cnt"] if result else 0
                    prev_cutoff = cutoff
                
                # Oldest record
                oldest_result = await db.execute_fetchall(
                    f"SELECT MIN({ts_col}) as oldest FROM {table}"
                )
                oldest = oldest_result[0]["oldest"] if oldest_result else None
                
                # Newest record
                newest_result = await db.execute_fetchall(
                    f"SELECT MAX({ts_col}) as newest FROM {table}"
                )
                newest = newest_result[0]["newest"] if newest_result else None
                
                # Approximate table size (SQLite doesn't provide per-table size easily)
                # We'll use row count as proxy
                avg_row_size = file_size / total_rows if total_rows > 0 else 0
                table_size_est = count * avg_row_size
                
                results[table] = {
                    "total_rows": count,
                    "age_distribution": age_dist,
                    "oldest_record": oldest,
                    "newest_record": newest,
                    "estimated_size_mb": round(table_size_est / 1024 / 1024, 2),
                }
                
            except Exception as e:
                results[table] = {"error": str(e)}
        
        results["_meta"]["total_rows"] = total_rows
    
    return results


def print_analysis(stats: dict):
    """Pretty print analysis with recommendations."""
    
    meta = stats.get("_meta", {})
    print("\n" + "="*70)
    print("📊 STATS.DB ANALYSIS")
    print("="*70)
    print(f"\n📁 File: {meta.get('file_path')}")
    print(f"💾 Size: {meta.get('file_size_mb')} MB ({meta.get('file_size_bytes'):,} bytes)")
    print(f"📝 Total rows: {meta.get('total_rows'):,}")
    
    print("\n" + "-"*70)
    print("📋 TABLE BREAKDOWN")
    print("-"*70)
    
    tables = {k: v for k, v in stats.items() if not k.startswith("_")}
    
    for table_name, table_data in sorted(tables.items(), key=lambda x: x[1].get("total_rows", 0), reverse=True):
        if "error" in table_data:
            print(f"\n❌ {table_name}: ERROR - {table_data['error']}")
            continue
        
        total = table_data["total_rows"]
        age_dist = table_data["age_distribution"]
        oldest = table_data.get("oldest_record")
        newest = table_data.get("newest_record")
        size_mb = table_data.get("estimated_size_mb", 0)
        
        print(f"\n📌 {table_name.upper()}")
        print(f"   Rows: {total:,} (~{size_mb:.2f} MB)")
        print(f"   Oldest: {oldest}")
        print(f"   Newest: {newest}")
        print(f"   Age distribution:")
        for band, count in age_dist.items():
            pct = (count / total * 100) if total > 0 else 0
            bar = "█" * int(pct / 2)
            print(f"      {band:8} {count:6,} ({pct:5.1f}%) {bar}")
    
    print("\n" + "-"*70)
    print("💡 RECOMMENDATIONS")
    print("-"*70)
    
    # Calculate cleanup impact
    total_rows = meta.get("total_rows", 0)
    total_size_mb = meta.get("file_size_mb", 0)
    
    old_rows_5h = sum(
        table_data.get("age_distribution", {}).get("5-24h", 0) +
        table_data.get("age_distribution", {}).get("1-7d", 0) +
        table_data.get("age_distribution", {}).get("7-30d", 0) +
        table_data.get("age_distribution", {}).get("> 30d", 0)
        for table_data in tables.values()
        if "error" not in table_data
    )
    
    old_rows_1h = sum(
        table_data.get("age_distribution", {}).get("1-5h", 0) +
        table_data.get("age_distribution", {}).get("5-24h", 0) +
        table_data.get("age_distribution", {}).get("1-7d", 0) +
        table_data.get("age_distribution", {}).get("7-30d", 0) +
        table_data.get("age_distribution", {}).get("> 30d", 0)
        for table_data in tables.values()
        if "error" not in table_data
    )
    
    old_rows_24h = sum(
        table_data.get("age_distribution", {}).get("1-7d", 0) +
        table_data.get("age_distribution", {}).get("7-30d", 0) +
        table_data.get("age_distribution", {}).get("> 30d", 0)
        for table_data in tables.values()
        if "error" not in table_data
    )
    
    pct_5h = (old_rows_5h / total_rows * 100) if total_rows > 0 else 0
    pct_1h = (old_rows_1h / total_rows * 100) if total_rows > 0 else 0
    pct_24h = (old_rows_24h / total_rows * 100) if total_rows > 0 else 0
    
    print(f"\n🔧 Current cleanup policy: Delete logs >5h (if table has ≥200 rows)")
    print(f"   Would delete: {old_rows_5h:,} rows ({pct_5h:.1f}%)")
    print(f"   Estimated space saved: ~{total_size_mb * pct_5h / 100:.2f} MB")
    
    print(f"\n🚀 Aggressive option: Delete logs >1h")
    print(f"   Would delete: {old_rows_1h:,} rows ({pct_1h:.1f}%)")
    print(f"   Estimated space saved: ~{total_size_mb * pct_1h / 100:.2f} MB")
    
    print(f"\n⚖️  Balanced option: Delete logs >24h")
    print(f"   Would delete: {old_rows_24h:,} rows ({pct_24h:.1f}%)")
    print(f"   Estimated space saved: ~{total_size_mb * pct_24h / 100:.2f} MB")
    
    print("\n📝 Configuration suggestions:")
    
    if total_size_mb > 100:
        print("   ⚠️  Database is >100MB - consider immediate cleanup")
        print("   ✅ Add these settings to config/settings.yaml:")
        print()
        print("   log_retention:")
        print("     enabled: true")
        print("     max_age_hours: 24        # Keep logs for 24h instead of 5h")
        print("     cleanup_interval_hours: 2 # Run cleanup every 2h instead of 5h")
        print("     min_rows_threshold: 100   # Lower threshold from 200 to 100")
        print()
    elif total_size_mb > 50:
        print("   ⚠️  Database is >50MB - cleanup policy could be more aggressive")
        print("   ✅ Consider shortening retention to 3h or running cleanup more frequently")
    else:
        print("   ✅ Current policy seems adequate for your usage")
    
    print("\n" + "="*70)


async def main():
    parser = argparse.ArgumentParser(description="Analyze stats.db size and log retention")
    parser.add_argument(
        "--db-path",
        default="./data/stats.db",
        help="Path to stats.db file (default: ./data/stats.db)"
    )
    args = parser.parse_args()
    
    db_path = args.db_path
    
    if not os.path.exists(db_path):
        print(f"❌ Error: Database file not found: {db_path}")
        print(f"   Current directory: {os.getcwd()}")
        sys.exit(1)
    
    print("🔍 Analyzing stats.db...")
    stats = await get_table_stats(db_path)
    print_analysis(stats)


if __name__ == "__main__":
    asyncio.run(main())

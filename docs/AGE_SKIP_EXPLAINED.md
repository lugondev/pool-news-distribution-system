# Age Skip Logic Explained

## Problem Overview

When a Telegram channel is configured to receive **all categories** (`filter_categories_mode: all`), users may notice that some articles get skipped with the status `age_skipped` in Redis. This document explains why this happens and how to adjust it.

## Root Cause

The AI processing system includes an **age-based filtering mechanism** to avoid wasting API quota on stale articles. The logic works as follows:

### 1. Category Volume Analysis

Every AI job batch samples recent articles (last 2 hours) and counts how many articles each category has fetched. This creates a volume distribution across categories.

### 2. Dynamic Age Thresholds

Categories are classified into 3 tiers based on their article volume:

| Tier | Volume Threshold | Default Max Age | Rationale |
|------|-----------------|-----------------|-----------|
| **Busy** | Top 1/3 by count | 15 minutes | High-volume categories (tech, world, ai) get constant fresh articles — older ones can be skipped |
| **Moderate** | Middle 1/3 | 20 minutes | Medium-volume categories still get regular updates |
| **Quiet** | Bottom 1/3 | 30 minutes | Low-volume categories (esports, music) need longer retention to ensure coverage |

### 3. Skip Logic

When the AI job processes an article:
1. Calculate article age: `now - fetched_at`
2. Determine category tier → get max age threshold
3. If `article_age > max_age` → skip with status `age_skipped`

**Code location:** `ai/rewriter.py`, lines 334-481

## Why "All Categories" Channels Are Affected

### Example: `tele-ai-hub-news` Channel

```yaml
telegram:
  channels:
  - id: tele-ai-hub-news
    filter_categories_mode: all  # ← Receives ALL categories
    filter_categories: []
```

**Problem Sequence:**

1. This channel accepts articles from **all 12 categories** (world, tech, business, science, politics, finance, ai, gaming, sports, esports, entertainment, music)

2. Busy categories like `tech`, `ai`, `world` generate many articles → threshold = **15 minutes**

3. The AI job runs every **2 minutes** with a batch size of **10 articles**

4. When AI processing is delayed (heavy load, rate limits, or previous batch still running), articles from busy categories can age beyond 15 minutes

5. Result: **Age skip** — the article is marked `age_skipped` and never processed

### Why Other Channels Are Less Affected

**`@pool_news_spoesenmu`** (sports, entertainment, music, esports):
- **All categories are "quiet"** → max age = 30 minutes
- Much more tolerance for processing delays
- Rarely skipped

**`@pool_news_fipowo`** (finance, politics, world):
- Only 3 categories → less competition for batch slots
- Mix of moderate/busy categories → some tolerance

**`@pool_news_buscite`** (business, science, tech):
- 3 categories, tech is busy but others are moderate

## Solution: Configurable Age Thresholds

### Changed Files

1. **`config/settings.yaml`** — Added new config parameters:
   ```yaml
   ai:
     # Age thresholds for skipping old articles (minutes)
     age_threshold_busy_minutes: 15      # Top 1/3 volume categories
     age_threshold_moderate_minutes: 20  # Middle 1/3
     age_threshold_quiet_minutes: 30     # Bottom 1/3 + unknown
   ```

2. **`ai/rewriter.py`** — Updated `_max_age_for_category()` to read from config

### Recommended Values

For **"all categories"** channels to reduce skips:

```yaml
ai:
  age_threshold_busy_minutes: 30      # Increased from 15
  age_threshold_moderate_minutes: 40  # Increased from 20
  age_threshold_quiet_minutes: 60     # Increased from 30
```

**Trade-off:** Longer retention = more articles processed but also more API usage on potentially stale news.

### Conservative Values (Original)

```yaml
ai:
  age_threshold_busy_minutes: 15
  age_threshold_moderate_minutes: 20
  age_threshold_quiet_minutes: 30
```

**Trade-off:** Aggressive skipping = lower API costs but may miss articles during processing delays.

## Monitoring Age Skips

### Check Redis for Skipped Articles

```bash
# Find articles with age_skipped status
redis-cli --scan --pattern "news:*" | while read key; do
  status=$(redis-cli HGET "$key" ai_status)
  if [ "$status" = "age_skipped" ]; then
    echo "$key: $(redis-cli HGET "$key" title)"
  fi
done
```

### Check SQLite Logs

```bash
sqlite3 data/stats.db "
  SELECT 
    strftime('%H', started_at) as hour,
    COUNT(*) as jobs,
    json_extract(metadata, '$.processed') as articles
  FROM system_logs
  WHERE job_name = 'ai_job' 
    AND started_at > datetime('now', '-1 day')
  GROUP BY hour
  ORDER BY hour;
"
```

## Alternative Approaches

### 1. Disable Age Skip Entirely

Set all thresholds to a very high value:

```yaml
ai:
  age_threshold_busy_minutes: 720    # 12 hours
  age_threshold_moderate_minutes: 720
  age_threshold_quiet_minutes: 720
```

**Warning:** This will process ALL articles regardless of age until they expire from Redis (12 hour TTL). May increase API costs significantly.

### 2. Increase Batch Size

Process more articles per job to reduce queue backlog:

```yaml
ai:
  batch_size: 20  # Increased from 10
  interval_minutes: 2
```

**Trade-off:** Higher API usage per job, but faster queue clearing.

### 3. Increase AI Job Frequency

```yaml
ai:
  interval_minutes: 1  # Reduced from 2
  batch_size: 10
```

**Trade-off:** More frequent jobs = faster processing but more scheduler overhead.

## Best Practices

1. **Monitor skip rates** — Check `ai_status=age_skipped` articles daily
2. **Tune per workload** — Adjust thresholds based on your article volume and API budget
3. **Category-specific channels** — Consider splitting "all categories" into multiple focused channels
4. **Test configuration changes** — Restart the app and monitor for 1-2 hours after changing thresholds

## Technical Details

### Age Skip Flow (Code Path)

```
scheduler.py:ai_job()
  ↓
ai/rewriter.py:process_pending_articles()
  ↓
ai/rewriter.py:_get_category_counts()  # Sample last 2 hours
  ↓
[For each article in batch]
  ↓
ai/rewriter.py:_max_age_for_category()  # Determine threshold
  ↓
Check: article_age > max_age?
  ↓ YES
  Set Redis: ai_status = "age_skipped"
  Skip article
  ↓ NO
  Continue to AI processing
```

### Category Volume Calculation

**Function:** `_get_category_counts()` (line 309-331)

- Samples up to **500 recent articles** from `news:feed` sorted set
- Counts articles per category within **2-hour window**
- Returns `dict[category_id, count]`

**Example output:**
```python
{
  'tech': 85,      # Busy (top 1/3)
  'world': 72,     # Busy
  'ai': 68,        # Busy
  'business': 42,  # Moderate (middle 1/3)
  'science': 38,   # Moderate
  'politics': 35,  # Moderate
  'finance': 28,   # Quiet (bottom 1/3)
  'gaming': 15,    # Quiet
  'sports': 12,    # Quiet
  'esports': 5,    # Quiet
  'entertainment': 4,  # Quiet
  'music': 2       # Quiet
}
```

### Tier Calculation Logic

```python
sorted_vals = sorted(counts.values())  # [2, 4, 5, 12, 15, 28, 35, 38, 42, 68, 72, 85]
n = len(sorted_vals)  # 12
low_thresh = sorted_vals[n // 3]      # sorted_vals[4] = 15
high_thresh = sorted_vals[(n*2) // 3] # sorted_vals[8] = 42

# Category with count >= 42 → Busy (15 min)
# Category with count >= 15 and < 42 → Moderate (20 min)
# Category with count < 15 → Quiet (30 min)
```

## Summary

The "age skip" mechanism is a **cost optimization feature** that prevents wasting AI API quota on old articles. It works well for category-specific channels but can be aggressive for "all categories" channels. 

**Solution:** Increase age thresholds in `settings.yaml` based on your tolerance for older articles and API budget.

**Config location:** `config/settings.yaml` → `ai:age_threshold_*_minutes`

**Default values (as of this fix):**
- Busy: 15 minutes
- Moderate: 20 minutes  
- Quiet: 30 minutes

**Recommended for "all categories" channels:**
- Busy: 30-45 minutes
- Moderate: 40-60 minutes
- Quiet: 60-90 minutes

# Topic-Based News Synthesis

## Tổng quan

Thay vì dịch từng bài riêng lẻ (1 bài RSS → 1 AI summary), hệ thống mới có khả năng:

**Nhóm nhiều bài cùng chủ đề → AI tự quyết định số lượng outputs → Tạo 1-8 bài tổng hợp với các góc nhìn khác nhau**

### Ví dụ

**Input:** 5 bài politics trong 1 giờ về US-China trade tensions

**AI tự động tạo ra:**
1. Timeline summary (diễn biến theo thời gian)
2. US perspective (góc nhìn từ Mỹ)
3. China perspective (góc nhìn từ Trung Quốc)
4. Economic impact (ảnh hưởng kinh tế)
5. Tech sector implications (tác động lên ngành công nghệ)

**Không cần config số lượng outputs** — AI tự quyết định dựa trên:
- Độ đa dạng nội dung (nhiều góc nhìn → nhiều outputs)
- Độ phức tạp chủ đề (breaking news đơn giản → 1-2 bài; geopolitics → 5-8 bài)
- Số nguồn khác nhau (5 nguồn khác nhau → nhiều perspectives)
- Timeline span (trong 1 giờ vs trong 24h)

---

## Architecture

### Storage Schema

```redis
# Original articles (từ RSS)
news:{article_id}              → Hash (standard article)
news:feed                      → Sorted Set (all articles)
news:cat:{category}            → Sorted Set (per category)

# Synthetic articles (AI-generated)
news:{synth_id}                → Hash (synthetic article)
news:synth:feed                → Sorted Set (all synthetic)
news:synth:cat:{category}      → Sorted Set (per category)
```

### Synthetic Article Fields

```json
{
  "id": "synth_abc123_0",
  "type": "synthetic",
  "category": "politics",
  "angle": "timeline|analysis|comparison|impact|perspective|summary",
  "title_vi": "Tiêu đề tiếng Việt",
  "title_en": "English title",
  "content_vi": "Nội dung tóm tắt tiếng Việt (3-4 câu)",
  "content_en": "English summary content (3-4 sentences)",
  "source_article_ids": ["id1", "id2", "id3", "id4", "id5"],
  "num_source_articles": 5,
  "created_at": "2026-03-24T10:00:00Z",
  "ai_model": "gpt-4o-mini",
  "ai_tokens": 450,
  "ai_analysis": "AI's reasoning for why it generated N outputs"
}
```

---

## Configuration

### Enable Topic Synthesis

Edit `config/settings.yaml`:

```yaml
ai:
  # Existing single-article translation
  enabled: true
  interval_minutes: 2
  batch_size: 10
  
  # NEW: Topic synthesis
  topic_synthesis:
    enabled: true               # Set to false to disable
    interval_minutes: 5         # Run every 5 minutes
    min_articles: 5             # Minimum articles needed per category
    max_articles: 15            # Maximum articles to analyze per batch
    temperature: 0.5            # Higher = more creative outputs
    max_tokens: 2000            # Max tokens per synthesis call
```

### How It Works

1. **Scheduler Job** (runs every 5 minutes):
   - For each active category (politics, tech, business, ...)
   - Fetch 15 most recent articles (excluding synthetic)
   - If >= 5 articles available → call AI

2. **AI Analysis**:
   - AI receives: 15 articles + metadata (time span, # sources)
   - AI decides: How many summaries to generate (1-8)
   - AI outputs: JSON array with N summaries

3. **Storage**:
   - Each synthetic article saved to Redis (12h TTL)
   - Indexed in both main feed and synthetic-specific feed
   - Available via webhooks/Telegram

---

## API Usage

### Query All Articles (Mixed)

```bash
curl http://localhost:8000/api/news?limit=20
```

Returns: Original RSS articles + Synthetic articles, mixed by timestamp

### Query Only Synthetic Articles

```bash
curl http://localhost:8000/api/news?article_type=synthetic&limit=20
```

### Query Only Original RSS Articles

```bash
curl http://localhost:8000/api/news?article_type=original&limit=20
```

### Query Synthetic by Category

```bash
curl http://localhost:8000/api/news?article_type=synthetic&category=politics
```

---

## Response Format

### Original Article

```json
{
  "id": "abc123",
  "type": null,  // or absent
  "source_id": "bbc-world",
  "source_name": "BBC World",
  "title": "Original article title",
  "url": "https://...",
  "ai_summary_vi": "Tóm tắt",
  "ai_summary_en": "Summary",
  "category": "politics",
  "published_at": "2026-03-24T09:00:00Z"
}
```

### Synthetic Article

```json
{
  "id": "synth_def456_0",
  "type": "synthetic",
  "category": "politics",
  "angle": "timeline",
  "title_vi": "Diễn biến căng thẳng thương mại Mỹ-Trung",
  "title_en": "Timeline of US-China trade tensions",
  "content_vi": "Trong 6 giờ qua, Mỹ tuyên bố tăng thuế 25% lên hàng Trung Quốc...",
  "content_en": "Over the past 6 hours, the US announced 25% tariffs on Chinese goods...",
  "source_article_ids": ["id1", "id2", "id3", "id4", "id5"],
  "num_source_articles": 5,
  "created_at": "2026-03-24T10:00:00Z"
}
```

---

## Webhook Integration

### Existing Webhooks Still Work

- Webhooks continue to receive original articles (no change)
- Synthetic articles also flow through webhooks
- Filter by checking `article.type == "synthetic"`

### Filter Synthetic Articles

```yaml
webhook:
  endpoints:
    - id: my-webhook
      url: https://example.com/webhook
      enabled: true
      # Option 1: Filter in your webhook handler
      # Check article["type"] == "synthetic" and skip/handle differently
      
      # Option 2: Category filtering still works
      filter_categories_mode: include
      filter_categories:
        - politics  # Only politics (both original + synthetic)
```

### Telegram Channels

Same behavior — synthetic articles will be sent to Telegram channels unless filtered.

---

## Monitoring

### Check Scheduler Logs

```bash
# View system logs
curl http://localhost:8000/api/logs/system?limit=20 | jq

# Look for "topic_synthesis_job" entries
# Check metadata:
#   - total_generated: Number of synthetic articles created
#   - categories_processed: Number of categories analyzed
#   - results: Per-category breakdown
```

### Example Log Entry

```json
{
  "job_name": "topic_synthesis_job",
  "started_at": "2026-03-24T10:00:00Z",
  "status": "ok",
  "metadata": {
    "total_generated": 12,
    "categories_processed": 8,
    "categories_with_output": 3,
    "results": {
      "politics": 5,
      "tech": 4,
      "business": 3
    }
  }
}
```

### Cost Tracking

Each synthesis call uses more tokens than single-article translation:

- **Single article**: ~150-200 tokens input + 100-150 tokens output = **~300 tokens**
- **Topic synthesis** (5 articles → 3 outputs): ~800 tokens input + 600 tokens output = **~1400 tokens**

**But more efficient overall:**
- Old way: 5 articles × 300 tokens = **1500 tokens** → 5 similar summaries
- New way: 5 articles → **1400 tokens** → 3 diverse summaries

---

## Testing

### Manual Test

1. **Enable feature:**
   ```yaml
   # config/settings.yaml
   ai:
     topic_synthesis:
       enabled: true
       min_articles: 3  # Lower threshold for testing
   ```

2. **Restart application:**
   ```bash
   python main.py
   ```

3. **Wait for crawl job** to populate articles (default: every 3 minutes)

4. **Wait for synthesis job** (default: every 5 minutes)

5. **Query results:**
   ```bash
   curl http://localhost:8000/api/news?article_type=synthetic | jq
   ```

### Check AI Decision-Making

Look at the `ai_analysis` field in synthetic articles:

```bash
curl http://localhost:8000/api/articles/synth_abc123_0 | jq .ai_analysis
```

Example output:
```
"These 5 articles cover 3 distinct narratives: US tariff announcement (3 articles), 
China's response (1 article), and market reactions (1 article). Generated 3 summaries: 
timeline, US perspective, and market impact."
```

---

## Troubleshooting

### No Synthetic Articles Generated

**Possible reasons:**

1. **Feature disabled:**
   ```yaml
   ai:
     topic_synthesis:
       enabled: false  # Change to true
   ```

2. **Not enough articles:**
   - Check `min_articles` setting (default: 5)
   - Run `curl http://localhost:8000/api/news?category=politics&limit=20` to see if enough articles exist

3. **AI error:**
   - Check logs: `curl http://localhost:8000/api/logs/system?limit=50 | jq`
   - Look for "topic_synthesis_job" with `status: "error"`

4. **Job not running:**
   - Check scheduler is active: `curl http://localhost:8000/health`
   - Verify `interval_minutes` is not too large

### Synthetic Articles Look Too Similar

**AI is generating redundant outputs** — increase `temperature`:

```yaml
ai:
  topic_synthesis:
    temperature: 0.7  # Higher = more diverse (default: 0.5)
```

### Too Many/Too Few Outputs

AI decides autonomously, but you can adjust guardrails in `ai/topic_synthesis.py`:

```python
# Line 81-82: Adjust min/max outputs
"Generate between 1 and 8 summaries"
# Change to: "Generate between 2 and 5 summaries"
```

---

## Advanced Usage

### Custom Prompt

You can override the synthesis prompt by editing `ai/topic_synthesis.py`:

```python
# Line 30: TOPIC_SYNTHESIS_PROMPT
TOPIC_SYNTHESIS_PROMPT = """
Your custom prompt here...
"""
```

### Per-Category Thresholds

Currently all categories use same `min_articles` threshold. To customize:

```python
# ai/topic_synthesis.py - process_category_synthesis()
# Add category-specific logic:
if category == "politics":
    min_articles = 3  # Politics is busy, lower threshold
elif category == "entertainment":
    min_articles = 10  # Entertainment is noisy, higher threshold
```

---

## Migration from Old System

### Backward Compatibility

✅ **No breaking changes** — existing workflows continue to work:

- Original article translation still runs (default: every 2 minutes)
- Webhooks receive same format
- Telegram channels work as before

### Gradual Rollout

1. **Phase 1:** Enable synthesis for 1 category only
   ```yaml
   categories:
     - id: politics
       enabled: true
     - id: tech
       enabled: false  # Disable others temporarily
   ```

2. **Phase 2:** Monitor cost and quality for 1-2 days

3. **Phase 3:** Enable for all categories

---

## FAQ

### Q: Sẽ tốn bao nhiêu token?

**A:** Phụ thuộc vào số lượng articles và outputs AI tạo ra:
- 5 articles → 3 outputs ≈ **1400 tokens** (~$0.0002 với GPT-4o-mini)
- 10 articles → 5 outputs ≈ **2500 tokens** (~$0.0004)

**Hiệu quả hơn dịch từng bài** khi có nhiều bài tương tự.

### Q: AI có thể tạo quá nhiều bài không?

**A:** Không. Hệ thống giới hạn **max 8 outputs** per batch. Nếu AI cố trả về 10 bài, chỉ 8 bài đầu được lưu.

### Q: Làm sao biết AI quyết định đúng?

**A:** Check field `ai_analysis` — AI phải giải thích lý do:
```json
{
  "ai_analysis": "Identified 3 distinct angles: timeline, impact, and perspectives"
}
```

Nếu AI nói "5 articles cover same event" nhưng lại generate 8 outputs → có thể AI đang hallucinate.

### Q: Có thể disable cho một số category?

**A:** Có. Disable category trong `settings.yaml`:
```yaml
categories:
  - id: entertainment
    enabled: false  # Skip synthesis for entertainment
```

### Q: Synthetic articles có được dispatch qua webhook không?

**A:** Có. Chúng flow qua webhook/Telegram giống original articles. Webhook handler có thể check `article.type == "synthetic"` để phân biệt.

---

## Next Steps

- [ ] Add UI toggle in dashboard Settings page
- [ ] Add real-time stats (synthetic vs original ratio)
- [ ] Add per-category enable/disable
- [ ] Add custom angle templates (user-defined perspectives)
- [ ] Add quality scoring (user feedback on synthetic articles)

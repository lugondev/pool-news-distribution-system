#!/usr/bin/env python3
"""
Test webhook template rendering with synthetic articles.
Verifies that payload_template correctly uses synthetic article fields.
"""

import asyncio
import json
from jinja2 import Template

# Sample synthetic article (from Redis)
SYNTHETIC_ARTICLE = {
    "id": "synth_27ddb31f13ddc357_6",
    "type": "synthetic",
    "category": "sports",
    "angle": "perspective B|summary",
    "title_en": "Manchester United Faces Potential Loss of Casemiro",
    "title_vi": "Manchester United có thể mất Casemiro miễn phí",
    "content_en": "Manchester United risks losing Casemiro at the end of the season when his contract expires. Failing to renew could impact the team's strength.",
    "content_vi": "Manchester United có nguy cơ mất đi Casemiro vào cuối mùa giải khi hợp đồng đáo hạn.",
    "source_article_ids": ["e319e928a06a6509", "c0d5d5ca3f7f8816"],
    "num_source_articles": 6,
    "created_at": "2026-03-25T05:30:23.943702+00:00",
    "ai_model": "google/gemma-3n-e4b-it",
    "ai_tokens": 373,
}

# Sample original article (for comparison)
ORIGINAL_ARTICLE = {
    "id": "e319e928a06a6509",
    "type": "original",
    "title": "Manchester United Transfer News",
    "url": "https://example.com/article",
    "source_id": "bbc_sport_en",
    "category": "sports",
    "language": "en",
    "ai_summary_en": "Manchester United is considering a major transfer...",
    "ai_summary_vi": "Manchester United đang cân nhắc một vụ chuyển nhượng lớn...",
}

TEMPLATES = {
    "❌ Old (only works for original)": {
        "template": '{\n  "profile_id": "9e660146-1430-4481-956e-2dd1c579f8a0",\n  "content": "{{ ai_summary_en }}",\n  "is_blue_verified": true\n}',
    },
    "✅ New (synthetic only)": {
        "template": '{\n  "profile_id": "9e660146-1430-4481-956e-2dd1c579f8a0",\n  "content": "{{ content_en }}",\n  "is_blue_verified": true\n}',
    },
    "✅ Universal (with fallback)": {
        "template": '{\n  "profile_id": "9e660146-1430-4481-956e-2dd1c579f8a0",\n  "content": "{{ content_en|default(ai_summary_en, true) }}",\n  "title": "{{ title_en|default(title, true) }}",\n  "is_blue_verified": true\n}',
    },
    "✅ Universal (with conditions)": {
        "template": '{\n  "profile_id": "9e660146-1430-4481-956e-2dd1c579f8a0",\n  "content": "{% if type == \'synthetic\' %}{{ content_en }}{% else %}{{ ai_summary_en }}{% endif %}",\n  "is_blue_verified": true\n}',
    },
    "✅ Rich format (with metadata)": {
        "template": """{\n  "profile_id": "9e660146-1430-4481-956e-2dd1c579f8a0",\n  "content": "{{ content_en|default(ai_summary_en, true) }}",\n  "metadata": {\n    "type": "{{ type }}",\n    "category": "{{ category }}",\n    {% if type == 'synthetic' %}"angle": "{{ angle }}",\n    "num_sources": {{ num_source_articles }}{% else %}"source": "{{ source_id }}",\n    "url": "{{ url }}"{% endif %}\n  },\n  "is_blue_verified": true\n}""",
    },
}


def test_template(name: str, template_str: str, article: dict) -> tuple[bool, str, str]:
    """Test a template against an article."""
    try:
        template = Template(template_str)
        rendered = template.render(**article)

        # Try to parse as JSON to verify validity
        parsed = json.loads(rendered)

        # Check if content is empty
        content = parsed.get("content", "")
        if not content or content.strip() == "":
            return False, "Empty content", rendered

        return True, "OK", rendered
    except Exception as e:
        return False, str(e), ""


def main():
    print("=" * 80)
    print("WEBHOOK TEMPLATE TEST - Synthetic vs Original Articles")
    print("=" * 80)

    for article_name, article in [
        ("Synthetic Article", SYNTHETIC_ARTICLE),
        ("Original Article", ORIGINAL_ARTICLE),
    ]:
        print(f"\n{'=' * 80}")
        print(f"Testing with: {article_name}")
        print(f"Type: {article['type']}, Category: {article['category']}")
        print(f"{'=' * 80}\n")

        for template_name, config in TEMPLATES.items():
            template_str = config["template"]
            success, message, rendered = test_template(
                template_name, template_str, article
            )

            status = "✅ PASS" if success else "❌ FAIL"
            print(f"{status} {template_name}")

            if success:
                # Pretty print JSON
                try:
                    parsed = json.loads(rendered)
                    print(f"   Content: {parsed.get('content', '')[:80]}...")
                    if "metadata" in parsed:
                        print(
                            f"   Metadata: {json.dumps(parsed['metadata'], ensure_ascii=False)}"
                        )
                except:
                    pass
            else:
                print(f"   Error: {message}")

            print()

    print("=" * 80)
    print("RECOMMENDATION")
    print("=" * 80)
    print("""
For webhook 'tools-test-polistic' with filter_article_types: [synthetic]:

CURRENT CONFIG (BROKEN):
  payload_template: '{
    "profile_id": "9e660146-1430-4481-956e-2dd1c579f8a0",
    "content": "{{ ai_summary_en }}",  <-- WRONG! Empty for synthetic
    "is_blue_verified": true
  }'

RECOMMENDED FIX:
  payload_template: '{
    "profile_id": "9e660146-1430-4481-956e-2dd1c579f8a0",
    "content": "{{ content_en }}",  <-- Correct for synthetic
    "is_blue_verified": true
  }'

Or if you want to support BOTH types:
  payload_template: '{
    "profile_id": "9e660146-1430-4481-956e-2dd1c579f8a0",
    "content": "{{ content_en|default(ai_summary_en, true) }}",
    "is_blue_verified": true
  }'
    """)

    print("\nSee SYNTHETIC_ARTICLE_FIELDS.md for complete field reference.")


if __name__ == "__main__":
    main()

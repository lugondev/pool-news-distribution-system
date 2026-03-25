#!/usr/bin/env python3
"""
Test article type filtering for webhooks and Telegram.
Validates that filters work correctly with different modes.
"""

import sys


def test_filter_logic():
    """Test the filter logic with various article types and configs."""
    print("=== Testing Article Type Filter Logic ===\n")

    # Import filter function
    try:
        from webhook.filters import passes_filter
    except ImportError:
        print("❌ Failed to import passes_filter from webhook.filters")
        return False

    # Test cases: (article, config, expected_result, description)
    test_cases = [
        # Mode: all (no filtering)
        (
            {"type": "original", "category": "tech"},
            {"filter_article_types_mode": "all", "filter_article_types": []},
            True,
            "All mode: original article should pass",
        ),
        (
            {"type": "synthetic", "category": "tech"},
            {"filter_article_types_mode": "all", "filter_article_types": []},
            True,
            "All mode: synthetic article should pass",
        ),
        # Mode: include
        (
            {"type": "original", "category": "tech"},
            {
                "filter_article_types_mode": "include",
                "filter_article_types": ["original"],
            },
            True,
            "Include original: original article should pass",
        ),
        (
            {"type": "synthetic", "category": "tech"},
            {
                "filter_article_types_mode": "include",
                "filter_article_types": ["original"],
            },
            False,
            "Include original: synthetic article should fail",
        ),
        (
            {"type": "synthetic", "category": "tech"},
            {
                "filter_article_types_mode": "include",
                "filter_article_types": ["synthetic"],
            },
            True,
            "Include synthetic: synthetic article should pass",
        ),
        (
            {"type": "original", "category": "tech"},
            {
                "filter_article_types_mode": "include",
                "filter_article_types": ["synthetic"],
            },
            False,
            "Include synthetic: original article should fail",
        ),
        # Mode: exclude
        (
            {"type": "original", "category": "tech"},
            {
                "filter_article_types_mode": "exclude",
                "filter_article_types": ["synthetic"],
            },
            True,
            "Exclude synthetic: original article should pass",
        ),
        (
            {"type": "synthetic", "category": "tech"},
            {
                "filter_article_types_mode": "exclude",
                "filter_article_types": ["synthetic"],
            },
            False,
            "Exclude synthetic: synthetic article should fail",
        ),
        (
            {"type": "synthetic", "category": "tech"},
            {
                "filter_article_types_mode": "exclude",
                "filter_article_types": ["original"],
            },
            True,
            "Exclude original: synthetic article should pass",
        ),
        (
            {"type": "original", "category": "tech"},
            {
                "filter_article_types_mode": "exclude",
                "filter_article_types": ["original"],
            },
            False,
            "Exclude original: original article should fail",
        ),
        # Backward compatibility: missing type field defaults to "original"
        (
            {"category": "tech"},  # No type field
            {
                "filter_article_types_mode": "include",
                "filter_article_types": ["original"],
            },
            True,
            "Missing type: should default to 'original' and pass",
        ),
        (
            {"category": "tech"},  # No type field
            {
                "filter_article_types_mode": "exclude",
                "filter_article_types": ["original"],
            },
            False,
            "Missing type: should default to 'original' and fail exclude",
        ),
    ]

    passed = 0
    failed = 0

    for i, (article, config, expected, description) in enumerate(test_cases, 1):
        result = passes_filter(article, config)
        status = "✅" if result == expected else "❌"

        if result == expected:
            passed += 1
        else:
            failed += 1
            print(f"{status} Test {i}: {description}")
            print(f"   Article: {article}")
            print(f"   Config: {config}")
            print(f"   Expected: {expected}, Got: {result}\n")

    print(f"\nResults: {passed}/{len(test_cases)} tests passed")

    if failed > 0:
        print(f"❌ {failed} tests failed\n")
        return False
    else:
        print("✅ All tests passed!\n")
        return True


def test_config_schema():
    """Verify config file has article type filters."""
    print("=== Checking Config Schema ===\n")

    try:
        import yaml

        with open("config/settings.yaml") as f:
            config = yaml.safe_load(f)
    except Exception as e:
        print(f"❌ Failed to load config: {e}\n")
        return False

    # Check webhooks
    webhooks = config.get("webhook", {}).get("endpoints", [])
    webhook_ok = 0
    for ep in webhooks:
        if "filter_article_types_mode" in ep and "filter_article_types" in ep:
            webhook_ok += 1

    print(f"Webhooks: {webhook_ok}/{len(webhooks)} have article type filters")

    # Check Telegram
    channels = config.get("telegram", {}).get("channels", [])
    telegram_ok = 0
    for ch in channels:
        if "filter_article_types_mode" in ch and "filter_article_types" in ch:
            telegram_ok += 1

    print(f"Telegram: {telegram_ok}/{len(channels)} have article type filters\n")

    if webhook_ok == len(webhooks) and telegram_ok == len(channels):
        print("✅ Config schema is correct\n")
        return True
    else:
        print("⚠️ Some endpoints/channels missing article type filters\n")
        return False


def test_ui_fields():
    """Check if UI templates have article type filter fields."""
    print("=== Checking UI Templates ===\n")

    templates_to_check = [
        "dashboard/templates/partials/settings_webhook.html",
        "dashboard/templates/partials/settings_telegram.html",
    ]

    all_ok = True
    for template_path in templates_to_check:
        try:
            with open(template_path) as f:
                content = f.read()

            has_mode = "filter_article_types_mode" in content
            has_list = "filter_article_types" in content
            has_input = "Article Types" in content or "article types" in content.lower()

            if has_mode and has_list and has_input:
                print(f"✅ {template_path.split('/')[-1]}: Has article type filter UI")
            else:
                print(f"❌ {template_path.split('/')[-1]}: Missing UI elements")
                all_ok = False
        except Exception as e:
            print(f"❌ Failed to check {template_path}: {e}")
            all_ok = False

    print()
    return all_ok


def test_backend_handlers():
    """Check if backend handlers accept article type parameters."""
    print("=== Checking Backend Handlers ===\n")

    try:
        with open("dashboard/app.py") as f:
            app_content = f.read()

        # Check for filter_article_types_mode parameter in forms
        webhook_add_ok = "filter_article_types_mode: str = Form" in app_content
        telegram_add_ok = (
            app_content.count("filter_article_types_mode") >= 4
        )  # Should appear in all 4 handlers

        if webhook_add_ok and telegram_add_ok:
            print("✅ dashboard/app.py: All form handlers have article type parameters")
        else:
            print(
                "❌ dashboard/app.py: Missing article type parameters in some handlers"
            )
            return False

        with open("dashboard/api_router.py") as f:
            api_content = f.read()

        # Check API models
        webhook_model_ok = "filter_article_types_mode: str" in api_content
        telegram_model_ok = api_content.count("filter_article_types_mode") >= 4

        if webhook_model_ok and telegram_model_ok:
            print("✅ dashboard/api_router.py: All API models have article type fields")
        else:
            print(
                "❌ dashboard/api_router.py: Missing article type fields in some models"
            )
            return False

        print()
        return True

    except Exception as e:
        print(f"❌ Failed to check backend: {e}\n")
        return False


def main():
    """Run all tests."""
    print("\n" + "=" * 60)
    print("  Article Type Filter - Validation Test Suite")
    print("=" * 60 + "\n")

    results = []

    # Test 1: Filter logic
    results.append(("Filter Logic", test_filter_logic()))

    # Test 2: Config schema
    results.append(("Config Schema", test_config_schema()))

    # Test 3: UI templates
    results.append(("UI Templates", test_ui_fields()))

    # Test 4: Backend handlers
    results.append(("Backend Handlers", test_backend_handlers()))

    # Summary
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)

    for name, passed in results:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"{status}: {name}")

    all_passed = all(result[1] for result in results)

    print("\n" + "=" * 60)
    if all_passed:
        print("🎉 ALL TESTS PASSED - Feature is ready for production!")
    else:
        print("❌ SOME TESTS FAILED - Please fix issues before deploying")
    print("=" * 60 + "\n")

    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())

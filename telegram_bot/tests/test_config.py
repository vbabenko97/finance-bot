"""Invariant tests for the bot's config data modules (accounts + categories).

These modules are shipped to Lambda and drive account resolution and keyword
categorization. They mirror the alias-casing invariant already covered for
merchants in test_merchants.py.
"""

from __future__ import annotations

from telegram_bot.config.accounts import ACCOUNT_GROUPS, ACCOUNTS, DEFAULT_ACCOUNTS
from telegram_bot.config.categories import (
    CATEGORIES,
    CATEGORY_BUCKET,
    CATEGORY_KEYWORDS,
    get_categories_for_mode,
    infer_category,
)

VALID_MODES = {"consumption", "income", "movement"}
VALID_BUCKETS = {"needs", "wants"}


def test_account_groups_reference_known_accounts():
    for group, account_ids in ACCOUNT_GROUPS.items():
        for account_id in account_ids:
            assert account_id in ACCOUNTS, f"{group} references unknown account {account_id}"


def test_default_accounts_exist_and_currency_matches():
    for currency, account_id in DEFAULT_ACCOUNTS.items():
        assert account_id in ACCOUNTS, f"default account {account_id} missing"
        assert ACCOUNTS[account_id][1] == currency


def test_categories_have_valid_modes_and_display_names():
    for cat_id, meta in CATEGORIES.items():
        assert meta["mode"] in VALID_MODES, f"{cat_id} has invalid mode {meta['mode']}"
        assert meta["display_name"], f"{cat_id} has empty display_name"


def test_category_keywords_reference_known_categories():
    # infer_category indexes CATEGORIES[category_id]; an unknown key would KeyError at runtime.
    for cat_id in CATEGORY_KEYWORDS:
        assert cat_id in CATEGORIES, f"CATEGORY_KEYWORDS has unknown category {cat_id}"


def test_category_keywords_are_lowercase():
    # infer_category lowercases the description, so a non-lowercase keyword can never match.
    for cat_id, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            assert kw == kw.lower(), f"{cat_id} keyword {kw!r} is not lowercase"


def test_infer_category_matches_keyword_within_mode():
    assert infer_category("Купив у АТБ продукти") == "groceries"
    assert infer_category("Netflix subscription", mode="consumption") == "subscriptions"
    assert infer_category("Salary payment", mode="income") == "salary"


def test_infer_category_respects_mode():
    # 'salary' is an income keyword; it must not classify in consumption mode.
    assert infer_category("salary", mode="consumption") == "unknown"


def test_every_consumption_category_has_a_bucket():
    # The loud one: adding a consumption category without a bucket would silently
    # drop its spend out of the 50/30/20 split.
    for cat_id, meta in CATEGORIES.items():
        if meta["mode"] != "consumption":
            continue
        assert cat_id in CATEGORY_BUCKET, f"consumption category {cat_id} has no CATEGORY_BUCKET entry"


def test_category_bucket_keys_are_consumption_categories():
    # Income is the denominator and movement is excluded, so neither may be bucketed.
    for cat_id in CATEGORY_BUCKET:
        assert cat_id in CATEGORIES, f"CATEGORY_BUCKET has unknown category {cat_id}"
        assert CATEGORIES[cat_id]["mode"] == "consumption", f"{cat_id} is bucketed but not a consumption category"


def test_category_bucket_values_are_valid():
    # Savings is derived (income - needs - wants), so it is never a bucket value.
    for cat_id, bucket in CATEGORY_BUCKET.items():
        assert bucket in VALID_BUCKETS, f"{cat_id} has invalid bucket {bucket}"


def test_get_categories_for_mode_filters_by_mode():
    income = get_categories_for_mode("income")
    assert "salary" in income
    assert "groceries" not in income
    assert all(CATEGORIES[c]["mode"] == "income" for c in income)

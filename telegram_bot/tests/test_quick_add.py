from __future__ import annotations

from telegram_bot.bot.quick_add import extract_tags, parse_quick_add, parse_quick_add_detailed


def test_basic_eur() -> None:
    tx = parse_quick_add("15 metro")
    assert tx is not None
    assert tx.amount_minor == 1500
    assert tx.currency == "EUR"
    assert tx.description == "metro"
    assert tx.category == "groceries"


def test_with_currency() -> None:
    tx = parse_quick_add("25.50 USD Netflix")
    assert tx is not None
    assert tx.amount_minor == 2550
    assert tx.currency == "USD"
    assert tx.description == "Netflix"


def test_with_comma() -> None:
    tx = parse_quick_add("10,50 Apotheke")
    assert tx is not None
    assert tx.amount_minor == 1050
    assert tx.currency == "EUR"


def test_income() -> None:
    tx = parse_quick_add("5000 Зарплатня", tx_type="income")
    assert tx is not None
    assert tx.signed_amount_minor > 0
    assert tx.mode == "income"


def test_no_match() -> None:
    tx = parse_quick_add("hello")
    assert tx is None


def test_amount_only() -> None:
    tx = parse_quick_add("150")
    assert tx is None


def test_invalid_amount_error() -> None:
    result = parse_quick_add_detailed("hello world")
    assert result.transaction is None
    assert result.error_code == "invalid_amount"


def test_missing_description_error() -> None:
    result = parse_quick_add_detailed("150")
    assert result.transaction is None
    assert result.error_code == "missing_description"


def test_unknown_account_error() -> None:
    result = parse_quick_add_detailed("100 coffee @missing_account")
    assert result.transaction is None
    assert result.error_code == "unknown_account"


def test_unsupported_currency_error() -> None:
    result = parse_quick_add_detailed("25 GBP Coffee")
    assert result.transaction is None
    assert result.error_code == "unsupported_currency"


def test_category_inference() -> None:
    tx = parse_quick_add("200 Bolt таксі")
    assert tx is not None
    assert tx.category == "transport"


def test_unknown_category() -> None:
    tx = parse_quick_add("100 Щось незрозуміле")
    assert tx is not None
    assert tx.category == "unknown"


# ---------------------------------------------------------------------------
# Tag extraction
# ---------------------------------------------------------------------------


def test_extract_tags_no_hash() -> None:
    cleaned, tags = extract_tags("Sunday brunch")
    assert cleaned == "Sunday brunch"
    assert tags == []


def test_extract_tags_single() -> None:
    cleaned, tags = extract_tags("lunch #work")
    assert cleaned == "lunch"
    assert tags == ["work"]


def test_extract_tags_multiple_sorted_deduped() -> None:
    cleaned, tags = extract_tags("#trip #italy2026 dinner #trip")
    assert cleaned == "dinner"
    assert tags == ["italy2026", "trip"]


def test_extract_tags_lowercases_and_strips_punct() -> None:
    cleaned, tags = extract_tags("flight #Italy2026! #Work-Trip")
    assert cleaned == "flight"
    assert tags == ["italy2026", "work-trip"]


def test_extract_tags_drops_pure_punct() -> None:
    cleaned, tags = extract_tags("lunch #!!! coffee")
    assert cleaned == "lunch coffee"
    assert tags == []


def test_extract_tags_ignores_mid_word_hash() -> None:
    cleaned, tags = extract_tags("brand#name lunch")
    assert cleaned == "brand#name lunch"
    assert tags == []


def test_quick_add_extracts_tags() -> None:
    tx = parse_quick_add("40 USD lunch #work")
    assert tx is not None
    assert tx.description == "lunch"
    assert tx.tags == ["work"]


def test_quick_add_multiple_tags() -> None:
    tx = parse_quick_add("80 EUR dinner #italy2026 #trip")
    assert tx is not None
    assert tx.description == "dinner"
    assert tx.tags == ["italy2026", "trip"]


def test_quick_add_only_tags_no_description() -> None:
    result = parse_quick_add_detailed("40 USD #work")
    assert result.transaction is None
    assert result.error_code == "missing_description"

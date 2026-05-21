from __future__ import annotations

from telegram_bot.bot.commands import _match_transaction, _parse_search_args
from telegram_bot.bot.formatters import format_search_results
from telegram_bot.storage.models import Transaction


def _make_tx(
    description: str = "Coffee",
    category: str = "dining",
    category_display: str = "Кафе та ресторани",
    source_account: str = "bank_uah_1",
    date: str = "2026-04-10",
    timestamp: str = "2026-04-10T12:00:00+00:00",
    currency: str = "UAH",
    amount_minor: int = 15000,
    tx_type: str = "expense",
    tags: list[str] | None = None,
) -> Transaction:
    return Transaction(
        tx_id="abc123",
        date=date,
        timestamp=timestamp,
        amount_minor=amount_minor,
        signed_amount_minor=-amount_minor if tx_type == "expense" else amount_minor,
        currency=currency,
        description=description,
        category=category,
        category_display=category_display,
        source_account=source_account,
        tx_type=tx_type,
        tags=tags or [],
    )


# ---------------------------------------------------------------------------
# Parser tests
# ---------------------------------------------------------------------------


class TestParseSearchArgs:
    def test_text_only(self):
        filters, err = _parse_search_args("coffee")
        assert err is None
        assert filters == {"text": "coffee"}

    def test_multi_word_text(self):
        filters, err = _parse_search_args("morning coffee")
        assert err is None
        assert filters == {"text": "morning coffee"}

    def test_category_flag(self):
        filters, err = _parse_search_args("-c groceries")
        assert err is None
        assert filters == {"category": "groceries"}

    def test_account_flag(self):
        filters, err = _parse_search_args("-a bank_uah_1")
        assert err is None
        assert filters == {"account": "bank_uah_1"}

    def test_date_flag_month(self):
        filters, err = _parse_search_args("-d 2026-03")
        assert err is None
        assert filters == {"date": "2026-03"}

    def test_date_flag_day(self):
        filters, err = _parse_search_args("-d 2026-03-15")
        assert err is None
        assert filters == {"date": "2026-03-15"}

    def test_combined_flags(self):
        filters, err = _parse_search_args("coffee -c groceries -d 2026-03")
        assert err is None
        assert filters == {"text": "coffee", "category": "groceries", "date": "2026-03"}

    def test_flags_before_text(self):
        filters, err = _parse_search_args("-c dining -a bank_uah_1 pizza")
        assert err is None
        assert filters == {"category": "dining", "account": "bank_uah_1", "text": "pizza"}

    def test_empty_input(self):
        _filters, err = _parse_search_args("")
        assert err is not None
        assert "Usage" in err

    def test_unknown_flag(self):
        _filters, err = _parse_search_args("-x value")
        assert err is not None
        assert "Unknown flag" in err

    def test_missing_flag_value(self):
        _filters, err = _parse_search_args("-c")
        assert err is not None
        assert "Missing value" in err

    def test_invalid_category(self):
        _filters, err = _parse_search_args("-c nonexistent")
        assert err is not None
        assert "Unknown category" in err

    def test_invalid_account(self):
        _filters, err = _parse_search_args("-a nonexistent")
        assert err is not None
        assert "Unknown account" in err

    def test_invalid_date_format(self):
        _filters, err = _parse_search_args("-d march")
        assert err is not None
        assert "YYYY-MM" in err

    def test_invalid_date_partial(self):
        _filters, err = _parse_search_args("-d 2026")
        assert err is not None
        assert "YYYY-MM" in err

    def test_tag_flag(self):
        filters, err = _parse_search_args("-t work")
        assert err is None
        assert filters == {"tag": "work"}

    def test_tag_flag_normalizes(self):
        filters, err = _parse_search_args("-t #Italy2026!")
        assert err is None
        assert filters == {"tag": "italy2026"}

    def test_multiple_tag_flags_sorted(self):
        filters, err = _parse_search_args("-t trip -t italy2026")
        assert err is None
        assert filters == {"tag": "italy2026,trip"}

    def test_tag_flag_invalid_value(self):
        _filters, err = _parse_search_args("-t !!!")
        assert err is not None
        assert "Invalid tag" in err

    def test_tag_with_text_and_other_filters(self):
        filters, err = _parse_search_args("dinner -c dining -t trip")
        assert err is None
        assert filters == {"text": "dinner", "category": "dining", "tag": "trip"}


# ---------------------------------------------------------------------------
# Filter / match tests
# ---------------------------------------------------------------------------


class TestMatchTransaction:
    def test_text_match_description(self):
        tx = _make_tx(description="Morning Coffee")
        assert _match_transaction(tx, {"text": "coffee"}) is True

    def test_text_match_category(self):
        tx = _make_tx(category="groceries")
        assert _match_transaction(tx, {"text": "groceries"}) is True

    def test_text_match_category_display(self):
        tx = _make_tx(category_display="Кафе та ресторани")
        assert _match_transaction(tx, {"text": "кафе"}) is True

    def test_text_match_source_account(self):
        tx = _make_tx(source_account="bank_uah_1")
        assert _match_transaction(tx, {"text": "bank_uah_1"}) is True

    def test_text_no_match(self):
        tx = _make_tx(description="Pizza")
        assert _match_transaction(tx, {"text": "coffee"}) is False

    def test_text_case_insensitive(self):
        tx = _make_tx(description="COFFEE")
        assert _match_transaction(tx, {"text": "coffee"}) is True

    def test_category_match(self):
        tx = _make_tx(category="groceries")
        assert _match_transaction(tx, {"category": "groceries"}) is True

    def test_category_no_match(self):
        tx = _make_tx(category="dining")
        assert _match_transaction(tx, {"category": "groceries"}) is False

    def test_account_match(self):
        tx = _make_tx(source_account="bank_uah_1")
        assert _match_transaction(tx, {"account": "bank_uah_1"}) is True

    def test_account_no_match(self):
        tx = _make_tx(source_account="bank_usd_1")
        assert _match_transaction(tx, {"account": "bank_uah_1"}) is False

    def test_date_month_match(self):
        tx = _make_tx(date="2026-03-15")
        assert _match_transaction(tx, {"date": "2026-03"}) is True

    def test_date_day_match(self):
        tx = _make_tx(date="2026-03-15")
        assert _match_transaction(tx, {"date": "2026-03-15"}) is True

    def test_date_no_match(self):
        tx = _make_tx(date="2026-04-10")
        assert _match_transaction(tx, {"date": "2026-03"}) is False

    def test_combined_all_match(self):
        tx = _make_tx(description="Coffee", category="dining", source_account="bank_uah_1", date="2026-03-15")
        filters = {"text": "coffee", "category": "dining", "account": "bank_uah_1", "date": "2026-03"}
        assert _match_transaction(tx, filters) is True

    def test_combined_one_fails(self):
        tx = _make_tx(description="Coffee", category="dining", date="2026-04-10")
        assert _match_transaction(tx, {"text": "coffee", "category": "dining", "date": "2026-03"}) is False

    def test_no_filters_matches_all(self):
        tx = _make_tx()
        assert _match_transaction(tx, {}) is True

    def test_tag_match(self):
        tx = _make_tx(tags=["work", "trip"])
        assert _match_transaction(tx, {"tag": "work"}) is True

    def test_tag_no_match(self):
        tx = _make_tx(tags=["work"])
        assert _match_transaction(tx, {"tag": "trip"}) is False

    def test_tag_match_untagged_tx(self):
        tx = _make_tx(tags=[])
        assert _match_transaction(tx, {"tag": "work"}) is False

    def test_tag_or_semantics(self):
        # Filter "italy2026,trip" matches if tx has either tag (OR).
        tx = _make_tx(tags=["trip"])
        assert _match_transaction(tx, {"tag": "italy2026,trip"}) is True

        tx2 = _make_tx(tags=["italy2026"])
        assert _match_transaction(tx2, {"tag": "italy2026,trip"}) is True

        tx3 = _make_tx(tags=["other"])
        assert _match_transaction(tx3, {"tag": "italy2026,trip"}) is False


# ---------------------------------------------------------------------------
# Formatter tests
# ---------------------------------------------------------------------------


class TestFormatSearchResults:
    def test_no_results(self):
        result = format_search_results([], 0, {"text": "coffee"})
        assert "No matching transactions" in result
        assert '"coffee"' in result

    def test_results_within_limit(self):
        txs = [_make_tx(description=f"Item {i}") for i in range(3)]
        result = format_search_results(txs, 3, {"text": "item"})
        assert "(3 results)" in result
        assert "showing" not in result

    def test_results_over_limit(self):
        txs = [_make_tx(description=f"Item {i}") for i in range(5)]
        result = format_search_results(txs, 25, {"category": "dining"})
        assert "(25 results, showing 5)" in result

    def test_multiple_filters_in_header(self):
        result = format_search_results([], 0, {"text": "x", "category": "dining", "date": "2026-03"})
        assert '"x"' in result
        assert "category: dining" in result
        assert "date: 2026-03" in result

    def test_tag_filter_in_header(self):
        result = format_search_results([], 0, {"tag": "italy2026,trip"})
        assert "tags: #italy2026 #trip" in result

    def test_tagged_tx_renders_tag_line(self):
        tx = _make_tx(description="Lunch", tags=["work", "italy2026"])
        result = format_search_results([tx], 1, {"tag": "work"})
        assert "#italy2026" in result
        assert "#work" in result

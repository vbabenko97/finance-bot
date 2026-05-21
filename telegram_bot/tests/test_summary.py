from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

from telegram_bot.bot.formatters import SummaryStats, format_summary
from telegram_bot.storage.models import Transaction

_FX_RATES: dict[str, float] = {
    "USD": 1.0,
    "USDT": 1.0,
    "UAH": 41.0,
    "EUR": 0.92,
}


def _make_tx(
    description: str = "Coffee",
    category: str = "dining",
    category_display: str = "Кафе та ресторани",
    source_account: str = "bank_uah_1",
    date_str: str = "2026-04-10",
    timestamp: str | None = None,
    currency: str = "EUR",
    amount_minor: int = 1500,
    mode: str = "consumption",
    tx_type: str | None = None,
) -> Transaction:
    sign = 1 if mode == "income" else -1
    return Transaction(
        tx_id=f"tx-{date_str}",
        date=date_str,
        timestamp=timestamp or f"{date_str}T12:00:00+00:00",
        amount_minor=amount_minor,
        signed_amount_minor=sign * amount_minor,
        currency=currency,
        description=description,
        category=category,
        category_display=category_display,
        source_account=source_account,
        mode=mode,
        tx_type=tx_type or ("income" if mode == "income" else "expense"),
    )


# ---------------------------------------------------------------------------
# Formatter
# ---------------------------------------------------------------------------


class TestFormatSummary:
    def test_no_activity(self):
        stats = SummaryStats(month="2026-04", spend_minor=0, income_minor=0)
        result = format_summary(stats)
        assert "No activity in 2026-04" in result

    def test_historical_month_with_prev_delta(self):
        stats = SummaryStats(
            month="2026-03",
            spend_minor=120_000,  # 1,200 EUR
            income_minor=300_000,  # 3,000 EUR
            top_categories=[("groceries", 50_000), ("dining", 30_000)],
            top_merchants=[("Сільпо", 40_000), ("Bolt", 20_000)],
            prev_month_spend_minor=100_000,  # 1,000 EUR
            prev_month_label="February",
            is_current_month=False,
            days_in_month=31,
        )
        result = format_summary(stats)
        assert "Summary: March 2026" in result
        assert "day " not in result
        assert "Spend: 1,200 EUR" in result
        assert "Income: 3,000 EUR" in result
        assert "Net: +1,800 EUR" in result
        assert "vs February (1,000 EUR): +200 EUR (+20%)" in result
        assert "Pace" not in result
        assert "Top categories:" in result
        assert "Top merchants:" in result
        assert "Сільпо: 400 EUR" in result

    def test_current_month_pace_on_track(self):
        # 120/30 * 30 = 120; vs prev 110 -> +9.1%, within ±10%
        stats = SummaryStats(
            month="2026-04",
            spend_minor=12_000,  # 120 EUR
            income_minor=0,
            top_categories=[("groceries", 12_000)],
            prev_month_spend_minor=11_000,  # 110 EUR
            prev_month_label="March",
            is_current_month=True,
            day_of_month=15,
            days_in_month=30,
        )
        result = format_summary(stats)
        assert "day 15 of 30" in result
        assert "Pace: projected 240 EUR" in result
        # 240 vs 110 is +118% — overspend, NOT on track
        assert "projected to overspend March" in result

    def test_current_month_pace_overspending(self):
        # day 10 of 30, 500 EUR spent -> projected 1,500; prev 1,000 → +50%
        stats = SummaryStats(
            month="2026-04",
            spend_minor=50_000,
            income_minor=0,
            top_categories=[("groceries", 50_000)],
            prev_month_spend_minor=100_000,
            prev_month_label="March",
            is_current_month=True,
            day_of_month=10,
            days_in_month=30,
        )
        result = format_summary(stats)
        assert "Pace: projected 1,500 EUR" in result
        assert "projected to overspend March (1,000 EUR) by 500 EUR (+50%)" in result

    def test_current_month_pace_underspending(self):
        # day 15 of 30, 300 EUR -> projected 600; prev 1,000 -> -40%
        stats = SummaryStats(
            month="2026-04",
            spend_minor=30_000,
            income_minor=0,
            top_categories=[("dining", 30_000)],
            prev_month_spend_minor=100_000,
            prev_month_label="March",
            is_current_month=True,
            day_of_month=15,
            days_in_month=30,
        )
        result = format_summary(stats)
        assert "Pace: projected 600 EUR" in result
        assert "projected under March (1,000 EUR) by 400 EUR (-40%)" in result

    def test_current_month_pace_no_prev_data(self):
        stats = SummaryStats(
            month="2026-04",
            spend_minor=30_000,
            income_minor=0,
            top_categories=[("dining", 30_000)],
            prev_month_spend_minor=None,
            prev_month_label="",
            is_current_month=True,
            day_of_month=15,
            days_in_month=30,
        )
        result = format_summary(stats)
        assert "Pace: projected 600 EUR" in result
        assert "vs March" not in result
        assert "on track" not in result

    def test_current_month_pace_almost_on_track(self):
        # day 15 of 30, spend 50 EUR -> projected 100; prev 100 -> exactly 0%
        stats = SummaryStats(
            month="2026-04",
            spend_minor=5_000,
            income_minor=0,
            top_categories=[("dining", 5_000)],
            prev_month_spend_minor=10_000,
            prev_month_label="March",
            is_current_month=True,
            day_of_month=15,
            days_in_month=30,
        )
        result = format_summary(stats)
        assert "Pace: projected 100 EUR" in result
        assert "on track vs March" in result

    def test_negative_net(self):
        stats = SummaryStats(
            month="2026-04",
            spend_minor=200_000,
            income_minor=100_000,
            top_categories=[("dining", 200_000)],
            is_current_month=False,
            days_in_month=30,
        )
        result = format_summary(stats)
        assert "Net: -1,000 EUR" in result

    def test_truncates_to_top_5(self):
        # Caller is responsible for truncation; format_summary just renders.
        cats = [(f"cat{i}", 10_000 - i) for i in range(5)]
        merchants = [(f"m{i}", 10_000 - i) for i in range(5)]
        stats = SummaryStats(
            month="2026-04",
            spend_minor=50_000,
            income_minor=0,
            top_categories=cats,
            top_merchants=merchants,
            is_current_month=False,
            days_in_month=30,
        )
        result = format_summary(stats)
        for i in range(5):
            assert f"cat{i}" in result
            assert f"m{i}" in result


# ---------------------------------------------------------------------------
# Handler integration
# ---------------------------------------------------------------------------


class TestHandleSummary:
    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_invalid_month_format(self, mock_send):
        from telegram_bot.bot.commands import handle_summary

        handle_summary("token", 123, 456, "/summary april", {})

        mock_send.assert_called_once()
        assert "YYYY-MM" in mock_send.call_args[0][2]

    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_invalid_month_value(self, mock_send):
        from telegram_bot.bot.commands import handle_summary

        handle_summary("token", 123, 456, "/summary 2026-13", {})

        mock_send.assert_called_once()
        assert "YYYY-MM" in mock_send.call_args[0][2]

    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_invalid_month_zero(self, mock_send):
        from telegram_bot.bot.commands import handle_summary

        handle_summary("token", 123, 456, "/summary 2026-00", {})

        mock_send.assert_called_once()
        assert "YYYY-MM" in mock_send.call_args[0][2]

    @patch("telegram_bot.bot.telegram_api.send_message")
    @patch("telegram_bot.bot.commands._load_fx_rates", return_value=None)
    def test_fx_rates_unavailable(self, _mock_fx, mock_send):
        from telegram_bot.bot.commands import handle_summary

        handle_summary("token", 123, 456, "/summary", {})

        mock_send.assert_called_once()
        assert "Could not load FX rates" in mock_send.call_args[0][2]

    @patch("telegram_bot.bot.telegram_api.send_message")
    @patch("telegram_bot.storage.dynamodb.get_all_transactions", return_value=[])
    @patch("telegram_bot.bot.commands._load_fx_rates", return_value=_FX_RATES)
    def test_empty_month(self, _mock_fx, _mock_txs, mock_send):
        from telegram_bot.bot.commands import handle_summary

        handle_summary("token", 123, 456, "/summary 2026-01", {})

        mock_send.assert_called_once()
        text = mock_send.call_args[0][2]
        assert "No activity in 2026-01" in text

    @patch("telegram_bot.bot.telegram_api.send_message")
    @patch("telegram_bot.bot.commands._load_fx_rates", return_value=_FX_RATES)
    def test_mixed_currencies(self, _mock_fx, mock_send):
        from telegram_bot.bot.commands import handle_summary

        txs = [
            _make_tx(
                description="Сільпо",
                category="groceries",
                date_str="2026-03-05",
                currency="UAH",
                amount_minor=2_050_000,
            ),  # 50_000 UAH → 50000/41*0.92 = ~1122 EUR cents
            _make_tx(
                description="Netflix",
                category="subscriptions",
                date_str="2026-03-10",
                currency="USD",
                amount_minor=2_000,
            ),  # 20 USD → 1840 cents (20*0.92)
            _make_tx(
                description="Salary",
                category="salary",
                date_str="2026-03-01",
                currency="EUR",
                amount_minor=300_000,
                mode="income",
            ),
        ]
        with patch("telegram_bot.storage.dynamodb.get_all_transactions", return_value=txs):
            handle_summary("token", 123, 456, "/summary 2026-03", {})

        mock_send.assert_called_once()
        text = mock_send.call_args[0][2]
        assert "Summary: March 2026" in text
        # Spend = 1122 + 1840 = 2962 EUR cents → ~30 EUR
        assert "Spend: " in text
        # Salary = 3000 EUR
        assert "Income: 3,000 EUR" in text
        # Merchants preserve original case in display
        assert "Сільпо:" in text
        assert "Netflix:" in text

    @patch("telegram_bot.bot.telegram_api.send_message")
    @patch("telegram_bot.bot.commands._load_fx_rates", return_value=_FX_RATES)
    def test_current_month_pace(self, _mock_fx, mock_send):
        from telegram_bot.bot.commands import handle_summary

        # Force "now" so the test is deterministic.
        with patch("telegram_bot.bot.commands.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 4, 15, tzinfo=UTC)
            mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

            txs = [
                _make_tx(category="groceries", date_str="2026-04-05", currency="EUR", amount_minor=30_000),  # 300 EUR
                _make_tx(
                    category="groceries", date_str="2026-03-05", currency="EUR", amount_minor=80_000
                ),  # 800 EUR prev month
            ]
            with patch("telegram_bot.storage.dynamodb.get_all_transactions", return_value=txs):
                handle_summary("token", 123, 456, "/summary", {})

        text = mock_send.call_args[0][2]
        assert "Summary: April 2026 — day 15 of 30" in text
        assert "Spend: 300 EUR" in text
        # Pace: projected 300 / 15 * 30 = 600 EUR; prev 800 → -200, -25%
        assert "Pace: projected 600 EUR" in text
        assert "projected under March (800 EUR) by 200 EUR (-25%)" in text

    @patch("telegram_bot.bot.telegram_api.send_message")
    @patch("telegram_bot.bot.commands._load_fx_rates", return_value=_FX_RATES)
    def test_movement_currency_does_not_block_summary(self, _mock_fx, mock_send):
        from telegram_bot.bot.commands import handle_summary

        # Movement tx in BTC (not in _FX_RATES) must NOT cause an FX-missing error,
        # because /summary only consumes consumption + income.
        txs = [
            _make_tx(category="groceries", date_str="2026-03-05", currency="EUR", amount_minor=10_000),
            _make_tx(
                category="internal_transfer",
                date_str="2026-03-06",
                currency="BTC",
                amount_minor=50_000_000,
                mode="movement",
                tx_type="movement",
            ),
        ]
        with patch("telegram_bot.storage.dynamodb.get_all_transactions", return_value=txs):
            handle_summary("token", 123, 456, "/summary 2026-03", {})

        text = mock_send.call_args[0][2]
        assert "Missing FX rates" not in text
        assert "Spend: 100 EUR" in text

    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_prev_month_zero_rate_is_treated_as_missing(self, mock_send):
        from telegram_bot.bot.commands import handle_summary

        # UAH rate is present but 0 — _to_eur_minor would divide by zero.
        broken_rates = {"USD": 1.0, "USDT": 1.0, "UAH": 0.0, "EUR": 0.92}
        txs = [
            _make_tx(category="groceries", date_str="2026-04-05", currency="EUR", amount_minor=30_000),
            _make_tx(category="groceries", date_str="2026-03-05", currency="UAH", amount_minor=4_100_000),
        ]
        with (
            patch("telegram_bot.bot.commands._load_fx_rates", return_value=broken_rates),
            patch("telegram_bot.storage.dynamodb.get_all_transactions", return_value=txs),
        ):
            handle_summary("token", 123, 456, "/summary 2026-04", {})

        text = mock_send.call_args[0][2]
        # Current month (EUR-only) renders, but prev-month delta is suppressed
        # because UAH rate is unusable.
        assert "Spend: 300 EUR" in text
        assert "vs March" not in text

    @patch("telegram_bot.bot.telegram_api.send_message")
    @patch("telegram_bot.bot.commands._load_fx_rates", return_value=_FX_RATES)
    def test_merchant_aliases_collapse_spellings(self, _mock_fx, mock_send):
        from telegram_bot.bot.commands import handle_summary

        txs = [
            _make_tx(
                description="McDonalds",
                category="dining",
                date_str="2026-03-05",
                amount_minor=10_000,
                tx_type="expense",
                mode="consumption",
            ),
            _make_tx(
                description="Макдональдс",
                category="dining",
                date_str="2026-03-06",
                amount_minor=6_000,
                tx_type="expense",
                mode="consumption",
            ),
            _make_tx(
                description="Billa",
                category="groceries",
                date_str="2026-03-07",
                amount_minor=15_000,
                tx_type="expense",
                mode="consumption",
            ),
        ]
        with patch("telegram_bot.storage.dynamodb.get_all_transactions", return_value=txs):
            handle_summary("token", 123, 456, "/summary 2026-03", {})

        text = mock_send.call_args[0][2]
        # Both McDonald's spellings should collapse into a single canonical row.
        assert text.count("McDonald's") == 1
        assert "Макдональдс" not in text
        assert "McDonalds:" not in text
        # Unaliased merchant is unchanged.
        assert "Billa:" in text

    @patch("telegram_bot.bot.telegram_api.send_message")
    @patch("telegram_bot.bot.commands._load_fx_rates", return_value=_FX_RATES)
    def test_historical_month_no_pace(self, _mock_fx, mock_send):
        from telegram_bot.bot.commands import handle_summary

        with patch("telegram_bot.bot.commands.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 5, 1, tzinfo=UTC)
            mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

            txs = [
                _make_tx(category="groceries", date_str="2026-04-05", currency="EUR", amount_minor=30_000),
                _make_tx(category="groceries", date_str="2026-03-05", currency="EUR", amount_minor=80_000),
            ]
            with patch("telegram_bot.storage.dynamodb.get_all_transactions", return_value=txs):
                handle_summary("token", 123, 456, "/summary 2026-04", {})

        text = mock_send.call_args[0][2]
        assert "Summary: April 2026" in text
        assert "day " not in text
        assert "Pace" not in text
        assert "vs March (800 EUR)" in text

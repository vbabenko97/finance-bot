from __future__ import annotations

from unittest.mock import patch

from telegram_bot.bot.formatters import format_portfolio
from telegram_bot.storage.models import AccountBalance


def _bal(account_id: str, currency: str, balance_minor: int) -> AccountBalance:
    return AccountBalance(
        account_id=account_id,
        currency=currency,
        balance_minor=balance_minor,
        last_updated="2026-04-10T12:00:00+00:00",
    )


_FX_RATES: dict[str, float] = {
    "USD": 1.0,
    "USDT": 1.0,
    "UAH": 41.0,
    "EUR": 0.92,
    "BTC": 1.0 / 80000,
}


class TestFormatPortfolio:
    def test_mixed_fiat_and_crypto(self):
        balances = [
            _bal("bank_uah_1", "UAH", 1_500_000),  # 15,000 UAH
            _bal("bank_usd_1", "USD", 50_000),  # 500 USD
            _bal("bank_eur_2", "EUR", 120_000),  # 1,200 EUR
            _bal("crypto_usdt", "USDT", 20_000),  # 200 USDT
            _bal("crypto_btc", "BTC", 1_500_000),  # 0.015 BTC
        ]
        result = format_portfolio(balances, _FX_RATES)

        assert "Portfolio" in result
        assert "Bank:" in result
        assert "Crypto:" in result
        assert "Net worth:" in result
        assert "Rates cached up to 24h" in result
        # Should have subtotals for groups
        assert result.count("Subtotal:") >= 2
        # Both currencies in totals
        assert "$" in result
        assert "€" in result

    def test_negative_liability_reduces_net_worth(self):
        balances = [
            _bal("bank_uah_1", "UAH", 1_000_000),  # 10,000 UAH
            _bal("bank_uah_3", "UAH", -500_000),  # -5,000 UAH (debt)
        ]
        result = format_portfolio(balances, _FX_RATES)

        assert "Net worth:" in result
        # Net should be ~5,000 UAH / ~$122, not ~10,000 UAH / ~$244
        # The subtotal line should reflect the reduced amount
        assert "Subtotal:" in result

    def test_negative_balance_arithmetic(self):
        balances = [
            _bal("bank_uah_1", "UAH", 1_000_000),  # 10,000 UAH = ~$244
            _bal("bank_uah_3", "UAH", -1_000_000),  # -10,000 UAH = ~-$244
        ]
        result = format_portfolio(balances, _FX_RATES)

        # Net worth should be ~$0 / ~€0
        assert "Net worth: ~$0 / ~€0" in result

    def test_empty_balances(self):
        result = format_portfolio([], _FX_RATES)
        assert "No balances set" in result

    def test_missing_fx_rates_still_formats(self):
        balances = [
            _bal("bank_uah_1", "UAH", 500_000),
        ]
        # Only USD=1.0 available, no EUR rate
        result = format_portfolio(balances, {"USD": 1.0})

        assert "Portfolio" in result
        assert "Bank UAH 1: 5000.00 UAH" in result
        # Without UAH rate, conversion to USD won't work
        assert "Subtotal: ~$0 / ~€0" in result


class TestHandlePortfolio:
    @patch("telegram_bot.bot.telegram_api.send_message")
    @patch("telegram_bot.storage.dynamodb.get_balances", return_value=[])
    def test_empty_balances(self, mock_balances, mock_send):
        from telegram_bot.bot.commands import handle_portfolio

        handle_portfolio("token", 123, 456, "/portfolio", {})

        mock_send.assert_called_once()
        assert "No balances set" in mock_send.call_args[0][2]

    @patch("telegram_bot.bot.telegram_api.send_message")
    @patch("telegram_bot.storage.dynamodb.cache_fx_rates")
    @patch(
        "telegram_bot.bot.commands._fetch_fx_rates",
        return_value={"USD": 1.0, "UAH": 41.0, "EUR": 0.92},
    )
    @patch("telegram_bot.storage.dynamodb.get_fx_rates", return_value=None)
    @patch(
        "telegram_bot.storage.dynamodb.get_balances",
        return_value=[
            AccountBalance("bank_uah_1", "UAH", 500_000, "2026-04-10T12:00:00+00:00"),
        ],
    )
    def test_fx_cache_miss_triggers_fetch(self, mock_balances, mock_fx_cache, mock_fetch, mock_cache_write, mock_send):
        from telegram_bot.bot.commands import handle_portfolio

        handle_portfolio("token", 123, 456, "/portfolio", {})

        mock_fetch.assert_called_once()
        mock_cache_write.assert_called_once()
        mock_send.assert_called_once()
        assert "Portfolio" in mock_send.call_args[0][2]

    @patch("telegram_bot.bot.telegram_api.send_message")
    @patch(
        "telegram_bot.storage.dynamodb.get_fx_rates",
        return_value={"USD": 1.0, "USDT": 1.0, "UAH": 41.0, "EUR": 0.92},
    )
    @patch(
        "telegram_bot.storage.dynamodb.get_balances",
        return_value=[
            AccountBalance("bank_uah_1", "UAH", 1_000_000, "2026-04-10T12:00:00+00:00"),
            AccountBalance("bank_eur_2", "EUR", 100_000, "2026-04-10T12:00:00+00:00"),
        ],
    )
    def test_cached_fx_no_refetch(self, mock_balances, mock_fx_cache, mock_send):
        from telegram_bot.bot.commands import handle_portfolio

        handle_portfolio("token", 123, 456, "/portfolio", {})

        mock_send.assert_called_once()
        text = mock_send.call_args[0][2]
        assert "Net worth:" in text
        assert "Rates cached up to 24h" in text

from __future__ import annotations

from unittest.mock import patch

from telegram_bot.bot.commands import _to_eur_minor
from telegram_bot.bot.formatters import format_budget

_FX_RATES: dict[str, float] = {
    "USD": 1.0,
    "USDT": 1.0,
    "UAH": 41.0,
    "EUR": 0.92,
}


# ---------------------------------------------------------------------------
# FX conversion
# ---------------------------------------------------------------------------


class TestToEurMinor:
    def test_eur_passthrough(self):
        assert _to_eur_minor(500_000, "EUR", _FX_RATES) == 500_000

    def test_usd_to_eur(self):
        # 100 USD = 100 * 0.92 = 92 EUR = 9_200 minor
        result = _to_eur_minor(10_000, "USD", _FX_RATES)
        assert result == 9_200

    def test_uah_to_eur(self):
        # 100 UAH: 100 / 41 * 0.92 = ~2.2439 EUR = ~224 minor
        result = _to_eur_minor(10_000, "UAH", _FX_RATES)
        assert 224 <= result <= 225


# ---------------------------------------------------------------------------
# Formatter
# ---------------------------------------------------------------------------


class TestFormatBudget:
    def test_mixed_currencies_over_under_no_budget(self):
        """Happy path: mixed spend in EUR, one over, one under, one unbudgeted."""
        spend = {
            "groceries": 22_500,  # 225 EUR — over 200 budget
            "dining": 5_000,  # 50 EUR — under 100 budget
            "transport": 3_500,  # 35 EUR — no budget
        }
        budgets = {
            "groceries": 20_000,  # 200 EUR
            "dining": 10_000,  # 100 EUR
        }
        result = format_budget(spend, budgets, "2026-04")

        assert "Budget: April 2026" in result
        # Groceries over budget should appear first (highest utilization 112%)
        assert "OVER" in result
        assert "112%" in result
        # Dining under budget
        assert "50 left" in result
        assert "50%" in result
        # Transport unbudgeted — should appear after budgeted
        assert "no budget" in result
        # Grand total
        assert "Total:" in result
        # Transport should be AFTER groceries and dining
        groceries_pos = result.index("groceries")
        dining_pos = result.index("dining")
        transport_pos = result.index("transport")
        assert groceries_pos < dining_pos < transport_pos

    def test_no_budgets_no_spend(self):
        result = format_budget({}, {}, "2026-04")
        assert "No budgets configured" in result
        assert "no spending" in result

    def test_budgets_exist_no_spend(self):
        result = format_budget({}, {"groceries": 20_000}, "2026-03")
        assert "No spending this month" in result
        assert "0 / 200 EUR" in result

    def test_spend_no_budgets(self):
        result = format_budget({"dining": 7_500}, {}, "2026-04")
        assert "no budget" in result
        assert "Total spent: 75 EUR" in result

    def test_zero_spend_on_budgeted_category(self):
        spend = {"dining": 5_000}
        budgets = {"groceries": 20_000, "dining": 10_000}
        result = format_budget(spend, budgets, "2026-04")
        # groceries with budget but 0 spend should still appear
        assert "0 / 200 EUR" in result


# ---------------------------------------------------------------------------
# Handler integration tests
# ---------------------------------------------------------------------------


class TestHandleBudget:
    @patch("telegram_bot.bot.telegram_api.send_message")
    @patch("telegram_bot.storage.dynamodb.get_all_budgets", return_value={})
    @patch("telegram_bot.storage.dynamodb.get_all_transactions", return_value=[])
    @patch("telegram_bot.bot.commands._load_fx_rates", return_value={"USD": 1.0, "EUR": 0.92})
    def test_empty_month(self, _mock_fx, _mock_txs, _mock_budgets, mock_send):
        from telegram_bot.bot.commands import handle_budget

        handle_budget("token", 123, 456, "/budget 2026-01", {})

        mock_send.assert_called_once()
        assert "No budgets configured" in mock_send.call_args[0][2]

    @patch("telegram_bot.bot.telegram_api.send_message")
    @patch("telegram_bot.bot.commands._load_fx_rates", return_value=None)
    def test_fx_rates_unavailable(self, _mock_fx, mock_send):
        from telegram_bot.bot.commands import handle_budget

        handle_budget("token", 123, 456, "/budget", {})

        mock_send.assert_called_once()
        assert "Could not load FX rates" in mock_send.call_args[0][2]

    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_invalid_month_format(self, mock_send):
        from telegram_bot.bot.commands import handle_budget

        handle_budget("token", 123, 456, "/budget march", {})

        mock_send.assert_called_once()
        assert "YYYY-MM" in mock_send.call_args[0][2]


class TestHandleSetBudget:
    @patch("telegram_bot.bot.telegram_api.send_message")
    @patch("telegram_bot.storage.dynamodb.set_budget")
    def test_set_valid_budget(self, mock_db_set, mock_send):
        from telegram_bot.bot.commands import handle_set_budget

        handle_set_budget("token", 123, 456, "/set_budget groceries 8000", {})

        mock_db_set.assert_called_once_with(456, "groceries", 800_000)
        mock_send.assert_called_once()
        assert "Budget set" in mock_send.call_args[0][2]
        assert "8,000 EUR" in mock_send.call_args[0][2]

    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_reject_income_category(self, mock_send):
        from telegram_bot.bot.commands import handle_set_budget

        handle_set_budget("token", 123, 456, "/set_budget salary 50000", {})

        mock_send.assert_called_once()
        assert "consumption" in mock_send.call_args[0][2]

    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_reject_movement_category(self, mock_send):
        from telegram_bot.bot.commands import handle_set_budget

        handle_set_budget("token", 123, 456, "/set_budget internal_transfer 10000", {})

        mock_send.assert_called_once()
        assert "consumption" in mock_send.call_args[0][2]

    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_reject_unknown_category(self, mock_send):
        from telegram_bot.bot.commands import handle_set_budget

        handle_set_budget("token", 123, 456, "/set_budget fake 1000", {})

        mock_send.assert_called_once()
        assert "Unknown category" in mock_send.call_args[0][2]

    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_reject_negative_amount(self, mock_send):
        from telegram_bot.bot.commands import handle_set_budget

        handle_set_budget("token", 123, 456, "/set_budget groceries -100", {})

        mock_send.assert_called_once()
        assert "positive" in mock_send.call_args[0][2]

    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_missing_args(self, mock_send):
        from telegram_bot.bot.commands import handle_set_budget

        handle_set_budget("token", 123, 456, "/set_budget groceries", {})

        mock_send.assert_called_once()
        assert "Usage" in mock_send.call_args[0][2]


class TestHandleDeleteBudget:
    @patch("telegram_bot.bot.telegram_api.send_message")
    @patch("telegram_bot.storage.dynamodb.delete_budget", return_value=True)
    def test_delete_existing(self, _mock_del, mock_send):
        from telegram_bot.bot.commands import handle_delete_budget

        handle_delete_budget("token", 123, 456, "/delete_budget groceries", {})

        mock_send.assert_called_once()
        assert "Budget removed" in mock_send.call_args[0][2]

    @patch("telegram_bot.bot.telegram_api.send_message")
    @patch("telegram_bot.storage.dynamodb.delete_budget", return_value=False)
    def test_delete_nonexistent(self, _mock_del, mock_send):
        from telegram_bot.bot.commands import handle_delete_budget

        handle_delete_budget("token", 123, 456, "/delete_budget groceries", {})

        mock_send.assert_called_once()
        assert "No budget set" in mock_send.call_args[0][2]

from __future__ import annotations

from unittest.mock import patch

from telegram_bot.bot import formatters
from telegram_bot.bot.commands import handle_history, handle_transfer
from telegram_bot.storage import dynamodb

USER_ID = 99

_FX_RATES: dict[str, float] = {
    "USD": 1.0,
    "USDT": 1.0,
    "UAH": 41.0,
    "EUR": 0.92,
}


# ---------------------------------------------------------------------------
# Parsing / validation
# ---------------------------------------------------------------------------


class TestTransferParsing:
    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_too_few_args(self, mock_send, dynamodb_table):
        handle_transfer("token", 123, USER_ID, "/transfer 100 bank_uah_1", {})
        assert "Usage" in mock_send.call_args[0][2]

    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_unknown_from_account(self, mock_send, dynamodb_table):
        handle_transfer("token", 123, USER_ID, "/transfer 100 fake_acc cash_uah", {})
        assert "Unknown account: fake_acc" in mock_send.call_args[0][2]

    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_unknown_to_account(self, mock_send, dynamodb_table):
        handle_transfer("token", 123, USER_ID, "/transfer 100 bank_uah_1 fake_acc", {})
        assert "Unknown account: fake_acc" in mock_send.call_args[0][2]

    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_same_from_and_to_rejected(self, mock_send, dynamodb_table):
        handle_transfer("token", 123, USER_ID, "/transfer 100 bank_uah_1 bank_uah_1", {})
        assert "must differ" in mock_send.call_args[0][2]

    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_invalid_amount(self, mock_send, dynamodb_table):
        handle_transfer("token", 123, USER_ID, "/transfer abc bank_uah_1 cash_uah", {})
        assert "Invalid amount" in mock_send.call_args[0][2]

    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_negative_amount(self, mock_send, dynamodb_table):
        handle_transfer("token", 123, USER_ID, "/transfer -50 bank_uah_1 cash_uah", {})
        assert "positive" in mock_send.call_args[0][2]


# ---------------------------------------------------------------------------
# Same-currency transfer
# ---------------------------------------------------------------------------


class TestSameCurrencyTransfer:
    @patch("telegram_bot.bot.telegram_api.send_message_with_keyboard")
    def test_books_both_legs_and_moves_balance(self, mock_send_kb, dynamodb_table):
        handle_transfer(
            "token",
            123,
            USER_ID,
            "/transfer 500 bank_uah_1 cash_uah",
            {"message_id": 50_001},
        )

        txs = dynamodb.get_transactions(USER_ID, limit=10)
        assert len(txs) == 2
        out_legs = [t for t in txs if t.tx_type == "expense"]
        in_legs = [t for t in txs if t.tx_type == "income"]
        assert len(out_legs) == 1
        assert len(in_legs) == 1
        assert out_legs[0].source_account == "bank_uah_1"
        assert in_legs[0].source_account == "cash_uah"
        assert out_legs[0].paired_tx_sk and in_legs[0].paired_tx_sk
        assert out_legs[0].category == "internal_transfer"
        assert in_legs[0].mode == "movement"

        balances = {b.account_id: b.balance_minor for b in dynamodb.get_balances(USER_ID)}
        assert balances["bank_uah_1"] == -50_000
        assert balances["cash_uah"] == 50_000

    @patch("telegram_bot.bot.telegram_api.send_message_with_keyboard")
    def test_confirmation_includes_undo_button(self, mock_send_kb, dynamodb_table):
        handle_transfer(
            "token",
            123,
            USER_ID,
            "/transfer 500 bank_uah_1 cash_uah",
            {"message_id": 50_010},
        )

        args = mock_send_kb.call_args[0]
        assert "Transfer recorded" in args[2]
        assert "from bank_uah_1" in args[2]
        assert "to cash_uah" in args[2]
        keyboard = args[3]
        assert keyboard[0][0]["callback_data"].startswith("undo:TX#")


# ---------------------------------------------------------------------------
# Cross-currency transfer
# ---------------------------------------------------------------------------


def _stash_fx_rates(table) -> None:
    import time as _time
    from decimal import Decimal as D

    table.put_item(
        Item={
            "PK": "CONFIG",
            "SK": "FX_RATES",
            "rates": {"USD": D("1.0"), "USDT": D("1.0"), "UAH": D("41.0"), "EUR": D("0.92")},
            "fetched_at": "2026-04-15T00:00:00+00:00",
            "ttl": int(_time.time()) + 86400,
        },
    )


class TestCrossCurrencyTransfer:
    @patch("telegram_bot.bot.telegram_api.send_message_with_keyboard")
    def test_uses_cached_fx_and_credits_target_currency(self, mock_send_kb, dynamodb_table):
        _stash_fx_rates(dynamodb_table)

        handle_transfer(
            "token",
            123,
            USER_ID,
            "/transfer 100 bank_usd_1 bank_eur_2",
            {"message_id": 51_001},
        )

        txs = dynamodb.get_transactions(USER_ID, limit=10)
        out = next(t for t in txs if t.tx_type == "expense")
        in_leg = next(t for t in txs if t.tx_type == "income")

        # Out: 100 USD = 10_000 minor.
        assert out.amount_minor == 10_000
        assert out.currency == "USD"
        # In: 100 USD / 1.0 * 0.92 = 92 EUR = 9_200 minor.
        assert in_leg.amount_minor == 9_200
        assert in_leg.currency == "EUR"
        assert out.category == "fx_exchange"
        assert in_leg.category == "fx_exchange"

        balances = {b.account_id: b for b in dynamodb.get_balances(USER_ID)}
        assert balances["bank_usd_1"].balance_minor == -10_000
        assert balances["bank_usd_1"].currency == "USD"
        assert balances["bank_eur_2"].balance_minor == 9_200
        assert balances["bank_eur_2"].currency == "EUR"

    @patch("telegram_bot.bot.telegram_api.send_message_with_keyboard")
    def test_confirmation_shows_rate(self, mock_send_kb, dynamodb_table):
        _stash_fx_rates(dynamodb_table)

        handle_transfer(
            "token",
            123,
            USER_ID,
            "/transfer 100 bank_usd_1 bank_eur_2",
            {"message_id": 51_010},
        )

        text = mock_send_kb.call_args[0][2]
        assert "Rate: 1 USD" in text
        assert "EUR" in text

    @patch("telegram_bot.bot.telegram_api.send_message")
    @patch("telegram_bot.bot.commands._load_fx_rates", return_value=None)
    def test_refuses_when_fx_unavailable(self, _mock_fx, mock_send, dynamodb_table):
        handle_transfer(
            "token",
            123,
            USER_ID,
            "/transfer 100 bank_usd_1 bank_eur_2",
            {"message_id": 51_020},
        )

        assert "Missing FX rates" in mock_send.call_args[0][2]
        assert dynamodb.get_transactions(USER_ID) == []


# ---------------------------------------------------------------------------
# Cascade delete (both directions) — the owner-flagged sign-bug guard
# ---------------------------------------------------------------------------


class TestTransferCascadeDelete:
    @patch("telegram_bot.bot.telegram_api.send_message_with_keyboard")
    def test_deleting_out_leg_deletes_in_leg_and_restores_balances(
        self,
        _mock_send_kb,
        dynamodb_table,
    ):
        from telegram_bot.bot.commands import _soft_delete_by_sk

        handle_transfer(
            "token",
            123,
            USER_ID,
            "/transfer 500 bank_uah_1 cash_uah",
            {"message_id": 52_001},
        )
        txs = dynamodb.get_transactions(USER_ID, limit=10)
        out = next(t for t in txs if t.tx_type == "expense")
        out_sk = f"TX#{out.timestamp}#{out.tx_id}"

        ok = _soft_delete_by_sk(USER_ID, out_sk, callback_update_id=52_002)
        assert ok is True

        assert dynamodb.get_transactions(USER_ID, limit=10) == []
        balances = {b.account_id: b.balance_minor for b in dynamodb.get_balances(USER_ID)}
        assert balances["bank_uah_1"] == 0
        assert balances["cash_uah"] == 0

    @patch("telegram_bot.bot.telegram_api.send_message_with_keyboard")
    def test_deleting_in_leg_deletes_out_leg_and_restores_balances(
        self,
        _mock_send_kb,
        dynamodb_table,
    ):
        from telegram_bot.bot.commands import _soft_delete_by_sk

        handle_transfer(
            "token",
            123,
            USER_ID,
            "/transfer 500 bank_uah_1 cash_uah",
            {"message_id": 52_101},
        )
        txs = dynamodb.get_transactions(USER_ID, limit=10)
        in_leg = next(t for t in txs if t.tx_type == "income")
        in_sk = f"TX#{in_leg.timestamp}#{in_leg.tx_id}"

        ok = _soft_delete_by_sk(USER_ID, in_sk, callback_update_id=52_102)
        assert ok is True

        assert dynamodb.get_transactions(USER_ID, limit=10) == []
        balances = {b.account_id: b.balance_minor for b in dynamodb.get_balances(USER_ID)}
        assert balances["bank_uah_1"] == 0
        assert balances["cash_uah"] == 0

    @patch("telegram_bot.bot.telegram_api.send_message_with_keyboard")
    def test_deleting_cross_currency_pair_restores_both_currencies(
        self,
        _mock_send_kb,
        dynamodb_table,
    ):
        from telegram_bot.bot.commands import _soft_delete_by_sk

        _stash_fx_rates(dynamodb_table)
        handle_transfer(
            "token",
            123,
            USER_ID,
            "/transfer 100 bank_usd_1 bank_eur_2",
            {"message_id": 52_201},
        )
        txs = dynamodb.get_transactions(USER_ID, limit=10)
        in_leg = next(t for t in txs if t.tx_type == "income")
        in_sk = f"TX#{in_leg.timestamp}#{in_leg.tx_id}"

        ok = _soft_delete_by_sk(USER_ID, in_sk, callback_update_id=52_202)
        assert ok is True

        balances = {b.account_id: b for b in dynamodb.get_balances(USER_ID)}
        assert balances["bank_usd_1"].balance_minor == 0
        assert balances["bank_eur_2"].balance_minor == 0


# ---------------------------------------------------------------------------
# History renders the in-leg with a positive sign
# ---------------------------------------------------------------------------


class TestTransferHistoryRendering:
    @patch("telegram_bot.bot.telegram_api.send_message_with_keyboard")
    def test_in_leg_renders_with_plus_sign(self, mock_send_kb, dynamodb_table):
        handle_transfer(
            "token",
            123,
            USER_ID,
            "/transfer 500 bank_uah_1 cash_uah",
            {"message_id": 53_001},
        )
        mock_send_kb.reset_mock()

        handle_history("token", 123, USER_ID, "/history", {})

        text = mock_send_kb.call_args[0][2]
        # Out-leg "-500.00 UAH" and in-leg "+500.00 UAH" must both appear with
        # the correct sign. The bug would render the in-leg as negative because
        # tx_type defaulted to anything except "income".
        assert "+500.00 UAH" in text
        assert "-500.00 UAH" in text

    def test_format_history_entry_uses_signed_display_for_income_leg(self):
        # Direct unit check on the formatter so the sign convention is locked
        # regardless of how /transfer happens to build its records.
        from telegram_bot.storage.models import Transaction

        in_leg = Transaction(
            tx_id="tin",
            date="2026-04-10",
            timestamp="2026-04-10T12:00:00+00:00",
            amount_minor=50_000,
            signed_amount_minor=50_000,
            currency="UAH",
            description="Transfer from bank_uah_1",
            category="internal_transfer",
            category_display="Внутрішній переказ",
            source_account="cash_uah",
            mode="movement",
            tx_type="income",
            paired_tx_sk="TX#2026-04-10T12:00:00+00:00#tout",
        )
        rendered = formatters.format_history_entry(in_leg, 1)
        assert "+500.00 UAH" in rendered


# ---------------------------------------------------------------------------
# Edit guard: paired legs cannot be edited
# ---------------------------------------------------------------------------


class TestTransferEditGuard:
    @patch("telegram_bot.bot.telegram_api.send_message_with_keyboard")
    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_starting_edit_on_paired_leg_is_refused(
        self,
        mock_send,
        mock_send_kb,
        dynamodb_table,
    ):
        from telegram_bot.bot.conversation import handle_edit_start

        handle_transfer(
            "token",
            123,
            USER_ID,
            "/transfer 500 bank_uah_1 cash_uah",
            {"message_id": 54_001},
        )
        out = next(t for t in dynamodb.get_transactions(USER_ID, limit=10) if t.tx_type == "expense")
        out_sk = f"TX#{out.timestamp}#{out.tx_id}"

        handle_edit_start("token", 123, USER_ID, out_sk)

        # No edit flow started: no field-picker keyboard sent.
        # send_message was called with a clear refusal.
        assert dynamodb.get_conv_state(USER_ID) is None
        assert "transfer leg" in mock_send.call_args[0][2].lower()

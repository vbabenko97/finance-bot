from __future__ import annotations

from unittest.mock import patch

from telegram_bot.bot.conversation import _apply_edit, handle_edit_callback, handle_edit_start
from telegram_bot.storage import dynamodb
from telegram_bot.storage.models import Transaction

USER_ID = 99


def _make_tx(
    tx_id: str = "abc123",
    amount_minor: int = 10_000,
    signed_amount_minor: int = -10_000,
    currency: str = "EUR",
    description: str = "Lunch",
    category: str = "dining",
    category_display: str = "Кафе та ресторани",
    source_account: str = "bank_eur_2",
    mode: str = "consumption",
    tx_type: str = "expense",
    tags: list[str] | None = None,
) -> Transaction:
    return Transaction(
        tx_id=tx_id,
        date="2026-04-10",
        timestamp="2026-04-10T12:00:00+00:00",
        amount_minor=amount_minor,
        signed_amount_minor=signed_amount_minor,
        currency=currency,
        description=description,
        category=category,
        category_display=category_display,
        source_account=source_account,
        mode=mode,
        tx_type=tx_type,
        tags=tags or [],
    )


# ---------------------------------------------------------------------------
# _apply_edit
# ---------------------------------------------------------------------------


class TestApplyEdit:
    def test_amount_only(self):
        old = _make_tx(amount_minor=10_000, signed_amount_minor=-10_000)
        new = _apply_edit(old, {"new_amount_minor": 15_000})
        assert new.amount_minor == 15_000
        assert new.signed_amount_minor == -15_000
        assert new.source_account == old.source_account
        assert new.currency == old.currency

    def test_description_only(self):
        old = _make_tx(description="old")
        new = _apply_edit(old, {"new_description": "new"})
        assert new.description == "new"
        assert new.amount_minor == old.amount_minor

    def test_category_only(self):
        old = _make_tx(category="dining", category_display="Кафе та ресторани")
        new = _apply_edit(old, {"new_category": "groceries", "new_category_display": "Продукти"})
        assert new.category == "groceries"
        assert new.category_display == "Продукти"

    def test_account_same_currency(self):
        old = _make_tx(currency="EUR", source_account="bank_eur_2", amount_minor=10_000)
        new = _apply_edit(old, {"new_account": "bank_eur_1"})
        assert new.source_account == "bank_eur_1"
        assert new.currency == "EUR"
        assert new.amount_minor == 10_000  # unchanged

    def test_account_different_currency_preserves_numeric_amount(self):
        # 100 EUR -> 100 USD (same numeric value, factor preserved 100->100)
        old = _make_tx(currency="EUR", source_account="bank_eur_2", amount_minor=10_000, signed_amount_minor=-10_000)
        new = _apply_edit(old, {"new_account": "bank_usd_1"})
        assert new.source_account == "bank_usd_1"
        assert new.currency == "USD"
        assert new.amount_minor == 10_000

    def test_account_to_btc_rescales_minor(self):
        # 100 EUR (factor 100) -> 100 BTC (factor 100_000_000)
        old = _make_tx(currency="EUR", source_account="bank_eur_2", amount_minor=10_000, signed_amount_minor=-10_000)
        new = _apply_edit(old, {"new_account": "crypto_btc"})
        assert new.source_account == "crypto_btc"
        assert new.currency == "BTC"
        assert new.amount_minor == 100 * 100_000_000

    def test_tags_only(self):
        old = _make_tx(tags=["old_tag"])
        new = _apply_edit(old, {"new_tags": ["work", "italy2026"]})
        assert new.tags == ["italy2026", "work"]

    def test_tags_clear(self):
        old = _make_tx(tags=["one"])
        new = _apply_edit(old, {"new_tags": []})
        assert new.tags == []

    def test_income_preserves_sign(self):
        old = _make_tx(mode="income", tx_type="income", amount_minor=300_000, signed_amount_minor=300_000)
        new = _apply_edit(old, {"new_amount_minor": 400_000})
        assert new.signed_amount_minor == 400_000


# ---------------------------------------------------------------------------
# End-to-end edit flow
# ---------------------------------------------------------------------------


class TestEditFlow:
    @patch("telegram_bot.bot.telegram_api.send_message_with_keyboard")
    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_start_edit_loads_tx_and_shows_picker(self, mock_send, mock_send_kb, dynamodb_table):
        tx = _make_tx()
        dynamodb.add_transaction(USER_ID, tx, update_id=8001)

        tx_sk = f"TX#{tx.timestamp}#{tx.tx_id}"
        handle_edit_start("token", 123, USER_ID, tx_sk)

        state = dynamodb.get_conv_state(USER_ID)
        assert state is not None
        assert state.step == "EDIT_FIELD"
        assert state.data["edit_tx_sk"] == tx_sk

        mock_send_kb.assert_called_once()
        text = mock_send_kb.call_args[0][2]
        assert "Edit transaction" in text
        assert "Lunch" in text

    @patch("telegram_bot.bot.telegram_api.send_message_with_keyboard")
    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_start_edit_missing_tx_reports_error(self, mock_send, mock_send_kb, dynamodb_table):
        handle_edit_start("token", 123, USER_ID, "TX#2026-04-10T12:00:00+00:00#missing")

        assert dynamodb.get_conv_state(USER_ID) is None
        mock_send.assert_called_once()
        assert "not found" in mock_send.call_args[0][2].lower()

    @patch("telegram_bot.bot.telegram_api.answer_callback")
    @patch("telegram_bot.bot.telegram_api.edit_message")
    @patch("telegram_bot.bot.telegram_api.send_message_with_keyboard")
    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_full_amount_edit_flow(self, mock_send, mock_send_kb, mock_edit, mock_answer, dynamodb_table):
        tx = _make_tx(amount_minor=10_000, signed_amount_minor=-10_000)
        dynamodb.add_transaction(USER_ID, tx, update_id=8101)
        tx_sk = f"TX#{tx.timestamp}#{tx.tx_id}"

        # 1. Start edit from history button.
        handle_edit_callback(
            "token",
            123,
            USER_ID,
            "cbq-1",
            f"edit:{tx_sk}",
            {"message_id": 500},
        )
        state = dynamodb.get_conv_state(USER_ID)
        assert state is not None
        assert state.step == "EDIT_FIELD"

        # 2. Pick "amount".
        handle_edit_callback(
            "token",
            123,
            USER_ID,
            "cbq-2",
            "edit:field:amount",
            {"message_id": 501},
        )
        state = dynamodb.get_conv_state(USER_ID)
        assert state.step == "EDIT_AMOUNT"

        # 3. User types new amount.
        from telegram_bot.bot.conversation import handle_edit_message

        handle_edit_message("token", 123, USER_ID, "250", {})
        state = dynamodb.get_conv_state(USER_ID)
        assert state.step == "EDIT_CONFIRM"
        assert state.data["new_amount_minor"] == 25_000

        # 4. Confirm.
        handle_edit_callback(
            "token",
            123,
            USER_ID,
            "cbq-3",
            "edit:confirm",
            {"message_id": 502},
        )
        assert dynamodb.get_conv_state(USER_ID) is None

        fetched = dynamodb.get_transaction_by_key(USER_ID, tx.timestamp, tx.tx_id)
        assert fetched is not None
        assert fetched.amount_minor == 25_000

        bal = {b.account_id: b.balance_minor for b in dynamodb.get_balances(USER_ID)}
        assert bal["bank_eur_2"] == -25_000

    @patch("telegram_bot.bot.telegram_api.answer_callback")
    @patch("telegram_bot.bot.telegram_api.edit_message")
    @patch("telegram_bot.bot.telegram_api.send_message_with_keyboard")
    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_cancel_clears_state(self, mock_send, mock_send_kb, mock_edit, mock_answer, dynamodb_table):
        tx = _make_tx()
        dynamodb.add_transaction(USER_ID, tx, update_id=8201)
        tx_sk = f"TX#{tx.timestamp}#{tx.tx_id}"

        handle_edit_callback("token", 123, USER_ID, "c1", f"edit:{tx_sk}", {"message_id": 500})
        assert dynamodb.get_conv_state(USER_ID) is not None

        handle_edit_callback("token", 123, USER_ID, "c2", "edit:cancel", {"message_id": 500})
        assert dynamodb.get_conv_state(USER_ID) is None
        mock_edit.assert_any_call("token", 123, 500, "Edit cancelled.")

    @patch("telegram_bot.bot.telegram_api.answer_callback")
    @patch("telegram_bot.bot.telegram_api.edit_message")
    @patch("telegram_bot.bot.telegram_api.send_message_with_keyboard")
    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_clear_tags_button_clears_and_commits(
        self,
        mock_send,
        mock_send_kb,
        mock_edit,
        mock_answer,
        dynamodb_table,
    ):
        tx = _make_tx(tags=["work", "italy2026"])
        dynamodb.add_transaction(USER_ID, tx, update_id=8401)
        tx_sk = f"TX#{tx.timestamp}#{tx.tx_id}"

        # Start edit, pick Tags, then click Clear tags.
        handle_edit_callback("token", 123, USER_ID, "c1", f"edit:{tx_sk}", {"message_id": 600})
        handle_edit_callback("token", 123, USER_ID, "c2", "edit:field:tags", {"message_id": 600})
        handle_edit_callback("token", 123, USER_ID, "c3", "edit:tags:clear", {"message_id": 600})

        state = dynamodb.get_conv_state(USER_ID)
        assert state is not None
        assert state.step == "EDIT_CONFIRM"
        assert state.data["new_tags"] == []

        # Confirm and verify tx tags cleared in storage.
        handle_edit_callback("token", 123, USER_ID, "c4", "edit:confirm", {"message_id": 600})
        assert dynamodb.get_conv_state(USER_ID) is None

        fetched = dynamodb.get_transaction_by_key(USER_ID, tx.timestamp, tx.tx_id)
        assert fetched is not None
        assert fetched.tags == []

        raw = dynamodb_table.get_item(
            Key={"PK": f"USER#{USER_ID}", "SK": f"TX#{tx.timestamp}#{tx.tx_id}"},
        )["Item"]
        assert "tags" not in raw

    @patch("telegram_bot.bot.telegram_api.answer_callback")
    @patch("telegram_bot.bot.telegram_api.edit_message")
    @patch("telegram_bot.bot.telegram_api.send_message_with_keyboard")
    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_cross_currency_account_edit_preview_shows_conversion(
        self,
        mock_send,
        mock_send_kb,
        mock_edit,
        mock_answer,
        dynamodb_table,
    ):
        tx = _make_tx(currency="EUR", source_account="bank_eur_2", amount_minor=10_000, signed_amount_minor=-10_000)
        dynamodb.add_transaction(USER_ID, tx, update_id=8301)
        tx_sk = f"TX#{tx.timestamp}#{tx.tx_id}"

        handle_edit_callback("token", 123, USER_ID, "c1", f"edit:{tx_sk}", {"message_id": 500})
        handle_edit_callback("token", 123, USER_ID, "c2", "edit:field:account", {"message_id": 500})
        handle_edit_callback("token", 123, USER_ID, "c3", "edit:acc:bank_usd_1", {"message_id": 500})

        state = dynamodb.get_conv_state(USER_ID)
        assert state.step == "EDIT_CONFIRM"

        # Preview must show currency change and the new amount in the new currency.
        last_edit_call = mock_edit.call_args
        preview_text = last_edit_call[0][3]
        assert "EUR" in preview_text
        assert "USD" in preview_text
        assert "100.00 USD" in preview_text  # numeric amount preserved

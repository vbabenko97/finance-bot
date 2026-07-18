"""Plain entry handlers: income, quick-add, history, delete, edit, set_balance, cancel."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from telegram_bot.bot import conversation, formatters, telegram_api
from telegram_bot.bot.quick_add import QuickAddParseResult, parse_quick_add_detailed
from telegram_bot.config.accounts import ACCOUNTS
from telegram_bot.storage import dynamodb
from telegram_bot.storage.models import format_amount, to_minor

_QUICK_ADD_ERROR_MESSAGES: dict[str, str] = {
    "empty_input": "Enter an amount and description. Example: 150 Groceries",
    "invalid_amount": "Amount must come first. Example: 150 Groceries or 25 USD Netflix",
    "missing_description": "Add a description after the amount. Example: 150 Groceries",
    "unsupported_currency": "Currency must be one of: EUR, USD, UAH, USDT, USDC, BTC",
    "unknown_account": "Unknown account after @account. Use /start to see valid examples.",
    "invalid_format": "Could not parse. Try: 150 Groceries, 25 USD Netflix, or 100 coffee @bank_usd_1",
}


def _quick_add_error_message(result: QuickAddParseResult, tx_type: str) -> str:
    default = "Could not parse. Try: /income 5000 Salary" if tx_type == "income" else "Try: 150 Groceries"
    base = _QUICK_ADD_ERROR_MESSAGES.get(result.error_code or "", default)
    if tx_type == "income":
        if result.error_code == "missing_description":
            return "Add a description after the amount. Example: /income 5000 Salary"
        if result.error_code == "invalid_amount":
            return "Amount must come first. Example: /income 5000 Salary"
        return base if base != default else default
    return base


def handle_history(token: str, chat_id: int, user_id: int, text: str, message: dict[str, Any]) -> None:
    transactions = dynamodb.get_transactions(user_id, limit=10)
    if not transactions:
        telegram_api.send_message(token, chat_id, "No transactions yet.")
        return

    lines: list[str] = ["Recent transactions:"]
    buttons: list[list[dict[str, str]]] = []

    for i, tx in enumerate(transactions, 1):
        entry = formatters.format_history_entry(tx, i)
        lines.append(f"\n{entry}")
        tx_sk = f"TX#{tx.timestamp}#{tx.tx_id}"
        buttons.append(
            [
                {"text": f"Edit #{i}", "callback_data": f"edit:{tx_sk}"},
                {"text": f"Delete #{i}", "callback_data": f"del:{tx_sk}"},
            ]
        )

    telegram_api.send_message_with_keyboard(token, chat_id, "\n".join(lines), buttons)


def handle_delete(token: str, chat_id: int, user_id: int, text: str, message: dict[str, Any]) -> None:
    tx = dynamodb.get_last_transaction(user_id)
    if tx is None:
        telegram_api.send_message(token, chat_id, "No transactions to delete.")
        return

    tx_sk = f"TX#{tx.timestamp}#{tx.tx_id}"
    entry = formatters.format_confirmation(tx)
    keyboard = telegram_api.build_keyboard([("Confirm delete", f"confirm_del:{tx_sk}"), ("Cancel", "cancel_del")])
    telegram_api.send_message_with_keyboard(token, chat_id, f"Delete this?\n\n{entry}", keyboard)


def handle_edit(token: str, chat_id: int, user_id: int, text: str, message: dict[str, Any]) -> None:
    parts = text.strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        telegram_api.send_message(token, chat_id, "Usage: /edit &lt;n&gt; (1-10, from /history)")
        return

    n = int(parts[1])
    if n < 1 or n > 10:
        telegram_api.send_message(token, chat_id, "Index must be 1-10. Use /history to see recent transactions.")
        return

    transactions = dynamodb.get_transactions(user_id, limit=10)
    if n > len(transactions):
        telegram_api.send_message(token, chat_id, f"Only {len(transactions)} recent transactions.")
        return

    tx = transactions[n - 1]
    tx_sk = f"TX#{tx.timestamp}#{tx.tx_id}"
    conversation.handle_edit_start(token, chat_id, user_id, tx_sk)


def handle_set_balance(token: str, chat_id: int, user_id: int, text: str, message: dict[str, Any]) -> None:
    parts = text.strip().split()
    # Expected: /set_balance <account_id> <amount>
    if len(parts) != 3:
        telegram_api.send_message(token, chat_id, "Usage: /set_balance &lt;account_id&gt; &lt;amount&gt;")
        return

    account_id = parts[1]
    if account_id not in ACCOUNTS:
        valid = ", ".join(sorted(ACCOUNTS))
        telegram_api.send_message(token, chat_id, f"Unknown account. Valid accounts:\n{valid}")
        return

    try:
        amount = Decimal(parts[2].replace(",", "."))
    except InvalidOperation:
        telegram_api.send_message(token, chat_id, "Invalid amount.")
        return

    currency = ACCOUNTS[account_id][1]
    balance_minor = to_minor(amount, currency)
    dynamodb.set_balance(user_id, account_id, balance_minor, currency)

    display = format_amount(balance_minor, currency)
    telegram_api.send_message(token, chat_id, f"Balance set: {account_id} = {display}")


def handle_income(token: str, chat_id: int, user_id: int, text: str, message: dict[str, Any]) -> None:
    # Strip "/income " prefix
    after_cmd = text.strip()
    if after_cmd.lower().startswith("/income"):
        after_cmd = after_cmd[7:].strip()

    if not after_cmd:
        telegram_api.send_message(token, chat_id, "Usage: /income &lt;amount&gt; &lt;description&gt;")
        return

    result = parse_quick_add_detailed(after_cmd, tx_type="income")
    tx = result.transaction
    if tx is None:
        telegram_api.send_message(token, chat_id, _quick_add_error_message(result, "income"))
        return

    update_id = message.get("message_id") or message.get("update_id", 0)
    added = dynamodb.add_transaction(user_id, tx, int(update_id))
    if not added:
        telegram_api.send_message(token, chat_id, "Duplicate, already recorded.")
        return

    confirmation = formatters.format_confirmation(tx)
    tx_sk = f"TX#{tx.timestamp}#{tx.tx_id}"
    keyboard = telegram_api.build_keyboard([("Undo", f"undo:{tx_sk}")])
    telegram_api.send_message_with_keyboard(token, chat_id, confirmation, keyboard)


def handle_cancel(token: str, chat_id: int, user_id: int, text: str, message: dict[str, Any]) -> None:
    dynamodb.delete_conv_state(user_id)
    telegram_api.send_message(token, chat_id, "Cancelled.")


def handle_quick_add(token: str, chat_id: int, user_id: int, text: str, message: dict[str, Any]) -> None:
    result = parse_quick_add_detailed(text)
    tx = result.transaction
    if tx is None:
        telegram_api.send_message(token, chat_id, _quick_add_error_message(result, "expense"))
        return

    update_id = message.get("message_id") or message.get("update_id", 0)
    added = dynamodb.add_transaction(user_id, tx, int(update_id))
    if not added:
        telegram_api.send_message(token, chat_id, "Duplicate, already recorded.")
        return

    confirmation = formatters.format_confirmation(tx)
    tx_sk = f"TX#{tx.timestamp}#{tx.tx_id}"
    keyboard = telegram_api.build_keyboard([("Undo", f"undo:{tx_sk}")])
    telegram_api.send_message_with_keyboard(token, chat_id, confirmation, keyboard)

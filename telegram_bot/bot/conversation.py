from __future__ import annotations

import hashlib
import logging
import time
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from telegram_bot.bot import formatters, telegram_api
from telegram_bot.bot.quick_add import extract_tags, normalize_tag
from telegram_bot.config.accounts import ACCOUNTS
from telegram_bot.config.categories import CATEGORIES, get_categories_for_mode
from telegram_bot.storage import dynamodb
from telegram_bot.storage.models import ConversationState, Transaction, from_minor, to_minor

logger = logging.getLogger(__name__)

_TTL_SECONDS = 300

_CURRENCY_OPTIONS: list[tuple[str, str]] = [
    ("EUR", "add:currency:EUR"),
    ("USD", "add:currency:USD"),
    ("UAH", "add:currency:UAH"),
    ("USDT", "add:currency:USDT"),
    ("BTC", "add:currency:BTC"),
]


def _make_state(step: str, data: dict[str, object]) -> ConversationState:
    now = datetime.now(UTC).isoformat()
    ttl = int(time.time()) + _TTL_SECONDS
    return ConversationState(step=step, data=data, updated_at=now, ttl=ttl)


def _accounts_for_currency(currency: str) -> list[tuple[str, str]]:
    return [(ACCOUNTS[aid][0], f"add:acc:{aid}") for aid, (_, cur) in ACCOUNTS.items() if cur == currency]


# ---------------------------------------------------------------------------
# /add entry point
# ---------------------------------------------------------------------------


def handle_add_start(token: str, chat_id: int, user_id: int) -> None:
    state = _make_state("AMOUNT", {})
    dynamodb.set_conv_state(user_id, state)
    telegram_api.send_message(token, chat_id, "Enter amount:")


# ---------------------------------------------------------------------------
# Text messages during /add flow
# ---------------------------------------------------------------------------


def handle_add_message(token: str, chat_id: int, user_id: int, text: str, message: dict[str, Any]) -> None:
    state = dynamodb.get_conv_state(user_id)
    if state is None:
        return

    step = state.step
    data = state.data

    if step == "AMOUNT":
        try:
            amount = Decimal(text.strip().replace(",", "."))
        except InvalidOperation:
            telegram_api.send_message(token, chat_id, "Invalid amount. Try again:")
            return

        data["amount"] = str(amount)
        new_state = _make_state("CURRENCY", data)
        dynamodb.set_conv_state(user_id, new_state)

        keyboard = telegram_api.build_keyboard(_CURRENCY_OPTIONS, columns=3)
        telegram_api.send_message_with_keyboard(token, chat_id, "Select currency:", keyboard)

    elif step == "DESCRIPTION":
        description, tags = extract_tags(text)
        if not description:
            telegram_api.send_message(token, chat_id, "Description cannot be only tags. Try again:")
            return

        data["description"] = description
        data["tags"] = tags
        currency = str(data.get("currency", "EUR"))
        accounts = _accounts_for_currency(currency)

        new_state = _make_state("ACCOUNT", data)
        dynamodb.set_conv_state(user_id, new_state)

        keyboard = telegram_api.build_keyboard(accounts, columns=2)
        telegram_api.send_message_with_keyboard(token, chat_id, "Select account:", keyboard)


# ---------------------------------------------------------------------------
# Callback queries during /add flow
# ---------------------------------------------------------------------------


def handle_add_callback(
    token: str,
    chat_id: int,
    user_id: int,
    callback_query_id: str,
    data_str: str,
    message: dict[str, Any],
) -> None:
    state = dynamodb.get_conv_state(user_id)
    if state is None:
        telegram_api.answer_callback(token, callback_query_id, "Session expired.")
        return

    step = state.step
    data = state.data
    message_id = int(message.get("message_id", 0))

    if step == "CURRENCY" and data_str.startswith("add:currency:"):
        currency = data_str.split(":", 2)[2]
        data["currency"] = currency

        new_state = _make_state("CATEGORY", data)
        dynamodb.set_conv_state(user_id, new_state)

        cats = get_categories_for_mode("consumption")
        buttons = [(display, f"add:cat:{cat_id}") for cat_id, display in cats.items()]
        keyboard = telegram_api.build_keyboard(buttons, columns=2)

        telegram_api.answer_callback(token, callback_query_id)
        telegram_api.edit_message(
            token,
            chat_id,
            message_id,
            f"Currency: {currency}\n\nSelect category:",
            keyboard,
        )

    elif step == "CATEGORY" and data_str.startswith("add:cat:"):
        cat_id = data_str.split(":", 2)[2]
        data["category"] = cat_id
        data["category_display"] = CATEGORIES[cat_id]["display_name"]
        data["mode"] = CATEGORIES[cat_id]["mode"]

        new_state = _make_state("DESCRIPTION", data)
        dynamodb.set_conv_state(user_id, new_state)

        telegram_api.answer_callback(token, callback_query_id)
        telegram_api.edit_message(
            token,
            chat_id,
            message_id,
            f"Category: {data['category_display']}\n\nEnter description:",
        )

    elif step == "ACCOUNT" and data_str.startswith("add:acc:"):
        account_id = data_str.split(":", 2)[2]
        data["source_account"] = account_id

        new_state = _make_state("CONFIRM", data)
        dynamodb.set_conv_state(user_id, new_state)

        summary = _build_summary(data)
        keyboard = telegram_api.build_keyboard([("Confirm", "add:confirm"), ("Cancel", "add:cancel")])

        telegram_api.answer_callback(token, callback_query_id)
        telegram_api.edit_message(token, chat_id, message_id, summary, keyboard)

    elif step == "CONFIRM" and data_str == "add:confirm":
        tx = _build_transaction(data)
        update_id = message.get("message_id") or message.get("update_id", 0)
        added = dynamodb.add_transaction(user_id, tx, int(update_id))
        dynamodb.delete_conv_state(user_id)

        telegram_api.answer_callback(token, callback_query_id)
        if added:
            confirmation = formatters.format_confirmation(tx)
            telegram_api.edit_message(token, chat_id, message_id, confirmation)
        else:
            telegram_api.edit_message(token, chat_id, message_id, "Duplicate, already recorded.")

    elif data_str == "add:cancel":
        dynamodb.delete_conv_state(user_id)
        telegram_api.answer_callback(token, callback_query_id, "Cancelled")
        telegram_api.edit_message(token, chat_id, message_id, "Cancelled.")

    else:
        telegram_api.answer_callback(token, callback_query_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_summary(data: dict[str, object]) -> str:
    amount = data.get("amount", "0")
    currency = data.get("currency", "EUR")
    cat_display = data.get("category_display", "")
    description = data.get("description", "")
    account_id = str(data.get("source_account", ""))
    account_name = ACCOUNTS[account_id][0] if account_id in ACCOUNTS else account_id

    return (
        "Confirm transaction:\n\n"
        f"Amount: {amount} {currency}\n"
        f"Category: {cat_display}\n"
        f"Description: {description}\n"
        f"Account: {account_name}"
    )


def _build_transaction(data: dict[str, object]) -> Transaction:
    amount = Decimal(str(data.get("amount", "0")))
    currency = str(data.get("currency", "EUR"))
    category = str(data.get("category", "unknown"))
    category_display = str(data.get("category_display", ""))
    description = str(data.get("description", ""))
    source_account = str(data.get("source_account", ""))
    mode = str(data.get("mode", "consumption"))
    tx_type = "income" if mode == "income" else "expense"
    raw_tags = data.get("tags") or []
    tags = [str(t) for t in raw_tags] if isinstance(raw_tags, list) else []

    amount_minor = to_minor(amount, currency)
    signed_amount_minor = amount_minor if tx_type == "income" else -amount_minor

    now = datetime.now(UTC)
    tx_id = uuid4().hex[:12]

    return Transaction(
        tx_id=tx_id,
        date=now.strftime("%Y-%m-%d"),
        timestamp=now.isoformat(),
        amount_minor=amount_minor,
        signed_amount_minor=signed_amount_minor,
        currency=currency,
        description=description,
        category=category,
        category_display=category_display,
        source_account=source_account,
        mode=mode,
        tx_type=tx_type,
        tags=tags,
    )


# ---------------------------------------------------------------------------
# /edit flow
# ---------------------------------------------------------------------------

_EDIT_STEPS = {
    "EDIT_FIELD",
    "EDIT_AMOUNT",
    "EDIT_DESCRIPTION",
    "EDIT_CATEGORY",
    "EDIT_ACCOUNT",
    "EDIT_TAGS",
    "EDIT_CONFIRM",
}

_EDIT_FIELD_BUTTONS: list[tuple[str, str]] = [
    ("Amount", "edit:field:amount"),
    ("Description", "edit:field:description"),
    ("Category", "edit:field:category"),
    ("Account", "edit:field:account"),
    ("Tags", "edit:field:tags"),
]


def _parse_tx_sk(tx_sk: str) -> tuple[str, str] | None:
    parts = tx_sk.split("#", 2)
    if len(parts) != 3 or parts[0] != "TX":
        return None
    return parts[1], parts[2]


def _load_edit_target(user_id: int, tx_sk: str) -> Transaction | None:
    parsed = _parse_tx_sk(tx_sk)
    if parsed is None:
        return None
    timestamp, tx_id = parsed
    return dynamodb.get_transaction_by_key(user_id, timestamp, tx_id)


def _field_picker_keyboard() -> list[list[dict[str, str]]]:
    return telegram_api.build_keyboard(
        [*_EDIT_FIELD_BUTTONS, ("Cancel", "edit:cancel")],
        columns=2,
    )


def _confirm_keyboard() -> list[list[dict[str, str]]]:
    return telegram_api.build_keyboard([("Confirm", "edit:confirm"), ("Cancel", "edit:cancel")])


def handle_edit_start(token: str, chat_id: int, user_id: int, tx_sk: str) -> None:
    tx = _load_edit_target(user_id, tx_sk)
    if tx is None:
        telegram_api.send_message(token, chat_id, "Transaction not found.")
        return
    if tx.paired_tx_sk:
        telegram_api.send_message(
            token,
            chat_id,
            "Cannot edit a transfer leg. Delete the transfer and create a new one.",
        )
        return

    state = _make_state("EDIT_FIELD", {"edit_tx_sk": tx_sk})
    dynamodb.set_conv_state(user_id, state)

    preview = formatters.format_history_entry(tx, 0).split("\n", 1)[1]
    telegram_api.send_message_with_keyboard(
        token,
        chat_id,
        f"Edit transaction:\n{preview}\n\nWhich field?",
        _field_picker_keyboard(),
    )


def handle_edit_message(token: str, chat_id: int, user_id: int, text: str, message: dict[str, Any]) -> None:
    state = dynamodb.get_conv_state(user_id)
    if state is None or state.step not in _EDIT_STEPS:
        return

    tx_sk = str(state.data.get("edit_tx_sk", ""))
    old_tx = _load_edit_target(user_id, tx_sk)
    if old_tx is None:
        dynamodb.delete_conv_state(user_id)
        telegram_api.send_message(token, chat_id, "Transaction no longer exists.")
        return

    if state.step == "EDIT_AMOUNT":
        try:
            amount = Decimal(text.strip().replace(",", "."))
        except InvalidOperation:
            telegram_api.send_message(token, chat_id, "Invalid amount. Try again:")
            return
        if amount <= 0:
            telegram_api.send_message(token, chat_id, "Amount must be positive. Try again:")
            return

        new_amount_minor = to_minor(amount, old_tx.currency)
        state.data["new_amount_minor"] = new_amount_minor
        new_state = _make_state("EDIT_CONFIRM", state.data)
        dynamodb.set_conv_state(user_id, new_state)

        new_tx = _apply_edit(old_tx, state.data)
        telegram_api.send_message_with_keyboard(
            token, chat_id, _build_edit_preview(old_tx, new_tx, "amount"), _confirm_keyboard()
        )

    elif state.step == "EDIT_DESCRIPTION":
        new_description = text.strip()
        if not new_description:
            telegram_api.send_message(token, chat_id, "Description cannot be empty. Try again:")
            return
        state.data["new_description"] = new_description
        new_state = _make_state("EDIT_CONFIRM", state.data)
        dynamodb.set_conv_state(user_id, new_state)

        new_tx = _apply_edit(old_tx, state.data)
        telegram_api.send_message_with_keyboard(
            token, chat_id, _build_edit_preview(old_tx, new_tx, "description"), _confirm_keyboard()
        )

    elif state.step == "EDIT_TAGS":
        tags = sorted({normalize_tag(t.lstrip("#")) for t in text.split()} - {""})
        state.data["new_tags"] = tags
        new_state = _make_state("EDIT_CONFIRM", state.data)
        dynamodb.set_conv_state(user_id, new_state)

        new_tx = _apply_edit(old_tx, state.data)
        telegram_api.send_message_with_keyboard(
            token, chat_id, _build_edit_preview(old_tx, new_tx, "tags"), _confirm_keyboard()
        )


def handle_edit_callback(
    token: str,
    chat_id: int,
    user_id: int,
    callback_query_id: str,
    data_str: str,
    message: dict[str, Any],
) -> None:
    if data_str.startswith("edit:TX#"):
        tx_sk = data_str[len("edit:") :]
        telegram_api.answer_callback(token, callback_query_id)
        handle_edit_start(token, chat_id, user_id, tx_sk)
        return

    state = dynamodb.get_conv_state(user_id)
    if state is None or state.step not in _EDIT_STEPS:
        telegram_api.answer_callback(token, callback_query_id, "Session expired.")
        return

    message_id = int(message.get("message_id", 0))
    tx_sk = str(state.data.get("edit_tx_sk", ""))
    old_tx = _load_edit_target(user_id, tx_sk)
    if old_tx is None:
        dynamodb.delete_conv_state(user_id)
        telegram_api.answer_callback(token, callback_query_id, "Transaction missing")
        telegram_api.edit_message(token, chat_id, message_id, "Transaction no longer exists.")
        return

    if data_str == "edit:cancel":
        dynamodb.delete_conv_state(user_id)
        telegram_api.answer_callback(token, callback_query_id, "Cancelled")
        telegram_api.edit_message(token, chat_id, message_id, "Edit cancelled.")
        return

    if state.step == "EDIT_FIELD" and data_str.startswith("edit:field:"):
        field = data_str.split(":", 2)[2]
        _handle_field_pick(token, chat_id, user_id, callback_query_id, message_id, state, old_tx, field)
        return

    if state.step == "EDIT_CATEGORY" and data_str.startswith("edit:cat:"):
        cat_id = data_str.split(":", 2)[2]
        if cat_id not in CATEGORIES:
            telegram_api.answer_callback(token, callback_query_id, "Unknown category")
            return
        state.data["new_category"] = cat_id
        state.data["new_category_display"] = CATEGORIES[cat_id]["display_name"]
        new_state = _make_state("EDIT_CONFIRM", state.data)
        dynamodb.set_conv_state(user_id, new_state)

        new_tx = _apply_edit(old_tx, state.data)
        telegram_api.answer_callback(token, callback_query_id)
        telegram_api.edit_message(
            token, chat_id, message_id, _build_edit_preview(old_tx, new_tx, "category"), _confirm_keyboard()
        )
        return

    if state.step == "EDIT_TAGS" and data_str == "edit:tags:clear":
        state.data["new_tags"] = []
        new_state = _make_state("EDIT_CONFIRM", state.data)
        dynamodb.set_conv_state(user_id, new_state)

        new_tx = _apply_edit(old_tx, state.data)
        telegram_api.answer_callback(token, callback_query_id, "Tags cleared")
        telegram_api.edit_message(
            token, chat_id, message_id, _build_edit_preview(old_tx, new_tx, "tags"), _confirm_keyboard()
        )
        return

    if state.step == "EDIT_ACCOUNT" and data_str.startswith("edit:acc:"):
        account_id = data_str.split(":", 2)[2]
        if account_id not in ACCOUNTS:
            telegram_api.answer_callback(token, callback_query_id, "Unknown account")
            return
        state.data["new_account"] = account_id
        new_state = _make_state("EDIT_CONFIRM", state.data)
        dynamodb.set_conv_state(user_id, new_state)

        new_tx = _apply_edit(old_tx, state.data)
        telegram_api.answer_callback(token, callback_query_id)
        telegram_api.edit_message(
            token, chat_id, message_id, _build_edit_preview(old_tx, new_tx, "account"), _confirm_keyboard()
        )
        return

    if state.step == "EDIT_CONFIRM" and data_str == "edit:confirm":
        new_tx = _apply_edit(old_tx, state.data)
        update_id = _edit_update_id(callback_query_id)
        try:
            updated = dynamodb.update_transaction(user_id, old_tx, new_tx, update_id)
        except ValueError as exc:
            dynamodb.delete_conv_state(user_id)
            telegram_api.answer_callback(token, callback_query_id, "Rejected")
            telegram_api.edit_message(token, chat_id, message_id, f"Cannot edit: {exc}")
            return

        dynamodb.delete_conv_state(user_id)
        telegram_api.answer_callback(token, callback_query_id)
        if updated:
            telegram_api.edit_message(token, chat_id, message_id, _build_updated_message(new_tx))
        else:
            telegram_api.edit_message(
                token, chat_id, message_id, "Could not update — transaction changed or was deleted."
            )
        return

    telegram_api.answer_callback(token, callback_query_id)


def _handle_field_pick(
    token: str,
    chat_id: int,
    user_id: int,
    callback_query_id: str,
    message_id: int,
    state: ConversationState,
    old_tx: Transaction,
    field: str,
) -> None:
    state.data["edit_field"] = field

    if field == "amount":
        new_state = _make_state("EDIT_AMOUNT", state.data)
        dynamodb.set_conv_state(user_id, new_state)
        telegram_api.answer_callback(token, callback_query_id)
        telegram_api.edit_message(
            token,
            chat_id,
            message_id,
            f"Current amount: {old_tx.display_amount()}\n\nEnter new amount in {old_tx.currency}:",
        )

    elif field == "description":
        new_state = _make_state("EDIT_DESCRIPTION", state.data)
        dynamodb.set_conv_state(user_id, new_state)
        telegram_api.answer_callback(token, callback_query_id)
        telegram_api.edit_message(
            token,
            chat_id,
            message_id,
            f"Current description: {old_tx.description}\n\nEnter new description:",
        )

    elif field == "category":
        new_state = _make_state("EDIT_CATEGORY", state.data)
        dynamodb.set_conv_state(user_id, new_state)
        cats = get_categories_for_mode(old_tx.mode)
        buttons = [(display, f"edit:cat:{cat_id}") for cat_id, display in cats.items()]
        keyboard = telegram_api.build_keyboard([*buttons, ("Cancel", "edit:cancel")], columns=2)
        telegram_api.answer_callback(token, callback_query_id)
        telegram_api.edit_message(
            token,
            chat_id,
            message_id,
            f"Current category: {old_tx.category_display} ({old_tx.category})\n\nSelect new category:",
            keyboard,
        )

    elif field == "account":
        new_state = _make_state("EDIT_ACCOUNT", state.data)
        dynamodb.set_conv_state(user_id, new_state)
        buttons = [(f"{ACCOUNTS[aid][0]} ({cur})", f"edit:acc:{aid}") for aid, (_, cur) in ACCOUNTS.items()]
        keyboard = telegram_api.build_keyboard([*buttons, ("Cancel", "edit:cancel")], columns=2)
        telegram_api.answer_callback(token, callback_query_id)
        telegram_api.edit_message(
            token,
            chat_id,
            message_id,
            f"Current account: {old_tx.source_account} ({old_tx.currency})\n\nSelect new account:",
            keyboard,
        )

    elif field == "tags":
        new_state = _make_state("EDIT_TAGS", state.data)
        dynamodb.set_conv_state(user_id, new_state)
        current_tags = " ".join(f"#{t}" for t in old_tx.tags) or "(none)"
        keyboard = telegram_api.build_keyboard([("Clear tags", "edit:tags:clear"), ("Cancel", "edit:cancel")])
        telegram_api.answer_callback(token, callback_query_id)
        telegram_api.edit_message(
            token,
            chat_id,
            message_id,
            f"Current tags: {current_tags}\n\nEnter new tags (space-separated) or tap Clear tags:",
            keyboard,
        )

    else:
        telegram_api.answer_callback(token, callback_query_id, "Unknown field")


def _apply_edit(old_tx: Transaction, data: dict[str, object]) -> Transaction:
    new_amount_minor = data.get("new_amount_minor")
    new_description = data.get("new_description")
    new_category = data.get("new_category")
    new_category_display = data.get("new_category_display")
    new_account = data.get("new_account")
    new_tags = data.get("new_tags")

    source_account = old_tx.source_account
    currency = old_tx.currency
    amount_minor = old_tx.amount_minor

    if isinstance(new_account, str):
        source_account = new_account
        new_currency = ACCOUNTS[new_account][1]
        if new_currency != old_tx.currency:
            old_value = from_minor(old_tx.amount_minor, old_tx.currency)
            amount_minor = to_minor(old_value, new_currency)
            currency = new_currency
        else:
            currency = new_currency

    if isinstance(new_amount_minor, int | Decimal):
        amount_minor = int(new_amount_minor)

    sign = 1 if old_tx.tx_type == "income" else -1
    signed_amount_minor = sign * amount_minor

    description = str(new_description) if isinstance(new_description, str) else old_tx.description
    category = str(new_category) if isinstance(new_category, str) else old_tx.category
    category_display = str(new_category_display) if isinstance(new_category_display, str) else old_tx.category_display
    tags = sorted({str(t) for t in new_tags if t}) if isinstance(new_tags, list | tuple | set) else list(old_tx.tags)

    return Transaction(
        tx_id=old_tx.tx_id,
        date=old_tx.date,
        timestamp=old_tx.timestamp,
        amount_minor=amount_minor,
        signed_amount_minor=signed_amount_minor,
        currency=currency,
        description=description,
        category=category,
        category_display=category_display,
        subcategory=old_tx.subcategory,
        source_account=source_account,
        mode=old_tx.mode,
        tx_type=old_tx.tx_type,
        tags=tags,
    )


def _build_edit_preview(old_tx: Transaction, new_tx: Transaction, field: str) -> str:
    lines = ["Update preview:", ""]
    lines.append(formatters.format_history_entry(new_tx, 0).split("\n", 1)[1])
    lines.append("")
    if field == "amount":
        lines.append(f"Amount: {old_tx.display_amount()} → {new_tx.display_amount()}")
    elif field == "description":
        lines.append(f"Description: {old_tx.description} → {new_tx.description}")
    elif field == "category":
        lines.append(
            f"Category: {old_tx.category_display} ({old_tx.category}) → {new_tx.category_display} ({new_tx.category})"
        )
    elif field == "account":
        lines.append(
            f"Account: {old_tx.source_account} ({old_tx.currency}) → {new_tx.source_account} ({new_tx.currency})"
        )
        if old_tx.currency != new_tx.currency:
            lines.append(f"Amount: {old_tx.display_amount()} → {new_tx.display_amount()}")
    elif field == "tags":
        old_tag_str = " ".join(f"#{t}" for t in old_tx.tags) or "(none)"
        new_tag_str = " ".join(f"#{t}" for t in new_tx.tags) or "(none)"
        lines.append(f"Tags: {old_tag_str} → {new_tag_str}")
    return "\n".join(lines)


def _build_updated_message(new_tx: Transaction) -> str:
    prefix = "+" if new_tx.tx_type == "income" else "-"
    lines = [
        "Updated:",
        f"{prefix}{new_tx.display_amount()} — {new_tx.description}",
        f"Category: {new_tx.category_display} ({new_tx.category})",
        f"Account: {new_tx.source_account}",
    ]
    if new_tx.tags:
        lines.append("Tags: " + " ".join(f"#{t}" for t in new_tx.tags))
    return "\n".join(lines)


def _edit_update_id(callback_query_id: str) -> int:
    digest = hashlib.sha1(callback_query_id.encode("utf-8")).hexdigest()
    return int(digest[:12], 16)

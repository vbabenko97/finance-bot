"""/export handler (download transactions as CSV)."""

from __future__ import annotations

import csv
import io
import re
from datetime import date
from decimal import Decimal
from typing import Any

from telegram_bot.bot import telegram_api
from telegram_bot.storage import dynamodb
from telegram_bot.storage.models import MINOR_UNIT_FACTOR, Transaction

_EXPORT_COLUMNS = [
    "date",
    "timestamp",
    "tx_type",
    "amount",
    "currency",
    "description",
    "category",
    "category_id",
    "account",
    "tags",
    "recur_id",
]


def _build_export_csv(transactions: list[Transaction]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(_EXPORT_COLUMNS)
    for tx in transactions:
        factor = MINOR_UNIT_FACTOR[tx.currency]
        places = len(str(factor)) - 1
        value = Decimal(tx.signed_amount_minor) / Decimal(factor)
        writer.writerow(
            [
                tx.date,
                tx.timestamp,
                tx.tx_type,
                f"{value:.{places}f}",
                tx.currency,
                tx.description,
                tx.category_display,
                tx.category,
                tx.source_account,
                " ".join(f"#{t}" for t in sorted(tx.tags)),
                tx.recur_id,
            ]
        )
    return buf.getvalue().encode("utf-8-sig")


def handle_export(token: str, chat_id: int, user_id: int, text: str, message: dict[str, Any]) -> None:
    after_cmd = text.strip()
    if after_cmd.lower().startswith("/export"):
        after_cmd = after_cmd[7:].strip()

    if after_cmd:
        if not re.fullmatch(r"\d{4}-\d{2}", after_cmd):
            telegram_api.send_message(token, chat_id, "Usage: /export or /export YYYY-MM")
            return
        try:
            date.fromisoformat(f"{after_cmd}-01")
        except ValueError:
            telegram_api.send_message(token, chat_id, "Usage: /export or /export YYYY-MM")
            return
        month: str | None = after_cmd
        filename = f"transactions_{after_cmd}.csv"
    else:
        month = None
        filename = "transactions_all.csv"

    transactions = dynamodb.get_all_transactions(user_id)
    if month is not None:
        transactions = [tx for tx in transactions if tx.date.startswith(month)]
    transactions.sort(key=lambda t: t.timestamp)

    content = _build_export_csv(transactions)
    caption = (
        f"{len(transactions)} transaction(s)" if month is None else f"{len(transactions)} transaction(s) in {month}"
    )
    telegram_api.send_document(token, chat_id, filename, content, "text/csv", caption=caption)

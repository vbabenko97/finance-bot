from __future__ import annotations

import calendar
import csv
import hashlib
import io
import json
import logging
import re
import urllib.request
from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from telegram_bot.bot import conversation, formatters, scheduler, telegram_api
from telegram_bot.bot.quick_add import QuickAddParseResult, extract_tags, normalize_tag, parse_quick_add_detailed
from telegram_bot.config.accounts import ACCOUNTS, DEFAULT_ACCOUNTS
from telegram_bot.config.categories import CATEGORIES
from telegram_bot.config.merchants import canonical_merchant
from telegram_bot.storage import dynamodb
from telegram_bot.storage.models import (
    MINOR_UNIT_FACTOR,
    RecurringTemplate,
    Transaction,
    format_amount,
    from_minor,
    to_minor,
)

logger = logging.getLogger(__name__)

_QUICK_ADD_ERROR_MESSAGES: dict[str, str] = {
    "empty_input": "Enter an amount and description. Example: 150 Сільпо",
    "invalid_amount": "Amount must come first. Example: 150 Сільпо or 25 USD Netflix",
    "missing_description": "Add a description after the amount. Example: 150 Сільпо",
    "unsupported_currency": "Currency must be one of: EUR, USD, UAH, USDT, BTC",
    "unknown_account": "Unknown account after @account. Use /start to see valid examples.",
    "invalid_format": "Could not parse. Try: 150 Сільпо, 25 USD Netflix, or 100 coffee @bank_usd_1",
}


def _quick_add_error_message(result: QuickAddParseResult, tx_type: str) -> str:
    default = "Could not parse. Try: /income 5000 Зарплатня" if tx_type == "income" else "Try: 150 Сільпо"
    base = _QUICK_ADD_ERROR_MESSAGES.get(result.error_code or "", default)
    if tx_type == "income":
        if result.error_code == "missing_description":
            return "Add a description after the amount. Example: /income 5000 Зарплатня"
        if result.error_code == "invalid_amount":
            return "Amount must come first. Example: /income 5000 Зарплатня"
        return base if base != default else default
    return base


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------


def _build_help_text() -> str:
    return (
        "Finance Bot help\n\n"
        "Quick add:\n"
        "  <code>15 Billa</code>\n"
        "  <code>25 USD Netflix</code>\n"
        "  <code>100 UAH coffee @bank_uah_1</code>\n"
        "  <code>40 USD lunch #work</code>\n\n"
        "Income:\n"
        "  <code>/income 3000 Salary</code>\n\n"
        "Commands:\n"
        "  /balance - show balances\n"
        "  /history - recent transactions\n"
        "  /search - search transactions\n"
        "  /portfolio - portfolio &amp; net worth\n"
        "  /budget - monthly budget\n"
        "  /summary - monthly summary &amp; pace\n"
        "  /export [YYYY-MM] - download transactions as CSV\n"
        "  /set_budget - set budget limit\n"
        "  /delete_budget - remove budget\n"
        "  /add - step-by-step add\n"
        "  /edit &lt;n&gt; - edit nth transaction from history\n"
        "  /delete - delete last transaction\n"
        "  /recurring - manage recurring templates\n"
        "  /transfer &lt;amount&gt; &lt;from&gt; &lt;to&gt; - move funds between accounts\n"
        "  /set_balance &lt;account&gt; &lt;amount&gt;\n"
        "  /rates - NBU exchange rates\n"
        "  /cancel - cancel current action\n"
        "  /help - this help\n\n"
        "Budget:\n"
        "  <code>/set_budget groceries 200</code>\n"
        "  <code>/budget</code> or <code>/budget 2026-03</code>\n"
        "  <code>/delete_budget groceries</code>\n\n"
        "Search:\n"
        "  <code>/search coffee</code>\n"
        "  <code>/search -c groceries -d 2026-03</code>\n"
        "  <code>/search -t italy2026 -t trip</code>\n\n"
        "Tips:\n"
        "  amount must come first, default currency: EUR\n"
        "  supported currencies: EUR, USD, UAH, USDT, BTC\n"
        "  use @account to override the default account\n"
        "  use #tag in description to label transactions"
    )


def handle_start(token: str, chat_id: int, user_id: int, text: str, message: dict[str, Any]) -> None:
    greeting = (
        "Finance Bot\n\n"
        "Start with one of these:\n"
        "  <code>15 Billa</code>\n"
        "  <code>25 USD Netflix</code>\n"
        "  <code>/income 3000 Salary</code>\n\n"
        "Use /help for commands, account overrides, and examples."
    )
    telegram_api.send_message(token, chat_id, greeting)


def handle_help(token: str, chat_id: int, user_id: int, text: str, message: dict[str, Any]) -> None:
    telegram_api.send_message(token, chat_id, _build_help_text())


# ---------------------------------------------------------------------------
# /balance
# ---------------------------------------------------------------------------


def _fetch_fx_rates() -> dict[str, float]:
    rates: dict[str, float] = {"USD": 1.0, "USDT": 1.0}
    try:
        req = urllib.request.Request(
            "https://api.exchangerate-api.com/v4/latest/USD",
            headers={"User-Agent": "finance-bot/1.0"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        rates_raw: dict[str, Any] = data.get("rates", {})
        rates["UAH"] = float(rates_raw.get("UAH", 0))
        rates["EUR"] = float(rates_raw.get("EUR", 0))
    except Exception:
        logger.exception("Failed to fetch fiat FX rates")

    try:
        req = urllib.request.Request(
            "https://api.coinbase.com/v2/prices/BTC-USD/spot",
            headers={"User-Agent": "finance-bot/1.0"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        btc_price = float(data.get("data", {}).get("amount", 0))
        if btc_price > 0:
            rates["BTC"] = 1.0 / btc_price
    except Exception:
        logger.exception("Failed to fetch BTC rate")

    return rates


def handle_balance(token: str, chat_id: int, user_id: int, text: str, message: dict[str, Any]) -> None:
    balances = dynamodb.get_balances(user_id)
    if not balances:
        telegram_api.send_message(token, chat_id, "No balances set. Use /set_balance to initialize.")
        return

    fx_rates = dynamodb.get_fx_rates()
    if fx_rates is None:
        fx_rates = _fetch_fx_rates()
        if fx_rates:
            dynamodb.cache_fx_rates(fx_rates)

    fx_rates = fx_rates or {}
    fx_rates.setdefault("USD", 1.0)
    fx_rates.setdefault("USDT", 1.0)

    table_text = formatters.format_balance_table(balances, fx_rates)
    telegram_api.send_message(token, chat_id, table_text)


# ---------------------------------------------------------------------------
# /history
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# /delete
# ---------------------------------------------------------------------------


def handle_delete(token: str, chat_id: int, user_id: int, text: str, message: dict[str, Any]) -> None:
    tx = dynamodb.get_last_transaction(user_id)
    if tx is None:
        telegram_api.send_message(token, chat_id, "No transactions to delete.")
        return

    tx_sk = f"TX#{tx.timestamp}#{tx.tx_id}"
    entry = formatters.format_confirmation(tx)
    keyboard = telegram_api.build_keyboard([("Confirm delete", f"confirm_del:{tx_sk}"), ("Cancel", "cancel_del")])
    telegram_api.send_message_with_keyboard(token, chat_id, f"Delete this?\n\n{entry}", keyboard)


# ---------------------------------------------------------------------------
# /edit
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# /set_balance
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# /income
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# /rates
# ---------------------------------------------------------------------------


def handle_rates(token: str, chat_id: int, user_id: int, text: str, message: dict[str, Any]) -> None:
    try:
        req = urllib.request.Request(
            "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?json",
            headers={"User-Agent": "finance-bot/1.0"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
    except Exception:
        logger.exception("Failed to fetch NBU rates")
        telegram_api.send_message(token, chat_id, "Failed to fetch NBU rates.")
        return

    nbu_by_code: dict[str, dict[str, Any]] = {}
    for item in data:
        nbu_by_code[item["cc"]] = item

    date_str = nbu_by_code.get("USD", {}).get("exchangedate", "")

    lines = [f"NBU rates ({date_str}):"]
    for code in ("USD", "EUR", "GBP", "PLN", "CZK"):
        r = nbu_by_code.get(code)
        if r:
            lines.append(f"  {code}/UAH: {r['rate']:.4f}")

    usd_rate = nbu_by_code.get("USD", {}).get("rate")
    eur_rate = nbu_by_code.get("EUR", {}).get("rate")
    if usd_rate and eur_rate:
        lines.append(f"\n  EUR/USD: {eur_rate / usd_rate:.4f}")

    # BTC from Coinbase
    try:
        req = urllib.request.Request(
            "https://api.coinbase.com/v2/prices/BTC-USD/spot",
            headers={"User-Agent": "finance-bot/1.0"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            btc_data = json.loads(resp.read())
        btc_price = float(btc_data.get("data", {}).get("amount", 0))
        if btc_price > 0:
            lines.append(f"  BTC/USD: ${btc_price:,.0f}")
    except Exception:
        pass

    telegram_api.send_message(token, chat_id, "\n".join(lines))


# ---------------------------------------------------------------------------
# /portfolio
# ---------------------------------------------------------------------------


def handle_portfolio(token: str, chat_id: int, user_id: int, text: str, message: dict[str, Any]) -> None:
    balances = dynamodb.get_balances(user_id)
    if not balances:
        telegram_api.send_message(token, chat_id, "No balances set. Use /set_balance to initialize.")
        return

    fx_rates = dynamodb.get_fx_rates()
    if fx_rates is None:
        fx_rates = _fetch_fx_rates()
        if fx_rates:
            dynamodb.cache_fx_rates(fx_rates)

    fx_rates = fx_rates or {}
    fx_rates.setdefault("USD", 1.0)
    fx_rates.setdefault("USDT", 1.0)

    result = formatters.format_portfolio(balances, fx_rates)
    telegram_api.send_message(token, chat_id, result)


# ---------------------------------------------------------------------------
# /budget
# ---------------------------------------------------------------------------

_CONSUMPTION_CATEGORIES = {k for k, v in CATEGORIES.items() if v["mode"] == "consumption"}


def _to_eur_minor(amount_minor: int, currency: str, fx_rates: dict[str, float]) -> int:
    if currency == "EUR":
        return amount_minor
    value = from_minor(amount_minor, currency)
    rate = Decimal(str(fx_rates[currency]))
    eur_rate = Decimal(str(fx_rates["EUR"]))
    eur = value / rate * eur_rate
    return round(eur * 100)


def _load_fx_rates() -> dict[str, float] | None:
    fx_rates = dynamodb.get_fx_rates()
    if fx_rates is None:
        fx_rates = _fetch_fx_rates()
        if fx_rates:
            dynamodb.cache_fx_rates(fx_rates)
    if not fx_rates:
        return None
    fx_rates.setdefault("USD", 1.0)
    fx_rates.setdefault("USDT", 1.0)
    return fx_rates


def handle_budget(token: str, chat_id: int, user_id: int, text: str, message: dict[str, Any]) -> None:
    after_cmd = text.strip()
    if after_cmd.lower().startswith("/budget"):
        after_cmd = after_cmd[7:].strip()

    if after_cmd:
        if not re.fullmatch(r"\d{4}-\d{2}", after_cmd):
            telegram_api.send_message(token, chat_id, "Usage: /budget or /budget YYYY-MM")
            return
        month = after_cmd
    else:
        month = datetime.now(UTC).strftime("%Y-%m")

    fx_rates = _load_fx_rates()
    if not fx_rates or not fx_rates.get("EUR"):
        telegram_api.send_message(token, chat_id, "Could not load FX rates. Try again later.")
        return

    transactions = dynamodb.get_all_transactions(user_id)
    month_txs = [tx for tx in transactions if tx.mode == "consumption" and tx.date.startswith(month)]

    currencies_needed = {tx.currency for tx in month_txs} - {"EUR"}
    missing = {c for c in currencies_needed if not fx_rates.get(c)}
    if missing:
        telegram_api.send_message(
            token, chat_id, f"Missing FX rates for: {', '.join(sorted(missing))}. Cannot convert to EUR."
        )
        return

    spend_by_category: dict[str, int] = defaultdict(int)
    for tx in month_txs:
        spend_by_category[tx.category] += _to_eur_minor(tx.amount_minor, tx.currency, fx_rates)

    budgets = dynamodb.get_all_budgets(user_id)

    result = formatters.format_budget(dict(spend_by_category), budgets, month)
    telegram_api.send_message(token, chat_id, result)


# ---------------------------------------------------------------------------
# /summary
# ---------------------------------------------------------------------------

_SUMMARY_TOP_N = 5


def _previous_month(month: str) -> tuple[str, str]:
    month_date = date.fromisoformat(f"{month}-01")
    prev = date(month_date.year - 1, 12, 1) if month_date.month == 1 else date(month_date.year, month_date.month - 1, 1)
    return prev.strftime("%Y-%m"), prev.strftime("%B")


_SUMMARY_MODES = {"consumption", "income"}


def handle_summary(token: str, chat_id: int, user_id: int, text: str, message: dict[str, Any]) -> None:
    after_cmd = text.strip()
    if after_cmd.lower().startswith("/summary"):
        after_cmd = after_cmd[8:].strip()

    now = datetime.now(UTC)
    if after_cmd:
        if not re.fullmatch(r"\d{4}-\d{2}", after_cmd):
            telegram_api.send_message(token, chat_id, "Usage: /summary or /summary YYYY-MM")
            return
        try:
            date.fromisoformat(f"{after_cmd}-01")
        except ValueError:
            telegram_api.send_message(token, chat_id, "Usage: /summary or /summary YYYY-MM")
            return
        month = after_cmd
    else:
        month = now.strftime("%Y-%m")

    fx_rates = _load_fx_rates()
    if not fx_rates or not fx_rates.get("EUR"):
        telegram_api.send_message(token, chat_id, "Could not load FX rates. Try again later.")
        return

    transactions = dynamodb.get_all_transactions(user_id)
    month_txs = [tx for tx in transactions if tx.date.startswith(month) and tx.mode in _SUMMARY_MODES]

    currencies_needed = {tx.currency for tx in month_txs} - {"EUR"}
    missing = {c for c in currencies_needed if not fx_rates.get(c)}
    if missing:
        telegram_api.send_message(
            token, chat_id, f"Missing FX rates for: {', '.join(sorted(missing))}. Cannot convert to EUR."
        )
        return

    spend_minor = 0
    income_minor = 0
    spend_by_category: dict[str, int] = defaultdict(int)
    spend_by_merchant: dict[str, int] = defaultdict(int)
    merchant_display: dict[str, str] = {}

    for tx in month_txs:
        eur = _to_eur_minor(tx.amount_minor, tx.currency, fx_rates)
        if tx.mode == "consumption":
            spend_minor += eur
            spend_by_category[tx.category] += eur
            canonical = canonical_merchant(tx.description)
            if canonical:
                key = canonical.lower()
                merchant_display.setdefault(key, canonical)
                spend_by_merchant[key] += eur
        else:
            income_minor += eur

    prev_month, prev_label = _previous_month(month)
    prev_txs = [tx for tx in transactions if tx.date.startswith(prev_month) and tx.mode == "consumption"]
    prev_currencies = {tx.currency for tx in prev_txs} - {"EUR"}
    if any(not fx_rates.get(c) for c in prev_currencies) or not prev_txs:
        prev_spend_minor: int | None = None
    else:
        prev_spend_minor = sum(_to_eur_minor(tx.amount_minor, tx.currency, fx_rates) for tx in prev_txs)

    top_categories = sorted(spend_by_category.items(), key=lambda kv: -kv[1])[:_SUMMARY_TOP_N]
    top_merchants = [
        (merchant_display[k], v) for k, v in sorted(spend_by_merchant.items(), key=lambda kv: -kv[1])[:_SUMMARY_TOP_N]
    ]

    is_current = month == now.strftime("%Y-%m")
    days_in_month = calendar.monthrange(int(month[:4]), int(month[5:]))[1]
    day_of_month = now.day if is_current else 0

    stats = formatters.SummaryStats(
        month=month,
        spend_minor=spend_minor,
        income_minor=income_minor,
        top_categories=top_categories,
        top_merchants=top_merchants,
        prev_month_spend_minor=prev_spend_minor,
        prev_month_label=prev_label if prev_spend_minor is not None else "",
        is_current_month=is_current,
        day_of_month=day_of_month,
        days_in_month=days_in_month,
    )
    telegram_api.send_message(token, chat_id, formatters.format_summary(stats))


# ---------------------------------------------------------------------------
# /export
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# /set_budget
# ---------------------------------------------------------------------------


def handle_set_budget(token: str, chat_id: int, user_id: int, text: str, message: dict[str, Any]) -> None:
    parts = text.strip().split()
    if len(parts) != 3:
        telegram_api.send_message(token, chat_id, "Usage: /set_budget &lt;category&gt; &lt;amount_eur&gt;")
        return

    category = parts[1]
    if category not in CATEGORIES:
        valid = ", ".join(sorted(CATEGORIES))
        telegram_api.send_message(token, chat_id, f"Unknown category: {category}\nValid: {valid}")
        return

    if category not in _CONSUMPTION_CATEGORIES:
        telegram_api.send_message(
            token,
            chat_id,
            f"Budget only applies to consumption categories. '{category}' is {CATEGORIES[category]['mode']}.",
        )
        return

    try:
        amount = Decimal(parts[2].replace(",", "."))
    except InvalidOperation:
        telegram_api.send_message(token, chat_id, "Invalid amount.")
        return

    if amount <= 0:
        telegram_api.send_message(token, chat_id, "Amount must be positive.")
        return

    limit_minor = to_minor(amount, "EUR")
    dynamodb.set_budget(user_id, category, limit_minor)

    display = CATEGORIES[category]["display_name"]
    telegram_api.send_message(token, chat_id, f"Budget set: {display} ({category}) = {amount:,.0f} EUR/month")


# ---------------------------------------------------------------------------
# /delete_budget
# ---------------------------------------------------------------------------


def handle_delete_budget(token: str, chat_id: int, user_id: int, text: str, message: dict[str, Any]) -> None:
    parts = text.strip().split()
    if len(parts) != 2:
        telegram_api.send_message(token, chat_id, "Usage: /delete_budget &lt;category&gt;")
        return

    category = parts[1]
    deleted = dynamodb.delete_budget(user_id, category)
    if deleted:
        display = CATEGORIES.get(category, {}).get("display_name", category)
        telegram_api.send_message(token, chat_id, f"Budget removed: {display} ({category})")
    else:
        telegram_api.send_message(token, chat_id, f"No budget set for '{category}'.")


# ---------------------------------------------------------------------------
# /recurring
# ---------------------------------------------------------------------------


_RECURRING_USAGE = (
    "Usage:\n"
    "  /recurring — list templates\n"
    "  /recurring add monthly &lt;day 1-31&gt; &lt;amount&gt; &lt;currency&gt; &lt;category&gt; &lt;description&gt; [@account]\n"
    "  /recurring add weekly &lt;day 0-6&gt; &lt;amount&gt; &lt;currency&gt; &lt;category&gt; &lt;description&gt; [@account]\n"
    "  /recurring add daily &lt;amount&gt; &lt;currency&gt; &lt;category&gt; &lt;description&gt; [@account]\n"
    "  /recurring pause &lt;id&gt;\n"
    "  /recurring resume &lt;id&gt;\n"
    "  /recurring delete &lt;id&gt;\n\n"
    "Weekly days: 0=Mon, 1=Tue, … 6=Sun"
)

_RECURRING_ACCOUNT_RE = re.compile(r"\s+@(\S+)\s*$")
_SCHEDULES = {"monthly", "weekly", "daily"}


def handle_recurring(token: str, chat_id: int, user_id: int, text: str, message: dict[str, Any]) -> None:
    after_cmd = text.strip()
    if after_cmd.lower().startswith("/recurring"):
        after_cmd = after_cmd[10:].strip()

    if not after_cmd:
        _recurring_list(token, chat_id, user_id)
        return

    parts = after_cmd.split(maxsplit=1)
    sub = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""

    if sub == "add":
        _recurring_add(token, chat_id, user_id, rest)
    elif sub == "pause":
        _recurring_set_active(token, chat_id, user_id, rest, active=False)
    elif sub == "resume":
        _recurring_set_active(token, chat_id, user_id, rest, active=True)
    elif sub == "delete":
        _recurring_delete(token, chat_id, user_id, rest)
    else:
        telegram_api.send_message(token, chat_id, _RECURRING_USAGE)


def _recurring_list(token: str, chat_id: int, user_id: int) -> None:
    templates = dynamodb.get_all_recurring_templates(user_id)
    if not templates:
        telegram_api.send_message(token, chat_id, "No recurring templates. Use /recurring add to create one.")
        return

    templates.sort(key=lambda t: (not t.active, t.next_run_date, t.description))
    lines: list[str] = [f"Recurring templates ({len(templates)}):"]
    for tpl in templates:
        prefix = "+" if tpl.tx_type == "income" else "-"
        amount_str = format_amount(tpl.amount_minor, tpl.currency)
        status = "Active" if tpl.active else "Paused"
        schedule = _schedule_display(tpl.schedule, tpl.schedule_day)
        lines.append(
            f"\n<code>{tpl.recur_id}</code> — {status}\n"
            f"{prefix}{amount_str} — {tpl.description}\n"
            f"{tpl.category_display} ({tpl.category}) | {tpl.source_account}\n"
            f"{schedule} | next: {tpl.next_run_date}"
        )
    telegram_api.send_message(token, chat_id, "\n".join(lines))


def _schedule_display(schedule: str, schedule_day: int) -> str:
    if schedule == "daily":
        return "Daily"
    if schedule == "weekly":
        weekday_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        name = weekday_names[schedule_day] if 0 <= schedule_day < 7 else f"day {schedule_day}"
        return f"Weekly ({name})"
    return f"Monthly (day {schedule_day})"


def _recurring_add(token: str, chat_id: int, user_id: int, args: str) -> None:
    args_stripped = args.strip()
    if not args_stripped:
        telegram_api.send_message(token, chat_id, _RECURRING_USAGE)
        return

    account_override: str | None = None
    acc_match = _RECURRING_ACCOUNT_RE.search(args_stripped)
    if acc_match:
        account_id = acc_match.group(1)
        if account_id not in ACCOUNTS:
            telegram_api.send_message(token, chat_id, f"Unknown account: {account_id}")
            return
        account_override = account_id
        args_stripped = args_stripped[: acc_match.start()].rstrip()

    tokens = args_stripped.split()
    if not tokens:
        telegram_api.send_message(token, chat_id, _RECURRING_USAGE)
        return

    schedule = tokens[0].lower()
    if schedule not in _SCHEDULES:
        telegram_api.send_message(token, chat_id, _RECURRING_USAGE)
        return

    idx = 1
    if schedule == "monthly":
        if len(tokens) < 6 or not tokens[1].isdigit():
            telegram_api.send_message(token, chat_id, _RECURRING_USAGE)
            return
        schedule_day = int(tokens[1])
        if not 1 <= schedule_day <= 31:
            telegram_api.send_message(token, chat_id, "Monthly day must be 1-31.")
            return
        idx = 2
    elif schedule == "weekly":
        if len(tokens) < 6 or not tokens[1].isdigit():
            telegram_api.send_message(token, chat_id, _RECURRING_USAGE)
            return
        schedule_day = int(tokens[1])
        if not 0 <= schedule_day <= 6:
            telegram_api.send_message(token, chat_id, "Weekly day must be 0-6 (0=Mon).")
            return
        idx = 2
    else:
        if len(tokens) < 5:
            telegram_api.send_message(token, chat_id, _RECURRING_USAGE)
            return
        schedule_day = 0

    if idx + 3 >= len(tokens):
        telegram_api.send_message(token, chat_id, _RECURRING_USAGE)
        return

    amount_token = tokens[idx]
    currency = tokens[idx + 1].upper()
    category = tokens[idx + 2]
    description_raw = " ".join(tokens[idx + 3 :])

    try:
        amount = Decimal(amount_token.replace(",", "."))
    except InvalidOperation:
        telegram_api.send_message(token, chat_id, "Invalid amount.")
        return
    if amount <= 0:
        telegram_api.send_message(token, chat_id, "Amount must be positive.")
        return

    if currency not in MINOR_UNIT_FACTOR:
        telegram_api.send_message(token, chat_id, f"Unsupported currency: {currency}")
        return

    if category not in CATEGORIES:
        valid = ", ".join(sorted(CATEGORIES))
        telegram_api.send_message(token, chat_id, f"Unknown category: {category}\nValid: {valid}")
        return

    mode = CATEGORIES[category]["mode"]
    if mode == "movement":
        telegram_api.send_message(token, chat_id, "Recurring movement categories not supported.")
        return
    tx_type = "income" if mode == "income" else "expense"

    description, tags = extract_tags(description_raw)
    if not description:
        telegram_api.send_message(token, chat_id, "Description cannot be only tags.")
        return

    if account_override is None:
        account_id = DEFAULT_ACCOUNTS.get(currency, "")
        if not account_id:
            telegram_api.send_message(token, chat_id, f"No default account for {currency}. Use @account.")
            return
    else:
        account_id = account_override
        if ACCOUNTS[account_id][1] != currency:
            telegram_api.send_message(
                token,
                chat_id,
                f"Account {account_id} is {ACCOUNTS[account_id][1]}, not {currency}.",
            )
            return

    now = datetime.now(UTC)
    next_run = scheduler.initial_next_run_for_now(now, schedule, schedule_day)

    template = RecurringTemplate(
        recur_id=uuid4().hex[:12],
        description=description,
        amount_minor=to_minor(amount, currency),
        currency=currency,
        category=category,
        category_display=CATEGORIES[category]["display_name"],
        source_account=account_id,
        mode=mode,
        tx_type=tx_type,
        schedule=schedule,
        schedule_day=schedule_day,
        next_run_date=next_run.strftime("%Y-%m-%d"),
        active=True,
        tags=tags,
    )
    dynamodb.put_recurring_template(user_id, template)

    prefix = "+" if tx_type == "income" else "-"
    schedule_text = _schedule_display(schedule, schedule_day)
    telegram_api.send_message(
        token,
        chat_id,
        (
            f"Added recurring template:\n"
            f"<code>{template.recur_id}</code>\n"
            f"{prefix}{format_amount(template.amount_minor, currency)} — {description}\n"
            f"{template.category_display} ({category}) | {account_id}\n"
            f"{schedule_text} | next: {template.next_run_date}"
        ),
    )


def _recurring_set_active(token: str, chat_id: int, user_id: int, args: str, active: bool) -> None:
    recur_id = args.strip()
    if not recur_id:
        telegram_api.send_message(token, chat_id, _RECURRING_USAGE)
        return

    tpl = dynamodb.get_recurring_template(user_id, recur_id)
    if tpl is None:
        telegram_api.send_message(token, chat_id, f"No template with id {recur_id}")
        return

    if active and tpl.active:
        telegram_api.send_message(token, chat_id, f"Already active: {recur_id}")
        return
    if not active and not tpl.active:
        telegram_api.send_message(token, chat_id, f"Already paused: {recur_id}")
        return

    tpl.active = active

    if active:
        today = datetime.now(UTC).date()
        if tpl.next_run_date <= today.strftime("%Y-%m-%d"):
            new_next = scheduler.advance_to_future(tpl.next_run_date, tpl.schedule, tpl.schedule_day, today)
            tpl.next_run_date = new_next.strftime("%Y-%m-%d")

    dynamodb.put_recurring_template(user_id, tpl)
    label = "resumed" if active else "paused"
    telegram_api.send_message(token, chat_id, f"Template {recur_id} {label}. Next run: {tpl.next_run_date}")


def _recurring_delete(token: str, chat_id: int, user_id: int, args: str) -> None:
    recur_id = args.strip()
    if not recur_id:
        telegram_api.send_message(token, chat_id, _RECURRING_USAGE)
        return

    deleted = dynamodb.delete_recurring_template(user_id, recur_id)
    if deleted:
        telegram_api.send_message(token, chat_id, f"Deleted template {recur_id}.")
    else:
        telegram_api.send_message(token, chat_id, f"No template with id {recur_id}")


# ---------------------------------------------------------------------------
# /transfer
# ---------------------------------------------------------------------------

_TRANSFER_USAGE = "Usage: /transfer &lt;amount&gt; &lt;from_account&gt; &lt;to_account&gt;"


def _convert_minor(
    amount_minor: int,
    from_currency: str,
    to_currency: str,
    fx_rates: dict[str, float],
) -> int:
    if from_currency == to_currency:
        return amount_minor
    factor_from = Decimal(MINOR_UNIT_FACTOR[from_currency])
    factor_to = Decimal(MINOR_UNIT_FACTOR[to_currency])
    rate_from = Decimal(str(fx_rates[from_currency]))
    rate_to = Decimal(str(fx_rates[to_currency]))
    value_usd = Decimal(amount_minor) / factor_from / rate_from
    value_to = value_usd * rate_to
    return int((value_to * factor_to).to_integral_value(rounding="ROUND_HALF_UP"))


def handle_transfer(token: str, chat_id: int, user_id: int, text: str, message: dict[str, Any]) -> None:
    after_cmd = text.strip()
    if after_cmd.lower().startswith("/transfer"):
        after_cmd = after_cmd[9:].strip()

    tokens = after_cmd.split()
    if len(tokens) != 3:
        telegram_api.send_message(token, chat_id, _TRANSFER_USAGE)
        return

    try:
        amount = Decimal(tokens[0].replace(",", "."))
    except InvalidOperation:
        telegram_api.send_message(token, chat_id, "Invalid amount.")
        return
    if amount <= 0:
        telegram_api.send_message(token, chat_id, "Amount must be positive.")
        return

    from_account, to_account = tokens[1], tokens[2]
    if from_account not in ACCOUNTS:
        telegram_api.send_message(token, chat_id, f"Unknown account: {from_account}")
        return
    if to_account not in ACCOUNTS:
        telegram_api.send_message(token, chat_id, f"Unknown account: {to_account}")
        return
    if from_account == to_account:
        telegram_api.send_message(token, chat_id, "From and to accounts must differ.")
        return

    from_currency = ACCOUNTS[from_account][1]
    to_currency = ACCOUNTS[to_account][1]

    if from_currency == to_currency:
        category = "internal_transfer"
        from_minor_amount = to_minor(amount, from_currency)
        to_minor_amount = from_minor_amount
        rate_note = ""
    else:
        fx_rates = _load_fx_rates()
        if not fx_rates or not fx_rates.get(from_currency) or not fx_rates.get(to_currency):
            telegram_api.send_message(
                token,
                chat_id,
                f"Missing FX rates for {from_currency}/{to_currency}. Try again later.",
            )
            return
        category = "fx_exchange"
        from_minor_amount = to_minor(amount, from_currency)
        to_minor_amount = _convert_minor(from_minor_amount, from_currency, to_currency, fx_rates)
        converted_value = Decimal(to_minor_amount) / Decimal(MINOR_UNIT_FACTOR[to_currency])
        per_unit = converted_value / amount
        rate_note = f"\nRate: 1 {from_currency} = {per_unit:.4f} {to_currency}"

    category_display = CATEGORIES[category]["display_name"]

    now = datetime.now(UTC)
    ts = now.isoformat()
    out_tx_id = uuid4().hex[:12]
    in_tx_id = uuid4().hex[:12]
    out_sk = f"TX#{ts}#{out_tx_id}"
    in_sk = f"TX#{ts}#{in_tx_id}"

    out_tx = Transaction(
        tx_id=out_tx_id,
        date=now.strftime("%Y-%m-%d"),
        timestamp=ts,
        amount_minor=from_minor_amount,
        signed_amount_minor=-from_minor_amount,
        currency=from_currency,
        description=f"Transfer to {to_account}",
        category=category,
        category_display=category_display,
        source_account=from_account,
        mode="movement",
        tx_type="expense",
        paired_tx_sk=in_sk,
    )
    in_tx = Transaction(
        tx_id=in_tx_id,
        date=now.strftime("%Y-%m-%d"),
        timestamp=ts,
        amount_minor=to_minor_amount,
        signed_amount_minor=to_minor_amount,
        currency=to_currency,
        description=f"Transfer from {from_account}",
        category=category,
        category_display=category_display,
        source_account=to_account,
        mode="movement",
        tx_type="income",
        paired_tx_sk=out_sk,
    )

    update_id = message.get("message_id") or message.get("update_id", 0)
    ok = dynamodb.transfer(user_id, out_tx, in_tx, int(update_id))
    if not ok:
        telegram_api.send_message(token, chat_id, "Duplicate, already recorded.")
        return

    out_display = format_amount(from_minor_amount, from_currency)
    in_display = format_amount(to_minor_amount, to_currency)
    confirmation = f"Transfer recorded:\n-{out_display} from {from_account}\n+{in_display} to {to_account}{rate_note}"
    keyboard = telegram_api.build_keyboard([("Undo", f"undo:{out_sk}")])
    telegram_api.send_message_with_keyboard(token, chat_id, confirmation, keyboard)


# ---------------------------------------------------------------------------
# /search
# ---------------------------------------------------------------------------

_SEARCH_FLAGS = {"-c": "category", "-a": "account", "-d": "date", "-t": "tag"}

_SEARCH_USAGE = (
    "Usage: /search &lt;text&gt; [-c category] [-a account] [-d YYYY-MM or YYYY-MM-DD] [-t tag]\n\n"
    "Examples:\n"
    "  <code>/search coffee</code>\n"
    "  <code>/search -c groceries</code>\n"
    "  <code>/search -a bank_uah_1 -d 2026-03</code>\n"
    "  <code>/search -t italy2026 -t trip</code>\n"
    "  <code>/search netflix -c subscriptions</code>"
)

_SEARCH_RESULT_LIMIT = 20


def _parse_search_args(args: str) -> tuple[dict[str, str], str | None]:
    tokens = args.split()
    filters: dict[str, str] = {}
    text_parts: list[str] = []
    tags: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in _SEARCH_FLAGS:
            if i + 1 >= len(tokens):
                return {}, f"Missing value for {tok}"
            field = _SEARCH_FLAGS[tok]
            value = tokens[i + 1]
            if field == "tag":
                normalized = normalize_tag(value.lstrip("#"))
                if not normalized:
                    return {}, f"Invalid tag value: {value}"
                tags.append(normalized)
            else:
                filters[field] = value
            i += 2
        elif tok.startswith("-"):
            return {}, f"Unknown flag: {tok}\nSupported: -c (category), -a (account), -d (date), -t (tag)"
        else:
            text_parts.append(tok)
            i += 1

    if text_parts:
        filters["text"] = " ".join(text_parts)
    if tags:
        filters["tag"] = ",".join(sorted(set(tags)))

    if not filters:
        return {}, _SEARCH_USAGE

    if "category" in filters and filters["category"] not in CATEGORIES:
        valid = ", ".join(sorted(CATEGORIES))
        return {}, f"Unknown category: {filters['category']}\nValid: {valid}"

    if "account" in filters and filters["account"] not in ACCOUNTS:
        valid = ", ".join(sorted(ACCOUNTS))
        return {}, f"Unknown account: {filters['account']}\nValid: {valid}"

    if "date" in filters and not re.fullmatch(r"\d{4}-\d{2}(-\d{2})?", filters["date"]):
        return {}, "Date must be YYYY-MM or YYYY-MM-DD"

    return filters, None


def _match_transaction(tx: Transaction, filters: dict[str, str]) -> bool:
    if "category" in filters and tx.category != filters["category"]:
        return False
    if "account" in filters and tx.source_account != filters["account"]:
        return False
    if "date" in filters and not tx.date.startswith(filters["date"]):
        return False
    if "tag" in filters:
        wanted = set(filters["tag"].split(","))
        if not wanted & set(tx.tags):
            return False
    if "text" in filters:
        query = filters["text"].lower()
        searchable = f"{tx.description} {tx.category} {tx.category_display} {tx.source_account}".lower()
        if query not in searchable:
            return False
    return True


def handle_search(token: str, chat_id: int, user_id: int, text: str, message: dict[str, Any]) -> None:
    after_cmd = text.strip()
    if after_cmd.lower().startswith("/search"):
        after_cmd = after_cmd[7:].strip()

    if not after_cmd:
        telegram_api.send_message(token, chat_id, _SEARCH_USAGE)
        return

    filters, error = _parse_search_args(after_cmd)
    if error:
        telegram_api.send_message(token, chat_id, error)
        return

    transactions = dynamodb.get_all_transactions(user_id)
    matches = [tx for tx in transactions if _match_transaction(tx, filters)]

    total = len(matches)
    shown = matches[:_SEARCH_RESULT_LIMIT]
    result_text = formatters.format_search_results(shown, total, filters)
    telegram_api.send_message(token, chat_id, result_text)


# ---------------------------------------------------------------------------
# /cancel
# ---------------------------------------------------------------------------


def handle_cancel(token: str, chat_id: int, user_id: int, text: str, message: dict[str, Any]) -> None:
    dynamodb.delete_conv_state(user_id)
    telegram_api.send_message(token, chat_id, "Cancelled.")


# ---------------------------------------------------------------------------
# Quick-add (plain text, not a command)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Callback query handler
# ---------------------------------------------------------------------------


def _find_transaction_by_sk(
    tx_sk: str,
) -> tuple[str, str, str] | None:
    """Extract (tx_id, timestamp) from SK format TX#<timestamp>#<tx_id>."""
    parts = tx_sk.split("#", 2)
    if len(parts) != 3 or parts[0] != "TX":
        return None
    return parts[0], parts[1], parts[2]


def _stable_callback_id(callback_query_id: str) -> int:
    # Stable across Lambda invocations — handles transport retries only.
    digest = hashlib.sha1(callback_query_id.encode("utf-8")).hexdigest()
    return int(digest[:12], 16)


def _soft_delete_by_sk(
    user_id: int,
    tx_sk: str,
    callback_update_id: int,
) -> bool:
    parsed = _find_transaction_by_sk(tx_sk)
    if parsed is None:
        return False
    _, timestamp, tx_id = parsed

    tx = dynamodb.get_transaction_by_key(user_id, timestamp, tx_id)
    if tx is None:
        return False
    if tx.paired_tx_sk:
        return dynamodb.delete_paired_transaction(user_id, tx, callback_update_id)
    return dynamodb.delete_transaction(user_id, tx, callback_update_id)


def handle_callback(
    token: str,
    chat_id: int,
    user_id: int,
    callback_query_id: str,
    data: str,
    message: dict[str, Any],
) -> None:
    message_id = message.get("message_id", 0)

    if data.startswith("undo:"):
        tx_sk = data[5:]
        deleted = _soft_delete_by_sk(user_id, tx_sk, _stable_callback_id(callback_query_id))
        if deleted:
            telegram_api.answer_callback(token, callback_query_id, "Undone")
            telegram_api.edit_message(token, chat_id, int(message_id), "Undone.")
        else:
            telegram_api.answer_callback(token, callback_query_id, "Could not undo")

    elif data.startswith("confirm_del:"):
        tx_sk = data[12:]
        deleted = _soft_delete_by_sk(user_id, tx_sk, _stable_callback_id(callback_query_id))
        if deleted:
            telegram_api.answer_callback(token, callback_query_id, "Deleted")
            telegram_api.edit_message(token, chat_id, int(message_id), "Deleted.")
        else:
            telegram_api.answer_callback(token, callback_query_id, "Could not delete")

    elif data == "cancel_del":
        telegram_api.answer_callback(token, callback_query_id, "Cancelled")
        telegram_api.edit_message(token, chat_id, int(message_id), "Cancelled.")

    elif data.startswith("del:"):
        tx_sk = data[4:]
        # Show confirmation for the specific transaction
        parsed = _find_transaction_by_sk(tx_sk)
        if parsed is None:
            telegram_api.answer_callback(token, callback_query_id, "Transaction not found")
            return

        _, timestamp, tx_id = parsed
        target = dynamodb.get_transaction_by_key(user_id, timestamp, tx_id)

        telegram_api.answer_callback(token, callback_query_id)
        if target is None:
            telegram_api.send_message(token, chat_id, "Transaction not found.")
            return

        entry = formatters.format_confirmation(target)
        keyboard = telegram_api.build_keyboard(
            [
                ("Confirm delete", f"confirm_del:{tx_sk}"),
                ("Cancel", "cancel_del"),
            ]
        )
        telegram_api.send_message_with_keyboard(token, chat_id, f"Delete this?\n\n{entry}", keyboard)

    elif data.startswith("add:"):
        conversation.handle_add_callback(token, chat_id, user_id, callback_query_id, data, message)

    elif data.startswith("edit:"):
        conversation.handle_edit_callback(token, chat_id, user_id, callback_query_id, data, message)

    else:
        telegram_api.answer_callback(token, callback_query_id)

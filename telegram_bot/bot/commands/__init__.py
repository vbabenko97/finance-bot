"""Telegram command handlers.

Self-contained command groups live in submodules (help, rates, search, export,
entries) and are re-exported below so the public surface stays
``telegram_bot.bot.commands.<name>`` for handler.py and the tests.

The handlers kept in this module are the ones coupled to FX/time helpers that
tests patch on this namespace (``commands._fetch_fx_rates``,
``commands._load_fx_rates``, ``commands.datetime``): balance, portfolio, budget,
summary, transfer, recurring — plus the callback dispatcher.
"""

from __future__ import annotations

import calendar
import json
import logging
import re
import urllib.request
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from telegram_bot.bot import conversation, formatters, scheduler, telegram_api
from telegram_bot.bot.commands.entries import handle_cancel as handle_cancel
from telegram_bot.bot.commands.entries import handle_delete as handle_delete
from telegram_bot.bot.commands.entries import handle_edit as handle_edit
from telegram_bot.bot.commands.entries import handle_history as handle_history
from telegram_bot.bot.commands.entries import handle_income as handle_income
from telegram_bot.bot.commands.entries import handle_quick_add as handle_quick_add
from telegram_bot.bot.commands.entries import handle_set_balance as handle_set_balance
from telegram_bot.bot.commands.export import _build_export_csv as _build_export_csv
from telegram_bot.bot.commands.export import handle_export as handle_export
from telegram_bot.bot.commands.help import handle_help as handle_help
from telegram_bot.bot.commands.help import handle_start as handle_start
from telegram_bot.bot.commands.rates import handle_rates as handle_rates
from telegram_bot.bot.commands.search import _match_transaction as _match_transaction
from telegram_bot.bot.commands.search import _parse_search_args as _parse_search_args
from telegram_bot.bot.commands.search import handle_search as handle_search
from telegram_bot.bot.ids import stable_update_id
from telegram_bot.bot.quick_add import extract_tags
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
        try:
            date.fromisoformat(f"{after_cmd}-01")
        except ValueError:
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
    return stable_update_id(callback_query_id)


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


@dataclass
class CallbackContext:
    """Bundles the args shared by every callback-query handler.

    A scoped instance of the "BotContext" idea: introduced here (where it is
    low-risk and clearly useful) rather than across the ~25 command handlers,
    whose uniform positional signature is exercised directly by many tests.
    """

    token: str
    chat_id: int
    user_id: int
    callback_query_id: str
    data: str
    message: dict[str, Any]

    @property
    def message_id(self) -> int:
        return self.message.get("message_id", 0)


def _cb_undo(ctx: CallbackContext) -> None:
    tx_sk = ctx.data[len("undo:") :]
    deleted = _soft_delete_by_sk(ctx.user_id, tx_sk, _stable_callback_id(ctx.callback_query_id))
    if deleted:
        telegram_api.answer_callback(ctx.token, ctx.callback_query_id, "Undone")
        telegram_api.edit_message(ctx.token, ctx.chat_id, int(ctx.message_id), "Undone.")
    else:
        telegram_api.answer_callback(ctx.token, ctx.callback_query_id, "Could not undo")


def _cb_confirm_del(ctx: CallbackContext) -> None:
    tx_sk = ctx.data[len("confirm_del:") :]
    deleted = _soft_delete_by_sk(ctx.user_id, tx_sk, _stable_callback_id(ctx.callback_query_id))
    if deleted:
        telegram_api.answer_callback(ctx.token, ctx.callback_query_id, "Deleted")
        telegram_api.edit_message(ctx.token, ctx.chat_id, int(ctx.message_id), "Deleted.")
    else:
        telegram_api.answer_callback(ctx.token, ctx.callback_query_id, "Could not delete")


def _cb_cancel_del(ctx: CallbackContext) -> None:
    telegram_api.answer_callback(ctx.token, ctx.callback_query_id, "Cancelled")
    telegram_api.edit_message(ctx.token, ctx.chat_id, int(ctx.message_id), "Cancelled.")


def _cb_del(ctx: CallbackContext) -> None:
    tx_sk = ctx.data[len("del:") :]
    # Show confirmation for the specific transaction
    parsed = _find_transaction_by_sk(tx_sk)
    if parsed is None:
        telegram_api.answer_callback(ctx.token, ctx.callback_query_id, "Transaction not found")
        return

    _, timestamp, tx_id = parsed
    target = dynamodb.get_transaction_by_key(ctx.user_id, timestamp, tx_id)

    telegram_api.answer_callback(ctx.token, ctx.callback_query_id)
    if target is None:
        telegram_api.send_message(ctx.token, ctx.chat_id, "Transaction not found.")
        return

    entry = formatters.format_confirmation(target)
    keyboard = telegram_api.build_keyboard(
        [
            ("Confirm delete", f"confirm_del:{tx_sk}"),
            ("Cancel", "cancel_del"),
        ]
    )
    telegram_api.send_message_with_keyboard(ctx.token, ctx.chat_id, f"Delete this?\n\n{entry}", keyboard)


def _cb_add(ctx: CallbackContext) -> None:
    conversation.handle_add_callback(ctx.token, ctx.chat_id, ctx.user_id, ctx.callback_query_id, ctx.data, ctx.message)


def _cb_edit(ctx: CallbackContext) -> None:
    conversation.handle_edit_callback(ctx.token, ctx.chat_id, ctx.user_id, ctx.callback_query_id, ctx.data, ctx.message)


# Exact-match callbacks take priority; prefix matches are then tried in order.
_CALLBACK_EXACT_HANDLERS: dict[str, Callable[[CallbackContext], None]] = {
    "cancel_del": _cb_cancel_del,
}
_CALLBACK_PREFIX_HANDLERS: list[tuple[str, Callable[[CallbackContext], None]]] = [
    ("undo:", _cb_undo),
    ("confirm_del:", _cb_confirm_del),
    ("del:", _cb_del),
    ("add:", _cb_add),
    ("edit:", _cb_edit),
]


def _resolve_callback_handler(data: str) -> Callable[[CallbackContext], None] | None:
    handler = _CALLBACK_EXACT_HANDLERS.get(data)
    if handler is not None:
        return handler
    for prefix, prefix_handler in _CALLBACK_PREFIX_HANDLERS:
        if data.startswith(prefix):
            return prefix_handler
    return None


def handle_callback(
    token: str,
    chat_id: int,
    user_id: int,
    callback_query_id: str,
    data: str,
    message: dict[str, Any],
) -> None:
    handler = _resolve_callback_handler(data)
    if handler is None:
        telegram_api.answer_callback(token, callback_query_id)
        return
    handler(CallbackContext(token, chat_id, user_id, callback_query_id, data, message))

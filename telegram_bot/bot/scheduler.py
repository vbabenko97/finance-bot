from __future__ import annotations

import calendar
import hashlib
import logging
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from uuid import uuid4

from telegram_bot.bot import telegram_api
from telegram_bot.config.categories import CATEGORIES
from telegram_bot.storage import dynamodb
from telegram_bot.storage.models import RecurringTemplate, Transaction, from_minor

logger = logging.getLogger(__name__)

_PACE_OVERRUN_THRESHOLD = 1.10
CRON_HOUR_UTC = 6


def run_scheduled_tasks(bot_token: str, user_id: int, chat_id: int, today: date) -> None:
    book_due_recurring(bot_token, user_id, chat_id, today)
    send_pace_alerts(bot_token, user_id, chat_id, today)


# ---------------------------------------------------------------------------
# Recurring templates
# ---------------------------------------------------------------------------


def book_due_recurring(bot_token: str, user_id: int, chat_id: int, today: date) -> None:
    today_str = today.strftime("%Y-%m-%d")
    templates = dynamodb.get_all_recurring_templates(user_id)
    for tpl in templates:
        if not tpl.active:
            continue
        if tpl.next_run_date > today_str:
            continue

        update_id = _recurring_update_id(tpl.recur_id, tpl.next_run_date)
        tx = _build_tx_from_template(tpl, today)
        try:
            added = dynamodb.add_transaction(user_id, tx, update_id)
        except Exception:
            logger.exception("Failed to book recurring tpl=%s user=%d", tpl.recur_id, user_id)
            continue

        # No backfill: advance past today even if multiple periods elapsed.
        new_next = advance_to_future(tpl.next_run_date, tpl.schedule, tpl.schedule_day, today)
        tpl.next_run_date = new_next.strftime("%Y-%m-%d")
        dynamodb.put_recurring_template(user_id, tpl)

        if added:
            _notify_recurring_booked(bot_token, chat_id, tx, tpl)


def _build_tx_from_template(tpl: RecurringTemplate, today: date) -> Transaction:
    now = datetime.combine(today, time(6, 0), tzinfo=UTC)
    tx_id = uuid4().hex[:12]
    signed = tpl.amount_minor if tpl.tx_type == "income" else -tpl.amount_minor
    return Transaction(
        tx_id=tx_id,
        date=today.strftime("%Y-%m-%d"),
        timestamp=now.isoformat(),
        amount_minor=tpl.amount_minor,
        signed_amount_minor=signed,
        currency=tpl.currency,
        description=tpl.description,
        category=tpl.category,
        category_display=tpl.category_display,
        source_account=tpl.source_account,
        mode=tpl.mode,
        tx_type=tpl.tx_type,
        tags=list(tpl.tags),
        recur_id=tpl.recur_id,
    )


def _notify_recurring_booked(bot_token: str, chat_id: int, tx: Transaction, tpl: RecurringTemplate) -> None:
    prefix = "+" if tx.tx_type == "income" else "-"
    msg = (
        f"Booked recurring: {prefix}{tx.display_amount()} — {tx.description}\n"
        f"Account: {tx.source_account}\n"
        f"Template: {tpl.recur_id}"
    )
    tx_sk = f"TX#{tx.timestamp}#{tx.tx_id}"
    keyboard = telegram_api.build_keyboard([("Undo", f"undo:{tx_sk}")])
    telegram_api.send_message_with_keyboard(bot_token, chat_id, msg, keyboard)


def _recurring_update_id(recur_id: str, run_date: str) -> int:
    digest = hashlib.sha1(f"recur:{recur_id}:{run_date}".encode()).hexdigest()
    return int(digest[:12], 16)


def advance_one_period(current: date, schedule: str, schedule_day: int) -> date:
    if schedule == "daily":
        return current + timedelta(days=1)
    if schedule == "weekly":
        return current + timedelta(days=7)
    year, month = current.year, current.month + 1
    if month > 12:
        year += 1
        month = 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(schedule_day, last_day))


def advance_to_future(current_str: str, schedule: str, schedule_day: int, today: date) -> date:
    candidate = date.fromisoformat(current_str)
    while candidate <= today:
        candidate = advance_one_period(candidate, schedule, schedule_day)
    return candidate


def initial_next_run_date(today: date, schedule: str, schedule_day: int) -> date:
    if schedule == "daily":
        return today
    if schedule == "weekly":
        days_ahead = (schedule_day - today.weekday()) % 7
        return today + timedelta(days=days_ahead)
    last_day_this = calendar.monthrange(today.year, today.month)[1]
    actual_day = min(schedule_day, last_day_this)
    if today.day <= actual_day:
        return date(today.year, today.month, actual_day)
    year, month = today.year, today.month + 1
    if month > 12:
        year += 1
        month = 1
    last_day_next = calendar.monthrange(year, month)[1]
    return date(year, month, min(schedule_day, last_day_next))


def initial_next_run_for_now(now: datetime, schedule: str, schedule_day: int) -> date:
    today = now.date()
    candidate = initial_next_run_date(today, schedule, schedule_day)
    if candidate == today and now.hour >= CRON_HOUR_UTC:
        # Today's cron window has already passed; advance one period so the
        # template books on its next scheduled occurrence rather than tomorrow's
        # cron stamping it with the wrong date.
        candidate = advance_one_period(candidate, schedule, schedule_day)
    return candidate


# ---------------------------------------------------------------------------
# Pace alerts
# ---------------------------------------------------------------------------


def send_pace_alerts(bot_token: str, user_id: int, chat_id: int, today: date) -> None:
    fx_rates = _load_fx_rates()
    if not fx_rates or not fx_rates.get("EUR"):
        logger.warning("Pace alerts skipped: FX rates unavailable")
        return

    today_str = today.strftime("%Y-%m-%d")
    month = today_str[:7]
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    day = today.day

    budgets = dynamodb.get_all_budgets(user_id)
    if not budgets:
        return

    transactions = dynamodb.get_all_transactions(user_id)
    month_txs = [tx for tx in transactions if tx.mode == "consumption" and tx.date.startswith(month)]
    currencies_needed = {tx.currency for tx in month_txs} - {"EUR"}
    if any(not fx_rates.get(c) for c in currencies_needed):
        logger.warning("Pace alerts skipped: missing FX rates for some currencies")
        return

    spend_by_cat: dict[str, int] = defaultdict(int)
    for tx in month_txs:
        spend_by_cat[tx.category] += _to_eur_minor(tx.amount_minor, tx.currency, fx_rates)

    for cat, limit in budgets.items():
        if day <= 0 or limit <= 0:
            continue
        spent = spend_by_cat.get(cat, 0)
        projected = spent / day * days_in_month
        if projected <= limit * _PACE_OVERRUN_THRESHOLD:
            continue
        if not dynamodb.mark_alert_sent(user_id, today_str, cat):
            continue

        display = CATEGORIES.get(cat, {}).get("display_name", cat)
        spent_eur = from_minor(spent, "EUR")
        limit_eur = from_minor(limit, "EUR")
        projected_eur = from_minor(int(projected), "EUR")
        pct = spent / limit * 100
        msg = (
            f"⚠️ {display} ({cat}): {spent_eur:,.0f}/{limit_eur:,.0f} EUR ({pct:.0f}%) on day {day}"
            f" — projected {projected_eur:,.0f} EUR"
        )
        telegram_api.send_message(bot_token, chat_id, msg)


def _load_fx_rates() -> dict[str, float] | None:
    fx_rates = dynamodb.get_fx_rates()
    if fx_rates is None:
        from telegram_bot.bot.commands import _fetch_fx_rates

        fx_rates = _fetch_fx_rates()
        if fx_rates:
            dynamodb.cache_fx_rates(fx_rates)
    if not fx_rates:
        return None
    fx_rates.setdefault("USD", 1.0)
    fx_rates.setdefault("USDT", 1.0)
    return fx_rates


def _to_eur_minor(amount_minor: int, currency: str, fx_rates: dict[str, float]) -> int:
    if currency == "EUR":
        return amount_minor
    value = from_minor(amount_minor, currency)
    rate = Decimal(str(fx_rates[currency]))
    eur_rate = Decimal(str(fx_rates["EUR"]))
    eur = value / rate * eur_rate
    return round(eur * 100)

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import patch

from telegram_bot.bot import scheduler
from telegram_bot.storage import dynamodb
from telegram_bot.storage.models import RecurringTemplate, Transaction

USER_ID = 99

_FX_RATES: dict[str, float] = {
    "USD": 1.0,
    "USDT": 1.0,
    "UAH": 41.0,
    "EUR": 0.92,
}


def _make_template(
    recur_id: str = "rcr1",
    description: str = "Rent",
    amount_minor: int = 120_000,
    currency: str = "EUR",
    category: str = "home",
    category_display: str = "Дім",
    source_account: str = "bank_eur_2",
    mode: str = "consumption",
    tx_type: str = "expense",
    schedule: str = "monthly",
    schedule_day: int = 1,
    next_run_date: str = "2026-04-01",
    active: bool = True,
    tags: list[str] | None = None,
) -> RecurringTemplate:
    return RecurringTemplate(
        recur_id=recur_id,
        description=description,
        amount_minor=amount_minor,
        currency=currency,
        category=category,
        category_display=category_display,
        source_account=source_account,
        mode=mode,
        tx_type=tx_type,
        schedule=schedule,
        schedule_day=schedule_day,
        next_run_date=next_run_date,
        active=active,
        tags=tags or [],
    )


# ---------------------------------------------------------------------------
# Date arithmetic
# ---------------------------------------------------------------------------


class TestAdvancePeriod:
    def test_daily(self):
        result = scheduler.advance_one_period(date(2026, 4, 10), "daily", 0)
        assert result == date(2026, 4, 11)

    def test_weekly(self):
        result = scheduler.advance_one_period(date(2026, 4, 10), "weekly", 4)
        assert result == date(2026, 4, 17)

    def test_monthly_same_day(self):
        result = scheduler.advance_one_period(date(2026, 4, 15), "monthly", 15)
        assert result == date(2026, 5, 15)

    def test_monthly_year_boundary(self):
        result = scheduler.advance_one_period(date(2026, 12, 1), "monthly", 1)
        assert result == date(2027, 1, 1)

    def test_monthly_clamps_to_short_month(self):
        # Jan 31 -> Feb 28 (2026 is not leap)
        result = scheduler.advance_one_period(date(2026, 1, 31), "monthly", 31)
        assert result == date(2026, 2, 28)

    def test_monthly_clamps_to_february_leap_year(self):
        result = scheduler.advance_one_period(date(2028, 1, 31), "monthly", 31)
        assert result == date(2028, 2, 29)


class TestAdvanceToFuture:
    def test_advances_past_today_single_period(self):
        result = scheduler.advance_to_future("2026-04-01", "monthly", 1, date(2026, 4, 1))
        assert result == date(2026, 5, 1)

    def test_advances_through_multiple_missed_periods(self):
        # Cron missed several days; advance past today in one shot.
        result = scheduler.advance_to_future("2026-04-01", "daily", 0, date(2026, 4, 10))
        assert result == date(2026, 4, 11)

    def test_no_advance_when_already_future(self):
        # advance_to_future is only called for due templates; guard regardless.
        result = scheduler.advance_to_future("2026-05-01", "monthly", 1, date(2026, 4, 15))
        assert result == date(2026, 5, 1)


class TestInitialNextRunDate:
    def test_monthly_today_is_before_day(self):
        result = scheduler.initial_next_run_date(date(2026, 4, 10), "monthly", 15)
        assert result == date(2026, 4, 15)

    def test_monthly_today_is_after_day(self):
        result = scheduler.initial_next_run_date(date(2026, 4, 20), "monthly", 15)
        assert result == date(2026, 5, 15)

    def test_monthly_today_equals_day(self):
        result = scheduler.initial_next_run_date(date(2026, 4, 15), "monthly", 15)
        assert result == date(2026, 4, 15)

    def test_weekly(self):
        # Apr 10, 2026 is a Friday (weekday=4); ask for Tuesday (1)
        result = scheduler.initial_next_run_date(date(2026, 4, 10), "weekly", 1)
        assert result == date(2026, 4, 14)

    def test_daily(self):
        result = scheduler.initial_next_run_date(date(2026, 4, 10), "daily", 0)
        assert result == date(2026, 4, 10)


class TestInitialNextRunForNow:
    def test_today_before_cron_keeps_today(self):
        # 04:00 UTC, schedule_day == today.day → cron hasn't run yet
        result = scheduler.initial_next_run_for_now(datetime(2026, 4, 15, 4, 0, tzinfo=UTC), "monthly", 15)
        assert result == date(2026, 4, 15)

    def test_today_after_cron_advances_monthly(self):
        # 14:00 UTC, schedule_day == today.day → cron already passed
        result = scheduler.initial_next_run_for_now(datetime(2026, 4, 15, 14, 0, tzinfo=UTC), "monthly", 15)
        assert result == date(2026, 5, 15)

    def test_today_at_cron_hour_advances_daily(self):
        # 06:00 UTC exactly: cron has run for today's date by the time the
        # user finishes typing /recurring add, so advance to tomorrow.
        result = scheduler.initial_next_run_for_now(datetime(2026, 4, 15, 6, 0, tzinfo=UTC), "daily", 0)
        assert result == date(2026, 4, 16)

    def test_today_after_cron_advances_weekly(self):
        # 2026-04-15 is a Wednesday (weekday=2). schedule_day=2 (Wed), 14:00 UTC.
        result = scheduler.initial_next_run_for_now(datetime(2026, 4, 15, 14, 0, tzinfo=UTC), "weekly", 2)
        assert result == date(2026, 4, 22)

    def test_future_candidate_unaffected_by_hour(self):
        # 14:00 UTC on day 10, schedule_day=15 → candidate is the 15th, not
        # today, so cron hour is irrelevant.
        result = scheduler.initial_next_run_for_now(datetime(2026, 4, 10, 14, 0, tzinfo=UTC), "monthly", 15)
        assert result == date(2026, 4, 15)


# ---------------------------------------------------------------------------
# book_due_recurring
# ---------------------------------------------------------------------------


class TestBookDueRecurring:
    @patch("telegram_bot.bot.telegram_api.send_message_with_keyboard")
    def test_books_due_template_and_advances(self, mock_send_kb, dynamodb_table):
        tpl = _make_template(next_run_date="2026-04-01")
        dynamodb.put_recurring_template(USER_ID, tpl)

        scheduler.book_due_recurring("token", USER_ID, USER_ID, date(2026, 4, 1))

        # TX was booked.
        txs = dynamodb.get_transactions(USER_ID)
        assert len(txs) == 1
        assert txs[0].description == "Rent"
        assert txs[0].amount_minor == 120_000
        assert txs[0].signed_amount_minor == -120_000
        assert txs[0].recur_id == "rcr1"

        # Template advanced to next month, past today.
        stored = dynamodb.get_recurring_template(USER_ID, "rcr1")
        assert stored is not None
        assert stored.next_run_date == "2026-05-01"

        # User was notified via send_message_with_keyboard (Undo button).
        mock_send_kb.assert_called_once()
        args = mock_send_kb.call_args
        assert "Booked recurring" in args[0][2]
        keyboard = args[0][3]
        callback_data = keyboard[0][0]["callback_data"]
        assert callback_data.startswith("undo:TX#")

    @patch("telegram_bot.bot.telegram_api.send_message_with_keyboard")
    def test_skips_inactive_template(self, mock_send_kb, dynamodb_table):
        tpl = _make_template(next_run_date="2026-04-01", active=False)
        dynamodb.put_recurring_template(USER_ID, tpl)

        scheduler.book_due_recurring("token", USER_ID, USER_ID, date(2026, 4, 1))

        assert dynamodb.get_transactions(USER_ID) == []
        mock_send_kb.assert_not_called()
        # next_run_date unchanged
        stored = dynamodb.get_recurring_template(USER_ID, "rcr1")
        assert stored.next_run_date == "2026-04-01"

    @patch("telegram_bot.bot.telegram_api.send_message_with_keyboard")
    def test_skips_template_not_yet_due(self, mock_send_kb, dynamodb_table):
        tpl = _make_template(next_run_date="2026-04-15")
        dynamodb.put_recurring_template(USER_ID, tpl)

        scheduler.book_due_recurring("token", USER_ID, USER_ID, date(2026, 4, 10))

        assert dynamodb.get_transactions(USER_ID) == []
        mock_send_kb.assert_not_called()

    @patch("telegram_bot.bot.telegram_api.send_message_with_keyboard")
    def test_no_backfill_when_multiple_periods_missed(self, mock_send_kb, dynamodb_table):
        tpl = _make_template(schedule="daily", schedule_day=0, next_run_date="2026-04-01")
        dynamodb.put_recurring_template(USER_ID, tpl)

        scheduler.book_due_recurring("token", USER_ID, USER_ID, date(2026, 4, 10))

        # Only one transaction booked, even though 10 days elapsed.
        txs = dynamodb.get_transactions(USER_ID)
        assert len(txs) == 1

        # next_run_date jumped past today.
        stored = dynamodb.get_recurring_template(USER_ID, "rcr1")
        assert stored.next_run_date == "2026-04-11"

    @patch("telegram_bot.bot.telegram_api.send_message_with_keyboard")
    def test_idempotent_same_day_replay(self, mock_send_kb, dynamodb_table):
        tpl = _make_template(next_run_date="2026-04-01")
        dynamodb.put_recurring_template(USER_ID, tpl)

        scheduler.book_due_recurring("token", USER_ID, USER_ID, date(2026, 4, 1))
        # Simulate cron firing twice — reset next_run_date as if first run didn't persist
        # the advance, then re-run.
        tpl_reset = _make_template(next_run_date="2026-04-01")
        dynamodb.put_recurring_template(USER_ID, tpl_reset)

        scheduler.book_due_recurring("token", USER_ID, USER_ID, date(2026, 4, 1))

        # Still only one TX (UPD marker rejects the duplicate).
        txs = dynamodb.get_transactions(USER_ID)
        assert len(txs) == 1

    @patch("telegram_bot.bot.telegram_api.send_message_with_keyboard")
    def test_income_template_credits_balance(self, mock_send_kb, dynamodb_table):
        tpl = _make_template(
            recur_id="salary1",
            description="Salary",
            amount_minor=300_000,
            category="salary",
            category_display="Зарплатня",
            source_account="bank_eur_1",
            mode="income",
            tx_type="income",
            schedule="monthly",
            schedule_day=1,
            next_run_date="2026-04-01",
        )
        dynamodb.put_recurring_template(USER_ID, tpl)

        scheduler.book_due_recurring("token", USER_ID, USER_ID, date(2026, 4, 1))

        balances = {b.account_id: b.balance_minor for b in dynamodb.get_balances(USER_ID)}
        assert balances["bank_eur_1"] == 300_000


# ---------------------------------------------------------------------------
# send_pace_alerts
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


def _add_consumption(user_id: int, amount_minor: int, category: str, date_str: str, tx_id: str) -> None:
    tx = Transaction(
        tx_id=tx_id,
        date=date_str,
        timestamp=f"{date_str}T12:00:00+00:00",
        amount_minor=amount_minor,
        signed_amount_minor=-amount_minor,
        currency="EUR",
        description="Sample",
        category=category,
        category_display=category,
        source_account="bank_eur_2",
        mode="consumption",
        tx_type="expense",
    )
    dynamodb.add_transaction(user_id, tx, update_id=hash((tx_id, date_str)) & 0x7FFFFFFF)


class TestSendPaceAlerts:
    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_alert_fires_when_projection_exceeds_budget_by_more_than_10pct(self, mock_send, dynamodb_table):
        _stash_fx_rates(dynamodb_table)
        dynamodb.set_budget(USER_ID, "groceries", 35_000)  # 350 EUR
        # Day 10: spent 200 EUR -> projected 600 -> way over 350*1.1=385
        _add_consumption(USER_ID, 20_000, "groceries", "2026-04-05", "g1")

        scheduler.send_pace_alerts("token", USER_ID, USER_ID, date(2026, 4, 10))

        mock_send.assert_called_once()
        text = mock_send.call_args[0][2]
        assert "groceries" in text
        assert "projected" in text
        assert "⚠️" in text

    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_no_alert_when_within_threshold(self, mock_send, dynamodb_table):
        _stash_fx_rates(dynamodb_table)
        dynamodb.set_budget(USER_ID, "groceries", 35_000)
        # Day 15: spent 100 EUR -> projected 200 -> well under 350
        _add_consumption(USER_ID, 10_000, "groceries", "2026-04-05", "g1")

        scheduler.send_pace_alerts("token", USER_ID, USER_ID, date(2026, 4, 15))
        mock_send.assert_not_called()

    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_alert_deduplicated_same_day(self, mock_send, dynamodb_table):
        _stash_fx_rates(dynamodb_table)
        dynamodb.set_budget(USER_ID, "groceries", 35_000)
        _add_consumption(USER_ID, 20_000, "groceries", "2026-04-05", "g1")

        scheduler.send_pace_alerts("token", USER_ID, USER_ID, date(2026, 4, 10))
        scheduler.send_pace_alerts("token", USER_ID, USER_ID, date(2026, 4, 10))

        mock_send.assert_called_once()

    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_alert_re_fires_next_day(self, mock_send, dynamodb_table):
        _stash_fx_rates(dynamodb_table)
        dynamodb.set_budget(USER_ID, "groceries", 35_000)
        _add_consumption(USER_ID, 20_000, "groceries", "2026-04-05", "g1")

        scheduler.send_pace_alerts("token", USER_ID, USER_ID, date(2026, 4, 10))
        scheduler.send_pace_alerts("token", USER_ID, USER_ID, date(2026, 4, 11))

        assert mock_send.call_count == 2

    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_skips_when_no_budgets(self, mock_send, dynamodb_table):
        _stash_fx_rates(dynamodb_table)
        _add_consumption(USER_ID, 100_000, "groceries", "2026-04-05", "g1")

        scheduler.send_pace_alerts("token", USER_ID, USER_ID, date(2026, 4, 10))
        mock_send.assert_not_called()

    @patch("telegram_bot.bot.commands._fetch_fx_rates", return_value=None)
    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_skips_when_fx_unavailable(self, mock_send, _mock_fetch, dynamodb_table):
        dynamodb.set_budget(USER_ID, "groceries", 35_000)

        scheduler.send_pace_alerts("token", USER_ID, USER_ID, date(2026, 4, 10))
        mock_send.assert_not_called()

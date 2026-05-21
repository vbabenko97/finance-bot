from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

from telegram_bot.bot.commands import handle_recurring
from telegram_bot.storage import dynamodb
from telegram_bot.storage.models import RecurringTemplate

USER_ID = 99


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
        tags=[],
    )


# ---------------------------------------------------------------------------
# /recurring list
# ---------------------------------------------------------------------------


class TestRecurringList:
    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_empty(self, mock_send, dynamodb_table):
        handle_recurring("token", 123, USER_ID, "/recurring", {})
        mock_send.assert_called_once()
        assert "No recurring templates" in mock_send.call_args[0][2]

    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_renders_active_and_paused(self, mock_send, dynamodb_table):
        dynamodb.put_recurring_template(USER_ID, _make_template(recur_id="r1"))
        dynamodb.put_recurring_template(
            USER_ID,
            _make_template(recur_id="r2", description="Gym", active=False),
        )

        handle_recurring("token", 123, USER_ID, "/recurring", {})
        text = mock_send.call_args[0][2]

        assert "Recurring templates (2)" in text
        assert "r1" in text
        assert "r2" in text
        assert "Active" in text
        assert "Paused" in text


# ---------------------------------------------------------------------------
# /recurring add
# ---------------------------------------------------------------------------


class TestRecurringAdd:
    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_add_monthly_minimal(self, mock_send, dynamodb_table):
        with patch("telegram_bot.bot.commands.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 4, 10, tzinfo=UTC)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            handle_recurring("token", 123, USER_ID, "/recurring add monthly 15 1200 EUR home Rent", {})

        templates = dynamodb.get_all_recurring_templates(USER_ID)
        assert len(templates) == 1
        tpl = templates[0]
        assert tpl.schedule == "monthly"
        assert tpl.schedule_day == 15
        assert tpl.amount_minor == 120_000
        assert tpl.currency == "EUR"
        assert tpl.category == "home"
        assert tpl.description == "Rent"
        assert tpl.source_account == "bank_eur_2"  # DEFAULT_ACCOUNTS["EUR"]
        assert tpl.mode == "consumption"
        assert tpl.tx_type == "expense"
        assert tpl.next_run_date == "2026-04-15"
        mock_send.assert_called_once()
        assert "Added recurring template" in mock_send.call_args[0][2]

    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_add_with_account_override(self, mock_send, dynamodb_table):
        with patch("telegram_bot.bot.commands.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 4, 10, tzinfo=UTC)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            handle_recurring(
                "token",
                123,
                USER_ID,
                "/recurring add monthly 1 1200 EUR home Rent @cash_eur",
                {},
            )

        tpl = dynamodb.get_all_recurring_templates(USER_ID)[0]
        assert tpl.source_account == "cash_eur"

    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_add_account_currency_mismatch_rejected(self, mock_send, dynamodb_table):
        handle_recurring(
            "token",
            123,
            USER_ID,
            "/recurring add monthly 1 1200 EUR home Rent @bank_uah_1",
            {},
        )
        assert dynamodb.get_all_recurring_templates(USER_ID) == []
        assert "bank_uah_1" in mock_send.call_args[0][2]

    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_add_weekly(self, mock_send, dynamodb_table):
        with patch("telegram_bot.bot.commands.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 4, 10, tzinfo=UTC)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            # Apr 10, 2026 is Friday (weekday=4). Weekly schedule_day=1 (Tue).
            handle_recurring("token", 123, USER_ID, "/recurring add weekly 1 50 EUR groceries Weekly-shop", {})

        tpl = dynamodb.get_all_recurring_templates(USER_ID)[0]
        assert tpl.schedule == "weekly"
        assert tpl.schedule_day == 1
        assert tpl.next_run_date == "2026-04-14"  # next Tuesday

    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_add_daily(self, mock_send, dynamodb_table):
        with patch("telegram_bot.bot.commands.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 4, 10, tzinfo=UTC)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            handle_recurring("token", 123, USER_ID, "/recurring add daily 5 EUR transport metro", {})

        tpl = dynamodb.get_all_recurring_templates(USER_ID)[0]
        assert tpl.schedule == "daily"
        assert tpl.next_run_date == "2026-04-10"

    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_add_daily_after_cron_advances_to_tomorrow(self, mock_send, dynamodb_table):
        # Created at 14:00 UTC — past the 06:00 cron window. Today's cron has
        # already fired without this template; first booking must be tomorrow.
        with patch("telegram_bot.bot.commands.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 4, 10, 14, 0, tzinfo=UTC)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            handle_recurring("token", 123, USER_ID, "/recurring add daily 5 EUR transport metro", {})

        tpl = dynamodb.get_all_recurring_templates(USER_ID)[0]
        assert tpl.next_run_date == "2026-04-11"

    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_add_rejects_movement_category(self, mock_send, dynamodb_table):
        handle_recurring(
            "token",
            123,
            USER_ID,
            "/recurring add monthly 1 100 EUR internal_transfer X",
            {},
        )
        assert dynamodb.get_all_recurring_templates(USER_ID) == []
        assert "movement" in mock_send.call_args[0][2].lower()

    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_add_invalid_day_rejected(self, mock_send, dynamodb_table):
        handle_recurring("token", 123, USER_ID, "/recurring add monthly 32 100 EUR home Rent", {})
        assert dynamodb.get_all_recurring_templates(USER_ID) == []
        assert "1-31" in mock_send.call_args[0][2]

    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_add_strips_tags_from_description(self, mock_send, dynamodb_table):
        with patch("telegram_bot.bot.commands.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 4, 10, tzinfo=UTC)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            handle_recurring(
                "token",
                123,
                USER_ID,
                "/recurring add monthly 1 1200 EUR home Rent #fixed",
                {},
            )

        tpl = dynamodb.get_all_recurring_templates(USER_ID)[0]
        assert tpl.description == "Rent"
        assert tpl.tags == ["fixed"]

    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_add_income_category(self, mock_send, dynamodb_table):
        with patch("telegram_bot.bot.commands.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 4, 10, tzinfo=UTC)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            handle_recurring("token", 123, USER_ID, "/recurring add monthly 1 3000 EUR salary Salary", {})

        tpl = dynamodb.get_all_recurring_templates(USER_ID)[0]
        assert tpl.mode == "income"
        assert tpl.tx_type == "income"


# ---------------------------------------------------------------------------
# /recurring pause / resume / delete
# ---------------------------------------------------------------------------


class TestRecurringPauseResume:
    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_pause_then_resume_no_backfill(self, mock_send, dynamodb_table):
        # Template is due 2026-04-01, today is 2026-04-25 → resume must skip
        # the missed April run and advance to May 1.
        tpl = _make_template(recur_id="r1", next_run_date="2026-04-01", active=False)
        dynamodb.put_recurring_template(USER_ID, tpl)

        with patch("telegram_bot.bot.commands.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 4, 25, tzinfo=UTC)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            handle_recurring("token", 123, USER_ID, "/recurring resume r1", {})

        stored = dynamodb.get_recurring_template(USER_ID, "r1")
        assert stored.active is True
        assert stored.next_run_date == "2026-05-01"
        assert "resumed" in mock_send.call_args[0][2].lower()

    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_pause_active(self, mock_send, dynamodb_table):
        dynamodb.put_recurring_template(USER_ID, _make_template(recur_id="r1"))

        handle_recurring("token", 123, USER_ID, "/recurring pause r1", {})

        stored = dynamodb.get_recurring_template(USER_ID, "r1")
        assert stored.active is False
        assert "paused" in mock_send.call_args[0][2].lower()

    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_pause_already_paused(self, mock_send, dynamodb_table):
        dynamodb.put_recurring_template(USER_ID, _make_template(recur_id="r1", active=False))

        handle_recurring("token", 123, USER_ID, "/recurring pause r1", {})

        assert "Already paused" in mock_send.call_args[0][2]

    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_pause_unknown_id(self, mock_send, dynamodb_table):
        handle_recurring("token", 123, USER_ID, "/recurring pause nope", {})
        assert "No template" in mock_send.call_args[0][2]


class TestRecurringDelete:
    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_delete_existing(self, mock_send, dynamodb_table):
        dynamodb.put_recurring_template(USER_ID, _make_template(recur_id="r1"))

        handle_recurring("token", 123, USER_ID, "/recurring delete r1", {})

        assert dynamodb.get_recurring_template(USER_ID, "r1") is None
        assert "Deleted template" in mock_send.call_args[0][2]

    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_delete_unknown(self, mock_send, dynamodb_table):
        handle_recurring("token", 123, USER_ID, "/recurring delete missing", {})
        assert "No template" in mock_send.call_args[0][2]


# ---------------------------------------------------------------------------
# Usage / unknown subcommand
# ---------------------------------------------------------------------------


class TestRecurringUsage:
    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_unknown_subcommand_shows_usage(self, mock_send, dynamodb_table):
        handle_recurring("token", 123, USER_ID, "/recurring whatever", {})
        assert "Usage" in mock_send.call_args[0][2]

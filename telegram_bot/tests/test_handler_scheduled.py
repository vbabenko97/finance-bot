from __future__ import annotations

from unittest.mock import patch

import telegram_bot.handler as handler_mod
from telegram_bot.handler import lambda_handler


def _scheduled_event() -> dict:
    return {
        "version": "0",
        "id": "abc-123",
        "detail-type": "Scheduled Event",
        "source": "aws.events",
        "account": "123456789012",
        "time": "2026-04-15T06:00:00Z",
        "region": "eu-central-1",
        "resources": ["arn:aws:events:eu-central-1:123456789012:rule/finance-bot-daily-cron"],
        "detail": {},
    }


@patch("telegram_bot.bot.scheduler.run_scheduled_tasks")
@patch(f"{handler_mod.__name__}.ALLOWED_USER_ID", 12345)
@patch(f"{handler_mod.__name__}._get_bot_token", return_value="test-token-123")
def test_scheduled_event_runs_tasks(mock_bot_token, mock_run) -> None:
    result = lambda_handler(_scheduled_event(), None)

    assert result["statusCode"] == 200
    mock_run.assert_called_once()
    args, _ = mock_run.call_args
    assert args[0] == "test-token-123"
    assert args[1] == 12345
    assert args[2] == 12345


@patch("telegram_bot.bot.scheduler.run_scheduled_tasks")
@patch(f"{handler_mod.__name__}.ALLOWED_USER_ID", 12345)
@patch(f"{handler_mod.__name__}._get_webhook_secret")
@patch(f"{handler_mod.__name__}._get_bot_token", return_value="test-token-123")
def test_scheduled_event_branches_before_webhook_validation(mock_bot_token, mock_secret, mock_run) -> None:
    # Scheduled events have no headers and no body. Webhook validation would
    # 500 if it ran. The branch must short-circuit before that.
    lambda_handler(_scheduled_event(), None)

    mock_run.assert_called_once()
    mock_secret.assert_not_called()


@patch("telegram_bot.bot.scheduler.run_scheduled_tasks")
@patch(f"{handler_mod.__name__}.ALLOWED_USER_ID", 0)
@patch(f"{handler_mod.__name__}._get_bot_token", return_value="test-token-123")
def test_scheduled_event_without_user_id_returns_500(mock_bot_token, mock_run) -> None:
    result = lambda_handler(_scheduled_event(), None)
    assert result["statusCode"] == 500
    mock_run.assert_not_called()


@patch("telegram_bot.bot.scheduler.run_scheduled_tasks")
@patch(f"{handler_mod.__name__}._get_bot_token", return_value="")
def test_scheduled_event_without_bot_token_returns_500(mock_bot_token, mock_run) -> None:
    result = lambda_handler(_scheduled_event(), None)
    assert result["statusCode"] == 500
    mock_run.assert_not_called()

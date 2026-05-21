from __future__ import annotations

import json
from unittest.mock import patch

import telegram_bot.handler as handler_mod
from telegram_bot.handler import lambda_handler


def _webhook_event(
    body: dict,
    secret: str = "test-secret",
) -> dict:
    headers = {}
    if secret:
        headers["X-Telegram-Bot-Api-Secret-Token"] = secret
    return {
        "headers": headers,
        "body": json.dumps(body),
    }


def _message_body(
    text: str,
    user_id: int = 12345,
    chat_id: int = 12345,
    message_id: int = 100,
    update_id: int = 999,
) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "from": {"id": user_id},
            "chat": {"id": chat_id},
            "text": text,
        },
    }


@patch("telegram_bot.bot.telegram_api.send_message")
@patch(f"{handler_mod.__name__}.ALLOWED_USER_ID", 12345)
@patch(f"{handler_mod.__name__}._get_webhook_secret", return_value="test-secret")
@patch(f"{handler_mod.__name__}._get_bot_token", return_value="test-token-123")
def test_start_command(mock_bot_token, mock_webhook_secret, mock_send, dynamodb_table) -> None:
    body = _message_body("/start")
    event = _webhook_event(body)

    result = lambda_handler(event, None)

    assert result["statusCode"] == 200
    mock_send.assert_called_once()
    args = mock_send.call_args
    assert args[0][0] == "test-token-123"
    assert args[0][1] == 12345
    assert "Finance Bot" in args[0][2]
    assert "/help" in args[0][2]


@patch("telegram_bot.bot.telegram_api.send_message_with_keyboard")
@patch("telegram_bot.bot.telegram_api.send_message")
@patch(f"{handler_mod.__name__}.ALLOWED_USER_ID", 12345)
@patch(f"{handler_mod.__name__}._get_webhook_secret", return_value="test-secret")
@patch(f"{handler_mod.__name__}._get_bot_token", return_value="test-token-123")
def test_quick_add_message(mock_bot_token, mock_webhook_secret, mock_send, mock_send_kb, dynamodb_table) -> None:
    body = _message_body("150 Сільпо")
    event = _webhook_event(body)

    result = lambda_handler(event, None)

    assert result["statusCode"] == 200
    # Quick-add success sends confirmation with keyboard (undo button)
    mock_send_kb.assert_called_once()
    args = mock_send_kb.call_args
    text = args[0][2]
    assert "Added expense" in text
    assert "Сільпо" in text


@patch(f"{handler_mod.__name__}._get_webhook_secret", return_value="test-secret")
def test_webhook_secret_validation(mock_webhook_secret) -> None:
    body = _message_body("/start")
    event = _webhook_event(body, secret="wrong-secret")

    result = lambda_handler(event, None)

    assert result["statusCode"] == 403


@patch(f"{handler_mod.__name__}._get_webhook_secret", return_value="test-secret")
@patch(f"{handler_mod.__name__}._get_bot_token", return_value="test-token-123")
def test_webhook_invalid_json(mock_bot_token, mock_webhook_secret) -> None:
    event = {
        "headers": {"X-Telegram-Bot-Api-Secret-Token": "test-secret"},
        "body": "{bad json",
    }

    result = lambda_handler(event, None)

    assert result["statusCode"] == 400


@patch(f"{handler_mod.__name__}._get_webhook_secret", return_value="test-secret")
@patch(f"{handler_mod.__name__}._get_bot_token", return_value="test-token-123")
def test_webhook_missing_body(mock_bot_token, mock_webhook_secret) -> None:
    event = {
        "headers": {"X-Telegram-Bot-Api-Secret-Token": "test-secret"},
    }

    result = lambda_handler(event, None)

    assert result["statusCode"] == 400


@patch(f"{handler_mod.__name__}._get_webhook_secret", return_value="test-secret")
@patch(f"{handler_mod.__name__}._get_bot_token", return_value="test-token-123")
def test_webhook_unsupported_update_type(mock_bot_token, mock_webhook_secret) -> None:
    event = _webhook_event({"update_id": 999, "edited_message": {}})

    result = lambda_handler(event, None)

    assert result["statusCode"] == 400


@patch(f"{handler_mod.__name__}._get_webhook_secret", return_value="")
def test_webhook_secret_not_configured(mock_webhook_secret) -> None:
    body = _message_body("/start")
    event = _webhook_event(body)

    result = lambda_handler(event, None)

    assert result["statusCode"] == 500


@patch(f"{handler_mod.__name__}.ALLOWED_USER_ID", 12345)
@patch(f"{handler_mod.__name__}._get_webhook_secret", return_value="test-secret")
@patch(f"{handler_mod.__name__}._get_bot_token", return_value="")
def test_bot_token_not_configured(mock_bot_token, mock_webhook_secret) -> None:
    body = _message_body("/start")
    event = _webhook_event(body)

    result = lambda_handler(event, None)

    assert result["statusCode"] == 500


@patch("telegram_bot.bot.telegram_api.send_message")
def test_help_command(mock_send) -> None:
    from telegram_bot.bot import commands

    commands.handle_help("token", 12345, 12345, "/help", {})

    mock_send.assert_called_once()
    text = mock_send.call_args[0][2]
    assert "Finance Bot help" in text
    assert "/help - this help" in text


@patch("telegram_bot.bot.telegram_api.send_message")
@patch("telegram_bot.bot.telegram_api.send_message_with_keyboard")
@patch(f"{handler_mod.__name__}.ALLOWED_USER_ID", 12345)
@patch(f"{handler_mod.__name__}._get_webhook_secret", return_value="test-secret")
@patch(f"{handler_mod.__name__}._get_bot_token", return_value="test-token-123")
def test_unauthorized_user(mock_bot_token, mock_webhook_secret, mock_send_kb, mock_send, dynamodb_table) -> None:
    body = _message_body("/start", user_id=99999)
    event = _webhook_event(body)

    result = lambda_handler(event, None)

    assert result["statusCode"] == 200
    mock_send.assert_not_called()
    mock_send_kb.assert_not_called()

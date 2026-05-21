from __future__ import annotations

import functools
import json
import logging
import os
from collections.abc import Callable
from typing import Any

import boto3

from telegram_bot.bot import commands, conversation
from telegram_bot.storage import dynamodb

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))

CommandHandler = Callable[[str, int, int, str, dict[str, Any]], None]

_MAX_BODY_BYTES = 256_000


@functools.lru_cache(maxsize=1)
def _get_ssm_client() -> Any:
    return boto3.client("ssm")


@functools.cache
def _load_secret(secret_env: str, param_env: str) -> str:
    direct_value = os.environ.get(secret_env, "").strip()
    if direct_value and direct_value != "PLACEHOLDER":
        return direct_value

    parameter_name = os.environ.get(param_env, "").strip()
    if not parameter_name:
        return ""

    try:
        response = _get_ssm_client().get_parameter(Name=parameter_name, WithDecryption=True)
    except Exception:
        logger.exception("Failed to load secret from SSM: %s", parameter_name)
        return ""

    value = response.get("Parameter", {}).get("Value", "")
    return value.strip() if isinstance(value, str) else ""


def _get_bot_token() -> str:
    return _load_secret("TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN_PARAM")


def _get_webhook_secret() -> str:
    return _load_secret("WEBHOOK_SECRET_TOKEN", "WEBHOOK_SECRET_TOKEN_PARAM")


_COMMAND_HANDLERS: dict[str, CommandHandler] = {
    "/start": commands.handle_start,
    "/help": commands.handle_help,
    "/balance": commands.handle_balance,
    "/history": commands.handle_history,
    "/delete": commands.handle_delete,
    "/set_balance": commands.handle_set_balance,
    "/income": commands.handle_income,
    "/rates": commands.handle_rates,
    "/cancel": commands.handle_cancel,
    "/portfolio": commands.handle_portfolio,
    "/budget": commands.handle_budget,
    "/set_budget": commands.handle_set_budget,
    "/delete_budget": commands.handle_delete_budget,
    "/search": commands.handle_search,
    "/summary": commands.handle_summary,
    "/edit": commands.handle_edit,
    "/recurring": commands.handle_recurring,
    "/export": commands.handle_export,
    "/transfer": commands.handle_transfer,
}


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    # EventBridge scheduled invocations (lowercase event fields)
    if event.get("source") == "aws.events":
        return _handle_scheduled_event(event)

    # SNS alarm notifications (not from API Gateway)
    if "Records" in event:
        return _handle_sns_event(event)

    try:
        expected_secret = _get_webhook_secret()
        if not expected_secret:
            logger.error("Webhook secret is not configured")
            return {"statusCode": 500, "body": "Webhook secret is not configured"}

        headers = event.get("headers") or {}
        # API Gateway v2 lowercases header names
        secret = headers.get("x-telegram-bot-api-secret-token", "") or headers.get(
            "X-Telegram-Bot-Api-Secret-Token", ""
        )
        if not secret or secret != expected_secret:
            return {"statusCode": 403, "body": "Forbidden"}

        bot_token = _get_bot_token()
        if not bot_token:
            logger.error("Telegram bot token is not configured")
            return {"statusCode": 500, "body": "Bot token is not configured"}

        raw_body = event.get("body")
        if not isinstance(raw_body, str) or not raw_body.strip():
            return {"statusCode": 400, "body": "Missing request body"}
        if len(raw_body.encode("utf-8")) > _MAX_BODY_BYTES:
            return {"statusCode": 413, "body": "Payload too large"}

        try:
            body = json.loads(raw_body)
        except json.JSONDecodeError:
            return {"statusCode": 400, "body": "Invalid JSON"}
        if not isinstance(body, dict):
            return {"statusCode": 400, "body": "Invalid payload"}

        update_id = body.get("update_id", "")
        logger.info("Update %s", update_id)

        if "message" in body:
            _handle_message(body, bot_token)
        elif "callback_query" in body:
            _handle_callback_query(body, bot_token)
        else:
            return {"statusCode": 400, "body": "Unsupported update type"}

    except Exception:
        logger.exception("Error processing update")
        return {"statusCode": 500, "body": "Internal Server Error"}

    return {"statusCode": 200, "body": "OK"}


def _handle_message(body: dict[str, Any], bot_token: str) -> None:
    msg = body["message"]
    chat_id: int = msg["chat"]["id"]
    user_id: int = msg["from"]["id"]
    text: str = msg.get("text", "")

    if user_id != ALLOWED_USER_ID:
        return

    if not text.startswith("/"):
        state = dynamodb.get_conv_state(user_id)
        if state is not None:
            if state.step.startswith("EDIT_"):
                conversation.handle_edit_message(bot_token, chat_id, user_id, text, msg)
            else:
                conversation.handle_add_message(bot_token, chat_id, user_id, text, msg)
            return
        commands.handle_quick_add(bot_token, chat_id, user_id, text, msg)
        return

    cmd = text.split()[0].split("@")[0].lower()

    if cmd == "/add":
        conversation.handle_add_start(bot_token, chat_id, user_id)
        return

    handler = _COMMAND_HANDLERS.get(cmd)
    if handler is not None:
        handler(bot_token, chat_id, user_id, text, msg)


def _handle_callback_query(body: dict[str, Any], bot_token: str) -> None:
    cq = body["callback_query"]
    message = cq["message"]
    chat_id: int = message["chat"]["id"]
    user_id: int = cq["from"]["id"]
    callback_query_id: str = cq["id"]
    data: str = cq.get("data", "")

    if user_id != ALLOWED_USER_ID:
        return

    commands.handle_callback(bot_token, chat_id, user_id, callback_query_id, data, message)


def _handle_scheduled_event(event: dict[str, Any]) -> dict[str, Any]:
    from datetime import UTC, datetime

    from telegram_bot.bot import scheduler

    bot_token = _get_bot_token()
    if not bot_token:
        logger.error("Bot token not configured, cannot run scheduled tasks")
        return {"statusCode": 500, "body": "Bot token not configured"}
    if not ALLOWED_USER_ID:
        logger.error("ALLOWED_USER_ID not configured")
        return {"statusCode": 500, "body": "User not configured"}

    today = datetime.now(UTC).date()
    logger.info("Scheduled run for user=%d date=%s", ALLOWED_USER_ID, today.isoformat())
    scheduler.run_scheduled_tasks(bot_token, ALLOWED_USER_ID, ALLOWED_USER_ID, today)
    return {"statusCode": 200, "body": "OK"}


def _handle_sns_event(event: dict[str, Any]) -> dict[str, Any]:
    from telegram_bot.bot import telegram_api

    bot_token = _get_bot_token()
    if not bot_token:
        logger.error("Bot token not configured, cannot forward alarm")
        return {"statusCode": 500, "body": "Bot token not configured"}

    chat_id = ALLOWED_USER_ID

    for record in event.get("Records", []):
        sns = record.get("Sns", {})
        subject = sns.get("Subject", "CloudWatch Alarm")
        raw_message = sns.get("Message", "")

        try:
            alarm = json.loads(raw_message)
            name = alarm.get("AlarmName", "Unknown")
            state = alarm.get("NewStateValue", "Unknown")
            reason = alarm.get("NewStateReason", "")
            text = f"⚠️ <b>{name}</b>\nState: {state}\n{reason}"
        except (json.JSONDecodeError, AttributeError):
            text = f"⚠️ <b>{subject}</b>\n{raw_message[:500]}"

        telegram_api.send_message(bot_token, chat_id, text)
        logger.info("Forwarded alarm to Telegram: %s", subject)

    return {"statusCode": 200, "body": "OK"}

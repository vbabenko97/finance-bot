"""/search handler and its argument parsing."""

from __future__ import annotations

import re
from typing import Any

from telegram_bot.bot import formatters, telegram_api
from telegram_bot.bot.quick_add import normalize_tag
from telegram_bot.config.accounts import ACCOUNTS
from telegram_bot.config.categories import CATEGORIES
from telegram_bot.storage import dynamodb
from telegram_bot.storage.models import Transaction

_SEARCH_FLAGS = {"-c": "category", "-a": "account", "-d": "date", "-t": "tag"}

_SEARCH_USAGE = (
    "Usage: /search &lt;text&gt; [-c category] [-a account] [-d YYYY-MM or YYYY-MM-DD] [-t tag]\n\n"
    "Examples:\n"
    "  <code>/search coffee</code>\n"
    "  <code>/search -c groceries</code>\n"
    "  <code>/search -a bank_uah_1 -d 2026-03</code>\n"
    "  <code>/search -t trip2026 -t trip</code>\n"
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

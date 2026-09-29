"""Shared constants for the bot's operational scripts.

These scripts are excluded from the Lambda deploy zip; they run locally against
DynamoDB / xlsx files. Set the FINANCE_USER_ID env var to the owner's Telegram
user id before running them; the source carries only a non-PII placeholder so a
real account identifier is never committed.
"""

from __future__ import annotations

import os

# Placeholder fallback only — set FINANCE_USER_ID to operate on a real user's data.
DEFAULT_USER_ID = int(os.environ.get("FINANCE_USER_ID", "0"))


def require_user_id(user_id: int) -> int:
    """Guard against the placeholder id: operating on USER#0 is always a mistake."""
    if user_id <= 0:
        raise SystemExit(
            f"ERROR: no real user id configured (got {user_id}); refusing to operate on USER#{user_id}.\n"
            "Set FINANCE_USER_ID to your Telegram user id, or pass --user-id, e.g.:\n"
            "    FINANCE_USER_ID=123456789 python -m telegram_bot.scripts.export_snapshot"
        )
    return user_id


def user_pk(user_id: int = DEFAULT_USER_ID) -> str:
    """Single-table partition key for a user's items."""
    return f"USER#{user_id}"

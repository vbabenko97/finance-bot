from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from telegram_bot.config.accounts import ACCOUNTS, DEFAULT_ACCOUNTS
from telegram_bot.config.categories import CATEGORIES, infer_category
from telegram_bot.storage.models import MINOR_UNIT_FACTOR, Transaction, to_minor

_QUICK_ADD_RE = re.compile(r"^(\d+(?:[.,]\d{1,2})?)(?:\s+([A-Z]{3}))?\s+(.+)$")
_ACCOUNT_RE = re.compile(r"\s+@(\S+)\s*$")
_TAG_RE = re.compile(r"(?:^|(?<=\s))#([^\s#]+)")
_TAG_NORMALIZE_RE = re.compile(r"[^a-z0-9_-]")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_tag(raw: str) -> str:
    return _TAG_NORMALIZE_RE.sub("", raw.lower())


def extract_tags(text: str) -> tuple[str, list[str]]:
    tags: list[str] = []

    def _capture(match: re.Match[str]) -> str:
        normalized = normalize_tag(match.group(1))
        if normalized:
            tags.append(normalized)
        return ""

    cleaned = _TAG_RE.sub(_capture, text)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
    return cleaned, sorted(set(tags))


@dataclass(frozen=True)
class QuickAddParseResult:
    transaction: Transaction | None
    error_code: str | None = None


def parse_quick_add_detailed(text: str, tx_type: str = "expense") -> QuickAddParseResult:
    stripped = text.strip()
    if not stripped:
        return QuickAddParseResult(transaction=None, error_code="empty_input")

    # Extract optional @account suffix
    account_override = None
    acc_match = _ACCOUNT_RE.search(stripped)
    if acc_match:
        account_id = acc_match.group(1)
        if account_id not in ACCOUNTS:
            return QuickAddParseResult(transaction=None, error_code="unknown_account")
        account_override = account_id
        stripped = stripped[: acc_match.start()].rstrip()

    m = _QUICK_ADD_RE.match(stripped)
    if not m:
        if re.fullmatch(r"\d+(?:[.,]\d{1,2})?", stripped):
            return QuickAddParseResult(transaction=None, error_code="missing_description")
        amount_token = stripped.split(maxsplit=1)[0] if stripped else ""
        if amount_token and not re.fullmatch(r"\d+(?:[.,]\d{1,2})?", amount_token):
            return QuickAddParseResult(transaction=None, error_code="invalid_amount")
        return QuickAddParseResult(transaction=None, error_code="invalid_format")

    amount = Decimal(m.group(1).replace(",", "."))
    currency = m.group(2) or "EUR"
    description, tags = extract_tags(m.group(3))

    if currency not in MINOR_UNIT_FACTOR:
        return QuickAddParseResult(transaction=None, error_code="unsupported_currency")
    if not description:
        return QuickAddParseResult(transaction=None, error_code="missing_description")

    if account_override:
        # Override currency to match the account
        currency = ACCOUNTS[account_override][1]

    mode = "income" if tx_type == "income" else "consumption"
    category = infer_category(description, mode)
    category_display = CATEGORIES[category]["display_name"]
    source_account = account_override or DEFAULT_ACCOUNTS.get(currency, "")

    amount_minor = to_minor(amount, currency)
    signed_amount_minor = amount_minor if tx_type == "income" else -amount_minor

    now = datetime.now(UTC)
    tx_id = uuid4().hex[:12]

    return QuickAddParseResult(
        transaction=Transaction(
            tx_id=tx_id,
            date=now.strftime("%Y-%m-%d"),
            timestamp=now.isoformat(),
            amount_minor=amount_minor,
            signed_amount_minor=signed_amount_minor,
            currency=currency,
            description=description,
            category=category,
            category_display=category_display,
            source_account=source_account,
            mode=mode,
            tx_type=tx_type,
            tags=tags,
        )
    )


def parse_quick_add(text: str, tx_type: str = "expense") -> Transaction | None:
    return parse_quick_add_detailed(text, tx_type=tx_type).transaction

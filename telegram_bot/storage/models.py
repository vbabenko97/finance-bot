from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

MINOR_UNIT_FACTOR: dict[str, int] = {
    "UAH": 100,
    "USD": 100,
    "EUR": 100,
    "USDT": 100,
    "BTC": 100_000_000,
}

_DECIMAL_PLACES: dict[str, int] = {
    "UAH": 2,
    "USD": 2,
    "EUR": 2,
    "USDT": 2,
    "BTC": 8,
}


def to_minor(amount: Decimal, currency: str) -> int:
    factor = MINOR_UNIT_FACTOR[currency]
    return int(amount * factor)


def from_minor(amount_minor: int, currency: str) -> Decimal:
    factor = MINOR_UNIT_FACTOR[currency]
    return Decimal(amount_minor) / Decimal(factor)


def format_amount(amount_minor: int, currency: str) -> str:
    value = from_minor(amount_minor, currency)
    places = _DECIMAL_PLACES[currency]
    formatted = f"{value:.{places}f}"
    return f"{formatted} {currency}"


@dataclass
class Transaction:
    tx_id: str
    date: str
    timestamp: str
    amount_minor: int
    signed_amount_minor: int
    currency: str
    description: str
    category: str
    category_display: str
    subcategory: str = ""
    source_account: str = ""
    mode: str = "consumption"
    tx_type: str = "expense"
    deleted: bool = False
    tags: list[str] = field(default_factory=list)
    recur_id: str = ""
    paired_tx_sk: str = ""

    def to_item(self, user_id: int) -> dict[str, str | int | bool | list[str]]:
        item: dict[str, str | int | bool | list[str]] = {
            "PK": f"USER#{user_id}",
            "SK": f"TX#{self.timestamp}#{self.tx_id}",
            "tx_id": self.tx_id,
            "date": self.date,
            "timestamp": self.timestamp,
            "amount_minor": self.amount_minor,
            "signed_amount_minor": self.signed_amount_minor,
            "currency": self.currency,
            "description": self.description,
            "category": self.category,
            "category_display": self.category_display,
            "subcategory": self.subcategory,
            "source_account": self.source_account,
            "mode": self.mode,
            "tx_type": self.tx_type,
            "deleted": self.deleted,
        }
        if self.tags:
            item["tags"] = sorted(set(self.tags))
        if self.recur_id:
            item["recur_id"] = self.recur_id
        if self.paired_tx_sk:
            item["paired_tx_sk"] = self.paired_tx_sk
        return item

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> Transaction:
        raw_tags = item.get("tags")
        tags = sorted({str(t) for t in raw_tags if t}) if isinstance(raw_tags, set | list | tuple) else []
        return cls(
            tx_id=str(item["tx_id"]),
            date=str(item["date"]),
            timestamp=str(item["timestamp"]),
            amount_minor=int(item["amount_minor"]),
            signed_amount_minor=int(item["signed_amount_minor"]),
            currency=str(item["currency"]),
            description=str(item["description"]),
            category=str(item["category"]),
            category_display=str(item["category_display"]),
            subcategory=str(item.get("subcategory", "")),
            source_account=str(item.get("source_account", "")),
            mode=str(item.get("mode", "consumption")),
            tx_type=str(item.get("tx_type", "expense")),
            deleted=bool(item.get("deleted", False)),
            tags=tags,
            recur_id=str(item.get("recur_id", "")),
            paired_tx_sk=str(item.get("paired_tx_sk", "")),
        )

    def display_amount(self) -> str:
        return format_amount(abs(self.signed_amount_minor), self.currency)


@dataclass
class AccountBalance:
    account_id: str
    currency: str
    balance_minor: int
    last_updated: str

    def to_item(self, user_id: int) -> dict[str, str | int]:
        return {
            "PK": f"USER#{user_id}",
            "SK": f"BAL#{self.account_id}",
            "account_id": self.account_id,
            "currency": self.currency,
            "balance_minor": self.balance_minor,
            "last_updated": self.last_updated,
        }

    @classmethod
    def from_item(cls, item: dict[str, str | int]) -> AccountBalance:
        return cls(
            account_id=str(item["account_id"]),
            currency=str(item["currency"]),
            balance_minor=int(item["balance_minor"]),
            last_updated=str(item["last_updated"]),
        )

    def display_balance(self) -> str:
        return format_amount(self.balance_minor, self.currency)


@dataclass
class RecurringTemplate:
    recur_id: str
    description: str
    amount_minor: int
    currency: str
    category: str
    category_display: str
    source_account: str
    mode: str  # "consumption" or "income"
    tx_type: str  # "expense" or "income"
    schedule: str  # "daily" | "weekly" | "monthly"
    schedule_day: int  # 0 for daily; 0-6 for weekly (Mon-Sun); 1-31 for monthly
    next_run_date: str  # YYYY-MM-DD
    active: bool = True
    tags: list[str] = field(default_factory=list)

    def to_item(self, user_id: int) -> dict[str, str | int | bool | list[str]]:
        item: dict[str, str | int | bool | list[str]] = {
            "PK": f"USER#{user_id}",
            "SK": f"RECUR#{self.recur_id}",
            "recur_id": self.recur_id,
            "description": self.description,
            "amount_minor": self.amount_minor,
            "currency": self.currency,
            "category": self.category,
            "category_display": self.category_display,
            "source_account": self.source_account,
            "mode": self.mode,
            "tx_type": self.tx_type,
            "schedule": self.schedule,
            "schedule_day": self.schedule_day,
            "next_run_date": self.next_run_date,
            "active": self.active,
        }
        if self.tags:
            item["tags"] = sorted(set(self.tags))
        return item

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> RecurringTemplate:
        raw_tags = item.get("tags")
        tags = sorted({str(t) for t in raw_tags if t}) if isinstance(raw_tags, set | list | tuple) else []
        return cls(
            recur_id=str(item["recur_id"]),
            description=str(item["description"]),
            amount_minor=int(item["amount_minor"]),
            currency=str(item["currency"]),
            category=str(item["category"]),
            category_display=str(item["category_display"]),
            source_account=str(item["source_account"]),
            mode=str(item["mode"]),
            tx_type=str(item["tx_type"]),
            schedule=str(item["schedule"]),
            schedule_day=int(item["schedule_day"]),
            next_run_date=str(item["next_run_date"]),
            active=bool(item.get("active", True)),
            tags=tags,
        )


@dataclass
class ConversationState:
    step: str
    data: dict[str, object] = field(default_factory=dict)
    updated_at: str = ""
    ttl: int = 0

    def to_item(self, user_id: int) -> dict[str, str | int | dict[str, object]]:
        return {
            "PK": f"USER#{user_id}",
            "SK": "CONV",
            "step": self.step,
            "data": self.data,
            "updated_at": self.updated_at,
            "ttl": self.ttl,
        }

    @classmethod
    def from_item(cls, item: dict[str, str | int | dict[str, object]]) -> ConversationState:
        data_raw = item.get("data", {})
        data: dict[str, object] = data_raw if isinstance(data_raw, dict) else {}
        return cls(
            step=str(item["step"]),
            data=data,
            updated_at=str(item.get("updated_at", "")),
            ttl=int(item.get("ttl", 0)),
        )

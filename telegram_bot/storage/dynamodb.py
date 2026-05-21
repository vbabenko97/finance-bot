from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import boto3

from telegram_bot.storage.models import (
    AccountBalance,
    ConversationState,
    RecurringTemplate,
    Transaction,
)

logger = logging.getLogger(__name__)

_TABLE_NAME = os.environ.get("DYNAMODB_TABLE_NAME", "finance-bot")

_table: Any = None
_client: Any = None


def _get_table() -> Any:
    global _table
    if _table is None:
        resource = boto3.resource("dynamodb")
        _table = resource.Table(_TABLE_NAME)
    return _table


def _get_client() -> Any:
    global _client
    if _client is None:
        _client = boto3.client("dynamodb")
    return _client


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _s(value: str) -> dict[str, str]:
    return {"S": value}


def _n(value: int) -> dict[str, str]:
    return {"N": str(value)}


def _bool(value: bool) -> dict[str, bool]:
    return {"BOOL": value}


def _item_to_dynamo(item: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for key, value in item.items():
        if isinstance(value, str):
            result[key] = _s(value)
        elif isinstance(value, bool):
            result[key] = _bool(value)
        elif isinstance(value, int):
            result[key] = _n(value)
        elif isinstance(value, list | set):
            members = sorted({v for v in value if isinstance(v, str) and v})
            if members:
                result[key] = {"SS": members}
    return result


def add_transaction(user_id: int, tx: Transaction, update_id: int) -> bool:
    client = _get_client()
    now = _now_iso()
    now_epoch = int(time.time())
    tx_item = _item_to_dynamo(tx.to_item(user_id))

    try:
        client.transact_write_items(
            TransactItems=[
                {
                    "Put": {
                        "TableName": _TABLE_NAME,
                        "Item": tx_item,
                    },
                },
                {
                    "Update": {
                        "TableName": _TABLE_NAME,
                        "Key": {
                            "PK": _s(f"USER#{user_id}"),
                            "SK": _s(f"BAL#{tx.source_account}"),
                        },
                        "UpdateExpression": (
                            "ADD balance_minor :amt SET currency = :cur, last_updated = :ts, account_id = :aid"
                        ),
                        "ExpressionAttributeValues": {
                            ":amt": _n(tx.signed_amount_minor),
                            ":cur": _s(tx.currency),
                            ":ts": _s(now),
                            ":aid": _s(tx.source_account),
                        },
                    },
                },
                {
                    "Put": {
                        "TableName": _TABLE_NAME,
                        "Item": {
                            "PK": _s(f"USER#{user_id}"),
                            "SK": _s(f"UPD#{update_id}"),
                            "processed_at": _s(now),
                            "ttl": _n(now_epoch + 86400),
                        },
                        "ConditionExpression": "attribute_not_exists(PK)",
                    },
                },
            ],
        )
    except client.exceptions.TransactionCanceledException as exc:
        reasons = exc.response.get("CancellationReasons", [])
        for reason in reasons:
            if reason.get("Code") == "ConditionalCheckFailed":
                logger.info("Duplicate update_id=%d for user=%d", update_id, user_id)
                return False
        raise
    return True


def get_transactions(user_id: int, limit: int = 10) -> list[Transaction]:
    table = _get_table()
    response = table.query(
        KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
        ExpressionAttributeValues={
            ":pk": f"USER#{user_id}",
            ":prefix": "TX#",
        },
        ScanIndexForward=False,
        Limit=limit,
    )
    items: list[dict[str, Any]] = response.get("Items", [])
    return [Transaction.from_item(item) for item in items if not item.get("deleted", False)]


def get_transaction_by_key(user_id: int, timestamp: str, tx_id: str) -> Transaction | None:
    table = _get_table()
    response = table.get_item(
        Key={
            "PK": f"USER#{user_id}",
            "SK": f"TX#{timestamp}#{tx_id}",
        },
    )
    item = response.get("Item")
    if item is None or item.get("deleted", False):
        return None
    return Transaction.from_item(item)


def transfer(user_id: int, out_tx: Transaction, in_tx: Transaction, update_id: int) -> bool:
    client = _get_client()
    now = _now_iso()
    now_epoch = int(time.time())
    out_item = _item_to_dynamo(out_tx.to_item(user_id))
    in_item = _item_to_dynamo(in_tx.to_item(user_id))

    try:
        client.transact_write_items(
            TransactItems=[
                {"Put": {"TableName": _TABLE_NAME, "Item": out_item}},
                {"Put": {"TableName": _TABLE_NAME, "Item": in_item}},
                {
                    "Update": {
                        "TableName": _TABLE_NAME,
                        "Key": {
                            "PK": _s(f"USER#{user_id}"),
                            "SK": _s(f"BAL#{out_tx.source_account}"),
                        },
                        "UpdateExpression": (
                            "ADD balance_minor :amt SET currency = :cur, last_updated = :ts, account_id = :aid"
                        ),
                        "ExpressionAttributeValues": {
                            ":amt": _n(out_tx.signed_amount_minor),
                            ":cur": _s(out_tx.currency),
                            ":ts": _s(now),
                            ":aid": _s(out_tx.source_account),
                        },
                    },
                },
                {
                    "Update": {
                        "TableName": _TABLE_NAME,
                        "Key": {
                            "PK": _s(f"USER#{user_id}"),
                            "SK": _s(f"BAL#{in_tx.source_account}"),
                        },
                        "UpdateExpression": (
                            "ADD balance_minor :amt SET currency = :cur, last_updated = :ts, account_id = :aid"
                        ),
                        "ExpressionAttributeValues": {
                            ":amt": _n(in_tx.signed_amount_minor),
                            ":cur": _s(in_tx.currency),
                            ":ts": _s(now),
                            ":aid": _s(in_tx.source_account),
                        },
                    },
                },
                {
                    "Put": {
                        "TableName": _TABLE_NAME,
                        "Item": {
                            "PK": _s(f"USER#{user_id}"),
                            "SK": _s(f"UPD#{update_id}"),
                            "processed_at": _s(now),
                            "ttl": _n(now_epoch + 86400),
                        },
                        "ConditionExpression": "attribute_not_exists(PK)",
                    },
                },
            ],
        )
    except client.exceptions.TransactionCanceledException as exc:
        reasons = exc.response.get("CancellationReasons", [])
        for reason in reasons:
            if reason.get("Code") == "ConditionalCheckFailed":
                logger.info("Duplicate update_id=%d for user=%d transfer", update_id, user_id)
                return False
        raise
    return True


def delete_paired_transaction(user_id: int, tx: Transaction, update_id: int) -> bool:
    if not tx.paired_tx_sk:
        raise ValueError("delete_paired_transaction requires tx with paired_tx_sk set")

    parts = tx.paired_tx_sk.split("#", 2)
    if len(parts) != 3 or parts[0] != "TX":
        raise ValueError(f"Invalid paired_tx_sk format: {tx.paired_tx_sk}")
    paired_timestamp, paired_tx_id = parts[1], parts[2]

    paired = get_transaction_by_key(user_id, paired_timestamp, paired_tx_id)
    if paired is None:
        logger.warning(
            "Paired leg missing or deleted for tx=%s; falling back to single delete",
            tx.tx_id,
        )
        return delete_transaction(user_id, tx, update_id)

    client = _get_client()
    now = _now_iso()
    now_epoch = int(time.time())

    try:
        client.transact_write_items(
            TransactItems=[
                {
                    "Update": {
                        "TableName": _TABLE_NAME,
                        "Key": {
                            "PK": _s(f"USER#{user_id}"),
                            "SK": _s(f"TX#{tx.timestamp}#{tx.tx_id}"),
                        },
                        "UpdateExpression": "SET deleted = :t",
                        "ConditionExpression": (
                            "attribute_exists(SK) AND (attribute_not_exists(deleted) OR deleted = :f)"
                        ),
                        "ExpressionAttributeValues": {
                            ":t": _bool(True),
                            ":f": _bool(False),
                        },
                    },
                },
                {
                    "Update": {
                        "TableName": _TABLE_NAME,
                        "Key": {
                            "PK": _s(f"USER#{user_id}"),
                            "SK": _s(f"TX#{paired.timestamp}#{paired.tx_id}"),
                        },
                        "UpdateExpression": "SET deleted = :t",
                        "ConditionExpression": (
                            "attribute_exists(SK) AND (attribute_not_exists(deleted) OR deleted = :f)"
                        ),
                        "ExpressionAttributeValues": {
                            ":t": _bool(True),
                            ":f": _bool(False),
                        },
                    },
                },
                {
                    "Update": {
                        "TableName": _TABLE_NAME,
                        "Key": {
                            "PK": _s(f"USER#{user_id}"),
                            "SK": _s(f"BAL#{tx.source_account}"),
                        },
                        "UpdateExpression": "ADD balance_minor :amt",
                        "ExpressionAttributeValues": {":amt": _n(-tx.signed_amount_minor)},
                    },
                },
                {
                    "Update": {
                        "TableName": _TABLE_NAME,
                        "Key": {
                            "PK": _s(f"USER#{user_id}"),
                            "SK": _s(f"BAL#{paired.source_account}"),
                        },
                        "UpdateExpression": "ADD balance_minor :amt",
                        "ExpressionAttributeValues": {":amt": _n(-paired.signed_amount_minor)},
                    },
                },
                {
                    "Put": {
                        "TableName": _TABLE_NAME,
                        "Item": {
                            "PK": _s(f"USER#{user_id}"),
                            "SK": _s(f"UPD#{update_id}"),
                            "processed_at": _s(now),
                            "ttl": _n(now_epoch + 86400),
                        },
                        "ConditionExpression": "attribute_not_exists(PK)",
                    },
                },
            ],
        )
    except client.exceptions.TransactionCanceledException as exc:
        reasons = exc.response.get("CancellationReasons", [])
        for index, reason in enumerate(reasons):
            if reason.get("Code") != "ConditionalCheckFailed":
                continue
            if index in (0, 1):
                logger.info(
                    "Paired delete rejected (leg already deleted) tx=%s user=%d",
                    tx.tx_id,
                    user_id,
                )
            elif index == 4:
                logger.info("Duplicate update_id=%d for user=%d", update_id, user_id)
            return False
        raise
    return True


def delete_transaction(user_id: int, tx: Transaction, update_id: int) -> bool:
    client = _get_client()
    now = _now_iso()
    now_epoch = int(time.time())
    tx_key = {
        "PK": _s(f"USER#{user_id}"),
        "SK": _s(f"TX#{tx.timestamp}#{tx.tx_id}"),
    }

    try:
        client.transact_write_items(
            TransactItems=[
                {
                    "Update": {
                        "TableName": _TABLE_NAME,
                        "Key": tx_key,
                        "UpdateExpression": "SET deleted = :t",
                        "ConditionExpression": "attribute_exists(SK) AND (attribute_not_exists(deleted) OR deleted = :f)",
                        "ExpressionAttributeValues": {
                            ":t": _bool(True),
                            ":f": _bool(False),
                        },
                    },
                },
                {
                    "Update": {
                        "TableName": _TABLE_NAME,
                        "Key": {
                            "PK": _s(f"USER#{user_id}"),
                            "SK": _s(f"BAL#{tx.source_account}"),
                        },
                        "UpdateExpression": "ADD balance_minor :amt",
                        "ExpressionAttributeValues": {
                            ":amt": _n(-tx.signed_amount_minor),
                        },
                    },
                },
                {
                    "Put": {
                        "TableName": _TABLE_NAME,
                        "Item": {
                            "PK": _s(f"USER#{user_id}"),
                            "SK": _s(f"UPD#{update_id}"),
                            "processed_at": _s(now),
                            "ttl": _n(now_epoch + 86400),
                        },
                        "ConditionExpression": "attribute_not_exists(PK)",
                    },
                },
            ],
        )
    except client.exceptions.TransactionCanceledException as exc:
        reasons = exc.response.get("CancellationReasons", [])
        for index, reason in enumerate(reasons):
            if reason.get("Code") != "ConditionalCheckFailed":
                continue
            if index == 0:
                logger.info("Soft-delete rejected (already deleted) tx=%s user=%d", tx.tx_id, user_id)
            elif index == 2:
                logger.info("Duplicate update_id=%d for user=%d", update_id, user_id)
            return False
        raise
    return True


def update_transaction(user_id: int, old_tx: Transaction, new_tx: Transaction, update_id: int) -> bool:
    if old_tx.source_account == new_tx.source_account and old_tx.currency != new_tx.currency:
        raise ValueError("Cannot change currency without changing account: accounts are currency-bound")

    client = _get_client()
    now = _now_iso()
    now_epoch = int(time.time())
    tx_key = {
        "PK": _s(f"USER#{user_id}"),
        "SK": _s(f"TX#{old_tx.timestamp}#{old_tx.tx_id}"),
    }

    set_parts = [
        "amount_minor = :amt",
        "signed_amount_minor = :sam",
        "currency = :cur",
        "description = :desc",
        "category = :cat",
        "category_display = :cd",
        "source_account = :acc",
    ]
    values: dict[str, dict[str, Any]] = {
        ":amt": _n(new_tx.amount_minor),
        ":sam": _n(new_tx.signed_amount_minor),
        ":cur": _s(new_tx.currency),
        ":desc": _s(new_tx.description),
        ":cat": _s(new_tx.category),
        ":cd": _s(new_tx.category_display),
        ":acc": _s(new_tx.source_account),
        ":old_sam": _n(old_tx.signed_amount_minor),
        ":old_acc": _s(old_tx.source_account),
        ":f": _bool(False),
    }
    remove_parts: list[str] = []
    if new_tx.tags:
        set_parts.append("tags = :tags")
        values[":tags"] = {"SS": sorted(set(new_tx.tags))}
    else:
        remove_parts.append("tags")

    update_expression = "SET " + ", ".join(set_parts)
    if remove_parts:
        update_expression += " REMOVE " + ", ".join(remove_parts)

    transact_items: list[dict[str, Any]] = [
        {
            "Update": {
                "TableName": _TABLE_NAME,
                "Key": tx_key,
                "UpdateExpression": update_expression,
                "ConditionExpression": (
                    "attribute_exists(SK) AND (attribute_not_exists(deleted) OR deleted = :f)"
                    " AND signed_amount_minor = :old_sam AND source_account = :old_acc"
                ),
                "ExpressionAttributeValues": values,
            },
        },
    ]

    if old_tx.source_account == new_tx.source_account:
        if old_tx.signed_amount_minor != new_tx.signed_amount_minor:
            transact_items.append(
                {
                    "Update": {
                        "TableName": _TABLE_NAME,
                        "Key": {
                            "PK": _s(f"USER#{user_id}"),
                            "SK": _s(f"BAL#{old_tx.source_account}"),
                        },
                        "UpdateExpression": "ADD balance_minor :delta",
                        "ExpressionAttributeValues": {
                            ":delta": _n(new_tx.signed_amount_minor - old_tx.signed_amount_minor),
                        },
                    },
                },
            )
    else:
        transact_items.append(
            {
                "Update": {
                    "TableName": _TABLE_NAME,
                    "Key": {
                        "PK": _s(f"USER#{user_id}"),
                        "SK": _s(f"BAL#{old_tx.source_account}"),
                    },
                    "UpdateExpression": "ADD balance_minor :neg",
                    "ExpressionAttributeValues": {
                        ":neg": _n(-old_tx.signed_amount_minor),
                    },
                },
            },
        )
        transact_items.append(
            {
                "Update": {
                    "TableName": _TABLE_NAME,
                    "Key": {
                        "PK": _s(f"USER#{user_id}"),
                        "SK": _s(f"BAL#{new_tx.source_account}"),
                    },
                    "UpdateExpression": (
                        "ADD balance_minor :new_sam SET currency = :new_cur, last_updated = :ts, account_id = :new_acc"
                    ),
                    "ExpressionAttributeValues": {
                        ":new_sam": _n(new_tx.signed_amount_minor),
                        ":new_cur": _s(new_tx.currency),
                        ":ts": _s(now),
                        ":new_acc": _s(new_tx.source_account),
                    },
                },
            },
        )

    upd_index = len(transact_items)
    transact_items.append(
        {
            "Put": {
                "TableName": _TABLE_NAME,
                "Item": {
                    "PK": _s(f"USER#{user_id}"),
                    "SK": _s(f"UPD#{update_id}"),
                    "processed_at": _s(now),
                    "ttl": _n(now_epoch + 86400),
                },
                "ConditionExpression": "attribute_not_exists(PK)",
            },
        },
    )

    try:
        client.transact_write_items(TransactItems=transact_items)
    except client.exceptions.TransactionCanceledException as exc:
        reasons = exc.response.get("CancellationReasons", [])
        for index, reason in enumerate(reasons):
            if reason.get("Code") != "ConditionalCheckFailed":
                continue
            if index == 0:
                logger.info(
                    "Update rejected (pre-state mismatch or deleted) tx=%s user=%d",
                    old_tx.tx_id,
                    user_id,
                )
            elif index == upd_index:
                logger.info("Duplicate update_id=%d for user=%d", update_id, user_id)
            return False
        raise
    return True


def get_balances(user_id: int) -> list[AccountBalance]:
    table = _get_table()
    response = table.query(
        KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
        ExpressionAttributeValues={
            ":pk": f"USER#{user_id}",
            ":prefix": "BAL#",
        },
    )
    items: list[dict[str, Any]] = response.get("Items", [])
    return [AccountBalance.from_item(item) for item in items]


def set_balance(user_id: int, account_id: str, balance_minor: int, currency: str) -> None:
    table = _get_table()
    now = _now_iso()
    table.put_item(
        Item={
            "PK": f"USER#{user_id}",
            "SK": f"BAL#{account_id}",
            "account_id": account_id,
            "currency": currency,
            "balance_minor": balance_minor,
            "last_updated": now,
        },
    )


def get_conv_state(user_id: int) -> ConversationState | None:
    table = _get_table()
    response = table.get_item(
        Key={
            "PK": f"USER#{user_id}",
            "SK": "CONV",
        },
    )
    item = response.get("Item")
    if item is None:
        return None
    ttl = int(item.get("ttl", 0))
    if ttl and ttl < int(time.time()):
        return None
    return ConversationState.from_item(item)


def set_conv_state(user_id: int, state: ConversationState) -> None:
    table = _get_table()
    table.put_item(Item=state.to_item(user_id))


def delete_conv_state(user_id: int) -> None:
    table = _get_table()
    table.delete_item(
        Key={
            "PK": f"USER#{user_id}",
            "SK": "CONV",
        },
    )


def get_fx_rates() -> dict[str, float] | None:
    table = _get_table()
    response = table.get_item(
        Key={
            "PK": "CONFIG",
            "SK": "FX_RATES",
        },
    )
    item = response.get("Item")
    if item is None:
        return None
    ttl = int(item.get("ttl", 0))
    if ttl and ttl < int(time.time()):
        return None
    rates_raw = item.get("rates")
    if not isinstance(rates_raw, dict):
        return None
    return {k: float(v) for k, v in rates_raw.items()}


def cache_fx_rates(rates: dict[str, float]) -> None:
    table = _get_table()
    now = _now_iso()
    now_epoch = int(time.time())
    table.put_item(
        Item={
            "PK": "CONFIG",
            "SK": "FX_RATES",
            "rates": {k: Decimal(str(v)) for k, v in rates.items()},
            "fetched_at": now,
            "ttl": now_epoch + 86400,
        },
    )


def get_all_budgets(user_id: int) -> dict[str, int]:
    table = _get_table()
    response = table.query(
        KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
        ExpressionAttributeValues={
            ":pk": f"USER#{user_id}",
            ":prefix": "BUDGET#",
        },
    )
    return {item["category"]: int(item["limit_minor"]) for item in response.get("Items", [])}


def set_budget(user_id: int, category: str, limit_minor: int) -> None:
    table = _get_table()
    table.put_item(
        Item={
            "PK": f"USER#{user_id}",
            "SK": f"BUDGET#{category}",
            "category": category,
            "limit_minor": limit_minor,
            "currency": "EUR",
        },
    )


def delete_budget(user_id: int, category: str) -> bool:
    table = _get_table()
    response = table.delete_item(
        Key={
            "PK": f"USER#{user_id}",
            "SK": f"BUDGET#{category}",
        },
        ReturnValues="ALL_OLD",
    )
    return "Attributes" in response


def get_all_transactions(user_id: int) -> list[Transaction]:
    table = _get_table()
    items: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": "PK = :pk AND begins_with(SK, :prefix)",
        "ExpressionAttributeValues": {
            ":pk": f"USER#{user_id}",
            ":prefix": "TX#",
        },
        "ScanIndexForward": False,
    }
    while True:
        response = table.query(**kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key
    return [Transaction.from_item(item) for item in items if not item.get("deleted", False)]


def get_last_transaction(user_id: int) -> Transaction | None:
    table = _get_table()
    response = table.query(
        KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
        ExpressionAttributeValues={
            ":pk": f"USER#{user_id}",
            ":prefix": "TX#",
        },
        ScanIndexForward=False,
        Limit=10,
    )
    items: list[dict[str, Any]] = response.get("Items", [])
    for item in items:
        if not item.get("deleted", False):
            return Transaction.from_item(item)
    return None


def put_recurring_template(user_id: int, template: RecurringTemplate) -> None:
    client = _get_client()
    item = _item_to_dynamo(template.to_item(user_id))
    client.put_item(TableName=_TABLE_NAME, Item=item)


def get_recurring_template(user_id: int, recur_id: str) -> RecurringTemplate | None:
    table = _get_table()
    response = table.get_item(
        Key={
            "PK": f"USER#{user_id}",
            "SK": f"RECUR#{recur_id}",
        },
    )
    item = response.get("Item")
    if item is None:
        return None
    return RecurringTemplate.from_item(item)


def get_all_recurring_templates(user_id: int) -> list[RecurringTemplate]:
    table = _get_table()
    response = table.query(
        KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
        ExpressionAttributeValues={
            ":pk": f"USER#{user_id}",
            ":prefix": "RECUR#",
        },
    )
    items: list[dict[str, Any]] = response.get("Items", [])
    return [RecurringTemplate.from_item(item) for item in items]


def delete_recurring_template(user_id: int, recur_id: str) -> bool:
    client = _get_client()
    try:
        client.delete_item(
            TableName=_TABLE_NAME,
            Key={
                "PK": _s(f"USER#{user_id}"),
                "SK": _s(f"RECUR#{recur_id}"),
            },
            ConditionExpression="attribute_exists(PK)",
        )
    except client.exceptions.ConditionalCheckFailedException:
        return False
    return True


def mark_alert_sent(user_id: int, date_str: str, category: str) -> bool:
    client = _get_client()
    now = _now_iso()
    now_epoch = int(time.time())
    try:
        client.put_item(
            TableName=_TABLE_NAME,
            Item={
                "PK": _s(f"USER#{user_id}"),
                "SK": _s(f"ALERT#{date_str}#{category}"),
                "sent_at": _s(now),
                "ttl": _n(now_epoch + 90_000),  # 25h
            },
            ConditionExpression="attribute_not_exists(PK)",
        )
    except client.exceptions.ConditionalCheckFailedException:
        return False
    return True

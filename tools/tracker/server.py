#!/usr/bin/env python3
"""Localhost-only HTTP server backing the single-page finance tracker.

Reads live DynamoDB (TX#, BAL#, FX cache) and writes new transactions through
the bot's own storage layer (dynamodb.add_transaction / dynamodb.transfer), so
BAL# stays atomic and update_id dedup keeps syncs idempotent. It never touches
append_rows.py and never reimplements DynamoDB access.

Binds to 127.0.0.1 only: it serves unauthenticated financial data.

Usage:
    FINANCE_USER_ID=123456789 python tools/tracker/server.py [--port 8791] [--user-id N]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from telegram_bot.config.accounts import ACCOUNT_GROUPS, ACCOUNTS  # noqa: E402
from telegram_bot.config.categories import CATEGORIES, CATEGORY_BUCKET  # noqa: E402
from telegram_bot.scripts._common import DEFAULT_USER_ID, require_user_id  # noqa: E402
from telegram_bot.storage import dynamodb  # noqa: E402
from telegram_bot.storage.models import (  # noqa: E402
    MINOR_UNIT_FACTOR,
    AccountBalance,
    Subscription,
    Transaction,
    format_amount,
    monthly_minor,
    to_minor,
)

HOST = "127.0.0.1"
DEFAULT_PORT = 8791
INDEX_HTML = Path(__file__).resolve().parent / "index.html"

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# The UPD#<update_id> dedup key is shared with Telegram's own update_ids, which
# the bot writes from real messages. The tracker only accepts ids in a range
# Telegram will never reach, so a tracker sync can never collide with (and
# silently swallow) a real bot message — or vice versa.
MIN_UPDATE_ID = 1_000_000_000_000

_ACCOUNT_GROUP_OF: dict[str, str] = {
    account_id: group for group, account_ids in ACCOUNT_GROUPS.items() for account_id in account_ids
}


class EntryError(ValueError):
    """A rejected sync entry. Reported per-entry; never aborts the rest of the batch."""


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------


def _accounts_payload() -> dict[str, dict[str, str]]:
    return {
        account_id: {
            "display_name": display_name,
            "currency": currency,
            "group": _ACCOUNT_GROUP_OF.get(account_id, ""),
        }
        for account_id, (display_name, currency) in ACCOUNTS.items()
    }


def _categories_payload() -> dict[str, dict[str, str]]:
    return {
        category_id: {"display_name": category["display_name"], "mode": category["mode"]}
        for category_id, category in CATEGORIES.items()
    }


def _balance_payload(balance: AccountBalance) -> dict[str, Any]:
    return {
        "account_id": balance.account_id,
        "currency": balance.currency,
        "balance_minor": balance.balance_minor,
        "last_updated": balance.last_updated,
    }


def _tx_payload(tx: Transaction) -> dict[str, Any]:
    return {
        "tx_id": tx.tx_id,
        "date": tx.date,
        "timestamp": tx.timestamp,
        "amount_minor": tx.amount_minor,
        "signed_amount_minor": tx.signed_amount_minor,
        "currency": tx.currency,
        "description": tx.description,
        "category": tx.category,
        "category_display": tx.category_display,
        "subcategory": tx.subcategory,
        "source_account": tx.source_account,
        "mode": tx.mode,
        "tx_type": tx.tx_type,
        "paired_tx_sk": tx.paired_tx_sk,
    }


def _sub_payload(sub: Subscription) -> dict[str, Any]:
    return {
        "sub_id": sub.sub_id,
        "name": sub.name,
        "kind": sub.kind,
        "amount_minor": sub.amount_minor,
        "monthly_minor": monthly_minor(sub.amount_minor, sub.period),
        "currency": sub.currency,
        "period": sub.period,
        "source_account": sub.source_account,
        "active": sub.active,
        "note": sub.note,
    }


def _parse_subscription(body: dict[str, Any]) -> Subscription:
    """Validate a subscription upsert. Reference data — it never posts a transaction."""
    name = str(body.get("name", "")).strip()
    if not name:
        raise EntryError("name is required")
    period = str(body.get("period", ""))
    if period not in ("monthly", "weekly", "yearly"):
        raise EntryError(f"period must be monthly/weekly/yearly, got {period!r}")
    account = _parse_account(body, "source_account")
    currency = ACCOUNTS[account][1]

    # 0 is allowed: a commitment whose amount isn't known yet (e.g. Fernwärme).
    raw = str(body.get("amount", "0")).strip() or "0"
    try:
        amount = Decimal(raw)
    except InvalidOperation as exc:
        raise EntryError(f"invalid amount: {raw!r}") from exc
    if amount < 0:
        raise EntryError(f"amount cannot be negative, got {amount}")

    return Subscription(
        sub_id=str(body.get("sub_id") or uuid.uuid4().hex[:12]),
        name=name,
        kind=str(body.get("kind", "")).strip(),
        amount_minor=to_minor(amount, currency),
        currency=currency,
        period=period,
        source_account=account,
        active=bool(body.get("active", True)),
        note=str(body.get("note", "")).strip(),
    )


def _recon(balances: list[AccountBalance], transactions: list[Transaction]) -> list[dict[str, Any]]:
    """Per-account BAL# vs sum-of-transactions residual; drives the UI's recon header."""
    sums: dict[str, int] = {}
    tx_currencies: dict[str, str] = {}
    for tx in transactions:
        sums[tx.source_account] = sums.get(tx.source_account, 0) + tx.signed_amount_minor
        tx_currencies.setdefault(tx.source_account, tx.currency)

    balance_by_account = {balance.account_id: balance for balance in balances}
    rows: list[dict[str, Any]] = []
    for account_id in sorted(set(balance_by_account) | set(sums)):
        balance = balance_by_account.get(account_id)
        balance_minor = balance.balance_minor if balance else 0
        currency = balance.currency if balance else tx_currencies.get(account_id, "")
        sum_minor = sums.get(account_id, 0)
        rows.append(
            {
                "account_id": account_id,
                "currency": currency,
                "balance_minor": balance_minor,
                "sum_minor": sum_minor,
                "residual_minor": balance_minor - sum_minor,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Entry validation
# ---------------------------------------------------------------------------


def _parse_update_id(entry: dict[str, Any]) -> int:
    raw = entry.get("update_id")
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise EntryError(f"update_id must be an int, got {raw!r}")
    if raw < MIN_UPDATE_ID:
        raise EntryError(f"update_id must be >= {MIN_UPDATE_ID} (tracker range), got {raw}")
    return raw


def _parse_date(entry: dict[str, Any]) -> str:
    date = str(entry.get("date", ""))
    if not _DATE_RE.match(date):
        raise EntryError(f"date must be YYYY-MM-DD, got {date!r}")
    return date


def _parse_account(entry: dict[str, Any], key: str) -> str:
    account = str(entry.get(key, ""))
    if account not in ACCOUNTS:
        raise EntryError(f"unknown {key}: {account!r}")
    return account


def _parse_category(entry: dict[str, Any]) -> str:
    category = str(entry.get("category", ""))
    if category not in CATEGORIES:
        raise EntryError(f"unknown category: {category!r}")
    return category


def _parse_amount_minor(entry: dict[str, Any], key: str, currency: str) -> int:
    raw = entry.get(key)
    try:
        amount = Decimal(str(raw).strip())
    except InvalidOperation as exc:
        raise EntryError(f"invalid {key}: {raw!r}") from exc
    if amount <= 0:
        raise EntryError(f"{key} must be positive, got {amount}")
    return to_minor(amount, currency)


def _timestamp_for(date: str) -> str:
    """The user's date joined to the current UTC time-of-day, microseconds included.

    Same convention the bot uses; the microseconds keep SKs distinct when several
    entries land on the same backdated day.
    """
    now = datetime.now(UTC)
    return f"{date}T{now:%H:%M:%S.%f}+00:00"


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


def _write_tx(user_id: int, entry: dict[str, Any], update_id: int) -> tuple[bool, str]:
    date = _parse_date(entry)
    account = _parse_account(entry, "account")
    currency = ACCOUNTS[account][1]
    amount_minor = _parse_amount_minor(entry, "amount", currency)
    category = _parse_category(entry)
    direction = entry.get("direction")
    if direction not in ("out", "in"):
        raise EntryError(f"direction must be 'out' or 'in', got {direction!r}")

    signed_amount_minor = -amount_minor if direction == "out" else amount_minor
    tx = Transaction(
        tx_id=uuid.uuid4().hex[:12],
        date=date,
        timestamp=_timestamp_for(date),
        amount_minor=amount_minor,
        signed_amount_minor=signed_amount_minor,
        currency=currency,
        description=str(entry.get("description") or "").strip(),
        category=category,
        category_display=CATEGORIES[category]["display_name"],
        subcategory=str(entry.get("subcategory") or "").strip(),
        source_account=account,
        # mode follows the category, not the direction: a movement category stays
        # "movement" whichever way the money moved.
        mode=CATEGORIES[category]["mode"],
        tx_type="expense" if direction == "out" else "income",
    )
    written = dynamodb.add_transaction(user_id, tx, update_id)

    sign = "-" if signed_amount_minor < 0 else "+"
    summary = f"{account}  {sign}{format_amount(amount_minor, currency)}  {tx.description}".rstrip()
    return written, summary


def _write_transfer(user_id: int, entry: dict[str, Any], update_id: int) -> tuple[bool, str]:
    date = _parse_date(entry)
    from_account = _parse_account(entry, "from_account")
    to_account = _parse_account(entry, "to_account")
    if from_account == to_account:
        raise EntryError("from_account and to_account must differ")

    from_currency = ACCOUNTS[from_account][1]
    to_currency = ACCOUNTS[to_account][1]
    # Both legs come straight from the request — no FX conversion. The amount the
    # user actually received beats the cached rate, and it works for currencies
    # with no rate at all (USDC).
    from_minor_amount = _parse_amount_minor(entry, "from_amount", from_currency)
    to_minor_amount = _parse_amount_minor(entry, "to_amount", to_currency)

    category = "internal_transfer" if from_currency == to_currency else "fx_exchange"
    category_display = CATEGORIES[category]["display_name"]
    description = str(entry.get("description") or "").strip()

    ts = _timestamp_for(date)
    out_tx_id = uuid.uuid4().hex[:12]
    in_tx_id = uuid.uuid4().hex[:12]
    out_sk = f"TX#{ts}#{out_tx_id}"
    in_sk = f"TX#{ts}#{in_tx_id}"

    out_tx = Transaction(
        tx_id=out_tx_id,
        date=date,
        timestamp=ts,
        amount_minor=from_minor_amount,
        signed_amount_minor=-from_minor_amount,
        currency=from_currency,
        description=description or f"Transfer to {to_account}",
        category=category,
        category_display=category_display,
        source_account=from_account,
        mode="movement",
        tx_type="expense",
        paired_tx_sk=in_sk,
    )
    in_tx = Transaction(
        tx_id=in_tx_id,
        date=date,
        timestamp=ts,
        amount_minor=to_minor_amount,
        signed_amount_minor=to_minor_amount,
        currency=to_currency,
        description=description or f"Transfer from {from_account}",
        category=category,
        category_display=category_display,
        source_account=to_account,
        mode="movement",
        tx_type="income",
        paired_tx_sk=out_sk,
    )
    written = dynamodb.transfer(user_id, out_tx, in_tx, update_id)

    summary = (
        f"{from_account}  -{format_amount(from_minor_amount, from_currency)}"
        f"  ->  {to_account}  +{format_amount(to_minor_amount, to_currency)}"
        f"  {out_tx.description}"
    ).rstrip()
    return written, summary


def _sync_entry(user_id: int, entry: Any) -> tuple[dict[str, Any], str]:
    """Write one entry. Returns (result, one-line stdout summary). Never raises."""
    raw_update_id = entry.get("update_id") if isinstance(entry, dict) else None
    reported_update_id = raw_update_id if isinstance(raw_update_id, int) and not isinstance(raw_update_id, bool) else 0

    try:
        if not isinstance(entry, dict):
            raise EntryError("entry must be a JSON object")
        update_id = _parse_update_id(entry)
        kind = entry.get("kind")
        if kind == "tx":
            written, summary = _write_tx(user_id, entry, update_id)
        elif kind == "transfer":
            written, summary = _write_transfer(user_id, entry, update_id)
        else:
            raise EntryError(f"unknown kind: {kind!r}")
    except Exception as exc:  # one bad row must not take down the batch
        detail = str(exc) or exc.__class__.__name__
        return (
            {"update_id": reported_update_id, "status": "error", "detail": detail},
            f"{'error':<9} {detail}",
        )

    if written:
        return (
            {"update_id": update_id, "status": "written", "detail": summary},
            f"{'written':<9} {summary}",
        )
    # add_transaction/transfer return False on a duplicate update_id: that is the
    # idempotency guard doing its job, not a failure.
    detail = f"already recorded (update_id {update_id}); nothing written"
    return (
        {"update_id": update_id, "status": "duplicate", "detail": detail},
        f"{'duplicate':<9} {summary}",
    )


def _load_fx_raw() -> tuple[dict[str, Any], str] | None:
    """Cached FX as (rates, fetched_at). If the cache is empty/expired, fetch
    live rates once and cache them — mirrors scheduler._load_fx_rates so the
    converted tabs (net worth, categories, 50/30/20, subscriptions) never
    silently zero out just because the daily FX refresh lapsed.

    Never raises: on a fetch/cache failure it returns None and the page still
    loads (uncached amounts are excluded from conversions, as before).
    """
    fx = dynamodb.get_fx_rates_raw()
    if fx is not None:
        return fx
    try:
        from telegram_bot.bot.commands import _fetch_fx_rates

        rates = _fetch_fx_rates()
        if not rates or not rates.get("EUR"):
            return None
        dynamodb.cache_fx_rates(rates)
    except Exception:
        return None
    return dynamodb.get_fx_rates_raw()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class TrackerHandler(BaseHTTPRequestHandler):
    def __init__(self, *args: Any, user_id: int, **kwargs: Any) -> None:
        # Set before super().__init__: it handles the request inline.
        self.user_id = user_id
        super().__init__(*args, **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        """Silence per-request logging; the sync summary is the useful output."""

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        try:
            if path == "/":
                self._serve_index()
            elif path == "/api/data":
                self._serve_data()
            else:
                self._send_json(404, {"error": f"not found: {path}"})
        except Exception as exc:  # keep the browser from hanging on e.g. expired AWS creds
            self._send_json(500, {"error": f"{exc.__class__.__name__}: {exc}"})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        routes = {
            "/api/sync": self._serve_sync,
            "/api/subscription": self._serve_subscription_put,
            "/api/subscription/delete": self._serve_subscription_delete,
        }
        handler = routes.get(path)
        if handler is None:
            self._send_json(404, {"error": f"not found: {path}"})
            return
        try:
            handler()
        except Exception as exc:
            self._send_json(500, {"error": f"{exc.__class__.__name__}: {exc}"})

    def _serve_subscription_put(self) -> None:
        try:
            sub = _parse_subscription(self._read_json_body())
        except (ValueError, EntryError) as exc:
            self._send_json(400, {"error": str(exc)})
            return
        dynamodb.put_subscription(self.user_id, sub)
        print(f"subscription  {sub.name}  {format_amount(sub.amount_minor, sub.currency)}/{sub.period}", flush=True)
        self._send_subscriptions()

    def _serve_subscription_delete(self) -> None:
        try:
            body = self._read_json_body()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        sub_id = str(body.get("sub_id", ""))
        if not sub_id:
            self._send_json(400, {"error": "sub_id is required"})
            return
        deleted = dynamodb.delete_subscription(self.user_id, sub_id)
        print(f"subscription  {'deleted' if deleted else 'not found'}  {sub_id}", flush=True)
        self._send_subscriptions()

    def _send_subscriptions(self) -> None:
        subs = dynamodb.get_all_subscriptions(self.user_id)
        self._send_json(200, {"subscriptions": [_sub_payload(s) for s in sorted(subs, key=lambda s: s.name)]})

    def _serve_index(self) -> None:
        if not INDEX_HTML.is_file():
            self._send_json(404, {"error": f"missing {INDEX_HTML}"})
            return
        body = INDEX_HTML.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_data(self) -> None:
        balances = dynamodb.get_balances(self.user_id)
        transactions = dynamodb.get_all_transactions(self.user_id)
        subscriptions = dynamodb.get_all_subscriptions(self.user_id)
        fx = _load_fx_raw()
        self._send_json(
            200,
            {
                "accounts": _accounts_payload(),
                "categories": _categories_payload(),
                "buckets": CATEGORY_BUCKET,
                "minor_unit_factor": MINOR_UNIT_FACTOR,
                "fx": None if fx is None else {"rates": fx[0], "fetched_at": fx[1]},
                "balances": [_balance_payload(balance) for balance in balances],
                "transactions": [_tx_payload(tx) for tx in transactions],
                "subscriptions": [_sub_payload(sub) for sub in sorted(subscriptions, key=lambda s: s.name)],
                # Free-text, so the only source of truth is what's already been used.
                "subcategories": sorted({tx.subcategory for tx in transactions if tx.subcategory}),
                "recon": _recon(balances, transactions),
            },
        )

    def _serve_sync(self) -> None:
        try:
            payload = self._read_json_body()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        entries = payload.get("entries")
        if not isinstance(entries, list):
            self._send_json(400, {"error": 'body must be {"entries": [...]}'})
            return

        results: list[dict[str, Any]] = []
        for entry in entries:
            result, summary = _sync_entry(self.user_id, entry)
            results.append(result)
            print(summary, flush=True)

        # Re-read after the writes so the UI can refresh without a second round trip.
        balances = dynamodb.get_balances(self.user_id)
        transactions = dynamodb.get_all_transactions(self.user_id)
        self._send_json(
            200,
            {
                "results": results,
                "balances": [_balance_payload(balance) for balance in balances],
                "recon": _recon(balances, transactions),
            },
        )

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid JSON body: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("body must be a JSON object")
        return payload

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Localhost finance tracker server (live DynamoDB read/write).")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port to listen on")
    parser.add_argument("--user-id", type=int, default=DEFAULT_USER_ID, help="Telegram user id whose data to serve")
    args = parser.parse_args(argv)
    require_user_id(args.user_id)

    handler = partial(TrackerHandler, user_id=args.user_id)
    # 127.0.0.1 only, never 0.0.0.0: this serves unauthenticated financial data.
    server = ThreadingHTTPServer((HOST, args.port), handler)
    print(f"Tracker for USER#{args.user_id} — http://{HOST}:{args.port}/  (Ctrl-C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

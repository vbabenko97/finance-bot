#!/usr/bin/env python3
"""Export a read-only snapshot of finance data to CSV files.

Reads DynamoDB (TX#, BAL#, FX cache) READ-ONLY and writes three CSVs into an
output directory (default: a repo-local exports/ dir; override with --out-dir or
the FINANCE_EXPORT_DIR env var), overwriting stable *_latest.csv names
atomically. This module NEVER writes to DynamoDB.

Usage:
    python -m telegram_bot.scripts.export_snapshot [--out-dir DIR] [--user-id N]
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sys
from decimal import Decimal
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError

from telegram_bot.config.accounts import ACCOUNTS
from telegram_bot.scripts._common import DEFAULT_USER_ID, require_user_id
from telegram_bot.storage import dynamodb
from telegram_bot.storage.models import MINOR_UNIT_FACTOR, AccountBalance, Transaction

DEFAULT_OUT_DIR = os.environ.get("FINANCE_EXPORT_DIR", str(Path(__file__).resolve().parents[2] / "exports"))

TX_COLUMNS = [
    "date",
    "timestamp",
    "tx_type",
    "mode",
    "amount",
    "currency",
    "description",
    "category",
    "category_id",
    "account",
    "tags",
    "recur_id",
    "paired_tx_sk",
    "tx_id",
]


def _format_amount(amount_minor: int, currency: str) -> str:
    # Mirrors bot/commands._build_export_csv: 2 dp fiat, 8 dp BTC.
    factor = MINOR_UNIT_FACTOR[currency]
    places = len(str(factor)) - 1
    value = Decimal(amount_minor) / Decimal(factor)
    return f"{value:.{places}f}"


def build_transactions_csv(transactions: list[Transaction]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(TX_COLUMNS)
    for tx in sorted(transactions, key=lambda t: t.timestamp):
        writer.writerow(
            [
                tx.date,
                tx.timestamp,
                tx.tx_type,
                tx.mode,
                _format_amount(tx.signed_amount_minor, tx.currency),
                tx.currency,
                tx.description,
                tx.category_display,  # CSV "category" = human/Cyrillic name
                tx.category,  # CSV "category_id" = slug
                tx.source_account,
                " ".join(f"#{t}" for t in sorted(tx.tags)),
                tx.recur_id,
                tx.paired_tx_sk,
                tx.tx_id,
            ]
        )
    return buf.getvalue().encode("utf-8-sig")


BALANCE_COLUMNS = ["account_id", "display_name", "currency", "balance", "balance_minor", "last_updated"]


def build_balances_csv(balances: list[AccountBalance]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(BALANCE_COLUMNS)
    for bal in sorted(balances, key=lambda b: b.account_id):
        account = ACCOUNTS.get(bal.account_id)
        display_name = account[0] if account else bal.account_id
        writer.writerow(
            [
                bal.account_id,
                display_name,
                bal.currency,
                _format_amount(bal.balance_minor, bal.currency),
                bal.balance_minor,
                bal.last_updated,
            ]
        )
    return buf.getvalue().encode("utf-8-sig")


FX_COLUMNS = ["currency", "rate_per_usd", "usd_per_unit", "fetched_at_utc"]


def build_fx_csv(rates: dict[str, float], fetched_at: str) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(FX_COLUMNS)
    for currency in sorted(rates):
        rate = rates[currency]
        usd_per_unit = 1.0 / rate if rate else 0.0
        writer.writerow([currency, f"{rate:.10g}", f"{usd_per_unit:.10g}", fetched_at])
    return buf.getvalue().encode("utf-8-sig")


def write_atomic(path: Path, content: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(content)
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export a read-only finance snapshot to CSVs.")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="Directory to write the *_latest.csv files into")
    parser.add_argument("--user-id", type=int, default=DEFAULT_USER_ID, help="Telegram user id to export")
    args = parser.parse_args(argv)
    require_user_id(args.user_id)

    try:
        transactions = dynamodb.get_all_transactions(args.user_id)
        balances = dynamodb.get_balances(args.user_id)
        fx = dynamodb.get_fx_rates_raw()
    except (BotoCoreError, ClientError) as exc:
        print(f"ERROR: failed to read DynamoDB (check AWS credentials and region): {exc}", file=sys.stderr)
        return 1

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    write_atomic(out_dir / "2026_full_latest.csv", build_transactions_csv(transactions))
    write_atomic(out_dir / "balances_latest.csv", build_balances_csv(balances))
    if fx is None:
        print("WARNING: no FX cache found; writing header-only fx_rates_latest.csv", file=sys.stderr)
        write_atomic(out_dir / "fx_rates_latest.csv", build_fx_csv({}, ""))
    else:
        rates, fetched_at = fx
        write_atomic(out_dir / "fx_rates_latest.csv", build_fx_csv(rates, fetched_at))

    print(f"Wrote 3 CSVs to {out_dir} ({len(transactions)} transactions, {len(balances)} balances)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

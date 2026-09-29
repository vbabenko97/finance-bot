from __future__ import annotations

from pathlib import Path

import pytest

from telegram_bot.scripts import export_snapshot
from telegram_bot.storage import dynamodb
from telegram_bot.storage.models import AccountBalance, Transaction


def test_get_fx_rates_raw_returns_rates_and_fetched_at(dynamodb_table):
    dynamodb.cache_fx_rates({"USD": 1.0, "UAH": 41.5, "EUR": 0.92})

    result = dynamodb.get_fx_rates_raw()

    assert result is not None
    rates, fetched_at = result
    assert rates["USD"] == 1.0
    assert rates["UAH"] == 41.5
    assert rates["EUR"] == 0.92
    assert fetched_at  # non-empty ISO timestamp


def test_get_fx_rates_raw_returns_none_when_absent(dynamodb_table):
    assert dynamodb.get_fx_rates_raw() is None


def _tx(**kw) -> Transaction:
    base = dict(
        tx_id="abc123def456",
        date="2026-05-24",
        timestamp="2026-05-24T00:00:00+00:00",
        amount_minor=1250,
        signed_amount_minor=-1250,
        currency="EUR",
        description="Spar",
        category="groceries",
        category_display="Продукти",
        source_account="bank_eur_2",
        mode="consumption",
        tx_type="expense",
        tags=["work", "food"],
        recur_id="",
        paired_tx_sk="",
    )
    base.update(kw)
    return Transaction(**base)


def test_build_transactions_csv_header_and_row():
    content = export_snapshot.build_transactions_csv([_tx()])

    assert content.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM
    text = content.decode("utf-8-sig")
    lines = text.strip().split("\n")
    assert lines[0] == (
        "date,timestamp,tx_type,mode,amount,currency,description,"
        "category,category_id,account,tags,recur_id,paired_tx_sk,tx_id"
    )
    assert lines[1] == (
        "2026-05-24,2026-05-24T00:00:00+00:00,expense,consumption,-12.50,EUR,"
        "Spar,Продукти,groceries,bank_eur_2,#food #work,,,abc123def456"
    )


def test_build_transactions_csv_btc_uses_8_decimals():
    tx = _tx(currency="BTC", signed_amount_minor=-150000, source_account="crypto_btc")
    text = export_snapshot.build_transactions_csv([tx]).decode("utf-8-sig")
    amount = text.strip().split("\n")[1].split(",")[4]
    assert amount == "-0.00150000"


def test_build_transactions_csv_sorts_by_timestamp():
    later = _tx(timestamp="2026-05-24T00:00:02+00:00", description="B")
    earlier = _tx(timestamp="2026-05-24T00:00:01+00:00", description="A")
    text = export_snapshot.build_transactions_csv([later, earlier]).decode("utf-8-sig")
    rows = text.strip().split("\n")[1:]
    assert rows[0].split(",")[6] == "A"
    assert rows[1].split(",")[6] == "B"


def test_build_balances_csv_maps_display_name_and_major_amount():
    balances = [
        AccountBalance(
            account_id="bank_uah_1", currency="UAH", balance_minor=50000, last_updated="2026-05-24T10:00:00+00:00"
        ),
        AccountBalance(
            account_id="bank_eur_2", currency="EUR", balance_minor=20000, last_updated="2026-05-24T10:00:00+00:00"
        ),
    ]
    content = export_snapshot.build_balances_csv(balances)
    assert content.startswith(b"\xef\xbb\xbf")
    lines = content.decode("utf-8-sig").strip().split("\n")
    assert lines[0] == "account_id,display_name,currency,balance,balance_minor,last_updated"
    assert lines[1] == "bank_eur_2,Bank EUR 2,EUR,200.00,20000,2026-05-24T10:00:00+00:00"
    assert lines[2] == "bank_uah_1,Bank UAH 1,UAH,500.00,50000,2026-05-24T10:00:00+00:00"


def test_build_balances_csv_unknown_account_falls_back_to_id():
    bal = AccountBalance(
        account_id="mystery", currency="USD", balance_minor=100, last_updated="2026-05-24T10:00:00+00:00"
    )
    line = export_snapshot.build_balances_csv([bal]).decode("utf-8-sig").strip().split("\n")[1]
    assert line.startswith("mystery,mystery,USD,1.00,100,")


def test_build_fx_csv_columns_and_inverse():
    content = export_snapshot.build_fx_csv({"USD": 1.0, "UAH": 41.5}, "2026-05-24T09:00:00+00:00")
    assert content.startswith(b"\xef\xbb\xbf")
    lines = content.decode("utf-8-sig").strip().split("\n")
    assert lines[0] == "currency,rate_per_usd,usd_per_unit,fetched_at_utc"
    uah = lines[1].split(",")
    assert uah[0] == "UAH"
    assert float(uah[1]) == 41.5
    assert abs(float(uah[2]) - (1 / 41.5)) < 1e-9
    assert uah[3] == "2026-05-24T09:00:00+00:00"


def test_build_fx_csv_zero_rate_safe():
    line = export_snapshot.build_fx_csv({"XXX": 0.0}, "t").decode("utf-8-sig").strip().split("\n")[1]
    parts = line.split(",")
    assert parts[0] == "XXX"
    assert float(parts[2]) == 0.0


def test_build_fx_csv_empty_is_header_only():
    text = export_snapshot.build_fx_csv({}, "").decode("utf-8-sig")
    assert text.strip() == "currency,rate_per_usd,usd_per_unit,fetched_at_utc"


def test_write_atomic_writes_and_leaves_no_tmp(tmp_path: Path):
    target = tmp_path / "x.csv"
    export_snapshot.write_atomic(target, b"hello")
    assert target.read_bytes() == b"hello"
    assert list(tmp_path.glob("*.tmp")) == []


def test_write_atomic_overwrites_existing(tmp_path: Path):
    target = tmp_path / "x.csv"
    export_snapshot.write_atomic(target, b"old")
    export_snapshot.write_atomic(target, b"new")
    assert target.read_bytes() == b"new"


def test_main_writes_three_csvs_with_data(dynamodb_table, tmp_path: Path):
    tx = _tx()
    dynamodb_table.put_item(Item=tx.to_item(12345))
    dynamodb.set_balance(12345, "bank_eur_2", 20000, "EUR")
    dynamodb.cache_fx_rates({"USD": 1.0, "EUR": 0.92, "UAH": 41.5})

    rc = export_snapshot.main(["--out-dir", str(tmp_path), "--user-id", "12345"])
    assert rc == 0

    tx_csv = (tmp_path / "2026_full_latest.csv").read_bytes().decode("utf-8-sig")
    bal_csv = (tmp_path / "balances_latest.csv").read_bytes().decode("utf-8-sig")
    fx_csv = (tmp_path / "fx_rates_latest.csv").read_bytes().decode("utf-8-sig")

    assert "abc123def456" in tx_csv
    assert "Bank EUR 2" in bal_csv
    assert "UAH" in fx_csv


def test_main_creates_out_dir_and_handles_empty_table(dynamodb_table, tmp_path: Path):
    out = tmp_path / "nested" / "exports"
    rc = export_snapshot.main(["--out-dir", str(out), "--user-id", "12345"])
    assert rc == 0
    for name, header in [
        ("2026_full_latest.csv", "date,timestamp,tx_type,mode"),
        ("balances_latest.csv", "account_id,display_name,currency"),
        ("fx_rates_latest.csv", "currency,rate_per_usd,usd_per_unit,fetched_at_utc"),
    ]:
        text = (out / name).read_bytes().decode("utf-8-sig").strip()
        assert text.split("\n")[0].startswith(header)


def test_main_refuses_placeholder_user_id_without_writing(tmp_path: Path):
    out = tmp_path / "exports"
    with pytest.raises(SystemExit):
        export_snapshot.main(["--out-dir", str(out), "--user-id", "0"])
    assert not out.exists()  # no header-only CSVs


def test_main_handles_dynamodb_error_without_writing(monkeypatch, tmp_path: Path, capsys):
    from botocore.exceptions import ClientError

    def boom(*args, **kwargs):
        raise ClientError({"Error": {"Code": "ExpiredToken", "Message": "token expired"}}, "Query")

    monkeypatch.setattr(export_snapshot.dynamodb, "get_all_transactions", boom)
    out = tmp_path / "exports"
    rc = export_snapshot.main(["--out-dir", str(out), "--user-id", "12345"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "ExpiredToken" in err or "credentials" in err.lower()
    assert not (out.exists() and list(out.glob("*.csv")))  # wrote nothing

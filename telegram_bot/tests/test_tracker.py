from __future__ import annotations

from unittest.mock import patch

import pytest

from telegram_bot.storage import dynamodb
from tools.tracker import server


def test_tracker_entry_retry_does_not_double_apply_balance(dynamodb_table):
    entry = {
        "kind": "tx",
        "update_id": server.MIN_UPDATE_ID,
        "date": "2026-09-01",
        "account": "bank_eur_2",
        "amount": "12.50",
        "category": "groceries",
        "direction": "out",
        "description": "Example groceries",
    }
    first, _ = server._sync_entry(12345, entry)
    retry, _ = server._sync_entry(12345, entry)
    assert first["status"] == "written"
    assert retry["status"] == "duplicate"
    balances = dynamodb.get_balances(12345)
    assert len(balances) == 1
    assert balances[0].balance_minor == -1250
    assert len(dynamodb.get_all_transactions(12345)) == 1


def test_tracker_cross_currency_transfer_keeps_both_actual_amounts(dynamodb_table):
    entry = {
        "kind": "transfer",
        "update_id": server.MIN_UPDATE_ID,
        "date": "2026-09-01",
        "from_account": "bank_usd_1",
        "from_amount": "10",
        "to_account": "bank_eur_2",
        "to_amount": "9.25",
    }
    result, _ = server._sync_entry(12345, entry)
    assert result["status"] == "written"
    balances = dynamodb.get_balances(12345)
    assert {b.account_id: b.balance_minor for b in balances} == {
        "bank_usd_1": -1000,
        "bank_eur_2": 925,
    }
    transactions = dynamodb.get_all_transactions(12345)
    assert len(transactions) == 2
    assert all(t.paired_tx_sk and t.mode == "movement" for t in transactions)
    assert all(r["residual_minor"] == 0 for r in server._recon(balances, transactions))


def test_tracker_rejects_telegram_update_id_without_writing(dynamodb_table):
    result, _ = server._sync_entry(12345, {"kind": "tx", "update_id": 123})
    assert result["status"] == "error"
    assert dynamodb.get_all_transactions(12345) == []


def test_tracker_refreshes_missing_fx_cache(dynamodb_table):
    with patch("telegram_bot.bot.commands._fetch_fx_rates", return_value={"USD": 1.0, "EUR": 0.92}) as fetch:
        rates, fetched_at = server._load_fx_raw()
        assert rates["EUR"] == 0.92
        assert fetched_at
        assert server._load_fx_raw()[0] == rates
        fetch.assert_called_once()


def test_tracker_survives_failed_fx_refresh(dynamodb_table):
    with patch("telegram_bot.bot.commands._fetch_fx_rates", side_effect=OSError("offline")):
        assert server._load_fx_raw() is None


def test_tracker_requires_user_id_before_starting_server():
    with patch.object(server, "ThreadingHTTPServer") as http_server:
        with pytest.raises(SystemExit):
            server.main(["--user-id", "0"])
        http_server.assert_not_called()
    assert server.HOST == "127.0.0.1"

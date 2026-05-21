from __future__ import annotations

import time

import pytest

from telegram_bot.storage import dynamodb
from telegram_bot.storage.models import ConversationState, RecurringTemplate, Transaction

USER_ID = 99


def _make_tx(
    tx_id: str = "abc123",
    amount_minor: int = 5000,
    signed_amount_minor: int = -5000,
    currency: str = "UAH",
    description: str = "Test",
    category: str = "unknown",
    source_account: str = "bank_uah_1",
    timestamp: str | None = None,
) -> Transaction:
    ts = timestamp or "2025-01-15T10:00:00+00:00"
    return Transaction(
        tx_id=tx_id,
        date="2025-01-15",
        timestamp=ts,
        amount_minor=amount_minor,
        signed_amount_minor=signed_amount_minor,
        currency=currency,
        description=description,
        category=category,
        category_display="Інше",
        source_account=source_account,
        mode="consumption",
        tx_type="expense",
    )


def test_add_transaction(dynamodb_table) -> None:
    tx = _make_tx()
    result = dynamodb.add_transaction(USER_ID, tx, update_id=1001)
    assert result is True

    transactions = dynamodb.get_transactions(USER_ID)
    assert len(transactions) == 1
    assert transactions[0].tx_id == "abc123"
    assert transactions[0].description == "Test"


def test_add_transaction_idempotency(dynamodb_table) -> None:
    tx = _make_tx()
    first = dynamodb.add_transaction(USER_ID, tx, update_id=2001)
    assert first is True

    tx2 = _make_tx(tx_id="def456", timestamp="2025-01-15T10:01:00+00:00")
    second = dynamodb.add_transaction(USER_ID, tx2, update_id=2001)
    assert second is False


def test_balance_update(dynamodb_table) -> None:
    expense = _make_tx(tx_id="exp1", signed_amount_minor=-10000)
    dynamodb.add_transaction(USER_ID, expense, update_id=3001)

    balances = dynamodb.get_balances(USER_ID)
    bal_map = {b.account_id: b for b in balances}
    assert bal_map["bank_uah_1"].balance_minor == -10000

    income = _make_tx(
        tx_id="inc1",
        signed_amount_minor=25000,
        timestamp="2025-01-15T10:02:00+00:00",
    )
    dynamodb.add_transaction(USER_ID, income, update_id=3002)

    balances = dynamodb.get_balances(USER_ID)
    bal_map = {b.account_id: b for b in balances}
    assert bal_map["bank_uah_1"].balance_minor == 15000


def test_delete_transaction(dynamodb_table) -> None:
    tx = _make_tx(tx_id="del1", signed_amount_minor=-7000)
    dynamodb.add_transaction(USER_ID, tx, update_id=4001)

    balances_before = dynamodb.get_balances(USER_ID)
    bal_before = {b.account_id: b for b in balances_before}
    assert bal_before["bank_uah_1"].balance_minor == -7000

    deleted = dynamodb.delete_transaction(USER_ID, tx, update_id=4002)
    assert deleted is True

    # Deleted transactions are filtered from get_transactions
    transactions = dynamodb.get_transactions(USER_ID)
    assert len(transactions) == 0

    # Balance should be restored (reversal of -7000 -> +7000 offset, net = 0)
    balances_after = dynamodb.get_balances(USER_ID)
    bal_after = {b.account_id: b for b in balances_after}
    assert bal_after["bank_uah_1"].balance_minor == 0


def test_delete_transaction_idempotent_same_update_id(dynamodb_table) -> None:
    tx = _make_tx(tx_id="dup1", signed_amount_minor=-4000)
    dynamodb.add_transaction(USER_ID, tx, update_id=4101)

    first = dynamodb.delete_transaction(USER_ID, tx, update_id=4102)
    assert first is True

    # Transport retry: same update_id arrives again. UPD marker rejects.
    second = dynamodb.delete_transaction(USER_ID, tx, update_id=4102)
    assert second is False

    balances = dynamodb.get_balances(USER_ID)
    assert {b.account_id: b.balance_minor for b in balances}["bank_uah_1"] == 0


def test_delete_transaction_rejects_already_deleted(dynamodb_table) -> None:
    tx = _make_tx(tx_id="dup2", signed_amount_minor=-6000)
    dynamodb.add_transaction(USER_ID, tx, update_id=4201)

    first = dynamodb.delete_transaction(USER_ID, tx, update_id=4202)
    assert first is True

    # User double-tap: different update_id, but TX is already deleted.
    # The deleted=false condition must reject, leaving balance untouched.
    second = dynamodb.delete_transaction(USER_ID, tx, update_id=4203)
    assert second is False

    balances = dynamodb.get_balances(USER_ID)
    assert {b.account_id: b.balance_minor for b in balances}["bank_uah_1"] == 0


def test_get_last_transaction(dynamodb_table) -> None:
    tx1 = _make_tx(tx_id="t1", timestamp="2025-01-15T08:00:00+00:00")
    tx2 = _make_tx(tx_id="t2", timestamp="2025-01-15T09:00:00+00:00")
    tx3 = _make_tx(tx_id="t3", timestamp="2025-01-15T10:00:00+00:00")

    dynamodb.add_transaction(USER_ID, tx1, update_id=5001)
    dynamodb.add_transaction(USER_ID, tx2, update_id=5002)
    dynamodb.add_transaction(USER_ID, tx3, update_id=5003)

    # Delete the most recent one
    dynamodb.delete_transaction(USER_ID, tx3, update_id=5004)

    last = dynamodb.get_last_transaction(USER_ID)
    assert last is not None
    assert last.tx_id == "t2"


def test_get_transaction_by_key(dynamodb_table) -> None:
    tx = _make_tx(tx_id="lookup1", timestamp="2025-01-15T11:00:00+00:00")
    dynamodb.add_transaction(USER_ID, tx, update_id=5101)

    fetched = dynamodb.get_transaction_by_key(USER_ID, tx.timestamp, tx.tx_id)
    assert fetched is not None
    assert fetched.tx_id == tx.tx_id


def test_get_transaction_by_key_filters_deleted(dynamodb_table) -> None:
    tx = _make_tx(tx_id="lookup2", timestamp="2025-01-15T11:05:00+00:00")
    dynamodb.add_transaction(USER_ID, tx, update_id=5102)
    dynamodb.delete_transaction(USER_ID, tx, update_id=5103)

    fetched = dynamodb.get_transaction_by_key(USER_ID, tx.timestamp, tx.tx_id)
    assert fetched is None


def test_set_balance(dynamodb_table) -> None:
    dynamodb.set_balance(USER_ID, "cash_usd", 50000, "USD")

    balances = dynamodb.get_balances(USER_ID)
    assert len(balances) == 1
    assert balances[0].account_id == "cash_usd"
    assert balances[0].balance_minor == 50000
    assert balances[0].currency == "USD"


def test_conv_state(dynamodb_table) -> None:
    assert dynamodb.get_conv_state(USER_ID) is None

    now_epoch = int(time.time())
    state = ConversationState(
        step="choose_category",
        data={"currency": "UAH"},
        updated_at="2025-01-15T10:00:00+00:00",
        ttl=now_epoch + 3600,
    )
    dynamodb.set_conv_state(USER_ID, state)

    retrieved = dynamodb.get_conv_state(USER_ID)
    assert retrieved is not None
    assert retrieved.step == "choose_category"
    assert retrieved.data == {"currency": "UAH"}

    dynamodb.delete_conv_state(USER_ID)
    assert dynamodb.get_conv_state(USER_ID) is None


def test_fx_rates_cache(dynamodb_table) -> None:
    from decimal import Decimal as D

    assert dynamodb.get_fx_rates() is None

    # DynamoDB Table resource requires Decimal, not float. Store via table
    # directly to test the get_fx_rates retrieval path.
    now_epoch = int(time.time())
    dynamodb_table.put_item(
        Item={
            "PK": "CONFIG",
            "SK": "FX_RATES",
            "rates": {"UAH": D("41.5"), "EUR": D("0.92"), "USDT": D("1.0")},
            "fetched_at": "2025-01-15T10:00:00+00:00",
            "ttl": now_epoch + 86400,
        },
    )

    retrieved = dynamodb.get_fx_rates()
    assert retrieved is not None
    assert retrieved["UAH"] == pytest.approx(41.5)
    assert retrieved["EUR"] == pytest.approx(0.92)
    assert retrieved["USDT"] == pytest.approx(1.0)


def test_transaction_tags_round_trip(dynamodb_table) -> None:
    tx = _make_tx(tx_id="tagged1", timestamp="2025-01-15T12:00:00+00:00")
    tx.tags = ["work", "italy2026", "trip"]
    dynamodb.add_transaction(USER_ID, tx, update_id=6001)

    fetched = dynamodb.get_transaction_by_key(USER_ID, tx.timestamp, tx.tx_id)
    assert fetched is not None
    assert fetched.tags == ["italy2026", "trip", "work"]


def test_transaction_tags_omitted_when_empty(dynamodb_table) -> None:
    tx = _make_tx(tx_id="untagged1", timestamp="2025-01-15T12:01:00+00:00")
    assert tx.tags == []
    dynamodb.add_transaction(USER_ID, tx, update_id=6002)

    raw = dynamodb_table.get_item(
        Key={"PK": f"USER#{USER_ID}", "SK": f"TX#{tx.timestamp}#{tx.tx_id}"},
    )["Item"]
    assert "tags" not in raw

    fetched = dynamodb.get_transaction_by_key(USER_ID, tx.timestamp, tx.tx_id)
    assert fetched is not None
    assert fetched.tags == []


# ---------------------------------------------------------------------------
# update_transaction
# ---------------------------------------------------------------------------


def _copy_tx(tx: Transaction, **changes) -> Transaction:
    base = Transaction(
        tx_id=tx.tx_id,
        date=tx.date,
        timestamp=tx.timestamp,
        amount_minor=tx.amount_minor,
        signed_amount_minor=tx.signed_amount_minor,
        currency=tx.currency,
        description=tx.description,
        category=tx.category,
        category_display=tx.category_display,
        subcategory=tx.subcategory,
        source_account=tx.source_account,
        mode=tx.mode,
        tx_type=tx.tx_type,
        tags=list(tx.tags),
    )
    for k, v in changes.items():
        setattr(base, k, v)
    return base


def test_update_transaction_amount_only(dynamodb_table) -> None:
    tx = _make_tx(tx_id="up1", signed_amount_minor=-5000, currency="UAH", source_account="bank_uah_1")
    dynamodb.add_transaction(USER_ID, tx, update_id=7001)

    new_tx = _copy_tx(tx, amount_minor=8000, signed_amount_minor=-8000)
    ok = dynamodb.update_transaction(USER_ID, tx, new_tx, update_id=7002)
    assert ok is True

    bal = {b.account_id: b.balance_minor for b in dynamodb.get_balances(USER_ID)}
    assert bal["bank_uah_1"] == -8000

    fetched = dynamodb.get_transaction_by_key(USER_ID, tx.timestamp, tx.tx_id)
    assert fetched is not None
    assert fetched.amount_minor == 8000


def test_update_transaction_account_same_currency(dynamodb_table) -> None:
    tx = _make_tx(tx_id="up2", signed_amount_minor=-6000, currency="UAH", source_account="bank_uah_1")
    dynamodb.add_transaction(USER_ID, tx, update_id=7101)

    new_tx = _copy_tx(tx, source_account="cash_uah")
    ok = dynamodb.update_transaction(USER_ID, tx, new_tx, update_id=7102)
    assert ok is True

    bal = {b.account_id: b.balance_minor for b in dynamodb.get_balances(USER_ID)}
    assert bal["bank_uah_1"] == 0
    assert bal["cash_uah"] == -6000


def test_update_transaction_account_different_currency(dynamodb_table) -> None:
    tx = _make_tx(
        tx_id="up3", amount_minor=10000, signed_amount_minor=-10000, currency="EUR", source_account="bank_eur_2"
    )
    dynamodb.add_transaction(USER_ID, tx, update_id=7201)

    new_tx = _copy_tx(tx, amount_minor=10000, signed_amount_minor=-10000, currency="USD", source_account="bank_usd_1")
    ok = dynamodb.update_transaction(USER_ID, tx, new_tx, update_id=7202)
    assert ok is True

    balances = {b.account_id: b for b in dynamodb.get_balances(USER_ID)}
    assert balances["bank_eur_2"].balance_minor == 0
    assert balances["bank_eur_2"].currency == "EUR"
    assert balances["bank_usd_1"].balance_minor == -10000
    assert balances["bank_usd_1"].currency == "USD"


def test_update_transaction_description_only_no_balance_writes(dynamodb_table) -> None:
    tx = _make_tx(tx_id="up4", signed_amount_minor=-5000, source_account="bank_uah_1", description="old")
    dynamodb.add_transaction(USER_ID, tx, update_id=7301)
    bal_before = {b.account_id: b.balance_minor for b in dynamodb.get_balances(USER_ID)}

    new_tx = _copy_tx(tx, description="new desc")
    ok = dynamodb.update_transaction(USER_ID, tx, new_tx, update_id=7302)
    assert ok is True

    bal_after = {b.account_id: b.balance_minor for b in dynamodb.get_balances(USER_ID)}
    assert bal_before == bal_after
    fetched = dynamodb.get_transaction_by_key(USER_ID, tx.timestamp, tx.tx_id)
    assert fetched is not None
    assert fetched.description == "new desc"


def test_update_transaction_tags_set_and_clear(dynamodb_table) -> None:
    tx = _make_tx(tx_id="up5", signed_amount_minor=-4000)
    dynamodb.add_transaction(USER_ID, tx, update_id=7401)

    tagged = _copy_tx(tx, tags=["work", "italy2026"])
    assert dynamodb.update_transaction(USER_ID, tx, tagged, update_id=7402) is True
    fetched = dynamodb.get_transaction_by_key(USER_ID, tx.timestamp, tx.tx_id)
    assert fetched is not None
    assert fetched.tags == ["italy2026", "work"]

    cleared = _copy_tx(tagged, tags=[])
    assert dynamodb.update_transaction(USER_ID, tagged, cleared, update_id=7403) is True
    fetched = dynamodb.get_transaction_by_key(USER_ID, tx.timestamp, tx.tx_id)
    assert fetched is not None
    assert fetched.tags == []

    raw = dynamodb_table.get_item(
        Key={"PK": f"USER#{USER_ID}", "SK": f"TX#{tx.timestamp}#{tx.tx_id}"},
    )["Item"]
    assert "tags" not in raw


def test_update_transaction_rejects_deleted(dynamodb_table) -> None:
    tx = _make_tx(tx_id="up6", signed_amount_minor=-3000)
    dynamodb.add_transaction(USER_ID, tx, update_id=7501)
    dynamodb.delete_transaction(USER_ID, tx, update_id=7502)

    new_tx = _copy_tx(tx, description="post-delete")
    ok = dynamodb.update_transaction(USER_ID, tx, new_tx, update_id=7503)
    assert ok is False


def test_update_transaction_rejects_prestate_mismatch(dynamodb_table) -> None:
    tx = _make_tx(tx_id="up7", signed_amount_minor=-5000, source_account="bank_uah_1")
    dynamodb.add_transaction(USER_ID, tx, update_id=7601)

    # First edit succeeds, changes account.
    intermediate = _copy_tx(tx, source_account="cash_uah")
    assert dynamodb.update_transaction(USER_ID, tx, intermediate, update_id=7602) is True

    # Second edit with the original tx as pre-state (stale) must fail.
    stale_new = _copy_tx(tx, description="stale edit")
    ok = dynamodb.update_transaction(USER_ID, tx, stale_new, update_id=7603)
    assert ok is False

    # Balances unaffected by the rejected edit.
    bal = {b.account_id: b.balance_minor for b in dynamodb.get_balances(USER_ID)}
    assert bal["bank_uah_1"] == 0
    assert bal["cash_uah"] == -5000


def test_update_transaction_duplicate_update_id(dynamodb_table) -> None:
    tx = _make_tx(tx_id="up8", signed_amount_minor=-5000)
    dynamodb.add_transaction(USER_ID, tx, update_id=7701)

    new_tx = _copy_tx(tx, description="first")
    assert dynamodb.update_transaction(USER_ID, tx, new_tx, update_id=7702) is True

    # Transport retry: same update_id, different proposed value.
    even_newer = _copy_tx(tx, description="second")
    # NB: pre-state for the second call would also be 'tx' if the caller blindly
    # retries, but the UPD marker kills it before reaching the TX condition.
    ok = dynamodb.update_transaction(USER_ID, tx, even_newer, update_id=7702)
    assert ok is False

    fetched = dynamodb.get_transaction_by_key(USER_ID, tx.timestamp, tx.tx_id)
    assert fetched is not None
    assert fetched.description == "first"


def test_update_transaction_same_account_different_currency_raises(dynamodb_table) -> None:
    tx = _make_tx(tx_id="up9", currency="UAH", source_account="bank_uah_1")
    dynamodb.add_transaction(USER_ID, tx, update_id=7801)

    bad = _copy_tx(tx, currency="USD")  # same account, different currency
    with pytest.raises(ValueError):
        dynamodb.update_transaction(USER_ID, tx, bad, update_id=7802)


# ---------------------------------------------------------------------------
# RecurringTemplate storage
# ---------------------------------------------------------------------------


def _make_template(recur_id: str = "rcr1", **changes) -> RecurringTemplate:
    tpl = RecurringTemplate(
        recur_id=recur_id,
        description="Rent",
        amount_minor=120_000,
        currency="EUR",
        category="home",
        category_display="Дім",
        source_account="bank_eur_2",
        mode="consumption",
        tx_type="expense",
        schedule="monthly",
        schedule_day=1,
        next_run_date="2026-04-01",
        active=True,
        tags=[],
    )
    for k, v in changes.items():
        setattr(tpl, k, v)
    return tpl


def test_recurring_template_round_trip(dynamodb_table) -> None:
    tpl = _make_template(tags=["rent", "fixed"])
    dynamodb.put_recurring_template(USER_ID, tpl)

    fetched = dynamodb.get_recurring_template(USER_ID, "rcr1")
    assert fetched is not None
    assert fetched.description == "Rent"
    assert fetched.amount_minor == 120_000
    assert fetched.tags == ["fixed", "rent"]
    assert fetched.active is True


def test_get_all_recurring_templates(dynamodb_table) -> None:
    dynamodb.put_recurring_template(USER_ID, _make_template(recur_id="r1"))
    dynamodb.put_recurring_template(USER_ID, _make_template(recur_id="r2", active=False))

    templates = dynamodb.get_all_recurring_templates(USER_ID)
    ids = {t.recur_id for t in templates}
    assert ids == {"r1", "r2"}


def test_delete_recurring_template(dynamodb_table) -> None:
    dynamodb.put_recurring_template(USER_ID, _make_template())

    assert dynamodb.delete_recurring_template(USER_ID, "rcr1") is True
    assert dynamodb.get_recurring_template(USER_ID, "rcr1") is None
    assert dynamodb.delete_recurring_template(USER_ID, "rcr1") is False


def test_mark_alert_sent_idempotent(dynamodb_table) -> None:
    first = dynamodb.mark_alert_sent(USER_ID, "2026-04-10", "groceries")
    assert first is True

    second = dynamodb.mark_alert_sent(USER_ID, "2026-04-10", "groceries")
    assert second is False

    # Different day or category → fresh marker.
    assert dynamodb.mark_alert_sent(USER_ID, "2026-04-11", "groceries") is True
    assert dynamodb.mark_alert_sent(USER_ID, "2026-04-10", "dining") is True


# ---------------------------------------------------------------------------
# transfer + delete_paired_transaction
# ---------------------------------------------------------------------------


def _make_transfer_pair(
    timestamp: str = "2026-04-10T12:00:00+00:00",
    out_account: str = "bank_usd_1",
    out_currency: str = "USD",
    out_amount_minor: int = 10_000,
    in_account: str = "bank_eur_2",
    in_currency: str = "EUR",
    in_amount_minor: int = 9_200,
) -> tuple[Transaction, Transaction]:
    out_tx_id = "outA"
    in_tx_id = "inA"
    out_sk = f"TX#{timestamp}#{out_tx_id}"
    in_sk = f"TX#{timestamp}#{in_tx_id}"
    out_tx = Transaction(
        tx_id=out_tx_id,
        date=timestamp[:10],
        timestamp=timestamp,
        amount_minor=out_amount_minor,
        signed_amount_minor=-out_amount_minor,
        currency=out_currency,
        description=f"Transfer to {in_account}",
        category="fx_exchange" if out_currency != in_currency else "internal_transfer",
        category_display="FX",
        source_account=out_account,
        mode="movement",
        tx_type="expense",
        paired_tx_sk=in_sk,
    )
    in_tx = Transaction(
        tx_id=in_tx_id,
        date=timestamp[:10],
        timestamp=timestamp,
        amount_minor=in_amount_minor,
        signed_amount_minor=in_amount_minor,
        currency=in_currency,
        description=f"Transfer from {out_account}",
        category="fx_exchange" if out_currency != in_currency else "internal_transfer",
        category_display="FX",
        source_account=in_account,
        mode="movement",
        tx_type="income",
        paired_tx_sk=out_sk,
    )
    return out_tx, in_tx


def test_transfer_same_currency_updates_both_balances(dynamodb_table) -> None:
    out_tx, in_tx = _make_transfer_pair(
        out_account="bank_uah_1",
        out_currency="UAH",
        out_amount_minor=50_000,
        in_account="cash_uah",
        in_currency="UAH",
        in_amount_minor=50_000,
    )

    ok = dynamodb.transfer(USER_ID, out_tx, in_tx, update_id=9001)
    assert ok is True

    bal = {b.account_id: b.balance_minor for b in dynamodb.get_balances(USER_ID)}
    assert bal["bank_uah_1"] == -50_000
    assert bal["cash_uah"] == 50_000


def test_transfer_cross_currency_each_balance_in_own_currency(dynamodb_table) -> None:
    out_tx, in_tx = _make_transfer_pair(
        out_account="bank_usd_1",
        out_currency="USD",
        out_amount_minor=10_000,
        in_account="bank_eur_2",
        in_currency="EUR",
        in_amount_minor=9_200,
    )

    assert dynamodb.transfer(USER_ID, out_tx, in_tx, update_id=9002) is True

    balances = {b.account_id: b for b in dynamodb.get_balances(USER_ID)}
    assert balances["bank_usd_1"].balance_minor == -10_000
    assert balances["bank_usd_1"].currency == "USD"
    assert balances["bank_eur_2"].balance_minor == 9_200
    assert balances["bank_eur_2"].currency == "EUR"


def test_transfer_persists_paired_links(dynamodb_table) -> None:
    out_tx, in_tx = _make_transfer_pair()
    dynamodb.transfer(USER_ID, out_tx, in_tx, update_id=9003)

    fetched_out = dynamodb.get_transaction_by_key(USER_ID, out_tx.timestamp, out_tx.tx_id)
    fetched_in = dynamodb.get_transaction_by_key(USER_ID, in_tx.timestamp, in_tx.tx_id)
    assert fetched_out.paired_tx_sk == f"TX#{in_tx.timestamp}#{in_tx.tx_id}"
    assert fetched_in.paired_tx_sk == f"TX#{out_tx.timestamp}#{out_tx.tx_id}"


def test_transfer_idempotent_on_duplicate_update_id(dynamodb_table) -> None:
    out_tx, in_tx = _make_transfer_pair()
    assert dynamodb.transfer(USER_ID, out_tx, in_tx, update_id=9004) is True

    # Replay: same update_id, fresh tx_ids (simulates Telegram retry).
    out2, in2 = _make_transfer_pair(timestamp="2026-04-10T12:01:00+00:00")
    assert dynamodb.transfer(USER_ID, out2, in2, update_id=9004) is False

    # Balances reflect only the first transfer.
    balances = {b.account_id: b.balance_minor for b in dynamodb.get_balances(USER_ID)}
    assert balances["bank_usd_1"] == -10_000
    assert balances["bank_eur_2"] == 9_200


def test_delete_paired_via_out_leg_cascades(dynamodb_table) -> None:
    out_tx, in_tx = _make_transfer_pair()
    dynamodb.transfer(USER_ID, out_tx, in_tx, update_id=9101)

    fetched_out = dynamodb.get_transaction_by_key(USER_ID, out_tx.timestamp, out_tx.tx_id)
    ok = dynamodb.delete_paired_transaction(USER_ID, fetched_out, update_id=9102)
    assert ok is True

    assert dynamodb.get_transaction_by_key(USER_ID, out_tx.timestamp, out_tx.tx_id) is None
    assert dynamodb.get_transaction_by_key(USER_ID, in_tx.timestamp, in_tx.tx_id) is None

    balances = {b.account_id: b.balance_minor for b in dynamodb.get_balances(USER_ID)}
    assert balances["bank_usd_1"] == 0
    assert balances["bank_eur_2"] == 0


def test_delete_paired_via_in_leg_cascades(dynamodb_table) -> None:
    out_tx, in_tx = _make_transfer_pair()
    dynamodb.transfer(USER_ID, out_tx, in_tx, update_id=9201)

    fetched_in = dynamodb.get_transaction_by_key(USER_ID, in_tx.timestamp, in_tx.tx_id)
    ok = dynamodb.delete_paired_transaction(USER_ID, fetched_in, update_id=9202)
    assert ok is True

    assert dynamodb.get_transaction_by_key(USER_ID, in_tx.timestamp, in_tx.tx_id) is None
    assert dynamodb.get_transaction_by_key(USER_ID, out_tx.timestamp, out_tx.tx_id) is None

    balances = {b.account_id: b.balance_minor for b in dynamodb.get_balances(USER_ID)}
    assert balances["bank_usd_1"] == 0
    assert balances["bank_eur_2"] == 0


def test_delete_paired_rejects_when_either_leg_already_deleted(dynamodb_table) -> None:
    out_tx, in_tx = _make_transfer_pair()
    dynamodb.transfer(USER_ID, out_tx, in_tx, update_id=9301)

    fetched_out = dynamodb.get_transaction_by_key(USER_ID, out_tx.timestamp, out_tx.tx_id)
    assert dynamodb.delete_paired_transaction(USER_ID, fetched_out, update_id=9302) is True

    # Try to delete again with the (stale, in-memory) handle. The condition
    # `deleted = false` on both legs must reject.
    second = dynamodb.delete_paired_transaction(USER_ID, fetched_out, update_id=9303)
    assert second is False

    balances = {b.account_id: b.balance_minor for b in dynamodb.get_balances(USER_ID)}
    assert balances["bank_usd_1"] == 0
    assert balances["bank_eur_2"] == 0


def test_delete_paired_falls_back_to_single_when_partner_missing(dynamodb_table) -> None:
    # Construct a tx that claims to be paired but the partner doesn't exist.
    tx = _make_tx(tx_id="orphan", signed_amount_minor=-5000)
    tx.paired_tx_sk = "TX#2026-04-10T12:00:00+00:00#nonexistent"
    dynamodb.add_transaction(USER_ID, tx, update_id=9401)

    ok = dynamodb.delete_paired_transaction(USER_ID, tx, update_id=9402)
    assert ok is True

    assert dynamodb.get_transaction_by_key(USER_ID, tx.timestamp, tx.tx_id) is None
    balances = {b.account_id: b.balance_minor for b in dynamodb.get_balances(USER_ID)}
    assert balances["bank_uah_1"] == 0

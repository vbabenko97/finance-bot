from __future__ import annotations

from unittest.mock import patch

from telegram_bot.bot.commands import _build_export_csv, handle_export
from telegram_bot.bot.telegram_api import _build_multipart
from telegram_bot.storage import dynamodb
from telegram_bot.storage.models import Transaction

USER_ID = 99


def _make_tx(
    tx_id: str = "tx1",
    date_str: str = "2026-04-10",
    timestamp: str | None = None,
    amount_minor: int = 10_000,
    signed_amount_minor: int | None = None,
    currency: str = "EUR",
    description: str = "Lunch",
    category: str = "dining",
    category_display: str = "Кафе та ресторани",
    source_account: str = "bank_eur_2",
    mode: str = "consumption",
    tx_type: str | None = None,
    tags: list[str] | None = None,
    recur_id: str = "",
) -> Transaction:
    sign = 1 if mode == "income" else -1
    return Transaction(
        tx_id=tx_id,
        date=date_str,
        timestamp=timestamp or f"{date_str}T12:00:00+00:00",
        amount_minor=amount_minor,
        signed_amount_minor=signed_amount_minor if signed_amount_minor is not None else sign * amount_minor,
        currency=currency,
        description=description,
        category=category,
        category_display=category_display,
        source_account=source_account,
        mode=mode,
        tx_type=tx_type or ("income" if mode == "income" else "expense"),
        tags=tags or [],
        recur_id=recur_id,
    )


# ---------------------------------------------------------------------------
# _build_export_csv
# ---------------------------------------------------------------------------


class TestBuildExportCsv:
    def test_empty_input_returns_header_only(self):
        content = _build_export_csv([])
        decoded = content.decode("utf-8-sig")
        assert decoded.splitlines() == [
            "date,timestamp,tx_type,amount,currency,description,category,category_id,account,tags,recur_id"
        ]

    def test_utf8_bom_prefix(self):
        content = _build_export_csv([])
        # utf-8-sig encoding prefixes with 0xEF 0xBB 0xBF so Excel auto-detects UTF-8.
        assert content[:3] == b"\xef\xbb\xbf"

    def test_signed_amount_decimal_per_currency(self):
        txs = [
            _make_tx(amount_minor=2_550, signed_amount_minor=-2_550, currency="USD"),
            _make_tx(
                tx_id="t2", currency="BTC", amount_minor=50_000_000, signed_amount_minor=50_000_000, mode="income"
            ),
        ]
        decoded = _build_export_csv(txs).decode("utf-8-sig")
        rows = [line for line in decoded.splitlines()[1:] if line]
        # USD: -25.50; BTC: 0.5
        assert "-25.50" in rows[0]
        assert "0.5" in rows[1]

    def test_cyrillic_description_preserved(self):
        tx = _make_tx(description="Сільпо", category_display="Продукти")
        decoded = _build_export_csv([tx]).decode("utf-8-sig")
        assert "Сільпо" in decoded
        assert "Продукти" in decoded

    def test_description_with_comma_is_quoted(self):
        tx = _make_tx(description="Coffee, milk")
        decoded = _build_export_csv([tx]).decode("utf-8-sig")
        assert '"Coffee, milk"' in decoded

    def test_tags_rendered_as_hashtags(self):
        tx = _make_tx(tags=["work", "italy2026"])
        decoded = _build_export_csv([tx]).decode("utf-8-sig")
        assert "#italy2026 #work" in decoded

    def test_empty_tags_field(self):
        tx = _make_tx(tags=[])
        decoded = _build_export_csv([tx]).decode("utf-8-sig")
        # Tags column is empty between commas (or end-of-line)
        rows = decoded.splitlines()
        assert rows[1].count(",") >= 10  # at least 11 columns separated by 10 commas

    def test_recur_id_passthrough(self):
        tx = _make_tx(recur_id="rcr1")
        decoded = _build_export_csv([tx]).decode("utf-8-sig")
        assert "rcr1" in decoded


# ---------------------------------------------------------------------------
# handle_export
# ---------------------------------------------------------------------------


class TestHandleExport:
    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_invalid_month_format(self, mock_send, dynamodb_table):
        handle_export("token", 123, USER_ID, "/export april", {})
        mock_send.assert_called_once()
        assert "YYYY-MM" in mock_send.call_args[0][2]

    @patch("telegram_bot.bot.telegram_api.send_message")
    def test_invalid_month_value(self, mock_send, dynamodb_table):
        handle_export("token", 123, USER_ID, "/export 2026-13", {})
        mock_send.assert_called_once()
        assert "YYYY-MM" in mock_send.call_args[0][2]

    @patch("telegram_bot.bot.telegram_api.send_document")
    def test_empty_db_still_sends_header_only_csv(self, mock_send_doc, dynamodb_table):
        handle_export("token", 123, USER_ID, "/export", {})

        mock_send_doc.assert_called_once()
        args = mock_send_doc.call_args[0]
        kwargs = mock_send_doc.call_args.kwargs
        # positional: token, chat_id, filename, content, mime_type
        assert args[2] == "transactions_all.csv"
        decoded = args[3].decode("utf-8-sig")
        assert decoded.splitlines() == [
            "date,timestamp,tx_type,amount,currency,description,category,category_id,account,tags,recur_id"
        ]
        assert kwargs["caption"] == "0 transaction(s)"

    @patch("telegram_bot.bot.telegram_api.send_document")
    def test_all_transactions_default_filename(self, mock_send_doc, dynamodb_table):
        dynamodb.add_transaction(USER_ID, _make_tx(tx_id="a", date_str="2026-03-10"), update_id=1)
        dynamodb.add_transaction(USER_ID, _make_tx(tx_id="b", date_str="2026-04-10"), update_id=2)

        handle_export("token", 123, USER_ID, "/export", {})

        args = mock_send_doc.call_args[0]
        assert args[2] == "transactions_all.csv"
        decoded = args[3].decode("utf-8-sig")
        rows = [line for line in decoded.splitlines() if line]
        assert len(rows) == 3  # header + 2

    @patch("telegram_bot.bot.telegram_api.send_document")
    def test_month_filter_respected(self, mock_send_doc, dynamodb_table):
        dynamodb.add_transaction(USER_ID, _make_tx(tx_id="m1", date_str="2026-03-10"), update_id=10)
        dynamodb.add_transaction(USER_ID, _make_tx(tx_id="m2", date_str="2026-04-10"), update_id=11)
        dynamodb.add_transaction(USER_ID, _make_tx(tx_id="m3", date_str="2026-04-25"), update_id=12)

        handle_export("token", 123, USER_ID, "/export 2026-04", {})

        args = mock_send_doc.call_args[0]
        kwargs = mock_send_doc.call_args.kwargs
        assert args[2] == "transactions_2026-04.csv"
        decoded = args[3].decode("utf-8-sig")
        rows = [line for line in decoded.splitlines() if line]
        assert len(rows) == 3  # header + 2 April rows
        assert "2026-03-10" not in decoded
        assert kwargs["caption"] == "2 transaction(s) in 2026-04"

    @patch("telegram_bot.bot.telegram_api.send_document")
    def test_rows_sorted_ascending_by_timestamp(self, mock_send_doc, dynamodb_table):
        dynamodb.add_transaction(
            USER_ID,
            _make_tx(tx_id="late", date_str="2026-04-20", timestamp="2026-04-20T15:00:00+00:00"),
            update_id=20,
        )
        dynamodb.add_transaction(
            USER_ID,
            _make_tx(tx_id="early", date_str="2026-04-05", timestamp="2026-04-05T09:00:00+00:00"),
            update_id=21,
        )

        handle_export("token", 123, USER_ID, "/export", {})

        decoded = mock_send_doc.call_args[0][3].decode("utf-8-sig")
        early_pos = decoded.index("2026-04-05")
        late_pos = decoded.index("2026-04-20")
        assert early_pos < late_pos


# ---------------------------------------------------------------------------
# Multipart payload (telegram_api._build_multipart)
# ---------------------------------------------------------------------------


class TestBuildMultipart:
    def test_includes_boundary_and_fields(self):
        boundary, body = _build_multipart(
            {"chat_id": "12345"},
            "document",
            "report.csv",
            b"a,b,c\n1,2,3\n",
            "text/csv",
        )
        assert boundary.startswith("----TGBotFormBoundary")
        body_str = body.decode("utf-8", errors="replace")
        assert f"--{boundary}" in body_str
        assert 'name="chat_id"' in body_str
        assert "12345" in body_str
        assert 'name="document"' in body_str
        assert 'filename="report.csv"' in body_str
        assert "Content-Type: text/csv" in body_str
        assert "a,b,c\n1,2,3\n" in body_str
        assert body_str.endswith(f"--{boundary}--\r\n")

    def test_handles_unicode_field_values(self):
        _, body = _build_multipart(
            {"caption": "Сільпо звіт"},
            "document",
            "f.csv",
            b"x\n",
            "text/csv",
        )
        assert "Сільпо звіт".encode() in body

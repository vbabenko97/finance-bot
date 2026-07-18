"""Characterization tests for the pure, normally-mocked helpers in telegram_api.

`build_keyboard` and `_build_multipart` are deterministic and never exercised by
the rest of the suite (every caller mocks the module), so they are pinned here.
"""

from __future__ import annotations

from telegram_bot.bot import telegram_api


def test_build_keyboard_chunks_into_columns():
    buttons = [("A", "a"), ("B", "b"), ("C", "c")]
    assert telegram_api.build_keyboard(buttons, columns=2) == [
        [{"text": "A", "callback_data": "a"}, {"text": "B", "callback_data": "b"}],
        [{"text": "C", "callback_data": "c"}],
    ]


def test_build_keyboard_single_column():
    assert telegram_api.build_keyboard([("A", "a"), ("B", "b")], columns=1) == [
        [{"text": "A", "callback_data": "a"}],
        [{"text": "B", "callback_data": "b"}],
    ]


def test_build_keyboard_empty():
    assert telegram_api.build_keyboard([]) == []


def test_build_multipart_structure():
    fields = {"chat_id": "123", "caption": "hello"}
    boundary, body = telegram_api._build_multipart(fields, "document", "export.csv", b"col1,col2\n1,2\n", "text/csv")
    assert boundary.startswith("----TGBotFormBoundary")
    text = body.decode("utf-8")
    assert f'--{boundary}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n123\r\n' in text
    assert 'Content-Disposition: form-data; name="caption"\r\n\r\nhello\r\n' in text
    assert 'Content-Disposition: form-data; name="document"; filename="export.csv"\r\n' in text
    assert "Content-Type: text/csv\r\n\r\n" in text
    assert "col1,col2\n1,2\n" in text
    assert body.endswith(f"--{boundary}--\r\n".encode())


def test_build_multipart_preserves_binary_content():
    raw = b"\x00\x01\xff binary payload"
    _, body = telegram_api._build_multipart({"chat_id": "1"}, "document", "f.bin", raw, "application/octet-stream")
    assert raw in body

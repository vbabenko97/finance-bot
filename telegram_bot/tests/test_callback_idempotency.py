from __future__ import annotations

from telegram_bot.bot.commands import _stable_callback_id


def test_stable_callback_id_is_deterministic() -> None:
    assert _stable_callback_id("abc123") == _stable_callback_id("abc123")


def test_stable_callback_id_distinguishes_inputs() -> None:
    assert _stable_callback_id("abc123") != _stable_callback_id("abc124")


def test_stable_callback_id_fits_in_signed_64() -> None:
    value = _stable_callback_id("some-telegram-callback-id-string")
    assert 0 <= value < 2**48

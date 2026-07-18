"""Unit tests for the callback-query dispatch table (3.5 refactor)."""

from __future__ import annotations

from telegram_bot.bot import commands


class TestResolveCallbackHandler:
    def test_exact_match_takes_priority(self) -> None:
        assert commands._resolve_callback_handler("cancel_del") is commands._cb_cancel_del

    def test_prefix_matches(self) -> None:
        assert commands._resolve_callback_handler("undo:TX#1") is commands._cb_undo
        assert commands._resolve_callback_handler("confirm_del:TX#1") is commands._cb_confirm_del
        assert commands._resolve_callback_handler("del:TX#1") is commands._cb_del
        assert commands._resolve_callback_handler("add:foo") is commands._cb_add
        assert commands._resolve_callback_handler("edit:bar") is commands._cb_edit

    def test_confirm_del_not_shadowed_by_del(self) -> None:
        # "confirm_del:" must not be captured by the "del:" prefix.
        assert commands._resolve_callback_handler("confirm_del:X") is commands._cb_confirm_del

    def test_unknown_returns_none(self) -> None:
        assert commands._resolve_callback_handler("totally_unknown") is None
        assert commands._resolve_callback_handler("") is None

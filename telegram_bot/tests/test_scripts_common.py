from __future__ import annotations

import pytest

from telegram_bot.scripts._common import require_user_id


def test_require_user_id_returns_positive_id_unchanged():
    assert require_user_id(12345) == 12345


def test_require_user_id_rejects_placeholder_zero():
    with pytest.raises(SystemExit) as exc:
        require_user_id(0)
    assert "FINANCE_USER_ID" in str(exc.value)


def test_require_user_id_rejects_negative_id():
    with pytest.raises(SystemExit) as exc:
        require_user_id(-1)
    assert "FINANCE_USER_ID" in str(exc.value)

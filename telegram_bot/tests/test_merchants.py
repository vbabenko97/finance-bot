from __future__ import annotations

from telegram_bot.config.merchants import MERCHANT_ALIASES, canonical_merchant


def test_alias_collapses_latin_to_canonical() -> None:
    assert canonical_merchant("mcdonalds") == "McDonald's"


def test_alias_collapses_cyrillic_to_canonical() -> None:
    assert canonical_merchant("Макдональдс") == "McDonald's"


def test_alias_is_case_insensitive() -> None:
    assert canonical_merchant("MCDONALDS") == "McDonald's"
    assert canonical_merchant("McDonalds") == "McDonald's"


def test_alias_strips_whitespace() -> None:
    assert canonical_merchant("  Макдональдс  ") == "McDonald's"


def test_unaliased_returns_original_stripped() -> None:
    assert canonical_merchant("  Billa ") == "Billa"


def test_empty_input_returns_empty() -> None:
    assert canonical_merchant("") == ""
    assert canonical_merchant("   ") == ""


def test_alias_keys_are_lowercase() -> None:
    # The lookup lowercases the input, so all keys must be lowercase or they'll
    # never match.
    for key in MERCHANT_ALIASES:
        assert key == key.lower(), f"non-lowercase alias key: {key!r}"

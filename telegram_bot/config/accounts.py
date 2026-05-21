from __future__ import annotations

ACCOUNTS: dict[str, tuple[str, str]] = {
    "bank_uah_1": ("Bank UAH 1", "UAH"),
    "bank_usd_1": ("Bank USD 1", "USD"),
    "bank_eur_1": ("Bank EUR 1", "EUR"),
    "bank_eur_2": ("Bank EUR 2", "EUR"),
    "bank_uah_2": ("Bank UAH 2 (debit)", "UAH"),
    "bank_uah_3": ("Bank UAH 3 (credit)", "UAH"),
    "bank_uah_4": ("Bank UAH 4 (savings)", "UAH"),
    "bank_uah_5": ("Bank UAH 5 (business)", "UAH"),
    "bank_usd_2": ("Bank USD 2 (business)", "USD"),
    "cash_uah": ("Cash UAH", "UAH"),
    "cash_usd": ("Cash USD", "USD"),
    "cash_eur": ("Cash EUR", "EUR"),
    "crypto_usdt": ("Crypto USDT", "USDT"),
    "crypto_btc": ("Crypto BTC", "BTC"),
    "loans_eur": ("Loans (EUR)", "EUR"),
}

DEFAULT_ACCOUNTS: dict[str, str] = {
    "UAH": "bank_uah_1",
    "USD": "bank_usd_1",
    "EUR": "bank_eur_2",
    "USDT": "crypto_usdt",
    "BTC": "crypto_btc",
}

ACCOUNT_GROUPS: dict[str, list[str]] = {
    "Bank": [
        "bank_uah_1",
        "bank_usd_1",
        "bank_eur_1",
        "bank_uah_2",
        "bank_uah_3",
        "bank_uah_4",
        "bank_uah_5",
        "bank_usd_2",
        "bank_eur_2",
    ],
    "Cash": [
        "cash_uah",
        "cash_usd",
        "cash_eur",
    ],
    "Crypto": [
        "crypto_usdt",
        "crypto_btc",
    ],
    "Liabilities": [
        "loans_eur",
    ],
}

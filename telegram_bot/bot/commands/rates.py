"""/rates handler (NBU fiat rates + Coinbase BTC)."""

from __future__ import annotations

import json
import logging
import urllib.request
from typing import Any

from telegram_bot.bot import telegram_api

logger = logging.getLogger(__name__)


def handle_rates(token: str, chat_id: int, user_id: int, text: str, message: dict[str, Any]) -> None:
    try:
        req = urllib.request.Request(
            "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?json",
            headers={"User-Agent": "finance-bot/1.0"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
    except Exception:
        logger.exception("Failed to fetch NBU rates")
        telegram_api.send_message(token, chat_id, "Failed to fetch NBU rates.")
        return

    nbu_by_code: dict[str, dict[str, Any]] = {}
    for item in data:
        nbu_by_code[item["cc"]] = item

    date_str = nbu_by_code.get("USD", {}).get("exchangedate", "")

    lines = [f"NBU rates ({date_str}):"]
    for code in ("USD", "EUR", "GBP", "PLN", "CZK"):
        r = nbu_by_code.get(code)
        if r:
            lines.append(f"  {code}/UAH: {r['rate']:.4f}")

    usd_rate = nbu_by_code.get("USD", {}).get("rate")
    eur_rate = nbu_by_code.get("EUR", {}).get("rate")
    if usd_rate and eur_rate:
        lines.append(f"\n  EUR/USD: {eur_rate / usd_rate:.4f}")

    # BTC from Coinbase
    try:
        req = urllib.request.Request(
            "https://api.coinbase.com/v2/prices/BTC-USD/spot",
            headers={"User-Agent": "finance-bot/1.0"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            btc_data = json.loads(resp.read())
        btc_price = float(btc_data.get("data", {}).get("amount", 0))
        if btc_price > 0:
            lines.append(f"  BTC/USD: ${btc_price:,.0f}")
    except Exception:
        # BTC is supplementary to the NBU fiat rates; log but still send the rest.
        logger.exception("Failed to fetch BTC rate for /rates (omitting BTC line)")

    telegram_api.send_message(token, chat_id, "\n".join(lines))

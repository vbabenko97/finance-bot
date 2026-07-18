"""/start and /help handlers."""

from __future__ import annotations

from typing import Any

from telegram_bot.bot import telegram_api


def _build_help_text() -> str:
    return (
        "Finance Bot help\n\n"
        "Quick add:\n"
        "  <code>15 Groceries</code>\n"
        "  <code>25 USD Netflix</code>\n"
        "  <code>100 UAH coffee @bank_uah_1</code>\n"
        "  <code>40 USD lunch #work</code>\n\n"
        "Income:\n"
        "  <code>/income 3000 Salary</code>\n\n"
        "Commands:\n"
        "  /balance - show balances\n"
        "  /history - recent transactions\n"
        "  /search - search transactions\n"
        "  /portfolio - portfolio &amp; net worth\n"
        "  /budget - monthly budget\n"
        "  /summary - monthly summary &amp; pace\n"
        "  /export [YYYY-MM] - download transactions as CSV\n"
        "  /set_budget - set budget limit\n"
        "  /delete_budget - remove budget\n"
        "  /add - step-by-step add\n"
        "  /edit &lt;n&gt; - edit nth transaction from history\n"
        "  /delete - delete last transaction\n"
        "  /recurring - manage recurring templates\n"
        "  /transfer &lt;amount&gt; &lt;from&gt; &lt;to&gt; - move funds between accounts\n"
        "  /set_balance &lt;account&gt; &lt;amount&gt;\n"
        "  /rates - NBU exchange rates\n"
        "  /cancel - cancel current action\n"
        "  /help - this help\n\n"
        "Budget:\n"
        "  <code>/set_budget groceries 200</code>\n"
        "  <code>/budget</code> or <code>/budget 2026-03</code>\n"
        "  <code>/delete_budget groceries</code>\n\n"
        "Search:\n"
        "  <code>/search coffee</code>\n"
        "  <code>/search -c groceries -d 2026-03</code>\n"
        "  <code>/search -t trip2026 -t trip</code>\n\n"
        "Tips:\n"
        "  amount must come first, default currency: EUR\n"
        "  supported currencies: EUR, USD, UAH, USDT, USDC, BTC\n"
        "  use @account to override the default account\n"
        "  use #tag in description to label transactions"
    )


def handle_start(token: str, chat_id: int, user_id: int, text: str, message: dict[str, Any]) -> None:
    greeting = (
        "Finance Bot\n\n"
        "Start with one of these:\n"
        "  <code>15 Groceries</code>\n"
        "  <code>25 USD Netflix</code>\n"
        "  <code>/income 3000 Salary</code>\n\n"
        "Use /help for commands, account overrides, and examples."
    )
    telegram_api.send_message(token, chat_id, greeting)


def handle_help(token: str, chat_id: int, user_id: int, text: str, message: dict[str, Any]) -> None:
    telegram_api.send_message(token, chat_id, _build_help_text())

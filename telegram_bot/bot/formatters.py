from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from telegram_bot.config.accounts import ACCOUNT_GROUPS, ACCOUNTS
from telegram_bot.config.categories import CATEGORIES
from telegram_bot.storage.models import AccountBalance, Transaction, from_minor


def format_confirmation(tx: Transaction) -> str:
    prefix = "+" if tx.tx_type == "income" else "-"
    kind = "income" if tx.tx_type == "income" else "expense"
    amount_str = tx.display_amount()
    lines = [
        f"Added {kind}:",
        f"{prefix}{amount_str} \u2014 {tx.description}",
        f"Category: {tx.category_display} ({tx.category})",
        f"Account: {tx.source_account}",
    ]
    if tx.tags:
        lines.append("Tags: " + " ".join(f"#{t}" for t in tx.tags))
    return "\n".join(lines)


def format_balance_table(
    balances: list[AccountBalance],
    fx_rates: dict[str, float] | None = None,
) -> str:
    balance_map: dict[str, AccountBalance] = {b.account_id: b for b in balances}

    lines: list[str] = ["Balances:"]

    for group_name, account_ids in ACCOUNT_GROUPS.items():
        group_lines: list[str] = []
        for account_id in account_ids:
            bal = balance_map.get(account_id)
            if bal is None:
                continue
            display_name = ACCOUNTS[account_id][0]
            group_lines.append(f"  {display_name}: {bal.display_balance()}")
        if group_lines:
            lines.append(f"\n{group_name}:")
            lines.extend(group_lines)

    if fx_rates:
        total_usd = Decimal(0)
        for bal in balances:
            value = from_minor(bal.balance_minor, bal.currency)
            rate = Decimal(str(fx_rates.get(bal.currency, 0)))
            if rate:
                total_usd += value / rate
        lines.append(f"\nTotal: ~${total_usd:,.2f}")

    return "\n".join(lines)


def format_transaction_list(transactions: list[Transaction]) -> str:
    if not transactions:
        return "No transactions yet."

    lines: list[str] = ["Recent transactions:"]
    for tx in transactions:
        ts = datetime.fromisoformat(tx.timestamp)
        date_str = ts.strftime("%d.%m.%Y %H:%M")
        prefix = "+" if tx.tx_type == "income" else "-"
        amount_str = tx.display_amount()
        lines.append(
            f"\n{date_str}\n{prefix}{amount_str} \u2014 {tx.description}\n{tx.category_display} | {tx.source_account}"
        )
    return "\n".join(lines)


def format_search_results(
    transactions: list[Transaction],
    total_matches: int,
    filters: dict[str, str],
) -> str:
    parts: list[str] = []
    if "text" in filters:
        parts.append(f'"{filters["text"]}"')
    if "category" in filters:
        parts.append(f"category: {filters['category']}")
    if "account" in filters:
        parts.append(f"account: {filters['account']}")
    if "date" in filters:
        parts.append(f"date: {filters['date']}")
    if "tag" in filters:
        tag_display = " ".join(f"#{t}" for t in filters["tag"].split(","))
        parts.append(f"tags: {tag_display}")
    filter_summary = " | ".join(parts)

    if not transactions:
        return f"Search: {filter_summary}\nNo matching transactions."

    shown = len(transactions)
    if shown == total_matches:
        header = f"Search: {filter_summary} ({total_matches} results)"
    else:
        header = f"Search: {filter_summary} ({total_matches} results, showing {shown})"

    lines: list[str] = [header]
    for i, tx in enumerate(transactions, 1):
        lines.append(f"\n{format_history_entry(tx, i)}")

    return "\n".join(lines)


def format_portfolio(
    balances: list[AccountBalance],
    fx_rates: dict[str, float],
) -> str:
    if not balances:
        return "No balances set. Use /set_balance to initialize."

    balance_map: dict[str, AccountBalance] = {b.account_id: b for b in balances}
    lines: list[str] = ["Portfolio"]

    grand_usd = Decimal(0)
    grand_eur = Decimal(0)

    for group_name, account_ids in ACCOUNT_GROUPS.items():
        group_lines: list[str] = []
        group_usd = Decimal(0)
        group_eur = Decimal(0)

        for account_id in account_ids:
            bal = balance_map.get(account_id)
            if bal is None:
                continue
            display_name = ACCOUNTS[account_id][0]
            group_lines.append(f"  {display_name}: {bal.display_balance()}")

            value = from_minor(bal.balance_minor, bal.currency)
            rate = Decimal(str(fx_rates.get(bal.currency, 0)))
            if rate:
                usd = value / rate
                group_usd += usd
                eur_rate = Decimal(str(fx_rates.get("EUR", 0)))
                if eur_rate:
                    group_eur += usd * eur_rate

        if group_lines:
            lines.append(f"\n{group_name}:")
            lines.extend(group_lines)
            lines.append(f"  Subtotal: ~${group_usd:,.0f} / ~€{group_eur:,.0f}")
            grand_usd += group_usd
            grand_eur += group_eur

    lines.append(f"\nNet worth: ~${grand_usd:,.0f} / ~€{grand_eur:,.0f}")
    lines.append("\nRates cached up to 24h")

    return "\n".join(lines)


def format_budget(
    spend_by_category: dict[str, int],
    budgets: dict[str, int],
    month: str,
) -> str:
    month_date = datetime.strptime(month, "%Y-%m")
    month_display = month_date.strftime("%B %Y")

    all_cats = set(spend_by_category) | set(budgets)
    if not all_cats:
        return f"Budget: {month_display}\n\nNo budgets configured and no spending this month.\nUse /set_budget to get started."

    has_budgets = bool(budgets)
    has_spend = any(v > 0 for v in spend_by_category.values())

    if has_budgets and not has_spend:
        lines = [f"Budget: {month_display}\n\nNo spending this month."]
        for cat in sorted(budgets):
            display = CATEGORIES.get(cat, {}).get("display_name", cat)
            limit = from_minor(budgets[cat], "EUR")
            lines.append(f"\n{display} ({cat})")
            lines.append(f"  0 / {limit:,.0f} EUR — {limit:,.0f} left (0%)")
        total_budget = sum(budgets.values())
        lines.append(f"\nTotal: 0 / {from_minor(total_budget, 'EUR'):,.0f} EUR")
        lines.append("Rates cached up to 24h")
        return "\n".join(lines)

    # Build rows: (category, display, spent, budget_or_None)
    budgeted: list[tuple[str, str, int, int, float]] = []
    unbudgeted: list[tuple[str, str, int]] = []

    for cat in all_cats:
        spent = spend_by_category.get(cat, 0)
        budget = budgets.get(cat)
        display = CATEGORIES.get(cat, {}).get("display_name", cat)

        if budget is not None:
            pct = (spent / budget * 100) if budget > 0 else 0
            budgeted.append((cat, display, spent, budget, pct))
        elif spent > 0:
            unbudgeted.append((cat, display, spent))

    budgeted.sort(key=lambda r: (-r[4], -r[2]))
    unbudgeted.sort(key=lambda r: -r[2])

    lines: list[str] = [f"Budget: {month_display}"]

    total_spent = 0
    total_budget = 0

    for _cat, display, spent, budget, pct in budgeted:
        spent_eur = from_minor(spent, "EUR")
        budget_eur = from_minor(budget, "EUR")
        diff = budget - spent
        total_spent += spent
        total_budget += budget

        status = f"{from_minor(diff, 'EUR'):,.0f} left" if diff >= 0 else f"{from_minor(-diff, 'EUR'):,.0f} OVER"

        lines.append(f"\n{display} ({_cat})")
        lines.append(f"  {spent_eur:,.0f} / {budget_eur:,.0f} EUR — {status} ({pct:.0f}%)")

    for _cat, display, spent in unbudgeted:
        spent_eur = from_minor(spent, "EUR")
        total_spent += spent
        lines.append(f"\n{display} ({_cat})")
        lines.append(f"  {spent_eur:,.0f} EUR — no budget")

    total_spent_eur = from_minor(total_spent, "EUR")
    if total_budget > 0:
        total_budget_eur = from_minor(total_budget, "EUR")
        lines.append(f"\nTotal: {total_spent_eur:,.0f} / {total_budget_eur:,.0f} EUR")
    else:
        lines.append(f"\nTotal spent: {total_spent_eur:,.0f} EUR")

    lines.append("Rates cached up to 24h")

    return "\n".join(lines)


@dataclass
class SummaryStats:
    month: str
    spend_minor: int
    income_minor: int
    top_categories: list[tuple[str, int]] = field(default_factory=list)
    top_merchants: list[tuple[str, int]] = field(default_factory=list)
    prev_month_spend_minor: int | None = None
    prev_month_label: str = ""
    is_current_month: bool = False
    day_of_month: int = 0
    days_in_month: int = 0


def format_summary(stats: SummaryStats) -> str:
    month_date = datetime.strptime(stats.month, "%Y-%m")
    month_display = month_date.strftime("%B %Y")

    if stats.spend_minor == 0 and stats.income_minor == 0:
        return f"Summary: {month_display}\n\nNo activity in {stats.month}."

    header = f"Summary: {month_display}"
    if stats.is_current_month:
        header += f" — day {stats.day_of_month} of {stats.days_in_month}"

    lines: list[str] = [header, ""]

    spend_eur = from_minor(stats.spend_minor, "EUR")
    income_eur = from_minor(stats.income_minor, "EUR")
    net_minor = stats.income_minor - stats.spend_minor
    net_eur = from_minor(abs(net_minor), "EUR")
    net_sign = "+" if net_minor >= 0 else "-"

    lines.append(f"Spend: {spend_eur:,.0f} EUR")
    lines.append(f"Income: {income_eur:,.0f} EUR")
    lines.append(f"Net: {net_sign}{net_eur:,.0f} EUR")

    if stats.prev_month_spend_minor is not None and stats.prev_month_label:
        prev_eur = from_minor(stats.prev_month_spend_minor, "EUR")
        if not stats.is_current_month:
            delta_minor = stats.spend_minor - stats.prev_month_spend_minor
            delta_eur = from_minor(abs(delta_minor), "EUR")
            delta_sign = "+" if delta_minor >= 0 else "-"
            if stats.prev_month_spend_minor > 0:
                pct = delta_minor / stats.prev_month_spend_minor * 100
                lines.append(
                    f"\nvs {stats.prev_month_label} ({prev_eur:,.0f} EUR): {delta_sign}{delta_eur:,.0f} EUR ({pct:+.0f}%)"
                )
            else:
                lines.append(f"\nvs {stats.prev_month_label} ({prev_eur:,.0f} EUR): {delta_sign}{delta_eur:,.0f} EUR")

    if stats.is_current_month and stats.day_of_month > 0 and stats.spend_minor > 0:
        projected_minor = round(stats.spend_minor / stats.day_of_month * stats.days_in_month)
        projected_eur = from_minor(projected_minor, "EUR")
        pace_line = f"\nPace: projected {projected_eur:,.0f} EUR"
        if stats.prev_month_spend_minor and stats.prev_month_spend_minor > 0:
            prev_eur = from_minor(stats.prev_month_spend_minor, "EUR")
            diff_minor = projected_minor - stats.prev_month_spend_minor
            diff_pct = diff_minor / stats.prev_month_spend_minor * 100
            if abs(diff_pct) <= 10:
                pace_line += f" — on track vs {stats.prev_month_label} ({prev_eur:,.0f} EUR)"
            elif diff_minor > 0:
                diff_eur = from_minor(diff_minor, "EUR")
                pace_line += (
                    f" — projected to overspend {stats.prev_month_label} ({prev_eur:,.0f} EUR)"
                    f" by {diff_eur:,.0f} EUR ({diff_pct:+.0f}%)"
                )
            else:
                diff_eur = from_minor(-diff_minor, "EUR")
                pace_line += (
                    f" — projected under {stats.prev_month_label} ({prev_eur:,.0f} EUR)"
                    f" by {diff_eur:,.0f} EUR ({diff_pct:+.0f}%)"
                )
        lines.append(pace_line)

    if stats.top_categories:
        lines.append("\nTop categories:")
        for cat, amount in stats.top_categories:
            display = CATEGORIES.get(cat, {}).get("display_name", cat)
            lines.append(f"  {display} ({cat}): {from_minor(amount, 'EUR'):,.0f} EUR")

    if stats.top_merchants:
        lines.append("\nTop merchants:")
        for description, amount in stats.top_merchants:
            lines.append(f"  {description}: {from_minor(amount, 'EUR'):,.0f} EUR")

    lines.append("\nRates cached up to 24h")
    return "\n".join(lines)


def format_history_entry(tx: Transaction, index: int) -> str:
    ts = datetime.fromisoformat(tx.timestamp)
    date_str = ts.strftime("%d.%m.%Y %H:%M")
    prefix = "+" if tx.tx_type == "income" else "-"
    amount_str = tx.display_amount()
    lines = [
        f"{index}. {date_str}",
        f"{prefix}{amount_str} \u2014 {tx.description}",
        f"{tx.category_display} | {tx.source_account}",
    ]
    if tx.tags:
        lines.append(" ".join(f"#{t}" for t in tx.tags))
    return "\n".join(lines)

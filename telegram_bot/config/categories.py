from __future__ import annotations

CATEGORIES: dict[str, dict[str, str]] = {
    # consumption
    "groceries": {"display_name": "Продукти", "mode": "consumption"},
    "dining": {"display_name": "Кафе та ресторани", "mode": "consumption"},
    "shopping": {"display_name": "Покупки", "mode": "consumption"},
    "clothing": {"display_name": "Одяг", "mode": "consumption"},
    "transport": {"display_name": "Транспорт", "mode": "consumption"},
    "healthcare": {"display_name": "Медицина", "mode": "consumption"},
    "entertainment": {"display_name": "Розваги", "mode": "consumption"},
    "subscriptions": {"display_name": "Підписки", "mode": "consumption"},
    "education": {"display_name": "Навчання", "mode": "consumption"},
    "utilities": {"display_name": "Комунальні послуги", "mode": "consumption"},
    "home": {"display_name": "Дім", "mode": "consumption"},
    "services": {"display_name": "Послуги", "mode": "consumption"},
    "personal": {"display_name": "Особисті витрати", "mode": "consumption"},
    "gifts": {"display_name": "Подарунки", "mode": "consumption"},
    "gift_out": {"display_name": "Подарунки (надані)", "mode": "consumption"},
    "charity": {"display_name": "Благодійність", "mode": "consumption"},
    "tobacco": {"display_name": "Тютюн", "mode": "consumption"},
    "taxes_fees": {"display_name": "Податки та збори", "mode": "consumption"},
    "travel": {"display_name": "Подорожі / Квитки", "mode": "consumption"},
    "gaming": {"display_name": "Ігри", "mode": "consumption"},
    "blackout_resilience": {"display_name": "Блекаут", "mode": "consumption"},
    "adult_services": {"display_name": "Особисті розваги", "mode": "consumption"},
    "unknown": {"display_name": "Інше", "mode": "consumption"},
    # income
    "salary": {"display_name": "Зарплатня", "mode": "income"},
    "freelance": {"display_name": "Фріланс", "mode": "income"},
    "cashback": {"display_name": "Кешбек", "mode": "income"},
    "gift_in": {"display_name": "Подарунки (отримані)", "mode": "income"},
    "investment_income": {"display_name": "Інвестиційний дохід", "mode": "income"},
    "refund": {"display_name": "Повернення", "mode": "income"},
    "bonus": {"display_name": "Бонус", "mode": "income"},
    "other_income": {"display_name": "Інший дохід", "mode": "income"},
    # movement
    "internal_transfer": {"display_name": "Внутрішній переказ", "mode": "movement"},
    "cash_withdrawal": {"display_name": "Зняття готівки", "mode": "movement"},
    "cash_deposit": {"display_name": "Внесення готівки", "mode": "movement"},
    "fx_exchange": {"display_name": "Обмін валют", "mode": "movement"},
    "crypto_exchange": {"display_name": "Криптообмін", "mode": "movement"},
    "p2p_transfer": {"display_name": "P2P переказ", "mode": "movement"},
    "asset_buy": {"display_name": "Купівля активів", "mode": "movement"},
    "loan_out": {"display_name": "Позика", "mode": "movement"},
    "loan_in": {"display_name": "Позика (отримана)", "mode": "movement"},
}

CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "groceries": [
        "сільпо",
        "атб",
        "фора",
        "ашан",
        "новус",
        "фудмаркет",
        "varus",
        "продукти",
        "metro",
        "billa",
        "spar",
        "interspar",
        "rewe",
        "penny",
    ],
    "dining": [
        "кафе",
        "ресторан",
        "піца",
        "суші",
        "mcdonald",
        "burger",
        "kfc",
        "фастфуд",
        "starbucks",
        "juicefactory",
    ],
    "transport": [
        "bolt",
        "uber",
        "uklon",
        "таксі",
        "метро",
        "маршрутка",
        "бензин",
        "fuel",
        "wog",
        "okko",
        "flixbus",
    ],
    "healthcare": [
        "аптека",
        "лікар",
        "synevo",
        "клініка",
        "психолог",
        "стоматолог",
    ],
    "subscriptions": [
        "youtube",
        "spotify",
        "netflix",
        "chatgpt",
        "claude",
        "notion",
        "icloud",
        "suno",
    ],
    "entertainment": [
        "кіно",
        "театр",
        "концерт",
        "steam",
        "playstation",
    ],
    "shopping": [
        "rozetka",
        "epicentr",
        "amazon",
        "aliexpress",
    ],
    "utilities": [
        "комунальні",
        "інтернет",
        "vodafone",
        "kyivstar",
        "lifecell",
    ],
    "tobacco": [
        "сигарети",
        "тютюн",
        "iqos",
        "heets",
        "tabak",
        "trafik",
    ],
    "education": [
        "курс",
        "udemy",
        "coursera",
        "книга",
    ],
    "charity": [
        "донат",
        "благодійність",
        "зсу",
        "волонтер",
    ],
    "travel": [
        "квитки",
        "квиток",
        "готель",
        "airbnb",
        "booking",
        "літак",
        "потяг",
        "ryanair",
        "wizzair",
        "укрзалізниця",
    ],
    "services": [
        "документи",
        "нотаріус",
        "пошта",
        "ремонт",
        "хімчистка",
        "перукарня",
        "барбер",
        "доставка",
        "delivery",
    ],
    "salary": [
        "salary",
        "зарплат",
        "devrain",
        "робота",
    ],
    "cashback": [
        "кешбек",
        "cashback",
    ],
}


def infer_category(description: str, mode: str = "consumption") -> str:
    description_lower = description.lower()
    for category_id, keywords in CATEGORY_KEYWORDS.items():
        if CATEGORIES[category_id]["mode"] != mode:
            continue
        for keyword in keywords:
            if keyword in description_lower:
                return category_id
    return "unknown"


def get_categories_for_mode(mode: str) -> dict[str, str]:
    return {cat_id: cat["display_name"] for cat_id, cat in CATEGORIES.items() if cat["mode"] == mode}

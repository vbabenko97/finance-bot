from __future__ import annotations

# Maps a lowercased description spelling to the canonical display name used
# when grouping merchants in /summary. Lets us collapse rows for the same
# merchant typed with different spellings or transliterations.
MERCHANT_ALIASES: dict[str, str] = {
    "макдональдс": "McDonald's",
    "mcdonalds": "McDonald's",
    "mcdonald's": "McDonald's",
}


def canonical_merchant(description: str) -> str:
    stripped = description.strip()
    return MERCHANT_ALIASES.get(stripped.lower(), stripped)

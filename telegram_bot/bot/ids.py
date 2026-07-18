"""Stable idempotency-id derivation shared across handlers.

Derives a 48-bit integer update id from a seed string. Used to dedupe DynamoDB
writes across Lambda transport retries, so the seed must be stable for a given
logical action (a callback query id, or a recurring template + run date).
"""

from __future__ import annotations

import hashlib


def stable_update_id(seed: str) -> int:
    return int(hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12], 16)

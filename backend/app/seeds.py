"""Seed data and small helpers shared by migrations and services."""
from __future__ import annotations

import re

# (id, name, hue, kind) in display order; position = index.
SEED_CATEGORIES: tuple[tuple[str, str, int, str], ...] = (
    ("FOOD_AND_DRINK", "Food and drink", 25, "spending"),
    ("TRANSPORTATION", "Transportation", 230, "spending"),
    ("GENERAL_MERCHANDISE", "Shopping", 300, "spending"),
    ("ENTERTAINMENT", "Entertainment", 340, "spending"),
    ("RENT_AND_UTILITIES", "Rent and utilities", 85, "spending"),
    ("MEDICAL", "Medical", 160, "spending"),
    ("TRAVEL", "Travel", 200, "spending"),
    ("PERSONAL_CARE", "Personal care", 120, "spending"),
    ("GENERAL_SERVICES", "Services", 260, "spending"),
    ("HOME_IMPROVEMENT", "Home improvement", 50, "spending"),
    ("BANK_FEES", "Bank fees", 10, "spending"),
    ("GOVERNMENT_AND_NON_PROFIT", "Taxes and donations", 280, "spending"),
    ("OTHER", "Other", 0, "spending"),
    ("INCOME", "Income", 150, "income"),
    ("TRANSFER_IN", "Transfer in", 240, "transfer"),
    ("TRANSFER_OUT", "Transfer out", 240, "transfer"),
    ("LOAN_PAYMENTS", "Loan payments", 55, "fixed"),
)

# key -> (value, unit), all enabled.
ALERT_SEEDS: dict[str, tuple[float | None, str | None]] = {
    "low": (2000.0, "usd"),
    "big": (250.0, "usd"),
    "budget": (90.0, "percent"),
    "due": (3.0, "days"),
    "newrec": (None, None),
    "price": (None, None),
    "conn": (None, None),
    # Release 3: "$X new to assign" after a sync brings in income.
    "income": (None, None),
    # Release 3.6: the projected balance drops below the ``low`` value within N days.
    "low_ahead": (7.0, "days"),
    # Release 3.6: bills with "Remind me" on, N days before (N per item).
    "reminder": (None, None),
}
ALERT_ORDER = tuple(ALERT_SEEDS)

OTHER = "OTHER"
CATEGORY_KINDS = ("spending", "income", "transfer", "fixed")

# Plaid PFC primaries look like FOOD_AND_DRINK. Anything else is treated as unknown (OTHER).
_PRIMARY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


def valid_primary(value: object) -> str | None:
    return value if isinstance(value, str) and _PRIMARY_RE.match(value) else None


def fnv1a_hue(text: str) -> int:
    """Same as the frontend's ``hueFor``: 32-bit FNV-1a over UTF-16 code units, |h| % 360."""
    if not text:
        return 2166136261 % 360
    h = 2166136261
    data = text.encode("utf-16-le", "surrogatepass")
    for i in range(0, len(data), 2):
        h ^= data[i] | (data[i + 1] << 8)
        h = (h * 16777619) & 0xFFFFFFFF
    signed = h - (1 << 32) if h >= (1 << 31) else h
    return abs(signed) % 360


def title_case(primary: str) -> str:
    return primary.replace("_", " ").strip().title()

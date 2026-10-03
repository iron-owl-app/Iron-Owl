from __future__ import annotations

import calendar as _calendar
import datetime as dt
import re as _re
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

LIABILITY_CATEGORIES = frozenset({"loan", "credit"})
CATEGORIES = ("bank", "hsa", "retirement", "investment", "loan", "credit", "other")


def utcnow() -> dt.datetime:
    """Naive UTC datetime (SQLite has no timezone support)."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def today() -> dt.date:
    """The user's local calendar date, used for balance snapshots."""
    return dt.date.today()


def to_cents(amount: Any) -> int:
    """Dollars (float/str/Decimal) -> integer cents, rounding half away from zero."""
    return int((Decimal(str(amount)) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def to_cents_opt(amount: Any) -> int | None:
    return None if amount is None else to_cents(amount)


def from_cents(cents: int | None) -> float | None:
    return None if cents is None else cents / 100


def iso_utc(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return value.replace(microsecond=0).isoformat() + "Z"


def as_date(value: Any) -> dt.date | None:
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value)[:10])


# ------------------------------------------------------------------ Release 2 helpers

_MONTH_RE = _re.compile(r"^([0-9]{4})-(0[1-9]|1[0-2])\Z")


def parse_month(value: str) -> tuple[int, int]:
    """'YYYY-MM' -> (year, month); ValueError otherwise."""
    m = _MONTH_RE.match(value or "")
    if not m or int(m.group(1)) < 1:
        raise ValueError("month must be YYYY-MM")
    return int(m.group(1)), int(m.group(2))


def month_str(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def month_of(day: dt.date) -> str:
    return month_str(day.year, day.month)


def shift_month(month: str, delta: int) -> str:
    y, m = parse_month(month)
    index = y * 12 + (m - 1) + delta
    return month_str(index // 12, index % 12 + 1)


def month_bounds(month: str) -> tuple[dt.date, dt.date]:
    y, m = parse_month(month)
    return dt.date(y, m, 1), dt.date(y, m, _calendar.monthrange(y, m)[1])


def days_in_month(year: int, month: int) -> int:
    return _calendar.monthrange(year, month)[1]


def add_months(day: dt.date, months: int, anchor_day: int | None = None) -> dt.date:
    """Same day-of-month ``months`` later, clamped to the month's last day."""
    index = day.year * 12 + (day.month - 1) + months
    year, month = index // 12, index % 12 + 1
    wanted = anchor_day or day.day
    return dt.date(year, month, min(wanted, days_in_month(year, month)))


def month_index(day: dt.date) -> int:
    return day.year * 12 + day.month - 1


def fmt_usd(cents: int) -> str:
    """$2,000 for whole dollars, $1,842.10 otherwise; negatives as -$5."""
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    dollars, rem = divmod(cents, 100)
    return f"{sign}${dollars:,}" if rem == 0 else f"{sign}${dollars:,}.{rem:02d}"


def cents_to_str(cents: int) -> str:
    """Plain decimal string for CSV: -12.34, 5.00."""
    sign = "-" if cents < 0 else ""
    dollars, rem = divmod(abs(int(cents)), 100)
    return f"{sign}{dollars}.{rem:02d}"

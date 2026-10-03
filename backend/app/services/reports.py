"""Monthly reports and the Spending tab (SPEC Release 3 section 6, Release 3.10).

Release 2 spending definitions over every non-hidden bank, credit, hsa and other account,
split-aware (each split part counts with its own category). All math in integer cents.
"""
from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict
from dataclasses import dataclass, replace

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Account, Budget, CategoryGroup, RecurringItem
from ..utils import add_months, from_cents, month_bounds, month_of, month_str, parse_month, shift_month, today
from . import calendar as occ_calendar
from . import debt_budget
from .categories import CategoryMap
from .categories import load as load_categories
from .recurring import (
    AMOUNT_TOLERANCE, category_from_rows, claims, display_name, item_aliases, match_keys, matched_history, merchant_key,
)
from .spending import Totals, first_data_day, members_in, plans_by_month, spending_lines, totals_by_month, visits

MAX_MONTHS = 60

# Bills: a spending category counts as a fixed bill when recurring payments (Recurring items
# you haven't dismissed) make up at least this share of what it spent over the last
# BILL_WINDOW_MONTHS months. "Fixed" kind categories (loan payments) are always bills.
BILL_SHARE = 0.75
BILL_WINDOW_MONTHS = 6
DETAIL_TOP = 5
SMALL_TOP = 5
DAY_PLACES = 2


def savings_rate(net: int, income: int) -> float | None:
    """net / income x 100, rounded to 1 decimal; None without income."""
    if income <= 0:
        return None
    return round(net * 100 / income, 1)


def _groups(session: Session) -> list[CategoryGroup]:
    return list(session.scalars(select(CategoryGroup).order_by(CategoryGroup.position, CategoryGroup.id)))


def _month_out(month: str, t: Totals, cmap: CategoryMap, group_ids: set[int]) -> dict:
    spending, fixed = t.total_spent, t.total_fixed
    net = t.income - spending - fixed
    by_category = t.spent_by_category()
    by_group: dict[str, int] = defaultdict(int)
    for category, cents in by_category.items():
        group = cmap.get(category).group_id
        by_group[str(group) if group in group_ids else "none"] += cents
    return {
        "month": month,
        "income": from_cents(t.income),
        "spending": from_cents(spending),
        "fixed": from_cents(fixed),
        "net": from_cents(net),
        "savings_rate": savings_rate(net, t.income),
        "by_category": {c: from_cents(v) for c, v in sorted(by_category.items(), key=lambda cv: cmap.sort_key(cv[0]))},
        "by_group": {g: from_cents(v) for g, v in by_group.items()},
        # Fixed-kind categories (loan payments), which "spending" and by_category leave out.
        "by_fixed": {
            c: from_cents(v) for c, v in sorted(t.fixed_by_category().items(), key=lambda cv: cmap.sort_key(cv[0]))
        },
    }


def _is_recurring(row, bill_items: tuple[dict[str, list[RecurringItem]], dict]) -> bool:  # noqa: ANN001
    """The line is a payment of one of these Recurring items: same merchant, same account (an item
    without an account matches any; 3.19: or one of its other names/accounts) and an amount
    within the detection tolerance."""
    items, aliases = bill_items
    key = merchant_key(row.merchant_name, row.name)
    for item in items.get(key, ()):
        if not claims(item, aliases, row.account_id, key):
            continue
        if abs(abs(row.amount_cents) - abs(item.amount_cents)) <= AMOUNT_TOLERANCE * abs(item.amount_cents):
            return True
    return False


def _bill_items(session: Session) -> tuple[dict[str, list[RecurringItem]], dict]:
    """Recurring payments (money out) you haven't dismissed, by merchant key (each of their
    keys, 3.19), and their aliases."""
    found = list(session.scalars(
        select(RecurringItem).where(RecurringItem.status != "dismissed", RecurringItem.amount_cents < 0)
    ))
    aliases = item_aliases(session, found)
    items: dict[str, list[RecurringItem]] = defaultdict(list)
    for item in found:
        for key in match_keys(item, aliases):
            items[key].append(item)
    return items, aliases


def bill_ids(session: Session, cmap: CategoryMap, day: dt.date) -> set[str]:
    """Categories that are fixed bills (see BILL_SHARE) as of ``day``: fixed kind, or mostly
    payments of Recurring items over the BILL_WINDOW_MONTHS months ending with ``day``."""
    bills = cmap.ids_of_kind("fixed")
    items = _bill_items(session)
    if not items[0]:
        return bills
    start, _ = month_bounds(shift_month(month_of(day), -(BILL_WINDOW_MONTHS - 1)))
    spent: dict[str, int] = defaultdict(int)
    from_recurring: dict[str, int] = defaultdict(int)
    for row in spending_lines(session, start, day):
        info = cmap.get(row.category)
        if info.kind != "spending":
            continue
        spent[info.id] -= row.amount_cents
        if row.amount_cents < 0 and _is_recurring(row, items):
            from_recurring[info.id] -= row.amount_cents
    for category, cents in spent.items():
        if cents > 0 and from_recurring[category] >= BILL_SHARE * cents:
            bills.add(category)
    return bills


def planned_by_month(
    session: Session, cmap: CategoryMap, first: str, last: str, day: dt.date,
) -> dict[str, dict[str, int]]:
    """month -> {category: plan cents}: what was assigned that month, plans of zero or less left
    out (Release 3.14). Spending categories: the Budget's members, from their own row or, from
    the repeat start month on, the plan the month repeats (Release 3.17; one-time moves are
    included, like the Budget page shows). Fixed categories: their own non-removed rows."""
    out: dict[str, dict[str, int]] = defaultdict(dict)
    for month, category, cents in session.execute(
        select(Budget.month, Budget.category, Budget.limit_cents)
        .where(Budget.month >= first, Budget.month <= last, Budget.removed.is_(False), Budget.limit_cents > 0)
    ):
        info = cmap.by_id.get(category)
        if info is not None and info.kind == "fixed":
            out[month][category] = int(cents)
    for month, plans in plans_by_month(session, cmap, day, first, last).items():
        for category, cents in plans.items():
            if cents > 0:
                out[month][category] = cents
    return out


def monthly(session: Session, months: int, day: dt.date | None = None) -> dict:
    day = day or today()
    cmap = load_categories(session)
    last = month_of(day)
    first = shift_month(last, -(months - 1))
    groups = _groups(session)
    group_ids = {g.id for g in groups}
    by_month = totals_by_month(session, cmap, first, last)
    bills = bill_ids(session, cmap, day)
    plans = planned_by_month(session, cmap, first, last, day)
    first_day = first_data_day(session)
    out_months = []
    for m, t in by_month.items():
        month_out = _month_out(m, t, cmap, group_ids)
        plan = plans.get(m, {})
        month_out["planned"] = {
            c: from_cents(plan[c]) for c in sorted(plan, key=cmap.sort_key)
        }
        out_months.append(month_out)
    return {
        "months": out_months,
        "categories": [
            {
                "id": c.id, "name": c.name, "hue": c.hue, "kind": c.kind,
                "group_id": c.group_id if c.group_id in group_ids else None,
                "bill": c.id in bills,
            }
            for c in cmap.ordered("spending") + cmap.ordered("fixed")
        ],
        "groups": [{"id": g.id, "name": g.name} for g in groups],
        # The month of the first transaction (any kind) on the spending accounts, however far
        # back, or None: how far back Overview's month picker reaches.
        "first_month": month_of(first_day) if first_day is not None else None,
    }


# ------------------------------------------------------------------ Reports page drill-downs


def _common_name(rows: list) -> str:
    return Counter(display_name(r.merchant_name, r.name) for r in rows).most_common(1)[0][0]


def category_detail(session: Session, category: str, start: dt.date, end: dt.date, months: int) -> dict | None:
    """One spending or fixed category: spent per month (``months`` months ending with ``end``'s
    month), plus its top merchants and biggest purchases between ``start`` and ``end``.

    None when the category doesn't exist or isn't a spending or fixed category.
    """
    cmap = load_categories(session)
    info = cmap.by_id.get(category)
    if info is None or info.kind not in ("spending", "fixed"):
        return None
    last = month_of(end)
    first = shift_month(last, -(months - 1))
    series_start, _ = month_bounds(first)
    _, series_end = month_bounds(last)

    net: dict[str, int] = {shift_month(first, i): 0 for i in range(months)}
    in_range: list = []
    for row in spending_lines(session, min(series_start, start), max(series_end, end)):
        if cmap.effective_id(row.category) != category:
            continue
        month = month_of(row.date)
        if month in net:
            net[month] += row.amount_cents
        if start <= row.date <= end:
            in_range.append(row)

    by_merchant: dict[str, list] = defaultdict(list)
    for row in in_range:
        by_merchant[merchant_key(row.merchant_name, row.name)].append(row)
    merchants = []
    for rows in by_merchant.values():
        spent = -sum(r.amount_cents for r in rows)
        if spent > 0:
            merchants.append((spent, visits(rows), _common_name(rows)))
    merchants.sort(key=lambda m: (-m[0], -m[1], m[2].lower()))

    # Biggest purchases: whole transactions (a split's parts in this category add up to one).
    by_txn: dict[int, list] = defaultdict(list)
    for row in in_range:
        if row.amount_cents < 0:
            by_txn[row.txn_id].append(row)
    biggest = sorted(
        ((sum(r.amount_cents for r in rows), rows[0]) for rows in by_txn.values()),
        key=lambda cr: (cr[0], cr[1].date, cr[1].txn_id),
    )
    return {
        "category": category,
        "months": [{"month": m, "spent": from_cents(max(0, -v))} for m, v in net.items()],
        "merchants": [
            {"name": name, "spent": from_cents(spent), "count": count} for spent, count, name in merchants[:DETAIL_TOP]
        ],
        "biggest": [
            {
                "id": r.txn_id, "date": r.date.isoformat(), "name": display_name(r.merchant_name, r.name),
                "amount": from_cents(-cents),
            }
            for cents, r in biggest[:DETAIL_TOP]
        ],
    }


def _day_places(stores: dict[str, list]) -> list[str]:
    """The day's top DAY_PLACES stores by money out (net of refunds that day), most first."""
    spent = []
    for rows in stores.values():
        cents = -sum(r.amount_cents for r in rows)
        if cents > 0:
            spent.append((cents, _common_name(rows)))
    spent.sort(key=lambda cn: (-cn[0], cn[1].lower()))
    return [name for _, name in spent[:DAY_PLACES]]


def habits(session: Session, month: str, under_cents: int, day: dt.date | None = None) -> dict:
    """Day-to-day spending (spending categories that aren't bills) in one month: spent per day
    through today, and the purchases under ``under_cents`` grouped by merchant.
    """
    day = day or today()
    cmap = load_categories(session)
    start, end = month_bounds(month)
    through = min(end, day)
    out: dict = {"month": month, "through": None, "days": [],
                 "small": {"under": from_cents(under_cents), "count": 0, "total": 0.0, "merchants": []}}
    if through < start:
        return out
    bills = bill_ids(session, cmap, through)

    by_day: dict[dt.date, int] = {start + dt.timedelta(days=i): 0 for i in range((through - start).days + 1)}
    by_txn: dict[int, list] = defaultdict(list)
    day_stores: dict[dt.date, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for row in spending_lines(session, start, through):
        info = cmap.get(row.category)
        if info.kind != "spending" or info.id in bills:
            continue
        by_day[row.date] -= row.amount_cents
        by_txn[row.txn_id].append(row)
        day_stores[row.date][merchant_key(row.merchant_name, row.name)].append(row)

    # A small purchase is a transaction whose day-to-day outflow is under the limit. Refunds
    # and other credits aren't purchases.
    small: dict[str, list] = defaultdict(list)
    for rows in by_txn.values():
        cents = -sum(r.amount_cents for r in rows)
        if 0 < cents < under_cents:
            small[merchant_key(rows[0].merchant_name, rows[0].name)].append((cents, rows[0]))
    merchants = sorted(
        ((sum(c for c, _ in items), len(items), _common_name([r for _, r in items])) for items in small.values()),
        key=lambda m: (-m[0], -m[1], m[2].lower()),
    )
    out["through"] = through.isoformat()
    out["days"] = [
        {"date": d.isoformat(), "spent": from_cents(max(0, v)), "places": _day_places(day_stores.get(d, {}))}
        for d, v in by_day.items()
    ]
    out["small"].update({
        "count": sum(m[1] for m in merchants),
        "total": from_cents(sum(m[0] for m in merchants)),
        "merchants": [{"name": n, "count": c, "total": from_cents(t)} for t, c, n in merchants[:SMALL_TOP]],
    })
    return out


# ------------------------------------------------------------------ Spending tab (Release 3.10)

SPENDING_MONTHS = 6  # the bars: 6 months ending today's month
PURCHASES_CAP = 100
STORES_TOP = 5


class SpendingMonthError(ValueError):
    """The month is outside the tab's range (422; the message is safe to show)."""


def _category_cents(t: Totals) -> dict[str, int]:
    """category -> what it cost in the month (spending and fixed kinds; refunds net out)."""
    out = t.spent_by_category()
    out.update(t.fixed_by_category())
    return out


def _purchases(rows: list) -> list[dict]:
    """One row per transaction (a split's parts in the category add up), newest first.
    ``amount`` is money out as a positive number (a refund is negative), so the rows add up
    to the category's amount."""
    by_txn: dict[int, list] = defaultdict(list)
    for row in rows:
        by_txn[row.txn_id].append(row)
    items = sorted(by_txn.values(), key=lambda rs: (rs[0].date, rs[0].txn_id), reverse=True)
    return [
        {
            "id": rs[0].txn_id,
            "split": any(r.split_id is not None for r in rs),
            "date": rs[0].date.isoformat(),
            "name": display_name(rs[0].merchant_name, rs[0].name),
            "amount": from_cents(-sum(r.amount_cents for r in rs)),
            "pending": bool(rs[0].pending),
        }
        for rs in items
    ]


def _stores(cmap: CategoryMap, store_rows: dict[str, list]) -> list[dict]:
    """The top stores: net spent (refunds out), ``count`` = transactions that were money out
    (a split is one), ``category`` = the one the store spent most in."""
    stores = []
    for rows in store_rows.values():
        spent = -sum(r.amount_cents for r in rows)
        if spent <= 0:
            continue
        by_txn: dict[int, int] = defaultdict(int)
        by_cat: dict[str, int] = defaultdict(int)
        for r in rows:
            by_txn[r.txn_id] += r.amount_cents
            by_cat[cmap.effective_id(r.category)] -= r.amount_cents
        top = min(by_cat, key=lambda c: (-by_cat[c], cmap.sort_key(c)))
        info = cmap.get(top)
        stores.append((
            spent, sum(1 for v in by_txn.values() if v < 0), _common_name(rows),
            {"id": info.id, "name": info.name, "hue": info.hue},
        ))
    stores.sort(key=lambda s: (-s[0], -s[1], s[2].lower()))
    return [
        {"name": name, "amount": from_cents(spent), "count": count, "category": cat}
        for spent, count, name, cat in stores[:STORES_TOP]
    ]


def spending_page(session: Session, month: str | None, day: dt.date) -> dict:
    """``GET /api/reports/spending`` (SPEC "Accounts, Reports tabs and debt plan (Release 3.10)").

    Every visible bank, card, HSA and other account (the Reports definitions: splits by part,
    pending counts, transfers out). A month's total = spending + fixed (loan payments) = the
    sum of its category amounts. ``month`` must be in [range.first, today's month], else
    ``SpendingMonthError``."""
    cmap = load_categories(session)
    current = month_of(day)
    window_first = shift_month(current, -(SPENDING_MONTHS - 1))
    first_day = first_data_day(session)
    first_month = month_of(first_day) if first_day is not None else None
    if first_month is not None and first_month <= current:
        range_first = max(window_first, first_month)
    else:
        range_first = current
    month = month or current
    if not range_first <= month <= current:
        raise SpendingMonthError(f"Pick a month from {range_first} to {current}.")

    # One query: the window plus the month before it (the first month's "compared with").
    by_month = totals_by_month(session, cmap, shift_month(window_first, -1), current)

    def has_data(m: str) -> bool:
        return first_month is not None and first_month <= m <= current

    def partial(m: str) -> bool:
        return first_day is not None and m == first_month and first_day.day > 1

    def total(m: str) -> int:
        return sum(_category_cents(by_month[m]).values()) if has_data(m) else 0

    months = []
    for index in range(SPENDING_MONTHS):
        m = shift_month(window_first, index)
        months.append({
            "month": m, "total": from_cents(total(m)), "complete": m != current,
            "has_data": has_data(m), "partial": partial(m),
        })
    # The average: whole months only (not today's, not a first month that started late).
    counted = [m["month"] for m in months if m["complete"] and m["has_data"] and not m["partial"]]
    average = None
    if counted:
        cents = sum(total(m) for m in counted)
        average = {"amount": from_cents(round(cents / len(counted))), "months": counted}

    previous_month = shift_month(month, -1)
    previous = None
    if has_data(previous_month):
        previous = {
            "month": previous_month, "total": from_cents(total(previous_month)),
            "has_data": True, "partial": partial(previous_month),
        }

    this = _category_cents(by_month[month])
    before = _category_cents(by_month[previous_month]) if has_data(previous_month) else None
    grand = sum(this.values())
    start, end = month_bounds(month)
    bills = bill_ids(session, cmap, min(end, day))
    in_budget = members_in(session, cmap, current)

    lines_by_cat: dict[str, list] = defaultdict(list)
    store_rows: dict[str, list] = defaultdict(list)
    items = _bill_items(session)
    for row in spending_lines(session, start, end):
        info = cmap.get(row.category)
        if info.kind not in ("spending", "fixed"):
            continue
        lines_by_cat[info.id].append(row)
        # "Where you spent the most" leaves out bills: bill categories (fixed kind included)
        # and payments of Recurring items.
        if info.id in bills or (row.amount_cents < 0 and _is_recurring(row, items)):
            continue
        store_rows[merchant_key(row.merchant_name, row.name)].append(row)

    categories = []
    for category, cents in sorted(this.items(), key=lambda cv: (-cv[1], cmap.sort_key(cv[0]))):
        if cents <= 0:
            continue
        info = cmap.get(category)
        purchases = _purchases(lines_by_cat.get(category, []))
        categories.append({
            "id": category, "name": info.name, "hue": info.hue, "kind": info.kind,
            "bill": category in bills,
            "in_budget": info.kind == "spending" and category in in_budget,
            "amount": from_cents(cents),
            "previous": from_cents(before.get(category, 0)) if before is not None else None,
            "share": round(cents * 100 / grand) if grand > 0 else 0,
            "purchases": purchases[:PURCHASES_CAP],
            "purchase_count": len(purchases),
            "more": max(0, len(purchases) - PURCHASES_CAP),
        })

    return {
        "today": day.isoformat(),
        "current": current,
        "month": month,
        "first_data_date": first_day.isoformat() if first_day is not None else None,
        "range": {"first": range_first, "last": current},
        "months": months,
        "average": average,
        "summary": {
            "month": month, "total": from_cents(grand), "complete": month != current,
            "partial": partial(month), "previous": previous,
        },
        "categories": categories,
        "stores": _stores(cmap, store_rows),
        "stores_left_out": [c["name"] for c in categories if c["bill"]],
    }


# ------------------------------------------------------------------ Year over year (Release 3.14)

YOY_MODES = ("year", "month")


def same_day_last_year(day: dt.date) -> dt.date:
    """The same date a year earlier; Feb 29 becomes Feb 28."""
    return add_months(day, -12)


def yoy_ranges(
    mode: str, day: dt.date, month: str | None = None,
) -> tuple[tuple[dt.date, dt.date], tuple[dt.date, dt.date]]:
    """(current, previous) date ranges, inclusive. ``year`` = Jan 1 to ``day`` vs the same days
    last year. ``month`` (default ``day``'s month): this month = the 1st to ``day`` vs the same days
    last year; a finished month = the whole month vs the whole same month last year. A month after
    ``day``'s month is a ValueError."""
    if mode not in YOY_MODES:
        raise ValueError("mode must be year or month")
    end_prev = same_day_last_year(day)
    if mode == "year":
        return (dt.date(day.year, 1, 1), day), (dt.date(end_prev.year, 1, 1), end_prev)
    current = month_of(day)
    month = month or current
    if month > current:
        raise ValueError("month is in the future")
    if month == current:
        return (day.replace(day=1), day), (end_prev.replace(day=1), end_prev)
    year, mon = parse_month(month)
    return month_bounds(month), month_bounds(month_str(year - 1, mon))


def _yoy_side(session: Session, cmap: CategoryMap, bills: set[str], start: dt.date, end: dt.date) -> dict:
    """Spending categories (bills included, fixed kind left out) between start and end:
    the total, per month, per category and per store (like "Where, exactly")."""
    month_net: dict[str, dict[str, int]] = {}
    m = month_of(start)
    while m <= month_of(end):
        month_net[m] = defaultdict(int)
        m = shift_month(m, 1)
    stores: dict[str, list] = defaultdict(list)
    for row in spending_lines(session, start, end):
        info = cmap.get(row.category)
        if info.kind != "spending":
            continue
        month_net[month_of(row.date)][info.id] += row.amount_cents
        stores[merchant_key(row.merchant_name, row.name)].append(row)

    by_category: dict[str, int] = defaultdict(int)
    months = []
    for month, net in month_net.items():
        # Like Totals.spent: a category's month never goes below zero.
        spent = {c: -v for c, v in net.items() if v < 0}
        for c, v in spent.items():
            by_category[c] += v
        months.append({"month": month, "total": from_cents(sum(spent.values()))})

    merchants = []
    for rows in stores.values():
        spent = -sum(r.amount_cents for r in rows)
        if spent <= 0:
            continue
        by_cat: dict[str, int] = defaultdict(int)
        for r in rows:
            by_cat[cmap.effective_id(r.category)] -= r.amount_cents
        top = min(by_cat, key=lambda c: (-by_cat[c], cmap.sort_key(c)))
        merchants.append((spent, visits(rows), _common_name(rows), top))
    merchants.sort(key=lambda x: (-x[0], -x[1], x[2].lower()))

    categories = []
    for c, cents in sorted(by_category.items(), key=lambda cv: (-cv[1], cmap.sort_key(cv[0]))):
        info = cmap.get(c)
        categories.append({
            "id": c, "name": info.name, "hue": info.hue, "bill": c in bills, "total": from_cents(cents),
        })
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "total": from_cents(sum(by_category.values())),
        "months": months,
        "categories": categories,
        "merchants": [
            {"name": name, "category": cat, "total": from_cents(spent), "count": count}
            for spent, count, name, cat in merchants
        ],
    }


def yoy(session: Session, mode: str, day: dt.date, month: str | None = None) -> dict:
    """``GET /api/reports/yoy`` (SPEC Release 3.14, 3.18): this year and last year.

    In ``year`` mode the months run Jan to ``day``'s month on both sides, the last one cut at
    the same day. ``month`` (month mode only; default ``day``'s month): see ``yoy_ranges``.
    ``partial``: the range is cut at today. ``first_month``: the first month with any data
    (None without data)."""
    if mode != "month":
        month = None
    (cur_start, cur_end), (prev_start, prev_end) = yoy_ranges(mode, day, month)
    cmap = load_categories(session)
    bills = bill_ids(session, cmap, day)
    first_day = first_data_day(session)
    return {
        "mode": mode,
        "month": month_of(cur_start) if mode == "month" else None,
        "partial": cur_end == day,
        "current": _yoy_side(session, cmap, bills, cur_start, cur_end),
        "previous": _yoy_side(session, cmap, bills, prev_start, prev_end),
        "first_month": month_of(first_day) if first_day is not None else None,
    }


# ------------------------------------------------------------------ Subscriptions (Release 3.14)

# Charges a month, by cadence ("once" items aren't subscriptions): (charges a year, 12).
PER_MONTH = {"weekly": (52, 12), "biweekly": (26, 12), "semimonthly": (24, 12), "monthly": (12, 12),
             "quarterly": (4, 12), "yearly": (1, 12)}


def monthly_cents(cents: int, cadence: str) -> int:
    """A charge converted to a month, in whole cents (integer math, half rounds up)."""
    per_year, months = PER_MONTH[cadence]
    return (2 * cents * per_year + months) // (2 * months)
CHANGE_MIN_CENTS = 50
CHANGE_MIN_SHARE = 0.01
FULL_YEAR_MONTHS = 11


def _left_out_category(info, debt_category: str | None) -> bool:  # noqa: ANN001 - CatInfo
    """Not a subscription: loan payments (fixed kind, or the "Paying off debt" category) and
    money moved between accounts (transfer kind)."""
    return info.kind in ("fixed", "transfer") or info.id == debt_category


def _own_charges(item: RecurringItem, rows: list, by_key: dict[str, list[RecurringItem]], aliases: dict) -> list:
    """This item's charges among the merchant's matched rows: when other items share the
    merchant (two plans at one store), each charge goes to the item closest in amount (a tie: the
    lower id), so a charge counts for one item only."""
    out = []
    for r in rows:
        if getattr(r, "is_transfer", False):
            continue
        key = merchant_key(r.merchant_name, r.name)
        rivals = [o for o in by_key.get(key, ()) if claims(o, aliases, r.account_id, key)] or [item]
        best = min(rivals, key=lambda o: (abs(abs(r.amount_cents) - abs(o.amount_cents)), o.id))
        if best.id == item.id:
            out.append(r)
    return out


def price_change(charges: list, day: dt.date) -> tuple[dict | None, bool]:
    """(change, full_year) from an item's charges (any order). Only the last 12 months count.
    ``change`` = the most recent charge that differs from the one before by more than
    CHANGE_MIN_CENTS and more than CHANGE_MIN_SHARE: {amount: signed dollars, + = went up,
    month}. ``full_year``: a charge 11+ months ago, so "same price all year" is known."""
    since = same_day_last_year(day)
    recent = sorted((r for r in charges if since < r.date <= day), key=lambda r: (r.date, r.id))
    full_year = bool(recent) and recent[0].date <= add_months(day, -FULL_YEAR_MONTHS)
    for before, after in zip(reversed(recent[:-1]), reversed(recent[1:])):
        prev, cur = abs(before.amount_cents), abs(after.amount_cents)
        diff = cur - prev
        if abs(diff) > CHANGE_MIN_CENTS and abs(diff) > CHANGE_MIN_SHARE * prev:
            return {"amount": from_cents(diff), "month": month_of(after.date)}, full_year
    return None, full_year


@dataclass(frozen=True)
class Subscription:
    """One subscription (SPEC Release 3.14): a Recurring item charged to a credit card."""

    item: RecurringItem
    category: str | None
    card: Account
    monthly_cents: int
    change: dict | None
    full_year: bool
    next_date: dt.date | None = None


def _card_of(item: RecurringItem, charges: list, accounts: dict[int, Account]) -> Account | None:
    """The credit card the item is charged to: its own account when it has one (a visible
    credit account, else None); without one, the card holding most of its charges, when more
    than half of them are on visible credit cards."""
    def card(account_id: int | None) -> Account | None:
        account = accounts.get(account_id) if account_id is not None else None
        return account if account is not None and account.category == "credit" and not account.hidden else None

    if item.account_id is not None:
        return card(item.account_id)
    on_cards = Counter(r.account_id for r in charges if card(r.account_id) is not None)
    if not on_cards or sum(on_cards.values()) * 2 <= len(charges):
        return None
    best = min(on_cards, key=lambda a: (-on_cards[a], a))
    return accounts[best]


def subscription_list(session: Session, day: dt.date) -> list[Subscription]:
    """Subscriptions, largest a month first (shared by Reports and Recurring): Recurring
    money-out items you haven't dismissed (not one-time) that are charged to a credit card,
    except loan payments, card bill payments and transfers. ``category`` is the item's own,
    else the one its history uses most (None when neither)."""
    cmap = load_categories(session)
    debt_category = debt_budget.filed_category(session, cmap)
    accounts = {a.id: a for a in session.scalars(select(Account))}
    items = [
        i for i in session.scalars(
            select(RecurringItem).where(RecurringItem.status != "dismissed", RecurringItem.amount_cents < 0)
        )
        if i.cadence in PER_MONTH
    ]
    history = matched_history(session, items, day)
    aliases = item_aliases(session, items)
    by_key: dict[str, list[RecurringItem]] = defaultdict(list)
    for item in items:
        for key in match_keys(item, aliases):
            by_key[key].append(item)

    out = []
    for item in items:
        rows = history.get(item.id, [])
        if rows and all(getattr(r, "is_transfer", False) for r in rows):
            continue  # a credit card bill (its payments are marked transfers)
        category = item.category_id if item.category_id in cmap.by_id else category_from_rows(rows, cmap)
        if category is not None and _left_out_category(cmap.get(category), debt_category):
            continue
        charges = _own_charges(item, rows, by_key, aliases)
        card = _card_of(item, charges, accounts)
        if card is None:
            continue
        change, full_year = price_change(charges, day)
        monthly = monthly_cents(-item.amount_cents, item.cadence)
        out.append(Subscription(item, category, card, monthly, change, full_year))
    out.sort(key=lambda s: (-s.monthly_cents, s.item.name.lower(), s.item.id))
    return _with_next_dates(session, out, day)


def _with_next_dates(session: Session, subs: list[Subscription], day: dt.date) -> list[Subscription]:
    """Each subscription's next charge (Release 3.15, the Recurring page's "Next: Oct 8"): its
    first calendar occurrence from today on that is still expected (moves and skips applied,
    charges already matched left out). With no occurrence in the horizon at all: its
    ``next_date`` when that isn't past, else None."""
    if not subs:
        return subs
    occs = occ_calendar.occurrences_for(
        session, day, day, occ_calendar.horizon_end(day), items=[s.item for s in subs],
    )
    first: dict[int, dt.date] = {}
    for occ in occs:  # by date
        if occ.status == "upcoming" and occ.date >= day:
            first.setdefault(occ.item.id, occ.date)
    seen = {occ.item.id for occ in occs}
    out = []
    for s in subs:
        nd = s.item.next_date
        if s.item.id in first:
            nxt = first[s.item.id]
        elif s.item.id in seen:
            nxt = None  # every one in the horizon is skipped or already charged
        else:
            nxt = nd if nd is not None and nd >= day else None
        out.append(replace(s, next_date=nxt))
    return out


def subscriptions(session: Session, day: dt.date) -> dict:
    """``GET /api/reports/subscriptions`` (SPEC Release 3.14), see ``subscription_list``."""
    subs = subscription_list(session, day)
    return {
        "items": [
            {
                "id": s.item.id, "name": s.item.name, "category": s.category, "cadence": s.item.cadence,
                "monthly": from_cents(s.monthly_cents), "amount": from_cents(-s.item.amount_cents),
                "change": s.change, "full_year": s.full_year, "card": {"id": s.card.id, "name": s.card.name},
                "next_date": s.next_date.isoformat() if s.next_date else None,
            }
            for s in subs
        ],
        "monthly_total": from_cents(sum(s.monthly_cents for s in subs)),
    }

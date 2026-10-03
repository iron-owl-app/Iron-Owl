"""Budget for a beginner: the guided first setup and "Add a category" (SPEC "Release 3.6").

The setup is one transaction (the router commits or rolls back):
- the "Groceries", "Eating out" and "Gas" rows get their own custom spending categories
  (an existing spending category with the same name is reused);
- "Bills", "Fun" and "Other" fill the matching built-in categories, split by what each
  spent over the last 3 complete months;
- an "Emergency savings" category is created (or reused) and remembered in
  ``app_settings.budget_savings_category`` (it is never found by name afterwards);
- groups are created only when there are none (only ungrouped categories join them); an
  "Income" group holds the income categories that had money in this year, so paychecks
  aren't "Needs a category" (the budget doesn't show a group without spending categories);
- expected income is switched on with the confirmed amount for this month;
- the money already in the accounts can be kept in Emergency savings as last month's
  leftover: the one server-side write to a past month (``Budget(T-1, savings)``), made only
  here, when the vault has no budget rows yet.

Plaid's own "Food and drink" and "Transportation" stay out of the plan. Phase 2: the setup
records its Groceries / Eating out / Gas categories (``app_settings.budget_auto_map``) and
re-applies the rules, so bank-set transactions with Plaid's detailed code are sorted into
them from now on (services/automap.py: never over hand-set categories or user rules).
Older transactions without a detailed code stay where they are (they show under "Spending
without a plan" and "Needs a category").

Phase 2 bills: the Bills row starts from this month's recurring bills (the calendar's
occurrences, one source of truth); every visible spending category with a bill this month
joins the Bills row (and the Bills group when groups are made), gets its bill total first,
and gets a ``bills`` target ("Cover my bills") unless it already has a target.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..models import Budget, CategoryGroup, Transaction, TxnCategory
from ..utils import fmt_usd, from_cents, month_bounds, month_of, shift_month
from . import automap
from . import categories as cat_service
from . import rules as rules_service
from . import spending
from .categories import CategoryMap
from .categories import load as load_categories
from .spending import BudgetError

WINDOW = 3  # complete months averaged
SAVINGS_NAME = "Emergency savings"
SUPERSEDED = ("FOOD_AND_DRINK", "TRANSPORTATION")
BILLS = ("RENT_AND_UTILITIES", "GENERAL_SERVICES")
FUN = ("ENTERTAINMENT", "TRAVEL")
OTHER_BUCKET = (
    "OTHER", "GENERAL_MERCHANDISE", "PERSONAL_CARE", "MEDICAL", "HOME_IMPROVEMENT",
    "BANK_FEES", "GOVERNMENT_AND_NON_PROFIT",
)
ROW_KEYS = ("groceries", "gas", "eating_out", "bills", "fun", "other")


@dataclass(frozen=True)
class RowSpec:
    key: str
    label: str
    hint: str
    fallback_chips: tuple[int, ...]  # dollars, when there's no history
    custom: str | None = None  # the custom category this row fills
    bucket: tuple[str, ...] = ()  # the built-in categories this row is split across


ROWS = (
    RowSpec("groceries", "Groceries", "Food and household items from the store", (400, 600, 800), custom="Groceries"),
    RowSpec("gas", "Gas", "For the car", (100, 200, 300), custom="Gas"),
    RowSpec("eating_out", "Eating out", "Restaurants, takeout and coffee", (100, 250, 400), custom="Eating out"),
    RowSpec("bills", "Bills", "Rent or mortgage, utilities, phone and internet", (1000, 1500, 2000), bucket=BILLS),
    RowSpec("fun", "Fun", "Hobbies, movies, streaming and trips", (100, 200, 300), bucket=FUN),
    RowSpec("other", "Other", "Anything else: shopping, haircuts, doctor visits, gifts", (200, 300, 500), bucket=OTHER_BUCKET),
)

# "@key" = the custom category of that row; "@savings" = Emergency savings; "@extras" = any
# other spending category the "Other" row fills.
GROUPS = (
    ("Bills", (*BILLS, "@bill_extras")),
    ("Everyday", ("@groceries", "@eating_out", "@gas", "FOOD_AND_DRINK", "TRANSPORTATION",
                  "GENERAL_MERCHANDISE", "PERSONAL_CARE", "MEDICAL")),
    ("Fun", FUN),
    ("Other and saving", ("OTHER", "HOME_IMPROVEMENT", "BANK_FEES", "GOVERNMENT_AND_NON_PROFIT", "@savings", "@extras")),
    # Income categories that had money in this year, so paychecks aren't "Needs a category"
    # once groups exist (the budget itself doesn't show a group without spending categories).
    ("Income", ("@income",)),
)


class SetupDone(ValueError):
    """409: the budget already has plans or the setup already ran."""


def is_needed(session: Session) -> bool:
    return not spending.has_budget_rows(session) and not spending.setup_done(session)


# ------------------------------------------------------------------ history


@dataclass
class History:
    """Spending history as the setup sees it. Keys are category ids, plus ``"@groceries"``,
    ``"@eating_out"`` and ``"@gas"``: what the setup's automatic sorting (services/automap.py)
    will move into those rows once it runs (moved out of the category they're in now)."""

    cmap: CategoryMap
    book: spending.Book
    current: str
    averages: dict[str, int]  # category -> average monthly spend (cents) over the window
    this_month: dict[str, int]  # category -> spent this month (cents)
    has_window: bool  # at least one window month is on or after the first spending month

    def average(self, category: str) -> int:
        return self.averages.get(category, 0)


def _auto_moves(session: Session, cmap: CategoryMap, account_ids: list[int], start: str, end: str) -> list[tuple[str, str, str, int]]:
    """(month, category now, row key, cents) for each bank-set transaction on the budget
    accounts that the setup's automatic sorting will move into the Groceries / Eating out /
    Gas rows: a mapped Plaid detailed code, not hand-set or split, and no user rule ahead of
    the mapping (``rules.resolve`` decides, exactly like the real run in ``run_setup``).
    Empty once the sorting has run (moved rows are ``auto``, not ``plaid``/``rule``)."""
    if not account_ids:
        return []
    first, _ = month_bounds(start)
    _, last = month_bounds(end)
    matchers = rules_service.enabled_matchers(session)
    rows = session.execute(
        select(
            Transaction.date, Transaction.name, Transaction.merchant_name, Transaction.amount_cents,
            Transaction.category, Transaction.plaid_category, Transaction.plaid_detailed,
            Transaction.is_transfer, Transaction.transfer_source,
        ).where(
            Transaction.account_id.in_(account_ids), Transaction.is_transfer.is_(False),
            Transaction.category_source.in_(("plaid", "rule")),
            Transaction.plaid_detailed.in_(list(automap.DETAILED_TO_ROW)),
            Transaction.date >= first, Transaction.date <= last,
            ~rules_service._has_splits(),  # noqa: SLF001
        )
    )
    out = []
    for r in rows:
        want = rules_service.resolve(
            matchers, name=r.name, merchant=r.merchant_name, amount_cents=r.amount_cents,
            plaid_category=r.plaid_category, is_transfer=False, transfer_source=r.transfer_source,
            plaid_detailed=r.plaid_detailed, auto_map=automap.DETAILED_TO_ROW,
        )
        if want["category_source"] == automap.AUTO_SOURCE and not want["is_transfer"]:
            out.append((month_of(r.date), cmap.get(r.category).id, want["category"], int(r.amount_cents)))
    return out


def _coded_months(session: Session, account_ids: list[int], months: list[str]) -> set[str]:
    """The ``months`` with at least one transaction on the budget accounts that has Plaid's
    detailed code (stored since schema v7)."""
    if not account_ids or not months:
        return set()
    first, _ = month_bounds(months[0])
    _, last = month_bounds(months[-1])
    month_col = func.strftime("%Y-%m", Transaction.date)
    return set(session.scalars(
        select(month_col).where(
            Transaction.account_id.in_(account_ids), Transaction.plaid_detailed.is_not(None),
            Transaction.date >= first, Transaction.date <= last,
        ).distinct()
    ))


def _history(session: Session, today: dt.date) -> History:
    cmap = load_categories(session)
    book = spending.load_book(session, cmap, today)
    current = book.current
    ids = [a.id for a, inc in book.accounts if inc]
    first = spending._first_spending_month(session, cmap)  # noqa: SLF001
    window = [shift_month(current, -k) for k in range(WINDOW, 0, -1)]
    counted = [m for m in window if first is not None and m >= first]
    net = spending._envelope_net(session, cmap, ids, counted[0], counted[-1]) if counted else {}  # noqa: SLF001
    net = {m: dict(v) for m, v in net.items()}
    net[current] = dict(book.net.get(current, {}))
    # Phase 2: the rows the automatic sorting will fill count as those rows' spending.
    for month, now, key, cents in _auto_moves(session, cmap, ids, counted[0] if counted else current, current):
        by_cat = net.setdefault(month, {})
        if cmap.kind(now) == "spending":
            by_cat[now] = by_cat.get(now, 0) - cents
        by_cat[f"@{key}"] = by_cat.get(f"@{key}", 0) + cents
    # Plaid's detailed code is only stored from schema v7 on: a vault upgraded with older
    # history would average too little, so those rows' averages need a code in every month.
    covered = _coded_months(session, ids, counted) == set(counted)
    averages: dict[str, int] = {}
    if counted:
        totals: dict[str, int] = {}
        for m in counted:
            for category, value in net.get(m, {}).items():
                if category.startswith("@") and not covered:
                    continue
                totals[category] = totals.get(category, 0) + max(0, -value)
        averages = {c: v // len(counted) for c, v in totals.items() if v > 0}
    this_month = {c: max(0, -v) for c, v in net[current].items() if v < 0}
    return History(cmap, book, current, averages, this_month, bool(counted))


def _visible_spending(cmap: CategoryMap, category: str) -> bool:
    info = cmap.by_id.get(category)
    return info is not None and info.kind == "spending" and not info.hidden


def _existing_spending(session: Session, name: str) -> TxnCategory | None:
    found = cat_service.find_by_name(session, name)
    return found if found is not None and found.kind == "spending" else None


def _buckets(
    cmap: CategoryMap, customs: set[str], savings: str | None, bill_cats: set[str] | frozenset = frozenset(),
) -> dict[str, list[str]]:
    """row key -> the visible spending categories it fills (extras join "other"). Categories
    with a bill this month (``bill_cats``) join "bills" and leave the other rows."""
    reserved = set(SUPERSEDED) | customs | ({savings} if savings else set())
    moved = [
        c.id for c in cmap.ordered("spending")
        if c.id in bill_cats and c.id not in reserved and c.id not in BILLS and not c.hidden
    ]
    out = {
        spec.key: [c for c in spec.bucket if _visible_spending(cmap, c) and c not in moved]
        for spec in ROWS if spec.bucket
    }
    out["bills"] += moved
    known = {c for spec in ROWS for c in spec.bucket} | reserved | set(moved)
    out["other"] += [c.id for c in cmap.ordered("spending") if not c.hidden and c.id not in known]
    return out


def _month_bills(session: Session, h: History, today: dt.date) -> tuple[dict[str, int], int]:
    """(visible spending category -> this month's bill total in cents, how many bills);
    bills paid from the budget's accounts or a credit card (``spending.bill_account_ids``)."""
    cmap = h.cmap
    bills = spending.month_bills(session, month_of(today), today, h.book) or {}
    totals: dict[str, int] = {}
    count = 0
    for category, occs in bills.items():
        if not _visible_spending(cmap, category):
            continue
        total = spending.bill_total(occs)
        if total > 0:
            totals[category] = total
            count += sum(1 for o in occs if o.status != "skipped")
    return totals, count


def _bill_extras(h: History, buckets: dict[str, list[str]], bill_totals: dict[str, int]) -> dict[str, int]:
    """Categories that joined the Bills row from elsewhere (a bill in Entertainment) ->
    their usual spending besides the bills (cents): the window's average minus this month's
    bill total, never below 0. Their plan is bills + this, so a category with a streaming
    bill and movie nights isn't "Over" on day one. (An estimate: the history doesn't say
    which past payments were the bill.)"""
    out = {}
    for category in buckets["bills"]:
        if category in BILLS or category not in bill_totals:
            continue
        extra = max(0, h.average(category) - bill_totals[category])
        if extra > 0:
            out[category] = extra
    return out


def _bill_chips(total: int) -> tuple[list[float], float]:
    """Quick picks from the real bill total: the total (whole dollars, rounded up), then the
    next two $50 steps above it."""
    whole = -(-total // 100)
    step = (whole // 50 + 1) * 50
    return [float(whole), float(step), float(step + 50)], float(whole)


def _chips(average: int | None, fallback: tuple[int, ...]) -> tuple[list[float], float]:
    """Quick picks from the real average (±25%, rounded to $50), else the design's."""
    def r50(cents: float) -> int:
        return max(50, int(round(cents / 5000)) * 50)

    if average is not None and average >= 1000:
        chips = sorted({r50(average * 0.75), r50(average), r50(average * 1.25)})
        return [float(c) for c in chips], float(r50(average))
    return [float(c) for c in fallback], float(fallback[len(fallback) // 2])


def _income_start(inc: spending.Income, month: str) -> int | None:
    """The month's income the setup screen starts from (cents), so the month doesn't read
    "Paycheck was short" right after setup:

    - paychecks on the calendar this month: what came in plus what's still expected (the
      pay schedule knows this month; a two-paycheck month isn't the average of months that
      had three, and a three-paycheck month is more than it);
    - otherwise: the larger of the 3-month average and what already came in.
    """
    received = inc.received_if_expected
    start, end = month_bounds(month)
    scheduled = any(start <= o.date <= end and o.status != "skipped" for o in inc.occurrences)
    this_month = received + inc.still_expected(month)
    if scheduled and this_month > 0:
        return this_month
    return max(inc.suggested or 0, received) or None


# ------------------------------------------------------------------ GET /api/budgets/setup


def setup_info(session: Session, today: dt.date) -> dict:
    h = _history(session, today)
    customs: dict[str, TxnCategory | None] = {
        spec.key: _existing_spending(session, spec.custom) for spec in ROWS if spec.custom
    }
    savings = _existing_spending(session, SAVINGS_NAME)
    bill_totals, bill_count = _month_bills(session, h, today)
    buckets = _buckets(
        h.cmap, {c.id for c in customs.values() if c is not None}, savings.id if savings else None, set(bill_totals)
    )
    extras = _bill_extras(h, buckets, bill_totals)
    rows = []
    for spec in ROWS:
        if spec.custom:
            existing = customs[spec.key]
            categories = [spec.custom]
            # What the category already has, plus what the automatic sorting will move in.
            average = (h.average(existing.id) if existing is not None else 0) + h.average(f"@{spec.key}")
            if not h.has_window or (existing is None and not average):
                average = None
        else:
            categories = [h.cmap.get(c).name for c in buckets[spec.key]]
            average = sum(h.average(c) for c in buckets[spec.key]) if h.has_window else None
        chips, suggested = _chips(average, spec.fallback_chips)
        if spec.key == "bills" and bill_totals:
            # "We found $1,890 in bills on your calendar": start from the real bills, plus
            # the usual other spending of categories that joined the row for a bill.
            chips, suggested = _bill_chips(sum(bill_totals.values()) + sum(extras.values()))
        rows.append({
            "key": spec.key, "label": spec.label, "hint": spec.hint, "categories": categories,
            "average": from_cents(average) if average else None, "chips": chips, "suggested": suggested,
        })
    inc = h.book.income
    existing = _existing_money(h, buckets, customs, savings)
    return {
        "month": h.current,
        "needed": is_needed(session),
        "income": {
            # This month's paychecks when one is on the calendar, else the last 3 complete
            # months' average (Settings D7: ``suggested_source`` says which).
            "suggested": from_cents(inc.suggested),
            "suggested_source": inc.suggested_source,
            "received": from_cents(inc.received_if_expected),  # the setup switches expected income on
            "expected_more": from_cents(inc.still_expected(h.current)),  # paychecks still to come this month
            "start": from_cents(_income_start(inc, h.current)),  # the amount the screen starts from
        },
        "rows": rows,
        # This month's recurring bills with a budget category (calendar occurrences).
        "bills": {
            "total": from_cents(sum(bill_totals.values())),
            "count": bill_count,
            "by_category": {c: from_cents(v) for c, v in bill_totals.items()},
            # Categories that joined the row for a bill: their other usual spending.
            "extra": from_cents(sum(extras.values())),
            "extra_categories": [h.cmap.get(c).name for c in buckets["bills"] if c in extras],
        } if bill_totals else None,
        "existing_money": from_cents(max(0, existing)),
        # Card debt larger than the cash: it comes out of this month's income, so the plan
        # can be at most ``income - owed_beyond_cash``.
        "owed_beyond_cash": from_cents(max(0, -existing)),
    }


def _existing_money(h: History, buckets: dict[str, list[str]], customs: dict, savings) -> int:  # noqa: ANN001
    """Money already there before this month's income (signed): cash + this month's spending
    in the plan's categories − income received. Below zero when cards owe more than the
    accounts hold."""
    members = {c for cats in buckets.values() for c in cats}
    members |= {c.id for c in customs.values() if c is not None}
    members |= {f"@{key}" for key in customs}  # moved in by the automatic sorting
    if savings is not None:
        members.add(savings.id)
    spent = sum(h.this_month.get(c, 0) for c in members)
    return h.book.cash_total + spent - h.book.income.received_if_expected


def _existing_now(session: Session, today: dt.date) -> int:
    """``_existing_money`` as the setup screen showed it (before setup makes any category)."""
    h = _history(session, today)
    customs = {spec.key: _existing_spending(session, spec.custom) for spec in ROWS if spec.custom}
    savings = _existing_spending(session, SAVINGS_NAME)
    bill_totals, _ = _month_bills(session, h, today)
    buckets = _buckets(
        h.cmap, {c.id for c in customs.values() if c is not None}, savings.id if savings else None, set(bill_totals)
    )
    return _existing_money(h, buckets, customs, savings)


# ------------------------------------------------------------------ POST /api/budgets/setup


def _split(total: int, categories: list[str], weights: dict[str, int]) -> dict[str, int]:
    """``total`` cents across categories by weight (floors; the remainder goes to the largest).
    Without any weight it all goes to the first (main) category."""
    if not categories or total <= 0:
        return {}
    weighted = [c for c in categories if weights.get(c, 0) > 0]
    if not weighted:
        return {categories[0]: total}
    whole = sum(weights[c] for c in weighted)
    shares = {c: total * weights[c] // whole for c in weighted}
    largest = max(weighted, key=lambda c: (weights[c], -weighted.index(c)))
    shares[largest] += total - sum(shares.values())
    return shares


def _split_bills(
    total: int, categories: list[str], bills: dict[str, int], weights: dict[str, int],
    extras: dict[str, int] | None = None,
) -> dict[str, int]:
    """The Bills row: each category with bills gets its bill total first; next, each
    category that joined the row for a bill gets its usual other spending (``extras``,
    ``_bill_extras``); the rest is split by ``weights`` (``_split``). An answer below the
    bills is split by bill total; one below bills + extras gives what's left after the
    bills to the extras by size."""
    extras = {c: v for c, v in (extras or {}).items() if v > 0 and c in categories}
    billed = [c for c in categories if bills.get(c, 0) > 0]
    need = sum(bills[c] for c in billed)
    if not billed or total <= 0:
        return _split(total, categories, weights)
    if total <= need:
        return _split(total, billed, bills)
    shares = {c: bills[c] for c in billed}
    left = total - need
    more = sum(extras.values())
    if left <= more:
        parts = _split(left, list(extras), extras)
    else:
        parts = dict(extras)
        for category, cents in _split(left - more, categories, weights).items():
            parts[category] = parts.get(category, 0) + cents
    for category, cents in parts.items():
        shares[category] = shares.get(category, 0) + cents
    return shares


def _ensure_category(session: Session, name: str) -> TxnCategory:
    found = cat_service.find_by_name(session, name)
    if found is None:
        return cat_service.create_custom(session, name, "spending")
    if found.kind != "spending":
        raise BudgetError(f"A category named “{found.name}” already exists but isn't for spending. Rename it first.")
    found.hidden = False
    return found


def _income_categories(session: Session, cmap: CategoryMap, today: dt.date) -> list[str]:
    """Income-kind categories with money in since January 1 (any account, transfers out)."""
    found = set(session.scalars(
        select(Transaction.category).where(
            Transaction.date >= dt.date(today.year, 1, 1), Transaction.amount_cents > 0,
            Transaction.is_transfer.is_(False), Transaction.category.is_not(None),
        ).distinct()
    ))
    return [c.id for c in cmap.ordered("income") if c.id in found and not c.hidden]


def _group_categories(
    session: Session, custom_ids: dict[str, str], savings_id: str, extras: list[str], bill_extras: list[str] = (),
    income: list[str] = (),
) -> None:
    """Create the starter groups when there are none; only ungrouped categories join them.
    A vault that already has groups keeps them as they are (only the new rows join)."""
    if session.scalar(select(func.count()).select_from(CategoryGroup)):
        # Groups exist: the new rows join the group of the category they come from.
        for key, source in (("groceries", "FOOD_AND_DRINK"), ("eating_out", "FOOD_AND_DRINK"), ("gas", "TRANSPORTATION")):
            source_row = session.get(TxnCategory, source)
            if source_row is not None and source_row.group_id is not None:
                session.execute(
                    update(TxnCategory)
                    .where(TxnCategory.id == custom_ids[key], TxnCategory.group_id.is_(None))
                    .values(group_id=source_row.group_id)
                )
        return
    for position, (name, members) in enumerate(GROUPS):
        if members == ("@income",) and not income:
            continue
        group = CategoryGroup(name=name, position=position)
        session.add(group)
        session.flush()
        ids: list[str] = []
        for member in members:
            if member == "@savings":
                ids.append(savings_id)
            elif member == "@extras":
                ids += extras
            elif member == "@bill_extras":
                ids += list(bill_extras)
            elif member == "@income":
                ids += list(income)
            elif member.startswith("@"):
                ids.append(custom_ids[member[1:]])
            else:
                ids.append(member)
        session.execute(
            update(TxnCategory)
            .where(TxnCategory.id.in_(ids), TxnCategory.group_id.is_(None))
            .values(group_id=group.id)
        )
    session.flush()


def run_setup(
    session: Session, today: dt.date, *, answers: dict[str, int], income: int, keep_existing: bool, skip: bool,
) -> int:
    """Make the first plan. Returns the cents kept in Emergency savings (0 if none)."""
    if not is_needed(session):
        raise SetupDone("Your budget is already set up.")
    current = month_of(today)
    first_look = spending.load_book(session, load_categories(session), today)
    received = first_look.income.received_if_expected  # expected income is switched on below
    if income < received:
        raise BudgetError(f"{fmt_usd(received)} has already come in this month, so pick at least {fmt_usd(received)}.")
    if not skip:
        owed = max(0, -_existing_now(session, today))
        if sum(answers.values()) > income - owed:
            if owed:
                raise BudgetError(
                    f"Your cards owe {fmt_usd(owed)} more than your accounts hold, so plan at most "
                    f"{fmt_usd(max(0, income - owed))}. Lower an amount to continue."
                )
            raise BudgetError("Lower an amount to continue.")

    custom_ids = {spec.key: _ensure_category(session, spec.custom).id for spec in ROWS if spec.custom}
    savings_id = _ensure_category(session, SAVINGS_NAME).id
    session.flush()
    # Phase 2: from now on bank-set transactions with Plaid's detailed code go to these rows
    # (ones already synced with a code move now); user rules and hand-set categories win.
    automap.record(session, {key: custom_ids[key] for key in automap.ROWS})
    rules_service.apply_all(session)
    session.flush()

    h = _history(session, today)
    bill_totals, _ = _month_bills(session, h, today)
    buckets = _buckets(h.cmap, set(custom_ids.values()), savings_id, set(bill_totals))
    extras = _bill_extras(h, buckets, bill_totals)
    known = {c for spec in ROWS for c in spec.bucket}
    _group_categories(
        session, custom_ids, savings_id, [c for c in buckets["other"] if c not in known],
        [c for c in buckets["bills"] if c not in BILLS], _income_categories(session, h.cmap, today),
    )

    plans: dict[str, int] = {c: 0 for c in custom_ids.values()}
    plans[savings_id] = 0
    for key, cats in buckets.items():
        for category in cats:
            if h.average(category) > 0 or h.this_month.get(category, 0) > 0 or bill_totals.get(category, 0) > 0:
                plans[category] = 0  # a member: it has spending or bills
    if skip:
        # Plans start at what's been spent so far, so nothing reads "Over by" on day one.
        plans = {c: h.this_month.get(c, 0) for c in plans}
        plans[savings_id] = 0
    else:
        for spec in ROWS:
            amount = answers.get(spec.key, 0)
            if spec.custom:
                plans[custom_ids[spec.key]] = amount
                continue
            # Weight by the window's average; a category that only has spending this month
            # (a brand-new history) is weighted by that.
            weights = {c: h.average(c) or h.this_month.get(c, 0) for c in buckets[spec.key]}
            if spec.key == "bills":
                shares = _split_bills(amount, buckets[spec.key], bill_totals, weights, extras)
            else:
                shares = _split(amount, buckets[spec.key], weights)
            for category, cents in shares.items():
                plans[category] = cents
    # "Cover my bills" for each category with bills this month that has no target yet.
    for category in bill_totals:
        row = session.get(TxnCategory, category)
        if row is not None and row.target_kind is None and category in plans:
            row.target_kind, row.target_cents, row.target_date = "bills", None, None

    for category, cents in plans.items():
        session.add(Budget(month=current, category=category, limit_cents=cents))
    settings = spending.load_income_settings(session)
    settings.mode = "expected"
    settings.months[current] = income
    spending.store_income_settings(session, settings, today)
    spending.put_setting(session, spending.BUDGET_SAVINGS_SETTING, savings_id)
    spending.put_setting(session, spending.BUDGET_SETUP_DONE_SETTING, "1")
    spending.put_setting(session, spending.BUDGET_SETUP_SEED_SETTING, None)
    session.flush()

    if not keep_existing:
        return 0
    cmap = load_categories(session)
    book = spending.load_book(session, cmap, today)
    extra = book.ready_to_assign() + book.planned(current) - income
    if extra <= 0:
        return 0
    previous = shift_month(current, -1)
    ids = [a.id for a, inc in book.accounts if inc]
    line = book.members(previous).get(savings_id)
    if line is not None:
        # Setup runs only without budget rows, so this is a safety net: keep what the month
        # already planned (its row or, Release 3.17, a repeated plan) and cover its overspending.
        value = int(line.assigned) + extra + max(0, -line.available)
    else:
        spent_before = max(0, -spending._envelope_net(session, cmap, ids, previous, previous).get(previous, {}).get(savings_id, 0))  # noqa: SLF001
        value = extra + spent_before
    # Last month's leftover = extra, so Money for {Month} is exactly the expected income.
    row = session.scalar(select(Budget).where(Budget.month == previous, Budget.category == savings_id))
    if row is None:
        session.add(Budget(month=previous, category=savings_id, limit_cents=value))
    else:
        row.limit_cents, row.removed, row.moved_cents = value, False, 0
    spending.put_setting(
        session, spending.BUDGET_SETUP_SEED_SETTING, json.dumps({"month": previous, "category": savings_id})
    )
    session.flush()
    return extra


# ------------------------------------------------------------------ POST /api/budgets/{month}/categories


def add_category(session: Session, month: str, name: str, group_id: int | None, plan: int, today: dt.date) -> TxnCategory:
    """Create a spending category and give it a plan in ``month`` (atomic with the caller's commit).

    Raises EmptyName (422), NameTaken (409) or BudgetError (422).
    """
    spending.check_editable_month(month, today)
    name = cat_service.clean_name(name)
    cat_service.ensure_unique(session, name)
    if group_id is not None and session.get(CategoryGroup, group_id) is None:
        raise BudgetError("unknown group")
    cmap = load_categories(session)
    free = max(0, spending.load_book(session, cmap, today, month).ready_to_assign())
    if plan > free:
        raise BudgetError(f"Only {fmt_usd(free)} isn't planned yet. Use a smaller amount, or change the month's amount.")
    category = cat_service.create_custom(session, name, "spending", group_id=group_id)
    spending.save_budget_cents(session, load_categories(session), month, {category.id: plan}, [], today)
    return category

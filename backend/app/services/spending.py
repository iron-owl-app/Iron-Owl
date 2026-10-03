"""Budgets and spending math (SPEC "Spending definitions", "Release 2.1: Envelope budgets").

A spending transaction: account category bank|credit|hsa|other and not hidden,
is_transfer false, effective category of kind ``spending`` (pending counts).
``spent`` per category = max(0, -sum(amount)) so refunds net out.

Envelope budgets: every budgeted category is an envelope with
``available = carryover + assigned - spent``. Positive leftovers roll into the next month;
overspending doesn't (it comes out of Ready to Assign). All math is in integer cents.
"""
from __future__ import annotations

import calendar
import datetime as dt
import json
import logging
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..models import Account, AppSetting, Budget, CategoryGroup, RecurringItem, Transaction, TxnCategory
from ..utils import (
    LIABILITY_CATEGORIES,
    days_in_month,
    fmt_usd,
    from_cents,
    month_bounds,
    month_of,
    month_str,
    parse_month,
    shift_month,
    to_cents,
)
from . import calendar as occ_calendar
from . import goal_links, ledger, prefs
from .categories import CategoryMap, Target, target_out
from .categories import load as load_categories
from .recurring import display_name, item_aliases, match_keys, merchant_key

log = logging.getLogger("fintrack.spending")

SPEND_ACCOUNT_CATEGORIES = ("bank", "credit", "hsa", "other")

# Accounts that can fund the budget, and the default selection when none was saved.
BUDGET_ACCOUNT_CATEGORIES = ("bank", "credit", "other")
DEFAULT_BUDGET_ACCOUNT_CATEGORIES = ("bank", "credit")
BUDGET_ACCOUNTS_SETTING = "budget_account_ids"

PLAN_AHEAD_MONTHS = 12
MAX_AMOUNT_CENTS = 100_000_000_000_000  # $1 trillion, like every other money field
PAST_MONTH_READ_ONLY = "Past months are read-only. Make changes in the current month."


class BudgetError(ValueError):
    """Validation failure; the message is safe to show (422)."""


# ------------------------------------------------------------------ budget settings (Release 3.6)
# Stored in app_settings (no migration). SPEC "Release 3.6: Budget for a beginner".

BUDGET_INCOME_SETTING = "budget_income"  # {"mode": "expected"|"off", "months": {"YYYY-MM": cents}}
BUDGET_SAVINGS_SETTING = "budget_savings_category"  # a spending category id
BUDGET_SETUP_DONE_SETTING = "budget_setup_done"  # "1" once the guided setup ran
# {"month": "YYYY-MM", "category": id}: the Emergency savings row setup wrote into T−1 for the
# money the user already had (not a real plan: Quick fill and the Detailed view say so).
BUDGET_SETUP_SEED_SETTING = "budget_setup_seed"
# Release 3.17: "YYYY-MM", the first month whose plans repeat (a member category without a row
# of its own that month plans what it planned last). Set once, to the month this code first
# ran (``start_plans_repeat``); absent = plans don't repeat (the old rule everywhere).
BUDGET_PLANS_REPEAT_SETTING = "budget_plans_repeat_from"
# Release 3.17: "YYYY-MM", the last month whose goal and debt plans were written as rows of their
# own once the month was over (``_freeze_rule_months``).
BUDGET_RULES_FROZEN_SETTING = "budget_rules_frozen_through"
INCOME_MODES = ("expected", "off")
INCOME_KEEP_BEFORE = 3  # stored months kept: [T - 3, T + 12]
SUGGEST_MONTHS = 3  # suggested = average of the last 3 complete months with income
SUGGEST_ROUND_CENTS = 1000  # rounded down to $10
INCOME_EVENT_MIN_CENTS = 100  # "Earned more" / "Paycheck was short" need at least $1
# With no active income recurring item to say when the next paycheck comes, a short month
# is called out from the 21st on (otherwise it would show on the 1st, before payday).
SHORT_FALLBACK_AFTER_DAY = 20
# "Next paycheck expected" looks this far ahead of today.
INCOME_AHEAD_DAYS = 62
# Deposits that count as money that came in: income, and "transfer" kind money that isn't a
# matched transfer between the user's own accounts (a client paying by Zelle or Venmo is
# often categorized Transfer in). Refunds (spending kinds) never count.
RECEIVED_KINDS = frozenset({"income", "transfer"})


def get_setting(session: Session, key: str) -> str | None:
    row = session.get(AppSetting, key)
    return row.value if row is not None else None


def put_setting(session: Session, key: str, value: str | None) -> None:
    row = session.get(AppSetting, key)
    if value is None:
        if row is not None:
            session.delete(row)
    elif row is None:
        session.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    session.flush()


@dataclass
class IncomeSettings:
    """``mode`` "expected" counts paychecks still expected this month; "off" = money already in."""

    mode: str = "off"
    months: dict[str, int] = field(default_factory=dict)


def load_income_settings(session: Session) -> IncomeSettings:
    raw = get_setting(session, BUDGET_INCOME_SETTING)
    if not raw:
        return IncomeSettings()
    try:
        data = json.loads(raw)
    except ValueError:
        return IncomeSettings()
    if not isinstance(data, dict):
        return IncomeSettings()
    mode = data.get("mode") if data.get("mode") in INCOME_MODES else "off"
    months: dict[str, int] = {}
    stored = data.get("months")
    if isinstance(stored, dict):
        for key, value in stored.items():
            try:
                parse_month(key)
            except ValueError:
                continue
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_AMOUNT_CENTS:
                months[key] = value
    return IncomeSettings(mode, months)


def store_income_settings(session: Session, settings: IncomeSettings, today: dt.date) -> None:
    current = month_of(today)
    first, last = shift_month(current, -INCOME_KEEP_BEFORE), latest_month(today)
    months = {m: c for m, c in sorted(settings.months.items()) if first <= m <= last}
    put_setting(session, BUDGET_INCOME_SETTING, json.dumps({"mode": settings.mode, "months": months}))


def savings_category(session: Session, cmap: CategoryMap) -> str | None:
    """The Emergency savings category (by setting, never by name); None unless it's spending kind."""
    value = get_setting(session, BUDGET_SAVINGS_SETTING)
    if value and value in cmap.by_id and cmap.kind(value) == "spending":
        return value
    return None


def setup_done(session: Session) -> bool:
    return get_setting(session, BUDGET_SETUP_DONE_SETTING) == "1"


def setup_seed(session: Session, month: str) -> str | None:
    """The category of setup's "money you already had" row in ``month``, if it wrote one there."""
    raw = get_setting(session, BUDGET_SETUP_SEED_SETTING)
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("month") != month or not isinstance(data.get("category"), str):
        return None
    return data["category"]


def has_budget_rows(session: Session) -> bool:
    return session.scalar(select(Budget.id).limit(1)) is not None


@dataclass(frozen=True)
class Income:
    """Today's month's income on the budget accounts (all in cents)."""

    mode: str
    received: int
    suggested: int | None
    override: int | None
    by_month: dict[str, int] = field(default_factory=dict)
    # Phase 2: income occurrences (services/calendar.py) of the active paychecks on the budget
    # accounts, effective dates from a week before today's month to ``INCOME_AHEAD_DAYS`` ahead.
    occurrences: tuple = ()
    # Deposits matched to a paycheck that aren't income-kind (``matched_deposits``). They
    # count in ``received`` only in "expected" mode: with the switch off every number stays
    # exactly what it was before phase 2.
    matched: int = 0
    # Settings (D7): where ``suggested`` came from: "paychecks" (this month's paychecks, when
    # there is an active income item on the budget accounts) or "average" (3 months).
    suggested_source: str = "average"

    @property
    def received_if_expected(self) -> int:
        """``received`` as "expected" mode counts it (matched deposits in), e.g. for the
        guided setup, which switches expected income on."""
        return self.received if self.mode == "expected" else self.received + self.matched

    def still_expected(self, month: str) -> int:
        """Paychecks still expected in ``month`` (calendar occurrences due or a few days
        late, not yet matched to a deposit), in cents."""
        start, end = month_bounds(month)
        return sum(
            int(o.amount_cents) for o in self.occurrences
            if start <= o.date <= end and o.status in ("upcoming", "pending")
        )

    @property
    def expected(self) -> int | None:
        return self.override if self.override is not None else self.suggested

    def pending_for(self, expected: int | None) -> int:
        """Money still expected this month; only counts in "expected" mode."""
        if self.mode != "expected" or expected is None:
            return 0
        return max(0, expected - self.received)

    @property
    def pending(self) -> int:
        return self.pending_for(self.expected)


NO_INCOME = Income("off", 0, None, None)


def received_by_month(
    session: Session, cmap: CategoryMap, account_ids: list[int], first: str, last: str, *where: Any
) -> dict[str, int]:
    """month -> Σ positive ledger lines of a ``RECEIVED_KINDS`` category on the budget accounts
    (matched transfers out; pending and split parts in), aggregated in SQL. A posted deposit
    replaces its pending version (Plaid removes it), so nothing is counted twice.
    ``where`` narrows the transactions (e.g. to one sync's new ones)."""
    if not account_ids:
        return {}
    start, _ = month_bounds(first)
    _, end = month_bounds(last)
    lines = ledger.lines(
        Transaction.account_id.in_(account_ids),
        Transaction.is_transfer.is_(False),
        Transaction.date >= start,
        Transaction.date <= end,
        *where,
    )
    month_col = func.strftime("%Y-%m", lines.c.date).label("m")
    out: dict[str, int] = defaultdict(int)
    for month, category, total in session.execute(
        select(month_col, lines.c.category, func.sum(lines.c.amount_cents))
        .where(lines.c.amount_cents > 0)
        .group_by(month_col, lines.c.category)
    ):
        if month is not None and cmap.kind(category) in RECEIVED_KINDS:
            out[month] += int(total or 0)
    return dict(out)


def suggested_income(by_month: dict[str, int], current: str) -> int | None:
    """Average received over the last 3 complete months, skipping months with no income,
    rounded down to $10; None when none of them had income."""
    values = [by_month.get(shift_month(current, -k), 0) for k in range(1, SUGGEST_MONTHS + 1)]
    values = [v for v in values if v > 0]
    if not values:
        return None
    average = sum(values) // len(values)
    rounded = average // SUGGEST_ROUND_CENTS * SUGGEST_ROUND_CENTS
    return rounded or None


def income_items(session: Session, account_ids: list[int]) -> list[RecurringItem]:
    """Active recurring deposits on a budget account (or on no account), transfers left out
    (``transfer_items``)."""
    ids = set(account_ids)
    items = [
        item
        for item in session.scalars(
            select(RecurringItem)
            .where(RecurringItem.status == "active", RecurringItem.amount_cents > 0)
            .order_by(RecurringItem.next_date, RecurringItem.id)
        )
        if item.account_id is None or item.account_id in ids
    ]
    if not items:
        return items
    transfers = transfer_items(session, items)
    return [item for item in items if item.id not in transfers]


TRANSFER_EVIDENCE = 12  # the newest deposits of a series looked at


def transfer_items(session: Session, items: list[RecurringItem]) -> set[int]:
    """Settings (D7): recurring deposits that are money moved, not income, so they are never
    paychecks (Paychecks tab, Budget's suggestion and "next paycheck", "Paycheck was short").

    A series is a transfer when most of its newest deposits (same merchant key; same account,
    or any visible account for an item with no account) are marked as a transfer between the
    user's own accounts or sit in a transfer-kind category (e.g. Transfer in from savings).
    An item whose budget category is an income one, or with no deposits seen yet (added by
    hand), always counts as income.
    """
    kinds = dict(session.execute(select(TxnCategory.id, TxnCategory.kind)).all())
    candidates = [i for i in items if not (i.category_id and kinds.get(i.category_id) == "income")]
    if not candidates:
        return set()
    aliases = item_aliases(session, candidates)  # 3.19: renamed or moved deposits
    keys = set().union(*(match_keys(i, aliases) for i in candidates))
    evidence: dict[tuple[str, int | None], list[tuple]] = defaultdict(list)  # (date, id, moved), newest first
    rows = session.execute(
        select(
            Transaction.id, Transaction.date, Transaction.account_id, Transaction.name, Transaction.merchant_name,
            Transaction.category, Transaction.is_transfer, Account.hidden,
        )
        .join(Account, Account.id == Transaction.account_id)
        .where(Transaction.amount_cents > 0, Transaction.pending.is_(False))
        .order_by(Transaction.date.desc(), Transaction.id.desc())
    )
    for row in rows:
        key = merchant_key(row.merchant_name, row.name)
        if key not in keys:
            continue
        moved = bool(row.is_transfer) or kinds.get(row.category) == "transfer"
        slots = [(key, row.account_id)] + ([] if row.hidden else [(key, None)])
        for slot in slots:
            if len(evidence[slot]) < TRANSFER_EVIDENCE:
                evidence[slot].append((row.date, row.id, moved))
    out = set()
    for item in candidates:
        found = evidence.get((item.merchant_key, item.account_id), [])
        extra = [e for a, k in aliases.get(item.id, ()) for e in evidence.get((k, a), [])]
        if extra:
            found = sorted(set(found + extra), reverse=True)[:TRANSFER_EVIDENCE]
        seen = [moved for _d, _i, moved in found]
        if seen and sum(seen) * 2 > len(seen):
            out.add(item.id)
    return out


def _received_txn_ids(session: Session, cmap: CategoryMap, account_ids: list[int], month: str) -> set[int]:
    """Transactions with a part counted in ``received_by_month`` for ``month``."""
    start, end = month_bounds(month)
    lines = ledger.lines(
        Transaction.account_id.in_(account_ids), Transaction.is_transfer.is_(False),
        Transaction.date >= start, Transaction.date <= end,
    )
    return {
        txn_id
        for txn_id, category in session.execute(select(lines.c.txn_id, lines.c.category).where(lines.c.amount_cents > 0))
        if cmap.kind(category) in RECEIVED_KINDS
    }


def matched_deposits(
    session: Session, cmap: CategoryMap, account_ids: list[int], month: str, occurrences: tuple | list,
) -> int:
    """Phase 2: deposits matched to an expected paycheck (calendar occurrences) that the
    income-kind sum doesn't already count, e.g. a paycheck Plaid filed under Other. Each
    transaction counts once (deduped by transaction id against ``received_by_month``);
    only deposits dated in ``month`` on a budget account, never a transfer between the
    user's own accounts."""
    ids = {
        o.transaction_id for o in occurrences
        if o.status == "paid" and o.transaction_id is not None and o.actual_date is not None
        and month_of(o.actual_date) == month
    }
    if not ids or not account_ids:
        return 0
    ids -= _received_txn_ids(session, cmap, account_ids, month)
    if not ids:
        return 0
    total = session.scalar(
        select(func.sum(Transaction.amount_cents)).where(
            Transaction.id.in_(ids), Transaction.account_id.in_(account_ids),
            Transaction.is_transfer.is_(False), Transaction.amount_cents > 0,
        )
    )
    return int(total or 0)


def income_occurrences(
    session: Session, account_ids: list[int], today: dt.date, items: list[RecurringItem] | None = None,
) -> tuple:
    """Income occurrences of the paychecks on the budget accounts, from a week before
    today's month to ``INCOME_AHEAD_DAYS`` after today (the single source of truth for
    "Next paycheck", "Paycheck was short" and matched deposits). ``items``: the budget
    accounts' ``income_items`` when the caller already has them."""
    if items is None:
        items = income_items(session, account_ids)
    if not items:
        return ()
    start, end = month_bounds(month_of(today))
    lo = start - dt.timedelta(days=occ_calendar.MAX_WINDOW)
    hi = max(end + dt.timedelta(days=occ_calendar.MAX_WINDOW), today + dt.timedelta(days=INCOME_AHEAD_DAYS))
    return tuple(occ_calendar.income_occurrences(session, today, lo, hi, items=items))


def paychecks_in_month(occurrences: tuple | list, month: str) -> int:
    """Settings (D7): the month's paychecks (cents): every income occurrence whose effective
    date (calendar moves applied) is in ``month``, at the item's amount, skipped ones left out."""
    start, end = month_bounds(month)
    return sum(
        int(o.amount_cents) for o in occurrences
        if start <= o.date <= end and o.status != "skipped" and o.amount_cents > 0
    )


def load_income(session: Session, cmap: CategoryMap, account_ids: list[int], today: dt.date) -> Income:
    current = month_of(today)
    settings = load_income_settings(session)
    by_month = received_by_month(session, cmap, account_ids, shift_month(current, -SUGGEST_MONTHS), current)
    items = income_items(session, account_ids)
    occurrences = income_occurrences(session, account_ids, today, items=items)
    matched = matched_deposits(session, cmap, account_ids, current, occurrences)
    if items:
        # Settings (D7): with a paycheck on the calendar, Budget suggests this month's
        # paychecks (a two-paycheck month isn't the average of months that had three).
        suggested, source = paychecks_in_month(occurrences, current) or None, "paychecks"
    else:
        suggested, source = suggested_income(by_month, current), "average"
    return Income(
        mode=settings.mode,
        # Matched deposits count only with expected income on (the switch off = unchanged).
        received=by_month.get(current, 0) + (matched if settings.mode == "expected" else 0),
        suggested=suggested,
        override=settings.months.get(current),
        by_month=by_month,
        occurrences=occurrences,
        matched=matched,
        suggested_source=source,
    )


@dataclass
class Totals:
    spending_net: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    fixed_net: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    income: int = 0
    merchants: dict[str, list] = field(default_factory=lambda: defaultdict(list))

    def spent(self, category: str) -> int:
        return max(0, -self.spending_net.get(category, 0))

    def spent_by_category(self) -> dict[str, int]:
        return {c: self.spent(c) for c in self.spending_net if self.spent(c) > 0}

    @property
    def total_spent(self) -> int:
        return sum(self.spent(c) for c in self.spending_net)

    def fixed_by_category(self) -> dict[str, int]:
        return {c: max(0, -v) for c, v in self.fixed_net.items() if -v > 0}

    @property
    def total_fixed(self) -> int:
        return max(0, -sum(self.fixed_net.values()))


def spending_lines(session: Session, start: dt.date, end: dt.date, *where) -> list:
    """Split-aware ledger lines on the Release 2 spending accounts, transfers excluded.

    Rows have txn_id, split_id, account_id, date, amount_cents, category, name, merchant_name,
    pending.
    """
    lines = ledger.lines(Transaction.is_transfer.is_(False), Transaction.date >= start, Transaction.date <= end, *where)
    return session.execute(
        select(
            lines.c.txn_id, lines.c.split_id, lines.c.account_id, lines.c.date, lines.c.amount_cents,
            lines.c.category, lines.c.name, lines.c.merchant_name, lines.c.pending,
        )
        .join(Account, Account.id == lines.c.account_id)
        .where(Account.category.in_(SPEND_ACCOUNT_CATEGORIES), Account.hidden.is_(False))
        .order_by(lines.c.date, lines.c.txn_id, lines.c.split_id)
    ).all()


def add_line(out: Totals, cmap: CategoryMap, row) -> None:  # noqa: ANN001
    info = cmap.get(row.category)
    if info.kind == "spending":
        out.spending_net[info.id] += row.amount_cents
        out.merchants[merchant_key(row.merchant_name, row.name)].append(row)
    elif info.kind == "income":
        if row.amount_cents > 0:
            out.income += row.amount_cents
    elif info.kind == "fixed":
        out.fixed_net[info.id] += row.amount_cents


def totals(session: Session, cmap: CategoryMap, start: dt.date, end: dt.date) -> Totals:
    out = Totals()
    for row in spending_lines(session, start, end):
        add_line(out, cmap, row)
    return out


def totals_by_month(session: Session, cmap: CategoryMap, first: str, last: str) -> dict[str, Totals]:
    """``Totals`` for every month in [first, last] (one query)."""
    start, _ = month_bounds(first)
    _, end = month_bounds(last)
    out = {_from_index(i): Totals() for i in range(_index(first), _index(last) + 1)}
    for row in spending_lines(session, start, end):
        add_line(out[month_of(row.date)], cmap, row)
    return out


def first_data_day(session: Session) -> dt.date | None:
    """The date of the first transaction on the spending accounts (every visible bank, card,
    HSA and other account; any kind, transfers too): where Home's and Reports' "has data"
    and "partial first month" start (Release 3.10)."""
    return session.scalar(
        select(func.min(Transaction.date))
        .join(Account, Account.id == Transaction.account_id)
        .where(Account.category.in_(SPEND_ACCOUNT_CATEGORIES), Account.hidden.is_(False))
    )


def visits(rows: list) -> int:
    """How many transactions (not split parts) these lines belong to."""
    return len({r.txn_id for r in rows})


def month_totals(session: Session, cmap: CategoryMap, month: str) -> Totals:
    start, end = month_bounds(month)
    return totals(session, cmap, start, end)


# ------------------------------------------------------------------ months


def _index(month: str) -> int:
    year, mon = parse_month(month)
    return year * 12 + mon - 1


def _from_index(index: int) -> str:
    return month_str(index // 12, index % 12 + 1)


def latest_month(today: dt.date) -> str:
    return shift_month(month_of(today), PLAN_AHEAD_MONTHS)


def _calendar(month: str, today: dt.date) -> dict:
    year, mon = parse_month(month)
    dim = days_in_month(year, mon)
    current = month_of(today)
    if month == current:
        day = today.day
        return {"is_current": True, "is_future": False, "day_of_month": day, "days_in_month": dim,
                "days_left": dim - day, "pace": day / dim}
    if month < current:
        return {"is_current": False, "is_future": False, "day_of_month": dim, "days_in_month": dim,
                "days_left": 0, "pace": 1.0}
    return {"is_current": False, "is_future": True, "day_of_month": 0, "days_in_month": dim,
            "days_left": dim, "pace": 0.0}


# ------------------------------------------------------------------ budget accounts


def signed_balance(account: Account) -> int:
    """Assets count positive; balances owed on credit/loan accounts count negative."""
    cents = int(account.current_balance_cents or 0)
    return -cents if account.category in LIABILITY_CATEGORIES else cents


def _eligible_accounts(session: Session) -> list[Account]:
    return list(session.scalars(
        select(Account)
        .where(Account.hidden.is_(False), Account.category.in_(BUDGET_ACCOUNT_CATEGORIES))
        .order_by(Account.category, Account.name, Account.id)
    ))


@dataclass(frozen=True)
class Selection:
    """The saved budget-account choice.

    Stored as exceptions to the default (``{"exclude": [...], "include": [...]}``) so newly
    linked banks and cards join automatically. ``exact`` is a legacy plain list (development
    builds only): exactly those ids are included.
    """

    exclude: frozenset[int] = frozenset()
    include: frozenset[int] = frozenset()
    exact: frozenset[int] | None = None

    def includes(self, account: Account) -> bool:
        if self.exact is not None:
            return account.id in self.exact
        if account.id in self.include:
            return True
        return account.category in DEFAULT_BUDGET_ACCOUNT_CATEGORIES and account.id not in self.exclude

    def ids(self) -> set[int]:
        return set(self.exact) if self.exact is not None else set(self.exclude) | set(self.include)

    def keep_only(self, existing: set[int]) -> Selection:
        if self.exact is not None:
            return Selection(exact=self.exact & existing)
        return Selection(exclude=self.exclude & existing, include=self.include & existing)

    def to_json(self) -> str:
        if self.exact is not None:
            return json.dumps(sorted(self.exact))
        return json.dumps({"exclude": sorted(self.exclude), "include": sorted(self.include)})


def _ids(value: object) -> frozenset[int]:
    if not isinstance(value, list):
        return frozenset()
    return frozenset(v for v in value if isinstance(v, int) and not isinstance(v, bool))


def _saved_selection(session: Session) -> Selection | None:
    """The saved selection, or None when there is none (every bank and credit account)."""
    setting = session.get(AppSetting, BUDGET_ACCOUNTS_SETTING)
    if setting is None or setting.value is None:
        return None
    try:
        value = json.loads(setting.value)
    except ValueError:
        return None
    if isinstance(value, list):
        return Selection(exact=_ids(value))
    if isinstance(value, dict):
        return Selection(exclude=_ids(value.get("exclude")), include=_ids(value.get("include")))
    return None


def _store_selection(session: Session, selection: Selection) -> None:
    value = selection.to_json()
    setting = session.get(AppSetting, BUDGET_ACCOUNTS_SETTING)
    if setting is None:
        session.add(AppSetting(key=BUDGET_ACCOUNTS_SETTING, value=value))
    else:
        setting.value = value
    session.flush()


def budget_accounts(session: Session) -> list[tuple[Account, bool]]:
    """Every account that could fund the budget, with whether it's included."""
    saved = _saved_selection(session) or Selection()
    return [(a, saved.includes(a)) for a in _eligible_accounts(session)]


def set_budget_accounts(session: Session, account_ids: list[int]) -> None:
    """Save the included accounts as exceptions to the default.

    ``exclude`` gets the eligible bank/credit accounts not requested; ``include`` gets the
    requested accounts of other categories. Choices already saved for accounts that aren't
    eligible right now (hidden ones) are kept, so un-hiding an account restores its choice.
    """
    eligible = _eligible_accounts(session)
    eligible_ids = {a.id for a in eligible}
    for account_id in account_ids:
        if account_id not in eligible_ids:
            raise BudgetError(
                f"Account {account_id} can't fund the budget. Pick visible bank, credit card or other accounts."
            )
    requested = set(account_ids)
    saved = _saved_selection(session)
    kept_exclude = kept_include = frozenset()
    if saved is not None and saved.exact is None:
        kept_exclude = frozenset(i for i in saved.exclude if i not in eligible_ids)
        kept_include = frozenset(i for i in saved.include if i not in eligible_ids)
    default = DEFAULT_BUDGET_ACCOUNT_CATEGORIES
    _store_selection(session, Selection(
        exclude=kept_exclude | {a.id for a in eligible if a.category in default and a.id not in requested},
        include=kept_include | {a.id for a in eligible if a.category not in default and a.id in requested},
    ))


def prune_budget_accounts(session: Session) -> None:
    """Drop deleted accounts from the saved selection (call in the deleting transaction).

    SQLite can reuse the highest rowid, so a stale id left in the setting could silently
    make a brand-new account a budget account (or leave a brand-new bank out).
    """
    saved = _saved_selection(session)
    if saved is None:
        return
    ids = saved.ids()
    existing = set(session.scalars(select(Account.id).where(Account.id.in_(ids)))) if ids else set()
    if existing != ids:
        _store_selection(session, saved.keep_only(existing))


def _budget_account_out(account: Account, included: bool) -> dict:
    return {
        "id": account.id,
        "name": account.name,
        "mask": account.mask,
        "institution_name": account.institution_name,
        "category": account.category,
        "balance": from_cents(signed_balance(account)),
        "included": included,
    }


# ------------------------------------------------------------------ envelope data

class Row(NamedTuple):
    """One budgets row. ``restart``: re-added in the month it was removed (no carryover).
    ``moved`` (Release 3.17): the part of ``cents`` that came from one-time moves; the plan
    that repeats is ``cents - moved``."""

    cents: int
    removed: bool = False
    restart: bool = False
    moved: int = 0

    @property
    def plan(self) -> int:
        return self.cents - self.moved


# category -> month -> Row
Rows = dict[str, dict[str, Row]]

# A row's state for exact Undo: (limit_cents, removed, restart, moved_cents).
RowState = tuple[int, bool, bool, int]


def _budget_rows(session: Session, cmap: CategoryMap) -> Rows:
    """Every budgets row of a (current) spending category."""
    rows: Rows = defaultdict(dict)
    for month, category, cents, removed, restart, moved in session.execute(
        select(Budget.month, Budget.category, Budget.limit_cents, Budget.removed, Budget.restart, Budget.moved_cents)
    ):
        if category in cmap.by_id and cmap.kind(category) == "spending":
            rows[category][month] = Row(int(cents), bool(removed), bool(restart), int(moved or 0))
    return dict(rows)


# ------------------------------------------------------------------ plans repeat (Release 3.17)


def plans_repeat_from(session: Session) -> str | None:
    """The first month whose plans repeat; None = never (not set yet, or malformed)."""
    value = get_setting(session, BUDGET_PLANS_REPEAT_SETTING)
    try:
        parse_month(value or "")
    except ValueError:
        return None
    return value


def start_plans_repeat(session: Session, today: dt.date) -> str:
    """Set the first month whose plans repeat to today's month, once (an existing valid value
    is never moved). Returns the month in effect."""
    current = plans_repeat_from(session)
    if current is None:
        current = month_of(today)
        put_setting(session, BUDGET_PLANS_REPEAT_SETTING, current)
    return current


def safe_start_plans_repeat(session: Session, today: dt.date) -> None:
    """``start_plans_repeat`` and commit (after setup and every unlock); never raises."""
    try:
        if plans_repeat_from(session) is not None:
            return
        start_plans_repeat(session, today)
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        log.error("plans repeat start failed: %s", type(exc).__name__)


@dataclass(frozen=True)
class Repeat:
    """What a month without a row of its own plans (months >= ``start``; None = never).

    ``seeds``: (category, month) rows that hold money the user already had (setup's and goals'
    "Already saved"), not a plan: they repeat as 0. ``goals``: category -> (monthly, target)
    cents, planned by the goal's rule; ``debt``: the "Paying off debt" category and its extra."""

    start: str | None = None
    seeds: frozenset[tuple[str, str]] = frozenset()
    goals: dict[str, tuple[int, int]] = field(default_factory=dict)
    debt: tuple[str, int] | None = None
    # The goal and debt rules (today's settings) plan months from here on: the month after
    # ``budget_rules_frozen_through`` (earlier months that ended have rows of their own for
    # them, or repeat rows like any category: a goal added later never rewrites them).
    rules_from: str = ""

    def applies(self, month: str) -> bool:
        return self.start is not None and month >= self.start


NO_REPEAT = Repeat()


def _load_repeat(session: Session, cmap: CategoryMap) -> Repeat:
    start = plans_repeat_from(session)
    if start is None:
        return NO_REPEAT
    from . import debt_budget  # debt_budget builds on this module

    seeds = {(link.category, link.seed_month) for link in goal_links.load_links(session).values() if link.seed_month}
    raw = get_setting(session, BUDGET_SETUP_SEED_SETTING)
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        data = None
    if isinstance(data, dict) and isinstance(data.get("month"), str) and isinstance(data.get("category"), str):
        seeds.add((data["category"], data["month"]))
    goals = {
        category: (int(goal.monthly_cents or 0), int(goal.target_cents or 0))
        for category, goal in goal_links.goal_categories(session, cmap).items()
    }
    done = get_setting(session, BUDGET_RULES_FROZEN_SETTING)
    try:
        parse_month(done or "")
        rules_from = max(start, shift_month(done, 1))
    except ValueError:
        rules_from = start
    return Repeat(start, frozenset(seeds), goals, debt_budget.linked(session, cmap), rules_from)


def members_in(session: Session, cmap: CategoryMap, month: str) -> set[str]:
    """The categories in ``month``'s Budget (a row at or before it that isn't removed)."""
    return _members_at(_budget_rows(session, cmap), month)


def _members_at(rows: Rows, month: str) -> set[str]:
    """Categories whose latest row at or before ``month`` exists and isn't removed."""
    out = set()
    for category, by_month in rows.items():
        latest = max((m for m in by_month if m <= month), default=None)
        if latest is not None and not by_month[latest].removed:
            out.add(category)
    return out


def _envelope_net(
    session: Session, cmap: CategoryMap, account_ids: list[int], start: str, end: str
) -> dict[str, dict[str, int]]:
    """month -> {spending category: net cents} on the budget accounts, aggregated in SQL."""
    if not account_ids:
        return {}
    first, _ = month_bounds(start)
    _, last = month_bounds(end)
    lines = ledger.lines(
        Transaction.account_id.in_(account_ids),
        Transaction.is_transfer.is_(False),
        Transaction.date >= first,
        Transaction.date <= last,
    )
    month_col = func.strftime("%Y-%m", lines.c.date).label("m")
    out: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for month, category, net in session.execute(
        select(month_col, lines.c.category, func.sum(lines.c.amount_cents))
        .group_by(month_col, lines.c.category)
    ):
        info = cmap.get(category)
        if info.kind == "spending" and month is not None:
            out[month][info.id] += int(net or 0)
    return out


def _first_spending_month(session: Session, cmap: CategoryMap) -> str | None:
    """Earliest month with a spending transaction (Release 2 definition)."""
    first = None
    lines = ledger.lines(Transaction.is_transfer.is_(False))
    for category, day in session.execute(
        select(lines.c.category, func.min(lines.c.date))
        .join(Account, Account.id == lines.c.account_id)
        .where(Account.category.in_(SPEND_ACCOUNT_CATEGORIES), Account.hidden.is_(False))
        .group_by(lines.c.category)
    ):
        if day is not None and cmap.kind(category) == "spending" and (first is None or day < first):
            first = day
    return month_of(first) if first is not None else None


@dataclass(frozen=True)
class Line:
    carryover: int
    assigned: int
    spent: int
    # Release 3.17: the part of ``assigned`` from this month's one-time moves (0 without a row).
    moved: int = 0

    @property
    def available(self) -> int:
        return self.carryover + self.assigned - self.spent


def _repeat_base(
    repeat: Repeat, category: str, month: str, last: dict[str, int], carryover: int, spent: int,
) -> int:
    """What a member month without a row of its own plans (the repeat start already checked).
    The goal and debt rules use today's settings; a month that is over has a row of its own
    for them (``_freeze_rule_months``), so changing a setting never rewrites a past month."""
    if month < repeat.rules_from:
        return last.get(category, 0)
    goal = repeat.goals.get(category)
    if goal is not None:
        # The goal's rule (goal_links.plan_cents): min(monthly, target − saved), saved not
        # counting the month's own plan.
        monthly, target = goal
        return min(max(0, monthly), max(0, target - max(0, carryover - spent)))
    if repeat.debt is not None and repeat.debt[0] == category:
        return max(0, repeat.debt[1])
    return last.get(category, 0)


def _run(
    rows: Rows, net: dict[str, dict[str, int]], start: str, end: str, repeat: Repeat = NO_REPEAT,
) -> dict[str, dict[str, Line]]:
    """One pass over the months: each month's member categories and their envelope lines."""
    member: dict[str, bool] = {}
    previous: dict[str, int | None] = {}  # available last month, None if not a member then
    last: dict[str, int] = {}  # the plan a later month without a row repeats
    out: dict[str, dict[str, Line]] = {}
    for index in range(_index(start), _index(end) + 1):
        month = _from_index(index)
        spent_net = net.get(month, {})
        repeating = repeat.applies(month)
        lines: dict[str, Line] = {}
        for category, by_month in rows.items():
            row = by_month.get(month)
            if row is not None:
                member[category] = not row.removed
            if not member.get(category, False):
                previous[category] = None
                last.pop(category, None)  # a removal stops the repeat
                continue
            before = previous.get(category)
            # A category re-added in the month it was removed starts fresh: the leftover
            # that removing released can't come back.
            restart = row is not None and row.restart
            carryover = max(0, before) if before is not None and not restart else 0
            spent = max(0, -spent_net.get(category, 0))
            base = _repeat_base(repeat, category, month, last, carryover, spent) if repeating else 0
            line = Line(
                carryover=carryover,
                assigned=row.cents if row is not None else base,
                spent=spent,
                moved=row.moved if row is not None else 0,
            )
            if row is not None:
                # Money moved out (a negative plan) and "money you already had" aren't plans.
                last[category] = 0 if (category, month) in repeat.seeds else max(0, row.plan)
            lines[category] = line
            previous[category] = line.available
        out[month] = lines
    return out


def _future_set_aside(rows: Rows, lines: dict[str, dict[str, Line]], current: str, repeat: Repeat) -> int:
    """Release 3.17: what later months take from Ready to Assign now (rule A).

    Each later month's money pays the plans it repeats. Per category, from today's month on:
    a later month's own row sets aside only what its plan raises above the highest plan so far
    (today's plan to start with), so lowering a later month never takes money and repeated
    plans take none. The money in a later envelope that is here now (``pool``: today's
    leftover plus what was set aside) is the most a move out of a later month can free: a move
    out comes first out of that month's own unfunded plan, then out of the pool, then out of
    earlier months' unfunded plans. Moves between categories in one month take nothing new;
    a move in not covered by moves out comes from Ready to Assign. Before the repeat start
    month a later row sets aside its whole amount (the Release 2 rule).
    """
    now = lines.get(current, {})
    level: dict[str, int] = {}
    pool: dict[str, int] = {}
    for category, line in now.items():
        row = rows.get(category, {}).get(current)
        level[category] = max(0, row.plan if row is not None else line.assigned)
        pool[category] = max(0, line.available)
    total = 0
    for month in sorted(m for m in lines if m > current):
        month_lines = lines[month]
        funded_out = unfunded_out = moved_in = 0
        into: dict[str, int] = {}
        own_left: dict[str, int] = {}
        for category, by_month in rows.items():
            row = by_month.get(month)
            line = month_lines.get(category)
            if line is None or (row is not None and row.restart):
                # Not a member, or a fresh start: the leftover is released (not here now). The
                # level stays: a re-add up to the highest plan so far is that month's money.
                pool[category] = 0
                if line is None:
                    continue
            if not repeat.applies(month):
                if row is not None:
                    total += row.cents
                own_left[category] = 0
                continue
            if row is None:
                own_left[category] = max(0, line.assigned)  # a repeated plan: that month's money
                continue
            own = max(0, row.plan)
            aside = max(0, own - level.get(category, 0))
            level[category] = max(level.get(category, 0), own)
            pool[category] = pool.get(category, 0) + aside
            total += aside
            unfunded = own - aside
            out = max(0, -row.plan) + max(0, -row.moved)
            from_own = min(out, unfunded)
            from_pool = min(out - from_own, pool[category])
            pool[category] -= from_pool
            funded_out += from_pool
            unfunded_out += out - from_pool
            own_left[category] = unfunded - from_own
            if row.moved > 0:
                into[category] = row.moved
                moved_in += row.moved
        # Moves this month: money in comes from the money moved out (funded first), then from
        # Ready to Assign; money moved out and not moved in frees only funded money.
        from_ready = max(0, moved_in - funded_out - unfunded_out)
        freed = max(0, funded_out - moved_in)
        total += from_ready - freed
        funded_in = min(funded_out, moved_in) + from_ready
        if moved_in:
            for category, amount in into.items():
                pool[category] = pool.get(category, 0) + funded_in * amount // moved_in
        # Spending in a later month (a dated transaction) beyond its own unfunded plan uses the pool.
        for category, left in own_left.items():
            line = month_lines.get(category)
            if line is not None:
                pool[category] = max(0, pool.get(category, 0) - max(0, line.spent - left))
    return total


@dataclass
class Book:
    """Everything the envelope math needs, loaded once."""

    today: dt.date
    current: str
    earliest: str
    latest: str
    accounts: list[tuple[Account, bool]]
    cash_total: int
    rows: Rows
    net: dict[str, dict[str, int]]
    start: str
    end: str
    lines: dict[str, dict[str, Line]]
    # Release 3.6: today's month's income; ``income.pending`` (0 with mode "off") is added to
    # Ready to Assign, so with the switch off every number is exactly what it was before.
    income: Income = NO_INCOME
    # Release 3.17: how months without a row of their own plan.
    repeat: Repeat = NO_REPEAT

    def members(self, month: str) -> dict[str, Line]:
        return self.lines.get(month, {})

    @property
    def pending(self) -> int:
        return self.income.pending

    def run(self, rows: Rows, end: str | None = None) -> dict[str, dict[str, Line]]:
        """The envelope lines for other rows (a save being checked), same months and rules."""
        return _run(rows, self.net, self.start, self.end if end is None else end, self.repeat)

    def ready_to_assign(
        self, rows: Rows | None = None, lines: dict[str, dict[str, Line]] | None = None, pending: int | None = None
    ) -> int:
        rows = self.rows if rows is None else rows
        if lines is None:
            lines = self.lines if rows is self.rows else self.run(rows)
        future = _future_set_aside(rows, lines, self.current, self.repeat)
        pending = self.pending if pending is None else pending
        return self.cash_total - sum(line.available for line in lines.get(self.current, {}).values()) - future + pending

    def planned(self, month: str, lines: dict[str, dict[str, Line]] | None = None) -> int:
        """Σ assigned in ``month`` over its members ("Planned")."""
        return sum(line.assigned for line in (self.lines if lines is None else lines).get(month, {}).values())


def load_book(
    session: Session, cmap: CategoryMap, today: dt.date, month: str | None = None, *, persist_freeze: bool = False,
) -> Book:
    """The envelope book. It never writes, except with ``persist_freeze`` (only
    ``freeze_rule_months``): months that ended get their goal and debt plans as rows of their own
    in memory on every load, and in the database only in that short transaction of its own."""
    current = month_of(today)
    month = month or current
    rows = _budget_rows(session, cmap)
    first_row = min((m for by_month in rows.values() for m in by_month), default=None)
    first_spend = _first_spending_month(session, cmap)
    # Never later than today's month, so the current month is always viewable.
    earliest = min(m for m in (current, first_row, first_spend) if m is not None)
    last_row = max((m for by_month in rows.values() for m in by_month), default=None)
    start = min(m for m in (current, month, first_row) if m is not None)
    # Through the last row too, so every saved month has its envelope lines (for the checks
    # a save makes on later months).
    end = max(m for m in (current, month, last_row) if m is not None)
    accounts = budget_accounts(session)
    included = [a for a, inc in accounts if inc]
    net = _envelope_net(session, cmap, [a.id for a in included], start, end)
    income = load_income(session, cmap, [a.id for a in included], today)
    repeat = _load_repeat(session, cmap)
    lines = _run(rows, net, start, end, repeat)
    _freeze_rule_months(session, rows, lines, repeat, current, persist=persist_freeze)
    return Book(
        repeat=repeat,
        income=income,
        today=today,
        current=current,
        earliest=earliest,
        latest=latest_month(today),
        accounts=accounts,
        cash_total=sum(signed_balance(a) for a in included),
        rows=rows,
        net=net,
        start=start,
        end=end,
        lines=lines,
    )


def _freeze_rule_months(
    session: Session, rows: Rows, lines: dict[str, dict[str, Line]], repeat: Repeat, current: str,
    *, persist: bool = False,
) -> None:
    """Release 3.17: once a month is over, the goal and debt plans it showed become rows of its
    own, so later changes to a goal or the extra (and a goal reaching its target) never rewrite
    it. Months from the repeat start (or after ``budget_rules_frozen_through``) up to today's
    month are frozen with what the rules planned for them (computed with the settings as they
    are: they can only change while FinTrack runs, and they are frozen before any change).

    Every ``load_book`` freezes in memory only (``rows``; reads never write or lock the
    database). ``persist`` (``freeze_rule_months``) also writes the rows and the setting; a month
    that already has a row is treated as frozen. The rows are exactly the plans the month showed,
    so its envelopes don't change; a later month repeats them only if the category stops being
    a goal or debt category (its last month's plan)."""
    if repeat.start is None:
        return
    first = repeat.rules_from
    if first >= current:
        return
    ruled = set(repeat.goals) | ({repeat.debt[0]} if repeat.debt else set())
    for index in range(_index(first), _index(current)):
        month = _from_index(index)
        for category, line in lines.get(month, {}).items():
            if category not in ruled or month in rows.get(category, {}):
                continue
            rows.setdefault(category, {})[month] = Row(line.assigned)
            if persist and session.scalar(
                select(Budget.id).where(Budget.month == month, Budget.category == category)
            ) is None:
                session.add(Budget(month=month, category=category, limit_cents=line.assigned))
    if persist:
        put_setting(session, BUDGET_RULES_FROZEN_SETTING, shift_month(current, -1))


def freeze_rule_months(session: Session, today: dt.date) -> bool:
    """Write the frozen goal and debt plans of months that ended (``_freeze_rule_months``); the
    caller commits right away. Returns False when there was nothing to do."""
    if plans_repeat_from(session) is None:
        return False
    load_book(session, load_categories(session), today, persist_freeze=True)
    return True


def safe_freeze_rule_months(session: Session, today: dt.date) -> bool:
    """``freeze_rule_months`` in a short transaction of its own (unlock, the automation loop,
    before rule changes); never raises. False when it failed (it runs again next time)."""
    try:
        if freeze_rule_months(session, today):
            session.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        log.error("plans freeze failed: %s", type(exc).__name__)
        return False


def plans_by_month(session: Session, cmap: CategoryMap, today: dt.date, first: str, last: str) -> dict[str, dict[str, int]]:
    """month -> {member spending category: assigned cents} for ``first``..``last``: each month's
    own row, or from the repeat start on the plan it repeats (Release 3.17). Before the start
    this is exactly the month's rows (0 for a member without one)."""
    book = load_book(session, cmap, today, first)
    lines = book.lines if last <= book.end else book.run(book.rows, last)
    out: dict[str, dict[str, int]] = {}
    for index in range(_index(first), _index(last) + 1):
        month = _from_index(index)
        out[month] = {c: line.assigned for c, line in lines.get(month, {}).items()}
    return out


# ------------------------------------------------------------------ budget month view


def _line(cmap: CategoryMap, category: str, **values: float) -> dict:
    info = cmap.get(category)
    return {"category": category, "name": info.name, "hue": info.hue, **values}


def _fixed_by_category(session: Session, cmap: CategoryMap, month: str) -> dict[str, int]:
    """Fixed-kind spending this month (Release 2 spending accounts), aggregated in SQL."""
    start, end = month_bounds(month)
    net: dict[str, int] = defaultdict(int)
    lines = ledger.lines(Transaction.is_transfer.is_(False), Transaction.date >= start, Transaction.date <= end)
    for category, total in session.execute(
        select(lines.c.category, func.sum(lines.c.amount_cents))
        .join(Account, Account.id == lines.c.account_id)
        .where(Account.category.in_(SPEND_ACCOUNT_CATEGORIES), Account.hidden.is_(False))
        .group_by(lines.c.category)
    ):
        info = cmap.get(category)
        if info.kind == "fixed":
            net[info.id] += int(total or 0)
    return {c: -v for c, v in net.items() if v < 0}


def _non_spending_groups(cmap: CategoryMap) -> set[int]:
    """Groups whose categories are all income, fixed or transfer ones (e.g. the setup's
    "Income"): the budget doesn't show them. An empty group still shows (to add to)."""
    kinds: dict[int, set[str]] = defaultdict(set)
    for info in cmap.by_id.values():
        if info.group_id is not None:
            kinds[info.group_id].add(info.kind)
    return {g for g, k in kinds.items() if "spending" not in k}


def _groups(session: Session) -> list[CategoryGroup]:
    return list(session.scalars(select(CategoryGroup).order_by(CategoryGroup.position, CategoryGroup.id)))


def needed_cents(target: Target | None, month: str, line: Line, bills: int = 0) -> int:
    """What to assign in ``month`` to stay on track with the target (SPEC R3 "Budget targets").

    monthly: ``max(0, amount - assigned)``.
    bills (Release 3.6 phase 2): ``max(0, bills - assigned)`` with ``bills`` the month's bill
    total for the category (``bill_total``); carryover is ignored, like a monthly target.
    by_date: ``per_month = ceil_to_cent(max(0, amount - carryover) / months_left)`` with
    ``months_left`` counted from ``month`` to the target month inclusive (at least 1), then
    ``max(0, per_month - assigned)``; 0 once ``month`` is past the target month.
    """
    if target is None:
        return 0
    if target.kind == "bills":
        return max(0, bills - line.assigned)
    assert target.cents is not None
    if target.kind == "monthly":
        return max(0, target.cents - line.assigned)
    assert target.date is not None
    target_month = month_of(target.date)
    if month > target_month:
        return 0
    months_left = max(1, _index(target_month) - _index(month) + 1)
    remaining = max(0, target.cents - line.carryover)
    per_month = -(-remaining // months_left)  # ceiling division in integer cents
    return max(0, per_month - line.assigned)


# ------------------------------------------------------------------ bills per category (phase 2)
# One bill, two places: a recurring bill with a budget category shows on the Recurring
# calendar and in its category here, from the same occurrences (services/calendar.py).


def bill_account_ids(session: Session, book: Book) -> set[int]:
    """The accounts whose bills count on the budget: the budget's own accounts, plus every
    visible credit card even when it isn't one of them (a bill charged to a card is still
    spending in its category; paying the card later is a transfer). A bill paid from any
    other account (savings, a brokerage, an account left out of the budget) doesn't count;
    a bill without an account does."""
    ids = set(_included_ids(book))
    ids.update(session.scalars(
        select(Account.id).where(Account.category == "credit", Account.hidden.is_(False))
    ))
    return ids


def month_bills(session: Session, month: str, today: dt.date, book: Book) -> dict[str, list] | None:
    """category id -> this month's bill occurrences (effective date in the month, after moves;
    only bills paid from ``bill_account_ids``), or None when the month ended more than the
    calendar's ``PAST_DAYS`` ago (its matching isn't kept that far back)."""
    start, end = month_bounds(month)
    if end < today - dt.timedelta(days=occ_calendar.PAST_DAYS):
        return None
    return occ_calendar.bill_occurrences(session, today, start, end, bill_account_ids(session, book))


def bill_cents(occ) -> int:  # noqa: ANN001 - calendar.Occ
    """What one occurrence costs: 0 when skipped, the actual amount once paid, else the
    expected amount (positive cents). ``include_in_forecast`` doesn't matter here."""
    if occ.status == "skipped":
        return 0
    if occ.status == "paid" and occ.actual_cents is not None:
        return abs(int(occ.actual_cents))
    return abs(int(occ.amount_cents))


def bill_total(occs: list) -> int:
    return sum(bill_cents(o) for o in occs)


def bill_totals(bills: dict[str, list] | None) -> dict[str, int]:
    return {c: bill_total(o) for c, o in (bills or {}).items()}


def _bill_out(occ) -> dict:  # noqa: ANN001
    paid = occ.status == "paid"
    return {
        "key": occ.key,
        "recurring_id": occ.recurring_id,
        "name": occ.name,
        "date": occ.date.isoformat(),
        "amount": from_cents(abs(int(occ.amount_cents))),
        "status": occ.status,
        "actual_amount": from_cents(abs(int(occ.actual_cents))) if paid and occ.actual_cents is not None else None,
        # When it was paid: the matched transaction's date, or the due date when marked paid by hand.
        "paid_date": (occ.actual_date or occ.date).isoformat() if paid else None,
    }


def bills_out(occs: list | None) -> dict | None:
    """``CategoryBills`` (positive amounts), or None without bills."""
    if not occs:
        return None
    return {"total": from_cents(bill_total(occs)), "items": [_bill_out(o) for o in occs]}


def check_view_month(book: Book, month: str) -> None:
    if month < book.earliest or month > book.latest:
        raise BudgetError(f"month must be between {book.earliest} and {book.latest}")


# ------------------------------------------------------------------ expected income (Release 3.6)


def _included_ids(book: Book) -> list[int]:
    return [a.id for a, inc in book.accounts if inc]


def next_income(book: Book, today: dt.date) -> dict | None:
    """The next expected paycheck: the first upcoming income occurrence from today on
    (calendar moves and skips applied; one already paid early doesn't count)."""
    for occ in book.income.occurrences:
        if occ.status == "upcoming" and occ.date >= today:
            return {"date": occ.date.isoformat(), "name": occ.name, "amount": from_cents(occ.amount_cents)}
    return None


def _more_income_expected(session: Session, book: Book, today: dt.date) -> bool:
    """Is a paycheck still expected in today's month? One due from today to the month's end,
    or one a few days late ("pending") that may still arrive."""
    if not income_items(session, _included_ids(book)):
        # Nothing tells us when money comes: pay is assumed in by the 20th, so a month is
        # called short from the 21st on.
        return today.day <= SHORT_FALLBACK_AFTER_DAY
    start, end = month_bounds(book.current)
    return any(
        start <= occ.date <= end and occ.status in ("upcoming", "pending")
        for occ in book.income.occurrences
    )


def _missed_paycheck(book: Book, today: dt.date):  # noqa: ANN202 - Occ | None
    """This month's latest paycheck that was due before today and didn't come."""
    start, _ = month_bounds(book.current)
    missed = [o for o in book.income.occurrences if start <= o.date < today and o.status in ("late", "past")]
    return missed[-1] if missed else None


def _latest_deposit(session: Session, cmap: CategoryMap, book: Book) -> tuple[dt.date, str] | None:
    """(date, display name) of this month's newest deposit counted as received."""
    ids = _included_ids(book)
    if not ids:
        return None
    start, end = month_bounds(book.current)
    lines = ledger.lines(
        Transaction.account_id.in_(ids), Transaction.is_transfer.is_(False),
        Transaction.date >= start, Transaction.date <= end,
    )
    rows = session.execute(
        select(lines.c.date, lines.c.category, lines.c.name, lines.c.merchant_name)
        .where(lines.c.amount_cents > 0)
        .order_by(lines.c.date.desc(), lines.c.txn_id.desc())
    ).all()
    for row in rows:
        if cmap.kind(row.category) in RECEIVED_KINDS:
            return row.date, display_name(row.merchant_name, row.name)
    return None


def income_event(session: Session, cmap: CategoryMap, book: Book, today: dt.date) -> dict | None:
    """"Earned more" / "Paycheck was short" (derived; a PUT of income_expected resolves it)."""
    inc = book.income
    expected = inc.expected
    if inc.mode != "expected" or expected is None:
        return None
    surplus = inc.received - expected
    if surplus >= INCOME_EVENT_MIN_CENTS:
        latest = _latest_deposit(session, cmap, book)
        return {
            "kind": "more", "amount": from_cents(surplus),
            "date": latest[0].isoformat() if latest else None, "name": latest[1] if latest else None,
            "expected": from_cents(expected), "received": from_cents(inc.received),
        }
    short = -surplus
    if short >= INCOME_EVENT_MIN_CENTS and not _more_income_expected(session, book, today):
        # The paycheck that didn't come (calendar occurrences), when there is one.
        missed = _missed_paycheck(book, today)
        return {
            "kind": "less", "amount": from_cents(short),
            "date": missed.date.isoformat() if missed else None, "name": missed.name if missed else None,
            "expected": from_cents(expected), "received": from_cents(inc.received),
            "reason": "short" if inc.received > 0 else "missing",
            "can_use_unplanned": book.ready_to_assign() >= short,
        }
    return None


def income_out(session: Session, cmap: CategoryMap, book: Book, month: str, today: dt.date) -> dict:
    """``BudgetIncome`` for the viewed month; ``pending``, ``next`` and ``event`` only for today's month."""
    inc = book.income
    current = month == book.current
    if current:
        received, override = inc.received, inc.override
    else:
        received = inc.by_month.get(month)
        if received is None:
            received = received_by_month(session, cmap, _included_ids(book), month, month).get(month, 0)
        override = load_income_settings(session).months.get(month)
    expected = override if override is not None else inc.suggested
    # Settings (D7): "Tell me when a paycheck is different" off -> no earned-more/short note.
    notify = current and prefs.paycheck_notify(session)
    return {
        "mode": inc.mode,
        "expected": from_cents(expected),
        "expected_override": from_cents(override),
        "suggested": from_cents(inc.suggested),
        "suggested_source": inc.suggested_source,
        "received": from_cents(received),
        "pending": from_cents(inc.pending if current else 0),
        "next": next_income(book, today) if current else None,
        "event": income_event(session, cmap, book, today) if notify else None,
    }


def budget_month(session: Session, month: str, today: dt.date) -> dict:
    """``BudgetMonth`` for a month in [earliest_month, today's month + 12].

    Saves are limited to [today's month, today's month + 12], which is always inside it.
    """
    cmap = load_categories(session)
    book = load_book(session, cmap, today, month)
    check_view_month(book, month)
    lines = book.members(month)
    members = sorted(lines, key=cmap.sort_key)

    def by_spent(items: dict[str, int]) -> list[tuple[str, int]]:
        return sorted(items.items(), key=lambda cs: (-cs[1], cmap.sort_key(cs[0])))

    unbudgeted = {
        c: -n for c, n in book.net.get(month, {}).items() if c not in lines and n < 0
    }

    bills = month_bills(session, month, today, book)
    totals = bill_totals(bills)
    needs = {c: needed_cents(cmap.get(c).target, month, lines[c], totals.get(c, 0)) for c in members}
    hidden_groups = _non_spending_groups(cmap)

    def target(category: str) -> dict | None:
        out = target_out(cmap.get(category).target, totals.get(category, 0) if bills is not None else None)
        return None if out is None else {**out, "needed": from_cents(needs[category])}

    # Bill categories that aren't in the plan ("Your bills here add up to $X").
    outside = sorted(
        (
            c for c in totals
            if c not in lines and c in cmap.by_id and cmap.kind(c) == "spending" and not cmap.get(c).hidden
            and totals[c] > 0
        ),
        key=lambda c: (-totals[c], cmap.sort_key(c)),
    )

    ready = book.ready_to_assign()
    seed = setup_seed(session, month)
    out = {
        "month": month,
        **_calendar(month, today),
        "ready_to_assign": from_cents(ready),
        # Ready to assign is the same in every month, so the part of it that is income still
        # expected this month is too (the Detailed view's "Includes $X still expected" note).
        "ready_pending": from_cents(book.pending),
        # Release 3.6 (Simple view): "Money for {Month}" = Ready to assign + Planned.
        "month_money": from_cents(ready + book.planned(month)),
        "income": income_out(session, cmap, book, month, today),
        "savings_category": savings_category(session, cmap),
        "setup_needed": not has_budget_rows(session) and not setup_done(session),
        # Phase 2: bills per category come from the calendar's occurrences; a month that
        # ended more than 62 days ago has none (bills_available false, every line's bills null).
        "bills_available": bills is not None,
        "bills_outside": [
            {**_line(cmap, c), "total": from_cents(totals[c])} for c in outside
        ],
        "cash_total": from_cents(book.cash_total),
        "budget_accounts": [_budget_account_out(a, inc) for a, inc in book.accounts],
        "assigned": from_cents(sum(lines[c].assigned for c in members)),
        "spent": from_cents(sum(lines[c].spent for c in members)),
        "available": from_cents(sum(lines[c].available for c in members)),
        "categories": [
            _line(
                cmap, c,
                carryover=from_cents(lines[c].carryover),
                assigned=from_cents(lines[c].assigned),
                # Release 3.17: the part of ``assigned`` from this month's one-time moves.
                moved=from_cents(lines[c].moved),
                spent=from_cents(lines[c].spent),
                available=from_cents(lines[c].available),
                group_id=cmap.get(c).group_id,
                custom=cmap.get(c).custom,
                target=target(c),
                bills=bills_out((bills or {}).get(c)),
            )
            for c in members
        ],
        "groups": [
            {"id": g.id, "name": g.name, "position": int(g.position or 0)}
            for g in _groups(session)
            if g.id not in hidden_groups
        ],
        "needed_total": from_cents(sum(needs.values())),
        "unbudgeted": [_line(cmap, c, spent=from_cents(s)) for c, s in by_spent(unbudgeted)],
        "fixed": [_line(cmap, c, spent=from_cents(s)) for c, s in by_spent(_fixed_by_category(session, cmap, month))],
        "addable": [
            {"id": c.id, "name": c.name, "hue": c.hue}
            for c in cmap.ordered("spending")
            if not c.hidden and c.id not in lines
        ],
        # Setup's "money you already had" row (T−1 only): not a plan to copy.
        "seed_category": seed if seed in lines else None,
        "earliest_month": book.earliest,
        "latest_month": book.latest,
    }
    _apply_goals(session, cmap, book, month, out)
    _apply_debt(session, cmap, out)
    return out


def _apply_debt(session: Session, cmap: CategoryMap, out: dict) -> None:
    """Release 3.10: ``EnvelopeLine.debt = {extra}`` on the "Paying off debt" line (Reports ›
    Paying off debt), null elsewhere. Release 3.17: a month without its own row plans the
    extra (``Repeat.debt``)."""
    from . import debt_budget  # debt_budget builds on this module

    found = debt_budget.linked(session, cmap)
    category, extra = found if found is not None else (None, 0)
    for line in out["categories"]:
        line["debt"] = {"extra": from_cents(extra)} if line["category"] == category else None


def _apply_goals(session: Session, cmap: CategoryMap, book: Book, month: str, out: dict) -> None:
    """Goals (D8, Release 3.8): ``EnvelopeLine.goal`` / ``.seeded`` and ``savings_category``
    (the emergency goal's category first). Release 3.17: a goal category's month without its
    own row plans min(monthly, target − saved), 0 once reached (``Repeat.goals``)."""
    by_category = goal_links.goal_categories(session, cmap)
    lines = book.members(month)
    seeded = goal_links.seeded_categories(session, month)
    if out["seed_category"]:
        seeded.add(out["seed_category"])
    for line in out["categories"]:
        goal = by_category.get(line["category"])
        line["goal"] = goal_links.goal_ref(goal, lines.get(line["category"])) if goal is not None else None
        line["seeded"] = line["category"] in seeded
    out["savings_category"] = goal_links.resolve_savings(
        by_category, book.members(book.current), out["savings_category"]
    )


# ------------------------------------------------------------------ saving


def _amount_cents(value: float) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise BudgetError("Amounts must be finite numbers.")
    # Range-check before converting: to_cents' Decimal quantize fails past 28 digits.
    if abs(value) * 100 > MAX_AMOUNT_CENTS + 1 or abs(cents := to_cents(value)) > MAX_AMOUNT_CENTS:
        raise BudgetError(f"Amounts must be within {fmt_usd(MAX_AMOUNT_CENTS)}.")
    return cents


def _month_label(month: str) -> str:
    year, mon = parse_month(month)
    return f"{calendar.month_name[mon]} {year}"


def _carryover(lines: dict[str, dict[str, Line]], category: str, month: str) -> int:
    """The carryover of a member envelope (0 for a restart row); 0 if not a member."""
    line = lines.get(month, {}).get(category)
    return line.carryover if line is not None else 0


def _check_later_move_outs(
    cmap: CategoryMap, book: Book, after: Rows, after_lines: dict[str, dict[str, Line]],
    month: str, cents: dict[str, int],
) -> None:
    """``carryover + assigned >= 0`` must still hold in later months after this save.

    Lowering an earlier month shrinks the leftover a later month moved out; without this the
    same dollars would be freed twice and Ready to Assign would include money that isn't there.
    Only violations this save creates or worsens are refused (a late transaction can also
    shrink a leftover; that shows as overspending instead).
    """
    for category in cents:
        for later in sorted(m for m in after.get(category, {}) if m > month):
            row = after[category][later]
            value = row.cents
            if row.removed or value >= 0 or category not in after_lines.get(later, {}):
                continue
            carry = _carryover(after_lines, category, later)
            if carry + value < 0 and carry < _carryover(book.lines, category, later):
                label = _month_label(later)
                raise BudgetError(
                    f"You moved {fmt_usd(-value)} out of {cmap.get(category).name} in {label}, "
                    f"so {_month_label(month)} can't leave less than that behind. Change {label} first."
                )


@dataclass(frozen=True)
class IncomeChange:
    """A ``BudgetSave``'s ``income_expected`` (cents; None = back to the suggestion),
    ``accept_received`` ("Leave it for next month": skip the over-plan check once) and
    ``restore`` (Undo putting back an earlier amount: it may be below what came in)."""

    expected: int | None
    accept_received: bool = False
    restore: bool = False


UNSET = object()
MONTH_AMOUNT_ONLY_NOW = "The month's amount can only be changed for this month."


def save_budget(
    session: Session, month: str, assigned: dict[str, float], removed: list[str], today: dt.date,
    *, income_expected: float | None | object = UNSET, accept_received: bool = False, restore: bool = False,
    moved: dict[str, float] | None = None,
) -> None:
    """Apply a ``BudgetSave`` to ``month`` (partial and atomic).

    Everything is validated against an in-memory copy before anything is written, so a
    rejected save changes nothing. Raises BudgetError (422) on any rule violation.

    ``moved`` (Release 3.17): the month's one-time moves per category (Move money, Cover it),
    already included in ``assigned``; each one must be in ``assigned`` too. A category saved
    without one has no one-time moves that month: the amount is its plan.
    """
    check_editable_month(month, today)
    cmap = load_categories(session)
    cents: dict[str, int] = {}
    for category, value in assigned.items():
        _check_category(cmap, category)
        cents[category] = _amount_cents(value)
    moved_cents: dict[str, int] = {}
    for category, value in (moved or {}).items():
        if category not in cents:
            raise BudgetError("A one-time move needs the category's new amount too.")
        moved_cents[category] = _amount_cents(value)
    change = None
    if restore and income_expected is UNSET and not cents:
        raise BudgetError("Undo needs the amounts to put back.")
    if restore and accept_received:
        raise BudgetError("Undo can't be combined with Leave it for next month.")
    if income_expected is not UNSET or accept_received:
        if month != month_of(today):
            raise BudgetError(MONTH_AMOUNT_ONLY_NOW)
        if income_expected is UNSET:
            raise BudgetError("Leave it for next month needs the amount that came in.")
        value = None if income_expected is None else _amount_cents(income_expected)
        if value is not None and value < 0:
            raise BudgetError("The month's amount can't be below $0.")
        change = IncomeChange(value, bool(accept_received), bool(restore))
    save_budget_cents(
        session, cmap, month, cents, removed, today, income=change, restore=bool(restore), moved=moved_cents,
    )


def check_editable_month(month: str, today: dt.date) -> None:
    latest = latest_month(today)
    if month < month_of(today):
        # Overspending from an earlier month is covered in the current month instead.
        raise BudgetError(PAST_MONTH_READ_ONLY)
    if month > latest:
        raise BudgetError(f"You can budget up to {latest}, 12 months ahead.")


def _check_category(cmap: CategoryMap, category: str) -> None:
    if category not in cmap.by_id:
        raise BudgetError(f"Unknown category {category!r}.")
    if cmap.kind(category) != "spending":
        raise BudgetError(f"{cmap.get(category).name} isn't a spending category.")


def save_budget_cents(
    session: Session, cmap: CategoryMap, month: str, cents: dict[str, int], removed: list[str], today: dt.date,
    *, income: IncomeChange | None = None, restore: bool = False, release_overspent: bool = False,
    moved: dict[str, int] | None = None,
) -> None:
    """``save_budget`` with amounts already in integer cents (month already checked).

    ``moved`` (Release 3.17): the one-time part of each saved amount (Move money, Cover it;
    a goal's "Already saved" taken from the emergency fund). A saved category without one has
    none: the whole amount is the month's plan, which later months repeat.

    ``release_overspent`` (Goals: Delete / "I spent it"): removing an overspent category lowers
    Ready to Assign by its overspending (the money was spent; it comes out of the month's money
    either way). That lowering alone never refuses the save.

    ``restore`` (Undo): assigning a category removed this month takes the removal back (the
    envelope continues with its carryover, so a negative plan fits again) instead of
    re-adding it with a fresh start. The money the removal released goes back into the
    envelope, so nothing is freed twice; every other rule still applies. Later months' rows
    the removal deleted don't come back."""
    check_category = lambda category: _check_category(cmap, category)  # noqa: E731
    for category in cents:
        check_category(category)
    removing = list(dict.fromkeys(removed))
    for category in removing:
        check_category(category)
        if category in cents:
            raise BudgetError(f"{cmap.get(category).name} can't be both assigned and removed.")
    if removing and income is not None and income.accept_received:
        raise BudgetError("Leave it for next month can't remove a category.")
    moved = moved or {}
    for category in moved:
        if category not in cents:
            raise BudgetError("A one-time move needs the category's new amount too.")
    if not cents and not removing and income is None:
        return

    book = load_book(session, cmap, today, month)
    members = book.members(month)
    after: Rows = {c: dict(by_month) for c, by_month in book.rows.items()}
    for category, value in cents.items():
        saved = after.get(category, {}).get(month)
        # Assigning a category removed this month re-adds it here with a fresh start (no
        # carryover), so the leftover that removing released can't come back. Undo takes
        # the removal back instead: the row's own restart flag (kept on the removed row).
        if restore and saved is not None and saved.removed:
            restart = saved.restart
        else:
            restart = saved is not None and (saved.removed or saved.restart)
        after.setdefault(category, {})[month] = Row(value, False, restart, moved.get(category, 0))
    for category in removing:
        by_month = after.setdefault(category, {})
        for m in [m for m in by_month if m > month]:
            del by_month[m]
        if category in members:
            # Keep the row's restart flag, so an Undo of this removal puts back exactly the
            # envelope it was (the flag means nothing on a removed row otherwise).
            saved = by_month.get(month)
            by_month[month] = Row(0, True, saved is not None and saved.restart)

    after_lines = book.run(after)
    for category, value in cents.items():
        carry = _carryover(after_lines, category, month)
        if carry + value < 0:
            raise BudgetError(f"You can move at most {fmt_usd(carry)} out of {cmap.get(category).name}.")
    _check_later_move_outs(cmap, book, after, after_lines, month, cents)

    pending_after = book.pending
    if income is not None:
        pending_after = _check_income_change(cmap, book, after, after_lines, month, cents, income)

    # Refuse a save that lowers Ready to Assign and leaves it below zero. Saves that raise it
    # (or leave it unchanged) are always allowed, so an over-assigned budget can be fixed.
    # Release 3.6: "Leave it for next month" (accept_received) may leave it below zero only by
    # the money that didn't come in, so its plan changes are checked against the income still
    # expected before the save (they can only lower plans; removing is refused above).
    ready_before = book.ready_to_assign()
    accepting = income is not None and income.accept_received
    ready_after = book.ready_to_assign(after, after_lines, book.pending if accepting else pending_after)
    overspent = 0
    if release_overspent:
        overspent = sum(max(0, -members[c].available) for c in removing if c in members)
    # Release 3.17: a save that only lowers amounts never needs money, so it is never refused
    # here (lowering one later month can change what another later month sets aside).
    lowering_only = income is None and not removing and all(
        c in members and value <= members[c].assigned for c, value in cents.items()
    )
    if ready_after + overspent < min(ready_before, 0) and not lowering_only:
        if income is not None and not accepting and pending_after < book.pending:
            planned = fmt_usd(book.planned(month, after_lines))
            raise BudgetError(f"Your plans add up to {planned}. Lower some plans first, or pick at least {planned}.")
        raise BudgetError(f"Only {fmt_usd(max(0, ready_before))} is ready to assign. Lower another category first.")

    # Validated: write the same changes to the database.
    existing = {
        b.category: b
        for b in session.scalars(
            select(Budget).where(Budget.month == month, Budget.category.in_(list(cents) + removing))
        )
    }

    def put(category: str) -> None:
        planned = after[category][month]
        row = existing.get(category)
        if row is None:
            session.add(Budget(
                month=month, category=category, limit_cents=planned.cents,
                removed=planned.removed, restart=planned.restart, moved_cents=planned.moved,
            ))
        else:
            row.limit_cents, row.removed, row.restart = planned.cents, planned.removed, planned.restart
            row.moved_cents = planned.moved

    for category in cents:
        put(category)
    if removing:
        session.execute(delete(Budget).where(Budget.category.in_(removing), Budget.month > month))
        for category in removing:
            if category in members:
                put(category)
    if income is not None:
        settings = load_income_settings(session)
        if income.expected is None:
            settings.months.pop(month, None)
        else:
            settings.months[month] = income.expected
        store_income_settings(session, settings, today)
    session.flush()


def row_state(session: Session, month: str, category: str) -> RowState | None:
    """One budgets row as (limit_cents, removed, restart, moved_cents); None when there is none."""
    row = session.scalar(select(Budget).where(Budget.month == month, Budget.category == category))
    if row is None:
        return None
    return (int(row.limit_cents), bool(row.removed), bool(row.restart), int(row.moved_cents or 0))


class RowChanged(BudgetError):
    """The row isn't what the change being undone left (409; the message is safe to show)."""


ROW_CHANGED = "This plan was changed since, so it wasn't undone."


def as_row_state(state: tuple | list | None) -> RowState | None:
    """A row state from an Undo record: (limit, removed, restart[, moved]); records saved before
    Release 3.17 have no ``moved`` (0)."""
    if state is None:
        return None
    cents, removed, restart, *rest = state
    return (int(cents), bool(removed), bool(restart), int(rest[0]) if rest else 0)


def restore_rows(
    session: Session, month: str,
    rows: dict[str, tuple[tuple | None, tuple | None]], today: dt.date,
) -> None:
    """Release 3.14 (Reports' Undo of "Plan $X for next month" and "Move $X to savings"): put
    budgets rows of one editable month back exactly, all or nothing. ``rows``: category ->
    (state, expected); ``state`` None = no row; ``expected`` = the row right after the change
    being undone. Any row that isn't ``expected`` any more (an edit since, in another window
    too) raises ``RowChanged`` and nothing is written. Then ``put_back_many``' rules (one Ready
    to Assign check). Only these rows are touched; past months are refused like every save."""
    check_editable_month(month, today)
    for category, (_, expected) in rows.items():
        if row_state(session, month, category) != as_row_state(expected):
            raise RowChanged(ROW_CHANGED)
    put_back_many(session, load_categories(session), today, {c: {month: state} for c, (state, _) in rows.items()})


def put_back_rows(
    session: Session, cmap: CategoryMap, today: dt.date, category: str,
    states: dict[str, tuple | None],
) -> None:
    """Undo (Goals: the page's Undo of adding a goal that took over a category): put one
    category's budgets rows back exactly as they were (``None``: no row). Refused with
    ``BudgetError`` when the result would break the move-out rule in a month (only a
    violation this creates or worsens) or lower Ready to Assign below zero."""
    put_back_many(session, cmap, today, {category: states})


def put_back_many(
    session: Session, cmap: CategoryMap, today: dt.date,
    changes: dict[str, dict[str, tuple | None]],
) -> None:
    """``put_back_rows`` for several categories at once: every row is checked against the
    result of all of them (one Ready to Assign check), and nothing is written on a refusal."""
    for category in changes:
        _check_category(cmap, category)
    changes = {
        c: {m: as_row_state(state) for m, state in states.items()} for c, states in changes.items() if states
    }
    if not changes:
        return
    first = min(min(states) for states in changes.values())
    book = load_book(session, cmap, today, first)
    after: Rows = {c: dict(by_month) for c, by_month in book.rows.items()}
    for category, states in changes.items():
        by_month = after.setdefault(category, {})
        for month, state in states.items():
            if state is None:
                by_month.pop(month, None)
            else:
                by_month[month] = Row(*state)
    after_lines = book.run(after)
    for category in changes:
        by_month = after[category]
        for month in sorted(m for m in by_month if m >= first):
            row = by_month[month]
            if row.removed or row.cents >= 0 or category not in after_lines.get(month, {}):
                continue
            carry = _carryover(after_lines, category, month)
            if carry + row.cents < 0 and carry < _carryover(book.lines, category, month):
                raise BudgetError(f"{cmap.get(category).name} doesn't have that money any more.")
    ready_before = book.ready_to_assign()
    if book.ready_to_assign(after, after_lines) < min(ready_before, 0):
        raise BudgetError(f"Only {fmt_usd(max(0, ready_before))} is ready to assign.")
    for category, states in changes.items():
        for month, state in states.items():
            row = session.scalar(select(Budget).where(Budget.month == month, Budget.category == category))
            if state is None:
                if row is not None:
                    session.delete(row)
            elif row is None:
                session.add(Budget(
                    month=month, category=category, limit_cents=state[0], removed=state[1], restart=state[2],
                    moved_cents=state[3],
                ))
            else:
                row.limit_cents, row.removed, row.restart, row.moved_cents = state
    session.flush()


def _check_income_change(
    cmap: CategoryMap, book: Book, after: Rows, after_lines: dict[str, dict[str, Line]],
    month: str, cents: dict[str, int], income: IncomeChange,
) -> int:
    """Validate a change of this month's expected income; returns ``pending`` after it."""
    inc = book.income
    if month != book.current:
        raise BudgetError(MONTH_AMOUNT_ONLY_NOW)
    if inc.mode != "expected":
        raise BudgetError(
            "The month's amount follows the money in your accounts. "
            "Choose “Count paychecks I expect this month” to change it."
        )
    expected = income.expected if income.expected is not None else inc.suggested
    # Undo (restore) may put back an amount below what came in since: that is the state the
    # month was in before, and the "earned more" note comes back with it.
    if income.expected is not None and income.expected < inc.received and not income.restore:
        # The smallest month amount allowed: the one where the expected income is what came in.
        least = book.ready_to_assign(after, after_lines, 0) + book.planned(month, after_lines)
        raise BudgetError(
            f"{fmt_usd(inc.received)} has already come in this month, so pick at least {fmt_usd(least)}."
        )
    if income.accept_received:
        if income.expected is None or income.expected != inc.received:
            raise BudgetError("Leave it for next month sets the month's amount to what came in, nothing else.")
        members = book.members(month)
        for category, value in cents.items():
            if value > (members[category].assigned if category in members else 0):
                raise BudgetError("Leave it for next month can't raise a plan.")
    return inc.pending_for(expected)


def set_budget_settings(
    session: Session, today: dt.date, *, income_mode: str | object = UNSET, savings: str | None | object = UNSET
) -> None:
    """``PUT /api/budgets/settings``: the income switch keeps every stored month's amount."""
    if income_mode is not UNSET:
        if income_mode not in INCOME_MODES:
            raise BudgetError("income_mode must be expected or off.")
        settings = load_income_settings(session)
        settings.mode = str(income_mode)
        store_income_settings(session, settings, today)
    if savings is not UNSET:
        if savings is None:
            put_setting(session, BUDGET_SAVINGS_SETTING, None)
        else:
            cmap = load_categories(session)
            if savings not in cmap.by_id:
                raise BudgetError("Unknown category.")
            if cmap.kind(savings) != "spending":
                raise BudgetError(f"{cmap.get(savings).name} isn't a spending category.")
            put_setting(session, BUDGET_SAVINGS_SETTING, str(savings))


# ------------------------------------------------------------------ targets


def fund_targets(session: Session, month: str, today: dt.date) -> tuple[int, int]:
    """Assign each member's ``needed`` in ``month`` while Ready to Assign lasts.

    Order: group position (ungrouped last), then category position, then name. The last
    category funded may get only part of what it needs; after that, stop. Written through
    ``save_budget_cents`` so every Release 2.1 rule applies and the save is atomic.
    Returns ``(funded, unfunded)`` in cents.
    """
    check_editable_month(month, today)
    cmap = load_categories(session)
    book = load_book(session, cmap, today, month)
    lines = book.members(month)
    totals = bill_totals(month_bills(session, month, today, book))
    needs = {c: needed_cents(cmap.get(c).target, month, lines[c], totals.get(c, 0)) for c in lines}
    group_pos = {g.id: (int(g.position or 0), g.id) for g in _groups(session)}

    def order(category: str) -> tuple:
        info = cmap.get(category)
        group = group_pos.get(info.group_id) if info.group_id is not None else None
        return (group is None, group or (0, 0), info.position, info.name.lower(), info.id)

    budget = max(0, book.ready_to_assign())
    assigned: dict[str, int] = {}
    for category in sorted((c for c in needs if needs[c] > 0), key=order):
        if budget <= 0:
            break
        amount = min(needs[category], budget)
        assigned[category] = lines[category].assigned + amount
        budget -= amount
    funded = sum(assigned[c] - lines[c].assigned for c in assigned)
    if assigned:
        save_budget_cents(session, cmap, month, assigned, [], today)
    return funded, sum(needs.values()) - funded


# ------------------------------------------------------------------ alerts


def budget_book(session: Session, today: dt.date) -> tuple[CategoryMap, Book]:
    """The envelope book as of today (for the ``income`` alert's Ready to Assign)."""
    cmap = load_categories(session)
    return cmap, load_book(session, cmap, today)


def budget_alert_lines(session: Session, today: dt.date) -> tuple[CategoryMap, dict[str, Line]]:
    """This month's member envelopes (for the ``budget`` alert)."""
    cmap = load_categories(session)
    book = load_book(session, cmap, today)
    return cmap, book.members(book.current)


# ------------------------------------------------------------------ review


def _pct(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator * 100, 1)


def review(session: Session, month: str, today: dt.date) -> dict:
    cmap = load_categories(session)
    t = month_totals(session, cmap, month)
    rows = _budget_rows(session, cmap)
    budgeted = sum(t.spent(c) for c in _members_at(rows, month))
    total_spent = t.total_spent
    other = total_spent - budgeted
    left_over = t.income - t.total_fixed - budgeted - other

    prev_month = shift_month(month, -1)
    prev_spent = month_totals(session, cmap, prev_month).total_spent

    biggest = None
    by_cat = t.spent_by_category()
    if by_cat:
        top = min(by_cat, key=lambda c: (-by_cat[c], cmap.sort_key(c)))
        biggest = _line(cmap, top, spent=from_cents(by_cat[top]))

    most_visited = None
    if t.merchants:
        def rank(key: str) -> tuple:
            rows = t.merchants[key]
            return (-visits(rows), sum(r.amount_cents for r in rows), key)

        best = min(t.merchants, key=rank)
        best_rows = t.merchants[best]
        names = Counter(display_name(r.merchant_name, r.name) for r in best_rows)
        most_visited = {
            "merchant": names.most_common(1)[0][0],
            "count": visits(best_rows),
            "spent": from_cents(max(0, -sum(r.amount_cents for r in best_rows))),
        }

    history = []
    # Release 3.17: what each month planned, repeated plans included (before the repeat start
    # month that's exactly its rows).
    plans = plans_by_month(session, cmap, today, shift_month(month, -5), month)
    for back in range(5, -1, -1):
        m = shift_month(month, -back)
        mt = month_totals(session, cmap, m) if back else t
        assigned = sum(plans.get(m, {}).values())
        history.append({"month": m, "spent": from_cents(mt.total_spent), "assigned": from_cents(assigned)})

    return {
        "month": month,
        "income": from_cents(t.income),
        "fixed": from_cents(t.total_fixed),
        "budgeted_spending": from_cents(budgeted),
        "other_spending": from_cents(other),
        "left_over": from_cents(left_over),
        "left_over_pct": _pct(left_over, t.income),
        "compare": {
            "prev_month": prev_month,
            "prev_spent": from_cents(prev_spent),
            "spent": from_cents(total_spent),
            "delta_pct": _pct(total_spent - prev_spent, prev_spent),
        },
        "biggest": biggest,
        "most_visited": most_visited,
        "history": history,
    }


def last_complete_months(today: dt.date, count: int) -> list[str]:
    current = month_str(today.year, today.month)
    return [shift_month(current, -k) for k in range(count, 0, -1)]

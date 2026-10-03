"""Cash forecast calendar (Release 3.6): occurrences, paid/late matching and the projection.

**Single source of truth.** Everything that asks "what happens to a recurring item on which
day" reads the occurrences generated here: the calendar route (``build_calendar``), the
``low_ahead`` and ``reminder`` alerts, and the Budget screen's bills
(``bill_occurrences`` / ``income_occurrences``). An occurrence's status (paid by a
transaction or by the user, skipped, moved, pending, late...) is decided in one place,
``_generate``, so the calendar and the budget can never disagree.

Occurrence rules (SPEC "Release 3.6"):
- Base dates come from the item's rule (``recurring.rule_dates``), both directions from
  ``next_date``, never before ``start_date``. An override row can move one (``moved_to``),
  skip it or mark it paid. Overrides whose base isn't on the current rule are ignored.
- Bases in [today, next_date) exist only when paid: detection already consumed them.
- Matching: same merchant key and sign, the item's account (any visible one when it has
  none), transfers and pending rows included, within ±3/5/7 days (weekly / biweekly and
  semimonthly / otherwise) of the effective date, amount 0.5×–2× the expected one. In date
  order, each transaction is used once, the nearest date wins (then the nearest amount,
  then the lowest id).
"""
from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Account, AlertSetting, AppSetting, RecurringItem, RecurringOverride, Transaction, TxnCategory
from ..utils import add_months, days_in_month, from_cents, month_of
from .categories import load as load_categories
from .forecast import daily_spend_cents, forecast_account, low_threshold_cents
from .recurring import (
    DAY_STEPS,
    MONTH_STEPS,
    PERIOD_DAYS,
    is_rule_date as _is_rule_date,
    item_aliases,
    match_window,
    matched_history,
    merchant_key,
    next_base,
    parse_days,
    recurring_out,
    rule_dates,
)

HORIZON_MONTHS = 12
PAST_DAYS = 62  # how far back ``from`` may go
AHEAD_DAYS = 400  # how far ahead ``to`` may go
DEFAULT_DAYS = 35
CARRY_MAX_DAYS = 14
PENDING_DAYS = 3
MAX_WINDOW = max(7, 5, 3)

INCLUDE_DAILY_KEY = "forecast_include_daily"
EXCLUDED_PLANS_KEY = "forecast_excluded_plans"
MAX_EXCLUDED_PLANS = 200

KIND_ORDER = {"in": 0, "out": 1, "card": 2, "plan": 3}
# Kinds of suggested items shown as "Maybe" on the calendar (bills, card charges and paychecks).
MAYBE_KINDS = ("in", "out", "card")


def horizon_end(today: dt.date) -> dt.date:
    """The last day of the month 12 months after today's month (the calendar's last month)."""
    month = add_months(today.replace(day=1), HORIZON_MONTHS, 1)
    return month.replace(day=days_in_month(month.year, month.month))


def is_rule_date(item: RecurringItem, day: dt.date) -> bool:
    """``day`` is one of the item's base dates under its current rule."""
    return _is_rule_date(item, day)


# ------------------------------------------------------------------ settings


def _setting(session: Session, key: str) -> str | None:
    row = session.get(AppSetting, key)
    return row.value if row is not None else None


def _put(session: Session, key: str, value: str) -> None:
    row = session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    session.flush()


def include_daily(session: Session) -> bool:
    return _setting(session, INCLUDE_DAILY_KEY) != "0"


def excluded_plans(session: Session) -> list[str]:
    """Category ids whose by-date plan isn't counted (ids of deleted categories are dropped)."""
    try:
        raw = json.loads(_setting(session, EXCLUDED_PLANS_KEY) or "[]")
    except ValueError:
        return []
    if not isinstance(raw, list):
        return []
    ids = [x for x in raw if isinstance(x, str)]
    if not ids:
        return []
    known = set(session.scalars(select(TxnCategory.id).where(TxnCategory.id.in_(ids))))
    return [x for x in dict.fromkeys(ids) if x in known]


def settings_out(session: Session) -> dict:
    return {"include_daily": include_daily(session), "excluded_plans": excluded_plans(session)}


def save_settings(session: Session, *, include: bool | None = None, excluded: list[str] | None = None) -> None:
    if include is not None:
        _put(session, INCLUDE_DAILY_KEY, "1" if include else "0")
    if excluded is not None:
        _put(session, EXCLUDED_PLANS_KEY, json.dumps(list(dict.fromkeys(excluded))[:MAX_EXCLUDED_PLANS]))


# ------------------------------------------------------------------ occurrences


@dataclass
class Occ:
    """One occurrence of a recurring item (after moves, skips and matching)."""

    item: RecurringItem
    kind: str  # in | out | card
    base_date: dt.date
    date: dt.date  # effective: moved_to or base_date
    status: str  # upcoming | pending | late | paid | past | skipped
    override: RecurringOverride | None
    actual_cents: int | None = None  # signed; paid only
    actual_date: dt.date | None = None
    transaction_id: int | None = None
    by: str | None = None  # match | you
    carry_ok: bool = False  # still expected: may be counted tomorrow (today's, pending, late in window)

    @property
    def recurring_id(self) -> int:
        return self.item.id

    @property
    def key(self) -> str:
        return f"r{self.item.id}:{self.base_date.isoformat()}"

    @property
    def name(self) -> str:
        return self.item.name

    @property
    def category_id(self) -> str | None:
        return self.item.category_id

    @property
    def amount_cents(self) -> int:
        """The expected amount, signed."""
        return self.item.amount_cents


def item_kind(item: RecurringItem, accounts: dict[int, Account]) -> str:
    account = accounts.get(item.account_id) if item.account_id is not None else None
    if account is not None and account.category == "credit":
        return "card"
    return "in" if item.amount_cents > 0 else "out"


def on_calendar(
    item: RecurringItem, forecast: Account | None, accounts: dict[int, Account], *, visible_only: bool = False,
) -> bool:
    """Calendar-eligible: no account (follows the forecast account), the forecast account, or a card.

    ``visible_only`` (suggestions: Maybe rows and ``candidates``' ``on_calendar``) also leaves out
    items on a hidden account, so a suggestion never brings a hidden account's charges onto the
    page. Active items keep the old rule (the person put them there).
    """
    if forecast is None:
        return False
    if item.account_id is None:
        return True
    account = accounts.get(item.account_id)
    if account is None:
        return True
    if visible_only and account.hidden:
        return False
    return account.id == forecast.id or account.category == "credit"


def _trackable(item: RecurringItem) -> bool:
    return item.source != "manual" or item.last_seen_date is not None


def _amount_ok(expected: int, actual: int) -> bool:
    e, a = abs(expected), abs(actual)
    return 2 * a >= e and a <= 2 * e


class _Txns:
    """Transactions dated [lo, today] indexed by (merchant key, sign), for matching."""

    def __init__(
        self, session: Session, lo: dt.date, today: dt.date, items: list[RecurringItem] | None = None,
    ) -> None:
        # Release 3.19: other (account, merchant key) pairs a renamed or moved bill used.
        self.aliases = item_aliases(session, items) if items else {}
        rows = session.execute(
            select(
                Transaction.id, Transaction.account_id, Transaction.date, Transaction.name,
                Transaction.merchant_name, Transaction.amount_cents, Account.hidden,
            )
            .join(Account, Account.id == Transaction.account_id)
            .where(Transaction.date >= lo, Transaction.date <= today, Transaction.amount_cents != 0)
        ).all()
        self.by_key: dict[tuple[str, int], list] = defaultdict(list)
        for row in rows:
            sign = 1 if row.amount_cents > 0 else -1
            self.by_key[(merchant_key(row.merchant_name, row.name), sign)].append(row)

    def for_item(self, item: RecurringItem) -> list:
        sign = 1 if item.amount_cents > 0 else -1
        rows = self.by_key.get((item.merchant_key, sign), [])
        if item.account_id is not None:
            mine = [r for r in rows if r.account_id == item.account_id]
        else:
            mine = [r for r in rows if not r.hidden]
        aliases = self.aliases.get(item.id)
        if not aliases:
            return mine
        seen = {r.id for r in mine}
        for account_id, key in aliases:
            for r in self.by_key.get((key, sign), []):
                if r.account_id == account_id and not r.hidden and r.id not in seen:
                    seen.add(r.id)
                    mine.append(r)
        return mine


def _generate(
    session: Session, today: dt.date, lo: dt.date, hi: dt.date, items: list[RecurringItem],
    accounts: dict[int, Account],
) -> list[Occ]:
    """Every occurrence of ``items`` whose effective date is in [lo, hi], with its status.

    Callers filter further (the calendar keeps [from, to] plus carried ones). Matching looks
    a little before ``lo`` so an occurrence near the edge is matched the same way whatever
    range was asked for.
    """
    if not items:
        return []
    gen_lo = lo - dt.timedelta(days=MAX_WINDOW + 1)
    overrides: dict[int, dict[dt.date, RecurringOverride]] = defaultdict(dict)
    for ov in session.scalars(
        select(RecurringOverride).where(RecurringOverride.recurring_id.in_([i.id for i in items]))
    ):
        overrides[ov.recurring_id][ov.base_date] = ov
    txns = _Txns(session, gen_lo - dt.timedelta(days=MAX_WINDOW), today, items)

    out: list[Occ] = []
    for item in items:
        item_ovs = overrides.get(item.id, {})
        bases = set(rule_dates(item, gen_lo, hi))
        for base, ov in item_ovs.items():
            # A base outside the range that was moved into it.
            if ov.moved_to is not None and gen_lo <= ov.moved_to <= hi and base not in bases \
                    and is_rule_date(item, base):
                bases.add(base)
        kind = item_kind(item, accounts)
        window = match_window(item.cadence)
        candidates = txns.for_item(item)
        used: set[int] = set()
        occs: list[Occ] = []
        for base in bases:
            ov = item_ovs.get(base)
            day = ov.moved_to if ov is not None and ov.moved_to is not None else base
            occs.append(Occ(item=item, kind=kind, base_date=base, date=day, status="upcoming", override=ov))
        occs.sort(key=lambda o: (o.date, o.base_date))
        for occ in occs:
            ov = occ.override
            if ov is not None and ov.skipped:
                occ.status = "skipped"
                continue
            if ov is not None and ov.paid:
                occ.status, occ.by = "paid", "you"
                occ.actual_cents = ov.paid_amount_cents if ov.paid_amount_cents is not None else item.amount_cents
                continue
            best = None
            for row in candidates:
                if row.id in used or abs((row.date - occ.date).days) > window:
                    continue
                if not _amount_ok(item.amount_cents, row.amount_cents):
                    continue
                rank = (abs((row.date - occ.date).days), abs(abs(row.amount_cents) - abs(item.amount_cents)), row.id)
                if best is None or rank < best[0]:
                    best = (rank, row)
            if best is not None:
                row = best[1]
                used.add(row.id)
                occ.status, occ.by = "paid", "match"
                occ.actual_cents, occ.actual_date, occ.transaction_id = row.amount_cents, row.date, row.id
                continue
            if occ.date > today:
                occ.status = "upcoming"
            elif occ.date == today:
                occ.status, occ.carry_ok = "upcoming", True
            elif not _trackable(item) or (item.last_seen_date is not None and occ.base_date <= item.last_seen_date):
                # Never tracked (added by hand, never seen), or a later payment was already seen.
                occ.status = "past"
            else:
                age = (today - occ.date).days
                occ.status = "pending" if age <= PENDING_DAYS else "late"
                occ.carry_ok = age <= min(CARRY_MAX_DAYS, PERIOD_DAYS.get(item.cadence, 30))
        for occ in occs:
            # Bases detection already consumed (moved next_date past them) exist only when paid.
            if today <= occ.base_date < item.next_date and occ.status != "paid":
                continue
            if lo <= occ.date <= hi or occ.carry_ok:
                out.append(occ)
    return out


def occurrences_for(
    session: Session, today: dt.date, start: dt.date, end: dt.date, items: list[RecurringItem] | None = None,
) -> list[Occ]:
    """Occurrences of active items (all accounts) whose effective date is in [start, end], by date."""
    if items is None:
        items = list(session.scalars(
            select(RecurringItem).where(RecurringItem.status == "active").order_by(RecurringItem.id)
        ))
    accounts = {a.id: a for a in session.scalars(select(Account))}
    occs = [o for o in _generate(session, today, start, end, items, accounts) if start <= o.date <= end]
    occs.sort(key=lambda o: (o.date, KIND_ORDER.get(o.kind, 9), o.name.lower(), o.key))
    return occs


def bill_occurrences(
    session: Session, today: dt.date, start: dt.date, end: dt.date, account_ids: set[int] | None = None,
) -> dict[str, list[Occ]]:
    """Bills (money out) of active items with a budget category, by category id.

    ``account_ids`` narrows them to items paid from those accounts (or from no account);
    None = every account. The budget passes its own accounts plus every credit card (a bill
    charged to a card still belongs to its budget category even though it doesn't move
    checking). Effective date in [start, end];
    each ``Occ`` carries its status (upcoming / pending / late / paid / past / skipped), the
    actual paid amount (``actual_cents``, signed) after moves, skips and matching. The
    budget's "needed" for a ``bills`` target should use the statuses that still cost money
    (everything but ``skipped``; ``paid`` at ``actual_cents``). This and the calendar share
    ``_generate``: the single source of truth for occurrences.
    """
    items = None
    if account_ids is not None:
        items = [
            item for item in session.scalars(
                select(RecurringItem).where(RecurringItem.status == "active").order_by(RecurringItem.id)
            )
            if item.account_id is None or item.account_id in account_ids
        ]
    out: dict[str, list[Occ]] = defaultdict(list)
    for occ in occurrences_for(session, today, start, end, items):
        if occ.item.amount_cents < 0 and occ.item.category_id is not None:
            out[occ.item.category_id].append(occ)
    return dict(out)


def income_occurrences(
    session: Session, today: dt.date, start: dt.date, end: dt.date, items: list[RecurringItem] | None = None,
) -> list[Occ]:
    """Income (money in) occurrences of active items on any account, with or without a
    category, effective date in [start, end], by date. Same rules as ``bill_occurrences``.
    ``items`` narrows the items (the budget passes the ones on its accounts)."""
    return [o for o in occurrences_for(session, today, start, end, items) if o.item.amount_cents > 0]


# ------------------------------------------------------------------ output


def override_out(ov: RecurringOverride | None) -> dict | None:
    if ov is None:
        return None
    return {
        "base_date": ov.base_date.isoformat(),
        "moved_to": ov.moved_to.isoformat() if ov.moved_to else None,
        "skipped": bool(ov.skipped),
        "paid": bool(ov.paid),
        # Positive, as the PUT body takes it (stored with the item's sign).
        "paid_amount": from_cents(abs(ov.paid_amount_cents)) if ov.paid_amount_cents is not None else None,
    }


def _account_ref(account: Account | None) -> dict | None:
    if account is None:
        return None
    return {"id": account.id, "name": account.name, "mask": account.mask, "category": account.category}


def _iso(day: dt.date | None) -> str | None:
    return day.isoformat() if day is not None else None


def _alert(session: Session, key: str) -> AlertSetting | None:
    return session.get(AlertSetting, key)


def build_calendar(session: Session, today: dt.date, start: dt.date, end: dt.date) -> dict:
    """``ForecastCalendar`` for [start, end]; the projection always starts from today."""
    tomorrow = today + dt.timedelta(days=1)
    low_ahead = _alert(session, "low_ahead")
    reminder = _alert(session, "reminder")
    daily_on = include_daily(session)
    result: dict = {
        "account": None,
        "today": today.isoformat(),
        "from": start.isoformat(),
        "to": end.isoformat(),
        "horizon_end": horizon_end(today).isoformat(),
        "balance": 0.0,
        "threshold": from_cents(low_threshold_cents(session)),
        "daily_spend": 0.0,
        "include_daily": daily_on,
        "low_ahead": {
            "enabled": bool(low_ahead.enabled) if low_ahead is not None else True,
            "days": int(low_ahead.value) if low_ahead is not None and low_ahead.value else 7,
        },
        "reminders_enabled": bool(reminder.enabled) if reminder is not None else True,
        "days": [],
        "occurrences": [],
        "months": [],
        "first_dip": None,
        "series": [],
        "maybe": [],
    }
    account = forecast_account(session)
    if account is None:
        return result
    threshold = low_threshold_cents(session)
    active = list(session.scalars(
        select(RecurringItem).where(RecurringItem.status == "active").order_by(RecurringItem.id)
    ))
    accounts = {a.id: a for a in session.scalars(select(Account))}
    items = [i for i in active if on_calendar(i, account, accounts)]
    daily = daily_spend_cents(session, account, today, active)
    cmap = load_categories(session)

    series_counted = {i.id: bool(i.include_in_forecast) and item_kind(i, accounts) in ("in", "out") for i in items}
    nexts = {i.id: next_base(i, today) for i in items}

    # Occurrences: the projection needs every counted one from today on, even before ``start``.
    lo = min(start, today - dt.timedelta(days=CARRY_MAX_DAYS))
    counted_on_day: dict[dt.date, list[dict]] = defaultdict(list)
    occ_out: list[dict] = []
    for occ in _generate(session, today, lo, end, items, accounts):
        item = occ.item
        counted_on = None
        if series_counted[item.id]:
            if occ.status == "upcoming" and occ.date > today:
                counted_on = occ.date
            elif occ.carry_ok:
                counted_on = tomorrow
        carried = counted_on is not None and counted_on != occ.date
        can_move_all = item.cadence not in ("once", "semimonthly")
        row = {
            "key": occ.key,
            "series": f"r{item.id}",
            "recurring_id": item.id,
            "plan_category": None,
            "category_id": item.category_id,
            "base_date": occ.base_date.isoformat(),
            "date": occ.date.isoformat(),
            "name": item.name,
            "amount": from_cents(item.amount_cents),
            "kind": occ.kind,
            "status": occ.status,
            "counted": counted_on is not None,
            "counted_on": _iso(counted_on),
            "carried": carried,
            "actual": None if occ.status != "paid" else {
                "amount": from_cents(occ.actual_cents),
                "date": _iso(occ.actual_date),
                "transaction_id": occ.transaction_id,
                "by": occ.by,
            },
            "override": override_out(occ.override),
            "movable": occ.status not in ("paid", "skipped", "past"),
            "next_in_series": can_move_all and occ.base_date == nexts[item.id],
            "reminder_days": int(item.reminder_days or 0),
            "maybe": False,
        }
        if counted_on is not None and today < counted_on <= end:
            counted_on_day[counted_on].append({"key": occ.key, "amount": item.amount_cents, "row": row})
        if start <= occ.date <= end or carried:
            occ_out.append(row)

    # Plans: non-hidden spending categories with a by-date target in [today, end].
    excluded = set(excluded_plans(session))
    plan_series: list[dict] = []
    for cat in cmap.by_id.values():
        target = cat.target
        if cat.hidden or target is None or target.kind != "by_date" or target.date is None:
            continue
        if not (today <= target.date <= end):
            continue
        counted = cat.id not in excluded
        counted_on = max(target.date, tomorrow) if counted else None
        key = f"p:{cat.id}:{target.date.isoformat()}"
        row = {
            "key": key,
            "series": f"p:{cat.id}",
            "recurring_id": None,
            "plan_category": cat.id,
            "category_id": cat.id,
            "base_date": target.date.isoformat(),
            "date": target.date.isoformat(),
            "name": cat.name,
            "amount": from_cents(-target.cents),
            "kind": "plan",
            "status": "upcoming",
            "counted": counted,
            "counted_on": _iso(counted_on),
            "carried": counted_on is not None and counted_on != target.date,
            "actual": None,
            "override": None,
            "movable": False,
            "next_in_series": False,
            "reminder_days": 0,
            "maybe": False,
        }
        if counted_on is not None and counted_on <= end:
            counted_on_day[counted_on].append({"key": key, "amount": -target.cents, "row": row})
        if start <= target.date <= end:
            occ_out.append(row)
        plan_series.append({
            "id": f"p:{cat.id}", "recurring_id": None, "plan_category": cat.id, "name": cat.name,
            "amount": from_cents(-target.cents), "cadence": "once", "anchor_days": None,
            "next_date": target.date.isoformat(), "kind": "plan", "source": "spending", "account": None,
            "counted": counted, "reminder_days": 0, "can_move_all": False, "category_id": cat.id,
        })

    occ_out.sort(key=lambda o: (o["date"], 0 if o["amount"] > 0 else 1, o["name"].lower(), o["key"]))

    # Projection (cents), from today whatever ``start`` is.
    balances: dict[dt.date, int] = {}
    if end >= today:
        balance = account.current_balance_cents
        balances[today] = balance
        day = tomorrow
        while day <= end:
            balance -= daily if daily_on else 0
            balance += sum(c["amount"] for c in counted_on_day.get(day, ()))
            balances[day] = balance
            day += dt.timedelta(days=1)

    def cause(day: dt.date) -> str | None:
        outflows = [c for c in counted_on_day.get(day, ()) if c["amount"] < 0]
        if not outflows:
            return None
        ordered = sorted(outflows, key=lambda c: (c["amount"], c["row"]["name"].lower(), c["key"]))
        return ordered[0]["key"]

    def dip(day: dt.date) -> dict:
        return {"date": day.isoformat(), "balance": from_cents(balances[day]), "cause_key": cause(day)}

    days_out = []
    day = start
    while day <= end:
        bal = balances.get(day) if day >= today else None
        days_out.append({"date": day.isoformat(), "balance": from_cents(bal),
                         "below": bal is not None and bal < threshold})
        day += dt.timedelta(days=1)

    months_out = []
    if end >= today:
        month_start = today.replace(day=1)
        while month_start <= end:
            last = month_start.replace(day=days_in_month(month_start.year, month_start.month))
            span = [d for d in _days(max(month_start, today), min(last, end))]
            low_day = min(span, key=lambda d: (balances[d], d))
            first_below = next((d for d in span if balances[d] < threshold), None)
            months_out.append({
                "month": month_of(month_start),
                "low": from_cents(balances[low_day]),
                "low_date": low_day.isoformat(),
                "end": from_cents(balances[last]) if last <= end else None,
                "end_date": last.isoformat(),
                "first_dip": dip(first_below) if first_below is not None else None,
            })
            month_start = add_months(month_start, 1, 1)

    first_dip_day = next((d for d in _days(max(start, today), end) if balances[d] < threshold), None)

    series_out = []
    for item in items:
        account_ref = _account_ref(accounts.get(item.account_id)) if item.account_id is not None else None
        series_out.append({
            "id": f"r{item.id}",
            "recurring_id": item.id,
            "plan_category": None,
            "name": item.name,
            "amount": from_cents(item.amount_cents),
            "cadence": item.cadence,
            "anchor_days": parse_days(item.month_days),
            "next_date": item.next_date.isoformat(),
            "kind": item_kind(item, accounts),
            "source": item.source,
            "account": account_ref,
            "counted": series_counted[item.id],
            "reminder_days": int(item.reminder_days or 0),
            "can_move_all": item.cadence not in ("once", "semimonthly"),
            "category_id": item.category_id,
        })
    series_out.sort(key=lambda s: (KIND_ORDER.get(s["kind"], 9), s["name"].lower(), s["id"]))
    plan_series.sort(key=lambda s: (s["next_date"], s["name"].lower()))

    result.update(
        account={
            "id": account.id,
            "name": account.name,
            "mask": account.mask,
            "institution_name": account.institution_name,
        },
        balance=from_cents(account.current_balance_cents),
        daily_spend=from_cents(daily),
        days=days_out,
        occurrences=occ_out,
        months=months_out,
        first_dip=dip(first_dip_day) if first_dip_day is not None else None,
        series=series_out + plan_series,
        maybe=maybe_rows(session, today, start, end, account, accounts),
    )
    return result


def maybe_rows(
    session: Session, today: dt.date, start: dt.date, end: dt.date, forecast: Account,
    accounts: dict[int, Account],
) -> list[dict]:
    """``ForecastCalendar.maybe``: upcoming occurrences of **suggested** items ("Maybe" bills).

    Same calendar filter (minus hidden accounts) and ``_generate`` as active items, effective date in
    [max(start, today), end]. Display only: never counted, never in ``days``/``months``/
    ``first_dip``/``series``, reminders, ``low_ahead`` or Home (they read ``occurrences``).
    One already matched to a payment (status paid) is left out: it isn't "coming up".
    """
    lo = max(start, today)
    if lo > end:
        return []
    items = [
        i for i in session.scalars(
            select(RecurringItem).where(RecurringItem.status == "suggested").order_by(RecurringItem.id)
        )
        if on_calendar(i, forecast, accounts, visible_only=True)
    ]
    out = []
    for occ in _generate(session, today, lo, end, items, accounts):
        if not (lo <= occ.date <= end) or occ.status != "upcoming" or occ.kind not in MAYBE_KINDS:
            continue
        item = occ.item
        out.append({
            "key": occ.key,
            "series": f"r{item.id}",
            "recurring_id": item.id,
            "plan_category": None,
            "category_id": item.category_id,
            "base_date": occ.base_date.isoformat(),
            "date": occ.date.isoformat(),
            "name": item.name,
            "amount": from_cents(item.amount_cents),
            "kind": occ.kind,
            "status": "upcoming",
            "counted": False,
            "counted_on": None,
            "carried": False,
            "actual": None,
            "override": None,
            "movable": False,
            "next_in_series": False,
            "reminder_days": 0,
            "maybe": True,
        })
    out.sort(key=lambda o: (o["date"], 0 if o["amount"] > 0 else 1, o["name"].lower(), o["key"]))
    return out


def _days(start: dt.date, end: dt.date) -> list[dt.date]:
    out = []
    day = start
    while day <= end:
        out.append(day)
        day += dt.timedelta(days=1)
    return out


# ------------------------------------------------------------------ candidates


def _is_checking(account: Account) -> bool:
    return account.category == "bank" and (account.plaid_subtype or "checking").lower() == "checking"


def candidates(session: Session, today: dt.date) -> list[dict]:
    """``RecurringCandidate[]``: suggested items with the evidence for suggesting them."""
    items = list(session.scalars(select(RecurringItem).where(RecurringItem.status == "suggested")))
    if not items:
        return []
    forecast = forecast_account(session)
    accounts = {a.id: a for a in session.scalars(select(Account))}
    names = {c.id: c.name for c in session.scalars(select(TxnCategory))}
    history = matched_history(session, items, today)
    out = []
    for item in items:
        account = accounts.get(item.account_id) if item.account_id is not None else None
        if account is not None and account.category == "credit":
            paid_with = "card"
        elif account is None or _is_checking(account):
            paid_with = "checking"
        else:
            paid_with = "other"
        dates = sorted({r.date for r in history.get(item.id, [])})
        day_of_month = None
        if item.cadence in MONTH_STEPS:
            days = parse_days(item.month_days)
            nd = item.next_date
            day_of_month = days[0] if days else (31 if nd.day == days_in_month(nd.year, nd.month) else nd.day)
        weekday = (item.next_date.weekday() + 1) % 7 if item.cadence in DAY_STEPS else None
        last = dates[-1] if dates else item.last_seen_date
        out.append({
            "item": recurring_out(item, account.name if account else None, names.get(item.category_id)),
            "account": _account_ref(account),
            "paid_with": paid_with,
            "on_calendar": on_calendar(item, forecast, accounts, visible_only=True),
            "day_of_month": day_of_month,
            "weekday": weekday,
            "months_seen": sorted({month_of(d) for d in dates})[-4:],
            "count": len(dates),
            "last_date": _iso(last),
        })
    out.sort(key=lambda c: (-c["count"], c["item"]["name"].lower(), c["item"]["id"]))
    return out

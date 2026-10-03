"""Loan payoff projections and the debt planner (SPEC Release 3 section 8).

All balances are integer cents (positive = owed). Credit cards assume no new charges.
Each month: ``interest = round_cents(balance x apr/100/12)``, then payments.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Account, BalanceSnapshot
from ..schemas import loan_group
from ..utils import LIABILITY_CATEGORIES, from_cents, month_bounds, month_of, shift_month
from . import account_facts

MAX_MONTHS = 600
# Stop simulating a balance that only grows once it passes $1 trillion (it will never be paid).
RUNAWAY_CENTS = 100_000_000_000_000


class LoanError(ValueError):
    """Validation failure; the message is safe to show (422)."""


def monthly_interest(balance: int, apr: float) -> int:
    """round_cents(balance x apr / 100 / 12), half away from zero, exact (no float drift)."""
    if balance <= 0 or apr <= 0:
        return 0
    value = Decimal(balance) * Decimal(str(apr)) / Decimal(1200)
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def payoff_date(day: dt.date, months: int | None) -> str | None:
    """The first of the month ``months`` months after today's month; None if never."""
    if months is None:
        return None
    return f"{shift_month(month_of(day), months)}-01"


@dataclass
class Projection:
    months: int | None
    total_interest: int
    never: bool
    schedule: list[dict] = field(default_factory=list)  # cents


def project(balance: int, apr: float, payment: int, day: dt.date) -> Projection:
    """Month-by-month payoff of one balance with a fixed monthly payment (minimum + extra).

    ``payment = min(balance + interest, minimum + extra)``; stops at 0 or after 600 months
    (never). ``never`` also when the payment doesn't exceed the first month's interest.
    """
    if balance <= 0:
        return Projection(0, 0, False)
    first_interest = monthly_interest(balance, apr)
    never = payment <= first_interest
    schedule: list[dict] = []
    total = 0
    current = month_of(day)
    for k in range(1, MAX_MONTHS + 1):
        interest = monthly_interest(balance, apr)
        paid = min(balance + interest, payment)
        balance = balance + interest - paid
        total += interest
        schedule.append({"month": shift_month(current, k), "balance": balance, "interest": interest,
                         "principal": paid - interest})
        if balance == 0:
            return Projection(None if never else k, total, never, schedule)
        if balance > RUNAWAY_CENTS:
            break
    return Projection(None, total, True, schedule)


def _summary(p: Projection, day: dt.date) -> dict:
    return {
        "months": p.months,
        "payoff_date": payoff_date(day, p.months),
        "total_interest": from_cents(p.total_interest),
        "never": p.never,
    }


def liability(session: Session, account_id: int) -> Account:
    account = session.get(Account, account_id)
    if account is None:
        raise LookupError(account_id)
    if account.category not in LIABILITY_CATEGORIES:
        raise LoanError(f"{account.name} isn't a loan or credit card.")
    return account


def payoff(session: Session, account: Account, extra: int, minimum: int | None, day: dt.date) -> dict:
    """``PayoffProjection`` (history is added by the route)."""
    minimum = minimum if minimum is not None else account.minimum_payment_cents
    if minimum is None:
        raise LoanError(f"{account.name} has no minimum payment. Enter one to see a payoff date.")
    balance = max(0, int(account.current_balance_cents or 0))
    apr = float(account.interest_rate or 0.0)
    plan = project(balance, apr, minimum + extra, day)
    base = plan if extra == 0 else project(balance, apr, minimum, day)
    return {
        "account_id": account.id,
        "name": account.name,
        "balance": from_cents(balance),
        "apr": apr,
        "minimum": from_cents(minimum),
        "extra": from_cents(extra),
        **_summary(plan, day),
        "schedule": [
            {"month": s["month"], "balance": from_cents(s["balance"]), "interest": from_cents(s["interest"]),
             "principal": from_cents(s["principal"])}
            for s in plan.schedule
        ],
        "baseline": _summary(base, day),
    }


# ------------------------------------------------------------------ debt planner

KINDS = ("card", "loan", "mortgage")
MORTGAGE_SUBTYPES = frozenset({"mortgage", "home equity"})
MORTGAGE_GROUP = "Home loans"


def debt_kind(account: Account) -> str:
    """Release 3.10: card (credit), mortgage (a mortgage or home equity loan, or one in the
    "Home loans" group) or loan."""
    if account.category == "credit":
        return "card"
    if (account.plaid_subtype or "").strip().lower() in MORTGAGE_SUBTYPES or loan_group(account) == MORTGAGE_GROUP:
        return "mortgage"
    return "loan"


@dataclass
class Debt:
    account_id: int
    name: str
    group: str | None
    balance: int
    apr: float
    minimum: int
    kind: str = "loan"
    apr_known: bool = True


def avalanche_order(debts: list[Debt]) -> list[Debt]:
    """Highest APR first; ties go to the smaller balance."""
    return sorted(debts, key=lambda d: (-d.apr, d.balance, d.account_id))


def snowball_order(debts: list[Debt]) -> list[Debt]:
    """Smallest balance first; ties go to the higher APR."""
    return sorted(debts, key=lambda d: (d.balance, -d.apr, d.account_id))


@dataclass
class PlanResult:
    months: int | None
    total_interest: int
    never: bool
    payoff: dict[int, int | None]  # account_id -> months to pay off
    interest: dict[int, int]
    timeline: list[dict]
    applied: dict[int, int] = field(default_factory=dict)  # a one-time payment, per debt

    @property
    def interest_12(self) -> int:
        """Interest charged in the plan's first 12 months."""
        return sum(p["interest"] for p in self.timeline[:12])


def apply_lump(order: list[Debt], cents: int, target: int | None) -> dict[int, int]:
    """Release 3.10: a one-time payment at the start goes to ``target`` first (when given),
    then to the debts in plan order. Returns what each debt got (cents > 0 only)."""
    left = max(0, cents)
    applied: dict[int, int] = {}
    ids = ([target] if target is not None else []) + [d.account_id for d in order if d.account_id != target]
    balances = {d.account_id: max(0, d.balance) for d in order}
    for account_id in ids:
        if left <= 0:
            break
        paid = min(left, balances.get(account_id, 0))
        if paid > 0:
            applied[account_id] = paid
            left -= paid
    return applied


def simulate(order: list[Debt], extra: int, day: dt.date, lump: dict[int, int] | None = None) -> PlanResult:
    """Every debt pays its minimum; the extra plus minimums freed by paid-off debts go to the
    first unpaid debt in ``order`` (then the next, if it's paid off with money left).

    The monthly budget is constant: extra + every debt's minimum, until all are paid.
    ``lump`` (``apply_lump``) comes off the balances before the first month.
    """
    lump = lump or {}
    balances = {d.account_id: max(0, d.balance - lump.get(d.account_id, 0)) for d in order}
    interest_paid = {d.account_id: 0 for d in order}
    done: dict[int, int | None] = {d.account_id: 0 for d in order if balances[d.account_id] == 0}
    budget = extra + sum(d.minimum for d in order)
    timeline: list[dict] = []
    current = month_of(day)
    month = 0
    runaway = False
    while len(done) < len(order) and month < MAX_MONTHS:
        month += 1
        charged = 0
        for d in order:
            if balances[d.account_id] > 0:
                i = monthly_interest(balances[d.account_id], d.apr)
                balances[d.account_id] += i
                interest_paid[d.account_id] += i
                charged += i
        left = budget
        for d in order:  # minimums first
            owed = balances[d.account_id]
            if owed > 0:
                paid = min(owed, d.minimum, left)
                balances[d.account_id] -= paid
                left -= paid
        for d in order:  # then the extra and freed minimums, in plan order
            if left <= 0:
                break
            owed = balances[d.account_id]
            if owed > 0:
                paid = min(owed, left)
                balances[d.account_id] -= paid
                left -= paid
        for d in order:
            if balances[d.account_id] == 0 and d.account_id not in done:
                done[d.account_id] = month
        timeline.append({
            "month": shift_month(current, month),
            "total": sum(balances.values()),
            "interest": charged,
            "by_account": {str(d.account_id): balances[d.account_id] for d in order},
        })
        if sum(balances.values()) > RUNAWAY_CENTS:
            runaway = True
            break
    never = runaway or len(done) < len(order)
    payoff = {d.account_id: done.get(d.account_id) for d in order}
    months = None if never else max(payoff.values(), default=0)
    return PlanResult(months, sum(interest_paid.values()), never, payoff, interest_paid, timeline, dict(lump))


@dataclass
class Baseline:
    """Each debt pays only its own minimum (no extra, no rollover): "today's payments"."""

    months: int | None
    total_interest: int
    per_debt: dict[int, int | None]  # account_id -> months (None: never)
    timeline: list[dict]  # [{"month", "total"}] cents

    @property
    def never(self) -> bool:
        return self.months is None


def baseline(debts: list[Debt], day: dt.date) -> Baseline:
    per_debt: dict[int, int | None] = {}
    schedules: list[list[int]] = []
    total = 0
    for d in debts:
        p = project(max(0, d.balance), d.apr, d.minimum, day)
        per_debt[d.account_id] = p.months
        total += p.total_interest
        schedules.append([s["balance"] for s in p.schedule])
    months = None if any(m is None for m in per_debt.values()) else max(per_debt.values(), default=0)
    length = max((len(s) for s in schedules), default=0)
    current = month_of(day)
    timeline = [
        # A schedule that stopped early (paid off, or runaway) keeps its last balance.
        {"month": shift_month(current, k + 1), "total": sum((s[k] if k < len(s) else (s[-1] if s else 0)) for s in schedules)}
        for k in range(length)
    ]
    return Baseline(months, total, per_debt, timeline)


def minimums_only(debts: list[Debt], day: dt.date) -> tuple[int | None, int]:
    """Each debt pays only its own minimum (no extra, no rollover)."""
    b = baseline(debts, day)
    return b.months, b.total_interest


def _visible_liabilities(session: Session) -> list[Account]:
    return list(session.scalars(
        select(Account)
        .where(Account.hidden.is_(False), Account.category.in_(LIABILITY_CATEGORIES))
        .order_by(Account.id)
    ))


def _debt(a: Account) -> Debt:
    return Debt(
        account_id=a.id, name=a.name, group=loan_group(a), balance=max(0, int(a.current_balance_cents or 0)),
        apr=float(a.interest_rate or 0.0), minimum=int(a.minimum_payment_cents or 0), kind=debt_kind(a),
        apr_known=a.interest_rate is not None,
    )


def plan_debts(session: Session, account_ids: list[int] | None) -> list[Debt]:
    return plan_debts_skipped(session, account_ids)[0]


def plan_debts_skipped(session: Session, account_ids: list[int] | None) -> tuple[list[Debt], list[dict]]:
    """The debts in the plan, and (``account_ids`` omitted) the visible debts with a balance
    left out because they have no minimum payment (``skipped``)."""
    skipped: list[dict] = []
    if account_ids is None:
        accounts = []
        for a in _visible_liabilities(session):
            if (a.current_balance_cents or 0) <= 0:
                continue
            if a.minimum_payment_cents is None:
                skipped.append({"account_id": a.id, "name": a.name, "reason": "no_minimum"})
            else:
                accounts.append(a)
    else:
        wanted = list(dict.fromkeys(account_ids))
        found = {a.id: a for a in session.scalars(select(Account).where(Account.id.in_(wanted)))}
        accounts = []
        for account_id in wanted:
            account = found.get(account_id)
            if account is None:
                raise LoanError(f"Unknown account {account_id}.")
            if account.category not in LIABILITY_CATEGORIES:
                raise LoanError(f"{account.name} isn't a loan or credit card.")
            if account.minimum_payment_cents is None:
                raise LoanError(f"{account.name} has no minimum payment. Add one on the Accounts page first.")
            accounts.append(account)
    return [_debt(a) for a in accounts], skipped


def _order(strategy: str, debts: list[Debt], order: list[int] | None) -> list[Debt]:
    by_id = {d.account_id: d for d in debts}
    if strategy == "custom":
        if not order:
            raise LoanError("List the debts in the order you want to pay them off.")
        if len(order) != len(set(order)):
            raise LoanError("Each debt can appear only once in the order.")
        unknown = [i for i in order if i not in by_id]
        if unknown:
            raise LoanError(f"Account {unknown[0]} isn't one of the debts in this plan.")
        # Debts left out of the order are paid after the listed ones, highest APR first.
        return [by_id[i] for i in order] + [d for d in avalanche_order(debts) if d.account_id not in set(order)]
    if strategy == "snowball":
        return snowball_order(debts)
    return avalanche_order(debts)


def _timeline_out(timeline: list[dict]) -> list[dict]:
    return [
        {"month": p["month"], "total": from_cents(p["total"]), "interest": from_cents(p["interest"]),
         "by_account": {k: from_cents(v) for k, v in p["by_account"].items()}}
        for p in timeline
    ]


def debt_plan(
    session: Session, strategy: str, extra: int, order: list[int] | None, account_ids: list[int] | None,
    day: dt.date, lump: tuple[int, int | None] | None = None,
) -> dict:
    """``DebtPlan``. ``lump`` = (cents, account id or None): a one-time payment; the top-level
    numbers stay the plan without it and ``lump`` holds the plan with it (Release 3.10)."""
    debts, skipped = plan_debts_skipped(session, account_ids)
    chosen = _order(strategy, debts, order)

    result = simulate(chosen, extra, day)
    avalanche = result if strategy == "avalanche" else simulate(avalanche_order(debts), extra, day)
    snowball = result if strategy == "snowball" else simulate(snowball_order(debts), extra, day)
    base = baseline(debts, day)

    lump_out = None
    if lump is not None:
        cents, target = lump
        if target is not None and target not in {d.account_id for d in debts}:
            raise LoanError("Put the one-time payment on one of the debts you're looking at.")
        applied = apply_lump(chosen, cents, target)
        with_lump = simulate(chosen, extra, day, applied)
        lump_out = {
            "amount": from_cents(cents),
            "account_id": target,
            "applied": [{"account_id": k, "amount": from_cents(v)} for k, v in applied.items()],
            "paid_off": [d.account_id for d in chosen if d.balance > 0 and with_lump.payoff[d.account_id] == 0],
            "months": with_lump.months,
            "payoff_date": payoff_date(day, with_lump.months),
            "total_interest": from_cents(with_lump.total_interest),
            "never": with_lump.never,
        }

    return {
        "strategy": strategy,
        "extra": from_cents(extra),
        "months": result.months,
        "payoff_date": payoff_date(day, result.months),
        "total_interest": from_cents(result.total_interest),
        "never": result.never,
        # This month's interest at today's balances (an estimate), and the plan's first year.
        "interest_this_month": from_cents(sum(monthly_interest(d.balance, d.apr) for d in debts)),
        "interest_12": from_cents(result.interest_12),
        "debts": [
            {
                "account_id": d.account_id,
                "name": d.name,
                "kind": d.kind,
                "loan_group": d.group,
                "balance": from_cents(d.balance),
                "apr": d.apr,
                "apr_known": d.apr_known,
                "minimum": from_cents(d.minimum),
                "payoff_months": result.payoff[d.account_id],
                "payoff_date": payoff_date(day, result.payoff[d.account_id]),
                "interest": from_cents(result.interest[d.account_id]),
                "baseline_months": base.per_debt[d.account_id],
                "baseline_date": payoff_date(day, base.per_debt[d.account_id]),
            }
            for d in chosen
        ],
        "timeline": _timeline_out(result.timeline),
        "baseline": {
            "months": base.months,
            "payoff_date": payoff_date(day, base.months),
            "total_interest": from_cents(base.total_interest),
            "never": base.never,
            "timeline": [{"month": p["month"], "total": from_cents(p["total"])} for p in base.timeline],
        },
        "compare": {
            "avalanche": {"months": avalanche.months, "total_interest": from_cents(avalanche.total_interest)},
            "snowball": {"months": snowball.months, "total_interest": from_cents(snowball.total_interest)},
            "minimums_only": {"months": base.months, "total_interest": from_cents(base.total_interest)},
        },
        "lump": lump_out,
        "skipped": skipped,
    }


# ------------------------------------------------------------------ progress (Release 3.10)

PROGRESS_MONTHS = 12


def card_limits(session: Session) -> dict[int, int]:
    """account id -> the credit limit the bank shared (cents > 0), from
    ``app_settings.account_facts`` (``account_facts.load``). Missing or malformed entries mean
    "unknown"."""
    return {
        account_id: facts["limit_cents"]
        for account_id, facts in account_facts.load(session).items()
        if facts["limit_cents"] is not None and facts["limit_cents"] > 0
    }


def progress_accounts(session: Session, ids: list[int] | None) -> list[Account]:
    """The debts to look at: ``ids`` (each must be a loan or card), else every visible one."""
    if ids is None:
        return _visible_liabilities(session)
    wanted = list(dict.fromkeys(ids))
    found = {a.id: a for a in session.scalars(select(Account).where(Account.id.in_(wanted)))}
    out = []
    for account_id in wanted:
        account = found.get(account_id)
        if account is None:
            raise LoanError(f"Unknown account {account_id}.")
        if account.category not in LIABILITY_CATEGORIES:
            raise LoanError(f"{account.name} isn't a loan or credit card.")
        out.append(account)
    return out


def debt_progress(session: Session, ids: list[int] | None, day: dt.date) -> dict:
    """``GET /api/debt/progress``: what the debts added up to at the end of each of the last
    12 months (today's month: today's balances), honest months only.

    A month counts only when every included debt has a balance by its end: a snapshot (real
    or backfilled) on or before the month's last day. The value is the newest such snapshot;
    ``estimated`` when any value is a backfilled estimate or carried over from an earlier
    month (no snapshot in the month itself). ``missing``: debts whose history starts after
    the window's first month (why the chart starts later)."""
    accounts = progress_accounts(session, ids)
    current = month_of(day)
    window = [shift_month(current, k - (PROGRESS_MONTHS - 1)) for k in range(PROGRESS_MONTHS)]
    snaps: dict[int, list[tuple[dt.date, int, bool]]] = {a.id: [] for a in accounts}
    if accounts:
        for account_id, when, cents, est in session.execute(
            select(BalanceSnapshot.account_id, BalanceSnapshot.date, BalanceSnapshot.balance_cents,
                   BalanceSnapshot.estimated)
            .where(BalanceSnapshot.account_id.in_(list(snaps)), BalanceSnapshot.date <= day)
            .order_by(BalanceSnapshot.date, BalanceSnapshot.id)
        ):
            snaps[account_id].append((when, int(cents), bool(est)))

    def value(account: Account, month: str) -> tuple[int, bool] | None:
        if month == current:
            return max(0, int(account.current_balance_cents or 0)), False
        start, end = month_bounds(month)
        found = None
        for when, cents, est in snaps[account.id]:
            if when > end:
                break
            found = (when, cents, est)
        if found is None:
            return None
        when, cents, est = found
        return max(0, cents), est or when < start

    months: list[dict] = []
    totals: list[int] = []
    for month in window:
        values = [value(a, month) for a in accounts]
        if not accounts or any(v is None for v in values):
            continue
        totals.append(sum(v[0] for v in values))
        months.append({
            "month": month,
            "total": from_cents(totals[-1]),
            "estimated": any(v[1] for v in values),
        })
    since = months[0]["month"] if months else None
    # Negative = went down.
    change = from_cents(totals[-1] - totals[0]) if len(totals) >= 2 else None
    missing = []
    if since is not None and since != window[0]:
        missing = [a.name for a in accounts if value(a, window[0]) is None]
    limits = card_limits(session)
    cards = []
    for a in accounts:
        if a.category != "credit":
            continue
        balance = max(0, int(a.current_balance_cents or 0))
        limit = limits.get(a.id)
        cards.append({
            "account_id": a.id,
            "name": a.name,
            "balance": from_cents(balance),
            "limit": from_cents(limit) if limit is not None else None,
            "used_pct": round(balance * 100 / limit) if limit else None,
        })
    return {"months": months, "since": since, "change": change, "missing": missing, "cards": cards}

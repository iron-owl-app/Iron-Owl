from __future__ import annotations

import datetime as dt
from collections import defaultdict

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..models import Account, BalanceSnapshot, Transaction
from ..utils import CATEGORIES, LIABILITY_CATEGORIES, from_cents, today

BACKFILL_DAYS = 730
BACKFILL_CATEGORIES = ("bank", "credit")


def totals(session: Session) -> dict:
    """Today's net worth from non-hidden accounts (``/api/summary``); cents."""
    by_category = dict.fromkeys(CATEGORIES, 0)
    count = 0
    for account in session.scalars(select(Account).where(Account.hidden.is_(False))):
        category = account.category if account.category in by_category else "other"
        by_category[category] += account.current_balance_cents
        count += 1
    liabilities = sum(v for k, v in by_category.items() if k in LIABILITY_CATEGORIES)
    assets = sum(v for k, v in by_category.items() if k not in LIABILITY_CATEGORIES)
    return {"assets": assets, "liabilities": liabilities, "by_category": by_category, "count": count}


def _window_start(days: int) -> dt.date:
    return today() - dt.timedelta(days=days - 1)


def _net_worth_points(session: Session, start: dt.date | None, end: dt.date) -> list[dict]:
    """One point per day from ``start`` (or the earliest snapshot, if later) to ``end``.

    Each visible account contributes its last known balance on or before the day; a point
    is ``estimated`` when any contributing balance is an estimated snapshot.
    """
    liability = {
        a.id: a.category in LIABILITY_CATEGORIES
        for a in session.scalars(select(Account).where(Account.hidden.is_(False)))
    }
    if not liability:
        return []
    snaps = session.execute(
        select(BalanceSnapshot.account_id, BalanceSnapshot.date, BalanceSnapshot.balance_cents, BalanceSnapshot.estimated)
        .where(BalanceSnapshot.account_id.in_(liability.keys()), BalanceSnapshot.date <= end)
        .order_by(BalanceSnapshot.date, BalanceSnapshot.id)
    ).all()
    if not snaps:
        return []

    first = max(snaps[0].date, start) if start is not None else snaps[0].date
    balances: dict[int, int] = {}
    estimated: dict[int, bool] = {}
    totals = {True: 0, False: 0}  # keyed by is_liability
    points: list[dict] = []
    i = 0
    day = first
    while day <= end:
        while i < len(snaps) and snaps[i].date <= day:
            acct_id, _, cents, est = snaps[i]
            is_liab = liability[acct_id]
            totals[is_liab] += cents - balances.get(acct_id, 0)
            balances[acct_id] = cents
            estimated[acct_id] = bool(est)
            i += 1
        assets, liabilities = totals[False], totals[True]
        points.append(
            {
                "date": day.isoformat(),
                "assets": from_cents(assets),
                "liabilities": from_cents(liabilities),
                "net_worth": from_cents(assets - liabilities),
                "estimated": any(estimated.values()),
                "_net_cents": assets - liabilities,
            }
        )
        day += dt.timedelta(days=1)
    return points


def networth_history(session: Session, days: int) -> list[dict]:
    """One point per day from the earliest snapshot (clamped to `days`) to today."""
    points = _net_worth_points(session, _window_start(days), today())
    for p in points:
        del p["_net_cents"]
    return points


def net_worth_on(session: Session, start: dt.date, end: dt.date) -> tuple[int | None, int | None]:
    """Net worth (cents) at the first day of [start, end] that has data, and at ``end``."""
    if end < start:
        return None, None
    points = _net_worth_points(session, start, end)
    if not points:
        return None, None
    return points[0]["_net_cents"], points[-1]["_net_cents"]


def account_history(session: Session, account_id: int, days: int) -> list[dict]:
    """Snapshots within the window, plus the carried-in balance at the window start."""
    start = _window_start(days)
    end = today()
    rows = session.execute(
        select(BalanceSnapshot.date, BalanceSnapshot.balance_cents, BalanceSnapshot.estimated)
        .where(BalanceSnapshot.account_id == account_id, BalanceSnapshot.date <= end)
        .where(BalanceSnapshot.date >= start)
        .order_by(BalanceSnapshot.date)
    ).all()
    points = [{"date": d.isoformat(), "balance": from_cents(c), "estimated": bool(e)} for d, c, e in rows]
    if not rows or rows[0].date > start:
        prior = session.execute(
            select(BalanceSnapshot.balance_cents, BalanceSnapshot.estimated)
            .where(BalanceSnapshot.account_id == account_id, BalanceSnapshot.date < start)
            .order_by(BalanceSnapshot.date.desc())
            .limit(1)
        ).first()
        if prior is not None:
            points.insert(0, {"date": start.isoformat(), "balance": from_cents(prior[0]), "estimated": bool(prior[1])})
    return points


# ------------------------------------------------------------------ backfill


def backfill_account(session: Session, account: Account, day: dt.date) -> int:
    """Estimate end-of-day balances backwards (SPEC R3 section 7).

    The walk starts from the account's earliest real snapshot when it has one (so the
    estimates join it without a jump), otherwise from the current balance.

    bank:   balance(d-1) = balance(d) - Σ amounts dated d
    credit: owed(d-1)    = owed(d)    + Σ amounts dated d   (a purchase is negative)

    Pending transactions are ignored. Estimates cover at most BACKFILL_DAYS, never go before
    the earliest posted transaction, and are written only for dates before the account's
    earliest real snapshot, so a recorded balance is never overwritten. Returns the number
    of estimated snapshots now stored.
    """
    posted = (Transaction.account_id == account.id, Transaction.pending.is_(False))
    first_txn = session.scalar(select(func.min(Transaction.date)).where(*posted))
    first_real = session.scalar(
        select(func.min(BalanceSnapshot.date)).where(
            BalanceSnapshot.account_id == account.id, BalanceSnapshot.estimated.is_(False)
        )
    )
    lo = hi = None
    if first_txn is not None:
        lo = max(first_txn, day - dt.timedelta(days=BACKFILL_DAYS))
        hi = min(first_real, day + dt.timedelta(days=1)) if first_real is not None else day + dt.timedelta(days=1)
        hi -= dt.timedelta(days=1)
    if lo is None or hi is None or lo > hi:
        # Nothing to estimate: drop stale estimates (e.g. its transactions were removed).
        session.execute(delete(BalanceSnapshot).where(
            BalanceSnapshot.account_id == account.id, BalanceSnapshot.estimated.is_(True)
        ))
        return 0

    # Walk back from the earliest recorded balance when there is one: the estimates then meet
    # it exactly, instead of carrying every gap between today and that snapshot into the seam.
    anchor = day
    balance = int(account.current_balance_cents or 0)
    if first_real is not None and first_real <= day:
        anchor = first_real
        balance = int(session.scalar(
            select(BalanceSnapshot.balance_cents).where(
                BalanceSnapshot.account_id == account.id, BalanceSnapshot.date == first_real,
                BalanceSnapshot.estimated.is_(False),
            ).limit(1)
        ) or 0)
    by_date: dict[dt.date, int] = defaultdict(int)
    for when, total in session.execute(
        select(Transaction.date, func.sum(Transaction.amount_cents))
        .where(*posted, Transaction.date > lo, Transaction.date <= anchor)
        .group_by(Transaction.date)
    ):
        by_date[when] += int(total or 0)
    sign = -1 if account.category in LIABILITY_CATEGORIES else 1  # owed moves opposite to cash
    wanted: dict[dt.date, int] = {}
    d = anchor
    while d > lo:
        balance -= sign * by_date.get(d, 0)  # balance at the end of d - 1
        d -= dt.timedelta(days=1)
        if d <= hi:
            wanted[d] = balance

    session.execute(delete(BalanceSnapshot).where(
        BalanceSnapshot.account_id == account.id,
        BalanceSnapshot.estimated.is_(True),
        (BalanceSnapshot.date < lo) | (BalanceSnapshot.date > hi),
    ))
    existing = {
        s.date: s
        for s in session.scalars(select(BalanceSnapshot).where(
            BalanceSnapshot.account_id == account.id, BalanceSnapshot.date >= lo, BalanceSnapshot.date <= hi
        ))
    }
    for when, cents in wanted.items():
        snap = existing.get(when)
        if snap is None:
            session.add(BalanceSnapshot(account_id=account.id, date=when, balance_cents=cents, estimated=True))
        elif snap.estimated:
            snap.balance_cents = cents
    session.flush()
    return len(wanted)


def backfill(session: Session, day: dt.date | None = None, account_ids: list[int] | None = None) -> None:
    """Backfill every non-hidden bank or credit account that has posted transactions."""
    day = day or today()
    query = select(Account).where(Account.hidden.is_(False), Account.category.in_(BACKFILL_CATEGORIES))
    if account_ids is not None:
        query = query.where(Account.id.in_(account_ids))
    for account in session.scalars(query.order_by(Account.id)).all():
        backfill_account(session, account, day)

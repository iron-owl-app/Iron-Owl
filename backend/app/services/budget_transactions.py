"""Read-only category activity using the envelope budget's exact ledger selection."""
from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..models import Account, Transaction
from ..seeds import OTHER
from ..utils import from_cents, month_bounds
from . import ledger, spending
from .categories import load as load_categories


def category_transactions(
    session: Session, month: str, category: str, *, limit: int = 50, offset: int = 0,
) -> dict:
    """Each parent appears once, with only its matching split parts in ``amount``.

    Pending purchases count; refunds remain positive. The full category net is floored
    at zero, exactly as ``_envelope_net`` / ``_carryover`` do for EnvelopeLine.spent.
    Pagination never changes that full-month total.
    """
    cmap = load_categories(session)
    if category not in cmap.by_id or cmap.kind(category) != "spending":
        raise spending.BudgetError("Choose a spending category that still exists.")
    account_ids = [account.id for account, included in spending.budget_accounts(session) if included]
    if not account_ids:
        return {"items": [], "total": 0, "next_offset": None, "spent": 0.0}
    first, last = month_bounds(month)
    lines = ledger.lines(
        Transaction.account_id.in_(account_ids),
        Transaction.is_transfer.is_(False),
        Transaction.date >= first,
        Transaction.date <= last,
    )
    match = lines.c.category == category
    if category == OTHER:
        # CategoryMap.get also resolves missing/removed categories and NULL to Other.
        match = or_(match, lines.c.category.is_(None), lines.c.category.not_in(list(cmap.by_id)))
    grouped = (
        select(
            lines.c.txn_id.label("id"), lines.c.date, lines.c.name, lines.c.merchant_name,
            lines.c.pending, Account.name.label("account_name"),
            func.sum(lines.c.amount_cents).label("amount_cents"),
            func.count(lines.c.split_id).label("split_parts"),
        )
        .join(Account, Account.id == lines.c.account_id)
        .where(match)
        .group_by(lines.c.txn_id, lines.c.date, lines.c.name, lines.c.merchant_name, lines.c.pending, Account.name)
        .subquery("budget_category_activity")
    )
    total, net = session.execute(select(func.count(), func.coalesce(func.sum(grouped.c.amount_cents), 0))).one()
    rows = session.execute(
        select(grouped).order_by(grouped.c.date.desc(), grouped.c.id.desc()).limit(limit).offset(offset)
    ).all()
    return {
        "items": [
            {
                "id": row.id, "date": row.date.isoformat(), "name": row.merchant_name or row.name,
                "account_name": row.account_name, "amount": from_cents(int(row.amount_cents)),
                "pending": bool(row.pending), "split": row.split_parts > 0,
            }
            for row in rows
        ],
        "total": int(total),
        "next_offset": offset + len(rows) if offset + len(rows) < total else None,
        "spent": from_cents(max(0, -int(net))),
    }

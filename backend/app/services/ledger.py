"""Split-aware ledger lines (SPEC Release 3, "Spending definition update").

A transaction without splits is one line (its own amount and category). A transaction with
splits is replaced by its parts: each part keeps the parent's account, date, name, merchant,
pending and transfer flags, but has its own amount and category. Every total (envelopes,
review, reports, tags, alerts, forecast) reads these lines instead of ``transactions``.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import exists, null, select, union_all
from sqlalchemy.sql import Subquery

from ..models import Transaction, TransactionSplit


def lines(*where: Any) -> Subquery:
    """A subquery of ledger lines; ``where`` filters apply to the parent transaction.

    Columns: txn_id, split_id (NULL for an unsplit transaction), account_id, date,
    amount_cents, category, name, merchant_name, pending, is_transfer.
    """
    has_splits = exists().where(TransactionSplit.transaction_id == Transaction.id)
    whole = select(
        Transaction.id.label("txn_id"),
        null().label("split_id"),
        Transaction.account_id.label("account_id"),
        Transaction.date.label("date"),
        Transaction.amount_cents.label("amount_cents"),
        Transaction.category.label("category"),
        Transaction.name.label("name"),
        Transaction.merchant_name.label("merchant_name"),
        Transaction.pending.label("pending"),
        Transaction.is_transfer.label("is_transfer"),
    ).where(~has_splits, *where)
    parts = (
        select(
            Transaction.id,
            TransactionSplit.id,
            Transaction.account_id,
            Transaction.date,
            TransactionSplit.amount_cents,
            TransactionSplit.category,
            Transaction.name,
            Transaction.merchant_name,
            Transaction.pending,
            Transaction.is_transfer,
        )
        .join(Transaction, Transaction.id == TransactionSplit.transaction_id)
        .where(*where)
    )
    return union_all(whole, parts).subquery("ledger_lines")


def split_parent_ids(session: Any) -> set[int]:
    """Ids of every transaction that currently has splits."""
    return set(session.scalars(select(TransactionSplit.transaction_id).distinct()))

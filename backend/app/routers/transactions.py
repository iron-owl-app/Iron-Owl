from __future__ import annotations

import csv
import datetime as dt
import io
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.responses import Response
from sqlalchemy import and_, case, exists, func, or_, select
from sqlalchemy.orm import Session

from ..deps import get_db
from ..models import Account, Transaction, TransactionSplit, TransactionTag, TxnCategory
from ..schemas import (
    BulkCategoryBody,
    RecategorizeApply,
    RecategorizeBody,
    RestoreBody,
    SplitsBody,
    TransactionPatch,
    TxnTagsBody,
    TxnView,
    non_null,
)
from ..seeds import OTHER
from ..services import debt_budget
from ..services import rules as rules_service
from ..services import txns
from ..services.categories import load as load_categories
from ..utils import cents_to_str, from_cents, today

router = APIRouter(prefix="/api", tags=["transactions"])

MAX_LIMIT = 500
CSV_COLUMNS = ("date", "account", "name", "merchant", "category", "amount", "pending", "transfer", "notes", "tags")
# Spreadsheet apps treat cells starting with these as formulas (CSV injection): the
# OWASP set, "|" and "%" (DDE / some importers), and the fullwidth forms that some
# locales normalize to their ASCII equivalents.
FORMULA_CHARS = frozenset("=+-@|%＝＋－＠")
# A leading control character is prefixed as-is (tab and CR per OWASP, LF likewise).
CONTROL_PREFIXES = ("\t", "\r", "\n")
MAX_ID = 2**63 - 1
MAX_IDS = 1000  # GET /transactions/ids
# Keyset cursor "YYYY-MM-DD.<id>": the last row of the previous page (date DESC, id DESC).
CURSOR_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}\.[0-9]{1,19}$"


@dataclass(frozen=True)
class TxnFilters:
    account_id: int | None
    search: str | None
    start: dt.date | None
    end: dt.date | None
    category: str | None
    tag: int | None
    view: str


def txn_filters(
    account_id: int | None = Query(default=None, ge=-MAX_ID, le=MAX_ID),
    search: str | None = Query(default=None, max_length=200),
    start: dt.date | None = None,
    end: dt.date | None = None,
    category: str | None = Query(default=None, max_length=64),
    tag: int | None = Query(default=None, ge=1, le=MAX_ID),
    view: TxnView = "all",
) -> TxnFilters:
    """The filters shared by the list, summary, ids and CSV export."""
    return TxnFilters(account_id, search, start, end, category, tag, view)


def _category_match(column, category: str):  # noqa: ANN001, ANN202
    if category == OTHER:
        # NULL (and ids that no longer exist) count as Other.
        known = select(TxnCategory.id).where(TxnCategory.id != OTHER)
        return or_(column == OTHER, column.is_(None), column.not_in(known))
    return column == category


def _view_clause(view: str):  # noqa: ANN202
    if view == "needs_category":
        return txns.needs_category_clause()
    if view == "in":
        return Transaction.amount_cents > 0
    if view == "out":
        return Transaction.amount_cents < 0
    return None  # all ($0 rows are in neither in nor out)


def _filters(f: TxnFilters, *, with_view: bool = True) -> list:
    filters = _base_filters(f.account_id, f.search, f.start, f.end, f.category, f.tag)
    if with_view and (clause := _view_clause(f.view)) is not None:
        filters.append(clause)
    return filters


def _base_filters(
    account_id: int | None,
    search: str | None,
    start: dt.date | None,
    end: dt.date | None,
    category: str | None,
    tag: int | None = None,
) -> list:
    filters = []
    if account_id is not None:
        filters.append(Transaction.account_id == account_id)
    if start is not None:
        filters.append(Transaction.date >= start)
    if end is not None:
        filters.append(Transaction.date <= end)
    if category:
        # Split-aware: a split transaction matches when one of its parts is in the category
        # (its own category no longer counts).
        has_splits = exists().where(TransactionSplit.transaction_id == Transaction.id)
        part_matches = exists().where(
            TransactionSplit.transaction_id == Transaction.id,
            _category_match(TransactionSplit.category, category),
        )
        filters.append(or_(and_(~has_splits, _category_match(Transaction.category, category)), part_matches))
    if tag is not None:
        filters.append(
            exists().where(TransactionTag.transaction_id == Transaction.id, TransactionTag.tag_id == tag)
        )
    if search and search.strip():
        needle = search.strip().lower()
        filters.append(
            or_(
                func.lower(Transaction.name).contains(needle, autoescape=True),
                func.lower(func.coalesce(Transaction.merchant_name, "")).contains(needle, autoescape=True),
                func.lower(func.coalesce(Transaction.notes, "")).contains(needle, autoescape=True),
            )
        )
    return filters


def _after_cursor(cursor: str):  # noqa: ANN202
    """Rows strictly after the cursor in (date DESC, id DESC); the parts are bound, never formatted."""
    day_text, _, id_text = cursor.partition(".")
    try:
        day = dt.date.fromisoformat(day_text)
    except ValueError:
        raise HTTPException(status_code=422, detail="invalid cursor") from None
    txn_id = int(id_text)
    if txn_id > MAX_ID:
        raise HTTPException(status_code=422, detail="invalid cursor")
    return or_(Transaction.date < day, and_(Transaction.date == day, Transaction.id < txn_id))


def _money_columns():  # noqa: ANN202
    """(money in, money out) sums in cents; money out is signed (<= 0)."""
    cents = Transaction.amount_cents
    return (
        func.coalesce(func.sum(case((cents > 0, cents), else_=0)), 0),
        func.coalesce(func.sum(case((cents < 0, cents), else_=0)), 0),
    )


def _day_totals(db: Session, filters: list, days: list[dt.date]) -> dict[str, dict]:
    """Whole-day money in/out for these dates over the full filter set, transfers excluded."""
    out = {d.isoformat(): {"money_in": 0.0, "money_out": 0.0} for d in days}
    if not days:
        return out
    money_in, money_out = _money_columns()
    rows = db.execute(
        select(Transaction.date, money_in, money_out)
        .where(*filters, Transaction.is_transfer.is_(False), Transaction.date.in_(days))
        .group_by(Transaction.date)
    ).all()
    for day, cents_in, cents_out in rows:
        out[day.isoformat()] = {"money_in": from_cents(int(cents_in)), "money_out": from_cents(int(cents_out))}
    return out


@router.get("/transactions")
def list_transactions(
    f: TxnFilters = Depends(txn_filters),
    limit: int = Query(default=50, ge=1),
    offset: int = Query(default=0, ge=0),
    cursor: str | None = Query(default=None, max_length=40, pattern=CURSOR_PATTERN),
    db: Session = Depends(get_db),
) -> dict:
    limit = min(limit, MAX_LIMIT)
    if cursor is not None and offset > 0:
        raise HTTPException(status_code=422, detail="Use either cursor or offset, not both.")
    filters = _filters(f)
    total = db.scalar(select(func.count()).select_from(Transaction).where(*filters)) or 0
    query = (
        select(Transaction, Account.name)
        .join(Account, Account.id == Transaction.account_id)
        .where(*filters)
    )
    if cursor is not None:
        query = query.where(_after_cursor(cursor))
    rows = db.execute(
        query.order_by(Transaction.date.desc(), Transaction.id.desc()).limit(limit + 1).offset(offset)
    ).all()
    more = len(rows) > limit
    rows = rows[:limit]
    last = rows[-1][0] if rows else None
    days = list(dict.fromkeys(t.date for t, _ in rows))
    return {
        "items": txns.out_many(db, [(t, name) for t, name in rows]),
        "total": total,
        "next_cursor": f"{last.date.isoformat()}.{last.id}" if more and last is not None else None,
        "days": _day_totals(db, filters, days),
    }


@router.get("/transactions/summary")
def transactions_summary(f: TxnFilters = Depends(txn_filters), db: Session = Depends(get_db)) -> dict:
    filters = _filters(f)
    total, first_date = db.execute(
        select(func.count(), func.min(Transaction.date)).select_from(Transaction).where(*filters)
    ).one()
    money_in, money_out = db.execute(
        select(*_money_columns()).select_from(Transaction).where(*filters, Transaction.is_transfer.is_(False))
    ).one()
    needs = db.scalar(
        select(func.count()).select_from(Transaction).where(*filters, txns.needs_category_clause())
    )
    # The pills' counts: the same filters without the view.
    counts = db.execute(
        select(
            func.count(),
            func.coalesce(func.sum(case((txns.needs_category_clause(), 1), else_=0)), 0),
            func.coalesce(func.sum(case((Transaction.amount_cents > 0, 1), else_=0)), 0),
            func.coalesce(func.sum(case((Transaction.amount_cents < 0, 1), else_=0)), 0),
        )
        .select_from(Transaction)
        .where(*_filters(f, with_view=False))
    ).one()
    return {
        "total": int(total or 0),
        "first_date": first_date.isoformat() if first_date else None,
        "money_in": from_cents(int(money_in)),
        "money_out": from_cents(int(money_out)),
        "needs_category": int(needs or 0),
        "counts": {
            "all": int(counts[0] or 0),
            "needs_category": int(counts[1]),
            "in": int(counts[2]),
            "out": int(counts[3]),
        },
    }


@router.get("/transactions/ids")
def transaction_ids(
    f: TxnFilters = Depends(txn_filters),
    needs_category: bool = False,
    db: Session = Depends(get_db),
) -> dict:
    """Ids in list order (at most 1000), e.g. to tick every transaction needing a category."""
    filters = _filters(f)
    if needs_category:
        filters.append(txns.needs_category_clause())
    total = db.scalar(select(func.count()).select_from(Transaction).where(*filters)) or 0
    ids = list(db.scalars(
        select(Transaction.id)
        .where(*filters)
        .order_by(Transaction.date.desc(), Transaction.id.desc())
        .limit(MAX_IDS)
    ))
    return {"ids": ids, "total": int(total), "truncated": int(total) > len(ids)}


def _csv_text(value: str | None) -> str:
    """Neutralize formula-looking text cells with a leading apostrophe.

    Leading whitespace doesn't make a cell safe: spreadsheet importers may trim it
    (" =cmd|..." evaluates once trimmed), so the first non-whitespace character counts.
    """
    text = value or ""
    if text.startswith(CONTROL_PREFIXES) or text.lstrip()[:1] in FORMULA_CHARS:
        return "'" + text
    return text


@router.get("/transactions/export.csv")
def export_csv(f: TxnFilters = Depends(txn_filters), db: Session = Depends(get_db)) -> Response:
    filters = _filters(f)
    category = f.category
    rows = db.execute(
        select(Transaction, Account.name)
        .join(Account, Account.id == Transaction.account_id)
        .where(*filters)
        .order_by(Transaction.date.desc(), Transaction.id.desc())
    ).all()
    cmap = load_categories(db)
    splits, tags = txns.extras(db, [t.id for t, _ in rows])
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    for t, account_name in rows:
        tag_names = "; ".join(tag.name for tag in tags.get(t.id, ()))
        parts = splits.get(t.id) or []
        # One row per split part: the parts replace the transaction in every total. With a
        # category filter, only the parts in that category (the others are other categories'
        # money, and summing the export must match the category's total).
        numbered = [
            (k, p) for k, p in enumerate(parts, 1)
            if not category or p.category == category
            or (category == OTHER and cmap.effective_id(p.category) == OTHER)
        ]
        lines = (
            [(p.amount_cents, p.category, f"split {k}/{len(parts)}" + (f": {p.notes}" if p.notes else ""))
             for k, p in numbered]
            if parts else [(t.amount_cents, t.category, t.notes)]
        )
        for cents, line_category, notes in lines:
            writer.writerow([
                t.date.isoformat(),
                _csv_text(account_name),
                _csv_text(t.name),
                _csv_text(t.merchant_name),
                _csv_text(cmap.get(line_category).name),
                cents_to_str(cents),  # numeric: written as a plain number, never prefixed
                "true" if t.pending else "false",
                "true" if t.is_transfer else "false",
                _csv_text(notes),
                _csv_text(tag_names),
            ])
    filename = f"iron-owl-transactions-{today().isoformat()}.csv"
    return Response(
        content=buf.getvalue().encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _get_txn(db: Session, transaction_id: int) -> Transaction:
    txn = db.get(Transaction, transaction_id)
    if txn is None:
        raise HTTPException(status_code=404, detail="transaction not found")
    return txn


def _unprocessable(db: Session, exc: txns.TxnError) -> HTTPException:
    db.rollback()
    return HTTPException(status_code=422, detail=str(exc))


def _not_found(db: Session) -> HTTPException:
    db.rollback()
    return HTTPException(status_code=404, detail="transaction not found")


# Fixed paths, declared before the /transactions/{id} routes.
@router.patch("/transactions")
def bulk_set_category(body: BulkCategoryBody, db: Session = Depends(get_db)) -> dict:
    """Set (or reset to automatic) the category of up to 1000 transactions, all or nothing.
    Release 3.10: credit card payments never go in "Paying off debt": they are skipped
    (``skipped`` and ``skipped_card_payments``), the rest change."""
    ids, cards = body.ids, []
    if debt_budget.filing_blocked(db, body.category):
        blocked = debt_budget.card_payment_ids(db, body.ids)
        cards = [i for i in dict.fromkeys(body.ids) if i in blocked]
        ids = [i for i in body.ids if i not in blocked]
    try:
        changed, previous, skipped = txns.bulk_set_category(db, ids, body.category)
    except txns.TxnError as exc:
        raise _unprocessable(db, exc) from None
    except txns.TxnNotFound:
        raise _not_found(db) from None
    debt_budget.forget_released(db, changed)  # their new choice: an older cleared mark stays gone
    debt_budget.release_filed(db)  # Release 3.10: filed under "Paying off debt" counts
    db.commit()
    out = {"items": txns.out_ids(db, changed), "previous": previous, "skipped": skipped + cards}
    if debt_budget.filing_blocked(db, body.category):
        out["skipped_card_payments"] = cards
    return out


@router.post("/transactions/restore")
def restore_categories(body: RestoreBody, db: Session = Depends(get_db)) -> dict:
    """Undo: put categories and their sources back, all or nothing."""
    items = [(i.id, i.category, i.category_source) for i in body.items]
    try:
        restored = txns.restore_categories(db, items)
    except txns.TxnError as exc:
        raise _unprocessable(db, exc) from None
    except txns.TxnNotFound:
        raise _not_found(db) from None
    debt_budget.release_filed(db)  # Release 3.10: restored under "Paying off debt" counts
    db.commit()
    return {"items": txns.out_ids(db, restored)}


# Declared before the /transactions/{id} routes; the methods differ anyway (POST vs PATCH/PUT).
@router.post("/transactions/recategorize/preview")
def recategorize_preview(body: RecategorizeBody, db: Session = Depends(get_db)) -> dict:
    try:
        return txns.recategorize_preview(db, body.field, body.text, body.category)
    except txns.TxnError as exc:
        raise _unprocessable(db, exc) from None


@router.post("/transactions/recategorize")
def recategorize(body: RecategorizeApply, db: Session = Depends(get_db)) -> dict:
    blocked = debt_budget.filing_blocked(db, body.category)
    cards: set[int] = set()

    def skip(ids: list[int]) -> set[int]:
        # Release 3.10: credit card payments never go in "Paying off debt".
        cards.update(debt_budget.card_payment_ids(db, ids))
        return cards

    try:
        changed = txns.recategorize(
            db, body.field, body.text, body.category, body.include_manual, skip=skip if blocked else None,
        )
    except txns.TxnError as exc:
        raise _unprocessable(db, exc) from None
    debt_budget.release_filed(db)  # Release 3.10: filed under "Paying off debt" counts
    db.commit()
    return {"changed": changed, "skipped": len(cards)} if blocked else {"changed": changed}


@router.patch("/transactions/{transaction_id}")
def update_transaction(transaction_id: int, body: TransactionPatch, db: Session = Depends(get_db)) -> dict:
    txn = _get_txn(db, transaction_id)
    if non_null(body, ("is_transfer",)):
        raise HTTPException(status_code=422, detail="is_transfer must not be null")
    fields = body.model_fields_set
    if "category" in fields:
        _check_debt_filing(db, txn.id, [body.category])
    reapply = False
    if "category" in fields:
        if body.category is None:
            # Back to automatic: rules, then Plaid.
            txn.category_source = "plaid"
            txn.rule_id = None
            txn.category = txn.plaid_category
            reapply = True
        else:
            if db.get(TxnCategory, body.category) is None:
                raise HTTPException(status_code=422, detail="unknown category")
            txn.category = body.category
            txn.category_source = "user"
            txn.rule_id = None
    if "notes" in fields:
        notes = (body.notes or "").strip()
        txn.notes = notes or None
    if "is_transfer" in fields:
        txn.is_transfer = bool(body.is_transfer)
        txn.transfer_source = "user"
    if "category" in fields or "is_transfer" in fields:
        debt_budget.forget_released(db, [txn.id])  # their new choice: an older cleared mark stays gone
    db.flush()
    if reapply:
        rules_service.apply_all(db)
    elif "category" in fields:
        # Release 3.9.1: a category set by hand wins over the automatic card payment mark.
        rules_service.release_hand_set(db)
        if "is_transfer" not in fields:
            # Release 3.10: a payment the user files under "Paying off debt" isn't a transfer any more.
            debt_budget.release_filed(db, txn.id)
    db.commit()
    return txns.out_one(db, transaction_id)


def _check_debt_filing(db: Session, txn_id: int, categories: list[str | None]) -> None:
    """Release 3.10: a credit card payment can't go in "Paying off debt" (422, plain words)."""
    for category in dict.fromkeys(categories):
        try:
            debt_budget.check_filing(db, txn_id, category)
        except debt_budget.DebtBudgetError as exc:
            raise HTTPException(status_code=422, detail=exc.detail) from None


@router.put("/transactions/{transaction_id}/splits")
def set_splits(transaction_id: int, body: SplitsBody, db: Session = Depends(get_db)) -> dict:
    txn = _get_txn(db, transaction_id)
    _check_debt_filing(db, txn.id, [p.category for p in body.splits])
    try:
        txns.set_splits(db, txn, [(p.amount, p.category, p.notes) for p in body.splits])
    except txns.TxnError as exc:
        raise _unprocessable(db, exc) from None
    debt_budget.forget_released(db, [txn.id])
    if not body.splits:
        # Rules skipped it while it was split; now it's a whole transaction again.
        rules_service.apply_all(db)
    else:
        debt_budget.release_filed(db, txn.id)  # Release 3.10: a part filed under "Paying off debt"
    db.commit()
    return txns.out_one(db, transaction_id)


@router.put("/transactions/{transaction_id}/tags")
def set_tags(transaction_id: int, body: TxnTagsBody, db: Session = Depends(get_db)) -> dict:
    txn = _get_txn(db, transaction_id)
    try:
        txns.set_tags(db, txn, body.tag_ids)
    except txns.TxnError as exc:
        raise _unprocessable(db, exc) from None
    db.commit()
    return txns.out_one(db, transaction_id)


@router.get("/transactions/{transaction_id}/related")
def related_transactions(transaction_id: int = Path(ge=1, le=MAX_ID), db: Session = Depends(get_db)) -> dict:
    """How many transactions (and how much) from the same merchant, for the details panel."""
    return txns.related(db, _get_txn(db, transaction_id))

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..deps import get_db
from ..models import Account
from ..schemas import AccountBalanceBody, AccountCreate310, AccountPatch, AccountUndoBody
from ..services import accounts as service
from ..services.alerts import safe_evaluate
from ..services import rules as rules_service
from ..services.networth import account_history
from ..services.sync import upsert_snapshot
from ..utils import LIABILITY_CATEGORIES, to_cents, to_cents_opt, today, utcnow

router = APIRouter(prefix="/api/accounts", tags=["accounts"])

NON_NULLABLE_PATCH_FIELDS = ("name", "category", "hidden", "current_balance")


def _get_account(db: Session, account_id: int) -> Account:
    account = db.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")
    return account


def _clean(text: str | None) -> str | None:
    if text is None:
        return None
    text = text.strip()
    return text or None


def _refused(db: Session, exc: service.AccountError) -> JSONResponse:
    """Nothing the request did is kept (an Undo token it used is back, too)."""
    db.rollback()
    return JSONResponse(status_code=exc.status, content={"detail": exc.detail})


@router.get("")
def list_accounts(db: Session = Depends(get_db)) -> list[dict]:
    """Every account plus (Release 3.10) ``bank_name``, ``connection``, ``balance_date``,
    ``balance_age_days``, ``stale`` and ``credit_limit``."""
    return service.list_out(db, today())


@router.post("", status_code=201)
def create_account(body: AccountCreate310, db: Session = Depends(get_db)):
    day = today()
    try:
        account = service.create(db, body, day)
    except service.AccountError as exc:
        return _refused(db, exc)
    db.commit()
    safe_evaluate(db, day)
    return service.one_out(db, account, day)


@router.put("/{account_id}/balance")
def set_balance(account_id: int, body: AccountBalanceBody, db: Session = Depends(get_db)):
    """A manual account's balance, typed in (debts: the amount owed). Returns the Undo token."""
    day = today()
    try:
        account, token = service.set_balance(db, account_id, body.balance, day)
    except service.AccountError as exc:
        return _refused(db, exc)
    db.commit()
    safe_evaluate(db, day)
    return {"account": service.one_out(db, account, day), "undo": {"token": token}}


@router.post("/balance/undo")
def undo_balance(body: AccountUndoBody, db: Session = Depends(get_db)):
    day = today()
    try:
        account = service.undo_balance(db, body.token, day)
    except service.AccountError as exc:
        return _refused(db, exc)
    db.commit()
    safe_evaluate(db, day)
    return service.one_out(db, account, day)


@router.post("/restore", status_code=201)
def restore_account(body: AccountUndoBody, db: Session = Depends(get_db)):
    """Undo ``DELETE /api/accounts/{id}`` (today, once)."""
    day = today()
    try:
        account = service.restore(db, body.token, day)
    except service.AccountError as exc:
        return _refused(db, exc)
    db.commit()
    safe_evaluate(db, day)
    return service.one_out(db, account, day)


@router.patch("/{account_id}")
def update_account(account_id: int, body: AccountPatch, db: Session = Depends(get_db)) -> dict:
    account = _get_account(db, account_id)
    fields = body.model_fields_set
    for name in NON_NULLABLE_PATCH_FIELDS:
        if name in fields and getattr(body, name) is None:
            raise HTTPException(status_code=422, detail=f"{name} must not be null")
    if "current_balance" in fields and account.source != "manual":
        raise HTTPException(
            status_code=400, detail="current_balance can only be set on manual accounts"
        )

    if "name" in fields:
        name = _clean(body.name)
        if not name:
            raise HTTPException(status_code=422, detail="name must not be empty")
        account.name = name
    if "category" in fields:
        account.category = body.category
    if "hidden" in fields:
        account.hidden = bool(body.hidden)
    if "notes" in fields:
        account.notes = body.notes
    if "interest_rate" in fields:
        account.interest_rate = body.interest_rate
    if "minimum_payment" in fields:
        account.minimum_payment_cents = to_cents_opt(body.minimum_payment)
    if "next_payment_due" in fields:
        account.next_payment_due = body.next_payment_due
    if "current_balance" in fields:
        account.current_balance_cents = to_cents(body.current_balance)
    if "current_balance" in fields or "category" in fields:
        # After the category is set: a manual loan or card keeps the positive amount owed
        # (so net worth, card_due and the debt pages count it as owed), history too.
        service.normalize_debt(db, account)
    if "current_balance" in fields:
        upsert_snapshot(db, account.id, account.current_balance_cents)
    if "loan_group" in fields:
        # After a category change in the same request, so the check sees the new category.
        if account.category not in LIABILITY_CATEGORIES:
            db.rollback()
            raise HTTPException(status_code=422, detail="Only loans and credit cards have a loan group.")
        if body.loan_group is None:
            account.loan_group = None  # back to the automatic group
        else:
            label = " ".join(body.loan_group.split())
            if not label:
                db.rollback()
                raise HTTPException(status_code=422, detail="loan_group must not be empty")
            account.loan_group = label
    account.updated_at = utcnow()
    if "hidden" in fields or "category" in fields:
        # Release 3.9.1: a payment from checking is a card payment only while it pairs with
        # a payment on one of their visible credit cards (services/card_payments.py).
        db.flush()
        rules_service.apply_all(db)
    db.commit()
    if "current_balance" in fields:
        safe_evaluate(db, today())
    return service.one_out(db, account, today())


@router.delete("/{account_id}")
def delete_account(account_id: int, db: Session = Depends(get_db)):
    """Manual accounts only (a bank-connected one can only be hidden here). Returns
    ``{"undo": {"token", "name"}}`` for ``POST /api/accounts/restore``."""
    account = _get_account(db, account_id)
    if account.source != "manual":
        raise HTTPException(
            status_code=400, detail="linked accounts are removed by removing their Plaid item"
        )
    try:
        undo = service.delete(db, account_id, today())
    except service.AccountError as exc:
        return _refused(db, exc)
    db.commit()
    return {"undo": undo}


@router.get("/{account_id}/history")
def history(
    account_id: int,
    days: int = Query(default=365, ge=1, le=36500),
    db: Session = Depends(get_db),
) -> list[dict]:
    _get_account(db, account_id)
    return account_history(db, account_id, days)

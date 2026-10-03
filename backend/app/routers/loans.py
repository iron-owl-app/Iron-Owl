from __future__ import annotations

import math

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..deps import freeze_first, get_db
from ..schemas import DebtBudgetBody, DebtPlanBody, DebtUndoBody
from ..services import debt_budget, loans
from ..services.networth import account_history
from ..utils import to_cents, today

router = APIRouter(prefix="/api", tags=["loans"])

MAX_MONEY = 1_000_000_000_000
HISTORY_DAYS = 730


def _cents(name: str, value: float | None) -> int | None:
    if value is None:
        return None
    if not math.isfinite(value) or value < 0 or value > MAX_MONEY:
        raise HTTPException(status_code=422, detail=f"{name} must be a number from 0 to {MAX_MONEY:,}")
    return to_cents(value)


@router.get("/loans/{account_id}/payoff")
def payoff(
    account_id: int,
    extra: float = Query(default=0.0, ge=0, le=MAX_MONEY),
    minimum: float | None = Query(default=None, ge=0, le=MAX_MONEY),
    db: Session = Depends(get_db),
) -> dict:
    """Payoff schedule with ``extra`` per month, the extra = 0 baseline, and balance history.

    ``minimum`` overrides the account's minimum payment (required when it has none).
    """
    try:
        account = loans.liability(db, account_id)
        out = loans.payoff(db, account, _cents("extra", extra) or 0, _cents("minimum", minimum), today())
    except LookupError:
        raise HTTPException(status_code=404, detail="account not found") from None
    except loans.LoanError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    out["history"] = account_history(db, account.id, HISTORY_DAYS)
    return out


@router.post("/debt/plan")
def debt_plan(body: DebtPlanBody, db: Session = Depends(get_db)) -> dict:
    lump = None
    if body.lump is not None:
        lump = (_cents("lump", body.lump.amount) or 0, body.lump.account_id)
    try:
        return loans.debt_plan(
            db, body.strategy, _cents("extra", body.extra) or 0, body.order, body.account_ids, today(), lump=lump,
        )
    except loans.LoanError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


MAX_PROGRESS_IDS = 100
_ID_MAX = 2**63 - 1


def _ids(value: str | None) -> list[int] | None:
    """``ids=1,2,3`` → [1, 2, 3]; None when left out. 422 for anything else."""
    if value is None:
        return None
    parts = [p.strip() for p in value.split(",")] if value.strip() else []
    if not parts or len(parts) > MAX_PROGRESS_IDS:
        raise HTTPException(status_code=422, detail=f"ids must list 1 to {MAX_PROGRESS_IDS} account ids")
    out = []
    for part in parts:
        if not part.isascii() or not part.isdecimal() or len(part) > 19 or not 1 <= int(part) <= _ID_MAX:
            raise HTTPException(status_code=422, detail="ids must be account ids separated by commas")
        out.append(int(part))
    return out


@router.get("/debt/progress")
def debt_progress(ids: str | None = Query(default=None, max_length=2000), db: Session = Depends(get_db)) -> dict:
    """Release 3.10: the debts' month-end totals (honest months only) and card limit used."""
    try:
        return loans.debt_progress(db, _ids(ids), today())
    except loans.LoanError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


# ------------------------------------------------------------------ "Paying off debt" in the Budget


def _refused(db: Session, exc: debt_budget.DebtBudgetError) -> JSONResponse:
    db.rollback()
    return JSONResponse(exc.body(), status_code=exc.status)


@router.get("/debt/budget")
def debt_budget_state(db: Session = Depends(get_db)) -> dict:
    return debt_budget.state(db, today())


@router.put("/debt/budget", response_model=None)
def put_debt_budget(body: DebtBudgetBody, db: Session = Depends(get_db)) -> dict | JSONResponse:
    freeze_first(db)  # Release 3.17: ended months keep their goal and debt plans
    try:
        token = debt_budget.put(db, today(), to_cents(body.extra), body.strategy, body.account_ids)
    except debt_budget.DebtBudgetError as exc:
        return _refused(db, exc)
    db.commit()
    return {"state": debt_budget.state(db, today()), "undo": {"token": token}}


@router.delete("/debt/budget", response_model=None)
def delete_debt_budget(db: Session = Depends(get_db)) -> dict | JSONResponse:
    freeze_first(db)  # Release 3.17: ended months keep their goal and debt plans
    try:
        token = debt_budget.remove(db, today())
    except debt_budget.DebtBudgetError as exc:
        return _refused(db, exc)
    db.commit()
    return {"state": debt_budget.state(db, today()), "undo": {"token": token}}


@router.post("/debt/budget/undo", response_model=None)
def undo_debt_budget(body: DebtUndoBody, db: Session = Depends(get_db)) -> dict | JSONResponse:
    freeze_first(db)  # Release 3.17: ended months keep their goal and debt plans
    try:
        debt_budget.undo(db, today(), body.token)
    except debt_budget.DebtBudgetError as exc:
        return _refused(db, exc)
    db.commit()
    return {"state": debt_budget.state(db, today())}

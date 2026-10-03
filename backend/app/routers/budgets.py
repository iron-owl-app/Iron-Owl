from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.orm import Session

from ..deps import freeze_first, get_db
from ..schemas import (
    BudgetAccountsBody,
    BudgetCategoryBody,
    BudgetRowsRestore,
    BudgetSave,
    BudgetSettingsBody,
    BudgetSetupBody,
)
from ..services import budget_setup, budget_transactions, spending
from ..services import categories as cat_service
from ..services.categories import category_out
from ..utils import from_cents, month_of, parse_month, to_cents, today

router = APIRouter(prefix="/api", tags=["budgets"])

MIN_YEAR = 1900


def _month(value: str | None) -> str:
    if value is None or value == "":
        return month_of(today())
    try:
        year, _ = parse_month(value)
    except ValueError:
        raise HTTPException(status_code=422, detail="month must be YYYY-MM") from None
    if year < MIN_YEAR:
        # The review looks back 6 months; before year 1 there is no calendar to go to.
        raise HTTPException(status_code=422, detail=f"month must be {MIN_YEAR}-01 or later")
    return value


def _unprocessable(db: Session, exc: ValueError) -> HTTPException:
    db.rollback()
    return HTTPException(status_code=422, detail=str(exc))


@router.get("/budgets")
def get_budget(month: str | None = Query(default=None, max_length=7), db: Session = Depends(get_db)) -> dict:
    try:
        return spending.budget_month(db, _month(month), today())
    except spending.BudgetError as exc:
        raise _unprocessable(db, exc) from None


@router.get("/budgets/{month}/categories/{category}/transactions")
def get_category_transactions(
    month: str = Path(max_length=7),
    category: str = Path(min_length=1, max_length=64),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1_000_000),
    db: Session = Depends(get_db),
) -> dict:
    try:
        return budget_transactions.category_transactions(db, _month(month), category, limit=limit, offset=offset)
    except spending.BudgetError as exc:
        raise _unprocessable(db, exc) from None


# Declared before PUT /budgets/{month} so "accounts", "settings" and "setup" aren't taken for a month.
@router.put("/budgets/accounts")
def set_budget_accounts(body: BudgetAccountsBody, db: Session = Depends(get_db)) -> dict:
    try:
        spending.set_budget_accounts(db, body.account_ids)
    except spending.BudgetError as exc:
        raise _unprocessable(db, exc) from None
    db.commit()
    return spending.budget_month(db, month_of(today()), today())


@router.put("/budgets/settings")
def set_budget_settings(body: BudgetSettingsBody, db: Session = Depends(get_db)) -> dict:
    """Release 3.6: the income switch ("expected" | "off") and the Emergency savings category."""
    fields = body.model_fields_set
    if "income_mode" in fields and body.income_mode is None:
        raise HTTPException(status_code=422, detail="income_mode must not be null")
    try:
        spending.set_budget_settings(
            db, today(),
            income_mode=body.income_mode if "income_mode" in fields else spending.UNSET,
            savings=body.savings_category if "savings_category" in fields else spending.UNSET,
        )
    except spending.BudgetError as exc:
        raise _unprocessable(db, exc) from None
    db.commit()
    return spending.budget_month(db, month_of(today()), today())


@router.get("/budgets/setup")
def get_setup(db: Session = Depends(get_db)) -> dict:
    return budget_setup.setup_info(db, today())


@router.post("/budgets/setup", status_code=201)
def run_setup(body: BudgetSetupBody, db: Session = Depends(get_db)) -> dict:
    freeze_first(db)  # Release 3.17: ended months keep their goal and debt plans
    try:
        budget_setup.run_setup(
            db, today(),
            answers={key: to_cents(value) for key, value in body.answers.items()},
            income=to_cents(body.income_expected),
            keep_existing=body.keep_existing_in_savings,
            skip=body.skip,
        )
    except budget_setup.SetupDone as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except spending.BudgetError as exc:
        raise _unprocessable(db, exc) from None
    db.commit()
    return spending.budget_month(db, month_of(today()), today())


@router.put("/budgets/{month}")
def save_budget(month: str, body: BudgetSave, db: Session = Depends(get_db)) -> dict:
    month = _month(month)
    fields = body.model_fields_set
    try:
        spending.save_budget(
            db, month, body.assigned or {}, body.removed or [], today(),
            income_expected=body.income_expected if "income_expected" in fields else spending.UNSET,
            accept_received=bool(body.accept_received),
            restore=bool(body.restore),
            moved=body.moved,
        )
    except spending.BudgetError as exc:
        raise _unprocessable(db, exc) from None
    db.commit()
    return spending.budget_month(db, month, today())


@router.get("/budgets/{month}/rows/{category}")
def budget_row(month: str, category: str = Path(..., max_length=64), db: Session = Depends(get_db)) -> dict:
    """Release 3.14: one category's budgets row in ``month`` exactly (for an exact Undo);
    ``state`` null = no row."""
    month = _month(month)
    state = spending.row_state(db, month, category)
    if state is None:
        return {"state": None}
    cents, removed, restart, moved = state
    return {"state": {"assigned": from_cents(cents), "removed": removed, "restart": restart, "moved": from_cents(moved)}}


@router.put("/budgets/{month}/rows")
def restore_budget_rows(month: str, body: BudgetRowsRestore, db: Session = Depends(get_db)) -> dict:
    """Release 3.14: put rows back exactly as ``GET .../rows/{category}`` gave them (Reports'
    Undo), all or nothing. 409 when a row isn't its ``expected`` any more (changed since);
    other refusals are 422. Either way nothing changes."""
    month = _month(month)

    def as_row(s):  # noqa: ANN001, ANN202 - BudgetRowState | None
        return None if s is None else (to_cents(s.assigned), s.removed, s.restart, to_cents(s.moved))

    rows = {r.category: (as_row(r.state), as_row(r.expected)) for r in body.rows}
    try:
        spending.restore_rows(db, month, rows, today())
    except spending.RowChanged as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except spending.BudgetError as exc:
        raise _unprocessable(db, exc) from None
    db.commit()
    return spending.budget_month(db, month, today())


@router.post("/budgets/{month}/categories", status_code=201)
def add_category(month: str, body: BudgetCategoryBody, db: Session = Depends(get_db)) -> dict:
    """Release 3.6: a new spending category with this month's plan, in one transaction."""
    month = _month(month)
    try:
        category = budget_setup.add_category(db, month, body.name, body.group_id, to_cents(body.plan), today())
    except cat_service.NameTaken as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except (cat_service.EmptyName, spending.BudgetError) as exc:
        raise _unprocessable(db, exc) from None
    db.commit()
    return {"category": category_out(category), "month": spending.budget_month(db, month, today())}


@router.post("/budgets/{month}/fund-targets")
def fund_targets(month: str, db: Session = Depends(get_db)) -> dict:
    """Assign every target's ``needed`` in this (current or future) month while Ready to Assign lasts."""
    month = _month(month)
    try:
        funded, unfunded = spending.fund_targets(db, month, today())
    except spending.BudgetError as exc:
        raise _unprocessable(db, exc) from None
    db.commit()
    return {
        "month": spending.budget_month(db, month, today()),
        "funded": from_cents(funded),
        "unfunded": from_cents(unfunded),
    }


@router.get("/spending/review")
def review(month: str | None = Query(default=None, max_length=7), db: Session = Depends(get_db)) -> dict:
    return spending.review(db, _month(month), today())

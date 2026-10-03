from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.orm import Session

from ..deps import get_db
from ..services import reports
from ..utils import month_str, parse_month, to_cents, today

router = APIRouter(prefix="/api/reports", tags=["reports"])

MIN_YEAR = 1900
MAX_RANGE_DAYS = 366 * 5


@router.get("/monthly")
def monthly(months: int = Query(default=12, ge=1, le=reports.MAX_MONTHS), db: Session = Depends(get_db)) -> dict:
    return reports.monthly(db, months, today())


@router.get("/spending")
def spending(month: str | None = Query(default=None, max_length=7), db: Session = Depends(get_db)) -> dict:
    """The Spending tab (Release 3.10): one month's spending by category and store, and the
    last 6 months' totals. ``month`` defaults to today's month."""
    if month is not None:
        try:
            parse_month(month)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        return reports.spending_page(db, month, today())
    except reports.SpendingMonthError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.get("/category/{category_id}")
def category(
    category_id: str = Path(..., max_length=64),
    start: dt.date = Query(...),
    end: dt.date = Query(...),
    months: int = Query(default=6, ge=1, le=24),
    db: Session = Depends(get_db),
) -> dict:
    if start > end:
        raise HTTPException(status_code=422, detail="start must be on or before end")
    if (end - start).days > MAX_RANGE_DAYS or start.year < MIN_YEAR:
        raise HTTPException(status_code=422, detail="date range is out of bounds")
    out = reports.category_detail(db, category_id, start, end, months)
    if out is None:
        raise HTTPException(status_code=404, detail="category not found")
    return out


@router.get("/habits")
def habits(
    month: str = Query(..., max_length=7),
    under: float = Query(default=15, ge=0.01, le=1000),
    db: Session = Depends(get_db),
) -> dict:
    try:
        year_, _ = parse_month(month)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if year_ < MIN_YEAR:
        raise HTTPException(status_code=422, detail="month is out of bounds")
    return reports.habits(db, month, to_cents(under), today())


@router.get("/yoy")
def yoy(
    mode: str = Query(default="year", max_length=8),
    month: str | None = Query(default=None, max_length=7),
    db: Session = Depends(get_db),
) -> dict:
    """Year over year (Release 3.14): this year and last year. ``month`` (Release 3.18, month mode
    only): any month up to today's; a finished month compares whole months."""
    if mode not in reports.YOY_MODES:
        raise HTTPException(status_code=422, detail="mode must be year or month")
    day = today()
    if month is not None:
        try:
            year_, _ = parse_month(month)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if year_ < MIN_YEAR:
            raise HTTPException(status_code=422, detail="month is out of bounds")
        if month > month_str(day.year, day.month):
            raise HTTPException(status_code=422, detail="Pick this month or an earlier one.")
    return reports.yoy(db, mode, day, month)


@router.get("/subscriptions")
def subscriptions(db: Session = Depends(get_db)) -> dict:
    """Recurring money-out items that are subscriptions (Release 3.14), with price changes."""
    return reports.subscriptions(db, today())

"""Investments (D9, Release 3.8): SPEC "Investments"."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..deps import get_db
from ..services import investments
from ..utils import today

router = APIRouter(prefix="/api", tags=["investments"])


@router.get("/investments")
def get_investments(db: Session = Depends(get_db)) -> dict:
    return investments.overview(db, today())

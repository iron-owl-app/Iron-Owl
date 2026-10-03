from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import get_db
from ..models import Account, Holding
from ..schemas import holding_out

router = APIRouter(prefix="/api", tags=["holdings"])


@router.get("/holdings")
def list_holdings(account_id: int | None = None, db: Session = Depends(get_db)) -> list[dict]:
    query = select(Holding, Account.name).join(Account, Account.id == Holding.account_id)
    if account_id is not None:
        query = query.where(Holding.account_id == account_id)
    query = query.order_by(Account.name, Holding.account_id, Holding.value_cents.desc(), Holding.id)
    return [holding_out(h, name) for h, name in db.execute(query).all()]

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..deps import get_db
from ..models import AlertEvent, PlaidItem
from ..services.alerts import disabled_keys
from ..services.networth import networth_history, totals
from ..utils import from_cents, iso_utc

router = APIRouter(prefix="/api", tags=["summary"])


@router.get("/summary")
def summary(db: Session = Depends(get_db)) -> dict:
    t = totals(db)
    assets, liabilities = t["assets"], t["liabilities"]
    last_synced = db.scalar(select(func.max(PlaidItem.last_synced_at)))
    attention = db.scalar(select(func.count()).where(PlaidItem.status != "ok")) or 0
    # Settings D7 update 2: events of alerts that are turned off aren't counted.
    unread = db.scalar(
        select(func.count()).where(
            AlertEvent.read.is_(False), AlertEvent.cleared.is_(False), AlertEvent.key.not_in(disabled_keys(db))
        )
    ) or 0
    return {
        "net_worth": from_cents(assets - liabilities),
        "total_assets": from_cents(assets),
        "total_liabilities": from_cents(liabilities),
        "by_category": {k: from_cents(v) for k, v in t["by_category"].items()},
        "account_count": t["count"],
        "last_synced_at": iso_utc(last_synced),
        "items_needing_attention": attention,
        "unread_alerts": unread,
    }


@router.get("/networth/history")
def history(days: int = Query(default=365, ge=1, le=36500), db: Session = Depends(get_db)) -> list[dict]:
    return networth_history(db, days)

"""Home v2: the whole Home screen in one call (SPEC "Home v2 (Dashboard v2 handoff, Release 3.9)").

"Not now" (``/api/home/dismissals/{key}``) stays in routers/home.py.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..deps import AppState, get_db, get_state
from ..plaid_client import PlaidClient, get_plaid_client
from ..services import dashboard
from ..utils import today

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("")
def get_dashboard(
    db: Session = Depends(get_db),
    state: AppState = Depends(get_state),
    plaid: PlaidClient = Depends(get_plaid_client),
) -> dict:
    out = dashboard.build(db, state, plaid, today())
    db.commit()
    return out

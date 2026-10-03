"""Home's "Not now" store (SPEC Home "Today" > "Not now"; Home v2 keeps these routes).

The Home screen itself is ``GET /api/dashboard`` (routers/dashboard.py, Release 3.9).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from ..deps import get_db
from ..schemas import DismissBody
from ..services import dashboard
from ..utils import today

router = APIRouter(prefix="/api/home", tags=["home"])


def _key(key: str) -> str:
    if not dashboard.KEY_RE.fullmatch(key):
        raise HTTPException(status_code=422, detail="invalid alert key")
    return key


@router.put("/dismissals/{key}", status_code=204)
def dismiss(key: str, body: DismissBody, db: Session = Depends(get_db)) -> Response:
    dashboard.dismiss(db, _key(key), body.fingerprint, today())
    db.commit()
    return Response(status_code=204)


@router.delete("/dismissals/{key}", status_code=204)
def undismiss(key: str, db: Session = Depends(get_db)) -> Response:
    dashboard.undismiss(db, _key(key))
    db.commit()
    return Response(status_code=204)

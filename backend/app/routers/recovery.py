"""Settings -> Recovery sheet (session required).

A new sheet is shown once (``POST``) and stays pending, in memory, until Done (``/activate``);
until then the old sheet keeps working. Any lock or a new session drops the pending sheet.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse

from ..db import DatabaseLocked
from ..deps import AppState, get_state, locked, require_session
from ..schemas import ConfirmSheetBody, CurrentPasswordBody, PendingBody
from ..security import VaultError
from .auth import _error

router = APIRouter(prefix="/api/recovery", tags=["recovery"], dependencies=[Depends(require_session)])


def _run(fn, *args) -> Response:  # noqa: ANN001
    try:
        return JSONResponse(fn(*args))
    except VaultError as exc:
        return _error(exc)
    except DatabaseLocked:
        raise locked() from None


@router.get("")
def get_status(state: AppState = Depends(get_state)) -> Response:
    """``{status: none|active|unconfirmed|stale, sheet, created_at}``."""
    return _run(state.vault.recovery_status)


@router.post("")
def create(body: CurrentPasswordBody, state: AppState = Depends(get_state)) -> Response:
    """Re-auth, then a new pending sheet: ``{pending_id, sheet, created_at, groups}``."""
    return _run(state.vault.create_pending, body.current_password)


@router.post("/activate")
def activate(body: PendingBody, state: AppState = Depends(get_state)) -> Response:
    return _run(state.vault.activate_pending, body.pending_id)


@router.post("/confirm")
def confirm(body: ConfirmSheetBody, state: AppState = Depends(get_state)) -> Response:
    return _run(state.vault.confirm_sheet, body.sheet)

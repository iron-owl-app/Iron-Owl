from __future__ import annotations

import datetime as dt
import logging
from typing import Literal

from fastapi import APIRouter, Body, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from .. import presence as presence_mod
from ..db import DatabaseLocked
from ..deps import AppState, get_state, locked, require_session, session_credentials, tab_id
from ..schemas import ChangePasswordBody, PasswordBody, RecoverBody, RecoveryCodeBody
from ..security import SESSION_COOKIE, SESSION_MOVED, SESSION_OK, NewSession, VaultError
from ..services import accounts, alerts, automation, card_payments, prefs, recurring, spending
from ..utils import today

router = APIRouter(prefix="/api/auth", tags=["auth"])

log = logging.getLogger("fintrack.auth")


def _set_cookie(response: Response, session_id: str) -> None:
    # Not `Secure`: the app is served over plain HTTP on loopback only.
    response.set_cookie(
        SESSION_COOKIE, session_id, httponly=True, samesite="strict", path="/", secure=False
    )


def _clear_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, httponly=True, samesite="strict", path="/")


def _error(exc: VaultError) -> JSONResponse:
    """``{"detail", "code"}`` plus ``tries_left`` (wrong password / sheet), ``retry_after``
    (429, also as a header) and the error's extra fields (e.g. ``groups``, ``active_sheet``)."""
    body: dict = {"detail": exc.detail, "code": exc.code}
    headers = {}
    if exc.tries_left is not None:
        body["tries_left"] = exc.tries_left
    body.update(exc.extra)
    if exc.retry_after is not None:
        body["retry_after"] = exc.retry_after
        headers["Retry-After"] = str(exc.retry_after)
    return JSONResponse(body, status_code=exc.status, headers=headers)


def _ok_with_session(session: NewSession, extra: dict | None = None) -> JSONResponse:
    response = JSONResponse({"ok": True, "session_token": session.token, **(extra or {})})
    _set_cookie(response, session.cookie)
    return response


def bound(state: AppState, tab: str | None) -> None:
    """After a session was created for window ``tab``: that window is open now."""
    if tab is not None:
        state.presence.seen(tab, keep=tab)


def window_fields(request: Request, state: AppState) -> dict:
    """``unlocked``, ``open_elsewhere`` and ``lock_reason`` for the calling window (status
    and heartbeats). Marks the window seen (cancelling its pending close); never activity."""
    tab = tab_id(request)
    if tab is not None:
        # The session's window is never dropped to make room for other ids (presence.seen).
        state.presence.seen(tab, keep=state.vault.bound_tab())
    view = state.vault.presence_view(*session_credentials(request))
    return {
        "unlocked": view.state == SESSION_OK,
        "open_elsewhere": presence_mod.open_elsewhere(state.presence, view, tab),
        "lock_reason": presence_mod.reported_lock_reason(view),
    }


@router.get("/status")
def status(request: Request, state: AppState = Depends(get_state)) -> dict:
    # Polling status does not count as activity, so it cannot defeat the auto-lock.
    window = window_fields(request, state)
    limiter = state.vault.limiter
    return {
        "initialized": state.vault.initialized,
        "unlocked": window["unlocked"],
        # Settings (D7): the vault's saved choice for an unlocked caller, else the env value.
        "auto_lock_minutes": state.vault.auto_lock_minutes if window["unlocked"] else state.settings.auto_lock_minutes,
        # Not secret: whether a sheet exists and its number (printed on it).
        "recovery": state.vault.recovery_public(),
        "open_elsewhere": window["open_elsewhere"],
        "lock_reason": window["lock_reason"],
        "retry_after": limiter.retry_after(),
        "tries_left": limiter.tries_left(),
        "support_contact": state.settings.support_contact.strip() or None,
    }


@router.post("/setup")
def setup(request: Request, body: PasswordBody, state: AppState = Depends(get_state)) -> Response:
    tab = tab_id(request)
    try:
        result = state.vault.setup(body.password, tab=tab)
    except VaultError as exc:
        return _error(exc)
    bound(state, tab)
    prefs.record_password_changed(state)
    start_plans_repeat(state)
    # The only time these groups leave the server: POST response, no-store, never logged.
    recovery = {"sheet": result.sheet, "created_at": result.created_at, "groups": result.groups}
    return _ok_with_session(result.session, {"recovery": recovery})


@router.post("/recover/check")
def recover_check(body: RecoveryCodeBody, state: AppState = Depends(get_state)) -> Response:
    """Is this the sheet? Before asking for a new password. Wrong codes count as wrong tries."""
    try:
        sheet = state.vault.recover_check(body.code)
    except VaultError as exc:
        return _error(exc)
    return JSONResponse({"ok": True, "sheet": sheet})


@router.post("/recover")
def recover(request: Request, body: RecoverBody, state: AppState = Depends(get_state)) -> Response:
    """Set a new password with the recovery sheet; opens FinTrack like unlock."""
    tab = tab_id(request)
    try:
        new_session = state.vault.recover(body.code, body.new_password, tab=tab)
    except VaultError as exc:
        return _error(exc)
    bound(state, tab)
    prefs.record_password_changed(state)
    evaluate_after_unlock(state)
    return _ok_with_session(new_session)


def evaluate_after_unlock(state: AppState) -> None:
    """Alerts are evaluated after every unlock; a problem there never blocks the unlock."""
    try:
        session = state.vault.db.acquire()
    except DatabaseLocked:
        return
    try:
        # After the v6 upgrade: active recurring items get a budget category (once).
        recurring.safe_backfill(session, today())
        # Release 3.9.1: card payments already in the vault become transfers (once).
        card_payments.safe_backfill(session)
        # Release 3.10: manual debts saved negative become the positive amount owed (once).
        accounts.safe_fix_debt_signs(session)
        # Release 3.17: budget plans repeat from the month this version first ran (set once).
        spending.safe_start_plans_repeat(session, today())
        # Release 3.17: months that ended keep the goal and debt plans they showed (once).
        spending.safe_freeze_rule_months(session, today())
        # Iron Owl 2.0.0: an older vault with a bank connection keeps the Trial limit (once).
        accounts.safe_decide_items_cap(session)
        alerts.safe_evaluate(session, today())
    finally:
        state.vault.db.release(session)


def start_plans_repeat(state: AppState) -> None:
    """Release 3.17 for a new vault: its plans repeat from its first month (never raises)."""
    try:
        session = state.vault.db.acquire()
    except DatabaseLocked:
        return
    try:
        spending.safe_start_plans_repeat(session, today())
    finally:
        state.vault.db.release(session)


@router.post("/unlock")
def unlock(request: Request, body: PasswordBody, state: AppState = Depends(get_state)) -> Response:
    tab = tab_id(request)
    try:
        new_session = state.vault.unlock(body.password, tab=tab)
    except VaultError as exc:
        return _error(exc)
    bound(state, tab)
    evaluate_after_unlock(state)
    return _ok_with_session(new_session)


class LockBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Why (GET /api/auth/status "lock_reason"): the window's idle timer, or the Lock button.
    reason: Literal["idle", "manual"] = "manual"


@router.post("/lock")
def lock(
    request: Request, body: LockBody | None = Body(default=None), state: AppState = Depends(get_state)
) -> Response:
    # Locking is always allowed (fail closed), even without a valid session. One exception:
    # a window whose session moved to another window can't lock that one, nor clear the
    # cookie (every window of the browser shares it).
    if state.vault.session_state(*session_credentials(request), touch=False) == SESSION_MOVED:
        return JSONResponse({"ok": True})
    state.vault.lock(body.reason if body is not None else "manual")
    response = JSONResponse({"ok": True})
    _clear_cookie(response)
    return response


@router.post("/change-password", dependencies=[Depends(require_session)])
def change_password(request: Request, body: ChangePasswordBody, state: AppState = Depends(get_state)) -> Response:
    tab = tab_id(request)
    try:
        new_session = state.vault.change_password(body.current_password, body.new_password, tab=tab)
        bound(state, tab)
    except VaultError as exc:
        return _error(exc)
    except DatabaseLocked:
        raise locked() from None
    prefs.record_password_changed(state)
    # The password has changed: nothing below may fail this response, which carries the
    # new session cookie.
    try:
        backup = forced_backup(state)
    except Exception as exc:  # noqa: BLE001
        log.error("the backup after a password change failed: %s", type(exc).__name__)
        backup = {"ok": False}
    return _ok_with_session(new_session, {"backup": backup})


def forced_backup(state: AppState) -> dict | None:
    """Settings (D7): right after a password change, an automatic backup that opens with
    the new password (older ones still need the old one). ``{"ok"}``, or None when no
    backup folder is set. Never fails the password change."""
    try:
        session = state.vault.db.acquire()
    except DatabaseLocked:
        return None
    try:
        folder = automation.backup_folder(session)
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        log.error("could not read the backup folder: %s", type(exc).__name__)
        return None
    finally:
        state.vault.db.release(session)
    if not folder or state.auto_backup is None:
        return None
    return {"ok": bool(state.auto_backup.run(dt.timedelta(0)))}

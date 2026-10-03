from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .config import Settings
from .db import DatabaseLocked
from .plaid_client import PlaidClient
from .presence import TAB_HEADER, valid_tab
from .security import SESSION_COOKIE, SESSION_HEADER, SESSION_MOVED, SESSION_OK, Vault


@dataclass
class AppState:
    settings: Settings
    vault: Vault
    plaid: PlaidClient
    # Release 3 automation (services.automation): the backup runner, and when auto-sync
    # last tried (in memory: a restart means locked, and the first due sync runs again).
    auto_backup: Any = None
    auto_sync_last_attempt: dt.datetime | None = None
    # Release 3.4: the client for keys saved in the vault (services.plaid_keys.PlaidKeyStore)
    # and the factory that builds clients from keys (PlaidClient.from_keys; tests inject fakes).
    plaid_keys: Any = None
    plaid_factory: Any = None
    # Packaged start (lifecycle.py): a random id per server start (GET /api/health), and the
    # heartbeat self-exit watcher (lifecycle.ExitWatcher; disabled unless configured).
    boot_id: str = ""
    exit_watcher: Any = None
    # Release 3.6: when the hourly alert check last ran (automation.monotonic()).
    alerts_last_tick: float | None = None
    # App-window states (D4): which windows are open (presence.Presence).
    presence: Any = None
    # Private updates (updates.service.Updater; its mode is decided once at startup).
    updater: Any = None
    # Settings (D7): the native folder picker for automatic backups (services.folder_picker).
    folder_picker: Any = None
    # Release 3.17: the month the automation loop last froze ended months' goal and debt plans.
    rules_frozen_month: str | None = None

    def active_plaid(self) -> PlaidClient:
        """The client routes and auto-sync use: the ``.env`` keys (or an injected client)
        win; otherwise the vault's keys while unlocked; otherwise the unconfigured client."""
        if self.plaid.configured or self.plaid_keys is None:
            return self.plaid
        return self.plaid_keys.client or self.plaid


def get_state(request: Request) -> AppState:
    return request.app.state.fintrack


def locked() -> HTTPException:
    return HTTPException(status_code=401, detail="locked")


def moved() -> HTTPException:
    """The caller's session was taken over by another FinTrack window ("Open here" or an
    unlock there): the frontend shows "Iron Owl is open in another window"."""
    return HTTPException(status_code=401, detail="moved")


def session_ended(session_state: str) -> HTTPException:
    return moved() if session_state == SESSION_MOVED else locked()


def session_credentials(request: Request) -> tuple[str | None, str | None]:
    return request.cookies.get(SESSION_COOKIE), request.headers.get(SESSION_HEADER)


def tab_id(request: Request) -> str | None:
    """The caller's window id (``X-FinTrack-Tab``), or None when missing or malformed.
    Not a secret and never an auth factor."""
    return valid_tab(request.headers.get(TAB_HEADER))


def require_session(request: Request, state: AppState = Depends(get_state)) -> None:
    """Valid session cookie + header token required; counts as activity for the idle auto-lock.
    401 ``{"detail":"moved"}`` for a session another window took over, else ``"locked"``."""
    session_state = state.vault.session_state(*session_credentials(request), touch=True)
    if session_state != SESSION_OK:
        raise session_ended(session_state)


def get_db(state: AppState = Depends(get_state), _: None = Depends(require_session)) -> Iterator[Session]:
    try:
        session = state.vault.db.acquire()
    except DatabaseLocked:
        raise locked() from None
    try:
        yield session
    finally:
        state.vault.db.release(session)


FREEZE_BUSY = "Iron Owl is busy. Try again in a moment."


def freeze_first(db: Session) -> None:
    """Release 3.17: before a change that can alter goal or debt plans (or their categories),
    months that ended keep the plans they showed (``spending.safe_freeze_rule_months``, its own
    transaction). If that can't be saved, the change is refused (409) and nothing changes."""
    from .services import spending  # local: services import this module's peers
    from .utils import today

    if not spending.safe_freeze_rule_months(db, today()):
        raise HTTPException(status_code=409, detail=FREEZE_BUSY)

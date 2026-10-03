"""Routes for the launcher and the app window.

- ``GET /api/health`` (public) and ``POST /api/app/shutdown`` (control secret): the launcher.
- ``POST /api/app/alive``, ``/closing`` and ``/handoff``: the app window (App-window states,
  D4; see presence.py). ``alive`` and ``closing`` need no session and are never activity.

The SecurityMiddleware Host allowlist applies as everywhere, and every POST also needs the
usual CSRF header (and a JSON body or none).
"""
from __future__ import annotations

import asyncio
import logging
import re

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from ..deps import AppState, get_state, session_credentials, session_ended, tab_id
from ..lifecycle import (
    EXIT_OK,
    EXIT_RESTART,
    HEALTH_PATH,
    SHUTDOWN_PATH,
    health_proof,
    secret_matches,
)
from .. import security
from ..presence import CLOSE_GRACE, PRESENCE_TTL, valid_tab
from ..security import SESSION_OK
from ..version import __version__
from .auth import window_fields

log = logging.getLogger("fintrack.app")

router = APIRouter(tags=["app"])

CHALLENGE_HEADER = "X-FinTrack-Control-Challenge"
CONTROL_HEADER = "X-FinTrack-Control"
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
# Let the 202 reach the caller before uvicorn starts shutting down.
EXIT_DELAY_SECONDS = 0.2
# The close timer fires this much after the grace, so the grace has surely run out.
CLOSE_TIMER_SLACK = 0.25

ALIVE_PATH = "/api/app/alive"
CLOSING_PATH = "/api/app/closing"
HANDOFF_PATH = "/api/app/handoff"

# Close timers still running (a reference keeps their tasks from being garbage collected).
_close_tasks: set[asyncio.Task] = set()


def _require_tab(request: Request) -> str:
    tab = tab_id(request)
    if tab is None:
        raise HTTPException(status_code=400, detail="invalid tab")
    return tab


@router.get(HEALTH_PATH)
def health(request: Request, state: AppState = Depends(get_state)) -> dict:
    """Is FinTrack answering here? No session and no activity (never touches the vault).

    ``web`` is false when the built SPA is missing (launcher/app-window code FT-START-05).
    ``window_open``: some FinTrack window sent a heartbeat or status within PRESENCE_TTL
    and isn't closing (the launcher then focuses it instead of opening another).
    With a control secret configured, a ``X-FinTrack-Control-Challenge`` nonce is answered
    with ``proof`` = HMAC-SHA256(secret, "fintrack-health-v1\\n" + nonce + "\\n" + boot_id).
    """
    settings = state.settings
    body: dict = {
        "app": "fintrack",
        "version": __version__,
        "boot_id": state.boot_id,
        "window_open": state.presence.window_open(),
        "web": (settings.frontend_dist / "index.html").is_file(),
    }
    nonce = request.headers.get(CHALLENGE_HEADER)
    if nonce is not None:
        if not _NONCE_RE.fullmatch(nonce):
            raise HTTPException(status_code=400, detail="invalid challenge")
        if settings.control_enabled:
            body["proof"] = health_proof(
                settings.control_secret.get_secret_value(), nonce, state.boot_id
            )
    return body


class ShutdownBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # True: exit with 75 so the launcher starts FinTrack again (re-reading state.json).
    restart: bool = False


@router.post(SHUTDOWN_PATH, status_code=202)
async def shutdown(
    request: Request,
    body: ShutdownBody | None = Body(default=None),
    state: AppState = Depends(get_state),
) -> dict:
    """Stop the server (the vault locks and backs up on the way out, as on any exit).

    Only for whoever holds this launch's control secret (the launcher, uninstall.ps1).
    404 when the server was started without one (the owner's ``run.py``).
    """
    settings = state.settings
    if not settings.control_enabled:
        raise HTTPException(status_code=404, detail="Not Found")
    if not secret_matches(settings.control_secret.get_secret_value(), request.headers.get(CONTROL_HEADER)):
        log.warning("shutdown refused: wrong or missing control secret")
        raise HTTPException(status_code=403, detail="forbidden")
    code = EXIT_RESTART if body is not None and body.restart else EXIT_OK
    log.info("shutdown requested (exit code %d)", code)
    asyncio.get_running_loop().call_later(EXIT_DELAY_SECONDS, request.app.state.request_exit, code)
    return {"ok": True, "exit_code": code}


# ------------------------------------------------------------------- app window


@router.post(ALIVE_PATH)
def alive(request: Request, state: AppState = Depends(get_state)) -> dict:
    """The window's heartbeat (every 30 s, locked and "open elsewhere" windows too).

    The only request that keeps a packaged server from exiting when unused (the exit
    watcher's heartbeat). Marks the window seen and cancels its pending close. Never
    activity: an open but idle window still auto-locks.
    """
    _require_tab(request)
    state.exit_watcher.beat()
    window = window_fields(request, state)
    return {
        "open_elsewhere": window["open_elsewhere"],
        "unlocked": window["unlocked"],
        "lock_reason": window["lock_reason"],
        "boot_id": state.boot_id,
    }


def finish_close(state: AppState, tab: str) -> bool:
    """After the grace: lock (reason ``closed``, backing up first) if ``tab`` didn't come
    back and still holds the session. True when it locked."""
    try:
        if state.presence.close_due(tab):
            return state.vault.lock_if_bound(tab, "closed")
    except Exception as exc:  # noqa: BLE001 - a timer must never take anything down
        log.error("close-lock failed: %s", type(exc).__name__)
    return False


def finish_due_closes(state: AppState) -> None:
    """Backstop for the timers (the lifespan's periodic check)."""
    for tab in state.presence.due_closes():
        finish_close(state, tab)


def finish_silent_window(state: AppState) -> bool:
    """Backstop for a lost closing beacon (browser killed, crash): lock (``closed``) when the
    window holding the session hasn't sent status or a heartbeat for over PRESENCE_TTL.

    Packaged start only (``exit_when_unused_seconds`` > 0). The owner's ``run.py`` keeps its
    old behaviour: a background tab's throttled timers must not lock the user out, and the idle
    auto-lock still applies. True when it locked.
    """
    if state.settings.exit_when_unused_seconds <= 0:
        return False
    try:
        tab = state.vault.bound_tab()
        if tab is None:
            return False
        last = state.presence.last_seen(tab)
        # Never heard at all: a session bound a moment ago (``bound`` runs just after it).
        if last is None or security.now() - last <= PRESENCE_TTL:
            return False
        locked = state.vault.lock_if_bound(tab, "closed")
        if locked:
            log.info("locked: the FinTrack window stopped answering")
            state.presence.forget(tab)
        return locked
    except Exception as exc:  # noqa: BLE001 - the sweep must never take anything down
        log.error("silent-window lock failed: %s", type(exc).__name__)
        return False


def _schedule_close(state: AppState, tab: str) -> None:
    loop = asyncio.get_running_loop()

    def fire() -> None:
        task = loop.create_task(asyncio.to_thread(finish_close, state, tab))
        _close_tasks.add(task)
        task.add_done_callback(_close_tasks.discard)

    loop.call_later(CLOSE_GRACE + CLOSE_TIMER_SLACK, fire)


@router.post(CLOSING_PATH, status_code=204)
async def closing(request: Request, state: AppState = Depends(get_state)) -> Response:
    """The window is going away (pagehide, not persisted; sent with fetch keepalive).

    If it holds the session, FinTrack locks (``closed``) after CLOSE_GRACE seconds unless
    the same window calls status or alive again first (a reload). Any other window is just
    forgotten. It can only ever lock.
    """
    # When the beacon arrived: a status/heartbeat from the same window handled after this
    # (e.g. while waiting for the vault lock below) means it came back, so no close.
    arrived = security.now()
    tab = _require_tab(request)
    # Off the event loop: the vault lock can be held for seconds (an unlock's Argon2 run).
    if await asyncio.to_thread(state.vault.bound_tab) == tab:
        if state.presence.request_close(tab, arrived):
            _schedule_close(state, tab)
    else:
        state.presence.forget(tab)
    return Response(status_code=204)


class HandoffBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to_tab: str = Field(max_length=200)


@router.post(HANDOFF_PATH)
def handoff(request: Request, body: HandoffBody, state: AppState = Depends(get_state)) -> dict:
    """"Open here": the window holding the session moves it to window ``to_tab`` (which
    asked over the browser's BroadcastChannel). The token rotates; the caller's old token
    then answers 401 ``moved``. Not a way in: it needs the current session.

    400 ``invalid tab`` (malformed, or the session's own window); 401 ``locked``/``moved``;
    409 ``window not found`` (not heard from within PRESENCE_TTL, or closing).
    """
    to_tab = valid_tab(body.to_tab)
    if to_tab is None:
        raise HTTPException(status_code=400, detail="invalid tab")
    credentials = session_credentials(request)
    session_state = state.vault.session_state(*credentials, touch=False)
    if session_state != SESSION_OK:
        raise session_ended(session_state)
    if to_tab == state.vault.bound_tab():
        raise HTTPException(status_code=400, detail="invalid tab")
    if not state.presence.present(to_tab):
        raise HTTPException(status_code=409, detail="window not found")
    session_state, token = state.vault.handoff(*credentials, to_tab)
    if token is None:
        raise session_ended(session_state)
    return {"session_token": token}

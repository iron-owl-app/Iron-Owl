"""Private updates: ``/api/update/*`` (API table: SPEC.md, "Private updates (D5)").

Every route needs the session. The two polling routes (status, progress) never count as
activity, so a window left on the progress screen can't keep the vault unlocked. Errors are
``{"detail", "code"}``; the frontend maps ``code`` to its copy. Every POST needs the CSRF
header and a JSON body, except ``/api/update/file`` (multipart, ``file`` only; the only
other multipart path is the backup restore). In git/dev/not-installed copies (and with
``UPDATE_SOURCE=off``) every action answers 409 ``disabled`` and nothing is scanned or written.
In the GitHub source, ``scan`` checks GitHub (``{"now": true}`` = "Check now"; otherwise only
when the daily check is due) and ``PUT /auto-check`` is the Settings switch.
"""
from __future__ import annotations

import logging
import shutil

from fastapi import APIRouter, Body, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import Field, StrictBool
from starlette.concurrency import run_in_threadpool

from ..db import DatabaseLocked
from ..deps import AppState, get_state, locked, require_session, session_credentials, session_ended
from ..schemas import StrictModel, StrictModel3
from ..security import SESSION_OK, VaultError
from ..services import backup as backup_service
from ..updates import downloads
from ..updates.package import MAX_PACKAGE_BYTES
from ..updates.service import Busy, Disabled, NeedsHelp, RollbackUnavailable
from ..updates.store import StaleOffer
from .auth import _error

log = logging.getLogger("fintrack.updates")

router = APIRouter(prefix="/api/update", tags=["update"])

# The file part plus multipart framing.
MAX_FILE_REQUEST_BYTES = MAX_PACKAGE_BYTES + 1024 * 1024
DEFAULT_FILE_NAME = "update.ftupdate"


class IdBody(StrictModel):
    id: str = Field(max_length=64)


class AckBody(StrictModel):
    at: str = Field(max_length=64)


class RollbackBody(StrictModel):
    password: str


class ScanBody(StrictModel3):
    """``{}`` (look now if due) or ``{"now": true}`` ("Check now" in Settings)."""

    now: StrictBool = False


class AutoCheckBody(StrictModel3):
    on: StrictBool


def _err(status: int, code: str, detail: str | None = None, **extra) -> JSONResponse:
    return JSONResponse({"detail": detail or code, "code": code, **extra}, status_code=status)


def _disabled() -> JSONResponse:
    return _err(409, "disabled", "Updates are off in this copy of Iron Owl.")


def _busy(status: int = 409) -> JSONResponse:
    return _err(status, "busy", "An update is already being installed or checked.")


def _stale() -> JSONResponse:
    return _err(404, "stale_offer", "That update is no longer offered.")


def _too_large() -> JSONResponse:
    return _err(413, "FT-UPD-BIG", "The file is too large to be an Iron Owl update.")


def require_session_quiet(request: Request, state: AppState = Depends(get_state)) -> None:
    """A valid session, but never activity (polling must not delay the idle auto-lock)."""
    session_state = state.vault.session_state(*session_credentials(request), touch=False)
    if session_state != SESSION_OK:
        raise session_ended(session_state)


@router.get("/status", dependencies=[Depends(require_session_quiet)])
def status(state: AppState = Depends(get_state)) -> dict:
    return state.updater.status()


@router.get("/progress", dependencies=[Depends(require_session_quiet)])
def progress(state: AppState = Depends(get_state)) -> Response:
    current = state.updater.progress()
    if current is None:
        return Response(status_code=204)
    return JSONResponse(current)


@router.post("/scan", dependencies=[Depends(require_session)])
def scan(body: ScanBody | None = Body(default=None), state: AppState = Depends(get_state)) -> Response:
    """File source: look in Downloads now (server throttle: at most every 30 s, else the
    cached result). GitHub source: check GitHub when due, or now with ``{"now": true}``."""
    try:
        return JSONResponse(state.updater.scan(now=bool(body and body.now)))
    except Disabled:
        return _disabled()


@router.put("/auto-check", dependencies=[Depends(require_session)])
def auto_check(body: AutoCheckBody, state: AppState = Depends(get_state)) -> Response:
    """Settings: "Check for updates once a day" (GitHub source only; else 409 disabled)."""
    try:
        return JSONResponse(state.updater.set_auto_check(body.on))
    except Disabled:
        return _disabled()


@router.post("/dismiss", dependencies=[Depends(require_session)])
def dismiss(body: IdBody, state: AppState = Depends(get_state)) -> Response:
    """"Not now": no banner for this offer until tomorrow (local date)."""
    try:
        return JSONResponse(state.updater.dismiss(body.id))
    except Disabled:
        return _disabled()
    except StaleOffer:
        return _stale()


@router.post("/install", dependencies=[Depends(require_session)])
def install(body: IdBody, state: AppState = Depends(get_state)) -> Response:
    """202 UpdateProgress (step backup); the window then polls /progress. A check that fails
    before anything starts answers 202 with ``state: "failed"`` (FT-UPD-02)."""
    try:
        return JSONResponse(state.updater.start_install(body.id), status_code=202)
    except Disabled:
        return _disabled()
    except Busy:
        return _busy()
    except StaleOffer:
        return _stale()
    except NeedsHelp as exc:
        return _err(409, "needs_help", "This update needs help to install.", help=exc.help)


@router.post("/result/ack", dependencies=[Depends(require_session)])
def ack_result(body: AckBody, state: AppState = Depends(get_state)) -> Response:
    """The window showed the last result ("up to date" or "didn't install")."""
    try:
        state.updater.ack_result(body.at)
    except Disabled:
        return _disabled()
    return Response(status_code=204)


@router.post("/rollback", dependencies=[Depends(require_session)])
def rollback(body: RollbackBody, state: AppState = Depends(get_state)) -> Response:
    """Optional "Go back to version X" (same data format only). Needs the password (wrong
    tries count toward the shared limiter). 202 ``{to_version}``, then a restart."""
    updater = state.updater
    if not updater.enabled:
        return _disabled()
    try:
        state.vault.verify_password(body.password)
    except VaultError as exc:
        return _error(exc)
    except DatabaseLocked:
        raise locked() from None
    try:
        to = updater.rollback()
    except RollbackUnavailable:
        return _err(409, "rollback_unavailable", "There is no earlier version to go back to.")
    return JSONResponse({"to_version": to}, status_code=202)


@router.post("/file")
async def check_file(request: Request, state: AppState = Depends(get_state)) -> Response:
    """Multipart with one ``file`` part (a picked ``.ftupdate``): UpdateCheck.

    A valid file becomes the offer (source ``picked``). One check at a time (429 busy).
    """
    session_state = await run_in_threadpool(
        state.vault.session_state, *session_credentials(request), touch=True,
    )
    if session_state != SESSION_OK:
        raise session_ended(session_state)
    updater = state.updater
    if not updater.can_update:
        return _disabled()
    if updater.busy:
        return _busy()
    length = request.headers.get("content-length")
    if length is not None and (not length.isdigit() or int(length) > MAX_FILE_REQUEST_BYTES):
        return _too_large()
    if not updater.check_lock.acquire(blocking=False):
        return _busy(429)
    incoming = None
    try:
        incoming = await run_in_threadpool(downloads.new_incoming_dir, updater.store)
        staged = incoming / "package.ftupdate"
        try:
            received = await backup_service.receive_file(
                request, staged, MAX_PACKAGE_BYTES, max_request=MAX_FILE_REQUEST_BYTES,
            )
        except backup_service.TooLarge:
            return _too_large()
        except backup_service.UploadError:
            return _err(400, "bad_upload", "Upload the update file as multipart/form-data.")
        if "file" not in received.seen:
            return _err(400, "bad_upload", "Send the update file.")
        # The upload can take a while: the session must still be theirs (not locked, not
        # taken over by another window) before the file can become the offer.
        session_state = await run_in_threadpool(
            state.vault.session_state, *session_credentials(request), touch=False,
        )
        if session_state != SESSION_OK:
            raise session_ended(session_state)
        try:
            result = await run_in_threadpool(
                updater.check_file, staged, received.filename or DEFAULT_FILE_NAME,
            )
        except Busy:
            return _busy()
        except OSError as exc:
            log.warning("picked update file not checked (%s)", type(exc).__name__)
            return _err(500, "check_failed", "The file couldn't be checked.")
        return JSONResponse(result)
    finally:
        if incoming is not None:
            await run_in_threadpool(shutil.rmtree, incoming, True)
        updater.check_lock.release()

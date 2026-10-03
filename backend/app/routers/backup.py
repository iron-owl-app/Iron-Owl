from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from ..db import DatabaseLocked
from sqlalchemy.orm import Session

from ..deps import AppState, get_db, get_state, locked, require_session, session_credentials, session_ended, tab_id
from ..schemas import AutoBackupBody, EmptyBody, PasswordBody
from ..security import SESSION_OK, VaultError
from ..services import automation
from ..services import backup as backup_service
from ..services import folder_picker
from ..utils import today
from .auth import _error, _ok_with_session, bound, evaluate_after_unlock

log = logging.getLogger("fintrack.backup")

router = APIRouter(prefix="/api", tags=["backup"])


@router.post("/backup", dependencies=[Depends(require_session)])
def download_backup(body: PasswordBody, state: AppState = Depends(get_state)) -> Response:
    try:
        state.vault.verify_password(body.password)
        data = backup_service.build_backup(state.vault)
    except VaultError as exc:
        return _error(exc)
    except DatabaseLocked:
        raise locked() from None
    filename = f"iron-owl-backup-{today().isoformat()}.ftbackup"
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/restore")
async def restore(request: Request, state: AppState = Depends(get_state)) -> Response:
    """Multipart ``file`` + ``password`` (+ ``current_password`` when a vault exists).

    Without a session only while the vault is uninitialized.
    """
    vault = state.vault
    tab = tab_id(request)
    session_state = vault.session_state(*session_credentials(request), touch=True)
    had_session = session_state == SESSION_OK
    if vault.initialized and not had_session:
        raise session_ended(session_state)
    length = request.headers.get("content-length")
    if length is not None and (not length.isdigit() or int(length) > backup_service.MAX_REQUEST_BYTES):
        return JSONResponse({"detail": "The backup file is larger than 200 MB."}, status_code=413)

    staging = await run_in_threadpool(backup_service.staging_dir, vault, "restore")
    try:
        upload = staging / "upload.zip"
        password, current_password = await backup_service.receive_upload(request, upload)
        db_file, keyfile = await run_in_threadpool(backup_service.extract_backup, upload, staging)
        session = await run_in_threadpool(
            vault.restore, db_file, keyfile, password,
            require_uninitialized=not had_session,
            session=session_credentials(request) if had_session else None,
            current_password=current_password,
            tab=tab,
        )
    except backup_service.TooLarge:
        return JSONResponse({"detail": "The backup file is larger than 200 MB."}, status_code=413)
    except VaultError as exc:
        return _error(exc)
    finally:
        await run_in_threadpool(backup_service.remove_dir, staging)
    bound(state, tab)
    await run_in_threadpool(evaluate_after_unlock, state)
    return _ok_with_session(session)


@router.get("/backup/auto")
def get_auto_backup(db: Session = Depends(get_db)) -> dict:
    return automation.backup_settings(db)


@router.put("/backup/auto")
def set_auto_backup(body: AutoBackupBody, db: Session = Depends(get_db), state: AppState = Depends(get_state)) -> dict:
    """Folder for automatic backups (absolute, local, writable, outside data/) or null to turn them off."""
    try:
        automation.save_backup_settings(db, body.dir, body.keep, state.settings.data_dir, create=body.create)
    except automation.BackupDirError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None
    db.commit()
    return automation.backup_settings(db)


@router.get("/backup/auto/suggested-folder", dependencies=[Depends(require_session)])
def suggested_folder() -> dict:
    """Settings (D7) "Turn on": OneDrive\\Iron Owl backups, else Documents\\Iron Owl backups (or an older
    "FinTrack backups" folder that is already there)."""
    return automation.suggested_folder()


@router.post("/backup/auto/pick-folder")
def pick_folder(
    request: Request, body: EmptyBody | None = None, state: AppState = Depends(get_state),
    _: None = Depends(require_session),
) -> Response:
    """Settings (D7) "Change folder…": the native Windows folder picker, shown by the server
    (same PC). ``{dir}`` (null = cancelled); the folder is checked by ``PUT /backup/auto``.

    Never holds a database session while the window is open; the session is checked again
    when it closes (the vault may have locked meanwhile).
    """
    try:
        chosen = state.folder_picker.pick()
    except folder_picker.PickerUnavailable:
        return JSONResponse(
            {"detail": "Choosing a folder this way only works on Windows.", "code": "unavailable"}, status_code=501
        )
    except folder_picker.PickerBusy:
        return JSONResponse(
            {"detail": "A folder window is already open. Finish it first.", "code": "busy"}, status_code=409
        )
    except folder_picker.PickerFailed as exc:
        log.error("folder picker failed: %s", exc)  # an HRESULT or exception type, never a path
        return JSONResponse({"detail": "The folder window couldn't open.", "code": "failed"}, status_code=500)
    session_state = state.vault.session_state(*session_credentials(request), touch=True)
    if session_state != SESSION_OK:
        raise session_ended(session_state)
    return JSONResponse({"dir": chosen})


@router.post("/backup/auto/run")
def run_auto_backup(
    body: EmptyBody | None = None, db: Session = Depends(get_db), state: AppState = Depends(get_state)
) -> Response:
    """Home's "Back up now": one automatic backup at once, to the chosen folder.

    Serialized with every other backup (``AutoBackups.run``); at most one start a minute.
    The folder is re-checked before writing and never listed here. Failures are stored like
    any automatic backup's and logged by exception type only.
    """
    if not automation.backup_folder(db):
        db.rollback()
        return JSONResponse({"detail": "Choose a backup folder first."}, status_code=409)
    db.commit()  # no read transaction held while the backup runs
    try:
        ok = state.auto_backup.run_now()
    except automation.TooSoon as exc:
        return JSONResponse(
            # One message whether the last attempt worked or failed.
            {"detail": "Iron Owl just tried. Wait a minute, then try again.", "retry_after": exc.retry_after},
            status_code=429, headers={"Retry-After": str(exc.retry_after)},
        )
    db.expire_all()  # the runner wrote through its own session
    out = {"ok": ok, "backup": automation.backup_status(db)}
    db.commit()
    return JSONResponse(out)


@router.get("/backup/previous", dependencies=[Depends(require_session)])
def list_previous(state: AppState = Depends(get_state)) -> list[dict]:
    """Vault copies set aside by earlier restores (each opens with its old password)."""
    return backup_service.list_previous(state.settings.data_dir)


@router.post("/backup/previous/{previous_id}/delete", dependencies=[Depends(require_session)])
def delete_previous(previous_id: str, body: PasswordBody, state: AppState = Depends(get_state)) -> Response:
    try:
        state.vault.verify_password(body.password)
    except VaultError as exc:
        return _error(exc)
    except DatabaseLocked:
        raise locked() from None
    if not backup_service.delete_previous(state.settings.data_dir, previous_id):
        raise HTTPException(status_code=404, detail="previous vault not found")
    log.info("deleted a pre-restore vault copy")
    return Response(status_code=204)

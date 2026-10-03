"""Settings -> Bank connection: Plaid keys saved in the vault (SPEC Release 3.4).

The secret is write-only: no response includes it (only its last 4 characters), and
Plaid's free-text error messages are never passed on.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..db import DatabaseLocked
from ..deps import AppState, get_db, get_state, locked
from ..schemas import CurrentPasswordBody, PlaidKeysSaveBody, PlaidKeysTestBody
from ..security import VaultError
from ..services import plaid_keys as keys_service
from .auth import _error

log = logging.getLogger("fintrack.plaid_keys")

router = APIRouter(prefix="/api/plaid/keys", tags=["plaid"])


def _keys_error(status: int, code: str, detail: str, plaid_code: str | None = None) -> JSONResponse:
    body: dict = {"detail": detail, "code": code}
    if plaid_code:
        body["plaid_code"] = plaid_code
    return JSONResponse(body, status_code=status)


def _confirm_password(state: AppState, password: str, missing_detail: str) -> JSONResponse | None:
    try:
        state.vault.confirm_current_password(password, missing_detail=missing_detail)
    except VaultError as exc:
        return _error(exc)
    except DatabaseLocked:
        raise locked() from None
    return None


@router.get("")
def get_keys(db: Session = Depends(get_db), state: AppState = Depends(get_state)) -> dict:
    out = keys_service.status(db, state)
    db.commit()
    return out


@router.post("/test")
def test_keys(
    body: PlaidKeysTestBody, db: Session = Depends(get_db), state: AppState = Depends(get_state)
) -> Response:
    """Candidate keys (stores nothing), or ``{}`` for the active keys (records the result
    when they are the vault's).

    Candidate keys skip the linked-Item ownership probe: the Items' access tokens are never
    sent to Plaid with keys the user hasn't confirmed with their password (the save probes).
    """
    settings = state.settings
    saved_meta: dict | None = None  # set when checking the vault's keys
    candidate = body.client_id is not None and body.secret is not None and body.env is not None
    if candidate:
        client_id, secret, env = body.client_id, body.secret.get_secret_value(), body.env
    elif settings.plaid_configured:
        client_id, secret, env = (
            settings.plaid_client_id, settings.plaid_secret.get_secret_value(), settings.plaid_env
        )
    else:
        stored = keys_service.read_keys(db)
        if stored is None:
            db.commit()
            return _keys_error(409, "not_set", keys_service.NOT_SET)
        client_id, secret, env = stored.client_id, stored.secret, stored.env
        saved_meta = keys_service.read_meta(db)
    tokens = keys_service.item_tokens(db)
    db.commit()  # no DB transaction held during the network calls

    throttle = state.plaid_keys.throttle
    wait = throttle.start()
    if wait is not None:
        return JSONResponse(
            {"detail": keys_service.THROTTLED, "retry_after": wait},
            status_code=429, headers={"Retry-After": str(wait)},
        )
    try:
        probe = not candidate
        if env == "auto":
            result = keys_service.check_auto(state.plaid_factory, client_id, secret, tokens, probe_items=probe)
        else:
            result = keys_service.check(state.plaid_factory, client_id, secret, env, tokens, probe_items=probe)
    finally:
        throttle.done()
    out = result.result()
    if saved_meta is not None:
        # The vault's keys: remember the result, unless they were replaced or removed meanwhile.
        if keys_service.read_meta(db) == saved_meta:
            keys_service.write_last_test(db, out)
        db.commit()
    return JSONResponse(out)


@router.put("")
def save_keys(
    body: PlaidKeysSaveBody, db: Session = Depends(get_db), state: AppState = Depends(get_state)
) -> Response:
    """Re-auth, check the keys with Plaid, then store them; nothing changes on any failure."""
    if state.settings.plaid_configured:
        # Before the password check, so it neither reveals nor uses up attempts.
        return _keys_error(409, "managed_by_env", keys_service.MANAGED_BY_ENV)
    denied = _confirm_password(state, body.current_password, keys_service.SAVE_PASSWORD_MISSING)
    if denied is not None:
        return denied
    tokens = keys_service.item_tokens(db)
    db.commit()  # no DB transaction held during the network calls
    secret = body.secret.get_secret_value()
    result = keys_service.check(state.plaid_factory, body.client_id, secret, body.env, tokens)
    out = result.result()
    if not result.ok:
        return _keys_error(keys_service.SAVE_STATUS[result.code], result.code, out["message"], result.plaid_code)
    if set(keys_service.item_tokens(db)) != set(tokens):
        # A bank was linked or removed during the check: what was validated is stale.
        db.commit()
        return _keys_error(409, "items_changed", keys_service.ITEMS_CHANGED)
    keys_service.write_keys(db, body.client_id, secret, body.env, out)
    db.commit()
    state.plaid_keys.reload(state)
    log.info("plaid keys saved (env=%s)", body.env)
    status = keys_service.status(db, state)
    db.commit()
    return JSONResponse(status)


@router.post("/delete", status_code=204)
def remove_keys(
    body: CurrentPasswordBody, db: Session = Depends(get_db), state: AppState = Depends(get_state)
) -> Response:
    """Idempotent; allowed whatever the source (e.g. keys ignored while ``.env`` has some)."""
    denied = _confirm_password(state, body.current_password, keys_service.REMOVE_PASSWORD_MISSING)
    if denied is not None:
        return denied
    keys_service.delete_keys(db)
    db.commit()
    state.plaid_keys.reload(state)
    log.info("plaid keys removed")
    return Response(status_code=204)

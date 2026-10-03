from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import JSONResponse
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..deps import get_db, require_session
from ..models import Account, PlaidItem
from ..plaid_client import PlaidClient, PlaidError, get_plaid_client
from ..deps import AppState, get_state
from ..schemas import (
    AutoSyncBody,
    ExchangeBody,
    ImportBody,
    ItemsCapBody,
    ItemsLinkedBody,
    LinkTokenBody,
    SyncBody,
    discovered_account_out,
    item_out,
)
from ..services import account_facts, automation
from ..services import accounts as account_service
from ..services import rules as rules_service
from ..services.spending import prune_budget_accounts
from ..services.sync import SyncResult, after_sync, excluded_ids, sync_and_process, sync_item

log = logging.getLogger("fintrack.plaid")

router = APIRouter(prefix="/api/plaid", tags=["plaid"])


def _require_configured(plaid: PlaidClient) -> None:
    if not plaid.configured:
        raise HTTPException(status_code=503, detail="Plaid is not configured")


def _plaid_http_error(exc: PlaidError) -> HTTPException:
    return HTTPException(status_code=502, detail=f"{exc.error_code}: {exc.error_message}")


def _account_counts(db: Session) -> dict[int, int]:
    rows = db.execute(
        select(Account.item_id, func.count()).where(Account.item_id.is_not(None)).group_by(Account.item_id)
    ).all()
    return {item_id: count for item_id, count in rows}


def _get_item(db: Session, item_id: int) -> PlaidItem:
    item = db.get(PlaidItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    return item


def _account_key(name: str | None, mask: str | None, subtype: str | None) -> tuple:
    return ((name or "").strip().lower(), mask or "", (subtype or "").lower())


def _duplicates_existing_item(
    db: Session, plaid: PlaidClient, incoming: list[dict], plaid_item_id: str, institution_id: str | None
) -> bool:
    """True if a *different* Item at the same institution already has any of these accounts.

    Plaid issues new account_ids per Item, so compare by name + mask + subtype. Two separate
    logins at one bank (e.g. two people's accounts) share no accounts and stay allowed.
    A ``pending`` Item has no local accounts yet, so its accounts are fetched live.

    The incoming side keys on Plaid's ``official_name or name``. ``accounts.name`` is their
    nickname, so the local side uses the stored ``official_name``, else the bank's own name
    from ``account_facts`` (Plaid's ``name``), and only then ``accounts.name`` (an account
    renamed before Release 3.10 that hasn't synced since).
    """
    if not institution_id or not incoming:
        return False
    others = list(
        db.scalars(
            select(PlaidItem).where(
                PlaidItem.institution_id == institution_id, PlaidItem.plaid_item_id != plaid_item_id
            )
        )
    )
    if not others:
        return False
    facts = account_facts.load(db)
    existing = {
        _account_key(a.official_name or (facts.get(a.id) or {}).get("bank_name") or a.name, a.mask, a.plaid_subtype)
        for a in db.scalars(select(Account).where(Account.item_id.in_([o.id for o in others])))
    }
    pending_tokens = [o.access_token for o in others if o.status == "pending"]
    db.commit()  # no DB transaction held during network calls
    for token in pending_tokens:
        try:
            existing |= {
                _account_key(pa.get("official_name") or pa.get("name"), pa.get("mask"), _subtype(pa))
                for pa in plaid.get_accounts(token)
            }
        except PlaidError:
            continue
    return any(
        _account_key(pa.get("official_name") or pa.get("name"), pa.get("mask"), _subtype(pa)) in existing
        for pa in incoming
    )


def _subtype(plaid_account: dict) -> str | None:
    value = plaid_account.get("subtype")
    return None if value is None else str(getattr(value, "value", value))


@router.get("/status", dependencies=[Depends(require_session)])
def status(plaid: PlaidClient = Depends(get_plaid_client), db: Session = Depends(get_db)) -> dict:
    # Release 3.4: where the active keys come from ("env" | "vault" | "none"); an injected
    # test client has no ``source``.
    source = getattr(plaid, "source", None) or ("env" if plaid.configured else "none")
    # Release 3.10: "You've used N of 10 bank connections" (a lifetime count) and how many
    # connections there are now (the lowest count the owner can set).
    return {
        "configured": bool(plaid.configured), "env": plaid.env, "source": source,
        **_slots(db),
    }


def _slots(db: Session) -> dict:
    # Iron Owl 2.0.0: the Trial limit is a vault setting; off = no limit (``items_limit`` 0).
    return {
        "items_cap": account_service.items_cap(db),
        "items_linked": account_service.items_linked(db),
        "items_limit": account_service.items_limit(db),
        "items_now": account_service.items_now(db),
    }


@router.put("/items-cap")
def set_items_cap(body: ItemsCapBody, db: Session = Depends(get_db)) -> dict:
    """Settings › Banks "Count bank connections": on keeps the Plaid Trial limit of 10, off
    shows no count and blocks nothing. The lifetime count keeps going up either way."""
    account_service.set_items_cap(db, body.on)
    db.commit()
    return _slots(db)


@router.put("/items-linked")
def set_items_linked(body: ItemsLinkedBody, db: Session = Depends(get_db)):
    """Settings › Banks "Bank connections used: N of 10" › Change…: the real count from
    Plaid's dashboard (connections removed before Release 3.10 weren't counted). From the
    connections there are now up to 10; anything else is refused (422); 409 while
    counting is off."""
    try:
        account_service.set_items_linked(db, body.count)
    except account_service.AccountError as exc:
        db.rollback()
        return JSONResponse(status_code=exc.status, content={"detail": exc.detail})
    db.commit()
    return _slots(db)


@router.post("/link-token")
def link_token(
    body: LinkTokenBody,
    db: Session = Depends(get_db),
    plaid: PlaidClient = Depends(get_plaid_client),
) -> dict:
    _require_configured(plaid)
    access_token = None
    if body.item_id is not None:
        access_token = _get_item(db, body.item_id).access_token
    db.commit()  # no DB transaction held during the network call
    try:
        return {"link_token": plaid.create_link_token(body.kind, access_token)}
    except PlaidError as exc:
        raise _plaid_http_error(exc) from None


def _discovered(db: Session, plaid_accounts: list[dict]) -> list[dict]:
    ids = [pa.get("account_id") for pa in plaid_accounts if pa.get("account_id")]
    imported = set()
    if ids:
        imported = set(db.scalars(select(Account.plaid_account_id).where(Account.plaid_account_id.in_(ids))))
    return [
        discovered_account_out(pa, pa.get("account_id") in imported)
        for pa in plaid_accounts
        if pa.get("account_id")
    ]


@router.post("/exchange", status_code=201)
def exchange(
    body: ExchangeBody,
    db: Session = Depends(get_db),
    plaid: PlaidClient = Depends(get_plaid_client),
) -> dict:
    """Store the Item as ``pending`` and return its accounts; nothing is imported yet."""
    _require_configured(plaid)
    try:
        access_token, plaid_item_id = plaid.exchange_public_token(body.public_token)
    except PlaidError as exc:
        raise _plaid_http_error(exc) from None
    try:
        institution_id, institution_name = plaid.get_institution(access_token)
    except PlaidError:
        institution_id, institution_name = None, None
    fetch_error: PlaidError | None = None
    try:
        incoming = plaid.get_accounts(access_token)
    except PlaidError as exc:
        incoming, fetch_error = [], exc

    if _duplicates_existing_item(db, plaid, incoming, plaid_item_id, institution_id):
        # The duplicate Item was created at Plaid all the same: it used up a connection.
        account_service.count_new_item(db)
        db.commit()
        try:
            plaid.remove_item(access_token)  # don't leave a billed duplicate Item at Plaid
        except PlaidError as exc:
            log.warning("item/remove of duplicate failed: %s", exc.error_code)
        name = institution_name or "This institution"
        raise HTTPException(
            status_code=409,
            detail=f"{name} is already linked. Use Sync to refresh it, or Sign in again if it needs a new login.",
        )

    item = db.scalar(select(PlaidItem).where(PlaidItem.plaid_item_id == plaid_item_id))
    if item is None:
        account_service.count_new_item(db)  # before the row exists: it counts the Items there are
        item = PlaidItem(plaid_item_id=plaid_item_id, kind=body.kind, status="pending", excluded_account_ids="[]")
        db.add(item)
    item.access_token = access_token
    item.institution_id = institution_id
    item.institution_name = institution_name
    db.commit()
    if fetch_error is not None:
        # The Item is kept (pending); the client can list its accounts again later.
        raise _plaid_http_error(fetch_error)
    return {
        "item": item_out(item, _account_counts(db).get(item.id, 0)),
        "accounts": _discovered(db, incoming),
    }


@router.get("/items/{item_id}/accounts")
def discovered_accounts(
    item_id: int,
    db: Session = Depends(get_db),
    plaid: PlaidClient = Depends(get_plaid_client),
) -> list[dict]:
    _require_configured(plaid)
    access_token = _get_item(db, item_id).access_token
    db.commit()
    try:
        incoming = plaid.get_accounts(access_token)
    except PlaidError as exc:
        raise _plaid_http_error(exc) from None
    return _discovered(db, incoming)


@router.post("/items/{item_id}/import")
def import_accounts(
    item_id: int,
    body: ImportBody,
    db: Session = Depends(get_db),
    plaid: PlaidClient = Depends(get_plaid_client),
) -> dict:
    """Choose which accounts to import (first time, or "Manage accounts" later), then sync."""
    _require_configured(plaid)
    access_token = _get_item(db, item_id).access_token
    db.commit()
    try:
        live = [pa.get("account_id") for pa in plaid.get_accounts(access_token) if pa.get("account_id")]
    except PlaidError as exc:
        raise _plaid_http_error(exc) from None
    chosen = set(body.plaid_account_ids)
    if chosen - set(live):
        raise HTTPException(status_code=422, detail="plaid_account_ids contains an unknown account")

    item = _get_item(db, item_id)
    previously_excluded = excluded_ids(item)
    excluded = [pid for pid in live if pid not in chosen]
    # In order: exclusions, status ok, drop newly excluded accounts, then sync.
    item.excluded_account_ids = json.dumps(excluded)
    item.status = "ok"
    item.error_code = None
    if chosen & previously_excluded:
        # Re-included accounts need their history, but the stored cursor is already past it.
        item.transactions_cursor = None
    if excluded:
        db.execute(delete(Account).where(Account.item_id == item.id, Account.plaid_account_id.in_(excluded)))
        prune_budget_accounts(db)
    db.commit()

    result = sync_item(db, plaid, item_id) or SyncResult(item_id=item_id, institution_name=None)
    after_sync(db, [result])
    item = _get_item(db, item_id)
    return {"item": item_out(item, _account_counts(db).get(item.id, 0)), "result": result.as_dict()}


@router.get("/items")
def list_items(db: Session = Depends(get_db)) -> list[dict]:
    counts = _account_counts(db)
    items = db.scalars(select(PlaidItem).order_by(PlaidItem.institution_name, PlaidItem.id))
    return [item_out(i, counts.get(i.id, 0)) for i in items]


@router.post("/sync")
def sync(
    body: SyncBody | None = None,
    db: Session = Depends(get_db),
    plaid: PlaidClient = Depends(get_plaid_client),
) -> dict:
    item_id = body.item_id if body else None
    if item_id is not None:
        ids = [_get_item(db, item_id).id]
    else:
        ids = list(db.scalars(select(PlaidItem.id).order_by(PlaidItem.id)))
    if ids:
        _require_configured(plaid)
    return {"results": [r.as_dict() for r in sync_and_process(db, plaid, ids)]}


@router.get("/auto-sync")
def get_auto_sync(
    db: Session = Depends(get_db),
    state: AppState = Depends(get_state),
    plaid: PlaidClient = Depends(get_plaid_client),
) -> dict:
    return automation.sync_status(db, state, plaid)


@router.put("/auto-sync")
def set_auto_sync(
    body: AutoSyncBody,
    db: Session = Depends(get_db),
    state: AppState = Depends(get_state),
    plaid: PlaidClient = Depends(get_plaid_client),
) -> dict:
    """Sync every ``hours`` while unlocked (0 = off)."""
    automation.set_sync_hours(db, body.hours)
    db.commit()
    return automation.sync_status(db, state, plaid)


@router.delete("/items/{item_id}", status_code=204)
def remove_item(
    item_id: int,
    db: Session = Depends(get_db),
    plaid: PlaidClient = Depends(get_plaid_client),
) -> Response:
    item = _get_item(db, item_id)
    access_token = item.access_token
    db.commit()
    if plaid.configured:
        try:
            plaid.remove_item(access_token)
        except PlaidError as exc:  # best effort: still delete locally
            log.warning("item/remove failed for item %s: %s", item_id, exc.error_code)
    item = _get_item(db, item_id)
    db.delete(item)
    db.flush()
    prune_budget_accounts(db)
    # Release 3.9.1: a removed card's payments from checking count again (card_payments.py).
    rules_service.apply_all(db)
    db.commit()
    return Response(status_code=204)

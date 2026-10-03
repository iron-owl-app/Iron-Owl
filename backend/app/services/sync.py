"""Plaid sync: fetch everything for an item first, then apply it in one transaction.

Fetching before writing keeps the SQLite write transaction short (no network I/O
while holding a lock) and makes each item's update all-or-nothing.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from ..models import (
    Account, AlertEvent, BalanceSnapshot, Holding, PlaidItem, Transaction, TransactionSplit, TransactionTag,
    TxnCategory,
)
from ..plaid_client import KIND_PRODUCTS, PlaidClient, PlaidError
from ..utils import as_date, to_cents, to_cents_opt, today, utcnow
from . import account_facts, alerts, automap, card_payments, debt_budget, investments, ledger, networth, recurring, rules, txns
from .categories import ensure_primary
from .categorize import categorize

log = logging.getLogger("fintrack.sync")

# Products an institution/item simply doesn't have: skip them, don't flag the item.
SKIPPABLE_ERRORS = frozenset(
    {
        "PRODUCTS_NOT_SUPPORTED",
        "PRODUCT_NOT_ENABLED",
        "PRODUCT_NOT_READY",
        "NO_ACCOUNTS",
        "NO_INVESTMENT_ACCOUNTS",
        "NO_INVESTMENT_AUTH_ACCOUNTS",
        "NO_LIABILITY_ACCOUNTS",
    }
)
MUTATION_DURING_PAGINATION = "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION"
MAX_PAGINATION_RESTARTS = 3
MAX_PAGES = 2000


PUBLIC_RESULT_FIELDS = (
    "item_id", "institution_name", "ok", "error_code", "accounts", "transactions_added",
    "transactions_modified", "transactions_removed", "holdings",
)


@dataclass
class SyncResult:
    item_id: int
    institution_name: str | None
    ok: bool = True
    error_code: str | None = None
    accounts: int = 0
    transactions_added: int = 0
    transactions_modified: int = 0
    transactions_removed: int = 0
    holdings: int = 0
    # Internal (not part of the API response): inputs for rules/detection/alerts.
    new_transaction_ids: list[int] = field(default_factory=list)
    prev_status: str | None = None
    status: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in PUBLIC_RESULT_FIELDS}


def excluded_ids(item: PlaidItem) -> set[str]:
    try:
        value = json.loads(item.excluded_account_ids or "[]")
    except ValueError:
        return set()
    return {str(v) for v in value} if isinstance(value, list) else set()


@dataclass
class _Fetched:
    accounts: list[dict] = field(default_factory=list)
    run_transactions: bool = False
    added: list[dict] = field(default_factory=list)
    modified: list[dict] = field(default_factory=list)
    removed: list[dict] = field(default_factory=list)
    cursor: str | None = None
    holdings: dict | None = None
    liabilities: dict | None = None


def _enum(value: Any) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _skippable(call, *args):  # noqa: ANN001, ANN202
    try:
        return call(*args)
    except PlaidError as exc:
        if exc.error_code in SKIPPABLE_ERRORS:
            return None
        raise


# ------------------------------------------------------------------- fetch phase


def _fetch_transactions(plaid: PlaidClient, token: str, cursor: str | None, out: _Fetched) -> None:
    restarts = 0
    while True:
        added: list[dict] = []
        modified: list[dict] = []
        removed: list[dict] = []
        next_cursor = cursor
        try:
            for _ in range(MAX_PAGES):
                page = plaid.sync_transactions(token, next_cursor)
                added.extend(page.get("added") or [])
                modified.extend(page.get("modified") or [])
                removed.extend(page.get("removed") or [])
                next_cursor = page.get("next_cursor") or next_cursor
                if not page.get("has_more"):
                    break
        except PlaidError as exc:
            if exc.error_code == MUTATION_DURING_PAGINATION and restarts < MAX_PAGINATION_RESTARTS:
                restarts += 1  # restart the whole loop from the original cursor
                continue
            raise
        out.added, out.modified, out.removed, out.cursor = added, modified, removed, next_cursor
        return


def _fetch(plaid: PlaidClient, token: str, kind: str, cursor: str | None) -> _Fetched:
    out = _Fetched(cursor=cursor)
    out.accounts = plaid.get_accounts(token)
    # Only call endpoints for products the Item has; fall back to the link kind if Plaid
    # reports none (never guess wider, since an unrequested call adds a billed product).
    products = plaid.item_products(token) or {KIND_PRODUCTS[kind]}
    if "transactions" in products or cursor is not None:
        try:
            _fetch_transactions(plaid, token, cursor, out)
            out.run_transactions = True
        except PlaidError as exc:
            if exc.error_code not in SKIPPABLE_ERRORS:
                raise
    if "investments" in products:
        out.holdings = _skippable(plaid.get_holdings, token)
    if "liabilities" in products:
        out.liabilities = _skippable(plaid.get_liabilities, token)
    return out


# ------------------------------------------------------------------- apply phase


def upsert_snapshot(session: Session, account_id: int, balance_cents: int, day: dt.date | None = None) -> None:
    day = day or today()
    snap = session.scalar(
        select(BalanceSnapshot).where(
            BalanceSnapshot.account_id == account_id, BalanceSnapshot.date == day
        )
    )
    if snap is None:
        session.add(BalanceSnapshot(account_id=account_id, date=day, balance_cents=balance_cents, estimated=False))
    else:
        # A recorded balance replaces an estimated one for the same day.
        snap.balance_cents = balance_cents
        snap.estimated = False


def _upsert_accounts(
    session: Session, item: PlaidItem, plaid_accounts: list[dict], touched: dict[str, Account], excluded: set[str]
) -> None:
    for pa in plaid_accounts:
        pid = pa.get("account_id")
        if not pid or pid in excluded:
            continue  # excluded accounts are skipped entirely: no row, no transactions, no holdings
        acct = touched.get(pid) or session.scalar(
            select(Account).where(Account.plaid_account_id == pid)
        )
        ptype, psub = _enum(pa.get("type")), _enum(pa.get("subtype"))
        balances = pa.get("balances") or {}
        current = balances.get("current")
        if current is None:
            current = balances.get("available")
        if acct is None:
            # Name and category are only set on creation so user edits survive syncs.
            acct = Account(
                item_id=item.id,
                plaid_account_id=pid,
                source="plaid",
                name=pa.get("name") or pa.get("official_name") or "Account",
                category=categorize(ptype, psub),
                institution_name=item.institution_name,
                hidden=False,
            )
            session.add(acct)
        acct.official_name = pa.get("official_name")
        acct.mask = pa.get("mask")
        acct.plaid_type = ptype
        acct.plaid_subtype = psub
        acct.current_balance_cents = to_cents(current) if current is not None else 0
        acct.available_balance_cents = to_cents_opt(balances.get("available"))
        acct.currency = (
            balances.get("iso_currency_code") or balances.get("unofficial_currency_code") or "USD"
        )
        acct.updated_at = utcnow()
        touched[pid] = acct
    session.flush()


def _txn_fields(t: dict) -> dict[str, Any]:
    pfc = t.get("personal_finance_category") or {}
    return {
        "date": as_date(t.get("date")),
        "name": t.get("name") or t.get("merchant_name") or "Transaction",
        "merchant_name": t.get("merchant_name"),
        # Plaid: positive = money out. Ours: negative = money out.
        "amount_cents": -to_cents(t.get("amount") or 0),
        "plaid_category": pfc.get("primary") if isinstance(pfc, dict) else None,
        # Schema v7: the detailed code (e.g. FOOD_AND_DRINK_GROCERIES) for the budget's mapping.
        "plaid_detailed": automap.valid_detailed(pfc.get("detailed")) if isinstance(pfc, dict) else None,
        "pending": bool(t.get("pending")),
    }


def _apply_transactions(session: Session, f: _Fetched, accounts: dict[str, Account], result: SyncResult) -> None:
    known = set(session.scalars(select(TxnCategory.id)))
    matchers = rules.enabled_matchers(session)
    auto_map = automap.load(session)
    # Release 3.9.1: card payments are transfers. The card side is marked here; a new
    # bank-side payment has no id yet, so it is paired with its card side by the rule run in
    # ``after_sync`` (before alerts: a paired payment never raises "Large transaction").
    card_ctx = card_payments.Context.load(session)
    blocker = rules.debt_blocker(session, card_ctx)  # Release 3.10: no card payment in "Paying off debt"
    new_rows: list[Transaction] = []
    split_parents = ledger.split_parent_ids(session)

    def upsert(t: dict) -> bool:
        acct = accounts.get(t.get("account_id"))
        tid = t.get("transaction_id")
        if acct is None or not tid:
            return False
        fields = _txn_fields(t)
        # Before the row is added: inserting a new category may autoflush the session.
        fields["plaid_category"] = ensure_primary(session, fields["plaid_category"], known)
        row = session.scalar(select(Transaction).where(Transaction.plaid_transaction_id == tid))
        if row is None:
            row = Transaction(
                plaid_transaction_id=tid, account_id=acct.id, category_source="plaid", is_transfer=False
            )
            new_rows.append(row)
        old_amount = row.amount_cents
        row.account_id = acct.id
        for key, value in fields.items():
            setattr(row, key, value)
        if row.id in split_parents:
            # Rules never change a split transaction; a new amount rescales its parts.
            if old_amount is not None and old_amount != row.amount_cents:
                txns.rescale_splits(session, row.id, int(old_amount), int(row.amount_cents))
        elif row.category_source not in rules.USER_SOURCES:
            # user > first matching rule > Plaid; resolved here so the sync commit is consistent.
            wanted = rules.resolve(
                matchers, name=row.name, merchant=row.merchant_name, amount_cents=row.amount_cents,
                plaid_category=row.plaid_category, is_transfer=bool(row.is_transfer),
                transfer_source=row.transfer_source, plaid_detailed=row.plaid_detailed, auto_map=auto_map,
                card_payment=rules.card_payment(card_ctx, row), blocked_category=blocker(row),
            )
            for key, value in wanted.items():
                setattr(row, key, value)
        session.add(row)
        return True

    result.transactions_added = sum(upsert(t) for t in f.added)
    result.transactions_modified = sum(upsert(t) for t in f.modified)
    session.flush()
    # Plain values now: the carry-over and the removal below delete rows with bulk statements,
    # and an expired ORM object for a deleted row can't be read afterwards.
    new_pairs = [(r.id, r.plaid_transaction_id) for r in new_rows]
    new_ids = {rid for rid, _ in new_pairs}
    carried = _carry_over_pending(session, f.added, {a.id for a in accounts.values()})
    # Release 3.10: a payment the user filed under "Paying off debt" while it was pending stays
    # counted once it posts (a transfer rule may have marked the posted copy).
    debt_budget.release_filed(session)
    removed_ids = [r.get("transaction_id") for r in f.removed if r.get("transaction_id")]
    if removed_ids:
        res = session.execute(
            delete(Transaction).where(Transaction.plaid_transaction_id.in_(removed_ids))
        )
        result.transactions_removed = res.rowcount or 0
    removed = set(removed_ids)
    gone = set(carried.values())  # pending rows the carry-over deleted
    # A posted copy of an older pending transaction isn't new: its alerts fired when it was
    # pending. When both arrived in this sync, the posted copy is the one to alert on.
    skip = {new_id for new_id, old_id in carried.items() if old_id not in new_ids}
    result.new_transaction_ids = [
        rid for rid, tid in new_pairs if tid not in removed and rid not in gone and rid not in skip
    ]


def _carry_over_pending(session: Session, added: list[dict], account_ids: set[int]) -> dict[int, int]:
    """Keep the user's edits when a pending transaction posts.

    Plaid gives the posted transaction a new id (linked by ``pending_transaction_id``) and
    removes the pending one, so without this a hand-set category, transfer mark, notes,
    splits and tags on the pending row would be lost. The pending row is deleted here too,
    in case Plaid's removal of it comes in a later sync. Only rows on this item's synced
    accounts (``account_ids``) take part. The pending row's "big" alert event moves to the
    posted row (``_move_big_event``), so Home's "Was this you?" stays. Returns
    {posted row id: deleted pending row id}.
    """
    carried: dict[int, int] = {}
    for t in added:
        old_tid, new_tid = t.get("pending_transaction_id"), t.get("transaction_id")
        if not old_tid or not new_tid or old_tid == new_tid:
            continue
        old = session.scalar(select(Transaction).where(Transaction.plaid_transaction_id == old_tid))
        new = session.scalar(select(Transaction).where(Transaction.plaid_transaction_id == new_tid))
        if (
            old is None or new is None or not old.pending or old.account_id != new.account_id
            or new.account_id not in account_ids
        ):
            continue
        has_splits = session.scalar(
            select(TransactionSplit.id).where(TransactionSplit.transaction_id == old.id).limit(1)
        ) is not None
        if has_splits or old.category_source in rules.USER_SOURCES:
            new.category, new.category_source, new.rule_id = old.category, old.category_source, old.rule_id
            if new.transfer_source == card_payments.AUTO:
                # The user's category (or splits) wins over the automatic card payment mark.
                new.is_transfer, new.transfer_source = False, None
        if old.transfer_source == "user":
            new.is_transfer, new.transfer_source = old.is_transfer, old.transfer_source
        if old.notes and not new.notes:
            new.notes = old.notes
        if has_splits:
            session.execute(
                update(TransactionSplit).where(TransactionSplit.transaction_id == old.id)
                .values(transaction_id=new.id)
            )
            # The posted amount can differ (a tip): scale the parts like any amount change.
            txns.rescale_splits(session, new.id, int(old.amount_cents), int(new.amount_cents))
        have = set(session.scalars(select(TransactionTag.tag_id).where(TransactionTag.transaction_id == new.id)))
        for tag_id in session.scalars(select(TransactionTag.tag_id).where(TransactionTag.transaction_id == old.id)).all():
            if tag_id not in have:
                session.add(TransactionTag(transaction_id=new.id, tag_id=tag_id))
        session.flush()
        old_id = old.id
        session.execute(delete(Transaction).where(Transaction.id == old_id))
        _move_big_event(session, old_id, new.id)
        carried[new.id] = old_id
    if carried:
        session.expire_all()  # rows were moved and deleted with bulk statements
    return carried


def _move_big_event(session: Session, old_id: int, new_id: int) -> None:
    """Point the pending row's large-purchase event (``big:{old_id}``) at the posted row.

    The event keeps its id, text, read/cleared state and time (so Home's 3-day "Was this
    you?" and its "Not now" carry on). Nothing happens when there is no such event, or when
    the posted row already has one (``dedupe_key`` is unique: never a second event).
    """
    old_key, new_key = f"big:{old_id}", f"big:{new_id}"
    if session.scalar(select(AlertEvent.id).where(AlertEvent.dedupe_key == new_key)) is not None:
        return
    session.execute(
        update(AlertEvent).where(AlertEvent.key == "big", AlertEvent.dedupe_key == old_key)
        .values(dedupe_key=new_key)
    )


def _apply_holdings(session: Session, item: PlaidItem, data: dict, accounts: dict[str, Account], result: SyncResult) -> None:
    item_account_ids = session.scalars(select(Account.id).where(Account.item_id == item.id)).all()
    if item_account_ids:
        session.execute(delete(Holding).where(Holding.account_id.in_(item_account_ids)))
    securities = {s.get("security_id"): s for s in data.get("securities") or []}
    count = 0
    for h in data.get("holdings") or []:
        acct = accounts.get(h.get("account_id"))
        if acct is None:
            continue
        sec = securities.get(h.get("security_id")) or {}
        value = h.get("institution_value")
        session.add(
            Holding(
                account_id=acct.id,
                security_id=h.get("security_id") or "",
                name=sec.get("name") or sec.get("ticker_symbol") or "Unknown security",
                ticker=sec.get("ticker_symbol"),
                quantity=float(h.get("quantity") or 0),
                price=h.get("institution_price"),
                value_cents=to_cents(value) if value is not None else 0,
                cost_basis_cents=to_cents_opt(h.get("cost_basis")),
            )
        )
        count += 1
    result.holdings = count
    # D9: what each held security is (type, subtype, cash) for the Investments page.
    session.flush()
    investments.store_security_info(session, securities)


def _apply_liabilities(data: dict, accounts: dict[str, Account]) -> None:
    liabilities = data.get("liabilities") or {}

    def set_fields(account_id: str | None, rate: Any, payment: Any, due: Any) -> None:
        acct = accounts.get(account_id)
        if acct is None:
            return
        acct.interest_rate = float(rate) if rate is not None else None
        acct.minimum_payment_cents = to_cents_opt(payment)
        acct.next_payment_due = as_date(due)

    for s in liabilities.get("student") or []:
        set_fields(
            s.get("account_id"),
            s.get("interest_rate_percentage"),
            s.get("minimum_payment_amount"),
            s.get("next_payment_due_date"),
        )
    for m in liabilities.get("mortgage") or []:
        rate = (m.get("interest_rate") or {}).get("percentage")
        set_fields(m.get("account_id"), rate, m.get("next_monthly_payment"), m.get("next_payment_due_date"))
    for c in liabilities.get("credit") or []:
        aprs = c.get("aprs") or []
        rate = aprs[0].get("apr_percentage") if aprs else None
        set_fields(c.get("account_id"), rate, c.get("minimum_payment_amount"), c.get("next_payment_due_date"))


def _record_facts(session: Session, touched: dict[str, Account], f: _Fetched) -> None:
    """``account_facts.record_sync`` in a savepoint: the bank's name and card limit are extras,
    so a failure there is logged (type only) and undone alone; the sync carries on. The
    account upserts are flushed first, outside the savepoint, so their own errors still fail
    the sync as before."""
    session.flush()
    try:
        with session.begin_nested():
            account_facts.record_sync(session, touched, [
                f.accounts, (f.holdings or {}).get("accounts"), (f.liabilities or {}).get("accounts"),
            ])
    except Exception as exc:  # noqa: BLE001
        log.warning("bank's account names not saved: %s", type(exc).__name__)


def _apply(session: Session, item: PlaidItem, f: _Fetched, result: SyncResult) -> None:
    touched: dict[str, Account] = {}
    excluded = excluded_ids(item)
    _upsert_accounts(session, item, f.accounts, touched, excluded)
    result.accounts = len(touched)
    if f.holdings is not None:
        # Investment balances come from the holdings response as well.
        _upsert_accounts(session, item, f.holdings.get("accounts") or [], touched, excluded)
    if f.liabilities is not None:
        _upsert_accounts(session, item, f.liabilities.get("accounts") or [], touched, excluded)
    result.accounts = len(touched)
    # Release 3.10: the bank's own name and card limit (accounts.name is the user's nickname).
    _record_facts(session, touched, f)

    if f.run_transactions:
        _apply_transactions(session, f, touched, result)
        item.transactions_cursor = f.cursor
    if f.holdings is not None:
        _apply_holdings(session, item, f.holdings, touched, result)
    if f.liabilities is not None:
        _apply_liabilities(f.liabilities, touched)

    for acct in touched.values():
        upsert_snapshot(session, acct.id, acct.current_balance_cents)

    item.status = "ok"
    item.error_code = None
    item.last_synced_at = utcnow()
    result.status = "ok"


# ------------------------------------------------------------------------ public


# One item sync at a time: the automatic sync (Release 3) runs in the background and must not
# interleave with a sync the user started (both would insert the same new transactions).
_SYNC_LOCK = threading.RLock()


@contextlib.contextmanager
def sync_lock(*, blocking: bool = True) -> Iterator[bool]:
    """Hold the sync lock; yields False (without waiting) when ``blocking`` is off and it's busy.

    The automatic sync uses ``blocking=False`` so it never queues behind a sync the user
    started (that would only repeat the same Plaid calls right after it).
    """
    acquired = _SYNC_LOCK.acquire(blocking=blocking)
    try:
        yield acquired
    finally:
        if acquired:
            _SYNC_LOCK.release()


def sync_item(session: Session, plaid: PlaidClient, item_id: int) -> SyncResult | None:
    """Sync one item; None for a ``pending`` item (still awaiting account selection)."""
    with _SYNC_LOCK:
        return _sync_item(session, plaid, item_id)


def _sync_item(session: Session, plaid: PlaidClient, item_id: int) -> SyncResult | None:
    # Re-read under the lock: sessions keep loaded objects across commits
    # (expire_on_commit=False), so a copy loaded before another sync committed would
    # still hold the old cursor and fetch the same Plaid pages again.
    item = session.get(PlaidItem, item_id, populate_existing=True)
    if item is None:
        raise LookupError(item_id)
    if item.status == "pending":
        session.commit()
        return None
    result = SyncResult(item_id=item.id, institution_name=item.institution_name, prev_status=item.status)
    token, kind, cursor = item.access_token, item.kind, item.transactions_cursor
    session.commit()  # end the read transaction before any network I/O

    try:
        fetched = _fetch(plaid, token, kind, cursor)
        item = session.get(PlaidItem, item_id)
        if item is None:  # removed while we were fetching
            raise LookupError(item_id)
        _apply(session, item, fetched, result)
        session.commit()
        return result
    except PlaidError as exc:
        session.rollback()
        status = "login_required" if exc.error_code == "ITEM_LOGIN_REQUIRED" else "error"
        return _mark_failed(session, item_id, result, status, exc.error_code)
    except LookupError:
        session.rollback()
        raise
    except Exception as exc:  # noqa: BLE001 - one item must never abort the others
        session.rollback()
        log.error("sync of item %s failed: %s", item_id, type(exc).__name__)
        return _mark_failed(session, item_id, result, "error", "INTERNAL_ERROR")


def _mark_failed(session: Session, item_id: int, result: SyncResult, status: str, code: str) -> SyncResult:
    item = session.get(PlaidItem, item_id)
    if item is not None:
        item.status = status
        item.error_code = code
        item.last_error_at = utcnow()  # every failure, so a dismissed bank error comes back
        session.commit()
    return SyncResult(
        item_id=result.item_id, institution_name=result.institution_name, ok=False, error_code=code,
        prev_status=result.prev_status, status=status,
    )


def sync_items(session: Session, plaid: PlaidClient, item_ids: list[int]) -> list[SyncResult]:
    results = []
    for item_id in item_ids:
        try:
            result = sync_item(session, plaid, item_id)
        except LookupError:
            continue
        if result is not None:
            results.append(result)
    return results


def after_sync(session: Session, results: list[SyncResult], day: dt.date | None = None) -> None:
    """Rules, recurring detection and alerts after a sync.

    Each step commits on its own; a failure is logged (type only) and never turns a
    completed sync into an error.
    """
    if not results:
        return
    day = day or today()
    try:
        # Estimated balance history for bank and credit accounts (never over real snapshots).
        networth.backfill(session, day)
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        log.error("balance backfill after sync failed: %s", type(exc).__name__)
    try:
        rules.apply_all(session)
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        log.error("re-applying rules after sync failed: %s", type(exc).__name__)
    new_recurring: list[int] = []
    try:
        new_recurring = recurring.detect(session, day)
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        new_recurring = []
        log.error("recurring detection after sync failed: %s", type(exc).__name__)
    alerts.safe_evaluate(
        session,
        day,
        new_transaction_ids=[i for r in results for i in r.new_transaction_ids],
        new_recurring_ids=new_recurring,
        status_changes=[(r.item_id, r.status) for r in results if r.status and r.status != r.prev_status],
    )


def sync_and_process(session: Session, plaid: PlaidClient, item_ids: list[int]) -> list[SyncResult]:
    results = sync_items(session, plaid, item_ids)
    after_sync(session, results)
    return results

"""Accounts page (Release 3.10, SPEC "Accounts, Reports tabs and debt plan (Release 3.10)").

- ``list_out``: ``GET /api/accounts`` with the connection, the bank's own name and card limit
  (``account_facts``), the date of the newest recorded balance and whether a manual balance is
  stale (7 days or more; 30 for "other" things the user owns, like a house or car), in a fixed
  number of queries.
- Manual accounts: ``create`` (a ``kind`` sets category + subtype), ``set_balance`` /
  ``undo_balance`` and ``delete`` / ``restore``. Both Undos are single-use tokens kept in
  ``app_settings.account_undo`` (today only, the last 20); the server's own record says what
  to put back, the request only names it.
- The lifetime count of linked bank connections (``app_settings.plaid_items_linked``), which
  the owner can correct in Settings › Banks (``set_items_linked``).
- Whether that count is kept against Plaid's Trial limit (``app_settings.plaid_items_cap``,
  "1"/"0"; Iron Owl 2.0.0): ``items_cap``, ``decide_items_cap_once``, ``set_items_cap``.
- Debts are the positive amount owed: ``normalize_debt`` (PATCH) and the one-time
  ``fix_debt_signs_once`` for manual debts saved negative before Release 3.10.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import secrets
from typing import Any

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select
from sqlalchemy import update as sql_update
from sqlalchemy.orm import Session

from ..models import Account, AppSetting, BalanceSnapshot, Goal, PlaidItem, RecurringItem
from ..schemas import account_out
from ..utils import LIABILITY_CATEGORIES, as_date, from_cents, iso_utc, to_cents, utcnow
from . import account_facts, spending
from .forecast import FORECAST_SETTING
from .sync import upsert_snapshot

log = logging.getLogger("fintrack.accounts")

STALE_DAYS = 7
OTHER_STALE_DAYS = 30  # a house or a car: its value changes slowly
UNDO_SETTING = "account_undo"
MAX_UNDO = 20
ITEMS_LINKED_SETTING = "plaid_items_linked"
ITEMS_LIMIT = 10  # Plaid Trial plan: 10 linked Items, ever
ITEMS_CAP_SETTING = "plaid_items_cap"  # "1" = count against ITEMS_LIMIT, "0" = no limit

NO_UNDO = "This can't be undone any more."
CHANGED = "This can't be undone any more: the balance was changed again since."

# The add-account form's kinds: (category, subtype). The subtype makes the automatic loan
# group work ("Student loans", "Car loans"; none -> "Other loans").
KINDS: dict[str, tuple[str, str | None]] = {
    "checking": ("bank", "checking"),
    "savings": ("bank", "savings"),
    "credit_card": ("credit", "credit card"),
    "student": ("loan", "student"),
    "auto": ("loan", "auto"),
    "other_loan": ("loan", None),
    "investment": ("investment", None),
    "other": ("other", None),  # something else the user owns, like a house or car
}


class AccountError(Exception):
    """A refusal the router turns into ``{"detail"}`` with ``status``."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def owed_cents(category: str, cents: int) -> int:
    """Debts are stored as the positive amount owed, whichever sign the user typed."""
    return abs(cents) if category in LIABILITY_CATEGORIES else cents


# ------------------------------------------------------------------ list


def last_balance_dates(session: Session, account_ids: list[int] | None = None) -> dict[int, dt.date]:
    """Each account's newest recorded (not estimated) balance snapshot date."""
    stmt = (
        select(BalanceSnapshot.account_id, func.max(BalanceSnapshot.date))
        .where(BalanceSnapshot.estimated.is_(False))
        .group_by(BalanceSnapshot.account_id)
    )
    if account_ids is not None:
        if not account_ids:
            return {}
        stmt = stmt.where(BalanceSnapshot.account_id.in_(account_ids))
    return {account_id: as_date(day) for account_id, day in session.execute(stmt).all() if day is not None}


def balance_day(snapshot_day: dt.date | None, created_at: dt.datetime | None) -> dt.date | None:
    """When the balance was last recorded: the newest snapshot, else (an account with no
    snapshot, from before snapshots were kept) the local day it was added, as every balance
    change since writes a snapshot."""
    if snapshot_day is not None or created_at is None:
        return snapshot_day
    return created_at.replace(tzinfo=dt.timezone.utc).astimezone().date()


def age_days(day: dt.date | None, today: dt.date) -> int | None:
    return None if day is None else max(0, (today - day).days)


def stale_days(category: str | None) -> int:
    """How old a typed-in balance may get before FinTrack asks for a new one."""
    return OTHER_STALE_DAYS if category == "other" else STALE_DAYS


def is_stale(source: str, age: int | None, category: str | None) -> bool:
    return source == "manual" and (age is None or age >= stale_days(category))


def _extras(a: Account, item: PlaidItem | None, facts: dict | None, day: dt.date | None, today: dt.date) -> dict:
    age = age_days(day, today)
    # Facts are the bank's: only a linked account shows them (a stale entry can't reach a
    # manual account that reused an id before the next sync pruned it).
    facts = facts if a.source == "plaid" else None
    limit = facts.get("limit_cents") if facts and a.category == "credit" else None
    return {
        "bank_name": facts.get("bank_name") if facts else None,
        "connection": None if item is None else {
            "item_id": item.id, "kind": item.kind, "status": item.status,
            "last_synced_at": iso_utc(item.last_synced_at),
        },
        "balance_date": day.isoformat() if day else None,
        "balance_age_days": age,
        "stale": is_stale(a.source, age, a.category),
        "credit_limit": from_cents(limit),
    }


def list_out(session: Session, today: dt.date) -> list[dict]:
    accounts = list(session.scalars(select(Account).order_by(Account.category, Account.name, Account.id)))
    items = {i.id: i for i in session.scalars(select(PlaidItem))}
    days = last_balance_dates(session)
    facts = account_facts.load(session)
    return [
        {**account_out(a), **_extras(
            a, items.get(a.item_id), facts.get(a.id), balance_day(days.get(a.id), a.created_at), today)}
        for a in accounts
    ]


def one_out(session: Session, a: Account, today: dt.date) -> dict:
    item = session.get(PlaidItem, a.item_id) if a.item_id is not None else None
    day = balance_day(last_balance_dates(session, [a.id]).get(a.id), a.created_at)
    return {**account_out(a), **_extras(a, item, account_facts.load(session).get(a.id), day, today)}


# ------------------------------------------------------------------ undo records


def _records(session: Session, today: dt.date, raw: str | None = None) -> dict[str, dict]:
    raw = raw if raw is not None else spending.get_setting(session, UNDO_SETTING)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    day = today.isoformat()
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, dict) and v.get("day") == day}


def _remember(session: Session, today: dt.date, record: dict) -> str:
    token = secrets.token_urlsafe(16)
    records = _records(session, today)
    records[token] = {**record, "day": today.isoformat()}
    items = list(records.items())[-MAX_UNDO:]
    spending.put_setting(session, UNDO_SETTING, json.dumps(dict(items)))
    return token


def _consume(session: Session, today: dt.date, token: str, kind: str) -> dict | None:
    """Take today's ``kind`` record for ``token`` out of ``account_undo`` in one conditional
    write (it only succeeds while the stored value is still the one read here), so two Undos
    at the same time can't both use it. None when it's unknown, used, from another day or of
    another kind (a balance token can't restore a deleted account, nor the other way round)."""
    raw = spending.get_setting(session, UNDO_SETTING)
    if raw is None:
        return None
    records = _records(session, today, raw)
    rec = records.get(token)
    if rec is None or rec.get("kind") != kind:
        return None
    records.pop(token)
    where = (AppSetting.key == UNDO_SETTING, AppSetting.value == raw)
    if records:
        stmt = sql_update(AppSetting).where(*where).values(value=json.dumps(records))
    else:
        stmt = sql_delete(AppSetting).where(*where)
    result = session.execute(stmt.execution_options(synchronize_session=False))
    for obj in list(session.identity_map.values()):
        if isinstance(obj, AppSetting) and obj.key == UNDO_SETTING:
            session.expire(obj)
    return rec if result.rowcount == 1 else None


def _int(value: Any, low: int = -spending.MAX_AMOUNT_CENTS, high: int = spending.MAX_AMOUNT_CENTS) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise AccountError(409, NO_UNDO)
    return value


def _stamp(value: dt.datetime | None) -> str | None:
    """A row's ``created_at`` to the microsecond (``iso_utc`` drops them), naive UTC + "Z":
    what tells a row apart from a later one that reused its id."""
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return value.isoformat() + "Z"


def _links(rows: Any) -> list[list]:
    """``[[id, created_at stamp, name], ...]`` of the goals or repeating bills that used an account."""
    return [[r.id, _stamp(r.created_at), r.name] for r in rows]


def _link_keys(value: Any) -> dict[int, tuple[str | None, str | None]]:
    """``{id: (stamp, name)}`` from a delete record's links; malformed entries are ignored."""
    out: dict[int, tuple[str | None, str | None]] = {}
    if not isinstance(value, list):
        return out
    for v in value:
        if not (isinstance(v, list) and len(v) == 3):
            continue
        rid, stamp, name = v
        if isinstance(rid, bool) or not isinstance(rid, int) or not 1 <= rid <= 2**63 - 1:
            continue
        if not isinstance(stamp, str) and stamp is not None:
            continue
        if not isinstance(name, str) and name is not None:
            continue
        out[rid] = (stamp, name)
    return out


def _same_row(row: Any, key: tuple[str | None, str | None]) -> bool:
    """Is ``row`` the one the delete saw (not a newer row that reused its id)? By ``created_at``,
    else (a row without one) by name."""
    stamp, name = key
    if stamp is not None:
        return _stamp(row.created_at) == stamp
    return row.created_at is None and name is not None and row.name == name


# ------------------------------------------------------------------ manual accounts


def create(session: Session, body: Any, today: dt.date) -> Account:
    """A manual account (``AccountCreate310``) with today's balance snapshot."""
    name = " ".join((body.name or "").split())
    if not name:
        raise AccountError(422, "Give the account a name.")
    category, subtype = body.category, None
    if body.kind is not None:
        category, subtype = KINDS[body.kind]
        if body.category is not None and body.category != category:
            raise AccountError(422, "The kind and the category don't match. Send just the kind.")
    institution = " ".join((body.institution_name or "").split()) or None
    account = Account(
        source="manual",
        item_id=None,
        name=name,
        category=category,
        plaid_subtype=subtype,
        mask=body.mask,
        current_balance_cents=owed_cents(category, to_cents(body.current_balance)),
        institution_name=institution,
        interest_rate=body.interest_rate,
        minimum_payment_cents=None if body.minimum_payment is None else to_cents(body.minimum_payment),
        next_payment_due=body.next_payment_due,
        notes=body.notes,
        hidden=False,
        currency="USD",
    )
    session.add(account)
    session.flush()
    upsert_snapshot(session, account.id, account.current_balance_cents, today)
    session.flush()
    return account


def _manual(session: Session, account_id: int) -> Account:
    account = session.get(Account, account_id)
    if account is None:
        raise AccountError(404, "account not found")
    if account.source != "manual":
        raise AccountError(400, "Only accounts you add yourself have a balance you type in.")
    return account


def _snapshot(session: Session, account_id: int, day: dt.date) -> BalanceSnapshot | None:
    return session.scalar(
        select(BalanceSnapshot).where(BalanceSnapshot.account_id == account_id, BalanceSnapshot.date == day)
    )


def set_balance(session: Session, account_id: int, balance: float, today: dt.date) -> tuple[Account, str]:
    """Type in a manual account's balance (today's snapshot too). Returns the Undo token."""
    account = _manual(session, account_id)
    new = owed_cents(account.category, to_cents(balance))
    snap = _snapshot(session, account.id, today)
    record = {
        "kind": "balance",
        "account_id": account.id,
        # Which account: an id can be reused after a delete, so the token names this one.
        "created": _stamp(account.created_at),
        "old": int(account.current_balance_cents),
        "new": new,
        "snap": None if snap is None else {"balance_cents": int(snap.balance_cents), "estimated": bool(snap.estimated)},
    }
    account.current_balance_cents = new
    upsert_snapshot(session, account.id, new, today)
    account.updated_at = utcnow()
    session.flush()
    return account, _remember(session, today, record)


def undo_balance(session: Session, token: str, today: dt.date) -> Account:
    """Put back the balance and today's snapshot exactly as they were before the save: today
    only, once, and only while the balance is still the one that save typed in."""
    rec = _consume(session, today, token, "balance")
    if rec is None:
        raise AccountError(409, NO_UNDO)
    account_id = _int(rec.get("account_id"), 1, 2**63 - 1)
    old, new = _int(rec.get("old")), _int(rec.get("new"))
    account = session.get(Account, account_id)
    if account is None or account.source != "manual":
        raise AccountError(409, NO_UNDO)
    created = rec.get("created")
    if not isinstance(created, str) or _stamp(account.created_at) != created:
        # Another account that reused the id (the one this token was for was removed).
        raise AccountError(409, NO_UNDO)
    snap = _snapshot(session, account.id, today)
    if account.current_balance_cents != new or snap is None or snap.balance_cents != new or snap.estimated:
        raise AccountError(409, CHANGED)
    prior = rec.get("snap")
    account.current_balance_cents = old
    if prior is None:
        session.delete(snap)
    elif isinstance(prior, dict):
        snap.balance_cents = _int(prior.get("balance_cents"))
        snap.estimated = prior.get("estimated") is True
    else:
        raise AccountError(409, NO_UNDO)
    account.updated_at = utcnow()
    session.flush()
    return account


_COPIED = (
    "name", "official_name", "mask", "institution_name", "category", "plaid_type", "plaid_subtype",
    "currency", "interest_rate", "notes", "loan_group",
)


def _budget_membership(session: Session, account_id: int) -> str | None:
    saved = spending._saved_selection(session)  # noqa: SLF001 - read only; spending.py is shared
    if saved is None:
        return None
    if saved.exact is not None:
        return "exact" if account_id in saved.exact else None
    if account_id in saved.exclude:
        return "exclude"
    if account_id in saved.include:
        return "include"
    return None


def delete(session: Session, account_id: int, today: dt.date) -> dict:
    """Remove a manual account with everything a restore needs to put it back. Returns the
    ``undo`` ({token, name})."""
    account = _manual(session, account_id)
    aid = account.id
    snaps = session.scalars(
        select(BalanceSnapshot).where(BalanceSnapshot.account_id == account.id).order_by(BalanceSnapshot.date)
    )
    facts = account_facts.load(session)
    forecast = spending.get_setting(session, FORECAST_SETTING) == str(account.id)
    record = {
        "kind": "delete",
        "id": account.id,
        "account": {
            **{name: getattr(account, name) for name in _COPIED},
            "current_balance_cents": int(account.current_balance_cents),
            "available_balance_cents": account.available_balance_cents,
            "minimum_payment_cents": account.minimum_payment_cents,
            "next_payment_due": account.next_payment_due.isoformat() if account.next_payment_due else None,
            "hidden": bool(account.hidden),
            "created_at": _stamp(account.created_at),
        },
        "snapshots": [[s.date.isoformat(), int(s.balance_cents), bool(s.estimated)] for s in snaps],
        # [id, created_at, name]: a restore relinks only these very rows (not newer ones that reused an id).
        "goals": _links(session.scalars(select(Goal).where(Goal.account_id == account.id))),
        "recurring": _links(session.scalars(select(RecurringItem).where(RecurringItem.account_id == account.id))),
        "budget": _budget_membership(session, account.id),
        "forecast": forecast,
        "facts": facts.get(account.id),
    }
    name = account.name
    session.delete(account)
    session.flush()
    spending.prune_budget_accounts(session)
    if forecast:
        # A stale id would silently make the next account that reuses it the forecast account.
        spending.put_setting(session, FORECAST_SETTING, None)
    if aid in facts:
        facts.pop(aid)
        account_facts.store(session, facts)
    _forget_balance_undos(session, today, aid)
    token = _remember(session, today, record)
    return {"token": token, "name": name}


def _forget_balance_undos(session: Session, today: dt.date, account_id: int) -> None:
    """A removed account's balance Undos go with it: a later account that reuses the id must
    never be changed by them."""
    records = _records(session, today)
    kept = {
        k: v for k, v in records.items()
        if not (v.get("kind") == "balance" and v.get("account_id") == account_id)
    }
    if len(kept) != len(records):
        spending.put_setting(session, UNDO_SETTING, json.dumps(kept) if kept else None)


def _text(value: Any, limit: int = 5000) -> str | None:
    return value[:limit] if isinstance(value, str) else None


def _restore_budget(session: Session, membership: Any, account_id: int) -> None:
    if membership not in ("exact", "exclude", "include"):
        return
    saved = spending._saved_selection(session)  # noqa: SLF001
    if membership == "exact":
        if saved is None or saved.exact is None:
            return  # the saved choice changed format since: the default decides
        new = spending.Selection(exact=saved.exact | {account_id})
    else:
        if saved is not None and saved.exact is not None:
            return
        exclude = saved.exclude if saved is not None else frozenset()
        include = saved.include if saved is not None else frozenset()
        if membership == "exclude":
            new = spending.Selection(exclude=exclude | {account_id}, include=include - {account_id})
        else:
            new = spending.Selection(exclude=exclude - {account_id}, include=include | {account_id})
    spending._store_selection(session, new)  # noqa: SLF001


def restore(session: Session, token: str, today: dt.date) -> Account:
    """Undo a delete (today, once): the account (same id if it's still free), its balance
    history, the goals and repeating bills that used it (unless they were changed since), its
    budget-account choice, the forecast account and its ``account_facts`` entry."""
    rec = _consume(session, today, token, "delete")
    if rec is None:
        raise AccountError(409, NO_UNDO)
    old_id = _int(rec.get("id"), 1, 2**63 - 1)
    a = rec.get("account")
    if not isinstance(a, dict) or not isinstance(a.get("name"), str) or not a["name"].strip():
        raise AccountError(409, NO_UNDO)
    if a.get("category") not in ("bank", "hsa", "retirement", "investment", "loan", "credit", "other"):
        raise AccountError(409, NO_UNDO)
    rate = a.get("interest_rate")
    due = a.get("next_payment_due")
    created = a.get("created_at")
    try:
        due_date = dt.date.fromisoformat(due) if isinstance(due, str) else None
        created_at = dt.datetime.fromisoformat(created.rstrip("Z")) if isinstance(created, str) else None
    except ValueError:
        raise AccountError(409, NO_UNDO) from None
    minimum = a.get("minimum_payment_cents")
    available = a.get("available_balance_cents")
    account = Account(
        source="manual",
        item_id=None,
        plaid_account_id=None,
        name=a["name"][:200],
        official_name=_text(a.get("official_name"), 200),
        mask=_text(a.get("mask"), 32),
        institution_name=_text(a.get("institution_name"), 200),
        category=a["category"],
        plaid_type=_text(a.get("plaid_type"), 64),
        plaid_subtype=_text(a.get("plaid_subtype"), 64),
        current_balance_cents=_int(a.get("current_balance_cents")),
        available_balance_cents=None if available is None else _int(available),
        currency=_text(a.get("currency"), 8) or "USD",
        interest_rate=float(rate) if isinstance(rate, (int, float)) and not isinstance(rate, bool) else None,
        minimum_payment_cents=None if minimum is None else _int(minimum, 0),
        next_payment_due=due_date,
        notes=_text(a.get("notes")),
        hidden=a.get("hidden") is True,
        loan_group=_text(a.get("loan_group"), 40),
    )
    if created_at is not None:
        account.created_at = created_at
    if session.get(Account, old_id) is None:
        account.id = old_id
    session.add(account)
    session.flush()
    new_id = account.id
    seen: set[str] = set()
    for row in rec.get("snapshots") if isinstance(rec.get("snapshots"), list) else []:
        if not (isinstance(row, list) and len(row) == 3 and isinstance(row[0], str)) or row[0] in seen:
            continue
        try:
            day = dt.date.fromisoformat(row[0])
        except ValueError:
            continue
        seen.add(row[0])
        session.add(BalanceSnapshot(account_id=new_id, date=day, balance_cents=_int(row[1]), estimated=row[2] is True))
    # Only links the delete cleared, on the very rows it saw: a goal pointed somewhere else
    # since keeps its choice, and a newer goal that reused a deleted one's id isn't touched.
    for model, key in ((Goal, "goals"), (RecurringItem, "recurring")):
        links = _link_keys(rec.get(key))
        if not links:
            continue
        rows = session.scalars(select(model).where(model.id.in_(list(links)), model.account_id.is_(None)))
        for row in rows:
            if _same_row(row, links[row.id]):
                row.account_id = new_id
    _restore_budget(session, rec.get("budget"), new_id)
    if rec.get("forecast") is True and spending.get_setting(session, FORECAST_SETTING) is None:
        spending.put_setting(session, FORECAST_SETTING, str(new_id))
    fact = account_facts.entry(rec.get("facts"))
    if fact is not None:
        facts = account_facts.load(session)
        facts[new_id] = fact
        account_facts.store(session, facts)
    session.flush()
    session.expire_all()
    return session.get(Account, new_id)


# ------------------------------------------------------------------ bank connections used


def items_linked(session: Session) -> int:
    """How many bank connections were ever linked: the stored count, or at least the number
    of connections there are now (a count never stored starts there)."""
    now = items_now(session)
    raw = spending.get_setting(session, ITEMS_LINKED_SETTING)
    stored = int(raw) if raw is not None and raw.isascii() and raw.isdigit() and len(raw) <= 9 else 0
    return max(stored, int(now))


def count_new_item(session: Session) -> None:
    """A new Item was created at Plaid (call before adding its row): one more slot used.
    The count goes up whether or not the limit is on; the limit is decided first, so a new
    vault's first connection doesn't turn it on."""
    decide_items_cap_once(session)
    spending.put_setting(session, ITEMS_LINKED_SETTING, str(items_linked(session) + 1))


def items_now(session: Session) -> int:
    """The bank connections there are now."""
    return int(session.scalar(select(func.count()).select_from(PlaidItem)) or 0)


def set_items_linked(session: Session, count: int) -> int:
    """The owner's correction from Plaid's dashboard (connections removed before Release 3.10
    weren't counted): from the connections there are now up to the plan's limit. Only while
    the limit is on (409 otherwise)."""
    if not decide_items_cap_once(session):
        raise AccountError(409, "Bank connections aren't being counted. Turn on counting first.")
    now = items_now(session)
    if isinstance(count, bool) or not isinstance(count, int) or not now <= count <= ITEMS_LIMIT:
        raise AccountError(422, f"Type a whole number from {now} to {ITEMS_LIMIT}.")
    spending.put_setting(session, ITEMS_LINKED_SETTING, str(count))
    return items_linked(session)


def _stored_cap(session: Session) -> bool | None:
    raw = spending.get_setting(session, ITEMS_CAP_SETTING)
    return True if raw == "1" else False if raw == "0" else None


def _cap_for_old_vault(session: Session) -> bool:
    """The one-time choice for a vault that never saved it: on when it already has a bank
    connection or a saved count (an install from before Iron Owl 2.0.0, on Plaid's Trial
    plan), off otherwise (a new vault)."""
    if items_now(session) > 0:
        return True
    raw = spending.get_setting(session, ITEMS_LINKED_SETTING)
    return raw is not None and raw.isascii() and raw.isdigit() and len(raw) <= 9 and int(raw) > 0


def items_cap(session: Session) -> bool:
    """Is the Trial limit on? Never writes: a vault that hasn't saved the choice yet gets
    the one it would save (``decide_items_cap_once``)."""
    stored = _stored_cap(session)
    return _cap_for_old_vault(session) if stored is None else stored


def items_limit(session: Session) -> int:
    """The limit to show and check: ``ITEMS_LIMIT`` while it's on, 0 (none) while it's off."""
    return ITEMS_LIMIT if items_cap(session) else 0


def decide_items_cap_once(session: Session) -> bool:
    """Save the choice the first time it's needed (after unlock, before a new Item is
    counted, before a count or the switch changes). Flushes; the caller commits."""
    stored = _stored_cap(session)
    if stored is not None:
        return stored
    on = _cap_for_old_vault(session)
    spending.put_setting(session, ITEMS_CAP_SETTING, "1" if on else "0")
    session.flush()
    return on


def safe_decide_items_cap(session: Session) -> None:
    """``decide_items_cap_once`` and commit (after every unlock); never raises."""
    try:
        if _stored_cap(session) is not None:
            return
        decide_items_cap_once(session)
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        log.error("items cap decision failed: %s", type(exc).__name__)


def set_items_cap(session: Session, on: bool) -> bool:
    """Settings › Banks switch. The lifetime count is kept either way."""
    if not isinstance(on, bool):
        raise AccountError(422, "Choose on or off.")
    spending.put_setting(session, ITEMS_CAP_SETTING, "1" if on else "0")
    session.flush()
    return on


# ------------------------------------------------------------------ debts: the amount owed, positive


def normalize_debt(session: Session, account: Account) -> bool:
    """A manual loan or card keeps the positive amount owed (``owed_cents``), its balance
    history too. True when something changed. Linked accounts keep what the bank says."""
    if account.source != "manual" or account.category not in LIABILITY_CATEGORIES:
        return False
    changed = False
    if account.current_balance_cents is not None and account.current_balance_cents < 0:
        account.current_balance_cents = -account.current_balance_cents
        changed = True
    if account.id is not None:
        result = session.execute(
            sql_update(BalanceSnapshot)
            .where(BalanceSnapshot.account_id == account.id, BalanceSnapshot.balance_cents < 0)
            .values(balance_cents=-BalanceSnapshot.balance_cents)
            .execution_options(synchronize_session="fetch")
        )
        changed = changed or bool(result.rowcount)
    return changed


DEBT_SIGN_FIX_KEY = "debt_sign_fix_done"
DEBT_SIGN_FIX_FAILURES_KEY = "debt_sign_fix_failures"
DEBT_SIGN_FIX_MAX_FAILURES = 3


def fix_debt_signs_once(session: Session) -> int:
    """Once (flag ``app_settings.debt_sign_fix_done``): manual loans and cards saved as a
    negative amount before Release 3.10 become the positive amount owed, snapshots included,
    so net worth and the debt pages count them as owed. Returns how many accounts changed."""
    if session.get(AppSetting, DEBT_SIGN_FIX_KEY) is not None:
        return 0
    fixed = 0
    accounts = list(session.scalars(
        select(Account).where(Account.source == "manual", Account.category.in_(LIABILITY_CATEGORIES))
    ))
    for account in accounts:
        if normalize_debt(session, account):
            fixed += 1
    session.add(AppSetting(key=DEBT_SIGN_FIX_KEY, value="1"))
    session.flush()
    return fixed


def safe_fix_debt_signs(session: Session) -> None:
    """``fix_debt_signs_once`` and commit (after every unlock); a failure is logged (type
    only) and never blocks the unlock. After ``DEBT_SIGN_FIX_MAX_FAILURES`` failures in a row
    it is marked done, so it doesn't run on every unlock forever."""
    try:
        if session.get(AppSetting, DEBT_SIGN_FIX_KEY) is not None:
            return
        fix_debt_signs_once(session)
        count = session.get(AppSetting, DEBT_SIGN_FIX_FAILURES_KEY)
        if count is not None:
            session.delete(count)
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        log.error("debt sign fix failed: %s", type(exc).__name__)
        try:
            _count_debt_fix_failure(session)
        except Exception as exc2:  # noqa: BLE001
            session.rollback()
            log.error("debt sign fix failure count failed: %s", type(exc2).__name__)


def _count_debt_fix_failure(session: Session) -> None:
    row = session.get(AppSetting, DEBT_SIGN_FIX_FAILURES_KEY)
    try:
        failures = int(row.value) + 1 if row is not None and row.value else 1
    except ValueError:
        failures = 1
    if failures >= DEBT_SIGN_FIX_MAX_FAILURES:
        if session.get(AppSetting, DEBT_SIGN_FIX_KEY) is None:
            session.add(AppSetting(key=DEBT_SIGN_FIX_KEY, value="gave up"))
        if row is not None:
            session.delete(row)
        log.error("debt sign fix given up after %d failures", failures)
    elif row is None:
        session.add(AppSetting(key=DEBT_SIGN_FIX_FAILURES_KEY, value=str(failures)))
    else:
        row.value = str(failures)
    session.commit()

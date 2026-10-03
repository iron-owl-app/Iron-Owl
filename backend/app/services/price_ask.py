"""Release 3.19: "Netflix now charges $17.99. Update your amount?" (money in: "Acme Payroll
came in at $2,410.00. Update your amount?")

A bill or paycheck whose amount the user set by hand (a detected item whose amount detection's note marks as
theirs, ``recurring.hand_set_ids``, or any item they added themselves) is never changed by price
tracking. When a new charge for it differs by more than ``PRICE_MIN_CENTS`` and more than
``PRICE_MIN_SHARE`` (``alerts._price``, once per charge; charges on hidden accounts never count),
FinTrack asks them instead: on Home's "needs you" list (with "Not now", which hides it from Home
only) and on the item on the Recurring page, until they answer.

- Yes = ``PATCH /api/recurring/{id}`` with the new amount (``is_yes``): it stays theirs
  (``recurring.mark_hand_set``, even when it equals what detection last wrote), so the next price
  change asks again.
- No = ``POST /api/recurring/{id}/price-question`` ``{"transaction_id", "answer": "no"}``: the
  amount stays and that charge never asks again. Every later charge at a price different from their
  amount asks again (even the same new price), until they say Yes or change their amount
  (owner's decision). ``"undo"`` takes a No back.

Stored in ``app_settings.recurring_price_questions`` (``KEY``), one entry per item id:
``{key, created, txn, cents, date, kept}`` (``kept`` = their amount when they said No, else null).
An entry counts only while the item's merchant key and created_at still match (a reused id gets
nothing). A broken value (bad JSON, wrong types, amounts past the money maximum) is ignored, never
raised: Home and Recurring show no question.
"""
from __future__ import annotations

import datetime as dt
import json
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Account, AlertSetting, AppSetting, RecurringItem
from ..schemas import _MAX_MONEY
from ..utils import from_cents, to_cents
from .recurring import _stamp, _whole

log = logging.getLogger("fintrack.price_ask")

KEY = "recurring_price_questions"
MAX_ENTRIES = 200
PRICE_MIN_CENTS = 50
PRICE_MIN_SHARE = 0.01
MAX_CENTS = _MAX_MONEY * 100  # the largest amount the API takes, in cents


def changed(new_cents: int, old_cents: int) -> bool:
    """A price change: more than $0.50 and more than 1% apart."""
    diff = abs(new_cents - old_cents)
    return diff > PRICE_MIN_CENTS and diff > PRICE_MIN_SHARE * abs(old_cents)


def askable(item: RecurringItem) -> bool:
    """Active bills and paychecks that repeat (not one-time items)."""
    return item.status == "active" and item.amount_cents != 0 and item.cadence != "once"


def _money(value: object) -> int | None:
    cents = _whole(value)
    return cents if cents is not None and abs(cents) <= MAX_CENTS else None


def hand_set(item: RecurringItem, by_hand: set[int]) -> bool:
    """Their amount: an item they added (its amount was typed) or one the note marks as theirs."""
    return item.source == "manual" or item.id in by_hand


def _iso_day(value: object) -> str | None:
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        return dt.date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def load(session: Session, items: list[RecurringItem]) -> dict[int, dict]:
    """The valid entries for ``items`` (pass every item before ``save``: others are dropped)."""
    row = session.get(AppSetting, KEY)
    try:
        raw = json.loads(row.value) if row is not None and row.value else {}
    except (ValueError, RecursionError):
        raw = {}
    if not isinstance(raw, dict):
        return {}
    by_id = {item.id: item for item in items}
    out: dict[int, dict] = {}
    for sid, entry in raw.items():
        item = by_id.get(int(sid)) if isinstance(sid, str) and sid.isdecimal() and len(sid) <= 18 else None
        if item is None or not isinstance(entry, dict):
            continue
        if entry.get("key") != item.merchant_key or entry.get("created") != _stamp(item):
            continue
        txn, cents, day = _whole(entry.get("txn")), _money(entry.get("cents")), _iso_day(entry.get("date"))
        kept = entry.get("kept")
        if txn is None or cents is None or day is None or (kept is not None and _money(kept) is None):
            continue
        out[item.id] = {"key": item.merchant_key, "created": _stamp(item), "txn": txn, "cents": cents,
                        "date": day, "kept": kept}
    return out


def save(session: Session, data: dict[int, dict]) -> None:
    newest = sorted(data.items(), key=lambda p: (p[1]["date"], p[1]["txn"]), reverse=True)[:MAX_ENTRIES]
    value = json.dumps({str(i): e for i, e in sorted(newest)}, sort_keys=True)
    row = session.get(AppSetting, KEY)
    if row is None:
        if newest:
            session.add(AppSetting(key=KEY, value=value))
    elif row.value != value:
        row.value = value


def _every(session: Session) -> list[RecurringItem]:
    return list(session.scalars(select(RecurringItem)))


def record(data: dict[int, dict], item: RecurringItem, txn_id: int, cents: int, day: dt.date) -> None:
    """A new charge for a hand-set item, at a different price: ask (``alerts._price``), every
    charge, even at a price they said No to before (owner's decision)."""
    data[item.id] = {"key": item.merchant_key, "created": _stamp(item), "txn": txn_id, "cents": cents,
                     "date": day.isoformat(), "kept": None}


def tidy(data: dict[int, dict], items: list[RecurringItem]) -> None:
    """Drop entries of items that no longer ask (removed, dismissed...) and open questions they
    answered another way (their amount already is the new price)."""
    by_id = {item.id: item for item in items}
    for item_id in list(data):
        item = by_id.get(item_id)
        entry = data[item_id]
        if item is None or not askable(item) or (entry["kept"] is None and not changed(entry["cents"], item.amount_cents)):
            del data[item_id]


def switch_on(session: Session) -> bool:
    """The Settings "price" switch (a missing row counts as on, like the seeds)."""
    setting = session.get(AlertSetting, "price")
    return setting is None or bool(setting.enabled)


def open_questions(session: Session) -> dict[int, dict]:
    """``{item_id: {"transaction_id", "amount" (signed dollars, like the item), "date"}}``: the questions to
    show now. None while the "price" switch is off, for an item on a hidden account, or once
    answered (Yes made the amount the new price; No kept it)."""
    if not switch_on(session):
        return {}
    items = _every(session)
    data = load(session, items)
    if not data:
        return {}
    hidden = set(session.scalars(select(Account.id).where(Account.hidden.is_(True))))
    by_id = {item.id: item for item in items}
    out = {}
    for item_id, entry in data.items():
        item = by_id[item_id]
        if (not askable(item) or item.account_id in hidden or entry["kept"] is not None
                or (entry["cents"] > 0) != (item.amount_cents > 0) or not changed(entry["cents"], item.amount_cents)):
            continue
        out[item_id] = {"transaction_id": entry["txn"], "amount": from_cents(entry["cents"]), "date": entry["date"]}
    return out


def safe_open_questions(session: Session) -> dict[int, dict]:
    """``open_questions``, but a problem only hides the questions (type-only log)."""
    try:
        return open_questions(session)
    except Exception as exc:  # noqa: BLE001 - the Recurring list must still load
        log.error("price questions failed: %s", type(exc).__name__)
        return {}


def is_yes(session: Session, item: RecurringItem, cents: int) -> bool:
    """``PATCH`` sets the amount of the open question's new charge: that is their Yes."""
    q = safe_open_questions(session).get(item.id)
    return q is not None and to_cents(q["amount"]) == cents


def answer(session: Session, item: RecurringItem, transaction_id: int, keep: bool) -> bool:
    """No (``keep``): keep their amount for the question about ``transaction_id``. Undo of No
    (not ``keep``): ask again. False when there is no such question (already answered, or a
    newer charge replaced it); nothing changes then."""
    items = _every(session)
    data = load(session, items)
    entry = data.get(item.id)
    if entry is None or entry["txn"] != transaction_id or (entry["kept"] is None) != keep:
        return False
    entry["kept"] = item.amount_cents if keep else None
    save(session, data)
    return True

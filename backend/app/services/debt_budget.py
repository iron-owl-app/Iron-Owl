"""The Budget's "Paying off debt" category (Release 3.10; SPEC "Accounts, Reports tabs and debt
plan (Release 3.10)", same machinery as the D8 goals).

``app_settings.debt_budget`` = ``{"category": id, "linked": bool, "extra_cents": int,
"strategy": "avalanche"|"snowball", "account_ids": [...], "created_category": bool}`` (no
migration). "Add $X a month to my Budget" on Reports › Paying off debt finds or makes the
"Paying off debt" spending category in the goal group ("Other and saving") and changes this
month's plan by the difference; later months without a row of their own plan ``extra``
(Release 3.17, ``spending.Repeat.debt``) and the line is marked (``EnvelopeLine.debt = {extra}``).

It is for LOANS ONLY (owner decision): a card's balance is already subtracted from the Budget's
money, so paying a card extra isn't new spending. ``account_ids`` may only hold loans, and a
credit card payment can't be filed under the category by any path (by hand, in bulk, by an
Undo, or by a category rule: ``is_card_payment`` / ``card_payment_ids``: a bank-side payment
that pairs with a card's payment (``card_payments.Context``, so it doesn't depend on the mark),
the card-payment mark ``transfer_source = "auto"``, any row on a credit card, or Plaid's
``LOAN_PAYMENTS_CREDIT_CARD_PAYMENT``).

It is a normal envelope: money leaves it only when the user files a loan payment under it on the
Transactions page. Loan payments are fixed-kind (and a transfer rule may mark one), so filing
one there by hand clears a transfer mark on it (``release_filed``): it then counts exactly once
everywhere, as spending in this category (it leaves Fixed / transfers). Which mark it cleared
is kept in ``app_settings.debt_released`` so the Transactions page's Undo puts it back exactly
(``marks_to_put_back``); setting the category or the transfer mark by hand again forgets it.

Every Budget write goes through ``spending.save_budget_cents`` / ``put_back_rows``; the
router commits or rolls back the whole request. Undo records live in ``app_settings.debt_undo``
(this month only, single use, at most ``MAX_UNDO``).
"""
from __future__ import annotations

import datetime as dt
import json
import secrets
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete as sql_delete
from sqlalchemy import exists, func, or_, select
from sqlalchemy import update as sql_update
from sqlalchemy.orm import Session

from ..models import (
    Account,
    AppSetting,
    Budget,
    CategoryGroup,
    RecurringItem,
    Rule,
    Transaction,
    TransactionSplit,
    TxnCategory,
)
from ..utils import LIABILITY_CATEGORIES, from_cents, month_of
from . import budget_setup, card_payments, goal_links, goals, spending
from . import rules as rules_service
from . import categories as cat_service
from .categories import CategoryMap
from .categories import load as load_categories

SETTING = "debt_budget"
UNDO_SETTING = "debt_undo"
RELEASED_SETTING = "debt_released"  # {"<txn id>": {"source": "user"|"rule"|null, "category": id}}
MAX_RELEASED = 500
_MARKS = ("user", "rule", None)
MAX_UNDO = 20
NAME = "Paying off debt"
STRATEGIES = ("avalanche", "snowball")
MAX_ACCOUNTS = 100
_ID_MAX = 2**63 - 1
USER_SOURCES = ("user", "user_bulk")  # services/rules.USER_SOURCES (hand-set categories)

NOT_LINKED = "Paying off debt isn't in your Budget."
CARDS_NOT_HERE = "Credit card payments are already part of your Budget money, so they don't go in this line."
CARD_PAYMENT = (
    "This is a credit card payment. Card payments are already part of your Budget money, "
    "so they don't go in “Paying off debt”."
)
PICK_LOANS = "Pick the loans this is for."
CARD_PAYMENT_DETAILED = "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"  # services/card_payments.DETAILED
CARD_PAYMENT_SOURCE = "auto"  # services/card_payments.AUTO: FinTrack's card payment mark
NO_UNDO = "This can't be undone any more."
CHANGED = "This can't be undone any more: Paying off debt changed on the Budget page since."
NEW_MONTH = "This can't be undone any more: a new month has started."


class DebtBudgetError(goals.GoalError):
    """A refusal the router turns into ``{"detail", **extra}`` with ``status`` (like goals)."""


def _refusal(exc: goals.GoalError) -> DebtBudgetError:
    return DebtBudgetError(exc.status, exc.detail, **exc.extra)


# ------------------------------------------------------------------ the setting


@dataclass(frozen=True)
class Link:
    category: str
    linked: bool
    extra_cents: int
    strategy: str
    account_ids: tuple[int, ...]
    created_category: bool = False

    def to_json(self) -> str:
        return json.dumps({
            "category": self.category, "linked": self.linked, "extra_cents": self.extra_cents,
            "strategy": self.strategy, "account_ids": list(self.account_ids),
            "created_category": self.created_category,
        })


def load(session: Session) -> Link | None:
    """The stored setting; malformed → None (never an error)."""
    raw = spending.get_setting(session, SETTING)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    category, extra = data.get("category"), data.get("extra_cents")
    if not isinstance(category, str) or not 1 <= len(category) <= 64:
        return None
    if not isinstance(extra, int) or isinstance(extra, bool) or not 0 <= extra <= spending.MAX_AMOUNT_CENTS:
        return None
    strategy = data.get("strategy") if data.get("strategy") in STRATEGIES else "avalanche"
    ids = data.get("account_ids")
    account_ids = tuple(
        i for i in (ids if isinstance(ids, list) else [])[:MAX_ACCOUNTS]
        if isinstance(i, int) and not isinstance(i, bool) and 1 <= i <= _ID_MAX
    )
    return Link(category, data.get("linked") is True, extra, strategy, account_ids, data.get("created_category") is True)


def filed_category(session: Session, cmap: CategoryMap) -> str | None:
    """The "Paying off debt" category (linked or not), when it is still a spending category:
    payments filed under it count as spending (``release_filed``)."""
    link = load(session)
    if link is None or not goal_links.valid_category(cmap, link.category):
        return None
    return link.category


def linked(session: Session, cmap: CategoryMap) -> tuple[str, int] | None:
    """(category, extra cents) while the Budget has the debt plan's category, else None."""
    link = load(session)
    if link is None or not link.linked or not goal_links.valid_category(cmap, link.category):
        return None
    if link.category in goal_links.goal_categories(session, cmap):
        return None  # a goal holds it now: the goal wins
    return link.category, link.extra_cents


def linked_category(session: Session, cmap: CategoryMap) -> str | None:
    found = linked(session, cmap)
    return found[0] if found is not None else None


# ------------------------------------------------------------------ filing payments under it


def _card_payment_clause():  # noqa: ANN202 - a SQL expression over Transaction
    """A credit card payment (never filed under "Paying off debt"): FinTrack's card payment
    mark, any row on a credit card, or Plaid's card payment code."""
    cards = select(Account.id).where(Account.category == "credit")
    # coalesce: a NULL must read as "no", so that ``~`` of this clause is never NULL.
    return or_(
        func.coalesce(Transaction.transfer_source, "") == CARD_PAYMENT_SOURCE,
        func.coalesce(Transaction.plaid_detailed, "") == CARD_PAYMENT_DETAILED,
        Transaction.account_id.in_(cards),
    )


def card_payment_ids(session: Session, ids: list[int]) -> set[int]:
    """Which of these transactions are credit card payments: ``_card_payment_clause``, or a
    bank-side payment that pairs with a card's payment (master's ``card_payments.Context``;
    the "auto" mark alone isn't enough: a category set by hand clears it)."""
    unique = list(dict.fromkeys(ids))
    if not unique:
        return set()
    paired = card_payments.Context.load(session).paired
    out: set[int] = {i for i in unique if i in paired}
    for start in range(0, len(unique), 500):
        chunk = unique[start:start + 500]
        out.update(session.scalars(select(Transaction.id).where(Transaction.id.in_(chunk), _card_payment_clause())))
    return out


def is_card_payment(ctx: card_payments.Context, row) -> bool:  # noqa: ANN001 - a Row or a Transaction
    """``card_payment_ids`` for one row already in hand (rule runs and the sync): needs id,
    account_id, amount_cents, plaid_category, plaid_detailed, transfer_source."""
    return (
        row.transfer_source == CARD_PAYMENT_SOURCE
        or row.plaid_detailed == CARD_PAYMENT_DETAILED
        or ctx.kinds.get(row.account_id) == card_payments.CARD_ACCOUNT
        or rules_service.card_payment(ctx, row)
    )


def blocked_category(session: Session) -> str | None:
    """The category card payments can't go in (``filed_category``), for the rule runs: a
    category rule for it never matches a card payment (the next rule decides)."""
    return filed_category(session, load_categories(session))


def filing_blocked(session: Session, category: str | None) -> bool:
    """Is ``category`` the "Paying off debt" category (so card payments can't go in it)?"""
    return category is not None and category == filed_category(session, load_categories(session))


def check_filing(session: Session, txn_id: int, category: str | None) -> None:
    """Refuse (422, ``CARD_PAYMENT``) filing a credit card payment under "Paying off debt"."""
    if filing_blocked(session, category) and card_payment_ids(session, [txn_id]):
        raise DebtBudgetError(422, CARD_PAYMENT)


def release_filed(session: Session, txn_id: int | None = None) -> int:
    """Filing a payment under "Paying off debt" by hand wins over a transfer mark: clear
    ``is_transfer`` on transactions whose hand-set category (or one of whose split parts) is
    that category, so the payment counts once, as spending in its envelope.

    ``txn_id`` (the user just set this one's category): any mark, theirs included (the later choice
    wins). Without it (bulk changes, sync): only a transfer rule's mark, never a "Mark as
    transfer" they chose themselves. Credit card payments are never touched. Runs in the caller's
    transaction. Returns how many rows changed."""
    category = filed_category(session, load_categories(session))
    if category is None:
        return 0
    has_part = exists().where(
        TransactionSplit.transaction_id == Transaction.id, TransactionSplit.category == category
    )
    has_splits = exists().where(TransactionSplit.transaction_id == Transaction.id)
    where = [
        Transaction.is_transfer.is_(True),
        ~_card_payment_clause(),  # a card payment keeps its mark (it never goes in here)
        or_(
            (Transaction.category == category) & Transaction.category_source.in_(USER_SOURCES) & ~has_splits,
            has_part,
        ),
    ]
    if txn_id is not None:
        where.append(Transaction.id == txn_id)
    else:
        where.append(or_(Transaction.transfer_source.is_(None), Transaction.transfer_source != "user"))
    found = session.execute(select(Transaction.id, Transaction.transfer_source, Transaction.category).where(*where)).all()
    if not found:
        return 0
    cards = card_payment_ids(session, [r.id for r in found])  # a paired one may have lost its mark
    found = [r for r in found if r.id not in cards]
    ids = [r.id for r in found]
    for start in range(0, len(ids), 500):
        session.execute(
            sql_update(Transaction).where(Transaction.id.in_(ids[start:start + 500]))
            .values(is_transfer=False, transfer_source=None).execution_options(synchronize_session="fetch")
        )
    if found:
        records = _released(session)
        for r in found:
            records.pop(r.id, None)  # newest last: the oldest go first past MAX_RELEASED
            records[r.id] = {"source": r.transfer_source if r.transfer_source in _MARKS else None, "category": r.category}
        _store_released(session, records)
    return len(found)


def _released(session: Session) -> dict[int, dict]:
    """``debt_released``: the transfer mark ``release_filed`` cleared, per transaction."""
    raw = spending.get_setting(session, RELEASED_SETTING)
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[int, dict] = {}
    for key, rec in data.items():
        if not isinstance(key, str) or not key.isascii() or not key.isdecimal() or len(key) > 18:
            continue
        if not isinstance(rec, dict) or rec.get("source") not in _MARKS or not isinstance(rec.get("category"), str):
            continue
        out[int(key)] = {"source": rec["source"], "category": rec["category"]}
    return out


def _store_released(session: Session, records: dict[int, dict]) -> None:
    items = list(records.items())[-MAX_RELEASED:]
    spending.put_setting(session, RELEASED_SETTING, json.dumps({str(k): v for k, v in items}) if items else None)


def forget_released(session: Session, ids: list[int]) -> None:
    """The user set these transactions' category or transfer mark by hand: an older cleared mark
    must never come back on a later Undo."""
    records = _released(session)
    if records and any(i in records for i in ids):
        for i in ids:
            records.pop(i, None)
        _store_released(session, records)


def marks_to_put_back(
    session: Session, rows: dict[int, Transaction], items: list[tuple[int, str | None, str]],
) -> dict[int, str | None]:
    """The Transactions page's Undo (``txns.restore_categories``): id -> the transfer mark to
    put back. Only for a row still exactly as the filing left it (filed by hand under the
    category it was filed under, no transfer mark) that the Undo moves somewhere else. Call
    before the categories change; the records used are dropped."""
    records = _released(session)
    out: dict[int, str | None] = {}
    for txn_id, category, source in items:
        rec, txn = records.get(txn_id), rows.get(txn_id)
        if rec is None or txn is None or txn.is_transfer or txn.transfer_source is not None:
            continue
        if txn.category != rec["category"] or txn.category_source not in USER_SOURCES:
            continue
        if source in USER_SOURCES and category == txn.category:
            continue  # it stays filed there
        out[txn_id] = rec["source"]
    if out:
        for txn_id in out:
            records.pop(txn_id, None)
        _store_released(session, records)
    return out


# ------------------------------------------------------------------ state


def state(session: Session, today: dt.date) -> dict:
    """``DebtBudgetState``."""
    cmap = load_categories(session)
    book = spending.load_book(session, cmap, today)
    current = book.current
    link = load(session)
    found = linked(session, cmap)
    line = book.members(current).get(found[0]) if found is not None else None
    return {
        "budget_ready": not budget_setup.is_needed(session),
        "linked": found is not None,
        "category_id": found[0] if found is not None else None,
        "extra": from_cents(found[1]) if found is not None else None,
        "strategy": link.strategy if link is not None else None,
        "account_ids": list(link.account_ids) if link is not None else [],
        # The loans the Budget amount is for (still loans; names for the page).
        "loans": _loans_out(session, link.account_ids) if link is not None else [],
        "month": current,
        "planned_this_month": from_cents(int(line.assigned)) if line is not None else None,
        "not_planned": from_cents(book.ready_to_assign()),
    }


# ------------------------------------------------------------------ writes


def _loans_out(session: Session, account_ids: tuple[int, ...]) -> list[dict]:
    if not account_ids:
        return []
    found = {
        a.id: a for a in session.scalars(
            select(Account).where(Account.id.in_(list(account_ids)), Account.category == "loan")
        )
    }
    return [{"account_id": i, "name": found[i].name} for i in account_ids if i in found]


def _check_accounts(session: Session, account_ids: list[int]) -> tuple[int, ...]:
    """Loans only (owner decision): a credit card → 422 ``CARDS_NOT_HERE``."""
    ids = list(dict.fromkeys(account_ids))
    kinds = dict(session.execute(
        select(Account.id, Account.category).where(Account.id.in_(ids), Account.category.in_(LIABILITY_CATEGORIES))
    ).all())
    if any(kinds.get(i) == "credit" for i in ids):
        raise DebtBudgetError(422, CARDS_NOT_HERE)
    if any(i not in kinds for i in ids):
        raise DebtBudgetError(422, PICK_LOANS)
    return tuple(ids)


def _find_category(session: Session, cmap: CategoryMap, link: Link | None) -> tuple[str | None, bool]:
    """The category to use: the one the plan used before (even if it was hidden since), else
    a spending category named "Paying off debt" nobody's goal uses. (None, _) → make one.
    Returns (id, hidden before)."""
    goal_cats = goal_links.goal_categories(session, cmap)
    if link is not None and goal_links.valid_category(cmap, link.category) and link.category not in goal_cats:
        return link.category, bool(cmap.get(link.category).hidden)
    found = cat_service.find_by_name(session, NAME)
    if found is None:
        return None, False
    if found.kind != "spending" or found.id in goal_cats:
        raise DebtBudgetError(
            409, f"You have a category named “{found.name}” that can't hold this plan. "
            "Rename it on the Settings page first.",
        )
    return found.id, bool(found.hidden)


def _undo_records(session: Session, current: str) -> dict[str, dict]:
    raw = spending.get_setting(session, UNDO_SETTING)
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, dict) and v.get("month") == current}


def _remember(session: Session, current: str, record: dict) -> str:
    token = secrets.token_urlsafe(12)
    records = _undo_records(session, current)
    records[token] = {"month": current, **record}
    items = list(records.items())[-MAX_UNDO:]
    spending.put_setting(session, UNDO_SETTING, json.dumps(dict(items)))
    return token


def _states_from(session: Session, category: str, first: str) -> dict[str, list | None]:
    return {
        row.month: [int(row.limit_cents), bool(row.removed), bool(row.restart), int(row.moved_cents or 0)]
        for row in session.scalars(select(Budget).where(Budget.category == category, Budget.month >= first))
    }


def put(session: Session, today: dt.date, extra: int, strategy: str, account_ids: list[int]) -> str:
    """"Add $X a month to my Budget". A first add plans max(what's planned already, extra)
    this month; a change moves this month's plan by (new extra − old extra), never below
    what was already spent from it (``goals.lowest_plan``). Refused past Not planned yet.
    Returns the Undo token."""
    if budget_setup.is_needed(session):
        raise DebtBudgetError(409, goals.SETUP_FIRST)
    if strategy not in STRATEGIES:
        raise DebtBudgetError(422, "strategy must be avalanche or snowball")
    if not 0 < extra <= spending.MAX_AMOUNT_CENTS:
        raise DebtBudgetError(422, "Type how much extra you can pay each month.")
    ids = _check_accounts(session, account_ids)
    cmap = load_categories(session)
    book = spending.load_book(session, cmap, today)
    current = book.current
    link = load(session)
    raw_before = spending.get_setting(session, SETTING)
    found = linked(session, cmap)
    created = False
    if found is not None:
        category, hidden_before = found[0], bool(cmap.get(found[0]).hidden)
    else:
        category, hidden_before = _find_category(session, cmap, link)
    line = book.members(current).get(category) if category is not None else None
    assigned = int(line.assigned) if line is not None else 0
    if found is not None and line is not None:
        delta = extra - found[1]
        value = assigned + delta if delta > 0 else max(assigned + delta, goals.lowest_plan(line))
    else:
        value = max(assigned, extra)
    ready = book.ready_to_assign()
    if value - assigned > max(0, ready):
        raise _refusal(goals._not_enough(ready))

    rows_before: dict[str, list | None] = {}
    group_made: int | None = None
    if category is None:
        last_group = session.scalar(select(func.max(CategoryGroup.id))) or 0
        group_id = goals._goal_group(session, cmap, spending.savings_category(session, cmap))
        group_made = group_id if group_id > last_group else None  # a new "Other and saving"
        category = cat_service.create_custom(session, NAME, "spending", group_id=group_id).id
        created = True
        cmap = load_categories(session)
    else:
        state_before = spending.row_state(session, current, category)
        rows_before[current] = list(state_before) if state_before is not None else None
        session.get(TxnCategory, category).hidden = False
    rows_before.setdefault(current, None)
    session.flush()
    if line is None or value != assigned:
        try:
            spending.save_budget_cents(session, cmap, current, {category: value}, [], today)
        except spending.BudgetError as exc:
            raise DebtBudgetError(422, str(exc)) from None
    made = created or (link is not None and link.category == category and link.created_category)
    spending.put_setting(session, SETTING, Link(category, True, extra, strategy, ids, made).to_json())
    after = spending.row_state(session, current, category)
    return _remember(session, current, {
        "category": category, "setting": raw_before, "created": created, "hidden": hidden_before,
        "rows": rows_before, "after": {current: list(after) if after is not None else None},
        "group": group_made,
    })


def remove(session: Session, today: dt.date) -> str:
    """Take "Paying off debt" out of the Budget (DELETE): it leaves this month's Budget (its
    leftover goes back to Not planned yet; overspending comes out of it) and is hidden when no
    transaction uses it. Payments filed under it stay there. Returns the Undo token."""
    cmap = load_categories(session)
    found = linked(session, cmap)
    link = load(session)
    if found is None or link is None:
        raise DebtBudgetError(409, NOT_LINKED)
    category = found[0]
    current = month_of(today)
    raw_before = spending.get_setting(session, SETTING)
    hidden_before = bool(cmap.get(category).hidden)
    rows_before = _states_from(session, category, current)
    book = spending.load_book(session, cmap, today)
    if category in book.members(current):
        try:
            spending.save_budget_cents(session, cmap, current, {}, [category], today, release_overspent=True)
        except spending.BudgetError as exc:
            raise DebtBudgetError(422, f"Iron Owl couldn't take {NAME} out of your Budget. {exc}") from None
    if not goals._has_transactions(session, category):
        session.get(TxnCategory, category).hidden = True
    spending.put_setting(session, SETTING, Link(
        category, False, link.extra_cents, link.strategy, link.account_ids, link.created_category,
    ).to_json())
    session.flush()
    after = _states_from(session, category, current)
    rows = {m: rows_before.get(m) for m in set(rows_before) | set(after)}
    return _remember(session, current, {
        "category": category, "setting": raw_before, "created": False, "hidden": hidden_before,
        "rows": rows, "after": {m: after.get(m) for m in rows},
    })


def _consume(session: Session, current: str, token: str) -> dict | None:
    """Take the record out of ``debt_undo`` in one conditional write (like goals): two Undos
    at the same time can't both use it."""
    raw = spending.get_setting(session, UNDO_SETTING)
    if raw is None:
        return None
    records = _undo_records(session, current)
    rec = records.pop(token, None)
    if rec is None:
        return None
    items = list(records.items())[-MAX_UNDO:]
    where = (AppSetting.key == UNDO_SETTING, AppSetting.value == raw)
    if items:
        stmt = sql_update(AppSetting).where(*where).values(value=json.dumps(dict(items)))
    else:
        stmt = sql_delete(AppSetting).where(*where)
    result = session.execute(stmt.execution_options(synchronize_session=False))
    for obj in list(session.identity_map.values()):
        if isinstance(obj, AppSetting) and obj.key == UNDO_SETTING:
            session.expire(obj)
    return rec if result.rowcount == 1 else None


def _state_tuple(value: Any) -> tuple[bool, tuple[int, bool, bool, int] | None]:
    return goal_links._row_state(value)


def _rows(value: Any) -> dict[str, tuple[int, bool, bool, int] | None] | None:
    if not isinstance(value, dict) or len(value) > 24:
        return None
    out: dict[str, tuple[int, bool, bool, int] | None] = {}
    for month, state_ in value.items():
        ok, parsed = _state_tuple(state_)
        if goal_links.valid_month(month) is None or not ok:
            return None
        out[month] = parsed
    return out


def _unused(session: Session, category: str) -> bool:
    """Nothing but this plan uses the category (so the Undo of making it can delete it)."""
    checks = (
        select(Transaction.id).where(Transaction.category == category),
        select(TransactionSplit.id).where(TransactionSplit.category == category),
        select(Budget.id).where(Budget.category == category),
        select(Rule.id).where(Rule.category == category),
        select(RecurringItem.id).where(RecurringItem.category_id == category),
    )
    return all(session.scalar(q.limit(1)) is None for q in checks)


def _drop_group(session: Session, group_id: Any) -> None:
    """The "Other and saving" group the first add made, when nothing is in it any more."""
    if not isinstance(group_id, int) or isinstance(group_id, bool):
        return
    group = session.get(CategoryGroup, group_id)
    if group is not None and session.scalar(select(TxnCategory.id).where(TxnCategory.group_id == group_id).limit(1)) is None:
        session.delete(group)


def undo(session: Session, today: dt.date, token: str) -> None:
    """Undo a PUT or DELETE: this month only, once, while the category's Budget rows are as
    that change left them. The rows go back exactly (``spending.put_back_rows``), and so do
    the setting and the category (a category the add made is deleted again when nothing uses
    it). 409 and nothing changes otherwise."""
    current = month_of(today)
    rec = _consume(session, current, token)
    if rec is None:
        raise DebtBudgetError(409, NO_UNDO)
    category = rec.get("category")
    before, after = _rows(rec.get("rows")), _rows(rec.get("after"))
    setting = rec.get("setting")
    if not isinstance(category, str) or before is None or after is None or not (setting is None or isinstance(setting, str)):
        raise DebtBudgetError(409, NO_UNDO)
    if any(m < current for m in before):
        raise DebtBudgetError(409, NEW_MONTH)
    cmap = load_categories(session)
    if not goal_links.valid_category(cmap, category):
        raise DebtBudgetError(409, "This can't be undone any more: its Budget category is gone.")
    for month, state_ in after.items():
        if spending.row_state(session, month, category) != state_:
            raise DebtBudgetError(409, CHANGED)
    try:
        spending.put_back_rows(session, cmap, today, category, before)
    except spending.BudgetError:
        raise DebtBudgetError(409, CHANGED) from None
    spending.put_setting(session, SETTING, setting)
    row = session.get(TxnCategory, category)
    if rec.get("created") is True:
        if _unused(session, category):
            session.delete(row)
            session.flush()
            _drop_group(session, rec.get("group"))
        else:
            row.hidden = True
    else:
        row.hidden = rec.get("hidden") is True
    session.flush()


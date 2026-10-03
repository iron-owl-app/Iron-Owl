"""Transaction categories: lookups, effective-id resolution, and unknown Plaid primaries."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import AppSetting, TxnCategory
from ..seeds import CATEGORY_KINDS, OTHER, fnv1a_hue, title_case, valid_primary
from ..utils import from_cents

KIND_ORDER = {k: i for i, k in enumerate(CATEGORY_KINDS)}
TARGET_KINDS = ("monthly", "by_date", "bills")


@dataclass(frozen=True)
class Target:
    """``bills`` (Release 3.6 phase 2): no stored amount (``cents`` None); what it needs each
    month is that month's bill total for the category (services/calendar.py occurrences)."""

    kind: str  # monthly | by_date | bills
    cents: int | None
    date: dt.date | None


@dataclass(frozen=True)
class CatInfo:
    id: str
    name: str
    hue: int
    kind: str
    hidden: bool
    position: int = 0
    group_id: int | None = None
    target: Target | None = None
    # Only categories the user made can be deleted (routers/categories.py delete_category).
    custom: bool = False


def target_of(c: TxnCategory) -> Target | None:
    """The category's budget target, if it has a complete one (spending kind only)."""
    if c.kind != "spending" or c.target_kind not in TARGET_KINDS:
        return None
    if c.target_kind == "bills":
        return Target("bills", None, None)
    if not c.target_cents or c.target_cents <= 0:
        return None
    if c.target_kind == "by_date":
        if c.target_date is None:
            return None
        return Target("by_date", int(c.target_cents), c.target_date)
    return Target("monthly", int(c.target_cents), None)


def target_out(target: Target | None, bills_cents: int | None = None) -> dict | None:
    """``CategoryTarget``. A ``bills`` target's amount is the month's bill total when the
    caller knows it (the budget month), else null (``/api/categories``)."""
    if target is None:
        return None
    amount = bills_cents if target.kind == "bills" else target.cents
    return {
        "kind": target.kind,
        "amount": from_cents(amount) if amount is not None else None,
        "date": target.date.isoformat() if target.date else None,
    }


class CategoryMap:
    """Every category by id; a NULL or unknown id resolves to OTHER."""

    def __init__(self, rows: list[TxnCategory]) -> None:
        self.by_id = {
            r.id: CatInfo(
                r.id, r.name, int(r.hue), r.kind, bool(r.hidden), int(r.position or 0),
                r.group_id, target_of(r), bool(r.custom),
            )
            for r in rows
        }
        self.other = self.by_id.get(OTHER) or CatInfo(OTHER, "Other", 0, "spending", False)

    def get(self, category_id: str | None) -> CatInfo:
        if category_id is None:
            return self.other
        return self.by_id.get(category_id) or self.other

    def effective_id(self, category_id: str | None) -> str:
        return self.get(category_id).id

    def kind(self, category_id: str | None) -> str:
        return self.get(category_id).kind

    def ids_of_kind(self, kind: str) -> set[str]:
        return {c.id for c in self.by_id.values() if c.kind == kind}

    def ordered(self, kind: str | None = None) -> list[CatInfo]:
        cats = [c for c in self.by_id.values() if kind is None or c.kind == kind]
        return sorted(cats, key=lambda c: (KIND_ORDER.get(c.kind, 99), c.position, c.name.lower()))

    def sort_key(self, category_id: str) -> tuple:
        c = self.get(category_id)
        return (KIND_ORDER.get(c.kind, 99), c.position, c.name.lower())


def load(session: Session) -> CategoryMap:
    return CategoryMap(list(session.scalars(select(TxnCategory))))


def category_out(c: TxnCategory) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "hue": int(c.hue),
        "kind": c.kind,
        "custom": bool(c.custom),
        "hidden": bool(c.hidden),
        "group_id": c.group_id,
        "target": target_out(target_of(c)),
    }


def next_position(session: Session) -> int:
    current = session.scalar(select(func.max(TxnCategory.position)))
    return 0 if current is None else int(current) + 1


# ------------------------------------------------------------------ names and custom ids
# Shared by POST /api/categories, POST /api/budgets/{month}/categories and the budget setup.

CUSTOM_SEQ = "custom_category_seq"


class EmptyName(ValueError):
    """An empty category name (422); the message is safe to show."""


class NameTaken(ValueError):
    """A category with this name already exists (409); the message is safe to show."""


def clean_name(name: str | None) -> str:
    cleaned = " ".join((name or "").split())
    if not cleaned:
        raise EmptyName("name must not be empty")
    return cleaned


def find_by_name(session: Session, name: str, exclude_id: str | None = None) -> TxnCategory | None:
    """The category with this name, ignoring case (full Unicode, not just ASCII)."""
    query = select(TxnCategory).where(func.lower(TxnCategory.name) == name.lower())
    if exclude_id is not None:
        query = query.where(TxnCategory.id != exclude_id)
    # SQLite's lower() only folds ASCII; compare in Python as well for full Unicode.
    return session.scalar(query) or next(
        (c for c in session.scalars(select(TxnCategory))
         if c.id != exclude_id and c.name.casefold() == name.casefold()),
        None,
    )


def ensure_unique(session: Session, name: str, exclude_id: str | None = None) -> None:
    if find_by_name(session, name, exclude_id) is not None:
        raise NameTaken(f"A category named “{name}” already exists.")


def custom_seq(session: Session) -> int:
    """The highest custom-category number issued so far (0 when none): every ``c_<n>`` ever
    handed out has ``n`` at most this."""
    setting = session.get(AppSetting, CUSTOM_SEQ)
    highest = 0
    for cid in session.scalars(select(TxnCategory.id).where(TxnCategory.id.like("c\\_%", escape="\\"))):
        suffix = cid[2:]
        if suffix.isdigit():
            highest = max(highest, int(suffix))
    stored = int(setting.value) if setting is not None and (setting.value or "").isdigit() else 0
    return max(highest, stored)


def issued_custom_id(session: Session, category_id: str) -> bool:
    """``category_id`` is a ``c_<n>`` this vault has already handed out (Undo of a delete
    may only bring back one of those, never claim an id the counter hasn't reached)."""
    if not category_id.startswith("c_"):
        return False
    suffix = category_id[2:]
    if not (suffix.isascii() and suffix.isdigit()) or suffix != str(int(suffix)):
        return False
    return 1 <= int(suffix) <= custom_seq(session)


def next_custom_id(session: Session) -> str:
    """c_<n> from a counter that never goes back, so a deleted id is never reused."""
    setting = session.get(AppSetting, CUSTOM_SEQ)
    n = custom_seq(session) + 1
    if setting is None:
        session.add(AppSetting(key=CUSTOM_SEQ, value=str(n)))
    else:
        setting.value = str(n)
    return f"c_{n}"


def create_custom(session: Session, name: str, kind: str, hue: int | None = None, group_id: int | None = None) -> TxnCategory:
    """Insert a custom category (name already cleaned and checked unique); flushes."""
    category = TxnCategory(
        id=next_custom_id(session),
        name=name,
        hue=hue if hue is not None else fnv1a_hue(name),
        kind=kind,
        custom=True,
        hidden=False,
        position=next_position(session),
        group_id=group_id,
    )
    session.add(category)
    session.flush()
    return category


def ensure_primary(session: Session, primary: str | None, known: set[str]) -> str | None:
    """Insert an unknown Plaid primary as a spending category; returns the id to store.

    Values that don't look like a Plaid primary are dropped (the transaction becomes OTHER).
    """
    primary = valid_primary(primary)
    if primary is None:
        return None
    if primary not in known:
        if session.get(TxnCategory, primary) is None:
            session.add(
                TxnCategory(
                    id=primary,
                    name=title_case(primary),
                    hue=fnv1a_hue(primary),
                    kind="spending",
                    custom=False,
                    hidden=False,
                    position=next_position(session),
                )
            )
            session.flush()
        known.add(primary)
    return primary

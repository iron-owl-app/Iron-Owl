"""Goals (D8) that live in the Budget (SPEC "Goals (D8) and Investments (D9) redesign").

A goal is linked to one spending category: ``app_settings.goal_links`` =
``{"<goal_id>": {"category": "<id>", "seed_month": "YYYY-MM"|null, "prior"?: {"name", "hidden",
"savings", "added"?}}}`` (no migration). ``prior`` is set when the goal took over a category that
existed before it (the Budget's Emergency savings): deleting the goal hands that category back as
it was. ``prior.added`` = how adding the goal changed that category's budgets rows, so the Goals
page's Undo of the add can put them back exactly (``AddedRows``).
This module has no dependency on ``spending`` so the Budget math can use it; the goal
routes live in ``services/goals.py``.

- **saved** = max(0, available(c, M) − max(0, assigned(c, M))): the envelope without this
  month's plan (month end needs no job).
- **plan** for a month = min(monthly, max(0, target − saved)) (0 once reached).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AppSetting, Goal
from ..utils import from_cents, parse_month
from .categories import CategoryMap

GOAL_LINKS_SETTING = "goal_links"
GOAL_KINDS = ("emergency", "save")
MAX_LINKS = 1000
_ID_MAX = 2**63 - 1


_MAX_CENTS = 10**15


@dataclass(frozen=True)
class RowChange:
    """One budgets row of the taken-over category before and right after the goal was added."""

    month: str
    before: tuple[int, bool, bool, int] | None  # (limit, removed, restart, moved)
    after: tuple[int, bool, bool, int] | None


@dataclass(frozen=True)
class AddedRows:
    """What adding a goal that took over a category did to the Budget (``prior.added``):
    ``month`` of the add, the category's rows it changed (last month's "Already saved" row and
    this month's plan) and ``taken`` (cents it took from Not planned yet). The Goals page's Undo
    of the add puts those rows back exactly, in that month, while they are unchanged."""

    month: str
    rows: tuple[RowChange, ...]
    taken: int

    def to_json(self) -> dict:
        return {
            "month": self.month,
            "taken": self.taken,
            "rows": [
                {"month": r.month, "before": list(r.before) if r.before else None,
                 "after": list(r.after) if r.after else None}
                for r in self.rows
            ],
        }


def _cents(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and abs(value) <= _MAX_CENTS


def _row_state(value: Any) -> tuple[bool, tuple[int, bool, bool, int] | None]:
    """(valid, state) for a stored row state: [limit, removed, restart, moved]; states stored
    before Release 3.17 have no ``moved`` (0)."""
    if value is None:
        return True, None
    if (
        isinstance(value, list) and len(value) in (3, 4)
        and _cents(value[0]) and isinstance(value[1], bool) and isinstance(value[2], bool)
        and (len(value) == 3 or _cents(value[3]))
    ):
        return True, (value[0], value[1], value[2], value[3] if len(value) == 4 else 0)
    return False, None


def load_added(value: Any) -> AddedRows | None:
    if not isinstance(value, dict):
        return None
    month, taken, rows = valid_month(value.get("month")), value.get("taken"), value.get("rows")
    if month is None or not isinstance(taken, int) or isinstance(taken, bool) or abs(taken) > _MAX_CENTS:
        return None
    if not isinstance(rows, list) or not 1 <= len(rows) <= 2:
        return None
    changes = []
    for row in rows:
        if not isinstance(row, dict) or valid_month(row.get("month")) is None:
            return None
        ok_before, before = _row_state(row.get("before"))
        ok_after, after = _row_state(row.get("after"))
        if not ok_before or not ok_after:
            return None
        changes.append(RowChange(row["month"], before, after))
    return AddedRows(month, tuple(changes), taken)


@dataclass(frozen=True)
class Prior:
    """A taken-over category as it was before the goal: its name, hidden flag, whether it
    was the Budget's savings category (``budget_savings_category``) and, for the add's Undo,
    how adding the goal changed its budgets rows (``added``)."""

    name: str
    hidden: bool = False
    savings: bool = False
    added: AddedRows | None = None

    def to_json(self) -> dict:
        out: dict[str, Any] = {"name": self.name, "hidden": self.hidden, "savings": self.savings}
        if self.added is not None:
            out["added"] = self.added.to_json()
        return out


@dataclass(frozen=True)
class Link:
    category: str
    seed_month: str | None = None
    prior: Prior | None = None


def load_prior(value: Any) -> Prior | None:
    if not isinstance(value, dict):
        return None
    name = value.get("name")
    if not isinstance(name, str) or not 1 <= len(name) <= 200:
        return None
    return Prior(name, value.get("hidden") is True, value.get("savings") is True, load_added(value.get("added")))


def valid_month(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parse_month(value)
    except ValueError:
        return None
    return value


def load_links(session: Session) -> dict[int, Link]:
    """The stored links (malformed entries are ignored, never an error)."""
    row = session.get(AppSetting, GOAL_LINKS_SETTING)
    if row is None or not row.value:
        return {}
    try:
        data = json.loads(row.value)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[int, Link] = {}
    for key, value in list(data.items())[:MAX_LINKS]:
        # isdecimal + ASCII: "²" passes isdigit() but int() refuses it.
        if not isinstance(key, str) or not key.isascii() or not key.isdecimal() or not isinstance(value, dict):
            continue
        try:
            goal_id = int(key)
        except ValueError:
            continue
        category = value.get("category")
        if not 1 <= goal_id <= _ID_MAX or not isinstance(category, str) or not 1 <= len(category) <= 64:
            continue
        out[goal_id] = Link(category, valid_month(value.get("seed_month")), load_prior(value.get("prior")))
    return out


def store_links(session: Session, links: dict[int, Link]) -> None:
    def entry(link: Link) -> dict:
        out: dict[str, Any] = {"category": link.category, "seed_month": link.seed_month}
        if link.prior is not None:
            out["prior"] = link.prior.to_json()
        return out

    value = json.dumps({str(k): entry(v) for k, v in sorted(links.items())})
    row = session.get(AppSetting, GOAL_LINKS_SETTING)
    if not links:
        if row is not None:
            session.delete(row)
    elif row is None:
        session.add(AppSetting(key=GOAL_LINKS_SETTING, value=value))
    else:
        row.value = value
    session.flush()


def goals(session: Session) -> list[Goal]:
    """The goals the Goals page shows (debt goals stay in the database, untouched)."""
    return list(session.scalars(
        select(Goal).where(Goal.kind.in_(GOAL_KINDS)).order_by(Goal.created_at, Goal.id)
    ))


def valid_category(cmap: CategoryMap, category: str | None) -> bool:
    return category is not None and category in cmap.by_id and cmap.kind(category) == "spending"


def linked(session: Session, cmap: CategoryMap, rows: list[Goal] | None = None) -> dict[int, str]:
    """goal id -> category for every live link (the goal exists and isn't a debt goal, the
    category exists and is for spending; one goal per category, the oldest wins)."""
    links = load_links(session)
    out: dict[int, str] = {}
    taken: set[str] = set()
    for goal in goals(session) if rows is None else rows:
        link = links.get(goal.id)
        if link is None or not valid_category(cmap, link.category) or link.category in taken:
            continue
        out[goal.id] = link.category
        taken.add(link.category)
    return out


def goal_categories(session: Session, cmap: CategoryMap) -> dict[str, Goal]:
    """category -> its goal (live links only)."""
    rows = goals(session)
    by_id = {g.id: g for g in rows}
    return {category: by_id[goal_id] for goal_id, category in linked(session, cmap, rows).items()}


def saved_cents(line: Any) -> int:
    """What's saved in an envelope line, not counting the month's own plan (0 without one)."""
    if line is None:
        return 0
    return max(0, int(line.available) - max(0, int(line.assigned)))


def plan_cents(goal: Goal, saved: int) -> int:
    return min(max(0, int(goal.monthly_cents or 0)), max(0, int(goal.target_cents or 0) - saved))


def reached(goal: Goal, saved: int) -> bool:
    return int(goal.target_cents or 0) > 0 and saved >= int(goal.target_cents)


def goal_ref(goal: Goal, line: Any) -> dict:
    """``EnvelopeLine.goal``. ``plan``: what the goal's rule plans for that month
    (min(monthly, target − saved); 0 once reached), for the Budget's quick fills."""
    saved = saved_cents(line)
    return {
        "id": goal.id,
        "kind": goal.kind,
        "saved": from_cents(saved),
        "target": from_cents(int(goal.target_cents or 0)),
        "reached": reached(goal, saved),
        "plan": from_cents(plan_cents(goal, saved)),
    }


def seeded_categories(session: Session, month: str) -> set[str]:
    """Categories whose goal's "Already saved" row is in ``month``."""
    return {link.category for link in load_links(session).values() if link.seed_month == month}


def resolve_savings(by_category: dict[str, Goal], lines_now: dict, fallback: str | None) -> str | None:
    """``BudgetMonth.savings_category``: the emergency goal's category → the Budget's savings
    category (not yet taken over) → the first unfinished goal in this month's Budget → None."""
    for category, goal in by_category.items():
        if goal.kind == "emergency":
            return category
    if fallback is not None:
        return fallback
    for category, goal in by_category.items():  # oldest goal first (``goal_categories``)
        line = lines_now.get(category)
        if line is not None and not reached(goal, saved_cents(line)):
            return category
    return None

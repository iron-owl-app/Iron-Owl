"""Goals (D8, Release 3.8): goals that live in the Budget.

SPEC "Goals (D8) and Investments (D9) redesign". A goal is linked to one spending category
(``services/goal_links.py``); its money is that envelope. Every Budget write goes through
``spending.save_budget_cents`` so the Release 2.1 rules (over-assign, move-out, later months)
still apply; the router commits or rolls back the whole request.
"""
from __future__ import annotations

import datetime as dt
import json
import secrets
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select
from sqlalchemy import update as sql_update
from sqlalchemy.orm import Session

from ..models import Account, AppSetting, Budget, CategoryGroup, Goal, Transaction, TransactionSplit, TxnCategory
from ..seeds import fnv1a_hue
from ..utils import (
    LIABILITY_CATEGORIES, fmt_usd, from_cents, month_bounds, month_of, parse_month, shift_month, to_cents,
)
from . import budget_setup, spending
from . import categories as cat_service
from . import goal_links
from .categories import CategoryMap
from .categories import load as load_categories
from .goal_links import AddedRows, Link, Prior, RowChange, plan_cents, reached, saved_cents

NOT_ENOUGH = "not_enough_unplanned"
GOAL_GROUP = "Other and saving"
DUE_AHEAD = 24  # a "save" goal's month: next month .. 24 months ahead
NEEDED_STEP = 500  # "Set aside $N": rounded up to $5
EMERGENCY_MONTHS = 3
EMERGENCY_ROUND = 10_000  # rounded up to $100
LAST_YEAR = 9999
NAME_MAX = 60
SETUP_FIRST = "Set up your Budget first."
NO_UNDO = "This can't be undone any more."
NEW_MONTH = "This can't be undone any more: a new month has started."
# Categories that goals made and left when they were deleted (hidden; a new goal with the same
# name may reuse one). A list of category ids.
RETIRED_SETTING = "retired_goal_categories"
MAX_RETIRED = 1000
# Recent deletes' Undo records, by token (this month only): POST /api/goals/restore puts back
# exactly what DELETE did, once.
UNDO_SETTING = "goal_undo"
MAX_UNDO = 20


class GoalError(Exception):
    """A refusal the router turns into ``{"detail", **extra}`` with ``status``."""

    def __init__(self, status: int, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.extra = extra

    def body(self) -> dict:
        return {"detail": self.detail, **self.extra}


def _not_enough(not_planned: int, detail: str | None = None, **extra: Any) -> GoalError:
    free = max(0, not_planned)
    return GoalError(
        422,
        detail or (
            f"Only {fmt_usd(free)} isn't planned yet this month. Use a smaller amount, "
            "or take money from another category on the Budget page."
        ),
        code=NOT_ENOUGH,
        not_planned=from_cents(free),
        **extra,
    )


# ------------------------------------------------------------------ months


def _index(month: str) -> int:
    year, mon = parse_month(month)
    return year * 12 + mon - 1


def _first_day(month: str) -> dt.date:
    return month_bounds(month)[0]


def _later(month: str, months: int) -> str | None:
    """``month`` + ``months``, or None past year 9999 (for all practical purposes, never)."""
    if _index(month) + months > LAST_YEAR * 12 + 11:
        return None
    return shift_month(month, months)


def check_due(kind: str, due: str | None, current: str) -> str | None:
    """A new due month: "save" goals need one from next month to 24 months ahead;
    an emergency fund has none."""
    if kind == "emergency":
        if due is not None:
            raise GoalError(422, "An emergency fund has no date.")
        return None
    if due is None:
        raise GoalError(422, "Pick the month you need the money by.")
    try:
        parse_month(due)
    except ValueError:
        raise GoalError(422, "due_month must be YYYY-MM") from None
    first, last = shift_month(current, 1), shift_month(current, DUE_AHEAD)
    if not first <= due <= last:
        raise GoalError(422, f"Pick a month from {first} to {last}.")
    return due


# ------------------------------------------------------------------ context


@dataclass
class Ctx:
    cmap: CategoryMap
    book: spending.Book
    current: str
    rows: list[Goal]
    live: dict[int, str]  # goal id -> category (live links only)

    @property
    def lines(self) -> dict:
        return self.book.members(self.current)

    @property
    def ready(self) -> int:
        return self.book.ready_to_assign()

    def emergency_goal(self) -> Goal | None:
        return next((g for g in self.rows if g.kind == "emergency"), None)


def _ctx(session: Session, today: dt.date) -> Ctx:
    cmap = load_categories(session)
    current = month_of(today)
    # Loaded from last month on, so "Already saved" can look at last month's envelopes.
    book = spending.load_book(session, cmap, today, shift_month(current, -1))
    rows = goal_links.goals(session)
    return Ctx(cmap, book, current, rows, goal_links.linked(session, cmap, rows))


def _account_label(account: Account | None) -> str | None:
    if account is None:
        return None
    return f"{account.name} ··{account.mask}" if account.mask else account.name


# ------------------------------------------------------------------ GoalsState


def lowest_plan(line: Any) -> int:
    """The lowest this month's plan goes when a goal's monthly amount or target is lowered:
    only this month's own plan moves out, and never more than the envelope still has, so it
    never ends up below $0 and what's saved stays (SPEC "This month's plan")."""
    assigned = int(line.assigned)
    return assigned - min(max(0, assigned), max(0, int(line.available)))


def goal_out(goal: Goal, category: str | None, line: Any, account: Account | None, current: str) -> dict:
    target = int(goal.target_cents or 0)
    monthly = max(0, int(goal.monthly_cents or 0))
    saved = saved_cents(line)
    due = month_of(goal.target_date) if goal.kind == "save" and goal.target_date else None
    # This month counts; a due month that has passed (an old goal) leaves this month.
    months = max(1, _index(due) - _index(current) + 1) if due is not None else None
    done = reached(goal, saved)
    remaining = max(0, target - saved)
    if months is not None:
        projected = min(target, saved + monthly * months)
    else:
        projected = target if monthly > 0 else min(target, saved)
    if done:
        status = "reached"
    elif monthly == 0:
        status = "no_plan"
    elif months is not None and projected < target:
        status = "behind"
    else:
        status = "on_track"
    projected_month = None
    if not done and monthly > 0 and remaining > 0:  # an old goal's target of 0 has no month
        # This month's plan is month 1.
        projected_month = _later(current, -(-remaining // monthly) - 1)
    needed = None
    if months is not None and not done:
        per_month = -(-remaining // months)
        needed = -(-per_month // NEEDED_STEP) * NEEDED_STEP
    return {
        "id": goal.id,
        "kind": goal.kind,
        "name": goal.name,
        "category_id": category,
        "in_budget": line is not None,
        "target": from_cents(target),
        "due_month": due,
        "monthly": from_cents(monthly),
        "planned_this_month": from_cents(int(line.assigned) if line is not None else 0),
        # The Edit window's preview uses the same floor as ``shift_plan`` (null: not in the Budget).
        "lowest_plan": from_cents(lowest_plan(line)) if line is not None else None,
        "saved": from_cents(saved),
        "account_id": goal.account_id if account is not None else None,
        "account_label": _account_label(account),
        "status": status,
        "months_to_save": months,
        "projected": from_cents(projected),
        "projected_month": projected_month,
        "short_by": from_cents(target - projected) if status == "behind" else None,
        "needed_monthly": from_cents(needed),
    }


def _emergency_basis(session: Session, ctx: Ctx, today: dt.date) -> tuple[str | None, int]:
    """("bills", this month's bill total) from the calendar, as the setup's Bills row; else
    ("spending", the last 3 complete months' spending + fixed, per month); else (None, 0)."""
    bills = spending.month_bills(session, ctx.current, today, ctx.book) or {}
    total = sum(
        spending.bill_total(occs) for category, occs in bills.items()
        if category in ctx.cmap.by_id and ctx.cmap.kind(category) == "spending" and not ctx.cmap.get(category).hidden
    )
    if total > 0:
        return "bills", total
    spent = 0
    for month in spending.last_complete_months(today, EMERGENCY_MONTHS):
        t = spending.month_totals(session, ctx.cmap, month)
        spent += t.total_spent + t.total_fixed
    if spent > 0:
        return "spending", -(-spent // EMERGENCY_MONTHS)
    return None, 0


def _round_up(cents: int, step: int) -> int:
    return -(-cents // step) * step


def state(session: Session, today: dt.date) -> dict:
    ctx = _ctx(session, today)
    lines = ctx.lines
    accounts = {a.id: a for a in session.scalars(select(Account))}
    goals = []
    for goal in ctx.rows:
        category = ctx.live.get(goal.id)
        line = lines.get(category) if category is not None else None
        goals.append(goal_out(goal, category, line, accounts.get(goal.account_id), ctx.current))

    basis = suggestion = basis_monthly = available = None
    if ctx.emergency_goal() is None:
        basis, monthly = _emergency_basis(session, ctx, today)
        if basis is not None:
            suggestion = _round_up(EMERGENCY_MONTHS * monthly, EMERGENCY_ROUND)
            basis_monthly = monthly
        savings = _free_savings(session, ctx)
        if savings is not None:
            available = saved_cents(lines.get(savings))
    ready = ctx.ready
    return {
        "month": ctx.current,
        "month_money": from_cents(ready + ctx.book.planned(ctx.current)),
        "not_planned": from_cents(ready),
        "monthly_total": from_cents(sum(
            max(0, int(lines[c].assigned)) for c in ctx.live.values() if c in lines
        )),
        "budget_ready": not budget_setup.is_needed(session),
        "emergency_suggestion": from_cents(suggestion),
        "emergency_basis": basis,
        "emergency_monthly": from_cents(basis_monthly),
        "emergency_available": from_cents(available),
        "goals": goals,
    }


HOME_TOP = 3  # Home's goals card: up to 3 unfinished goals


@dataclass(frozen=True)
class _LineCents:
    """The two envelope numbers ``goal_out`` reads, from a ``BudgetMonth`` line (dollars)."""

    available: int
    assigned: int


def summary(session: Session, today: dt.date, bm: dict | None = None) -> dict:
    """Home v2's goals tile and card, light: ``{budget_ready, total_saved, in_progress, top}``.

    ``bm`` = ``spending.budget_month`` for today's month when the caller already built it
    (Home does), so the envelopes aren't computed twice. Each goal's numbers are exactly
    ``GoalsState``'s (``goal_out`` on the same line: this month's member line of its live
    category, none when unlinked or not in this month's Budget). ``top``: the first
    ``HOME_TOP`` unfinished goals, oldest first; ``in_progress`` counts every unfinished one.
    """
    current = month_of(today)
    if bm is None or bm.get("month") != current:
        bm = spending.budget_month(session, current, today)
    lines: dict[int, dict] = {}
    for line in bm["categories"]:
        goal = line.get("goal")
        if goal is not None:
            lines[int(goal["id"])] = line
    total = 0
    unfinished: list[dict] = []
    for goal in goal_links.goals(session):
        line = lines.get(goal.id)
        cents = None if line is None else _LineCents(to_cents(line["available"]), to_cents(line["assigned"]))
        out = goal_out(goal, None, cents, None, current)
        total += to_cents(out["saved"])
        if out["status"] != "reached":
            unfinished.append(out)
    return {
        "budget_ready": not bm["setup_needed"],
        "total_saved": from_cents(total),
        "in_progress": len(unfinished),
        "top": [
            {"id": g["id"], "name": g["name"], "saved": g["saved"], "target": g["target"], "status": g["status"]}
            for g in unfinished[:HOME_TOP]
        ],
    }


# ------------------------------------------------------------------ helpers for writes


def clean_name(name: str, limit: int = NAME_MAX) -> str:
    try:
        cleaned = cat_service.clean_name(name)
    except cat_service.EmptyName:
        raise GoalError(422, "Give the goal a name.") from None
    if len(cleaned) > limit:
        raise GoalError(422, f"Use a name of {NAME_MAX} characters or fewer.")
    return cleaned


def _unique(session: Session, name: str, exclude: str | None = None) -> None:
    try:
        cat_service.ensure_unique(session, name, exclude)
    except cat_service.NameTaken as exc:
        raise GoalError(409, f"{exc} Pick another name for the goal.") from None


def check_account(session: Session, account_id: int | None) -> None:
    """"Keep the money in" is only a label: any existing account that holds money."""
    if account_id is None:
        return
    account = session.get(Account, account_id)
    if account is None or account.category in LIABILITY_CATEGORIES:
        raise GoalError(422, "unknown account")


def _goal_group(session: Session, cmap: CategoryMap, savings: str | None) -> int:
    """"Other and saving" (ignoring case) → the savings category's group → a new
    "Other and saving" group."""
    for group in session.scalars(select(CategoryGroup).order_by(CategoryGroup.position, CategoryGroup.id)):
        if group.name.casefold() == GOAL_GROUP.casefold():
            return group.id
    group_id = cmap.get(savings).group_id if savings is not None else None
    if group_id is not None and session.get(CategoryGroup, group_id) is not None:
        return int(group_id)
    position = session.scalar(select(func.max(CategoryGroup.position)))
    group = CategoryGroup(name=GOAL_GROUP, position=0 if position is None else int(position) + 1)
    session.add(group)
    session.flush()
    return group.id


def _free_savings(session: Session, ctx: Ctx) -> str | None:
    """The Budget's Emergency savings category when no goal has taken it over."""
    savings = spending.savings_category(session, ctx.cmap)
    return savings if savings is not None and savings not in ctx.live.values() else None


def _emergency_source(session: Session, ctx: Ctx, exclude: str | None) -> str | None:
    """Where a short "Already saved" comes from: the emergency goal's category, else the
    Budget's Emergency savings category (never the goal's own category)."""
    emergency = ctx.emergency_goal()
    category = ctx.live.get(emergency.id) if emergency is not None else None
    if category is None:
        category = _free_savings(session, ctx)
    return category if category != exclude else None


def _write_seed(session: Session, ctx: Ctx, category: str, cents: int) -> str:
    """Last month's row so this month's envelope starts ``cents`` higher ("Money you already
    had"), like the setup's seed. Returns that month."""
    previous = shift_month(ctx.current, -1)
    line = ctx.book.members(previous).get(category)
    row = session.scalar(select(Budget).where(Budget.month == previous, Budget.category == category))
    if line is not None:
        # What the month planned: its own row, or (Release 3.17) the plan it repeated.
        base = int(line.assigned)
        value = base + cents + max(0, -line.available)
    else:
        value = cents + max(0, -ctx.book.net.get(previous, {}).get(category, 0))
    if row is None:
        session.add(Budget(month=previous, category=category, limit_cents=value))
    else:
        if line is None:
            # Removed before: it starts again here, with nothing carried in.
            row.removed, row.restart = False, True
        row.limit_cents = value
    session.flush()
    return previous




def _store_link(session: Session, goal_id: int, link: Link | None) -> None:
    links = goal_links.load_links(session)
    if link is None:
        links.pop(goal_id, None)
    else:
        links[goal_id] = link
    # Links to goals or categories that no longer exist are dropped on every write.
    existing_goals = set(session.scalars(select(Goal.id)))
    existing_cats = set(session.scalars(select(TxnCategory.id)))
    links = {k: v for k, v in links.items() if k in existing_goals and v.category in existing_cats}
    goal_links.store_links(session, links)


def _json_setting(session: Session, key: str) -> Any:
    raw = spending.get_setting(session, key)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _retired(session: Session) -> list[str]:
    data = _json_setting(session, RETIRED_SETTING)
    if not isinstance(data, list):
        return []
    return [c for c in data[:MAX_RETIRED] if isinstance(c, str) and 1 <= len(c) <= 64]


def _set_retired(session: Session, category: str, retired: bool) -> None:
    ids = [c for c in _retired(session) if c != category]
    if retired:
        ids.append(category)
    existing = set(session.scalars(select(TxnCategory.id)))
    ids = [c for c in ids if c in existing][-MAX_RETIRED:]
    spending.put_setting(session, RETIRED_SETTING, json.dumps(ids) if ids else None)


def _undo_records(session: Session, current: str) -> dict[str, dict]:
    data = _json_setting(session, UNDO_SETTING)
    if not isinstance(data, dict):
        return {}
    return {
        k: v for k, v in data.items()
        if isinstance(k, str) and isinstance(v, dict) and v.get("month") == current
    }


def _store_undo_records(session: Session, records: dict[str, dict]) -> None:
    items = list(records.items())[-MAX_UNDO:]
    spending.put_setting(session, UNDO_SETTING, json.dumps(dict(items)) if items else None)


# ------------------------------------------------------------------ attach (create / link)


def attach(session: Session, today: dt.date, goal: Goal, already_saved: int) -> None:
    """Give ``goal`` (flushed, not linked yet) its Budget category, "Already saved" and this
    month's plan. Emergency fund: takes over the Budget's Emergency savings category
    (renamed to the goal's name; its name, hidden flag and savings role are kept in the link
    so deleting the goal hands it back) or makes one and remembers it."""
    if budget_setup.is_needed(session):
        raise GoalError(409, SETUP_FIRST)
    clean_name(goal.name)  # an old goal's name may be too long for a category
    ctx = _ctx(session, today)
    current, lines = ctx.current, ctx.lines
    _check_old_category(session, ctx, goal)
    takeover = _free_savings(session, ctx) if goal.kind == "emergency" else None
    prior: Prior | None = None
    if takeover is not None:
        row = session.get(TxnCategory, takeover)
        prior = Prior(row.name, bool(row.hidden), True)
    else:
        takeover = _hidden_twin(session, ctx, goal.name, already_saved)
    _unique(session, goal.name, exclude=takeover)

    # This month's plan first; "Already saved" then comes from what's left of Not planned
    # yet, then from the Emergency fund's plan this month (the window says so).
    line = lines.get(takeover) if takeover is not None else None
    if line is not None:
        # What's in it now (money moved out this month already came off), plus Already saved.
        saved_after = max(0, int(line.available) - max(0, int(line.assigned)) + already_saved)
    else:
        saved_after = already_saved
    plan = plan_cents(goal, saved_after)
    # Money moved out of it this month stays moved out (its plan starts next month).
    new_assigned = int(line.assigned) if line is not None and int(line.assigned) < 0 else plan
    ready = ctx.ready
    delta = new_assigned - (int(line.assigned) if line is not None else 0)
    if delta > max(0, ready):
        raise _not_enough(ready)
    left = ready - delta
    rest = already_saved - min(already_saved, max(0, left))
    source = None
    if rest > 0:
        source = _emergency_source(session, ctx, exclude=takeover)
        source_line = lines.get(source) if source is not None else None
        takeable = 0
        if source_line is not None:
            takeable = max(0, min(int(source_line.available), int(source_line.carryover) + int(source_line.assigned)))
        if rest > takeable:
            fund = f", and {ctx.cmap.get(source).name} has {fmt_usd(takeable)}" if source_line is not None else ""
            raise _not_enough(
                left,
                f"Only {fmt_usd(max(0, left))} isn't planned yet this month{fund}. "
                "Use a smaller amount for Already saved.",
                emergency_available=from_cents(takeable),
            )

    # Validated: write. A takeover remembers the category's rows as they were (last month's
    # "Already saved" row, this month's plan) so the page's Undo of the add can put them back.
    tracked = (shift_month(current, -1), current)
    rows_before = {m: spending.row_state(session, m, takeover) for m in tracked} if prior is not None else {}
    if takeover is not None:
        category = session.get(TxnCategory, takeover)
        category.name, category.hidden = goal.name, False
        category_id = takeover
        if prior is None:
            _set_retired(session, takeover, False)  # a deleted goal's category, reused
    else:
        group_id = _goal_group(session, ctx.cmap, spending.savings_category(session, ctx.cmap))
        category_id = cat_service.create_custom(session, goal.name, "spending", group_id=group_id).id
    session.flush()
    seed_month = _write_seed(session, ctx, category_id, already_saved) if already_saved > 0 else None
    cents = {category_id: new_assigned}
    moved: dict[str, int] = {}
    if rest > 0 and source is not None:
        cents[source] = int(lines[source].assigned) - rest
        # Release 3.17: taken from the fund this month only (a one-time move), so later months
        # still repeat its plan.
        moved[source] = int(lines[source].moved) - rest
    cmap = load_categories(session)
    try:
        spending.save_budget_cents(session, cmap, current, cents, [], today, moved=moved)
    except spending.BudgetError as exc:
        raise GoalError(422, str(exc)) from None
    if goal.kind == "emergency":
        spending.put_setting(session, spending.BUDGET_SAVINGS_SETTING, category_id)
    book_after = spending.load_book(session, cmap, today)
    if prior is not None:
        changes = tuple(RowChange(m, rows_before[m], spending.row_state(session, m, category_id)) for m in tracked)
        prior = replace(prior, added=AddedRows(current, changes, ready - book_after.ready_to_assign()))
    _store_link(session, goal.id, Link(category_id, seed_month, prior))

    # Safety net: the envelope must hold exactly what the goal says is saved.
    after = book_after.members(current).get(category_id)
    if after is None or saved_cents(after) != saved_after:
        raise GoalError(
            422, "Iron Owl couldn't set that money aside in the Budget. Try again without Already saved."
        )


def _check_old_category(session: Session, ctx: Ctx, goal: Goal) -> None:
    """A goal whose category was changed to another kind (Fixed, Income...) on the Categories
    page lost its link; that category still has the goal's name, so say how to go on."""
    old = goal_links.load_links(session).get(goal.id)
    if old is None or old.category not in ctx.cmap.by_id or goal_links.valid_category(ctx.cmap, old.category):
        return
    found = cat_service.find_by_name(session, goal.name)
    if found is not None and found.id == old.category:
        raise GoalError(
            409,
            f"“{found.name}” isn't a spending category any more, so it can't hold this goal. "
            "Give the goal another name to put it back in your Budget.",
        )


def _hidden_twin(session: Session, ctx: Ctx, name: str, already_saved: int) -> str | None:
    """A hidden category with this name that a deleted goal left behind (made by a goal, no
    goal uses it now): it is reused instead of refusing the name. Any money still in its
    envelope counts as saved; money released when it left the Budget doesn't come back.
    Any other hidden category with this name is a plain name conflict."""
    found = cat_service.find_by_name(session, name)
    if found is None or not found.hidden:
        return None  # a visible one: ``_unique`` refuses it
    reusable = (
        found.kind == "spending" and found.custom and found.id in set(_retired(session))
        and found.id not in ctx.live.values() and found.id != spending.savings_category(session, ctx.cmap)
    )
    if not reusable:
        raise GoalError(409, f"You have a hidden category named “{found.name}”. Pick another name for the goal.")
    removed_now = session.scalar(
        select(Budget.id).where(Budget.month == ctx.current, Budget.category == found.id, Budget.removed.is_(True))
    )
    if removed_now is not None and already_saved > 0:
        # Taken out of the Budget this month, it starts again with nothing carried in, so
        # last month's "Already saved" row couldn't reach it.
        raise GoalError(
            409, f"A goal named “{found.name}” was deleted this month. Pick another name, "
            "or add it without Already saved."
        )
    return found.id


def _has_emergency(session: Session) -> bool:
    return session.scalar(select(Goal.id).where(Goal.kind == "emergency").limit(1)) is not None


def create(
    session: Session, today: dt.date, *, kind: str, name: str, target: int, due_month: str | None,
    monthly: int, already_saved: int, account_id: int | None,
) -> int:
    if budget_setup.is_needed(session):
        raise GoalError(409, SETUP_FIRST)
    name = clean_name(name)
    due = check_due(kind, due_month, month_of(today))
    check_account(session, account_id)
    if kind == "emergency" and _has_emergency(session):
        raise GoalError(409, "You already have an emergency fund.")
    goal = Goal(
        name=name, kind=kind, account_id=account_id, target_cents=target, start_cents=0, current_cents=0,
        monthly_cents=monthly, apr=None, target_date=_first_day(due) if due else None, hue=fnv1a_hue(name),
    )
    session.add(goal)
    session.flush()
    attach(session, today, goal, already_saved)
    return goal.id


# ------------------------------------------------------------------ edit


def get_goal(session: Session, goal_id: int) -> Goal:
    goal = session.get(Goal, goal_id)
    if goal is None or goal.kind not in goal_links.GOAL_KINDS:
        raise GoalError(404, "goal not found")
    return goal


def _save_plan(session: Session, ctx: Ctx, today: dt.date, category: str, cents: int) -> None:
    try:
        spending.save_budget_cents(session, ctx.cmap, ctx.current, {category: cents}, [], today)
    except spending.BudgetError as exc:
        raise GoalError(422, str(exc)) from None


def write_plan(session: Session, today: dt.date, goal: Goal, category: str) -> None:
    """Put a goal (back) into this month's Budget: its plan = min(monthly, target − saved);
    refused past Not planned yet."""
    ctx = _ctx(session, today)
    line = ctx.lines.get(category)
    plan = plan_cents(goal, saved_cents(line))
    delta = plan - (int(line.assigned) if line is not None else 0)
    ready = ctx.ready
    if delta > max(0, ready):
        raise _not_enough(ready)
    _save_plan(session, ctx, today, category, plan)


def shift_plan(
    session: Session, today: dt.date, goal: Goal, category: str, old_monthly: int, old_target: int
) -> None:
    """A monthly or target edit changes this month's plan by the difference between the new
    and the old rule's plan (same saved), so money the user moved in or out on the Budget page
    ("Use …", "Put it in …", Move money) stays as they left it."""
    ctx = _ctx(session, today)
    line = ctx.lines.get(category)
    if line is None:
        write_plan(session, today, goal, category)
        return
    saved = saved_cents(line)
    old = min(max(0, old_monthly), max(0, old_target - saved))
    delta = plan_cents(goal, saved) - old
    if delta == 0:
        return
    ready = ctx.ready
    if spending.row_state(session, ctx.current, category) is None:
        # Release 3.17: this month has no row of its own, so it already plans the edited goal's
        # rule; work from the plan it had before the edit (and the money not planned then).
        ready += int(line.assigned) - old
        line = replace(line, assigned=old)
    if delta > max(0, ready):
        raise _not_enough(ready)
    assigned = int(line.assigned)
    # A lower plan only takes back this month's own plan, and never more than the envelope
    # still has: it never goes below $0 and never lowers what's saved (``lowest_plan``).
    value = assigned + delta if delta > 0 else max(assigned + delta, lowest_plan(line))
    if value != assigned:
        _save_plan(session, ctx, today, category, value)


def restore_plan(session: Session, today: dt.date, goal: Goal, category: str, cents: int) -> None:
    """The page's Undo of an edit: this month's plan goes back to exactly what it was, but
    never below ``lowest_plan`` (money spent since the edit stays covered: the envelope never
    ends below $0 and Saved never drops); below it → 409 and nothing changes."""
    ctx = _ctx(session, today)
    line = ctx.lines.get(category)
    delta = cents - (int(line.assigned) if line is not None else 0)
    if delta == 0:
        return
    if delta < 0 and line is not None and cents < lowest_plan(line):
        raise GoalError(
            409,
            f"This can't be undone any more: money from {goal.name} was spent since. "
            "You can change the monthly amount in its Edit window.",
        )
    ready = ctx.ready
    if delta > max(0, ready):
        raise _not_enough(
            ready,
            f"This can't be undone: only {fmt_usd(max(0, ready))} isn't planned yet this month. "
            "Take money from another category on the Budget page first.",
        )
    _save_plan(session, ctx, today, category, cents)


def update(session: Session, today: dt.date, goal_id: int, fields: dict[str, Any]) -> None:
    """``fields``: name, target (cents), due_month, monthly (cents), account_id,
    already_saved (cents; only on the PATCH that links an old goal) and plan_before
    ((month, cents): the page's Undo of an edit puts this month's plan back exactly)."""
    goal = get_goal(session, goal_id)
    current = month_of(today)
    plan_before = fields.pop("plan_before", None)
    if plan_before is not None and plan_before[0] != current:
        raise GoalError(409, NEW_MONTH)
    cmap = load_categories(session)
    category = goal_links.linked(session, cmap).get(goal.id)
    if "already_saved" in fields and category is not None:
        raise GoalError(422, "Already saved can't change once the goal is in your Budget.")
    old_monthly, old_target = int(goal.monthly_cents or 0), int(goal.target_cents or 0)
    if "name" in fields:
        name = clean_name(fields["name"])
        if category is not None and cmap.get(category).name != name:
            _unique(session, name, exclude=category)
            session.get(TxnCategory, category).name = name
        goal.name = name
    if "target" in fields:
        goal.target_cents = int(fields["target"])
    if "due_month" in fields:
        stored = month_of(goal.target_date) if goal.kind == "save" and goal.target_date else None
        if fields["due_month"] is None or fields["due_month"] != stored:
            due = check_due(goal.kind, fields["due_month"], current)
            goal.target_date = _first_day(due) if due else None
    if "monthly" in fields:
        goal.monthly_cents = int(fields["monthly"])
    if "account_id" in fields:
        check_account(session, fields["account_id"])
        goal.account_id = fields["account_id"]
    session.flush()
    if category is None:
        # Saving an old goal (or one whose category is gone) links it: like a new goal, it
        # needs a month (save goals) and an amount.
        if plan_before is not None:
            raise GoalError(409, f"This can't be undone any more: {goal.name} isn't in your Budget now.")
        if goal.kind == "save" and goal.target_date is None:
            raise GoalError(422, "Pick the month you need the money by.")
        if int(goal.target_cents or 0) <= 0:
            raise GoalError(422, "Type how much you want saved.")
        attach(session, today, goal, int(fields.get("already_saved") or 0))
        return
    in_budget = category in spending.load_book(session, cmap, today).members(current)
    if plan_before is not None:
        if not in_budget:
            raise GoalError(409, f"This can't be undone any more: {goal.name} isn't in your Budget now.")
        restore_plan(session, today, goal, category, plan_before[1])
    elif not in_budget:
        write_plan(session, today, goal, category)  # removed on the Budget page: any PATCH puts it back
    elif "monthly" in fields or "target" in fields:
        shift_plan(session, today, goal, category, old_monthly, old_target)


# ------------------------------------------------------------------ delete / restore


def _has_transactions(session: Session, category: str) -> bool:
    txn = session.scalar(select(Transaction.id).where(Transaction.category == category).limit(1))
    split = session.scalar(select(TransactionSplit.id).where(TransactionSplit.category == category).limit(1))
    return txn is not None or split is not None


def _hand_back(session: Session, category: str, prior: Prior, in_budget: bool) -> str:
    """A taken-over category goes back to what it was: its old name (unless another category
    has taken it since), its hidden flag and its role as the Budget's savings category. It
    stays in the Budget with its money. Returns its name."""
    row = session.get(TxnCategory, category)
    if row.name != prior.name and cat_service.find_by_name(session, prior.name, exclude_id=category) is None:
        row.name = prior.name
    row.hidden = prior.hidden and not in_budget
    setting = spending.get_setting(session, spending.BUDGET_SAVINGS_SETTING)
    if prior.savings and setting in (None, category):
        spending.put_setting(session, spending.BUDGET_SAVINGS_SETTING, category)
    elif not prior.savings and setting == category:
        spending.put_setting(session, spending.BUDGET_SAVINGS_SETTING, None)
    session.flush()
    return row.name


def delete(session: Session, today: dt.date, goal_id: int, *, record: bool = True) -> tuple[dict, str | None]:
    """Delete a goal and its link. A category the goal made leaves this month's Budget (its
    leftover goes back to Not planned yet; overspending comes out of it) and is hidden if
    nothing uses it. A category the goal took over is handed back as it was, money and all.
    Returns (the ``undo`` for ``restore``, the handed-back category's name or None).
    ``record`` False keeps no Undo record (the add's own Undo, ``undo_add``)."""
    goal = get_goal(session, goal_id)
    ctx = _ctx(session, today)
    category = ctx.live.get(goal.id)
    link = goal_links.load_links(session).get(goal.id)
    prior = link.prior if link is not None and category is not None else None
    line = ctx.lines.get(category) if category is not None else None
    assigned = int(line.assigned) if line is not None else 0
    released = 0
    removed = False
    kept_in = None
    if category is not None:
        if prior is not None:
            kept_in = _hand_back(session, category, prior, in_budget=line is not None)
        else:
            if line is not None:
                released = int(line.available)
                try:
                    spending.save_budget_cents(
                        session, ctx.cmap, ctx.current, {}, [category], today, release_overspent=True
                    )
                except spending.BudgetError as exc:
                    raise GoalError(422, f"Iron Owl couldn't take {goal.name} out of your Budget. {exc}") from None
                removed = True
            if not _has_transactions(session, category):
                session.get(TxnCategory, category).hidden = True
            _set_retired(session, category, True)
            if spending.get_setting(session, spending.BUDGET_SAVINGS_SETTING) == category:
                spending.put_setting(session, spending.BUDGET_SAVINGS_SETTING, None)
    due = month_of(goal.target_date) if goal.kind == "save" and goal.target_date else None
    seed_month = link.seed_month if link is not None and category is not None else None
    token = secrets.token_urlsafe(12)
    undo = {
        "goal": {
            "kind": goal.kind,
            "name": goal.name,
            "target": from_cents(int(goal.target_cents or 0)),
            "due_month": due,
            "monthly": from_cents(int(goal.monthly_cents or 0)),
            "account_id": goal.account_id,
        },
        "category_id": category,
        "month": ctx.current,
        "assigned": from_cents(assigned),
        "released": from_cents(released),
        "seed_month": seed_month,
        "removed": removed,
        "token": token,
    }
    if record:
        # What restore trusts: the server's own record (the body only names it).
        records = _undo_records(session, ctx.current)
        records[token] = {
            "month": ctx.current,
            "category": category,
            "goal": {
                "kind": goal.kind, "name": goal.name, "target": int(goal.target_cents or 0), "due_month": due,
                "monthly": int(goal.monthly_cents or 0), "account_id": goal.account_id,
            },
            "assigned": assigned,
            "seed_month": seed_month,
            "removed": removed,
            "prior": prior.to_json() if prior is not None else None,
        }
        _store_undo_records(session, records)
    session.delete(goal)
    session.flush()
    _store_link(session, goal_id, None)
    return undo, kept_in


UNDO_ADD_CHANGED = (
    "This can't be undone any more: {name} changed on the Budget page since. "
    "You can still delete the goal in its Edit window."
)


def undo_add(session: Session, today: dt.date, goal_id: int) -> tuple[str | None, int]:
    """The Goals page's Undo right after adding a goal. Returns (the handed-back category's
    name or None, cents that went back to Not planned yet).

    A goal that made its own category is deleted like Delete does (its money goes back to Not
    planned yet). A goal that took over a category (Emergency savings) puts that category's
    rows back exactly as they were before the add (``prior.added``), so Not planned yet and
    the category are what they were, and hands the category back. Only in the month of the
    add and while those rows are as the add left them; otherwise 409 and nothing changes."""
    goal = get_goal(session, goal_id)
    ctx = _ctx(session, today)
    category = ctx.live.get(goal.id)
    link = goal_links.load_links(session).get(goal.id)
    prior = link.prior if link is not None and category is not None else None
    if prior is None:
        undo, _ = delete(session, today, goal_id, record=False)
        return None, to_cents(undo["released"])
    added = prior.added
    if added is None or added.month != ctx.current:
        why = NEW_MONTH if added is not None else NO_UNDO
        raise GoalError(409, f"{why} You can still delete the goal in its Edit window.")
    changed = UNDO_ADD_CHANGED.format(name=ctx.cmap.get(category).name)
    if any(spending.row_state(session, r.month, category) != r.after for r in added.rows):
        raise GoalError(409, changed)
    ready_before = ctx.ready
    line_before = ctx.lines.get(category)
    available_before = int(line_before.available) if line_before is not None else 0
    try:
        spending.put_back_rows(session, ctx.cmap, today, category, {r.month: r.before for r in added.rows})
    except spending.BudgetError:
        raise GoalError(409, changed) from None
    book = spending.load_book(session, ctx.cmap, today)
    line_after = book.members(ctx.current).get(category)
    # Money spent from it since the add stays covered: the put-back may not leave the
    # envelope overspent, or more overspent than it is now (the router rolls back the rows).
    if line_after is not None and int(line_after.available) < min(0, available_before):
        raise GoalError(409, changed)
    kept_in = _hand_back(session, category, prior, in_budget=category in book.members(ctx.current))
    session.delete(goal)
    session.flush()
    _store_link(session, goal_id, None)
    return kept_in, book.ready_to_assign() - ready_before


def _consume_undo(session: Session, current: str, token: str) -> dict | None:
    """Take the delete's record out of ``goal_undo`` in one conditional write: it only
    succeeds if the stored value is still the one read here, so two restores at the same
    time can't both use it (the second finds the value changed and gets None)."""
    raw = spending.get_setting(session, UNDO_SETTING)
    records = _undo_records(session, current) if raw is not None else {}
    rec = records.pop(token, None)
    if rec is None or raw is None:
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


def _stored_int(value: Any, low: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < low:
        raise GoalError(409, NO_UNDO)
    return value


def restore(session: Session, today: dt.date, *, token: str, month: str, category: str | None) -> int:
    """Undo a delete: in the same month only, once, from the record the delete kept."""
    current = month_of(today)
    if month != current:
        raise GoalError(409, NEW_MONTH)
    # Used up by this write (a refusal below rolls it back); the first write of the request.
    rec = _consume_undo(session, current, token)
    if rec is None or rec.get("category") != category:
        raise GoalError(409, NO_UNDO)
    g = rec.get("goal") if isinstance(rec.get("goal"), dict) else {}
    kind = g.get("kind")
    if kind not in goal_links.GOAL_KINDS or not isinstance(g.get("name"), str):
        raise GoalError(409, NO_UNDO)
    target, monthly = _stored_int(g.get("target")), _stored_int(g.get("monthly"))
    assigned = _stored_int(rec.get("assigned"), -spending.MAX_AMOUNT_CENTS)
    due = goal_links.valid_month(g.get("due_month")) if kind == "save" else None
    seed_month = goal_links.valid_month(rec.get("seed_month"))
    account_id = g.get("account_id") if isinstance(g.get("account_id"), int) else None
    prior = goal_links.load_prior(rec.get("prior"))
    if category is not None and budget_setup.is_needed(session):
        raise GoalError(409, SETUP_FIRST)
    name = clean_name(g["name"], NAME_MAX if category is not None else 200)
    if kind == "emergency" and _has_emergency(session):
        raise GoalError(409, "You already have an emergency fund.")
    if account_id is not None and session.get(Account, account_id) is None:
        account_id = None  # deleted since: the goal just has no "Keep the money in"
    check_account(session, account_id)
    cmap = load_categories(session)
    row = session.get(TxnCategory, category) if category is not None else None
    if category is not None:
        # A goal-made category is a custom one; a taken-over one may be any spending category.
        if not goal_links.valid_category(cmap, category) or row is None or (prior is None and not row.custom):
            raise GoalError(409, "This can't be undone any more: its Budget category is gone.")
        if category in goal_links.linked(session, cmap).values():
            raise GoalError(409, "This can't be undone any more: another goal uses its Budget category.")
        _unique(session, name, exclude=category)
    goal = Goal(
        name=name, kind=kind, account_id=account_id, target_cents=target, start_cents=0, current_cents=0,
        monthly_cents=monthly, apr=None, target_date=_first_day(due) if due else None, hue=fnv1a_hue(name),
    )
    session.add(goal)
    session.flush()
    if category is None:
        return goal.id
    row.name, row.hidden = name, False
    if prior is None:
        _set_retired(session, category, False)
        removed = session.scalar(
            select(Budget.id).where(Budget.month == current, Budget.category == category, Budget.removed.is_(True))
        )
        if rec.get("removed") is True and removed is not None:
            # Only the delete's own removal is taken back: the envelope continues with what it had.
            try:
                spending.save_budget_cents(session, cmap, current, {category: assigned}, [], today, restore=True)
            except spending.BudgetError as exc:
                raise GoalError(422, str(exc)) from None
    if kind == "emergency" and spending.savings_category(session, cmap) in (None, category):
        spending.put_setting(session, spending.BUDGET_SAVINGS_SETTING, category)
    session.flush()
    _store_link(session, goal.id, Link(category, seed_month, prior))
    return goal.id

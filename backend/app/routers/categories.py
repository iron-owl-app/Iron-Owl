from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from ..deps import freeze_first, get_db
from ..models import Budget, CategoryGroup, RecurringItem, Rule, Transaction, TransactionSplit, TxnCategory
from ..schemas import CategoryCreate, CategoryPatch3, CategorySnapshot, TargetBody, non_null
from ..seeds import OTHER
from ..services import automap, debt_budget, goal_links, spending
from ..services import calendar as cal
from ..services import categories as cat_service
from ..services import rules as rules_service
from ..services.categories import KIND_ORDER, category_out, target_of, target_out
from ..utils import from_cents, to_cents, today
from .rules import rule_out

router = APIRouter(prefix="/api/categories", tags=["categories"])


def _get(db: Session, category_id: str) -> TxnCategory:
    category = db.get(TxnCategory, category_id)
    if category is None:
        raise HTTPException(status_code=404, detail="category not found")
    return category


def _clean_name(name: str | None) -> str:
    try:
        return cat_service.clean_name(name)
    except cat_service.EmptyName as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


def _goal_guard(db: Session, category: TxnCategory, action: str) -> None:
    """A category that holds a goal (Goals D8, including an Emergency savings category a goal
    took over) can't be deleted, hidden or made a non-spending kind here: the goal would lose
    its money's home. 409 with where to go instead. The same holds for the Budget's "Paying
    off debt" category (Release 3.10) while the debt plan uses it."""
    cmap = cat_service.load(db)
    if category.id not in goal_links.goal_categories(db, cmap):
        if category.id == debt_budget.linked_category(db, cmap):
            _debt_guard(category, action)
        return
    if action == "delete":
        detail = f"“{category.name}” holds your goal. Delete the goal on the Goals page."
    elif action == "kind":
        detail = (
            f"“{category.name}” holds your goal, so it stays a spending category. "
            "To change it, delete the goal on the Goals page."
        )
    else:
        detail = (
            f"“{category.name}” holds your goal, so it stays on the Budget page. "
            "To remove it, delete the goal on the Goals page."
        )
    raise HTTPException(status_code=409, detail=detail)


def _debt_guard(category: TxnCategory, action: str) -> None:
    where = "Take it out on Reports › Paying off debt"
    if action == "delete":
        detail = f"“{category.name}” holds your plan for paying off debt. {where} first."
    elif action == "kind":
        detail = f"“{category.name}” holds your plan for paying off debt, so it stays a spending category. {where} first."
    else:
        detail = f"“{category.name}” holds your plan for paying off debt, so it stays on the Budget page. {where} first."
    raise HTTPException(status_code=409, detail=detail)


def _ensure_unique(db: Session, name: str, exclude_id: str | None = None) -> None:
    try:
        cat_service.ensure_unique(db, name, exclude_id)
    except cat_service.NameTaken as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.get("")
def list_categories(db: Session = Depends(get_db)) -> list[dict]:
    rows = list(db.scalars(select(TxnCategory)))
    rows.sort(key=lambda c: (KIND_ORDER.get(c.kind, 99), c.position, c.name.lower()))
    return [category_out(c) for c in rows]


@router.post("", status_code=201)
def create_category(body: CategoryCreate, db: Session = Depends(get_db)) -> dict:
    name = _clean_name(body.name)
    _ensure_unique(db, name)
    if body.group_id is not None and db.get(CategoryGroup, body.group_id) is None:
        raise HTTPException(status_code=422, detail="unknown group")
    category = cat_service.create_custom(db, name, body.kind, body.hue, group_id=body.group_id)
    db.commit()
    return category_out(category)


@router.patch("/{category_id}")
def update_category(category_id: str, body: CategoryPatch3, db: Session = Depends(get_db)) -> dict:
    freeze_first(db)  # Release 3.17: ended months keep their goal and debt plans
    category = _get(db, category_id)
    if bad := non_null(body, ("name", "hue", "kind", "hidden", "restore")):
        raise HTTPException(status_code=422, detail=f"{bad} must not be null")
    fields = body.model_fields_set
    if "hidden" in fields and body.hidden and not category.hidden:
        _goal_guard(db, category, "hide")
    if "kind" in fields and category.kind == "spending" and body.kind != "spending":
        _goal_guard(db, category, "kind")
    if "name" in fields:
        name = _clean_name(body.name)
        _ensure_unique(db, name, exclude_id=category.id)
        category.name = name
    if "hue" in fields:
        category.hue = body.hue
    # Phase 2: the budget's automatic sorting only fills visible spending categories.
    resort = False
    if "kind" in fields:
        resort |= category.kind != body.kind
        category.kind = body.kind
    if "hidden" in fields:
        resort |= category.hidden != bool(body.hidden)
        category.hidden = bool(body.hidden)
    if "group_id" in fields:
        if body.group_id is not None and db.get(CategoryGroup, body.group_id) is None:
            raise HTTPException(status_code=422, detail="unknown group")
        category.group_id = body.group_id
    if body.restore and "target" not in fields:
        raise HTTPException(status_code=422, detail="Undo needs the target to put back.")
    if "target" in fields:
        _set_target(category, body.target, restore=bool(body.restore))
    elif category.kind != "spending":
        # Only spending categories have targets; changing the kind clears it.
        _set_target(category, None)
    if resort and category.id in automap.stored(db).values():
        # A sorted-into category hidden or no longer for spending: its automatically sorted
        # transactions go back to the bank's category (automatic again, so "Needs a category"
        # sees them); shown again or back to spending, the sorting fills it again. Same commit.
        db.flush()
        rules_service.apply_all(db)
    db.commit()
    return category_out(category)


def _set_target(category: TxnCategory, target: TargetBody | None, *, restore: bool = False) -> None:
    """``restore``: Undo putting back an earlier target, so a by-date target's date may have
    passed since (every other rule still applies)."""
    if target is None:
        category.target_kind = category.target_cents = category.target_date = None
        return
    if category.kind != "spending":
        raise HTTPException(status_code=422, detail="Only spending categories can have a target.")
    if target.kind == "bills":
        # Release 3.6 phase 2: "Cover my bills" — the amount is each month's bill total.
        if target.amount is not None:
            raise HTTPException(status_code=422, detail="A bills target has no amount: it follows your bills each month.")
        if target.date is not None:
            raise HTTPException(status_code=422, detail="A bills target has no date.")
        category.target_kind, category.target_cents, category.target_date = "bills", None, None
        return
    if target.amount is None:
        raise HTTPException(status_code=422, detail="The target amount must be more than $0.")
    cents = to_cents(target.amount)
    if cents <= 0:
        raise HTTPException(status_code=422, detail="The target amount must be more than $0.")
    if target.kind == "by_date":
        if target.date is None:
            raise HTTPException(status_code=422, detail="Pick the date you need the money by.")
        if target.date < today() and not restore:
            raise HTTPException(status_code=422, detail="The target date must be today or later.")
        category.target_date = target.date
    else:
        if target.date is not None:
            raise HTTPException(status_code=422, detail="A monthly target has no date.")
        category.target_date = None
    category.target_kind = target.kind
    category.target_cents = cents


@router.delete("/{category_id}", status_code=204)
def delete_category(category_id: str, db: Session = Depends(get_db)) -> Response:
    freeze_first(db)  # Release 3.17: ended months keep their goal and debt plans
    category = _get(db, category_id)
    _goal_guard(db, category, "delete")
    if not category.custom:
        raise HTTPException(status_code=400, detail="only custom categories can be deleted")
    # Settings (D7): the rules that put purchases in it go too (Undo restores them from
    # GET /{id}/snapshot); the others keep their order, renumbered 0..n-1.
    db.execute(delete(Rule).where(Rule.category == category_id))
    db.flush()
    for position, rule in enumerate(db.scalars(select(Rule).order_by(Rule.position, Rule.id))):
        rule.position = position
    # Its transactions go back to automatic (rules, then Plaid); its budgets go away.
    db.execute(
        update(Transaction)
        .where(Transaction.category == category_id)
        .values(category_source="plaid", rule_id=None)
    )
    db.execute(delete(Budget).where(Budget.category == category_id))
    # Split parts can't lose their category: they move to Other.
    db.execute(update(TransactionSplit).where(TransactionSplit.category == category_id).values(category=OTHER))
    db.delete(category)
    db.flush()
    rules_service.apply_all(db)
    db.commit()
    return Response(status_code=204)


# ------------------------------------------------------------------ snapshot / restore (Undo, Settings D7)

# Bounded IN (...) lists: SQLite's host-parameter limit.
_CHUNK = 500


def _chunks(ids: list) -> list[list]:
    return [ids[i:i + _CHUNK] for i in range(0, len(ids), _CHUNK)]


def _flags(db: Session, category_id: str) -> dict:
    auto_key = next((k for k, v in automap.stored(db).items() if v == category_id), None)
    return {
        "savings_category": spending.get_setting(db, spending.BUDGET_SAVINGS_SETTING) == category_id,
        "auto_map_key": auto_key,
        "excluded_plan": category_id in cal.excluded_plans(db),
    }


def _snapshot(db: Session, category: TxnCategory) -> dict:
    cid = category.id
    counts = dict(db.execute(
        select(Transaction.rule_id, func.count()).where(Transaction.rule_id.is_not(None)).group_by(Transaction.rule_id)
    ).all())
    rules = db.scalars(select(Rule).where(Rule.category == cid).order_by(Rule.position, Rule.id))
    return {
        "category": {
            "id": cid,
            "name": category.name,
            "hue": int(category.hue),
            "kind": category.kind,
            "custom": bool(category.custom),
            "hidden": bool(category.hidden),
            "position": int(category.position or 0),
            "group_id": category.group_id,
            "target": target_out(target_of(category)),
        },
        "hand_set": [
            {"transaction_id": tid, "source": source}
            for tid, source in db.execute(
                select(Transaction.id, Transaction.category_source)
                .where(Transaction.category == cid, Transaction.category_source.in_(rules_service.USER_SOURCES))
                .order_by(Transaction.id)
            )
        ],
        "splits": list(db.scalars(
            select(TransactionSplit.id).where(TransactionSplit.category == cid).order_by(TransactionSplit.id)
        )),
        "budgets": [
            {
                "month": b.month, "assigned": from_cents(b.limit_cents), "removed": bool(b.removed),
                "restart": bool(b.restart), "moved": from_cents(int(b.moved_cents or 0)),
            }
            for b in db.scalars(select(Budget).where(Budget.category == cid).order_by(Budget.month))
        ],
        "bills": list(db.scalars(
            select(RecurringItem.id).where(RecurringItem.category_id == cid).order_by(RecurringItem.id)
        )),
        "rules": [rule_out(r, counts.get(r.id, 0)) for r in rules],
        "flags": _flags(db, cid),
    }


@router.get("/{category_id}/snapshot")
def get_snapshot(category_id: str, db: Session = Depends(get_db)) -> dict:
    """Everything ``DELETE`` changes, for Undo (``POST /restore``)."""
    return _snapshot(db, _get(db, category_id))


@router.post("/restore", status_code=201)
def restore_category(body: CategorySnapshot, db: Session = Depends(get_db)) -> dict:
    """Undo a delete: put the category back with what still exists of its purchases set by
    hand, split parts, budgets, bills, rules (at their positions) and settings."""
    freeze_first(db)  # Release 3.17: ended months keep their goal and debt plans
    s = body.category
    # Validate everything before writing anything.
    name = _clean_name(s.name)
    if db.get(TxnCategory, s.id) is not None:
        raise HTTPException(status_code=409, detail="This category exists again. Undo isn't available.")
    if not cat_service.issued_custom_id(db, s.id):
        # Only an id the counter has already handed out: a made-up c_<n> above it would be
        # taken by a later new category's id.
        raise HTTPException(status_code=422, detail="unknown category id")
    _ensure_unique(db, name)
    months = [b.month for b in body.budgets]
    if len(set(months)) != len(months):
        raise HTTPException(status_code=422, detail="budgets has the same month twice")
    rule_ids = [r.id for r in body.rules]
    if len(set(rule_ids)) != len(rule_ids):
        raise HTTPException(status_code=422, detail="rules has the same id twice")
    for r in body.rules:
        if r.category != s.id:
            raise HTTPException(status_code=422, detail="A rule in the snapshot is for another category.")
        if not r.text.strip():
            raise HTTPException(status_code=422, detail="text must not be empty")
        if r.amount_op is not None and r.amount is None:
            raise HTTPException(status_code=422, detail="amount is required with amount_op")
    group_id = s.group_id if s.group_id is not None and db.get(CategoryGroup, s.group_id) is not None else None
    category = TxnCategory(
        id=s.id, name=name, hue=s.hue, kind=s.kind, custom=True, hidden=s.hidden,
        position=s.position, group_id=group_id,
    )
    if s.target is not None:
        _set_target(category, s.target, restore=True)  # 422s before anything is added

    db.add(category)
    db.flush()
    cid = category.id
    # Purchases set by hand that nobody has set by hand to something else since (Release
    # 3.10: never a credit card payment into "Paying off debt").
    cards = (
        debt_budget.card_payment_ids(db, [h.transaction_id for h in body.hand_set])
        if debt_budget.filing_blocked(db, cid) else set()
    )
    by_source: dict[str, list[int]] = {}
    for h in body.hand_set:
        if h.transaction_id not in cards:
            by_source.setdefault(h.source, []).append(h.transaction_id)
    for source, ids in by_source.items():
        for chunk in _chunks(sorted(set(ids))):
            db.execute(
                update(Transaction)
                .where(Transaction.id.in_(chunk), Transaction.category_source.not_in(rules_service.USER_SOURCES))
                .values(category=cid, category_source=source, rule_id=None)
            )
    # Split parts moved to Other (and not changed since) come back.
    for chunk in _chunks(sorted(set(body.splits))):
        db.execute(
            update(TransactionSplit)
            .where(TransactionSplit.id.in_(chunk), TransactionSplit.category == OTHER)
            .values(category=cid)
        )
    for b in body.budgets:
        db.add(Budget(
            month=b.month, category=cid, limit_cents=to_cents(b.assigned), removed=b.removed, restart=b.restart,
            moved_cents=to_cents(b.moved),
        ))
    # Bills that lost their category (ON DELETE SET NULL) and weren't given another since.
    for chunk in _chunks(sorted(set(body.bills))):
        db.execute(
            update(RecurringItem)
            .where(RecurringItem.id.in_(chunk), RecurringItem.category_id.is_(None))
            .values(category_id=cid)
        )
    # Rules at their old positions (lowest first, so each lands where it was).
    max_rule_id = db.scalar(select(func.max(Rule.id))) or 0
    for r in sorted(body.rules, key=lambda r: (r.position, r.id)):
        rule = Rule(
            field=r.field, op=r.op, text=r.text.strip(), amount_op=r.amount_op,
            amount_cents=to_cents(r.amount) if r.amount_op is not None and r.amount is not None else None,
            action="category", category=cid, enabled=r.enabled,
        )
        # Its old id when still free and below the highest id in use (a gap left by the
        # delete); any other id the client sends is ignored and SQLite picks the next one.
        if r.id <= max_rule_id and db.get(Rule, r.id) is None:
            rule.id = r.id
        rule.position = rules_service.make_room(db, r.position)
        db.add(rule)
        db.flush()
    _restore_flags(db, cid, body)
    db.flush()
    rules_service.apply_all(db)
    debt_budget.release_filed(db)  # Release 3.10: payments back under "Paying off debt" count
    db.commit()
    return category_out(category)


def _restore_flags(db: Session, cid: str, body: CategorySnapshot) -> None:
    flags = body.flags
    if flags.savings_category:
        current = spending.get_setting(db, spending.BUDGET_SAVINGS_SETTING)
        if not current or db.get(TxnCategory, current) is None:
            spending.put_setting(db, spending.BUDGET_SAVINGS_SETTING, cid)
    if flags.auto_map_key is not None:
        rows = automap.stored(db)
        current = rows.get(flags.auto_map_key)
        if current is None or db.get(TxnCategory, current) is None:
            rows[flags.auto_map_key] = cid
            automap.record(db, rows)
    if flags.excluded_plan:
        excluded = cal.excluded_plans(db)
        if cid not in excluded and len(excluded) < cal.MAX_EXCLUDED_PLANS:
            cal.save_settings(db, excluded=[*excluded, cid])

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..deps import get_db
from ..models import Rule, Transaction, TxnCategory
from ..schemas import ReorderBody, RuleInput, RulePatch, RulePreviewBody, non_null
from ..services import categories as cat_service
from ..services import rules as rules_service
from ..utils import from_cents, to_cents

router = APIRouter(prefix="/api/rules", tags=["rules"])


def _get(db: Session, rule_id: int) -> Rule:
    rule = db.get(Rule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="rule not found")
    return rule


def _normalize(db: Session, rule: Rule) -> None:
    """Validate the combined rule state (after a create or patch)."""
    rule.text = rule.text.strip()
    if not rule.text:
        raise HTTPException(status_code=422, detail="text must not be empty")
    if rule.action == "category":
        if not rule.category:
            raise HTTPException(status_code=422, detail="A category rule needs a category.")
        if db.get(TxnCategory, rule.category) is None:
            raise HTTPException(status_code=422, detail="unknown category")
    else:
        rule.category = None  # a transfer rule leaves the category alone
    if rule.amount_op is None:
        rule.amount_cents = None
    elif rule.amount_cents is None:
        raise HTTPException(status_code=422, detail="amount is required with amount_op")


def _match_counts(db: Session) -> dict[int, int]:
    rows = db.execute(
        select(Transaction.rule_id, func.count()).where(Transaction.rule_id.is_not(None)).group_by(Transaction.rule_id)
    ).all()
    return {rule_id: count for rule_id, count in rows}


def rule_out(rule: Rule, matches: int) -> dict:
    return {
        "id": rule.id,
        "position": rule.position,
        "field": rule.field,
        "op": rule.op,
        "text": rule.text,
        "amount_op": rule.amount_op,
        "amount": from_cents(rule.amount_cents),
        "action": rule.action,
        "category": rule.category,
        "enabled": bool(rule.enabled),
        "matches": matches,
    }


def _ordered(db: Session) -> list[Rule]:
    return list(db.scalars(select(Rule).order_by(Rule.position, Rule.id)))


def _list(db: Session) -> list[dict]:
    counts = _match_counts(db)
    return [rule_out(r, counts.get(r.id, 0)) for r in _ordered(db)]


def _commit_and_apply(db: Session) -> None:
    db.flush()
    rules_service.apply_all(db)
    db.commit()


def _one(db: Session, rule_id: int) -> dict:
    return rule_out(_get(db, rule_id), _match_counts(db).get(rule_id, 0))


@router.get("")
def list_rules(db: Session = Depends(get_db)) -> list[dict]:
    return _list(db)


@router.post("", status_code=201)
def create_rule(body: RuleInput, db: Session = Depends(get_db)) -> dict:
    last = db.scalar(select(func.max(Rule.position)))
    rule = Rule(
        position=0 if last is None else last + 1,
        field=body.field,
        op=body.op,
        text=body.text,
        amount_op=body.amount_op,
        amount_cents=to_cents(body.amount) if body.amount is not None else None,
        action=body.action,
        category=body.category,
        enabled=body.enabled,
    )
    _normalize(db, rule)
    # "Always put … in …" (first) and new rules from Settings go ahead of every existing rule,
    # so the user's newest choice wins; Undo of a delete puts a rule back at its position.
    wanted = 0 if body.first else body.position
    if wanted is not None:
        rule.position = rules_service.make_room(db, wanted)
    db.add(rule)
    _commit_and_apply(db)
    return _one(db, rule.id)


@router.post("/reorder")
def reorder(body: ReorderBody, db: Session = Depends(get_db)) -> list[dict]:
    rules = {r.id: r for r in _ordered(db)}
    if len(body.ids) != len(set(body.ids)) or set(body.ids) != set(rules):
        raise HTTPException(status_code=422, detail="ids must list every rule exactly once")
    for position, rule_id in enumerate(body.ids):
        rules[rule_id].position = position
    _commit_and_apply(db)
    return _list(db)


@router.post("/preview")
def preview(body: RulePreviewBody, db: Session = Depends(get_db)) -> dict:
    if body.rule_id is not None:
        _get(db, body.rule_id)  # 404 for an unknown rule
    if body.action == "category" and body.category and db.get(TxnCategory, body.category) is None:
        raise HTTPException(status_code=422, detail="unknown category")  # as create does
    matcher = rules_service.Matcher.build(
        id=body.rule_id,
        field=body.field,
        op=body.op,
        text=body.text,
        amount_op=body.amount_op,
        amount_cents=to_cents(body.amount) if body.amount is not None and body.amount_op else None,
        action=body.action,
        category=body.category,
    )
    # Settings D7 update 2: what saving it would change. A category rule without a category
    # yet, or turned off, changes nothing it would put in place.
    usable = body.enabled and (body.action != "category" or bool(body.category))
    if usable or body.rule_id is not None:
        would_change, sample = rules_service.preview_changes(
            db, matcher if usable else None, replace_id=body.rule_id,
        )
    else:
        would_change, sample = 0, []
    cmap = cat_service.load(db)

    def ref(category_id: str | None) -> dict:
        info = cmap.get(category_id)
        return {"id": info.id, "name": info.name, "hue": info.hue}

    return {
        "matches": rules_service.preview_count(db, matcher),
        "would_change": would_change,
        "sample": [
            {
                "id": row.id,
                "date": row.date.isoformat(),
                "name": row.name,
                "merchant": row.merchant_name,
                "amount": from_cents(row.amount_cents),
                "from": ref(row.category) if row.category is not None else None,
                "to": ref(want["category"]),
            }
            for row, want in sample
        ],
    }


@router.patch("/{rule_id}")
def update_rule(rule_id: int, body: RulePatch, db: Session = Depends(get_db)) -> dict:
    rule = _get(db, rule_id)
    if bad := non_null(body, ("field", "op", "text", "action", "enabled")):
        raise HTTPException(status_code=422, detail=f"{bad} must not be null")
    fields = body.model_fields_set
    for name in ("field", "op", "text", "amount_op", "action", "category", "enabled"):
        if name in fields:
            setattr(rule, name, getattr(body, name))
    if "amount" in fields:
        rule.amount_cents = to_cents(body.amount) if body.amount is not None else None
    try:
        _normalize(db, rule)
    except HTTPException:
        db.rollback()
        raise
    _commit_and_apply(db)
    return _one(db, rule_id)


@router.delete("/{rule_id}", status_code=204)
def delete_rule(rule_id: int, db: Session = Depends(get_db)) -> Response:
    db.delete(_get(db, rule_id))
    _commit_and_apply(db)
    # Keep positions dense (0..n-1).
    for position, rule in enumerate(_ordered(db)):
        rule.position = position
    db.commit()
    return Response(status_code=204)

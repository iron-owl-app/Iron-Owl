from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..deps import freeze_first, get_db
from ..models import CategoryGroup, TxnCategory
from ..schemas import GroupCreate, GroupReorder, GroupRestoreBody
from ..services.categories import KIND_ORDER

router = APIRouter(prefix="/api/category-groups", tags=["category-groups"])

MAX_GROUPS = 100

# SPEC Release 3 "Category groups": starter groups and the seeded categories they take.
STARTER_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Home", ("RENT_AND_UTILITIES", "HOME_IMPROVEMENT")),
    ("Car", ("TRANSPORTATION",)),
    ("Food", ("FOOD_AND_DRINK",)),
    ("Personal", ("PERSONAL_CARE", "ENTERTAINMENT", "GENERAL_MERCHANDISE", "TRAVEL")),
    ("Health", ("MEDICAL",)),
    ("Bills & services", ("GENERAL_SERVICES", "BANK_FEES", "GOVERNMENT_AND_NON_PROFIT")),
)


def groups_out(db: Session) -> list[dict]:
    groups = list(db.scalars(select(CategoryGroup).order_by(CategoryGroup.position, CategoryGroup.id)))
    members: dict[int, list[TxnCategory]] = {g.id: [] for g in groups}
    for c in db.scalars(select(TxnCategory).where(TxnCategory.group_id.is_not(None))):
        if c.group_id in members:
            members[c.group_id].append(c)
    out = []
    for g in groups:
        cats = sorted(members[g.id], key=lambda c: (KIND_ORDER.get(c.kind, 99), c.position, c.name.lower()))
        out.append({"id": g.id, "name": g.name, "position": int(g.position or 0), "category_ids": [c.id for c in cats]})
    return out


def _one(db: Session, group_id: int) -> dict:
    return next(g for g in groups_out(db) if g["id"] == group_id)


def _get(db: Session, group_id: int) -> CategoryGroup:
    group = db.get(CategoryGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="group not found")
    return group


def _clean(name: str | None) -> str:
    cleaned = " ".join((name or "").split())
    if not cleaned:
        raise HTTPException(status_code=422, detail="name must not be empty")
    return cleaned


def _find(db: Session, name: str) -> CategoryGroup | None:
    # The column is COLLATE NOCASE (ASCII only); compare with casefold() for full Unicode.
    return next((g for g in db.scalars(select(CategoryGroup)) if g.name.casefold() == name.casefold()), None)


def _next_position(db: Session) -> int:
    current = db.scalar(select(func.max(CategoryGroup.position)))
    return 0 if current is None else int(current) + 1


def _check_room(db: Session, adding: int = 1) -> None:
    if (db.scalar(select(func.count()).select_from(CategoryGroup)) or 0) + adding > MAX_GROUPS:
        raise HTTPException(status_code=422, detail=f"You can have at most {MAX_GROUPS} groups.")


@router.get("")
def list_groups(db: Session = Depends(get_db)) -> list[dict]:
    return groups_out(db)


@router.post("", status_code=201)
def create_group(body: GroupCreate, db: Session = Depends(get_db)) -> dict:
    name = _clean(body.name)
    if _find(db, name) is not None:
        raise HTTPException(status_code=409, detail=f"A group named “{name}” already exists.")
    _check_room(db)
    group = CategoryGroup(name=name, position=_next_position(db))
    db.add(group)
    db.commit()
    return _one(db, group.id)


# Declared before /{group_id} routes so "reorder" and "starter" are never taken for an id.
@router.post("/reorder")
def reorder(body: GroupReorder, db: Session = Depends(get_db)) -> list[dict]:
    groups = {g.id: g for g in db.scalars(select(CategoryGroup))}
    if len(body.ids) != len(set(body.ids)) or set(body.ids) != set(groups):
        raise HTTPException(status_code=422, detail="ids must list every group exactly once")
    for position, group_id in enumerate(body.ids):
        groups[group_id].position = position
    db.commit()
    return groups_out(db)


@router.post("/restore", status_code=201)
def restore_group(body: GroupRestoreBody, db: Session = Depends(get_db)) -> list[dict]:
    """Settings (D7) Undo of a group delete: put it back at ``position`` (its old id when
    still free) with the listed categories that still exist and are still ungrouped."""
    freeze_first(db)  # Release 3.17: ended months keep their goal and debt plans
    name = _clean(body.name)
    if _find(db, name) is not None:
        raise HTTPException(status_code=409, detail=f"A group named “{name}” already exists.")
    _check_room(db)
    ordered = list(db.scalars(select(CategoryGroup).order_by(CategoryGroup.position, CategoryGroup.id)))
    index = min(body.position, len(ordered))
    group = CategoryGroup(name=name, position=index)
    # Its old id only when free and below the highest id in use (a gap left by the delete);
    # any other id the client sends is ignored and SQLite picks the next one.
    max_id = max((g.id for g in ordered), default=0)
    if body.id <= max_id and db.get(CategoryGroup, body.id) is None:
        group.id = body.id
    for i, other in enumerate(ordered):
        other.position = i if i < index else i + 1
    db.add(group)
    db.flush()
    ids = sorted(set(body.category_ids))
    for start in range(0, len(ids), 500):
        db.execute(
            update(TxnCategory)
            .where(TxnCategory.id.in_(ids[start:start + 500]), TxnCategory.group_id.is_(None))
            .values(group_id=group.id)
        )
    db.commit()
    return groups_out(db)


@router.post("/starter")
def starter(db: Session = Depends(get_db)) -> list[dict]:
    """Create the missing starter groups; only currently ungrouped categories are assigned."""
    missing = [name for name, _ in STARTER_GROUPS if _find(db, name) is None]
    _check_room(db, len(missing))
    for name, category_ids in STARTER_GROUPS:
        group = _find(db, name)
        if group is None:
            group = CategoryGroup(name=name, position=_next_position(db))
            db.add(group)
            db.flush()
        db.execute(
            update(TxnCategory)
            .where(TxnCategory.id.in_(category_ids), TxnCategory.group_id.is_(None))
            .values(group_id=group.id)
        )
    db.commit()
    return groups_out(db)


@router.patch("/{group_id}")
def rename_group(group_id: int, body: GroupCreate, db: Session = Depends(get_db)) -> dict:
    group = _get(db, group_id)
    name = _clean(body.name)
    clash = _find(db, name)
    if clash is not None and clash.id != group.id:
        raise HTTPException(status_code=409, detail=f"A group named “{name}” already exists.")
    group.name = name
    db.commit()
    return _one(db, group_id)


@router.delete("/{group_id}", status_code=204)
def delete_group(group_id: int, db: Session = Depends(get_db)) -> Response:
    freeze_first(db)  # Release 3.17: ended months keep their goal and debt plans
    group = _get(db, group_id)
    # Its categories become ungrouped (explicitly, as well as ON DELETE SET NULL).
    db.execute(update(TxnCategory).where(TxnCategory.group_id == group.id).values(group_id=None))
    db.delete(group)
    db.commit()
    return Response(status_code=204)

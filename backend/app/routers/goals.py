"""Goals (D8, Release 3.8): SPEC "Goals: routes". Money in dollars, months YYYY-MM."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Path, Query
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..deps import freeze_first, get_db
from ..schemas import GoalCreateBody, GoalPatchBody, GoalRestoreBody, non_null
from ..services import goals as service
from ..utils import from_cents, to_cents, today

router = APIRouter(prefix="/api/goals", tags=["goals"])

GoalId = Path(ge=1, le=2**63 - 1)


def _freeze(db: Session) -> None:
    """Release 3.17: months that ended keep the goal plans they showed, before a goal changes."""
    freeze_first(db)  # Release 3.17: ended months keep their goal and debt plans


def _refused(db: Session, exc: service.GoalError) -> JSONResponse:
    db.rollback()
    return JSONResponse(exc.body(), status_code=exc.status)


@router.get("")
def goals_state(db: Session = Depends(get_db)) -> dict:
    return service.state(db, today())


@router.post("", status_code=201, response_model=None)
def create_goal(body: GoalCreateBody, db: Session = Depends(get_db)) -> dict | JSONResponse:
    _freeze(db)
    try:
        goal_id = service.create(
            db, today(), kind=body.kind, name=body.name, target=to_cents(body.target),
            due_month=body.due_month, monthly=to_cents(body.monthly),
            already_saved=to_cents(body.already_saved), account_id=body.account_id,
        )
    except service.GoalError as exc:
        return _refused(db, exc)
    db.commit()
    return {**service.state(db, today()), "goal_id": goal_id}


# Declared before /{goal_id} so "restore" is never taken for an id.
@router.post("/restore", status_code=201, response_model=None)
def restore_goal(body: GoalRestoreBody, db: Session = Depends(get_db)) -> dict | JSONResponse:
    _freeze(db)
    # The server kept its own record of the delete (by ``token``); the rest of the body is
    # checked against it, never trusted.
    try:
        goal_id = service.restore(db, today(), token=body.token, month=body.month, category=body.category_id)
    except service.GoalError as exc:
        return _refused(db, exc)
    db.commit()
    return {**service.state(db, today()), "goal_id": goal_id}


@router.patch("/{goal_id}", response_model=None)
def update_goal(body: GoalPatchBody, goal_id: int = GoalId, db: Session = Depends(get_db)) -> dict | JSONResponse:
    if bad := non_null(body, ("name", "target", "monthly", "already_saved")):
        return JSONResponse({"detail": f"{bad} must not be null"}, status_code=422)
    fields = {}
    for name in body.model_fields_set:
        value = getattr(body, name)
        if name == "plan_before":
            if value is not None:
                fields[name] = (value.month, to_cents(value.planned))
            continue
        fields[name] = to_cents(value) if name in ("target", "monthly", "already_saved") else value
    _freeze(db)
    try:
        service.update(db, today(), goal_id, fields)
    except service.GoalError as exc:
        return _refused(db, exc)
    db.commit()
    return service.state(db, today())


@router.delete("/{goal_id}", response_model=None)
def delete_goal(
    goal_id: int = GoalId,
    reason: Literal["deleted", "spent", "undo_add"] = Query(default="deleted"),
    db: Session = Depends(get_db),
) -> dict | JSONResponse:
    """``deleted`` and ``spent`` only change the words the page shows; both remove the goal the
    same way. ``undo_add`` is the page's Undo right after adding it: a taken-over category's
    rows go back exactly as they were (``service.undo_add``); no Undo of the Undo."""
    _freeze(db)
    if reason == "undo_add":
        try:
            kept_in, released = service.undo_add(db, today(), goal_id)
        except service.GoalError as exc:
            return _refused(db, exc)
        db.commit()
        # ``released``: what went back to Not planned yet (negative: overspending that came out of it).
        return {"state": service.state(db, today()), "kept_in": kept_in, "released": from_cents(released)}
    try:
        undo, kept_in = service.delete(db, today(), goal_id)
    except service.GoalError as exc:
        return _refused(db, exc)
    db.commit()
    # ``kept_in``: the name of a category the goal had taken over (Emergency savings), which
    # stays in the Budget with its money; null when the goal's own category left the Budget.
    return {"state": service.state(db, today()), "undo": undo, "kept_in": kept_in}

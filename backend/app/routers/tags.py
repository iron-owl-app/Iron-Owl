from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..deps import get_db
from ..models import Tag
from ..schemas import TagCreate, TagPatch, non_null
from ..seeds import fnv1a_hue
from ..services import txns

router = APIRouter(prefix="/api/tags", tags=["tags"])


def _clean(name: str | None) -> str:
    cleaned = " ".join((name or "").split())
    if not cleaned:
        raise HTTPException(status_code=422, detail="name must not be empty")
    return cleaned


def _ensure_unique(db: Session, name: str, exclude_id: int | None = None) -> None:
    # The column is COLLATE NOCASE (ASCII only); compare with casefold() for full Unicode.
    for tag in db.scalars(select(Tag)):
        if tag.id != exclude_id and tag.name.casefold() == name.casefold():
            raise HTTPException(status_code=409, detail=f"A tag named “{name}” already exists.")


def _get(db: Session, tag_id: int) -> Tag:
    tag = db.get(Tag, tag_id)
    if tag is None:
        raise HTTPException(status_code=404, detail="tag not found")
    return tag


def _out(db: Session, tag_id: int) -> dict:
    return next(t for t in txns.tags_out(db) if t["id"] == tag_id)


@router.get("")
def list_tags(db: Session = Depends(get_db)) -> list[dict]:
    return txns.tags_out(db)


@router.post("", status_code=201)
def create_tag(body: TagCreate, db: Session = Depends(get_db)) -> dict:
    name = _clean(body.name)
    _ensure_unique(db, name)
    if (db.scalar(select(func.count()).select_from(Tag)) or 0) >= txns.MAX_TAGS:
        raise HTTPException(status_code=422, detail=f"You can have at most {txns.MAX_TAGS} tags.")
    tag = Tag(name=name, hue=body.hue if body.hue is not None else fnv1a_hue(name))
    db.add(tag)
    db.commit()
    return txns.tag_out(tag)


@router.patch("/{tag_id}")
def update_tag(tag_id: int, body: TagPatch, db: Session = Depends(get_db)) -> dict:
    tag = _get(db, tag_id)
    if bad := non_null(body, ("name", "hue")):
        raise HTTPException(status_code=422, detail=f"{bad} must not be null")
    if "name" in body.model_fields_set:
        name = _clean(body.name)
        _ensure_unique(db, name, exclude_id=tag.id)
        tag.name = name
    if "hue" in body.model_fields_set:
        tag.hue = body.hue
    db.commit()
    return _out(db, tag_id)


@router.delete("/{tag_id}", status_code=204)
def delete_tag(tag_id: int, db: Session = Depends(get_db)) -> Response:
    db.delete(_get(db, tag_id))  # transaction_tags rows go with it (ON DELETE CASCADE)
    db.commit()
    return Response(status_code=204)

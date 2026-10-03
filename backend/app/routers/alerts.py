from __future__ import annotations

import math

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..deps import get_db
from ..models import AlertEvent, AlertSetting
from ..schemas import AlertSettingPatch, AlertSettingsBody
from ..seeds import ALERT_ORDER, ALERT_SEEDS
from ..services.alerts import LOW_AHEAD_MAX_DAYS, PAIRED, disabled_keys, event_out, settings_out

router = APIRouter(prefix="/api/alerts", tags=["alerts"])

MAX_VALUE = {"usd": 1_000_000_000.0, "percent": 100.0, "days": 365.0}
# Per-key limits (Release 3.6): "warn me N days ahead" looks at most a month ahead.
KEY_MAX = {"low_ahead": float(LOW_AHEAD_MAX_DAYS)}
WHOLE_DAYS = frozenset({"low_ahead"})


@router.get("/settings")
def list_settings(db: Session = Depends(get_db)) -> list[dict]:
    out = settings_out(db)
    db.commit()
    return out


def _check(key: str, body: AlertSettingPatch) -> None:
    """422 unless ``body`` is a valid change of alert ``key`` (nothing is written)."""
    unit = ALERT_SEEDS[key][1]
    fields = body.model_fields_set
    if "enabled" in fields and body.enabled is None:
        raise HTTPException(status_code=422, detail="enabled must not be null")
    if "value" in fields:
        if unit is None:
            raise HTTPException(status_code=422, detail="this alert has no value")
        value = body.value
        if value is None or not math.isfinite(value) or value <= 0:
            raise HTTPException(status_code=422, detail="value must be greater than 0")
        maximum = KEY_MAX.get(key, MAX_VALUE[unit])
        if value > maximum:
            limit = "100" if unit == "percent" else f"{maximum:g}"
            raise HTTPException(status_code=422, detail=f"value must be at most {limit}")
        if key in WHOLE_DAYS and value != int(value):
            raise HTTPException(status_code=422, detail="value must be a whole number of days")


def _apply(db: Session, key: str, *, enabled: bool | None = None, value: float | None = None) -> None:
    setting = db.get(AlertSetting, key)
    if setting is None:
        setting = AlertSetting(key=key, enabled=True, value=ALERT_SEEDS[key][0])
        db.add(setting)
    if enabled is not None:
        setting.enabled = bool(enabled)
    if value is not None:
        setting.value = float(value)


@router.put("/settings")
def update_settings(body: AlertSettingsBody, db: Session = Depends(get_db)) -> list[dict]:
    """Settings D7 update 2: the Alerts tab's rows in one atomic change. ``low.enabled`` sets
    ``low`` and ``low_ahead``; ``reminder.enabled`` sets ``reminder`` and ``due``; values are
    the row's own key only. Everything is checked first: any 422 saves nothing."""
    rows = {key: getattr(body, key) for key in body.model_fields_set}
    for key, patch in rows.items():
        if patch is None:
            raise HTTPException(status_code=422, detail=f"{key} must not be null")
        _check(key, patch)
    for key, patch in rows.items():
        fields = patch.model_fields_set
        if "enabled" in fields:
            for paired in PAIRED.get(key, (key,)):
                _apply(db, paired, enabled=patch.enabled)
        if "value" in fields:
            _apply(db, key, value=patch.value)
    db.commit()
    return settings_out(db)


@router.put("/settings/{key}")
def update_setting(key: str, body: AlertSettingPatch, db: Session = Depends(get_db)) -> dict:
    if key not in ALERT_SEEDS:
        raise HTTPException(status_code=404, detail="unknown alert")
    _check(key, body)
    fields = body.model_fields_set
    _apply(db, key, enabled=body.enabled if "enabled" in fields else None,
           value=body.value if "value" in fields else None)
    db.commit()
    return settings_out(db)[ALERT_ORDER.index(key)]


@router.get("/events")
def list_events(limit: int = Query(default=50, ge=1, le=500), db: Session = Depends(get_db)) -> list[dict]:
    # Settings D7 update 2: events of alerts that are turned off are hidden.
    events = db.scalars(
        select(AlertEvent)
        .where(AlertEvent.cleared.is_(False), AlertEvent.key.not_in(disabled_keys(db)))
        .order_by(AlertEvent.created_at.desc(), AlertEvent.id.desc())
        .limit(limit)
    )
    return [event_out(e) for e in events]


@router.post("/events/read")
def mark_read(db: Session = Depends(get_db)) -> dict:
    db.execute(update(AlertEvent).where(AlertEvent.read.is_(False)).values(read=True))
    db.commit()
    return {"ok": True}


@router.delete("/events", status_code=204)
def clear_events(db: Session = Depends(get_db)) -> Response:
    # Keep a scrubbed tombstone per dedupe_key so a cleared alert can't fire again.
    db.execute(
        update(AlertEvent)
        .where(AlertEvent.cleared.is_(False))
        .values(cleared=True, read=True, title="", body="", data=None)
    )
    db.commit()
    return Response(status_code=204)

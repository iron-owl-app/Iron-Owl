from __future__ import annotations

import datetime as dt
import json

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..deps import get_db
from ..models import Account, RecurringItem, RecurringOverride, TxnCategory
from ..schemas import (
    CategoryGuessBody,
    ForecastAccountBody,
    ForecastSettingsBody,
    OccurrenceBody,
    PriceAnswerBody,
    RecurringCreate,
    RecurringPatch,
    RescheduleBody,
    SnapshotBody,
    non_null,
)
from ..services import alerts, forecast, price_ask
from ..services import calendar as cal
from ..services import recurring as rec
from ..services.recurring import (
    MONTH_STEPS,
    STATUS_ORDER,
    anchor_days,
    detect,
    merchant_key,
    parse_days,
    recurring_out,
)
from ..utils import days_in_month, iso_utc, to_cents, today

router = APIRouter(prefix="/api", tags=["recurring"])


def _get(db: Session, item_id: int) -> RecurringItem:
    item = db.get(RecurringItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="recurring item not found")
    return item


def _account_name(db: Session, account_id: int | None) -> str | None:
    if account_id is None:
        return None
    return db.scalar(select(Account.name).where(Account.id == account_id))


def _category_name(db: Session, category_id: str | None) -> str | None:
    if category_id is None:
        return None
    return db.scalar(select(TxnCategory.name).where(TxnCategory.id == category_id))


def _out(db: Session, item: RecurringItem) -> dict:
    return recurring_out(item, _account_name(db, item.account_id), _category_name(db, item.category_id))


def _check_account(db: Session, account_id: int | None) -> None:
    if account_id is not None and db.get(Account, account_id) is None:
        raise HTTPException(status_code=422, detail="unknown account")


def _check_category(db: Session, category_id: str | None) -> str | None:
    """A budget category for a recurring item: an existing spending, fixed or income one."""
    if category_id is None:
        return None
    category = db.get(TxnCategory, category_id)
    if category is None or category.kind == "transfer":
        raise HTTPException(status_code=422, detail="unknown category")
    return category.id


def _clean_name(name: str | None) -> str:
    cleaned = " ".join((name or "").split())
    if not cleaned:
        raise HTTPException(status_code=422, detail="name must not be empty")
    return cleaned


def _amount(value: float) -> int:
    cents = to_cents(value)
    if cents == 0:
        raise HTTPException(status_code=422, detail="amount must not be zero")
    return cents


def _start_from(item: RecurringItem, day: dt.date) -> None:
    """Re-anchoring: the series' dates before ``day`` stop existing (never moves the start earlier)."""
    if item.start_date is None or item.start_date < day:
        item.start_date = day


@router.get("/recurring")
def list_recurring(db: Session = Depends(get_db)) -> list[dict]:
    rec.safe_backfill(db, today())
    names = {a.id: a.name for a in db.scalars(select(Account))}
    categories = {c.id: c.name for c in db.scalars(select(TxnCategory))}
    items = sorted(
        db.scalars(select(RecurringItem)),
        key=lambda i: (STATUS_ORDER.get(i.status, 9), i.next_date, i.name.lower(), i.id),
    )
    asks = price_ask.safe_open_questions(db)  # 3.19: "now charges $X. Update your amount?"
    return [
        {**recurring_out(i, names.get(i.account_id), categories.get(i.category_id)), "price_question": asks.get(i.id)}
        for i in items
    ]


@router.post("/recurring", status_code=201)
def create_recurring(body: RecurringCreate, db: Session = Depends(get_db)) -> dict:
    name = _clean_name(body.name)
    _check_account(db, body.account_id)
    amount = _amount(body.amount)
    if "category_id" in body.model_fields_set:
        category_id = _check_category(db, body.category_id)
    else:
        category_id = rec.guess_category(db, today(), name, amount, body.account_id)
    days = anchor_days(body.cadence, body.next_date)
    item = RecurringItem(
        name=name,
        merchant_key=merchant_key(name, name),
        account_id=body.account_id,
        amount_cents=amount,
        cadence=body.cadence,
        next_date=body.next_date,
        status="active",
        include_in_forecast=True,
        source="manual",
        last_seen_date=None,
        month_days=json.dumps(days) if days else None,
        reminder_days=body.reminder_days,
        start_date=body.next_date,  # an item added by hand has no occurrences before its first date
        category_id=category_id,
    )
    db.add(item)
    db.commit()
    return _out(db, item)


@router.post("/recurring/category-guess")
def category_guess(body: CategoryGuessBody, db: Session = Depends(get_db)) -> dict:
    """The Add dialog's "Which budget category?" pre-fill, from the merchant's past transactions.

    A POST body, so a typed bill name never lands in a URL or an access log.
    """
    guess = rec.guess_category(db, today(), " ".join(body.name.split()), body.amount_sign, body.account_id)
    return {"category_id": guess, "category_name": _category_name(db, guess)}


@router.get("/recurring/candidates")
def list_candidates(db: Session = Depends(get_db)) -> list[dict]:
    return cal.candidates(db, today())


@router.patch("/recurring/{item_id}")
def update_recurring(item_id: int, body: RecurringPatch, db: Session = Depends(get_db)) -> dict:
    item = _get(db, item_id)
    if bad := non_null(
        body, ("name", "amount", "cadence", "next_date", "status", "include_in_forecast", "reminder_days")
    ):
        raise HTTPException(status_code=422, detail=f"{bad} must not be null")
    fields = body.model_fields_set
    if "name" in fields:
        item.name = _clean_name(body.name)  # merchant_key stays, so detection keeps matching
    if "amount" in fields:
        cents = _amount(body.amount)
        if price_ask.is_yes(db, item, cents):
            rec.mark_hand_set(db, item)  # 3.19: Yes to "now charges $X": the new amount stays theirs
        item.amount_cents = cents
    if "account_id" in fields:
        _check_account(db, body.account_id)
        item.account_id = body.account_id
    if "category_id" in fields:
        item.category_id = _check_category(db, body.category_id)
    if "reminder_days" in fields:
        item.reminder_days = body.reminder_days
    if "status" in fields:
        confirming = item.status == "suggested" and body.status == "active"
        item.status = body.status
        if confirming and "category_id" not in fields and item.category_id is None:
            # Confirmed from a suggestion: its budget category from its past transactions.
            item.category_id = rec.category_from_history(db, item, today())
    if "include_in_forecast" in fields:
        item.include_in_forecast = bool(body.include_in_forecast)
    if "cadence" in fields or "next_date" in fields:
        cadence = body.cadence if "cadence" in fields else item.cadence
        next_date = body.next_date if "next_date" in fields else item.next_date
        days = anchor_days(cadence, next_date, parse_days(item.month_days) if cadence == item.cadence else None)
        same_rule = (
            cadence == item.cadence and days == parse_days(item.month_days) and cal.is_rule_date(item, next_date)
        )
        if not same_rule:
            # A new schedule starts from here: past dates aren't regenerated on it (that would
            # orphan their overrides, e.g. count a bill marked paid again).
            _start_from(item, min(max(item.next_date, today()), next_date))
        item.cadence, item.next_date = cadence, next_date
        item.month_days = json.dumps(days) if days else None
        if item.start_date is not None and next_date < item.start_date:
            item.start_date = next_date  # the rule may not start after its own next date
    db.commit()
    return _out(db, item)


@router.post("/recurring/{item_id}/price-question", status_code=204)
def answer_price_question(
    body: PriceAnswerBody, item_id: int = Path(ge=1, le=2**63 - 1), db: Session = Depends(get_db),
) -> Response:
    """Release 3.19: No to "now charges $X. Update your amount?" (or its Undo). Answering a
    question that is gone (answered, or replaced by a newer charge) changes nothing."""
    item = _get(db, item_id)
    if price_ask.answer(db, item, body.transaction_id, keep=body.answer == "no"):
        db.commit()
    return Response(status_code=204)


@router.delete("/recurring/{item_id}", status_code=204)
def delete_recurring(item_id: int, db: Session = Depends(get_db)) -> Response:
    db.delete(_get(db, item_id))
    db.commit()
    return Response(status_code=204)


@router.post("/recurring/detect")
def run_detection(db: Session = Depends(get_db)) -> dict:
    day = today()
    new_ids = detect(db, day)
    db.commit()
    alerts.safe_evaluate(db, day, new_recurring_ids=new_ids)
    return {"suggested": len(new_ids)}


# ------------------------------------------------------------------ snapshot / restore (Undo)


def _overrides(db: Session, item_id: int) -> list[RecurringOverride]:
    return list(db.scalars(
        select(RecurringOverride).where(RecurringOverride.recurring_id == item_id).order_by(RecurringOverride.base_date)
    ))


def _snapshot(db: Session, item: RecurringItem) -> dict:
    return {
        "item": {**_out(db, item), "merchant_key": item.merchant_key, "created_at": iso_utc(item.created_at)},
        "overrides": [cal.override_out(o) for o in _overrides(db, item.id)],
    }


@router.get("/recurring/{item_id}/snapshot")
def get_snapshot(item_id: int, db: Session = Depends(get_db)) -> dict:
    return _snapshot(db, _get(db, item_id))


def _anchor_fits(cadence: str, days: list[int] | None) -> bool:
    if days is None:
        return True
    if cadence == "semimonthly":
        return len(days) == 2
    return cadence in MONTH_STEPS and len(days) == 1


@router.post("/recurring/restore")
def restore(body: SnapshotBody, response: Response, db: Session = Depends(get_db)) -> dict:
    """Undo: put an item (and its overrides) back exactly as a snapshot had it."""
    s = body.item
    # Validate everything before writing anything.
    amount = _amount(s.amount)
    name = _clean_name(s.name)
    key = " ".join(s.merchant_key.split())
    if not key:
        raise HTTPException(status_code=422, detail="merchant_key must not be empty")
    if not _anchor_fits(s.cadence, s.anchor_days):
        raise HTTPException(status_code=422, detail="anchor_days doesn't fit the cadence")
    bases = [o.base_date for o in body.overrides]
    if len(set(bases)) != len(bases):
        raise HTTPException(status_code=422, detail="overrides has the same base_date twice")
    if any(o.paid_amount is not None and not o.paid for o in body.overrides):
        raise HTTPException(status_code=422, detail="paid_amount needs paid: true")
    account_id = s.account_id if s.account_id is not None and db.get(Account, s.account_id) is not None else None
    # Like create: a transfer category is refused; one deleted since comes back as none.
    category_id = None
    if s.category_id is not None and db.get(TxnCategory, s.category_id) is not None:
        category_id = _check_category(db, s.category_id)
    created_at = s.created_at
    if created_at.tzinfo is not None:
        created_at = created_at.astimezone(dt.timezone.utc).replace(tzinfo=None)

    item = db.get(RecurringItem, s.id)
    created = item is None
    if not created and (
        item.merchant_key != key or item.created_at.replace(microsecond=0) != created_at.replace(microsecond=0)
    ):
        # Not the snapshot's item: SQLite may give a deleted item's id to a new one.
        raise HTTPException(status_code=409, detail="This item changed since. Undo isn't available.")
    if created:
        item = RecurringItem(id=s.id)
        db.add(item)
    item.name, item.merchant_key, item.account_id = name, key, account_id
    item.amount_cents, item.cadence, item.next_date = amount, s.cadence, s.next_date
    item.status, item.include_in_forecast, item.source = s.status, s.include_in_forecast, s.source
    item.last_seen_date, item.month_days = s.last_seen_date, json.dumps(s.anchor_days) if s.anchor_days else None
    item.created_at, item.reminder_days, item.start_date = created_at, s.reminder_days, s.start_date
    item.category_id = category_id
    db.flush()
    db.execute(delete(RecurringOverride).where(RecurringOverride.recurring_id == s.id))
    sign = 1 if amount > 0 else -1
    for o in body.overrides:
        moved_to = None if o.moved_to == o.base_date else o.moved_to
        if moved_to is None and not o.skipped and not o.paid:
            continue
        db.add(RecurringOverride(
            recurring_id=s.id, base_date=o.base_date, moved_to=moved_to, skipped=o.skipped, paid=o.paid,
            paid_amount_cents=sign * to_cents(o.paid_amount) if o.paid_amount is not None else None,
        ))
    db.commit()
    response.status_code = 201 if created else 200
    return _out(db, item)


# ------------------------------------------------------------------ calendar changes


def _require_active(item: RecurringItem) -> None:
    if item.status != "active":
        raise HTTPException(status_code=422, detail="Only items on your calendar can be changed.")


def _check_new_date(day: dt.date, now: dt.date) -> None:
    if day < now:
        raise HTTPException(status_code=422, detail="Pick today or a later date.")
    if day > cal.horizon_end(now):
        raise HTTPException(status_code=422, detail="Pick a date within the next 12 months.")


@router.post("/recurring/{item_id}/reschedule")
def reschedule(item_id: int, body: RescheduleBody, db: Session = Depends(get_db)) -> dict:
    """"Move all future ones too": re-anchor the series on ``to`` from its next scheduled base."""
    item = _get(db, item_id)
    now = today()
    _require_active(item)
    if item.cadence in ("once", "semimonthly"):
        raise HTTPException(
            status_code=422,
            detail="This one can only be moved one at a time. Use Edit details to change its usual day.",
        )
    if body.from_base != rec.next_base(item, now):
        raise HTTPException(status_code=422, detail="Only the next one can move every future one.")
    _check_new_date(body.to, now)
    undo = _snapshot(db, item)
    old = parse_days(item.month_days)
    days = anchor_days(item.cadence, body.to)
    if days and old == [31] and body.to.day == days_in_month(body.to.year, body.to.month):
        days = [31]  # a month-end bill stays on the month's last day
    # Dates before the move keep their old rule: they aren't regenerated on the new anchor.
    _start_from(item, min(body.from_base, body.to))
    item.next_date = body.to
    item.month_days = json.dumps(days) if days else None
    if item.start_date > body.to:
        item.start_date = body.to
    db.execute(delete(RecurringOverride).where(
        RecurringOverride.recurring_id == item.id, RecurringOverride.base_date >= body.from_base
    ))
    db.commit()
    return {"item": _out(db, item), "undo": undo}


@router.put("/recurring/{item_id}/occurrences/{base_date}")
def set_occurrence(item_id: int, base_date: dt.date, body: OccurrenceBody, db: Session = Depends(get_db)) -> dict:
    item = _get(db, item_id)
    now = today()
    _require_active(item)
    if not cal.is_rule_date(item, base_date):
        raise HTTPException(status_code=422, detail=f"That date isn't one of {item.name}'s dates.")
    if body.paid_amount is not None and not body.paid:
        raise HTTPException(status_code=422, detail="paid_amount needs paid: true")
    existing = db.scalar(select(RecurringOverride).where(
        RecurringOverride.recurring_id == item.id, RecurringOverride.base_date == base_date
    ))
    previous = cal.override_out(existing)
    moved_to = None if body.moved_to == base_date else body.moved_to
    # A new date must be ahead; an unchanged one (e.g. marking a moved, now late, one paid) may be past.
    if moved_to is not None and (existing is None or existing.moved_to != moved_to):
        _check_new_date(moved_to, now)
    if moved_to is None and not body.skipped and not body.paid:
        if existing is not None:
            db.delete(existing)
        db.commit()
        return {"override": None, "previous": previous}
    if existing is None:
        existing = RecurringOverride(recurring_id=item.id, base_date=base_date)
        db.add(existing)
    sign = 1 if item.amount_cents > 0 else -1
    existing.moved_to, existing.skipped, existing.paid = moved_to, body.skipped, body.paid
    existing.paid_amount_cents = sign * to_cents(body.paid_amount) if body.paid_amount is not None else None
    db.commit()
    return {"override": cal.override_out(existing), "previous": previous}


@router.delete("/recurring/{item_id}/occurrences/{base_date}", status_code=204)
def clear_occurrence(item_id: int, base_date: dt.date, db: Session = Depends(get_db)) -> Response:
    item = _get(db, item_id)
    db.execute(delete(RecurringOverride).where(
        RecurringOverride.recurring_id == item.id, RecurringOverride.base_date == base_date
    ))
    db.commit()
    return Response(status_code=204)


# ------------------------------------------------------------------ forecast


@router.get("/forecast")
def get_forecast(end: dt.date | None = None, db: Session = Depends(get_db)) -> dict:
    day = today()
    if end is not None and end > day + dt.timedelta(days=forecast.MAX_HORIZON_DAYS):
        raise HTTPException(status_code=422, detail="end is too far in the future (max 2 years)")
    return forecast.build(db, day, end)


@router.put("/forecast/account")
def set_forecast_account(body: ForecastAccountBody, db: Session = Depends(get_db)) -> dict:
    account = db.get(Account, body.account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")
    if not forecast.is_depository(account):
        raise HTTPException(status_code=422, detail="The forecast account must be a checking or savings account.")
    forecast.set_forecast_account(db, account.id)
    db.commit()
    return forecast.build(db, today())


@router.get("/forecast/calendar")
def get_calendar(
    start: dt.date | None = Query(default=None, alias="from"),
    end: dt.date | None = Query(default=None, alias="to"),
    db: Session = Depends(get_db),
) -> dict:
    day = today()
    start = start or day
    end = end or day + dt.timedelta(days=cal.DEFAULT_DAYS)
    if start > end:
        raise HTTPException(status_code=422, detail="from must be on or before to")
    if start < day - dt.timedelta(days=cal.PAST_DAYS):
        raise HTTPException(status_code=422, detail="from is too far back")
    if end > day + dt.timedelta(days=cal.AHEAD_DAYS):
        raise HTTPException(status_code=422, detail="to is too far ahead")
    rec.safe_backfill(db, day)
    return cal.build_calendar(db, day, start, end)


@router.get("/forecast/settings")
def get_forecast_settings(db: Session = Depends(get_db)) -> dict:
    return cal.settings_out(db)


@router.patch("/forecast/settings")
def update_forecast_settings(body: ForecastSettingsBody, db: Session = Depends(get_db)) -> dict:
    if bad := non_null(body, ("include_daily", "excluded_plans")):
        raise HTTPException(status_code=422, detail=f"{bad} must not be null")
    excluded = None
    if "excluded_plans" in body.model_fields_set:
        excluded = list(dict.fromkeys(body.excluded_plans))
        known = set(db.scalars(select(TxnCategory.id).where(TxnCategory.id.in_(excluded)))) if excluded else set()
        if any(cid not in known for cid in excluded):
            raise HTTPException(status_code=422, detail="unknown category")
    cal.save_settings(db, include=body.include_daily, excluded=excluded)
    db.commit()
    return cal.settings_out(db)

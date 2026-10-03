"""Settings redesign (D7): ``/api/settings`` (auto-lock, paycheck notes), ``/api/paychecks`` and
``/api/settings/support-contact`` (who to call for help)."""
from __future__ import annotations

from fractions import Fraction

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import AppState, get_db, get_state
from ..models import Account
from ..schemas import SettingsPatch, SupportContactBody, non_null
from ..services import prefs, spending, support_contact
from ..services.categories import load as load_categories
from ..utils import from_cents, month_of, today

router = APIRouter(prefix="/api", tags=["settings"])


@router.get("/settings")
def get_settings(db: Session = Depends(get_db), state: AppState = Depends(get_state)) -> dict:
    return prefs.settings_out(db, state.settings.auto_lock_minutes)


@router.patch("/settings")
def patch_settings(body: SettingsPatch, db: Session = Depends(get_db), state: AppState = Depends(get_state)) -> dict:
    if bad := non_null(body, ("auto_lock_minutes", "paycheck_notify")):
        raise HTTPException(status_code=422, detail=f"{bad} must not be null")
    fields = body.model_fields_set
    if "auto_lock_minutes" in fields:
        prefs.set_auto_lock(db, body.auto_lock_minutes)
    if "paycheck_notify" in fields:
        prefs.set_paycheck_notify(db, bool(body.paycheck_notify))
    db.commit()
    if "auto_lock_minutes" in fields:
        # At once: the idle timeout of this unlocked session (every unlock reads it again).
        state.vault.set_auto_lock(body.auto_lock_minutes)
    return prefs.settings_out(db, state.settings.auto_lock_minutes)


@router.put("/settings/support-contact")
def put_support_contact(
    body: SupportContactBody, db: Session = Depends(get_db), state: AppState = Depends(get_state),
) -> dict:
    """Who to call for help (Iron Owl 2.0.0). Session required (``get_db``); the name is read
    back, also while locked, through ``GET /api/auth/status`` ``support_contact``."""
    try:
        saved = support_contact.save(state.settings, body.contact)
    except OSError:
        raise HTTPException(status_code=500, detail="The name couldn't be saved. Try again.") from None
    return {"support_contact": saved or None}


# ------------------------------------------------------------------ paychecks

# Paychecks per month by cadence (Fraction: exact until the final rounding to cents).
PER_MONTH: dict[str, Fraction] = {
    "weekly": Fraction(52, 12),
    "biweekly": Fraction(26, 12),
    "semimonthly": Fraction(2),
    "monthly": Fraction(1),
    "quarterly": Fraction(1, 3),
    "yearly": Fraction(1, 12),
    "once": Fraction(0),
}


def per_month_cents(amount_cents: int, cadence: str) -> int:
    """``amount × how often it comes`` per month, rounded half away from zero to cents."""
    exact = amount_cents * PER_MONTH.get(cadence, Fraction(0))
    sign = -1 if exact < 0 else 1
    return sign * int(abs(exact) + Fraction(1, 2))


def _next_date(item, occurrences: tuple, day) -> str | None:  # noqa: ANN001 - RecurringItem, date
    """The item's next paycheck from today on (calendar moves and skips applied), else its
    ``next_date`` when that isn't past (beyond the budget's look-ahead), else null."""
    for occ in occurrences:
        if occ.item.id == item.id and occ.status == "upcoming" and occ.date >= day:
            return occ.date.isoformat()
    return item.next_date.isoformat() if item.next_date is not None and item.next_date >= day else None


@router.get("/paychecks")
def get_paychecks(db: Session = Depends(get_db)) -> dict:
    """The Paychecks tab: active money coming in on the budget accounts (``income_items``).

    Writes go through ``/api/recurring`` (create, edit, delete + snapshot/restore for Undo).
    """
    day = today()
    included = [a.id for a, inc in spending.budget_accounts(db) if inc]
    items = spending.income_items(db, included)
    cmap = load_categories(db)
    income = spending.load_income(db, cmap, included, day)
    accounts = {
        a.id: a for a in db.scalars(select(Account).where(Account.id.in_([i.account_id for i in items if i.account_id])))
    } if items else {}
    out = []
    monthly = 0
    for item in items:
        per_month = per_month_cents(item.amount_cents, item.cadence)
        monthly += per_month
        account = accounts.get(item.account_id) if item.account_id is not None else None
        out.append({
            "id": item.id,
            "name": item.name,
            "amount": from_cents(item.amount_cents),
            "cadence": item.cadence,
            "next_date": _next_date(item, income.occurrences, day),
            "account": {"id": account.id, "name": account.name, "mask": account.mask} if account else None,
            "per_month": from_cents(per_month),
        })
    this_month = spending.paychecks_in_month(income.occurrences, month_of(day)) if items else 0
    result = {
        "items": out,
        "monthly_total": from_cents(monthly),
        "this_month_total": from_cents(this_month),
        "income_mode": income.mode,
        "notify": prefs.paycheck_notify(db),
    }
    db.commit()
    return result

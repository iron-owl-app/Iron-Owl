"""Cash-flow forecast for one depository account (the client runs ``project()``)."""
from __future__ import annotations

import calendar
import datetime as dt
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Account, AlertSetting, AppSetting, RecurringItem, Transaction
from ..utils import from_cents, to_cents
from . import ledger
from .categories import load as load_categories
from .recurring import item_aliases, merchant_key, occurrences, parse_days

FORECAST_SETTING = "forecast_account_id"
DAILY_WINDOW_DAYS = 90
MAX_HORIZON_DAYS = 731


def default_end(today: dt.date) -> dt.date:
    """Last day of next month."""
    year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return dt.date(year, month, calendar.monthrange(year, month)[1])


def is_depository(account: Account) -> bool:
    if account.plaid_type is not None:
        return account.plaid_type.lower() == "depository"
    # Manual accounts carry no Plaid type; a manual bank account is depository.
    return account.source == "manual" and account.category == "bank"


def forecast_account(session: Session) -> Account | None:
    setting = session.get(AppSetting, FORECAST_SETTING)
    if setting is not None and setting.value and setting.value.isdigit():
        account = session.get(Account, int(setting.value))
        if account is not None:
            return account
    return session.scalar(
        select(Account)
        .where(
            Account.hidden.is_(False),
            func.lower(Account.plaid_type) == "depository",
            func.lower(Account.plaid_subtype) == "checking",
        )
        .order_by(Account.current_balance_cents.desc(), Account.id)
        .limit(1)
    )


def set_forecast_account(session: Session, account_id: int) -> None:
    setting = session.get(AppSetting, FORECAST_SETTING)
    if setting is None:
        session.add(AppSetting(key=FORECAST_SETTING, value=str(account_id)))
    else:
        setting.value = str(account_id)
    session.flush()


def low_threshold_cents(session: Session) -> int:
    setting = session.get(AlertSetting, "low")
    value = setting.value if setting is not None and setting.value is not None else 2000.0
    return to_cents(value)


def _active_keys(items: list[RecurringItem], aliases: dict) -> tuple[set[tuple[int, str]], set[str]]:
    with_account = {(i.account_id, i.merchant_key) for i in items if i.account_id is not None}
    with_account |= {pair for pairs in aliases.values() for pair in pairs}  # 3.19: renamed or moved bills
    any_account = {i.merchant_key for i in items if i.account_id is None}
    return with_account, any_account


def daily_spend_cents(session: Session, account: Account, today: dt.date, active: list[RecurringItem]) -> int:
    """Average daily everyday outflow on the account over the last 90 days.

    Excludes transfers, fixed/income/transfer kinds and anything matching an active
    recurring item (those are forecast as events). If the account's history is shorter
    than 90 days, the average is over the days it has.
    """
    start = today - dt.timedelta(days=DAILY_WINDOW_DAYS - 1)
    cmap = load_categories(session)
    lines = ledger.lines(
        Transaction.account_id == account.id,
        Transaction.date >= start,
        Transaction.date <= today,
        Transaction.is_transfer.is_(False),
    )
    # Split-aware: each part counts with its own category.
    rows = session.execute(
        select(lines.c.amount_cents, lines.c.category, lines.c.name, lines.c.merchant_name)
        .where(lines.c.amount_cents < 0)
    ).all()
    with_account, any_account = _active_keys(active, item_aliases(session, active))
    total = 0
    for row in rows:
        if cmap.kind(row.category) != "spending":
            continue
        key = merchant_key(row.merchant_name, row.name)
        if (account.id, key) in with_account or key in any_account:
            continue
        total += -row.amount_cents
    if total == 0:
        return 0
    first = session.scalar(select(func.min(Transaction.date)).where(Transaction.account_id == account.id))
    days = DAILY_WINDOW_DAYS
    if first is not None and first > start:
        days = max(1, (today - first).days + 1)
    return int((Decimal(total) / days).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def events(session: Session, account: Account, today: dt.date, end: dt.date, active: list[RecurringItem]) -> list[dict]:
    tomorrow = today + dt.timedelta(days=1)
    accounts = {a.id: a for a in session.scalars(select(Account))}
    out: list[dict] = []
    for item in active:
        item_account = accounts.get(item.account_id) if item.account_id is not None else None
        is_card = item_account is not None and item_account.category == "credit"
        if item_account is not None and item_account.id != account.id and not is_card:
            continue
        kind = "card" if is_card else ("in" if item.amount_cents > 0 else "out")
        for day in occurrences(item.next_date, item.cadence, parse_days(item.month_days)):
            if day > end:
                break
            if day < tomorrow:
                continue
            out.append({
                "date": day.isoformat(),
                "recurring_id": item.id,
                "name": item.name,
                "amount": from_cents(item.amount_cents),
                "kind": kind,
                "included": bool(item.include_in_forecast),
            })
    out.sort(key=lambda e: (e["date"], e["name"].lower(), e["recurring_id"]))
    return out


def build(session: Session, today: dt.date, end: dt.date | None = None) -> dict:
    end = end or default_end(today)
    account = forecast_account(session)
    result = {
        "account": None,
        "balance": 0.0,
        "today": today.isoformat(),
        "end": end.isoformat(),
        "daily_spend": 0.0,
        "threshold": from_cents(low_threshold_cents(session)),
        "events": [],
    }
    if account is None:
        return result
    active = list(
        session.scalars(select(RecurringItem).where(RecurringItem.status == "active").order_by(RecurringItem.id))
    )
    result.update(
        account={
            "id": account.id,
            "name": account.name,
            "mask": account.mask,
            "institution_name": account.institution_name,
        },
        balance=from_cents(account.current_balance_cents),
        daily_spend=from_cents(daily_spend_cents(session, account, today, active)),
        events=events(session, account, today, end, active),
    )
    return result

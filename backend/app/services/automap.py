"""Release 3.6 phase 2: sort bank-set transactions into the budget's own rows.

Plaid only gives FinTrack's categories its *primary* code (FOOD_AND_DRINK, TRANSPORTATION...),
but the guided budget setup makes separate rows for Groceries, Eating out and Gas. Since
schema v7, sync stores Plaid's *detailed* code too (``transactions.plaid_detailed``), and this
built-in mapping sorts a transaction into those rows:

    FOOD_AND_DRINK_GROCERIES                                   -> Groceries
    FOOD_AND_DRINK_RESTAURANT / _FAST_FOOD / _COFFEE           -> Eating out
    TRANSPORTATION_GAS                                         -> Gas

(codes checked against Plaid's personal finance category taxonomy CSV; liquor stores,
vending machines and "other food and drink" are left alone.)

The mapping only applies when the budget has the matching category: the setup records the
categories it made in ``app_settings.budget_auto_map`` (``{"groceries": id, ...}``); a key
whose category was deleted, is hidden, or isn't a spending category any more is ignored (from
the next rule run or sync on).

Precedence (services/rules.resolve): a hand-set category > the first matching user rule >
this mapping > Plaid's primary. A mapped transaction gets ``category_source = "auto"``: it is
still automatic (rule runs, "back to automatic" and later syncs recompute it), but it is
settled, so it never shows under "Needs a category". Transactions synced before v7 have no
detailed code and are never touched.
"""
from __future__ import annotations

import json
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AppSetting, TxnCategory

SETTING = "budget_auto_map"
AUTO_SOURCE = "auto"

# Plaid detailed code -> the budget row it sorts into.
DETAILED_TO_ROW: dict[str, str] = {
    "FOOD_AND_DRINK_GROCERIES": "groceries",
    "FOOD_AND_DRINK_RESTAURANT": "eating_out",
    "FOOD_AND_DRINK_FAST_FOOD": "eating_out",
    "FOOD_AND_DRINK_COFFEE": "eating_out",
    "TRANSPORTATION_GAS": "gas",
}
ROWS = ("groceries", "eating_out", "gas")

_DETAILED_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


def valid_detailed(value: object) -> str | None:
    """Plaid's detailed code as stored, or None for anything that doesn't look like one."""
    if not isinstance(value, str):
        return None
    value = value.strip().upper()
    return value if _DETAILED_RE.match(value) else None


def stored(session: Session) -> dict[str, str]:
    """The recorded ``{row: category id}`` (unvalidated ids; bad JSON → empty)."""
    row = session.get(AppSetting, SETTING)
    if row is None or not row.value:
        return {}
    try:
        data = json.loads(row.value)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k in ROWS and isinstance(v, str)}


def record(session: Session, rows: dict[str, str]) -> None:
    """Remember the setup's categories for the mapping (called only by the budget setup)."""
    value = json.dumps({k: rows[k] for k in ROWS if k in rows})
    row = session.get(AppSetting, SETTING)
    if row is None:
        session.add(AppSetting(key=SETTING, value=value))
    else:
        row.value = value
    session.flush()


def load(session: Session) -> dict[str, str]:
    """Plaid detailed code -> category id, for the rows whose category still exists and is
    a visible spending category. Empty when the setup never ran (the mapping is off)."""
    rows = stored(session)
    if not rows:
        return {}
    alive = set(session.scalars(
        select(TxnCategory.id).where(
            TxnCategory.id.in_(list(rows.values())), TxnCategory.kind == "spending", TxnCategory.hidden.is_(False),
        )
    ))
    return {code: rows[key] for code, key in DETAILED_TO_ROW.items() if rows.get(key) in alive}

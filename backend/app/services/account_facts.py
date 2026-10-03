"""What the bank itself says about each linked account (Release 3.10), kept in
``app_settings.account_facts`` (no migration):

    {"<account id>": {"bank_name": "Plaid Checking" | null, "limit_cents": 500000 | null}}

``accounts.name`` is the user's nickname (a sync only sets it when it creates the row), so the
bank's own name lives here: the Accounts page shows it ("Use the bank's name") and the Plaid
duplicate check keys on it, so renaming an account can't hide a duplicate link. Written by
every sync (``record_sync``); entries of accounts that no longer exist are pruned.
"""
from __future__ import annotations

import json
import math
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Account, AppSetting

FACTS_SETTING = "account_facts"
NAME_MAX = 200
_MAX_LIMIT_CENTS = 100_000_000_000_000  # $1 trillion, like every other money field
_MAX_LIMIT_DOLLARS = 1e13  # checked before multiplying: a huge float would overflow round()


def _clean_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())[:NAME_MAX]
    return value or None


def _clean_limit(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or abs(value) > _MAX_LIMIT_CENTS:
        return None
    return value


def load(session: Session) -> dict[int, dict]:
    """``{account id: {"bank_name", "limit_cents"}}``; malformed entries are ignored."""
    row = session.get(AppSetting, FACTS_SETTING)
    if row is None or not row.value:
        return {}
    try:
        data = json.loads(row.value)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[int, dict] = {}
    for key, value in data.items():
        if not isinstance(key, str) or not key.isdigit() or len(key) > 18 or not isinstance(value, dict):
            continue
        out[int(key)] = {
            "bank_name": _clean_name(value.get("bank_name")),
            "limit_cents": _clean_limit(value.get("limit_cents")),
        }
    return out


def store(session: Session, facts: dict[int, dict]) -> None:
    value = json.dumps({str(k): facts[k] for k in sorted(facts)}) if facts else None
    row = session.get(AppSetting, FACTS_SETTING)
    if value is None:
        if row is not None:
            session.delete(row)
    elif row is None:
        session.add(AppSetting(key=FACTS_SETTING, value=value))
    else:
        row.value = value
    session.flush()


def entry(value: Any) -> dict | None:
    """A stored entry, cleaned (for an Undo record put back by ``POST /api/accounts/restore``)."""
    if not isinstance(value, dict):
        return None
    return {"bank_name": _clean_name(value.get("bank_name")), "limit_cents": _clean_limit(value.get("limit_cents"))}


def _limit_cents(balances: Any) -> int | None:
    if not isinstance(balances, dict):
        return None
    limit = balances.get("limit")
    if isinstance(limit, bool) or not isinstance(limit, (int, float)) or not math.isfinite(limit):
        return None
    if abs(limit) > _MAX_LIMIT_DOLLARS:
        return None
    return _clean_limit(round(limit * 100))


def record_sync(session: Session, touched: dict[str, Account], plaid_lists: list[Any]) -> None:
    """After a sync's account upserts: the bank's name (``name``, else ``official_name``) and
    ``balances.limit`` of every account it touched replace their entries; entries of accounts
    that no longer exist are pruned. An account the bank left out this time keeps its entry."""
    fresh: dict[int, dict] = {}
    for plaid_accounts in plaid_lists:
        for pa in plaid_accounts or []:
            if not isinstance(pa, dict):
                continue
            acct = touched.get(pa.get("account_id"))
            if acct is None or acct.id is None:
                continue
            got = fresh.setdefault(acct.id, {"bank_name": None, "limit_cents": None})
            if got["bank_name"] is None:
                got["bank_name"] = _clean_name(pa.get("name")) or _clean_name(pa.get("official_name"))
            if got["limit_cents"] is None:
                got["limit_cents"] = _limit_cents(pa.get("balances"))
    facts = load(session)
    facts.update(fresh)
    if facts:
        existing = set(session.scalars(select(Account.id).where(Account.id.in_(list(facts)))))
        facts = {k: v for k, v in facts.items() if k in existing}
    store(session, facts)

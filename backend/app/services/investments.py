"""Investments (D9, Release 3.8): SPEC "Investments".

Data honesty: an account's worth is its balance; history is real balance snapshots only
(investment accounts are never estimated), so it starts when the account was linked or
added. FinTrack has no contribution data, so every change "includes any money added".

Holdings are sorted into plain kinds (``classify``); what the bank doesn't break down is
``not_broken_down``. Plaid's security type, subtype and cash flag are kept in
``app_settings.security_info`` = ``{security_id: {type, subtype, cash}}``, written by
``sync._apply_holdings``.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Account, AppSetting, BalanceSnapshot, Holding, PlaidItem
from ..utils import from_cents, iso_utc, month_bounds, month_of, parse_month, shift_month

SECURITY_INFO_SETTING = "security_info"
MAX_SECURITIES = 5000
MAX_TEXT = 40
MAX_ID = 200
INVESTMENT_CATEGORIES = ("retirement", "hsa", "investment")
HISTORY_MONTHS = 61
MIN_NOT_BROKEN_DOWN = 100  # less than $1 left over is rounding, not money the bank hides
MAX_NAMES = 20

KINDS = ("us_stock", "intl_stock", "bond", "target_date", "cash", "company_stock", "other", "not_broken_down")

_CASH = re.compile(r"\b(money market|cash|sweep|settlement)\b", re.I)
_TARGET = re.compile(r"\b(target|retirement|lifecycle|freedom|lifepath)\b", re.I)
_YEAR = re.compile(r"(?<![0-9])(20[0-9]{2})(?![0-9])")
_BOND = re.compile(r"\b(bonds?|treasury|treasuries|fixed income|aggregate|tips|municipal|stable value)\b", re.I)
_INTL = re.compile(
    r"\b(international|intl|ex[- ]?us|ex[- ]u\.s\.?|developed|emerging|foreign|europe|european|pacific|"
    r"europacific)\b",
    re.I,
)
# World and global funds hold US and international stocks together: "other", never US stocks.
_GLOBAL = re.compile(r"\b(world|global|acwi|all[- ]?world)\b", re.I)
# Names that say "U.S. stocks" specifically (a fund that says nothing specific is "other").
_US = re.compile(
    r"(?<![0-9])500(?![0-9])|s&p|\b(total stock|total (u\.?s\.? )?market|stock market|extended market|"
    r"large[- ]?cap|mid[- ]?cap|small[- ]?cap|mega[- ]?cap|all[- ]?cap|russell|nasdaq(-100)?|qqq|dow jones|"
    r"u\.?s\.? (stock|equity)|domestic|growth|dividend|value|blue[- ]?chip|equity[- ]income|broad market|"
    r"institutional index)\b",
    re.I,
)
# A fund whose name says it holds stocks (international and bonds are checked first): US stocks.
_STOCK_WORDS = re.compile(r"\b(stocks?|equity|equities)\b", re.I)
# Gold, silver, crypto and other commodities are neither stocks nor bonds.
_COMMODITY = re.compile(r"\b(gold|silver|bitcoin|ether|ethereum|crypto|commodity|commodities)\b", re.I)
# Words that make a name a fund's (without Plaid's type, before the first sync after an upgrade).
# A word ending in "fund" counts too ("Fidelity Contrafund").
_FUND_NAME = re.compile(
    r"(?<![0-9])500(?![0-9])|\b(index|[a-z]*funds?|etfs?|etn|trust|portfolio|admiral|institutional|"
    r"total stock|qqq)\b",
    re.I,
)
# Plaid types many ETFs "equity" (with a fund subtype, or not): such a holding is a fund, not a
# company's stock, when its subtype or name says so. "Trust" alone is common in company names
# ("Northern Trust Corp"), so it counts only next to fund words ("Invesco QQQ Trust").
_EQUITY_FUND_NAME = re.compile(r"\b(index|[a-z]*funds?|etfs?|etn)\b", re.I)
_TRUST = re.compile(r"\btrust\b", re.I)
_FUND_TYPES = frozenset({"etf", "mutual fund"})
# Plaid's security subtype strings (plaid-python ``Security.subtype``) for pooled funds.
_FUND_SUBTYPES = frozenset({
    "etf", "mutual fund", "fund of funds", "real estate investment trust", "hedge fund", "private equity fund",
})
_OTHER_TYPES = frozenset({"cryptocurrency", "derivative", "loan"})


# ------------------------------------------------------------------ security info


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(getattr(value, "value", value)).strip().lower()
    return text[:MAX_TEXT] or None


def load_security_info(session: Session) -> dict[str, dict]:
    row = session.get(AppSetting, SECURITY_INFO_SETTING)
    if row is None or not row.value:
        return {}
    try:
        data = json.loads(row.value)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict] = {}
    for key, value in list(data.items())[:MAX_SECURITIES]:
        if not isinstance(key, str) or len(key) > MAX_ID or not isinstance(value, dict):
            continue
        out[key] = {
            "type": _text(value.get("type")) if isinstance(value.get("type"), str) else None,
            "subtype": _text(value.get("subtype")) if isinstance(value.get("subtype"), str) else None,
            "cash": value.get("cash") is True,
        }
    return out


def store_security_info(session: Session, securities: dict[Any, dict]) -> None:
    """Replace the stored info with that of every security still held: this sync's
    securities, plus what was stored for securities other banks' accounts hold."""
    held = set(session.scalars(select(Holding.security_id).distinct()))
    old = load_security_info(session)
    info: dict[str, dict] = {}
    for security_id in sorted(s for s in held if s and len(s) <= MAX_ID):
        sec = securities.get(security_id)
        if isinstance(sec, dict):
            info[security_id] = {
                "type": _text(sec.get("type")),
                "subtype": _text(sec.get("subtype")),
                "cash": sec.get("is_cash_equivalent") is True,
            }
        elif security_id in old:
            info[security_id] = old[security_id]
        if len(info) >= MAX_SECURITIES:
            break
    value = json.dumps(info)
    row = session.get(AppSetting, SECURITY_INFO_SETTING)
    if row is None:
        session.add(AppSetting(key=SECURITY_INFO_SETTING, value=value))
    else:
        row.value = value


# ------------------------------------------------------------------ classification


def classify(name: str | None, ticker: str | None, info: dict | None) -> tuple[str, int | None]:
    """(kind, year) for one holding; first match wins (SPEC "Investments"). Plaid's type
    decides where it can (an ``equity`` is a company's stock whatever its name says, unless its
    subtype or name says it is a fund); names only sort funds and holdings whose type isn't
    known yet."""
    name = name or ""
    info = info or {}
    kind = info.get("type")
    subtype = info.get("subtype")
    if kind == "cash" or info.get("cash") or (ticker or "").upper().startswith("CUR:"):
        return "cash", None
    equity_fund = kind == "equity" and (
        subtype in _FUND_SUBTYPES
        or bool(_EQUITY_FUND_NAME.search(name))
        or bool(_TRUST.search(name) and (_US.search(name) or _INTL.search(name) or _BOND.search(name)
                                         or _COMMODITY.search(name)))
    )
    if kind == "equity" and not equity_fund:
        return "company_stock", None
    if _CASH.search(name):
        return "cash", None
    if kind in _OTHER_TYPES or _COMMODITY.search(name):
        return "other", None
    targeted = bool(_TARGET.search(name))
    if targeted and (year := _YEAR.search(name)):
        return "target_date", int(year.group(1))
    fund_name = bool(_FUND_NAME.search(name))
    if kind is None and ticker and not fund_name:
        return "company_stock", None  # "Microsoft Corp": a plain company name with a ticker
    if kind == "fixed income" or _BOND.search(name):
        return "bond", None
    fund = kind in _FUND_TYPES or subtype in _FUND_SUBTYPES or fund_name or equity_fund
    if targeted and fund:
        return "target_date", None  # "Target Retirement Income Fund": no year in the name
    if _INTL.search(name):
        return "intl_stock", None
    if _GLOBAL.search(name):
        return "other", None  # "Vanguard Total World Stock ETF": US and international together
    if _US.search(name):
        return "us_stock", None
    if fund and _STOCK_WORDS.search(name):
        return "us_stock", None  # "Fidelity Stock Selector All Cap Fund"
    return "other", None


def _mix(groups: dict[tuple[str, int | None], dict]) -> list[dict]:
    total = sum(g["value"] for g in groups.values())
    out = []
    for (kind, year), g in groups.items():
        if g["value"] <= 0:
            continue
        names = sorted(g["names"], key=lambda n: (-g["names"][n], n.lower()))[:MAX_NAMES]
        out.append({
            "kind": kind,
            "year": year,
            "value": from_cents(g["value"]),
            "pct": round(g["value"] * 100 / total, 1) if total > 0 else 0.0,
            "names": names,
            "_cents": g["value"],
        })
    out.sort(key=lambda m: (-m["_cents"], KINDS.index(m["kind"]), m["year"] or 0))
    for m in out:
        del m["_cents"]
    return out


def _new_group() -> dict:
    return {"value": 0, "names": defaultdict(int)}


def holdings_differ(account: Account, holdings: list[Holding]) -> bool:
    """The bank's holdings add up to more than the account's balance (a stale balance): the
    mix shows what it holds, with a note."""
    held = sum(max(0, int(h.value_cents or 0)) for h in holdings)
    return bool(holdings) and held - int(account.current_balance_cents or 0) >= MIN_NOT_BROKEN_DOWN


def account_groups(account: Account, holdings: list[Holding], info: dict[str, dict]) -> dict:
    """(kind, year) -> {value cents, names: {name: cents}} for one account."""
    groups: dict[tuple[str, int | None], dict] = defaultdict(_new_group)
    held = 0
    for h in holdings:
        value = int(h.value_cents or 0)
        if value <= 0:
            continue
        key = classify(h.name, h.ticker, info.get(h.security_id))
        groups[key]["value"] += value
        groups[key]["names"][h.name or "Unknown security"] += value
        held += value
    worth = int(account.current_balance_cents or 0)
    rest = worth - held if holdings else worth
    if rest >= MIN_NOT_BROKEN_DOWN:
        groups[("not_broken_down", None)]["value"] += rest
    return groups


# ------------------------------------------------------------------ history


def _index(month: str) -> int:
    year, mon = parse_month(month)
    return year * 12 + mon - 1


def _months(first: str, last: str) -> list[str]:
    return [shift_month(first, k) for k in range(_index(last) - _index(first) + 1)]


def _history(
    accounts: list[Account], snaps: dict[int, list[tuple[dt.date, int]]], current: str
) -> tuple[list[dict], dict[str, list[dict]]]:
    first_month = {a.id: month_of(snaps[a.id][0][0]) if snaps.get(a.id) else current for a in accounts}
    if not accounts:
        return [], {}
    start = max(min(first_month.values()), shift_month(current, -(HISTORY_MONTHS - 1)))
    months = _months(start, current)
    values: dict[int, dict[str, tuple[int, bool]]] = {}
    for a in accounts:
        by_month: dict[str, int] = {}
        for day, cents in snaps.get(a.id, []):
            by_month[month_of(day)] = cents  # ordered by date: the month's last snapshot wins
        series: dict[str, tuple[int, bool]] = {}
        last = None
        for m in _months(first_month[a.id], current):
            if m == current:
                series[m] = (int(a.current_balance_cents or 0), False)  # today's worth
            elif m in by_month:
                last = by_month[m]
                series[m] = (last, False)
            else:
                series[m] = (last or 0, True)
        values[a.id] = series
    total = []
    by_account: dict[str, list[dict]] = {}
    for a in accounts:
        by_account[str(a.id)] = [
            {"month": m, "worth": from_cents(values[a.id][m][0]), "carried": values[a.id][m][1], "added": []}
            for m in months if m in values[a.id]
        ]
    for m in months:
        live = [a for a in accounts if m in values[a.id]]
        total.append({
            "month": m,
            "worth": from_cents(sum(values[a.id][m][0] for a in live)),
            "carried": any(values[a.id][m][1] for a in live),
            "added": [a.name for a in live if first_month[a.id] == m],
        })
    return total, by_account


# ------------------------------------------------------------------ GET /api/investments


def _pct(change: int, base: int) -> float | None:
    return round(change * 100 / base, 1) if base > 0 else None


def totals(session: Session, today: dt.date) -> dict:
    """Home v2's "Investments worth" tile, light (no holdings, no 61-month history):
    ``{has_accounts, worth, change|null, change_missing}``, the same numbers as
    ``overview``'s ``total`` (change vs each account's last real snapshot on or before the
    end of last month; accounts without one are listed and left out of the change)."""
    _, last_end = month_bounds(shift_month(month_of(today), -1))
    accounts = sorted(
        session.execute(
            select(Account.id, Account.name, Account.current_balance_cents)
            .where(Account.hidden.is_(False), Account.category.in_(INVESTMENT_CATEGORIES))
        ).all(),
        key=lambda a: (-int(a.current_balance_cents or 0), a.name.lower(), a.id),
    )
    base: dict[int, int] = {}
    if accounts:
        # Ordered by (date, id): the last row per account is the one ``overview`` picks.
        for account_id, cents in session.execute(
            select(BalanceSnapshot.account_id, BalanceSnapshot.balance_cents)
            .where(
                BalanceSnapshot.account_id.in_([a.id for a in accounts]),
                BalanceSnapshot.estimated.is_(False),
                BalanceSnapshot.date <= last_end,
            )
            .order_by(BalanceSnapshot.date, BalanceSnapshot.id)
        ):
            base[account_id] = int(cents)
    worth = change = 0
    missing: list[str] = []
    for a in accounts:
        cents = int(a.current_balance_cents or 0)
        worth += cents
        if a.id in base:
            change += cents - base[a.id]
        else:
            missing.append(a.name)
    return {
        "has_accounts": bool(accounts),
        "worth": from_cents(worth),
        "change": from_cents(change) if len(missing) < len(accounts) else None,
        "change_missing": missing,
    }


def overview(session: Session, today: dt.date) -> dict:
    current = month_of(today)
    last_month = shift_month(current, -1)
    _, last_end = month_bounds(last_month)
    accounts = list(session.scalars(
        select(Account).where(Account.hidden.is_(False), Account.category.in_(INVESTMENT_CATEGORIES))
    ))
    accounts.sort(key=lambda a: (-int(a.current_balance_cents or 0), a.name.lower(), a.id))
    ids = [a.id for a in accounts]
    snaps: dict[int, list[tuple[dt.date, int]]] = defaultdict(list)
    if ids:
        for account_id, day, cents in session.execute(
            select(BalanceSnapshot.account_id, BalanceSnapshot.date, BalanceSnapshot.balance_cents)
            .where(
                BalanceSnapshot.account_id.in_(ids), BalanceSnapshot.estimated.is_(False),
                BalanceSnapshot.date <= today,
            )
            .order_by(BalanceSnapshot.date, BalanceSnapshot.id)
        ):
            snaps[account_id].append((day, int(cents)))
    holdings: dict[int, list[Holding]] = defaultdict(list)
    if ids:
        for h in session.scalars(select(Holding).where(Holding.account_id.in_(ids)).order_by(Holding.id)):
            holdings[h.account_id].append(h)
    items = {i.id: i for i in session.scalars(select(PlaidItem))}
    info = load_security_info(session)

    out_accounts = []
    all_groups: dict[tuple[str, int | None], dict] = defaultdict(_new_group)
    total_worth = total_change = total_base = 0
    have_change = False
    missing: list[str] = []
    since_total: int | None = None
    tracked: list[str] = []
    for a in accounts:
        worth = int(a.current_balance_cents or 0)
        total_worth += worth
        history = snaps.get(a.id, [])
        base = next((cents for day, cents in reversed(history) if day <= last_end), None)
        change = worth - base if base is not None else None
        if change is None:
            missing.append(a.name)
        else:
            have_change = True
            total_change += change
            total_base += base
        since = worth - history[0][1] if history else None
        if since is not None:
            since_total = (since_total or 0) + since
            tracked.append(month_of(history[0][0]))
        groups = account_groups(a, holdings.get(a.id, []), info)
        for key, g in groups.items():
            all_groups[key]["value"] += g["value"]
            for name, cents in g["names"].items():
                all_groups[key]["names"][name] += cents
        item = items.get(a.item_id) if a.item_id is not None else None
        out_accounts.append({
            "id": a.id,
            "name": a.name,
            "institution_name": a.institution_name,
            "category": a.category,
            "source": a.source,
            "worth": from_cents(worth),
            "change": from_cents(change),
            "change_pct": _pct(change, base) if change is not None else None,
            "tracked_since": month_of(history[0][0]) if history else None,
            "change_since_tracking": from_cents(since),
            "updated_at": iso_utc(a.updated_at),
            "connection": {
                "item_id": item.id,
                "status": item.status,
                "kind": item.kind,
                "last_synced_at": iso_utc(item.last_synced_at),
            } if item is not None else None,
            "holdings_known": bool(holdings.get(a.id)),
            "holdings_differ": holdings_differ(a, holdings.get(a.id, [])),
            "mix": _mix(groups),
        })
    total_history, by_account = _history(accounts, snaps, current)
    updated = [a.updated_at for a in accounts if a.updated_at is not None]
    return {
        "as_of": iso_utc(max(updated)) if updated else None,
        "last_month": last_month,
        "total": {
            "worth": from_cents(total_worth),
            "change": from_cents(total_change) if have_change else None,
            "change_pct": _pct(total_change, total_base) if have_change else None,
            "change_missing": missing,
            "tracked_since": min(tracked) if tracked else None,
            "change_since_tracking": from_cents(since_total),
        },
        "accounts": out_accounts,
        "mix": _mix(all_groups),
        "holdings_differ": any(a["holdings_differ"] for a in out_accounts),
        "history": {"total": total_history, "by_account": by_account},
    }

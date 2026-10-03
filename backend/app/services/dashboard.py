"""Home v2 (``GET /api/dashboard``, SPEC "Home v2 (Dashboard v2 handoff, Release 3.9)").

Everything the card-grid Home needs, in one call. Every section is built field by field,
never from whole rows: no access tokens, no backup folder path (only its own name), no Plaid
free text (only error codes matching ``^[A-Z0-9_]{1,64}$``), account masks only. A section
that fails is null and named in ``errors`` (only its exception type is logged), so the page
can offer "Try again" on that card alone; the rest still loads.

The envelope month (``spending.budget_month``) and the cash calendar
(``calendar.build_calendar(today, today + 35)``) are built once and shared: the budget card,
the goals card and ``over_plan`` read the first; "Coming up", ``low_balance`` and
``bill_due`` read the second, so Home, the Budget page and the Bills calendar agree.

"Things that need you" (``needs``) is computed live at every load (not from alert events,
except ``big_purchase``), filtered by the Settings > Alerts switches, and sorted act → warn →
info, then by kind in ``KIND_ORDER``. "Not now" is stored in the vault as
``app_settings.home_dismissed`` = ``{key: fingerprint}``; the client hides a dismissible
alert while its fingerprint matches, so it comes back when something changes. The two
roll-ups are the exception: after "Not now" on ``over_plan:many`` or ``newrec:many`` the
server leaves out what the user already put off (see ``_over_plan_alerts``, ``_new_recurring_alerts``).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
from collections.abc import Callable
from typing import Any, TypeVar

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..models import Account, AlertEvent, AlertSetting, PlaidItem, RecurringItem, Transaction
from ..seeds import ALERT_SEEDS
from ..utils import LIABILITY_CATEGORIES, from_cents, iso_utc, month_of, shift_month, to_cents, utcnow
from . import automation, calendar, goals, investments, price_ask, spending
from . import plaid_keys as keys_service
from .accounts import age_days, balance_day, is_stale, last_balance_dates
from .alerts import disabled_keys
from .automation import _get, _put
from .categories import load as load_categories
from .forecast import forecast_account
from .recurring import display_name
from .txns import needs_category_clause

log = logging.getLogger("fintrack.dashboard")

T = TypeVar("T")

DISMISSED_KEY = "home_dismissed"
MAX_DISMISSED = 100
# Dismissal keys: "needs_category", "bank_error:3", "over_plan:c_7", "low_balance:11", "newrec:many".
KEY_RE = re.compile(r"[a-z_]{1,32}(?::[A-Za-z0-9_.-]{1,40})?")
FINGERPRINT_RE = re.compile(r"[A-Za-z0-9:._-]{1,64}")
_PLAID_CODE_RE = re.compile(r"[A-Z0-9_]{1,64}")
_BIG_KEY_RE = re.compile(r"big:([0-9]{1,18})")
_NEWREC_MANY_RE = re.compile(r"r([0-9]{1,18})")
# What was over when "Not now" was pressed on over_plan:many (read_over_snapshot).
OVER_SNAPSHOT_KEY = "home_dismissed_over"

TONE_ORDER = {"act": 0, "warn": 1, "info": 2}
KIND_ORDER = ("bank_signin", "bank_error", "finish_setup", "low_balance", "over_plan", "backup_failed",
              "bill_due", "card_due", "update_balance", "price_change", "new_recurring", "needs_category",
              "big_purchase")
SECTIONS = ("cash", "budget", "coming_up", "months", "goals", "investments", "needs")

CALENDAR_DAYS = 35  # the shared calendar: today .. today + 35 (the low-balance look-ahead)
COMING_DAYS = 7  # "Coming up": today .. today + 7
MAX_COMING_ROWS = 8
FULL_MONTHS = 5  # "Left over each month": 5 full months + this month so far
MANY = 3  # more than this many over_plan / new_recurring -> one "many" alert
NEWREC_DAYS = 30
BIG_DAYS = 3
BIG_SCAN = 50
MAX_BIG = 10
MAX_BILLS_DUE = 10
MAX_CARDS_DUE = 10
MAX_UPDATE_BALANCE = 10
MAX_PRICE_CHANGES = 10
MAX_DUE_DAYS = 31
NAMES_SHOWN = 3


def _plaid_code(value: str | None) -> str | None:
    if value is None:
        return None
    return value if _PLAID_CODE_RE.fullmatch(value) else "UNKNOWN"


def _alert(key: str, kind: str, tone: str, fingerprint: str, data: dict, *, dismissible: bool = True) -> dict:
    return {"key": key, "kind": kind, "tone": tone, "dismissible": dismissible,
            "fingerprint": fingerprint, "data": data}


def _iso(day: dt.date) -> str:
    return day.isoformat()


# ------------------------------------------------------------------ setup, banks, cash


def items(session: Session) -> list[Any]:
    """Plaid items, column by column (the access token is never even loaded)."""
    return list(session.execute(
        select(
            PlaidItem.id, PlaidItem.institution_name, PlaidItem.kind, PlaidItem.status,
            PlaidItem.error_code, PlaidItem.last_synced_at, PlaidItem.last_error_at,
        ).order_by(PlaidItem.id)
    ))


def banks(rows: list[Any]) -> list[dict]:
    return [
        {
            "item_id": i.id,
            "institution_name": i.institution_name,
            "kind": i.kind,
            "status": i.status,
            "error_code": _plaid_code(i.error_code),
            "last_synced_at": iso_utc(i.last_synced_at),
        }
        for i in rows
    ]


def _keys_rejected(session: Session, state: Any) -> bool:
    """The vault's saved keys failed their last check for a reason only new keys fix."""
    if state.settings.plaid_configured or keys_service.read_meta(session) is None:
        return False
    last = keys_service.read_last_test(session)
    return last is not None and not last["ok"] and last["code"] not in ("unreachable", "plaid_error")


def setup(session: Session, state: Any, plaid: Any, rows: list[Any]) -> dict:
    configured = bool(plaid.configured)
    pending = sum(1 for i in rows if i.status == "pending")
    return {
        "plaid_configured": configured,
        # As GET /api/plaid/status ("env" | "vault" | "none"); an injected test client has no source.
        "plaid_source": getattr(plaid, "source", None) or ("env" if configured else "none"),
        "keys_rejected": _keys_rejected(session, state),
        "visible_accounts": session.scalar(
            select(func.count()).select_from(Account).where(Account.hidden.is_(False))
        ) or 0,
        "linked_items": len(rows) - pending,
        "pending_items": pending,
    }


_CASH_GROUP = {"checking": 0, "savings": 1}


def cash(session: Session, rows: list[Any]) -> dict:
    """Checking and savings: every non-hidden account in the ``bank`` category. Listed as the
    page shows them: the forecast (checking) account first, then checking, savings, and other
    cash accounts (manual ones without a type included), each group by name."""
    accounts = session.execute(
        select(
            Account.id, Account.name, Account.mask, Account.institution_name,
            Account.current_balance_cents, Account.source, Account.item_id, Account.plaid_subtype,
        )
        .where(Account.category == "bank", Account.hidden.is_(False))
        .order_by(Account.name, Account.id)
    ).all()
    main = forecast_account(session)
    main_id = main.id if main is not None else None
    accounts = sorted(accounts, key=lambda r: (
        r.id != main_id, _CASH_GROUP.get((r.plaid_subtype or "").lower(), 2), r.name.lower(), r.id,
    ))
    synced = {i.id: i.last_synced_at for i in rows}
    times = [synced[r.item_id] for r in accounts if r.item_id in synced and synced[r.item_id] is not None]
    return {
        "total": from_cents(sum(r.current_balance_cents for r in accounts)),
        "updated_at": iso_utc(max(times)) if times else None,
        "accounts": [
            {
                "id": r.id,
                "name": r.name,
                "mask": r.mask,
                "institution_name": r.institution_name,
                "balance": from_cents(r.current_balance_cents),
                "source": "plaid" if r.source == "plaid" else "manual",
                "item_id": r.item_id,
                "subtype": r.plaid_subtype,
            }
            for r in accounts
        ],
    }


# ------------------------------------------------------------------ budget ("Left to spend")


def _spending_lines(bm: dict) -> list[dict]:
    """This month's envelopes that are money to spend: not the Emergency savings category
    (``savings_category``), no goal category (D8) and not "Paying off debt" (Release 3.10):
    those are money set aside."""
    return [
        c for c in bm["categories"]
        if c["category"] != bm["savings_category"] and c.get("goal") is None and c.get("debt") is None
    ]


def budget(bm: dict) -> dict:
    """As GET /api/budgets for today's month: left = available, planned = available + spent
    (so left = planned − spent, negative when over), plus the next expected paycheck
    (``income.next``: the first upcoming paycheck on the budget accounts, moves and skips
    applied)."""
    lines = _spending_lines(bm)
    available = sum(to_cents(c["available"]) for c in lines)
    spent = sum(to_cents(c["spent"]) for c in lines)
    nxt = bm["income"].get("next")
    return {
        "month": bm["month"],
        "is_current": bm["is_current"],
        "days_left": bm["days_left"],
        "has_budget": bool(lines),
        "planned": from_cents(available + spent),
        "spent": from_cents(spent),
        "left": from_cents(available),
        "next_paycheck": {"date": nxt["date"], "name": nxt["name"], "amount": nxt["amount"]} if nxt else None,
    }


# ------------------------------------------------------------------ coming up (next 7 days)


def _still_expected(o: dict) -> bool:
    """Late or pending, not paid yet, and counted tomorrow by the projection."""
    return bool(o["carried"] and o["counted"] and o["status"] in ("late", "pending"))


def _account_names(cal: dict) -> dict[str, str | None]:
    """series id -> the account its occurrences use (items without one follow checking)."""
    default = cal["account"]["name"] if cal["account"] else None
    out: dict[str, str | None] = {}
    for s in cal["series"]:
        if s["recurring_id"] is None:
            out[s["id"]] = None
        else:
            out[s["id"]] = s["account"]["name"] if s["account"] else default
    return out


def coming_up(cal: dict, today: dt.date, warning_on: bool) -> dict:
    """"Coming up": the calendar's rows from today to today + 7 and checking's balance after
    each one.

    Rows: upcoming occurrences dated in [today, to] that the projection counts, plus card
    charges (never counted: "Not from checking"), plus late/pending ones still expected
    (listed first). Sorted by the day they count (``counted_on``, else the date), then by
    their own date, money in first, then by name. ``after`` = checking after that row,
    consistent with the calendar's day balances: a day opens at the previous day's balance
    less the everyday-spending amount (when that setting is on), and the day's last counted
    row (``day_end: true``) ends at that day's ``days[].balance``. ``low`` = the lowest day
    in [today, to] (earliest on ties); ``first_below`` = the first day under the warning
    amount (null with the warning off). The page flags "Below your warning" only on
    ``day_end`` rows, so rows, note and Bills calendar all judge by day-end balances.
    """
    to = today + dt.timedelta(days=COMING_DAYS)
    out: dict = {
        "account": None, "from": _iso(today), "to": _iso(to), "balance": None,
        "threshold": cal["threshold"], "warning_on": warning_on,
        "daily_spend": cal["daily_spend"] if cal["include_daily"] else None,
        "rows": [], "more": 0, "low": None, "first_below": None,
    }
    account = cal["account"]
    if account is None:
        return out
    out["account"] = {k: account[k] for k in ("id", "name", "mask", "institution_name")}
    out["balance"] = cal["balance"]
    t_iso, to_iso = _iso(today), _iso(to)
    names = _account_names(cal)
    picked = []
    for o in cal["occurrences"]:
        expected = _still_expected(o)
        coming = (
            o["status"] == "upcoming" and t_iso <= o["date"] <= to_iso
            and (o["kind"] == "card" or o["counted"])
        )
        if expected or coming:
            picked.append((o, expected))
    # Within a counted day, by the row's own date first: a bill due today (unpaid, so counted
    # tomorrow) comes before tomorrow's paycheck, and its ``after`` doesn't include that pay.
    picked.sort(key=lambda p: (
        0 if p[1] else 1, p[0]["counted_on"] or p[0]["date"], p[0]["date"],
        calendar.KIND_ORDER.get(p[0]["kind"], 9), p[0]["name"].lower(), p[0]["key"],
    ))

    balances = {d["date"]: to_cents(d["balance"]) for d in cal["days"] if d["balance"] is not None}
    daily = to_cents(cal["daily_spend"]) if cal["include_daily"] else 0
    running: dict[str, int] = {}
    rows = []
    for o, expected in picked:
        after = None
        day = o["counted_on"] if o["counted"] else None
        if day is not None:
            if day not in running:
                before = _iso(dt.date.fromisoformat(day) - dt.timedelta(days=1))
                running[day] = balances[before] - daily
            running[day] += to_cents(o["amount"])
            after = from_cents(running[day])
        rows.append({
            "key": o["key"],
            "recurring_id": o["recurring_id"],
            "date": o["date"],
            "counted_on": o["counted_on"],
            "name": o["name"],
            "kind": o["kind"],
            "account_name": names.get(o["series"]) if o["recurring_id"] is not None else None,
            "amount": o["amount"],
            "status": o["status"],
            "still_expected": expected,
            "after": after,
            "day_end": False,
        })
    # The last counted row of each day: its ``after`` is that day's calendar balance, the
    # one the note, ``low``/``first_below`` and the Bills calendar use. Marked before rows
    # are cut to 8, so a day cut short has no ``day_end`` row.
    last_of_day: dict[str, dict] = {}
    for row in rows:
        if row["after"] is not None:
            last_of_day[row["counted_on"]] = row
    for row in last_of_day.values():
        row["day_end"] = True
    out["rows"] = rows[:MAX_COMING_ROWS]
    out["more"] = max(0, len(rows) - MAX_COMING_ROWS)

    week = [d for d in cal["days"] if t_iso <= d["date"] <= to_iso and d["balance"] is not None]
    if week:
        low = min(week, key=lambda d: (to_cents(d["balance"]), d["date"]))
        out["low"] = {"date": low["date"], "balance": low["balance"]}
        threshold = to_cents(cal["threshold"])
        below = next((d for d in week if to_cents(d["balance"]) < threshold), None)
        if warning_on and below is not None:
            out["first_below"] = {"date": below["date"], "balance": below["balance"]}
    return out


# ------------------------------------------------------------------ months ("Left over each month")


def months(session: Session, today: dt.date) -> list[dict]:
    """The last 5 full months and this month so far, oldest first, with the Reports
    definitions (every visible bank, card, HSA and other account; splits count by part;
    transfers out): came_in = income, went_out = spending + fixed (loan payments).
    ``has_data``: on or after the month of the first transaction on those accounts.
    ``partial``: that first month, when its first transaction is after the 1st (history
    started mid-month, so its totals aren't a whole month; the page mutes it and leaves it
    out of its note)."""
    cmap = load_categories(session)
    current = month_of(today)
    first = shift_month(current, -FULL_MONTHS)
    by_month = spending.totals_by_month(session, cmap, first, current)
    first_day = spending.first_data_day(session)
    first_month = month_of(first_day) if first_day is not None else None
    out = []
    for month, t in by_month.items():
        came_in, went_out = t.income, t.total_spent + t.total_fixed
        out.append({
            "month": month,
            "came_in": from_cents(came_in),
            "went_out": from_cents(went_out),
            "left_over": from_cents(came_in - went_out),
            "complete": month != current,
            "has_data": first_month is not None and month >= first_month,
            "partial": month == first_month and first_day.day > 1,
        })
    return out


# ------------------------------------------------------------------ things that need you


def _bank_alerts(rows: list[Any]) -> list[dict]:
    out = []
    for i in rows:
        data = {"item_id": i.id, "item_kind": i.kind, "institution_name": i.institution_name,
                "error_code": _plaid_code(i.error_code)}
        if i.status == "login_required":
            out.append(_alert(f"bank_signin:{i.id}", "bank_signin", "act", "", data, dismissible=False))
        elif i.status == "error":
            # "Not now" lasts until the next failed sync attempt (a new last_error_at) or another
            # code. Items that failed before v5 have no last_error_at: fall back to the last sync.
            when = iso_utc(i.last_error_at) or iso_utc(i.last_synced_at) or "never"
            fingerprint = f"{(data['error_code'] or 'ERROR')[:40]}:{when}"
            out.append(_alert(f"bank_error:{i.id}", "bank_error", "warn", fingerprint, data))
    for i in rows:
        if i.status == "pending":
            out.append(_alert(f"finish_setup:{i.id}", "finish_setup", "info", str(i.id),
                              {"item_id": i.id, "institution_name": i.institution_name}))
    return out


def _backup_alerts(backup: dict) -> list[dict]:
    if not backup["enabled"] or backup["last_error_at"] is None:
        return []
    return [_alert("backup_failed", "backup_failed", "warn", backup["last_error_at"], {
        "code": backup["last_error_code"], "folder_name": backup["folder_name"], "at": backup["last_error_at"],
    })]


def _needs_category_alerts(session: Session, today: dt.date) -> list[dict]:
    """This month's transactions needing a category (the same rows as the Transactions link:
    ``view=needs_category&start=<month start>``); the newest one's id is the fingerprint."""
    start = today.replace(day=1)
    count, newest = session.execute(
        select(func.count(), func.max(Transaction.id))
        .select_from(Transaction)
        .where(Transaction.date >= start, needs_category_clause())
    ).one()
    if not count:
        return []
    return [_alert("needs_category", "needs_category", "info", f"t{newest}",
                   {"count": int(count), "month_start": start.isoformat()})]


def _low_balance_alerts(cal: dict, today: dt.date) -> list[dict]:
    """Checking is projected under the warning amount within 35 days (the calendar's first
    dip; today when it already is). Keyed by account; comes back when the dip date moves."""
    account, dip = cal["account"], cal["first_dip"]
    if account is None or dip is None or to_cents(cal["threshold"]) <= 0:
        return []
    if not (_iso(today) <= dip["date"] <= _iso(today + dt.timedelta(days=CALENDAR_DAYS))):
        return []
    cause = next((o for o in cal["occurrences"] if o["key"] == dip["cause_key"]), None) if dip["cause_key"] else None
    return [_alert(f"low_balance:{account['id']}", "low_balance", "warn", dip["date"], {
        "account_id": account["id"], "account_name": account["name"], "date": dip["date"],
        "balance": dip["balance"], "threshold": cal["threshold"], "cause": cause["name"] if cause else None,
    })]


def _over_lines(bm: dict) -> list[tuple[dict, int, int]]:
    """(line, planned cents, spent cents) for this month's envelopes that are over."""
    over = []
    for c in _spending_lines(bm):
        planned = to_cents(c["carryover"]) + to_cents(c["assigned"])
        spent = to_cents(c["spent"])
        # Only envelopes with money planned, as the ``budget`` alert: spending with no plan
        # shows on the Budget page under "Spending without a plan".
        if planned > 0 and spent > planned:
            over.append((c, planned, spent))
    return over


def _over_amounts(over: list[tuple[dict, int, int]]) -> dict[str, int]:
    return {c["category"]: spent - planned for c, planned, spent in over}


def _many_fingerprint(month: str, over: list[tuple[dict, int, int]]) -> str:
    """``YYYY-MM:{count}:{digest}``: changes whenever the set of over categories or any
    over amount changes (so a plain "Not now" comes back on any change)."""
    amounts = _over_amounts(over)
    text = ";".join(f"{k}={amounts[k]}" for k in sorted(amounts))
    return f"{month}:{len(over)}:{hashlib.sha256(text.encode()).hexdigest()[:8]}"


def read_over_snapshot(session: Session) -> dict | None:
    """What was over when "Not now" was pressed on ``over_plan:many``:
    ``{"month", "fp", "over": {category id: cents over}}``, or None (missing or malformed)."""
    raw = _get(session, OVER_SNAPSHOT_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    month, fp, over = data.get("month"), data.get("fp"), data.get("over")
    if not (isinstance(month, str) and isinstance(fp, str) and isinstance(over, dict)):
        return None
    if not all(isinstance(k, str) and type(v) is int for k, v in over.items()):
        return None
    return {"month": month, "fp": fp, "over": over}


def _snapshot_over(session: Session, fingerprint: str, today: dt.date) -> str | None:
    """The snapshot to keep for a ``over_plan:many`` "Not now" (JSON), when the fingerprint is
    this month's current one; else None (then only the plain fingerprint rule applies)."""
    try:
        bm = spending.budget_month(session, month_of(today), today)
    except Exception as exc:  # noqa: BLE001 - "Not now" must still be saved
        session.rollback()
        log.error("over-plan snapshot failed: %s", type(exc).__name__)
        return None
    over = _over_lines(bm)
    if len(over) <= MANY or _many_fingerprint(bm["month"], over) != fingerprint:
        return None
    return json.dumps({"month": bm["month"], "fp": fingerprint, "over": _over_amounts(over)})


def _over_plan_alerts(bm: dict, dismissed_many: str | None = None, snapshot: dict | None = None) -> list[dict]:
    """This month's spending envelopes where spent > carried over + planned (the Budget
    page's "over") with something planned; never Emergency savings, goal categories or "Paying off
    debt" (Release 3.10). More than 3 → one alert
    (``names``: the 3 furthest over).

    After "Not now" on this month's ``over_plan:many`` (``snapshot`` matches the dismissed
    fingerprint), nothing is shown again this month, singly or as a smaller "many", until
    something gets worse: a category that wasn't over goes over, or one grows past the
    amount it was over by then."""
    over = _over_lines(bm)
    month = bm["month"]
    if (
        snapshot is not None and dismissed_many is not None and snapshot["fp"] == dismissed_many
        and snapshot["month"] == month
    ):
        before = snapshot["over"]
        worse = any(cat not in before or cents > before[cat] for cat, cents in _over_amounts(over).items())
        if not worse:
            return []
    if len(over) > MANY:
        return [_alert("over_plan:many", "over_plan", "warn", _many_fingerprint(month, over), {
            "count": len(over), "month": month,
            "total_over": from_cents(sum(s - p for _c, p, s in over)),
            # The biggest overspending first (then the Budget's order).
            "names": [c["name"] for c, _p, _s in sorted(over, key=lambda o: o[1] - o[2])[:NAMES_SHOWN]],
        })]
    return [
        _alert(f"over_plan:{c['category']}", "over_plan", "warn", month, {
            "category_id": c["category"], "name": c["name"], "planned": from_cents(planned),
            "spent": from_cents(spent), "over": from_cents(spent - planned), "month": month,
        })
        for c, planned, spent in over
    ]


def _bill_due_alerts(cal: dict, today: dt.date) -> list[dict]:
    """Bills and card charges (money out only) with "Remind me" on, from N days before their (moved) date through
    the day, not paid or skipped: the nearest one per item (the ``reminder`` alert's rule)."""
    if cal["account"] is None:
        return []
    names = _account_names(cal)
    seen: set[int] = set()
    out = []
    for o in sorted(cal["occurrences"], key=lambda o: (o["date"], o["key"])):
        n = o["reminder_days"]
        rid = o["recurring_id"]
        if rid is None or n <= 0 or rid in seen or o["status"] != "upcoming" or o["amount"] >= 0:
            continue
        when = dt.date.fromisoformat(o["date"])
        if not (when - dt.timedelta(days=n) <= today <= when):
            continue
        seen.add(rid)
        out.append(_alert(f"bill_due:{rid}", "bill_due", "info", o["base_date"], {
            "recurring_id": rid, "name": o["name"], "date": o["date"], "amount": abs(o["amount"]),
            "kind": o["kind"], "account_name": names.get(o["series"]), "days": (when - today).days,
        }))
        if len(out) >= MAX_BILLS_DUE:
            break
    return out


def _card_due_alerts(session: Session, today: dt.date) -> list[dict]:
    """Card and loan payments due within the ``due`` alert's days (as that alert), for
    accounts that owe something (balance > 0) and whose minimum payment isn't $0."""
    setting = session.get(AlertSetting, "due")
    raw = setting.value if setting is not None and setting.value is not None else ALERT_SEEDS["due"][0]
    days = max(0, min(MAX_DUE_DAYS, int(raw)))
    rows = session.execute(
        select(
            Account.id, Account.name, Account.mask, Account.institution_name, Account.category,
            Account.next_payment_due, Account.minimum_payment_cents,
        )
        .where(
            Account.hidden.is_(False),
            Account.category.in_(LIABILITY_CATEGORIES),
            Account.next_payment_due.is_not(None),
            Account.next_payment_due >= today,
            Account.next_payment_due <= today + dt.timedelta(days=days),
            # Nothing to pay: nothing owed (0 or a credit), or the bank says the minimum is $0.
            Account.current_balance_cents > 0,
            or_(Account.minimum_payment_cents.is_(None), Account.minimum_payment_cents != 0),
        )
        .order_by(Account.next_payment_due, Account.id)
        .limit(MAX_CARDS_DUE)
    ).all()
    return [
        _alert(f"card_due:{r.id}", "card_due", "info", _iso(r.next_payment_due), {
            "account_id": r.id, "name": r.name, "mask": r.mask, "institution_name": r.institution_name,
            "due_date": _iso(r.next_payment_due), "days": (r.next_payment_due - today).days,
            "minimum_payment": from_cents(r.minimum_payment_cents) if r.minimum_payment_cents else None,
            "account_category": r.category,
        })
        for r in rows
    ]


def _update_balance_alerts(session: Session, today: dt.date) -> list[dict]:
    """Release 3.10: visible accounts the user adds by hand whose balance was last typed in 7 or
    more days ago (30 for "other" things the user owns, like a house), stalest first. The fingerprint
    is that date, so "Not now" lasts until they type in a new balance."""
    rows = session.execute(
        select(Account.id, Account.name, Account.mask, Account.institution_name, Account.category,
               Account.created_at)
        .where(Account.source == "manual", Account.hidden.is_(False))
        .order_by(Account.id)
    ).all()
    snaps = last_balance_dates(session, [r.id for r in rows])
    days = {r.id: balance_day(snaps.get(r.id), r.created_at) for r in rows}
    stale = [(r, days[r.id]) for r in rows if is_stale("manual", age_days(days[r.id], today), r.category)]
    stale.sort(key=lambda p: (p[1] or dt.date.min, p[0].id))
    return [
        _alert(f"update_balance:{r.id}", "update_balance", "warn", _iso(day) if day else "none", {
            "account_id": r.id, "name": r.name, "mask": r.mask, "institution_name": r.institution_name,
            "days": age_days(day, today), "balance_date": _iso(day) if day else None,
            "account_category": r.category,
        })
        for r, day in stale[:MAX_UPDATE_BALANCE]
    ]


def _new_recurring_alerts(session: Session, dismissed_many: str | None = None) -> list[dict]:
    """Repeating charges (money out) FinTrack found in the last 30 days that the user hasn't
    confirmed or dismissed (status ``suggested``), on a visible account or none. More than 3
    → one alert.

    After "Not now" on ``newrec:many`` (fingerprint ``r{newest id}``), items no newer than
    that id aren't shown again (singly or as a smaller "many"); only newer ones count."""
    since = utcnow() - dt.timedelta(days=NEWREC_DAYS)
    found = session.execute(
        select(
            RecurringItem.id, RecurringItem.name, RecurringItem.amount_cents, RecurringItem.cadence,
            Account.name.label("account_name"),
        )
        .outerjoin(Account, Account.id == RecurringItem.account_id)
        .where(
            RecurringItem.status == "suggested", RecurringItem.created_at >= since,
            RecurringItem.amount_cents < 0,  # charges only ("New repeating charge")
            or_(RecurringItem.account_id.is_(None), Account.hidden.is_(False)),
        )
        .order_by(RecurringItem.id)
    ).all()
    seen = _NEWREC_MANY_RE.fullmatch(dismissed_many or "")
    if seen is not None:
        found = [r for r in found if r.id > int(seen.group(1))]
    if len(found) > MANY:
        return [_alert("newrec:many", "new_recurring", "info", f"r{found[-1].id}", {
            "count": len(found), "names": [r.name for r in found[:NAMES_SHOWN]],
        })]
    return [
        _alert(f"newrec:{r.id}", "new_recurring", "info", str(r.id), {
            "recurring_id": r.id, "name": r.name, "amount": from_cents(abs(r.amount_cents)),
            "kind": "out", "cadence": r.cadence, "account_name": r.account_name,
        })
        for r in found
    ]


def _price_change_alerts(session: Session) -> list[dict]:
    """Release 3.19: "Netflix now charges $17.99. Update your amount?" (money in: "Acme
    Payroll came in at $2,410.00. …") for bills and paychecks whose amount the user set by
    hand (``price_ask.open_questions``: none on hidden accounts). Yes / No, and "Not now" (the
    fingerprint is the charge id, so the next charge comes back); newest charge first, at most 10."""
    asks = price_ask.open_questions(session)
    if not asks:
        return []
    items = {i.id: i for i in session.scalars(select(RecurringItem).where(RecurringItem.id.in_(list(asks))))}
    rows = sorted(asks.items(), key=lambda p: (p[1]["date"], p[1]["transaction_id"]), reverse=True)
    return [
        _alert(f"price:{item_id}", "price_change", "warn", str(q["transaction_id"]), {
            "recurring_id": item_id, "name": items[item_id].name, "amount": abs(q["amount"]),
            "current": from_cents(abs(items[item_id].amount_cents)), "income": items[item_id].amount_cents > 0,
            "date": q["date"], "transaction_id": q["transaction_id"],
        })
        for item_id, q in rows[:MAX_PRICE_CHANGES]
    ]


def _big_purchase_alerts(session: Session) -> list[dict]:
    """"Was this you?": large purchase events of the last 3 days (not cleared), newest first,
    for purchases still on a visible account. When a pending purchase posts, the sync moves
    its event to the posted copy (``sync._move_big_event``), so it stays with the posted
    amount. An event whose transaction is gone (the bank removed it, or it was deleted) is
    left out: its stored text would describe a purchase that no longer exists. So is one
    whose transaction is now a transfer (Release 3.9.1: a credit card payment marked after
    the event fired, e.g. by the one-time pass): moving their own money isn't a purchase."""
    since = utcnow() - dt.timedelta(days=BIG_DAYS)
    events = session.execute(
        select(AlertEvent.id, AlertEvent.dedupe_key)
        .where(AlertEvent.key == "big", AlertEvent.cleared.is_(False), AlertEvent.created_at >= since)
        .order_by(AlertEvent.created_at.desc(), AlertEvent.id.desc())
        .limit(BIG_SCAN)
    ).all()
    out = []
    for event_id, dedupe in events:
        m = _BIG_KEY_RE.fullmatch(dedupe or "")
        if m is None:
            continue
        row = session.execute(
            select(
                Transaction.id, Transaction.date, Transaction.name, Transaction.merchant_name,
                Transaction.amount_cents, Account.name.label("account_name"), Account.mask,
            )
            .join(Account, Account.id == Transaction.account_id)
            .where(Transaction.id == int(m.group(1)), Account.hidden.is_(False), Transaction.is_transfer.is_(False))
        ).first()
        if row is None or row.amount_cents >= 0:
            continue
        out.append(_alert(f"big:{event_id}", "big_purchase", "info", str(event_id), {
            "event_id": event_id, "transaction_id": row.id, "name": display_name(row.merchant_name, row.name),
            "amount": from_cents(-row.amount_cents), "date": _iso(row.date),
            "account_name": row.account_name, "account_mask": row.mask,
        }))
        if len(out) >= MAX_BIG:
            break
    return out


def sort_needs(alerts: list[dict]) -> list[dict]:
    return sorted(alerts, key=lambda a: (TONE_ORDER[a["tone"]], KIND_ORDER.index(a["kind"])))


# ------------------------------------------------------------------ dismissals


def read_dismissed(session: Session) -> dict[str, str]:
    """``{key: fingerprint}``, oldest first; malformed entries (or JSON) are ignored."""
    raw = _get(session, DISMISSED_KEY)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        log.warning("ignoring unreadable home dismissals")
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        k: v for k, v in data.items()
        if isinstance(v, str) and KEY_RE.fullmatch(k) and FINGERPRINT_RE.fullmatch(v)
    }


def _write_dismissed(session: Session, dismissed: dict[str, str]) -> None:
    _put(session, DISMISSED_KEY, json.dumps(dismissed) if dismissed else None)


def dismiss(session: Session, key: str, fingerprint: str, today: dt.date | None = None) -> None:
    """Remember "Not now" for ``key`` (newest last; the oldest are dropped past MAX_DISMISSED).

    For ``over_plan:many`` (with ``today``) it also keeps what was over at that moment
    (``OVER_SNAPSHOT_KEY``), so fixing some categories doesn't bring the others back."""
    if key == "over_plan:many" and today is not None:
        _put(session, OVER_SNAPSHOT_KEY, _snapshot_over(session, fingerprint, today))
    dismissed = read_dismissed(session)
    dismissed.pop(key, None)
    dismissed[key] = fingerprint
    while len(dismissed) > MAX_DISMISSED:
        dismissed.pop(next(iter(dismissed)))
    _write_dismissed(session, dismissed)


def undismiss(session: Session, key: str) -> None:
    dismissed = read_dismissed(session)
    if dismissed.pop(key, None) is not None:
        _write_dismissed(session, dismissed)


# ------------------------------------------------------------------ the whole screen


def build(session: Session, state: Any, plaid: Any, today: dt.date) -> dict:
    """``DashboardData`` (SPEC Home v2)."""
    errors: list[str] = []

    def section(name: str, fn: Callable[[], T]) -> T | None:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - one card must never break the screen
            session.rollback()
            log.error("dashboard section %s failed: %s", name, type(exc).__name__)
            if name not in errors:
                errors.append(name)
            return None

    def shared(fn: Callable[[], T]) -> T | None:
        """A computation several sections use; its failure shows on the sections that need it."""
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            log.error("dashboard data failed: %s", type(exc).__name__)
            return None

    def need(fn: Callable[[], list[dict]]) -> list[dict]:
        return section("needs", fn) or []

    def missing() -> Any:
        raise LookupError("shared data unavailable")

    rows = items(session)
    backup = automation.backup_status(session)
    off = disabled_keys(session)
    bm = shared(lambda: spending.budget_month(session, month_of(today), today))
    cal = shared(lambda: calendar.build_calendar(
        session, today, today, today + dt.timedelta(days=CALENDAR_DAYS)))

    out: dict = {
        "today": today.isoformat(),
        "setup": setup(session, state, plaid, rows),
        "banks": banks(rows),
        "cash": section("cash", lambda: cash(session, rows)),
        "budget": section("budget", lambda: budget(bm) if bm is not None else missing()),
        "coming_up": section("coming_up", lambda: coming_up(cal, today, "low" not in off)
                             if cal is not None else missing()),
        "months": section("months", lambda: months(session, today)),
        # Without the shared month, the goals card builds its own (it doesn't need the rest).
        "goals": section("goals", lambda: goals.summary(session, today, bm)),
        "investments": section("investments", lambda: investments.totals(session, today)),
    }

    needs: list[dict] = []
    needs += need(lambda: _bank_alerts(rows))
    needs += need(lambda: _backup_alerts(backup))
    needs += need(lambda: _needs_category_alerts(session, today))
    if "low" not in off:
        needs += need(lambda: _low_balance_alerts(cal, today) if cal is not None else missing())
    dismissed = read_dismissed(session)
    if "budget" not in off:
        needs += need(lambda: _over_plan_alerts(
            bm, dismissed.get("over_plan:many"), read_over_snapshot(session)) if bm is not None else missing())
    if "reminder" not in off:
        needs += need(lambda: _bill_due_alerts(cal, today) if cal is not None else missing())
    if "due" not in off:
        needs += need(lambda: _card_due_alerts(session, today))
    needs += need(lambda: _update_balance_alerts(session, today))  # no Settings switch
    if "price" not in off:
        needs += need(lambda: _price_change_alerts(session))
    if "newrec" not in off:
        needs += need(lambda: _new_recurring_alerts(session, dismissed.get("newrec:many")))
    if "big" not in off:
        needs += need(lambda: _big_purchase_alerts(session))
    out["needs"] = sort_needs(needs)
    out["dismissed"] = dismissed
    out["errors"] = errors
    return out

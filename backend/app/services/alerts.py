"""Alert evaluation and the alert event log.

``evaluate`` runs after every sync, after a manual account's creation or balance
change, and after unlock. Every event has a ``dedupe_key`` and is created at most once
(cleared events are kept as scrubbed tombstones so they can't fire again).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Account, AlertEvent, AlertSetting, RecurringItem, Transaction
from ..seeds import ALERT_ORDER, ALERT_SEEDS
from ..utils import LIABILITY_CATEGORIES, fmt_usd, iso_utc, month_bounds, month_of, to_cents, utcnow
from . import calendar, prefs, price_ask
from .categories import CategoryMap
from .forecast import forecast_account
from .goal_links import goal_categories
from .price_ask import PRICE_MIN_CENTS, PRICE_MIN_SHARE  # noqa: F401 - kept here for older imports
from .recurring import (
    LOOKBACK_DAYS, display_name, follow_amount, hand_set_ids, item_aliases, load_notes, merchant_key, save_notes,
)
from .spending import RECEIVED_KINDS, Book, budget_alert_lines, budget_book, received_by_month

log = logging.getLogger("fintrack.alerts")

SEVERITY = {"low": "neg", "big": "warn", "budget": "warn", "due": "accent", "newrec": "accent",
            "price": "warn", "conn": "neg", "income": "accent", "low_ahead": "warn", "reminder": "accent"}
# Checked by the hourly tick while unlocked (they depend on the date, not on new data).
TICK_KEYS = ("low_ahead", "reminder")
LOW_AHEAD_MAX_DAYS = 31
# Only new transactions this recent raise "big" alerts, so importing years of history
# (first link, or re-including an account) doesn't flood the feed.
BIG_RECENT_DAYS = 14
INCOME_SURPLUS_MIN_CENTS = 100  # like the Budget page's "Earned more" note


def _day(d: dt.date) -> str:
    return f"{d:%b} {d.day}"


def _local_date(value: dt.datetime) -> str:
    return value.replace(tzinfo=dt.timezone.utc).astimezone().date().isoformat()


# ------------------------------------------------------------------ settings / events out


def setting_out(setting: AlertSetting, last: AlertEvent | None) -> dict:
    unit = ALERT_SEEDS.get(setting.key, (None, None))[1]
    return {
        "key": setting.key,
        "enabled": bool(setting.enabled),
        "value": setting.value if unit is not None else None,
        "unit": unit,
        "last": {"date": _local_date(last.created_at), "text": last.title} if last is not None else None,
    }


def settings_out(session: Session) -> list[dict]:
    rows = {s.key: s for s in session.scalars(select(AlertSetting))}
    out = []
    for key in ALERT_ORDER:
        setting = rows.get(key)
        if setting is None:  # re-seed a deleted row rather than failing
            value, _unit = ALERT_SEEDS[key]
            setting = AlertSetting(key=key, enabled=True, value=value)
            session.add(setting)
            session.flush()
        last = session.scalar(
            select(AlertEvent)
            .where(AlertEvent.key == key, AlertEvent.cleared.is_(False))
            .order_by(AlertEvent.created_at.desc(), AlertEvent.id.desc())
            .limit(1)
        )
        out.append(setting_out(setting, last))
    return out


# Settings D7 update 2: the two-key rows of the Alerts tab (one switch sets both).
PAIRED = {"low": ("low", "low_ahead"), "reminder": ("reminder", "due")}


def disabled_keys(session: Session) -> set[str]:
    """Alert keys turned off; their events are hidden (events list, unread count, Home)."""
    return set(session.scalars(select(AlertSetting.key).where(AlertSetting.enabled.is_(False))))


def event_out(e: AlertEvent) -> dict:
    return {
        "id": e.id,
        "key": e.key,
        "severity": e.severity,
        "title": e.title,
        "body": e.body,
        "created_at": iso_utc(e.created_at),
        "read": bool(e.read),
    }


# ------------------------------------------------------------------ evaluation


class _Emitter:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.created: list[AlertEvent] = []

    def emit(
        self, key: str, dedupe_key: str, title: str, body: str, data: dict | None = None
    ) -> AlertEvent | None:
        exists = self.session.scalar(select(AlertEvent.id).where(AlertEvent.dedupe_key == dedupe_key))
        if exists is not None:
            return None
        event = AlertEvent(
            key=key, severity=SEVERITY[key], title=title[:200], body=body[:500],
            created_at=utcnow(), read=False, dedupe_key=dedupe_key, cleared=False,
            data=json.dumps(data) if data is not None else None,
        )
        self.session.add(event)
        self.session.flush()
        self.created.append(event)
        return event

    def tombstone(self, key: str, dedupe_key: str) -> AlertEvent | None:
        """Record ``dedupe_key`` as seen without showing anything (cleared, no text)."""
        exists = self.session.scalar(select(AlertEvent.id).where(AlertEvent.dedupe_key == dedupe_key))
        if exists is not None:
            return None
        event = AlertEvent(
            key=key, severity=SEVERITY[key], title="", body="", created_at=utcnow(), read=True,
            dedupe_key=dedupe_key, cleared=True, data=None,
        )
        self.session.add(event)
        self.session.flush()
        return event


def evaluate(
    session: Session,
    today: dt.date,
    new_transaction_ids: Iterable[int] = (),
    *,
    new_recurring_ids: Iterable[int] = (),
    status_changes: Iterable[tuple[int, str]] = (),
    only: Iterable[str] | None = None,
) -> list[AlertEvent]:
    """Create any due alert events (caller commits). Returns the events created.

    ``only``: evaluate just these keys (the hourly tick passes ``TICK_KEYS``).
    """
    settings = {s.key: s for s in session.scalars(select(AlertSetting))}
    wanted = None if only is None else set(only)

    def on(key: str) -> AlertSetting | None:
        if wanted is not None and key not in wanted:
            return None
        s = settings.get(key)
        return s if s is not None and s.enabled else None

    out = _Emitter(session)
    if s := on("low"):
        _low(session, out, today, s)
    if (s := on("big")) and (ids := list(new_transaction_ids)):
        _big(session, out, today, s, ids)
    if s := on("budget"):
        _budget(session, out, today, s)
    if s := on("due"):
        _due(session, out, today, s)
    if on("newrec") and (ids := list(new_recurring_ids)):
        _newrec(session, out, ids)
    # Settings D7 update 2: ``price`` and ``conn`` no longer create alert events (bank
    # problems show on Home and in Settings > Banks). Price tracking still updates detected
    # bills' amounts; ``status_changes`` is accepted and ignored.
    if on("price"):
        _price(session, out, today)
    if on("income") and (ids := list(new_transaction_ids)):
        _income(session, out, today, ids)
    # The calendar ones read a lot; a problem there must not stop the others (type-only log).
    for key, check in (("low_ahead", _low_ahead), ("reminder", _reminder)):
        if s := on(key):
            try:
                check(session, out, today, s)
            except Exception as exc:  # noqa: BLE001
                log.error("%s alert check failed: %s", key, type(exc).__name__)
    return out.created


def _value_cents(setting: AlertSetting) -> int:
    return to_cents(setting.value if setting.value is not None else ALERT_SEEDS[setting.key][0])


def _low(session: Session, out: _Emitter, today: dt.date, s: AlertSetting) -> None:
    account = forecast_account(session)
    threshold = _value_cents(s)
    if account is None or account.current_balance_cents >= threshold:
        return
    out.emit(
        "low", f"low:{account.id}:{today.isoformat()}",
        f"{account.name} is below {fmt_usd(threshold)}",
        f"Balance {fmt_usd(account.current_balance_cents)}",
    )


def _big(session: Session, out: _Emitter, today: dt.date, s: AlertSetting, ids: list[int]) -> None:
    limit = _value_cents(s)
    since = today - dt.timedelta(days=BIG_RECENT_DAYS)
    rows = session.execute(
        select(Transaction, Account.name)
        .join(Account, Account.id == Transaction.account_id)
        .where(
            Transaction.id.in_(ids),
            Transaction.amount_cents < -limit,
            Transaction.is_transfer.is_(False),
            Transaction.date >= since,
            Account.hidden.is_(False),
        )
        .order_by(Transaction.date, Transaction.id)
    ).all()
    for txn, account_name in rows:
        out.emit(
            "big", f"big:{txn.id}",
            f"Large transaction at {display_name(txn.merchant_name, txn.name)}",
            f"{fmt_usd(-txn.amount_cents)} on {account_name}, {_day(txn.date)}",
        )


def _budget(session: Session, out: _Emitter, today: dt.date, _s: AlertSetting) -> None:
    """This month's envelopes: spent > carryover + assigned.

    Settings D7 update 2: only *over* the plan, never at exactly 100% (bills paid exactly as
    planned sent a flood of "used 100%" alerts). The setting's percent value is not used.
    """
    month = month_of(today)
    cmap, lines = budget_alert_lines(session, today)
    goal_cats = goal_categories(session, cmap)  # D8: money set aside, not a spending plan
    from .debt_budget import linked_category  # Release 3.10; builds on spending and goals

    debt_cat = linked_category(session, cmap)  # "Paying off debt": the user files payments there
    for category in sorted(lines, key=cmap.sort_key):
        if category in goal_cats or category == debt_cat:
            continue
        line = lines[category]
        budget, spent = line.carryover + line.assigned, line.spent
        if budget <= 0 or spent <= budget:
            continue
        name = cmap.get(category).name
        # Release 3.6: plain words ("plan", not "budget"), like the Budget page. The title has
        # no amount (the event is sent once a month and the overspend keeps changing); the
        # body has the numbers at the time.
        out.emit("budget", f"budget:{month}:{category}", f"{name} went over its plan",
                 f"{fmt_usd(spent)} of {fmt_usd(budget)} spent")


def _due(session: Session, out: _Emitter, today: dt.date, s: AlertSetting) -> None:
    days = int(s.value if s.value is not None else 3)
    horizon = today + dt.timedelta(days=days)
    for account in session.scalars(
        select(Account).where(
            Account.hidden.is_(False),
            Account.category.in_(LIABILITY_CATEGORIES),
            Account.next_payment_due.is_not(None),
            Account.next_payment_due >= today,
            Account.next_payment_due <= horizon,
        ).order_by(Account.next_payment_due, Account.id)
    ):
        due = account.next_payment_due
        when = "today" if due == today else ("tomorrow" if due == today + dt.timedelta(days=1) else _day(due))
        body = (
            f"Minimum payment {fmt_usd(account.minimum_payment_cents)}"
            if account.minimum_payment_cents else f"Due {_day(due)}"
        )
        out.emit("due", f"due:{account.id}:{due.isoformat()}", f"{account.name} payment due {when}", body)


NEWREC_BATCH = 3  # more than this many in one detection run → one summary alert


def _newrec(session: Session, out: _Emitter, ids: list[int]) -> None:
    items = [
        item
        for item in session.scalars(
            select(RecurringItem).where(RecurringItem.id.in_(ids)).order_by(RecurringItem.id)
        )
        if item.status == "suggested"
    ]
    if len(items) > NEWREC_BATCH:
        # A first import (or years of history) finds many at once; one alert, not a flood.
        names = ", ".join(i.name for i in items[:3])
        out.emit(
            "newrec", f"newrec:batch:{items[0].id}-{items[-1].id}",
            f"{len(items)} new recurring items found",
            f"Review them in Bills and paychecks: {names} and {len(items) - 3} more.",
        )
        return
    for item in items:
        what = "deposit" if item.amount_cents > 0 else "charge"
        out.emit(
            "newrec", f"newrec:{item.id}",
            f"New recurring {what}: {item.name}",
            f"{fmt_usd(abs(item.amount_cents))} {item.cadence}",
        )


def _price(session: Session, out: _Emitter, today: dt.date) -> None:
    every = list(session.scalars(select(RecurringItem)))
    items = [i for i in every if i.status == "active"]
    if not items:
        return
    since = today - dt.timedelta(days=LOOKBACK_DAYS)
    # Hidden accounts' charges never count: an item without an account must not follow (or ask
    # about) a charge on a hidden card.
    hidden = list(session.scalars(select(Account.id).where(Account.hidden.is_(True))))
    rows = session.execute(
        select(
            Transaction.id, Transaction.account_id, Transaction.date, Transaction.name,
            Transaction.merchant_name, Transaction.amount_cents,
        ).where(Transaction.pending.is_(False), Transaction.date >= since, Transaction.date <= today,
                Transaction.account_id.not_in(hidden))
    ).all()
    latest: dict[tuple[int, str, int], object] = {}
    latest_any: dict[tuple[str, int], object] = {}
    for row in rows:
        if row.amount_cents == 0:
            continue
        key, sign = merchant_key(row.merchant_name, row.name), 1 if row.amount_cents > 0 else -1
        for bucket, k in ((latest, (row.account_id, key, sign)), (latest_any, (key, sign))):
            best = bucket.get(k)
            if best is None or (row.date, row.id) > (best.date, best.id):
                bucket[k] = row
    aliases = item_aliases(session, items)
    memo = load_notes(session)  # read once, saved once at the end
    by_hand = hand_set_ids(memo, items)  # 3.19: an amount the user set (detection's note) stays theirs
    asks = price_ask.load(session, every)
    followed = False
    for item in items:
        sign = 1 if item.amount_cents > 0 else -1
        if item.account_id is not None:
            txn = latest.get((item.account_id, item.merchant_key, sign))
        else:
            txn = latest_any.get((item.merchant_key, sign))
        for account_id, key in aliases.get(item.id, ()):  # 3.19: a renamed or moved bill
            other = latest.get((account_id, key, sign))
            if other is not None and (txn is None or (other.date, other.id) > (txn.date, txn.id)):
                txn = other
        if txn is None:
            continue
        if not price_ask.changed(txn.amount_cents, item.amount_cents):
            asks.pop(item.id, None)  # the price is back to their amount: nothing to ask
            continue
        # Settings D7 update 2: no visible event any more. A scrubbed tombstone (like "Clear
        # all" leaves) still records that this charge was seen, so each charge is handled once.
        if out.tombstone("price", f"price:{item.id}:{txn.id}") is None:
            continue
        if price_ask.hand_set(item, by_hand):
            # 3.19: their amount is never overwritten; ask them (bills and paychecks, not one-time items).
            if price_ask.askable(item):
                price_ask.record(asks, item, txn.id, txn.amount_cents, txn.date)
        else:
            follow_amount(memo, item, txn.amount_cents)  # a detected bill follows the new price
            followed = True
    if followed:
        save_notes(session, memo)
    price_ask.tidy(asks, every)
    price_ask.save(session, asks)


def _income(session: Session, out: _Emitter, today: dt.date, ids: list[int]) -> None:
    """One "$X new to assign" event per sync that brings posted income into budget accounts.

    Only recent deposits count (like "big"), so importing years of history doesn't announce
    it all as new money.
    """
    cmap, book = budget_book(session, today)
    included = {a.id: a for a, inc in book.accounts if inc}
    if not included:
        return
    income = book.income
    # Settings (D7): with "Tell me when a paycheck is different" off, the plain wording.
    if income.mode == "expected" and income.expected is not None and prefs.paycheck_notify(session):
        _income_expected(session, out, today, ids, cmap, book, included)
        return
    since = today - dt.timedelta(days=BIG_RECENT_DAYS)
    rows = [
        txn
        for txn in session.scalars(
            select(Transaction)
            .where(
                Transaction.id.in_(ids),
                Transaction.pending.is_(False),
                Transaction.is_transfer.is_(False),
                Transaction.amount_cents > 0,
                Transaction.account_id.in_(list(included)),
                Transaction.date >= since,
            )
            .order_by(Transaction.date, Transaction.id)
        )
        if cmap.kind(txn.category) == "income"
    ]
    if not rows:
        return
    total = sum(t.amount_cents for t in rows)
    dedupe = "income:" + ",".join(str(i) for i in sorted(t.id for t in rows))
    if len(rows) == 1:
        txn = rows[0]
        source = f"{display_name(txn.merchant_name, txn.name)} on {included[txn.account_id].name}."
    else:
        source = f"{len(rows)} deposits."
    ready = fmt_usd(book.ready_to_assign())
    out.emit("income", dedupe, f"{fmt_usd(total)} new to assign", f"{source} Ready to assign is now {ready}.")


def _income_expected(
    session: Session, out: _Emitter, today: dt.date, ids: list[int],
    cmap: CategoryMap, book: Book, included: dict[int, Account],
) -> None:
    """Release 3.6, expected income on: expected paychecks are already in the month's amount,
    so only the part of this sync's deposits beyond what was expected is news.

    Only deposits dated in today's month count (like the month's "received"): one synced late
    and dated last month is in last month's income, so it isn't announced here.
    """
    since = max(today - dt.timedelta(days=BIG_RECENT_DAYS), month_bounds(book.current)[0])
    rows = [
        txn
        for txn in session.scalars(
            select(Transaction)
            .where(
                Transaction.id.in_(ids),
                Transaction.pending.is_(False),
                Transaction.is_transfer.is_(False),
                Transaction.amount_cents > 0,
                Transaction.account_id.in_(list(included)),
                Transaction.date >= since,
            )
            .order_by(Transaction.date, Transaction.id)
        )
        if cmap.kind(txn.category) in RECEIVED_KINDS
    ]
    if not rows:
        return
    # What these deposits added to the month's received (their split parts that count).
    new = received_by_month(
        session, cmap, list(included), book.current, book.current, Transaction.id.in_([t.id for t in rows])
    ).get(book.current, 0)
    income = book.income
    before = max(0, income.received - new - income.expected)
    contribution = max(0, income.received - income.expected) - before
    if contribution < INCOME_SURPLUS_MIN_CENTS:
        return
    if len(rows) == 1:
        txn = rows[0]
        source = f"{display_name(txn.merchant_name, txn.name)} on {included[txn.account_id].name}."
    else:
        source = f"{len(rows)} deposits."
    dedupe = "income:" + ",".join(str(i) for i in sorted(t.id for t in rows))
    out.emit("income", dedupe, f"{fmt_usd(contribution)} more than expected came in",
             f"{source} It's under Not planned yet.")


def _weekday_day(d: dt.date) -> str:
    return f"{d:%a}, {d:%b} {d.day}"


def _low_ahead(session: Session, out: _Emitter, today: dt.date, s: AlertSetting) -> None:
    """The projected balance drops below the ``low`` value within N days (whether or not
    ``low`` itself is on). Nothing when checking is already below it today: that is the
    ``low`` alert's job, and every later day would repeat it."""
    days = int(s.value) if s.value is not None else int(ALERT_SEEDS["low_ahead"][0])
    days = max(1, min(LOW_AHEAD_MAX_DAYS, days))
    cal = calendar.build_calendar(session, today, today, today + dt.timedelta(days=days))
    account, dip = cal["account"], cal["first_dip"]
    if account is None or dip is None or dip["date"] == today.isoformat():
        return
    threshold, balance = to_cents(cal["threshold"]), to_cents(dip["balance"])
    when = dt.date.fromisoformat(dip["date"])
    cause = next((o for o in cal["occurrences"] if o["key"] == dip["cause_key"]), None)
    after = f" after {cause['name']}" if cause is not None else ""
    # Keyed on the bill that causes the dip (and its own date, so moving it may warn again):
    # a carried, still unpaid, bill moves the dip date every day, and that is the same warning.
    if dip["cause_key"]:
        reason = f"{dip['cause_key']}@{cause['date']}" if cause is not None else dip["cause_key"]
    else:
        reason = dip["date"]
    out.emit(
        "low_ahead", f"low_ahead:{account['id']}:{reason}",
        f"{account['name']} may drop below {fmt_usd(threshold)} on {_day(when)}",
        f"Projected {fmt_usd(balance)}{after}. Open Bills and paychecks to move a bill or money.",
        {"account_id": account["id"], "date": dip["date"], "balance_cents": balance,
         "threshold_cents": threshold, "cause_key": dip["cause_key"]},
    )


def _reminder(session: Session, out: _Emitter, today: dt.date, _s: AlertSetting) -> None:
    """Bills and paychecks with "Remind me" on, N days before their (moved) date, until the day."""
    cal = calendar.build_calendar(session, today, today, today + dt.timedelta(days=3))
    if cal["account"] is None:
        return
    series = {s["id"]: s for s in cal["series"]}
    for occ in cal["occurrences"]:
        n = occ["reminder_days"]
        if occ["recurring_id"] is None or n <= 0:
            continue
        if occ["status"] not in ("upcoming", "pending") and not occ["carried"]:
            continue
        when = dt.date.fromisoformat(occ["date"])
        if not (when - dt.timedelta(days=n) <= today <= when):
            continue
        left = (when - today).days
        due = "today" if left == 0 else ("tomorrow" if left == 1 else f"in {left} days")
        cents = to_cents(occ["amount"])
        ref = series.get(occ["series"], {}).get("account") or cal["account"]
        if occ["kind"] == "in":
            title, where = f"{occ['name']} is expected {due}", f"to {ref['name']}"
        elif occ["kind"] == "card":
            title, where = f"{occ['name']} is due {due}", f"on {ref['name']}"
        else:
            title, where = f"{occ['name']} is due {due}", f"from {ref['name']}"
        out.emit(
            "reminder", f"reminder:{occ['recurring_id']}:{occ['base_date']}:{occ['date']}",
            title, f"{fmt_usd(abs(cents))} {where} on {_weekday_day(when)}.",
            {"recurring_id": occ["recurring_id"], "base_date": occ["base_date"], "date": occ["date"],
             "amount_cents": cents},
        )


def safe_evaluate(session: Session, today: dt.date, **kwargs) -> None:  # noqa: ANN003
    """Evaluate and commit; alert problems must never break the calling operation."""
    try:
        evaluate(session, today, **kwargs)
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        log.error("alert evaluation failed: %s", type(exc).__name__)

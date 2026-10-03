"""Recurring items: merchant keys, cadence math and detection from transaction history."""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from ..models import Account, AppSetting, RecurringItem, Transaction
from ..seeds import OTHER
from ..utils import add_months, days_in_month, from_cents, month_index
from .card_payments import AUTO as CARD_PAYMENT_SOURCE
from .categories import CategoryMap
from .categories import load as load_categories

log = logging.getLogger("fintrack.recurring")

# Detected cadences (``classify`` loops over these with BANDS). Manual items may also be
# "once" (Release 3.6): it never repeats and detection never touches it.
CADENCES = ("weekly", "biweekly", "semimonthly", "monthly", "quarterly", "yearly")
MANUAL_CADENCES = CADENCES + ("once",)
BANDS: dict[str, tuple[int, int]] = {
    "weekly": (6, 8),
    "biweekly": (13, 15),
    "semimonthly": (14, 17),
    "monthly": (27, 33),
    "quarterly": (85, 97),
    "yearly": (355, 375),
}
# Nominal length in days, for staleness checks and the calendar's carry window.
PERIOD_DAYS = {"weekly": 7, "biweekly": 14, "semimonthly": 15, "monthly": 30, "quarterly": 91, "yearly": 365,
               "once": 14}
MONTH_STEPS = {"monthly": 1, "quarterly": 3, "yearly": 12}
DAY_STEPS = {"weekly": 7, "biweekly": 14}
STATUS_ORDER = {"active": 0, "suggested": 1, "dismissed": 2}

LOOKBACK_DAYS = 400
IN_BAND_SHARE = 0.7
AMOUNT_TOLERANCE = 0.2
DEFAULT_SEMIMONTHLY = (1, 15)
MAX_STEPS = 20000

# Release 3.19: finding more bills.
# A monthly bill after 2 charges: consecutive months, amounts at most 5% apart, and the last
# charge recent (its next one isn't overdue by more than a week).
TWO_CHARGE_TOLERANCE = 0.05
TWO_CHARGE_MAX_AGE_DAYS = 40
# Bills whose amount changes: the last few charges, every gap in the band, nearly the same day.
VARIABLE_CADENCES = ("monthly", "quarterly")
VARIABLE_MIN_CHARGES = 3
VARIABLE_RECENT = 6
VARIABLE_DAY_SPREAD = 5
VARIABLE_MAX_RATIO = 3.0
# New weekly/biweekly money-out suggestions need the same amount on at least half the charges.
SHOPPING_CADENCES = ("weekly", "biweekly")
FIXED_SHARE = 0.5
# Name variants ("NETFLIX.COM", "Netflix", "NETFLIX INC") share a loose key: these words,
# numbers and single letters are left out.
NAME_NOISE = frozenset({
    "com", "net", "org", "www", "inc", "llc", "ltd", "co", "corp", "corporation", "company", "the",
    "usa", "pos", "debit", "purchase", "recurring", "ach", "ppd", "ccd", "web", "des", "id", "indn",
    "conf", "ref", "trace", "trn", "txn", "pmt", "payment", "autopay", "online", "epay", "sq", "tst",
})
# Detection's notes per item (app_settings): what it last wrote and the other names/accounts.
MEMO_KEY = "recurring_detection"
# Joining name variants or accounts: the gap at each join must be in the cadence's band, +- this.
JOIN_SLACK_DAYS = 3
# A loose name with more series than this (across accounts) is never joined across accounts.
MAX_JOIN_SERIES = 20
# Other names/accounts noted per item: the most recent ones.
MAX_ALIASES = 20

_NOT_WORD = re.compile(r"[^\w&' ]+|[\d_]+")


# ------------------------------------------------------------------ keys


def merchant_key(merchant_name: str | None, name: str | None) -> str:
    """Normalized lowercase merchant_name, else the description without digits/punctuation."""
    if merchant_name and merchant_name.strip():
        return " ".join(merchant_name.lower().split())[:120]
    raw = (name or "").lower()
    cleaned = " ".join(_NOT_WORD.sub(" ", raw).split())
    return (cleaned or " ".join(raw.split()) or "unknown")[:120]


_TOKEN_SPLIT = re.compile(r"[^\w&']+")


def loose_key(key: str) -> str:
    """A merchant key without noise words, numbers and stray letters, so "netflix.com",
    "netflix inc" and "netflix" all give "netflix". The key itself when nothing real is left."""
    kept = [
        t for t in (t.strip("'") for t in _TOKEN_SPLIT.split(key.lower()))
        if len(t) > 1 and not any(c.isdigit() or c == "_" for c in t) and t not in NAME_NOISE
    ]
    if not any(len(t) >= 3 for t in kept):
        return key
    return " ".join(kept)


def display_name(merchant_name: str | None, name: str | None) -> str:
    text = (merchant_name or "").strip() or (name or "").strip() or "Recurring"
    return text[:200]


# ------------------------------------------------------------------ cadence math


def parse_days(value: str | None) -> list[int] | None:
    if not value:
        return None
    try:
        days = json.loads(value)
    except ValueError:
        return None
    if isinstance(days, list) and days and all(isinstance(d, int) and 1 <= d <= 31 for d in days):
        return days
    return None


def occurrences(first: dt.date, cadence: str, days: list[int] | None = None) -> Iterator[dt.date]:
    """``first``, then every later occurrence of the schedule (bounded)."""
    yield first
    if cadence == "once":
        return
    if cadence in DAY_STEPS:
        step = dt.timedelta(days=DAY_STEPS[cadence])
        current = first
        for _ in range(MAX_STEPS):
            current += step
            yield current
    elif cadence == "semimonthly":
        pair = sorted(days[:2]) if days and len(days) >= 2 else list(DEFAULT_SEMIMONTHLY)
        month_start = first.replace(day=1)
        for k in range(MAX_STEPS):
            base = add_months(month_start, k, 1)
            for day in pair:
                candidate = add_months(base, 0, day)
                if candidate > first:
                    yield candidate
    else:
        step = MONTH_STEPS[cadence]
        anchor = days[0] if days else first.day
        for k in range(1, MAX_STEPS):
            yield add_months(first, k * step, anchor)


def roll_forward(first: dt.date, cadence: str, days: list[int] | None, not_before: dt.date) -> dt.date:
    """The first occurrence on or after ``not_before``."""
    for day in occurrences(first, cadence, days):
        if day >= not_before:
            return day
    return first  # pragma: no cover - MAX_STEPS covers centuries


def next_after(last: dt.date, cadence: str, days: list[int] | None, today: dt.date) -> dt.date:
    """Last occurrence + cadence, rolled forward until it's today or later."""
    for day in occurrences(last, cadence, days):
        if day > last and day >= today:
            return day
    return last  # pragma: no cover


def anchor_days(cadence: str, next_date: dt.date, current: list[int] | None = None) -> list[int] | None:
    """The day(s) of month a schedule sticks to, for storage in ``month_days``."""
    if cadence == "semimonthly":
        return current if current and len(current) == 2 else None
    if cadence in MONTH_STEPS:
        return [next_date.day]
    return None


def schedule_dates(
    first: dt.date,
    cadence: str,
    days: list[int] | None,
    start: dt.date,
    end: dt.date,
    not_before: dt.date | None = None,
) -> list[dt.date]:
    """Every date of the schedule in [start, end], before and after ``first``, ascending.

    ``first`` itself is always a date of the schedule (like ``occurrences``). Monthly-type
    steps are counted from ``first`` with the anchor day (31 = the month's last day);
    weekly steps in whole weeks either way; semimonthly is the two anchor days of every
    month. Dates before ``not_before`` (the item's ``start_date``) don't exist.
    """
    if not_before is not None and start < not_before:
        start = not_before
    if start > end:
        return []
    if cadence == "once":
        return [first] if start <= first <= end else []
    out: list[dt.date] = []
    if cadence in DAY_STEPS:
        step = DAY_STEPS[cadence]
        k = -((first - start).days // step)  # ceil((start - first) / step)
        current = first + dt.timedelta(days=k * step)
        for _ in range(MAX_STEPS):
            if current > end:
                break
            out.append(current)
            current += dt.timedelta(days=step)
        return out
    if cadence == "semimonthly":
        pair = sorted(days[:2]) if days and len(days) >= 2 else list(DEFAULT_SEMIMONTHLY)
        found = {first} if start <= first <= end else set()
        month = start.replace(day=1)
        for _ in range(MAX_STEPS):
            if month > end:
                break
            for day in pair:
                candidate = add_months(month, 0, day)
                if start <= candidate <= end:
                    found.add(candidate)
            month = add_months(month, 1, 1)
        return sorted(found)
    step = MONTH_STEPS[cadence]
    anchor = days[0] if days else first.day
    k = (month_index(start) - month_index(first)) // step - 1
    for _ in range(MAX_STEPS):
        current = first if k == 0 else add_months(first, k * step, anchor)
        if current > end:
            break
        if current >= start:
            out.append(current)
        k += 1
    return out


def rule_dates(item: RecurringItem, start: dt.date, end: dt.date) -> list[dt.date]:
    """The item's rule dates ("base dates") in [start, end], both directions from next_date."""
    return schedule_dates(item.next_date, item.cadence, parse_days(item.month_days), start, end, item.start_date)


def is_rule_date(item: RecurringItem, day: dt.date) -> bool:
    return rule_dates(item, day, day) == [day]


def next_base(item: RecurringItem, today: dt.date) -> dt.date | None:
    """The series' next scheduled base: the first rule date on or after max(today, next_date)."""
    start = max(today, item.next_date)
    try:
        dates = rule_dates(item, start, start + dt.timedelta(days=400))
    except OverflowError:  # a date near 9999 (stored before dates were bounded): no next one
        return None
    return dates[0] if dates else None


# Occurrence ↔ transaction matching (the calendar): days either side of the expected date.
MATCH_WINDOW = {"weekly": 3, "biweekly": 5, "semimonthly": 5}
DEFAULT_MATCH_WINDOW = 7


def match_window(cadence: str) -> int:
    return MATCH_WINDOW.get(cadence, DEFAULT_MATCH_WINDOW)


# ------------------------------------------------------------------ detection


def _month_days(dates: list[dt.date]) -> set[int]:
    """Distinct days of the month, with any month's last day counted as 31."""
    return {31 if d.day == days_in_month(d.year, d.month) else d.day for d in dates}


def _month_anchor(dates: list[dt.date]) -> int:
    """Day of month a monthly/quarterly/yearly series sticks to (31 = last day)."""
    days = [d.day for d in dates]
    to_end = [days_in_month(d.year, d.month) - d.day for d in dates]
    if max(to_end) <= 3 and min(to_end) == 0:
        return 31  # paid at month end (Jan 31, Feb 28, Apr 30...)
    counts = defaultdict(int)
    for day in days:
        counts[day] += 1
    best = max(counts.values())
    return next(d for d in reversed(days) if counts[d] == best)  # ties: the most recent


def classify(
    dates: list[dt.date], amounts: list[int], *, crowded: bool = False, refresh: bool = False,
) -> tuple[str, list[int] | None] | None:
    """(cadence, month_days) if these dated amounts (one per day, signed) form a recurring series.

    The rules before Release 3.19 come first, then (3.19) a monthly bill after 2 charges and
    bills whose amount changes but whose dates are very regular. ``crowded`` = the merchant had
    more than one charge on some day (those two new rules are then off). ``refresh`` = an
    existing item's series: the weekly/biweekly shopping check is off, so an item that was
    found before keeps being found.
    """
    found = _classify_basic(dates, amounts)
    if found is not None:
        if not refresh and _looks_like_shopping(found[0], amounts):
            return None
        return found
    if crowded:
        return None
    return _two_charges(dates, amounts) or _variable(dates, amounts)


def _looks_like_shopping(cadence: str, amounts: list[int]) -> bool:
    """Weekly/biweekly money out where most charges differ (groceries, gas): not a bill.
    Real weekly bills charge the same amount again and again."""
    if cadence not in SHOPPING_CADENCES or amounts[0] > 0:
        return False
    most = Counter(amounts).most_common(1)[0][1]
    return most / len(amounts) < FIXED_SHARE


def _two_charges(dates: list[dt.date], amounts: list[int]) -> tuple[str, list[int]] | None:
    """A monthly bill seen twice: consecutive months, a monthly gap, nearly the same amount."""
    if len(dates) != 2:
        return None
    lo, hi = BANDS["monthly"]
    if not lo <= (dates[1] - dates[0]).days <= hi or month_index(dates[1]) - month_index(dates[0]) != 1:
        return None
    a, b = abs(amounts[0]), abs(amounts[1])
    if min(a, b) <= 0 or abs(a - b) > TWO_CHARGE_TOLERANCE * max(a, b):
        return None
    return "monthly", [_month_anchor(dates)]


def _day_spread(dates: list[dt.date]) -> int:
    """How many days of the month the dates spread over, around month ends (31 = last day)."""
    days = sorted(_month_days(dates))
    if len(days) < 2:
        return 0
    gaps = [b - a for a, b in zip(days, days[1:])] + [days[0] + 31 - days[-1]]
    return 31 - max(gaps)


def _variable(dates: list[dt.date], amounts: list[int]) -> tuple[str, list[int]] | None:
    """A bill whose amount changes (electric, phone): its recent charges come every month (or
    quarter) on nearly the same day, with no extra charges in between; the largest is at most
    ``VARIABLE_MAX_RATIO`` times the smallest."""
    dates, amounts = dates[-VARIABLE_RECENT:], amounts[-VARIABLE_RECENT:]
    if len(dates) < VARIABLE_MIN_CHARGES:
        return None
    sizes = [abs(a) for a in amounts]
    if min(sizes) <= 0 or max(sizes) > VARIABLE_MAX_RATIO * min(sizes):
        return None
    if _day_spread(dates) > VARIABLE_DAY_SPREAD:
        return None
    intervals = [(b - a).days for a, b in zip(dates, dates[1:])]
    for cadence in VARIABLE_CADENCES:
        lo, hi = BANDS[cadence]
        if all(lo <= i <= hi for i in intervals):
            return cadence, [_month_anchor(dates)]
    return None


def _classify_basic(dates: list[dt.date], amounts: list[int]) -> tuple[str, list[int] | None] | None:
    """The rules before Release 3.19: amounts within 20% and 70% of the gaps in one band."""
    n = len(dates)
    if n < 2:
        return None
    intervals = [(b - a).days for a, b in zip(dates, dates[1:])]
    median = statistics.median(intervals)
    magnitudes = [abs(a) for a in amounts]
    typical = statistics.median(magnitudes)
    if typical <= 0 or any(abs(m - typical) > AMOUNT_TOLERANCE * typical for m in magnitudes):
        return None

    month_days = _month_days(dates)
    candidates: list[str] = []
    lo, hi = BANDS["semimonthly"]
    # Semimonthly overlaps the biweekly band; what tells them apart is landing on exactly
    # two days of the month (biweekly dates drift through the month).
    if lo <= median <= hi and len(month_days) == 2:
        candidates.append("semimonthly")
    candidates += [c for c in CADENCES if c != "semimonthly" and BANDS[c][0] <= median <= BANDS[c][1]]

    for cadence in candidates:
        if n < (2 if cadence in ("quarterly", "yearly") else 3):
            continue
        lo, hi = BANDS[cadence]
        if sum(lo <= i <= hi for i in intervals) / len(intervals) < IN_BAND_SHARE:
            continue
        if cadence == "semimonthly":
            return cadence, sorted(month_days)
        if cadence in MONTH_STEPS:
            return cadence, [_month_anchor(dates)]
        return cadence, None
    return None


def _history(session: Session, today: dt.date) -> list:
    """The last 400 days of non-pending rows on visible accounts (detection's input): no
    transfers, except card payments FinTrack marked (Release 3.9.1). Paying the card is still
    a bill the user has to pay, so it stays on the Bills calendar; it just isn't spending."""
    since = today - dt.timedelta(days=LOOKBACK_DAYS)
    return list(session.execute(
        select(
            Transaction.id, Transaction.account_id, Transaction.date, Transaction.name,
            Transaction.merchant_name, Transaction.amount_cents, Transaction.category, Transaction.is_transfer,
        )
        .join(Account, Account.id == Transaction.account_id)
        .where(
            Account.hidden.is_(False),
            Transaction.pending.is_(False),
            or_(
                Transaction.is_transfer.is_(False),
                and_(Transaction.transfer_source == CARD_PAYMENT_SOURCE, Transaction.amount_cents < 0),
            ),
            Transaction.date >= since,
            Transaction.date <= today,
            Transaction.amount_cents != 0,
        )
        .order_by(Transaction.date, Transaction.id)
    ).all())


@dataclass
class _Series:
    """One bill's charges, maybe under several names or accounts: ``parts`` are the
    (account_id, merchant_key) pairs, oldest first; ``rows`` one charge per day, by date."""

    parts: list[tuple[int, str]]
    rows: list
    crowded: bool = False
    joins: list[int] = field(default_factory=list)  # days between one part's end and the next's start

    @property
    def first(self) -> dt.date:
        return self.rows[0].date

    @property
    def last(self) -> dt.date:
        return self.rows[-1].date

    @property
    def sign(self) -> int:
        return _sign(self.rows[0].amount_cents)

    def classify(self, refresh: bool = False) -> tuple[str, list[int] | None] | None:
        return classify(
            [r.date for r in self.rows], [r.amount_cents for r in self.rows], crowded=self.crowded, refresh=refresh,
        )

    def joins_fit(self) -> bool:
        """Joined parts recur together and every join is one period apart (give or take
        ``JOIN_SLACK_DAYS``): an extra charge a few days later is not the same bill."""
        found = self.classify()
        if found is None:
            return False
        lo, hi = BANDS[found[0]]
        return all(lo - JOIN_SLACK_DAYS <= gap <= hi + JOIN_SLACK_DAYS for gap in self.joins)


def _joined(a: _Series, b: _Series) -> _Series:
    return _Series(
        a.parts + b.parts, a.rows + b.rows, a.crowded or b.crowded, a.joins + b.joins + [(b.first - a.last).days],
    )


def _one_account(parts: list[_Series]) -> list[_Series]:
    """Names on one account that differ only in noise ("NETFLIX.COM" then "Netflix"): one
    series when each name starts after the previous one stopped and together they recur.
    Otherwise they stay apart, as before Release 3.19."""
    if len(parts) < 2:
        return parts
    parts = sorted(parts, key=lambda s: (s.first, s.parts[0][1]))
    chain, rest = parts[0], []
    for s in parts[1:]:
        if s.first > chain.last:
            chain = _joined(chain, s)
        else:
            rest.append(s)
    if len(chain.parts) < 2 or not chain.joins_fit():
        return parts
    return [chain, *rest]


def _across_accounts(series: list[_Series]) -> list[_Series]:
    """A bill that moved to another card or account: the old account's charges stopped before
    the new ones began, about one period apart, and together they still recur."""
    if len(series) < 2 or len(series) > MAX_JOIN_SERIES:
        return series  # one name on very many accounts/parts: too unclear to join
    chains: list[_Series] = []
    for s in sorted(series, key=lambda s: (s.first, s.parts[0])):
        targets = [i for i, c in enumerate(chains) if c.last < s.first and c.parts[-1][0] != s.parts[0][0]]
        for i in sorted(targets, key=lambda i: chains[i].last, reverse=True):
            merged = _joined(chains[i], s)
            if merged.joins_fit():
                chains[i] = merged
                break
        else:
            chains.append(s)
    return chains


def _build_series(groups: dict, counts: dict) -> list[_Series]:
    """Detection's series: exact (account, merchant key, sign) groups, joined across name
    variants on one account, then across accounts when a bill moved."""
    by_account: dict[tuple[int, str, int], list[_Series]] = defaultdict(list)
    for (account_id, key, sign), by_date in groups.items():
        rows = [by_date[d] for d in sorted(by_date)]
        crowded = counts[(account_id, key, sign)] > len(rows)
        by_account[(account_id, loose_key(key), sign)].append(_Series([(account_id, key)], rows, crowded))
    by_name: dict[tuple[str, int], list[_Series]] = defaultdict(list)
    for (_account, loose, sign), parts in sorted(by_account.items()):
        by_name[(loose, sign)] += _one_account(parts)
    out: list[_Series] = []
    for name in sorted(by_name):
        out += _across_accounts(by_name[name])
    return sorted(out, key=lambda s: (s.parts[-1][0], s.parts[-1][1], s.sign))


# Detection's notes (app_settings ``MEMO_KEY``), per item id: the amount and account detection
# last wrote (None = the user's, never changed by detection) and the item's other
# (account_id, merchant_key) pairs. A note counts only while the item's merchant key and
# created_at still match, so a reused id never inherits one.


def _stamp(item: RecurringItem) -> str:
    return item.created_at.replace(microsecond=0).isoformat() if item.created_at else ""


def _whole(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _load_memo(session: Session, items: list[RecurringItem]) -> dict[int, dict]:
    """The valid notes for ``items``. A broken note (bad JSON, wrong types) is ignored, never raised."""
    row = session.get(AppSetting, MEMO_KEY)
    try:
        raw = json.loads(row.value) if row is not None and row.value else {}
    except (ValueError, RecursionError):
        raw = {}
    if not isinstance(raw, dict):
        return {}
    by_id = {item.id: item for item in items}
    out: dict[int, dict] = {}
    for sid, entry in raw.items():
        try:
            item = by_id.get(int(sid)) if isinstance(sid, str) and sid.isdecimal() else None
        except ValueError:  # e.g. more digits than int() takes
            item = None
        if item is None or not isinstance(entry, dict):
            continue
        if entry.get("key") != item.merchant_key or entry.get("created") != _stamp(item):
            continue
        listed = entry.get("aliases")
        aliases = [
            (a[0], a[1]) for a in (listed if isinstance(listed, list) else [])
            if isinstance(a, list) and len(a) == 2 and _whole(a[0]) is not None and isinstance(a[1], str)
        ][-MAX_ALIASES:]
        out[item.id] = {"key": item.merchant_key, "created": _stamp(item), "amount": _whole(entry.get("amount")),
                        "account": _whole(entry.get("account")), "aliases": aliases}
    return out


def _save_memo(session: Session, memo: dict[int, dict]) -> None:
    value = json.dumps(
        {str(i): {**e, "aliases": [list(a) for a in e["aliases"][-MAX_ALIASES:]]} for i, e in sorted(memo.items())},
        sort_keys=True,
    )
    row = session.get(AppSetting, MEMO_KEY)
    if row is None:
        if memo:
            session.add(AppSetting(key=MEMO_KEY, value=value))
    elif row.value != value:
        row.value = value


def item_aliases(session: Session, items: list[RecurringItem]) -> dict[int, list[tuple[int, str]]]:
    """Each item's other (account_id, merchant_key) pairs: names and accounts its bill used
    before (Release 3.19), hidden accounts left out. Items without any are left out, so they
    match exactly as before. Every place that matches charges to items uses this with
    ``match_keys`` and ``claims``.

    One owner per pair, also when reading (whatever the note says): a pair that is another
    item's own (account, key), or (no account, key), or that an item with a lower id already
    lists, is dropped."""
    every = list(session.scalars(select(RecurringItem).order_by(RecurringItem.id)))
    memo = _load_memo(session, every)
    if not any(e["aliases"] for e in memo.values()):
        return {}
    own: dict[tuple[int | None, str], set[int]] = defaultdict(set)
    for item in every:
        own[(item.account_id, item.merchant_key)].add(item.id)
    taken: set[tuple[int, str]] = set()
    kept: dict[int, list[tuple[int, str]]] = {}
    for item_id in sorted(memo):
        pairs = []
        for pair in memo[item_id]["aliases"]:
            others = (own.get(pair, set()) | own.get((None, pair[1]), set())) - {item_id}
            if others or pair in taken:
                continue
            taken.add(pair)
            pairs.append(pair)
        kept[item_id] = pairs
    wanted = {item.id for item in items}
    hidden = set(session.scalars(select(Account.id).where(Account.hidden.is_(True))))
    out = {i: [a for a in pairs if a[0] not in hidden] for i, pairs in kept.items() if i in wanted}
    return {i: pairs for i, pairs in out.items() if pairs}


def load_notes(session: Session) -> dict[int, dict]:
    """Detection's valid notes for every item (to change some and ``save_notes`` once)."""
    return _load_memo(session, list(session.scalars(select(RecurringItem))))


def save_notes(session: Session, memo: dict[int, dict]) -> None:
    _save_memo(session, memo)


def hand_set_ids(memo: dict[int, dict], items: list[RecurringItem]) -> set[int]:
    """Ids of items whose amount the user set by hand, per detection's note (the note's amount
    is unknown or differs from the item's). Items without a note aren't in it."""
    return {i.id for i in items if i.id in memo and memo[i.id]["amount"] != i.amount_cents}


def users_amounts(session: Session, items: list[RecurringItem]) -> set[int]:
    """``hand_set_ids`` with the notes read from the vault."""
    return hand_set_ids(_load_memo(session, items), items)


def follow_amount(memo: dict[int, dict], item: RecurringItem, cents: int) -> None:
    """Price tracking set a detected item's amount from a new charge: keep the note (``memo``,
    from ``load_notes``; the caller saves it once) in step, so the new amount still counts as
    detection's, not the user's."""
    item.amount_cents = cents
    if item.id in memo:
        memo[item.id]["amount"] = cents


def mark_hand_set(session: Session, item: RecurringItem) -> None:
    """Yes to "now charges $X" (Release 3.19): the new amount is theirs for good, even when it
    equals what detection last wrote, so detection never changes it and the next price asks them."""
    memo = load_notes(session)
    if item.id in memo and memo[item.id]["amount"] is not None:
        memo[item.id]["amount"] = None
        _save_memo(session, memo)


def match_keys(item: RecurringItem, aliases: dict[int, list[tuple[int, str]]]) -> set[str]:
    """The merchant keys whose charges may be this item's: its own and its aliases'."""
    return {item.merchant_key, *(k for _a, k in aliases.get(item.id, ()))}


def claims(item: RecurringItem, aliases: dict[int, list[tuple[int, str]]], account_id: int, key: str) -> bool:
    """A charge on ``account_id`` with merchant key ``key`` is this item's: its own key on its
    account (any account when it has none; callers leave hidden ones out as before), or one
    of its aliases (exact account)."""
    if key == item.merchant_key and (item.account_id is None or item.account_id == account_id):
        return True
    return (account_id, key) in aliases.get(item.id, ())


def _first_memo(item: RecurringItem, s: _Series) -> dict:
    """Before Release 3.19 nothing was noted: a detected item's amount (account) counts as
    detection's when it is one of the series' charges (accounts), else as the user's."""
    detected = item.source == "detected"
    return {
        "key": item.merchant_key, "created": _stamp(item),
        "amount": item.amount_cents if detected and item.amount_cents in {r.amount_cents for r in s.rows} else None,
        "account": item.account_id if detected and item.account_id in {p[0] for p in s.parts} else None,
        "aliases": [],
    }


def _refresh(item: RecurringItem, s: _Series, found: tuple, today: dt.date, memo: dict, owner: dict) -> None:
    """An existing item's series was seen again: roll its dates forward (as before). 3.19: a
    detected item's amount and account follow the last charge unless the user set them, and
    the series' other names/accounts are noted for matching."""
    if item.cadence == "once":
        return  # a one-time item: its date is the user's, never rolled forward
    last = s.rows[-1]
    item_days = parse_days(item.month_days) or found[1]
    if item.last_seen_date is None or last.date > item.last_seen_date:
        item.last_seen_date = last.date
        item.next_date = next_after(last.date, item.cadence, item_days, today)
    elif item.next_date < today:
        item.next_date = roll_forward(item.next_date, item.cadence, item_days, today)
    if _sign(item.amount_cents) != s.sign or item.last_seen_date != last.date:
        return
    entry = memo.get(item.id) or _first_memo(item, s)
    if item.source == "detected":
        if entry["amount"] is not None and entry["amount"] == item.amount_cents:
            item.amount_cents = entry["amount"] = last.amount_cents
        if entry["account"] is not None and entry["account"] == item.account_id:
            item.account_id = entry["account"] = last.account_id
    own = (item.account_id, item.merchant_key)
    for part in s.parts:
        if (part != own and part not in entry["aliases"] and owner.get(part) in (None, item)
                and owner.get((None, part[1])) in (None, item)):  # not a hand-added item's name
            entry["aliases"].append(part)
            owner.setdefault(part, item)
    memo[item.id] = entry


def _suppressed(s: _Series, items: list[RecurringItem], memo: dict) -> bool:
    """A new series that is only a name variant of an item on the same account (or one with
    no account), e.g. a bill added by hand as "Netflix": no second suggestion."""
    loose = loose_key(s.parts[-1][1])
    accounts = {p[0] for p in s.parts}
    for item in items:
        if _sign(item.amount_cents) != s.sign or (item.account_id is not None and item.account_id not in accounts):
            continue
        keys = [item.merchant_key, *(k for _a, k in memo.get(item.id, {}).get("aliases", ()))]
        if any(loose_key(k) == loose for k in keys):
            return True
    return False


def detect(session: Session, today: dt.date) -> list[int]:
    """Suggest new recurring items and refresh existing ones; returns the new item ids."""
    rows = _history(session, today)
    cmap = load_categories(session)

    groups: dict[tuple[int, str, int], dict[dt.date, object]] = defaultdict(dict)
    counts: dict[tuple[int, str, int], int] = defaultdict(int)
    for row in rows:
        key = (row.account_id, merchant_key(row.merchant_name, row.name), _sign(row.amount_cents))
        groups[key][row.date] = row  # one occurrence per day: the latest id wins
        counts[key] += 1

    items = list(session.scalars(select(RecurringItem).order_by(RecurringItem.id)))
    memo = _load_memo(session, items)
    owner: dict[tuple[int | None, str], RecurringItem] = {}
    for item in items:
        owner.setdefault((item.account_id, item.merchant_key), item)
    for item in items:
        for alias in memo.get(item.id, {}).get("aliases", ()):
            owner.setdefault(alias, item)

    created: list[tuple[RecurringItem, _Series]] = []
    for s in _build_series(groups, counts):
        linked: list[RecurringItem] = []
        for part in reversed(s.parts):  # the latest name first
            item = owner.get(part)
            if item is not None and item not in linked:
                linked.append(item)
        if linked:
            primary = min(linked, key=lambda i: STATUS_ORDER.get(i.status, 9))
            found = s.classify(refresh=True)
            if found is not None:
                _refresh(primary, s, found, today, memo, owner)
            for other in linked:
                if other is primary:
                    continue  # another item on a part of the series: refreshed from its own part
                parts = [p for p in s.parts if owner.get(p) is other]
                rows = [r for r in s.rows if (r.account_id, merchant_key(r.merchant_name, r.name)) in parts]
                own = _Series(parts, rows, s.crowded)
                if rows and (f := own.classify(refresh=True)) is not None:
                    _refresh(other, own, f, today, memo, owner)
            continue
        found = s.classify()
        if found is None or _suppressed(s, items, memo):
            continue
        cadence, days = found
        last = s.rows[-1]
        # Don't suggest a series that has clearly stopped (an expected occurrence was missed).
        max_age = TWO_CHARGE_MAX_AGE_DAYS if len(s.rows) == 2 and cadence == "monthly" \
            else PERIOD_DAYS[cadence] + BANDS[cadence][1]
        if (today - last.date).days > max_age:
            continue
        account_id, key = s.parts[-1]
        item = RecurringItem(
            name=display_name(last.merchant_name, last.name),
            merchant_key=key,
            account_id=account_id,
            amount_cents=last.amount_cents,
            cadence=cadence,
            next_date=next_after(last.date, cadence, days, today),
            status="suggested",
            include_in_forecast=True,
            source="detected",
            last_seen_date=last.date,
            month_days=json.dumps(days) if days else None,
            category_id=category_from_rows(s.rows, cmap),
        )
        session.add(item)
        items.append(item)
        for part in s.parts:
            owner.setdefault(part, item)
        created.append((item, s))
    session.flush()
    for item, s in created:
        memo[item.id] = {
            "key": item.merchant_key, "created": _stamp(item), "amount": item.amount_cents,
            "account": item.account_id, "aliases": [p for p in s.parts if p != (item.account_id, item.merchant_key)],
        }
    _save_memo(session, memo)
    return [item.id for item, _s in created]


# ------------------------------------------------------------------ budget category (Release 3.6)


def category_from_rows(rows: list, cmap: CategoryMap) -> str | None:
    """The most common effective category of these transactions (ties: the most recent).

    OTHER (and uncategorized), hidden, unknown and transfer-kind categories don't count, nor
    do transfers (a card payment's bill gets no budget category: the card's purchases are
    the spending); None when nothing is left.
    """
    counts: dict[str, int] = defaultdict(int)
    latest: dict[str, tuple] = {}
    for row in rows:
        if getattr(row, "is_transfer", False):
            continue
        cid = row.category
        info = cmap.by_id.get(cid) if cid else None
        if info is None or cid == OTHER or info.hidden or info.kind == "transfer":
            continue
        counts[cid] += 1
        latest[cid] = max(latest.get(cid, (dt.date.min, 0)), (row.date, row.id))
    if not counts:
        return None
    return max(counts, key=lambda cid: (counts[cid], latest[cid]))


def _sign(cents: int) -> int:
    return 1 if cents > 0 else -1


def matched_history(session: Session, items: list[RecurringItem], today: dt.date) -> dict[int, list]:
    """Each item's past transactions (detection's input): same merchant key and sign, on its
    account (any visible account when the item has none), plus (3.19) the other names and
    accounts detection noted for it."""
    by_key: dict[tuple[str, int], list] = defaultdict(list)
    for row in _history(session, today):
        by_key[(merchant_key(row.merchant_name, row.name), _sign(row.amount_cents))].append(row)
    aliases = item_aliases(session, items)
    out: dict[int, list] = {}
    for item in items:
        sign = _sign(item.amount_cents)
        rows = by_key.get((item.merchant_key, sign), [])
        mine = [r for r in rows if item.account_id is None or r.account_id == item.account_id]
        extra = [r for a, k in aliases.get(item.id, ()) for r in by_key.get((k, sign), []) if r.account_id == a]
        if extra:
            mine = sorted({r.id: r for r in mine + extra}.values(), key=lambda r: (r.date, r.id))
        out[item.id] = mine
    return out


def category_from_history(session: Session, item: RecurringItem, today: dt.date) -> str | None:
    return category_from_rows(matched_history(session, [item], today)[item.id], load_categories(session))


def guess_category(
    session: Session, today: dt.date, name: str, amount_cents: int | None = None, account_id: int | None = None
) -> str | None:
    """A budget category guess for a new manual item, from the merchant's past transactions.

    Matches the merchant key of ``name`` exactly or as a leading word run ("rocket mortgage"
    matches "rocket mortgage pmt"); same sign when an amount is given; same account when given.
    """
    key = merchant_key(name, name)
    rows = [
        r for r in _history(session, today)
        if (amount_cents is None or _sign(r.amount_cents) == _sign(amount_cents))
        and (account_id is None or r.account_id == account_id)
        and ((k := merchant_key(r.merchant_name, r.name)) == key or k.startswith(key + " "))
    ]
    return category_from_rows(rows, load_categories(session))


BACKFILL_KEY = "recurring_category_backfill"


def backfill_categories_once(session: Session, today: dt.date) -> int:
    """After the v6 upgrade: give active items without a category one from their history.

    The migration leaves a flag in app_settings; this runs once (the flag is removed), so a
    category the user clears later is never refilled. Returns how many items were filled.
    """
    flag = session.get(AppSetting, BACKFILL_KEY)
    if flag is None:
        return 0
    items = list(session.scalars(
        select(RecurringItem).where(RecurringItem.status == "active", RecurringItem.category_id.is_(None))
    ))
    filled = 0
    if items:
        cmap = load_categories(session)
        history = matched_history(session, items, today)
        for item in items:
            cid = category_from_rows(history[item.id], cmap)
            if cid is not None:
                item.category_id = cid
                filled += 1
    session.delete(flag)
    session.flush()
    return filled


BACKFILL_FAILURES_KEY = "recurring_category_backfill_failures"
BACKFILL_MAX_FAILURES = 3


def safe_backfill(session: Session, today: dt.date) -> None:
    """``backfill_categories_once`` and commit; a failure is logged (type only) and ignored.

    After ``BACKFILL_MAX_FAILURES`` failures in a row the flag is removed, so a backfill that
    can't succeed doesn't run on every list and calendar request forever.
    """
    try:
        if session.get(AppSetting, BACKFILL_KEY) is None:
            return
        backfill_categories_once(session, today)
        count = session.get(AppSetting, BACKFILL_FAILURES_KEY)
        if count is not None:
            session.delete(count)
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        log.error("recurring category backfill failed: %s", type(exc).__name__)
        try:
            _count_backfill_failure(session)
        except Exception as exc2:  # noqa: BLE001
            session.rollback()
            log.error("recurring category backfill failure count failed: %s", type(exc2).__name__)


def _count_backfill_failure(session: Session) -> None:
    row = session.get(AppSetting, BACKFILL_FAILURES_KEY)
    try:
        failures = int(row.value) + 1 if row is not None and row.value else 1
    except ValueError:
        failures = 1
    if failures >= BACKFILL_MAX_FAILURES:
        flag = session.get(AppSetting, BACKFILL_KEY)
        if flag is not None:
            session.delete(flag)
        if row is not None:
            session.delete(row)
        log.error("recurring category backfill given up after %d failures", failures)
    elif row is None:
        session.add(AppSetting(key=BACKFILL_FAILURES_KEY, value=str(failures)))
    else:
        row.value = str(failures)
    session.commit()


# ------------------------------------------------------------------ output


def recurring_out(item: RecurringItem, account_name: str | None, category_name: str | None = None) -> dict:
    return {
        "id": item.id,
        "name": item.name,
        "account_id": item.account_id,
        "account_name": account_name,
        "amount": from_cents(item.amount_cents),
        "cadence": item.cadence,
        "next_date": item.next_date.isoformat(),
        "status": item.status,
        "include_in_forecast": bool(item.include_in_forecast),
        "source": item.source,
        "last_seen_date": item.last_seen_date.isoformat() if item.last_seen_date else None,
        "reminder_days": int(item.reminder_days or 0),
        "start_date": item.start_date.isoformat() if item.start_date else None,
        "anchor_days": parse_days(item.month_days),
        "category_id": item.category_id,
        "category_name": category_name if item.category_id else None,
    }

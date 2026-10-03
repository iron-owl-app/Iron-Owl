"""Reports v2 (Release 3.14): budget plans on the monthly report, Year over year, Subscriptions,
and the stores on each Habits calendar day."""
from __future__ import annotations

import datetime as dt

import pytest

from app.models import RecurringItem
from app.services import reports
from tests.conftest import add_budget, add_txn, make_account


def ok(response) -> dict:
    assert response.status_code == 200, response.text
    return response.json()


def test_new_routes_need_a_session(client):
    for url in ("/api/reports/yoy", "/api/reports/yoy?mode=month", "/api/reports/subscriptions"):
        assert client.get(url).status_code == 401, url


# ------------------------------------------------------------------ planned


def test_monthly_planned(unlocked, db, fixed_today):
    chk = make_account(db, "Checking", "bank", 100)
    add_txn(db, chk.id, "2026-08-03", -10, "Grocer")
    add_budget(db, "2026-06", {"FOOD_AND_DRINK": 999})  # before the range
    add_budget(db, "2026-08", {"FOOD_AND_DRINK": 300, "LOAN_PAYMENTS": 500, "MEDICAL": 0})
    add_budget(db, "2026-09", {"FOOD_AND_DRINK": 350.5, "GENERAL_MERCHANDISE": -20}, removed=["MEDICAL"])
    add_budget(db, "2026-09", {"INCOME": 100})  # not a spending or fixed category
    months = ok(unlocked.get("/api/reports/monthly", params={"months": 3}))["months"]
    assert [(m["month"], m["planned"]) for m in months] == [
        ("2026-07", {}),
        ("2026-08", {"FOOD_AND_DRINK": 300.0, "LOAN_PAYMENTS": 500.0}),
        ("2026-09", {"FOOD_AND_DRINK": 350.5}),
    ]


def test_monthly_planned_empty_before_budgets(unlocked, db, fixed_today):
    months = ok(unlocked.get("/api/reports/monthly", params={"months": 2}))["months"]
    assert [m["planned"] for m in months] == [{}, {}]


def test_monthly_first_month(unlocked, db, fixed_today):
    """``first_month``: the first transaction on a visible spending account, however far back
    (not limited by ``months``); None without any."""
    get = lambda: ok(unlocked.get("/api/reports/monthly", params={"months": 3}))  # noqa: E731
    assert get()["first_month"] is None
    hidden = make_account(db, "Old card", "credit", 0, hidden=True)
    broker = make_account(db, "Brokerage", "investment", 0)
    add_txn(db, hidden.id, "2019-02-10", -5, "Hidden")
    add_txn(db, broker.id, "2019-03-10", -5, "Broker")
    assert get()["first_month"] is None
    chk = make_account(db, "Checking", "bank", 100)
    add_txn(db, chk.id, "2026-08-03", -10, "Grocer")
    assert get()["first_month"] == "2026-08"
    add_txn(db, chk.id, "2021-05-20", 50, "Refund", is_transfer=True)  # any kind counts
    rep = get()
    assert rep["first_month"] == "2021-05"
    assert [m["month"] for m in rep["months"]] == ["2026-07", "2026-08", "2026-09"]


# ------------------------------------------------------------------ Year over year: ranges


@pytest.mark.parametrize(("day", "mode", "current", "previous"), [
    ("2026-09-26", "year", ("2026-01-01", "2026-09-26"), ("2025-01-01", "2025-09-26")),
    ("2026-09-26", "month", ("2026-09-01", "2026-09-26"), ("2025-09-01", "2025-09-26")),
    # Feb 29 -> Feb 28 last year.
    ("2028-02-29", "year", ("2028-01-01", "2028-02-29"), ("2027-01-01", "2027-02-28")),
    ("2028-02-29", "month", ("2028-02-01", "2028-02-29"), ("2027-02-01", "2027-02-28")),
    # The day after a leap year's Feb 28 is the same days: Feb 29 last year isn't included.
    ("2029-02-28", "month", ("2029-02-01", "2029-02-28"), ("2028-02-01", "2028-02-28")),
    # Month ends.
    ("2026-03-31", "month", ("2026-03-01", "2026-03-31"), ("2025-03-01", "2025-03-31")),
    ("2026-12-31", "year", ("2026-01-01", "2026-12-31"), ("2025-01-01", "2025-12-31")),
    ("2026-01-01", "year", ("2026-01-01", "2026-01-01"), ("2025-01-01", "2025-01-01")),
])
def test_yoy_ranges(day, mode, current, previous):
    d = dt.date.fromisoformat
    (cs, ce), (ps, pe) = reports.yoy_ranges(mode, d(day))
    assert (cs.isoformat(), ce.isoformat()) == current
    assert (ps.isoformat(), pe.isoformat()) == previous


def test_yoy_ranges_reject_other_modes():
    with pytest.raises(ValueError):
        reports.yoy_ranges("week", dt.date(2026, 9, 26))


@pytest.mark.parametrize(("day", "month", "current", "previous"), [
    # Release 3.18: a finished month is the whole month vs the whole same month last year.
    ("2026-09-26", "2026-03", ("2026-03-01", "2026-03-31"), ("2025-03-01", "2025-03-31")),
    ("2026-09-26", "2025-12", ("2025-12-01", "2025-12-31"), ("2024-12-01", "2024-12-31")),
    # February in and after a leap year: each side is its own whole month.
    ("2028-09-26", "2028-02", ("2028-02-01", "2028-02-29"), ("2027-02-01", "2027-02-28")),
    ("2029-09-26", "2029-02", ("2029-02-01", "2029-02-28"), ("2028-02-01", "2028-02-29")),
    # Today's month stays "so far", given or not.
    ("2026-09-26", "2026-09", ("2026-09-01", "2026-09-26"), ("2025-09-01", "2025-09-26")),
    ("2026-09-26", None, ("2026-09-01", "2026-09-26"), ("2025-09-01", "2025-09-26")),
])
def test_yoy_ranges_any_month(day, month, current, previous):
    d = dt.date.fromisoformat
    (cs, ce), (ps, pe) = reports.yoy_ranges("month", d(day), month)
    assert (cs.isoformat(), ce.isoformat()) == current
    assert (ps.isoformat(), pe.isoformat()) == previous


def test_yoy_ranges_refuse_a_future_month():
    with pytest.raises(ValueError):
        reports.yoy_ranges("month", dt.date(2026, 9, 26), "2026-10")


def test_yoy_month_is_validated(unlocked, fixed_today):
    bad = ("2026-13", "2026-00", "2026-1", "26-09", "", "2026-09-01", "<script>", "0999-01", "1899-12",
           "2026-10", "2027-01")  # the last two are after today (2026-09-26)
    for month in bad:
        r = unlocked.get("/api/reports/yoy", params={"mode": "month", "month": month})
        assert r.status_code == 422, month
        if month:
            assert month not in r.text, month  # never echoed back
    rep = ok(unlocked.get("/api/reports/yoy", params={"mode": "month", "month": "1900-01"}))
    assert rep["month"] == "1900-01" and rep["partial"] is False


def test_yoy_month_fields(unlocked, fixed_today):
    year = ok(unlocked.get("/api/reports/yoy", params={"mode": "year", "month": "2026-03"}))
    assert year["month"] is None and year["partial"] is True  # month is ignored in year mode
    assert year["current"]["start"] == "2026-01-01"
    now = ok(unlocked.get("/api/reports/yoy", params={"mode": "month"}))
    assert now["month"] == "2026-09" and now["partial"] is True
    same = ok(unlocked.get("/api/reports/yoy", params={"mode": "month", "month": "2026-09"}))
    assert same == now


def test_yoy_past_month_totals(yoy_data):
    rep = ok(yoy_data.get("/api/reports/yoy", params={"mode": "month", "month": "2025-09"}))
    assert rep["month"] == "2025-09" and rep["partial"] is False
    cur, prev = rep["current"], rep["previous"]
    assert (cur["start"], cur["end"], prev["start"], prev["end"]) == (
        "2025-09-01", "2025-09-30", "2024-09-01", "2024-09-30")
    # The whole of September 2025, the 27th included this time.
    assert cur["total"] == 1044.0 and cur["months"] == [{"month": "2025-09", "total": 1044.0}]
    assert cur["merchants"][0] == {"name": "Walmart", "category": "FOOD_AND_DRINK", "total": 1039.0, "count": 2}
    # Nothing a year before: the page still answers, last year is all zeros.
    assert prev["total"] == 0 and prev["categories"] == [] and prev["merchants"] == []
    feb = ok(yoy_data.get("/api/reports/yoy", params={"mode": "month", "month": "2026-02"}))
    assert (feb["current"]["end"], feb["current"]["total"]) == ("2026-02-28", 30.0)
    assert feb["previous"]["total"] == 0


def test_yoy_mode_is_validated(unlocked, fixed_today):
    for bad in ("week", "YEAR", "", "year;drop", "x" * 20):
        assert unlocked.get("/api/reports/yoy", params={"mode": bad}).status_code == 422, bad
    assert ok(unlocked.get("/api/reports/yoy"))["mode"] == "year"


# ------------------------------------------------------------------ Year over year: totals and stores


@pytest.fixture
def yoy_data(unlocked, db, fixed_today):
    c = unlocked
    chk = make_account(db, "Checking", "bank", 2000)
    visa = make_account(db, "Visa", "credit", 300)
    ira = make_account(db, "IRA", "retirement", 5000)
    # Last year (2025-01-01 .. 2025-09-26).
    add_txn(db, chk.id, "2025-01-10", -80, "WALMART 123", merchant="Walmart")
    add_txn(db, visa.id, "2025-03-15", -60, "WALMART 123", merchant="Walmart", category="GENERAL_MERCHANDISE")
    add_txn(db, visa.id, "2025-09-03", -5, "SBUX", merchant="Starbucks")
    add_txn(db, chk.id, "2025-09-26", -40, "WALMART 123", merchant="Walmart")  # the same day: counts
    add_txn(db, chk.id, "2025-09-27", -999, "WALMART 123", merchant="Walmart")  # a day later: left out
    add_txn(db, chk.id, "2025-12-01", -500, "Target", merchant="Target")  # outside both
    # This year (2026-01-01 .. 2026-09-26).
    add_txn(db, chk.id, "2026-01-05", -100, "WALMART 9", merchant="Walmart")
    add_txn(db, chk.id, "2026-02-14", -50, "WALMART 9", merchant="Walmart")
    add_txn(db, chk.id, "2026-02-20", 20, "WALMART 9", merchant="Walmart")  # refund
    add_txn(db, visa.id, "2026-09-20", -5, "SBUX", merchant="Starbucks")
    add_txn(db, visa.id, "2026-09-21", -6, "SBUX", merchant="Starbucks")
    costco = add_txn(db, visa.id, "2026-09-22", -300, "Costco run", merchant="Costco", category="GENERAL_MERCHANDISE")
    add_txn(db, visa.id, "2026-09-24", -30, "Kroger", merchant="Kroger", pending=True)  # pending counts
    add_txn(db, chk.id, "2026-08-15", -500, "Car loan", category="LOAN_PAYMENTS")  # fixed: left out
    add_txn(db, chk.id, "2026-09-01", 3000, "Paycheck", category="INCOME")
    add_txn(db, chk.id, "2026-09-10", -1000, "To savings", category="TRANSFER_OUT")
    add_txn(db, chk.id, "2026-09-11", -40, "Venmo", is_transfer=True)
    add_txn(db, ira.id, "2026-09-07", -999, "Not spending")
    r_ = c.put(f"/api/transactions/{costco.id}/splits", json={"splits": [
        {"amount": -120, "category": "FOOD_AND_DRINK"}, {"amount": -180, "category": "GENERAL_MERCHANDISE"}]})
    assert r_.status_code == 200
    return c


FOOD = {"id": "FOOD_AND_DRINK", "name": "Food and drink", "hue": 25, "bill": False}
SHOP = {"id": "GENERAL_MERCHANDISE", "name": "Shopping", "hue": 300, "bill": False}


def test_yoy_year(yoy_data):
    rep = ok(yoy_data.get("/api/reports/yoy", params={"mode": "year"}))
    assert rep["mode"] == "year" and rep["first_month"] == "2025-01"
    cur, prev = rep["current"], rep["previous"]
    assert (cur["start"], cur["end"], prev["start"], prev["end"]) == (
        "2026-01-01", "2026-09-26", "2025-01-01", "2025-09-26")
    # Food: Walmart 100 + 50 - 20, Starbucks 11, Costco's food part 120, Kroger 30 (pending).
    assert cur["total"] == 471.0
    assert cur["categories"] == [{**FOOD, "total": 291.0}, {**SHOP, "total": 180.0}]
    assert cur["months"] == [{"month": f"2026-{m:02d}", "total": t} for m, t in
                             zip(range(1, 10), (100.0, 30.0, 0, 0, 0, 0, 0, 0, 341.0))]
    assert cur["merchants"] == [
        {"name": "Costco", "category": "GENERAL_MERCHANDISE", "total": 300.0, "count": 1},
        {"name": "Walmart", "category": "FOOD_AND_DRINK", "total": 130.0, "count": 3},
        {"name": "Kroger", "category": "FOOD_AND_DRINK", "total": 30.0, "count": 1},
        {"name": "Starbucks", "category": "FOOD_AND_DRINK", "total": 11.0, "count": 2},
    ]
    assert prev["total"] == 185.0
    assert prev["categories"] == [{**FOOD, "total": 125.0}, {**SHOP, "total": 60.0}]
    assert prev["months"] == [{"month": f"2025-{m:02d}", "total": t} for m, t in
                              zip(range(1, 10), (80.0, 0, 60.0, 0, 0, 0, 0, 0, 45.0))]
    assert prev["merchants"] == [
        {"name": "Walmart", "category": "FOOD_AND_DRINK", "total": 180.0, "count": 3},
        {"name": "Starbucks", "category": "FOOD_AND_DRINK", "total": 5.0, "count": 1},
    ]


def test_yoy_month(yoy_data):
    rep = ok(yoy_data.get("/api/reports/yoy", params={"mode": "month"}))
    cur, prev = rep["current"], rep["previous"]
    assert (cur["start"], prev["start"], prev["end"]) == ("2026-09-01", "2025-09-01", "2025-09-26")
    assert cur["months"] == [{"month": "2026-09", "total": 341.0}] and cur["total"] == 341.0
    assert prev["months"] == [{"month": "2025-09", "total": 45.0}]
    assert prev["merchants"] == [
        {"name": "Walmart", "category": "FOOD_AND_DRINK", "total": 40.0, "count": 1},
        {"name": "Starbucks", "category": "FOOD_AND_DRINK", "total": 5.0, "count": 1},
    ]


def test_yoy_leap_day(unlocked, db, fixed_today):
    chk = make_account(db, "Checking", "bank", 100)
    add_txn(db, chk.id, "2027-02-28", -10, "Walmart", merchant="Walmart")
    add_txn(db, chk.id, "2027-03-01", -99, "Walmart", merchant="Walmart")  # after the same day
    add_txn(db, chk.id, "2028-02-29", -20, "Walmart", merchant="Walmart")
    fixed_today.set(dt.date(2028, 2, 29))
    rep = ok(unlocked.get("/api/reports/yoy", params={"mode": "month"}))
    assert (rep["previous"]["end"], rep["previous"]["total"]) == ("2027-02-28", 10.0)
    assert (rep["current"]["end"], rep["current"]["total"]) == ("2028-02-29", 20.0)
    year = ok(unlocked.get("/api/reports/yoy", params={"mode": "year"}))
    assert [m["month"] for m in year["previous"]["months"]] == ["2027-01", "2027-02"]
    assert [m["month"] for m in year["current"]["months"]] == ["2028-01", "2028-02"]


def test_yoy_without_last_year(unlocked, db, fixed_today):
    empty = ok(unlocked.get("/api/reports/yoy"))
    assert empty["first_month"] is None and empty["current"]["total"] == 0 and empty["previous"]["total"] == 0
    chk = make_account(db, "Checking", "bank", 100)
    add_txn(db, chk.id, "2026-09-05", -25, "Kroger", merchant="Kroger")
    rep = ok(unlocked.get("/api/reports/yoy"))
    assert rep["first_month"] == "2026-09"
    assert rep["current"]["total"] == 25.0
    prev = rep["previous"]
    assert prev["total"] == 0 and prev["merchants"] == [] and prev["categories"] == []
    assert all(m["total"] == 0 for m in prev["months"]) and len(prev["months"]) == 9


def test_yoy_category_bill_flag(unlocked, db, fixed_today):
    chk = make_account(db, "Checking", "bank", 100)
    for month in range(4, 10):
        add_txn(db, chk.id, f"2026-{month:02d}-01", -1200, "RENT PMT", merchant="Landlord", category="RENT_AND_UTILITIES")
    db.add(RecurringItem(name="Rent", merchant_key="landlord", account_id=chk.id, amount_cents=-120000,
                         cadence="monthly", next_date=dt.date(2026, 10, 1), status="active", source="detected"))
    db.commit()
    cats = ok(unlocked.get("/api/reports/yoy"))["current"]["categories"]
    assert cats == [{"id": "RENT_AND_UTILITIES", "name": "Rent and utilities", "hue": 85, "bill": True,
                     "total": 7200.0}]


# ------------------------------------------------------------------ Habits: stores per day


def test_habits_day_places(unlocked, db, fixed_today):
    visa = make_account(db, "Visa", "credit", 100)
    add_txn(db, visa.id, "2026-09-20", -5, "SBUX", merchant="Starbucks")
    add_txn(db, visa.id, "2026-09-20", -30, "Kroger", merchant="Kroger")
    add_txn(db, visa.id, "2026-09-20", -12, "Target", merchant="Target")
    add_txn(db, visa.id, "2026-09-20", 12, "Target", merchant="Target")  # refunded the same day
    add_txn(db, visa.id, "2026-09-21", -4, "SBUX", merchant="Starbucks")
    add_txn(db, visa.id, "2026-09-21", -9, "Deli", merchant="Deli")
    add_txn(db, visa.id, "2026-09-21", -7, "Bakery", merchant="Bakery")
    days = {d["date"]: d for d in ok(unlocked.get("/api/reports/habits", params={"month": "2026-09"}))["days"]}
    assert days["2026-09-20"]["places"] == ["Kroger", "Starbucks"]
    assert days["2026-09-21"]["places"] == ["Deli", "Bakery"]  # top 2 only
    assert days["2026-09-19"] == {"date": "2026-09-19", "spent": 0.0, "places": []}


# ------------------------------------------------------------------ Subscriptions


def _item(db, name, key, cents, cadence="monthly", account=None, category=None, status="active"):
    item = RecurringItem(name=name, merchant_key=key, account_id=account.id if account else None,
                         amount_cents=cents, cadence=cadence, next_date=dt.date(2026, 10, 3), status=status,
                         source="detected", category_id=category)
    db.add(item)
    db.commit()
    return item


def _monthly_charges(db, account, merchant, category, amounts: dict[str, float], day=3):
    for month, amount in amounts.items():
        add_txn(db, account.id, f"{month}-{day:02d}", -amount, merchant.upper(), merchant=merchant, category=category)


def _months(first: str, last: str) -> list[str]:
    out, m = [], first
    while m <= last:
        out.append(m)
        y, mm = int(m[:4]), int(m[5:])
        m = f"{y + mm // 12}-{mm % 12 + 1:02d}"
    return out


@pytest.fixture
def subs(unlocked, db, fixed_today):
    visa = make_account(db, "Visa", "credit", 100)
    chk = make_account(db, "Checking", "bank", 100)
    # Big one-off purchases keep these categories from looking like bills.
    for cat in ("ENTERTAINMENT", "GENERAL_MERCHANDISE", "FOOD_AND_DRINK"):
        add_txn(db, chk.id, "2026-08-20", -900, "One-off", merchant=f"Store {cat}", category=cat)

    # Netflix: $15.49 since last October, $17.99 from July -> went up $2.50 in July; a full year.
    _monthly_charges(db, visa, "Netflix", "ENTERTAINMENT",
                     {m: 15.49 for m in _months("2025-10", "2026-06")} | {m: 17.99 for m in _months("2026-07", "2026-09")})
    # A charge older than 12 months doesn't count.
    add_txn(db, visa.id, "2025-09-01", -5.00, "NETFLIX", merchant="Netflix", category="ENTERTAINMENT")
    netflix = _item(db, "Netflix", "netflix", -1799, account=visa, category="ENTERTAINMENT")
    # Spotify: no category on the item (from its history), same price since March: no full year.
    _monthly_charges(db, visa, "Spotify", "ENTERTAINMENT", {m: 10.99 for m in _months("2026-03", "2026-09")})
    spotify = _item(db, "Spotify", "spotify", -1099, account=visa)
    # Hulu: went down $2.00 in May (it was $9.99).
    _monthly_charges(db, visa, "Hulu", "ENTERTAINMENT",
                     {m: 9.99 for m in _months("2026-01", "2026-04")} | {m: 7.99 for m in _months("2026-05", "2026-09")})
    hulu = _item(db, "Hulu", "hulu", -799, account=visa, category="ENTERTAINMENT")
    # iCloud: 30 cents more isn't a price change (needs more than $0.50).
    _monthly_charges(db, visa, "iCloud", "GENERAL_MERCHANDISE",
                     {m: 2.99 for m in _months("2025-10", "2026-05")} | {m: 3.29 for m in _months("2026-06", "2026-09")})
    icloud = _item(db, "iCloud", "icloud", -329, account=visa, category="GENERAL_MERCHANDISE")
    # Two plans at one store: each charge goes to the closer item, so neither "changed".
    _monthly_charges(db, visa, "Apple", "GENERAL_MERCHANDISE", {m: 0.99 for m in _months("2026-01", "2026-09")}, day=5)
    _monthly_charges(db, visa, "Apple", "GENERAL_MERCHANDISE", {m: 9.99 for m in _months("2026-01", "2026-09")}, day=6)
    apple_small = _item(db, "Apple storage", "apple", -99, account=visa, category="GENERAL_MERCHANDISE")
    apple_big = _item(db, "Apple TV", "apple", -999, account=visa, category="GENERAL_MERCHANDISE")
    # Charges on another account aren't this item's.
    _monthly_charges(db, chk, "Gym", "GENERAL_MERCHANDISE", {"2026-06": 10, "2026-07": 50})
    gym = _item(db, "Gym", "gym", -3000, account=visa, category="GENERAL_MERCHANDISE")
    # Weekly and yearly convert to a month.
    coffee = _item(db, "Coffee club", "coffee club", -500, cadence="weekly", account=visa, category="FOOD_AND_DRINK")
    news = _item(db, "News", "news", -12000, cadence="yearly", account=visa, category="GENERAL_MERCHANDISE")
    # An item with no category at all.
    mystery = _item(db, "Mystery", "mystery", -700, account=visa)
    # No account on the item: most of its charges are on the card, so it's a subscription there.
    _monthly_charges(db, visa, "Disney", "ENTERTAINMENT", {m: 13.99 for m in _months("2026-06", "2026-09")})
    _monthly_charges(db, chk, "Disney", "ENTERTAINMENT", {"2026-05": 13.99})
    disney = _item(db, "Disney+", "disney", -1399, category="ENTERTAINMENT")
    # Left out: bills paid from checking (an account on the item, or most charges there), an item
    # with no account and no charges, a hidden card.
    _monthly_charges(db, chk, "Landlord", "RENT_AND_UTILITIES", {m: 1200 for m in _months("2026-04", "2026-09")})
    _item(db, "Rent", "landlord", -120000, account=chk, category="RENT_AND_UTILITIES")
    _monthly_charges(db, chk, "Phone co", "GENERAL_SERVICES", {m: 80 for m in _months("2026-05", "2026-09")})
    _monthly_charges(db, visa, "Phone co", "GENERAL_SERVICES", {"2026-04": 80})
    _item(db, "Phone", "phone co", -8000, category="GENERAL_SERVICES")
    _item(db, "Insurance", "insurance", -9000, category="GENERAL_SERVICES")
    old_card = make_account(db, "Old card", "credit", 0, hidden=True)
    _item(db, "Old card thing", "old card thing", -500, account=old_card, category="ENTERTAINMENT")
    # Left out even on the card: loan payments, the "Paying off debt" category, a transfer, a card
    # bill, dismissed, money in, one time.
    _item(db, "Car loan", "car loan", -30000, account=visa, category="LOAN_PAYMENTS")
    _item(db, "Extra debt payment", "extra debt", -5000, account=visa, category="BANK_FEES")
    _item(db, "To savings", "to savings", -20000, account=visa, category="TRANSFER_OUT")
    for month in _months("2026-06", "2026-09"):
        add_txn(db, chk.id, f"{month}-12", -250, "VISA PAYMENT", merchant="Visa payment", category="TRANSFER_OUT",
                is_transfer=True, transfer_source="auto")
    _item(db, "Visa payment", "visa payment", -25000, account=chk)
    _item(db, "Old", "old", -999, account=visa, category="ENTERTAINMENT", status="dismissed")
    _item(db, "Paycheck", "paycheck", 300000, account=visa, category="INCOME")
    _item(db, "Concert", "concert", -8000, cadence="once", account=visa, category="ENTERTAINMENT")
    return {"netflix": netflix, "spotify": spotify, "hulu": hulu, "icloud": icloud, "apple_small": apple_small,
            "apple_big": apple_big, "gym": gym, "coffee": coffee, "news": news, "mystery": mystery,
            "disney": disney, "visa": visa}


def test_subscriptions(unlocked, subs, monkeypatch):
    from app.services import debt_budget

    monkeypatch.setattr(debt_budget, "filed_category", lambda session, cmap: "BANK_FEES")
    rep = ok(unlocked.get("/api/reports/subscriptions"))
    got = {i["id"]: i for i in rep["items"]}
    visa = subs.pop("visa")
    assert set(got) == {i.id for i in subs.values()}
    assert all(i["card"] == {"id": visa.id, "name": "Visa"} for i in rep["items"])

    assert got[subs["netflix"].id] == {
        "id": subs["netflix"].id, "name": "Netflix", "category": "ENTERTAINMENT", "cadence": "monthly",
        "monthly": 17.99, "amount": 17.99, "change": {"amount": 2.5, "month": "2026-07"}, "full_year": True,
        "card": {"id": visa.id, "name": "Visa"}, "next_date": "2026-10-03",
    }
    assert got[subs["spotify"].id]["category"] == "ENTERTAINMENT"
    assert (got[subs["spotify"].id]["change"], got[subs["spotify"].id]["full_year"]) == (None, False)
    assert got[subs["hulu"].id]["change"] == {"amount": -2.0, "month": "2026-05"}
    assert (got[subs["icloud"].id]["change"], got[subs["icloud"].id]["full_year"]) == (None, True)
    assert got[subs["apple_small"].id]["change"] is None and got[subs["apple_big"].id]["change"] is None
    assert (got[subs["gym"].id]["change"], got[subs["gym"].id]["full_year"]) == (None, False)
    assert (got[subs["coffee"].id]["monthly"], got[subs["coffee"].id]["amount"]) == (21.67, 5.0)
    assert (got[subs["news"].id]["monthly"], got[subs["news"].id]["cadence"]) == (10.0, "yearly")
    assert got[subs["mystery"].id]["category"] is None
    # Without an account: only its own charges count (Disney's 4 on the card, 1 on checking).
    assert got[subs["disney"].id]["change"] is None

    monthly = [i["monthly"] for i in rep["items"]]
    assert monthly == sorted(monthly, reverse=True)
    assert rep["monthly_total"] == round(sum(monthly), 2)


def test_subscriptions_next_date(unlocked, db, subs):
    """Release 3.15: the next charge still expected, with calendar moves and skips applied."""
    from app.models import RecurringOverride

    def next_dates():
        return {i["id"]: i["next_date"] for i in ok(unlocked.get("/api/reports/subscriptions"))["items"]}

    got = next_dates()
    assert got[subs["netflix"].id] == "2026-10-03"
    assert got[subs["coffee"].id] == "2026-10-03"  # weekly: today (Sep 26) is before its next_date
    assert got[subs["news"].id] == "2026-10-03"  # yearly
    # Moved, skipped, and a next_date in the past (the rule rolls forward to the next one).
    db.add(RecurringOverride(recurring_id=subs["netflix"].id, base_date=dt.date(2026, 10, 3), moved_to=dt.date(2026, 10, 6)))
    db.add(RecurringOverride(recurring_id=subs["hulu"].id, base_date=dt.date(2026, 10, 3), skipped=True))
    subs["spotify"].next_date = dt.date(2026, 8, 3)
    db.commit()
    got = next_dates()
    assert got[subs["netflix"].id] == "2026-10-06"
    assert got[subs["hulu"].id] == "2026-11-03"
    assert got[subs["spotify"].id] == "2026-10-03"
    assert all(d is None or d >= "2026-09-26" for d in got.values())


def test_subscriptions_none(unlocked, fixed_today):
    assert ok(unlocked.get("/api/reports/subscriptions")) == {"items": [], "monthly_total": 0.0}


def test_price_change_rules():
    from types import SimpleNamespace as N

    day = dt.date(2026, 9, 26)

    def charges(*pairs):
        return [N(id=i, date=dt.date.fromisoformat(d), amount_cents=c) for i, (d, c) in enumerate(pairs)]

    # The most recent change wins; amounts are compared as money out.
    assert reports.price_change(charges(("2026-01-03", -1000), ("2026-03-03", -1200), ("2026-06-03", -1500)),
                                day) == ({"amount": 3.0, "month": "2026-06"}, False)
    # Exactly $0.50 isn't enough; neither is 1% of a big charge.
    assert reports.price_change(charges(("2026-01-03", -1000), ("2026-02-03", -1050)), day)[0] is None
    assert reports.price_change(charges(("2026-01-03", -100000), ("2026-02-03", -100090)), day)[0] is None
    # No charges: nothing to say.
    assert reports.price_change([], day) == (None, False)


# ------------------------------------------------------------------ exact Undo of budget rows


@pytest.fixture
def plan(unlocked, db, fixed_today):
    """$850 in checking; September plans Food 100 and Transportation 50 (October has no rows)."""
    chk = make_account(db, "Checking", "bank", 850)
    add_budget(db, "2026-09", {"FOOD_AND_DRINK": 100, "TRANSPORTATION": 50})
    add_txn(db, chk.id, "2026-09-03", -10, "Coffee")
    return unlocked


ROW = {"assigned": 10.0, "removed": False, "restart": False}


def _row(client, month, category):
    return ok(client.get(f"/api/budgets/{month}/rows/{category}"))["state"]


def _restore(client, month, *rows):
    """rows: (category, state, expected)."""
    return client.put(f"/api/budgets/{month}/rows", json={"rows": [
        {"category": c, "state": st, "expected": ex} for c, st, ex in rows]})


def _db_rows(db, category):
    from sqlalchemy import select

    from app.models import Budget

    db.expire_all()
    return [(b.month, b.limit_cents, b.removed, b.restart)
            for b in db.scalars(select(Budget).where(Budget.category == category).order_by(Budget.month))]


@pytest.mark.parametrize("category", ["FOOD_AND_DRINK", "ENTERTAINMENT"])  # carried forward / not in the plan
def test_restore_rows_puts_back_no_row(plan, db, category):
    before = ok(plan.get("/api/budgets", params={"month": "2026-10"}))
    rows_before = _db_rows(db, category)
    was = _row(plan, "2026-10", category)
    assert was is None
    ok(plan.put("/api/budgets/2026-10", json={"assigned": {category: 30}}))
    now = _row(plan, "2026-10", category)
    assert now == {"assigned": 30.0, "removed": False, "restart": False, "moved": 0.0}
    after = ok(_restore(plan, "2026-10", (category, was, now)))
    assert after == before and _db_rows(db, category) == rows_before


def test_restore_rows_puts_back_an_existing_row(plan, db):
    ok(plan.put("/api/budgets/2026-10", json={"assigned": {"FOOD_AND_DRINK": 120}}))
    was = _row(plan, "2026-10", "FOOD_AND_DRINK")
    before = ok(plan.get("/api/budgets", params={"month": "2026-10"}))
    ok(plan.put("/api/budgets/2026-10", json={"assigned": {"FOOD_AND_DRINK": 200}}))
    now = _row(plan, "2026-10", "FOOD_AND_DRINK")
    assert ok(_restore(plan, "2026-10", ("FOOD_AND_DRINK", was, now))) == before
    # A removed row comes back with its flags.
    ok(plan.put("/api/budgets/2026-10", json={"removed": ["TRANSPORTATION"]}))
    removed = _row(plan, "2026-10", "TRANSPORTATION")
    assert removed == {"assigned": 0.0, "removed": True, "restart": False, "moved": 0.0}
    ok(_restore(plan, "2026-10", ("TRANSPORTATION", None, removed)))
    ok(_restore(plan, "2026-10", ("TRANSPORTATION", removed, None)))
    assert _db_rows(db, "TRANSPORTATION")[-1] == ("2026-10", 0, True, False)


def test_restore_rows_refuses_a_row_changed_since(plan, db):
    """``expected`` must be the row as it is now (an edit since, e.g. in another window, wins)."""
    ok(plan.put("/api/budgets/2026-10", json={"assigned": {"FOOD_AND_DRINK": 30}}))
    saved = _row(plan, "2026-10", "FOOD_AND_DRINK")
    ok(plan.put("/api/budgets/2026-10", json={"assigned": {"FOOD_AND_DRINK": 45}}))  # the newer edit
    r = _restore(plan, "2026-10", ("FOOD_AND_DRINK", None, saved))
    assert r.status_code == 409 and r.json()["detail"] == "This plan was changed since, so it wasn't undone."
    assert _db_rows(db, "FOOD_AND_DRINK")[-1] == ("2026-10", 4500, False, False)
    # expected null but there is a row now; expected a row but there is none.
    assert _restore(plan, "2026-10", ("FOOD_AND_DRINK", None, None)).status_code == 409
    assert _restore(plan, "2026-10", ("ENTERTAINMENT", None, ROW)).status_code == 409
    # Expected matches: null -> null is allowed (and changes nothing).
    ok(_restore(plan, "2026-10", ("ENTERTAINMENT", None, None)))
    assert _db_rows(db, "ENTERTAINMENT") == []


def test_restore_rows_is_all_or_nothing(plan, db):
    """Undo of "Move $X to savings": both categories go back together, or neither does."""
    ok(plan.put("/api/budgets/2026-09", json={"assigned": {"FOOD_AND_DRINK": 60, "PERSONAL_CARE": 40}}))
    food = _row(plan, "2026-09", "FOOD_AND_DRINK")
    care = _row(plan, "2026-09", "PERSONAL_CARE")
    was_food = {"assigned": 100.0, "removed": False, "restart": False}
    # One row changed since: nothing is written.
    r = _restore(plan, "2026-09", ("FOOD_AND_DRINK", was_food, food), ("PERSONAL_CARE", None, ROW))
    assert r.status_code == 409
    assert _db_rows(db, "FOOD_AND_DRINK") == [("2026-09", 6000, False, False)]
    assert _db_rows(db, "PERSONAL_CARE") == [("2026-09", 4000, False, False)]
    ok(_restore(plan, "2026-09", ("FOOD_AND_DRINK", was_food, food), ("PERSONAL_CARE", None, care)))
    assert _db_rows(db, "FOOD_AND_DRINK") == [("2026-09", 10000, False, False)]
    assert _db_rows(db, "PERSONAL_CARE") == []


def test_restore_rows_only_touches_that_month(plan, db):
    add_budget(db, "2026-11", {"FOOD_AND_DRINK": 40})
    ok(_restore(plan, "2026-10", ("FOOD_AND_DRINK", {**ROW, "assigned": 25}, None)))
    assert _db_rows(db, "FOOD_AND_DRINK") == [
        ("2026-09", 10000, False, False), ("2026-10", 2500, False, False), ("2026-11", 4000, False, False)]


def test_restore_rows_over_assign_rule(plan, db):
    ready = ok(plan.get("/api/budgets", params={"month": "2026-10"}))["ready_to_assign"]
    too_much = {"assigned": ready + 100, "removed": False, "restart": False}
    r = _restore(plan, "2026-10", ("FOOD_AND_DRINK", too_much, None))
    assert r.status_code == 422 and "ready to assign" in r.json()["detail"]
    assert _db_rows(db, "FOOD_AND_DRINK") == [("2026-09", 10000, False, False)]
    # Over-assigned already: a restore that raises Ready to Assign is allowed even though it
    # stays below zero.
    add_budget(db, "2026-10", {"FOOD_AND_DRINK": 5000, "TRANSPORTATION": 5000})
    after = ok(_restore(plan, "2026-10", ("FOOD_AND_DRINK", None, {**ROW, "assigned": 5000})))
    assert after["ready_to_assign"] < 0


def test_restore_rows_refusals(plan, db):
    # Past months are read-only, like every budget save; so is more than 12 months ahead.
    for month in ("2026-08", "2027-10"):
        assert _restore(plan, month, ("FOOD_AND_DRINK", ROW, None)).status_code == 422
    for category in ("NOPE", "LOAN_PAYMENTS", "INCOME", "X" * 65, ""):
        assert _restore(plan, "2026-10", (category, ROW, None)).status_code == 422, category
    one = {"category": "FOOD_AND_DRINK", "state": ROW, "expected": None}
    for body in ({}, {"rows": []}, {"rows": [one, one]}, {"rows": [one] * 11}, {"rows": [one], "extra": 1},
                 {"rows": [{**one, "state": {**ROW, "assigned": "10"}}]},
                 {"rows": [{**one, "state": {**ROW, "removed": "yes"}}]},
                 {"rows": [{"category": "FOOD_AND_DRINK", "state": ROW}]},  # expected is required
                 {"rows": [{**one, "state": {"assigned": 10, "removed": False}}]},
                 {"rows": [{**one, "state": {**ROW, "other": 1}}]},
                 {"rows": [{**one, "state": {**ROW, "assigned": 1e13}}]}):
        assert plan.put("/api/budgets/2026-10/rows", json=body).status_code == 422, body
    bad_json = plan.put("/api/budgets/2026-10/rows", content=b'{"rows": [{"category": "FOOD_AND_DRINK", '
                        b'"state": {"assigned": NaN, "removed": false, "restart": false}, "expected": null}]}',
                        headers={"Content-Type": "application/json"})
    assert bad_json.status_code == 422
    assert plan.get("/api/budgets/2026-1/rows/FOOD_AND_DRINK").status_code == 422
    assert _db_rows(db, "FOOD_AND_DRINK") == [("2026-09", 10000, False, False)]


def test_restore_rows_needs_a_session(client):
    assert client.get("/api/budgets/2026-10/rows/FOOD_AND_DRINK").status_code == 401
    assert client.put("/api/budgets/2026-10/rows", json={"rows": []}).status_code == 401


def test_monthly_cents_integer_math():
    assert reports.monthly_cents(500, "weekly") == 2167  # 21.666...
    assert reports.monthly_cents(1000, "biweekly") == 2167
    assert reports.monthly_cents(12000, "yearly") == 1000
    assert reports.monthly_cents(1000, "quarterly") == 333
    assert reports.monthly_cents(1500, "semimonthly") == 3000
    assert reports.monthly_cents(1799, "monthly") == 1799
    assert reports.monthly_cents(18, "yearly") == 2  # 1.5 rounds up

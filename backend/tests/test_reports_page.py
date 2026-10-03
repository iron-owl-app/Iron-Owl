"""Reports page redesign: bill flags, the category drill-down and the habits endpoint."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from app.models import Account, RecurringItem
from tests.conftest import add_txn, make_account


def ok(response) -> dict:
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def p(unlocked, db, fixed_today):  # today is 2026-09-26
    c = unlocked
    chk = make_account(db, "Checking", "bank", 2000)
    visa = make_account(db, "Visa", "credit", 300)
    ira = make_account(db, "IRA", "retirement", 5000)

    # Rent: the same recurring payment every month -> a bill.
    for month in (4, 5, 6, 7, 8, 9):
        add_txn(db, chk.id, f"2026-{month:02d}-01", -1200, "RENT PMT 0042", category="RENT_AND_UTILITIES")
    # One non-recurring utility charge keeps the recurring share at 7200 / 7300 (> 75%).
    add_txn(db, chk.id, "2026-07-15", -100, "Plumber", category="RENT_AND_UTILITIES")
    # Streaming is recurring but only a small share of Entertainment -> not a bill.
    add_txn(db, visa.id, "2026-09-02", -15, "Netflix", merchant="Netflix", category="ENTERTAINMENT")
    add_txn(db, visa.id, "2026-09-12", -85, "Concert", category="ENTERTAINMENT")

    # Day-to-day food in September.
    add_txn(db, visa.id, "2026-09-03", -6.5, "Starbucks 123", merchant="Starbucks", category="FOOD_AND_DRINK")
    add_txn(db, visa.id, "2026-09-05", -7.25, "Starbucks 123", merchant="Starbucks", category="FOOD_AND_DRINK")
    add_txn(db, visa.id, "2026-09-05", -120, "Costco", merchant="Costco", category="FOOD_AND_DRINK")
    add_txn(db, visa.id, "2026-09-06", 20, "Costco", merchant="Costco", category="FOOD_AND_DRINK")  # refund
    add_txn(db, visa.id, "2026-09-20", -14.99, "Taco stand", category="FOOD_AND_DRINK", pending=True)
    add_txn(db, visa.id, "2026-09-21", -15, "Bakery", category="FOOD_AND_DRINK")  # not under $15
    add_txn(db, chk.id, "2026-09-22", -40, "Venmo", category="FOOD_AND_DRINK", is_transfer=True)  # transfer
    add_txn(db, ira.id, "2026-09-23", -5, "Not spending", category="FOOD_AND_DRINK")  # retirement account
    add_txn(db, chk.id, "2026-09-27", -9, "Tomorrow", category="FOOD_AND_DRINK")  # after today
    add_txn(db, chk.id, "2026-08-10", -300, "Costco", merchant="Costco", category="FOOD_AND_DRINK")
    add_txn(db, chk.id, "2026-03-10", -50, "Old", category="FOOD_AND_DRINK")  # before the 6-month series
    add_txn(db, chk.id, "2026-09-15", -250, "Car loan", category="LOAN_PAYMENTS")

    # A split: $12 of food (small, and a Starbucks visit) and $30 of shopping.
    split = add_txn(db, visa.id, "2026-09-08", -42, "Starbucks 123", merchant="Starbucks", category="FOOD_AND_DRINK")
    r = c.put(f"/api/transactions/{split.id}/splits", json={"splits": [
        {"amount": -12, "category": "FOOD_AND_DRINK"}, {"amount": -30, "category": "GENERAL_MERCHANDISE"}]})
    assert r.status_code == 200, r.text

    db.add_all([
        RecurringItem(name="Rent", merchant_key="rent pmt", account_id=chk.id, amount_cents=-120000,
                      cadence="monthly", next_date=dt.date(2026, 10, 1), status="active", source="detected"),
        RecurringItem(name="Netflix", merchant_key="netflix", account_id=visa.id, amount_cents=-1500,
                      cadence="monthly", next_date=dt.date(2026, 10, 2), status="suggested", source="detected"),
        # Dismissed items never make a category a bill.
        RecurringItem(name="Costco", merchant_key="costco", account_id=visa.id, amount_cents=-12000,
                      cadence="monthly", next_date=dt.date(2026, 10, 5), status="dismissed", source="detected"),
    ])
    db.commit()
    return SimpleNamespace(client=c, split=split)


def test_bill_flags_and_fixed(p):
    rep = ok(p.client.get("/api/reports/monthly", params={"months": 6}))
    cats = {c["id"]: c for c in rep["categories"]}
    assert cats["RENT_AND_UTILITIES"]["bill"] is True
    assert cats["ENTERTAINMENT"]["bill"] is False
    assert cats["FOOD_AND_DRINK"]["bill"] is False  # its only recurring merchant is dismissed
    assert cats["LOAN_PAYMENTS"] == {**cats["LOAN_PAYMENTS"], "kind": "fixed", "bill": True}
    sep = rep["months"][-1]
    assert sep["month"] == "2026-09" and sep["by_fixed"] == {"LOAN_PAYMENTS": 250.0}
    assert sep["by_category"]["RENT_AND_UTILITIES"] == 1200.0


def test_bill_share_threshold(p, db):
    # Enough non-recurring utility spending pushes Rent and utilities under the 75% share.
    chk = make_account(db, "Other checking", "bank", 0)
    add_txn(db, chk.id, "2026-09-10", -2500, "Big repair", category="RENT_AND_UTILITIES")
    cats = {c["id"]: c for c in ok(p.client.get("/api/reports/monthly", params={"months": 6}))["categories"]}
    assert cats["RENT_AND_UTILITIES"]["bill"] is False


def test_category_detail(p):
    d = ok(p.client.get("/api/reports/category/FOOD_AND_DRINK", params={"start": "2026-09-01", "end": "2026-09-30"}))
    assert d["category"] == "FOOD_AND_DRINK"
    assert d["months"] == [
        {"month": "2026-04", "spent": 0.0}, {"month": "2026-05", "spent": 0.0}, {"month": "2026-06", "spent": 0.0},
        {"month": "2026-07", "spent": 0.0}, {"month": "2026-08", "spent": 300.0},
        # 6.5 + 7.25 + 120 - 20 + 14.99 + 15 + 12 (split part) + 9 (dated after today, still September)
        {"month": "2026-09", "spent": 164.74},
    ]
    assert d["merchants"][0] == {"name": "Costco", "spent": 100.0, "count": 2}
    starbucks = next(m for m in d["merchants"] if m["name"] == "Starbucks")
    assert starbucks == {"name": "Starbucks", "spent": 25.75, "count": 3}
    assert [(b["name"], b["amount"]) for b in d["biggest"]] == [
        ("Costco", 120.0), ("Bakery", 15.0), ("Taco stand", 14.99), ("Starbucks", 12.0), ("Tomorrow", 9.0)]
    assert d["biggest"][3]["id"] == p.split.id and d["biggest"][0]["date"] == "2026-09-05"

    # A longer range and a custom month count.
    q = ok(p.client.get("/api/reports/category/FOOD_AND_DRINK",
                        params={"start": "2026-07-01", "end": "2026-09-30", "months": 7}))
    assert q["months"][0] == {"month": "2026-03", "spent": 50.0}
    assert q["merchants"][0] == {"name": "Costco", "spent": 400.0, "count": 3}

    fixed = ok(p.client.get("/api/reports/category/LOAN_PAYMENTS", params={"start": "2026-09-01", "end": "2026-09-30"}))
    assert fixed["months"][-1] == {"month": "2026-09", "spent": 250.0}


def test_category_detail_validation(p):
    c = p.client
    base = {"start": "2026-09-01", "end": "2026-09-30"}
    assert c.get("/api/reports/category/NOPE", params=base).status_code == 404
    assert c.get("/api/reports/category/INCOME", params=base).status_code == 404
    assert c.get("/api/reports/category/TRANSFER_OUT", params=base).status_code == 404
    assert c.get("/api/reports/category/OTHER", params=base).status_code == 200
    for bad in ({"start": "2026-09-30", "end": "2026-09-01"}, {"start": "2000-01-01", "end": "2026-09-01"},
                {"start": "x", "end": "2026-09-01"}, {"end": "2026-09-01"}, {**base, "months": 0},
                {**base, "months": 25}):
        assert c.get("/api/reports/category/FOOD_AND_DRINK", params=bad).status_code == 422, bad


def test_habits(p):
    h = ok(p.client.get("/api/reports/habits", params={"month": "2026-09"}))
    assert h["month"] == "2026-09" and h["through"] == "2026-09-26"
    days = {d["date"]: d["spent"] for d in h["days"]}
    assert len(days) == 26 and "2026-09-27" not in days
    # Rent (a bill) on the 1st and the car loan (fixed) on the 15th are left out.
    assert days["2026-09-01"] == 0.0 and days["2026-09-15"] == 0.0
    assert days["2026-09-02"] == 15.0  # Netflix: Entertainment isn't a bill
    assert days["2026-09-05"] == 127.25
    assert days["2026-09-06"] == 0.0  # a refund day never goes negative
    assert days["2026-09-08"] == 42.0  # both split parts are day-to-day
    assert days["2026-09-22"] == 0.0 and days["2026-09-23"] == 0.0  # transfer, retirement account

    small = h["small"]
    assert small["under"] == 15.0
    # Starbucks 6.50 + 7.25 (the $42 split isn't small), Taco stand 14.99 (pending counts), Netflix 15 isn't under.
    assert small["merchants"] == [
        {"name": "Taco stand", "count": 1, "total": 14.99},
        {"name": "Starbucks", "count": 2, "total": 13.75},
    ]
    assert (small["count"], small["total"]) == (3, 28.74)

    # Under $50 adds the $42 split visit, the Bakery and Netflix.
    wider = ok(p.client.get("/api/reports/habits", params={"month": "2026-09", "under": 50}))["small"]
    assert wider["count"] == 6 and wider["merchants"][0]["name"] == "Starbucks"


def test_habits_edges(p):
    c = p.client
    past = ok(c.get("/api/reports/habits", params={"month": "2026-08"}))
    assert past["through"] == "2026-08-31" and len(past["days"]) == 31
    future = ok(c.get("/api/reports/habits", params={"month": "2026-10"}))
    assert future["through"] is None and future["days"] == [] and future["small"]["count"] == 0
    for bad in ({"month": "2026-13"}, {"month": "2026-9"}, {"month": "0000-01"}, {"month": "1800-01"}, {},
                {"month": "2026-09", "under": 0}, {"month": "2026-09", "under": 1001}):
        assert c.get("/api/reports/habits", params=bad).status_code == 422, bad


def test_reports_need_unlock(client):
    for url in ("/api/reports/category/FOOD_AND_DRINK?start=2026-09-01&end=2026-09-30",
                "/api/reports/habits?month=2026-09"):
        assert client.get(url).status_code in (401, 423), url


def test_bill_needs_matching_account_and_amount(p, db):
    # A $2.99 subscription on the card plus a one-off $1,299 purchase at the same merchant on
    # checking: only the subscription is a recurring payment, so Shopping isn't a bill.
    visa = db.get(Account, 2)
    chk = db.get(Account, 1)
    for month in (7, 8, 9):
        add_txn(db, visa.id, f"2026-{month:02d}-03", -2.99, "APPLE.COM/BILL", merchant="Apple",
                category="GENERAL_MERCHANDISE")
    add_txn(db, chk.id, "2026-09-10", -1299, "Apple Store", merchant="Apple", category="GENERAL_MERCHANDISE")
    db.add(RecurringItem(name="Apple", merchant_key="apple", account_id=visa.id, amount_cents=-299,
                         cadence="monthly", next_date=dt.date(2026, 10, 3), status="suggested", source="detected"))
    db.commit()
    cats = {c["id"]: c for c in ok(p.client.get("/api/reports/monthly", params={"months": 6}))["categories"]}
    assert cats["GENERAL_MERCHANDISE"]["bill"] is False
    days = {d["date"]: d["spent"] for d in ok(p.client.get("/api/reports/habits", params={"month": "2026-09"}))["days"]}
    assert days["2026-09-10"] == 1299.0


def test_biggest_is_per_transaction_and_hidden_accounts_stay_out(p, db):
    hidden = make_account(db, "Old card", "credit", 0, hidden=True)
    loan = make_account(db, "Car loan", "loan", 9000)
    add_txn(db, hidden.id, "2026-09-09", -500, "Hidden", category="FOOD_AND_DRINK")
    add_txn(db, loan.id, "2026-09-09", -400, "Loan side", category="FOOD_AND_DRINK")
    visa = db.get(Account, 2)
    t = add_txn(db, visa.id, "2026-09-11", -90, "Market", category="FOOD_AND_DRINK")
    r = p.client.put(f"/api/transactions/{t.id}/splits", json={"splits": [
        {"amount": -60, "category": "FOOD_AND_DRINK"}, {"amount": -30, "category": "FOOD_AND_DRINK"}]})
    assert r.status_code == 200, r.text
    d = ok(p.client.get("/api/reports/category/FOOD_AND_DRINK", params={"start": "2026-09-01", "end": "2026-09-30"}))
    assert [(b["name"], b["amount"]) for b in d["biggest"]][:2] == [("Costco", 120.0), ("Market", 90.0)]
    assert len({b["id"] for b in d["biggest"]}) == len(d["biggest"])
    assert d["months"][-1]["spent"] == 254.74  # 164.74 + 90; hidden and loan accounts left out
    days = {x["date"]: x["spent"] for x in ok(p.client.get("/api/reports/habits", params={"month": "2026-09"}))["days"]}
    assert days["2026-09-09"] == 0.0


def test_strict_params(p):
    c = p.client
    assert c.get("/api/reports/habits", params={"month": "２０２６-09"}).status_code == 422
    assert c.get("/api/reports/habits", params={"month": "2026-09", "under": 0.004}).status_code == 422
    assert c.get("/api/reports/category/" + "X" * 65, params={"start": "2026-09-01", "end": "2026-09-30"}).status_code == 422

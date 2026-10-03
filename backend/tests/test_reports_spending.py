"""Release 3.10: the Spending tab, GET /api/reports/spending (SPEC "Accounts, Reports tabs and
debt plan (Release 3.10)"). Hand-computed totals; today is 2026-09-26."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.conftest import add_budget, add_txn, make_account
from tests.test_r36_calendar import make_item

URL = "/api/reports/spending"


def ok(response) -> dict:
    assert response.status_code == 200, response.text
    return response.json()


def page(client, month=None) -> dict:
    return ok(client.get(URL, params={"month": month} if month else None))


def cat(p: dict, category: str) -> dict | None:
    return next((c for c in p["categories"] if c["id"] == category), None)


@pytest.fixture
def s(unlocked, db, fixed_today):
    c = unlocked
    chk = make_account(db, "Checking", "bank", 2000)
    visa = make_account(db, "Visa", "credit", 300)
    hsa = make_account(db, "HSA", "hsa", 800)
    ira = make_account(db, "IRA", "retirement", 5000)
    hidden = make_account(db, "Old", "bank", 1, hidden=True)

    add_txn(db, hidden.id, "2026-04-02", -999, "Hidden")  # hidden: not even the first data day
    add_txn(db, chk.id, "2026-05-10", -40, "First")  # history starts mid-May: May is partial
    add_txn(db, chk.id, "2026-06-05", -300, "Green Basket")
    add_txn(db, chk.id, "2026-07-05", -200, "Green Basket")
    add_txn(db, chk.id, "2026-07-15", -500, "Car loan", category="LOAN_PAYMENTS")
    add_txn(db, chk.id, "2026-08-05", -250, "Green Basket")
    add_txn(db, chk.id, "2026-08-15", -500, "Car loan", category="LOAN_PAYMENTS")

    add_txn(db, visa.id, "2026-09-03", -100, "Grocer")
    add_txn(db, visa.id, "2026-09-04", 20, "Grocer")  # a refund nets out
    costco = add_txn(db, chk.id, "2026-09-05", -300, "Costco", category="GENERAL_MERCHANDISE")
    add_txn(db, hsa.id, "2026-09-06", -50, "Pharmacy", category="MEDICAL")
    add_txn(db, ira.id, "2026-09-07", -999, "Not spending")  # retirement: out
    add_txn(db, hidden.id, "2026-09-08", -777, "Hidden")  # hidden: out
    add_txn(db, chk.id, "2026-09-09", -1000, "To savings", category="TRANSFER_OUT")  # transfer kind: out
    add_txn(db, chk.id, "2026-09-10", -40, "Venmo", is_transfer=True)  # a transfer: out
    add_txn(db, chk.id, "2026-09-11", -30, "Corner store", pending=True)  # pending counts
    add_txn(db, chk.id, "2026-09-12", 3000, "Payroll", category="INCOME")  # income: out
    add_txn(db, chk.id, "2026-09-15", -500, "Car loan", category="LOAN_PAYMENTS")  # fixed counts

    assert c.put(f"/api/transactions/{costco.id}/splits", json={"splits": [
        {"amount": -120, "category": "FOOD_AND_DRINK"}, {"amount": -180, "category": "GENERAL_MERCHANDISE"},
    ]}).status_code == 200
    add_budget(db, "2026-09", {"FOOD_AND_DRINK": 400})
    return SimpleNamespace(client=c, db=db, chk=chk, visa=visa, costco=costco)


def test_this_month(s):
    p = page(s.client)
    assert (p["today"], p["current"], p["month"], p["first_data_date"]) == ("2026-09-26", "2026-09", "2026-09", "2026-05-10")
    assert p["range"] == {"first": "2026-05", "last": "2026-09"}
    assert p["months"] == [
        {"month": "2026-04", "total": 0.0, "complete": True, "has_data": False, "partial": False},
        {"month": "2026-05", "total": 40.0, "complete": True, "has_data": True, "partial": True},
        {"month": "2026-06", "total": 300.0, "complete": True, "has_data": True, "partial": False},
        {"month": "2026-07", "total": 700.0, "complete": True, "has_data": True, "partial": False},
        {"month": "2026-08", "total": 750.0, "complete": True, "has_data": True, "partial": False},
        # Food 100 - 20 + 120 (Costco's part) + 30 pending; shopping 180; medical 50; loan 500.
        {"month": "2026-09", "total": 960.0, "complete": False, "has_data": True, "partial": False},
    ]
    # Whole months only: not September (not over), not May (started mid-month).
    assert p["average"] == {"amount": 583.33, "months": ["2026-06", "2026-07", "2026-08"]}
    assert p["summary"] == {"month": "2026-09", "total": 960.0, "complete": False, "partial": False,
                            "previous": {"month": "2026-08", "total": 750.0, "has_data": True, "partial": False}}
    assert [c["id"] for c in p["categories"]] == ["LOAN_PAYMENTS", "FOOD_AND_DRINK", "GENERAL_MERCHANDISE", "MEDICAL"]
    assert round(sum(c["amount"] for c in p["categories"]), 2) == p["summary"]["total"]
    loan, food, shop, med = p["categories"]
    assert {k: loan[k] for k in ("name", "kind", "bill", "in_budget", "amount", "previous", "share")} == {
        "name": "Loan payments", "kind": "fixed", "bill": True, "in_budget": False, "amount": 500.0,
        "previous": 500.0, "share": 52}
    assert (food["hue"], food["bill"], food["in_budget"], food["amount"], food["previous"], food["share"]) == (
        25, False, True, 230.0, 250.0, 24)
    assert (shop["in_budget"], shop["previous"], shop["share"], med["share"]) == (False, 0.0, 19, 5)
    # Purchases newest first, refunds in (so they add up), split parts marked.
    assert food["purchases"] == [
        {"id": food["purchases"][0]["id"], "split": False, "date": "2026-09-11", "name": "Corner store",
         "amount": 30.0, "pending": True},
        {"id": s.costco.id, "split": True, "date": "2026-09-05", "name": "Costco", "amount": 120.0, "pending": False},
        {"id": food["purchases"][2]["id"], "split": False, "date": "2026-09-04", "name": "Grocer",
         "amount": -20.0, "pending": False},
        {"id": food["purchases"][3]["id"], "split": False, "date": "2026-09-03", "name": "Grocer",
         "amount": 100.0, "pending": False},
    ]
    assert round(sum(x["amount"] for x in food["purchases"]), 2) == food["amount"]
    assert (food["purchase_count"], food["more"]) == (4, 0)
    # Stores: bills left out (Loan payments); Costco's two parts are one visit, in the category
    # it spent most in; the refund isn't a purchase.
    assert p["stores"] == [
        {"name": "Costco", "amount": 300.0, "count": 1,
         "category": {"id": "GENERAL_MERCHANDISE", "name": "Shopping", "hue": 300}},
        {"name": "Grocer", "amount": 80.0, "count": 1,
         "category": {"id": "FOOD_AND_DRINK", "name": "Food and drink", "hue": 25}},
        {"name": "Pharmacy", "amount": 50.0, "count": 1, "category": {"id": "MEDICAL", "name": "Medical", "hue": 160}},
        {"name": "Corner store", "amount": 30.0, "count": 1,
         "category": {"id": "FOOD_AND_DRINK", "name": "Food and drink", "hue": 25}},
    ]
    assert p["stores_left_out"] == ["Loan payments"]


def test_other_months_and_the_month_before(s):
    july = page(s.client, "2026-07")
    assert july["summary"] == {"month": "2026-07", "total": 700.0, "complete": True, "partial": False,
                               "previous": {"month": "2026-06", "total": 300.0, "has_data": True, "partial": False}}
    assert cat(july, "FOOD_AND_DRINK")["previous"] == 300.0
    may = page(s.client, "2026-05")
    # The first month: partial, and nothing before it.
    assert may["summary"]["partial"] is True and may["summary"]["previous"] is None
    assert cat(may, "FOOD_AND_DRINK")["previous"] is None
    june = page(s.client, "2026-06")
    assert june["summary"]["previous"] == {"month": "2026-05", "total": 40.0, "has_data": True, "partial": True}
    # The months and the average don't depend on the month picked.
    assert june["months"] == page(s.client)["months"] and june["average"] == may["average"]


def test_month_before_the_window(unlocked, db, fixed_today):
    chk = make_account(db, "Checking", "bank", 100)
    add_txn(db, chk.id, "2026-01-02", -10, "Old")
    add_txn(db, chk.id, "2026-03-05", -60, "March")
    add_txn(db, chk.id, "2026-04-05", -70, "April")
    p = page(unlocked, "2026-04")
    assert p["range"] == {"first": "2026-04", "last": "2026-09"}
    # March is outside the 6-month window but still the month before.
    assert p["summary"]["previous"] == {"month": "2026-03", "total": 60.0, "has_data": True, "partial": False}
    assert p["months"][0] == {"month": "2026-04", "total": 70.0, "complete": True, "has_data": True, "partial": False}
    assert unlocked.get(URL, params={"month": "2026-03"}).status_code == 422


def test_bad_months(s):
    for bad in ("2026-04", "2026-10", "2027-01", "2026-13", "abc", "2026-9", "0000-01", "2026-09-01"):
        r = s.client.get(URL, params={"month": bad})
        assert r.status_code == 422, bad
        assert "abc" not in r.text


def test_needs_a_session(unlocked, app, fixed_today):
    from fastapi.testclient import TestClient

    from tests.conftest import BASE_URL
    anon = TestClient(app, base_url=BASE_URL)
    assert anon.get(URL).status_code == 401


def test_empty_vault(unlocked, fixed_today):
    p = page(unlocked)
    assert p["range"] == {"first": "2026-09", "last": "2026-09"} and p["first_data_date"] is None
    assert not any(m["has_data"] for m in p["months"]) and p["average"] is None
    assert p["summary"] == {"month": "2026-09", "total": 0.0, "complete": False, "partial": False, "previous": None}
    assert (p["categories"], p["stores"], p["stores_left_out"]) == ([], [], [])
    assert unlocked.get(URL, params={"month": "2026-08"}).status_code == 422


def test_recurring_bills_leave_the_stores(unlocked, db, fixed_today):
    chk = make_account(db, "Checking", "bank", 1000)
    make_item(db, "Rent Co", -1200, "monthly", "2026-10-01", account=chk, source="manual")
    make_item(db, "StreamFlix", -15, "monthly", "2026-10-03", account=chk, source="manual")
    for month in ("07", "08", "09"):
        add_txn(db, chk.id, f"2026-{month}-01", -1200, "Rent Co", category="RENT_AND_UTILITIES")
    # StreamFlix is a Recurring payment in a category that isn't a bill (mostly other spending).
    add_txn(db, chk.id, "2026-09-03", -15, "StreamFlix", category="ENTERTAINMENT")
    add_txn(db, chk.id, "2026-09-04", -200, "Concert hall", category="ENTERTAINMENT")
    p = page(unlocked)
    rent, ent = cat(p, "RENT_AND_UTILITIES"), cat(p, "ENTERTAINMENT")
    assert (rent["bill"], ent["bill"]) == (True, False)
    assert [x["name"] for x in p["stores"]] == ["Concert hall"]
    assert p["stores_left_out"] == ["Rent and utilities"]
    assert ent["amount"] == 215.0 and ent["purchase_count"] == 2  # the category still has it


def test_caps(unlocked, db, fixed_today):
    chk = make_account(db, "Checking", "bank", 1000)
    for i in range(105):
        add_txn(db, chk.id, f"2026-09-{1 + i % 20:02d}", -1, ["Alder", "Birch", "Cedar", "Dogwood", "Elm", "Fir", "Gum"][i % 7])
    food = cat(page(unlocked), "FOOD_AND_DRINK")
    assert (len(food["purchases"]), food["purchase_count"], food["more"], food["amount"]) == (100, 105, 5, 105.0)
    dates = [x["date"] for x in food["purchases"]]
    assert dates == sorted(dates, reverse=True)
    stores = page(unlocked)["stores"]
    assert len(stores) == 5 and stores[0]["count"] == 15

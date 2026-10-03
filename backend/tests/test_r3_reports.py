"""Release 3: monthly reports (split-aware, hand-computed totals)."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from app.models import BalanceSnapshot
from tests.conftest import add_txn, make_account


def ok(response) -> dict:
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def r(unlocked, db, fixed_today):
    c = unlocked
    chk = make_account(db, "Checking", "bank", 2000)
    visa = make_account(db, "Visa", "credit", 300)
    hsa = make_account(db, "HSA", "hsa", 800)
    ira = make_account(db, "IRA", "retirement", 5000)
    hidden = make_account(db, "Old", "bank", 1, hidden=True)

    add_txn(db, chk.id, "2025-12-20", -10, "Grocer", category="FOOD_AND_DRINK")
    add_txn(db, chk.id, "2026-08-05", 3000, "Paycheck", category="INCOME")
    grocer_aug = add_txn(db, chk.id, "2026-08-10", -200, "Grocer", category="FOOD_AND_DRINK")
    add_txn(db, chk.id, "2026-08-15", -500, "Car loan", category="LOAN_PAYMENTS")
    add_txn(db, chk.id, "2026-09-01", 3000, "Paycheck", category="INCOME")
    add_txn(db, visa.id, "2026-09-03", -100, "Grocer", category="FOOD_AND_DRINK")
    add_txn(db, visa.id, "2026-09-04", 20, "Grocer", category="FOOD_AND_DRINK")  # refund
    costco = add_txn(db, chk.id, "2026-09-05", -300, "Costco run", merchant="Costco", category="GENERAL_MERCHANDISE")
    add_txn(db, hsa.id, "2026-09-06", -50, "Pharmacy", category="MEDICAL")
    add_txn(db, ira.id, "2026-09-07", -999, "Not spending", category="FOOD_AND_DRINK")  # retirement: excluded
    add_txn(db, hidden.id, "2026-09-08", -777, "Hidden", category="FOOD_AND_DRINK")  # hidden: excluded
    add_txn(db, chk.id, "2026-09-09", -1000, "To savings", category="TRANSFER_OUT")
    add_txn(db, chk.id, "2026-09-10", -40, "Venmo", category="FOOD_AND_DRINK", is_transfer=True)
    add_txn(db, chk.id, "2026-09-11", 100, "Interest", category="OTHER", pending=True)  # a refund-like credit

    r_ = c.put(f"/api/transactions/{costco.id}/splits", json={"splits": [
        {"amount": -120, "category": "FOOD_AND_DRINK"}, {"amount": -180, "category": "GENERAL_MERCHANDISE"}]})
    assert r_.status_code == 200
    food = c.post("/api/category-groups", json={"name": "Food"}).json()
    c.patch("/api/categories/FOOD_AND_DRINK", json={"group_id": food["id"]})
    groceries = c.post("/api/tags", json={"name": "Groceries"}).json()
    for t in (grocer_aug, costco):
        assert c.put(f"/api/transactions/{t.id}/tags", json={"tag_ids": [groceries["id"]]}).status_code == 200

    for account, day, cents in ((chk, "2025-12-31", 100000), (chk, "2026-06-01", 200000),
                                (visa, "2026-03-01", 30000), (ira, "2026-02-01", 500000), (hidden, "2025-01-01", 99)):
        db.add(BalanceSnapshot(account_id=account.id, date=dt.date.fromisoformat(day), balance_cents=cents))
    db.commit()
    return SimpleNamespace(client=c, food=food, groceries=groceries)


def test_monthly_report(r):
    rep = ok(r.client.get("/api/reports/monthly", params={"months": 2}))
    gid = str(r.food["id"])
    assert rep["months"] == [
        {"month": "2026-08", "income": 3000.0, "spending": 200.0, "fixed": 500.0, "net": 2300.0,
         "savings_rate": 76.7, "by_category": {"FOOD_AND_DRINK": 200.0}, "by_group": {gid: 200.0},
         "by_fixed": {"LOAN_PAYMENTS": 500.0}, "planned": {}},
        # Food: -100 + 20 refund - 120 (Costco's food part) = 200; the pending +100 credit is
        # "Other" (spending) so it nets Other to 0; shopping 180 (part); medical 50 (HSA).
        {"month": "2026-09", "income": 3000.0, "spending": 430.0, "fixed": 0.0, "net": 2570.0,
         "savings_rate": 85.7,
         "by_category": {"FOOD_AND_DRINK": 200.0, "GENERAL_MERCHANDISE": 180.0, "MEDICAL": 50.0},
         "by_group": {gid: 200.0, "none": 230.0}, "by_fixed": {}, "planned": {}},
    ]
    assert rep["groups"] == [{"id": r.food["id"], "name": "Food"}]
    cats = {c["id"]: c for c in rep["categories"]}
    assert cats["FOOD_AND_DRINK"] == {"id": "FOOD_AND_DRINK", "name": "Food and drink", "hue": 25, "kind": "spending",
                                      "group_id": r.food["id"], "bill": False}
    assert "INCOME" not in cats and cats["MEDICAL"]["group_id"] is None
    # Fixed categories come last and are always bills.
    assert rep["categories"][-1]["id"] == "LOAN_PAYMENTS" and cats["LOAN_PAYMENTS"]["bill"] is True

    default = ok(r.client.get("/api/reports/monthly"))
    assert [m["month"] for m in default["months"]] == [f"2025-{m:02d}" for m in range(10, 13)] + [
        f"2026-{m:02d}" for m in range(1, 10)]
    assert default["months"][2] == {"month": "2025-12", "income": 0.0, "spending": 10.0, "fixed": 0.0, "net": -10.0,
                                    "savings_rate": None, "by_category": {"FOOD_AND_DRINK": 10.0},
                                    "by_group": {str(r.food["id"]): 10.0}, "by_fixed": {}, "planned": {}}
    assert len(ok(r.client.get("/api/reports/monthly", params={"months": 60}))["months"]) == 60
    for bad in (0, 61, "x"):
        assert r.client.get("/api/reports/monthly", params={"months": bad}).status_code == 422

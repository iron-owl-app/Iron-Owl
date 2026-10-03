"""Budget panel activity must explain exactly the category's envelope spending."""
from __future__ import annotations

import pytest

from app.models import TransactionSplit
from app.seeds import OTHER
from tests.conftest import add_budget, add_txn, make_account


MONTH = "2026-09"
FOOD = "FOOD_AND_DRINK"
PATH = f"/api/budgets/{MONTH}/categories/{FOOD}/transactions"


def ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def category_spent(client, category=FOOD):
    month = ok(client.get("/api/budgets", params={"month": MONTH}))
    return next(line["spent"] for line in month["categories"] if line["category"] == category)


def test_activity_matches_envelope_pending_refunds_splits_accounts(unlocked, db, fixed_today):
    bank = make_account(db, "Checking", "bank", 2000)
    card = make_account(db, "Card", "credit", 0)
    other = make_account(db, "Cash", "other", 100)
    hidden = make_account(db, "Hidden", "bank", 0, hidden=True)
    hsa = make_account(db, "HSA", "hsa", 0)
    add_budget(db, MONTH, {FOOD: 500})
    purchase = add_txn(db, bank.id, "2026-09-01", -100, "Whole purchase")
    pending = add_txn(db, card.id, "2026-09-02", -35.12, "Pending purchase", pending=True)
    refund = add_txn(db, bank.id, "2026-09-03", 15.01, "Refund")
    split = add_txn(db, bank.id, "2026-09-04", -90, "Split merchant", merchant="Shop")
    db.add_all([
        TransactionSplit(transaction_id=split.id, category=FOOD, amount_cents=-3001),
        TransactionSplit(transaction_id=split.id, category=FOOD, amount_cents=-1999),
        TransactionSplit(transaction_id=split.id, category="TRANSPORTATION", amount_cents=-4000),
    ])
    db.commit()
    for account in (other, hidden, hsa):
        add_txn(db, account.id, "2026-09-05", -200, "Outside budget")
    add_txn(db, bank.id, "2026-09-06", -50, "Transfer", is_transfer=True)
    add_txn(db, bank.id, "2026-08-31", -75, "Other month")
    parent_food = add_txn(db, bank.id, "2026-09-07", -10, "Parent no longer counts")
    db.add(TransactionSplit(transaction_id=parent_food.id, category="TRANSPORTATION", amount_cents=-1000))
    db.commit()
    data = ok(unlocked.get(PATH))
    assert data["total"] == 4 and data["next_offset"] is None
    assert data["spent"] == category_spent(unlocked) == 170.11
    assert sum(round(item["amount"] * 100) for item in data["items"]) == -17011
    by_id = {item["id"]: item for item in data["items"]}
    assert set(by_id) == {purchase.id, pending.id, refund.id, split.id}
    assert by_id[split.id]["amount"] == -50 and by_id[split.id]["split"] is True
    assert by_id[split.id]["name"] == "Shop"
    assert by_id[pending.id]["pending"] is True
    assert by_id[refund.id]["amount"] == 15.01
    # Excluding a formerly included account must update contributions as well as totals.
    ok(unlocked.put("/api/budgets/accounts", json={"account_ids": [bank.id, other.id]}))
    revised = ok(unlocked.get(PATH))
    assert revised["spent"] == category_spent(unlocked) == 334.99
    assert pending.id not in {item["id"] for item in revised["items"]}


def test_activity_other_normalization_and_over_refund_floor(unlocked, db, fixed_today):
    account = make_account(db, "Checking", "bank", 1000)
    add_budget(db, MONTH, {OTHER: 50})
    add_txn(db, account.id, "2026-09-01", -20, "Unknown", category="REMOVED_CATEGORY")
    add_txn(db, account.id, "2026-09-02", -10, "No category", category=None)
    add_txn(db, account.id, "2026-09-03", 40, "Large refund", category=OTHER)
    data = ok(unlocked.get(f"/api/budgets/{MONTH}/categories/{OTHER}/transactions"))
    assert data["total"] == 3
    assert data["spent"] == category_spent(unlocked, OTHER) == 0
    assert sum(item["amount"] for item in data["items"]) == 10


def test_activity_pagination_orders_equal_dates_without_parent_duplicates(unlocked, db, fixed_today):
    account = make_account(db, "Checking", "bank", 1000)
    add_budget(db, MONTH, {FOOD: 100})
    transactions = [add_txn(db, account.id, "2026-09-04", -index, f"Purchase {index}") for index in range(1, 5)]
    first = ok(unlocked.get(PATH, params={"limit": 2}))
    second = ok(unlocked.get(PATH, params={"limit": 2, "offset": first["next_offset"]}))
    assert first["spent"] == second["spent"] == 10
    assert first["total"] == second["total"] == 4
    assert first["next_offset"] == 2 and second["next_offset"] is None
    assert [item["id"] for item in first["items"] + second["items"]] == [item.id for item in reversed(transactions)]
    assert ok(unlocked.get(PATH, params={"offset": 10}))["items"] == []


def test_activity_includes_month_ends_and_nets_same_category_split_parts(unlocked, db, fixed_today):
    account = make_account(db, "Checking", "bank", 1000)
    add_budget(db, MONTH, {FOOD: 100, OTHER: 100})
    first = add_txn(db, account.id, "2026-09-01", -10, "First day")
    last = add_txn(db, account.id, "2026-09-30", -20, "Last day")
    add_txn(db, account.id, "2026-08-31", -99, "Before month")
    add_txn(db, account.id, "2026-10-01", -99, "After month")
    split = add_txn(db, account.id, "2026-09-15", -30, "Mixed split")
    db.add_all([
        TransactionSplit(transaction_id=split.id, category=OTHER, amount_cents=-2000),
        TransactionSplit(transaction_id=split.id, category=OTHER, amount_cents=500),
        TransactionSplit(transaction_id=split.id, category="TRANSPORTATION", amount_cents=-1500),
    ])
    db.commit()
    food = ok(unlocked.get(PATH))
    assert {item["id"] for item in food["items"]} == {first.id, last.id}
    assert food["spent"] == category_spent(unlocked) == 30
    other = ok(unlocked.get(f"/api/budgets/{MONTH}/categories/{OTHER}/transactions"))
    assert other["spent"] == category_spent(unlocked, OTHER) == 15
    assert other["total"] == 1 and other["items"][0]["amount"] == -15
    assert other["items"][0]["split"] is True


def test_activity_empty_month_and_no_selected_accounts(unlocked, db, fixed_today):
    account = make_account(db, "Checking", "bank", 1000)
    add_txn(db, account.id, "2026-09-01", -20)
    assert ok(unlocked.get("/api/budgets/2026-10/categories/FOOD_AND_DRINK/transactions")) == {"items": [], "total": 0, "next_offset": None, "spent": 0}
    ok(unlocked.put("/api/budgets/accounts", json={"account_ids": []}))
    assert ok(unlocked.get(PATH)) == {"items": [], "total": 0, "next_offset": None, "spent": 0}


@pytest.mark.parametrize("path,params", [
    ("/api/budgets/2026-13/categories/FOOD_AND_DRINK/transactions", {}),
    ("/api/budgets/1899-12/categories/FOOD_AND_DRINK/transactions", {}),
    ("/api/budgets/2026-09/categories/does-not-exist/transactions", {}),
    ("/api/budgets/2026-09/categories/INCOME/transactions", {}),
    (PATH, {"limit": 0}), (PATH, {"limit": 101}),
    (PATH, {"offset": -1}), (PATH, {"offset": 1000001}),
    (PATH, {"limit": "NaN"}),
    ("/api/budgets/2026-09/categories/x%27%20OR%201%3D1/transactions", {}),
])
def test_activity_rejects_invalid_lookup_and_paging(unlocked, path, params):
    assert unlocked.get(path, params=params).status_code == 422


def test_activity_requires_unlocked_session(client):
    assert client.get(PATH).status_code == 401

from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from app.models import Account, BalanceSnapshot
from app.utils import today


def snapshots(vault, account_id: int) -> list[tuple[str, int]]:
    s = vault.db.acquire()
    try:
        rows = s.execute(
            select(BalanceSnapshot.date, BalanceSnapshot.balance_cents)
            .where(BalanceSnapshot.account_id == account_id)
            .order_by(BalanceSnapshot.date)
        ).all()
        return [(d.isoformat(), c) for d, c in rows]
    finally:
        vault.db.release(s)


def add_snapshot(vault, account_id: int, days_ago: int, dollars: float) -> None:
    s = vault.db.acquire()
    try:
        s.add(BalanceSnapshot(account_id=account_id, date=today() - dt.timedelta(days=days_ago),
                              balance_cents=round(dollars * 100)))
        s.commit()
    finally:
        vault.db.release(s)


def add_plaid_account(vault) -> int:
    s = vault.db.acquire()
    try:
        a = Account(source="plaid", plaid_account_id="pa_1", name="Linked", category="bank",
                    current_balance_cents=100, hidden=False)
        s.add(a)
        s.commit()
        return a.id
    finally:
        vault.db.release(s)


def create(client, **body) -> dict:
    r = client.post("/api/accounts", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_manual_account_crud(unlocked, vault):
    acct = create(unlocked, name=" Emergency fund ", category="bank", current_balance=1500.10,
                  institution_name="Credit Union", notes="rainy day")
    assert acct["source"] == "manual" and acct["item_id"] is None
    assert acct["name"] == "Emergency fund"
    assert acct["current_balance"] == 1500.10
    assert acct["is_liability"] is False and acct["hidden"] is False
    assert acct["updated_at"].endswith("Z")
    assert snapshots(vault, acct["id"]) == [(today().isoformat(), 150010)]

    loan = create(unlocked, name="Car loan", category="loan", current_balance=9000,
                  interest_rate=6.5, minimum_payment=250.5, next_payment_due="2026-10-15")
    assert loan["is_liability"] is True
    assert loan["minimum_payment"] == 250.5 and loan["next_payment_due"] == "2026-10-15"

    listed = unlocked.get("/api/accounts").json()
    assert [a["name"] for a in listed] == ["Emergency fund", "Car loan"]  # category order

    r = unlocked.patch(f"/api/accounts/{acct['id']}", json={"current_balance": 1600, "hidden": True,
                                                            "notes": None})
    assert r.status_code == 200
    body = r.json()
    assert body["current_balance"] == 1600 and body["hidden"] is True and body["notes"] is None
    # Same-day change upserts the snapshot rather than adding one.
    assert snapshots(vault, acct["id"]) == [(today().isoformat(), 160000)]

    assert unlocked.patch(f"/api/accounts/{acct['id']}", json={"name": None}).status_code == 422
    assert unlocked.patch(f"/api/accounts/{acct['id']}", json={"source": "plaid"}).status_code == 422
    assert unlocked.patch(f"/api/accounts/{acct['id']}", json={"category": "bogus"}).status_code == 422
    assert unlocked.patch("/api/accounts/999", json={"hidden": False}).status_code == 404

    assert unlocked.delete(f"/api/accounts/{acct['id']}").status_code == 200
    assert unlocked.delete(f"/api/accounts/{acct['id']}").status_code == 404
    assert snapshots(vault, acct["id"]) == []
    assert [a["name"] for a in unlocked.get("/api/accounts").json()] == ["Car loan"]


def test_create_validation(unlocked):
    assert unlocked.post("/api/accounts", json={"name": "x", "category": "bank"}).status_code == 422
    assert unlocked.post("/api/accounts", json={"name": "  ", "category": "bank",
                                                "current_balance": 1}).status_code == 422
    assert unlocked.post("/api/accounts", json={"name": "x", "category": "gold",
                                                "current_balance": 1}).status_code == 422


def test_plaid_account_restrictions(unlocked, vault):
    account_id = add_plaid_account(vault)
    r = unlocked.patch(f"/api/accounts/{account_id}", json={"current_balance": 5})
    assert r.status_code == 400
    assert unlocked.delete(f"/api/accounts/{account_id}").status_code == 400
    r = unlocked.patch(f"/api/accounts/{account_id}", json={"name": "Renamed", "hidden": True})
    assert r.status_code == 200 and r.json()["name"] == "Renamed" and r.json()["current_balance"] == 1


def test_account_history(unlocked, vault):
    acct = create(unlocked, name="Savings", category="bank", current_balance=300)
    add_snapshot(vault, acct["id"], 40, 100)
    add_snapshot(vault, acct["id"], 10, 200)
    r = unlocked.get(f"/api/accounts/{acct['id']}/history", params={"days": 30})
    start = (today() - dt.timedelta(days=29)).isoformat()
    assert r.json() == [
        {"date": start, "balance": 100.0, "estimated": False},  # carried in from before the window
        {"date": (today() - dt.timedelta(days=10)).isoformat(), "balance": 200.0, "estimated": False},
        {"date": today().isoformat(), "balance": 300.0, "estimated": False},
    ]
    full = unlocked.get(f"/api/accounts/{acct['id']}/history").json()
    assert [p["balance"] for p in full] == [100.0, 200.0, 300.0]
    assert unlocked.get("/api/accounts/999/history").status_code == 404


def test_summary_math(unlocked):
    create(unlocked, name="Checking", category="bank", current_balance=1000.25)
    create(unlocked, name="HSA", category="hsa", current_balance=500)
    create(unlocked, name="401k", category="retirement", current_balance=20000)
    create(unlocked, name="Mortgage", category="loan", current_balance=150000)
    create(unlocked, name="Visa", category="credit", current_balance=1200.75)
    hidden = create(unlocked, name="Old account", category="bank", current_balance=99999)
    unlocked.patch(f"/api/accounts/{hidden['id']}", json={"hidden": True})

    s = unlocked.get("/api/summary").json()
    assert s["total_assets"] == 21500.25
    assert s["total_liabilities"] == 151200.75
    assert s["net_worth"] == -129700.5
    assert s["by_category"] == {"bank": 1000.25, "hsa": 500.0, "retirement": 20000.0,
                                "investment": 0.0, "loan": 150000.0, "credit": 1200.75, "other": 0.0}
    assert s["account_count"] == 5
    assert s["last_synced_at"] is None and s["items_needing_attention"] == 0


def test_networth_history_carry_forward(unlocked, vault):
    bank = create(unlocked, name="Bank", category="bank", current_balance=150)
    loan = create(unlocked, name="Loan", category="loan", current_balance=900)
    hidden = create(unlocked, name="Hidden", category="bank", current_balance=5000)
    unlocked.patch(f"/api/accounts/{hidden['id']}", json={"hidden": True})
    add_snapshot(vault, hidden["id"], 5, 5000)

    add_snapshot(vault, bank["id"], 4, 100)
    add_snapshot(vault, loan["id"], 2, 1000)
    # today's snapshots: bank 150, loan 900 (from creation)

    points = unlocked.get("/api/networth/history").json()
    day = lambda n: (today() - dt.timedelta(days=n)).isoformat()  # noqa: E731
    assert points == [
        {"date": day(4), "assets": 100.0, "liabilities": 0.0, "net_worth": 100.0, "estimated": False},
        {"date": day(3), "assets": 100.0, "liabilities": 0.0, "net_worth": 100.0, "estimated": False},
        {"date": day(2), "assets": 100.0, "liabilities": 1000.0, "net_worth": -900.0, "estimated": False},
        {"date": day(1), "assets": 100.0, "liabilities": 1000.0, "net_worth": -900.0, "estimated": False},
        {"date": day(0), "assets": 150.0, "liabilities": 900.0, "net_worth": -750.0, "estimated": False},
    ]

    clamped = unlocked.get("/api/networth/history", params={"days": 2}).json()
    assert clamped == [
        {"date": day(1), "assets": 100.0, "liabilities": 1000.0, "net_worth": -900.0, "estimated": False},
        {"date": day(0), "assets": 150.0, "liabilities": 900.0, "net_worth": -750.0, "estimated": False},
    ]
    assert unlocked.get("/api/networth/history", params={"days": 0}).status_code == 422


def test_networth_history_empty(unlocked):
    assert unlocked.get("/api/networth/history").json() == []

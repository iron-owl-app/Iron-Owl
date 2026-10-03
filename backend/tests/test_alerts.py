"""Alerts: every alert type with its dedupe key, the settings/events routes and the hooks."""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from app.models import AlertEvent, PlaidItem, RecurringItem
from app.services.alerts import evaluate
from tests.conftest import PASSWORD, add_budget, add_txn, make_account

D = dt.date


def events(session) -> list[AlertEvent]:
    session.expire_all()
    return list(session.scalars(select(AlertEvent).order_by(AlertEvent.id)))


def run(session, day, **kw):
    session.expire_all()  # see changes made through the API (expire_on_commit is off)
    created = evaluate(session, day, **kw)
    session.commit()
    return created


@pytest.fixture
def base(unlocked, db, fixed_today):
    chk = make_account(db, "Chase Checking", "bank", 1842.10, plaid_type="depository", plaid_subtype="checking")
    return unlocked, db, chk


def only(created, key):
    return [e for e in created if e.key == key]


def test_low_balance(base):
    client, db, chk = base
    created = run(db, D(2026, 9, 26))
    low = only(created, "low")
    assert len(low) == 1
    assert (low[0].severity, low[0].title, low[0].body) == ("neg", "Chase Checking is below $2,000", "Balance $1,842.10")
    assert low[0].dedupe_key == f"low:{chk.id}:2026-09-26"
    assert only(run(db, D(2026, 9, 26)), "low") == []  # deduped
    assert len(only(run(db, D(2026, 9, 27)), "low")) == 1  # once per day
    client.put("/api/alerts/settings/low", json={"value": 1000})
    assert only(run(db, D(2026, 9, 28)), "low") == []
    client.put("/api/alerts/settings/low", json={"value": 5000, "enabled": False})
    assert only(run(db, D(2026, 9, 29)), "low") == []


def test_big_transaction(base):
    client, db, chk = base
    big = add_txn(db, chk.id, "2026-09-25", -251, "BESTBUY #12", merchant="Best Buy")
    small = add_txn(db, chk.id, "2026-09-25", -250, "Exactly the limit")
    income = add_txn(db, chk.id, "2026-09-25", 5000, "Payroll", category="INCOME")
    old = add_txn(db, chk.id, "2026-08-01", -900, "Backfilled history")
    xfer = add_txn(db, chk.id, "2026-09-25", -900, "To savings", is_transfer=True)
    ids = [big.id, small.id, income.id, old.id, xfer.id]
    created = only(run(db, D(2026, 9, 26), new_transaction_ids=ids), "big")
    assert [(e.dedupe_key, e.severity, e.title, e.body) for e in created] == [
        (f"big:{big.id}", "warn", "Large transaction at Best Buy", "$251 on Chase Checking, Sep 25")]
    assert only(run(db, D(2026, 9, 26), new_transaction_ids=ids), "big") == []
    assert only(run(db, D(2026, 9, 26)), "big") == []  # only newly added transactions are checked


def test_budget_warning(base):
    client, db, chk = base
    assert client.put("/api/budgets/2026-09", json={"assigned": {"FOOD_AND_DRINK": 100, "TRAVEL": 100}}).status_code == 200
    add_budget(db, "2026-08", {"MEDICAL": 10})  # past months are read-only through the API
    add_txn(db, chk.id, "2026-09-10", -89.99, "Food")
    # August's Medical envelope is overspent (10 - 50), so nothing carries into September.
    add_txn(db, chk.id, "2026-08-10", -50, "Doctor", category="MEDICAL")
    assert only(run(db, D(2026, 9, 26)), "budget") == []
    add_txn(db, chk.id, "2026-09-11", -0.01, "Gum")
    # Settings D7 update 2: only *over* the plan (was: at 90%), never at exactly 100%.
    assert only(run(db, D(2026, 9, 26)), "budget") == []
    add_txn(db, chk.id, "2026-09-11", -10, "Lunch")  # exactly $100 of $100
    assert only(run(db, D(2026, 9, 26)), "budget") == []
    add_txn(db, chk.id, "2026-09-12", -40, "More food")
    created = only(run(db, D(2026, 9, 26)), "budget")
    assert [(e.dedupe_key, e.title, e.body) for e in created] == [
        ("budget:2026-09:FOOD_AND_DRINK", "Food and drink went over its plan", "$140 of $100 spent")]
    add_txn(db, chk.id, "2026-09-12", -50, "Even more food")
    assert only(run(db, D(2026, 9, 26)), "budget") == []  # once per month and category
    add_txn(db, chk.id, "2026-09-12", -150, "Flight", category="TRAVEL")
    created = only(run(db, D(2026, 9, 26)), "budget")
    assert [(e.dedupe_key, e.title, e.body) for e in created] == [
        ("budget:2026-09:TRAVEL", "Travel went over its plan", "$150 of $100 spent")]


def test_budget_warning_counts_carryover_and_budget_accounts_only(base):
    client, db, chk = base
    hsa = make_account(db, "HSA", "hsa", 500)
    add_budget(db, "2026-08", {"FOOD_AND_DRINK": 100})
    add_txn(db, chk.id, "2026-08-10", -60, "August food")  # leaves $40 to carry into September
    add_txn(db, chk.id, "2026-09-10", -35, "Food")
    add_txn(db, hsa.id, "2026-09-10", -100, "Not a budget account")
    # September: carryover 40 + assigned 0; 35 * 100 < 90 * 40.
    assert only(run(db, D(2026, 9, 26)), "budget") == []
    add_txn(db, chk.id, "2026-09-11", -1, "Gum")  # 36 of 40 = 90%: not over (Settings D7)
    assert only(run(db, D(2026, 9, 26)), "budget") == []
    add_txn(db, chk.id, "2026-09-12", -5, "Snack")  # 41 of 40
    created = only(run(db, D(2026, 9, 26)), "budget")
    assert [(e.dedupe_key, e.title, e.body) for e in created] == [
        ("budget:2026-09:FOOD_AND_DRINK", "Food and drink went over its plan", "$41 of $40 spent")]


def test_budget_warning_skips_empty_and_removed_envelopes(base):
    client, db, chk = base
    assert client.put("/api/budgets/2026-09", json={"assigned": {"FOOD_AND_DRINK": 0, "TRAVEL": 10}}).status_code == 200
    assert client.put("/api/budgets/2026-09", json={"removed": ["TRAVEL"]}).status_code == 200
    add_txn(db, chk.id, "2026-09-10", -5, "Food")
    add_txn(db, chk.id, "2026-09-10", -50, "Flight", category="TRAVEL")
    assert only(run(db, D(2026, 9, 26)), "budget") == []


def test_payment_due(base):
    client, db, _chk = base
    day = D(2026, 9, 26)
    card = make_account(db, "Visa", "credit", 100, next_payment_due=day + dt.timedelta(days=3), minimum_payment_cents=3500)
    loan = make_account(db, "Car", "loan", 100, next_payment_due=day)
    make_account(db, "Later", "loan", 100, next_payment_due=day + dt.timedelta(days=4))
    make_account(db, "Past", "loan", 100, next_payment_due=day - dt.timedelta(days=1))
    make_account(db, "Hidden", "credit", 100, next_payment_due=day, hidden=True)
    make_account(db, "Asset", "bank", 100, next_payment_due=day)
    created = only(run(db, day), "due")
    assert [(e.dedupe_key, e.severity, e.title, e.body) for e in created] == [
        (f"due:{loan.id}:2026-09-26", "accent", "Car payment due today", "Due Sep 26"),
        (f"due:{card.id}:2026-09-29", "accent", "Visa payment due Sep 29", "Minimum payment $35"),
    ]
    # Next day: the window moves to include "Later"; the others are deduped by due date.
    assert [e.title for e in only(run(db, day + dt.timedelta(days=1)), "due")] == ["Later payment due Sep 30"]
    client.patch(f"/api/accounts/{loan.id}", json={"next_payment_due": "2026-09-28"})
    assert [e.title for e in only(run(db, day + dt.timedelta(days=1)), "due")] == ["Car payment due tomorrow"]
    make_account(db, "Far", "loan", 100, next_payment_due=day + dt.timedelta(days=9))
    assert only(run(db, day), "due") == []
    client.put("/api/alerts/settings/due", json={"value": 10})
    assert [e.title for e in only(run(db, day), "due")] == ["Far payment due Oct 5"]


def test_new_recurring(base):
    _client, db, chk = base
    item = RecurringItem(name="Netflix", merchant_key="netflix", account_id=chk.id, amount_cents=-1549,
                         cadence="monthly", next_date=D(2026, 10, 10), status="suggested", source="detected")
    active = RecurringItem(name="Pay", merchant_key="pay", account_id=chk.id, amount_cents=200000,
                           cadence="semimonthly", next_date=D(2026, 10, 1), status="active", source="detected")
    db.add_all([item, active])
    db.commit()
    created = only(run(db, D(2026, 9, 26), new_recurring_ids=[item.id, active.id]), "newrec")
    assert [(e.dedupe_key, e.title, e.body) for e in created] == [
        (f"newrec:{item.id}", "New recurring charge: Netflix", "$15.49 monthly")]
    assert only(run(db, D(2026, 9, 26), new_recurring_ids=[item.id]), "newrec") == []


def test_many_new_recurring_items_become_one_summary_alert(base):
    _client, db, chk = base
    items = [
        RecurringItem(name=f"Sub {n}", merchant_key=f"sub{n}", account_id=chk.id, amount_cents=-500 - n,
                      cadence="monthly", next_date=D(2026, 10, 1), status="suggested", source="detected")
        for n in range(5)
    ]
    db.add_all(items)
    db.commit()
    created = only(run(db, D(2026, 9, 26), new_recurring_ids=[i.id for i in items]), "newrec")
    assert [(e.dedupe_key, e.title, e.body) for e in created] == [(
        f"newrec:batch:{items[0].id}-{items[-1].id}",
        "5 new recurring items found",
        "Review them in Bills and paychecks: Sub 0, Sub 1, Sub 2 and 2 more.",
    )]


def test_price_change(base):
    _client, db, chk = base
    item = RecurringItem(name="Netflix", merchant_key="netflix", account_id=chk.id, amount_cents=-1549,
                         cadence="monthly", next_date=D(2026, 10, 10), status="active", source="detected")
    rent = RecurringItem(name="Rent", merchant_key="rent", account_id=None, amount_cents=-100000,
                         cadence="monthly", next_date=D(2026, 10, 1), status="active", source="manual")
    db.add_all([item, rent])
    db.commit()
    add_txn(db, chk.id, "2026-08-10", -15.49, "NETFLIX", merchant="Netflix")
    add_txn(db, chk.id, "2026-09-01", -1004, "Rent")  # 0.4%: more than $0.50 but not more than 1%
    assert only(run(db, D(2026, 9, 26)), "price") == []
    add_txn(db, chk.id, "2026-09-10", -15.98, "NETFLIX", merchant="Netflix")  # +$0.49: under $0.50
    assert only(run(db, D(2026, 9, 26)), "price") == []
    add_txn(db, chk.id, "2026-09-11", -17.99, "NETFLIX", merchant="Netflix", pending=True)  # pending: ignored
    assert only(run(db, D(2026, 9, 26)), "price") == []
    txn = add_txn(db, chk.id, "2026-09-12", -17.99, "NETFLIX", merchant="Netflix")
    # Settings D7 update 2: no visible price event any more; a scrubbed tombstone records it.
    assert only(run(db, D(2026, 9, 26)), "price") == []
    tomb = db.scalar(select(AlertEvent).where(AlertEvent.dedupe_key == f"price:{item.id}:{txn.id}"))
    assert tomb is not None and tomb.cleared and tomb.read and tomb.title == "" and tomb.data is None
    db.refresh(item)
    assert item.amount_cents == -1799  # the item still follows the new price
    item.amount_cents = -1600  # edited by hand since: not overwritten for the same charge
    db.commit()
    run(db, D(2026, 9, 26))
    db.refresh(item)
    assert item.amount_cents == -1600
    rent_txn = add_txn(db, chk.id, "2026-09-20", -1100, "Rent")  # unlinked item: matches any account
    assert only(run(db, D(2026, 9, 26)), "price") == []
    assert db.scalar(select(AlertEvent.id).where(AlertEvent.dedupe_key == f"price:{rent.id}:{rent_txn.id}"))
    db.refresh(rent)
    assert rent.amount_cents == -100000  # a manual item's amount is the user's
    assert all(e.cleared for e in events(db) if e.key == "price")  # nothing visible


def test_connection_alert_dedupes_per_day(base):
    _client, db, _chk = base
    item = PlaidItem(plaid_item_id="i1", access_token="tok", institution_name="Chase", kind="bank",
                     status="login_required", error_code="ITEM_LOGIN_REQUIRED")
    db.add(item)
    db.commit()
    # Settings D7 update 2: bank problems no longer create alert events (Home and Settings >
    # Banks show them from the item's status).
    assert only(run(db, D(2026, 9, 26), status_changes=[(item.id, "login_required"), (item.id, "ok")]), "conn") == []
    assert only(run(db, D(2026, 9, 27), status_changes=[(item.id, "error")]), "conn") == []
    assert db.scalar(select(AlertEvent.id).where(AlertEvent.key == "conn")) is None


def test_disabled_alerts_never_fire(base):
    client, db, chk = base
    for key in ("low", "big", "budget", "due", "newrec", "price", "conn"):
        assert client.put(f"/api/alerts/settings/{key}", json={"enabled": False}).status_code == 200
    txn = add_txn(db, chk.id, "2026-09-25", -5000, "Huge")
    make_account(db, "Visa", "credit", 1, next_payment_due=D(2026, 9, 26))
    assert run(db, D(2026, 9, 26), new_transaction_ids=[txn.id], status_changes=[(1, "error")]) == []


# ------------------------------------------------------------------ routes


def test_settings_routes(base):
    client, db, _chk = base
    settings = client.get("/api/alerts/settings").json()
    assert settings == [
        {"key": "low", "enabled": True, "value": 2000.0, "unit": "usd", "last": None},
        {"key": "big", "enabled": True, "value": 250.0, "unit": "usd", "last": None},
        {"key": "budget", "enabled": True, "value": 90.0, "unit": "percent", "last": None},
        {"key": "due", "enabled": True, "value": 3.0, "unit": "days", "last": None},
        {"key": "newrec", "enabled": True, "value": None, "unit": None, "last": None},
        {"key": "price", "enabled": True, "value": None, "unit": None, "last": None},
        {"key": "conn", "enabled": True, "value": None, "unit": None, "last": None},
        {"key": "income", "enabled": True, "value": None, "unit": None, "last": None},
        {"key": "low_ahead", "enabled": True, "value": 7.0, "unit": "days", "last": None},
        {"key": "reminder", "enabled": True, "value": None, "unit": None, "last": None},
    ]
    run(db, D(2026, 9, 26))
    low = client.get("/api/alerts/settings").json()[0]
    assert low["last"]["text"] == "Chase Checking is below $2,000"
    assert dt.date.fromisoformat(low["last"]["date"])  # local calendar date of the event

    r = client.put("/api/alerts/settings/budget", json={"value": 75.5})
    assert r.json() == {"key": "budget", "enabled": True, "value": 75.5, "unit": "percent", "last": None}
    assert client.put("/api/alerts/settings/newrec", json={"enabled": False}).json()["enabled"] is False
    for key, body in [
        ("budget", {"value": 101}), ("budget", {"value": 0}), ("low", {"value": -5}), ("big", {"value": None}),
        ("newrec", {"value": 1}), ("low", {"enabled": None}), ("low", {"value": "lots"}), ("due", {"value": 400}),
        ("low", {"extra": 1}),
    ]:
        assert client.put(f"/api/alerts/settings/{key}", json=body).status_code == 422, (key, body)
    assert client.put("/api/alerts/settings/nope", json={"enabled": True}).status_code == 404
    assert client.put("/api/alerts/settings/budget", json={"value": 100}).status_code == 200


def test_event_routes_and_summary(base):
    client, db, chk = base
    run(db, D(2026, 9, 26))  # low
    t = add_txn(db, chk.id, "2026-09-26", -300, "TV")
    run(db, D(2026, 9, 26), new_transaction_ids=[t.id])  # big
    listed = client.get("/api/alerts/events").json()
    assert [e["key"] for e in listed] == ["big", "low"]  # newest first
    first = listed[0]
    assert set(first) == {"id", "key", "severity", "title", "body", "created_at", "read"}
    assert first["created_at"].endswith("Z") and first["read"] is False
    assert len(client.get("/api/alerts/events", params={"limit": 1}).json()) == 1
    assert client.get("/api/alerts/events", params={"limit": 0}).status_code == 422
    assert client.get("/api/summary").json()["unread_alerts"] == 2

    assert client.post("/api/alerts/events/read", json={}).json() == {"ok": True}
    assert all(e["read"] for e in client.get("/api/alerts/events").json())
    assert client.get("/api/summary").json()["unread_alerts"] == 0

    assert client.delete("/api/alerts/events").status_code == 204
    assert client.get("/api/alerts/events").json() == []
    assert client.get("/api/alerts/settings").json()[0]["last"] is None
    # Cleared alerts don't come back on the next evaluation (their dedupe keys are kept).
    run(db, D(2026, 9, 26), new_transaction_ids=[t.id])
    assert client.get("/api/alerts/events").json() == []
    assert not any(e.title or e.body for e in events(db))  # scrubbed


def test_alert_routes_require_session(client):
    for path in ("/api/alerts/settings", "/api/alerts/events"):
        assert client.get(path).status_code == 401
    assert client.post("/api/alerts/events/read", json={}).status_code == 401
    assert client.delete("/api/alerts/events").status_code == 401


# ------------------------------------------------------------------ hooks


def test_evaluated_after_unlock(base):
    client, db, _chk = base
    assert client.get("/api/alerts/events").json() == []
    client.post("/api/auth/lock")
    assert client.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200
    assert [e["key"] for e in client.get("/api/alerts/events").json()] == ["low"]


def test_evaluated_after_manual_account_changes(unlocked, fixed_today):
    acct = unlocked.post("/api/accounts", json={"name": "Wallet", "category": "bank", "current_balance": 5000}).json()
    unlocked.put("/api/forecast/account", json={"account_id": acct["id"]})
    assert unlocked.get("/api/alerts/events").json() == []
    unlocked.patch(f"/api/accounts/{acct['id']}", json={"current_balance": 150})
    assert [e["title"] for e in unlocked.get("/api/alerts/events").json()] == ["Wallet is below $2,000"]
    unlocked.post("/api/accounts", json={"name": "Visa", "category": "credit", "current_balance": 10,
                                         "next_payment_due": "2026-09-27"})
    assert [e["key"] for e in unlocked.get("/api/alerts/events").json()] == ["due", "low"]


def test_evaluated_after_sync(unlocked, fake_plaid, fixed_today):
    from tests.test_plaid import BANK_TOKEN, acct, txn

    fake_plaid.add_item("public-bank", BANK_TOKEN, "item_bank", "First Bank")
    fake_plaid.accounts[BANK_TOKEN] = [acct("chk", "Checking", "depository", "checking", 9000)]
    fake_plaid.txn_pages[BANK_TOKEN] = [
        {"cursor_in": None, "added": [txn("old", "chk", 999, "Old purchase", date="2026-01-05")],
         "modified": [], "removed": [], "next_cursor": "c1", "has_more": False},
        {"cursor_in": "c1", "added": [txn("new", "chk", 400, "New TV", date="2026-09-25", merchant="Best Buy")],
         "modified": [], "removed": [], "next_cursor": "c2", "has_more": False},
    ]
    item = unlocked.post("/api/plaid/exchange", json={"public_token": "public-bank", "kind": "bank"}).json()["item"]
    unlocked.post(f"/api/plaid/items/{item['id']}/import", json={"plaid_account_ids": ["chk"]})
    assert unlocked.get("/api/alerts/events").json() == []  # backfilled history doesn't alert

    unlocked.post("/api/plaid/sync", json={})
    assert [e["title"] for e in unlocked.get("/api/alerts/events").json()] == ["Large transaction at Best Buy"]

    fake_plaid.fail(BANK_TOKEN, "accounts", "ITEM_LOGIN_REQUIRED")
    unlocked.post("/api/plaid/sync", json={})
    fake_plaid.fail(BANK_TOKEN, "accounts", "ITEM_LOGIN_REQUIRED")
    unlocked.post("/api/plaid/sync", json={})  # still broken: not a new change
    titles = [e["title"] for e in unlocked.get("/api/alerts/events").json()]
    assert titles == ["Large transaction at Best Buy"]  # Settings D7: no "conn" events any more
    assert unlocked.get("/api/summary").json()["unread_alerts"] == 1


def test_alert_failure_never_breaks_the_caller(unlocked, fixed_today, monkeypatch):
    import app.services.alerts as alerts_mod

    def boom(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(alerts_mod, "evaluate", boom)
    r = unlocked.post("/api/accounts", json={"name": "Wallet", "category": "bank", "current_balance": 5})
    assert r.status_code == 201
    unlocked.post("/api/auth/lock")
    assert unlocked.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200

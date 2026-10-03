"""Recurring detection (cadence bands and edge cases), recurring CRUD and the forecast."""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from app.models import RecurringItem
from app.services.recurring import classify, merchant_key, next_after, occurrences
from tests.conftest import add_txn, make_account

D = dt.date


def series(*isos: str) -> list[dt.date]:
    return [D.fromisoformat(s) for s in isos]


def same(n: int, cents: int = -1000) -> list[int]:
    return [cents] * n


# ------------------------------------------------------------------ classify (unit)


@pytest.mark.parametrize(
    ("dates", "expected"),
    [
        (series("2026-08-01", "2026-08-08", "2026-08-15", "2026-08-22"), ("weekly", None)),
        (series("2026-08-01", "2026-08-07", "2026-08-15", "2026-08-22"), ("weekly", None)),  # 6 and 8 days
        (series("2026-08-04", "2026-08-18", "2026-09-01", "2026-09-15"), ("biweekly", None)),
        (series("2026-08-04", "2026-08-17", "2026-08-31"), ("biweekly", None)),  # 13 and 14
        # 1st and 15th: intervals 14/16/14/17/14 straddle the biweekly band.
        (series("2026-06-01", "2026-06-15", "2026-07-01", "2026-07-15", "2026-08-01", "2026-08-15"),
         ("semimonthly", [1, 15])),
        # 15th and month end, including February (13 days: 4 of 5 intervals in band = 80%).
        (series("2026-01-15", "2026-01-31", "2026-02-15", "2026-02-28", "2026-03-15", "2026-03-31"),
         ("semimonthly", [15, 31])),
        # 1st and last day across 30/31-day months: the month end counts as one day.
        (series("2026-04-01", "2026-04-30", "2026-05-01", "2026-05-31", "2026-06-01", "2026-06-30"),
         None),  # intervals 29/1: not a cadence at all
        (series("2026-04-15", "2026-04-30", "2026-05-15", "2026-05-31", "2026-06-15", "2026-06-30"),
         ("semimonthly", [15, 31])),
        (series("2026-06-10", "2026-07-10", "2026-08-10", "2026-09-10"), ("monthly", [10])),
        (series("2026-01-31", "2026-02-28", "2026-03-31", "2026-04-30"), ("monthly", [31])),
        (series("2026-06-05", "2026-07-07", "2026-08-05", "2026-09-05"), ("monthly", [5])),
        (series("2026-03-10", "2026-06-10"), ("quarterly", [10])),  # 2 occurrences suffice
        (series("2026-08-10", "2026-09-10"), ("monthly", [10])),  # Release 3.19: 2 monthly charges
        (series("2025-09-01", "2025-12-01", "2026-03-02", "2026-06-01"), ("quarterly", [1])),
        (series("2025-09-20", "2026-09-20"), ("yearly", [20])),
        (series("2024-02-29", "2025-02-28"), ("yearly", [31])),  # leap day: 365 days, month end
        (series("2024-03-01", "2025-03-01"), ("yearly", [1])),  # 365 days across a leap Feb
        (series("2023-09-20", "2024-09-20", "2025-09-20"), ("yearly", [20])),  # 366, 365
        # 70% of intervals in band is enough (3 of 4); the median decides the band.
        (series("2026-03-01", "2026-04-01", "2026-05-01", "2026-06-29", "2026-07-29"), ("monthly", [1])),
    ],
)
def test_classify_cadences(dates, expected):
    assert classify(dates, same(len(dates))) == expected


def test_semimonthly_with_a_weekend_shift_is_not_forced():
    # A payday moved to Friday the 12th gives 3 distinct days and 11/19-day gaps: no band fits 70%.
    dates = series("2026-05-01", "2026-05-15", "2026-06-01", "2026-06-12", "2026-07-01", "2026-07-15")
    assert classify(dates, same(6)) is None


@pytest.mark.parametrize(
    "dates",
    [
        # Release 3.19: 2 monthly charges are enough, but only in consecutive months.
        series("2026-01-31", "2026-03-02"),
        series("2026-08-01", "2026-08-15"),  # semimonthly/biweekly need 3
        series("2026-09-01"),
        series("2026-01-01", "2026-02-15", "2026-04-01", "2026-04-20"),  # irregular
        series("2026-03-01", "2026-04-01", "2026-06-01", "2026-08-01"),  # median 61: no band
        series("2026-01-01", "2026-01-31", "2026-03-31", "2026-05-30", "2026-06-29"),  # 60% in band
        series("2025-01-01", "2025-06-01"),  # 151 days
        series("2024-09-01", "2025-09-20"),  # 384 days: outside the yearly band
    ],
)
def test_classify_rejects(dates):
    assert classify(dates, same(len(dates))) is None


def test_classify_amount_tolerance():
    dates = series("2026-06-10", "2026-07-10", "2026-08-10", "2026-09-10")
    assert classify(dates, [-1000, -1100, -1200, -1000]) is not None  # within 20% of the median
    weekly = series("2026-08-01", "2026-08-08", "2026-08-15", "2026-08-22")
    assert classify(weekly, [-1000, -1000, -1000, -1300]) is None  # one outlier is too far
    # Release 3.19: monthly on the same day every time, so the amount may change (a utility).
    assert classify(dates, [-1000, -1000, -1000, -1300]) == ("monthly", [10])
    assert classify(dates, [0, 0, 0, 0]) is None


def test_biweekly_three_occurrences_on_two_days_is_semimonthly():
    # Feb 1, Feb 15, Mar 1 (non-leap) is indistinguishable from the 1st and 15th.
    assert classify(series("2026-02-01", "2026-02-15", "2026-03-01"), same(3)) == ("semimonthly", [1, 15])


# ------------------------------------------------------------------ cadence math (unit)


def take(it, n):
    return [d.isoformat() for _, d in zip(range(n), it)]


def test_occurrences():
    assert take(occurrences(D(2026, 1, 31), "monthly", [31]), 4) == ["2026-01-31", "2026-02-28", "2026-03-31", "2026-04-30"]
    assert take(occurrences(D(2026, 1, 30), "monthly"), 3) == ["2026-01-30", "2026-02-28", "2026-03-30"]
    assert take(occurrences(D(2026, 9, 15), "semimonthly"), 4) == ["2026-09-15", "2026-10-01", "2026-10-15", "2026-11-01"]
    assert take(occurrences(D(2026, 2, 15), "semimonthly", [15, 31]), 4) == ["2026-02-15", "2026-02-28", "2026-03-15", "2026-03-31"]
    assert take(occurrences(D(2026, 9, 3), "semimonthly", [1, 15]), 3) == ["2026-09-03", "2026-09-15", "2026-10-01"]
    assert take(occurrences(D(2026, 9, 1), "weekly"), 3) == ["2026-09-01", "2026-09-08", "2026-09-15"]
    assert take(occurrences(D(2026, 9, 1), "biweekly"), 3) == ["2026-09-01", "2026-09-15", "2026-09-29"]
    assert take(occurrences(D(2025, 11, 30), "quarterly", [31]), 3) == ["2025-11-30", "2026-02-28", "2026-05-31"]
    assert take(occurrences(D(2024, 2, 29), "yearly"), 3) == ["2024-02-29", "2025-02-28", "2026-02-28"]


def test_next_after_rolls_forward_to_today():
    today = D(2026, 9, 26)
    assert next_after(D(2026, 9, 10), "monthly", [10], today) == D(2026, 10, 10)
    assert next_after(D(2026, 6, 10), "monthly", [10], today) == D(2026, 10, 10)  # rolled past Jul-Sep
    assert next_after(D(2026, 8, 26), "monthly", [26], today) == D(2026, 9, 26)  # today counts
    assert next_after(D(2026, 9, 15), "semimonthly", [1, 15], today) == D(2026, 10, 1)
    assert next_after(D(2026, 9, 20), "weekly", None, today) == D(2026, 9, 27)


def test_merchant_key():
    assert merchant_key("  Blue   Bottle ", "x") == "blue bottle"
    assert merchant_key(None, "NETFLIX.COM 866-579-7172 CA") == "netflix com ca"
    assert merchant_key("", "SQ *JOE'S #1234") == "sq joe's"
    assert merchant_key(None, "12345") == "12345"


# ------------------------------------------------------------------ detection (integration)


@pytest.fixture
def history(unlocked, db, fixed_today):
    chk = make_account(db, "Checking", "bank", 5000, plaid_type="depository", plaid_subtype="checking")
    card = make_account(db, "Visa", "credit", 200, plaid_type="credit", plaid_subtype="credit card")
    hidden = make_account(db, "Old", "bank", 0, hidden=True)
    for day in ("2026-06-10", "2026-07-10", "2026-08-10", "2026-09-10"):
        add_txn(db, card.id, day, -15.49, "NETFLIX.COM", merchant="Netflix", category="ENTERTAINMENT")
        add_txn(db, hidden.id, day, -9.99, "Hidden sub", merchant="Hulu", category="ENTERTAINMENT")
    for day in ("2026-07-01", "2026-07-15", "2026-08-01", "2026-08-15", "2026-09-01", "2026-09-15"):
        add_txn(db, chk.id, day, 2000, "ACME PAYROLL PPD", category="INCOME")
        add_txn(db, chk.id, day, -100, "Transfer to savings", category="TRANSFER_OUT", is_transfer=True)
    for day in ("2026-07-20", "2026-08-20", "2026-09-20"):
        add_txn(db, chk.id, day, -60, "Gym", merchant="Gym Co", pending=True)
    for day in ("2026-01-05", "2026-02-05", "2026-03-05"):  # stopped in March
        add_txn(db, chk.id, day, -30, "Old sub", merchant="Old Co")
    return unlocked, chk, card


def test_detection_suggests_items(history):
    client, chk, card = history
    assert client.post("/api/recurring/detect", json={}).json() == {"suggested": 2}
    items = {i["name"]: i for i in client.get("/api/recurring").json()}
    assert set(items) == {"Netflix", "ACME PAYROLL PPD"}
    assert items["Netflix"] == {
        "id": items["Netflix"]["id"], "name": "Netflix", "account_id": card.id, "account_name": "Visa",
        "amount": -15.49, "cadence": "monthly", "next_date": "2026-10-10", "status": "suggested",
        "include_in_forecast": True, "source": "detected", "last_seen_date": "2026-09-10",
        "reminder_days": 0, "start_date": None, "anchor_days": [10],
        # Release 3.6: the most common category of its transactions.
        "category_id": "ENTERTAINMENT", "category_name": "Entertainment",
        "price_question": None,  # Release 3.19 (only GET /api/recurring)
    }
    pay = items["ACME PAYROLL PPD"]
    assert (pay["cadence"], pay["amount"], pay["next_date"], pay["account_id"]) == ("semimonthly", 2000.0, "2026-10-01", chk.id)
    # Idempotent: nothing new on a second run.
    assert client.post("/api/recurring/detect", json={}).json() == {"suggested": 0}
    assert len(client.get("/api/recurring").json()) == 2


def test_detection_updates_existing_items_and_keeps_dismissed(history, db, fixed_today):
    client, chk, card = history
    client.post("/api/recurring/detect", json={})
    netflix = next(i for i in client.get("/api/recurring").json() if i["name"] == "Netflix")
    client.patch(f"/api/recurring/{netflix['id']}", json={"status": "dismissed"})

    fixed_today.set(D(2026, 10, 12))
    add_txn(db, card.id, "2026-10-10", -15.49, "NETFLIX.COM", merchant="Netflix")
    assert client.post("/api/recurring/detect", json={}).json() == {"suggested": 0}
    netflix = next(i for i in client.get("/api/recurring").json() if i["name"] == "Netflix")
    assert netflix["status"] == "dismissed"
    assert (netflix["last_seen_date"], netflix["next_date"]) == ("2026-10-10", "2026-11-10")
    pay = next(i for i in client.get("/api/recurring").json() if i["name"] == "ACME PAYROLL PPD")
    assert pay["next_date"] == "2026-10-15"  # rolled forward even without a new occurrence
    assert db.scalar(select(RecurringItem.month_days).where(RecurringItem.id == pay["id"])) == "[1, 15]"


def test_detection_runs_after_sync(unlocked, fake_plaid, fixed_today):
    from tests.test_plaid import BANK_TOKEN, acct, txn

    fake_plaid.add_item("public-bank", BANK_TOKEN, "item_bank", "First Bank")
    fake_plaid.accounts[BANK_TOKEN] = [acct("chk", "Checking", "depository", "checking", 100)]
    fake_plaid.txn_pages[BANK_TOKEN] = [{
        "cursor_in": None,
        "added": [txn(f"s{m}", "chk", 9.99, "SPOTIFY", date=f"2026-0{m}-03", merchant="Spotify") for m in (6, 7, 8, 9)],
        "modified": [], "removed": [], "next_cursor": "c1", "has_more": False,
    }]
    item = unlocked.post("/api/plaid/exchange", json={"public_token": "public-bank", "kind": "bank"}).json()["item"]
    unlocked.post(f"/api/plaid/items/{item['id']}/import", json={"plaid_account_ids": ["chk"]})
    items = unlocked.get("/api/recurring").json()
    assert [(i["name"], i["status"], i["next_date"]) for i in items] == [("Spotify", "suggested", "2026-10-03")]


# ------------------------------------------------------------------ CRUD


def test_recurring_crud(unlocked, db, fixed_today):
    chk = make_account(db, "Checking", "bank", 100)
    r = unlocked.post("/api/recurring", json={"name": " Rent ", "amount": -1500, "cadence": "monthly",
                                              "next_date": "2026-10-01", "account_id": chk.id})
    assert r.status_code == 201
    rent = r.json()
    assert rent == {"id": rent["id"], "name": "Rent", "account_id": chk.id, "account_name": "Checking",
                    "amount": -1500.0, "cadence": "monthly", "next_date": "2026-10-01", "status": "active",
                    "include_in_forecast": True, "source": "manual", "last_seen_date": None,
                    "reminder_days": 0, "start_date": "2026-10-01", "anchor_days": [1],
                    "category_id": None, "category_name": None}
    bad = [
        {"name": "x", "amount": 0, "cadence": "monthly", "next_date": "2026-10-01"},
        {"name": "x", "amount": 5, "cadence": "fortnightly", "next_date": "2026-10-01"},
        {"name": "x", "amount": 5, "cadence": "monthly", "next_date": "2026-10-01", "account_id": 999},
        {"name": " ", "amount": 5, "cadence": "monthly", "next_date": "2026-10-01"},
        {"name": "x", "amount": 5, "cadence": "monthly"},
    ]
    for body in bad:
        assert unlocked.post("/api/recurring", json=body).status_code == 422, body

    sub = unlocked.post("/api/recurring", json={"name": "Gym", "amount": -40, "cadence": "weekly",
                                                "next_date": "2026-09-28"}).json()
    assert sub["account_id"] is None and sub["account_name"] is None

    r = unlocked.patch(f"/api/recurring/{rent['id']}", json={"include_in_forecast": False, "amount": -1550.5,
                                                             "status": "suggested", "account_id": None})
    body = r.json()
    assert (body["include_in_forecast"], body["amount"], body["status"], body["account_id"]) == (False, -1550.5, "suggested", None)
    r = unlocked.patch(f"/api/recurring/{rent['id']}", json={"cadence": "quarterly", "next_date": "2026-11-30"})
    assert (r.json()["cadence"], r.json()["next_date"]) == ("quarterly", "2026-11-30")
    for patch in ({"name": None}, {"status": "paused"}, {"amount": 0}, {"cadence": None}, {"account_id": 999}):
        assert unlocked.patch(f"/api/recurring/{rent['id']}", json=patch).status_code == 422, patch
    assert unlocked.patch("/api/recurring/999", json={"name": "x"}).status_code == 404

    dismissed = unlocked.post("/api/recurring", json={"name": "Old", "amount": -1, "cadence": "yearly",
                                                      "next_date": "2026-09-27"}).json()
    unlocked.patch(f"/api/recurring/{dismissed['id']}", json={"status": "dismissed"})
    order = [(i["name"], i["status"]) for i in unlocked.get("/api/recurring").json()]
    assert order == [("Gym", "active"), ("Rent", "suggested"), ("Old", "dismissed")]

    assert unlocked.delete(f"/api/recurring/{sub['id']}").status_code == 204
    assert unlocked.delete(f"/api/recurring/{sub['id']}").status_code == 404

    # Deleting the account keeps the item, unlinked.
    unlocked.patch(f"/api/recurring/{rent['id']}", json={"account_id": chk.id})
    unlocked.delete(f"/api/accounts/{chk.id}")
    assert next(i for i in unlocked.get("/api/recurring").json() if i["id"] == rent["id"])["account_id"] is None


# ------------------------------------------------------------------ forecast


@pytest.fixture
def cash(unlocked, db, fixed_today):
    small = make_account(db, "Small Checking", "bank", 3000, plaid_type="depository", plaid_subtype="checking",
                         mask="1111", institution_name="Bank A")
    main = make_account(db, "Total Checking", "bank", 8432.18, plaid_type="depository", plaid_subtype="checking",
                        mask="4417", institution_name="Chase")
    make_account(db, "Hidden Checking", "bank", 99999, plaid_type="depository", plaid_subtype="checking", hidden=True)
    savings = make_account(db, "Savings", "bank", 20000, plaid_type="depository", plaid_subtype="savings")
    card = make_account(db, "Visa", "credit", 400, plaid_type="credit", plaid_subtype="credit card")
    manual = make_account(db, "Cash jar", "bank", 50)

    def item(name, amount, cadence, next_date, account=None, status="active", include=True):
        r = unlocked.post("/api/recurring", json={"name": name, "amount": amount, "cadence": cadence,
                                                  "next_date": next_date,
                                                  "account_id": account.id if account else None})
        body = r.json()
        unlocked.patch(f"/api/recurring/{body['id']}", json={"status": status, "include_in_forecast": include})
        return body["id"]

    ids = {
        "rent": item("Rent", -1500, "monthly", "2026-10-01", main),
        "pay": item("Payroll", 2000, "semimonthly", "2026-10-01", main),
        "netflix": item("Netflix", -15.49, "monthly", "2026-10-10", card),
        "gym": item("Gym", -40, "monthly", "2026-09-28", None, include=False),
        "today": item("Paper", -5, "monthly", "2026-09-26", main),
        "other": item("Other acct", -10, "weekly", "2026-09-27", small),
        "sugg": item("Maybe", -10, "weekly", "2026-09-27", main, status="suggested"),
        "gone": item("Gone", -10, "weekly", "2026-09-27", main, status="dismissed"),
    }
    # Everyday spending on the main account (last 90 days = Jun 29..Sep 26).
    add_txn(db, main.id, "2026-09-20", -90, "Groceries")
    add_txn(db, main.id, "2026-07-15", -45, "Dinner")
    add_txn(db, main.id, "2026-09-21", 30, "Refund")  # inflows don't reduce outflow
    add_txn(db, main.id, "2026-09-01", -1500, "Rent")  # matches the active Rent item
    add_txn(db, main.id, "2026-09-02", -500, "Move", category="TRANSFER_OUT")
    add_txn(db, main.id, "2026-09-03", -200, "Car", category="LOAN_PAYMENTS")
    add_txn(db, main.id, "2026-09-04", -300, "To savings", is_transfer=True)
    add_txn(db, main.id, "2026-06-01", -1000, "Too old")
    add_txn(db, small.id, "2026-09-05", -700, "Other account")
    return unlocked, {"main": main, "savings": savings, "card": card, "manual": manual, "small": small}, ids


def test_forecast(cash):
    client, accts, ids = cash
    f = client.get("/api/forecast").json()
    assert f["account"] == {"id": accts["main"].id, "name": "Total Checking", "mask": "4417", "institution_name": "Chase"}
    assert (f["balance"], f["today"], f["end"], f["threshold"]) == (8432.18, "2026-09-26", "2026-10-31", 2000.0)
    assert f["daily_spend"] == 1.5  # (90 + 45) / 90
    ev = [(e["date"], e["name"], e["kind"], e["amount"], e["included"]) for e in f["events"]]
    assert ev == [
        ("2026-09-28", "Gym", "out", -40.0, False),
        ("2026-10-01", "Payroll", "in", 2000.0, True),
        ("2026-10-01", "Rent", "out", -1500.0, True),
        ("2026-10-10", "Netflix", "card", -15.49, True),
        ("2026-10-15", "Payroll", "in", 2000.0, True),
        ("2026-10-26", "Paper", "out", -5.0, True),  # today's occurrence is skipped
        ("2026-10-28", "Gym", "out", -40.0, False),
    ]
    assert {e["recurring_id"] for e in f["events"]} == {ids[k] for k in ("gym", "pay", "rent", "netflix", "today")}

    short = client.get("/api/forecast", params={"end": "2026-10-01"}).json()
    assert [e["date"] for e in short["events"]] == ["2026-09-28", "2026-10-01", "2026-10-01"]
    assert client.get("/api/forecast", params={"end": "2026-09-01"}).json()["events"] == []
    assert client.get("/api/forecast", params={"end": "2030-01-01"}).status_code == 422
    assert client.get("/api/forecast", params={"end": "soon"}).status_code == 422


def test_forecast_account_setting(cash):
    client, accts, _ids = cash
    r = client.put("/api/forecast/account", json={"account_id": accts["savings"].id})
    assert r.status_code == 200 and r.json()["account"]["id"] == accts["savings"].id
    # Items on other depository accounts drop out; unlinked and card items follow any account.
    assert {e["name"] for e in r.json()["events"]} == {"Gym", "Netflix"}
    assert client.get("/api/forecast").json()["account"]["name"] == "Savings"
    assert client.put("/api/forecast/account", json={"account_id": accts["card"].id}).status_code == 422
    assert client.put("/api/forecast/account", json={"account_id": 9999}).status_code == 404
    assert client.put("/api/forecast/account", json={"account_id": accts["manual"].id}).status_code == 200
    # If the chosen account disappears, fall back to the largest visible checking account.
    client.delete(f"/api/accounts/{accts['manual'].id}")
    assert client.get("/api/forecast").json()["account"]["name"] == "Total Checking"


def test_forecast_events_for_unlinked_and_card_items_follow_any_account(cash):
    client, accts, _ids = cash
    client.put("/api/forecast/account", json={"account_id": accts["small"].id})
    f = client.get("/api/forecast").json()
    names = {e["name"] for e in f["events"]}
    assert names == {"Gym", "Netflix", "Other acct"}
    assert f["daily_spend"] == round(700 / 22, 2)  # history starts Sep 5: 22 days, not 90


def test_forecast_without_checking(unlocked, db, fixed_today):
    make_account(db, "Savings", "bank", 100, plaid_type="depository", plaid_subtype="savings")
    f = unlocked.get("/api/forecast").json()
    assert f == {"account": None, "balance": 0.0, "today": "2026-09-26", "end": "2026-10-31",
                 "daily_spend": 0.0, "threshold": 2000.0, "events": []}
    unlocked.put("/api/alerts/settings/low", json={"value": 1500})
    assert unlocked.get("/api/forecast").json()["threshold"] == 1500.0


def test_forecast_default_end_in_december(unlocked, fixed_today):
    fixed_today.set(D(2026, 12, 5))
    assert unlocked.get("/api/forecast").json()["end"] == "2027-01-31"

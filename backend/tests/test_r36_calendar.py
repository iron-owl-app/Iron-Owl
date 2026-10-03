"""Release 3.6: the cash forecast calendar, occurrence overrides, reschedule/restore,
candidates, budget categories on bills, the low_ahead/reminder alerts and migration v6."""
from __future__ import annotations

import datetime as dt
import json
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import db as dbmod
from app import migrations
from app.models import AlertEvent, AppSetting, RecurringItem, RecurringOverride, TxnCategory
from app.seeds import ALERT_ORDER
from app.services import automation, calendar
from app.services import recurring as recsvc
from app.services.alerts import evaluate
from app.services.categories import load as load_categories
from app.services.recurring import (
    anchor_days,
    category_from_rows,
    merchant_key,
    next_base,
    occurrences,
    schedule_dates,
)
from tests.conftest import BASE_URL, PASSWORD, SessionClient, add_txn, make_account
from tests.test_home import build_v4_vault
from tests.test_migrations import REAL_LATEST, columns, raw

D = dt.date.fromisoformat
TODAY = dt.date(2026, 9, 26)  # conftest.FIXED_TODAY, a Saturday


def ok(response, status: int = 200):
    assert response.status_code == status, response.text
    return response.json() if response.content else None


def cal(client, **params) -> dict:
    return ok(client.get("/api/forecast/calendar", params={("from" if k == "start" else "to" if k == "end" else k): v
                                                             for k, v in params.items()}))


def by_key(body: dict) -> dict[str, dict]:
    return {o["key"]: o for o in body["occurrences"]}


def day_balances(body: dict) -> dict[str, float | None]:
    return {d["date"]: d["balance"] for d in body["days"]}


def add(client, name, amount, cadence, next_date, **kw) -> dict:
    return ok(client.post("/api/recurring", json={"name": name, "amount": amount, "cadence": cadence,
                                                  "next_date": next_date, **kw}), 201)


def make_item(db, name, amount, cadence, next_date, *, account=None, source="detected", last_seen=None,
              days=None, start=None, status="active", include=True, category=None, reminder=0) -> RecurringItem:
    nd = D(next_date)
    if days is None:
        days = anchor_days(cadence, nd)
    item = RecurringItem(
        name=name, merchant_key=merchant_key(name, name), account_id=account.id if account else None,
        amount_cents=round(amount * 100), cadence=cadence, next_date=nd, status=status,
        include_in_forecast=include, source=source, last_seen_date=D(last_seen) if last_seen else None,
        month_days=json.dumps(days) if days else None, start_date=D(start) if start else None,
        category_id=category, reminder_days=reminder,
    )
    db.add(item)
    db.commit()
    return item


def statuses(db, item, start, end) -> dict[str, tuple]:
    db.expire_all()
    return {o.base_date.isoformat(): (o.status, o.transaction_id)
            for o in calendar.occurrences_for(db, TODAY, D(start), D(end), [item])}


@pytest.fixture
def base(unlocked, db, fixed_today):
    chk = make_account(db, "Total Checking", "bank", 5000, plaid_type="depository", plaid_subtype="checking",
                       mask="4417", institution_name="Chase")
    card = make_account(db, "Visa", "credit", 300, plaid_type="credit", plaid_subtype="credit card", mask="9911")
    savings = make_account(db, "Savings", "bank", 9000, plaid_type="depository", plaid_subtype="savings")
    return SimpleNamespace(client=unlocked, db=db, chk=chk, card=card, savings=savings, today=fixed_today)


# ------------------------------------------------------------------ generation (unit)


def iso(dates):
    return [d.isoformat() for d in dates]


def test_schedule_dates_both_directions():
    assert iso(schedule_dates(D("2026-03-31"), "monthly", [31], D("2026-01-01"), D("2026-05-31"))) == [
        "2026-01-31", "2026-02-28", "2026-03-31", "2026-04-30", "2026-05-31"]
    assert iso(schedule_dates(D("2026-09-26"), "weekly", None, D("2026-09-10"), D("2026-10-10"))) == [
        "2026-09-12", "2026-09-19", "2026-09-26", "2026-10-03", "2026-10-10"]
    assert iso(schedule_dates(D("2026-09-26"), "biweekly", None, D("2026-09-01"), D("2026-10-15"))) == [
        "2026-09-12", "2026-09-26", "2026-10-10"]
    assert iso(schedule_dates(D("2026-11-30"), "quarterly", [31], D("2026-01-01"), D("2027-06-30"))) == [
        "2026-02-28", "2026-05-31", "2026-08-31", "2026-11-30", "2027-02-28", "2027-05-31"]
    assert iso(schedule_dates(D("2025-03-15"), "yearly", [15], D("2024-01-01"), D("2026-12-31"))) == [
        "2024-03-15", "2025-03-15", "2026-03-15"]
    assert iso(schedule_dates(D("2026-10-05"), "once", None, D("2026-01-01"), D("2026-12-31"))) == ["2026-10-05"]
    assert schedule_dates(D("2026-10-05"), "once", None, D("2026-10-06"), D("2026-12-31")) == []
    assert iso(schedule_dates(D("2026-02-15"), "semimonthly", [15, 31], D("2026-01-01"), D("2026-03-01"))) == [
        "2026-01-15", "2026-01-31", "2026-02-15", "2026-02-28"]
    # start_date clamps the past; the rule date itself is always kept.
    assert iso(schedule_dates(D("2026-10-01"), "monthly", [1], D("2026-07-01"), D("2026-11-30"),
                              not_before=D("2026-09-15"))) == ["2026-10-01", "2026-11-01"]
    # Forward, it agrees with the legacy generator.
    first = D("2026-01-31")
    legacy = [d for _, d in zip(range(12), occurrences(first, "monthly", [31]))]
    assert schedule_dates(first, "monthly", [31], first, legacy[-1]) == legacy
    assert list(occurrences(D("2026-10-05"), "once")) == [D("2026-10-05")]


def test_horizon_end():
    assert calendar.horizon_end(TODAY) == D("2027-09-30")
    assert calendar.horizon_end(D("2026-01-31")) == D("2027-01-31")
    assert calendar.horizon_end(D("2027-02-10")) == D("2028-02-29")


# ------------------------------------------------------------------ status and matching


@pytest.mark.parametrize(("cadence", "next_date", "base_date", "inside", "outside"), [
    ("monthly", "2026-10-10", "2026-09-10", 7, 8),
    ("biweekly", "2026-10-03", "2026-09-05", 5, 6),
    ("weekly", "2026-10-03", "2026-09-12", 3, None),
])
def test_match_window_per_cadence(base, cadence, next_date, base_date, inside, outside):
    db = base.db
    item = make_item(db, "Acme", -50, cadence, next_date, account=base.chk, last_seen="2026-08-01")
    txn = add_txn(db, base.chk.id, D(base_date) + dt.timedelta(days=inside), -50, "ACME", merchant="Acme")
    assert statuses(db, item, "2026-08-20", "2026-09-25")[base_date] == ("paid", txn.id)
    if outside is not None:
        db.delete(txn)
        db.commit()
        add_txn(db, base.chk.id, D(base_date) + dt.timedelta(days=outside), -50, "ACME", merchant="Acme")
        assert statuses(db, item, "2026-08-20", "2026-09-25")[base_date] == ("late", None)


def test_amount_between_half_and_double(base):
    db = base.db
    item = make_item(db, "Power Co", -100, "monthly", "2026-10-10", account=base.chk, last_seen="2026-05-01")
    add_txn(db, base.chk.id, "2026-06-10", -200.01, "x", merchant="Power Co")
    add_txn(db, base.chk.id, "2026-07-10", -49.99, "x", merchant="Power Co")
    t8 = add_txn(db, base.chk.id, "2026-08-10", -200, "x", merchant="Power Co")
    t9 = add_txn(db, base.chk.id, "2026-09-10", -50, "x", merchant="Power Co")
    add_txn(db, base.chk.id, "2026-09-10", 100, "refund", merchant="Power Co")  # other sign
    got = statuses(db, item, "2026-06-01", "2026-09-30")
    assert got == {"2026-06-10": ("late", None), "2026-07-10": ("late", None),
                   "2026-08-10": ("paid", t8.id), "2026-09-10": ("paid", t9.id)}


def test_transfers_and_pending_rows_match(base):
    db = base.db
    item = make_item(db, "Visa Autopay", -500, "monthly", "2026-10-15", account=base.chk, last_seen="2026-07-15")
    t8 = add_txn(db, base.chk.id, "2026-08-15", -480, "x", merchant="Visa Autopay", is_transfer=True,
                 category="TRANSFER_OUT")
    t9 = add_txn(db, base.chk.id, "2026-09-16", -500, "x", merchant="Visa Autopay", pending=True)
    assert statuses(db, item, "2026-08-01", "2026-09-30") == {"2026-08-15": ("paid", t8.id),
                                                               "2026-09-15": ("paid", t9.id)}


def test_each_transaction_once_and_nearest_wins(base):
    db = base.db
    item = make_item(db, "Lunch Club", -50, "weekly", "2026-10-03", account=base.chk, last_seen="2026-08-01")
    t12 = add_txn(db, base.chk.id, "2026-09-12", -50, "x", merchant="Lunch Club")
    add_txn(db, base.chk.id, "2026-09-18", -52, "x", merchant="Lunch Club")  # 1 day off, further in amount
    t20 = add_txn(db, base.chk.id, "2026-09-20", -50.5, "x", merchant="Lunch Club")
    got = statuses(db, item, "2026-09-10", "2026-09-26")
    assert got["2026-09-12"] == ("paid", t12.id)
    assert got["2026-09-19"] == ("paid", t20.id)  # tie on days: nearest amount wins
    # 9-26 is in [today, next_date): detection already consumed it, so it exists only when paid.
    assert "2026-09-26" not in got
    # One transaction, two occurrences near it (one moved): the first in date order takes it.
    db.add(RecurringOverride(recurring_id=item.id, base_date=D("2026-09-19"), moved_to=D("2026-09-13")))
    db.commit()
    got = statuses(db, item, "2026-09-10", "2026-09-26")
    assert got["2026-09-12"] == ("paid", t12.id)
    assert got["2026-09-19"][0] == "late"  # 13 days ago


def test_null_account_matches_any_visible_account(base):
    db = base.db
    hidden = make_account(db, "Old", "bank", 0, hidden=True)
    item = make_item(db, "Gym Co", -40, "monthly", "2026-10-20", last_seen="2026-07-20")
    add_txn(db, hidden.id, "2026-08-20", -40, "x", merchant="Gym Co")
    t9 = add_txn(db, base.savings.id, "2026-09-20", -40, "x", merchant="Gym Co")
    assert statuses(db, item, "2026-08-01", "2026-09-30") == {"2026-08-20": ("late", None),
                                                               "2026-09-20": ("paid", t9.id)}


def test_past_pending_late_and_carry(base):
    client, db = base.client, base.db
    manual = add(client, "Paper", -5, "monthly", "2026-09-20", account_id=base.chk.id)  # never seen: past
    today_item = add(client, "Magazine", -7, "monthly", "2026-09-26", account_id=base.chk.id)
    phone = make_item(db, "Phone", -80, "monthly", "2026-10-24", account=base.chk, last_seen="2026-08-24")
    ins = make_item(db, "Insurance", -200, "monthly", "2026-10-10", account=base.chk, last_seen="2026-08-10")
    lunch = make_item(db, "Lunch", -10, "weekly", "2026-10-03", account=base.chk, last_seen="2026-09-12")
    body = cal(client, start="2026-09-01", end="2026-10-05")
    occ = by_key(body)
    assert occ[f"r{manual['id']}:2026-09-20"]["status"] == "past"
    assert occ[f"r{manual['id']}:2026-09-20"]["counted"] is False
    assert (occ[f"r{phone.id}:2026-09-24"]["status"], occ[f"r{phone.id}:2026-09-24"]["counted_on"],
            occ[f"r{phone.id}:2026-09-24"]["carried"]) == ("pending", "2026-09-27", True)
    assert (occ[f"r{ins.id}:2026-09-10"]["status"], occ[f"r{ins.id}:2026-09-10"]["counted"]) == ("late", False)
    assert occ[f"r{lunch.id}:2026-09-12"]["status"] == "past"  # the last one seen: not late
    assert (occ[f"r{lunch.id}:2026-09-19"]["status"], occ[f"r{lunch.id}:2026-09-19"]["carried"]) == ("late", True)
    assert occ[f"r{lunch.id}:2026-09-05"]["status"] == "past"
    t = occ[f"r{today_item['id']}:2026-09-26"]
    assert (t["status"], t["counted_on"], t["carried"]) == ("upcoming", "2026-09-27", True)
    # 9-26 is before lunch's next_date (10-03): consumed by detection, so it isn't expected.
    assert f"r{lunch.id}:2026-09-26" not in occ
    # Tomorrow: 5000 - 80 (phone) - 10 (lunch 9-19, carried) - 7 (magazine, today's).
    assert day_balances(body)["2026-09-27"] == 4903.0
    assert day_balances(body)["2026-09-25"] is None and day_balances(body)["2026-09-26"] == 5000.0
    # The default range (from today) still lists the carried ones, dated before it.
    keys = set(by_key(cal(client)))
    assert f"r{phone.id}:2026-09-24" in keys and f"r{ins.id}:2026-09-10" not in keys
    # Beyond the carry window (min(14, period)): a weekly one 8 days late isn't carried.
    base.today.set(D("2026-09-27"))
    occ = by_key(cal(client, start="2026-09-01", end="2026-10-05"))
    assert occ[f"r{lunch.id}:2026-09-19"]["carried"] is False
    assert occ[f"r{lunch.id}:2026-09-19"]["counted"] is False


def test_paid_early_is_not_counted_and_consumed_bases(base):
    client, db = base.client, base.db
    water = make_item(db, "Water", -40, "monthly", "2026-10-28", account=base.chk, last_seen="2026-09-25")
    txn = add_txn(db, base.chk.id, "2026-09-25", -41.5, "x", merchant="Water")
    # Detection moved next_date past 9-28 because of the 9-25 payment: the 9-28 base shows as paid.
    occ = by_key(cal(client))
    o = occ[f"r{water.id}:2026-09-28"]
    assert (o["status"], o["counted"], o["actual"]) == ("paid", False, {
        "amount": -41.5, "date": "2026-09-25", "transaction_id": txn.id, "by": "match"})
    assert day_balances(cal(client))["2026-09-28"] == 5000.0
    # Without the payment, a consumed base [today, next_date) doesn't appear at all.
    db.delete(txn)
    db.commit()
    assert f"r{water.id}:2026-09-28" not in by_key(cal(client))


def test_override_paid_and_skipped(base):
    client = base.client
    rent = add(client, "Rent", -1500, "monthly", "2026-10-01", account_id=base.chk.id)
    url = f"/api/recurring/{rent['id']}/occurrences"
    ok(client.put(f"{url}/2026-10-01", json={"paid": True}))
    o = by_key(cal(client))[f"r{rent['id']}:2026-10-01"]
    assert (o["status"], o["counted"], o["actual"], o["movable"]) == (
        "paid", False, {"amount": -1500.0, "date": None, "transaction_id": None, "by": "you"}, False)
    ok(client.put(f"{url}/2026-10-01", json={"paid": True, "paid_amount": 1490.25}))
    o = by_key(cal(client))[f"r{rent['id']}:2026-10-01"]
    assert o["actual"]["amount"] == -1490.25 and o["override"]["paid_amount"] == 1490.25
    ok(client.put(f"{url}/2026-11-01", json={"skipped": True}))
    o = by_key(cal(client, end="2026-11-30"))[f"r{rent['id']}:2026-11-01"]
    assert (o["status"], o["counted"], o["movable"]) == ("skipped", False, False)
    assert day_balances(cal(client, end="2026-11-30"))["2026-11-01"] == 5000.0


# ------------------------------------------------------------------ projection


@pytest.fixture
def forecast(base):
    client, db = base.client, base.db
    add_txn(db, base.chk.id, "2026-06-01", -1000, "Too old")  # history starts before the 90 days
    add_txn(db, base.chk.id, "2026-09-20", -90, "Groceries")
    add_txn(db, base.chk.id, "2026-07-15", -45, "Dinner")  # daily = 135 / 90 = 1.50
    ids = {
        "rent": add(client, "Rent", -1500, "monthly", "2026-10-01", account_id=base.chk.id)["id"],
        "pay": add(client, "Payroll", 2000, "biweekly", "2026-10-02")["id"],
        "netflix": add(client, "Netflix", -15.49, "monthly", "2026-09-28", account_id=base.card.id)["id"],
        "gym": add(client, "Gym", -40, "monthly", "2026-09-29")["id"],
        "saving": add(client, "To savings", -100, "monthly", "2026-09-30", account_id=base.savings.id)["id"],
    }
    ok(client.patch(f"/api/recurring/{ids['gym']}", json={"include_in_forecast": False}))
    return base, ids


def test_calendar_projection(forecast):
    base, ids = forecast
    body = cal(base.client)
    assert body["account"] == {"id": base.chk.id, "name": "Total Checking", "mask": "4417",
                               "institution_name": "Chase"}
    assert (body["today"], body["from"], body["to"], body["horizon_end"]) == (
        "2026-09-26", "2026-09-26", "2026-10-31", "2027-09-30")
    assert (body["balance"], body["threshold"], body["daily_spend"], body["include_daily"]) == (
        5000.0, 2000.0, 1.5, True)
    assert body["low_ahead"] == {"enabled": True, "days": 7} and body["reminders_enabled"] is True
    bal = day_balances(body)
    assert len(bal) == 36
    assert [bal[d] for d in ("2026-09-26", "2026-09-27", "2026-09-28", "2026-09-29", "2026-09-30",
                             "2026-10-01", "2026-10-02")] == [5000.0, 4998.5, 4997.0, 4995.5, 4994.0, 3492.5, 5491.0]
    assert bal["2026-10-31"] == 9447.5
    occ = by_key(body)
    rent = occ[f"r{ids['rent']}:2026-10-01"]
    assert rent == {
        "key": f"r{ids['rent']}:2026-10-01", "series": f"r{ids['rent']}", "recurring_id": ids["rent"],
        "plan_category": None, "category_id": None, "base_date": "2026-10-01", "date": "2026-10-01",
        "name": "Rent", "amount": -1500.0, "kind": "out", "status": "upcoming", "counted": True,
        "counted_on": "2026-10-01", "carried": False, "actual": None, "override": None, "movable": True,
        "next_in_series": True, "reminder_days": 0, "maybe": False,
    }
    assert (occ[f"r{ids['netflix']}:2026-09-28"]["kind"], occ[f"r{ids['netflix']}:2026-09-28"]["counted"]) == (
        "card", False)
    assert occ[f"r{ids['gym']}:2026-09-29"]["counted"] is False
    assert occ[f"r{ids['pay']}:2026-10-16"]["next_in_series"] is False
    assert not any(k.startswith(f"r{ids['saving']}:") for k in occ)  # another bank account: not here
    # Sorted by date, then money in before money out, then name.
    order = [(o["date"], o["name"]) for o in body["occurrences"]]
    assert order[:4] == [("2026-09-28", "Netflix"), ("2026-09-29", "Gym"), ("2026-10-01", "Rent"),
                         ("2026-10-02", "Payroll")]
    series = {s["id"]: s for s in body["series"]}
    assert set(series) == {f"r{ids[k]}" for k in ("rent", "pay", "netflix", "gym")}
    assert series[f"r{ids['netflix']}"]["account"] == {"id": base.card.id, "name": "Visa", "mask": "9911",
                                                       "category": "credit"}
    assert (series[f"r{ids['netflix']}"]["counted"], series[f"r{ids['gym']}"]["counted"],
            series[f"r{ids['rent']}"]["counted"]) == (False, False, True)
    assert series[f"r{ids['pay']}"] | {} == {
        "id": f"r{ids['pay']}", "recurring_id": ids["pay"], "plan_category": None, "name": "Payroll",
        "amount": 2000.0, "cadence": "biweekly", "anchor_days": None, "next_date": "2026-10-02", "kind": "in",
        "source": "manual", "account": None, "counted": True, "reminder_days": 0, "can_move_all": True,
        "category_id": None}
    # Months: September from today, October whole.
    assert body["months"] == [
        {"month": "2026-09", "low": 4994.0, "low_date": "2026-09-30", "end": 4994.0, "end_date": "2026-09-30",
         "first_dip": None},
        {"month": "2026-10", "low": 3492.5, "low_date": "2026-10-01", "end": 9447.5, "end_date": "2026-10-31",
         "first_dip": None},
    ]
    assert body["first_dip"] is None


def test_daily_switch_is_a_server_setting(forecast):
    base, _ids = forecast
    client = base.client
    assert ok(client.get("/api/forecast/settings")) == {"include_daily": True, "excluded_plans": []}
    assert ok(client.patch("/api/forecast/settings", json={"include_daily": False})) == {
        "include_daily": False, "excluded_plans": []}
    body = cal(client)
    assert body["include_daily"] is False and body["daily_spend"] == 1.5
    assert day_balances(body)["2026-09-30"] == 5000.0
    for bad in ({"include_daily": None}, {"excluded_plans": None}, {"excluded_plans": ["NOPE"]},
                {"excluded_plans": ["x" * 65]}, {"excluded_plans": ["TRAVEL"] * 201}, {"other": 1}):
        assert client.patch("/api/forecast/settings", json=bad).status_code == 422, bad
    r = ok(client.patch("/api/forecast/settings", json={"excluded_plans": ["TRAVEL", "TRAVEL", "MEDICAL"]}))
    assert r == {"include_daily": False, "excluded_plans": ["TRAVEL", "MEDICAL"]}


def test_dips_months_and_cause(forecast):
    base, ids = forecast
    client = base.client
    add(client, "Water", -20, "monthly", "2026-10-01", account_id=base.chk.id)
    ok(client.put("/api/alerts/settings/low", json={"value": 4000}))
    body = cal(client)
    dip = {"date": "2026-10-01", "balance": 3472.5, "cause_key": f"r{ids['rent']}:2026-10-01"}
    assert body["first_dip"] == dip
    assert body["months"][0]["first_dip"] is None and body["months"][1]["first_dip"] == dip
    below = [d["date"] for d in body["days"] if d["below"]]
    assert below == ["2026-10-01"]
    # A dip from everyday spending alone has no cause.
    ok(client.put("/api/alerts/settings/low", json={"value": 4999}))
    assert cal(client)["first_dip"] == {"date": "2026-09-27", "balance": 4998.5, "cause_key": None}


def test_from_after_today_still_projects_from_today(forecast):
    base, _ids = forecast
    body = cal(base.client, start="2026-10-01", end="2026-10-02")
    assert [(d["date"], d["balance"]) for d in body["days"]] == [("2026-10-01", 3492.5), ("2026-10-02", 5491.0)]
    assert [m["month"] for m in body["months"]] == ["2026-09", "2026-10"]
    assert body["months"][1]["end"] is None and body["months"][1]["end_date"] == "2026-10-31"
    assert {o["date"] for o in body["occurrences"]} == {"2026-10-01", "2026-10-02"}
    past = cal(base.client, start="2026-09-20", end="2026-09-26")
    assert [d["balance"] for d in past["days"]] == [None] * 6 + [5000.0]
    assert [m["month"] for m in past["months"]] == ["2026-09"]


def test_moves_into_and_out_of_the_window(forecast):
    base, ids = forecast
    client = base.client
    url = f"/api/recurring/{ids['rent']}/occurrences"
    ok(client.put(f"{url}/2026-10-01", json={"moved_to": "2026-11-03"}))  # out of October
    ok(client.put(f"{url}/2026-11-01", json={"moved_to": "2026-10-20"}))  # into October
    body = cal(client)
    occ = by_key(body)
    assert f"r{ids['rent']}:2026-10-01" not in occ
    moved = occ[f"r{ids['rent']}:2026-11-01"]
    assert (moved["date"], moved["base_date"], moved["counted_on"]) == ("2026-10-20", "2026-11-01", "2026-10-20")
    assert moved["override"] == {"base_date": "2026-11-01", "moved_to": "2026-10-20", "skipped": False,
                                 "paid": False, "paid_amount": None}
    bal = day_balances(body)
    assert bal["2026-10-01"] == 4992.5 and bal["2026-10-20"] == bal["2026-10-19"] - 1.5 - 1500
    later = by_key(cal(client, start="2026-11-01", end="2026-11-05"))
    assert later[f"r{ids['rent']}:2026-10-01"]["date"] == "2026-11-03"
    assert f"r{ids['rent']}:2026-11-01" not in later


def test_plans_from_spending(forecast):
    base, _ids = forecast
    client, db = base.client, base.db
    ok(client.patch("/api/categories/TRAVEL", json={"target": {"kind": "by_date", "amount": 1200,
                                                               "date": "2026-10-12"}}))
    ok(client.patch("/api/categories/FOOD_AND_DRINK", json={"target": {"kind": "monthly", "amount": 300}}))
    ok(client.patch("/api/categories/ENTERTAINMENT", json={"target": {"kind": "by_date", "amount": 50,
                                                                      "date": "2026-09-26"}}))
    # A future "bills" target kind (no amount) is not a plan.
    medical = db.get(TxnCategory, "MEDICAL")
    medical.target_kind, medical.target_cents, medical.target_date = "bills", None, D("2026-10-05")
    db.commit()
    body = cal(client)
    plans = [o for o in body["occurrences"] if o["kind"] == "plan"]
    assert [(o["key"], o["name"], o["amount"], o["counted_on"], o["carried"]) for o in plans] == [
        ("p:ENTERTAINMENT:2026-09-26", "Entertainment", -50.0, "2026-09-27", True),
        ("p:TRAVEL:2026-10-12", "Travel", -1200.0, "2026-10-12", False),
    ]
    travel = by_key(body)["p:TRAVEL:2026-10-12"]
    assert travel | {} == {
        "key": "p:TRAVEL:2026-10-12", "series": "p:TRAVEL", "recurring_id": None, "plan_category": "TRAVEL",
        "category_id": "TRAVEL", "base_date": "2026-10-12", "date": "2026-10-12", "name": "Travel",
        "amount": -1200.0, "kind": "plan", "status": "upcoming", "counted": True, "counted_on": "2026-10-12",
        "carried": False, "actual": None, "override": None, "movable": False, "next_in_series": False,
        "reminder_days": 0, "maybe": False}
    series = {s["id"]: s for s in body["series"]}
    assert series["p:TRAVEL"]["source"] == "spending" and series["p:TRAVEL"]["counted"] is True
    before = day_balances(body)
    ok(client.patch("/api/forecast/settings", json={"excluded_plans": ["TRAVEL"]}))
    after = cal(client)
    assert by_key(after)["p:TRAVEL:2026-10-12"]["counted"] is False
    assert {s["id"]: s for s in after["series"]}["p:TRAVEL"]["counted"] is False
    assert day_balances(after)["2026-10-12"] == before["2026-10-12"] + 1200
    # Hidden categories and past dates aren't plans.
    ok(client.patch("/api/categories/ENTERTAINMENT", json={"hidden": True}))
    assert "p:ENTERTAINMENT:2026-09-26" not in by_key(cal(client))
    base.today.set(D("2026-10-13"))
    assert not [o for o in cal(client)["occurrences"] if o["kind"] == "plan"]


def test_calendar_without_an_account(unlocked, db, fixed_today):
    make_account(db, "Savings", "bank", 100, plaid_type="depository", plaid_subtype="savings")
    body = cal(unlocked)
    assert body["account"] is None and body["balance"] == 0.0
    assert body["days"] == body["occurrences"] == body["months"] == body["series"] == body["maybe"] == []
    assert body["first_dip"] is None and body["horizon_end"] == "2027-09-30"


def test_calendar_query_limits(base):
    client = base.client
    assert cal(client, start="2026-07-26", end="2027-10-31")["to"] == "2027-10-31"
    for params, detail in [
        ({"from": "2026-10-02", "to": "2026-10-01"}, "from must be on or before to"),
        ({"from": "2026-07-25"}, "from is too far back"),
        ({"to": "2027-11-01"}, "to is too far ahead"),
    ]:
        r = client.get("/api/forecast/calendar", params=params)
        assert (r.status_code, r.json()["detail"]) == (422, detail), params
    assert client.get("/api/forecast/calendar", params={"from": "soon"}).status_code == 422


# ------------------------------------------------------------------ series changes


def test_create_once_and_reminders(base):
    client = base.client
    once = add(client, "Car tax", -310, "once", "2026-10-05", reminder_days=3)
    assert (once["cadence"], once["start_date"], once["reminder_days"], once["anchor_days"]) == (
        "once", "2026-10-05", 3, None)
    body = cal(client, end="2027-09-30")
    assert [o["date"] for o in body["occurrences"] if o["recurring_id"] == once["id"]] == ["2026-10-05"]
    s = {x["id"]: x for x in body["series"]}[f"r{once['id']}"]
    assert (s["can_move_all"], s["reminder_days"]) == (False, 3)
    # The legacy forecast shows it once too.
    events = ok(client.get("/api/forecast", params={"end": "2027-01-31"}))["events"]
    assert [e["date"] for e in events if e["recurring_id"] == once["id"]] == ["2026-10-05"]
    item_url = f"/api/recurring/{once['id']}"
    assert ok(client.patch(item_url, json={"reminder_days": 1}))["reminder_days"] == 1
    for bad in ({"reminder_days": None}, {"reminder_days": 2}, {"reminder_days": 7}):
        r = client.patch(item_url, json=bad)
        assert r.status_code == 422, bad
    assert client.patch(item_url, json={"reminder_days": None}).json()["detail"] == "reminder_days must not be null"
    assert client.post("/api/recurring", json={"name": "x", "amount": 5, "cadence": "monthly",
                                               "next_date": "2026-10-01", "reminder_days": 2}).status_code == 422
    # Moving next_date before start_date moves the start too (the rule can't start after it).
    moved = ok(client.patch(item_url, json={"next_date": "2026-10-01"}))
    assert (moved["next_date"], moved["start_date"]) == ("2026-10-01", "2026-10-01")


def test_occurrence_override_routes(base):
    client, db = base.client, base.db
    rent = add(client, "Rent", -1500, "monthly", "2026-10-01", account_id=base.chk.id)
    url = f"/api/recurring/{rent['id']}/occurrences"
    for path, body, detail in [
        ("2026-10-02", {"moved_to": "2026-10-05"}, "That date isn't one of Rent's dates."),
        ("2026-09-01", {"skipped": True}, "That date isn't one of Rent's dates."),  # before start_date
        ("2026-10-01", {"moved_to": "2026-09-25"}, "Pick today or a later date."),
        ("2026-10-01", {"moved_to": "2027-10-01"}, "Pick a date within the next 12 months."),
        ("2026-10-01", {"paid_amount": 5}, "paid_amount needs paid: true"),
    ]:
        r = client.put(f"{url}/{path}", json=body)
        assert (r.status_code, r.json()["detail"]) == (422, detail), body
    for body in ({"paid": True, "paid_amount": 0}, {"skipped": None}, {"moved_to": "x"}, {"extra": 1},
                 {"paid": True, "paid_amount": 1e13}):
        assert client.put(f"{url}/2026-10-01", json=body).status_code == 422, body
    assert client.put(f"{url}/not-a-date", json={}).status_code == 422
    first = ok(client.put(f"{url}/2026-10-01", json={"moved_to": "2026-09-26"}))
    ov = {"base_date": "2026-10-01", "moved_to": "2026-09-26", "skipped": False, "paid": False, "paid_amount": None}
    assert first == {"override": ov, "previous": None}
    second = ok(client.put(f"{url}/2026-10-01", json={"moved_to": "2026-09-26", "paid": True, "paid_amount": 1490}))
    assert second == {"override": {**ov, "paid": True, "paid_amount": 1490.0}, "previous": ov}
    # Undo = PUT previous.
    assert ok(client.put(f"{url}/2026-10-01", json={k: v for k, v in ov.items() if k != "base_date"}))[
        "override"] == ov
    # moved_to == base is no move; an all-default body deletes the row.
    cleared = ok(client.put(f"{url}/2026-10-01", json={"moved_to": "2026-10-01"}))
    assert cleared == {"override": None, "previous": ov}
    assert db.scalar(select(RecurringOverride.id).where(RecurringOverride.recurring_id == rent["id"])) is None
    assert ok(client.put(f"{url}/2026-10-01", json={})) == {"override": None, "previous": None}
    # A moved one that is now in the past keeps its date when marked paid.
    ok(client.put(f"{url}/2026-10-01", json={"moved_to": "2026-09-28"}))
    base.today.set(D("2026-10-03"))
    assert ok(client.put(f"{url}/2026-10-01", json={"moved_to": "2026-09-28", "paid": True}))["override"][
        "paid"] is True
    assert client.put(f"{url}/2026-10-01", json={"moved_to": "2026-09-29"}).status_code == 422
    # DELETE is idempotent; unknown items are 404.
    assert client.delete(f"{url}/2026-10-01").status_code == 204
    assert client.delete(f"{url}/2026-10-01").status_code == 204
    assert client.delete("/api/recurring/999/occurrences/2026-10-01").status_code == 404
    assert client.put("/api/recurring/999/occurrences/2026-10-01", json={}).status_code == 404
    # Only active items.
    ok(client.patch(f"/api/recurring/{rent['id']}", json={"status": "dismissed"}))
    r = client.put(f"{url}/2026-11-01", json={"skipped": True})
    assert (r.status_code, r.json()["detail"]) == (422, "Only items on your calendar can be changed.")
    # Overrides go with the item.
    ok(client.patch(f"/api/recurring/{rent['id']}", json={"status": "active"}))
    ok(client.put(f"{url}/2026-11-01", json={"skipped": True}))
    assert client.delete(f"/api/recurring/{rent['id']}").status_code == 204
    db.expire_all()
    assert db.scalar(select(RecurringOverride.id)) is None


def test_reschedule_monthly_and_undo(base):
    client, db = base.client, base.db
    rent = add(client, "Rent", -1500, "monthly", "2026-10-01", account_id=base.chk.id)
    url = f"/api/recurring/{rent['id']}"
    ok(client.put(f"{url}/occurrences/2026-11-01", json={"skipped": True}))
    ok(client.put(f"{url}/occurrences/2026-10-01", json={"moved_to": "2026-10-03"}))
    assert by_key(cal(client))[f"r{rent['id']}:2026-10-01"]["next_in_series"] is True
    for body, detail in [
        ({"from_base": "2026-11-01", "to": "2026-11-03"}, "Only the next one can move every future one."),
        ({"from_base": "2026-10-01", "to": "2026-09-25"}, "Pick today or a later date."),
        ({"from_base": "2026-10-01", "to": "2027-10-01"}, "Pick a date within the next 12 months."),
    ]:
        r = client.post(f"{url}/reschedule", json=body)
        assert (r.status_code, r.json()["detail"]) == (422, detail), body
    before = ok(client.get(f"{url}/snapshot"))
    r = ok(client.post(f"{url}/reschedule", json={"from_base": "2026-10-01", "to": "2026-10-03"}))
    assert r["undo"] == before
    assert (r["item"]["next_date"], r["item"]["anchor_days"]) == ("2026-10-03", [3])
    assert ok(client.get(f"{url}/snapshot"))["overrides"] == []  # future overrides went
    dates = [o["date"] for o in cal(client, end="2026-12-31")["occurrences"] if o["recurring_id"] == rent["id"]]
    assert dates == ["2026-10-03", "2026-11-03", "2026-12-03"]
    # Undo: restore the snapshot (same id, month_days and overrides).
    assert ok(client.post("/api/recurring/restore", json=r["undo"]))["next_date"] == "2026-10-01"
    assert ok(client.get(f"{url}/snapshot")) == before
    assert db.scalar(select(RecurringItem.month_days).where(RecurringItem.id == rent["id"])) == "[1]"


def test_reschedule_keeps_month_end_and_weekly_and_refusals(base):
    client = base.client
    eom = add(client, "Loan", -300, "monthly", "2026-10-31", account_id=base.chk.id)
    ok(client.patch(f"/api/recurring/{eom['id']}", json={"next_date": "2026-10-31"}))
    r = ok(client.post(f"/api/recurring/{eom['id']}/reschedule", json={"from_base": "2026-10-31",
                                                                        "to": "2026-11-30"}))
    assert r["item"]["anchor_days"] == [31]
    r = ok(client.post(f"/api/recurring/{eom['id']}/reschedule", json={"from_base": "2026-11-30",
                                                                        "to": "2026-12-29"}))
    assert r["item"]["anchor_days"] == [29]
    weekly = add(client, "Cleaner", -60, "weekly", "2026-09-28", account_id=base.chk.id)
    ok(client.post(f"/api/recurring/{weekly['id']}/reschedule", json={"from_base": "2026-09-28",
                                                                       "to": "2026-09-30"}))
    dates = [o["date"] for o in cal(client, end="2026-10-14")["occurrences"] if o["recurring_id"] == weekly["id"]]
    assert dates == ["2026-09-30", "2026-10-07", "2026-10-14"]
    detail = "This one can only be moved one at a time. Use Edit details to change its usual day."
    for cadence in ("once", "semimonthly"):
        item = add(client, cadence, -5, cadence, "2026-10-01")
        r = client.post(f"/api/recurring/{item['id']}/reschedule", json={"from_base": "2026-10-01",
                                                                         "to": "2026-10-02"})
        assert (r.status_code, r.json()["detail"]) == (422, detail)
        assert {s["id"]: s for s in cal(client)["series"]}[f"r{item['id']}"]["can_move_all"] is False
    assert client.post("/api/recurring/999/reschedule", json={"from_base": "2026-10-01",
                                                              "to": "2026-10-02"}).status_code == 404


def test_snapshot_and_restore(base):
    client, db = base.client, base.db
    rent = add(client, "Rent", -1500, "monthly", "2026-10-01", account_id=base.chk.id,
               category_id="RENT_AND_UTILITIES", reminder_days=3)
    url = f"/api/recurring/{rent['id']}"
    ok(client.put(f"{url}/occurrences/2026-11-01", json={"skipped": True}))
    ok(client.put(f"{url}/occurrences/2026-12-01", json={"paid": True, "paid_amount": 1450}))
    snap = ok(client.get(f"{url}/snapshot"))
    assert snap["item"]["merchant_key"] == "rent" and snap["item"]["created_at"].endswith("Z")
    assert snap["item"]["category_name"] == "Rent and utilities" and snap["item"]["reminder_days"] == 3
    assert [o["base_date"] for o in snap["overrides"]] == ["2026-11-01", "2026-12-01"]
    assert client.get("/api/recurring/999/snapshot").status_code == 404
    # Delete, then Undo: the same id comes back with its overrides (201).
    assert client.delete(url).status_code == 204
    restored = ok(client.post("/api/recurring/restore", json=snap), 201)
    assert restored["id"] == rent["id"]
    assert ok(client.get(f"{url}/snapshot")) == snap
    assert db.scalar(select(RecurringOverride.paid_amount_cents).where(
        RecurringOverride.recurring_id == rent["id"], RecurringOverride.paid.is_(True))) == -145000
    # In place (200).
    ok(client.patch(url, json={"amount": -1600, "name": "Rent!"}))
    assert ok(client.post("/api/recurring/restore", json=snap))["amount"] == -1500.0
    assert ok(client.get(f"{url}/snapshot")) == snap
    item = snap["item"]
    bad = [
        {**snap, "item": {**item, "anchor_days": [1, 15]}},
        {**snap, "item": {**item, "cadence": "weekly"}},  # anchor [1] doesn't fit weekly
        {**snap, "item": {**item, "cadence": "fortnightly"}},
        {**snap, "item": {**item, "status": "paused"}},
        {**snap, "item": {**item, "amount": 0}},
        {**snap, "item": {**item, "id": 0}},
        {**snap, "item": {**item, "id": 2**63}},
        {**snap, "item": {**item, "merchant_key": ""}},
        {**snap, "item": {**item, "merchant_key": "x" * 121}},
        {**snap, "item": {**item, "extra": 1}},
        {**snap, "overrides": snap["overrides"] + snap["overrides"][:1]},
        {**snap, "overrides": [{"base_date": "2026-11-01", "skipped": True}] * 501},
        {**snap, "overrides": [{"base_date": "2026-11-01", "paid_amount": 5}]},
    ]
    for body in bad:
        assert client.post("/api/recurring/restore", json=body).status_code == 422, body
    # A deleted account or category comes back as null.
    gone = {**snap, "item": {**item, "id": 4242, "account_id": 999, "category_id": "c_999"}}
    back = ok(client.post("/api/recurring/restore", json=gone), 201)
    assert (back["account_id"], back["category_id"]) == (None, None)


def test_candidates(base):
    client, db = base.client, base.db
    for m in (5, 6, 7, 8, 9):
        add_txn(db, base.card.id, f"2026-0{m}-12", -15.49, "NETFLIX", merchant="Netflix", category="ENTERTAINMENT")
    for day in ("2026-07-20", "2026-08-20", "2026-09-20"):
        add_txn(db, base.chk.id, day, -60, "Gym", merchant="Gym Co")
        add_txn(db, base.savings.id, day, -25, "Fee", merchant="Savings Fee", category="BANK_FEES")
    for day in ("2026-09-05", "2026-09-12", "2026-09-19"):
        add_txn(db, base.chk.id, day, -9, "Car wash", merchant="Suds")
    assert ok(client.post("/api/recurring/detect", json={})) == {"suggested": 4}
    got = ok(client.get("/api/recurring/candidates"))
    assert [c["item"]["name"] for c in got] == ["Netflix", "Gym Co", "Savings Fee", "Suds"]
    netflix, gym, fee, suds = got
    assert {k: netflix[k] for k in ("paid_with", "on_calendar", "day_of_month", "weekday", "months_seen",
                                    "count", "last_date")} == {
        "paid_with": "card", "on_calendar": True, "day_of_month": 12, "weekday": None,
        "months_seen": ["2026-06", "2026-07", "2026-08", "2026-09"], "count": 5, "last_date": "2026-09-12"}
    assert netflix["account"] == {"id": base.card.id, "name": "Visa", "mask": "9911", "category": "credit"}
    assert netflix["item"]["category_id"] == "ENTERTAINMENT"
    assert (gym["paid_with"], gym["on_calendar"], gym["count"], gym["day_of_month"]) == ("checking", True, 3, 20)
    assert (fee["paid_with"], fee["on_calendar"]) == ("other", False)
    assert (suds["weekday"], suds["day_of_month"]) == (6, None)  # Saturdays (0 = Sunday)
    ok(client.patch(f"/api/recurring/{gym['item']['id']}", json={"status": "active"}))
    assert [c["item"]["name"] for c in ok(client.get("/api/recurring/candidates"))] == [
        "Netflix", "Savings Fee", "Suds"]


def test_detection_skips_once_items(base):
    client, db = base.client, base.db
    once = add(client, "Acme", -30, "once", "2026-10-05", account_id=base.chk.id)
    for day in ("2026-06-05", "2026-07-05", "2026-08-05", "2026-09-05"):
        add_txn(db, base.chk.id, day, -30, "ACME", merchant="Acme")
    assert ok(client.post("/api/recurring/detect", json={})) == {"suggested": 0}
    item = next(i for i in ok(client.get("/api/recurring")) if i["id"] == once["id"])
    assert (item["next_date"], item["last_seen_date"], item["cadence"]) == ("2026-10-05", None, "once")


# ------------------------------------------------------------------ budget categories (addendum)


def test_category_auto_fill_rules(base):
    db = base.db
    db.get(TxnCategory, "MEDICAL").hidden = True
    db.commit()
    cmap = load_categories(db)

    def rows(*cats):
        return [SimpleNamespace(id=i, date=D("2026-09-01") + dt.timedelta(days=i), category=c)
                for i, c in enumerate(cats)]

    assert category_from_rows(rows("OTHER", "OTHER", None, "TRAVEL"), cmap) == "TRAVEL"
    assert category_from_rows(rows("TRANSFER_OUT", "TRANSFER_OUT", "MEDICAL", "NOPE"), cmap) is None
    assert category_from_rows(rows("TRAVEL", "FOOD_AND_DRINK", "FOOD_AND_DRINK", "TRAVEL"), cmap) == "TRAVEL"  # tie: latest
    assert category_from_rows(rows("LOAN_PAYMENTS", "INCOME", "LOAN_PAYMENTS"), cmap) == "LOAN_PAYMENTS"


def test_category_on_confirm_manual_set_clear_and_guess(base):
    client, db = base.client, base.db
    for m in (6, 7, 8, 9):
        add_txn(db, base.chk.id, f"2026-0{m}-03", -2210.44, "ROCKET MORTGAGE PMT", category="LOAN_PAYMENTS")
        add_txn(db, base.chk.id, f"2026-0{m}-08", -120, "City Power", merchant="City Power",
                category="RENT_AND_UTILITIES")
    # Confirmed from a suggestion: filled from its transactions (unless the PATCH sets one).
    sugg = make_item(db, "City Power", -120, "monthly", "2026-10-08", account=base.chk, status="suggested",
                     last_seen="2026-09-08")
    confirmed = ok(client.patch(f"/api/recurring/{sugg.id}", json={"status": "active"}))
    assert (confirmed["category_id"], confirmed["category_name"]) == ("RENT_AND_UTILITIES", "Rent and utilities")
    sugg2 = make_item(db, "City Power", -120, "monthly", "2026-10-08", status="suggested")
    assert ok(client.patch(f"/api/recurring/{sugg2.id}", json={"status": "active", "category_id": None}))[
        "category_id"] is None
    # Manual: omitted = guessed from the merchant's history; null = none; unknown or transfer = 422.
    guess = "/api/recurring/category-guess"
    assert ok(client.post(guess, json={"name": "Rocket Mortgage", "amount_sign": -1})) == {
        "category_id": "LOAN_PAYMENTS", "category_name": "Loan payments"}
    assert ok(client.post(guess, json={"name": "Rocket Mortgage", "amount_sign": 1}))["category_id"] is None
    assert ok(client.post(guess, json={"name": "Rocket Mortgage", "amount_sign": -1,
                                       "account_id": base.card.id}))["category_id"] is None
    assert ok(client.post(guess, json={"name": "Nobody"}))["category_id"] is None
    # A POST body: the typed name never goes in a URL (the old GET is gone).
    assert client.get(guess, params={"name": "Rocket Mortgage"}).status_code in (404, 405)
    for body in ({"name": ""}, {"name": "x", "amount_sign": 5}, {"name": "x", "amount": -5}, {"name": "x" * 201},
                 {"name": "x", "account_id": 0}):
        assert client.post(guess, json=body).status_code == 422, body
    guessed = add(client, "Rocket Mortgage", -2210.44, "monthly", "2026-10-03")
    assert guessed["category_id"] == "LOAN_PAYMENTS"
    none = add(client, "Rocket Mortgage", -2210.44, "monthly", "2026-10-03", category_id=None)
    assert (none["category_id"], none["category_name"]) == (None, None)
    for cid in ("NOPE", "TRANSFER_OUT"):
        r = client.post("/api/recurring", json={"name": "x", "amount": -5, "cadence": "monthly",
                                                "next_date": "2026-10-01", "category_id": cid})
        assert (r.status_code, r.json()["detail"]) == (422, "unknown category")
        r = client.patch(f"/api/recurring/{none['id']}", json={"category_id": cid})
        assert (r.status_code, r.json()["detail"]) == (422, "unknown category")
    set_ = ok(client.patch(f"/api/recurring/{none['id']}", json={"category_id": "INCOME"}))
    assert (set_["category_id"], set_["category_name"]) == ("INCOME", "Income")
    assert ok(client.patch(f"/api/recurring/{none['id']}", json={"category_id": None}))["category_id"] is None
    listed = {i["id"]: i for i in ok(client.get("/api/recurring"))}
    assert listed[guessed["id"]]["category_name"] == "Loan payments"
    # The calendar carries it too.
    occ = next(o for o in cal(client)["occurrences"] if o["recurring_id"] == guessed["id"])
    assert occ["category_id"] == "LOAN_PAYMENTS"


def test_deleting_a_category_unlinks_its_bills(base):
    client, db = base.client, base.db
    kids = ok(client.post("/api/categories", json={"name": "Kids", "kind": "spending"}), 201)
    item = add(client, "Daycare", -900, "monthly", "2026-10-01", category_id=kids["id"])
    assert item["category_name"] == "Kids"
    assert client.delete(f"/api/categories/{kids['id']}").status_code == 204
    db.expire_all()
    assert db.get(RecurringItem, item["id"]).category_id is None
    assert next(i for i in ok(client.get("/api/recurring")) if i["id"] == item["id"])["category_id"] is None


def test_bill_occurrences_single_source_of_truth(base):
    client, db = base.client, base.db
    electric = make_item(db, "Electric", -120, "monthly", "2026-10-15", account=base.chk,
                         last_seen="2026-09-14", category="RENT_AND_UTILITIES")
    paid = add_txn(db, base.chk.id, "2026-09-14", -131.5, "x", merchant="Electric")
    water = add(client, "Water", -40, "monthly", "2026-09-28", category_id="RENT_AND_UTILITIES")
    internet = add(client, "Internet", -70, "monthly", "2026-10-02", category_id="GENERAL_SERVICES")
    ok(client.put(f"/api/recurring/{internet['id']}/occurrences/2026-10-02", json={"moved_to": "2026-09-30"}))
    gym = add(client, "Gym", -30, "monthly", "2026-09-29", category_id="PERSONAL_CARE")
    ok(client.put(f"/api/recurring/{gym['id']}/occurrences/2026-09-29", json={"skipped": True}))
    netflix = add(client, "Netflix", -15.49, "monthly", "2026-10-10", account_id=base.card.id,
                  category_id="ENTERTAINMENT")
    pay = add(client, "Payroll", 2000, "biweekly", "2026-10-02", category_id="INCOME")
    add(client, "No category", -10, "monthly", "2026-09-28", category_id=None)
    add(client, "Elsewhere", -25, "monthly", "2026-09-28", account_id=base.savings.id,
        category_id="BANK_FEES")  # not on the calendar, still a bill of its category
    db.expire_all()

    def summary(month_start, month_end):
        got = calendar.bill_occurrences(db, TODAY, D(month_start), D(month_end))
        return {cid: [(o.name, o.date.isoformat(), o.status, o.amount_cents, o.actual_cents, o.base_date.isoformat())
                      for o in occs] for cid, occs in got.items()}

    assert summary("2026-09-01", "2026-09-30") == {
        "RENT_AND_UTILITIES": [("Electric", "2026-09-15", "paid", -12000, -13150, "2026-09-15"),
                               ("Water", "2026-09-28", "upcoming", -4000, None, "2026-09-28")],
        "GENERAL_SERVICES": [("Internet", "2026-09-30", "upcoming", -7000, None, "2026-10-02")],
        "PERSONAL_CARE": [("Gym", "2026-09-29", "skipped", -3000, None, "2026-09-29")],
        "BANK_FEES": [("Elsewhere", "2026-09-28", "upcoming", -2500, None, "2026-09-28")],
    }
    october = summary("2026-10-01", "2026-10-31")
    assert october["ENTERTAINMENT"] == [("Netflix", "2026-10-10", "upcoming", -1549, None, "2026-10-10")]
    assert october["RENT_AND_UTILITIES"] == [("Electric", "2026-10-15", "upcoming", -12000, None, "2026-10-15"),
                                             ("Water", "2026-10-28", "upcoming", -4000, None, "2026-10-28")]
    assert "GENERAL_SERVICES" not in october  # its October one moved into September
    assert "INCOME" not in october
    income = calendar.income_occurrences(db, TODAY, D("2026-10-01"), D("2026-10-31"))
    assert [(o.name, o.date.isoformat()) for o in income] == [("Payroll", "2026-10-02"), ("Payroll", "2026-10-16"),
                                                              ("Payroll", "2026-10-30")]
    # The calendar agrees (same generator): Electric's September payment and status.
    occ = by_key(cal(client, start="2026-09-01", end="2026-09-30"))
    assert occ[f"r{electric.id}:2026-09-15"]["actual"]["transaction_id"] == paid.id
    assert occ[f"r{gym['id']}:2026-09-29"]["status"] == "skipped"
    assert pay["category_id"] == "INCOME" and netflix["category_id"] == "ENTERTAINMENT"
    assert water["category_name"] == "Rent and utilities"


# ------------------------------------------------------------------ alerts


def run(db, day, **kw):
    db.expire_all()
    created = evaluate(db, day, **kw)
    db.commit()
    return created


def keyed(created, key):
    return [e for e in created if e.key == key]


def test_low_ahead_alert(base):
    client, db = base.client, base.db
    base.chk.current_balance_cents = 250000
    db.commit()
    mortgage = add(client, "Rocket Mortgage", -1000, "monthly", "2026-09-30", account_id=base.chk.id)
    created = keyed(run(db, TODAY), "low_ahead")
    assert [(e.severity, e.title, e.body, e.dedupe_key) for e in created] == [(
        "warn", "Total Checking may drop below $2,000 on Sep 30",
        "Projected $1,500 after Rocket Mortgage. Open Bills and paychecks to move a bill or money.",
        f"low_ahead:{base.chk.id}:r{mortgage['id']}:2026-09-30@2026-09-30")]
    assert json.loads(created[0].data)["cause_key"] == f"r{mortgage['id']}:2026-09-30"
    assert keyed(run(db, TODAY), "low_ahead") == []  # once per cause (the bill and its date)
    # Only N days ahead.
    ok(client.put(f"/api/recurring/{mortgage['id']}/occurrences/2026-09-30", json={"moved_to": "2026-10-04"}))
    assert keyed(run(db, TODAY), "low_ahead") == []  # 8 days away
    ok(client.put("/api/alerts/settings/low_ahead", json={"value": 8}))
    assert [e.dedupe_key for e in keyed(run(db, TODAY), "low_ahead")] == [
        f"low_ahead:{base.chk.id}:r{mortgage['id']}:2026-09-30@2026-10-04"]  # moved: warns again
    # Respects enabled; bad values.
    ok(client.put(f"/api/recurring/{mortgage['id']}/occurrences/2026-09-30", json={"moved_to": "2026-10-03"}))
    ok(client.put("/api/alerts/settings/low_ahead", json={"enabled": False}))
    assert keyed(run(db, TODAY), "low_ahead") == []
    for value in (32, 0, 2.5, -1):
        assert client.put("/api/alerts/settings/low_ahead", json={"value": value}).status_code == 422, value
    assert client.put("/api/alerts/settings/low_ahead", json={"value": 31}).status_code == 200
    # Already below today: that's the "low" alert's job.
    ok(client.put("/api/alerts/settings/low_ahead", json={"enabled": True}))
    base.chk.current_balance_cents = 150000
    db.commit()
    created = run(db, TODAY)
    assert keyed(created, "low_ahead") == [] and len(keyed(created, "low")) == 1


def test_low_ahead_honors_daily_and_plan_exclusions(base):
    client, db = base.client, base.db
    base.chk.current_balance_cents = 201000
    db.commit()
    add_txn(db, base.chk.id, "2026-06-01", -1000, "Too old")
    add_txn(db, base.chk.id, "2026-09-20", -135, "Groceries")  # 1.50/day: below $2,000 on day 7
    assert [e.title for e in keyed(run(db, TODAY), "low_ahead")] == [
        "Total Checking may drop below $2,000 on Oct 3"]
    ok(client.patch("/api/forecast/settings", json={"include_daily": False}))
    ok(client.patch("/api/categories/TRAVEL", json={"target": {"kind": "by_date", "amount": 500,
                                                               "date": "2026-09-29"}}))
    assert [e.body for e in keyed(run(db, TODAY), "low_ahead")] == [
        "Projected $1,510 after Travel. Open Bills and paychecks to move a bill or money."]
    ok(client.patch("/api/categories/TRAVEL", json={"target": {"kind": "by_date", "amount": 500,
                                                               "date": "2026-09-30"}}))
    ok(client.patch("/api/forecast/settings", json={"excluded_plans": ["TRAVEL"]}))
    assert keyed(run(db, TODAY), "low_ahead") == []


def test_reminder_alert(base):
    client, db = base.client, base.db
    bill = add(client, "Rocket Mortgage", -2210.44, "monthly", "2026-09-29", account_id=base.chk.id, reminder_days=3)
    pay = add(client, "Payroll", 1500, "monthly", "2026-09-27", reminder_days=1)
    card = add(client, "Netflix", -15.49, "monthly", "2026-09-28", account_id=base.card.id, reminder_days=3)
    add(client, "Quiet", -5, "monthly", "2026-09-27")  # no reminder
    add(client, "Soon", -5, "monthly", "2026-09-29", reminder_days=1)  # 3 days away: not yet
    created = keyed(run(db, TODAY), "reminder")
    assert sorted((e.severity, e.title, e.body, e.dedupe_key) for e in created) == sorted([
        ("accent", "Rocket Mortgage is due in 3 days", "$2,210.44 from Total Checking on Tue, Sep 29.",
         f"reminder:{bill['id']}:2026-09-29:2026-09-29"),
        ("accent", "Payroll is expected tomorrow", "$1,500 to Total Checking on Sun, Sep 27.",
         f"reminder:{pay['id']}:2026-09-27:2026-09-27"),
        ("accent", "Netflix is due in 2 days", "$15.49 on Visa on Mon, Sep 28.",
         f"reminder:{card['id']}:2026-09-28:2026-09-28"),
    ])
    assert keyed(run(db, TODAY), "reminder") == []
    assert keyed(run(db, D("2026-09-27")), "reminder") == []  # still the same occurrence and date
    # Moved: reminded again for the new date.
    base.today.set(D("2026-09-27"))
    ok(client.put(f"/api/recurring/{bill['id']}/occurrences/2026-09-29", json={"moved_to": "2026-09-30"}))
    assert [e.title for e in keyed(run(db, D("2026-09-27")), "reminder")] == ["Rocket Mortgage is due in 3 days"]
    # Due today, then not after it's paid or skipped.
    assert [e.title for e in keyed(run(db, D("2026-09-29")), "reminder")] == ["Soon is due today"]
    ok(client.put(f"/api/recurring/{bill['id']}/occurrences/2026-09-29", json={"moved_to": "2026-10-01",
                                                                             "paid": True}))
    ok(client.put(f"/api/recurring/{pay['id']}/occurrences/2026-10-27", json={"skipped": True}))
    assert keyed(run(db, D("2026-09-30")), "reminder") == []
    # October 26: the mortgage's and Netflix's next ones, but not Payroll's skipped 10-27.
    assert sorted(e.title for e in keyed(run(db, D("2026-10-26")), "reminder")) == [
        "Netflix is due in 2 days", "Rocket Mortgage is due in 3 days"]
    ok(client.put("/api/alerts/settings/reminder", json={"enabled": False}))
    ok(client.patch(f"/api/recurring/{bill['id']}", json={"reminder_days": 3}))
    assert keyed(run(db, D("2026-10-29")), "reminder") == []


def test_alert_order_and_only(base):
    assert ALERT_ORDER[-2:] == ("low_ahead", "reminder")
    assert [s["key"] for s in ok(base.client.get("/api/alerts/settings"))][-2:] == ["low_ahead", "reminder"]
    db = base.db
    base.chk.current_balance_cents = 100  # "low" would fire
    db.commit()
    assert run(db, TODAY, only=("low_ahead", "reminder")) == []
    assert keyed(run(db, TODAY), "low")


def test_alerts_tick(app, base, vault):
    state = app.state.fintrack
    base.chk.current_balance_cents = 250000
    base.db.commit()
    add(base.client, "Rocket Mortgage", -1000, "monthly", "2026-09-30", account_id=base.chk.id)
    assert automation.alerts_tick(state, now=10_000.0) is True
    assert automation.alerts_tick(state, now=10_000.0 + 3599) is False  # hourly
    titles = [e["title"] for e in ok(base.client.get("/api/alerts/events"))]
    assert titles == ["Total Checking may drop below $2,000 on Sep 30"]
    base.client.post("/api/auth/lock")
    assert automation.alerts_tick(state, now=10_000.0 + 7200) is False  # locked: nothing


# ------------------------------------------------------------------ migration v6


def build_v5_vault(settings, monkeypatch) -> bytearray:
    key = build_v4_vault(settings, monkeypatch)
    monkeypatch.setattr(migrations, "LATEST", 5)
    database = dbmod.Database(settings.db_path)
    database.open(bytearray(key))
    database.close()
    monkeypatch.setattr(migrations, "LATEST", REAL_LATEST)
    conn = raw(settings, key)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        assert "category_id" not in columns(conn, "recurring_items")
        rows = [
            (7, "Blue Bottle", "blue bottle", 1, -450, "monthly", "2026-10-01", "active", "[1]"),
            (8, "Mystery", "mystery", 1, -100, "monthly", "2026-10-04", "active", "[4]"),
            (9, "Blue Bottle", "blue bottle", None, -450, "monthly", "2026-10-01", "dismissed", "[1]"),
        ]
        for row in rows:
            conn.execute("INSERT INTO recurring_items (id, name, merchant_key, account_id, amount_cents, cadence, "
                         "next_date, status, include_in_forecast, source, month_days, created_at) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'detected', ?, '2026-09-01 00:00:00')", row)
        conn.commit()
    finally:
        conn.close()
    return key


def test_v5_vault_upgrades_to_v6(settings, client, monkeypatch, fixed_today):
    key = build_v5_vault(settings, monkeypatch)
    assert client.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200
    items = {i["id"]: i for i in ok(client.get("/api/recurring"))}
    # Existing active items got a category from their transactions (Blue Bottle: Food and drink).
    assert (items[7]["category_id"], items[7]["category_name"]) == ("FOOD_AND_DRINK", "Food and drink")
    assert items[8]["category_id"] is None and items[9]["category_id"] is None
    assert (items[7]["reminder_days"], items[7]["start_date"], items[7]["anchor_days"]) == (0, None, [1])
    # Once: clearing it isn't undone by the next unlock.
    ok(client.patch("/api/recurring/7", json={"category_id": None}))
    client.post("/api/auth/lock")
    assert client.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200
    assert next(i for i in ok(client.get("/api/recurring")) if i["id"] == 7)["category_id"] is None
    assert cal(client)["account"]["id"] == 1
    client.post("/api/auth/lock")
    conn = raw(settings, key)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == migrations.LATEST == 8
        cols = columns(conn, "recurring_items")
        assert cols["reminder_days"][0] == 1 and cols["start_date"] == (0, None) and cols["category_id"] == (0, None)
        assert set(columns(conn, "recurring_overrides")) == {
            "id", "recurring_id", "base_date", "moved_to", "skipped", "paid", "paid_amount_cents", "created_at"}
        assert dict(conn.execute("SELECT \"key\", value FROM alert_settings WHERE \"key\" IN "
                                 "('low_ahead', 'reminder')").fetchall()) == {"low_ahead": 7.0, "reminder": None}
        assert conn.execute("SELECT count(*) FROM app_settings WHERE \"key\" = 'recurring_category_backfill'"
                            ).fetchone()[0] == 0
        fks = conn.execute("PRAGMA foreign_key_list(recurring_items)").fetchall()
        assert any(f[2] == "categories" and f[3] == "category_id" and f[6] == "SET NULL" for f in fks)
        fks = conn.execute("PRAGMA foreign_key_list(recurring_overrides)").fetchall()
        assert any(f[2] == "recurring_items" and f[6] == "CASCADE" for f in fks)
    finally:
        conn.close()
    names = [p.name for p in settings.data_dir.iterdir() if p.name.startswith("pre-migrate-")]
    assert any(n.startswith("pre-migrate-v5-") for n in names)  # the safety copy was made


def test_failed_v6_migration_rolls_back_to_v5(settings, monkeypatch):
    key = build_v5_vault(settings, monkeypatch)

    def broken(conn):
        conn.execute("ALTER TABLE recurring_items ADD COLUMN reminder_days INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE nope ADD COLUMN x TEXT")

    monkeypatch.setitem(migrations.MIGRATIONS, 6, broken)
    database = dbmod.Database(settings.db_path)
    with pytest.raises(Exception):  # noqa: B017 - sqlcipher3.OperationalError
        database.open(bytearray(key))
    conn = raw(settings, key)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        assert "reminder_days" not in columns(conn, "recurring_items")
    finally:
        conn.close()


def test_fresh_vault_has_no_backfill_flag(unlocked, db):
    assert db.get(AppSetting, "recurring_category_backfill") is None


# ------------------------------------------------------------------ security and performance


def test_routes_need_session_and_csrf(base, app):
    client = base.client
    rent = add(client, "Rent", -1500, "monthly", "2026-10-01")
    snap = ok(client.get(f"/api/recurring/{rent['id']}/snapshot"))
    no_csrf = SessionClient(app, base_url=BASE_URL, headers={k: v for k, v in client.headers.items()
                                                             if k.lower() != "x-fintrack"})
    assert no_csrf.put(f"/api/recurring/{rent['id']}/occurrences/2026-10-01", json={}).status_code == 403
    assert no_csrf.delete(f"/api/recurring/{rent['id']}/occurrences/2026-10-01").status_code == 403
    assert no_csrf.post(f"/api/recurring/{rent['id']}/reschedule",
                        json={"from_base": "2026-10-01", "to": "2026-10-02"}).status_code == 403
    assert no_csrf.post("/api/recurring/restore", json=snap).status_code == 403
    assert no_csrf.post("/api/recurring/category-guess", json={"name": "x"}).status_code == 403
    assert no_csrf.patch("/api/forecast/settings", json={}).status_code == 403
    # NaN / Infinity are refused.
    r = client.put(f"/api/recurring/{rent['id']}/occurrences/2026-10-01",
                   content=b'{"paid": true, "paid_amount": NaN}', headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    r = client.post("/api/recurring", content=b'{"name": "x", "amount": Infinity, "cadence": "monthly", '
                                              b'"next_date": "2026-10-01"}',
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    client.post("/api/auth/lock")
    for method, path in [("get", "/api/forecast/calendar"), ("get", "/api/forecast/settings"),
                         ("get", "/api/recurring/candidates"), ("get", f"/api/recurring/{rent['id']}/snapshot")]:
        assert getattr(client, method)(path).status_code == 401, path
    assert client.post("/api/recurring/restore", json=snap).status_code == 401
    assert client.post("/api/recurring/category-guess", json={"name": "x"}).status_code == 401
    assert client.patch("/api/forecast/settings", json={}).status_code == 401


def test_full_horizon_is_fast(base):
    client, db = base.client, base.db
    cadences = ("weekly", "biweekly", "semimonthly", "monthly", "quarterly", "yearly")
    for n in range(60):
        make_item(db, f"Bill {n:02d}", -(10 + n), cadences[n % 6], f"2026-10-{(n % 28) + 1:02d}",
                  account=base.chk if n % 3 else None, last_seen="2026-09-01", reminder=3 if n % 5 == 0 else 0)
    for n in range(400):
        add_txn(db, base.chk.id, TODAY - dt.timedelta(days=n % 60), -(10 + n % 60), "x",
                merchant=f"Bill {n % 60:02d}")
    started = time.perf_counter()
    body = cal(client, start="2026-08-30", end="2027-10-02")
    elapsed = time.perf_counter() - started
    assert len(body["days"]) == 399 and body["occurrences"]
    assert elapsed < 1.0, elapsed  # ~100 ms here; the design target is < 300 ms (generous for slow disks)


# ------------------------------------------------------------------ review fixes


def test_low_ahead_once_while_a_bill_is_carried(base):
    """A late (carried) bill moves the dip to "tomorrow" every day; that's one warning, not one a day."""
    db = base.db
    ok(base.client.put("/api/forecast/account", json={"account_id": base.chk.id}))
    rent = make_item(db, "Rent", -4900, "monthly", "2026-09-30", account=base.chk, last_seen="2026-08-30")
    for n in range(18):
        evaluate(db, TODAY + dt.timedelta(days=n), only=("low_ahead", "reminder"))
        db.commit()
    events = db.scalars(select(AlertEvent).where(AlertEvent.key == "low_ahead")).all()
    assert [e.dedupe_key for e in events] == [f"low_ahead:{base.chk.id}:r{rent.id}:2026-09-30@2026-09-30"]


def test_low_ahead_without_a_cause_keys_on_the_date(base):
    db = base.db
    base.chk.current_balance_cents = 201000
    db.commit()
    add_txn(db, base.chk.id, "2026-06-01", -1000, "Too old")
    add_txn(db, base.chk.id, "2026-09-20", -135, "Groceries")  # everyday spending only: no bill to blame
    assert [e.dedupe_key for e in keyed(run(db, TODAY), "low_ahead")] == [f"low_ahead:{base.chk.id}:2026-10-03"]
    assert keyed(run(db, TODAY), "low_ahead") == []


def test_restore_refuses_a_reused_id(base):
    client, db = base.client, base.db
    add(client, "Alpha", -10, "monthly", "2026-10-05")
    bravo = add(client, "Bravo", -20, "monthly", "2026-10-06")
    snap = ok(client.get(f"/api/recurring/{bravo['id']}/snapshot"))
    assert client.delete(f"/api/recurring/{bravo['id']}").status_code == 204
    # SQLite (no AUTOINCREMENT) may hand the freed id to the next item.
    db.add(RecurringItem(id=bravo["id"], name="Newbie", merchant_key="newbie", amount_cents=-9900, cadence="weekly",
                         next_date=D("2026-10-07"), status="active", include_in_forecast=True, source="manual",
                         start_date=D("2026-10-07")))
    db.commit()
    ok(client.put(f"/api/recurring/{bravo['id']}/occurrences/2026-10-07", json={"skipped": True}))
    r = client.post("/api/recurring/restore", json=snap)
    assert (r.status_code, r.json()["detail"]) == (409, "This item changed since. Undo isn't available.")
    assert sorted(i["name"] for i in ok(client.get("/api/recurring"))) == ["Alpha", "Newbie"]
    newbie = ok(client.get(f"/api/recurring/{bravo['id']}/snapshot"))
    assert newbie["overrides"][0]["skipped"] is True
    # Same merchant, another item (a different created_at): also refused.
    other = {**newbie, "item": {**newbie["item"], "created_at": "2020-01-01T00:00:00Z"}}
    assert client.post("/api/recurring/restore", json=other).status_code == 409
    # The item's own snapshot still restores in place.
    assert ok(client.post("/api/recurring/restore", json=newbie))["name"] == "Newbie"


def test_restore_category_rules_and_date_bounds(base):
    client = base.client
    rent = add(client, "Rent", -1500, "monthly", "2026-10-01", category_id="RENT_AND_UTILITIES")
    snap = ok(client.get(f"/api/recurring/{rent['id']}/snapshot"))
    item = snap["item"]
    r = client.post("/api/recurring/restore", json={**snap, "item": {**item, "category_id": "TRANSFER_OUT"}})
    assert (r.status_code, r.json()["detail"]) == (422, "unknown category")
    assert ok(client.post("/api/recurring/restore", json={**snap, "item": {**item, "category_id": "c_gone"}}))[
        "category_id"] is None
    blank = {"skipped": False, "paid": False, "paid_amount": None}
    for body in (
        {**snap, "item": {**item, "start_date": "0001-01-01"}},
        {**snap, "item": {**item, "next_date": "9999-12-01"}},
        {**snap, "item": {**item, "last_seen_date": "9999-12-01"}},
        {**snap, "overrides": [{**blank, "base_date": "0001-01-01", "skipped": True}]},
        {**snap, "overrides": [{**blank, "base_date": "2026-11-01", "moved_to": "9999-12-31"}]},
    ):
        assert client.post("/api/recurring/restore", json=body).status_code == 422, body
    assert ok(client.post("/api/recurring/restore", json=snap))["category_id"] == "RENT_AND_UTILITIES"


def test_far_dates_are_refused_and_the_calendar_survives_old_ones(base):
    client, db = base.client, base.db
    ok(client.put("/api/forecast/account", json={"account_id": base.chk.id}))
    for day in ("9999-12-01", "9001-01-01", "1899-12-31"):
        r = client.post("/api/recurring", json={"name": "Far", "amount": -5, "cadence": "monthly", "next_date": day})
        assert r.status_code == 422, day
    edge = add(client, "Edge", -5, "monthly", "9000-12-01")
    assert client.patch(f"/api/recurring/{edge['id']}", json={"next_date": "9999-12-01"}).status_code == 422
    assert cal(client)["account"]["id"] == base.chk.id
    # One stored before dates were bounded: no next base, and the calendar still loads.
    old = make_item(db, "Ancient", -5, "weekly", "9999-12-20", account=base.chk)
    assert next_base(old, TODAY) is None
    assert cal(client)["account"]["id"] == base.chk.id
    assert client.get("/api/forecast").status_code == 200


def test_reschedule_keeps_history_on_the_old_rule(base):
    client, db = base.client, base.db
    ok(client.put("/api/forecast/account", json={"account_id": base.chk.id}))
    gym = make_item(db, "Gym", -80, "monthly", "2026-09-15", account=base.chk, last_seen="2026-08-15")
    ok(client.put(f"/api/recurring/{gym.id}/occurrences/2026-09-15", json={"paid": True}))
    before = ok(client.get(f"/api/recurring/{gym.id}/snapshot"))
    assert day_balances(cal(client, start="2026-09-01", end="2026-10-31"))["2026-09-27"] == 5000.0
    r = ok(client.post(f"/api/recurring/{gym.id}/reschedule", json={"from_base": "2026-10-15", "to": "2026-10-17"}))
    assert r["item"]["start_date"] == "2026-10-15" and r["undo"] == before
    body = cal(client, start="2026-09-01", end="2026-10-31")
    # No "late" Sep 17 regenerated on the new anchor, so the paid Sep 15 isn't counted again.
    assert [o["key"] for o in body["occurrences"] if o["recurring_id"] == gym.id] == [f"r{gym.id}:2026-10-17"]
    assert day_balances(body)["2026-09-27"] == 5000.0
    # Undo puts start_date (none) and the paid override back exactly.
    ok(client.post("/api/recurring/restore", json=r["undo"]))
    assert ok(client.get(f"/api/recurring/{gym.id}/snapshot")) == before
    assert by_key(cal(client, start="2026-09-01", end="2026-10-31"))[f"r{gym.id}:2026-09-15"]["status"] == "paid"


def test_patch_schedule_change_keeps_history_but_same_rule_does_not_touch_it(base):
    client, db = base.client, base.db
    ok(client.put("/api/forecast/account", json={"account_id": base.chk.id}))
    gym = make_item(db, "Gym", -80, "monthly", "2026-09-15", account=base.chk, last_seen="2026-08-15")
    ok(client.put(f"/api/recurring/{gym.id}/occurrences/2026-09-15", json={"paid": True}))
    # The Edit dialog sends every field: an unchanged rule leaves the history alone.
    same = ok(client.patch(f"/api/recurring/{gym.id}", json={"name": "Gym!", "cadence": "monthly",
                                                             "next_date": "2026-10-15"}))
    assert same["start_date"] is None
    assert by_key(cal(client, start="2026-09-01", end="2026-10-31"))[f"r{gym.id}:2026-09-15"]["status"] == "paid"
    snap = ok(client.get(f"/api/recurring/{gym.id}/snapshot"))
    moved = ok(client.patch(f"/api/recurring/{gym.id}", json={"next_date": "2026-10-20"}))
    assert moved["start_date"] == "2026-10-15"
    body = cal(client, start="2026-09-01", end="2026-10-31")
    assert [o["key"] for o in body["occurrences"] if o["recurring_id"] == gym.id] == [f"r{gym.id}:2026-10-20"]
    assert day_balances(body)["2026-09-27"] == 5000.0
    ok(client.post("/api/recurring/restore", json=snap))
    assert ok(client.get(f"/api/recurring/{gym.id}/snapshot")) == snap
    # A late manual bill (past next date) moved ahead: the new rule starts today, nothing regenerated before.
    water = add(client, "Water", -40, "monthly", "2026-09-20", account_id=base.chk.id)
    assert ok(client.patch(f"/api/recurring/{water['id']}", json={"next_date": "2026-10-22"}))["start_date"] == (
        "2026-09-26")
    keys = [o["key"] for o in cal(client, start="2026-09-01", end="2026-10-31")["occurrences"]
            if o["recurring_id"] == water["id"]]
    assert keys == [f"r{water['id']}:2026-10-22"]


def test_backfill_gives_up_after_three_failures(base, monkeypatch, caplog):
    client, db = base.client, base.db
    db.add(AppSetting(key=recsvc.BACKFILL_KEY, value="1"))
    db.commit()

    def broken(session, today):
        raise RuntimeError("secret merchant name")

    monkeypatch.setattr(recsvc, "backfill_categories_once", broken)

    def stored(key):
        db.expire_all()
        row = db.get(AppSetting, key)
        return None if row is None else row.value

    for n in (1, 2):
        ok(client.get("/api/recurring"))
        assert (stored(recsvc.BACKFILL_KEY), stored(recsvc.BACKFILL_FAILURES_KEY)) == ("1", str(n))
    ok(client.get("/api/recurring"))
    assert (stored(recsvc.BACKFILL_KEY), stored(recsvc.BACKFILL_FAILURES_KEY)) == (None, None)
    assert "RuntimeError" in caplog.text and "secret merchant name" not in caplog.text
    # A success clears the count.
    monkeypatch.undo()
    db.add_all([AppSetting(key=recsvc.BACKFILL_KEY, value="1"),
                AppSetting(key=recsvc.BACKFILL_FAILURES_KEY, value="2")])
    db.commit()
    ok(client.get("/api/recurring"))
    assert (stored(recsvc.BACKFILL_KEY), stored(recsvc.BACKFILL_FAILURES_KEY)) == (None, None)


def test_price_change_keeps_a_manual_amount(base):
    client, db = base.client, base.db
    rent = add(client, "Rent", -1000, "monthly", "2026-10-01", account_id=base.chk.id)
    txn = add_txn(db, base.chk.id, "2026-09-20", -1100, "Rent")
    # Settings D7 update 2: no visible price event any more (a cleared tombstone).
    assert keyed(run(db, TODAY), "price") == []
    assert db.scalar(select(AlertEvent.cleared).where(AlertEvent.dedupe_key == f"price:{rent['id']}:{txn.id}"))
    assert ok(client.get(f"/api/recurring/{rent['id']}/snapshot"))["item"]["amount"] == -1000.0

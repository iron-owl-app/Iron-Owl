"""Release 3.6 "Budget for a beginner", phase 1: expected income, the income switch, income
notes, the guided setup, "Add a category", settings, alerts and Home.

Every expected number is worked out by hand from the fixtures (today is 2026-09-26, 4 days
left). Phase 2 (bills per category, ``bills`` targets, occurrence-based income, Plaid
detailed categories) is not tested here.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models import AlertEvent, AppSetting, Budget, CategoryGroup, RecurringItem, TransactionSplit, TxnCategory
from app.security import SESSION_HEADER
from app.services.alerts import evaluate
from tests.conftest import BASE_URL, FIXED_TODAY, add_budget, add_txn, make_account

T = "2026-09"


def ok(response, status=200) -> dict:
    assert response.status_code == status, response.text
    return response.json()


def err(response, status=422) -> str:
    assert response.status_code == status, response.text
    return response.json()["detail"]


def get(client, month=None) -> dict:
    return ok(client.get("/api/budgets", params={"month": month} if month else None))


def put(client, month=T, **body):
    return client.put(f"/api/budgets/{month}", json=body)


def settings(client, **body):
    return client.put("/api/budgets/settings", json=body)


def line(bm, category) -> dict:
    return next(c for c in bm["categories"] if c["category"] == category)


def setting(db, key):
    db.expire_all()
    row = db.get(AppSetting, key)
    return None if row is None else row.value


# ------------------------------------------------------------------ expected income


@pytest.fixture
def world(unlocked, db, fixed_today):
    """Budget accounts: Checking (+5000) and Visa (-300 owed) → cash 4,700.

    Received in September on budget accounts: 2,400 payroll + 100 pending bonus + 200 (the
    income part of a split deposit) = 2,700. Suggested: Aug 4,810.55 and Jun 4,700 (July had
    none) → 4,755.27 → 4,750.
    """
    chk = make_account(db, "Checking", "bank", 5000)
    card = make_account(db, "Visa", "credit", 300)
    hsa = make_account(db, "HSA", "hsa", 100)
    add_txn(db, chk.id, "2026-09-15", 2400, "PAYROLL", merchant="Acme Corp", category="INCOME")
    add_txn(db, chk.id, "2026-09-20", 100, "BONUS", merchant="Acme Bonus", category="INCOME", pending=True)
    split = add_txn(db, chk.id, "2026-09-10", 300, "MIXED DEPOSIT", category="INCOME")
    db.add_all([
        TransactionSplit(transaction_id=split.id, amount_cents=20000, category="INCOME", position=0),
        # A spending-kind part (like a refund) never counts as income.
        TransactionSplit(transaction_id=split.id, amount_cents=10000, category="GENERAL_MERCHANDISE", position=1),
    ])
    db.commit()
    add_txn(db, hsa.id, "2026-09-12", 999, "HSA INTEREST", category="INCOME")  # not a budget account
    add_txn(db, chk.id, "2026-09-14", 500, "FROM SAVINGS", category="INCOME", is_transfer=True)  # a transfer
    add_txn(db, card.id, "2026-09-05", 20, "REFUND", category="FOOD_AND_DRINK")  # not income kind
    add_txn(db, chk.id, "2026-10-01", 777, "NEXT MONTH", category="INCOME")
    add_txn(db, chk.id, "2026-08-31", 4810.55, "AUG PAY", category="INCOME")
    add_txn(db, chk.id, "2026-06-15", 4700, "JUN PAY", category="INCOME")
    add_txn(db, chk.id, "2026-05-15", 9999, "MAY PAY", category="INCOME")  # outside the 3 months
    add_txn(db, chk.id, "2026-09-02", -100, "TJ", category="FOOD_AND_DRINK")
    add_txn(db, card.id, "2026-09-03", -70, "TJ 2", category="FOOD_AND_DRINK")
    add_txn(db, chk.id, "2026-08-10", -10, "AUG", category="FOOD_AND_DRINK")  # August is viewable
    add_budget(db, T, {"FOOD_AND_DRINK": 600, "ENTERTAINMENT": 100})
    return chk, card, hsa


def numbers(bm) -> tuple:
    return (bm["ready_to_assign"], bm["month_money"], bm["assigned"], bm["available"], bm["cash_total"])


def test_mode_off_is_exactly_todays_numbers(world, unlocked, db):
    # FOOD 600 - (170 - 20) = 450 left, ENT 100: RTA = 4,700 - 550 = 4,150.
    bm = get(unlocked)
    assert numbers(bm) == (4150.0, 4850.0, 700.0, 550.0, 4700.0)
    assert bm["income"] == {"mode": "off", "expected": 4750.0, "expected_override": None, "suggested": 4750.0,
                            "suggested_source": "average",  # Settings D7: no paycheck on the calendar
                            "received": 2700.0, "pending": 0.0, "next": None, "event": None}
    assert bm["savings_category"] is None and bm["setup_needed"] is False and bm["bills_outside"] == []

    # Switching on counts the paycheck still expected; switching off gives today's numbers back.
    ok(settings(unlocked, income_mode="expected"))
    on = get(unlocked)
    assert on["income"]["pending"] == 2050.0  # 4,750 - 2,700
    assert numbers(on) == (6200.0, 6900.0, 700.0, 550.0, 4700.0)
    ok(put(unlocked, income_expected=5000))
    ok(settings(unlocked, income_mode="off"))
    off = get(unlocked)
    assert numbers(off) == numbers(bm)
    # The stored month is kept while off.
    assert off["income"]["expected_override"] == 5000.0 and off["income"]["pending"] == 0.0
    assert json.loads(setting(db, "budget_income")) == {"mode": "off", "months": {T: 500000}}


def test_received_counts_only_income_on_budget_accounts(world, unlocked, db):
    chk, card, hsa = world
    assert get(unlocked)["income"]["received"] == 2700.0
    # Month bounds: August's last day and October's first day belong to their own months.
    assert get(unlocked, "2026-08")["income"]["received"] == 4810.55
    assert get(unlocked, "2026-10")["income"]["received"] == 777.0
    # Only budget accounts count: without Checking nothing came in.
    ok(unlocked.put("/api/budgets/accounts", json={"account_ids": [card.id]}))
    assert get(unlocked)["income"]["received"] == 0.0


def test_suggested_income(unlocked, db, fixed_today):
    chk = make_account(db, "Checking", "bank", 0)
    assert get(unlocked)["income"]["suggested"] is None  # no income at all
    add_txn(db, chk.id, "2026-07-10", 3333.33, "PAY", category="INCOME")
    assert get(unlocked)["income"]["suggested"] == 3330.0  # rounded down to $10
    add_txn(db, chk.id, "2026-08-10", 1000, "PAY", category="INCOME")
    add_txn(db, chk.id, "2026-06-10", 1000, "PAY", category="INCOME")
    assert get(unlocked)["income"]["suggested"] == 1770.0  # 5,333.33 / 3 = 1,777.77
    add_txn(db, chk.id, "2026-09-10", 50000, "PAY", category="INCOME")  # this month doesn't count
    assert get(unlocked)["income"]["suggested"] == 1770.0


def test_ready_pending_in_every_month(world, unlocked):
    """Ready to assign (and the part of it still expected) is the same whichever month is viewed."""
    assert get(unlocked)["ready_pending"] == 0.0  # mode off
    ok(settings(unlocked, income_mode="expected"))
    ok(put(unlocked, income_expected=5000))
    for month in (T, "2026-10", "2027-03", "2026-08"):
        bm = get(unlocked, month)
        assert bm["ready_pending"] == 2300.0, month
    assert get(unlocked, "2026-10")["income"]["pending"] == 0.0


def test_pending_only_in_the_current_month(world, unlocked):
    ok(settings(unlocked, income_mode="expected"))
    assert get(unlocked, "2026-10")["income"]["pending"] == 0.0
    assert get(unlocked, "2026-10")["ready_to_assign"] == 6200.0  # Ready to assign is global
    assert get(unlocked, "2026-08")["income"]["event"] is None


def test_pending_counts_for_the_over_assign_rule_and_fund_targets(world, unlocked):
    ok(settings(unlocked, income_mode="expected"))
    # RTA 6,200 with 2,050 still expected: assigning 6,200 more is allowed, a cent more isn't.
    assert err(put(unlocked, assigned={"ENTERTAINMENT": 6300.01})) == "Only $6,200 is ready to assign. Lower another category first."
    ok(put(unlocked, assigned={"ENTERTAINMENT": 6300}))
    ok(put(unlocked, assigned={"ENTERTAINMENT": 100}))
    # fund-targets uses the same Ready to assign.
    ok(unlocked.patch("/api/categories/TRAVEL", json={"target": {"kind": "monthly", "amount": 7000}}))
    ok(put(unlocked, assigned={"TRAVEL": 0}))
    res = ok(unlocked.post(f"/api/budgets/{T}/fund-targets", json={}))
    assert (res["funded"], res["unfunded"]) == (6200.0, 800.0)


def test_change_amount_maps_to_expected_income(world, unlocked):
    ok(settings(unlocked, income_mode="expected"))
    bm = ok(put(unlocked, income_expected=5000))
    assert bm["income"]["expected"] == 5000.0 and bm["income"]["expected_override"] == 5000.0
    assert bm["month_money"] == 7150.0  # 6,900 + (5,000 - 4,750)
    # Below what already came in.
    assert err(put(unlocked, income_expected=2000)) == "$2,700 has already come in this month, so pick at least $4,850."
    # Plans over the month's amount.
    ok(put(unlocked, assigned={"FOOD_AND_DRINK": 5000}))  # RTA 6,450 - 4,400 = 2,050
    assert err(put(unlocked, income_expected=2700)) == "Your plans add up to $5,100. Lower some plans first, or pick at least $5,100."
    # Only today's month.
    assert err(put(unlocked, "2026-10", income_expected=100)) == "The month's amount can only be changed for this month."
    assert err(put(unlocked, "2026-08", income_expected=100)) == "Past months are read-only. Make changes in the current month."
    # Atomic: a refused save changes neither the plan nor the amount.
    assert err(put(unlocked, assigned={"ENTERTAINMENT": 1}, income_expected=2000)).startswith("$2,700 has already")
    bm = get(unlocked)
    assert line(bm, "ENTERTAINMENT")["assigned"] == 100.0 and bm["income"]["expected"] == 5000.0
    # Together in one save: lower a plan and the amount by the same $300 (Take it from categories).
    bm = ok(put(unlocked, assigned={"FOOD_AND_DRINK": 4700}, income_expected=4700))
    assert (bm["ready_to_assign"], line(bm, "FOOD_AND_DRINK")["assigned"]) == (2050.0, 4700.0)
    # null goes back to the suggestion.
    bm = ok(put(unlocked, income_expected=None))
    assert bm["income"]["expected_override"] is None and bm["income"]["expected"] == 4750.0


def test_change_amount_needs_expected_mode(world, unlocked):
    assert err(put(unlocked, income_expected=5000)).startswith("The month's amount follows the money in your accounts.")


def test_accept_received(world, unlocked):
    ok(settings(unlocked, income_mode="expected"))
    ok(put(unlocked, income_expected=5000))
    ok(put(unlocked, assigned={"FOOD_AND_DRINK": 5000}))
    detail = "Leave it for next month sets the month's amount to what came in, nothing else."
    assert err(put(unlocked, income_expected=3000, accept_received=True)) == detail
    assert err(put(unlocked, income_expected=None, accept_received=True)) == detail
    assert err(put(unlocked, accept_received=True)) == "Leave it for next month needs the amount that came in."
    assert err(put(unlocked, income_expected=2700, accept_received=True, assigned={"ENTERTAINMENT": 101})) == (
        "Leave it for next month can't raise a plan."
    )
    bm = ok(put(unlocked, income_expected=2700, accept_received=True))
    assert (bm["ready_to_assign"], bm["month_money"], bm["assigned"]) == (-250.0, 4850.0, 5100.0)
    # Only once: the next plan raise is refused as usual.
    assert err(put(unlocked, assigned={"ENTERTAINMENT": 101})).startswith("Only $0 is ready to assign.")


# ------------------------------------------------------------------ income notes


def event(client) -> dict | None:
    return get(client)["income"]["event"]


def test_earned_more_threshold_and_resolution(world, unlocked, db):
    chk, _card, _hsa = world
    ok(settings(unlocked, income_mode="expected"))
    ok(put(unlocked, income_expected=2700))
    assert event(unlocked) is None
    add_txn(db, chk.id, "2026-09-21", 0.99, "INTEREST", merchant="Acme Corp", category="INCOME")
    assert event(unlocked) is None  # $0.99 isn't worth a note
    add_txn(db, chk.id, "2026-09-21", 0.01, "INTEREST", merchant="Acme Corp", category="INCOME")
    assert event(unlocked) == {"kind": "more", "amount": 1.0, "date": "2026-09-21", "name": "Acme Corp",
                               "expected": 2700.0, "received": 2701.0}
    # Add it to this month's plan (and Leave it for now): the amount becomes what came in.
    ok(put(unlocked, income_expected=2701))
    assert event(unlocked) is None


def test_earned_more_put_in_savings(world, unlocked, db):
    chk, _card, _hsa = world
    ok(settings(unlocked, income_mode="expected"))
    ok(put(unlocked, income_expected=2500 + 200))
    add_txn(db, chk.id, "2026-09-21", 200, "BONUS", category="INCOME")
    assert event(unlocked)["amount"] == 200.0
    ok(put(unlocked, assigned={"TRAVEL": 0}))
    ok(settings(unlocked, savings_category="TRAVEL"))
    bm = ok(put(unlocked, income_expected=2900, assigned={"TRAVEL": 200}))
    assert bm["income"]["event"] is None and bm["savings_category"] == "TRAVEL"
    assert line(bm, "TRAVEL")["assigned"] == 200.0


@pytest.mark.parametrize("choice", ["add", "save", "leave"])
def test_earned_more_choice_undo_restores_exactly(world, unlocked, db, choice):
    """Undo of an "earned more" choice puts back an amount below what came in (restore)."""
    chk, _card, _hsa = world
    ok(settings(unlocked, income_mode="expected"))
    ok(put(unlocked, assigned={"TRAVEL": 50}))
    ok(settings(unlocked, savings_category="TRAVEL"))
    ok(put(unlocked, income_expected=2700))
    add_txn(db, chk.id, "2026-09-21", 200, "BONUS", merchant="Acme Bonus", category="INCOME")
    before = get(unlocked)
    note = before["income"]["event"]
    assert note["kind"] == "more" and note["amount"] == 200.0
    # The choice: the amount becomes what came in (and "save" also plans it in savings).
    body = {"income_expected": 2900}
    if choice == "save":
        body["assigned"] = {"TRAVEL": 250}
    after = ok(put(unlocked, **body))
    assert after["income"]["event"] is None
    # Undo without restore is refused (below what came in), and changes nothing.
    back = {"income_expected": 2700}
    if choice == "save":
        back["assigned"] = {"TRAVEL": 50}
    assert err(put(unlocked, **back)).startswith("$2,900 has already come in this month")
    assert numbers(get(unlocked)) == numbers(after)
    # With restore the month is exactly as before and the note is back.
    undone = ok(put(unlocked, restore=True, **back))
    assert numbers(undone) == numbers(before)
    assert undone["income"] == before["income"]
    assert [(c["category"], c["assigned"], c["available"]) for c in undone["categories"]] == [
        (c["category"], c["assigned"], c["available"]) for c in before["categories"]
    ]


def test_restore_rules(world, unlocked, db):
    chk, _card, _hsa = world
    ok(settings(unlocked, income_mode="expected"))
    ok(put(unlocked, income_expected=2700))
    add_txn(db, chk.id, "2026-09-21", 200, "BONUS", category="INCOME")
    assert err(put(unlocked, restore=True)) == "Undo needs the amounts to put back."
    assert err(put(unlocked, restore=True, removed=["ENTERTAINMENT"])) == "Undo needs the amounts to put back."
    assert err(put(unlocked, restore=True, income_expected=2900, accept_received=True)) == (
        "Undo can't be combined with Leave it for next month."
    )
    # Every other rule still applies: only this month, only in expected mode, plans within the money.
    assert err(put(unlocked, "2026-10", restore=True, income_expected=100)) == (
        "The month's amount can only be changed for this month."
    )
    ready = get(unlocked)["ready_to_assign"]
    assert err(put(unlocked, restore=True, income_expected=2700, assigned={"TRAVEL": ready + 1})).startswith(
        f"Only ${ready:,.0f} is ready to assign."
    )
    ok(settings(unlocked, income_mode="off"))
    assert err(put(unlocked, restore=True, income_expected=2700)).startswith("The month's amount follows the money")


def test_paycheck_short(world, unlocked, fixed_today, db):
    chk, _card, _hsa = world
    ok(settings(unlocked, income_mode="expected"))
    ok(put(unlocked, income_expected=3000))
    # Without a recurring paycheck, pay is assumed in by the 20th: a short month is called
    # out from the 21st on.
    fixed_today.set(dt.date(2026, 9, 20))
    assert event(unlocked) is None
    fixed_today.set(dt.date(2026, 9, 21))
    short = event(unlocked)
    assert short == {"kind": "less", "amount": 300.0, "date": None, "name": None, "expected": 3000.0,
                     "received": 2700.0, "reason": "short", "can_use_unplanned": True}
    # A paycheck still expected this month holds the note back...
    item = RecurringItem(name="Acme Corp", merchant_key="acme corp", account_id=chk.id, amount_cents=240000,
                         cadence="monthly", next_date=dt.date(2026, 9, 30), status="active")
    db.add(item)
    db.commit()
    bm = get(unlocked)
    assert bm["income"]["event"] is None
    assert bm["income"]["next"] == {"date": "2026-09-30", "name": "Acme Corp", "amount": 2400.0}
    # ...one due next month doesn't.
    item.next_date = dt.date(2026, 10, 15)
    db.commit()
    assert event(unlocked)["kind"] == "less"
    # can_use_unplanned: Not planned yet (RTA incl. the $300 still expected) must cover it.
    ok(put(unlocked, assigned={"FOOD_AND_DRINK": 600 + 4450 - 299.99}))  # RTA 4,450 → 299.99
    assert event(unlocked)["can_use_unplanned"] is False
    # Take it from categories: lower a plan and the amount together; the note is gone.
    bm = ok(put(unlocked, assigned={"FOOD_AND_DRINK": 4750.01 - 300}, income_expected=2700))
    assert bm["income"]["event"] is None and bm["ready_to_assign"] == 299.99


def test_paycheck_missing_and_use_unplanned(unlocked, db, fixed_today):
    chk = make_account(db, "Checking", "bank", 1000)
    add_txn(db, chk.id, "2026-08-15", 2000, "PAY", category="INCOME")
    fixed_today.set(dt.date(2026, 9, 29))
    ok(settings(unlocked, income_mode="expected"))
    bm = get(unlocked)
    assert bm["income"]["event"] == {"kind": "less", "amount": 2000.0, "date": None, "name": None,
                                     "expected": 2000.0, "received": 0.0, "reason": "missing",
                                     "can_use_unplanned": True}
    # Use money that isn't planned yet: the amount becomes what came in; no plan changes.
    bm = ok(put(unlocked, income_expected=0))
    assert bm["income"]["event"] is None and bm["ready_to_assign"] == 1000.0


def test_income_event_off_mode(world, unlocked, db):
    chk, _card, _hsa = world
    ok(settings(unlocked, income_mode="expected"))
    ok(put(unlocked, income_expected=2700))
    add_txn(db, chk.id, "2026-09-21", 500, "BONUS", category="INCOME")
    assert event(unlocked)["kind"] == "more"
    ok(settings(unlocked, income_mode="off"))
    assert event(unlocked) is None


# ------------------------------------------------------------------ settings


def test_settings(world, unlocked, db):
    bm = ok(settings(unlocked, income_mode="expected", savings_category="TRAVEL"))
    assert bm["income"]["mode"] == "expected" and bm["savings_category"] == "TRAVEL"
    assert err(settings(unlocked, savings_category="NOPE")) == "Unknown category."
    assert err(settings(unlocked, savings_category="INCOME")) == "Income isn't a spending category."
    assert err(settings(unlocked, income_mode=None)) == "income_mode must not be null"
    assert settings(unlocked, income_mode="maybe").status_code == 422
    assert settings(unlocked, sneaky=1).status_code == 422
    assert ok(settings(unlocked, savings_category=None))["savings_category"] is None
    assert setting(db, "budget_savings_category") is None
    # A savings category that stops being a spending category is ignored.
    ok(settings(unlocked, savings_category="TRAVEL"))
    ok(unlocked.patch("/api/categories/TRAVEL", json={"kind": "fixed"}))
    assert get(unlocked)["savings_category"] is None


def test_stored_months_are_pruned(world, unlocked, db):
    db.add(AppSetting(key="budget_income", value=json.dumps(
        {"mode": "expected", "months": {"2026-01": 1, "2026-06": 2, "2027-09": 3, "2027-10": 4, "bad": 5, "2026-08": -1}})))
    db.commit()
    ok(put(unlocked, income_expected=4000))
    assert json.loads(setting(db, "budget_income")) == {"mode": "expected", "months": {"2026-06": 2, T: 400000, "2027-09": 3}}


# ------------------------------------------------------------------ guided setup


@pytest.fixture
def fresh(unlocked, db, fixed_today):
    """A new vault: history June–August, no budget rows. Cash 3,000.

    Averages (3 months): rent 1,200, services 100 (300 in August), entertainment 90,
    other 50, food 400 (Food and drink stays out of the plan in phase 1).
    September: pay 2,000, rent 1,200, food 150 → existing money = 3,000 + 1,200 - 2,000 = 2,200.
    """
    chk = make_account(db, "Checking", "bank", 3000)
    for month in ("06", "07", "08"):
        add_txn(db, chk.id, f"2026-{month}-01", -1200, "RENT", category="RENT_AND_UTILITIES")
        add_txn(db, chk.id, f"2026-{month}-05", 4000, "PAY", category="INCOME")
        add_txn(db, chk.id, f"2026-{month}-09", -90, "MOVIES", category="ENTERTAINMENT")
        add_txn(db, chk.id, f"2026-{month}-10", -50, "GIFT", category="OTHER")
        add_txn(db, chk.id, f"2026-{month}-11", -400, "FOOD", category="FOOD_AND_DRINK")
    add_txn(db, chk.id, "2026-08-20", -300, "PHONE", category="GENERAL_SERVICES")
    add_txn(db, chk.id, "2026-09-01", -1200, "RENT", category="RENT_AND_UTILITIES")
    add_txn(db, chk.id, "2026-09-15", 2000, "PAY", category="INCOME")
    add_txn(db, chk.id, "2026-09-16", -150, "FOOD", category="FOOD_AND_DRINK")
    return chk


ANSWERS = {"groceries": 500, "gas": 150, "eating_out": 200, "bills": 1300.01, "fun": 100, "other": 100}


def setup(client, **body):
    return client.post("/api/budgets/setup", json={"answers": ANSWERS, "income_expected": 4000,
                                                   "keep_existing_in_savings": True, **body})


def cat_id(db, name) -> str:
    db.expire_all()
    return db.scalar(select(TxnCategory.id).where(TxnCategory.name == name))


def test_setup_info(fresh, unlocked):
    info = ok(unlocked.get("/api/budgets/setup"))
    rows = {r["key"]: r for r in info["rows"]}
    assert list(rows) == ["groceries", "gas", "eating_out", "bills", "fun", "other"]
    assert (info["month"], info["needed"], info["bills"], info["existing_money"]) == (T, True, None, 2200.0)
    assert info["owed_beyond_cash"] == 0.0
    # No paychecks on the calendar: the screen starts from the larger of the average and what came in.
    assert info["income"] == {"suggested": 4000.0, "suggested_source": "average", "received": 2000.0,
                              "expected_more": 0.0, "start": 4000.0}
    assert rows["groceries"] == {"key": "groceries", "label": "Groceries", "hint": "Food and household items from the store",
                                 "categories": ["Groceries"], "average": None, "chips": [400.0, 600.0, 800.0], "suggested": 600.0}
    assert (rows["bills"]["average"], rows["bills"]["chips"], rows["bills"]["suggested"]) == (1300.0, [1000.0, 1300.0, 1600.0], 1300.0)
    assert rows["bills"]["categories"] == ["Rent and utilities", "Services"]
    assert (rows["fun"]["average"], rows["fun"]["chips"], rows["fun"]["suggested"]) == (90.0, [50.0, 100.0], 100.0)
    assert (rows["other"]["average"], rows["other"]["chips"]) == (50.0, [50.0])
    assert get(unlocked)["setup_needed"] is True


def test_setup_makes_the_plan(fresh, unlocked, db):
    bm = ok(setup(unlocked), 201)
    groceries, gas, eating, savings = (cat_id(db, n) for n in ("Groceries", "Gas", "Eating out", "Emergency savings"))
    assert all(c and c.startswith("c_") for c in (groceries, gas, eating, savings))
    assigned = {c["category"]: c["assigned"] for c in bm["categories"]}
    assert assigned == {groceries: 500.0, gas: 150.0, eating: 200.0, savings: 0.0,
                        # 1,300.01 split 12:1 by average; the leftover cent goes to the largest.
                        "RENT_AND_UTILITIES": 1200.01, "GENERAL_SERVICES": 100.0,
                        "ENTERTAINMENT": 100.0, "OTHER": 100.0}
    # Food and drink stays out of the plan (phase 1): its spending is "without a plan".
    assert [u["category"] for u in bm["unbudgeted"]] == ["FOOD_AND_DRINK"]
    # The money already there is last month's leftover in Emergency savings.
    assert line(bm, savings)["carryover"] == 2200.0 and line(bm, savings)["available"] == 2200.0
    assert bm["month_money"] == 4000.0 and bm["ready_to_assign"] == 1649.99
    assert (bm["savings_category"], bm["setup_needed"]) == (savings, False)
    assert bm["income"]["mode"] == "expected" and bm["income"]["expected"] == 4000.0
    db.expire_all()
    assert [(b.month, b.limit_cents) for b in db.scalars(select(Budget).where(Budget.category == savings).order_by(Budget.month))] == [
        ("2026-08", 220000), (T, 0)]
    assert setting(db, "budget_setup_done") == "1"
    # The August row is marked as money already had (not a plan Quick fill should copy).
    assert json.loads(setting(db, "budget_setup_seed")) == {"month": "2026-08", "category": savings}
    assert bm["seed_category"] is None
    august = get(unlocked, "2026-08")
    assert august["seed_category"] == savings and line(august, savings)["assigned"] == 2200.0
    assert get(unlocked, "2026-10")["seed_category"] is None
    # Groups: created because there were none.
    groups = {g["name"]: g["category_ids"] for g in ok(unlocked.get("/api/category-groups"))}
    assert list(groups) == ["Bills", "Everyday", "Fun", "Other and saving", "Income"]
    assert groups["Bills"] == ["RENT_AND_UTILITIES", "GENERAL_SERVICES"]
    assert groups["Income"] == ["INCOME"]  # paid this year; not shown on the budget
    assert [g["name"] for g in bm["groups"]] == ["Bills", "Everyday", "Fun", "Other and saving"]
    assert set(groups["Everyday"]) >= {groceries, eating, gas, "FOOD_AND_DRINK", "TRANSPORTATION", "MEDICAL"}
    assert savings in groups["Other and saving"] and "OTHER" in groups["Other and saving"]
    # Once only.
    assert err(setup(unlocked), 409) == "Your budget is already set up."


def test_setup_without_keeping_existing_money(fresh, unlocked, db):
    bm = ok(setup(unlocked, keep_existing_in_savings=False), 201)
    savings = bm["savings_category"]
    assert line(bm, savings)["carryover"] == 0.0 and bm["month_money"] == 6200.0
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Budget).where(Budget.month == "2026-08")) == 0
    assert setting(db, "budget_setup_seed") is None and bm["seed_category"] is None


def test_setup_skip_starts_plans_at_spent_so_far(fresh, unlocked, db):
    bm = ok(setup(unlocked, skip=True, answers={}, keep_existing_in_savings=False), 201)
    assigned = {c["name"]: c["assigned"] for c in bm["categories"]}
    assert assigned == {"Groceries": 0.0, "Gas": 0.0, "Eating out": 0.0, "Emergency savings": 0.0,
                        "Rent and utilities": 1200.0, "Services": 0.0, "Entertainment": 0.0, "Other": 0.0}
    assert all(c["available"] >= 0 for c in bm["categories"])


def test_setup_refusals_change_nothing(fresh, unlocked, db):
    assert err(setup(unlocked, answers={**ANSWERS, "bills": 3000})) == "Lower an amount to continue."
    assert err(setup(unlocked, income_expected=1999)) == "$2,000 has already come in this month, so pick at least $2,000."
    assert setup(unlocked, answers={"rent": 1}).status_code == 422
    assert setup(unlocked, answers={"bills": -1}).status_code == 422
    db.expire_all()
    assert cat_id(db, "Groceries") is None and cat_id(db, "Emergency savings") is None
    assert db.scalar(select(func.count()).select_from(CategoryGroup)) == 0
    assert setting(db, "budget_income") is None and setting(db, "budget_setup_done") is None
    assert get(unlocked)["setup_needed"] is True


def test_setup_reuses_categories_and_existing_groups(fresh, unlocked, db):
    chk = fresh
    mine = ok(unlocked.post("/api/category-groups", json={"name": "Mine"}), 201)
    ok(unlocked.patch("/api/categories/FOOD_AND_DRINK", json={"group_id": mine["id"]}))
    saved = ok(unlocked.post("/api/categories", json={"name": "emergency SAVINGS", "kind": "spending"}), 201)
    groceries = ok(unlocked.post("/api/categories", json={"name": "Groceries", "kind": "spending"}), 201)
    add_txn(db, chk.id, "2026-07-03", -300, "MARKET", category=groceries["id"])
    info = ok(unlocked.get("/api/budgets/setup"))
    assert next(r for r in info["rows"] if r["key"] == "groceries")["average"] == 100.0

    bm = ok(setup(unlocked), 201)
    assert bm["savings_category"] == saved["id"] and cat_id(db, "Groceries") == groceries["id"]
    groups = {g["name"]: g["category_ids"] for g in ok(unlocked.get("/api/category-groups"))}
    assert list(groups) == ["Mine"]  # no starter groups when there are groups
    assert set(groups["Mine"]) == {"FOOD_AND_DRINK", groceries["id"], cat_id(db, "Eating out")}


def test_setup_refuses_a_same_name_category_of_another_kind(fresh, unlocked, db):
    ok(unlocked.post("/api/categories", json={"name": "Gas", "kind": "fixed"}), 201)
    assert err(setup(unlocked)) == "A category named “Gas” already exists but isn't for spending. Rename it first."
    assert cat_id(db, "Groceries") is None


def test_setup_not_needed_with_budget_rows(world, unlocked):
    assert ok(unlocked.get("/api/budgets/setup"))["needed"] is False
    assert err(setup(unlocked), 409) == "Your budget is already set up."


def test_home_leaves_savings_out(fresh, unlocked):
    ok(setup(unlocked), 201)
    budget = ok(unlocked.get("/api/dashboard"))["budget"]
    assert (budget["planned"], budget["spent"], budget["left"], budget["has_budget"]) == (2350.01, 1200.0, 1150.01, True)


# ------------------------------------------------------------------ add a category


def add(client, month=T, **body):
    return client.post(f"/api/budgets/{month}/categories", json=body)


def test_add_category(world, unlocked, db):
    group = ok(unlocked.post("/api/category-groups", json={"name": "Pets & kids"}), 201)
    res = ok(add(unlocked, name="  Pet   food ", group_id=group["id"], plan=50), 201)
    cat = res["category"]
    assert (cat["name"], cat["kind"], cat["custom"], cat["group_id"]) == ("Pet food", "spending", True, group["id"])
    assert line(res["month"], cat["id"])["assigned"] == 50.0 and res["month"]["ready_to_assign"] == 4100.0
    count = db.scalar(select(func.count()).select_from(TxnCategory))
    assert err(add(unlocked, name="PET FOOD", plan=0), 409) == "A category named “PET FOOD” already exists."
    assert err(add(unlocked, name="Toys", group_id=9999)) == "unknown group"
    assert err(add(unlocked, name="Toys", plan=4100.01)) == (
        "Only $4,100 isn't planned yet. Use a smaller amount, or change the month's amount.")
    assert err(add(unlocked, "2026-08", name="Toys")) == "Past months are read-only. Make changes in the current month."
    assert err(add(unlocked, name="   ")) == "name must not be empty"
    assert add(unlocked, name="Toys", plan=-1).status_code == 422
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(TxnCategory)) == count
    # Undo = delete the category; its plan goes with it.
    assert unlocked.delete(f"/api/categories/{cat['id']}").status_code == 204
    assert get(unlocked)["ready_to_assign"] == 4150.0


# ------------------------------------------------------------------ alerts


def income_alerts(db):
    db.expire_all()
    return [(e.title, e.body) for e in db.scalars(select(AlertEvent).where(AlertEvent.key == "income").order_by(AlertEvent.id))]


def test_income_alert_in_expected_mode(world, unlocked, db):
    chk, _card, _hsa = world
    ok(settings(unlocked, income_mode="expected"))
    ok(put(unlocked, income_expected=3000))
    pay = add_txn(db, chk.id, "2026-09-25", 200, "PAYROLL", merchant="Acme Corp", category="INCOME")
    evaluate(db, FIXED_TODAY, new_transaction_ids=[pay.id])
    db.commit()
    assert income_alerts(db) == []  # 2,900 came in of the 3,000 expected: nothing new
    more = add_txn(db, chk.id, "2026-09-25", 420, "BONUS", merchant="Acme Corp", category="INCOME")
    evaluate(db, FIXED_TODAY, new_transaction_ids=[more.id])
    db.commit()
    assert income_alerts(db) == [("$320 more than expected came in", "Acme Corp on Checking. It's under Not planned yet.")]


def test_income_alert_off_mode_unchanged(world, unlocked, db):
    chk, _card, _hsa = world
    pay = add_txn(db, chk.id, "2026-09-25", 200, "PAYROLL", merchant="Acme Corp", category="INCOME")
    evaluate(db, FIXED_TODAY, new_transaction_ids=[pay.id])
    db.commit()
    assert income_alerts(db) == [("$200 new to assign", "Acme Corp on Checking. Ready to assign is now $4,150.")]


def test_budget_alert_copy(world, unlocked, db):
    chk, _card, _hsa = world
    add_budget(db, T, {"TRAVEL": 100})
    add_txn(db, chk.id, "2026-09-06", -130.5, "FLIGHT", category="TRAVEL")
    add_txn(db, chk.id, "2026-09-07", -95, "MOVIES", category="ENTERTAINMENT")
    evaluate(db, FIXED_TODAY)
    db.commit()
    db.expire_all()
    titles = [e.title for e in db.scalars(select(AlertEvent).where(AlertEvent.key == "budget").order_by(AlertEvent.id))]
    # The title has no amount (sent once a month while the overspend keeps changing).
    # Settings D7 update 2: only over the plan (Entertainment at 95% no longer alerts).
    assert titles == ["Travel went over its plan"]


# ------------------------------------------------------------------ security


ROUTES = (
    ("GET", "/api/budgets/setup", None),
    ("POST", "/api/budgets/setup", {"income_expected": 1}),
    ("PUT", "/api/budgets/settings", {"income_mode": "off"}),
    ("POST", f"/api/budgets/{T}/categories", {"name": "X"}),
    ("PUT", f"/api/budgets/{T}", {"income_expected": 1}),
)


def test_routes_need_a_session(client):
    for method, url, body in ROUTES:
        assert client.request(method, url, json=body).status_code == 401, url


def test_writes_need_the_csrf_header(unlocked):
    anon = TestClient(unlocked.app, base_url=BASE_URL, headers={SESSION_HEADER: unlocked.headers[SESSION_HEADER]})
    anon.cookies = unlocked.cookies
    for method, url, body in ROUTES[1:]:
        assert anon.request(method, url, json=body).status_code == 403, url


def test_unknown_fields_and_nan_are_refused(world, unlocked):
    for method, url, body in ROUTES[1:]:
        assert unlocked.request(method, url, json={**body, "sneaky": 1}).status_code == 422, url
    for literal in ("NaN", "Infinity"):
        for url, template in ((f"/api/budgets/{T}", '{"income_expected": %s}'),
                              ("/api/budgets/setup", '{"income_expected": %s}'),
                              (f"/api/budgets/{T}/categories", '{"name": "X", "plan": %s}')):
            method = "PUT" if url.endswith(T) else "POST"
            r = unlocked.request(method, url, content=template % literal, headers={"Content-Type": "application/json"})
            assert r.status_code == 422, (url, literal)


# ------------------------------------------------------------------ review fixes


def test_pay_categorized_as_transfer_in_counts_as_received(unlocked, db, fixed_today):
    """A freelancer paid by Venmo: the deposit is "Transfer in" but not a matched transfer.
    It is in the cash already, so it must not also be still expected (RTA $3,000, not $7,000)."""
    chk = make_account(db, "Checking", "bank", 4000)
    add_txn(db, chk.id, "2026-09-15", 4000, "VENMO CLIENT", category="TRANSFER_IN")
    bm = ok(unlocked.post("/api/budgets/setup", json={
        "income_expected": 4000, "answers": {"bills": 1000}, "keep_existing_in_savings": False}), 201)
    assert bm["ready_to_assign"] == 3000.0 and bm["month_money"] == 4000.0
    assert (bm["income"]["received"], bm["income"]["pending"]) == (4000.0, 0.0)
    # Not counted: a matched transfer between own accounts, a refund of a spending category,
    # a deposit dated last month or next month.
    add_txn(db, chk.id, "2026-09-16", 500, "FROM SAVINGS", category="TRANSFER_IN", is_transfer=True)
    add_txn(db, chk.id, "2026-09-17", 20, "REFUND", category="FOOD_AND_DRINK")
    add_txn(db, chk.id, "2026-08-31", 300, "VENMO AUG", category="TRANSFER_IN")
    add_txn(db, chk.id, "2026-10-01", 300, "VENMO OCT", category="TRANSFER_IN")
    bm = get(unlocked)
    assert (bm["income"]["received"], bm["income"]["pending"], bm["ready_to_assign"]) == (4000.0, 0.0, 3000.0)
    # The "earned more" note names the newest deposit counted in received.
    add_txn(db, chk.id, "2026-09-20", 250, "ZELLE CLIENT", category="TRANSFER_IN")
    more = event(unlocked)
    assert (more["kind"], more["amount"], more["name"], more["date"]) == ("more", 250.0, "ZELLE CLIENT", "2026-09-20")


def test_accept_received_skips_only_the_income_part(unlocked, db, fixed_today):
    """Reviewer probe: "Leave it for next month" may leave Ready to Assign below 0 only by the
    income that didn't come in; removing an overspent category with it is refused."""
    chk = make_account(db, "Checking", "bank", 3000)
    add_txn(db, chk.id, "2026-09-01", 2000, "PAY", category="INCOME")
    for m in ("2026-06", "2026-07", "2026-08"):
        add_txn(db, chk.id, f"{m}-01", 5000, "PAY", category="INCOME")
    add_txn(db, chk.id, "2026-09-05", -700, "FOOD", category="FOOD_AND_DRINK")
    add_budget(db, T, {"FOOD_AND_DRINK": 400, "ENTERTAINMENT": 100})
    fixed_today.set(dt.date(2026, 9, 29))
    ok(settings(unlocked, income_mode="expected"))
    # Cash 3,000 - available (-300 + 100) + pending 3,000 = 6,200.
    assert get(unlocked)["ready_to_assign"] == 6200.0
    ok(put(unlocked, assigned={"TRAVEL": 6150}))
    detail = "Only $50 is ready to assign. Lower another category first."
    assert err(put(unlocked, removed=["FOOD_AND_DRINK"])) == detail  # -300 released
    assert err(put(unlocked, income_expected=2000, accept_received=True, removed=["FOOD_AND_DRINK"])) == (
        "Leave it for next month can't remove a category."
    )
    # Lowering a plan with it is fine; only the $3,000 that didn't come in goes negative.
    bm = ok(put(unlocked, income_expected=2000, accept_received=True, assigned={"TRAVEL": 6100}))
    assert bm["ready_to_assign"] == 50 + 50 - 3000.0
    db.expire_all()
    assert db.scalar(select(Budget.removed).where(Budget.category == "FOOD_AND_DRINK")) is False


def test_setup_card_debt_reduces_what_can_be_planned(unlocked, db, fixed_today):
    chk = make_account(db, "Checking", "bank", 500)
    make_account(db, "Visa", "credit", 2500)  # owes 2,500: cash -2,000
    add_txn(db, chk.id, "2026-08-01", 5000, "PAY", category="INCOME")
    info = ok(unlocked.get("/api/budgets/setup"))
    assert (info["existing_money"], info["owed_beyond_cash"]) == (0.0, 2000.0)
    body = {"income_expected": 5000, "keep_existing_in_savings": True}
    assert err(unlocked.post("/api/budgets/setup", json={**body, "answers": {"bills": 3000, "groceries": 2000}})) == (
        "Your cards owe $2,000 more than your accounts hold, so plan at most $3,000. Lower an amount to continue."
    )
    assert unlocked.post("/api/budgets/setup", json={**body, "answers": {"bills": 2000, "groceries": 1000.01}}).status_code == 422
    assert get(unlocked)["setup_needed"] is True
    bm = ok(unlocked.post("/api/budgets/setup", json={**body, "answers": {"bills": 2000, "groceries": 1000}}), 201)
    # -2,000 cash - 3,000 planned + 5,000 still expected: nothing over-assigned.
    assert (bm["ready_to_assign"], bm["month_money"], bm["assigned"]) == (0.0, 3000.0, 3000.0)


def test_income_alert_reports_the_new_deposits_part(world, unlocked, db, fixed_today):
    chk, _card, _hsa = world
    ok(settings(unlocked, income_mode="expected"))
    ok(put(unlocked, income_expected=3000))
    first = add_txn(db, chk.id, "2026-09-25", 420, "BONUS", merchant="Acme Corp", category="INCOME")
    evaluate(db, FIXED_TODAY, new_transaction_ids=[first.id])
    db.commit()
    assert income_alerts(db) == [("$120 more than expected came in", "Acme Corp on Checking. It's under Not planned yet.")]
    # Already over: the next deposit reports its own amount, not the month's whole surplus.
    tip = add_txn(db, chk.id, "2026-09-25", 50, "VENMO TIP", category="TRANSFER_IN")
    evaluate(db, FIXED_TODAY, new_transaction_ids=[tip.id])
    db.commit()
    assert income_alerts(db)[-1] == ("$50 more than expected came in", "VENMO TIP on Checking. It's under Not planned yet.")
    # A deposit synced late and dated last month is last month's income: no alert. A matched
    # transfer isn't announced either.
    fixed_today.set(dt.date(2026, 9, 3))
    late = add_txn(db, chk.id, "2026-08-30", 900, "LATE PAY", merchant="Acme Corp", category="INCOME")
    moved = add_txn(db, chk.id, "2026-09-02", 900, "FROM SAVINGS", category="TRANSFER_IN", is_transfer=True)
    evaluate(db, dt.date(2026, 9, 3), new_transaction_ids=[late.id, moved.id])
    db.commit()
    assert len(income_alerts(db)) == 2
    # With a late one and a new one together, only the new one counts.
    late2 = add_txn(db, chk.id, "2026-08-31", 700, "LATE PAY 2", category="INCOME")
    new = add_txn(db, chk.id, "2026-09-02", 30, "INTEREST", category="INCOME")
    evaluate(db, dt.date(2026, 9, 3), new_transaction_ids=[late2.id, new.id])
    db.commit()
    assert income_alerts(db)[-1] == ("$30 more than expected came in", "INTEREST on Checking. It's under Not planned yet.")


def test_income_fields_are_strictly_typed(world, unlocked):
    ok(settings(unlocked, income_mode="expected"))
    for body in ({"income_expected": "5000"}, {"income_expected": True}, {"income_expected": 5000, "restore": "yes"},
                 {"income_expected": 5000, "restore": 1}, {"income_expected": 2700, "accept_received": "true"},
                 {"income_expected": 2700, "accept_received": 1}):
        assert put(unlocked, **body).status_code == 422, body
    assert ok(put(unlocked, income_expected=5000))["income"]["expected"] == 5000.0  # a JSON integer is fine
    assert ok(put(unlocked, income_expected=4999.5, restore=True))["income"]["expected"] == 4999.5

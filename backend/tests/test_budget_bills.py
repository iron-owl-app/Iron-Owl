"""Release 3.6 "Budget for a beginner", phase 2: bills per category (from the calendar's
occurrences), the ``bills`` target, income from occurrences, the setup's Bills row and
``bills`` targets, and the built-in mapping of Plaid's detailed categories (schema v7).

Today is 2026-09-26 (conftest.FIXED_TODAY); every number is worked out by hand.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlalchemy import select

from app import db as dbmod
from app import migrations
from app.models import AppSetting, CategoryGroup, RecurringOverride, Transaction, TxnCategory
from app.services import automap
from app.services import rules as rules_service
from app.services import sync as sync_service
from tests.conftest import PASSWORD, add_budget, add_txn, make_account
from tests.test_budget_beginner import ANSWERS, fresh, setup  # noqa: F401 - fixture
from tests.test_migrations import REAL_LATEST, columns, raw
from tests.test_r36_calendar import build_v5_vault, make_item

T = "2026-09"
D = dt.date.fromisoformat


def ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json() if response.content else None


def err(response, status=422) -> str:
    assert response.status_code == status, response.text
    return response.json()["detail"]


def get(client, month=None) -> dict:
    return ok(client.get("/api/budgets", params={"month": month} if month else None))


def line(bm, category) -> dict:
    return next(c for c in bm["categories"] if c["category"] == category)


def override(db, item, base, **kw) -> None:
    db.add(RecurringOverride(recurring_id=item.id, base_date=D(base), **kw))
    db.commit()


# ------------------------------------------------------------------ bills per category


@pytest.fixture
def bills(unlocked, db, fixed_today):
    """Checking (+5,000) and a Visa (−300). September bills by budget category:

    Rent and utilities: Landlord Sep 1 paid 1,460 (expected 1,450) + Power Sep 28 120
    (not counted in the forecast, still a bill) = 1,580.
    Services: Gym Sep 10 skipped (0); Phone's Sep 20 moved to Oct 2 (out) and its Oct 20
    moved to Sep 29 (in) 80; Insurance on the card, Sep 15 late, 100 → 180.
    Entertainment (not in the plan): Netflix on the card, Sep 5 late, 15.49.
    """
    chk = make_account(db, "Checking", "bank", 5000)
    card = make_account(db, "Visa", "credit", 300)
    make_item(db, "Landlord", -1450, "monthly", "2026-10-01", account=chk, category="RENT_AND_UTILITIES")
    add_txn(db, chk.id, "2026-09-01", -1460, "Landlord", category="RENT_AND_UTILITIES")
    make_item(db, "Power Co", -120, "monthly", "2026-09-28", account=chk, category="RENT_AND_UTILITIES", include=False)
    gym = make_item(db, "Gym", -40, "monthly", "2026-10-10", account=chk, category="GENERAL_SERVICES")
    override(db, gym, "2026-09-10", skipped=True)
    phone = make_item(db, "Phone", -80, "monthly", "2026-10-20", account=chk, category="GENERAL_SERVICES")
    override(db, phone, "2026-09-20", moved_to=D("2026-10-02"))
    override(db, phone, "2026-10-20", moved_to=D("2026-09-29"))
    make_item(db, "Insurance", -100, "monthly", "2026-10-15", account=card, category="GENERAL_SERVICES")
    make_item(db, "Netflix", -15.49, "monthly", "2026-10-05", account=card, category="ENTERTAINMENT")
    add_txn(db, chk.id, "2026-06-10", -20, "Old coffee", category="FOOD_AND_DRINK")  # June is viewable
    ok(unlocked.put(f"/api/budgets/{T}", json={"assigned": {"RENT_AND_UTILITIES": 1500, "GENERAL_SERVICES": 50}}))
    return chk, card


def test_bills_in_each_category(bills, unlocked):
    bm = get(unlocked)
    assert bm["bills_available"] is True
    rent = line(bm, "RENT_AND_UTILITIES")["bills"]
    assert rent["total"] == 1580.0
    assert [(b["name"], b["date"], b["amount"], b["status"], b["actual_amount"], b["paid_date"]) for b in rent["items"]] == [
        ("Landlord", "2026-09-01", 1450.0, "paid", 1460.0, "2026-09-01"),  # paid at the actual amount
        ("Power Co", "2026-09-28", 120.0, "upcoming", None, None),  # include_in_forecast off: still a bill
    ]
    assert rent["items"][0]["key"].startswith("r") and isinstance(rent["items"][0]["recurring_id"], int)
    services = line(bm, "GENERAL_SERVICES")["bills"]
    assert [(b["name"], b["date"], b["status"]) for b in services["items"]] == [
        ("Gym", "2026-09-10", "skipped"), ("Insurance", "2026-09-15", "late"), ("Phone", "2026-09-29", "upcoming"),
    ]
    # Skipped costs nothing; the card's bill counts; Phone moved one in and one out.
    assert services["total"] == 180.0
    # Bill categories outside the plan.
    assert bm["bills_outside"] == [{"category": "ENTERTAINMENT", "name": "Entertainment",
                                    "hue": bm["bills_outside"][0]["hue"], "total": 15.49}]
    # Next month: one of each; Phone's Oct 2 moved in; the Oct 20 one moved out.
    october = get(unlocked, "2026-10")
    assert line(october, "RENT_AND_UTILITIES")["bills"]["total"] == 1570.0
    assert [(b["name"], b["date"]) for b in line(october, "GENERAL_SERVICES")["bills"]["items"]] == [
        ("Phone", "2026-10-02"), ("Gym", "2026-10-10"), ("Insurance", "2026-10-15")]


def test_old_months_have_no_bills(bills, unlocked):
    july = get(unlocked, "2026-07")  # ended Jul 31, within 62 days of Sep 26
    assert july["bills_available"] is True
    june = get(unlocked, "2026-06")  # ended more than 62 days ago
    assert june["bills_available"] is False and june["bills_outside"] == []
    assert all(c["bills"] is None for c in june["categories"])


def test_a_category_without_bills_has_none(bills, unlocked, db):
    ok(unlocked.put(f"/api/budgets/{T}", json={"assigned": {"TRAVEL": 10}}))
    assert line(get(unlocked), "TRAVEL")["bills"] is None


# ------------------------------------------------------------------ the bills target


def patch_target(client, category, target):
    return client.patch(f"/api/categories/{category}", json={"target": target})


def test_bills_target(bills, unlocked, db):
    assert err(patch_target(unlocked, "GENERAL_SERVICES", {"kind": "bills", "amount": 180})) == (
        "A bills target has no amount: it follows your bills each month.")
    assert err(patch_target(unlocked, "GENERAL_SERVICES", {"kind": "bills", "date": "2026-12-01"})) == (
        "A bills target has no date.")
    assert err(patch_target(unlocked, "GENERAL_SERVICES", {"kind": "monthly"})) == "The target amount must be more than $0."
    out = ok(patch_target(unlocked, "GENERAL_SERVICES", {"kind": "bills", "amount": None, "date": None}))
    assert out["target"] == {"kind": "bills", "amount": None, "date": None}
    cats = {c["id"]: c for c in ok(unlocked.get("/api/categories"))}
    assert cats["GENERAL_SERVICES"]["target"] == {"kind": "bills", "amount": None, "date": None}
    # In the month: amount = this month's bills; needed = bills − assigned (carryover ignored).
    bm = get(unlocked)
    assert line(bm, "GENERAL_SERVICES")["target"] == {"kind": "bills", "amount": 180.0, "date": None, "needed": 130.0}
    assert bm["needed_total"] == 130.0
    october = get(unlocked, "2026-10")
    assert line(october, "GENERAL_SERVICES")["target"]["needed"] == 220.0  # 80 + 40 + 100, nothing planned
    # Fund targets covers it.
    res = ok(unlocked.post(f"/api/budgets/{T}/fund-targets"))
    assert (res["funded"], res["unfunded"]) == (130.0, 0.0)
    assert line(res["month"], "GENERAL_SERVICES")["assigned"] == 180.0
    assert line(res["month"], "GENERAL_SERVICES")["target"]["needed"] == 0.0
    # A skip lowers what's needed.
    phone = db.scalar(select(RecurringOverride).where(RecurringOverride.base_date == D("2026-10-20")))
    ok(unlocked.put(f"/api/recurring/{phone.recurring_id}/occurrences/2026-10-20", json={"moved_to": None, "skipped": True}))
    assert line(get(unlocked), "GENERAL_SERVICES")["bills"]["total"] == 100.0
    # Changing the kind away from spending clears it.
    out = ok(unlocked.patch("/api/categories/GENERAL_SERVICES", json={"kind": "fixed"}))
    assert out["target"] is None
    db.expire_all()
    row = db.get(TxnCategory, "GENERAL_SERVICES")
    assert (row.target_kind, row.target_cents, row.target_date) == (None, None, None)


def test_bills_target_isnt_a_calendar_plan(bills, unlocked):
    ok(patch_target(unlocked, "RENT_AND_UTILITIES", {"kind": "bills"}))
    cal = ok(unlocked.get("/api/forecast/calendar", params={"from": "2026-09-01", "to": "2026-10-31"}))
    assert not [o for o in cal["occurrences"] if o["kind"] == "plan"]
    assert not [s for s in cal["series"] if s["kind"] == "plan"]


def test_suggestion_numbers(bills, unlocked):
    """The "Your bills here add up to $X" line shows when bills.total > assigned (client)."""
    bm = get(unlocked)
    rent, services = line(bm, "RENT_AND_UTILITIES"), line(bm, "GENERAL_SERVICES")
    assert rent["bills"]["total"] > rent["assigned"] and services["bills"]["total"] > services["assigned"]
    ok(unlocked.put(f"/api/budgets/{T}", json={"assigned": {"RENT_AND_UTILITIES": 1580, "GENERAL_SERVICES": 180}}))
    bm = get(unlocked)
    assert all(line(bm, c)["bills"]["total"] <= line(bm, c)["assigned"] for c in ("RENT_AND_UTILITIES", "GENERAL_SERVICES"))


# ------------------------------------------------------------------ income from occurrences


@pytest.fixture
def paychecks(unlocked, db, fixed_today):
    chk = make_account(db, "Checking", "bank", 3000)
    make_item(db, "Acme Payroll", 2000, "biweekly", "2026-10-02", account=chk)
    add_txn(db, chk.id, "2026-09-04", 2000, "Acme Payroll", category="INCOME")
    # Plaid filed this one under Other: matched to the paycheck, so it still came in.
    add_txn(db, chk.id, "2026-09-18", 2000, "Acme Payroll", category="OTHER")
    return chk


def test_received_adds_matched_deposits_once(paychecks, unlocked, db):
    # With expected income off, nothing changes: only income-kind money counts.
    assert get(unlocked)["income"]["received"] == 2000.0
    ok(unlocked.put("/api/budgets/settings", json={"income_mode": "expected"}))
    bm = get(unlocked)
    assert bm["income"]["received"] == 4000.0  # 2,000 income + 2,000 matched (the income one isn't counted twice)
    assert bm["income"]["next"] == {"date": "2026-10-02", "name": "Acme Payroll", "amount": 2000.0}


def test_matched_transfer_isnt_received(paychecks, unlocked, db):
    ok(unlocked.put("/api/budgets/settings", json={"income_mode": "expected"}))
    txn = db.scalar(select(Transaction).where(Transaction.category == "OTHER"))
    txn.is_transfer = True
    db.commit()
    assert get(unlocked)["income"]["received"] == 2000.0


def test_next_paycheck_follows_moves_and_skips(paychecks, unlocked, db):
    item_id = ok(unlocked.get("/api/recurring"))[0]["id"]
    url = f"/api/recurring/{item_id}/occurrences"
    ok(unlocked.put(f"{url}/2026-10-02", json={"skipped": True}))
    assert get(unlocked)["income"]["next"]["date"] == "2026-10-16"
    ok(unlocked.put(f"{url}/2026-10-16", json={"moved_to": "2026-10-14"}))
    assert get(unlocked)["income"]["next"]["date"] == "2026-10-14"


def test_missed_paycheck_names_the_event(unlocked, db, fixed_today):
    chk = make_account(db, "Checking", "bank", 1000)
    make_item(db, "Big Client", 3000, "monthly", "2026-10-15", account=chk)
    ok(unlocked.put("/api/budgets/settings", json={"income_mode": "expected"}))
    ok(unlocked.put(f"/api/budgets/{T}", json={"income_expected": 3000}))
    event = get(unlocked)["income"]["event"]
    assert event == {"kind": "less", "amount": 3000.0, "date": "2026-09-15", "name": "Big Client",
                     "expected": 3000.0, "received": 0.0, "reason": "missing", "can_use_unplanned": True}
    # A paycheck a few days late ("pending") may still come: no note yet.
    fixed_today.set(dt.date(2026, 9, 17))
    assert get(unlocked)["income"]["event"] is None


# ------------------------------------------------------------------ setup: Bills row and targets


@pytest.fixture
def fresh_bills(fresh, db):
    make_item(db, "RENT", -1200, "monthly", "2026-10-01", account=fresh, category="RENT_AND_UTILITIES")
    make_item(db, "Water", -60, "monthly", "2026-09-28", account=fresh, category="RENT_AND_UTILITIES")
    make_item(db, "Netflix", -15.49, "monthly", "2026-10-05", account=fresh, category="ENTERTAINMENT")
    return fresh


def test_setup_info_bills_row(fresh_bills, unlocked):
    info = ok(unlocked.get("/api/budgets/setup"))
    # Entertainment's usual other spending: its 90 average minus its 15.49 bill.
    assert info["bills"] == {"total": 1275.49, "count": 3,
                             "by_category": {"RENT_AND_UTILITIES": 1260.0, "ENTERTAINMENT": 15.49},
                             "extra": 74.51, "extra_categories": ["Entertainment"]}
    rows = {r["key"]: r for r in info["rows"]}
    # Entertainment has a bill: it joins the Bills row (and leaves Fun).
    assert rows["bills"]["categories"] == ["Rent and utilities", "Services", "Entertainment"]
    # The suggestion is the bills plus Entertainment's other spending: 1,275.49 + 74.51.
    assert (rows["bills"]["chips"], rows["bills"]["suggested"]) == ([1350.0, 1400.0, 1450.0], 1350.0)
    assert rows["fun"]["categories"] == ["Travel"]


def test_setup_plans_bills_and_sets_bills_targets(fresh_bills, unlocked, db):
    bm = ok(setup(unlocked), 201)
    assigned = {c["category"]: c["assigned"] for c in bm["categories"]}
    # 1,300.01: each category's bills first (1,260 and 15.49); the 24.52 left goes to
    # Entertainment's other spending (74.51) before anything is split by average.
    assert assigned["RENT_AND_UTILITIES"] == 1260.0
    assert assigned["GENERAL_SERVICES"] == 0.0
    assert assigned["ENTERTAINMENT"] == 40.01
    assert assigned["TRAVEL"] == 100.0  # Fun is now just Travel
    assert line(bm, "RENT_AND_UTILITIES")["target"] == {"kind": "bills", "amount": 1260.0, "date": None, "needed": 0.0}
    assert line(bm, "ENTERTAINMENT")["target"]["kind"] == "bills"
    assert line(bm, "GENERAL_SERVICES")["target"] is None  # no bills: no target
    groups = {g["name"]: g["category_ids"] for g in ok(unlocked.get("/api/category-groups"))}
    assert set(groups["Bills"]) == {"RENT_AND_UTILITIES", "GENERAL_SERVICES", "ENTERTAINMENT"}
    assert "ENTERTAINMENT" not in groups["Fun"]


@pytest.mark.parametrize("answer, expect", [
    # The suggestion: bills, then Entertainment's other spending (15.49 + 74.51 = its average).
    (1350, {"RENT_AND_UTILITIES": 1260.0, "GENERAL_SERVICES": 0.0, "ENTERTAINMENT": 90.0}),
    # 50 more than that: split by average (1,200 : 100 : 90), the leftover cents to the largest.
    (1400, {"RENT_AND_UTILITIES": 1303.18, "GENERAL_SERVICES": 3.59, "ENTERTAINMENT": 93.23}),
])
def test_setup_plans_a_mixed_category_for_its_other_spending(fresh_bills, unlocked, db, answer, expect):
    """Entertainment has a 15.49 streaming bill and movie nights: moved into the Bills row, its
    plan is the bill plus its usual other spending, so it isn't "Over" on day one."""
    add_txn(db, fresh_bills.id, "2026-09-12", -60, "MOVIES", category="ENTERTAINMENT")
    bm = ok(setup(unlocked, answers={**ANSWERS, "bills": answer}), 201)
    assert {c: line(bm, c)["assigned"] for c in expect} == expect
    ent = line(bm, "ENTERTAINMENT")
    # The bills target stays; it's covered.
    assert ent["target"] == {"kind": "bills", "amount": 15.49, "date": None, "needed": 0.0}
    assert ent["available"] >= 0 and ent["spent"] == 60.0


def test_setup_keeps_an_existing_target(fresh_bills, unlocked, db):
    row = db.get(TxnCategory, "RENT_AND_UTILITIES")
    row.target_kind, row.target_cents = "monthly", 130000
    db.commit()
    bm = ok(setup(unlocked), 201)
    assert line(bm, "RENT_AND_UTILITIES")["target"]["kind"] == "monthly"


def test_setup_records_the_auto_map_and_sorts_coded_transactions(fresh, unlocked, db):
    coded = add_txn(db, fresh.id, "2026-09-17", -80, "SAFEWAY", category="FOOD_AND_DRINK",
                    plaid_detailed="FOOD_AND_DRINK_GROCERIES")
    bm = ok(setup(unlocked), 201)
    groceries = next(c["category"] for c in bm["categories"] if c["name"] == "Groceries")
    eating = next(c["category"] for c in bm["categories"] if c["name"] == "Eating out")
    gas = next(c["category"] for c in bm["categories"] if c["name"] == "Gas")
    db.expire_all()
    assert json.loads(db.get(AppSetting, automap.SETTING).value) == {"groceries": groceries, "eating_out": eating, "gas": gas}
    txn = db.get(Transaction, coded.id)
    assert (txn.category, txn.category_source) == (groceries, "auto")
    assert line(bm, groceries)["spent"] == 80.0
    # The older one without a code stays under Food and drink (Spending without a plan).
    assert [u["category"] for u in bm["unbudgeted"]] == ["FOOD_AND_DRINK"]


def test_setup_screen_counts_what_the_sorting_will_move(fresh, unlocked, db):
    """The setup screen shows the numbers the setup will really use: coded grocery spending
    counts as Groceries (its average, and "the money you already had"), except where a user
    rule wins. Before, the screen promised 2,230 and the setup kept 2,310 in savings."""
    for day, amount in (("2026-07-17", -100), ("2026-08-17", -120), ("2026-09-17", -80)):
        add_txn(db, fresh.id, day, amount, "SAFEWAY", category="FOOD_AND_DRINK", plaid_detailed="FOOD_AND_DRINK_GROCERIES")
    add_txn(db, fresh.id, "2026-09-18", -30, "COSTCO", merchant="Costco", category="FOOD_AND_DRINK",
            plaid_detailed="FOOD_AND_DRINK_GROCERIES")
    ok(unlocked.post("/api/rules", json={"field": "merchant", "op": "is", "text": "Costco", "action": "category",
                                         "category": "GENERAL_MERCHANDISE"}), 201)
    info = ok(unlocked.get("/api/budgets/setup"))
    rows = {r["key"]: r for r in info["rows"]}
    # June has no detailed codes (history from before schema v7): no average from them yet.
    assert (rows["groceries"]["average"], rows["groceries"]["chips"]) == (None, [400.0, 600.0, 800.0])
    # 3,000 cash + September's planned spending (rent 1,200, groceries 80, Costco 30 by the
    # rule under Shopping) − 2,000 pay. Food and drink's own 150 stays without a plan.
    assert info["existing_money"] == 2310.0
    add_txn(db, fresh.id, "2026-06-17", -60, "SAFEWAY", category="FOOD_AND_DRINK", plaid_detailed="FOOD_AND_DRINK_GROCERIES")
    info = ok(unlocked.get("/api/budgets/setup"))
    rows = {r["key"]: r for r in info["rows"]}
    # Every month has codes now: (60 + 100 + 120) / 3.
    assert (rows["groceries"]["average"], rows["groceries"]["chips"], rows["groceries"]["suggested"]) == (
        93.33, [50.0, 100.0], 100.0)
    assert rows["gas"]["average"] is None  # nothing coded for gas: the design's chips
    assert info["existing_money"] == 2310.0
    bm = ok(setup(unlocked), 201)
    savings = bm["savings_category"]
    assert line(bm, savings)["carryover"] == info["existing_money"]
    assert bm["month_money"] == 4000.0


# ------------------------------------------------------------------ the built-in mapping


@pytest.fixture
def mapped(unlocked, db, fixed_today):
    chk = make_account(db, "Checking", "bank", 1000)
    groceries = TxnCategory(id="c_1", name="Groceries", hue=10, kind="spending", custom=True, hidden=False, position=90)
    eating = TxnCategory(id="c_2", name="Eating out", hue=20, kind="spending", custom=True, hidden=False, position=91)
    db.add_all([groceries, eating])
    db.commit()
    automap.record(db, {"groceries": "c_1", "eating_out": "c_2", "gas": "c_404"})  # Gas was deleted
    db.commit()

    def txn(name, detailed, **kw):
        return add_txn(db, chk.id, "2026-09-20", -10, name, category=kw.pop("category", "FOOD_AND_DRINK"),
                       plaid_detailed=detailed, **kw).id

    ids = {
        "grocer": txn("Safeway", "FOOD_AND_DRINK_GROCERIES"),
        "coffee": txn("Blue Bottle", "FOOD_AND_DRINK_COFFEE"),
        "fast": txn("Burger Place", "FOOD_AND_DRINK_FAST_FOOD"),
        "diner": txn("Diner", "FOOD_AND_DRINK_RESTAURANT"),
        "liquor": txn("Wine Shop", "FOOD_AND_DRINK_BEER_WINE_AND_LIQUOR"),
        "gas": txn("Shell", "TRANSPORTATION_GAS", category="TRANSPORTATION"),
        "old": txn("Old Grocer", None),
        "hand": txn("Hand Grocer", "FOOD_AND_DRINK_GROCERIES", effective="TRAVEL", category_source="user"),
    }
    rules_service.apply_all(db)
    db.commit()
    return ids


def state(db, txn_id) -> tuple:
    db.expire_all()
    t = db.get(Transaction, txn_id)
    return t.category, t.category_source


def test_mapping_sorts_bank_set_transactions(mapped, db):
    assert state(db, mapped["grocer"]) == ("c_1", "auto")
    assert state(db, mapped["coffee"]) == ("c_2", "auto")
    assert state(db, mapped["fast"]) == ("c_2", "auto")
    assert state(db, mapped["diner"]) == ("c_2", "auto")
    assert state(db, mapped["liquor"]) == ("FOOD_AND_DRINK", "plaid")  # not mapped
    assert state(db, mapped["gas"]) == ("TRANSPORTATION", "plaid")  # its category no longer exists
    assert state(db, mapped["old"]) == ("FOOD_AND_DRINK", "plaid")  # no detailed code: untouched
    assert state(db, mapped["hand"]) == ("TRAVEL", "user")  # hand-set wins


def test_user_rules_win(mapped, unlocked, db):
    ok(unlocked.post("/api/rules", json={"field": "any", "op": "contains", "text": "safeway",
                                         "action": "category", "category": "GENERAL_MERCHANDISE"}), 201)
    assert state(db, mapped["grocer"]) == ("GENERAL_MERCHANDISE", "rule")
    rule_id = ok(unlocked.get("/api/rules"))[0]["id"]
    ok(unlocked.delete(f"/api/rules/{rule_id}"), 204)
    assert state(db, mapped["grocer"]) == ("c_1", "auto")


def test_mapping_is_settled_and_resets_like_automatic(mapped, unlocked, db):
    db.add(CategoryGroup(name="Everyday", position=0))
    db.commit()
    items = {t["id"]: t for t in ok(unlocked.get("/api/transactions"))["items"]}
    assert items[mapped["grocer"]]["category_source"] == "auto"
    assert items[mapped["grocer"]]["needs_category"] is False
    # Set by hand, then back to automatic: the mapping applies again.
    ok(unlocked.patch(f"/api/transactions/{mapped['grocer']}", json={"category": "TRAVEL"}))
    assert state(db, mapped["grocer"]) == ("TRAVEL", "user")
    ok(unlocked.patch(f"/api/transactions/{mapped['grocer']}", json={"category": None}))
    assert state(db, mapped["grocer"]) == ("c_1", "auto")
    # Undo accepts an "auto" state (it goes back to automatic).
    ok(unlocked.post("/api/transactions/restore",
                     json={"items": [{"id": mapped["grocer"], "category": "c_1", "category_source": "auto"}]}))
    assert state(db, mapped["grocer"]) == ("c_1", "auto")


def test_deleting_the_category_stops_the_mapping(mapped, unlocked, db):
    ok(unlocked.delete("/api/categories/c_1"), 204)
    assert state(db, mapped["grocer"]) == ("FOOD_AND_DRINK", "plaid")
    assert state(db, mapped["coffee"]) == ("c_2", "auto")


def test_a_hidden_category_isnt_mapped_into(mapped, unlocked, db):
    # Hiding it moves its sorted transactions out right away (no rule run needed) ...
    ok(unlocked.patch("/api/categories/c_2", json={"hidden": True}))
    assert state(db, mapped["coffee"]) == ("FOOD_AND_DRINK", "plaid")
    assert state(db, mapped["diner"]) == ("FOOD_AND_DRINK", "plaid")
    assert state(db, mapped["grocer"]) == ("c_1", "auto")  # other rows stay
    # ... automatic again, so "Needs a category" sees them (Food and drink isn't in a group).
    db.add(CategoryGroup(name="Everyday", position=0))
    db.commit()
    needs = {t["id"] for t in ok(unlocked.get("/api/transactions", params={"view": "needs_category"}))["items"]}
    assert {mapped["coffee"], mapped["diner"]} <= needs
    # Shown again: the sorting fills it again.
    ok(unlocked.patch("/api/categories/c_2", json={"hidden": False}))
    assert state(db, mapped["coffee"]) == ("c_2", "auto")


def test_a_category_no_longer_for_spending_isnt_mapped_into(mapped, unlocked, db):
    ok(unlocked.patch("/api/categories/c_1", json={"kind": "fixed"}))
    assert state(db, mapped["grocer"]) == ("FOOD_AND_DRINK", "plaid")
    assert state(db, mapped["coffee"]) == ("c_2", "auto")
    ok(unlocked.patch("/api/categories/c_1", json={"kind": "spending"}))
    assert state(db, mapped["grocer"]) == ("c_1", "auto")


def test_other_category_changes_dont_rerun_the_rules(mapped, unlocked, db, monkeypatch):
    calls = []
    monkeypatch.setattr(rules_service, "apply_all", lambda session: calls.append(1) or 0)
    ok(unlocked.patch("/api/categories/c_1", json={"name": "Food at home", "hue": 30}))  # not hidden or kind
    ok(unlocked.patch("/api/categories/c_1", json={"hidden": False}))  # unchanged
    ok(unlocked.patch("/api/categories/TRAVEL", json={"hidden": True}))  # not sorted into
    assert calls == []


def test_no_mapping_before_setup(unlocked, db, fixed_today):
    chk = make_account(db, "Checking", "bank", 1000)
    db.add(TxnCategory(id="c_1", name="Groceries", hue=10, kind="spending", custom=True, hidden=False, position=90))
    db.commit()
    tid = add_txn(db, chk.id, "2026-09-20", -10, "Safeway", plaid_detailed="FOOD_AND_DRINK_GROCERIES").id
    rules_service.apply_all(db)
    db.commit()
    assert state(db, tid) == ("FOOD_AND_DRINK", "plaid")


def test_valid_detailed():
    assert automap.valid_detailed("food_and_drink_groceries") == "FOOD_AND_DRINK_GROCERIES"
    for bad in (None, 5, "", "bad code!", "X" * 65, "_LEADING"):
        assert automap.valid_detailed(bad) is None


def test_sync_stores_the_detailed_code_and_maps(unlocked, db, fake_plaid, fixed_today):
    from tests.test_plaid import acct, exchange, txn

    db.add(TxnCategory(id="c_1", name="Groceries", hue=10, kind="spending", custom=True, hidden=False, position=90))
    db.commit()
    automap.record(db, {"groceries": "c_1"})
    db.commit()
    token = "access-sandbox-bank-0000-SECRET"
    fake_plaid.add_item("public-bank", token, "item_bank", "First Bank")
    fake_plaid.accounts[token] = [acct("chk", "Checking", "depository", "checking", 1000)]
    grocer = txn("g1", "chk", 50, "Safeway")
    grocer["personal_finance_category"] = {"primary": "FOOD_AND_DRINK", "detailed": "FOOD_AND_DRINK_GROCERIES"}
    odd = txn("o1", "chk", 5, "Odd")
    odd["personal_finance_category"] = {"primary": "FOOD_AND_DRINK", "detailed": "not a code!"}
    fake_plaid.txn_pages[token] = [{"cursor_in": None, "added": [grocer, odd, txn("p1", "chk", 9, "Pizza")],
                                    "modified": [], "removed": [], "next_cursor": "c1", "has_more": False}]
    exchange(unlocked, "public-bank", "bank")
    db.expire_all()
    rows = {t.plaid_transaction_id: t for t in db.scalars(select(Transaction))}
    assert (rows["g1"].plaid_detailed, rows["g1"].category, rows["g1"].category_source) == (
        "FOOD_AND_DRINK_GROCERIES", "c_1", "auto")
    assert rows["g1"].plaid_category == "FOOD_AND_DRINK"
    assert (rows["o1"].plaid_detailed, rows["o1"].category_source) == (None, "plaid")
    assert (rows["p1"].plaid_detailed, rows["p1"].category) == ("FOOD_AND_DRINK_OTHER", "FOOD_AND_DRINK")
    assert sync_service._txn_fields(grocer)["plaid_detailed"] == "FOOD_AND_DRINK_GROCERIES"  # noqa: SLF001


# ------------------------------------------------------------------ migration v7


def build_v6_vault(settings, monkeypatch) -> bytearray:
    key = build_v5_vault(settings, monkeypatch)
    monkeypatch.setattr(migrations, "LATEST", 6)
    database = dbmod.Database(settings.db_path)
    database.open(bytearray(key))
    database.close()
    monkeypatch.setattr(migrations, "LATEST", REAL_LATEST)
    return key


def test_v6_vault_upgrades_to_v7(settings, client, monkeypatch, fixed_today):
    key = build_v6_vault(settings, monkeypatch)
    conn = raw(settings, key)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
        assert "plaid_detailed" not in columns(conn, "transactions")
        before = conn.execute("SELECT count(*) FROM transactions").fetchone()[0]
    finally:
        conn.close()
    assert client.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200
    client.post("/api/auth/lock")
    conn = raw(settings, key)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == migrations.LATEST
        assert columns(conn, "transactions")["plaid_detailed"] == (0, None)
        assert conn.execute("SELECT count(*) FROM transactions WHERE plaid_detailed IS NULL").fetchone()[0] == before
    finally:
        conn.close()
    names = [p.name for p in settings.data_dir.iterdir() if p.name.startswith("pre-migrate-")]
    assert any(n.startswith("pre-migrate-v6-") for n in names)  # the safety copy was made


def test_failed_v7_migration_rolls_back_to_v6(settings, monkeypatch):
    key = build_v6_vault(settings, monkeypatch)

    def broken(conn):
        conn.execute("ALTER TABLE transactions ADD COLUMN plaid_detailed TEXT")
        conn.execute("ALTER TABLE nope ADD COLUMN x TEXT")

    monkeypatch.setitem(migrations.MIGRATIONS, 7, broken)
    database = dbmod.Database(settings.db_path)
    with pytest.raises(Exception):  # noqa: B017 - sqlcipher3.OperationalError
        database.open(bytearray(key))
    conn = raw(settings, key)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
        assert "plaid_detailed" not in columns(conn, "transactions")
    finally:
        conn.close()


def test_last_month_shows_its_bills(bills, unlocked, db):
    """A past month (view only) lists its own bills: Landlord Aug 1 and Power Co Aug 28."""
    add_budget(db, "2026-08", {"RENT_AND_UTILITIES": 1450})
    august = get(unlocked, "2026-08")
    assert line(august, "RENT_AND_UTILITIES")["bills"]["total"] == 1570.0


# ------------------------------------------------------------------ security


def test_bills_target_needs_session_csrf_and_strict_body(bills, unlocked, client):
    from fastapi.testclient import TestClient

    from app.security import SESSION_HEADER
    from tests.conftest import BASE_URL

    url = "/api/categories/GENERAL_SERVICES"
    anon = TestClient(unlocked.app, base_url=BASE_URL, headers={SESSION_HEADER: unlocked.headers[SESSION_HEADER]})
    anon.cookies = unlocked.cookies
    assert anon.patch(url, json={"target": {"kind": "bills"}}).status_code == 403
    assert unlocked.patch(url, json={"target": {"kind": "bills", "sneaky": 1}}).status_code == 422
    assert unlocked.patch(url, json={"target": {"kind": "weekly"}}).status_code == 422
    r = unlocked.request("PATCH", url, content='{"target": {"kind": "bills", "amount": NaN}}',
                         headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    unlocked.post("/api/auth/lock")
    assert unlocked.get("/api/budgets").status_code == 401


# ------------------------------------------------------------------ review fixes (phase 2)


def test_bills_only_count_on_budget_accounts_and_cards(bills, unlocked, db):
    """A bill paid from an account outside the budget (savings, a brokerage) isn't budget
    spending; one on a credit card is, even when the card isn't a budget account."""
    savings = make_account(db, "Savings", "savings", 9000)
    other = make_account(db, "Side account", "other", 100)  # "other" accounts aren't in by default
    make_item(db, "Storage unit", -75, "monthly", "2026-10-12", account=savings, category="GENERAL_SERVICES")
    make_item(db, "Club dues", -30, "monthly", "2026-10-14", account=other, category="GENERAL_SERVICES")
    bm = get(unlocked)
    names = [b["name"] for b in line(bm, "GENERAL_SERVICES")["bills"]["items"]]
    assert "Storage unit" not in names and "Club dues" not in names
    assert line(bm, "GENERAL_SERVICES")["bills"]["total"] == 180.0
    # Leave the Visa out of the budget: its bills still count (a card bill is spending).
    chk, card = bills
    ok(unlocked.put("/api/budgets/accounts", json={"account_ids": [chk.id]}))
    bm = get(unlocked)
    assert "Insurance" in [b["name"] for b in line(bm, "GENERAL_SERVICES")["bills"]["items"]]
    assert bm["bills_outside"][0]["category"] == "ENTERTAINMENT"  # Netflix on the card
    # Put the side account in the budget: its bill counts from then on.
    ok(unlocked.put("/api/budgets/accounts", json={"account_ids": [chk.id, other.id]}))
    assert line(get(unlocked), "GENERAL_SERVICES")["bills"]["total"] == 210.0


@pytest.fixture
def biweekly(unlocked, db, fixed_today):
    """The e2e vault's pay: 2,100 every other Friday (Sep 25, Sep 11, Aug 28 ... Jun 5).
    June-August averages 4,900 (July had three paychecks); September has two: 4,200."""
    chk = make_account(db, "Checking", "bank", 6000)
    day = D("2026-09-25")
    while day >= D("2026-06-01"):
        add_txn(db, chk.id, day.isoformat(), 2100, "ACME PAYROLL", category="INCOME")
        day -= dt.timedelta(days=14)
    for month in ("06", "07", "08", "09"):
        add_txn(db, chk.id, f"2026-{month}-01", -1450, "RENT", category="RENT_AND_UTILITIES")
    make_item(db, "ACME PAYROLL", 2100, "biweekly", "2026-10-09", account=chk)
    return chk


def run(client, income):
    return ok(client.post("/api/budgets/setup", json={"answers": ANSWERS, "income_expected": income,
                                                      "keep_existing_in_savings": False}), 201)


def test_setup_starts_from_this_months_paychecks(biweekly, unlocked):
    info = ok(unlocked.get("/api/budgets/setup"))
    # The average would say 4,900, but the calendar knows September has two paychecks, both
    # in. Settings D7: the suggestion itself is now this month's paychecks (2 x 2,100).
    assert info["income"] == {"suggested": 4200.0, "suggested_source": "paychecks", "received": 4200.0,
                              "expected_more": 0.0, "start": 4200.0}
    bm = run(unlocked, info["income"]["start"])
    assert bm["income"]["event"] is None  # no "Paycheck was short" right after setup


def test_setup_average_above_this_month_is_short(biweekly, unlocked):
    """Picking more than came in + is still expected is the one way to see the note now."""
    bm = run(unlocked, 4900)
    assert bm["income"]["event"]["kind"] == "less" and bm["income"]["event"]["amount"] == 700.0


def test_setup_counts_paychecks_still_to_come(biweekly, unlocked, fixed_today):
    fixed_today.set(dt.date(2026, 10, 2))  # October: Oct 9 and Oct 23 still to come
    info = ok(unlocked.get("/api/budgets/setup"))
    assert (info["income"]["received"], info["income"]["expected_more"], info["income"]["start"]) == (0.0, 4200.0, 4200.0)
    assert run(unlocked, 4200)["income"]["event"] is None


def test_setup_start_follows_the_calendar_month(unlocked, db, fixed_today):
    chk = make_account(db, "Checking", "bank", 6000)
    for month in ("06", "07", "08"):
        add_txn(db, chk.id, f"2026-{month}-05", 4000, "PAY", category="INCOME")
    make_item(db, "PAY", 2000, "biweekly", "2026-10-02", account=chk)  # Sep 4, Sep 18 (and Oct 2)
    add_txn(db, chk.id, "2026-09-04", 2000, "PAY", category="INCOME")
    info = ok(unlocked.get("/api/budgets/setup"))
    # Sep 18 didn't come (8 days late): what came in + what's still due is the month.
    assert (info["income"]["suggested"], info["income"]["received"], info["income"]["expected_more"]) == (
        4000.0, 2000.0, 0.0)
    assert info["income"]["start"] == 2000.0
    # No paycheck on the calendar: the larger of the average and what came in.
    ok(unlocked.delete(f"/api/recurring/{ok(unlocked.get('/api/recurring'))[0]['id']}"), 204)
    assert ok(unlocked.get("/api/budgets/setup"))["income"]["start"] == 4000.0


def test_setup_received_counts_matched_deposits(paychecks, unlocked):
    """Setup switches expected income on, so a paycheck Plaid filed under Other already counts."""
    info = ok(unlocked.get("/api/budgets/setup"))
    assert info["income"]["received"] == 4000.0
    assert get(unlocked)["income"]["received"] == 2000.0  # the budget itself is unchanged until then
    msg = err(unlocked.post("/api/budgets/setup", json={"answers": {}, "income_expected": 3000,
                                                         "keep_existing_in_savings": False, "skip": True}))
    assert msg == "$4,000 has already come in this month, so pick at least $4,000."


def test_setup_groups_income_so_paychecks_dont_need_a_category(fresh, unlocked, db):
    run(unlocked, 4000)
    groups = {g["name"]: g["category_ids"] for g in ok(unlocked.get("/api/category-groups"))}
    assert groups["Income"] == ["INCOME"]
    summary = ok(unlocked.get("/api/transactions/summary", params={"view": "needs_category", "start": "2026-09-01"}))
    assert summary["counts"]["needs_category"] == 0  # the Sep 15 paycheck (Income) isn't flagged
    assert "needs_category" not in [a["kind"] for a in ok(unlocked.get("/api/dashboard"))["needs"]]
    # Without the Income group the paycheck would need a category.
    db.get(TxnCategory, "INCOME").group_id = None
    db.commit()
    alert = next(a for a in ok(unlocked.get("/api/dashboard"))["needs"] if a["kind"] == "needs_category")
    assert alert["data"]["count"] == 1


def test_setup_leaves_existing_groups_alone(fresh, unlocked, db):
    ok(unlocked.post("/api/category-groups", json={"name": "Mine"}), 201)
    run(unlocked, 4000)
    assert [g["name"] for g in ok(unlocked.get("/api/category-groups"))] == ["Mine"]
    db.expire_all()
    assert db.get(TxnCategory, "INCOME").group_id is None


def test_undo_puts_back_a_past_by_date_target(bills, unlocked, db, fixed_today):
    past = {"kind": "by_date", "amount": 600, "date": "2026-09-30"}
    ok(patch_target(unlocked, "GENERAL_SERVICES", past))
    fixed_today.set(dt.date(2026, 10, 3))  # the date has passed
    ok(patch_target(unlocked, "GENERAL_SERVICES", {"kind": "bills"}))
    assert err(patch_target(unlocked, "GENERAL_SERVICES", past)) == "The target date must be today or later."
    out = ok(unlocked.patch("/api/categories/GENERAL_SERVICES", json={"target": past, "restore": True}))
    assert out["target"] == {"kind": "by_date", "amount": 600.0, "date": "2026-09-30"}
    # Every other rule still applies; restore needs a target and must be a real boolean.
    zero = {"kind": "by_date", "amount": 0, "date": "2026-09-30"}
    assert unlocked.patch("/api/categories/GENERAL_SERVICES", json={"target": zero, "restore": True}).status_code == 422
    assert err(unlocked.patch("/api/categories/GENERAL_SERVICES", json={"restore": True})) == "Undo needs the target to put back."
    assert unlocked.patch("/api/categories/GENERAL_SERVICES", json={"target": None, "restore": "yes"}).status_code == 422


def test_undo_of_remove_puts_back_a_negative_plan(unlocked, db, fixed_today):
    make_account(db, "Checking", "bank", 1000)
    add_budget(db, "2026-08", {"TRAVEL": 300})
    ok(unlocked.put(f"/api/budgets/{T}", json={"assigned": {"TRAVEL": -120}}))  # moved 120 out of the leftover
    bm = get(unlocked)
    before, ready = line(bm, "TRAVEL"), bm["ready_to_assign"]
    assert (before["carryover"], before["assigned"], before["available"]) == (300.0, -120.0, 180.0)
    ok(unlocked.put(f"/api/budgets/{T}", json={"removed": ["TRAVEL"]}))
    # A plain re-add starts fresh: a negative plan doesn't fit.
    assert err(unlocked.put(f"/api/budgets/{T}", json={"assigned": {"TRAVEL": -120}})) == (
        "You can move at most $0 out of Travel.")
    bm = ok(unlocked.put(f"/api/budgets/{T}", json={"assigned": {"TRAVEL": -120}, "restore": True}))
    after = line(bm, "TRAVEL")
    assert (after["carryover"], after["assigned"], after["available"]) == (300.0, -120.0, 180.0)
    assert bm["ready_to_assign"] == ready

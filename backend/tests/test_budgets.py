"""Envelope budgets (Release 2.1) and the monthly spending review.

Every expected number below is worked out by hand from the fixtures (today is 2026-09-26).
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models import AppSetting, Budget
from tests.conftest import add_budget, add_txn, make_account


def save(client, month, assigned=None, removed=None):
    body = {}
    if assigned is not None:
        body["assigned"] = assigned
    if removed is not None:
        body["removed"] = removed
    return client.put(f"/api/budgets/{month}", json=body)


def ok(response) -> dict:
    assert response.status_code == 200, response.text
    return response.json()


def get(client, month=None) -> dict:
    return ok(client.get("/api/budgets", params={"month": month} if month else None))


def lines(b) -> dict[str, tuple]:
    """category -> (carryover, assigned, spent, available)."""
    return {c["category"]: (c["carryover"], c["assigned"], c["spent"], c["available"]) for c in b["categories"]}


def rows(db, category) -> list[tuple]:
    db.expire_all()
    return [
        (b.month, b.limit_cents, b.removed)
        for b in db.scalars(select(Budget).where(Budget.category == category).order_by(Budget.month))
    ]


def restarts(db, category) -> list[str]:
    """Months where the category was re-added in the month it was removed."""
    db.expire_all()
    return list(db.scalars(
        select(Budget.month).where(Budget.category == category, Budget.restart.is_(True)).order_by(Budget.month)
    ))


def setting(db) -> object:
    db.expire_all()
    row = db.get(AppSetting, "budget_account_ids")
    return None if row is None else json.loads(row.value)


def included(b) -> list[tuple[str, bool]]:
    return [(a["name"], a["included"]) for a in b["budget_accounts"]]


@pytest.fixture
def spend(unlocked, db, fixed_today):
    """Accounts of every kind. Budget accounts by default: Checking (+5000) and Visa (-300 owed)."""
    chk = make_account(db, "Checking", "bank", 5000)
    card = make_account(db, "Visa", "credit", 300)
    hidden = make_account(db, "Old", "bank", 0, hidden=True)
    ira = make_account(db, "IRA", "retirement", 1000)
    hsa = make_account(db, "HSA", "hsa", 100)
    jar = make_account(db, "Cash jar", "other", 50)
    sep = "2026-09-"
    add_txn(db, chk.id, sep + "02", -100, "TJ #1", merchant="Trader Joes")
    add_txn(db, card.id, sep + "03", -50, "TJ #2", merchant="Trader Joes")
    add_txn(db, chk.id, sep + "04", 20, "TJ refund", merchant="Trader Joes")
    add_txn(db, chk.id, sep + "05", -40, "Metro", category="TRANSPORTATION")
    add_txn(db, hidden.id, sep + "05", -300, "Concert", category="ENTERTAINMENT")
    add_txn(db, card.id, sep + "06", -60, "Target", category="GENERAL_MERCHANDISE", pending=True)
    add_txn(db, ira.id, sep + "06", -999, "Not spending", category="FOOD_AND_DRINK")
    add_txn(db, chk.id, sep + "07", -70, "Venmo", is_transfer=True)
    add_txn(db, chk.id, sep + "01", 3000, "Payroll", category="INCOME")
    add_txn(db, chk.id, sep + "15", -10, "Payroll fix", category="INCOME")
    add_txn(db, chk.id, sep + "10", -500, "Mortgage", category="LOAN_PAYMENTS")
    add_txn(db, chk.id, sep + "11", -1000, "To savings", category="TRANSFER_OUT")
    add_txn(db, hsa.id, sep + "12", 25, "Pharmacy refund", category="MEDICAL")
    add_txn(db, jar.id, sep + "13", -15, "Arcade", category="ENTERTAINMENT")
    add_txn(db, chk.id, "2026-08-20", -200, "Aug food", merchant="Safeway")
    return SimpleNamespace(client=unlocked, db=db, chk=chk, card=card, hidden=hidden, ira=ira, hsa=hsa, jar=jar)


@pytest.fixture
def env(unlocked, db, fixed_today):
    """One checking account holding $850 after August's and September's spending.

    August: assigned Food 100 / Transportation 50; spent Food 60 (40 left) and
    Transportation 80 (overspent by 30). September: no rows yet; Food spent 10.
    """
    chk = make_account(db, "Checking", "bank", 850)
    add_budget(db, "2026-08", {"FOOD_AND_DRINK": 100, "TRANSPORTATION": 50})  # history: read-only via the API
    add_txn(db, chk.id, "2026-08-05", -60, "Groceries")
    add_txn(db, chk.id, "2026-08-06", -80, "Train pass", category="TRANSPORTATION")
    add_txn(db, chk.id, "2026-09-03", -10, "Coffee")
    return SimpleNamespace(client=unlocked, db=db, chk=chk)


# ------------------------------------------------------------------ the month view


def release36(b) -> dict:
    """Take out the Release 3.6 fields (additive); with the income switch off they add nothing."""
    extra = {k: b.pop(k) for k in (
        "month_money", "income", "savings_category", "setup_needed", "bills_outside", "ready_pending", "seed_category",
        "bills_available",
    )}
    # Phase 2: no recurring bills here, so no category has any.
    assert all(c.pop("bills") is None for c in b["categories"])
    # Release 3.8 (Goals, additive): no goals here.
    assert all(c.pop("goal") is None and c.pop("seeded") is False for c in b["categories"])
    assert all(c.pop("debt") is None for c in b["categories"])  # Release 3.10
    assert all(c.pop("custom") is False for c in b["categories"])  # Budget delete: built-ins only here
    assert all(c.pop("moved") == 0.0 for c in b["categories"])  # Release 3.17: no one-time moves here
    assert extra["bills_outside"] == [] and extra["bills_available"] is True
    assert extra["income"]["mode"] == "off" and extra["income"]["pending"] == 0.0
    assert extra["ready_pending"] == 0.0 and extra["seed_category"] is None
    assert extra["month_money"] == round(b["ready_to_assign"] + b["assigned"], 2)
    return extra


def test_empty_budget_month(unlocked, fixed_today):
    b = get(unlocked)
    addable = b.pop("addable")
    assert release36(b)["setup_needed"] is True
    assert b == {
        "month": "2026-09", "is_current": True, "is_future": False, "day_of_month": 26, "days_in_month": 30,
        "days_left": 4, "pace": 26 / 30, "ready_to_assign": 0.0, "cash_total": 0.0, "budget_accounts": [],
        "assigned": 0.0, "spent": 0.0, "available": 0.0, "categories": [], "unbudgeted": [], "fixed": [],
        "earliest_month": "2026-09", "latest_month": "2027-09",
        "groups": [], "needed_total": 0.0,  # Release 3
    }
    assert len(addable) == 13 and addable[0] == {"id": "FOOD_AND_DRINK", "name": "Food and drink", "hue": 25}


def test_envelope_month_math(spend):
    client = spend.client
    b = ok(save(client, "2026-09", {"FOOD_AND_DRINK": 200, "TRANSPORTATION": 30, "TRAVEL": 0}))
    addable = b.pop("addable")
    assert release36(b)["setup_needed"] is False
    # Budget-account spending only: Food 100 + 50 - 20 refund = 130 (not the IRA, hidden or HSA
    # accounts, nor the jar, which isn't included by default); Transportation 40.
    # Ready to assign = cash 5000 - 300 - available (70 - 10 + 0) = 4640.
    assert b == {
        "month": "2026-09", "is_current": True, "is_future": False, "day_of_month": 26, "days_in_month": 30,
        "days_left": 4, "pace": 26 / 30,
        "ready_to_assign": 4640.0, "cash_total": 4700.0,
        "budget_accounts": [
            {"id": spend.chk.id, "name": "Checking", "mask": None, "institution_name": None, "category": "bank",
             "balance": 5000.0, "included": True},
            {"id": spend.card.id, "name": "Visa", "mask": None, "institution_name": None, "category": "credit",
             "balance": -300.0, "included": True},
            {"id": spend.jar.id, "name": "Cash jar", "mask": None, "institution_name": None, "category": "other",
             "balance": 50.0, "included": False},
        ],
        "assigned": 230.0, "spent": 170.0, "available": 60.0,
        "categories": [
            {"category": "FOOD_AND_DRINK", "name": "Food and drink", "hue": 25,
             "carryover": 0.0, "assigned": 200.0, "spent": 130.0, "available": 70.0,
             "group_id": None, "target": None},
            {"category": "TRANSPORTATION", "name": "Transportation", "hue": 230,
             "carryover": 0.0, "assigned": 30.0, "spent": 40.0, "available": -10.0,
             "group_id": None, "target": None},
            {"category": "TRAVEL", "name": "Travel", "hue": 200,
             "carryover": 0.0, "assigned": 0.0, "spent": 0.0, "available": 0.0,
             "group_id": None, "target": None},
        ],
        "unbudgeted": [{"category": "GENERAL_MERCHANDISE", "name": "Shopping", "hue": 300, "spent": 60.0}],
        "fixed": [{"category": "LOAN_PAYMENTS", "name": "Loan payments", "hue": 55, "spent": 500.0}],
        "earliest_month": "2026-08",
        "latest_month": "2027-09",
        "groups": [], "needed_total": 0.0,  # Release 3
    }
    assert [a["id"] for a in addable] == [
        "GENERAL_MERCHANDISE", "ENTERTAINMENT", "RENT_AND_UTILITIES", "MEDICAL", "PERSONAL_CARE",
        "GENERAL_SERVICES", "HOME_IMPROVEMENT", "BANK_FEES", "GOVERNMENT_AND_NON_PROFIT", "OTHER",
    ]
    again = get(client, "2026-09")
    release36(again)
    assert again == {**b, "addable": addable}

    # A partial update only touches the listed categories.
    b = ok(save(client, "2026-09", {"TRANSPORTATION": 45}))
    assert lines(b)["FOOD_AND_DRINK"] == (0.0, 200.0, 130.0, 70.0)
    assert lines(b)["TRANSPORTATION"] == (0.0, 45.0, 40.0, 5.0)
    assert b["ready_to_assign"] == 4625.0  # 4700 - (70 + 5 + 0)

    # August has no members; its budget-account spending is all unbudgeted.
    aug = get(client, "2026-08")
    assert aug["categories"] == []  # August is the earliest month
    assert aug["unbudgeted"] == [{"category": "FOOD_AND_DRINK", "name": "Food and drink", "hue": 25, "spent": 200.0}]
    assert (aug["is_current"], aug["is_future"], aug["pace"], aug["day_of_month"], aug["days_left"]) == (
        False, False, 1.0, 31, 0)
    assert aug["ready_to_assign"] == 4625.0  # the same on every month's view


def test_hidden_and_recategorised_categories(spend):
    client = spend.client
    ok(save(client, "2026-09", {"TRAVEL": 10, "MEDICAL": 5}))
    client.patch("/api/categories/TRAVEL", json={"hidden": True})
    b = get(client)
    assert "TRAVEL" not in [c["id"] for c in b["addable"]]
    assert "TRAVEL" in lines(b)  # a hidden member stays in the budget
    # A category that's no longer spending-kind drops out of the budget (and can't be saved).
    client.patch("/api/categories/MEDICAL", json={"kind": "fixed"})
    b = get(client)
    assert list(lines(b)) == ["TRAVEL"] and b["ready_to_assign"] == 4690.0  # 4700 - 10
    assert save(client, "2026-09", {"MEDICAL": 1}).json()["detail"] == "Medical isn't a spending category."


# ------------------------------------------------------------------ carryover and ready to assign


def test_leftover_carries_and_overspending_lowers_ready_to_assign(env):
    aug = get(env.client, "2026-08")
    assert lines(aug) == {
        "FOOD_AND_DRINK": (0.0, 100.0, 60.0, 40.0),
        "TRANSPORTATION": (0.0, 50.0, 80.0, -30.0),
    }
    assert (aug["assigned"], aug["spent"], aug["available"]) == (150.0, 140.0, 10.0)

    sep = get(env.client, "2026-09")
    # Food's $40 leftover rolls in; Transportation's overspending doesn't.
    assert lines(sep) == {
        "FOOD_AND_DRINK": (40.0, 0.0, 10.0, 30.0),
        "TRANSPORTATION": (0.0, 0.0, 0.0, 0.0),
    }
    # 850 in the bank - 30 still in envelopes. Equivalently: 1000 before spending - 150 assigned
    # - the 30 overspent in August.
    assert sep["ready_to_assign"] == 820.0 and aug["ready_to_assign"] == 820.0
    # Release 3.17: no repeat start month here (tests pin it), so September starts at $0.
    assert "suggested" not in sep
    assert rows(env.db, "FOOD_AND_DRINK") == [("2026-08", 10000, False)]


def test_future_assignments_reduce_ready_to_assign(env):
    client = env.client
    assert ok(save(client, "2026-10", {"FOOD_AND_DRINK": 200}))["ready_to_assign"] == 620.0
    assert ok(save(client, "2026-11", {"TRANSPORTATION": 25}))["ready_to_assign"] == 595.0  # 850 - 30 - 225

    oct_ = get(client, "2026-10")
    assert (oct_["is_current"], oct_["is_future"], oct_["pace"], oct_["day_of_month"], oct_["days_left"],
            oct_["days_in_month"]) == (False, True, 0.0, 0, 31, 31)
    assert lines(oct_) == {"FOOD_AND_DRINK": (30.0, 200.0, 0.0, 230.0), "TRANSPORTATION": (0.0, 0.0, 0.0, 0.0)}
    assert get(client, "2026-09")["ready_to_assign"] == 595.0
    nov = get(client, "2026-11")
    assert lines(nov) == {"FOOD_AND_DRINK": (230.0, 0.0, 0.0, 230.0), "TRANSPORTATION": (0.0, 25.0, 0.0, 25.0)}

    # December has no rows (and no repeat start month here): viewing saves nothing.
    dec = get(client, "2026-12")
    assert lines(dec) == {"FOOD_AND_DRINK": (230.0, 0.0, 0.0, 230.0), "TRANSPORTATION": (25.0, 0.0, 0.0, 25.0)}
    assert get(client, "2026-12") == dec
    assert [r[0] for r in rows(env.db, "FOOD_AND_DRINK")] == ["2026-08", "2026-10"]

    r = save(client, "2026-12", {"FOOD_AND_DRINK": 595.01})
    assert r.status_code == 422
    assert r.json()["detail"] == "Only $595 is ready to assign. Lower another category first."
    b = ok(save(client, "2026-12", {"FOOD_AND_DRINK": 595}))
    assert b["ready_to_assign"] == 0.0

    # A transaction dated in a future month counts in that month.
    add_txn(env.db, env.chk.id, "2026-10-05", -12, "Preorder")
    oct_ = get(client, "2026-10")
    assert lines(oct_)["FOOD_AND_DRINK"] == (30.0, 200.0, 12.0, 218.0)
    assert oct_["ready_to_assign"] == 0.0  # only today's month's envelopes and future assignments count


# ------------------------------------------------------------------ removing and re-adding


def test_remove_category_returns_leftover_and_deletes_later_rows(env):
    client, db = env.client, env.db
    ok(save(client, "2026-10", {"FOOD_AND_DRINK": 200}))
    b = ok(save(client, "2026-09", removed=["FOOD_AND_DRINK"]))
    assert lines(b) == {"TRANSPORTATION": (0.0, 0.0, 0.0, 0.0)}
    assert b["unbudgeted"] == [{"category": "FOOD_AND_DRINK", "name": "Food and drink", "hue": 25, "spent": 10.0}]
    assert "FOOD_AND_DRINK" in [c["id"] for c in b["addable"]]
    # The $30 leftover and October's $200 are back: 820 + 30 + 200.
    assert b["ready_to_assign"] == 850.0
    assert rows(db, "FOOD_AND_DRINK") == [("2026-08", 10000, False), ("2026-09", 0, True)]
    assert "FOOD_AND_DRINK" in lines(get(client, "2026-08"))  # history is untouched
    assert "FOOD_AND_DRINK" not in lines(get(client, "2026-10"))

    # Re-adding starts fresh: no carryover from before the removal.
    b = ok(save(client, "2026-10", {"FOOD_AND_DRINK": 20}))
    assert lines(b)["FOOD_AND_DRINK"] == (0.0, 20.0, 0.0, 20.0)
    assert b["ready_to_assign"] == 830.0
    assert "FOOD_AND_DRINK" not in lines(get(client, "2026-09"))

    # Removing a category that isn't in the budget changes nothing.
    ok(save(client, "2026-09", removed=["TRAVEL"]))
    assert rows(db, "TRAVEL") == []

    # Removing in a future month deletes the rows after it too.
    ok(save(client, "2026-11", {"FOOD_AND_DRINK": 5}))
    ok(save(client, "2026-10", removed=["FOOD_AND_DRINK"]))
    assert rows(db, "FOOD_AND_DRINK") == [("2026-08", 10000, False), ("2026-09", 0, True), ("2026-10", 0, True)]
    assert get(client)["ready_to_assign"] == 850.0
    assert restarts(db, "FOOD_AND_DRINK") == []  # re-added a month later: no marker needed


def test_readding_in_the_month_it_was_removed_starts_fresh(env):
    """Removing releases Food's $30 (carryover 40 - spent 10); re-adding the same month must not
    bring the $40 carryover back, or the same dollars would be in Ready to Assign and the envelope."""
    client, db = env.client, env.db
    b = ok(save(client, "2026-09", removed=["FOOD_AND_DRINK"]))
    assert b["ready_to_assign"] == 850.0  # 820 + the $30 released
    b = ok(save(client, "2026-09", {"TRAVEL": 845}))
    assert b["ready_to_assign"] == 5.0  # 850 - 845

    # Re-add, covering this month's $10 of spending: carryover 0, Ready to Assign unchanged.
    # (With the old carryover it would be 5 - 40 = -35, and the save refused.)
    b = ok(save(client, "2026-09", {"FOOD_AND_DRINK": 10}))
    assert lines(b)["FOOD_AND_DRINK"] == (0.0, 10.0, 10.0, 0.0)
    assert b["ready_to_assign"] == 5.0  # 850 - (0 + 0 + 845)
    assert rows(db, "FOOD_AND_DRINK") == [("2026-08", 10000, False), ("2026-09", 1000, False)]
    assert restarts(db, "FOOD_AND_DRINK") == ["2026-09"]
    # The released leftover can't be moved out again either.
    r = save(client, "2026-09", {"FOOD_AND_DRINK": -1})
    assert (r.status_code, r.json()["detail"]) == (422, "You can move at most $0 out of Food and drink.")

    # The last $5 can go in; the restart marker survives later saves of the same month.
    b = ok(save(client, "2026-09", {"FOOD_AND_DRINK": 15}))
    assert lines(b)["FOOD_AND_DRINK"] == (0.0, 15.0, 10.0, 5.0)
    assert b["ready_to_assign"] == 0.0
    assert restarts(db, "FOOD_AND_DRINK") == ["2026-09"]
    r = save(client, "2026-09", {"FOOD_AND_DRINK": 15.01})
    assert (r.status_code, r.json()["detail"]) == (422, "Only $0 is ready to assign. Lower another category first.")
    # August is untouched, and October carries September's $5 on as usual.
    assert lines(get(client, "2026-08"))["FOOD_AND_DRINK"] == (0.0, 100.0, 60.0, 40.0)
    assert lines(get(client, "2026-10"))["FOOD_AND_DRINK"] == (5.0, 0.0, 0.0, 5.0)

    # Removing again and re-adding with nothing assigned still starts from zero.
    assert ok(save(client, "2026-09", removed=["FOOD_AND_DRINK"]))["ready_to_assign"] == 5.0  # 850 - 845
    b = ok(save(client, "2026-09", {"FOOD_AND_DRINK": 0}))
    assert lines(b)["FOOD_AND_DRINK"] == (0.0, 0.0, 10.0, -10.0)
    assert b["ready_to_assign"] == 15.0  # 850 - (-10 + 845): the overspending isn't taken out until next month


# ------------------------------------------------------------------ validation of amounts


def test_negative_assignment_moves_leftover_out(env):
    client = env.client
    b = ok(save(client, "2026-09", {"FOOD_AND_DRINK": -30}))
    assert lines(b)["FOOD_AND_DRINK"] == (40.0, -30.0, 10.0, 0.0)
    assert b["ready_to_assign"] == 850.0  # 820 + the 30 moved out

    for body, detail in [
        ({"FOOD_AND_DRINK": -40.01}, "You can move at most $40 out of Food and drink."),
        ({"TRANSPORTATION": -1}, "You can move at most $0 out of Transportation."),  # overspent: nothing carried
        ({"TRAVEL": -5}, "You can move at most $0 out of Travel."),  # new to the budget
    ]:
        r = save(client, "2026-09", body)
        assert (r.status_code, r.json()["detail"]) == (422, detail)
    assert lines(get(client))["FOOD_AND_DRINK"] == (40.0, -30.0, 10.0, 0.0)

    # All of the carryover can go, even though some was spent (the envelope is then overspent).
    b = ok(save(client, "2026-09", {"FOOD_AND_DRINK": -40}))
    assert lines(b)["FOOD_AND_DRINK"] == (40.0, -40.0, 10.0, -10.0)
    assert b["ready_to_assign"] == 860.0


def test_over_assigning_is_refused_but_reductions_are_allowed(env):
    client, db = env.client, env.db
    r = save(client, "2026-09", {"TRAVEL": 820.01})
    assert (r.status_code, r.json()["detail"]) == (
        422, "Only $820 is ready to assign. Lower another category first.")
    # Atomic: a save with one bad category writes nothing.
    r = save(client, "2026-09", {"FOOD_AND_DRINK": 5, "NOPE": 1})
    assert (r.status_code, r.json()["detail"]) == (422, "Unknown category 'NOPE'.")
    assert rows(db, "TRAVEL") == [] and rows(db, "FOOD_AND_DRINK") == [("2026-08", 10000, False)]

    assert ok(save(client, "2026-09", {"TRAVEL": 820}))["ready_to_assign"] == 0.0

    # The balance drops (e.g. a sync): 800 - (30 + 0 + 820) = -50.
    env.chk.current_balance_cents = 80000
    db.commit()
    assert get(client)["ready_to_assign"] == -50.0
    # A save that raises Ready to Assign is allowed while it's negative, even if it doesn't fix it.
    assert ok(save(client, "2026-09", {"TRAVEL": 800}))["ready_to_assign"] == -30.0
    r = save(client, "2026-09", {"FOOD_AND_DRINK": 1})
    assert (r.status_code, r.json()["detail"]) == (
        422, "Only $0 is ready to assign. Lower another category first.")
    # Leaving it unchanged is allowed too (moving money between envelopes).
    assert ok(save(client, "2026-09", {"TRAVEL": 799, "FOOD_AND_DRINK": 1}))["ready_to_assign"] == -30.0
    # A save that lowers the total more than it raises it is allowed.
    b = ok(save(client, "2026-09", {"TRAVEL": 700, "FOOD_AND_DRINK": 10}))
    assert lines(b)["FOOD_AND_DRINK"] == (40.0, 10.0, 10.0, 40.0)
    assert b["ready_to_assign"] == 60.0  # 800 - (40 + 0 + 700)
    # Cents are compared exactly.
    ok(save(client, "2026-09", {"TRAVEL": 700.3, "FOOD_AND_DRINK": 9.7}))
    assert ok(save(client, "2026-09", {"TRAVEL": 760.1, "FOOD_AND_DRINK": 9.9}))["ready_to_assign"] == 0.0


def test_same_total_save_that_drops_ready_to_assign_below_zero_is_refused(env):
    """The old rule only looked at the total assigned. Removing an overspent envelope takes its
    overspending out of Ready to Assign, so a save can keep the total and still over-assign."""
    client, db = env.client, env.db
    add_txn(db, env.chk.id, "2026-09-08", -50, "Taxi", category="TRANSPORTATION")
    env.chk.current_balance_cents = 80000  # the taxi left the bank
    db.commit()
    # Transportation: 0 + 20 - 50 = -30; Food 40 + 0 - 10 = 30. 800 - (30 - 30) = 800.
    assert ok(save(client, "2026-09", {"TRANSPORTATION": 20}))["ready_to_assign"] == 800.0
    assert ok(save(client, "2026-09", {"TRAVEL": 800}))["ready_to_assign"] == 0.0

    # Same total assigned (20 + 800 before, 0 + 820 after), but 800 - (30 + 820) = -50.
    r = save(client, "2026-09", {"TRAVEL": 820}, ["TRANSPORTATION"])
    assert (r.status_code, r.json()["detail"]) == (422, "Only $0 is ready to assign. Lower another category first.")
    # Removing it alone lowers Ready to Assign below zero too (800 - (30 + 800) = -30).
    r = save(client, "2026-09", removed=["TRANSPORTATION"])
    assert (r.status_code, r.json()["detail"]) == (422, "Only $0 is ready to assign. Lower another category first.")
    assert lines(get(client))["TRANSPORTATION"] == (0.0, 20.0, 50.0, -30.0)
    assert rows(db, "TRAVEL") == [("2026-09", 80000, False)]

    # Covering the overspending first (moving $30 from Travel) keeps it at 0; then removing
    # Transportation (available 0) leaves it unchanged, so it's allowed.
    assert ok(save(client, "2026-09", {"TRAVEL": 770, "TRANSPORTATION": 50}))["ready_to_assign"] == 0.0
    b = ok(save(client, "2026-09", removed=["TRANSPORTATION"]))
    assert "TRANSPORTATION" not in lines(b) and b["ready_to_assign"] == 0.0  # 800 - (30 + 770)


def test_raising_ready_to_assign_is_allowed_while_negative(env):
    client, db = env.client, env.db
    assert ok(save(client, "2026-09", {"TRAVEL": 820}))["ready_to_assign"] == 0.0
    env.chk.current_balance_cents = 70000  # a sync lowers the balance by $150
    db.commit()
    assert get(client)["ready_to_assign"] == -150.0  # 700 - (30 + 820)
    # Removing Food releases its $30: -120, still negative but higher, so allowed.
    assert ok(save(client, "2026-09", removed=["FOOD_AND_DRINK"]))["ready_to_assign"] == -120.0
    # Moving money out of a future month raises it too (the $50 goes back to Ready to Assign).
    add_budget(db, "2026-10", {"TRAVEL": 100})  # planned before the balance dropped
    assert get(client)["ready_to_assign"] == -220.0
    assert ok(save(client, "2026-10", {"TRAVEL": 50}))["ready_to_assign"] == -170.0
    # But a save that lowers it further is refused, with X = max(0, Ready to Assign).
    r = save(client, "2026-10", {"TRAVEL": 50.01})
    assert (r.status_code, r.json()["detail"]) == (422, "Only $0 is ready to assign. Lower another category first.")


def test_past_months_are_read_only(env):
    client, db = env.client, env.db
    before = get(client, "2026-08")
    for body in ({"assigned": {"FOOD_AND_DRINK": 120}}, {"removed": ["FOOD_AND_DRINK"]}, {}):
        r = client.put("/api/budgets/2026-08", json=body)
        assert (r.status_code, r.json()["detail"]) == (
            422, "Past months are read-only. Make changes in the current month."), body
    r = save(client, "2020-01", {"MEDICAL": 0})
    assert (r.status_code, r.json()["detail"]) == (422, "Past months are read-only. Make changes in the current month.")
    assert get(client, "2026-08") == before  # viewing still works; nothing changed
    assert rows(db, "FOOD_AND_DRINK") == [("2026-08", 10000, False)] and rows(db, "MEDICAL") == []
    # The current month and later are editable.
    ok(save(client, "2026-09", {"FOOD_AND_DRINK": 1}))
    ok(save(client, "2027-09", {"FOOD_AND_DRINK": 1}))


@pytest.mark.parametrize(
    ("month", "body", "detail"),
    [
        ("2026-09", {"assigned": {"NOPE": 5}}, "Unknown category 'NOPE'."),
        ("2026-09", {"assigned": {"INCOME": 5}}, "Income isn't a spending category."),
        ("2026-09", {"assigned": {"LOAN_PAYMENTS": 5}}, "Loan payments isn't a spending category."),
        ("2026-09", {"removed": ["TRANSFER_IN"]}, "Transfer in isn't a spending category."),
        ("2026-09", {"removed": ["NOPE"]}, "Unknown category 'NOPE'."),
        ("2026-09", {"assigned": {"FOOD_AND_DRINK": 5}, "removed": ["FOOD_AND_DRINK"]},
         "Food and drink can't be both assigned and removed."),
        ("2026-09", {"assigned": {"FOOD_AND_DRINK": 1e13}}, "Amounts must be within $1,000,000,000,000."),
        ("2027-10", {"assigned": {"FOOD_AND_DRINK": 0}}, "You can budget up to 2027-09, 12 months ahead."),
        ("2026-08", {"assigned": {"FOOD_AND_DRINK": 5}}, "Past months are read-only. Make changes in the current month."),
        ("1900-01", {"removed": ["FOOD_AND_DRINK"]}, "Past months are read-only. Make changes in the current month."),
        ("1899-12", {}, "month must be 1900-01 or later"),
        ("2026-13", {}, "month must be YYYY-MM"),
        ("2026-9", {}, "month must be YYYY-MM"),
        ("2026-09", {"pool": 100, "limits": {}}, None),  # the Release 2 shape is gone
        ("2026-09", {"assigned": {"FOOD_AND_DRINK": "lots"}}, None),
        ("2026-09", {"removed": "FOOD_AND_DRINK"}, None),
    ],
)
def test_budget_validation(spend, month, body, detail):
    r = spend.client.put(f"/api/budgets/{month}", json=body)
    assert r.status_code == 422, r.text
    assert isinstance(r.json()["detail"], str)
    if detail is not None:
        assert r.json()["detail"] == detail
    assert spend.db.scalars(select(Budget)).first() is None


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_amounts_must_be_finite(spend, value):
    r = spend.client.put("/api/budgets/2026-09", content=f'{{"assigned": {{"FOOD_AND_DRINK": {value}}}}}',
                         headers={"Content-Type": "application/json"})
    assert (r.status_code, r.json()["detail"]) == (422, "Amounts must be finite numbers.")


def test_empty_save_changes_nothing(env):
    before = get(env.client)
    assert ok(save(env.client, "2026-09")) == before
    assert ok(save(env.client, "2026-09", {}, [])) == before


# ------------------------------------------------------------------ budget accounts


def test_budget_accounts(spend):
    client = spend.client
    b = ok(save(client, "2026-09", {"FOOD_AND_DRINK": 100}))
    assert b["cash_total"] == 4700.0  # 5000 - 300 on the card
    assert lines(b)["FOOD_AND_DRINK"] == (0.0, 100.0, 130.0, -30.0)
    assert b["ready_to_assign"] == 4730.0

    assert setting(spend.db) is None  # the default: every bank and credit account
    b = ok(client.put("/api/budgets/accounts", json={"account_ids": [spend.jar.id, spend.chk.id, spend.chk.id]}))
    assert b["month"] == "2026-09"
    assert included(b) == [("Checking", True), ("Visa", False), ("Cash jar", True)]
    # Stored as exceptions to the default: the card left out, the "other" jar added.
    assert setting(spend.db) == {"exclude": [spend.card.id], "include": [spend.jar.id]}
    assert b["cash_total"] == 5050.0
    # Only budget-account spending counts: the card's $50 and $60 drop out, the jar's $15 comes in.
    assert lines(b)["FOOD_AND_DRINK"] == (0.0, 100.0, 80.0, 20.0)
    assert b["unbudgeted"] == [
        {"category": "TRANSPORTATION", "name": "Transportation", "hue": 230, "spent": 40.0},
        {"category": "ENTERTAINMENT", "name": "Entertainment", "hue": 340, "spent": 15.0},
    ]
    assert b["ready_to_assign"] == 5030.0

    for bad in ([spend.hidden.id], [spend.ira.id], [spend.hsa.id], [spend.chk.id, 999999]):
        r = client.put("/api/budgets/accounts", json={"account_ids": bad})
        assert r.status_code == 422, bad
        assert r.json()["detail"].endswith("can't fund the budget. Pick visible bank, credit card or other accounts.")
    assert client.put("/api/budgets/accounts", json={"account_ids": ["x"]}).status_code == 422
    assert client.put("/api/budgets/accounts", json={}).status_code == 422
    assert get(client)["cash_total"] == 5050.0  # unchanged by the rejected calls

    # A bank or card linked after the selection was saved joins automatically; a new "other"
    # account doesn't; the card that was explicitly left out stays out.
    new_bank = make_account(spend.db, "New bank", "bank", 10)
    new_card = make_account(spend.db, "New card", "credit", 4)
    make_account(spend.db, "Piggy bank", "other", 7)
    b = get(client)
    assert included(b) == [
        ("Checking", True), ("New bank", True), ("New card", True), ("Visa", False),
        ("Cash jar", True), ("Piggy bank", False)]
    assert b["cash_total"] == 5056.0  # 5000 + 10 - 4 + 50
    assert b["ready_to_assign"] == 5036.0  # 5056 - (100 - 80)

    # No budget accounts: Ready to Assign is minus what's assigned.
    b = ok(client.put("/api/budgets/accounts", json={"account_ids": []}))
    assert b["cash_total"] == 0.0 and not any(a["included"] for a in b["budget_accounts"])
    assert lines(b)["FOOD_AND_DRINK"] == (0.0, 100.0, 0.0, 100.0)
    assert b["unbudgeted"] == [] and b["ready_to_assign"] == -100.0
    assert setting(spend.db) == {
        "exclude": sorted([spend.chk.id, spend.card.id, new_bank.id, new_card.id]), "include": []}


def test_budget_accounts_legacy_list(spend):
    """Development builds stored a plain list: exactly those accounts are included."""
    db = spend.db
    db.add(AppSetting(key="budget_account_ids", value=json.dumps([spend.chk.id, spend.jar.id, 424242])))
    db.commit()
    make_account(db, "New bank", "bank", 10)  # a plain list has no exceptions: it stays out
    b = get(spend.client)
    assert included(b) == [("Checking", True), ("New bank", False), ("Visa", False), ("Cash jar", True)]
    assert b["cash_total"] == 5050.0

    # Deleting an account prunes the list and keeps its format.
    assert spend.client.delete(f"/api/accounts/{spend.jar.id}").status_code == 200
    assert setting(db) == [spend.chk.id]
    # The next save converts it to exceptions.
    b = ok(spend.client.put("/api/budgets/accounts", json={"account_ids": [spend.chk.id]}))
    new_bank = next(a for a in b["budget_accounts"] if a["name"] == "New bank")
    assert setting(db) == {"exclude": sorted([new_bank["id"], spend.card.id]), "include": []}
    assert included(b) == [("Checking", True), ("New bank", False), ("Visa", False)]


@pytest.mark.parametrize("value", ["not json", "42", '"text"', "null", '{"exclude": "x", "include": [true, 1.5]}'])
def test_budget_accounts_unreadable_setting(spend, value):
    """An unreadable setting falls back to the default; junk ids are ignored."""
    spend.db.add(AppSetting(key="budget_account_ids", value=value))
    spend.db.commit()
    assert included(get(spend.client)) == [("Checking", True), ("Visa", True), ("Cash jar", False)]


def test_hidden_budget_account_stops_counting(spend):
    client, db = spend.client, spend.db
    ok(client.put("/api/budgets/accounts", json={"account_ids": [spend.chk.id, spend.card.id]}))
    # A saved account that's later hidden stops counting.
    client.patch(f"/api/accounts/{spend.card.id}", json={"hidden": True})
    b = get(client)
    assert [a["name"] for a in b["budget_accounts"]] == ["Checking", "Cash jar"]
    assert b["cash_total"] == 5000.0


def test_hidden_account_keeps_its_choice(spend):
    """A choice saved for an account that's hidden during a later save survives un-hiding."""
    client = spend.client
    ok(client.put("/api/budgets/accounts", json={"account_ids": [spend.chk.id, spend.jar.id]}))  # Visa out
    client.patch(f"/api/accounts/{spend.card.id}", json={"hidden": True})
    client.patch(f"/api/accounts/{spend.jar.id}", json={"hidden": True})
    ok(client.put("/api/budgets/accounts", json={"account_ids": [spend.chk.id]}))
    client.patch(f"/api/accounts/{spend.card.id}", json={"hidden": False})
    client.patch(f"/api/accounts/{spend.jar.id}", json={"hidden": False})
    b = get(client)
    assert included(b) == [("Checking", True), ("Visa", False), ("Cash jar", True)]
    assert b["cash_total"] == 5050.0


# ------------------------------------------------------------------ month range


def test_month_range(env):
    client = env.client
    assert get(client, "2027-09")["latest_month"] == "2027-09"
    assert get(client, "2026-08")["earliest_month"] == "2026-08"
    for month, detail in [("2027-10", "month must be between 2026-08 and 2027-09"),
                          ("2026-07", "month must be between 2026-08 and 2027-09"),
                          ("0001-01", "month must be 1900-01 or later")]:
        r = client.get("/api/budgets", params={"month": month})
        assert (r.status_code, r.json()["detail"]) == (422, detail)
    assert client.get("/api/budgets", params={"month": "Sept"}).status_code == 422

    ok(save(client, "2027-09", {"TRAVEL": 1}))
    assert save(client, "2027-10", {"TRAVEL": 1}).status_code == 422
    # Saves before today's month are refused, even ones that would write nothing.
    assert save(client, "2001-01").status_code == 422
    # An older budgets row (e.g. from a Release 2 upgrade) extends the range back.
    add_budget(env.db, "2020-01", {"MEDICAL": 0})
    assert get(client, "2020-01")["earliest_month"] == "2020-01"


def test_bad_review_month(spend):
    assert spend.client.get("/api/spending/review", params={"month": "2026-00"}).status_code == 422


# ------------------------------------------------------------------ review


def test_spending_review(spend):
    client = spend.client
    add_budget(spend.db, "2026-06", {"TRAVEL": 100})
    ok(save(client, "2026-09", {"FOOD_AND_DRINK": 200, "TRANSPORTATION": 30}))
    r = client.get("/api/spending/review", params={"month": "2026-09"}).json()
    assert r == {
        "month": "2026-09",
        "income": 3000.0,
        "fixed": 500.0,
        "budgeted_spending": 170.0,  # Food 130 + Transportation 40 + Travel 0 (a member since June)
        "other_spending": 75.0,  # Shopping 60 + the jar's Entertainment 15 (all spending accounts)
        "left_over": 2255.0,
        "left_over_pct": 75.2,
        "compare": {"prev_month": "2026-08", "prev_spent": 200.0, "spent": 245.0, "delta_pct": 22.5},
        "biggest": {"category": "FOOD_AND_DRINK", "name": "Food and drink", "hue": 25, "spent": 130.0},
        "most_visited": {"merchant": "Trader Joes", "count": 3, "spent": 130.0},
        "history": [
            {"month": "2026-04", "spent": 0.0, "assigned": 0.0},
            {"month": "2026-05", "spent": 0.0, "assigned": 0.0},
            {"month": "2026-06", "spent": 0.0, "assigned": 100.0},
            {"month": "2026-07", "spent": 0.0, "assigned": 0.0},  # assignments are per month
            {"month": "2026-08", "spent": 200.0, "assigned": 0.0},
            {"month": "2026-09", "spent": 245.0, "assigned": 230.0},
        ],
    }
    assert client.get("/api/spending/review").json()["month"] == "2026-09"


def test_review_of_empty_month(spend):
    r = spend.client.get("/api/spending/review", params={"month": "2026-05"}).json()
    assert r["income"] == 0 and r["left_over"] == 0 and r["left_over_pct"] is None
    assert r["compare"] == {"prev_month": "2026-04", "prev_spent": 0.0, "spent": 0.0, "delta_pct": None}
    assert r["biggest"] is None and r["most_visited"] is None


def test_review_left_over_can_be_negative(unlocked, db, fixed_today):
    chk = make_account(db, "Checking", "bank", 0)
    add_txn(db, chk.id, "2026-09-01", 1000, "Pay", category="INCOME")
    add_txn(db, chk.id, "2026-09-02", -1500, "Rent", category="RENT_AND_UTILITIES")
    r = unlocked.get("/api/spending/review").json()
    assert r["left_over"] == -500.0 and r["left_over_pct"] == -50.0
    assert r["compare"]["delta_pct"] is None  # nothing spent last month


def test_deleted_account_id_is_not_reused_as_a_budget_account(spend):
    """SQLite may reuse the highest rowid; a stale id must not include a brand-new account."""
    client, db = spend.client, spend.db
    newest = make_account(db, "Newest cash", "other", 40)
    ok(client.put("/api/budgets/accounts", json={"account_ids": [spend.chk.id, newest.id]}))
    assert setting(db) == {"exclude": [spend.card.id], "include": [newest.id]}
    assert client.delete(f"/api/accounts/{newest.id}").status_code == 200
    db.expunge(newest)  # deleted by the API's session; SQLite may hand its id to the next account
    assert setting(db) == {"exclude": [spend.card.id], "include": []}  # pruned from include
    reused = make_account(db, "Brand new cash", "other", 999)
    b = get(client)
    assert ("Brand new cash", False) in included(b)
    assert b["cash_total"] == 5000.0  # Checking only: Visa is excluded, the "other" accounts aren't included
    assert reused.id  # whether or not the id was reused, it isn't a budget account


def test_deleted_excluded_account_id_is_pruned(spend):
    """A stale id in ``exclude`` would silently leave a brand-new bank out of the budget."""
    client, db = spend.client, spend.db
    newest = make_account(db, "Newest bank", "bank", 40)
    ok(client.put("/api/budgets/accounts", json={"account_ids": [spend.chk.id, spend.card.id]}))
    assert setting(db) == {"exclude": [newest.id], "include": []}
    assert client.delete(f"/api/accounts/{newest.id}").status_code == 200
    db.expunge(newest)
    assert setting(db) == {"exclude": [], "include": []}  # pruned from exclude
    reused = make_account(db, "Brand new bank", "bank", 999)
    b = get(client)
    assert ("Brand new bank", True) in included(b)
    assert b["cash_total"] == 5699.0  # 5000 - 300 + 999
    assert reused.id


# ------------------------------------------------------------------ review fixes (Release 2.1)


def test_huge_amounts_are_a_422_not_a_crash(spend):
    # Past Decimal's 28-digit precision: must be the plain range error, not a 500.
    for value in (1e27, -1e27, 1e300):
        r = save(spend.client, "2026-09", {"FOOD_AND_DRINK": value})
        assert (r.status_code, r.json()["detail"]) == (422, "Amounts must be within $1,000,000,000,000."), value


def test_lowering_a_month_cant_undo_a_later_move_out(env):
    """September's $80 leftover is moved out in October; September can't then give it back too."""
    client = env.client
    # September: 40 carried + 50 - 10 spent = 80, all moved out in October.
    assert ok(save(client, "2026-09", {"FOOD_AND_DRINK": 50}))["ready_to_assign"] == 770.0  # 850 - 80
    assert ok(save(client, "2026-10", {"FOOD_AND_DRINK": -80}))["ready_to_assign"] == 850.0  # 850 - 80 + 80
    r = save(client, "2026-09", {"FOOD_AND_DRINK": 20})
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == (
        "You moved $80 out of Food and drink in October 2026, so September 2026 can't leave less "
        "than that behind. Change October 2026 first."
    )
    assert get(client)["ready_to_assign"] == 850.0
    # Raising September is fine, and so is lowering it back down to what October needs.
    assert ok(save(client, "2026-09", {"FOOD_AND_DRINK": 60}))["ready_to_assign"] == 840.0  # 850 - 90 + 80
    assert ok(save(client, "2026-09", {"FOOD_AND_DRINK": 50}))["ready_to_assign"] == 850.0
    # Once October moves less out, September can go lower.
    assert ok(save(client, "2026-10", {"FOOD_AND_DRINK": -30}))["ready_to_assign"] == 800.0  # 850 - 80 + 30
    assert ok(save(client, "2026-09", {"FOOD_AND_DRINK": 0}))["ready_to_assign"] == 850.0  # 850 - 30 + 30
    assert lines(get(client, "2026-10"))["FOOD_AND_DRINK"] == (30.0, -30.0, 0.0, 0.0)


def test_lowering_a_past_month_checks_future_move_outs_too(env):
    client = env.client
    ok(save(client, "2026-10", {"TRAVEL": 50}))
    ok(save(client, "2026-11", {"TRAVEL": -50}))
    r = save(client, "2026-10", {"TRAVEL": 20})
    assert r.status_code == 422 and "November 2026" in r.json()["detail"]


def test_adding_a_category_covers_its_spending_without_new_money(env):
    """The page's draft relies on this: this month's spending already left the bank balance,
    so assigning up to it when adding the category leaves Ready to Assign unchanged."""
    client, db = env.client, env.db
    add_txn(db, env.chk.id, "2026-09-04", -60, "Target", category="GENERAL_MERCHANDISE")
    env.chk.current_balance_cents = 79000
    db.commit()
    assert ok(save(client, "2026-09", {"TRAVEL": 760}))["ready_to_assign"] == 0.0
    r = save(client, "2026-09", {"GENERAL_MERCHANDISE": 60.01})
    assert (r.status_code, r.json()["detail"]) == (422, "Only $0 is ready to assign. Lower another category first.")
    b = ok(save(client, "2026-09", {"GENERAL_MERCHANDISE": 60}))
    assert lines(b)["GENERAL_MERCHANDISE"] == (0.0, 60.0, 60.0, 0.0) and b["ready_to_assign"] == 0.0

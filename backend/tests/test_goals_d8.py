"""Goals (D8, Release 3.8): goals that live in the Budget (SPEC "Goals (D8) and Investments (D9)")."""
from __future__ import annotations

import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.models import AlertEvent, AppSetting, Budget, CategoryGroup, Goal, TxnCategory
from app.services import categories as cat_service
from app.services import spending
from app.services.alerts import evaluate
from tests.conftest import BASE_URL, CSRF, add_budget, add_txn, make_account
from tests.test_r36_calendar import make_item

T = "2026-09"
D = dt.date.fromisoformat
NOT_ENOUGH_TAIL = "isn't planned yet this month. Use a smaller amount, or take money from another category on the Budget page."


def ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json() if response.content else None


def err(response, status=422) -> dict:
    assert response.status_code == status, response.text
    return response.json()


def state(client) -> dict:
    return ok(client.get("/api/goals"))


def goal_in(st: dict, goal_id: int) -> dict:
    return next(g for g in st["goals"] if g["id"] == goal_id)


def budget(client, month=None) -> dict:
    return ok(client.get("/api/budgets", params={"month": month} if month else None))


def line(bm: dict, category: str) -> dict | None:
    return next((c for c in bm["categories"] if c["category"] == category), None)


def save_goal(client, name="Trip", target=600, due="2026-12", monthly=90, **kw) -> dict:
    body = {"kind": "save", "name": name, "target": target, "due_month": due, "monthly": monthly, **kw}
    return ok(client.post("/api/goals", json=body), 201)


def cat_row(db, category: str) -> TxnCategory:
    db.expire_all()
    return db.get(TxnCategory, category)


@pytest.fixture
def base(unlocked, db, fixed_today):
    """Checking 4,000 + Savings 1,000 (both fund the Budget); September plans Food 500, so
    4,500 isn't planned yet. Today is 2026-09-26."""
    chk = make_account(db, "Checking", "bank", 4000)
    sav = make_account(db, "Savings", "bank", 1000, mask="2210")
    add_budget(db, T, {"FOOD_AND_DRINK": 500})
    return unlocked, db, chk, sav


# ------------------------------------------------------------------ GET / setup first


def test_goals_need_the_budget_set_up_first(unlocked, db, fixed_today):
    make_account(db, "Checking", "bank", 1000)
    st = state(unlocked)
    assert st["budget_ready"] is False and st["goals"] == []
    r = err(unlocked.post("/api/goals", json={
        "kind": "save", "name": "Trip", "target": 100, "due_month": "2026-12", "monthly": 10,
    }), 409)
    assert r["detail"] == "Set up your Budget first."
    assert db.scalar(select(Goal.id)) is None


def test_empty_state(base):
    client, *_ = base
    assert state(client) == {
        "month": T, "month_money": 5000.0, "not_planned": 4500.0, "monthly_total": 0.0, "budget_ready": True,
        "emergency_suggestion": None, "emergency_basis": None, "emergency_monthly": None,
        "emergency_available": None, "goals": [],
    }


def test_old_suggestions_route_is_gone(base):
    client, *_ = base
    assert client.get("/api/goals/suggestions").status_code != 200


# ------------------------------------------------------------------ create


def test_create_save_goal_makes_a_budget_category(base):
    client, db, _chk, sav = base
    st = save_goal(client, account_id=sav.id)
    goal = goal_in(st, st["goal_id"])
    c = goal["category_id"]
    assert goal == {
        "id": st["goal_id"], "kind": "save", "name": "Trip", "category_id": c, "in_budget": True,
        "target": 600.0, "due_month": "2026-12", "monthly": 90.0, "planned_this_month": 90.0,
        "lowest_plan": 0.0, "saved": 0.0, "account_id": sav.id, "account_label": "Savings ··2210", "status": "behind",
        "months_to_save": 4,
        "projected": 360.0, "projected_month": "2027-03", "short_by": 240.0, "needed_monthly": 150.0,
    }
    assert (st["not_planned"], st["monthly_total"], st["month_money"]) == (4410.0, 90.0, 5000.0)
    row = cat_row(db, c)
    assert (row.name, row.kind, row.custom, row.hidden) == ("Trip", "spending", True, False)
    assert db.get(CategoryGroup, row.group_id).name == "Other and saving"
    bm = budget(client)
    assert line(bm, c)["assigned"] == 90.0
    assert line(bm, c)["goal"] == {
        "id": goal["id"], "kind": "save", "saved": 0.0, "target": 600.0, "reached": False, "plan": 90.0,
    }
    assert line(bm, c)["seeded"] is False
    assert line(bm, "FOOD_AND_DRINK")["goal"] is None
    links = json.loads(db.get(AppSetting, "goal_links").value)
    assert links == {str(goal["id"]): {"category": c, "seed_month": None}}


def test_goal_joins_an_existing_other_and_saving_group(base):
    client, db, *_ = base
    group = CategoryGroup(name="OTHER AND SAVING", position=3)
    db.add(group)
    db.commit()
    st = save_goal(client)
    assert cat_row(db, goal_in(st, st["goal_id"])["category_id"]).group_id == group.id


def test_on_track_and_needed_rounding(base):
    client, *_ = base
    st = save_goal(client, name="Gift", target=620, due="2026-12", monthly=155)
    g = goal_in(st, st["goal_id"])
    assert (g["status"], g["projected"], g["short_by"], g["needed_monthly"]) == ("on_track", 620.0, None, 155.0)
    st = save_goal(client, name="Bike", target=601, due="2026-12", monthly=0)
    g = goal_in(st, st["goal_id"])
    # no plan: never "behind"; needed = ceil(601 / 4) = 150.25 -> $155
    assert (g["status"], g["projected_month"], g["needed_monthly"], g["short_by"]) == ("no_plan", None, 155.0, None)


def test_already_saved_seeds_last_month(base):
    client, db, *_ = base
    st = save_goal(client, name="Car", target=1000, due="2027-06", monthly=100, already_saved=250)
    g = goal_in(st, st["goal_id"])
    assert (g["saved"], g["planned_this_month"], st["not_planned"]) == (250.0, 100.0, 4150.0)
    db.expire_all()
    seed = db.scalar(select(Budget).where(Budget.month == "2026-08", Budget.category == g["category_id"]))
    assert seed.limit_cents == 25000 and not seed.removed
    august = line(budget(client, "2026-08"), g["category_id"])
    assert (august["assigned"], august["seeded"]) == (250.0, True)
    assert line(budget(client), g["category_id"])["seeded"] is False
    links = json.loads(db.get(AppSetting, "goal_links").value)
    assert links[str(g["id"])]["seed_month"] == "2026-08"


def test_already_saved_takes_from_the_emergency_fund_when_short(unlocked, db, fixed_today):
    make_account(db, "Checking", "bank", 1000)
    add_budget(db, T, {"FOOD_AND_DRINK": 500})  # 500 not planned
    st = ok(unlocked.post("/api/goals", json={
        "kind": "emergency", "name": "Emergency fund", "target": 3000, "monthly": 300,
    }), 201)
    fund = goal_in(st, st["goal_id"])
    assert st["not_planned"] == 200.0
    st = save_goal(unlocked, target=1000, due="2027-06", monthly=100, already_saved=250)
    trip, fund = goal_in(st, st["goal_id"]), goal_in(st, fund["id"])
    # the plan (100) first, 100 of Already saved from Not planned yet, 150 from the fund's plan
    assert (trip["saved"], trip["planned_this_month"]) == (250.0, 100.0)
    assert (fund["planned_this_month"], fund["saved"], st["not_planned"]) == (150.0, 0.0, 0.0)

    before = db.scalar(select(TxnCategory.id).order_by(TxnCategory.id.desc()))
    r = err(unlocked.post("/api/goals", json={
        "kind": "save", "name": "Bike", "target": 900, "due_month": "2027-06", "monthly": 0, "already_saved": 200,
    }))
    assert r == {
        "detail": "Only $0 isn't planned yet this month, and Emergency fund has $150. Use a smaller amount for Already saved.",
        "code": "not_enough_unplanned", "not_planned": 0.0, "emergency_available": 150.0,
    }
    db.expire_all()
    assert db.scalar(select(TxnCategory.id).order_by(TxnCategory.id.desc())) == before  # nothing written
    assert len(state(unlocked)["goals"]) == 2


def test_emergency_goal_cannot_take_from_itself(unlocked, db, fixed_today):
    make_account(db, "Checking", "bank", 600)
    add_budget(db, T, {"FOOD_AND_DRINK": 500})
    r = err(unlocked.post("/api/goals", json={
        "kind": "emergency", "name": "Rainy day", "target": 3000, "monthly": 0, "already_saved": 101,
    }))
    assert r["code"] == "not_enough_unplanned" and r["not_planned"] == 100.0 and r["emergency_available"] == 0.0


def test_over_assign_refusal(base):
    client, db, *_ = base
    r = err(client.post("/api/goals", json={
        "kind": "save", "name": "House", "target": 100000, "due_month": "2028-09", "monthly": 5000,
    }))
    assert r == {"detail": f"Only $4,500 {NOT_ENOUGH_TAIL}", "code": "not_enough_unplanned", "not_planned": 4500.0}
    assert state(client)["goals"] == [] and cat_service.find_by_name(db, "House") is None
    st = save_goal(client, name="House", target=100000, due="2028-09", monthly=100)
    r = err(client.patch(f"/api/goals/{st['goal_id']}", json={"monthly": 5000}))
    assert r == {"detail": f"Only $4,400 {NOT_ENOUGH_TAIL}", "code": "not_enough_unplanned", "not_planned": 4400.0}
    g = goal_in(state(client), st["goal_id"])
    assert (g["monthly"], g["planned_this_month"]) == (100.0, 100.0)
    # exactly what's left fits
    g = goal_in(ok(client.patch(f"/api/goals/{st['goal_id']}", json={"monthly": 4500})), st["goal_id"])
    assert g["planned_this_month"] == 4500.0 and state(client)["not_planned"] == 0.0


# ------------------------------------------------------------------ emergency fund


def _savings_category(db, name="Emergency savings") -> str:
    category = cat_service.create_custom(db, name, "spending")
    spending.put_setting(db, spending.BUDGET_SAVINGS_SETTING, category.id)
    db.commit()
    return category.id


def test_emergency_goal_takes_over_emergency_savings(base):
    client, db, *_ = base
    sc = _savings_category(db)
    add_budget(db, "2026-08", {sc: 400})
    add_budget(db, T, {sc: 0})
    st = state(client)
    assert (st["emergency_available"], st["not_planned"]) == (400.0, 4100.0)
    assert budget(client)["savings_category"] == sc
    st = ok(client.post("/api/goals", json={
        "kind": "emergency", "name": "Rainy day", "target": 2000, "monthly": 100, "already_saved": 50,
    }), 201)
    g = goal_in(st, st["goal_id"])
    assert (g["category_id"], g["saved"], g["planned_this_month"], g["status"]) == (sc, 450.0, 100.0, "on_track")
    assert (g["due_month"], g["months_to_save"], g["projected"], g["projected_month"]) == (None, None, 2000.0, "2027-12")
    assert (g["short_by"], g["needed_monthly"]) == (None, None)
    assert (st["not_planned"], st["emergency_available"], st["emergency_suggestion"]) == (3950.0, None, None)
    assert cat_row(db, sc).name == "Rainy day"
    db.expire_all()
    assert db.scalar(select(Budget.limit_cents).where(Budget.month == "2026-08", Budget.category == sc)) == 45000
    assert budget(client)["savings_category"] == sc
    r = err(client.post("/api/goals", json={"kind": "emergency", "name": "Another", "target": 10, "monthly": 0}), 409)
    assert r["detail"] == "You already have an emergency fund."


def test_new_emergency_goal_becomes_the_savings_category(base):
    client, db, *_ = base
    st = ok(client.post("/api/goals", json={"kind": "emergency", "name": "Emergency fund", "target": 6000, "monthly": 0}), 201)
    g = goal_in(st, st["goal_id"])
    assert g["status"] == "no_plan" and g["projected"] == 0.0 and g["projected_month"] is None
    assert spending.get_setting(db, spending.BUDGET_SAVINGS_SETTING) == g["category_id"]
    assert budget(client)["savings_category"] == g["category_id"]


def test_emergency_suggestion_from_bills(base):
    client, db, chk, _ = base
    make_item(db, "Rent", -1234, "monthly", "2026-09-28", account=chk, category="RENT_AND_UTILITIES")
    st = state(client)
    assert (st["emergency_basis"], st["emergency_monthly"], st["emergency_suggestion"]) == ("bills", 1234.0, 3800.0)


def test_emergency_suggestion_from_spending_without_bills(base):
    client, db, chk, _ = base
    for month in ("2026-06", "2026-07", "2026-08"):
        add_txn(db, chk.id, f"{month}-10", -1000, "Groceries")
    st = state(client)
    assert (st["emergency_basis"], st["emergency_monthly"], st["emergency_suggestion"]) == ("spending", 1000.0, 3000.0)


# ------------------------------------------------------------------ Budget: savings category, new months


def test_savings_category_resolution(base):
    client, db, *_ = base
    assert budget(client)["savings_category"] is None
    done = save_goal(client, name="Done already", target=100, due="2026-12", monthly=50, already_saved=100)
    first = goal_in(done, done["goal_id"])
    assert first["status"] == "reached" and first["planned_this_month"] == 0.0
    assert budget(client)["savings_category"] is None  # a reached goal drops out
    st = save_goal(client, name="Trip")
    trip = goal_in(st, st["goal_id"])["category_id"]
    save_goal(client, name="Later")
    assert budget(client)["savings_category"] == trip  # the first unfinished goal
    sc = _savings_category(db)
    add_budget(db, T, {sc: 0})
    assert budget(client)["savings_category"] == sc  # the Budget's own savings category next
    st = ok(client.post("/api/goals", json={"kind": "emergency", "name": "Fund", "target": 10, "monthly": 1}), 201)
    assert budget(client)["savings_category"] == goal_in(st, st["goal_id"])["category_id"] == sc


def test_new_month_rules(base, fixed_today):
    client, db, *_ = base
    gift = save_goal(client, name="Gift", target=90, due="2026-12", monthly=90)
    trip = save_goal(client, name="Trip", target=600, due="2026-12", monthly=90)
    gift_id, trip_id = gift["goal_id"], trip["goal_id"]
    gift_c = goal_in(trip, gift_id)["category_id"]
    trip_c = goal_in(trip, trip_id)["category_id"]
    assert budget(client)["savings_category"] == gift_c
    # Release 3.17: plans repeat from September on; a goal's month plans the goal's rule.
    spending.put_setting(db, spending.BUDGET_PLANS_REPEAT_SETTING, "2026-09")
    db.commit()
    fixed_today.set(dt.date(2026, 10, 5))
    bm = budget(client, "2026-10")
    assert line(bm, trip_c)["assigned"] == 90.0  # min(90, 600 - 90)
    assert line(bm, gift_c)["assigned"] == 0.0  # reached: nothing more to set aside
    assert line(bm, "FOOD_AND_DRINK")["assigned"] == 500.0
    assert line(bm, gift_c)["goal"] == {
        "id": gift_id, "kind": "save", "saved": 90.0, "target": 90.0, "reached": True, "plan": 0.0,
    }
    assert line(bm, trip_c)["goal"]["plan"] == 90.0  # what Quick fill gives a goal category
    assert bm["savings_category"] == trip_c  # the reached goal dropped out
    st = state(client)
    g, t = goal_in(st, gift_id), goal_in(st, trip_id)
    assert (g["status"], g["saved"], g["planned_this_month"], g["in_budget"]) == ("reached", 90.0, 0.0, True)
    assert (t["saved"], t["planned_this_month"]) == (90.0, 90.0)


# ------------------------------------------------------------------ edit


def test_patch_edits(base):
    client, db, chk, sav = base
    card = make_account(db, "Visa", "credit", 10)
    st = save_goal(client)
    gid = st["goal_id"]
    c = goal_in(st, gid)["category_id"]
    g = goal_in(ok(client.patch(f"/api/goals/{gid}", json={"name": "  Big   trip "})), gid)
    assert g["name"] == "Big trip" and cat_row(db, c).name == "Big trip"
    r = err(client.patch(f"/api/goals/{gid}", json={"name": "food and drink"}), 409)
    assert "already exists" in r["detail"]
    # a target change rewrites this month's plan too
    g = goal_in(ok(client.patch(f"/api/goals/{gid}", json={"target": 50})), gid)
    assert (g["target"], g["planned_this_month"], g["status"]) == (50.0, 50.0, "on_track")
    g = goal_in(ok(client.patch(f"/api/goals/{gid}", json={"target": 900, "due_month": "2027-01"})), gid)
    assert (g["due_month"], g["months_to_save"], g["planned_this_month"]) == ("2027-01", 5, 90.0)
    g = goal_in(ok(client.patch(f"/api/goals/{gid}", json={"due_month": "2027-01", "name": "Trip"})), gid)
    assert g["due_month"] == "2027-01"
    for due in ("2026-09", "2028-10"):
        assert err(client.patch(f"/api/goals/{gid}", json={"due_month": due}))["detail"] == "Pick a month from 2026-10 to 2028-09."
    assert err(client.patch(f"/api/goals/{gid}", json={"due_month": None}))["detail"] == "Pick the month you need the money by."
    g = goal_in(ok(client.patch(f"/api/goals/{gid}", json={"account_id": sav.id})), gid)
    assert g["account_label"] == "Savings ··2210"
    g = goal_in(ok(client.patch(f"/api/goals/{gid}", json={"account_id": chk.id})), gid)
    assert g["account_label"] == "Checking"
    g = goal_in(ok(client.patch(f"/api/goals/{gid}", json={"account_id": None})), gid)
    assert (g["account_id"], g["account_label"]) == (None, None)
    assert err(client.patch(f"/api/goals/{gid}", json={"account_id": card.id}))["detail"] == "unknown account"
    assert err(client.patch(f"/api/goals/{gid}", json={"account_id": 99999}))["detail"] == "unknown account"
    assert err(client.patch(f"/api/goals/{gid}", json={"monthly": None}))["detail"] == "monthly must not be null"
    assert err(client.patch(f"/api/goals/{gid}", json={"kind": "emergency"}))
    assert err(client.patch(f"/api/goals/{gid}", json={"already_saved": 5}))["detail"] == (
        "Already saved can't change once the goal is in your Budget."
    )
    assert client.patch("/api/goals/99999", json={"monthly": 1}).status_code == 404
    fund = ok(client.post("/api/goals", json={"kind": "emergency", "name": "Fund", "target": 10, "monthly": 1}), 201)
    assert err(client.patch(f"/api/goals/{fund['goal_id']}", json={"due_month": "2027-01"}))["detail"] == (
        "An emergency fund has no date."
    )
    ok(client.patch(f"/api/goals/{fund['goal_id']}", json={"due_month": None}))


def test_patch_readds_a_goal_removed_on_the_budget_page(base):
    client, *_ = base
    st = save_goal(client)
    gid = st["goal_id"]
    c = goal_in(st, gid)["category_id"]
    ok(client.put(f"/api/budgets/{T}", json={"removed": [c]}))
    g = goal_in(state(client), gid)
    assert g["in_budget"] is False and g["category_id"] == c
    g = goal_in(ok(client.patch(f"/api/goals/{gid}", json={"name": "Trip"})), gid)
    assert g["in_budget"] is True and g["planned_this_month"] == 90.0


# ------------------------------------------------------------------ old goals


def test_old_goals(base):
    client, db, _chk, sav = base
    old = Goal(name="Old trip", kind="save", account_id=sav.id, target_cents=50000, monthly_cents=2500,
               current_cents=12345, target_date=dt.date(2027, 3, 15), hue=10)
    debt = Goal(name="Card", kind="debt", target_cents=0, start_cents=90000, monthly_cents=5000, hue=20)
    db.add_all([old, debt])
    db.commit()
    st = state(client)
    assert [g["id"] for g in st["goals"]] == [old.id]  # debt goals leave the Goals page
    g = st["goals"][0]
    assert (g["in_budget"], g["category_id"], g["saved"], g["planned_this_month"]) == (False, None, 0.0, 0.0)
    assert (g["due_month"], g["months_to_save"], g["status"], g["account_label"]) == ("2027-03", 7, "behind", "Savings ··2210")
    # Saving it once in Edit links it (with Already saved allowed this one time).
    g = goal_in(ok(client.patch(f"/api/goals/{old.id}", json={"already_saved": 100})), old.id)
    assert (g["in_budget"], g["saved"], g["planned_this_month"]) == (True, 100.0, 25.0)
    assert cat_row(db, g["category_id"]).name == "Old trip"
    assert err(client.patch(f"/api/goals/{old.id}", json={"already_saved": 5}))
    # debt goals: not found here, and untouched
    assert client.patch(f"/api/goals/{debt.id}", json={"monthly": 1}).status_code == 404
    assert client.delete(f"/api/goals/{debt.id}").status_code == 404
    db.expire_all()
    row = db.get(Goal, debt.id)
    assert (row.kind, row.start_cents, row.monthly_cents) == ("debt", 90000, 5000)


def test_links_to_a_missing_category_are_dropped(base):
    client, db, *_ = base
    st = save_goal(client)
    gid = st["goal_id"]
    c = goal_in(st, gid)["category_id"]
    # Settings refuses to delete a goal's category (see test_settings_cannot_delete_or_hide...);
    # a link to a category that is gone anyway (older data) is dropped.
    assert client.delete(f"/api/categories/{c}").status_code == 409
    db.execute(delete(Budget).where(Budget.category == c))
    db.delete(db.get(TxnCategory, c))
    db.commit()
    g = goal_in(state(client), gid)
    assert (g["in_budget"], g["category_id"]) == (False, None)
    g = goal_in(ok(client.patch(f"/api/goals/{gid}", json={"monthly": 90})), gid)
    assert g["in_budget"] is True and g["category_id"] != c
    links = json.loads(db.get(AppSetting, "goal_links").value)
    assert list(links) == [str(gid)]


# ------------------------------------------------------------------ delete / "I spent it" / restore


def test_delete_and_restore_round_trip(base):
    client, db, *_ = base
    st = save_goal(client, already_saved=200)
    gid = st["goal_id"]
    c = goal_in(st, gid)["category_id"]
    assert st["not_planned"] == 4210.0
    out = ok(client.delete(f"/api/goals/{gid}", params={"reason": "spent"}))
    assert out["undo"] == {
        "goal": {"kind": "save", "name": "Trip", "target": 600.0, "due_month": "2026-12", "monthly": 90.0,
                 "account_id": None},
        "category_id": c, "month": T, "assigned": 90.0, "released": 290.0, "seed_month": "2026-08",
        "removed": True, "token": out["undo"]["token"],
    }
    assert out["kept_in"] is None  # its own category left the Budget
    assert out["state"]["goals"] == [] and out["state"]["not_planned"] == 4500.0
    assert line(budget(client), c) is None
    assert cat_row(db, c).hidden is True  # nothing uses it
    st = ok(client.post("/api/goals/restore", json=out["undo"]), 201)
    g = goal_in(st, st["goal_id"])
    assert (g["category_id"], g["saved"], g["planned_this_month"], g["in_budget"]) == (c, 200.0, 90.0, True)
    assert st["not_planned"] == 4210.0 and cat_row(db, c).hidden is False
    assert line(budget(client, "2026-08"), c)["seeded"] is True


def test_restore_refusals(base, fixed_today):
    client, db, *_ = base
    st = save_goal(client)
    out = ok(client.delete(f"/api/goals/{st['goal_id']}"))
    fixed_today.set(dt.date(2026, 10, 1))
    r = err(client.post("/api/goals/restore", json=out["undo"]), 409)
    assert r["detail"] == "This can't be undone any more: a new month has started."
    fixed_today.set(dt.date(2026, 9, 26))
    assert client.delete(f"/api/categories/{out['undo']['category_id']}").status_code == 204
    r = err(client.post("/api/goals/restore", json=out["undo"]), 409)
    assert r["detail"] == "This can't be undone any more: its Budget category is gone."
    builtin = {**out["undo"], "category_id": "FOOD_AND_DRINK"}
    assert err(client.post("/api/goals/restore", json=builtin), 409)
    bad = {**out["undo"], "surprise": 1}
    assert client.post("/api/goals/restore", json=bad).status_code == 422
    assert state(client)["goals"] == []


def test_delete_keeps_a_category_with_transactions_visible(base):
    client, db, chk, _ = base
    st = save_goal(client)
    c = goal_in(st, st["goal_id"])["category_id"]
    add_txn(db, chk.id, "2026-09-20", -10, "Tickets", category=c)
    ok(client.delete(f"/api/goals/{st['goal_id']}"))
    assert cat_row(db, c).hidden is False


def test_same_name_after_delete_reuses_the_hidden_category(base, fixed_today):
    client, db, *_ = base
    st = save_goal(client, already_saved=100)
    c = goal_in(st, st["goal_id"])["category_id"]
    ok(client.delete(f"/api/goals/{st['goal_id']}"))
    st = save_goal(client)
    g = goal_in(st, st["goal_id"])
    # its old money was released when it was removed: nothing comes back
    assert (g["category_id"], g["saved"], g["planned_this_month"]) == (c, 0.0, 90.0)
    assert st["not_planned"] == 4410.0 and cat_row(db, c).hidden is False
    # Deleted again, then added with Already saved in the same month: the name is refused
    # (its envelope restarts this month, so the money couldn't reach it).
    ok(client.delete(f"/api/goals/{st['goal_id']}"))
    r = err(client.post("/api/goals", json={**GOOD, "already_saved": 50}), 409)
    assert r["detail"] == "A goal named “Trip” was deleted this month. Pick another name, or add it without Already saved."
    fixed_today.set(dt.date(2026, 10, 3))  # next month it is reused, and the money reaches it
    st = save_goal(client, due="2027-01", already_saved=50)
    g = goal_in(st, st["goal_id"])
    assert (g["category_id"], g["saved"]) == (c, 50.0)


def test_delete_and_restore_an_emergency_fund(base):
    client, db, *_ = base
    st = ok(client.post("/api/goals", json={"kind": "emergency", "name": "Fund", "target": 1000, "monthly": 50}), 201)
    c = goal_in(st, st["goal_id"])["category_id"]
    out = ok(client.delete(f"/api/goals/{st['goal_id']}"))
    assert spending.get_setting(db, spending.BUDGET_SAVINGS_SETTING) is None
    assert budget(client)["savings_category"] is None
    st = ok(client.post("/api/goals/restore", json=out["undo"]), 201)
    assert goal_in(st, st["goal_id"])["kind"] == "emergency"
    db.expire_all()
    assert spending.get_setting(db, spending.BUDGET_SAVINGS_SETTING) == c
    # Undo works once (a double click can't make a second goal).
    assert err(client.post("/api/goals/restore", json=out["undo"]), 409)["detail"] == "This can't be undone any more."


def test_delete_and_restore_an_unlinked_goal(base):
    client, db, *_ = base
    old = Goal(name="Old", kind="save", target_cents=1000, monthly_cents=100, hue=1)
    db.add(old)
    db.commit()
    out = ok(client.delete(f"/api/goals/{old.id}", params={"reason": "deleted"}))
    assert (out["undo"]["category_id"], out["undo"]["assigned"], out["undo"]["seed_month"]) == (None, 0.0, None)
    st = ok(client.post("/api/goals/restore", json=out["undo"]), 201)
    g = goal_in(st, st["goal_id"])
    assert (g["name"], g["in_budget"]) == ("Old", False)
    assert client.delete(f"/api/goals/{g['id']}", params={"reason": "nope"}).status_code == 422
    assert client.delete("/api/goals/100000000000000000000").status_code == 422


# ------------------------------------------------------------------ Home and alerts leave goals out


def test_home_and_budget_alert_leave_goal_categories_out(base):
    client, db, chk, _ = base
    st = save_goal(client, monthly=90)
    c = goal_in(st, st["goal_id"])["category_id"]
    # Over the goal's $90 plan: the budget alert fires only above a plan, so this checks that
    # goal categories are left out, not just under their plan.
    add_txn(db, chk.id, "2026-09-20", -95, "Tickets", category=c)
    home = ok(client.get("/api/dashboard"))["budget"]
    assert (home["planned"], home["spent"]) == (500.0, 0.0)
    db.expire_all()
    evaluate(db, dt.date(2026, 9, 26))
    db.commit()
    assert [e.dedupe_key for e in db.scalars(select(AlertEvent).where(AlertEvent.key == "budget"))] == []
    add_txn(db, chk.id, "2026-09-20", -510, "Groceries")  # over the $500 plan
    db.expire_all()
    evaluate(db, dt.date(2026, 9, 26))
    db.commit()
    assert [e.dedupe_key for e in db.scalars(select(AlertEvent).where(AlertEvent.key == "budget"))] == [
        "budget:2026-09:FOOD_AND_DRINK"
    ]


# ------------------------------------------------------------------ validation and auth


GOOD = {"kind": "save", "name": "Trip", "target": 600, "due_month": "2026-12", "monthly": 90}


@pytest.mark.parametrize("change", [
    {"name": ""}, {"name": "   "}, {"name": "x" * 61}, {"target": 0}, {"target": -1}, {"target": 1e13},
    {"monthly": -1}, {"already_saved": -5}, {"due_month": None}, {"due_month": "2026-09"},
    {"due_month": "2028-10"}, {"due_month": "2026-13"}, {"due_month": "26-12"}, {"kind": "debt"},
    {"hue": 3}, {"account_id": 0}, {"account_id": 99999}, {"target": "600"}, {"monthly": True},
    {"kind": "emergency"},  # an emergency fund has no date
])
def test_create_validation(base, change):
    client, *_ = base
    body = {**GOOD, **change}
    r = client.post("/api/goals", json=body)
    assert r.status_code == 422, (change, r.text)
    assert state(client)["goals"] == []


def test_nan_and_infinity_are_rejected(base):
    client, *_ = base
    for raw in ("NaN", "Infinity"):
        content = '{"kind": "save", "name": "Trip", "target": %s, "due_month": "2026-12", "monthly": 1}' % raw
        r = client.post("/api/goals", content=content, headers={"Content-Type": "application/json"})
        assert r.status_code == 422, r.text
        r = client.patch("/api/goals/1", content='{"monthly": %s}' % raw, headers={"Content-Type": "application/json"})
        assert r.status_code == 422, r.text


def test_goals_need_a_session_and_the_csrf_header(base):
    client, *_ = base
    st = save_goal(client)
    gid = st["goal_id"]
    anon = TestClient(client.app, base_url=BASE_URL, headers=CSRF)
    assert anon.get("/api/goals").status_code == 401
    assert anon.post("/api/goals", json=GOOD).status_code == 401
    assert anon.patch(f"/api/goals/{gid}", json={"monthly": 1}).status_code == 401
    assert anon.delete(f"/api/goals/{gid}").status_code == 401
    assert anon.post("/api/goals/restore", json={}).status_code == 401
    no_csrf = {"X-FinTrack": ""}
    assert client.post("/api/goals", json={**GOOD, "name": "Other"}, headers=no_csrf).status_code == 403
    assert client.patch(f"/api/goals/{gid}", json={"monthly": 1}, headers=no_csrf).status_code == 403
    assert client.delete(f"/api/goals/{gid}", headers=no_csrf).status_code == 403
    assert client.post("/api/goals/restore", json={}, headers=no_csrf).status_code == 403
    assert [g["name"] for g in state(client)["goals"]] == ["Trip"]


# ------------------------------------------------------------------ review fixes (2026-09-28)


def _takeover_setup(db) -> str:
    """Emergency savings with $3,000 carried in from August and nothing planned in September:
    Not planned yet is $1,500."""
    sc = _savings_category(db)
    add_budget(db, "2026-08", {sc: 3000})
    add_budget(db, T, {sc: 0})
    return sc


@pytest.mark.parametrize("reason", ["deleted", "spent"])
def test_deleting_a_takeover_goal_hands_emergency_savings_back(base, reason):
    """H1: Delete / "I spent it" of an Emergency fund must not wipe the Emergency savings it took
    over: it stays in the Budget with its money, gets its name back, and stays the Budget's
    savings category. (The page's Undo right after the add: test_undo_of_adding_a_takeover_fund...)"""
    client, db, *_ = base
    sc = _takeover_setup(db)
    assert budget(client)["ready_to_assign"] == 1500.0
    st = ok(client.post("/api/goals", json={
        "kind": "emergency", "name": "Emergency fund", "target": 6000, "monthly": 100,
    }), 201)
    gid = st["goal_id"]
    assert st["not_planned"] == 1400.0 and cat_row(db, sc).name == "Emergency fund"
    out = ok(client.delete(f"/api/goals/{gid}", params={"reason": reason}))
    assert out["kept_in"] == "Emergency savings"
    assert (out["undo"]["category_id"], out["undo"]["removed"], out["undo"]["released"]) == (sc, False, 0.0)
    bm = budget(client)
    assert bm["ready_to_assign"] == 1400.0  # nothing released: the money stays where it is
    assert (line(bm, sc)["available"], line(bm, sc)["assigned"], line(bm, sc)["goal"]) == (3100.0, 100.0, None)
    assert bm["savings_category"] == sc
    row = cat_row(db, sc)
    assert (row.name, row.hidden) == ("Emergency savings", False)
    assert spending.get_setting(db, spending.BUDGET_SAVINGS_SETTING) == sc
    st = state(client)
    assert st["goals"] == [] and st["emergency_available"] == 3000.0
    # Undo of the delete takes it over again.
    st = ok(client.post("/api/goals/restore", json=out["undo"]), 201)
    g = goal_in(st, st["goal_id"])
    assert (g["category_id"], g["saved"], g["planned_this_month"], g["in_budget"]) == (sc, 3000.0, 100.0, True)
    assert st["not_planned"] == 1400.0 and cat_row(db, sc).name == "Emergency fund"
    assert spending.get_setting(db, spending.BUDGET_SAVINGS_SETTING) == sc
    links = json.loads(db.get(AppSetting, "goal_links").value)
    prior = links[str(g["id"])]["prior"]
    assert {k: prior[k] for k in ("name", "hidden", "savings")} == {
        "name": "Emergency savings", "hidden": False, "savings": True,
    }
    # Deleting it again still hands it back.
    out = ok(client.delete(f"/api/goals/{g['id']}"))
    assert out["kept_in"] == "Emergency savings" and line(budget(client), sc)["available"] == 3100.0


def test_goal_edits_change_the_plan_by_the_difference(base):
    """M1: a target or monthly edit changes this month's plan by the difference between the new
    and old plan, so "Use …", "Put it in …" and Move money on the Budget page stay."""
    client, db, *_ = base
    st = ok(client.post("/api/goals", json={
        "kind": "emergency", "name": "Fund", "target": 5000, "monthly": 100, "already_saved": 1000,
    }), 201)
    gid = st["goal_id"]
    c = goal_in(st, gid)["category_id"]
    assert st["not_planned"] == 3400.0
    ok(client.put(f"/api/budgets/{T}", json={"assigned": {c: -300}}))  # "Use Fund": 400 out
    assert state(client)["not_planned"] == 3800.0
    st = ok(client.patch(f"/api/goals/{gid}", json={"target": 6000}))  # the plan is still 100
    g = goal_in(st, gid)
    assert (st["not_planned"], g["planned_this_month"], g["saved"]) == (3800.0, -300.0, 700.0)
    st = ok(client.patch(f"/api/goals/{gid}", json={"monthly": 150}))  # 50 more
    assert (st["not_planned"], goal_in(st, gid)["planned_this_month"]) == (3750.0, -250.0)
    # A name-only or account-only edit doesn't touch the Budget.
    db.expire_all()
    before = db.scalar(select(Budget).where(Budget.month == T, Budget.category == c)).limit_cents
    st = ok(client.patch(f"/api/goals/{gid}", json={"name": "Rainy day"}))
    st = ok(client.patch(f"/api/goals/{gid}", json={"account_id": None}))
    db.expire_all()
    assert db.scalar(select(Budget).where(Budget.month == T, Budget.category == c)).limit_cents == before
    assert st["not_planned"] == 3750.0
    # "Put it in": 500 more, then a target edit keeps it.
    ok(client.put(f"/api/budgets/{T}", json={"assigned": {c: 250}}))
    st = ok(client.patch(f"/api/goals/{gid}", json={"target": 6500}))
    assert (st["not_planned"], goal_in(st, gid)["planned_this_month"]) == (3250.0, 250.0)
    # Lowering the plan below what's been moved out never moves out more than it had.
    ok(client.put(f"/api/budgets/{T}", json={"assigned": {c: -1000}}))  # all 1,000 out
    st = ok(client.patch(f"/api/goals/{gid}", json={"monthly": 0}))
    assert goal_in(st, gid)["planned_this_month"] == -1000.0


def test_emergency_takeover_after_a_move_out_this_month(base):
    """M2: money moved out of Emergency savings this month stays moved out: the new fund
    starts from what's there (emergency_available) and doesn't take more from Not planned yet."""
    client, db, *_ = base
    sc = _savings_category(db)
    add_budget(db, "2026-08", {sc: 1000})
    add_budget(db, T, {sc: -400})  # 400 moved out this month
    st = state(client)
    assert (st["emergency_available"], st["not_planned"]) == (600.0, 3900.0)
    st = ok(client.post("/api/goals", json={"kind": "emergency", "name": "Fund", "target": 5000, "monthly": 100}), 201)
    g = goal_in(st, st["goal_id"])
    assert (g["saved"], g["planned_this_month"], st["not_planned"]) == (600.0, -400.0, 3900.0)
    assert g["projected_month"] == "2030-04"  # 4,400 at 100 a month (44 months), this month first


def test_undo_of_delete_leaves_an_earlier_budget_page_removal(base):
    """M3: a goal the user took out of the Budget on the Budget page stays out after Undo."""
    client, *_ = base
    st = save_goal(client, already_saved=200)
    gid = st["goal_id"]
    c = goal_in(st, gid)["category_id"]
    ok(client.put(f"/api/budgets/{T}", json={"removed": [c]}))
    assert state(client)["not_planned"] == 4500.0
    out = ok(client.delete(f"/api/goals/{gid}"))
    assert (out["undo"]["removed"], out["undo"]["released"], out["state"]["not_planned"]) == (False, 0.0, 4500.0)
    st = ok(client.post("/api/goals/restore", json=out["undo"]), 201)
    g = goal_in(st, st["goal_id"])
    assert (st["not_planned"], g["in_budget"], g["category_id"]) == (4500.0, False, c)


def test_spent_it_works_when_the_goal_is_overspent(base):
    """M4: "I spent it" on a goal that spent more than it had: the overspending comes out of
    Not planned yet (it would at month end anyway) instead of a refusal."""
    client, db, chk, _ = base
    st = save_goal(client, name="Trip", target=600, due="2026-12", monthly=90, already_saved=200)
    g = goal_in(st, st["goal_id"])
    add_txn(db, chk.id, "2026-09-20", -700, "Airline", category=g["category_id"])
    np = state(client)["not_planned"]
    ok(client.put(f"/api/budgets/{T}", json={"assigned": {"FOOD_AND_DRINK": 500 + np}}))  # nothing left
    assert state(client)["not_planned"] == 0.0
    out = ok(client.delete(f"/api/goals/{g['id']}", params={"reason": "spent"}))
    assert (out["undo"]["released"], out["undo"]["removed"], out["state"]["not_planned"]) == (-410.0, True, -410.0)
    st = ok(client.post("/api/goals/restore", json=out["undo"]), 201)
    assert st["not_planned"] == 0.0 and goal_in(st, st["goal_id"])["in_budget"] is True


def test_hidden_categories_a_goal_did_not_leave_are_not_adopted(base):
    """M5: only a hidden custom category a deleted goal left behind is reused; a hidden
    built-in (with its history) or the user's own hidden category is a plain name conflict."""
    client, db, chk, _ = base
    add_budget(db, "2026-08", {"TRAVEL": 300})
    add_txn(db, chk.id, "2026-09-10", -40, "Hotel", category="TRAVEL")
    db.get(TxnCategory, "TRAVEL").hidden = True
    pets = cat_service.create_custom(db, "Pets", "spending")
    pets.hidden = True
    db.commit()
    before = budget(client)["ready_to_assign"]
    for name in ("Travel", "pets"):
        r = err(client.post("/api/goals", json={**GOOD, "name": name}), 409)
        assert r["detail"].startswith("You have a hidden category named") and r["detail"].endswith(
            "Pick another name for the goal."
        )
    assert state(client)["goals"] == [] and budget(client)["ready_to_assign"] == before
    assert (cat_row(db, "TRAVEL").hidden, cat_row(db, pets.id).hidden) == (True, True)


def test_restore_uses_the_delete_record_once(base):
    """L3: restore needs the delete's own record (by token), uses it once, and ignores
    anything else in the body."""
    client, db, *_ = base
    pets = cat_service.create_custom(db, "Pets", "spending")
    db.commit()
    add_budget(db, T, {pets.id: 50})
    forged = {"goal": {"kind": "save", "name": "Whatever", "target": 10, "due_month": "1999-01", "monthly": 0,
                       "account_id": None}, "category_id": pets.id, "month": T, "assigned": 0, "released": 0,
              "seed_month": "2026-08"}
    assert client.post("/api/goals/restore", json=forged).status_code == 422  # no token
    r = err(client.post("/api/goals/restore", json={**forged, "token": "made-up"}), 409)
    assert r["detail"] == "This can't be undone any more."
    old = Goal(name="Old", kind="save", target_cents=1000, monthly_cents=100, hue=1)
    db.add(old)
    db.commit()
    out = ok(client.delete(f"/api/goals/{old.id}"))
    assert out["undo"]["category_id"] is None
    # The body can't point the record at another category or change the goal.
    assert err(client.post("/api/goals/restore", json={**out["undo"], "category_id": pets.id}), 409)
    tampered = {**out["undo"], "goal": {**out["undo"]["goal"], "name": "Hacked", "due_month": "1999-01"}}
    st = ok(client.post("/api/goals/restore", json=tampered), 201)
    assert [(g["name"], g["due_month"]) for g in st["goals"]] == [("Old", None)]
    for _ in range(2):  # a double-clicked Undo
        assert err(client.post("/api/goals/restore", json=out["undo"]), 409)["detail"] == "This can't be undone any more."
    assert len(state(client)["goals"]) == 1
    st = save_goal(client)
    out = ok(client.delete(f"/api/goals/{st['goal_id']}"))
    ok(client.post("/api/goals/restore", json=out["undo"]), 201)
    assert err(client.post("/api/goals/restore", json=out["undo"]), 409)
    assert [g["name"] for g in state(client)["goals"]] == ["Old", "Trip"]


def test_old_goals_zero_target_and_no_date(base):
    """L5: an old goal with target 0 gets no (past) projected month; linking an old save goal
    without a date asks for one."""
    client, db, *_ = base
    zero = Goal(name="Old", kind="save", target_cents=0, monthly_cents=100, hue=1)
    nodate = Goal(name="OldNoDate", kind="save", target_cents=50000, monthly_cents=100, hue=1)
    db.add_all([zero, nodate])
    db.commit()
    st = state(client)
    assert goal_in(st, zero.id)["projected_month"] is None
    r = err(client.patch(f"/api/goals/{nodate.id}", json={"name": "OldNoDate"}))
    assert r["detail"] == "Pick the month you need the money by."
    assert goal_in(state(client), nodate.id)["in_budget"] is False
    g = goal_in(ok(client.patch(f"/api/goals/{nodate.id}", json={"due_month": "2027-06"})), nodate.id)
    assert (g["in_budget"], g["due_month"], g["planned_this_month"]) == (True, "2027-06", 1.0)
    assert err(client.patch(f"/api/goals/{zero.id}", json={"due_month": "2027-06"}))["detail"] == (
        "Type how much you want saved."
    )


def test_goal_whose_category_became_another_kind(base):
    """L6: a goal whose category became Fixed (before Settings refused that, review 3) gets a
    plain way out: a new name puts it back in the Budget."""
    client, db, *_ = base
    st = save_goal(client)
    g = goal_in(st, st["goal_id"])
    cat_row(db, g["category_id"]).kind = "fixed"
    db.commit()
    assert goal_in(state(client), g["id"])["in_budget"] is False
    r = err(client.patch(f"/api/goals/{g['id']}", json={"monthly": 50}), 409)
    assert r["detail"] == (
        "“Trip” isn't a spending category any more, so it can't hold this goal. "
        "Give the goal another name to put it back in your Budget."
    )
    g2 = goal_in(ok(client.patch(f"/api/goals/{g['id']}", json={"name": "Trip savings"})), g["id"])
    assert g2["in_budget"] is True and g2["category_id"] != g["category_id"]


def test_odd_goal_link_keys_are_ignored(base):
    """L7: keys like "²" (isdigit but not a number int() takes) never cause a 500."""
    client, db, *_ = base
    st = save_goal(client)
    c = goal_in(st, st["goal_id"])["category_id"]
    row = db.get(AppSetting, "goal_links")
    data = json.loads(row.value)
    data.update({"²": {"category": c}, "１２": {"category": c}, "-1": {"category": c}})
    row.value = json.dumps(data)
    db.commit()
    st = state(client)
    assert [g["category_id"] for g in st["goals"]] == [c]


# ------------------------------------------------------------------ second review fixes (2026-09-28)


def _add_fund(client, **kw) -> dict:
    body = {"kind": "emergency", "name": "Emergency fund", "target": 6000, "monthly": 100, **kw}
    return ok(client.post("/api/goals", json=body), 201)


def _row(db, month: str, category: str):
    db.expire_all()
    row = db.scalar(select(Budget).where(Budget.month == month, Budget.category == category))
    return None if row is None else (row.limit_cents, row.removed, row.restart)


@pytest.mark.parametrize("september_row", [True, False])
def test_undo_of_adding_a_takeover_fund_puts_everything_back(base, september_row):
    """M1 (review 2, test A): the page's Undo right after adding an Emergency fund that took
    over Emergency savings (with Already saved) puts back exactly what was there: Not planned
    yet, Emergency savings' money, last month's row and this month's plan."""
    client, db, *_ = base
    sc = _savings_category(db)
    add_budget(db, "2026-08", {sc: 1000})
    if september_row:
        add_budget(db, T, {sc: 0})
    before = budget(client)
    assert (before["ready_to_assign"], line(before, sc)["available"]) == (3500.0, 1000.0)
    st = _add_fund(client, already_saved=200)
    assert st["not_planned"] == 3200.0 and line(budget(client), sc)["available"] == 1300.0
    out = ok(client.delete(f"/api/goals/{st['goal_id']}", params={"reason": "undo_add"}))
    assert (out["kept_in"], out["released"]) == ("Emergency savings", 300.0)
    assert out["state"]["goals"] == [] and out["state"]["not_planned"] == 3500.0
    after = budget(client)
    assert (after["ready_to_assign"], line(after, sc)["available"], line(after, sc)["assigned"]) == (
        3500.0, 1000.0, 0.0
    )
    assert _row(db, "2026-08", sc) == (100000, False, False)
    assert _row(db, T, sc) == ((0, False, False) if september_row else None)
    row = cat_row(db, sc)
    assert (row.name, row.hidden) == ("Emergency savings", False)
    assert spending.get_setting(db, spending.BUDGET_SAVINGS_SETTING) == sc
    assert db.get(AppSetting, "goal_undo") is None  # no Undo of the Undo
    assert state(client)["emergency_available"] == 1000.0


def test_undo_of_adding_a_takeover_fund_refuses_after_a_change(base, fixed_today):
    """M1: once the fund's plan changed on the Budget page (or a new month started), the add's
    Undo says so plainly and changes nothing; Delete still works as before."""
    client, db, *_ = base
    sc = _takeover_setup(db)
    st = _add_fund(client, already_saved=200)
    gid = st["goal_id"]
    ok(client.put(f"/api/budgets/{T}", json={"assigned": {sc: 150}}))
    np = state(client)["not_planned"]
    r = err(client.delete(f"/api/goals/{gid}", params={"reason": "undo_add"}), 409)
    assert r["detail"] == (
        "This can't be undone any more: Emergency fund changed on the Budget page since. "
        "You can still delete the goal in its Edit window."
    )
    st = state(client)
    assert [g["id"] for g in st["goals"]] == [gid] and st["not_planned"] == np
    assert cat_row(db, sc).name == "Emergency fund"
    ok(client.put(f"/api/budgets/{T}", json={"assigned": {sc: 100}}))  # back as the add left it
    fixed_today.set(dt.date(2026, 10, 2))
    r = err(client.delete(f"/api/goals/{gid}", params={"reason": "undo_add"}), 409)
    assert r["detail"] == (
        "This can't be undone any more: a new month has started. You can still delete the goal in its Edit window."
    )
    out = ok(client.delete(f"/api/goals/{gid}"))
    assert out["kept_in"] == "Emergency savings"


def test_undo_of_adding_a_goal_with_its_own_category(base):
    """M1: a goal that made its own category: the add's Undo takes it out of the Budget like
    Delete (its money goes back to Not planned yet) and keeps no Undo record."""
    client, db, *_ = base
    st = save_goal(client, already_saved=200)
    assert st["not_planned"] == 4210.0
    out = ok(client.delete(f"/api/goals/{st['goal_id']}", params={"reason": "undo_add"}))
    assert (out["kept_in"], out["released"], out["state"]["not_planned"], out["state"]["goals"]) == (
        None, 290.0, 4500.0, []
    )
    assert db.get(AppSetting, "goal_undo") is None


def test_settings_cannot_delete_or_hide_a_goal_category(base):
    """M2 (test B): Settings › Categories refuses to delete or hide a category that holds a
    goal (including Emergency savings a goal took over), and says where to go instead."""
    client, db, *_ = base
    st = save_goal(client, already_saved=300)
    gid = st["goal_id"]
    c = goal_in(st, gid)["category_id"]
    np = st["not_planned"]
    r = err(client.delete(f"/api/categories/{c}"), 409)
    assert r["detail"] == "“Trip” holds your goal. Delete the goal on the Goals page."
    r = err(client.patch(f"/api/categories/{c}", json={"hidden": True}), 409)
    assert r["detail"] == (
        "“Trip” holds your goal, so it stays on the Budget page. To remove it, delete the goal on the Goals page."
    )
    st = state(client)
    g = goal_in(st, gid)
    assert (g["saved"], g["in_budget"], g["category_id"], st["not_planned"]) == (300.0, True, c, np)
    assert cat_row(db, c).hidden is False
    ok(client.patch(f"/api/categories/{c}", json={"hue": 40}))  # other edits still work
    sc = _takeover_setup(db)
    fund = _add_fund(client)
    assert goal_in(fund, fund["goal_id"])["category_id"] == sc
    r = err(client.delete(f"/api/categories/{sc}"), 409)
    assert r["detail"] == "“Emergency fund” holds your goal. Delete the goal on the Goals page."
    assert err(client.patch(f"/api/categories/{sc}", json={"hidden": True}), 409)
    # Once the goal is deleted, its category can go.
    ok(client.delete(f"/api/goals/{gid}"))
    assert client.delete(f"/api/categories/{c}").status_code == 204


def test_lowering_a_plan_never_overspends_the_envelope(base):
    """L3 (test D): lowering the monthly amount of a goal that spent most of its money only
    takes back what the envelope still has: it ends at $0, not overspent."""
    client, db, chk, _ = base
    st = save_goal(client, name="Trip", target=1000, due="2027-06", monthly=100, already_saved=500)
    gid = st["goal_id"]
    c = goal_in(st, gid)["category_id"]
    add_txn(db, chk.id, "2026-09-20", -580, "Airline", category=c)
    g = goal_in(state(client), gid)
    assert (g["planned_this_month"], g["lowest_plan"]) == (100.0, 80.0)
    ready = budget(client)["ready_to_assign"]
    ok(client.patch(f"/api/goals/{gid}", json={"monthly": 0}))
    bm = budget(client)
    assert (line(bm, c)["assigned"], line(bm, c)["available"]) == (80.0, 0.0)
    assert bm["ready_to_assign"] == ready + 20.0


def test_lowering_a_plan_never_lowers_what_is_saved(base):
    """L3 (test E): with this month's plan already moved out ("Use …"), lowering the monthly
    amount moves nothing more out: Saved stays."""
    client, *_ = base
    st = save_goal(client, name="Trip", target=1000, due="2027-06", monthly=100, already_saved=500)
    gid = st["goal_id"]
    c = goal_in(st, gid)["category_id"]
    ok(client.put(f"/api/budgets/{T}", json={"assigned": {c: -200}}))  # Use 300
    st = state(client)
    g = goal_in(st, gid)
    assert (g["saved"], g["planned_this_month"], g["lowest_plan"]) == (300.0, -200.0, -200.0)
    np = st["not_planned"]
    st = ok(client.patch(f"/api/goals/{gid}", json={"monthly": 0}))
    g = goal_in(st, gid)
    assert (g["saved"], g["planned_this_month"], st["not_planned"]) == (300.0, -200.0, np)


def test_edit_undo_puts_this_months_plan_back_exactly(base):
    """L1 (test H): a lower plan that was held at its floor is undone exactly: the page's Undo
    sends this month's plan as it was, so Not planned yet is what it was."""
    client, *_ = base
    st = save_goal(client, name="Trip", target=1000, due="2027-06", monthly=100)
    gid = st["goal_id"]
    c = goal_in(st, gid)["category_id"]
    ok(client.put(f"/api/budgets/{T}", json={"assigned": {c: 30}}))  # 70 of this month's plan used elsewhere
    st = state(client)
    np0, g = st["not_planned"], goal_in(st, gid)
    assert (g["planned_this_month"], g["lowest_plan"]) == (30.0, 0.0)  # the window's preview: 30 back
    st = ok(client.patch(f"/api/goals/{gid}", json={"monthly": 0}))
    assert (goal_in(st, gid)["planned_this_month"], st["not_planned"]) == (0.0, np0 + 30.0)
    undo = {"monthly": 100, "plan_before": {"month": T, "planned": 30}}
    stale = {**undo, "plan_before": {"month": "2026-08", "planned": 30}}
    assert err(client.patch(f"/api/goals/{gid}", json=stale), 409)["detail"] == (
        "This can't be undone any more: a new month has started."
    )
    for bad in ({"month": T, "planned": "30"}, {"month": T, "planned": 30, "extra": 1}, {"month": T}):
        assert client.patch(f"/api/goals/{gid}", json={**undo, "plan_before": bad}).status_code == 422
    st = ok(client.patch(f"/api/goals/{gid}", json=undo))
    g = goal_in(st, gid)
    assert (g["monthly"], g["planned_this_month"], st["not_planned"]) == (100.0, 30.0, np0)


def test_restore_uses_a_record_once_even_when_two_arrive_together(base, monkeypatch):
    """L4: two restores at the same time can't both use one record. The second one read the
    record before the first one used it (simulated); its conditional write finds it changed."""
    client, db, *_ = base
    old = Goal(name="Old", kind="save", target_cents=1000, monthly_cents=100, hue=1)
    db.add(old)
    db.commit()
    out = ok(client.delete(f"/api/goals/{old.id}"))
    db.expire_all()
    stale = db.get(AppSetting, "goal_undo").value
    ok(client.post("/api/goals/restore", json=out["undo"]), 201)
    real = spending.get_setting

    def stale_read(session, key):
        return stale if key == "goal_undo" else real(session, key)

    monkeypatch.setattr(spending, "get_setting", stale_read)
    r = err(client.post("/api/goals/restore", json=out["undo"]), 409)
    assert r["detail"] == "This can't be undone any more."
    monkeypatch.undo()
    assert [g["name"] for g in state(client)["goals"]] == ["Old"]


# ------------------------------------------------------------------ third review fixes (2026-09-28)


@pytest.mark.parametrize(("spent", "undone"), [(1250, False), (1000.01, False), (1000, True)])
def test_undo_of_adding_a_takeover_fund_never_leaves_it_overspent(base, spent, undone):
    """Review 3: money spent from the fund after the add stays covered. The add's Undo puts the
    rows back only if the envelope doesn't end overspent (or more overspent than it is);
    otherwise 409 and nothing changes."""
    client, db, chk, _ = base
    sc = _savings_category(db)
    add_budget(db, "2026-08", {sc: 1000})
    st = _add_fund(client, already_saved=200)
    gid = st["goal_id"]
    assert line(budget(client), sc)["available"] == 1300.0
    add_txn(db, chk.id, "2026-09-25", -spent, "Car repair", category=sc)
    db.commit()
    before = budget(client)
    rows = (_row(db, "2026-08", sc), _row(db, T, sc))
    response = client.delete(f"/api/goals/{gid}", params={"reason": "undo_add"})
    if undone:
        out = ok(response)
        assert out["kept_in"] == "Emergency savings" and out["state"]["goals"] == []
        assert line(budget(client), sc)["available"] == 0.0
        return
    r = err(response, 409)
    assert r["detail"] == (
        "This can't be undone any more: Emergency fund changed on the Budget page since. "
        "You can still delete the goal in its Edit window."
    )
    after = budget(client)
    assert (after["ready_to_assign"], line(after, sc)["available"]) == (
        before["ready_to_assign"], line(before, sc)["available"]
    )
    assert (_row(db, "2026-08", sc), _row(db, T, sc)) == rows
    assert [g["id"] for g in state(client)["goals"]] == [gid]
    assert cat_row(db, sc).name == "Emergency fund"


def test_edit_undo_never_leaves_the_envelope_overspent(base):
    """Review 3: after a raise and some spending, the edit's Undo can't put back a plan below
    ``lowest_plan`` (the envelope would end overspent): 409, nothing changes. A plan at the
    floor still goes back."""
    client, db, chk, _ = base
    st = save_goal(client, name="Trip", target=1000, due="2027-06", monthly=100)
    gid = st["goal_id"]
    c = goal_in(st, gid)["category_id"]
    ok(client.patch(f"/api/goals/{gid}", json={"monthly": 300}))
    add_txn(db, chk.id, "2026-09-25", -250, "Airline", category=c)
    db.commit()
    st = state(client)
    g, np = goal_in(st, gid), st["not_planned"]
    assert (g["planned_this_month"], g["lowest_plan"]) == (300.0, 250.0)
    undo = {"monthly": 100, "plan_before": {"month": T, "planned": 100}}
    r = err(client.patch(f"/api/goals/{gid}", json=undo), 409)
    assert r["detail"] == (
        "This can't be undone any more: money from Trip was spent since. "
        "You can change the monthly amount in its Edit window."
    )
    st = state(client)
    g = goal_in(st, gid)
    assert (g["monthly"], g["planned_this_month"], st["not_planned"]) == (300.0, 300.0, np)
    assert line(budget(client), c)["available"] == 50.0
    st = ok(client.patch(f"/api/goals/{gid}", json={**undo, "plan_before": {"month": T, "planned": 250}}))
    assert goal_in(st, gid)["planned_this_month"] == 250.0
    assert line(budget(client), c)["available"] == 0.0


def test_settings_cannot_change_a_goal_categorys_kind(base):
    """Review 3: changing the kind of a category that holds a goal (Emergency savings a goal
    took over included) away from spending gets around the hide guard, so it's refused the
    same way; other edits and a goal-free category still work."""
    client, db, *_ = base
    st = save_goal(client, already_saved=300)
    gid = st["goal_id"]
    c = goal_in(st, gid)["category_id"]
    np = st["not_planned"]
    msg = "“Trip” holds your goal, so it stays a spending category. To change it, delete the goal on the Goals page."
    for kind in ("fixed", "income", "transfer"):
        assert err(client.patch(f"/api/categories/{c}", json={"kind": kind}), 409)["detail"] == msg
    assert err(client.patch(f"/api/categories/{c}", json={"kind": "fixed", "hue": 40}), 409)
    ok(client.patch(f"/api/categories/{c}", json={"kind": "spending", "hue": 40}))
    row = cat_row(db, c)
    assert (row.kind, row.hue) == ("spending", 40)
    st = state(client)
    g = goal_in(st, gid)
    assert (g["in_budget"], g["saved"], g["category_id"], st["not_planned"]) == (True, 300.0, c, np)
    sc = _takeover_setup(db)
    _add_fund(client)
    r = err(client.patch(f"/api/categories/{sc}", json={"kind": "fixed"}), 409)
    assert r["detail"] == (
        "“Emergency fund” holds your goal, so it stays a spending category. To change it, delete the goal on the Goals page."
    )
    assert cat_row(db, sc).kind == "spending"
    ok(client.delete(f"/api/goals/{gid}"))
    assert ok(client.patch(f"/api/categories/{c}", json={"kind": "fixed"}))["kind"] == "fixed"

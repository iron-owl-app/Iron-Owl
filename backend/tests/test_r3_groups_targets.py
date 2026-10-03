"""Release 3: category groups, budget targets and fund-targets (hand-computed cents)."""
from __future__ import annotations

import pytest

from tests.conftest import add_budget, add_txn, make_account

TODAY_MONTH = "2026-09"  # fixed_today = 2026-09-26


def ok(response) -> dict:
    assert response.status_code == 200, response.text
    return response.json()


def lines(budget: dict) -> dict[str, dict]:
    return {c["category"]: c for c in budget["categories"]}


def get(client, month=TODAY_MONTH) -> dict:
    return ok(client.get("/api/budgets", params={"month": month}))


def save(client, month, assigned=None, removed=None):
    body = {}
    if assigned is not None:
        body["assigned"] = assigned
    if removed is not None:
        body["removed"] = removed
    return client.put(f"/api/budgets/{month}", json=body)


def target(client, category, body):
    return client.patch(f"/api/categories/{category}", json={"target": body})


# ------------------------------------------------------------------ groups


def test_group_crud_reorder_and_membership(unlocked, fixed_today):
    c = unlocked
    assert ok(c.get("/api/category-groups")) == []
    r = c.post("/api/category-groups", json={"name": "  Fun   stuff "})
    assert r.status_code == 201
    fun = r.json()
    assert fun == {"id": fun["id"], "name": "Fun stuff", "position": 0, "category_ids": []}
    home = c.post("/api/category-groups", json={"name": "Home"}).json()
    assert home["position"] == 1
    assert c.post("/api/category-groups", json={"name": "HOME"}).status_code == 409
    assert c.post("/api/category-groups", json={"name": "   "}).status_code == 422
    assert c.post("/api/category-groups", json={"name": "x" * 61}).status_code == 422
    assert c.post("/api/category-groups", json={"name": "A", "position": 3}).status_code == 422

    # Categories join a group through the category PATCH; unknown groups are refused.
    cat = ok(c.patch("/api/categories/ENTERTAINMENT", json={"group_id": fun["id"]}))
    assert cat["group_id"] == fun["id"]
    ok(c.patch("/api/categories/TRAVEL", json={"group_id": fun["id"]}))
    ok(c.patch("/api/categories/RENT_AND_UTILITIES", json={"group_id": home["id"]}))
    assert c.patch("/api/categories/TRAVEL", json={"group_id": 999}).status_code == 422
    groups = ok(c.get("/api/category-groups"))
    assert [g["category_ids"] for g in groups] == [["ENTERTAINMENT", "TRAVEL"], ["RENT_AND_UTILITIES"]]

    # Rename (duplicate names refused), reorder (must be a permutation).
    assert ok(c.patch(f"/api/category-groups/{fun['id']}", json={"name": "Fun"}))["name"] == "Fun"
    assert c.patch(f"/api/category-groups/{fun['id']}", json={"name": "home"}).status_code == 409
    assert c.patch("/api/category-groups/999", json={"name": "X"}).status_code == 404
    assert c.post("/api/category-groups/reorder", json={"ids": [home["id"]]}).status_code == 422
    assert c.post("/api/category-groups/reorder", json={"ids": [home["id"], home["id"]]}).status_code == 422
    reordered = ok(c.post("/api/category-groups/reorder", json={"ids": [home["id"], fun["id"]]}))
    assert [(g["name"], g["position"]) for g in reordered] == [("Home", 0), ("Fun", 1)]

    # The budget month lists the groups and each line's group.
    b = ok(save(c, TODAY_MONTH, {"TRAVEL": 0, "FOOD_AND_DRINK": 0}))
    assert b["groups"] == [{"id": home["id"], "name": "Home", "position": 0},
                           {"id": fun["id"], "name": "Fun", "position": 1}]
    assert lines(b)["TRAVEL"]["group_id"] == fun["id"] and lines(b)["FOOD_AND_DRINK"]["group_id"] is None

    # Deleting a group ungroups its categories.
    assert c.delete(f"/api/category-groups/{fun['id']}").status_code == 204
    assert c.delete(f"/api/category-groups/{fun['id']}").status_code == 404
    cats = {x["id"]: x for x in ok(c.get("/api/categories"))}
    assert cats["TRAVEL"]["group_id"] is None and cats["RENT_AND_UTILITIES"]["group_id"] == home["id"]
    ok(c.patch("/api/categories/RENT_AND_UTILITIES", json={"group_id": None}))
    assert ok(c.get("/api/category-groups"))[0]["category_ids"] == []


def test_starter_groups_only_take_ungrouped_categories(unlocked, fixed_today):
    c = unlocked
    mine = c.post("/api/category-groups", json={"name": "Mine"}).json()
    existing_home = c.post("/api/category-groups", json={"name": "home"}).json()  # starter "Home" exists already
    ok(c.patch("/api/categories/TRAVEL", json={"group_id": mine["id"]}))
    groups = ok(c.post("/api/category-groups/starter", json={}))
    by_name = {g["name"]: g for g in groups}
    assert list(by_name) == ["Mine", "home", "Car", "Food", "Personal", "Health", "Bills & services"]
    assert by_name["home"]["id"] == existing_home["id"]
    assert by_name["home"]["category_ids"] == ["RENT_AND_UTILITIES", "HOME_IMPROVEMENT"]
    assert by_name["Mine"]["category_ids"] == ["TRAVEL"]  # not moved to Personal
    assert by_name["Personal"]["category_ids"] == ["GENERAL_MERCHANDISE", "ENTERTAINMENT", "PERSONAL_CARE"]
    assert by_name["Bills & services"]["category_ids"] == ["GENERAL_SERVICES", "BANK_FEES", "GOVERNMENT_AND_NON_PROFIT"]
    assert by_name["Car"]["category_ids"] == ["TRANSPORTATION"] and by_name["Health"]["category_ids"] == ["MEDICAL"]
    # Idempotent.
    assert ok(c.post("/api/category-groups/starter", json={})) == groups


# ------------------------------------------------------------------ targets


def test_target_validation(unlocked, fixed_today):
    c = unlocked
    t = ok(target(c, "FOOD_AND_DRINK", {"kind": "monthly", "amount": 150}))["target"]
    assert t == {"kind": "monthly", "amount": 150.0, "date": None}
    t = ok(target(c, "TRAVEL", {"kind": "by_date", "amount": 1000, "date": "2027-02-15"}))["target"]
    assert t == {"kind": "by_date", "amount": 1000.0, "date": "2027-02-15"}
    # Today is allowed; the past isn't.
    assert ok(target(c, "MEDICAL", {"kind": "by_date", "amount": 5, "date": "2026-09-26"}))["target"]["date"] == "2026-09-26"
    assert target(c, "MEDICAL", {"kind": "by_date", "amount": 5, "date": "2026-09-25"}).status_code == 422
    assert target(c, "MEDICAL", {"kind": "by_date", "amount": 5}).status_code == 422
    assert target(c, "MEDICAL", {"kind": "monthly", "amount": 5, "date": "2027-01-01"}).status_code == 422
    for bad in (0, -1, 1e13):
        assert target(c, "MEDICAL", {"kind": "monthly", "amount": bad}).status_code == 422
    assert target(c, "MEDICAL", {"kind": "weekly", "amount": 5}).status_code == 422
    assert target(c, "MEDICAL", {"kind": "monthly", "amount": 5, "extra": 1}).status_code == 422
    for raw in ('{"target": {"kind": "monthly", "amount": NaN}}', '{"target": {"kind": "monthly", "amount": Infinity}}'):
        r = c.patch("/api/categories/MEDICAL", content=raw, headers={"Content-Type": "application/json"})
        assert r.status_code == 422
    # Only spending categories.
    assert target(c, "INCOME", {"kind": "monthly", "amount": 5}).status_code == 422
    # null clears; changing the kind away from spending clears it too.
    assert ok(target(c, "MEDICAL", None))["target"] is None
    kids = c.post("/api/categories", json={"name": "Kids", "kind": "spending"}).json()
    ok(target(c, kids["id"], {"kind": "monthly", "amount": 20}))
    assert ok(c.patch(f"/api/categories/{kids['id']}", json={"kind": "fixed"}))["target"] is None
    cats = {x["id"]: x for x in ok(c.get("/api/categories"))}
    assert cats["FOOD_AND_DRINK"]["target"]["amount"] == 150.0 and cats["INCOME"]["target"] is None


@pytest.fixture
def targets(unlocked, db, fixed_today):
    """Cash 300 in checking; Food (monthly 150) and Travel (1000 by Feb 15, 2027) and
    Entertainment (monthly 40) in the budget; groups: A (Travel) before B (Food)."""
    c = unlocked
    chk = make_account(db, "Checking", "bank", 300)
    a = c.post("/api/category-groups", json={"name": "A"}).json()
    b = c.post("/api/category-groups", json={"name": "B"}).json()
    ok(c.patch("/api/categories/TRAVEL", json={"group_id": a["id"]}))
    ok(c.patch("/api/categories/FOOD_AND_DRINK", json={"group_id": b["id"]}))
    ok(target(c, "FOOD_AND_DRINK", {"kind": "monthly", "amount": 150}))
    ok(target(c, "TRAVEL", {"kind": "by_date", "amount": 1000, "date": "2027-02-15"}))
    ok(target(c, "ENTERTAINMENT", {"kind": "monthly", "amount": 40}))
    ok(save(c, TODAY_MONTH, {"FOOD_AND_DRINK": 100, "TRAVEL": 0, "ENTERTAINMENT": 0}))
    return c, chk, db


def test_needed_per_line(targets):
    c, chk, _db = targets
    ok(c.patch(f"/api/accounts/{chk.id}", json={"current_balance": 1000}))  # room to assign
    sep = get(c)
    line = lines(sep)
    # Monthly: 150 - 100 assigned.
    assert line["FOOD_AND_DRINK"]["target"] == {"kind": "monthly", "amount": 150.0, "date": None, "needed": 50.0}
    # By date: Sep..Feb is 6 months; 1000 / 6 = 166.666.. -> 166.67 (ceil to the cent).
    assert line["TRAVEL"]["target"] == {"kind": "by_date", "amount": 1000.0, "date": "2027-02-15", "needed": 166.67}
    assert line["ENTERTAINMENT"]["target"]["needed"] == 40.0
    assert sep["needed_total"] == 256.67  # 50 + 166.67 + 40

    # Over-assigned monthly target: needed is 0, never negative.
    ok(save(c, TODAY_MONTH, {"ENTERTAINMENT": 55}))
    assert lines(get(c))["ENTERTAINMENT"]["target"]["needed"] == 0.0

    # By date across months: assign 166.67 in September; October carries it over:
    # (1000 - 166.67) / 5 months = 166.666 -> 166.67.
    ok(save(c, TODAY_MONTH, {"TRAVEL": 166.67}))
    assert lines(get(c))["TRAVEL"]["target"]["needed"] == 0.0
    oct_ = get(c, "2026-10")
    assert lines(oct_)["TRAVEL"]["carryover"] == 166.67
    assert lines(oct_)["TRAVEL"]["target"]["needed"] == 166.67
    # Food carries 100 into October, but a monthly target counts only what's assigned there.
    assert lines(oct_)["FOOD_AND_DRINK"]["target"]["needed"] == 150.0
    # The target month itself: months_left = 1. Carryover by Feb = 166.67 (only September).
    feb = get(c, "2027-02")
    assert lines(feb)["TRAVEL"]["carryover"] == 166.67
    assert lines(feb)["TRAVEL"]["target"]["needed"] == 833.33
    # After the target month nothing more is needed.
    assert lines(get(c, "2027-03"))["TRAVEL"]["target"]["needed"] == 0.0
    # Past months still show a target (read-only).
    assert get(c, "2026-09")["categories"]


def test_by_date_counts_carryover_not_spending_this_month(targets):
    c, chk, db = targets
    add_budget(db, "2026-08", {"TRAVEL": 400})  # history: August put 400 aside
    add_txn(db, chk.id, "2026-08-20", -100, "Train", category="TRAVEL")
    add_txn(db, chk.id, "2026-09-05", -50, "Bus", category="TRAVEL")
    line = lines(get(c))["TRAVEL"]
    # Carryover into September = 400 - 100 = 300; (1000 - 300) / 6 = 116.666 -> 116.67.
    assert line["carryover"] == 300.0 and line["spent"] == 50.0
    assert line["target"]["needed"] == 116.67


def test_fund_targets_partial_at_ready_to_assign_limit(targets):
    c, chk, db = targets
    before = get(c)
    assert before["ready_to_assign"] == 200.0  # cash 300 - Food 100
    r = c.post(f"/api/budgets/{TODAY_MONTH}/fund-targets", json={})
    body = ok(r)
    # Group A (Travel) first: 166.67, leaving 33.33 for Food (group B) of its 50; then stop.
    assert body["funded"] == 200.0 and body["unfunded"] == 56.67
    month = body["month"]
    assert month["ready_to_assign"] == 0.0
    line = lines(month)
    assert (line["TRAVEL"]["assigned"], line["FOOD_AND_DRINK"]["assigned"], line["ENTERTAINMENT"]["assigned"]) == (
        166.67, 133.33, 0.0)
    assert month["needed_total"] == 56.67  # Food 16.67 + Entertainment 40

    # Nothing left to assign: nothing funded, nothing changes.
    again = ok(c.post(f"/api/budgets/{TODAY_MONTH}/fund-targets", json={}))
    assert (again["funded"], again["unfunded"]) == (0.0, 56.67)
    assert lines(again["month"])["FOOD_AND_DRINK"]["assigned"] == 133.33

    # More cash: fund October (a future month). Needs there: Travel (1000 - 166.67) / 5 ->
    # 166.67, Food 150, Entertainment 40 = 356.67; Ready to assign = 1000 - 300 = 700.
    ok(c.patch(f"/api/accounts/{chk.id}", json={"current_balance": 1000}))
    oct_ = ok(c.post("/api/budgets/2026-10/fund-targets", json={}))
    assert (oct_["funded"], oct_["unfunded"]) == (356.67, 0.0)
    assert oct_["month"]["ready_to_assign"] == 343.33
    assert oct_["month"]["needed_total"] == 0.0
    assert {k: v["assigned"] for k, v in lines(oct_["month"]).items()} == {
        "FOOD_AND_DRINK": 150.0, "TRAVEL": 166.67, "ENTERTAINMENT": 40.0}


def test_fund_targets_ungrouped_last_then_position(unlocked, db, fixed_today):
    c = unlocked
    make_account(db, "Checking", "bank", 60)
    g = c.post("/api/category-groups", json={"name": "G"}).json()
    ok(c.patch("/api/categories/MEDICAL", json={"group_id": g["id"]}))
    for cat in ("FOOD_AND_DRINK", "TRANSPORTATION", "MEDICAL"):
        ok(target(c, cat, {"kind": "monthly", "amount": 25}))
    ok(save(c, TODAY_MONTH, {"FOOD_AND_DRINK": 0, "TRANSPORTATION": 0, "MEDICAL": 0}))
    body = ok(c.post(f"/api/budgets/{TODAY_MONTH}/fund-targets", json={}))
    # Grouped Medical first (25), then ungrouped by position: Food 25, Transportation 10 of 25.
    assert {k: v["assigned"] for k, v in lines(body["month"]).items()} == {
        "FOOD_AND_DRINK": 25.0, "TRANSPORTATION": 10.0, "MEDICAL": 25.0}
    assert (body["funded"], body["unfunded"]) == (60.0, 15.0)


def test_fund_targets_month_rules(targets):
    c, _chk, _db = targets
    r = c.post("/api/budgets/2026-08/fund-targets", json={})
    assert r.status_code == 422 and r.json()["detail"] == "Past months are read-only. Make changes in the current month."
    assert c.post("/api/budgets/2027-10/fund-targets", json={}).status_code == 422
    assert c.post("/api/budgets/2026-13/fund-targets", json={}).status_code == 422
    # A month with no targets needed funds nothing.
    ok(target(c, "FOOD_AND_DRINK", None))
    ok(target(c, "TRAVEL", None))
    ok(target(c, "ENTERTAINMENT", None))
    body = ok(c.post(f"/api/budgets/{TODAY_MONTH}/fund-targets", json={}))
    assert (body["funded"], body["unfunded"], body["month"]["needed_total"]) == (0.0, 0.0, 0.0)


def test_fund_targets_with_negative_ready_to_assign_funds_nothing(targets):
    c, chk, _db = targets
    ok(c.patch(f"/api/accounts/{chk.id}", json={"current_balance": 50}))  # RTA = 50 - 100 = -50
    body = ok(c.post(f"/api/budgets/{TODAY_MONTH}/fund-targets", json={}))
    assert body["funded"] == 0.0 and body["month"]["ready_to_assign"] == -50.0

"""Budget page delete (Simple view): lines say whether the category can be deleted."""
from __future__ import annotations

from tests.conftest import add_budget


MONTH = "2026-09"
FOOD = "FOOD_AND_DRINK"


def ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json() if response.content else None


def lines(client):
    month = ok(client.get("/api/budgets", params={"month": MONTH}))
    return {line["category"]: line for line in month["categories"]}


def test_budget_lines_mark_only_user_made_categories_as_deletable(unlocked, db, fixed_today):
    mine = ok(unlocked.post("/api/categories", json={"name": "Credit cards", "kind": "spending"}), 201)
    add_budget(db, MONTH, {FOOD: 100, mine["id"]: 50})
    by_id = lines(unlocked)
    assert by_id[mine["id"]]["custom"] is True
    assert by_id[FOOD]["custom"] is False
    # The flag matches what DELETE accepts: a built-in category is refused, the user's own is deleted.
    assert unlocked.delete(f"/api/categories/{FOOD}").status_code == 400
    assert unlocked.delete(f"/api/categories/{mine['id']}").status_code == 204
    assert mine["id"] not in lines(unlocked)


def test_deleting_a_group_keeps_its_categories_without_a_group(unlocked, db, fixed_today):
    group = ok(unlocked.post("/api/category-groups", json={"name": "Cards"}), 201)
    mine = ok(unlocked.post("/api/categories", json={"name": "Credit cards", "kind": "spending", "group_id": group["id"]}), 201)
    add_budget(db, MONTH, {mine["id"]: 40})
    assert lines(unlocked)[mine["id"]]["group_id"] == group["id"]
    assert unlocked.delete(f"/api/category-groups/{group['id']}").status_code == 204
    line = lines(unlocked)[mine["id"]]
    assert line["group_id"] is None and line["assigned"] == 40

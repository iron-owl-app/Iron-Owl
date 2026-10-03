"""Categories, transaction edits, rules (precedence, overrides, transfer flag) and CSV export."""
from __future__ import annotations

import csv
import io

import pytest

from app.services.rules import Matcher
from tests.conftest import add_txn, make_account


@pytest.fixture
def setup(unlocked, db, fixed_today):
    acct = make_account(db, "Everyday", "bank", 1000)
    t = {
        "coffee": add_txn(db, acct.id, "2026-09-01", -4.5, "SQ *BLUE BOTTLE 1234", merchant="Blue Bottle"),
        "grocer": add_txn(db, acct.id, "2026-09-02", -82.1, "WHOLEFDS #123", merchant="Whole Foods",
                          category="FOOD_AND_DRINK"),
        "xfer": add_txn(db, acct.id, "2026-09-03", -500, "Online transfer to savings", category="TRANSFER_OUT"),
        "pay": add_txn(db, acct.id, "2026-09-04", 2500, "ACME PAYROLL", category="INCOME"),
        "none": add_txn(db, acct.id, "2026-09-05", -12, "Mystery", category=None),
    }
    return unlocked, acct, {k: v.id for k, v in t.items()}


def txns(client, **params) -> dict[int, dict]:
    return {t["id"]: t for t in client.get("/api/transactions", params=params).json()["items"]}


# ------------------------------------------------------------------ categories


def test_categories_seeded_and_ordered(unlocked):
    cats = unlocked.get("/api/categories").json()
    assert cats[0] == {"id": "FOOD_AND_DRINK", "name": "Food and drink", "hue": 25, "kind": "spending",
                       "custom": False, "hidden": False, "group_id": None, "target": None}
    kinds = [c["kind"] for c in cats]
    assert kinds == sorted(kinds, key=["spending", "income", "transfer", "fixed"].index)
    assert [c["id"] for c in cats if c["kind"] != "spending"] == ["INCOME", "TRANSFER_IN", "TRANSFER_OUT",
                                                                 "LOAN_PAYMENTS"]


def test_category_crud(setup):
    client, _acct, ids = setup
    r = client.post("/api/categories", json={"name": "  Kids  ", "kind": "spending"})
    assert r.status_code == 201
    kids = r.json()
    assert kids["id"] == "c_1" and kids["name"] == "Kids" and kids["custom"] is True
    assert 0 <= kids["hue"] <= 359
    assert client.post("/api/categories", json={"name": "kids", "kind": "spending"}).status_code == 409
    assert client.post("/api/categories", json={"name": "FOOD AND DRINK", "kind": "fixed"}).status_code == 409
    assert client.post("/api/categories", json={"name": "Gym", "hue": 400, "kind": "spending"}).status_code == 422
    assert client.post("/api/categories", json={"name": "Gym", "kind": "savings"}).status_code == 422
    gym = client.post("/api/categories", json={"name": "Gym", "hue": 12, "kind": "fixed"}).json()
    assert gym["id"] == "c_2" and gym["hue"] == 12

    r = client.patch("/api/categories/c_1", json={"name": "Children", "hidden": True, "hue": 90})
    assert r.json() == {"id": "c_1", "name": "Children", "hue": 90, "kind": "spending", "custom": True, "hidden": True,
                        "group_id": None, "target": None}
    assert client.patch("/api/categories/c_1", json={"name": "gym"}).status_code == 409
    assert client.patch("/api/categories/c_1", json={"kind": None}).status_code == 422
    assert client.patch("/api/categories/OTHER", json={"name": "Misc"}).json()["name"] == "Misc"
    assert client.patch("/api/categories/nope", json={"name": "x"}).status_code == 404

    # Seeded categories can't be deleted.
    assert client.delete("/api/categories/FOOD_AND_DRINK").status_code == 400

    # Deleting a custom category: its transactions revert to automatic, its budgets go.
    client.patch(f"/api/transactions/{ids['coffee']}", json={"category": "c_1"})
    r = client.put("/api/budgets/2026-09", json={"assigned": {"c_1": 50, "FOOD_AND_DRINK": 20}})
    assert r.status_code == 200, r.text
    client.post("/api/rules", json={"field": "any", "op": "contains", "text": "gym", "action": "category",
                                    "category": "c_2"})
    # Settings D7: deleting a category a rule uses deletes those rules too (was 409).
    assert client.delete("/api/categories/c_2").status_code == 204
    assert client.get("/api/rules").json() == []
    assert client.delete("/api/categories/c_1").status_code == 204
    coffee = txns(client)[ids["coffee"]]
    assert coffee["category"] == "FOOD_AND_DRINK" and coffee["category_source"] == "plaid"
    budget = client.get("/api/budgets", params={"month": "2026-09"}).json()
    assert [line["category"] for line in budget["categories"]] == ["FOOD_AND_DRINK"]
    # Ids are never reused.
    assert client.post("/api/categories", json={"name": "Kids again", "kind": "spending"}).json()["id"] == "c_3"


# ------------------------------------------------------------------ transaction edits


def test_transaction_patch_and_reset(setup):
    client, _acct, ids = setup
    r = client.patch(f"/api/transactions/{ids['coffee']}", json={"category": "ENTERTAINMENT", "notes": " date "})
    assert r.status_code == 200
    body = r.json()
    assert body["category"] == "ENTERTAINMENT" and body["category_source"] == "user"
    assert body["category_name"] == "Entertainment" and body["category_hue"] == 340
    assert body["plaid_category"] == "FOOD_AND_DRINK" and body["notes"] == "date" and body["rule_id"] is None
    assert body["account_name"] == "Everyday" and body["amount"] == -4.5

    assert client.patch(f"/api/transactions/{ids['coffee']}", json={"category": "NOPE"}).status_code == 422
    assert client.patch(f"/api/transactions/{ids['coffee']}", json={"is_transfer": None}).status_code == 422
    assert client.patch(f"/api/transactions/{ids['coffee']}", json={"amount": 1}).status_code == 422
    assert client.patch("/api/transactions/99999", json={"notes": "x"}).status_code == 404

    r = client.patch(f"/api/transactions/{ids['xfer']}", json={"is_transfer": True})
    assert r.json()["is_transfer"] is True

    # null resets to automatic: rules first, then Plaid.
    client.post("/api/rules", json={"field": "merchant", "op": "contains", "text": "bottle", "action": "category",
                                    "category": "TRAVEL"})
    assert txns(client)[ids["coffee"]]["category"] == "ENTERTAINMENT"  # user override beats the rule
    r = client.patch(f"/api/transactions/{ids['coffee']}", json={"category": None, "notes": None})
    body = r.json()
    assert body["category"] == "TRAVEL" and body["category_source"] == "rule" and body["rule_id"] is not None
    assert body["notes"] is None


def test_transaction_filters_by_category_and_notes(setup):
    client, _acct, ids = setup
    client.patch(f"/api/transactions/{ids['grocer']}", json={"notes": "Birthday cake"})
    assert set(txns(client, search="birthday")) == {ids["grocer"]}
    assert set(txns(client, category="FOOD_AND_DRINK")) == {ids["coffee"], ids["grocer"]}
    assert set(txns(client, category="OTHER")) == {ids["none"]}  # NULL counts as Other
    assert client.get("/api/transactions", params={"category": "INCOME"}).json()["total"] == 1
    none = txns(client)[ids["none"]]
    assert none["category"] is None and none["category_name"] == "Other" and none["category_hue"] == 0


# ------------------------------------------------------------------ rules


def rule(client, **body) -> dict:
    payload = {"field": "any", "op": "contains", "action": "category", **body}
    r = client.post("/api/rules", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def test_rule_crud_and_matches(setup):
    client, _acct, ids = setup
    a = rule(client, text="blue bottle", category="ENTERTAINMENT")
    assert a == {"id": a["id"], "position": 0, "field": "any", "op": "contains", "text": "blue bottle",
                 "amount_op": None, "amount": None, "action": "category", "category": "ENTERTAINMENT",
                 "enabled": True, "matches": 1}
    b = rule(client, field="name", op="is", text=" acme payroll ", action="transfer", category="TRAVEL")
    assert b["position"] == 1 and b["category"] is None and b["text"] == "acme payroll" and b["matches"] == 1
    pay = txns(client)[ids["pay"]]
    assert pay["is_transfer"] is True and pay["category"] == "INCOME" and pay["category_source"] == "plaid"
    assert pay["rule_id"] == b["id"]

    assert client.post("/api/rules", json={"field": "any", "op": "contains", "text": "x", "action": "category"}).status_code == 422
    assert client.post("/api/rules", json={"field": "any", "op": "contains", "text": "x", "action": "category",
                                           "category": "NOPE"}).status_code == 422
    assert client.post("/api/rules", json={"field": "any", "op": "contains", "text": "", "action": "transfer"}).status_code == 422
    assert client.post("/api/rules", json={"field": "any", "op": "like", "text": "x", "action": "transfer"}).status_code == 422
    assert client.post("/api/rules", json={"field": "any", "op": "is", "text": "x", "action": "transfer",
                                           "amount_op": "gt"}).status_code == 422

    # Disabling the transfer rule clears the flag it set.
    r = client.patch(f"/api/rules/{b['id']}", json={"enabled": False})
    assert r.json()["enabled"] is False and r.json()["matches"] == 0
    pay = txns(client)[ids["pay"]]
    assert pay["is_transfer"] is False and pay["rule_id"] is None
    assert client.patch(f"/api/rules/{b['id']}", json={"text": None}).status_code == 422
    assert client.patch("/api/rules/999", json={"enabled": True}).status_code == 404

    listed = client.get("/api/rules").json()
    assert [(r["id"], r["matches"]) for r in listed] == [(a["id"], 1), (b["id"], 0)]

    assert client.delete(f"/api/rules/{a['id']}").status_code == 204
    coffee = txns(client)[ids["coffee"]]
    assert coffee["category"] == "FOOD_AND_DRINK" and coffee["category_source"] == "plaid" and coffee["rule_id"] is None
    assert [r["position"] for r in client.get("/api/rules").json()] == [0]
    assert client.delete(f"/api/rules/{a['id']}").status_code == 404


def test_first_matching_rule_wins_and_reorder(setup):
    client, _acct, ids = setup
    travel = rule(client, text="bottle", category="TRAVEL")
    ent = rule(client, field="merchant", op="is", text="BLUE BOTTLE", category="ENTERTAINMENT")
    assert txns(client)[ids["coffee"]]["category"] == "TRAVEL"
    r = client.post("/api/rules/reorder", json={"ids": [ent["id"], travel["id"]]})
    assert r.status_code == 200
    assert [(x["id"], x["position"], x["matches"]) for x in r.json()] == [(ent["id"], 0, 1), (travel["id"], 1, 0)]
    assert txns(client)[ids["coffee"]]["category"] == "ENTERTAINMENT"
    assert client.post("/api/rules/reorder", json={"ids": [ent["id"]]}).status_code == 422
    assert client.post("/api/rules/reorder", json={"ids": [ent["id"], ent["id"]]}).status_code == 422
    assert client.post("/api/rules/reorder", json={"ids": [ent["id"], travel["id"], 999]}).status_code == 422


def test_amount_condition_uses_absolute_amount(setup):
    client, _acct, ids = setup
    r = rule(client, field="any", op="contains", text="o", amount_op="gt", amount=80, category="HOME_IMPROVEMENT")
    # "o" appears in coffee (4.50), grocer (82.10), transfer (500), payroll (+2500), none ("Mystery" has no o)
    matched = {i for i, t in txns(client).items() if t["rule_id"] == r["id"]}
    assert matched == {ids["grocer"], ids["xfer"], ids["pay"]}
    client.patch(f"/api/rules/{r['id']}", json={"amount_op": "lt", "amount": 80})
    matched = {i for i, t in txns(client).items() if t["rule_id"] == r["id"]}
    assert matched == {ids["coffee"]}
    client.patch(f"/api/rules/{r['id']}", json={"amount_op": None})
    assert client.get("/api/rules").json()[0]["amount"] is None


def test_preview_ignores_other_rules(setup):
    client, _acct, _ids = setup
    rule(client, text="o", category="TRAVEL")
    body = {"field": "name", "op": "contains", "text": "WHOLEFDS", "action": "category", "category": "OTHER"}
    assert client.post("/api/rules/preview", json=body).json()["matches"] == 1
    body = {"field": "merchant", "op": "is", "text": "blue", "action": "transfer"}
    assert client.post("/api/rules/preview", json=body).json()["matches"] == 0
    body = {"field": "any", "op": "contains", "text": "e", "action": "transfer", "amount_op": "gt", "amount": 100}
    assert client.post("/api/rules/preview", json=body).json()["matches"] == 2  # transfer 500, payroll 2500


def test_user_transfer_flag_is_never_touched_by_rules(setup):
    client, _acct, ids = setup
    client.patch(f"/api/transactions/{ids['grocer']}", json={"is_transfer": False})
    r = rule(client, text="whole", action="transfer")
    grocer = txns(client)[ids["grocer"]]
    assert grocer["is_transfer"] is False and grocer["rule_id"] == r["id"]
    client.patch(f"/api/transactions/{ids['coffee']}", json={"is_transfer": True})
    client.delete(f"/api/rules/{r['id']}")
    assert txns(client)[ids["coffee"]]["is_transfer"] is True


@pytest.mark.parametrize(
    ("field", "op", "text", "name", "merchant", "expected"),
    [
        ("any", "contains", "BOTTLE", "SQ *BLUE BOTTLE", None, True),
        ("any", "contains", "bottle", "x", "Blue Bottle", True),
        ("name", "contains", "bottle", "x", "Blue Bottle", False),
        ("merchant", "contains", "bottle", "Blue Bottle", None, False),
        ("merchant", "is", "blue bottle", "x", "  Blue Bottle ", True),
        ("merchant", "is", "blue", "x", "Blue Bottle", False),
        ("any", "is", "x", "X", None, True),
    ],
)
def test_matcher_unit(field, op, text, name, merchant, expected):
    m = Matcher.build(id=1, field=field, op=op, text=text, amount_op=None, amount_cents=None,
                      action="category", category="OTHER")
    assert m.matches(name, merchant, -100) is expected


def test_sync_reapplies_rules_but_keeps_user_overrides(unlocked, fake_plaid):
    from tests.test_plaid import BANK_TOKEN, acct, txn

    fake_plaid.add_item("public-bank", BANK_TOKEN, "item_bank", "First Bank")
    fake_plaid.accounts[BANK_TOKEN] = [acct("chk", "Checking", "depository", "checking", 100)]
    fake_plaid.txn_pages[BANK_TOKEN] = [
        {"cursor_in": None, "added": [txn("a", "chk", 10, "Uber trip", category="TRANSPORTATION"),
                                      txn("b", "chk", 20, "Uber eats", category="FOOD_AND_DRINK")],
         "modified": [], "removed": [], "next_cursor": "c1", "has_more": False},
        {"cursor_in": "c1", "added": [],
         "modified": [txn("a", "chk", 11, "Uber trip", category="TRAVEL"),
                      txn("b", "chk", 21, "Uber eats", category="GENERAL_MERCHANDISE")],
         "removed": [], "next_cursor": "c2", "has_more": False},
    ]
    unlocked.post("/api/rules", json={"field": "name", "op": "contains", "text": "uber", "action": "category",
                                      "category": "TRANSPORTATION"})
    item = unlocked.post("/api/plaid/exchange", json={"public_token": "public-bank", "kind": "bank"}).json()["item"]
    unlocked.post(f"/api/plaid/items/{item['id']}/import", json={"plaid_account_ids": ["chk"]})
    by_name = {t["name"]: t for t in unlocked.get("/api/transactions").json()["items"]}
    assert by_name["Uber eats"]["category"] == "TRANSPORTATION" and by_name["Uber eats"]["category_source"] == "rule"
    unlocked.patch(f"/api/transactions/{by_name['Uber eats']['id']}", json={"category": "FOOD_AND_DRINK"})

    unlocked.post("/api/plaid/sync", json={})
    by_name = {t["name"]: t for t in unlocked.get("/api/transactions").json()["items"]}
    assert by_name["Uber trip"]["plaid_category"] == "TRAVEL"
    assert by_name["Uber trip"]["category"] == "TRANSPORTATION"  # rule still wins over Plaid
    assert by_name["Uber eats"]["plaid_category"] == "GENERAL_MERCHANDISE"
    assert by_name["Uber eats"]["category"] == "FOOD_AND_DRINK"  # user override survives sync
    assert by_name["Uber eats"]["category_source"] == "user"


# ------------------------------------------------------------------ CSV export


def test_csv_export_with_injection_guard(setup, db):
    client, acct, ids = setup
    add_txn(db, acct.id, "2026-09-06", -20, "=HYPERLINK(\"http://evil\")", merchant="+cmd", category="OTHER")
    add_txn(db, acct.id, "2026-09-07", -1, "@SUM(A1)", merchant="\tTabbed", category="OTHER")
    add_txn(db, acct.id, "2026-09-08", -1, "\rCR", merchant="-2+3", category="OTHER")
    client.patch(f"/api/transactions/{ids['coffee']}", json={"notes": "=1+1", "is_transfer": True})

    r = client.get("/api/transactions/export.csv")
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/csv; charset=utf-8"
    assert r.headers["content-disposition"] == 'attachment; filename="iron-owl-transactions-2026-09-26.csv"'
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8"))))
    assert rows[0] == ["date", "account", "name", "merchant", "category", "amount", "pending", "transfer", "notes",
                       "tags"]
    body = {row[0]: row for row in rows[1:]}
    assert len(rows) == 1 + 8
    assert body["2026-09-06"][2] == "'=HYPERLINK(\"http://evil\")" and body["2026-09-06"][3] == "'+cmd"
    assert body["2026-09-07"][2] == "'@SUM(A1)" and body["2026-09-07"][3] == "'\tTabbed"
    assert body["2026-09-08"][2] == "'\rCR" and body["2026-09-08"][3] == "'-2+3"
    coffee = body["2026-09-01"]
    assert coffee == ["2026-09-01", "Everyday", "SQ *BLUE BOTTLE 1234", "Blue Bottle", "Food and drink",
                      "-4.50", "false", "true", "'=1+1", ""]
    assert body["2026-09-04"][5] == "2500.00"  # numbers are plain, never prefixed
    assert body["2026-09-05"][4] == "Other"

    # Same filters as the list, and no limit.
    r = client.get("/api/transactions/export.csv", params={"category": "INCOME"})
    assert len(list(csv.reader(io.StringIO(r.text)))) == 2
    r = client.get("/api/transactions/export.csv", params={"search": "whole", "limit": 1})
    assert len(list(csv.reader(io.StringIO(r.text)))) == 2


def test_csv_export_requires_session(client):
    assert client.get("/api/transactions/export.csv").status_code == 401

"""Release 3: split transactions (validation, split-aware totals, rescaling), bulk recategorize, tags."""
from __future__ import annotations

import csv
import io
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.models import TransactionSplit, TransactionTag
from app.services.txns import rescale
from tests.conftest import add_txn, make_account
from tests.test_plaid import BANK_TOKEN, acct, txn


def ok(response) -> dict:
    assert response.status_code == 200, response.text
    return response.json()


def put_splits(client, txn_id, parts):
    return client.put(f"/api/transactions/{txn_id}/splits", json={"splits": parts})


def envelope(client, month="2026-09") -> dict[str, dict]:
    return {c["category"]: c for c in ok(client.get("/api/budgets", params={"month": month}))["categories"]}


@pytest.fixture
def s(unlocked, db, fixed_today):
    """Checking (a budget account) with a $100 Costco run and a paycheck."""
    chk = make_account(db, "Checking", "bank", 5000)
    costco = add_txn(db, chk.id, "2026-09-10", -100, "COSTCO #12", merchant="Costco", category="GENERAL_MERCHANDISE")
    pay = add_txn(db, chk.id, "2026-09-01", 2500, "Payroll", category="INCOME")
    return SimpleNamespace(client=unlocked, db=db, chk=chk, costco=costco, pay=pay)


# ------------------------------------------------------------------ splits


def test_split_and_unsplit(s):
    c = s.client
    body = ok(put_splits(c, s.costco.id, [
        {"amount": -60, "category": "FOOD_AND_DRINK"},
        {"amount": -40, "category": "GENERAL_MERCHANDISE", "notes": "  towels "},
    ]))
    assert body["amount"] == -100.0 and body["category"] == "GENERAL_MERCHANDISE"
    assert [(p["amount"], p["category"], p["category_name"], p["category_hue"], p["notes"]) for p in body["splits"]] == [
        (-60.0, "FOOD_AND_DRINK", "Food and drink", 25, None),
        (-40.0, "GENERAL_MERCHANDISE", "Shopping", 300, "towels"),
    ]
    assert all(isinstance(p["id"], int) for p in body["splits"])
    listed = {t["id"]: t for t in ok(c.get("/api/transactions"))["items"]}
    assert listed[s.costco.id]["splits"] == body["splits"] and listed[s.pay.id]["splits"] == []
    assert listed[s.pay.id]["tags"] == []

    # Replacing keeps exactly the new parts; an empty list removes them.
    body = ok(put_splits(c, s.costco.id, [{"amount": -0.01, "category": "OTHER"}, {"amount": -99.99, "category": "TRAVEL"}]))
    assert [p["amount"] for p in body["splits"]] == [-0.01, -99.99]
    assert s.db.scalar(select(func.count()).select_from(TransactionSplit)) == 2
    assert ok(put_splits(c, s.costco.id, []))["splits"] == []
    assert s.db.scalar(select(func.count()).select_from(TransactionSplit)) == 0


@pytest.mark.parametrize(("parts", "detail"), [
    ([{"amount": -100, "category": "FOOD_AND_DRINK"}], "Split a transaction into 2 to 20 parts."),
    ([{"amount": -100, "category": "FOOD_AND_DRINK"}, {"amount": 0, "category": "OTHER"}],
     "Each part needs an amount other than $0.00."),
    ([{"amount": -110, "category": "FOOD_AND_DRINK"}, {"amount": 10, "category": "OTHER"}],
     "Every part must be money out, like the transaction itself."),
    ([{"amount": -60, "category": "FOOD_AND_DRINK"}, {"amount": -39.99, "category": "OTHER"}],
     "The parts add up to -$99.99, not -$100. Add $0.01."),
    ([{"amount": -60, "category": "FOOD_AND_DRINK"}, {"amount": -40.01, "category": "OTHER"}],
     "The parts add up to -$100.01, not -$100. Remove $0.01."),
    ([{"amount": -60, "category": "FOOD_AND_DRINK"}, {"amount": -40, "category": "NOPE"}], "Unknown category 'NOPE'."),
])
def test_split_validation(s, parts, detail):
    r = put_splits(s.client, s.costco.id, parts)
    assert r.status_code == 422 and r.json()["detail"] == detail
    assert s.db.scalar(select(func.count()).select_from(TransactionSplit)) == 0


def test_split_bounds_and_body_rules(s):
    c = s.client
    twenty = [{"amount": -5, "category": "OTHER"}] * 20
    assert len(ok(put_splits(c, s.costco.id, twenty))["splits"]) == 20
    assert put_splits(c, s.costco.id, [{"amount": -5, "category": "OTHER"}] * 21).status_code == 422
    parts = [{"amount": -50, "category": "OTHER"}, {"amount": -50, "category": "OTHER", "x": 1}]
    assert put_splits(c, s.costco.id, parts).status_code == 422
    assert put_splits(c, s.costco.id, [{"amount": -50, "category": "OTHER", "notes": "n" * 501},
                                       {"amount": -50, "category": "OTHER"}]).status_code == 422
    raw = '{"splits": [{"amount": NaN, "category": "OTHER"}, {"amount": -100, "category": "OTHER"}]}'
    r = c.put(f"/api/transactions/{s.costco.id}/splits", content=raw, headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    assert put_splits(c, 999999, []).status_code == 404
    # Income splits must be positive; a $0 transaction can't be split.
    assert ok(put_splits(c, s.pay.id, [{"amount": 2000, "category": "INCOME"}, {"amount": 500, "category": "OTHER"}]))
    zero = add_txn(s.db, s.chk.id, "2026-09-11", 0, "Zero")
    r = put_splits(c, zero.id, [{"amount": 1, "category": "OTHER"}, {"amount": -1, "category": "OTHER"}])
    assert r.status_code == 422 and r.json()["detail"] == "A $0.00 transaction can't be split."


def test_envelopes_review_and_budget_alert_are_split_aware(s):
    c = s.client
    ok(c.put("/api/budgets/2026-09", json={"assigned": {"FOOD_AND_DRINK": 200, "GENERAL_MERCHANDISE": 100}}))
    lines = envelope(c)
    assert (lines["FOOD_AND_DRINK"]["spent"], lines["GENERAL_MERCHANDISE"]["spent"]) == (0.0, 100.0)
    ok(put_splits(c, s.costco.id, [{"amount": -60, "category": "FOOD_AND_DRINK"},
                                   {"amount": -25, "category": "GENERAL_MERCHANDISE"},
                                   {"amount": -15, "category": "LOAN_PAYMENTS"}]))
    lines = envelope(c)
    assert (lines["FOOD_AND_DRINK"]["spent"], lines["FOOD_AND_DRINK"]["available"]) == (60.0, 140.0)
    assert (lines["GENERAL_MERCHANDISE"]["spent"], lines["GENERAL_MERCHANDISE"]["available"]) == (25.0, 75.0)
    b = ok(c.get("/api/budgets", params={"month": "2026-09"}))
    assert b["fixed"] == [{"category": "LOAN_PAYMENTS", "name": "Loan payments", "hue": 55, "spent": 15.0}]
    # Ready to assign: 5000 - (140 + 75).
    assert b["ready_to_assign"] == 4785.0

    review = ok(c.get("/api/spending/review", params={"month": "2026-09"}))
    assert (review["income"], review["fixed"], review["budgeted_spending"], review["other_spending"]) == (
        2500.0, 15.0, 85.0, 0.0)
    assert review["left_over"] == 2400.0
    # One visit, even though two of its parts are spending.
    assert review["most_visited"] == {"merchant": "Costco", "count": 1, "spent": 85.0}
    assert review["biggest"]["category"] == "FOOD_AND_DRINK"

    # The budget alert sees the parts: shopping is at 25% of 100, food at 30% of 200 -> no alert;
    # re-split so food is 210 of 200 -> over the plan (Settings D7: only over alerts).
    ok(put_splits(c, s.costco.id, [{"amount": -95, "category": "FOOD_AND_DRINK"},
                                   {"amount": -5, "category": "GENERAL_MERCHANDISE"}]))
    add_txn(s.db, s.chk.id, "2026-09-12", -115, "Grocer", category="FOOD_AND_DRINK")
    from app.services.alerts import evaluate
    from tests.conftest import FIXED_TODAY
    created = evaluate(s.db, FIXED_TODAY)
    s.db.commit()
    assert [e.dedupe_key for e in created if e.key == "budget"] == ["budget:2026-09:FOOD_AND_DRINK"]


def test_transfer_split_parent_stays_out_of_totals(s):
    c = s.client
    ok(c.patch(f"/api/transactions/{s.costco.id}", json={"is_transfer": True}))
    ok(put_splits(c, s.costco.id, [{"amount": -60, "category": "FOOD_AND_DRINK"}, {"amount": -40, "category": "OTHER"}]))
    ok(c.put("/api/budgets/2026-09", json={"assigned": {"FOOD_AND_DRINK": 10}}))
    assert envelope(c)["FOOD_AND_DRINK"]["spent"] == 0.0


def test_rules_never_change_split_transactions(s):
    c = s.client
    other = add_txn(s.db, s.chk.id, "2026-09-12", -20, "COSTCO #12", merchant="Costco", category="GENERAL_MERCHANDISE")
    ok(put_splits(c, s.costco.id, [{"amount": -60, "category": "FOOD_AND_DRINK"}, {"amount": -40, "category": "OTHER"}]))
    rule = {"field": "merchant", "op": "is", "text": "costco", "action": "category", "category": "TRAVEL"}
    preview = ok(c.post("/api/rules/preview", json=rule))
    assert (preview["matches"], preview["would_change"]) == (1, 1)
    assert [row["id"] for row in preview["sample"]] == [other.id]  # never the split one
    assert c.post("/api/rules", json=rule).status_code == 201
    txns = {t["id"]: t for t in ok(c.get("/api/transactions"))["items"]}
    assert txns[other.id]["category"] == "TRAVEL" and txns[other.id]["category_source"] == "rule"
    assert txns[s.costco.id]["category"] == "GENERAL_MERCHANDISE" and txns[s.costco.id]["rule_id"] is None
    assert [p["category"] for p in txns[s.costco.id]["splits"]] == ["FOOD_AND_DRINK", "OTHER"]


def test_category_filter_is_split_aware(s):
    c = s.client
    ok(put_splits(c, s.costco.id, [{"amount": -60, "category": "FOOD_AND_DRINK"}, {"amount": -40, "category": "OTHER"}]))
    ids = lambda **q: [t["id"] for t in ok(c.get("/api/transactions", params=q))["items"]]  # noqa: E731
    assert ids(category="FOOD_AND_DRINK") == [s.costco.id]
    assert ids(category="OTHER") == [s.costco.id]
    assert ids(category="GENERAL_MERCHANDISE") == []  # the parent's own category no longer counts


def test_deleting_a_custom_category_moves_its_parts_to_other(s):
    c = s.client
    kids = c.post("/api/categories", json={"name": "Kids", "kind": "spending"}).json()
    ok(put_splits(c, s.costco.id, [{"amount": -60, "category": kids["id"]}, {"amount": -40, "category": "OTHER"}]))
    assert c.delete(f"/api/categories/{kids['id']}").status_code == 204
    parts = ok(c.get("/api/transactions"))["items"][0]["splits"]
    assert [p["category"] for p in parts] == ["OTHER", "OTHER"]


# ------------------------------------------------------------------ rescaling


def test_rescale_math():
    assert rescale([-6000, -4000], -10000, -12000) == [-7200, -4800]
    # 1.0001 x -3333.x rounds to -3333, -3333, -3334; the -1 cent remainder goes to the largest.
    assert rescale([-3333, -3333, -3334], -10000, -10001) == [-3333, -3333, -3335]
    # Half a cent rounds away from zero; the largest part absorbs the difference.
    assert rescale([-5, -5], -10, -11) == [-5, -6]
    assert rescale([700, 300], 1000, 1001) == [701, 300]
    # A new sign flips every part consistently.
    assert rescale([-6000, -4000], -10000, 5000) == [3000, 2000]
    # Impossible to keep a valid split.
    assert rescale([-1, -99], -100, -10) is None  # a part would round to 0
    assert rescale([-6000, -4000], -10000, 0) is None
    for parts, old, new in (([-6000, -4000], -10000, -10003), ([-1234, -5678, -91], -7003, -12345)):
        out = rescale(parts, old, new)
        assert sum(out) == new and all(p < 0 for p in out)


def test_sync_rescales_parts_and_removal_cascades(unlocked, fake_plaid, fixed_today):
    c = unlocked
    fake_plaid.add_item("public-bank", BANK_TOKEN, "item_bank", "First Bank")
    fake_plaid.accounts[BANK_TOKEN] = [acct("chk", "Checking", "depository", "checking", 1000)]
    fake_plaid.txn_pages[BANK_TOKEN] = [
        {"cursor_in": None, "added": [txn("t1", "chk", 100, "Costco", date="2026-09-10")],
         "modified": [], "removed": [], "next_cursor": "c1", "has_more": False},
        {"cursor_in": "c1", "added": [], "modified": [txn("t1", "chk", 120, "Costco", date="2026-09-10")],
         "removed": [], "next_cursor": "c2", "has_more": False},
        {"cursor_in": "c2", "added": [], "modified": [], "removed": [{"transaction_id": "t1"}],
         "next_cursor": "c3", "has_more": False},
    ]
    item = c.post("/api/plaid/exchange", json={"public_token": "public-bank", "kind": "bank"}).json()["item"]
    ok(c.post(f"/api/plaid/items/{item['id']}/import", json={"plaid_account_ids": ["chk"]}))
    t1 = ok(c.get("/api/transactions"))["items"][0]
    assert t1["amount"] == -100.0
    ok(put_splits(c, t1["id"], [{"amount": -60, "category": "FOOD_AND_DRINK"}, {"amount": -40, "category": "OTHER"}]))
    # A rule that would match is ignored for the split transaction during sync too.
    c.post("/api/rules", json={"field": "name", "op": "is", "text": "costco", "action": "category", "category": "TRAVEL"})

    ok(c.post("/api/plaid/sync", json={}))
    t1 = ok(c.get("/api/transactions"))["items"][0]
    assert t1["amount"] == -120.0 and t1["category"] == "FOOD_AND_DRINK"  # Plaid's primary, not the rule
    assert [p["amount"] for p in t1["splits"]] == [-72.0, -48.0]

    ok(c.post("/api/plaid/sync", json={}))
    assert ok(c.get("/api/transactions"))["items"] == []
    vault = c.app.state.fintrack.vault
    session = vault.db.acquire()
    try:
        assert session.scalar(select(func.count()).select_from(TransactionSplit)) == 0
    finally:
        vault.db.release(session)


# ------------------------------------------------------------------ bulk recategorize


@pytest.fixture
def merchants(s):
    db = s.db
    a = add_txn(db, s.chk.id, "2026-09-02", -5, "SQ *BLUE BOTTLE", merchant="Blue Bottle", category="FOOD_AND_DRINK")
    b = add_txn(db, s.chk.id, "2026-09-03", -6, "SQ *BLUE BOTTLE 2", merchant=" blue bottle ", category="GENERAL_MERCHANDISE")
    manual = add_txn(db, s.chk.id, "2026-09-04", -7, "Blue Bottle kiosk", merchant="BLUE BOTTLE", category="TRAVEL",
                     category_source="user")
    split = add_txn(db, s.chk.id, "2026-09-05", -8, "Blue Bottle beans", merchant="Blue Bottle", category="OTHER")
    ok(put_splits(s.client, split.id, [{"amount": -4, "category": "OTHER"}, {"amount": -4, "category": "TRAVEL"}]))
    near = add_txn(db, s.chk.id, "2026-09-06", -9, "Blue Bottle", merchant="Blue Bottle Coffee", category="OTHER")
    return SimpleNamespace(**vars(s), a=a, b=b, manual=manual, split=split, near=near)


def test_recategorize_preview_and_apply(merchants):
    m = merchants
    c = m.client
    body = {"field": "merchant", "text": "Blue Bottle", "category": "FOOD_AND_DRINK"}
    # b (other category) and manual (hand-set) would change; a is already there; split and
    # "Blue Bottle Coffee" (not an exact match) are skipped.
    assert ok(c.post("/api/transactions/recategorize/preview", json=body)) == {"matches": 2, "already": 1, "manual": 1}
    assert ok(c.post("/api/transactions/recategorize", json={**body, "include_manual": False})) == {"changed": 1}
    txns = {t["id"]: t for t in ok(c.get("/api/transactions"))["items"]}
    assert (txns[m.b.id]["category"], txns[m.b.id]["category_source"]) == ("FOOD_AND_DRINK", "user")
    assert txns[m.manual.id]["category"] == "TRAVEL"
    assert txns[m.near.id]["category"] == "OTHER" and txns[m.split.id]["category"] == "OTHER"
    assert ok(c.post("/api/transactions/recategorize/preview", json=body)) == {"matches": 1, "already": 2, "manual": 1}
    assert ok(c.post("/api/transactions/recategorize", json={**body, "include_manual": True})) == {"changed": 1}
    assert ok(c.post("/api/transactions/recategorize/preview", json=body)) == {"matches": 0, "already": 3, "manual": 0}
    # No rule was created, and a later rules run keeps the hand-set categories.
    assert ok(c.get("/api/rules")) == []
    c.post("/api/rules", json={"field": "any", "op": "contains", "text": "blue", "action": "category",
                               "category": "TRAVEL"})
    txns = {t["id"]: t for t in ok(c.get("/api/transactions"))["items"]}
    assert txns[m.b.id]["category"] == "FOOD_AND_DRINK"

    # By name: exact, trimmed, case-insensitive.
    body = {"field": "name", "text": "  sq *blue bottle ", "category": "MEDICAL"}
    # (The "blue" rule re-categorized a, so it isn't a hand-set match.)
    assert ok(c.post("/api/transactions/recategorize/preview", json=body)) == {"matches": 1, "already": 0, "manual": 0}


def test_recategorize_validation(merchants):
    c = merchants.client
    base = {"field": "merchant", "text": "Blue Bottle", "category": "FOOD_AND_DRINK"}
    assert c.post("/api/transactions/recategorize/preview", json={**base, "category": "NOPE"}).status_code == 422
    assert c.post("/api/transactions/recategorize/preview", json={**base, "field": "any"}).status_code == 422
    assert c.post("/api/transactions/recategorize/preview", json={**base, "text": "   "}).status_code == 422
    assert c.post("/api/transactions/recategorize/preview", json={**base, "text": "x" * 201}).status_code == 422
    assert c.post("/api/transactions/recategorize/preview", json={**base, "include_manual": True}).status_code == 422
    assert c.post("/api/transactions/recategorize", json=base).json() == {"changed": 1}  # include_manual defaults off


# ------------------------------------------------------------------ tags


def test_tag_crud(s):
    c = s.client
    r = c.post("/api/tags", json={"name": "  Summer   trip ", "hue": 30})
    assert r.status_code == 201
    trip = r.json()
    assert trip == {"id": trip["id"], "name": "Summer trip", "hue": 30, "count": 0, "spent": 0.0}
    home = c.post("/api/tags", json={"name": "Home"}).json()
    assert 0 <= home["hue"] <= 359
    assert c.post("/api/tags", json={"name": "home"}).status_code == 409
    assert c.post("/api/tags", json={"name": ""}).status_code == 422
    assert c.post("/api/tags", json={"name": "x" * 41}).status_code == 422
    assert c.post("/api/tags", json={"name": "A", "hue": 360}).status_code == 422
    assert [t["name"] for t in ok(c.get("/api/tags"))] == ["Home", "Summer trip"]
    assert ok(c.patch(f"/api/tags/{trip['id']}", json={"name": "Trip"}))["name"] == "Trip"
    assert c.patch(f"/api/tags/{trip['id']}", json={"name": "HOME"}).status_code == 409
    assert c.patch(f"/api/tags/{trip['id']}", json={"hue": None}).status_code == 422
    assert ok(c.patch(f"/api/tags/{trip['id']}", json={"hue": 200}))["hue"] == 200
    assert c.patch("/api/tags/999", json={"hue": 1}).status_code == 404

    ok(c.put(f"/api/transactions/{s.costco.id}/tags", json={"tag_ids": [trip["id"], home["id"], trip["id"]]}))
    assert c.delete(f"/api/tags/{trip['id']}").status_code == 204
    assert c.delete(f"/api/tags/{trip['id']}").status_code == 404
    t = ok(c.get("/api/transactions"))["items"]
    assert [x["tags"] for x in t if x["id"] == s.costco.id][0] == [{"id": home["id"], "name": "Home", "hue": home["hue"]}]
    assert s.db.scalar(select(func.count()).select_from(TransactionTag)) == 1


def test_transaction_tags_filter_count_and_spent(s):
    c, db = s.client, s.db
    trip = c.post("/api/tags", json={"name": "Trip"}).json()
    work = c.post("/api/tags", json={"name": "Work"}).json()
    ok(put_splits(c, s.costco.id, [{"amount": -60, "category": "FOOD_AND_DRINK"},
                                   {"amount": -40, "category": "LOAN_PAYMENTS"}]))
    body = ok(c.put(f"/api/transactions/{s.costco.id}/tags", json={"tag_ids": [work["id"], trip["id"]]}))
    assert [t["name"] for t in body["tags"]] == ["Trip", "Work"]
    refund = add_txn(db, s.chk.id, "2026-09-12", 15, "Refund", category="FOOD_AND_DRINK")
    xfer = add_txn(db, s.chk.id, "2026-09-13", -500, "To savings", category="FOOD_AND_DRINK", is_transfer=True)
    for t in (refund, xfer, s.pay):
        ok(c.put(f"/api/transactions/{t.id}/tags", json={"tag_ids": [trip["id"]]}))

    tags = {t["name"]: t for t in ok(c.get("/api/tags"))}
    # Trip: food part 60 - refund 15 (the fixed part, the transfer and the income don't count).
    assert (tags["Trip"]["count"], tags["Trip"]["spent"]) == (4, 45.0)
    assert (tags["Work"]["count"], tags["Work"]["spent"]) == (1, 60.0)

    ids = lambda **q: sorted(t["id"] for t in ok(c.get("/api/transactions", params=q))["items"])  # noqa: E731
    assert ids(tag=work["id"]) == [s.costco.id]
    assert ids(tag=trip["id"]) == sorted([s.costco.id, refund.id, xfer.id, s.pay.id])
    assert ok(c.get("/api/transactions", params={"tag": trip["id"]}))["total"] == 4
    assert c.get("/api/transactions", params={"tag": 0}).status_code == 422

    # Validation: unknown tag, too many, extra fields; clearing.
    assert c.put(f"/api/transactions/{s.costco.id}/tags", json={"tag_ids": [999]}).status_code == 422
    assert c.put(f"/api/transactions/{s.costco.id}/tags", json={"tag_ids": list(range(1, 22))}).status_code == 422
    assert c.put(f"/api/transactions/{s.costco.id}/tags", json={"tag_ids": [], "x": 1}).status_code == 422
    assert c.put("/api/transactions/999999/tags", json={"tag_ids": []}).status_code == 404
    assert ok(c.put(f"/api/transactions/{s.costco.id}/tags", json={"tag_ids": []}))["tags"] == []


# ------------------------------------------------------------------ CSV


def test_csv_has_one_row_per_split_part_and_tags(s):
    c = s.client
    trip = c.post("/api/tags", json={"name": "Trip"}).json()
    home = c.post("/api/tags", json={"name": "=Home"}).json()
    ok(put_splits(c, s.costco.id, [{"amount": -60.5, "category": "FOOD_AND_DRINK"},
                                   {"amount": -39.5, "category": "GENERAL_MERCHANDISE", "notes": "towels"}]))
    ok(c.patch(f"/api/transactions/{s.costco.id}", json={"notes": "parent note"}))
    ok(c.put(f"/api/transactions/{s.costco.id}/tags", json={"tag_ids": [trip["id"], home["id"]]}))
    r = c.get("/api/transactions/export.csv")
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8"))))
    assert rows[0][-1] == "tags"
    assert rows[1:] == [
        ["2026-09-10", "Checking", "COSTCO #12", "Costco", "Food and drink", "-60.50", "false", "false",
         "split 1/2", "'=Home; Trip"],
        ["2026-09-10", "Checking", "COSTCO #12", "Costco", "Shopping", "-39.50", "false", "false",
         "split 2/2: towels", "'=Home; Trip"],
        ["2026-09-01", "Checking", "Payroll", "", "Income", "2500.00", "false", "false", "", ""],
    ]
    only_trip = list(csv.reader(io.StringIO(c.get("/api/transactions/export.csv", params={"tag": trip["id"]}).text)))
    assert len(only_trip) == 3

"""Follow-ups from the Release 3 review: pending→posted keeps the user's edits; "always" rules go first."""
from __future__ import annotations

from sqlalchemy import func, select

from app.models import AlertEvent, Transaction, TransactionSplit, TransactionTag
from tests.conftest import add_txn, make_account
from tests.test_plaid import BANK_TOKEN, acct, txn


def ok(response) -> dict:
    assert response.status_code in (200, 201), response.text
    return response.json()


def posted(tid, pending_tid, amount, name, date="2026-09-12"):
    t = txn(tid, "chk", amount, name, date=date, category="FOOD_AND_DRINK")
    t["pending_transaction_id"] = pending_tid
    return t


def link(c, fake_plaid, pages):
    fake_plaid.add_item("public-bank", BANK_TOKEN, "item_bank", "First Bank")
    fake_plaid.accounts[BANK_TOKEN] = [acct("chk", "Checking", "depository", "checking", 1000)]
    fake_plaid.txn_pages[BANK_TOKEN] = pages
    item = ok(c.post("/api/plaid/exchange", json={"public_token": "public-bank", "kind": "bank"}))["item"]
    ok(c.post(f"/api/plaid/items/{item['id']}/import", json={"plaid_account_ids": ["chk"]}))
    return item


def by_name(c) -> dict[str, dict]:
    return {t["name"]: t for t in ok(c.get("/api/transactions"))["items"]}


# ------------------------------------------------------------------ pending → posted


def test_posting_keeps_category_notes_transfer_splits_and_tags(unlocked, fake_plaid, fixed_today, db):
    c = unlocked
    item = link(c, fake_plaid, [
        {"cursor_in": None, "added": [
            txn("p1", "chk", 40, "Olive Garden", date="2026-09-10", pending=True),
            txn("p2", "chk", 25, "Card payment", date="2026-09-10", pending=True),
            txn("p3", "chk", 9, "Untouched", date="2026-09-10", pending=True),
        ], "modified": [], "removed": [], "next_cursor": "c1", "has_more": False},
        {"cursor_in": "c1", "added": [
            posted("t1", "p1", 48, "Olive Garden"),  # the tip was added when it posted
            posted("t2", "p2", 25, "Card payment"),
            posted("t3", "p3", 9, "Untouched"),
        ], "modified": [], "removed": [{"transaction_id": "p1"}, {"transaction_id": "p2"}, {"transaction_id": "p3"}],
         "next_cursor": "c2", "has_more": False},
    ])
    rows = by_name(c)
    og, pay = rows["Olive Garden"], rows["Card payment"]
    assert og["pending"] is True
    ok(c.patch(f"/api/transactions/{og['id']}", json={"category": "TRAVEL", "notes": "Sam's birthday"}))
    ok(c.put(f"/api/transactions/{og['id']}/splits", json={"splits": [
        {"amount": -30, "category": "FOOD_AND_DRINK", "notes": "dinner"}, {"amount": -10, "category": "OTHER"},
    ]}))
    tag = ok(c.post("/api/tags", json={"name": "Family"}))
    ok(c.put(f"/api/transactions/{og['id']}/tags", json={"tag_ids": [tag["id"]]}))
    ok(c.patch(f"/api/transactions/{pay['id']}", json={"is_transfer": True}))

    result = ok(c.post("/api/plaid/sync", json={"item_id": item["id"]}))["results"][0]
    assert result["ok"] and result["transactions_added"] == 3

    rows = by_name(c)
    assert len(rows) == 3  # the pending copies are gone, no duplicates
    og = rows["Olive Garden"]
    assert og["pending"] is False and og["amount"] == -48.0
    assert og["category"] == "TRAVEL" and og["category_source"] == "user"
    assert og["notes"] == "Sam's birthday"
    assert [t["name"] for t in og["tags"]] == ["Family"]
    # Parts rescaled to the posted total, notes kept.
    assert [(p["amount"], p["category"], p["notes"]) for p in og["splits"]] == [
        (-36.0, "FOOD_AND_DRINK", "dinner"), (-12.0, "OTHER", None),
    ]
    assert rows["Card payment"]["is_transfer"] is True
    untouched = rows["Untouched"]
    assert untouched["category_source"] != "user" and untouched["notes"] is None and untouched["tags"] == []
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(TransactionSplit)) == 2
    assert db.scalar(select(func.count()).select_from(TransactionTag)) == 1
    assert set(db.scalars(select(Transaction.plaid_transaction_id))) == {"t1", "t2", "t3"}


def test_posting_before_plaid_removes_the_pending_copy(unlocked, fake_plaid, fixed_today):
    """Plaid's removal of the pending row arrives a sync later: no duplicate meanwhile."""
    c = unlocked
    item = link(c, fake_plaid, [
        {"cursor_in": None, "added": [txn("p1", "chk", 40, "Olive Garden", date="2026-09-10", pending=True)],
         "modified": [], "removed": [], "next_cursor": "c1", "has_more": False},
        {"cursor_in": "c1", "added": [posted("t1", "p1", 40, "Olive Garden")],
         "modified": [], "removed": [], "next_cursor": "c2", "has_more": False},
        {"cursor_in": "c2", "added": [], "modified": [], "removed": [{"transaction_id": "p1"}],
         "next_cursor": "c3", "has_more": False},
    ])
    og = by_name(c)["Olive Garden"]
    ok(c.patch(f"/api/transactions/{og['id']}", json={"category": "TRAVEL"}))
    ok(c.post("/api/plaid/sync", json={"item_id": item["id"]}))
    items = ok(c.get("/api/transactions"))["items"]
    assert [(t["pending"], t["category"]) for t in items] == [(False, "TRAVEL")]
    ok(c.post("/api/plaid/sync", json={"item_id": item["id"]}))
    assert [(t["pending"], t["category"]) for t in ok(c.get("/api/transactions"))["items"]] == [(False, "TRAVEL")]


def test_posted_copy_never_takes_over_an_unrelated_row(unlocked, fake_plaid, fixed_today, db):
    """Only a still-pending row on the same account is replaced."""
    c = unlocked
    item = link(c, fake_plaid, [
        {"cursor_in": None, "added": [txn("x1", "chk", 40, "Already posted", date="2026-09-10")],
         "modified": [], "removed": [], "next_cursor": "c1", "has_more": False},
        {"cursor_in": "c1", "added": [posted("t1", "x1", 40, "Posted"), posted("t2", "nope", 5, "Orphan")],
         "modified": [], "removed": [], "next_cursor": "c2", "has_more": False},
    ])
    ok(c.post("/api/plaid/sync", json={"item_id": item["id"]}))
    assert set(by_name(c)) == {"Already posted", "Posted", "Orphan"}


def test_posted_copy_does_not_repeat_the_large_purchase_alert(unlocked, fake_plaid, fixed_today, db):
    c = unlocked
    item = link(c, fake_plaid, [
        {"cursor_in": None, "added": [txn("p1", "chk", 900, "Best Buy", date="2026-09-25", pending=True)],
         "modified": [], "removed": [], "next_cursor": "c1", "has_more": False},
        {"cursor_in": "c1", "added": [posted("t1", "p1", 900, "Best Buy", date="2026-09-25")],
         "modified": [], "removed": [{"transaction_id": "p1"}], "next_cursor": "c2", "has_more": False},
    ])

    def big_alerts() -> int:
        db.expire_all()
        return db.scalar(select(func.count()).select_from(AlertEvent).where(AlertEvent.key == "big"))

    assert big_alerts() == 1
    ok(c.post("/api/plaid/sync", json={"item_id": item["id"]}))
    assert big_alerts() == 1


def test_large_purchase_need_follows_the_posted_copy(unlocked, fake_plaid, fixed_today, db):
    """Review: Home's "Was this you?" must not vanish when the pending purchase posts. The
    event moves to the posted row (same event, so "Not now" still holds) with its amount."""
    c = unlocked
    item = link(c, fake_plaid, [
        {"cursor_in": None, "added": [txn("p1", "chk", 900, "Best Buy", date="2026-09-25", pending=True)],
         "modified": [], "removed": [], "next_cursor": "c1", "has_more": False},
        {"cursor_in": "c1", "added": [posted("t1", "p1", 912.5, "Best Buy", date="2026-09-25")],
         "modified": [], "removed": [{"transaction_id": "p1"}], "next_cursor": "c2", "has_more": False},
    ])

    def big_needs() -> list[dict]:
        body = ok(c.get("/api/dashboard"))
        return [a for a in body["needs"] if a["kind"] == "big_purchase"]

    (before,) = big_needs()
    assert before["data"]["amount"] == 900.0
    ok(c.post("/api/plaid/sync", json={"item_id": item["id"]}))
    (after,) = big_needs()
    posted_row = by_name(c)["Best Buy"]
    assert after["key"] == before["key"] and after["fingerprint"] == before["fingerprint"]
    assert after["data"]["transaction_id"] == posted_row["id"] != before["data"]["transaction_id"]
    assert after["data"]["amount"] == 912.5
    db.expire_all()
    keys = db.scalars(select(AlertEvent.dedupe_key).where(AlertEvent.key == "big")).all()
    assert keys == [f"big:{posted_row['id']}"]


def test_moving_a_large_purchase_event_never_duplicates(unlocked, db, fixed_today):
    from app.services.sync import _move_big_event
    from app.utils import utcnow

    chk = make_account(db, "Checking", "bank", 100)
    old, new = add_txn(db, chk.id, "2026-09-25", -900, "Old"), add_txn(db, chk.id, "2026-09-25", -900, "New")
    _move_big_event(db, old.id, new.id)  # no event at all: nothing happens
    assert db.scalar(select(func.count()).select_from(AlertEvent)) == 0
    for tid in (old.id, new.id):
        db.add(AlertEvent(key="big", severity="warn", title="Large", body="", read=False, created_at=utcnow(),
                          dedupe_key=f"big:{tid}", cleared=False))
    db.commit()
    _move_big_event(db, old.id, new.id)  # the posted row has one already: left as is
    db.commit()
    assert sorted(db.scalars(select(AlertEvent.dedupe_key))) == sorted([f"big:{old.id}", f"big:{new.id}"])


def test_posting_and_a_short_lived_hold_in_one_sync(unlocked, fake_plaid, fixed_today):
    """A carry-over plus a row added and removed in the same sync must not crash the sync."""
    c = unlocked
    item = link(c, fake_plaid, [
        {"cursor_in": None, "added": [txn("p1", "chk", 40, "Olive Garden", date="2026-09-10", pending=True)],
         "modified": [], "removed": [], "next_cursor": "c1", "has_more": False},
        {"cursor_in": "c1", "added": [posted("t1", "p1", 40, "Olive Garden"),
                                      txn("p9", "chk", 1, "Gas hold", date="2026-09-12", pending=True)],
         "modified": [], "removed": [{"transaction_id": "p1"}, {"transaction_id": "p9"}],
         "next_cursor": "c2", "has_more": False},
    ])
    og = by_name(c)["Olive Garden"]
    ok(c.patch(f"/api/transactions/{og['id']}", json={"category": "TRAVEL"}))
    result = ok(c.post("/api/plaid/sync", json={"item_id": item["id"]}))["results"][0]
    assert result["ok"] is True, result
    assert [(t["name"], t["pending"], t["category"]) for t in ok(c.get("/api/transactions"))["items"]] == [
        ("Olive Garden", False, "TRAVEL"),
    ]


def test_pending_and_posted_in_the_first_import_still_alert(unlocked, fake_plaid, fixed_today, db):
    """Both copies arrive together (first link): the posted one is the new transaction to alert on."""
    c = unlocked
    link(c, fake_plaid, [
        {"cursor_in": None, "added": [txn("p1", "chk", 900, "Best Buy", date="2026-09-24", pending=True),
                                      posted("t1", "p1", 900, "Best Buy", date="2026-09-25")],
         "modified": [], "removed": [{"transaction_id": "p1"}], "next_cursor": "c1", "has_more": False},
    ])
    assert [(t["name"], t["pending"]) for t in ok(c.get("/api/transactions"))["items"]] == [("Best Buy", False)]
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(AlertEvent).where(AlertEvent.key == "big")) == 1


# ------------------------------------------------------------------ rule order


def test_first_rule_goes_ahead_of_older_matching_rules(unlocked, db, fixed_today):
    c = unlocked
    chk = make_account(db, "Checking", "bank", 100)
    t = add_txn(db, chk.id, "2026-09-10", -80, "COSTCO WHSE #12", merchant="Costco", category="GENERAL_MERCHANDISE")
    db.commit()
    older = ok(c.post("/api/rules", json={"field": "any", "op": "contains", "text": "costco",
                                          "action": "category", "category": "TRAVEL"}))
    other = ok(c.post("/api/rules", json={"field": "any", "op": "contains", "text": "uber",
                                          "action": "category", "category": "TRAVEL"}))
    assert (older["position"], other["position"]) == (0, 1)

    new = ok(c.post("/api/rules", json={"field": "merchant", "op": "is", "text": "Costco", "action": "category",
                                        "category": "FOOD_AND_DRINK", "first": True}))
    assert [(r["id"], r["position"]) for r in ok(c.get("/api/rules"))] == [
        (new["id"], 0), (older["id"], 1), (other["id"], 2),
    ]
    got = next(x for x in ok(c.get("/api/transactions"))["items"] if x["id"] == t.id)
    assert (got["category"], got["category_source"], got["rule_id"]) == ("FOOD_AND_DRINK", "rule", new["id"])

    # Without "first" a rule still goes last.
    last = ok(c.post("/api/rules", json={"field": "any", "op": "contains", "text": "zzz",
                                         "action": "category", "category": "OTHER"}))
    assert last["position"] == 3

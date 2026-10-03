"""Release 3.10: the Budget's "Paying off debt" category (GET/PUT/DELETE /api/debt/budget,
POST /api/debt/budget/undo), payments filed under it, and that nothing counts them twice.
Today is 2026-09-26."""
from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlalchemy import select

from app.models import AlertEvent, AppSetting, Budget, CategoryGroup, Transaction, TxnCategory
from app.services import spending
from app.services.alerts import evaluate
from tests.conftest import add_budget, add_txn, make_account

T = "2026-09"
URL = "/api/debt/budget"
NOT_ENOUGH = "isn't planned yet this month. Use a smaller amount, or take money from another category on the Budget page."


def ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json() if response.content else None


def err(response, status=422) -> dict:
    assert response.status_code == status, response.text
    return response.json()


def budget(client, month=None) -> dict:
    return ok(client.get("/api/budgets", params={"month": month} if month else None))


def line(bm: dict, category: str) -> dict | None:
    return next((c for c in bm["categories"] if c["category"] == category), None)


def put(client, extra, strategy="avalanche", ids=None, loan=None):
    return client.put(URL, json={"extra": extra, "strategy": strategy, "account_ids": ids or [loan.id]})


def undo(client, token):
    return client.post(f"{URL}/undo", json={"token": token})


def txn(db, txn_id) -> Transaction:
    db.expire_all()
    return db.get(Transaction, txn_id)


@pytest.fixture
def base(unlocked, db, fixed_today):
    """Checking 4,000 + Savings 1,000 fund the Budget; September plans Food 500, so 4,500 isn't
    planned yet. A car loan (not a Budget account)."""
    chk = make_account(db, "Checking", "bank", 4000)
    make_account(db, "Savings", "bank", 1000)
    loan = make_account(db, "Car loan", "loan", 8000, minimum_payment_cents=30000, interest_rate=6.0)
    add_budget(db, T, {"FOOD_AND_DRINK": 500})
    return unlocked, db, chk, loan


def add(client, loan, extra=150) -> tuple[dict, str]:
    out = ok(put(client, extra, loan=loan))
    return out["state"], out["undo"]["token"]


# ------------------------------------------------------------------ state and add


def test_budget_set_up_first(unlocked, db, fixed_today):
    loan = make_account(db, "Car loan", "loan", 100, minimum_payment_cents=1000)
    st = ok(unlocked.get(URL))
    assert (st["budget_ready"], st["linked"], st["category_id"], st["extra"]) == (False, False, None, None)
    assert err(put(unlocked, 50, loan=loan), 409) == {"detail": "Set up your Budget first."}
    assert db.scalar(select(TxnCategory).where(TxnCategory.name == "Paying off debt")) is None


def test_add_makes_the_category_and_plans_this_month(base):
    client, db, _chk, loan = base
    st, token = add(client, loan, 150)
    c = st["category_id"]
    assert st == {"budget_ready": True, "linked": True, "category_id": c, "extra": 150.0, "strategy": "avalanche",
                  "account_ids": [loan.id],
                  "loans": [{"account_id": loan.id, "name": "Car loan"}], "month": T, "planned_this_month": 150.0, "not_planned": 4350.0}
    assert len(token) >= 8 and ok(client.get(URL)) == st
    db.expire_all()
    row = db.get(TxnCategory, c)
    assert (row.name, row.kind, row.hidden, row.custom) == ("Paying off debt", "spending", False, True)
    assert db.get(CategoryGroup, row.group_id).name == "Other and saving"
    bm = budget(client)
    assert line(bm, c)["debt"] == {"extra": 150.0} and line(bm, c)["goal"] is None
    assert line(bm, c)["assigned"] == 150.0 and line(bm, "FOOD_AND_DRINK")["debt"] is None
    # Release 3.17: next month plans the extra for it (once plans repeat).
    assert line(budget(client, "2026-10"), c)["assigned"] == 0.0
    spending.put_setting(db, spending.BUDGET_PLANS_REPEAT_SETTING, T)
    db.commit()
    assert line(budget(client, "2026-10"), c)["assigned"] == 150.0


def test_change_moves_the_plan_by_the_difference(base):
    client, db, chk, loan = base
    st, _ = add(client, loan, 150)
    c = st["category_id"]
    # The user moved 30 more in on the Budget page: a change keeps it.
    ok(client.put(f"/api/budgets/{T}", json={"assigned": {c: 180}}))
    assert ok(put(client, 200, "snowball", loan=loan))["state"]["planned_this_month"] == 230.0
    assert ok(client.get(URL))["strategy"] == "snowball"
    add_txn(db, chk.id, "2026-09-20", -120, "Extra car payment", category=c)
    # Lower: by the difference, but never below what was already spent from it (120).
    st = ok(put(client, 20, loan=loan))["state"]
    assert (st["planned_this_month"], st["extra"]) == (120.0, 20.0)


def test_add_keeps_a_bigger_plan_and_reuses_a_hidden_category(base):
    client, db, _chk, loan = base
    ok(client.post("/api/categories", json={"name": "Paying off debt", "kind": "spending"}), 201)
    c = db.scalar(select(TxnCategory.id).where(TxnCategory.name == "Paying off debt"))
    ok(client.put(f"/api/budgets/{T}", json={"assigned": {c: 400}}))
    ok(client.patch(f"/api/categories/{c}", json={"hidden": True}))
    st, _ = add(client, loan, 150)
    assert (st["category_id"], st["planned_this_month"]) == (c, 400.0)
    db.expire_all()
    assert db.get(TxnCategory, c).hidden is False


def test_a_category_that_cant_hold_it(base):
    client, db, _chk, loan = base
    ok(client.post("/api/categories", json={"name": "Paying off debt", "kind": "fixed"}), 201)
    detail = err(put(client, 50, loan=loan), 409)["detail"]
    assert "can't hold this plan" in detail and ok(client.get(URL))["linked"] is False


def test_not_enough_unplanned(base):
    client, db, _chk, loan = base
    body = err(put(client, 4600, loan=loan))
    assert body == {"detail": f"Only $4,500 {NOT_ENOUGH}", "code": "not_enough_unplanned", "not_planned": 4500.0}
    assert db.scalar(select(TxnCategory).where(TxnCategory.name == "Paying off debt")) is None
    assert ok(client.get(URL))["linked"] is False


def test_validation(base):
    client, db, chk, loan = base
    for body in ({"extra": 0, "strategy": "avalanche", "account_ids": [loan.id]},
                 {"extra": -1, "strategy": "avalanche", "account_ids": [loan.id]},
                 {"extra": 10, "strategy": "custom", "account_ids": [loan.id]},
                 {"extra": 10, "strategy": "avalanche", "account_ids": []},
                 {"extra": 10, "strategy": "avalanche"},
                 {"extra": 10, "strategy": "avalanche", "account_ids": [loan.id], "more": 1},
                 {"extra": 10**13, "strategy": "avalanche", "account_ids": [loan.id]},
                 {"extra": 10, "strategy": "avalanche", "account_ids": [0]}):
        assert client.put(URL, json=body).status_code == 422, body
    assert err(put(client, 10, ids=[chk.id]))["detail"] == "Pick the loans this is for."
    assert err(put(client, 10, ids=[999999]))["detail"] == "Pick the loans this is for."
    for token in ("", "short", "a" * 65, "abc$defgh", "../../etc"):
        assert undo(client, token).status_code == 422, token
    assert client.put(URL, json={"extra": 10, "strategy": "avalanche", "account_ids": [loan.id]},
                      headers={"X-FinTrack": ""}).status_code in (400, 403)


# ------------------------------------------------------------------ undo


def test_undo_of_the_first_add_puts_everything_back(base):
    client, db, _chk, loan = base
    st, token = add(client, loan, 150)
    c = st["category_id"]
    st = ok(undo(client, token))["state"]
    assert (st["linked"], st["not_planned"], st["category_id"]) == (False, 4500.0, None)
    db.expire_all()
    assert db.get(TxnCategory, c) is None
    assert db.scalar(select(Budget).where(Budget.category == c)) is None
    assert db.get(AppSetting, "debt_budget") is None
    # Single use.
    assert err(undo(client, token), 409) == {"detail": "This can't be undone any more."}


def test_undo_of_a_change(base):
    client, db, _chk, loan = base
    add(client, loan, 150)
    token = ok(put(client, 250, loan=loan))["undo"]["token"]
    st = ok(undo(client, token))["state"]
    assert (st["extra"], st["planned_this_month"], st["not_planned"]) == (150.0, 150.0, 4350.0)


def test_undo_refused_after_a_budget_change(base):
    client, db, _chk, loan = base
    st, token = add(client, loan, 150)
    c = st["category_id"]
    ok(client.put(f"/api/budgets/{T}", json={"assigned": {c: 175}}))
    body = err(undo(client, token), 409)
    assert body["detail"] == "This can't be undone any more: Paying off debt changed on the Budget page since."
    assert ok(client.get(URL))["planned_this_month"] == 175.0
    # A refused Undo changes nothing (the request rolls back), so it stays refused.
    assert err(undo(client, token), 409) == body


def test_undo_is_this_month_only(base):
    client, db, _chk, loan = base
    _st, token = add(client, loan, 150)
    raw = json.loads(db.get(AppSetting, "debt_undo").value)
    raw[token]["month"] = "2026-08"
    db.get(AppSetting, "debt_undo").value = json.dumps(raw)
    db.commit()
    assert err(undo(client, token), 409)["detail"] == "This can't be undone any more."


def test_undo_record_tampering_is_refused(base):
    client, db, _chk, loan = base
    _st, token = add(client, loan, 150)
    row = db.get(AppSetting, "debt_undo")
    raw = json.loads(row.value)
    raw[token]["rows"] = {"2026-09": ["x", 1, 2]}
    row.value = json.dumps(raw)
    db.commit()
    assert err(undo(client, token), 409)["detail"] == "This can't be undone any more."
    row.value = "not json"
    db.commit()
    assert err(undo(client, "abcdefghij"), 409)["detail"] == "This can't be undone any more."


# ------------------------------------------------------------------ delete


def test_delete_and_its_undo(base):
    client, db, _chk, loan = base
    st, _ = add(client, loan, 150)
    c = st["category_id"]
    out = ok(client.delete(URL))
    assert (out["state"]["linked"], out["state"]["not_planned"]) == (False, 4500.0)
    assert line(budget(client), c) is None
    db.expire_all()
    assert db.get(TxnCategory, c).hidden is True  # no transaction uses it
    assert err(client.delete(URL), 409) == {"detail": "Paying off debt isn't in your Budget."}
    st = ok(undo(client, out["undo"]["token"]))["state"]
    assert (st["linked"], st["planned_this_month"], st["extra"], st["not_planned"]) == (True, 150.0, 150.0, 4350.0)
    db.expire_all()
    assert db.get(TxnCategory, c).hidden is False
    # Added again later: the same category comes back.
    ok(client.delete(URL))
    assert ok(put(client, 60, loan=loan))["state"]["category_id"] == c


def test_delete_keeps_a_category_with_payments_visible(base):
    client, db, chk, loan = base
    st, _ = add(client, loan, 150)
    c = st["category_id"]
    add_txn(db, chk.id, "2026-09-20", -150, "Extra car payment", category=c)
    ok(client.delete(URL))
    db.expire_all()
    assert db.get(TxnCategory, c).hidden is False


def test_category_guard(base):
    client, db, _chk, loan = base
    st, _ = add(client, loan, 150)
    c = st["category_id"]
    for response in (client.delete(f"/api/categories/{c}"), client.patch(f"/api/categories/{c}", json={"hidden": True}),
                     client.patch(f"/api/categories/{c}", json={"kind": "fixed"})):
        assert response.status_code == 409 and "Reports › Paying off debt" in response.json()["detail"]
    ok(client.patch(f"/api/categories/{c}", json={"name": "Extra on debt"}))  # renaming is fine
    ok(client.delete(URL))
    assert client.delete(f"/api/categories/{c}").status_code == 204


# ------------------------------------------------------------------ Home and alerts leave it out


def test_home_and_alerts_leave_it_out(base):
    client, db, chk, loan = base
    st, _ = add(client, loan, 100)
    c = st["category_id"]
    add_txn(db, chk.id, "2026-09-20", -150, "Extra car payment", category=c)  # over its plan
    dash = ok(client.get("/api/dashboard"))
    assert (dash["budget"]["planned"], dash["budget"]["spent"]) == (500.0, 0.0)
    assert not [n for n in dash["needs"] if n["kind"] == "over_plan"]
    db.expire_all()
    evaluate(db, dt.date(2026, 9, 26))
    db.commit()
    assert [e.dedupe_key for e in db.scalars(select(AlertEvent).where(AlertEvent.key == "budget"))] == []


# ------------------------------------------------------------------ filing payments: counted once


def totals(client) -> dict:
    rep = ok(client.get("/api/reports/monthly", params={"months": 1}))["months"][0]
    tab = ok(client.get("/api/reports/spending"))
    home = next(m for m in ok(client.get("/api/dashboard"))["months"] if m["month"] == T)
    return {"spending": rep["spending"], "fixed": rep["fixed"], "tab": tab["summary"]["total"],
            "home": home["went_out"], "tab_cats": {c["id"]: c["amount"] for c in tab["categories"]}}


def test_a_loan_payment_filed_under_it_moves_from_fixed(base):
    client, db, chk, loan = base
    st, _ = add(client, loan, 300)
    c = st["category_id"]
    add_txn(db, chk.id, "2026-09-02", -80, "Groceries")
    pay = add_txn(db, chk.id, "2026-09-15", -300, "Car loan payment", category="LOAN_PAYMENTS")
    before = totals(client)
    assert (before["spending"], before["fixed"], before["tab"], before["home"]) == (80.0, 300.0, 380.0, 380.0)
    ok(client.patch(f"/api/transactions/{pay.id}", json={"category": c}))
    after = totals(client)
    # The same 380 out, now 380 = 80 food + 300 paying off debt, once.
    assert (after["spending"], after["fixed"], after["tab"], after["home"]) == (380.0, 0.0, 380.0, 380.0)
    assert after["tab_cats"] == {c: 300.0, "FOOD_AND_DRINK": 80.0}
    assert line(budget(client), c)["spent"] == 300.0
    assert ok(client.get("/api/dashboard"))["budget"]["spent"] == 80.0  # Home's "left" leaves it out


def test_a_transfer_filed_under_it_counts_once(base):
    client, db, chk, loan = base
    st, _ = add(client, loan, 200)
    c = st["category_id"]
    pay = add_txn(db, chk.id, "2026-09-15", -200, "Loan payment", category="LOAN_PAYMENTS",
                  is_transfer=True, transfer_source="rule")
    before = totals(client)
    assert (before["tab"], before["fixed"]) == (0.0, 0.0)
    ok(client.patch(f"/api/transactions/{pay.id}", json={"category": c}))
    t = txn(db, pay.id)
    assert (t.is_transfer, t.transfer_source, t.category_source) == (False, None, "user")
    after = totals(client)
    assert (after["tab"], after["spending"], after["home"], after["tab_cats"]) == (200.0, 200.0, 200.0, {c: 200.0})
    assert line(budget(client), c)["spent"] == 200.0
    # Setting it back to automatic lets the transfer rule mark it again (rules run).
    ok(client.patch(f"/api/transactions/{pay.id}", json={"category": None}))
    assert txn(db, pay.id).category_source != "user"


def test_users_own_transfer_mark(base):
    client, db, chk, loan = base
    c = add(client, loan, 200)[0]["category_id"]
    a = add_txn(db, chk.id, "2026-09-15", -200, "Moved", is_transfer=True, transfer_source="user")
    b = add_txn(db, chk.id, "2026-09-16", -50, "Moved too", is_transfer=True, transfer_source="user")
    # Bulk changes never undo a "Mark as transfer" the user chose themselves...
    ok(client.patch("/api/transactions", json={"ids": [a.id, b.id], "category": c}))
    assert (txn(db, a.id).is_transfer, txn(db, b.id).is_transfer) == (True, True)
    # ...filing one by hand does (the later choice wins)...
    ok(client.patch(f"/api/transactions/{a.id}", json={"category": c}))
    assert txn(db, a.id).is_transfer is False
    # ...unless the user marks it a transfer in the same change.
    ok(client.patch(f"/api/transactions/{b.id}", json={"category": c, "is_transfer": True}))
    assert txn(db, b.id).is_transfer is True
    assert line(budget(client), c)["spent"] == 200.0


def test_bulk_and_split_filing_clear_automatic_marks(base):
    client, db, chk, loan = base
    c = add(client, loan, 200)[0]["category_id"]
    a = add_txn(db, chk.id, "2026-09-15", -100, "Loan payment", is_transfer=True, transfer_source="rule")
    b = add_txn(db, chk.id, "2026-09-16", -60, "Loan payment 2", category="LOAN_PAYMENTS")
    s = add_txn(db, chk.id, "2026-09-17", -100, "Loan and lunch", is_transfer=True, transfer_source="rule")
    other = add_txn(db, chk.id, "2026-09-18", -70, "Savings move", is_transfer=True, transfer_source="rule")
    out = ok(client.patch("/api/transactions", json={"ids": [a.id, b.id], "category": c}))
    assert (out["skipped"], out["skipped_card_payments"]) == ([], [])
    assert (txn(db, a.id).is_transfer, txn(db, b.id).is_transfer) == (False, False)
    ok(client.put(f"/api/transactions/{s.id}/splits", json={"splits": [
        {"amount": -60, "category": c}, {"amount": -40, "category": "FOOD_AND_DRINK"}]}))
    assert txn(db, s.id).is_transfer is False
    assert txn(db, other.id).is_transfer is True  # not filed under it: untouched
    assert line(budget(client), c)["spent"] == 220.0
    assert totals(client)["tab_cats"] == {c: 220.0, "FOOD_AND_DRINK": 40.0}


def test_no_debt_category_changes_nothing(base):
    client, db, chk, _loan = base
    a = add_txn(db, chk.id, "2026-09-15", -100, "Loan payment", is_transfer=True, transfer_source="rule")
    ok(client.patch(f"/api/transactions/{a.id}", json={"category": "GENERAL_MERCHANDISE"}))
    assert txn(db, a.id).is_transfer is True


def test_filed_while_pending_stays_counted_after_it_posts(unlocked, db, fake_plaid, fixed_today):
    from tests.test_followups import by_name, link, posted
    from tests.test_plaid import txn as plaid_txn

    c = unlocked
    item = link(c, fake_plaid, [
        {"cursor_in": None, "added": [plaid_txn("p1", "chk", 200, "Loan payment", date="2026-09-10", pending=True)],
         "modified": [], "removed": [], "next_cursor": "c1", "has_more": False},
        {"cursor_in": "c1", "added": [posted("t1", "p1", 200, "Loan payment")],
         "modified": [], "removed": [{"transaction_id": "p1"}], "next_cursor": "c2", "has_more": False},
    ])
    ok(c.post("/api/rules", json={"field": "name", "op": "contains", "text": "loan payment", "action": "transfer"}), 201)
    loan = make_account(db, "Car loan", "loan", 8000, minimum_payment_cents=30000)
    add_budget(db, T, {"FOOD_AND_DRINK": 100})
    cat_id = ok(put(c, 200, loan=loan))["state"]["category_id"]
    pay = by_name(c)["Loan payment"]
    assert pay["is_transfer"] is True and pay["pending"] is True
    ok(c.patch(f"/api/transactions/{pay['id']}", json={"category": cat_id}))
    assert by_name(c)["Loan payment"]["is_transfer"] is False
    assert ok(c.post("/api/plaid/sync", json={"item_id": item["id"]}))["results"][0]["ok"]
    pay = by_name(c)["Loan payment"]
    assert (pay["pending"], pay["category"], pay["is_transfer"]) == (False, cat_id, False)
    assert line(budget(c), cat_id)["spent"] == 200.0


# ------------------------------------------------------------------ loans only (owner decision)

CARD_PAYMENT = ("This is a credit card payment. Card payments are already part of your Budget money, "
                "so they don't go in “Paying off debt”.")


def test_loans_only(base):
    client, db, _chk, loan = base
    card = make_account(db, "Visa", "credit", 900, minimum_payment_cents=2500)
    body = err(put(client, 50, ids=[loan.id, card.id]))
    assert body == {"detail": "Credit card payments are already part of your Budget money, so they don't go in this line."}
    assert ok(client.get(URL))["linked"] is False
    student = make_account(db, "Student loan", "loan", 5000, minimum_payment_cents=10000)
    st = ok(put(client, 50, ids=[student.id, loan.id]))["state"]
    assert st["loans"] == [{"account_id": student.id, "name": "Student loan"}, {"account_id": loan.id, "name": "Car loan"}]
    assert st["account_ids"] == [student.id, loan.id]


@pytest.fixture
def cards(base):
    """The debt budget is set up; three kinds of credit card payment and one loan payment."""
    client, db, chk, loan = base
    c = add(client, loan, 300)[0]["category_id"]
    visa = make_account(db, "Visa", "credit", 900)
    paired = add_txn(db, chk.id, "2026-09-10", -200, "Online pmt", category="LOAN_PAYMENTS",
                     is_transfer=True, transfer_source="auto")  # the card-payment fix paired it
    on_card = add_txn(db, visa.id, "2026-09-11", 200, "Payment thank you", category="LOAN_PAYMENTS")
    coded = add_txn(db, chk.id, "2026-09-12", -150, "Online pmt", category="LOAN_PAYMENTS",
                    plaid_detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
    loan_pay = add_txn(db, chk.id, "2026-09-13", -300, "Online pmt", category="LOAN_PAYMENTS",
                       plaid_detailed="LOAN_PAYMENTS_CAR_PAYMENT")
    return client, db, c, (paired, on_card, coded), loan_pay


def test_a_card_payment_cant_be_filed_there(cards):
    client, db, c, blocked, loan_pay = cards
    for t in blocked:
        assert err(client.patch(f"/api/transactions/{t.id}", json={"category": c})) == {"detail": CARD_PAYMENT}
        assert err(client.put(f"/api/transactions/{t.id}/splits", json={"splits": [
            {"amount": t.amount_cents / 200, "category": c},
            {"amount": t.amount_cents / 200, "category": "FOOD_AND_DRINK"}]})) == {"detail": CARD_PAYMENT}
        row = txn(db, t.id)
        assert (row.category, row.is_transfer, row.transfer_source) == (
            "LOAN_PAYMENTS", t.is_transfer, t.transfer_source)  # untouched, mark kept
    # Other categories are fine for them; a loan payment files as before.
    ok(client.patch(f"/api/transactions/{blocked[2].id}", json={"category": "GENERAL_MERCHANDISE"}))
    ok(client.patch(f"/api/transactions/{loan_pay.id}", json={"category": c}))
    assert line(budget(client), c)["spent"] == 300.0


def test_bulk_and_apply_to_similar_skip_card_payments(cards):
    client, db, c, blocked, loan_pay = cards
    ids = [b.id for b in blocked] + [loan_pay.id]
    out = ok(client.patch("/api/transactions", json={"ids": ids, "category": c}))
    assert [i["id"] for i in out["items"]] == [loan_pay.id]
    assert out["skipped"] == [b.id for b in blocked] and out["skipped_card_payments"] == [b.id for b in blocked]
    paired = txn(db, blocked[0].id)
    assert (paired.category, paired.is_transfer, paired.transfer_source) == ("LOAN_PAYMENTS", True, "auto")
    ok(client.patch(f"/api/transactions/{loan_pay.id}", json={"category": "LOAN_PAYMENTS"}))
    out = ok(client.post("/api/transactions/recategorize", json={
        "field": "name", "text": "Online pmt", "category": c, "include_manual": True}))
    assert out == {"changed": 1, "skipped": 2}  # the paired and the coded card payments stay out
    assert txn(db, loan_pay.id).category == c and txn(db, blocked[2].id).category == "LOAN_PAYMENTS"
    assert line(budget(client), c)["spent"] == 300.0


# ------------------------------------------------------------------ review fixes (Release 3.10 pair 2)


def test_undo_of_filing_puts_the_transfer_mark_back(base):
    """The Transactions page's Undo of filing a payment the user had marked as a transfer brings
    their mark back exactly (the server remembers which mark filing cleared)."""
    client, db, chk, loan = base
    c = add(client, loan, 200)[0]["category_id"]
    pay = add_txn(db, chk.id, "2026-09-15", -200, "Loan payment", category="LOAN_PAYMENTS",
                  is_transfer=True, transfer_source="user")
    before = ok(client.get("/api/reports/spending"))["summary"]["total"]
    ok(client.patch(f"/api/transactions/{pay.id}", json={"category": c}))
    assert (txn(db, pay.id).is_transfer, line(budget(client), c)["spent"]) == (False, 200.0)
    ok(client.post("/api/transactions/restore", json={"items": [
        {"id": pay.id, "category": "LOAN_PAYMENTS", "category_source": "plaid"}]}))
    t = txn(db, pay.id)
    assert (t.category, t.is_transfer, t.transfer_source) == ("LOAN_PAYMENTS", True, "user")
    assert ok(client.get("/api/reports/spending"))["summary"]["total"] == before
    assert line(budget(client), c)["spent"] == 0.0
    # Used once: a later filing and its Undo don't bring back a mark the user has since changed.
    ok(client.patch(f"/api/transactions/{pay.id}", json={"is_transfer": False}))
    ok(client.patch(f"/api/transactions/{pay.id}", json={"category": c}))
    ok(client.post("/api/transactions/restore", json={"items": [
        {"id": pay.id, "category": "LOAN_PAYMENTS", "category_source": "plaid"}]}))
    assert txn(db, pay.id).is_transfer is False


def test_undo_back_into_debt_counts_again(base):
    """Undo of "back to automatic" puts a payment back under Paying off debt: a transfer
    rule's mark the reset brought back is cleared again, so it counts."""
    client, db, chk, loan = base
    ok(client.post("/api/rules", json={"field": "name", "op": "contains", "text": "loan payment", "action": "transfer"}), 201)
    c = add(client, loan, 200)[0]["category_id"]
    pay = add_txn(db, chk.id, "2026-09-15", -200, "Loan payment", category="LOAN_PAYMENTS")
    ok(client.patch(f"/api/transactions/{pay.id}", json={"category": c}))
    assert line(budget(client), c)["spent"] == 200.0
    ok(client.patch("/api/transactions", json={"ids": [pay.id], "category": None}))
    assert txn(db, pay.id).is_transfer is True
    ok(client.post("/api/transactions/restore", json={"items": [
        {"id": pay.id, "category": c, "category_source": "user"}]}))
    assert txn(db, pay.id).is_transfer is False
    assert line(budget(client), c)["spent"] == 200.0


def test_restore_cant_file_a_card_payment(base):
    client, db, chk, loan = base
    c = add(client, loan, 200)[0]["category_id"]
    coded = add_txn(db, chk.id, "2026-09-12", -150, "Online pmt", category="LOAN_PAYMENTS",
                    plaid_detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
    r = client.post("/api/transactions/restore", json={"items": [{"id": coded.id, "category": c, "category_source": "user"}]})
    assert err(r) == {"detail": CARD_PAYMENT}
    assert txn(db, coded.id).category == "LOAN_PAYMENTS"


def test_a_rule_never_files_a_card_payment(base):
    """A category rule for Paying off debt skips card payments (the next rule decides); it
    still files a loan payment."""
    client, db, chk, loan = base
    c = add(client, loan, 200)[0]["category_id"]
    coded = add_txn(db, chk.id, "2026-09-12", -150, "Online pmt", category="LOAN_PAYMENTS",
                    plaid_detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
    loan_pay = add_txn(db, chk.id, "2026-09-13", -300, "Online pmt", category="LOAN_PAYMENTS",
                       plaid_detailed="LOAN_PAYMENTS_CAR_PAYMENT")
    ok(client.post("/api/rules", json={"field": "name", "op": "contains", "text": "online pmt", "action": "category",
                                        "category": c}), 201)
    ok(client.post("/api/rules", json={"field": "name", "op": "contains", "text": "pmt", "action": "category",
                                        "category": "GENERAL_MERCHANDISE"}), 201)  # after the first
    assert txn(db, coded.id).category == "GENERAL_MERCHANDISE"  # fell through to the next rule
    assert txn(db, loan_pay.id).category == c
    assert line(budget(client), c)["spent"] == 300.0


def test_a_paired_card_payment_stays_out_after_its_mark_is_cleared(cards):
    """Setting a paired bank-side card payment's category by hand clears the "auto" mark
    (Release 3.9.1); it is still a card payment (by its pairing), so it can't be filed."""
    client, db, c, blocked, loan_pay = cards
    paired = blocked[0]
    ok(client.patch(f"/api/transactions/{paired.id}", json={"category": "GENERAL_MERCHANDISE"}))
    row = txn(db, paired.id)
    assert (row.is_transfer, row.transfer_source) == (False, None)
    assert err(client.patch(f"/api/transactions/{paired.id}", json={"category": c})) == {"detail": CARD_PAYMENT}
    out = ok(client.patch("/api/transactions", json={"ids": [paired.id, loan_pay.id], "category": c}))
    assert out["skipped_card_payments"] == [paired.id]
    r = client.post("/api/transactions/restore", json={"items": [{"id": paired.id, "category": c, "category_source": "user"}]})
    assert err(r) == {"detail": CARD_PAYMENT}
    assert txn(db, paired.id).category == "GENERAL_MERCHANDISE"
    assert line(budget(client), c)["spent"] == 300.0


def test_undo_of_the_first_add_removes_the_group_it_made(base):
    client, db, _chk, loan = base
    groups = set(db.scalars(select(CategoryGroup.id)))
    st, token = add(client, loan, 150)
    db.expire_all()
    made = db.get(TxnCategory, st["category_id"]).group_id
    assert made not in groups  # the add made "Other and saving"
    ok(undo(client, token))
    db.expire_all()
    assert set(db.scalars(select(CategoryGroup.id))) == groups
    # A group that was there already stays.
    st, token = add(client, loan, 150)
    db.expire_all()
    kept = db.get(TxnCategory, st["category_id"]).group_id
    db.add(TxnCategory(id="c_keep_test", name="Keep me", hue=200, kind="spending", custom=True, group_id=kept))
    db.commit()
    ok(undo(client, token))
    db.expire_all()
    assert db.get(CategoryGroup, kept) is not None

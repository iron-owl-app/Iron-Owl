"""Release 3.9.1: credit card payments are transfers (services/card_payments.py).

The card's purchases count as spending; the payment from checking (and the payment as the
card sees it) is money moved between the user's own accounts, so it counts nowhere: not in Reports,
Home's months, the Budget, income, "Needs a category" or the large-purchase alert. It stays a
bill on the calendar (real money leaving checking).
"""
from __future__ import annotations

import csv
import datetime as dt
import io

import pytest
from sqlalchemy import select

from app.models import AlertEvent, AppSetting, CategoryGroup, RecurringItem, Transaction, TxnCategory
from app.services import calendar, card_payments, dashboard, recurring, spending, txns
from app.services import rules as rules_service
from app.services.alerts import evaluate
from app.services.categories import load as load_categories
from app.utils import utcnow
from tests.conftest import PASSWORD, add_budget, add_txn, make_account

D = dt.date
TODAY = D(2026, 9, 26)  # conftest.FIXED_TODAY
CC = card_payments.DETAILED


def ok(response, status: int = 200):
    assert response.status_code == status, response.text
    return response.json() if response.content else None


def checking(db, balance=1000.0, **kw):
    return make_account(db, "Checking", "bank", balance, plaid_type="depository", plaid_subtype="checking",
                        mask="4417", institution_name="Neighborhood Bank", **kw)


def card(db, name="Visa", **kw):
    return make_account(db, name, "credit", 300, plaid_type="credit", plaid_subtype="credit card", mask="9911", **kw)


def run(db) -> int:
    changed = rules_service.apply_all(db)
    db.commit()
    db.expire_all()
    return changed


def mark(db, txn) -> tuple:
    db.expire_all()
    t = db.get(Transaction, txn.id)
    return bool(t.is_transfer), t.transfer_source


AUTO = (True, "auto")
NONE = (False, None)


@pytest.fixture
def paid(unlocked, db, fixed_today):
    """Checking pays the Visa $500 (Plaid's detailed code on both sides); the Visa has a $120
    purchase. Nothing marked yet (rows written directly, no rule run)."""
    chk, visa = checking(db), card(db)
    purchase = add_txn(db, visa.id, "2026-09-10", -120, "Target", category="GENERAL_MERCHANDISE")
    out = add_txn(db, chk.id, "2026-09-20", -500, "CHASE CREDIT CRD AUTOPAY", category="LOAN_PAYMENTS",
                  plaid_detailed=CC)
    into = add_txn(db, visa.id, "2026-09-21", 500, "AUTOMATIC PAYMENT - THANK", category="LOAN_PAYMENTS",
                   plaid_detailed=CC)
    return unlocked, db, chk, visa, purchase, out, into


# ------------------------------------------------------------------ the checking side


def test_card_payment_on_checking_is_a_transfer(paid):
    client, db, chk, visa, purchase, out, into = paid
    assert mark(db, out) == NONE
    run(db)
    assert mark(db, out) == AUTO and mark(db, into) == AUTO
    assert mark(db, purchase) == NONE
    # The category stays Plaid's (Loan payments): only the transfer flag changes.
    t = db.get(Transaction, out.id)
    assert (t.category, t.category_source, t.rule_id) == ("LOAN_PAYMENTS", "plaid", None)
    # The Transactions page (badge) and the CSV export agree.
    items = {i["id"]: i for i in ok(client.get("/api/transactions"))["items"]}
    assert items[out.id]["is_transfer"] is True and items[into.id]["is_transfer"] is True
    assert items[purchase.id]["is_transfer"] is False
    rows = list(csv.DictReader(io.StringIO(client.get("/api/transactions/export.csv").text)))
    assert {r["name"]: r["transfer"] for r in rows} == {
        "CHASE CREDIT CRD AUTOPAY": "true", "AUTOMATIC PAYMENT - THANK": "true", "Target": "false",
    }
    # Money in/out on the Transactions page leaves both sides out (like any transfer).
    summary = ok(client.get("/api/transactions/summary"))
    assert (summary["money_in"], summary["money_out"]) == (0.0, -120.0)
    # A second run changes nothing.
    assert run(db) == 0


def test_user_not_a_transfer_wins_and_is_reversible(paid):
    client, db, chk, visa, purchase, out, into = paid
    run(db)
    ok(client.patch(f"/api/transactions/{out.id}", json={"is_transfer": False}))
    assert mark(db, out) == (False, "user")
    run(db)
    assert mark(db, out) == (False, "user")  # rule runs and syncs never undo the user's choice
    months = {m["month"]: m for m in ok(client.get("/api/dashboard"))["months"]}
    assert months["2026-09"]["went_out"] == 620.0  # the user said it counts: 120 + 500
    ok(client.patch(f"/api/transactions/{out.id}", json={"is_transfer": True}))
    assert mark(db, out) == (True, "user")
    run(db)
    assert mark(db, out) == (True, "user")


def test_hand_set_category_wins_and_back_to_automatic_restores_the_mark(paid):
    client, db, chk, visa, purchase, out, into = paid
    run(db)
    ok(client.patch(f"/api/transactions/{out.id}", json={"category": "RENT_AND_UTILITIES"}))
    assert mark(db, out) == NONE  # the user's category counts
    run(db)
    assert mark(db, out) == NONE
    ok(client.patch(f"/api/transactions/{out.id}", json={"category": None}))  # back to automatic
    assert mark(db, out) == AUTO


def test_bulk_category_undo_and_splits_release_the_mark(paid):
    client, db, chk, visa, purchase, out, into = paid
    run(db)
    changed, previous, _ = txns.bulk_set_category(db, [out.id, into.id], "OTHER")
    db.commit()
    assert mark(db, out) == NONE and mark(db, into) == NONE
    txns.restore_categories(db, [(p["id"], p["category"], p["category_source"]) for p in previous])
    db.commit()
    assert mark(db, out) == AUTO and mark(db, into) == AUTO
    # Splitting it is setting its categories by hand.
    ok(client.put(f"/api/transactions/{out.id}/splits", json={"splits": [
        {"amount": -300, "category": "LOAN_PAYMENTS"}, {"amount": -200, "category": "OTHER"},
    ]}))
    assert mark(db, out) == NONE
    ok(client.put(f"/api/transactions/{out.id}/splits", json={"splits": []}))
    assert mark(db, out) == AUTO
    # Change all from this merchant: set by hand.
    txns.recategorize(db, "name", "CHASE CREDIT CRD AUTOPAY", "RENT_AND_UTILITIES", include_manual=True)
    db.commit()
    assert mark(db, out) == NONE


def test_rules_and_the_mark(paid):
    client, db, chk, visa, purchase, out, into = paid
    run(db)
    # A transfer rule takes over (source "rule"); switched off, the card payment mark is back.
    rule = ok(client.post("/api/rules", json={"field": "any", "op": "contains", "text": "crd autopay",
                                              "action": "transfer"}), 201)
    assert mark(db, out) == (True, "rule")
    ok(client.patch(f"/api/rules/{rule['id']}", json={"enabled": False}))
    assert mark(db, out) == AUTO
    # A category rule sets the category and leaves the mark alone.
    ok(client.post("/api/rules", json={"field": "any", "op": "contains", "text": "crd autopay",
                                       "action": "category", "category": "GENERAL_MERCHANDISE"}), 201)
    db.expire_all()
    t = db.get(Transaction, out.id)
    assert (t.category, t.category_source) == ("GENERAL_MERCHANDISE", "rule")
    assert mark(db, out) == AUTO
    # Nothing left for a preview to report.
    assert rules_service.preview_changes(db, None)[0] == 0


def test_no_card_in_fintrack_means_the_payment_is_the_spending(unlocked, db, fixed_today):
    """Paying a card whose purchases FinTrack never sees: the payment is the only record.
    A card with purchases isn't enough: the payment must show up on that card."""
    chk = checking(db)
    out = add_txn(db, chk.id, "2026-09-20", -500, "STORE CARD PAYMENT", category="LOAN_PAYMENTS",
                  plaid_detailed=CC)
    empty = card(db, "New card")  # linked, but no transactions yet
    run(db)
    assert mark(db, out) == NONE
    add_txn(db, empty.id, "2026-09-01", -10, "Coffee")
    run(db)
    assert mark(db, out) == NONE  # a card with purchases, but not the one they paid
    add_txn(db, empty.id, "2026-09-22", 500, "PAYMENT THANK YOU", category="LOAN_PAYMENTS", plaid_detailed=CC)
    run(db)
    assert mark(db, out) == AUTO
    # Hiding the card (its purchases stop counting) makes the payment count again, and back.
    ok(unlocked.patch(f"/api/accounts/{empty.id}", json={"hidden": True}))
    assert mark(db, out) == NONE
    ok(unlocked.patch(f"/api/accounts/{empty.id}", json={"hidden": False}))
    assert mark(db, out) == AUTO


def test_other_loan_payments_are_untouched(unlocked, db, fixed_today):
    chk, visa = checking(db), card(db)
    add_txn(db, visa.id, "2026-09-01", -10, "Coffee")
    mortgage = add_txn(db, chk.id, "2026-09-01", -1500, "ROCKET MORTGAGE", category="LOAN_PAYMENTS",
                       plaid_detailed="LOAN_PAYMENTS_MORTGAGE_PAYMENT")
    car = add_txn(db, chk.id, "2026-09-02", -350, "TOYOTA CREDIT CARD PAYMENT", category="LOAN_PAYMENTS",
                  plaid_detailed="LOAN_PAYMENTS_CAR_PAYMENT")  # the detailed code decides
    run(db)
    assert mark(db, mortgage) == NONE and mark(db, car) == NONE


# ------------------------------------------------------------------ the card side


def test_money_into_a_card_is_never_income_spending_or_a_refund(unlocked, db, fixed_today):
    chk, visa = checking(db), card(db)
    add_txn(db, chk.id, "2026-09-01", 2000, "Payroll", category="INCOME")
    purchase = add_txn(db, visa.id, "2026-09-05", -80, "Target", category="GENERAL_MERCHANDISE")
    refund = add_txn(db, visa.id, "2026-09-06", 20, "Target refund", category="GENERAL_MERCHANDISE")
    # Older rows (no detailed code) and a Plaid "Transfer in" on the card: still the payment.
    paid_in = add_txn(db, visa.id, "2026-09-10", 400, "ONLINE PAYMENT", category="TRANSFER_IN")
    loan_in = add_txn(db, visa.id, "2026-09-12", 100, "PAYMENT RECEIVED", category="LOAN_PAYMENTS")
    cmap = load_categories(db)
    assert spending.received_by_month(db, cmap, [chk.id, visa.id], "2026-09", "2026-09") == {"2026-09": 240000}
    run(db)
    assert mark(db, paid_in) == AUTO and mark(db, loan_in) == AUTO
    assert mark(db, refund) == NONE and mark(db, purchase) == NONE  # a refund still nets out
    assert spending.received_by_month(db, cmap, [chk.id, visa.id], "2026-09", "2026-09") == {"2026-09": 200000}
    bm = ok(unlocked.get("/api/budgets", params={"month": "2026-09"}))
    assert bm["income"]["received"] == 2000.0
    t = spending.month_totals(db, cmap, "2026-09")
    assert (t.income, t.total_spent, t.total_fixed) == (200000, 6000, 0)


# ------------------------------------------------------------------ older rows (before schema v7)


def test_names_a_loan():
    loans = ("ROCKET MORTGAGE PAYMENT", "PAYMENT TO NAVIENT STUDENT", "CREDIT UNION AUTO LOAN PMT",
             "HONDA AUTO FINANCE CARD PAYMENT", "CREDIT CARD LOAN", "CAR PAYMENT", "BMW LEASE")
    others = ("CHASE CREDIT CRD AUTOPAY", "ONLINE PMT 12345", "AUTOPAY", "CARDMEMBER SERV WEB PYMT")
    assert all(card_payments.names_a_loan(n, None) for n in loans)
    assert not any(card_payments.names_a_loan(n, None) for n in others)
    assert card_payments.names_a_loan("ACH DEBIT", "Rocket Mortgage")


def test_older_rows_pair_or_keep_counting(unlocked, db, fixed_today):
    chk, visa = checking(db), card(db)
    add_txn(db, visa.id, "2026-08-01", -50, "Coffee")
    # An equal payment into the card within 5 days pairs, whatever the checking name says.
    paired = add_txn(db, chk.id, "2026-08-10", -250, "ONLINE PMT 12345", category="LOAN_PAYMENTS")
    card_side = add_txn(db, visa.id, "2026-08-12", 250, "PAYMENT THANK YOU", category="LOAN_PAYMENTS")
    # A second payment of the same amount, farther from the card row, has nothing left to pair with.
    unpaired = add_txn(db, chk.id, "2026-08-07", -250, "ONLINE PMT 67890", category="LOAN_PAYMENTS")
    # Too far apart, or a cent off: no pair.
    late = add_txn(db, chk.id, "2026-07-01", -75, "ONLINE PMT", category="LOAN_PAYMENTS")
    add_txn(db, visa.id, "2026-07-09", 75, "PAYMENT THANK YOU", category="LOAN_PAYMENTS")
    cent = add_txn(db, chk.id, "2026-06-01", -80, "ONLINE PMT", category="LOAN_PAYMENTS")
    add_txn(db, visa.id, "2026-06-02", 80.01, "PAYMENT THANK YOU", category="LOAN_PAYMENTS")
    # The name alone is no longer enough: no card row, no mark.
    named = add_txn(db, chk.id, "2026-08-20", -300, "CHASE CREDIT CRD AUTOPAY", category="LOAN_PAYMENTS")
    # A loan never pairs, even with an equal payment into a card.
    mortgage = add_txn(db, chk.id, "2026-08-01", -1500, "ROCKET MORTGAGE PAYMENT", category="LOAN_PAYMENTS")
    add_txn(db, visa.id, "2026-08-02", 1500, "PAYMENT THANK YOU", category="LOAN_PAYMENTS")
    utility = add_txn(db, chk.id, "2026-08-03", -90, "DUKE ENERGY PAYMENT", category="RENT_AND_UTILITIES")
    same_as_card = add_txn(db, chk.id, "2026-08-11", -40, "CITY WATER PAYMENT", category="RENT_AND_UTILITIES")
    add_txn(db, visa.id, "2026-08-12", 40, "Refund", category="GENERAL_MERCHANDISE")  # a refund isn't a payment
    run(db)
    assert mark(db, paired) == AUTO and mark(db, card_side) == AUTO
    for t in (unpaired, late, cent, named, mortgage, utility, same_as_card):
        assert mark(db, t) == NONE, t.name
    # A card left out of FinTrack (hidden) doesn't pair.
    ok(unlocked.patch(f"/api/accounts/{visa.id}", json={"hidden": True}))
    assert mark(db, paired) == NONE


# ------------------------------------------------------------------ Reports and Home


def test_reports_and_home_months_count_the_purchases_once(unlocked, db, fixed_today):
    chk, visa = checking(db, 1000), card(db)
    add_txn(db, chk.id, "2026-08-01", 3000, "Payroll", category="INCOME")
    add_txn(db, visa.id, "2026-08-05", -400, "Safeway", category="FOOD_AND_DRINK")
    add_txn(db, chk.id, "2026-08-02", -1200, "ROCKET MORTGAGE", category="LOAN_PAYMENTS",
            plaid_detailed="LOAN_PAYMENTS_MORTGAGE_PAYMENT")
    add_txn(db, chk.id, "2026-08-25", -400, "CARD PAYMENT", category="LOAN_PAYMENTS", plaid_detailed=CC)
    add_txn(db, visa.id, "2026-08-26", 400, "PAYMENT THANK YOU", category="TRANSFER_IN",
            plaid_detailed="TRANSFER_IN_ACCOUNT_TRANSFER")

    def august() -> tuple:
        rep = {m["month"]: m for m in ok(unlocked.get("/api/reports/monthly", params={"months": 3}))["months"]}
        home = {m["month"]: m for m in ok(unlocked.get("/api/dashboard"))["months"]}
        r, h = rep["2026-08"], home["2026-08"]
        assert h["went_out"] == round(r["spending"] + r["fixed"], 2)
        return r["income"], r["spending"], r["fixed"], h["went_out"], h["left_over"]

    assert august() == (3000.0, 400.0, 1600.0, 2000.0, 1000.0)  # before: the card counted twice
    run(db)
    assert august() == (3000.0, 400.0, 1200.0, 1600.0, 1400.0)
    # Reports › Spending (Release 3.10 dropped Year in review): total = spending + fixed.
    spent = ok(unlocked.get("/api/reports/spending", params={"month": "2026-08"}))
    assert spent["summary"]["total"] == 1600.0
    assert sorted((c["id"], c["amount"]) for c in spent["categories"]) == [
        ("FOOD_AND_DRINK", 400.0), ("LOAN_PAYMENTS", 1200.0)]


def test_card_payment_bill_stays_on_the_calendar(unlocked, db, fixed_today):
    """Detection still finds the card payment as a bill (no budget category: the purchases are
    the spending), the calendar marks it paid, and Coming up subtracts the next one."""
    chk, visa = checking(db, 1000), card(db)
    add_txn(db, visa.id, "2026-09-02", -60, "Target", category="GENERAL_MERCHANDISE")
    for day in ("2026-06-24", "2026-07-24", "2026-08-24", "2026-09-24"):
        add_txn(db, chk.id, day, -500, "CHASE CREDIT CRD AUTOPAY", category="LOAN_PAYMENTS", plaid_detailed=CC)
        add_txn(db, visa.id, day, 500, "AUTOMATIC PAYMENT - THANK", category="LOAN_PAYMENTS", plaid_detailed=CC)
    run(db)
    assert all(t.is_transfer for t in db.scalars(select(Transaction).where(Transaction.amount_cents == -50000)))
    created = recurring.detect(db, TODAY)
    db.commit()
    item = db.get(RecurringItem, created[0])
    assert (item.amount_cents, item.cadence, item.category_id, item.account_id) == (-50000, "monthly", None, chk.id)
    ok(unlocked.patch(f"/api/recurring/{item.id}", json={"status": "active"}))
    db.expire_all()
    assert db.get(RecurringItem, item.id).category_id is None  # confirming doesn't give it one either
    occ = {o.base_date.isoformat(): o for o in calendar.occurrences_for(db, TODAY, D(2026, 9, 1), D(2026, 10, 31))}
    assert occ["2026-09-24"].status == "paid" and occ["2026-10-24"].status == "upcoming"
    cal = ok(unlocked.get("/api/forecast/calendar", params={"from": "2026-09-26", "to": "2026-10-31"}))
    days = {d["date"]: d["balance"] for d in cal["days"]}
    assert days["2026-10-23"] - days["2026-10-24"] == 500.0  # real money leaving checking
    # Coming up (today + 7) with the bill moved into the week.
    ok(unlocked.put(f"/api/recurring/{item.id}/occurrences/2026-10-24", json={"moved_to": "2026-09-30"}))
    cu = ok(unlocked.get("/api/dashboard"))["coming_up"]
    rows = [r for r in cu["rows"] if r["recurring_id"] == item.id]
    assert len(rows) == 1 and rows[0]["amount"] == -500.0 and rows[0]["after"] == 500.0
    # And the Budget's bills don't include it (no category).
    bm = ok(unlocked.get("/api/budgets", params={"month": "2026-09"}))
    assert bm["bills_outside"] == [] and all(c["bills"] is None for c in bm["categories"])


# ------------------------------------------------------------------ Budget


def test_budget_envelopes_count_the_purchases_not_the_payment(unlocked, db, fixed_today):
    chk, visa = checking(db, 3000), card(db)
    add_budget(db, "2026-09", {"GENERAL_MERCHANDISE": 500})
    db.add(CategoryGroup(name="Everyday", position=0))
    db.commit()
    group_id = db.scalar(select(CategoryGroup.id))
    db.get(TxnCategory, "GENERAL_MERCHANDISE").group_id = group_id
    db.commit()
    add_txn(db, visa.id, "2026-09-05", -120, "Target", category="GENERAL_MERCHANDISE")
    out = add_txn(db, chk.id, "2026-09-20", -620, "CITI CARD PAYMENT", category="LOAN_PAYMENTS", plaid_detailed=CC)
    # The card files the payment under Transfer in, so nothing offsets the checking side.
    into = add_txn(db, visa.id, "2026-09-21", 620, "PAYMENT THANK YOU", category="TRANSFER_IN",
                   plaid_detailed="TRANSFER_IN_ACCOUNT_TRANSFER")
    # Before: the payment sits in the Budget's loan payments, nags for a category (Loan
    # payments isn't in any group), and the card side counts as money that came in.
    bm = ok(unlocked.get("/api/budgets", params={"month": "2026-09"}))
    assert [(f["category"], f["spent"]) for f in bm["fixed"]] == [("LOAN_PAYMENTS", 620.0)]
    needs = ok(unlocked.get("/api/transactions", params={"view": "needs_category"}))["items"]
    assert [i["id"] for i in needs] == [out.id]
    assert bm["income"]["received"] == 620.0
    run(db)
    bm = ok(unlocked.get("/api/budgets", params={"month": "2026-09"}))
    assert bm["fixed"] == [] and bm["unbudgeted"] == [] and bm["income"]["received"] == 0.0
    assert ok(unlocked.get("/api/transactions", params={"view": "needs_category"}))["items"] == []
    # Even with a rule putting the payment into the Shopping envelope, only the purchase counts.
    ok(unlocked.post("/api/rules", json={"field": "any", "op": "contains", "text": "card payment",
                                         "action": "category", "category": "GENERAL_MERCHANDISE"}), 201)
    bm = ok(unlocked.get("/api/budgets", params={"month": "2026-09"}))
    lines = {c["category"]: c for c in bm["categories"]}
    assert lines["GENERAL_MERCHANDISE"]["spent"] == 120.0 and lines["GENERAL_MERCHANDISE"]["available"] == 380.0
    assert bm["fixed"] == [] and bm["unbudgeted"] == []  # "Spending without a plan": nothing
    needs = ok(unlocked.get("/api/transactions", params={"view": "needs_category"}))["items"]
    assert needs == []
    review_rows = ok(unlocked.get("/api/spending/review", params={"month": "2026-09"}))
    assert "620" not in str(review_rows)


# ------------------------------------------------------------------ alerts


def test_large_card_payment_is_not_a_large_purchase(unlocked, db, fake_plaid, fixed_today):
    """End to end through a sync: the payment from checking (Plaid's detailed code) is marked
    before alerts run, so only the real purchase is "Large"."""
    from tests.test_plaid import acct, exchange, txn

    token = "access-sandbox-bank-0000-SECRET"
    fake_plaid.add_item("public-bank", token, "item_bank", "First Bank")
    fake_plaid.accounts[token] = [
        acct("chk", "Checking", "depository", "checking", 5000),
        acct("visa", "Visa", "credit", "credit card", 620),
    ]
    purchase = txn("p1", "visa", 620, "BESTBUY #12", date="2026-09-24", merchant="Best Buy",
                   category="GENERAL_MERCHANDISE")
    pay = txn("pay1", "chk", 900, "CHASE CREDIT CRD AUTOPAY", date="2026-09-25", category="LOAN_PAYMENTS")
    pay["personal_finance_category"]["detailed"] = CC
    pay_in = txn("pay1c", "visa", -900, "AUTOMATIC PAYMENT - THANK", date="2026-09-25", category="LOAN_PAYMENTS")
    pay_in["personal_finance_category"]["detailed"] = CC
    fake_plaid.txn_pages[token] = [{"cursor_in": None, "added": [purchase, pay, pay_in], "modified": [],
                                    "removed": [], "next_cursor": "c1", "has_more": False}]
    exchange(unlocked, "public-bank", "bank")
    db.expire_all()
    rows = {t.plaid_transaction_id: t for t in db.scalars(select(Transaction))}
    assert (rows["pay1"].is_transfer, rows["pay1"].transfer_source) == AUTO
    assert (rows["pay1c"].is_transfer, rows["pay1c"].transfer_source) == AUTO
    assert rows["p1"].is_transfer is False
    big = {e.dedupe_key for e in db.scalars(select(AlertEvent).where(AlertEvent.key == "big"))}
    assert big == {f"big:{rows['p1'].id}"}
    needs = [n for n in ok(unlocked.get("/api/dashboard"))["needs"] if n["kind"] == "big_purchase"]
    assert [n["data"]["transaction_id"] for n in needs] == [rows["p1"].id]


def test_big_alert_and_home_skip_card_payments(paid):
    client, db, chk, visa, purchase, out, into = paid
    # An event that fired before the payment was marked (e.g. before this release).
    db.add(AlertEvent(key="big", severity="warn", title="Large transaction", body="", read=False,
                      created_at=utcnow(), dedupe_key=f"big:{out.id}", cleared=False))
    db.commit()
    assert [n["data"]["transaction_id"] for n in ok(client.get("/api/dashboard"))["needs"]
            if n["kind"] == "big_purchase"] == [out.id]
    run(db)
    assert [n for n in ok(client.get("/api/dashboard"))["needs"] if n["kind"] == "big_purchase"] == []
    assert dashboard._big_purchase_alerts(db) == []  # noqa: SLF001
    # And no new event for it.
    db.execute(AlertEvent.__table__.delete())
    db.commit()
    evaluate(db, TODAY, [out.id, into.id])
    db.commit()
    assert db.scalar(select(AlertEvent).where(AlertEvent.key == "big")) is None


# ------------------------------------------------------------------ the one-time pass


def test_backfill_runs_once(paid):
    client, db, chk, visa, purchase, out, into = paid
    assert db.get(AppSetting, card_payments.BACKFILL_KEY) is None  # a fresh vault hasn't run it yet
    assert card_payments.backfill_once(db) == 2
    db.commit()
    assert mark(db, out) == AUTO and mark(db, into) == AUTO
    assert db.get(AppSetting, card_payments.BACKFILL_KEY).value == "1"
    later = add_txn(db, chk.id, "2026-09-25", -50, "CARD PAYMENT", category="LOAN_PAYMENTS", plaid_detailed=CC)
    assert card_payments.backfill_once(db) == 0  # done: never again (rule runs keep marks current)
    db.commit()
    assert mark(db, later) == NONE


def test_backfill_on_unlock(paid):
    client, db, chk, visa, purchase, out, into = paid
    ok(client.post("/api/auth/lock"))
    ok(client.post("/api/auth/unlock", json={"password": PASSWORD}))
    session = client.app.state.fintrack.vault.db.acquire()
    try:
        assert session.get(AppSetting, card_payments.BACKFILL_KEY) is not None
        row = session.get(Transaction, out.id)
        assert (row.is_transfer, row.transfer_source) == AUTO
    finally:
        client.app.state.fintrack.vault.db.release(session)


def test_backfill_failure_is_retried(paid, monkeypatch, caplog):
    client, db, chk, visa, purchase, out, into = paid

    def boom(session):
        raise RuntimeError("secret detail")

    with monkeypatch.context() as m:
        m.setattr(rules_service, "apply_all", boom)
        card_payments.safe_backfill(db)
    assert db.get(AppSetting, card_payments.BACKFILL_KEY) is None
    assert "RuntimeError" in caplog.text and "secret detail" not in caplog.text
    card_payments.safe_backfill(db)
    assert db.get(AppSetting, card_payments.BACKFILL_KEY) is not None
    assert mark(db, out) == AUTO


def test_backfill_gives_up_after_repeated_failures(paid, monkeypatch, caplog):
    """A pass that keeps failing stops after BACKFILL_MAX_FAILURES unlocks (no re-run on every
    unlock); the rule run after the next sync still marks the payments."""
    client, db, chk, visa, purchase, out, into = paid
    calls = []

    def boom(session):
        calls.append(1)
        raise RuntimeError("secret detail")

    with monkeypatch.context() as m:
        m.setattr(rules_service, "apply_all", boom)
        for _ in range(card_payments.BACKFILL_MAX_FAILURES - 1):
            card_payments.safe_backfill(db)
        assert db.get(AppSetting, card_payments.BACKFILL_KEY) is None  # still retried
        assert db.get(AppSetting, card_payments.BACKFILL_FAILURES_KEY).value == str(
            card_payments.BACKFILL_MAX_FAILURES - 1)
        card_payments.safe_backfill(db)
        assert db.get(AppSetting, card_payments.BACKFILL_KEY).value == "gave_up"
        assert db.get(AppSetting, card_payments.BACKFILL_FAILURES_KEY) is None
        card_payments.safe_backfill(db)  # given up: no further attempt
    assert len(calls) == card_payments.BACKFILL_MAX_FAILURES
    assert "secret detail" not in caplog.text
    assert mark(db, out) == NONE
    run(db)  # e.g. the rule run after the next sync
    assert mark(db, out) == AUTO


def test_backfill_success_clears_the_failure_count(paid, monkeypatch):
    client, db, chk, visa, purchase, out, into = paid

    def boom(session):
        raise RuntimeError("x")

    with monkeypatch.context() as m:
        m.setattr(rules_service, "apply_all", boom)
        card_payments.safe_backfill(db)
    assert db.get(AppSetting, card_payments.BACKFILL_FAILURES_KEY).value == "1"
    card_payments.safe_backfill(db)
    assert db.get(AppSetting, card_payments.BACKFILL_KEY).value == "1"
    assert db.get(AppSetting, card_payments.BACKFILL_FAILURES_KEY) is None
    assert mark(db, out) == AUTO


# ------------------------------------------------------------------ never hide real spending
# The bank side is marked only when it pairs with the payment on one of their visible cards.
# Each of these hid real spending while the bank side could be marked without a pair.


def test_unlinked_store_card_payment_keeps_counting(unlocked, db, fixed_today):
    chk, visa = checking(db), card(db)
    add_txn(db, visa.id, "2026-09-05", -120, "Target", category="GENERAL_MERCHANDISE")
    # The user pays their linked Visa (both sides seen) ...
    visa_pay = add_txn(db, chk.id, "2026-09-20", -120, "CHASE CREDIT CRD AUTOPAY", category="LOAN_PAYMENTS",
                       plaid_detailed=CC)
    add_txn(db, visa.id, "2026-09-21", 120, "PAYMENT THANK YOU", category="LOAN_PAYMENTS", plaid_detailed=CC)
    # ... and a store card FinTrack never sees; Plaid tags that payment the same way.
    store = add_txn(db, chk.id, "2026-09-15", -400, "SYNCHRONY BANK PAYMENT", category="LOAN_PAYMENTS",
                    plaid_detailed=CC)
    run(db)
    assert mark(db, visa_pay) == AUTO
    assert mark(db, store) == NONE  # the only record of $400 of store-card spending
    home = {m["month"]: m for m in ok(unlocked.get("/api/dashboard"))["months"]}
    assert home["2026-09"]["went_out"] == 520.0  # the Visa purchase + the store card payment


def test_hidden_second_card_payment_keeps_counting(unlocked, db, fixed_today):
    chk, visa = checking(db), card(db)
    amex = card(db, "Amex", hidden=True)
    add_txn(db, visa.id, "2026-09-05", -10, "Coffee")
    add_txn(db, amex.id, "2026-09-05", -900, "Big purchase")  # hidden: not counted
    pay_amex = add_txn(db, chk.id, "2026-09-20", -900, "AMEX EPAYMENT", category="LOAN_PAYMENTS", plaid_detailed=CC)
    add_txn(db, amex.id, "2026-09-21", 900, "PAYMENT RECEIVED", category="LOAN_PAYMENTS", plaid_detailed=CC)
    run(db)
    assert mark(db, pay_amex) == NONE  # the hidden card's purchases don't count, so its payment does


def test_card_linked_after_the_bank_keeps_older_payments(unlocked, db, fixed_today):
    """Bank history from March, the card linked later (its history starts mid-August)."""
    chk = checking(db)
    old = [add_txn(db, chk.id, f"2026-0{m}-20", -300, "CARD PAYMENT", category="LOAN_PAYMENTS", plaid_detailed=CC)
           for m in (3, 4, 5, 6)]
    visa = card(db)
    add_txn(db, visa.id, "2026-08-15", -40, "Coffee")
    recent = add_txn(db, chk.id, "2026-08-20", -300, "CARD PAYMENT", category="LOAN_PAYMENTS", plaid_detailed=CC)
    add_txn(db, visa.id, "2026-08-21", 300, "PAYMENT THANK YOU", category="LOAN_PAYMENTS", plaid_detailed=CC)
    run(db)
    assert all(mark(db, t) == NONE for t in old)  # no card data those months: the payment is the spending
    assert mark(db, recent) == AUTO
    rep = {m["month"]: m for m in ok(unlocked.get("/api/reports/monthly", params={"months": 12}))["months"]}
    assert rep["2026-04"]["spending"] + rep["2026-04"]["fixed"] == 300.0


def test_store_card_names_alone_never_hide_a_payment(unlocked, db, fixed_today):
    chk, visa = checking(db), card(db)
    add_txn(db, visa.id, "2026-08-05", -10, "Coffee")
    names = ["TARGET CARD PMT", "KOHLS CARD PAYMENT", "SYNCHRONY CREDIT CARD PAYMENT", "CARDMEMBER SERV WEB PYMT",
             "PAYPAL CREDIT CARD PAYMENT"]
    rows = [add_txn(db, chk.id, "2026-08-10", -(50 + i), n, category="LOAN_PAYMENTS") for i, n in enumerate(names)]
    run(db)
    assert {r.name: mark(db, r) for r in rows} == {n: NONE for n in names}


def test_two_equal_payments_only_the_paired_one_is_marked(unlocked, db, fixed_today):
    chk, visa = checking(db), card(db)
    add_txn(db, visa.id, "2026-09-02", -100, "Coffee")
    a = add_txn(db, chk.id, "2026-09-20", -100, "CARD PAYMENT", category="LOAN_PAYMENTS", plaid_detailed=CC)
    b = add_txn(db, chk.id, "2026-09-21", -100, "CARD PAYMENT", category="LOAN_PAYMENTS", plaid_detailed=CC)
    add_txn(db, visa.id, "2026-09-21", 100, "PAYMENT THANK YOU", category="LOAN_PAYMENTS", plaid_detailed=CC)
    run(db)
    assert mark(db, b) == AUTO  # same day as the card row: the nearest pairs
    assert mark(db, a) == NONE  # the other $100 went somewhere FinTrack doesn't see


# ------------------------------------------------------------------ pairing rules


def test_pairing_is_nearest_first_and_ties_go_by_id(unlocked, db, fixed_today):
    chk, visa = checking(db), card(db)
    # Every possible pair within 5 days, nearest first: each row is used once.
    far_card = add_txn(db, visa.id, "2026-09-10", 200, "PAYMENT THANK YOU", category="LOAN_PAYMENTS",
                       plaid_detailed=CC)
    near_card = add_txn(db, visa.id, "2026-09-14", 200, "PAYMENT THANK YOU", category="LOAN_PAYMENTS",
                        plaid_detailed=CC)
    early = add_txn(db, chk.id, "2026-09-12", -200, "CARD PAYMENT", category="LOAN_PAYMENTS", plaid_detailed=CC)
    late = add_txn(db, chk.id, "2026-09-15", -200, "CARD PAYMENT", category="LOAN_PAYMENTS", plaid_detailed=CC)
    run(db)
    # 15th <-> 14th (1 day) is taken first; then 12th <-> 10th (2 days).
    assert mark(db, early) == AUTO and mark(db, late) == AUTO
    assert mark(db, far_card) == AUTO and mark(db, near_card) == AUTO
    # A tie (two bank rows equally near one card row): the lower id pairs, every time.
    t1 = add_txn(db, chk.id, "2026-08-10", -60, "CARD PAYMENT", category="LOAN_PAYMENTS", plaid_detailed=CC)
    t2 = add_txn(db, chk.id, "2026-08-12", -60, "CARD PAYMENT", category="LOAN_PAYMENTS", plaid_detailed=CC)
    add_txn(db, visa.id, "2026-08-11", 60, "PAYMENT THANK YOU", category="LOAN_PAYMENTS", plaid_detailed=CC)
    run(db)
    assert mark(db, t1) == AUTO and mark(db, t2) == NONE
    assert run(db) == 0


def test_greedy_by_date_would_pick_the_wrong_row(unlocked, db, fixed_today):
    """Bank rows on the 10th and 12th, card rows on the 12th and 6th: taking bank rows in date
    order would pair 10th <-> 12th and strand the 12th (the 6th is 6 days from it); nearest
    first pairs 12th <-> 12th, then 10th <-> 6th."""
    chk, visa = checking(db), card(db)
    b10 = add_txn(db, chk.id, "2026-09-10", -80, "CARD PAYMENT", category="LOAN_PAYMENTS", plaid_detailed=CC)
    b12 = add_txn(db, chk.id, "2026-09-12", -80, "CARD PAYMENT", category="LOAN_PAYMENTS", plaid_detailed=CC)
    add_txn(db, visa.id, "2026-09-12", 80, "PAYMENT THANK YOU", category="LOAN_PAYMENTS", plaid_detailed=CC)
    add_txn(db, visa.id, "2026-09-06", 80, "PAYMENT THANK YOU", category="LOAN_PAYMENTS", plaid_detailed=CC)
    run(db)
    assert mark(db, b10) == AUTO and mark(db, b12) == AUTO


def test_returned_payment_pairs_with_money_out_of_the_card(unlocked, db, fixed_today):
    chk, visa = checking(db), card(db)
    add_txn(db, visa.id, "2026-09-02", -30, "Coffee")
    back = add_txn(db, chk.id, "2026-09-22", 250, "CARD PAYMENT RETURNED", category="LOAN_PAYMENTS",
                   plaid_detailed=CC)
    card_back = add_txn(db, visa.id, "2026-09-21", -250, "PAYMENT REVERSAL", category="LOAN_PAYMENTS",
                        plaid_detailed=CC)
    # Money into checking with no card-side reversal stays money that came in.
    alone = add_txn(db, chk.id, "2026-09-05", 75, "CARD PAYMENT RETURNED", category="LOAN_PAYMENTS",
                    plaid_detailed=CC)
    run(db)
    assert mark(db, back) == AUTO and mark(db, card_back) == AUTO
    assert mark(db, alone) == NONE


def test_pending_card_row_pairs(unlocked, db, fixed_today):
    chk, visa = checking(db), card(db)
    out = add_txn(db, chk.id, "2026-09-24", -140, "CARD PAYMENT", category="LOAN_PAYMENTS", plaid_detailed=CC)
    pend = add_txn(db, visa.id, "2026-09-25", 140, "PAYMENT - THANK YOU", category="LOAN_PAYMENTS",
                   plaid_detailed=CC)
    db.get(Transaction, pend.id).pending = True
    db.commit()
    run(db)
    assert mark(db, out) == AUTO and mark(db, pend) == AUTO


# ------------------------------------------------------------------ through a sync


def _bank_and_card(fake_plaid, token):
    from tests.test_plaid import acct

    fake_plaid.add_item("public-bank", token, "item_bank", "First Bank")
    fake_plaid.accounts[token] = [
        acct("chk", "Checking", "depository", "checking", 5000),
        acct("visa", "Visa", "credit", "credit card", 620),
    ]


def _cc(plaid_txn: dict) -> dict:
    plaid_txn["personal_finance_category"]["detailed"] = CC
    return plaid_txn


def _big_needs(client) -> list[int]:
    return [n["data"]["transaction_id"] for n in ok(client.get("/api/dashboard"))["needs"]
            if n["kind"] == "big_purchase"]


def test_both_sides_in_one_sync_never_raise_a_large_purchase(unlocked, db, fake_plaid, fixed_today):
    """The new bank row has no id while the sync writes it; the rule run in after_sync pairs
    it before alerts run, so no "Large transaction" for the payment."""
    from tests.test_plaid import exchange, txn

    token = "access-sandbox-bank-0000-SECRET"
    _bank_and_card(fake_plaid, token)
    fake_plaid.txn_pages[token] = [
        {"cursor_in": None, "added": [txn("old", "visa", 12, "Coffee", date="2026-03-02")],
         "modified": [], "removed": [], "next_cursor": "c1", "has_more": False},
        {"cursor_in": "c1", "added": [
            _cc(txn("pay1", "chk", 900, "ONLINE PAYMENT", date="2026-09-25", category="LOAN_PAYMENTS")),
            _cc(txn("pay1c", "visa", -900, "PAYMENT - THANK YOU", date="2026-09-25", category="LOAN_PAYMENTS")),
        ], "modified": [], "removed": [], "next_cursor": "c2", "has_more": False},
    ]
    exchange(unlocked, "public-bank", "bank")
    ok(unlocked.post("/api/plaid/sync", json={}))
    db.expire_all()
    rows = {t.plaid_transaction_id: t for t in db.scalars(select(Transaction))}
    assert (rows["pay1"].is_transfer, rows["pay1"].transfer_source) == AUTO
    assert (rows["pay1c"].is_transfer, rows["pay1c"].transfer_source) == AUTO
    assert db.scalar(select(AlertEvent).where(AlertEvent.key == "big")) is None
    assert _big_needs(unlocked) == []


def test_card_side_a_sync_later_counts_until_then(unlocked, db, fake_plaid, fixed_today):
    """The bank payment arrives first: with nothing to pair it counts (and may ask "Was this
    you?"). When the card side arrives, the rule run marks it and the question goes away."""
    from tests.test_plaid import exchange, txn

    token = "access-sandbox-bank-0000-SECRET"
    _bank_and_card(fake_plaid, token)
    fake_plaid.txn_pages[token] = [
        {"cursor_in": None, "added": [txn("old", "visa", 12, "Coffee", date="2026-03-02")],
         "modified": [], "removed": [], "next_cursor": "c1", "has_more": False},
        {"cursor_in": "c1", "added": [
            _cc(txn("pay1", "chk", 900, "ONLINE PAYMENT", date="2026-09-24", category="LOAN_PAYMENTS")),
        ], "modified": [], "removed": [], "next_cursor": "c2", "has_more": False},
        {"cursor_in": "c2", "added": [
            _cc(txn("pay1c", "visa", -900, "PAYMENT - THANK YOU", date="2026-09-25", category="LOAN_PAYMENTS",
                    pending=True)),
        ], "modified": [], "removed": [], "next_cursor": "c3", "has_more": False},
    ]
    exchange(unlocked, "public-bank", "bank")
    ok(unlocked.post("/api/plaid/sync", json={}))
    db.expire_all()
    pay = db.scalar(select(Transaction).where(Transaction.plaid_transaction_id == "pay1"))
    assert mark(db, pay) == NONE
    assert _big_needs(unlocked) == [pay.id]
    home = {m["month"]: m for m in ok(unlocked.get("/api/dashboard"))["months"]}
    assert home["2026-09"]["went_out"] == 900.0
    ok(unlocked.post("/api/plaid/sync", json={}))
    assert mark(db, pay) == AUTO
    assert _big_needs(unlocked) == []
    home = {m["month"]: m for m in ok(unlocked.get("/api/dashboard"))["months"]}
    assert home["2026-09"]["went_out"] == 0.0

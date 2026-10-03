"""Release 3: balance history backfill, loan groups, payoff projections and the debt planner."""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from app.models import BalanceSnapshot
from app.services import loans, networth
from app.services.sync import upsert_snapshot
from tests.conftest import FIXED_TODAY, add_txn, make_account
from tests.test_plaid import BANK_TOKEN, acct, txn

D = dt.date


def ok(response) -> dict:
    assert response.status_code == 200, response.text
    return response.json()


def snaps(db, account_id) -> dict[str, tuple[int, bool]]:
    db.expire_all()
    return {
        s.date.isoformat(): (s.balance_cents, bool(s.estimated))
        for s in db.scalars(select(BalanceSnapshot).where(BalanceSnapshot.account_id == account_id).order_by(BalanceSnapshot.date))
    }


# ------------------------------------------------------------------ backfill


@pytest.fixture
def bank(unlocked, db, fixed_today):
    chk = make_account(db, "Checking", "bank", 1000)
    upsert_snapshot(db, chk.id, 100000, FIXED_TODAY)  # today's real balance
    for day, amount in (("2026-09-26", -50), ("2026-09-24", 200), ("2026-09-24", -30), ("2026-09-20", -100)):
        add_txn(db, chk.id, day, amount, "T")
    add_txn(db, chk.id, "2026-09-25", -999, "Pending", pending=True)
    db.commit()
    return chk


def test_backfill_bank_math(bank, db):
    networth.backfill(db, FIXED_TODAY)
    db.commit()
    # balance(d-1) = balance(d) - Σ amounts on d; pending ignored; nothing before Sep 20.
    assert snaps(db, bank.id) == {
        "2026-09-20": (88000, True), "2026-09-21": (88000, True), "2026-09-22": (88000, True),
        "2026-09-23": (88000, True), "2026-09-24": (105000, True), "2026-09-25": (105000, True),
        "2026-09-26": (100000, False),
    }
    # Idempotent.
    networth.backfill(db, FIXED_TODAY)
    db.commit()
    assert len(snaps(db, bank.id)) == 7


def test_backfill_credit_math(unlocked, db, fixed_today):
    card = make_account(db, "Visa", "credit", 500)
    upsert_snapshot(db, card.id, 50000, FIXED_TODAY)
    for day, amount in (("2026-09-26", -25), ("2026-09-22", 100), ("2026-09-21", -40)):
        add_txn(db, card.id, day, amount, "T")
    db.commit()
    networth.backfill(db, FIXED_TODAY)
    db.commit()
    # owed(d-1) = owed(d) + Σ amounts on d: a purchase (negative) raised what's owed.
    assert snaps(db, card.id) == {
        "2026-09-21": (57500, True), "2026-09-22": (47500, True), "2026-09-23": (47500, True),
        "2026-09-24": (47500, True), "2026-09-25": (47500, True), "2026-09-26": (50000, False),
    }


def test_backfill_never_overwrites_real_snapshots(bank, db):
    upsert_snapshot(db, bank.id, 99900, D(2026, 9, 23))  # a real, recorded balance
    db.commit()
    networth.backfill(db, FIXED_TODAY)
    db.commit()
    # Only dates before the earliest real snapshot are estimated, walked back from that
    # snapshot (not from today), so the line meets it without a jump.
    assert snaps(db, bank.id) == {
        "2026-09-20": (99900, True), "2026-09-21": (99900, True), "2026-09-22": (99900, True),
        "2026-09-23": (99900, False), "2026-09-26": (100000, False),
    }
    # A later real snapshot for an estimated date replaces it, and becomes the new anchor.
    upsert_snapshot(db, bank.id, 77700, D(2026, 9, 21))
    db.commit()
    assert snaps(db, bank.id)["2026-09-21"] == (77700, False)
    networth.backfill(db, FIXED_TODAY)
    db.commit()
    assert snaps(db, bank.id) == {"2026-09-20": (77700, True), "2026-09-21": (77700, False),
                                  "2026-09-23": (99900, False), "2026-09-26": (100000, False)}


def test_backfill_anchor_subtracts_the_snapshot_days_own_transactions(bank, db):
    # The anchor is an end-of-day balance: Sep 24's +$200 and -$30 are already in it.
    upsert_snapshot(db, bank.id, 99000, D(2026, 9, 24))
    db.commit()
    networth.backfill(db, FIXED_TODAY)
    db.commit()
    assert snaps(db, bank.id) == {
        "2026-09-20": (82000, True), "2026-09-21": (82000, True), "2026-09-22": (82000, True),
        "2026-09-23": (82000, True), "2026-09-24": (99000, False), "2026-09-26": (100000, False),
    }


def test_backfill_skips_hidden_other_categories_and_is_capped(unlocked, db, fixed_today):
    hidden = make_account(db, "Hidden", "bank", 10, hidden=True)
    hsa = make_account(db, "HSA", "hsa", 10)
    old = make_account(db, "Old bank", "bank", 10)
    for a in (hidden, hsa, old):
        upsert_snapshot(db, a.id, 1000, FIXED_TODAY)
        add_txn(db, a.id, "2024-01-01", -1, "Ancient")
    db.commit()
    networth.backfill(db, FIXED_TODAY)
    db.commit()
    assert len(snaps(db, hidden.id)) == 1 and len(snaps(db, hsa.id)) == 1
    estimated = [d for d, (_, est) in snaps(db, old.id).items() if est]
    # At most 730 days back: 2024-09-26 .. 2026-09-25.
    assert (len(estimated), min(estimated), max(estimated)) == (730, "2024-09-26", "2026-09-25")


def test_backfill_removes_stale_estimates(bank, db):
    networth.backfill(db, FIXED_TODAY)
    db.commit()
    from app.models import Transaction
    for t in db.scalars(select(Transaction).where(Transaction.account_id == bank.id)):
        db.delete(t)
    db.commit()
    networth.backfill(db, FIXED_TODAY)
    db.commit()
    assert snaps(db, bank.id) == {"2026-09-26": (100000, False)}


def test_history_endpoints_flag_estimated_points(bank, db, unlocked):
    card = make_account(db, "Visa", "credit", 100)
    upsert_snapshot(db, card.id, 10000, D(2026, 9, 22))
    db.commit()
    networth.backfill(db, FIXED_TODAY)
    db.commit()
    history = ok(unlocked.get(f"/api/accounts/{bank.id}/history", params={"days": 3}))
    assert history == [
        {"date": "2026-09-24", "balance": 1050.0, "estimated": True},
        {"date": "2026-09-25", "balance": 1050.0, "estimated": True},
        {"date": "2026-09-26", "balance": 1000.0, "estimated": False},
    ]
    points = {p["date"]: p for p in ok(unlocked.get("/api/networth/history", params={"days": 10}))}
    assert points["2026-09-20"] == {"date": "2026-09-20", "assets": 880.0, "liabilities": 0.0, "net_worth": 880.0,
                                    "estimated": True}
    assert points["2026-09-22"] == {"date": "2026-09-22", "assets": 880.0, "liabilities": 100.0, "net_worth": 780.0,
                                    "estimated": True}  # the card is real, the bank is estimated
    assert points["2026-09-26"]["estimated"] is False


def test_sync_backfills_history(unlocked, fake_plaid, fixed_today):
    c = unlocked
    fake_plaid.add_item("public-bank", BANK_TOKEN, "item_bank", "First Bank")
    fake_plaid.accounts[BANK_TOKEN] = [acct("chk", "Checking", "depository", "checking", 1000)]
    fake_plaid.txn_pages[BANK_TOKEN] = [
        {"cursor_in": None, "added": [txn("t1", "chk", 100, "Rent", date="2026-09-24"),
                                      txn("t2", "chk", -40, "Refund", date="2026-09-25"),
                                      txn("t3", "chk", 5, "Pending", date="2026-09-26", pending=True)],
         "modified": [], "removed": [], "next_cursor": "c1", "has_more": False},
    ]
    item = c.post("/api/plaid/exchange", json={"public_token": "public-bank", "kind": "bank"}).json()["item"]
    ok(c.post(f"/api/plaid/items/{item['id']}/import", json={"plaid_account_ids": ["chk"]}))
    account_id = ok(c.get("/api/accounts"))[0]["id"]
    # Today 1000 (real; the pending one is ignored). Sep 25: 1000. Sep 24: 1000 - 40 = 960, the
    # earliest posted date (t1 is on the 24th).
    assert ok(c.get(f"/api/accounts/{account_id}/history", params={"days": 5})) == [
        {"date": "2026-09-24", "balance": 960.0, "estimated": True},
        {"date": "2026-09-25", "balance": 1000.0, "estimated": True},
        {"date": "2026-09-26", "balance": 1000.0, "estimated": False},
    ]


# ------------------------------------------------------------------ loan groups


def test_loan_groups(unlocked, db, fixed_today):
    c = unlocked
    kinds = {
        "Visa": ("credit", "credit card"), "Car": ("loan", "auto"), "School": ("loan", "student"),
        "House": ("loan", "mortgage"), "HELOC": ("loan", "home equity"), "Boat": ("loan", "consumer"),
    }
    for name, (category, subtype) in kinds.items():
        make_account(db, name, category, 100, plaid_type=category, plaid_subtype=subtype)
    make_account(db, "IOU", "loan", 50)
    make_account(db, "Store card", "credit", 50)
    chk = make_account(db, "Checking", "bank", 50)
    groups = {a["name"]: a["loan_group"] for a in ok(c.get("/api/accounts"))}
    assert groups == {"Visa": "Credit cards", "Car": "Car loans", "School": "Student loans", "House": "Home loans",
                      "HELOC": "Home loans", "Boat": "Other loans", "IOU": "Other loans",
                      "Store card": "Credit cards", "Checking": None}

    iou = next(a for a in ok(c.get("/api/accounts")) if a["name"] == "IOU")
    assert ok(c.patch(f"/api/accounts/{iou['id']}", json={"loan_group": "  Family   loans "}))["loan_group"] == "Family loans"
    assert ok(c.patch(f"/api/accounts/{iou['id']}", json={"loan_group": None}))["loan_group"] == "Other loans"
    assert c.patch(f"/api/accounts/{iou['id']}", json={"loan_group": "x" * 41}).status_code == 422
    assert c.patch(f"/api/accounts/{iou['id']}", json={"loan_group": "   "}).status_code == 422
    r = c.patch(f"/api/accounts/{chk.id}", json={"loan_group": "Nope"})
    assert r.status_code == 422 and r.json()["detail"] == "Only loans and credit cards have a loan group."
    # A custom label on an account that becomes an asset is hidden (null), and returns if it's a liability again.
    ok(c.patch(f"/api/accounts/{iou['id']}", json={"loan_group": "Family"}))
    assert ok(c.patch(f"/api/accounts/{iou['id']}", json={"category": "other"}))["loan_group"] is None
    assert ok(c.patch(f"/api/accounts/{iou['id']}", json={"category": "loan"}))["loan_group"] == "Family"


# ------------------------------------------------------------------ payoff


def payoff(c, account_id, **q):
    return c.get(f"/api/loans/{account_id}/payoff", params=q)


def test_payoff_apr_zero_and_extra(unlocked, db, fixed_today):
    loan = make_account(db, "IOU", "loan", 1000, minimum_payment_cents=10000)
    p = ok(payoff(unlocked, loan.id))
    assert {k: p[k] for k in ("account_id", "name", "balance", "apr", "minimum", "extra", "months", "payoff_date",
                              "total_interest", "never")} == {
        "account_id": loan.id, "name": "IOU", "balance": 1000.0, "apr": 0.0, "minimum": 100.0, "extra": 0.0,
        "months": 10, "payoff_date": "2027-07-01", "total_interest": 0.0, "never": False}
    assert p["schedule"][0] == {"month": "2026-10", "balance": 900.0, "interest": 0.0, "principal": 100.0}
    assert p["schedule"][-1] == {"month": "2027-07", "balance": 0.0, "interest": 0.0, "principal": 100.0}
    assert p["baseline"] == {"months": 10, "payoff_date": "2027-07-01", "total_interest": 0.0, "never": False}

    p = ok(payoff(unlocked, loan.id, extra=50))
    # 6 x 150 = 900, then the last 100.
    assert (p["months"], p["payoff_date"], p["schedule"][-1]["principal"]) == (7, "2027-04-01", 100.0)
    assert p["baseline"]["months"] == 10


def test_payoff_with_interest(unlocked, db, fixed_today):
    card = make_account(db, "Visa", "credit", 1000, interest_rate=12.0, minimum_payment_cents=10000)
    p = ok(payoff(unlocked, card.id))
    # 1% a month on the balance, rounded to the cent each month (hand-computed).
    assert [s["interest"] for s in p["schedule"]] == [10.0, 9.1, 8.19, 7.27, 6.35, 5.41, 4.46, 3.51, 2.54, 1.57, 0.58]
    assert [s["balance"] for s in p["schedule"]] == [
        910.0, 819.1, 727.29, 634.56, 540.91, 446.32, 350.78, 254.29, 156.83, 58.4, 0.0]
    assert p["schedule"][-1]["principal"] == 58.4
    assert (p["months"], p["payoff_date"], p["total_interest"], p["never"]) == (11, "2027-08-01", 58.98, False)
    assert len(p["history"]) == 0


def test_payoff_never(unlocked, db, fixed_today):
    card = make_account(db, "Visa", "credit", 1000, interest_rate=24.0, minimum_payment_cents=2000)
    p = ok(payoff(unlocked, card.id))  # $20 minimum == $20 first-month interest
    assert (p["months"], p["payoff_date"], p["never"]) == (None, None, True)
    assert p["baseline"]["never"] is True
    better = ok(payoff(unlocked, card.id, extra=80))
    assert better["never"] is False and better["months"] is not None and better["baseline"]["never"] is True
    # 600-month cap: $1 a month on $1,000 at 0% would take 1000 months.
    slow = make_account(db, "IOU", "loan", 1000, minimum_payment_cents=100)
    p = ok(payoff(unlocked, slow.id))
    assert (p["never"], p["months"], len(p["schedule"])) == (True, None, 600)
    assert p["schedule"][-1] == {"month": "2076-09", "balance": 400.0, "interest": 0.0, "principal": 1.0}


def test_payoff_validation_and_minimum_override(unlocked, db, fixed_today):
    c = unlocked
    chk = make_account(db, "Checking", "bank", 100)
    loan = make_account(db, "IOU", "loan", 300)
    assert payoff(c, chk.id).status_code == 422
    r = payoff(c, loan.id)
    assert r.status_code == 422 and "no minimum payment" in r.json()["detail"]
    assert ok(payoff(c, loan.id, minimum=100))["months"] == 3
    assert payoff(c, 99999).status_code == 404
    for q in ({"extra": -1}, {"extra": "nan"}, {"extra": "inf"}, {"minimum": -5}, {"minimum": "nan"}, {"extra": 1e13}):
        assert payoff(c, loan.id, **({"minimum": 100} | q)).status_code == 422, q
    paid = make_account(db, "Paid", "credit", 0, minimum_payment_cents=2500)
    p = ok(payoff(c, paid.id))
    assert (p["months"], p["payoff_date"], p["schedule"], p["never"]) == (0, "2026-09-01", [], False)


def test_payoff_includes_history(unlocked, db, fixed_today):
    loan = make_account(db, "IOU", "loan", 900, minimum_payment_cents=10000)
    upsert_snapshot(db, loan.id, 100000, D(2026, 8, 1))
    upsert_snapshot(db, loan.id, 90000, FIXED_TODAY)
    db.commit()
    assert ok(payoff(unlocked, loan.id))["history"] == [
        {"date": "2026-08-01", "balance": 1000.0, "estimated": False},
        {"date": "2026-09-26", "balance": 900.0, "estimated": False},
    ]


def test_monthly_interest_is_exact():
    assert loans.monthly_interest(100000, 12.0) == 1000
    assert loans.monthly_interest(63456, 12.0) == 635  # 634.56
    assert loans.monthly_interest(5, 30.0) == 0  # 0.125 cents
    assert loans.monthly_interest(20, 30.0) == 1  # 0.5 cents rounds half up
    assert loans.monthly_interest(123456789, 19.99) == 2056584  # 2,056,584.0...
    assert loans.monthly_interest(0, 24) == 0 and loans.monthly_interest(100, 0) == 0


# ------------------------------------------------------------------ debt plan


@pytest.fixture
def zero_apr(unlocked, db, fixed_today):
    small = make_account(db, "Small", "credit", 300, minimum_payment_cents=5000, interest_rate=0.0)
    big = make_account(db, "Big", "loan", 1000, minimum_payment_cents=10000, interest_rate=0.0)
    # Not debts in the default plan: hidden, paid off, no minimum.
    make_account(db, "Hidden", "loan", 500, minimum_payment_cents=100, hidden=True)
    make_account(db, "Paid", "credit", 0, minimum_payment_cents=100)
    make_account(db, "No min", "loan", 500)
    return unlocked, small, big


def plan(c, **body):
    return c.post("/api/debt/plan", json=body)


def test_snowball_rolls_minimums_over(zero_apr):
    c, small, big = zero_apr
    p = ok(plan(c, extra=100, strategy="snowball"))
    # Budget 250/month: Small (300) gets its 50 + the 100 extra; once it's gone its 50 rolls over.
    assert [t["total"] for t in p["timeline"]] == [1050.0, 800.0, 550.0, 300.0, 50.0, 0.0]
    assert p["timeline"][0] == {"month": "2026-10", "total": 1050.0, "interest": 0.0,
                                "by_account": {str(small.id): 150.0, str(big.id): 900.0}}
    assert (p["months"], p["payoff_date"], p["total_interest"], p["never"]) == (6, "2027-03-01", 0.0, False)
    debts = {d["name"]: d for d in p["debts"]}
    assert [d["name"] for d in p["debts"]] == ["Small", "Big"]
    assert debts["Small"] == {"account_id": small.id, "name": "Small", "loan_group": "Credit cards", "balance": 300.0,
                              "apr": 0.0, "minimum": 50.0, "payoff_months": 2, "payoff_date": "2026-11-01",
                              "interest": 0.0,
                              # Release 3.10 (additive)
                              "kind": "card", "apr_known": True, "baseline_months": 6,
                              "baseline_date": "2027-03-01"}
    assert (debts["Big"]["payoff_months"], debts["Big"]["loan_group"]) == (6, "Other loans")
    assert p["compare"] == {
        "avalanche": {"months": 6, "total_interest": 0.0},  # equal APRs: smaller balance first, same plan
        "snowball": {"months": 6, "total_interest": 0.0},
        "minimums_only": {"months": 10, "total_interest": 0.0},
    }
    assert (p["strategy"], p["extra"]) == ("snowball", 100.0)


def test_custom_order(zero_apr):
    c, small, big = zero_apr
    p = ok(plan(c, extra=100, strategy="custom", order=[big.id, small.id]))
    assert [d["name"] for d in p["debts"]] == ["Big", "Small"]
    debts = {d["name"]: d for d in p["debts"]}
    assert (debts["Big"]["payoff_months"], debts["Small"]["payoff_months"], p["months"]) == (5, 6, 6)
    assert [t["by_account"][str(big.id)] for t in p["timeline"]] == [800.0, 600.0, 400.0, 200.0, 0.0, 0.0]
    # Debts missing from the order go last (highest APR first).
    assert [d["name"] for d in ok(plan(c, extra=0, strategy="custom", order=[big.id]))["debts"]] == ["Big", "Small"]


def test_plan_validation(zero_apr, db):
    c, small, big = zero_apr
    chk = make_account(db, "Checking", "bank", 100)
    no_min = make_account(db, "No minimum", "credit", 100)
    assert plan(c, extra=0, strategy="custom").status_code == 422
    assert plan(c, extra=0, strategy="custom", order=[small.id, small.id]).status_code == 422
    assert plan(c, extra=0, strategy="custom", order=[chk.id]).status_code == 422
    assert plan(c, extra=0, strategy="snowball", account_ids=[chk.id]).status_code == 422
    assert plan(c, extra=0, strategy="snowball", account_ids=[no_min.id]).status_code == 422
    assert plan(c, extra=0, strategy="snowball", account_ids=[99999]).status_code == 422
    assert plan(c, extra=-1, strategy="snowball").status_code == 422
    assert plan(c, extra=0, strategy="fastest").status_code == 422
    assert plan(c, extra=0, strategy="snowball", x=1).status_code == 422
    assert plan(c, extra=0, strategy="snowball", account_ids=list(range(1, 102))).status_code == 422
    r = c.post("/api/debt/plan", content='{"extra": NaN, "strategy": "snowball"}', headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    only = ok(plan(c, extra=0, strategy="avalanche", account_ids=[big.id]))
    assert [d["name"] for d in only["debts"]] == ["Big"] and only["months"] == 10
    none = ok(plan(c, extra=0, strategy="avalanche", account_ids=[]))
    assert (none["months"], none["debts"], none["timeline"], none["never"]) == (0, [], [], False)


@pytest.fixture
def interest(unlocked, db, fixed_today):
    a = make_account(db, "Card A", "credit", 1000, interest_rate=24.0, minimum_payment_cents=5000)
    b = make_account(db, "Loan B", "loan", 500, interest_rate=6.0, minimum_payment_cents=5000)
    cc = make_account(db, "Card C", "credit", 2000, interest_rate=24.0, minimum_payment_cents=10000)
    return unlocked, a, b, cc


def test_avalanche_and_snowball_order(interest):
    c, a, b, cc = interest
    av = ok(plan(c, extra=100, strategy="avalanche"))
    # Highest APR first; the 24% tie goes to the smaller balance.
    assert [d["account_id"] for d in av["debts"]] == [a.id, cc.id, b.id]
    # Month 1: interest 20 / 2.50 / 40; minimums 50 / 50 / 100; the 100 extra goes to Card A.
    assert av["timeline"][0]["by_account"] == {str(a.id): 870.0, str(cc.id): 1940.0, str(b.id): 452.5}
    assert av["timeline"][0]["total"] == 3262.5
    sb = ok(plan(c, extra=100, strategy="snowball"))
    assert [d["account_id"] for d in sb["debts"]] == [b.id, a.id, cc.id]
    assert sb["timeline"][0]["by_account"] == {str(b.id): 352.5, str(a.id): 970.0, str(cc.id): 1940.0}
    # The compare block matches each strategy's own plan.
    assert av["compare"]["avalanche"] == {"months": av["months"], "total_interest": av["total_interest"]}
    assert av["compare"]["snowball"] == {"months": sb["months"], "total_interest": sb["total_interest"]}
    assert av["compare"] == sb["compare"]
    assert av["total_interest"] <= sb["total_interest"]
    mins = av["compare"]["minimums_only"]
    assert mins["total_interest"] > av["total_interest"] and mins["months"] > av["months"]
    # Payoff months are consistent with the timeline.
    for d in av["debts"]:
        k = d["payoff_months"]
        assert av["timeline"][k - 1]["by_account"][str(d["account_id"])] == 0.0
        assert av["timeline"][k - 2]["by_account"][str(d["account_id"])] > 0.0
    assert sum(d["interest"] for d in av["debts"]) == pytest.approx(av["total_interest"])
    # More extra is never slower.
    assert ok(plan(c, extra=500, strategy="avalanche"))["months"] < av["months"]


def test_plan_never(unlocked, db, fixed_today):
    make_account(db, "Card", "credit", 10000, interest_rate=24.0, minimum_payment_cents=1000)
    p = ok(plan(unlocked, extra=0, strategy="avalanche"))
    assert (p["months"], p["payoff_date"], p["never"]) == (None, None, True)
    assert p["debts"][0]["payoff_months"] is None and len(p["timeline"]) == 600
    assert p["compare"]["minimums_only"]["months"] is None

"""Release 3.10: the Paying off debt tab's plan additions (POST /api/debt/plan: kind, apr_known,
lump, per-debt baseline, interest this month / 12 months, timeline interest, baseline timeline,
skipped) and GET /api/debt/progress. Today is 2026-09-26."""
from __future__ import annotations

import datetime as dt
import json

import pytest

from app.models import AppSetting, BalanceSnapshot
from app.services import loans
from tests.conftest import make_account

D = dt.date.fromisoformat


def ok(response) -> dict:
    assert response.status_code == 200, response.text
    return response.json()


def plan(c, **body):
    return c.post("/api/debt/plan", json=body)


def progress(c, ids=None):
    return c.get("/api/debt/progress", params={"ids": ids} if ids is not None else None)


@pytest.fixture
def zero(unlocked, db, fixed_today):
    """0% debts, so every number is easy by hand."""
    small = make_account(db, "Small", "credit", 300, minimum_payment_cents=5000, interest_rate=0.0)
    big = make_account(db, "Big", "loan", 1000, minimum_payment_cents=10000, interest_rate=0.0,
                       plaid_subtype="auto")
    return unlocked, db, small, big


# ------------------------------------------------------------------ plan additions


def test_kinds_skipped_and_mortgages(unlocked, db, fixed_today):
    card = make_account(db, "Card", "credit", 1200, minimum_payment_cents=4000, interest_rate=24.0)
    home = make_account(db, "Home", "loan", 200000, minimum_payment_cents=150000, interest_rate=6.0,
                        plaid_subtype="mortgage")
    heloc = make_account(db, "Heloc", "loan", 9000, minimum_payment_cents=20000, interest_rate=8.0,
                         loan_group="Home loans")
    student = make_account(db, "Student", "loan", 5000, minimum_payment_cents=10000, plaid_subtype="student")
    nomin = make_account(db, "No min", "loan", 700)
    make_account(db, "Hidden", "loan", 500, minimum_payment_cents=100, hidden=True)
    make_account(db, "Paid", "credit", 0)
    p = ok(plan(unlocked, strategy="avalanche"))
    kinds = {d["account_id"]: d["kind"] for d in p["debts"]}
    assert kinds == {card.id: "card", home.id: "mortgage", heloc.id: "mortgage", student.id: "loan"}
    assert p["skipped"] == [{"account_id": nomin.id, "name": "No min", "reason": "no_minimum"}]
    known = {d["account_id"]: d["apr_known"] for d in p["debts"]}
    assert known[student.id] is False and known[card.id] is True
    assert next(d for d in p["debts"] if d["account_id"] == student.id)["apr"] == 0.0
    # This month's interest at today's balances: 24.00 + 1000.00 + 60.00 + 0.
    assert p["interest_this_month"] == 1084.0
    # With account ids: nothing is skipped; a debt without a minimum is still refused.
    assert ok(plan(unlocked, strategy="avalanche", account_ids=[card.id]))["skipped"] == []
    assert plan(unlocked, strategy="avalanche", account_ids=[nomin.id]).status_code == 422


def test_timeline_interest_and_12_months(unlocked, db, fixed_today):
    make_account(db, "Card", "credit", 3000, minimum_payment_cents=10000, interest_rate=18.0)
    make_account(db, "Loan", "loan", 2000, minimum_payment_cents=10000, interest_rate=6.0)
    p = ok(plan(unlocked, strategy="avalanche", extra=50))
    months = p["timeline"]
    assert months[0]["interest"] == 55.0  # 45.00 + 10.00
    assert round(sum(m["interest"] for m in months), 2) == p["total_interest"]
    assert p["interest_12"] == round(sum(m["interest"] for m in months[:12]), 2)
    assert p["interest_this_month"] == 55.0


def test_per_debt_baseline_and_baseline_timeline(zero):
    c, _db, small, big = zero
    p = ok(plan(c, strategy="snowball", extra=100))
    by = {d["account_id"]: d for d in p["debts"]}
    # Minimums only: Small 300 / 50 = 6 months, Big 1000 / 100 = 10 months.
    assert (by[small.id]["baseline_months"], by[small.id]["baseline_date"]) == (6, "2027-03-01")
    assert (by[big.id]["baseline_months"], by[big.id]["baseline_date"]) == (10, "2027-07-01")
    assert p["baseline"]["months"] == 10 and p["baseline"]["never"] is False
    assert p["baseline"]["payoff_date"] == "2027-07-01" and p["baseline"]["total_interest"] == 0.0
    assert p["compare"]["minimums_only"] == {"months": 10, "total_interest": 0.0}
    totals = [t["total"] for t in p["baseline"]["timeline"]]
    assert totals == [1150.0, 1000.0, 850.0, 700.0, 550.0, 400.0, 300.0, 200.0, 100.0, 0.0]
    assert p["baseline"]["timeline"][0]["month"] == "2026-10"
    assert p["lump"] is None


def test_lump_goes_to_the_plan_order_first(zero):
    c, _db, small, big = zero
    base = ok(plan(c, strategy="snowball", extra=100))
    p = ok(plan(c, strategy="snowball", extra=100, lump={"amount": 400}))
    # The top-level numbers stay the plan without the lump.
    assert (p["months"], p["timeline"]) == (base["months"], base["timeline"])
    lump = p["lump"]
    # Small (first in snowball order) takes 300, the rest (100) goes to Big.
    assert lump["applied"] == [{"account_id": small.id, "amount": 300.0}, {"account_id": big.id, "amount": 100.0}]
    assert lump["paid_off"] == [small.id] and lump["account_id"] is None and lump["amount"] == 400.0
    # Big 900 with 250 a month: 4 months.
    assert (lump["months"], lump["payoff_date"], lump["never"], lump["total_interest"]) == (4, "2027-01-01", False, 0.0)


def test_lump_on_a_chosen_debt(zero):
    c, _db, small, big = zero
    lump = ok(plan(c, strategy="snowball", extra=0, lump={"amount": 500, "account_id": big.id}))["lump"]
    assert lump["applied"] == [{"account_id": big.id, "amount": 500.0}] and lump["paid_off"] == []
    everything = ok(plan(c, strategy="snowball", lump={"amount": 5000, "account_id": big.id}))["lump"]
    assert everything["applied"] == [{"account_id": big.id, "amount": 1000.0}, {"account_id": small.id, "amount": 300.0}]
    assert everything["months"] == 0 and sorted(everything["paid_off"]) == sorted([small.id, big.id])


def test_lump_refusals(zero):
    c, db, small, _big = zero
    other = make_account(db, "Other card", "credit", 100, minimum_payment_cents=2500)
    assert plan(c, strategy="avalanche", account_ids=[small.id], lump={"amount": 10, "account_id": other.id}).status_code == 422
    for bad in ({"amount": 0}, {"amount": -5}, {"amount": 10, "extra": 1}, {"amount": "NaN"},
                {"amount": 10**13}, {"account_id": small.id}, {"amount": 10, "account_id": 0}):
        assert plan(c, strategy="avalanche", lump=bad).status_code == 422, bad


def test_debt_kind_helper(db, unlocked):
    card = make_account(db, "Card", "credit", 1)
    assert loans.debt_kind(card) == "card"
    assert loans.debt_kind(make_account(db, "M", "loan", 1, plaid_subtype=" Mortgage ")) == "mortgage"
    assert loans.debt_kind(make_account(db, "Car", "loan", 1, plaid_subtype="auto")) == "loan"


# ------------------------------------------------------------------ progress


def snap(db, account, day, balance, estimated=False):
    db.add(BalanceSnapshot(account_id=account.id, date=D(day), balance_cents=round(balance * 100), estimated=estimated))
    db.commit()


@pytest.fixture
def hist(unlocked, db, fixed_today):
    card = make_account(db, "Card", "credit", 700, minimum_payment_cents=2500, interest_rate=20.0)
    loan = make_account(db, "Car loan", "loan", 4800, minimum_payment_cents=20000, interest_rate=5.0)
    snap(db, card, "2026-06-15", 1000, estimated=True)
    snap(db, card, "2026-08-20", 800)  # nothing in July: June's balance carries over (estimated)
    snap(db, loan, "2026-08-01", 5000)
    return unlocked, db, card, loan


def test_progress_honest_months_only(hist):
    c, _db, card, loan = hist
    p = ok(progress(c, f"{card.id},{loan.id}"))
    # The loan's history starts in August: only August and September have every debt.
    assert p["months"] == [
        {"month": "2026-08", "total": 5800.0, "estimated": False},
        {"month": "2026-09", "total": 5500.0, "estimated": False},  # today's balances
    ]
    assert (p["since"], p["change"], p["missing"]) == ("2026-08", -300.0, ["Card", "Car loan"])
    only_card = ok(progress(c, str(card.id)))
    assert only_card["months"] == [
        {"month": "2026-06", "total": 1000.0, "estimated": True},  # a backfilled estimate
        {"month": "2026-07", "total": 1000.0, "estimated": True},  # carried over from June
        {"month": "2026-08", "total": 800.0, "estimated": False},
        {"month": "2026-09", "total": 700.0, "estimated": False},
    ]
    assert (only_card["since"], only_card["change"]) == ("2026-06", -300.0)


def test_progress_without_history(unlocked, db, fixed_today):
    card = make_account(db, "Card", "credit", 250)
    p = ok(progress(unlocked, str(card.id)))
    assert p["months"] == [{"month": "2026-09", "total": 250.0, "estimated": False}]
    assert (p["since"], p["change"], p["missing"]) == ("2026-09", None, ["Card"])
    empty = ok(progress(unlocked))
    assert empty["months"] and ok(progress(unlocked, str(card.id)))["cards"][0]["limit"] is None


def test_progress_full_window(unlocked, db, fixed_today):
    loan = make_account(db, "Loan", "loan", 900)
    snap(db, loan, "2025-01-01", 2000)
    p = ok(progress(unlocked, str(loan.id)))
    assert len(p["months"]) == 12 and p["since"] == "2025-10" and p["missing"] == []
    assert all(m["estimated"] for m in p["months"][:-1]) and p["change"] == -1100.0


def test_progress_cards_and_limits(hist):
    c, db, card, loan = hist
    other = make_account(db, "Store card", "credit", 90)
    db.add(AppSetting(key="account_facts", value=json.dumps({
        str(card.id): {"bank_name": "Bank card", "limit_cents": 500000},
        str(other.id): {"bank_name": "Store", "limit_cents": None},
        "x": {"limit_cents": 5}, "99": "junk",
    })))
    db.commit()
    p = ok(progress(c, f"{card.id},{loan.id},{other.id}"))
    assert p["cards"] == [
        {"account_id": card.id, "name": "Card", "balance": 700.0, "limit": 5000.0, "used_pct": 14},
        {"account_id": other.id, "name": "Store card", "balance": 90.0, "limit": None, "used_pct": None},
    ]
    row = db.get(AppSetting, "account_facts")
    for junk in ("not json", "[1,2]", json.dumps({str(card.id): {"limit_cents": True}}),
                 json.dumps({str(card.id): {"limit_cents": -5}})):
        row.value = junk
        db.commit()
        assert ok(progress(c, str(card.id)))["cards"][0]["limit"] is None


def test_progress_default_is_every_visible_debt(hist):
    c, db, card, loan = hist
    make_account(db, "Hidden", "loan", 5, hidden=True)
    make_account(db, "Checking", "bank", 5)
    assert ok(progress(c))["months"][-1]["total"] == 5500.0


def test_progress_refusals(hist):
    c, db, card, _loan = hist
    chk = make_account(db, "Checking", "bank", 5)
    for bad in ("", "abc", "1,,2", "-1", "0", "1.5", ",".join(["1"] * 101), "²", str(2**63), f"{chk.id}", "999999"):
        assert progress(c, bad).status_code == 422, bad


def test_new_routes_need_a_session(client):
    for method, url, body in (
        ("GET", "/api/debt/progress", None), ("GET", "/api/debt/budget", None),
        ("PUT", "/api/debt/budget", {"extra": 1, "strategy": "avalanche", "account_ids": [1]}),
        ("DELETE", "/api/debt/budget", None), ("POST", "/api/debt/budget/undo", {"token": "abcdefgh"}),
    ):
        assert client.request(method, url, json=body).status_code == 401, (method, url)

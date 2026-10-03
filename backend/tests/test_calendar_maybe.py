"""Release 3.19: suggested ("Maybe") bills on the calendar (`ForecastCalendar.maybe`).

Maybe rows are display only: never in days/months/first_dip/series/occurrences, reminders,
low_ahead, Home or the Budget screen's bills."""
from __future__ import annotations

import datetime as dt

import pytest

from app.services import calendar
from tests.conftest import add_txn, make_account
from tests.test_dashboard_v2 import dash, needs_of
from tests.test_r36_calendar import TODAY, base, cal, keyed, make_item, ok, run  # noqa: F401 - fixture

D = dt.date.fromisoformat
FROM, TO = "2026-09-01", "2026-10-31"
PROJECTION = ("days", "months", "first_dip", "series", "occurrences", "balance", "daily_spend")


def maybe_keys(body: dict) -> list[str]:
    return [o["key"] for o in body["maybe"]]


@pytest.fixture
def setup(base):
    db = base.db
    items = {
        "rent": make_item(db, "Rent", -1500, "monthly", "2026-10-01", account=base.chk, source="manual"),
        "flix": make_item(db, "Netflix", -15.49, "monthly", "2026-09-28", account=base.chk, status="suggested",
                          reminder=3),
        "spot": make_item(db, "Spotify", -10.99, "monthly", "2026-10-03", account=base.card, status="suggested"),
        "gig": make_item(db, "Side gig", 300, "monthly", "2026-10-05", status="suggested"),
        "other": make_item(db, "Savings fee", -20, "monthly", "2026-09-29", account=base.savings,
                           status="suggested"),
    }
    return base, items


def test_suggested_items_are_in_maybe_only(setup):
    base, items = setup
    body = cal(base.client, start=FROM, end=TO)
    flix, spot, gig = items["flix"].id, items["spot"].id, items["gig"].id
    # From today on (not before), within [from, to]; sorted like occurrences (date, money in first, name).
    assert maybe_keys(body) == [
        f"r{flix}:2026-09-28", f"r{spot}:2026-10-03", f"r{gig}:2026-10-05", f"r{flix}:2026-10-28"]
    assert body["maybe"][0] == {
        "key": f"r{flix}:2026-09-28", "series": f"r{flix}", "recurring_id": flix, "plan_category": None,
        "category_id": None, "base_date": "2026-09-28", "date": "2026-09-28", "name": "Netflix",
        "amount": -15.49, "kind": "out", "status": "upcoming", "counted": False, "counted_on": None,
        "carried": False, "actual": None, "override": None, "movable": False, "next_in_series": False,
        "reminder_days": 0, "maybe": True,
    }
    assert body["maybe"][1]["kind"] == "card" and body["maybe"][2]["kind"] == "in"
    # Suggested on another bank account: not on the calendar.
    assert not any(o["recurring_id"] == items["other"].id for o in body["maybe"])
    # Never in the other arrays; every normal row says maybe: false.
    suggested = {f"r{i.id}" for k, i in items.items() if k != "rent"}
    assert not any(o["series"] in suggested for o in body["occurrences"])
    assert not any(s["id"] in suggested for s in body["series"])
    assert body["occurrences"] and all(o["maybe"] is False for o in body["occurrences"])


def test_paychecks_are_maybe_too(setup):
    """Owner's decision: paychecks (money in) show as Maybe too."""
    base, items = setup
    assert calendar.MAYBE_KINDS == ("in", "out", "card")
    body = cal(base.client, start=FROM, end=TO)
    assert any(o["recurring_id"] == items["gig"].id for o in body["maybe"])


def test_balances_identical_with_and_without_suggestions(setup):
    base, items = setup
    client = base.client
    with_maybe = cal(client, start=FROM, end=TO)
    for key in ("flix", "spot", "gig", "other"):
        ok(client.patch(f"/api/recurring/{items[key].id}", json={"status": "dismissed"}))
    without = cal(client, start=FROM, end=TO)
    assert without["maybe"] == []  # dismissed: gone
    for field in PROJECTION:
        assert with_maybe[field] == without[field], field


def test_confirming_moves_it_to_occurrences(setup):
    base, items = setup
    client = base.client
    flix = items["flix"].id
    before = cal(client, start=FROM, end=TO)
    ok(client.patch(f"/api/recurring/{flix}", json={"status": "active"}))
    after = cal(client, start=FROM, end=TO)
    assert not any(o["recurring_id"] == flix for o in after["maybe"])
    row = next(o for o in after["occurrences"] if o["key"] == f"r{flix}:2026-09-28")
    assert (row["maybe"], row["counted"], row["counted_on"]) == (False, True, "2026-09-28")
    assert any(s["id"] == f"r{flix}" for s in after["series"])
    bal = {d["date"]: d["balance"] for d in after["days"]}
    old = {d["date"]: d["balance"] for d in before["days"]}
    assert round(old["2026-09-28"] - bal["2026-09-28"], 2) == 15.49


def test_matched_and_past_ones_are_left_out(base):
    db = base.db
    water = make_item(db, "Water", -60, "monthly", "2026-09-26", account=base.chk, status="suggested")
    add_txn(db, base.chk.id, "2026-09-26", -60, "Water")  # today's one is already paid
    body = cal(base.client, start=FROM, end=TO)
    assert maybe_keys(body) == [f"r{water.id}:2026-10-26"]
    # A range that ends before today: nothing.
    assert cal(base.client, start="2026-08-01", end="2026-09-25")["maybe"] == []


def test_no_reminder_or_low_ahead_from_maybe(base):
    db = base.db
    base.chk.current_balance_cents = 201000
    db.commit()
    make_item(db, "Big bill", -500, "monthly", "2026-09-28", account=base.chk, status="suggested", reminder=3)
    created = run(db, TODAY)
    assert keyed(created, "low_ahead") == [] and keyed(created, "reminder") == []
    assert cal(base.client)["first_dip"] is None


def test_home_and_budget_bills_ignore_maybe(base):
    db = base.db
    flix = make_item(db, "Netflix", -15.49, "monthly", "2026-09-28", account=base.chk, status="suggested",
                     reminder=3, category="ENTERTAINMENT")
    body = dash(base.client)
    assert body["coming_up"]["rows"] == []
    assert needs_of(body, "bill_due") == []
    assert calendar.bill_occurrences(db, TODAY, D("2026-09-01"), D("2026-10-31")) == {}
    assert cal(base.client)["maybe"][0]["recurring_id"] == flix.id


def test_empty_without_a_forecast_account(unlocked, db, fixed_today):
    make_account(db, "Savings", "bank", 100, plaid_type="depository", plaid_subtype="savings")
    make_item(db, "Netflix", -15.49, "monthly", "2026-09-28", status="suggested")
    body = cal(unlocked)
    assert body["account"] is None and body["maybe"] == []


def test_hidden_accounts_never_bring_maybe_rows(base):
    """Suggestions on a hidden account stay off the calendar and out of the candidates'
    ``on_calendar``; active items on a hidden card keep the old rule."""
    db = base.db
    hidden_card = make_account(db, "Old Visa", "credit", 0, plaid_type="credit", plaid_subtype="credit card",
                               hidden=True)
    sugg = make_item(db, "Hidden sub", -9.99, "monthly", "2026-09-30", account=hidden_card, status="suggested")
    shown = make_item(db, "Shown sub", -5, "monthly", "2026-09-30", account=base.card, status="suggested")
    active = make_item(db, "Active on hidden", -20, "monthly", "2026-09-30", account=hidden_card)
    body = cal(base.client, start=FROM, end=TO)
    ids = {o["recurring_id"] for o in body["maybe"]}
    assert shown.id in ids and sugg.id not in ids
    assert any(o["recurring_id"] == active.id for o in body["occurrences"])  # unchanged for active items
    cands = {c["item"]["id"]: c for c in ok(base.client.get("/api/recurring/candidates"))}
    assert cands[sugg.id]["on_calendar"] is False and cands[shown.id]["on_calendar"] is True

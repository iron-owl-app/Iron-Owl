"""Home v2 (Release 3.9): GET /api/dashboard — every section, every "needs" kind and its
Settings switch, the running balance vs the calendar, section isolation and auth."""
from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlalchemy import select

from app.models import Account, AlertEvent, AlertSetting, PlaidItem, RecurringItem
from app.services import dashboard, goals, investments, spending
from app.services.alerts import evaluate
from app.utils import utcnow
from tests.conftest import BASE_URL, SessionClient, add_budget, add_txn, make_account
from tests.test_investments_d9 import snap
from tests.test_r36_calendar import make_item
from tests.test_r3_automation import folder, wall  # noqa: F401 - fixtures
from tests.test_static_and_run import spa_client  # noqa: F401 - fixture

D = dt.date
TODAY = D(2026, 9, 26)  # conftest.FIXED_TODAY (a Saturday; 4 days left in September)


def ok(response, status: int = 200):
    assert response.status_code == status, response.text
    return response.json() if response.content else None


def dash(client) -> dict:
    return ok(client.get("/api/dashboard"))


def needs_of(body: dict, kind: str) -> list[dict]:
    return [a for a in body["needs"] if a["kind"] == kind]


def one(body: dict, kind: str) -> dict:
    found = needs_of(body, kind)
    assert len(found) == 1, body["needs"]
    return found[0]


def switch(client, key: str, enabled: bool, value=None) -> None:
    body = {"enabled": enabled} if value is None else {"enabled": enabled, "value": value}
    ok(client.put(f"/api/alerts/settings/{key}", json=body))


def calendar_days(client) -> dict[str, float]:
    cal = ok(client.get("/api/forecast/calendar", params={"from": "2026-09-26", "to": "2026-10-31"}))
    return {d["date"]: d["balance"] for d in cal["days"] if d["balance"] is not None}


def checking(db, balance=1000.0, **kw):
    return make_account(db, "Checking", "bank", balance, plaid_type="depository", plaid_subtype="checking",
                        mask="4417", institution_name="Neighborhood Bank", **kw)


# ------------------------------------------------------------------ whole screen, auth


def test_brand_new_vault(unlocked, fixed_today):
    body = dash(unlocked)
    assert set(body) == {"today", "setup", "banks", "cash", "budget", "coming_up", "months", "goals",
                         "investments", "needs", "dismissed", "errors"}
    assert body["today"] == "2026-09-26" and body["errors"] == [] and body["needs"] == []
    assert body["dismissed"] == {} and body["banks"] == []
    assert body["setup"] == {"plaid_configured": True, "plaid_source": "env", "keys_rejected": False,
                             "visible_accounts": 0, "linked_items": 0, "pending_items": 0}
    assert body["cash"] == {"total": 0.0, "updated_at": None, "accounts": []}
    assert body["budget"] == {"month": "2026-09", "is_current": True, "days_left": 4, "has_budget": False,
                              "planned": 0.0, "spent": 0.0, "left": 0.0, "next_paycheck": None}
    assert body["coming_up"] == {"account": None, "from": "2026-09-26", "to": "2026-10-03", "balance": None,
                                 "threshold": 2000.0, "warning_on": True, "daily_spend": 0.0, "rows": [],
                                 "more": 0, "low": None, "first_below": None}
    assert [m["month"] for m in body["months"]] == ["2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09"]
    assert all(not m["has_data"] for m in body["months"])
    assert [m["complete"] for m in body["months"]] == [True] * 5 + [False]
    assert body["goals"] == {"budget_ready": False, "total_saved": 0.0, "in_progress": 0, "top": []}
    assert body["investments"] == {"has_accounts": False, "worth": 0.0, "change": None, "change_missing": []}
    # Gone vs GET /api/home.
    assert not {"version", "forecast", "low_alert_enabled", "backup", "net_worth"} & set(body)


def test_dashboard_needs_a_session(unlocked, app):
    anon = SessionClient(app, base_url=BASE_URL)
    assert anon.get("/api/dashboard").status_code == 401
    unlocked.post("/api/auth/lock")
    assert unlocked.get("/api/dashboard").status_code == 401


def test_no_tokens_or_plaid_text(unlocked, db, fixed_today):
    item = PlaidItem(plaid_item_id="i1", access_token="access-sandbox-TOPSECRET", institution_name="Neighborhood Bank",
                     kind="bank", status="error", error_code="bad code <script>",
                     last_synced_at=dt.datetime(2026, 9, 26, 10, 0, 0))
    db.add(item)
    db.commit()
    chk = checking(db, 8432.18, item_id=item.id)
    make_account(db, "Cash jar", "bank", 50)
    make_account(db, "Old savings", "bank", 999, hidden=True)
    body = dash(unlocked)
    text = json.dumps(body)
    assert "TOPSECRET" not in text and "<script>" not in text and "access-" not in text
    assert body["cash"]["total"] == 8482.18 and body["cash"]["updated_at"] == "2026-09-26T10:00:00Z"
    assert [a["name"] for a in body["cash"]["accounts"]] == ["Checking", "Cash jar"]
    assert body["cash"]["accounts"][0] == {
        "id": chk.id, "name": "Checking", "mask": "4417", "institution_name": "Neighborhood Bank",
        "balance": 8432.18, "source": "plaid", "item_id": item.id, "subtype": "checking"}
    assert one(body, "bank_error")["data"]["error_code"] == "UNKNOWN"
    assert body["coming_up"]["account"] == {"id": chk.id, "name": "Checking", "mask": "4417",
                                            "institution_name": "Neighborhood Bank"}


def test_cash_order_checking_then_savings_then_others(unlocked, db, fixed_today):
    def bank(name, subtype, balance=100.0, **kw):
        return make_account(db, name, "bank", balance, plaid_type="depository", plaid_subtype=subtype, **kw)

    bank("Zeta checking", "checking", 50)
    bank("Alpha savings", "savings")
    main = bank("Main checking", "checking", 9000)  # the richest checking: the forecast account
    bank("beta checking", "checking", 10)
    bank("Money market", "money market")
    make_account(db, "Cash jar", "bank", 20)  # manual, no type: other
    bank("Holiday savings", "Savings")
    names = lambda: [a["name"] for a in dash(unlocked)["cash"]["accounts"]]  # noqa: E731
    assert names() == ["Main checking", "beta checking", "Zeta checking", "Alpha savings", "Holiday savings",
                       "Cash jar", "Money market"]
    # A forecast account chosen by hand comes first whatever its type.
    jar = db.scalar(select(Account).where(Account.name == "Cash jar"))
    ok(unlocked.put("/api/forecast/account", json={"account_id": jar.id}))
    assert names() == ["Cash jar", "beta checking", "Main checking", "Zeta checking", "Alpha savings",
                       "Holiday savings", "Money market"]
    assert main.id != jar.id


# ------------------------------------------------------------------ budget ("Left to spend")


def test_budget_matches_the_budget_screen_and_next_paycheck(unlocked, db, fixed_today):
    chk = checking(db, 5000)
    add_budget(db, "2026-09", {"FOOD_AND_DRINK": 600, "ENTERTAINMENT": 100.10, "TRAVEL": 300})
    ok(unlocked.put("/api/budgets/settings", json={"savings_category": "TRAVEL"}))
    add_txn(db, chk.id, "2026-09-05", -412.37, "Groceries")
    add_txn(db, chk.id, "2026-09-12", -150.02, "Concert", category="ENTERTAINMENT")
    add_txn(db, chk.id, "2026-09-13", -20, "Bus", category="TRAVEL")  # Emergency savings: not to spend
    make_item(db, "Paycheck", 1500, "biweekly", "2026-09-30", account=chk, source="manual")
    budget = dash(unlocked)["budget"]
    bm = ok(unlocked.get("/api/budgets"))
    lines = [c for c in bm["categories"] if c["category"] != "TRAVEL"]
    left = round(sum(c["available"] for c in lines), 2)
    spent = round(sum(c["spent"] for c in lines), 2)
    assert budget == {"month": "2026-09", "is_current": True, "days_left": 4, "has_budget": True,
                      "planned": 700.1, "spent": 562.39, "left": 137.71,
                      "next_paycheck": {"date": "2026-09-30", "name": "Paycheck", "amount": 1500.0}}
    assert (left, spent) == (137.71, 562.39)
    assert budget["next_paycheck"] == bm["income"]["next"]
    assert round(budget["planned"] - budget["spent"], 2) == budget["left"]


def test_budget_over_plan_goes_negative(unlocked, db, fixed_today):
    chk = checking(db, 5000)
    add_budget(db, "2026-09", {"FOOD_AND_DRINK": 100})
    add_txn(db, chk.id, "2026-09-05", -130.5, "Groceries")
    budget = dash(unlocked)["budget"]
    assert (budget["planned"], budget["spent"], budget["left"]) == (100.0, 130.5, -30.5)


# ------------------------------------------------------------------ coming up


@pytest.fixture
def week(unlocked, db, fixed_today):
    """Checking 1,000; everyday spending $10 a day ($100 over the 10 days since the first
    transaction); a pending phone bill (still expected), a card charge, a paycheck, rent, an
    uncounted gym and water (outside the week)."""
    chk = checking(db, 1000)
    visa = make_account(db, "Visa", "credit", 250, mask="1111")
    add_txn(db, chk.id, "2026-09-17", -100, "Corner store")
    items = {
        "phone": make_item(db, "Phone", -80, "monthly", "2026-09-24", account=chk, last_seen="2026-08-24"),
        "flix": make_item(db, "StreamFlix", -15.99, "monthly", "2026-09-28", account=visa),
        "pay": make_item(db, "Paycheck", 1500, "biweekly", "2026-09-30", account=chk, source="manual"),
        "rent": make_item(db, "Rent", -1200, "monthly", "2026-10-01", source="manual"),
        "gym": make_item(db, "Gym", -40, "monthly", "2026-09-27", account=chk, source="manual", include=False),
        "water": make_item(db, "Water", -60, "monthly", "2026-10-20", account=chk, source="manual"),
    }
    return unlocked, db, chk, visa, items


def test_coming_up_rows_and_running_balance(week):
    client, db, chk, visa, items = week
    cu = dash(client)["coming_up"]
    assert (cu["from"], cu["to"], cu["balance"], cu["daily_spend"]) == ("2026-09-26", "2026-10-03", 1000.0, 10.0)
    assert [(r["name"], r["kind"], r["status"], r["still_expected"], r["counted_on"], r["after"]) for r in cu["rows"]] == [
        ("Phone", "out", "pending", True, "2026-09-27", 910.0),  # 1000 − 10 a day − 80
        ("StreamFlix", "card", "upcoming", False, None, None),  # not from checking
        ("Paycheck", "in", "upcoming", False, "2026-09-30", 2380.0),  # 890 − 10 + 1500
        ("Rent", "out", "upcoming", False, "2026-10-01", 1170.0),
    ]
    rows = {r["name"]: r for r in cu["rows"]}
    assert rows["StreamFlix"]["account_name"] == "Visa" and rows["Phone"]["account_name"] == "Checking"
    assert rows["Rent"]["account_name"] == "Checking"  # no account: follows checking
    assert rows["Rent"]["amount"] == -1200.0 and rows["Paycheck"]["amount"] == 1500.0
    assert rows["Rent"]["key"] == f"r{items['rent'].id}:2026-10-01" and rows["Rent"]["recurring_id"] == items["rent"].id
    assert cu["more"] == 0
    # Each day's last counted row ends at the calendar's balance for that day.
    days = calendar_days(client)
    last: dict[str, float] = {}
    for r in cu["rows"]:
        if r["after"] is not None:
            last[r["counted_on"]] = r["after"]
    assert last and all(days[d] == after for d, after in last.items())
    # Lowest point in the week and the warning amount ($2,000: already below today).
    assert cu["low"] == {"date": "2026-09-29", "balance": 890.0}
    assert cu["threshold"] == 2000.0 and cu["warning_on"] is True
    assert cu["first_below"] == {"date": "2026-09-26", "balance": 1000.0}


def test_coming_up_note_inputs(week):
    client = week[0]
    switch(client, "low", True, 100)  # stays above all week
    cu = dash(client)["coming_up"]
    assert cu["first_below"] is None and cu["low"] == {"date": "2026-09-29", "balance": 890.0}
    switch(client, "low", True, 900)  # could drop below on the 29th
    cu = dash(client)["coming_up"]
    assert cu["threshold"] == 900.0 and cu["first_below"] == {"date": "2026-09-29", "balance": 890.0}
    switch(client, "low", False)  # warning off: only the lowest point
    cu = dash(client)["coming_up"]
    assert cu["warning_on"] is False and cu["first_below"] is None and cu["low"]["balance"] == 890.0


def test_coming_up_without_everyday_spending(week):
    client = week[0]
    ok(client.patch("/api/forecast/settings", json={"include_daily": False}))
    cu = dash(client)["coming_up"]
    assert cu["daily_spend"] is None
    assert [r["after"] for r in cu["rows"]] == [920.0, None, 2420.0, 1220.0]
    days = calendar_days(client)
    assert days["2026-09-27"] == 920.0 and days["2026-10-01"] == 1220.0


def test_coming_up_keeps_eight_rows(unlocked, db, fixed_today):
    chk = checking(db, 5000)
    for n in range(11):
        make_item(db, f"Bill {n:02d}", -(n + 1), "monthly", f"2026-09-{27 + n % 4}", account=chk, source="manual")
    client = unlocked
    cu = dash(client)["coming_up"]
    assert len(cu["rows"]) == 8 and cu["more"] == 3
    assert [r["counted_on"] for r in cu["rows"]] == sorted(r["counted_on"] for r in cu["rows"])
    days = calendar_days(client)
    by_day: dict[str, float] = {}
    for r in cu["rows"]:
        by_day[r["counted_on"]] = r["after"]
    # Full days in the shown rows end on the calendar's balance.
    assert by_day["2026-09-27"] == days["2026-09-27"] and by_day["2026-09-28"] == days["2026-09-28"]


def test_coming_up_counts_by_date_plans(unlocked, db, fixed_today):
    checking(db, 3000)
    ok(unlocked.patch("/api/categories/TRAVEL", json={"target": {"kind": "by_date", "amount": 400, "date": "2026-10-02"}}))
    cu = dash(unlocked)["coming_up"]
    (row,) = cu["rows"]
    assert (row["kind"], row["name"], row["amount"], row["recurring_id"], row["after"]) == (
        "plan", "Travel", -400.0, None, 2600.0)
    assert row["account_name"] is None
    ok(unlocked.patch("/api/forecast/settings", json={"excluded_plans": ["TRAVEL"]}))
    assert dash(unlocked)["coming_up"]["rows"] == []  # not counted: not listed


def _day_ends_match_calendar(client, rows: list[dict]) -> None:
    """Each counted day's last row is marked ``day_end`` and ends at the calendar's balance."""
    days = calendar_days(client)
    last: dict[str, dict] = {}
    for r in rows:
        if r["after"] is not None:
            last[r["counted_on"]] = r
    assert [r["key"] for r in rows if r["day_end"]] == [r["key"] for r in rows if r in last.values()]
    assert all(days[d] == r["after"] for d, r in last.items())


def test_coming_up_bill_due_today_before_tomorrows_paycheck(unlocked, db, fixed_today):
    """Review: an unpaid bill due today counts tomorrow; it must still come before tomorrow's
    paycheck, and its "Checking after" must not include that pay."""
    chk = checking(db, 1000)
    make_item(db, "Rent", -800, "monthly", "2026-09-26", account=chk, source="manual")  # today, unpaid
    make_item(db, "Paycheck", 1500, "biweekly", "2026-09-27", account=chk, source="manual")
    cu = dash(unlocked)["coming_up"]
    assert [(r["date"], r["name"], r["counted_on"], r["after"], r["day_end"]) for r in cu["rows"]] == [
        ("2026-09-26", "Rent", "2026-09-27", 200.0, False),  # 1000 − 800 (no everyday spending yet)
        ("2026-09-27", "Paycheck", "2026-09-27", 1700.0, True),
    ]
    _day_ends_match_calendar(unlocked, cu["rows"])


def test_coming_up_mixed_days_in_date_order(unlocked, db, fixed_today):
    """Late, due-today, card and several same-day rows: dates never run backwards (after the
    still-expected ones), and every day's last row ends on the calendar's balance."""
    chk = checking(db, 3000)
    visa = make_account(db, "Visa", "credit", 0, mask="1111")
    add_txn(db, chk.id, "2026-09-10", -300, "Corner store")  # some everyday spending
    make_item(db, "Phone", -80, "monthly", "2026-09-22", account=chk, last_seen="2026-08-22")  # late
    make_item(db, "Rent", -800, "monthly", "2026-09-26", account=chk, source="manual")  # today
    make_item(db, "Gas card", -30, "monthly", "2026-09-26", account=visa)  # card, today
    make_item(db, "Pay", 1500, "biweekly", "2026-09-27", account=chk, source="manual")
    make_item(db, "Water", -50, "monthly", "2026-09-27", account=chk, source="manual")
    make_item(db, "Power", -70, "monthly", "2026-09-27", account=chk, source="manual")
    make_item(db, "Flix", -16, "monthly", "2026-09-27", account=visa)
    make_item(db, "Ins", -120, "monthly", "2026-10-03", account=chk, source="manual")
    cu = dash(unlocked)["coming_up"]
    rows = cu["rows"]
    assert [(r["name"], r["date"], r["day_end"]) for r in rows] == [
        ("Phone", "2026-09-22", False),  # still expected: first
        ("Gas card", "2026-09-26", False),  # card: not from checking, never a day end
        ("Rent", "2026-09-26", False),
        ("Pay", "2026-09-27", False),  # then money in first, by name
        ("Power", "2026-09-27", False),
        ("Water", "2026-09-27", True),
        ("Flix", "2026-09-27", False),
        ("Ins", "2026-10-03", True),
    ]
    dates = [r["date"] for r in rows if not r["still_expected"]]
    assert dates == sorted(dates)
    _day_ends_match_calendar(unlocked, rows)


def test_coming_up_day_end_rows_agree_with_the_note(unlocked, db, fixed_today):
    """Review: a row mid-day can dip under the warning while the day ends above it. Only
    ``day_end`` rows are judged, like the note (``first_below``) and the Bills calendar."""
    chk = checking(db, 2050)
    make_item(db, "Phone", -80, "monthly", "2026-09-22", account=chk, last_seen="2026-08-22")  # late: expected
    make_item(db, "Pay", 1500, "biweekly", "2026-09-27", account=chk, source="manual")
    ok(unlocked.patch("/api/forecast/settings", json={"include_daily": False}))
    cu = dash(unlocked)["coming_up"]
    assert [(r["name"], r["after"], r["day_end"]) for r in cu["rows"]] == [
        ("Phone", 1970.0, False), ("Pay", 3470.0, True)]
    flagged = [r for r in cu["rows"] if r["day_end"] and r["after"] < cu["threshold"]]
    assert flagged == [] and cu["first_below"] is None
    # When a day does end below, its day_end row and first_below name the same day.
    switch(unlocked, "low", True, 3500)
    cu = dash(unlocked)["coming_up"]
    flagged = [r for r in cu["rows"] if r["day_end"] and r["after"] < cu["threshold"]]
    assert cu["first_below"] == {"date": "2026-09-26", "balance": 2050.0}  # already below today
    assert [r["counted_on"] for r in flagged] == ["2026-09-27"]


def test_coming_up_day_cut_short_has_no_day_end(unlocked, db, fixed_today):
    chk = checking(db, 5000)
    for n in range(10):
        make_item(db, f"Bill {n:02d}", -(n + 1), "monthly", "2026-09-27", account=chk, source="manual")
    cu = dash(unlocked)["coming_up"]
    assert len(cu["rows"]) == 8 and cu["more"] == 2
    assert not any(r["day_end"] for r in cu["rows"])  # the day's last row is in "more"


# ------------------------------------------------------------------ months ("Left over each month")


def test_months(unlocked, db, fixed_today):
    chk = checking(db, 1000)
    hidden = make_account(db, "Old", "bank", 0, hidden=True)
    add_txn(db, hidden.id, "2026-04-02", -999, "Hidden")  # hidden accounts don't count, even for has_data
    add_txn(db, chk.id, "2026-05-10", -40, "First")  # history starts mid-May: May is partial
    add_txn(db, chk.id, "2026-08-01", 2000, "Payroll", category="INCOME")
    add_txn(db, chk.id, "2026-08-03", -300, "Groceries")
    add_txn(db, chk.id, "2026-08-04", -100, "Loan", category="LOAN_PAYMENTS")
    add_txn(db, chk.id, "2026-08-05", -500, "To savings", category="TRANSFER_OUT", is_transfer=True)
    add_txn(db, chk.id, "2026-07-06", -105, "Groceries")
    add_txn(db, chk.id, "2026-09-02", 700, "Payroll", category="INCOME")
    months = {m["month"]: m for m in dash(unlocked)["months"]}
    assert months["2026-04"] == {"month": "2026-04", "came_in": 0.0, "went_out": 0.0, "left_over": 0.0,
                                 "complete": True, "has_data": False, "partial": False}
    assert months["2026-05"]["has_data"] is True and months["2026-05"]["went_out"] == 40.0
    assert months["2026-05"]["partial"] is True and months["2026-05"]["complete"] is True
    assert [m for m, v in months.items() if v["partial"]] == ["2026-05"]
    assert months["2026-07"]["left_over"] == -105.0
    assert months["2026-08"] == {"month": "2026-08", "came_in": 2000.0, "went_out": 400.0, "left_over": 1600.0,
                                 "complete": True, "has_data": True, "partial": False}
    assert months["2026-09"]["complete"] is False and months["2026-09"]["came_in"] == 700.0
    # The Reports page's numbers.
    for r in ok(unlocked.get("/api/reports/monthly", params={"months": 6}))["months"]:
        m = months[r["month"]]
        assert (m["came_in"], m["went_out"]) == (r["income"], round(r["spending"] + r["fixed"], 2))


def test_first_month_starting_on_the_1st_is_whole(unlocked, db, fixed_today):
    chk = checking(db, 1000)
    add_txn(db, chk.id, "2026-06-01", -40, "First")
    months = {m["month"]: m for m in dash(unlocked)["months"]}
    assert months["2026-06"]["has_data"] is True and months["2026-06"]["partial"] is False
    assert not any(m["partial"] for m in months.values())


# ------------------------------------------------------------------ goals and investments


def _save_goal(client, name, target, monthly=90, due="2026-12", **kw) -> dict:
    body = {"kind": "save", "name": name, "target": target, "due_month": due, "monthly": monthly, **kw}
    return ok(client.post("/api/goals", json=body), 201)


def test_goals_card_matches_the_goals_page(unlocked, db, fixed_today):
    checking(db, 4000)
    make_account(db, "Savings", "bank", 1000)
    add_budget(db, "2026-09", {"FOOD_AND_DRINK": 500})
    _save_goal(unlocked, "Done", 100, monthly=0, already_saved=100)  # reached
    for name, target in (("Trip", 600), ("Couch", 900), ("Gifts", 300), ("Car", 2000)):
        _save_goal(unlocked, name, target, already_saved=50)
    state = ok(unlocked.get("/api/goals"))
    card = dash(unlocked)["goals"]
    assert card["budget_ready"] is True and card["in_progress"] == 4
    assert card["total_saved"] == round(sum(g["saved"] for g in state["goals"]), 2) == 300.0
    unfinished = [g for g in state["goals"] if g["status"] != "reached"]
    assert card["top"] == [{k: g[k] for k in ("id", "name", "saved", "target", "status")} for g in unfinished[:3]]
    assert [g["name"] for g in card["top"]] == ["Trip", "Couch", "Gifts"]
    # Without a shared Budget month, the card builds its own: same numbers.
    assert goals.summary(db, TODAY) == card
    assert goals.summary(db, TODAY, spending.budget_month(db, "2026-09", TODAY)) == card


def test_investments_tile_matches_the_investments_page(unlocked, db, fixed_today):
    ret = make_account(db, "Retirement", "retirement", 38912.77)
    hsa = make_account(db, "HSA", "hsa", 4210.40)
    inv = make_account(db, "Investment", "investment", 1000)
    make_account(db, "Hidden brokerage", "investment", 99999, hidden=True)
    snap(db, ret, "2026-07-31", 36000)
    snap(db, ret, "2026-08-20", 37000)
    snap(db, ret, "2026-08-31", 99999, estimated=True)
    snap(db, inv, "2026-07-10", 850)
    snap(db, hsa, "2026-09-10", 4000)
    tile = dash(unlocked)["investments"]
    total = ok(unlocked.get("/api/investments"))["total"]
    assert tile == {"has_accounts": True, "worth": total["worth"], "change": total["change"],
                    "change_missing": total["change_missing"]}
    assert tile == {"has_accounts": True, "worth": 44123.17, "change": 2062.77, "change_missing": ["HSA"]}
    assert investments.totals(db, TODAY) == tile


def test_investments_all_new_this_month(unlocked, db, fixed_today):
    make_account(db, "IRA", "retirement", 500)
    assert dash(unlocked)["investments"] == {"has_accounts": True, "worth": 500.0, "change": None,
                                             "change_missing": ["IRA"]}


# ------------------------------------------------------------------ needs: low balance


@pytest.fixture
def rent_dip(unlocked, db, fixed_today):
    chk = checking(db, 1500)
    make_item(db, "Rent", -1200, "monthly", "2026-10-01", source="manual")
    switch(unlocked, "low", True, 500)
    return unlocked, chk


def test_low_balance_need(rent_dip):
    client, chk = rent_dip
    need = one(dash(client), "low_balance")
    assert need == {"key": f"low_balance:{chk.id}", "kind": "low_balance", "tone": "warn", "dismissible": True,
                    "fingerprint": "2026-10-01",
                    "data": {"account_id": chk.id, "account_name": "Checking", "date": "2026-10-01",
                             "balance": 300.0, "threshold": 500.0, "cause": "Rent"}}
    # "Not now" until the dip date moves.
    ok(client.put(f"/api/home/dismissals/{need['key']}", json={"fingerprint": need["fingerprint"]}), 204)
    assert dash(client)["dismissed"][need["key"]] == need["fingerprint"]
    switch(client, "low", False)
    assert needs_of(dash(client), "low_balance") == []
    # The Alerts tab's row turns both keys on together.
    ok(client.put("/api/alerts/settings", json={"low": {"enabled": True}}))
    assert len(needs_of(dash(client), "low_balance")) == 1


def test_low_balance_already_below_today(unlocked, db, fixed_today):
    chk = checking(db, 100)
    need = one(dash(unlocked), "low_balance")  # default warning amount $2,000
    assert need["fingerprint"] == "2026-09-26" and need["data"]["cause"] is None
    assert need["key"] == f"low_balance:{chk.id}"


# ------------------------------------------------------------------ needs: over plan


def test_over_plan_need(unlocked, db, fixed_today):
    chk = checking(db, 5000)
    add_budget(db, "2026-09", {"FOOD_AND_DRINK": 100, "ENTERTAINMENT": 50, "TRAVEL": 10, "MEDICAL": 0})
    ok(unlocked.put("/api/budgets/settings", json={"savings_category": "TRAVEL"}))
    add_txn(db, chk.id, "2026-09-05", -118.75, "Diner")
    add_txn(db, chk.id, "2026-09-06", -50, "Movie", category="ENTERTAINMENT")  # exactly the plan: not over
    add_txn(db, chk.id, "2026-09-07", -90, "Bus", category="TRAVEL")  # Emergency savings: skipped
    body = dash(unlocked)
    assert needs_of(body, "over_plan") == [{
        "key": "over_plan:FOOD_AND_DRINK", "kind": "over_plan", "tone": "warn", "dismissible": True,
        "fingerprint": "2026-09",
        "data": {"category_id": "FOOD_AND_DRINK", "name": "Food and drink", "planned": 100.0, "spent": 118.75,
                 "over": 18.75, "month": "2026-09"},
    }]
    # Nothing planned: "Spending without a plan" on the Budget page, not "over plan" (as the alert).
    add_txn(db, chk.id, "2026-09-08", -20, "Pharmacy", category="MEDICAL")
    assert [a["key"] for a in needs_of(dash(unlocked), "over_plan")] == ["over_plan:FOOD_AND_DRINK"]
    switch(unlocked, "budget", False)
    assert needs_of(dash(unlocked), "over_plan") == []


def test_over_plan_skips_goal_categories(unlocked, db, fixed_today):
    chk = checking(db, 4000)
    add_budget(db, "2026-09", {"FOOD_AND_DRINK": 500})
    created = _save_goal(unlocked, "Trip", 600, monthly=10)
    goal = next(g for g in created["goals"] if g["id"] == created["goal_id"])
    add_txn(db, chk.id, "2026-09-10", -300, "Airline", category=goal["category_id"])
    assert needs_of(dash(unlocked), "over_plan") == []


def test_over_plan_many(unlocked, db, fixed_today):
    chk = checking(db, 5000)
    cats = ["FOOD_AND_DRINK", "ENTERTAINMENT", "MEDICAL", "PERSONAL_CARE"]
    add_budget(db, "2026-09", {c: 10 for c in cats})
    for c, spent in zip(cats, (15, 12, 30, 16)):
        add_txn(db, chk.id, "2026-09-05", -spent, "Spend", category=c)
    need = one(dash(unlocked), "over_plan")
    fp = need["fingerprint"]
    assert need == {"key": "over_plan:many", "kind": "over_plan", "tone": "warn", "dismissible": True,
                    "fingerprint": fp,
                    "data": {"count": 4, "month": "2026-09", "total_over": 33.0,
                             "names": ["Medical", "Personal care", "Food and drink"]}}  # furthest over first
    assert fp.startswith("2026-09:4:") and dashboard.FINGERPRINT_RE.fullmatch(fp)
    ok(unlocked.put("/api/home/dismissals/over_plan:many", json={"fingerprint": fp}), 204)
    add_budget(db, "2026-09", {"TRAVEL": 1})
    add_txn(db, chk.id, "2026-09-06", -5, "Train", category="TRAVEL")
    body = dash(unlocked)
    fp5 = one(body, "over_plan")["fingerprint"]
    assert fp5.startswith("2026-09:5:") and fp5 != body["dismissed"]["over_plan:many"]


def _set_plan(db, month: str, category: str, dollars: float) -> None:
    from app.models import Budget

    row = db.scalar(select(Budget).where(Budget.month == month, Budget.category == category))
    row.limit_cents = round(dollars * 100)
    db.commit()


def _visible(body: dict, kind: str) -> list[dict]:
    """What the page shows: the client hides a need while its dismissed fingerprint matches."""
    return [a for a in needs_of(body, kind) if body["dismissed"].get(a["key"]) != a["fingerprint"]]


def test_over_plan_many_not_now_stays_until_worse(unlocked, db, fixed_today):
    """Review: after "Not now" on the roll-up, fixing a category must not bring back the
    others one by one (or as a smaller roll-up); only something getting worse does."""
    chk = checking(db, 5000)
    cats = ["FOOD_AND_DRINK", "ENTERTAINMENT", "TRAVEL", "MEDICAL", "PERSONAL_CARE"]
    add_budget(db, "2026-09", {c: 10 for c in cats})
    for c in cats:
        add_txn(db, chk.id, "2026-09-05", -50, f"x{c}", category=c)  # each $40 over
    many = one(dash(unlocked), "over_plan")
    ok(unlocked.put(f"/api/home/dismissals/{many['key']}", json={"fingerprint": many["fingerprint"]}), 204)
    assert _visible(dash(unlocked), "over_plan") == []
    _set_plan(db, "2026-09", "MEDICAL", 100)  # the user covers one: 4 over, a smaller roll-up
    assert needs_of(dash(unlocked), "over_plan") == []
    for c in ("TRAVEL", "PERSONAL_CARE"):  # two more: 2 over, would be single needs
        _set_plan(db, "2026-09", c, 100)
    assert needs_of(dash(unlocked), "over_plan") == []
    # An already-over one grows, but not past what it was over by: still hidden.
    _set_plan(db, "2026-09", "ENTERTAINMENT", 20)  # over $30 now (was $40)
    add_txn(db, chk.id, "2026-09-06", -5, "Movie", category="ENTERTAINMENT")  # over $35
    assert needs_of(dash(unlocked), "over_plan") == []
    # One grows past its dismissed amount ($40 → $45): shown again (the two still over).
    add_txn(db, chk.id, "2026-09-07", -5, "More", category="FOOD_AND_DRINK")
    shown = _visible(dash(unlocked), "over_plan")
    assert sorted(a["key"] for a in shown) == ["over_plan:ENTERTAINMENT", "over_plan:FOOD_AND_DRINK"]


def test_over_plan_many_not_now_new_category_over(unlocked, db, fixed_today):
    chk = checking(db, 5000)
    cats = ["FOOD_AND_DRINK", "ENTERTAINMENT", "TRAVEL", "MEDICAL"]
    add_budget(db, "2026-09", {c: 10 for c in cats + ["PERSONAL_CARE"]})
    for c in cats:
        add_txn(db, chk.id, "2026-09-05", -50, f"x{c}", category=c)
    many = one(dash(unlocked), "over_plan")
    ok(unlocked.put(f"/api/home/dismissals/{many['key']}", json={"fingerprint": many["fingerprint"]}), 204)
    _set_plan(db, "2026-09", "MEDICAL", 100)  # 3 over, then a new one goes over: 4 again
    add_txn(db, chk.id, "2026-09-06", -20, "Haircut", category="PERSONAL_CARE")
    (again,) = _visible(dash(unlocked), "over_plan")
    assert again["key"] == "over_plan:many" and again["data"]["count"] == 4
    # A "Not now" on an out-of-date fingerprint keeps no snapshot: the plain rule applies.
    ok(unlocked.put("/api/home/dismissals/over_plan:many", json={"fingerprint": "2026-09:4:00000000"}), 204)
    assert dashboard.read_over_snapshot(db) is None
    assert len(_visible(dash(unlocked), "over_plan")) == 1
    # Next month: last month's "Not now" doesn't hide anything.
    ok(unlocked.put(f"/api/home/dismissals/{again['key']}", json={"fingerprint": again["fingerprint"]}), 204)
    assert dashboard.read_over_snapshot(db)["month"] == "2026-09"
    fixed_today.day = D(2026, 10, 2)
    add_budget(db, "2026-10", {"GENERAL_MERCHANDISE": 10})
    add_txn(db, chk.id, "2026-10-01", -50, "Store", category="GENERAL_MERCHANDISE")
    assert [a["key"] for a in _visible(dash(unlocked), "over_plan")] == ["over_plan:GENERAL_MERCHANDISE"]


# ------------------------------------------------------------------ needs: bills and card payments


def test_bill_due_and_card_due(unlocked, db, fixed_today):
    chk = checking(db, 5000)
    rent = make_item(db, "Rent", -1200, "monthly", "2026-10-01", source="manual", reminder=5)
    make_item(db, "Paycheck", 1500, "monthly", "2026-09-26", account=chk, source="manual", reminder=1)  # money in
    visa = make_account(db, "Visa", "credit", 250, mask="1111", institution_name="Card Co",
                        next_payment_due=D(2026, 9, 28), minimum_payment_cents=3500)
    flix = make_item(db, "StreamFlix", -15.99, "monthly", "2026-09-27", account=visa, reminder=2)
    make_item(db, "Water", -60, "monthly", "2026-10-20", account=chk, source="manual", reminder=3)  # not yet
    make_item(db, "Gas", -45, "monthly", "2026-09-28", account=chk, source="manual")  # no reminder
    make_account(db, "Car loan", "loan", 9000, next_payment_due=D(2026, 10, 15))  # beyond 3 days
    body = dash(unlocked)
    assert needs_of(body, "bill_due") == [
        {"key": f"bill_due:{flix.id}", "kind": "bill_due", "tone": "info", "dismissible": True,
         "fingerprint": "2026-09-27",
         "data": {"recurring_id": flix.id, "name": "StreamFlix", "date": "2026-09-27", "amount": 15.99, "kind": "card",
                  "account_name": "Visa", "days": 1}},
        {"key": f"bill_due:{rent.id}", "kind": "bill_due", "tone": "info", "dismissible": True,
         "fingerprint": "2026-10-01",
         "data": {"recurring_id": rent.id, "name": "Rent", "date": "2026-10-01", "amount": 1200.0, "kind": "out",
                  "account_name": "Checking", "days": 5}},
    ]
    card = one(body, "card_due")
    visa_id = card["data"]["account_id"]
    assert card == {"key": f"card_due:{visa_id}", "kind": "card_due", "tone": "info", "dismissible": True,
                    "fingerprint": "2026-09-28",
                    "data": {"account_id": visa_id, "name": "Visa", "mask": "1111", "institution_name": "Card Co",
                             "due_date": "2026-09-28", "days": 2, "minimum_payment": 35.0,
                             "account_category": "credit"}}
    # Only card_due follows the `due` key...
    switch(unlocked, "due", False)
    body = dash(unlocked)
    assert needs_of(body, "card_due") == [] and len(needs_of(body, "bill_due")) == 2
    switch(unlocked, "due", True)
    # ...and the Alerts tab's "Bill due soon" row turns off both.
    ok(unlocked.put("/api/alerts/settings", json={"reminder": {"enabled": False}}))
    body = dash(unlocked)
    assert needs_of(body, "card_due") == [] and needs_of(body, "bill_due") == []


def test_card_due_skips_nothing_owed_and_zero_minimum(unlocked, db, fixed_today):
    checking(db, 5000)
    due = D(2026, 9, 28)
    make_account(db, "Paid off", "credit", 0, next_payment_due=due, minimum_payment_cents=2500)
    make_account(db, "In credit", "credit", -12.5, next_payment_due=due)
    make_account(db, "Zero minimum", "credit", 80, next_payment_due=due, minimum_payment_cents=0)
    make_account(db, "Loan done", "loan", 0, next_payment_due=due)
    owed = make_account(db, "Visa", "credit", 250, next_payment_due=due)  # minimum unknown: still shown
    assert [a["key"] for a in needs_of(dash(unlocked), "card_due")] == [f"card_due:{owed.id}"]


def test_bill_due_skips_paid_and_moved(unlocked, db, fixed_today):
    chk = checking(db, 5000)
    rent = make_item(db, "Rent", -1200, "monthly", "2026-09-28", account=chk, source="manual", reminder=3)
    ok(unlocked.put(f"/api/recurring/{rent.id}/occurrences/2026-09-28", json={"moved_to": "2026-10-05"}))
    assert needs_of(dash(unlocked), "bill_due") == []  # moved out of its reminder window
    ok(unlocked.put(f"/api/recurring/{rent.id}/occurrences/2026-09-28", json={"moved_to": "2026-09-29"}))
    need = one(dash(unlocked), "bill_due")
    assert need["fingerprint"] == "2026-09-28" and need["data"]["date"] == "2026-09-29" and need["data"]["days"] == 3
    ok(unlocked.put(f"/api/recurring/{rent.id}/occurrences/2026-09-28", json={"moved_to": None, "paid": True}))
    assert needs_of(dash(unlocked), "bill_due") == []


# ------------------------------------------------------------------ needs: new repeating charges


def _suggested(db, name, cents, days_ago=1, account=None) -> RecurringItem:
    item = RecurringItem(name=name, merchant_key=name.lower(), account_id=account.id if account else None,
                         amount_cents=cents, cadence="monthly", next_date=D(2026, 10, 10), status="suggested",
                         source="detected", created_at=utcnow() - dt.timedelta(days=days_ago))
    db.add(item)
    db.commit()
    return item


def test_new_recurring_need(unlocked, db, fixed_today):
    chk = checking(db, 100)
    gym = _suggested(db, "FitLife Gym", -4000, account=chk)
    _suggested(db, "Old one", -1000, days_ago=31)
    _suggested(db, "Refund club", 2500, account=chk)  # a deposit: not a "new repeating charge"
    active = _suggested(db, "Confirmed", -1000)
    active.status = "active"
    db.commit()
    assert needs_of(dash(unlocked), "new_recurring") == [{
        "key": f"newrec:{gym.id}", "kind": "new_recurring", "tone": "info", "dismissible": True,
        "fingerprint": str(gym.id),
        "data": {"recurring_id": gym.id, "name": "FitLife Gym", "amount": 40.0, "kind": "out", "cadence": "monthly",
                 "account_name": "Checking"},
    }]
    extra = [_suggested(db, f"Thing {n}", -500 * (n + 1)) for n in range(3)]
    need = one(dash(unlocked), "new_recurring")
    assert need["key"] == "newrec:many" and need["fingerprint"] == f"r{extra[-1].id}"
    assert need["data"] == {"count": 4, "names": ["FitLife Gym", "Thing 0", "Thing 1"]}
    switch(unlocked, "newrec", False)
    assert needs_of(dash(unlocked), "new_recurring") == []


def test_new_recurring_skips_hidden_accounts(unlocked, db, fixed_today):
    chk = checking(db, 100)
    hidden = make_account(db, "Old card", "credit", 0, hidden=True)
    _suggested(db, "Hidden gym", -4000, account=hidden)
    no_account = _suggested(db, "Manual thing", -1000)
    shown = _suggested(db, "FitLife Gym", -3000, account=chk)
    assert [a["key"] for a in needs_of(dash(unlocked), "new_recurring")] == [
        f"newrec:{no_account.id}", f"newrec:{shown.id}"]


def test_new_recurring_many_not_now_stays_until_a_newer_one(unlocked, db, fixed_today):
    """Review: after "Not now" on the roll-up, confirming some must not bring the rest back
    one by one; only an item newer than the roll-up's newest shows."""
    found = [_suggested(db, f"Thing {n}", -500 * (n + 1)) for n in range(5)]
    many = one(dash(unlocked), "new_recurring")
    assert many["key"] == "newrec:many"
    ok(unlocked.put(f"/api/home/dismissals/{many['key']}", json={"fingerprint": many["fingerprint"]}), 204)
    for item in found[:3]:  # the user confirms three: two left, would be single needs
        item.status = "active"
    db.commit()
    assert needs_of(dash(unlocked), "new_recurring") == []
    newer = _suggested(db, "Streaming", -1599)
    body = dash(unlocked)
    assert [a["key"] for a in _visible(body, "new_recurring")] == [f"newrec:{newer.id}"]
    more = [_suggested(db, f"New {n}", -100) for n in range(3)]
    (again,) = _visible(dash(unlocked), "new_recurring")
    assert again["key"] == "newrec:many" and again["fingerprint"] == f"r{more[-1].id}"
    assert again["data"]["count"] == 4  # only the ones newer than the dismissed roll-up


# ------------------------------------------------------------------ needs: large purchases


def test_big_purchase_need(unlocked, db, fixed_today):
    chk = checking(db, 5000)
    hidden = make_account(db, "Old card", "credit", 0, hidden=True)
    txn = add_txn(db, chk.id, "2026-09-25", -612.5, "BESTBUY #12", merchant="Best Buy")
    small = add_txn(db, chk.id, "2026-09-25", -20, "Coffee")
    evaluate(db, TODAY, [txn.id, small.id])  # the real "big" alert makes the event ($250 default)
    db.commit()
    event = db.scalar(select(AlertEvent).where(AlertEvent.key == "big"))
    old = add_txn(db, chk.id, "2026-09-20", -900, "Old purchase")
    gone = add_txn(db, hidden.id, "2026-09-25", -900, "Hidden")
    for t, days_ago, cleared in ((old, 4, False), (gone, 1, False), (txn, 1, True)):
        db.add(AlertEvent(key="big", severity="warn", title="Large transaction", body="", read=False,
                          created_at=utcnow() - dt.timedelta(days=days_ago), dedupe_key=f"big:{t.id}"
                          + ("" if t is not txn else ":x"), cleared=cleared))
    db.add(AlertEvent(key="big", severity="warn", title="Large", body="", read=False, created_at=utcnow(),
                      dedupe_key="big:999999", cleared=False))  # transaction gone (a pending one that posted)
    db.commit()
    assert needs_of(dash(unlocked), "big_purchase") == [{
        "key": f"big:{event.id}", "kind": "big_purchase", "tone": "info", "dismissible": True,
        "fingerprint": str(event.id),
        "data": {"event_id": event.id, "transaction_id": txn.id, "name": "Best Buy", "amount": 612.5,
                 "date": "2026-09-25", "account_name": "Checking", "account_mask": "4417"},
    }]
    switch(unlocked, "big", False)
    assert needs_of(dash(unlocked), "big_purchase") == []


def test_price_changes_are_no_longer_needs(unlocked, db, fixed_today):
    chk = checking(db, 100)
    item = make_item(db, "StreamFlix", -17.99, "monthly", "2026-10-01", account=chk)
    db.add(AlertEvent(key="price", severity="warn", title="StreamFlix went up", body="", read=False,
                      created_at=utcnow(), dedupe_key=f"price:{item.id}:1", cleared=False,
                      data=json.dumps({"recurring_id": item.id, "old_cents": -1599, "new_cents": -1799,
                                       "cadence": "monthly"})))
    db.commit()
    assert [a["kind"] for a in dash(unlocked)["needs"]] == ["low_balance"]  # no price_up


# ------------------------------------------------------------------ needs: order, kept kinds


def test_needs_sorted_by_tone_then_kind(unlocked, db, fixed_today, folder, wall):  # noqa: F811
    from app.services import automation

    chk = checking(db, 100)  # below $2,000 today: low_balance
    add_budget(db, "2026-09", {"FOOD_AND_DRINK": 1})
    add_txn(db, chk.id, "2026-09-05", -5, "Mystery", category=None)
    add_txn(db, chk.id, "2026-09-06", -5, "Diner")
    _suggested(db, "FitLife Gym", -4000)
    make_item(db, "Rent", -1200, "monthly", "2026-09-27", account=chk, source="manual", reminder=2)
    make_account(db, "Visa", "credit", 250, next_payment_due=D(2026, 9, 27))
    big = add_txn(db, chk.id, "2026-09-25", -600, "TV")
    evaluate(db, TODAY, [big.id], only=("big",))
    db.add_all([
        PlaidItem(plaid_item_id="p", access_token="t1", institution_name="P", kind="bank", status="pending"),
        PlaidItem(plaid_item_id="e", access_token="t2", institution_name="E", kind="bank", status="error",
                  error_code="X"),
        PlaidItem(plaid_item_id="l", access_token="t3", institution_name="L", kind="loan", status="login_required",
                  error_code="ITEM_LOGIN_REQUIRED"),
    ])
    db.commit()
    automation._put(db, automation.DIR_KEY, str(folder))
    automation._put(db, automation.LAST_ERROR_AT_KEY, "2026-09-25T03:00:00Z")
    db.commit()
    body = dash(unlocked)
    assert [a["kind"] for a in body["needs"]] == [
        "bank_signin", "bank_error", "low_balance", "over_plan", "backup_failed",
        "finish_setup", "bill_due", "card_due", "new_recurring", "needs_category", "big_purchase",
    ]
    assert [a["tone"] for a in body["needs"]] == ["act"] + ["warn"] * 4 + ["info"] * 6
    assert body["errors"] == []


# ------------------------------------------------------------------ section isolation


def test_a_failing_section_is_null_and_named(unlocked, db, fixed_today, monkeypatch, caplog):
    chk = checking(db, 100)
    add_budget(db, "2026-09", {"FOOD_AND_DRINK": 1})
    add_txn(db, chk.id, "2026-09-05", -5, "Mystery", category=None)
    add_txn(db, chk.id, "2026-09-06", -5, "Diner")

    def broken(*_a, **_k):
        raise RuntimeError("secret detail")

    real = spending.budget_month
    monkeypatch.setattr(spending, "budget_month", broken)
    body = dash(unlocked)
    assert body["budget"] is None and body["goals"] is None
    assert body["errors"] == ["budget", "goals", "needs"]  # over_plan can't be built either
    assert body["cash"]["total"] == 100.0 and body["coming_up"]["account"]["id"] == chk.id
    assert body["months"] is not None and body["investments"] is not None
    assert {a["kind"] for a in body["needs"]} == {"low_balance", "needs_category"}
    monkeypatch.setattr(spending, "budget_month", real)  # (undo() would also unpin today)

    monkeypatch.setattr(dashboard.calendar, "build_calendar", broken)
    monkeypatch.setattr(dashboard, "months", broken)
    monkeypatch.setattr(dashboard.investments, "totals", broken)
    body = dash(unlocked)
    assert body["coming_up"] is None and body["months"] is None and body["investments"] is None
    assert body["errors"] == ["coming_up", "months", "investments", "needs"]
    assert body["budget"]["days_left"] == 4 and {a["kind"] for a in body["needs"]} == {"over_plan", "needs_category"}
    assert "secret detail" not in caplog.text and "RuntimeError" in caplog.text


def test_a_switched_off_kind_is_never_built(unlocked, db, fixed_today, monkeypatch):
    checking(db, 100)

    def broken(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(dashboard, "_new_recurring_alerts", broken)
    assert dash(unlocked)["errors"] == ["needs"]
    switch(unlocked, "newrec", False)
    assert dash(unlocked)["errors"] == []


# ------------------------------------------------------------------ "Not now"


def test_dismissal_keys_for_the_new_kinds(unlocked):
    for key, fp in (("over_plan:c_7", "2026-09"), ("over_plan:many", "2026-09:4"), ("newrec:many", "r12"),
                    ("bill_due:12", "2026-10-01"), ("card_due:3", "2026-09-28"), ("big:41", "41"),
                    ("over_plan:FOOD_AND_DRINK", "2026-09")):
        ok(unlocked.put(f"/api/home/dismissals/{key}", json={"fingerprint": fp}), 204)
    dismissed = dash(unlocked)["dismissed"]
    assert dismissed["over_plan:many"] == "2026-09:4" and dismissed["big:41"] == "41" and len(dismissed) == 7
    ok(unlocked.delete("/api/home/dismissals/big:41"), 204)
    assert "big:41" not in dash(unlocked)["dismissed"]


def test_setting_row_missing_counts_as_on(unlocked, db, fixed_today):
    """A deleted alert_settings row is on (it is re-seeded enabled)."""
    checking(db, 100)
    db.delete(db.get(AlertSetting, "low"))
    db.commit()
    assert len(needs_of(dash(unlocked), "low_balance")) == 1



def test_fonts_are_served_with_font_types(spa_client):
    # Home v2's number font ships in dist/assets; every response is nosniff, so the type matters.
    dist = spa_client.app.state.fintrack.settings.frontend_dist
    (dist / "assets" / "plex-500.woff2").write_bytes(b"wOF2 fake")
    (dist / "assets" / "plex-500.woff").write_bytes(b"wOFF fake")
    r = spa_client.get("/assets/plex-500.woff2")
    assert r.status_code == 200 and r.headers["content-type"] == "font/woff2"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert spa_client.get("/assets/plex-500.woff").headers["content-type"] == "font/woff"


# ------------------------------------------------------------------ Release 3.10: update_balance


def _manual(db, name, category="bank", balance=10.0, last=None, **kw):
    from app.models import BalanceSnapshot

    a = make_account(db, name, category, balance, **kw)
    if last is not None:
        db.add(BalanceSnapshot(account_id=a.id, date=last, balance_cents=round(balance * 100)))
        db.commit()
    return a


def test_update_balance_need(unlocked, db, fixed_today):
    fresh = _manual(db, "Fresh jar", last=D(2026, 9, 20))  # 6 days: not yet
    week = _manual(db, "Week jar", last=D(2026, 9, 19), mask="0042", institution_name="Credit Union")  # 7 days
    older = _manual(db, "Car loan", "loan", 9000, last=D(2026, 8, 1))
    _manual(db, "Hidden jar", last=D(2026, 1, 1), hidden=True)
    checking(db, 5000)  # bank-connected: never asked
    body = dash(unlocked)
    found = needs_of(body, "update_balance")
    assert [n["key"] for n in found] == [f"update_balance:{older.id}", f"update_balance:{week.id}"]  # stalest first
    assert found[1] == {"key": f"update_balance:{week.id}", "kind": "update_balance", "tone": "warn",
                        "dismissible": True, "fingerprint": "2026-09-19",
                        "data": {"account_id": week.id, "name": "Week jar", "mask": "0042",
                                 "institution_name": "Credit Union", "days": 7, "balance_date": "2026-09-19",
                                 "account_category": "bank"}}
    assert fresh.id not in [n["data"]["account_id"] for n in found]
    assert dashboard.KIND_ORDER.index("update_balance") == dashboard.KIND_ORDER.index("card_due") + 1

    # "Not now" lasts until a new balance is typed in (the fingerprint is the balance date).
    ok(unlocked.put(f"/api/home/dismissals/update_balance:{week.id}", json={"fingerprint": "2026-09-19"}), 204)
    assert dash(unlocked)["dismissed"][f"update_balance:{week.id}"] == "2026-09-19"
    ok(unlocked.put(f"/api/accounts/{week.id}/balance", json={"balance": 12}))
    assert week.id not in [n["data"]["account_id"] for n in needs_of(dash(unlocked), "update_balance")]
    fixed_today.set(D(2026, 10, 3))  # 7 days after that save: back, with a new fingerprint
    again = [n for n in needs_of(dash(unlocked), "update_balance") if n["data"]["account_id"] == week.id]
    assert again[0]["fingerprint"] == "2026-09-26"


def test_update_balance_need_is_capped(unlocked, db, fixed_today):
    for i in range(12):
        _manual(db, f"Jar {i:02d}", last=D(2026, 9, 1) + dt.timedelta(days=i % 3))
    assert len(needs_of(dash(unlocked), "update_balance")) == 10


def test_update_balance_need_failure_is_isolated(unlocked, db, fixed_today, monkeypatch):
    _manual(db, "Old jar", last=D(2026, 8, 1))

    def boom(*_a, **_k):
        raise RuntimeError("nope")

    monkeypatch.setattr(dashboard, "last_balance_dates", boom)
    body = dash(unlocked)
    assert "needs" in body["errors"] and body["cash"] is not None

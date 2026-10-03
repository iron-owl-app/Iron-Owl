"""Release 3.19 round 2: every place that matches charges to a recurring item also sees the
item's other names/accounts (aliases); hidden accounts' alias rows never count; a broken
note never breaks anything."""
from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models import Account, AlertEvent, AppSetting, RecurringItem
from app.services import alerts, calendar, recurring, reports, spending
from app.services.categories import load as load_categories
from app.services.forecast import daily_spend_cents
from app.services.recurring import MAX_ALIASES, MAX_JOIN_SERIES, MEMO_KEY, _across_accounts, _Series
from tests.conftest import add_txn, make_account

D = dt.date
TODAY = D(2026, 9, 26)  # conftest FIXED_TODAY


def ok(response, status: int = 200):
    assert response.status_code == status, response.text
    return response.json() if response.content else None


@pytest.fixture
def env(unlocked, db, fixed_today):
    chk = make_account(db, "Checking", "bank", 5000, plaid_type="depository", plaid_subtype="checking")
    card = make_account(db, "Visa", "credit", 200, plaid_type="credit", plaid_subtype="credit card")
    old = make_account(db, "Old Visa", "credit", 0, plaid_type="credit", plaid_subtype="credit card")
    return SimpleNamespace(client=unlocked, db=db, chk=chk, card=card, old=old)


def make(db, name, key, account_id, cents, **kw) -> RecurringItem:
    fields = dict(cadence="monthly", next_date=D(2026, 10, 10), month_days="[10]", status="active",
                  source="detected", last_seen_date=D(2026, 9, 10))
    fields.update(kw)
    item = RecurringItem(name=name, merchant_key=key, account_id=account_id, amount_cents=cents, **fields)
    db.add(item)
    db.commit()
    return item


def note(db, item, aliases) -> None:
    """Detection's note for ``item``: its amount and account are detection's, plus aliases."""
    row = db.get(AppSetting, MEMO_KEY)
    data = json.loads(row.value) if row is not None else {}
    data[str(item.id)] = {"key": item.merchant_key, "created": recurring._stamp(item), "amount": item.amount_cents,
                          "account": item.account_id, "aliases": [list(a) for a in aliases]}
    if row is None:
        db.add(AppSetting(key=MEMO_KEY, value=json.dumps(data)))
    else:
        row.value = json.dumps(data)
    db.commit()


def hide(db, account) -> None:
    db.get(Account, account.id).hidden = True
    db.commit()


MODES = ("alias", "none", "hidden")  # with the alias / no note / the alias account hidden


def setup_mode(db, mode, item, aliases, hidden_account) -> None:
    if mode != "none":
        note(db, item, aliases)
    if mode == "hidden":
        hide(db, hidden_account)


# ------------------------------------------------------------------ the shared helpers


def test_item_aliases_leave_out_hidden_accounts(env):
    db = env.db
    item = make(db, "Netflix", "netflix inc", env.card.id, -1549)
    note(db, item, [(env.old.id, "netflix.com"), (env.chk.id, "netflix")])
    assert recurring.item_aliases(db, [item]) == {item.id: [(env.old.id, "netflix.com"), (env.chk.id, "netflix")]}
    hide(db, env.old)
    assert recurring.item_aliases(db, [item]) == {item.id: [(env.chk.id, "netflix")]}
    hide(db, env.chk)
    assert recurring.item_aliases(db, [item]) == {}
    aliases = {item.id: [(env.old.id, "netflix.com")]}
    assert recurring.match_keys(item, aliases) == {"netflix inc", "netflix.com"}
    assert recurring.claims(item, aliases, env.card.id, "netflix inc")
    assert recurring.claims(item, aliases, env.old.id, "netflix.com")
    assert not recurring.claims(item, aliases, env.card.id, "netflix.com")  # alias is account-exact
    assert not recurring.claims(item, {}, env.old.id, "netflix inc")


# ------------------------------------------------------------------ each place


@pytest.mark.parametrize("mode", MODES)
def test_subscriptions_count_old_name_charges(env, mode):
    db = env.db
    for m in range(-1, 7):  # Nov 2025 .. Jun 2026 at the old price, old name, old card
        day = D(2025 + (10 + m) // 12, (10 + m) % 12 + 1, 10)
        add_txn(db, env.old.id, day, -15.49, "NETFLIX.COM", merchant="Netflix.com", category="ENTERTAINMENT")
    for m in (7, 8, 9):
        add_txn(db, env.card.id, D(2026, m, 10), -17.99, "NETFLIX INC", merchant="Netflix Inc",
                category="ENTERTAINMENT")
    item = make(db, "Netflix", "netflix inc", env.card.id, -1799, category_id="ENTERTAINMENT")
    setup_mode(db, mode, item, [(env.old.id, "netflix.com")], env.old)
    (sub,) = ok(env.client.get("/api/reports/subscriptions"))["items"]
    if mode == "alias":
        assert sub["change"] == {"amount": 2.5, "month": "2026-07"} and sub["full_year"] is True
    else:  # as before: only the new name's charges
        assert sub["change"] is None and sub["full_year"] is False


@pytest.mark.parametrize("mode", MODES)
def test_report_bills_count_old_name_charges(env, mode):
    db = env.db
    for m in (4, 5, 6):
        add_txn(db, env.chk.id, D(2026, m, 10), -60, "GOLDS GYM", merchant="Golds Gym", category="PERSONAL_CARE")
    for m in (7, 8, 9):
        add_txn(db, env.card.id, D(2026, m, 10), -60, "GOLDS GYM INC", merchant="Golds Gym Inc",
                category="PERSONAL_CARE")
    item = make(db, "Gym", "golds gym inc", env.card.id, -6000)
    setup_mode(db, mode, item, [(env.chk.id, "golds gym")], env.chk)
    bills = reports.bill_ids(db, load_categories(db), TODAY)
    # With the alias every gym charge is the bill's; without it half are (BILL_SHARE 75%).
    # A hidden account's rows don't count anywhere in reports, so only the new name's remain.
    assert ("PERSONAL_CARE" in bills) == (mode in ("alias", "hidden"))


@pytest.mark.parametrize("mode", MODES)
def test_price_tracking_sees_the_new_name(env, mode):
    db = env.db
    for m in (6, 7, 8):
        add_txn(db, env.card.id, D(2026, m, 10), -15.49, "NETFLIX INC", merchant="Netflix Inc")
    add_txn(db, env.old.id, D(2026, 9, 10), -17.99, "NETFLIX.COM", merchant="Netflix.com")
    item = make(db, "Netflix", "netflix inc", env.card.id, -1549)
    setup_mode(db, mode, item, [(env.old.id, "netflix.com")], env.old)
    alerts.evaluate(db, TODAY, only=["price"])
    db.commit()
    db.refresh(item)
    assert item.amount_cents == (-1799 if mode == "alias" else -1549)


@pytest.mark.parametrize("mode", MODES)
def test_forecast_daily_spend_leaves_out_old_name(env, mode):
    db = env.db
    for day in ("2026-07-05", "2026-08-05", "2026-09-05"):
        add_txn(db, env.chk.id, day, -90, "PF CLUB FEES", merchant="PF Club Fees", category="PERSONAL_CARE")
    add_txn(db, env.chk.id, "2026-09-20", -9, "LUNCH", merchant="Deli", category="FOOD_AND_DRINK")
    item = make(db, "Gym", "planet fitness", env.chk.id, -9000, next_date=D(2026, 10, 5), month_days="[5]")
    setup_mode(db, mode, item, [(env.chk.id, "pf club fees")], env.chk)
    chk = db.get(Account, env.chk.id)
    daily = daily_spend_cents(db, chk, TODAY, [item])
    with_gym = daily_spend_cents(db, chk, TODAY, [])
    if mode == "alias":
        assert daily < with_gym
    else:  # no alias, or its account hidden: counted as before
        assert daily == with_gym


@pytest.mark.parametrize("mode", MODES)
def test_paycheck_transfer_evidence_counts_old_name(env, mode):
    db = env.db
    savings = make_account(db, "Savings", "bank", 100, plaid_type="depository", plaid_subtype="savings")
    acct = savings if mode == "hidden" else env.chk
    for m in (2, 3, 4, 5, 6, 7):
        add_txn(db, acct.id, D(2026, m, 1), 2000, "ACME CORP XFER", category="TRANSFER_IN", is_transfer=True)
    for m in (8, 9):
        add_txn(db, env.chk.id, D(2026, m, 1), 2000, "ACME PAYROLL", category="INCOME")
    item = make(db, "Acme", "acme payroll", env.chk.id, 200000, next_date=D(2026, 10, 1), month_days="[1]")
    setup_mode(db, mode, item, [(acct.id, "acme corp xfer")], savings)
    assert (item.id in spending.transfer_items(db, [item])) == (mode == "alias")


@pytest.mark.parametrize("mode", MODES)
def test_calendar_matches_alias_rows_except_hidden(env, mode):
    db = env.db
    add_txn(db, env.card.id, D(2026, 8, 10), -15.49, "NETFLIX INC", merchant="Netflix Inc")
    add_txn(db, env.old.id, D(2026, 9, 10), -15.49, "NETFLIX.COM", merchant="Netflix.com")
    item = make(db, "Netflix", "netflix inc", env.card.id, -1549)
    setup_mode(db, mode, item, [(env.old.id, "netflix.com")], env.old)
    occ = {o.base_date: o for o in calendar.occurrences_for(db, TODAY, D(2026, 8, 1), D(2026, 9, 30))}
    assert occ[D(2026, 8, 10)].status == "paid"
    assert (occ[D(2026, 9, 10)].status == "paid") == (mode == "alias")


# ------------------------------------------------------------------ broken notes and limits


BROKEN = [
    "{", "[" * 100000 + "]" * 100000, '"text"', "[1, 2]", "null",
    json.dumps({"9" * 5000: {}}), json.dumps({"-1": {}}), json.dumps({"²": {}}), json.dumps({"1": "x"}),
]


def broken_entries(item) -> list[str]:
    base = {"key": item.merchant_key, "created": recurring._stamp(item), "amount": item.amount_cents,
            "account": item.account_id}
    return [json.dumps({str(item.id): {**base, "aliases": aliases}}) for aliases in (
        5, "abc", {"a": 1}, [["x", "netflix"], [1], "a", [1, 2], [True, "k"], [None, "k"], [[1], "k"], [1, "k", 3]],
    )] + [json.dumps({str(item.id): {**base, "amount": "12", "account": [1], "aliases": []}}),
          json.dumps({str(item.id): {**base, "key": 5, "aliases": []}})]


def test_broken_notes_never_break_anything(env):
    client, db = env.client, env.db
    for m in (6, 7, 8, 9):
        add_txn(db, env.chk.id, D(2026, m, 10), -15.49, "NETFLIX", merchant="Netflix", category="ENTERTAINMENT")
    item = make(db, "Netflix", "netflix", env.chk.id, -1549, category_id="ENTERTAINMENT")
    ok(client.put("/api/forecast/account", json={"account_id": env.chk.id}))
    for value in BROKEN + broken_entries(item):
        db.expire_all()
        row = db.get(AppSetting, MEMO_KEY)
        if row is None:
            db.add(AppSetting(key=MEMO_KEY, value=value))
        else:
            row.value = value
        db.commit()
        assert recurring.item_aliases(db, [db.get(RecurringItem, item.id)]) == {}
        recurring.detect(db, TODAY)
        db.rollback()  # leave the broken note in place for the reads below
        cal = ok(client.get("/api/forecast/calendar", params={"from": "2026-09-01", "to": "2026-10-31"}))
        assert any(o["date"] == "2026-09-10" and o["status"] == "paid" for o in cal["occurrences"])
        assert calendar.bill_occurrences(db, TODAY, D(2026, 9, 1), D(2026, 10, 31))["ENTERTAINMENT"]
        ok(client.get("/api/dashboard"))
        ok(client.get("/api/reports/subscriptions"))
        ok(client.post("/api/recurring/detect", json={}))


def test_aliases_are_capped(env):
    db = env.db
    item = make(db, "Netflix", "netflix", env.card.id, -1549)
    pairs = [(env.card.id, f"netflix {chr(97 + i)}{chr(97 + i)}") for i in range(MAX_ALIASES + 5)]
    memo = {item.id: {"key": item.merchant_key, "created": recurring._stamp(item), "amount": -1549,
                      "account": env.card.id, "aliases": list(pairs)}}
    recurring._save_memo(db, memo)
    db.commit()
    stored = json.loads(db.get(AppSetting, MEMO_KEY).value)[str(item.id)]["aliases"]
    assert [tuple(a) for a in stored] == pairs[-MAX_ALIASES:]  # the most recent ones
    note(db, item, pairs)  # a longer list written some other way is cut when read
    assert recurring.item_aliases(db, [item]) == {item.id: pairs[-MAX_ALIASES:]}


def test_no_cross_account_join_for_very_many_series():
    def series(account_id, day):
        row = SimpleNamespace(id=account_id, account_id=account_id, date=day, amount_cents=-1549)
        return _Series([(account_id, "netflix")], [row])

    def months(n):
        return [series(i + 1, D(2024, 1, 10) + dt.timedelta(days=30 * i)) for i in range(n)]

    assert len(_across_accounts(months(3))) == 1  # one bill that moved card twice
    assert len(_across_accounts(months(MAX_JOIN_SERIES + 1))) == MAX_JOIN_SERIES + 1


def test_price_tracking_leaves_a_hand_set_amount(env):
    db = env.db
    for m in (7, 8):
        add_txn(db, env.card.id, D(2026, m, 10), -15.49, "NETFLIX", merchant="Netflix")
    sep = add_txn(db, env.card.id, D(2026, 9, 10), -17.99, "NETFLIX", merchant="Netflix")
    mine = make(db, "Netflix", "netflix", env.card.id, -1549)
    note(db, mine, [])
    followed = make(db, "Netflix 2", "netflix", env.card.id, -1549)
    note(db, followed, [])
    no_note = make(db, "Netflix 3", "netflix", env.card.id, -1600)  # edited by hand, but no note: D7
    mine.amount_cents = -1600  # the user typed it (PATCH)
    db.commit()
    alerts.evaluate(db, TODAY, only=["price"])
    db.commit()
    db.expire_all()
    assert db.get(RecurringItem, mine.id).amount_cents == -1600
    assert db.get(RecurringItem, followed.id).amount_cents == -1799
    assert db.get(RecurringItem, no_note.id).amount_cents == -1799
    # The price event is still recorded for every item, as in D7 (a cleared tombstone).
    keys = set(db.scalars(select(AlertEvent.dedupe_key).where(AlertEvent.key == "price")))
    assert keys == {f"price:{i.id}:{sep.id}" for i in (mine, followed, no_note)}
    # The note follows too, so the new amount is still detection's (not theirs).
    items = [db.get(RecurringItem, i.id) for i in (mine, followed, no_note)]
    assert recurring.users_amounts(db, items) == {mine.id}

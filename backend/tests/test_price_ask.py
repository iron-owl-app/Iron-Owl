"""Release 3.19: "Netflix now charges $17.99. Update your amount?" for bills whose amount the user set
by hand (Home's needs and the Recurring list), Yes / No, the "price" switch and broken storage.
Plus the security review minors: one owner per alias (reading and detection) and Subscriptions'
tie-break."""
from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models import Account, AppSetting, RecurringItem
from app.services import alerts, price_ask, recurring, reports
from app.services.recurring import MEMO_KEY
from tests.conftest import add_txn, make_account

D = dt.date
TODAY = D(2026, 9, 26)  # conftest FIXED_TODAY


def ok(response, status: int = 200):
    assert response.status_code == status, response.text
    return response.json() if response.content else None


@pytest.fixture
def env(unlocked, db, fixed_today):
    card = make_account(db, "Visa", "credit", 200, plaid_type="credit", plaid_subtype="credit card")
    card2 = make_account(db, "Mastercard", "credit", 100, plaid_type="credit", plaid_subtype="credit card")
    return SimpleNamespace(client=unlocked, db=db, card=card, card2=card2)


def make(db, name, key, account_id, cents, **kw) -> RecurringItem:
    fields = dict(cadence="monthly", next_date=D(2026, 10, 10), month_days="[10]", status="active",
                  source="detected", last_seen_date=D(2026, 9, 10))
    fields.update(kw)
    item = RecurringItem(name=name, merchant_key=key, account_id=account_id, amount_cents=cents, **fields)
    db.add(item)
    db.commit()
    return item


def note(db, item, amount, aliases=()) -> None:
    """Detection's note: ``amount`` is what detection last wrote (None = theirs for good)."""
    row = db.get(AppSetting, MEMO_KEY)
    data = json.loads(row.value) if row is not None else {}
    data[str(item.id)] = {"key": item.merchant_key, "created": recurring._stamp(item), "amount": amount,
                          "account": item.account_id, "aliases": [list(a) for a in aliases]}
    if row is None:
        db.add(AppSetting(key=MEMO_KEY, value=json.dumps(data)))
    else:
        row.value = json.dumps(data)
    db.commit()


def charge(db, account_id, day, amount, name="NETFLIX"):
    return add_txn(db, account_id, day, amount, name, merchant=name.title(), category="ENTERTAINMENT")


def check(db) -> None:
    db.expire_all()
    alerts.evaluate(db, TODAY, only=["price"])
    db.commit()
    db.expire_all()


def questions(client) -> dict[int, dict]:
    return {i["id"]: i["price_question"] for i in ok(client.get("/api/recurring")) if i["price_question"]}


def home(client) -> list[dict]:
    return [n for n in ok(client.get("/api/dashboard"))["needs"] if n["kind"] == "price_change"]


def amount(db, item) -> int:
    db.expire_all()
    return db.get(RecurringItem, item.id).amount_cents


# ------------------------------------------------------------------ who is asked


def test_only_hand_set_bills_are_asked(env):
    client, db = env.client, env.db
    for m in (7, 8):
        charge(db, env.card.id, D(2026, m, 10), -15.49)
    sep = charge(db, env.card.id, D(2026, 9, 10), -17.99)
    detected = make(db, "Netflix", "netflix", env.card.id, -1549)
    note(db, detected, -1549)  # detection's amount
    mine = make(db, "Netflix 2", "netflix", env.card.id, -1600)
    note(db, mine, -1549)  # the user typed $16.00
    manual = make(db, "Netflix 3", "netflix", None, -1549, source="manual", last_seen_date=None)
    no_note = make(db, "Netflix 4", "netflix", env.card.id, -1600)  # D7: follows
    check(db)
    assert amount(db, detected) == -1799 and amount(db, no_note) == -1799  # silently, as before
    assert amount(db, mine) == -1600 and amount(db, manual) == -1549  # never overwritten
    q = questions(client)
    assert set(q) == {mine.id, manual.id}
    assert q[mine.id] == {"transaction_id": sep.id, "amount": -17.99, "date": "2026-09-10"}
    needs = {n["data"]["recurring_id"]: n for n in home(client)}
    assert set(needs) == {mine.id, manual.id}
    n = needs[mine.id]
    assert (n["key"], n["tone"], n["dismissible"], n["fingerprint"]) == (f"price:{mine.id}", "warn", True, str(sep.id))
    assert n["data"] == {"recurring_id": mine.id, "name": "Netflix 2", "amount": 17.99, "current": 16.0,
                         "income": False, "date": "2026-09-10", "transaction_id": sep.id}
    check(db)  # the same charge never asks twice or changes anything
    assert set(questions(client)) == {mine.id, manual.id}


def test_paychecks_ask_small_changes_once_and_hidden_do_not(env):
    client, db = env.client, env.db
    charge(db, env.card.id, D(2026, 9, 10), -15.70)  # 21 cents: not a price change
    small = make(db, "Netflix", "netflix", None, -1549, source="manual")
    pay_txn = add_txn(db, env.card.id, D(2026, 9, 15), 2410, "ACME PAYROLL", merchant="Acme Payroll")
    pay = make(db, "Acme Payroll", "acme payroll", None, 200000, source="manual")
    charge(db, env.card2.id, D(2026, 9, 12), -99, "GYM")
    once = make(db, "Gym", "gym", None, -5000, source="manual", cadence="once", month_days=None)
    charge(db, env.card2.id, D(2026, 9, 14), -30, "HULU")
    hulu = make(db, "Hulu", "hulu", env.card2.id, -2000, source="manual")
    check(db)
    assert all(amount(db, i) == c for i, c in ((small, -1549), (pay, 200000), (once, -5000), (hulu, -2000)))
    q = questions(client)
    assert set(q) == {pay.id, hulu.id}
    assert q[pay.id] == {"transaction_id": pay_txn.id, "amount": 2410.0, "date": "2026-09-15"}
    need = next(n for n in home(client) if n["data"]["recurring_id"] == pay.id)
    assert (need["data"]["income"], need["data"]["amount"], need["data"]["current"]) == (True, 2410.0, 2000.0)
    # Yes on a paycheck: the deposit's amount (money in).
    ok(client.patch(f"/api/recurring/{pay.id}", json={"amount": 2410}))
    assert amount(db, pay) == 241000 and pay.id not in questions(client)
    db.get(Account, env.card2.id).hidden = True
    db.commit()
    assert questions(client) == {} and home(client) == []


def test_hidden_card_charges_never_count(env):
    """An item without an account must not follow, or ask about, a charge on a hidden card."""
    client, db = env.client, env.db
    db.get(Account, env.card2.id).hidden = True
    db.commit()
    charge(db, env.card2.id, D(2026, 9, 10), -17.99)
    mine = make(db, "Netflix", "netflix", None, -1549, source="manual")
    detected = make(db, "Netflix 2", "netflix", None, -1549)  # no note: would follow (D7)
    check(db)
    assert amount(db, mine) == -1549 and amount(db, detected) == -1549
    assert questions(client) == {} and db.get(AppSetting, price_ask.KEY) is None


def test_not_now_hides_it_from_home_only(env):
    client, db = env.client, env.db
    mine = make(db, "Netflix", "netflix", None, -1549, source="manual")
    sep = charge(db, env.card.id, D(2026, 9, 1), -17.99)
    check(db)
    ok(client.put(f"/api/home/dismissals/price:{mine.id}", json={"fingerprint": str(sep.id)}), 204)
    data = ok(client.get("/api/dashboard"))
    assert data["dismissed"][f"price:{mine.id}"] == str(sep.id)  # the client hides it while this matches
    assert set(questions(client)) == {mine.id}  # still on the bill until answered
    later = charge(db, env.card.id, D(2026, 9, 20), -19.99)
    check(db)
    assert home(client)[0]["fingerprint"] == str(later.id)  # a new charge: back on Home


# ------------------------------------------------------------------ Yes and No


def test_yes_takes_the_new_amount_and_the_next_price_asks_again(env):
    client, db = env.client, env.db
    charge(db, env.card.id, D(2026, 8, 10), -17.99)
    mine = make(db, "Netflix", "netflix", env.card.id, -1549)
    note(db, mine, -1799)  # detection last wrote $17.99, the user typed $15.49
    sep = charge(db, env.card.id, D(2026, 9, 10), -17.99)
    check(db)
    assert questions(client)[mine.id]["transaction_id"] == sep.id
    # Yes: the new charge's amount, through the usual PATCH. It equals detection's old note,
    # but stays theirs.
    ok(client.patch(f"/api/recurring/{mine.id}", json={"amount": -17.99}))
    assert amount(db, mine) == -1799
    assert questions(client) == {} and home(client) == []
    db.expire_all()
    assert recurring.users_amounts(db, [db.get(RecurringItem, mine.id)]) == {mine.id}
    nxt = charge(db, env.card.id, D(2026, 9, 24), -19.99)
    check(db)
    assert amount(db, mine) == -1799  # not followed: asked
    assert questions(client)[mine.id] == {"transaction_id": nxt.id, "amount": -19.99, "date": "2026-09-24"}


def test_no_dismisses_only_that_charge(env):
    client, db = env.client, env.db
    mine = make(db, "Netflix", "netflix", None, -1549, source="manual")
    sep = charge(db, env.card.id, D(2026, 9, 1), -17.99)
    check(db)
    body = {"transaction_id": sep.id, "answer": "no"}
    ok(client.post(f"/api/recurring/{mine.id}/price-question", json=body), 204)
    assert amount(db, mine) == -1549 and questions(client) == {} and home(client) == []
    ok(client.post(f"/api/recurring/{mine.id}/price-question", json=body), 204)  # again: nothing changes
    check(db)
    assert questions(client) == {}  # the same charge never asks again
    # Undo of No asks again; No once more.
    ok(client.post(f"/api/recurring/{mine.id}/price-question", json={**body, "answer": "undo"}), 204)
    assert set(questions(client)) == {mine.id}
    ok(client.post(f"/api/recurring/{mine.id}/price-question", json=body), 204)
    # Owner's decision: every later charge at a price other than theirs asks, even the same $17.99.
    same = charge(db, env.card.id, D(2026, 9, 15), -17.99)
    check(db)
    assert questions(client)[mine.id] == {"transaction_id": same.id, "amount": -17.99, "date": "2026-09-15"}
    later = charge(db, env.card.id, D(2026, 9, 20), -19.99)
    check(db)
    assert questions(client)[mine.id]["transaction_id"] == later.id
    # A No for an old charge doesn't answer the new question.
    ok(client.post(f"/api/recurring/{mine.id}/price-question", json=body), 204)
    assert set(questions(client)) == {mine.id}


def test_price_back_to_their_amount_or_edited_clears_the_question(env):
    client, db = env.client, env.db
    mine = make(db, "Netflix", "netflix", None, -1549, source="manual")
    charge(db, env.card.id, D(2026, 9, 1), -17.99)
    check(db)
    assert set(questions(client)) == {mine.id}
    charge(db, env.card.id, D(2026, 9, 20), -15.49)
    check(db)
    assert questions(client) == {}
    assert price_ask.load(db, [db.get(RecurringItem, mine.id)]) == {}


def test_answer_route_checks(env):
    client, db = env.client, env.db
    mine = make(db, "Netflix", "netflix", None, -1549, source="manual")
    url = f"/api/recurring/{mine.id}/price-question"
    ok(client.post("/api/recurring/999999/price-question", json={"transaction_id": 1, "answer": "no"}), 404)
    for bad_id in ("0", "-1", str(2**63), "x"):
        ok(client.post(f"/api/recurring/{bad_id}/price-question", json={"transaction_id": 1, "answer": "no"}), 422)
    for bad in ({"transaction_id": 1}, {"transaction_id": 0, "answer": "no"}, {"transaction_id": 1, "answer": "yes"},
                {"transaction_id": 1, "answer": "no", "extra": 1}, {"transaction_id": "1x", "answer": "no"}):
        ok(client.post(url, json=bad), 422)
    ok(client.post(url, json={"transaction_id": 5, "answer": "no"}), 204)  # no question: nothing happens
    assert db.get(AppSetting, price_ask.KEY) is None


# ------------------------------------------------------------------ the switch and storage


def test_switch_off_asks_nothing(env):
    client, db = env.client, env.db
    mine = make(db, "Netflix", "netflix", None, -1549, source="manual")
    charge(db, env.card.id, D(2026, 9, 1), -17.99)
    check(db)
    assert set(questions(client)) == {mine.id}
    ok(client.put("/api/alerts/settings/price", json={"enabled": False}))
    assert questions(client) == {} and home(client) == []  # an open one is hidden too
    later = charge(db, env.card.id, D(2026, 9, 20), -19.99)
    check(db)
    assert amount(db, mine) == -1549
    ok(client.put("/api/alerts/settings/price", json={"enabled": True}))
    assert questions(client)[mine.id]["transaction_id"] != later.id  # nothing new was recorded while off
    check(db)
    assert questions(client)[mine.id]["transaction_id"] == later.id


BROKEN = ["{", "[]", "5", "null", '"x"', "[" * 5000 + "]" * 5000, '{"a": 1}', '{"99999999999999999999999": {}}']


def broken_entries(item) -> list[str]:
    base = {"key": item.merchant_key, "created": recurring._stamp(item), "txn": 1, "cents": -1799,
            "date": "2026-09-01", "kept": None}
    return [json.dumps({str(item.id): {**base, **change}}) for change in (
        {"txn": "1"}, {"cents": 17.99}, {"cents": True}, {"date": "2026-13-01"}, {"date": 5}, {"kept": "x"},
        {"key": "other"}, {"created": ""}, {"txn": None},
        {"cents": -(price_ask.MAX_CENTS + 1)}, {"cents": 10**30}, {"kept": price_ask.MAX_CENTS + 1},
    )] + [json.dumps({str(item.id): 5}), json.dumps({"x": base})]


def test_broken_storage_never_breaks_home_or_recurring(env):
    client, db = env.client, env.db
    mine = make(db, "Netflix", "netflix", None, -1549, source="manual")
    sep = charge(db, env.card.id, D(2026, 9, 1), -17.99)
    for value in BROKEN + broken_entries(mine):
        db.expire_all()
        row = db.get(AppSetting, price_ask.KEY)
        if row is None:
            db.add(AppSetting(key=price_ask.KEY, value=value))
        else:
            row.value = value
        db.commit()
        assert questions(client) == {}
        data = ok(client.get("/api/dashboard"))
        assert "needs" not in data["errors"] and not [n for n in data["needs"] if n["kind"] == "price_change"]
        ok(client.post(f"/api/recurring/{mine.id}/price-question", json={"transaction_id": 1, "answer": "no"}), 204)
    check(db)  # the next check writes a clean value with the real question
    assert questions(client)[mine.id]["transaction_id"] == sep.id


def test_storage_is_bounded(env):
    db = env.db
    items = [make(db, f"Bill {i}", f"bill {i}", None, -1000, source="manual") for i in range(price_ask.MAX_ENTRIES + 5)]
    data = {i.id: {"key": i.merchant_key, "created": recurring._stamp(i), "txn": n + 1, "cents": -1500,
                   "date": "2026-09-01", "kept": None} for n, i in enumerate(items)}
    price_ask.save(db, data)
    db.commit()
    kept = price_ask.load(db, items)
    assert len(kept) == price_ask.MAX_ENTRIES and min(e["txn"] for e in kept.values()) == 6  # the newest


# ------------------------------------------------------------------ security review minors


def test_detection_never_aliases_a_hand_added_bills_name(env):
    client, db = env.client, env.db
    for m in (5, 6, 7, 8):
        charge(db, env.card.id, D(2026, m, 10), -15.49)
    (item,) = [db.get(RecurringItem, i) for i in recurring.detect(db, D(2026, 8, 20))]
    db.commit()
    ok(client.patch(f"/api/recurring/{item.id}", json={"status": "active"}))
    ok(client.post("/api/recurring", json={"name": "Netflix", "amount": -15.49, "cadence": "monthly",
                                           "next_date": "2026-10-10"}), 201)  # no account: "netflix" anywhere
    charge(db, env.card2.id, D(2026, 9, 10), -15.49)  # the bill moved to another card
    db.expire_all()
    assert recurring.detect(db, TODAY) == []
    db.commit()
    memo = json.loads(db.get(AppSetting, MEMO_KEY).value)
    assert memo[str(item.id)]["aliases"] == []  # "netflix" belongs to the hand-added bill


def test_item_aliases_one_owner_when_reading(env):
    db = env.db
    a = make(db, "Netflix", "netflix", env.card.id, -1549)
    b = make(db, "Hulu", "hulu", env.card.id, -999)
    c = make(db, "Gym", "gym", None, -3000, source="manual")
    d = make(db, "Spotify", "spotify", env.card.id, -1199)
    note(db, a, -1549, [(env.card2.id, "netflix"), (env.card.id, "hulu"), (env.card2.id, "gym"), (env.card2.id, "x")])
    note(db, d, -1199, [(env.card2.id, "x"), (env.card2.id, "spotify")])
    got = recurring.item_aliases(db, [a, b, c, d])
    # b's own pair, c's any-account key and the pair a (lower id) already lists are dropped.
    assert got == {a.id: [(env.card2.id, "netflix"), (env.card2.id, "x")], d.id: [(env.card2.id, "spotify")]}
    assert recurring.item_aliases(db, [d]) == {d.id: [(env.card2.id, "spotify")]}  # same answer for a subset


def test_subscriptions_tie_counts_a_charge_once():
    a = SimpleNamespace(id=1, merchant_key="netflix", account_id=None, amount_cents=-1000)
    b = SimpleNamespace(id=2, merchant_key="netflix", account_id=None, amount_cents=-1200)
    row = SimpleNamespace(id=9, account_id=3, date=D(2026, 9, 1), amount_cents=-1100, name="NETFLIX",
                          merchant_name="Netflix", is_transfer=False)
    by_key = {"netflix": [a, b]}
    counted = [reports._own_charges(i, [row], by_key, {}) for i in (a, b)]
    assert counted == [[row], []]


def test_price_check_saves_detection_notes_once(env, monkeypatch):
    db = env.db
    items = [make(db, f"Bill {n}", f"bill {n}", env.card.id, -1000) for n in range(3)]
    for i in items:
        note(db, i, -1000)
        charge(db, env.card.id, D(2026, 9, 10), -12, f"BILL {items.index(i)}")
    saves = []
    real = recurring._save_memo
    monkeypatch.setattr(recurring, "_save_memo", lambda s, m: (saves.append(1), real(s, m)))
    check(db)
    assert [amount(db, i) for i in items] == [-1200] * 3 and len(saves) == 1
    db.expire_all()
    assert recurring.users_amounts(db, list(db.scalars(select(RecurringItem)))) == set()

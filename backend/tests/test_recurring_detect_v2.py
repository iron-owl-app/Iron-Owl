"""Release 3.19 detection: bills whose amount changes, monthly after 2 charges, name variants
and bills that move to another card; user edits always win."""
from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlalchemy import select

from app.models import AppSetting, RecurringItem
from app.services import calendar, recurring
from app.services.recurring import MEMO_KEY, _classify_basic, classify, loose_key
from tests.conftest import add_txn, make_account

D = dt.date
TODAY = D(2026, 9, 26)  # conftest FIXED_TODAY


def dates(*isos: str) -> list[dt.date]:
    return [D.fromisoformat(s) for s in isos]


@pytest.fixture
def env(unlocked, db, fixed_today):
    chk = make_account(db, "Checking", "bank", 5000, plaid_type="depository", plaid_subtype="checking")
    card = make_account(db, "Visa", "credit", 200, plaid_type="credit", plaid_subtype="credit card")
    card2 = make_account(db, "Mastercard", "credit", 100, plaid_type="credit", plaid_subtype="credit card")
    return unlocked, db, fixed_today, chk, card, card2


def run(db, day=TODAY) -> list[RecurringItem]:
    db.expire_all()  # the API changed items in its own session
    ids = recurring.detect(db, day)
    db.commit()
    return [db.get(RecurringItem, i) for i in ids]


def items(db) -> list[RecurringItem]:
    db.expire_all()
    return list(db.scalars(select(RecurringItem).order_by(RecurringItem.id)))


def memo(db) -> dict:
    row = db.get(AppSetting, MEMO_KEY)
    return json.loads(row.value) if row is not None else {}


# ------------------------------------------------------------------ names


@pytest.mark.parametrize(("key", "loose"), [
    ("netflix.com", "netflix"), ("netflix inc", "netflix"), ("netflix", "netflix"),
    ("netflix com ca", "netflix ca"), ("att*bill 0612", "att bill"), ("at&t", "at&t"),
    ("online pmt", "online pmt"),  # nothing real left: the key itself
    ("ach debit geico trace", "geico"), ("ach debit progressive trace", "progressive"),
    ("12345", "12345"), ("joe's", "joe's"),
])
def test_loose_key(key, loose):
    assert loose_key(key) == loose


# ------------------------------------------------------------------ classify (unit)


def test_variable_monthly_bill_is_found():
    electric = dates("2026-04-15", "2026-05-14", "2026-06-15", "2026-07-16", "2026-08-15", "2026-09-15")
    amounts = [-6200, -9500, -14000, -16800, -11000, -8000]
    assert _classify_basic(electric, amounts) is None  # before 3.19
    assert classify(electric, amounts) == ("monthly", [15])
    # Too wide a swing (more than 3x), or a drifting day of the month: not a bill.
    assert classify(electric, [-4000, -9500, -14000, -16800, -11000, -8000]) is None
    drifting = dates("2026-04-02", "2026-05-03", "2026-06-05", "2026-07-07", "2026-08-09", "2026-09-11")
    assert classify(drifting, amounts) is None
    # Two charges on one day at the merchant: the relaxed rules are off.
    assert classify(electric, amounts, crowded=True) is None


def test_two_monthly_charges():
    assert classify(dates("2026-08-20", "2026-09-20"), [-1299, -1299]) == ("monthly", [20])
    assert classify(dates("2026-08-31", "2026-09-30"), [-1299, -1299]) == ("monthly", [31])
    assert classify(dates("2026-08-20", "2026-09-20"), [-4500, -6000]) is None  # amounts 25% apart
    assert classify(dates("2026-08-01", "2026-09-15"), [-1299, -1299]) is None  # 45 days
    assert classify(dates("2026-08-20", "2026-08-30"), [-1299, -1299]) is None
    assert classify(dates("2026-08-20", "2026-09-20"), [-1299, -1299], crowded=True) is None


def test_weekly_shopping_is_not_a_bill():
    weekly = dates("2026-08-01", "2026-08-08", "2026-08-15", "2026-08-22", "2026-08-29", "2026-09-05")
    groceries = [-9512, -10233, -8890, -11020, -9705, -10410]
    assert _classify_basic(weekly, groceries) == ("weekly", None)  # found before 3.19
    assert classify(weekly, groceries) is None
    # An existing item keeps being found (refresh), and a real weekly bill is found.
    assert classify(weekly, groceries, refresh=True) == ("weekly", None)
    assert classify(weekly, [-2500] * 5 + [-2600]) == ("weekly", None)
    # Weekly money in (a paycheck that varies) is unchanged.
    assert classify(weekly, [-a for a in groceries]) == ("weekly", None)


def test_quarterly_and_yearly_unchanged():
    assert classify(dates("2026-03-10", "2026-06-10"), [-5000, -5000]) == ("quarterly", [10])
    assert classify(dates("2025-09-20", "2026-09-20"), [-90000, -95000]) == ("yearly", [20])
    assert classify(dates("2025-09-20", "2026-09-20"), [-90000, -150000]) is None


# ------------------------------------------------------------------ detection


def test_new_bills_found_and_shopping_not(env):
    client, db, _today, chk, card, card2 = env
    for day, amount in (("2026-04-15", -62), ("2026-05-14", -95), ("2026-06-15", -140),
                        ("2026-07-16", -168), ("2026-08-15", -110), ("2026-09-15", -80.55)):
        add_txn(db, chk.id, day, amount, "CITY ELECTRIC", merchant="City Electric", category="RENT_AND_UTILITIES")
    for day, amount in (("2026-06-03", -71.20), ("2026-07-03", -88.10), ("2026-08-04", -64.00), ("2026-09-03", -79.99)):
        add_txn(db, card.id, day, amount, "VERIZON WIRELESS", merchant="Verizon")
    add_txn(db, card.id, "2026-08-22", -12.99, "NEWSUB", merchant="New Sub")
    add_txn(db, card.id, "2026-09-22", -12.99, "NEWSUB", merchant="New Sub")
    # Not bills: weekly groceries at one store, coffee a few times a week, gas, Amazon.
    day = D(2026, 6, 6)
    for k, cents in enumerate([9512, 10233, 8890, 11020, 9705, 10410, 9980, 10120, 9340, 10870, 9650, 10300, 9900, 10050, 9420, 10600]):
        add_txn(db, chk.id, day + dt.timedelta(days=7 * k), -cents / 100, "KROGER #123", merchant="Kroger")
    day = D(2026, 7, 1)
    for k in range(36):
        day += dt.timedelta(days=(2, 3, 2)[k % 3])
        add_txn(db, card.id, day, -(5.25 + (k % 4) * 0.5), "STARBUCKS", merchant="Starbucks")
    day = D(2026, 7, 1)
    for k, gap in enumerate((6, 8, 7, 7, 6, 8, 7, 7, 6, 8, 7)):
        day += dt.timedelta(days=gap)
        add_txn(db, card2.id, day, -(41.0 + (k * 3.7) % 8), "SHELL OIL 5744", merchant="Shell")
    for day, amount in (("2026-06-02", -23.5), ("2026-06-19", -48.1), ("2026-07-03", -12.0),
                        ("2026-08-01", -77.3), ("2026-08-30", -19.9), ("2026-09-12", -33.3)):
        add_txn(db, card.id, day, amount, "AMAZON MKTPLACE", merchant="Amazon")
    got = {i.name: i for i in run(db)}
    assert set(got) == {"City Electric", "Verizon", "New Sub"}
    electric = got["City Electric"]
    assert (electric.cadence, electric.amount_cents, electric.next_date) == ("monthly", -8055, D(2026, 10, 15))
    assert got["Verizon"].amount_cents == -7999  # the last charge
    assert (got["New Sub"].cadence, got["New Sub"].next_date) == ("monthly", D(2026, 10, 22))


def test_two_charges_must_be_recent(env):
    _client, db, _today, chk, *_ = env
    add_txn(db, chk.id, "2026-06-10", -20, "OLDTHING", merchant="Old Thing")
    add_txn(db, chk.id, "2026-07-10", -20, "OLDTHING", merchant="Old Thing")
    add_txn(db, chk.id, "2026-07-12", -45, "RANDOM SHOP", merchant="Random Shop")
    add_txn(db, chk.id, "2026-08-12", -61, "RANDOM SHOP", merchant="Random Shop")
    assert run(db) == []


def test_yearly_insurance_with_two_charges(env):
    _client, db, _today, chk, *_ = env
    add_txn(db, chk.id, "2025-09-18", -880, "STATE FARM", merchant="State Farm")
    add_txn(db, chk.id, "2026-09-18", -912, "STATE FARM", merchant="State Farm")
    (item,) = run(db)
    assert (item.cadence, item.amount_cents, item.next_date) == ("yearly", -91200, D(2027, 9, 18))


def netflix(db, account_id, months, merchant, amount=-15.49, day=10):
    for m in months:
        add_txn(db, account_id, f"2026-{m:02d}-{day:02d}", amount, merchant.upper(), merchant=merchant,
                category="ENTERTAINMENT")


def test_renamed_bill_updates_the_existing_item(env):
    client, db, today, _chk, card, _card2 = env
    netflix(db, card.id, (5, 6, 7, 8), "Netflix.com")
    (item,) = run(db, D(2026, 8, 20))
    assert client.patch(f"/api/recurring/{item.id}", json={"status": "active"}).status_code == 200
    netflix(db, card.id, (9,), "Netflix Inc")  # the bank's name changed
    assert run(db) == []
    (item,) = items(db)
    assert (item.merchant_key, item.last_seen_date, item.next_date) == ("netflix.com", D(2026, 9, 10), D(2026, 10, 10))
    assert memo(db)[str(item.id)]["aliases"] == [[card.id, "netflix inc"]]
    # The calendar matches the new name; the Recurring page's evidence counts both.
    occ = {o.base_date: o for o in calendar.occurrences_for(db, TODAY, D(2026, 8, 1), D(2026, 9, 30))}
    assert occ[D(2026, 9, 10)].status == "paid" and occ[D(2026, 8, 10)].status == "paid"
    assert len(recurring.matched_history(db, [item], TODAY)[item.id]) == 5


def test_name_variant_both_in_history_is_one_suggestion(env):
    _client, db, _today, _chk, card, _card2 = env
    netflix(db, card.id, (5, 6), "NETFLIX.COM")
    netflix(db, card.id, (7, 8, 9), "Netflix")
    (item,) = run(db)
    assert (item.name, item.merchant_key, item.cadence) == ("Netflix", "netflix", "monthly")
    assert memo(db)[str(item.id)]["aliases"] == [[card.id, "netflix.com"]]
    assert run(db) == []


def test_dismissed_stays_dismissed_after_renames(env):
    client, db, _today, _chk, card, _card2 = env
    netflix(db, card.id, (4, 5, 6), "Netflix.com")
    (item,) = run(db, D(2026, 6, 20))
    client.patch(f"/api/recurring/{item.id}", json={"status": "dismissed"})
    netflix(db, card.id, (7, 8), "Netflix Inc")
    assert run(db, D(2026, 8, 20)) == []
    netflix(db, card.id, (9,), "NETFLIX")
    assert run(db) == []
    (item,) = items(db)
    assert (item.status, item.last_seen_date) == ("dismissed", D(2026, 9, 10))


def test_a_variant_of_a_hand_added_bill_is_not_suggested(env):
    client, db, _today, _chk, card, _card2 = env
    r = client.post("/api/recurring", json={"name": "Netflix", "amount": -15.49, "cadence": "monthly",
                                            "next_date": "2026-10-10"})
    assert r.status_code == 201
    netflix(db, card.id, (6, 7, 8, 9), "Netflix.com")
    assert run(db) == []


def test_bill_moved_to_another_card(env):
    client, db, _today, _chk, card, card2 = env
    netflix(db, card.id, (5, 6, 7, 8), "Netflix")
    (item,) = run(db, D(2026, 8, 20))
    client.patch(f"/api/recurring/{item.id}", json={"status": "active"})
    netflix(db, card2.id, (9,), "Netflix")  # the old card stopped, the new one took over
    assert run(db) == []
    (item,) = items(db)
    assert (item.account_id, item.last_seen_date, item.next_date) == (card2.id, D(2026, 9, 10), D(2026, 10, 10))
    assert memo(db)[str(item.id)]["aliases"] == [[card.id, "netflix"]]
    occ = {o.base_date: o for o in calendar.occurrences_for(db, TODAY, D(2026, 7, 1), D(2026, 9, 30))}
    assert [occ[D(2026, m, 10)].status for m in (7, 8, 9)] == ["paid", "paid", "paid"]


def test_hand_set_account_does_not_follow(env):
    client, db, _today, _chk, card, card2 = env
    netflix(db, card.id, (5, 6, 7), "Netflix")
    netflix(db, card2.id, (8,), "Netflix")
    (item,) = run(db, D(2026, 8, 20))
    assert item.account_id == card2.id  # already moved: found on the new card
    # The user says it's paid with the old card: detection doesn't move it back.
    client.patch(f"/api/recurring/{item.id}", json={"status": "active", "account_id": card.id})
    netflix(db, card2.id, (9,), "Netflix")
    assert run(db) == []
    (item,) = items(db)
    assert (item.account_id, item.last_seen_date) == (card.id, D(2026, 9, 10))


def test_two_cards_at_once_stay_two_bills(env):
    _client, db, _today, _chk, card, card2 = env
    netflix(db, card.id, (6, 7, 8, 9), "Netflix")
    netflix(db, card2.id, (6, 7, 8, 9), "Netflix", amount=-22.99, day=18)
    got = sorted((i.account_id, i.amount_cents) for i in run(db))
    assert got == [(card.id, -1549), (card2.id, -2299)]


def test_names_with_changing_numbers(env):
    _client, db, _today, chk, _card, _card2 = env
    for m in (6, 7, 8, 9):
        add_txn(db, chk.id, f"2026-{m:02d}-12", -54.20, f"ACH DEBIT GEICO {m:02d}12 TRACE {m * 7919}")
        add_txn(db, chk.id, f"2026-{m:02d}-18", -98.00, f"ACH DEBIT PROGRESSIVE {m:02d}18 TRACE {m * 104729}")
        # A merchant name that carries the date: each month's key differs.
        add_txn(db, chk.id, f"2026-{m:02d}-03", -65.00, "ATT BILL", merchant=f"ATT*BILL {m:02d}03")
    got = {i.name: i for i in run(db)}
    assert len(got) == 3
    att = next(i for i in got.values() if i.merchant_key.startswith("att"))
    assert (att.cadence, att.merchant_key, att.amount_cents) == ("monthly", "att*bill 0903", -6500)
    assert len(memo(db)[str(att.id)]["aliases"]) == 3
    keys = sorted(i.merchant_key for i in got.values() if i is not att)
    assert keys == ["ach debit geico trace", "ach debit progressive trace"]


def test_amount_follows_the_last_charge_unless_set_by_hand(env):
    client, db, today, chk, card, _card2 = env
    for day, amount in (("2026-05-15", -62), ("2026-06-15", -95), ("2026-07-15", -140), ("2026-08-14", -110)):
        add_txn(db, chk.id, day, amount, "CITY ELECTRIC", merchant="City Electric")
        add_txn(db, chk.id, day, -45, "CITY WATER", merchant="City Water")
    electric, water = run(db, D(2026, 8, 20))
    assert (electric.amount_cents, water.amount_cents) == (-11000, -4500)
    client.patch(f"/api/recurring/{electric.id}", json={"status": "active"})
    client.patch(f"/api/recurring/{water.id}", json={"status": "active", "amount": -50})  # by hand
    add_txn(db, chk.id, "2026-09-15", -80.55, "CITY ELECTRIC", merchant="City Electric")
    add_txn(db, chk.id, "2026-09-15", -47, "CITY WATER", merchant="City Water")
    run(db)
    electric, water = items(db)
    assert (electric.amount_cents, electric.next_date) == (-8055, D(2026, 10, 15))
    assert (water.amount_cents, water.next_date) == (-5000, D(2026, 10, 15))  # the user's amount stays
    run(db)  # idempotent
    assert [i.amount_cents for i in items(db)] == [-8055, -5000]


def test_items_from_before_this_release(env):
    """No note yet: an amount equal to one of the charges is detection's; any other is the user's."""
    _client, db, _today, chk, *_ = env
    for day, amount in (("2026-06-15", -95), ("2026-07-15", -100), ("2026-08-14", -98), ("2026-09-15", -104)):
        add_txn(db, chk.id, day, amount, "CITY GAS", merchant="City Gas")
        add_txn(db, chk.id, day, amount - 10, "CITY TRASH", merchant="City Trash")
    old = dict(cadence="monthly", status="active", source="detected", last_seen_date=D(2026, 8, 14),
               next_date=D(2026, 9, 15), month_days="[15]")
    db.add(RecurringItem(name="City Gas", merchant_key="city gas", account_id=chk.id, amount_cents=-9800, **old))
    db.add(RecurringItem(name="City Trash", merchant_key="city trash", account_id=chk.id, amount_cents=-12000, **old))
    db.commit()
    assert run(db) == []
    gas, trash = items(db)
    assert (gas.amount_cents, trash.amount_cents) == (-10400, -12000)
    assert memo(db)[str(trash.id)]["amount"] is None


def test_items_without_aliases_match_as_before(env):
    """The calendar matcher is unchanged for an item detection never joined to another name."""
    client, db, _today, _chk, card, _card2 = env
    netflix(db, card.id, (5, 6, 7, 8, 9), "Netflix")
    add_txn(db, card.id, "2026-09-12", -15.49, "NETFLIX INC", merchant="Netflix Inc")  # overlaps: not joined
    (item,) = run(db)
    client.patch(f"/api/recurring/{item.id}", json={"status": "active"})
    assert recurring.item_aliases(db, items(db)) == {}

    def statuses():
        return [(o.base_date, o.status, o.transaction_id)
                for o in calendar.occurrences_for(db, TODAY, D(2026, 5, 1), D(2026, 10, 31))]

    before = statuses()
    db.delete(db.get(AppSetting, MEMO_KEY))
    db.commit()
    assert statuses() == before


def test_memo_ignores_a_reused_id(env):
    client, db, _today, _chk, card, card2 = env
    netflix(db, card.id, (5, 6), "Netflix.com")
    netflix(db, card.id, (7, 8, 9), "Netflix")
    (item,) = run(db)
    assert recurring.item_aliases(db, [item]) == {item.id: [(card.id, "netflix.com")]}
    item_id, card2_id = item.id, card2.id
    assert client.delete(f"/api/recurring/{item_id}").status_code == 204
    db.expunge(item)
    db.add(RecurringItem(id=item_id, name="Gym", merchant_key="gym", account_id=card2_id, amount_cents=-3000,
                         cadence="monthly", next_date=D(2026, 10, 1), status="active", source="manual"))
    db.commit()
    assert recurring.item_aliases(db, items(db)) == {}


def test_once_items_untouched(env):
    client, db, _today, chk, *_ = env
    once = client.post("/api/recurring", json={"name": "Acme", "amount": -30, "cadence": "once",
                                               "next_date": "2026-10-05", "account_id": chk.id}).json()
    for day in ("2026-07-05", "2026-08-05", "2026-09-05"):
        add_txn(db, chk.id, day, -30 - int(day[6]) , "ACME CO", merchant="Acme Co")
    assert run(db) == []
    (item,) = items(db)
    assert (item.id, item.amount_cents, item.last_seen_date, item.next_date) == (once["id"], -3000, None, D(2026, 10, 5))

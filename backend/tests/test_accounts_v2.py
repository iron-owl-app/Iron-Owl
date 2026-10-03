"""Accounts page (Release 3.10, pair 1): the new GET /api/accounts fields, adding an account by
kind, typing in a balance with Undo, removing a manual account with Undo, and the bank's own
name / card limit written by every sync (``app_settings.account_facts``)."""
from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlalchemy import func, select

from app.models import Account, AppSetting, BalanceSnapshot, Goal, PlaidItem, RecurringItem, Transaction
from app.services import account_facts
from tests.conftest import make_account
from tests.test_plaid import BANK_TOKEN, LOAN_TOKEN, exchange, plaid_setup  # noqa: F401 - fixture

D = dt.date
TODAY = D(2026, 9, 26)  # conftest.FIXED_TODAY


def ok(response, status: int = 200):
    assert response.status_code == status, response.text
    return response.json() if response.content else None


def create(client, status: int = 201, **body):
    return ok(client.post("/api/accounts", json=body), status)


def listed(client) -> dict[int, dict]:
    return {a["id"]: a for a in ok(client.get("/api/accounts"))}


def snaps(db, account_id: int) -> list[tuple[str, int, bool]]:
    db.expire_all()
    rows = db.scalars(select(BalanceSnapshot).where(BalanceSnapshot.account_id == account_id)
                      .order_by(BalanceSnapshot.date))
    return [(s.date.isoformat(), s.balance_cents, bool(s.estimated)) for s in rows]


def add_snap(db, account_id: int, day: D, cents: int, estimated: bool = False) -> None:
    db.add(BalanceSnapshot(account_id=account_id, date=day, balance_cents=cents, estimated=estimated))
    db.commit()


def setting(db, key: str):
    db.expire_all()
    row = db.get(AppSetting, key)
    return None if row is None or row.value is None else json.loads(row.value)


# ------------------------------------------------------------------ list fields


NEW_FIELDS = {"bank_name", "connection", "balance_date", "balance_age_days", "stale", "credit_limit"}


def test_manual_account_list_fields_and_staleness(unlocked, db, fixed_today):
    acct = create(unlocked, name="Cash jar", kind="savings", current_balance=40)
    assert NEW_FIELDS <= set(acct)
    assert acct["connection"] is None and acct["bank_name"] is None and acct["credit_limit"] is None
    assert acct["balance_date"] == "2026-09-26" and acct["balance_age_days"] == 0 and acct["stale"] is False

    fixed_today.set(D(2026, 10, 2))  # 6 days
    row = listed(unlocked)[acct["id"]]
    assert row["balance_age_days"] == 6 and row["stale"] is False
    fixed_today.set(D(2026, 10, 3))  # 7 days: time to update
    row = listed(unlocked)[acct["id"]]
    assert row["balance_age_days"] == 7 and row["stale"] is True
    # An estimated snapshot (reconstructed, not typed in) doesn't count as an update.
    add_snap(db, acct["id"], D(2026, 10, 1), 4000, estimated=True)
    assert listed(unlocked)[acct["id"]]["balance_date"] == "2026-09-26"


def test_manual_account_without_snapshot_dates_from_when_it_was_added(unlocked, db, fixed_today):
    old = make_account(db, "Old manual", "bank", 10)
    old.created_at = dt.datetime(2026, 9, 1, 12, 0)
    db.commit()
    row = listed(unlocked)[old.id]
    assert row["balance_date"] == "2026-09-01" and row["stale"] is True


def test_plaid_account_fields_after_sync(unlocked, db, fixed_today, plaid_setup):  # noqa: F811
    for pa in plaid_setup.accounts[LOAN_TOKEN]:
        if pa["account_id"] == "cc":
            pa["balances"]["limit"] = 5000
    exchange(unlocked, "public-loan", "loan")
    rows = {a["name"]: a for a in ok(unlocked.get("/api/accounts"))}
    visa, mtg = rows["Visa"], rows["Mortgage"]
    item = ok(unlocked.get("/api/plaid/items"))[0]
    assert visa["connection"] == {"item_id": item["id"], "kind": "loan", "status": "ok",
                                  "last_synced_at": item["last_synced_at"]}
    assert visa["bank_name"] == "Visa" and visa["credit_limit"] == 5000.0
    assert mtg["credit_limit"] is None  # only cards show a limit
    assert visa["stale"] is False and visa["balance_date"] == TODAY.isoformat()

    # Renaming is a nickname: the bank's own name stays, and survives the next sync.
    ok(unlocked.patch(f"/api/accounts/{visa['id']}", json={"name": "My card"}))
    ok(unlocked.post("/api/plaid/sync", json={}))
    visa = listed(unlocked)[visa["id"]]
    assert visa["name"] == "My card" and visa["bank_name"] == "Visa"

    # A connection that needs signing in again shows on the account.
    db.execute(PlaidItem.__table__.update().values(status="login_required"))
    db.commit()
    assert listed(unlocked)[visa["id"]]["connection"]["status"] == "login_required"


def test_list_uses_a_fixed_number_of_queries(unlocked, db, fixed_today):
    from sqlalchemy import event

    for i in range(12):
        create(unlocked, name=f"Acct {i}", kind="checking", current_balance=i)
    engine = db.get_bind()
    count = [0]

    def before(*_args):
        count[0] += 1

    event.listen(engine, "before_cursor_execute", before)
    try:
        ok(unlocked.get("/api/accounts"))
    finally:
        event.remove(engine, "before_cursor_execute", before)
    assert count[0] <= 8  # no query per account


# ------------------------------------------------------------------ add by kind


@pytest.mark.parametrize(("kind", "category", "subtype", "group"), [
    ("checking", "bank", "checking", None),
    ("savings", "bank", "savings", None),
    ("credit_card", "credit", "credit card", "Credit cards"),
    ("student", "loan", "student", "Student loans"),
    ("auto", "loan", "auto", "Car loans"),
    ("other_loan", "loan", None, "Other loans"),
    ("investment", "investment", None, None),
    ("other", "other", None, None),  # something else the user owns, like a house or car
])
def test_create_by_kind(unlocked, kind, category, subtype, group):
    acct = create(unlocked, name="My account", kind=kind, current_balance=100, mask="0042",
                  institution_name="  Local  Credit Union ")
    assert (acct["category"], acct["plaid_subtype"], acct["loan_group"]) == (category, subtype, group)
    assert acct["source"] == "manual" and acct["mask"] == "0042"
    assert acct["institution_name"] == "Local Credit Union"


def test_create_debt_stores_the_amount_owed_positive(unlocked):
    card = create(unlocked, name="Store card", kind="credit_card", current_balance=-420.5, minimum_payment=35,
                  interest_rate=24.9, next_payment_due="2026-10-10")
    assert card["current_balance"] == 420.5 and card["minimum_payment"] == 35.0
    loan = create(unlocked, name="Old loan", category="loan", current_balance=-9000)  # older form, too
    assert loan["current_balance"] == 9000.0
    cash = create(unlocked, name="Overdrawn", kind="checking", current_balance=-20)  # assets keep their sign
    assert cash["current_balance"] == -20.0


def test_manual_card_due_need_works(unlocked, fixed_today):
    create(unlocked, name="Store card", kind="credit_card", current_balance=-300, minimum_payment=25,
           next_payment_due="2026-09-28")
    needs = [n for n in ok(unlocked.get("/api/dashboard"))["needs"] if n["kind"] == "card_due"]
    assert len(needs) == 1 and needs[0]["data"]["minimum_payment"] == 25.0


@pytest.mark.parametrize("body", [
    {"name": "x", "current_balance": 1},  # neither kind nor category
    {"name": "x", "kind": "checking", "category": "loan", "current_balance": 1},  # disagree
    {"name": "x", "kind": "gold", "current_balance": 1},
    {"name": "x", "kind": "checking", "current_balance": 1, "mask": "12345"},
    {"name": "x", "kind": "checking", "current_balance": 1, "mask": "12a4"},
    {"name": "x", "kind": "checking", "current_balance": 1, "mask": "١٢٣٤"},  # non-ASCII digits
    {"name": "x", "kind": "checking", "current_balance": 1, "source": "plaid"},
    {"name": "x", "kind": "checking", "current_balance": 1, "item_id": 1},
    {"name": "x", "kind": "checking", "current_balance": 1e13},
    {"name": "x", "kind": "student", "current_balance": 1, "minimum_payment": -5},
    {"name": "   ", "kind": "checking", "current_balance": 1},
    {"name": "x" * 201, "kind": "checking", "current_balance": 1},
])
def test_create_validation(unlocked, body):
    r = unlocked.post("/api/accounts", json=body)
    assert r.status_code == 422, r.text
    assert "12a4" not in r.text and "xxxxxxxx" not in r.text  # never echoed back


def test_create_rejects_nan(unlocked):
    r = unlocked.post("/api/accounts", content='{"name": "x", "kind": "checking", "current_balance": NaN}',
                      headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    assert ok(unlocked.get("/api/accounts")) == []


def test_kind_and_matching_category_is_fine(unlocked):
    acct = create(unlocked, name="Chk", kind="checking", category="bank", current_balance=1)
    assert acct["category"] == "bank"


# ------------------------------------------------------------------ balance + Undo


def test_set_balance_and_undo_restores_exactly(unlocked, db, fixed_today):
    acct = create(unlocked, name="Car loan", kind="auto", current_balance=9000)
    aid = acct["id"]
    fixed_today.set(D(2026, 10, 5))  # stale by now
    assert listed(unlocked)[aid]["stale"] is True

    body = ok(unlocked.put(f"/api/accounts/{aid}/balance", json={"balance": -8750.25}))
    assert body["account"]["current_balance"] == 8750.25  # owed, positive
    assert body["account"]["stale"] is False and body["account"]["balance_date"] == "2026-10-05"
    token = body["undo"]["token"]
    assert snaps(db, aid) == [("2026-09-26", 900000, False), ("2026-10-05", 875025, False)]

    back = ok(unlocked.post("/api/accounts/balance/undo", json={"token": token}))
    assert back["current_balance"] == 9000.0 and back["stale"] is True and back["balance_date"] == "2026-09-26"
    assert snaps(db, aid) == [("2026-09-26", 900000, False)]  # today's snapshot didn't exist: gone again
    # Single use.
    r = unlocked.post("/api/accounts/balance/undo", json={"token": token})
    assert r.status_code == 409 and r.json()["detail"].startswith("This can't be undone")


def test_undo_puts_back_todays_earlier_snapshot(unlocked, db, fixed_today):
    acct = create(unlocked, name="Jar", kind="savings", current_balance=10)
    aid = acct["id"]
    first = ok(unlocked.put(f"/api/accounts/{aid}/balance", json={"balance": 20}))["undo"]["token"]
    second = ok(unlocked.put(f"/api/accounts/{aid}/balance", json={"balance": 30}))["undo"]["token"]
    # The first save can't be undone while the second one's balance is in place...
    assert unlocked.post("/api/accounts/balance/undo", json={"token": first}).status_code == 409
    # ...but undoing in order works, back to the created balance.
    assert ok(unlocked.post("/api/accounts/balance/undo", json={"token": second}))["current_balance"] == 20.0
    assert snaps(db, aid) == [("2026-09-26", 2000, False)]


def test_undo_order_after_a_refusal(unlocked, db, fixed_today):
    """A refused Undo doesn't use up its token (the refusal rolls back)."""
    aid = create(unlocked, name="Jar", kind="savings", current_balance=10)["id"]
    first = ok(unlocked.put(f"/api/accounts/{aid}/balance", json={"balance": 20}))["undo"]["token"]
    second = ok(unlocked.put(f"/api/accounts/{aid}/balance", json={"balance": 30}))["undo"]["token"]
    assert unlocked.post("/api/accounts/balance/undo", json={"token": first}).status_code == 409
    ok(unlocked.post("/api/accounts/balance/undo", json={"token": second}))
    assert ok(unlocked.post("/api/accounts/balance/undo", json={"token": first}))["current_balance"] == 10.0
    assert snaps(db, aid) == [("2026-09-26", 1000, False)]


def test_undo_restores_an_estimated_snapshot(unlocked, db, fixed_today):
    aid = create(unlocked, name="Jar", kind="savings", current_balance=10)["id"]
    fixed_today.set(D(2026, 9, 27))
    add_snap(db, aid, D(2026, 9, 27), 777, estimated=True)
    token = ok(unlocked.put(f"/api/accounts/{aid}/balance", json={"balance": 50}))["undo"]["token"]
    assert snaps(db, aid)[-1] == ("2026-09-27", 5000, False)
    ok(unlocked.post("/api/accounts/balance/undo", json={"token": token}))
    assert snaps(db, aid)[-1] == ("2026-09-27", 777, True)


def test_balance_undo_is_today_only(unlocked, fixed_today):
    aid = create(unlocked, name="Jar", kind="savings", current_balance=10)["id"]
    token = ok(unlocked.put(f"/api/accounts/{aid}/balance", json={"balance": 20}))["undo"]["token"]
    fixed_today.set(D(2026, 9, 27))
    assert unlocked.post("/api/accounts/balance/undo", json={"token": token}).status_code == 409
    assert listed(unlocked)[aid]["current_balance"] == 20.0


def test_balance_undo_refused_when_changed_since(unlocked, fixed_today):
    aid = create(unlocked, name="Jar", kind="savings", current_balance=10)["id"]
    token = ok(unlocked.put(f"/api/accounts/{aid}/balance", json={"balance": 20}))["undo"]["token"]
    ok(unlocked.patch(f"/api/accounts/{aid}", json={"current_balance": 25}))  # the older edit path
    r = unlocked.post("/api/accounts/balance/undo", json={"token": token})
    assert r.status_code == 409 and "changed" in r.json()["detail"]
    assert listed(unlocked)[aid]["current_balance"] == 25.0


def test_balance_rules(unlocked, db, fixed_today):
    linked = make_account(db, "Linked", "bank", 5, plaid_type="depository", plaid_subtype="checking")
    assert unlocked.put(f"/api/accounts/{linked.id}/balance", json={"balance": 9}).status_code == 400
    assert unlocked.put("/api/accounts/99999/balance", json={"balance": 9}).status_code == 404
    aid = create(unlocked, name="Jar", kind="savings", current_balance=10)["id"]
    for body in ({}, {"balance": "lots"}, {"balance": 1e13}, {"balance": 1, "extra": 1}):
        assert unlocked.put(f"/api/accounts/{aid}/balance", json=body).status_code == 422
    for token in ("", "short", "x" * 65, "bad token!", "../../etc"):
        assert unlocked.post("/api/accounts/balance/undo", json={"token": token}).status_code == 422
    assert unlocked.post("/api/accounts/balance/undo", json={"token": "A" * 22}).status_code == 409


def test_undo_records_are_capped(unlocked, db, fixed_today):
    aid = create(unlocked, name="Jar", kind="savings", current_balance=0)["id"]
    for i in range(25):
        ok(unlocked.put(f"/api/accounts/{aid}/balance", json={"balance": i}))
    assert len(setting(db, "account_undo")) == 20


# ------------------------------------------------------------------ remove + Undo


def test_delete_and_restore_puts_everything_back(unlocked, db, fixed_today):
    jar = create(unlocked, name="Cash jar", kind="checking", current_balance=50, mask="1234",
                 institution_name="Home", notes="under the bed")
    aid = jar["id"]
    ok(unlocked.put(f"/api/accounts/{aid}/balance", json={"balance": 60}))
    add_snap(db, aid, D(2026, 9, 1), 4000)
    ok(unlocked.patch(f"/api/accounts/{aid}", json={"hidden": False, "interest_rate": 1.5}))
    other = make_account(db, "Other bank", "bank", 10)
    goal = Goal(name="Trip", kind="save", account_id=aid, target_cents=100000)
    rec = RecurringItem(name="Allowance", merchant_key="allowance", account_id=aid, amount_cents=-500,
                        cadence="monthly", next_date=D(2026, 10, 1), status="active")
    rec2 = RecurringItem(name="Moved later", merchant_key="moved", account_id=aid, amount_cents=-100,
                         cadence="monthly", next_date=D(2026, 10, 1), status="active")
    db.add_all([goal, rec, rec2])
    db.add(AppSetting(key="budget_account_ids", value=json.dumps({"exclude": [aid], "include": []})))
    db.add(AppSetting(key="forecast_account_id", value=str(aid)))
    db.commit()
    before_snaps = snaps(db, aid)

    body = ok(unlocked.delete(f"/api/accounts/{aid}"))
    assert body["undo"]["name"] == "Cash jar" and isinstance(body["undo"]["token"], str)
    assert aid not in listed(unlocked) and snaps(db, aid) == []
    assert setting(db, "budget_account_ids") == {"exclude": [], "include": []}
    assert setting(db, "forecast_account_id") is None
    db.expire_all()
    assert db.get(Goal, goal.id).account_id is None and db.get(RecurringItem, rec.id).account_id is None
    # Pointed somewhere else while it was gone: that choice is kept.
    db.get(RecurringItem, rec2.id).account_id = other.id
    db.commit()

    back = ok(unlocked.post("/api/accounts/restore", json={"token": body["undo"]["token"]}), 201)
    assert back["id"] == aid  # the id was still free
    for key in ("name", "mask", "institution_name", "notes", "category", "plaid_subtype", "current_balance",
                "interest_rate", "hidden", "loan_group", "balance_date", "stale"):
        assert back[key] == jar[key] or key in ("current_balance", "interest_rate"), key
    assert back["current_balance"] == 60.0 and back["interest_rate"] == 1.5
    assert snaps(db, aid) == before_snaps
    db.expire_all()
    assert db.get(Goal, goal.id).account_id == aid
    assert db.get(RecurringItem, rec.id).account_id == aid
    assert db.get(RecurringItem, rec2.id).account_id == other.id
    assert setting(db, "budget_account_ids") == {"exclude": [aid], "include": []}
    assert setting(db, "forecast_account_id") == aid
    # Once.
    assert unlocked.post("/api/accounts/restore", json={"token": body["undo"]["token"]}).status_code == 409
    assert len([a for a in listed(unlocked).values() if a["name"] == "Cash jar"]) == 1


def test_restore_gets_a_new_id_when_the_old_one_was_reused(unlocked, db, fixed_today):
    aid = create(unlocked, name="Old", kind="savings", current_balance=5)["id"]
    token = ok(unlocked.delete(f"/api/accounts/{aid}"))["undo"]["token"]
    reused = create(unlocked, name="Newer", kind="checking", current_balance=7)
    assert reused["id"] == aid  # SQLite hands the highest id out again
    back = ok(unlocked.post("/api/accounts/restore", json={"token": token}), 201)
    assert back["id"] != aid and back["name"] == "Old" and back["current_balance"] == 5.0
    assert snaps(db, back["id"]) == [("2026-09-26", 500, False)]
    assert listed(unlocked)[aid]["name"] == "Newer"


def test_restore_puts_back_the_facts_entry(unlocked, db, fixed_today):
    aid = create(unlocked, name="Old", kind="savings", current_balance=5)["id"]
    account_facts.store(db, {aid: {"bank_name": "Bank's name", "limit_cents": None}})
    db.commit()
    token = ok(unlocked.delete(f"/api/accounts/{aid}"))["undo"]["token"]
    db.expire_all()
    assert account_facts.load(db) == {}
    ok(unlocked.post("/api/accounts/restore", json={"token": token}), 201)
    db.expire_all()
    assert account_facts.load(db) == {aid: {"bank_name": "Bank's name", "limit_cents": None}}


def test_delete_rules(unlocked, db, fixed_today):
    linked = make_account(db, "Linked", "bank", 5, plaid_type="depository", plaid_subtype="checking")
    assert unlocked.delete(f"/api/accounts/{linked.id}").status_code == 400
    assert unlocked.delete("/api/accounts/99999").status_code == 404
    aid = create(unlocked, name="Jar", kind="savings", current_balance=10)["id"]
    token = ok(unlocked.delete(f"/api/accounts/{aid}"))["undo"]["token"]
    fixed_today.set(D(2026, 9, 27))
    assert unlocked.post("/api/accounts/restore", json={"token": token}).status_code == 409
    assert aid not in listed(unlocked)


def test_tokens_are_scoped_to_their_kind(unlocked, fixed_today):
    aid = create(unlocked, name="Jar", kind="savings", current_balance=10)["id"]
    balance_token = ok(unlocked.put(f"/api/accounts/{aid}/balance", json={"balance": 20}))["undo"]["token"]
    other = create(unlocked, name="Other", kind="savings", current_balance=1)["id"]
    delete_token = ok(unlocked.delete(f"/api/accounts/{other}"))["undo"]["token"]
    assert unlocked.post("/api/accounts/restore", json={"token": balance_token}).status_code == 409
    assert unlocked.post("/api/accounts/balance/undo", json={"token": delete_token}).status_code == 409
    # Neither was used up by the wrong route.
    ok(unlocked.post("/api/accounts/balance/undo", json={"token": balance_token}))
    ok(unlocked.post("/api/accounts/restore", json={"token": delete_token}), 201)


def test_new_routes_need_the_session_and_header(unlocked, fixed_today):
    from app.security import SESSION_HEADER

    aid = create(unlocked, name="Jar", kind="savings", current_balance=10)["id"]
    token = ok(unlocked.put(f"/api/accounts/{aid}/balance", json={"balance": 20}))["undo"]["token"]
    no_csrf = {"X-FinTrack": "0"}
    assert unlocked.put(f"/api/accounts/{aid}/balance", json={"balance": 1}, headers=no_csrf).status_code == 403
    assert unlocked.post("/api/accounts/balance/undo", json={"token": token}, headers=no_csrf).status_code == 403
    assert unlocked.delete(f"/api/accounts/{aid}", headers=no_csrf).status_code == 403
    assert unlocked.post("/api/accounts/restore", json={"token": token}, headers=no_csrf).status_code == 403
    no_session = {SESSION_HEADER: "wrong"}
    assert unlocked.put(f"/api/accounts/{aid}/balance", json={"balance": 1}, headers=no_session).status_code == 401
    assert unlocked.post("/api/accounts/balance/undo", json={"token": token}, headers=no_session).status_code == 401
    assert listed(unlocked)[aid]["current_balance"] == 20.0


# ------------------------------------------------------------------ sync: account_facts


def test_sync_writes_and_prunes_account_facts(unlocked, db, fixed_today, plaid_setup):  # noqa: F811
    exchange(unlocked, "public-bank", "bank")
    rows = {a["name"]: a for a in ok(unlocked.get("/api/accounts"))}
    chk = rows["Checking"]
    db.expire_all()
    facts = account_facts.load(db)
    assert facts[chk["id"]] == {"bank_name": "Checking", "limit_cents": None}
    # A stale entry for an account that is gone is pruned at the next sync.
    facts[987654] = {"bank_name": "Gone", "limit_cents": 1}
    account_facts.store(db, facts)
    db.commit()
    ok(unlocked.post("/api/plaid/sync", json={}))
    db.expire_all()
    assert 987654 not in account_facts.load(db) and chk["id"] in account_facts.load(db)


def test_facts_fall_back_to_official_name_and_ignore_junk(unlocked, db, fixed_today, plaid_setup):  # noqa: F811
    plaid_setup.accounts[BANK_TOKEN][0]["name"] = None
    plaid_setup.accounts[BANK_TOKEN][0]["balances"]["limit"] = float("inf")
    exchange(unlocked, "public-bank", "bank")
    db.expire_all()
    names = {v["bank_name"] for v in account_facts.load(db).values()}
    assert "Checking official" in names
    assert all(v["limit_cents"] is None for v in account_facts.load(db).values())


def test_unreadable_facts_setting_is_ignored(unlocked, db, fixed_today):
    db.add(AppSetting(key="account_facts", value="not json"))
    db.commit()
    create(unlocked, name="Jar", kind="savings", current_balance=1)
    assert all(a["bank_name"] is None for a in ok(unlocked.get("/api/accounts")))
    db.get(AppSetting, "account_facts").value = json.dumps({"1": {"bank_name": 5, "limit_cents": "x"}, "x": {}})
    db.commit()
    db.expire_all()
    assert account_facts.load(db) == {1: {"bank_name": None, "limit_cents": None}}


def test_manual_account_never_shows_a_stale_facts_entry(unlocked, db, fixed_today):
    aid = create(unlocked, name="Card", kind="credit_card", current_balance=1)["id"]
    account_facts.store(db, {aid: {"bank_name": "Someone else's", "limit_cents": 999}})
    db.commit()
    row = listed(unlocked)[aid]
    assert row["bank_name"] is None and row["credit_limit"] is None


def test_plaid_hide_only(unlocked, db, fixed_today, plaid_setup):  # noqa: F811
    """Owner decision: a bank-connected account can only be hidden from Accounts."""
    exchange(unlocked, "public-bank", "bank")
    chk = next(a for a in ok(unlocked.get("/api/accounts")) if a["name"] == "Checking")
    assert unlocked.delete(f"/api/accounts/{chk['id']}").status_code == 400
    assert ok(unlocked.patch(f"/api/accounts/{chk['id']}", json={"hidden": True}))["hidden"] is True
    assert len(ok(unlocked.get("/api/plaid/items"))) == 1
    db.expire_all()
    assert db.scalar(select(Account).where(Account.id == chk["id"])) is not None


# ------------------------------------------------------------------ review fixes (Release 3.10 pair 1)


def test_other_things_the_user_owns_are_stale_after_30_days(unlocked, db, fixed_today):
    house = create(unlocked, name="House", kind="other", current_balance=250000)
    jar = create(unlocked, name="Jar", kind="savings", current_balance=40)
    fixed_today.set(D(2026, 10, 3))  # 7 days
    rows = listed(unlocked)
    assert rows[jar["id"]]["stale"] is True and rows[house["id"]]["stale"] is False
    needs = {n["data"]["account_id"] for n in ok(unlocked.get("/api/dashboard"))["needs"] if n["kind"] == "update_balance"}
    assert needs == {jar["id"]}
    fixed_today.set(D(2026, 10, 25))  # 29 days
    assert listed(unlocked)[house["id"]]["stale"] is False
    fixed_today.set(D(2026, 10, 26))  # 30 days: time to update
    assert listed(unlocked)[house["id"]]["stale"] is True
    needs = {n["data"]["account_id"] for n in ok(unlocked.get("/api/dashboard"))["needs"] if n["kind"] == "update_balance"}
    assert needs == {jar["id"], house["id"]}


def test_balance_token_never_changes_an_account_that_reused_the_id(unlocked, db, fixed_today):
    """Reviewer's repro: Undo of a removed account's balance save must not touch a newer
    account that got the same id."""
    a = create(unlocked, name="A", kind="savings", current_balance=10)
    token = ok(unlocked.put(f"/api/accounts/{a['id']}/balance", json={"balance": 50}))["undo"]["token"]
    ok(unlocked.delete(f"/api/accounts/{a['id']}"))
    # The delete drops that account's balance Undos.
    assert all(v["kind"] != "balance" for v in setting(db, "account_undo").values())
    b = create(unlocked, name="B", kind="savings", current_balance=50)
    assert b["id"] == a["id"]  # SQLite hands the highest id out again
    r = unlocked.post("/api/accounts/balance/undo", json={"token": token})
    assert r.status_code == 409
    assert listed(unlocked)[b["id"]]["current_balance"] == 50.0
    assert snaps(db, b["id"]) == [("2026-09-26", 5000, False)]


def test_balance_token_checks_which_account_it_was_for(unlocked, db, fixed_today):
    """Even with the record still there (say, written before the delete dropped it), a token
    names the account by when it was added, not by id alone."""
    a = create(unlocked, name="A", kind="savings", current_balance=10)
    ok(unlocked.put(f"/api/accounts/{a['id']}/balance", json={"balance": 50}))
    saved = setting(db, "account_undo")
    ok(unlocked.delete(f"/api/accounts/{a['id']}"))
    b = create(unlocked, name="B", kind="savings", current_balance=50)
    assert b["id"] == a["id"]
    (token,) = saved
    db.expire_all()
    db.get(AppSetting, "account_undo").value = json.dumps(saved)  # the old record, put back
    db.commit()
    assert unlocked.post("/api/accounts/balance/undo", json={"token": token}).status_code == 409
    assert listed(unlocked)[b["id"]]["current_balance"] == 50.0
    # A record without the account's identity (from before this fix) is refused too.
    saved[token].pop("created")
    db.expire_all()
    db.get(AppSetting, "account_undo").value = json.dumps(saved)
    db.commit()
    assert unlocked.post("/api/accounts/balance/undo", json={"token": token}).status_code == 409
    assert listed(unlocked)[b["id"]]["current_balance"] == 50.0


def test_delete_keeps_other_accounts_balance_undos(unlocked, db, fixed_today):
    keep = create(unlocked, name="Keep", kind="savings", current_balance=1)["id"]
    gone = create(unlocked, name="Gone", kind="savings", current_balance=1)["id"]
    token = ok(unlocked.put(f"/api/accounts/{keep}/balance", json={"balance": 2}))["undo"]["token"]
    ok(unlocked.put(f"/api/accounts/{gone}/balance", json={"balance": 3}))
    ok(unlocked.delete(f"/api/accounts/{gone}"))
    assert ok(unlocked.post("/api/accounts/balance/undo", json={"token": token}))["current_balance"] == 1.0


def test_restore_relinks_only_the_goals_and_bills_it_unlinked(unlocked, db, fixed_today):
    """Reviewer's repro: a goal deleted while the account was gone, and a new goal that got
    its id, must not be linked to the restored account."""
    aid = create(unlocked, name="Savings", kind="savings", current_balance=10)["id"]
    kept_goal = Goal(name="Kept goal", kind="save", account_id=aid, target_cents=1000,
                     created_at=dt.datetime(2026, 9, 1, 12, 0, 0, 0))
    old_goal = Goal(name="Old goal", kind="save", account_id=aid, target_cents=1000, start_cents=0,
                    current_cents=0, created_at=dt.datetime(2026, 9, 1, 12, 0, 0, 1))
    old_bill = RecurringItem(name="Old bill", merchant_key="old", account_id=aid, amount_cents=-100,
                             cadence="monthly", next_date=D(2026, 10, 1), status="active",
                             created_at=dt.datetime(2026, 9, 2, 8, 0))
    db.add(kept_goal)
    db.commit()
    db.add_all([old_goal, old_bill])
    db.commit()
    gid, kept_id, bid = old_goal.id, kept_goal.id, old_bill.id
    token = ok(unlocked.delete(f"/api/accounts/{aid}"))["undo"]["token"]
    db.expire_all()
    db.delete(db.get(Goal, gid))
    db.delete(db.get(RecurringItem, bid))
    db.commit()
    # Same second as the old goal, one microsecond apart, and the same id.
    new_goal = Goal(name="Brand new goal", kind="save", account_id=None, target_cents=500,
                    created_at=dt.datetime(2026, 9, 1, 12, 0, 0, 2))
    new_bill = RecurringItem(name="New bill", merchant_key="new", account_id=None, amount_cents=-100,
                             cadence="monthly", next_date=D(2026, 10, 1), status="active",
                             created_at=dt.datetime(2026, 9, 20, 8, 0))
    db.add_all([new_goal, new_bill])
    db.commit()
    assert (new_goal.id, new_bill.id) == (gid, bid)
    back = ok(unlocked.post("/api/accounts/restore", json={"token": token}), 201)
    db.expire_all()
    assert db.get(Goal, gid).account_id is None
    assert db.get(RecurringItem, bid).account_id is None
    assert db.get(Goal, kept_id).account_id == back["id"]


def test_restore_link_identity_falls_back_to_the_name():
    """A row without ``created_at`` (the column is NOT NULL today; older data or a hand-made
    record) is recognised by its name instead; malformed links are ignored."""
    from types import SimpleNamespace

    from app.services import accounts as service

    when = dt.datetime(2026, 9, 1, 12, 0, 0, 5)
    links = service._link_keys([  # noqa: SLF001
        [4, service._stamp(when), "Trip"], [5, None, "Car"],  # noqa: SLF001
        [True, None, "x"], [0, None, "x"], ["6", None, "x"], [7, 5, "x"], [8, None], "junk",
    ])
    assert set(links) == {4, 5}
    same = service._same_row  # noqa: SLF001
    assert same(SimpleNamespace(created_at=when, name="Renamed"), links[4])
    assert not same(SimpleNamespace(created_at=when.replace(microsecond=6), name="Trip"), links[4])
    assert same(SimpleNamespace(created_at=None, name="Car"), links[5])
    assert not same(SimpleNamespace(created_at=None, name="Boat"), links[5])
    assert not same(SimpleNamespace(created_at=when, name="Car"), links[5])


def test_patch_keeps_debts_positive(unlocked, db, fixed_today):
    """Reviewer's repro: PATCH a manual debt to a negative balance, or recategorize an
    overdrawn account as a loan: the amount owed is stored positive, history too."""
    card = create(unlocked, name="Card", kind="credit_card", current_balance=-300)
    assert card["current_balance"] == 300.0
    assert ok(unlocked.patch(f"/api/accounts/{card['id']}", json={"current_balance": -275}))["current_balance"] == 275.0
    assert snaps(db, card["id"]) == [("2026-09-26", 27500, False)]

    chk = create(unlocked, name="Chk", kind="checking", current_balance=-40)
    add_snap(db, chk["id"], D(2026, 9, 1), -1000)
    moved = ok(unlocked.patch(f"/api/accounts/{chk['id']}", json={"category": "loan"}))
    assert moved["current_balance"] == 40.0
    assert snaps(db, chk["id"]) == [("2026-09-01", 1000, False), ("2026-09-26", 4000, False)]
    # Both in one request: the category is set first.
    other = create(unlocked, name="Other", kind="savings", current_balance=5)
    both = ok(unlocked.patch(f"/api/accounts/{other['id']}", json={"current_balance": -60, "category": "credit"}))
    assert both["current_balance"] == 60.0
    # Assets keep their sign; linked debts keep what the bank says.
    back = ok(unlocked.patch(f"/api/accounts/{chk['id']}", json={"category": "bank", "current_balance": -5}))
    assert back["current_balance"] == -5.0
    linked = make_account(db, "Linked card", "credit", -12, plaid_type="credit", plaid_subtype="credit card")
    assert ok(unlocked.patch(f"/api/accounts/{linked.id}", json={"name": "Card 2"}))["current_balance"] == -12.0


def _lock_and_unlock(client):
    from tests.conftest import PASSWORD

    assert client.post("/api/auth/lock", json={}).status_code == 200
    r = client.post("/api/auth/unlock", json={"password": PASSWORD})
    assert r.status_code == 200, r.text


def test_old_negative_manual_debts_are_fixed_once_on_unlock(unlocked, vault, fixed_today):
    s = vault.db.acquire()
    try:
        loan = make_account(s, "Old loan", "loan", -900)
        card = make_account(s, "Old card", "credit", -50)
        cash = make_account(s, "Overdrawn", "bank", -20)
        linked = make_account(s, "Linked card", "credit", -30, plaid_type="credit", plaid_subtype="credit card")
        ids = (loan.id, card.id, cash.id, linked.id)
        for aid, cents in zip(ids, (-90000, -5000, -2000, -3000)):
            s.add(BalanceSnapshot(account_id=aid, date=D(2026, 9, 1), balance_cents=cents, estimated=False))
        s.commit()
    finally:
        vault.db.release(s)

    _lock_and_unlock(unlocked)
    rows = listed(unlocked)
    assert [rows[i]["current_balance"] for i in ids] == [900.0, 50.0, -20.0, -30.0]
    s = vault.db.acquire()
    try:
        assert [snaps(s, i)[0][1] for i in ids] == [90000, 5000, -2000, -3000]
        assert s.get(AppSetting, "debt_sign_fix_done") is not None
        # Once: a negative debt written later (say, by an older path) is left alone.
        s.get(Account, loan.id).current_balance_cents = -1
        s.commit()
    finally:
        vault.db.release(s)
    _lock_and_unlock(unlocked)
    assert listed(unlocked)[loan.id]["current_balance"] == -0.01


def test_debt_fix_gives_up_after_three_failures(unlocked, db, monkeypatch):
    from app.services import accounts as service

    loan = make_account(db, "Old loan", "loan", -900)

    def boom(session):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(service, "fix_debt_signs_once", boom)
    for n in (1, 2):
        service.safe_fix_debt_signs(db)
        db.expire_all()
        assert db.get(AppSetting, "debt_sign_fix_failures").value == str(n)
        assert db.get(AppSetting, "debt_sign_fix_done") is None
    service.safe_fix_debt_signs(db)
    db.expire_all()
    assert db.get(AppSetting, "debt_sign_fix_done") is not None
    assert db.get(AppSetting, "debt_sign_fix_failures") is None
    monkeypatch.undo()
    service.safe_fix_debt_signs(db)  # marked done: nothing runs
    db.expire_all()
    assert db.get(Account, loan.id).current_balance_cents == -90000


def test_facts_ignore_a_huge_card_limit():
    """Reviewer's repro: a limit too big to multiply used to raise OverflowError."""
    assert account_facts._limit_cents({"limit": 1e307}) is None  # noqa: SLF001
    assert account_facts._limit_cents({"limit": -1e14}) is None  # noqa: SLF001
    assert account_facts._limit_cents({"limit": 2e12}) is None  # noqa: SLF001 - over $1 trillion
    assert account_facts._limit_cents({"limit": 5000.25}) == 500025  # noqa: SLF001


def test_huge_card_limit_doesnt_break_the_sync(unlocked, db, fixed_today, plaid_setup):  # noqa: F811
    plaid_setup.accounts[BANK_TOKEN][0]["balances"]["limit"] = 1e307
    exchange(unlocked, "public-bank", "bank")
    rows = {a["name"]: a for a in ok(unlocked.get("/api/accounts"))}
    assert rows["Checking"]["current_balance"] == 1000.5
    db.expire_all()
    assert account_facts.load(db)[rows["Checking"]["id"]]["limit_cents"] is None


def test_a_failing_facts_step_doesnt_stop_the_sync(unlocked, db, fixed_today, plaid_setup, monkeypatch, caplog):  # noqa: F811
    real = account_facts.record_sync

    def broken(session, touched, lists):
        real(session, touched, lists)  # writes, then fails: the write is undone with it
        session.add(AppSetting(key="facts_half_written", value="1"))
        session.flush()
        raise ValueError("secret-ish detail")

    monkeypatch.setattr(account_facts, "record_sync", broken)
    exchange(unlocked, "public-bank", "bank")
    rows = {a["name"]: a for a in ok(unlocked.get("/api/accounts"))}
    assert rows["Checking"]["current_balance"] == 1000.5
    db.expire_all()
    assert db.get(AppSetting, "facts_half_written") is None
    assert account_facts.load(db) == {}
    assert db.scalar(select(func.count()).select_from(Transaction)) > 0  # transactions came in
    items = ok(unlocked.get("/api/plaid/items"))
    assert items[0]["status"] == "ok"
    assert "ValueError" in caplog.text and "secret-ish" not in caplog.text

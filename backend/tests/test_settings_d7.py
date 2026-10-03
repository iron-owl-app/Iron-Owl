"""Settings redesign (D7, Release 3.7) and D7 update 2: every new backend route."""
from __future__ import annotations

import datetime as dt
import json
import os
import threading

import pytest
from sqlalchemy import select

from app.models import (
    AlertEvent,
    AlertSetting,
    AppSetting,
    Budget,
    CategoryGroup,
    RecurringItem,
    Rule,
    Transaction,
    TransactionSplit,
    TxnCategory,
)
from app.services import automap, automation, folder_picker, prefs, spending
from app.services import calendar as cal
from app.services.alerts import evaluate
from app.services.recurring import anchor_days, merchant_key
from tests.conftest import (
    CSRF,
    FIXED_TODAY,
    PASSWORD,
    SESSION_HEADER,
    add_budget,
    add_txn,
    make_account,
)

D = dt.date
NEW_PASSWORD = "a brand new passphrase 77"


def ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


def make_item(db, name, amount, cadence, next_date, *, account=None, category=None, source="detected"):
    nd = D.fromisoformat(next_date)
    days = anchor_days(cadence, nd)
    item = RecurringItem(
        name=name, merchant_key=merchant_key(name, name), account_id=account.id if account else None,
        amount_cents=round(amount * 100), cadence=cadence, next_date=nd, status="active",
        include_in_forecast=True, source=source, month_days=json.dumps(days) if days else None,
        category_id=category,
    )
    db.add(item)
    db.commit()
    return item


@pytest.fixture
def chk(db, fixed_today):
    return make_account(db, "Checking", "bank", 5000, plaid_type="depository", plaid_subtype="checking", mask="1234")


def no_csrf(client):
    """The same session without the X-FinTrack header."""
    headers = {k: v for k, v in client.headers.items() if k.lower() != "x-fintrack"}
    return headers


# ------------------------------------------------------------------ GET/PATCH /api/settings


def test_settings_defaults_and_patch(unlocked, vault):
    s = ok(unlocked.get("/api/settings"))
    assert s["auto_lock_minutes"] == 15 and s["auto_lock_source"] == "env"
    assert s["auto_lock_choices"] == [5, 15, 30, 60] and s["paycheck_notify"] is True
    assert s["password_changed_at"].endswith("Z")  # written by setup
    s = ok(unlocked.patch("/api/settings", json={"auto_lock_minutes": 5, "paycheck_notify": False}))
    assert (s["auto_lock_minutes"], s["auto_lock_source"], s["paycheck_notify"]) == (5, "vault", False)
    assert ok(unlocked.get("/api/settings")) == s
    # Applied to the vault at once, and shown by /api/auth/status while unlocked.
    assert vault.auto_lock_minutes == 5 and vault.idle_seconds == 300
    assert ok(unlocked.get("/api/auth/status"))["auto_lock_minutes"] == 5
    s = ok(unlocked.patch("/api/settings", json={"paycheck_notify": True}))
    assert (s["auto_lock_minutes"], s["paycheck_notify"]) == (5, True)


@pytest.mark.parametrize("body", [
    {"auto_lock_minutes": 7}, {"auto_lock_minutes": 0}, {"auto_lock_minutes": None}, {"auto_lock_minutes": "off"},
    {"paycheck_notify": None}, {"paycheck_notify": "yes"}, {"paycheck_notify": 1}, {"theme": "dark"},
    {"auto_lock_minutes": 5.5},
])
def test_settings_patch_validation(unlocked, body):
    assert unlocked.patch("/api/settings", json=body).status_code == 422
    assert ok(unlocked.get("/api/settings"))["auto_lock_source"] == "env"


def test_settings_auth_and_csrf(client, unlocked):
    assert client.patch("/api/settings", json={"auto_lock_minutes": 5}, headers={"X-FinTrack": ""}).status_code == 403
    unlocked.headers.pop(SESSION_HEADER)
    assert unlocked.get("/api/settings").status_code == 401
    assert unlocked.patch("/api/settings", json={"auto_lock_minutes": 5}).status_code == 401
    assert unlocked.get("/api/paychecks").status_code == 401


def test_auto_lock_applies_now_and_after_every_unlock(clock, unlocked, vault):
    ok(unlocked.patch("/api/settings", json={"auto_lock_minutes": 5}))
    clock.advance(5 * 60 - 1)
    vault.check_idle()
    assert vault.unlocked
    clock.advance(2)
    vault.check_idle()
    assert not vault.unlocked and vault.lock_reason == "idle"
    # Locked: the env value (the saved one is inside the vault).
    status = ok(unlocked.get("/api/auth/status"))
    assert (status["unlocked"], status["auto_lock_minutes"]) == (False, 15)
    assert vault.auto_lock_minutes == 15
    ok(unlocked.post("/api/auth/unlock", json={"password": PASSWORD}))
    assert vault.auto_lock_minutes == 5  # read again at unlock
    ok(unlocked.patch("/api/settings", json={"auto_lock_minutes": 60}))
    clock.advance(59 * 60)
    vault.check_idle()
    assert vault.unlocked


def test_auto_lock_set_while_locked_is_ignored(unlocked, vault):
    ok(unlocked.post("/api/auth/lock", json={}))
    vault.set_auto_lock(5)
    assert vault.auto_lock_minutes == 15


def test_bad_stored_auto_lock_falls_back_to_env(unlocked, vault, db):
    db.add(AppSetting(key=prefs.AUTO_LOCK_KEY, value="2"))
    db.commit()
    ok(unlocked.post("/api/auth/lock", json={}))
    ok(unlocked.post("/api/auth/unlock", json={"password": PASSWORD}))
    assert vault.auto_lock_minutes == 15
    assert ok(unlocked.get("/api/settings"))["auto_lock_source"] == "env"


# ------------------------------------------------------------------ password_changed_at + forced backup


def _stored(vault, key):
    session = vault.db.acquire()
    try:
        row = session.get(AppSetting, key)
        return row.value if row else None
    finally:
        vault.db.release(session)


def test_change_password_records_time_and_forces_a_backup(unlocked, vault, tmp_path, monkeypatch):
    first = _stored(vault, prefs.PASSWORD_CHANGED_KEY)
    assert first and first.endswith("Z")
    monkeypatch.setattr(prefs, "utcnow", lambda: dt.datetime(2030, 1, 2, 3, 4, 5))
    body = ok(unlocked.post("/api/auth/change-password",
                            json={"current_password": PASSWORD, "new_password": NEW_PASSWORD}))
    assert body["backup"] is None  # no backup folder set
    assert ok(unlocked.get("/api/settings"))["password_changed_at"] == "2030-01-02T03:04:05Z"

    folder = tmp_path / "backups"
    folder.mkdir()
    ok(unlocked.put("/api/backup/auto", json={"dir": str(folder), "keep": 10}))
    body = ok(unlocked.post("/api/auth/change-password",
                            json={"current_password": NEW_PASSWORD, "new_password": PASSWORD}))
    assert body["backup"] == {"ok": True}
    assert len([p for p in folder.iterdir() if automation.FILE_RE.match(p.name)]) == 1
    # A wrong current password changes nothing and forces nothing.
    r = unlocked.post("/api/auth/change-password", json={"current_password": "nope nope nope", "new_password": NEW_PASSWORD})
    assert r.status_code == 401 and "backup" not in r.json()


def test_forced_backup_failure_is_reported_not_raised(unlocked, tmp_path, monkeypatch):
    folder = tmp_path / "backups"
    folder.mkdir()
    ok(unlocked.put("/api/backup/auto", json={"dir": str(folder), "keep": 10}))
    folder.rmdir()  # gone since
    body = ok(unlocked.post("/api/auth/change-password",
                            json={"current_password": PASSWORD, "new_password": NEW_PASSWORD}))
    assert body["backup"] == {"ok": False} and body["ok"] is True


def test_recover_records_password_time(client, vault, monkeypatch):
    r = ok(client.post("/api/auth/setup", json={"password": PASSWORD}))
    code = "".join(r["recovery"]["groups"])
    ok(client.post("/api/auth/lock", json={}))
    monkeypatch.setattr(prefs, "utcnow", lambda: dt.datetime(2031, 5, 6, 7, 8, 9))
    ok(client.post("/api/auth/recover", json={"code": code, "new_password": NEW_PASSWORD}))
    assert ok(client.get("/api/settings"))["password_changed_at"] == "2031-05-06T07:08:09Z"


# ------------------------------------------------------------------ paychecks + Budget's suggestion


def test_paychecks_list(unlocked, db, chk):
    pay = make_item(db, "ACME PAYROLL", 1400, "biweekly", "2026-10-02", account=chk)
    pension = make_item(db, "STATE PENSION", 1000.01, "monthly", "2026-10-01")
    make_item(db, "Netflix", -15.49, "monthly", "2026-10-05", account=chk)  # money out: never listed
    make_item(db, "Bonus", 1200, "yearly", "2027-03-01", account=chk)
    body = ok(unlocked.get("/api/paychecks"))
    by_name = {i["name"]: i for i in body["items"]}
    assert set(by_name) == {"ACME PAYROLL", "STATE PENSION", "Bonus"}
    assert by_name["ACME PAYROLL"] == {
        "id": pay.id, "name": "ACME PAYROLL", "amount": 1400.0, "cadence": "biweekly", "next_date": "2026-10-02",
        "account": {"id": chk.id, "name": "Checking", "mask": "1234"}, "per_month": 3033.33,
    }
    assert by_name["STATE PENSION"]["account"] is None and by_name["STATE PENSION"]["per_month"] == 1000.01
    assert by_name["Bonus"]["per_month"] == 100.0 and by_name["Bonus"]["next_date"] == "2027-03-01"
    assert body["monthly_total"] == round(3033.33 + 1000.01 + 100.0, 2)
    # September: paychecks on 9/4 and 9/18 (2 x 1,400) + the pension on 9/1.
    assert body["this_month_total"] == 3800.01
    assert (body["income_mode"], body["notify"]) == ("off", True)
    assert pension.id in {i["id"] for i in body["items"]}


def test_per_month_factors():
    from app.routers.preferences import per_month_cents

    assert [per_month_cents(100_00, c) for c in
            ("weekly", "biweekly", "semimonthly", "monthly", "quarterly", "yearly", "once")] == [
        433_33, 216_67, 200_00, 100_00, 33_33, 8_33, 0]


def test_transfers_are_never_paychecks(unlocked, db, chk):
    # Money moved in from their own savings every month: Transfer in, not income.
    for month in ("06", "07", "08", "09"):
        add_txn(db, chk.id, f"2026-{month}-03", 200, "VACATION SAVINGS", category="TRANSFER_IN")
        add_txn(db, chk.id, f"2026-{month}-05", 300, "OWN ACCOUNT MOVE", category="INCOME", is_transfer=True)
        add_txn(db, chk.id, f"2026-{month}-01", 1000, "STATE PENSION", category="INCOME")
    make_item(db, "VACATION SAVINGS", 200, "monthly", "2026-10-03", account=chk)
    make_item(db, "OWN ACCOUNT MOVE", 300, "monthly", "2026-10-05", account=chk)
    pension = make_item(db, "STATE PENSION", 1000, "monthly", "2026-10-01", account=chk)
    body = ok(unlocked.get("/api/paychecks"))
    assert [i["id"] for i in body["items"]] == [pension.id]
    assert body["this_month_total"] == 1000.0
    # Budget's suggestion uses the same filtered list.
    income = ok(unlocked.get("/api/budgets"))["income"]
    assert (income["suggested"], income["suggested_source"]) == (1000.0, "paychecks")
    # Given an income budget category by hand, it counts as income again.
    savings = db.scalar(select(RecurringItem).where(RecurringItem.name == "VACATION SAVINGS"))
    savings.category_id = "INCOME"
    db.commit()
    assert len(ok(unlocked.get("/api/paychecks"))["items"]) == 2


def test_budget_suggests_this_months_paychecks(unlocked, db, chk):
    for month in ("06", "07", "08"):
        add_txn(db, chk.id, f"2026-{month}-15", 3000, "ACME PAYROLL", category="INCOME")
    income = ok(unlocked.get("/api/budgets"))["income"]
    assert (income["suggested"], income["suggested_source"]) == (3000.0, "average")
    setup = ok(unlocked.get("/api/budgets/setup"))["income"]
    assert (setup["suggested"], setup["suggested_source"]) == (3000.0, "average")
    item = make_item(db, "ACME PAYROLL", 1400, "biweekly", "2026-10-02", account=chk)
    income = ok(unlocked.get("/api/budgets"))["income"]
    assert (income["suggested"], income["suggested_source"]) == (2800.0, "paychecks")
    assert ok(unlocked.get("/api/budgets/setup"))["income"]["suggested_source"] == "paychecks"
    # Skips are applied.
    ok(unlocked.put(f"/api/recurring/{item.id}/occurrences/2026-09-18", json={"skipped": True}))
    assert ok(unlocked.get("/api/budgets"))["income"]["suggested"] == 1400.0
    assert ok(unlocked.get("/api/paychecks"))["this_month_total"] == 1400.0


def test_paychecks_with_none_this_month_suggest_nothing(unlocked, db, chk):
    add_txn(db, chk.id, "2026-08-15", 3000, "PAY", category="INCOME")
    make_item(db, "DIVIDEND", 500, "quarterly", "2026-11-15", account=chk)
    income = ok(unlocked.get("/api/budgets"))["income"]
    assert (income["suggested"], income["suggested_source"]) == (None, "paychecks")


def _expected_mode(unlocked, amount):
    ok(unlocked.put("/api/budgets/settings", json={"income_mode": "expected"}))
    ok(unlocked.put(f"/api/budgets/{FIXED_TODAY:%Y-%m}", json={"income_expected": amount}))


def test_paycheck_notify_gates_the_budget_note_and_alert_wording(unlocked, db, chk):
    _expected_mode(unlocked, 1000)
    pay = add_txn(db, chk.id, "2026-09-25", 1500, "PAYROLL", merchant="Acme Corp", category="INCOME")
    assert ok(unlocked.get("/api/budgets"))["income"]["event"]["kind"] == "more"
    ok(unlocked.patch("/api/settings", json={"paycheck_notify": False}))
    assert ok(unlocked.get("/api/budgets"))["income"]["event"] is None
    db.expire_all()
    evaluate(db, FIXED_TODAY, new_transaction_ids=[pay.id])
    db.commit()
    titles = [e.title for e in db.scalars(select(AlertEvent).where(AlertEvent.key == "income"))]
    assert titles == ["$1,500 new to assign"]  # the plain wording, not "more than expected"
    ok(unlocked.patch("/api/settings", json={"paycheck_notify": True}))
    assert ok(unlocked.get("/api/budgets"))["income"]["event"]["kind"] == "more"


def test_paycheck_notify_on_keeps_expected_wording(unlocked, db, chk):
    _expected_mode(unlocked, 1000)
    pay = add_txn(db, chk.id, "2026-09-25", 1500, "PAYROLL", merchant="Acme Corp", category="INCOME")
    db.expire_all()
    evaluate(db, FIXED_TODAY, new_transaction_ids=[pay.id])
    db.commit()
    titles = [e.title for e in db.scalars(select(AlertEvent).where(AlertEvent.key == "income"))]
    assert titles == ["$500 more than expected came in"]


# ------------------------------------------------------------------ categories: create in a group


def test_create_category_in_a_group(unlocked):
    group = ok(unlocked.post("/api/category-groups", json={"name": "Home"}), 201)
    cat = ok(unlocked.post("/api/categories", json={"name": "Garden", "kind": "spending", "group_id": group["id"]}), 201)
    assert cat["group_id"] == group["id"]
    assert ok(unlocked.post("/api/categories", json={"name": "Pets", "kind": "spending", "group_id": None}), 201)["group_id"] is None
    r = unlocked.post("/api/categories", json={"name": "Kids", "kind": "spending", "group_id": 999})
    assert r.status_code == 422 and r.json()["detail"] == "unknown group"
    assert unlocked.post("/api/categories", json={"name": "Kids", "kind": "spending", "group_id": 0}).status_code == 422
    assert unlocked.post("/api/categories", json={"name": "Kids", "kind": "spending", "group_id": "x"}).status_code == 422
    assert "Kids" not in {c["name"] for c in ok(unlocked.get("/api/categories"))}


# ------------------------------------------------------------------ categories: snapshot / delete / restore


def _state(db, cid):
    """Everything a category delete touches, comparable before/after."""
    db.expire_all()
    cat = db.get(TxnCategory, cid)
    return {
        "category": None if cat is None else (cat.name, cat.hue, cat.kind, cat.custom, cat.hidden, cat.position,
                                              cat.group_id, cat.target_kind, cat.target_cents, cat.target_date),
        "txns": sorted((t.id, t.category, t.category_source, t.rule_id) for t in db.scalars(select(Transaction))),
        "splits": sorted((s.id, s.category) for s in db.scalars(select(TransactionSplit))),
        "budgets": sorted((b.month, b.category, b.limit_cents, b.removed, b.restart) for b in db.scalars(select(Budget))),
        "bills": sorted((i.id, i.category_id) for i in db.scalars(select(RecurringItem))),
        "rules": [(r.id, r.position, r.field, r.op, r.text, r.amount_op, r.amount_cents, r.action, r.category, r.enabled)
                  for r in db.scalars(select(Rule).order_by(Rule.position, Rule.id))],
        "savings": spending.get_setting(db, spending.BUDGET_SAVINGS_SETTING),
        "automap": automap.stored(db),
        "excluded": cal.excluded_plans(db),
    }


@pytest.fixture
def pets(unlocked, db, chk):
    c = unlocked
    group = ok(c.post("/api/category-groups", json={"name": "Home"}), 201)
    cat = ok(c.post("/api/categories", json={"name": "Pets", "kind": "spending", "hue": 30, "group_id": group["id"]}), 201)
    cid = cat["id"]
    ok(c.patch(f"/api/categories/{cid}", json={"target": {"kind": "monthly", "amount": 80}}))
    vet = add_txn(db, chk.id, "2026-09-05", -120, "VET CLINIC", merchant="Vet")
    chewy = add_txn(db, chk.id, "2026-09-06", -40, "CHEWY.COM", merchant="Chewy")
    petco = add_txn(db, chk.id, "2026-09-07", -25, "PETCO", merchant="Petco")
    costco = add_txn(db, chk.id, "2026-09-08", -100, "COSTCO", merchant="Costco", category="GENERAL_MERCHANDISE")
    ok(c.patch(f"/api/transactions/{vet.id}", json={"category": cid}))  # hand-set
    ok(c.put(f"/api/transactions/{costco.id}/splits", json={"splits": [
        {"amount": -70, "category": "GENERAL_MERCHANDISE"}, {"amount": -30, "category": cid}]}))
    # Rules: one before, two for Pets around another, one after.
    r0 = ok(c.post("/api/rules", json={"field": "any", "op": "contains", "text": "shell", "action": "category",
                                       "category": "TRANSPORTATION"}), 201)
    r1 = ok(c.post("/api/rules", json={"field": "merchant", "op": "is", "text": "chewy", "action": "category",
                                       "category": cid}), 201)
    r2 = ok(c.post("/api/rules", json={"field": "any", "op": "contains", "text": "zzz", "action": "transfer"}), 201)
    r3 = ok(c.post("/api/rules", json={"field": "any", "op": "contains", "text": "petco", "action": "category",
                                       "category": cid, "amount_op": "gt", "amount": 10, "enabled": True}), 201)
    ok(c.put(f"/api/budgets/{FIXED_TODAY:%Y-%m}", json={"assigned": {cid: 80}}))
    add_budget(db, "2026-08", {cid: 60})
    bill = make_item(db, "Pet insurance", -35, "monthly", "2026-10-12", account=chk, category=cid)
    # Settings that point at it.
    spending.put_setting(db, spending.BUDGET_SAVINGS_SETTING, cid)
    automap.record(db, {"groceries": "FOOD_AND_DRINK", "gas": cid})
    cal.save_settings(db, excluded=[cid])
    db.commit()
    return {"cid": cid, "group": group, "rules": [r0, r1, r2, r3], "bill": bill, "vet": vet, "chewy": chewy,
            "petco": petco}


def test_category_delete_and_restore_round_trip(unlocked, db, pets):
    c, cid = unlocked, pets["cid"]
    before = _state(db, cid)
    assert [r[4] for r in before["rules"]] == ["shell", "chewy", "zzz", "petco"]
    snap = ok(c.get(f"/api/categories/{cid}/snapshot"))
    assert snap["category"]["group_id"] == pets["group"]["id"] and snap["category"]["custom"] is True
    assert snap["hand_set"] == [{"transaction_id": pets["vet"].id, "source": "user"}]
    assert len(snap["splits"]) == 1 and [b["month"] for b in snap["budgets"]] == ["2026-08", "2026-09"]
    assert snap["bills"] == [pets["bill"].id]
    assert [(r["text"], r["position"], r["matches"]) for r in snap["rules"]] == [("chewy", 1, 1), ("petco", 3, 1)]
    assert snap["flags"] == {"savings_category": True, "auto_map_key": "gas", "excluded_plan": True}

    assert c.delete(f"/api/categories/{cid}").status_code == 204
    gone = _state(db, cid)
    assert gone["category"] is None
    assert [(r[1], r[4]) for r in gone["rules"]] == [(0, "shell"), (1, "zzz")]  # dense again
    assert all(t[1] != cid for t in gone["txns"]) and all(s[1] != cid for s in gone["splits"])
    assert gone["bills"] == [(pets["bill"].id, None)] and not any(b[1] == cid for b in gone["budgets"])

    restored = ok(c.post("/api/categories/restore", json=snap), 201)
    assert restored["id"] == cid and restored["name"] == "Pets" and restored["target"]["amount"] == 80.0
    assert _state(db, cid) == before


def test_restore_skips_what_changed_since(unlocked, db, pets, chk):
    c, cid = unlocked, pets["cid"]
    snap = ok(c.get(f"/api/categories/{cid}/snapshot"))
    assert c.delete(f"/api/categories/{cid}").status_code == 204
    # Since the delete: the vet visit set by hand to Medical, the bill given another category,
    # the group deleted, a rule's old id taken.
    ok(c.patch(f"/api/transactions/{pets['vet'].id}", json={"category": "MEDICAL"}))
    ok(c.patch(f"/api/recurring/{pets['bill'].id}", json={"category_id": "GENERAL_SERVICES"}))
    assert c.delete(f"/api/category-groups/{pets['group']['id']}").status_code == 204
    extra = _rule(c, "extra")  # takes the highest deleted rule's id (SQLite reuses it)
    assert extra["id"] == pets["rules"][3]["id"]
    restored = ok(c.post("/api/categories/restore", json=snap), 201)
    assert restored["group_id"] is None
    db.expire_all()
    assert db.get(Transaction, pets["vet"].id).category == "MEDICAL"
    assert db.get(RecurringItem, pets["bill"].id).category_id == "GENERAL_SERVICES"
    rules = ok(c.get("/api/rules"))
    assert [r["text"] for r in rules] == ["shell", "chewy", "zzz", "petco", "extra"]
    assert rules[3]["id"] != extra["id"] and rules[1]["id"] == pets["rules"][1]["id"]
    # Rules work again: Chewy is back in Pets through its rule.
    assert db.get(Transaction, pets["chewy"].id).category == cid


def test_restore_conflicts_and_validation(unlocked, db, pets):
    c, cid = unlocked, pets["cid"]
    snap = ok(c.get(f"/api/categories/{cid}/snapshot"))
    assert c.post("/api/categories/restore", json=snap).status_code == 409  # still exists
    assert c.delete(f"/api/categories/{cid}").status_code == 204
    ok(c.post("/api/categories", json={"name": "pets", "kind": "spending"}), 201)
    assert c.post("/api/categories/restore", json=snap).status_code == 409  # name taken meanwhile
    snap["category"]["name"] = "Pets 2"

    def bad(mutate):
        body = json.loads(json.dumps(snap))
        mutate(body)
        r = c.post("/api/categories/restore", json=body)
        assert r.status_code == 422, r.text

    bad(lambda b: b["category"].update(custom=False))
    bad(lambda b: b["category"].update(id="FOOD_AND_DRINK"))
    bad(lambda b: b["category"].update(id="c_1; DROP"))
    bad(lambda b: b["category"].update(hue=400))
    bad(lambda b: b["category"].update(extra=1))
    bad(lambda b: b["rules"][0].update(category="TRAVEL"))
    bad(lambda b: b["rules"][0].update(action="transfer"))
    bad(lambda b: b["rules"].append(dict(b["rules"][0])))  # same id twice
    bad(lambda b: b["budgets"].append(dict(b["budgets"][0])))  # same month twice
    bad(lambda b: b["budgets"][0].update(month="2026-13"))
    bad(lambda b: b["budgets"][0].update(assigned=1e13))
    bad(lambda b: b["hand_set"][0].update(source="plaid"))
    bad(lambda b: b.update(splits=list(range(1, 50_002))))
    bad(lambda b: b["flags"].update(auto_map_key="rent"))
    bad(lambda b: b["category"].update(target={"kind": "monthly", "amount": 0}))
    r = c.post("/api/categories/restore", content=json.dumps(snap).replace('"hue": 30', '"hue": NaN'),
               headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    # Nothing was written by any of them.
    db.expire_all()
    assert db.get(TxnCategory, cid) is None
    assert c.post("/api/categories/restore", json=snap).status_code == 201


def test_restore_by_date_target_in_the_past(unlocked, db, pets, fixed_today):
    c, cid = unlocked, pets["cid"]
    ok(c.patch(f"/api/categories/{cid}", json={"target": {"kind": "by_date", "amount": 500, "date": "2026-12-01"}}))
    snap = ok(c.get(f"/api/categories/{cid}/snapshot"))
    assert c.delete(f"/api/categories/{cid}").status_code == 204
    fixed_today.set(D(2027, 1, 5))  # the date has passed since
    assert ok(c.post("/api/categories/restore", json=snap), 201)["target"]["date"] == "2026-12-01"


def test_category_routes_csrf(client, unlocked, pets):
    cid = pets["cid"]
    snap = ok(unlocked.get(f"/api/categories/{cid}/snapshot"))
    headers = {"X-FinTrack": "0"}
    assert unlocked.delete(f"/api/categories/{cid}", headers=headers).status_code == 403
    assert unlocked.post("/api/categories/restore", json=snap, headers=headers).status_code == 403
    assert unlocked.post("/api/category-groups/restore", json={"id": 1, "name": "x", "position": 0},
                         headers=headers).status_code == 403
    unlocked.headers.pop(SESSION_HEADER)
    assert unlocked.get(f"/api/categories/{cid}/snapshot").status_code == 401
    assert unlocked.post("/api/categories/restore", json=snap).status_code == 401


# ------------------------------------------------------------------ category groups: restore


def test_group_restore_round_trip(unlocked, db):
    c = unlocked
    for name in ("A", "B", "C"):
        ok(c.post("/api/category-groups", json={"name": name}), 201)
    b = next(g for g in ok(c.get("/api/category-groups")) if g["name"] == "B")
    garden = ok(c.post("/api/categories", json={"name": "Garden", "kind": "spending", "group_id": b["id"]}), 201)
    tools = ok(c.post("/api/categories", json={"name": "Tools", "kind": "spending", "group_id": b["id"]}), 201)
    before = ok(c.get("/api/category-groups"))
    assert c.delete(f"/api/category-groups/{b['id']}").status_code == 204
    # Tools was put in another group meanwhile: it stays there.
    a = before[0]
    ok(c.patch(f"/api/categories/{tools['id']}", json={"group_id": a["id"]}))
    body = {"id": b["id"], "name": "B", "position": 1, "category_ids": [garden["id"], tools["id"], "NOPE"]}
    out = ok(c.post("/api/category-groups/restore", json=body), 201)
    assert [(g["id"], g["name"], g["position"]) for g in out] == [(g["id"], g["name"], i) for i, g in enumerate(before)]
    assert next(g for g in out if g["name"] == "B")["category_ids"] == [garden["id"]]
    assert tools["id"] in next(g for g in out if g["name"] == "A")["category_ids"]


def test_group_restore_new_id_when_taken_and_conflicts(unlocked, db, monkeypatch):
    c = unlocked
    g = ok(c.post("/api/category-groups", json={"name": "Old"}), 201)
    assert c.post("/api/category-groups/restore", json={"id": g["id"], "name": "old", "position": 0}).status_code == 409
    out = ok(c.post("/api/category-groups/restore", json={"id": g["id"], "name": "Back", "position": 99}), 201)
    back = next(x for x in out if x["name"] == "Back")
    assert back["id"] != g["id"] and [x["name"] for x in out] == ["Old", "Back"]
    from app.routers import category_groups

    monkeypatch.setattr(category_groups, "MAX_GROUPS", 2)
    r = c.post("/api/category-groups/restore", json={"id": 50, "name": "Third", "position": 0})
    assert r.status_code == 422
    for body in ({"id": 0, "name": "x", "position": 0}, {"id": 1, "name": "", "position": 0},
                 {"id": 1, "name": "x", "position": -1}, {"id": 1, "name": "x", "position": 0, "extra": 1},
                 {"id": 1, "name": "x" * 61, "position": 0},
                 {"id": 1, "name": "x", "position": 0, "category_ids": ["a"] * 50_001}):
        assert c.post("/api/category-groups/restore", json=body).status_code == 422


# ------------------------------------------------------------------ rules: position


def _rule(c, text, **kw):
    body = {"field": "any", "op": "contains", "text": text, "action": "category", "category": "TRAVEL", **kw}
    return ok(c.post("/api/rules", json=body), 201)


def test_rule_position(unlocked):
    c = unlocked
    for t in ("a", "b", "c"):
        _rule(c, t)
    assert _rule(c, "top", position=0)["position"] == 0
    assert _rule(c, "mid", position=2)["position"] == 2
    assert _rule(c, "end", position=999)["position"] == 5
    assert [(r["text"], r["position"]) for r in ok(c.get("/api/rules"))] == [
        ("top", 0), ("a", 1), ("mid", 2), ("b", 3), ("c", 4), ("end", 5)]
    for bad in (-1, 100_001, "x", 1.5):
        assert c.post("/api/rules", json={"field": "any", "op": "contains", "text": "z", "action": "transfer",
                                          "position": bad}).status_code == 422
    # A rejected rule never renumbers the others.
    assert c.post("/api/rules", json={"field": "any", "op": "contains", "text": "  ", "action": "transfer",
                                      "position": 0}).status_code == 422
    assert [r["position"] for r in ok(c.get("/api/rules"))] == [0, 1, 2, 3, 4, 5]


def test_rule_delete_then_undo_at_old_position(unlocked):
    c = unlocked
    rules = [_rule(c, t) for t in ("a", "b", "c")]
    assert c.delete(f"/api/rules/{rules[1]['id']}").status_code == 204
    back = _rule(c, "b", position=rules[1]["position"])
    assert [r["text"] for r in ok(c.get("/api/rules"))] == ["a", "b", "c"] and back["position"] == 1


# ------------------------------------------------------------------ rules: preview (update 2)


def test_preview_would_change_and_sample(unlocked, db, chk):
    c = unlocked
    old = add_txn(db, chk.id, "2026-08-01", -10, "STARBUCKS 1", merchant="Starbucks")
    new = add_txn(db, chk.id, "2026-09-01", -12, "STARBUCKS 2", merchant="Starbucks")
    hand = add_txn(db, chk.id, "2026-09-02", -9, "STARBUCKS 3", merchant="Starbucks")
    ok(c.patch(f"/api/transactions/{hand.id}", json={"category": "ENTERTAINMENT"}))
    body = {"field": "any", "op": "contains", "text": "starbucks", "action": "category", "category": "TRAVEL"}
    p = ok(c.post("/api/rules/preview", json=body))
    assert (p["matches"], p["would_change"]) == (3, 2)  # the hand-set one never changes
    assert [s["id"] for s in p["sample"]] == [new.id, old.id]  # newest first
    assert p["sample"][0] == {
        "id": new.id, "date": "2026-09-01", "name": "STARBUCKS 2", "merchant": "Starbucks", "amount": -12.0,
        "from": {"id": "FOOD_AND_DRINK", "name": "Food and drink", "hue": p["sample"][0]["from"]["hue"]},
        "to": {"id": "TRAVEL", "name": "Travel", "hue": p["sample"][0]["to"]["hue"]},
    }
    # Already in Food and drink: a rule to the same category changes nothing.
    same = {**body, "category": "FOOD_AND_DRINK"}
    assert ok(c.post("/api/rules/preview", json=same))["would_change"] == 0
    # A new rule goes on top: an existing rule for the same purchases doesn't hide it.
    _rule(c, "starbucks", category="MEDICAL")
    assert ok(c.post("/api/rules/preview", json=body))["would_change"] == 2
    # Editing that rule (rule_id) previews it at its own position.
    rid = ok(c.get("/api/rules"))[0]["id"]
    p = ok(c.post("/api/rules/preview", json={**body, "rule_id": rid}))
    assert p["would_change"] == 2 and p["sample"][0]["from"]["id"] == "MEDICAL"
    assert ok(c.post("/api/rules/preview", json={**body, "category": "MEDICAL", "rule_id": rid}))["would_change"] == 0
    # Turned off: its purchases go back to the bank's category.
    p = ok(c.post("/api/rules/preview", json={**body, "category": "MEDICAL", "rule_id": rid, "enabled": False}))
    assert p["would_change"] == 2 and p["sample"][0]["to"]["id"] == "FOOD_AND_DRINK"
    assert c.post("/api/rules/preview", json={**body, "rule_id": 9999}).status_code == 404
    assert c.post("/api/rules/preview", json={**body, "rule_id": 0}).status_code == 422
    # Nothing was saved by previewing.
    assert len(ok(c.get("/api/rules"))) == 1


def test_preview_sample_is_capped(unlocked, db, chk):
    for i in range(25):
        add_txn(db, chk.id, D(2026, 9, 1) + dt.timedelta(days=i % 20), -1, f"UBER {i}", merchant="Uber")
    p = ok(unlocked.post("/api/rules/preview", json={"field": "any", "op": "contains", "text": "uber",
                                                     "action": "category", "category": "TRAVEL"}))
    assert (p["matches"], p["would_change"], len(p["sample"])) == (25, 25, 20)


# ------------------------------------------------------------------ alerts (update 2)


def _settings(c):
    return {s["key"]: s for s in ok(c.get("/api/alerts/settings"))}


def test_combined_alert_settings(unlocked):
    c = unlocked
    out = ok(c.put("/api/alerts/settings", json={"low": {"enabled": False, "value": 750},
                                                 "reminder": {"enabled": False}, "big": {"value": 300}}))
    s = {x["key"]: x for x in out}
    assert (s["low"]["enabled"], s["low_ahead"]["enabled"], s["low"]["value"], s["low_ahead"]["value"]) == (
        False, False, 750.0, 7.0)
    assert (s["reminder"]["enabled"], s["due"]["enabled"]) == (False, False)
    assert (s["big"]["enabled"], s["big"]["value"]) == (True, 300.0)
    s = {x["key"]: x for x in ok(c.put("/api/alerts/settings", json={"low": {"enabled": True},
                                                                      "reminder": {"enabled": True}}))}
    assert s["low"]["enabled"] and s["low_ahead"]["enabled"] and s["reminder"]["enabled"] and s["due"]["enabled"]
    assert ok(c.put("/api/alerts/settings", json={})) == ok(c.get("/api/alerts/settings"))


@pytest.mark.parametrize("body", [
    {"low": {"value": 0}}, {"low": {"value": -5}}, {"low": None}, {"newrec": {"value": 3}},
    {"price": {"value": 3}}, {"low_ahead": {"enabled": False}}, {"low": {"enabled": None}},
    {"low": {"enabled": False}, "big": {"value": 2e12}}, {"low": {"enabled": False, "extra": 1}},
])
def test_combined_alert_settings_is_atomic(unlocked, body):
    before = ok(unlocked.get("/api/alerts/settings"))
    assert unlocked.put("/api/alerts/settings", json=body).status_code == 422
    assert ok(unlocked.get("/api/alerts/settings")) == before


def test_price_switch_in_the_alerts_tab(unlocked):
    """Release 3.19: the Alerts tab's "amount changes" row sets the ``price`` key (on by default)."""
    assert _settings(unlocked)["price"]["enabled"] is True
    s = {x["key"]: x for x in ok(unlocked.put("/api/alerts/settings", json={"price": {"enabled": False}}))}
    assert s["price"]["enabled"] is False and s["newrec"]["enabled"] is True
    assert _settings(unlocked)["price"]["enabled"] is False
    ok(unlocked.put("/api/alerts/settings", json={"price": {"enabled": True}}))
    assert _settings(unlocked)["price"]["enabled"] is True


def test_events_of_alerts_that_are_off_are_hidden(unlocked, db):
    now = dt.datetime(2026, 9, 26, 12)
    for key in ("low", "big", "reminder"):
        db.add(AlertEvent(key=key, severity="warn", title=key, body="", created_at=now, read=False,
                          dedupe_key=f"{key}:x", cleared=False))
    db.commit()
    assert {e["key"] for e in ok(unlocked.get("/api/alerts/events"))} == {"low", "big", "reminder"}
    assert ok(unlocked.get("/api/summary"))["unread_alerts"] == 3
    ok(unlocked.put("/api/alerts/settings", json={"big": {"enabled": False}, "reminder": {"enabled": False}}))
    assert [e["key"] for e in ok(unlocked.get("/api/alerts/events"))] == ["low"]
    assert ok(unlocked.get("/api/summary"))["unread_alerts"] == 1
    ok(unlocked.put("/api/alerts/settings", json={"big": {"enabled": True}}))
    assert ok(unlocked.get("/api/summary"))["unread_alerts"] == 2


def test_budget_alert_only_over_the_plan(unlocked, db, chk):
    ok(unlocked.put(f"/api/budgets/{FIXED_TODAY:%Y-%m}", json={"assigned": {"TRAVEL": 100}}))
    add_txn(db, chk.id, "2026-09-05", -100, "FLIGHT", category="TRAVEL")
    db.expire_all()
    assert [e for e in evaluate(db, FIXED_TODAY) if e.key == "budget"] == []
    add_txn(db, chk.id, "2026-09-06", -0.01, "FEE", category="TRAVEL")
    db.expire_all()
    assert [e.title for e in evaluate(db, FIXED_TODAY) if e.key == "budget"] == ["Travel went over its plan"]


def test_alert_routes_csrf(unlocked):
    assert unlocked.put("/api/alerts/settings", json={}, headers={"X-FinTrack": "no"}).status_code == 403
    unlocked.headers.pop(SESSION_HEADER)
    assert unlocked.put("/api/alerts/settings", json={}).status_code == 401


# ------------------------------------------------------------------ backups: suggested folder, create


def test_suggested_folder(unlocked, tmp_path, monkeypatch):
    one = tmp_path / "OneDrive"
    docs = tmp_path / "Documents"
    docs.mkdir()
    monkeypatch.setattr(automation, "onedrive_dir", lambda: str(one))
    monkeypatch.setattr(automation, "documents_dir", lambda: str(docs))
    # OneDrive isn't here (the folder doesn't exist): Documents.
    assert ok(unlocked.get("/api/backup/auto/suggested-folder")) == {
        "dir": str(docs / "Iron Owl backups"), "exists": False}
    one.mkdir()
    assert ok(unlocked.get("/api/backup/auto/suggested-folder")) == {
        "dir": str(one / "Iron Owl backups"), "exists": False}
    (one / "Iron Owl backups").mkdir()
    assert ok(unlocked.get("/api/backup/auto/suggested-folder"))["exists"] is True
    # A network OneDrive path is never touched or suggested.
    monkeypatch.setattr(automation, "onedrive_dir", lambda: r"\\server\share\OneDrive")
    assert ok(unlocked.get("/api/backup/auto/suggested-folder"))["dir"] == str(docs / "Iron Owl backups")
    monkeypatch.setattr(automation, "documents_dir", lambda: None)
    assert ok(unlocked.get("/api/backup/auto/suggested-folder")) == {"dir": None, "exists": False}
    unlocked.headers.pop(SESSION_HEADER)
    assert unlocked.get("/api/backup/auto/suggested-folder").status_code == 401


def test_older_fintrack_backups_folder_keeps_working(unlocked, tmp_path, monkeypatch):
    """Before 2.0.0 the suggested folder was "FinTrack backups": it keeps working with no steps."""
    one = tmp_path / "OneDrive"
    legacy = one / "FinTrack backups"
    legacy.mkdir(parents=True)
    monkeypatch.setattr(automation, "onedrive_dir", lambda: str(one))
    monkeypatch.setattr(automation, "documents_dir", lambda: None)
    # Turning backups back on suggests the folder that is already there.
    assert ok(unlocked.get("/api/backup/auto/suggested-folder")) == {"dir": str(legacy), "exists": True}
    # A location saved by an older version still validates, and backups land there.
    out = ok(unlocked.put("/api/backup/auto", json={"dir": str(legacy), "keep": 10}))
    assert out["dir"] == str(legacy.resolve())
    assert ok(unlocked.post("/api/backup/auto/run", json={}))["ok"] is True
    assert len(list(legacy.glob("*.ftbackup"))) == 1
    # Once an "Iron Owl backups" folder exists, new suggestions use it.
    (one / "Iron Owl backups").mkdir()
    assert ok(unlocked.get("/api/backup/auto/suggested-folder")) == {
        "dir": str(one / "Iron Owl backups"), "exists": True}


def test_turn_on_creates_the_folder(unlocked, tmp_path):
    target = tmp_path / "OneDrive" / "FinTrack backups"
    body = {"dir": str(target), "keep": 10}
    assert unlocked.put("/api/backup/auto", json=body).status_code == 422  # missing, create not asked
    assert not target.exists()
    out = ok(unlocked.put("/api/backup/auto", json={**body, "create": True}))
    assert target.is_dir() and out["dir"] == str(target.resolve())
    # Already there: create is harmless.
    ok(unlocked.put("/api/backup/auto", json={**body, "create": True}))
    run = ok(unlocked.post("/api/backup/auto/run", json={}))
    assert run["ok"] is True


@pytest.mark.parametrize("make", [
    lambda tmp, data: r"\\server\share\FinTrack backups",
    lambda tmp, data: "//server/share/x",
    lambda tmp, data: "relative\\folder",
    lambda tmp, data: str(data / "backups"),
    lambda tmp, data: str(tmp / "a" / "b" / "c" / "d" / "e"),
    lambda tmp, data: str(tmp / "CON"),
    lambda tmp, data: str(tmp / "bad name."),
    lambda tmp, data: str(tmp / "x?y"),
])
def test_create_refuses_unsafe_folders(unlocked, settings, tmp_path, make):
    raw = make(tmp_path, settings.data_dir)
    r = unlocked.put("/api/backup/auto", json={"dir": raw, "keep": 10, "create": True})
    assert r.status_code == 422, r.text
    assert not (settings.data_dir / "backups").exists()
    assert not (tmp_path / "a").exists()
    assert ok(unlocked.get("/api/backup/auto"))["dir"] is None


def test_create_refuses_a_network_drive_before_touching_it(unlocked, tmp_path, monkeypatch):
    touched = []
    monkeypatch.setattr(automation, "drive_type", lambda root: automation.DRIVE_REMOTE)
    monkeypatch.setattr(automation.os, "mkdir", lambda *a, **k: touched.append(a))
    r = unlocked.put("/api/backup/auto", json={"dir": "Z:\\FinTrack backups", "keep": 10, "create": True})
    assert r.status_code == 422 and "Network" in r.json()["detail"] and touched == []


def test_create_flag_is_strict(unlocked, tmp_path):
    for value in ("yes", 1, None):
        assert unlocked.put("/api/backup/auto", json={"dir": str(tmp_path), "keep": 10, "create": value}).status_code == 422


# ------------------------------------------------------------------ backups: native folder picker


@pytest.fixture
def picker(app, monkeypatch):
    monkeypatch.setattr(folder_picker, "available", lambda: True)
    return app.state.fintrack.folder_picker


def test_pick_folder(unlocked, picker, tmp_path):
    picker.dialog = lambda: f"  {tmp_path}  "
    assert ok(unlocked.post("/api/backup/auto/pick-folder", json={})) == {"dir": str(tmp_path)}
    picker.dialog = lambda: None  # cancelled
    assert ok(unlocked.post("/api/backup/auto/pick-folder", json={})) == {"dir": None}
    assert ok(unlocked.post("/api/backup/auto/pick-folder")) == {"dir": None}  # no body is fine too
    assert unlocked.post("/api/backup/auto/pick-folder", json={"dir": "C:\\"}).status_code == 422


def test_pick_folder_runs_on_its_own_thread(unlocked, picker):
    seen = []
    picker.dialog = lambda: seen.append(threading.current_thread().name) or None
    ok(unlocked.post("/api/backup/auto/pick-folder", json={}))
    assert seen == ["fintrack-folder-picker"]


def test_pick_folder_busy(unlocked, picker):
    picker._busy.acquire()
    try:
        r = unlocked.post("/api/backup/auto/pick-folder", json={})
        assert r.status_code == 409 and r.json()["code"] == "busy"
    finally:
        picker._busy.release()
    picker.dialog = lambda: None
    assert ok(unlocked.post("/api/backup/auto/pick-folder", json={})) == {"dir": None}


def test_pick_folder_off_windows(unlocked, app, monkeypatch):
    monkeypatch.setattr(folder_picker, "available", lambda: False)
    app.state.fintrack.folder_picker.dialog = lambda: pytest.fail("no dialog off Windows")
    r = unlocked.post("/api/backup/auto/pick-folder", json={})
    assert r.status_code == 501 and r.json()["code"] == "unavailable"


def test_pick_folder_never_while_locked(client, unlocked, picker):
    picker.dialog = lambda: pytest.fail("never while locked")
    ok(unlocked.post("/api/auth/lock", json={}))
    assert unlocked.post("/api/backup/auto/pick-folder", json={}).status_code == 401
    assert client.post("/api/backup/auto/pick-folder", json={}, headers={"X-FinTrack": ""}).status_code == 403


def test_pick_folder_locked_while_open(unlocked, picker, vault):
    def dialog():
        vault.lock("idle")  # auto-lock while the window was open
        return "C:\\Somewhere"

    picker.dialog = dialog
    r = unlocked.post("/api/backup/auto/pick-folder", json={})
    assert r.status_code == 401 and "Somewhere" not in r.text


def test_pick_folder_failure_logs_no_path(unlocked, picker, caplog):
    def dialog():
        raise OSError("C:\\Users\\user\\secret\\path")

    picker.dialog = dialog
    r = unlocked.post("/api/backup/auto/pick-folder", json={})
    assert r.status_code == 500 and r.json()["code"] == "failed"
    assert "secret" not in caplog.text and "secret" not in r.text
    assert not picker.busy  # released after a failure


def test_real_dialog_is_blocked_in_tests(unlocked, picker):
    picker.dialog = None  # falls back to native_dialog, which conftest replaces
    r = unlocked.post("/api/backup/auto/pick-folder", json={})
    assert r.status_code == 500


# ------------------------------------------------------------------ security review fixes


def test_turn_on_without_a_folder_is_refused(unlocked, tmp_path):
    # "Turn on" (create) with no folder must never be read as "turn off".
    r = unlocked.put("/api/backup/auto", json={"dir": None, "keep": 10, "create": True})
    assert r.status_code == 422 and r.json()["detail"] == "Pick a folder for the backups."
    folder = tmp_path / "backups"
    folder.mkdir()
    ok(unlocked.put("/api/backup/auto", json={"dir": str(folder), "keep": 10}))
    r = unlocked.put("/api/backup/auto", json={"dir": None, "keep": 5, "create": True})
    assert r.status_code == 422
    after = ok(unlocked.get("/api/backup/auto"))
    assert after["dir"] == str(folder.resolve()) and after["keep"] == 10  # nothing changed
    # Turning off is still a plain PUT without create.
    assert ok(unlocked.put("/api/backup/auto", json={"dir": None, "keep": 10}))["dir"] is None


def _record_walk(monkeypatch, fake_links=None):
    """Record every link_target / lexists call create_dir makes, in order."""
    calls: list[tuple[str, str]] = []
    real_link_target = automation.link_target
    real_lexists = os.path.lexists
    links = {os.path.normcase(k): v for k, v in (fake_links or {}).items()}

    def link_target(path):
        calls.append(("link", str(path)))
        fake = links.get(os.path.normcase(str(path)))
        return fake if fake is not None else real_link_target(path)

    def lexists(path):
        calls.append(("lexists", str(path)))
        return real_lexists(path)

    monkeypatch.setattr(automation, "link_target", link_target)
    monkeypatch.setattr(automation.os.path, "lexists", lexists)
    return calls


def _under(path, parent) -> bool:
    return os.path.normcase(str(path)).startswith(os.path.normcase(str(parent)) + os.sep)


def test_create_dir_never_touches_anything_under_a_network_link(settings, tmp_path, monkeypatch):
    base = tmp_path / "base"
    base.mkdir()
    link = base / "share"  # a directory symlink to \\server\share (faked: never a real share)
    calls = _record_walk(monkeypatch, {str(link): r"\\server\share"})
    with pytest.raises(automation.BackupDirError) as err:
        automation.create_dir(str(link / "FinTrack backups" / "more"), settings.data_dir)
    assert str(err.value) == automation.NETWORK_REFUSED
    # Top-down from the drive root, stopping at the link: nothing beneath it was touched.
    assert not any(_under(p, link) for _, p in calls)
    link_calls = [p for kind, p in calls if kind == "link"]
    assert link_calls[-1] == str(link)
    assert all(_under(b, a) for a, b in zip(link_calls, link_calls[1:]))
    assert not link.exists()


def test_create_dir_checks_each_part_before_looking_below_it(settings, tmp_path, monkeypatch):
    base = tmp_path / "base"
    base.mkdir()
    new = base / "new"
    calls = _record_walk(monkeypatch)
    automation.create_dir(str(new / "deeper"), settings.data_dir)
    assert (new / "deeper").is_dir()
    # The first missing part: link_target, then lexists, and nothing below it was inspected.
    first_missing = calls.index(("lexists", str(new)))
    assert calls[first_missing - 1] == ("link", str(new))
    assert not any(_under(p, new) for _, p in calls)
    # Every existing parent was read (link_target) from the root down before it.
    link_calls = [p for kind, p in calls[:first_missing] if kind == "link"]
    assert link_calls[-2:] == [str(base), str(new)]
    assert all(_under(b, a) for a, b in zip(link_calls, link_calls[1:]))


def test_create_dir_checks_a_local_link_before_going_through_it(settings, tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "alias"  # a link to a local folder (faked)
    order: list[tuple[str, str]] = []
    monkeypatch.setattr(automation, "_check_links", lambda text, hops=0: order.append(("check", text)))
    calls = _record_walk(monkeypatch, {str(link): str(real)})
    with pytest.raises(automation.BackupDirError):
        # The fake link isn't on disk, so nothing below it exists: refused as missing.
        automation.create_dir(str(link / "x"), settings.data_dir)
    assert order == [("check", str(real))]  # its local target was checked, once
    below = [i for i, (_, p) in enumerate(calls) if _under(p, link)]
    assert below and min(below) > calls.index(("link", str(link)))


def test_category_restore_only_takes_an_issued_id(unlocked, db, pets):
    from app.services import categories as cat_service

    c, cid = unlocked, pets["cid"]
    snap = ok(c.get(f"/api/categories/{cid}/snapshot"))
    assert c.delete(f"/api/categories/{cid}").status_code == 204
    seq = cat_service.custom_seq(db)
    for made_up in (f"c_{seq + 1}", "c_999999999", "c_0", "c_01"):
        body = json.loads(json.dumps(snap))
        body["category"]["id"] = made_up
        r = c.post("/api/categories/restore", json=body)
        assert r.status_code == 422 and r.json()["detail"] == "unknown category id", made_up
    db.expire_all()
    assert db.get(TxnCategory, f"c_{seq + 1}") is None
    assert ok(c.post("/api/categories/restore", json=snap), 201)["id"] == cid


def test_category_restore_ignores_rule_ids_above_the_highest(unlocked, db, pets):
    c, cid = unlocked, pets["cid"]
    snap = ok(c.get(f"/api/categories/{cid}/snapshot"))
    assert c.delete(f"/api/categories/{cid}").status_code == 204
    highest = max(r["id"] for r in ok(c.get("/api/rules")))
    chewy, petco = snap["rules"]
    assert chewy["id"] < highest  # a gap left by the delete: reused
    petco["id"] = 10_000  # never issued: SQLite picks the next id instead
    ok(c.post("/api/categories/restore", json=snap), 201)
    ids = {r["text"]: r["id"] for r in ok(c.get("/api/rules"))}
    assert ids["chewy"] == chewy["id"]
    assert ids["petco"] != 10_000 and ids["petco"] == highest + 1


def test_group_restore_ignores_an_id_above_the_highest(unlocked, db):
    c = unlocked
    a = ok(c.post("/api/category-groups", json={"name": "A"}), 201)
    b = ok(c.post("/api/category-groups", json={"name": "B"}), 201)
    assert c.delete(f"/api/category-groups/{b['id']}").status_code == 204
    out = ok(c.post("/api/category-groups/restore", json={"id": 5000, "name": "B", "position": 1}), 201)
    back = next(g for g in out if g["name"] == "B")
    assert back["id"] != 5000 and back["id"] == a["id"] + 1


def test_forced_backup_crash_never_fails_the_password_change(unlocked, vault, monkeypatch, caplog):
    from app.routers import auth as auth_router

    def boom(state):
        raise RuntimeError("C:\\secret\\folder")

    monkeypatch.setattr(auth_router, "forced_backup", boom)
    r = unlocked.post("/api/auth/change-password", json={"current_password": PASSWORD, "new_password": NEW_PASSWORD})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["backup"] == {"ok": False} and body["session_token"]
    assert "set-cookie" in r.headers
    assert "RuntimeError" in caplog.text and "secret" not in caplog.text
    # The new password is the one in force, and the session still works.
    ok(unlocked.get("/api/settings"))
    ok(unlocked.post("/api/auth/lock", json={}))
    assert unlocked.post("/api/auth/unlock", json={"password": NEW_PASSWORD}).status_code == 200


def test_preview_unknown_category_is_422(unlocked):
    body = {"field": "any", "op": "contains", "text": "shell", "action": "category", "category": "NOPE"}
    r = unlocked.post("/api/rules/preview", json=body)
    assert r.status_code == 422 and r.json()["detail"] == "unknown category"
    assert ok(unlocked.post("/api/rules/preview", json={**body, "category": "TRAVEL"}))["matches"] == 0
    # A transfer rule has no category to check.
    ok(unlocked.post("/api/rules/preview", json={**body, "action": "transfer", "category": None}))


def test_lock_closes_an_open_folder_picker(unlocked, picker, vault):
    opened = threading.Event()
    closed = threading.Event()
    seen: dict[str, int] = {}

    def dialog():
        seen["worker"] = threading.get_native_id()
        opened.set()
        assert closed.wait(5), "the lock never closed the window"
        return None  # a closed window returns as cancelled

    def closer(thread_id):
        seen["closed"] = thread_id
        closed.set()

    picker.dialog = dialog
    picker.closer = closer
    result: dict[str, object] = {}
    t = threading.Thread(target=lambda: result.setdefault("dir", picker.pick()))
    t.start()
    assert opened.wait(5)
    vault.lock("idle")
    t.join(5)
    assert not t.is_alive() and result == {"dir": None}
    assert seen["closed"] == seen["worker"]
    # Nothing open: closing does nothing.
    assert picker.close() is False and not picker.busy

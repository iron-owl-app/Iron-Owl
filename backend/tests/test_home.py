"""Home "Today" parts Home v2 keeps: setup, banks, cash, budget and the kept "needs" kinds of
GET /api/dashboard (the rest is in test_dashboard_v2.py), the "Not now" store, "Back up now",
alert_events.data (schema v5) and the pre-migration safety copy."""
from __future__ import annotations

import datetime as dt
import json
import threading
import time

import pytest
import sqlcipher3
from sqlalchemy import select

from app import db as dbmod
from app import migrations, security
from app.models import AlertEvent, PlaidItem, RecurringItem
from app.plaid_client import PlaidClient, get_plaid_client
from app.services import automation, dashboard
from app.services import backup as backup_service
from app.services import plaid_keys as keys_service
from app.services import sync as sync_service
from app.services.alerts import evaluate
from app.utils import utcnow
from app.version import __version__
from tests.conftest import BASE_URL, PASSWORD, SessionClient, add_budget, add_txn, make_account
from tests.test_migrations import REAL_LATEST, columns, raw
from tests.test_plaid import BANK_TOKEN, exchange, plaid_setup  # noqa: F401 - plaid_setup is a fixture
from tests.test_r3_automation import configure, folder, wall  # noqa: F401 - fixtures
from tests.test_r3_migration import build_v3_vault

D = dt.date


def ok(response) -> dict:
    assert response.status_code == 200, response.text
    return response.json()


def get_home(client) -> dict:
    return ok(client.get("/api/dashboard"))


def kinds(body: dict) -> list[str]:
    return [a["kind"] for a in body["needs"]]


def alert(body: dict, kind: str) -> dict:
    found = [a for a in body["needs"] if a["kind"] == kind]
    assert len(found) == 1, body["needs"]
    return found[0]


@pytest.fixture
def manual_run(monkeypatch):
    """A fake monotonic clock for the "Back up now" throttle."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(automation, "monotonic", lambda: clock["t"])
    return clock


# ------------------------------------------------------------------ the whole screen


def test_unconfigured_plaid_and_rejected_vault_keys(unlocked, db, app, settings):
    app.dependency_overrides[get_plaid_client] = lambda: PlaidClient(settings)
    setup = get_home(unlocked)["setup"]
    assert (setup["plaid_configured"], setup["plaid_source"], setup["keys_rejected"]) == (False, "none", False)
    result = {"ok": False, "code": "invalid_keys", "message": "m", "plaid_code": "INVALID_API_KEYS",
              "at": "2026-09-26T12:00:00Z", "env": "sandbox"}
    keys_service.write_keys(db, "a" * 24, "secretsecretsecret", "sandbox", result)
    db.commit()
    assert get_home(unlocked)["setup"]["keys_rejected"] is True
    keys_service.write_last_test(db, {**result, "code": "unreachable", "plaid_code": None})
    db.commit()
    assert get_home(unlocked)["setup"]["keys_rejected"] is False  # a network problem isn't the keys' fault


def test_version_is_only_for_unlocked_sessions(client, app):
    assert app.version == __version__
    status = client.get("/api/auth/status")
    assert status.status_code == 200 and __version__ not in status.text and "version" not in status.text
    assert client.get("/api/dashboard").status_code == 401


def test_get_api_home_is_gone(unlocked):
    # Home v2 (Release 3.9): the screen is GET /api/dashboard; "Not now" stays under /api/home.
    assert unlocked.get("/api/home").status_code == 404
    assert "version" not in get_home(unlocked)


def test_cash_is_only_visible_bank_accounts(unlocked, db, fixed_today):
    item = PlaidItem(plaid_item_id="i1", access_token="access-sandbox-TOPSECRET", institution_name="Neighborhood Bank",
                     kind="bank", status="ok", last_synced_at=dt.datetime(2026, 9, 26, 10, 0, 0))
    db.add(item)
    db.commit()
    chk = make_account(db, "Checking", "bank", 8432.18, plaid_type="depository", plaid_subtype="checking",
                       item_id=item.id, mask="4417", institution_name="Neighborhood Bank")
    jar = make_account(db, "Cash jar", "bank", 50)
    make_account(db, "Old savings", "bank", 999, hidden=True)
    make_account(db, "Visa", "credit", 300)
    make_account(db, "401k", "retirement", 10000)
    body = get_home(unlocked)
    assert body["cash"] == {
        "total": 8482.18,
        "updated_at": "2026-09-26T10:00:00Z",
        "accounts": [
            {"id": chk.id, "name": "Checking", "mask": "4417", "institution_name": "Neighborhood Bank",
             "balance": 8432.18, "source": "plaid", "item_id": item.id, "subtype": "checking"},
            {"id": jar.id, "name": "Cash jar", "mask": None, "institution_name": None, "balance": 50.0,
             "source": "manual", "item_id": None, "subtype": None},
        ],
    }
    assert body["setup"]["visible_accounts"] == 4 and body["setup"]["linked_items"] == 1
    assert body["banks"] == [{"item_id": item.id, "institution_name": "Neighborhood Bank", "kind": "bank",
                              "status": "ok", "error_code": None, "last_synced_at": "2026-09-26T10:00:00Z"}]
    assert "TOPSECRET" not in json.dumps(body)


def test_budget_matches_the_budget_screen(unlocked, db, fixed_today):
    chk = make_account(db, "Checking", "bank", 5000, plaid_type="depository", plaid_subtype="checking")
    add_budget(db, "2026-09", {"FOOD_AND_DRINK": 600, "ENTERTAINMENT": 100.10})
    add_txn(db, chk.id, "2026-09-05", -412.37, "Groceries")
    add_txn(db, chk.id, "2026-09-12", -150.02, "Concert", category="ENTERTAINMENT")
    bm = ok(unlocked.get("/api/budgets"))
    budget = get_home(unlocked)["budget"]
    assert budget == {"month": "2026-09", "is_current": True, "days_left": 4, "has_budget": True,
                      "planned": round(bm["available"] + bm["spent"], 2), "spent": bm["spent"],
                      "left": bm["available"], "next_paycheck": None}
    assert (budget["planned"], budget["spent"], budget["left"]) == (700.1, 562.39, 137.71)


# ------------------------------------------------------------------ bank connections


def test_login_required_then_cleared_by_a_good_sync(unlocked, plaid_setup, fake_plaid, fixed_today):  # noqa: F811
    item = exchange(unlocked, "public-bank", "bank")
    assert "bank_signin" not in kinds(get_home(unlocked))
    fake_plaid.fail(BANK_TOKEN, "accounts", "ITEM_LOGIN_REQUIRED")
    unlocked.post("/api/plaid/sync")
    body = get_home(unlocked)
    assert alert(body, "bank_signin") == {
        "key": f"bank_signin:{item['id']}", "kind": "bank_signin", "tone": "act", "dismissible": False,
        "fingerprint": "",
        "data": {"item_id": item["id"], "item_kind": "bank", "institution_name": "First Bank",
                 "error_code": "ITEM_LOGIN_REQUIRED"},
    }
    assert body["banks"][0]["status"] == "login_required" and body["banks"][0]["error_code"] == "ITEM_LOGIN_REQUIRED"
    text = json.dumps(body)
    assert BANK_TOKEN not in text and "access-" not in text and "fake ITEM_LOGIN_REQUIRED" not in text
    unlocked.post("/api/plaid/sync")  # signed in again: the next sync works
    body = get_home(unlocked)
    assert "bank_signin" not in kinds(body) and body["banks"][0]["status"] == "ok"


def test_error_item_and_pending_item(unlocked, plaid_setup, fake_plaid, fixed_today, monkeypatch):  # noqa: F811
    item = exchange(unlocked, "public-bank", "bank")
    fake_plaid.fail(BANK_TOKEN, "accounts", "INTERNAL_SERVER_ERROR", error_type="API_ERROR")
    monkeypatch.setattr(sync_service, "utcnow", lambda: dt.datetime(2026, 9, 26, 13, 5, 7, 123456))
    unlocked.post("/api/plaid/sync")
    pending = unlocked.post("/api/plaid/exchange", json={"public_token": "public-inv", "kind": "investment"}).json()
    unlocked.put("/api/alerts/settings/low", json={"enabled": False})  # the fake checking is under $2,000
    body = get_home(unlocked)
    assert kinds(body) == ["bank_error", "finish_setup"]
    assert alert(body, "bank_error") == {
        "key": f"bank_error:{item['id']}", "kind": "bank_error", "tone": "warn", "dismissible": True,
        "fingerprint": "INTERNAL_SERVER_ERROR:2026-09-26T13:05:07Z",  # when the sync failed
        "data": {"item_id": item["id"], "item_kind": "bank", "institution_name": "First Bank",
                 "error_code": "INTERNAL_SERVER_ERROR"},
    }
    assert alert(body, "finish_setup") == {
        "key": f"finish_setup:{pending['item']['id']}", "kind": "finish_setup", "tone": "info", "dismissible": True,
        "fingerprint": str(pending["item"]["id"]),
        "data": {"item_id": pending["item"]["id"], "institution_name": "Big Brokerage"},
    }
    assert body["setup"]["linked_items"] == 1 and body["setup"]["pending_items"] == 1
    assert "fake INTERNAL_SERVER_ERROR" not in json.dumps(body)


def test_dismissed_bank_error_comes_back_after_the_next_failure(unlocked, plaid_setup, fake_plaid, monkeypatch):  # noqa: F811
    exchange(unlocked, "public-bank", "bank")
    clock = {"now": dt.datetime(2026, 9, 26, 13, 0, 0)}
    monkeypatch.setattr(sync_service, "utcnow", lambda: clock["now"])

    def fail_sync() -> dict:
        fake_plaid.fail(BANK_TOKEN, "accounts", "INTERNAL_SERVER_ERROR", error_type="API_ERROR")
        unlocked.post("/api/plaid/sync")
        return get_home(unlocked)

    first = fail_sync()
    shown = alert(first, "bank_error")
    assert unlocked.put(f"/api/home/dismissals/{shown['key']}", json={"fingerprint": shown["fingerprint"]}).status_code == 204
    assert get_home(unlocked)["dismissed"][shown["key"]] == shown["fingerprint"]  # hidden ("Not now")

    clock["now"] += dt.timedelta(hours=4)
    again = fail_sync()  # same code, and the last good sync hasn't moved
    assert again["banks"][0]["last_synced_at"] == first["banks"][0]["last_synced_at"]
    back = alert(again, "bank_error")
    assert back["fingerprint"] == "INTERNAL_SERVER_ERROR:2026-09-26T17:00:00Z"
    assert again["dismissed"][back["key"]] != back["fingerprint"]  # so the client shows it again


def test_odd_plaid_error_codes_are_not_passed_on(unlocked, db):
    db.add(PlaidItem(plaid_item_id="i1", access_token="tok", institution_name="Bank", kind="bank",
                     status="error", error_code="bad code <script>"))
    db.commit()
    body = get_home(unlocked)
    assert body["banks"][0]["error_code"] == "UNKNOWN" and alert(body, "bank_error")["fingerprint"] == "UNKNOWN:never"


# ------------------------------------------------------------------ backups


def test_backup_failure_alert_and_back_up_now(unlocked, folder, wall, manual_run, monkeypatch, caplog):  # noqa: F811
    c = unlocked
    r = c.post("/api/backup/auto/run", json={})
    assert r.status_code == 409 and r.json() == {"detail": "Choose a backup folder first."}
    ok(configure(c, folder))

    def denied(_vault):
        raise PermissionError("C:\\secret\\place")

    monkeypatch.setattr(backup_service, "build_backup", denied)
    body = ok(c.post("/api/backup/auto/run", json={}))
    failed = {"enabled": True, "folder_name": "backups", "last_at": None,
              "last_error_at": "2026-09-26T12:00:00Z", "last_error_code": "denied"}
    assert body == {"ok": False, "backup": failed}
    assert "secret" not in caplog.text and "PermissionError" in caplog.text

    # GET /api/home never lists or touches the folder.
    def boom(*_a, **_k):
        raise AssertionError("the backup folder must not be listed")

    monkeypatch.setattr(automation, "list_files", boom)
    monkeypatch.setattr(automation, "still_local", boom)
    home_body = get_home(c)
    assert "backup" not in home_body
    assert alert(home_body, "backup_failed") == {
        "key": "backup_failed", "kind": "backup_failed", "tone": "warn", "dismissible": True,
        "fingerprint": "2026-09-26T12:00:00Z",
        "data": {"code": "denied", "folder_name": "backups", "at": "2026-09-26T12:00:00Z"},
    }
    assert str(folder) not in json.dumps(home_body) and str(folder.parent) not in json.dumps(home_body)

    # At most one start a minute.
    manual_run["t"] += 59
    r = c.post("/api/backup/auto/run", json={})
    assert r.status_code == 429 and r.headers["Retry-After"] == "1" and r.json() == {
        "detail": "Iron Owl just tried. Wait a minute, then try again.", "retry_after": 1}
    manual_run["t"] += 1
    monkeypatch.undo()
    wall.__init__(monkeypatch)
    manual_run.update(t=5000.0)
    monkeypatch.setattr(automation, "monotonic", lambda: manual_run["t"])
    wall.advance(minutes=1)
    body = ok(c.post("/api/backup/auto/run"))  # no body is fine too
    assert body == {"ok": True, "backup": {"enabled": True, "folder_name": "backups", "last_at": "2026-09-26T12:01:00Z",
                                           "last_error_at": None, "last_error_code": None}}
    assert [p.name for p in folder.iterdir()] == ["fintrack-auto-20260926-120100.ftbackup"]
    assert "backup_failed" not in kinds(get_home(c))
    auto = ok(c.get("/api/backup/auto"))
    assert auto["last_error"] is None and auto["last_error_at"] is None

    # A missing folder is recorded as "missing" (re-checked before writing).
    for p in folder.iterdir():
        p.unlink()
    folder.rmdir()
    manual_run["t"] += 60
    body = ok(c.post("/api/backup/auto/run", json={}))
    assert body["ok"] is False and body["backup"]["last_error_code"] == "missing"
    assert c.post("/api/backup/auto/run", json={"x": 1}).status_code == 422
    # Turning backups off clears the failure.
    ok(configure(c, None))
    assert "backup_failed" not in kinds(get_home(c))
    auto = ok(c.get("/api/backup/auto"))
    assert (auto["dir"], auto["last_at"], auto["last_error_at"], auto["last_error_code"]) == (
        None, "2026-09-26T12:01:00Z", None, None)


@pytest.mark.parametrize(("exc", "code"), [
    (FileNotFoundError(), "missing"), (PermissionError(), "denied"), (OSError(), "write"),
    (automation.BackupDirError(automation.NETWORK_REFUSED), "network"),
    (automation.BackupDirError(automation.CANT_WRITE, "denied"), "denied"),
    (automation.BackupDirError("Pick a folder outside Iron Owl's data folder."), "other"), (RuntimeError(), "other"),
])
def test_friendly_codes(exc, code):
    assert automation._friendly_code(exc) == code


def test_back_up_now_waits_for_a_running_backup(unlocked, folder, wall, manual_run, app):  # noqa: F811
    ok(configure(unlocked, folder))
    runner = app.state.fintrack.auto_backup
    result: list = []
    with runner._run:  # another backup (lock, periodic) is running
        t = threading.Thread(target=lambda: result.append(unlocked.post("/api/backup/auto/run", json={}).status_code))
        t.start()
        time.sleep(0.3)
        assert t.is_alive() and result == []
    t.join(10)
    assert result == [200] and len(list(folder.iterdir())) == 1


def test_back_up_now_never_prunes_older_backups(unlocked, folder, wall, manual_run, app):  # noqa: F811
    ok(configure(unlocked, folder, keep=2))
    old = [f"fintrack-auto-2026090{d}-120000.ftbackup" for d in (1, 2, 3)]
    for name in old:
        (folder / name).write_bytes(b"older automatic backup")
    for _ in range(2):
        assert ok(unlocked.post("/api/backup/auto/run", json={}))["ok"] is True
        manual_run["t"] += 60
        wall.advance(minutes=1)
    names = sorted(p.name for p in folder.iterdir())
    assert names == [*old, "fintrack-auto-20260926-120000.ftbackup", "fintrack-auto-20260926-120100.ftbackup"]
    # The next automatic backup trims the folder back to ``keep``.
    wall.advance(minutes=10)
    assert app.state.fintrack.auto_backup.on_lock() is True
    assert sorted(p.name for p in folder.iterdir()) == [
        "fintrack-auto-20260926-120100.ftbackup", "fintrack-auto-20260926-121200.ftbackup"]


def test_folder_name_is_only_the_leaf():
    assert automation.folder_name("D:\\Users\\user\\OneDrive") == "OneDrive"
    assert automation.folder_name("D:\\") == "D:"
    assert automation.folder_name(None) is None


# ------------------------------------------------------------------ purchases that need a category


def test_needs_category_is_this_month_only(unlocked, db, fixed_today):
    chk = make_account(db, "Checking", "bank", 100)
    add_txn(db, chk.id, "2026-08-30", -5, "Last month", category=None)
    first = add_txn(db, chk.id, "2026-09-20", -5, "Mystery 1", category=None)
    add_txn(db, chk.id, "2026-09-02", -5, "Groceries")  # has a budget category
    add_txn(db, chk.id, "2026-09-03", -50, "To savings", category=None, is_transfer=True)
    newest = add_txn(db, chk.id, "2026-09-01", -5, "Mystery 2", category=None)
    body = get_home(unlocked)
    assert alert(body, "needs_category") == {
        "key": "needs_category", "kind": "needs_category", "tone": "info", "dismissible": True,
        "fingerprint": f"t{newest.id}", "data": {"count": 2, "month_start": "2026-09-01"},
    }
    assert newest.id > first.id
    # The same rows as the link it opens.
    summary = ok(unlocked.get("/api/transactions/summary", params={"view": "needs_category", "start": "2026-09-01"}))
    assert summary["counts"]["needs_category"] == 2
    unlocked.patch(f"/api/transactions/{newest.id}", json={"category": "FOOD_AND_DRINK"})
    unlocked.patch(f"/api/transactions/{first.id}", json={"category": "FOOD_AND_DRINK"})
    assert "needs_category" not in kinds(get_home(unlocked))


# ------------------------------------------------------------------ price changes


def test_price_alert_writes_structured_data(unlocked, db, fixed_today):
    chk = make_account(db, "Chase Checking", "bank", 5000, plaid_type="depository", plaid_subtype="checking")
    item = RecurringItem(name="Netflix", merchant_key="netflix", account_id=chk.id, amount_cents=-1549,
                         cadence="monthly", next_date=D(2026, 10, 10), status="active", source="detected")
    db.add(item)
    db.commit()
    txn = add_txn(db, chk.id, "2026-09-12", -17.99, "NETFLIX", merchant="Netflix")
    evaluate(db, D(2026, 9, 26))
    db.commit()
    # Settings D7 update 2: price changes no longer create visible events (a cleared tombstone).
    assert db.scalar(select(AlertEvent).where(AlertEvent.key == "price")).cleared
    # Home v2 (Release 3.9): no price_up need any more, even for an event an earlier version made.
    event = AlertEvent(key="price", severity="warn", title="Netflix went up", body="$15.49 to $17.99",
                       created_at=utcnow(), read=False, dedupe_key=f"price:{item.id}:old{txn.id}", cleared=False,
                       data=json.dumps({"recurring_id": item.id, "old_cents": -1549, "new_cents": -1799,
                                        "cadence": "monthly"}))
    db.add(event)
    db.commit()
    assert "price_up" not in kinds(get_home(unlocked))
    # "Clear all" scrubs the data with the text.
    assert unlocked.delete("/api/alerts/events").status_code == 204
    db.expire_all()
    assert db.get(AlertEvent, event.id).data is None


def test_needs_are_sorted_by_tone_then_kind(unlocked, db, fixed_today, folder, wall):  # noqa: F811
    chk = make_account(db, "Checking", "bank", 100)
    add_txn(db, chk.id, "2026-09-05", -5, "Mystery", category=None)
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
    automation._put(db, automation.LAST_ERROR_CODE_KEY, "bogus")
    db.commit()
    body = get_home(unlocked)
    assert kinds(body) == ["bank_signin", "bank_error", "backup_failed", "finish_setup", "needs_category"]
    assert [a["tone"] for a in body["needs"]] == ["act", "warn", "warn", "info", "info"]
    assert alert(body, "backup_failed")["data"]["code"] == "other"  # unknown stored codes are "other"


# ------------------------------------------------------------------ "Not now"


def test_dismissals_round_trip(unlocked):
    c = unlocked
    assert c.put("/api/home/dismissals/price:40", json={"fingerprint": "40"}).status_code == 204
    assert c.put("/api/home/dismissals/needs_category", json={"fingerprint": "t9812"}).status_code == 204
    assert c.put("/api/home/dismissals/backup_failed", json={"fingerprint": "2026-09-26T12:00:00Z"}).status_code == 204
    assert c.put("/api/home/dismissals/low_balance:11", json={"fingerprint": "2026-10-08"}).status_code == 204
    assert get_home(c)["dismissed"] == {"price:40": "40", "needs_category": "t9812",
                                        "backup_failed": "2026-09-26T12:00:00Z", "low_balance:11": "2026-10-08"}
    # A new fingerprint replaces the old one (and becomes the newest).
    assert c.put("/api/home/dismissals/price:40", json={"fingerprint": "41"}).status_code == 204
    assert list(get_home(c)["dismissed"].items())[-1] == ("price:40", "41")
    assert c.delete("/api/home/dismissals/price:40").status_code == 204
    assert c.delete("/api/home/dismissals/price:40").status_code == 204  # idempotent
    assert "price:40" not in get_home(c)["dismissed"]


@pytest.mark.parametrize("key", [
    "Bad", "price:", "price:a:b", "x" * 33, "price:" + "1" * 41, "a-b", "price:1 2", ":1", "price%0A", "price:1%00",
])
def test_bad_dismissal_keys(unlocked, key):
    r = unlocked.put(f"/api/home/dismissals/{key}", json={"fingerprint": "1"})
    assert r.status_code == 422 and r.json() == {"detail": "invalid alert key"}
    assert unlocked.delete(f"/api/home/dismissals/{key}").status_code == 422
    assert get_home(unlocked)["dismissed"] == {}


@pytest.mark.parametrize("body", [
    {}, {"fingerprint": ""}, {"fingerprint": "a b"}, {"fingerprint": "x" * 65}, {"fingerprint": "<script>"},
    {"fingerprint": "1", "extra": 1}, {"fingerprint": 1}, {"fingerprint": "1\n"},
])
def test_bad_dismissal_bodies(unlocked, body):
    assert unlocked.put("/api/home/dismissals/price:1", json=body).status_code == 422
    assert get_home(unlocked)["dismissed"] == {}


def test_dismissals_are_capped_at_100(unlocked, db):
    for n in range(105):
        assert unlocked.put(f"/api/home/dismissals/price:{n}", json={"fingerprint": str(n)}).status_code == 204
    dismissed = get_home(unlocked)["dismissed"]
    assert list(dismissed) == [f"price:{n}" for n in range(5, 105)]
    # Stored inside the vault; malformed stored entries are ignored.
    automation._put(db, dashboard.DISMISSED_KEY, json.dumps({"ok_key": "1", "Bad": "1", "x": "a b", "y": 3}))
    db.commit()
    assert get_home(unlocked)["dismissed"] == {"ok_key": "1"}
    automation._put(db, dashboard.DISMISSED_KEY, "not json")
    db.commit()
    assert get_home(unlocked)["dismissed"] == {}


def test_home_routes_need_a_session_and_the_csrf_header(unlocked, app):
    anon = SessionClient(app, base_url=BASE_URL)  # no session, no X-FinTrack
    assert anon.get("/api/dashboard").status_code == 401
    no_csrf = SessionClient(app, base_url=BASE_URL, headers={k: v for k, v in unlocked.headers.items()
                                                             if k.lower() != "x-fintrack"})
    assert no_csrf.put("/api/home/dismissals/price:1", json={"fingerprint": "1"}).status_code == 403
    assert no_csrf.delete("/api/home/dismissals/price:1").status_code == 403
    assert no_csrf.post("/api/backup/auto/run", json={}).status_code == 403
    unlocked.post("/api/auth/lock")
    assert unlocked.get("/api/dashboard").status_code == 401
    assert unlocked.put("/api/home/dismissals/price:1", json={"fingerprint": "1"}).status_code == 401
    assert unlocked.delete("/api/home/dismissals/price:1").status_code == 401
    assert unlocked.post("/api/backup/auto/run", json={}).status_code == 401


# ------------------------------------------------------------------ migration v5


def test_v4_vault_gains_alert_data(settings, client, monkeypatch):
    key = build_v3_vault(settings, monkeypatch)
    monkeypatch.setattr(migrations, "LATEST", 4)
    database = dbmod.Database(settings.db_path)
    database.open(bytearray(key))
    database.close()
    monkeypatch.setattr(migrations, "LATEST", REAL_LATEST)
    conn = raw(settings, key)
    created = (utcnow() - dt.timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S.000000")
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert "data" not in columns(conn, "alert_events")
        conn.execute("INSERT INTO recurring_items (id, name, merchant_key, account_id, amount_cents, cadence, "
                      "next_date, status, include_in_forecast, source, created_at) VALUES "
                      "(7, 'Coffee club', 'coffee', 1, -450, 'monthly', '2026-10-01', 'active', 1, 'manual', ?)",
                      (created,))
        conn.execute("""INSERT INTO alert_events ("key", severity, title, body, created_at, read, dedupe_key, cleared)
                        VALUES ('price', 'warn', 'Coffee club went up', '$4.00 to $4.50', ?, 1, 'price:7:1', 0)""",
                     (created,))
        conn.commit()
    finally:
        conn.close()
    assert client.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200
    assert "price_up" not in kinds(get_home(client))  # Home v2: price changes aren't needs any more
    client.post("/api/auth/lock")
    conn = raw(settings, key)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == migrations.LATEST == 8
        assert columns(conn, "alert_events")["data"] == (0, None)  # nullable, no default
        assert columns(conn, "plaid_items")["last_error_at"] == (0, None)
        assert conn.execute("SELECT last_error_at FROM plaid_items").fetchall() == [(None,)]
        assert conn.execute("SELECT data FROM alert_events WHERE dedupe_key = 'price:7:1'").fetchone() == (None,)
    finally:
        conn.close()


def test_failed_v5_migration_rolls_back_to_v4(settings, monkeypatch):
    key = build_v3_vault(settings, monkeypatch)
    monkeypatch.setattr(migrations, "LATEST", 4)
    database = dbmod.Database(settings.db_path)
    database.open(bytearray(key))
    database.close()
    monkeypatch.setattr(migrations, "LATEST", 5)

    def broken(conn):
        conn.execute("ALTER TABLE alert_events ADD COLUMN data TEXT")
        conn.execute("ALTER TABLE nope ADD COLUMN x TEXT")

    monkeypatch.setitem(migrations.MIGRATIONS, 5, broken)
    with pytest.raises(Exception):  # noqa: B017 - sqlcipher3.OperationalError
        database.open(bytearray(key))
    conn = raw(settings, key)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert "data" not in columns(conn, "alert_events")
    finally:
        conn.close()


# ------------------------------------------------------------------ pre-migration safety copy


def build_v4_vault(settings, monkeypatch) -> bytearray:
    key = build_v3_vault(settings, monkeypatch)
    monkeypatch.setattr(migrations, "LATEST", 4)
    database = dbmod.Database(settings.db_path)  # no safety-copy hook: just the test's setup
    database.open(bytearray(key))
    database.close()
    monkeypatch.setattr(migrations, "LATEST", REAL_LATEST)
    return key


def pre_migrate_dirs(settings) -> list[str]:
    return sorted(p.name for p in settings.data_dir.iterdir() if p.name.startswith("pre-migrate-"))


def test_migrating_a_v4_vault_first_copies_it_aside(settings, client, monkeypatch):
    key = build_v4_vault(settings, monkeypatch)
    db_bytes, keyfile_bytes = settings.db_path.read_bytes(), settings.keyfile_path.read_bytes()
    assert pre_migrate_dirs(settings) == []
    assert client.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200
    (name,) = pre_migrate_dirs(settings)
    assert security.PRE_MIGRATE_RE.match(name) and name.startswith("pre-migrate-v4-")
    copy = settings.data_dir / name
    assert sorted(p.name for p in copy.iterdir()) == ["fintrack.db", "keyfile.json"]
    # Byte for byte what was there before the upgrade: still encrypted, still schema v4.
    assert (copy / "fintrack.db").read_bytes() == db_bytes
    assert (copy / "keyfile.json").read_bytes() == keyfile_bytes
    assert backup_service.is_plaintext_sqlite(copy / "fintrack.db") is False
    conn = sqlcipher3.connect(str(copy / "fintrack.db"))
    try:
        conn.execute(dbmod._key_pragma(key))
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
    finally:
        conn.close()
    # The live vault was upgraded.
    client.post("/api/auth/lock")
    live = raw(settings, key)
    try:
        assert live.execute("PRAGMA user_version").fetchone()[0] == migrations.LATEST
    finally:
        live.close()
    # Reopening an up-to-date vault makes no new copy.
    assert client.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200
    assert pre_migrate_dirs(settings) == [name]
    # Startup cleanup, the previous-vaults list and backups leave these folders alone.
    backup_service.cleanup_staging(settings.data_dir)
    assert pre_migrate_dirs(settings) == [name]
    assert backup_service.list_previous(settings.data_dir) == []
    assert ok(client.get("/api/backup/previous")) == []


def test_only_the_three_newest_pre_migrate_copies_are_kept(settings, client, monkeypatch):
    build_v4_vault(settings, monkeypatch)
    for name in ("pre-migrate-v2-20200101T000000Z", "pre-migrate-v3-20210101T000000Z",
                 "pre-migrate-v3-20210101T000000Z-2", "pre-migrate-notes", "pre-restore-20200101-000000"):
        (settings.data_dir / name).mkdir()
        (settings.data_dir / name / "fintrack.db").write_bytes(b"old")
    assert client.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200
    names = pre_migrate_dirs(settings)
    new = [n for n in names if n.startswith("pre-migrate-v4-")]
    assert len(new) == 1
    assert names == sorted([*new, "pre-migrate-notes", "pre-migrate-v3-20210101T000000Z",
                            "pre-migrate-v3-20210101T000000Z-2"])
    assert (settings.data_dir / "pre-restore-20200101-000000").is_dir()  # not ours to prune


def test_a_clock_set_back_never_prunes_the_new_copy(settings, client, monkeypatch):
    build_v4_vault(settings, monkeypatch)
    for year in (2090, 2091, 2092):  # copies stamped by a clock that was set ahead
        (settings.data_dir / f"pre-migrate-v3-{year}0101T000000Z").mkdir()
    assert client.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200
    names = pre_migrate_dirs(settings)
    assert len([n for n in names if n.startswith("pre-migrate-v4-")]) == 1
    assert len(names) == security.PRE_MIGRATE_KEEP
    assert "pre-migrate-v3-20900101T000000Z" not in names  # the oldest of the others went


def test_no_migration_if_the_safety_copy_fails(settings, client, vault, monkeypatch):
    key = build_v4_vault(settings, monkeypatch)
    db_bytes = settings.db_path.read_bytes()
    real_copy = security._copy_synced

    def disk_full(src, dest):
        if src.name == "keyfile.json":
            raise OSError(28, "No space left on device")
        real_copy(src, dest)

    monkeypatch.setattr(security, "_copy_synced", disk_full)
    r = client.post("/api/auth/unlock", json={"password": PASSWORD})
    assert r.status_code == 500 and r.json() == {
        "detail": security.PreMigrationCopyFailed.detail, "code": "pre_migration_copy_failed",
    }
    assert not vault.unlocked and vault.db.open_connection_count == 0
    assert settings.db_path.read_bytes() == db_bytes  # untouched: still v4
    assert pre_migrate_dirs(settings) == []  # the partial copy was removed
    assert not [p for p in settings.data_dir.iterdir() if p.name.startswith(".pre-migrate-tmp-")]
    conn = raw(settings, key)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert "last_error_at" not in columns(conn, "plaid_items")
    finally:
        conn.close()
    # Once copying works again, the next unlock copies and upgrades.
    monkeypatch.setattr(security, "_copy_synced", real_copy)
    assert client.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200
    assert len(pre_migrate_dirs(settings)) == 1


def test_a_fresh_vault_makes_no_pre_migrate_copy(settings, unlocked):
    unlocked.post("/api/auth/lock")
    assert unlocked.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200
    assert pre_migrate_dirs(settings) == []

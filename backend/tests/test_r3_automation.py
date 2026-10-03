"""Release 3: automatic encrypted backups, automatic sync, and the "new money to assign" alert."""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import zipfile

import pytest

from app import db as dbmod
from app.main import _periodic_backup
from app.security import KeyParams
from app.services import automation
from app.services import backup as backup_service
from app.services.alerts import evaluate
from tests.conftest import BASE_URL, CSRF, FIXED_TODAY, PASSWORD, SessionClient, add_txn, make_account
from tests.test_plaid import BANK_TOKEN, acct, txn

T0 = dt.datetime(2026, 9, 26, 12, 0, 0)


def ok(response) -> dict:
    assert response.status_code == 200, response.text
    return response.json()


class Clock:
    """Fake wall clock for the automation module (UTC and local move together)."""

    def __init__(self, monkeypatch) -> None:
        self.now = T0
        monkeypatch.setattr(automation, "now_utc", lambda: self.now)
        monkeypatch.setattr(automation, "now_local", lambda: self.now)

    def advance(self, **kw) -> None:
        self.now += dt.timedelta(**kw)


@pytest.fixture
def wall(monkeypatch) -> Clock:
    return Clock(monkeypatch)


@pytest.fixture
def folder(tmp_path) -> pathlib.Path:
    path = tmp_path / "backups"
    path.mkdir()
    return path


def configure(client, folder, keep=10):
    return client.put("/api/backup/auto", json={"dir": None if folder is None else str(folder), "keep": keep})


def backups(folder) -> list[str]:
    return sorted(p.name for p in folder.iterdir() if automation.FILE_RE.match(p.name))


def unlock(client):
    assert client.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200


# ------------------------------------------------------------------ settings + directory rules


def test_auto_backup_settings(unlocked, folder, settings):
    c = unlocked
    none = {"last_error_at": None, "last_error_code": None}
    assert ok(c.get("/api/backup/auto")) == {
        "dir": None, "keep": 10, "last_at": None, "last_error": None, **none, "files": []}
    body = ok(configure(c, folder, keep=3))
    assert body == {"dir": str(folder.resolve()), "keep": 3, "last_at": None, "last_error": None, **none, "files": []}
    # Only exact matches are listed (newest first).
    for name in ("fintrack-auto-20260101-010101.ftbackup", "fintrack-auto-20260201-010101.ftbackup",
                 "fintrack-auto-20260101-010101.ftbackup.bak", "notes.txt", "fintrack-auto-bad.ftbackup"):
        (folder / name).write_bytes(b"x" * 5)
    files = ok(c.get("/api/backup/auto"))["files"]
    assert files == [
        {"name": "fintrack-auto-20260201-010101.ftbackup", "size_bytes": 5, "created_at": "2026-02-01T01:01:01"},
        {"name": "fintrack-auto-20260101-010101.ftbackup", "size_bytes": 5, "created_at": "2026-01-01T01:01:01"},
    ]
    assert not list(folder.glob(".fintrack-write-test-*"))  # the write probe is cleaned up
    assert ok(configure(c, None, keep=5))["dir"] is None


@pytest.mark.parametrize("path", [
    "\\\\server\\share\\backups", "//server/share/backups", "\\\\?\\UNC\\server\\share", "\\\\?\\C:\\backups",
    "\\\\.\\pipe\\x", "/\\server\\share", "\\/server/share",
])
def test_network_paths_are_refused_without_touching_them(unlocked, monkeypatch, path):
    def boom(*_a, **_k):
        raise AssertionError("the filesystem must not be touched for a network path")

    monkeypatch.setattr(automation, "drive_type", boom)
    monkeypatch.setattr(pathlib.Path, "resolve", boom)
    r = configure(unlocked, path)
    assert r.status_code == 422 and r.json()["detail"] == automation.NETWORK_REFUSED


def test_mapped_network_drive_and_missing_drive_are_refused(unlocked, monkeypatch):
    seen = []
    monkeypatch.setattr(automation, "drive_type", lambda root: seen.append(root) or automation.DRIVE_REMOTE)
    r = configure(unlocked, "z:\\FinTrack")
    assert r.status_code == 422 and r.json()["detail"] == automation.NETWORK_REFUSED and seen == ["Z:\\"]
    monkeypatch.setattr(automation, "drive_type", lambda root: automation.DRIVE_NO_ROOT_DIR)
    r = configure(unlocked, "Q:\\FinTrack")
    assert r.status_code == 422 and r.json()["detail"] == "That drive doesn't exist."


def test_directory_validation(unlocked, tmp_path, settings):
    c = unlocked
    a_file = tmp_path / "file.txt"
    a_file.write_text("x")
    inside = settings.data_dir / "sub"
    inside.mkdir()
    cases = {
        "backups": "Enter a full folder path, like D:\\Iron Owl backups.",
        "C:backups": "Enter a full folder path, like D:\\Iron Owl backups.",
        "   ": "Enter a folder path.",
        str(tmp_path / "missing"): "That folder doesn't exist.",
        str(a_file): "That folder doesn't exist.",
        str(settings.data_dir): "Pick a folder outside Iron Owl's data folder.",
        str(inside): "Pick a folder outside Iron Owl's data folder.",
        str(inside) + "\\..\\sub": "Pick a folder outside Iron Owl's data folder.",
    }
    for path, detail in cases.items():
        r = configure(c, path)
        assert r.status_code == 422 and r.json()["detail"] == detail, path
    assert c.put("/api/backup/auto", json={"dir": "C:\\x\x00y", "keep": 3}).status_code == 422
    for keep in (0, 101):
        assert configure(c, tmp_path, keep=keep).status_code == 422
    assert c.put("/api/backup/auto", json={"dir": str(tmp_path), "keep": 3, "x": 1}).status_code == 422
    assert c.put("/api/backup/auto", json={"keep": 3}).status_code == 422
    assert c.put("/api/backup/auto", json={"dir": "x" * 1001, "keep": 3}).status_code == 422
    assert ok(c.get("/api/backup/auto"))["dir"] is None


def test_unwritable_folder_is_refused(unlocked, folder, monkeypatch):
    real_open = open

    def deny(path, mode="r", *a, **k):
        if ".fintrack-write-test-" in str(path):
            raise PermissionError("denied")
        return real_open(path, mode, *a, **k)

    monkeypatch.setattr("builtins.open", deny)
    r = configure(unlocked, folder)
    assert r.status_code == 422 and r.json()["detail"] == "Iron Owl can't write to that folder."


# ------------------------------------------------------------------ backups on lock


def test_lock_makes_an_encrypted_backup_that_opens_with_the_password(unlocked, folder, wall, tmp_path):
    c = unlocked
    c.post("/api/accounts", json={"name": "Zebra Marker", "category": "bank", "current_balance": 1})
    ok(configure(c, folder))
    assert c.post("/api/auth/lock").status_code == 200
    names = backups(folder)
    assert names == ["fintrack-auto-20260926-120000.ftbackup"]
    with zipfile.ZipFile(folder / names[0]) as zf:
        assert sorted(zf.namelist()) == ["fintrack.db", "keyfile.json", "manifest.json"]
        db_bytes = zf.read("fintrack.db")
        params = KeyParams.from_json(json.loads(zf.read("keyfile.json")))
        manifest = json.loads(zf.read("manifest.json"))
    assert manifest["format"] == 1
    assert not db_bytes.startswith(b"SQLite format 3") and b"Zebra Marker" not in db_bytes
    copy = tmp_path / "copy.db"
    copy.write_bytes(db_bytes)
    assert dbmod.verify_key(copy, params.derive(PASSWORD))
    assert not dbmod.verify_key(copy, params.derive("wrong password here"))

    unlock(c)
    body = ok(c.get("/api/backup/auto"))
    assert body["last_at"] == "2026-09-26T12:00:00Z" and body["last_error"] is None
    assert [f["name"] for f in body["files"]] == names


def test_backups_are_skipped_within_ten_minutes_and_pruned_to_keep(unlocked, folder, wall):
    c = unlocked
    ok(configure(c, folder, keep=2))
    (folder / "notes.txt").write_text("mine")
    (folder / "fintrack-auto-20000101-000000.ftbackup.bak").write_text("mine")
    c.post("/api/auth/lock")
    unlock(c)
    wall.advance(minutes=9, seconds=59)
    c.post("/api/auth/lock")
    assert len(backups(folder)) == 1  # under 10 minutes: skipped
    unlock(c)
    wall.advance(minutes=1)
    c.post("/api/auth/lock")
    unlock(c)
    wall.advance(minutes=10)
    c.post("/api/auth/lock")
    # Three backups made; only the newest two kept; other files untouched.
    assert backups(folder) == ["fintrack-auto-20260926-121059.ftbackup", "fintrack-auto-20260926-122059.ftbackup"]
    assert (folder / "notes.txt").exists() and (folder / "fintrack-auto-20000101-000000.ftbackup.bak").exists()


def test_idle_auto_lock_backs_up_first(unlocked, folder, wall, clock, vault):
    ok(configure(unlocked, folder))
    clock.advance(16 * 60)
    vault.check_idle()
    assert not vault.unlocked
    assert len(backups(folder)) == 1


def test_shutdown_backs_up_first(app, folder, wall):
    with SessionClient(app, base_url=BASE_URL, headers=CSRF) as c:
        assert c.post("/api/auth/setup", json={"password": PASSWORD}).status_code == 200
        ok(configure(c, folder))
        assert backups(folder) == []
    assert len(backups(folder)) == 1
    assert not app.state.fintrack.vault.unlocked


def test_backup_failure_never_blocks_locking(unlocked, folder, wall, monkeypatch, vault):
    ok(configure(unlocked, folder))

    def broken(_vault):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(backup_service, "build_backup", broken)
    assert unlocked.post("/api/auth/lock").status_code == 200
    assert not vault.unlocked and backups(folder) == []
    unlock(unlocked)
    body = ok(unlocked.get("/api/backup/auto"))
    assert body["last_error"] == "The automatic backup failed (RuntimeError)." and body["last_at"] is None
    assert "disk on fire" not in json.dumps(body)

    # The folder disappears: the error says so, and the lock still happens.
    monkeypatch.undo()
    wall.__init__(monkeypatch)
    folder.rmdir()
    unlocked.post("/api/auth/lock")
    assert not vault.unlocked
    unlock(unlocked)
    assert ok(unlocked.get("/api/backup/auto"))["last_error"] == "That folder doesn't exist."


def test_a_success_clears_the_last_error(unlocked, folder, wall, monkeypatch):
    ok(configure(unlocked, folder))
    monkeypatch.setattr(backup_service, "build_backup", lambda _v: (_ for _ in ()).throw(OSError("x")))
    unlocked.post("/api/auth/lock")
    unlock(unlocked)
    assert ok(unlocked.get("/api/backup/auto"))["last_error"] == "The backup couldn't be written to the backup folder."
    monkeypatch.undo()
    wall.__init__(monkeypatch)
    unlocked.post("/api/auth/lock")
    unlock(unlocked)
    body = ok(unlocked.get("/api/backup/auto"))
    assert body["last_error"] is None and body["last_at"] == "2026-09-26T12:00:00Z"


def test_no_folder_no_backup(unlocked, tmp_path, wall, vault):
    unlocked.post("/api/auth/lock")
    assert not vault.unlocked
    assert not list(tmp_path.rglob("fintrack-auto-*"))


def test_periodic_backup_every_24_hours(unlocked, folder, wall, app, monkeypatch):
    state = app.state.fintrack
    ok(configure(unlocked, folder))
    assert state.auto_backup.periodic() is True  # none yet
    wall.advance(hours=23, minutes=59)
    assert state.auto_backup.periodic() is False
    wall.advance(minutes=1)
    assert state.auto_backup.periodic() is True
    assert len(backups(folder)) == 2

    # A failing periodic backup is retried after an hour, not on every 5-minute check.
    calls = []

    def broken(_vault):
        calls.append(1)
        raise RuntimeError("nope")

    monkeypatch.setattr(backup_service, "build_backup", broken)
    wall.advance(hours=24)
    assert state.auto_backup.periodic() is False and len(calls) == 1
    wall.advance(minutes=5)
    state.auto_backup.periodic()
    assert len(calls) == 1
    wall.advance(minutes=56)
    state.auto_backup.periodic()
    assert len(calls) == 2

    # Only while unlocked.
    attempts = []
    monkeypatch.setattr(backup_service, "build_backup", lambda _v: attempts.append(1) or b"")
    state.vault.before_lock = None
    unlocked.post("/api/auth/lock")
    wall.advance(hours=48)
    _periodic_backup(state)
    assert state.auto_backup.periodic() is False
    assert attempts == []


# ------------------------------------------------------------------ auto-sync


@pytest.fixture
def linked(unlocked, fake_plaid, app, fixed_today):
    fake_plaid.add_item("public-bank", BANK_TOKEN, "item_bank", "First Bank")
    fake_plaid.accounts[BANK_TOKEN] = [acct("chk", "Checking", "depository", "checking", 1000)]
    item = unlocked.post("/api/plaid/exchange", json={"public_token": "public-bank", "kind": "bank"}).json()["item"]
    ok(unlocked.post(f"/api/plaid/items/{item['id']}/import", json={"plaid_account_ids": ["chk"]}))
    state = app.state.fintrack
    state.plaid = fake_plaid  # the lifespan loop uses the app's client
    synced = dt.datetime.fromisoformat(ok(unlocked.get("/api/plaid/items"))[0]["last_synced_at"].rstrip("Z"))
    fake_plaid.calls.clear()
    return unlocked, state, fake_plaid, synced


def plaid_syncs(fake) -> int:
    return sum(1 for call in fake.calls if call[0] == "accounts")


def test_auto_sync_settings(unlocked, fake_plaid):
    c = unlocked
    assert ok(c.get("/api/plaid/auto-sync")) == {"hours": 6, "next_at": None}  # default, nothing linked
    for bad in (5, -1, "6", None):
        assert c.put("/api/plaid/auto-sync", json={"hours": bad}).status_code == 422
    assert c.put("/api/plaid/auto-sync", json={"hours": 12, "x": 1}).status_code == 422
    assert ok(c.put("/api/plaid/auto-sync", json={"hours": 0})) == {"hours": 0, "next_at": None}
    assert ok(c.get("/api/plaid/auto-sync"))["hours"] == 0


def test_auto_sync_next_at(linked):
    c, _state, _fake, synced = linked
    body = ok(c.put("/api/plaid/auto-sync", json={"hours": 12}))
    assert body == {"hours": 12, "next_at": (synced + dt.timedelta(hours=12)).isoformat() + "Z"}


def test_auto_sync_runs_when_due_with_a_fake_clock(linked):
    c, state, fake, synced = linked
    ok(c.put("/api/plaid/auto-sync", json={"hours": 6}))
    assert automation.sync_tick(state, now=synced + dt.timedelta(hours=5, minutes=59)) is False
    assert plaid_syncs(fake) == 0
    due = synced + dt.timedelta(hours=6, seconds=1)  # `synced` is truncated to the second
    assert automation.sync_tick(state, now=due) is True
    assert plaid_syncs(fake) == 1
    assert state.auto_sync_last_attempt == due
    # The next check 5 minutes later doesn't sync again.
    assert automation.sync_tick(state, now=due + dt.timedelta(minutes=5)) is False
    assert plaid_syncs(fake) == 1


def test_auto_sync_retries_a_failing_item_once_per_period(linked):
    c, state, fake, synced = linked
    ok(c.put("/api/plaid/auto-sync", json={"hours": 3}))
    fake.fail(BANK_TOKEN, "accounts", "INTERNAL_SERVER_ERROR", error_type="API_ERROR")
    fake.fail(BANK_TOKEN, "accounts", "INTERNAL_SERVER_ERROR", error_type="API_ERROR")
    due = synced + dt.timedelta(hours=3, seconds=1)
    assert automation.sync_tick(state, now=due) is True
    assert ok(c.get("/api/plaid/items"))[0]["status"] == "error"
    events = [e["key"] for e in ok(c.get("/api/alerts/events"))]
    # Settings D7 update 2: bank problems no longer create "conn" events (the item's status
    # above is what Home and Settings > Banks show).
    assert "conn" not in events
    # last_synced_at didn't move, but the next attempt waits a full period.
    for minutes in (5, 60, 179):
        assert automation.sync_tick(state, now=due + dt.timedelta(minutes=minutes)) is False
    assert automation.sync_tick(state, now=due + dt.timedelta(hours=3)) is True
    assert plaid_syncs(fake) == 2


def test_auto_sync_conditions(linked):
    c, state, fake, synced = linked
    later = synced + dt.timedelta(days=2)
    ok(c.put("/api/plaid/auto-sync", json={"hours": 0}))
    assert automation.sync_tick(state, now=later) is False
    ok(c.put("/api/plaid/auto-sync", json={"hours": 24}))

    class Unconfigured:
        configured = False

    state.plaid = Unconfigured()
    assert automation.sync_tick(state, now=later) is False
    state.plaid = fake
    state.vault.before_lock = None
    c.post("/api/auth/lock")
    assert automation.sync_tick(state, now=later) is False
    assert plaid_syncs(fake) == 0
    unlock(c)
    assert automation.sync_tick(state, now=later) is True


def test_auto_sync_skips_pending_items_and_never_synced_items_are_due(unlocked, fake_plaid, app):
    state = app.state.fintrack
    state.plaid = fake_plaid
    fake_plaid.add_item("public-bank", BANK_TOKEN, "item_bank", "First Bank")
    fake_plaid.accounts[BANK_TOKEN] = [acct("chk", "Checking", "depository", "checking", 1000)]
    item = unlocked.post("/api/plaid/exchange", json={"public_token": "public-bank", "kind": "bank"}).json()["item"]
    assert automation.sync_tick(state, now=T0) is False  # only a pending item
    vault = state.vault
    session = vault.db.acquire()
    try:
        from app.models import PlaidItem

        session.get(PlaidItem, item["id"]).status = "ok"  # never synced: due at once
        session.commit()
    finally:
        vault.db.release(session)
    assert ok(unlocked.get("/api/plaid/auto-sync"))["next_at"] is not None
    assert automation.sync_tick(state, now=T0) is True


# ------------------------------------------------------------------ income alert


@pytest.fixture
def budget(unlocked, db, fixed_today):
    chk = make_account(db, "Checking", "bank", 5000)
    hsa = make_account(db, "HSA", "hsa", 100)
    return db, chk, hsa


def income_events(db):
    from sqlalchemy import select

    from app.models import AlertEvent

    return list(db.scalars(select(AlertEvent).where(AlertEvent.key == "income").order_by(AlertEvent.id)))


def test_income_alert_single_deposit(budget):
    db, chk, _hsa = budget
    pay = add_txn(db, chk.id, "2026-09-25", 2500, "PAYROLL 123", merchant="Acme Corp", category="INCOME")
    evaluate(db, FIXED_TODAY, new_transaction_ids=[pay.id])
    db.commit()
    [event] = income_events(db)
    assert (event.severity, event.title) == ("accent", "$2,500 new to assign")
    assert event.body == "Acme Corp on Checking. Ready to assign is now $5,000."
    assert event.dedupe_key == f"income:{pay.id}"
    evaluate(db, FIXED_TODAY, new_transaction_ids=[pay.id])  # deduped
    db.commit()
    assert len(income_events(db)) == 1


def test_income_alert_several_deposits_and_filters(budget):
    db, chk, hsa = budget
    a = add_txn(db, chk.id, "2026-09-25", 1000.5, "Payroll", category="INCOME")
    b = add_txn(db, chk.id, "2026-09-26", 200, "Side gig", category="INCOME")
    skipped = [
        add_txn(db, chk.id, "2026-09-26", 50, "Pending pay", category="INCOME", pending=True),
        add_txn(db, hsa.id, "2026-09-26", 70, "HSA deposit", category="INCOME"),  # not a budget account
        add_txn(db, chk.id, "2026-09-26", 20, "Refund", category="FOOD_AND_DRINK"),  # not income
        add_txn(db, chk.id, "2026-09-26", 90, "From savings", category="INCOME", is_transfer=True),
        add_txn(db, chk.id, "2026-09-01", 3000, "Old paycheck", category="INCOME"),  # backfilled history
        add_txn(db, chk.id, "2026-09-26", -10, "Clawback", category="INCOME"),
    ]
    ids = [b.id, a.id] + [t.id for t in skipped]
    evaluate(db, FIXED_TODAY, new_transaction_ids=ids)
    db.commit()
    [event] = income_events(db)
    assert event.title == "$1,200.50 new to assign"
    assert event.body == "2 deposits. Ready to assign is now $5,000."
    assert event.dedupe_key == f"income:{min(a.id, b.id)},{max(a.id, b.id)}"


def test_income_alert_ready_to_assign_and_setting(budget, unlocked):
    db, chk, _hsa = budget
    ok(unlocked.put("/api/budgets/2026-09", json={"assigned": {"FOOD_AND_DRINK": 1234.56}}))
    ok(unlocked.put("/api/alerts/settings/income", json={"enabled": False}))
    pay = add_txn(db, chk.id, "2026-09-25", 100, "Pay", category="INCOME")
    evaluate(db, FIXED_TODAY, new_transaction_ids=[pay.id])
    db.commit()
    assert income_events(db) == []
    assert unlocked.put("/api/alerts/settings/income", json={"value": 5}).status_code == 422
    ok(unlocked.put("/api/alerts/settings/income", json={"enabled": True}))
    evaluate(db, FIXED_TODAY, new_transaction_ids=[pay.id])
    db.commit()
    # Ready to assign = 5000 - 1234.56.
    assert income_events(db)[0].body == "Pay on Checking. Ready to assign is now $3,765.44."


def test_income_alert_after_sync(unlocked, fake_plaid, fixed_today):
    fake_plaid.add_item("public-bank", BANK_TOKEN, "item_bank", "First Bank")
    fake_plaid.accounts[BANK_TOKEN] = [acct("chk", "Checking", "depository", "checking", 1000)]
    fake_plaid.txn_pages[BANK_TOKEN] = [
        {"cursor_in": None, "added": [txn("p1", "chk", -1500, "Payroll", date="2026-09-25", category="INCOME")],
         "modified": [], "removed": [], "next_cursor": "c1", "has_more": False},
    ]
    item = unlocked.post("/api/plaid/exchange", json={"public_token": "public-bank", "kind": "bank"}).json()["item"]
    ok(unlocked.post(f"/api/plaid/items/{item['id']}/import", json={"plaid_account_ids": ["chk"]}))
    events = [e for e in ok(unlocked.get("/api/alerts/events")) if e["key"] == "income"]
    assert [(e["title"], e["body"], e["severity"]) for e in events] == [
        ("$1,500 new to assign", "Payroll on Checking. Ready to assign is now $1,000.", "accent")]
    settings = ok(unlocked.get("/api/alerts/settings"))
    income = next(s for s in settings if s["key"] == "income")
    assert income["last"]["text"] == "$1,500 new to assign"



def test_auto_sync_skips_items_that_need_the_user_to_sign_in(linked):
    """Plaid can't return data for login_required items; calling it again is pointless."""
    c, state, fake, synced = linked
    ok(c.put("/api/plaid/auto-sync", json={"hours": 3}))
    fake.fail(BANK_TOKEN, "accounts", "ITEM_LOGIN_REQUIRED")
    due = synced + dt.timedelta(hours=3, seconds=1)
    assert automation.sync_tick(state, now=due) is True
    assert ok(c.get("/api/plaid/items"))[0]["status"] == "login_required"
    for later in (dt.timedelta(hours=3), dt.timedelta(days=2)):
        assert automation.sync_tick(state, now=due + later) is False
    assert plaid_syncs(fake) == 1
    assert ok(c.get("/api/plaid/auto-sync"))["next_at"] is None  # nothing to sync automatically

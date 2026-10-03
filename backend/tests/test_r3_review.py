"""Release 3 adversarial review: regressions for the bugs found (one test per finding)."""
from __future__ import annotations

import datetime as dt
import os
import pathlib
import sys
import threading

import pytest
from sqlalchemy import select

from app.models import PlaidItem
from app.services import automation
from app.services import sync as sync_service
from tests.conftest import PASSWORD
from tests.test_backup import download, restore
from tests.test_plaid import BANK_TOKEN, txn
from tests.test_r3_automation import backups, configure, linked, ok, plaid_syncs, wall  # noqa: F401

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows paths and junctions")


# ------------------------------------------------------------------ backup folder: links


@windows_only
def test_a_link_to_a_network_share_is_refused_before_anything_follows_it(unlocked, tmp_path, monkeypatch):
    """Path.resolve() opens links, which would already authenticate to the share's host."""
    parent = tmp_path / "parent"
    parent.mkdir()
    link = parent / "nas"
    link.mkdir()  # stands in for a symlink; link_target says where it "points"

    real = automation.link_target

    def fake_link_target(path: str) -> str | None:
        if os.path.normcase(path) == os.path.normcase(str(link)):
            return "\\\\?\\UNC\\evil-host\\share"
        return real(path)

    def boom(*_a, **_k):
        raise AssertionError("resolve() must not run for a path through a network link")

    monkeypatch.setattr(automation, "link_target", fake_link_target)
    monkeypatch.setattr(pathlib.Path, "resolve", boom)
    for path in (str(link), str(link / "sub"), str(parent) + "\\.\\nas\\x"):
        r = configure(unlocked, path)
        assert r.status_code == 422 and r.json()["detail"] == automation.NETWORK_REFUSED, path


@windows_only
@pytest.mark.parametrize("target", [
    "\\\\evil-host\\share", "//evil-host/share", "\\??\\UNC\\evil-host\\share", "\\\\.\\UNC\\evil-host\\s",
])
def test_every_network_link_target_form_is_refused(unlocked, tmp_path, monkeypatch, target):
    link = tmp_path / "link"
    link.mkdir()
    real = automation.link_target
    monkeypatch.setattr(
        automation, "link_target",
        lambda p: target if os.path.normcase(p) == os.path.normcase(str(link)) else real(p),
    )
    r = configure(unlocked, link)
    assert r.status_code == 422 and r.json()["detail"] == automation.NETWORK_REFUSED


@windows_only
def test_a_local_link_that_leads_through_a_network_link_is_refused(unlocked, tmp_path, monkeypatch):
    """link1 -> C:\\...\\hop\\inner, where hop is itself a link to a share."""
    hop = tmp_path / "hop"
    (hop / "inner").mkdir(parents=True)
    link1 = tmp_path / "link1"
    link1.mkdir()
    real = automation.link_target
    fake = {
        os.path.normcase(str(link1)): "\\\\?\\" + str(hop / "inner"),
        os.path.normcase(str(hop)): "\\\\server\\share",
    }
    monkeypatch.setattr(automation, "link_target", lambda p: fake.get(os.path.normcase(p)) or real(p))
    r = configure(unlocked, link1)
    assert r.status_code == 422 and r.json()["detail"] == automation.NETWORK_REFUSED


@windows_only
def test_link_loops_are_refused(unlocked, tmp_path, monkeypatch):
    loop = tmp_path / "loop"
    loop.mkdir()
    real = automation.link_target
    monkeypatch.setattr(
        automation, "link_target",
        lambda p: str(loop) if os.path.normcase(p) == os.path.normcase(str(loop)) else real(p),
    )
    r = configure(unlocked, loop)
    assert r.status_code == 422 and r.json()["detail"] == "That folder path goes through too many links."


@windows_only
def test_a_real_junction_to_a_local_folder_works(unlocked, tmp_path, settings):
    import _winapi

    real_dir = tmp_path / "real"
    real_dir.mkdir()
    junction = tmp_path / "junction"
    _winapi.CreateJunction(str(real_dir), str(junction))
    body = ok(configure(unlocked, junction))
    assert body["dir"] == str(real_dir.resolve())
    # A junction into data/ is still refused.
    inside = settings.data_dir / "sub"
    inside.mkdir()
    sneaky = tmp_path / "sneaky"
    _winapi.CreateJunction(str(inside), str(sneaky))
    r = configure(unlocked, sneaky)
    assert r.status_code == 422 and r.json()["detail"] == "Pick a folder outside Iron Owl's data folder."


# ------------------------------------------------------------------ backup folder: listing and pruning


def test_look_alike_names_are_never_listed_or_pruned(unlocked, tmp_path, wall):  # noqa: F811
    folder = tmp_path / "bk"
    folder.mkdir()
    ok(configure(unlocked, folder, keep=1))
    odd = [
        "fintrack-auto-20261399-996099.ftbackup",  # matched \d{8}-\d{6} but isn't a date: GET was a 500
        "fintrack-auto-\u0662\u0660\u0662\u0666\u0660\u0661\u0660\u0661-010101.ftbackup",  # Arabic-Indic digits
    ]
    for name in odd:
        (folder / name).write_bytes(b"mine")
    assert ok(unlocked.get("/api/backup/auto"))["files"] == []
    assert unlocked.post("/api/auth/lock").status_code == 200
    assert unlocked.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200
    wall.advance(minutes=11)
    unlocked.post("/api/auth/lock")  # second backup; keep=1 prunes the first
    assert unlocked.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200
    body = ok(unlocked.get("/api/backup/auto"))
    assert [f["name"] for f in body["files"]] == ["fintrack-auto-20260926-121100.ftbackup"]
    assert body["last_error"] is None
    for name in odd:
        assert (folder / name).read_bytes() == b"mine"
    assert not automation.FILE_RE.match("fintrack-auto-20260101-010101.ftbackup\n")


# ------------------------------------------------------------------ backups before every lock


def test_restore_backs_up_the_current_vault_before_locking_it(unlocked, tmp_path, wall):  # noqa: F811
    c = unlocked
    folder = tmp_path / "bk"
    folder.mkdir()
    snapshot = download(c)
    assert snapshot.status_code == 200
    c.post("/api/accounts", json={"name": "Made after the snapshot", "category": "bank", "current_balance": 5})
    ok(configure(c, folder))
    r = restore(c, snapshot.content, current_password=PASSWORD)
    assert r.status_code == 200, r.text
    # The vault that the restore replaced was backed up first (it has the newer account).
    assert backups(folder) == ["fintrack-auto-20260926-120000.ftbackup"]


# ------------------------------------------------------------------ auto-sync vs a user sync


def test_auto_sync_does_not_queue_behind_a_running_sync(linked):  # noqa: F811
    c, state, fake, synced = linked
    ok(c.put("/api/plaid/auto-sync", json={"hours": 3}))
    due = synced + dt.timedelta(hours=3, seconds=1)
    holding, release = threading.Event(), threading.Event()

    def users_sync() -> None:
        with sync_service.sync_lock():
            holding.set()
            release.wait(10)

    worker = threading.Thread(target=users_sync)
    worker.start()
    try:
        assert holding.wait(10)
        assert automation.sync_tick(state, now=due) is False  # returns at once, no Plaid calls
        assert plaid_syncs(fake) == 0 and state.auto_sync_last_attempt is None
    finally:
        release.set()
        worker.join(10)
    assert automation.sync_tick(state, now=due) is True
    assert plaid_syncs(fake) == 1


def test_sync_uses_the_latest_cursor_even_with_a_stale_copy_loaded(linked):  # noqa: F811
    c, state, fake, _synced = linked
    vault = state.vault
    stale = vault.db.acquire()
    try:
        item = stale.scalars(select(PlaidItem)).one()
        first_cursor = item.transactions_cursor
        stale.commit()  # expire_on_commit=False: the loaded copy keeps first_cursor
        fake.txn_pages[BANK_TOKEN] = [{
            "cursor_in": first_cursor, "added": [txn("t-new", "chk", 12.5, "Cafe")],
            "modified": [], "removed": [], "next_cursor": "c-after", "has_more": False,
        }]
        ok(c.post("/api/plaid/sync", json={}))  # another session moves the cursor on
        fake.calls.clear()
        sync_service.sync_item(stale, fake, item.id)
        cursors = [call[2] for call in fake.calls if call[0] == "transactions"]
        assert cursors == ["c-after"]
    finally:
        vault.db.release(stale)


# ------------------------------------------------------------------ splits: CSV and rules


def _split_coffee(db, client):
    from tests.conftest import add_txn, make_account

    chk = make_account(db, "Checking", "bank", 100)
    t = add_txn(db, chk.id, "2026-09-10", -30, name="Cafe Luna", merchant="Cafe Luna")
    r = client.put(f"/api/transactions/{t.id}/splits", json={"splits": [
        {"amount": -20, "category": "FOOD_AND_DRINK"}, {"amount": -10, "category": "MEDICAL", "notes": "aspirin"}]})
    assert r.status_code == 200, r.text
    return t


def _csv_rows(client, **params) -> list[list[str]]:
    import csv
    import io

    r = client.get("/api/transactions/export.csv", params=params)
    assert r.status_code == 200
    return list(csv.reader(io.StringIO(r.text)))[1:]


def test_csv_with_a_category_filter_exports_only_that_categorys_parts(unlocked, db):
    _split_coffee(db, unlocked)
    rows = _csv_rows(unlocked, category="FOOD_AND_DRINK")
    assert [(r[4], r[5], r[8]) for r in rows] == [("Food and drink", "-20.00", "split 1/2")]
    rows = _csv_rows(unlocked, category="MEDICAL")
    assert [(r[4], r[5], r[8]) for r in rows] == [("Medical", "-10.00", "split 2/2: aspirin")]
    assert sorted(r[5] for r in _csv_rows(unlocked)) == ["-10.00", "-20.00"]  # no filter: every part


def test_removing_splits_reapplies_rules(unlocked, db):
    t = _split_coffee(db, unlocked)
    r = unlocked.post("/api/rules", json={
        "field": "merchant", "op": "is", "text": "Cafe Luna", "action": "category", "category": "MEDICAL",
        "enabled": True})
    assert r.status_code == 201, r.text
    # While split, the rule leaves it alone.
    assert unlocked.get(f"/api/transactions?search=Cafe").json()["items"][0]["category"] == "FOOD_AND_DRINK"
    body = unlocked.put(f"/api/transactions/{t.id}/splits", json={"splits": []}).json()
    assert body["splits"] == [] and body["category"] == "MEDICAL" and body["category_source"] == "rule"


def test_listing_a_folder_whose_drive_became_a_network_share_touches_nothing(unlocked, tmp_path, monkeypatch):
    folder = tmp_path / "bk"
    folder.mkdir()
    ok(configure(unlocked, folder))

    def boom(*_a, **_k):
        raise AssertionError("a network folder must not be listed")

    monkeypatch.setattr(automation, "drive_type", lambda _root: automation.DRIVE_REMOTE)
    monkeypatch.setattr(pathlib.Path, "iterdir", boom)
    body = ok(unlocked.get("/api/backup/auto"))
    assert body["dir"] == str(folder.resolve()) and body["files"] == []

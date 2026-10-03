"""Security-review regressions for the updater: finishing an update can never be undone by
a later failed open (M1), recovering the password while an update is pending (M2), good
files are never blacklisted for problems with the user's computer and nothing replaces the offer an
install uses (M3), plus the smaller follow-ups (L1, L4, failed-update pruning, the test-only
Downloads folder)."""
from __future__ import annotations

import errno
import json
import os
import sqlite3
import threading
from pathlib import Path

import pytest

from app import migrations, update_keys
from app import security as security_mod
from app.db import DatabaseLocked
from app.services import backup as backup_service
from app.updates import downloads
from app.updates import service as service_mod
from app.updates.service import Busy, data_upgrade_failed
from app.updates.store import UpdateStore
from tests.conftest import BASE_URL, CSRF, PASSWORD, SessionClient
from tests.update_helpers import (
    Signer,
    aged,
    build_app,
    make_install_root,
    read_root_state,
    write_package,
    write_root_state,
)

NEW_PASSWORD = "a brand new password 123"
PENDING_ID = "0123456789abcdef"


@pytest.fixture
def signer(monkeypatch) -> Signer:
    s = Signer()
    monkeypatch.setattr(update_keys, "TRUSTED_KEYS", s.trusted)
    return s


def start(tmp_path, monkeypatch, fake_plaid, root, *, setup=True):
    app, exits = build_app(tmp_path, monkeypatch, fake_plaid, root)
    c = SessionClient(app, base_url=BASE_URL, headers=CSRF)
    c.__enter__()
    c.exits, c.root, c.data = exits, root, tmp_path / "data"
    c.updater = app.state.fintrack.updater
    c.vault = app.state.fintrack.vault
    if setup:
        r = c.post("/api/auth/setup", json={"password": PASSWORD})
        assert r.status_code == 200
        c.groups = r.json()["recovery"]["groups"]
    return c


@pytest.fixture
def client(tmp_path, monkeypatch, fake_plaid):
    c = start(tmp_path, monkeypatch, fake_plaid, make_install_root(tmp_path, previous="1.3.0"))
    yield c
    c.__exit__(None, None, None)


def fresh_process(c) -> None:
    """As after the launcher's restart onto the new version: this process hasn't opened the
    data yet (the test's setup did, which rightly blocks any rollback in this process)."""
    c.updater._opened = False
    c.vault.opened_once = False


def pend(c, **extra) -> None:
    """Locked, with a just-installed 1.4.0 (from 1.3.0) pending, in a fresh process."""
    c.post("/api/auth/lock")
    state = read_root_state(c.root)
    state["pending"] = {"version": "1.4.0", "from": "1.3.0", "at": "2026-09-28T12:00:00Z",
                        "id": PENDING_ID, "snapshot": "", "gate_passed": True}
    state.update(extra)
    write_root_state(c.root, state)
    fresh_process(c)


def break_migrations(monkeypatch, exc: BaseException, *, upgrading: bool = True) -> None:
    """``migrate`` fails with ``exc``. With ``upgrading`` it first starts a schema upgrade
    (as for data one version behind: the ``before_upgrade`` hook, i.e. the vault's
    pre-migration copy, runs); without it the data is already at the latest schema."""
    def fail(engine, *, fresh=False, before_upgrade=None):
        if upgrading and before_upgrade is not None:
            before_upgrade(migrations.LATEST - 1)
        raise exc

    monkeypatch.setattr(migrations, "migrate", fail)


def unlock(c):
    return c.post("/api/auth/unlock", json={"password": PASSWORD})


# ------------------------------------------------------------------ M1: finishing comes first


def test_probe_a_pending_is_cleared_even_if_history_fails_then_no_rollback(client, monkeypatch):
    c = client
    pend(c)

    def boom(**k):
        raise OSError("sharing violation on data/updates/state.json")

    monkeypatch.setattr(c.updater.store, "add_history", boom)
    assert unlock(c).status_code == 200
    assert read_root_state(c.root)["pending"] is None  # cleared first, whatever failed after
    c.post("/api/auth/lock")
    break_migrations(monkeypatch, sqlite3.OperationalError("database is locked"))
    r = unlock(c)
    assert r.status_code == 500 and r.json().get("code") != "FT-UPD-05"
    assert read_root_state(c.root).get("rollback") is None and c.exits.codes == []


def test_pending_write_failure_at_finish_still_blocks_a_rollback(client, monkeypatch):
    c = client
    pend(c)
    real = service_mod.write_install_state

    def locked_file(root, state):
        raise PermissionError("in use")

    monkeypatch.setattr(service_mod, "write_install_state", locked_file)
    assert unlock(c).status_code == 200  # unlocking never fails because of the updater
    assert read_root_state(c.root)["pending"]["version"] == "1.4.0"
    monkeypatch.setattr(service_mod, "write_install_state", real)
    c.post("/api/auth/lock")
    # even a genuine upgrade error: this process already opened the data, no going back
    break_migrations(monkeypatch, sqlite3.OperationalError("no such column"))
    assert unlock(c).status_code == 500
    assert read_root_state(c.root).get("rollback") is None and c.exits.codes == []


@pytest.mark.parametrize("exc", [
    sqlite3.OperationalError("database is locked"),
    sqlite3.OperationalError("disk I/O error"),
    OSError(errno.EACCES, "in use"),
    DatabaseLocked(),
])
def test_passing_open_errors_while_pending_are_not_a_rollback(client, monkeypatch, exc):
    c = client
    pend(c)
    break_migrations(monkeypatch, exc)
    r = unlock(c)
    assert r.status_code != 409
    assert read_root_state(c.root).get("rollback") is None and c.exits.codes == []
    # the pending update is still there: a real upgrade error later still goes back
    break_migrations(monkeypatch, sqlite3.OperationalError("no such column"))
    r = unlock(c)
    assert r.status_code == 409 and r.json()["code"] == "FT-UPD-05"


def test_classifying_open_errors():
    assert data_upgrade_failed(sqlite3.OperationalError("no such column: x"))
    assert data_upgrade_failed(sqlite3.IntegrityError("UNIQUE constraint failed"))
    assert data_upgrade_failed(KeyError("migration bug"))
    assert not data_upgrade_failed(sqlite3.OperationalError("database is locked"))
    assert not data_upgrade_failed(sqlite3.OperationalError("database or disk is full"))
    assert not data_upgrade_failed(PermissionError("in use"))
    assert not data_upgrade_failed(MemoryError())
    assert not data_upgrade_failed(DatabaseLocked())
    assert not data_upgrade_failed(sqlite3.OperationalError("locking protocol"))
    assert not data_upgrade_failed(sqlite3.OperationalError("database schema has changed"))


def _raised_from(exc: BaseException, cause: BaseException, *, explicit: bool) -> BaseException:
    try:
        try:
            raise cause
        except BaseException as inner:
            if explicit:
                raise exc from inner
            raise exc
    except BaseException as outer:
        return outer


@pytest.mark.parametrize("explicit", [True, False])
@pytest.mark.parametrize("cause", [PermissionError("in use"), OSError(errno.EIO, "disk"), MemoryError()])
def test_classifying_walks_the_cause_chain(cause, explicit):
    # e.g. a migration's own error raised from (or while handling) a file/memory problem
    exc = _raised_from(RuntimeError("migration step failed"), cause, explicit=explicit)
    assert (exc.__cause__ if explicit else exc.__context__) is cause
    assert not data_upgrade_failed(exc)
    # and two levels down
    outer = _raised_from(KeyError("wrapper"), exc, explicit=True)
    assert not data_upgrade_failed(outer)
    # an unrelated chain is still an upgrade error
    assert data_upgrade_failed(_raised_from(RuntimeError("x"), ValueError("y"), explicit=explicit))


def test_classifying_a_wrapped_driver_error_by_message():
    from sqlalchemy.exc import OperationalError as SAOperationalError

    wrapped = SAOperationalError("PRAGMA x", None, sqlite3.OperationalError("locking protocol"))
    assert not data_upgrade_failed(wrapped)
    exc = _raised_from(RuntimeError("step"), sqlite3.OperationalError("database schema has changed"),
                       explicit=True)
    assert not data_upgrade_failed(exc)


# ------------------------------------------------------------------ only a started migration


def test_open_error_with_data_already_upgraded_is_not_a_rollback(client, monkeypatch):
    """Pending, but the data is already at the latest schema (no upgrade starts in this open):
    even a non-passing error is raised as it is, never answered by going back."""
    c = client
    pend(c)
    break_migrations(monkeypatch, sqlite3.OperationalError("no such column"), upgrading=False)
    r = unlock(c)
    assert r.status_code == 500 and r.json().get("code") != "FT-UPD-05"
    assert c.vault.migration_started is False
    assert read_root_state(c.root).get("rollback") is None and c.exits.codes == []
    assert read_root_state(c.root)["pending"]["version"] == "1.4.0"  # still pending


def test_a_migration_started_by_an_earlier_open_does_not_count(client, monkeypatch):
    c = client
    pend(c)
    # an open that started the upgrade but failed for a passing reason: no rollback
    break_migrations(monkeypatch, sqlite3.OperationalError("database is locked"))
    assert unlock(c).status_code != 409
    assert c.vault.migration_started is True
    # the next open finds nothing to upgrade and fails: the earlier start doesn't carry over
    break_migrations(monkeypatch, sqlite3.OperationalError("no such column"), upgrading=False)
    r = unlock(c)
    assert r.status_code == 500 and r.json().get("code") != "FT-UPD-05"
    assert c.vault.migration_started is False
    assert read_root_state(c.root).get("rollback") is None and c.exits.codes == []


def test_a_real_migration_failure_while_pending_rolls_back(client, monkeypatch):
    """No stand-in for ``migrate``: the data is one schema version behind and the real
    upgrade step fails. The vault's pre-migration copy runs (the migration started), so
    the failure goes back to 1.3.0."""
    c = client
    with migrations._raw(c.vault.db._engine) as conn:
        migrations._set_version(conn, migrations.LATEST - 1)
    pend(c)

    def broken_step(conn):
        raise sqlite3.OperationalError("no such column: x")

    monkeypatch.setitem(migrations.MIGRATIONS, migrations.LATEST, broken_step)
    r = unlock(c)
    assert r.status_code == 409
    assert r.json() == {"detail": "update_rolled_back", "code": "FT-UPD-05", "to_version": "1.3.0"}
    assert c.vault.migration_started is True
    assert any(p.name.startswith(f"pre-migrate-v{migrations.LATEST - 1}-") for p in c.data.iterdir())
    assert read_root_state(c.root)["rollback"] == {"to": "1.3.0", "code": "FT-UPD-05"}
    assert c.exits.codes == [75] and not c.vault.unlocked


# ------------------------------------------------------------------ L1: only for this version


def test_pending_is_ignored_when_state_names_another_current(client, monkeypatch):
    c = client
    pend(c, current="1.3.0")  # the launcher says another version is current
    assert unlock(c).status_code == 200
    state = read_root_state(c.root)
    assert state["pending"]["version"] == "1.4.0" and state["current"] == "1.3.0"  # not finished
    c.post("/api/auth/lock")
    fresh_process(c)
    break_migrations(monkeypatch, sqlite3.OperationalError("no such column"))
    assert unlock(c).status_code == 500
    assert read_root_state(c.root).get("rollback") is None and c.exits.codes == []


# ------------------------------------------------------------------ M2: recover while pending


def recover(c, new_password: str = NEW_PASSWORD):
    return c.post("/api/auth/recover", json={"code": "".join(c.groups), "new_password": new_password})


def test_probe_b_recover_while_pending_and_the_upgrade_fails_rolls_back_without_rekey(client, monkeypatch):
    c = client
    pend(c)
    keyfile = c.vault.keyfile
    before = keyfile.read_bytes()
    real = migrations.migrate
    break_migrations(monkeypatch, sqlite3.OperationalError("no such column"))
    r = recover(c)
    assert r.status_code == 409
    assert r.json() == {"detail": "update_rolled_back", "code": "FT-UPD-05", "to_version": "1.3.0"}
    assert read_root_state(c.root)["rollback"] == {"to": "1.3.0", "code": "FT-UPD-05"}
    assert c.exits.codes == [75] and not c.vault.unlocked
    # nothing was re-encrypted: the pre-update copy still matches this recovery sheet
    assert keyfile.read_bytes() == before and not c.vault.keyfile_new.exists()
    monkeypatch.setattr(migrations, "migrate", real)
    assert unlock(c).status_code == 200  # the old password still opens it


def test_recover_while_pending_schema_too_new_changes_nothing(client, monkeypatch):
    c = client
    pend(c)
    before = c.vault.keyfile.read_bytes()
    break_migrations(monkeypatch, migrations.SchemaTooNew(99))
    r = recover(c)
    assert r.status_code == 409 and r.json()["code"] == "schema_too_new"
    assert c.vault.keyfile.read_bytes() == before
    assert read_root_state(c.root).get("rollback") is None and c.exits.codes == []


def test_recover_while_pending_opens_then_rekeys_and_finishes(client):
    c = client
    pend(c)
    r = recover(c)
    assert r.status_code == 200
    assert read_root_state(c.root)["pending"] is None
    c.post("/api/auth/lock")
    assert unlock(c).status_code == 401
    assert c.post("/api/auth/unlock", json={"password": NEW_PASSWORD}).status_code == 200


def test_recover_rekey_failure_leaves_the_vault_locked(client, monkeypatch):
    c = client
    c.post("/api/auth/lock")
    before = c.vault.keyfile.read_bytes()

    def cannot_rekey(path, old_key, new_key):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(security_mod.dbmod, "rekey", cannot_rekey)
    try:
        r = recover(c)
        assert r.status_code == 500
    except sqlite3.OperationalError:
        pass  # the test client may re-raise the server's error instead
    # opened first to check the data, then locked again: never left open without a session
    assert not c.vault.unlocked
    assert c.vault.keyfile.read_bytes() == before and not c.vault.keyfile_new.exists()
    assert unlock(c).status_code == 200


# ------------------------------------------------------------------ M3: offers and blacklisting


@pytest.fixture
def uc(tmp_path, monkeypatch, fake_plaid, signer):
    dl = tmp_path / "Downloads"
    dl.mkdir()
    monkeypatch.setattr(downloads, "downloads_folder", lambda: dl)
    c = start(tmp_path, monkeypatch, fake_plaid, make_install_root(tmp_path, previous="1.3.0"))
    c.downloads = dl
    yield c
    c.__exit__(None, None, None)


def offer_update(c, signer, version: str = "1.6.0") -> dict:
    aged(write_package(c.downloads / f"FinTrack-{version}.ftupdate", signer, version=version))
    offer = c.post("/api/update/scan", json={}).json()["offer"]
    assert offer is not None and offer["version"] == version
    return offer


def slow_backup(monkeypatch):
    gate, entered = threading.Event(), threading.Event()
    real = backup_service.build_backup

    def slow(vault):
        entered.set()
        assert gate.wait(30)
        return real(vault)

    monkeypatch.setattr(backup_service, "build_backup", slow)
    return gate, entered


def test_probe_c_a_picked_file_during_an_install_changes_nothing(uc, signer, monkeypatch, tmp_path):
    c = uc
    offer = offer_update(c, signer)
    gate, entered = slow_backup(monkeypatch)
    picked = tmp_path / "picked.ftupdate"
    write_package(picked, signer, version="1.5.0")
    try:
        assert c.post("/api/update/install", json={"id": offer["id"]}).status_code == 202
        assert entered.wait(30)
        # through the updater: refused while installing
        inc = downloads.new_incoming_dir(c.updater.store)
        (inc / "package.ftupdate").write_bytes(picked.read_bytes())
        with pytest.raises(Busy):
            c.updater.check_file(inc / "package.ftupdate", "x.ftupdate")
        # even straight at the store level (a check that passed the busy test earlier)
        inc2 = downloads.new_incoming_dir(c.updater.store)
        (inc2 / "package.ftupdate").write_bytes(picked.read_bytes())
        downloads.accept_picked_file(c.updater.store, inc2 / "package.ftupdate", file_name="x.ftupdate",
                                     current_version="1.4.0", env=c.updater.environment())
        assert c.updater.store.offer()["id"] == offer["id"]  # the install's offer stays
        assert c.updater.scan_tick(True) is False  # no scan while installing
    finally:
        gate.set()
    assert c.updater._job.wait(30)
    assert c.updater._job.snapshot()["state"] == "restarting" and c.exits.codes == [75]
    assert offer["id"] not in c.updater.store.failed_ids()
    assert read_root_state(c.root)["pending"]["id"] == offer["id"]


def test_offer_gone_during_install_is_not_recorded(uc, signer, monkeypatch):
    c = uc
    offer = offer_update(c, signer)
    gate, entered = slow_backup(monkeypatch)
    try:
        assert c.post("/api/update/install", json={"id": offer["id"]}).status_code == 202
        assert entered.wait(30)
        c.updater.store.clear_offer(offer["id"])
    finally:
        gate.set()
    assert c.updater._job.wait(30)
    snap = c.updater._job.snapshot()
    assert snap["state"] == "failed" and snap["code"] == "FT-UPD-02"
    assert offer["id"] not in c.updater.store.failed_ids()
    assert c.updater.store.last_result() is None and c.exits.codes == []


def test_file_unreadable_before_install_is_not_blacklisted(uc, signer, monkeypatch):
    c = uc
    offer = offer_update(c, signer)

    def in_use(*a, **k):
        raise PermissionError("the file is in use")

    monkeypatch.setattr(service_mod, "verify_package", in_use)
    r = c.post("/api/update/install", json={"id": offer["id"]})
    assert r.status_code == 202 and r.json()["state"] == "failed" and r.json()["code"] == "FT-UPD-02"
    assert offer["id"] not in c.updater.store.failed_ids()
    assert c.updater.store.offer()["id"] == offer["id"]  # the user can try again


def test_launcher_ft_upd_01_does_not_blacklist(tmp_path, monkeypatch, fake_plaid, signer):
    root = make_install_root(tmp_path)
    result = {"outcome": "failed", "code": "FT-UPD-01", "from_version": "1.4.0", "to_version": "1.5.0",
              "at": "2026-09-28T12:00:00Z", "backup_kept": False, "id": PENDING_ID}
    (tmp_path / "data" / "updates").mkdir(parents=True)
    (tmp_path / "data" / "updates" / "update_result.json").write_text(json.dumps(result))
    c = start(tmp_path, monkeypatch, fake_plaid, root)
    try:
        assert PENDING_ID not in c.updater.store.failed_ids()
        assert c.get("/api/update/status").json()["last_result"]["code"] == "FT-UPD-01"
    finally:
        c.__exit__(None, None, None)


# ------------------------------------------------------------------ L4: session after the upload


def test_picked_file_needs_the_session_after_the_upload(uc, signer, monkeypatch, tmp_path):
    c = uc
    data = write_package(tmp_path / "p.ftupdate", signer, version="1.5.0").read_bytes()
    real = backup_service.receive_file

    async def then_locked(*a, **k):
        received = await real(*a, **k)
        c.vault.lock("manual")  # locked (or taken over) while the file was uploading
        return received

    monkeypatch.setattr(backup_service, "receive_file", then_locked)
    r = c.post("/api/update/file", files={"file": ("FinTrack-1.5.0.ftupdate", data, "application/octet-stream")})
    assert r.status_code == 401
    assert c.updater.store.offer() is None
    assert not [p for p in c.updater.store.dir.iterdir() if p.name.startswith(".incoming-")]


# ------------------------------------------------------------------ failed-update folders


def failed_folder(data: Path, name: str) -> None:
    (data / name).mkdir(parents=True)
    (data / name / "fintrack.db").write_bytes(b"x")


FAILED = ["failed-update-1.2.0-20260101T000000Z-aaaa", "failed-update-1.3.0-20260601T000000Z-bbbb",
          "failed-update-1.3.0-20260901T000000Z-cccc"]


def failed_left(data: Path) -> list[str]:
    return sorted(p.name for p in data.iterdir() if p.name.startswith("failed-update-"))


def test_finishing_keeps_the_newest_two_failed_update_folders(client):
    c = client
    for name in (*FAILED, "failed-update-1.2.0-x"):
        failed_folder(c.data, name)
    pend(c)
    assert unlock(c).status_code == 200
    assert failed_left(c.data) == sorted([*FAILED[1:], "failed-update-1.2.0-x"])


def test_startup_after_a_rollback_keeps_the_newest_two(tmp_path, monkeypatch, fake_plaid):
    root = make_install_root(tmp_path)
    data = tmp_path / "data"
    for name in FAILED:
        failed_folder(data, name)
    result = {"outcome": "rolled_back", "code": "FT-UPD-05", "from_version": "1.5.0", "to_version": "1.4.0",
              "at": "2026-09-28T12:00:00Z", "backup_kept": True, "id": PENDING_ID}
    (data / "updates").mkdir(parents=True)
    (data / "updates" / "update_result.json").write_text(json.dumps(result))
    c = start(tmp_path, monkeypatch, fake_plaid, root)
    try:
        assert failed_left(data) == FAILED[1:]
        assert PENDING_ID in c.updater.store.failed_ids()  # FT-UPD-05 condemns the update
    finally:
        c.__exit__(None, None, None)


# ------------------------------------------------------------------ the test-only Downloads folder


def test_downloads_folder_override_is_env_only(monkeypatch, tmp_path):
    monkeypatch.delenv(downloads.DOWNLOADS_OVERRIDE_ENV, raising=False)
    assert downloads.downloads_folder() != tmp_path
    monkeypatch.setenv(downloads.DOWNLOADS_OVERRIDE_ENV, str(tmp_path))
    assert downloads.downloads_folder() == tmp_path
    monkeypatch.setenv(downloads.DOWNLOADS_OVERRIDE_ENV, "  ")
    assert downloads.downloads_folder() != tmp_path


def test_banner_from_the_test_downloads_folder(tmp_path, monkeypatch, fake_plaid, signer):
    folder = tmp_path / "drill-downloads"
    folder.mkdir()
    monkeypatch.setenv(downloads.DOWNLOADS_OVERRIDE_ENV, str(folder))
    aged(write_package(folder / "FinTrack-1.5.0.ftupdate", signer, version="1.5.0"))
    c = start(tmp_path, monkeypatch, fake_plaid, make_install_root(tmp_path))
    try:
        status = c.post("/api/update/scan", json={}).json()
        assert status["offer"]["version"] == "1.5.0" and status["offer"]["source"] == "downloads"
        assert status["banner"]["show"] is True
    finally:
        c.__exit__(None, None, None)


# ------------------------------------------------------------------ the store's atomic write


def test_store_write_retries_a_sharing_violation(tmp_path, monkeypatch):
    store = UpdateStore(tmp_path)
    real = os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError(errno.EACCES, "sharing violation")
        return real(src, dst)

    monkeypatch.setattr(security_mod.os, "replace", flaky)
    monkeypatch.setattr(security_mod.time, "sleep", lambda s: None)
    store.set_last_result({"outcome": "installed"})
    assert store.last_result() == {"outcome": "installed"} and calls["n"] == 3
    assert not list(store.dir.glob("*.tmp"))

"""Installing an update: backup, extraction, the state.json handshake, failures, finishing
after the first unlock, rollback on a failed data upgrade, and the optional manual rollback."""
from __future__ import annotations

import errno
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from app import migrations, update_keys
from app.services import backup as backup_service
from app.updates import downloads
from app.updates import install as install_mod
from app.updates import package as pkg
from tests.conftest import BASE_URL, CSRF, PASSWORD, SessionClient
from tests.update_helpers import (
    INSTALLED_AT,
    Signer,
    aged,
    build_app,
    make_install_root,
    read_root_state,
    write_package,
    write_root_state,
)

ID_RE = r"^[0-9a-f]{16}$"


@pytest.fixture
def signer(monkeypatch) -> Signer:
    s = Signer()
    monkeypatch.setattr(update_keys, "TRUSTED_KEYS", s.trusted)
    return s


@pytest.fixture
def downloads_dir(tmp_path, monkeypatch) -> Path:
    folder = tmp_path / "Downloads"
    folder.mkdir()
    monkeypatch.setattr(downloads, "downloads_folder", lambda: folder)
    return folder


def start(tmp_path, monkeypatch, fake_plaid, root, *, setup=True):
    app, exits = build_app(tmp_path, monkeypatch, fake_plaid, root)
    c = SessionClient(app, base_url=BASE_URL, headers=CSRF)
    c.__enter__()
    c.exits, c.root, c.data = exits, root, tmp_path / "data"
    c.updater = app.state.fintrack.updater
    if setup:
        assert c.post("/api/auth/setup", json={"password": PASSWORD}).status_code == 200
    return c


@pytest.fixture
def uc(tmp_path, monkeypatch, fake_plaid, downloads_dir, signer):
    c = start(tmp_path, monkeypatch, fake_plaid, make_install_root(tmp_path, previous="1.3.0"))
    c.downloads = downloads_dir
    yield c
    c.__exit__(None, None, None)


def offer_update(c, signer, version: str = "1.5.0", **kw) -> dict:
    aged(write_package(c.downloads / f"FinTrack-{version}.ftupdate", signer, version=version, **kw))
    offer = c.post("/api/update/scan", json={}).json()["offer"]
    assert offer is not None and offer["version"] == version
    return offer


def install(c, offer_id: str, *, wait: bool = True):
    r = c.post("/api/update/install", json={"id": offer_id})
    if wait and r.status_code == 202:
        assert c.updater._job.wait(30)
    return r


def versions(root: Path) -> list[str]:
    return sorted(p.name for p in (root / "versions").iterdir())


# ------------------------------------------------------------------ the happy path


def test_install_writes_version_backup_and_pending_then_restarts(uc, signer, tmp_path):
    offer = offer_update(uc, signer)
    before = read_root_state(uc.root)
    r = install(uc, offer["id"], wait=False)
    assert r.status_code == 202
    body = r.json()
    assert set(body) == {"id", "to_version", "step", "state", "code", "at"}
    assert body["id"] == offer["id"] and body["to_version"] == "1.5.0" and body["code"] is None
    assert body["step"] in ("backup", "install", "restart")
    assert uc.updater._job.wait(30)
    progress = uc.get("/api/update/progress")
    assert progress.status_code == 200
    assert progress.json()["step"] == "restart" and progress.json()["state"] == "restarting"
    assert uc.exits.codes == [75]
    # versions/1.5.0 from the package, byte for byte
    vdir = uc.root / "versions" / "1.5.0"
    assert json.loads((vdir / "release.json").read_text())["version"] == "1.5.0"
    assert (vdir / "backend" / "app" / "main.py").read_bytes() == b"VALUE = 1\n"
    assert not [p for p in (uc.root / "versions").iterdir() if p.name.startswith(".partial-")]
    # state.json: the launcher contract, unknown keys kept
    state = read_root_state(uc.root)
    assert state["current"] == "1.5.0" and state["previous"] == "1.4.0" and state["rollback"] is None
    pending = state["pending"]
    assert set(pending) == {"version", "from", "at", "id"}
    assert pending["version"] == "1.5.0" and pending["from"] == "1.4.0" and pending["id"] == offer["id"]
    assert state["future_key"] == before["future_key"] and state["installed_at"] == INSTALLED_AT
    assert not list(uc.root.glob("state.json.*.tmp"))
    # the backup before the update
    backups = list((uc.data / "update-backups").iterdir())
    assert len(backups) == 1 and backups[0].name.startswith("fintrack-before-1.4.0-")
    assert backups[0].suffix == ".ftbackup"
    # while restarting: no banner, no second install
    status = uc.get("/api/update/status").json()
    assert status["install"]["state"] == "restarting" and status["banner"]["show"] is False
    assert install(uc, offer["id"]).status_code == 409


def test_update_backups_keep_three_and_the_automatic_backup_runs(uc, signer, tmp_path):
    folder = uc.data / "update-backups"
    folder.mkdir()
    for version, day in (("1.3.0", "01"), ("1.3.0", "02"), ("1.10.0", "03"), ("1.3.0", "04")):
        (folder / f"fintrack-before-{version}-202601{day}-120000.ftbackup").write_bytes(b"old")
    (folder / "fintrack-before-1.3.0-20260105-120000.ftbackup.partial").write_bytes(b"half")
    (folder / "keep-me.txt").write_bytes(b"not ours")
    auto = tmp_path / "AutoBackups"
    auto.mkdir()
    assert uc.put("/api/backup/auto", json={"dir": str(auto), "keep": 5}).status_code == 200
    offer = offer_update(uc, signer)
    assert install(uc, offer["id"]).status_code == 202
    names = sorted(p.name for p in folder.iterdir())
    ours = [n for n in names if n.endswith(".ftbackup")]
    # newest 3 by time (not by version text): the new one, then the 4th and the 3rd
    assert len(ours) == 3
    assert "fintrack-before-1.10.0-20260103-120000.ftbackup" in ours
    assert "fintrack-before-1.3.0-20260104-120000.ftbackup" in ours
    assert any(n.startswith("fintrack-before-1.4.0-") for n in ours)
    assert "keep-me.txt" in names and not any(n.endswith(".partial") for n in names)
    assert len(list(auto.glob("fintrack-auto-*.ftbackup"))) == 1


def test_stale_leftover_version_folder_is_replaced(uc, signer):
    leftover = uc.root / "versions" / "1.5.0"
    (leftover / "junk").mkdir(parents=True)
    offer = offer_update(uc, signer)
    assert install(uc, offer["id"]).status_code == 202
    assert uc.exits.codes == [75]
    assert not (leftover / "junk").exists() and (leftover / "release.json").is_file()


# ------------------------------------------------------------------ failures: nothing changed


def assert_nothing_changed(c, before: dict) -> None:
    assert read_root_state(c.root) == before
    assert versions(c.root) == ["1.3.0", "1.4.0"]
    assert c.exits.codes == []


def test_tampered_inbox_copy_fails_with_02(uc, signer):
    offer = offer_update(uc, signer)
    before = read_root_state(uc.root)
    inbox = uc.updater.store.offer_package()
    data = bytearray(inbox.read_bytes())
    data[-10] ^= 0xFF
    inbox.write_bytes(bytes(data))
    r = install(uc, offer["id"])
    assert r.status_code == 202
    assert r.json()["state"] == "failed" and r.json()["code"] == "FT-UPD-02"
    assert_nothing_changed(uc, before)
    status = uc.get("/api/update/status").json()
    assert status["offer"] is None and status["install"]["code"] == "FT-UPD-02"
    last = status["last_result"]
    # the check failed before anything started: no backup was made this time
    assert last["outcome"] == "failed" and last["code"] == "FT-UPD-02" and last["backup_kept"] is False
    assert offer["id"] in uc.updater.store.failed_ids()
    # the same file is never offered again
    uc.updater.scanner._last = None
    assert uc.post("/api/update/scan", json={}).json()["offer"] is None
    # "Back to FinTrack": the ack clears the result and the failed progress
    assert uc.post("/api/update/result/ack", json={"at": last["at"]}).status_code == 204
    status = uc.get("/api/update/status").json()
    assert status["install"] is None and status["last_result"] is None
    assert uc.get("/api/update/progress").status_code == 204


def test_backup_failure_is_01_and_keeps_nothing(uc, signer, monkeypatch):
    offer = offer_update(uc, signer)
    before = read_root_state(uc.root)

    def broken(vault):
        raise OSError(errno.ENOSPC, "full")

    monkeypatch.setattr(backup_service, "build_backup", broken)
    r = install(uc, offer["id"])
    assert r.status_code == 202
    progress = uc.get("/api/update/progress").json()
    assert progress["state"] == "failed" and progress["code"] == "FT-UPD-01" and progress["step"] == "backup"
    assert_nothing_changed(uc, before)
    last = uc.get("/api/update/status").json()["last_result"]
    assert last["code"] == "FT-UPD-01" and last["backup_kept"] is False
    # a problem with the user's computer, not with the file: still offered, can be tried again
    assert offer["id"] not in uc.updater.store.failed_ids()
    assert uc.get("/api/update/status").json()["offer"]["id"] == offer["id"]


def test_disk_full_while_extracting_is_02_and_removes_the_partial_folder(uc, signer, monkeypatch):
    offer = offer_update(uc, signer)
    before = read_root_state(uc.root)
    real = pkg._extract_into

    def full(decoded, m, partial, *a, **k):
        (partial / "backend").mkdir()
        (partial / "backend" / "half.py").write_bytes(b"x")
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(pkg, "_extract_into", full)
    install(uc, offer["id"])
    assert uc.get("/api/update/progress").json()["code"] == "FT-UPD-02"
    assert_nothing_changed(uc, before)
    assert not [p for p in (uc.root / "versions").iterdir() if p.name.startswith(".partial-")]
    assert offer["id"] not in uc.updater.store.failed_ids()  # a full disk isn't the file's fault
    monkeypatch.setattr(pkg, "_extract_into", real)


def test_state_json_write_failure_removes_the_new_version(uc, signer, monkeypatch):
    offer = offer_update(uc, signer)
    before = read_root_state(uc.root)

    def fail(root, state):
        raise PermissionError("locked")

    monkeypatch.setattr(install_mod, "write_install_state", fail)
    install(uc, offer["id"])
    assert uc.get("/api/update/progress").json()["code"] == "FT-UPD-02"
    assert_nothing_changed(uc, before)


def test_install_state_must_name_this_version(uc, signer):
    offer = offer_update(uc, signer)
    state = read_root_state(uc.root)
    state["pending"] = {"version": "1.4.0", "from": "1.3.0"}  # not finished yet: never stack
    write_root_state(uc.root, state)
    install(uc, offer["id"])
    assert uc.get("/api/update/progress").json()["code"] == "FT-UPD-02"
    assert read_root_state(uc.root) == state and uc.exits.codes == []


def test_needs_help_found_at_install_time(uc, signer):
    offer = offer_update(uc, signer)
    import shutil
    shutil.rmtree(uc.root / "runtimes")  # the runtime this app update needs is gone
    before = read_root_state(uc.root)
    r = install(uc, offer["id"])
    assert r.status_code == 409 and r.json()["code"] == "needs_help"
    assert r.json()["help"] == {"reason": "runtime_missing", "code": "FT-UPD-HELP-RUNTIME"}
    assert_nothing_changed(uc, before)
    offer_now = uc.get("/api/update/status").json()["offer"]
    assert offer_now["can_install"] is False and offer_now["help"]["reason"] == "runtime_missing"
    assert offer["id"] not in uc.updater.store.failed_ids()  # not a failure: may be installed later


def test_stale_and_unknown_offer_ids(uc, signer):
    assert install(uc, "0123456789abcdef").status_code == 404
    offer = offer_update(uc, signer)
    assert install(uc, "fedcba9876543210").json()["code"] == "stale_offer"
    uc.updater.store.offer_package().unlink()  # the inbox copy is gone
    r = install(uc, offer["id"])
    assert r.status_code == 404 and uc.get("/api/update/status").json()["offer"] is None


def test_one_install_at_a_time(uc, signer, monkeypatch, tmp_path):
    offer = offer_update(uc, signer)
    gate, entered = threading.Event(), threading.Event()
    real = backup_service.build_backup

    def slow(vault):
        entered.set()
        assert gate.wait(30)
        return real(vault)

    monkeypatch.setattr(backup_service, "build_backup", slow)
    r = install(uc, offer["id"], wait=False)
    assert r.status_code == 202 and r.json()["step"] == "backup" and r.json()["state"] == "running"
    try:
        assert entered.wait(30)
        second = uc.post("/api/update/install", json={"id": offer["id"]})
        assert second.status_code == 409 and second.json()["code"] == "busy"
        picked = uc.post("/api/update/file", files={"file": ("x.ftupdate", b"x" * 300)})
        assert picked.status_code == 409 and picked.json()["code"] == "busy"
        status = uc.get("/api/update/status").json()
        assert status["install"]["state"] == "running" and status["banner"]["show"] is False
        assert uc.updater.busy and uc.app.state.fintrack.exit_watcher._busy()
    finally:
        gate.set()
    assert uc.updater._job.wait(30)
    assert uc.exits.codes == [75]


# ------------------------------------------------------------------ startup: the launcher's result


def test_launcher_result_is_ingested_and_its_id_never_offered(tmp_path, monkeypatch, fake_plaid, signer,
                                                               downloads_dir):
    root = make_install_root(tmp_path)
    package = aged(write_package(downloads_dir / "FinTrack-1.5.0.ftupdate", signer))
    offer_id = pkg.verify_package(package, "1.4.0").id
    result = {"outcome": "failed", "code": "FT-UPD-03", "from_version": "1.4.0", "to_version": "1.5.0",
              "at": "2026-09-28T12:00:00Z", "backup_kept": True, "id": offer_id}
    (tmp_path / "data" / "updates").mkdir(parents=True)
    (tmp_path / "data" / "updates" / "update_result.json").write_text(json.dumps(result))
    c = start(tmp_path, monkeypatch, fake_plaid, root)
    try:
        status = c.get("/api/update/status").json()
        assert status["last_result"] == {k: v for k, v in result.items() if k != "id"}
        assert not (tmp_path / "data" / "updates" / "update_result.json").exists()
        assert c.post("/api/update/scan", json={}).json()["offer"] is None
        assert offer_id in c.updater.store.failed_ids()
        r = c.post("/api/update/result/ack", json={"at": result["at"]})
        assert r.status_code == 204 and c.get("/api/update/status").json()["last_result"] is None
    finally:
        c.__exit__(None, None, None)


# ------------------------------------------------------------------ finishing after the first unlock


def make_snapshot(data: Path, name: str) -> None:
    (data / name).mkdir(parents=True)
    (data / name / "fintrack.db").write_bytes(b"x")


def test_first_unlock_finishes_the_update_and_prunes(tmp_path, monkeypatch, fake_plaid):
    offer_id = "0123456789abcdef"
    root = make_install_root(
        tmp_path, previous="1.3.0", versions=("1.1.0", "1.2.0"),
        pending={"version": "1.4.0", "from": "1.3.0", "at": "2026-09-28T12:00:00Z", "id": offer_id,
                 "snapshot": "pre-update-1.3.0-20260928T120000Z", "gate_passed": True},
    )
    (root / "versions" / ".partial-1.6.0-abc").mkdir()
    data = tmp_path / "data"
    for name in ("pre-update-1.1.0-20260101T000000Z", "pre-update-1.2.0-20260601T000000Z",
                 "pre-update-1.3.0-20260928T120000Z", "failed-update-1.2.0-x"):
        make_snapshot(data, name)
    c = start(tmp_path, monkeypatch, fake_plaid, root)  # setup = the first successful open
    try:
        state = read_root_state(root)
        assert state["pending"] is None and state["current"] == "1.4.0" and state["previous"] == "1.3.0"
        assert state["future_key"] == {"kept": True}
        assert versions(root) == [".partial-1.6.0-abc", "1.3.0", "1.4.0"]
        snaps = sorted(p.name for p in data.iterdir() if p.name.startswith(("pre-update-", "failed-update-")))
        assert snaps == ["failed-update-1.2.0-x", "pre-update-1.2.0-20260601T000000Z",
                         "pre-update-1.3.0-20260928T120000Z"]
        status = c.get("/api/update/status").json()
        last = status["last_result"]
        assert last["outcome"] == "installed" and last["from_version"] == "1.3.0"
        assert last["to_version"] == "1.4.0" and last["code"] is None
        assert status["current"]["last_update_at"] == last["at"]
        history = c.updater.store.history()
        assert len(history) == 1 and history[0]["id"] == offer_id
        # later unlocks change nothing
        c.post("/api/auth/lock")
        assert c.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200
        assert len(c.updater.store.history()) == 1
        assert c.exits.codes == []
    finally:
        c.__exit__(None, None, None)


def test_pending_for_another_version_is_not_finished(tmp_path, monkeypatch, fake_plaid):
    root = make_install_root(tmp_path, previous="1.3.0", pending={"version": "1.5.0", "from": "1.4.0"})
    c = start(tmp_path, monkeypatch, fake_plaid, root)
    try:
        assert read_root_state(root)["pending"] == {"version": "1.5.0", "from": "1.4.0"}
        assert c.get("/api/update/status").json()["last_result"] is None
    finally:
        c.__exit__(None, None, None)


# ------------------------------------------------------------------ data upgrade fails while pending


@pytest.fixture
def locked_pending(tmp_path, monkeypatch, fake_plaid):
    """An installed 1.4.0 that was just updated from 1.3.0 (pending), vault set up then locked."""
    root = make_install_root(tmp_path, previous="1.3.0")
    c = start(tmp_path, monkeypatch, fake_plaid, root)
    c.post("/api/auth/lock")
    state = read_root_state(root)
    state["pending"] = {"version": "1.4.0", "from": "1.3.0", "at": "2026-09-28T12:00:00Z",
                        "id": "0123456789abcdef", "snapshot": "pre-update-1.3.0-20260928T120000Z",
                        "gate_passed": True}
    write_root_state(root, state)
    fresh_process(c)
    yield c
    c.__exit__(None, None, None)


def fresh_process(c) -> None:
    """As after the launcher's restart onto the new version: this process hasn't opened the
    data yet (the setup above did, which rightly blocks any rollback in this process)."""
    c.updater._opened = False
    c.app.state.fintrack.vault.opened_once = False


def break_migrations(monkeypatch, exc: BaseException, *, upgrading: bool = True) -> None:
    """``migrate`` fails with ``exc``. With ``upgrading`` it first starts a schema upgrade
    (as for data one version behind: the ``before_upgrade`` hook, i.e. the vault's
    pre-migration copy, runs); without it the data is already at the latest schema."""
    def fail(engine, *, fresh=False, before_upgrade=None):
        if upgrading and before_upgrade is not None:
            before_upgrade(migrations.LATEST - 1)
        raise exc

    monkeypatch.setattr(migrations, "migrate", fail)


def test_migration_failure_while_pending_rolls_back(locked_pending, monkeypatch):
    c = locked_pending
    break_migrations(monkeypatch, sqlite3.OperationalError("no such column"))
    r = c.post("/api/auth/unlock", json={"password": PASSWORD})
    assert r.status_code == 409
    assert r.json() == {"detail": "update_rolled_back", "code": "FT-UPD-05", "to_version": "1.3.0"}
    state = read_root_state(c.root)
    assert state["rollback"] == {"to": "1.3.0", "code": "FT-UPD-05"}
    assert state["pending"]["version"] == "1.4.0" and state["current"] == "1.4.0"
    assert c.exits.codes == [75]
    assert not c.app.state.fintrack.vault.unlocked
    # asked again before the restart: same answer, one restart request
    r = c.post("/api/auth/unlock", json={"password": PASSWORD})
    assert r.json()["to_version"] == "1.3.0" and c.exits.codes == [75]


def test_wrong_password_while_pending_is_just_wrong(locked_pending, monkeypatch):
    c = locked_pending
    break_migrations(monkeypatch, sqlite3.OperationalError("x"))
    r = c.post("/api/auth/unlock", json={"password": "wrong password here"})
    assert r.status_code == 401
    assert read_root_state(c.root).get("rollback") is None and c.exits.codes == []


def test_migration_failure_without_pending_is_not_a_rollback(tmp_path, monkeypatch, fake_plaid):
    root = make_install_root(tmp_path, previous="1.3.0")
    c = start(tmp_path, monkeypatch, fake_plaid, root)
    try:
        c.post("/api/auth/lock")
        before = read_root_state(root)
        break_migrations(monkeypatch, sqlite3.OperationalError("x"))
        r = c.post("/api/auth/unlock", json={"password": PASSWORD})
        assert r.status_code == 500
        assert read_root_state(root) == before and c.exits.codes == []
    finally:
        c.__exit__(None, None, None)


def test_pre_migration_copy_failure_is_not_a_rollback(locked_pending, monkeypatch):
    from app.security import PreMigrationCopyFailed

    c = locked_pending
    break_migrations(monkeypatch, PreMigrationCopyFailed())
    r = c.post("/api/auth/unlock", json={"password": PASSWORD})
    assert r.status_code == 500 and r.json()["code"] == "pre_migration_copy_failed"
    assert read_root_state(c.root).get("rollback") is None and c.exits.codes == []


def test_schema_too_new_is_409(locked_pending, monkeypatch):
    c = locked_pending
    break_migrations(monkeypatch, migrations.SchemaTooNew(99))
    r = c.post("/api/auth/unlock", json={"password": PASSWORD})
    assert r.status_code == 409 and r.json()["code"] == "schema_too_new"
    assert read_root_state(c.root).get("rollback") is None and c.exits.codes == []


def test_schema_too_new_in_a_git_checkout_is_409(tmp_path, fake_plaid, monkeypatch):
    from app.main import create_app
    from tests.conftest import make_settings

    app = create_app(make_settings(tmp_path), fake_plaid)
    with SessionClient(app, base_url=BASE_URL, headers=CSRF) as c:
        c.post("/api/auth/setup", json={"password": PASSWORD})
        c.post("/api/auth/lock")
        break_migrations(monkeypatch, migrations.SchemaTooNew(99))
        r = c.post("/api/auth/unlock", json={"password": PASSWORD})
        assert r.status_code == 409 and r.json()["code"] == "schema_too_new"


# ------------------------------------------------------------------ manual rollback (optional)


def test_manual_rollback(uc):
    status = uc.get("/api/update/status").json()
    assert status["rollback"] == {"available": True, "to_version": "1.3.0"}
    r = uc.post("/api/update/rollback", json={"password": PASSWORD})
    assert r.status_code == 202 and r.json() == {"to_version": "1.3.0"}
    state = read_root_state(uc.root)
    assert state["current"] == "1.3.0" and state["previous"] is None
    assert state["pending"] is None and state["rollback"] is None  # a plain restart: no data restore
    assert state["future_key"] == {"kept": True}
    assert uc.exits.codes == [75]


def test_manual_rollback_wrong_password_is_counted(uc):
    limiter = uc.app.state.fintrack.vault.limiter
    r = uc.post("/api/update/rollback", json={"password": "not the password!"})
    assert r.status_code == 401 and r.json()["tries_left"] == limiter.THRESHOLD - 1
    for _ in range(limiter.THRESHOLD - 1):
        uc.post("/api/update/rollback", json={"password": "not the password!"})
    r = uc.post("/api/update/rollback", json={"password": PASSWORD})
    assert r.status_code == 429
    assert read_root_state(uc.root)["current"] == "1.4.0" and uc.exits.codes == []


def test_manual_rollback_needs_the_same_data_format(tmp_path, monkeypatch, fake_plaid):
    root = make_install_root(tmp_path, previous="1.3.0")
    release = root / "versions" / "1.3.0" / "release.json"
    data = json.loads(release.read_text())
    data["schema_version"] = migrations.LATEST - 1
    release.write_text(json.dumps(data))
    c = start(tmp_path, monkeypatch, fake_plaid, root)
    try:
        assert c.get("/api/update/status").json()["rollback"] == {"available": False, "to_version": None}
        r = c.post("/api/update/rollback", json={"password": PASSWORD})
        assert r.status_code == 409 and r.json()["code"] == "rollback_unavailable"
        assert read_root_state(root)["current"] == "1.4.0" and c.exits.codes == []
    finally:
        c.__exit__(None, None, None)


@pytest.mark.parametrize("case", ["no_previous", "pending", "missing_files"])
def test_manual_rollback_unavailable(tmp_path, monkeypatch, fake_plaid, case):
    root = make_install_root(tmp_path, previous=None if case == "no_previous" else "1.3.0")
    if case == "pending":
        state = read_root_state(root)
        state["pending"] = {"version": "1.5.0", "from": "1.4.0"}
        write_root_state(root, state)
    if case == "missing_files":
        (root / "versions" / "1.3.0" / "run_packaged.py").unlink()
    c = start(tmp_path, monkeypatch, fake_plaid, root)
    try:
        assert c.get("/api/update/status").json()["rollback"]["available"] is False
        assert c.post("/api/update/rollback", json={"password": PASSWORD}).status_code == 409
    finally:
        c.__exit__(None, None, None)

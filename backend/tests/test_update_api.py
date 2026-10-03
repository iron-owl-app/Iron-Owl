"""/api/update/*: modes, sessions, CSRF/multipart, scan, picked files, Not now, polling."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from app import update_keys
from app.routers import update as update_router
from app.security import SESSION_HEADER
from app.updates import downloads
from app.updates.store import UpdateStore
from tests.conftest import BASE_URL, CSRF, PASSWORD, SessionClient
from tests.update_helpers import (
    INSTALLED_AT,
    Signer,
    aged,
    build_app,
    make_install_root,
    write_package,
)

STATUS_KEYS = {"mode", "support_contact", "current", "offer", "banner", "install", "last_result", "rollback",
               "source", "auto_check", "last_check", "check_error"}
OFFER_KEYS = {"id", "version", "display_version", "released_at", "notes", "kind", "minutes", "size_bytes",
              "source", "file_name", "can_install", "help"}
POSTS = ("/api/update/scan", "/api/update/dismiss", "/api/update/install", "/api/update/result/ack",
         "/api/update/rollback")


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


@pytest.fixture
def root(tmp_path) -> Path:
    return make_install_root(tmp_path)


@pytest.fixture
def inst(tmp_path, monkeypatch, fake_plaid, root, downloads_dir):
    app, exits = build_app(tmp_path, monkeypatch, fake_plaid, root)
    with SessionClient(app, base_url=BASE_URL, headers=CSRF) as c:
        c.exits = exits
        c.root = root
        c.downloads = downloads_dir
        c.data = tmp_path / "data"
        c.responses = []
        yield c


@pytest.fixture
def uc(inst):
    """Installed copy, unlocked."""
    r = inst.post("/api/auth/setup", json={"password": PASSWORD})
    assert r.status_code == 200, r.text
    return inst


def put_download(folder: Path, signer: Signer, name: str = "FinTrack-1.5.0.ftupdate", **kw) -> Path:
    return aged(write_package(folder / name, signer, **kw))


def pick(c, data: bytes, name: str = "FinTrack-1.5.0.ftupdate", **kw):
    return c.post("/api/update/file", files={"file": (name, data, "application/octet-stream")}, **kw)


def assert_no_paths(tmp_path: Path, *bodies: str) -> None:
    for body in bodies:
        for needle in (str(tmp_path), tmp_path.as_posix(), "\\\\", ":/", ":\\\\", "Users"):
            assert needle not in body, (needle, body)


# ------------------------------------------------------------------ modes


@pytest.mark.parametrize("mode", ["git", "dev", "not_installed"])
def test_disabled_modes_never_scan_or_write(tmp_path, monkeypatch, fake_plaid, signer, mode):
    def boom(*a, **k):
        raise AssertionError("Downloads looked up")

    monkeypatch.setattr(downloads, "downloads_folder", boom)
    monkeypatch.setattr(downloads, "known_downloads_folder", boom)
    root = None if mode == "not_installed" else make_install_root(tmp_path)
    if mode == "git":  # the code folder is a git checkout (a worktree has a .git file)
        (root / "versions" / "1.4.0" / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
    overrides = {"dev": True} if mode == "dev" else {}
    app, exits = build_app(tmp_path, monkeypatch, fake_plaid, root, **overrides)
    updater = app.state.fintrack.updater
    assert updater.mode == mode and updater.scanner is None
    with SessionClient(app, base_url=BASE_URL, headers=CSRF) as c:
        assert c.post("/api/auth/setup", json={"password": PASSWORD}).status_code == 200
        s = c.get("/api/update/status")
        assert s.status_code == 200
        body = s.json()
        assert set(body) == STATUS_KEYS and body["mode"] == mode
        assert body["offer"] is None and body["banner"] == {"show": False, "remind_after": None}
        assert body["install"] is None and body["rollback"] == {"available": False, "to_version": None}
        assert body["current"]["version"] == "1.4.0" and body["current"]["display_version"] == "1.4"
        assert body["support_contact"] == "Sam"
        bodies = {
            "/api/update/scan": {}, "/api/update/dismiss": {"id": "0" * 16},
            "/api/update/install": {"id": "0" * 16}, "/api/update/result/ack": {"at": "x"},
            "/api/update/rollback": {"password": PASSWORD},
        }
        for path, payload in bodies.items():
            r = c.post(path, json=payload)
            assert r.status_code == 409 and r.json()["code"] == "disabled", path
        assert pick(c, b"x" * 300).json()["code"] == "disabled"
        assert c.get("/api/update/progress").status_code == 204
        updater.scan_tick(True)
    assert not (tmp_path / "data" / "updates").exists()
    assert exits.codes == []


def test_owners_git_checkout_is_never_offered(tmp_path, monkeypatch, fake_plaid, signer, downloads_dir):
    """The real code folder is a git checkout: default settings mean mode git."""
    from app.main import create_app
    from tests.conftest import make_settings

    put_download(downloads_dir, signer)
    app = create_app(make_settings(tmp_path), fake_plaid)
    assert app.state.fintrack.updater.mode == "git"
    with SessionClient(app, base_url=BASE_URL, headers=CSRF) as c:
        c.post("/api/auth/setup", json={"password": PASSWORD})
        assert c.post("/api/update/scan", json={}).status_code == 409
        assert pick(c, (downloads_dir / "FinTrack-1.5.0.ftupdate").read_bytes()).status_code == 409
        assert c.get("/api/update/status").json()["offer"] is None
    assert not (tmp_path / "data" / "updates").exists()


# ------------------------------------------------------------------ sessions, CSRF, multipart


def test_every_route_needs_a_session(inst):
    assert inst.get("/api/update/status").status_code == 401
    assert inst.get("/api/update/progress").status_code == 401
    bodies = ({}, {"id": "0" * 16}, {"id": "0" * 16}, {"at": "x"}, {"password": PASSWORD})
    for path, body in zip(POSTS, bodies):
        r = inst.post(path, json=body)
        assert r.status_code == 401 and r.json()["detail"] == "locked", path
    assert pick(inst, b"x" * 300).status_code == 401
    assert not (inst.data / "updates" / "inbox").exists()


def test_csrf_header_required(uc):
    for path in POSTS:
        assert uc.post(path, json={}, headers={"X-FinTrack": "0"}).status_code == 403, path
    assert pick(uc, b"x" * 300, headers={"X-FinTrack": "0"}).status_code == 403


def test_multipart_only_on_the_exact_file_path(uc, signer):
    data = write_package(uc.data.parent / "p.ftupdate", signer).read_bytes()
    files = {"file": ("FinTrack-1.5.0.ftupdate", data, "application/octet-stream")}
    for path in (*POSTS, "/api/update/file/", "/api/update/files", "/api/update", "/api/accounts"):
        assert uc.post(path, files=files).status_code == 403, path
    # and /file takes nothing but multipart with one "file" part
    r = uc.post("/api/update/file", json={"file": "x"})
    assert r.status_code == 400 and r.json()["code"] == "bad_upload"
    r = uc.post("/api/update/file", files=files, data={"password": PASSWORD})
    assert r.status_code == 400 and r.json()["code"] == "bad_upload"
    r = uc.post("/api/update/file", files={"other": ("x", b"y")})
    assert r.status_code == 400
    assert pick(uc, data).json()["result"] == "ready"
    assert not list((uc.data / "updates").glob(".incoming-*"))


# ------------------------------------------------------------------ status / scan


def test_status_shape_and_scan_offer(uc, signer, tmp_path):
    s = uc.get("/api/update/status").json()
    assert set(s) == STATUS_KEYS
    assert s["mode"] == "enabled" and s["support_contact"] == "Sam"
    assert s["current"] == {"version": "1.4.0", "display_version": "1.4", "installed_at": INSTALLED_AT,
                            "last_update_at": None}
    assert s["offer"] is None and s["last_result"] is None and s["install"] is None
    put_download(uc.downloads, signer)
    r = uc.post("/api/update/scan", json={})
    assert r.status_code == 200
    body = r.json()
    offer = body["offer"]
    assert set(offer) == OFFER_KEYS
    assert offer["version"] == "1.5.0" and offer["display_version"] == "1.5" and offer["source"] == "downloads"
    assert offer["file_name"] == "FinTrack-1.5.0.ftupdate" and offer["can_install"] is True
    assert offer["kind"] == "app" and offer["minutes"] == 1 and offer["help"] is None
    assert body["banner"] == {"show": True, "remind_after": None}
    assert uc.get("/api/update/status").json()["offer"] == offer
    assert_no_paths(tmp_path, r.text)


def test_scan_body_must_be_empty_json(uc):
    assert uc.post("/api/update/scan").status_code == 200
    assert uc.post("/api/update/scan", json={"x": 1}).status_code == 422


def test_scan_is_throttled(uc, signer):
    scanner = uc.app.state.fintrack.updater.scanner
    calls = []
    real = scanner._scan
    scanner._scan = lambda now: calls.append(now) or real(now)
    uc.post("/api/update/scan", json={})
    put_download(uc.downloads, signer)
    body = uc.post("/api/update/scan", json={}).json()
    assert len(calls) == 1 and body["offer"] is None  # within 30 s: the cached answer


def test_scan_tick_only_while_unlocked(inst, signer):
    updater = inst.app.state.fintrack.updater
    put_download(inst.downloads, signer)
    assert updater.scan_tick(False) is False
    assert updater.store.offer() is None
    assert updater.scan_tick(True) is True
    assert updater.store.offer()["version"] == "1.5.0"


# ------------------------------------------------------------------ picked files


def test_picked_file_becomes_the_offer(uc, signer, tmp_path):
    data = write_package(tmp_path / "x.ftupdate", signer).read_bytes()
    r = pick(uc, data, name="C:\\Users\\user\\Downloads\\FinTrack-1.5.0 (1).ftupdate")
    assert r.status_code == 200
    body = r.json()
    assert body["result"] == "ready" and set(body["offer"]) == OFFER_KEYS
    assert body["offer"]["source"] == "picked" and body["offer"]["file_name"] == "FinTrack-1.5.0 (1).ftupdate"
    status = uc.get("/api/update/status").json()
    assert status["offer"]["id"] == body["offer"]["id"] and status["banner"]["show"] is True
    assert_no_paths(tmp_path, r.text, json.dumps(status))


def test_picked_rejected_older_same(uc, signer, tmp_path):
    r = pick(uc, b"not an update" * 30, name="bad.ftupdate")
    assert r.json() == {"result": "rejected", "reason": "not_update", "code": "FT-UPD-NOTUPD",
                        "file_name": "bad.ftupdate"}
    other = Signer()
    r = pick(uc, write_package(tmp_path / "u.ftupdate", other).read_bytes())
    assert r.json()["code"] == "FT-UPD-SIG" and r.json()["reason"] == "signature"
    r = pick(uc, write_package(tmp_path / "o.ftupdate", signer, version="1.2.0").read_bytes(), name="o.ftupdate")
    assert r.json() == {"result": "older", "code": "FT-UPD-OLD", "file_name": "o.ftupdate",
                        "file_version": "1.2", "current_version": "1.4"}
    r = pick(uc, write_package(tmp_path / "s.ftupdate", signer, version="1.4.0").read_bytes(), name="s.ftupdate")
    assert r.json()["result"] == "same" and r.json()["code"] == "FT-UPD-SAME"
    assert uc.get("/api/update/status").json()["offer"] is None


def test_picked_file_that_failed_before(uc, signer, tmp_path):
    data = write_package(tmp_path / "x.ftupdate", signer).read_bytes()
    offer_id = pick(uc, data).json()["offer"]["id"]
    uc.app.state.fintrack.updater.store.record_failed(
        offer_id=offer_id, version="1.5.0", code="FT-UPD-03", at="2026-09-28T12:00:00Z")
    r = pick(uc, data, name="again.ftupdate")
    assert r.json() == {"result": "failed", "code": "FT-UPD-03", "file_name": "again.ftupdate",
                        "file_version": "1.5", "current_version": "1.4"}
    assert uc.get("/api/update/status").json()["offer"] is None


def test_picked_file_too_large(uc, monkeypatch):
    monkeypatch.setattr(update_router, "MAX_PACKAGE_BYTES", 1000)
    monkeypatch.setattr(update_router, "MAX_FILE_REQUEST_BYTES", 5000)
    r = pick(uc, b"x" * 2000)
    assert r.status_code == 413 and r.json()["code"] == "FT-UPD-BIG"
    r = pick(uc, b"x" * 6000)  # refused by Content-Length before reading
    assert r.status_code == 413 and r.json()["code"] == "FT-UPD-BIG"
    assert not list((uc.data / "updates").glob(".incoming-*"))


def test_one_check_at_a_time(uc):
    updater = uc.app.state.fintrack.updater
    assert updater.check_lock.acquire(blocking=False)
    try:
        r = pick(uc, b"x" * 300)
        assert r.status_code == 429 and r.json()["code"] == "busy"
    finally:
        updater.check_lock.release()
    assert pick(uc, b"x" * 300).status_code == 200


# ------------------------------------------------------------------ Not now


def test_not_now_until_tomorrow_and_a_new_offer_resets_it(uc, signer, tmp_path, fixed_today):
    fixed_today.set(dt.date(2026, 9, 28))
    first = pick(uc, write_package(tmp_path / "a.ftupdate", signer).read_bytes()).json()["offer"]
    r = uc.post("/api/update/dismiss", json={"id": first["id"]})
    assert r.status_code == 200
    assert r.json()["banner"] == {"show": False, "remind_after": "2026-09-29"}
    assert r.json()["offer"]["id"] == first["id"]  # Settings still offers it
    fixed_today.set(dt.date(2026, 9, 29))
    assert uc.get("/api/update/status").json()["banner"] == {"show": True, "remind_after": None}
    fixed_today.set(dt.date(2026, 9, 28))
    assert uc.get("/api/update/status").json()["banner"]["show"] is False
    newer = pick(uc, write_package(tmp_path / "b.ftupdate", signer, version="1.6.0").read_bytes()).json()
    assert newer["offer"]["version"] == "1.6.0"
    assert uc.get("/api/update/status").json()["banner"] == {"show": True, "remind_after": None}


def test_dismiss_stale_offer(uc, signer, tmp_path):
    for payload in ({"id": "0123456789abcdef"}, {"id": "../../x"}):
        r = uc.post("/api/update/dismiss", json=payload)
        assert r.status_code == 404 and r.json()["code"] == "stale_offer"
    assert uc.post("/api/update/dismiss", json={"id": "x", "extra": 1}).status_code == 422


def test_needs_help_offer_gets_no_banner(uc, signer, tmp_path):
    body = pick(uc, write_package(tmp_path / "h.ftupdate", signer, requires_reinstall=True).read_bytes()).json()
    assert body["offer"]["can_install"] is False
    assert body["offer"]["help"] == {"reason": "reinstall", "code": "FT-UPD-HELP-REINSTALL"}
    status = uc.get("/api/update/status").json()
    assert status["offer"]["can_install"] is False and status["banner"]["show"] is False
    r = uc.post("/api/update/install", json={"id": body["offer"]["id"]})
    assert r.status_code == 409 and r.json()["code"] == "needs_help"
    assert r.json()["help"] == {"reason": "reinstall", "code": "FT-UPD-HELP-REINSTALL"}


# ------------------------------------------------------------------ polling and the idle lock


def test_polling_does_not_delay_the_idle_lock(clock, uc):
    vault = uc.app.state.fintrack.vault
    clock.advance(14 * 60)
    for _ in range(3):
        assert uc.get("/api/update/status").status_code == 200
        assert uc.get("/api/update/progress").status_code == 204
    clock.advance(60 + 1)
    vault.check_idle()
    assert not vault.unlocked
    assert uc.get("/api/update/status").status_code == 401


def test_actions_count_as_activity(clock, uc):
    vault = uc.app.state.fintrack.vault
    clock.advance(14 * 60)
    uc.post("/api/update/scan", json={})
    clock.advance(2 * 60)
    vault.check_idle()
    assert vault.unlocked


# ------------------------------------------------------------------ results


def test_ack_clears_the_last_result(uc):
    store: UpdateStore = uc.app.state.fintrack.updater.store
    result = {"outcome": "installed", "from_version": "1.3.0", "to_version": "1.4.0", "code": None,
              "at": "2026-09-28T12:00:00Z", "backup_kept": True}
    store.set_last_result(result)
    assert uc.get("/api/update/status").json()["last_result"] == result
    assert uc.post("/api/update/result/ack", json={"at": "2026-01-01T00:00:00Z"}).status_code == 204
    assert uc.get("/api/update/status").json()["last_result"] == result
    assert uc.post("/api/update/result/ack", json={"at": result["at"]}).status_code == 204
    assert uc.get("/api/update/status").json()["last_result"] is None


def test_version_is_public_on_health_only(inst):
    assert inst.get("/api/health").json()["version"] == "1.4.0"
    assert "version" not in inst.get("/api/auth/status").json()


def test_no_support_contact_is_null(tmp_path, monkeypatch, fake_plaid, root, downloads_dir):
    app, _ = build_app(tmp_path, monkeypatch, fake_plaid, root, support_contact="")
    with SessionClient(app, base_url=BASE_URL, headers=CSRF) as c:
        c.post("/api/auth/setup", json={"password": PASSWORD})
        assert c.get("/api/update/status").json()["support_contact"] is None

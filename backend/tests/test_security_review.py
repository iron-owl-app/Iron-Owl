"""Regression tests for issues found in the adversarial security review."""
from __future__ import annotations

import ntpath
import os
import subprocess
import sys
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import db as dbmod
from app.main import create_app
from tests.conftest import BASE_URL, CSRF, PASSWORD, make_settings

# ------------------------------------------------------------ static file serving


@pytest.fixture
def spa(tmp_path, fake_plaid):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>FinTrack</title>")
    return TestClient(create_app(make_settings(tmp_path), fake_plaid), base_url=BASE_URL)


@pytest.mark.parametrize(
    "url",
    [
        # UNC paths: resolving/stat-ing these makes Windows authenticate to the
        # remote host (SMB/WebDAV), leaking the user's NTLM hash. A malicious web
        # page can trigger this with <img src="http://127.0.0.1:8000/%5C%5Chost%5Cx">.
        "/%5C%5Cnonexistent-host.invalid%5Cshare%5Cx",
        "/%2F%2Fnonexistent-host.invalid%2Fshare%2Fx",
        "/assets/%5C%5Cnonexistent-host.invalid%5Cshare",
        # Absolute / drive-relative paths outside dist.
        "/C:%2FWindows%2Fnonexistent-host.invalid",
        "/C:nonexistent-host.invalid",
    ],
)
def test_spa_never_touches_paths_outside_dist(spa, monkeypatch, url):
    touched: list[str] = []
    real_stat, real_realpath = os.stat, ntpath.realpath

    def spy_stat(p, *a, **k):
        touched.append(os.fspath(p))
        return real_stat(p, *a, **k)

    def spy_realpath(p, *a, **k):
        touched.append(os.fspath(p))
        return real_realpath(p, *a, **k)

    monkeypatch.setattr(os, "stat", spy_stat)
    monkeypatch.setattr(ntpath, "realpath", spy_realpath)
    monkeypatch.setattr(os.path, "realpath", spy_realpath)
    r = spa.get(url)
    assert r.status_code in (200, 404)
    assert not [p for p in touched if "nonexistent-host" in p], touched


def test_spa_nul_byte_is_not_a_server_error(spa):
    r = spa.get("/assets/a%00b.js")
    assert r.status_code in (200, 404)


# ------------------------------------------------------------------ lock hygiene


def test_connection_opened_during_lock_is_not_leaked(unlocked, vault, monkeypatch):
    """A connection being created while lock() runs must not survive the lock."""
    database = vault.db
    session = database.acquire()
    database._engine.dispose()  # force the next query to open a fresh raw connection
    database._close_raw()
    opened: list = []
    real_open = dbmod.open_raw_connection

    def racing_open(path, key):
        conn = real_open(path, key)
        opened.append(conn)
        database.close(wait_seconds=0)  # lock lands while the creator is mid-flight
        return conn

    monkeypatch.setattr(dbmod, "open_raw_connection", racing_open)
    with pytest.raises(Exception):
        session.execute(text("SELECT 1"))
    monkeypatch.setattr(dbmod, "open_raw_connection", real_open)
    try:
        database.release(session)
    except Exception:  # noqa: BLE001
        pass

    assert not database.is_open
    assert database.open_connection_count == 0
    assert opened, "the race was not exercised"
    with pytest.raises(dbmod.sqlcipher3.ProgrammingError):
        opened[0].execute("SELECT 1")  # closed, so it no longer holds the key


# ---------------------------------------------------------------- tampered inputs


@pytest.mark.parametrize(
    "content",
    [
        "[]",
        '"x"',
        '{"version":1,"kdf":"argon2id","salt":"AAAAAAAAAAAAAAAAAAAAAA==",'
        '"time_cost":Infinity,"memory_cost":65536,"parallelism":4}',
        '{"version":1,"kdf":"argon2id","salt":null,"time_cost":3,"memory_cost":65536,"parallelism":4}',
    ],
)
def test_tampered_keyfile_fails_closed_without_500(unlocked, settings, content):
    unlocked.post("/api/auth/lock")
    settings.keyfile_path.write_text(content)
    r = unlocked.post("/api/auth/unlock", json={"password": PASSWORD})
    assert r.status_code == 401
    assert r.json() == {"detail": "invalid password", "code": "wrong_password", "tries_left": 4}
    # The recovery sheet fails closed too: no block can be read, so "no recovery", never a 500.
    r = unlocked.post("/api/auth/recover/check", json={"code": "739151204868581233916405362581047913"})
    assert r.status_code == 409 and r.json()["code"] == "no_recovery"


def test_lone_surrogate_password_is_not_a_server_error(client):
    body = b'{"password": "\\ud800' + b"a" * 16 + b'"}'
    headers = {"content-type": "application/json"}
    r = client.post("/api/auth/setup", content=body, headers=headers)
    assert r.status_code == 200, r.text
    client.post("/api/auth/lock")
    assert client.post("/api/auth/unlock", content=body, headers=headers).status_code == 200


# ------------------------------------------------------------------- brute force


def test_parallel_unlock_attempts_cannot_exceed_backoff(unlocked):
    unlocked.post("/api/auth/lock")
    codes: list[int] = []

    def attempt() -> None:
        c = TestClient(unlocked.app, base_url=BASE_URL, headers=CSRF)
        codes.append(c.post("/api/auth/unlock", json={"password": "wrong password!!"}).status_code)

    threads = [threading.Thread(target=attempt) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert codes.count(401) == 4  # the 5th failure starts the lockout
    assert codes.count(429) == 8


# ------------------------------------------------------------ data dir permissions


def _sddl(path) -> str:
    out = path.parent / (path.name + ".acl")
    subprocess.run(["icacls", str(path), "/save", str(out)], check=True, capture_output=True)
    try:
        return out.read_bytes().decode("utf-16").splitlines()[1]
    finally:
        out.unlink()


def _my_sid() -> str:
    whoami = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "whoami.exe")
    out = subprocess.run([whoami, "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True)
    return out.stdout.strip().rsplit(",", 1)[-1].strip('"')


def _assert_private(path) -> None:
    import re

    sddl = _sddl(path)
    sids = set(re.findall(r"\(A;[^;]*;[^;]*;;;([^)]+)\)", sddl))
    # Only this user, SYSTEM and Administrators: no Everyone/Users/Authenticated Users.
    assert sids and sids <= {_my_sid(), "SY", "BA", "S-1-5-18", "S-1-5-32-544"}, sddl


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACLs")
def test_data_dir_is_private_to_the_user_windows(unlocked, settings):
    assert _sddl(settings.data_dir).startswith("D:P")  # inheritance from the drive is cut
    for path in (settings.data_dir, settings.db_path, settings.keyfile_path):
        _assert_private(path)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACLs")
def test_existing_data_dir_is_restricted_on_startup(tmp_path, fake_plaid):
    settings = make_settings(tmp_path)
    settings.data_dir.mkdir()
    (settings.data_dir / "fintrack.db").write_bytes(b"x")  # pre-existing vault file
    with TestClient(create_app(settings, fake_plaid), base_url=BASE_URL):
        pass
    _assert_private(settings.data_dir)
    _assert_private(settings.data_dir / "fintrack.db")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_data_dir_is_private_to_the_user_posix(unlocked, settings):
    assert settings.data_dir.stat().st_mode & 0o077 == 0

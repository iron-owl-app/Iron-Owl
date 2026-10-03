from __future__ import annotations

import json
import os

import pytest
import sqlcipher3

from app import db as dbmod
from app.security import KeyParams, read_keyfile, write_json_atomic
from tests.conftest import PASSWORD

NEW_PASSWORD = "another long passphrase 42"


def status(client) -> dict:
    return client.get("/api/auth/status").json()


def _status(initialized: bool, unlocked: bool, sheet: str | None = None) -> dict:
    return {
        "initialized": initialized, "unlocked": unlocked, "auto_lock_minutes": 15,
        "recovery": {"available": sheet is not None, "sheet": sheet},
        "open_elsewhere": False, "lock_reason": None,
        "retry_after": None, "tries_left": 5, "support_contact": None,
    }


def test_status_before_setup_and_no_db(client, settings):
    assert status(client) == _status(False, False)
    assert not settings.db_path.exists()
    assert client.get("/api/accounts").json() == {"detail": "locked"}


def test_setup_sets_session_cookie(client, settings):
    r = client.post("/api/auth/setup", json={"password": PASSWORD})
    assert r.status_code == 200 and r.json()["ok"] is True
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("ft_session=")
    lowered = cookie.lower()
    assert "httponly" in lowered and "samesite=strict" in lowered and "path=/" in lowered
    assert "secure" not in lowered.replace("samesite", "")
    assert status(client) == _status(True, True, r.json()["recovery"]["sheet"])

    keyfile = json.loads(settings.keyfile_path.read_text())
    # The recovery sheet's block is optional and ignored by older builds (KeyParams.from_json).
    assert keyfile.keys() == {"version", "kdf", "salt", "time_cost", "memory_cost", "parallelism", "recovery"}
    assert keyfile["kdf"] == "argon2id" and keyfile["version"] == 1
    assert (keyfile["time_cost"], keyfile["memory_cost"], keyfile["parallelism"]) == (3, 65536, 4)
    assert len(read_keyfile(settings.keyfile_path).salt) == 16


def test_setup_rejects_short_password_and_reinit(client):
    r = client.post("/api/auth/setup", json={"password": "short"})
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], str)
    assert client.post("/api/auth/setup", json={"password": PASSWORD}).status_code == 200
    assert client.post("/api/auth/setup", json={"password": PASSWORD}).status_code == 409


def test_validation_errors_do_not_echo_password(client):
    r = client.post("/api/auth/setup", json={"password": 123456789012345, "extra": "x"})
    assert r.status_code == 422
    assert "123456789012345" not in r.text
    assert isinstance(r.json()["detail"], str)


def test_lock_and_unlock(unlocked, vault):
    r = unlocked.post("/api/auth/lock")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert "ft_session=" in r.headers["set-cookie"]
    assert status(unlocked)["unlocked"] is False
    assert not vault.db.is_open and vault.db.key is None
    assert vault.db.open_connection_count == 0

    for path in ("/api/accounts", "/api/summary", "/api/networth/history", "/api/transactions",
                 "/api/holdings", "/api/plaid/items", "/api/plaid/status"):
        r = unlocked.get(path)
        assert r.status_code == 401 and r.json() == {"detail": "locked"}, path
    assert unlocked.post("/api/accounts", json={"name": "x", "category": "bank", "current_balance": 1}).status_code == 401

    r = unlocked.post("/api/auth/unlock", json={"password": PASSWORD})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert status(unlocked)["unlocked"] is True
    assert unlocked.get("/api/accounts").status_code == 200


def test_lock_closes_every_connection(unlocked, vault):
    unlocked.post("/api/accounts", json={"name": "a", "category": "bank", "current_balance": 1})
    unlocked.get("/api/accounts")
    assert vault.db.open_connection_count >= 1
    unlocked.post("/api/auth/lock")
    assert vault.db.open_connection_count == 0


def test_wrong_password_then_rate_limited(unlocked, clock):
    unlocked.post("/api/auth/lock")
    for left in (4, 3, 2, 1):
        r = unlocked.post("/api/auth/unlock", json={"password": "wrong password!!"})
        assert r.status_code == 401
        assert r.json() == {"detail": "invalid password", "code": "wrong_password", "tries_left": left}
    r = unlocked.post("/api/auth/unlock", json={"password": "wrong password!!"})
    assert r.status_code == 429
    assert r.json() == {"detail": "too many attempts", "code": "rate_limited", "retry_after": 30}
    assert r.headers["retry-after"] == "30"

    # Even the right password is refused during the lockout.
    r = unlocked.post("/api/auth/unlock", json={"password": PASSWORD})
    assert r.status_code == 429 and 0 < r.json()["retry_after"] <= 30

    clock.advance(31)
    r = unlocked.post("/api/auth/unlock", json={"password": "wrong password!!"})
    assert r.status_code == 429 and r.json()["retry_after"] == 60  # doubled

    clock.advance(61)
    assert unlocked.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200
    # Success resets the counter.
    unlocked.post("/api/auth/lock")
    assert unlocked.post("/api/auth/unlock", json={"password": "nope nope nope"}).status_code == 401


def test_backoff_is_capped(vault, clock):
    limiter = vault.limiter
    for _ in range(30):
        limiter.record_failure()
    assert limiter.retry_after() == 15 * 60


def test_db_file_is_encrypted(unlocked, settings):
    unlocked.post("/api/accounts", json={"name": "Checking", "category": "bank", "current_balance": 1234.56})
    unlocked.post("/api/auth/lock")
    raw = settings.db_path.read_bytes()
    assert not raw.startswith(b"SQLite format 3")
    assert b"Checking" not in raw


def test_db_cannot_be_opened_with_wrong_key(unlocked, settings):
    unlocked.post("/api/auth/lock")
    assert not dbmod.verify_key(settings.db_path, os.urandom(32))
    wrong = read_keyfile(settings.keyfile_path).derive("not the password at all")
    assert not dbmod.verify_key(settings.db_path, wrong)

    conn = sqlcipher3.connect(str(settings.db_path))
    conn.execute("PRAGMA key = \"x'" + os.urandom(32).hex() + "'\"")
    with pytest.raises(sqlcipher3.DatabaseError):
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    conn.close()

    right = read_keyfile(settings.keyfile_path).derive(PASSWORD)
    assert dbmod.verify_key(settings.db_path, right)


def test_plain_sqlite_cannot_read_db(unlocked, settings):
    import sqlite3

    unlocked.post("/api/auth/lock")
    conn = sqlite3.connect(str(settings.db_path))
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("SELECT * FROM sqlite_master").fetchall()
    conn.close()


def test_change_password(unlocked, settings):
    unlocked.post("/api/accounts", json={"name": "Savings", "category": "bank", "current_balance": 10})
    salt_before = read_keyfile(settings.keyfile_path).salt

    r = unlocked.post("/api/auth/change-password",
                      json={"current_password": "wrong password!!", "new_password": NEW_PASSWORD})
    assert r.status_code == 401
    assert r.json() == {"detail": "invalid password", "code": "wrong_password", "tries_left": 4}

    r = unlocked.post("/api/auth/change-password",
                      json={"current_password": PASSWORD, "new_password": "short"})
    assert r.status_code == 422

    r = unlocked.post("/api/auth/change-password",
                      json={"current_password": PASSWORD, "new_password": NEW_PASSWORD})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert read_keyfile(settings.keyfile_path).salt != salt_before
    assert not settings.keyfile_path.with_name("keyfile.json.new").exists()
    # Still unlocked with the rotated session, data intact.
    assert [a["name"] for a in unlocked.get("/api/accounts").json()] == ["Savings"]

    unlocked.post("/api/auth/lock")
    assert unlocked.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 401
    assert unlocked.post("/api/auth/unlock", json={"password": NEW_PASSWORD}).status_code == 200
    assert [a["name"] for a in unlocked.get("/api/accounts").json()] == ["Savings"]


def test_change_password_requires_session(unlocked):
    unlocked.post("/api/auth/lock")
    r = unlocked.post("/api/auth/change-password",
                      json={"current_password": PASSWORD, "new_password": NEW_PASSWORD})
    assert r.status_code == 401


def test_unlock_recovers_interrupted_password_change(unlocked, settings):
    """Crash after PRAGMA rekey but before keyfile.json was replaced."""
    unlocked.post("/api/accounts", json={"name": "Brokerage", "category": "investment", "current_balance": 5})
    unlocked.post("/api/auth/lock")

    old_key = read_keyfile(settings.keyfile_path).derive(PASSWORD)
    new_params = KeyParams.new()
    new_path = settings.keyfile_path.with_name("keyfile.json.new")
    write_json_atomic(new_path, new_params.to_json())
    dbmod.rekey(settings.db_path, old_key, new_params.derive(NEW_PASSWORD))

    assert unlocked.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 401
    assert unlocked.post("/api/auth/unlock", json={"password": NEW_PASSWORD}).status_code == 200
    assert not new_path.exists()
    assert read_keyfile(settings.keyfile_path).salt == new_params.salt
    assert [a["name"] for a in unlocked.get("/api/accounts").json()] == ["Brokerage"]


def test_unlock_discards_stale_new_keyfile(unlocked, settings):
    """Crash after writing keyfile.json.new but before the rekey."""
    unlocked.post("/api/auth/lock")
    new_path = settings.keyfile_path.with_name("keyfile.json.new")
    write_json_atomic(new_path, KeyParams.new().to_json())
    assert unlocked.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200
    assert not new_path.exists()


def test_new_unlock_replaces_old_session(unlocked, app):
    from fastapi.testclient import TestClient

    from tests.conftest import BASE_URL, CSRF, SessionClient

    old_cookie = unlocked.cookies.get("ft_session")
    other = SessionClient(app, base_url=BASE_URL, headers=CSRF)  # no lifespan: shares app state
    assert other.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200
    assert other.get("/api/accounts").status_code == 200
    assert unlocked.cookies.get("ft_session") == old_cookie
    # App-window states: the replaced window learns its session moved (not just "locked").
    r = unlocked.get("/api/accounts")
    assert r.status_code == 401 and r.json() == {"detail": "moved"}
    assert other.get("/api/accounts").status_code == 200


def test_auto_lock_after_idle(unlocked, vault, clock, settings):
    # Session was created before the clock was patched; re-unlock under the fake clock.
    unlocked.post("/api/auth/lock")
    unlocked.post("/api/auth/unlock", json={"password": PASSWORD})

    clock.advance(14 * 60)
    assert unlocked.get("/api/accounts").status_code == 200  # activity resets the timer
    clock.advance(14 * 60)
    assert unlocked.get("/api/summary").status_code == 200

    # Status polling is not activity.
    clock.advance(10 * 60)
    assert status(unlocked)["unlocked"] is True
    clock.advance(5 * 60 + 1)
    assert status(unlocked)["unlocked"] is False
    assert not vault.db.is_open and vault.db.key is None
    assert unlocked.get("/api/accounts").json() == {"detail": "locked"}


def test_background_idle_check_locks_without_requests(unlocked, vault, clock):
    unlocked.post("/api/auth/lock")
    unlocked.post("/api/auth/unlock", json={"password": PASSWORD})
    clock.advance(15 * 60 + 1)
    vault.check_idle()  # what the lifespan watcher calls periodically
    assert not vault.db.is_open


def test_unlock_before_setup(client):
    r = client.post("/api/auth/unlock", json={"password": PASSWORD})
    assert r.status_code == 409


def test_restart_means_locked(unlocked, settings, fake_plaid):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.conftest import BASE_URL, CSRF, SessionClient

    with TestClient(create_app(settings, fake_plaid), base_url=BASE_URL, headers=CSRF) as fresh:
        fresh.cookies.set("ft_session", unlocked.cookies.get("ft_session"))
        assert fresh.get("/api/auth/status").json()["unlocked"] is False
        assert fresh.get("/api/accounts").status_code == 401

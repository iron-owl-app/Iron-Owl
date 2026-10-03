"""Encrypted backup download and multipart restore (security-sensitive paths)."""
from __future__ import annotations

import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app import migrations
from app.main import create_app
from app.security import SESSION_HEADER, read_keyfile
from app.services import backup as backup_service
from tests.conftest import BASE_URL, CSRF, PASSWORD, SessionClient, make_settings

MARKER = "Zebra Credit Union Marker"


@pytest.fixture
def vault_with_data(unlocked):
    r = unlocked.post("/api/accounts", json={"name": MARKER, "category": "bank", "current_balance": 1234.56})
    assert r.status_code == 201
    return unlocked


def download(client, password=PASSWORD):
    return client.post("/api/backup", json={"password": password})


def restore(client, data: bytes, password=PASSWORD, current_password=None, **kw):
    form = {"password": password}
    if current_password is not None:
        form["current_password"] = current_password
    return client.post(
        "/api/restore",
        files={"file": ("vault.ftbackup", data, "application/octet-stream")},
        data=form,
        **kw,
    )


def zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(zipfile.ZipInfo(name), content)
    return buf.getvalue()


def entries_of(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


@pytest.fixture
def fresh_client(tmp_path, fake_plaid):
    """A second, never-initialized FinTrack instance to restore into."""
    app = create_app(make_settings(tmp_path / "other"), fake_plaid)
    with SessionClient(app, base_url=BASE_URL, headers=CSRF) as c:
        yield c


# ------------------------------------------------------------------ backup


def test_backup_file_is_encrypted_and_opens_with_the_key(vault_with_data, settings, vault):
    r = download(vault_with_data)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["content-disposition"].startswith('attachment; filename="iron-owl-backup-')
    assert r.headers["content-disposition"].endswith('.ftbackup"')
    assert r.headers["cache-control"] == "no-store"

    entries = entries_of(r.content)
    assert set(entries) == {"fintrack.db", "keyfile.json", "manifest.json"}
    manifest = json.loads(entries["manifest.json"])
    assert manifest["format"] == 1 and manifest["schema_version"] == migrations.LATEST
    assert manifest["created_at"].endswith("Z")
    assert entries["keyfile.json"] == settings.keyfile_path.read_bytes()

    db_bytes = entries["fintrack.db"]
    assert not db_bytes.startswith(b"SQLite format 3")
    assert MARKER.encode() not in db_bytes and MARKER.encode("utf-16-le") not in db_bytes
    assert MARKER.encode() not in r.content

    # The copy opens with the vault key, derived from the backup's own keyfile + password.
    copy = settings.data_dir.parent / "copy.db"
    copy.write_bytes(db_bytes)
    key = read_keyfile(settings.data_dir / "keyfile.json").derive(PASSWORD)
    assert bytes(key) == bytes(vault.db.key)
    conn = dbmod.open_raw_connection(copy, key)
    try:
        assert conn.execute("SELECT name FROM accounts").fetchall() == [(MARKER,)]
        assert conn.execute("PRAGMA user_version").fetchone()[0] == migrations.LATEST
    finally:
        conn.close()
    assert not dbmod.verify_key(copy, bytearray(32))
    # No scratch files left behind in data/.
    assert not [p for p in settings.data_dir.iterdir() if p.name.startswith(".")]


def test_backup_wrong_password_counts_toward_limiter(vault_with_data, clock):
    for left in (4, 3, 2, 1):
        r = download(vault_with_data, "wrong password!!")
        assert r.status_code == 401
        assert r.json() == {"detail": "invalid password", "code": "wrong_password", "tries_left": left}
    r = download(vault_with_data, "wrong password!!")
    assert r.status_code == 429 and r.json()["retry_after"] == 30
    # The limiter is shared: even the right password (and unlock) must wait.
    assert download(vault_with_data).status_code == 429
    vault_with_data.post("/api/auth/lock")
    assert vault_with_data.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 429
    clock.advance(31)
    assert vault_with_data.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200
    assert download(vault_with_data).status_code == 200


def test_backup_requires_session_and_csrf(vault_with_data, client):
    raw = TestClient(vault_with_data.app, base_url=BASE_URL, cookies=dict(vault_with_data.cookies),
                     headers={SESSION_HEADER: vault_with_data.headers[SESSION_HEADER]})
    assert raw.post("/api/backup", json={"password": PASSWORD}).status_code == 403  # no X-FinTrack
    thief = TestClient(vault_with_data.app, base_url=BASE_URL, cookies=dict(vault_with_data.cookies), headers=CSRF)
    assert thief.post("/api/backup", json={"password": PASSWORD}).status_code == 401
    vault_with_data.post("/api/auth/lock")
    assert download(vault_with_data).status_code == 401


# ------------------------------------------------------------------ restore


def test_restore_into_uninitialized_vault_without_session(vault_with_data, fresh_client):
    backup = download(vault_with_data).content
    assert fresh_client.get("/api/auth/status").json()["initialized"] is False
    r = restore(fresh_client, backup)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True and r.json()["session_token"]
    assert "ft_session=" in r.headers["set-cookie"]
    st = fresh_client.get("/api/auth/status").json()
    assert (st["initialized"], st["unlocked"], st["auto_lock_minutes"]) == (True, True, 15)
    # The backup's recovery sheet came with it (restore at setup keeps a valid block).
    assert st["recovery"] == {"available": True, "sheet": json.loads(entries_of(backup)["manifest.json"])["recovery_sheet"]}
    assert [a["name"] for a in fresh_client.get("/api/accounts").json()] == [MARKER]
    # The restored vault is the backup's: same password works after a lock.
    fresh_client.post("/api/auth/lock")
    assert fresh_client.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200


def test_restore_without_session_is_refused_once_initialized(vault_with_data, settings):
    backup = download(vault_with_data).content
    anonymous = SessionClient(vault_with_data.app, base_url=BASE_URL, headers=CSRF)
    r = restore(anonymous, backup)
    assert r.status_code == 401 and r.json() == {"detail": "locked"}
    # Locked vault: still refused (restore needs an unlocked session once set up).
    vault_with_data.post("/api/auth/lock")
    assert restore(anonymous, backup).status_code == 401
    assert restore(vault_with_data, backup).status_code == 401


def test_restore_with_session_replaces_vault_and_keeps_the_old_one(vault_with_data, settings, fresh_client):
    # A different vault (different password and salt) to restore over this one.
    other_pw = "the other vault password"
    fresh_client.post("/api/auth/setup", json={"password": other_pw})
    fresh_client.post("/api/accounts", json={"name": "Other bank", "category": "bank", "current_balance": 1})
    backup = download(fresh_client, other_pw).content

    old_token = vault_with_data.headers[SESSION_HEADER]
    old_keyfile = settings.keyfile_path.read_bytes()
    # Replacing an existing vault needs its current password, not just a session.
    r = restore(vault_with_data, backup, password=other_pw)
    assert r.status_code == 422 and "current Iron Owl password" in r.json()["detail"]
    r = restore(vault_with_data, backup, password=other_pw, current_password="not the current one")
    assert r.status_code == 401 and r.json() == {
        "detail": "Your current Iron Owl password is incorrect.", "code": "wrong_password", "tries_left": 4,
    }
    assert [a["name"] for a in vault_with_data.get("/api/accounts").json()] != ["Other bank"]
    assert not [p for p in settings.data_dir.iterdir() if p.name.startswith("pre-restore-")]

    r = restore(vault_with_data, backup, password=other_pw, current_password=PASSWORD)
    assert r.status_code == 200, r.text
    assert vault_with_data.headers[SESSION_HEADER] != old_token
    assert [a["name"] for a in vault_with_data.get("/api/accounts").json()] == ["Other bank"]

    stale = TestClient(vault_with_data.app, base_url=BASE_URL, cookies=dict(vault_with_data.cookies),
                       headers={**CSRF, SESSION_HEADER: old_token})
    assert stale.get("/api/accounts").status_code == 401

    moved = [p for p in settings.data_dir.iterdir() if p.name.startswith("pre-restore-")]
    assert len(moved) == 1
    assert {p.name for p in moved[0].iterdir()} >= {"fintrack.db", "keyfile.json"}
    assert (moved[0] / "keyfile.json").read_bytes() == old_keyfile
    # The previous vault is intact and still opens with its own password.
    key = read_keyfile(moved[0] / "keyfile.json").derive(PASSWORD)
    assert dbmod.verify_key(moved[0] / "fintrack.db", key)
    vault_with_data.post("/api/auth/lock")
    assert vault_with_data.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 401
    assert vault_with_data.post("/api/auth/unlock", json={"password": other_pw}).status_code == 200


def test_restore_wrong_password_is_401_and_counted(vault_with_data, fresh_client, clock):
    backup = download(vault_with_data).content
    for left in (4, 3, 2, 1):
        r = restore(fresh_client, backup, password="not the password")
        assert r.status_code == 401 and r.json() == {
            "detail": "That password doesn't open this backup.", "code": "wrong_password", "tries_left": left,
        }
    r = restore(fresh_client, backup, password="not the password")
    assert r.status_code == 429
    assert restore(fresh_client, backup).status_code == 429
    assert fresh_client.get("/api/auth/status").json()["initialized"] is False
    clock.advance(31)
    assert restore(fresh_client, backup).status_code == 200


def test_restore_csrf(vault_with_data, fresh_client):
    backup = download(vault_with_data).content
    app = fresh_client.app
    no_header = TestClient(app, base_url=BASE_URL)
    r = restore(no_header, backup)
    assert r.status_code == 403 and r.json() == {"detail": "forbidden"}
    r = restore(fresh_client, backup, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    r = restore(fresh_client, backup, headers={"X-FinTrack": "0"})
    assert r.status_code == 403
    assert fresh_client.get("/api/auth/status").json()["initialized"] is False
    assert restore(fresh_client, backup, headers={"Origin": "http://127.0.0.1:8000"}).status_code == 200


def test_multipart_is_refused_on_every_other_route(unlocked):
    files = {"file": ("x.txt", b"x", "text/plain")}
    for path in ("/api/accounts", "/api/auth/unlock", "/api/backup", "/api/plaid/sync", "/api/rules"):
        assert unlocked.post(path, files=files, data={"password": PASSWORD}).status_code == 403, path
    # Other form-encodable (no-preflight) content types are refused too.
    form = unlocked.post("/api/auth/unlock", content=b"password=x", headers={"content-type": "application/x-www-form-urlencoded"})
    assert form.status_code == 403
    text = unlocked.post("/api/auth/unlock", content=b'{"password":"x"}', headers={"content-type": "text/plain"})
    assert text.status_code == 403
    assert unlocked.post("/api/auth/lock").status_code == 200  # no body at all is fine


@pytest.mark.parametrize(
    "evil",
    ["../evil.txt", "../../evil.txt", "/evil.txt", "C:/evil.txt", "..\\evil.txt", "sub/../../evil.txt",
     "fintrack.db/../../evil.txt"],
)
def test_restore_rejects_zip_slip(vault_with_data, fresh_client, tmp_path, evil):
    good = entries_of(download(vault_with_data).content)
    data = zip_bytes({**good, evil: b"pwned"})
    r = restore(fresh_client, data)
    assert r.status_code == 400
    assert fresh_client.get("/api/auth/status").json()["initialized"] is False
    assert not list(tmp_path.rglob("evil.txt"))


@pytest.mark.parametrize(
    ("mutate", "detail"),
    [
        (lambda e: {"x": b"not a backup"}, None),
        (lambda e: {k: v for k, v in e.items() if k != "keyfile.json"}, "incomplete"),
        (lambda e: {**e, "manifest.json": b'{"format": 2, "schema_version": 2}'}, "format"),
        (lambda e: {**e, "manifest.json": b'{"format": 1, "schema_version": 99}'}, "newer version"),
        (lambda e: {**e, "manifest.json": b"not json"}, "manifest"),
        (lambda e: {**e, "keyfile.json": b'{"version":1,"kdf":"argon2id","salt":"AAAA","time_cost":999,'
                                          b'"memory_cost":65536,"parallelism":4}'}, "keyfile"),
        (lambda e: {**e, "fintrack.db": b"SQLite format 3\x00" + b"\x00" * 100}, "not encrypted"),
    ],
)
def test_restore_rejects_bad_files(vault_with_data, fresh_client, mutate, detail):
    good = entries_of(download(vault_with_data).content)
    r = restore(fresh_client, zip_bytes(mutate(good)))
    assert r.status_code == 400
    if detail:
        assert detail in r.json()["detail"]
    assert fresh_client.get("/api/auth/status").json()["initialized"] is False


def test_restore_rejects_non_zip_and_bad_forms(fresh_client, settings):
    assert restore(fresh_client, b"definitely not a zip").status_code == 400
    r = fresh_client.post("/api/restore", files={"file": ("b", b"x", "application/octet-stream")})
    assert r.status_code == 400  # no password
    r = fresh_client.post("/api/restore", files={"file": ("b", b"x", "application/octet-stream"),
                                                 "other": ("c", b"y", "application/octet-stream")},
                          data={"password": PASSWORD})
    assert r.status_code == 400
    r = fresh_client.post("/api/restore", json={"password": PASSWORD})
    assert r.status_code == 400


def test_restore_wrong_db_for_keyfile(vault_with_data, fresh_client):
    other_pw = "the other vault password"
    fresh_client.post("/api/auth/setup", json={"password": other_pw})
    mixed = {**entries_of(download(vault_with_data).content),
             "keyfile.json": entries_of(download(fresh_client, other_pw).content)["keyfile.json"]}
    fresh_client.post("/api/auth/lock")
    # PASSWORD with the other vault's salt derives a key that doesn't open this DB.
    r = restore(SessionClient(fresh_client.app, base_url=BASE_URL, headers=CSRF), zip_bytes(mixed))
    assert r.status_code == 401


def test_restore_rejects_oversize_uploads(vault_with_data, fresh_client, monkeypatch):
    backup = download(vault_with_data).content
    monkeypatch.setattr(backup_service, "MAX_UPLOAD_BYTES", len(backup) - 1)
    r = restore(fresh_client, backup)
    assert r.status_code == 413
    monkeypatch.setattr(backup_service, "MAX_UPLOAD_BYTES", 10 * len(backup))
    monkeypatch.setattr(backup_service, "MAX_REQUEST_BYTES", 1000)
    r = restore(fresh_client, backup)  # Content-Length checked before reading the body
    assert r.status_code == 413
    assert fresh_client.get("/api/auth/status").json()["initialized"] is False
    assert not [p for p in fresh_client.app.state.fintrack.settings.data_dir.iterdir() if p.name.startswith(".")]


def test_restore_zip_bomb_entry_is_capped(vault_with_data, fresh_client, monkeypatch):
    good = entries_of(download(vault_with_data).content)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("fintrack.db", good["fintrack.db"])
        zf.writestr("keyfile.json", good["keyfile.json"])
        zf.writestr("manifest.json", b" " * (200 * 1024) + good["manifest.json"])  # > 64 KB inflated
    r = restore(fresh_client, buf.getvalue())
    assert r.status_code == 400 and "too large" in r.json()["detail"]


def test_restore_runs_migrations_on_a_release1_backup(tmp_path, fresh_client):
    from tests.test_migrations import build_r1_vault

    r1 = make_settings(tmp_path / "r1")
    build_r1_vault(r1)
    data = zip_bytes({
        "fintrack.db": r1.db_path.read_bytes(),
        "keyfile.json": r1.keyfile_path.read_bytes(),
        "manifest.json": json.dumps({"format": 1, "created_at": "2026-01-01T00:00:00Z", "schema_version": 0}).encode(),
    })
    r = restore(fresh_client, data)
    assert r.status_code == 200, r.text
    txns = {t["name"]: t for t in fresh_client.get("/api/transactions").json()["items"]}
    assert txns["Coffee"]["plaid_category"] == "FOOD_AND_DRINK"
    assert len(fresh_client.get("/api/categories").json()) >= 17


def test_wrong_current_password_counts_toward_the_limiter(vault_with_data, fresh_client, clock):
    fresh_client.post("/api/auth/setup", json={"password": "the other vault password"})
    backup = download(fresh_client, "the other vault password").content
    for _ in range(4):
        assert restore(vault_with_data, backup, password="the other vault password",
                       current_password="guess").status_code == 401
    assert restore(vault_with_data, backup, password="the other vault password",
                   current_password="guess").status_code == 429


def test_previous_vaults_list_and_delete(vault_with_data, settings, fresh_client):
    fresh_client.post("/api/auth/setup", json={"password": "the other vault password"})
    backup = download(fresh_client, "the other vault password").content
    assert vault_with_data.get("/api/backup/previous").json() == []
    r = restore(vault_with_data, backup, password="the other vault password", current_password=PASSWORD)
    assert r.status_code == 200

    previous = vault_with_data.get("/api/backup/previous").json()
    assert len(previous) == 1 and previous[0]["id"].startswith("pre-restore-")
    assert previous[0]["size_bytes"] > 0 and "T" in previous[0]["created_at"]
    pid = previous[0]["id"]

    # Deleting needs the (now current = restored) vault password; the old one no longer works.
    r = vault_with_data.post(f"/api/backup/previous/{pid}/delete", json={"password": PASSWORD})
    assert r.status_code == 401
    assert (settings.data_dir / pid).is_dir()
    for bad in ("..", "pre-restore-x", "fintrack.db", "pre-restore-20260101-000000%2F..%2F.."):
        r = vault_with_data.post(f"/api/backup/previous/{bad}/delete", json={"password": "the other vault password"})
        assert r.status_code in (404, 405), (bad, r.status_code)
    r = vault_with_data.post(f"/api/backup/previous/{pid}/delete", json={"password": "the other vault password"})
    assert r.status_code == 204
    assert not (settings.data_dir / pid).exists()
    assert vault_with_data.get("/api/backup/previous").json() == []
    assert settings.db_path.exists()  # the live vault is untouched


def test_previous_vault_routes_need_a_session(fresh_client):
    assert fresh_client.get("/api/backup/previous").status_code == 401
    r = fresh_client.post("/api/backup/previous/pre-restore-20260101-000000/delete", json={"password": "x" * 12})
    assert r.status_code == 401


def test_stale_staging_folders_are_removed_at_startup(settings, fake_plaid):
    from app.main import create_app

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    stale = settings.data_dir / ".restore-0123456789abcdef"
    stale.mkdir()
    (stale / "upload.zip").write_bytes(b"x")
    keep = settings.data_dir / "pre-restore-20260101-000000"
    keep.mkdir()
    with TestClient(create_app(settings, fake_plaid), base_url=BASE_URL):
        pass
    assert not stale.exists()
    assert keep.is_dir()  # only scratch folders are cleaned; previous vaults stay until deleted

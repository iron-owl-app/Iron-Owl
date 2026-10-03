"""Adversarial review of Release 2: regressions for the findings of the security pass."""
from __future__ import annotations

import csv
import datetime as dt
import io
import struct
import zipfile

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app import db as dbmod
from app import migrations
from app.models import Base
from app.security import read_keyfile
from app.services import backup as backup_service
from tests.conftest import BASE_URL, CSRF, PASSWORD, add_txn, make_account
from tests.test_backup import download, entries_of, fresh_client, restore, vault_with_data, zip_bytes  # noqa: F401

# ------------------------------------------------------------------ every route needs a session

# Routes that must work without a session (restore only while uninitialized, tested elsewhere).
PUBLIC_ROUTES = {
    ("GET", "/api/auth/status"),
    ("POST", "/api/auth/setup"),
    ("POST", "/api/auth/unlock"),
    ("POST", "/api/auth/lock"),
    ("POST", "/api/restore"),
    ("POST", "/api/auth/recover/check"),
    ("POST", "/api/auth/recover"),
}


def _api_routes():
    from app.routers import (
        accounts, alerts, auth, backup, budgets, categories, category_groups, goals, holdings, home, investments,
        loans, plaid, plaid_keys, recovery, recurring, reports, rules, summary, tags, transactions,
    )

    for module in (accounts, alerts, auth, backup, budgets, categories, goals, holdings, plaid, recurring, rules,
                   summary, transactions, category_groups, tags, reports, loans, plaid_keys, home, recovery, investments):
        yield from module.router.routes


def test_every_api_route_requires_a_session(unlocked):
    app = unlocked.app
    anon = TestClient(app, base_url=BASE_URL, headers=CSRF)  # CSRF header, but no cookie or token
    checked = 0
    for route in _api_routes():
        if not isinstance(route, APIRoute) or not route.path.startswith("/api"):
            continue
        for method in route.methods - {"HEAD", "OPTIONS"}:
            if (method, route.path) in PUBLIC_ROUTES:
                continue
            path = route.path
            for name in route.param_convertors:
                path = path.replace("{" + name + "}", "1")
            r = anon.request(method, path, json={} if method != "GET" else None)
            assert r.status_code == 401, (method, route.path, r.status_code, r.text)
            checked += 1
    assert checked > 60


def test_restore_route_refuses_anonymous_callers_once_initialized(unlocked):
    anon = TestClient(unlocked.app, base_url=BASE_URL, headers=CSRF)
    r = anon.post("/api/restore", files={"file": ("x", b"x")}, data={"password": "x"})
    assert r.status_code == 401


# ------------------------------------------------------------------ CSV export


@pytest.mark.parametrize(
    "text",
    [
        " =cmd|' /C calc'!A0",     # leading space before a formula
        " =1+1",              # non-breaking space
        "\n=1+1",                  # leading newline
        "|cmd",                    # DDE pipe
        "%0A=1",                   # percent
        "＝1+1",               # fullwidth equals (normalized by some spreadsheet locales)
        "＠SUM(A1)",           # fullwidth at
        "  \t-2+3",
    ],
)
def test_csv_injection_guard_covers_whitespace_and_dde_prefixes(unlocked, db, fixed_today, text):
    acct = make_account(db, "Everyday", "bank", 10)
    add_txn(db, acct.id, "2026-09-02", -1, text, merchant=text, category="OTHER")
    r = unlocked.get("/api/transactions/export.csv")
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8"))))
    name, merchant = rows[1][2], rows[1][3]
    assert name == "'" + text and merchant == "'" + text


def test_csv_guard_leaves_ordinary_text_alone(unlocked, db, fixed_today):
    acct = make_account(db, "Everyday", "bank", 10)
    add_txn(db, acct.id, "2026-09-02", -1, "Blue Bottle 50% off", merchant=" Blue Bottle", category="OTHER")
    rows = list(csv.reader(io.StringIO(unlocked.get("/api/transactions/export.csv").text)))
    assert rows[1][2] == "Blue Bottle 50% off" and rows[1][3] == " Blue Bottle"
    assert rows[1][5] == "-1.00"


# ------------------------------------------------------------------ goals: stored values must not break listing


def test_goal_projection_beyond_the_calendar_is_never_not_a_500(unlocked, fixed_today, db):
    # D8: goals are Budget categories; stored extremes (an old goal, a tiny plan toward a huge
    # target, a due month long past) must still list.
    from app.models import Goal

    db.add_all([
        Goal(name="Moon", kind="save", target_cents=100_000_000_000_000, monthly_cents=1, hue=1),
        Goal(name="Mortgage", kind="save", target_cents=100_000_000_000_000, monthly_cents=1,
             target_date=dt.date(1990, 1, 1), hue=2),
        Goal(name="Fund", kind="emergency", target_cents=100_000_000_000_000, monthly_cents=1, hue=3),
    ])
    db.commit()
    r = unlocked.get("/api/goals")
    assert r.status_code == 200, r.text
    goals = {g["name"]: g for g in r.json()["goals"]}
    assert goals["Moon"]["projected_month"] is None and goals["Fund"]["projected_month"] is None
    assert goals["Mortgage"]["status"] == "behind" and goals["Mortgage"]["months_to_save"] == 1


# ------------------------------------------------------------------ months and big integers


@pytest.mark.parametrize("url", ["/api/spending/review?month=0001-01", "/api/budgets?month=0001-01"])
def test_ancient_months_are_rejected_not_500(unlocked, fixed_today, url):
    assert unlocked.get(url).status_code == 422


def test_ancient_budget_month_cannot_be_saved(unlocked, fixed_today):
    assert unlocked.put("/api/budgets/0001-01", json={"assigned": {"FOOD_AND_DRINK": 0}}).status_code == 422
    assert unlocked.put("/api/budgets/1899-12", json={"assigned": {"FOOD_AND_DRINK": 0}}).status_code == 422


@pytest.mark.parametrize(
    "method,url",
    [
        ("GET", "/api/transactions?offset=100000000000000000000"),
        ("GET", "/api/transactions?account_id=100000000000000000000"),
        ("GET", "/api/transactions/export.csv?account_id=100000000000000000000"),
        ("PATCH", "/api/transactions/100000000000000000000"),
        ("PATCH", "/api/rules/100000000000000000000"),
        ("DELETE", "/api/goals/100000000000000000000"),
    ],
)
def test_out_of_range_integers_are_422_not_500(unlocked, method, url):
    r = unlocked.request(method, url, json={} if method != "GET" else None)
    assert r.status_code == 422, r.text


# ------------------------------------------------------------------ restore: a bad DB is a bad file


def _with_user_version(backup: bytes, tmp_path, version: int) -> bytes:
    entries = entries_of(backup)
    path = tmp_path / "staged.db"
    path.write_bytes(entries["fintrack.db"])
    keyfile = tmp_path / "keyfile.json"
    keyfile.write_bytes(entries["keyfile.json"])
    conn = dbmod.open_raw_connection(path, read_keyfile(keyfile).derive(PASSWORD))
    conn.execute(f"PRAGMA user_version = {int(version)}")
    conn.commit()
    conn.close()
    entries["fintrack.db"] = path.read_bytes()
    return zip_bytes(entries)


def test_restore_of_a_db_that_fails_to_migrate_is_400_and_harmless(vault_with_data, settings, tmp_path):
    data = _with_user_version(download(vault_with_data).content, tmp_path, 99)
    r = restore(vault_with_data, data, current_password=PASSWORD)
    assert r.status_code == 400, r.text
    # The current vault was never touched: still unlocked, same data, nothing moved aside.
    assert vault_with_data.get("/api/accounts").json()[0]["name"] == "Zebra Credit Union Marker"
    assert not [p for p in settings.data_dir.iterdir() if p.name.startswith(("pre-restore-", "."))]


def test_restore_into_fresh_vault_with_bad_db_leaves_it_uninitialized(vault_with_data, fresh_client, tmp_path):
    data = _with_user_version(download(vault_with_data).content, tmp_path, 99)
    assert restore(fresh_client, data).status_code == 400
    assert fresh_client.get("/api/auth/status").json()["initialized"] is False
    data_dir = fresh_client.app.state.fintrack.settings.data_dir
    assert [p.name for p in data_dir.iterdir()] == []


def test_restore_is_refused_if_the_session_ended_during_the_upload(vault_with_data, settings, monkeypatch):
    backup = download(vault_with_data).content
    vault = vault_with_data.app.state.fintrack.vault
    real = backup_service.extract_backup

    def lock_meanwhile(*args, **kwargs):
        vault.lock()  # e.g. "Lock" clicked in another tab while the file was uploading
        return real(*args, **kwargs)

    monkeypatch.setattr(backup_service, "extract_backup", lock_meanwhile)
    r = restore(vault_with_data, backup)
    assert r.status_code == 401
    assert not [p for p in settings.data_dir.iterdir() if p.name.startswith("pre-restore-")]


# ------------------------------------------------------------------ restore: zip central-directory bomb


def _central_directory_bomb(entries: int) -> bytes:
    """A tiny zip whose central directory lists ``entries`` records (all pointing at one file)."""
    local = struct.pack("<4sHHHHHIIIHH", b"PK\x03\x04", 20, 0, 0, 0, 0, 0, 0, 0, 1, 0) + b"a"
    record = struct.pack("<4sHHHHHHIIIHHHHHII", b"PK\x01\x02", 20, 20, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0) + b"a"
    cd = record * entries
    eocd = struct.pack("<4sHHHHIIH", b"PK\x05\x06", 0, 0, min(entries, 0xFFFF), min(entries, 0xFFFF),
                       len(cd), len(local), 0)
    return local + cd + eocd


def test_restore_refuses_huge_central_directory_before_parsing_it(fresh_client, monkeypatch):
    parsed = []
    real = zipfile.ZipFile

    class Spy(real):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            parsed.append(args)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(backup_service.zipfile, "ZipFile", Spy)
    r = restore(fresh_client, _central_directory_bomb(20_000))
    assert r.status_code == 400
    assert parsed == []  # rejected from the end-of-central-directory record alone


def test_normal_backup_still_passes_the_central_directory_check(vault_with_data, fresh_client):
    assert restore(fresh_client, download(vault_with_data).content).status_code == 200


# ------------------------------------------------------------------ migrations: interrupted fresh setup


def test_fresh_schema_interrupted_before_stamping_still_opens(tmp_path):
    """create_all ran but the process died before user_version was stamped."""
    key = bytearray(range(32))
    path = tmp_path / "fintrack.db"
    import sqlcipher3

    conn = sqlcipher3.connect(str(path))
    conn.execute(dbmod._key_pragma(key))
    conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    conn.close()
    engine = create_engine("sqlite://", creator=lambda: dbmod.open_raw_connection(path, key))
    Base.metadata.create_all(engine)  # latest schema, user_version still 0
    assert migrations.migrate(engine) == migrations.LATEST
    with engine.connect() as c:
        assert c.exec_driver_sql("SELECT count(*) FROM categories").scalar() >= 17
        assert c.exec_driver_sql("PRAGMA user_version").scalar() == migrations.LATEST
    engine.dispose()


# ------------------------------------------------------------------ CSRF: content-type tricks


@pytest.mark.parametrize(
    "content_type",
    [
        "MULTIPART/FORM-DATA; boundary=x",
        " multipart/form-data ; boundary=x",
        "multipart/mixed; boundary=x",
        "text/plain; charset=application/json",
        "application/json, text/plain",
        "application/x-www-form-urlencoded; x=application/json",
    ],
)
def test_form_encodable_content_types_refused_off_the_restore_route(unlocked, content_type):
    for path in ("/api/rules", "/api/auth/change-password", "/api/backup", "/api/restore/"):
        r = unlocked.post(path, content=b"--x--", headers={"content-type": content_type})
        assert r.status_code == 403, (path, content_type, r.status_code)


@pytest.mark.parametrize("content_type", ["multipart/mixed; boundary=x", "text/plain", "application/x-www-form-urlencoded"])
def test_restore_accepts_only_multipart_form_data(fresh_client, content_type):
    r = fresh_client.post("/api/restore", content=b"--x--", headers={"content-type": content_type})
    assert r.status_code == 403


def test_restore_with_json_or_no_content_type_is_a_bad_upload(fresh_client):
    assert fresh_client.post("/api/restore", json={"password": "x"}).status_code == 400
    assert fresh_client.post("/api/restore", content=b"x").status_code == 400
    assert fresh_client.get("/api/auth/status").json()["initialized"] is False


def test_method_override_headers_do_not_bypass_csrf(unlocked):
    anon = TestClient(unlocked.app, base_url=BASE_URL)  # no X-FinTrack header
    for headers in ({"X-HTTP-Method-Override": "GET"}, {"X-Method-Override": "GET"}):
        assert anon.post("/api/alerts/events/read", headers=headers).status_code == 403
    assert anon.post("/api/alerts/events/read?_method=GET").status_code == 403

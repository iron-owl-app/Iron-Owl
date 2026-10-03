"""Migration v4: a real Release 2.1 (schema v3) vault with rows is upgraded in place."""
from __future__ import annotations

import pytest
import sqlcipher3

from app import db as dbmod
from app import migrations
from tests.conftest import PASSWORD
from tests.test_migrations import REAL_LATEST, build_v2_vault, columns, raw


def build_v3_vault(settings, monkeypatch) -> bytearray:
    """A schema-v3 vault holding envelope-budget rows, snapshots and settings."""
    key = build_v2_vault(settings, monkeypatch)
    monkeypatch.setattr(migrations, "LATEST", 3)
    database = dbmod.Database(settings.db_path)
    database.open(bytearray(key))
    database.close()
    monkeypatch.setattr(migrations, "LATEST", REAL_LATEST)
    conn = raw(settings, key)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert "group_id" not in columns(conn, "categories")
        conn.execute("INSERT INTO budgets (month, category, limit_cents, removed, restart) "
                     "VALUES ('2026-09', 'MEDICAL', 1500, 0, 1)")
        conn.execute("INSERT INTO balance_snapshots (account_id, date, balance_cents) VALUES (1, '2026-08-01', 120000)")
        conn.execute("""INSERT INTO app_settings ("key", value) VALUES ('budget_account_ids', '{"exclude": [], "include": [2]}')""")
        conn.execute("""INSERT INTO alert_events ("key", severity, title, body, created_at, read, dedupe_key, cleared)
                        VALUES ('low', 'neg', 'Checking is low', 'Balance $1', '2026-09-01 00:00:00', 0, 'low:1:2026-09-01', 0)""")
        conn.execute("UPDATE accounts SET category = 'loan', minimum_payment_cents = 5000 WHERE id = 2")
        conn.commit()
    finally:
        conn.close()
    return key


def test_v3_vault_with_rows_is_upgraded(settings, client, monkeypatch, fixed_today):
    key = build_v3_vault(settings, monkeypatch)
    assert client.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200

    # Existing data works with the Release 3 shape.
    cats = {c["id"]: c for c in client.get("/api/categories").json()}
    assert cats["FOOD_AND_DRINK"]["group_id"] is None and cats["FOOD_AND_DRINK"]["target"] is None
    assert cats["PETS"]["kind"] == "spending"  # added by v2, kept
    txns = client.get("/api/transactions").json()["items"]
    assert len(txns) == 5 and all(t["splits"] == [] and t["tags"] == [] for t in txns)
    history = client.get("/api/accounts/1/history", params={"days": 3650}).json()
    assert history == [{"date": "2026-08-01", "balance": 1200.0, "estimated": False},
                       {"date": "2026-09-01", "balance": 1500.0, "estimated": False}]
    accounts = {a["id"]: a for a in client.get("/api/accounts").json()}
    assert accounts[1]["loan_group"] is None and accounts[2]["loan_group"] == "Other loans"
    sep = client.get("/api/budgets", params={"month": "2026-09"}).json()
    lines = {c["category"]: c for c in sep["categories"]}
    assert lines["MEDICAL"]["assigned"] == 15.0 and lines["FOOD_AND_DRINK"]["available"] == 695.5
    assert sep["groups"] == [] and sep["needed_total"] == 0.0
    assert [s["key"] for s in client.get("/api/alerts/settings").json()] == [
        "low", "big", "budget", "due", "newrec", "price", "conn", "income",
        "low_ahead", "reminder"]
    assert "Checking is low" in [e["title"] for e in client.get("/api/alerts/events").json()]
    # The new features work on the upgraded vault.
    g = client.post("/api/category-groups", json={"name": "Food"}).json()
    assert client.patch("/api/categories/FOOD_AND_DRINK", json={"group_id": g["id"]}).status_code == 200
    tag = client.post("/api/tags", json={"name": "Coffee"}).json()
    coffee = next(t for t in txns if t["name"] == "Coffee")
    assert client.put(f"/api/transactions/{coffee['id']}/tags", json={"tag_ids": [tag["id"]]}).status_code == 200
    r = client.put(f"/api/transactions/{coffee['id']}/splits", json={"splits": [
        {"amount": -4, "category": "FOOD_AND_DRINK"}, {"amount": -0.5, "category": "OTHER"}]})
    assert r.status_code == 200
    client.post("/api/auth/lock")

    conn = raw(settings, key)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == migrations.LATEST == 8
        assert conn.execute("SELECT enabled, value FROM alert_settings WHERE \"key\" = 'income'").fetchone() == (1, None)
        assert conn.execute("SELECT count(*) FROM balance_snapshots WHERE estimated != 0").fetchone()[0] == 0
        assert columns(conn, "balance_snapshots")["estimated"][0] == 1  # NOT NULL
        assert conn.execute("SELECT month, category, limit_cents, removed, restart FROM budgets "
                            "WHERE category = 'MEDICAL'").fetchone() == ("2026-09", "MEDICAL", 1500, 0, 1)
        assert conn.execute("SELECT value FROM app_settings WHERE \"key\" = 'budget_account_ids'").fetchone()[0] == (
            '{"exclude": [], "include": [2]}')
        fk = {(r[2], r[3], r[4], r[6]) for r in conn.execute("PRAGMA foreign_key_list(categories)")}
        assert ("category_groups", "group_id", "id", "SET NULL") in fk

        # The upgraded schema matches a fresh create_all schema, column for column.
        fresh_settings = settings.model_copy(update={"data_dir": settings.data_dir.parent / "fresh"})
        fresh_settings.data_dir.mkdir()
        fresh = dbmod.Database(fresh_settings.db_path)
        fresh.open(bytearray(key), create_schema=True)
        fresh.close()
        fconn = sqlcipher3.connect(str(fresh_settings.db_path))
        fconn.execute(dbmod._key_pragma(key))
        try:
            names = "SELECT name FROM sqlite_master WHERE type='{}' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            assert [r[0] for r in fconn.execute(names.format("table"))] == [r[0] for r in conn.execute(names.format("table"))]
            assert [r[0] for r in fconn.execute(names.format("index"))] == [r[0] for r in conn.execute(names.format("index"))]
            for (table,) in fconn.execute(names.format("table")).fetchall():
                assert {c: v[0] for c, v in columns(fconn, table).items()} == {
                    c: v[0] for c, v in columns(conn, table).items()}, table
            assert dict(fconn.execute('SELECT "key", value FROM alert_settings').fetchall())["income"] is None
        finally:
            fconn.close()
    finally:
        conn.close()


def test_failed_v4_migration_rolls_back_to_v3(settings, monkeypatch):
    key = build_v3_vault(settings, monkeypatch)
    monkeypatch.setattr(migrations, "_V4_ALTERS", migrations._V4_ALTERS + ("ALTER TABLE nope ADD COLUMN x TEXT",))
    database = dbmod.Database(settings.db_path)
    with pytest.raises(sqlcipher3.OperationalError):
        database.open(bytearray(key))
    conn = raw(settings, key)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert conn.execute("SELECT count(*) FROM sqlite_master WHERE name = 'category_groups'").fetchone()[0] == 0
        assert "estimated" not in columns(conn, "balance_snapshots")
        assert "loan_group" not in columns(conn, "accounts")
    finally:
        conn.close()
    monkeypatch.setattr(migrations, "_V4_ALTERS", migrations._V4_ALTERS[:-1])
    database.open(bytearray(key))
    database.close()
    conn = raw(settings, key)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == migrations.LATEST
        assert conn.execute("SELECT count(*) FROM budgets").fetchone()[0] == 7
    finally:
        conn.close()


def test_backup_manifest_is_latest(unlocked):
    import io
    import json
    import zipfile

    r = unlocked.post("/api/backup", json={"password": PASSWORD})
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert json.loads(zf.read("manifest.json"))["schema_version"] == migrations.LATEST

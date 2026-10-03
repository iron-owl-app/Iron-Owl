"""Schema migrations: a real Release 1 database is upgraded in place (PRAGMA user_version)."""
from __future__ import annotations

import pytest
import sqlcipher3

from app import db as dbmod
from app import migrations
from app.security import KeyParams, write_json_atomic
from app.seeds import SEED_CATEGORIES, fnv1a_hue
from tests.conftest import PASSWORD

# Release 1 schema exactly as create_all produced it (generated from the R1 models).
R1_SCHEMA = (
    """CREATE TABLE plaid_items (
        id INTEGER NOT NULL, plaid_item_id VARCHAR NOT NULL, access_token VARCHAR NOT NULL,
        institution_id VARCHAR, institution_name VARCHAR, kind VARCHAR NOT NULL,
        transactions_cursor TEXT, status VARCHAR NOT NULL, error_code VARCHAR,
        last_synced_at DATETIME, created_at DATETIME NOT NULL,
        PRIMARY KEY (id), UNIQUE (plaid_item_id))""",
    """CREATE TABLE accounts (
        id INTEGER NOT NULL, item_id INTEGER, plaid_account_id VARCHAR, source VARCHAR NOT NULL,
        name VARCHAR NOT NULL, official_name VARCHAR, mask VARCHAR, institution_name VARCHAR,
        category VARCHAR NOT NULL, plaid_type VARCHAR, plaid_subtype VARCHAR,
        current_balance_cents INTEGER NOT NULL, available_balance_cents INTEGER,
        currency VARCHAR NOT NULL, interest_rate FLOAT, minimum_payment_cents INTEGER,
        next_payment_due DATE, notes TEXT, hidden BOOLEAN NOT NULL, updated_at DATETIME NOT NULL,
        created_at DATETIME NOT NULL, PRIMARY KEY (id),
        FOREIGN KEY(item_id) REFERENCES plaid_items (id) ON DELETE CASCADE, UNIQUE (plaid_account_id))""",
    "CREATE INDEX ix_accounts_item_id ON accounts (item_id)",
    """CREATE TABLE balance_snapshots (
        id INTEGER NOT NULL, account_id INTEGER NOT NULL, date DATE NOT NULL,
        balance_cents INTEGER NOT NULL, PRIMARY KEY (id),
        CONSTRAINT uq_snapshot_account_date UNIQUE (account_id, date),
        FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE)""",
    "CREATE INDEX ix_balance_snapshots_account_id ON balance_snapshots (account_id)",
    """CREATE TABLE holdings (
        id INTEGER NOT NULL, account_id INTEGER NOT NULL, security_id VARCHAR NOT NULL,
        name VARCHAR NOT NULL, ticker VARCHAR, quantity FLOAT NOT NULL, price FLOAT,
        value_cents INTEGER NOT NULL, cost_basis_cents INTEGER, PRIMARY KEY (id),
        FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE)""",
    "CREATE INDEX ix_holdings_account_id ON holdings (account_id)",
    """CREATE TABLE transactions (
        id INTEGER NOT NULL, account_id INTEGER NOT NULL, plaid_transaction_id VARCHAR NOT NULL,
        date DATE NOT NULL, name VARCHAR NOT NULL, merchant_name VARCHAR,
        amount_cents INTEGER NOT NULL, category VARCHAR, pending BOOLEAN NOT NULL,
        PRIMARY KEY (id), FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE,
        UNIQUE (plaid_transaction_id))""",
    "CREATE INDEX ix_transactions_account_id ON transactions (account_id)",
    "CREATE INDEX ix_transactions_date ON transactions (date)",
)

NOW = "2026-09-01 12:00:00.000000"
REAL_LATEST = migrations.LATEST


def build_r1_vault(settings, password: str = PASSWORD) -> bytearray:
    """Create data/keyfile.json + a Release 1 fintrack.db (user_version 0) with rows."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    params = KeyParams.new()
    write_json_atomic(settings.keyfile_path, params.to_json())
    key = params.derive(password)
    conn = sqlcipher3.connect(str(settings.db_path))
    conn.execute(dbmod._key_pragma(key))
    for stmt in R1_SCHEMA:
        conn.execute(stmt)
    conn.execute(
        "INSERT INTO plaid_items VALUES (1, 'item_bank', 'access-sandbox-r1', 'ins_1', 'First Bank', 'bank', "
        "'cursor-7', 'ok', NULL, ?, ?)", (NOW, NOW),
    )
    conn.execute(
        "INSERT INTO accounts VALUES (1, 1, 'chk', 'plaid', 'Checking', NULL, '0000', 'First Bank', 'bank', "
        "'depository', 'checking', 150000, NULL, 'USD', NULL, NULL, NULL, NULL, 0, ?, ?)", (NOW, NOW),
    )
    conn.execute(
        "INSERT INTO accounts VALUES (2, NULL, NULL, 'manual', 'Cash', NULL, NULL, NULL, 'other', "
        "NULL, NULL, 5000, NULL, 'USD', NULL, NULL, NULL, 'jar', 0, ?, ?)", (NOW, NOW),
    )
    conn.execute("INSERT INTO balance_snapshots VALUES (1, 1, '2026-09-01', 150000)")
    rows = [
        (1, "t1", "2026-09-01", "Coffee", "Blue Bottle", -450, "FOOD_AND_DRINK"),
        (2, "t2", "2026-09-02", "Payroll", None, 250000, "INCOME"),
        (3, "t3", "2026-09-03", "Vet", None, -8000, "PETS"),  # unknown primary
        (4, "t4", "2026-09-04", "Mystery", None, -100, None),
        (5, "t5", "2026-09-05", "Odd", None, -100, "not a primary"),
    ]
    for tid, ptid, day, name, merchant, cents, cat in rows:
        conn.execute(
            "INSERT INTO transactions VALUES (?, 1, ?, ?, ?, ?, ?, ?, 0)",
            (tid, ptid, day, name, merchant, cents, cat),
        )
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()
    return key


def raw(settings, key):
    conn = sqlcipher3.connect(str(settings.db_path))
    conn.execute(dbmod._key_pragma(key))
    return conn


def columns(conn, table: str) -> dict[str, tuple]:
    return {r[1]: (r[3], r[4]) for r in conn.execute(f"PRAGMA table_info({table})")}  # name -> (notnull, default)


def test_release1_database_is_upgraded(settings, client):
    key = build_r1_vault(settings)
    r = client.post("/api/auth/unlock", json={"password": PASSWORD})
    assert r.status_code == 200, r.text

    # Data survived and gained Release 2 fields.
    txns = {t["name"]: t for t in client.get("/api/transactions").json()["items"]}
    assert txns["Coffee"]["plaid_category"] == "FOOD_AND_DRINK"
    assert txns["Coffee"]["category"] == "FOOD_AND_DRINK"
    assert txns["Coffee"]["category_name"] == "Food and drink" and txns["Coffee"]["category_hue"] == 25
    assert txns["Coffee"]["category_source"] == "plaid"
    assert txns["Coffee"]["is_transfer"] is False and txns["Coffee"]["notes"] is None
    assert txns["Coffee"]["rule_id"] is None
    assert txns["Mystery"]["category"] is None and txns["Mystery"]["category_name"] == "Other"
    assert txns["Odd"]["category"] is None and txns["Odd"]["plaid_category"] is None
    assert txns["Vet"]["category"] == "PETS" and txns["Vet"]["category_name"] == "Pets"
    assert txns["Vet"]["category_hue"] == fnv1a_hue("PETS")

    cats = {c["id"]: c for c in client.get("/api/categories").json()}
    assert {cid for cid, *_ in SEED_CATEGORIES} <= set(cats)
    assert cats["PETS"]["kind"] == "spending" and cats["PETS"]["custom"] is False
    assert [s["key"] for s in client.get("/api/alerts/settings").json()] == [
        "low", "big", "budget", "due", "newrec", "price", "conn", "income",
        "low_ahead", "reminder"]
    items = client.get("/api/plaid/items").json()
    assert items[0]["status"] == "ok" and items[0]["account_count"] == 1
    assert {a["name"] for a in client.get("/api/accounts").json()} == {"Checking", "Cash"}
    client.post("/api/auth/lock")

    conn = raw(settings, key)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == migrations.LATEST == 8
        assert conn.execute("SELECT excluded_account_ids, transactions_cursor FROM plaid_items").fetchone() == (
            "[]", "cursor-7")
        # The upgraded schema matches a fresh create_all schema, column for column.
        fresh_settings = settings.model_copy(update={"data_dir": settings.data_dir.parent / "fresh"})
        fresh = dbmod.Database(fresh_settings.db_path)
        fresh_settings.data_dir.mkdir()
        fresh.open(bytearray(key), create_schema=True)
        fresh.close()
        fconn = sqlcipher3.connect(str(fresh_settings.db_path))
        fconn.execute(dbmod._key_pragma(key))
        tables = [r[0] for r in fconn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        upgraded = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        assert tables == upgraded
        for table in tables:
            assert set(columns(fconn, table)) == set(columns(conn, table)), table
            for col, (notnull, _default) in columns(fconn, table).items():
                assert columns(conn, table)[col][0] == notnull, (table, col)
        indexes = lambda c: {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'")}  # noqa: E731
        assert indexes(fconn) == indexes(conn)
        assert fconn.execute("PRAGMA user_version").fetchone()[0] == migrations.LATEST
        fconn.close()
    finally:
        conn.close()


def test_upgraded_vault_reopens_without_rerunning(settings, client):
    build_r1_vault(settings)
    assert client.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200
    client.post("/api/categories", json={"name": "Kids", "kind": "spending"})
    client.post("/api/auth/lock")
    assert client.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200
    names = [c["name"] for c in client.get("/api/categories").json()]
    assert names.count("Food and drink") == 1 and "Kids" in names


def test_failed_migration_rolls_back_to_v1(settings, monkeypatch):
    key = build_r1_vault(settings)
    monkeypatch.setattr(migrations, "_V2_ALTERS", migrations._V2_ALTERS + ("ALTER TABLE nope ADD COLUMN x TEXT",))
    database = dbmod.Database(settings.db_path)
    with pytest.raises(sqlcipher3.OperationalError):
        database.open(bytearray(key))
    assert not database.is_open and database.open_connection_count == 0
    conn = raw(settings, key)
    try:
        # Stamped v1, but nothing of v2 was applied.
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM sqlite_master WHERE name = 'categories'").fetchone()[0] == 0
        assert "plaid_category" not in columns(conn, "transactions")
    finally:
        conn.close()
    monkeypatch.undo()
    database.open(bytearray(key))  # the real migration now succeeds
    database.close()
    conn = raw(settings, key)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == migrations.LATEST
        assert conn.execute("SELECT plaid_category FROM transactions WHERE id = 1").fetchone()[0] == "FOOD_AND_DRINK"
    finally:
        conn.close()


def test_fresh_vault_is_stamped_latest_and_seeded(unlocked, settings, vault):
    key = bytearray(vault.db.key)
    unlocked.post("/api/auth/lock")
    conn = raw(settings, key)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == migrations.LATEST
        assert conn.execute("SELECT count(*) FROM categories").fetchone()[0] == len(SEED_CATEGORIES)
        assert dict(conn.execute('SELECT "key", value FROM alert_settings').fetchall()) == {
            "low": 2000.0, "big": 250.0, "budget": 90.0, "due": 3.0,
            "newrec": None, "price": None, "conn": None, "income": None, "low_ahead": 7.0, "reminder": None,
        }
        assert all(row[0] == 1 for row in conn.execute("SELECT enabled FROM alert_settings"))
    finally:
        conn.close()


def test_newer_schema_is_refused(settings, client, vault):
    key = build_r1_vault(settings)
    conn = raw(settings, key)
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()
    with pytest.raises(migrations.SchemaTooNew):
        vault.db.open(bytearray(key))
    assert not vault.db.is_open


# ------------------------------------------------------------------ v3: envelope budgets


def build_v2_vault(settings, monkeypatch) -> bytearray:
    """A schema-v2 vault (Release 2) holding Release 2 budgets: whole saved months, no ``removed``."""
    key = build_r1_vault(settings)
    monkeypatch.setattr(migrations, "LATEST", 2)
    database = dbmod.Database(settings.db_path)
    database.open(bytearray(key))
    database.close()
    # Back to the real latest version: the app's models need the newest schema to run.
    monkeypatch.setattr(migrations, "LATEST", REAL_LATEST)
    conn = raw(settings, key)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert "removed" not in columns(conn, "budgets")
        # June: pool only. July: Food + Travel. August drops Travel. September re-adds it.
        conn.executemany("INSERT INTO budget_months VALUES (?, ?)",
                         [("2026-06", 10000), ("2026-07", 50000), ("2026-08", 50000), ("2026-09", 50000)])
        conn.executemany(
            "INSERT INTO budgets (month, category, limit_cents) VALUES (?, ?, ?)",
            [("2026-07", "FOOD_AND_DRINK", 20000), ("2026-07", "TRAVEL", 10000),
             ("2026-08", "FOOD_AND_DRINK", 25000),
             ("2026-09", "FOOD_AND_DRINK", 25000), ("2026-09", "TRAVEL", 5000)],
        )
        conn.commit()
    finally:
        conn.close()
    return key


def budget_rows(conn) -> list[tuple]:
    return conn.execute(
        "SELECT month, category, limit_cents, removed FROM budgets ORDER BY month, category"
    ).fetchall()


def test_v2_database_with_budgets_is_upgraded(settings, client, monkeypatch, fixed_today):
    key = build_v2_vault(settings, monkeypatch)
    assert client.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200
    client.post("/api/auth/lock")
    conn = raw(settings, key)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == REAL_LATEST
        assert columns(conn, "budgets")["removed"][0] == 1  # NOT NULL
        assert columns(conn, "budgets")["restart"][0] == 1  # NOT NULL
        # No migrated row is a same-month re-add (Travel rejoined a month later).
        assert conn.execute("SELECT count(*) FROM budgets WHERE restart != 0").fetchone()[0] == 0
        # Rows kept as assignments; the category August's saved month dropped gets a removed row.
        assert budget_rows(conn) == [
            ("2026-07", "FOOD_AND_DRINK", 20000, 0), ("2026-07", "TRAVEL", 10000, 0),
            ("2026-08", "FOOD_AND_DRINK", 25000, 0), ("2026-08", "TRAVEL", 0, 1),
            ("2026-09", "FOOD_AND_DRINK", 25000, 0), ("2026-09", "TRAVEL", 5000, 0),
        ]
        assert conn.execute("SELECT count(*) FROM budget_months").fetchone()[0] == 4  # left alone
    finally:
        conn.close()

    assert client.post("/api/auth/unlock", json={"password": PASSWORD}).status_code == 200
    aug = client.get("/api/budgets", params={"month": "2026-08"}).json()
    assert [c["category"] for c in aug["categories"]] == ["FOOD_AND_DRINK"]
    assert "TRAVEL" in [c["id"] for c in aug["addable"]]
    sep = client.get("/api/budgets", params={"month": "2026-09"}).json()
    lines = {c["category"]: c for c in sep["categories"]}
    # Food: July 200 left, August 200 + 250, September 450 + 250 - 4.50 coffee.
    assert (lines["FOOD_AND_DRINK"]["carryover"], lines["FOOD_AND_DRINK"]["available"]) == (450.0, 695.5)
    # Travel rejoined in September, so nothing carries in from July.
    assert (lines["TRAVEL"]["carryover"], lines["TRAVEL"]["assigned"], lines["TRAVEL"]["available"]) == (0.0, 50.0, 50.0)
    assert sep["earliest_month"] == "2026-07"
    # Default budget accounts: Checking (bank) 1500; the "other" Cash account isn't included by default.
    assert sep["cash_total"] == 1500.0 and sep["ready_to_assign"] == 754.5


def test_failed_v3_migration_rolls_back_to_v2(settings, monkeypatch):
    key = build_v2_vault(settings, monkeypatch)
    monkeypatch.setattr(migrations, "_V3_ALTERS", migrations._V3_ALTERS + ("ALTER TABLE nope ADD COLUMN x TEXT",))
    database = dbmod.Database(settings.db_path)
    with pytest.raises(sqlcipher3.OperationalError):
        database.open(bytearray(key))
    conn = raw(settings, key)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert "removed" not in columns(conn, "budgets")
        assert "restart" not in columns(conn, "budgets")
        assert len(budget_rows_v2(conn)) == 5
    finally:
        conn.close()
    monkeypatch.setattr(migrations, "_V3_ALTERS", migrations._V3_ALTERS[:-1])
    database.open(bytearray(key))
    database.close()
    conn = raw(settings, key)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == REAL_LATEST
        assert len(budget_rows(conn)) == 6
    finally:
        conn.close()


def budget_rows_v2(conn) -> list[tuple]:
    return conn.execute("SELECT month, category, limit_cents FROM budgets").fetchall()

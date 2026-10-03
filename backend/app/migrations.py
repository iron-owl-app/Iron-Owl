"""Schema migrations keyed on ``PRAGMA user_version``.

- A fresh database gets the latest schema from ``create_all`` and is stamped LATEST.
- ``user_version = 0`` with existing tables is a Release 1 database: stamp it v1.
- Each later step runs in its own transaction together with its version bump, so a
  crash leaves the database at the previous version, never half-migrated.

Migration DDL is frozen SQL (not derived from the models), so a future model change
can't silently alter what an old migration does.
"""
from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Iterator
from typing import Any

from sqlalchemy.engine import Engine

from .models import Base
from .seeds import ALERT_SEEDS, SEED_CATEGORIES, fnv1a_hue, title_case, valid_primary

log = logging.getLogger("fintrack.migrations")

LATEST = 8


class SchemaTooNew(Exception):
    """The database was written by a newer FinTrack."""


_V2_TABLES = (
    """CREATE TABLE categories (
        id VARCHAR NOT NULL,
        name VARCHAR NOT NULL,
        hue INTEGER NOT NULL,
        kind VARCHAR NOT NULL,
        custom BOOLEAN NOT NULL,
        hidden BOOLEAN NOT NULL,
        position INTEGER NOT NULL,
        PRIMARY KEY (id)
    )""",
    """CREATE TABLE budget_months (
        month VARCHAR NOT NULL,
        pool_cents INTEGER NOT NULL,
        PRIMARY KEY (month)
    )""",
    """CREATE TABLE budgets (
        id INTEGER NOT NULL,
        month VARCHAR NOT NULL,
        category VARCHAR NOT NULL,
        limit_cents INTEGER NOT NULL,
        PRIMARY KEY (id),
        CONSTRAINT uq_budget_month_category UNIQUE (month, category),
        FOREIGN KEY(category) REFERENCES categories (id) ON DELETE CASCADE
    )""",
    "CREATE INDEX ix_budgets_month ON budgets (month)",
    """CREATE TABLE recurring_items (
        id INTEGER NOT NULL,
        name VARCHAR NOT NULL,
        merchant_key VARCHAR NOT NULL,
        account_id INTEGER,
        amount_cents INTEGER NOT NULL,
        cadence VARCHAR NOT NULL,
        next_date DATE NOT NULL,
        status VARCHAR NOT NULL,
        include_in_forecast BOOLEAN NOT NULL,
        source VARCHAR NOT NULL,
        last_seen_date DATE,
        month_days TEXT,
        created_at DATETIME NOT NULL,
        PRIMARY KEY (id),
        FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE SET NULL
    )""",
    "CREATE INDEX ix_recurring_items_merchant_key ON recurring_items (merchant_key)",
    """CREATE TABLE goals (
        id INTEGER NOT NULL,
        name VARCHAR NOT NULL,
        kind VARCHAR NOT NULL,
        account_id INTEGER,
        target_cents INTEGER NOT NULL,
        start_cents INTEGER NOT NULL,
        current_cents INTEGER NOT NULL,
        monthly_cents INTEGER NOT NULL,
        apr FLOAT,
        target_date DATE,
        hue INTEGER NOT NULL,
        created_at DATETIME NOT NULL,
        PRIMARY KEY (id),
        FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE SET NULL
    )""",
    """CREATE TABLE rules (
        id INTEGER NOT NULL,
        position INTEGER NOT NULL,
        field VARCHAR NOT NULL,
        op VARCHAR NOT NULL,
        text VARCHAR NOT NULL,
        amount_op VARCHAR,
        amount_cents INTEGER,
        action VARCHAR NOT NULL,
        category VARCHAR,
        enabled BOOLEAN NOT NULL,
        PRIMARY KEY (id),
        FOREIGN KEY(category) REFERENCES categories (id)
    )""",
    """CREATE TABLE alert_settings (
        "key" VARCHAR NOT NULL,
        enabled BOOLEAN NOT NULL,
        value FLOAT,
        PRIMARY KEY ("key")
    )""",
    """CREATE TABLE alert_events (
        id INTEGER NOT NULL,
        "key" VARCHAR NOT NULL,
        severity VARCHAR NOT NULL,
        title VARCHAR NOT NULL,
        body VARCHAR NOT NULL,
        created_at DATETIME NOT NULL,
        read BOOLEAN NOT NULL,
        dedupe_key VARCHAR NOT NULL,
        cleared BOOLEAN NOT NULL,
        PRIMARY KEY (id),
        UNIQUE (dedupe_key)
    )""",
    "CREATE INDEX ix_alert_events_created_at ON alert_events (created_at)",
    """CREATE TABLE app_settings (
        "key" VARCHAR NOT NULL,
        value TEXT,
        PRIMARY KEY ("key")
    )""",
)

_V2_ALTERS = (
    "ALTER TABLE transactions ADD COLUMN plaid_category TEXT",
    "ALTER TABLE transactions ADD COLUMN category_source TEXT NOT NULL DEFAULT 'plaid'",
    "ALTER TABLE transactions ADD COLUMN rule_id INTEGER",
    "ALTER TABLE transactions ADD COLUMN notes TEXT",
    "ALTER TABLE transactions ADD COLUMN is_transfer BOOLEAN NOT NULL DEFAULT 0",
    "ALTER TABLE transactions ADD COLUMN transfer_source TEXT",
    "ALTER TABLE plaid_items ADD COLUMN excluded_account_ids TEXT NOT NULL DEFAULT '[]'",
)


def seed(conn: Any) -> None:
    """Idempotent: seeded categories and alert settings (existing rows are kept)."""
    for position, (cid, name, hue, kind) in enumerate(SEED_CATEGORIES):
        conn.execute(
            "INSERT OR IGNORE INTO categories (id, name, hue, kind, custom, hidden, position) "
            "VALUES (?, ?, ?, ?, 0, 0, ?)",
            (cid, name, hue, kind, position),
        )
    for key, (value, _unit) in ALERT_SEEDS.items():
        conn.execute(
            'INSERT OR IGNORE INTO alert_settings ("key", enabled, value) VALUES (?, 1, ?)',
            (key, value),
        )


def _v2(conn: Any) -> None:
    for stmt in _V2_TABLES:
        conn.execute(stmt)
    for stmt in _V2_ALTERS:
        conn.execute(stmt)
    conn.execute("UPDATE transactions SET plaid_category = category")
    seed(conn)
    # Primaries Release 1 stored that aren't seeded become spending categories, as sync would.
    known = {row[0] for row in conn.execute("SELECT id FROM categories")}
    position = conn.execute("SELECT COALESCE(MAX(position), -1) FROM categories").fetchone()[0]
    for (primary,) in conn.execute(
        "SELECT DISTINCT plaid_category FROM transactions WHERE plaid_category IS NOT NULL"
    ).fetchall():
        if primary in known:
            continue
        if valid_primary(primary) is None:
            # Not a Plaid primary: the transaction falls back to OTHER.
            conn.execute(
                "UPDATE transactions SET plaid_category = NULL, category = NULL WHERE plaid_category = ?",
                (primary,),
            )
            continue
        position += 1
        conn.execute(
            "INSERT INTO categories (id, name, hue, kind, custom, hidden, position) "
            "VALUES (?, ?, ?, 'spending', 0, 0, ?)",
            (primary, title_case(primary), fnv1a_hue(primary), position),
        )
        known.add(primary)


_V3_ALTERS = (
    "ALTER TABLE budgets ADD COLUMN removed BOOLEAN NOT NULL DEFAULT 0",
    # A category re-added in the month it was removed starts with no carryover.
    "ALTER TABLE budgets ADD COLUMN restart BOOLEAN NOT NULL DEFAULT 0",
)

# Release 2 saved a month's limits as a whole: a category missing from a saved month wasn't
# budgeted that month (later months inherited the latest saved month). Envelope membership is
# "the latest row at or before the month", so a category that a saved month dropped gets a
# removed row there; otherwise it would silently rejoin the budget with its old leftover.
_V3_REMOVED_ROWS = """
    INSERT INTO budgets (month, category, limit_cents, removed)
    SELECT bm.month, prev.category, 0, 1
    FROM budget_months AS bm
    JOIN budgets AS prev
      ON prev.month = (SELECT MAX(p.month) FROM budget_months AS p WHERE p.month < bm.month)
    WHERE NOT EXISTS (
        SELECT 1 FROM budgets AS cur WHERE cur.month = bm.month AND cur.category = prev.category
    )
"""


def _v3(conn: Any) -> None:
    for stmt in _V3_ALTERS:
        conn.execute(stmt)
    conn.execute(_V3_REMOVED_ROWS)


_V4_TABLES = (
    """CREATE TABLE category_groups (
        id INTEGER NOT NULL,
        name VARCHAR COLLATE "NOCASE" NOT NULL,
        position INTEGER NOT NULL,
        created_at DATETIME NOT NULL,
        PRIMARY KEY (id),
        UNIQUE (name)
    )""",
    """CREATE TABLE transaction_splits (
        id INTEGER NOT NULL,
        transaction_id INTEGER NOT NULL,
        amount_cents INTEGER NOT NULL,
        category VARCHAR NOT NULL,
        notes TEXT,
        position INTEGER NOT NULL,
        PRIMARY KEY (id),
        FOREIGN KEY(transaction_id) REFERENCES transactions (id) ON DELETE CASCADE,
        FOREIGN KEY(category) REFERENCES categories (id)
    )""",
    "CREATE INDEX ix_transaction_splits_transaction_id ON transaction_splits (transaction_id)",
    """CREATE TABLE tags (
        id INTEGER NOT NULL,
        name VARCHAR COLLATE "NOCASE" NOT NULL,
        hue INTEGER NOT NULL,
        PRIMARY KEY (id),
        UNIQUE (name)
    )""",
    """CREATE TABLE transaction_tags (
        transaction_id INTEGER NOT NULL,
        tag_id INTEGER NOT NULL,
        PRIMARY KEY (transaction_id, tag_id),
        FOREIGN KEY(transaction_id) REFERENCES transactions (id) ON DELETE CASCADE,
        FOREIGN KEY(tag_id) REFERENCES tags (id) ON DELETE CASCADE
    )""",
    "CREATE INDEX ix_transaction_tags_tag_id ON transaction_tags (tag_id)",
)

_V4_ALTERS = (
    # SQLite allows ADD COLUMN with a REFERENCES clause when the default is NULL.
    "ALTER TABLE categories ADD COLUMN group_id INTEGER REFERENCES category_groups (id) ON DELETE SET NULL",
    "ALTER TABLE categories ADD COLUMN target_kind TEXT",
    "ALTER TABLE categories ADD COLUMN target_cents INTEGER",
    "ALTER TABLE categories ADD COLUMN target_date DATE",
    "ALTER TABLE accounts ADD COLUMN loan_group TEXT",
    "ALTER TABLE balance_snapshots ADD COLUMN estimated BOOLEAN NOT NULL DEFAULT '0'",
)


def _v4(conn: Any) -> None:
    for stmt in _V4_TABLES:
        conn.execute(stmt)
    for stmt in _V4_ALTERS:
        conn.execute(stmt)
    # The "income" alert ("$X new to assign"); enabled, no value.
    conn.execute('INSERT OR IGNORE INTO alert_settings ("key", enabled, value) VALUES (\'income\', 1, NULL)')


def _v5(conn: Any) -> None:
    # Home "Today": structured data for new alert events (JSON; e.g. a price change's
    # recurring id and old/new amounts). Older events keep NULL.
    conn.execute("ALTER TABLE alert_events ADD COLUMN data TEXT")
    # When the item's last sync attempt failed (Home's "Not now" on a bank error comes back
    # after the next failure). NULL until an item fails after this migration.
    conn.execute("ALTER TABLE plaid_items ADD COLUMN last_error_at DATETIME")


_V6_STATEMENTS = (
    # Release 3.6: cash forecast calendar.
    "ALTER TABLE recurring_items ADD COLUMN reminder_days INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE recurring_items ADD COLUMN start_date DATE",
    # SQLite allows ADD COLUMN with a REFERENCES clause when the default is NULL.
    "ALTER TABLE recurring_items ADD COLUMN category_id TEXT REFERENCES categories (id) ON DELETE SET NULL",
    """CREATE TABLE recurring_overrides (
        id INTEGER NOT NULL,
        recurring_id INTEGER NOT NULL,
        base_date DATE NOT NULL,
        moved_to DATE,
        skipped BOOLEAN DEFAULT '0' NOT NULL,
        paid BOOLEAN DEFAULT '0' NOT NULL,
        paid_amount_cents INTEGER,
        created_at DATETIME NOT NULL,
        PRIMARY KEY (id),
        CONSTRAINT uq_recurring_override_base UNIQUE (recurring_id, base_date),
        FOREIGN KEY(recurring_id) REFERENCES recurring_items (id) ON DELETE CASCADE
    )""",
    "CREATE INDEX ix_recurring_overrides_recurring_id ON recurring_overrides (recurring_id)",
    # "Low balance ahead" (days) and bill reminders; both enabled.
    'INSERT OR IGNORE INTO alert_settings ("key", enabled, value) VALUES (\'low_ahead\', 1, 7)',
    'INSERT OR IGNORE INTO alert_settings ("key", enabled, value) VALUES (\'reminder\', 1, NULL)',
    # Existing active items get a budget category from their past transactions on the
    # first unlock after this upgrade (services.recurring.backfill_categories_once).
    'INSERT OR IGNORE INTO app_settings ("key", value) VALUES (\'recurring_category_backfill\', \'1\')',
)


def _v6(conn: Any) -> None:
    for stmt in _V6_STATEMENTS:
        conn.execute(stmt)


def _v7(conn: Any) -> None:
    # Release 3.6 phase 2 (Budget for a beginner): Plaid's detailed personal finance
    # category, stored by sync from now on, so bank-set transactions can be sorted into the
    # budget's Groceries / Eating out / Gas rows (services/automap.py). Older rows stay NULL.
    conn.execute("ALTER TABLE transactions ADD COLUMN plaid_detailed TEXT")


def _v8(conn: Any) -> None:
    # Release 3.17 (budget plans repeat each month): the part of a month's ``limit_cents`` that
    # came from one-time moves (Move money, Cover it). ``limit_cents`` stays the month's full
    # amount; the plan that repeats into later months is ``limit_cents - moved_cents``. Rows
    # saved before this keep 0 (no backfill): their whole amount counts as the plan.
    conn.execute("ALTER TABLE budgets ADD COLUMN moved_cents INTEGER NOT NULL DEFAULT 0")


MIGRATIONS: dict[int, Callable[[Any], None]] = {2: _v2, 3: _v3, 4: _v4, 5: _v5, 6: _v6, 7: _v7, 8: _v8}


# ------------------------------------------------------------------ plumbing


@contextlib.contextmanager
def _raw(engine: Engine) -> Iterator[Any]:
    """A DBAPI connection in autocommit mode, so BEGIN/COMMIT are ours alone."""
    proxied = engine.raw_connection()
    conn = proxied.driver_connection
    previous = conn.isolation_level
    conn.isolation_level = None
    try:
        yield conn
    finally:
        try:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
        finally:
            conn.isolation_level = previous
            proxied.close()


@contextlib.contextmanager
def _transaction(conn: Any) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def user_version(conn: Any) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _set_version(conn: Any, version: int) -> None:
    conn.execute(f"PRAGMA user_version = {int(version)}")


def _has_table(conn: Any, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def migrate(
    engine: Engine, *, fresh: bool = False, before_upgrade: Callable[[int], None] | None = None
) -> int:
    """Bring the database to LATEST; returns the resulting version.

    ``before_upgrade(old_version)`` runs before the first write when an existing database
    needs upgrading (never for a fresh one). At that point the pool's only connection is
    idle in autocommit mode with no transaction open, and its first read (the key check)
    has already rolled back any hot journal, so the file on disk is complete and nothing
    is writing to it. If it raises, the database is left exactly as it was.
    """
    with _raw(engine) as conn:
        version = user_version(conn)
        if version == 0 and (not _has_table(conn, "accounts") or _has_table(conn, "categories")):
            # Empty, or a fresh create_all (it already has v2's tables) that stopped before
            # the stamp: finish it as fresh. Re-running v2's CREATE TABLEs would fail forever.
            fresh = True
    if version > LATEST:
        raise SchemaTooNew(version)

    if fresh:
        Base.metadata.create_all(engine)
        with _raw(engine) as conn, _transaction(conn):
            seed(conn)
            _set_version(conn, LATEST)
        return LATEST

    if before_upgrade is not None and version < LATEST:
        before_upgrade(version)

    with _raw(engine) as conn:
        if version == 0:
            with _transaction(conn):
                _set_version(conn, 1)
            version = 1
        for target in range(version + 1, LATEST + 1):
            with _transaction(conn):
                MIGRATIONS[target](conn)
                _set_version(conn, target)
            log.info("migrated database to schema v%s", target)
            version = target
    return version

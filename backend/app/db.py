"""SQLCipher engine lifecycle.

The engine exists only while the app is unlocked. Every raw connection is keyed
in the ``creator`` and tracked, so that locking can deterministically close all of
them (``engine.dispose()`` alone leaves checked-out connections to the GC).
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

import sqlcipher3
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool

from . import migrations


class InvalidKey(Exception):
    """The key does not decrypt the database."""


class DatabaseLocked(Exception):
    """No engine is open (the app is locked or locking)."""


def _key_pragma(key: bytes | bytearray) -> str:
    # Raw 256-bit key: SQLCipher uses it directly and skips its own PBKDF2.
    return f"PRAGMA key = \"x'{bytes(key).hex()}'\""


def open_raw_connection(path: Path, key: bytes | bytearray) -> sqlcipher3.Connection:
    """Open and key a connection, verifying the key actually decrypts the file."""
    conn = sqlcipher3.connect(str(path), check_same_thread=False, timeout=15)
    try:
        # Silence SQLCipher's stderr logging so wrong-key attempts leave no noise.
        conn.execute("PRAGMA cipher_log_level = NONE")
        conn.execute(_key_pragma(key))
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        conn.execute("PRAGMA foreign_keys = ON")
        # Keep temporary tables/indices in memory, never in plaintext temp files.
        conn.execute("PRAGMA temp_store = MEMORY")
    except sqlcipher3.DatabaseError as exc:
        conn.close()
        raise InvalidKey() from exc
    except BaseException:
        conn.close()
        raise
    return conn


def verify_key(path: Path, key: bytes | bytearray) -> bool:
    if not path.exists():
        # Connecting to a missing file would create an empty DB that "accepts" any key.
        return False
    try:
        open_raw_connection(path, key).close()
    except InvalidKey:
        return False
    return True


def rekey(path: Path, old_key: bytes | bytearray, new_key: bytes | bytearray) -> None:
    conn = open_raw_connection(path, old_key)
    try:
        conn.execute(f"PRAGMA rekey = \"x'{bytes(new_key).hex()}'\"")
    finally:
        conn.close()


class Database:
    def __init__(self, path: Path, *, before_migrate: Callable[[int], None] | None = None) -> None:
        self.path = path
        # Called with the old ``user_version`` before an existing database is upgraded, while
        # nothing has written to it yet (the vault's pre-migration safety copy). If it raises,
        # nothing is migrated and ``open`` fails.
        self._before_migrate = before_migrate
        self._cond = threading.Condition()
        self._engine: Engine | None = None
        self._sessionmaker: sessionmaker[Session] | None = None
        self._key: bytearray | None = None
        self._live: list[bool] = [False]
        self._raw_conns: set[sqlcipher3.Connection] = set()
        self._raw_lock = threading.Lock()
        self._active = 0
        self._closing = False

    @property
    def is_open(self) -> bool:
        return self._engine is not None

    @property
    def key(self) -> bytearray | None:
        return self._key

    def open(self, key: bytes | bytearray, *, create_schema: bool = False) -> None:
        with self._cond:
            if self._engine is not None:
                raise RuntimeError("database already open")
            held = bytearray(key)
            # Cleared (under _raw_lock) by close(): a connection that finishes opening
            # after the lock started must be closed instead of registered, or it would
            # outlive the lock while holding the key.
            live = [True]

            def creator() -> sqlcipher3.Connection:
                conn = open_raw_connection(self.path, held)
                with self._raw_lock:
                    if live[0]:
                        self._raw_conns.add(conn)
                        return conn
                conn.close()
                raise DatabaseLocked()

            engine = create_engine(
                "sqlite://",
                creator=creator,
                poolclass=QueuePool,
                pool_size=3,
                max_overflow=5,
                pool_timeout=30,
                # Keep bound parameters (e.g. access tokens) out of exception messages.
                hide_parameters=True,
            )

            @event.listens_for(engine, "close")
            def _on_close(dbapi_conn, _record):  # noqa: ANN001
                with self._raw_lock:
                    self._raw_conns.discard(dbapi_conn)

            @event.listens_for(engine, "close_detached")
            def _on_close_detached(dbapi_conn):  # noqa: ANN001
                with self._raw_lock:
                    self._raw_conns.discard(dbapi_conn)

            try:
                # Fresh vault: latest schema. Existing vault: run pending migrations.
                migrations.migrate(engine, fresh=create_schema, before_upgrade=self._before_migrate)
            except BaseException:
                with self._raw_lock:
                    live[0] = False
                _wipe(held)
                engine.dispose()
                self._close_raw()
                raise

            self._engine = engine
            self._sessionmaker = sessionmaker(bind=engine, expire_on_commit=False)
            self._key = held
            self._live = live

    def acquire(self) -> Session:
        with self._cond:
            if self._engine is None or self._closing or self._sessionmaker is None:
                raise DatabaseLocked()
            self._active += 1
            factory = self._sessionmaker
        try:
            return factory()
        except BaseException:
            self._release_slot()
            raise

    def backup_to(self, dest: Path) -> int:
        """Write a consistent copy of the open database to ``dest``; returns its user_version.

        ``VACUUM INTO`` on a SQLCipher connection writes the copy encrypted with the same key.
        """
        with self._cond:
            if self._engine is None or self._closing:
                raise DatabaseLocked()
            self._active += 1
            engine = self._engine
        try:
            proxied = engine.raw_connection()
            try:
                conn = proxied.driver_connection
                conn.execute("VACUUM INTO ?", (str(dest),))
                return int(conn.execute("PRAGMA user_version").fetchone()[0])
            finally:
                proxied.close()
        finally:
            self._release_slot()

    def release(self, session: Session) -> None:
        try:
            session.close()
        finally:
            self._release_slot()

    def _release_slot(self) -> None:
        with self._cond:
            self._active -= 1
            self._cond.notify_all()

    def close(self, wait_seconds: float = 30.0) -> None:
        """Dispose the engine, close every raw connection and wipe the held key."""
        with self._cond:
            if self._engine is None:
                return
            self._closing = True
            # Let in-flight requests finish; after the timeout we close regardless.
            self._cond.wait_for(lambda: self._active == 0, timeout=wait_seconds)
            engine, key, live = self._engine, self._key, self._live
            self._engine = None
            self._sessionmaker = None
            self._key = None
            self._live = [False]
            # Stop new connections first (flag + wiped key), then close the rest.
            with self._raw_lock:
                live[0] = False
            if key is not None:
                _wipe(key)
            try:
                engine.dispose()
            finally:
                self._close_raw()
                self._closing = False

    def _close_raw(self) -> None:
        with self._raw_lock:
            conns = list(self._raw_conns)
            self._raw_conns.clear()
        for conn in conns:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - best effort on lock
                pass

    @property
    def open_connection_count(self) -> int:
        with self._raw_lock:
            return len(self._raw_conns)


def _wipe(buf: bytearray) -> None:
    # Best effort: overwrite our copy of the key. CPython may still hold transient
    # copies (e.g. the hex string built for PRAGMA key), which we cannot scrub.
    for i in range(len(buf)):
        buf[i] = 0

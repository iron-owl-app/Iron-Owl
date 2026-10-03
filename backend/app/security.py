"""Key derivation, vault lock state, sessions, brute-force limiting and request guards."""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import NamedTuple, NoReturn

from argon2.low_level import Type, hash_secret_raw
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import db as dbmod
from . import recovery as rec
from .config import Settings
from .migrations import SchemaTooNew
from .models import AppSetting

log = logging.getLogger("fintrack.security")

SESSION_COOKIE = "ft_session"
SESSION_HEADER = "X-FinTrack-Session"


class NewSession(NamedTuple):
    cookie: str  # HttpOnly cookie value
    token: str  # returned once in the JSON body; the frontend echoes it in SESSION_HEADER


# Vault.session_state answers
SESSION_OK = "ok"
SESSION_LOCKED = "locked"
SESSION_MOVED = "moved"  # this token's session was taken over by another window

# Why the vault last locked (GET /api/auth/status "lock_reason"; None = unknown / not since start)
LOCK_REASONS = ("idle", "closed", "manual", "moved", "shutdown")


class PresenceView(NamedTuple):
    """One consistent look at the vault for a caller (status, heartbeat)."""

    state: str  # SESSION_OK | SESSION_LOCKED | SESSION_MOVED for the caller's credentials
    vault_unlocked: bool
    bound_tab: str | None  # the window the session belongs to
    lock_reason: str | None


def _token_digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8", "surrogatepass")).digest()

CSRF_HEADER = "x-fintrack"
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024

KDF_DEFAULTS = {"time_cost": 3, "memory_cost": 65536, "parallelism": 4}
SALT_BYTES = 16
KEY_BYTES = 32


def now() -> float:
    """Monotonic clock for idle and lockout timing (tests monkeypatch this)."""
    return time.monotonic()


# --------------------------------------------------------------------------- KDF


@dataclass(frozen=True)
class KeyParams:
    salt: bytes
    time_cost: int
    memory_cost: int
    parallelism: int

    @classmethod
    def new(cls) -> KeyParams:
        return cls(salt=secrets.token_bytes(SALT_BYTES), **KDF_DEFAULTS)

    def to_json(self) -> dict:
        return {
            "version": 1,
            "kdf": "argon2id",
            "salt": base64.b64encode(self.salt).decode("ascii"),
            **{k: getattr(self, k) for k in ("time_cost", "memory_cost", "parallelism")},
        }

    @classmethod
    def from_json(cls, data: dict) -> KeyParams:
        if not isinstance(data, dict) or data.get("version") != 1 or data.get("kdf") != "argon2id":
            raise ValueError("unsupported keyfile")
        salt = base64.b64decode(data["salt"], validate=True)
        params = cls(
            salt=salt,
            time_cost=int(data["time_cost"]),
            memory_cost=int(data["memory_cost"]),
            parallelism=int(data["parallelism"]),
        )
        # The keyfile is not secret but it is untrusted input: bound the cost
        # parameters so a tampered file can't be used to exhaust memory/CPU.
        if not (
            len(salt) >= SALT_BYTES
            and 1 <= params.time_cost <= 20
            and 8 * 1024 <= params.memory_cost <= 1024 * 1024
            and 1 <= params.parallelism <= 16
        ):
            raise ValueError("keyfile parameters out of range")
        return params

    def derive(self, password: str) -> bytearray:
        return bytearray(
            hash_secret_raw(
                # "surrogatepass" so a lone surrogate in the JSON body can't raise (500).
                # For every well-formed string the bytes are identical to strict UTF-8.
                secret=password.encode("utf-8", "surrogatepass"),
                salt=self.salt,
                time_cost=self.time_cost,
                memory_cost=self.memory_cost,
                parallelism=self.parallelism,
                hash_len=KEY_BYTES,
                type=Type.ID,
            )
        )


def read_keyfile(path: Path) -> KeyParams | None:
    """The password KDF parameters only (backup validation, older callers)."""
    return read_keyfile_full(path)[0]


def read_keyfile_full(path: Path, *, quiet: bool = False) -> tuple[KeyParams | None, rec.RecoveryBlock | None]:
    """``(params, recovery block)``. A malformed keyfile gives ``(None, None)``; a malformed
    recovery block gives ``(params, None)``: the password still works (fail closed, never 500)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        params = KeyParams.from_json(data)
    except FileNotFoundError:
        return None, None
    except Exception:  # noqa: BLE001 - untrusted file: any malformation fails closed
        if not quiet:
            log.warning("ignoring unreadable keyfile %s", path.name)
        return None, None
    block = None
    if "recovery" in data:
        try:
            block = rec.RecoveryBlock.from_json(data["recovery"])
        except ValueError:
            if not quiet:
                log.warning("ignoring a malformed recovery block in %s", path.name)
    return params, block


def keyfile_json(params: KeyParams, block: rec.RecoveryBlock | None) -> dict:
    """keyfile.json stays version 1; older builds ignore the optional ``recovery`` key."""
    data = params.to_json()
    if block is not None:
        data["recovery"] = block.to_json()
    return data


# The packaged app runs under pythonw.exe (no console): without this flag every console
# tool it runs (whoami, icacls) would flash a console window on the user's screen.
_NO_CONSOLE = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def restrict_to_owner(path: Path) -> bool:
    """Best effort: make a directory (and what it contains) private to the current user.

    On Windows a folder on a secondary drive inherits "Authenticated Users: Modify"
    from the drive root, so any local account could copy or tamper with the vault.
    Cut inheritance and grant only this user, SYSTEM and Administrators.
    """
    try:
        if sys.platform != "win32":
            os.chmod(path, 0o700)
            return True
        system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
        out = subprocess.run(
            [str(system32 / "whoami.exe"), "/user", "/fo", "csv", "/nh"],
            capture_output=True, text=True, check=True, timeout=30, creationflags=_NO_CONSOLE,
        ).stdout
        sid = out.strip().rsplit(",", 1)[-1].strip().strip('"')
        if not sid.startswith("S-1-"):
            raise ValueError("could not determine the current user's SID")
        # Directory only (no /T): existing files hold inherited ACEs, which Windows
        # re-propagates from the new directory DACL. /T would strip inheritance from each
        # file and leave it with an empty DACL, making the vault unreadable.
        subprocess.run(
            [
                str(system32 / "icacls.exe"), str(path), "/inheritance:r",
                "/grant:r", f"*{sid}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F",
                "/Q",
            ],
            capture_output=True, check=True, timeout=60, creationflags=_NO_CONSOLE,
        )
        return True
    except Exception as exc:  # noqa: BLE001 - never block the app on this
        log.warning("could not restrict permissions on %s: %s", path, type(exc).__name__)
        return False


def _new_sheet(kdf: rec.RecoveryKdf) -> tuple[list[str], rec.RecoveryKdf, bytes]:
    """Fresh groups and their public key. The code itself is never stored."""
    groups = rec.generate_groups()
    data = rec.data_digits("".join(groups))
    seed = kdf.seed(data)
    try:
        return groups, kdf, rec.public_key(seed)
    finally:
        rec.wipe(seed)
        rec.wipe(data)


def write_json_atomic(path: Path, data: dict) -> None:
    """Temp file beside ``path``, fsync, then ``os.replace`` (retried on a Windows sharing
    violation, see ``_replace_with_retry``). A failed write removes its temp file."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
            fh.flush()
            os.fsync(fh.fileno())
        _replace_with_retry(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


# On Windows an antivirus or indexer briefly holding keyfile.json (or data/updates/state.json)
# open makes os.replace fail with a sharing violation (PermissionError): retry a few times
# before giving up (the launcher's ``replace_with_retry`` does the same).
REPLACE_RETRY_DELAYS = (0.05, 0.15, 0.3)


def _replace_with_retry(src: Path, dst: Path) -> None:
    for delay in (*REPLACE_RETRY_DELAYS, None):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if delay is None:
                raise
            time.sleep(delay)


# ---------------------------------------------------------------- rate limiting


class RateLimiter:
    """Consecutive-failure backoff: 5 failures -> 30s lockout, doubling, max 15 min.

    With ``path`` (the vault's ``data/limiter.json``) the count and the end of a running wait
    survive a server restart, so closing and reopening FinTrack doesn't reset them. The file
    holds no secret: ``{"version":1, "failures":n, "locked_until":<unix time or 0>}``. It is
    untrusted input: a malformed file is ignored, and a loaded wait is never longer than the
    lockout that failure count gives (a clock moved back can't lock the user out for longer).
    Writing it is best effort (a failure is logged by type, never raised).
    """

    THRESHOLD = 5
    BASE_SECONDS = 30
    MAX_SECONDS = 15 * 60
    # 2**5 * 30 s > 15 min already: a larger stored count changes nothing.
    MAX_STORED_FAILURES = THRESHOLD + 16

    def __init__(self, path: Path | None = None) -> None:
        self.failures = 0
        self.locked_until = 0.0
        self.path = path
        if path is not None:
            self._load()

    def _lockout_seconds(self, failures: int) -> int:
        return min(self.BASE_SECONDS * 2 ** (failures - self.THRESHOLD), self.MAX_SECONDS)

    def _load(self) -> None:
        assert self.path is not None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            failures = data["failures"]
            until = data["locked_until"]
            if data.get("version") != 1 or type(failures) is not int or not isinstance(until, (int, float)):
                raise ValueError("bad limiter file")
            if isinstance(until, bool) or not math.isfinite(until) or failures < 0:
                raise ValueError("bad limiter file")
        except FileNotFoundError:
            return
        except Exception:  # noqa: BLE001 - untrusted file: ignore it (fail open to a fresh count)
            log.warning("ignoring an unreadable limiter file")
            return
        self.failures = min(failures, self.MAX_STORED_FAILURES)
        if self.failures >= self.THRESHOLD:
            remaining = min(until - time.time(), self._lockout_seconds(self.failures))
            if remaining > 0:
                self.locked_until = now() + remaining

    def _save(self) -> None:
        path = self.path
        if path is None:
            return
        try:
            if self.failures == 0:
                path.unlink(missing_ok=True)
                return
            if not path.parent.is_dir():
                return  # no vault folder yet (before setup): nothing to keep
            remaining = self.locked_until - now()
            write_json_atomic(path, {
                "version": 1,
                "failures": self.failures,
                "locked_until": time.time() + remaining if remaining > 0 else 0,
            })
        except OSError as exc:
            log.warning("could not save the wrong-try count: %s", type(exc).__name__)

    def retry_after(self) -> int | None:
        remaining = self.locked_until - now()
        return math.ceil(remaining) if remaining > 0 else None

    def record_failure(self) -> int | None:
        """Register a failure; returns the lockout length if one starts now."""
        self.failures += 1
        if self.failures < self.THRESHOLD:
            self._save()
            return None
        seconds = self._lockout_seconds(self.failures)
        self.locked_until = now() + seconds
        self._save()
        return seconds

    def reset(self) -> None:
        had = self.failures or self.locked_until
        self.failures = 0
        self.locked_until = 0.0
        if had:
            self._save()

    def tries_left(self) -> int:
        """Wrong tries left before a wait starts (0 once a lockout has been reached)."""
        return max(0, self.THRESHOLD - self.failures)


class SoftLimiter:
    """Recovery-sheet answers that cost an Argon2 run but aren't wrong tries ("outdated",
    "unusable"): at most LIMIT per WINDOW seconds, so they can't keep the CPU busy without
    end. Separate from ``RateLimiter``: it never adds to the shared failure count."""

    LIMIT = 10
    WINDOW = 60

    def __init__(self) -> None:
        self._times: deque[float] = deque()

    def _prune(self) -> None:
        cutoff = now() - self.WINDOW
        while self._times and self._times[0] <= cutoff:
            self._times.popleft()

    def retry_after(self) -> int | None:
        self._prune()
        if len(self._times) < self.LIMIT:
            return None
        return max(1, math.ceil(self._times[0] + self.WINDOW - now()))

    def record(self) -> None:
        self._prune()
        self._times.append(now())


# ------------------------------------------------------------------------ vault


class VaultError(Exception):
    """An expected failure. Routes answer ``{"detail", "code", ...extra}`` (routers.auth._error).

    Neither ``detail`` nor ``extra`` may ever contain a password or recovery code digits.
    """

    status = 400
    detail = "error"
    code = "error"
    retry_after: int | None = None

    def __init__(self, *args: object, tries_left: int | None = None, extra: dict | None = None) -> None:
        super().__init__(*args)
        self.tries_left = tries_left
        self.extra: dict = dict(extra or {})


class AlreadyInitialized(VaultError):
    status, detail, code = 409, "already initialized", "already_initialized"


class NotInitialized(VaultError):
    status, detail, code = 409, "not initialized", "not_initialized"


class InvalidPassword(VaultError):
    status, detail, code = 401, "invalid password", "wrong_password"


class WeakPassword(VaultError):
    status, detail, code = 422, f"password must be at least {MIN_PASSWORD_LENGTH} characters", "weak_password"


class BadBackup(VaultError):
    status, detail, code = 400, "not a valid Iron Owl backup", "bad_backup"

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(detail or self.detail)
        if detail:
            self.detail = detail


class SessionEnded(VaultError):
    status, detail, code = 401, "locked", "locked"


class CurrentPasswordRequired(VaultError):
    status, detail, code = 422, "Enter your current Iron Owl password to replace this vault.", "password_required"


class WrongCurrentPassword(InvalidPassword):
    detail = "Your current Iron Owl password is incorrect."


class WrongBackupPassword(InvalidPassword):
    detail = "That password doesn't open this backup."


class PreMigrationCopyFailed(VaultError):
    status = 500
    code = "pre_migration_copy_failed"
    detail = (
        "Iron Owl needs to update your data, but couldn't save a safety copy first, so nothing "
        "was changed. Check that the drive has free space, then try again."
    )


class SchemaTooNewError(VaultError):
    """The data was saved by a newer FinTrack than this one (``migrations.SchemaTooNew``)."""

    status = 409
    code = "schema_too_new"
    detail = "This data was saved by a newer version of Iron Owl."


class UpdateRolledBack(VaultError):
    """The just-installed update couldn't update the data: FinTrack goes back to the previous
    version (the launcher restores the copy made before the update). ``extra`` carries
    ``to_version``."""

    status = 409
    code = "FT-UPD-05"
    detail = "update_rolled_back"


# Recovery sheet errors. The details are generic on purpose: never any digit of the code.


class CodeFormatError(VaultError):
    status, detail, code = 422, "Enter the 36 digits from your recovery sheet.", "bad_format"


class CodeTypo(VaultError):
    status, detail, code = 422, "Some groups have a typo.", "typo"


class NoRecovery(VaultError):
    status, detail, code = 409, "This Iron Owl doesn't have a recovery sheet.", "no_recovery"


class NoMatch(InvalidPassword):
    detail, code = "Those numbers don't match this Iron Owl.", "no_match"


class SheetOutdated(VaultError):
    status, detail, code = 409, "This recovery sheet is out of date.", "outdated"


class SheetUnusable(VaultError):
    status, detail, code = 409, "This recovery sheet can't open this Iron Owl.", "unusable"


class PendingExpired(VaultError):
    status, detail, code = 409, "The new recovery sheet was not saved. Make it again.", "pending_expired"


class SheetChanged(VaultError):
    status, detail, code = 409, "The recovery sheet has changed.", "sheet_changed"


class FinishOnUnlock(VaultError):
    """Done on a new sheet failed after the vault was re-encrypted: FinTrack is locked, and
    the next unlock (password or the new sheet) finishes the step from keyfile.json.new."""

    status, code = 500, "finish_on_unlock"
    detail = "Iron Owl couldn't finish this step. Unlock Iron Owl to complete it."


@dataclass(frozen=True)
class SetupResult:
    session: NewSession
    groups: list[str]
    sheet: str
    created_at: str


@dataclass
class PendingSheet:
    """A new sheet shown in Settings but not active yet: its public key, plus the fresh
    password key K' (new salt, same password) the vault is rekeyed to on Done. ``key`` is
    wiped whenever the pending sheet is dropped (``Vault._drop_pending``)."""

    id: str
    session_id: str | None
    public_key: bytes
    kdf: rec.RecoveryKdf
    created_at: str
    expires: float  # monotonic (security.now)
    params: KeyParams
    key: bytearray


PENDING_SECONDS = 30 * 60

# The recovery public key on record, inside the encrypted database (SHA-256, hex). A block
# is trusted for a re-seal only if its MAC checks out AND its public key is on record, so an
# older block (even one validly MAC'd) put back into keyfile.json is never revived. ``prev``
# covers the moment of a new sheet's rekey: written before it, cleared after.
ACTIVE_PUB_ROW = "recovery_active_pub"
PREV_PUB_ROW = "recovery_prev_pub"


def pub_digest(pub: bytes) -> str:
    return hashlib.sha256(pub).hexdigest()


def read_pub_rows(db: dbmod.Database) -> tuple[str | None, str | None]:
    session = db.acquire()
    try:
        rows = [session.get(AppSetting, name) for name in (ACTIVE_PUB_ROW, PREV_PUB_ROW)]
        return tuple(row.value if row is not None else None for row in rows)  # type: ignore[return-value]
    finally:
        db.release(session)


def write_pub_rows(db: dbmod.Database, active: bytes | None, prev: bytes | None) -> None:
    session = db.acquire()
    try:
        for name, pub in ((ACTIVE_PUB_ROW, active), (PREV_PUB_ROW, prev)):
            row = session.get(AppSetting, name)
            if pub is None:
                if row is not None:
                    session.delete(row)
            elif row is None:
                session.add(AppSetting(key=name, value=pub_digest(pub)))
            else:
                row.value = pub_digest(pub)
        session.commit()
    finally:
        db.release(session)


def pub_on_record(db: dbmod.Database, pub: bytes) -> bool:
    try:
        rows = read_pub_rows(db)
    except dbmod.DatabaseLocked:
        return False
    digest = pub_digest(pub).encode("ascii")
    return any(
        isinstance(v, str) and hmac.compare_digest(v.encode("utf-8", "surrogatepass"), digest) for v in rows
    )


def _sidecars(db_path: Path) -> tuple[Path, ...]:
    return tuple(db_path.with_name(db_path.name + suffix) for suffix in ("-wal", "-shm", "-journal"))


# ------------------------------------------------------------------ pre-migration copies

# Before an existing vault is upgraded to a newer schema, its files are copied (still
# encrypted, byte for byte) to data/pre-migrate-v<old>-<YYYYmmddTHHMMSSZ>[-n]/. Only the
# newest PRE_MIGRATE_KEEP are kept.
PRE_MIGRATE_RE = re.compile(r"^pre-migrate-v(\d+)-(\d{8}T\d{6}Z)(?:-(\d+))?$")
PRE_MIGRATE_KEEP = 3


def _copy_synced(src: Path, dest: Path) -> None:
    with open(src, "rb") as fin, open(dest, "xb") as fout:
        shutil.copyfileobj(fin, fout, 1024 * 1024)
        fout.flush()
        os.fsync(fout.fileno())


def _fsync_dir(path: Path) -> None:
    if sys.platform == "win32":
        return  # directories can't be opened for fsync on Windows; NTFS journals the entries
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def pre_migrate_copy(data_dir: Path, files: list[Path], old_version: int) -> Path:
    """Copy ``files`` (those that exist) into a new private pre-migrate folder, fsynced.

    Raises ``PreMigrationCopyFailed`` (and removes the partial folder) on any error, so the
    caller never migrates without a complete copy. Older copies beyond the newest
    ``PRE_MIGRATE_KEEP`` are then removed, best effort.
    """
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = f"pre-migrate-v{int(old_version)}-{stamp}"
    dest = data_dir / base
    n = 1
    while dest.exists():
        n += 1
        dest = data_dir / f"{base}-{n}"
    # Copy under a staging name and rename when complete, so an interrupted copy (power
    # loss) never looks like a finished pre-migrate folder.
    staging = data_dir / f".pre-migrate-tmp-{secrets.token_hex(8)}"
    created = False
    try:
        staging.mkdir()
        created = True
        restrict_to_owner(staging)
        for path in files:
            if path.exists():
                _copy_synced(path, staging / path.name)
        _fsync_dir(staging)
        os.rename(staging, dest)
        created = False
        _fsync_dir(data_dir)
    except Exception as exc:  # noqa: BLE001 - any failure: don't migrate
        log.error("pre-migration copy failed: %s", type(exc).__name__)
        if created:
            shutil.rmtree(staging, ignore_errors=True)
        raise PreMigrationCopyFailed() from None
    log.info("saved a pre-migration copy of the vault (schema v%s)", old_version)
    try:
        _prune_pre_migrate(data_dir, keep=dest)
    except OSError as exc:  # the new copy is safe; an old one left behind is harmless
        log.warning("could not remove old pre-migration copies: %s", type(exc).__name__)
    return dest


def _prune_pre_migrate(data_dir: Path, keep: Path | None = None) -> None:
    """Keep ``keep`` (the copy just made) plus the newest others, up to PRE_MIGRATE_KEEP.

    ``keep`` is never removed, even if a wrong clock gave older copies later stamps.
    """
    found = []
    for path in data_dir.iterdir():
        m = PRE_MIGRATE_RE.match(path.name)
        if m and path.is_dir() and not path.is_symlink() and path != keep:
            found.append(((m.group(2), int(m.group(3) or 1)), path))
    found.sort(reverse=True)
    others = PRE_MIGRATE_KEEP - (1 if keep is not None else 0)
    for _, path in found[others:]:
        shutil.rmtree(path, ignore_errors=True)


class TooManyAttempts(VaultError):
    status, detail, code = 429, "too many attempts", "rate_limited"

    def __init__(self, retry_after: int) -> None:
        super().__init__(self.detail)
        self.retry_after = retry_after


def check_password_policy(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPassword()
    if len(password) > MAX_PASSWORD_LENGTH:
        err = WeakPassword()
        err.detail = f"password must be at most {MAX_PASSWORD_LENGTH} characters"
        raise err


class Vault:
    """Owns the lock state: the DB engine/key, the single session and the limiter."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db = dbmod.Database(settings.db_path, before_migrate=self._pre_migrate_copy)
        # Kept in data/limiter.json so a restart doesn't reset the wrong-try count.
        self.limiter = RateLimiter(settings.data_dir / "limiter.json")
        self.soft_limiter = SoftLimiter()
        self._lock = threading.RLock()
        self._session_id: str | None = None
        self._session_token: str | None = None
        # The app window (tab id, not a secret) the session belongs to; the SHA-256 of the
        # token another window took over (its requests then get 401 "moved" instead of
        # "locked"); why the vault last locked. The moved digest is cleared by every lock.
        self._session_tab: str | None = None
        self._moved_digest: bytes | None = None
        self.lock_reason: str | None = None
        self._last_activity = 0.0
        # Runs while still unlocked, right before a lock wipes the key (Release 3: the
        # automatic backup). Its failures are logged and never block locking.
        self.before_lock: Callable[[], object] | None = None
        # Release 3.4: run after the database opens (setup, unlock, restore, password change)
        # and after every lock (``_lock_now``), to load and drop the Plaid keys saved in the
        # vault. Failures are logged and never block unlocking or locking.
        self.after_unlock: Callable[[], object] | None = None
        self.after_lock: Callable[[], object] | None = None
        # Updates: called with the error when the right key was given but the database
        # couldn't be opened or upgraded. Returns the version FinTrack is going back to when
        # that was a just-installed update's data upgrade (it has asked the launcher to roll
        # back), else None (the error is then raised as it is).
        self.on_open_failed: Callable[[BaseException], str | None] | None = None
        # Set by the first successful unlock/recover open in this process (see _open_checked).
        self.opened_once = False
        # Whether the current (or last) ``_open_checked`` started a schema upgrade: reset at
        # the start of each open, set by ``_pre_migrate_copy`` (the ``before_migrate`` hook).
        # A failed open that didn't start one (data already at the latest schema) is never a
        # failed data upgrade, so it never rolls an update back.
        self.migration_started = False
        # A new recovery sheet shown in Settings, until Done (activate) or any lock.
        self._pending: PendingSheet | None = None
        # Settings (D7): the auto-lock minutes saved in the vault, applied while it is open
        # (set after every unlock and by PATCH /api/settings; every lock drops it).
        self._auto_lock_minutes: int | None = None

    # --- paths / state

    @property
    def keyfile(self) -> Path:
        return self.settings.keyfile_path

    @property
    def keyfile_new(self) -> Path:
        return self.keyfile.with_name(self.keyfile.name + ".new")

    def _pre_migrate_copy(self, old_version: int) -> None:
        """Before ``Database.open`` upgrades the vault: copy its encrypted files aside."""
        self.migration_started = True
        db_path = self.settings.db_path
        pre_migrate_copy(
            self.settings.data_dir,
            [db_path, *_sidecars(db_path), self.keyfile, self.keyfile_new],
            old_version,
        )

    @property
    def initialized(self) -> bool:
        path = self.settings.db_path
        return path.exists() and path.stat().st_size > 0

    @property
    def unlocked(self) -> bool:
        return self.db.is_open

    @property
    def auto_lock_minutes(self) -> int:
        """The idle timeout in force: the vault's saved choice while open, else the env value."""
        minutes = self._auto_lock_minutes
        return minutes if minutes is not None and self.db.is_open else self.settings.auto_lock_minutes

    def set_auto_lock(self, minutes: int | None) -> None:
        """Apply the vault's saved auto-lock minutes (None = the env value). Ignored while
        locked: the next unlock reads the saved value again."""
        with self._lock:
            self._auto_lock_minutes = minutes if self.db.is_open else None

    @property
    def idle_seconds(self) -> float:
        return self.auto_lock_minutes * 60

    # --- sessions

    def _takeover_digest(self, tab: str | None) -> bytes | None:
        """The digest to remember when a new session for ``tab`` replaces the current one:
        None when there is none, or when the same window replaces its own session."""
        token = self._session_token
        if token is None or (tab is not None and tab == self._session_tab):
            return None
        return _token_digest(token)

    def _new_session(self, tab: str | None = None, *, keep_pending: bool = False, holder: bool = False) -> NewSession:
        """A new session replaces the old one and is bound to window ``tab``. A pending new
        sheet dies with it, unless ``keep_pending`` (same person, same tab: change password)
        moves it over. Unless ``holder`` (the caller proved it held the old session), the
        old token's digest is kept, so that window learns it was moved, not locked."""
        digest = None if holder else self._takeover_digest(tab)
        old = self._session_id
        self._session_id = secrets.token_urlsafe(32)
        self._session_token = secrets.token_urlsafe(32)
        self._session_tab = tab
        if digest is not None:
            self._moved_digest = digest
        self._last_activity = now()
        p = self._pending
        if p is not None:
            if keep_pending and old is not None and p.session_id == old:
                p.session_id = self._session_id
            else:
                self._drop_pending()
        return NewSession(self._session_id, self._session_token)

    def _drop_pending(self) -> None:
        p, self._pending = self._pending, None
        if p is not None:
            rec.wipe(p.key)

    def check_idle(self) -> None:
        with self._lock:
            p = self._pending
            if p is not None and now() > p.expires:
                self._drop_pending()  # an expired new sheet's key isn't kept around until Done
            if self.db.is_open and now() - self._last_activity > self.idle_seconds:
                log.info("auto-locking after inactivity")
                self._run_before_lock()
                self._lock_now("idle")

    def _matches(self, session_id: str | None, session_token: str | None) -> bool:
        if not (self.db.is_open and self._session_id and self._session_token and session_id and session_token):
            return False
        id_ok = hmac.compare_digest(session_id.encode(), self._session_id.encode())
        token_ok = hmac.compare_digest(session_token.encode(), self._session_token.encode())
        return id_ok and token_ok

    def _is_moved(self, session_token: str | None) -> bool:
        digest = self._moved_digest
        return bool(
            session_token and digest is not None and self.db.is_open
            and hmac.compare_digest(_token_digest(session_token), digest)
        )

    def session_state(self, session_id: str | None, session_token: str | None, *, touch: bool) -> str:
        """``SESSION_OK``, ``SESSION_MOVED`` (the token of a session another window took
        over, until the next lock) or ``SESSION_LOCKED`` (anything else).

        Both the cookie and the header token must match.
        Browsers share cookies across every port on 127.0.0.1, so another local server
        could receive the cookie. The token lives in the page's port-isolated
        sessionStorage and is sent as a header, so a leaked cookie alone is useless.
        "Moved" is decided by the token alone: the cookie is shared by every window of the
        browser, so the moved window already carries the new session's cookie.
        """
        with self._lock:
            self.check_idle()
            if self._matches(session_id, session_token):
                if touch:
                    self._last_activity = now()
                return SESSION_OK
            return SESSION_MOVED if self._is_moved(session_token) else SESSION_LOCKED

    def session_valid(self, session_id: str | None, session_token: str | None, *, touch: bool) -> bool:
        return self.session_state(session_id, session_token, touch=touch) == SESSION_OK

    def presence_view(self, session_id: str | None, session_token: str | None) -> PresenceView:
        """For status and heartbeats: never activity (the idle auto-lock still runs first)."""
        with self._lock:
            state = self.session_state(session_id, session_token, touch=False)
            is_open = self.db.is_open
            return PresenceView(state, is_open, self._session_tab if is_open else None, self.lock_reason)

    def bound_tab(self) -> str | None:
        with self._lock:
            return self._session_tab if self.db.is_open and self._session_id else None

    def handoff(self, session_id: str | None, session_token: str | None, to_tab: str) -> tuple[str, str | None]:
        """Open here: move the caller's session to window ``to_tab``.

        ``(SESSION_OK, new token)``, or ``(state, None)`` when the caller has no valid session.
        The token rotates (the cookie, shared by the browser's windows, stays); the old
        token then answers "moved". A pending new recovery sheet is dropped.
        """
        with self._lock:
            state = self.session_state(session_id, session_token, touch=True)
            if state != SESSION_OK:
                return state, None
            assert self._session_token is not None
            self._moved_digest = _token_digest(self._session_token)
            self._session_token = secrets.token_urlsafe(32)
            self._session_tab = to_tab
            self._drop_pending()
            log.info("session moved to another window")
            return SESSION_OK, self._session_token

    def lock_if_bound(self, tab: str, reason: str) -> bool:
        """Lock (backing up first) only if the session belongs to window ``tab``."""
        with self._lock:
            if not (self.db.is_open and self._session_id and self._session_tab == tab):
                return False
            log.info("locking: the FinTrack window was closed")
            self._run_before_lock()
            self._lock_now(reason)
            return True

    # --- rate limiting

    def _guard(self) -> None:
        retry = self.limiter.retry_after()
        if retry is not None:
            raise TooManyAttempts(retry)

    def _fail(self, error: type[InvalidPassword] = InvalidPassword, extra: dict | None = None) -> NoReturn:
        lockout = self.limiter.record_failure()
        if lockout is not None:
            raise TooManyAttempts(lockout)
        raise error(tries_left=self.limiter.tries_left(), extra=extra)

    # --- operations

    def setup(self, password: str, tab: str | None = None) -> SetupResult:
        """Create the vault and its recovery sheet (active, ``confirmed: false`` until the
        setup screen's Continue). The groups are returned once and never stored."""
        with self._lock:
            self._guard()
            if self.initialized:
                self.limiter.record_failure()
                raise AlreadyInitialized()
            check_password_policy(password)
            self.settings.data_dir.mkdir(parents=True, exist_ok=True)
            # Before any vault file exists, so every file is created private.
            restrict_to_owner(self.settings.data_dir)
            params = KeyParams.new()
            key = params.derive(password)
            try:
                groups, kdf, pub = _new_sheet(rec.RecoveryKdf.new())
                block = rec.new_block(key, pub, kdf, params.salt, confirmed=False)
                # Keyfile first: a crash before the DB exists leaves the app uninitialized
                # (initialized == DB file exists), so setup can simply be retried.
                write_json_atomic(self.keyfile, keyfile_json(params, block))
                self.keyfile_new.unlink(missing_ok=True)
                try:
                    self.db.open(key, create_schema=True)
                    write_pub_rows(self.db, pub, None)
                except BaseException:
                    self.db.close()
                    self.settings.db_path.unlink(missing_ok=True)
                    raise
            finally:
                dbmod._wipe(key)
            self.limiter.reset()
            self._run_after_unlock()
            return SetupResult(self._new_session(tab), groups, block.sheet, block.created_at)

    def unlock(self, password: str, tab: str | None = None) -> NewSession:
        with self._lock:
            self._guard()
            if not self.initialized:
                raise NotInitialized()
            key = self._find_working_key(password)
            if key is None:
                self._fail()
            assert key is not None
            try:
                if not self.db.is_open:
                    self._open_checked(key)
            finally:
                dbmod._wipe(key)
            self.limiter.reset()
            self._run_after_unlock()
            return self._new_session(tab)

    def _open_checked(self, key: bytes | bytearray) -> None:
        """Open (and upgrade) the database with a key known to be right. SchemaTooNew → 409
        ``schema_too_new``; a data upgrade that fails while a just-installed update is pending
        → ``UpdateRolledBack`` (the updater asked the launcher to go back); anything else is
        raised as it is (a VaultError such as PreMigrationCopyFailed changed nothing)."""
        self.migration_started = False
        try:
            self.db.open(key)
            # This process's version has opened (and upgraded) the data: from now on a
            # failed open is never answered by going back to the pre-update copy.
            self.opened_once = True
        except SchemaTooNew:
            raise SchemaTooNewError() from None
        except VaultError:
            raise  # e.g. PreMigrationCopyFailed: nothing was changed, the user can try again
        except Exception as exc:
            to_version = self._open_failed(exc)
            if to_version is not None:
                raise UpdateRolledBack(extra={"to_version": to_version}) from None
            raise

    def _open_failed(self, exc: BaseException) -> str | None:
        hook = self.on_open_failed
        if hook is None:
            return None
        try:
            return hook(exc)
        except Exception as exc:  # noqa: BLE001 - the original error is what the user gets then
            log.error("update rollback request failed: %s", type(exc).__name__)
            return None

    def _find_working_key(self, password: str) -> bytearray | None:
        """Try keyfile.json, then keyfile.json.new (crash recovery after a rekey)."""
        db_path = self.settings.db_path
        candidates = [(self.keyfile, read_keyfile(self.keyfile))]
        if self.keyfile_new.exists():
            candidates.append((self.keyfile_new, read_keyfile(self.keyfile_new)))
        for path, params in candidates:
            if params is None:
                continue
            key = params.derive(password)
            if dbmod.verify_key(db_path, key):
                if path == self.keyfile_new:
                    _replace_with_retry(self.keyfile_new, self.keyfile)
                    log.info("finalized interrupted password change")
                else:
                    self.keyfile_new.unlink(missing_ok=True)
                return key
            dbmod._wipe(key)
        return None

    def change_password(self, current: str, new: str, tab: str | None = None) -> NewSession:
        with self._lock:
            self._guard()
            if not self.db.is_open or self.db.key is None:
                raise dbmod.DatabaseLocked()
            params, block = read_keyfile_full(self.keyfile)
            current_key = params.derive(current) if params else bytearray(KEY_BYTES)
            held = bytearray(self.db.key)
            try:
                if not hmac.compare_digest(bytes(current_key), bytes(held)):
                    self._fail()
                check_password_policy(new)
                self._rekey_to(held, new, block, trusted=self._trusted(block, held))
            finally:
                dbmod._wipe(current_key)
                dbmod._wipe(held)
            self.limiter.reset()
            self._run_after_unlock()  # same keys; a harmless reload after the reopen
            # Same person, same tab: a new sheet waiting for Done stays valid. Its K' came
            # from the old password, so it is derived again from the new one (fresh salt);
            # otherwise Done would leave a vault the new password can't open.
            p = self._pending
            if p is not None and p.session_id is not None and p.session_id == self._session_id:
                # Derive into locals first: the pending sheet only ever holds a complete K'.
                # If this Argon2 run fails, the pending sheet is dropped (Done then answers
                # "pending_expired"), never left holding a wiped (all-zero) key. The password
                # change itself is already done, so an ordinary failure here doesn't fail it.
                try:
                    new_params = KeyParams.new()
                    new_pending_key = new_params.derive(new)
                except Exception as exc:  # noqa: BLE001 - e.g. MemoryError from Argon2
                    self._drop_pending()
                    log.warning("dropped the new recovery sheet after a password change: %s", type(exc).__name__)
                except BaseException:
                    self._drop_pending()
                    raise
                else:
                    old_key, p.params, p.key = p.key, new_params, new_pending_key
                    rec.wipe(old_key)
            return self._new_session(tab or self._session_tab, keep_pending=True, holder=True)

    def _trusted(self, block: rec.RecoveryBlock | None, key: bytes | bytearray | None) -> bool:
        """Whether ``block`` may be re-sealed or kept: its MAC checks out under ``key`` (the
        key the open database holds) and its public key is the one on record in the database.
        Needs the database open (locked -> False)."""
        return block is not None and block.mac_ok(key) and pub_on_record(self.db, block.public_key)

    def _rekey_to(
        self, held: bytearray, new_password: str, block: rec.RecoveryBlock | None, *, trusted: bool
    ) -> None:
        """Re-encrypt the vault from key ``held`` to a key derived from ``new_password``.

        Shared by change-password and recover. The recovery sheet keeps working: its block
        is re-sealed to the same public key for the new key, but only if ``trusted`` (see
        ``_trusted``; a planted or rolled-back public key is never given the key; a block that
        fails is kept as it was, so it shows as out of date).
        """
        new_params = KeyParams.new()
        new_key = new_params.derive(new_password)
        try:
            new_block = block
            if block is not None:
                if trusted:
                    new_block = block.resealed(new_key, new_params.salt)
                else:
                    log.warning("recovery block failed its integrity check; not re-sealed")
            self._rekey_with(held, new_params, new_key, new_block)
        finally:
            dbmod._wipe(new_key)

    def _rekey_with(
        self,
        held: bytearray,
        new_params: KeyParams,
        new_key: bytearray,
        new_block: rec.RecoveryBlock | None,
        *,
        after_rekey_error: type[VaultError] | None = None,
    ) -> None:
        """Re-encrypt the vault from ``held`` to ``new_key`` and install ``new_params`` with
        ``new_block``. ``keyfile.json.new`` is written first; a crash after the rekey is
        finished by the next unlock or recover, which try it too. Ends with the database open
        with the new key, or locked if that fails. The caller owns (and wipes) both keys;
        ``held`` must be a copy, since closing the database wipes its own key.

        ``after_rekey_error``: raised (from None) instead of an ordinary failure that happens
        once the database is re-encrypted, so the caller can tell the user the step is finished
        by the next unlock rather than "nothing was saved".
        """
        was_open = self.db.is_open
        write_json_atomic(self.keyfile_new, keyfile_json(new_params, new_block))
        self.db.close()
        try:
            try:
                dbmod.rekey(self.settings.db_path, held, new_key)
            except BaseException:
                self.keyfile_new.unlink(missing_ok=True)
                if was_open:
                    self.db.open(held)
                raise
            try:
                _replace_with_retry(self.keyfile_new, self.keyfile)
                self.db.open(new_key)
            except Exception as exc:
                if after_rekey_error is None:
                    raise
                log.error("could not finish after the rekey (finished by the next unlock): %s", type(exc).__name__)
                raise after_rekey_error() from None
        except BaseException:
            if not self.db.is_open:
                # Left locked: finish the lock (session, after-lock hook -> Plaid client).
                self._lock_now(None)
            raise

    # --- recovery sheet

    def _recovery_code(self, code: str) -> str:
        """Checks that cost nothing and are never counted: format, then per-group typos."""
        try:
            code36 = rec.normalize(code)
        except rec.CodeFormatError:
            raise CodeFormatError() from None
        bad = rec.bad_groups(code36)
        if bad:
            raise CodeTypo(extra={"groups": bad})
        return code36

    def _keyfile_candidates(self) -> list[tuple[Path, KeyParams | None, rec.RecoveryBlock | None]]:
        out = [(self.keyfile, *read_keyfile_full(self.keyfile))]
        if self.keyfile_new.exists():
            out.append((self.keyfile_new, *read_keyfile_full(self.keyfile_new)))
        return out

    def _find_recovery_key(
        self, code36: str, candidates: list[tuple[Path, KeyParams | None, rec.RecoveryBlock | None]]
    ) -> tuple[bytearray, rec.RecoveryBlock]:
        """Like ``_find_working_key`` for the sheet: keyfile.json, then keyfile.json.new.

        Returns the vault key (the caller wipes it) and the block it came from. Raises
        ``SheetUnusable`` (the sheet's public key matches but nothing opens: not counted),
        ``SheetOutdated`` (an older sheet of this vault: not counted), else counts a failure
        (``NoMatch`` 401 or ``TooManyAttempts``).
        """
        db_path = self.settings.db_path
        data = rec.data_digits(code36)
        seeds: dict[rec.RecoveryKdf, bytearray] = {}
        pub_matched = outdated = False
        active_sheet: str | None = None
        try:
            for path, params, block in candidates:
                if params is None or block is None:
                    continue
                if active_sheet is None:
                    active_sheet = block.sheet
                seed = seeds.get(block.kdf)
                if seed is None:
                    seed = seeds[block.kdf] = block.kdf.seed(data)
                pub = rec.public_key(seed)
                if hmac.compare_digest(pub, block.public_key):
                    pub_matched = True
                    try:
                        key = rec.unseal(seed, pub, block.sealed)
                    except rec.Unusable:
                        continue
                    if dbmod.verify_key(db_path, key):
                        if path == self.keyfile_new:
                            _replace_with_retry(self.keyfile_new, self.keyfile)
                            log.info("finalized interrupted password change")
                        else:
                            self.keyfile_new.unlink(missing_ok=True)
                        return key, block
                    dbmod._wipe(key)
                if block.is_retired(pub):
                    outdated = True
        finally:
            rec.wipe(data)
            for seed in seeds.values():
                rec.wipe(seed)
        extra = {"active_sheet": active_sheet} if active_sheet else {}
        # Outdated wins over unusable: after a crash between a new sheet's rekey and the
        # rename, keyfile.json still holds the old sheet (whose key opens nothing now) while
        # keyfile.json.new has already retired it.
        if outdated:
            self.soft_limiter.record()
            raise SheetOutdated(extra=extra)
        if pub_matched:
            self.soft_limiter.record()
            log.warning("a recovery sheet matched but could not open the vault")
            raise SheetUnusable(extra=extra)
        self._fail(NoMatch, extra=extra)

    def _recovery_start(self, code: str, new_password: str | None = None) -> tuple[bytearray, rec.RecoveryBlock]:
        self._guard()
        if not self.initialized:
            raise NotInitialized()
        code36 = self._recovery_code(code)
        if new_password is not None:
            check_password_policy(new_password)
        candidates = self._keyfile_candidates()
        if not any(block is not None for _, params, block in candidates if params is not None):
            raise NoRecovery()
        # "Outdated" and "unusable" aren't counted as wrong tries, but each costs an Argon2
        # run: a separate soft limit keeps them from being repeated without end.
        retry = self.soft_limiter.retry_after()
        if retry is not None:
            raise TooManyAttempts(retry)
        return self._find_recovery_key(code36, candidates)

    def recover_check(self, code: str) -> str:
        """Check a sheet before asking for a new password; returns its number. Wrong codes
        count toward the shared limiter; success does not reset it (``recover`` does)."""
        with self._lock:
            key, block = self._recovery_start(code)
            dbmod._wipe(key)
            return block.sheet

    def recover(self, code: str, new_password: str, tab: str | None = None) -> NewSession:
        """Set a new password with the sheet. Any other open session is locked first (its
        window then learns it was moved)."""
        with self._lock:
            key, block = self._recovery_start(code, new_password)
            moved = self._takeover_digest(tab)
            try:
                if self.db.is_open:
                    self._run_before_lock()
                    self._lock_now(None)
                # Open (and upgrade) the data with the sheet's key BEFORE anything is
                # re-encrypted. If a just-installed update can't upgrade it, FinTrack goes
                # back (UpdateRolledBack) with keyfile.json untouched, so the pre-update copy
                # the launcher restores still opens with this same recovery sheet; a data
                # format that is too new is 409 schema_too_new. Nothing is saved either way.
                self._open_checked(key)
                try:
                    # This block's own sealed key just opened the vault, and every new sheet
                    # rotates the key, so no other block is sealed to it (as before, only its
                    # MAC decides whether it is re-sealed).
                    self._rekey_to(key, new_password, block, trusted=block.mac_ok(key))
                except BaseException:
                    if self.db.is_open:  # e.g. keyfile.json.new couldn't be written
                        self._lock_now(None)
                    raise
            finally:
                dbmod._wipe(key)
            self.limiter.reset()
            log.info("password reset with the recovery sheet")
            self._run_after_unlock()
            session = self._new_session(tab)
            if moved is not None:
                self._moved_digest = moved
            return session

    def recovery_public(self) -> dict:
        """For ``/api/auth/status`` (no session): whether a sheet exists and its number.

        Under the vault lock: on Windows an open handle would make a concurrent
        ``os.replace`` of keyfile.json (password change, new sheet) fail.
        """
        with self._lock:
            block = read_keyfile_full(self.keyfile, quiet=True)[1] if self.initialized else None
        return {"available": block is not None, "sheet": block.sheet if block else None}

    def _status_of(self, params: KeyParams | None, block: rec.RecoveryBlock | None) -> dict:
        if params is None or block is None:
            return {"status": "none", "sheet": None, "created_at": None}
        if not self._trusted(block, self.db.key) or not hmac.compare_digest(block.sealed.for_salt, params.salt):
            state = "stale"
        else:
            state = "active" if block.confirmed else "unconfirmed"
        return {"status": state, "sheet": block.sheet, "created_at": block.created_at}

    def _require_open(self) -> bytearray:
        if not self.db.is_open or self.db.key is None:
            raise dbmod.DatabaseLocked()
        return self.db.key

    def recovery_status(self) -> dict:
        with self._lock:
            self._require_open()
            return self._status_of(*read_keyfile_full(self.keyfile))

    def create_pending(self, current_password: str | None) -> dict:
        """A new sheet for Settings, after the password. Nothing changes on disk until
        ``activate_pending``; kept in memory: its public key (and KDF salt), and the key K'
        (new salt, the password just checked) that Done rekeys the vault to."""
        with self._lock:
            self.confirm_current_password(
                current_password, missing_detail="Enter your Iron Owl password to make a new recovery sheet."
            )
            assert current_password is not None
            key = self._require_open()
            _, block = read_keyfile_full(self.keyfile)
            valid = self._trusted(block, key)
            # Same KDF salt as the current sheet, so one Argon2 run also recognizes old sheets.
            kdf = block.kdf if valid else rec.RecoveryKdf.new()
            taken = {block.sheet, *(rec.sheet_number(r.public_key) for r in block.retired)} if valid else set()
            for _ in range(20):
                groups, kdf, pub = _new_sheet(kdf)
                if rec.sheet_number(pub) not in taken:
                    break
            created = rec.utc_now_iso()
            new_params = KeyParams.new()
            new_key = new_params.derive(current_password)
            self._drop_pending()
            self._pending = PendingSheet(
                id=secrets.token_urlsafe(24), session_id=self._session_id, public_key=pub, kdf=kdf,
                created_at=created, expires=now() + PENDING_SECONDS, params=new_params, key=new_key,
            )
            return {"pending_id": self._pending.id, "sheet": rec.sheet_number(pub), "created_at": created, "groups": groups}

    def activate_pending(self, pending_id: str) -> dict:
        """Done: rekey the vault to the pending K' and seal K' to the new sheet; the old
        sheet retires. Rotating the key is what revokes the old sheet: its block (still in
        every older backup and keyfile copy) unseals a key that opens nothing current."""
        with self._lock:
            self._require_open()
            p = self._pending
            if p is not None and now() > p.expires:
                self._drop_pending()
                p = None
            if p is None or p.session_id is None or p.session_id != self._session_id:
                self._drop_pending()
                raise PendingExpired()
            if not hmac.compare_digest(p.id.encode(), pending_id.encode("utf-8", "surrogatepass")):
                raise PendingExpired()
            if len(p.key) != KEY_BYTES or not any(p.key):
                # Never rekey the vault to a wiped or malformed key.
                log.error("refused a new recovery sheet whose key is not usable")
                self._drop_pending()
                raise PendingExpired()
            self._pending = None  # used up, whatever happens next; p.key is wiped below
            held = bytearray(self.db.key or b"")  # a copy: closing the database wipes its own
            try:
                params, block = read_keyfile_full(self.keyfile)
                if params is None:
                    raise PendingExpired()
                retired: tuple[rec.Retired, ...] = ()
                old_pub: bytes | None = None
                if self._trusted(block, held):
                    assert block is not None
                    old = rec.Retired(block.public_key, block.created_at, rec.utc_now_iso())
                    retired = rec.merge_retired([old], block.retired, exclude=p.public_key)
                    old_pub = block.public_key
                new = rec.new_block(p.key, p.public_key, p.kdf, p.params.salt, confirmed=True,
                                    created_at=p.created_at, retired=retired)
                # On record before the rekey, with the old sheet as ``prev``: after a crash on
                # either side of the rekey, the block whose MAC checks out under the key the
                # vault ends up with is on record.
                write_pub_rows(self.db, p.public_key, old_pub)
                self._rekey_with(held, p.params, p.key, new, after_rekey_error=FinishOnUnlock)
                try:
                    write_pub_rows(self.db, p.public_key, None)
                except Exception as exc:  # noqa: BLE001 - done already; a stale prev is harmless
                    log.warning("could not clear the previous recovery sheet: %s", type(exc).__name__)
            finally:
                dbmod._wipe(held)
                rec.wipe(p.key)
            log.info("new recovery sheet is active")
            self._run_after_unlock()  # same data, new key; a harmless reload after the reopen
            return self._status_of(p.params, new)

    def confirm_sheet(self, sheet: str) -> dict:
        """The setup screen's Continue: the user has the sheet on paper."""
        with self._lock:
            key = self._require_open()
            params, block = read_keyfile_full(self.keyfile)
            if params is None or block is None or block.sheet != sheet or not self._trusted(block, key):
                raise SheetChanged()
            if not block.confirmed:
                block = replace(block, confirmed=True).signed(key)
                write_json_atomic(self.keyfile, keyfile_json(params, block))
            return self._status_of(params, block)

    def verify_password(self, password: str) -> None:
        """Re-check the vault password (e.g. before a backup); failures count toward the limiter."""
        with self._lock:
            self._guard()
            if not self.db.is_open or self.db.key is None:
                raise dbmod.DatabaseLocked()
            params = read_keyfile(self.keyfile)
            candidate = params.derive(password) if params else bytearray(KEY_BYTES)
            held = bytearray(self.db.key)
            try:
                ok = hmac.compare_digest(bytes(candidate), bytes(held))
            finally:
                dbmod._wipe(candidate)
                dbmod._wipe(held)
            if not ok:
                self._fail()
            self.limiter.reset()

    def confirm_current_password(self, password: str | None, *, missing_detail: str | None = None) -> None:
        """Re-auth before a sensitive change: a live session alone is not enough.

        Empty -> ``CurrentPasswordRequired`` (422, ``missing_detail`` if given, not counted);
        wrong -> ``WrongCurrentPassword`` (401, counted by the shared limiter); the limiter's
        ``TooManyAttempts`` (429) passes through.
        """
        if not password:
            err = CurrentPasswordRequired()
            if missing_detail:
                err.detail = missing_detail
            raise err
        try:
            self.verify_password(password)
        except InvalidPassword as exc:
            raise WrongCurrentPassword(tries_left=exc.tries_left) from None

    def restore(
        self,
        staged_db: Path,
        staged_keyfile: Path,
        password: str,
        *,
        require_uninitialized: bool,
        session: tuple[str | None, str | None] | None = None,
        current_password: str | None = None,
        tab: str | None = None,
    ) -> NewSession:
        """Install a validated backup and unlock it.

        In order: check the password against the backup, lock, move the current vault
        files to ``data/pre-restore-<timestamp>/``, install, run migrations and unlock
        (``Database.open``). Any failure after the move puts the previous files back.

        ``session`` (cookie, token) is re-checked here, under the vault lock: the caller
        checked it before a possibly long upload, and the vault may have been locked since.
        """
        with self._lock:
            self._guard()
            if require_uninitialized and self.initialized:
                raise AlreadyInitialized()
            if not require_uninitialized:
                if session is None or not self.session_valid(*session, touch=False):
                    raise SessionEnded()
                # Replacing an existing vault needs its password, not just a live session:
                # a session left unlocked must not be enough to swap out the user's data.
                self.confirm_current_password(current_password)
            params, backup_block = read_keyfile_full(staged_keyfile)
            if params is None:
                raise BadBackup("The backup's keyfile is missing or invalid.")
            key = params.derive(password)
            new_key: bytearray | None = None
            try:
                if not dbmod.verify_key(staged_db, key):
                    try:
                        self._fail()
                    except InvalidPassword as exc:
                        raise WrongBackupPassword(tries_left=exc.tries_left) from None
                # Run the migrations on the staged copy first, while the current vault is
                # still untouched: a backup whose database can't be opened or upgraded is a
                # bad file (400), not a reason to lock and swap out the working vault.
                staged = dbmod.Database(staged_db)
                try:
                    staged.open(key)
                except Exception:  # noqa: BLE001 - untrusted file: any failure is a bad backup
                    raise BadBackup("The backup's database could not be opened or upgraded.") from None
                try:
                    # While the current vault is still open (its key proves its block): which
                    # recovery sheet opens the restored data, and whether its key rotates.
                    install_params, block, new_key = self._restored_keyfile(
                        key, params, backup_block, staged, password, in_app=not require_uninitialized
                    )
                finally:
                    staged.close()
                if new_key is not None:
                    dbmod.rekey(staged_db, key, new_key)
                if block is not backup_block or install_params is not params:
                    write_json_atomic(staged_keyfile, keyfile_json(install_params, block))
                install_key = new_key if new_key is not None else key

                # Restoring locks the current vault like any other lock: back it up first.
                self._run_before_lock()
                self._lock_now(None)
                data_dir = self.settings.data_dir
                data_dir.mkdir(parents=True, exist_ok=True)
                db_path = self.settings.db_path
                current = [
                    p for p in (db_path, self.keyfile, self.keyfile_new, *_sidecars(db_path)) if p.exists()
                ]
                moved_to: Path | None = None
                if current:
                    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
                    moved_to = data_dir / f"pre-restore-{stamp}"
                    n = 1
                    while moved_to.exists():
                        n += 1
                        moved_to = data_dir / f"pre-restore-{stamp}-{n}"
                    moved_to.mkdir()
                    for path in current:
                        os.replace(path, moved_to / path.name)
                try:
                    os.replace(staged_keyfile, self.keyfile)
                    os.replace(staged_db, db_path)
                    self.db.open(install_key)  # runs pending migrations
                except BaseException:
                    self._lock_now(None)
                    # Remove the installed backup and any sidecar it produced, so no journal
                    # of the backup DB is left next to the previous vault when it moves back.
                    for path in (db_path, self.keyfile, *_sidecars(db_path)):
                        path.unlink(missing_ok=True)
                    if moved_to is not None:
                        for path in moved_to.iterdir():
                            os.replace(path, data_dir / path.name)
                        moved_to.rmdir()
                    raise
            finally:
                dbmod._wipe(key)
                if new_key is not None:
                    dbmod._wipe(new_key)
            self.limiter.reset()
            log.info("restored vault from backup")
            self._run_after_unlock()  # the restored vault may hold other Plaid keys
            # In-app, the caller held the session (checked above): same person, not a move.
            return self._new_session(tab, holder=True)

    def _restored_keyfile(
        self,
        key: bytearray,
        params: KeyParams,
        backup_block: rec.RecoveryBlock | None,
        staged: dbmod.Database,
        password: str,
        *,
        in_app: bool,
    ) -> tuple[KeyParams, rec.RecoveryBlock | None, bytearray | None]:
        """``(params, block, new_key)`` to install with a restored backup (key = the backup's
        key, ``staged`` = its database, open with it).

        In-app restore with a valid current sheet: that sheet keeps working, and the restored
        vault gets a fresh key K' (new salt, the backup's password) sealed to it, so neither
        the backup's own sheets nor any copy of the backup's keyfile opens the restored vault;
        the backup's sheets become "out of date". ``new_key`` is then K' (the caller rekeys the
        staged database to it and wipes it); the public key on record in ``staged`` is set.
        Otherwise (restore at setup, or no valid current sheet) the backup's block is kept if
        it checks out (MAC, salt, and on record in the backup's database), and ``new_key`` is
        None.
        """
        backup_ok = backup_block is not None and backup_block.mac_ok(key)
        if in_app:
            _, current = read_keyfile_full(self.keyfile)
            if current is not None and self._trusted(current, self.db.key):
                from_backup: list[rec.Retired] = []
                if backup_ok:
                    assert backup_block is not None
                    from_backup = [
                        rec.Retired(backup_block.public_key, backup_block.created_at, rec.utc_now_iso()),
                        *backup_block.retired,
                    ]
                retired = rec.merge_retired(current.retired, from_backup, exclude=current.public_key)
                new_params = KeyParams.new()
                new_key = new_params.derive(password)
                try:
                    write_pub_rows(staged, current.public_key, None)
                    block = replace(current, retired=retired).resealed(new_key, new_params.salt)
                except BaseException:
                    dbmod._wipe(new_key)
                    raise
                return new_params, block, new_key
        if (
            backup_ok
            and hmac.compare_digest(backup_block.sealed.for_salt, params.salt)
            and pub_on_record(staged, backup_block.public_key)
        ):
            # The backup's own sheet is kept, but nothing says the user still has that paper
            # (it may be the lost one): shown as "not finished", so Settings and Home ask for
            # a new sheet.
            if backup_block.confirmed:
                return params, replace(backup_block, confirmed=False).signed(key), None
            return params, backup_block, None
        return params, None, None

    def lock(self, reason: str | None = "manual") -> None:
        """Lock for any reason (manual lock, server shutdown); auto-lock is ``check_idle``."""
        with self._lock:
            self._run_before_lock()
            self._lock_now(reason)

    def _run_before_lock(self) -> None:
        hook = self.before_lock
        if hook is None or not self.db.is_open:
            return
        try:
            hook()
        except Exception as exc:  # noqa: BLE001 - locking must never fail
            log.error("pre-lock task failed: %s", type(exc).__name__)

    def _run_after_unlock(self) -> None:
        hook = self.after_unlock
        if hook is None or not self.db.is_open:
            return
        try:
            hook()
        except Exception as exc:  # noqa: BLE001 - unlocking must never fail because of it
            log.error("post-unlock task failed: %s", type(exc).__name__)

    def _run_after_lock(self) -> None:
        hook = self.after_lock
        if hook is None:
            return
        try:
            hook()
        except Exception as exc:  # noqa: BLE001 - locking must never fail
            log.error("post-lock task failed: %s", type(exc).__name__)

    def _lock_now(self, reason: str | None) -> None:
        """The single choke point for every lock (manual, idle, closed, shutdown, restore).

        ``reason`` (``LOCK_REASONS`` or None = unknown) is recorded only when something was
        open, so locking an already locked vault doesn't change why it locked.
        """
        if self.db.is_open or self._session_id is not None:
            self.lock_reason = reason
        self._session_id = None
        self._session_token = None
        self._session_tab = None
        self._moved_digest = None
        self._auto_lock_minutes = None  # locked: the env value applies until the next unlock
        self._drop_pending()  # a new sheet not yet activated is dropped (and its key wiped) by any lock
        try:
            self.db.close()
        finally:
            self._run_after_lock()


# ------------------------------------------------------------------- middleware

CSP = (
    "default-src 'self'; script-src 'self' https://cdn.plaid.com; "
    "frame-src https://cdn.plaid.com; connect-src 'self' https://*.plaid.com; "
    "img-src 'self' data:; style-src 'self' 'unsafe-inline'; object-src 'none'; "
    "base-uri 'none'; frame-ancestors 'none'"
)
SAFE_METHODS = frozenset({"GET", "HEAD"})


class SecurityMiddleware:
    """Host allowlist (anti DNS-rebinding), CSRF guard and security headers."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.allowed_hosts = {h.lower() for h in settings.allowed_hosts}
        self.allowed_origins = {o.lower() for o in settings.allowed_origins}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        is_api = path == "/api" or path.startswith("/api/")
        headers = Headers(scope=scope)

        started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                managed = _API_MANAGED_HEADERS if is_api else _MANAGED_HEADERS
                raw = [(k, v) for k, v in message.get("headers", []) if k.lower() not in managed]
                raw.extend(_SECURITY_HEADERS)
                if is_api:
                    raw.append((b"cache-control", b"no-store"))
                message["headers"] = raw
            await send(message)

        host = headers.get("host", "").lower()
        if host not in self.allowed_hosts:
            await JSONResponse({"detail": "invalid host"}, status_code=400)(
                scope, receive, send_wrapper
            )
            return

        if is_api and scope["method"] not in SAFE_METHODS:
            origin = headers.get("origin")
            if (
                headers.get(CSRF_HEADER) != "1"
                or (origin is not None and origin.lower() not in self.allowed_origins)
                or not _content_type_allowed(path, headers.get("content-type"))
            ):
                await JSONResponse({"detail": "forbidden"}, status_code=403)(
                    scope, receive, send_wrapper
                )
                return

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:  # noqa: BLE001
            # Log only the exception type: messages may carry data we must not log.
            log.error("unhandled error on %s %s: %s", scope["method"], path, type(exc).__name__)
            if not started:
                await JSONResponse({"detail": "internal error"}, status_code=500)(
                    scope, receive, send_wrapper
                )


RESTORE_PATH = "/api/restore"
UPDATE_FILE_PATH = "/api/update/file"
# The only exact paths that take multipart/form-data (file uploads).
MULTIPART_PATHS = frozenset({RESTORE_PATH, UPDATE_FILE_PATH})


def _content_type_allowed(path: str, content_type: str | None) -> bool:
    """State-changing API bodies are JSON; multipart is accepted only on the two upload
    paths (backup restore, update file), matched exactly.

    Form-encodable types (text/plain, x-www-form-urlencoded, multipart) are what a
    cross-site form can send without a preflight, so they are refused everywhere else.
    """
    if content_type is None:
        return True
    media = content_type.split(";", 1)[0].strip().lower()
    if media == "application/json":
        return True
    return media == "multipart/form-data" and path in MULTIPART_PATHS


_SECURITY_HEADERS = [
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
]
_MANAGED_HEADERS = {k for k, _ in _SECURITY_HEADERS}
_API_MANAGED_HEADERS = _MANAGED_HEADERS | {b"cache-control"}

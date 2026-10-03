"""Automatic encrypted backups and automatic Plaid sync (SPEC Release 3 sections 9 and 10).

Both only ever run while the vault is unlocked: their settings live inside the encrypted
database, and a backup is the same encrypted ``VACUUM INTO`` copy the manual backup makes.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import re
import secrets
import stat
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import DatabaseLocked
from ..models import AppSetting, PlaidItem
from ..utils import iso_utc, month_of, today, utcnow
from . import backup as backup_service

if TYPE_CHECKING:  # pragma: no cover
    from ..security import Vault

log = logging.getLogger("fintrack.automation")

# ------------------------------------------------------------------ settings

DIR_KEY = "auto_backup_dir"
KEEP_KEY = "auto_backup_keep"
LAST_AT_KEY = "auto_backup_last_at"
LAST_ERROR_KEY = "auto_backup_last_error"
# Home "Today": when the last failure happened and a short code for it (cleared on success).
LAST_ERROR_AT_KEY = "auto_backup_last_error_at"
LAST_ERROR_CODE_KEY = "auto_backup_last_error_code"
SYNC_HOURS_KEY = "auto_sync_hours"

DEFAULT_KEEP = 10
DEFAULT_SYNC_HOURS = 6
SYNC_CHOICES = (0, 3, 6, 12, 24)

MIN_GAP = dt.timedelta(minutes=10)
PERIOD = dt.timedelta(hours=24)
# After a failed periodic backup, wait this long before the loop tries again.
RETRY_AFTER_FAILURE = dt.timedelta(hours=1)
# "Back up now" (POST /api/backup/auto/run) starts at most once per this many seconds.
MANUAL_MIN_SECONDS = 60
BACKUP_ERROR_CODES = ("missing", "denied", "write", "network", "other")

# ASCII digits only (``\d`` would match other scripts' digits) and ``\Z`` (``$`` also matches
# before a trailing newline); list_files also requires a real date and time.
FILE_RE = re.compile(r"^fintrack-auto-([0-9]{8}-[0-9]{6})\.ftbackup\Z")


def now_utc() -> dt.datetime:
    """Naive UTC now (tests monkeypatch this)."""
    return utcnow()


def monotonic() -> float:
    """For the "Back up now" throttle (tests monkeypatch this)."""
    return time.monotonic()


def now_local() -> dt.datetime:
    """Local time, for the file name (tests monkeypatch this)."""
    return dt.datetime.now()


def _get(session: Session, key: str) -> str | None:
    row = session.get(AppSetting, key)
    return row.value if row is not None else None


def _put(session: Session, key: str, value: str | None) -> None:
    row = session.get(AppSetting, key)
    if value is None:
        if row is not None:
            session.delete(row)
    elif row is None:
        session.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    session.flush()


def _parse_utc(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.rstrip("Z"))
    except ValueError:
        return None


def _int(value: str | None, default: int, lo: int, hi: int) -> int:
    try:
        n = int(value) if value is not None else default
    except ValueError:
        return default
    return n if lo <= n <= hi else default


# ------------------------------------------------------------------ backup directory


class BackupDirError(ValueError):
    """The backup folder can't be used; the message is safe to show (422)."""

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code  # a BACKUP_ERROR_CODES value when the message alone doesn't say


DRIVE_UNKNOWN, DRIVE_NO_ROOT_DIR, DRIVE_REMOTE = 0, 1, 4
NETWORK_REFUSED = (
    "Network folders can't be used for automatic backups (Windows could send your sign-in "
    "to another computer). Pick a folder on this PC or a USB drive."
)
NO_DRIVE = "That drive doesn't exist."
NO_FOLDER = "That folder doesn't exist."
CANT_WRITE = "Iron Owl can't write to that folder."


def drive_type(root: str) -> int:
    """``GetDriveTypeW`` for a drive root like ``D:\\`` (tests monkeypatch this)."""
    if sys.platform != "win32":  # pragma: no cover - the app runs on Windows
        return 3  # DRIVE_FIXED
    import ctypes

    return int(ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root)))


def _is_network_text(text: str) -> bool:
    # \\server\share, //server/share, and the \\?\ / \\.\ device forms (\\?\UNC\...).
    return text[:2] in ("\\\\", "//", "\\/", "/\\")


def _check_drive(text: str) -> None:
    if sys.platform != "win32":  # pragma: no cover
        return
    kind = drive_type(text[:2].upper() + "\\")
    if kind == DRIVE_REMOTE:
        raise BackupDirError(NETWORK_REFUSED)
    if kind in (DRIVE_UNKNOWN, DRIVE_NO_ROOT_DIR):
        raise BackupDirError(NO_DRIVE)


_WIN_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")
_DEVICE_DRIVE = re.compile(r"^(?:\\\\\?\\|\\\?\?\\)([A-Za-z]:\\.*)\Z", re.DOTALL)
_DEVICE_VOLUME = re.compile(r"^(?:\\\\\?\\|\\\?\?\\)Volume\{[0-9A-Fa-f-]+\}\\", re.IGNORECASE)
IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003  # junctions and volume mount points
IO_REPARSE_TAG_SYMLINK = 0xA000000C
MAX_LINK_HOPS = 16


def link_target(path: str) -> str | None:
    """Where a symlink or junction points, read *without* following it; None otherwise.

    ``os.lstat`` opens the link itself (FILE_FLAG_OPEN_REPARSE_POINT), so a link to a
    network share is never contacted here. Tests monkeypatch this.
    """
    try:
        st = os.lstat(path)
    except OSError:
        raise BackupDirError(NO_FOLDER) from None
    tag = getattr(st, "st_reparse_tag", 0)
    if not (stat.S_ISLNK(st.st_mode) or tag in (IO_REPARSE_TAG_MOUNT_POINT, IO_REPARSE_TAG_SYMLINK)):
        return None
    try:
        return os.readlink(path)
    except (OSError, ValueError):
        raise BackupDirError("That folder is a link Iron Owl can't follow. Pick the real folder.") from None


def _local_target(target: str, link_dir: str) -> str | None:
    """A link's target as a local drive path; None for a local volume mount point.

    Raises BackupDirError for anything that could reach another computer.
    """
    if m := _DEVICE_DRIVE.match(target):
        return m.group(1)
    if _DEVICE_VOLUME.match(target):
        return None  # a local volume mounted in a folder
    if _is_network_text(target) or target.startswith("\\??\\"):
        raise BackupDirError(NETWORK_REFUSED)
    if _WIN_ABSOLUTE.match(target):
        return target
    # Relative or rooted ("\dir"): Windows resolves it against the link's own folder.
    return os.path.abspath(os.path.join(link_dir, target))


def _check_links(text: str, hops: int = 0) -> None:
    """Walk the path one component at a time and refuse links that lead to a network share.

    ``Path.resolve`` follows links by opening them, which would already send the user's
    credentials to a share a symlink points at. Here every component is inspected with
    ``lstat`` from the drive root down, so each link is read before anything goes through it,
    and a link to a local folder is itself checked the same way (up to MAX_LINK_HOPS).
    """
    if sys.platform != "win32":  # pragma: no cover - the app runs on Windows
        return
    if hops > MAX_LINK_HOPS:
        raise BackupDirError("That folder path goes through too many links.")
    full = os.path.abspath(text)  # GetFullPathNameW: lexical, never touches the disk
    if _is_network_text(full) or not _WIN_ABSOLUTE.match(full):
        raise BackupDirError(NETWORK_REFUSED)
    _check_drive(full)
    drive, rest = os.path.splitdrive(full)
    current = drive + "\\"
    for part in (p for p in rest.split("\\") if p):
        parent, current = current, os.path.join(current, part)
        target = link_target(current)
        if target is None:
            continue
        local = _local_target(target, parent)
        if local is not None:
            _check_links(local, hops + 1)


def validate_dir(raw: str, data_dir: Path) -> str:
    """An absolute local path to an existing, writable folder outside ``data/``.

    Checked lexically (and with ``GetDriveType``) *before* touching the filesystem: even a
    stat of a UNC path makes Windows authenticate to that host with the user's NTLM hash.
    Links on the way are read without being followed (``_check_links``) before resolving.
    """
    text = (raw or "").strip()
    if not text or "\x00" in text:
        raise BackupDirError("Enter a folder path.")
    if _is_network_text(text):
        raise BackupDirError(NETWORK_REFUSED)
    if sys.platform == "win32":
        if not _WIN_ABSOLUTE.match(text):
            raise BackupDirError("Enter a full folder path, like D:\\Iron Owl backups.")
    elif not text.startswith("/"):  # pragma: no cover
        raise BackupDirError("Enter a full folder path.")
    _check_drive(text)
    _check_links(text)
    try:
        resolved = Path(text).resolve(strict=True)
    except (OSError, RuntimeError):
        raise BackupDirError(NO_FOLDER) from None
    as_text = str(resolved)
    if _is_network_text(as_text):  # a link or mapping that leads to a share
        raise BackupDirError(NETWORK_REFUSED)
    if sys.platform == "win32" and _WIN_ABSOLUTE.match(as_text):
        _check_drive(as_text)
    if not resolved.is_dir():
        raise BackupDirError(NO_FOLDER)
    data = data_dir.resolve()
    if resolved == data or resolved.is_relative_to(data):
        raise BackupDirError("Pick a folder outside Iron Owl's data folder.")
    probe = resolved / f".fintrack-write-test-{secrets.token_hex(6)}"
    try:
        with open(probe, "xb") as fh:
            fh.write(b"ok")
        probe.unlink()
    except OSError as exc:
        probe.unlink(missing_ok=True)
        code = "denied" if isinstance(exc, PermissionError) else "write"
        raise BackupDirError(CANT_WRITE, code) from None
    return as_text


# ------------------------------------------------------------------ Settings (D7): "Turn on"

SUGGESTED_NAME = "Iron Owl backups"
LEGACY_SUGGESTED_NAME = "FinTrack backups"  # suggested before 2.0.0
# A folder name Windows accepts: no reserved characters, no trailing dot or space, not a
# device name (CON, NUL, COM1...), not "." or "..".
_BAD_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_DEVICE_NAMES = re.compile(r"^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(\..*)?\Z", re.IGNORECASE)
MAX_NEW_LEVELS = 3


def onedrive_dir() -> str | None:
    """The user's OneDrive folder, from the environment the OneDrive app sets (tests patch this)."""
    for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        value = (os.environ.get(var) or "").strip()
        if value:
            return value
    return None


def documents_dir() -> str | None:
    """The user's Documents folder (``FOLDERID_Documents``; tests patch this)."""
    if sys.platform != "win32":  # pragma: no cover - the app runs on Windows
        return str(Path.home() / "Documents")
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("d1", ctypes.c_ulong), ("d2", ctypes.c_ushort), ("d3", ctypes.c_ushort),
                    ("d4", ctypes.c_ubyte * 8)]

    # FOLDERID_Documents {FDD39AD0-238F-46AF-ADB4-6C85480369C7}
    folder = GUID(0xFDD39AD0, 0x238F, 0x46AF, (ctypes.c_ubyte * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7))
    out = ctypes.c_wchar_p()
    shell32 = ctypes.WinDLL("shell32")
    ole32 = ctypes.WinDLL("ole32")
    ole32.CoTaskMemFree.restype = None
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    hr = shell32.SHGetKnownFolderPath(ctypes.byref(folder), 0, wintypes.HANDLE(0), ctypes.byref(out))
    try:
        return out.value if hr == 0 and out.value else None
    finally:
        if out:
            ole32.CoTaskMemFree(ctypes.cast(out, ctypes.c_void_p))


def _local_dir(path: str | None) -> bool:
    """An existing local folder, checked without ever touching a network path."""
    return bool(path) and still_local(path) and os.path.isdir(path)


def suggested_folder() -> dict:
    """``{dir, exists}``: OneDrive\\Iron Owl backups when OneDrive is here, else
    Documents\\Iron Owl backups (None when neither is a local folder). A "FinTrack backups"
    folder made by an older version is suggested instead when it is there and the new one isn't,
    so turning backups back on keeps using it."""
    for base in (onedrive_dir(), documents_dir()):
        if base and _local_dir(base):
            target = os.path.join(base, SUGGESTED_NAME)
            if still_local(target) and os.path.isdir(target):
                return {"dir": target, "exists": True}
            legacy = os.path.join(base, LEGACY_SUGGESTED_NAME)
            if still_local(legacy) and os.path.isdir(legacy):
                return {"dir": legacy, "exists": True}
            return {"dir": target, "exists": False}
    return {"dir": None, "exists": False}


def create_dir(raw: str | None, data_dir: Path) -> None:
    """Make a missing backup folder (``PUT /api/backup/auto`` with ``create``), then the
    caller runs ``validate_dir`` as usual.

    Nothing is created unless the nearest existing parent passes the same no-network checks
    (lexical, drive type, and links read top-down by ``_existing_prefix`` before anything
    beneath them is touched) and the new names are plain folder names, at most
    MAX_NEW_LEVELS deep and outside data/. Folders made here are removed again (when still
    empty) if making the rest fails.
    """
    text = (raw or "").strip()
    if not text or "\x00" in text or _is_network_text(text):
        return  # validate_dir answers with the right message
    if sys.platform == "win32":
        if not _WIN_ABSOLUTE.match(text):
            return
    elif not text.startswith("/"):  # pragma: no cover
        return
    # Windows drops trailing dots and spaces from names ("backups." is "backups"): refuse
    # them rather than make a folder named differently from what was asked.
    if any(part not in (".", "..") and part != part.rstrip(" .") for part in re.split(r"[\\/]", text)):
        raise BackupDirError("That folder name can't be used. Pick another folder.")
    full = os.path.abspath(text)  # lexical: never touches the disk
    if _is_network_text(full):
        raise BackupDirError(NETWORK_REFUSED)
    if sys.platform == "win32":
        _check_drive(full)
    current, missing = _existing_prefix(full)
    if len(missing) > MAX_NEW_LEVELS:
        raise BackupDirError(NO_FOLDER)
    if not os.path.isdir(current):  # its links were all checked above
        # A file (or a broken link) is in the way, or the drive has no root folder.
        raise BackupDirError(NO_DRIVE if current == _root_of(full) else NO_FOLDER)
    if not missing:
        return
    for name in missing:
        if name in (".", "..") or _BAD_NAME_CHARS.search(name) or _DEVICE_NAMES.match(name) \
                or name != name.rstrip(" .") or len(name) > 200:
            raise BackupDirError("That folder name can't be used. Pick another folder.")
    data = data_dir.resolve()
    target = Path(full)
    if target == data or target.is_relative_to(data) or Path(current).resolve().is_relative_to(data):
        raise BackupDirError("Pick a folder outside Iron Owl's data folder.")
    made: list[str] = []
    try:
        for name in missing:
            current = os.path.join(current, name)
            os.mkdir(current)
            made.append(current)
    except OSError as exc:
        for path in reversed(made):
            try:
                os.rmdir(path)
            except OSError:
                pass
        code = "denied" if isinstance(exc, PermissionError) else "write"
        raise BackupDirError(CANT_WRITE, code) from None
    log.info("created the automatic backup folder")


def _root_of(full: str) -> str:
    drive, _ = os.path.splitdrive(full)
    return drive + os.sep if drive else os.sep


def _existing_prefix(full: str) -> tuple[str, list[str]]:
    """``(nearest existing folder, names missing below it)`` for an absolute local path.

    Walked TOP-DOWN from the drive root: each component is inspected with ``link_target``
    (an ``lstat`` that never follows it) and then ``lexists``, and only after every folder
    above it passed. So nothing beneath a link is ever touched before that link was read and
    found to stay on this PC (a directory symlink to ``\\\\server\\share`` would otherwise make
    Windows send the user's sign-in to that host). Stops at the first missing component.
    """
    parts = [p for p in re.split(r"[\\/]", os.path.splitdrive(full)[1]) if p]
    current = _root_of(full)
    for index, part in enumerate(parts):
        parent, candidate = current, os.path.join(current, part)
        try:
            target = link_target(candidate)
        except BackupDirError:
            if os.path.lexists(candidate):
                raise  # there, but not something FinTrack can read
            return current, parts[index:]
        if target is not None and sys.platform == "win32":
            local = _local_target(target, parent)  # raises for anything off this PC
            if local is not None:
                _check_links(local)
        current = candidate
    return current, []


def list_files(folder: str | None) -> list[dict]:
    """Automatic backup files in the folder, newest first (only names matching exactly)."""
    if not folder:
        return []
    path = Path(folder)
    out = []
    try:
        entries = list(path.iterdir())
    except OSError:
        return []
    for entry in entries:
        m = FILE_RE.match(entry.name)
        if not m:
            continue
        try:
            if entry.is_symlink() or not entry.is_file():
                continue
            size = entry.stat().st_size
        except OSError:
            continue
        try:
            created = dt.datetime.strptime(m.group(1), "%Y%m%d-%H%M%S")
        except ValueError:  # e.g. month 13: not one of ours, so never listed or pruned
            continue
        out.append({"name": entry.name, "size_bytes": size, "created_at": created.isoformat(timespec="seconds")})
    return sorted(out, key=lambda f: f["name"], reverse=True)


def prune(folder: Path, keep: int) -> int:
    """Delete all but the newest ``keep`` automatic backups; other files are never touched."""
    removed = 0
    for f in list_files(str(folder))[keep:]:
        try:
            (folder / f["name"]).unlink()
            removed += 1
        except OSError as exc:
            log.warning("could not delete an old automatic backup: %s", type(exc).__name__)
    return removed


def _friendly(exc: BaseException) -> str:
    if isinstance(exc, BackupDirError):
        return str(exc)
    if isinstance(exc, backup_service.BackupRaced):
        return exc.detail
    if isinstance(exc, FileNotFoundError):
        return "The backup folder is missing."
    if isinstance(exc, PermissionError):
        return "Iron Owl can't write to the backup folder."
    if isinstance(exc, OSError):
        return "The backup couldn't be written to the backup folder."
    return f"The automatic backup failed ({type(exc).__name__})."


_DIR_ERROR_CODES = {NETWORK_REFUSED: "network", NO_DRIVE: "missing", NO_FOLDER: "missing", CANT_WRITE: "write"}


def _friendly_code(exc: BaseException) -> str:
    """One of BACKUP_ERROR_CODES, for the Home screen's wording."""
    if isinstance(exc, BackupDirError):
        return exc.code or _DIR_ERROR_CODES.get(str(exc), "other")
    if isinstance(exc, FileNotFoundError):
        return "missing"
    if isinstance(exc, PermissionError):
        return "denied"
    if isinstance(exc, OSError):
        return "write"
    return "other"


# ------------------------------------------------------------------ backups


def still_local(folder: str) -> bool:
    """The saved folder still passes the no-network checks (without writing to it).

    A drive letter that was a USB stick when the folder was chosen can later be mapped to
    a network share; listing it would then authenticate to that host.
    """
    try:
        if _is_network_text(folder):
            return False
        if sys.platform == "win32":
            if not _WIN_ABSOLUTE.match(folder):
                return False
            _check_drive(folder)
            _check_links(folder)
    except BackupDirError:
        return False
    return True


def backup_settings(session: Session) -> dict:
    folder = _get(session, DIR_KEY) or None
    return {
        "dir": folder,
        "keep": _int(_get(session, KEEP_KEY), DEFAULT_KEEP, 1, 100),
        "last_at": iso_utc(_parse_utc(_get(session, LAST_AT_KEY))),
        "last_error": _get(session, LAST_ERROR_KEY) or None,
        **last_error_info(session),
        "files": list_files(folder) if folder and still_local(folder) else [],
    }


def backup_folder(session: Session) -> str | None:
    return _get(session, DIR_KEY) or None


def folder_name(folder: str | None) -> str | None:
    """Only the folder's own name ("OneDrive"; "D:" for a drive root), never the full path."""
    if not folder:
        return None
    path = Path(folder)
    return path.name or path.drive or None


def backup_status(session: Session) -> dict:
    """The Home screen's backup status: settings keys only; the folder is never listed or touched."""
    folder = backup_folder(session)
    return {
        "enabled": folder is not None,
        "folder_name": folder_name(folder),
        "last_at": iso_utc(_parse_utc(_get(session, LAST_AT_KEY))),
        **last_error_info(session),
    }


def last_error_info(session: Session) -> dict:
    """``{last_error_at, last_error_code}`` of the last failed automatic backup (both null after a success)."""
    at = iso_utc(_parse_utc(_get(session, LAST_ERROR_AT_KEY)))
    code = _get(session, LAST_ERROR_CODE_KEY)
    if at is None:
        return {"last_error_at": None, "last_error_code": None}
    return {"last_error_at": at, "last_error_code": code if code in BACKUP_ERROR_CODES else "other"}


def _set_error(session: Session, exc: BaseException | None, now: dt.datetime) -> None:
    _put(session, LAST_ERROR_KEY, _friendly(exc) if exc is not None else None)
    _put(session, LAST_ERROR_AT_KEY, iso_utc(now) if exc is not None else None)
    _put(session, LAST_ERROR_CODE_KEY, _friendly_code(exc) if exc is not None else None)


def save_backup_settings(
    session: Session, folder: str | None, keep: int, data_dir: Path, *, create: bool = False
) -> None:
    if create and folder is None:
        # "Turn on" with no folder must never read as "turn off" (the UI would then say
        # backups are on).
        raise BackupDirError("Pick a folder for the backups.")
    if folder is not None and create:
        create_dir(folder, data_dir)
    value = validate_dir(folder, data_dir) if folder is not None else None
    _put(session, DIR_KEY, value)
    _put(session, KEEP_KEY, str(int(keep)))
    if value is None:
        _set_error(session, None, now_utc())


class TooSoon(Exception):
    """"Back up now" was started less than MANUAL_MIN_SECONDS ago."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(retry_after)
        self.retry_after = retry_after


class AutoBackups:
    """Runs automatic backups for one vault (on lock, and every 24h while unlocked)."""

    def __init__(self, vault: Vault) -> None:
        self.vault = vault
        self._run = threading.Lock()
        self._last_periodic_failure: dt.datetime | None = None
        self._manual = threading.Lock()
        self._last_manual: float | None = None

    def on_lock(self) -> bool:
        """Called by the vault before it wipes the key (manual lock, auto-lock, shutdown)."""
        return self.run(MIN_GAP)

    def periodic(self) -> bool:
        now = now_utc()
        failed = self._last_periodic_failure
        if failed is not None and now - failed < RETRY_AFTER_FAILURE:
            return False
        return self.run(PERIOD, periodic=True)

    def run_now(self) -> bool:
        """"Back up now": at once (no gap), but at most one start per MANUAL_MIN_SECONDS.

        Raises TooSoon when throttled. Serialized with every other backup by ``run``.
        """
        with self._manual:
            now = monotonic()
            if self._last_manual is not None and now - self._last_manual < MANUAL_MIN_SECONDS:
                raise TooSoon(max(1, int(MANUAL_MIN_SECONDS - (now - self._last_manual) + 0.999)))
            self._last_manual = now
        return self.run(dt.timedelta(0), manual=True)

    def run(self, gap: dt.timedelta, *, periodic: bool = False, manual: bool = False) -> bool:
        """Back up if a folder is set and the last automatic backup is at least ``gap`` old.

        Never raises: a failure is stored in ``auto_backup_last_error`` and logged by type only.
        Returns True when a backup file was written. A ``manual`` run ("Back up now") never
        prunes: pressing it a few times must not delete older automatic backups; the next
        automatic run trims the folder back to ``keep``.
        """
        with self._run:
            db = self.vault.db
            try:
                session = db.acquire()
            except DatabaseLocked:
                return False
            try:
                return self._run_locked(session, gap, periodic, manual)
            except Exception as exc:  # noqa: BLE001 - never block locking
                session.rollback()
                log.error("automatic backup failed: %s", type(exc).__name__)
                return False
            finally:
                db.release(session)

    def _run_locked(self, session: Session, gap: dt.timedelta, periodic: bool, manual: bool = False) -> bool:
        folder = _get(session, DIR_KEY)
        if not folder:
            return False
        now = now_utc()
        last = _parse_utc(_get(session, LAST_AT_KEY))
        if last is not None and now - last < gap:
            return False
        keep = _int(_get(session, KEEP_KEY), DEFAULT_KEEP, 1, 100)
        session.commit()  # no read transaction held while the copy is made
        try:
            # Re-check the folder: a drive letter can be remapped to a network share later.
            target_dir = Path(validate_dir(folder, self.vault.settings.data_dir))
            data = backup_service.build_backup(self.vault)
            name = f"fintrack-auto-{now_local():%Y%m%d-%H%M%S}.ftbackup"
            _write_atomic(target_dir / name, data)
            if not manual:
                prune(target_dir, keep)
        except Exception as exc:  # noqa: BLE001
            log.error("automatic backup failed: %s", type(exc).__name__)
            if periodic:
                self._last_periodic_failure = now
            _set_error(session, exc, now)
            session.commit()
            return False
        self._last_periodic_failure = None
        _put(session, LAST_AT_KEY, iso_utc(now))
        _set_error(session, None, now)
        session.commit()
        log.info("automatic backup written")
        return True


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".partial")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# ------------------------------------------------------------------ hourly alert check (Release 3.6)

ALERTS_TICK_SECONDS = 60 * 60


def freeze_tick(state: Any) -> bool:
    """Release 3.17: once per new month while unlocked, write the goal and debt plans of the
    month(s) that ended as rows of their own (``spending.safe_freeze_rule_months``, a short
    transaction of its own), so an app left open over the month's end saves them early.
    Independent of Plaid (auto-sync may be off). Returns True when it ran."""
    from . import spending  # local: spending imports calendar, which imports this module's peers

    vault = state.vault
    if not vault.unlocked:
        return False
    current = month_of(today())
    if getattr(state, "rules_frozen_month", None) == current:
        return False
    try:
        session = vault.db.acquire()
    except DatabaseLocked:
        return False
    try:
        if not spending.safe_freeze_rule_months(session, today()):
            return False
        state.rules_frozen_month = current
        return True
    finally:
        vault.db.release(session)


def alerts_tick(state: Any, now: float | None = None) -> bool:
    """Hourly while unlocked: the date-driven alerts (low balance ahead, bill reminders).

    Does nothing while locked (the alerts live in the vault). Returns True when it ran.
    Only the exception type is logged.
    """
    from . import alerts  # local: alerts imports spending, calendar...

    vault = state.vault
    if not vault.unlocked:
        return False
    now = monotonic() if now is None else now
    last = getattr(state, "alerts_last_tick", None)
    if last is not None and now - last < ALERTS_TICK_SECONDS:
        return False
    try:
        session = vault.db.acquire()
    except DatabaseLocked:
        return False
    try:
        state.alerts_last_tick = now
        alerts.evaluate(session, today(), only=alerts.TICK_KEYS)
        session.commit()
        return True
    except Exception as exc:  # noqa: BLE001 - the loop must keep running
        session.rollback()
        log.error("automatic alert check failed: %s", type(exc).__name__)
        return False
    finally:
        vault.db.release(session)


# ------------------------------------------------------------------ auto-sync


def sync_hours(session: Session) -> int:
    value = _int(_get(session, SYNC_HOURS_KEY), DEFAULT_SYNC_HOURS, 0, 24)
    return value if value in SYNC_CHOICES else DEFAULT_SYNC_HOURS


def set_sync_hours(session: Session, hours: int) -> None:
    _put(session, SYNC_HOURS_KEY, str(int(hours)))


def _items(session: Session) -> list[PlaidItem]:
    """Items automatic sync should call Plaid for.

    Skips pending items and ones that need the user to sign in again: Plaid can't return
    new data for those until the user re-authenticates (which syncs the item itself).
    """
    return list(
        session.scalars(
            select(PlaidItem)
            .where(PlaidItem.status.not_in(("pending", "login_required")))
            .order_by(PlaidItem.id)
        )
    )


def due_at(items: list[PlaidItem], hours: int, last_attempt: dt.datetime | None) -> dt.datetime | None:
    """When the next automatic sync is due (None when off or nothing to sync).

    Due once the oldest ``last_synced_at`` is ``hours`` old (at once if an item never
    synced), and at most once per ``hours`` of attempts, so an item that keeps failing
    (its ``last_synced_at`` never moves) isn't retried every few minutes.
    """
    if hours <= 0 or not items:
        return None
    period = dt.timedelta(hours=hours)
    synced = [i.last_synced_at for i in items]
    due = dt.datetime.min if any(s is None for s in synced) else min(synced) + period
    if last_attempt is not None:
        due = max(due, last_attempt + period)
    return due


def sync_status(session: Session, state: Any, plaid: Any) -> dict:
    hours = sync_hours(session)
    due = due_at(_items(session), hours, state.auto_sync_last_attempt) if plaid.configured else None
    if due is not None:
        due = max(due, now_utc())
    return {"hours": hours, "next_at": iso_utc(due)}


def sync_tick(state: Any, now: dt.datetime | None = None) -> bool:
    """One check of the lifespan loop; runs the same sync as ``POST /api/plaid/sync`` when due."""
    from .sync import sync_and_process, sync_lock  # local: sync imports alerts, which import spending...

    # The .env keys (or an injected client), else the vault's keys while unlocked.
    vault, plaid = state.vault, state.active_plaid()
    if not vault.unlocked or not plaid.configured:
        return False
    # Skip this check while a sync (the user's) is running rather than queueing behind it:
    # its items would be up to date by then, and syncing again only repeats Plaid calls.
    # Holding the lock for the whole run also makes the due check below see fresh data.
    with sync_lock(blocking=False) as free:
        if not free:
            return False
        try:
            session = vault.db.acquire()
        except DatabaseLocked:
            return False
        try:
            now = now or now_utc()
            hours = sync_hours(session)
            items = _items(session)
            due = due_at(items, hours, state.auto_sync_last_attempt)
            if due is None or now < due:
                return False
            state.auto_sync_last_attempt = now
            ids = [i.id for i in items]
            session.commit()
            sync_and_process(session, plaid, ids)
            log.info("automatic sync ran")
            return True
        except Exception as exc:  # noqa: BLE001 - the loop must keep running
            session.rollback()
            log.error("automatic sync failed: %s", type(exc).__name__)
            return False
        finally:
            vault.db.release(session)

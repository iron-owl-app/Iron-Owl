"""Find update files in the user's Downloads folder, and accept files they pick in Settings.

Only for mode=enabled (``DownloadsScanner.scan`` returns None otherwise without even
resolving the folder). The folder is the Known Folder "Downloads" (``SHGetKnownFolderPath``
with KF_FLAG_DONT_VERIFY, so resolving it touches nothing), falling back to
``~/Downloads``; network folders (UNC, mapped network drives, links to shares) are refused.

The scan looks at top-level ``*.ftupdate`` entries only and skips anything that is not a
plain local file: links/reparse points, OneDrive/Files On-Demand placeholders (offline /
recall-on-access attributes, which would start a download when opened), files smaller than
200 bytes or larger than the package cap, and files modified in the last 5 seconds (still
downloading). Newest 20 by mtime.

Each new ``(name, size, mtime_ns)`` is copied (streamed, capped, hashed) into a private
``data/updates/.incoming-<hex>/`` folder and the COPY is verified; a good copy moves to
``data/updates/inbox/<sha16>.ftupdate``. The first block (1 MB, which holds any valid
update's head) is checked before anything is written -- magic, the section-length walk
against the file's size, caps and the signature (``package.check_head``); a file that
fails is rejected and never copied further. The user's
Downloads file is only ever opened for reading: never modified, moved, deleted or executed.
Verdicts are cached in the store's ``seen`` map so rejected/older/same/installed/failed
files are never offered or re-read; ids that were installed, or failed / were rolled back,
are never offered again even if the cache is lost. Ids read back from state.json are
checked (16 hex digits) before any path is built from them.
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
import shutil
import stat
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from ..services import automation
from ..utils import iso_utc, utcnow
from . import semver
from . import package
from .package import MIN_PACKAGE_BYTES, UpdateRejected, verify_package
from .requirements import Environment
from .store import UpdateStore, is_valid_id, leaf_name, make_offer, public_offer

log = logging.getLogger("fintrack.updates")

FOLDERID_DOWNLOADS = "{374DE290-123F-4565-9164-39C4925E467B}"
KF_FLAG_DONT_VERIFY = 0x00004000
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
FILE_ATTRIBUTE_OFFLINE = 0x00001000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000
SKIP_ATTRIBUTES = (
    FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_OFFLINE
    | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
)
EXTENSION = ".ftupdate"
MAX_CANDIDATES = 20
SETTLE_SECONDS = 5
SCAN_THROTTLE_SECONDS = 30
COPY_CHUNK = 1024 * 1024
INCOMING_PREFIX = ".incoming-"
SKIP_VERDICTS = frozenset({"rejected", "older", "same", "installed", "failed"})


# ------------------------------------------------------------------ the folder


def known_downloads_folder() -> Path | None:
    """FOLDERID_Downloads via SHGetKnownFolderPath; None if unavailable."""
    if sys.platform != "win32":  # pragma: no cover - the app runs on Windows
        return None
    try:
        import ctypes
        import uuid
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        u = uuid.UUID(FOLDERID_DOWNLOADS)
        guid = GUID(u.fields[0], u.fields[1], u.fields[2], (ctypes.c_ubyte * 8)(*u.bytes[8:]))
        shell32 = ctypes.WinDLL("shell32")
        ole32 = ctypes.WinDLL("ole32")
        fn = shell32.SHGetKnownFolderPath
        fn.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
        fn.restype = ctypes.c_long
        ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        ole32.CoTaskMemFree.restype = None
        ptr = ctypes.c_void_p()
        hr = fn(ctypes.byref(guid), KF_FLAG_DONT_VERIFY, None, ctypes.byref(ptr))
        try:
            if hr != 0 or not ptr.value:
                return None
            return Path(ctypes.wstring_at(ptr.value))
        finally:
            if ptr.value:
                ole32.CoTaskMemFree(ptr)
    except (OSError, AttributeError, ValueError):
        return None


DOWNLOADS_OVERRIDE_ENV = "FINTRACK_DOWNLOADS_DIR"


def downloads_override() -> Path | None:
    """FOR TESTING ONLY (release drills): ``FINTRACK_DOWNLOADS_DIR`` names the folder to scan
    instead of the user's Downloads. Read from the real process environment only, never from a
    settings file. In a packaged install the launcher drops every ``FINTRACK_*`` variable it
    inherits from the user's environment and sets this one only when it was started with the
    explicit ``--test-downloads-dir`` flag, so it can't be set by accident. The folder
    still has to pass ``is_local_folder`` and every file still needs a trusted signature."""
    raw = os.environ.get(DOWNLOADS_OVERRIDE_ENV, "").strip()
    return Path(raw) if raw and "\x00" not in raw else None


def downloads_folder() -> Path | None:
    override = downloads_override()
    if override is not None:
        log.info("scanning the test downloads folder (%s)", DOWNLOADS_OVERRIDE_ENV)
        return override
    return known_downloads_folder() or Path.home() / "Downloads"


def is_local_folder(path: Path) -> bool:
    """A local, existing folder. Checked lexically and link by link (never following a
    link to a share) before anything touches it, like the automatic-backup folder."""
    text = str(path)
    if not text or "\x00" in text or automation._is_network_text(text):  # noqa: SLF001
        return False
    try:
        automation._check_links(text)  # noqa: SLF001 - refuses UNC/remote drives/links to shares
    except automation.BackupDirError:
        return False
    try:
        return stat.S_ISDIR(os.stat(text).st_mode)
    except OSError:
        return False


# ------------------------------------------------------------------ candidates


@dataclass(frozen=True)
class Candidate:
    name: str
    path: Path
    size: int
    mtime_ns: int


def _entry_stat(entry: os.DirEntry) -> os.stat_result:
    """The entry's own metadata. On Windows this comes from the directory listing, so a
    cloud placeholder is not opened (and not downloaded) by looking at it."""
    return entry.stat(follow_symlinks=False)


def list_candidates(folder: Path, *, now: float, max_bytes: int | None = None) -> list[Candidate]:
    max_bytes = package.MAX_PACKAGE_BYTES if max_bytes is None else max_bytes
    out: list[Candidate] = []
    settled_ns = int((now - SETTLE_SECONDS) * 1_000_000_000)
    try:
        with os.scandir(folder) as it:
            for entry in it:
                if not entry.name.lower().endswith(EXTENSION):
                    continue
                try:
                    if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                        continue
                    st = _entry_stat(entry)
                except OSError:
                    continue
                if (
                    not stat.S_ISREG(st.st_mode)
                    or getattr(st, "st_file_attributes", 0) & SKIP_ATTRIBUTES
                    or getattr(st, "st_reparse_tag", 0)
                    or not MIN_PACKAGE_BYTES <= st.st_size <= max_bytes
                    or st.st_mtime_ns > settled_ns
                ):
                    continue
                out.append(Candidate(entry.name, Path(entry.path), st.st_size, st.st_mtime_ns))
    except OSError as exc:
        log.info("downloads folder not readable (%s)", type(exc).__name__)
        return []
    out.sort(key=lambda c: c.mtime_ns, reverse=True)
    return out[:MAX_CANDIDATES]


# ------------------------------------------------------------------ copy + verify


class _TooLarge(Exception):
    pass


def copy_capped(
    src: Path,
    dest: Path,
    cap: int | None = None,
    *,
    head_check: Callable[[bytes, int], None] | None = None,
) -> tuple[int, str]:
    """Stream ``src`` into a new file ``dest`` (``"xb"``); returns (bytes, sha256 hex).

    With ``head_check``, it is called with the first block (at most ``COPY_CHUNK`` bytes,
    which holds any valid update's whole head) and the file's size BEFORE anything is
    written; if it raises, the copy stops there (so an unsigned file is never copied)."""
    cap = package.MAX_PACKAGE_BYTES if cap is None else cap
    digest, total = hashlib.sha256(), 0
    with open(src, "rb") as fin, open(dest, "xb") as fout:
        while block := fin.read(COPY_CHUNK):
            if head_check is not None and total == 0:
                head_check(block, os.fstat(fin.fileno()).st_size)
            total += len(block)
            if total > cap:
                raise _TooLarge()
            digest.update(block)
            fout.write(block)
        fout.flush()
        os.fsync(fout.fileno())
    return total, digest.hexdigest()


@dataclass(frozen=True)
class Checked:
    """What one file turned out to be."""

    verdict: str  # ready | rejected | older | same | failed
    sha16: str | None  # None when the file wasn't read to the end (too large / not an update)
    check: dict  # UpdateCheck for the frontend
    version: str | None = None
    offer: dict | None = None  # for ready: the offer (private fields included)


def new_incoming_dir(store: UpdateStore) -> Path:
    store.dir.mkdir(parents=True, exist_ok=True)
    path = store.dir / f"{INCOMING_PREFIX}{secrets.token_hex(8)}"
    path.mkdir()
    return path


def check_staged_file(
    store: UpdateStore,
    staged: Path,
    *,
    file_name: str,
    source: str,
    current_version: str,
    sha256: str | None = None,
    verify: Callable[..., object] = verify_package,
    trusted_keys: Mapping[str, str] | None = None,
    env: Environment | None = None,
) -> Checked:
    """Verify a private copy; a good one moves to ``inbox/<sha16>.ftupdate``.

    Used by the Downloads scan and by POST /api/update/file (the router stages the upload
    in ``new_incoming_dir`` first). Does not change the offer.
    """
    leaf = leaf_name(file_name)
    if sha256 is None:
        sha256 = _file_sha256(staged)
    sha16 = sha256[:16]
    try:
        verified = verify(staged, current_version, trusted_keys=trusted_keys, env=env)
    except UpdateRejected as exc:
        log.info("update file rejected (%s)", exc.code)
        return Checked("rejected", sha16, {"result": "rejected", "reason": exc.reason, "code": exc.code, "file_name": leaf})
    version = verified.manifest.version
    if verified.result != "ready":
        return Checked(verified.result, verified.id, {
            "result": verified.result, "code": verified.code, "file_name": leaf,
            "file_version": semver.display(version), "current_version": semver.display(current_version),
        }, version)
    if verified.id in store.failed_ids():
        # this exact file failed to install (or was rolled back) before: never offered again
        staged.unlink()
        failure = store.failure(verified.id) or {}
        return Checked("failed", verified.id, {
            "result": "failed", "code": failure.get("code"), "file_name": leaf,
            "file_version": semver.display(version), "current_version": semver.display(current_version),
        }, version)
    store.inbox.mkdir(parents=True, exist_ok=True)
    inbox_name = f"{verified.id}{EXTENSION}"
    target = store.inbox / inbox_name
    if target.exists():
        staged.unlink()
    else:
        os.replace(staged, target)
    offer = make_offer(verified, source=source, file_name=leaf, inbox_name=inbox_name)
    return Checked("ready", verified.id, {"result": "ready", "offer": public_offer(offer)}, version, offer)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(COPY_CHUNK):
            digest.update(block)
    return digest.hexdigest()


def prune_inbox(store: UpdateStore, keep: str | None) -> None:
    """Keep one package: the current offer's."""
    if not store.inbox.is_dir():
        return
    for path in store.inbox.iterdir():
        if path.name != keep and path.is_file() and not path.is_symlink():
            try:
                path.unlink()
            except OSError:
                pass


def accept_picked_file(
    store: UpdateStore,
    staged: Path,
    *,
    file_name: str,
    current_version: str,
    trusted_keys: Mapping[str, str] | None = None,
    env: Environment | None = None,
    verify: Callable[..., object] = verify_package,
) -> dict:
    """POST /api/update/file: a valid picked file becomes the offer. Returns UpdateCheck
    (plus ``{"result": "failed", ...}`` for a file whose install failed before)."""
    checked = check_staged_file(
        store, staged, file_name=file_name, source="picked", current_version=current_version,
        verify=verify, trusted_keys=trusted_keys, env=env,
    )
    if checked.offer is not None and store.set_offer(checked.offer):
        prune_inbox(store, checked.offer["inbox"])
    return checked.check


# ------------------------------------------------------------------ the scanner


@dataclass(frozen=True)
class ScanResult:
    offer: dict | None  # public offer after the scan
    checked: int  # files copied and verified by this scan
    at: float
    cached: bool = False


class DownloadsScanner:
    """One scan at a time, at most every 30 s (a busy or recent scan returns the cached
    result). Callers: POST /api/update/scan and the automation loop, unlocked only."""

    def __init__(
        self,
        store: UpdateStore,
        *,
        current_version: str,
        folder_resolver: Callable[[], Path | None] = downloads_folder,
        verify: Callable[..., object] = verify_package,
        trusted_keys: Mapping[str, str] | None = None,
        env: Environment | None = None,
        clock: Callable[[], float] = time.time,
        throttle_seconds: float = SCAN_THROTTLE_SECONDS,
    ) -> None:
        self.store = store
        self.current_version = current_version
        self.folder_resolver = folder_resolver
        self.verify = verify
        self.trusted_keys = trusted_keys
        self.env = env
        self.clock = clock
        self.throttle_seconds = throttle_seconds
        self._lock = threading.Lock()
        self._last: ScanResult | None = None

    def scan(self, mode: str, *, force: bool = False) -> ScanResult | None:
        if mode != "enabled":
            return None
        now = self.clock()
        last = self._last
        if not force and last is not None and 0 <= now - last.at < self.throttle_seconds:
            return ScanResult(public_offer(self.store.offer()), 0, last.at, cached=True)
        if not self._lock.acquire(blocking=False):
            return ScanResult(public_offer(self.store.offer()), 0, last.at if last else now, cached=True)
        try:
            result = self._scan(now)
            self._last = result
            return result
        finally:
            self._lock.release()

    def _scan(self, now: float) -> ScanResult:
        store, current = self.store, self.current_version
        store.drop_stale_offer(current)
        folder = self.folder_resolver()
        if folder is None or not is_local_folder(folder):
            log.info("downloads folder unavailable or not local; skipped")
            return ScanResult(public_offer(store.offer()), 0, now)
        at = iso_utc(utcnow())
        installed = store.installed_ids()
        failed = store.failed_ids()
        seen = store.seen()
        ready: list[tuple[tuple[int, int, int], int, str, Candidate, dict | None]] = []
        checked = 0
        for cand in list_candidates(folder, now=now):
            key = store.seen_key(cand.name, cand.size, cand.mtime_ns)
            entry = seen.get(key)
            if isinstance(entry, dict) and entry.get("verdict") in SKIP_VERDICTS:
                continue
            if (
                isinstance(entry, dict) and entry.get("verdict") == "ready"
                and semver.is_valid(entry.get("version")) and is_valid_id(entry.get("sha16"))
            ):
                sha16, version = entry["sha16"], entry["version"]
                if sha16 in installed or sha16 in failed:
                    verdict = "installed" if sha16 in installed else "failed"
                    store.mark_seen(key, sha16=sha16, verdict=verdict, version=version, at=at)
                elif semver.compare(version, current) > 0:
                    ready.append((semver.parse(version), cand.mtime_ns, sha16, cand, None))
                continue
            try:
                outcome = self._ingest(cand)
                if outcome is None:
                    continue  # changed or unreadable while copying: look again next time
                checked += 1
                verdict = outcome.verdict
                if verdict == "ready" and outcome.sha16 in installed:
                    verdict = "installed"
                store.mark_seen(key, sha16=outcome.sha16, verdict=verdict, version=outcome.version, at=at)
            except OSError as exc:  # one bad file (or a full disk) never stops the scan
                log.info("update file not checked (%s)", type(exc).__name__)
                continue
            if verdict == "ready":
                ready.append((semver.parse(outcome.version), cand.mtime_ns, outcome.sha16, cand, outcome.offer))
        if ready:
            self._offer_best(max(ready, key=lambda r: (r[0], r[1])))
        offer = store.offer()
        prune_inbox(store, offer["inbox"] if offer else None)
        return ScanResult(public_offer(offer), checked, now)

    def _offer_best(self, best) -> None:
        version_t, _mtime, sha16, cand, offer = best
        current_offer = self.store.offer()
        if current_offer is not None and semver.is_valid(current_offer.get("version")):
            if current_offer["id"] == sha16 or semver.parse(current_offer["version"]) >= version_t:
                return  # already offered, or a picked/earlier offer at least as new
        if offer is None:  # from the seen cache: re-verify the inbox copy for its details
            offer = self._reverify_inbox(sha16, cand)
            if offer is None:
                return
        self.store.set_offer(offer)  # refuses (False) an id that failed before

    def _reverify_inbox(self, sha16: str, cand: Candidate) -> dict | None:
        if not is_valid_id(sha16):  # never build a path from an unchecked id
            self.store.forget_seen(self.store.seen_key(cand.name, cand.size, cand.mtime_ns))
            return None
        path = self.store.inbox / f"{sha16}{EXTENSION}"
        outcome = None
        if path.is_file():
            incoming = new_incoming_dir(self.store)
            try:
                staged = incoming / "package.ftupdate"
                shutil.copyfile(path, staged)
                outcome = check_staged_file(
                    self.store, staged, file_name=cand.name, source="downloads",
                    current_version=self.current_version, verify=self.verify,
                    trusted_keys=self.trusted_keys, env=self.env,
                )
            except OSError:
                outcome = None
            finally:
                shutil.rmtree(incoming, ignore_errors=True)
        else:
            outcome = self._ingest(cand)
        if outcome is None or outcome.verdict != "ready" or outcome.sha16 != sha16:
            self.store.forget_seen(self.store.seen_key(cand.name, cand.size, cand.mtime_ns))
            return None
        return outcome.offer

    def _check_head(self, prefix: bytes, size: int) -> None:
        package.check_head(prefix, size, self.trusted_keys)

    def _ingest(self, cand: Candidate) -> Checked | None:
        """Copy one Downloads file into a private folder and verify the copy."""
        try:
            incoming = new_incoming_dir(self.store)
        except OSError as exc:
            log.warning("update inbox not writable (%s)", type(exc).__name__)
            return None
        try:
            staged = incoming / "package.ftupdate"
            try:
                size, sha = copy_capped(cand.path, staged, head_check=self._check_head)
            except _TooLarge:
                return Checked("rejected", None, {"result": "rejected", "reason": "too_large",
                                                  "code": "FT-UPD-BIG", "file_name": leaf_name(cand.name)})
            except UpdateRejected as exc:  # bad magic, lengths or signature: not copied further
                log.info("update file rejected (%s)", exc.code)
                return Checked("rejected", None, {"result": "rejected", "reason": exc.reason,
                                                  "code": exc.code, "file_name": leaf_name(cand.name)})
            except OSError as exc:
                log.info("update file not readable (%s)", type(exc).__name__)
                return None
            try:
                after = os.stat(cand.path, follow_symlinks=False)
            except OSError:
                return None
            if size != cand.size or after.st_size != cand.size or after.st_mtime_ns != cand.mtime_ns:
                return None  # still being written
            try:
                return check_staged_file(
                    self.store, staged, file_name=cand.name, source="downloads",
                    current_version=self.current_version, sha256=sha, verify=self.verify,
                    trusted_keys=self.trusted_keys, env=self.env,
                )
            except OSError as exc:  # e.g. the inbox isn't writable: try again next scan
                log.info("update file not checked (%s)", type(exc).__name__)
                return None
        finally:
            shutil.rmtree(incoming, ignore_errors=True)

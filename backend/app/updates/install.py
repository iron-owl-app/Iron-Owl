"""Installing an update, and the install-root ``state.json`` handshake with the launcher.

The launcher contract is in ``packaging/launcher/launcher_core.py`` (module docstring):

- **Install** (one ``InstallJob`` thread): a backup of the vault into
  ``data/update-backups/fintrack-before-<v>-<stamp>.ftbackup`` (newest 3 kept) plus a forced
  automatic backup when a backup folder is set; then ``package.extract_payload`` re-verifies
  the inbox copy with a full ``Environment`` and unpacks ``versions/<new>``; the result must
  name exactly the offered id and version; then ``state.json`` gets ``current=<new>``,
  ``previous=<old>``, ``pending={version, from, at, id}`` in one atomic write, and the server
  exits with 75 about a second later. Any failure before that write leaves nothing changed:
  FT-UPD-01 (backup) / FT-UPD-02 (files) and ``last_result`` says so. The offer's id is
  recorded as failed (never offered again) only when the file itself was refused at
  re-verification (signature or hash); a failure of the user's computer (disk full, a file in use,
  no backup) never blacklists a good file, and they can simply try again.
- **Finish** (first successful unlock of the new version while ``pending`` names it):
  ``pending`` cleared FIRST (so nothing can later roll back data they have used), then
  best effort: history + ``last_result`` installed, version folders pruned to current +
  previous, ``pre-update-*`` snapshots to the newest 2, ``failed-update-*`` to the newest 2.
- **Rollback request** (the new version couldn't upgrade the data while pending, and no
  open of it has succeeded in this process): ``rollback={to: previous, code: "FT-UPD-05"}``
  then exit 75; the launcher restores the snapshot and starts the previous version.

``state.json`` is rewritten atomically (temp file in the same folder, fsync, ``os.replace``
retried on sharing violations) and unknown keys are always kept.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..migrations import LATEST
from ..services import backup as backup_service
from ..utils import iso_utc, utcnow
from . import semver
from .package import (
    ExtractError,
    NotInstallable,
    UpdateRejected,
    extract_payload,
    long_path,
    strict_json,
)

log = logging.getLogger("fintrack.updates")

STATE_NAME = "state.json"
MAX_STATE_BYTES = 64 * 1024
EXIT_RESTART = 75
UPDATE_BACKUPS_DIRNAME = "update-backups"
UPDATE_BACKUPS_KEEP = 3
SNAPSHOT_KEEP = 2
SNAPSHOT_RE = re.compile(r"^pre-update-(\d{1,3}\.\d{1,3}\.\d{1,3})-(\d{8}T\d{6}Z)(?:-(\d+))?$")
# The launcher's data/failed-update-<new>-<YYYYmmddTHHMMSSZ>-<4 hex>/ (the live vault files
# it moved aside when it restored the pre-update copy).
FAILED_UPDATE_RE = re.compile(r"^failed-update-\d{1,3}\.\d{1,3}\.\d{1,3}-(\d{8}T\d{6}Z)-[0-9a-f]{4}$")
FAILED_UPDATE_KEEP = 2
UPDATE_BACKUP_RE = re.compile(r"^fintrack-before-\d{1,3}\.\d{1,3}\.\d{1,3}-(\d{8}-\d{6})(?:-(\d+))?\.ftbackup$")
_INSTALLED_AT_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.+\-Z]{8,32}$")
_ID_RE = re.compile(r"^[0-9a-f]{16}$")

FT_UPD_01 = "FT-UPD-01"  # the backup before the update failed; nothing changed
FT_UPD_02 = "FT-UPD-02"  # the files couldn't be checked / installed; nothing changed
FT_UPD_05 = "FT-UPD-05"  # the new version couldn't update the data: rolled back

REPLACE_RETRY_DELAYS = (0.05, 0.15, 0.3)


# ------------------------------------------------------------------ install-root state.json


class InstallStateError(Exception):
    """The install root's state.json is missing or unusable."""


def state_path(install_root: Path) -> Path:
    return Path(install_root) / STATE_NAME


def read_install_state(install_root: Path) -> dict[str, Any]:
    """The launcher's state.json (checked for the fields the updater relies on)."""
    try:
        with open(state_path(install_root), "rb") as fh:
            raw = fh.read(MAX_STATE_BYTES + 1)
    except OSError as exc:
        raise InstallStateError(type(exc).__name__) from None
    if len(raw) > MAX_STATE_BYTES:
        raise InstallStateError("too large")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise InstallStateError("unreadable") from None
    if not isinstance(data, dict) or not semver.is_valid(data.get("current")):
        raise InstallStateError("no current version")
    return data


def write_install_state(install_root: Path, state: dict[str, Any]) -> None:
    """Temp file beside it, fsync, then os.replace (retried briefly on a sharing violation)."""
    path = state_path(install_root)
    tmp = path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        for delay in (*REPLACE_RETRY_DELAYS, None):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if delay is None:
                    raise
                time.sleep(delay)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def pending_of(state: dict[str, Any]) -> dict | None:
    pending = state.get("pending")
    if isinstance(pending, str) and semver.is_valid(pending):  # the launcher's short form
        return {"version": pending}
    return pending if isinstance(pending, dict) and semver.is_valid(pending.get("version")) else None


def installed_at(state: dict[str, Any]) -> str | None:
    value = state.get("installed_at")
    return value if isinstance(value, str) and _INSTALLED_AT_RE.fullmatch(value) else None


def version_ok(versions_dir: Path, version: object) -> bool:
    """``versions/<version>`` looks runnable (the launcher checks the same file)."""
    return semver.is_valid(version) and (versions_dir / str(version) / "run_packaged.py").is_file()


def release_schema_version(version_dir: Path) -> int | None:
    """``schema_version`` from a version folder's release.json, or None."""
    try:
        with open(version_dir / "release.json", "rb") as fh:
            raw = fh.read(4097)
        if len(raw) > 4096:
            return None
        data = strict_json(raw)
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        return None
    value = data.get("schema_version") if isinstance(data, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# ------------------------------------------------------------------ pruning


def prune_versions(versions_dir: Path, keep: set[str]) -> list[str]:
    """Remove version folders other than ``keep`` (best effort; ``.partial-*`` are the
    launcher's to clean). Only folders named like a version are ever touched."""
    removed: list[str] = []
    try:
        entries = list(versions_dir.iterdir())
    except OSError:
        return removed
    for path in entries:
        if not semver.is_valid(path.name) or path.name in keep:
            continue
        if path.is_symlink() or not path.is_dir():
            continue
        shutil.rmtree(long_path(path), ignore_errors=True)
        removed.append(path.name)
    return removed


def prune_snapshots(data_dir: Path, count: int = SNAPSHOT_KEEP) -> list[str]:
    """Keep the newest ``count`` ``data/pre-update-*`` copies the launcher made."""
    try:
        names = [p.name for p in data_dir.iterdir()
                 if SNAPSHOT_RE.fullmatch(p.name) and p.is_dir() and not p.is_symlink()]
    except OSError:
        return []

    def stamp(name: str) -> tuple[str, int]:
        m = SNAPSHOT_RE.fullmatch(name)
        assert m is not None
        return m.group(2), int(m.group(3) or 1)

    removed = sorted(names, key=stamp, reverse=True)[count:]
    for name in removed:
        shutil.rmtree(data_dir / name, ignore_errors=True)
    return removed


def prune_failed_updates(data_dir: Path, count: int = FAILED_UPDATE_KEEP) -> list[str]:
    """Keep the newest ``count`` ``data/failed-update-*`` folders (by the launcher's stamp).
    Only folders named exactly like the launcher's are ever touched."""
    try:
        names = [p.name for p in data_dir.iterdir()
                 if FAILED_UPDATE_RE.fullmatch(p.name) and p.is_dir() and not p.is_symlink()]
    except OSError:
        return []

    def stamp(name: str) -> tuple[str, str]:
        m = FAILED_UPDATE_RE.fullmatch(name)
        assert m is not None
        return m.group(1), name

    removed = sorted(names, key=stamp, reverse=True)[count:]
    for name in removed:
        shutil.rmtree(long_path(data_dir / name), ignore_errors=True)
    return removed


def prune_update_backups(folder: Path, keep: int = UPDATE_BACKUPS_KEEP) -> None:
    try:
        entries = list(folder.iterdir())
    except OSError:
        return
    for path in entries:  # an interrupted write of one of ours
        name = path.name
        if name.endswith(".partial") and UPDATE_BACKUP_RE.fullmatch(name[: -len(".partial")]) and path.is_file():
            path.unlink(missing_ok=True)

    def stamp(name: str) -> tuple[str, int]:  # newest first by time, never by version text
        m = UPDATE_BACKUP_RE.fullmatch(name)
        assert m is not None
        return m.group(1), int(m.group(2) or 1)

    names = sorted(
        (p.name for p in entries if UPDATE_BACKUP_RE.fullmatch(p.name) and p.is_file()),
        key=stamp, reverse=True,
    )
    for name in names[keep:]:
        try:
            (folder / name).unlink()
        except OSError:
            pass


# ------------------------------------------------------------------ the install job


class InstallJob:
    """One update being installed, in its own thread. ``progress`` is UpdateProgress."""

    def __init__(self, updater: Any, offer: dict, *, failed_code: str | None = None,
                 blacklist: bool = False) -> None:
        self.updater = updater
        self.offer = offer
        self.offer_id: str = offer["id"]
        self.version: str = offer["version"]
        self._lock = threading.Lock()
        self.done = threading.Event()
        self.acked = False
        # The backup before the update was written (the result says "The backup was kept."
        # only then; a file that fails its check before installing never made one).
        self.backed_up = False
        self.progress: dict = {
            "id": self.offer_id, "to_version": self.version, "step": "backup",
            "state": "running", "code": None, "at": iso_utc(utcnow()),
        }
        self.thread: threading.Thread | None = None
        if failed_code is not None:  # the pre-install check failed: nothing was started
            self._fail(failed_code, blacklist=blacklist)
            self.done.set()

    # --- state

    @property
    def running(self) -> bool:
        return self.progress["state"] in ("running", "restarting")

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self.progress)

    def _set(self, **changes: Any) -> None:
        with self._lock:
            self.progress.update(changes, at=iso_utc(utcnow()))

    def _fail(self, code: str, *, record: bool = True, blacklist: bool = False) -> None:
        """Stop with ``code``. ``record``: write ``last_result`` (the "didn't install"
        message). ``blacklist``: also record the offer's id as failed, so this exact file is
        never offered again -- only when the FILE was refused (signature or hash at
        re-verification), never for a failure of the user's computer (FT-UPD-01, a disk or file
        error, the offer changing underneath): a good file must stay installable."""
        self._set(state="failed", code=code)
        at = self.progress["at"]
        if not record:
            return
        store = self.updater.store
        try:
            if blacklist:
                store.record_failed(offer_id=self.offer_id, version=self.version, code=code, at=at)
            store.set_last_result({
                "outcome": "failed", "from_version": self.updater.current_version,
                "to_version": self.version, "code": code, "at": at,
                "backup_kept": self.backed_up,
            })
        except OSError as exc:
            log.error("could not record the failed update (%s)", type(exc).__name__)

    def wait(self, timeout: float | None = None) -> bool:
        return self.done.wait(timeout)

    # --- running

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name="fintrack-update-install", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        try:
            try:
                self._backup()
            except Exception as exc:  # noqa: BLE001
                log.error("%s: backup before the update failed (%s)", FT_UPD_01, type(exc).__name__)
                self._fail(FT_UPD_01)
                return
            self.backed_up = True
            self._set(step="install")
            if not self._install():
                return
            self._set(step="restart", state="restarting")
            log.info("update to %s installed; restarting", self.version)
            self.updater.schedule_exit(EXIT_RESTART)
        except Exception as exc:  # noqa: BLE001 - never leave the job "running"
            log.error("%s: update failed (%s)", FT_UPD_02, type(exc).__name__)
            if self.running:
                self._fail(FT_UPD_02)
        finally:
            self.updater.store.release_offer(self.offer_id)
            self.done.set()

    def _backup(self) -> None:
        up = self.updater
        data = backup_service.build_backup(up.vault)
        folder = up.data_dir / UPDATE_BACKUPS_DIRNAME
        folder.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        name = f"fintrack-before-{up.current_version}-{stamp}.ftbackup"
        n = 1
        while (folder / name).exists():
            n += 1
            name = f"fintrack-before-{up.current_version}-{stamp}-{n}.ftbackup"
        tmp = folder / f"{name}.partial"
        try:
            with open(tmp, "xb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, folder / name)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        prune_update_backups(folder)
        log.info("backup before the update written")
        # The user's own automatic backup folder too, at once (never raises; not required).
        if up.auto_backup is not None:
            up.auto_backup.run(dt.timedelta(0))

    def _install(self) -> bool:
        """Extract and hand over to the launcher. False (job failed) when nothing changed."""
        up = self.updater
        package_path = up.store.offer_package()
        offer = up.store.offer()
        if package_path is None or offer is None or offer["id"] != self.offer_id:
            # Not the file's fault (the offer changed or its copy went away): no result, and
            # the id is not recorded as failed.
            log.error("%s: the update file is gone", FT_UPD_02)
            self._fail(FT_UPD_02, record=False)
            return False
        root: Path = up.install_root
        versions = root / "versions"
        try:
            state = read_install_state(root)
        except InstallStateError as exc:
            log.error("%s: install state unusable (%s)", FT_UPD_02, exc)
            self._fail(FT_UPD_02)
            return False
        if state.get("current") != up.current_version or pending_of(state) or state.get("rollback"):
            log.error("%s: install state doesn't match this version", FT_UPD_02)
            self._fail(FT_UPD_02)
            return False
        stale = versions / self.version
        if self.version not in (state.get("current"), state.get("previous")) and stale.exists():
            # An earlier attempt that stopped before state.json named it: never ran, not kept.
            shutil.rmtree(long_path(stale), ignore_errors=True)
        try:
            extracted = extract_payload(
                package_path, up.current_version, versions, trusted_keys=up.trusted_keys(), env=up.environment(),
                runtimes_dir=root / "runtimes", compile_bytecode=True,
            )
        except NotInstallable as exc:
            verified = exc.verified
            if verified.result == "ready" and verified.help is not None:
                # Needs help after all (e.g. the disk filled up): not a failed update.
                up.mark_needs_help(self.offer_id, verified.help.as_dict())
                self._fail(verified.help.code, record=False)
            else:
                self._fail(FT_UPD_02)
            return False
        except UpdateRejected as exc:
            log.error("%s: update file refused at install (%s)", FT_UPD_02, exc.code)
            self._fail(FT_UPD_02, blacklist=file_refused(exc))
            return False
        except (ExtractError, OSError, ValueError) as exc:
            log.error("%s: update files not installed (%s)", FT_UPD_02, type(exc).__name__)
            self._fail(FT_UPD_02)
            return False
        if extracted.verified.id != self.offer_id or extracted.manifest.version != self.version:
            # The private copy no longer is the file that was checked: drop it (a scan copies
            # the Downloads file again), but the offered file itself wasn't proven bad.
            log.error("%s: the update file changed since it was checked", FT_UPD_02)
            shutil.rmtree(long_path(extracted.path), ignore_errors=True)
            up.drop_offer_copy(self.offer_id)
            self._fail(FT_UPD_02)
            return False
        state.update(
            current=self.version,
            previous=up.current_version,
            pending={"version": self.version, "from": up.current_version,
                     "at": iso_utc(utcnow()), "id": self.offer_id},
            rollback=None,
        )
        try:
            write_install_state(root, state)
        except OSError as exc:
            log.error("%s: install state not written (%s)", FT_UPD_02, type(exc).__name__)
            shutil.rmtree(long_path(extracted.path), ignore_errors=True)
            self._fail(FT_UPD_02)
            return False
        return True


def file_refused(exc: UpdateRejected) -> bool:
    """Re-verification refused the FILE itself (signature, or its bytes don't match the
    signed hashes / lengths) -- not a read error of the user's disk (those are raised as
    "damaged" too, from an OSError)."""
    if exc.reason not in ("signature", "damaged"):
        return False
    return not isinstance(exc.__context__, OSError) and not isinstance(exc.__cause__, OSError)


def valid_id(value: object) -> bool:
    return isinstance(value, str) and _ID_RE.fullmatch(value) is not None


def rollback_target(state: dict[str, Any], current: str, versions_dir: Path) -> str | None:
    """Where a rollback of ``current`` goes: ``previous`` (else ``pending.from``), when it is
    an older version whose files are there."""
    pending = pending_of(state) or {}
    for candidate in (state.get("previous"), pending.get("from")):
        if (
            semver.is_valid(candidate) and semver.compare(candidate, current) < 0
            and version_ok(versions_dir, candidate)
        ):
            return candidate
    return None


def manual_rollback_target(state: dict[str, Any], current: str, versions_dir: Path) -> str | None:
    """Settings' optional "Go back to version X": only after a finished update (no pending,
    no rollback request), to a previous version that uses the same data format."""
    if pending_of(state) or state.get("rollback") or state.get("current") != current:
        return None
    previous = state.get("previous")
    if not (semver.is_valid(previous) and semver.compare(previous, current) < 0):
        return None
    if not version_ok(versions_dir, previous):
        return None
    if release_schema_version(versions_dir / previous) != LATEST:
        return None
    return previous


def exit_later(request_exit: Callable[[int], None], code: int, delay: float) -> None:
    def fire() -> None:
        try:
            request_exit(code)
        except Exception as exc:  # noqa: BLE001
            log.error("restart request failed: %s", type(exc).__name__)

    if delay <= 0:
        fire()
        return
    timer = threading.Timer(delay, fire)
    timer.daemon = True
    timer.start()

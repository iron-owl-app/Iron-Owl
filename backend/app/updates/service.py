"""The updater as the app uses it: one ``Updater`` per app (``AppState.updater``).

Ties the core (store, Downloads scanner, package checks) to the vault, the automatic
backups, the install root and ``request_exit``. The mode is decided once at startup;
in every mode but ``enabled`` nothing here scans, reads or creates ``data/updates/``,
and every action answers ``Disabled`` (409 ``disabled``).

Update sources (``source.py``): ``file`` scans Downloads and takes picked files, verified
with the file keys only; ``github`` checks GitHub Releases once a day (``github.py``; the
Settings switch, stored in ``data/updates/state.json``, turns the automatic check off) and
on "Check now", verified with the GitHub keys only; ``off`` answers ``Disabled`` to every
action (an install already under way still finishes: the unlock hook and the rollbacks don't
depend on the source). GitHub is only ever contacted while the vault is unlocked.

Responses carry file leaf names only (never a folder path); logs carry codes and exception
types only.
"""
from __future__ import annotations

import datetime as dt
import logging
import shutil
import sqlite3
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from sqlalchemy.exc import TimeoutError as PoolTimeout

from .. import update_keys
from ..db import DatabaseLocked, InvalidKey
from ..utils import iso_utc, today, utcnow
from . import downloads, github, semver
from .install import (
    UPDATE_BACKUPS_DIRNAME,
    EXIT_RESTART,
    FT_UPD_02,
    FT_UPD_05,
    InstallJob,
    InstallStateError,
    exit_later,
    file_refused,
    installed_at,
    manual_rollback_target,
    pending_of,
    prune_failed_updates,
    prune_snapshots,
    prune_update_backups,
    prune_versions,
    read_install_state,
    rollback_target,
    valid_id,
    write_install_state,
)
from .mode import UpdaterMode, mode_for_settings, support_contact
from .package import UpdateRejected, verify_package
from .requirements import Environment
from .source import UpdateSource, source_for_settings
from .store import StaleOffer, UpdateStore, is_valid_id, public_offer

log = logging.getLogger("fintrack.updates")

EXIT_DELAY_SECONDS = 1.0  # let the answer (and a progress poll) reach the window first
# A picked-file check or a Downloads scan can hold the offer for a few seconds (copying and
# verifying up to 150 MB); an install request waits this long for it, then answers busy.
OFFER_LOCK_WAIT_SECONDS = 60.0
# GitHub checks: once a day; after a failed try, again after a few hours; "Check now" at most
# once a minute (a quicker second click answers with the last result).
CHECK_INTERVAL = dt.timedelta(days=1)
RETRY_AFTER_ERROR = dt.timedelta(hours=3)
MANUAL_CHECK_GAP = dt.timedelta(seconds=60)

# SQLite errors that say "not now" (another program has the file, the disk hiccuped, it is
# full), not "this version can't upgrade the data": never a reason to go back to the copy
# made before the update. Matched against the driver's message, lowercased.
_TRANSIENT_DB_MESSAGES = (
    "database is locked", "database table is locked", "database is busy", "disk i/o error",
    "unable to open database file", "database or disk is full", "readonly database",
    "interrupted", "out of memory", "locking protocol", "schema has changed",
)


def _causes(exc: BaseException):
    """``exc`` and the errors behind it (``__cause__``/``__context__``, SQLAlchemy's
    ``orig``), each once."""
    seen: set[int] = set()
    todo: list[BaseException] = [exc]
    while todo:
        cur = todo.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        yield cur
        for nxt in (getattr(cur, "orig", None), cur.__cause__, cur.__context__):
            if isinstance(nxt, BaseException):
                todo.append(nxt)


def data_upgrade_failed(exc: BaseException) -> bool:
    """Whether a failed open (with the right key) means this version couldn't read or
    upgrade the data -- a migration or schema error -- rather than a passing problem of
    the user's computer (file in use, disk error, out of memory), which is raised as a normal error
    and never rolls an update back."""
    if isinstance(exc, (DatabaseLocked, InvalidKey, PoolTimeout)):
        return False
    # The error itself, SQLAlchemy's wrapped driver error (``orig``) and whatever it was
    # raised from or while handling (``__cause__``/``__context__``): a file or memory
    # problem anywhere in that chain is the computer's fault, not this version's.
    for err in _causes(exc):
        if isinstance(err, (OSError, MemoryError)):
            return False
        if isinstance(err, sqlite3.Error) or type(err).__name__ in ("OperationalError", "DatabaseError"):
            text = str(err).lower()
            if any(m in text for m in _TRANSIENT_DB_MESSAGES):
                return False
    return True


class Disabled(Exception):
    """The updater is off in this copy (git checkout, dev server, not installed)."""


class Busy(Exception):
    """An update is being installed (or a file is being checked)."""


class NeedsHelp(Exception):
    def __init__(self, help_: dict | None) -> None:
        super().__init__("needs_help")
        self.help = help_


class RollbackUnavailable(Exception):
    pass


class Updater:
    def __init__(
        self,
        settings: Any,
        vault: Any,
        *,
        auto_backup: Any = None,
        request_exit: Callable[[int], None] | None = None,
        mode: UpdaterMode | None = None,
        current_version: str | None = None,
        source: tuple[UpdateSource, str | None] | None = None,
        transport: github.Transport | None = None,
        now: Callable[[], dt.datetime] | None = None,
    ) -> None:
        from ..version import __version__

        self.settings = settings
        self.vault = vault
        self.auto_backup = auto_backup
        self.request_exit = request_exit or (lambda code: None)
        self.mode: UpdaterMode = mode or mode_for_settings(settings)
        self.current_version = current_version or __version__
        self.data_dir = Path(settings.data_dir)
        root = getattr(settings, "fintrack_install_root", None)
        self.install_root: Path | None = Path(root) if root else None
        self.exit_delay = EXIT_DELAY_SECONDS
        self.store = UpdateStore(self.data_dir)
        self.source: UpdateSource = "off"
        self.repo: str | None = None  # owner/name, github only (from the build's release.json)
        if self.enabled:
            self.source, self.repo = source if source is not None else source_for_settings(settings, self.mode)
        self.transport: github.Transport = transport or github.urllib_transport
        self.now: Callable[[], dt.datetime] = now or utcnow  # naive UTC
        self._check_lock = threading.Lock()  # one GitHub check at a time
        self.scanner: downloads.DownloadsScanner | None = None
        if self.enabled and self.source == "file":
            self.scanner = downloads.DownloadsScanner(
                self.store, current_version=self.current_version,
                # looked up at call time, so tests can replace it
                folder_resolver=lambda: downloads.downloads_folder(),
                env=self.environment(),
            )
        self._job: InstallJob | None = None
        self._job_lock = threading.Lock()
        self.check_lock = threading.Lock()  # one picked file checked at a time
        # Held while anything may change the offer (a picked-file check, a Downloads scan)
        # and while an install starts: an install never starts while the offer is being
        # replaced, and nothing replaces it once the install job runs (busy is re-checked
        # under this lock).
        self._offer_lock = threading.Lock()
        self._exit_lock = threading.Lock()
        self._exit_requested = False
        # Set at the top of every successful open (on_unlocked): once this process's version
        # has opened the data, a later failed open never asks to go back to the pre-update
        # copy (that would throw away what the user did since).
        self._opened = False

    # ------------------------------------------------------------------ basics

    @property
    def enabled(self) -> bool:
        return self.mode == "enabled" and self.install_root is not None

    def require_enabled(self) -> None:
        if not self.enabled:
            raise Disabled()

    @property
    def can_update(self) -> bool:
        """Installed, and updates not turned off (source file or github)."""
        return self.enabled and self.source != "off"

    def require_updates(self) -> None:
        if not self.can_update:
            raise Disabled()

    def trusted_keys(self) -> Mapping[str, str] | None:
        """The keys an update for this copy may be signed with: the file keys (None = the
        default, ``update_keys.TRUSTED_KEYS``, as before 2.0.0) or only the GitHub keys."""
        if self.source == "file":
            return None
        return update_keys.keys_for(self.source)  # github; "off" trusts nothing

    def environment(self) -> Environment:
        return Environment(install_root=self.install_root, data_dir=self.data_dir)

    @property
    def versions_dir(self) -> Path:
        assert self.install_root is not None
        return self.install_root / "versions"

    @property
    def busy(self) -> bool:
        job = self._job
        return job is not None and job.running

    def schedule_exit(self, code: int) -> None:
        with self._exit_lock:
            if self._exit_requested:
                return
            self._exit_requested = True
        exit_later(self.request_exit, code, self.exit_delay)

    def _install_state(self) -> dict | None:
        if not self.enabled:
            return None
        try:
            return read_install_state(self.install_root)
        except InstallStateError:
            return None

    # ------------------------------------------------------------------ startup / loop

    def startup(self) -> None:
        """Lifespan: take in the launcher's result, forget offers that aren't newer."""
        if not self.enabled:
            return
        result = self.store.ingest_launcher_result()  # failed / rolled back ids are never offered again
        if result is not None and result.get("outcome") == "rolled_back":
            prune_failed_updates(self.data_dir)  # the launcher just made another one
        self.store.drop_stale_offer(self.current_version)
        folder = self.data_dir / UPDATE_BACKUPS_DIRNAME
        if folder.is_dir():
            prune_update_backups(folder)

    def _scan_now(self) -> bool:
        """One Downloads scan, never while an install runs or starts, or a file is checked."""
        if self.scanner is None or self.busy:
            return False
        if not self._offer_lock.acquire(blocking=False):
            return False  # an install is starting or a picked file is being checked
        try:
            if self.busy:
                return False
            self.scanner.scan(self.mode)
            return True
        finally:
            self._offer_lock.release()

    def scan_tick(self, unlocked: bool) -> bool:
        """The automation loop (every 5 minutes), unlocked only: look in Downloads (file
        source), or check GitHub when the daily check is due (github source, switch on)."""
        if not self.can_update or not unlocked:
            return False
        if self.source == "github":
            return self.check_due() and self.check_github()
        return self._scan_now()

    # ------------------------------------------------------------------ GitHub checks

    @staticmethod
    def _parse_at(value: object) -> dt.datetime | None:
        if not isinstance(value, str):
            return None
        try:
            return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            return None

    def check_due(self) -> bool:
        """The automatic daily check: switched on, and a day since the last try (a few hours
        after a failed one). A last try "in the future" (the clock moved back) counts as due."""
        if self.source != "github" or not self.can_update:
            return False
        check = self.store.check_state()
        if not check.get("auto"):
            return False
        last = self._parse_at(check.get("last_try"))
        if last is None:
            return True
        elapsed = self.now() - last
        wait = RETRY_AFTER_ERROR if check.get("error") else CHECK_INTERVAL
        return elapsed >= wait or elapsed < dt.timedelta(0)

    def check_github(self, *, manual: bool = False) -> bool:
        """One GitHub check (never while an install runs; one at a time). Records the time
        and any error (plain words in the status). True when the check finished."""
        if self.source != "github" or not self.can_update or self.busy:
            return False
        if not self._check_lock.acquire(blocking=False):
            return False
        try:
            if manual:
                last = self._parse_at(self.store.check_state().get("last_try"))
                if last is not None and dt.timedelta(0) <= self.now() - last < MANUAL_CHECK_GAP:
                    return False
            at = iso_utc(self.now())
            error: str | None = None
            try:
                self._check_github(at)
            except github.CheckFailed as exc:
                error = exc.code
                log.info("update check stopped (%s)", exc.code)
            except OSError as exc:
                error = "disk"
                log.warning("update check stopped (%s)", type(exc).__name__)
            except Exception as exc:  # noqa: BLE001 - anything odd: a plain "didn't look right", still recorded
                error = "bad_answer"
                log.warning("update check stopped (%s)", type(exc).__name__)
            try:
                self.store.record_check(at=at, error=error)
            except OSError as exc:
                log.warning("update check not recorded (%s)", type(exc).__name__)
            return error is None
        finally:
            self._check_lock.release()

    def _check_github(self, at: str) -> None:
        keys = update_keys.keys_for("github")
        if not keys:
            raise github.CheckFailed("no_keys")  # nothing is asked or downloaded
        assert self.repo is not None
        release = github.latest_release(self.transport, self.repo)
        store, current = self.store, self.current_version
        store.drop_stale_offer(current)
        if release is None or semver.compare(release.version, current) <= 0:
            return  # up to date
        offer = store.offer()
        if offer and semver.is_valid(offer.get("version")) and semver.compare(offer["version"], release.version) >= 0:
            return  # already offered (or a picked file at least as new)
        key = store.seen_key(f"github:{release.asset_name}", release.size, release.asset_id)
        entry = store.seen().get(key)
        if isinstance(entry, dict) and entry.get("verdict") in downloads.SKIP_VERDICTS:
            if entry.get("verdict") == "rejected":
                raise github.CheckFailed("rejected")  # this exact file was refused before
            return  # installed, or failed to install before: never again
        incoming = downloads.new_incoming_dir(store)
        try:
            staged = incoming / "package.ftupdate"
            try:
                got = github.download(self.transport, release, staged, keys)
            except github.CheckFailed as exc:
                if exc.code == "rejected":  # the signature check failed: don't fetch it again
                    store.mark_seen(key, sha16=None, verdict="rejected", version=release.version, at=at)
                raise
            self._offer_downloaded(release, staged, got.sha256, keys, key, at)
        finally:
            shutil.rmtree(incoming, ignore_errors=True)

    def _offer_downloaded(self, release: github.Release, staged: Path, sha256: str,
                          keys: Mapping[str, str], key: str, at: str) -> None:
        """Verify the downloaded copy (GitHub keys only) and make it the offer, under the
        offer lock (an install never starts while the offer changes)."""
        if not self._offer_lock.acquire(timeout=OFFER_LOCK_WAIT_SECONDS):
            return  # busy: the next check downloads it again
        try:
            if self.busy:
                return
            store = self.store
            checked = downloads.check_staged_file(
                store, staged, file_name=release.asset_name, source="github",
                current_version=self.current_version, sha256=sha256, trusted_keys=keys,
                env=self.environment(),
            )
            verdict = checked.verdict
            if checked.version is not None and checked.version != release.version:
                verdict = "rejected"  # the file isn't the version its release names
            elif verdict == "ready" and checked.sha16 in store.installed_ids():
                verdict = "installed"
            store.mark_seen(key, sha16=checked.sha16, verdict=verdict, version=checked.version, at=at)
            if verdict == "rejected":
                if is_valid_id(checked.sha16):
                    current = store.offer()
                    if not current or current.get("id") != checked.sha16:
                        (store.inbox / f"{checked.sha16}{downloads.EXTENSION}").unlink(missing_ok=True)
                raise github.CheckFailed("rejected")
            if verdict == "ready" and checked.offer is not None and store.set_offer(checked.offer):
                downloads.prune_inbox(store, checked.offer["inbox"])
        finally:
            self._offer_lock.release()

    # ------------------------------------------------------------------ status

    def status(self) -> dict:
        """UpdateStatus. Never writes anything."""
        state = self._install_state()
        current = {
            "version": self.current_version,
            "display_version": semver.display(self.current_version),
            "installed_at": installed_at(state) if state else None,
            "last_update_at": None,
        }
        out: dict = {
            "mode": self.mode,
            "support_contact": support_contact(getattr(self.settings, "support_contact", "")) or None,
            "current": current,
            "offer": None,
            "banner": {"show": False, "remind_after": None},
            "install": None,
            "last_result": None,
            "rollback": {"available": False, "to_version": None},
            "source": self.source,
            "auto_check": False,
            "last_check": None,
            "check_error": None,
        }
        if not self.enabled:
            return out
        if self.source == "github":
            check = self.store.check_state()
            out["auto_check"] = bool(check.get("auto"))
            out["last_check"] = check.get("last_check")
            out["check_error"] = github.ERROR_TEXT.get(check.get("error") or "")
        current["last_update_at"] = self.store.last_update_at()
        if self.can_update:  # updates turned off: no offer, no banner
            out["offer"] = public_offer(self.store.offer())
            out["banner"] = self.store.banner(today(), install_running=self.busy)
        job = self._job
        if job is not None and (job.running or not job.acked):
            out["install"] = job.snapshot()
        out["last_result"] = self.store.last_result()
        to = None
        if state is not None and not self.busy:
            to = manual_rollback_target(state, self.current_version, self.versions_dir)
        out["rollback"] = {"available": to is not None, "to_version": to}
        return out

    def progress(self) -> dict | None:
        job = self._job
        if job is None or not (job.running or not job.acked):
            return None
        return job.snapshot()

    # ------------------------------------------------------------------ actions

    def scan(self, *, now: bool = False) -> dict:
        """POST /api/update/scan. File source: look in Downloads (30 s throttle). GitHub
        source: ``now`` ("Check now") checks right away (at most once a minute, even with the
        daily switch off); otherwise only when the daily check is due."""
        self.require_updates()
        if self.source == "github":
            if now:
                self.check_github(manual=True)
            elif self.check_due():
                self.check_github()
        else:
            self._scan_now()
        return self.status()

    def set_auto_check(self, on: bool) -> dict:
        """Settings: "Check for updates once a day" (github source only)."""
        self.require_updates()
        if self.source != "github":
            raise Disabled()
        self.store.set_auto_check(on)
        return self.status()

    def check_file(self, staged: Path, file_name: str) -> dict:
        """A picked file (already staged in ``data/updates/.incoming-*``): UpdateCheck.

        Under the offer lock, with busy checked again once it is held: a check that started
        before an install can't replace the offer the install job is using."""
        self.require_updates()
        if self.busy:
            raise Busy()
        if not self._offer_lock.acquire(timeout=OFFER_LOCK_WAIT_SECONDS):
            raise Busy()
        try:
            if self.busy:
                raise Busy()
            return downloads.accept_picked_file(
                self.store, staged, file_name=file_name, current_version=self.current_version,
                trusted_keys=self.trusted_keys(), env=self.environment(),
            )
        finally:
            self._offer_lock.release()

    def dismiss(self, offer_id: str) -> dict:
        self.require_updates()
        if not valid_id(offer_id):
            raise StaleOffer(offer_id)
        self.store.dismiss(offer_id, today())
        return self.status()

    def ack_result(self, at: str) -> None:
        self.require_enabled()
        self.store.ack_result(at)
        job = self._job
        if job is not None and not job.running and job.snapshot()["at"] == at:
            job.acked = True

    def mark_needs_help(self, offer_id: str, help_: dict) -> None:
        offer = self.store.offer()
        if offer and offer["id"] == offer_id:
            self.store.set_offer({**offer, "can_install": False, "help": help_})

    def drop_offer_copy(self, offer_id: str) -> None:
        """The private copy no longer matches its offer: forget both (best effort). The
        Downloads file is checked again by the next scan; nothing is recorded as failed."""
        try:
            self.store.clear_offer(offer_id)
            if valid_id(offer_id):
                (self.store.inbox / f"{offer_id}{downloads.EXTENSION}").unlink(missing_ok=True)
        except OSError as exc:
            log.warning("update copy not removed (%s)", type(exc).__name__)

    def start_install(self, offer_id: str) -> dict:
        """POST /api/update/install: checks, then the InstallJob. Returns UpdateProgress."""
        self.require_updates()
        if not self._offer_lock.acquire(timeout=OFFER_LOCK_WAIT_SECONDS):
            raise Busy()
        try:
            with self._job_lock:
                return self._start_install_locked(offer_id)
        finally:
            self._offer_lock.release()

    def _start_install_locked(self, offer_id: str) -> dict:
        if self.busy:
            raise Busy()
        offer = self.store.offer()
        if not valid_id(offer_id) or offer is None or offer["id"] != offer_id:
            raise StaleOffer(offer_id)
        if not offer.get("can_install"):
            raise NeedsHelp(offer.get("help"))
        path = self.store.offer_package()
        if path is None:
            self.store.clear_offer(offer_id)
            raise StaleOffer(offer_id)
        failed_code = None
        blacklist = False
        try:
            verified = verify_package(path, self.current_version, trusted_keys=self.trusted_keys(),
                                      env=self.environment())
        except UpdateRejected as exc:
            log.error("%s: the update file failed its check before installing (%s)", FT_UPD_02, exc.code)
            failed_code = FT_UPD_02
            # Only a refused signature or hash condemns this exact file.
            blacklist = file_refused(exc)
        except (OSError, ValueError) as exc:
            # The user's disk or a program holding the file: the file itself may be fine.
            log.error("%s: the update file couldn't be checked before installing (%s)",
                      FT_UPD_02, type(exc).__name__)
            failed_code = FT_UPD_02
        else:
            if verified.result != "ready":
                self.store.clear_offer(offer_id)
                raise StaleOffer(offer_id)
            if verified.id != offer_id or verified.manifest.version != offer["version"]:
                log.error("%s: the update file changed since it was checked", FT_UPD_02)
                self.drop_offer_copy(offer_id)
                failed_code = FT_UPD_02
            elif not verified.can_install:
                help_ = verified.help.as_dict() if verified.help else None
                self.mark_needs_help(offer_id, help_)
                raise NeedsHelp(help_)
        job = InstallJob(self, offer, failed_code=failed_code, blacklist=blacklist)
        self._job = job
        if failed_code is None:
            self.store.hold_offer(offer_id)
            job.start()
        return job.snapshot()

    # ------------------------------------------------------------------ vault hooks

    def on_unlocked(self) -> None:
        """After every successful open: the first one of a just-installed version finishes
        the update. Never raises."""
        # First, before anything can fail: this version has opened the data, so from now
        # on nothing in this process asks to go back to the pre-update copy.
        self._opened = True
        if not self.enabled:
            return
        try:
            self._finish_update()
        except Exception as exc:  # noqa: BLE001 - unlocking never fails because of this
            log.error("finishing the update failed: %s", type(exc).__name__)

    def _finish_update(self) -> None:
        state = self._install_state()
        if state is None or state.get("rollback") or state.get("current") != self.current_version:
            return
        pending = pending_of(state)
        if pending is None or pending["version"] != self.current_version:
            return
        at = iso_utc(utcnow())
        from_version = pending.get("from") if semver.is_valid(pending.get("from")) else state.get("previous")
        if not semver.is_valid(from_version):
            from_version = self.current_version
        offer_id = pending.get("id") if valid_id(pending.get("id")) else None
        # 1. pending cleared FIRST (atomic write, retried on a sharing violation): once they
        # have used this version, a later failed open can never roll their data back to the
        # copy made before the update. If this write fails, the in-process flag still stops
        # a rollback, and the next open tries again.
        state["pending"] = None
        write_install_state(self.install_root, state)
        log.info("update to %s finished", self.current_version)
        # 2. The rest is bookkeeping: each step best effort, none undoes step 1.
        self._best_effort("history", lambda: self._record_installed(offer_id, from_version, at))
        keep = {self.current_version}
        if semver.is_valid(state.get("previous")):
            keep.add(state["previous"])
        self._best_effort("prune versions", lambda: prune_versions(self.versions_dir, keep))
        self._best_effort("prune snapshots", lambda: prune_snapshots(self.data_dir))
        self._best_effort("prune failed updates", lambda: prune_failed_updates(self.data_dir))
        self._best_effort("drop offer", lambda: self.store.drop_stale_offer(self.current_version))

    def _record_installed(self, offer_id: str | None, from_version: str, at: str) -> None:
        history = self.store.history()
        last = history[-1] if history else None
        if not (last and last.get("id") == offer_id and last.get("to_version") == self.current_version):
            self.store.add_history(offer_id=offer_id, from_version=from_version,
                                   to_version=self.current_version, at=at)
        self.store.set_last_result({
            "outcome": "installed", "from_version": from_version, "to_version": self.current_version,
            "code": None, "at": at, "backup_kept": True,
        })

    @staticmethod
    def _best_effort(what: str, fn: Callable[[], object]) -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            log.error("after the update: %s failed (%s)", what, type(exc).__name__)

    def on_open_failed(self, exc: BaseException) -> str | None:
        """The right key, but this version couldn't open or upgrade the data (``exc``).
        Only while a just-installed update of THIS version is pending, no open of it has
        succeeded in this process, THIS open started a schema upgrade (the vault's
        ``migration_started``: data already at the latest schema never goes back), and
        ``exc`` is a data-upgrade (migration/schema) error: ask the launcher to go back
        (FT-UPD-05) and restart. Returns the version it goes back to, or None (not an
        update problem: the error is raised as it is)."""
        if self._opened or getattr(self.vault, "opened_once", False) is True:
            log.warning("open failed after this version already opened the data: no rollback")
            return None
        if getattr(self.vault, "migration_started", False) is not True:
            return None  # no data upgrade began in this open: not this update's doing
        if not data_upgrade_failed(exc):
            return None  # a passing problem (file in use, disk error): the user can try again
        state = self._install_state()
        if state is None or state.get("current") != self.current_version:
            return None
        pending = pending_of(state)
        if pending is None or pending["version"] != self.current_version:
            return None
        request = state.get("rollback")
        if isinstance(request, dict) and semver.is_valid(request.get("to")):
            self.schedule_exit(EXIT_RESTART)
            return request["to"]
        to = rollback_target(state, self.current_version, self.versions_dir)
        if to is None:
            log.error("%s: no previous version to go back to", FT_UPD_05)
            return None
        state["rollback"] = {"to": to, "code": FT_UPD_05}
        write_install_state(self.install_root, state)
        log.error("%s: the new version couldn't update the data; going back to %s", FT_UPD_05, to)
        self.schedule_exit(EXIT_RESTART)
        return to

    # ------------------------------------------------------------------ manual rollback

    def rollback_available(self) -> str | None:
        state = self._install_state()
        if state is None or self.busy:
            return None
        return manual_rollback_target(state, self.current_version, self.versions_dir)

    def rollback(self) -> str:
        """Settings' "Go back to version X" (the caller checked the password): switch to the
        previous version (same data format, so no data is restored) and restart."""
        self.require_enabled()
        with self._job_lock:
            state = self._install_state()
            to = None
            if state is not None and not self.busy:
                to = manual_rollback_target(state, self.current_version, self.versions_dir)
            if to is None:
                raise RollbackUnavailable()
            state.update(current=to, previous=None, pending=None, rollback=None)
            write_install_state(self.install_root, state)
        log.info("going back to version %s (chosen in Settings)", to)
        self.schedule_exit(EXIT_RESTART)
        return to

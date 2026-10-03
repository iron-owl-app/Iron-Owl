"""The updater's small state file: ``data/updates/state.json``.

Holds the current offer, per-offer "Not now" dates, the Downloads ``seen`` cache, install
history, the ids of updates that failed or were rolled back (never offered again) and the
last result (ingested from the launcher's ``data/updates/update_result.json``).
Everything is plain JSON rewritten atomically (tmp + fsync + ``os.replace``); a missing or
malformed file reads as empty, never as an error, and every
field is re-validated on load (an id that isn't 16 hex digits is dropped before anything
builds a path from it). Reading never creates ``data/updates/``
(git/dev copies must not grow one); only a write does.

Not vault data: no amounts or names, only versions, dates, file names and hashes.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any

from ..security import write_json_atomic
from . import semver
from .package import VerifiedPackage

log = logging.getLogger("fintrack.updates")

STATE_VERSION = 1
UPDATES_DIRNAME = "updates"
STATE_NAME = "state.json"
LAUNCHER_RESULT_NAME = "update_result.json"
INBOX_DIRNAME = "inbox"
MAX_SEEN = 200
MAX_HISTORY = 50
MAX_FAILED = 50
MAX_RESULT_BYTES = 4096
OUTCOMES = frozenset({"installed", "failed", "rolled_back"})
_CODE_RE = re.compile(r"^FT-UPD-[0-9]{2}$")
_ISO_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.+\-Z]{8,32}$")
_ID_RE = re.compile(r"^[0-9a-f]{16}$")
VERDICTS = frozenset({"ready", "rejected", "older", "same", "installed", "failed"})
# Launcher results that condemn the update itself (never offered again): it didn't start
# (FT-UPD-03) or couldn't upgrade the data (FT-UPD-05). FT-UPD-01 (the launcher's copy
# before the update failed, e.g. a full disk) is a problem with the user's computer, not with the file.
LAUNCHER_FAILED_CODES = frozenset({"FT-UPD-03", "FT-UPD-05"})


def is_valid_id(value: object) -> bool:
    """An offer id / sha16: exactly 16 lowercase hex digits (safe to put in a file name)."""
    return isinstance(value, str) and _ID_RE.fullmatch(value) is not None


class StaleOffer(Exception):
    """The id doesn't name the current offer (404 stale_offer)."""


def updates_dir(data_dir: Path) -> Path:
    return Path(data_dir) / UPDATES_DIRNAME


CHECK_ERRORS = frozenset({"no_keys", "offline", "busy", "bad_answer", "too_large", "rejected", "disk"})


def empty_check() -> dict[str, Any]:
    """GitHub checks (source ``github`` only): the Settings switch "Check for updates once a
    day" (``auto``, on unless turned off), the last successful check, the last try and its
    error code (``github.ERROR_TEXT`` has the words)."""
    return {"auto": True, "last_check": None, "last_try": None, "error": None}


def empty_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION, "offer": None, "dismissals": {}, "seen": {},
        "history": [], "failed": [], "last_result": None, "check": empty_check(),
    }


def make_offer(verified: VerifiedPackage, *, source: str, file_name: str, inbox_name: str) -> dict:
    """An UpdateOffer (frontend type) plus the private ``inbox`` file name and sha256."""
    m = verified.manifest
    return {
        "id": verified.id,
        "version": m.version,
        "display_version": m.display_version,
        "released_at": m.released_at,
        "notes": list(m.notes),
        "kind": m.kind,
        "minutes": m.minutes,
        "size_bytes": verified.size,
        "source": source,
        "file_name": leaf_name(file_name),
        "can_install": verified.can_install,
        "help": verified.help.as_dict() if verified.help else None,
        # private (the router strips these):
        "sha256": verified.sha256,
        "inbox": inbox_name,
    }


PRIVATE_OFFER_KEYS = ("sha256", "inbox")


def public_offer(offer: dict | None) -> dict | None:
    if offer is None:
        return None
    return {k: v for k, v in offer.items() if k not in PRIVATE_OFFER_KEYS}


def leaf_name(name: str) -> str:
    """Only the file's own name, never a folder path, at most 120 characters."""
    leaf = re.split(r"[\\/]", str(name))[-1]
    leaf = "".join(ch for ch in leaf if ch.isprintable())
    return leaf[:120]


def _valid_iso(value: object) -> bool:
    return isinstance(value, str) and _ISO_RE.fullmatch(value) is not None


def parse_launcher_result(raw: bytes) -> dict | None:
    """Validate the launcher's update_result.json; None when malformed."""
    if len(raw) > MAX_RESULT_BYTES:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    keys = {"outcome", "from_version", "to_version", "code", "at", "backup_kept"}
    if not keys <= set(data):
        return None
    code = data["code"]
    ok = (
        (data.get("id") is None or is_valid_id(data["id"]))
        and 
        data["outcome"] in OUTCOMES
        and semver.is_valid(data["from_version"])
        and semver.is_valid(data["to_version"])
        and (code is None or (isinstance(code, str) and _CODE_RE.fullmatch(code) is not None))
        and _valid_iso(data["at"])
        and isinstance(data["backup_kept"], bool)
    )
    if not ok:
        return None
    result = {k: data[k] for k in keys}
    if data.get("id") is not None:
        result["id"] = data["id"]  # which update (the launcher copies it from ``pending``)
    return result


def _valid_seen_entry(entry: object) -> bool:
    return (
        isinstance(entry, dict)
        and entry.get("verdict") in VERDICTS
        and (entry.get("sha16") is None or is_valid_id(entry.get("sha16")))
        and (entry.get("version") is None or semver.is_valid(entry.get("version")))
    )


class UpdateStore:
    """Thread-safe access to ``data/updates/state.json``. One instance per app."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self._lock = threading.RLock()
        # The offer an install job is using (in memory only): while set, no other offer
        # replaces it (``set_offer`` refuses), whoever calls.
        self._held: str | None = None

    def hold_offer(self, offer_id: str) -> None:
        with self._lock:
            self._held = offer_id

    def release_offer(self, offer_id: str) -> None:
        with self._lock:
            if self._held == offer_id:
                self._held = None

    # -------------------------------------------------------------- paths

    @property
    def dir(self) -> Path:
        return updates_dir(self.data_dir)

    @property
    def path(self) -> Path:
        return self.dir / STATE_NAME

    @property
    def inbox(self) -> Path:
        return self.dir / INBOX_DIRNAME

    @property
    def launcher_result_path(self) -> Path:
        return self.dir / LAUNCHER_RESULT_NAME

    # -------------------------------------------------------------- load / save

    def load(self) -> dict[str, Any]:
        with self._lock:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return empty_state()
            except (OSError, ValueError, UnicodeDecodeError) as exc:
                log.warning("update state unreadable (%s); starting empty", type(exc).__name__)
                return empty_state()
            return self._normalize(raw)

    @staticmethod
    def _normalize(raw: object) -> dict[str, Any]:
        state = empty_state()
        if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
            return state
        offer = raw.get("offer")
        if (
            isinstance(offer, dict)
            and is_valid_id(offer.get("id"))
            and offer.get("inbox") == f"{offer['id']}.ftupdate"
            and semver.is_valid(offer.get("version"))
        ):
            state["offer"] = offer
        if isinstance(raw.get("dismissals"), dict):
            state["dismissals"] = {
                k: v for k, v in raw["dismissals"].items() if is_valid_id(k) and isinstance(v, str)
            }
        if isinstance(raw.get("seen"), dict):
            state["seen"] = {k: v for k, v in raw["seen"].items() if isinstance(k, str) and _valid_seen_entry(v)}
        if isinstance(raw.get("history"), list):
            state["history"] = [
                h for h in raw["history"]
                if isinstance(h, dict) and (h.get("id") is None or is_valid_id(h.get("id")))
            ][-MAX_HISTORY:]
        if isinstance(raw.get("failed"), list):
            state["failed"] = [
                f for f in raw["failed"] if isinstance(f, dict) and is_valid_id(f.get("id"))
            ][-MAX_FAILED:]
        if isinstance(raw.get("last_result"), dict):
            state["last_result"] = raw["last_result"]
        if state["offer"] and state["offer"]["id"] in {f["id"] for f in state["failed"]}:
            state["offer"] = None
        check = raw.get("check")
        if isinstance(check, dict):
            if isinstance(check.get("auto"), bool):
                state["check"]["auto"] = check["auto"]
            for key in ("last_check", "last_try"):
                if _valid_iso(check.get(key)):
                    state["check"][key] = check[key]
            if check.get("error") in CHECK_ERRORS:
                state["check"]["error"] = check["error"]
        return state

    def save(self, state: dict[str, Any]) -> None:
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            write_json_atomic(self.path, state)

    def _update(self, fn) -> Any:
        with self._lock:
            state = self.load()
            before = copy.deepcopy(state)
            result = fn(state)
            if state != before:
                self.save(state)
            return result

    # -------------------------------------------------------------- offer

    def offer(self) -> dict | None:
        return self.load()["offer"]

    def offer_package(self) -> Path | None:
        """The current offer's private copy, ``inbox/<id>.ftupdate`` (the install step
        re-verifies it). The name is rebuilt from the validated id, never read as a path."""
        offer = self.offer()
        if not offer:
            return None
        path = self.inbox / f"{offer['id']}.ftupdate"
        return path if path.is_file() and not path.is_symlink() else None

    def set_offer(self, offer: dict) -> bool:
        """Replace the offer. A different offer id drops older "Not now" dates, so a newer
        update is announced even if the previous one was dismissed until tomorrow.

        Returns False (and changes nothing) for an id that failed or was rolled back before
        (those are never offered again), and for a different offer while an install job
        holds the current one (``hold_offer``)."""
        if not is_valid_id(offer.get("id")):
            raise ValueError("offer id")

        def fn(state):
            if self._held is not None and offer["id"] != self._held:
                return False
            if offer["id"] in {f["id"] for f in state["failed"]}:
                return False
            state["offer"] = offer
            state["dismissals"] = {k: v for k, v in state["dismissals"].items() if k == offer["id"]}
            return True
        return self._update(fn)

    def clear_offer(self, offer_id: str | None = None) -> None:
        def fn(state):
            if state["offer"] and (offer_id is None or state["offer"]["id"] == offer_id):
                state["offer"] = None
                state["dismissals"] = {}
        self._update(fn)

    def drop_stale_offer(self, current_version: str) -> None:
        """After an update (or if the offer is somehow not newer), forget it."""
        def fn(state):
            offer = state["offer"]
            if offer and (not semver.is_valid(offer.get("version"))
                          or semver.compare(offer["version"], current_version) <= 0):
                state["offer"] = None
                state["dismissals"] = {}
        self._update(fn)

    def dismiss(self, offer_id: str, today: dt.date) -> str:
        """"Not now": hide the banner for this offer until tomorrow (local date)."""
        def fn(state):
            if not state["offer"] or state["offer"]["id"] != offer_id:
                raise StaleOffer(offer_id)
            remind = (today + dt.timedelta(days=1)).isoformat()
            state["dismissals"] = {offer_id: remind}
            return remind
        return self._update(fn)

    def banner(self, today: dt.date, *, install_running: bool = False) -> dict:
        """{show, remind_after}. The caller also requires mode=enabled and unlocked.
        Needs-help offers never get a banner (Settings only)."""
        state = self.load()
        offer = state["offer"]
        if not offer:
            return {"show": False, "remind_after": None}
        remind = state["dismissals"].get(offer["id"])
        remind = remind if isinstance(remind, str) else None
        dismissed = remind is not None and today.isoformat() < remind
        show = bool(offer.get("can_install")) and not dismissed and not install_running
        return {"show": show, "remind_after": remind if dismissed else None}

    # -------------------------------------------------------------- GitHub checks

    def check_state(self) -> dict[str, Any]:
        return dict(self.load()["check"])

    def set_auto_check(self, on: bool) -> None:
        def fn(state):
            state["check"]["auto"] = bool(on)
        self._update(fn)

    def record_check(self, *, at: str, error: str | None) -> None:
        """One GitHub check finished (``error`` None) or stopped (an error code)."""
        if error is not None and error not in CHECK_ERRORS:
            raise ValueError("check error")

        def fn(state):
            check = state["check"]
            check["last_try"] = at
            check["error"] = error
            if error is None:
                check["last_check"] = at
        self._update(fn)

    # -------------------------------------------------------------- seen cache

    @staticmethod
    def seen_key(name: str, size: int, mtime_ns: int) -> str:
        return f"{name}|{size}|{mtime_ns}"

    def seen(self) -> dict[str, dict]:
        return self.load()["seen"]

    def mark_seen(self, key: str, *, sha16: str | None, verdict: str, version: str | None, at: str) -> None:
        if verdict not in VERDICTS:
            raise ValueError("verdict")

        def fn(state):
            seen = state["seen"]
            seen.pop(key, None)
            seen[key] = {"sha16": sha16, "verdict": verdict, "version": version, "at": at}
            while len(seen) > MAX_SEEN:  # dicts keep insertion order: oldest first
                seen.pop(next(iter(seen)))
        self._update(fn)

    def forget_seen(self, key: str) -> None:
        self._update(lambda state: state["seen"].pop(key, None))

    # -------------------------------------------------------------- history / results

    def history(self) -> list[dict]:
        return self.load()["history"]

    def installed_ids(self) -> set[str]:
        return {h["id"] for h in self.history() if is_valid_id(h.get("id"))}

    # -------------------------------------------------------------- failed updates

    def failed(self) -> list[dict]:
        return self.load()["failed"]

    def failed_ids(self) -> set[str]:
        return {f["id"] for f in self.failed()}

    def failure(self, offer_id: str) -> dict | None:
        return next((f for f in reversed(self.failed()) if f["id"] == offer_id), None)

    def record_failed(self, *, offer_id: str, version: str | None, code: str | None, at: str) -> None:
        """An update that failed to install or was rolled back: never offer it again.

        Clears it as the offer and marks its Downloads files ``failed`` (the scan skips
        them); a newer release (a different file, so a different id) is offered as usual.
        The startup ingest calls this for the launcher's failed / rolled_back results
        (FT-UPD-03/05), the install job only when re-verification refused the file itself
        (signature or hash, FT-UPD-02) -- never for FT-UPD-01 or a disk/file error."""
        if not is_valid_id(offer_id):
            raise ValueError("offer id")

        def fn(state):
            state["failed"] = [f for f in state["failed"] if f["id"] != offer_id]
            state["failed"].append({
                "id": offer_id,
                "version": version if semver.is_valid(version) else None,
                "code": code if isinstance(code, str) and _CODE_RE.fullmatch(code) else None,
                "at": at,
            })
            state["failed"] = state["failed"][-MAX_FAILED:]
            for entry in state["seen"].values():
                if entry.get("sha16") == offer_id:
                    entry["verdict"] = "failed"
            if state["offer"] and state["offer"]["id"] == offer_id:
                state["offer"] = None
                state["dismissals"] = {}
        self._update(fn)

    def add_history(self, *, offer_id: str | None, from_version: str, to_version: str, at: str) -> None:
        def fn(state):
            state["history"].append(
                {"id": offer_id, "from_version": from_version, "to_version": to_version, "at": at}
            )
            state["history"] = state["history"][-MAX_HISTORY:]
            for entry in state["seen"].values():
                if offer_id and entry.get("sha16") == offer_id:
                    entry["verdict"] = "installed"
            if state["offer"] and state["offer"]["id"] == offer_id:
                state["offer"] = None
                state["dismissals"] = {}
        self._update(fn)

    def last_update_at(self) -> str | None:
        history = self.history()
        return history[-1].get("at") if history else None

    def last_result(self) -> dict | None:
        return self.load()["last_result"]

    def set_last_result(self, result: dict) -> None:
        def fn(state):
            state["last_result"] = dict(result)
        self._update(fn)

    def ack_result(self, at: str) -> bool:
        """POST /api/update/result/ack {at}: clears the result it names."""
        def fn(state):
            if state["last_result"] and state["last_result"].get("at") == at:
                state["last_result"] = None
                return True
            return False
        return self._update(fn)

    def ingest_launcher_result(self) -> dict | None:
        """At startup: take the launcher's update_result.json into last_result, delete it.

        A malformed file is deleted too (logged by type only). Returns the result."""
        path = self.launcher_result_path
        with self._lock:
            try:
                raw = path.read_bytes()
            except FileNotFoundError:
                return None
            except OSError as exc:
                log.warning("launcher update result unreadable (%s)", type(exc).__name__)
                return None
            result = parse_launcher_result(raw)
            if result is None:
                log.warning("launcher update result malformed; ignored")
            else:
                self.set_last_result({k: v for k, v in result.items() if k != "id"})
                if (
                    result["outcome"] in ("failed", "rolled_back") and result.get("id")
                    and result["code"] in LAUNCHER_FAILED_CODES
                ):
                    self.record_failed(offer_id=result["id"], version=result["to_version"],
                                       code=result["code"], at=result["at"])
            try:
                os.remove(path)
            except OSError as exc:
                log.warning("launcher update result not removed (%s)", type(exc).__name__)
            return result

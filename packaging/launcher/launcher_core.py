"""FinTrack launcher logic (stdlib only; runs under the bundled embeddable Python 3.11).

launch.pyw holds the Windows glue (named mutex, window focus, MessageBoxW); everything that
can be tested without Windows lives here and takes its side effects from a ``System``
object that tests replace with fakes.

Layout (see design/launcher/ABC-PLAN.md §B and UPDATES-MAPPING.md):

    INSTALL_ROOT  (%LOCALAPPDATA%\\Programs\\FinTrack)
        launch.pyw, launcher_core.py, install.ps1, uninstall.ps1, FinTrack.ico
        python\\                       embeddable CPython 3.11 x64 (python.exe, pythonw.exe)
        state.json                    see STATE CONTRACT below
        versions\\<X.Y.Z>\\             run_packaged.py, backend\\app\\, web\\, release.json
        versions\\.partial-*            an update being unpacked (removed when found here)
        runtimes\\<deps_id>\\site-packages
    FINTRACK_HOME (%LOCALAPPDATA%\\FinTrack; private to the user)
        data\\          fintrack.db, keyfile.json, backups, pre-update-<old>-<stamp>\\ snapshots
        data\\updates\\update_result.json   written here, read + deleted by the server
        browser\\       dedicated Edge profile for the app window
        logs\\          launcher.log, server.log
        run\\control.json  {"port", "secret", "pid", "started_at"} of the running server

STATE CONTRACT (install-root state.json, written atomically: temp file + os.replace;
unknown keys are preserved on every rewrite):

    {"schema": 1, "bootstrap_version": 1,
     "current": "1.4.0",                  version folder to run
     "previous": "1.3.0" | null,          kept for rollback
     "pending": null | {"version": "1.5.0", "from": "1.4.0", "at": "<iso>",
                        "id": "<16 hex: the update file's id>" | absent,
                        "snapshot": "<data subfolder>" | "" (nothing to copy) | absent,
                        "gate_passed": true | absent},
     "rollback": null | {"to": "1.4.0", "code": "FT-UPD-05"},
     "port": 8000, "support_contact": "Sam", "installed_at": "<iso>"}
    (a name in FINTRACK_HOME\\data\\support_contact.json, saved in Settings, wins over
    support_contact here; see read_saved_contact)

The updater installs versions\\<new>, writes current=<new>, previous=<old>,
pending={version,from,at,id} and exits with 75. A failed or rolled-back update's
update_result.json carries that ``id`` (so the server never offers the same file again). The launcher then snapshots the vault into
data\\pre-update-<old>-<stamp>\\ (recorded as pending.snapshot), starts <new> on the same port
and waits up to 60 s for a healthy answer reporting version == pending.version. Pass: sets
pending.gate_passed on a fresh read of state.json (the server clears pending after the first
unlock, possibly already; a cleared pending is never written back). Fail: current=previous,
pending=null, versions\\<new> deleted, update_result.json {outcome "failed", code FT-UPD-03}
and the previous version is started (30 s, else the "couldn't start" message box).
When the server finds the new version can't open the data, it writes rollback={to, code} and
exits 75: the launcher copies the snapshot into a staging folder in data\\, moves the live
vault files to data\\failed-update-<new>-<stamp>\\ and the staged copy in, switches to ``to``
and writes {outcome "rolled_back", code}. If any step fails the live files are moved back,
state.json is left unchanged (the rollback stays pending) and the user gets the "couldn't
start" box (FT-UPD-06); versions are never switched onto a half-restored data folder.
Exit 75 with neither pending nor rollback = plain restart of ``current``.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import http.client
import json
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("fintrack.launcher")

BOOTSTRAP_VERSION = 1
STATE_SCHEMA = 1

PORT_FIRST = 8000
PORT_LAST = 8010

START_TIMEOUT = 45.0
UPDATE_GATE_TIMEOUT = 60.0
ROLLBACK_GATE_TIMEOUT = 30.0
POLL_INTERVAL = 0.25
PROBE_TIMEOUT = 2.0
CONNECT_TIMEOUT = 0.5
MUTEX_WAIT_ON_RESTART = 60.0

EXIT_RESTART = 75
# Heartbeat self-exit handed to the server (EXIT_WHEN_UNUSED_SECONDS). 0 = off.
# Every FinTrack window (locked or not) posts /api/app/alive every 30 s, and a window whose
# server went away shows "FinTrack isn't running", so 3 minutes without one means no window.
EXIT_WHEN_UNUSED_SECONDS = 180
SNAPSHOT_KEEP = 2
SUPPORT_CONTACT_MAX = 60

VERSION_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}$")
DEPS_ID_RE = re.compile(r"^[0-9a-f]{16}$")
SNAPSHOT_RE = re.compile(r"^pre-update-(\d{1,3}\.\d{1,3}\.\d{1,3})-(\d{8}T\d{6}Z)(?:-(\d+))?$")
CODE_RE = re.compile(r"^FT-[A-Z]{3,5}-[0-9A-Z]{2,10}$")

# Codes (logged only; never shown in the message box). See APPWINDOW-MAPPING.md.
FT_START_01 = "FT-START-01"  # no healthy answer within the start timeout
FT_START_02 = "FT-START-02"  # (web side) page loaded but the service is unreachable
FT_START_03 = "FT-START-03"  # the server exited while starting
FT_START_04 = "FT-START-04"  # no free port in 8000-8010
FT_START_05 = "FT-START-05"  # runtime or files missing (state.json, version, web, python)
FT_START_06 = "FT-START-06"  # no browser could be opened
FT_START_07 = "FT-START-07"  # something else answers on the port (health proof failed)
FT_START_08 = "FT-START-08"  # our last server still runs but doesn't answer, and couldn't be stopped
FT_UPD_01 = "FT-UPD-01"  # the pre-update copy of the data failed
FT_UPD_03 = "FT-UPD-03"  # the new version didn't start
FT_UPD_05 = "FT-UPD-05"  # the new version couldn't update the data (server asked to roll back)
FT_UPD_06 = "FT-UPD-06"  # the pre-update copy couldn't be put back (the rollback stays pending)
# Start failures that mean the new version itself is broken (the update is undone).
NEW_VERSION_FAULTS = frozenset({FT_START_01, FT_START_03, FT_START_05})

VAULT_FILES = (
    "fintrack.db", "fintrack.db-wal", "fintrack.db-shm", "fintrack.db-journal",
    "keyfile.json", "keyfile.json.new",
)

# Settings the server reads from its environment: the launcher decides all of them, so
# stray values in the user's environment can't change where data lives or what is exposed.
_SERVER_ENV_PREFIXES = ("PYTHON", "FINTRACK_", "PLAID_")
_SERVER_ENV_KEYS = frozenset({
    "AUTO_LOCK_MINUTES", "SUPPORT_CONTACT", "HOST", "PORT", "REPO_ROOT", "DATA_DIR",
    "FRONTEND_DIST", "EXIT_WHEN_UNUSED_SECONDS", "CONTROL_SECRET", "DEBUG", "DEV", "DEV_PORT",
})

EDGE_FLAGS = (
    "--no-first-run",
    "--no-default-browser-check",
    "--window-size=1280,860",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    "--hide-crash-restore-bubble",
)

MESSAGE_TITLE = "Iron Owl"
_GENERIC_HELP = "ask the person who set up Iron Owl for help"


# ------------------------------------------------------------------------------ helpers


class StateError(Exception):
    """state.json is missing or unusable (FT-START-05)."""


def utc_stamp(when: dt.datetime) -> str:
    return when.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def iso(when: dt.datetime) -> str:
    return when.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def version_key(version: str) -> tuple[int, int, int]:
    major, minor, patch = (int(p) for p in version.split("."))
    return major, minor, patch


def clean_contact(value: Any) -> str:
    """Printable, trimmed, at most SUPPORT_CONTACT_MAX characters ("" when unusable)."""
    if not isinstance(value, str):
        return ""
    text = "".join(ch for ch in value if ch.isprintable()).strip()
    return text[:SUPPORT_CONTACT_MAX].strip()


SUPPORT_CONTACT_FILE = "support_contact.json"  # in data\; written by Settings and install.ps1
SUPPORT_CONTACT_FILE_MAX = 4096


def read_saved_contact(paths: "Paths") -> str | None:
    """The name saved in Settings or by the installer (data\\support_contact.json; the
    server's ``services/support_contact.py`` reads the same file). "" = no one named; None
    when there is no usable file (then state.json's ``support_contact`` applies)."""
    try:
        with open(paths.data / SUPPORT_CONTACT_FILE, "rb") as fh:
            raw = fh.read(SUPPORT_CONTACT_FILE_MAX + 1)
        if len(raw) > SUPPORT_CONTACT_FILE_MAX:
            return None
        data = json.loads(raw.decode("utf-8-sig"))
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("support_contact"), str):
        return None
    return clean_contact(data["support_contact"])


def error_message(contact: str) -> str:
    """The "couldn't start" message box text (codes go to the log only)."""
    who = clean_contact(contact)
    tail = f"call {who}." if who else f"{_GENERIC_HELP}."
    return (
        "Iron Owl couldn’t start.\n\n"
        f"Restart your computer and try again. If it still doesn’t work, {tail}"
    )


REPLACE_RETRIES = 3
REPLACE_RETRY_DELAY = 0.1


def replace_with_retry(src: Path, dest: Path, sleep: Callable[[float], None] | None = None) -> None:
    """os.replace, retried up to REPLACE_RETRIES times on PermissionError: on Windows a
    virus scanner or the indexer can hold the target open for a moment."""
    sleep = sleep or time.sleep
    for attempt in range(REPLACE_RETRIES + 1):
        try:
            os.replace(src, dest)
            return
        except PermissionError:
            if attempt == REPLACE_RETRIES:
                raise
            sleep(REPLACE_RETRY_DELAY * (attempt + 1))


def write_json_atomic(path: Path, data: Mapping[str, Any]) -> None:
    """Temp file in the same folder, fsync, then os.replace (never a half-written file)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        replace_with_retry(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def read_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# -------------------------------------------------------------------------------- paths


@dataclass(frozen=True)
class Paths:
    install_root: Path
    home: Path

    @property
    def state_file(self) -> Path:
        return self.install_root / "state.json"

    @property
    def versions(self) -> Path:
        return self.install_root / "versions"

    @property
    def runtimes(self) -> Path:
        return self.install_root / "runtimes"

    @property
    def data(self) -> Path:
        return self.home / "data"

    @property
    def logs(self) -> Path:
        return self.home / "logs"

    @property
    def browser_profile(self) -> Path:
        return self.home / "browser"

    @property
    def control_file(self) -> Path:
        return self.home / "run" / "control.json"

    @property
    def update_result(self) -> Path:
        return self.data / "updates" / "update_result.json"

    def version_dir(self, version: str) -> Path:
        if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
            raise StateError("invalid version")
        return self.versions / version


def default_home(environ: Mapping[str, str]) -> Path:
    override = environ.get("FINTRACK_HOME", "").strip()
    if override:
        return Path(override)
    local = environ.get("LOCALAPPDATA", "").strip()
    if not local:
        raise StateError("LOCALAPPDATA is not set")
    return Path(local) / "FinTrack"


# -------------------------------------------------------------------------------- state


def _check_version(value: Any, what: str, optional: bool = False) -> None:
    if value is None and optional:
        return
    if not isinstance(value, str) or not VERSION_RE.fullmatch(value):
        raise StateError(f"state.json: bad {what}")


def validate_state(state: Any) -> dict[str, Any]:
    """Check the fields the launcher relies on; unknown keys are kept as they are."""
    if not isinstance(state, dict):
        raise StateError("state.json: not an object")
    _check_version(state.get("current"), "current")
    _check_version(state.get("previous"), "previous", optional=True)
    pending = state.get("pending")
    if pending is not None:
        if isinstance(pending, str):  # tolerate the short form "pending": "1.5.0"
            pending = {"version": pending}
            state["pending"] = pending
        if not isinstance(pending, dict):
            raise StateError("state.json: bad pending")
        _check_version(pending.get("version"), "pending.version")
        _check_version(pending.get("from"), "pending.from", optional=True)
        snap = pending.get("snapshot")
        if snap not in (None, "") and not (isinstance(snap, str) and SNAPSHOT_RE.fullmatch(snap)):
            raise StateError("state.json: bad pending.snapshot")
    rollback = state.get("rollback")
    if rollback is not None:
        if not isinstance(rollback, dict):
            raise StateError("state.json: bad rollback")
        _check_version(rollback.get("to"), "rollback.to")
        code = rollback.get("code")
        if code is not None and not (isinstance(code, str) and CODE_RE.fullmatch(code)):
            raise StateError("state.json: bad rollback.code")
    port = state.get("port")
    if port is not None and not (isinstance(port, int) and not isinstance(port, bool) and 1024 <= port <= 65535):
        state["port"] = None  # a bad port is not fatal: pick a free one
    return state


def read_state(path: Path) -> dict[str, Any]:
    try:
        data = read_json(path)
    except FileNotFoundError:
        raise StateError("state.json is missing") from None
    except (OSError, ValueError) as exc:
        raise StateError(f"state.json is unreadable ({type(exc).__name__})") from None
    return validate_state(data)


def write_state(path: Path, state: Mapping[str, Any]) -> None:
    write_json_atomic(path, state)


def pending_version(state: Mapping[str, Any]) -> str | None:
    pending = state.get("pending")
    return pending.get("version") if isinstance(pending, dict) else None


# -------------------------------------------------------------------------------- ports


def candidate_ports(preferred: int | None, first: int = PORT_FIRST, last: int = PORT_LAST) -> list[int]:
    ports = list(range(first, last + 1))
    if preferred in ports:
        ports.remove(preferred)
        ports.insert(0, preferred)
    return ports


def choose_port(
    preferred: int | None,
    is_free: Callable[[int], bool],
    first: int = PORT_FIRST,
    last: int = PORT_LAST,
    skip: Iterable[int] = (),
) -> int | None:
    skipped = set(skip)
    for port in candidate_ports(preferred, first, last):
        if port not in skipped and is_free(port):
            return port
    return None


def port_is_free(port: int) -> bool:
    """Can a server bind 127.0.0.1:port right now? (Same kind of bind uvicorn does.)"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            # Windows: fail if anything else holds the port, even with SO_REUSEADDR.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


# --------------------------------------------------------------------------------- edge

EDGE_RELATIVE = Path("Microsoft") / "Edge" / "Application" / "msedge.exe"


def edge_candidates(registry_values: Iterable[str], environ: Mapping[str, str]) -> list[Path]:
    """Where msedge.exe may be: App Paths registry values first, then the usual folders."""
    found: list[Path] = []
    for raw in registry_values:
        if not isinstance(raw, str):
            continue
        text = os.path.expandvars(raw.strip().strip('"'))
        if text.lower().endswith("msedge.exe"):
            found.append(Path(text))
    for var in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = environ.get(var, "").strip()
        if base:
            found.append(Path(base) / EDGE_RELATIVE)
    unique: list[Path] = []
    seen: set[str] = set()
    for path in found:
        key = str(path).lower()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def find_edge(
    registry_values: Iterable[str],
    environ: Mapping[str, str],
    exists: Callable[[Path], bool] = Path.is_file,
) -> Path | None:
    for path in edge_candidates(registry_values, environ):
        try:
            if exists(path):
                return path
        except OSError:
            continue
    return None


def registry_edge_paths() -> list[str]:
    """Default values of the msedge.exe "App Paths" keys (HKCU, then HKLM, both views)."""
    try:
        import winreg
    except ImportError:
        return []
    sub = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"
    values: list[str] = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, sub, 0, winreg.KEY_READ | view) as key:
                    value, _kind = winreg.QueryValueEx(key, "")
                    if isinstance(value, str):
                        values.append(value)
            except OSError:
                continue
    return values


def app_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/"


def edge_args(edge: Path, url: str, profile_dir: Path) -> list[str]:
    return [str(edge), f"--app={url}", f"--user-data-dir={profile_dir}", *EDGE_FLAGS]


# ------------------------------------------------------------------------------- health

PROOF_DOMAIN = b"fintrack-health-v1"
CHALLENGE_HEADER = "X-FinTrack-Control-Challenge"
CONTROL_HEADER = "X-FinTrack-Control"
_BOOT_ID_RE = re.compile(r"^[0-9a-f]{8,64}$")


def make_nonce() -> str:
    return secrets.token_urlsafe(24)  # 32 characters of [A-Za-z0-9_-]


def make_secret() -> str:
    return secrets.token_urlsafe(32)  # 43 characters


def expected_proof(secret: str, nonce: str, boot_id: str) -> str:
    """Same formula as backend app/lifecycle.py ``health_proof``."""
    message = b"\n".join((PROOF_DOMAIN, nonce.encode("ascii"), boot_id.encode("ascii")))
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class Health:
    status: str  # "ok" | "down" | "foreign" | "bad_proof"
    version: str | None = None
    boot_id: str | None = None
    web: bool = False
    window_open: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "ok"


DOWN = Health("down")


def evaluate_health(status_code: int, body: bytes, secret: str | None, nonce: str) -> Health:
    """Classify a /api/health answer. Only a correct HMAC proof counts as "our server"."""
    if status_code != 200:
        return Health("foreign")
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return Health("foreign")
    if not isinstance(data, dict) or data.get("app") != "fintrack":
        return Health("foreign")
    version = data.get("version") if isinstance(data.get("version"), str) else None
    boot_id = data.get("boot_id")
    if not isinstance(boot_id, str) or not _BOOT_ID_RE.fullmatch(boot_id):
        return Health("bad_proof", version=version)
    proof = data.get("proof")
    if not secret or not isinstance(proof, str):
        return Health("bad_proof", version=version, boot_id=boot_id)
    if not hmac.compare_digest(proof.encode("ascii", "replace"), expected_proof(secret, nonce, boot_id).encode("ascii")):
        return Health("bad_proof", version=version, boot_id=boot_id)
    return Health(
        "ok",
        version=version,
        boot_id=boot_id,
        web=data.get("web") is True,
        window_open=data.get("window_open") is True,
    )


def probe_health(port: int, secret: str | None, timeout: float = PROBE_TIMEOUT) -> Health:
    """GET /api/health on 127.0.0.1:port with a fresh challenge (no proxies involved).

    The connect gets a short timeout of its own: Windows retries a refused loopback connect
    for about 2 s, which would otherwise make every probe during start-up that slow.
    """
    nonce = make_nonce()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=min(timeout, CONNECT_TIMEOUT))
    try:
        conn.connect()
        conn.sock.settimeout(timeout)
        conn.request(
            "GET", "/api/health",
            headers={"Host": f"127.0.0.1:{port}", CHALLENGE_HEADER: nonce, "Accept": "application/json"},
        )
        response = conn.getresponse()
        body = response.read(64 * 1024)
        return evaluate_health(response.status, body, secret, nonce)
    except (OSError, http.client.HTTPException):
        return DOWN
    finally:
        conn.close()


def request_shutdown(port: int, secret: str, restart: bool = False, timeout: float = 5.0) -> bool:
    body = json.dumps({"restart": restart}).encode("utf-8")
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(
            "POST", "/api/app/shutdown", body=body,
            headers={
                "Host": f"127.0.0.1:{port}", "Content-Type": "application/json",
                "X-FinTrack": "1", CONTROL_HEADER: secret,
            },
        )
        return conn.getresponse().status == 202
    except (OSError, http.client.HTTPException):
        return False
    finally:
        conn.close()


# ------------------------------------------------------------------------------ control


def read_control(path: Path) -> tuple[int, str] | None:
    """(port, secret) of the server the last launcher started, if the file is sane."""
    try:
        data = read_json(path)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    port, secret = data.get("port"), data.get("secret")
    if not (isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535):
        return None
    if not (isinstance(secret, str) and 32 <= len(secret) <= 256):
        return None
    return port, secret


def read_control_pid(path: Path) -> int | None:
    """The server pid recorded in control.json, if any."""
    try:
        data = read_json(path)
    except (OSError, ValueError):
        return None
    pid = data.get("pid") if isinstance(data, dict) else None
    if isinstance(pid, int) and not isinstance(pid, bool) and 0 < pid < 2**32:
        return pid
    return None


def same_path(a: str | Path, b: str | Path) -> bool:
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


def write_control(path: Path, port: int, secret: str, pid: int | None, when: dt.datetime) -> None:
    write_json_atomic(path, {"port": port, "secret": secret, "pid": pid, "started_at": iso(when)})


# ------------------------------------------------------------------------ server launch


def server_env(
    base: Mapping[str, str],
    paths: Paths,
    version_dir: Path,
    port: int,
    secret: str,
    contact: str,
    exit_when_unused: int = EXIT_WHEN_UNUSED_SECONDS,
    test_downloads_dir: str | None = None,
) -> dict[str, str]:
    env = {
        k: v for k, v in base.items()
        if not k.upper().startswith(_SERVER_ENV_PREFIXES) and k.upper() not in _SERVER_ENV_KEYS
    }
    env.update({
        "FINTRACK_HOME": str(paths.home),
        # Explicit, so no settings file line can move the data somewhere else.
        "DATA_DIR": str(paths.data),
        "FINTRACK_WEB": str(version_dir / "web"),
        "FINTRACK_INSTALL_ROOT": str(paths.install_root),
        "HOST": "127.0.0.1",
        "PORT": str(port),
        "SUPPORT_CONTACT": clean_contact(contact),
        "EXIT_WHEN_UNUSED_SECONDS": str(int(exit_when_unused)),
        "CONTROL_SECRET": secret,
        "PYTHONIOENCODING": "utf-8",
    })
    if test_downloads_dir:
        # Testing only (Options.test_downloads_dir, set by an explicit launcher flag).
        env["FINTRACK_DOWNLOADS_DIR"] = str(test_downloads_dir)
    return env


def check_version_files(paths: Paths, version: str) -> str | None:
    """None when versions/<v> looks runnable, else FT-START-05."""
    try:
        vdir = paths.version_dir(version)
    except StateError:
        return FT_START_05
    if not (vdir / "run_packaged.py").is_file() or not (vdir / "backend" / "app" / "__init__.py").is_file():
        return FT_START_05
    if not (vdir / "web" / "index.html").is_file():
        return FT_START_05
    try:
        release = read_json(vdir / "release.json")
    except (OSError, ValueError):
        return FT_START_05
    deps_id = release.get("deps_id") if isinstance(release, dict) else None
    if isinstance(release, dict) and release.get("version") not in (None, version):
        return FT_START_05
    if not (isinstance(deps_id, str) and DEPS_ID_RE.fullmatch(deps_id)):
        return FT_START_05
    if not (paths.runtimes / deps_id / "site-packages").is_dir():
        return FT_START_05
    return None


def cleanup_partials(versions: Path) -> list[str]:
    """Remove versions/.partial-* left by an interrupted update unpack."""
    removed: list[str] = []
    try:
        entries = list(versions.iterdir())
    except OSError:
        return removed
    for entry in entries:
        if entry.name.startswith(".partial-") and entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry.name)
    return removed


# ----------------------------------------------------------------------- data snapshots


def _copy_file_synced(src: Path, dest: Path) -> None:
    with open(src, "rb") as fin, open(dest, "xb") as fout:
        shutil.copyfileobj(fin, fout, 1024 * 1024)
        fout.flush()
        os.fsync(fout.fileno())


def take_snapshot(data_dir: Path, old_version: str, when: dt.datetime) -> str:
    """Copy the vault files to data/pre-update-<old>-<stamp>/; "" when there is no vault yet.

    Copied under a staging name and renamed when complete, so a half-made copy never
    looks finished. Raises OSError on failure (the staging folder is removed).
    """
    present = [data_dir / name for name in VAULT_FILES if (data_dir / name).is_file()]
    if not present:
        return ""
    for leftover in data_dir.glob(".pre-update-tmp-*"):
        shutil.rmtree(leftover, ignore_errors=True)
    base = f"pre-update-{old_version}-{utc_stamp(when)}"
    name, n = base, 1
    while (data_dir / name).exists():
        n += 1
        name = f"{base}-{n}"
    staging = data_dir / f".pre-update-tmp-{secrets.token_hex(8)}"
    staging.mkdir()
    try:
        for src in present:
            _copy_file_synced(src, staging / src.name)
        os.rename(staging, data_dir / name)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return name


def prune_snapshots(data_dir: Path, keep: str, count: int = SNAPSHOT_KEEP) -> list[str]:
    """Keep ``keep`` plus the newest others, ``count`` in total (best effort)."""
    try:
        names = [p.name for p in data_dir.iterdir() if p.is_dir() and SNAPSHOT_RE.fullmatch(p.name)]
    except OSError:
        return []

    def stamp(name: str) -> tuple[str, int]:
        m = SNAPSHOT_RE.fullmatch(name)
        assert m is not None
        return m.group(2), int(m.group(3) or 1)

    others = sorted((n for n in names if n != keep), key=stamp, reverse=True)
    removed: list[str] = []
    for name in others[max(0, count - 1):]:
        shutil.rmtree(data_dir / name, ignore_errors=True)
        removed.append(name)
    return removed


def restore_snapshot(data_dir: Path, snapshot: str, failed_name: str) -> None:
    """Put the snapshot's vault files back; the live ones go to data/<failed_name>/.

    1. The snapshot is copied into a staging folder beside the data (nothing live is touched;
       a failed copy only removes the staging folder).
    2. The live vault files are moved into data/<failed_name>/, then the staged ones are
       moved in (renames on the same volume).
    3. If any move fails, what was moved in goes back to staging and the live files are
       moved back, so data/ is never left empty or half restored. Raises OSError on failure.
    """
    src = data_dir / snapshot
    if not SNAPSHOT_RE.fullmatch(snapshot) or not src.is_dir():
        raise FileNotFoundError("snapshot missing")
    saved = [name for name in VAULT_FILES if (src / name).is_file()]
    if not saved:
        raise FileNotFoundError("snapshot is empty")
    for leftover in data_dir.glob(".restore-tmp-*"):
        shutil.rmtree(leftover, ignore_errors=True)
    staging = data_dir / f".restore-tmp-{secrets.token_hex(8)}"
    staging.mkdir()
    failed = data_dir / failed_name
    try:
        for name in saved:
            _copy_file_synced(src / name, staging / name)
        failed.mkdir()
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    moved_out: list[str] = []
    moved_in: list[str] = []
    try:
        for name in VAULT_FILES:
            live = data_dir / name
            if live.is_file():
                os.replace(live, failed / name)
                moved_out.append(name)
        for name in saved:
            os.replace(staging / name, data_dir / name)
            moved_in.append(name)
    except BaseException:
        for name in reversed(moved_in):
            try:
                os.replace(data_dir / name, staging / name)
            except OSError:
                log.error("could not undo restoring %s", name)
        for name in reversed(moved_out):
            try:
                os.replace(failed / name, data_dir / name)
            except OSError:
                log.error("could not move %s back (it is in %s)", name, failed_name)
        shutil.rmtree(staging, ignore_errors=True)
        try:
            failed.rmdir()  # only when everything went back
        except OSError:
            pass
        raise
    shutil.rmtree(staging, ignore_errors=True)


def write_update_result(
    path: Path, outcome: str, code: str | None, from_version: str | None, to_version: str,
    when: dt.datetime, backup_kept: bool, update_id: Any = None,
) -> None:
    """data/updates/update_result.json: read (then deleted) by the server at startup.

    ``update_id`` (``pending.id``, 16 lowercase hex) is added as ``"id"`` when valid."""
    result: dict[str, Any] = {
        "outcome": outcome,
        "code": code,
        "from_version": from_version,
        "to_version": to_version,
        "at": iso(when),
        "backup_kept": backup_kept,
    }
    if isinstance(update_id, str) and DEPS_ID_RE.fullmatch(update_id):  # same shape: 16 hex
        result["id"] = update_id
    write_json_atomic(path, result)


# ------------------------------------------------------------------------------- system


class System:
    """Everything with side effects. launch.pyw subclasses it for the Windows-only parts
    (mutex, focusing a window, the message box); tests replace it with a fake."""

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def utcnow(self) -> dt.datetime:
        return dt.datetime.now(dt.timezone.utc)

    def environ(self) -> Mapping[str, str]:
        return os.environ

    def port_free(self, port: int) -> bool:
        return port_is_free(port)

    def probe(self, port: int, secret: str | None) -> Health:
        return probe_health(port, secret)

    def python_exe(self, install_root: Path) -> Path:
        bundled = install_root / "python" / "pythonw.exe"
        if bundled.is_file():
            return bundled
        here = Path(sys.executable)
        windowless = here.with_name("pythonw.exe")
        return windowless if windowless.is_file() else here

    def spawn(self, python: Path, script: Path, cwd: Path, env: Mapping[str, str]) -> Any:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return subprocess.Popen(
            [str(python), str(script)],
            cwd=str(cwd), env=dict(env), creationflags=flags, close_fds=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def find_edge(self) -> Path | None:
        return find_edge(registry_edge_paths(), os.environ)

    def run_browser(self, args: list[str]) -> None:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(args, creationflags=flags, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def open_default(self, url: str) -> None:
        startfile = getattr(os, "startfile", None)
        if startfile is None:
            raise OSError("no default browser opener")
        startfile(url)

    def focus_window(self) -> bool:
        return False

    def show_error(self, text: str) -> None:
        pass

    def acquire(self, timeout: float) -> bool:
        return True

    def release(self) -> None:
        pass

    def rmtree(self, path: Path) -> None:
        shutil.rmtree(path, ignore_errors=True)

    # --- a server left running by an earlier launcher (Windows only; elsewhere: none)

    def process_image(self, pid: int) -> str | None:
        """Full path of the program running as ``pid``; None when no such process runs."""
        k32 = _kernel32()
        if k32 is None:
            return None
        handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            return _image_if_running(k32, handle)
        finally:
            k32.CloseHandle(handle)

    def terminate(self, pid: int, expected_image: Path, timeout: float = 10.0) -> bool:
        """Stop ``pid`` if it still runs ``expected_image`` (checked on the same handle that
        is terminated, so a reused pid is never hit). True once it is gone."""
        k32 = _kernel32()
        if k32 is None:
            return False
        import ctypes

        access = _PROCESS_QUERY_LIMITED_INFORMATION | _PROCESS_TERMINATE | _SYNCHRONIZE
        handle = k32.OpenProcess(access, False, pid)
        if not handle:
            return ctypes.get_last_error() == _ERROR_INVALID_PARAMETER  # already gone
        try:
            image = _image_if_running(k32, handle)
            if image is None:
                return True
            if not same_path(image, expected_image):
                return False
            k32.TerminateProcess(handle, 1)
            return k32.WaitForSingleObject(handle, int(timeout * 1000)) == 0
        finally:
            k32.CloseHandle(handle)


_PROCESS_TERMINATE = 0x0001
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000
_STILL_ACTIVE = 259
_ERROR_INVALID_PARAMETER = 87


def _kernel32() -> Any:
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.GetExitCodeProcess.restype = wintypes.BOOL
    k32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.TerminateProcess.restype = wintypes.BOOL
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.WaitForSingleObject.restype = wintypes.DWORD
    return k32


def _image_if_running(k32: Any, handle: Any) -> str | None:
    import ctypes
    from ctypes import wintypes

    code = wintypes.DWORD(0)
    if not k32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != _STILL_ACTIVE:
        return None
    size = wintypes.DWORD(32768)
    buf = ctypes.create_unicode_buffer(size.value)
    if not k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
        return None
    return buf.value


# ----------------------------------------------------------------------------- launcher


@dataclass
class Started:
    proc: Any
    port: int


@dataclass
class Options:
    ui: bool = True  # False (--no-ui): never open a window or a message box (tests/diagnostics)
    port_first: int = PORT_FIRST
    port_last: int = PORT_LAST
    exit_when_unused: int = EXIT_WHEN_UNUSED_SECONDS
    # FOR TESTING ONLY (release drills, ``--test-downloads-dir <folder>``): the folder the
    # server's updater scans instead of the user's Downloads (FINTRACK_DOWNLOADS_DIR). Never read
    # from the user's environment: server_env drops every FINTRACK_* variable it inherits.
    test_downloads_dir: str | None = None


@dataclass
class Launcher:
    paths: Paths
    system: System = field(default_factory=System)
    options: Options = field(default_factory=Options)
    contact: str = ""
    errors: list[str] = field(default_factory=list)

    # --- entry points

    def launch(self) -> int:
        """Open FinTrack: show the running one, or start it and supervise until it stops.

        Called with the launch mutex held; releases it once the window is up.
        """
        try:
            state = read_state(self.paths.state_file)
        except StateError as exc:
            log.error("%s: %s", FT_START_05, exc)
            return self.fail(FT_START_05)
        saved = read_saved_contact(self.paths)
        self.contact = saved if saved is not None else clean_contact(state.get("support_contact"))

        running = self.find_running(state)
        if running is not None:
            port, health = running
            if not health.web:
                return self.fail(FT_START_05)
            self.show_existing(port)
            self.system.release()
            return 0
        if not self.stop_unresponsive():
            return self.fail(FT_START_08)

        cleanup_partials(self.paths.versions)
        started = self.boot(state, open_window=True)
        self.system.release()
        if started is None:
            return 1
        return self.supervise(started)

    def supervise(self, started: Started) -> int:
        """Wait for the server; exit code 75 means "start again" (updates, restarts)."""
        while True:
            code = started.proc.wait()
            if code != EXIT_RESTART:
                if code:
                    log.warning("server exited with code %s", code)
                else:
                    log.info("server stopped")
                return 0 if code in (0, None) else 1
            log.info("server asked for a restart")
            if not self.system.acquire(MUTEX_WAIT_ON_RESTART):
                log.warning("another launcher kept the lock during a restart")
            try:
                try:
                    state = read_state(self.paths.state_file)
                except StateError as exc:
                    log.error("%s: %s", FT_START_05, exc)
                    return self.fail(FT_START_05)
                running = self.find_running(state)
                if running is not None:
                    log.info("another launcher already started FinTrack again")
                    return 0
                if not self.stop_unresponsive():
                    return self.fail(FT_START_08)
                cleanup_partials(self.paths.versions)
                next_started = self.boot(state, open_window=False, same_port=started.port)
            finally:
                self.system.release()
            if next_started is None:
                return 1
            started = next_started

    # --- finding a running server

    def find_running(self, state: Mapping[str, Any]) -> tuple[int, Health] | None:
        control = read_control(self.paths.control_file)
        ports: list[tuple[int, str | None]] = []
        if control is not None:
            ports.append(control)
        port = state.get("port")
        if isinstance(port, int) and (control is None or control[0] != port):
            ports.append((port, control[1] if control else None))
        for candidate, secret in ports:
            health = self.system.probe(candidate, secret)
            if health.ok:
                return candidate, health
            if health.status in ("foreign", "bad_proof"):
                log.warning("%s: port %d answers but it isn't this FinTrack", FT_START_07, candidate)
        return None

    def stop_unresponsive(self) -> bool:
        """No healthy answer, but the server control.json names may still be running (hung,
        or stuck stopping): stop it rather than start a second server on the same data.

        Only a process running this install's python\\pythonw.exe is touched (a pid Windows
        gave to another program is left alone). False when ours couldn't be stopped.
        """
        pid = read_control_pid(self.paths.control_file)
        if pid is None or pid == os.getpid():
            return True
        image = self.system.process_image(pid)
        if image is None:
            return True
        expected = self.paths.install_root / "python" / "pythonw.exe"
        if not same_path(image, expected):
            log.info("pid %d from control.json is another program now; leaving it alone", pid)
            return True
        log.warning("the last FinTrack server (pid %d) runs but doesn't answer; stopping it", pid)
        if self.system.terminate(pid, expected):
            return True
        log.error("%s: could not stop the unresponsive server (pid %d)", FT_START_08, pid)
        return False

    def show_existing(self, port: int) -> None:
        if not self.options.ui:
            log.info("FinTrack is already running on port %d", port)
            return
        if self.system.focus_window():
            log.info("focused the open FinTrack window")
            return
        self.open_window(port)

    # --- starting

    def boot(self, state: dict[str, Any], open_window: bool, same_port: int | None = None) -> Started | None:
        """Start what state.json says, applying pending update / rollback rules."""
        if state.get("rollback") is not None:
            state = self.apply_rollback(state)
            if state is None:  # the copy couldn't be put back: don't start anything
                return self.after_start(FT_UPD_06, open_window, same_port)
            started = self.start(state, ROLLBACK_GATE_TIMEOUT, None, same_port)
            return self.after_start(started, open_window, same_port)

        pending = state.get("pending")
        gating = isinstance(pending, dict) and not pending.get("gate_passed")
        if gating:
            if pending.get("snapshot") is None:
                snapped = self.snapshot_for(state)
                if snapped is None:  # no copy and nothing to go back to: don't risk the data
                    return self.after_start(FT_UPD_01, open_window, same_port)
                state = snapped
                if state.get("pending") is None:  # the copy failed: already went back
                    started = self.start(state, ROLLBACK_GATE_TIMEOUT, None, same_port)
                    return self.after_start(started, open_window, same_port)
                pending = state["pending"]
            started = self.start(state, UPDATE_GATE_TIMEOUT, pending["version"], same_port)
            if isinstance(started, Started):
                self.mark_gate_passed(state, pending["version"])
                return self.after_start(started, open_window, same_port)
            if started not in NEW_VERSION_FAULTS:
                # No free port or a squatter: not the new version's fault, keep it.
                return self.after_start(started, open_window, same_port)
            log.error("%s: the new version didn't start (%s)", FT_UPD_03, started)
            state = self.revert_update(state, FT_UPD_03)
            if state is None:
                return self.after_start(started, open_window, same_port)
            started = self.start(state, ROLLBACK_GATE_TIMEOUT, None, same_port)
            return self.after_start(started, open_window, same_port)

        started = self.start(state, START_TIMEOUT, None, same_port)
        return self.after_start(started, open_window, same_port)

    def mark_gate_passed(self, state: dict[str, Any], version: str) -> None:
        """The new version answered: record ``pending.gate_passed`` -- on a FRESH read of
        state.json. The server is already running and may have cleared ``pending`` (the user's
        first sign-in finishes the update) or asked for a rollback; writing the copy read
        before the start would bring a cleared ``pending`` back. ``state`` is updated in
        place to what is on disk."""
        try:
            fresh = read_state(self.paths.state_file)
        except StateError as exc:
            log.warning("state.json not re-read after the health gate (%s); gate_passed not saved", exc)
            return
        pending = fresh.get("pending")
        if (
            isinstance(pending, dict) and pending.get("version") == version
            and fresh.get("current") == version and fresh.get("rollback") is None
            and not pending.get("gate_passed")
        ):
            pending["gate_passed"] = True
            self.save_state(fresh)
        state.clear()
        state.update(fresh)

    def after_start(self, started: Started | str, open_window: bool, same_port: int | None) -> Started | None:
        if not isinstance(started, Started):
            self.fail(started)
            return None
        if open_window or (same_port is not None and started.port != same_port):
            # A restart on a different port: the old window can't follow, open a new one.
            self.open_window(started.port)
        return started

    def start(self, state: dict[str, Any], timeout: float, expect_version: str | None,
              same_port: int | None) -> Started | str:
        """Spawn versions/<current> and wait for a verified healthy answer.

        Returns Started, or the failure code (FT-START-01/03/04/05/07).
        """
        version = state["current"]
        problem = check_version_files(self.paths, version)
        if problem:
            log.error("%s: files for version %s are missing", problem, version)
            return problem
        preferred = same_port if same_port is not None else state.get("port")
        tried: list[int] = []
        for _attempt in range(2):
            port = choose_port(preferred, self.system.port_free, self.options.port_first,
                               self.options.port_last, skip=tried)
            if port is None:
                return FT_START_04
            tried.append(port)
            result = self.spawn_and_wait(state, version, port, timeout, expect_version)
            if result != FT_START_07:
                break
            preferred = None  # something squats on that port: try another one
        if isinstance(result, Started) and state.get("port") != result.port:
            state["port"] = result.port
            self.save_state(state)
        return result

    def spawn_and_wait(self, state: Mapping[str, Any], version: str, port: int, timeout: float,
                       expect_version: str | None) -> Started | str:
        vdir = self.paths.version_dir(version)
        secret = make_secret()
        env = server_env(self.system.environ(), self.paths, vdir, port, secret, self.contact,
                         self.options.exit_when_unused,
                         test_downloads_dir=self.options.test_downloads_dir)
        try:
            proc = self.system.spawn(self.system.python_exe(self.paths.install_root),
                                     vdir / "run_packaged.py", vdir, env)
        except OSError as exc:
            log.error("%s: could not start Python (%s)", FT_START_05, type(exc).__name__)
            return FT_START_05
        try:
            write_control(self.paths.control_file, port, secret, getattr(proc, "pid", None),
                          self.system.utcnow())
        except OSError as exc:
            log.warning("could not save control.json (%s)", type(exc).__name__)
        deadline = self.system.monotonic() + timeout
        while True:
            code = proc.poll()
            if code is not None:
                log.error("%s: the server exited during start (code %s)", FT_START_03, code)
                return FT_START_03
            health = self.system.probe(port, secret)
            if health.ok:
                if not health.web:
                    log.error("%s: the app's web files are missing", FT_START_05)
                    self.kill(proc)
                    return FT_START_05
                if expect_version is not None and health.version != expect_version:
                    log.error("%s: started version %s, expected %s", FT_START_01, health.version,
                              expect_version)
                    self.kill(proc)
                    return FT_START_01
                log.info("FinTrack %s is up on port %d", health.version, port)
                return Started(proc, port)
            if health.status in ("foreign", "bad_proof"):
                log.error("%s: something else answers on port %d", FT_START_07, port)
                self.kill(proc)
                return FT_START_07
            if self.system.monotonic() >= deadline:
                log.error("%s: no answer within %.0f s", FT_START_01, timeout)
                self.kill(proc)
                return FT_START_01
            self.system.sleep(POLL_INTERVAL)

    def kill(self, proc: Any) -> None:
        try:
            proc.kill()
            proc.wait(timeout=10)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not stop the server process (%s)", type(exc).__name__)

    # --- updates

    def snapshot_for(self, state: dict[str, Any]) -> dict[str, Any] | None:
        pending = state["pending"]
        old = pending.get("from") or state.get("previous") or "0.0.0"
        try:
            name = take_snapshot(self.paths.data, old, self.system.utcnow())
        except OSError as exc:
            log.error("%s: could not copy the data before the update (%s)", FT_UPD_01, type(exc).__name__)
            return self.revert_update(state, FT_UPD_01, backup_kept=False)
        pending["snapshot"] = name
        self.save_state(state)
        if name:
            prune_snapshots(self.paths.data, keep=name)
        log.info("saved a copy of the data before the update")
        return state

    def revert_update(self, state: dict[str, Any], code: str, backup_kept: bool | None = None,
                      outcome: str = "failed") -> dict[str, Any] | None:
        """Go back to ``previous`` after a failed update. None when there is nothing to go back to."""
        pending = state.get("pending") or {}
        new = pending.get("version") or state["current"]
        previous = state.get("previous")
        if not previous or previous == new or check_version_files(self.paths, previous):
            log.error("%s: no previous version to go back to", code)
            return None
        if backup_kept is None:
            backup_kept = bool(pending.get("snapshot"))
        state["current"] = previous
        state["previous"] = None
        state["pending"] = None
        state["rollback"] = None
        self.save_state(state)
        try:
            write_update_result(self.paths.update_result, outcome, code, previous, new,
                                self.system.utcnow(), backup_kept, pending.get("id"))
        except OSError as exc:
            log.warning("could not write update_result.json (%s)", type(exc).__name__)
        if pending and new != previous:
            # Only a version that was being installed is removed (never one that ran fine).
            self.system.rmtree(self.paths.version_dir(new))
        log.info("went back to version %s (%s)", previous, code)
        return state

    def apply_rollback(self, state: dict[str, Any]) -> dict[str, Any] | None:
        """The server asked to undo the update (its data update failed): restore the copy."""
        request = state["rollback"]
        pending = state.get("pending") or {}
        code = request.get("code") or FT_UPD_05
        to = request["to"]
        new = pending.get("version") or state["current"]
        snapshot = pending.get("snapshot") or ""
        kept = False
        if snapshot:
            failed_name = f"failed-update-{new}-{utc_stamp(self.system.utcnow())}-{secrets.token_hex(2)}"
            try:
                restore_snapshot(self.paths.data, snapshot, failed_name)
                kept = True
                log.info("restored the data copied before the update")
            except OSError as exc:
                # Never switch versions onto data that wasn't put back: state.json is left
                # as it is (the rollback stays pending for the next start) and the user sees
                # the "couldn't start" box.
                log.error("%s: could not restore the data copy (%s; rollback %s to %s stays pending)",
                          FT_UPD_06, type(exc).__name__, code, to)
                return None
        if state.get("previous") != to:
            state["previous"] = to
        reverted = self.revert_update(state, code, backup_kept=kept, outcome="rolled_back")
        if reverted is None:
            # Nothing to go back to: forget the request so the current version still starts.
            state["rollback"] = None
            self.save_state(state)
            return state
        return reverted

    # --- window / errors

    def open_window(self, port: int) -> bool:
        if not self.options.ui:
            log.info("window not opened (--no-ui): %s", app_url(port))
            return True
        url = app_url(port)
        edge = self.system.find_edge()
        if edge is not None:
            try:
                self.system.run_browser(edge_args(edge, url, self.paths.browser_profile))
                return True
            except OSError as exc:
                log.warning("could not start Edge (%s)", type(exc).__name__)
        try:
            self.system.open_default(url)
            return True
        except OSError as exc:
            log.error("%s: no browser could be opened (%s)", FT_START_06, type(exc).__name__)
        self.fail(FT_START_06)
        return False

    def fail(self, code: str) -> int:
        self.errors.append(code)
        log.error("couldn't start: %s", code)
        if self.options.ui:
            self.system.show_error(error_message(self.contact))
        return 1

    def save_state(self, state: Mapping[str, Any]) -> None:
        write_state(self.paths.state_file, state)

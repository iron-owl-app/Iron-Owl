"""launcher_core: pure functions and launcher flows (with a fake System; no Windows needed).

Run: python -m pytest packaging/launcher/tests -p no:cacheprovider
"""
from __future__ import annotations

import datetime as dt
import http.server
import json
import sys
import threading
from pathlib import Path

import pytest

LAUNCHER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAUNCHER_DIR))

import launcher_core as core  # noqa: E402

DEPS = "0123456789abcdef"
NOW = dt.datetime(2026, 9, 28, 12, 0, 0, tzinfo=dt.timezone.utc)


# ------------------------------------------------------------------------ fixtures


def make_version(paths: core.Paths, version: str, deps: str = DEPS, web: bool = True) -> Path:
    vdir = paths.versions / version
    (vdir / "backend" / "app").mkdir(parents=True)
    (vdir / "backend" / "app" / "__init__.py").write_text("", encoding="utf-8")
    (vdir / "run_packaged.py").write_text("# stub", encoding="utf-8")
    if web:
        (vdir / "web").mkdir()
        (vdir / "web" / "index.html").write_text("<!doctype html>", encoding="utf-8")
    (vdir / "release.json").write_text(json.dumps({
        "version": version, "schema_version": 5, "python": "3.11", "bootstrap_version": 1,
        "kind": "full", "deps_id": deps,
    }), encoding="utf-8")
    (paths.runtimes / deps / "site-packages").mkdir(parents=True, exist_ok=True)
    return vdir


def write_state(paths: core.Paths, **fields) -> dict:
    state = {"schema": 1, "bootstrap_version": 1, "current": "1.4.0", "previous": None,
             "pending": None, "rollback": None, "port": 8000, "support_contact": "Sam",
             "installed_at": "2026-09-01T10:00:00Z", "future_key": {"kept": True}}
    state.update(fields)
    paths.install_root.mkdir(parents=True, exist_ok=True)
    paths.state_file.write_text(json.dumps(state), encoding="utf-8")
    return state


def read_state(paths: core.Paths) -> dict:
    return json.loads(paths.state_file.read_text(encoding="utf-8"))


def put_vault(paths: core.Paths, db: bytes = b"DB-OLD", key: bytes = b"KEY-OLD") -> None:
    paths.data.mkdir(parents=True, exist_ok=True)
    (paths.data / "fintrack.db").write_bytes(db)
    (paths.data / "keyfile.json").write_bytes(key)


class FakeProc:
    def __init__(self, system: FakeSystem, version: str, port: int, secret: str, behavior: str,
                 exit_code: int | None) -> None:
        self.system = system
        self.version = version
        self.port = port
        self.secret = secret
        self.behavior = behavior
        self.exit_code = exit_code
        self.pid = 4000 + len(system.spawned)
        self.polls = 0
        self.killed = False
        self.exited: int | None = None

    def poll(self) -> int | None:
        self.polls += 1
        if self.killed:
            return -9
        if self.behavior == "crash":
            self.exited = 1
            return 1
        return None

    def wait(self, timeout: float | None = None) -> int | None:
        if self.killed:
            return -9
        self.exited = self.exit_code
        return self.exit_code

    def kill(self) -> None:
        self.killed = True

    def alive(self) -> bool:
        return not self.killed and self.exited is None


class FakeSystem(core.System):
    """behaviors[version] in: ok | crash | hang | noweb | wrong_version.
    exit_codes: what successive healthy servers return from wait() (default 0)."""

    def __init__(self) -> None:
        self.t = 0.0
        self.behaviors: dict[str, str] = {}
        self.exit_codes: list[int] = []
        self.busy_ports: set[int] = set()
        self.foreign_ports: set[int] = set()
        self.external: dict[int, tuple[str, core.Health]] = {}  # port -> (secret, health)
        self.spawned: list[FakeProc] = []
        self.envs: list[dict] = []
        self.browser_runs: list[list[str]] = []
        self.default_opens: list[str] = []
        self.errors_shown: list[str] = []
        self.window_exists = False
        self.focused = 0
        self.edge: Path | None = Path(r"C:\Edge\msedge.exe")
        self.owned = False
        self.acquires = 0
        self.releases = 0
        self.processes: dict[int, str] = {}  # pid -> image of other (already running) processes
        self.terminated: list[int] = []
        self.terminate_works = True

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds

    def utcnow(self) -> dt.datetime:
        return NOW + dt.timedelta(seconds=self.t)

    def environ(self):
        return {"PATH": r"C:\Windows", "PYTHONPATH": r"C:\evil", "data_dir": r"C:\elsewhere",
                "PLAID_SECRET": "leak", "SystemRoot": r"C:\Windows"}

    def port_free(self, port: int) -> bool:
        if port in self.busy_ports or port in self.foreign_ports or port in self.external:
            return False
        return not any(p.port == port and p.alive() for p in self.spawned)

    def probe(self, port: int, secret: str | None) -> core.Health:
        if port in self.foreign_ports:
            return core.Health("foreign")
        if port in self.external:
            ext_secret, health = self.external[port]
            return health if secret == ext_secret else core.Health("bad_proof", version=health.version)
        for proc in self.spawned:
            if proc.port != port or not proc.alive():
                continue
            if proc.behavior in ("crash", "hang"):
                return core.DOWN
            if proc.polls < 2:  # takes a moment to come up
                return core.DOWN
            if secret != proc.secret:
                return core.Health("bad_proof", version=proc.version)
            version = "9.9.9" if proc.behavior == "wrong_version" else proc.version
            return core.Health("ok", version=version, boot_id="ab" * 8,
                               web=proc.behavior != "noweb")
        return core.DOWN

    def python_exe(self, install_root: Path) -> Path:
        return install_root / "python" / "pythonw.exe"

    def spawn(self, python, script, cwd, env):
        version = Path(cwd).name
        behavior = self.behaviors.get(version, "ok")
        # Only a server that comes up healthy gets to return a scripted exit code.
        code = self.exit_codes.pop(0) if (behavior == "ok" and self.exit_codes) else 0
        proc = FakeProc(self, version, int(env["PORT"]), env["CONTROL_SECRET"], behavior, code)
        self.spawned.append(proc)
        self.envs.append(dict(env))
        return proc

    def find_edge(self):
        return self.edge

    def run_browser(self, args):
        self.browser_runs.append(list(args))

    def open_default(self, url):
        self.default_opens.append(url)

    def focus_window(self) -> bool:
        if self.window_exists:
            self.focused += 1
            return True
        return False

    def show_error(self, text: str) -> None:
        self.errors_shown.append(text)

    def acquire(self, timeout: float) -> bool:
        self.acquires += 1
        self.owned = True
        return True

    def release(self) -> None:
        self.releases += 1
        self.owned = False

    def process_image(self, pid: int) -> str | None:
        return self.processes.get(pid)

    def terminate(self, pid: int, expected_image: Path, timeout: float = 10.0) -> bool:
        if not core.same_path(self.processes.get(pid, ""), expected_image):
            return False
        if not self.terminate_works:
            return False
        self.terminated.append(pid)
        del self.processes[pid]
        return True


@pytest.fixture
def paths(tmp_path) -> core.Paths:
    return core.Paths(install_root=tmp_path / "install", home=tmp_path / "home")


@pytest.fixture
def system() -> FakeSystem:
    return FakeSystem()


def launcher(paths, system, **opts) -> core.Launcher:
    return core.Launcher(paths, system, core.Options(**opts))


# ------------------------------------------------------------------ pure functions


def test_error_message_names_the_contact_or_a_generic_helper():
    assert core.error_message("Sam") == (
        "Iron Owl couldn\u2019t start.\n\nRestart your computer and try again. "
        "If it still doesn\u2019t work, call Sam."
    )
    generic = core.error_message("")
    assert generic.endswith("ask the person who set up Iron Owl for help.")
    assert "FT-" not in generic and "server" not in generic.lower() and "port" not in generic.lower()


def test_clean_contact():
    assert core.clean_contact("  Sam\n\x07 ") == "Sam"
    assert core.clean_contact("x" * 100) == "x" * 60
    assert core.clean_contact(None) == ""
    assert core.clean_contact(42) == ""


def test_ports_prefer_the_saved_one_then_the_range():
    assert core.candidate_ports(8003)[:3] == [8003, 8000, 8001]
    assert core.candidate_ports(9999) == list(range(8000, 8011))
    assert core.candidate_ports(None, 8030, 8032) == [8030, 8031, 8032]
    busy = {8000, 8001}
    assert core.choose_port(8000, lambda p: p not in busy) == 8002
    assert core.choose_port(8005, lambda p: p not in busy) == 8005
    assert core.choose_port(8000, lambda p: False) is None
    assert core.choose_port(8000, lambda p: True, skip=[8000]) == 8001


def test_port_is_free_detects_a_listener():
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    try:
        assert core.port_is_free(sock.getsockname()[1]) is False
    finally:
        sock.close()


def test_find_edge_prefers_registry_then_program_files():
    env = {"ProgramFiles(x86)": r"C:\PF86", "ProgramFiles": r"C:\PF", "LOCALAPPDATA": r"C:\LA"}
    reg = ['"D:\\Apps\\Edge\\msedge.exe"', "C:\\not-edge.exe"]
    cands = core.edge_candidates(reg, env)
    assert cands[0] == Path(r"D:\Apps\Edge\msedge.exe")
    assert Path(r"C:\not-edge.exe") not in cands
    assert cands[1] == Path(r"C:\PF86") / core.EDGE_RELATIVE
    present = {str(Path(r"C:\PF") / core.EDGE_RELATIVE)}
    assert core.find_edge(reg, env, exists=lambda p: str(p) in present) == Path(r"C:\PF") / core.EDGE_RELATIVE
    assert core.find_edge([], {}, exists=lambda p: True) is None


def test_edge_args_use_app_mode_and_the_dedicated_profile():
    args = core.edge_args(Path(r"C:\Edge\msedge.exe"), "http://127.0.0.1:8001/", Path(r"C:\Home\browser"))
    assert args[0] == r"C:\Edge\msedge.exe"
    assert "--app=http://127.0.0.1:8001/" in args
    assert r"--user-data-dir=C:\Home\browser" in args
    for flag in ("--no-first-run", "--no-default-browser-check", "--window-size=1280,860",
                 "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
                 "--disable-backgrounding-occluded-windows", "--hide-crash-restore-bubble"):
        assert flag in args


def _health_body(secret: str | None, nonce: str, **extra) -> bytes:
    boot = extra.pop("boot_id", "0123456789abcdef")
    body = {"app": "fintrack", "version": "1.4.0", "boot_id": boot, "window_open": False, "web": True}
    if secret:
        body["proof"] = core.expected_proof(secret, nonce, boot)
    body.update(extra)
    return json.dumps(body).encode()


def test_evaluate_health_needs_a_valid_proof():
    secret, nonce = "s" * 43, "n" * 32
    ok = core.evaluate_health(200, _health_body(secret, nonce), secret, nonce)
    assert ok.ok and ok.version == "1.4.0" and ok.web and not ok.window_open
    assert core.evaluate_health(200, _health_body("x" * 43, nonce), secret, nonce).status == "bad_proof"
    assert core.evaluate_health(200, _health_body(None, nonce), secret, nonce).status == "bad_proof"
    assert core.evaluate_health(200, _health_body(secret, nonce), None, nonce).status == "bad_proof"
    assert core.evaluate_health(200, _health_body(secret, "m" * 32), secret, nonce).status == "bad_proof"
    assert core.evaluate_health(200, _health_body(secret, nonce, boot_id="../x"), secret, nonce).status == "bad_proof"
    assert core.evaluate_health(200, _health_body(secret, nonce, app="other"), secret, nonce).status == "foreign"
    assert core.evaluate_health(404, b"{}", secret, nonce).status == "foreign"
    assert core.evaluate_health(200, b"not json", secret, nonce).status == "foreign"
    assert core.evaluate_health(200, b"[1]", secret, nonce).status == "foreign"
    noweb = core.evaluate_health(200, _health_body(secret, nonce, web=False), secret, nonce)
    assert noweb.ok and noweb.web is False


def test_proof_formula_matches_the_backend():
    backend = LAUNCHER_DIR.parents[1] / "backend"
    sys.path.insert(0, str(backend))
    try:
        lifecycle = pytest.importorskip("app.lifecycle")
    finally:
        sys.path.remove(str(backend))
    assert lifecycle.health_proof("k" * 43, "abcDEF_-" * 3, "00ff" * 4) == core.expected_proof(
        "k" * 43, "abcDEF_-" * 3, "00ff" * 4)


class _HealthHandler(http.server.BaseHTTPRequestHandler):
    secret = "q" * 43

    def do_GET(self):  # noqa: N802
        nonce = self.headers.get(core.CHALLENGE_HEADER, "")
        assert self.headers.get("Host", "").startswith("127.0.0.1:")
        body = _health_body(self.secret, nonce)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_probe_health_over_http():
    server = http.server.HTTPServer(("127.0.0.1", 0), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        assert core.probe_health(port, "q" * 43).ok
        assert core.probe_health(port, "w" * 43).status == "bad_proof"
    finally:
        server.shutdown()
        server.server_close()
    assert core.probe_health(port, "q" * 43, timeout=0.5).status == "down"


def test_state_roundtrip_is_atomic_and_keeps_unknown_keys(paths):
    write_state(paths)
    state = core.read_state(paths.state_file)
    state["port"] = 8004
    core.write_state(paths.state_file, state)
    again = read_state(paths)
    assert again["port"] == 8004 and again["future_key"] == {"kept": True}
    assert [p.name for p in paths.install_root.iterdir()] == ["state.json"]  # no temp files left


@pytest.mark.parametrize("content", [None, "{not json", "[]", json.dumps({"current": "1.4"}),
                                     json.dumps({"current": "1.4.0", "pending": {"version": "x"}}),
                                     json.dumps({"current": "1.4.0", "rollback": {"to": "1.3.0", "code": "rm -rf"}}),
                                     json.dumps({"current": "1.4.0", "pending": {"version": "1.5.0", "snapshot": "../x"}})])
def test_bad_state_is_refused(paths, content):
    paths.install_root.mkdir(parents=True)
    if content is not None:
        paths.state_file.write_text(content, encoding="utf-8")
    with pytest.raises(core.StateError):
        core.read_state(paths.state_file)


def test_state_tolerates_short_pending_and_bad_port(paths):
    write_state(paths, pending="1.5.0", port="8000")
    state = core.read_state(paths.state_file)
    assert state["pending"] == {"version": "1.5.0"} and state["port"] is None
    assert core.pending_version(state) == "1.5.0"


def test_server_env_is_decided_by_the_launcher(paths):
    base = {"PATH": "p", "PythonPath": "evil", "PYTHONHOME": "evil", "data_dir": "elsewhere",
            "Plaid_Secret": "x", "FINTRACK_WEB": "old", "Debug": "1", "USERPROFILE": "u"}
    vdir = paths.versions / "1.4.0"
    env = core.server_env(base, paths, vdir, 8002, "s" * 43, " Sam\x00 ")
    for gone in ("PythonPath", "PYTHONHOME", "data_dir", "Plaid_Secret", "Debug"):
        assert gone not in env
    assert env["PATH"] == "p" and env["USERPROFILE"] == "u"
    assert env["FINTRACK_HOME"] == str(paths.home)
    assert env["FINTRACK_WEB"] == str(vdir / "web")
    assert env["FINTRACK_INSTALL_ROOT"] == str(paths.install_root)
    assert env["PORT"] == "8002" and env["HOST"] == "127.0.0.1"
    assert env["SUPPORT_CONTACT"] == "Sam"
    assert env["DATA_DIR"] == str(paths.data) == str(paths.home / "data")
    assert env["EXIT_WHEN_UNUSED_SECONDS"] == "180"
    assert env["CONTROL_SECRET"] == "s" * 43
    moved = core.server_env({"DATA_DIR": r"C:\elsewhere"}, paths, vdir, 8002, "s" * 43, "", 0)
    assert moved["DATA_DIR"] == str(paths.data) and moved["EXIT_WHEN_UNUSED_SECONDS"] == "0"


def test_self_exit_is_on_for_packaged_starts(paths, system):
    # The App-window release: every window posts /api/app/alive every 30 s, so the server
    # stops 3 minutes after the last window is gone (and the vault is locked).
    assert core.EXIT_WHEN_UNUSED_SECONDS == 180 and core.Options().exit_when_unused == 180
    make_version(paths, "1.4.0")
    write_state(paths)
    assert launcher(paths, system).launch() == 0
    assert system.envs[0]["EXIT_WHEN_UNUSED_SECONDS"] == "180"


def test_check_version_files(paths):
    make_version(paths, "1.4.0")
    assert core.check_version_files(paths, "1.4.0") is None
    assert core.check_version_files(paths, "1.5.0") == core.FT_START_05
    assert core.check_version_files(paths, "..") == core.FT_START_05
    make_version(paths, "1.5.0", web=False)
    assert core.check_version_files(paths, "1.5.0") == core.FT_START_05
    make_version(paths, "1.6.0", deps="fedcba9876543210")
    (paths.runtimes / "fedcba9876543210" / "site-packages").rmdir()
    assert core.check_version_files(paths, "1.6.0") == core.FT_START_05
    vdir = make_version(paths, "1.7.0")
    (vdir / "release.json").write_text(json.dumps({"version": "1.7.1", "deps_id": DEPS}), encoding="utf-8")
    assert core.check_version_files(paths, "1.7.0") == core.FT_START_05


def test_cleanup_partials_only_removes_partial_folders(paths):
    make_version(paths, "1.4.0")
    (paths.versions / ".partial-1.5.0-abcd" / "web").mkdir(parents=True)
    (paths.versions / ".partial-note").write_text("file, not a folder", encoding="utf-8")
    assert core.cleanup_partials(paths.versions) == [".partial-1.5.0-abcd"]
    assert sorted(p.name for p in paths.versions.iterdir()) == [".partial-note", "1.4.0"]
    assert core.cleanup_partials(paths.install_root / "missing") == []


def test_snapshot_copies_vault_files_and_prunes(paths):
    assert core.take_snapshot(paths.data, "1.4.0", NOW) == ""  # no vault yet: nothing to copy
    put_vault(paths)
    (paths.data / "keyfile.json.new").write_bytes(b"NEW")
    (paths.data / "unrelated.txt").write_text("x", encoding="utf-8")
    names = []
    for i in range(3):
        names.append(core.take_snapshot(paths.data, "1.4.0", NOW + dt.timedelta(minutes=i)))
    assert names[0] == "pre-update-1.4.0-20260928T120000Z"
    snap = paths.data / names[2]
    assert sorted(p.name for p in snap.iterdir()) == ["fintrack.db", "keyfile.json", "keyfile.json.new"]
    assert (snap / "fintrack.db").read_bytes() == b"DB-OLD"
    same = core.take_snapshot(paths.data, "1.4.0", NOW)
    assert same == "pre-update-1.4.0-20260928T120000Z-2"
    assert not list(paths.data.glob(".pre-update-tmp-*"))
    removed = core.prune_snapshots(paths.data, keep=names[0])
    left = sorted(p.name for p in paths.data.iterdir() if p.name.startswith("pre-update-"))
    assert names[0] in left and len(left) == core.SNAPSHOT_KEEP
    assert names[2] in left  # the newest other one
    assert len(removed) == 2


def test_restore_snapshot_moves_live_files_aside(paths):
    put_vault(paths)
    name = core.take_snapshot(paths.data, "1.4.0", NOW)
    put_vault(paths, db=b"DB-MIGRATED", key=b"KEY-NEW")
    (paths.data / "fintrack.db-wal").write_bytes(b"WAL")
    core.restore_snapshot(paths.data, name, "failed-update-1.5.0-x")
    assert (paths.data / "fintrack.db").read_bytes() == b"DB-OLD"
    assert (paths.data / "keyfile.json").read_bytes() == b"KEY-OLD"
    assert not (paths.data / "fintrack.db-wal").exists()
    failed = paths.data / "failed-update-1.5.0-x"
    assert (failed / "fintrack.db").read_bytes() == b"DB-MIGRATED"
    assert (failed / "fintrack.db-wal").read_bytes() == b"WAL"
    assert (paths.data / name / "fintrack.db").read_bytes() == b"DB-OLD"  # the copy is kept
    with pytest.raises(FileNotFoundError):
        core.restore_snapshot(paths.data, "pre-update-1.0.0-20200101T000000Z", "failed-2")


def _fail_on_call(monkeypatch, name: str, n: int, match=lambda *a: True):
    """Make core.<name> (or os.replace as core sees it) raise OSError on its n-th matching call."""
    real = core.os.replace if name == "os.replace" else getattr(core, name)
    calls = {"n": 0}

    def wrapper(*args, **kwargs):
        if match(*args):
            calls["n"] += 1
            if calls["n"] == n:
                raise OSError("disk trouble")
        return real(*args, **kwargs)

    if name == "os.replace":
        monkeypatch.setattr(core.os, "replace", wrapper)
    else:
        monkeypatch.setattr(core, name, wrapper)
    return calls


def _restore_setup(paths):
    put_vault(paths)
    (paths.data / "fintrack.db-wal").write_bytes(b"WAL-OLD")
    name = core.take_snapshot(paths.data, "1.4.0", NOW)
    put_vault(paths, db=b"DB-MIGRATED", key=b"KEY-NEW")
    (paths.data / "fintrack.db-wal").write_bytes(b"WAL-NEW")
    return name


def _live_vault(paths) -> dict[str, bytes]:
    return {name: (paths.data / name).read_bytes() for name in core.VAULT_FILES if (paths.data / name).is_file()}


LIVE_AFTER_MIGRATION = {"fintrack.db": b"DB-MIGRATED", "fintrack.db-wal": b"WAL-NEW", "keyfile.json": b"KEY-NEW"}


def _no_leftovers(paths, snapshot: str) -> None:
    names = sorted(p.name for p in paths.data.iterdir() if p.is_dir())
    assert names == [snapshot], names  # no staging folder, no failed-update folder
    assert (paths.data / snapshot / "fintrack.db").read_bytes() == b"DB-OLD"


@pytest.mark.parametrize("n", [1, 2, 3])
def test_restore_copy_failure_leaves_live_data_untouched(paths, monkeypatch, n):
    name = _restore_setup(paths)
    _fail_on_call(monkeypatch, "_copy_file_synced", n)
    with pytest.raises(OSError):
        core.restore_snapshot(paths.data, name, "failed-update-1.5.0-x")
    assert _live_vault(paths) == LIVE_AFTER_MIGRATION
    _no_leftovers(paths, name)


@pytest.mark.parametrize("n", [1, 2, 3])
def test_restore_failure_moving_live_files_out_moves_them_back(paths, monkeypatch, n):
    name = _restore_setup(paths)
    _fail_on_call(monkeypatch, "os.replace", n, match=lambda src, dest: "failed-update" in str(dest))
    with pytest.raises(OSError):
        core.restore_snapshot(paths.data, name, "failed-update-1.5.0-x")
    assert _live_vault(paths) == LIVE_AFTER_MIGRATION
    _no_leftovers(paths, name)


@pytest.mark.parametrize("n", [1, 2, 3])
def test_restore_failure_moving_the_copy_in_puts_live_files_back(paths, monkeypatch, n):
    name = _restore_setup(paths)
    _fail_on_call(monkeypatch, "os.replace", n, match=lambda src, dest: ".restore-tmp-" in str(src))
    with pytest.raises(OSError):
        core.restore_snapshot(paths.data, name, "failed-update-1.5.0-x")
    assert _live_vault(paths) == LIVE_AFTER_MIGRATION
    _no_leftovers(paths, name)


def test_restore_failure_creating_the_failed_folder(paths):
    name = _restore_setup(paths)
    (paths.data / "failed-update-1.5.0-x").write_text("in the way", encoding="utf-8")
    with pytest.raises(OSError):
        core.restore_snapshot(paths.data, name, "failed-update-1.5.0-x")
    assert _live_vault(paths) == LIVE_AFTER_MIGRATION
    assert not list(paths.data.glob(".restore-tmp-*"))
    assert (paths.data / "failed-update-1.5.0-x").read_text(encoding="utf-8") == "in the way"
    (paths.data / "failed-update-1.5.0-x").unlink()
    (paths.data / "failed-update-1.5.0-x").mkdir()  # an existing folder is never reused or removed
    with pytest.raises(OSError):
        core.restore_snapshot(paths.data, name, "failed-update-1.5.0-x")
    assert (paths.data / "failed-update-1.5.0-x").is_dir()
    assert _live_vault(paths) == LIVE_AFTER_MIGRATION
    assert not list(paths.data.glob(".restore-tmp-*"))


def test_restore_refuses_an_empty_snapshot(paths):
    put_vault(paths)
    empty = paths.data / "pre-update-1.4.0-20260928T120000Z"
    empty.mkdir()
    with pytest.raises(FileNotFoundError):
        core.restore_snapshot(paths.data, empty.name, "failed-2")
    assert _live_vault(paths) == {"fintrack.db": b"DB-OLD", "keyfile.json": b"KEY-OLD"}
    assert not (paths.data / "failed-2").exists()


def test_restore_removes_old_staging_leftovers(paths):
    name = _restore_setup(paths)
    (paths.data / ".restore-tmp-deadbeef").mkdir()
    core.restore_snapshot(paths.data, name, "failed-update-1.5.0-x")
    assert not list(paths.data.glob(".restore-tmp-*"))
    assert _live_vault(paths) == {"fintrack.db": b"DB-OLD", "fintrack.db-wal": b"WAL-OLD", "keyfile.json": b"KEY-OLD"}


def test_atomic_write_retries_a_locked_target(paths, monkeypatch):
    real = core.os.replace
    fails = {"left": 2}
    sleeps: list[float] = []

    def flaky(src, dest):
        if fails["left"]:
            fails["left"] -= 1
            raise PermissionError("locked by a scanner")
        return real(src, dest)

    monkeypatch.setattr(core.os, "replace", flaky)
    monkeypatch.setattr(core.time, "sleep", sleeps.append)
    core.write_json_atomic(paths.state_file, {"a": 1})
    assert json.loads(paths.state_file.read_text(encoding="utf-8")) == {"a": 1}
    assert len(sleeps) == 2

    def always(src, dest):
        raise PermissionError("locked")

    monkeypatch.setattr(core.os, "replace", always)
    sleeps.clear()
    with pytest.raises(PermissionError):
        core.write_json_atomic(paths.state_file, {"a": 2})
    assert len(sleeps) == core.REPLACE_RETRIES == 3
    assert [p.name for p in paths.install_root.iterdir()] == ["state.json"]  # temp file removed
    assert json.loads(paths.state_file.read_text(encoding="utf-8")) == {"a": 1}

    def other(src, dest):
        raise OSError("not a lock")

    monkeypatch.setattr(core.os, "replace", other)
    sleeps.clear()
    with pytest.raises(OSError):
        core.write_json_atomic(paths.state_file, {"a": 3})
    assert sleeps == []  # only PermissionError is retried


def test_control_file_roundtrip(paths):
    core.write_control(paths.control_file, 8001, "s" * 43, 99, NOW)
    assert core.read_control(paths.control_file) == (8001, "s" * 43)
    paths.control_file.write_text(json.dumps({"port": 8001, "secret": "short"}), encoding="utf-8")
    assert core.read_control(paths.control_file) is None
    paths.control_file.write_text("garbage", encoding="utf-8")
    assert core.read_control(paths.control_file) is None
    assert core.read_control_pid(paths.control_file) is None
    core.write_control(paths.control_file, 8001, "s" * 43, 99, NOW)
    assert core.read_control_pid(paths.control_file) == 99
    for bad in (None, True, -1, 0, "99", 2**40):
        paths.control_file.write_text(json.dumps({"port": 8001, "pid": bad}), encoding="utf-8")
        assert core.read_control_pid(paths.control_file) is None


# ------------------------------------------------------------------------- flows


def test_cold_start_spawns_waits_opens_window_and_supervises(paths, system):
    make_version(paths, "1.4.0")
    write_state(paths)
    (paths.versions / ".partial-1.5.0-dead").mkdir()
    code = launcher(paths, system).launch()
    assert code == 0
    assert len(system.spawned) == 1
    proc = system.spawned[0]
    assert proc.version == "1.4.0" and proc.port == 8000
    env = system.envs[0]
    assert env["SUPPORT_CONTACT"] == "Sam" and "PYTHONPATH" not in env and "PLAID_SECRET" not in env
    assert core.read_control(paths.control_file) == (8000, env["CONTROL_SECRET"])
    assert len(system.browser_runs) == 1
    assert "--app=http://127.0.0.1:8000/" in system.browser_runs[0]
    assert f"--user-data-dir={paths.browser_profile}" in system.browser_runs[0]
    assert not (paths.versions / ".partial-1.5.0-dead").exists()
    assert system.errors_shown == [] and system.releases >= 1 and not system.owned


def test_running_server_is_shown_not_started_again(paths, system):
    make_version(paths, "1.4.0")
    write_state(paths)
    core.write_control(paths.control_file, 8000, "c" * 43, 1, NOW)
    system.external[8000] = ("c" * 43, core.Health("ok", version="1.4.0", boot_id="ab" * 8, web=True))
    assert launcher(paths, system).launch() == 0
    assert system.spawned == [] and len(system.browser_runs) == 1
    system.window_exists = True
    assert launcher(paths, system).launch() == 0
    assert system.focused == 1 and len(system.browser_runs) == 1


def test_running_server_without_web_files_is_an_error(paths, system):
    make_version(paths, "1.4.0")
    write_state(paths)
    core.write_control(paths.control_file, 8000, "c" * 43, 1, NOW)
    system.external[8000] = ("c" * 43, core.Health("ok", version="1.4.0", boot_id="ab" * 8, web=False))
    lau = launcher(paths, system)
    assert lau.launch() == 1
    assert lau.errors == [core.FT_START_05] and system.errors_shown == [core.error_message("Sam")]


def test_unverified_answer_on_saved_port_moves_to_another_port(paths, system):
    make_version(paths, "1.4.0")
    write_state(paths)
    system.external[8000] = ("someone-else" * 4, core.Health("ok", version="1.4.0", boot_id="ab" * 8, web=True))
    assert launcher(paths, system).launch() == 0
    assert system.spawned[0].port == 8001
    assert read_state(paths)["port"] == 8001
    assert "--app=http://127.0.0.1:8001/" in system.browser_runs[0]


def test_busy_ports_and_none_free(paths, system):
    make_version(paths, "1.4.0")
    write_state(paths, port=8003)
    system.busy_ports = {8003, 8000}
    assert launcher(paths, system).launch() == 0
    assert system.spawned[0].port == 8001

    system2 = FakeSystem()
    system2.busy_ports = set(range(8000, 8011))
    lau = launcher(paths, system2)
    assert lau.launch() == 1
    assert lau.errors == [core.FT_START_04] and len(system2.errors_shown) == 1
    assert system2.spawned == []


def test_squatter_during_start_retries_on_the_next_port(paths, system):
    make_version(paths, "1.4.0")
    write_state(paths)

    real_probe = system.probe
    calls = {"n": 0}

    def probe(port, secret):
        if port == 8000:
            calls["n"] += 1
            return core.Health("foreign")
        return real_probe(port, secret)

    system.probe = probe
    system.foreign_ports = set()
    lau = launcher(paths, system)
    assert lau.launch() == 0
    assert [p.port for p in system.spawned] == [8000, 8001]
    assert system.spawned[0].killed
    assert read_state(paths)["port"] == 8001


def test_crash_during_start_shows_the_message_box(paths, system):
    make_version(paths, "1.4.0")
    write_state(paths, support_contact="")
    system.behaviors["1.4.0"] = "crash"
    lau = launcher(paths, system)
    assert lau.launch() == 1
    assert lau.errors == [core.FT_START_03]
    assert system.errors_shown == [core.error_message("")]
    assert system.browser_runs == []


def test_no_answer_within_45_seconds(paths, system):
    make_version(paths, "1.4.0")
    write_state(paths)
    system.behaviors["1.4.0"] = "hang"
    lau = launcher(paths, system)
    assert lau.launch() == 1
    assert lau.errors == [core.FT_START_01]
    assert system.spawned[0].killed
    assert 45.0 <= system.t < 46.0


def test_missing_state_or_files(paths, system):
    lau = launcher(paths, system)
    assert lau.launch() == 1 and lau.errors == [core.FT_START_05]
    write_state(paths)
    lau = launcher(paths, system)
    assert lau.launch() == 1 and lau.errors == [core.FT_START_05]
    assert system.spawned == []


def test_no_ui_mode_never_shows_anything(paths, system):
    make_version(paths, "1.4.0")
    write_state(paths)
    system.behaviors["1.4.0"] = "crash"
    assert launcher(paths, system, ui=False).launch() == 1
    assert system.errors_shown == [] and system.browser_runs == []


def test_no_edge_falls_back_to_default_browser_then_fails(paths, system):
    make_version(paths, "1.4.0")
    write_state(paths)
    system.edge = None
    assert launcher(paths, system).launch() == 0
    assert system.default_opens == ["http://127.0.0.1:8000/"]

    s2 = FakeSystem()
    s2.edge = None

    def no_browser(url):
        raise OSError("none")

    s2.open_default = no_browser
    lau = launcher(paths, s2)
    lau.launch()
    assert core.FT_START_06 in lau.errors


def test_exit_75_restarts_on_the_same_port_without_a_new_window(paths, system):
    make_version(paths, "1.4.0")
    write_state(paths)
    system.exit_codes = [75, 0]
    assert launcher(paths, system).launch() == 0
    assert [(p.version, p.port) for p in system.spawned] == [("1.4.0", 8000), ("1.4.0", 8000)]
    assert len(system.browser_runs) == 1
    assert system.spawned[0].secret != system.spawned[1].secret
    assert core.read_control(paths.control_file)[1] == system.spawned[1].secret


def _install_update(paths: core.Paths, new: str = "1.5.0", old: str = "1.4.0") -> None:
    """What the updater does before exiting 75."""
    make_version(paths, new)
    state = read_state(paths)
    state.update(current=new, previous=old, pending={"version": new, "from": old, "at": "2026-09-28T11:59:00Z"})
    paths.state_file.write_text(json.dumps(state), encoding="utf-8")


def test_update_passes_the_health_gate(paths, system):
    make_version(paths, "1.4.0")
    write_state(paths)
    put_vault(paths)
    system.exit_codes = [75, 0]
    lau = launcher(paths, system)

    original_wait = FakeProc.wait

    def wait_and_install(self, timeout=None):
        code = original_wait(self, timeout)
        if code == 75:
            _install_update(paths)
            (paths.versions / ".partial-1.6.0-x").mkdir()
        return code

    FakeProc.wait = wait_and_install
    try:
        assert lau.launch() == 0
    finally:
        FakeProc.wait = original_wait
    assert [(p.version, p.port) for p in system.spawned] == [("1.4.0", 8000), ("1.5.0", 8000)]
    state = read_state(paths)
    assert state["current"] == "1.5.0" and state["previous"] == "1.4.0"
    snap = state["pending"]["snapshot"]
    assert snap == "pre-update-1.4.0-20260928T120000Z"
    assert state["pending"]["gate_passed"] is True
    assert (paths.data / snap / "fintrack.db").read_bytes() == b"DB-OLD"
    assert not (paths.versions / ".partial-1.6.0-x").exists()
    assert len(system.browser_runs) == 1 and system.errors_shown == []
    assert state["future_key"] == {"kept": True}
    assert not paths.update_result.exists()


@pytest.mark.parametrize("server_wrote", ["finished", "rollback"])
def test_gate_passed_never_brings_back_what_the_server_wrote(paths, system, server_wrote):
    """The new version is already running when the health gate passes: the user's first sign-in may
    have cleared ``pending`` (or the server asked for a rollback). gate_passed is written on
    a fresh read of state.json, never over that."""
    make_version(paths, "1.4.0")
    make_version(paths, "1.5.0")
    write_state(paths, current="1.5.0", previous="1.4.0",
                pending={"version": "1.5.0", "from": "1.4.0", "at": "2026-09-28T11:59:00Z", "snapshot": ""})
    put_vault(paths)
    lau = launcher(paths, system)
    real_start = lau.start

    def start_then_server_writes(state, timeout, expect_version, same_port):
        started = real_start(state, timeout, expect_version, same_port)
        on_disk = read_state(paths)
        if server_wrote == "finished":
            on_disk["pending"] = None
        else:
            on_disk["rollback"] = {"to": "1.4.0", "code": "FT-UPD-05"}
        paths.state_file.write_text(json.dumps(on_disk), encoding="utf-8")
        return started

    lau.start = start_then_server_writes
    state = core.read_state(paths.state_file)
    assert isinstance(lau.boot(state, open_window=False), core.Started)
    after = read_state(paths)
    if server_wrote == "finished":
        assert after["pending"] is None and after["current"] == "1.5.0"
    else:
        assert after["rollback"] == {"to": "1.4.0", "code": "FT-UPD-05"}
        assert "gate_passed" not in after["pending"]
    assert after["future_key"] == {"kept": True}


def test_test_downloads_dir_only_from_the_launcher_option(paths):
    vdir = paths.versions / "1.4.0"
    base = {"FINTRACK_DOWNLOADS_DIR": r"C:\from-the-user-environment", "PATH": "p"}
    env = core.server_env(base, paths, vdir, 8002, "s" * 43, "")
    assert "FINTRACK_DOWNLOADS_DIR" not in env  # the user's environment never sets it
    env = core.server_env(base, paths, vdir, 8002, "s" * 43, "", test_downloads_dir=r"C:\drill")
    assert env["FINTRACK_DOWNLOADS_DIR"] == r"C:\drill"
    assert core.Options().test_downloads_dir is None


def _run_update(paths, system, new_behavior: str, exit_codes=(75, 0)):
    make_version(paths, "1.4.0")
    write_state(paths)
    put_vault(paths)
    system.exit_codes = list(exit_codes)
    system.behaviors["1.5.0"] = new_behavior
    original_wait = FakeProc.wait

    def wait_and_install(self, timeout=None):
        code = original_wait(self, timeout)
        if code == 75 and self.version == "1.4.0":
            _install_update(paths)
        return code

    FakeProc.wait = wait_and_install
    try:
        lau = launcher(paths, system)
        return lau, lau.launch()
    finally:
        FakeProc.wait = original_wait


@pytest.mark.parametrize("behavior", ["hang", "crash", "wrong_version", "noweb"])
def test_failed_update_goes_back_to_the_previous_version(paths, system, behavior):
    lau, code = _run_update(paths, system, behavior)
    assert code == 0
    versions = [(p.version, p.port) for p in system.spawned]
    assert versions == [("1.4.0", 8000), ("1.5.0", 8000), ("1.4.0", 8000)]
    if behavior == "hang":
        assert system.t >= core.UPDATE_GATE_TIMEOUT
    state = read_state(paths)
    assert state["current"] == "1.4.0" and state["pending"] is None and state["previous"] is None
    assert not (paths.versions / "1.5.0").exists()
    result = json.loads(paths.update_result.read_text(encoding="utf-8"))
    assert result == {"outcome": "failed", "code": core.FT_UPD_03, "from_version": "1.4.0",
                      "to_version": "1.5.0", "at": result["at"], "backup_kept": True}
    assert system.errors_shown == [] and len(system.browser_runs) == 1
    assert (paths.data / "fintrack.db").read_bytes() == b"DB-OLD"


@pytest.mark.parametrize("update_id, written", [("0123456789abcdef", True), ("../../x", False), (7, False)])
def test_failed_update_result_names_the_update_id(paths, system, update_id, written):
    """The server records the id as failed, so the same file is never offered again."""
    make_version(paths, "1.4.0")
    make_version(paths, "1.5.0")
    put_vault(paths)
    write_state(paths, current="1.5.0", previous="1.4.0",
                pending={"version": "1.5.0", "from": "1.4.0", "id": update_id})
    system.behaviors["1.5.0"] = "crash"
    assert launcher(paths, system).launch() == 0
    result = json.loads(paths.update_result.read_text(encoding="utf-8"))
    assert result["code"] == core.FT_UPD_03 and result["outcome"] == "failed"
    assert result.get("id") == (update_id if written else None)


def test_failed_update_and_failed_previous_shows_the_message_box(paths, system):
    make_version(paths, "1.4.0")
    make_version(paths, "1.5.0")
    write_state(paths, current="1.5.0", previous="1.4.0", pending={"version": "1.5.0", "from": "1.4.0"})
    system.behaviors = {"1.5.0": "crash", "1.4.0": "crash"}
    lau = launcher(paths, system)
    assert lau.launch() == 1
    assert lau.errors == [core.FT_START_03] and len(system.errors_shown) == 1
    assert [p.version for p in system.spawned] == ["1.5.0", "1.4.0"]
    assert read_state(paths)["current"] == "1.4.0"


def test_rollback_request_restores_the_copy(paths, system):
    make_version(paths, "1.4.0")
    make_version(paths, "1.5.0")
    put_vault(paths)
    snap = core.take_snapshot(paths.data, "1.4.0", NOW - dt.timedelta(hours=1))
    put_vault(paths, db=b"DB-HALF-MIGRATED", key=b"KEY-OLD")
    write_state(paths, current="1.5.0", previous="1.4.0",
                pending={"version": "1.5.0", "from": "1.4.0", "snapshot": snap, "gate_passed": True},
                rollback={"to": "1.4.0", "code": "FT-UPD-05"})
    assert launcher(paths, system).launch() == 0
    assert [p.version for p in system.spawned] == ["1.4.0"]
    assert (paths.data / "fintrack.db").read_bytes() == b"DB-OLD"
    failed = [p for p in paths.data.iterdir() if p.name.startswith("failed-update-1.5.0-")]
    assert len(failed) == 1 and (failed[0] / "fintrack.db").read_bytes() == b"DB-HALF-MIGRATED"
    state = read_state(paths)
    assert state["current"] == "1.4.0" and state["pending"] is None and state["rollback"] is None
    assert not (paths.versions / "1.5.0").exists()
    result = json.loads(paths.update_result.read_text(encoding="utf-8"))
    assert result["outcome"] == "rolled_back" and result["code"] == "FT-UPD-05"
    assert result["from_version"] == "1.4.0" and result["to_version"] == "1.5.0"
    assert result["backup_kept"] is True


def test_rollback_via_exit_75_keeps_the_window(paths, system):
    make_version(paths, "1.4.0")
    make_version(paths, "1.5.0")
    put_vault(paths)
    write_state(paths, current="1.5.0", previous="1.4.0",
                pending={"version": "1.5.0", "from": "1.4.0"})
    system.exit_codes = [75, 0]
    original_wait = FakeProc.wait

    def wait_and_request(self, timeout=None):
        code = original_wait(self, timeout)
        if code == 75:
            state = read_state(paths)
            (paths.data / "fintrack.db").write_bytes(b"DB-BROKEN")
            state["rollback"] = {"to": "1.4.0", "code": "FT-UPD-05"}
            paths.state_file.write_text(json.dumps(state), encoding="utf-8")
        return code

    FakeProc.wait = wait_and_request
    try:
        assert launcher(paths, system).launch() == 0
    finally:
        FakeProc.wait = original_wait
    assert [(p.version, p.port) for p in system.spawned] == [("1.5.0", 8000), ("1.4.0", 8000)]
    assert (paths.data / "fintrack.db").read_bytes() == b"DB-OLD"
    assert len(system.browser_runs) == 1


def test_gate_already_passed_is_not_gated_again(paths, system):
    make_version(paths, "1.4.0")
    make_version(paths, "1.5.0")
    write_state(paths, current="1.5.0", previous="1.4.0",
                pending={"version": "1.5.0", "from": "1.4.0", "snapshot": "", "gate_passed": True})
    system.behaviors["1.5.0"] = "hang"
    lau = launcher(paths, system)
    assert lau.launch() == 1
    assert lau.errors == [core.FT_START_01]
    assert read_state(paths)["current"] == "1.5.0"  # a slow start is not a failed update
    assert system.t < core.UPDATE_GATE_TIMEOUT


def test_cold_start_with_pending_update_snapshots_first(paths, system):
    make_version(paths, "1.4.0")
    make_version(paths, "1.5.0")
    put_vault(paths)
    write_state(paths, current="1.5.0", previous="1.4.0", pending={"version": "1.5.0", "from": "1.4.0"})
    assert launcher(paths, system).launch() == 0
    state = read_state(paths)
    assert state["pending"]["snapshot"].startswith("pre-update-1.4.0-")
    assert state["pending"]["gate_passed"] is True
    assert len(system.browser_runs) == 1


def test_snapshot_failure_goes_back_without_starting_the_update(paths, system, monkeypatch):
    make_version(paths, "1.4.0")
    make_version(paths, "1.5.0")
    put_vault(paths)
    write_state(paths, current="1.5.0", previous="1.4.0", pending={"version": "1.5.0", "from": "1.4.0"})

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(core, "take_snapshot", broken)
    assert launcher(paths, system).launch() == 0
    assert [p.version for p in system.spawned] == ["1.4.0"]
    result = json.loads(paths.update_result.read_text(encoding="utf-8"))
    assert result["code"] == core.FT_UPD_01 and result["backup_kept"] is False


def test_snapshot_failure_without_previous_does_not_start(paths, system, monkeypatch):
    make_version(paths, "1.5.0")
    put_vault(paths)
    write_state(paths, current="1.5.0", previous=None, pending={"version": "1.5.0"})
    monkeypatch.setattr(core, "take_snapshot", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    lau = launcher(paths, system)
    assert lau.launch() == 1
    assert system.spawned == [] and lau.errors == [core.FT_UPD_01]


def test_restart_when_another_launcher_already_started_it(paths, system):
    make_version(paths, "1.4.0")
    write_state(paths)
    system.exit_codes = [75]
    original_wait = FakeProc.wait

    def wait_then_other_launcher(self, timeout=None):
        code = original_wait(self, timeout)
        core.write_control(paths.control_file, 8000, "o" * 43, 7, NOW)
        system.external[8000] = ("o" * 43, core.Health("ok", version="1.4.0", boot_id="cd" * 8, web=True))
        return code

    FakeProc.wait = wait_then_other_launcher
    try:
        assert launcher(paths, system).launch() == 0
    finally:
        FakeProc.wait = original_wait
    assert len(system.spawned) == 1


def test_restart_port_taken_opens_a_new_window(paths, system):
    make_version(paths, "1.4.0")
    write_state(paths)
    system.exit_codes = [75, 0]
    original_wait = FakeProc.wait

    def wait_then_busy(self, timeout=None):
        code = original_wait(self, timeout)
        if code == 75:
            system.busy_ports.add(8000)
        return code

    FakeProc.wait = wait_then_busy
    try:
        assert launcher(paths, system).launch() == 0
    finally:
        FakeProc.wait = original_wait
    assert [p.port for p in system.spawned] == [8000, 8001]
    assert len(system.browser_runs) == 2
    assert "--app=http://127.0.0.1:8001/" in system.browser_runs[1]


def test_server_crash_after_start_ends_quietly(paths, system):
    make_version(paths, "1.4.0")
    write_state(paths)
    system.exit_codes = [3]
    assert launcher(paths, system).launch() == 1
    assert system.errors_shown == []


def test_rollback_without_pending_switches_but_keeps_the_version(paths, system):
    make_version(paths, "1.4.0")
    make_version(paths, "1.5.0")
    put_vault(paths, db=b"DB-LIVE")
    write_state(paths, current="1.5.0", previous="1.4.0", pending=None,
                rollback={"to": "1.4.0", "code": "FT-UPD-05"})
    assert launcher(paths, system).launch() == 0
    assert [p.version for p in system.spawned] == ["1.4.0"]
    assert (paths.versions / "1.5.0").is_dir()
    assert (paths.data / "fintrack.db").read_bytes() == b"DB-LIVE"  # no copy to restore: untouched
    result = json.loads(paths.update_result.read_text(encoding="utf-8"))
    assert result["outcome"] == "rolled_back" and result["backup_kept"] is False


# ---------------------------------------------------- rollback restore failures (M2)


def _rollback_state(paths, snapshot):
    make_version(paths, "1.4.0")
    make_version(paths, "1.5.0")
    write_state(paths, current="1.5.0", previous="1.4.0",
                pending={"version": "1.5.0", "from": "1.4.0", "snapshot": snapshot, "gate_passed": True},
                rollback={"to": "1.4.0", "code": "FT-UPD-05"})
    return read_state(paths)


def _assert_rollback_stays_pending(paths, system, lau, before):
    assert lau.errors == [core.FT_UPD_06]
    assert system.errors_shown == [core.error_message("Sam")]
    assert system.spawned == [] and system.browser_runs == []
    assert read_state(paths) == before  # nothing switched; the request is still there
    assert (paths.versions / "1.5.0").is_dir()
    assert not paths.update_result.exists()
    assert _live_vault(paths) == LIVE_AFTER_MIGRATION
    assert not list(paths.data.glob(".restore-tmp-*"))
    assert not [p for p in paths.data.iterdir() if p.name.startswith("failed-update-")]


@pytest.mark.parametrize("step", ["copy", "move_out", "move_in"])
def test_rollback_restore_failure_keeps_the_request_and_starts_nothing(paths, system, monkeypatch, step):
    name = _restore_setup(paths)
    before = _rollback_state(paths, name)
    if step == "copy":
        _fail_on_call(monkeypatch, "_copy_file_synced", 2)
    elif step == "move_out":
        _fail_on_call(monkeypatch, "os.replace", 2, match=lambda src, dest: "failed-update" in str(dest))
    else:
        _fail_on_call(monkeypatch, "os.replace", 2, match=lambda src, dest: ".restore-tmp-" in str(src))
    lau = launcher(paths, system)
    assert lau.launch() == 1
    _assert_rollback_stays_pending(paths, system, lau, before)

    # Next start, with the disk fine again: the pending rollback completes.
    monkeypatch.undo()
    system2 = FakeSystem()
    assert launcher(paths, system2).launch() == 0
    assert [p.version for p in system2.spawned] == ["1.4.0"]
    assert (paths.data / "fintrack.db").read_bytes() == b"DB-OLD"
    state = read_state(paths)
    assert state["current"] == "1.4.0" and state["rollback"] is None


def test_rollback_with_a_missing_snapshot_folder_starts_nothing(paths, system):
    put_vault(paths, db=b"DB-MIGRATED", key=b"KEY-NEW")
    (paths.data / "fintrack.db-wal").write_bytes(b"WAL-NEW")
    before = _rollback_state(paths, "pre-update-1.4.0-20260928T110000Z")
    lau = launcher(paths, system)
    assert lau.launch() == 1
    _assert_rollback_stays_pending(paths, system, lau, before)


def test_rollback_restore_failure_during_a_restart_ends_with_the_message_box(paths, system, monkeypatch):
    name = _restore_setup(paths)
    make_version(paths, "1.4.0")
    make_version(paths, "1.5.0")
    write_state(paths, current="1.5.0", previous="1.4.0",
                pending={"version": "1.5.0", "from": "1.4.0", "snapshot": name, "gate_passed": True})
    system.exit_codes = [75]
    original_wait = FakeProc.wait

    def wait_and_request(self, timeout=None):
        code = original_wait(self, timeout)
        if code == 75:
            state = read_state(paths)
            state["rollback"] = {"to": "1.4.0", "code": "FT-UPD-05"}
            paths.state_file.write_text(json.dumps(state), encoding="utf-8")
            _fail_on_call(monkeypatch, "_copy_file_synced", 1)
        return code

    FakeProc.wait = wait_and_request
    try:
        lau = launcher(paths, system)
        assert lau.launch() == 1
    finally:
        FakeProc.wait = original_wait
    assert [p.version for p in system.spawned] == ["1.5.0"]
    assert lau.errors == [core.FT_UPD_06] and len(system.errors_shown) == 1
    state = read_state(paths)
    assert state["current"] == "1.5.0" and state["rollback"] == {"to": "1.4.0", "code": "FT-UPD-05"}
    assert _live_vault(paths) == LIVE_AFTER_MIGRATION


# ------------------------------------------------- a hung server from an earlier launch


def _hung_server(paths, system, image=None, pid=5150):
    make_version(paths, "1.4.0")
    write_state(paths)
    core.write_control(paths.control_file, 8000, "h" * 43, pid, NOW)
    system.processes[pid] = str(image or paths.install_root / "python" / "pythonw.exe")


def test_hung_server_of_this_install_is_stopped_before_starting(paths, system):
    _hung_server(paths, system)
    assert launcher(paths, system).launch() == 0
    assert system.terminated == [5150]
    assert len(system.spawned) == 1 and system.errors_shown == []


def test_same_pid_running_another_program_is_left_alone(paths, system):
    _hung_server(paths, system, image=r"C:\Windows\System32\notepad.exe")
    assert launcher(paths, system).launch() == 0
    assert system.terminated == [] and 5150 in system.processes
    assert len(system.spawned) == 1


def test_python_from_elsewhere_is_left_alone(paths, system):
    _hung_server(paths, system, image=paths.install_root.parent / "other" / "python" / "pythonw.exe")
    assert launcher(paths, system).launch() == 0
    assert system.terminated == [] and len(system.spawned) == 1


def test_dead_pid_needs_nothing(paths, system):
    _hung_server(paths, system)
    system.processes.clear()
    assert launcher(paths, system).launch() == 0
    assert system.terminated == [] and len(system.spawned) == 1


def test_hung_server_that_cannot_be_stopped_is_an_error_not_a_second_server(paths, system):
    _hung_server(paths, system)
    system.terminate_works = False
    lau = launcher(paths, system)
    assert lau.launch() == 1
    assert lau.errors == [core.FT_START_08] and len(system.errors_shown) == 1
    assert system.spawned == []


def test_healthy_server_is_never_stopped(paths, system):
    make_version(paths, "1.4.0")
    write_state(paths)
    core.write_control(paths.control_file, 8000, "c" * 43, 5150, NOW)
    system.processes[5150] = str(paths.install_root / "python" / "pythonw.exe")
    system.external[8000] = ("c" * 43, core.Health("ok", version="1.4.0", boot_id="ab" * 8, web=True))
    assert launcher(paths, system).launch() == 0
    assert system.terminated == [] and system.spawned == []


def test_hung_server_is_stopped_on_a_restart_too(paths, system):
    make_version(paths, "1.4.0")
    write_state(paths)
    system.exit_codes = [75, 0]
    original_wait = FakeProc.wait

    def wait_then_hang(self, timeout=None):
        code = original_wait(self, timeout)
        if code == 75:  # it asked for a restart but its process is still there, silent
            system.processes[self.pid] = str(paths.install_root / "python" / "pythonw.exe")
        return code

    FakeProc.wait = wait_then_hang
    try:
        assert launcher(paths, system).launch() == 0
    finally:
        FakeProc.wait = original_wait
    assert system.terminated == [system.spawned[0].pid]
    assert len(system.spawned) == 2


def test_real_system_process_checks_are_harmless():
    import os

    real = core.System()
    assert real.process_image(0x7FFFFFF0) is None  # no such process
    if sys.platform == "win32":
        assert Path(real.process_image(os.getpid())).name.lower().startswith("python")
        # Our own process never matches another image, so it is not terminated.
        assert real.terminate(os.getpid(), Path(r"C:\nowhere\pythonw.exe")) is False

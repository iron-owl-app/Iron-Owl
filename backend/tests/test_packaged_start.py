"""Packaged start (B0): FINTRACK_HOME config, /api/health, /api/app/shutdown, the heartbeat
self-exit watcher and run_packaged.py."""
from __future__ import annotations

import http.client
import json
import os
import re
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import config, lifecycle
from app.config import Settings
from app.main import create_app
from app.routers import app as app_routes
from app.version import __version__

from tests.conftest import BASE_URL, CSRF, PASSWORD, make_settings

BACKEND = Path(__file__).resolve().parents[1]
SECRET = "c" * 43
NONCE = "n0nce_-" * 4


# ------------------------------------------------------------------------ config


@pytest.fixture
def clean_env(monkeypatch):
    for name in ("FINTRACK_HOME", "FINTRACK_WEB", "FINTRACK_INSTALL_ROOT", "DATA_DIR",
                 "FRONTEND_DIST", "SUPPORT_CONTACT", "CONTROL_SECRET", "EXIT_WHEN_UNUSED_SECONDS",
                 "AUTO_LOCK_MINUTES", "PORT"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_home_moves_data_and_settings_file(tmp_path, clean_env):
    home = tmp_path / "FinTrack"
    home.mkdir()
    (home / config.HOME_SETTINGS_FILE).write_text(
        "AUTO_LOCK_MINUTES=7\nSUPPORT_CONTACT=Sam\nFINTRACK_HOME=C:/somewhere-else\n", encoding="utf-8")
    clean_env.setenv("FINTRACK_HOME", str(home))
    clean_env.setenv("FINTRACK_WEB", str(tmp_path / "versions" / "1.4.0" / "web"))
    clean_env.setenv("FINTRACK_INSTALL_ROOT", str(tmp_path / "Programs" / "FinTrack"))
    assert config.settings_file() == home / "fintrack.env"
    settings = config.load_settings()
    assert settings.fintrack_home == home
    assert settings.data_dir == home / "data"
    assert settings.db_path == home / "data" / "fintrack.db"
    assert settings.auto_lock_minutes == 7 and settings.support_contact == "Sam"
    assert settings.frontend_dist == tmp_path / "versions" / "1.4.0" / "web"
    assert settings.fintrack_install_root == tmp_path / "Programs" / "FinTrack"


def test_explicit_data_dir_still_wins(tmp_path, clean_env):
    clean_env.setenv("FINTRACK_HOME", str(tmp_path / "home"))
    clean_env.setenv("DATA_DIR", str(tmp_path / "elsewhere"))
    assert config.load_settings().data_dir == tmp_path / "elsewhere"


def test_without_home_the_repo_layout_is_unchanged(tmp_path, clean_env):
    assert config.settings_file() == config.REPO_ROOT / ".env"
    clean_env.setattr(config, "settings_file", lambda: tmp_path / "absent.env")
    settings = config.load_settings()
    assert settings.fintrack_home is None and settings.fintrack_install_root is None
    assert settings.data_dir == config.REPO_ROOT / "data"
    assert settings.frontend_dist == config.REPO_ROOT / "frontend" / "dist"
    assert settings.repo_root == config.REPO_ROOT
    assert settings.exit_when_unused_seconds == 0 and not settings.control_enabled
    assert settings.support_contact == ""


def test_packaged_settings_validation(tmp_path):
    assert make_settings(tmp_path, support_contact="  S\x00am\n ").support_contact == "Sam"
    assert make_settings(tmp_path, support_contact="x" * 60).support_contact == "x" * 60
    # Too long is cut, never refused: the owner's own settings must not stop FinTrack.
    assert make_settings(tmp_path, support_contact="x" * 61).support_contact == "x" * 60
    assert make_settings(tmp_path, support_contact="\t" + "y" * 59 + "  zzz").support_contact == "y" * 59
    with pytest.raises(ValidationError):
        make_settings(tmp_path, control_secret="too-short")
    with pytest.raises(ValidationError):
        make_settings(tmp_path, exit_when_unused_seconds=-1)
    s = make_settings(tmp_path, control_secret=SECRET)
    assert s.control_enabled and SECRET not in repr(s)


# ------------------------------------------------------------------------ health


def packaged_client(tmp_path, fake_plaid, **overrides) -> TestClient:
    app = create_app(make_settings(tmp_path, **overrides), fake_plaid)
    return TestClient(app, base_url=BASE_URL, headers=CSRF)


def test_health_without_session(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"app", "version", "boot_id", "window_open", "web"}
    assert body["app"] == "fintrack" and body["version"] == __version__
    assert re.fullmatch(r"[0-9a-f]{16}", body["boot_id"])
    assert body["window_open"] is False and body["web"] is False
    assert r.headers["cache-control"] == "no-store"
    # a challenge without a configured secret gets no proof
    assert "proof" not in client.get("/api/health", headers={app_routes.CHALLENGE_HEADER: NONCE}).json()


def test_health_web_flag_follows_the_built_spa(tmp_path, fake_plaid):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
    with packaged_client(tmp_path, fake_plaid) as c:
        assert c.get("/api/health").json()["web"] is True


def test_health_proof_and_boot_id(tmp_path, fake_plaid):
    with packaged_client(tmp_path, fake_plaid, control_secret=SECRET) as c:
        plain = c.get("/api/health").json()
        assert "proof" not in plain
        body = c.get("/api/health", headers={app_routes.CHALLENGE_HEADER: NONCE}).json()
        assert body["proof"] == lifecycle.health_proof(SECRET, NONCE, body["boot_id"])
        assert body["proof"] != lifecycle.health_proof("d" * 43, NONCE, body["boot_id"])
        for bad in ("short", "x" * 129, "has space here!!", "\u00e9" * 20):
            r = c.get("/api/health", headers={app_routes.CHALLENGE_HEADER: bad.encode("utf-8")})
            assert r.status_code == 400
    with packaged_client(tmp_path, fake_plaid, control_secret=SECRET) as c2:
        assert c2.get("/api/health").json()["boot_id"] != body["boot_id"]  # new per start


def test_health_host_allowlist_applies(client):
    assert client.get("/api/health", headers={"Host": "evil.example:8000"}).status_code == 400


def test_health_is_not_activity_or_a_heartbeat(clock, unlocked, vault, app):
    watcher = app.state.fintrack.exit_watcher
    watcher._heard = False
    clock.advance(15 * 60 + 1)
    assert unlocked.get("/api/health").status_code == 200
    assert watcher._heard is False
    vault.check_idle()
    assert not vault.unlocked


def test_health_body_never_has_session_data(unlocked):
    text = unlocked.get("/api/health").text
    assert "session" not in text and PASSWORD not in text


# ---------------------------------------------------------------------- shutdown


class ExitRecorder:
    def __init__(self) -> None:
        self.codes: list[int] = []

    def __call__(self, code: int) -> None:
        self.codes.append(code)

    def wait(self, timeout: float = 3.0) -> list[int]:
        deadline = time.monotonic() + timeout
        while not self.codes and time.monotonic() < deadline:
            time.sleep(0.02)
        return self.codes


def test_shutdown_is_off_without_a_control_secret(client):
    recorder = ExitRecorder()
    client.app.state.request_exit = recorder
    r = client.post("/api/app/shutdown", headers={app_routes.CONTROL_HEADER: "x" * 43})
    assert r.status_code == 404
    assert recorder.wait(0.3) == []


def test_shutdown_needs_the_secret_and_csrf(tmp_path, fake_plaid):
    with packaged_client(tmp_path, fake_plaid, control_secret=SECRET) as c:
        recorder = ExitRecorder()
        c.app.state.request_exit = recorder
        assert c.post("/api/app/shutdown").status_code == 403
        assert c.post("/api/app/shutdown", headers={app_routes.CONTROL_HEADER: "d" * 43}).status_code == 403
        assert c.post("/api/app/shutdown", headers={app_routes.CONTROL_HEADER: SECRET[:-1]}).status_code == 403
        raw = TestClient(c.app, base_url=BASE_URL)  # no X-FinTrack header
        assert raw.post("/api/app/shutdown", headers={app_routes.CONTROL_HEADER: SECRET}).status_code == 403
        form = c.post("/api/app/shutdown", headers={app_routes.CONTROL_HEADER: SECRET,
                                                    "Content-Type": "text/plain"}, content=b"x")
        assert form.status_code == 403
        assert c.post("/api/app/shutdown", headers={app_routes.CONTROL_HEADER: SECRET},
                      json={"restart": False, "extra": 1}).status_code == 422
        assert recorder.wait(0.3) == []


def test_shutdown_requests_exit_0(tmp_path, fake_plaid):
    with packaged_client(tmp_path, fake_plaid, control_secret=SECRET) as c:
        recorder = ExitRecorder()
        c.app.state.request_exit = recorder
        r = c.post("/api/app/shutdown", headers={app_routes.CONTROL_HEADER: SECRET})
        assert r.status_code == 202 and r.json() == {"ok": True, "exit_code": 0}
        assert recorder.wait() == [0]


def test_shutdown_restart_requests_exit_75(tmp_path, fake_plaid):
    with packaged_client(tmp_path, fake_plaid, control_secret=SECRET) as c:
        recorder = ExitRecorder()
        c.app.state.request_exit = recorder
        r = c.post("/api/app/shutdown", headers={app_routes.CONTROL_HEADER: SECRET}, json={"restart": True})
        assert r.status_code == 202 and r.json()["exit_code"] == lifecycle.EXIT_RESTART == 75
        assert recorder.wait() == [75]


def test_unsupervised_exit_request_only_logs(client, caplog):
    client.app.state.request_exit(0)
    assert "not started by run_packaged" in caplog.text


# ------------------------------------------------------------------ exit watcher


def make_watcher(limit=180, busy=None):
    recorder = ExitRecorder()
    watcher = lifecycle.ExitWatcher(limit, recorder, busy_checks=busy)
    return watcher, recorder


def test_watcher_off_by_default():
    watcher, recorder = make_watcher(limit=0)
    assert not watcher.enabled
    for _ in range(100):
        assert watcher.tick() is False
    assert recorder.codes == []


def test_watcher_exits_after_the_missed_ticks():
    watcher, recorder = make_watcher(limit=180)
    assert watcher.max_misses == 6
    for _ in range(5):
        assert watcher.tick() is False
    assert recorder.codes == []
    assert watcher.tick() is True
    assert recorder.codes == [0]
    for _ in range(10):
        watcher.tick()
    assert recorder.codes == [0]  # once


def test_watcher_rounds_up_partial_ticks():
    assert make_watcher(limit=31)[0].max_misses == 2
    assert make_watcher(limit=1)[0].max_misses == 1


def test_heartbeat_resets_the_count():
    watcher, recorder = make_watcher(limit=90)
    watcher.tick()
    watcher.tick()
    watcher.beat()
    assert watcher.tick() is False and watcher.misses == 0
    watcher.tick()
    watcher.tick()
    assert recorder.codes == []
    assert watcher.tick() is True


def test_unlocked_vault_keeps_the_server_up():
    busy = {"v": True}
    watcher, recorder = make_watcher(limit=60, busy=[lambda: busy["v"]])
    for _ in range(50):
        watcher.tick()
    assert recorder.codes == []
    busy["v"] = False
    watcher.tick()
    watcher.tick()
    assert recorder.codes == [0]


def test_failing_busy_check_counts_as_busy():
    def boom() -> bool:
        raise RuntimeError("x")

    watcher, recorder = make_watcher(limit=30, busy=[boom])
    for _ in range(5):
        watcher.tick()
    assert recorder.codes == []


def test_watcher_is_sleep_safe(monkeypatch):
    """A tick after the computer slept for hours is one miss, not an instant exit."""
    watcher, recorder = make_watcher(limit=180)
    fake_time = {"t": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: fake_time["t"])
    monkeypatch.setattr(time, "time", lambda: fake_time["t"])
    watcher.tick()
    fake_time["t"] += 8 * 3600
    watcher.tick()
    assert watcher.misses == 2 and recorder.codes == []


def test_only_the_window_heartbeat_counts(tmp_path, fake_plaid):
    # App-window release: POST /api/app/alive is the heartbeat; other requests are not.
    with packaged_client(tmp_path, fake_plaid, exit_when_unused_seconds=60, control_secret=SECRET) as c:
        watcher = c.app.state.fintrack.exit_watcher
        assert watcher.enabled and watcher.max_misses == 2
        watcher._heard = False
        c.get("/api/health")
        c.post("/api/app/shutdown", headers={app_routes.CONTROL_HEADER: "wrong" * 9})
        c.get("/api/auth/status")
        c.get("/api/accounts")
        tab = {"X-FinTrack-Tab": "tab-0000-0001"}
        c.post("/api/app/alive", headers={**tab, "Host": "evil:1"})  # refused: not a heartbeat
        assert watcher._heard is False
        assert c.post("/api/app/alive", headers=tab).status_code == 200
        assert watcher._heard is True


def test_lifespan_runs_the_watcher_and_requests_exit(tmp_path, fake_plaid):
    app = create_app(make_settings(tmp_path, exit_when_unused_seconds=60), fake_plaid)
    recorder = ExitRecorder()
    app.state.request_exit = recorder
    app.state.fintrack.exit_watcher.tick_seconds = 0.02
    with TestClient(app, base_url=BASE_URL, headers=CSRF):
        assert recorder.wait() == [0]


def test_no_watcher_task_when_disabled(tmp_path, fake_plaid):
    app = create_app(make_settings(tmp_path), fake_plaid)
    recorder = ExitRecorder()
    app.state.request_exit = recorder
    app.state.fintrack.exit_watcher.tick_seconds = 0.01
    with TestClient(app, base_url=BASE_URL, headers=CSRF):
        assert recorder.wait(0.3) == []


# ------------------------------------------------------------------- run_packaged


def _child_env() -> dict[str, str]:
    """This process's environment without anything that could point a child server at other
    data or real Plaid keys (FINTRACK_*, DATA_DIR, PLAID_*, ...)."""
    drop = {"DATA_DIR", "FRONTEND_DIST", "PORT", "HOST", "CONTROL_SECRET", "SUPPORT_CONTACT",
            "EXIT_WHEN_UNUSED_SECONDS", "REPO_ROOT"}
    return {k: v for k, v in os.environ.items()
            if not k.upper().startswith(("FINTRACK_", "PLAID_")) and k.upper() not in drop}


def run_python(code: str, env: dict | None = None, cwd: Path = BACKEND, timeout: float = 60) -> subprocess.CompletedProcess:
    full_env = _child_env()
    full_env.update(env or {})
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code)], cwd=str(cwd), env=full_env,
                          capture_output=True, text=True, timeout=timeout)


def test_importing_run_packaged_has_no_side_effects():
    out = run_python("""
        import json, sys
        before = sys.stdout
        import run_packaged
        print(json.dumps({"uvicorn": "uvicorn" in sys.modules, "app": "app" in sys.modules,
                          "same_stdout": sys.stdout is before}))
    """)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == {"uvicorn": False, "app": False, "same_stdout": True}


def test_logging_setup_survives_pythonw_streams(tmp_path):
    out = run_python(f"""
        import sys, logging
        sys.stdout = None
        sys.stderr = None  # what pythonw.exe gives us
        import run_packaged
        run_packaged.setup_logging(__import__("pathlib").Path({str(tmp_path / 'logs')!r}))
        print("printed line")
        sys.stderr.write("stderr line\\n")
        logging.getLogger("uvicorn.error").info("uvicorn line")
        import uvicorn  # imported after the streams were replaced: must not fail
        logging.shutdown()
    """)
    assert out.returncode == 0, out.stderr
    text = (tmp_path / "logs" / "server.log").read_text(encoding="utf-8")
    for line in ("printed line", "stderr line", "uvicorn line"):
        assert line in text


def test_main_needs_fintrack_home(tmp_path):
    out = run_python("import run_packaged, sys; sys.exit(run_packaged.main())")
    assert out.returncode == run_packaged_exit_config()


def run_packaged_exit_config() -> int:
    sys.path.insert(0, str(BACKEND))
    try:
        import run_packaged
    finally:
        sys.path.remove(str(BACKEND))
    return run_packaged.EXIT_CONFIG


def test_package_layout_paths(tmp_path, monkeypatch):
    sys.path.insert(0, str(BACKEND))
    try:
        import run_packaged
    finally:
        sys.path.remove(str(BACKEND))
    monkeypatch.delenv("FINTRACK_INSTALL_ROOT", raising=False)
    root = tmp_path / "Programs" / "FinTrack"
    vdir = root / "versions" / "1.4.0"
    (vdir / "backend" / "app").mkdir(parents=True)
    (vdir / "backend" / "app" / "__init__.py").write_text("", encoding="utf-8")
    assert run_packaged.code_dir(vdir) == vdir / "backend"
    assert run_packaged.code_dir(BACKEND) == BACKEND
    assert run_packaged.install_root(vdir) == root
    assert run_packaged.runtime_dirs(vdir) == []  # no release.json
    (vdir / "release.json").write_text(json.dumps({"deps_id": "0123456789abcdef"}), encoding="utf-8")
    assert run_packaged.runtime_dirs(vdir) == []  # runtime folder missing
    shared = root / "runtimes" / "0123456789abcdef" / "site-packages"
    shared.mkdir(parents=True)
    assert run_packaged.runtime_dirs(vdir) == [shared]
    (vdir / "release.json").write_text(json.dumps({"deps_id": "../../evil"}), encoding="utf-8")
    assert run_packaged.runtime_dirs(vdir) == []
    monkeypatch.setenv("FINTRACK_INSTALL_ROOT", str(tmp_path / "other"))
    assert run_packaged.install_root(vdir) == tmp_path / "other"


def test_setup_paths_adds_code_and_runtime_with_pth(tmp_path):
    root = tmp_path / "root"
    vdir = root / "versions" / "1.4.0"
    (vdir / "backend" / "app").mkdir(parents=True)
    (vdir / "backend" / "app" / "__init__.py").write_text("MARK = 'packaged'\n", encoding="utf-8")
    (vdir / "release.json").write_text(json.dumps({"deps_id": "0123456789abcdef"}), encoding="utf-8")
    site = root / "runtimes" / "0123456789abcdef" / "site-packages"
    extra = tmp_path / "extra"
    site.mkdir(parents=True)
    extra.mkdir()
    (site / "zz.pth").write_text(str(extra) + "\n", encoding="utf-8")
    out = run_python(f"""
        import json, sys
        from pathlib import Path
        sys.path.insert(0, {str(BACKEND)!r})
        import run_packaged
        sys.path.remove({str(BACKEND)!r})
        sys.path = [p for p in sys.path if p not in ("", ".")]
        run_packaged.setup_paths(Path({str(vdir)!r}))
        import app
        print(json.dumps({{"mark": getattr(app, "MARK", None), "site": {str(site)!r} in sys.path,
                          "extra": {str(extra)!r} in sys.path}}))
    """, cwd=tmp_path)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == {"mark": "packaged", "site": True, "extra": True}


def _free_port(first: int = 8030, last: int = 8099) -> int:
    for port in range(first, last + 1):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    pytest.skip("no free port in 8030-8099")


def _http(method: str, port: int, path: str, headers: dict, body: bytes | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        conn.request(method, path, body=body, headers={"Host": f"127.0.0.1:{port}", **headers})
        r = conn.getresponse()
        return r.status, r.read()
    finally:
        conn.close()


@pytest.mark.parametrize("restart", [False, True])
def test_run_packaged_serves_health_and_stops_on_request(tmp_path, restart):
    port = _free_port()
    home = tmp_path / "home"
    env = _child_env()
    env.update({"FINTRACK_HOME": str(home), "PORT": str(port), "CONTROL_SECRET": SECRET,
                "EXIT_WHEN_UNUSED_SECONDS": "0", "FINTRACK_WEB": str(tmp_path / "no-web")})
    proc = subprocess.Popen([sys.executable, str(BACKEND / "run_packaged.py")], cwd=str(tmp_path), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 30
        body = None
        while time.monotonic() < deadline:
            assert proc.poll() is None, proc.stderr.read().decode(errors="replace")
            try:
                status, raw = _http("GET", port, "/api/health", {app_routes.CHALLENGE_HEADER: NONCE})
            except OSError:
                time.sleep(0.2)
                continue
            if status == 200:
                body = json.loads(raw)
                break
        assert body is not None, "server did not answer"
        assert body["app"] == "fintrack" and body["web"] is False
        assert body["proof"] == lifecycle.health_proof(SECRET, NONCE, body["boot_id"])
        payload = json.dumps({"restart": restart}).encode()
        status, _ = _http("POST", port, "/api/app/shutdown",
                          {"X-FinTrack": "1", app_routes.CONTROL_HEADER: SECRET,
                           "Content-Type": "application/json"}, payload)
        assert status == 202
        code = proc.wait(timeout=30)
        assert code == (75 if restart else 0)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
    out, err = proc.communicate()
    assert out == b"" and err == b""  # everything went to the log
    log_text = (home / "logs" / "server.log").read_text(encoding="utf-8")
    assert f"starting FinTrack on port {port}" in log_text
    assert f"stopped (exit code {75 if restart else 0})" in log_text
    assert SECRET not in log_text


def test_long_support_contact_in_the_settings_file_still_starts(tmp_path, clean_env):
    home = tmp_path / "FinTrack"
    home.mkdir()
    (home / config.HOME_SETTINGS_FILE).write_text("SUPPORT_CONTACT=" + "T" * 200 + "\n", encoding="utf-8")
    clean_env.setenv("FINTRACK_HOME", str(home))
    assert config.load_settings().support_contact == "T" * config.SUPPORT_CONTACT_MAX


def test_validation_summary_names_fields_not_values(tmp_path):
    short = "tiny-secret-VALUE-42"
    with pytest.raises(ValidationError) as info:
        make_settings(tmp_path, control_secret=short, plaid_env="prod-" + short)
    text = config.validation_summary(info.value)
    assert "control_secret: value_error" in text and "plaid_env: literal_error" in text
    assert short not in text and "input" not in text


def test_bad_settings_log_field_names_only(tmp_path):
    """A bad CONTROL_SECRET (or any setting) stops the start with exit 2; server.log names the
    field and the error type but never the value."""
    home = tmp_path / "home"
    short = "shortSecretValue-9f3a"  # < 32 characters: refused
    env = _child_env()
    env.update({"FINTRACK_HOME": str(home), "PORT": str(_free_port()), "CONTROL_SECRET": short,
                "PLAID_ENV": "not-a-plaid-env-" + short, "FINTRACK_WEB": str(tmp_path / "no-web")})
    out = subprocess.run([sys.executable, str(BACKEND / "run_packaged.py")], cwd=str(tmp_path), env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == run_packaged_exit_config() == 2
    assert out.stdout == "" and out.stderr == ""
    log_text = (home / "logs" / "server.log").read_text(encoding="utf-8")
    assert "settings are invalid" in log_text
    assert "control_secret: value_error" in log_text and "plaid_env: literal_error" in log_text
    assert short not in log_text
    assert "Traceback" not in log_text and "input_value" not in log_text


def test_settings_repr_hides_the_secret(tmp_path):
    s = Settings(_env_file=None, control_secret=SECRET, data_dir=tmp_path)
    assert SECRET not in repr(s) and SECRET not in str(s.model_dump())

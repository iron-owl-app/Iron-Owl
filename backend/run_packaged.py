"""Start FinTrack from a packaged install (started by launch.pyw under pythonw.exe).

In the package this file sits at ``versions/<v>/run_packaged.py`` next to ``backend/app``,
``web/`` and ``release.json``; in the repo it is ``backend/run_packaged.py`` next to ``app/``.

Order matters here:
1. pythonw.exe has no console: ``sys.stdout``/``sys.stderr`` are None, and uvicorn's logging
   (and any stray print) would crash or vanish. They are pointed at a rotating log under
   ``FINTRACK_HOME/logs`` *before* uvicorn or the app is imported.
2. The embeddable Python ignores PYTHONPATH (its ``._pth`` file fixes ``sys.path``), so the
   code folder and the shared runtime ``runtimes/<deps_id>/site-packages`` are added here.
3. uvicorn runs through ``uvicorn.Server`` so ``app.state.request_exit(code)`` can stop it
   and choose the process exit code (0 = stopped, 75 = restart; see app/lifecycle.py).

Environment (set by the launcher): FINTRACK_HOME (required), FINTRACK_WEB,
FINTRACK_INSTALL_ROOT, PORT, SUPPORT_CONTACT, EXIT_WHEN_UNUSED_SECONDS, CONTROL_SECRET.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import sys
from pathlib import Path

LOG_NAME = "server.log"
LOG_MAX_BYTES = 1024 * 1024
LOG_BACKUPS = 3
DEPS_ID_RE = re.compile(r"^[0-9a-f]{16}$")
LOOPBACK_ALIASES = {"127.0.0.1": "127.0.0.1", "localhost": "127.0.0.1"}

EXIT_CONFIG = 2  # bad or missing configuration (the launcher logs FT-START-03)
# A request still open at shutdown (e.g. the "Change folder…" window) can't hold up the
# exit forever: uvicorn cancels it after this many seconds, then the shutdown lock closes
# that window.
GRACEFUL_SHUTDOWN_SECONDS = 5


class _LogStream:
    """A file-like stand-in for stdout/stderr that writes whole lines to a logger."""

    encoding = "utf-8"
    errors = "replace"

    def __init__(self, logger: logging.Logger, level: int) -> None:
        self._logger = logger
        self._level = level
        self._buffer = ""

    def write(self, text: str) -> int:
        if not isinstance(text, str):
            text = str(text)
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.strip():
                self._logger.log(self._level, line.rstrip())
        return len(text)

    def flush(self) -> None:
        if self._buffer.strip():
            self._logger.log(self._level, self._buffer.rstrip())
        self._buffer = ""

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        raise OSError("no file descriptor")

    def writable(self) -> bool:
        return True


def setup_logging(log_dir: Path) -> logging.Handler:
    """Root logger → rotating file; stdout/stderr → that logger. Safe under pythonw."""
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_dir / LOG_NAME, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8",
        delay=False,
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    # Replace the streams even when they exist (python.exe from a console): one log for all.
    sys.stdout = _LogStream(logging.getLogger("stdout"), logging.INFO)
    sys.stderr = _LogStream(logging.getLogger("stderr"), logging.ERROR)
    return handler


def code_dir(here: Path) -> Path:
    """Folder that holds the ``app`` package: next to this file (repo), or ``backend/``."""
    if (here / "app" / "__init__.py").is_file():
        return here
    return here / "backend"


def install_root(here: Path) -> Path | None:
    raw = os.environ.get("FINTRACK_INSTALL_ROOT", "").strip()
    if raw:
        return Path(raw)
    if here.parent.name == "versions":
        return here.parent.parent
    return None


def runtime_dirs(here: Path) -> list[Path]:
    """Site-packages folders for this version: the shared ``runtimes/<deps_id>`` one named by
    release.json (and a legacy per-version ``site-packages`` if present)."""
    dirs: list[Path] = []
    local = here / "site-packages"
    if local.is_dir():
        dirs.append(local)
    release = here / "release.json"
    root = install_root(here)
    if release.is_file() and root is not None:
        try:
            deps_id = json.loads(release.read_text(encoding="utf-8")).get("deps_id")
        except (OSError, ValueError, AttributeError):
            deps_id = None
        if isinstance(deps_id, str) and DEPS_ID_RE.fullmatch(deps_id):
            shared = root / "runtimes" / deps_id / "site-packages"
            if shared.is_dir():
                dirs.append(shared)
            else:
                logging.getLogger("fintrack").error("runtime %s is missing", deps_id)
        else:
            logging.getLogger("fintrack").error("release.json has no valid deps_id")
    return dirs


def setup_paths(here: Path) -> None:
    import site

    code = str(code_dir(here))
    if code not in sys.path:
        sys.path.insert(0, code)
    for folder in runtime_dirs(here):
        # addsitedir also runs the folder's .pth files (e.g. for packages that need them).
        site.addsitedir(str(folder))


def main() -> int:
    here = Path(__file__).resolve().parent
    home_raw = os.environ.get("FINTRACK_HOME", "").strip()
    if not home_raw:
        # Only the launcher starts this file; without FINTRACK_HOME there is nowhere safe
        # to put data or logs.
        return EXIT_CONFIG
    setup_logging(Path(home_raw) / "logs")
    log = logging.getLogger("fintrack")
    try:
        import faulthandler

        fault_file = open(Path(home_raw) / "logs" / "fault.log", "a", encoding="utf-8")  # noqa: SIM115
        faulthandler.enable(fault_file)
    except Exception:  # noqa: BLE001 - diagnostics only
        pass

    try:
        setup_paths(here)
        import uvicorn
        from pydantic import ValidationError

        from app.config import get_settings, validation_summary
        from app.main import create_app
    except Exception:  # noqa: BLE001
        log.exception("FinTrack could not load")
        return EXIT_CONFIG

    try:
        settings = get_settings()
    except ValidationError as exc:
        # Field names and error types only: the values (CONTROL_SECRET, Plaid keys) never
        # reach the log, so no traceback either (it would quote them).
        log.error("FinTrack settings are invalid: %s", validation_summary(exc))
        return EXIT_CONFIG
    except Exception as exc:  # noqa: BLE001
        log.error("FinTrack settings could not be read (%s)", type(exc).__name__)
        return EXIT_CONFIG

    host = LOOPBACK_ALIASES.get(settings.host.strip().lower())
    if host is None:
        # Never expose the vault on a network interface.
        log.error("refusing to bind to a non-loopback HOST")
        return EXIT_CONFIG

    app = create_app(settings)
    config = uvicorn.Config(
        app,
        host=host,
        port=settings.port,
        proxy_headers=False,
        server_header=False,
        log_config=None,  # keep our root handler; uvicorn's loggers propagate to it
        log_level="info",
        use_colors=False,
        access_log=False,  # URLs can carry search words; the log stays about the app
        lifespan="on",
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )
    server = uvicorn.Server(config)
    exit_code = {"code": 0}

    def request_exit(code: int) -> None:
        exit_code["code"] = int(code)
        server.should_exit = True

    app.state.request_exit = request_exit
    log.info("starting FinTrack on port %d (web: %s)", settings.port,
             settings.frontend_dist.joinpath("index.html").is_file())
    try:
        server.run()
    except SystemExit as exc:  # uvicorn exits(1) when the port can't be bound
        log.error("server stopped during start (%s)", exc.code)
        return 1
    except Exception:  # noqa: BLE001
        log.exception("server crashed")
        return 1
    if not server.started:
        log.error("server did not start")
        return 1
    log.info("stopped (exit code %d)", exit_code["code"])
    return exit_code["code"]


if __name__ == "__main__":
    code = main()
    logging.shutdown()
    sys.exit(code)

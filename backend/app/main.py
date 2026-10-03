from __future__ import annotations

import asyncio
import contextlib
import logging
import mimetypes
import secrets
from collections.abc import AsyncIterator, Callable
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import lifecycle
from .config import Settings, get_settings
from .deps import AppState
from .plaid_client import PlaidClient
from .presence import Presence
from .routers import app as app_routes
from .routers import (
    accounts,
    alerts,
    auth,
    backup,
    budgets,
    categories,
    category_groups,
    dashboard,
    goals,
    holdings,
    home,
    investments,
    loans,
    plaid,
    plaid_keys,
    recovery,
    recurring,
    reports,
    rules,
    summary,
    preferences,
    tags,
    transactions,
    update,
)
from .security import SecurityMiddleware, Vault, restrict_to_owner
from .services import automation, prefs, support_contact
from .services.folder_picker import FolderPicker
from .services.plaid_keys import PlaidKeyStore
from .services.backup import cleanup_staging
from .updates.service import Updater
from .version import __version__

log = logging.getLogger("fintrack")

# Home v2: the number font (@fontsource/ibm-plex-sans) ships as .woff2/.woff files in dist/.
# Windows' registry may not know these types, and every response is nosniff, so a wrong or
# missing type would stop the browser from using the font.
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("font/woff", ".woff")

IDLE_CHECK_SECONDS = 30
# Release 3: automatic sync and the 24-hourly automatic backup are checked this often.
AUTOMATION_CHECK_SECONDS = 5 * 60


def _validation_message(exc: RequestValidationError) -> str:
    # Only location + message: FastAPI's default body echoes the input back,
    # which could include a password.
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()) if p != "body")
        parts.append(f"{loc}: {err.get('msg')}" if loc else str(err.get("msg")))
    return "; ".join(parts) or "invalid request"


def create_app(
    settings: Settings | None = None,
    plaid_client: PlaidClient | None = None,
    plaid_factory: Callable[[str, str, str], PlaidClient] | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    # Who to call for help: a name saved in Settings (or by the installer) wins.
    support_contact.apply_saved(settings)
    state = AppState(
        settings=settings,
        vault=Vault(settings),
        plaid=plaid_client or PlaidClient(settings),
    )
    state.auto_backup = automation.AutoBackups(state.vault)
    # Every lock (manual, idle auto-lock, shutdown) backs up first, while the key is still held.
    state.vault.before_lock = state.auto_backup.on_lock
    # Release 3.4: the Plaid keys saved in the vault are loaded after every unlock and
    # dropped after every lock (every lock goes through Vault._lock_now).
    state.plaid_factory = plaid_factory or PlaidClient.from_keys
    state.plaid_keys = PlaidKeyStore(state.plaid_factory)
    # Settings (D7): the native "Change folder…" window for automatic backups (one at a time).
    state.folder_picker = FolderPicker()

    def after_lock() -> None:
        try:
            state.plaid_keys.clear()
        finally:
            # An open "Change folder…" window closes with the session (and never holds up
            # the server's exit: shutdown locks too). Never raises.
            state.folder_picker.close()

    state.vault.after_lock = after_lock
    # Private updates: off (409 "disabled") in git checkouts, dev and copies not installed
    # by the launcher. The first unlock of a just-installed version finishes the update; a
    # data upgrade that fails while an update is pending asks the launcher to roll back.
    state.updater = Updater(
        settings, state.vault, auto_backup=state.auto_backup,
        request_exit=lambda code: app.state.request_exit(code),
    )

    def after_unlock() -> None:
        try:
            # Settings (D7): the auto-lock minutes saved in the vault (never raises).
            prefs.load_auto_lock(state)
            state.plaid_keys.reload(state)
        finally:
            state.updater.on_unlocked()

    state.vault.after_unlock = after_unlock
    state.vault.on_open_failed = state.updater.on_open_failed
    state.boot_id = secrets.token_hex(8)
    # App-window states: which FinTrack windows are open (status, heartbeats, closing).
    state.presence = Presence()
    # Packaged start: stop once no window has sent POST /api/app/alive for a while (the
    # idle auto-lock still applies first: an unlocked vault always keeps the server up).
    state.exit_watcher = lifecycle.ExitWatcher(
        settings.exit_when_unused_seconds,
        request_exit=lambda code: app.state.request_exit(code),
        busy_checks=[lambda: state.vault.unlocked, lambda: state.updater.busy],
    )

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async def idle_watch() -> None:
            # The server enforces auto-lock even if no request ever arrives.
            while True:
                await asyncio.sleep(IDLE_CHECK_SECONDS)
                try:
                    await asyncio.to_thread(state.vault.check_idle)
                    # Backstop for the close timers (a window closed, grace over).
                    await asyncio.to_thread(app_routes.finish_due_closes, state)
                    # Packaged start: the session's window went silent (no beacon arrived).
                    await asyncio.to_thread(app_routes.finish_silent_window, state)
                except Exception:  # noqa: BLE001
                    log.exception("idle check failed")

        async def automation_loop() -> None:
            while True:
                await asyncio.sleep(AUTOMATION_CHECK_SECONDS)
                for job, name in (
                    (automation.sync_tick, "sync"),
                    (automation.freeze_tick, "plan freeze"),  # once per new month, unlocked only
                    (_periodic_backup, "backup"),
                    (automation.alerts_tick, "alert check"),  # hourly; its own throttle
                    (_update_scan, "update scan"),  # unlocked + updater on only; 30 s throttle
                ):
                    try:
                        await asyncio.to_thread(job, state)
                    except Exception as exc:  # noqa: BLE001
                        log.error("automatic %s check failed: %s", name, type(exc).__name__)

        async def exit_watch() -> None:
            watcher = state.exit_watcher
            while not watcher.fired:
                await asyncio.sleep(watcher.tick_seconds)
                try:
                    await asyncio.to_thread(watcher.tick)
                except Exception as exc:  # noqa: BLE001
                    log.error("exit watch failed: %s", type(exc).__name__)

        if settings.data_dir.is_dir():
            # Existing installs: make sure the vault folder is private to this user.
            await asyncio.to_thread(restrict_to_owner, settings.data_dir)
            # Scratch folders from a crash mid backup/restore hold nothing we still need.
            await asyncio.to_thread(cleanup_staging, settings.data_dir)
        try:
            # Updates (installed copies only): the launcher's result of the last update
            # (failed / rolled-back ids are never offered again), and offers that aren't
            # newer than this version any more. Never scans at startup.
            await asyncio.to_thread(state.updater.startup)
        except Exception as exc:  # noqa: BLE001 - never stops FinTrack from starting
            log.error("update startup check failed: %s", type(exc).__name__)
        tasks = [asyncio.create_task(idle_watch()), asyncio.create_task(automation_loop())]
        if state.exit_watcher.enabled:
            tasks.append(asyncio.create_task(exit_watch()))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            state.vault.lock("shutdown")  # backs up first when automatic backups are on

    docs = settings.debug
    app = FastAPI(
        title="Iron Owl",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.state.fintrack = state
    # run_packaged.py replaces this with one that stops uvicorn with ``code`` (lifecycle.py).
    app.state.request_exit = _exit_not_supervised

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse({"detail": _validation_message(exc)}, status_code=422)

    @app.exception_handler(OverflowError)
    async def overflow_handler(_: Request, __: OverflowError) -> JSONResponse:
        # e.g. an id or offset beyond SQLite's 64-bit INTEGER in a path or query string.
        return JSONResponse({"detail": "a number in the request is out of range"}, status_code=422)

    for module in (
        auth, accounts, summary, transactions, holdings, plaid,
        categories, budgets, recurring, goals, rules, alerts, backup,
        category_groups, tags, reports, loans, plaid_keys, home, dashboard, recovery, update, investments, preferences,
    ):
        app.include_router(module.router)
    app.include_router(app_routes.router)

    _add_frontend(app, settings.frontend_dist)
    app.add_middleware(SecurityMiddleware, settings=settings)
    return app


def _exit_not_supervised(code: int) -> None:
    log.warning("exit %d requested, but this server was not started by run_packaged.py", code)


def _periodic_backup(state: AppState) -> None:
    if state.vault.unlocked:
        state.auto_backup.periodic()


def _update_scan(state: AppState) -> None:
    state.updater.scan_tick(state.vault.unlocked)


def _add_frontend(app: FastAPI, dist: Path) -> None:
    """Serve the built SPA at / with index.html fallback; /api/* never falls through."""
    root = dist.resolve()

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def spa(path: str) -> FileResponse:
        if path == "api" or path.startswith("api/"):
            raise StarletteHTTPException(status_code=404, detail="Not Found")
        index = root / "index.html"
        if not index.is_file():
            raise StarletteHTTPException(status_code=404, detail="Not Found")
        candidate = _static_candidate(root, path)
        if candidate is not None:
            return FileResponse(candidate)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})


# Characters that let a URL path name something other than a file under dist/ on
# Windows: backslash (UNC shares / other roots), colon (drives, NTFS streams), NUL.
_UNSAFE_PATH_CHARS = frozenset("\\:\x00")


def _static_candidate(root: Path, path: str) -> Path | None:
    """The file under ``root`` that ``path`` names, or None.

    The path is validated lexically *before* touching the filesystem: resolving or
    stat-ing an attacker-supplied UNC path (\\\\host\\share) makes Windows authenticate
    to that host and leak the user's NTLM hash, and any web page can make the browser
    send such a GET to 127.0.0.1.
    """
    if not path or any(ch in _UNSAFE_PATH_CHARS for ch in path):
        return None
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    candidate = root.joinpath(*parts)
    try:
        # Lexically inside dist/ already; resolve() only guards against symlinks.
        if candidate.is_file() and candidate.resolve().is_relative_to(root):
            return candidate
    except (OSError, ValueError):
        pass
    return None

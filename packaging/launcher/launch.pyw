"""FinTrack launcher: the Desktop / Start Menu shortcut runs ``python\\pythonw.exe launch.pyw``.

Windows glue only (named mutex, focusing the open window, the error message box); the logic
is in launcher_core.py. No console window ever appears: this runs under pythonw.exe and
starts the server with pythonw.exe and CREATE_NO_WINDOW.

Diagnostics / tests (not used by the shortcuts):
    --no-ui          never open a window or a message box (errors go to the log only)
    --ports A-B      use ports A..B instead of 8000-8010
"""
from __future__ import annotations

import hashlib
import logging
import logging.handlers
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:  # the embeddable Python's ._pth doesn't add the script folder
    sys.path.insert(0, str(HERE))

import launcher_core as core  # noqa: E402

log = logging.getLogger("fintrack.launcher")

if sys.stdout is None or sys.stderr is None:  # pythonw: nowhere to print; keep writes harmless
    _null = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    sys.stdout = sys.stdout or _null
    sys.stderr = sys.stderr or _null

MUTEX_BASE = "Local\\FinTrack-Launcher"

MB_OK = 0x0
MB_ICONERROR = 0x10
MB_SETFOREGROUND = 0x10000
MB_TOPMOST = 0x40000
WAIT_OBJECT_0 = 0x0
WAIT_ABANDONED = 0x80
SW_RESTORE = 9
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
APP_WINDOW_CLASS = "Chrome_WidgetWin_1"
# The page title of the app window: "Iron Owl" from 2.0.0, "FinTrack" before (an older version
# can still be running while this launcher is new).
APP_WINDOW_TITLES = frozenset({"Iron Owl", "FinTrack"})


def mutex_name(home: Path) -> str:
    """The plan's name for a normal install; a distinct one when FINTRACK_HOME is overridden
    (a test copy must never block, or be blocked by, the real one)."""
    if os.environ.get("FINTRACK_HOME", "").strip():
        digest = hashlib.sha256(str(home).lower().encode("utf-8")).hexdigest()[:12]
        return f"{MUTEX_BASE}-{digest}"
    return MUTEX_BASE


class WinSystem(core.System):
    def __init__(self, name: str) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wt = wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        self._k32, self._u32 = k32, u32

        k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.ReleaseMutex.argtypes = [wintypes.HANDLE]
        k32.ReleaseMutex.restype = wintypes.BOOL
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL

        self._enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        u32.EnumWindows.argtypes = [self._enum_proc, wintypes.LPARAM]
        u32.EnumWindows.restype = wintypes.BOOL
        u32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u32.GetClassNameW.restype = ctypes.c_int
        u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u32.GetWindowTextW.restype = ctypes.c_int
        u32.IsWindowVisible.argtypes = [wintypes.HWND]
        u32.IsWindowVisible.restype = wintypes.BOOL
        u32.IsIconic.argtypes = [wintypes.HWND]
        u32.IsIconic.restype = wintypes.BOOL
        u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u32.ShowWindow.restype = wintypes.BOOL
        u32.SetForegroundWindow.argtypes = [wintypes.HWND]
        u32.SetForegroundWindow.restype = wintypes.BOOL
        u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u32.GetWindowThreadProcessId.restype = wintypes.DWORD
        u32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
        u32.MessageBoxW.restype = ctypes.c_int

        self._mutex = k32.CreateMutexW(None, False, name)
        self._owned = False

    # --- single launcher at a time

    def acquire(self, timeout: float) -> bool:
        if not self._mutex:
            return True  # can't create the mutex: better to start than to do nothing
        if self._owned:
            return True
        result = self._k32.WaitForSingleObject(self._mutex, int(max(0.0, timeout) * 1000))
        self._owned = result in (WAIT_OBJECT_0, WAIT_ABANDONED)
        return self._owned

    def release(self) -> None:
        if self._mutex and self._owned:
            self._k32.ReleaseMutex(self._mutex)
            self._owned = False

    # --- the open app window

    def _process_image(self, hwnd: int) -> str:
        pid = self._wt.DWORD(0)
        self._u32.GetWindowThreadProcessId(hwnd, self._ctypes.byref(pid))
        handle = self._k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not handle:
            return ""
        try:
            size = self._wt.DWORD(1024)
            buf = self._ctypes.create_unicode_buffer(size.value)
            if self._k32.QueryFullProcessImageNameW(handle, 0, buf, self._ctypes.byref(size)):
                return buf.value
            return ""
        finally:
            self._k32.CloseHandle(handle)

    def find_app_window(self) -> int | None:
        found: list[int] = []
        ctypes = self._ctypes

        def visit(hwnd: int, _lparam: int) -> bool:
            if not self._u32.IsWindowVisible(hwnd):
                return True
            cls = ctypes.create_unicode_buffer(256)
            self._u32.GetClassNameW(hwnd, cls, 256)
            if cls.value != APP_WINDOW_CLASS:
                return True
            title = ctypes.create_unicode_buffer(512)
            self._u32.GetWindowTextW(hwnd, title, 512)
            # An --app window's title is the page title alone; browser windows add
            # " - Microsoft Edge" etc., so an exact match skips ordinary tabs.
            if title.value not in APP_WINDOW_TITLES:
                return True
            image = self._process_image(hwnd).lower()
            if image and not image.endswith("msedge.exe"):
                return True
            found.append(hwnd)
            return False

        self._u32.EnumWindows(self._enum_proc(visit), 0)
        return found[0] if found else None

    def focus_window(self) -> bool:
        try:
            hwnd = self.find_app_window()
        except Exception as exc:  # noqa: BLE001
            log.warning("could not look for the window (%s)", type(exc).__name__)
            return False
        if hwnd is None:
            return False
        if self._u32.IsIconic(hwnd):
            self._u32.ShowWindow(hwnd, SW_RESTORE)
        self._u32.SetForegroundWindow(hwnd)
        return True

    # --- the "couldn't start" box

    def show_error(self, text: str) -> None:
        self._u32.MessageBoxW(None, text, core.MESSAGE_TITLE,
                              MB_OK | MB_ICONERROR | MB_SETFOREGROUND | MB_TOPMOST)


def parse_args(argv: list[str]) -> core.Options:
    options = core.Options()
    args = list(argv)
    while args:
        arg = args.pop(0)
        if arg == "--no-ui":
            options.ui = False
        elif arg == "--ports" and args:
            first, _, last = args.pop(0).partition("-")
            options.port_first, options.port_last = int(first), int(last or first)
            if not (1024 <= options.port_first <= options.port_last <= 65535):
                raise ValueError("bad --ports")
        elif arg == "--test-downloads-dir" and args:
            # Release drills only: the updater scans this folder instead of Downloads.
            options.test_downloads_dir = args.pop(0)
    return options


def setup_logging(log_dir: Path) -> None:
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            log_dir / "launcher.log", maxBytes=256 * 1024, backupCount=2, encoding="utf-8")
    except OSError:
        handler = logging.NullHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)


def _contact_for_crash(paths: core.Paths) -> str:
    try:
        saved = core.read_saved_contact(paths)
        if saved is not None:
            return saved
        return core.clean_contact(core.read_json(paths.state_file).get("support_contact"))
    except Exception:  # noqa: BLE001
        return ""


def main(argv: list[str]) -> int:
    try:
        options = parse_args(argv)
    except ValueError:
        return 2
    try:
        home = core.default_home(os.environ)
    except core.StateError:
        if options.ui:
            WinSystem(MUTEX_BASE).show_error(core.error_message(""))
        return 1
    paths = core.Paths(install_root=HERE, home=home)
    setup_logging(paths.logs)
    system = WinSystem(mutex_name(home))
    if not system.acquire(0):
        log.info("another launcher is already opening FinTrack")
        return 0
    launcher = core.Launcher(paths, system, options)
    try:
        return launcher.launch()
    except Exception:  # noqa: BLE001
        log.exception("launcher failed")
        launcher.contact = launcher.contact or _contact_for_crash(paths)
        launcher.fail(core.FT_START_01)
        return 1
    finally:
        system.release()
        logging.shutdown()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

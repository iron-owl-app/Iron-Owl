"""Settings (D7): "Change folder…" for automatic backups opens the native Windows folder picker.

The server and the window run on the same PC (loopback only), so the server shows the
dialog itself: COM ``IFileOpenDialog`` with ``FOS_PICKFOLDERS``, on a fresh worker thread
(its own single-threaded apartment), owned by the foreground window where possible.

- One picker at a time (``PickerBusy`` -> 409 ``busy``); off Windows ``PickerUnavailable``
  (-> 501 ``unavailable``). The route requires an unlocked session before and after.
- The folder chosen is only returned: ``PUT /api/backup/auto`` checks it (local, writable,
  not a network drive, outside data/) before it is ever used.
- Every lock (manual, idle, shutdown) closes an open picker (``FolderPicker.close``: WM_CLOSE
  to the dialog window of the worker thread), so it can't outlive the session or hold up
  the server's exit.
- Tests never show a real dialog: ``FolderPicker(dialog=...)`` or monkeypatching
  ``native_dialog`` replaces it (tests/conftest.py blocks the real one, and replaces
  ``close_thread_windows``).
"""
from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Callable

log = logging.getLogger("fintrack.folder_picker")

TITLE = "Choose a folder for automatic backups"
OK_LABEL = "Use this folder"


class PickerBusy(Exception):
    """A folder picker is already open."""


class PickerUnavailable(Exception):
    """No native folder picker here (not Windows)."""


class PickerFailed(Exception):
    """The dialog could not be shown (the COM error code only, never a path)."""


def available() -> bool:
    """Whether the native picker exists here (tests monkeypatch this)."""
    return sys.platform == "win32"


# ------------------------------------------------------------------ the COM dialog (Windows)

# IFileDialog options (shobjidl_core.h).
FOS_NOCHANGEDIR = 0x00000008  # never change the server process's working directory
FOS_PICKFOLDERS = 0x00000020
FOS_FORCEFILESYSTEM = 0x00000040  # real file-system folders only (no libraries or "This PC")
FOS_PATHMUSTEXIST = 0x00000800
FOS_DONTADDTORECENT = 0x02000000
OPTIONS = FOS_NOCHANGEDIR | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST | FOS_DONTADDTORECENT

SIGDN_FILESYSPATH = 0x80058000
COINIT_APARTMENTTHREADED = 0x2
COINIT_DISABLE_OLE1DDE = 0x4
CLSCTX_INPROC_SERVER = 0x1
S_OK, S_FALSE = 0, 1
RPC_E_CHANGED_MODE = -2147417850  # 0x80010106
HRESULT_CANCELLED = -2147023673  # HRESULT_FROM_WIN32(ERROR_CANCELLED) = 0x800704C7

CLSID_FILE_OPEN_DIALOG = "{DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7}"
IID_IFILE_OPEN_DIALOG = "{D57C7288-D4AD-4768-BE02-9D969532D960}"

# vtable slots: IUnknown 0-2, IModalWindow::Show 3, IFileDialog 4-26.
_RELEASE, _SHOW, _SET_OPTIONS, _GET_OPTIONS, _SET_TITLE, _SET_OK_LABEL, _GET_RESULT = 2, 3, 9, 10, 17, 18, 20
_ITEM_GET_DISPLAY_NAME = 5  # IShellItem::GetDisplayName


def native_dialog() -> str | None:  # pragma: no cover - needs a Windows desktop
    """Show the folder picker on this thread; the folder's path, or None when cancelled.

    Must run on a thread without COM initialized in another mode (``FolderPicker`` starts a
    fresh one). Raises ``PickerFailed`` with the HRESULT only.
    """
    import ctypes
    from ctypes import wintypes

    ole32 = ctypes.OleDLL("ole32")  # OleDLL: failed HRESULTs raise OSError
    ole32_void = ctypes.WinDLL("ole32")  # CoTaskMemFree / CoUninitialize return nothing
    ole32_void.CoTaskMemFree.restype = None
    ole32_void.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole32_void.CoUninitialize.restype = None
    user32 = ctypes.WinDLL("user32")
    user32.GetForegroundWindow.restype = wintypes.HWND

    class GUID(ctypes.Structure):
        _fields_ = [("d1", ctypes.c_ulong), ("d2", ctypes.c_ushort), ("d3", ctypes.c_ushort),
                    ("d4", ctypes.c_ubyte * 8)]

    def guid(text: str) -> GUID:
        g = GUID()
        ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(g))
        return g

    def call(ptr: ctypes.c_void_p, slot: int, *args: tuple[object, object]) -> int:
        """Call COM method ``slot`` of ``ptr``; returns the HRESULT (never raises for it)."""
        vtable = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *(t for t, _ in args))
        return int(proto(vtable[slot])(ptr, *(v for _, v in args)))

    def check(hr: int) -> None:
        if hr < 0:
            raise PickerFailed(f"0x{hr & 0xFFFFFFFF:08X}")

    try:
        ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE)
    except OSError as exc:
        raise PickerFailed(f"0x{(exc.winerror or 0) & 0xFFFFFFFF:08X}") from None
    dialog = ctypes.c_void_p()
    item = ctypes.c_void_p()
    try:
        try:
            ole32.CoCreateInstance(
                ctypes.byref(guid(CLSID_FILE_OPEN_DIALOG)), None, CLSCTX_INPROC_SERVER,
                ctypes.byref(guid(IID_IFILE_OPEN_DIALOG)), ctypes.byref(dialog),
            )
        except OSError as exc:
            raise PickerFailed(f"0x{(exc.winerror or 0) & 0xFFFFFFFF:08X}") from None
        current = ctypes.c_uint(0)
        check(call(dialog, _GET_OPTIONS, (ctypes.POINTER(ctypes.c_uint), ctypes.byref(current))))
        check(call(dialog, _SET_OPTIONS, (ctypes.c_uint, current.value | OPTIONS)))
        call(dialog, _SET_TITLE, (ctypes.c_wchar_p, TITLE))  # cosmetic: failures ignored
        call(dialog, _SET_OK_LABEL, (ctypes.c_wchar_p, OK_LABEL))
        owner = user32.GetForegroundWindow()
        hr = call(dialog, _SHOW, (wintypes.HWND, owner))
        if hr == HRESULT_CANCELLED:
            return None
        check(hr)
        check(call(dialog, _GET_RESULT, (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(item))))
        name = ctypes.c_void_p()
        check(call(item, _ITEM_GET_DISPLAY_NAME, (ctypes.c_uint, SIGDN_FILESYSPATH),
                   (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(name))))
        try:
            path = ctypes.wstring_at(name.value) if name.value else None
        finally:
            ole32_void.CoTaskMemFree(name)
        return path or None
    finally:
        if item.value:
            call(item, _RELEASE)
        if dialog.value:
            call(dialog, _RELEASE)
        ole32_void.CoUninitialize()


WM_CLOSE = 0x0010
DIALOG_CLASS = "#32770"  # the common item dialog's top-level window class


def close_thread_windows(thread_id: int) -> int:  # pragma: no cover - needs a Windows desktop
    """Post WM_CLOSE to the visible dialog windows (class ``#32770``) of ``thread_id``; the
    folder picker then returns as cancelled. Only posts (never waits); returns how many."""
    if sys.platform != "win32":
        return 0
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32")
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumThreadWindows.argtypes = [wintypes.DWORD, enum_proc, wintypes.LPARAM]
    user32.EnumThreadWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL
    found: list[int] = []

    def each(hwnd: int, _: int) -> bool:
        name = ctypes.create_unicode_buffer(64)
        if user32.IsWindowVisible(hwnd) and user32.GetClassNameW(hwnd, name, 64) and name.value == DIALOG_CLASS:
            found.append(hwnd)
        return True

    user32.EnumThreadWindows(thread_id, enum_proc(each), 0)
    return sum(1 for hwnd in found if user32.PostMessageW(hwnd, WM_CLOSE, 0, 0))


# ------------------------------------------------------------------ one picker at a time


class FolderPicker:
    """Runs the dialog on its own worker thread, one at a time."""

    def __init__(self, dialog: Callable[[], str | None] | None = None) -> None:
        # None: the module's ``native_dialog``, looked up at call time (tests patch it).
        self.dialog = dialog
        # None: the module's ``close_thread_windows``, looked up at call time.
        self.closer: Callable[[int], object] | None = None
        self._busy = threading.Lock()
        self._guard = threading.Lock()
        self._worker_id: int | None = None  # native id of the thread showing the dialog

    @property
    def busy(self) -> bool:
        return self._busy.locked()

    def close(self) -> bool:
        """Close the open picker, if any (every lock, including shutdown). Never raises.

        The dialog belongs to the worker thread, so this only posts WM_CLOSE to that
        thread's dialog window; ``pick`` then returns as cancelled.
        """
        with self._guard:
            worker_id = self._worker_id
        if worker_id is None:
            return False
        try:
            (self.closer or close_thread_windows)(worker_id)
        except Exception as exc:  # noqa: BLE001 - locking must never fail because of it
            log.error("could not close the folder picker: %s", type(exc).__name__)
            return False
        return True

    def pick(self) -> str | None:
        """The folder chosen, or None when cancelled. Blocks until the window closes.

        Raises PickerUnavailable (not Windows), PickerBusy (one is open) or PickerFailed.
        """
        if not available():
            raise PickerUnavailable()
        if not self._busy.acquire(blocking=False):
            raise PickerBusy()
        try:
            dialog = self.dialog or native_dialog
            result: dict[str, object] = {}

            def run() -> None:
                with self._guard:
                    self._worker_id = threading.get_native_id()
                try:
                    result["dir"] = dialog()
                except BaseException as exc:  # noqa: BLE001 - handed back to the caller
                    result["error"] = exc
                finally:
                    with self._guard:
                        self._worker_id = None

            # A fresh thread: COM is initialized there as a single-threaded apartment, never
            # on a shared worker whose COM mode someone else chose.
            worker = threading.Thread(target=run, name="fintrack-folder-picker", daemon=True)
            worker.start()
            worker.join()
            error = result.get("error")
            if isinstance(error, PickerFailed):
                raise error
            if error is not None:
                log.error("folder picker failed: %s", type(error).__name__)
                raise PickerFailed(type(error).__name__)
            chosen = result.get("dir")
            if chosen is None:
                return None
            if not isinstance(chosen, str):
                raise PickerFailed("not text")
            chosen = chosen.strip()
            return chosen or None
        finally:
            self._busy.release()

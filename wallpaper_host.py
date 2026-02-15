"""
Wallpaper Sensi — Desktop Host Module  (v2 — Performance-Optimised)
====================================================================
Injects a transparent PyQt6 window behind Windows desktop icons using the
Lively Wallpaper / WorkerW algorithm.

Performance features
--------------------
• **Event-driven idle** — Qt message pump (GetMessage) → 0 % CPU at rest.
• **SetWinEventHook** — listens for EVENT_SYSTEM_FOREGROUND to detect
  full-screen apps and auto-pause/resume rendering (Smart Pause).
• **QPixmap cache** — paintEvent only re-renders when size changes or
  after resuming from pause.  Static wallpapers cost zero GPU.
• **Handle caching** — Progman, WorkerW, Shell_TrayWnd resolved once.

Dependencies: PyQt6 (pip install PyQt6)
Win32 layer:  ctypes only — no pywin32 required.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import sys
import threading
from typing import Callable, Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt6.QtGui import QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QWidget

# ── Optional: WebEngine for HTML wallpapers ─────────────────
try:
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    from PyQt6.QtWebEngineCore import QWebEngineSettings
    from PyQt6.QtCore import QUrl
    HAS_WEBENGINE = True
except ImportError:
    HAS_WEBENGINE = False

# ─────────────────────────────────────────────────────────────
#  Win32 constants
# ─────────────────────────────────────────────────────────────
SMTO_NORMAL = 0x0000
GWL_EXSTYLE = -20
GWL_STYLE   = -16

WS_CHILD        = 0x40000000
WS_CLIPCHILDREN = 0x02000000
WS_CLIPSIBLINGS = 0x04000000

WS_EX_NOACTIVATE  = 0x08000000
WS_EX_TOOLWINDOW  = 0x00000080
WS_EX_LAYERED     = 0x00080000
WS_EX_TRANSPARENT = 0x00000020

SWP_NOACTIVATE = 0x0010
SWP_NOMOVE     = 0x0002
SWP_NOSIZE     = 0x0001
SWP_SHOWWINDOW = 0x0040
SWP_NOZORDER   = 0x0004

HWND_BOTTOM = ctypes.c_void_p(1)

DWMWA_TRANSITIONS_FORCEDISABLED = ctypes.c_uint(3)

SM_CXSCREEN = 0
SM_CYSCREEN = 1

# WinEvent constants
EVENT_SYSTEM_FOREGROUND = 0x0003
WINEVENT_OUTOFCONTEXT   = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002

# Monitor info
MONITOR_DEFAULTTONEAREST = 0x00000002

# ─────────────────────────────────────────────────────────────
#  Win32 function prototypes  (ctypes — zero extra dependencies)
# ─────────────────────────────────────────────────────────────
user32   = ctypes.windll.user32
dwmapi   = ctypes.windll.dwmapi
kernel32 = ctypes.windll.kernel32

FindWindowW         = user32.FindWindowW
FindWindowExW       = user32.FindWindowExW
EnumWindows         = user32.EnumWindows
GetWindow           = user32.GetWindow
GetClassNameW       = user32.GetClassNameW
SetParent           = user32.SetParent
SetWindowPos        = user32.SetWindowPos
SetWindowLongPtrW   = user32.SetWindowLongPtrW
GetWindowLongPtrW   = user32.GetWindowLongPtrW
ShowWindow          = user32.ShowWindow
SendMessageTimeoutW = user32.SendMessageTimeoutW
GetSystemMetrics    = user32.GetSystemMetrics
IsWindow            = user32.IsWindow
GetDesktopWindow    = user32.GetDesktopWindow
GetForegroundWindow = user32.GetForegroundWindow
GetWindowRect       = user32.GetWindowRect
MonitorFromWindow   = user32.MonitorFromWindow
GetMonitorInfoW     = user32.GetMonitorInfoW
SetWinEventHook     = user32.SetWinEventHook
UnhookWinEvent      = user32.UnhookWinEvent
GetMessageW         = user32.GetMessageW
TranslateMessage    = user32.TranslateMessage
DispatchMessageW    = user32.DispatchMessageW

DwmSetWindowAttribute = dwmapi.DwmSetWindowAttribute

# ── Callback types ──────────────────────────────────────────
WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
WINEVENTPROC = ctypes.WINFUNCTYPE(
    None,                   # return void
    wt.HANDLE,              # hWinEventHook
    wt.DWORD,               # event
    wt.HWND,                # hwnd
    ctypes.c_long,          # idObject
    ctypes.c_long,          # idChild
    wt.DWORD,               # dwEventThread
    wt.DWORD,               # dwmsEventTime
)

# ── Arg / res types ────────────────────────────────────────
FindWindowW.argtypes        = [wt.LPCWSTR, wt.LPCWSTR]
FindWindowW.restype         = wt.HWND
FindWindowExW.argtypes      = [wt.HWND, wt.HWND, wt.LPCWSTR, wt.LPCWSTR]
FindWindowExW.restype       = wt.HWND
EnumWindows.argtypes        = [WNDENUMPROC, wt.LPARAM]
EnumWindows.restype         = wt.BOOL
GetWindow.argtypes          = [wt.HWND, wt.UINT]
GetWindow.restype           = wt.HWND
GetClassNameW.argtypes      = [wt.HWND, wt.LPWSTR, ctypes.c_int]
GetClassNameW.restype       = ctypes.c_int
SetParent.argtypes          = [wt.HWND, wt.HWND]
SetParent.restype           = wt.HWND
SetWindowLongPtrW.argtypes  = [wt.HWND, ctypes.c_int, ctypes.c_longlong]
SetWindowLongPtrW.restype   = ctypes.c_longlong
GetWindowLongPtrW.argtypes  = [wt.HWND, ctypes.c_int]
GetWindowLongPtrW.restype   = ctypes.c_longlong
ShowWindow.argtypes         = [wt.HWND, ctypes.c_int]
ShowWindow.restype          = wt.BOOL
IsWindow.argtypes           = [wt.HWND]
IsWindow.restype            = wt.BOOL
GetForegroundWindow.argtypes = []
GetForegroundWindow.restype  = wt.HWND
GetWindowRect.argtypes       = [wt.HWND, ctypes.POINTER(wt.RECT)]
GetWindowRect.restype        = wt.BOOL
MonitorFromWindow.argtypes   = [wt.HWND, wt.DWORD]
MonitorFromWindow.restype    = wt.HANDLE
SetWinEventHook.argtypes = [
    wt.UINT, wt.UINT, wt.HMODULE, WINEVENTPROC,
    wt.DWORD, wt.DWORD, wt.UINT,
]
SetWinEventHook.restype  = wt.HANDLE
UnhookWinEvent.argtypes  = [wt.HANDLE]
UnhookWinEvent.restype   = wt.BOOL

SendMessageTimeoutW.argtypes = [
    wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM,
    wt.UINT, wt.UINT, ctypes.POINTER(ctypes.c_ulong),
]
SendMessageTimeoutW.restype = wt.LPARAM

SetWindowPos.argtypes = [
    wt.HWND, ctypes.c_void_p,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wt.UINT,
]
SetWindowPos.restype = wt.BOOL

DwmSetWindowAttribute.argtypes = [
    wt.HWND, wt.DWORD, ctypes.POINTER(ctypes.c_int), wt.DWORD,
]
DwmSetWindowAttribute.restype = ctypes.c_long

# ── MONITORINFO struct ──────────────────────────────────────
class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize",    wt.DWORD),
        ("rcMonitor", wt.RECT),
        ("rcWork",    wt.RECT),
        ("dwFlags",   wt.DWORD),
    ]

GetMonitorInfoW.argtypes = [wt.HANDLE, ctypes.POINTER(MONITORINFO)]
GetMonitorInfoW.restype  = wt.BOOL

log = logging.getLogger("WallpaperSensei")

# ─────────────────────────────────────────────────────────────
#  Helper: get class name of a HWND
# ─────────────────────────────────────────────────────────────
def _class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    GetClassNameW(hwnd, buf, 256)
    return buf.value


def _is_fullscreen(hwnd: int) -> bool:
    """
    Return True if *hwnd* covers the entire monitor it's on.
    Excludes the desktop shell itself (Progman / WorkerW / taskbar).
    """
    if not hwnd or not IsWindow(hwnd):
        return False

    cls = _class_name(hwnd)
    # Ignore the desktop shell and taskbar
    if cls in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
        return False

    # Get the window rect
    wr = wt.RECT()
    if not GetWindowRect(hwnd, ctypes.byref(wr)):
        return False

    # Get the monitor rect
    hmon = MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if not GetMonitorInfoW(hmon, ctypes.byref(mi)):
        return False

    mr = mi.rcMonitor
    return (
        wr.left   <= mr.left
        and wr.top    <= mr.top
        and wr.right  >= mr.right
        and wr.bottom >= mr.bottom
    )


# ═════════════════════════════════════════════════════════════
#  _ForegroundBridge — thread-safe Qt signal from WinEvent hook
# ═════════════════════════════════════════════════════════════
class _ForegroundBridge(QObject):
    """
    Bridges the SetWinEventHook callback (which fires on a background
    thread's message pump) into the Qt main thread via a signal.
    """
    foreground_changed = pyqtSignal(int)  # emits the new foreground HWND


# ═════════════════════════════════════════════════════════════
#  WallpaperHost — the core desktop host class
# ═════════════════════════════════════════════════════════════
class WallpaperHost:
    """
    Manages the full lifecycle of a wallpaper surface embedded behind
    desktop icons via the WorkerW injection technique.

    Usage
    -----
    >>> app = QApplication(sys.argv)
    >>> host = WallpaperHost()
    >>> host.start()          # injects & docks
    >>> host.set_paint_callback(my_painter_fn)
    >>> app.exec()            # event-driven — 0 % CPU at idle
    >>> host.stop()           # clean exit
    """

    def __init__(self) -> None:
        # ── Cached handles (populated once in start()) ──────
        self._progman:  Optional[int] = None
        self._workerw:  Optional[int] = None
        self._tray:     Optional[int] = None

        # ── Qt surface ──────────────────────────────────────
        self._widget: Optional[_HostSurface] = None

        # ── State flags ─────────────────────────────────────
        self._docked: bool  = False
        self._active: bool  = True   # rendering state
        self._smart_paused: bool = False  # True when auto-paused by fullscreen

        # ── User-supplied paint callback ────────────────────
        self._paint_cb: Optional[Callable[[QPainter, int, int], None]] = None

        # ── WinEvent hook (foreground listener) ─────────────
        self._hook_handle: Optional[int] = None
        self._hook_thread: Optional[threading.Thread] = None
        self._hook_stop_event = threading.Event()
        self._bridge: Optional[_ForegroundBridge] = None
        # prevent GC of the callback
        self._winevent_cb: Optional[WINEVENTPROC] = None

        # ── WebEngine view (HTML wallpapers) ─────────────────
        self._web_view: Optional['QWebEngineView'] = None

    # ─────────────────────────────────────────────────────────
    #  Public API
    # ─────────────────────────────────────────────────────────
    def start(self) -> bool:
        """
        Full injection sequence:
          1. Spawn the WorkerW layer  (Progman 0x052C)
          2. Find the correct WorkerW handle
          3. Cache Shell_TrayWnd
          4. Create the Qt host surface
          5. Dock into WorkerW via SetParent
          6. Harden against flicker
          7. Install foreground event hook (Smart Pause)

        Returns True on success.
        """
        if self._docked:
            log.warning("start() called but already docked — skipping.")
            return True

        # Step 1 — locate Progman & spawn WorkerW
        self._progman = FindWindowW("Progman", None)
        if not self._progman:
            log.error("Could not find Progman window.")
            return False
        log.info("Progman handle: 0x%X", self._progman)

        self._send_spawn_workerw()

        # Step 2 — find the correct WorkerW
        self._workerw = self._find_workerw()
        if not self._workerw:
            log.error("Could not locate WorkerW handle.")
            return False
        log.info("WorkerW handle: 0x%X", self._workerw)

        # Step 3 — cache Shell_TrayWnd (taskbar)
        self._tray = FindWindowW("Shell_TrayWnd", None)
        if self._tray:
            log.info("Shell_TrayWnd handle: 0x%X", self._tray)

        # Step 4 — create the Qt surface
        w = GetSystemMetrics(SM_CXSCREEN)
        h = GetSystemMetrics(SM_CYSCREEN)
        self._widget = _HostSurface(w, h, self._on_paint)
        self._widget.show()

        # Step 5 — dock
        hwnd = int(self._widget.winId())
        self._dock(hwnd)

        # Step 6 — harden
        self._harden(hwnd)

        # Step 7 — install SetWinEventHook for smart pause
        self._install_foreground_hook()

        self._docked = True
        log.info("Desktop host docked successfully (%d×%d).", w, h)
        return True

    def stop(self) -> None:
        """
        Clean exit: unhook → unparent → hide → destroy widget → restore.
        Idempotent — safe to call multiple times.
        """
        if not self._docked and self._widget is None:
            return

        # Unhook the foreground event
        self._uninstall_foreground_hook()

        if self._widget is not None:
            # Clean up web content before destroying the widget
            self.unload_web_content()

            hwnd = int(self._widget.winId())

            # Unparent back to the desktop root
            try:
                desktop = GetDesktopWindow()
                SetParent(hwnd, desktop)
                log.info("Unparented host window from WorkerW.")
            except Exception:
                pass

            # Hide & destroy
            ShowWindow(hwnd, 0)  # SW_HIDE
            self._widget.close()
            self._widget.deleteLater()
            self._widget = None

        self._docked       = False
        self._smart_paused = False
        self._progman      = None
        self._workerw      = None
        self._tray         = None
        log.info("Desktop host stopped — original desktop restored.")

    def set_paint_callback(
        self, cb: Callable[[QPainter, int, int], None]
    ) -> None:
        """
        Register a callable ``cb(painter, width, height)`` that will be
        invoked every time the host surface repaints.
        Setting a new callback invalidates the pixmap cache so the next
        paint re-renders.
        """
        self._paint_cb = cb
        if self._widget is not None:
            self._widget.invalidate_cache()
            self._widget.update()

    def set_rendering_state(self, active: bool) -> None:
        """
        Toggle rendering on / off.

        When *active* is ``False``:
          • The Qt repaint timer is stopped.
          • The widget content is frozen (cached pixmap used).
          • GPU usage drops to zero.

        When *active* is ``True``:
          • The repaint timer resumes.
          • The pixmap cache is invalidated → next frame re-renders.
        """
        if active == self._active:
            return
        self._active = active

        if self._widget is not None:
            if active:
                self._widget.resume()
                log.info("Rendering resumed.")
            else:
                self._widget.suspend()
                log.info("Rendering suspended — GPU idle.")

    @property
    def is_docked(self) -> bool:
        """True if the host is currently parented inside WorkerW."""
        return self._docked

    @property
    def is_rendering(self) -> bool:
        """True if the host surface is actively refreshing."""
        return self._active

    @property
    def is_smart_paused(self) -> bool:
        """True if auto-paused because a fullscreen app is in foreground."""
        return self._smart_paused

    @property
    def host_hwnd(self) -> Optional[int]:
        """Return the native HWND of the Qt surface, or None."""
        if self._widget is not None:
            return int(self._widget.winId())
        return None

    @property
    def workerw_hwnd(self) -> Optional[int]:
        """Return the cached WorkerW handle."""
        return self._workerw

    # ─────────────────────────────────────────────────────────
    #  WebEngine — HTML wallpaper support
    # ─────────────────────────────────────────────────────────
    def load_web_content(self, url) -> bool:
        """
        Load an HTML wallpaper via QWebEngineView embedded in the host
        surface.  GPU acceleration is enabled automatically.

        Parameters
        ----------
        url : QUrl
            URL to the wallpaper's ``index.html``.
            Use ``QUrl.fromLocalFile(path)`` for local files.

        Returns True on success.
        """
        if not HAS_WEBENGINE:
            log.error(
                "PyQt6-WebEngine is not installed — cannot load HTML wallpaper."
            )
            return False

        if self._widget is None:
            log.error("Host surface not created — call start() first.")
            return False

        # Tear down any previous web content
        self.unload_web_content()

        wv = QWebEngineView(self._widget)
        wv.setGeometry(0, 0, self._widget.width(), self._widget.height())

        # ── GPU-accelerated rendering ───────────────────────
        s = wv.settings()
        s.setAttribute(
            QWebEngineSettings.WebAttribute.Accelerated2dCanvasEnabled, True
        )
        s.setAttribute(
            QWebEngineSettings.WebAttribute.WebGLEnabled, True
        )
        s.setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls,
            False,
        )
        s.setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls,
            True,
        )
        s.setAttribute(
            QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture,
            False,
        )

        # Prevent focus stealing and right-click menus
        wv.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        wv.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)

        # Hide scrollbars after the page finishes loading
        wv.loadFinished.connect(
            lambda ok: wv.page().runJavaScript(
                "document.documentElement.style.overflow='hidden';"
            )
            if ok
            else None
        )

        wv.setUrl(url)
        wv.show()

        self._web_view = wv
        log.info("WebEngine loaded: %s", url.toString())
        return True

    def unload_web_content(self) -> None:
        """Remove the QWebEngineView if present.  Idempotent."""
        if self._web_view is not None:
            self._web_view.stop()
            self._web_view.setParent(None)
            self._web_view.deleteLater()
            self._web_view = None
            log.info("WebEngine content unloaded.")

    # ─────────────────────────────────────────────────────────
    #  Internals — WorkerW injection
    # ─────────────────────────────────────────────────────────
    def _send_spawn_workerw(self) -> None:
        """
        Send the undocumented 0x052C message to Progman.
        This forces Windows to create a WorkerW layer between the
        wallpaper bitmap and the desktop icons (SHELLDLL_DefView).
        """
        result = ctypes.c_ulong(0)
        SendMessageTimeoutW(
            self._progman,
            0x052C,        # undocumented "spawn WorkerW" message
            0xD,           # wParam
            0x1,           # lParam
            SMTO_NORMAL,
            1000,          # timeout ms
            ctypes.byref(result),
        )
        log.debug("Sent 0x052C to Progman — result: %d", result.value)

    def _find_workerw(self) -> Optional[int]:
        """
        Walk top-level windows to find the WorkerW that is the *sibling*
        of SHELLDLL_DefView.  This is the window we parent into.

        Algorithm (matches Lively Wallpaper):
          For each top-level window W:
            If class(W) == "WorkerW":
              Check if W has a child "SHELLDLL_DefView".
              If YES → walk to the next sibling WorkerW → that's ours.

        Strategy 2 is a 24H2 fallback.
        """
        target: list[int] = [0]

        # Strategy 1 — sibling of the WorkerW that owns SHELLDLL_DefView.
        def _enum_cb(hwnd: int, _lparam: int) -> bool:
            if _class_name(hwnd) != "WorkerW":
                return True

            child = FindWindowExW(hwnd, None, "SHELLDLL_DefView", None)
            if child:
                GW_HWNDNEXT = 2
                sibling = GetWindow(hwnd, GW_HWNDNEXT)
                while sibling:
                    if _class_name(sibling) == "WorkerW":
                        target[0] = sibling
                        return False
                    sibling = GetWindow(sibling, GW_HWNDNEXT)
            return True

        EnumWindows(WNDENUMPROC(_enum_cb), 0)

        if target[0]:
            return target[0]

        # Strategy 2 — fallback: any WorkerW without SHELLDLL_DefView.
        def _enum_fallback(hwnd: int, _lparam: int) -> bool:
            if _class_name(hwnd) != "WorkerW":
                return True
            child = FindWindowExW(hwnd, None, "SHELLDLL_DefView", None)
            if not child:
                target[0] = hwnd
                return False
            return True

        EnumWindows(WNDENUMPROC(_enum_fallback), 0)
        return target[0] if target[0] else None

    # ─────────────────────────────────────────────────────────
    #  Internals — window creation & docking
    # ─────────────────────────────────────────────────────────
    def _dock(self, hwnd: int) -> None:
        """Parent the Qt window into WorkerW and set correct Z-order."""
        ex_style = GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        ex_style |= WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
        ex_style &= ~WS_EX_TRANSPARENT
        SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex_style)

        style = GetWindowLongPtrW(hwnd, GWL_STYLE)
        style |= WS_CHILD | WS_CLIPCHILDREN | WS_CLIPSIBLINGS
        SetWindowLongPtrW(hwnd, GWL_STYLE, style)

        SetParent(hwnd, self._workerw)

        w = GetSystemMetrics(SM_CXSCREEN)
        h = GetSystemMetrics(SM_CYSCREEN)
        SetWindowPos(
            hwnd, HWND_BOTTOM,
            0, 0, w, h,
            SWP_NOACTIVATE | SWP_SHOWWINDOW,
        )
        log.debug("Docked HWND 0x%X into WorkerW 0x%X.", hwnd, self._workerw)

    def _harden(self, hwnd: int) -> None:
        """
        Anti-flicker hardening:
          • Disable DWM fade transitions on this window.
          • Uses ctypes.byref(c_int(1)) with correctly typed argtypes.
        """
        value = ctypes.c_int(1)  # TRUE — disable transitions
        hr = DwmSetWindowAttribute(
            hwnd,
            DWMWA_TRANSITIONS_FORCEDISABLED,
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
        if hr == 0:
            log.debug("DWM transitions disabled for HWND 0x%X.", hwnd)
        else:
            log.warning("DwmSetWindowAttribute HRESULT 0x%08X.", hr & 0xFFFFFFFF)

    # ─────────────────────────────────────────────────────────
    #  Internals — Smart Pause (SetWinEventHook)
    # ─────────────────────────────────────────────────────────
    def _install_foreground_hook(self) -> None:
        """
        Install a SetWinEventHook for EVENT_SYSTEM_FOREGROUND.
        The hook fires on a dedicated background thread that runs its own
        Win32 message pump (GetMessage loop).  The callback posts a Qt
        signal to the main thread, where _on_foreground_changed() decides
        whether to pause or resume rendering.
        """
        self._bridge = _ForegroundBridge()
        self._bridge.foreground_changed.connect(self._on_foreground_changed)

        self._hook_stop_event.clear()

        def _hook_thread_fn():
            """Background thread: installs hook + pumps messages."""
            def _win_event_callback(
                hWinEventHook, event, hwnd, idObject,
                idChild, dwEventThread, dwmsEventTime,
            ):
                # Fire the Qt signal (thread-safe crossing via queued conn.)
                if self._bridge is not None:
                    self._bridge.foreground_changed.emit(int(hwnd))

            # prevent GC of the callback
            self._winevent_cb = WINEVENTPROC(_win_event_callback)

            self._hook_handle = SetWinEventHook(
                EVENT_SYSTEM_FOREGROUND,
                EVENT_SYSTEM_FOREGROUND,
                None,
                self._winevent_cb,
                0, 0,
                WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS,
            )

            if not self._hook_handle:
                log.error("SetWinEventHook failed.")
                return

            log.info("Foreground hook installed (handle=%s).", self._hook_handle)

            # Standard Win32 message pump — keeps the hook alive
            # and the thread at 0% CPU (GetMessage blocks).
            msg = wt.MSG()
            while not self._hook_stop_event.is_set():
                ret = GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret <= 0:
                    break
                TranslateMessage(ctypes.byref(msg))
                DispatchMessageW(ctypes.byref(msg))

        self._hook_thread = threading.Thread(
            target=_hook_thread_fn, daemon=True, name="WinEventHook",
        )
        self._hook_thread.start()

    def _uninstall_foreground_hook(self) -> None:
        """Unhook and stop the background message pump thread."""
        if self._hook_handle:
            UnhookWinEvent(self._hook_handle)
            log.info("Foreground hook uninstalled.")
            self._hook_handle = None

        self._hook_stop_event.set()

        if self._hook_thread and self._hook_thread.is_alive():
            # Post WM_QUIT (0x0012) to unblock GetMessage
            tid = self._hook_thread.ident
            if tid:
                user32.PostThreadMessageW(wt.DWORD(tid), 0x0012, 0, 0)
            self._hook_thread.join(timeout=2.0)
            self._hook_thread = None

        self._bridge    = None
        self._winevent_cb = None

    def _on_foreground_changed(self, hwnd: int) -> None:
        """
        Slot (runs on Qt main thread).
        Checks if the new foreground window is fullscreen.
        If yes → pause.  If no → resume.
        """
        if not self._docked:
            return

        fullscreen = _is_fullscreen(hwnd)

        if fullscreen and not self._smart_paused:
            # A fullscreen app just gained focus → pause
            self._smart_paused = True
            if self._widget:
                self._widget.suspend()
            log.info(
                "Smart Pause: fullscreen app detected (0x%X, %s) → paused.",
                hwnd, _class_name(hwnd),
            )

        elif not fullscreen and self._smart_paused:
            # User returned to desktop → resume
            self._smart_paused = False
            if self._widget and self._active:
                self._widget.resume()
            log.info("Smart Pause: desktop visible → resumed.")

    # ─────────────────────────────────────────────────────────
    #  Paint dispatch
    # ─────────────────────────────────────────────────────────
    def _on_paint(self, painter: QPainter, w: int, h: int) -> None:
        """Forwarded from _HostSurface.paintEvent."""
        if self._paint_cb is not None:
            self._paint_cb(painter, w, h)


# ═════════════════════════════════════════════════════════════
#  _HostSurface — internal Qt widget  (pixmap-cached)
# ═════════════════════════════════════════════════════════════
class _HostSurface(QWidget):
    """
    A frameless, transparent QWidget that acts as the wallpaper canvas.

    Rendering pipeline
    ------------------
    1. On the *first* paint (or after invalidate_cache()), the user's
       paint callback renders into a QPixmap.
    2. Subsequent paintEvent calls blit the cached pixmap — zero GPU work.
    3. When the widget is resized the cache is invalidated automatically.
    4. When suspended, paintEvent draws the last cached frame only.
    """

    _REFRESH_INTERVAL_MS = 16  # ~60 fps when active

    def __init__(
        self,
        width: int,
        height: int,
        paint_fn: Callable[[QPainter, int, int], None],
    ) -> None:
        super().__init__()

        self._paint_fn = paint_fn
        self._suspended = False

        # ── Pixmap cache ────────────────────────────────────
        self._cache: Optional[QPixmap] = None
        self._cache_size: tuple[int, int] = (0, 0)

        # ── Window flags: frameless + transparent ───────────
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnBottomHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFixedSize(width, height)
        self.move(0, 0)

        # ── Refresh timer (animation-capable, off by default) ──
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._on_tick)

    # ── Cache management ────────────────────────────────────
    def invalidate_cache(self) -> None:
        """Force a full re-render on the next paintEvent."""
        self._cache = None

    def _needs_rerender(self) -> bool:
        """True if the cache is stale or missing."""
        w, h = self.width(), self.height()
        return self._cache is None or self._cache_size != (w, h)

    # ── Suspend / Resume ────────────────────────────────────
    def suspend(self) -> None:
        """Pause all repaints — GPU goes idle."""
        self._suspended = True
        self._timer.stop()

    def resume(self) -> None:
        """Re-enable repaints.  Invalidates cache so next frame re-renders."""
        self._suspended = False
        self.invalidate_cache()
        self.update()
        # Only restart the timer if it was previously running (animation mode)
        # For static wallpapers the timer stays off → 0 % CPU.

    def start_animation(self, fps: int = 60) -> None:
        """Start the refresh timer at the given FPS."""
        interval = max(1, 1000 // fps)
        self._REFRESH_INTERVAL_MS = interval
        if not self._suspended:
            self._timer.start(interval)

    def stop_animation(self) -> None:
        """Stop the refresh timer (static content — 0 % CPU)."""
        self._timer.stop()

    # ── Timer tick ──────────────────────────────────────────
    def _on_tick(self) -> None:
        """Called by the timer.  Invalidates cache so paintEvent re-renders."""
        self.invalidate_cache()
        self.update()

    # ── Paint ───────────────────────────────────────────────
    def paintEvent(self, event) -> None:  # noqa: N802
        w, h = self.width(), self.height()

        # If suspended, blit last cached frame if available
        if self._suspended:
            if self._cache:
                p = QPainter(self)
                p.drawPixmap(0, 0, self._cache)
                p.end()
            return

        # Re-render into cache only when needed
        if self._needs_rerender():
            pix = QPixmap(w, h)
            pix.fill(Qt.GlobalColor.transparent)
            pp = QPainter(pix)
            pp.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            try:
                self._paint_fn(pp, w, h)
            finally:
                pp.end()
            self._cache = pix
            self._cache_size = (w, h)

        # Blit the cached pixmap
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self._cache)
        painter.end()

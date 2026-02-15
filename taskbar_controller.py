"""
Wallpaper Sensi — Taskbar Controller
=====================================
Modifies the Windows taskbar appearance (transparency / acrylic blur)
using the undocumented ``SetWindowCompositionAttribute`` API from user32.

Standalone module — import alongside ``WallpaperHost``.

Usage
-----
>>> from taskbar_controller import TaskbarController
>>> tb = TaskbarController()
>>> tb.update_accent(mode=2, alpha=0)      # fully transparent
>>> tb.update_accent(mode=4, alpha=127)    # acrylic at 50 % opacity
>>> tb.restore()                           # back to default Windows style

Dependencies: none (stdlib ctypes only).
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
from typing import Optional

log = logging.getLogger("WallpaperSensei.Taskbar")

# ─────────────────────────────────────────────────────────────
#  Win32 constants
# ─────────────────────────────────────────────────────────────

# AccentPolicy.AccentState values
ACCENT_DISABLED                = 0   # default / restore
ACCENT_ENABLE_GRADIENT         = 1   # solid colour behind
ACCENT_ENABLE_TRANSPARENTGRADIENT = 2   # transparent
ACCENT_ENABLE_BLURBEHIND       = 3   # blur (Win 10 style)
ACCENT_ENABLE_ACRYLICBLURBEHIND = 4   # acrylic (Win 10 1803+)
ACCENT_INVALID_STATE           = 5

# WindowCompositionAttribute enum value
WCA_ACCENT_POLICY = 19

# ─────────────────────────────────────────────────────────────
#  ctypes structures
# ─────────────────────────────────────────────────────────────

class AccentPolicy(ctypes.Structure):
    """
    Maps the undocumented ``ACCENT_POLICY`` struct.

    Fields
    ------
    AccentState   : DWORD  — one of the ACCENT_* constants above.
    AccentFlags   : DWORD  — controls which part of the window is affected.
                             ``2`` = apply to the entire window.
    GradientColor : DWORD  — packed ABGR colour; the alpha byte controls
                             opacity (0x00 = fully transparent,
                             0xFF = fully opaque).
    AnimationId   : DWORD  — unused, set to 0.
    """
    _fields_ = [
        ("AccentState",   wt.DWORD),
        ("AccentFlags",   wt.DWORD),
        ("GradientColor", wt.DWORD),
        ("AnimationId",   wt.DWORD),
    ]


class WindowCompositionAttributeData(ctypes.Structure):
    """
    Maps the undocumented ``WINDOWCOMPOSITIONATTRIBUTEDATA`` struct
    consumed by ``SetWindowCompositionAttribute``.
    """
    _fields_ = [
        ("Attribute",  wt.DWORD),          # WCA_ACCENT_POLICY = 19
        ("Data",       ctypes.c_void_p),   # pointer to AccentPolicy
        ("SizeOfData", ctypes.c_size_t),   # sizeof(AccentPolicy)
    ]


# ─────────────────────────────────────────────────────────────
#  Win32 function binding
# ─────────────────────────────────────────────────────────────
user32 = ctypes.windll.user32

FindWindowW = user32.FindWindowW
FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
FindWindowW.restype  = wt.HWND

_SetWindowCompositionAttribute = user32.SetWindowCompositionAttribute
_SetWindowCompositionAttribute.argtypes = [
    wt.HWND,
    ctypes.POINTER(WindowCompositionAttributeData),
]
_SetWindowCompositionAttribute.restype = wt.BOOL


# ═════════════════════════════════════════════════════════════
#  TaskbarController
# ═════════════════════════════════════════════════════════════
class TaskbarController:
    """
    Controls the visual style (transparency / acrylic) of the Windows
    taskbar via the ``SetWindowCompositionAttribute`` API.

    Parameters
    ----------
    auto_find : bool
        If ``True`` (default), the ``Shell_TrayWnd`` handle is resolved
        automatically on construction.  Pass ``False`` to defer lookup
        (call ``find_taskbar()`` later).

    Example
    -------
    >>> tc = TaskbarController()
    >>> tc.update_accent(mode=4, alpha=100)   # acrylic, ~39 % opacity
    >>> tc.restore()
    """

    # ── Friendly name mapping ──────────────────────────────
    MODE_TRANSPARENT = ACCENT_ENABLE_TRANSPARENTGRADIENT   # 2
    MODE_ACRYLIC     = ACCENT_ENABLE_ACRYLICBLURBEHIND     # 4
    MODE_BLUR        = ACCENT_ENABLE_BLURBEHIND            # 3

    def __init__(self, auto_find: bool = True) -> None:
        self._taskbar_hwnd:    Optional[int] = None
        self._secondary_hwnd:  Optional[int] = None  # multi-monitor
        self._original_applied: bool = False

        if auto_find:
            self.find_taskbar()

    # ─────────────────────────────────────────────────────────
    #  Public API
    # ─────────────────────────────────────────────────────────
    def find_taskbar(self) -> bool:
        """
        Locate ``Shell_TrayWnd`` (primary taskbar) and
        ``Shell_SecondaryTrayWnd`` (secondary monitor taskbar, if any).

        Returns True if at least the primary taskbar was found.
        """
        self._taskbar_hwnd = FindWindowW("Shell_TrayWnd", None)
        if not self._taskbar_hwnd:
            log.error("Could not find Shell_TrayWnd.")
            return False

        log.info("Shell_TrayWnd handle: 0x%X", self._taskbar_hwnd)

        # Optional: secondary taskbar on multi-monitor setups
        self._secondary_hwnd = FindWindowW("Shell_SecondaryTrayWnd", None)
        if self._secondary_hwnd:
            log.info("Shell_SecondaryTrayWnd handle: 0x%X", self._secondary_hwnd)

        return True

    def update_accent(self, mode: int = 2, alpha: int = 0) -> bool:
        """
        Apply an accent effect to the taskbar.

        Parameters
        ----------
        mode : int
            Accent state.  Use the class constants for readability:
              - ``TaskbarController.MODE_TRANSPARENT`` (2) — transparent
              - ``TaskbarController.MODE_BLUR``        (3) — blur behind
              - ``TaskbarController.MODE_ACRYLIC``     (4) — acrylic glass

        alpha : int
            Opacity from 0 (fully clear) to 255 (fully opaque).
            Packed into the high byte of GradientColor (ABGR format).

        Returns True on success.
        """
        if not self._taskbar_hwnd:
            log.error("No taskbar HWND — call find_taskbar() first.")
            return False

        alpha = max(0, min(255, alpha))

        # Pack alpha into GradientColor:  0xAA_BB_GG_RR  (ABGR)
        # We use black (0x000000) + the requested alpha byte.
        gradient_color = (alpha << 24) & 0xFFFFFFFF

        ok = self._apply(self._taskbar_hwnd, mode, gradient_color)

        # Also apply to secondary taskbar if present
        if self._secondary_hwnd:
            self._apply(self._secondary_hwnd, mode, gradient_color)

        if ok:
            self._original_applied = True
            log.info(
                "Taskbar accent applied: mode=%d, alpha=%d (0x%08X).",
                mode, alpha, gradient_color,
            )

        return ok

    def restore(self) -> bool:
        """
        Restore the taskbar to the default Windows style.
        Idempotent — safe to call multiple times.
        """
        if not self._taskbar_hwnd:
            return False

        ok = self._apply(self._taskbar_hwnd, ACCENT_DISABLED, 0)

        if self._secondary_hwnd:
            self._apply(self._secondary_hwnd, ACCENT_DISABLED, 0)

        if ok:
            self._original_applied = False
            log.info("Taskbar restored to default style.")

        return ok

    @property
    def taskbar_hwnd(self) -> Optional[int]:
        """Primary taskbar HWND, or None."""
        return self._taskbar_hwnd

    @property
    def is_modified(self) -> bool:
        """True if an accent has been applied and not yet restored."""
        return self._original_applied

    # ─────────────────────────────────────────────────────────
    #  Internal — low-level API call
    # ─────────────────────────────────────────────────────────
    @staticmethod
    def _apply(hwnd: int, accent_state: int, gradient_color: int) -> bool:
        """
        Call ``SetWindowCompositionAttribute`` on *hwnd*.

        Returns True on success.
        """
        accent = AccentPolicy()
        accent.AccentState   = accent_state
        accent.AccentFlags   = 2            # apply to entire window
        accent.GradientColor = gradient_color
        accent.AnimationId   = 0

        data = WindowCompositionAttributeData()
        data.Attribute  = WCA_ACCENT_POLICY
        data.Data       = ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p)
        data.SizeOfData = ctypes.sizeof(accent)

        result = _SetWindowCompositionAttribute(hwnd, ctypes.byref(data))
        if not result:
            log.warning(
                "SetWindowCompositionAttribute failed for HWND 0x%X.", hwnd,
            )
        return bool(result)

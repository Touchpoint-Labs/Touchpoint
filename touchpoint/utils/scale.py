"""Display scale factor detection for DPR-aware coordinate conversion.

All Touchpoint public coordinates use **physical** (device) pixels — the
same space as screenshots, ``xdotool``, UIA ``BoundingRectangle``,
``SendInput``, and ``PIL.ImageGrab``.

Backends that return **logical** (CSS / DPI-scaled) coordinates must
multiply by the display scale factor before exposing positions/sizes
through the public API.  Backends that accept screen coordinates in
their ``get_element_at()`` must divide incoming physical coords by the
scale factor before using them internally.

Scale factor = ``physical pixels / logical pixels``  (e.g. 1.25).

Resolution order
----------------

1. ``tp.configure(scale_factor=1.25)`` — explicit user override
2. Auto-detection per platform:

   **Linux / X11**
       ``Xft.dpi / 96``.  X11 desktop environments write ``Xft.dpi``
       to the X resource database; both Qt and GTK read it for UI
       layout scaling.  AT-SPI then reports logical coordinates while
       ``xdotool`` and screenshots use physical X11 pixels.  Falls
       back to 1.0 if ``Xft.dpi`` is unset or equals 96.

   **Windows**
       Per-monitor DPI via ``MonitorFromPoint`` + ``GetDpiForMonitor``
       (Windows 8.1+).  Falls back to ``GetDpiForSystem() / 96``
       (system-wide DPI) on older systems.  UIA
       ``BoundingRectangle`` returns physical coordinates that
       require DPI awareness.

   **macOS**
       Not yet implemented — macOS Cocoa apps report physical coords
       natively.  Scale factor is always 1.0 here (NS APIs already
       account for Retina).
"""

from __future__ import annotations

import functools
import logging
import subprocess
import sys

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# User override (set via tp.configure(scale_factor=...))
# ---------------------------------------------------------------------------

_user_scale_factor: float | None = None


def set_scale_factor(value: float | None) -> None:
    """Set (or clear) the user-configured scale factor.

    Called by :func:`touchpoint.configure`.  ``None`` means
    auto-detect.
    """
    global _user_scale_factor
    _user_scale_factor = value
    # Invalidate all cached auto-detected values so the next call
    # re-evaluates from scratch (matters when user sets then clears).
    _get_x11_scale.cache_clear()
    _win32_monitor_cache.clear()


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def get_scale_factor(x: int = 0, y: int = 0) -> float:
    """Return the display scale factor at screen point ``(x, y)``.

    Resolution order:

    1. User override via ``tp.configure(scale_factor=...)``
    2. Platform auto-detection (Linux: ``Xft.dpi / 96`` via
       ``xrdb``, Windows: per-monitor DPI, macOS: 1.0)

    The ``(x, y)`` hint is only meaningful on Windows with
    per-monitor DPI — on Linux the scale is global (from ``Xft.dpi``).

    A scale factor of 1.0 means no scaling (logical == physical).

    The result is cached after first computation (Linux) or per
    monitor (Windows).
    """
    # 1. Explicit user override.
    if _user_scale_factor is not None:
        return _user_scale_factor

    # 2. Platform auto-detection.
    if sys.platform == "linux":
        return _get_x11_scale()
    if sys.platform == "win32":
        return _get_win32_scale(x, y)
    if sys.platform == "darwin":
        return _get_macos_scale()
    # Other platforms — assume 1.0.
    return 1.0


# ---------------------------------------------------------------------------
# Linux / X11
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def _get_x11_scale() -> float:
    """Return the display scale factor on Linux / X11.

    Reads ``Xft.dpi`` from ``xrdb -query`` and returns
    ``xft_dpi / 96.0``.  X11 desktop environments (KDE, GNOME, XFCE,
    etc.) write ``Xft.dpi`` to the X resource database.  Both Qt and
    GTK read it for UI layout scaling, causing AT-SPI to report
    coordinates in a logical pixel space smaller than the physical
    X11 screen.  ``xdotool`` and screenshots still operate in
    physical X11 pixels, so the scale factor bridges the two.

    The raw ratio is returned **without** rounding (unlike Windows
    where the OS enforces 0.25-step increments).  ``Xft.dpi`` can be
    any integer (96, 110, 120, 144…), so rounding would introduce
    positional error.

    Falls back to ``1.0`` if ``xrdb`` is unavailable, ``Xft.dpi`` is
    not set, or the parsed value is not a positive number.
    """
    try:
        out = subprocess.check_output(
            ["xrdb", "-query"],
            text=True,
            timeout=2,
            stderr=subprocess.DEVNULL,
        )
        for line in out.splitlines():
            if line.startswith("Xft.dpi"):
                # Format: "Xft.dpi:\t110" or "Xft.dpi:  110"
                _, _, value = line.partition(":")
                dpi = float(value.strip())
                if dpi > 0:
                    return dpi / 96.0
    except (FileNotFoundError, subprocess.TimeoutExpired,
            subprocess.CalledProcessError, ValueError, OSError):
        pass
    return 1.0


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------

# Cache: monitor handle (HMONITOR) → scale float
_win32_monitor_cache: dict[int, float] = {}


def _get_win32_scale(x: int = 0, y: int = 0) -> float:
    """Per-monitor DPI on Windows via ``shcore`` / ``user32``.

    Falls back to system DPI if ``GetDpiForMonitor`` is unavailable
    (Windows 7 and earlier).
    """
    if sys.platform != "win32":
        return 1.0

    import ctypes
    import ctypes.wintypes as wt

    try:
        # --- Per-monitor DPI (Windows 8.1+) --------------------------------
        shcore = ctypes.windll.shcore  # type: ignore[attr-defined]
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]

        pt = wt.POINT(x, y)
        hmonitor = user32.MonitorFromPoint(pt, 1)  # MONITOR_DEFAULTTONEAREST

        if hmonitor in _win32_monitor_cache:
            return _win32_monitor_cache[hmonitor]

        dpi_x = ctypes.c_uint()
        dpi_y = ctypes.c_uint()
        # MDT_EFFECTIVE_DPI = 0
        hr = shcore.GetDpiForMonitor(
            hmonitor, 0,
            ctypes.byref(dpi_x), ctypes.byref(dpi_y),
        )
        if hr == 0:
            scale = dpi_x.value / 96.0
            _win32_monitor_cache[hmonitor] = scale
            return scale
    except (AttributeError, OSError):
        pass

    # --- System DPI fallback (any Windows) ---------------------------------
    try:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        user32.SetProcessDPIAware()
        dc = user32.GetDC(0)
        gdi32 = ctypes.windll.gdi32  # type: ignore[attr-defined]
        dpi = gdi32.GetDeviceCaps(dc, 88)  # LOGPIXELSX
        user32.ReleaseDC(0, dc)
        return dpi / 96.0
    except Exception:
        pass

    return 1.0


# ---------------------------------------------------------------------------
# macOS
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def _get_macos_scale() -> float:
    """Return the display scale factor on macOS.

    The macOS Accessibility API (AX) reports positions and sizes in
    *points* — logical coordinates that already account for Retina
    scaling.  Quartz / CGEvent also operates in points.  Therefore,
    the scale factor between AX coordinates and input coordinates is
    **1.0** on macOS; no bridging is needed.

    We still expose this function (rather than a bare ``return 1.0``)
    so that ``get_scale_factor`` has a clearly labelled macOS path
    and so future callers can override via ``configure(scale_factor=…)``
    if a non-standard configuration is encountered.
    """
    return 1.0

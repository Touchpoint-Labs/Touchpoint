"""Screenshot utility for Touchpoint.

Captures screen pixels and returns a ``PIL.Image.Image`` object.
Supports full-screen capture, window/element cropping, and padding.

No encoding, no base64, no "send to LLM" helpers — the agent
developer decides what to do with the image.

Depends on ``Pillow`` (installed automatically with ``pip install touchpoint-py``).

Platform support:

============  ============================================
Platform      Mechanism
============  ============================================
Linux / X11   ``PIL.ImageGrab.grab()``
Windows       ``PIL.ImageGrab.grab()`` (Win32 GDI)
macOS         ``PIL.ImageGrab.grab()`` (screencapture)
============  ============================================

.. note::
    Wayland is not supported — ``ImageGrab`` requires X11.
    Wayland users should run under XWayland or use a
    Wayland-specific tool (``grim``, ``gnome-screenshot``)
    and load the result with ``PIL.Image.open()``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL.Image import Image


def _ensure_pillow() -> None:
    """Raise a clear error if Pillow is not installed."""
    try:
        import PIL  # noqa: F401
    except ImportError:
        raise ImportError(
            "Pillow is required for screenshots but could not be "
            "imported. Try: pip install Pillow"
        ) from None


def take_screenshot(
    region: tuple[int, int, int, int] | None = None,
) -> Image:
    """Capture screen pixels and return a PIL Image.

    Args:
        region: Optional ``(left, top, right, bottom)`` bounding box
            in absolute screen coordinates.  If ``None``, captures
            the full virtual desktop (all monitors).

    Returns:
        A ``PIL.Image.Image`` in RGB mode.

    Raises:
        ImportError: If Pillow is not installed.
        OSError: If the screen cannot be captured (e.g. no display
            server, Wayland without XWayland).
    """
    _ensure_pillow()
    from PIL import ImageGrab

    # ImageGrab.grab() accepts bbox=(left, top, right, bottom)
    # all_screens=True is required on Windows/macOS to capture
    # across all monitors.  On Linux/X11 it's a no-op (the full
    # virtual desktop is always captured).
    if region is not None:
        img = ImageGrab.grab(bbox=region, all_screens=True)
    else:
        img = ImageGrab.grab(all_screens=True)

    if img.mode != "RGB":
        img = img.convert("RGB")

    return img


def get_monitor_regions() -> list[tuple[int, int, int, int]]:
    """Return bounding boxes for each physical monitor.

    Each region is ``(left, top, right, bottom)`` in absolute
    virtual desktop coordinates.

    Uses ``screeninfo`` (cross-platform, zero-dependency) when
    available, otherwise falls back to treating the full virtual
    desktop as a single monitor.

    Returns:
        List of monitor regions, ordered by the OS.  Returns a
        single entry for the full virtual desktop if individual
        monitor info is not available.

    Raises:
        ImportError: If Pillow is not installed and ``screeninfo``
            is also unavailable.
    """
    # screeninfo: cross-platform (Linux/Windows/macOS), zero deps.
    try:
        from screeninfo import get_monitors

        regions = []
        for m in get_monitors():
            regions.append((m.x, m.y, m.x + m.width, m.y + m.height))
        if regions:
            return regions
    except Exception:
        pass

    # Fallback: full virtual desktop as a single "monitor".
    _ensure_pillow()
    from PIL import ImageGrab

    img = ImageGrab.grab(all_screens=True)
    w, h = img.size
    img.close()
    return [(0, 0, w, h)]

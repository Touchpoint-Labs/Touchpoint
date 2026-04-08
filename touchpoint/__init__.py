"""Touchpoint — unified accessibility API for AI agents.

Import as::

    import touchpoint as tp
    tp.apps()
    tp.windows()
    tp.elements(app="Firefox")

This module is the **only** public entry point.  Everything else
(backends, cache, matching) is internal.
"""

from __future__ import annotations

__version__ = "0.1.1"

import logging
import sys
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

from touchpoint.backends.base import Backend, InputProvider
from touchpoint.core.exceptions import (
    ActionFailedError,
    BackendUnavailableError,
)

# ---------------------------------------------------------------------------
# Backend auto-detection
# ---------------------------------------------------------------------------
# Touchpoint picks the right backend for the current platform.
# Platform backends: Linux (AT-SPI2), Windows (UIA), macOS (AX).
# The right backend is selected automatically at runtime.
# ---------------------------------------------------------------------------

_backend: Backend | None = None


def _init_backend() -> Backend:
    """Detect the platform and return an appropriate backend.

    Returns:
        A ready-to-use :class:`Backend` instance.

    Raises:
        BackendUnavailableError: If no backend can be initialised on
            this platform.
    """
    global _backend  # noqa: PLW0603

    if _backend is not None:
        return _backend

    if sys.platform.startswith("linux"):
        from touchpoint.backends.linux.atspi import AtSpiBackend

        candidate = AtSpiBackend()
        if candidate.is_available():
            _backend = candidate
            return _backend

        raise BackendUnavailableError(
            backend="atspi",
            reason=(
                "PyGObject or AT-SPI2 is not available. "
                "Ensure python3-gi and gir1.2-atspi-2.0 are installed "
                "(or equivalent for your distro) and the AT-SPI2 "
                "daemon is running (it starts automatically on most "
                "desktop sessions)"
            ),
        )

    if sys.platform == "win32":
        from touchpoint.backends.windows import UiaBackend

        candidate = UiaBackend()
        if candidate.is_available():
            _backend = candidate
            return _backend

        raise BackendUnavailableError(
            backend="uia",
            reason="comtypes is not installed or UIA is not accessible",
        )

    if sys.platform == "darwin":
        from touchpoint.backends.macos.ax import AxBackend

        candidate = AxBackend()
        if candidate.is_available():
            _backend = candidate
            return _backend

        raise BackendUnavailableError(
            backend="ax",
            reason=(
                "pyobjc is not installed or Accessibility permission "
                "has not been granted (System Settings → Privacy & "
                "Security → Accessibility)"
            ),
        )

    raise BackendUnavailableError(
        backend="unknown",
        reason=f"no backend available for platform {sys.platform!r}",
    )


def _get_backend() -> Backend:
    """Return the active backend, initialising on first call."""
    if _backend is None:
        return _init_backend()
    return _backend


# ---------------------------------------------------------------------------
# CDP backend (optional, additive)
# ---------------------------------------------------------------------------
# The CDP backend runs alongside the platform backend to provide
# richer accessibility trees for Electron/Chromium apps launched
# with ``--remote-debugging-port``.  It is initialised lazily on
# first use and can be configured via ``tp.configure(cdp_ports=...)``.
# ---------------------------------------------------------------------------

_cdp_backend: Any = None  # CdpBackend | None — Any avoids hard import
_cdp_attempted: bool = False
_cdp_last_refresh: float = 0.0  # monotonic timestamp of last refresh


def _init_cdp() -> Any:
    """Try to initialise the CDP backend.

    Returns the CdpBackend instance (may have zero connections if
    no CDP ports are discovered), or ``None`` if ``websocket-client``
    is not installed.  Never raises.
    """
    global _cdp_backend, _cdp_attempted, _cdp_last_refresh  # noqa: PLW0603
    _cdp_attempted = True

    try:
        from touchpoint.backends.cdp import CdpBackend
    except ImportError:
        return None

    ports = _config.get("cdp_ports") or None
    discover = _config.get("cdp_discover", True)

    try:
        _cdp_backend = CdpBackend(
            configured_ports=ports,
            auto_discover=discover,
        )
        _cdp_last_refresh = time.monotonic()
        # Inject platform display names immediately so CDP can
        # resolve display names from the very first call.
        try:
            names = {w.pid: w.app for w in _get_backend().get_windows()}
            _cdp_backend.set_pid_display_names(names)
        except Exception:
            logger.debug("CDP: failed to set initial PID display names",
                         exc_info=True)
    except Exception:
        logger.debug("CDP: backend initialisation failed", exc_info=True)
        _cdp_backend = None

    return _cdp_backend


def _get_cdp() -> Any:
    """Return the CDP backend, or ``None`` if unavailable.

    Initialises lazily on first call.  On subsequent calls, performs
    a lightweight ``refresh_targets()`` if more than
    ``cdp_refresh_interval`` seconds have elapsed since the last
    refresh — this picks up newly launched CDP applications (e.g.
    Chrome started after the MCP server).
    """
    global _cdp_last_refresh  # noqa: PLW0603
    if _cdp_backend is not None:
        now = time.monotonic()
        if now - _cdp_last_refresh >= _config["cdp_refresh_interval"]:
            try:
                _cdp_backend.refresh_targets()
            except Exception:
                logger.debug("CDP: refresh_targets failed", exc_info=True)
            # Inject platform display names so CDP can resolve
            # user-visible app names (e.g. "Google Chrome") to
            # PIDs without calling back into the public API.
            try:
                names = {w.pid: w.app for w in _get_backend().get_windows()}
                _cdp_backend.set_pid_display_names(names)
            except Exception:
                logger.debug("CDP: failed to set PID display names",
                             exc_info=True)
            _cdp_last_refresh = now
        return _cdp_backend
    if _cdp_attempted:
        return None
    return _init_cdp()


def _reinit_cdp() -> None:
    """Force re-initialisation of the CDP backend.

    Called by ``configure()`` when CDP-related keys change.
    """
    global _cdp_backend, _cdp_attempted, _cdp_last_refresh  # noqa: PLW0603
    if _cdp_backend is not None:
        try:
            _cdp_backend.close()
        except Exception:
            pass
    _cdp_backend = None
    _cdp_attempted = False
    _cdp_last_refresh = 0.0


def _is_cdp_id(element_id: str) -> bool:
    """Return ``True`` if *element_id* belongs to the CDP backend.

    Delegates to :meth:`Backend.owns_element` when the CDP backend
    is available.  Falls back to a format check as a bootstrap
    workaround before CDP has been initialised.
    """
    if _cdp_backend is not None:
        return _cdp_backend.owns_element(element_id)
    # Bootstrap: CDP not yet initialised — peek at format so routing
    # still works on the very first call.
    return isinstance(element_id, str) and element_id.startswith("cdp:")


def _strip_document_subtrees(elems: list["Element"]) -> list["Element"]:
    """Remove ``Role.DOCUMENT`` elements and all their descendants.

    Platform backends (AT-SPI2, UIA, AX) build element IDs
    hierarchically: deeper descendants append ``.{index}`` to their
    parent's ID.  Descendants of a document element therefore have
    IDs that start with the document's ID + ``"."``.  This lets us
    cheaply prune the web-content subtree that CDP already covers.

    Only called on platform backend elements, never on CDP elements.
    """
    # First pass — collect document-element ID prefixes.
    doc_prefixes: list[str] = []
    for e in elems:
        if e.role == Role.DOCUMENT:
            doc_prefixes.append(e.id + ".")

    if not doc_prefixes:
        return elems

    # Second pass — drop documents and their descendants.
    out: list["Element"] = []
    for e in elems:
        if e.role == Role.DOCUMENT:
            continue
        if any(e.id.startswith(p) for p in doc_prefixes):
            continue
        out.append(e)
    return out


def _is_cdp_app(app: str) -> bool:
    """Return ``True`` if *app* is served by the CDP backend.

    Checks by two routes:

    1. Direct match via the CDP backend's own comm-name and
       display-name knowledge (cheap dict lookup).
    2. Platform-window cross-reference — maps the user-visible
       display name to a PID and checks whether that PID is owned
       by CDP.  This is the slow path, but should rarely be hit
       now that ``set_pid_display_names()`` feeds display names
       into the CDP backend.
    """
    cdp = _get_cdp()
    if cdp is None:
        return False
    # Route 1 — CDP backend's own comm-name + display-name knowledge.
    if cdp.claims_app(app):
        return True
    # Route 2 — platform-window PID cross-reference.
    cdp_pids = cdp.get_owned_pids()
    if not cdp_pids:
        return False
    try:
        for w in _get_backend().get_windows():
            if w.app.lower() == app.lower() and w.pid in cdp_pids:
                return True
    except Exception:
        pass
    return False


def _resolve_platform_app(app: str) -> str:
    """Map a user-supplied app name to the platform backend's display name.

    CDP and the platform backend (AT-SPI / UIA) often use different
    names for the same application (e.g. ``"chrome"`` vs
    ``"Google Chrome"``).  This resolves the display name so the
    platform backend can be queried correctly during dual-backend merges.
    Falls back to the original *app* name if no mapping is found.
    """
    cdp = _get_cdp()
    if cdp is None:
        return app
    cdp_pids = cdp.get_owned_pids()
    if not cdp_pids:
        return app
    # Resolve PIDs for this name via the CDP backend's own mapping.
    app_pids = cdp.get_pids_for_app(app)
    try:
        for w in _get_backend().get_windows():
            if w.pid not in cdp_pids:
                continue
            # Match if the user passed the display name directly, or
            # if they passed the comm-name that maps to this window's PID.
            if w.app.lower() == app.lower() or w.pid in app_pids:
                return w.app
    except Exception:
        pass
    return app


def _topmost_pid_at(x: int, y: int) -> int | None:
    """Return the PID of the topmost application window at ``(x, y)``.

    Delegates entirely to the platform backend's :meth:`get_topmost_pid_at`,
    keeping all platform-specific logic (X11 stacking, Win32 POINT, etc.)
    inside the backend where it belongs.
    """
    return _get_backend().get_topmost_pid_at(x, y)


def _png_bytes_to_image(png_bytes: bytes) -> Any:
    """Convert raw PNG bytes to a ``PIL.Image.Image``."""
    import io

    from PIL import Image

    return Image.open(io.BytesIO(png_bytes)).convert("RGB")


def _backend_for_id(element_id: str) -> Backend:
    """Return the backend that owns *element_id*.

    Routes ``cdp:*`` IDs to the CDP backend and everything else
    to the platform backend.

    Raises:
        BackendUnavailableError: If the required backend is not
            available.
    """
    if _is_cdp_id(element_id):
        cdp = _get_cdp()
        if cdp is None:
            raise BackendUnavailableError(
                backend="cdp",
                reason="CDP backend is not available "
                "(install websocket-client and launch an app "
                "with --remote-debugging-port)",
            )
        return cdp
    return _get_backend()


# ---------------------------------------------------------------------------
# InputProvider auto-detection
# ---------------------------------------------------------------------------

_input_provider: InputProvider | None = None


def _init_input() -> InputProvider | None:
    """Detect the platform and return an appropriate input provider.

    Returns:
        A ready-to-use :class:`InputProvider`, or ``None`` if no
        provider is available on this platform.
    """
    global _input_provider  # noqa: PLW0603

    if _input_provider is not None:
        return _input_provider

    if sys.platform.startswith("linux"):
        from touchpoint.backends.linux.x11.input import XdotoolInput

        chunk = _config["type_chunk_size"] or None
        candidate = XdotoolInput(type_chunk_size=chunk)
        if candidate.is_available():
            _input_provider = candidate
            return _input_provider

    if sys.platform == "win32":
        from touchpoint.backends.windows.input import SendInputProvider

        candidate = SendInputProvider()
        if candidate.is_available():
            _input_provider = candidate
            return _input_provider

    if sys.platform == "darwin":
        from touchpoint.backends.macos.input import CGEventInput

        chunk = _config["type_chunk_size"] or None
        candidate = CGEventInput(type_chunk_size=chunk)
        if candidate.is_available():
            _input_provider = candidate
            return _input_provider

    return None


def _get_input() -> InputProvider:
    """Return the active input provider, initialising on first call.

    Raises:
        RuntimeError: If no input provider is available.
    """
    provider = _input_provider or _init_input()
    if provider is None:
        if sys.platform.startswith("linux"):
            import os
            import shutil

            if not os.environ.get("DISPLAY"):
                msg = (
                    "no input provider available — "
                    "$DISPLAY is not set (no X11 session). "
                    "Touchpoint input requires an X11 display server; "
                    "Wayland-only sessions are not yet supported"
                )
            elif not shutil.which("xdotool"):
                msg = (
                    "no input provider available — "
                    "xdotool is not installed. "
                    "Install it: sudo apt install xdotool "
                    "(or dnf/pacman equivalent)"
                )
            else:
                msg = "no input provider available"
        elif sys.platform == "win32":
            msg = (
                "no input provider available — "
                "on Windows, ctypes.windll must be accessible "
                "(requires a desktop session)"
            )
        elif sys.platform == "darwin":
            msg = (
                "no input provider available — "
                "on macOS, install pyobjc-framework-Quartz and grant "
                "Accessibility permission in System Settings → "
                "Privacy & Security → Accessibility"
            )
        else:
            msg = f"no input provider available for platform {sys.platform!r}"
        raise RuntimeError(msg)
    return provider


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_config: dict = {
    "fuzzy_threshold": 0.6,
    "fallback_input": True,
    "type_chunk_size": 40,
    "max_elements": 5000,
    "max_depth": 10,
    "cdp_ports": None,
    "cdp_discover": True,
    "cdp_refresh_interval": 5.0,
    "scale_factor": None,
}

_VALID_CONFIG_KEYS = frozenset(_config)

_VALID_SOURCES = ("full", "ax", "native", "dom")


def configure(**kwargs: Any) -> None:
    """Adjust Touchpoint runtime behaviour.

    Only the keys listed below are accepted.  Unknown keys raise
    :class:`ValueError`.

    Args:
        fuzzy_threshold: Minimum score (``0.0`` – ``1.0``) for fuzzy
            matches in :func:`find`.  Default ``0.6``.
        fallback_input: If ``True``, fall back to coordinate-based
            input (xdotool) when native accessibility actions fail.
            Default ``True``.
        type_chunk_size: Maximum characters per input tool
            invocation.  Long strings passed to :func:`type_text`
            are automatically split into chunks of this size.
            Set to ``0`` to disable chunking.  Default ``40``.
        max_elements: Maximum number of elements to collect per
            :func:`elements` call.  Prevents hanging on very large
            UI trees.  Default ``5000``.
        max_depth: Default maximum depth for tree walks when
            ``max_depth`` is not passed to :func:`elements`.
            Default ``10``.
        cdp_ports: Dict mapping application names to CDP debugging
            ports, e.g. ``{"Slack": 9222, "Discord": 9223}``.
            ``None`` (default) means rely on auto-discovery only.
        cdp_discover: If ``True`` (default), scan ``/proc/*/cmdline``
            for ``--remote-debugging-port`` flags.
        cdp_refresh_interval: Seconds between automatic CDP
            target re-discovery.  ``_get_cdp()`` calls
            ``refresh_targets()`` when this many seconds have
            elapsed, picking up newly launched browsers.  Set to
            ``0`` to refresh on every call (reliable but slower),
            or a large value to reduce overhead in tight loops.
            Default ``5.0``.
        scale_factor: Display scale factor (physical / logical
            pixels).  ``None`` (default) means auto-detect
            per platform (1.0 on X11, per-monitor DPI on
            Windows).  Set explicitly when auto-detection is
            wrong or on Wayland.  Example: ``1.25``.

    Raises:
        ValueError: If an unknown key is passed.

    Example::

        >>> import touchpoint as tp
        >>> tp.configure(fuzzy_threshold=0.8, fallback_input=False)
        >>> tp.configure(cdp_ports={"Slack": 9222})
    """
    global _input_provider
    for key in kwargs:
        if key not in _VALID_CONFIG_KEYS:
            msg = f"unknown config key {key!r} — valid keys: {sorted(_VALID_CONFIG_KEYS)}"
            raise ValueError(msg)
    # --- Value validation ---
    if "fuzzy_threshold" in kwargs:
        v = kwargs["fuzzy_threshold"]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not (0.0 <= v <= 1.0):
            raise ValueError(
                f"fuzzy_threshold must be a float between 0.0 and 1.0, got {v!r}"
            )
    if "type_chunk_size" in kwargs:
        v = kwargs["type_chunk_size"]
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise ValueError(
                f"type_chunk_size must be a non-negative integer, got {v!r}"
            )
    if "fallback_input" in kwargs:
        v = kwargs["fallback_input"]
        if not isinstance(v, bool):
            raise ValueError(
                f"fallback_input must be a bool, got {v!r}"
            )
    if "max_elements" in kwargs:
        v = kwargs["max_elements"]
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise ValueError(
                f"max_elements must be a positive integer, got {v!r}"
            )
    if "max_depth" in kwargs:
        v = kwargs["max_depth"]
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise ValueError(
                f"max_depth must be a non-negative integer, got {v!r}"
            )
    if "cdp_ports" in kwargs:
        v = kwargs["cdp_ports"]
        if v is not None:
            if not isinstance(v, dict):
                raise ValueError(
                    f"cdp_ports must be a dict or None, got {type(v).__name__}"
                )
            for name, port in v.items():
                if not isinstance(name, str) or not isinstance(port, int):
                    raise ValueError(
                        f"cdp_ports must map str → int, got {name!r} → {port!r}"
                    )
    if "cdp_discover" in kwargs:
        v = kwargs["cdp_discover"]
        if not isinstance(v, bool):
            raise ValueError(
                f"cdp_discover must be a bool, got {v!r}"
            )
    if "cdp_refresh_interval" in kwargs:
        v = kwargs["cdp_refresh_interval"]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            raise ValueError(
                f"cdp_refresh_interval must be a non-negative number, got {v!r}"
            )
    if "scale_factor" in kwargs:
        v = kwargs["scale_factor"]
        if v is not None:
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
                raise ValueError(
                    f"scale_factor must be a positive number or None, got {v!r}"
                )

    _config.update(kwargs)
    # InputProvider caches type_chunk_size at construction time.
    # Invalidate so the next call picks up the new value.
    if "type_chunk_size" in kwargs:
        _input_provider = None
    # Apply scale factor override.
    if "scale_factor" in kwargs:
        from touchpoint.utils.scale import set_scale_factor
        set_scale_factor(
            float(kwargs["scale_factor"]) if kwargs["scale_factor"] is not None
            else None
        )
    # Re-initialise CDP backend when CDP config changes.
    if "cdp_ports" in kwargs or "cdp_discover" in kwargs:
        _reinit_cdp()


# ---------------------------------------------------------------------------
# Discovery API
# ---------------------------------------------------------------------------


def apps() -> list[str]:
    """List applications that expose accessibility elements.

    Queries the backend for all applications currently registered
    in the accessibility tree.

    Returns:
        Application names (e.g. ``["Firefox", "Konsole", "Slack"]``).

    Raises:
        BackendUnavailableError: If no backend is available.

    Example::

        >>> import touchpoint as tp
        >>> tp.apps()
        ['Firefox', 'Konsole', 'Kate']
    """
    # Platform apps — deduplicate by name (AT-SPI can return the same
    # display name multiple times when multiple processes share one).
    seen: set[str] = set()
    result: list[str] = []
    for a in _get_backend().get_applications():
        if a.lower() not in seen:
            result.append(a)
            seen.add(a.lower())

    cdp = _get_cdp()
    if cdp is None:
        return result

    cdp_pids: set[int] = cdp.get_owned_pids()

    # Determine which CDP PIDs are already represented in `result`
    # via the platform backend's display name (e.g. "Google Chrome"
    # already covers PID 248837 even though CDP calls it "chrome").
    try:
        covered_pids: set[int] = {
            w.pid
            for w in _get_backend().get_windows()
            if w.pid in cdp_pids and w.app.lower() in seen
        }
    except Exception:
        covered_pids = set()

    # Build a comm-name → PIDs map from CDP windows so we can check
    # whether a CDP app name is already covered by a platform display name.
    # Uses only get_windows() which is declared on the Backend ABC.
    try:
        cdp_win_pids: dict[str, set[int]] = {}
        for w in cdp.get_windows():
            cdp_win_pids.setdefault(w.app.lower(), set()).add(w.pid)
    except Exception:
        cdp_win_pids = {}

    for cdp_app in cdp.get_applications():
        if cdp_app.lower() in seen:
            continue
        app_pids = cdp_win_pids.get(cdp_app.lower(), set())
        # Skip if every PID for this CDP name is already represented
        # under a platform display name in the result.
        if app_pids and app_pids <= covered_pids:
            continue
        result.append(cdp_app)
        seen.add(cdp_app.lower())

    return result


def windows() -> list[Window]:
    """List all windows from the accessibility tree.

    Returns every window the backend can see — visible, hidden,
    active, inactive.  No filtering is applied.

    Returns:
        List of :class:`~touchpoint.core.window.Window` instances.

    Raises:
        BackendUnavailableError: If no backend is available.

    Example::

        >>> import touchpoint as tp
        >>> tp.windows()
        [Window('untitled — Kate', app='Kate'), ...]
    """
    platform_wins = _get_backend().get_windows()
    cdp = _get_cdp()
    if cdp is None:
        return platform_wins

    cdp_wins = cdp.get_windows()
    if not cdp_wins:
        return platform_wins

    # Build a PID → display-name map from platform windows so we can
    # normalise CDP window app names.  CDP names windows by comm name
    # (e.g. "chrome") while the platform backend uses the display name
    # (e.g. "Google Chrome").  Normalising here keeps windows() and
    # apps() consistent — both surface the same name for the same app.
    pid_display: dict[int, str] = {w.pid: w.app for w in platform_wins}

    # Replace platform windows whose PID is claimed by the CDP
    # backend — CDP trees are far richer for Electron/Chromium apps.
    cdp_pids: set[int] = cdp.get_owned_pids()
    result = [w for w in platform_wins if w.pid not in cdp_pids]

    for w in cdp_wins:
        display = pid_display.get(w.pid)
        if display is not None and display != w.app:
            from dataclasses import replace as _dc_replace
            w = _dc_replace(w, app=display)
        result.append(w)
    return result


# ---------------------------------------------------------------------------
# Element retrieval + filtering
# ---------------------------------------------------------------------------


def elements(
    app: str | None = None,
    window_id: str | None = None,
    tree: bool = False,
    max_depth: int | None = None,
    root_element: str | Element | None = None,
    max_elements: int | None = None,
    states: list[State] | None = None,
    role: Role | None = None,
    named_only: bool = False,
    filter: Callable[[Element], bool] | None = None,
    sort_by: str | Callable[[Element], Any] | None = None,
    filter_children: bool = True,
    format: str | None = None,
    source: str = "full",
) -> list[Element] | str:
    """Get UI elements from the accessibility tree.

    Scoping parameters (``app``, ``window_id``, ``tree``) are passed
    to the backend.  Filtering parameters (``states``, ``role``,
    ``named_only``, ``filter``) are applied afterwards on the returned
    elements.

    Args:
        app: Only include elements from this application
            (case-insensitive).
        window_id: Only include elements under this window.
        tree: If ``True``, populate each element's ``children``
            list recursively.
        max_depth: Maximum recursion depth.  ``0`` returns only
            the immediate children of the root(s), ``1`` includes
            grandchildren, and so on.  ``None`` (default) uses
            the configured default (``10``).
        root_element: Start the walk from this element id instead
            of the window roots.  Pass an :attr:`Element.id` from a
            previous call to drill into a specific container.
        max_elements: Maximum number of elements to collect.
            Overrides :func:`configure` ``max_elements`` for this
            call.  ``None`` uses the configured default (``5000``).
        states: Only include elements that have **all** of these
            states (AND logic).  ``None`` means no filtering.
        role: Only include elements with this role.
        named_only: If ``True``, exclude elements with empty or
            missing names. Default ``False``.
        filter: An arbitrary callable ``(Element) → bool``.  Only
            elements for which the callable returns ``True`` are
            kept.  Applied **after** ``role``, ``states``, and
            ``named_only`` filtering.  Default ``None``.

            Tool-call agent developers can expose this to their LLM
            as a string expression (e.g. ``"len(e.name) > 20"``)
            and ``eval()`` it into a callable on their side.
        sort_by: Controls the order of the returned elements.
            ``"position"`` sorts top-to-bottom, left-to-right
            (reading order).  A callable ``(Element) → sort_key``
            is passed directly to :func:`sorted`.  ``None``
            (default) preserves the backend's tree-traversal order.
        filter_children: When ``True`` (default) and ``tree=True``,
            recursively apply ``states``, ``named_only``, and
            ``filter`` to each element's children.  ``role`` is
            **not** applied to children — you typically want to
            see what's *inside* a role-matched element.  Set to
            ``False`` to keep raw unfiltered children.
        format: If set, return a formatted string instead of a list.
            One of ``"flat"``, ``"json"``, ``"tree"``.
        source: Controls which backend(s) provide elements:

            - ``"full"`` (default) — **merged**: CDP web content +
              platform native UI.  Best for a complete picture of
              the application.  For non-CDP apps, equivalent to
              ``"native"``.
            - ``"ax"`` — **CDP AX tree only**: web content from the
              browser's accessibility tree, no native UI merge.
              Faster when you only need page content.
            - ``"native"`` — **platform only**: AT-SPI / UIA elements,
              no CDP.  For interacting with native chrome UI
              (toolbar, tabs, dialogs) without web content noise.
            - ``"dom"`` — **CDP DOM walker**: the live DOM tree.
              Catches elements the AX tree misses (canvas content,
              un-annotated divs).  CDP-only; raises an error for
              native desktop apps.

    Returns:
        List of :class:`Element` instances, or a formatted string
        if ``format`` is specified.

    Raises:
        BackendUnavailableError: If no backend is available.
        ValueError: If *format* is not a recognised format name,
            or *sort_by* is an unrecognised string.

    Example::

        >>> import touchpoint as tp
        >>> # Full merged view (default)
        >>> tp.elements(app="Chrome")
        >>> # CDP AX tree only — fast, web content focused
        >>> tp.elements(app="Chrome", source="ax")
        >>> # Native UI only — toolbar, tabs, bookmarks
        >>> tp.elements(app="Chrome", source="native")
        >>> # DOM source for content the AX tree misses
        >>> tp.elements(app="Discord", source="dom", named_only=True)
    """
    # Validate source.
    if source not in _VALID_SOURCES:
        raise ValueError(
            f"source must be one of {_VALID_SOURCES!r}, got {source!r}"
        )

    # Validate sort_by early — before touching the backend.
    if (
        sort_by is not None
        and sort_by != "position"
        and not callable(sort_by)
    ):
        raise ValueError(
            f"unknown sort_by value {sort_by!r} "
            f"— use 'position' or a callable"
        )

    if isinstance(root_element, Element):
        root_element = root_element.id

    # DOM source — CDP only.
    if source == "dom":
        if tree:
            raise ValueError(
                "source='dom' does not support tree=True yet"
            )
        if (
            root_element is not None
            and isinstance(root_element, str)
            and _is_cdp_id(root_element)
            and ":dom:" not in root_element
        ):
            raise ValueError(
                "root_element must be a DOM-sourced element ID "
                "when source='dom' (got an AX-sourced ID)"
            )
        cdp = _get_cdp()
        if cdp is None:
            raise BackendUnavailableError(
                backend="cdp",
                reason="source='dom' requires a CDP backend",
            )
        # Validate that the target is a CDP app.
        if (
            app is not None
            and not _is_cdp_app(app)
            and (window_id is None or not _is_cdp_id(window_id))
            and (root_element is None or not _is_cdp_id(
                root_element if isinstance(root_element, str)
                else root_element
            ))
        ):
            from touchpoint.core.exceptions import TouchpointError
            raise TouchpointError(
                f"source='dom' is only supported for CDP-backed "
                f"apps, but {app!r} is not a CDP app"
            )
        effective_max_elements = (
            max_elements if max_elements is not None
            else _config["max_elements"]
        )
        # DOM trees are much deeper than AX trees.  Only apply
        # the user's explicit max_depth; otherwise let the backend
        # use its own generous default (50).
        effective_max_depth = max_depth  # None → backend default
        result = cdp.get_dom_elements(
            app=app, window_id=window_id,
            root_element=root_element,
            tree=tree, max_depth=effective_max_depth,
            max_elements=effective_max_elements,
            role=role, states=states, named_only=named_only,
        )
        result = _filter(
            result, states=states, role=role,
            named_only=named_only, filter=filter, sort_by=sort_by,
            filter_children=filter_children and tree,
        )
        if format is not None:
            from touchpoint.format.formatter import format_elements
            return format_elements(result, format)
        return result

    # Resolve effective limits: explicit param → config → hardcoded.
    effective_max_elements = max_elements if max_elements is not None else _config["max_elements"]
    effective_max_depth = max_depth if max_depth is not None else _config["max_depth"]

    # Route to the correct backend(s).
    _get_kw: dict[str, Any] = dict(
        tree=tree, max_depth=effective_max_depth,
        max_elements=effective_max_elements,
        role=role, states=states, named_only=named_only,
    )

    # --- source="native": platform backend only, no CDP at all ---
    if source == "native":
        result = _get_backend().get_elements(
            app=app, window_id=window_id,
            root_element=root_element, **_get_kw,
        )

    elif root_element is not None and _is_cdp_id(root_element):
        # Rooted in a CDP element — use CDP only.
        cdp = _get_cdp()
        if cdp is None:
            raise BackendUnavailableError(
                backend="cdp",
                reason="CDP backend required for element "
                f"{root_element!r}",
            )
        result = cdp.get_elements(
            app=app, window_id=window_id,
            root_element=root_element, **_get_kw,
        )
    elif window_id is not None and _is_cdp_id(window_id):
        # Scoped to a CDP window — use CDP only.
        cdp = _get_cdp()
        if cdp is None:
            raise BackendUnavailableError(
                backend="cdp",
                reason="CDP backend required for window "
                f"{window_id!r}",
            )
        result = cdp.get_elements(
            app=app, window_id=window_id,
            root_element=root_element, **_get_kw,
        )

    # --- source="ax": CDP AX tree only, no native merge ---
    elif source == "ax":
        cdp = _get_cdp()
        if app is None:
            # Unscoped — query all CDP targets.
            if cdp is None:
                raise BackendUnavailableError(
                    backend="cdp",
                    reason="source='ax' requires a CDP backend",
                )
            result = cdp.get_elements(
                app=None, window_id=window_id,
                root_element=root_element, **_get_kw,
            )
        elif _is_cdp_app(app):
            if cdp is None:
                raise BackendUnavailableError(
                    backend="cdp",
                    reason="source='ax' requires a CDP backend",
                )
            result = cdp.get_elements(
                app=app, window_id=window_id,
                root_element=root_element, **_get_kw,
            )
        else:
            # source="ax" explicitly requests CDP accessibility tree.
            # Non-CDP apps don't have one — raise.
            from touchpoint.core.exceptions import TouchpointError
            raise TouchpointError(
                f"source='ax' is only supported for CDP-backed "
                f"apps, but {app!r} is not a CDP app"
            )

    # --- source="full" (default): merged CDP + native ---
    elif app is not None and _is_cdp_app(app):
        # Collect native UI first (small set), then fill remaining
        # budget with CDP web content.  This ensures native toolbar/
        # tab elements always appear even when CDP produces thousands.
        native: list[Element] = []
        try:
            platform_app = _resolve_platform_app(app)
            _native_kw = dict(
                app=platform_app, window_id=window_id,
                root_element=root_element,
                tree=tree, max_depth=effective_max_depth,
                max_elements=effective_max_elements,
                role=role, states=states, named_only=named_only,
                skip_subtree_roles={Role.DOCUMENT},
            )
            native = _strip_document_subtrees(
                _get_backend().get_elements(**_native_kw),
            )
        except Exception:
            pass  # platform backend unavailable; CDP results suffice
        # Fill remaining budget with CDP AX elements.
        cdp_budget = max(0, effective_max_elements - len(native))
        cdp = _get_cdp()
        cdp_kw = dict(
            tree=tree, max_depth=effective_max_depth,
            max_elements=cdp_budget,
            role=role, states=states, named_only=named_only,
        )
        # Only call CDP when root_element belongs to the CDP backend
        # or is absent.  A non-CDP root_element (e.g. AT-SPI) roots
        # in native UI; CDP knows nothing about it so skip it.
        if cdp is not None and (root_element is None or cdp.owns_element(root_element)):
            cdp_els = cdp.get_elements(
                app=app, window_id=window_id,
                root_element=root_element, **cdp_kw,
            )
        else:
            cdp_els = []
        result = cdp_els + native
    else:
        # Not a CDP app — platform backend.
        result = _get_backend().get_elements(
            app=app, window_id=window_id,
            root_element=root_element, **_get_kw,
        )
        # Merge CDP elements when fully unscoped (no app, no window, no root).
        # When app is specified, limit to that app's elements only.
        cdp = _get_cdp()
        if cdp is not None and app is None and root_element is None and window_id is None:
            cdp_pids: set[int] = cdp.get_owned_pids()
            # Strip document subtrees from platform elements for PIDs
            # that CDP owns — CDP has web content, keep native UI.
            if cdp_pids:
                native_cdp = [e for e in result if e.pid in cdp_pids]
                native_other = [e for e in result if e.pid not in cdp_pids]
                result = _strip_document_subtrees(native_cdp) + native_other
            cdp_budget = max(
                0, effective_max_elements - len(result),
            )
            cdp_elements = cdp.get_elements(
                app=app, window_id=window_id,
                root_element=root_element,
                tree=tree, max_depth=effective_max_depth,
                max_elements=cdp_budget,
                role=role, states=states, named_only=named_only,
            )
            result.extend(cdp_elements)

    result = _filter(
        result, states=states, role=role,
        named_only=named_only, filter=filter, sort_by=sort_by,
        filter_children=filter_children and tree,
    )

    if format is not None:
        from touchpoint.format.formatter import format_elements

        return format_elements(result, format)

    return result


def element_at(x: int, y: int) -> Element | None:
    """Get the deepest element at a screen coordinate.

    Args:
        x: Horizontal pixel coordinate (screen-absolute).
        y: Vertical pixel coordinate (screen-absolute).

    Returns:
        The deepest :class:`Element` at ``(x, y)``, or ``None``
        if nothing is found.

    Raises:
        BackendUnavailableError: If no backend is available.

    Example::

        >>> import touchpoint as tp
        >>> tp.element_at(500, 300)
        Element('Send', role=button, app='Slack')
    """
    # Route to the backend that owns the topmost window at (x, y).
    #
    # Desktop shells (plasmashell, kwin, mutter, gnome-shell) create
    # AT-SPI "windows" for panels and overlays that sit on top of
    # everything — pure geometry checks always misroute.
    #
    # On Linux/X11 we use _NET_CLIENT_LIST_STACKING (WM stacking
    # order) to find the topmost *real* application window at the
    # point, then check whether its PID belongs to CDP.
    cdp = _get_cdp()
    if cdp is not None:
        cdp_pids: set[int] = cdp.get_owned_pids()
        if cdp_pids:
            top_pid = _topmost_pid_at(x, y)
            if top_pid is not None and top_pid in cdp_pids:
                return cdp.get_element_at(x, y)

    # Not a CDP window (or stacking lookup failed) — platform backend.
    return _get_backend().get_element_at(x, y)


# ---------------------------------------------------------------------------
# Finding / matching
# ---------------------------------------------------------------------------


def find(
    query: str,
    app: str | None = None,
    window_id: str | None = None,
    states: list[State] | None = None,
    role: Role | None = None,
    max_results: int | None = None,
    fields: list[str] | None = None,
    filter: Callable[[Element], bool] | None = None,
    format: str | None = None,
    source: str = "full",
) -> list[Element] | str:
    """Search for elements by name using the matching pipeline.

    First retrieves elements from the backend (scoped by ``app`` /
    ``window_id``), then applies ``states`` / ``role`` filtering,
    and finally runs the matching pipeline (exact → contains → contains-words → fuzzy).
    Results are sorted best-first.

    Args:
        query: The search string (e.g. ``"Send"``, ``"submit"``).
        app: Only search within this application (case-insensitive).
        window_id: Only search within this window.
        states: Only match elements that have **all** of these states.
            ``None`` means no filtering.
        role: Only match elements with this role.
        max_results: Maximum number of matches to return.  ``None``
            returns all matches.
        fields: Which element fields to search.  A list of field
            names from ``["name", "value", "description"]``.
            Defaults to ``["name"]``.  When multiple fields are
            specified, the matcher uses the **best** score across
            all fields for each element.

            Example: ``fields=["name", "value"]`` finds a text
            field whose *value* contains the query, even if its
            *name* (label) does not.
        filter: An arbitrary callable ``(Element) → bool``.  Only
            elements for which the callable returns ``True`` are
            kept.  Applied **after** matching, so the callable
            receives fully matched elements.  Default ``None``.
        format: If set, return a formatted string instead of a list.
            One of ``"flat"`` or ``"json"``.  ``"tree"`` is not
            supported here — search results are ranked, not
            hierarchical.
        source: Controls which backend(s) provide the search pool.
            ``"full"`` (default) merges CDP + native.  ``"ax"`` is
            CDP AX tree only.  ``"native"`` is platform only.
            ``"dom"`` is the CDP DOM walker.  See :func:`elements`.

    Returns:
        List of :class:`Element` instances sorted by match quality
        (best first), or a formatted string if ``format`` is
        specified.  Empty list if nothing matched.

    Raises:
        BackendUnavailableError: If no backend is available.
        ValueError: If *format* is ``"tree"`` or an unrecognised name,
            or *fields* contains an invalid field name.

    Example::

        >>> import touchpoint as tp
        >>> tp.find("Send", role=tp.Role.BUTTON)
        [Element('Send', role=button, app='Slack')]
        >>> tp.find("hello@email.com", fields=["value"], app="Chrome")
        >>> tp.find("lol", app="Discord", source="dom")
    """
    if source not in _VALID_SOURCES:
        raise ValueError(
            f"source must be one of {_VALID_SOURCES!r}, got {source!r}"
        )

    if format == "tree":
        msg = "tree format is not supported by find() — results are ranked, not hierarchical"
        raise ValueError(msg)

    _valid_fields = {"name", "value", "description"}
    search_fields = fields or ["name"]
    bad = set(search_fields) - _valid_fields
    if bad:
        msg = f"invalid fields {bad!r} — allowed: {sorted(_valid_fields)}"
        raise ValueError(msg)

    from touchpoint.matching.matcher import match

    backend = _get_backend()

    # Some backends (AT-SPI2) support a lightweight walk that only
    # fetches name/role/states — much cheaper.  If available, use it
    # and inflate only the matches.  Otherwise fall back to a full walk.
    #
    # Lightweight mode only has name, so if the user wants to search
    # other fields we must do a full walk.
    use_lightweight = search_fields == ["name"]
    # When searching only the name field, unnamed elements can't match.
    skip_unnamed = "name" in search_fields and len(search_fields) == 1

    # DOM source — delegate to elements() which handles CDP routing.
    if source == "dom":
        pool = elements(
            app=app, window_id=window_id,
            role=role, states=states,
            named_only=skip_unnamed,
            source="dom",
        )
        if not isinstance(pool, list):
            pool = []  # safety
        pool = _filter(pool, states=states, role=role)
        text_fn_dom: Callable[[Element], list[str]] | None = None
        if search_fields != ["name"]:
            def _text_fn_dom(el: Element) -> list[str]:
                texts: list[str] = []
                for f in search_fields:
                    v = getattr(el, f, None)
                    if v:
                        texts.append(v)
                return texts
            text_fn_dom = _text_fn_dom
        results = match(
            query, pool, max_results=max_results,
            threshold=_config["fuzzy_threshold"],
            text_fn=text_fn_dom,
        )
        result = [r.element for r in results]
        if filter is not None:
            result = [el for el in result if filter(el)]
        if format is not None:
            from touchpoint.format.formatter import format_elements
            return format_elements(result, format)
        return result

    # Determine which backend to query based on scope + source.
    _cdp_native_merge = False
    if source == "native":
        # Platform backend only, no CDP.
        backends_to_search: list[tuple[Backend, bool]] = [
            (backend, use_lightweight),
        ]
    elif window_id is not None and _is_cdp_id(window_id):
        # Scoped to a CDP window — only search CDP.
        cdp = _get_cdp()
        if cdp is None:
            raise BackendUnavailableError(
                backend="cdp",
                reason="CDP backend required for window "
                f"{window_id!r}",
            )
        backends_to_search = [
            (cdp, use_lightweight),
        ]
    elif source == "ax":
        # CDP AX only — no native merge.
        cdp = _get_cdp()
        if cdp is None:
            raise BackendUnavailableError(
                backend="cdp",
                reason="source='ax' requires a CDP backend",
            )
        if app is None or _is_cdp_app(app):
            backends_to_search = [(cdp, use_lightweight)]
        else:
            # source="ax" explicitly requests CDP accessibility tree.
            # Non-CDP apps don't have one — raise.
            from touchpoint.core.exceptions import TouchpointError
            raise TouchpointError(
                f"source='ax' is only supported for CDP-backed "
                f"apps, but {app!r} is not a CDP app"
            )
    elif app is not None and _is_cdp_app(app):
        # source="full" — CDP for web content, platform for native UI.
        cdp = _get_cdp()
        if cdp is None:
            raise BackendUnavailableError(
                backend="cdp",
                reason=f"CDP backend required for app {app!r}",
            )
        backends_to_search = [(cdp, use_lightweight)]
        _cdp_native_merge = True  # flag: merge native UI after pool init
    else:
        backends_to_search = [(backend, use_lightweight)]
        cdp = _get_cdp()
        if (
            source != "native"
            and cdp is not None
            and window_id is None
        ):
            backends_to_search.append((cdp, use_lightweight))

    # Collect elements from all relevant backends.
    pool: list[Element] = []
    inflate_backends: dict[str, Backend] = {}
    inflate = use_lightweight
    for be, lw in backends_to_search:
        _get = be.get_elements
        if lw:
            elems = _get(app=app, window_id=window_id,
                         lightweight=True,
                         role=role, states=states,
                         named_only=skip_unnamed)
        else:
            elems = _get(app=app, window_id=window_id,
                         role=role, states=states,
                         named_only=skip_unnamed)
        pool.extend(elems)
        # Track which backend owns which elements for inflate.
        for el in elems:
            inflate_backends[el.backend] = be

    # For CDP apps, merge native UI from the platform backend.
    # skip_subtree_roles prunes the DOCUMENT subtree that CDP already
    # covers — the walker emits the DOCUMENT element but does not
    # descend into its (expensive) children.
    if _cdp_native_merge:
        try:
            platform_app = _resolve_platform_app(app)
            _native_kw: dict[str, Any] = dict(
                app=platform_app, window_id=window_id,
                role=role, states=states,
                named_only=skip_unnamed,
                skip_subtree_roles={Role.DOCUMENT},
            )
            if use_lightweight:
                _native_kw["lightweight"] = True
            native = backend.get_elements(**_native_kw)
            native = _strip_document_subtrees(native)
            pool.extend(native)
            for el in native:
                inflate_backends[el.backend] = backend
        except Exception:
            pass  # platform backend unavailable; CDP results suffice

    # When merging, strip document subtrees from platform elements for
    # PIDs owned by CDP — CDP covers web content, platform backend
    # covers native UI (title bar, dialogs, toolbars, etc.).
    if len(backends_to_search) > 1:
        cdp_obj = _get_cdp()
        if cdp_obj is not None:
            cdp_pids: set[int] = cdp_obj.get_owned_pids()
            if cdp_pids:
                cdp_elems = [e for e in pool if e.backend == "cdp"]
                native_cdp_pid = [
                    e for e in pool
                    if e.backend != "cdp" and e.pid in cdp_pids
                ]
                native_other = [
                    e for e in pool
                    if e.backend != "cdp" and e.pid not in cdp_pids
                ]
                pool = (
                    cdp_elems
                    + _strip_document_subtrees(native_cdp_pid)
                    + native_other
                )

    pool = _filter(pool, states=states, role=role)

    # Build a text-extraction function for the matcher.
    # For the default case (name-only) we pass None to let the
    # matcher use its fast built-in path.
    text_fn: Callable[[Element], list[str]] | None = None
    if search_fields != ["name"]:
        def _text_fn(el: Element) -> list[str]:
            texts: list[str] = []
            for f in search_fields:
                v = getattr(el, f, None)
                if v:
                    texts.append(v)
            return texts
        text_fn = _text_fn

    results = match(
        query, pool, max_results=max_results,
        threshold=_config["fuzzy_threshold"],
        text_fn=text_fn,
    )
    if inflate:
        result = []
        for r in results:
            be = inflate_backends.get(r.element.backend, backend)
            result.append(be.inflate_element(r.element))
    else:
        result = [r.element for r in results]

    if filter is not None:
        result = [el for el in result if filter(el)]

    if format is not None:
        from touchpoint.format.formatter import format_elements

        return format_elements(result, format)

    return result


# ---------------------------------------------------------------------------
# Waiting
# ---------------------------------------------------------------------------


def wait_for(
    query: str | list[str],
    *,
    app: str | None = None,
    window_id: str | None = None,
    states: list[State] | None = None,
    role: Role | None = None,
    fields: list[str] | None = None,
    mode: str = "any",
    timeout: float = 10.0,
    poll: float = 0.5,
    source: str = "full",
    max_results: int | None = None,
    wait_for_new: bool = False,
    gone: bool = False,
) -> list[Element] | bool:
    """Wait for elements matching *query* to appear or disappear.

    Polls :func:`find` every *poll* seconds until the condition is
    met or *timeout* is exceeded.

    **Appear mode** (``gone=False``, default):

    When *query* is a list, *mode* controls the logic:

    - ``"any"`` (default) — **race**: return as soon as **any**
      query produces a match.  This is the common pattern for
      branching (e.g. ``["Success", "Error"]``).
    - ``"all"`` — **convergence**: return only when **every**
      query has produced at least one match.

    **Disappear mode** (``gone=True``):

    When *query* is a list, *mode* controls the logic:

    - ``"all"`` — return when **every** query returns zero results.
    - ``"any"`` (default) — return as soon as **any** query returns
      zero results.

    Args:
        query: Search string or list of strings.
        app: Only search within this application (case-insensitive).
        window_id: Only search within this window.
        states: Only match elements that have **all** of these
            states.
        role: Only match elements with this role.
        fields: Which element fields to search.  Forwarded to
            :func:`find`.  Defaults to ``["name"]``.
        mode: ``"any"`` or ``"all"``.  Only meaningful when
            *query* is a list.  Default ``"any"``.
        timeout: Maximum seconds to wait.  Default ``10``.
        poll: Seconds between polls.  Default ``0.5``.
        source: Controls which backend(s) provide elements.
            ``"full"`` (default), ``"ax"``, ``"native"``, or
            ``"dom"``.  See :func:`elements`.
        max_results: Maximum number of matches to return.
            ``None`` returns all.  Useful for agents that only
            need confirmation that something exists.
        wait_for_new: If ``True``, ignores elements already
            present when the call starts.  Snapshots current
            match IDs, then waits for matches whose IDs are
            **not** in the snapshot.  Useful when the query
            already matches on-screen content but you're
            waiting for new content (e.g. page navigation).
            Default ``False``.  Only applies when ``gone=False``.
        gone: If ``True``, wait for matching elements to
            **disappear** instead of appear.  Returns ``True``
            when the condition is met.  Default ``False``.

    Returns:
        When ``gone=False``: matching :class:`Element` instances.
        When ``gone=True``: ``True`` when elements have disappeared.

    Raises:
        TimeoutError: If the condition is not met within *timeout*.
        ValueError: If *mode* is not ``"any"`` or ``"all"``.

    Example::

        >>> import touchpoint as tp
        >>> tp.wait_for("Success", timeout=15)
        >>> tp.wait_for(["Success", "Error"])       # race
        >>> tp.wait_for(["Header", "Footer"], mode="all")
        >>> tp.wait_for("hello@email.com", fields=["value"])
        >>> tp.wait_for("Article", wait_for_new=True)  # ignore existing
        >>> tp.wait_for("Result", max_results=3)
        >>> tp.wait_for("Loading", gone=True)       # wait for disappearance
        >>> tp.wait_for(["Loading", "Spinner"], gone=True)  # all gone
    """
    import time

    if mode not in ("any", "all"):
        raise ValueError(f"mode must be 'any' or 'all', got {mode!r}")

    queries = [query] if isinstance(query, str) else query
    deadline = time.monotonic() + timeout
    find_kw: dict = dict(app=app, window_id=window_id,
                         states=states, role=role,
                         source=source)
    if fields is not None:
        find_kw["fields"] = fields

    if gone:
        # --- Disappear mode ---
        while True:
            if mode == "any":
                if any(not find(q, **find_kw) for q in queries):
                    return True
            else:  # "all"
                if all(not find(q, **find_kw) for q in queries):
                    return True

            if time.monotonic() >= deadline:
                msg = (
                    f"wait_for({query!r}, gone=True, mode={mode!r}) "
                    f"timed out after {timeout}s"
                )
                raise TimeoutError(msg)
            time.sleep(poll)

    # --- Appear mode ---
    # Snapshot existing match IDs so we can filter them out.
    existing_ids: set[str] = set()
    if wait_for_new:
        for q in queries:
            for el in find(q, **find_kw):
                existing_ids.add(el.id)

    def _apply_limits(results: list[Element]) -> list[Element]:
        if wait_for_new:
            results = [e for e in results if e.id not in existing_ids]
        if max_results is not None and len(results) > max_results:
            results = results[:max_results]
        return results

    while True:
        if mode == "any":
            for q in queries:
                results = _apply_limits(find(q, **find_kw))
                if results:
                    return results
        else:  # "all"
            all_results: list[Element] = []
            all_matched = True
            for q in queries:
                results = _apply_limits(find(q, **find_kw))
                if results:
                    all_results.extend(results)
                else:
                    all_matched = False
            if all_matched and all_results:
                if max_results is not None:
                    all_results = all_results[:max_results]
                return all_results

        if time.monotonic() >= deadline:
            msg = (
                f"wait_for({query!r}, mode={mode!r}) timed out "
                f"after {timeout}s"
            )
            raise TimeoutError(msg)
        time.sleep(poll)


def wait_for_app(
    app: str,
    *,
    timeout: float = 10.0,
    poll: float = 0.5,
    gone: bool = False,
) -> bool:
    """Wait for an application to appear or disappear.

    Polls :func:`apps` every *poll* seconds until the application
    is found (or gone) or *timeout* is exceeded.

    Args:
        app: Application name to wait for (case-insensitive
            substring match against :func:`apps` results).
        timeout: Maximum seconds to wait.  Default ``10``.
        poll: Seconds between polls.  Default ``0.5``.
        gone: If ``True``, wait for the app to **disappear**
            instead of appear.  Default ``False``.

    Returns:
        ``True`` when the condition is met.

    Raises:
        TimeoutError: If the condition is not met within *timeout*.

    Example::

        >>> import touchpoint as tp
        >>> tp.wait_for_app("Firefox", timeout=15)
        >>> tp.wait_for_app("Firefox", gone=True)  # wait for close
    """
    import time

    app_lower = app.lower()
    deadline = time.monotonic() + timeout

    while True:
        current = apps()
        found = any(app_lower in a.lower() for a in current)

        if gone and not found:
            return True
        if not gone and found:
            return True

        if time.monotonic() >= deadline:
            action = "disappear" if gone else "appear"
            msg = (
                f"wait_for_app({app!r}, gone={gone}) timed out "
                f"after {timeout}s waiting for app to {action}"
            )
            raise TimeoutError(msg)
        time.sleep(poll)


def wait_for_window(
    title: str,
    *,
    app: str | None = None,
    timeout: float = 10.0,
    poll: float = 0.5,
    gone: bool = False,
) -> Window | bool:
    """Wait for a window to appear or disappear.

    Polls :func:`windows` every *poll* seconds until a window with
    a matching title is found (or gone) or *timeout* is exceeded.

    Args:
        title: Window title to search for (case-insensitive
            substring match).
        app: Only look for windows in this application.
        timeout: Maximum seconds to wait.  Default ``10``.
        poll: Seconds between polls.  Default ``0.5``.
        gone: If ``True``, wait for the window to **disappear**
            instead of appear.  Default ``False``.

    Returns:
        When ``gone=False``: the matching :class:`Window`.
        When ``gone=True``: ``True`` when the window has
        disappeared.

    Raises:
        TimeoutError: If the condition is not met within *timeout*.

    Example::

        >>> import touchpoint as tp
        >>> w = tp.wait_for_window("Settings")
        >>> tp.wait_for_window("Settings", gone=True)
        >>> w = tp.wait_for_window("Preferences", app="Firefox")
    """
    import time

    title_lower = title.lower()
    deadline = time.monotonic() + timeout

    while True:
        current = windows()
        if app:
            app_lower = app.lower()
            current = [w for w in current if app_lower in w.app.lower()]

        match = None
        for w in current:
            if title_lower in w.title.lower():
                match = w
                break

        if gone and match is None:
            return True
        if not gone and match is not None:
            return match

        if time.monotonic() >= deadline:
            action = "disappear" if gone else "appear"
            msg = (
                f"wait_for_window({title!r}, gone={gone}) timed out "
                f"after {timeout}s waiting for window to {action}"
            )
            raise TimeoutError(msg)
        time.sleep(poll)


# ---------------------------------------------------------------------------
# Window actions
# ---------------------------------------------------------------------------


def activate_window(window: Window | str) -> bool:
    """Bring a window to the foreground.

    Tries the backend's native activation first (e.g. AT-SPI2's
    ``activate`` action or ``grab_focus()``).  Falls back to the
    input provider's OS-level approach (e.g. ``xdotool`` search
    by title and PID) when native activation is unavailable.

    Args:
        window: A :class:`Window` instance (from :func:`windows`)
            or a window id string.

    Returns:
        ``True`` if the window was activated, ``False`` if
        activation failed or is not supported.

    Raises:
        BackendUnavailableError: If no backend is available.
        ValueError: If a string id is passed and no matching
            window can be found.

    Example::

        >>> import touchpoint as tp
        >>> wins = tp.windows()
        >>> chrome = [w for w in wins if w.app == "Google Chrome"][0]
        >>> tp.activate_window(chrome)
        True
    """
    win_id = window.id if isinstance(window, Window) else window

    # Route CDP windows to the CDP backend.
    if _is_cdp_id(win_id):
        cdp = _get_cdp()
        if cdp is not None:
            return cdp.activate_window(win_id)
        return False

    backend = _get_backend()

    if isinstance(window, str):
        # Resolve the Window object from the id so we have
        # title + pid for the InputProvider fallback.
        all_wins = backend.get_windows()
        found = next((w for w in all_wins if w.id == window), None)
        if found is None:
            msg = f"no window found with id {window!r}"
            raise ValueError(msg)
        window = found

    # Primary: backend activation (accessibility layer).
    if backend.activate_window(window.id):
        return True

    # Fallback: InputProvider activation (OS-level).
    provider = _input_provider or _init_input()
    if provider is not None:
        return provider.activate_window(window.title, window.pid)

    return False


# ---------------------------------------------------------------------------
# Actions — element-targeted
# ---------------------------------------------------------------------------

# Convenience action names are resolved to backend-specific action
# strings via Backend.ACTION_ALIASES, defined by each backend.


def _resolve_target(target: Element | str) -> str:
    """Extract an element id from *target*.

    Args:
        target: An :class:`Element` instance or a bare id string.

    Returns:
        The element id string.
    """
    if isinstance(target, Element):
        return target.id
    return target


def _get_element_position(element_id: str) -> tuple[int, int] | None:
    """Get the current screen position of an element by id.

    Uses ``get_element_by_id`` to resolve a fresh position,
    avoiding stale coordinates from an earlier call.

    Returns:
        ``(x, y)`` centre coordinates, or ``None`` if the
        element cannot be found.
    """
    el = _backend_for_id(element_id).get_element_by_id(element_id)
    if el is None:
        return None
    return el.position


def _try_actions(element_id: str, names: list[str]) -> bool:
    """Try each action name in *names* until one succeeds.

    Only treats an explicit ``True`` return as success.  A ``False``
    return is treated the same as an :class:`ActionFailedError` — the
    next alias is tried.  If all aliases fail, raises the last
    captured error so callers can fall back to coordinate input.
    """
    backend = _backend_for_id(element_id)
    last_err: ActionFailedError | None = None
    for name in names:
        try:
            if backend.do_action(element_id, name):
                return True
        except ActionFailedError as exc:
            last_err = exc

    if last_err is not None:
        raise last_err
    raise ActionFailedError(
        action=names[0] if names else "unknown",
        element_id=element_id,
        reason="no action aliases configured" if not names
        else f"all action aliases returned False: {names}",
    )


def click(element: Element | str) -> bool:
    """Click an element.

    Tries native accessibility actions in order: ``click``,
    ``press``, ``activate``.  If all fail and
    ``fallback_input=True`` (the default), falls back to a
    coordinate-based click via the InputProvider.

    Args:
        element: An :class:`Element` or an element id string.

    Returns:
        ``True`` if the click was dispatched.

    Raises:
        ActionFailedError: If no click-like action is available
            and fallback is disabled or unavailable.

    Example::

        >>> import touchpoint as tp
        >>> btn = tp.find("Send", role=tp.Role.BUTTON)[0]
        >>> tp.click(btn)
        True
    """
    eid = _resolve_target(element)
    backend = _backend_for_id(eid)
    try:
        return _try_actions(eid, backend.ACTION_ALIASES["click"])
    except ActionFailedError:
        # No InputProvider fallback for CDP: dispatchMouseEvent scrolls
        # the element into view before clicking; xdotool cannot, and the
        # real CDP failure modes (WebSocket issues) also break position
        # lookup so the fallback would fail at a different point anyway.
        if _is_cdp_id(eid) or not _config["fallback_input"]:
            raise
        pos = _get_element_position(eid)
        if pos is None:
            raise
        _get_input().click_at(*pos)
        return True


def double_click(element: Element | str) -> bool:
    """Double-click an element.

    Tries native accessibility actions first.  Falls back to
    coordinate-based double-click when ``fallback_input=True``.

    Args:
        element: An :class:`Element` or an element id string.

    Returns:
        ``True`` if the double-click was dispatched.

    Example::

        >>> import touchpoint as tp
        >>> tp.double_click("atspi:2269:1:2.1")
        True
    """
    eid = _resolve_target(element)
    backend = _backend_for_id(eid)
    try:
        return _try_actions(eid, backend.ACTION_ALIASES["double_click"])
    except ActionFailedError:
        # Same reasoning as click(): no InputProvider fallback for CDP.
        if _is_cdp_id(eid) or not _config["fallback_input"]:
            raise
        pos = _get_element_position(eid)
        if pos is None:
            raise
        _get_input().double_click_at(*pos)
        return True


def right_click(element: Element | str) -> bool:
    """Right-click (context menu) on an element.

    Tries native accessibility actions first.  Falls back to
    coordinate-based right-click when ``fallback_input=True``.

    Args:
        element: An :class:`Element` or an element id string.

    Returns:
        ``True`` if the right-click was dispatched.

    Example::

        >>> import touchpoint as tp
        >>> tp.right_click("atspi:2269:1:4.0")
        True
    """
    eid = _resolve_target(element)
    backend = _backend_for_id(eid)
    try:
        return _try_actions(eid, backend.ACTION_ALIASES["right_click"])
    except ActionFailedError:
        # Same reasoning as click(): no InputProvider fallback for CDP.
        if _is_cdp_id(eid) or not _config["fallback_input"]:
            raise
        pos = _get_element_position(eid)
        if pos is None:
            raise
        _get_input().right_click_at(*pos)
        return True


def set_value(
    element: Element | str, value: str, *, replace: bool = False,
) -> bool:
    """Set the text content of an editable element.

    By default **inserts** *value* at the current cursor position.
    Pass ``replace=True`` to clear the field first and replace its
    entire content.

    .. note:: On Windows (UIA), the Value pattern is used for
       single-line controls (e.g. text fields, combo boxes).
       For multi-line edits, the backend falls back to
       ``InputProvider`` just like the public fallback path.

    If the native ``EditableText`` interface is unavailable and
    ``fallback_input=True``, falls back to: focus the element →
    select-all (if *replace*) → type the text via InputProvider.

    Args:
        element: An :class:`Element` or an element id string.
        value: The text to write.
        replace: If ``True``, replace the field's current content.
            If ``False`` (default), insert at cursor position.

    Returns:
        ``True`` if the value was set successfully.

    Raises:
        ActionFailedError: If the element does not support text
            editing and fallback is disabled or unavailable.

    Example::

        >>> import touchpoint as tp
        >>> field = tp.find("Search", role=tp.Role.TEXT_FIELD)[0]
        >>> tp.set_value(field, "hello")
        True
        >>> tp.set_value(field, "world", replace=True)
        True
    """
    eid = _resolve_target(element)
    try:
        return _backend_for_id(eid).set_value(eid, value, replace=replace)
    except ActionFailedError:
        # No InputProvider fallback for CDP: the common failure modes
        # (element not in AX tree, DOM.focus rejected) also cause the
        # fallback focus() to silently fail, so xdotool ends up typing
        # into whatever Chrome currently has focused — wrong element,
        # silent data corruption.
        if _is_cdp_id(eid) or not _config["fallback_input"]:
            raise
        # Fallback: try native focus (best-effort) → select-all
        # (if replace) → type.  We don't bail if focus fails —
        # the element may already be focused, or the agent may
        # have clicked it before calling set_value.
        try:
            focus(element)
        except ActionFailedError:
            pass  # best-effort — continue to type anyway
        inp = _get_input()
        if replace:
            inp.hotkey(*inp.SELECT_ALL_KEYS)
        inp.type_text(value)
        return True


def select_text(
    element: Element | str,
    text: str,
    *,
    occurrence: int = 1,
) -> bool:
    """Select a substring within an element's text content.

    Reads the element's text, locates the *occurrence*-th match
    of *text*, and applies a native text selection over that range.

    Args:
        element: An :class:`Element` or an element id string.
        text: The substring to select.
        occurrence: Which occurrence to select (1-based).
            Defaults to the first.

    Returns:
        ``True`` if the selection was applied.

    Raises:
        ActionFailedError: If the element does not support text
            selection, the substring is not found, or the backend
            rejects the selection.

    Example::

        >>> import touchpoint as tp
        >>> tp.select_text("atspi:2269:1:2.1", "hello")
        True
        >>> tp.select_text("atspi:2269:1:2.1", "world", occurrence=2)
        True
    """
    from touchpoint.core.exceptions import ActionFailedError

    eid = _resolve_target(element)

    if not text:
        raise ActionFailedError(
            action="select_text",
            element_id=eid,
            reason="text must be a non-empty string",
        )
    if occurrence < 1:
        raise ActionFailedError(
            action="select_text",
            element_id=eid,
            reason=f"occurrence must be >= 1, got {occurrence}",
        )

    # Get the element's current text content.
    el = get_element(eid)
    if el is None:
        raise ActionFailedError(
            action="select_text",
            element_id=eid,
            reason="element not found",
        )

    content = el.value if el.value is not None else el.name
    if not content:
        raise ActionFailedError(
            action="select_text",
            element_id=eid,
            reason="element has no text content",
        )

    # Find the n-th occurrence of the substring.
    start = -1
    for _ in range(occurrence):
        start = content.find(text, start + 1)
        if start == -1:
            raise ActionFailedError(
                action="select_text",
                element_id=eid,
                reason=f"substring {text!r} not found "
                       f"(occurrence {occurrence})",
            )

    end = start + len(text)
    return _backend_for_id(eid).select_text(eid, start, end)


def focus(element: Element | str) -> bool:
    """Move keyboard focus to an element.

    Uses the backend's native focus mechanism (e.g.
    ``Component.grab_focus()`` on AT-SPI2,
    ``SetFocus()`` on Windows UIA, ``DOM.focus()`` on CDP).

    No InputProvider fallback — clicking has semantic side
    effects beyond focus (opens dropdowns, toggles checkboxes).

    Args:
        element: An :class:`Element` or an element id string.

    Returns:
        ``True`` if focus was moved.

    Raises:
        ActionFailedError: If the element cannot receive focus.

    Example::

        >>> import touchpoint as tp
        >>> tp.focus("atspi:2269:1:1.2")
        True
    """
    eid = _resolve_target(element)
    return _backend_for_id(eid).focus_element(eid)


def set_numeric_value(element: Element | str, value: float) -> bool:
    """Set the numeric value of a range element (slider, spinbox).

    No InputProvider fallback — numeric ranges require the native
    Value interface.

    Args:
        element: An :class:`Element` or an element id string.
        value: The numeric value to set.

    Returns:
        ``True`` if the value was set successfully.

    Raises:
        ActionFailedError: If the element does not support
            numeric values.

    Example::

        >>> import touchpoint as tp
        >>> slider = tp.find("Volume", role=tp.Role.SLIDER)[0]
        >>> tp.set_numeric_value(slider, 75.0)
        True
    """
    eid = _resolve_target(element)
    return _backend_for_id(eid).set_numeric_value(eid, value)


def action(element: Element | str, action_name: str) -> bool:
    """Perform a raw accessibility action by exact name.

    Unlike the convenience functions (:func:`click`, :func:`focus`,
    etc.), this does **not** try aliases — it calls exactly the
    action you specify.  Use this when you know the precise action
    name from :attr:`Element.actions`.

    No InputProvider fallback — raw actions are native-only.

    Args:
        element: An :class:`Element` or an element id string.
        action_name: Exact action name (e.g. ``"activate"``,
            ``"ShowMenu"``, ``"expand or collapse"``).

    Returns:
        ``True`` if the action was dispatched.

    Raises:
        ActionFailedError: If the element does not support this
            action.

    Example::

        >>> import touchpoint as tp
        >>> tp.action("atspi:2269:1:5.0", "expand or collapse")
        True
    """
    eid = _resolve_target(element)
    return _backend_for_id(eid).do_action(eid, action_name)


# ---------------------------------------------------------------------------
# Element lookup by id
# ---------------------------------------------------------------------------


def get_element(element_id: str, *, format: str | None = None) -> Element | str | None:
    """Retrieve a single element by its id.

    Returns a **fresh** snapshot of the element with current
    position, states, value, etc.  Useful for re-checking an
    element after performing an action, or when you only have
    a string id from a previous call.

    Args:
        element_id: The ``id`` of the target element (e.g.
            ``"atspi:2269:1:2.1.0"``).
        format: If set, return a formatted string instead of an
            :class:`Element`.  One of ``"flat"`` or ``"json"``.
            ``"tree"`` is not supported here — a single element
            has no hierarchy.

    Returns:
        The :class:`Element` if found (or a formatted string when
        *format* is specified), ``None`` if the element is not
        found.

    Raises:
        BackendUnavailableError: If no backend is available.
        ValueError: If *format* is ``"tree"`` or an unrecognised
            name.

    Example::

        >>> import touchpoint as tp
        >>> el = tp.get_element("atspi:2269:1:2.1")
        >>> el.value
        'hello world'
        >>> tp.get_element("atspi:2269:1:2.1", format="flat")
        '[atspi:2269:1:2.1] [BUTTON] Minimise'
    """
    if format == "tree":
        msg = "tree format is not supported by get_element() — a single element has no hierarchy"
        raise ValueError(msg)

    el = _backend_for_id(element_id).get_element_by_id(element_id)

    if el is not None and format is not None:
        from touchpoint.format.formatter import format_elements

        return format_elements([el], format)

    return el


# ---------------------------------------------------------------------------
# Screenshot
# ---------------------------------------------------------------------------


def screenshot(
    *,
    app: str | None = None,
    window_id: str | None = None,
    element: Element | str | None = None,
    padding: int = 0,
    monitor: int | None = None,
) -> Any:
    # Real return type is PIL.Image.Image but we use Any to
    # avoid a hard Pillow import.  See TYPE_CHECKING block below.
    """Capture screen pixels and return a ``PIL.Image.Image``.

    With no arguments, captures the entire virtual desktop (all
    monitors).  Use the optional parameters to crop to a specific
    region.

    Only one of *app*, *window_id*, *element*, or *monitor* may
    be specified.  Passing more than one raises ``ValueError``.

    Args:
        app: Crop to the active window of this application
            (case-insensitive).  If the app has multiple windows,
            prefers the active one, then the largest visible one.
        window_id: Crop to a specific window by its id.
        element: Crop to a specific element's bounding box.  Accepts
            an :class:`Element` object or a bare id string.
        padding: Extra pixels to include around the crop region on
            all sides.  Must be non-negative.  Left and top edges
            are clamped to zero.  Default ``0``.
        monitor: Capture only this monitor (0-indexed).  Use
            :func:`monitor_count` to discover how many are available.

    Returns:
        A ``PIL.Image.Image`` in RGB mode.

    Raises:
        ImportError: If Pillow is not installed.
        ValueError: If more than one scope parameter is given,
            if *monitor* index is out of range, if *app* /
            *window_id* / *element* cannot be found, if the
            resolved target has zero size, or if *padding* is
            negative.
        BackendUnavailableError: If cropping by app/window/element
            and no backend is available.

    .. note::
        Screenshot captures the screen framebuffer — whatever pixels
        are actually rendered at the target coordinates.  If the
        target window is behind another window, you get the pixels
        of the window on top.

    Examples::

        >>> import touchpoint as tp
        >>> img = tp.screenshot()                          # full desktop
        >>> img = tp.screenshot(app="Firefox")             # Firefox window
        >>> img = tp.screenshot(element=button, padding=20)  # button + context
        >>> img = tp.screenshot(monitor=0)                 # primary monitor
    """
    from touchpoint.utils.screenshot import take_screenshot

    if padding < 0:
        raise ValueError(
            f"padding must be non-negative, got {padding}"
        )

    # --- Validate mutually exclusive scope params ---

    scope_params = {
        "app": app, "window_id": window_id,
        "element": element, "monitor": monitor,
    }
    provided = [k for k, v in scope_params.items() if v is not None]
    if len(provided) > 1:
        raise ValueError(
            f"screenshot() accepts at most one scope parameter, "
            f"got: {', '.join(provided)}"
        )

    # --- Resolve crop region ---

    region: tuple[int, int, int, int] | None = None

    # --- CDP-native screenshot path ---
    # If target is a CDP element or window, use Page.captureScreenshot
    # for a clean viewport capture immune to window occlusion.

    if element is not None:
        eid = element.id if isinstance(element, Element) else element
        if _is_cdp_id(eid):
            cdp = _get_cdp()
            if cdp is not None:
                png_bytes = cdp.take_screenshot(
                    element_id=eid, padding=padding,
                )
                return _png_bytes_to_image(png_bytes)

    if window_id is not None and _is_cdp_id(window_id):
        cdp = _get_cdp()
        if cdp is not None:
            parts = window_id.split(":", 3)
            port = int(parts[1])
            tid = parts[2]
            png_bytes = cdp.take_screenshot(port=port, target_id=tid)
            return _png_bytes_to_image(png_bytes)

    if app is not None and _is_cdp_app(app):
        cdp = _get_cdp()
        if cdp is not None:
            # Prefer the active window, then the largest visible one.
            app_lower = app.lower()
            candidates = [
                w for w in cdp.get_windows()
                if w.app.lower() == app_lower
            ]
            best = None
            for w in candidates:
                if w.is_active:
                    best = w
                    break
            if best is None:
                visible = [
                    w for w in candidates
                    if w.is_visible and w.size[0] > 0 and w.size[1] > 0
                ]
                if visible:
                    best = max(visible,
                               key=lambda w: w.size[0] * w.size[1])
            if best is None and candidates:
                best = candidates[0]
            if best is not None:
                parts = best.id.split(":", 3)
                port = int(parts[1])
                tid = parts[2]
                png_bytes = cdp.take_screenshot(
                    port=port, target_id=tid,
                )
                return _png_bytes_to_image(png_bytes)

    # --- Platform screenshot path (OS-level framebuffer capture) ---

    if element is not None:
        # Element crop: resolve from object or id string.
        if isinstance(element, str):
            el = _backend_for_id(element).get_element_by_id(element)
            if el is None:
                raise ValueError(
                    f"element {element!r} not found"
                )
        else:
            el = element

        left, top, w, h = el.bounds
        if w <= 0 or h <= 0:
            raise ValueError(
                f"element {el.name!r} has zero size ({w}x{h}) "
                f"— cannot screenshot"
            )
        region = (left, top, left + w, top + h)

    elif window_id is not None:
        # Window crop by id.
        win = _find_window(window_id=window_id)
        if win is None:
            raise ValueError(
                f"window {window_id!r} not found"
            )
        ww, wh = win.size
        if ww <= 0 or wh <= 0:
            raise ValueError(
                f"window {window_id!r} has zero size ({ww}x{wh}) "
                f"— cannot screenshot"
            )
        wx, wy = win.position
        region = (wx, wy, wx + ww, wy + wh)

    elif app is not None:
        # App crop: find the best window for this app.
        win = _find_window(app=app)
        if win is None:
            raise ValueError(
                f"no window found for app {app!r}"
            )
        ww, wh = win.size
        if ww <= 0 or wh <= 0:
            raise ValueError(
                f"window for app {app!r} has zero size "
                f"({ww}x{wh}) — cannot screenshot"
            )
        wx, wy = win.position
        region = (wx, wy, wx + ww, wy + wh)

    elif monitor is not None:
        # Monitor crop.
        from touchpoint.utils.screenshot import get_monitor_regions

        regions = get_monitor_regions()
        if monitor < 0 or monitor >= len(regions):
            raise ValueError(
                f"monitor {monitor} out of range "
                f"(0–{len(regions) - 1})"
            )
        region = regions[monitor]

    # --- Apply padding ---

    if region is not None and padding > 0:
        left, top, right, bottom = region
        region = (
            max(0, left - padding),
            max(0, top - padding),
            right + padding,
            bottom + padding,
        )

    return take_screenshot(region=region)


def monitor_count() -> int:
    """Return the number of physical monitors detected.

    Uses ``screeninfo`` if available, otherwise returns ``1``
    (the full virtual desktop as a single monitor).

    Returns:
        Number of monitors.

    Raises:
        ImportError: If neither ``screeninfo`` nor Pillow is
            installed.
    """
    from touchpoint.utils.screenshot import get_monitor_regions

    return len(get_monitor_regions())


def _find_window(
    *,
    app: str | None = None,
    window_id: str | None = None,
) -> Window | None:
    """Find the best window for a screenshot crop.

    When *app* is given, prefers the active window, then the
    largest visible window (by area).  Windows with zero size
    are skipped when larger alternatives exist.

    Args:
        app: Application name (case-insensitive match).
        window_id: Exact window id.

    Returns:
        A :class:`Window`, or ``None`` if no match is found.
    """
    all_windows = windows()  # Uses the merged windows() function

    if window_id is not None:
        for w in all_windows:
            if w.id == window_id:
                return w
        return None

    if app is not None:
        app_lower = app.lower()
        candidates = [
            w for w in all_windows if w.app.lower() == app_lower
        ]
        if not candidates:
            return None

        # Prefer active window.
        for w in candidates:
            if w.is_active:
                return w

        # Fall back to largest visible window (by pixel area).
        visible = [
            w for w in candidates
            if w.is_visible and w.size[0] > 0 and w.size[1] > 0
        ]
        if visible:
            return max(visible, key=lambda w: w.size[0] * w.size[1])

        # Last resort: largest window regardless of visibility
        # (may have zero size — callers must check).
        return max(candidates, key=lambda w: w.size[0] * w.size[1])

    return None


# ---------------------------------------------------------------------------
# Raw input — coordinate / keyboard (InputProvider)
# ---------------------------------------------------------------------------


def type_text(text: str) -> None:
    """Type a string into the currently focused widget.

    Uses the platform's raw input simulation (e.g. ``xdotool`` on
    Linux, ``SendInput`` on Windows).  No element targeting —
    keystrokes go to whatever has keyboard focus.

    Special characters are converted to keystrokes:

    - ``\\n`` — Enter (line break)
    - ``\\t`` — Tab (move to next field)
    - ``\\b`` — Backspace (delete previous character)

    Args:
        text: The text to type.  Use ``\\n`` for Enter, ``\\t`` for
            Tab, ``\\b`` for Backspace.

    Raises:
        RuntimeError: If no input provider is available.

    Example::

        >>> import touchpoint as tp
        >>> tp.focus("atspi:2269:1:2.1")  # focus a text field first
        >>> tp.type_text("hello world")
        >>> tp.type_text("line one\\nline two")  # Enter between lines
        >>> tp.type_text("Rank\\tLanguage\\tUse\\n")  # fill a row of cells
    """
    _SPECIAL_KEYS = {"\n": "enter", "\t": "tab", "\b": "backspace"}
    inp = _get_input()
    buf: list[str] = []
    for ch in text:
        if ch in _SPECIAL_KEYS:
            if buf:
                inp.type_text("".join(buf))
                buf.clear()
            inp.press_key(_SPECIAL_KEYS[ch])
        else:
            buf.append(ch)
    if buf:
        inp.type_text("".join(buf))


def press_key(key: str) -> None:
    """Press and release a single key.

    Args:
        key: A canonical key name (e.g. ``"enter"``, ``"tab"``,
            ``"escape"``, ``"f5"``, ``"a"``).  Lowercase names
            are normalised to platform-native keysyms by the
            input provider.

    Raises:
        RuntimeError: If no input provider is available.

    Example::

        >>> import touchpoint as tp
        >>> tp.press_key("enter")
    """
    _get_input().press_key(key)


def hotkey(*keys: str) -> None:
    """Press a keyboard combination.

    All keys are held down in order, then released in reverse.

    Args:
        keys: Two or more canonical key names.

    Raises:
        RuntimeError: If no input provider is available.

    Example::

        >>> import touchpoint as tp
        >>> tp.hotkey("ctrl", "s")
        >>> tp.hotkey("ctrl", "shift", "p")
    """
    _get_input().hotkey(*keys)


def click_at(x: int, y: int) -> None:
    """Left-click at screen coordinates.

    Args:
        x: Horizontal pixel coordinate.
        y: Vertical pixel coordinate.

    Raises:
        RuntimeError: If no input provider is available.

    Example::

        >>> import touchpoint as tp
        >>> tp.click_at(500, 300)
    """
    _get_input().click_at(x, y)


def double_click_at(x: int, y: int) -> None:
    """Double-click at screen coordinates.

    Args:
        x: Horizontal pixel coordinate.
        y: Vertical pixel coordinate.

    Raises:
        RuntimeError: If no input provider is available.

    Example::

        >>> import touchpoint as tp
        >>> tp.double_click_at(500, 300)
    """
    _get_input().double_click_at(x, y)


def right_click_at(x: int, y: int) -> None:
    """Right-click at screen coordinates.

    Args:
        x: Horizontal pixel coordinate.
        y: Vertical pixel coordinate.

    Raises:
        RuntimeError: If no input provider is available.

    Example::

        >>> import touchpoint as tp
        >>> tp.right_click_at(500, 300)
    """
    _get_input().right_click_at(x, y)


_SCROLL_DIRECTIONS = frozenset({"up", "down", "left", "right"})


def scroll(
    x: int | None = None,
    y: int | None = None,
    *,
    direction: str,
    amount: int = 3,
) -> None:
    """Scroll at a screen position.

    When *x* and *y* are ``None`` (the default), scrolls at the
    current cursor position without moving it.

    Args:
        x: Horizontal pixel coordinate.  ``None`` means current
            cursor position.
        y: Vertical pixel coordinate.  ``None`` means current
            cursor position.
        direction: One of ``"up"``, ``"down"``, ``"left"``,
            ``"right"``.
        amount: Number of scroll ticks.  Default ``3``.

    Raises:
        RuntimeError: If no input provider is available.
        ValueError: If *direction* is invalid.

    Example::

        >>> import touchpoint as tp
        >>> tp.scroll(500, 300, direction="down", amount=5)
        >>> tp.scroll(direction="down")  # scroll at current cursor
    """
    if direction not in _SCROLL_DIRECTIONS:
        raise ValueError(
            f"invalid scroll direction {direction!r}, "
            f"expected one of {sorted(_SCROLL_DIRECTIONS)}"
        )
    _get_input().scroll(x, y, direction, amount)


def mouse_move(x: int, y: int) -> None:
    """Move the mouse pointer to screen coordinates.

    Args:
        x: Horizontal pixel coordinate.
        y: Vertical pixel coordinate.

    Raises:
        RuntimeError: If no input provider is available.

    Example::

        >>> import touchpoint as tp
        >>> tp.mouse_move(500, 300)
    """
    _get_input().mouse_move(x, y)


# ---------------------------------------------------------------------------
# Filtering helper
# ---------------------------------------------------------------------------


def _filter_children_recursive(
    children: list[Element],
    states: list[State] | None,
    named_only: bool,
    filter_fn: Callable[[Element], bool] | None,
) -> list[Element]:
    """Recursively filter children (states, named_only, filter).

    ``role`` is intentionally **not** applied — children should
    show the internal structure of role-matched parents.
    ``sort_by`` is also skipped — tree order is structural.
    """
    from dataclasses import replace

    result: list[Element] = []
    for el in children:
        if states is not None and not all(s in el.states for s in states):
            continue
        if named_only and not (el.name and el.name.strip()):
            continue
        if filter_fn is not None and not filter_fn(el):
            continue
        if el.children:
            el = replace(
                el,
                children=_filter_children_recursive(
                    el.children, states, named_only, filter_fn,
                ),
            )
        result.append(el)
    return result


def _filter(
    elements: list[Element],
    states: list[State] | None = None,
    role: Role | None = None,
    named_only: bool = False,
    filter: Callable[[Element], bool] | None = None,
    sort_by: str | Callable[[Element], Any] | None = None,
    filter_children: bool = False,
) -> list[Element]:
    """Post-filter, custom-filter, and sort elements.

    Applied in order: ``role`` → ``states`` → ``named_only`` →
    ``filter`` → ``sort_by``.

    When *filter_children* is ``True``, ``states``, ``named_only``,
    and ``filter`` are also applied recursively to each element's
    ``children`` list.  ``role`` and ``sort_by`` are **not** applied
    to children.

    Args:
        elements: Raw element list from the backend.
        states: If not ``None``, keep only elements whose states
            contain **all** of these (AND logic).
        role: If not ``None``, keep only elements with this role.
        named_only: If ``True``, drop elements whose name is
            empty, ``None``, or whitespace-only.
        filter: Optional callable ``(Element) → bool``.  Only
            elements returning ``True`` are kept.
        sort_by: ``"position"`` for reading order (top-to-bottom,
            left-to-right), or a callable ``(Element) → sort_key``.
            ``None`` preserves original order.
        filter_children: If ``True``, recursively apply ``states``,
            ``named_only``, and ``filter`` to children.

    Returns:
        Filtered (and optionally sorted) list.

    Raises:
        ValueError: If *sort_by* is an unrecognised string.
    """
    needs_filter = (
        role is not None
        or states is not None
        or named_only
        or filter is not None
    )
    if not needs_filter and sort_by is None:
        return elements

    # Determine whether children need filtering.
    needs_child_filter = filter_children and (
        states is not None or named_only or filter is not None
    )

    result: list[Element] = []
    for el in elements:
        if role is not None and el.role != role:
            continue
        if states is not None and not all(s in el.states for s in states):
            continue
        if named_only and not (el.name and el.name.strip()):
            continue
        if filter is not None and not filter(el):
            continue
        if needs_child_filter and el.children:
            from dataclasses import replace
            el = replace(
                el,
                children=_filter_children_recursive(
                    el.children, states, named_only, filter,
                ),
            )
        result.append(el)

    if sort_by is not None:
        if sort_by == "position":
            result.sort(key=lambda el: (el.position[1], el.position[0]))
        elif callable(sort_by):
            result.sort(key=sort_by)
        else:
            raise ValueError(
                f"unknown sort_by value {sort_by!r} "
                f"— use 'position' or a callable"
            )

    return result


# ---------------------------------------------------------------------------
# Re-exports (so users can do tp.Role, tp.State, etc.)
# ---------------------------------------------------------------------------

from touchpoint.core.element import Element as Element  # noqa: E402
from touchpoint.core.exceptions import (  # noqa: E402, F811
    ActionFailedError as ActionFailedError,
    BackendUnavailableError as BackendUnavailableError,
    TouchpointError as TouchpointError,
)
from touchpoint.core.types import Role as Role, State as State  # noqa: E402
from touchpoint.core.window import Window as Window  # noqa: E402

# ---------------------------------------------------------------------------
# Public API boundary
# ---------------------------------------------------------------------------

__all__ = [
    # Discovery
    "apps",
    "windows",
    "elements",
    "element_at",
    "get_element",
    # Window actions
    "activate_window",
    # Finding / matching
    "find",
    # Waiting
    "wait_for",
    "wait_for_app",
    "wait_for_window",
    # Screenshot
    "screenshot",
    "monitor_count",
    # Element-targeted actions
    "click",
    "double_click",
    "right_click",
    "set_value",
    "set_numeric_value",
    "focus",
    "select_text",
    "action",
    # Raw input (InputProvider)
    "type_text",
    "press_key",
    "hotkey",
    "click_at",
    "double_click_at",
    "right_click_at",
    "scroll",
    "mouse_move",
    # Configuration
    "configure",
    # Data models
    "Element",
    "Window",
    "Role",
    "State",
    # Exceptions
    "TouchpointError",
    "ActionFailedError",
    "BackendUnavailableError",
]

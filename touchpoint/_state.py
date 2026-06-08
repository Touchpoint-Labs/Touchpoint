"""Shared private state and internal helpers for the Touchpoint public API.

This module holds the module-level singletons (backend, CDP, input provider,
config) and the routing / init helpers that every API submodule needs.
It is **not** part of the public API.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import threading
import time
from functools import wraps
from typing import Any

logger = logging.getLogger("touchpoint")

from touchpoint.backends.base import Backend, InputProvider
from touchpoint.core.exceptions import (
    ActionFailedError,
    BackendUnavailableError,
)
from touchpoint.core.types import Role

# ---------------------------------------------------------------------------
# Request serialization
# ---------------------------------------------------------------------------

_request_lock = threading.RLock()


def _serialized(fn):
    """Serialize a complete public operation against shared backend state.

    Backend instances retain mutable per-walk caches and filter hints.
    A re-entrant lock keeps nested public calls safe (for example,
    ``screenshot(app=...)`` calling ``windows()``) while preventing
    overlapping traversals from corrupting each other's state.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        with _request_lock:
            return fn(*args, **kwargs)
    return wrapper

# ---------------------------------------------------------------------------
# Backend auto-detection
# ---------------------------------------------------------------------------

_backend: Backend | None = None


def _init_backend() -> Backend:
    global _backend

    with _request_lock:
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
                reason=(
                    "UIAutomation is not available. "
                    "Ensure you are running on Windows with a desktop session"
                ),
            )

        if sys.platform == "darwin":
            from touchpoint.backends.macos.ax import AxBackend

            candidate = AxBackend(
                messaging_timeout=_config["ax_messaging_timeout"],
            )
            if candidate.is_available():
                _backend = candidate
                return _backend

            raise BackendUnavailableError(
                backend="ax",
                reason=(
                    "macOS Accessibility API is not available. "
                    "Ensure pyobjc-framework-ApplicationServices is installed "
                    "and Accessibility permission for your terminal or IDE "
                    "has been granted (System Settings → Privacy & "
                    "Security → Accessibility)"
                ),
            )

        raise BackendUnavailableError(
            backend="unknown",
            reason=f"no backend available for platform {sys.platform!r}",
        )


def _get_backend() -> Backend:
    """Return the active backend, initialising on first call."""
    with _request_lock:
        if _backend is None:
            return _init_backend()
        return _backend


# ---------------------------------------------------------------------------
# CDP backend (optional, additive)
# ---------------------------------------------------------------------------

_cdp_backend: Any = None
_cdp_attempted: bool = False
_cdp_last_refresh: float = 0.0


def _init_cdp() -> Any:
    global _cdp_backend, _cdp_attempted, _cdp_last_refresh
    with _request_lock:
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
    global _cdp_last_refresh
    with _request_lock:
        if _cdp_backend is not None:
            now = time.monotonic()
            if now - _cdp_last_refresh >= _config["cdp_refresh_interval"]:
                try:
                    _cdp_backend.refresh_targets()
                except Exception:
                    logger.debug("CDP: refresh_targets failed", exc_info=True)
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
    global _cdp_backend, _cdp_attempted, _cdp_last_refresh
    with _request_lock:
        if _cdp_backend is not None:
            try:
                _cdp_backend.close()
            except Exception:
                pass
        _cdp_backend = None
        _cdp_attempted = False
        _cdp_last_refresh = 0.0


def _is_cdp_id(element_id: str) -> bool:
    if _cdp_backend is not None:
        return _cdp_backend.owns_element(element_id)
    return isinstance(element_id, str) and element_id.startswith("cdp:")


def _strip_document_subtrees(elems: list) -> list:
    doc_prefixes: list[str] = []
    for e in elems:
        if e.role == Role.DOCUMENT:
            doc_prefixes.append(e.id + ".")

    if not doc_prefixes:
        return elems

    out = []
    for e in elems:
        if e.role == Role.DOCUMENT:
            continue
        if any(e.id.startswith(p) for p in doc_prefixes):
            continue
        out.append(e)
    return out


def _is_cdp_app(app: str) -> bool:
    cdp = _get_cdp()
    if cdp is None:
        return False
    if cdp.claims_app(app):
        return True
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
    cdp = _get_cdp()
    if cdp is None:
        return app
    cdp_pids = cdp.get_owned_pids()
    if not cdp_pids:
        return app
    app_pids = cdp.get_pids_for_app(app)
    try:
        for w in _get_backend().get_windows():
            if w.pid not in cdp_pids:
                continue
            if w.app.lower() == app.lower() or w.pid in app_pids:
                return w.app
    except Exception:
        pass
    return app


def _topmost_pid_at(x: int, y: int) -> int | None:
    return _get_backend().get_topmost_pid_at(x, y)


def _png_bytes_to_image(png_bytes: bytes) -> Any:
    import io
    from PIL import Image
    return Image.open(io.BytesIO(png_bytes)).convert("RGB")


def _backend_for_id(element_id: str) -> Backend:
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


def _native_window_for_cdp(cdp_window_id: str) -> str | None:
    """Resolve a CDP page-target window id to its native OS window id.

    A CDP window is a browser page/tab target, not an OS window. OS-frame
    operations (minimize, move, resize, fullscreen, close) must act on the
    real platform window, which the merge in :func:`windows` hides behind
    the CDP target. Re-resolve it here by owning PID.

    When the PID owns several native windows, disambiguate by title (the
    OS window title typically contains the page title), then fall back to
    the active window, then the largest.

    Returns the native window id, or ``None`` if the CDP target is stale
    or its PID has no native window the platform backend can see.
    """
    cdp = _get_cdp()
    if cdp is None:
        return None
    target = next(
        (w for w in cdp.get_windows() if w.id == cdp_window_id), None,
    )
    if target is None:
        return None

    native = [w for w in _get_backend().get_windows() if w.pid == target.pid]
    if not native:
        return None
    if len(native) == 1:
        return native[0].id

    title = (target.title or "").strip().lower()
    if title:
        for w in native:
            if title in (w.title or "").lower():
                return w.id

    active = next((w for w in native if w.is_active), None)
    if active is not None:
        return active.id
    return max(native, key=lambda w: w.size[0] * w.size[1]).id


# ---------------------------------------------------------------------------
# InputProvider auto-detection
# ---------------------------------------------------------------------------

_input_provider: InputProvider | None = None


def _init_input() -> InputProvider | None:
    global _input_provider

    with _request_lock:
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
    "max_depth": 20,
    "cdp_ports": None,
    "cdp_discover": True,
    "cdp_refresh_interval": 5.0,
    "scale_factor": None,
    "ax_messaging_timeout": 1.0,
}

_VALID_CONFIG_KEYS = frozenset(_config)

_VALID_SOURCES = ("full", "cdp_ax", "ax", "native", "dom")


@_serialized
def configure(**kwargs) -> dict:
    """Adjust Touchpoint runtime behaviour, or read current settings.

    Called with no arguments, returns a copy of the current configuration
    dict.  Called with keyword arguments, updates the specified keys and
    returns the updated configuration.
    """
    if not kwargs:
        return dict(_config)
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
    if "ax_messaging_timeout" in kwargs:
        v = kwargs["ax_messaging_timeout"]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            raise ValueError(
                f"ax_messaging_timeout must be a positive number, got {v!r}"
            )

    _config.update(kwargs)
    if "type_chunk_size" in kwargs:
        _input_provider = None
    if "scale_factor" in kwargs:
        from touchpoint.utils.scale import set_scale_factor
        set_scale_factor(
            float(kwargs["scale_factor"]) if kwargs["scale_factor"] is not None
            else None
        )
    if "cdp_ports" in kwargs or "cdp_discover" in kwargs:
        _reinit_cdp()
    if "ax_messaging_timeout" in kwargs and _backend is not None:
        setter = getattr(_backend, "set_messaging_timeout", None)
        if setter is not None:
            setter(float(kwargs["ax_messaging_timeout"]))
    return dict(_config)


# ---------------------------------------------------------------------------
# Health diagnostics
# ---------------------------------------------------------------------------


def _tool_path(name: str) -> str | None:
    """Return an executable path for diagnostics without importing a backend."""
    return shutil.which(name)


@_serialized
def diagnostics(*, probe: bool = True) -> dict[str, Any]:
    """Return JSON-friendly backend, input, CDP, and dependency health data.

    Args:
        probe: If true (default), initialise optional providers so the
            report reflects current availability.  Pass false to inspect
            already-initialised state without performing discovery I/O.
    """
    backend = _backend
    input_provider = _input_provider
    cdp = _cdp_backend
    errors: list[str] = []

    if probe:
        try:
            backend = _get_backend()
        except Exception as exc:
            errors.append(f"backend: {type(exc).__name__}: {exc}")
        try:
            input_provider = _input_provider or _init_input()
        except Exception as exc:
            errors.append(f"input: {type(exc).__name__}: {exc}")
        try:
            cdp = _get_cdp()
        except Exception as exc:
            errors.append(f"cdp: {type(exc).__name__}: {exc}")

    backend_health: dict[str, Any] = {}
    if backend is not None:
        get_health = getattr(backend, "get_diagnostics", None)
        if get_health is not None:
            try:
                backend_health = get_health()
            except Exception as exc:
                errors.append(
                    f"backend diagnostics: {type(exc).__name__}: {exc}"
                )

    cdp_targets: list[dict[str, Any]] = []
    if cdp is not None:
        try:
            cdp_targets = [
                {
                    "id": w.id,
                    "title": w.title,
                    "app": w.app,
                    "pid": w.pid,
                }
                for w in cdp.get_windows()
            ]
        except Exception as exc:
            errors.append(f"cdp targets: {type(exc).__name__}: {exc}")

    dependencies: dict[str, Any] = {}
    if sys.platform.startswith("linux"):
        dependencies = {
            "display": os.environ.get("DISPLAY"),
            "session_type": os.environ.get("XDG_SESSION_TYPE"),
            "wayland_display": os.environ.get("WAYLAND_DISPLAY"),
            "wmctrl": _tool_path("wmctrl"),
            "xdotool": _tool_path("xdotool"),
        }
    elif sys.platform == "darwin":
        dependencies = {
            "application_services": backend is not None,
            "quartz_input": input_provider is not None,
        }
    elif sys.platform == "win32":
        dependencies = {
            "uiautomation": backend is not None,
            "send_input": input_provider is not None,
        }

    return {
        "platform": sys.platform,
        "request_serialization": "threading.RLock",
        "config": dict(_config),
        "backend": {
            "initialized": backend is not None,
            "name": type(backend).__name__ if backend is not None else None,
            "available": (
                bool(backend.is_available()) if backend is not None else False
            ),
            **backend_health,
        },
        "input_provider": {
            "initialized": input_provider is not None,
            "name": (
                type(input_provider).__name__
                if input_provider is not None else None
            ),
            "available": (
                bool(input_provider.is_available())
                if input_provider is not None else False
            ),
        },
        "cdp": {
            "attempted": _cdp_attempted,
            "initialized": cdp is not None,
            "owned_pids": sorted(cdp.get_owned_pids()) if cdp is not None else [],
            "targets": cdp_targets,
        },
        "dependencies": dependencies,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Action helpers
# ---------------------------------------------------------------------------


def _resolve_target(target) -> str:
    """Extract an element id from an Element or string."""
    from touchpoint.core.element import Element
    if isinstance(target, Element):
        return target.id
    return target


def _get_element_position(element_id: str) -> tuple[int, int] | None:
    try:
        el = _backend_for_id(element_id).get_element_by_id(element_id)
    except ValueError:
        return None
    if el is None:
        return None
    return el.position


def _resolve_for_fallback(
    element_id: str,
    action: str,
    original_exc: ActionFailedError,
):
    """Look up an element for the click / set_value fallback path.

    Raises a structured :class:`ActionFailedError` if the lookup itself
    fails (malformed ID or not-found), so the agent sees an accurate
    diagnostic instead of the original error — which can be misleading
    when it came from :func:`_try_actions` with an empty alias list
    (the backend never got a chance to validate the element).

    Returns the live :class:`~touchpoint.core.element.Element` on
    success.  Never returns ``None``.
    """
    try:
        el = _backend_for_id(element_id).get_element_by_id(element_id)
    except ValueError as exc:
        raise ActionFailedError(
            action=action,
            element_id=element_id,
            reason=f"malformed element_id: {exc}",
        ) from exc
    if el is None:
        raise ActionFailedError(
            action=action,
            element_id=element_id,
            reason="element not found — it may have been removed or its ID is stale",
        ) from original_exc
    return el


def _try_actions(
    element_id: str,
    names: list[str],
    *,
    action: str | None = None,
) -> bool:
    backend = _backend_for_id(element_id)
    action = action or (names[0] if names else "unknown")
    if not names:
        exc = ActionFailedError(
            action=action,
            element_id=element_id,
            reason="no action aliases configured",
        )
        _resolve_for_fallback(element_id, action, exc)
        raise exc

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
        action=action,
        element_id=element_id,
        reason=f"all action aliases returned False: {names}",
    )


# ---------------------------------------------------------------------------
# Scroll direction constant
# ---------------------------------------------------------------------------

_SCROLL_DIRECTIONS = frozenset({"up", "down", "left", "right"})

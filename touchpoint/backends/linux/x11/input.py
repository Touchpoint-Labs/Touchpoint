"""Linux X11 input simulation via ``xdotool``.

Provides raw keyboard and mouse input by shelling out to the
``xdotool`` binary.  No pip dependency — ``xdotool`` is a system
package (``sudo apt install xdotool``).

This module is the :class:`~touchpoint.backends.base.InputProvider`
implementation for Linux desktops running an X11 display server.
For Wayland, a separate provider wrapping ``ydotool`` / ``wtype``
will be needed in the future.

Usage::

    from touchpoint.backends.linux.x11.input import XdotoolInput

    inp = XdotoolInput()
    if inp.is_available():
        inp.type_text("hello")
        inp.hotkey("ctrl", "s")
        inp.click_at(500, 300)
"""

from __future__ import annotations

import shutil
import subprocess

from touchpoint.backends.base import InputProvider

# Valid directions for scroll().
_SCROLL_DIRECTIONS = frozenset({"up", "down", "left", "right"})

# xdotool mouse button numbers.
_SCROLL_BUTTON: dict[str, int] = {
    "up": 4,
    "down": 5,
    "left": 6,
    "right": 7,
}


# ---------------------------------------------------------------------------
# Canonical key name → xdotool keysym mapping.
# ---------------------------------------------------------------------------
# Touchpoint uses lowercase, LLM-friendly canonical names everywhere.
# This table translates them to the X11 keysym strings that xdotool
# expects.  Keys NOT in this table are passed through as-is, so
# power users can still use raw X11 keysyms ("XF86AudioPlay" etc.).
# ---------------------------------------------------------------------------

_KEY_MAP: dict[str, str] = {
    # Modifiers
    "ctrl": "ctrl",
    "control": "ctrl",
    "alt": "alt",
    "shift": "shift",
    "super": "super",
    "meta": "super",
    "win": "super",
    "command": "super",
    # Navigation
    "enter": "Return",
    "return": "Return",
    "tab": "Tab",
    "escape": "Escape",
    "esc": "Escape",
    "backspace": "BackSpace",
    "delete": "Delete",
    "del": "Delete",
    "insert": "Insert",
    "space": "space",
    # Arrows
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    # Page navigation
    "home": "Home",
    "end": "End",
    "pageup": "Page_Up",
    "page_up": "Page_Up",
    "pagedown": "Page_Down",
    "page_down": "Page_Down",
    # Function keys
    "f1": "F1",
    "f2": "F2",
    "f3": "F3",
    "f4": "F4",
    "f5": "F5",
    "f6": "F6",
    "f7": "F7",
    "f8": "F8",
    "f9": "F9",
    "f10": "F10",
    "f11": "F11",
    "f12": "F12",
    # Misc
    "capslock": "Caps_Lock",
    "caps_lock": "Caps_Lock",
    "numlock": "Num_Lock",
    "num_lock": "Num_Lock",
    "scrolllock": "Scroll_Lock",
    "scroll_lock": "Scroll_Lock",
    "printscreen": "Print",
    "print_screen": "Print",
    "pause": "Pause",
    "menu": "Menu",
}


class XdotoolInput(InputProvider):
    """Input simulation using the ``xdotool`` command-line tool.

    All methods call ``xdotool`` via :func:`subprocess.run` with a
    short timeout.  If ``xdotool`` is not installed or the
    ``DISPLAY`` environment variable is not set, :meth:`is_available`
    returns ``False``.

    Key names are normalised through ``_KEY_MAP`` so that
    LLM-friendly lowercase names (``"enter"``, ``"ctrl"``,
    ``"escape"``) are translated to the X11 keysym strings that
    ``xdotool`` expects.  Unknown names are passed through as-is.

    Args:
        type_chunk_size: Maximum characters per ``xdotool type``
            invocation.  Long strings are automatically split into
            chunks of this size to avoid subprocess timeouts.
            Set to ``None`` to disable chunking.  Default ``40``.
    """

    SELECT_ALL_KEYS: tuple[str, ...] = ("ctrl", "a")
    """Select-all shortcut for Linux (Ctrl+A)."""

    def __init__(self, *, type_chunk_size: int | None = 40) -> None:
        self._bin: str | None = shutil.which("xdotool")
        self._type_chunk_size = type_chunk_size

    def _run(self, *args: str, timeout: float = 5.0) -> None:
        """Run an xdotool command.

        Raises:
            RuntimeError: If ``xdotool`` is not installed or the
                command exits with a non-zero code.
        """
        if self._bin is None:
            msg = (
                "xdotool is not installed — "
                "install it via your package manager "
                "(e.g. apt/dnf/pacman install xdotool)"
            )
            raise RuntimeError(msg)

        try:
            subprocess.run(
                [self._bin, *args],
                check=True,
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.decode(errors="replace").strip()
            msg = f"xdotool failed: {stderr or exc}"
            raise RuntimeError(msg) from exc
        except subprocess.TimeoutExpired as exc:
            msg = f"xdotool timed out after {timeout}s"
            raise RuntimeError(msg) from exc

    # -- Keyboard ---------------------------------------------------------

    def type_text(self, text: str) -> None:
        """Type a string using ``xdotool type``.

        Uses ``--clearmodifiers`` so held modifier keys (Shift, Ctrl)
        don't interfere with the typed characters.

        Long strings are automatically split into chunks of
        :attr:`_type_chunk_size` characters so that no single
        ``xdotool`` invocation exceeds its timeout.  Pass
        ``type_chunk_size=None`` to the constructor to disable.
        """
        chunk = self._type_chunk_size
        if chunk is None or chunk <= 0 or len(text) <= chunk:
            self._run("type", "--clearmodifiers", "--", text)
            return
        for i in range(0, len(text), chunk):
            self._run("type", "--clearmodifiers", "--", text[i : i + chunk])

    @staticmethod
    def _normalize_key(key: str) -> str:
        """Translate a canonical key name to an xdotool keysym.

        Looks up *key* (case-insensitive) in ``_KEY_MAP``.  If not
        found, returns the original string unchanged so that raw
        X11 keysym names (``"XF86AudioPlay"``) still work.
        """
        return _KEY_MAP.get(key.lower(), key)

    def press_key(self, key: str) -> None:
        """Press and release a single key using ``xdotool key``."""
        self._run("key", "--clearmodifiers", self._normalize_key(key))

    def hotkey(self, *keys: str) -> None:
        """Press a key combination using ``xdotool key``.

        ``xdotool key`` accepts ``+``-separated key names for
        combos, e.g. ``xdotool key ctrl+shift+s``.

        Each key is normalised through ``_KEY_MAP`` so canonical
        names like ``"ctrl"``, ``"enter"`` are accepted.
        """
        if not keys:
            msg = "hotkey() requires at least one key"
            raise RuntimeError(msg)
        combo = "+".join(self._normalize_key(k) for k in keys)
        self._run("key", "--clearmodifiers", combo)

    # -- Mouse ------------------------------------------------------------

    def _move_mouse(self, x: int, y: int) -> None:
        """Move the pointer to (x, y), retrying without ``--sync`` on timeout.

        ``mousemove --sync`` waits for the X server to confirm
        the cursor has arrived.  This can hang when a popup,
        context menu, or autocomplete dropdown has grabbed input.

        Strategy:
        1. Try ``mousemove --sync`` with a short timeout (2s).
        2. On timeout, retry without ``--sync`` and sleep briefly
           to give the server time to process the move.
        """
        try:
            self._run("mousemove", "--sync", str(x), str(y), timeout=2.0)
        except RuntimeError as exc:
            import subprocess
            if not isinstance(exc.__cause__, subprocess.TimeoutExpired):
                raise
            import time
            self._run("mousemove", str(x), str(y), timeout=5.0)
            time.sleep(0.15)

    def click_at(self, x: int, y: int) -> None:
        """Left-click at (x, y) using ``xdotool mousemove`` + ``click``."""
        self._move_mouse(x, y)
        self._run("click", "1")

    def double_click_at(self, x: int, y: int) -> None:
        """Double-click at (x, y)."""
        self._move_mouse(x, y)
        self._run("click", "--repeat", "2", "--delay", "50", "1")

    def right_click_at(self, x: int, y: int) -> None:
        """Right-click at (x, y)."""
        self._move_mouse(x, y)
        self._run("click", "3")

    def scroll(
        self,
        x: int | None,
        y: int | None,
        direction: str,
        amount: int = 3,
    ) -> None:
        """Scroll at (x, y) by clicking the scroll button repeatedly.

        ``xdotool`` simulates scroll via mouse button clicks:
        button 4 = up, 5 = down, 6 = left, 7 = right.

        When *x* and *y* are ``None``, scrolls at the current
        cursor position without moving it first.
        """
        if direction not in _SCROLL_DIRECTIONS:
            msg = (
                f"invalid scroll direction {direction!r} — "
                f"expected one of {sorted(_SCROLL_DIRECTIONS)}"
            )
            raise ValueError(msg)
        button = _SCROLL_BUTTON[direction]
        if x is not None and y is not None:
            self._move_mouse(x, y)
        self._run("click", "--repeat", str(amount), "--delay", "30",
                  str(button))

    def mouse_move(self, x: int, y: int) -> None:
        """Move the pointer to (x, y)."""
        self._move_mouse(x, y)

    # -- Window management ------------------------------------------------

    def activate_window(self, title: str, pid: int) -> bool:
        """Bring a window to the foreground via ``xdotool``.

        Searches for X11 windows matching the given PID and title,
        then activates the best match.

        Search strategy (most precise first):

        1. PID + title — exact match, safest.
        2. PID only — title may have changed since the query.

        Uses a two-step approach (search → activate) rather than
        xdotool's chained syntax, because chaining with ``--sync``
        can hang on internal helper windows that can't be raised.

        Args:
            title: The window's title bar text.
            pid: The process id of the owning application.

        Returns:
            ``True`` if a matching window was found and activated.
        """
        if self._bin is None:
            return False

        # Strategy 1: PID + title — most precise.
        ids = self._search_windows(pid=pid, title=title)

        # Strategy 2: PID only — title may have changed.
        if not ids:
            ids = self._search_windows(pid=pid)

        if not ids:
            return False

        # Activate the last match — for multi-window apps this is
        # typically the most recently mapped (real) window.
        try:
            self._run("windowactivate", ids[-1])
            return True
        except RuntimeError:
            return False

    def _search_windows(
        self,
        pid: int | None = None,
        title: str | None = None,
    ) -> list[str]:
        """Search for X11 window IDs by PID and/or title.

        Args:
            pid: Filter by process id.
            title: Filter by window name (passed to xdotool's
                ``--name`` as a regex-escaped pattern).

        Returns:
            List of X11 window ID strings, or empty list.
        """
        import subprocess

        cmd = [self._bin, "search"]
        if pid is not None:
            cmd.extend(["--pid", str(pid)])
        if title is not None:
            import re
            cmd.extend(["--name", re.escape(title)])

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=5.0,
            )
            if result.stdout.strip():
                return result.stdout.strip().split("\n")
        except Exception:
            pass
        return []

    # -- Availability -----------------------------------------------------

    def is_available(self) -> bool:
        """Return ``True`` if ``xdotool`` is installed and X11 is active.

        Checks for both the ``xdotool`` binary on ``$PATH`` and the
        ``$DISPLAY`` environment variable (set by X11 sessions).
        Returns ``False`` on Wayland-only or headless systems even
        when ``xdotool`` is installed.
        """
        import os
        return self._bin is not None and bool(os.environ.get("DISPLAY"))

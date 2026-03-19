"""Windows input simulation via ``ctypes`` → ``SendInput``.

This is the :class:`~touchpoint.backends.base.InputProvider`
implementation for Windows desktops.  It uses the Win32
``SendInput()`` API through ``ctypes`` — no external binaries or
pip packages required.

All coordinate-based methods handle DPI scaling correctly by calling
``SetProcessDPIAware()`` at init time and normalising pixel
coordinates to the 0–65535 absolute coordinate space that
``SendInput`` expects.

Usage::

    from touchpoint.backends.windows.input import SendInputProvider

    inp = SendInputProvider()
    if inp.is_available():
        inp.type_text("hello")
        inp.hotkey("ctrl", "s")
        inp.click_at(500, 300)
"""

from __future__ import annotations

import sys
import time

from touchpoint.backends.base import InputProvider

# ---------------------------------------------------------------------------
# Constants — Win32 input event flags and structure definitions.
# These are defined here so we don't need any external dependency.
# ---------------------------------------------------------------------------

# Input type constants for the INPUT structure.
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

# Mouse event flags.
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_ABSOLUTE = 0x8000

# Keyboard event flags.
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

# Scroll amount per "tick" (matches Windows' WHEEL_DELTA = 120).
WHEEL_DELTA = 120

# Valid scroll directions.
_SCROLL_DIRECTIONS = frozenset({"up", "down", "left", "right"})

# System metrics indices for screen dimensions.
SM_CXSCREEN = 0
SM_CYSCREEN = 1

# Virtual screen metrics (entire multi-monitor desktop).
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

# MOUSEEVENTF_VIRTUALDESK — coordinates span the full virtual desktop.
MOUSEEVENTF_VIRTUALDESK = 0x4000

# ---------------------------------------------------------------------------
# Virtual key code mapping.
# ---------------------------------------------------------------------------
# Touchpoint uses lowercase, LLM-friendly canonical key names.
# This table maps them to Win32 virtual-key codes (VK_*).  Keys
# NOT in this table are passed through: single characters are sent
# via VkKeyScanW, unknown names raise RuntimeError.
#
# Reference: https://learn.microsoft.com/en-us/windows/win32/inputdev/
#            virtual-key-codes
# ---------------------------------------------------------------------------

_VK_MAP: dict[str, int] = {
    # Modifiers
    "ctrl": 0x11,       # VK_CONTROL
    "control": 0x11,
    "alt": 0x12,        # VK_MENU
    "shift": 0x10,      # VK_SHIFT
    "super": 0x5B,      # VK_LWIN
    "meta": 0x5B,
    "win": 0x5B,
    "command": 0x5B,
    "lshift": 0xA0,     # VK_LSHIFT
    "rshift": 0xA1,     # VK_RSHIFT
    "lctrl": 0xA2,      # VK_LCONTROL
    "rctrl": 0xA3,      # VK_RCONTROL
    "lalt": 0xA4,       # VK_LMENU
    "ralt": 0xA5,       # VK_RMENU

    # Navigation
    "enter": 0x0D,      # VK_RETURN
    "return": 0x0D,
    "tab": 0x09,        # VK_TAB
    "escape": 0x1B,     # VK_ESCAPE
    "esc": 0x1B,
    "backspace": 0x08,  # VK_BACK
    "delete": 0x2E,     # VK_DELETE
    "del": 0x2E,
    "insert": 0x2D,     # VK_INSERT
    "space": 0x20,      # VK_SPACE

    # Arrows
    "up": 0x26,         # VK_UP
    "down": 0x28,       # VK_DOWN
    "left": 0x25,       # VK_LEFT
    "right": 0x27,      # VK_RIGHT

    # Page navigation
    "home": 0x24,       # VK_HOME
    "end": 0x23,        # VK_END
    "pageup": 0x21,     # VK_PRIOR
    "page_up": 0x21,
    "pagedown": 0x22,   # VK_NEXT
    "page_down": 0x22,

    # Function keys
    "f1": 0x70,         # VK_F1
    "f2": 0x71,
    "f3": 0x72,
    "f4": 0x73,
    "f5": 0x74,
    "f6": 0x75,
    "f7": 0x76,
    "f8": 0x77,
    "f9": 0x78,
    "f10": 0x79,
    "f11": 0x7A,
    "f12": 0x7B,

    # Misc
    "capslock": 0x14,     # VK_CAPITAL
    "caps_lock": 0x14,
    "numlock": 0x90,      # VK_NUMLOCK
    "num_lock": 0x90,
    "scrolllock": 0x91,   # VK_SCROLL
    "scroll_lock": 0x91,
    "printscreen": 0x2C,  # VK_SNAPSHOT
    "print_screen": 0x2C,
    "pause": 0x13,        # VK_PAUSE
    "menu": 0x5D,         # VK_APPS (context menu key)
    "apps": 0x5D,
}


def _build_structures():
    """Build the ctypes structures needed for SendInput.

    Defined as a function so ctypes is only imported on Windows.
    Returns the INPUT structure class and the KEYBDINPUT and
    MOUSEINPUT classes.
    """
    import ctypes
    import ctypes.wintypes

    class KEYBDINPUT(ctypes.Structure):
        """Win32 KEYBDINPUT structure for keyboard events."""
        _fields_ = [
            ("wVk", ctypes.wintypes.WORD),
            ("wScan", ctypes.wintypes.WORD),
            ("dwFlags", ctypes.wintypes.DWORD),
            ("time", ctypes.wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]

    class MOUSEINPUT(ctypes.Structure):
        """Win32 MOUSEINPUT structure for mouse events."""
        _fields_ = [
            ("dx", ctypes.c_long),
            ("dy", ctypes.c_long),
            ("mouseData", ctypes.wintypes.DWORD),
            ("dwFlags", ctypes.wintypes.DWORD),
            ("time", ctypes.wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        """Win32 HARDWAREINPUT structure (unused, but required by union)."""
        _fields_ = [
            ("uMsg", ctypes.wintypes.DWORD),
            ("wParamL", ctypes.wintypes.WORD),
            ("wParamH", ctypes.wintypes.WORD),
        ]

    class _INPUT_UNION(ctypes.Union):
        """Union of the three input types."""
        _fields_ = [
            ("mi", MOUSEINPUT),
            ("ki", KEYBDINPUT),
            ("hi", HARDWAREINPUT),
        ]

    class INPUT(ctypes.Structure):
        """Win32 INPUT structure passed to SendInput()."""
        _fields_ = [
            ("type", ctypes.wintypes.DWORD),
            ("union", _INPUT_UNION),
        ]

    return INPUT, KEYBDINPUT, MOUSEINPUT


class SendInputProvider(InputProvider):
    """Input simulation using Win32 ``SendInput()``.

    All methods use ``ctypes.windll.user32.SendInput`` to inject
    input events into the OS input queue.  No external dependencies.

    Handles DPI scaling by calling ``SetProcessDPIAware()`` at init
    time and using ``GetSystemMetrics`` for coordinate normalisation.
    This means custom Windows display scaling (100%, 125%, 150%, etc.)
    is handled correctly — pixel coordinates from the UIA backend
    map 1:1 to where clicks land on screen.

    Key names follow the canonical names defined in
    :class:`~touchpoint.backends.base.InputProvider`.  The
    implementation maps them to Win32 virtual-key codes via the
    ``_VK_MAP`` table.
    """

    SELECT_ALL_KEYS: tuple[str, ...] = ("ctrl", "a")
    """Select-all shortcut for Windows (Ctrl+A)."""

    def __init__(self) -> None:
        self._available: bool = False
        self._screen_w: int = 0
        self._screen_h: int = 0
        self._virt_x: int = 0
        self._virt_y: int = 0
        self._virt_w: int = 0
        self._virt_h: int = 0

        if sys.platform != "win32":
            return

        try:
            import ctypes

            # Ensure DPI awareness so GetSystemMetrics returns physical
            # dimensions.  Usually already called by _init_uia(), but
            # repeated here as a safety net (the call is idempotent).
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

            # Get virtual desktop dimensions (spans all monitors).
            # Falls back to primary monitor if virtual metrics are 0
            # (should never happen on modern Windows).
            gm = ctypes.windll.user32.GetSystemMetrics
            self._virt_x = gm(SM_XVIRTUALSCREEN)
            self._virt_y = gm(SM_YVIRTUALSCREEN)
            self._virt_w = gm(SM_CXVIRTUALSCREEN)
            self._virt_h = gm(SM_CYVIRTUALSCREEN)

            if self._virt_w <= 0 or self._virt_h <= 0:
                # Fallback: use primary monitor only.
                self._virt_x = 0
                self._virt_y = 0
                self._virt_w = gm(SM_CXSCREEN)
                self._virt_h = gm(SM_CYSCREEN)

            self._screen_w = self._virt_w
            self._screen_h = self._virt_h

            if self._screen_w > 0 and self._screen_h > 0:
                self._available = True

            # Build the ctypes structures for SendInput.
            self._INPUT, self._KEYBDINPUT, self._MOUSEINPUT = _build_structures()
        except Exception:
            pass

    # -- Internal helpers -------------------------------------------------

    def _send_input(self, *inputs) -> None:
        """Send one or more INPUT structures via SendInput.

        Args:
            inputs: One or more INPUT structure instances.

        Raises:
            RuntimeError: If SendInput fails (returns fewer events
                than expected).
        """
        import ctypes

        n = len(inputs)
        arr = (self._INPUT * n)(*inputs)
        result = ctypes.windll.user32.SendInput(
            n, arr, ctypes.sizeof(self._INPUT),
        )
        if result != n:
            raise RuntimeError(
                f"SendInput failed: sent {result}/{n} events"
            )

    def _key_down(self, vk: int) -> object:
        """Build a key-down INPUT structure.

        Args:
            vk: Win32 virtual-key code.

        Returns:
            An INPUT structure for a key-down event.
        """
        inp = self._INPUT()
        inp.type = INPUT_KEYBOARD
        inp.union.ki.wVk = vk
        inp.union.ki.dwFlags = 0  # key down
        return inp

    def _key_up(self, vk: int) -> object:
        """Build a key-up INPUT structure.

        Args:
            vk: Win32 virtual-key code.

        Returns:
            An INPUT structure for a key-up event.
        """
        inp = self._INPUT()
        inp.type = INPUT_KEYBOARD
        inp.union.ki.wVk = vk
        inp.union.ki.dwFlags = KEYEVENTF_KEYUP
        return inp

    def _unicode_down(self, char: str) -> object:
        """Build a Unicode key-down INPUT structure.

        Uses ``KEYEVENTF_UNICODE`` to send arbitrary Unicode
        characters without needing a virtual-key code.  The
        character's ordinal is placed in ``wScan``, and Windows
        delivers it as a ``WM_CHAR`` message to the focused app.

        Args:
            char: A single Unicode character.

        Returns:
            An INPUT structure.
        """
        inp = self._INPUT()
        inp.type = INPUT_KEYBOARD
        inp.union.ki.wVk = 0
        inp.union.ki.wScan = ord(char)
        inp.union.ki.dwFlags = KEYEVENTF_UNICODE
        return inp

    def _unicode_up(self, char: str) -> object:
        """Build a Unicode key-up INPUT structure.

        Args:
            char: A single Unicode character.

        Returns:
            An INPUT structure.
        """
        inp = self._INPUT()
        inp.type = INPUT_KEYBOARD
        inp.union.ki.wVk = 0
        inp.union.ki.wScan = ord(char)
        inp.union.ki.dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP
        return inp

    def _mouse_event(
        self, x: int, y: int, flags: int, data: int = 0,
    ) -> object:
        """Build a mouse INPUT structure with absolute coordinates.

        Normalises pixel coordinates to the 0–65535 range that
        ``MOUSEEVENTF_ABSOLUTE`` expects.  When ``MOUSEEVENTF_VIRTUALDESK``
        is set the coordinate space spans the entire virtual desktop
        (all monitors), so coordinates are offset by the virtual
        screen origin before normalisation.

        ``normalised = ((pixel - origin) * 65536 + half_size) / size``

        Args:
            x: Pixel x coordinate.
            y: Pixel y coordinate.
            flags: Mouse event flags (``MOUSEEVENTF_*``).
            data: Extra data (e.g. wheel delta).

        Returns:
            An INPUT structure.
        """
        # Normalise to 0–65535 virtual-desktop coordinate space.
        nx = int(((x - self._virt_x) * 65536 + self._virt_w // 2) / self._virt_w)
        ny = int(((y - self._virt_y) * 65536 + self._virt_h // 2) / self._virt_h)

        inp = self._INPUT()
        inp.type = INPUT_MOUSE
        inp.union.mi.dx = nx
        inp.union.mi.dy = ny
        inp.union.mi.mouseData = data
        inp.union.mi.dwFlags = flags | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
        return inp

    def _resolve_vk(self, key: str) -> int:
        """Resolve a canonical key name to a Win32 virtual-key code.

        Lookup order:
        1. Check ``_VK_MAP`` for named keys (enter, ctrl, f1, etc.)
        2. For single characters, use ``VkKeyScanW`` to get the VK
           code from the current keyboard layout.
        3. Raise ``RuntimeError`` if nothing matches.

        Args:
            key: Canonical key name (lowercase).

        Returns:
            Win32 virtual-key code (integer).

        Raises:
            RuntimeError: If the key name can't be resolved to a
                virtual-key code.
        """
        import ctypes

        lower = key.lower()

        # Named key lookup.
        if lower in _VK_MAP:
            return _VK_MAP[lower]

        # Single character — use VkKeyScanW to get the VK code
        # for the current keyboard layout.
        if len(key) == 1:
            result = ctypes.windll.user32.VkKeyScanW(ord(key))
            if result != -1:
                # Low byte is the VK code, high byte has shift state.
                vk = result & 0xFF
                return vk

        raise RuntimeError(
            f"unknown key {key!r} — not in key map and "
            f"VkKeyScanW returned -1"
        )

    # -- Keyboard ---------------------------------------------------------

    def type_text(self, text: str) -> None:
        """Type a string using ``SendInput`` with ``KEYEVENTF_UNICODE``.

        Sends each character as a Unicode key event pair (down + up).
        This handles arbitrary Unicode characters (emoji, CJK,
        accented letters, symbols, etc.) without needing a keymap —
        Windows delivers each character as a ``WM_CHAR`` message.

        Args:
            text: The string to type.  May contain any Unicode
                characters.

        Raises:
            RuntimeError: If ``SendInput`` is unavailable.
        """
        if not self._available:
            raise RuntimeError("SendInput is not available")

        for char in text:
            self._send_input(
                self._unicode_down(char),
                self._unicode_up(char),
            )

    def press_key(self, key: str) -> None:
        """Press and release a single key using ``SendInput``.

        Maps the canonical key name to a Win32 virtual-key code
        and sends a key-down / key-up pair.

        For single printable characters (e.g. ``"a"``, ``"A"``,
        ``"1"``), uses ``VkKeyScanW`` to handle the current
        keyboard layout.  If the character requires Shift (like
        uppercase ``"A"``), the Shift modifier is added
        automatically.

        Args:
            key: Canonical key name (e.g. ``"enter"``, ``"tab"``,
                ``"a"``).

        Raises:
            RuntimeError: If the key can't be resolved or
                ``SendInput`` is unavailable.
        """
        if not self._available:
            raise RuntimeError("SendInput is not available")

        import ctypes

        lower = key.lower()

        # Check if this is a single character that needs shift.
        if len(key) == 1 and lower not in _VK_MAP:
            result = ctypes.windll.user32.VkKeyScanW(ord(key))
            if result != -1:
                vk = result & 0xFF
                shift = (result >> 8) & 0x01
                if shift:
                    # Character requires Shift — wrap with Shift
                    # key-down / key-up.
                    self._send_input(
                        self._key_down(0x10),  # VK_SHIFT down
                        self._key_down(vk),
                        self._key_up(vk),
                        self._key_up(0x10),    # VK_SHIFT up
                    )
                else:
                    self._send_input(
                        self._key_down(vk),
                        self._key_up(vk),
                    )
                return

        vk = self._resolve_vk(key)
        self._send_input(
            self._key_down(vk),
            self._key_up(vk),
        )

    def hotkey(self, *keys: str) -> None:
        """Press a keyboard combination using ``SendInput``.

        Holds all keys down in left-to-right order, then releases
        them in reverse order — the standard hotkey pattern that
        Windows expects.

        Each key is resolved through ``_VK_MAP`` and sent as a
        single batched ``SendInput`` call for atomicity.

        Args:
            keys: Two or more canonical key names.
                Example: ``hotkey("ctrl", "shift", "s")``

        Raises:
            RuntimeError: If any key name can't be resolved or
                ``SendInput`` is unavailable.
        """
        if not self._available:
            raise RuntimeError("SendInput is not available")
        if not keys:
            raise RuntimeError("hotkey() requires at least one key")

        vks = [self._resolve_vk(k) for k in keys]

        # Build all events: downs in order, then ups in reverse.
        events = []
        for vk in vks:
            events.append(self._key_down(vk))
        for vk in reversed(vks):
            events.append(self._key_up(vk))

        self._send_input(*events)

    # -- Mouse ------------------------------------------------------------

    def click_at(self, x: int, y: int) -> None:
        """Left-click at screen coordinates.

        Moves the cursor to ``(x, y)`` and performs a left button
        down + up sequence.  Uses ``MOUSEEVENTF_ABSOLUTE`` for
        precise positioning regardless of current cursor location.

        Args:
            x: Horizontal pixel coordinate.
            y: Vertical pixel coordinate.

        Raises:
            RuntimeError: If ``SendInput`` is unavailable.
        """
        if not self._available:
            raise RuntimeError("SendInput is not available")

        self._send_input(
            self._mouse_event(x, y, MOUSEEVENTF_MOVE),
            self._mouse_event(x, y, MOUSEEVENTF_LEFTDOWN),
            self._mouse_event(x, y, MOUSEEVENTF_LEFTUP),
        )

    def double_click_at(self, x: int, y: int) -> None:
        """Double-click (left button) at screen coordinates.

        Two rapid left-clicks with a 50ms pause between them.
        The pause ensures Windows recognises it as a double-click
        (requires both clicks within ``GetDoubleClickTime()``
        milliseconds, typically 500ms).

        Args:
            x: Horizontal pixel coordinate.
            y: Vertical pixel coordinate.

        Raises:
            RuntimeError: If ``SendInput`` is unavailable.
        """
        if not self._available:
            raise RuntimeError("SendInput is not available")

        self._send_input(
            self._mouse_event(x, y, MOUSEEVENTF_MOVE),
            self._mouse_event(x, y, MOUSEEVENTF_LEFTDOWN),
            self._mouse_event(x, y, MOUSEEVENTF_LEFTUP),
        )
        time.sleep(0.05)
        self._send_input(
            self._mouse_event(x, y, MOUSEEVENTF_LEFTDOWN),
            self._mouse_event(x, y, MOUSEEVENTF_LEFTUP),
        )

    def right_click_at(self, x: int, y: int) -> None:
        """Right-click at screen coordinates.

        Moves the cursor and performs a right button down + up.

        Args:
            x: Horizontal pixel coordinate.
            y: Vertical pixel coordinate.

        Raises:
            RuntimeError: If ``SendInput`` is unavailable.
        """
        if not self._available:
            raise RuntimeError("SendInput is not available")

        self._send_input(
            self._mouse_event(x, y, MOUSEEVENTF_MOVE),
            self._mouse_event(x, y, MOUSEEVENTF_RIGHTDOWN),
            self._mouse_event(x, y, MOUSEEVENTF_RIGHTUP),
        )

    def scroll(
        self,
        x: int | None,
        y: int | None,
        direction: str,
        amount: int = 3,
    ) -> None:
        """Scroll at a screen position.

        Uses ``MOUSEEVENTF_WHEEL`` for vertical scrolling and
        ``MOUSEEVENTF_HWHEEL`` for horizontal scrolling.

        Each "tick" sends ``WHEEL_DELTA`` (120) units, matching
        the standard Windows scroll increment.  Most applications
        treat one tick as three lines of text.

        When *x* and *y* are ``None``, scrolls at the current
        cursor position without moving it first.

        Args:
            x: Horizontal pixel coordinate to scroll at.
                ``None`` scrolls at the current cursor position.
            y: Vertical pixel coordinate to scroll at.
                ``None`` scrolls at the current cursor position.
            direction: One of ``"up"``, ``"down"``, ``"left"``,
                ``"right"``.
            amount: Number of scroll ticks.  Default ``3``.

        Raises:
            ValueError: If *direction* is not a valid value.
            RuntimeError: If ``SendInput`` is unavailable.
        """
        if not self._available:
            raise RuntimeError("SendInput is not available")

        if direction not in _SCROLL_DIRECTIONS:
            msg = (
                f"invalid scroll direction {direction!r} — "
                f"expected one of {sorted(_SCROLL_DIRECTIONS)}"
            )
            raise ValueError(msg)

        # Move cursor to scroll position first (skip if no coords).
        if x is not None and y is not None:
            self._send_input(
                self._mouse_event(x, y, MOUSEEVENTF_MOVE),
            )

        # Determine the wheel flag and delta per tick.
        if direction in ("up", "down"):
            flag = MOUSEEVENTF_WHEEL
            # Positive delta = scroll up, negative = scroll down.
            delta = WHEEL_DELTA if direction == "up" else -WHEEL_DELTA
        else:
            flag = MOUSEEVENTF_HWHEEL
            # Positive delta = scroll right, negative = scroll left.
            delta = WHEEL_DELTA if direction == "right" else -WHEEL_DELTA

        # Send scroll events one tick at a time for reliability
        # (some apps ignore large multi-tick scrolls).
        for _ in range(amount):
            inp = self._INPUT()
            inp.type = INPUT_MOUSE
            # mouseData is DWORD (unsigned), but WHEEL_DELTA can be
            # negative.  ctypes handles the two's-complement cast.
            inp.union.mi.mouseData = delta & 0xFFFFFFFF
            inp.union.mi.dwFlags = flag
            self._send_input(inp)

    def mouse_move(self, x: int, y: int) -> None:
        """Move the mouse pointer to screen coordinates.

        Uses ``MOUSEEVENTF_ABSOLUTE`` for precise absolute
        positioning.  Does not click.

        Args:
            x: Horizontal pixel coordinate.
            y: Vertical pixel coordinate.

        Raises:
            RuntimeError: If ``SendInput`` is unavailable.
        """
        if not self._available:
            raise RuntimeError("SendInput is not available")

        self._send_input(
            self._mouse_event(x, y, MOUSEEVENTF_MOVE),
        )

    # -- Window management ------------------------------------------------

    def activate_window(self, title: str, pid: int) -> bool:
        """Bring a window to the foreground using ``SetForegroundWindow``.

        Searches for a window matching the given PID (and optionally
        title) using ``EnumWindows``, then calls
        ``SetForegroundWindow`` to raise it.

        Search strategy:
        1. PID + title substring match — most precise.
        2. PID only — title may have changed since the query.

        Args:
            title: The window's title bar text.
            pid: The process id of the owning application.

        Returns:
            ``True`` if the window was found and activated.
        """
        if not self._available:
            return False

        import ctypes
        import ctypes.wintypes

        target_hwnd = None

        # Callback for EnumWindows — finds a visible window
        # belonging to the target PID.
        @ctypes.WINFUNCTYPE(
            ctypes.wintypes.BOOL, ctypes.wintypes.HWND,
            ctypes.wintypes.LPARAM,
        )
        def _enum_callback(hwnd, _lparam):
            nonlocal target_hwnd

            # Check if window belongs to the target PID.
            proc_id = ctypes.wintypes.DWORD()
            ctypes.windll.user32.GetWindowThreadProcessId(
                hwnd, ctypes.byref(proc_id),
            )
            if proc_id.value != pid:
                return True  # continue enumeration

            # Check if the window is visible.
            if not ctypes.windll.user32.IsWindowVisible(hwnd):
                return True  # continue

            # Check title match if provided.
            if title:
                buf = ctypes.create_unicode_buffer(256)
                ctypes.windll.user32.GetWindowTextW(hwnd, buf, 256)
                win_title = buf.value
                if title in win_title or win_title in title:
                    target_hwnd = hwnd
                    return False  # stop — exact match found

            # No title filter or title didn't match — still a
            # candidate (title may have changed since query).
            if target_hwnd is None:
                target_hwnd = hwnd
            return True  # continue looking for a title match

        try:
            ctypes.windll.user32.EnumWindows(_enum_callback, 0)
        except Exception:
            pass

        if target_hwnd is not None:
            try:
                if ctypes.windll.user32.IsIconic(target_hwnd):
                    SW_RESTORE = 9
                    ctypes.windll.user32.ShowWindow(target_hwnd, SW_RESTORE)
                ctypes.windll.user32.SetForegroundWindow(target_hwnd)
                return True
            except Exception:
                pass

        return False

    # -- Availability -----------------------------------------------------

    def is_available(self) -> bool:
        """Return ``True`` if running on Windows with SendInput access.

        Checks that ``sys.platform == 'win32'``, that the ctypes
        structures were built successfully, and that screen
        dimensions could be read (indicating the display is
        available).
        """
        return self._available

"""macOS input simulation via Quartz CGEvent APIs.

This is the :class:`~touchpoint.backends.base.InputProvider`
implementation for macOS desktops.  It uses the Quartz/CoreGraphics
``CGEvent*`` APIs through ``pyobjc`` — no external binaries required.

All coordinate-based methods use Quartz display points with the
origin at the **top-left** of the primary display, matching the
coordinate space used by the macOS Accessibility API (AX).

Requires:
    - ``pyobjc-framework-Quartz`` (installed automatically with ``pip install touchpoint-py``)
    - macOS Accessibility permission must be granted for the Python
      process in System Settings → Privacy & Security → Accessibility.

Usage::

    from touchpoint.backends.macos.input import CGEventInput

    inp = CGEventInput()
    if inp.is_available():
        inp.type_text("hello")
        inp.hotkey("super", "s")   # ⌘+S
        inp.click_at(500, 300)
"""

from __future__ import annotations

import sys
import time

from touchpoint.backends.base import InputProvider

# Valid scroll directions.
_SCROLL_DIRECTIONS = frozenset({"up", "down", "left", "right"})

# ---------------------------------------------------------------------------
# macOS virtual key code mapping (Carbon-era key codes).
# ---------------------------------------------------------------------------
# These are the hardware-independent virtual key codes used by
# CGEventCreateKeyboardEvent.  They differ from both Windows VK
# codes and X11 keysyms.
#
# Reference: Events.h / HIToolbox (Carbon framework)
# ---------------------------------------------------------------------------

_KEYCODE_MAP: dict[str, int] = {
    # Modifiers (used with CGEventSetFlags, but we also need
    # keycodes for key-down/key-up events)
    "command": 0x37,
    "super": 0x37,
    "meta": 0x37,
    "win": 0x37,
    "shift": 0x38,
    "lshift": 0x38,
    "capslock": 0x39,
    "caps_lock": 0x39,
    "alt": 0x3A,
    "option": 0x3A,
    "lalt": 0x3A,
    "ctrl": 0x3B,
    "control": 0x3B,
    "lctrl": 0x3B,
    "rshift": 0x3C,
    "ralt": 0x3D,
    "roption": 0x3D,
    "rctrl": 0x3E,
    "fn": 0x3F,

    # Navigation
    "enter": 0x24,
    "return": 0x24,
    "tab": 0x30,
    "space": 0x31,
    "backspace": 0x33,
    "delete": 0x75,         # forward delete
    "del": 0x75,
    "escape": 0x35,
    "esc": 0x35,

    # Arrows
    "left": 0x7B,
    "right": 0x7C,
    "down": 0x7D,
    "up": 0x7E,

    # Page navigation
    "home": 0x73,
    "end": 0x77,
    "pageup": 0x74,
    "page_up": 0x74,
    "pagedown": 0x79,
    "page_down": 0x79,

    # Function keys
    "f1": 0x7A,
    "f2": 0x78,
    "f3": 0x63,
    "f4": 0x76,
    "f5": 0x60,
    "f6": 0x61,
    "f7": 0x62,
    "f8": 0x64,
    "f9": 0x65,
    "f10": 0x6D,
    "f11": 0x67,
    "f12": 0x6F,

    # Misc
    "insert": 0x72,      # Help key on Mac keyboards
    "numlock": 0x47,
    "num_lock": 0x47,
    "printscreen": 0x69,  # F13 on Mac (closest equivalent)
    "print_screen": 0x69,
    "scrolllock": 0x6B,    # F14 on Mac
    "scroll_lock": 0x6B,
    "pause": 0x71,         # F15 on Mac
    "menu": 0x6E,          # No direct equivalent; F16

    # Letters (for hotkey use — type_text handles arbitrary text)
    "a": 0x00, "b": 0x0B, "c": 0x08, "d": 0x02,
    "e": 0x0E, "f": 0x03, "g": 0x05, "h": 0x04,
    "i": 0x22, "j": 0x26, "k": 0x28, "l": 0x25,
    "m": 0x2E, "n": 0x2D, "o": 0x1F, "p": 0x23,
    "q": 0x0C, "r": 0x0F, "s": 0x01, "t": 0x11,
    "u": 0x20, "v": 0x09, "w": 0x0D, "x": 0x07,
    "y": 0x10, "z": 0x06,

    # Numbers
    "0": 0x1D, "1": 0x12, "2": 0x13, "3": 0x14,
    "4": 0x15, "5": 0x17, "6": 0x16, "7": 0x1A,
    "8": 0x1C, "9": 0x19,

    # Punctuation (US layout)
    "-": 0x1B, "=": 0x18, "[": 0x21, "]": 0x1E,
    "\\": 0x2A, ";": 0x29, "'": 0x27, ",": 0x2B,
    ".": 0x2F, "/": 0x2C, "`": 0x32,
}

# ---------------------------------------------------------------------------
# Modifier key → CGEvent flag mapping.
# ---------------------------------------------------------------------------
# CGEventFlags are bitmasks set on keyboard events to indicate
# which modifier keys are held.
# ---------------------------------------------------------------------------

_MODIFIER_FLAGS: dict[str, int] = {
    "command": 0x00100000,   # kCGEventFlagMaskCommand
    "super": 0x00100000,
    "meta": 0x00100000,
    "win": 0x00100000,
    "shift": 0x00020000,     # kCGEventFlagMaskShift
    "lshift": 0x00020000,
    "rshift": 0x00020000,
    "alt": 0x00080000,       # kCGEventFlagMaskAlternate
    "option": 0x00080000,
    "lalt": 0x00080000,
    "ralt": 0x00080000,
    "roption": 0x00080000,
    "ctrl": 0x00040000,      # kCGEventFlagMaskControl
    "control": 0x00040000,
    "lctrl": 0x00040000,
    "rctrl": 0x00040000,
    "fn": 0x00800000,        # kCGEventFlagMaskSecondaryFn
    "capslock": 0x00010000,  # kCGEventFlagMaskAlphaShift
    "caps_lock": 0x00010000,
}


class CGEventInput(InputProvider):
    """Input simulation using macOS Quartz CGEvent APIs.

    All methods use ``CGEventPost`` to inject input events into
    the system event queue via the ``pyobjc-framework-Quartz``
    bridge.  No external dependencies beyond pyobjc.

    Coordinates use Quartz display points with origin at the
    top-left of the primary display — matching the macOS
    Accessibility API coordinate space.

    Key names follow the canonical names defined in
    :class:`~touchpoint.backends.base.InputProvider`.  The
    implementation maps them to macOS virtual key codes via
    ``_KEYCODE_MAP``.
    """

    SELECT_ALL_KEYS: tuple[str, ...] = ("super", "a")
    """Select-all shortcut for macOS (⌘+A)."""

    def __init__(self, *, type_chunk_size: int | None = None) -> None:
        self._available: bool = False
        self._type_chunk_size = type_chunk_size

        if sys.platform != "darwin":
            return

        try:
            import Quartz  # noqa: F401

            self._available = True
        except ImportError:
            pass

    # -- Internal helpers -------------------------------------------------

    def _post_event(self, event: object) -> None:
        """Post a CGEvent to the system event tap.

        Args:
            event: A CGEvent object.

        Raises:
            RuntimeError: If the event could not be posted.
        """
        from Quartz import CGEventPost, kCGHIDEventTap

        CGEventPost(kCGHIDEventTap, event)

    def _key_event(self, keycode: int, key_down: bool) -> object:
        """Create a keyboard CGEvent.

        Args:
            keycode: macOS virtual key code.
            key_down: ``True`` for key-down, ``False`` for key-up.

        Returns:
            A CGEvent for the keyboard action.
        """
        from Quartz import CGEventCreateKeyboardEvent

        return CGEventCreateKeyboardEvent(None, keycode, key_down)

    def _mouse_event(
        self,
        event_type: int,
        x: int,
        y: int,
        button: int = 0,
        click_count: int = 1,
    ) -> object:
        """Create a mouse CGEvent.

        Args:
            event_type: Quartz mouse event type constant.
            x: Horizontal pixel coordinate.
            y: Vertical pixel coordinate.
            button: Mouse button number (0=left, 1=right, 2=middle).
            click_count: Click count for multi-click detection.

        Returns:
            A CGEvent for the mouse action.
        """
        from Quartz import CGEventCreateMouseEvent, CGPointMake, CGEventSetIntegerValueField
        from Quartz import kCGMouseEventClickState

        point = CGPointMake(float(x), float(y))
        event = CGEventCreateMouseEvent(None, event_type, point, button)

        if click_count > 1:
            CGEventSetIntegerValueField(
                event, kCGMouseEventClickState, click_count,
            )

        return event

    def _resolve_keycode(self, key: str) -> int:
        """Resolve a canonical key name to a macOS virtual key code.

        Args:
            key: Canonical key name (e.g. ``"enter"``, ``"a"``).

        Returns:
            macOS virtual key code (integer).

        Raises:
            RuntimeError: If the key name can't be resolved.
        """
        lower = key.lower()
        if lower in _KEYCODE_MAP:
            return _KEYCODE_MAP[lower]

        # Single character — try to find in the map.
        if len(key) == 1:
            if key in _KEYCODE_MAP:
                return _KEYCODE_MAP[key]

        raise RuntimeError(
            f"unknown key {key!r} — not in macOS keycode map"
        )

    def _get_modifier_flags(self, keys: tuple[str, ...]) -> int:
        """Compute combined CGEventFlags for a set of modifier keys.

        Args:
            keys: Modifier key names.

        Returns:
            Combined bitmask of CGEventFlags.
        """
        flags = 0
        for key in keys:
            flag = _MODIFIER_FLAGS.get(key.lower(), 0)
            flags |= flag
        return flags

    # -- Keyboard ---------------------------------------------------------

    def type_text(self, text: str) -> None:
        """Type a string using CGEvent keyboard events.

        Uses ``CGEventKeyboardSetUnicodeString`` to handle
        arbitrary Unicode characters without needing a keymap.
        Each character is sent as a key-down / key-up pair.

        Args:
            text: The string to type.

        Raises:
            RuntimeError: If Quartz is unavailable.
        """
        if not self._available:
            raise RuntimeError("CGEvent is not available")

        from Quartz import (
            CGEventCreateKeyboardEvent,
            CGEventKeyboardSetUnicodeString,
            CGEventPost,
            kCGHIDEventTap,
        )

        chunk = self._type_chunk_size
        if chunk and chunk > 0 and len(text) > chunk:
            # Split into chunks to avoid overwhelming the event queue.
            for i in range(0, len(text), chunk):
                self.type_text(text[i : i + chunk])
            return

        for char in text:
            # Create a key-down event and set the Unicode character.
            down = CGEventCreateKeyboardEvent(None, 0, True)
            CGEventKeyboardSetUnicodeString(down, len(char), char)
            CGEventPost(kCGHIDEventTap, down)

            # Create a key-up event.
            up = CGEventCreateKeyboardEvent(None, 0, False)
            CGEventKeyboardSetUnicodeString(up, len(char), char)
            CGEventPost(kCGHIDEventTap, up)

    def press_key(self, key: str) -> None:
        """Press and release a single key.

        Maps the canonical key name to a macOS virtual key code
        and sends a key-down / key-up pair.

        Args:
            key: Canonical key name (e.g. ``"enter"``, ``"a"``).

        Raises:
            RuntimeError: If the key can't be resolved or Quartz
                is unavailable.
        """
        if not self._available:
            raise RuntimeError("CGEvent is not available")

        keycode = self._resolve_keycode(key)
        self._post_event(self._key_event(keycode, True))
        self._post_event(self._key_event(keycode, False))

    def hotkey(self, *keys: str) -> None:
        """Press a keyboard combination (e.g. ⌘+C).

        Holds all modifier keys down, presses the final key,
        then releases everything in reverse order.  Modifier
        flags are set on all events for correct handling by
        the receiving application.

        Args:
            keys: Two or more canonical key names.
                Example: ``hotkey("super", "shift", "s")`` → ⌘⇧S

        Raises:
            RuntimeError: If any key name can't be resolved or
                Quartz is unavailable.
        """
        if not self._available:
            raise RuntimeError("CGEvent is not available")
        if not keys:
            raise RuntimeError("hotkey() requires at least one key")

        from Quartz import CGEventSetFlags

        keycodes = [self._resolve_keycode(k) for k in keys]

        # Compute combined modifier flags for all modifier keys.
        mod_flags = self._get_modifier_flags(keys)

        # Press all keys down in order, setting modifier flags.
        for i, keycode in enumerate(keycodes):
            event = self._key_event(keycode, True)
            # Build cumulative flags up to and including this key.
            flags_so_far = 0
            for j in range(i + 1):
                flags_so_far |= _MODIFIER_FLAGS.get(
                    keys[j].lower(), 0
                )
            if flags_so_far:
                CGEventSetFlags(event, flags_so_far)
            self._post_event(event)

        # Release all keys in reverse order.
        for i, keycode in enumerate(reversed(keycodes)):
            event = self._key_event(keycode, False)
            # Flags should reflect which modifiers are still held.
            idx = len(keycodes) - 1 - i
            flags_remaining = 0
            for j in range(idx):
                flags_remaining |= _MODIFIER_FLAGS.get(
                    keys[j].lower(), 0
                )
            if flags_remaining:
                CGEventSetFlags(event, flags_remaining)
            self._post_event(event)

    # -- Mouse ------------------------------------------------------------

    def click_at(self, x: int, y: int) -> None:
        """Left-click at screen coordinates.

        Moves the cursor to ``(x, y)`` and performs a left button
        down + up sequence.

        Args:
            x: Horizontal pixel coordinate.
            y: Vertical pixel coordinate.

        Raises:
            RuntimeError: If Quartz is unavailable.
        """
        if not self._available:
            raise RuntimeError("CGEvent is not available")

        from Quartz import (
            kCGEventMouseMoved,
            kCGEventLeftMouseDown,
            kCGEventLeftMouseUp,
        )

        self._post_event(self._mouse_event(kCGEventMouseMoved, x, y))
        time.sleep(0.01)
        self._post_event(
            self._mouse_event(kCGEventLeftMouseDown, x, y)
        )
        self._post_event(
            self._mouse_event(kCGEventLeftMouseUp, x, y)
        )

    def double_click_at(self, x: int, y: int) -> None:
        """Double-click (left button) at screen coordinates.

        Two rapid clicks with proper ``clickCount`` set on the
        second click so macOS recognises it as a double-click.

        Args:
            x: Horizontal pixel coordinate.
            y: Vertical pixel coordinate.

        Raises:
            RuntimeError: If Quartz is unavailable.
        """
        if not self._available:
            raise RuntimeError("CGEvent is not available")

        from Quartz import (
            kCGEventMouseMoved,
            kCGEventLeftMouseDown,
            kCGEventLeftMouseUp,
        )

        self._post_event(self._mouse_event(kCGEventMouseMoved, x, y))
        time.sleep(0.01)

        # First click.
        self._post_event(
            self._mouse_event(kCGEventLeftMouseDown, x, y, click_count=1)
        )
        self._post_event(
            self._mouse_event(kCGEventLeftMouseUp, x, y, click_count=1)
        )
        time.sleep(0.05)

        # Second click with clickCount=2.
        self._post_event(
            self._mouse_event(kCGEventLeftMouseDown, x, y, click_count=2)
        )
        self._post_event(
            self._mouse_event(kCGEventLeftMouseUp, x, y, click_count=2)
        )

    def right_click_at(self, x: int, y: int) -> None:
        """Right-click at screen coordinates.

        Args:
            x: Horizontal pixel coordinate.
            y: Vertical pixel coordinate.

        Raises:
            RuntimeError: If Quartz is unavailable.
        """
        if not self._available:
            raise RuntimeError("CGEvent is not available")

        from Quartz import (
            kCGEventMouseMoved,
            kCGEventRightMouseDown,
            kCGEventRightMouseUp,
            kCGMouseButtonRight,
        )

        self._post_event(self._mouse_event(kCGEventMouseMoved, x, y))
        time.sleep(0.01)
        self._post_event(
            self._mouse_event(
                kCGEventRightMouseDown, x, y,
                button=kCGMouseButtonRight,
            )
        )
        self._post_event(
            self._mouse_event(
                kCGEventRightMouseUp, x, y,
                button=kCGMouseButtonRight,
            )
        )

    def scroll(
        self,
        x: int | None,
        y: int | None,
        direction: str,
        amount: int = 3,
    ) -> None:
        """Scroll at a screen position.

        Uses ``CGEventCreateScrollWheelEvent`` with line-based
        scrolling units.  Positive delta = scroll up/right,
        negative = scroll down/left.

        When *x* and *y* are ``None``, scrolls at the current
        cursor position.

        Args:
            x: Horizontal pixel coordinate.
            y: Vertical pixel coordinate.
            direction: One of ``"up"``, ``"down"``, ``"left"``,
                ``"right"``.
            amount: Number of scroll ticks.  Default ``3``.

        Raises:
            ValueError: If *direction* is not valid.
            RuntimeError: If Quartz is unavailable.
        """
        if not self._available:
            raise RuntimeError("CGEvent is not available")

        if direction not in _SCROLL_DIRECTIONS:
            msg = (
                f"invalid scroll direction {direction!r} — "
                f"expected one of {sorted(_SCROLL_DIRECTIONS)}"
            )
            raise ValueError(msg)

        from Quartz import (
            CGEventCreateScrollWheelEvent,
            CGEventPost,
            kCGHIDEventTap,
            kCGScrollEventUnitLine,
            kCGEventMouseMoved,
        )

        # Move cursor to scroll position first.
        if x is not None and y is not None:
            self._post_event(self._mouse_event(kCGEventMouseMoved, x, y))
            time.sleep(0.01)

        # Determine scroll deltas.
        if direction in ("up", "down"):
            # Vertical scroll: positive = up, negative = down.
            delta_y = amount if direction == "up" else -amount
            delta_x = 0
        else:
            # Horizontal scroll: positive = right(?), negative = left.
            # macOS CGEvent horizontal scroll: positive is typically
            # left in some interpretations; we follow the convention
            # that positive = scroll content right = wheel moves left.
            delta_y = 0
            delta_x = amount if direction == "right" else -amount

        # Send scroll events one tick at a time for reliability.
        for _ in range(abs(amount)):
            dy = 1 if delta_y > 0 else (-1 if delta_y < 0 else 0)
            dx = 1 if delta_x > 0 else (-1 if delta_x < 0 else 0)
            event = CGEventCreateScrollWheelEvent(
                None, kCGScrollEventUnitLine, 2, dy, dx,
            )
            CGEventPost(kCGHIDEventTap, event)

    def mouse_move(self, x: int, y: int) -> None:
        """Move the mouse pointer to screen coordinates.

        Args:
            x: Horizontal pixel coordinate.
            y: Vertical pixel coordinate.

        Raises:
            RuntimeError: If Quartz is unavailable.
        """
        if not self._available:
            raise RuntimeError("CGEvent is not available")

        from Quartz import kCGEventMouseMoved

        self._post_event(self._mouse_event(kCGEventMouseMoved, x, y))

    # -- Window management ------------------------------------------------

    def activate_window(self, title: str, pid: int) -> bool:
        """Bring a window to the foreground.

        Uses ``NSRunningApplication.activateWithOptions:`` to
        activate the application owning the window, then raises
        the matching window via AX.

        Args:
            title: The window's title bar text.
            pid: The process id of the owning application.

        Returns:
            ``True`` if the window was activated.
        """
        if not self._available:
            return False

        try:
            from AppKit import (
                NSWorkspace,
                NSApplicationActivateIgnoringOtherApps,
            )

            for app in NSWorkspace.sharedWorkspace().runningApplications():
                if app.processIdentifier() == pid:
                    app.activateWithOptions_(
                        NSApplicationActivateIgnoringOtherApps
                    )
                    return True
        except Exception:
            pass

        return False

    # -- Availability -----------------------------------------------------

    def is_available(self) -> bool:
        """Return ``True`` if running on macOS with Quartz available.

        Checks that ``sys.platform == 'darwin'`` and that the
        ``Quartz`` module can be imported (pyobjc installed).
        Also verifies Accessibility trust, which is required for
        CGEvent posting.
        """
        return self._available

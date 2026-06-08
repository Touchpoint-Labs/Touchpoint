"""Raw input wrappers — coordinate-based mouse and keyboard simulation.

All functions delegate to the platform's :class:`InputProvider`.
"""

from __future__ import annotations

from touchpoint._state import _get_input, _serialized, _SCROLL_DIRECTIONS


_SPECIAL_KEYS = {"\n": "enter", "\t": "tab", "\b": "backspace"}


@_serialized
def type_text(text: str) -> None:
    """Type a string into the currently focused widget.

    Special characters are converted to keystrokes:

    - ``\\n`` — Enter (line break)
    - ``\\t`` — Tab (move to next field)
    - ``\\b`` — Backspace (delete previous character)

    Args:
        text: The text to type.

    Raises:
        RuntimeError: If no input provider is available.
    """
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


@_serialized
def press_key(key: str) -> None:
    """Press and release a single key.

    Args:
        key: A canonical key name (e.g. ``"enter"``, ``"tab"``,
            ``"escape"``, ``"f5"``, ``"a"``).

    Raises:
        RuntimeError: If no input provider is available.
    """
    _get_input().press_key(key)


@_serialized
def hotkey(*keys: str) -> None:
    """Press a keyboard combination.

    All keys are held down in order, then released in reverse.

    Args:
        keys: Two or more canonical key names.

    Raises:
        RuntimeError: If no input provider is available.
    """
    _get_input().hotkey(*keys)


@_serialized
def click_at(x: int, y: int) -> None:
    """Left-click at screen coordinates.

    Args:
        x: Horizontal pixel coordinate.
        y: Vertical pixel coordinate.

    Raises:
        RuntimeError: If no input provider is available.
    """
    _get_input().click_at(x, y)


@_serialized
def double_click_at(x: int, y: int) -> None:
    """Double-click at screen coordinates.

    Args:
        x: Horizontal pixel coordinate.
        y: Vertical pixel coordinate.

    Raises:
        RuntimeError: If no input provider is available.
    """
    _get_input().double_click_at(x, y)


@_serialized
def right_click_at(x: int, y: int) -> None:
    """Right-click at screen coordinates.

    Args:
        x: Horizontal pixel coordinate.
        y: Vertical pixel coordinate.

    Raises:
        RuntimeError: If no input provider is available.
    """
    _get_input().right_click_at(x, y)


@_serialized
def scroll(
    x: int | None = None,
    y: int | None = None,
    *,
    direction: str,
    amount: int = 3,
) -> None:
    """Scroll at a screen position.

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
    """
    if direction not in _SCROLL_DIRECTIONS:
        raise ValueError(
            f"invalid scroll direction {direction!r}, "
            f"expected one of {sorted(_SCROLL_DIRECTIONS)}"
        )
    _get_input().scroll(x, y, direction, amount)


@_serialized
def mouse_move(x: int, y: int) -> None:
    """Move the mouse pointer to screen coordinates.

    Args:
        x: Horizontal pixel coordinate.
        y: Vertical pixel coordinate.

    Raises:
        RuntimeError: If no input provider is available.
    """
    _get_input().mouse_move(x, y)

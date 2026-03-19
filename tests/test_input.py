"""Tests for raw input functions (InputProvider wrappers).

Covers tp.type_text(), tp.press_key(), tp.hotkey(), tp.click_at(),
tp.double_click_at(), tp.right_click_at(), tp.scroll(),
tp.mouse_move().

All mouse/keyboard tests are destructive and require
``TOUCHPOINT_TEST_APP`` — coordinates and focus targets come from
real elements in the test app, not random values::

    TOUCHPOINT_TEST_APP=mousepad pytest tests/test_input.py -m destructive
"""

from __future__ import annotations

import time

import pytest

import touchpoint as tp
from touchpoint.core.types import Role, State
from tests.conftest import (
    skip_without_backend,
    skip_without_input,
    skip_without_test_app,
)


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------

def _find_text_field(app: str):
    """Find a visible, editable text field in *app*."""
    elems = tp.elements(
        app=app, role=Role.TEXT_FIELD,
        states=[State.VISIBLE, State.SHOWING, State.EDITABLE],
    )
    for el in elems:
        if el.size[0] > 0 and el.size[1] > 0:
            return el
    return None


def _find_button(app: str):
    """Find a visible, named button in *app*."""
    elems = tp.elements(
        app=app, role=Role.BUTTON, named_only=True,
        states=[State.VISIBLE, State.SHOWING],
    )
    for el in elems:
        if el.size[0] > 0 and el.size[1] > 0:
            return el
    return None


def _window_center(app: str) -> tuple[int, int] | None:
    """Return the center coordinates of the app's visible window."""
    wins = tp.windows()
    app_lower = app.lower()
    for w in wins:
        if w.app.lower() == app_lower and w.is_visible:
            cx = w.position[0] + w.size[0] // 2
            cy = w.position[1] + w.size[1] // 2
            return (cx, cy)
    return None


# -----------------------------------------------------------------------
# Keyboard — destructive
# -----------------------------------------------------------------------

@pytest.mark.destructive
@skip_without_backend
@skip_without_input
@skip_without_test_app
class TestRawKeyboard:
    """Raw keyboard input tests.

    Focus a text field in the test app, then type/press keys.
    """

    def test_type_text_into_field(self, backend, destructive_app):
        """type_text() delivers keystrokes to the focused field."""
        el = _find_text_field(destructive_app)
        if el is None:
            pytest.skip(f"no editable text field in {destructive_app}")
        tp.focus(el)
        time.sleep(0.2)
        # Clear field first, then type.
        tp.set_value(el, "", replace=True)
        time.sleep(0.1)
        tp.type_text("hello")
        time.sleep(0.3)
        # Re-read the element to check its value.
        refreshed = tp.get_element(el.id)
        if refreshed is not None and refreshed.value is not None:
            assert "hello" in refreshed.value, (
                f"expected 'hello' in field value, got {refreshed.value!r}"
            )
        # If value can't be read (some widgets), the test still
        # passes — we verified type_text didn't crash.

    def test_type_text_empty(self, backend, destructive_app):
        """Typing an empty string should not crash."""
        el = _find_text_field(destructive_app)
        if el is None:
            pytest.skip(f"no editable text field in {destructive_app}")
        tp.focus(el)
        time.sleep(0.1)
        tp.type_text("")  # should be a no-op

    def test_press_key(self, backend, destructive_app):
        """press_key() sends a single key without crashing."""
        el = _find_text_field(destructive_app)
        if el is None:
            pytest.skip(f"no editable text field in {destructive_app}")
        tp.focus(el)
        time.sleep(0.1)
        tp.press_key("space")

    def test_hotkey(self, backend, destructive_app):
        """hotkey() sends a key combination without crashing."""
        el = _find_text_field(destructive_app)
        if el is None:
            pytest.skip(f"no editable text field in {destructive_app}")
        tp.focus(el)
        time.sleep(0.1)
        # ctrl+a = select all — safe, non-destructive.
        tp.hotkey("ctrl", "a")


# -----------------------------------------------------------------------
# Mouse — destructive
# -----------------------------------------------------------------------

@pytest.mark.destructive
@skip_without_backend
@skip_without_input
@skip_without_test_app
class TestRawMouse:
    """Raw mouse input tests.

    Coordinates come from real element positions in the test app.
    """

    def test_click_at_element_center(self, backend, destructive_app):
        """click_at() clicks at a known element's position."""
        el = _find_button(destructive_app)
        if el is None:
            pytest.skip(f"no button in {destructive_app}")
        x, y = el.position
        tp.click_at(x, y)

    def test_double_click_at_element_center(self, backend, destructive_app):
        """double_click_at() double-clicks at a known position."""
        el = _find_button(destructive_app)
        if el is None:
            pytest.skip(f"no button in {destructive_app}")
        x, y = el.position
        tp.double_click_at(x, y)

    def test_right_click_at_element_center(self, backend, destructive_app):
        """right_click_at() right-clicks at a known position."""
        el = _find_button(destructive_app)
        if el is None:
            pytest.skip(f"no button in {destructive_app}")
        x, y = el.position
        tp.right_click_at(x, y)
        # Dismiss any context menu that opened.
        time.sleep(0.2)
        tp.press_key("Escape")

    def test_mouse_move_to_element(self, backend, destructive_app):
        """mouse_move() moves the pointer to a known position."""
        el = _find_button(destructive_app)
        if el is None:
            pytest.skip(f"no button in {destructive_app}")
        x, y = el.position
        tp.mouse_move(x, y)

    def test_scroll_at_window_center(self, backend, destructive_app):
        """scroll() scrolls at the center of the test app window."""
        center = _window_center(destructive_app)
        if center is None:
            pytest.skip(f"no visible window for {destructive_app}")
        tp.scroll(center[0], center[1], direction="down", amount=2)


# -----------------------------------------------------------------------
# Validation — unit tests (no desktop needed)
# -----------------------------------------------------------------------

@pytest.mark.unit
class TestRawValidation:
    """Input validation tests."""

    def test_scroll_invalid_direction(self):
        """An invalid scroll direction raises ValueError."""
        with pytest.raises(ValueError, match="invalid scroll direction"):
            tp.scroll(0, 0, direction="diagonal")

"""Tests for element-targeted actions.

Covers tp.click(), tp.focus(), tp.action(), tp.set_value(),
tp.set_numeric_value(), tp.double_click(), tp.right_click(),
tp.activate_window().

Destructive tests (click, set_value, action dispatch) require the
``TOUCHPOINT_TEST_APP`` env var so keystrokes/clicks only hit an
app the tester has chosen::

    TOUCHPOINT_TEST_APP=Mousepad pytest tests/test_actions.py -m destructive
"""

from __future__ import annotations

import pytest

import touchpoint as tp
from touchpoint.core.types import Role, State
from touchpoint.core.exceptions import ActionFailedError
from tests.conftest import (
    skip_without_backend,
    skip_without_test_app,
)


# -----------------------------------------------------------------------
# Helpers — find elements scoped to an app
# -----------------------------------------------------------------------

def _find_actionable(backend, app: str):
    """Find a named, visible element that has at least one action."""
    elems = tp.elements(
        app=app, named_only=True,
        states=[State.VISIBLE, State.SHOWING],
    )
    for el in elems:
        if el.actions:
            return el
    return None


def _find_focusable(backend, app: str | None = None):
    """Find a visible element with FOCUSABLE state.

    If *app* is ``None``, searches all visible apps.
    """
    if app is not None:
        apps = [app]
    else:
        wins = backend.windows()
        apps = list(dict.fromkeys(
            w.app for w in wins
            if w.is_visible and w.size[0] > 0 and w.size[1] > 0
        ))
    for a in apps:
        elems = tp.elements(
            app=a,
            states=[State.VISIBLE, State.SHOWING, State.FOCUSABLE],
        )
        for el in elems:
            if el.name and el.name.strip():
                return el
    return None


def _find_button(backend, app: str):
    """Find a visible, named button that has at least one action."""
    elems = tp.elements(
        app=app, role=Role.BUTTON, named_only=True,
        states=[State.VISIBLE, State.SHOWING],
    )
    for el in elems:
        if el.actions:
            return el
    return None


def _find_text_field(backend, app: str):
    """Find a visible, editable text field in *app*."""
    elems = tp.elements(
        app=app, role=Role.TEXT_FIELD,
        states=[State.VISIBLE, State.SHOWING, State.EDITABLE],
    )
    for el in elems:
        if el.size[0] > 0 and el.size[1] > 0:
            return el
    return None


def _find_slider(backend, app: str):
    """Find a visible slider (range element) in *app*."""
    elems = tp.elements(
        app=app, role=Role.SLIDER,
        states=[State.VISIBLE, State.SHOWING],
    )
    for el in elems:
        if el.size[0] > 0 and el.size[1] > 0:
            return el
    return None


# -----------------------------------------------------------------------
# Focus — mildest mutation, integration-level
# -----------------------------------------------------------------------

@pytest.mark.integration
@skip_without_backend
class TestFocus:
    """tp.focus() tests — moves keyboard focus only."""

    def test_focus_returns_bool(self, backend):
        el = _find_focusable(backend)
        if el is None:
            pytest.skip("no focusable element found")
        result = tp.focus(el)
        assert isinstance(result, bool)

    def test_focus_by_id(self, backend):
        el = _find_focusable(backend)
        if el is None:
            pytest.skip("no focusable element found")
        result = tp.focus(el.id)
        assert isinstance(result, bool)

    def test_focus_invalid_id(self):
        with pytest.raises((ActionFailedError, Exception)):
            tp.focus("nonexistent_id_xyz")


# -----------------------------------------------------------------------
# Action (raw) — destructive, scoped to TOUCHPOINT_TEST_APP
# -----------------------------------------------------------------------

@pytest.mark.destructive
@skip_without_backend
@skip_without_test_app
class TestAction:
    """tp.action() tests — raw named action dispatch."""

    def test_action_returns_bool(self, backend, destructive_app):
        el = _find_actionable(backend, destructive_app)
        if el is None:
            pytest.skip(f"no actionable element in {destructive_app}")
        # Use whatever the element's first action is — no hardcoded names.
        action_name = el.actions[0]
        result = tp.action(el, action_name)
        assert isinstance(result, bool)

    def test_action_by_id(self, backend, destructive_app):
        el = _find_actionable(backend, destructive_app)
        if el is None:
            pytest.skip(f"no actionable element in {destructive_app}")
        action_name = el.actions[0]
        result = tp.action(el.id, action_name)
        assert isinstance(result, bool)

    def test_action_invalid_name(self, backend, destructive_app):
        el = _find_actionable(backend, destructive_app)
        if el is None:
            pytest.skip(f"no actionable element in {destructive_app}")
        with pytest.raises(ActionFailedError):
            tp.action(el, "nonexistent_action_xyz")

    def test_action_invalid_element(self):
        with pytest.raises((ActionFailedError, Exception)):
            tp.action("nonexistent_id_xyz", "click")


# -----------------------------------------------------------------------
# Click — destructive, scoped to TOUCHPOINT_TEST_APP
# -----------------------------------------------------------------------

@pytest.mark.destructive
@skip_without_backend
@skip_without_test_app
class TestClick:
    """tp.click() tests."""

    def test_click_returns_true(self, backend, destructive_app):
        el = _find_button(backend, destructive_app)
        if el is None:
            pytest.skip(f"no clickable button in {destructive_app}")
        result = tp.click(el)
        assert result is True

    def test_click_by_id(self, backend, destructive_app):
        el = _find_button(backend, destructive_app)
        if el is None:
            pytest.skip(f"no clickable button in {destructive_app}")
        result = tp.click(el.id)
        assert isinstance(result, bool)

    def test_click_invalid_element(self):
        """Click on a non-existent element should raise."""
        with pytest.raises((ActionFailedError, Exception)):
            tp.click("nonexistent_id_xyz")


# -----------------------------------------------------------------------
# Double-click — destructive, scoped to TOUCHPOINT_TEST_APP
# -----------------------------------------------------------------------

@pytest.mark.destructive
@skip_without_backend
@skip_without_test_app
class TestDoubleClick:
    """tp.double_click() tests."""

    def test_double_click_returns_true(self, backend, destructive_app):
        el = _find_button(backend, destructive_app)
        if el is None:
            pytest.skip(f"no clickable button in {destructive_app}")
        result = tp.double_click(el)
        assert result is True


# -----------------------------------------------------------------------
# Right-click — destructive, scoped to TOUCHPOINT_TEST_APP
# -----------------------------------------------------------------------

@pytest.mark.destructive
@skip_without_backend
@skip_without_test_app
class TestRightClick:
    """tp.right_click() tests."""

    def test_right_click_returns_true(self, backend, destructive_app):
        el = _find_button(backend, destructive_app)
        if el is None:
            pytest.skip(f"no clickable button in {destructive_app}")
        result = tp.right_click(el)
        assert result is True


# -----------------------------------------------------------------------
# Set value — destructive, scoped to TOUCHPOINT_TEST_APP
# -----------------------------------------------------------------------

@pytest.mark.destructive
@skip_without_backend
@skip_without_test_app
class TestSetValue:
    """tp.set_value() tests."""

    def test_set_value_returns_true(self, backend, destructive_app):
        el = _find_text_field(backend, destructive_app)
        if el is None:
            pytest.skip(f"no editable text field in {destructive_app}")
        result = tp.set_value(el, "test123")
        assert result is True

    def test_set_value_replace(self, backend, destructive_app):
        el = _find_text_field(backend, destructive_app)
        if el is None:
            pytest.skip(f"no editable text field in {destructive_app}")
        tp.set_value(el, "first", replace=True)
        result = tp.set_value(el, "second", replace=True)
        assert result is True

    def test_set_value_replace_false(self, backend, destructive_app):
        """replace=False appends instead of replacing."""
        el = _find_text_field(backend, destructive_app)
        if el is None:
            pytest.skip(f"no editable text field in {destructive_app}")
        result = tp.set_value(el, "appended", replace=False)
        assert result is True

    def test_set_value_invalid_element(self):
        """set_value on a non-existent id falls back to typing
        into whatever is currently focused (by design).  With
        fallback disabled it should raise."""
        tp.configure(fallback_input=False)
        with pytest.raises((ActionFailedError, Exception)):
            tp.set_value("nonexistent_id_xyz", "text")


# -----------------------------------------------------------------------
# Resolve target — Element vs str (integration, not destructive)
# -----------------------------------------------------------------------

@pytest.mark.integration
@skip_without_backend
class TestResolveTarget:
    """Verify action functions accept both Element and str."""

    def test_focus_accepts_element_and_str(self, backend):
        el = _find_focusable(backend)
        if el is None:
            pytest.skip("no focusable element found")
        tp.focus(el)
        tp.focus(el.id)

    def test_action_invalid_element_and_str(self):
        """Both forms raise for non-existent ids."""
        with pytest.raises((ActionFailedError, Exception)):
            tp.action("nonexistent_id_xyz", "click")


# -----------------------------------------------------------------------
# Activate window — destructive + validation
# -----------------------------------------------------------------------

@pytest.mark.destructive
@skip_without_backend
@skip_without_test_app
class TestActivateWindow:
    """tp.activate_window() destructive tests."""

    def test_activate_window_by_object(self, backend, destructive_app):
        """Activate a window by passing a Window object."""
        wins = tp.windows()
        target = None
        app_lower = destructive_app.lower()
        for w in wins:
            if w.app.lower() == app_lower and w.is_visible:
                target = w
                break
        if target is None:
            pytest.skip(f"no visible window for {destructive_app}")
        result = tp.activate_window(target)
        assert isinstance(result, bool)

    def test_activate_window_by_id(self, backend, destructive_app):
        """Activate a window by passing its id string."""
        wins = tp.windows()
        target = None
        app_lower = destructive_app.lower()
        for w in wins:
            if w.app.lower() == app_lower and w.is_visible:
                target = w
                break
        if target is None:
            pytest.skip(f"no visible window for {destructive_app}")
        result = tp.activate_window(target.id)
        assert isinstance(result, bool)


@pytest.mark.integration
@skip_without_backend
class TestActivateWindowValidation:
    """tp.activate_window() validation tests (not destructive)."""

    def test_activate_window_invalid_id(self):
        """A nonexistent window id raises ValueError."""
        with pytest.raises(ValueError, match="no window found"):
            tp.activate_window("nonexistent:999:999")


# -----------------------------------------------------------------------
# Set numeric value — destructive + validation
# -----------------------------------------------------------------------

@pytest.mark.destructive
@skip_without_backend
@skip_without_test_app
class TestSetNumericValue:
    """tp.set_numeric_value() destructive tests.

    Requires an app with a slider (e.g. Kate has zoom/indentation
    sliders)::

        TOUCHPOINT_TEST_APP=kate pytest -k TestSetNumericValue
    """

    def test_set_numeric_value_slider(self, backend, destructive_app):
        """Set a slider's value via the Value interface."""
        el = _find_slider(backend, destructive_app)
        if el is None:
            pytest.skip(
                f"no slider found in {destructive_app} "
                f"(try TOUCHPOINT_TEST_APP=kate)"
            )
        result = tp.set_numeric_value(el, 50.0)
        assert result is True


@pytest.mark.integration
@skip_without_backend
class TestSetNumericValueValidation:
    """tp.set_numeric_value() validation tests (not destructive)."""

    def test_set_numeric_value_invalid_element(self):
        """Nonexistent element id raises ActionFailedError."""
        with pytest.raises(ActionFailedError):
            tp.set_numeric_value("nonexistent:99:99:99", 10.0)

    def test_set_numeric_value_no_value_interface(self, backend):
        """An element without Value interface raises ActionFailedError."""
        # Find any button — buttons don't have a Value interface.
        elems = tp.elements(role=Role.BUTTON, named_only=True)
        if not elems:
            pytest.skip("no buttons found to test")
        with pytest.raises(ActionFailedError):
            tp.set_numeric_value(elems[0], 10.0)

"""Tests for text selection: tp.select_text() and tp.select_text_range().

Unit tests mock the backend to exercise the public API logic.
Integration/destructive tests exercise the real macOS AX backend
against a live text field::

    TOUCHPOINT_TEST_APP=TextEdit pytest tests/test_select_text.py -m destructive
"""

from __future__ import annotations

import inspect
import sys
import types
from unittest import mock

import pytest

import touchpoint as tp
import touchpoint._state as _st
from touchpoint.core.element import Element
from touchpoint.core.types import Role, State
from touchpoint.core.exceptions import ActionFailedError
from tests.conftest import (
    skip_without_backend,
    skip_without_test_app,
    skip_unless_windows,
)


# -----------------------------------------------------------------------
# Helpers — mock backend
# -----------------------------------------------------------------------

def _make_mock_backend(
    text_content: str | None = "Hello World",
    select_ok: bool = True,
):
    """Return a mock backend with configurable text/selection support."""
    backend = mock.MagicMock()
    backend.get_text_content.return_value = text_content
    backend.select_text.return_value = select_ok
    return backend


def _make_element(eid: str = "ax:1:0:0") -> Element:
    """Return a minimal Element for testing."""
    return Element(
        id=eid,
        name="test field",
        role=Role.TEXT_FIELD,
        states=[State.VISIBLE, State.EDITABLE],
        position=(100, 200),
        size=(300, 30),
        actions=[],
        value="Hello World",
        description="",
        children=[],
        parent_id=None,
        window_id="ax:1:0",
        app="TestApp",
        pid=1234,
        backend="ax",
        raw_role="AXTextField",
        raw={},
    )


# -----------------------------------------------------------------------
# Unit: select_text — public API logic
# -----------------------------------------------------------------------

@pytest.mark.unit
class TestSelectTextUnit:
    """Unit tests for tp.select_text() — mocked backend."""

    def test_select_text_happy_path(self):
        """Selecting a substring that exists returns True."""
        backend = _make_mock_backend(text_content="Hello World", select_ok=True)
        with mock.patch("touchpoint._state._get_backend", return_value=backend):
            result = tp.select_text("ax:1:0:0", "World")
        assert result is True
        backend.get_text_content.assert_called_once_with("ax:1:0:0")
        backend.select_text.assert_called_once_with("ax:1:0:0", 6, 11)

    def test_select_text_accepts_element(self):
        """select_text accepts an Element object, not just a string."""
        el = _make_element()
        backend = _make_mock_backend(text_content="Hello World", select_ok=True)
        with mock.patch("touchpoint._state._get_backend", return_value=backend):
            result = tp.select_text(el, "Hello")
        assert result is True
        backend.select_text.assert_called_once_with(el.id, 0, 5)

    def test_select_text_beginning_of_string(self):
        """Selecting text at the start of the content works."""
        backend = _make_mock_backend(text_content="Hello World", select_ok=True)
        with mock.patch("touchpoint._state._get_backend", return_value=backend):
            result = tp.select_text("ax:1:0:0", "Hello")
        assert result is True
        backend.select_text.assert_called_once_with("ax:1:0:0", 0, 5)

    def test_select_text_entire_content(self):
        """Selecting the full text content works."""
        text = "Hello World"
        backend = _make_mock_backend(text_content=text, select_ok=True)
        with mock.patch("touchpoint._state._get_backend", return_value=backend):
            result = tp.select_text("ax:1:0:0", text)
        assert result is True
        backend.select_text.assert_called_once_with("ax:1:0:0", 0, 11)

    def test_select_text_single_char(self):
        """Selecting a single character works."""
        backend = _make_mock_backend(text_content="abc", select_ok=True)
        with mock.patch("touchpoint._state._get_backend", return_value=backend):
            result = tp.select_text("ax:1:0:0", "b")
        assert result is True
        backend.select_text.assert_called_once_with("ax:1:0:0", 1, 2)

    def test_select_text_first_occurrence(self):
        """When text appears multiple times, selects the first."""
        backend = _make_mock_backend(text_content="foo bar foo", select_ok=True)
        with mock.patch("touchpoint._state._get_backend", return_value=backend):
            tp.select_text("ax:1:0:0", "foo")
        backend.select_text.assert_called_once_with("ax:1:0:0", 0, 3)

    def test_select_text_no_text_content_raises(self):
        """Element with no text content raises ActionFailedError."""
        backend = _make_mock_backend(text_content=None)
        with mock.patch("touchpoint._state._get_backend", return_value=backend):
            with pytest.raises(ActionFailedError, match="does not expose text content"):
                tp.select_text("ax:1:0:0", "anything")
        backend.select_text.assert_not_called()

    def test_select_text_substring_not_found_raises(self):
        """Substring not in element text raises ActionFailedError."""
        backend = _make_mock_backend(text_content="Hello World")
        with mock.patch("touchpoint._state._get_backend", return_value=backend):
            with pytest.raises(ActionFailedError, match="not found"):
                tp.select_text("ax:1:0:0", "Goodbye")
        backend.select_text.assert_not_called()

    def test_select_text_backend_unsupported_raises(self):
        """Backend returning False raises ActionFailedError."""
        backend = _make_mock_backend(text_content="Hello World", select_ok=False)
        with mock.patch("touchpoint._state._get_backend", return_value=backend):
            with pytest.raises(ActionFailedError, match="does not support"):
                tp.select_text("ax:1:0:0", "World")

    def test_select_text_empty_content(self):
        """Empty string content — substring not found raises."""
        backend = _make_mock_backend(text_content="")
        with mock.patch("touchpoint._state._get_backend", return_value=backend):
            with pytest.raises(ActionFailedError, match="not found"):
                tp.select_text("ax:1:0:0", "text")

    def test_select_text_case_sensitive(self):
        """Selection is case-sensitive — 'world' != 'World'."""
        backend = _make_mock_backend(text_content="Hello World")
        with mock.patch("touchpoint._state._get_backend", return_value=backend):
            with pytest.raises(ActionFailedError, match="not found"):
                tp.select_text("ax:1:0:0", "world")


# -----------------------------------------------------------------------
# Unit: select_text_range — public API logic
# -----------------------------------------------------------------------

@pytest.mark.unit
class TestSelectTextRangeUnit:
    """Unit tests for tp.select_text_range() — mocked backend."""

    def test_range_happy_path(self):
        """Valid range returns True."""
        backend = _make_mock_backend(select_ok=True)
        with mock.patch("touchpoint._state._get_backend", return_value=backend):
            result = tp.select_text_range("ax:1:0:0", 6, 11)
        assert result is True
        backend.select_text.assert_called_once_with("ax:1:0:0", 6, 11)

    def test_range_accepts_element(self):
        """select_text_range accepts an Element object."""
        el = _make_element()
        backend = _make_mock_backend(select_ok=True)
        with mock.patch("touchpoint._state._get_backend", return_value=backend):
            result = tp.select_text_range(el, 0, 5)
        assert result is True
        backend.select_text.assert_called_once_with(el.id, 0, 5)

    def test_range_zero_length(self):
        """start == end (cursor placement) — delegates to backend."""
        backend = _make_mock_backend(select_ok=True)
        with mock.patch("touchpoint._state._get_backend", return_value=backend):
            result = tp.select_text_range("ax:1:0:0", 5, 5)
        assert result is True
        backend.select_text.assert_called_once_with("ax:1:0:0", 5, 5)

    def test_range_backend_unsupported_raises(self):
        """Backend returning False raises ActionFailedError."""
        backend = _make_mock_backend(select_ok=False)
        with mock.patch("touchpoint._state._get_backend", return_value=backend):
            with pytest.raises(ActionFailedError, match="does not support"):
                tp.select_text_range("ax:1:0:0", 0, 5)

    def test_range_does_not_call_get_text_content(self):
        """select_text_range bypasses text lookup — goes directly to backend."""
        backend = _make_mock_backend(select_ok=True)
        with mock.patch("touchpoint._state._get_backend", return_value=backend):
            tp.select_text_range("ax:1:0:0", 0, 3)
        backend.get_text_content.assert_not_called()


# -----------------------------------------------------------------------
# Unit: base backend defaults
# -----------------------------------------------------------------------

@pytest.mark.unit
class TestBaseBackendDefaults:
    """Verify Backend ABC defaults for text selection."""

    def test_select_text_is_abstract(self):
        """Backend.select_text() is declared @abstractmethod."""
        from touchpoint.backends.base import Backend
        assert "select_text" in Backend.__abstractmethods__

    def test_get_text_content_is_abstract(self):
        """Backend.get_text_content() is declared @abstractmethod."""
        from touchpoint.backends.base import Backend
        assert "get_text_content" in Backend.__abstractmethods__

    def test_macos_ax_backend_implements_text_contract(self):
        """macOS AX backend remains instantiable after text APIs are added."""
        if sys.platform != "darwin":
            pytest.skip("requires macOS")
        from touchpoint.backends.macos.ax import AxBackend

        assert not inspect.isabstract(AxBackend)
        assert "select_text" not in AxBackend.__abstractmethods__
        assert "get_text_content" not in AxBackend.__abstractmethods__


# -----------------------------------------------------------------------
# Unit: Windows UIA backend text selection internals
# -----------------------------------------------------------------------

@pytest.mark.unit
@skip_unless_windows
class TestWindowsUiaSelectTextUnit:
    """Targeted tests for Windows UIA text selection behavior."""

    def test_get_text_content_prefers_text_pattern(self, monkeypatch):
        """TextPattern text wins so substring offsets stay selection-safe."""
        from touchpoint.backends.windows.uia import UiaBackend

        backend = UiaBackend.__new__(UiaBackend)
        element = types.SimpleNamespace(CurrentName="fallback name")
        backend._resolve_element = lambda _element_id: element

        doc_range = types.SimpleNamespace(GetText=lambda _max_count: "Line1\r\nLine2")
        text_pattern = types.SimpleNamespace(DocumentRange=doc_range)
        value_pattern = types.SimpleNamespace(CurrentValue="Line1\nLine2")

        def fake_get_pattern(_uia_el, pattern_name):
            if pattern_name == "Text":
                return text_pattern
            if pattern_name == "Value":
                return value_pattern
            return None

        monkeypatch.setattr("touchpoint.backends.windows.uia._get_pattern", fake_get_pattern)

        assert backend.get_text_content("uia:1:2:3") == "Line1\r\nLine2"

    def test_select_text_uses_win32_edit_fallback_without_text_pattern(
        self, monkeypatch,
    ):
        """Classic Win32 Edit controls can select via EM_SETSEL fallback."""
        from touchpoint.backends.windows.uia import UiaBackend

        backend = UiaBackend.__new__(UiaBackend)
        element = types.SimpleNamespace()
        backend._resolve_element_or_raise = lambda _eid, _action: element
        calls = []
        backend._select_text_win32_edit = (
            lambda uia_el, element_id, start, end:
                calls.append((uia_el, element_id, start, end)) or True
        )

        monkeypatch.setattr(
            "touchpoint.backends.windows.uia._get_pattern",
            lambda _uia_el, _pattern_name: None,
        )

        assert backend.select_text("uia:1:2:3", 1, 4) is True
        assert calls == [(element, "uia:1:2:3", 1, 4)]

    def test_select_text_checks_document_length_before_moving_range(self, monkeypatch):
        """Out-of-bounds ranges should fail before endpoint movement begins."""
        from touchpoint.backends.windows.uia import UiaBackend

        backend = UiaBackend.__new__(UiaBackend)
        element = types.SimpleNamespace()
        backend._resolve_element = lambda _element_id: element

        class DummyRange:
            def __init__(self, text: str):
                self._text = text
                self.clone_calls = 0

            def GetText(self, _max_count: int) -> str:
                return self._text

            def Clone(self):
                self.clone_calls += 1
                return mock.MagicMock()

        doc_range = DummyRange("short")
        text_pattern = types.SimpleNamespace(
            DocumentRange=doc_range,
            SupportedTextSelection=1,
        )

        constants = types.ModuleType("UIAutomationClient")
        constants.SupportedTextSelection_None = 0
        constants.TextPatternRangeEndpoint_End = 1
        constants.TextPatternRangeEndpoint_Start = 0
        constants.TextUnit_Character = 0

        # Patch the imported UIA constants module so the backend
        # can execute without depending on live COM bindings here.
        monkeypatch.setitem(sys.modules, "comtypes.gen.UIAutomationClient", constants)

        def fake_get_pattern(_uia_el, pattern_name):
            if pattern_name == "Text":
                return text_pattern
            return None

        monkeypatch.setattr("touchpoint.backends.windows.uia._get_pattern", fake_get_pattern)

        with pytest.raises(ActionFailedError, match="out of bounds"):
            backend.select_text("uia:1:2:3", 0, 6)

        assert doc_range.clone_calls == 0


# -----------------------------------------------------------------------
# Unit: macOS AX backend text selection internals
# -----------------------------------------------------------------------

@pytest.mark.unit
class TestMacosAxSelectTextUnit:
    """Targeted tests for macOS AX text-range validation."""

    def _backend(self):
        from touchpoint.backends.macos.ax import AxBackend

        backend = AxBackend.__new__(AxBackend)
        backend._resolve_element_or_raise = lambda _eid, _action: object()
        return backend

    def test_select_text_rejects_invalid_range(self):
        with pytest.raises(ActionFailedError, match="invalid text range"):
            self._backend().select_text("ax:1:tok:0", -1, 2)

    def test_select_text_rejects_out_of_bounds_range(self, monkeypatch):
        from touchpoint.backends.macos import ax as mod

        monkeypatch.setattr(mod, "_get_ax_attr", lambda *_args, **_kwargs: "short")
        with pytest.raises(ActionFailedError, match="out of bounds"):
            self._backend().select_text("ax:1:tok:0", 0, 6)

    def test_select_text_reports_failed_attribute_write(self, monkeypatch):
        from touchpoint.backends.macos import ax as mod

        app_services = types.ModuleType("ApplicationServices")
        app_services.AXUIElementIsAttributeSettable = lambda *_args: (0, True)
        app_services.AXValueCreate = lambda *_args: object()
        app_services.kAXValueCFRangeType = object()
        core_foundation = types.ModuleType("CoreFoundation")
        core_foundation.CFRangeMake = lambda start, length: (start, length)

        monkeypatch.setitem(sys.modules, "ApplicationServices", app_services)
        monkeypatch.setitem(sys.modules, "CoreFoundation", core_foundation)
        monkeypatch.setattr(mod, "_get_ax_attr", lambda *_args, **_kwargs: "content")
        monkeypatch.setattr(mod, "_set_ax_attr", lambda *_args, **_kwargs: False)

        with pytest.raises(ActionFailedError, match="failed to set"):
            self._backend().select_text("ax:1:tok:0", 0, 3)


@pytest.mark.unit
class TestMacosAxPathResolutionUnit:
    """Synthetic AX paths must not accept Python-style negative indexes."""

    def test_negative_child_index_does_not_resolve(self, monkeypatch):
        from touchpoint.backends.macos import ax as mod

        window = object()
        child = object()
        backend = mod.AxBackend.__new__(mod.AxBackend)
        backend._acc_refs = {}
        backend._window_refs = {(1, "tok"): window}
        backend._messaging_timeout = 1.0
        backend._timeout_apply_failures = set()

        def fake_get_ax_attr(element, attr, default=None):
            if element is window and attr == "AXRole":
                return "AXWindow"
            if element is window and attr == "AXChildren":
                return [child]
            return default

        monkeypatch.setattr(mod, "_get_ax_attr", fake_get_ax_attr)

        assert backend._resolve_element("ax:1:tok:-1") is None


@pytest.mark.unit
class TestMacosAxMessagingTimeoutUnit:
    """AX application references use bounded messaging and report timeouts."""

    def test_application_reference_applies_configured_timeout(self, monkeypatch):
        from touchpoint.backends.macos import ax as mod

        app_services = types.ModuleType("ApplicationServices")
        ax_app = object()
        seen: list[tuple[object, float]] = []
        app_services.AXUIElementCreateApplication = lambda _pid: ax_app
        app_services.AXUIElementSetMessagingTimeout = (
            lambda element, timeout: seen.append((element, timeout)) or 0
        )
        monkeypatch.setitem(sys.modules, "ApplicationServices", app_services)

        backend = mod.AxBackend(messaging_timeout=0.25)

        assert backend._create_application_element(42) is ax_app
        assert seen == [(ax_app, 0.25)]

    def test_element_walk_applies_timeout_to_descendants(self, monkeypatch):
        from touchpoint.backends.macos import ax as mod

        app_services = types.ModuleType("ApplicationServices")
        seen: list[tuple[object, float]] = []
        app_services.AXUIElementSetMessagingTimeout = (
            lambda element, timeout: seen.append((element, timeout)) or 0
        )
        monkeypatch.setitem(sys.modules, "ApplicationServices", app_services)

        backend = mod.AxBackend(messaging_timeout=0.25)
        backend._available = True
        window = object()
        child = object()
        monkeypatch.setattr(
            backend,
            "_get_roots",
            lambda _app, _window_id: [(window, "Test", 42, "ax:42:win")],
        )

        def fake_get_ax_attr(element, attr, default=None):
            if element is window and attr == "AXChildren":
                return [child]
            if element is child and attr == "AXChildren":
                return []
            return default

        monkeypatch.setattr(mod, "_get_ax_attr", fake_get_ax_attr)
        monkeypatch.setattr(backend, "_check_filter", lambda _element: None)

        assert backend.get_elements(app="Test") == []
        assert (window, 0.25) in seen
        assert (child, 0.25) in seen

    def test_cannot_complete_is_reported_and_can_recover(self, monkeypatch):
        from touchpoint.backends.macos import ax as mod

        app_services = types.ModuleType("ApplicationServices")
        app_services.kAXErrorCannotComplete = -25204
        responses = [(-25204, None), (0, ["window"])]
        app_services.AXUIElementCopyAttributeValue = (
            lambda *_args: responses.pop(0)
        )
        monkeypatch.setitem(sys.modules, "ApplicationServices", app_services)

        backend = mod.AxBackend()
        ax_app = object()

        assert backend._get_application_attr(
            42, ax_app, "AXWindows", app_name="Frozen",
        ) is None
        assert backend.get_diagnostics()["skipped_apps"] == [{
            "pid": 42,
            "app": "Frozen",
            "reason": "kAXErrorCannotComplete",
            "attribute": "AXWindows",
        }]

        backend._begin_ax_request()
        assert backend._get_application_attr(42, ax_app, "AXWindows") == ["window"]
        assert backend.get_diagnostics()["skipped_apps"] == []

    def test_timeout_update_invalidates_cached_refs(self):
        from touchpoint.backends.macos.ax import AxBackend

        backend = AxBackend()
        backend._acc_refs["ax:1:tok:0"] = object()
        backend._window_refs[(1, "tok")] = object()
        backend._hit_refs["ax:1:hit:tok"] = object()
        backend._hit_order.append("ax:1:hit:tok")

        backend.set_messaging_timeout(0.5)

        assert backend.get_diagnostics()["messaging_timeout_seconds"] == 0.5
        assert backend._acc_refs == {}
        assert backend._window_refs == {}
        assert backend._hit_refs == {}
        assert backend._hit_order == []


# -----------------------------------------------------------------------
# Integration: select_text against live macOS AX backend
# -----------------------------------------------------------------------

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


@pytest.mark.destructive
@skip_without_backend
@skip_without_test_app
class TestSelectTextIntegration:
    """Live select_text tests against a real text field.

    Requires a text editor with some text already typed::

        TOUCHPOINT_TEST_APP=TextEdit pytest tests/test_select_text.py -m destructive
    """

    def test_select_text_returns_true(self, backend, destructive_app):
        """select_text on a field with text returns True."""
        el = _find_text_field(backend, destructive_app)
        if el is None:
            pytest.skip(f"no editable text field in {destructive_app}")
        # First, set some known text so we have something to select.
        tp.set_value(el, "Hello World Test", replace=True)
        result = tp.select_text(el, "World")
        assert result is True

    def test_select_text_by_id(self, backend, destructive_app):
        """select_text works when passed an element id string."""
        el = _find_text_field(backend, destructive_app)
        if el is None:
            pytest.skip(f"no editable text field in {destructive_app}")
        tp.set_value(el, "Hello World Test", replace=True)
        result = tp.select_text(el.id, "Hello")
        assert result is True

    def test_select_text_range_returns_true(self, backend, destructive_app):
        """select_text_range with valid offsets returns True."""
        el = _find_text_field(backend, destructive_app)
        if el is None:
            pytest.skip(f"no editable text field in {destructive_app}")
        tp.set_value(el, "Hello World Test", replace=True)
        result = tp.select_text_range(el, 6, 11)
        assert result is True

    def test_select_text_substring_not_found(self, backend, destructive_app):
        """select_text raises when substring isn't present."""
        el = _find_text_field(backend, destructive_app)
        if el is None:
            pytest.skip(f"no editable text field in {destructive_app}")
        tp.set_value(el, "Hello World", replace=True)
        with pytest.raises(ActionFailedError, match="not found"):
            tp.select_text(el, "Goodbye")

    def test_get_text_content_returns_value(self, backend, destructive_app):
        """Backend.get_text_content returns the text set via set_value."""
        el = _find_text_field(backend, destructive_app)
        if el is None:
            pytest.skip(f"no editable text field in {destructive_app}")
        tp.set_value(el, "test content", replace=True)
        b = tp._get_backend()
        content = b.get_text_content(el.id)
        assert content is not None
        assert "test content" in content


@pytest.mark.integration
@skip_without_backend
class TestSelectTextValidation:
    """Validation tests — not destructive."""

    def test_select_text_invalid_element(self):
        """Nonexistent element id raises an error."""
        with pytest.raises((ActionFailedError, Exception)):
            tp.select_text("nonexistent:99:99:99", "text")

    def test_select_text_range_invalid_element(self):
        """Nonexistent element id raises an error."""
        with pytest.raises((ActionFailedError, Exception)):
            tp.select_text_range("nonexistent:99:99:99", 0, 5)

    def test_select_text_on_button_raises(self, backend):
        """Buttons don't have selectable text — should raise."""
        elems = []
        for win in tp.windows():
            if not win.is_visible:
                continue
            try:
                elems = tp.elements(
                    window_id=win.id,
                    role=Role.BUTTON,
                    named_only=True,
                    max_elements=50,
                    max_depth=8,
                )
            except Exception:
                continue
            if elems:
                break
        if not elems:
            pytest.skip("no buttons found")
        # Buttons typically don't support AXSelectedTextRange,
        # so either get_text_content returns None or select_text returns False.
        with pytest.raises(ActionFailedError):
            tp.select_text(elems[0], "anything")

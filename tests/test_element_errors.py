"""Tests for element-method error handling — the 3 failure types.

Mirrors ``test_window_management.py``'s contract for elements:

   1. Malformed element_id → ActionFailedError("malformed ...")
   2. Well-formed but not-found → ActionFailedError("not found ...")
   3. Genuine op failure → ActionFailedError(op-specific reason)

Queries follow Python convention rather than ActionFailedError:

   - get_element_by_id / get_text_content:
       malformed → ValueError, not-found → None.

The public ``tp.select_text`` wraps the query so its own failure
contract stays ActionFailedError.  The click/double_click/right_click/
set_value fallback path catches ValueError internally so malformed IDs
surface as ActionFailedError end-to-end (not raw ValueError).

Action and query tests are cross-platform — they construct
platform-appropriate bogus IDs and exercise the public API.  Per-backend
unit tests cover the helper validators directly.
"""

from __future__ import annotations

import sys

import pytest

import touchpoint as tp
from touchpoint.core.exceptions import ActionFailedError
from tests.conftest import (
    skip_unless_linux,
    skip_unless_windows,
    skip_without_backend,
)


# =======================================================================
# Helpers — platform-appropriate bogus IDs
# =======================================================================
#
# A "malformed" ID has the wrong shape (no recognized prefix or non-int
# pid).  A "well-formed but bogus" ID has the right shape but doesn't
# correspond to a real element in the tree.


_MALFORMED_ID = "totally-not-a-valid-element-id"

_BOGUS_ID_BY_PLATFORM = {
    "linux":  "atspi:9999999:99999:0.1.2",
    "darwin": "ax:9999999:zzz:0.1.2",
    "win32":  "uia:9999999:99999.99",
}


def _bogus_well_formed_id() -> str:
    bid = _BOGUS_ID_BY_PLATFORM.get(sys.platform)
    if bid is None:
        pytest.skip(f"no bogus-id template for {sys.platform}")
    return bid


# =======================================================================
# CROSS-PLATFORM ACTION METHOD CONTRACT
# =======================================================================


# Action methods that take only an element_id.
_SIMPLE_OPS = [
    ("focus",                lambda eid: tp.focus(eid)),
    ("set_value",            lambda eid: tp.set_value(eid, "x")),
    ("set_numeric_value",    lambda eid: tp.set_numeric_value(eid, 1.0)),
    ("action",               lambda eid: tp.action(eid, "click")),
    ("select_text",          lambda eid: tp.select_text(eid, "x")),
    ("select_text_range",    lambda eid: tp.select_text_range(eid, 0, 1)),
]

# Click variants share the same fallback path; they're tested below in
# a dedicated class to also assert the fallback doesn't leak ValueError.
_CLICK_OPS = [
    ("click",        lambda eid: tp.click(eid)),
    ("double_click", lambda eid: tp.double_click(eid)),
    ("right_click",  lambda eid: tp.right_click(eid)),
]


@pytest.mark.unit
@skip_without_backend
class TestElementActionErrorHandling:
    """Public-API error contract for element-targeted action methods.

    Verifies that every action method:
      1. Raises ActionFailedError("malformed ...") on garbage IDs.
      2. Raises ActionFailedError("not found ...") on well-formed but
         non-existent IDs.
      3. Never leaks raw ValueError from underlying _parse_id paths.
    """

    @pytest.mark.parametrize("name,op", _SIMPLE_OPS + _CLICK_OPS)
    def test_malformed_element_id_raises_action_failed(self, name, op):
        with pytest.raises(ActionFailedError) as exc_info:
            op(_MALFORMED_ID)
        reason = exc_info.value.reason.lower()
        assert "malformed" in reason, (
            f"{name}: expected 'malformed' in reason, got {reason!r}"
        )

    @pytest.mark.parametrize("name,op", _SIMPLE_OPS + _CLICK_OPS)
    def test_well_formed_but_missing_raises_not_found(self, name, op):
        bogus = _bogus_well_formed_id()
        with pytest.raises(ActionFailedError) as exc_info:
            op(bogus)
        reason = exc_info.value.reason.lower()
        assert "not found" in reason, (
            f"{name}: expected 'not found' in reason, got {reason!r}"
        )


@pytest.mark.unit
@skip_without_backend
class TestClickFallbackPath:
    """Click variants must not leak ValueError when fallback is enabled.

    The click/double_click/right_click/set_value public functions catch
    ActionFailedError and try a coordinate-based InputProvider fallback.
    That path calls get_element_by_id which raises ValueError on
    malformed IDs.  _get_element_position must catch that so click()
    re-raises the original ActionFailedError("malformed ...") rather
    than letting ValueError leak.
    """

    @pytest.mark.parametrize("name,op", _CLICK_OPS)
    def test_malformed_id_with_fallback_enabled_still_raises_action_failed(
        self, name, op,
    ):
        tp.configure(fallback_input=True)
        with pytest.raises(ActionFailedError) as exc_info:
            op(_MALFORMED_ID)
        assert "malformed" in exc_info.value.reason.lower(), (
            f"{name}: leaked through fallback as {exc_info.value.reason!r}"
        )

    @pytest.mark.parametrize("name,op", _CLICK_OPS)
    def test_malformed_id_with_fallback_disabled_still_raises_action_failed(
        self, name, op,
    ):
        tp.configure(fallback_input=False)
        with pytest.raises(ActionFailedError) as exc_info:
            op(_MALFORMED_ID)
        assert "malformed" in exc_info.value.reason.lower()

    def test_set_value_malformed_id_with_fallback_still_raises_action_failed(self):
        tp.configure(fallback_input=True)
        with pytest.raises(ActionFailedError) as exc_info:
            tp.set_value(_MALFORMED_ID, "x")
        assert "malformed" in exc_info.value.reason.lower()


# =======================================================================
# CROSS-PLATFORM QUERY METHOD CONTRACT
# =======================================================================


@pytest.mark.unit
@skip_without_backend
class TestElementQueryErrorHandling:
    """Query methods (get_element / get_text_content) follow Python convention.

    Malformed input → ValueError (analogous to ``int("abc")``).
    Element not found → ``None`` (analogous to ``dict.get(key)``).
    """

    def test_get_element_malformed_raises_value_error(self):
        with pytest.raises(ValueError, match="[Mm]alformed"):
            tp.get_element(_MALFORMED_ID)

    def test_get_element_well_formed_but_missing_returns_none(self):
        assert tp.get_element(_bogus_well_formed_id()) is None

    def test_get_text_content_malformed_raises_value_error(self):
        with pytest.raises(ValueError, match="[Mm]alformed"):
            tp.get_text_content(_MALFORMED_ID)

    def test_get_text_content_well_formed_but_missing_returns_none(self):
        assert tp.get_text_content(_bogus_well_formed_id()) is None


# =======================================================================
# tp.select_text — wraps get_text_content's ValueError into ActionFailedError
# =======================================================================


@pytest.mark.unit
@skip_without_backend
class TestSelectTextWrapsMalformed:
    """tp.select_text's first call is get_text_content (a query).

    If the eid is malformed, get_text_content raises ValueError per the
    query contract.  tp.select_text must catch that and re-raise as
    ActionFailedError so the action-method contract is preserved.
    """

    def test_malformed_id_surfaces_as_action_failed(self):
        with pytest.raises(ActionFailedError) as exc_info:
            tp.select_text(_MALFORMED_ID, "anything")
        reason = exc_info.value.reason.lower()
        assert "malformed" in reason, (
            f"expected 'malformed' in reason, got {reason!r}"
        )

    def test_empty_text_still_validates_first(self):
        """Empty-text validation runs before the get_text_content call."""
        with pytest.raises(ActionFailedError, match="non-empty"):
            tp.select_text(_bogus_well_formed_id(), "")


# =======================================================================
# Internal: _get_element_position must guard ValueError
# =======================================================================


@pytest.mark.unit
@skip_without_backend
class TestGetElementPositionGuard:
    """_get_element_position is the click-fallback bridge.

    It calls get_element_by_id which raises ValueError on malformed IDs.
    The guard catches that and returns None so the fallback path re-
    raises the original ActionFailedError instead of leaking ValueError.
    """

    def test_malformed_id_returns_none(self):
        from touchpoint._state import _get_element_position
        assert _get_element_position(_MALFORMED_ID) is None

    def test_well_formed_missing_returns_none(self):
        from touchpoint._state import _get_element_position
        assert _get_element_position(_bogus_well_formed_id()) is None


# =======================================================================
# PER-BACKEND UNIT TESTS — validator helpers
# =======================================================================


@pytest.mark.unit
@skip_unless_linux
class TestAtspiValidateElementId:
    """``_validate_element_id`` on the Linux AT-SPI backend."""

    def _backend(self):
        from touchpoint.backends.linux import atspi as mod
        return mod.AtSpiBackend.__new__(mod.AtSpiBackend)

    def test_non_atspi_prefix_raises_malformed(self):
        with pytest.raises(ActionFailedError) as exc_info:
            self._backend()._validate_element_id("uia:1:2", "focus")
        assert "malformed element_id" in exc_info.value.reason

    def test_non_int_pid_raises_malformed(self):
        with pytest.raises(ActionFailedError) as exc_info:
            self._backend()._validate_element_id("atspi:abc:2", "focus")
        assert "malformed element_id" in exc_info.value.reason

    def test_non_int_child_segment_raises_malformed(self):
        with pytest.raises(ActionFailedError) as exc_info:
            self._backend()._validate_element_id(
                "atspi:1:2:0.x.2", "focus",
            )
        assert "malformed element_id" in exc_info.value.reason

    def test_three_part_window_id_passes(self):
        """Window-shaped IDs are valid element IDs (window IS an element)."""
        self._backend()._validate_element_id("atspi:1:2", "focus")

    def test_four_part_element_id_passes(self):
        self._backend()._validate_element_id("atspi:1:2:0.1.2", "focus")

    def test_resolve_or_raise_not_found_raises_not_found(self, monkeypatch):
        from touchpoint.backends.linux import atspi as mod
        backend = mod.AtSpiBackend.__new__(mod.AtSpiBackend)
        monkeypatch.setattr(
            backend, "_resolve_element", lambda _eid: None,
        )
        with pytest.raises(ActionFailedError) as exc_info:
            backend._resolve_element_or_raise("atspi:1:2", "focus")
        assert "not found" in exc_info.value.reason.lower()


@pytest.mark.unit
@skip_unless_windows
class TestUiaValidateElementId:
    """``_validate_element_id`` on the Windows UIA backend."""

    def _backend(self):
        from touchpoint.backends.windows.uia import UiaBackend
        return UiaBackend.__new__(UiaBackend)

    def test_non_uia_prefix_raises_malformed(self):
        with pytest.raises(ActionFailedError) as exc_info:
            self._backend()._validate_element_id("atspi:1:2", "focus")
        assert "malformed element_id" in exc_info.value.reason

    def test_non_int_pid_raises_malformed(self):
        with pytest.raises(ActionFailedError) as exc_info:
            self._backend()._validate_element_id("uia:abc:2", "focus")
        assert "malformed element_id" in exc_info.value.reason

    def test_well_formed_passes(self):
        self._backend()._validate_element_id("uia:1:2.3.4", "focus")

    def test_resolve_or_raise_not_found_raises_not_found(self, monkeypatch):
        from touchpoint.backends.windows.uia import UiaBackend
        backend = UiaBackend.__new__(UiaBackend)
        monkeypatch.setattr(
            backend, "_resolve_element", lambda _eid: None,
        )
        with pytest.raises(ActionFailedError) as exc_info:
            backend._resolve_element_or_raise("uia:1:2", "focus")
        assert "not found" in exc_info.value.reason.lower()


@pytest.mark.unit
@pytest.mark.skipif(
    sys.platform != "darwin", reason="requires macOS",
)
class TestAxValidateElementId:
    """``_validate_element_id`` on the macOS AX backend."""

    def _backend(self):
        from touchpoint.backends.macos.ax import AxBackend
        return AxBackend.__new__(AxBackend)

    def test_non_ax_prefix_raises_malformed(self):
        with pytest.raises(ActionFailedError) as exc_info:
            self._backend()._validate_element_id("atspi:1:2", "focus")
        assert "malformed element_id" in exc_info.value.reason

    def test_non_int_pid_raises_malformed(self):
        with pytest.raises(ActionFailedError) as exc_info:
            self._backend()._validate_element_id("ax:abc:tok", "focus")
        assert "malformed element_id" in exc_info.value.reason

    def test_well_formed_passes(self):
        self._backend()._validate_element_id("ax:1:tok:0.1", "focus")

    def test_synthetic_prefixes_pass_validation(self):
        """Synthetic IDs (menubar, popup{N}, hit, etc.) pass the cheap check."""
        backend = self._backend()
        for eid in (
            "ax:1:menubar", "ax:1:menubar:0",
            "ax:1:extras", "ax:1:popup0:1.2",
            "ax:1:app2", "ax:1:hit:abc",
        ):
            backend._validate_element_id(eid, "focus")


@pytest.mark.unit
class TestCdpValidateElementId:
    """``_parse_id_or_raise`` on the CDP backend (no platform gate).

    CDP backend can be instantiated without a live connection because
    we never call _send() — we only exercise the parser.
    """

    def _backend(self):
        from touchpoint.backends.cdp.cdp import CdpBackend
        return CdpBackend.__new__(CdpBackend)

    def test_non_cdp_prefix_raises_malformed(self):
        with pytest.raises(ActionFailedError) as exc_info:
            self._backend()._parse_id_or_raise("uia:1:2", "focus")
        assert "malformed element_id" in exc_info.value.reason

    def test_non_int_port_raises_malformed(self):
        with pytest.raises(ActionFailedError) as exc_info:
            self._backend()._parse_id_or_raise("cdp:abc:target", "focus")
        assert "malformed element_id" in exc_info.value.reason

    def test_well_formed_ax_id_passes(self):
        parts = self._backend()._parse_id_or_raise(
            "cdp:9222:target1:42", "focus",
        )
        assert parts["port"] == 9222
        assert parts["target_id"] == "target1"
        assert parts["node_id"] == "42"

    def test_well_formed_dom_id_passes(self):
        parts = self._backend()._parse_id_or_raise(
            "cdp:9222:target1:dom:100,200", "focus",
        )
        assert parts["port"] == 9222
        assert parts["node_id"] == "dom:100,200"


# =======================================================================
# Shared error builders in base.py
# =======================================================================


@pytest.mark.unit
class TestErrorBuilders:
    """Shared builders keep the wording uniform across backends."""

    def test_malformed_element_includes_format_hint(self):
        from touchpoint.backends.base import make_malformed_element_id_error
        exc = make_malformed_element_id_error(
            "click", "bad-id", "expected-format",
        )
        assert "malformed element_id" in exc.reason
        assert "expected-format" in exc.reason

    def test_element_not_found_distinguishes_from_window(self):
        """Element and window 'not found' errors use different wording."""
        from touchpoint.backends.base import (
            make_element_not_found_error,
            make_window_not_found_error,
        )
        el_exc = make_element_not_found_error("click", "ax:1:2:3")
        win_exc = make_window_not_found_error("activate_window", "ax:1:2")
        assert "element not found" in el_exc.reason
        assert "removed" in el_exc.reason
        assert "window not found" in win_exc.reason
        assert "closed" in win_exc.reason

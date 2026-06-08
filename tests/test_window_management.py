"""Tests for window-management operations.

Covers ``activate_window``, ``minimize_window``, ``fullscreen_window``,
``close_window``, ``move_window``, ``resize_window``.

Integration tests are cross-platform — they go through the public API
and skip only if a missing-dependency ``ActionFailedError`` is raised
(e.g. wmctrl not installed on Linux).  A ``False`` return now indicates
a real operational failure since every platform backend implements all
six window-management methods.  They require a live test window set via
``TOUCHPOINT_TEST_APP``::

    TOUCHPOINT_TEST_APP=Mousepad pytest tests/test_window_management.py -m destructive

Unit tests for the Linux AT-SPI implementation (EWMH helper, fallback
ordering, X11 window-ID mapping) are platform-gated to Linux.

``TestCloseWindowLive`` is opt-in via the ``TOUCHPOINT_TEST_CLOSE=1``
env var because it terminates the test window's process.
"""

from __future__ import annotations

import subprocess
import sys
import time
from unittest import mock

import pytest

import touchpoint as tp
from touchpoint.core.exceptions import ActionFailedError
from touchpoint.core.window import Window
from tests.conftest import (
    skip_unless_linux,
    skip_without_backend,
    skip_without_test_app,
)


# =======================================================================
# CROSS-PLATFORM ERROR HANDLING — 3 failure types
# =======================================================================
#
# Window methods on every backend distinguish:
#   1. malformed window_id  → ActionFailedError("malformed ...")
#   2. well-formed but window not found → ActionFailedError("not found ...")
#   3. genuine op failure → return False  (or raise for hard env errors)
#
# These tests verify (1) and (2) at the public-API layer.  They require
# the backend to be available but no specific app to be open.


@pytest.mark.unit
@skip_without_backend
class TestWindowMgmtErrorHandling:
    """Public-API error contract for window-management methods."""

    _OPS = [
        ("activate_window", lambda wid: tp.activate_window(wid)),
        ("minimize_window", lambda wid: tp.minimize_window(wid)),
        ("close_window",    lambda wid: tp.close_window(wid)),
        ("fullscreen_window", lambda wid: tp.fullscreen_window(wid)),
        ("move_window",     lambda wid: tp.move_window(wid, 100, 100)),
        ("resize_window",   lambda wid: tp.resize_window(wid, 800, 600)),
    ]

    @pytest.mark.parametrize("name,op", _OPS)
    def test_malformed_window_id_raises_action_failed(self, name, op):
        """Garbage IDs raise ActionFailedError with 'malformed' in reason."""
        with pytest.raises(ActionFailedError) as exc_info:
            op("totally-not-a-valid-window-id")
        assert "malformed" in exc_info.value.reason.lower(), (
            f"{name}: expected 'malformed' in reason, got {exc_info.value.reason!r}"
        )

    @pytest.mark.parametrize("name,op", _OPS)
    def test_well_formed_but_missing_window_raises_not_found(self, name, op):
        """Well-formed but non-existent window IDs raise 'not found'."""
        # Construct a plausibly-formed but bogus id per backend
        import sys
        if sys.platform == "linux":
            bogus = "atspi:9999999:99999"
        elif sys.platform == "darwin":
            bogus = "ax:9999999:zzz"
        elif sys.platform == "win32":
            bogus = "uia:9999999:99999"
        else:
            pytest.skip(f"no bogus-id template for {sys.platform}")
        with pytest.raises(ActionFailedError) as exc_info:
            op(bogus)
        assert "not found" in exc_info.value.reason.lower(), (
            f"{name}: expected 'not found' in reason, got {exc_info.value.reason!r}"
        )

    def test_resize_window_rejects_non_positive_dimensions(self):
        """resize_window raises on width/height <= 0."""
        # We need any well-formed id; the validation happens before lookup
        # on macOS/Windows but after on Linux — handle both by using a
        # bogus-but-well-formed id and accepting either "must be positive"
        # (validation fired) or "not found" (lookup fired first).
        import sys
        bogus = {"linux": "atspi:1:1", "darwin": "ax:1:1",
                 "win32": "uia:1:1"}.get(sys.platform)
        if bogus is None:
            pytest.skip(f"no template for {sys.platform}")
        with pytest.raises(ActionFailedError) as exc_info:
            tp.resize_window(bogus, -1, -1)
        # Either the size guard or the not-found check fires — both are
        # acceptable (different platforms order the checks differently).
        reason = exc_info.value.reason.lower()
        assert "positive" in reason or "not found" in reason, (
            f"expected 'positive' or 'not found' in reason, got {reason!r}"
        )


@pytest.mark.unit
class TestCdpWindowMgmtRouting:
    """OS-frame operations on a CDP window route to its native OS window."""

    # (name, op, native-backend method, expected positional args after the id)
    _OPS = [
        ("minimize_window", lambda wid: tp.minimize_window(wid),
         "minimize_window", ()),
        ("close_window", lambda wid: tp.close_window(wid),
         "close_window", ()),
        ("move_window", lambda wid: tp.move_window(wid, 100, 100),
         "move_window", (100, 100)),
        ("resize_window", lambda wid: tp.resize_window(wid, 800, 600),
         "resize_window", (800, 600)),
    ]

    @pytest.mark.parametrize("name,op,method,args", _OPS)
    def test_routes_to_native_window(self, name, op, method, args, monkeypatch):
        monkeypatch.setattr(tp, "_native_window_for_cdp", lambda _wid: "atspi:42:1")
        backend = mock.MagicMock()
        getattr(backend, method).return_value = True
        monkeypatch.setattr(tp, "_get_backend", lambda: backend)

        assert op("cdp:9222:target") is True
        getattr(backend, method).assert_called_once_with("atspi:42:1", *args)

    def test_fullscreen_routes_to_native_window(self, monkeypatch):
        monkeypatch.setattr(tp, "_native_window_for_cdp", lambda _wid: "atspi:42:1")
        backend = mock.MagicMock()
        backend.fullscreen_window.return_value = True
        monkeypatch.setattr(tp, "_get_backend", lambda: backend)

        assert tp.fullscreen_window("cdp:9222:target") is True
        backend.fullscreen_window.assert_called_once_with(
            "atspi:42:1", fullscreen=True,
        )

    @pytest.mark.parametrize("name,op,method,args", _OPS)
    def test_raises_when_no_native_window(self, name, op, method, args, monkeypatch):
        monkeypatch.setattr(tp, "_native_window_for_cdp", lambda _wid: None)
        with pytest.raises(ActionFailedError) as exc_info:
            op("cdp:9222:target")
        assert "no native OS window" in exc_info.value.reason
        assert exc_info.value.action == name


@pytest.mark.unit
class TestNativeWindowForCdp:
    """Resolving a CDP page-target to its native OS window by owning PID."""

    CDP_ID = "cdp:9222:abc"

    @staticmethod
    def _win(wid, *, pid, title="", active=False, size=(100, 100)):
        return Window(
            id=wid, title=title, app="Chrome", pid=pid,
            position=(0, 0), size=size, is_active=active, is_visible=True,
        )

    def _patch(self, monkeypatch, *, target, native):
        from touchpoint import _state
        cdp = mock.MagicMock()
        cdp.get_windows.return_value = [target] if target else []
        backend = mock.MagicMock()
        backend.get_windows.return_value = native
        monkeypatch.setattr(_state, "_get_cdp", lambda: cdp)
        monkeypatch.setattr(_state, "_get_backend", lambda: backend)

    def test_single_native_window(self, monkeypatch):
        self._patch(
            monkeypatch,
            target=self._win(self.CDP_ID, pid=5, title="Page"),
            native=[self._win("atspi:5:1", pid=5, title="Page - Chrome")],
        )
        assert tp._native_window_for_cdp(self.CDP_ID) == "atspi:5:1"

    def test_disambiguates_by_title_case_insensitive(self, monkeypatch):
        self._patch(
            monkeypatch,
            target=self._win(self.CDP_ID, pid=5, title="My DOC"),
            native=[
                self._win("atspi:5:1", pid=5, title="Other - Chrome"),
                self._win("atspi:5:2", pid=5, title="my doc - Chrome"),
            ],
        )
        assert tp._native_window_for_cdp(self.CDP_ID) == "atspi:5:2"

    def test_falls_back_to_active_then_largest(self, monkeypatch):
        # No title match → active wins.
        self._patch(
            monkeypatch,
            target=self._win(self.CDP_ID, pid=5, title="nomatch"),
            native=[
                self._win("atspi:5:1", pid=5, title="A - Chrome", size=(900, 900)),
                self._win("atspi:5:2", pid=5, title="B - Chrome", active=True),
            ],
        )
        assert tp._native_window_for_cdp(self.CDP_ID) == "atspi:5:2"

        # No title match, none active → largest by area.
        self._patch(
            monkeypatch,
            target=self._win(self.CDP_ID, pid=5, title="nomatch"),
            native=[
                self._win("atspi:5:1", pid=5, title="A", size=(100, 100)),
                self._win("atspi:5:2", pid=5, title="B", size=(900, 900)),
            ],
        )
        assert tp._native_window_for_cdp(self.CDP_ID) == "atspi:5:2"

    def test_none_when_no_native_window_for_pid(self, monkeypatch):
        self._patch(
            monkeypatch,
            target=self._win(self.CDP_ID, pid=5),
            native=[self._win("atspi:99:1", pid=99)],
        )
        assert tp._native_window_for_cdp(self.CDP_ID) is None

    def test_none_when_target_is_stale(self, monkeypatch):
        self._patch(
            monkeypatch, target=None,
            native=[self._win("atspi:5:1", pid=5)],
        )
        assert tp._native_window_for_cdp(self.CDP_ID) is None


@pytest.mark.unit
class TestMacosWindowIdStability:
    """macOS AX window-id fallback behaviour."""

    def test_fallback_token_ignores_mutable_attributes(self, monkeypatch):
        """Mutable geometry/subrole changes must not stale fallback ids."""
        from touchpoint.backends.macos import ax as mod

        fake_window = object()
        attrs = {
            "AXWindowNumber": None,
            "AXIdentifier": None,
            "AXTitle": "Untitled",
        }

        def fake_get_ax_attr(_element, name, default=None):
            if name == "AXSubrole":
                pytest.fail("fallback window token must not read mutable subrole")
            return attrs.get(name, default)

        monkeypatch.setattr(mod, "_get_ax_attr", fake_get_ax_attr)
        monkeypatch.setattr(
            mod,
            "_ax_position",
            lambda _element: pytest.fail(
                "fallback window token must not read mutable position"
            ),
        )
        monkeypatch.setattr(
            mod,
            "_ax_size",
            lambda _element: pytest.fail(
                "fallback window token must not read mutable size"
            ),
        )

        before = mod.AxBackend._window_token(fake_window, fallback_index=0)
        attrs["AXSubrole"] = "AXDialog"
        attrs["AXTitle"] = "Saved Document"
        after = mod.AxBackend._window_token(fake_window, fallback_index=0)
        sibling = mod.AxBackend._window_token(fake_window, fallback_index=1)

        assert before == after
        assert before != sibling

    def test_cached_window_reference_keeps_old_token_resolvable(
        self, monkeypatch,
    ):
        """A known AX window token should keep resolving after AX drift."""
        from touchpoint.backends.macos import ax as mod

        backend = mod.AxBackend.__new__(mod.AxBackend)
        fake_window = object()
        backend._window_refs = {(123, "fold"): fake_window}
        backend._messaging_timeout = 1.0
        backend._timeout_apply_failures = set()

        monkeypatch.setattr(
            mod,
            "_get_ax_attr",
            lambda _element, name, default=None: (
                "AXWindow" if name == "AXRole" else default
            ),
        )

        assert backend._get_window_element(123, "fold") is fake_window

    def test_duplicate_tokens_are_disambiguated(self):
        """macOS AXIdentifier can repeat across sibling windows."""
        from touchpoint.backends.macos import ax as mod

        seen_tokens: dict[str, int] = {}

        assert mod.AxBackend._unique_window_token("iabc", seen_tokens) == "iabc"
        assert mod.AxBackend._unique_window_token("iabc", seen_tokens) == "iabc.1"
        assert mod.AxBackend._unique_window_token("f123", seen_tokens) == "f123"


@pytest.mark.unit
class TestMacosCloseWindowTimeout:
    def test_close_button_receives_messaging_timeout(self, monkeypatch):
        from touchpoint.backends.macos import ax as mod

        backend = mod.AxBackend.__new__(mod.AxBackend)
        window = object()
        close_button = object()
        prepared = []
        backend._resolve_ax_window_or_raise = lambda _wid, _action: window
        backend._prepare_ax_element = (
            lambda element, pid: prepared.append((element, pid)) or element
        )
        monkeypatch.setattr(
            mod,
            "_get_ax_attr",
            lambda _element, attr: (
                close_button if attr == "AXCloseButton" else None
            ),
        )
        monkeypatch.setattr(mod, "_perform_ax_action", lambda *_args: True)

        assert backend.close_window("ax:123:fold") is True
        assert prepared == [(close_button, 123)]


# =======================================================================
# CROSS-PLATFORM INTEGRATION TESTS — public-API contract
# =======================================================================
#
# These exercise the real backend on whatever platform is hosting the
# test run.  Each test skips if a missing dependency is reported via
# ActionFailedError (e.g. wmctrl not installed); a False return is
# treated as a real failure, since every platform backend now
# implements all six window-management methods.


def _find_test_window(app_name: str) -> Window | None:
    """Find a visible window for *app_name* (case-insensitive)."""
    app_lower = app_name.lower()
    for w in tp.windows():
        if w.app.lower() == app_lower and w.is_visible:
            return w
    return None


@pytest.mark.destructive
@pytest.mark.integration
@skip_without_backend
@skip_without_test_app
class TestWindowMgmtLive:
    """End-to-end window-mgmt tests against a real window.

    Cross-platform: runs against macOS AX, Windows UIA, Linux AT-SPI,
    whichever is the active backend.  CDP-only windows skip these
    because CDP doesn't implement window mgmt.
    """

    def test_minimize_then_activate_restores(self, destructive_app):
        target = _find_test_window(destructive_app)
        if target is None:
            pytest.skip(f"no visible window for {destructive_app}")
        try:
            ok = tp.minimize_window(target)
        except ActionFailedError as exc:
            pytest.skip(f"minimize_window unavailable: {exc}")
        assert ok, "minimize_window returned False (real op failure)"
        time.sleep(0.3)
        try:
            ok = tp.activate_window(target)
        except ActionFailedError as exc:
            pytest.fail(f"activate after minimize raised: {exc}")
        assert ok is True
        time.sleep(0.3)

    def test_move_window_changes_position(self, destructive_app):
        target = _find_test_window(destructive_app)
        if target is None:
            pytest.skip(f"no visible window for {destructive_app}")

        # First move to a starting position so we have a non-default baseline.
        try:
            ok = tp.move_window(target, 50, 50)
        except ActionFailedError as exc:
            pytest.skip(f"move_window unavailable: {exc}")
        assert ok, "move_window returned False (real op failure)"
        time.sleep(0.3)
        baseline = _find_test_window(destructive_app)
        assert baseline is not None
        bx, by = baseline.position

        # Now move to a distinctly different position.
        target_x, target_y = bx + 300, by + 200
        assert tp.move_window(target, target_x, target_y) is True
        time.sleep(0.3)
        moved = _find_test_window(destructive_app)
        assert moved is not None
        # The position should have changed substantially in both axes —
        # tolerance covers WM gravity adjustments + frame-extent offsets.
        dx = abs(moved.position[0] - target_x)
        dy = abs(moved.position[1] - target_y)
        assert dx <= 80 and dy <= 80, (
            f"expected position near ({target_x}, {target_y}), "
            f"got {moved.position}"
        )

    def test_resize_window_changes_size(self, destructive_app):
        target = _find_test_window(destructive_app)
        if target is None:
            pytest.skip(f"no visible window for {destructive_app}")

        baseline = _find_test_window(destructive_app)
        assert baseline is not None
        bw, bh = baseline.size

        # Resize to a distinctly different (larger) size — small targets
        # may be rejected by WM_NORMAL_HINTS min-size on some apps.
        new_w, new_h = max(bw + 200, 1200), max(bh + 200, 800)
        try:
            ok = tp.resize_window(target, new_w, new_h)
        except ActionFailedError as exc:
            pytest.skip(f"resize_window unavailable: {exc}")
        assert ok, "resize_window returned False (real op failure)"
        time.sleep(0.3)
        resized = _find_test_window(destructive_app)
        assert resized is not None
        # Verify the size CHANGED in the direction we asked (loose check
        # because AT-SPI-reported size includes decorations differently
        # from what we requested via EWMH; some WMs/apps snap to size
        # hints).
        rw, rh = resized.size
        assert rw != bw or rh != bh, (
            f"size did not change at all: before={baseline.size}, "
            f"after={resized.size}, requested=({new_w}, {new_h})"
        )

    def test_fullscreen_round_trip(self, destructive_app):
        target = _find_test_window(destructive_app)
        if target is None:
            pytest.skip(f"no visible window for {destructive_app}")
        try:
            ok = tp.fullscreen_window(target, fullscreen=True)
        except ActionFailedError as exc:
            pytest.skip(f"fullscreen_window unavailable: {exc}")
        assert ok, "fullscreen_window returned False (real op failure)"
        time.sleep(0.4)
        ok = tp.fullscreen_window(target, fullscreen=False)
        assert ok is True
        time.sleep(0.3)


@pytest.mark.destructive
@pytest.mark.integration
@skip_without_backend
@skip_without_test_app
class TestCloseWindowLive:
    """Closes the test window — opt-in via ``TOUCHPOINT_TEST_CLOSE=1``.

    Cross-platform.  Skipped by default because the test app's process
    must be relaunched before other destructive tests can run.
    """

    def test_close_window(self, destructive_app):
        import os
        if os.environ.get("TOUCHPOINT_TEST_CLOSE") != "1":
            pytest.skip(
                "set TOUCHPOINT_TEST_CLOSE=1 to run "
                "(closes the test window)"
            )
        target = _find_test_window(destructive_app)
        if target is None:
            pytest.skip(f"no visible window for {destructive_app}")
        try:
            ok = tp.close_window(target)
        except ActionFailedError as exc:
            pytest.skip(f"close_window unavailable: {exc}")
        assert ok, "close_window returned False (real op failure)"
        time.sleep(0.5)
        assert _find_test_window(destructive_app) is None, (
            "window still present after close"
        )


# =======================================================================
# LINUX AT-SPI BACKEND UNIT TESTS — implementation details
# =======================================================================


@pytest.mark.unit
@skip_unless_linux
class TestRunWindowTool:
    """``_run_window_tool`` — subprocess wrapper for window-mgmt CLIs."""

    def test_exit_zero_returns_true(self):
        from touchpoint.backends.linux.atspi import _run_window_tool
        with mock.patch("subprocess.run") as run:
            run.return_value.returncode = 0
            assert _run_window_tool(["echo", "hi"]) is True

    def test_exit_nonzero_returns_false(self):
        from touchpoint.backends.linux.atspi import _run_window_tool
        with mock.patch("subprocess.run") as run:
            run.return_value.returncode = 1
            assert _run_window_tool(["false"]) is False

    def test_timeout_returns_false(self):
        from touchpoint.backends.linux.atspi import _run_window_tool
        with mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="x", timeout=1),
        ):
            assert _run_window_tool(["sleep", "10"], timeout=0.1) is False

    def test_missing_binary_returns_false(self):
        from touchpoint.backends.linux.atspi import _run_window_tool
        with mock.patch(
            "subprocess.run", side_effect=FileNotFoundError(),
        ):
            assert _run_window_tool(["nonexistent-binary"]) is False


@pytest.mark.unit
@skip_unless_linux
class TestParseIdGtk4Uuid:
    """``_parse_id`` accepts GTK4 UUID-style D-Bus path suffixes.

    GTK4 apps (e.g. gnome-calculator) expose accessibles under paths like
    ``/org/<app>/a11y/135d3278_0e7f_4d4a_...`` instead of the integer
    ``/org/a11y/atspi/accessible/<N>`` suffix used by GTK3/Qt. The path
    suffix is string-matched during resolution, so it must pass
    validation even when non-numeric. Only the PID and child-path
    indices must be integers.
    """

    def _backend(self):
        from touchpoint.backends.linux.atspi import AtSpiBackend
        return AtSpiBackend.__new__(AtSpiBackend)

    def test_uuid_window_suffix_accepted(self):
        wid = "atspi:954802:135d3278_0e7f_4d4a_973f_627904a9ed4f"
        assert self._backend()._parse_id(wid) == [
            "atspi", "954802", "135d3278_0e7f_4d4a_973f_627904a9ed4f",
        ]

    def test_uuid_suffix_with_child_path_accepted(self):
        eid = "atspi:954802:135d3278_0e7f_4d4a_973f_627904a9ed4f:0.1"
        assert self._backend()._parse_id(eid)[3] == "0.1"

    def test_non_numeric_pid_still_rejected(self):
        with pytest.raises(ValueError, match="[Mm]alformed"):
            self._backend()._parse_id("atspi:notapid:1")

    def test_non_numeric_child_index_still_rejected(self):
        with pytest.raises(ValueError, match="[Mm]alformed"):
            self._backend()._parse_id("atspi:123:abc_def:0.x.2")


@pytest.mark.unit
@skip_unless_linux
class TestCheckWmctrlOrRaise:
    """``_check_wmctrl_or_raise`` — environment preconditions.

    wmctrl is the mandatory floor for every window-mgmt op (used for
    AT-SPI → X11 id mapping).  This helper raises on Wayland or when
    wmctrl is missing.  Per-op extra tool requirements (e.g. xdotool
    for ``minimize_window``) layer on top in each method.
    """

    def test_wayland_raises(self):
        from touchpoint.backends.linux import atspi as mod
        with mock.patch.object(mod, "_IS_WAYLAND", True):
            with pytest.raises(ActionFailedError, match="Wayland"):
                mod._check_wmctrl_or_raise("activate_window", "atspi:1:2")

    def test_no_wmctrl_raises(self):
        from touchpoint.backends.linux import atspi as mod
        with (
            mock.patch.object(mod, "_IS_WAYLAND", False),
            mock.patch.object(mod, "_HAS_WMCTRL", False),
        ):
            with pytest.raises(ActionFailedError, match="wmctrl is required"):
                mod._check_wmctrl_or_raise("activate_window", "atspi:1:2")

    def test_wmctrl_present_ok(self):
        """No xdotool is fine — wmctrl alone satisfies the floor."""
        from touchpoint.backends.linux import atspi as mod
        with (
            mock.patch.object(mod, "_IS_WAYLAND", False),
            mock.patch.object(mod, "_HAS_WMCTRL", True),
            mock.patch.object(mod, "_HAS_XDOTOOL", False),
        ):
            mod._check_wmctrl_or_raise("x", "atspi:1:2")  # does not raise

    def test_only_xdotool_raises(self):
        """xdotool alone is NOT enough — wmctrl is mandatory."""
        from touchpoint.backends.linux import atspi as mod
        with (
            mock.patch.object(mod, "_IS_WAYLAND", False),
            mock.patch.object(mod, "_HAS_WMCTRL", False),
            mock.patch.object(mod, "_HAS_XDOTOOL", True),
        ):
            with pytest.raises(ActionFailedError, match="wmctrl is required"):
                mod._check_wmctrl_or_raise("x", "atspi:1:2")


@pytest.mark.unit
@skip_unless_linux
class TestValidateWindowIdOrRaise:
    """``_validate_window_id_or_raise`` enforces AT-SPI ID format."""

    def test_non_atspi_prefix_raises_malformed(self):
        from touchpoint.backends.linux import atspi as mod
        backend = mod.AtSpiBackend.__new__(mod.AtSpiBackend)
        with pytest.raises(ActionFailedError) as exc_info:
            backend._validate_window_id_or_raise(
                "uia:123:456", "activate_window",
            )
        assert (
            exc_info.value.reason
            == "malformed window_id (expected 'atspi:<pid>:<token>')"
        )

    def test_element_id_rejected_as_window_id(self):
        """4-part element IDs are rejected — only 3-part window IDs accepted."""
        from touchpoint.backends.linux import atspi as mod
        backend = mod.AtSpiBackend.__new__(mod.AtSpiBackend)
        with pytest.raises(ActionFailedError, match="malformed"):
            backend._validate_window_id_or_raise(
                "atspi:123:456:0.1.2", "activate_window",
            )


@pytest.mark.unit
@skip_unless_linux
class TestFullscreenWindowRequiresWmctrl:
    """``fullscreen_window`` raises a specific 'wmctrl required' error."""

    def test_wmctrl_missing_raises(self):
        from touchpoint.backends.linux import atspi as mod
        backend = mod.AtSpiBackend.__new__(mod.AtSpiBackend)
        with (
            mock.patch.object(mod, "_IS_WAYLAND", False),
            mock.patch.object(mod, "_HAS_WMCTRL", False),
        ):
            with pytest.raises(ActionFailedError, match="wmctrl is required"):
                backend.fullscreen_window("atspi:1:2")


@pytest.mark.unit
@skip_unless_linux
class TestAtspiToX11WindowId:
    """``_atspi_to_x11_window_id`` — wmctrl output parsing + matching."""

    def _make_backend(self, find_result=None):
        from touchpoint.backends.linux import atspi as mod
        backend = mod.AtSpiBackend.__new__(mod.AtSpiBackend)
        backend._find_window_accessible = mock.MagicMock(  # type: ignore[attr-defined]
            return_value=find_result,
        )
        return backend

    def _make_run(self, stdout: str, returncode: int = 0):
        m = mock.MagicMock()
        m.returncode = returncode
        m.stdout = stdout.encode()
        return m

    def test_no_wmctrl_returns_none(self):
        from touchpoint.backends.linux import atspi as mod
        backend = self._make_backend()
        with mock.patch.object(mod, "_HAS_WMCTRL", False):
            assert backend._atspi_to_x11_window_id("atspi:235580:162") is None

    def test_single_match_returns_hex_id(self):
        from touchpoint.backends.linux import atspi as mod
        stdout = (
            "0x06e00007  0 235580 9    69   640  480  host title-here\n"
        )
        backend = self._make_backend()
        with (
            mock.patch.object(mod, "_HAS_WMCTRL", True),
            mock.patch(
                "subprocess.run",
                return_value=self._make_run(stdout),
            ),
        ):
            assert (
                backend._atspi_to_x11_window_id("atspi:235580:162")
                == "0x06e00007"
            )

    def test_pid_mismatch_returns_none(self):
        from touchpoint.backends.linux import atspi as mod
        stdout = (
            "0x06e00007  0 99999 9    69   640  480  host title-here\n"
        )
        backend = self._make_backend()
        with (
            mock.patch.object(mod, "_HAS_WMCTRL", True),
            mock.patch(
                "subprocess.run",
                return_value=self._make_run(stdout),
            ),
        ):
            assert (
                backend._atspi_to_x11_window_id("atspi:235580:162") is None
            )

    def test_multiple_disambiguates_by_title(self):
        from touchpoint.backends.linux import atspi as mod
        stdout = (
            "0x06e00007  0 235580 0    0    100  100  host first window\n"
            "0x06e00008  0 235580 0    0    100  100  host second window\n"
        )
        accessible = mock.MagicMock()
        accessible.get_name.return_value = "second window"
        backend = self._make_backend(find_result=(accessible, None))
        with (
            mock.patch.object(mod, "_HAS_WMCTRL", True),
            mock.patch(
                "subprocess.run",
                return_value=self._make_run(stdout),
            ),
        ):
            assert (
                backend._atspi_to_x11_window_id("atspi:235580:162")
                == "0x06e00008"
            )

    def test_multiple_disambiguates_by_substring_match(self):
        """Multi-window disambiguation tolerates title asymmetries.

        AT-SPI may report a shorter title (e.g. 'Untitled') than wmctrl
        which adds the app name (e.g. 'Untitled - Mousepad').  The
        substring match handles both directions.
        """
        from touchpoint.backends.linux import atspi as mod
        stdout = (
            "0x06e00007  0 235580 0    0    100  100  host Untitled - Mousepad\n"
            "0x06e00008  0 235580 0    0    100  100  host README - Mousepad\n"
        )
        accessible = mock.MagicMock()
        # AT-SPI reports just "Untitled"; wmctrl has "Untitled - Mousepad"
        accessible.get_name.return_value = "Untitled"
        backend = self._make_backend(find_result=(accessible, None))
        with (
            mock.patch.object(mod, "_HAS_WMCTRL", True),
            mock.patch(
                "subprocess.run",
                return_value=self._make_run(stdout),
            ),
        ):
            assert (
                backend._atspi_to_x11_window_id("atspi:235580:162")
                == "0x06e00007"
            )

    def test_multiple_no_title_match_returns_none(self):
        from touchpoint.backends.linux import atspi as mod
        stdout = (
            "0x06e00007  0 235580 0    0    100  100  host one\n"
            "0x06e00008  0 235580 0    0    100  100  host two\n"
        )
        backend = self._make_backend(find_result=None)
        with (
            mock.patch.object(mod, "_HAS_WMCTRL", True),
            mock.patch(
                "subprocess.run",
                return_value=self._make_run(stdout),
            ),
        ):
            assert (
                backend._atspi_to_x11_window_id("atspi:235580:162") is None
            )


@pytest.mark.unit
@skip_unless_linux
class TestAtspiFallbackOrdering:
    """Each AT-SPI window op tries the right tool first."""

    def _setup(self):
        from touchpoint.backends.linux import atspi as mod
        backend = mod.AtSpiBackend.__new__(mod.AtSpiBackend)
        backend._atspi_to_x11_window_id = mock.MagicMock(  # type: ignore[attr-defined]
            return_value="0xdead",
        )
        backend._parse_id = mock.MagicMock(  # type: ignore[attr-defined]
            return_value=["atspi", "1", "2"],
        )
        backend._find_window_accessible = mock.MagicMock(  # type: ignore[attr-defined]
            return_value=None,  # skip AT-SPI Component path
        )
        return mod, backend

    def test_activate_uses_wmctrl_first(self):
        mod, backend = self._setup()
        runs: list[list[str]] = []
        with (
            mock.patch.object(mod, "_IS_WAYLAND", False),
            mock.patch.object(mod, "_HAS_WMCTRL", True),
            mock.patch.object(mod, "_HAS_XDOTOOL", True),
            mock.patch.object(
                mod,
                "_run_window_tool",
                side_effect=lambda cmd, **kw: (runs.append(cmd), True)[1],
            ),
        ):
            backend.activate_window("atspi:1:2")
        assert runs[0][0] == "wmctrl"

    def test_minimize_uses_xdotool_only(self):
        """Minimize uses xdotool exclusively (wmctrl has no real path)."""
        mod, backend = self._setup()
        runs: list[list[str]] = []
        with (
            mock.patch.object(mod, "_IS_WAYLAND", False),
            mock.patch.object(mod, "_HAS_WMCTRL", True),
            mock.patch.object(mod, "_HAS_XDOTOOL", True),
            mock.patch.object(
                mod,
                "_run_window_tool",
                side_effect=lambda cmd, **kw: (runs.append(cmd), True)[1],
            ),
        ):
            backend.minimize_window("atspi:1:2")
        assert len(runs) == 1
        assert runs[0][0] == "xdotool"
        assert runs[0][1] == "windowminimize"

    def test_minimize_raises_when_xdotool_missing(self):
        """Minimize requires xdotool specifically — wmctrl alone is insufficient."""
        mod, backend = self._setup()
        with (
            mock.patch.object(mod, "_IS_WAYLAND", False),
            mock.patch.object(mod, "_HAS_WMCTRL", True),
            mock.patch.object(mod, "_HAS_XDOTOOL", False),
        ):
            with pytest.raises(ActionFailedError, match="xdotool is required"):
                backend.minimize_window("atspi:1:2")

    def test_xdotool_runs_when_wmctrl_fails(self):
        mod, backend = self._setup()
        runs: list[list[str]] = []

        def fake_run(cmd, **kw):
            runs.append(cmd)
            return cmd[0] != "wmctrl"  # only xdotool succeeds

        with (
            mock.patch.object(mod, "_IS_WAYLAND", False),
            mock.patch.object(mod, "_HAS_WMCTRL", True),
            mock.patch.object(mod, "_HAS_XDOTOOL", True),
            mock.patch.object(mod, "_run_window_tool", side_effect=fake_run),
        ):
            assert backend.activate_window("atspi:1:2") is True
        assert [c[0] for c in runs] == ["wmctrl", "xdotool"]

    def test_move_uses_wmctrl_first(self):
        """move_window uses wmctrl as primary (AT-SPI is unreliable for top-levels)."""
        mod, backend = self._setup()
        runs: list[list[str]] = []
        with (
            mock.patch.object(mod, "_IS_WAYLAND", False),
            mock.patch.object(mod, "_HAS_WMCTRL", True),
            mock.patch.object(mod, "_HAS_XDOTOOL", True),
            mock.patch.object(
                mod,
                "_run_window_tool",
                side_effect=lambda cmd, **kw: (runs.append(cmd), True)[1],
            ),
        ):
            backend.move_window("atspi:1:2", 50, 50)
        assert runs[0][0] == "wmctrl"

    def test_activate_falls_back_to_grab_focus(self):
        """activate_window last-resort tries AT-SPI grab_focus."""
        mod, backend = self._setup()
        # All EWMH attempts fail
        accessible = mock.MagicMock()
        comp = mock.MagicMock()
        comp.grab_focus.return_value = True
        accessible.get_component_iface.return_value = comp
        backend._find_window_accessible = mock.MagicMock(  # type: ignore[attr-defined]
            return_value=(accessible, None),
        )
        with (
            mock.patch.object(mod, "_IS_WAYLAND", False),
            mock.patch.object(mod, "_HAS_WMCTRL", True),
            mock.patch.object(mod, "_HAS_XDOTOOL", True),
            mock.patch.object(mod, "_run_window_tool", return_value=False),
        ):
            assert backend.activate_window("atspi:1:2") is True
        comp.grab_focus.assert_called_once()

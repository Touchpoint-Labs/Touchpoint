"""Tests for screenshot functionality.

Covers tp.screenshot(), tp.monitor_count(), the internal
_find_window() helper, and the low-level utilities
take_screenshot() / get_monitor_regions().

Desktop and monitor tests require a running display server and
Pillow.  App/window/element tests also need a backend (any visible
app will do — screenshots are read-only)::

    pytest tests/test_screenshot.py -v
"""

from __future__ import annotations

import pytest

import touchpoint as tp
from touchpoint.utils.screenshot import get_monitor_regions, take_screenshot
from tests.conftest import (
    skip_without_backend,
)


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------

def _close(img) -> None:
    """Close an image, ignoring errors."""
    try:
        img.close()
    except Exception:
        pass


def _app_window(app: str):
    """Return a visible window for *app*, or None."""
    app_lower = app.lower()
    for w in tp.windows():
        if w.app.lower() == app_lower and w.is_visible:
            return w
    return None


def _app_element(app: str):
    """Return a visible, sized element from *app*, or None."""
    from touchpoint.core.types import State

    elems = tp.elements(
        app=app, named_only=True,
        states=[State.VISIBLE, State.SHOWING],
    )
    for el in elems:
        if el.size[0] > 0 and el.size[1] > 0:
            return el
    return None


# -----------------------------------------------------------------------
# Full desktop screenshots (no backend needed, just Pillow + display)
# -----------------------------------------------------------------------

@pytest.mark.integration
class TestScreenshotFullDesktop:
    """tp.screenshot() with no crop arguments."""

    def test_full_desktop_returns_image(self):
        """No arguments → full virtual desktop image."""
        img = tp.screenshot()
        try:
            assert img.mode == "RGB"
            w, h = img.size
            assert w > 0 and h > 0
        finally:
            _close(img)

    def test_monitor_zero(self):
        """monitor=0 returns an image matching the primary monitor region."""
        regions = get_monitor_regions()
        img = tp.screenshot(monitor=0)
        try:
            left, top, right, bottom = regions[0]
            expected_w = right - left
            expected_h = bottom - top
            w, h = img.size
            # Allow 1px tolerance for rounding.
            assert abs(w - expected_w) <= 1
            assert abs(h - expected_h) <= 1
        finally:
            _close(img)


# -----------------------------------------------------------------------
# App-scoped screenshots (backend + test app)
# -----------------------------------------------------------------------

@pytest.mark.integration
@skip_without_backend
class TestScreenshotApp:
    """tp.screenshot(app=...) tests."""

    def test_by_app(self, any_app):
        """Crop to the test app's window."""
        img = tp.screenshot(app=any_app)
        try:
            w, h = img.size
            assert w > 0 and h > 0
        finally:
            _close(img)

    def test_by_app_case_insensitive(self, any_app):
        """App name matching is case-insensitive."""
        img1 = tp.screenshot(app=any_app)
        img2 = tp.screenshot(app=any_app.upper())
        try:
            assert img1.size == img2.size
        finally:
            _close(img1)
            _close(img2)


# -----------------------------------------------------------------------
# Window / element screenshots (backend + test app)
# -----------------------------------------------------------------------

@pytest.mark.integration
@skip_without_backend
class TestScreenshotWindow:
    """tp.screenshot(window_id=...) and tp.screenshot(element=...) tests."""

    def test_by_window_id(self, backend, any_app):
        """Crop to a specific window by id."""
        win = _app_window(any_app)
        if win is None:
            pytest.skip(f"no visible window for {any_app}")
        img = tp.screenshot(window_id=win.id)
        try:
            w, h = img.size
            ww, wh = win.size
            # Image dimensions should be close to window size.
            assert abs(w - ww) <= 2
            assert abs(h - wh) <= 2
        finally:
            _close(img)

    def test_by_element(self, backend, any_app):
        """Crop to an element — accepts both Element and id string."""
        el = _app_element(any_app)
        if el is None:
            pytest.skip(f"no sized element in {any_app}")
        img_obj = tp.screenshot(element=el)
        img_str = tp.screenshot(element=el.id)
        try:
            # Both should produce the same size.
            assert img_obj.size == img_str.size
            ew, eh = el.size
            w, h = img_obj.size
            assert abs(w - ew) <= 2
            assert abs(h - eh) <= 2
        finally:
            _close(img_obj)
            _close(img_str)


# -----------------------------------------------------------------------
# Padding
# -----------------------------------------------------------------------

@pytest.mark.integration
@skip_without_backend
class TestScreenshotPadding:
    """tp.screenshot(padding=...) tests."""

    def test_padding_enlarges_region(self, backend, any_app):
        """padding=20 produces a larger image than padding=0."""
        el = _app_element(any_app)
        if el is None:
            pytest.skip(f"no sized element in {any_app}")
        img_no_pad = tp.screenshot(element=el, padding=0)
        img_padded = tp.screenshot(element=el, padding=20)
        try:
            w0, h0 = img_no_pad.size
            wp, hp = img_padded.size
            assert wp >= w0
            assert hp >= h0
        finally:
            _close(img_no_pad)
            _close(img_padded)

    def test_padding_zero_same_as_default(self, backend, any_app):
        """padding=0 produces the same size as no padding argument."""
        el = _app_element(any_app)
        if el is None:
            pytest.skip(f"no sized element in {any_app}")
        img_default = tp.screenshot(element=el)
        img_zero = tp.screenshot(element=el, padding=0)
        try:
            assert img_default.size == img_zero.size
        finally:
            _close(img_default)
            _close(img_zero)


# -----------------------------------------------------------------------
# Validation (error paths)
# -----------------------------------------------------------------------

@pytest.mark.unit
class TestScreenshotValidation:
    """Error paths that don't need a display or backend."""

    def test_negative_padding_raises(self):
        """Negative padding raises ValueError."""
        with pytest.raises(ValueError, match="padding must be non-negative"):
            tp.screenshot(padding=-1)

    def test_conflicting_scope_params_raises(self):
        """Passing multiple scope parameters raises ValueError."""
        with pytest.raises(ValueError, match="at most one scope parameter"):
            tp.screenshot(app="Firefox", monitor=0)

    def test_conflicting_element_and_app_raises(self):
        """element + app raises ValueError."""
        with pytest.raises(ValueError, match="at most one scope parameter"):
            tp.screenshot(element="fake:1", app="Firefox")


@pytest.mark.integration
class TestScreenshotValidationMonitor:
    """Monitor error paths (need Pillow + display)."""

    def test_monitor_out_of_range_raises(self):
        """monitor=999 raises ValueError."""
        with pytest.raises(ValueError, match="monitor 999 out of range"):
            tp.screenshot(monitor=999)

    def test_monitor_negative_raises(self):
        """monitor=-1 raises ValueError."""
        with pytest.raises(ValueError, match="monitor -1 out of range"):
            tp.screenshot(monitor=-1)


@pytest.mark.integration
@skip_without_backend
class TestScreenshotValidationBackend:
    """Error paths that need a backend but not a test app."""

    def test_nonexistent_app_raises(self):
        """A nonexistent app raises ValueError."""
        with pytest.raises(ValueError, match="no window found"):
            tp.screenshot(app="nonexistent_app_xyz_999")

    def test_nonexistent_window_id_raises(self):
        """A nonexistent window id raises ValueError."""
        with pytest.raises(ValueError, match="not found"):
            tp.screenshot(window_id="nonexistent:999:999")

    def test_nonexistent_element_string_raises(self):
        """A nonexistent element id string raises ValueError."""
        with pytest.raises(ValueError, match="not found"):
            tp.screenshot(element="nonexistent:999:999")

    def test_nonexistent_element_id_raises(self):
        """A well-formed but nonexistent element id raises ValueError."""
        with pytest.raises(ValueError, match="not found"):
            tp.screenshot(element="atspi:999:999:0.0.0")


# -----------------------------------------------------------------------
# monitor_count()
# -----------------------------------------------------------------------

@pytest.mark.integration
class TestMonitorCount:
    """tp.monitor_count() tests."""

    def test_returns_positive_int(self):
        """monitor_count() returns an int >= 1."""
        count = tp.monitor_count()
        assert isinstance(count, int)
        assert count >= 1

    def test_matches_get_monitor_regions(self):
        """monitor_count() == len(get_monitor_regions())."""
        assert tp.monitor_count() == len(get_monitor_regions())


# -----------------------------------------------------------------------
# get_monitor_regions() (low-level utility)
# -----------------------------------------------------------------------

@pytest.mark.integration
class TestGetMonitorRegions:
    """Low-level get_monitor_regions() tests."""

    def test_returns_non_empty_list(self):
        """At least one monitor region is returned."""
        regions = get_monitor_regions()
        assert isinstance(regions, list)
        assert len(regions) >= 1

    def test_regions_are_4_tuples(self):
        """Each region is a (left, top, right, bottom) tuple of ints."""
        for region in get_monitor_regions():
            assert len(region) == 4
            assert all(isinstance(v, int) for v in region)

    def test_regions_have_positive_size(self):
        """Each region has right > left and bottom > top."""
        for region in get_monitor_regions():
            left, top, right, bottom = region
            assert right > left, f"right ({right}) <= left ({left})"
            assert bottom > top, f"bottom ({bottom}) <= top ({top})"


# -----------------------------------------------------------------------
# _find_window() (internal helper)
# -----------------------------------------------------------------------

@pytest.mark.integration
@skip_without_backend
class TestFindWindow:
    """tp._find_window() internal helper tests."""

    def test_by_app_returns_window(self, any_app):
        """_find_window(app=...) returns a Window for a running app."""
        win = tp._find_window(app=any_app)
        assert win is not None
        assert win.app.lower() == any_app.lower()


@pytest.mark.integration
@skip_without_backend
class TestFindWindowEdgeCases:
    """_find_window() edge cases."""

    def test_by_app_nonexistent_returns_none(self):
        """A nonexistent app returns None."""
        result = tp._find_window(app="nonexistent_app_xyz_999")
        assert result is None

    def test_by_window_id_nonexistent_returns_none(self):
        """A nonexistent window id returns None."""
        result = tp._find_window(window_id="nonexistent:999:999")
        assert result is None

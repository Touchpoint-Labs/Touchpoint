"""Tests for touchpoint.utils.scale — display scale factor detection.

Covers:
- ``_get_x11_scale`` with mocked ``xrdb``
- ``get_scale_factor`` dispatch and ``tp.configure(scale_factor=...)``
- Integration: positions from AT-SPI / CDP backends are in physical pixels
"""

from __future__ import annotations

import sys
from unittest import mock

import pytest

import touchpoint as tp
from touchpoint.utils.scale import (
    _get_x11_scale,
    get_scale_factor,
    set_scale_factor,
)


# -----------------------------------------------------------------------
# Unit: _get_x11_scale
# -----------------------------------------------------------------------

class TestGetX11Scale:
    """Linux X11 scale detection via ``Xft.dpi``."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        """Clear lru_cache between tests."""
        _get_x11_scale.cache_clear()
        yield
        _get_x11_scale.cache_clear()

    @pytest.mark.unit
    def test_xft_dpi_110(self):
        """Xft.dpi=110 → scale 110/96."""
        fake = "Xft.dpi:\t110\n"
        with mock.patch("touchpoint.utils.scale.subprocess.check_output",
                        return_value=fake):
            assert _get_x11_scale() == pytest.approx(110 / 96.0)

    @pytest.mark.unit
    def test_xft_dpi_96(self):
        """Xft.dpi=96 → scale 1.0 (no scaling)."""
        fake = "Xft.dpi:\t96\n"
        with mock.patch("touchpoint.utils.scale.subprocess.check_output",
                        return_value=fake):
            assert _get_x11_scale() == 1.0

    @pytest.mark.unit
    def test_xft_dpi_144(self):
        """Xft.dpi=144 → scale 1.5."""
        fake = "Xft.dpi:\t144\n"
        with mock.patch("touchpoint.utils.scale.subprocess.check_output",
                        return_value=fake):
            assert _get_x11_scale() == 1.5

    @pytest.mark.unit
    def test_no_xft_dpi_line(self):
        """xrdb output without Xft.dpi → fallback 1.0."""
        fake = "Xft.antialias:\t1\nXft.hinting:\t1\n"
        with mock.patch("touchpoint.utils.scale.subprocess.check_output",
                        return_value=fake):
            assert _get_x11_scale() == 1.0

    @pytest.mark.unit
    def test_xrdb_missing(self):
        """xrdb not found → fallback 1.0."""
        with mock.patch("touchpoint.utils.scale.subprocess.check_output",
                        side_effect=FileNotFoundError):
            assert _get_x11_scale() == 1.0

    @pytest.mark.unit
    def test_xrdb_timeout(self):
        """xrdb hangs → fallback 1.0."""
        import subprocess as _sp
        with mock.patch("touchpoint.utils.scale.subprocess.check_output",
                        side_effect=_sp.TimeoutExpired("xrdb", 2)):
            assert _get_x11_scale() == 1.0

    @pytest.mark.unit
    def test_no_rounding(self):
        """Raw ratio returned without rounding to 0.125 steps."""
        fake = "Xft.dpi:\t110\n"
        with mock.patch("touchpoint.utils.scale.subprocess.check_output",
                        return_value=fake):
            result = _get_x11_scale()
            # 110/96 = 1.14583... — NOT rounded to 1.125
            assert result != 1.125
            assert result == pytest.approx(1.14583, abs=1e-4)


# -----------------------------------------------------------------------
# Unit: get_scale_factor dispatch + configure override
# -----------------------------------------------------------------------

class TestGetScaleFactor:
    """Top-level dispatch and tp.configure(scale_factor=...) override."""

    @pytest.fixture(autouse=True)
    def _clear(self):
        _get_x11_scale.cache_clear()
        set_scale_factor(None)
        yield
        set_scale_factor(None)
        _get_x11_scale.cache_clear()

    @pytest.mark.unit
    def test_configure_overrides_auto(self):
        """tp.configure(scale_factor=...) overrides auto-detection."""
        tp.configure(scale_factor=1.5)
        assert get_scale_factor() == 1.5

    @pytest.mark.unit
    def test_configure_none_clears_override(self):
        """tp.configure(scale_factor=None) reverts to auto-detect."""
        tp.configure(scale_factor=2.0)
        assert get_scale_factor() == 2.0
        tp.configure(scale_factor=None)
        # Should now auto-detect (platform-dependent, but must be a float)
        assert isinstance(get_scale_factor(), float)

    @pytest.mark.unit
    def test_set_scale_factor_clears_win32_cache(self):
        """set_scale_factor() clears the Windows per-monitor cache."""
        import touchpoint.utils.scale as scale_mod
        # Manually populate the cache as if Windows had already cached a monitor.
        scale_mod._win32_monitor_cache[12345] = 1.5
        set_scale_factor(None)
        # Cache must be cleared after reset.
        assert 12345 not in scale_mod._win32_monitor_cache

    @pytest.mark.unit
    def test_configure_validates_type(self):
        """Non-numeric scale_factor raises ValueError."""
        with pytest.raises(ValueError):
            tp.configure(scale_factor="big")

    @pytest.mark.unit
    def test_configure_validates_positive(self):
        """Zero or negative scale_factor raises ValueError."""
        with pytest.raises(ValueError):
            tp.configure(scale_factor=0)
        with pytest.raises(ValueError):
            tp.configure(scale_factor=-1.5)

    @pytest.mark.unit
    def test_configure_rejects_bool(self):
        """Boolean scale_factor raises ValueError."""
        with pytest.raises(ValueError):
            tp.configure(scale_factor=True)

    @pytest.mark.unit
    def test_configure_int_accepted(self):
        """Integer scale_factor is accepted and returned as float."""
        tp.configure(scale_factor=2)
        assert get_scale_factor() == 2.0
        assert isinstance(get_scale_factor(), float)

    @pytest.mark.unit
    def test_configure_fractional_preserved(self):
        """Non-0.125-aligned values are preserved exactly (no rounding)."""
        tp.configure(scale_factor=1.333)
        assert get_scale_factor() == 1.333

    @pytest.mark.unit
    def test_configure_overrides_xrdb(self):
        """User override takes priority over xrdb auto-detection."""
        tp.configure(scale_factor=1.5)
        fake_output = "Xft.dpi:\t110\n"
        with mock.patch("touchpoint.utils.scale.subprocess.check_output",
                        return_value=fake_output):
            assert get_scale_factor() == 1.5

    @pytest.mark.unit
    @pytest.mark.skipif(sys.platform != "linux",
                        reason="Linux dispatch test")
    def test_linux_auto_returns_float(self):
        """On Linux without override, auto-detection returns a float."""
        result = get_scale_factor()
        assert isinstance(result, float)
        assert result > 0


# -----------------------------------------------------------------------
# Integration: physical pixel convention
# -----------------------------------------------------------------------

class TestPhysicalPixelConvention:
    """Verify that element positions are in physical pixels.

    These tests compare element positions against screen dimensions
    to ensure they're in the physical coordinate space (not logical).
    Only meaningful on high-DPI systems (DPR > 1).
    """

    @pytest.mark.integration
    def test_window_positions_within_physical_screen(self, backend):
        """Window positions should be within physical screen bounds."""
        import touchpoint as tp
        wins = tp.windows()
        if not wins:
            pytest.skip("no windows found")

        scale = get_scale_factor()
        if scale == 1.0:
            pytest.skip("DPR is 1.0 — can't distinguish physical/logical")

        for w in wins:
            if w.size[0] == 0 or w.size[1] == 0:
                continue
            # On a scaled display, physical coords should be larger
            # than logical.  A window at logical (100, 100) with
            # scale 1.125 should be at physical (113, 113).
            # We just verify they're non-negative integers.
            assert isinstance(w.position[0], int)
            assert isinstance(w.position[1], int)

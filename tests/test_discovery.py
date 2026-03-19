"""Tests for tp.apps() and tp.windows() — discovery layer.

All tests are integration tests that require a live desktop with
at least one visible application window.
"""

from __future__ import annotations

import pytest

import touchpoint as tp
from tests.conftest import (
    assert_valid_window,
    skip_without_backend,
)


# -----------------------------------------------------------------------
# tp.apps()
# -----------------------------------------------------------------------

@pytest.mark.integration
@skip_without_backend
class TestApps:
    """Tests for tp.apps()."""

    def test_returns_list(self):
        apps = tp.apps()
        assert isinstance(apps, list)
        assert len(apps) > 0, "live desktop should have at least one app"

    def test_no_empty_names(self):
        apps = tp.apps()
        for name in apps:
            assert isinstance(name, str)
            assert name.strip() != "", f"app name should not be empty: {name!r}"


# -----------------------------------------------------------------------
# tp.windows()
# -----------------------------------------------------------------------

@pytest.mark.integration
@skip_without_backend
class TestWindows:
    """Tests for tp.windows()."""

    def test_returns_list(self):
        wins = tp.windows()
        assert isinstance(wins, list)
        assert len(wins) > 0, "live desktop should have at least one window"

    def test_valid_structure(self):
        wins = tp.windows()
        for w in wins:
            assert_valid_window(w)

    def test_have_apps(self):
        """Every window's app matches something in tp.apps()."""
        apps = set(tp.apps())
        wins = tp.windows()
        for w in wins:
            assert w.app in apps, (
                f"window {w.id!r} reports app {w.app!r} "
                f"which is not in tp.apps()"
            )

    def test_at_least_one_visible(self):
        wins = tp.windows()
        visible = [
            w for w in wins
            if w.is_visible and w.size[0] > 0 and w.size[1] > 0
        ]
        assert len(visible) > 0, "live desktop should have at least one visible window"

    def test_at_most_one_active(self):
        wins = tp.windows()
        active = [w for w in wins if w.is_active]
        assert len(active) <= 1, (
            f"expected at most 1 active window, got {len(active)}: "
            f"{[w.title for w in active]}"
        )

    def test_ids_unique(self):
        wins = tp.windows()
        ids = [w.id for w in wins]
        assert len(ids) == len(set(ids)), (
            f"duplicate window IDs: {[i for i in ids if ids.count(i) > 1]}"
        )

    def test_pid_positive(self):
        wins = tp.windows()
        for w in wins:
            assert w.pid > 0, (
                f"window {w.id!r} ({w.title!r}) has invalid pid={w.pid}"
            )

    def test_sizes_non_negative(self):
        """Window sizes should never be negative."""
        wins = tp.windows()
        for w in wins:
            assert w.size[0] >= 0 and w.size[1] >= 0, (
                f"window {w.id!r} ({w.title!r}) has negative size "
                f"{w.size}"
            )



"""Tests for tp.wait_for(), tp.wait_for_app(), tp.wait_for_window().

wait_for polls find() until elements appear/disappear (or timeout).
wait_for_app polls apps() until the app appears/disappears.
wait_for_window polls windows() until the window appears/disappears.

Integration tests use the live desktop.  Validation tests are
pure unit tests that run anywhere.
"""

from __future__ import annotations

import time

import pytest

import touchpoint as tp
from touchpoint.core.element import Element
from touchpoint.core.types import Role, State
from tests.conftest import skip_without_backend


# -----------------------------------------------------------------------
# Pure validation — no backend needed
# -----------------------------------------------------------------------

@pytest.mark.unit
class TestWaitValidation:
    """Input validation tests (no desktop required)."""

    def test_wait_for_invalid_mode(self):
        with pytest.raises(ValueError, match="mode"):
            tp.wait_for("anything", mode="bad")

    def test_wait_for_gone_invalid_mode(self):
        with pytest.raises(ValueError, match="mode"):
            tp.wait_for("anything", mode="bad", gone=True)


# -----------------------------------------------------------------------
# wait_for — integration
# -----------------------------------------------------------------------

@pytest.mark.integration
@skip_without_backend
class TestWaitFor:
    """wait_for() integration tests against the live desktop."""

    def test_existing_element_returns_immediately(self, any_element):
        """An element that already exists should return without delay."""
        start = time.monotonic()
        results = tp.wait_for(
            any_element.name,
            app=any_element.app,
            timeout=5.0,
        )
        elapsed = time.monotonic() - start
        assert len(results) > 0
        assert isinstance(results[0], Element)
        # Should have returned on the first poll — scoped to one app.
        assert elapsed < 5.0, f"wait_for returned but took {elapsed:.1f}s"

    def test_returns_element_list(self, any_element):
        results = tp.wait_for(
            any_element.name,
            app=any_element.app,
            timeout=5.0,
        )
        assert isinstance(results, list)
        for el in results:
            assert isinstance(el, Element)

    def test_timeout_raises(self, any_app):
        """Searching for a nonexistent element should time out."""
        with pytest.raises(TimeoutError):
            tp.wait_for(
                "nonexistent_element_xyz_42",
                app=any_app,
                timeout=2.0,
                poll=0.5,
            )

    def test_timeout_duration(self, any_app):
        """Timeout should take roughly the right amount of time."""
        timeout = 2.0
        start = time.monotonic()
        with pytest.raises(TimeoutError):
            tp.wait_for(
                "nonexistent_element_xyz_42",
                app=any_app,
                timeout=timeout,
                poll=0.5,
            )
        elapsed = time.monotonic() - start
        # Should be at least timeout, but not wildly over
        assert elapsed >= timeout - 0.1, (
            f"timed out too early: {elapsed:.2f}s < {timeout}s"
        )
        assert elapsed < timeout + 5.0, (
            f"timed out too late: {elapsed:.2f}s"
        )

    def test_scoped_by_app(self, any_element):
        results = tp.wait_for(
            any_element.name,
            app=any_element.app,
            timeout=5.0,
        )
        assert len(results) > 0
        for el in results:
            assert el.app.lower() == any_element.app.lower()

    def test_mode_any_single_query(self, any_element):
        """mode='any' with a single string should work like default."""
        results = tp.wait_for(
            any_element.name,
            app=any_element.app,
            mode="any",
            timeout=5.0,
        )
        assert len(results) > 0

    def test_mode_any_list(self, any_element):
        """mode='any' with a list: returns when first query matches."""
        results = tp.wait_for(
            [any_element.name, "nonexistent_xyz_42"],
            app=any_element.app,
            mode="any",
            timeout=5.0,
        )
        assert len(results) > 0

    def test_mode_all_success(self, any_element):
        """mode='all' with a single query that exists — should return."""
        results = tp.wait_for(
            [any_element.name],
            app=any_element.app,
            mode="all",
            timeout=5.0,
        )
        assert len(results) > 0

    def test_mode_all_timeout(self, any_element):
        """mode='all' where one query can't match — should timeout."""
        with pytest.raises(TimeoutError):
            tp.wait_for(
                [any_element.name, "nonexistent_xyz_42"],
                app=any_element.app,
                mode="all",
                timeout=2.0,
                poll=0.5,
            )

    def test_scoped_by_window_id(self, any_element, any_window):
        """window_id parameter is forwarded to find()."""
        wid = any_element.window_id
        if wid is None:
            pytest.skip("element has no window_id")
        # Use the element's window for scoping.
        results = tp.wait_for(
            any_element.name,
            window_id=wid,
            timeout=5.0,
        )
        assert len(results) > 0
        for el in results:
            assert el.window_id == wid

    def test_with_role(self, any_element):
        """role parameter is forwarded to find()."""
        results = tp.wait_for(
            any_element.name,
            app=any_element.app,
            role=any_element.role,
            timeout=5.0,
        )
        assert len(results) > 0
        for el in results:
            assert el.role == any_element.role

    def test_with_states(self, any_element):
        """states parameter is forwarded to find()."""
        # Use a state the element is known to have.
        if State.VISIBLE not in any_element.states:
            pytest.skip("any_element is not VISIBLE")
        results = tp.wait_for(
            any_element.name,
            app=any_element.app,
            states=[State.VISIBLE],
            timeout=5.0,
        )
        assert len(results) > 0
        for el in results:
            assert State.VISIBLE in el.states

    def test_mode_all_all_exist(self, any_element):
        """mode='all' returns immediately when all queries match."""
        # Use the same real name twice — both will match.
        start = time.monotonic()
        results = tp.wait_for(
            [any_element.name, any_element.name],
            app=any_element.app,
            mode="all",
            timeout=5.0,
        )
        elapsed = time.monotonic() - start
        assert len(results) > 0
        assert elapsed < 5.0, (
            f"mode='all' with all queries matching took {elapsed:.1f}s"
        )

    def test_fields_name(self, any_element):
        """fields=['name'] should match an element by its name."""
        results = tp.wait_for(
            any_element.name,
            app=any_element.app,
            fields=["name"],
            timeout=5.0,
        )
        assert len(results) > 0
        assert any(el.name == any_element.name for el in results)

    def test_fields_no_match(self, any_element):
        """fields=['value'] should miss a match that only exists
        in the name field, causing a timeout."""
        # Use the element's name as query but search only in 'value'.
        # Unless the value contains the name this should time out.
        if any_element.value and (
            any_element.value == any_element.name
            or any_element.name.lower() in any_element.value.lower()
        ):
            pytest.skip("element value contains its name — can't test")
        # Also skip if *any other* element in the app has the query
        # in its value — otherwise find() will match that element.
        pre_check = tp.find(
            any_element.name,
            app=any_element.app,
            fields=["value"],
        )
        if pre_check:
            pytest.skip(
                "another element in the app has the query in its value"
            )
        with pytest.raises(TimeoutError):
            tp.wait_for(
                any_element.name,
                app=any_element.app,
                fields=["value"],
                timeout=2.0,
                poll=0.5,
            )


# -----------------------------------------------------------------------
# wait_for(gone=True) — integration
# -----------------------------------------------------------------------

@pytest.mark.integration
@skip_without_backend
class TestWaitForGone:
    """wait_for(gone=True) integration tests against the live desktop."""

    def test_already_gone_returns_true(self, any_app):
        """An element that doesn't exist should return True immediately."""
        start = time.monotonic()
        result = tp.wait_for(
            "nonexistent_element_xyz_42",
            app=any_app,
            timeout=5.0,
            gone=True,
        )
        elapsed = time.monotonic() - start
        assert result is True
        assert elapsed < 5.0, (
            f"wait_for(gone=True) returned but took {elapsed:.1f}s"
        )

    def test_returns_bool(self, any_app):
        result = tp.wait_for(
            "nonexistent_element_xyz_42",
            app=any_app,
            timeout=5.0,
            gone=True,
        )
        assert isinstance(result, bool)

    def test_persistent_element_times_out(self, any_element):
        """An element that persists should cause TimeoutError."""
        with pytest.raises(TimeoutError):
            tp.wait_for(
                any_element.name,
                app=any_element.app,
                timeout=2.0,
                poll=0.5,
                gone=True,
            )

    def test_mode_all_already_gone(self, any_app):
        """mode='all' with two nonexistent queries: both gone -> True."""
        result = tp.wait_for(
            ["nonexistent_a_42", "nonexistent_b_42"],
            app=any_app,
            mode="all",
            timeout=5.0,
            gone=True,
        )
        assert result is True

    def test_mode_any_one_gone(self, any_app):
        """mode='any': at least one query not found -> True."""
        result = tp.wait_for(
            ["nonexistent_a_42", "nonexistent_b_42"],
            app=any_app,
            mode="any",
            timeout=5.0,
            gone=True,
        )
        assert result is True

    def test_mode_all_one_persists(self, any_element):
        """mode='all' where one query still matches -> timeout."""
        with pytest.raises(TimeoutError):
            tp.wait_for(
                [any_element.name, "nonexistent_xyz_42"],
                app=any_element.app,
                mode="all",
                timeout=2.0,
                poll=0.5,
                gone=True,
            )

    def test_mode_any_one_persists(self, any_element):
        """mode='any' where one query is gone -> returns True even
        though the other persists."""
        result = tp.wait_for(
            [any_element.name, "nonexistent_xyz_42"],
            app=any_element.app,
            mode="any",
            timeout=5.0,
            gone=True,
        )
        assert result is True

    def test_fields_already_gone(self, any_app):
        """fields=['value'] — nonexistent query should be gone."""
        result = tp.wait_for(
            "nonexistent_xyz_42",
            app=any_app,
            fields=["value"],
            timeout=5.0,
            gone=True,
        )
        assert result is True

    def test_fields_still_present(self, any_element):
        """fields=['name'] — an element that exists by name should
        cause a timeout."""
        with pytest.raises(TimeoutError):
            tp.wait_for(
                any_element.name,
                app=any_element.app,
                fields=["name"],
                timeout=2.0,
                poll=0.5,
                gone=True,
            )


# -----------------------------------------------------------------------
# wait_for_app — integration
# -----------------------------------------------------------------------

@pytest.mark.integration
@skip_without_backend
class TestWaitForApp:
    """wait_for_app() integration tests against the live desktop."""

    def test_existing_app_returns_immediately(self, any_app):
        """An app already running should return True without delay."""
        start = time.monotonic()
        result = tp.wait_for_app(any_app, timeout=10.0)
        elapsed = time.monotonic() - start
        assert result is True
        assert elapsed < 10.0, f"took {elapsed:.1f}s"

    def test_nonexistent_app_times_out(self):
        """A nonexistent app should cause TimeoutError."""
        with pytest.raises(TimeoutError):
            tp.wait_for_app(
                "nonexistent_app_xyz_42",
                timeout=2.0,
                poll=0.5,
            )

    def test_gone_nonexistent_returns_true(self):
        """gone=True for a nonexistent app should return immediately."""
        result = tp.wait_for_app(
            "nonexistent_app_xyz_42",
            timeout=5.0,
            gone=True,
        )
        assert result is True

    def test_gone_existing_times_out(self, any_app):
        """gone=True for a running app should time out."""
        with pytest.raises(TimeoutError):
            tp.wait_for_app(any_app, timeout=2.0, poll=0.5, gone=True)


# -----------------------------------------------------------------------
# wait_for_window — integration
# -----------------------------------------------------------------------

@pytest.mark.integration
@skip_without_backend
class TestWaitForWindow:
    """wait_for_window() integration tests against the live desktop."""

    def test_existing_window_returns_window(self, any_window):
        """An existing window should return a Window object."""
        from touchpoint.core.window import Window
        start = time.monotonic()
        result = tp.wait_for_window(any_window.title, timeout=5.0)
        elapsed = time.monotonic() - start
        assert isinstance(result, Window)
        assert elapsed < 5.0, f"took {elapsed:.1f}s"

    def test_existing_window_scoped_by_app(self, any_window):
        """Scoping by app should still find the window."""
        result = tp.wait_for_window(
            any_window.title,
            app=any_window.app,
            timeout=5.0,
        )
        from touchpoint.core.window import Window
        assert isinstance(result, Window)

    def test_nonexistent_window_times_out(self):
        """A nonexistent window should cause TimeoutError."""
        with pytest.raises(TimeoutError):
            tp.wait_for_window(
                "nonexistent_window_xyz_42",
                timeout=2.0,
                poll=0.5,
            )

    def test_gone_nonexistent_returns_true(self):
        """gone=True for a nonexistent window should return immediately."""
        result = tp.wait_for_window(
            "nonexistent_window_xyz_42",
            timeout=5.0,
            gone=True,
        )
        assert result is True

    def test_gone_existing_times_out(self, any_window):
        """gone=True for an existing window should time out."""
        with pytest.raises(TimeoutError):
            tp.wait_for_window(
                any_window.title,
                timeout=2.0,
                poll=0.5,
                gone=True,
            )

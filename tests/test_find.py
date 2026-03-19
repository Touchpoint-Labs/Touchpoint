"""Tests for tp.find() — search and matching pipeline.

Covers exact, contains, and fuzzy match stages, plus filtering,
field selection, and format output.
"""

from __future__ import annotations

import json

import pytest

import touchpoint as tp
from touchpoint.core.types import Role, State
from tests.conftest import (
    assert_valid_element,
    skip_without_backend,
)


# -----------------------------------------------------------------------
# Helper fixture — a known element name to search for
# -----------------------------------------------------------------------

@pytest.fixture
def known_element(backend):
    """Return a named, visible element with a name ≥ 4 chars.

    Searches all visible apps for a reliable search target.
    """
    wins = backend.windows()
    apps_to_try = list(dict.fromkeys(
        w.app for w in wins
        if w.is_visible and w.size[0] > 0 and w.size[1] > 0
    ))
    for app in apps_to_try:
        elems = tp.elements(
            app=app,
            named_only=True,
            states=[State.VISIBLE, State.SHOWING],
        )
        for el in elems:
            if el.name and len(el.name.strip()) >= 4:
                return el
    pytest.skip("no named element with ≥4 chars in any app")


# -----------------------------------------------------------------------
# Basic find behaviour
# -----------------------------------------------------------------------

@pytest.mark.integration
@skip_without_backend
class TestFindBasic:
    """Core find() tests."""

    def test_returns_list(self, any_app):
        result = tp.find("anything", app=any_app)
        assert isinstance(result, list)

    def test_exact_match(self, known_element):
        results = tp.find(known_element.name, app=known_element.app)
        assert len(results) > 0, (
            f"find({known_element.name!r}) returned empty"
        )
        names = [el.name for el in results]
        assert known_element.name in names

    def test_case_insensitive(self, known_element):
        query = known_element.name.upper()
        results = tp.find(query, app=known_element.app)
        assert len(results) > 0, (
            f"find({query!r}) should match case-insensitively"
        )

    def test_contains_match(self, known_element):
        name = known_element.name.strip()
        # Take a substring from the middle.
        mid = len(name) // 2
        substring = name[mid - 1 : mid + 2]  # 3-char slice
        results = tp.find(substring, app=known_element.app)
        assert len(results) > 0, (
            f"find({substring!r}) should match via contains"
        )

    def test_no_match(self, any_app):
        results = tp.find("nonexistent_xyz_12345", app=any_app)
        assert results == []

    def test_valid_elements(self, known_element):
        """Every returned element is fully populated (inflate works)."""
        results = tp.find(known_element.name, app=known_element.app)
        for el in results:
            assert_valid_element(el)


# -----------------------------------------------------------------------
# Filtering within find()
# -----------------------------------------------------------------------

@pytest.mark.integration
@skip_without_backend
class TestFindFiltering:
    """Filter and constraint tests."""

    def test_role_filter(self, any_app):
        # Find any button name first.
        buttons = tp.elements(
            app=any_app, role=Role.BUTTON, named_only=True,
        )
        if not buttons:
            pytest.skip("no named buttons to search")
        query = buttons[0].name
        results = tp.find(query, app=any_app, role=Role.BUTTON)
        for el in results:
            assert el.role == Role.BUTTON

    def test_state_filter(self, known_element):
        results = tp.find(
            known_element.name, app=known_element.app,
            states=[State.VISIBLE],
        )
        for el in results:
            assert State.VISIBLE in el.states

    def test_max_results(self, known_element):
        results = tp.find(
            known_element.name, app=known_element.app, max_results=1,
        )
        assert len(results) <= 1

    def test_custom_filter(self, known_element):
        results = tp.find(
            known_element.name, app=known_element.app,
            filter=lambda e: e.role == known_element.role,
        )
        for el in results:
            assert el.role == known_element.role

    def test_filter_rejects_all(self, known_element):
        """A filter that rejects everything returns empty list."""
        results = tp.find(
            known_element.name, app=known_element.app,
            filter=lambda e: False,
        )
        assert results == []

    def test_max_results_zero(self, known_element):
        """max_results=0 returns an empty list."""
        results = tp.find(
            known_element.name, app=known_element.app,
            max_results=0,
        )
        assert results == []

    def test_scoped_by_window_id(self, any_window):
        """find() with window_id only returns elements from that window."""
        # Get a named element in this window to search for.
        elems = tp.elements(
            window_id=any_window.id, named_only=True,
        )
        if not elems:
            pytest.skip("no named elements in window")
        target = elems[0]
        results = tp.find(
            target.name, window_id=any_window.id,
        )
        assert len(results) > 0, (
            f"find({target.name!r}, window_id=...) returned empty"
        )
        for el in results:
            assert el.window_id == any_window.id, (
                f"element {el.id!r} has window_id={el.window_id!r}, "
                f"expected {any_window.id!r}"
            )


# -----------------------------------------------------------------------
# Field selection
# -----------------------------------------------------------------------

@pytest.mark.integration
@skip_without_backend
class TestFindFields:
    """Search field selection tests."""

    def test_fields_value(self, any_app):
        """Searching by value finds elements with matching value."""
        # Find an element with a non-empty value.
        elems = tp.elements(app=any_app)
        valued = [e for e in elems if e.value and len(e.value) >= 4]
        if not valued:
            pytest.skip("no elements with value ≥4 chars")
        target = valued[0]
        results = tp.find(
            target.value, app=any_app, fields=["value"],
        )
        assert len(results) > 0, (
            f"find({target.value!r}, fields=['value']) returned empty"
        )

    def test_fields_multi(self, known_element):
        results = tp.find(
            known_element.name, app=known_element.app,
            fields=["name", "value"],
        )
        assert len(results) > 0

    def test_fields_description(self, any_app):
        """Searching by description field doesn't crash and returns
        a list (may be empty if no elements have descriptions)."""
        # We can't guarantee any element has a description, but
        # the code path must not error.
        results = tp.find("anything", app=any_app, fields=["description"])
        assert isinstance(results, list)


# -----------------------------------------------------------------------
# Pure validation — no backend needed
# -----------------------------------------------------------------------

@pytest.mark.unit
class TestFindValidation:
    """Tests that only exercise input validation (no desktop)."""

    def test_fields_invalid_raises(self):
        with pytest.raises(ValueError, match="invalid fields"):
            tp.find("x", fields=["invalid_field"])

    def test_format_tree_raises(self):
        with pytest.raises(ValueError, match="tree"):
            tp.find("anything", format="tree")


# -----------------------------------------------------------------------
# Format output
# -----------------------------------------------------------------------

@pytest.mark.integration
@skip_without_backend
class TestFindFormat:
    """Format parameter tests."""

    def test_format_flat(self, known_element):
        result = tp.find(
            known_element.name, app=known_element.app, format="flat",
        )
        assert isinstance(result, str)

    def test_format_json(self, known_element):
        result = tp.find(
            known_element.name, app=known_element.app, format="json",
        )
        assert isinstance(result, str)
        parsed = json.loads(result)
        assert isinstance(parsed, list)

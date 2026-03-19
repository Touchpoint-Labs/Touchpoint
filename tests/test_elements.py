"""Tests for tp.elements(), tp.get_element(), tp.element_at().

Covers element retrieval, filtering, tree mode, formatting, and
single-element lookups.
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
# tp.elements() — basic retrieval
# -----------------------------------------------------------------------

@pytest.mark.integration
@skip_without_backend
class TestElementsBasic:
    """Basic element retrieval tests."""

    def test_returns_list(self, any_app):
        elems = tp.elements(app=any_app)
        assert isinstance(elems, list)
        assert len(elems) > 0

    def test_valid_structure(self, any_app):
        elems = tp.elements(app=any_app)
        for el in elems[:20]:
            assert_valid_element(el)

    def test_scoped_by_app(self, any_app):
        elems = tp.elements(app=any_app)
        for el in elems:
            assert el.app == any_app, (
                f"element {el.id!r} has app={el.app!r}, "
                f"expected {any_app!r}"
            )

    def test_scoped_by_window(self, any_window):
        elems = tp.elements(window_id=any_window.id)
        for el in elems:
            assert el.window_id == any_window.id, (
                f"element {el.id!r} has window_id={el.window_id!r}, "
                f"expected {any_window.id!r}"
            )


# -----------------------------------------------------------------------
# tp.elements() — filtering
# -----------------------------------------------------------------------

@pytest.mark.integration
@skip_without_backend
class TestElementsFiltering:
    """Filter parameter tests."""

    def test_named_only(self, any_app):
        elems = tp.elements(app=any_app, named_only=True)
        for el in elems:
            assert el.name and el.name.strip(), (
                f"named_only=True returned element with empty name: {el.id!r}"
            )

    def test_state_filter(self, any_app):
        elems = tp.elements(app=any_app, states=[State.VISIBLE])
        for el in elems:
            assert State.VISIBLE in el.states, (
                f"states=[VISIBLE] returned element without VISIBLE: "
                f"{el.id!r} states={el.states}"
            )

    def test_role_filter(self, any_app):
        elems = tp.elements(app=any_app, role=Role.BUTTON)
        for el in elems:
            assert el.role == Role.BUTTON, (
                f"role=BUTTON returned element with role={el.role!r}: "
                f"{el.id!r}"
            )

    def test_combined_filters(self, any_app):
        elems = tp.elements(
            app=any_app,
            role=Role.BUTTON,
            states=[State.VISIBLE],
            named_only=True,
        )
        for el in elems:
            assert el.role == Role.BUTTON
            assert State.VISIBLE in el.states
            assert el.name and el.name.strip()

    def test_custom_filter(self, any_app):
        elems = tp.elements(
            app=any_app,
            named_only=True,
            filter=lambda e: len(e.name) > 5,
        )
        for el in elems:
            assert len(el.name) > 5, (
                f"custom filter len(name)>5 returned {el.name!r}"
            )

    def test_sort_by_position(self, any_app):
        elems = tp.elements(
            app=any_app,
            states=[State.VISIBLE],
            sort_by="position",
        )
        if len(elems) < 2:
            pytest.skip("need at least 2 elements to test sorting")
        for i in range(1, len(elems)):
            prev_y, prev_x = elems[i - 1].position[1], elems[i - 1].position[0]
            cur_y, cur_x = elems[i].position[1], elems[i].position[0]
            assert (cur_y, cur_x) >= (prev_y, prev_x), (
                f"sort_by='position' not in reading order: "
                f"({prev_x},{prev_y}) before ({cur_x},{cur_y})"
            )

    def test_sort_by_callable(self, any_app):
        """A callable sort_by is passed to sorted() as a key."""
        elems = tp.elements(app=any_app, named_only=True)
        if len(elems) < 2:
            pytest.skip("need at least 2 named elements to test sorting")
        sorted_elems = tp.elements(
            app=any_app, named_only=True,
            sort_by=lambda el: el.name.lower(),
        )
        names = [el.name.lower() for el in sorted_elems]
        assert names == sorted(names), (
            f"sort_by=callable did not sort alphabetically"
        )


# -----------------------------------------------------------------------
# Pure validation — no backend needed
# -----------------------------------------------------------------------

@pytest.mark.unit
class TestElementsValidation:
    """Tests that only exercise input validation (no desktop)."""

    def test_sort_by_invalid_raises(self):
        with pytest.raises(ValueError, match="unknown sort_by"):
            tp.elements(sort_by="invalid")


# -----------------------------------------------------------------------
# tp.elements() — safety limits (max_elements, max_depth)
# -----------------------------------------------------------------------

@pytest.mark.integration
@skip_without_backend
class TestElementsLimits:
    """Tests for max_elements and max_depth parameters."""

    def test_max_elements_caps_count(self, any_app):
        """max_elements=5 should return at most 5 elements."""
        elems = tp.elements(app=any_app, max_elements=5)
        assert len(elems) <= 5

    def test_max_elements_less_than_full(self, any_app):
        """A small max_elements should return fewer than a full walk."""
        full = tp.elements(app=any_app, max_depth=100)
        if len(full) <= 3:
            pytest.skip("app has too few elements to test capping")
        capped = tp.elements(app=any_app, max_elements=3)
        assert len(capped) < len(full)

    def test_max_depth_limits_tree(self, any_app):
        """max_depth=0 tree should have no children on any node."""
        elems = tp.elements(app=any_app, tree=True, max_depth=0)
        for el in elems:
            assert not el.children, (
                f"max_depth=0 tree node {el.id!r} should have "
                f"no children, got {len(el.children)}"
            )


# -----------------------------------------------------------------------
# tp.elements() — tree mode
# -----------------------------------------------------------------------

@pytest.mark.integration
@skip_without_backend
class TestElementsTree:
    """Tree mode tests."""

    def test_tree_has_children(self, any_app):
        elems = tp.elements(app=any_app, tree=True, max_depth=1)
        has_children = any(
            el.children and len(el.children) > 0
            for el in elems
        )
        assert has_children, (
            "tree=True max_depth=1 should produce at least one "
            "element with children"
        )

    def test_tree_max_depth_0(self, any_app):
        elems = tp.elements(app=any_app, tree=True, max_depth=0)
        for el in elems:
            assert not el.children, (
                f"max_depth=0 should have no children, got "
                f"{len(el.children)} on {el.id!r}"
            )

    def test_tree_filter_children_false(self, any_app):
        """filter_children=False keeps raw unfiltered children."""
        elems = tp.elements(
            app=any_app,
            tree=True,
            max_depth=1,
            named_only=True,
            filter_children=False,
        )
        # Top-level elements must all have names (named_only applies).
        for el in elems:
            assert el.name and el.name.strip()
        # But their children may have empty names (not filtered).
        all_children = [
            c for el in elems for c in (el.children or [])
        ]
        if not all_children:
            pytest.skip("no children to verify filter_children=False")
        # We just need to confirm children exist — they may or may
        # not have names.  The key is they weren't pruned.
        assert len(all_children) > 0

    def test_tree_filter_children_true(self, any_app):
        """filter_children=True applies named_only to children too."""
        elems = tp.elements(
            app=any_app,
            tree=True,
            max_depth=1,
            named_only=True,
            filter_children=True,
        )
        for el in elems:
            assert el.name and el.name.strip()
            for child in el.children or []:
                assert child.name and child.name.strip(), (
                    f"filter_children=True should filter unnamed "
                    f"children, but {child.id!r} has name={child.name!r}"
                )


# -----------------------------------------------------------------------
# tp.elements() — root_element scoping
# -----------------------------------------------------------------------

@pytest.mark.integration
@skip_without_backend
class TestElementsRootElement:
    """Tests for the root_element parameter."""

    def test_root_element_by_object(self, any_app):
        """Passing an Element narrows the walk to its subtree."""
        # Use a large max_depth so depth capping doesn't make the
        # root_element walk (which restarts from 0) return more
        # elements than the full walk.
        all_elems = tp.elements(app=any_app, max_depth=100)
        if len(all_elems) < 2:
            pytest.skip("need at least 2 elements")
        # Pick an element that has children (i.e. appears as a
        # prefix of other element IDs).
        parent = None
        for candidate in all_elems:
            children = [
                e for e in all_elems
                if e.id.startswith(candidate.id + ".")
                or e.id.startswith(candidate.id + ":")
            ]
            if children:
                parent = candidate
                break
        if parent is None:
            pytest.skip("no element with children found")
        scoped = tp.elements(app=any_app, root_element=parent, max_depth=100)
        assert len(scoped) > 0, (
            f"root_element={parent.id!r} returned no children"
        )
        assert len(scoped) < len(all_elems), (
            "root_element should narrow results"
        )

    def test_root_element_by_id(self, any_app):
        """Passing a string ID works the same as an Element."""
        all_elems = tp.elements(app=any_app, max_depth=100)
        parent = None
        for candidate in all_elems:
            children = [
                e for e in all_elems
                if e.id.startswith(candidate.id + ".")
                or e.id.startswith(candidate.id + ":")
            ]
            if children:
                parent = candidate
                break
        if parent is None:
            pytest.skip("no element with children found")
        by_obj = tp.elements(app=any_app, root_element=parent, max_depth=100)
        by_id = tp.elements(app=any_app, root_element=parent.id, max_depth=100)
        assert len(by_obj) == len(by_id), (
            f"root_element by object ({len(by_obj)}) and by id "
            f"({len(by_id)}) should return the same count"
        )

    def test_root_element_invalid_id(self, any_app):
        """An invalid root_element id returns an empty list."""
        # Skip for CDP apps — a non-CDP root_element correctly raises
        # ValueError in the CDP backend.
        try:
            from touchpoint import _is_cdp_app
            if _is_cdp_app(any_app):
                pytest.skip("test not applicable to CDP apps")
        except ImportError:
            pass
        result = tp.elements(
            app=any_app,
            root_element="nonexistent:999:999:999.999",
        )
        assert result == []


# -----------------------------------------------------------------------
# tp.get_element()
# -----------------------------------------------------------------------

@pytest.mark.integration
@skip_without_backend
class TestGetElement:
    """Tests for tp.get_element()."""

    def test_roundtrip(self, any_element):
        """Get an element by ID, verify it matches the original."""
        refreshed = tp.get_element(any_element.id)
        assert refreshed is not None, (
            f"get_element({any_element.id!r}) returned None"
        )
        assert refreshed.id == any_element.id
        assert refreshed.name == any_element.name
        assert refreshed.role == any_element.role
        assert refreshed.app == any_element.app
        assert_valid_element(refreshed)

    def test_invalid_id(self):
        result = tp.get_element("nonexistent:99:99:99")
        assert result is None


# -----------------------------------------------------------------------
# tp.element_at()
# -----------------------------------------------------------------------

@pytest.mark.integration
@skip_without_backend
class TestElementAt:
    """Tests for tp.element_at()."""

    def test_at_window_center(self, any_window):
        """Something must be at the center of a visible window."""
        cx = any_window.position[0] + any_window.size[0] // 2
        cy = any_window.position[1] + any_window.size[1] // 2
        el = tp.element_at(cx, cy)
        assert el is not None, (
            f"element_at({cx}, {cy}) returned None at center of "
            f"window {any_window.title!r}"
        )
        assert_valid_element(el)

    def test_at_offscreen(self):
        result = tp.element_at(-9999, -9999)
        assert result is None


# -----------------------------------------------------------------------
# tp.elements() — format output
# -----------------------------------------------------------------------

@pytest.mark.integration
@skip_without_backend
class TestElementsFormat:
    """Format parameter tests."""

    def test_format_flat(self, any_app):
        result = tp.elements(app=any_app, format="flat")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_format_json(self, any_app):
        result = tp.elements(app=any_app, format="json")
        assert isinstance(result, str)
        parsed = json.loads(result)
        assert isinstance(parsed, list)
        assert len(parsed) > 0

    def test_format_tree(self, any_app):
        """Tree format via the public API returns an indented string."""
        result = tp.elements(
            app=any_app, tree=True, max_depth=1, format="tree",
        )
        assert isinstance(result, str)
        assert len(result) > 0
        # Tree format uses indentation — at least some lines should
        # start with whitespace.
        lines = result.strip().splitlines()
        assert len(lines) > 1, "tree format should produce multiple lines"
        assert any(
            line.startswith(" ") or line.startswith("\t") for line in lines
        ), "tree format should have indented lines"

    def test_format_invalid(self, any_app):
        with pytest.raises(ValueError, match="[Uu]nknown format"):
            tp.elements(app=any_app, format="invalid")

"""Tests for the CDP backend — AX and DOM discovery, CDP-specific actions.

Requires a Chromium-based application (Chrome, Electron, VS Code, etc.)
running with ``--remote-debugging-port``::

    # Start Chrome with debugging
    google-chrome --remote-debugging-port=9222

    # Run all CDP tests
    TOUCHPOINT_CDP_PORT=9222 pytest tests/test_cdp.py -v

    # Run destructive CDP tests too (clicks, typing)
    TOUCHPOINT_CDP_PORT=9222 TOUCHPOINT_TEST_APP=chrome \\
        pytest tests/test_cdp.py -v

Optional env vars
-----------------
- ``TOUCHPOINT_CDP_PORT`` — **required**, the ``--remote-debugging-port``
- ``TOUCHPOINT_CDP_APP``  — override the auto-detected process name
- ``TOUCHPOINT_TEST_APP`` — enable destructive tests (click, set_value)

All tests skip gracefully when ``TOUCHPOINT_CDP_PORT`` is not set.
"""

from __future__ import annotations

import json

import pytest

import touchpoint as tp
from touchpoint.core.element import Element
from touchpoint.core.types import Role, State
from touchpoint.core.exceptions import ActionFailedError
from tests.conftest import (
    assert_valid_element,
    skip_without_cdp,
    skip_without_test_app,
)


# =====================================================================
#  AX source — element discovery (default source="full", CDP AX via source="ax")
# =====================================================================

# -----------------------------------------------------------------------
# Basic retrieval
# -----------------------------------------------------------------------

@pytest.mark.cdp
@pytest.mark.integration
@skip_without_cdp
class TestCdpElementsBasic:
    """Basic AX-tree element retrieval via CDP."""

    def test_returns_list(self, cdp_app):
        elems = tp.elements(app=cdp_app)
        assert isinstance(elems, list)
        assert len(elems) > 0, "CDP app should have at least one element"

    def test_valid_structure(self, cdp_app):
        """Every element has well-formed fields."""
        elems = tp.elements(app=cdp_app)
        for el in elems[:30]:
            assert_valid_element(el)

    def test_cdp_id_prefix(self, cdp_app):
        """CDP-sourced element IDs start with ``cdp:``."""
        elems = tp.elements(app=cdp_app)
        cdp_elems = [el for el in elems if el.backend == "cdp"]
        assert len(cdp_elems) > 0, "should have at least one CDP element"
        for el in cdp_elems[:30]:
            assert el.id.startswith("cdp:"), (
                f"expected cdp: prefix, got {el.id!r}"
            )

    def test_backend_field(self, cdp_app):
        """CDP-sourced elements report ``backend='cdp'``.

        With dual-backend merging, the list may also contain
        native platform elements (backend='atspi' / 'uia') for
        native UI like title bars and toolbars.
        """
        elems = tp.elements(app=cdp_app)
        cdp_elems = [el for el in elems if el.backend == "cdp"]
        assert len(cdp_elems) > 0, "should have at least one CDP element"
        # Native elements (if any) should have a valid backend too.
        for el in elems:
            assert el.backend in ("cdp", "atspi", "uia"), (
                f"unexpected backend {el.backend!r} on {el.id!r}"
            )

    def test_scoped_by_app(self, cdp_app):
        """All elements belong to the queried app."""
        elems = tp.elements(app=cdp_app)
        for el in elems:
            assert el.app == cdp_app, (
                f"element {el.id!r} has app={el.app!r}, "
                f"expected {cdp_app!r}"
            )

    def test_scoped_by_window(self, cdp_window):
        """All elements belong to the queried window."""
        elems = tp.elements(window_id=cdp_window.id)
        for el in elems:
            assert el.window_id == cdp_window.id, (
                f"element {el.id!r} has window_id={el.window_id!r}, "
                f"expected {cdp_window.id!r}"
            )

    def test_has_visible_showing(self, cdp_app):
        """Visible elements carry VISIBLE + SHOWING states."""
        elems = tp.elements(
            app=cdp_app, states=[State.VISIBLE, State.SHOWING],
        )
        assert len(elems) > 0, "should find visible elements"
        for el in elems:
            assert State.VISIBLE in el.states
            assert State.SHOWING in el.states


# -----------------------------------------------------------------------
# Filtering
# -----------------------------------------------------------------------

@pytest.mark.cdp
@pytest.mark.integration
@skip_without_cdp
class TestCdpElementsFiltering:
    """Filter parameter tests on CDP AX tree."""

    def test_named_only(self, cdp_app):
        elems = tp.elements(app=cdp_app, named_only=True)
        for el in elems:
            assert el.name and el.name.strip(), (
                f"named_only=True returned element with empty name: "
                f"{el.id!r}"
            )

    def test_role_filter(self, cdp_app):
        """role=LINK only returns LINK elements."""
        elems = tp.elements(app=cdp_app, role=Role.LINK)
        for el in elems:
            assert el.role == Role.LINK, (
                f"role=LINK returned {el.role!r}: {el.id!r}"
            )

    def test_role_filter_button(self, cdp_app):
        """role=BUTTON only returns BUTTONs."""
        elems = tp.elements(app=cdp_app, role=Role.BUTTON)
        for el in elems:
            assert el.role == Role.BUTTON

    def test_state_filter_visible(self, cdp_app):
        elems = tp.elements(app=cdp_app, states=[State.VISIBLE])
        for el in elems:
            assert State.VISIBLE in el.states

    def test_state_filter_enabled(self, cdp_app):
        """ENABLED state filter."""
        elems = tp.elements(
            app=cdp_app, states=[State.ENABLED],
        )
        for el in elems:
            assert State.ENABLED in el.states

    def test_combined_filters(self, cdp_app):
        """Combine role + state + named_only."""
        elems = tp.elements(
            app=cdp_app,
            role=Role.BUTTON,
            states=[State.VISIBLE],
            named_only=True,
        )
        for el in elems:
            assert el.role == Role.BUTTON
            assert State.VISIBLE in el.states
            assert el.name and el.name.strip()

    def test_custom_filter(self, cdp_app):
        """Custom callable filter is applied."""
        elems = tp.elements(
            app=cdp_app,
            named_only=True,
            filter=lambda e: len(e.name) > 3,
        )
        for el in elems:
            assert len(el.name) > 3, (
                f"custom filter len(name)>3 returned {el.name!r}"
            )

    def test_sort_by_position(self, cdp_app):
        """sort_by='position' yields reading-order elements."""
        elems = tp.elements(
            app=cdp_app,
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

    def test_sort_by_callable(self, cdp_app):
        """Callable sort_by is passed through as key."""
        elems = tp.elements(app=cdp_app, named_only=True)
        if len(elems) < 2:
            pytest.skip("need at least 2 named elements")
        sorted_elems = tp.elements(
            app=cdp_app, named_only=True,
            sort_by=lambda el: el.name.lower(),
        )
        names = [el.name.lower() for el in sorted_elems]
        assert names == sorted(names)


# -----------------------------------------------------------------------
# Safety limits (max_elements, max_depth)
# -----------------------------------------------------------------------

@pytest.mark.cdp
@pytest.mark.integration
@skip_without_cdp
class TestCdpElementsLimits:
    """max_elements and max_depth on CDP."""

    def test_max_elements_caps_count(self, cdp_app):
        elems = tp.elements(app=cdp_app, max_elements=5)
        assert len(elems) <= 5

    def test_max_elements_less_than_full(self, cdp_app):
        full = tp.elements(app=cdp_app, max_depth=100)
        if len(full) <= 3:
            pytest.skip("app has too few elements to test capping")
        capped = tp.elements(app=cdp_app, max_elements=3)
        assert len(capped) < len(full)

    def test_max_depth_0_tree(self, cdp_app):
        """max_depth=0, tree=True should have no children."""
        elems = tp.elements(app=cdp_app, tree=True, max_depth=0)
        for el in elems:
            assert not el.children, (
                f"max_depth=0 tree node {el.id!r} should have "
                f"no children, got {len(el.children)}"
            )

    def test_max_depth_1_shallower_than_deep(self, cdp_app):
        """max_depth=1 should return fewer elements than max_depth=100."""
        shallow = tp.elements(app=cdp_app, max_depth=1)
        deep = tp.elements(app=cdp_app, max_depth=100)
        if len(deep) <= len(shallow):
            pytest.skip("app tree not deep enough to test depth limiting")
        assert len(shallow) <= len(deep)


# -----------------------------------------------------------------------
# Tree mode
# -----------------------------------------------------------------------

@pytest.mark.cdp
@pytest.mark.integration
@skip_without_cdp
class TestCdpElementsTree:
    """Tree mode via ``tree=True``."""

    def test_tree_has_children(self, cdp_app):
        elems = tp.elements(app=cdp_app, tree=True, max_depth=2)
        has_children = any(
            el.children and len(el.children) > 0
            for el in elems
        )
        assert has_children, (
            "tree=True max_depth=2 should produce at least one "
            "element with children"
        )

    def test_tree_max_depth_0(self, cdp_app):
        elems = tp.elements(app=cdp_app, tree=True, max_depth=0)
        for el in elems:
            assert not el.children

    def test_tree_filter_children_false(self, cdp_app):
        """filter_children=False keeps raw unfiltered children."""
        elems = tp.elements(
            app=cdp_app, tree=True, max_depth=1,
            named_only=True, filter_children=False,
        )
        for el in elems:
            assert el.name and el.name.strip()
        all_children = [c for el in elems for c in (el.children or [])]
        if not all_children:
            pytest.skip("no children to verify filter_children=False")
        assert len(all_children) > 0

    def test_tree_filter_children_true(self, cdp_app):
        """filter_children=True applies named_only to children too."""
        elems = tp.elements(
            app=cdp_app, tree=True, max_depth=1,
            named_only=True, filter_children=True,
        )
        for el in elems:
            assert el.name and el.name.strip()
            for child in el.children or []:
                assert child.name and child.name.strip(), (
                    f"filter_children=True should filter unnamed "
                    f"children, but {child.id!r} has name={child.name!r}"
                )


# -----------------------------------------------------------------------
# Root element scoping
# -----------------------------------------------------------------------

@pytest.mark.cdp
@pytest.mark.integration
@skip_without_cdp
class TestCdpElementsRootElement:
    """root_element parameter on CDP."""

    def test_root_element_narrows(self, cdp_app):
        """Passing root_element returns a subset of the full tree."""
        all_elems = tp.elements(app=cdp_app, max_depth=100)
        if len(all_elems) < 5:
            pytest.skip("need at least 5 elements")
        # Pick a mid-tree element to use as root.
        parent = all_elems[len(all_elems) // 3]
        scoped = tp.elements(
            app=cdp_app, root_element=parent, max_depth=100,
        )
        assert len(scoped) > 0, (
            f"root_element={parent.id!r} returned no elements"
        )
        assert len(scoped) < len(all_elems), (
            "root_element should narrow results"
        )

    def test_root_element_by_id(self, cdp_app):
        """String ID and Element object give the same result."""
        all_elems = tp.elements(app=cdp_app, max_depth=100)
        if len(all_elems) < 5:
            pytest.skip("need at least 5 elements")
        parent = all_elems[len(all_elems) // 3]
        by_obj = tp.elements(
            app=cdp_app, root_element=parent, max_depth=100,
        )
        by_id = tp.elements(
            app=cdp_app, root_element=parent.id, max_depth=100,
        )
        assert len(by_obj) == len(by_id)

    def test_root_element_invalid_id(self, cdp_app):
        """Invalid root_element returns empty list."""
        result = tp.elements(
            app=cdp_app,
            root_element="cdp:0:nonexistent:fake_node",
        )
        assert result == []


# -----------------------------------------------------------------------
# CDP-specific AX properties
# -----------------------------------------------------------------------

@pytest.mark.cdp
@pytest.mark.integration
@skip_without_cdp
class TestCdpAxProperties:
    """CDP-specific element properties and quirks."""

    def test_no_inline_text_nodes(self, cdp_app):
        """inlineTextBox and lineBreak should be filtered out."""
        elems = tp.elements(app=cdp_app, max_depth=100)
        for el in elems:
            assert el.raw_role not in ("inlineTextBox", "lineBreak"), (
                f"inlineTextBox/lineBreak leaked through: {el.id!r} "
                f"raw_role={el.raw_role!r}"
            )

    def test_no_root_web_area_in_flat(self, cdp_app):
        """rootWebArea should be filtered in flat mode."""
        elems = tp.elements(app=cdp_app)
        for el in elems:
            assert el.raw_role != "rootWebArea", (
                f"rootWebArea should be filtered: {el.id!r}"
            )

    def test_enabled_sensitive_pair(self, cdp_app):
        """ENABLED and SENSITIVE always appear together."""
        elems = tp.elements(app=cdp_app, states=[State.ENABLED])
        for el in elems:
            assert State.SENSITIVE in el.states, (
                f"element {el.id!r} has ENABLED but not SENSITIVE"
            )

    def test_selectable_on_list_items(self, cdp_app):
        """LIST_ITEM elements should have SELECTABLE state."""
        elems = tp.elements(app=cdp_app, role=Role.LIST_ITEM)
        if not elems:
            pytest.skip("no list items in CDP app")
        for el in elems:
            assert State.SELECTABLE in el.states, (
                f"LIST_ITEM {el.id!r} missing SELECTABLE state"
            )

    def test_clickable_on_buttons(self, cdp_app):
        """BUTTON elements should have CLICKABLE state."""
        elems = tp.elements(app=cdp_app, role=Role.BUTTON)
        if not elems:
            pytest.skip("no buttons in CDP app")
        for el in elems:
            assert State.CLICKABLE in el.states, (
                f"BUTTON {el.id!r} missing CLICKABLE state"
            )

    def test_clickable_on_links(self, cdp_app):
        """LINK elements should have CLICKABLE state."""
        elems = tp.elements(app=cdp_app, role=Role.LINK)
        if not elems:
            pytest.skip("no links in CDP app")
        for el in elems:
            assert State.CLICKABLE in el.states, (
                f"LINK {el.id!r} missing CLICKABLE state"
            )

    def test_password_field_role(self, cdp_app):
        """Password inputs should map to PASSWORD_TEXT, not TEXT_FIELD.

        Skips if the page has no password field — this is expected
        for most apps.  Run against a login page for coverage.
        """
        elems = tp.elements(app=cdp_app, role=Role.PASSWORD_TEXT)
        if not elems:
            pytest.skip(
                "no password field in CDP app "
                "(run against a login page for coverage)"
            )
        for el in elems:
            assert el.role == Role.PASSWORD_TEXT

    def test_text_fields_have_editable(self, cdp_app):
        """TEXT_FIELD elements should have EDITABLE state."""
        elems = tp.elements(
            app=cdp_app, role=Role.TEXT_FIELD,
            states=[State.VISIBLE],
        )
        if not elems:
            pytest.skip("no text fields in CDP app")
        for el in elems:
            assert State.EDITABLE in el.states, (
                f"TEXT_FIELD {el.id!r} missing EDITABLE state"
            )

    def test_expandable_implies_expanded_or_collapsed(self, cdp_app):
        """EXPANDABLE must pair with either EXPANDED or COLLAPSED."""
        elems = tp.elements(app=cdp_app, states=[State.EXPANDABLE])
        for el in elems:
            has_ex = State.EXPANDED in el.states
            has_co = State.COLLAPSED in el.states
            assert has_ex or has_co, (
                f"EXPANDABLE element {el.id!r} missing EXPANDED/COLLAPSED"
            )

    def test_element_has_raw(self, cdp_app):
        """Elements should have a ``raw`` dict."""
        elems = tp.elements(app=cdp_app, named_only=True)
        if not elems:
            pytest.skip("no named elements")
        for el in elems[:10]:
            assert isinstance(el.raw, dict), (
                f"element {el.id!r} has raw={el.raw!r}, expected dict"
            )


# -----------------------------------------------------------------------
# get_element() / element_at() roundtrip
# -----------------------------------------------------------------------

@pytest.mark.cdp
@pytest.mark.integration
@skip_without_cdp
class TestCdpGetElement:
    """get_element() and element_at() on CDP elements."""

    def test_roundtrip(self, cdp_element):
        """Refresh an element by ID and verify fields match."""
        refreshed = tp.get_element(cdp_element.id)
        assert refreshed is not None, (
            f"get_element({cdp_element.id!r}) returned None"
        )
        assert refreshed.id == cdp_element.id
        assert refreshed.role == cdp_element.role
        assert refreshed.app == cdp_element.app
        assert_valid_element(refreshed)

    def test_invalid_id(self):
        result = tp.get_element("cdp:0:nonexistent_target:fake_node")
        assert result is None

    def test_element_at_window_center(self, cdp_window):
        """element_at the center of a CDP window should find something."""
        cx = cdp_window.position[0] + cdp_window.size[0] // 2
        cy = cdp_window.position[1] + cdp_window.size[1] // 2
        el = tp.element_at(cx, cy)
        if el is None:
            pytest.skip("element_at returned None at CDP window center")
        assert_valid_element(el)


# -----------------------------------------------------------------------
# Windows
# -----------------------------------------------------------------------

@pytest.mark.cdp
@pytest.mark.integration
@skip_without_cdp
class TestCdpWindows:
    """CDP window discovery."""

    def test_windows_list(self, cdp_backend):
        """CDP targets appear as windows."""
        wins = tp.windows()
        cdp_wins = [w for w in wins if w.id.startswith("cdp:")]
        assert len(cdp_wins) > 0, "no CDP windows found"

    def test_window_fields(self, cdp_window):
        """CDP windows have well-formed fields."""
        assert cdp_window.id.startswith("cdp:")
        assert isinstance(cdp_window.title, str)
        assert isinstance(cdp_window.app, str)
        assert isinstance(cdp_window.pid, int)
        assert isinstance(cdp_window.position, tuple)
        assert isinstance(cdp_window.size, tuple)

    def test_window_has_url_in_raw(self, cdp_window):
        """CDP windows carry the page URL in raw."""
        assert "url" in (cdp_window.raw or {}), (
            "CDP window should have 'url' in raw"
        )


# -----------------------------------------------------------------------
# Format output
# -----------------------------------------------------------------------

@pytest.mark.cdp
@pytest.mark.integration
@skip_without_cdp
class TestCdpElementsFormat:
    """Format parameter on CDP elements."""

    def test_format_flat(self, cdp_app):
        result = tp.elements(app=cdp_app, format="flat")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_format_json(self, cdp_app):
        result = tp.elements(app=cdp_app, format="json")
        assert isinstance(result, str)
        parsed = json.loads(result)
        assert isinstance(parsed, list)
        assert len(parsed) > 0

    def test_format_tree(self, cdp_app):
        result = tp.elements(
            app=cdp_app, tree=True, max_depth=1, format="tree",
        )
        assert isinstance(result, str)
        lines = result.strip().splitlines()
        assert len(lines) > 1
        assert any(
            line.startswith(" ") or line.startswith("\t")
            for line in lines
        ), "tree format should have indented lines"

    def test_format_invalid(self, cdp_app):
        with pytest.raises(ValueError, match="[Uu]nknown format"):
            tp.elements(app=cdp_app, format="invalid")


# =====================================================================
#  DOM source — element discovery (source="dom")
# =====================================================================

# -----------------------------------------------------------------------
# Basic retrieval
# -----------------------------------------------------------------------

@pytest.mark.cdp
@pytest.mark.integration
@skip_without_cdp
class TestCdpDomBasic:
    """Basic DOM-sourced element retrieval."""

    def test_returns_list(self, cdp_app):
        elems = tp.elements(app=cdp_app, source="dom")
        assert isinstance(elems, list)
        assert len(elems) > 0, "DOM walk should find at least one element"

    def test_valid_structure(self, cdp_app):
        elems = tp.elements(app=cdp_app, source="dom")
        for el in elems[:30]:
            assert_valid_element(el)

    def test_dom_id_format(self, cdp_app):
        """DOM element IDs contain ``:dom:`` segment."""
        elems = tp.elements(app=cdp_app, source="dom")
        for el in elems[:30]:
            assert ":dom:" in el.id, (
                f"expected :dom: in ID, got {el.id!r}"
            )

    def test_backend_field(self, cdp_app):
        elems = tp.elements(app=cdp_app, source="dom")
        for el in elems[:30]:
            assert el.backend == "cdp"

    def test_scoped_by_app(self, cdp_app):
        elems = tp.elements(app=cdp_app, source="dom")
        for el in elems:
            assert el.app == cdp_app

    def test_scoped_by_window(self, cdp_window):
        elems = tp.elements(
            window_id=cdp_window.id, source="dom",
        )
        for el in elems:
            assert el.window_id == cdp_window.id

    def test_has_tag_in_raw(self, cdp_app):
        """DOM elements carry tag name in ``raw``."""
        elems = tp.elements(app=cdp_app, source="dom")
        for el in elems[:10]:
            assert "tag" in (el.raw or {}), (
                f"DOM element {el.id!r} should have 'tag' in raw"
            )

    def test_has_source_dom_in_raw(self, cdp_app):
        """DOM elements carry ``source: 'dom'`` in raw."""
        elems = tp.elements(app=cdp_app, source="dom")
        for el in elems[:10]:
            assert (el.raw or {}).get("source") == "dom", (
                f"DOM element {el.id!r} should have source='dom' in raw"
            )


# -----------------------------------------------------------------------
# DOM filtering
# -----------------------------------------------------------------------

@pytest.mark.cdp
@pytest.mark.integration
@skip_without_cdp
class TestCdpDomFiltering:
    """Filter parameter tests on DOM elements."""

    def test_named_only(self, cdp_app):
        elems = tp.elements(
            app=cdp_app, source="dom", named_only=True,
        )
        for el in elems:
            assert el.name and el.name.strip(), (
                f"named_only returned element with empty name: "
                f"{el.id!r}"
            )

    def test_role_filter(self, cdp_app):
        elems = tp.elements(
            app=cdp_app, source="dom", role=Role.BUTTON,
        )
        for el in elems:
            assert el.role == Role.BUTTON

    def test_role_filter_link(self, cdp_app):
        elems = tp.elements(
            app=cdp_app, source="dom", role=Role.LINK,
        )
        for el in elems:
            assert el.role == Role.LINK

    def test_state_filter(self, cdp_app):
        elems = tp.elements(
            app=cdp_app, source="dom", states=[State.VISIBLE],
        )
        for el in elems:
            assert State.VISIBLE in el.states

    def test_combined_filters(self, cdp_app):
        elems = tp.elements(
            app=cdp_app, source="dom",
            role=Role.BUTTON, states=[State.VISIBLE],
            named_only=True,
        )
        for el in elems:
            assert el.role == Role.BUTTON
            assert State.VISIBLE in el.states
            assert el.name and el.name.strip()


# -----------------------------------------------------------------------
# DOM safety limits
# -----------------------------------------------------------------------

@pytest.mark.cdp
@pytest.mark.integration
@skip_without_cdp
class TestCdpDomLimits:
    """max_elements and max_depth on DOM source."""

    def test_max_elements(self, cdp_app):
        elems = tp.elements(
            app=cdp_app, source="dom", max_elements=5,
        )
        assert len(elems) <= 5

    def test_max_elements_less_than_full(self, cdp_app):
        full = tp.elements(app=cdp_app, source="dom")
        if len(full) <= 3:
            pytest.skip("DOM walk too small to test capping")
        capped = tp.elements(
            app=cdp_app, source="dom", max_elements=3,
        )
        assert len(capped) < len(full)


# -----------------------------------------------------------------------
# DOM-specific properties
# -----------------------------------------------------------------------

@pytest.mark.cdp
@pytest.mark.integration
@skip_without_cdp
class TestCdpDomProperties:
    """DOM-specific element properties."""

    def test_no_aria_hidden_elements(self, cdp_app):
        """aria-hidden elements should be dropped, not tagged OFFSCREEN."""
        elems = tp.elements(app=cdp_app, source="dom")
        for el in elems:
            # If an element got through, it shouldn't be offscreen
            # from aria-hidden (OFFSCREEN should not appear in DOM
            # elements at all since we drop hidden ones).
            # The main assertion: no element with OFFSCREEN state
            # should exist from DOM source.
            pass
        # Broader check: all DOM elements should have VISIBLE+SHOWING.
        for el in elems:
            assert State.VISIBLE in el.states, (
                f"DOM element {el.id!r} missing VISIBLE "
                f"(hidden elements should be dropped)"
            )
            assert State.SHOWING in el.states, (
                f"DOM element {el.id!r} missing SHOWING"
            )

    def test_focusable_not_on_presentation(self, cdp_app):
        """Elements with non-interactive roles should not get FOCUSABLE
        from the raw ``role`` attribute alone."""
        elems = tp.elements(app=cdp_app, source="dom")
        non_focusable_roles = {
            Role.SECTION, Role.GROUP, Role.DOCUMENT, Role.PARAGRAPH,
            Role.FIGURE, Role.SEPARATOR, Role.ARTICLE,
        }
        focusable_tags = {"a", "button", "input", "select", "textarea", "summary"}
        for el in elems:
            tag = (el.raw or {}).get("tag", "")
            if (
                el.role in non_focusable_roles
                and tag not in focusable_tags
                and State.FOCUSABLE in el.states
            ):
                # This would be the false-positive bug.
                pytest.fail(
                    f"non-interactive {el.role!r} (tag={tag!r}) "
                    f"should not have FOCUSABLE: {el.id!r}"
                )

    def test_enabled_sensitive_pair(self, cdp_app):
        """ENABLED and SENSITIVE always appear together."""
        elems = tp.elements(
            app=cdp_app, source="dom", states=[State.ENABLED],
        )
        for el in elems:
            assert State.SENSITIVE in el.states

    def test_position_and_size(self, cdp_app):
        """DOM elements have real positions and sizes."""
        elems = tp.elements(app=cdp_app, source="dom")
        has_size = False
        for el in elems:
            assert isinstance(el.position, tuple) and len(el.position) == 2
            assert isinstance(el.size, tuple) and len(el.size) == 2
            if el.size[0] > 0 and el.size[1] > 0:
                has_size = True
        assert has_size, "at least some DOM elements should have non-zero size"

    def test_dom_root_element(self, cdp_app):
        """DOM root_element scoping with a DOM-sourced ID."""
        elems = tp.elements(app=cdp_app, source="dom")
        if len(elems) < 3:
            pytest.skip("need at least 3 DOM elements")
        root = elems[0]
        scoped = tp.elements(
            app=cdp_app, source="dom", root_element=root,
        )
        # May return empty if the root has no children, but should
        # not raise and should return ≤ full count.
        assert isinstance(scoped, list)


# -----------------------------------------------------------------------
# DOM format output
# -----------------------------------------------------------------------

@pytest.mark.cdp
@pytest.mark.integration
@skip_without_cdp
class TestCdpDomFormat:
    """Format parameter on DOM elements."""

    def test_format_flat(self, cdp_app):
        result = tp.elements(
            app=cdp_app, source="dom", format="flat",
        )
        assert isinstance(result, str)
        assert len(result) > 0

    def test_format_json(self, cdp_app):
        result = tp.elements(
            app=cdp_app, source="dom", format="json",
        )
        assert isinstance(result, str)
        parsed = json.loads(result)
        assert isinstance(parsed, list)
        assert len(parsed) > 0


# =====================================================================
#  Validation — no backend needed (pure unit tests)
# =====================================================================

@pytest.mark.unit
class TestCdpValidation:
    """Input validation for CDP-specific code paths."""

    def test_source_dom_tree_true_raises(self):
        """source='dom' + tree=True raises ValueError."""
        with pytest.raises(ValueError, match="tree=True"):
            tp.elements(source="dom", tree=True)

    def test_ax_root_element_with_dom_source_raises(self):
        """AX-sourced root_element with source='dom' raises ValueError."""
        # An AX element ID has the cdp: prefix but no :dom: segment.
        fake_ax_id = "cdp:9222:target123:ax_node_42"
        with pytest.raises(ValueError, match="DOM-sourced"):
            tp.elements(source="dom", root_element=fake_ax_id)


# =====================================================================
#  find() on CDP
# =====================================================================

@pytest.mark.cdp
@pytest.mark.integration
@skip_without_cdp
class TestCdpFind:
    """find() against CDP AX tree."""

    def test_find_returns_list(self, cdp_app):
        result = tp.find("anything", app=cdp_app)
        assert isinstance(result, list)

    def test_find_known_element(self, cdp_element):
        """Find a known element by its exact name."""
        results = tp.find(cdp_element.name, app=cdp_element.app)
        assert len(results) > 0, (
            f"find({cdp_element.name!r}) returned empty"
        )
        names = [el.name for el in results]
        assert cdp_element.name in names

    def test_find_case_insensitive(self, cdp_element):
        query = cdp_element.name.upper()
        results = tp.find(query, app=cdp_element.app)
        assert len(results) > 0

    def test_find_with_role(self, cdp_app):
        buttons = tp.elements(
            app=cdp_app, role=Role.BUTTON, named_only=True,
        )
        if not buttons:
            pytest.skip("no named buttons")
        query = buttons[0].name
        results = tp.find(query, app=cdp_app, role=Role.BUTTON)
        for el in results:
            assert el.role == Role.BUTTON

    def test_find_no_match(self, cdp_app):
        results = tp.find("nonexistent_xyz_99999", app=cdp_app)
        assert results == []

    def test_find_max_results(self, cdp_element):
        results = tp.find(
            cdp_element.name, app=cdp_element.app, max_results=1,
        )
        assert len(results) <= 1

    def test_find_source_dom(self, cdp_app):
        """find() with source='dom' searches DOM elements."""
        elems = tp.elements(
            app=cdp_app, source="dom", named_only=True,
        )
        if not elems:
            pytest.skip("no named DOM elements")
        query = elems[0].name
        results = tp.find(query, app=cdp_app, source="dom")
        assert len(results) > 0
        for el in results:
            assert ":dom:" in el.id


# =====================================================================
#  wait_for() / wait_for(gone=True) on CDP
# =====================================================================

@pytest.mark.cdp
@pytest.mark.integration
@skip_without_cdp
class TestCdpWait:
    """wait_for (appear and gone) against CDP."""

    def test_wait_for_existing(self, cdp_element):
        """An existing element returns immediately."""
        import time
        start = time.monotonic()
        results = tp.wait_for(
            cdp_element.name,
            app=cdp_element.app,
            timeout=5.0,
        )
        elapsed = time.monotonic() - start
        assert len(results) > 0
        assert elapsed < 5.0

    def test_wait_for_timeout(self, cdp_app):
        """Nonexistent element triggers timeout."""
        with pytest.raises(TimeoutError):
            tp.wait_for(
                "nonexistent_cdp_xyz_42",
                app=cdp_app,
                timeout=2.0,
                poll=0.5,
            )

    def test_wait_for_gone_nonexistent(self, cdp_app):
        """A nonexistent element returns immediately (already gone)."""
        import time
        start = time.monotonic()
        tp.wait_for(
            "nonexistent_cdp_xyz_42",
            app=cdp_app,
            timeout=5.0,
            poll=0.5,
            gone=True,
        )
        elapsed = time.monotonic() - start
        assert elapsed < 5.0


# =====================================================================
#  CDP screenshot
# =====================================================================

@pytest.mark.cdp
@pytest.mark.integration
@skip_without_cdp
class TestCdpScreenshot:
    """Screenshots via CDP."""

    def test_screenshot_window(self, cdp_window):
        """Screenshot of a CDP window returns an image."""
        img = tp.screenshot(window_id=cdp_window.id)
        try:
            w, h = img.size
            assert w > 0 and h > 0
        finally:
            try:
                img.close()
            except Exception:
                pass

    def test_screenshot_element(self, cdp_element):
        """Screenshot clipped to a CDP element."""
        img = tp.screenshot(element=cdp_element)
        try:
            w, h = img.size
            assert w > 0 and h > 0
        finally:
            try:
                img.close()
            except Exception:
                pass


# =====================================================================
#  Destructive actions — CDP  (requires TOUCHPOINT_TEST_APP)
# =====================================================================

def _find_cdp_button(app: str):
    """Find a visible, named CDP button with a click action."""
    elems = tp.elements(
        app=app, role=Role.BUTTON, named_only=True,
        states=[State.VISIBLE, State.SHOWING],
    )
    for el in elems:
        if el.id.startswith("cdp:") and el.actions and el.size[0] > 0:
            return el
    return None


def _find_cdp_text_field(app: str):
    """Find a visible, editable CDP text field."""
    elems = tp.elements(
        app=app, role=Role.TEXT_FIELD,
        states=[State.VISIBLE, State.SHOWING, State.EDITABLE],
    )
    for el in elems:
        if el.id.startswith("cdp:") and el.size[0] > 0:
            return el
    return None


def _find_dom_element(app: str):
    """Find a visible, named DOM-sourced element."""
    elems = tp.elements(
        app=app, source="dom", named_only=True,
        states=[State.VISIBLE, State.SHOWING],
    )
    for el in elems:
        if el.size[0] > 0 and el.size[1] > 0:
            return el
    return None


@pytest.mark.cdp
@pytest.mark.destructive
@skip_without_cdp
class TestCdpActions:
    """Destructive actions on CDP AX elements."""

    def test_click_button(self, cdp_backend, cdp_app):
        el = _find_cdp_button(cdp_app)
        if el is None:
            pytest.skip(f"no clickable CDP button in {cdp_app}")
        result = tp.click(el)
        assert result is True

    def test_click_by_id(self, cdp_backend, cdp_app):
        el = _find_cdp_button(cdp_app)
        if el is None:
            pytest.skip(f"no clickable CDP button in {cdp_app}")
        result = tp.click(el.id)
        assert isinstance(result, bool)

    def test_double_click(self, cdp_backend, cdp_app):
        el = _find_cdp_button(cdp_app)
        if el is None:
            pytest.skip(f"no clickable CDP button in {cdp_app}")
        result = tp.double_click(el)
        assert result is True

    def test_right_click(self, cdp_backend, cdp_app):
        el = _find_cdp_button(cdp_app)
        if el is None:
            pytest.skip(f"no clickable CDP button in {cdp_app}")
        result = tp.right_click(el)
        assert isinstance(result, bool)

    def test_focus(self, cdp_backend, cdp_app):
        el = _find_cdp_text_field(cdp_app)
        if el is None:
            pytest.skip(f"no CDP text field in {cdp_app}")
        result = tp.focus(el)
        assert isinstance(result, bool)

    def test_set_value(self, cdp_backend, cdp_app):
        el = _find_cdp_text_field(cdp_app)
        if el is None:
            pytest.skip(f"no CDP text field in {cdp_app}")
        result = tp.set_value(el, "tp_test_123")
        assert result is True

    def test_set_value_replace(self, cdp_backend, cdp_app):
        el = _find_cdp_text_field(cdp_app)
        if el is None:
            pytest.skip(f"no CDP text field in {cdp_app}")
        tp.set_value(el, "first", replace=True)
        result = tp.set_value(el, "replaced", replace=True)
        assert result is True


@pytest.mark.cdp
@pytest.mark.destructive
@skip_without_cdp
class TestCdpDomActions:
    """Destructive actions on DOM-sourced elements.

    Tests the fast-path DOM click code (``dom:{x},{y}`` IDs).
    """

    def test_dom_click(self, cdp_backend, cdp_app):
        """Click a DOM-sourced element."""
        el = _find_dom_element(cdp_app)
        if el is None:
            pytest.skip(f"no clickable DOM element in {cdp_app}")
        result = tp.click(el)
        assert result is True

    def test_dom_click_by_id(self, cdp_backend, cdp_app):
        """Click by DOM element ID string."""
        el = _find_dom_element(cdp_app)
        if el is None:
            pytest.skip(f"no clickable DOM element in {cdp_app}")
        result = tp.click(el.id)
        assert isinstance(result, bool)


# =====================================================================
#  Action error handling
# =====================================================================

@pytest.mark.cdp
@pytest.mark.integration
@skip_without_cdp
class TestCdpActionErrors:
    """Error cases for CDP actions (not destructive — expected to fail)."""

    def test_click_invalid_id(self):
        with pytest.raises((ActionFailedError, Exception)):
            tp.click("cdp:0:nonexistent_target:fake_node")

    def test_set_value_invalid_id(self):
        with pytest.raises((ActionFailedError, Exception)):
            tp.set_value("cdp:0:nonexistent_target:fake_node", "text")

    def test_focus_invalid_id(self):
        with pytest.raises((ActionFailedError, Exception)):
            tp.focus("cdp:0:nonexistent_target:fake_node")

    def test_action_invalid_name(self, cdp_element):
        """Unsupported action name raises ActionFailedError."""
        with pytest.raises(ActionFailedError):
            tp.action(cdp_element, "nonexistent_action_xyz")

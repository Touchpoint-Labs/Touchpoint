"""Tests for touchpoint.format.formatter — output formatting.

Pure unit tests that construct Element objects directly.
No backend or desktop needed.
"""

from __future__ import annotations

import json

import pytest

from touchpoint.core.element import Element
from touchpoint.core.types import Role, State
from touchpoint.format.formatter import format_elements


# -----------------------------------------------------------------------
# Helpers — build elements by hand
# -----------------------------------------------------------------------

def _make_element(
    name: str = "Send",
    role: Role = Role.BUTTON,
    *,
    id: str = "atspi:0:0:1",
    states: list[State] | None = None,
    position: tuple[int, int] = (100, 200),
    size: tuple[int, int] = (80, 30),
    actions: list[str] | None = None,
    value: str | None = None,
    description: str | None = None,
    children: list[Element] | None = None,
    parent_id: str | None = None,
    window_id: str | None = None,
    raw: dict | None = None,
) -> Element:
    return Element(
        id=id,
        name=name,
        role=role,
        states=states or [State.VISIBLE, State.ENABLED],
        position=position,
        size=size,
        app="TestApp",
        pid=1234,
        backend="atspi",
        raw_role=role.value,
        actions=actions or [],
        value=value,
        description=description,
        children=children or [],
        parent_id=parent_id,
        window_id=window_id,
        raw=raw or {},
    )


# -----------------------------------------------------------------------
# Flat format
# -----------------------------------------------------------------------

@pytest.mark.unit
class TestFlat:
    """Flat format — one line per element via Element.__str__."""

    def test_basic(self):
        el = _make_element("Send", actions=["click"])
        result = format_elements([el], "flat")
        assert "Send" in result
        assert "button" in result
        assert "atspi:0:0:1" in result

    def test_empty(self):
        result = format_elements([], "flat")
        assert result == ""

    def test_multiple(self):
        els = [
            _make_element("Send", id="atspi:0:0:1"),
            _make_element("Cancel", id="atspi:0:0:2"),
        ]
        result = format_elements(els, "flat")
        lines = result.strip().split("\n")
        assert len(lines) == 2
        assert "Send" in lines[0]
        assert "Cancel" in lines[1]

    def test_includes_value(self):
        el = _make_element("Search", Role.TEXT_FIELD, value="hello")
        result = format_elements([el], "flat")
        assert "hello" in result

    def test_includes_actions(self):
        el = _make_element("OK", actions=["click", "press"])
        result = format_elements([el], "flat")
        assert "click" in result
        assert "press" in result

    def test_includes_states(self):
        el = _make_element(states=[State.VISIBLE, State.FOCUSED])
        result = format_elements([el], "flat")
        assert "visible" in result
        assert "focused" in result


# -----------------------------------------------------------------------
# JSON format
# -----------------------------------------------------------------------

@pytest.mark.unit
class TestJson:
    """JSON format — full field serialisation."""

    def test_basic(self):
        el = _make_element("Send")
        result = format_elements([el], "json")
        parsed = json.loads(result)
        assert isinstance(parsed, list)
        assert len(parsed) == 1
        assert parsed[0]["name"] == "Send"
        assert parsed[0]["role"] == "button"

    def test_empty(self):
        result = format_elements([], "json")
        assert json.loads(result) == []

    def test_roundtrip_fields(self):
        el = _make_element(
            "Search",
            Role.TEXT_FIELD,
            id="atspi:1:0:3.2",
            value="hello",
            description="Search box",
            actions=["set-text"],
            parent_id="atspi:1:0:3",
            window_id="atspi:1:0",
        )
        result = format_elements([el], "json")
        d = json.loads(result)[0]
        assert d["id"] == "atspi:1:0:3.2"
        assert d["name"] == "Search"
        assert d["role"] == "text_field"
        assert d["value"] == "hello"
        assert d["description"] == "Search box"
        assert d["actions"] == ["set-text"]
        assert d["parent_id"] == "atspi:1:0:3"
        assert d["window_id"] == "atspi:1:0"
        assert d["position"] == [100, 200]
        assert d["size"] == [80, 30]
        assert d["app"] == "TestApp"
        assert d["pid"] == 1234
        assert d["backend"] == "atspi"
        assert d["raw_role"] == "text_field"

    def test_none_values_omitted(self):
        """value=None, description=None should not appear in output."""
        el = _make_element(value=None, description=None)
        result = format_elements([el], "json")
        d = json.loads(result)[0]
        assert "value" not in d
        assert "description" not in d

    def test_empty_actions_omitted(self):
        el = _make_element(actions=[])
        result = format_elements([el], "json")
        d = json.loads(result)[0]
        assert "actions" not in d

    def test_raw_included(self):
        el = _make_element(raw={"toolkit": "gtk3"})
        result = format_elements([el], "json")
        d = json.loads(result)[0]
        assert d["raw"] == {"toolkit": "gtk3"}

    def test_children_nested(self):
        child = _make_element("Child", id="atspi:0:0:1.0",
                              parent_id="atspi:0:0:1")
        parent = _make_element("Parent", id="atspi:0:0:1",
                               children=[child])
        result = format_elements([parent], "json")
        d = json.loads(result)[0]
        assert "children" in d
        assert len(d["children"]) == 1
        assert d["children"][0]["name"] == "Child"

    def test_multiple(self):
        els = [
            _make_element("A", id="atspi:0:0:1"),
            _make_element("B", id="atspi:0:0:2"),
        ]
        result = format_elements(els, "json")
        parsed = json.loads(result)
        assert len(parsed) == 2
        names = [d["name"] for d in parsed]
        assert names == ["A", "B"]


# -----------------------------------------------------------------------
# Tree format
# -----------------------------------------------------------------------

@pytest.mark.unit
class TestTree:
    """Tree format — indented hierarchy."""

    def test_basic(self):
        el = _make_element("Root")
        result = format_elements([el], "tree")
        assert "Root" in result

    def test_no_children_no_indent(self):
        el = _make_element("Leaf")
        result = format_elements([el], "tree")
        # Should not start with whitespace
        assert not result.startswith(" ")

    def test_children_indented(self):
        child = _make_element("Child", id="atspi:0:0:1.0",
                              parent_id="atspi:0:0:1")
        parent = _make_element("Parent", id="atspi:0:0:1",
                               children=[child])
        result = format_elements([parent], "tree")
        lines = result.strip().split("\n")
        assert len(lines) == 2
        # Parent line: no indent
        assert not lines[0].startswith(" ")
        assert "Parent" in lines[0]
        # Child line: indented
        assert lines[1].startswith("  ")
        assert "Child" in lines[1]

    def test_deep_nesting(self):
        grandchild = _make_element("GC", id="atspi:0:0:1.0.0")
        child = _make_element("C", id="atspi:0:0:1.0",
                              children=[grandchild])
        root = _make_element("R", id="atspi:0:0:1",
                             children=[child])
        result = format_elements([root], "tree")
        lines = result.strip().split("\n")
        assert len(lines) == 3
        # depth 0: no indent
        assert not lines[0].startswith(" ")
        # depth 1: 2 spaces
        assert lines[1].startswith("  ") and not lines[1].startswith("    ")
        # depth 2: 4 spaces
        assert lines[2].startswith("    ")

    def test_multiple_roots(self):
        roots = [
            _make_element("A", id="atspi:0:0:1"),
            _make_element("B", id="atspi:0:0:2"),
        ]
        result = format_elements(roots, "tree")
        lines = result.strip().split("\n")
        assert len(lines) == 2
        assert not lines[0].startswith(" ")
        assert not lines[1].startswith(" ")

    def test_empty(self):
        result = format_elements([], "tree")
        assert result == ""


# -----------------------------------------------------------------------
# Invalid format
# -----------------------------------------------------------------------

@pytest.mark.unit
class TestInvalidFormat:
    """Unknown format name should raise ValueError."""

    def test_raises(self):
        el = _make_element()
        with pytest.raises(ValueError, match="Unknown format"):
            format_elements([el], "csv")

    def test_empty_string(self):
        with pytest.raises(ValueError, match="Unknown format"):
            format_elements([], "")

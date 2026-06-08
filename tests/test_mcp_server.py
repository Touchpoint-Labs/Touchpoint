"""Pure unit tests for MCP state and formatting helpers."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("mcp")

from touchpoint.core.element import Element
from touchpoint.core.types import Role
from touchpoint.mcp import server


def _state(
    *,
    window_ids: set[str] | None = None,
    window_titles: dict[str, str] | None = None,
    focused_window: str | None = None,
) -> dict:
    return {
        "window_ids": window_ids or set(),
        "window_titles": window_titles or {},
        "focused_window": focused_window,
        "focused_element": None,
        "elements": {},
    }


def _element(*, eid: str, window_id: str) -> Element:
    return Element(
        id=eid,
        name="Root",
        role=Role.GROUP,
        states=[],
        position=(0, 0),
        size=(0, 0),
        app="Test",
        pid=1,
        backend="ax",
        raw_role="AXGroup",
        window_id=window_id,
    )


@pytest.mark.unit
class TestMcpDiffFlags:
    def test_reports_all_closed_windows_with_focused_first(self):
        before = _state(
            window_ids={"ax:1:a", "ax:1:b", "ax:1:c"},
            window_titles={"ax:1:a": "A", "ax:1:b": "B", "ax:1:c": "C"},
            focused_window="ax:1:b",
        )
        after = _state()

        assert server._diff_flags(before, after) == "(window closed: 'B', 'A', 'C')"


@pytest.mark.unit
class TestMcpSnapshotBaseline:
    def test_subtree_snapshot_uses_owning_window(self, monkeypatch):
        captures: list[str | None] = []
        root = _element(eid="ax:1:target:0", window_id="ax:1:target")

        monkeypatch.setattr(server.tp, "elements", lambda **_kwargs: [])
        monkeypatch.setattr(server.tp, "get_element", lambda _eid: root)
        monkeypatch.setattr(server, "_snapshot_render", lambda *_args, **_kwargs: "body")
        monkeypatch.setattr(
            server,
            "_capture_state",
            lambda window_id=None: captures.append(window_id) or _state(),
        )
        monkeypatch.setattr(server, "_apply_state", lambda _state: None)

        assert server.snapshot(element_id=root.id) == f"element: [{root.id}]\nbody"
        assert captures == ["ax:1:target"]


@pytest.mark.unit
class TestMcpTypeText:
    def test_raw_text_preserves_literal_escape_sequences(self, monkeypatch):
        typed: list[str] = []

        monkeypatch.setattr(server, "_auto_activate_last", lambda: None)
        monkeypatch.setattr(server, "_capture_state", _state)
        monkeypatch.setattr(server, "_verify_wrap", lambda _before, result: result)
        monkeypatch.setattr(server.tp, "type_text", typed.append)

        assert server.type_text(r"\n", raw=True) == "type_text: OK"
        assert typed == [r"\n"]

    def test_default_text_converts_literal_escape_sequences(self, monkeypatch):
        typed: list[str] = []

        monkeypatch.setattr(server, "_auto_activate_last", lambda: None)
        monkeypatch.setattr(server, "_capture_state", _state)
        monkeypatch.setattr(server, "_verify_wrap", lambda _before, result: result)
        monkeypatch.setattr(server.tp, "type_text", typed.append)

        assert server.type_text(r"\n") == "type_text: OK"
        assert typed == ["\n"]


@pytest.mark.unit
class TestMcpAutoActivate:
    def test_raw_element_id_resolves_owning_window(self, monkeypatch):
        activated: list[str] = []
        root = _element(eid="ax:1:target:0", window_id="ax:1:target")

        monkeypatch.setattr(server.tp, "get_element", lambda _eid: root)
        monkeypatch.setattr(server, "_do_activate", activated.append)

        server._auto_activate_element(root.id)

        assert activated == ["ax:1:target"]


@pytest.mark.unit
class TestMcpDiagnostics:
    def test_diagnostics_returns_json(self, monkeypatch):
        monkeypatch.setattr(
            server.tp,
            "diagnostics",
            lambda probe=True: {"probe": probe, "backend": {"available": True}},
        )

        assert json.loads(server.diagnostics(probe=False)) == {
            "probe": False,
            "backend": {"available": True},
        }

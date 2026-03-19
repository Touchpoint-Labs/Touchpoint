"""Output formatting for UI element lists.

Converts a list of :class:`~touchpoint.core.element.Element` instances
into a string in one of three formats:

- **flat** — one compact line per element (uses ``Element.__str__``).
- **json** — JSON array with every field serialised.
- **tree** — indented text showing the parent/child hierarchy.

The public API calls ``format_elements`` when ``format=`` is passed
to ``tp.elements()``.

Example::

    >>> from touchpoint.format.formatter import format_elements
    >>> print(format_elements(elements, "flat"))
    [atspi:2269:1:2.1] [button] 'Send' (512, 340) app=Slack actions=[click]
    [atspi:2269:1:2.3] [text_field] 'Search' (400, 50) app=Slack value='tp' actions=[set-text]
"""

from __future__ import annotations

import json
from typing import Any

from touchpoint.core.element import Element


# ---------------------------------------------------------------------------
# Public function
# ---------------------------------------------------------------------------


def format_elements(elements: list[Element], fmt: str) -> str:
    """Format a list of elements as a string.

    Args:
        elements: The elements to format (pre-filtered by the public
            API).
        fmt: One of ``"flat"``, ``"json"``, or ``"tree"``.

    Returns:
        Formatted string representation of the elements.

    Raises:
        ValueError: If *fmt* is not a recognised format name.

    Example::

        >>> format_elements(elements, "flat")
        '[atspi:2269:1:2.1] [button] \'Send\' (512, 340) app=Slack'
    """
    formatter = _FORMATTERS.get(fmt)
    if formatter is None:
        raise ValueError(
            f"Unknown format {fmt!r}, expected one of: "
            f"{', '.join(_FORMATTERS)}"
        )
    return formatter(elements)


# ---------------------------------------------------------------------------
# Flat
# ---------------------------------------------------------------------------


def _flat(elements: list[Element]) -> str:
    """One line per element using ``Element.__str__``."""
    return "\n".join(str(el) for el in elements)


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def _element_to_dict(el: Element) -> dict[str, Any]:
    """Convert an Element to a JSON-friendly dict."""
    d: dict[str, Any] = {
        "id": el.id,
        "name": el.name,
        "role": el.role.value,
        "states": [s.value for s in el.states],
        "position": list(el.position),
        "size": list(el.size),
        "app": el.app,
        "pid": el.pid,
        "backend": el.backend,
        "raw_role": el.raw_role,
    }
    if el.actions:
        d["actions"] = el.actions
    if el.value is not None:
        d["value"] = el.value
    if el.description is not None:
        d["description"] = el.description
    if el.parent_id is not None:
        d["parent_id"] = el.parent_id
    if el.window_id is not None:
        d["window_id"] = el.window_id
    if el.children:
        d["children"] = [_element_to_dict(c) for c in el.children]
    if el.raw:
        d["raw"] = el.raw
    return d


def _json(elements: list[Element]) -> str:
    """JSON array with all fields serialised."""
    return json.dumps(
        [_element_to_dict(el) for el in elements],
        indent=2,
    )


# ---------------------------------------------------------------------------
# Tree
# ---------------------------------------------------------------------------


def _tree_line(el: Element, depth: int) -> str:
    """Build a single indented line for the tree format."""
    indent = "  " * depth
    return f"{indent}{el}"


def _tree_lines(el: Element, depth: int, out: list[str]) -> None:
    """Recursively build indented lines for an element and its children."""
    out.append(_tree_line(el, depth))
    for child in el.children:
        _tree_lines(child, depth + 1, out)


def _tree(elements: list[Element]) -> str:
    """Indented text showing parent/child hierarchy.

    If elements have populated ``children``, shows nesting.
    Otherwise renders the same as flat (no indentation).
    """
    lines: list[str] = []
    for el in elements:
        _tree_lines(el, 0, lines)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_FORMATTERS: dict[str, Any] = {
    "flat": _flat,
    "json": _json,
    "tree": _tree,
}

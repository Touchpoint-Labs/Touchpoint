"""Post-filtering and sorting helpers for element lists.

Pure functions — no module-level state or side effects.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from touchpoint.core.element import Element
from touchpoint.core.types import Role, State


def _filter_children_recursive(
    children: list[Element],
    states: list[State] | None,
    named_only: bool,
    filter_fn: Callable[[Element], bool] | None,
) -> list[Element]:
    """Recursively filter children (states, named_only, filter).

    ``role`` is intentionally **not** applied — children should
    show the internal structure of role-matched parents.
    ``sort_by`` is also skipped — tree order is structural.
    """
    from dataclasses import replace

    result: list[Element] = []
    for el in children:
        if states is not None and not all(s in el.states for s in states):
            continue
        if named_only and not (el.name and el.name.strip()):
            continue
        if filter_fn is not None and not filter_fn(el):
            continue
        if el.children:
            el = replace(
                el,
                children=_filter_children_recursive(
                    el.children, states, named_only, filter_fn,
                ),
            )
        result.append(el)
    return result


def _filter(
    elements: list[Element],
    states: list[State] | None = None,
    role: Role | None = None,
    named_only: bool = False,
    filter: Callable[[Element], bool] | None = None,
    sort_by: str | Callable[[Element], Any] | None = None,
    filter_children: bool = False,
) -> list[Element]:
    """Post-filter, custom-filter, and sort elements.

    Applied in order: ``role`` → ``states`` → ``named_only`` →
    ``filter`` → ``sort_by``.

    When *filter_children* is ``True``, ``states``, ``named_only``,
    and ``filter`` are also applied recursively to each element's
    ``children`` list.  ``role`` and ``sort_by`` are **not** applied
    to children.
    """
    needs_filter = (
        role is not None
        or states is not None
        or named_only
        or filter is not None
    )
    if not needs_filter and sort_by is None:
        return elements

    needs_child_filter = filter_children and (
        states is not None or named_only or filter is not None
    )

    result: list[Element] = []
    for el in elements:
        if role is not None and el.role != role:
            continue
        if states is not None and not all(s in el.states for s in states):
            continue
        if named_only and not (el.name and el.name.strip()):
            continue
        if filter is not None and not filter(el):
            continue
        if needs_child_filter and el.children:
            from dataclasses import replace
            el = replace(
                el,
                children=_filter_children_recursive(
                    el.children, states, named_only, filter,
                ),
            )
        result.append(el)

    if sort_by is not None:
        if sort_by == "position":
            result.sort(key=lambda el: (el.position[1], el.position[0]))
        elif callable(sort_by):
            result.sort(key=sort_by)
        else:
            raise ValueError(
                f"unknown sort_by value {sort_by!r} "
                f"— use 'position' or a callable"
            )

    return result

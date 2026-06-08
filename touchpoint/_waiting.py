"""Polling-based wait functions for elements, apps, and windows."""

from __future__ import annotations

import time
from typing import Any

from touchpoint.core.element import Element
from touchpoint.core.types import Role, State
from touchpoint.core.window import Window


def wait_for(
    query: str | list[str],
    *,
    app: str | None = None,
    window_id: str | None = None,
    states: list[State] | None = None,
    role: Role | None = None,
    fields: list[str] | None = None,
    mode: str = "any",
    timeout: float = 10.0,
    poll: float = 0.5,
    source: str = "full",
    max_results: int = 5,
    wait_for_new: bool = False,
    gone: bool = False,
) -> list[Element] | bool:
    """Wait for elements to appear or disappear.

    Polls :func:`find` until matching elements are found (or gone)
    or *timeout* is reached.

    Args:
        query: Text to search for.  Pass a single string or a list
            of strings for multi-query mode.
        app: Scope to this application.
        window_id: Scope to this window.
        states: Only match elements with ALL these states.
        role: Only match this role.
        fields: Which fields to search (default: ``["name"]``).
        mode: ``"any"`` (return when any query matches) or ``"all"``
            (wait for all queries to match).
        timeout: Maximum seconds to wait (default 10).
        poll: Seconds between polls (default 0.5).
        source: ``"full"`` (default), ``"cdp_ax"``, ``"native"``,
            or ``"dom"``. ``"ax"`` remains as a compatibility alias
            for ``"cdp_ax"``.
        max_results: Maximum elements to return (default 5).
        wait_for_new: If true, ignore elements already present.
        gone: If true, wait for elements to DISAPPEAR.

    Returns:
        List of matched elements, or ``True`` for gone mode.

    Raises:
        TimeoutError: If the condition is not met within *timeout*.
    """
    if mode not in ("any", "all"):
        raise ValueError(
            f"mode must be 'any' or 'all', got {mode!r}"
        )

    import touchpoint as tp

    raw_queries = [query] if isinstance(query, str) else list(query)
    queries = list(dict.fromkeys(raw_queries))
    unique_queries = set(queries)
    deadline = time.monotonic() + timeout

    # Snapshot existing element IDs when wait_for_new is set.
    existing_ids: set[str] | None = None
    if wait_for_new:
        existing_ids = set()
        for q in queries:
            try:
                pre = tp.find(
                    q, app=app, window_id=window_id,
                    states=states, role=role,
                    fields=fields, source=source, max_results=None,
                )
                existing_ids.update(e.id for e in pre)
            except Exception:
                pass

    while True:
        all_results: dict[str, list[Element]] = {}
        for q in queries:
            try:
                hits = tp.find(
                    q, app=app, window_id=window_id,
                    states=states, role=role,
                    fields=fields, source=source, max_results=max_results,
                )
            except Exception:
                hits = []

            if existing_ids is not None:
                hits = [e for e in hits if e.id not in existing_ids]

            if hits:
                all_results[q] = hits

        if gone:
            if mode == "all" and not all_results:
                return True
            if mode == "any" and len(all_results) < len(unique_queries):
                return True
        else:
            if mode == "any" and all_results:
                merged: list[Element] = []
                for hits in all_results.values():
                    merged.extend(hits)
                return merged[:max_results] if max_results else merged
            if mode == "all" and len(all_results) == len(unique_queries):
                merged = []
                for hits in all_results.values():
                    merged.extend(hits)
                return merged[:max_results] if max_results else merged

        if time.monotonic() >= deadline:
            action = "disappear" if gone else "appear"
            msg = (
                f"wait_for({queries!r}, gone={gone}) timed out "
                f"after {timeout}s waiting for elements to {action}"
            )
            raise TimeoutError(msg)
        time.sleep(poll)


def wait_for_app(
    app: str,
    *,
    timeout: float = 10.0,
    poll: float = 0.5,
    gone: bool = False,
) -> bool:
    """Wait for an application to appear or disappear.

    Args:
        app: Application name to wait for.
        timeout: Maximum seconds to wait (default 10).
        poll: Seconds between polls (default 0.5).
        gone: If true, wait for the app to DISAPPEAR.

    Returns:
        ``True`` when the condition is met.

    Raises:
        TimeoutError: If the condition is not met within *timeout*.
    """
    import touchpoint as tp

    app_lower = app.lower()
    deadline = time.monotonic() + timeout

    while True:
        current = tp.apps()
        found = any(a.lower() == app_lower for a in current)

        if gone and not found:
            return True
        if not gone and found:
            return True

        if time.monotonic() >= deadline:
            action = "disappear" if gone else "appear"
            msg = (
                f"wait_for_app({app!r}, gone={gone}) timed out "
                f"after {timeout}s waiting for app to {action}"
            )
            raise TimeoutError(msg)
        time.sleep(poll)


def wait_for_window(
    title: str,
    *,
    app: str | None = None,
    timeout: float = 10.0,
    poll: float = 0.5,
    gone: bool = False,
) -> Window | bool:
    """Wait for a window to appear or disappear.

    Args:
        title: Window title to search for (substring match).
        app: Only look for windows in this application.
        timeout: Maximum seconds to wait (default 10).
        poll: Seconds between polls (default 0.5).
        gone: If true, wait for the window to DISAPPEAR.

    Returns:
        The matched :class:`Window`, or ``True`` for gone mode.

    Raises:
        TimeoutError: If the condition is not met within *timeout*.
    """
    import touchpoint as tp

    title_lower = title.lower()
    deadline = time.monotonic() + timeout

    while True:
        current = tp.windows()
        if app:
            app_lower = app.lower()
            current = [w for w in current if app_lower in w.app.lower()]

        match = None
        for w in current:
            if title_lower in w.title.lower():
                match = w
                break

        if gone and match is None:
            return True
        if not gone and match is not None:
            return match

        if time.monotonic() >= deadline:
            action = "disappear" if gone else "appear"
            msg = (
                f"wait_for_window({title!r}, gone={gone}) timed out "
                f"after {timeout}s waiting for window to {action}"
            )
            raise TimeoutError(msg)
        time.sleep(poll)

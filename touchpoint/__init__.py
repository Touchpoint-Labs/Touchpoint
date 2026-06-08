"""Touchpoint — unified accessibility API for AI agents.

Import as::

    import touchpoint as tp
    tp.apps()
    tp.windows()
    tp.elements(app="Firefox")

This module is the **only** public entry point.  Everything else
(backends, cache, matching) is internal.
"""

from __future__ import annotations

__version__ = "0.3.0"

from collections.abc import Callable
from typing import Any

# ---------------------------------------------------------------------------
# Shared private state — backends, config, routing helpers.
# Imported once so the rest of this module can reference them directly.
# ---------------------------------------------------------------------------

from touchpoint._state import (  # noqa: F401  (used throughout)
    _backend_for_id,
    _config,
    _get_backend,
    _get_cdp,
    _get_element_position,
    _get_input,
    _is_cdp_app,
    _is_cdp_id,
    _native_window_for_cdp,
    _png_bytes_to_image,
    _resolve_for_fallback,
    _resolve_platform_app,
    _resolve_target,
    _serialized,
    _strip_document_subtrees,
    _topmost_pid_at,
    _try_actions,
    _VALID_SOURCES,
    configure,
    diagnostics,
    ActionFailedError,
    BackendUnavailableError,
)

from touchpoint.core.element import Element
from touchpoint.core.types import Role, State
from touchpoint.core.window import Window

# ---------------------------------------------------------------------------
# Submodule re-exports — extracted for code organisation.
# ---------------------------------------------------------------------------

from touchpoint._filtering import _filter  # noqa: F401

from touchpoint._input_api import (  # noqa: F401
    type_text,
    press_key,
    hotkey,
    click_at,
    double_click_at,
    right_click_at,
    scroll,
    mouse_move,
)

from touchpoint._screenshot import (  # noqa: F401
    screenshot,
    monitor_count,
    find_window,
    _find_window,
)

from touchpoint._waiting import (  # noqa: F401
    wait_for,
    wait_for_app,
    wait_for_window,
)


# ---------------------------------------------------------------------------
# Discovery API
# ---------------------------------------------------------------------------


@_serialized
def apps() -> list[str]:
    """List applications that expose accessibility elements.

    Queries the backend for all applications currently registered
    in the accessibility tree.

    Returns:
        Application names (e.g. ``["Firefox", "Konsole", "Slack"]``).

    Raises:
        BackendUnavailableError: If no backend is available.

    Example::

        >>> import touchpoint as tp
        >>> tp.apps()
        ['Firefox', 'Konsole', 'Kate']
    """
    seen: set[str] = set()
    result: list[str] = []
    for a in _get_backend().get_applications():
        if a.lower() not in seen:
            result.append(a)
            seen.add(a.lower())

    cdp = _get_cdp()
    if cdp is None:
        return result

    cdp_pids: set[int] = cdp.get_owned_pids()

    try:
        covered_pids: set[int] = {
            w.pid
            for w in _get_backend().get_windows()
            if w.pid in cdp_pids and w.app.lower() in seen
        }
    except Exception:
        covered_pids = set()

    try:
        cdp_win_pids: dict[str, set[int]] = {}
        for w in cdp.get_windows():
            cdp_win_pids.setdefault(w.app.lower(), set()).add(w.pid)
    except Exception:
        cdp_win_pids = {}

    for cdp_app in cdp.get_applications():
        if cdp_app.lower() in seen:
            continue
        app_pids = cdp_win_pids.get(cdp_app.lower(), set())
        if app_pids and app_pids <= covered_pids:
            continue
        result.append(cdp_app)
        seen.add(cdp_app.lower())

    return result


@_serialized
def windows() -> list[Window]:
    """List all windows from the accessibility tree.

    Returns every window the backend can see — visible, hidden,
    active, inactive.  No filtering is applied.

    Returns:
        List of :class:`~touchpoint.core.window.Window` instances.

    Raises:
        BackendUnavailableError: If no backend is available.

    Example::

        >>> import touchpoint as tp
        >>> tp.windows()
        [Window('untitled — Kate', app='Kate'), ...]
    """
    platform_wins = _get_backend().get_windows()
    cdp = _get_cdp()
    if cdp is None:
        return platform_wins

    cdp_wins = cdp.get_windows()
    if not cdp_wins:
        return platform_wins

    pid_display: dict[int, str] = {w.pid: w.app for w in platform_wins}

    cdp_pids: set[int] = cdp.get_owned_pids()
    result = [w for w in platform_wins if w.pid not in cdp_pids]

    for w in cdp_wins:
        display = pid_display.get(w.pid)
        if display is not None and display != w.app:
            from dataclasses import replace as _dc_replace
            w = _dc_replace(w, app=display)
        result.append(w)
    return result


# ---------------------------------------------------------------------------
# Element retrieval + filtering
# ---------------------------------------------------------------------------


@_serialized
def elements(
    app: str | None = None,
    window_id: str | None = None,
    tree: bool = False,
    max_depth: int | None = None,
    root_element: str | Element | None = None,
    max_elements: int | None = None,
    states: list[State] | None = None,
    role: Role | None = None,
    named_only: bool = False,
    filter: Callable[[Element], bool] | None = None,
    sort_by: str | Callable[[Element], Any] | None = None,
    filter_children: bool = True,
    format: str | None = None,
    source: str = "full",
) -> list[Element] | str:
    """Get UI elements from the accessibility tree.

    Scoping parameters (``app``, ``window_id``, ``tree``) are passed
    to the backend.  Filtering parameters (``states``, ``role``,
    ``named_only``, ``filter``) are applied afterwards on the returned
    elements.

    Args:
        app: Only include elements from this application
            (case-insensitive).
        window_id: Only include elements under this window.
        tree: If ``True``, populate each element's ``children``
            list recursively.
        max_depth: Maximum recursion depth.  ``None`` uses the
            configured default (``10``).
        root_element: Start the walk from this element id instead
            of the window roots.
        max_elements: Maximum number of elements to collect.
            ``None`` uses the configured default (``5000``).
        states: Only include elements that have **all** of these
            states (AND logic).
        role: Only include elements with this role.
        named_only: If ``True``, exclude elements with empty or
            missing names.
        filter: An arbitrary callable ``(Element) → bool``.
        sort_by: ``"position"`` for reading order, or a callable.
        filter_children: When ``True`` (default) and ``tree=True``,
            recursively apply filters to children.
        format: ``"flat"``, ``"json"``, or ``"tree"``.
        source: ``"full"`` (default), ``"cdp_ax"``, ``"native"``, or
            ``"dom"``. ``"ax"`` remains as a compatibility alias for
            ``"cdp_ax"``.

    Returns:
        List of :class:`Element` instances, or a formatted string.
    """
    if source not in _VALID_SOURCES:
        raise ValueError(
            f"source must be one of {_VALID_SOURCES!r}, got {source!r}"
        )

    if (
        sort_by is not None
        and sort_by != "position"
        and not callable(sort_by)
    ):
        raise ValueError(
            f"unknown sort_by value {sort_by!r} "
            f"— use 'position' or a callable"
        )

    if isinstance(root_element, Element):
        root_element = root_element.id

    # DOM source — CDP only.
    if source == "dom":
        if tree:
            raise ValueError(
                "source='dom' does not support tree=True yet"
            )
        if (
            root_element is not None
            and isinstance(root_element, str)
            and _is_cdp_id(root_element)
            and ":dom:" not in root_element
        ):
            raise ValueError(
                "root_element must be a DOM-sourced element ID "
                "when source='dom' (got an AX-sourced ID)"
            )
        cdp = _get_cdp()
        if cdp is None:
            raise BackendUnavailableError(
                backend="cdp",
                reason="source='dom' requires a CDP backend",
            )
        if (
            app is not None
            and not _is_cdp_app(app)
            and (window_id is None or not _is_cdp_id(window_id))
            and (root_element is None or not _is_cdp_id(
                root_element if isinstance(root_element, str)
                else root_element
            ))
        ):
            from touchpoint.core.exceptions import TouchpointError
            raise TouchpointError(
                f"source='dom' is only supported for CDP-backed "
                f"apps, but {app!r} is not a CDP app"
            )
        effective_max_elements = (
            max_elements if max_elements is not None
            else _config["max_elements"]
        )
        effective_max_depth = max_depth
        result = cdp.get_dom_elements(
            app=app, window_id=window_id,
            root_element=root_element,
            tree=tree, max_depth=effective_max_depth,
            max_elements=effective_max_elements,
            role=role, states=states, named_only=named_only,
        )
        result = _filter(
            result, states=states, role=role,
            named_only=named_only, filter=filter, sort_by=sort_by,
            filter_children=filter_children and tree,
        )
        if format is not None:
            from touchpoint.format.formatter import format_elements
            return format_elements(result, format)
        return result

    # Resolve effective limits.
    effective_max_elements = max_elements if max_elements is not None else _config["max_elements"]
    effective_max_depth = max_depth if max_depth is not None else _config["max_depth"]

    _get_kw: dict[str, Any] = dict(
        tree=tree, max_depth=effective_max_depth,
        max_elements=effective_max_elements,
        role=role, states=states, named_only=named_only,
    )

    # --- source="native": platform backend only ---
    if source == "native":
        result = _get_backend().get_elements(
            app=app, window_id=window_id,
            root_element=root_element, **_get_kw,
        )

    elif root_element is not None and _is_cdp_id(root_element):
        cdp = _get_cdp()
        if cdp is None:
            raise BackendUnavailableError(
                backend="cdp",
                reason="CDP backend required for element "
                f"{root_element!r}",
            )
        result = cdp.get_elements(
            app=app, window_id=window_id,
            root_element=root_element, **_get_kw,
        )
    elif window_id is not None and _is_cdp_id(window_id):
        cdp = _get_cdp()
        if cdp is None:
            raise BackendUnavailableError(
                backend="cdp",
                reason="CDP backend required for window "
                f"{window_id!r}",
            )
        result = cdp.get_elements(
            app=app, window_id=window_id,
            root_element=root_element, **_get_kw,
        )

    # --- source="cdp_ax": CDP AX tree only ---
    elif source in ("cdp_ax", "ax"):
        source_label = f"source={source!r}"
        cdp = _get_cdp()
        if app is None:
            if cdp is None:
                raise BackendUnavailableError(
                    backend="cdp",
                    reason=f"{source_label} requires a CDP backend",
                )
            result = cdp.get_elements(
                app=None, window_id=window_id,
                root_element=root_element, **_get_kw,
            )
        elif _is_cdp_app(app):
            if cdp is None:
                raise BackendUnavailableError(
                    backend="cdp",
                    reason=f"{source_label} requires a CDP backend",
                )
            result = cdp.get_elements(
                app=app, window_id=window_id,
                root_element=root_element, **_get_kw,
            )
        else:
            from touchpoint.core.exceptions import TouchpointError
            raise TouchpointError(
                f"{source_label} is only supported for CDP-backed "
                f"apps, but {app!r} is not a CDP app"
            )

    # --- source="full" (default): merged CDP + native ---
    elif app is not None and _is_cdp_app(app):
        native: list[Element] = []
        try:
            platform_app = _resolve_platform_app(app)
            _native_kw = dict(
                app=platform_app, window_id=window_id,
                root_element=root_element,
                tree=tree, max_depth=effective_max_depth,
                max_elements=effective_max_elements,
                role=role, states=states, named_only=named_only,
                skip_subtree_roles={Role.DOCUMENT},
            )
            native = _strip_document_subtrees(
                _get_backend().get_elements(**_native_kw),
            )
        except Exception:
            pass
        cdp_budget = max(0, effective_max_elements - len(native))
        cdp = _get_cdp()
        cdp_kw = dict(
            tree=tree, max_depth=effective_max_depth,
            max_elements=cdp_budget,
            role=role, states=states, named_only=named_only,
        )
        if cdp is not None and (root_element is None or cdp.owns_element(root_element)):
            cdp_els = cdp.get_elements(
                app=app, window_id=window_id,
                root_element=root_element, **cdp_kw,
            )
        else:
            cdp_els = []
        result = cdp_els + native
    else:
        result = _get_backend().get_elements(
            app=app, window_id=window_id,
            root_element=root_element, **_get_kw,
        )
        cdp = _get_cdp()
        if cdp is not None and app is None and root_element is None and window_id is None:
            cdp_pids: set[int] = cdp.get_owned_pids()
            if cdp_pids:
                native_cdp = [e for e in result if e.pid in cdp_pids]
                native_other = [e for e in result if e.pid not in cdp_pids]
                result = _strip_document_subtrees(native_cdp) + native_other
            cdp_budget = max(
                0, effective_max_elements - len(result),
            )
            cdp_elements = cdp.get_elements(
                app=app, window_id=window_id,
                root_element=root_element,
                tree=tree, max_depth=effective_max_depth,
                max_elements=cdp_budget,
                role=role, states=states, named_only=named_only,
            )
            result.extend(cdp_elements)

    result = _filter(
        result, states=states, role=role,
        named_only=named_only, filter=filter, sort_by=sort_by,
        filter_children=filter_children and tree,
    )

    if format is not None:
        from touchpoint.format.formatter import format_elements

        return format_elements(result, format)

    return result


@_serialized
def element_at(x: int, y: int) -> Element | None:
    """Get the deepest element at a screen coordinate.

    Args:
        x: Horizontal pixel coordinate (screen-absolute).
        y: Vertical pixel coordinate (screen-absolute).

    Returns:
        The deepest :class:`Element` at ``(x, y)``, or ``None``
        if nothing is found.
    """
    cdp = _get_cdp()
    if cdp is not None:
        cdp_pids: set[int] = cdp.get_owned_pids()
        if cdp_pids:
            top_pid = _topmost_pid_at(x, y)
            if top_pid is not None and top_pid in cdp_pids:
                return cdp.get_element_at(x, y)

    return _get_backend().get_element_at(x, y)


# ---------------------------------------------------------------------------
# Finding / matching
# ---------------------------------------------------------------------------

from touchpoint.backends.base import Backend  # noqa: E402


@_serialized
def find(
    query: str,
    app: str | None = None,
    window_id: str | None = None,
    states: list[State] | None = None,
    role: Role | None = None,
    max_results: int | None = None,
    fields: list[str] | None = None,
    filter: Callable[[Element], bool] | None = None,
    format: str | None = None,
    source: str = "full",
) -> list[Element] | str:
    """Search for elements by name using the matching pipeline.

    First retrieves elements from the backend, then applies filtering,
    and finally runs the matching pipeline (exact → contains →
    contains-words → fuzzy).  Results are sorted best-first.

    Args:
        query: The search string.
        app: Only search within this application.
        window_id: Only search within this window.
        states: Only match elements with ALL these states.
        role: Only match elements with this role.
        max_results: Maximum matches to return.
        fields: Which fields to search (default: ``["name"]``).
        filter: Arbitrary ``(Element) → bool`` callable.
        format: ``"flat"`` or ``"json"``.
        source: ``"full"``, ``"cdp_ax"``, ``"native"``, or ``"dom"``.
            ``"ax"`` remains as a compatibility alias for ``"cdp_ax"``.

    Returns:
        List of :class:`Element` sorted by match quality, or a
        formatted string.
    """
    if source not in _VALID_SOURCES:
        raise ValueError(
            f"source must be one of {_VALID_SOURCES!r}, got {source!r}"
        )

    if format == "tree":
        msg = "tree format is not supported by find() — results are ranked, not hierarchical"
        raise ValueError(msg)

    _valid_fields = {"name", "value", "description"}
    search_fields = fields or ["name"]
    bad = set(search_fields) - _valid_fields
    if bad:
        msg = f"invalid fields {bad!r} — allowed: {sorted(_valid_fields)}"
        raise ValueError(msg)

    from touchpoint.matching.matcher import match

    backend = _get_backend()

    use_lightweight = search_fields == ["name"]
    skip_unnamed = "name" in search_fields and len(search_fields) == 1

    # DOM source — delegate to elements().
    if source == "dom":
        pool = elements(
            app=app, window_id=window_id,
            role=role, states=states,
            named_only=skip_unnamed,
            source="dom",
        )
        if not isinstance(pool, list):
            pool = []
        pool = _filter(pool, states=states, role=role)
        text_fn_dom: Callable[[Element], list[str]] | None = None
        if search_fields != ["name"]:
            def _text_fn_dom(el: Element) -> list[str]:
                texts: list[str] = []
                for f in search_fields:
                    v = getattr(el, f, None)
                    if v:
                        texts.append(v)
                return texts
            text_fn_dom = _text_fn_dom
        results = match(
            query, pool, max_results=max_results,
            threshold=_config["fuzzy_threshold"],
            text_fn=text_fn_dom,
        )
        result = [r.element for r in results]
        if filter is not None:
            result = [el for el in result if filter(el)]
        if format is not None:
            from touchpoint.format.formatter import format_elements
            return format_elements(result, format)
        return result

    # Determine backends to search.
    _cdp_native_merge = False
    if source == "native":
        backends_to_search: list[tuple[Backend, bool]] = [
            (backend, use_lightweight),
        ]
    elif window_id is not None and _is_cdp_id(window_id):
        cdp = _get_cdp()
        if cdp is None:
            raise BackendUnavailableError(
                backend="cdp",
                reason="CDP backend required for window "
                f"{window_id!r}",
            )
        backends_to_search = [
            (cdp, use_lightweight),
        ]
    elif source in ("cdp_ax", "ax"):
        source_label = f"source={source!r}"
        cdp = _get_cdp()
        if cdp is None:
            raise BackendUnavailableError(
                backend="cdp",
                reason=f"{source_label} requires a CDP backend",
            )
        if app is None or _is_cdp_app(app):
            backends_to_search = [(cdp, use_lightweight)]
        else:
            from touchpoint.core.exceptions import TouchpointError
            raise TouchpointError(
                f"{source_label} is only supported for CDP-backed "
                f"apps, but {app!r} is not a CDP app"
            )
    elif app is not None and _is_cdp_app(app):
        cdp = _get_cdp()
        if cdp is None:
            raise BackendUnavailableError(
                backend="cdp",
                reason=f"CDP backend required for app {app!r}",
            )
        backends_to_search = [(cdp, use_lightweight)]
        _cdp_native_merge = True
    else:
        backends_to_search = [(backend, use_lightweight)]
        cdp = _get_cdp()
        if (
            source != "native"
            and cdp is not None
            and window_id is None
        ):
            backends_to_search.append((cdp, use_lightweight))

    # Collect elements from all relevant backends.
    pool: list[Element] = []
    inflate_backends: dict[str, Backend] = {}
    inflate = use_lightweight
    for be, lw in backends_to_search:
        _get = be.get_elements
        if lw:
            elems = _get(app=app, window_id=window_id,
                         lightweight=True,
                         role=role, states=states,
                         named_only=skip_unnamed)
        else:
            elems = _get(app=app, window_id=window_id,
                         role=role, states=states,
                         named_only=skip_unnamed)
        pool.extend(elems)
        for el in elems:
            inflate_backends[el.backend] = be

    if _cdp_native_merge:
        try:
            platform_app = _resolve_platform_app(app)
            _native_kw: dict[str, Any] = dict(
                app=platform_app, window_id=window_id,
                role=role, states=states,
                named_only=skip_unnamed,
                skip_subtree_roles={Role.DOCUMENT},
            )
            if use_lightweight:
                _native_kw["lightweight"] = True
            native = backend.get_elements(**_native_kw)
            native = _strip_document_subtrees(native)
            pool.extend(native)
            for el in native:
                inflate_backends[el.backend] = backend
        except Exception:
            pass

    if len(backends_to_search) > 1:
        cdp_obj = _get_cdp()
        if cdp_obj is not None:
            cdp_pids: set[int] = cdp_obj.get_owned_pids()
            if cdp_pids:
                cdp_elems = [e for e in pool if e.backend == "cdp"]
                native_cdp_pid = [
                    e for e in pool
                    if e.backend != "cdp" and e.pid in cdp_pids
                ]
                native_other = [
                    e for e in pool
                    if e.backend != "cdp" and e.pid not in cdp_pids
                ]
                pool = (
                    cdp_elems
                    + _strip_document_subtrees(native_cdp_pid)
                    + native_other
                )

    pool = _filter(pool, states=states, role=role)

    text_fn: Callable[[Element], list[str]] | None = None
    if search_fields != ["name"]:
        def _text_fn(el: Element) -> list[str]:
            texts: list[str] = []
            for f in search_fields:
                v = getattr(el, f, None)
                if v:
                    texts.append(v)
            return texts
        text_fn = _text_fn

    results = match(
        query, pool, max_results=max_results,
        threshold=_config["fuzzy_threshold"],
        text_fn=text_fn,
    )
    if inflate:
        result = []
        for r in results:
            be = inflate_backends.get(r.element.backend, backend)
            result.append(be.inflate_element(r.element))
    else:
        result = [r.element for r in results]

    if filter is not None:
        result = [el for el in result if filter(el)]

    if format is not None:
        from touchpoint.format.formatter import format_elements

        return format_elements(result, format)

    return result


# ---------------------------------------------------------------------------
# Window actions
# ---------------------------------------------------------------------------


@_serialized
def activate_window(window: Window | str) -> bool:
    """Bring a window to the foreground.

    Tries the backend's native activation first.  Falls back to the
    input provider's OS-level approach when native activation is
    unavailable.

    Args:
        window: A :class:`Window` instance or a window id string.

    Returns:
        ``True`` if the window was activated.
    """
    win_id = window.id if isinstance(window, Window) else window

    if _is_cdp_id(win_id):
        cdp = _get_cdp()
        if cdp is not None:
            return cdp.activate_window(win_id)
        return False

    backend = _get_backend()

    # Backend raises ActionFailedError for malformed / not-found and
    # returns True on success.  Returning False means "tried but the
    # native path didn't take" — fall through to the InputProvider.
    if backend.activate_window(win_id):
        return True

    # InputProvider fallback needs the Window object for title + pid.
    # Look it up lazily here only when the native path failed.
    if isinstance(window, str):
        all_wins = backend.get_windows()
        found = next((w for w in all_wins if w.id == window), None)
        if found is None:
            return False  # window vanished between native call and fallback
        window = found

    from touchpoint._state import _init_input, _input_provider
    provider = _input_provider or _init_input()
    if provider is not None:
        return provider.activate_window(window.title, window.pid)

    return False


def _resolve_window(window: Window | str) -> str:
    """Extract a window id from a :class:`Window` or pass a string through.

    Does not validate existence — that's the backend's job, so it can
    distinguish malformed IDs from windows that no longer exist and
    raise the right :class:`ActionFailedError`.
    """
    return window.id if isinstance(window, Window) else window


def _os_window_id(window: Window | str, action: str) -> str:
    """Return the OS-backend window id for *window*.

    A CDP window is a browser page/tab target, not an OS window. OS-frame
    operations (minimize, fullscreen, close, move, resize) are performed by
    the platform backend, so map a ``cdp:`` window to its native OS window
    (by owning PID) before routing. Non-CDP ids pass through unchanged.

    Raises:
        ActionFailedError: If the CDP target has no resolvable native OS
            window (e.g. it has been closed).
    """
    win_id = _resolve_window(window)
    if not _is_cdp_id(win_id):
        return win_id
    native_id = _native_window_for_cdp(win_id)
    if native_id is None:
        raise ActionFailedError(
            action=action,
            element_id=win_id,
            reason=(
                "no native OS window found for this CDP page-target window "
                "(it may have been closed)"
            ),
        )
    return native_id


@_serialized
def minimize_window(window: Window | str) -> bool:
    """Minimize a window. Use ``activate_window`` to restore."""
    return _get_backend().minimize_window(
        _os_window_id(window, "minimize_window"),
    )


@_serialized
def fullscreen_window(
    window: Window | str, fullscreen: bool = True,
) -> bool:
    """Enter or exit fullscreen for a window."""
    return _get_backend().fullscreen_window(
        _os_window_id(window, "fullscreen_window"), fullscreen=fullscreen,
    )


@_serialized
def close_window(window: Window | str) -> bool:
    """Close a window."""
    return _get_backend().close_window(
        _os_window_id(window, "close_window"),
    )


@_serialized
def move_window(window: Window | str, x: int, y: int) -> bool:
    """Move a window to a new screen position."""
    return _get_backend().move_window(
        _os_window_id(window, "move_window"), x, y,
    )


@_serialized
def resize_window(
    window: Window | str, width: int, height: int,
) -> bool:
    """Resize a window."""
    return _get_backend().resize_window(
        _os_window_id(window, "resize_window"), width, height,
    )


# ---------------------------------------------------------------------------
# Actions — element-targeted
# ---------------------------------------------------------------------------


@_serialized
def click(element: Element | str) -> bool:
    """Click an element.

    Tries native accessibility actions first.  Falls back to
    coordinate-based click when ``fallback_input=True``.

    Args:
        element: An :class:`Element` or an element id string.

    Returns:
        ``True`` if the click was dispatched.
    """
    eid = _resolve_target(element)
    backend = _backend_for_id(eid)
    try:
        return _try_actions(eid, backend.ACTION_ALIASES["click"], action="click")
    except ActionFailedError as exc:
        if _is_cdp_id(eid) or not _config["fallback_input"]:
            raise
        el = _resolve_for_fallback(eid, "click", exc)
        _get_input().click_at(*el.position)
        return True


@_serialized
def double_click(element: Element | str) -> bool:
    """Double-click an element."""
    eid = _resolve_target(element)
    backend = _backend_for_id(eid)
    try:
        return _try_actions(
            eid, backend.ACTION_ALIASES["double_click"], action="double_click",
        )
    except ActionFailedError as exc:
        if _is_cdp_id(eid) or not _config["fallback_input"]:
            raise
        el = _resolve_for_fallback(eid, "double_click", exc)
        _get_input().double_click_at(*el.position)
        return True


@_serialized
def right_click(element: Element | str) -> bool:
    """Right-click (context menu) on an element."""
    eid = _resolve_target(element)
    backend = _backend_for_id(eid)
    try:
        return _try_actions(
            eid, backend.ACTION_ALIASES["right_click"], action="right_click",
        )
    except ActionFailedError as exc:
        if _is_cdp_id(eid) or not _config["fallback_input"]:
            raise
        el = _resolve_for_fallback(eid, "right_click", exc)
        _get_input().right_click_at(*el.position)
        return True


@_serialized
def set_value(
    element: Element | str, value: str, *, replace: bool = False,
) -> bool:
    """Set the text content of an editable element.

    By default **inserts** *value* at the current cursor position.
    Pass ``replace=True`` to clear the field first.
    """
    eid = _resolve_target(element)
    try:
        return _backend_for_id(eid).set_value(eid, value, replace=replace)
    except ActionFailedError as set_err:
        if _is_cdp_id(eid) or not _config["fallback_input"]:
            raise
        # The fallback is "focus the target then type into focused
        # widget" — only safe if the element actually exists.  Otherwise
        # typing would land on whichever widget happens to hold focus
        # right now.  ``_resolve_for_fallback`` raises a structured
        # ActionFailedError (malformed / not-found) when the lookup
        # itself fails, so junk IDs never reach the type-text path.
        _resolve_for_fallback(eid, "set_value", set_err)
        try:
            focus(element)
        except ActionFailedError:
            pass
        inp = _get_input()
        if replace:
            inp.hotkey(*inp.SELECT_ALL_KEYS)
        inp.type_text(value)
        return True


@_serialized
def get_text_content(element: Element | str) -> str | None:
    """Return the full text content of an element.

    Fetches the element's text via the backend's native text interface
    (AT-SPI2 Text, UIA TextPattern, macOS AXValue, CDP textContent/value).

    Returns:
        The element's text, ``""`` for an accessible but empty element,
        or ``None`` if the element does not expose text content (or
        cannot be found in the tree).

    Raises:
        ValueError: If *element* is a string with a structurally
            malformed id.  Elements obtained via :func:`find` or
            :func:`snapshot` always pass validation.
    """
    eid = _resolve_target(element)
    return _backend_for_id(eid).get_text_content(eid)


@_serialized
def select_text(
    element: Element | str,
    text: str,
    *,
    occurrence: int = 1,
) -> bool:
    """Select a substring within an element's text content.

    Fetches the element's current text via the native text interface,
    locates the *occurrence*-th match of *text*, and applies a native
    text-range selection.

    Args:
        element: An :class:`Element` or an element id string.
        text: The substring to select.
        occurrence: Which occurrence to select (1-based, default 1).

    Returns:
        ``True`` if the selection was applied.

    Raises:
        ActionFailedError: If the element has no text interface,
            the substring is not found, or the backend rejects the
            selection.

    Example::

        >>> tp.select_text("atspi:2269:1:2.1", "hello")
        True
        >>> tp.select_text("atspi:2269:1:2.1", "world", occurrence=2)
        True
    """
    eid = _resolve_target(element)

    if not text:
        raise ActionFailedError(
            action="select_text",
            element_id=eid,
            reason="text must be a non-empty string",
        )
    if occurrence < 1:
        raise ActionFailedError(
            action="select_text",
            element_id=eid,
            reason=f"occurrence must be >= 1, got {occurrence}",
        )

    backend = _backend_for_id(eid)
    try:
        content = backend.get_text_content(eid)
    except ValueError as exc:
        raise ActionFailedError(
            action="select_text",
            element_id=eid,
            reason=f"malformed element_id: {exc}",
        ) from exc
    if content is None:
        # Disambiguate: "no element at all" vs "element exists but has
        # no text interface" — both surface as None from get_text_content,
        # so check existence to give the agent an accurate diagnosis.
        if backend.get_element_by_id(eid) is None:
            raise ActionFailedError(
                action="select_text",
                element_id=eid,
                reason="element not found — it may have been removed or its ID is stale",
            )
        raise ActionFailedError(
            action="select_text",
            element_id=eid,
            reason="element does not expose text content via the accessibility API",
        )

    start = -1
    for _ in range(occurrence):
        start = content.find(text, start + 1)
        if start == -1:
            raise ActionFailedError(
                action="select_text",
                element_id=eid,
                reason=f"substring {text!r} not found (occurrence {occurrence})",
            )

    end = start + len(text)
    if not backend.select_text(eid, start, end):
        raise ActionFailedError(
            action="select_text",
            element_id=eid,
            reason="backend does not support text selection",
        )
    return True


@_serialized
def focus(element: Element | str) -> bool:
    """Move keyboard focus to an element.

    No InputProvider fallback — clicking has semantic side effects.

    Args:
        element: An :class:`Element` or an element id string.

    Returns:
        ``True`` if focus was moved.
    """
    eid = _resolve_target(element)
    return _backend_for_id(eid).focus_element(eid)


@_serialized
def set_numeric_value(element: Element | str, value: float) -> bool:
    """Set the numeric value of a range element (slider, spinbox)."""
    eid = _resolve_target(element)
    return _backend_for_id(eid).set_numeric_value(eid, value)


@_serialized
def action(element: Element | str, action_name: str) -> bool:
    """Perform a raw accessibility action by exact name.

    Unlike :func:`click`/:func:`focus`, this does **not** try
    aliases — it calls exactly the action you specify.
    """
    eid = _resolve_target(element)
    return _backend_for_id(eid).do_action(eid, action_name)


# ---------------------------------------------------------------------------
# Text selection
# ---------------------------------------------------------------------------


@_serialized
def select_text_range(
    element: Element | str, start: int, end: int,
) -> bool:
    """Select a character range within an element's text.

    Low-level API for when you know the exact offsets.

    Args:
        element: An :class:`Element` or an element id string.
        start: Start character offset (0-based, inclusive).
        end: End character offset (0-based, exclusive).

    Returns:
        ``True`` if the selection was applied.

    Raises:
        ActionFailedError: If the backend doesn't support selection.
    """
    eid = _resolve_target(element)
    ok = _backend_for_id(eid).select_text(eid, start, end)
    if not ok:
        raise ActionFailedError(
            action="select_text_range",
            element_id=eid,
            reason="backend does not support text selection",
        )
    return True


# ---------------------------------------------------------------------------
# Element lookup by id
# ---------------------------------------------------------------------------


@_serialized
def get_element(element_id: str, *, format: str | None = None) -> Element | str | None:
    """Retrieve a single element by its id.

    Returns a **fresh** snapshot with current position, states, etc.
    """
    if format == "tree":
        msg = "tree format is not supported by get_element() — a single element has no hierarchy"
        raise ValueError(msg)

    el = _backend_for_id(element_id).get_element_by_id(element_id)

    if el is not None and format is not None:
        from touchpoint.format.formatter import format_elements

        return format_elements([el], format)

    return el


# ---------------------------------------------------------------------------
# Re-exports (so users can do tp.Role, tp.State, etc.)
# ---------------------------------------------------------------------------

from touchpoint.core.element import Element as Element  # noqa: E402, F811
from touchpoint.core.exceptions import (  # noqa: E402, F811
    ActionFailedError as ActionFailedError,
    BackendUnavailableError as BackendUnavailableError,
    TouchpointError as TouchpointError,
)
from touchpoint.core.types import Role as Role, State as State  # noqa: E402, F811
from touchpoint.core.types import (  # noqa: E402
    CONTAINER_ROLES as CONTAINER_ROLES,
    INTERACTIVE_ROLES as INTERACTIVE_ROLES,
    STRUCTURAL_ROLES as STRUCTURAL_ROLES,
)
from touchpoint.core.window import Window as Window  # noqa: E402, F811

# ---------------------------------------------------------------------------
# Public API boundary
# ---------------------------------------------------------------------------

__all__ = [
    # Discovery
    "apps",
    "windows",
    "elements",
    "element_at",
    "get_element",
    # Window actions
    "activate_window",
    "minimize_window",
    "fullscreen_window",
    "close_window",
    "move_window",
    "resize_window",
    # Finding / matching
    "find",
    # Waiting
    "wait_for",
    "wait_for_app",
    "wait_for_window",
    # Screenshot
    "screenshot",
    "monitor_count",
    "find_window",
    # Element-targeted actions
    "click",
    "double_click",
    "right_click",
    "set_value",
    "set_numeric_value",
    "focus",
    "select_text",
    "get_text_content",
    "action",
    # Text selection
    "select_text_range",
    # Raw input (InputProvider)
    "type_text",
    "press_key",
    "hotkey",
    "click_at",
    "double_click_at",
    "right_click_at",
    "scroll",
    "mouse_move",
    # Configuration
    "configure",
    "diagnostics",
    # Data models
    "Element",
    "Window",
    "Role",
    "State",
    "INTERACTIVE_ROLES",
    "CONTAINER_ROLES",
    "STRUCTURAL_ROLES",
    # Exceptions
    "TouchpointError",
    "ActionFailedError",
    "BackendUnavailableError",
]

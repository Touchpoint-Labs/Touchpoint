"""AT-SPI2 backend for Linux.

Uses ``gi.repository.Atspi`` (PyGObject) to read the accessibility
tree exposed by Qt, GTK, Firefox, LibreOffice, and other native
Linux applications.

This is the primary backend on Linux.  Electron/Chromium apps that
only expose 2-3 elements via AT-SPI2 should use the CDP backend
instead.

Requires:
    - System package: ``libatk-adaptor``, ``at-spi2-core``
    - Python package: ``PyGObject`` (installed automatically with ``pip install touchpoint-py``)
"""

from __future__ import annotations

import sys

from touchpoint.backends.base import Backend
from touchpoint.core.element import Element
from touchpoint.core.exceptions import ActionFailedError
from touchpoint.core.types import Role, State
from touchpoint.core.window import Window
from touchpoint.utils.scale import get_scale_factor

# ---------------------------------------------------------------------------
# Role mapping: AT-SPI2 role names → Touchpoint Role
# ---------------------------------------------------------------------------
# Atspi roles are like "ROLE_PUSH_BUTTON", "ROLE_TEXT", "ROLE_MENU_ITEM".
# We map them to our unified Role enum.  Anything not in this dict becomes
# Role.UNKNOWN with the original preserved in Element.raw_role.
# ---------------------------------------------------------------------------

_ATSPI_ROLE_MAP: dict[str, Role] = {
    "ROLE_APPLICATION": Role.APPLICATION,
    "ROLE_WINDOW": Role.WINDOW,
    "ROLE_DIALOG": Role.DIALOG,
    "ROLE_PANEL": Role.PANEL,
    "ROLE_FRAME": Role.FRAME,
    # Interactive
    "ROLE_PUSH_BUTTON": Role.BUTTON,
    "ROLE_BUTTON": Role.BUTTON,
    "ROLE_TOGGLE_BUTTON": Role.TOGGLE_BUTTON,
    "ROLE_CHECK_BOX": Role.CHECK_BOX,
    "ROLE_RADIO_BUTTON": Role.RADIO_BUTTON,
    "ROLE_LINK": Role.LINK,
    # Text
    "ROLE_TEXT": Role.TEXT_FIELD,
    "ROLE_ENTRY": Role.TEXT_FIELD,
    "ROLE_STATIC": Role.TEXT,
    "ROLE_LABEL": Role.LABEL,
    "ROLE_HEADING": Role.HEADING,
    "ROLE_PARAGRAPH": Role.PARAGRAPH,
    # Menus
    "ROLE_MENU_BAR": Role.MENU_BAR,
    "ROLE_MENU": Role.MENU,
    "ROLE_MENU_ITEM": Role.MENU_ITEM,
    # Lists & Trees
    "ROLE_LIST": Role.LIST,
    "ROLE_LIST_ITEM": Role.LIST_ITEM,
    "ROLE_TREE": Role.TREE,
    "ROLE_TREE_ITEM": Role.TREE_ITEM,
    # Tables
    "ROLE_TABLE": Role.TABLE,
    "ROLE_TABLE_ROW": Role.TABLE_ROW,
    "ROLE_TABLE_CELL": Role.TABLE_CELL,
    "ROLE_TABLE_COLUMN_HEADER": Role.TABLE_COLUMN_HEADER,
    "ROLE_TABLE_ROW_HEADER": Role.TABLE_ROW_HEADER,
    # Tabs
    "ROLE_PAGE_TAB_LIST": Role.TAB_LIST,
    "ROLE_PAGE_TAB": Role.TAB,
    # Selection & Range
    "ROLE_COMBO_BOX": Role.COMBO_BOX,
    "ROLE_SLIDER": Role.SLIDER,
    "ROLE_SPIN_BUTTON": Role.SPIN_BUTTON,
    "ROLE_SCROLL_BAR": Role.SCROLL_BAR,
    "ROLE_PROGRESS_BAR": Role.PROGRESS_BAR,
    # Toolbars & Status
    "ROLE_TOOL_BAR": Role.TOOLBAR,
    "ROLE_STATUS_BAR": Role.STATUS_BAR,
    "ROLE_SEPARATOR": Role.SEPARATOR,
    # Media & Content
    "ROLE_IMAGE": Role.IMAGE,
    "ROLE_ICON": Role.ICON,
    "ROLE_DOCUMENT_FRAME": Role.DOCUMENT,
    "ROLE_DOCUMENT_WEB": Role.DOCUMENT,
    "ROLE_CANVAS": Role.CANVAS,
    "ROLE_IMAGE_MAP": Role.IMAGE,
    "ROLE_MATH": Role.MATH,
    "ROLE_FIGURE": Role.FIGURE,
    # Containers
    "ROLE_FILLER": Role.GROUP,
    "ROLE_SECTION": Role.SECTION,
    "ROLE_FORM": Role.FORM,
    "ROLE_SCROLL_PANE": Role.PANEL,
    "ROLE_LAYERED_PANE": Role.PANEL,
    "ROLE_EMBEDDED": Role.GROUP,
    "ROLE_GRID": Role.GRID,
    "ROLE_GRID_CELL": Role.GRID_CELL,
    # Alerts & Live regions
    "ROLE_ALERT": Role.ALERT,
    "ROLE_NOTIFICATION": Role.NOTIFICATION,
    "ROLE_LOG": Role.LOG,
    # Tooltips & Popups
    "ROLE_TOOL_TIP": Role.TOOLTIP,
    "ROLE_POPUP_MENU": Role.MENU,
    # Toggles & Password
    "ROLE_PASSWORD_TEXT": Role.PASSWORD_TEXT,
    # Menu variants
    "ROLE_CHECK_MENU_ITEM": Role.CHECK_MENU_ITEM,
    "ROLE_RADIO_MENU_ITEM": Role.RADIO_MENU_ITEM,
    "ROLE_SPLIT_MENU_ITEM": Role.SPLIT_BUTTON,
    # Landmarks
    "ROLE_LANDMARK": Role.LANDMARK,
    # Headers & Footers
    "ROLE_HEADER": Role.HEADER,
    "ROLE_FOOTER": Role.FOOTER,
    # Tab content
    "ROLE_TAB_PANEL": Role.TAB_PANEL,
    # Window chrome
    "ROLE_TITLE_BAR": Role.TITLE_BAR,
    # Content types
    "ROLE_ARTICLE": Role.ARTICLE,
    # Toggles & Range (AT-SPI2 ≥ 2.28)
    "ROLE_SWITCH": Role.SWITCH,
    "ROLE_TIMER": Role.TIMER,
    "ROLE_LEVEL_BAR": Role.METER,
}

# ---------------------------------------------------------------------------
# State mapping: AT-SPI2 state names → Touchpoint State
# ---------------------------------------------------------------------------

_ATSPI_STATE_MAP: dict[str, State] = {
    "STATE_VISIBLE": State.VISIBLE,
    "STATE_SHOWING": State.SHOWING,
    "STATE_ENABLED": State.ENABLED,
    "STATE_SENSITIVE": State.SENSITIVE,
    "STATE_FOCUSABLE": State.FOCUSABLE,
    "STATE_FOCUSED": State.FOCUSED,
    "STATE_CLICKABLE": State.CLICKABLE,
    "STATE_SELECTED": State.SELECTED,
    "STATE_SELECTABLE": State.SELECTABLE,
    "STATE_CHECKED": State.CHECKED,
    "STATE_PRESSED": State.PRESSED,
    "STATE_EXPANDABLE": State.EXPANDABLE,
    "STATE_EXPANDED": State.EXPANDED,
    "STATE_COLLAPSED": State.COLLAPSED,
    "STATE_EDITABLE": State.EDITABLE,
    "STATE_READ_ONLY": State.READ_ONLY,
    "STATE_MULTI_LINE": State.MULTI_LINE,
    "STATE_SINGLE_LINE": State.SINGLE_LINE,
    "STATE_MODAL": State.MODAL,
    "STATE_ACTIVE": State.ACTIVE,
    "STATE_RESIZABLE": State.RESIZABLE,
    "STATE_REQUIRED": State.REQUIRED,
    "STATE_INVALID_ENTRY": State.INVALID,
    # Orientation
    "STATE_HORIZONTAL": State.HORIZONTAL,
    "STATE_VERTICAL": State.VERTICAL,
    # Async / Live
    "STATE_BUSY": State.BUSY,
    "STATE_INDETERMINATE": State.INDETERMINATE,
    # Popups
    "STATE_HAS_POPUP": State.HAS_POPUP,
    # Multi-select
    "STATE_MULTISELECTABLE": State.MULTISELECTABLE,
    # Off-screen / Stale
    "STATE_DEFUNCT": State.DEFUNCT,
    "STATE_IS_OFFSCREEN": State.OFFSCREEN,
    # Link history
    "STATE_VISITED": State.VISITED,
}

# ---------------------------------------------------------------------------
# Window roles: AT-SPI2 roles that represent top-level OS windows
# ---------------------------------------------------------------------------
# These are the *role_name* strings returned by ``accessible.get_role_name()``.
# Anything with one of these roles under an application node is treated as a
# window by ``get_windows``.
# ---------------------------------------------------------------------------

_WINDOW_ROLES: set[str] = {"frame", "window", "dialog", "popup menu"}


def _dbus_path_id(acc) -> str:
    """Extract the numeric suffix from an accessible's D-Bus path.

    Every AT-SPI2 accessible has a stable D-Bus object path like
    ``/org/a11y/atspi/accessible/42``.  The trailing integer is
    unique within the owning process and never changes for the
    lifetime of the accessible.

    Returns:
        The path suffix as a string, e.g. ``"42"``.
    """
    return acc.path.rsplit("/", 1)[-1]


class AtSpiBackend(Backend):
    """AT-SPI2 backend for native Linux applications.

    Connects to the AT-SPI2 D-Bus service and queries the accessibility
    tree.  Works with Qt, GTK, Firefox, LibreOffice, and most native
    Linux desktop applications.

    The ``gi.repository.Atspi`` module is imported lazily at init time
    so that the rest of Touchpoint can be imported on any platform.

    Raises:
        BackendUnavailableError: If PyGObject or AT-SPI2 is not
            installed on the system.
    """

    # Alias lists for convenience action helpers (_try_actions).
    # Matching is **case-insensitive** — do_action() compares with
    # .lower() on both sides, so e.g. "press" matches KDE's "Press".
    ACTION_ALIASES: dict[str, list[str]] = {
        "click": ["click", "toggle", "press", "activate", "doDefault"],
        "double_click": ["double_click"],
        "right_click": ["ShowMenu", "show_menu"],
    }

    def __init__(self) -> None:
        self._atspi = _import_atspi()
        # Cache accessible objects during lightweight walks so
        # inflate_element() can build the full Element without
        # re-walking the tree.
        self._acc_refs: dict[str, object] = {}
        # Per-call element counter for enforcing max_elements.
        self._element_count: int = 0
        self._max_elements: int = sys.maxsize
        # Cache (pid, path_id) → (win, app_name, pid) lookups so
        # repeated calls to _find_window_accessible (e.g. multiple
        # do_action / set_value calls) avoid a full desktop scan.
        # Cleared at the start of each get_elements() call.
        self._window_acc_cache: dict[
            tuple[int, str], tuple[object, str, int] | None
        ] = {}
        # Precompute AT-SPI StateType → Touchpoint State mapping
        # so _translate_states avoids per-element getattr lookups.
        self._state_lookup: dict[object, State] = {}
        for atspi_key, tp_state in _ATSPI_STATE_MAP.items():
            enum_name = atspi_key.removeprefix("STATE_")
            atspi_enum = getattr(self._atspi.StateType, enum_name, None)
            if atspi_enum is not None:
                self._state_lookup[atspi_enum] = tp_state
        # Per-walk scale factor set by get_elements(); None means
        # _build_element should derive it from the accessible's app.
        self._walk_scale: float | None = None
        # Cache: AT-SPI app name → effective scale factor.
        # Toolkits like Gecko (Firefox) report physical-pixel
        # coordinates, so their scale factor is 1.0 regardless
        # of the system DPI.
        self._app_scale_cache: dict[str, float] = {}

    # Toolkits whose AT-SPI bridges report coordinates in physical
    # (device) pixels rather than logical (DPI-scaled) pixels.
    # These must NOT be multiplied by the system scale factor.
    _PHYSICAL_COORD_TOOLKITS: frozenset[str] = frozenset({"Gecko"})

    def _scale_for_app(self, app: object) -> float:
        """Return the scale factor to use for elements of *app*.

        Most toolkits (Qt, GTK) report AT-SPI coordinates in logical
        pixels that must be multiplied by the system DPI scale to get
        physical screen coordinates.  Some toolkits (Gecko/Firefox)
        report physical coordinates already — for those we return 1.0.

        Results are cached per app name to avoid repeated D-Bus calls.
        """
        name = ""
        try:
            name = app.get_name() or ""  # type: ignore[union-attr]
        except Exception:
            return get_scale_factor()
        # Only use cache for non-empty names — empty names are
        # ambiguous and could belong to different toolkits.
        if name and name in self._app_scale_cache:
            return self._app_scale_cache[name]
        try:
            toolkit = app.get_toolkit_name() or ""  # type: ignore[union-attr]
        except Exception:
            toolkit = ""
        scale = 1.0 if toolkit in self._PHYSICAL_COORD_TOOLKITS else get_scale_factor()
        if name:
            self._app_scale_cache[name] = scale
        return scale

    # -- Backend interface ------------------------------------------------

    def is_available(self) -> bool:
        """Check if AT-SPI2 is accessible.

        Returns:
            ``True`` if PyGObject and the Atspi typelib are installed.
        """
        return self._atspi is not None

    # -- Backend ABC: routing methods -------------------------------------

    def get_owned_pids(self) -> set[int]:
        """AT-SPI does not own specific PIDs; returns empty set."""
        return set()

    def owns_element(self, element_id: str) -> bool:
        """Return ``True`` if *element_id* belongs to this AT-SPI backend."""
        return isinstance(element_id, str) and element_id.startswith("atspi:")

    def claims_app(self, app_name: str) -> bool:
        """Return ``True`` if *app_name* is a native AT-SPI application."""
        if self._atspi is None:
            return False
        desktop = self._atspi.get_desktop(0)
        app_lower = app_name.lower()
        for i in range(desktop.get_child_count()):
            child = desktop.get_child_at_index(i)
            if child is not None:
                name = (child.get_name() or "").lower()
                if name == app_lower:
                    return True
        return False

    def get_topmost_pid_at(self, x: int, y: int) -> int | None:
        """Return the PID of the topmost window at ``(x, y)``.

        On X11 sessions, uses ``xprop`` to read ``_NET_CLIENT_LIST_STACKING``
        for compositor Z-order and ``xdotool`` to check geometry, giving a
        true stacking-order answer.  Falls back to AT-SPI
        ``get_element_at`` for Wayland sessions or when the X11 tools
        are unavailable.
        """
        result = self._topmost_pid_at_x11(x, y)
        if result is not None:
            return result
        # Wayland / missing xdotool — best-effort via AT-SPI.
        try:
            el = self.get_element_at(x, y)
            if el is not None and el.pid:
                return el.pid
        except Exception:
            pass
        return None

    def _topmost_pid_at_x11(self, x: int, y: int) -> int | None:
        """X11 stacking-order lookup via ``xprop`` + ``xdotool``."""
        import subprocess

        try:
            raw = subprocess.check_output(
                ["xprop", "-root", "-notype", "_NET_CLIENT_LIST_STACKING"],
                text=True, timeout=2, stderr=subprocess.DEVNULL,
            )
        except Exception:
            return None

        # Parse window IDs from xprop output:
        #   _NET_CLIENT_LIST_STACKING: window id # 0x6001e, 0x5400006, ...
        sep = "#" if "#" in raw else ("=" if "=" in raw else None)
        if sep is None:
            return None
        wids_str = raw.split(sep, 1)[1].strip()
        try:
            wids = [
                int(w.strip().rstrip(","), 16)
                for w in wids_str.split(",") if w.strip()
            ]
        except ValueError:
            return None
        if not wids:
            return None

        for wid in reversed(wids):  # topmost last → iterate reversed
            try:
                geom = subprocess.check_output(
                    ["xdotool", "getwindowgeometry", "--shell", str(wid)],
                    text=True, timeout=1, stderr=subprocess.DEVNULL,
                )
            except Exception:
                continue
            gd: dict[str, str] = {}
            for line in geom.strip().splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    gd[k] = v
            try:
                wx = int(gd["X"])
                wy = int(gd["Y"])
                ww = int(gd["WIDTH"])
                wh = int(gd["HEIGHT"])
            except (KeyError, ValueError):
                continue
            if not (wx <= x < wx + ww and wy <= y < wy + wh):
                continue
            try:
                pid_str = subprocess.check_output(
                    ["xdotool", "getwindowpid", str(wid)],
                    text=True, timeout=1, stderr=subprocess.DEVNULL,
                ).strip()
                return int(pid_str)
            except Exception:
                continue
        return None

    def get_applications(self) -> list[str]:
        """List applications visible in the AT-SPI2 tree.

        Derives the list from :meth:`get_windows` so that only
        processes with real top-level windows are included.

        Returns:
            Unique application names (e.g. ``["Firefox", "Konsole"]``).
        """
        return sorted({w.app for w in self.get_windows() if w.app})

    def get_windows(self) -> list[Window]:
        """List all windows from the AT-SPI2 tree.

        Walks each application's direct children and collects those
        whose role is ``frame``, ``window``, or ``dialog`` — the three
        AT-SPI2 roles that represent top-level OS windows.

        Returns:
            List of :class:`~touchpoint.core.window.Window` instances.
        """
        Atspi = self._atspi
        desktop = Atspi.get_desktop(0)
        windows: list[Window] = []

        for app_idx in range(desktop.get_child_count()):
            app = desktop.get_child_at_index(app_idx)
            if app is None:
                continue
            app_name = app.get_name() or ""

            for child_idx in range(app.get_child_count()):
                child = app.get_child_at_index(child_idx)
                if child is None:
                    continue
                if child.get_role_name() not in _WINDOW_ROLES:
                    continue

                # Position and size via the Component interface.
                # AT-SPI returns logical (DPI-scaled) coordinates;
                # multiply by scale factor to get physical pixels.
                # (Some toolkits like Gecko already report physical.)
                try:
                    ext = child.get_extents(Atspi.CoordType.SCREEN)
                    _s = self._scale_for_app(app)
                    position = (round(ext.x * _s), round(ext.y * _s))
                    size = (round(ext.width * _s), round(ext.height * _s))
                except Exception:
                    position = (0, 0)
                    size = (0, 0)

                # Active and visible from the state set.
                state_set = child.get_state_set()
                is_active = state_set.contains(Atspi.StateType.ACTIVE)
                is_visible = state_set.contains(Atspi.StateType.VISIBLE)

                # Raw attributes from the window accessible.
                raw: dict = {}
                try:
                    attrs = child.get_attributes()
                    if attrs:
                        raw = dict(attrs)
                except Exception:
                    pass

                pid = app.get_process_id() or 0
                win_path_id = _dbus_path_id(child)
                windows.append(Window(
                    id=f"atspi:{pid}:{win_path_id}",
                    title=child.get_name() or "",
                    app=app_name,
                    pid=pid,
                    position=position,
                    size=size,
                    is_active=is_active,
                    is_visible=is_visible,
                    raw=raw,
                ))

        return windows

    def get_elements(
        self,
        app: str | None = None,
        window_id: str | None = None,
        tree: bool = False,
        max_depth: int | None = None,
        root_element: str | None = None,
        lightweight: bool = False,
        max_elements: int | None = None,
        role: Role | None = None,
        states: list[State] | None = None,
        named_only: bool = False,
        skip_subtree_roles: set[Role] | None = None,
    ) -> list[Element]:
        """Get UI elements from the AT-SPI2 tree.

        Walks each scoped window's subtree and returns every element
        found.  When ``role``, ``states``, or ``named_only`` are
        provided and ``tree`` is ``False``, non-matching elements
        are skipped during the walk (their children are still
        visited).

        Args:
            app: Only include elements from this application.
            window_id: Only include elements under this window
                (format ``"atspi:{pid}:{dbus_path_id}"``).
            tree: If ``True``, populate each element's ``children``
                list recursively.  If ``False`` (default), return a
                flat list with ``children`` empty.
            max_depth: Maximum depth to walk.  ``0`` returns only
                the immediate children of the root(s), ``1`` includes
                grandchildren, etc.  ``None`` walks everything.
            root_element: Start the walk from this element id
                instead of from the window roots.
            lightweight: If ``True``, build elements with only
                ``name``, ``role``, and ``states`` populated.  Use
                :meth:`inflate_element` to fill in the rest.
                Ignored when ``tree=True``.
            max_elements: Maximum number of elements to collect.
                Normally supplied by :func:`~touchpoint.elements`
                from the global config.  ``None`` imposes no cap.
            role: Early-skip hint — only include elements with this
                role.  Ignored when ``tree=True``.
            states: Early-skip hint — only include elements that
                have **all** of these states.  Ignored when
                ``tree=True``.
            named_only: If ``True``, skip elements with empty or
                whitespace-only names.  Ignored when ``tree=True``.
            skip_subtree_roles: If provided, emit elements with
                these roles but do **not** descend into their
                children.  Prevents walking expensive subtrees
                (e.g. ``Role.DOCUMENT`` in browsers).

        Returns:
            List of :class:`~touchpoint.core.element.Element` instances.
        """
        # Reset per-call element counter and limit.
        self._element_count = 0
        self._max_elements = max_elements if max_elements is not None else sys.maxsize

        # Store filter hints — used by _check_filter() in flat walks.
        # Tree mode ignores these (tree structure requires all nodes).
        self._filter_role: Role | None = role if not tree else None
        self._filter_states: list[State] | None = states if not tree else None
        self._filter_named_only: bool = named_only and not tree

        # Store skip-subtree roles — the walkers will emit elements
        # with these roles but not recurse into their children.
        self._skip_subtree_roles: set[Role] | None = skip_subtree_roles

        # Clear accessible refs from the previous walk — they are stale
        # regardless of walk mode (the previous walk's accessibles may
        # have been destroyed by the application since).
        self._acc_refs.clear()
        # Clear the window-accessible cache — the tree may have changed.
        self._window_acc_cache.clear()

        # Reset per-app walk scale; set per-root in the walk loop.
        self._walk_scale = None

        # lightweight is only for flat walks.
        light = lightweight and not tree

        _build = self._build_light_element if light else self._build_element
        _collect = self._collect_light_flat if light else self._collect_flat

        # -- root_element: resolve and walk from a specific node ----------
        if root_element is not None:
            try:
                acc = self._resolve_element(root_element)
            except ValueError:
                return []
            if acc is None:
                return []
            # Derive app_name / pid / win_id from the id.
            parts = root_element.split(":")
            pid_str, wpath = parts[1], parts[2]
            result = self._find_window_accessible(
                int(pid_str), wpath,
            )
            if result is None:
                return []
            _, app_name, pid = result
            # Set per-app scale for _build_element.
            try:
                self._walk_scale = self._scale_for_app(acc.get_application())
            except Exception:
                self._walk_scale = get_scale_factor()
            # Window id is the first three colon-separated parts.
            win_id = ":".join(parts[:3]) if len(parts) >= 3 else None

            elements: list[Element] = []
            for i in range(acc.get_child_count()):
                if self._element_count >= self._max_elements:
                    break
                child = acc.get_child_at_index(i)
                if child is None:
                    continue
                eid = f"{root_element}.{i}"
                if tree:
                    elements.append(
                        self._to_element_tree(
                            child, app_name, pid, eid,
                            root_element, max_depth, 0,
                            window_id=win_id,
                        )
                    )
                else:
                    pre = self._check_filter(child)
                    if pre is not None:
                        self._element_count += 1
                        elements.append(
                            _build(
                                child, app_name, pid, eid,
                                root_element,
                                window_id=win_id,
                                _pre=pre,
                            )
                        )
                    recurse = max_depth is None or max_depth > 0
                    if recurse and self._skip_subtree_roles is not None:
                        _role = pre[0] if pre is not None else self._translate_role(child)[0]
                        if _role in self._skip_subtree_roles:
                            recurse = False
                    if recurse:
                        _collect(
                            child, app_name, pid, eid,
                            elements, max_depth, 1,
                            window_id=win_id,
                        )
            return elements

        # -- Normal path: walk from window roots -------------------------
        roots = self._get_roots(app, window_id)
        elements = []

        for win_acc, app_name, pid, win_id in roots:
            # Set per-app scale so _build_element uses the cached value.
            try:
                self._walk_scale = self._scale_for_app(
                    win_acc.get_application(),
                )
            except Exception:
                self._walk_scale = get_scale_factor()
            for i in range(win_acc.get_child_count()):
                if self._element_count >= self._max_elements:
                    break
                child = win_acc.get_child_at_index(i)
                if child is None:
                    continue
                eid = f"{win_id}:{i}"
                if tree:
                    elements.append(
                        self._to_element_tree(
                            child, app_name, pid, eid,
                            None, max_depth, 0,
                            window_id=win_id,
                        )
                    )
                else:
                    pre = self._check_filter(child)
                    if pre is not None:
                        self._element_count += 1
                        elements.append(
                            _build(
                                child, app_name, pid, eid,
                                window_id=win_id,
                                _pre=pre,
                            )
                        )
                    recurse = max_depth is None or max_depth > 0
                    if recurse and self._skip_subtree_roles is not None:
                        _role = pre[0] if pre is not None else self._translate_role(child)[0]
                        if _role in self._skip_subtree_roles:
                            recurse = False
                    if recurse:
                        _collect(
                            child, app_name, pid, eid,
                            elements, max_depth, 1,
                            window_id=win_id,
                        )

        return elements

    def get_element_at(self, x: int, y: int) -> Element | None:
        """Get the deepest element at a screen coordinate.

        Collects every window whose bounds contain ``(x, y)``, then
        walks each one's children by bounding-box recursion.  The
        window whose walk reaches the **greatest depth** wins — this
        avoids false negatives from empty desktop overlays or hidden
        tooltip shells that technically contain the point.

        Args:
            x: Horizontal pixel coordinate (screen-absolute, physical).
            y: Vertical pixel coordinate (screen-absolute, physical).

        Returns:
            The deepest :class:`Element` at ``(x, y)``, or ``None``
            if no element is found.
        """
        Atspi = self._atspi
        desktop = Atspi.get_desktop(0)

        # 1. Collect all windows containing (x, y).
        #    Tuple: (pid, win_path_id, app_name, pid, win_accessible)
        candidates: list[tuple[int, str, str, int, object]] = []
        for ai in range(desktop.get_child_count()):
            app_node = desktop.get_child_at_index(ai)
            if app_node is None:
                continue
            a_name = app_node.get_name() or ""
            a_pid = app_node.get_process_id() or 0
            _s = self._scale_for_app(app_node)
            lx = round(x / _s)
            ly = round(y / _s)
            for wi in range(app_node.get_child_count()):
                child = app_node.get_child_at_index(wi)
                if child is None:
                    continue
                try:
                    ext = child.get_extents(Atspi.CoordType.SCREEN)
                    if (ext.x <= lx < ext.x + ext.width
                            and ext.y <= ly < ext.y + ext.height):
                        candidates.append(
                            (a_pid, _dbus_path_id(child),
                             a_name, a_pid, child))
                except Exception:
                    continue

        if not candidates:
            return None

        # 2. Walk each candidate; keep the one with the deepest hit.
        best_depth = -1
        best_node = None
        best_path: list[int] = []
        best_pid = 0
        best_wpath = ""
        best_app = ""

        for c_pid, c_wpath, a_name, a_pid, win_node in candidates:
            # Convert physical coords to this app's logical space.
            _sa = self._scale_for_app(win_node.get_application())
            _lx = round(x / _sa)
            _ly = round(y / _sa)
            current = win_node
            path: list[int] = []
            # depth 0 = window itself; start at -1 so depth 0 wins.
            depth = 0
            if depth > best_depth:
                best_depth = depth
                best_node = win_node
                best_path = []
                best_pid = c_pid
                best_wpath = c_wpath
                best_app = a_name
            while True:
                found_child = False
                try:
                    n = current.get_child_count()
                except Exception:
                    break
                for i in range(n):
                    child = current.get_child_at_index(i)
                    if child is None:
                        continue
                    try:
                        ext = child.get_extents(Atspi.CoordType.SCREEN)
                        if (ext.x <= _lx < ext.x + ext.width
                                and ext.y <= _ly < ext.y + ext.height):
                            path.append(i)
                            current = child
                            found_child = True
                            break
                    except Exception:
                        continue
                if not found_child:
                    break

            depth = len(path)
            if depth > best_depth:
                best_depth = depth
                best_node = current
                best_path = path
                best_pid = c_pid
                best_wpath = c_wpath
                best_app = a_name

        if best_node is None:
            return None

        # 3. Build the element.
        win_id = f"atspi:{best_pid}:{best_wpath}"
        if best_path:
            path_str = ".".join(str(i) for i in best_path)
            element_id = f"atspi:{best_pid}:{best_wpath}:{path_str}"
        else:
            # Window-level hit — no children contained the point,
            # so the window itself is the deepest match.
            element_id = win_id
        return self._build_element(best_node, best_app, best_pid,
                                   element_id, window_id=win_id)

    def get_element_by_id(self, element_id: str) -> Element | None:
        """Retrieve a single element by its AT-SPI2 path id.

        Navigates the tree to the accessible at *element_id* and
        returns a fresh :class:`Element` snapshot.

        Args:
            element_id: The element's id (e.g.
                ``"atspi:2269:1:2.1.0"``).

        Returns:
            The :class:`Element` if found, ``None`` otherwise.
        """
        acc = self._resolve_element(element_id)
        if acc is None:
            return None

        # Derive app_name / pid from the id parts.
        parts = element_id.split(":")
        pid = int(parts[1])
        result = self._find_window_accessible(pid, parts[2])
        app_name = result[1] if result else ""

        # Parent id: everything up to the last '.' in the child path.
        parent_id: str | None = None
        if len(parts) >= 4 and "." in parts[3]:
            parent_id = element_id.rsplit(".", 1)[0]

        win_id = ":".join(parts[:3])
        return self._build_element(acc, app_name, pid, element_id, parent_id,
                                   window_id=win_id, detail=True)

    def do_action(self, element_id: str, action: str) -> bool:
        """Perform an action on an element via AT-SPI2.

        Navigates to the element by parsing its ID path, then searches
        the element's supported actions for a match and invokes it.

        Args:
            element_id: The target element's id (e.g.
                ``"atspi:2269:1:2.1.0"``).
            action: Action name (e.g. ``"click"``, ``"activate"``).

        Returns:
            ``True`` if the action was found and executed.

        Raises:
            ActionFailedError: If the element cannot be found or the
                action is not supported.
        """
        acc = self._resolve_element(element_id)
        if acc is None:
            raise ActionFailedError(
                action=action,
                element_id=element_id,
                reason="element not found in the accessibility tree",
            )

        # Find the action by name and invoke it.
        try:
            n_actions = acc.get_n_actions()
        except Exception:
            n_actions = 0

        for i in range(n_actions):
            if acc.get_action_name(i).lower() == action.lower():
                try:
                    return acc.do_action(i)
                except Exception:
                    # D-Bus may timeout when the action opens a modal
                    # dialog (e.g. Save As) or otherwise blocks the
                    # reply.  The action was still dispatched.
                    return True

        raise ActionFailedError(
            action=action,
            element_id=element_id,
            reason=f"action {action!r} not supported, "
                   f"available: {self._get_action_names(acc)}",
        )

    def set_value(self, element_id: str, value: str, replace: bool) -> bool:
        """Set text on an editable element via AT-SPI2.

        Uses the ``EditableText`` interface when available.  In
        *replace* mode, calls ``set_text_contents()``.  In insert
        mode, calls ``insert_text()`` at the current caret position.

        Args:
            element_id: The target element's id (e.g.
                ``"atspi:2269:1:2.1.0"``).
            value: The text to write.
            replace: If ``True``, replace the entire field content.
                If ``False``, insert at the current cursor position.

        Returns:
            ``True`` if the text was set successfully.

        Raises:
            ActionFailedError: If the element cannot be found or
                does not support text editing.
        """
        acc = self._resolve_element(element_id)
        if acc is None:
            raise ActionFailedError(
                action="set_value",
                element_id=element_id,
                reason="element not found in the accessibility tree",
            )

        Atspi = self._atspi

        # Check if the element implements EditableText.
        try:
            iface = acc.get_editable_text_iface()
        except Exception:
            iface = None

        if iface is None:
            raise ActionFailedError(
                action="set_value",
                element_id=element_id,
                reason="element does not support the EditableText interface",
            )

        try:
            if replace:
                return iface.set_text_contents(value)

            # Insert at caret position.
            try:
                caret = acc.get_text_iface().get_caret_offset()
            except Exception:
                # Caret unknown — fall back to character count (end of text).
                # Last resort: -1 is the AT-SPI2 convention for "end."
                try:
                    caret = acc.get_character_count()
                except Exception:
                    caret = -1
            return iface.insert_text(caret, value, len(value))
        except Exception as exc:
            raise ActionFailedError(
                action="set_value",
                element_id=element_id,
                reason=str(exc),
            ) from exc

    def set_numeric_value(
        self, element_id: str, value: float,
    ) -> bool:
        """Set a numeric value via AT-SPI2's Value interface.

        Used for sliders, spinboxes, progress bars, and other
        range-valued controls.

        Args:
            element_id: The target element's id.
            value: The numeric value to set.

        Returns:
            ``True`` if the value was set successfully.

        Raises:
            ActionFailedError: If the element cannot be found or
                does not support the Value interface.
        """
        acc = self._resolve_element(element_id)
        if acc is None:
            raise ActionFailedError(
                action="set_numeric_value",
                element_id=element_id,
                reason="element not found in the accessibility tree",
            )

        try:
            iface = acc.get_value_iface()
        except Exception:
            iface = None

        if iface is None:
            raise ActionFailedError(
                action="set_numeric_value",
                element_id=element_id,
                reason="element does not support the Value interface",
            )

        try:
            return iface.set_current_value(value)
        except Exception as exc:
            raise ActionFailedError(
                action="set_numeric_value",
                element_id=element_id,
                reason=str(exc),
            ) from exc

    def focus_element(self, element_id: str) -> bool:
        """Move keyboard focus to an element via AT-SPI2.

        Tries ``Component.grab_focus()`` first (the standard
        AT-SPI2 mechanism), then falls back to invoking a
        focus-like action (``SetFocus``, ``focus``) from the
        Action interface.

        Args:
            element_id: The target element's id.

        Returns:
            ``True`` if focus was moved.

        Raises:
            ActionFailedError: If the element cannot be found or
                cannot receive focus.
        """
        acc = self._resolve_element(element_id)
        if acc is None:
            raise ActionFailedError(
                action="focus",
                element_id=element_id,
                reason="element not found in the accessibility tree",
            )

        # Primary: Component.grab_focus() — the standard way.
        try:
            comp = acc.get_component_iface()
            if comp is not None:
                result = comp.grab_focus()
                if result:
                    return True
        except Exception:
            pass

        # Fallback: Action interface (Qt uses "SetFocus").
        focus_lower = {"setfocus", "focus", "grab_focus"}
        try:
            n_actions = acc.get_n_actions()
        except Exception:
            n_actions = 0

        for i in range(n_actions):
            if acc.get_action_name(i).lower() in focus_lower:
                try:
                    return acc.do_action(i)
                except Exception:
                    return True  # dispatched despite D-Bus timeout

        raise ActionFailedError(
            action="focus",
            element_id=element_id,
            reason="element does not support focus "
                   "(no Component interface and no focus action)",
        )

    def activate_window(self, window_id: str) -> bool:
        """Bring a window to the foreground via AT-SPI2.

        Resolves the window's accessible from *window_id*, then
        tries an ``activate`` / ``raise`` action.  Falls back to
        ``Component.grab_focus()`` if no action is available.

        Args:
            window_id: Format ``"atspi:{pid}:{dbus_path_id}"``.

        Returns:
            ``True`` if the window was activated.
        """
        parts = self._parse_id(window_id)
        if len(parts) < 3:
            return False

        result = self._find_window_accessible(
            int(parts[1]), parts[2],
        )
        if result is None:
            return False
        win_node = result[0]

        # Try activation-related actions on the window accessible.
        _activate_names = {"activate", "raise", "focus"}
        try:
            n_actions = win_node.get_n_actions()
            for i in range(n_actions):
                if win_node.get_action_name(i).lower() in _activate_names:
                    try:
                        win_node.do_action(i)
                        return True
                    except Exception:
                        return True  # dispatched despite D-Bus timeout
        except Exception:
            pass

        # Fallback: Component.grab_focus().
        try:
            comp = win_node.get_component_iface()
            if comp is not None and comp.grab_focus():
                return True
        except Exception:
            pass

        return False

    # -- Private helpers --------------------------------------------------

    def _parse_id(self, id_str: str) -> list[str]:
        """Validate and split an AT-SPI2 element / window ID.

        ID format:
            ``"atspi:{pid}:{dbus_path_id}"`` for windows, or
            ``"atspi:{pid}:{dbus_path_id}:{child.path}"`` for
            elements.  *pid* is the OS process ID and
            *dbus_path_id* is the numeric suffix from the window
            accessible's D-Bus object path.

        Returns:
            The colon-split parts list (e.g.
            ``['atspi', '2269', '1', '2.1.0']``).

        Raises:
            ValueError: If any numeric component is not a valid integer.
        """
        parts = id_str.split(":")
        for idx in (1, 2):
            if idx < len(parts):
                try:
                    int(parts[idx])
                except ValueError:
                    raise ValueError(
                        f"Malformed element ID: {id_str!r}"
                    ) from None
        if len(parts) >= 4 and parts[3]:
            for seg in parts[3].split("."):
                try:
                    int(seg)
                except ValueError:
                    raise ValueError(
                        f"Malformed element ID: {id_str!r}"
                    ) from None
        return parts

    def _get_roots(
        self,
        app: str | None,
        window_id: str | None,
    ) -> list[tuple]:
        """Find window accessibles to walk based on scoping params.

        Returns:
            List of ``(win_accessible, app_name, pid, win_id)`` tuples.
        """
        Atspi = self._atspi
        desktop = Atspi.get_desktop(0)
        roots: list[tuple] = []

        if window_id is not None:
            parts = self._parse_id(window_id)
            result = self._find_window_accessible(
                int(parts[1]), parts[2],
            )
            if result is None:
                return roots
            win_node, app_name_r, pid_r = result
            roots.append((
                win_node,
                app_name_r,
                pid_r,
                window_id,
            ))
            return roots

        # All windows, optionally filtered by application name
        # (case-insensitive — AT-SPI2 app names vary in casing).
        app_lower = app.lower() if app is not None else None
        for ai in range(desktop.get_child_count()):
            app_node = desktop.get_child_at_index(ai)
            if app_node is None:
                continue
            name = app_node.get_name() or ""
            if app_lower is not None and name.lower() != app_lower:
                continue
            pid = app_node.get_process_id() or 0
            for wi in range(app_node.get_child_count()):
                win = app_node.get_child_at_index(wi)
                if win is None or win.get_role_name() not in _WINDOW_ROLES:
                    continue
                roots.append((
                    win, name, pid,
                    f"atspi:{pid}:{_dbus_path_id(win)}",
                ))

        return roots

    def _find_window_accessible(
        self,
        pid: int,
        path_id: str,
    ) -> tuple[object, str, int] | None:
        """Find a window accessible by PID and D-Bus path suffix.

        Scans the desktop's application children for one whose PID
        matches, then scans that application's children for a window
        whose D-Bus object path ends with *path_id*.

        Results are cached in ``_window_acc_cache`` so repeated
        lookups (e.g. multiple actions on elements in the same
        window) avoid a full desktop scan.  The cache is cleared
        at the start of each ``get_elements()`` call.

        Args:
            pid: The OS process ID.
            path_id: The numeric suffix from the window's D-Bus
                object path (e.g. ``"1"``, ``"2147483651"``).

        Returns:
            ``(win_accessible, app_name, pid)`` or ``None``.
        """
        cache_key = (pid, path_id)
        if cache_key in self._window_acc_cache:
            return self._window_acc_cache[cache_key]

        desktop = self._atspi.get_desktop(0)
        for ai in range(desktop.get_child_count()):
            app_node = desktop.get_child_at_index(ai)
            if app_node is None:
                continue
            if (app_node.get_process_id() or 0) != pid:
                continue
            # Found the app — scan its windows.
            for wi in range(app_node.get_child_count()):
                win = app_node.get_child_at_index(wi)
                if win is None:
                    continue
                if _dbus_path_id(win) == path_id:
                    result = (
                        win,
                        app_node.get_name() or "",
                        pid,
                    )
                    self._window_acc_cache[cache_key] = result
                    return result
            # PID matched but no window had the right path — keep
            # searching in case another app-node shares this PID.
        # Don't cache misses — the window may appear between actions
        # (e.g. a dialog opening).  Only hits are cached.
        return None

    def _resolve_element(self, element_id: str):
        """Navigate the AT-SPI2 tree to the accessible at *element_id*.

        ID format:
            ``"atspi:{pid}:{dbus_path_id}"`` — window-level, or
            ``"atspi:{pid}:{dbus_path_id}:{child.path}"`` — element.

        Returns:
            The ``Atspi.Accessible`` at that path, or ``None``.
        """
        parts = self._parse_id(element_id)
        if len(parts) < 3:
            raise ValueError(f"Malformed element ID: {element_id!r}")

        result = self._find_window_accessible(
            int(parts[1]), parts[2],
        )
        if result is None:
            return None
        win_node = result[0]

        # 3-part ID → window itself.
        if len(parts) < 4 or not parts[3]:
            return win_node

        # Walk the child path (e.g. "2.1.0").
        current = win_node
        for idx_str in parts[3].split("."):
            child = current.get_child_at_index(int(idx_str))
            if child is None:
                return None
            current = child
        return current

    @staticmethod
    def _get_action_names(acc) -> list[str]:
        """Return the list of action names an accessible supports."""
        names: list[str] = []
        try:
            for i in range(acc.get_n_actions()):
                name = acc.get_action_name(i)
                if name:
                    names.append(name)
        except Exception:
            pass
        return names

    def _translate_role(self, acc) -> tuple[Role, str]:
        """Map an accessible's role to ``(Role, raw_role_string)``."""
        raw = acc.get_role_name()
        key = f"ROLE_{raw.upper().replace(' ', '_')}"
        return _ATSPI_ROLE_MAP.get(key, Role.UNKNOWN), raw

    def _translate_states(self, state_set) -> list[State]:
        """Map an AT-SPI2 ``StateSet`` to a list of :class:`State`."""
        return [
            tp_state
            for atspi_enum, tp_state in self._state_lookup.items()
            if state_set.contains(atspi_enum)
        ]

    def _check_filter(
        self, acc,
    ) -> tuple[Role, str, list["State"], str] | None:
        """Check *acc* against the active filter hints.

        Returns ``None`` when the element should be **skipped**
        (role or states mismatch).  Otherwise returns the
        already-translated ``(role, raw_role, states, name)`` tuple
        so callers can feed them into ``_build_element`` /
        ``_build_light_element`` without re-fetching from D-Bus.

        When no filter hints are active, the returned tuple still
        contains the fully computed role, states, and name — the
        builder uses them directly and skips redundant D-Bus calls.

        Called before the builder so that non-matching elements are
        never materialised.  The caller must still recurse into the
        accessible's children — a non-matching parent may contain
        matching descendants.
        """
        role: Role | None = None
        raw_role: str | None = None
        states: list[State] | None = None
        name: str | None = None

        if self._filter_named_only:
            name = acc.get_name() or ""
            if not name.strip():
                return None

        if self._filter_role is not None:
            role, raw_role = self._translate_role(acc)
            if role != self._filter_role:
                return None
        if self._filter_states:
            states = self._translate_states(acc.get_state_set())
            if not all(s in states for s in self._filter_states):
                return None

        # No filters active, or element passed — return what we have.
        if role is None:
            role, raw_role = self._translate_role(acc)
        if states is None:
            states = self._translate_states(acc.get_state_set())
        assert raw_role is not None  # guaranteed by _translate_role

        # Final sanity check: if the element isn't showing, skip it.
        if State.SHOWING not in states:
            return None

        # Fetch name now (if not already fetched by named_only filter)
        # so the builder doesn't need a redundant D-Bus call.
        if name is None:
            name = acc.get_name() or ""

        return role, raw_role, states, name


    # -----------------------------------------------------------------
    # Lightweight element building (for find() optimisation)
    # -----------------------------------------------------------------

    def _build_light_element(
        self,
        acc,
        app_name: str,
        pid: int,
        element_id: str,
        parent_id: str | None = None,
        window_id: str | None = None,
        _pre: tuple[Role, str, list["State"], str] | None = None,
    ) -> Element:
        """Build a lightweight :class:`Element` — only name, role, states.

        Skips the expensive D-Bus calls for position, size, actions,
        value, description, and raw attributes.  The accessible
        reference is stored in :attr:`_acc_refs` so that
        :meth:`inflate_element` can fill in the rest later.

        Args:
            _pre: Pre-computed ``(role, raw_role, states, name)`` from
                :meth:`_check_filter`.  Avoids redundant D-Bus calls.
        """
        self._acc_refs[element_id] = acc
        if _pre is not None:
            role, raw_role, states, name = _pre
        else:
            role, raw_role = self._translate_role(acc)
            states = self._translate_states(acc.get_state_set())
            name = acc.get_name() or ""
        return Element(
            id=element_id,
            name=name,
            role=role,
            states=states,
            position=(0, 0),
            size=(0, 0),
            app=app_name,
            pid=pid,
            backend="atspi",
            raw_role=raw_role,
            parent_id=parent_id,
            window_id=window_id,
        )

    def _collect_light_flat(
        self,
        acc,
        app_name: str,
        pid: int,
        parent_id: str,
        out: list[Element],
        max_depth: int | None = None,
        current_depth: int = 0,
        window_id: str | None = None,
    ) -> None:
        """Like :meth:`_collect_flat` but builds lightweight elements."""
        if self._element_count >= self._max_elements:
            return
        try:
            n_children = acc.get_child_count()
        except Exception:
            return
        for i in range(n_children):
            if self._element_count >= self._max_elements:
                break
            child = acc.get_child_at_index(i)
            if child is None:
                continue
            child_id = f"{parent_id}.{i}"
            pre = self._check_filter(child)
            if pre is not None:
                self._element_count += 1
                out.append(
                    self._build_light_element(
                        child, app_name, pid, child_id, parent_id,
                        window_id=window_id,
                        _pre=pre,
                    )
                )
            if max_depth is None or current_depth < max_depth:
                recurse = True
                if self._skip_subtree_roles is not None:
                    _role = pre[0] if pre is not None else self._translate_role(child)[0]
                    if _role in self._skip_subtree_roles:
                        recurse = False
                if recurse:
                    self._collect_light_flat(
                        child, app_name, pid, child_id, out,
                        max_depth, current_depth + 1,
                        window_id=window_id,
                    )

    def inflate_element(self, element: Element) -> Element:
        """Inflate a lightweight element into a fully populated one.

        Looks up the AT-SPI2 accessible cached during the lightweight
        walk and performs the remaining D-Bus calls (position, size,
        actions, value, description, raw attributes).

        If the accessible is no longer cached (e.g. a full walk was
        done since), falls back to :meth:`get_element_by_id`.
        """
        acc = self._acc_refs.get(element.id)
        if acc is None:
            return self.get_element_by_id(element.id) or element

        try:
            return self._build_element(
                acc, element.app, element.pid, element.id,
                element.parent_id, window_id=element.window_id,
                detail=True,
            )
        except Exception:
            # Accessible went stale (D-Bus object removed).
            self._acc_refs.pop(element.id, None)
            return self.get_element_by_id(element.id) or element

    def _build_element(
        self,
        acc,
        app_name: str,
        pid: int,
        element_id: str,
        parent_id: str | None = None,
        window_id: str | None = None,
        _pre: tuple[Role, str, list["State"], str] | None = None,
        detail: bool = False,
    ) -> Element:
        """Build an :class:`Element` from an AT-SPI2 accessible.

        Args:
            _pre: Pre-computed ``(role, raw_role, states, name)`` from
                :meth:`_check_filter`.  Avoids redundant D-Bus calls.
            detail: If ``True``, also fetch ``description`` and ``raw``
                attributes.  Skipped during bulk walks for speed.
        """
        Atspi = self._atspi
        if _pre is not None:
            role, raw_role, states, name = _pre
        else:
            role, raw_role = self._translate_role(acc)
            states = self._translate_states(acc.get_state_set())
            name = acc.get_name() or ""

        # Position (center of bounding box) and size.
        # AT-SPI returns logical pixels; convert to physical for
        # the public API.  Some toolkits (Gecko) already report
        # physical pixels — _scale_for_app handles this.
        try:
            ext = acc.get_extents(Atspi.CoordType.SCREEN)
            _s = self._walk_scale
            if _s is None:
                try:
                    _s = self._scale_for_app(acc.get_application())
                except Exception:
                    _s = get_scale_factor()
            position = (
                round((ext.x + ext.width / 2) * _s),
                round((ext.y + ext.height / 2) * _s),
            )
            size = (round(ext.width * _s), round(ext.height * _s))
        except Exception:
            position = (0, 0)
            size = (0, 0)

        # Actions from the Action interface.
        actions: list[str] = []
        try:
            for i in range(acc.get_n_actions()):
                action_name = acc.get_action_name(i)
                if action_name:
                    actions.append(action_name)
        except Exception:
            pass

        # Interfaces reported by the accessible (used to guard
        # Text / Value extraction below).
        ifaces = acc.get_interfaces()

        # Value: prefer Text interface content, fall back to
        # numeric Value for roles where it is meaningful.
        value: str | None = None
        if "Text" in ifaces:
            try:
                count = acc.get_character_count()
                if count > 0:
                    # Use the explicit class method — the instance
                    # method is shadowed by Accessible.get_text in
                    # newer PyGObject and silently returns None.
                    value = Atspi.Text.get_text(acc, 0, count)
                else:
                    # Empty text field — return "" so callers can
                    # distinguish "cleared" from "never had a value".
                    value = ""
            except Exception:
                pass
        if value is None and "Value" in ifaces:
            try:
                v = acc.get_current_value()
                if v is not None:
                    value = str(v)
            except Exception:
                pass

        # Description and raw attributes — only fetched for detail
        # mode (single-element lookups / inflate) to save D-Bus calls
        # during bulk walks.
        description: str | None = None
        raw: dict = {}
        if detail:
            description = acc.get_description() or None
            try:
                attrs = acc.get_attributes()
                if attrs:
                    raw = dict(attrs)
            except Exception:
                pass

        return Element(
            id=element_id,
            name=name,
            role=role,
            states=states,
            position=position,
            size=size,
            app=app_name,
            pid=pid,
            backend="atspi",
            raw_role=raw_role,
            actions=actions,
            value=value,
            description=description,
            parent_id=parent_id,
            window_id=window_id,
            raw=raw,
        )

    def _collect_flat(
        self,
        acc,
        app_name: str,
        pid: int,
        parent_id: str,
        out: list[Element],
        max_depth: int | None = None,
        current_depth: int = 0,
        window_id: str | None = None,
    ) -> None:
        """Recursively collect descendants into a flat list.

        Args:
            max_depth: Stop recursing beyond this depth.  ``None``
                imposes no depth limit.
            current_depth: How deep we are from the starting point.
            window_id: The window id to attach to every element.
        """
        if self._element_count >= self._max_elements:
            return
        try:
            n_children = acc.get_child_count()
        except Exception:
            return
        for i in range(n_children):
            if self._element_count >= self._max_elements:
                break
            child = acc.get_child_at_index(i)
            if child is None:
                continue
            child_id = f"{parent_id}.{i}"
            pre = self._check_filter(child)
            if pre is not None:
                self._element_count += 1
                out.append(
                    self._build_element(
                        child, app_name, pid, child_id, parent_id,
                        window_id=window_id,
                        _pre=pre,
                    )
                )
            recurse = max_depth is None or current_depth < max_depth
            if recurse and self._skip_subtree_roles is not None:
                _role = pre[0] if pre is not None else self._translate_role(child)[0]
                if _role in self._skip_subtree_roles:
                    recurse = False
            if recurse:
                self._collect_flat(
                    child, app_name, pid, child_id, out,
                    max_depth, current_depth + 1,
                    window_id=window_id,
                )

    def _to_element_tree(
        self,
        acc,
        app_name: str,
        pid: int,
        element_id: str,
        parent_id: str | None,
        max_depth: int | None = None,
        current_depth: int = 0,
        window_id: str | None = None,
    ) -> Element:
        """Recursively build an Element with children populated.

        Args:
            max_depth: Stop recursing beyond this depth.  ``None``
                imposes no depth limit.
            current_depth: How deep we are from the starting point.
            window_id: The window id to attach to every element.
        """
        element = self._build_element(
            acc, app_name, pid, element_id, parent_id,
            window_id=window_id,
        )
        self._element_count += 1
        if max_depth is not None and current_depth >= max_depth:
            return element
        if self._element_count >= self._max_elements:
            return element
        # If this element's role is in _skip_subtree_roles, emit it
        # but don't descend into its children.
        if (self._skip_subtree_roles is not None
                and element.role in self._skip_subtree_roles):
            return element
        for i in range(acc.get_child_count()):
            if self._element_count >= self._max_elements:
                break
            child = acc.get_child_at_index(i)
            if child is None:
                continue
            child_id = f"{element_id}.{i}"
            element.children.append(
                self._to_element_tree(
                    child, app_name, pid, child_id, element_id,
                    max_depth, current_depth + 1,
                    window_id=window_id,
                )
            )
        return element


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _import_atspi():
    """Try to import ``gi.repository.Atspi``.

    Returns:
        The ``Atspi`` module if available, ``None`` otherwise.
    """
    try:
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi

        return Atspi
    except (ImportError, ValueError):
        return None

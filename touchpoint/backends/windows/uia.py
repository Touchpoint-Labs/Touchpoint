"""Windows UI Automation backend for Touchpoint.

Uses the Windows UI Automation (UIA) COM interface via ``comtypes``
to read the accessibility tree exposed by Win32, WPF, UWP,
WinForms, and most native Windows desktop applications.

This is the primary backend on Windows.  Electron/Chromium apps
that only expose a few elements via UIA should use the CDP backend
instead.

Requires:
    - Python package: ``comtypes`` (installed automatically with ``pip install touchpoint-py``)
    - Windows 7+ (UIA is built into the OS)

Usage::

    from touchpoint.backends.windows import UiaBackend

    backend = UiaBackend()
    if backend.is_available():
        apps = backend.get_applications()
        windows = backend.get_windows()
        elements = backend.get_elements(app="Notepad")
"""

from __future__ import annotations

import sys
from typing import Any

from touchpoint.backends.base import Backend
from touchpoint.core.element import Element
from touchpoint.core.exceptions import ActionFailedError
from touchpoint.core.types import Role, State
from touchpoint.core.window import Window

# ---------------------------------------------------------------------------
# Role mapping: UIA ControlType IDs → Touchpoint Role
# ---------------------------------------------------------------------------
# UIA control types are identified by integer IDs (e.g. 50000 = Button).
# We map them to our unified Role enum.  Anything not in this dict
# becomes Role.UNKNOWN with the original preserved in Element.raw_role.
#
# Reference: https://learn.microsoft.com/en-us/windows/win32/winauto/
#            uiauto-controltype-ids
# ---------------------------------------------------------------------------

_UIA_ROLE_MAP: dict[int, Role] = {
    # Containers / Structure
    50033: Role.APPLICATION,      # AppBar (closest UIA equivalent)
    50032: Role.WINDOW,           # Window
    50034: Role.PANEL,            # Pane — generic container

    # Interactive
    50000: Role.BUTTON,           # Button
    50001: Role.GROUP,            # Calendar (container, no exact match)
    50002: Role.CHECK_BOX,        # CheckBox
    50003: Role.COMBO_BOX,        # ComboBox
    50004: Role.TEXT_FIELD,        # Edit
    50005: Role.LINK,             # Hyperlink
    50006: Role.IMAGE,            # Image
    50007: Role.LIST_ITEM,        # ListItem
    50008: Role.LIST,             # List
    50009: Role.MENU,             # Menu
    50010: Role.MENU_BAR,         # MenuBar
    50011: Role.MENU_ITEM,        # MenuItem
    50012: Role.PROGRESS_BAR,     # ProgressBar
    50013: Role.RADIO_BUTTON,     # RadioButton
    50014: Role.SCROLL_BAR,       # ScrollBar
    50015: Role.SLIDER,           # Slider
    50016: Role.SPIN_BUTTON,      # Spinner
    50017: Role.STATUS_BAR,       # StatusBar
    50018: Role.TAB_LIST,         # Tab (the tab strip / container)
    50019: Role.TAB,              # TabItem (individual tab page)
    50020: Role.TEXT,             # Text (static label)
    50021: Role.TOOLBAR,          # ToolBar
    50022: Role.TOOLTIP,          # ToolTip
    50023: Role.TREE,             # Tree
    50024: Role.TREE_ITEM,        # TreeItem
    50025: Role.GROUP,            # Custom (catch-all)
    50026: Role.GROUP,            # Group
    # 50027 (Thumb) intentionally omitted — falls to UNKNOWN.
    50028: Role.TABLE,            # DataGrid
    50029: Role.TABLE_CELL,       # DataItem
    50030: Role.DOCUMENT,         # Document
    50031: Role.SPLIT_BUTTON,     # SplitButton
    50035: Role.HEADER,           # Header
    50036: Role.TABLE_COLUMN_HEADER,  # HeaderItem
    50037: Role.TABLE,            # Table
    50038: Role.TITLE_BAR,        # TitleBar
    50039: Role.SEPARATOR,        # Separator

    # Windows 8+ additions
    50040: Role.PANEL,            # SemanticZoom
    50041: Role.APPLICATION,      # AppBar
}

# Roles that imply CLICKABLE state (consistent with CDP / AT-SPI).
_CLICKABLE_ROLES = frozenset({
    Role.BUTTON, Role.LINK, Role.MENU_ITEM,
    Role.TOGGLE_BUTTON, Role.SWITCH, Role.SPLIT_BUTTON,
    Role.COMBO_BOX, Role.TAB,
})

# ---------------------------------------------------------------------------
# State mapping: UIA property values → Touchpoint State
# ---------------------------------------------------------------------------
# Unlike AT-SPI2, UIA doesn't return a set of states directly.  Instead
# we query individual boolean/pattern properties on each element and
# assemble the State list ourselves.
#
# This is done procedurally in _translate_states() below; the dict
# here documents the mapping for reference but isn't used as a
# lookup table.
# ---------------------------------------------------------------------------

# UIA ControlType IDs for window detection
_UIA_WINDOW_CONTROL_TYPE = 50032  # Window

# Top-level control types that represent application windows.
# Window (50032) is the standard.  Pane (50034) covers UWP apps.
# AppBar (50033) covers Electron/Chromium apps that report their
# top-level as AppBar instead of Window, plus system chrome like
# the Taskbar and Program Manager (desktop) — agents need these
# for launching apps, switching windows, and interacting with
# desktop icons, matching Linux AT-SPI behavior.
_UIA_TOPLEVEL_TYPES = (50032, 50033, 50034)

# Transient popup types that appear as top-level desktop children
# but are not application windows.  Context menus and popup menus
# surface as Menu (50009) at the desktop root.  These bypass the
# WS_EX_TOOLWINDOW / WS_EX_NOACTIVATE style filter because popups
# normally carry those styles by design.
_UIA_POPUP_TYPES = (50009,)  # Menu


class UiaBackend(Backend):
    """Windows UI Automation backend via ``comtypes``.

    Connects to the UIA COM server and queries the automation tree.
    Works with Win32, WPF, UWP, WinForms, and most native Windows
    desktop applications.

    The ``comtypes`` module and UIA interfaces are imported lazily
    at init time so that the rest of Touchpoint can be imported on
    any platform.

    Raises:
        BackendUnavailableError: If ``comtypes`` is not installed
            or UIA is not accessible.
    """

    # -- Action aliases ---------------------------------------------------
    # UIA doesn't use named "actions" like AT-SPI2.  Instead, it has
    # control patterns (Invoke, Toggle, ExpandCollapse, etc.).
    # We map our convenience names to the pattern methods we'll try
    # in do_action().
    #
    # The actual pattern dispatch is in do_action() — these aliases
    # tell _try_actions() which names to pass.
    ACTION_ALIASES: dict[str, list[str]] = {
        "click": ["invoke", "toggle", "select"],
        "double_click": [],  # no native double-click — fallback only
        "right_click": [],  # no UIA pattern for context menu — fallback only
    }

    def __init__(self) -> None:
        # Lazy-import everything UIA-related.  On non-Windows or
        # without comtypes, _uia will be None.
        self._uia: Any | None = None        # IUIAutomation interface
        self._root: Any | None = None        # Desktop root element
        self._module: Any | None = None      # comtypes UIA module reference
        self._runtime_map: dict[str, Any] = {}  # element_id → IUIAutomationElement
        self._uwp_names: dict[int, str] = {}     # ApplicationFrameHost PID → real app name
        self._element_count: int = 0             # per-call element counter
        self._max_elements: int = sys.maxsize      # per-call limit

        if sys.platform != "win32":
            return

        try:
            self._uia, self._root, self._module = _init_uia()
        except Exception:
            # comtypes not installed, or UIA init failed.
            pass

    # -- Availability -----------------------------------------------------

    def is_available(self) -> bool:
        """Check if UIA is accessible.

        Returns:
            ``True`` if comtypes and UIA are available on this
            Windows system.
        """
        return self._uia is not None and self._root is not None

    # -- UWP app name resolution ------------------------------------------

    def _resolve_app_name(self, uia_el: Any, pid: int) -> str:
        """Return the display app name for a top-level UIA element.

        For UWP apps hosted by ApplicationFrameHost, walks the element's
        immediate children to find the real app process and caches the
        result in ``_uwp_names``.  For all other apps, just returns the
        process name.
        """
        name = _get_process_name(pid) or ""
        if name != "ApplicationFrameHost":
            return name
        # Check cache first.
        cached = self._uwp_names.get(pid)
        if cached:
            return cached
        # Resolve by walking children for a different PID.
        if self._uia is not None:
            real = _resolve_uwp_name(
                self._uia.ControlViewWalker, uia_el, pid,
            )
            if real:
                self._uwp_names[pid] = real
                return real
        return name

    def _resolve_app_name_by_pid(self, pid: int) -> str:
        """Resolve app name from PID alone (no UIA element available).

        Uses the ``_uwp_names`` cache for ApplicationFrameHost PIDs
        that were previously resolved by :meth:`_resolve_app_name`.
        """
        name = _get_process_name(pid) or ""
        if name == "ApplicationFrameHost":
            return self._uwp_names.get(pid, name)
        return name

    # -- Backend ABC: routing methods -------------------------------------

    def get_owned_pids(self) -> set[int]:
        """UIA does not own specific PIDs; returns empty set."""
        return set()

    def owns_element(self, element_id: str) -> bool:
        """Return ``True`` if *element_id* belongs to this UIA backend."""
        return isinstance(element_id, str) and element_id.startswith("uia:")

    def claims_app(self, app_name: str) -> bool:
        """Return ``True`` if *app_name* is a native UIA application."""
        app_lower = app_name.lower()
        return any(w.app.lower() == app_lower for w in self.get_windows())

    def get_topmost_pid_at(self, x: int, y: int) -> int | None:
        """Return the PID of the topmost window at ``(x, y)``.

        Uses ``user32.WindowFromPoint`` + ``GetWindowThreadProcessId``,
        which is Z-order-aware and returns the correct result even when
        transparent or layered windows overlap the target point.
        """
        try:
            import ctypes
            import ctypes.wintypes

            class POINT(ctypes.Structure):  # noqa: PLC0115
                _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

            _WindowFromPoint = ctypes.windll.user32.WindowFromPoint  # type: ignore[attr-defined]
            _WindowFromPoint.argtypes = [POINT]
            _WindowFromPoint.restype = ctypes.wintypes.HWND

            _GetWindowThreadProcessId = ctypes.windll.user32.GetWindowThreadProcessId  # type: ignore[attr-defined]
            _GetWindowThreadProcessId.argtypes = [
                ctypes.wintypes.HWND,
                ctypes.POINTER(ctypes.wintypes.DWORD),
            ]
            _GetWindowThreadProcessId.restype = ctypes.wintypes.DWORD

            # Walk up to the root owner — WindowFromPoint may return a child
            # control inside the client area, not the top-level window.
            _GetAncestor = ctypes.windll.user32.GetAncestor  # type: ignore[attr-defined]
            _GetAncestor.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.UINT]
            _GetAncestor.restype = ctypes.wintypes.HWND
            GA_ROOT = 2

            pt = POINT(x, y)
            hwnd = _WindowFromPoint(pt)
            if not hwnd:
                return None
            root_hwnd = _GetAncestor(hwnd, GA_ROOT)
            if root_hwnd:
                hwnd = root_hwnd
            pid = ctypes.wintypes.DWORD(0)
            _GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            return int(pid.value) if pid.value else None
        except Exception:
            return None

    # -- Discovery --------------------------------------------------------

    def get_applications(self) -> list[str]:
        """List running applications visible in the UIA tree.

        Derives the list from :meth:`get_windows` so that only
        processes with real application windows are included.

        Returns:
            Unique application names, sorted alphabetically.
        """
        return sorted({w.app for w in self.get_windows() if w.app})

    def get_windows(self) -> list[Window]:
        """List all top-level windows from the UIA tree.

        Queries the desktop root for children with ControlType.Window
        and builds Window objects.  Filters out phantom UIA nodes
        (notification icons, service host processes) by requiring a
        real native window handle that is user-visible via Win32's
        ``IsWindowVisible``.

        Returns:
            List of :class:`~touchpoint.core.window.Window` instances.
        """
        if self._uia is None:
            return []

        import ctypes
        import ctypes.wintypes

        _IsWindowVisible = ctypes.windll.user32.IsWindowVisible
        _IsWindowVisible.argtypes = [ctypes.wintypes.HWND]
        _IsWindowVisible.restype = ctypes.wintypes.BOOL

        _GetWindowLongW = ctypes.windll.user32.GetWindowLongW
        _GetWindowLongW.argtypes = [ctypes.wintypes.HWND, ctypes.c_int]
        _GetWindowLongW.restype = ctypes.c_long

        _GetWindow = ctypes.windll.user32.GetWindow
        _GetWindow.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.UINT]
        _GetWindow.restype = ctypes.wintypes.HWND

        GWL_EXSTYLE = -20
        GW_OWNER = 4
        WS_EX_APPWINDOW = 0x00040000
        WS_EX_TOOLWINDOW = 0x00000080
        WS_EX_NOACTIVATE = 0x08000000

        def _is_app_window(hwnd: int) -> bool:
            """Return True if *hwnd* looks like a real application window.

            Mimics the logic Windows uses when deciding which windows
            show a taskbar button:
            - visible
            - either has WS_EX_APPWINDOW, or is unowned and is
              not a tool window / no-activate window
            """
            if not _IsWindowVisible(hwnd):
                return False
            ex_style = _GetWindowLongW(hwnd, GWL_EXSTYLE)
            if ex_style & WS_EX_APPWINDOW:
                return True
            # Exclude tool windows and no-activate windows.
            if ex_style & (WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE):
                return False
            # Exclude owned windows (child dialogs etc.)
            owner = _GetWindow(hwnd, GW_OWNER)
            if owner:
                return False
            return True

        windows: list[Window] = []
        walker = self._uia.ControlViewWalker
        child = walker.GetFirstChildElement(self._root)

        while child is not None:
            try:
                ct = child.CurrentControlType
                if ct in _UIA_TOPLEVEL_TYPES:
                    try:
                        hwnd = child.CurrentNativeWindowHandle
                    except Exception:
                        hwnd = None
                    # AppBar elements (Taskbar, Electron apps, desktop)
                    # are always included — they are real visible UI
                    # that agents need.  Window/Pane elements require
                    # the _is_app_window check to filter phantom UIA
                    # nodes (notification icons, service hosts).
                    if hwnd and (ct == 50033 or _is_app_window(hwnd)):
                        win = self._build_window(child)
                        if win is not None:
                            windows.append(win)
            except Exception:
                pass
            try:
                child = walker.GetNextSiblingElement(child)
            except Exception:
                break

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
        """Get UI elements from the UIA tree.

        Walks each scoped window's subtree and returns every element
        found.  When ``role``, ``states``, or ``named_only`` are
        provided and ``tree`` is ``False``, non-matching elements
        are skipped during the walk (their children are still
        visited).

        Args:
            app: Only include elements from this application.
            window_id: Only include elements under this window.
            tree: If ``True``, populate each element's ``children``
                list recursively.
            max_depth: Maximum depth to walk.  ``None`` walks
                everything.
            root_element: Start from this element id instead of
                the window roots.
            lightweight: Ignored on UIA.  UIA's ``CacheRequest``
                batches property reads at the COM level, so
                skipping individual properties saves negligible
                time.  A full walk is always performed.
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
            List of :class:`Element` instances.
        """
        if self._uia is None:
            return []

        # Reset per-call element counter and limit.
        self._element_count = 0
        self._max_elements = max_elements if max_elements is not None else sys.maxsize

        # Store filter hints — used by _check_filter() in flat walks.
        # Tree mode ignores these (tree structure requires all nodes).
        self._filter_role: Role | None = role if not tree else None
        self._filter_states: list[State] | None = states if not tree else None
        self._filter_named_only: bool = named_only and not tree

        # Store skip-subtree roles — walkers will emit elements with
        # these roles but not recurse into their children.
        self._skip_subtree_roles: set[Role] | None = skip_subtree_roles

        # Clear the runtime-ID cache from the previous call so stale
        # COM pointers don't accumulate across repeated walks.
        self._runtime_map.clear()

        # UIA's CacheRequest batches property reads at the COM level,
        # so skipping individual properties (position, actions, value)
        # saves negligible time.  Always do a full walk to avoid
        # returning elements with placeholder (0,0) positions.
        _build = self._build_element
        _collect = self._collect_flat

        # -- root_element: resolve and walk from a specific node ----------
        if root_element is not None:
            uia_el = self._resolve_element(root_element)
            if uia_el is None:
                return []

            # Parse the element id for context.
            parts = root_element.split(":")
            pid = int(parts[1]) if len(parts) > 1 else 0
            app_name = self._resolve_app_name_by_pid(pid)
            win_id = self._extract_window_id(root_element)

            elements: list[Element] = []
            walker = self._uia.ControlViewWalker
            child = walker.GetFirstChildElement(uia_el)
            idx = 0

            while child is not None:
                if self._element_count >= self._max_elements:
                    break
                eid = f"{root_element}.{idx}"
                try:
                    if tree:
                        node = self._to_element_tree(
                            child, app_name, pid, eid,
                            root_element, max_depth, 0,
                            window_id=win_id,
                        )
                        if node is not None:
                            elements.append(node)
                    else:
                        pre = self._check_filter(child)
                        if pre is not None:
                            el = _build(
                                child, app_name, pid, eid,
                                root_element, window_id=win_id,
                                _pre=pre,
                            )
                            if el is not None:
                                self._element_count += 1
                                elements.append(el)
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
                except (ValueError, OSError):
                    pass  # stale COM pointer — skip
                idx += 1
                try:
                    child = walker.GetNextSiblingElement(child)
                except Exception:
                    break

            return elements

        # -- Normal path: walk from window roots -------------------------
        roots = self._get_roots(app, window_id)
        elements = []

        for win_el, app_name, pid, win_id in roots:
            walker = self._uia.ControlViewWalker
            child = walker.GetFirstChildElement(win_el)
            idx = 0

            while child is not None:
                if self._element_count >= self._max_elements:
                    break
                eid = f"{win_id}:{idx}"
                try:
                    if tree:
                        node = self._to_element_tree(
                            child, app_name, pid, eid,
                            None, max_depth, 0,
                            window_id=win_id,
                        )
                        if node is not None:
                            elements.append(node)
                    else:
                        pre = self._check_filter(child)
                        if pre is not None:
                            el = _build(
                                child, app_name, pid, eid,
                                window_id=win_id,
                                _pre=pre,
                            )
                            if el is not None:
                                self._element_count += 1
                                elements.append(el)
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
                except (ValueError, OSError):
                    pass  # stale COM pointer — skip
                idx += 1
                try:
                    child = walker.GetNextSiblingElement(child)
                except Exception:
                    break

        return elements

    def get_element_at(self, x: int, y: int) -> Element | None:
        """Get the element at screen coordinates via UIA.

        Uses ``IUIAutomation.ElementFromPoint()`` which returns
        the deepest element at the given position.

        Args:
            x: Horizontal pixel coordinate.
            y: Vertical pixel coordinate.

        Returns:
            The :class:`Element` at ``(x, y)``, or ``None``.
        """
        if self._uia is None:
            return None

        try:
            from comtypes.gen.UIAutomationClient import tagPOINT  # type: ignore[import-not-found]

            pt = tagPOINT(x, y)
            uia_el = self._uia.ElementFromPoint(pt)
            if uia_el is None:
                return None

            # ElementFromPoint returns the desktop root for offscreen
            # coordinates.  Treat that as "nothing at this point".
            try:
                if self._root is not None:
                    root_rid = _get_runtime_id(self._root)
                    el_rid = _get_runtime_id(uia_el)
                    if root_rid == el_rid:
                        return None
            except Exception:
                pass

            pid = uia_el.CurrentProcessId
            app_name = self._resolve_app_name_by_pid(pid)
            runtime_id = _get_runtime_id(uia_el)
            eid = f"uia:{pid}:{runtime_id}"
            win_id = self._find_window_id_for_element(uia_el, pid)

            return self._build_element(
                uia_el, app_name, pid, eid,
                window_id=win_id,
            )
        except Exception:
            return None

    def get_element_by_id(self, element_id: str) -> Element | None:
        """Retrieve a single element by its UIA id.

        Uses the cached UIA element reference if available,
        otherwise falls back to searching by runtime ID.

        Args:
            element_id: The element's id (e.g.
                ``"uia:1234:42.7.1.0"``).

        Returns:
            The :class:`Element` if found, ``None`` otherwise.
        """
        uia_el = self._resolve_element(element_id)
        if uia_el is None:
            return None

        parts = element_id.split(":")
        pid = int(parts[1]) if len(parts) > 1 else 0
        app_name = self._resolve_app_name_by_pid(pid)
        win_id = self._extract_window_id(element_id)

        # Determine parent_id from the element_id structure.
        parent_id: str | None = None
        remaining = ":".join(parts[2:]) if len(parts) > 2 else ""
        if "." in remaining:
            parent_id = element_id.rsplit(".", 1)[0]

        return self._build_element(
            uia_el, app_name, pid, element_id, parent_id,
            window_id=win_id,
        )

    # -- Actions ----------------------------------------------------------

    def do_action(self, element_id: str, action: str) -> bool:
        """Perform an action on an element via UIA patterns.

        UIA doesn't have named "actions" like AT-SPI2.  Instead,
        we map action names to UIA control patterns:

        - ``"invoke"`` → ``IInvokePattern.Invoke()``
        - ``"toggle"`` → ``ITogglePattern.Toggle()``
        - ``"select"`` → ``ISelectionItemPattern.Select()``
        - ``"expand"`` → ``IExpandCollapsePattern.Expand()``
        - ``"collapse"`` → ``IExpandCollapsePattern.Collapse()``
        - ``"scroll_into_view"`` → ``IScrollItemPattern.ScrollIntoView()``

        Args:
            element_id: The target element's id.
            action: Action name to invoke.

        Returns:
            ``True`` if the action was dispatched.

        Raises:
            ActionFailedError: If the element is not found or
                the action is not supported.
        """
        uia_el = self._resolve_element(element_id)
        if uia_el is None:
            raise ActionFailedError(
                action=action,
                element_id=element_id,
                reason="element not found (ID may be malformed or stale)",
            )

        action_lower = action.lower()

        # Track whether any handler recognised the action name.
        # If a handler matches but its pattern is not retrievable
        # (null COM pointer), we return False instead of raising.
        action_matched = False

        try:
            # -- Invoke Pattern (click/press/activate) --------------------
            if action_lower in ("invoke", "click", "press", "activate"):
                action_matched = True
                pattern = _get_pattern(uia_el, "Invoke")
                if pattern is not None:
                    pattern.Invoke()
                    return True

            # -- Toggle Pattern (check_box, toggle_button) ----------------
            if action_lower in ("toggle", "check", "uncheck"):
                action_matched = True
                pattern = _get_pattern(uia_el, "Toggle")
                if pattern is not None:
                    pattern.Toggle()
                    return True

            # -- SelectionItem Pattern (list items, tabs) -----------------
            if action_lower in ("select", "select_item"):
                action_matched = True
                pattern = _get_pattern(uia_el, "SelectionItem")
                if pattern is not None:
                    pattern.Select()
                    return True

            # -- ExpandCollapse Pattern -----------------------------------
            if action_lower in ("expand",):
                action_matched = True
                pattern = _get_pattern(uia_el, "ExpandCollapse")
                if pattern is not None:
                    pattern.Expand()
                    return True

            if action_lower in ("collapse",):
                action_matched = True
                pattern = _get_pattern(uia_el, "ExpandCollapse")
                if pattern is not None:
                    pattern.Collapse()
                    return True

            if action_lower in ("expand_or_collapse", "expand or collapse"):
                action_matched = True
                pattern = _get_pattern(uia_el, "ExpandCollapse")
                if pattern is not None:
                    # Toggle between expanded and collapsed.
                    from comtypes.gen.UIAutomationClient import (  # type: ignore[import-not-found]
                        ExpandCollapseState_Collapsed,
                    )
                    state = pattern.CurrentExpandCollapseState
                    if state == ExpandCollapseState_Collapsed:
                        pattern.Expand()
                    else:
                        pattern.Collapse()
                    return True

            # -- ScrollItem Pattern (scroll element into view) ------------
            if action_lower in ("scroll_into_view",):
                action_matched = True
                pattern = _get_pattern(uia_el, "ScrollItem")
                if pattern is not None:
                    pattern.ScrollIntoView()
                    return True

            # -- Value Pattern (set_value without an argument) ------------
            if action_lower in ("set_value",):
                action_matched = True
                pattern = _get_pattern(uia_el, "Value")
                if pattern is not None:
                    # No value argument — set to empty string (clear).
                    pattern.SetValue("")
                    return True

            # -- RangeValue Pattern (set_numeric_value) -------------------
            if action_lower in ("set_numeric_value",):
                action_matched = True
                pattern = _get_pattern(uia_el, "RangeValue")
                if pattern is not None:
                    # No value argument — set to the minimum value.
                    val = pattern.CurrentMinimum
                    pattern.SetValue(val)
                    return True

        except Exception as exc:
            raise ActionFailedError(
                action=action,
                element_id=element_id,
                reason=str(exc),
            ) from exc

        # A handler recognised the action name but the underlying
        # UIA pattern was not retrievable (null COM pointer).  This
        # happens when Is*PatternAvailable is True but the actual
        # pattern is not implemented by the provider.
        if action_matched:
            return False

        # No handler recognised the action name at all.
        available = _get_supported_patterns(uia_el)
        raise ActionFailedError(
            action=action,
            element_id=element_id,
            reason=f"action {action!r} not supported, "
                   f"available patterns: {available}",
        )

    def set_value(self, element_id: str, value: str, replace: bool) -> bool:
        """Set the text value of an editable element via UIA.

        Uses the ``Value`` pattern when available.  In replace mode,
        calls ``SetValue()``.  In insert mode, uses ``Value`` pattern
        ``SetValue()`` with the concatenated text (existing + new)
        because UIA's ``Value`` pattern doesn't support insertion
        at cursor position natively.

        Args:
            element_id: The target element's id.
            value: The text to write.
            replace: If ``True``, replace the entire field.

        Returns:
            ``True`` if the value was set.

        Raises:
            ActionFailedError: If the element doesn't support text
                editing.
        """
        uia_el = self._resolve_element(element_id)
        if uia_el is None:
            raise ActionFailedError(
                action="set_value",
                element_id=element_id,
                reason="element not found (ID may be malformed or stale)",
            )

        # Try the Value pattern (most common for edit controls).
        pattern = _get_pattern(uia_el, "Value")
        if pattern is not None:
            try:
                if replace:
                    pattern.SetValue(value)
                else:
                    # Insert mode: append to current value since UIA
                    # Value pattern doesn't support cursor-position
                    # insertion.  For full cursor-aware insertion, the
                    # InputProvider fallback (focus → type) is better.
                    try:
                        current = pattern.CurrentValue or ""
                    except Exception:
                        current = ""
                    pattern.SetValue(current + value)
                return True
            except Exception as exc:
                raise ActionFailedError(
                    action="set_value",
                    element_id=element_id,
                    reason=str(exc),
                ) from exc

        raise ActionFailedError(
            action="set_value",
            element_id=element_id,
            reason="element does not support the Value pattern",
        )

    def set_numeric_value(
        self, element_id: str, value: float,
    ) -> bool:
        """Set a numeric value via UIA's RangeValue pattern.

        Used for sliders, spinboxes, progress bars, and other
        range-valued controls.

        Args:
            element_id: The target element's id.
            value: The numeric value to set.

        Returns:
            ``True`` if the value was set.

        Raises:
            ActionFailedError: If the element doesn't support
                the RangeValue pattern.
        """
        uia_el = self._resolve_element(element_id)
        if uia_el is None:
            raise ActionFailedError(
                action="set_numeric_value",
                element_id=element_id,
                reason="element not found (ID may be malformed or stale)",
            )

        pattern = _get_pattern(uia_el, "RangeValue")
        if pattern is not None:
            try:
                pattern.SetValue(value)
                return True
            except Exception as exc:
                raise ActionFailedError(
                    action="set_numeric_value",
                    element_id=element_id,
                    reason=str(exc),
                ) from exc

        # Fallback: some controls (e.g. VLC sliders) expose
        # only the Value pattern.  Try setting as a string.
        val_pattern = _get_pattern(uia_el, "Value")
        if val_pattern is not None:
            try:
                val_pattern.SetValue(str(value))
                return True
            except Exception as exc:
                raise ActionFailedError(
                    action="set_numeric_value",
                    element_id=element_id,
                    reason=str(exc),
                ) from exc

        raise ActionFailedError(
            action="set_numeric_value",
            element_id=element_id,
            reason="element does not support RangeValue or Value pattern",
        )

    def focus_element(self, element_id: str) -> bool:
        """Move keyboard focus to an element via UIA.

        Calls ``IUIAutomationElement.SetFocus()`` — the standard
        UIA mechanism for moving keyboard focus.

        Args:
            element_id: The target element's id.

        Returns:
            ``True`` if focus was moved.

        Raises:
            ActionFailedError: If the element cannot be found or
                cannot receive focus.
        """
        uia_el = self._resolve_element(element_id)
        if uia_el is None:
            raise ActionFailedError(
                action="focus",
                element_id=element_id,
                reason="element not found (ID may be malformed or stale)",
            )

        try:
            uia_el.SetFocus()
            return True
        except Exception as exc:
            raise ActionFailedError(
                action="focus",
                element_id=element_id,
                reason=str(exc),
            ) from exc

    def activate_window(self, window_id: str) -> bool:
        """Bring a window to the foreground via UIA.

        Resolves the window element and calls ``SetFocus()``,
        then additionally tries ``SetForegroundWindow()`` via
        ctypes for maximum reliability.

        Args:
            window_id: The window ID (e.g. ``"uia:1234:win"``).

        Returns:
            ``True`` if the window was activated.
        """
        uia_el = self._resolve_element(window_id)
        if uia_el is None:
            return False

        try:
            uia_el.SetFocus()

            # Also try Win32 calls for reliability — some apps need
            # them in addition to UIA SetFocus.  ShowWindow with
            # SW_RESTORE (9) is required to unminimize windows;
            # SetForegroundWindow alone cannot restore them.
            try:
                import ctypes
                hwnd = uia_el.CurrentNativeWindowHandle
                if hwnd:
                    if ctypes.windll.user32.IsIconic(hwnd):
                        SW_RESTORE = 9
                        ctypes.windll.user32.ShowWindow(hwnd, SW_RESTORE)
                    ctypes.windll.user32.SetForegroundWindow(hwnd)
            except Exception:
                pass

            return True
        except Exception:
            return False

    # -- Private helpers --------------------------------------------------

    def _get_roots(
        self,
        app: str | None,
        window_id: str | None,
    ) -> list[tuple]:
        """Find window elements to walk based on scoping params.

        Returns:
            List of ``(uia_element, app_name, pid, win_id)`` tuples.
        """
        if self._uia is None:
            return []

        roots: list[tuple] = []

        if window_id is not None:
            uia_el = self._resolve_element(window_id)
            if uia_el is None:
                return roots
            parts = window_id.split(":")
            pid = int(parts[1]) if len(parts) > 1 else 0
            app_name = self._resolve_app_name_by_pid(pid)
            roots.append((uia_el, app_name, pid, window_id))
            return roots

        # Walk desktop children and filter by app name.
        # Apply visibility filtering to exclude phantom windows
        # (notification icons, service hosts, tool windows) that
        # get_windows() also hides.  We intentionally do NOT
        # exclude owned windows here — modal dialogs (Save As,
        # Print, etc.) are owned but should be walkable.
        import ctypes
        import ctypes.wintypes

        _IsWindowVisible = ctypes.windll.user32.IsWindowVisible
        _IsWindowVisible.argtypes = [ctypes.wintypes.HWND]
        _IsWindowVisible.restype = ctypes.wintypes.BOOL

        _GetWindowLongW = ctypes.windll.user32.GetWindowLongW
        _GetWindowLongW.argtypes = [ctypes.wintypes.HWND, ctypes.c_int]
        _GetWindowLongW.restype = ctypes.c_long

        GWL_EXSTYLE = -20
        WS_EX_APPWINDOW = 0x00040000
        WS_EX_TOOLWINDOW = 0x00000080
        WS_EX_NOACTIVATE = 0x08000000

        app_lower = app.lower() if app is not None else None
        walker = self._uia.ControlViewWalker
        child = walker.GetFirstChildElement(self._root)

        while child is not None:
            try:
                ct = child.CurrentControlType
                if ct in _UIA_TOPLEVEL_TYPES:
                    try:
                        hwnd = child.CurrentNativeWindowHandle
                    except Exception:
                        hwnd = None
                    if hwnd:
                        if not _IsWindowVisible(hwnd):
                            child = walker.GetNextSiblingElement(child)
                            continue
                        # AppBar elements skip the tool/no-activate
                        # filter — they include Taskbar, desktop, and
                        # Electron apps that agents need to interact with.
                        if ct != 50033:
                            ex = _GetWindowLongW(hwnd, GWL_EXSTYLE)
                            if (ex & (WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)) and not (ex & WS_EX_APPWINDOW):
                                child = walker.GetNextSiblingElement(child)
                                continue

                    pid = child.CurrentProcessId
                    name = self._resolve_app_name(child, pid)

                    if app_lower is not None and name.lower() != app_lower:
                        child = walker.GetNextSiblingElement(child)
                        continue

                    runtime_id = _get_runtime_id(child)
                    wid = f"uia:{pid}:{runtime_id}"
                    self._runtime_map[wid] = child
                    roots.append((child, name, pid, wid))

                elif ct in _UIA_POPUP_TYPES:
                    # Transient popups (context menus, dropdown menus).
                    # Skip WS_EX style filtering — popups normally
                    # carry WS_EX_TOOLWINDOW / WS_EX_NOACTIVATE.
                    # Only require visibility.
                    try:
                        hwnd = child.CurrentNativeWindowHandle
                    except Exception:
                        hwnd = None
                    if not hwnd or not _IsWindowVisible(hwnd):
                        child = walker.GetNextSiblingElement(child)
                        continue

                    pid = child.CurrentProcessId
                    name = self._resolve_app_name(child, pid)

                    if app_lower is not None and name.lower() != app_lower:
                        child = walker.GetNextSiblingElement(child)
                        continue

                    runtime_id = _get_runtime_id(child)
                    wid = f"uia:{pid}:{runtime_id}"
                    self._runtime_map[wid] = child
                    roots.append((child, name, pid, wid))
            except Exception:
                pass
            try:
                child = walker.GetNextSiblingElement(child)
            except Exception:
                break

        return roots

    def _resolve_element(self, element_id: str) -> Any | None:
        """Resolve a UIA element from its touchpoint ID.

        First checks the runtime map cache.  If not found, tries
        to navigate using the ID structure.

        ID format: ``"uia:{pid}:{runtime_id_parts}"``
        Child IDs:  ``"uia:{pid}:{runtime_id}:{child_idx}"`` or
                    ``"uia:{pid}:{runtime_id}:{c1}.{c2}.{c3}"``

        Args:
            element_id: The touchpoint element ID string.

        Returns:
            The ``IUIAutomationElement``, or ``None``.
        """
        if self._uia is None:
            return None

        # Check cache first.
        cached = self._runtime_map.get(element_id)
        if cached is not None:
            # Verify it's still valid by trying to read a property.
            try:
                _ = cached.CurrentProcessId
                return cached
            except Exception:
                del self._runtime_map[element_id]

        # Parse the element_id and navigate.
        # Format: "uia:{pid}:{runtime_id}" possibly followed by
        #         ":{child_path}" where child_path is "0.1.2..."
        parts = element_id.split(":")
        if len(parts) < 3 or parts[0] != "uia":
            return None

        pid = int(parts[1])
        # The "base" runtime ID is parts[2], which may contain dots
        # for the UIA runtime ID array.  Parts beyond index 2 that
        # contain dots are child path components added by get_elements.

        # Try to find the window first — walk desktop children
        # to find one with matching PID.
        walker = self._uia.ControlViewWalker
        child = walker.GetFirstChildElement(self._root)
        window_el = None

        while child is not None:
            try:
                if child.CurrentProcessId == pid:
                    ct = child.CurrentControlType
                    if ct in _UIA_TOPLEVEL_TYPES:
                        # Check if this window's runtime ID matches.
                        rid = _get_runtime_id(child)
                        wid = f"uia:{pid}:{rid}"
                        self._runtime_map[wid] = child

                        if element_id == wid:
                            return child
                        if element_id.startswith(wid + ":"):
                            window_el = child
                            break
            except Exception:
                pass
            try:
                child = walker.GetNextSiblingElement(child)
            except Exception:
                break

        if window_el is None:
            return None

        # Navigate child path.  The child path is everything after
        # the window ID prefix, split on ':' then '.'.
        wid = f"uia:{pid}:{_get_runtime_id(window_el)}"
        remaining = element_id[len(wid):]
        if not remaining:
            return window_el

        # remaining looks like ":0" or ":0.1.2" — strip leading ':'
        if remaining.startswith(":"):
            remaining = remaining[1:]

        child_indices = remaining.split(".")
        current = window_el

        for idx_str in child_indices:
            try:
                idx = int(idx_str)
            except ValueError:
                return None

            # Walk to child at index.
            next_child = walker.GetFirstChildElement(current)
            for _ in range(idx):
                if next_child is None:
                    return None
                try:
                    next_child = walker.GetNextSiblingElement(next_child)
                except Exception:
                    return None

            if next_child is None:
                return None
            current = next_child

        # Cache for future lookups.
        self._runtime_map[element_id] = current
        return current

    def _build_window(self, uia_el: Any) -> Window | None:
        """Build a :class:`Window` from a UIA element.

        Args:
            uia_el: The ``IUIAutomationElement`` for a top-level window.

        Returns:
            A :class:`Window`, or ``None`` if the element can't be read.
        """
        try:
            import ctypes

            pid = uia_el.CurrentProcessId
            app_name = self._resolve_app_name(uia_el, pid)
            title = uia_el.CurrentName or ""
            runtime_id = _get_runtime_id(uia_el)
            wid = f"uia:{pid}:{runtime_id}"

            # Cache the window element for later lookups.
            self._runtime_map[wid] = uia_el

            # Bounding rectangle.
            rect = uia_el.CurrentBoundingRectangle
            position = (int(rect.left), int(rect.top))
            size = (int(rect.right - rect.left), int(rect.bottom - rect.top))

            # Active state: true only if this window is the
            # foreground window (not merely same-PID).
            try:
                import ctypes
                import ctypes.wintypes
                _GetForegroundWindow = ctypes.windll.user32.GetForegroundWindow
                _GetForegroundWindow.argtypes = []
                _GetForegroundWindow.restype = ctypes.wintypes.HWND
                fg_hwnd = _GetForegroundWindow()
                win_hwnd = uia_el.CurrentNativeWindowHandle
                is_active = bool(
                    fg_hwnd and win_hwnd and int(fg_hwnd) == int(win_hwnd)
                )
            except Exception:
                is_active = False

            # Visible: check if the window is not minimised.
            # UIA doesn't have a direct "visible" property, so
            # we check if the window has non-zero size and is
            # not offscreen.
            try:
                is_offscreen = uia_el.CurrentIsOffscreen
                is_visible = not is_offscreen and size[0] > 0 and size[1] > 0
            except Exception:
                is_visible = size[0] > 0 and size[1] > 0

            # Raw: store the native window handle for power users.
            raw: dict = {}
            try:
                hwnd = uia_el.CurrentNativeWindowHandle
                if hwnd:
                    raw["hwnd"] = hwnd
            except Exception:
                pass

            return Window(
                id=wid,
                title=title,
                app=app_name,
                pid=pid,
                position=position,
                size=size,
                is_active=is_active,
                is_visible=is_visible,
                raw=raw,
            )
        except Exception:
            return None

    def _build_element(
        self,
        uia_el: Any,
        app_name: str,
        pid: int,
        element_id: str,
        parent_id: str | None = None,
        window_id: str | None = None,
        _pre: tuple[Role, str, list["State"]] | None = None,
    ) -> Element | None:
        """Build an :class:`Element` from a UIA element.

        Reads all properties from the UIA element: name, role,
        states, position, size, actions (supported patterns),
        value, and description.

        Args:
            _pre: Pre-computed ``(role, raw_role, states)`` from
                :meth:`_check_filter`.  Avoids redundant COM calls.

        Returns ``None`` if the UIA element is stale (NULL COM
        pointer) — callers should skip these.
        """
        try:
            # Quick liveness check — if CurrentName throws,
            # every other property will too.
            _ = uia_el.CurrentControlType
        except (ValueError, OSError, Exception):
            return None

        # Cache the UIA element for future lookups.
        self._runtime_map[element_id] = uia_el

        # Role mapping.
        if _pre is not None:
            role, raw_role, states = _pre
        else:
            role, raw_role = self._translate_role(uia_el)
            states = self._translate_states(uia_el)

        # Bounding rectangle → center position and size.
        try:
            rect = uia_el.CurrentBoundingRectangle
            left, top = int(rect.left), int(rect.top)
            width = int(rect.right - rect.left)
            height = int(rect.bottom - rect.top)
            position = (round(left + width / 2), round(top + height / 2))
            size = (width, height)
        except Exception:
            position = (0, 0)
            size = (0, 0)

        # Supported patterns → actions list.
        # We translate UIA pattern names to user-friendly action names.
        actions = _get_supported_patterns(uia_el)

        # Value: read from Value pattern or RangeValue pattern.
        # Only attempt the expensive GetCurrentPatternAs call when
        # the element actually advertises pattern support.
        value: str | None = None
        try:
            has_value = uia_el.GetCurrentPropertyValue(30043)  # IsValuePatternAvailable
        except Exception:
            has_value = False
        if has_value:
            val_pattern = _get_pattern(uia_el, "Value")
            if val_pattern is not None:
                try:
                    value = val_pattern.CurrentValue
                except Exception:
                    pass
        if value is None:
            try:
                has_rv = uia_el.GetCurrentPropertyValue(30032)  # IsRangeValuePatternAvailable
            except Exception:
                has_rv = False
            if has_rv:
                rv_pattern = _get_pattern(uia_el, "RangeValue")
                if rv_pattern is not None:
                    try:
                        v = rv_pattern.CurrentValue
                        if v is not None:
                            value = str(v)
                    except Exception:
                        pass

        # Description: UIA has HelpText for this purpose.
        description: str | None = None
        try:
            ht = uia_el.CurrentHelpText
            if ht:
                description = ht
        except Exception:
            pass

        # Raw: extra UIA-specific properties for power users.
        raw: dict = {}
        try:
            class_name = uia_el.CurrentClassName
            if class_name:
                raw["class_name"] = class_name
        except Exception:
            pass
        try:
            auto_id = uia_el.CurrentAutomationId
            if auto_id:
                raw["automation_id"] = auto_id
        except Exception:
            pass
        try:
            hwnd = uia_el.CurrentNativeWindowHandle
            if hwnd:
                raw["hwnd"] = hwnd
        except Exception:
            pass

        # Read the element's name — may fail for stale COM pointers.
        try:
            name = uia_el.CurrentName or ""
        except (ValueError, OSError):
            name = ""

        return Element(
            id=element_id,
            name=name,
            role=role,
            states=states,
            position=position,
            size=size,
            app=app_name,
            pid=pid,
            backend="uia",
            raw_role=raw_role,
            actions=actions,
            value=value,
            description=description,
            parent_id=parent_id,
            window_id=window_id,
            raw=raw,
        )

    def _check_filter(
        self, uia_el: Any,
    ) -> tuple[Role, str, list["State"]] | None:
        """Check *uia_el* against the active filter hints.

        Returns ``None`` when the element should be **skipped**
        (role or states mismatch).  Otherwise returns the
        already-translated ``(role, raw_role, states)`` tuple so
        callers can feed them into ``_build_element`` without
        re-fetching via COM.

        Called before ``_build_element`` so that non-matching elements
        are never fully materialised.  The caller must still recurse
        into children — a non-matching parent may contain matching
        descendants.
        """
        role: Role | None = None
        raw_role: str | None = None
        states: list[State] | None = None

        if self._filter_named_only:
            try:
                name = uia_el.CurrentName
            except Exception:
                name = None
            if not (name and name.strip()):
                return None

        if self._filter_role is not None:
            role, raw_role = self._translate_role(uia_el)
            if role != self._filter_role:
                return None
        if self._filter_states:
            states = self._translate_states(uia_el)
            if not all(s in states for s in self._filter_states):
                return None

        # No filters active, or element passed — return what we have.
        if role is None:
            role, raw_role = self._translate_role(uia_el)
        if states is None:
            states = self._translate_states(uia_el)
        assert raw_role is not None  # guaranteed by _translate_role
        return role, raw_role, states

    def _collect_flat(
        self,
        uia_el: Any,
        app_name: str,
        pid: int,
        parent_id: str,
        out: list[Element],
        max_depth: int | None = None,
        current_depth: int = 0,
        window_id: str | None = None,
    ) -> None:
        """Recursively collect descendants into a flat list.

        Uses UIA's ``ControlViewWalker`` to iterate children
        (skips raw structural elements like scrollbar thumbs).

        Enforces ``max_elements`` and ``max_depth`` safety limits
        to prevent hanging on large UIA trees.

        Args:
            max_depth: Stop recursing beyond this depth.  ``None``
                imposes no depth limit.
            current_depth: How deep we are from the starting point.
            window_id: The window id to attach to every element.
        """
        # Safety: stop if we've already reached the element cap.
        if self._element_count >= self._max_elements:
            return

        try:
            walker = self._uia.ControlViewWalker
            child = walker.GetFirstChildElement(uia_el)
        except Exception:
            return  # parent element is stale or invalid
        idx = 0

        while child is not None:
            if self._element_count >= self._max_elements:
                break
            child_id = f"{parent_id}.{idx}"
            try:
                pre = self._check_filter(child)
                if pre is not None:
                    el = self._build_element(
                        child, app_name, pid, child_id, parent_id,
                        window_id=window_id,
                        _pre=pre,
                    )
                    if el is not None:
                        self._element_count += 1
                        out.append(el)
            except Exception:
                pass  # stale COM pointer — skip
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
            idx += 1
            try:
                child = walker.GetNextSiblingElement(child)
            except Exception:
                break

    def _to_element_tree(
        self,
        uia_el: Any,
        app_name: str,
        pid: int,
        element_id: str,
        parent_id: str | None,
        max_depth: int | None = None,
        current_depth: int = 0,
        window_id: str | None = None,
    ) -> Element | None:
        """Recursively build an Element with children populated.

        Args:
            max_depth: Stop recursing beyond this depth.  ``None``
                imposes no depth limit.
            current_depth: How deep we are from the starting point.
            window_id: The window id to attach to every element.

        Returns:
            The built :class:`Element`, or ``None`` if the UIA
            element is stale.
        """
        element = self._build_element(
            uia_el, app_name, pid, element_id, parent_id,
            window_id=window_id,
        )
        if element is None:
            return None
        self._element_count += 1

        if max_depth is not None and current_depth >= max_depth:
            return element

        # Safety: stop recursing if we've hit the element cap.
        if self._element_count >= self._max_elements:
            return element

        # If this element's role is in _skip_subtree_roles, emit it
        # but don't descend into its children.
        if (self._skip_subtree_roles is not None
                and element.role in self._skip_subtree_roles):
            return element

        try:
            walker = self._uia.ControlViewWalker
            child = walker.GetFirstChildElement(uia_el)
        except Exception:
            return element  # can't enumerate children — return as leaf
        idx = 0

        while child is not None:
            child_id = f"{element_id}.{idx}"
            try:
                child_el = self._to_element_tree(
                    child, app_name, pid, child_id, element_id,
                    max_depth, current_depth + 1,
                    window_id=window_id,
                )
                if child_el is not None:
                    element.children.append(child_el)
            except Exception:
                pass  # stale COM pointer — skip
            idx += 1
            try:
                child = walker.GetNextSiblingElement(child)
            except Exception:
                break

        return element

    # -----------------------------------------------------------------
    # Lightweight element building (for find() optimisation)
    # -----------------------------------------------------------------

    def inflate_element(self, element: Element) -> Element:
        """Inflate a lightweight element into a fully populated one.

        Looks up the UIA element cached during the lightweight walk
        and performs the remaining property reads (position, size,
        actions, value, description).

        If the element is no longer cached, falls back to
        :meth:`get_element_by_id`.
        """
        acc = self._runtime_map.get(element.id)
        if acc is None:
            return self.get_element_by_id(element.id) or element

        parts = element.id.split(":")
        pid = int(parts[1]) if len(parts) > 1 else 0
        app_name = self._resolve_app_name_by_pid(pid) or element.app

        try:
            return self._build_element(
                acc, app_name, pid, element.id,
                element.parent_id, window_id=element.window_id,
            ) or element
        except Exception:
            # COM pointer went stale (element removed from UI).
            self._runtime_map.pop(element.id, None)
            return self.get_element_by_id(element.id) or element

    def _build_light_element(
        self,
        uia_el: Any,
        app_name: str,
        pid: int,
        element_id: str,
        parent_id: str | None = None,
        window_id: str | None = None,
        _pre: tuple[Role, str, list["State"]] | None = None,
    ) -> Element | None:
        """Build a lightweight :class:`Element` — only name, role, states.

        Skips position, size, actions, value, description reads.
        The UIA element reference is stored in :attr:`_runtime_map`
        so :meth:`inflate_element` can fill in the rest later.
        """
        try:
            _ = uia_el.CurrentControlType
        except (ValueError, OSError, Exception):
            return None

        self._runtime_map[element_id] = uia_el

        if _pre is not None:
            role, raw_role, states = _pre
        else:
            role, raw_role = self._translate_role(uia_el)
            states = self._translate_states(uia_el)

        try:
            name = uia_el.CurrentName or ""
        except (ValueError, OSError):
            name = ""

        return Element(
            id=element_id,
            name=name,
            role=role,
            states=states,
            position=(0, 0),
            size=(0, 0),
            app=app_name,
            pid=pid,
            backend="uia",
            raw_role=raw_role,
            parent_id=parent_id,
            window_id=window_id,
        )

    def _collect_light_flat(
        self,
        uia_el: Any,
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
            walker = self._uia.ControlViewWalker
            child = walker.GetFirstChildElement(uia_el)
        except Exception:
            return
        idx = 0

        while child is not None:
            if self._element_count >= self._max_elements:
                break
            child_id = f"{parent_id}.{idx}"
            try:
                pre = self._check_filter(child)
                if pre is not None:
                    el = self._build_light_element(
                        child, app_name, pid, child_id, parent_id,
                        window_id=window_id,
                        _pre=pre,
                    )
                    if el is not None:
                        self._element_count += 1
                        out.append(el)
            except Exception:
                pass
            recurse = max_depth is None or current_depth < max_depth
            if recurse and self._skip_subtree_roles is not None:
                _role = pre[0] if pre is not None else self._translate_role(child)[0]
                if _role in self._skip_subtree_roles:
                    recurse = False
            if recurse:
                self._collect_light_flat(
                    child, app_name, pid, child_id, out,
                    max_depth, current_depth + 1,
                    window_id=window_id,
                )
            idx += 1
            try:
                child = walker.GetNextSiblingElement(child)
            except Exception:
                break

    @staticmethod
    def _translate_role(uia_el: Any) -> tuple[Role, str]:
        """Map a UIA element's ControlType to ``(Role, raw_role_string)``.

        Returns:
            Tuple of (unified Role, raw control type name string).
        """
        try:
            ct = uia_el.CurrentControlType
            raw = uia_el.CurrentLocalizedControlType or str(ct)
            role = _UIA_ROLE_MAP.get(ct, Role.UNKNOWN)

            # Edit with IsPassword → PASSWORD_TEXT
            if ct == 50004:  # Edit
                try:
                    if uia_el.CurrentIsPassword:
                        return Role.PASSWORD_TEXT, raw
                except Exception:
                    pass

            # Button with Toggle pattern → TOGGLE_BUTTON
            if ct == 50000:  # Button
                if _get_pattern(uia_el, "Toggle") is not None:
                    return Role.TOGGLE_BUTTON, raw

            # Window with WindowPattern.IsModal → DIALOG
            if ct == 50032:  # Window
                wp = _get_pattern(uia_el, "Window")
                if wp is not None:
                    try:
                        if wp.CurrentIsModal:
                            return Role.DIALOG, raw
                    except Exception:
                        pass

            return role, raw
        except Exception:
            return Role.UNKNOWN, "unknown"

    @staticmethod
    def _translate_states(uia_el: Any) -> list[State]:
        """Assemble a list of :class:`State` from UIA element properties.

        UIA doesn't have a state set like AT-SPI2.  Instead, we
        query individual properties and build the state list.
        """
        states: list[State] = []

        try:
            # Enabled: the element is interactive and accessible.
            if uia_el.CurrentIsEnabled:
                states.append(State.ENABLED)
                states.append(State.SENSITIVE)

            # Offscreen check → VISIBLE / OFFSCREEN.
            try:
                if uia_el.CurrentIsOffscreen:
                    states.append(State.OFFSCREEN)
                else:
                    states.append(State.VISIBLE)
                    states.append(State.SHOWING)
            except Exception:
                # If we can't determine offscreen status, assume visible.
                states.append(State.VISIBLE)

            # Keyboard focusable.
            try:
                if uia_el.CurrentIsKeyboardFocusable:
                    states.append(State.FOCUSABLE)
            except Exception:
                pass

            # Currently focused.
            try:
                if uia_el.CurrentHasKeyboardFocus:
                    states.append(State.FOCUSED)
            except Exception:
                pass

            # Toggle state (CheckBox, ToggleButton).
            toggle = _get_pattern(uia_el, "Toggle")
            if toggle is not None:
                try:
                    from comtypes.gen.UIAutomationClient import (  # type: ignore[import-not-found]
                        ToggleState_On,
                        ToggleState_Indeterminate,
                    )
                    ts = toggle.CurrentToggleState
                    if ts == ToggleState_On:
                        states.append(State.CHECKED)
                        states.append(State.PRESSED)
                    elif ts == ToggleState_Indeterminate:
                        states.append(State.INDETERMINATE)
                except Exception:
                    pass

            # Selection state (ListItem, Tab).
            sel = _get_pattern(uia_el, "SelectionItem")
            if sel is not None:
                try:
                    if sel.CurrentIsSelected:
                        states.append(State.SELECTED)
                    states.append(State.SELECTABLE)
                except Exception:
                    pass

            # Expand/Collapse state.
            ec = _get_pattern(uia_el, "ExpandCollapse")
            if ec is not None:
                try:
                    from comtypes.gen.UIAutomationClient import (  # type: ignore[import-not-found]
                        ExpandCollapseState_Collapsed,
                        ExpandCollapseState_Expanded,
                    )
                    ecs = ec.CurrentExpandCollapseState
                    states.append(State.EXPANDABLE)
                    if ecs == ExpandCollapseState_Expanded:
                        states.append(State.EXPANDED)
                    elif ecs == ExpandCollapseState_Collapsed:
                        states.append(State.COLLAPSED)
                except Exception:
                    pass

            # Value read-only check.
            val = _get_pattern(uia_el, "Value")
            if val is not None:
                try:
                    if val.CurrentIsReadOnly:
                        states.append(State.READ_ONLY)
                    else:
                        states.append(State.EDITABLE)
                except Exception:
                    pass

            # MULTI_LINE / SINGLE_LINE for text controls.
            try:
                ct = uia_el.CurrentControlType
                if ct == 50004:  # Edit
                    # Check if the edit supports multi-line via the
                    # ClassName hint or by trying to scroll vertically.
                    cls = uia_el.CurrentClassName or ""
                    if "RichEdit" in cls or cls == "TextBox":
                        states.append(State.MULTI_LINE)
                    else:
                        states.append(State.SINGLE_LINE)
                elif ct == 50030:  # Document
                    states.append(State.MULTI_LINE)
            except Exception:
                pass

            # MODAL — from Window pattern.
            try:
                wp = _get_pattern(uia_el, "Window")
                if wp is not None and wp.CurrentIsModal:
                    states.append(State.MODAL)
            except Exception:
                pass

            # REQUIRED — UIA property IsRequiredForForm (30025).
            try:
                if uia_el.GetCurrentPropertyValue(30025):
                    states.append(State.REQUIRED)
            except Exception:
                pass

            # BUSY — UIA AriaProperties or element-level busy hint.
            try:
                aria_props = uia_el.GetCurrentPropertyValue(30102)
                if aria_props and "busy=true" in str(aria_props).lower():
                    states.append(State.BUSY)
            except Exception:
                pass

            # ORIENTATION — UIA property Orientation (30023).
            # 0=None, 1=Horizontal, 2=Vertical.
            try:
                orient = uia_el.GetCurrentPropertyValue(30023)
                if orient == 1:
                    states.append(State.HORIZONTAL)
                elif orient == 2:
                    states.append(State.VERTICAL)
            except Exception:
                pass

            # HAS_POPUP — from UIA AriaProperties.
            try:
                if not aria_props:
                    aria_props = uia_el.GetCurrentPropertyValue(30102)
                if aria_props and "haspopup" in str(aria_props).lower():
                    popup_val = str(aria_props).lower()
                    if "haspopup=true" in popup_val or "haspopup=menu" in popup_val:
                        states.append(State.HAS_POPUP)
            except Exception:
                pass

            # CLICKABLE — inferred from role (consistent with CDP).
            try:
                ct = uia_el.CurrentControlType
                role = _UIA_ROLE_MAP.get(ct, Role.UNKNOWN)
                # Button with Toggle → TOGGLE_BUTTON (clickable)
                if ct == 50000 and _get_pattern(uia_el, "Toggle") is not None:
                    role = Role.TOGGLE_BUTTON
                if role in _CLICKABLE_ROLES:
                    states.append(State.CLICKABLE)
            except Exception:
                pass

        except Exception:
            pass

        return states

    def _find_window_id_for_element(
        self, uia_el: Any, pid: int,
    ) -> str | None:
        """Walk up the UIA tree to find the parent window's ID.

        Args:
            uia_el: The UIA element to start from.
            pid: Process ID for the window ID format.

        Returns:
            The window ID string, or ``None``.
        """
        walker = self._uia.RawViewWalker
        current = uia_el

        # Walk up to find a Window/Pane ancestor.
        for _ in range(50):  # safety limit
            try:
                parent = walker.GetParentElement(current)
                if parent is None:
                    break
                ct = parent.CurrentControlType
                if ct in _UIA_TOPLEVEL_TYPES:
                    rid = _get_runtime_id(parent)
                    return f"uia:{pid}:{rid}"
                current = parent
            except Exception:
                break

        return None

    @staticmethod
    def _extract_window_id(element_id: str) -> str | None:
        """Extract the window ID portion from a full element ID.

        ID format: ``"uia:{pid}:{runtime_id}"`` or
        ``"uia:{pid}:{runtime_id}:{child_path}"``.
        The window ID is the first three colon-separated parts.
        """
        parts = element_id.split(":")
        if len(parts) >= 3:
            return ":".join(parts[:3])
        return None


# ---------------------------------------------------------------------------
# Module-level helpers (outside the class for reuse)
# ---------------------------------------------------------------------------


def _init_uia() -> tuple:
    """Initialise the UIA COM interface.

    Returns:
        ``(uia, root_element, module)`` tuple.

    Raises:
        ImportError: If ``comtypes`` is not installed.
        Exception: If UIA initialisation fails.
    """
    import ctypes

    # Make the process DPI-aware *before* creating the UIA COM object.
    # UIA BoundingRectangle returns physical coordinates only when the
    # process is DPI-aware.  Without this, the first coordinate-based
    # fallback click lands at the wrong position because _get_input()
    # (which also calls SetProcessDPIAware) is initialised *after*
    # the position is read.
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

    import comtypes
    import comtypes.client

    # Create the UIA COM object.
    # CUIAutomation is the main entry point for UIAutomation.
    # CLSID: {ff48dba4-60ef-4201-aa87-54103eef594e}
    uia = comtypes.client.CreateObject(
        "{ff48dba4-60ef-4201-aa87-54103eef594e}",
        interface=_get_uia_interface(),
    )

    root = uia.GetRootElement()

    return uia, root, comtypes


def _get_uia_interface():
    """Get the IUIAutomation COM interface.

    On the first call the type library hasn't been generated yet,
    so we call ``GetModule("UIAutomationCore.dll")`` to generate
    it on the fly.  Subsequent calls import directly from the
    cached ``comtypes.gen`` package.

    Returns:
        The ``IUIAutomation`` interface class from the generated
        type library.
    """
    try:
        from comtypes.gen.UIAutomationClient import IUIAutomation  # type: ignore[import-not-found]
    except (ImportError, ModuleNotFoundError):
        # Type library not yet generated — generate it now.
        import comtypes.client
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen.UIAutomationClient import IUIAutomation  # type: ignore[import-not-found]
    return IUIAutomation


def _get_process_name(pid: int) -> str | None:
    """Get the process name for a given PID.

    Uses ``ctypes`` to call ``OpenProcess`` + ``GetModuleBaseName``
    to avoid external dependencies.

    Args:
        pid: The process ID.

    Returns:
        The process name (e.g. ``"notepad.exe"``), or ``None``.
    """
    if pid <= 0:
        return None

    import ctypes
    import ctypes.wintypes

    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_VM_READ = 0x0010

    try:
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_INFORMATION | PROCESS_VM_READ,
            False, pid,
        )
        if not handle:
            return None

        try:
            # GetModuleBaseNameW returns the executable name.
            buf = ctypes.create_unicode_buffer(260)
            size = ctypes.windll.psapi.GetModuleBaseNameW(
                handle, None, buf, 260,
            )
            if size > 0:
                name = buf.value
                # Strip the .exe extension for cleaner display.
                if name.lower().endswith(".exe"):
                    name = name[:-4]
                return name
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        pass

    return None


def _resolve_uwp_name(walker: Any, uia_el: Any, host_pid: int) -> str | None:
    """Find the real app name for a UWP window hosted by ApplicationFrameHost.

    UWP apps run inside ApplicationFrameHost.exe.  The UIA tree has::

        Window "Calculator" pid=<host>  (ApplicationFrameHost.exe)
          Window "Calculator" pid=<host>
          Window "Calculator" pid=<real>  (CalculatorApp.exe)  ← different PID
          ...

    We walk the immediate children of the top-level element looking for
    the first child whose PID differs from *host_pid*.  That child
    belongs to the actual UWP app process.

    Args:
        walker: A UIA ``IUIAutomationTreeWalker`` (e.g. ControlViewWalker).
        uia_el: The top-level UIA element for the ApplicationFrameHost window.
        host_pid: The PID of the ApplicationFrameHost process.

    Returns:
        The real process name (e.g. ``"CalculatorApp"``), or ``None``
        if no child with a different PID is found.
    """
    try:
        child = walker.GetFirstChildElement(uia_el)
        while child is not None:
            try:
                cpid = child.CurrentProcessId
                if cpid != host_pid:
                    name = _get_process_name(cpid)
                    if name:
                        return name
            except Exception:
                pass
            try:
                child = walker.GetNextSiblingElement(child)
            except Exception:
                break
    except Exception:
        pass
    return None


def _get_runtime_id(uia_el: Any) -> str:
    """Get the runtime ID of a UIA element as a dot-separated string.

    UIA runtime IDs are arrays of integers that uniquely identify
    an element within the current session.

    Args:
        uia_el: The ``IUIAutomationElement``.

    Returns:
        Dot-separated string like ``"42.7.1.0"``, or ``"0"``
        if the runtime ID can't be read.
    """
    try:
        rid = uia_el.GetRuntimeId()
        if rid is not None and len(rid) > 0:
            return ".".join(str(x) for x in rid)
    except Exception:
        pass
    return "0"


# ---------------------------------------------------------------------------
# Pattern maps — module-level to avoid rebuilding on every call
# ---------------------------------------------------------------------------

_PATTERN_MAP: dict[str, tuple[int, str]] = {
    "Invoke": (10000, "IUIAutomationInvokePattern"),
    "Value": (10002, "IUIAutomationValuePattern"),
    "RangeValue": (10003, "IUIAutomationRangeValuePattern"),
    "Toggle": (10015, "IUIAutomationTogglePattern"),
    "SelectionItem": (10010, "IUIAutomationSelectionItemPattern"),
    "ExpandCollapse": (10005, "IUIAutomationExpandCollapsePattern"),
    "ScrollItem": (10017, "IUIAutomationScrollItemPattern"),
    "Scroll": (10004, "IUIAutomationScrollPattern"),
    "Window": (10009, "IUIAutomationWindowPattern"),
    "Transform": (10016, "IUIAutomationTransformPattern"),
    "Grid": (10006, "IUIAutomationGridPattern"),
    "GridItem": (10007, "IUIAutomationGridItemPattern"),
    "Table": (10012, "IUIAutomationTablePattern"),
    "TableItem": (10013, "IUIAutomationTableItemPattern"),
}

# UIA property IDs for Is*PatternAvailable checks.
# These are simple boolean properties — one COM call each.
_PATTERN_AVAILABILITY: list[tuple[int, str]] = [
    (30031, "invoke"),              # IsInvokePatternAvailable
    (30043, "set_value"),           # IsValuePatternAvailable
    (30032, "set_numeric_value"),   # IsRangeValuePatternAvailable
    (30086, "toggle"),              # IsTogglePatternAvailable
    (30036, "select"),              # IsSelectionItemPatternAvailable
    (30009, "expand_or_collapse"),  # IsExpandCollapsePatternAvailable
    (30035, "scroll_into_view"),    # IsScrollItemPatternAvailable
]


def _get_pattern(uia_el: Any, pattern_name: str) -> Any | None:
    """Try to get a UIA control pattern from an element.

    UIA patterns are interfaces that elements optionally support
    (Invoke, Value, Toggle, ExpandCollapse, etc.).  This function
    safely tries to get the requested pattern, returning ``None``
    if the element doesn't support it.

    Args:
        uia_el: The ``IUIAutomationElement``.
        pattern_name: Pattern name without prefix (e.g. ``"Invoke"``,
            ``"Value"``, ``"Toggle"``).

    Returns:
        The pattern interface, or ``None``.
    """
    if pattern_name not in _PATTERN_MAP:
        return None

    pattern_id, interface_name = _PATTERN_MAP[pattern_name]

    try:
        import comtypes.gen.UIAutomationClient as uia_client  # type: ignore[import-not-found]

        iface = getattr(uia_client, interface_name, None)
        if iface is None:
            return None

        # Use GetCurrentPattern (returns IUnknown) then
        # QueryInterface, because GetCurrentPatternAs can return
        # a raw integer pointer for certain patterns in comtypes.
        unknown = uia_el.GetCurrentPattern(pattern_id)
        if unknown is not None and bool(unknown):
            return unknown.QueryInterface(iface)
    except Exception:
        pass

    return None


def _get_supported_patterns(uia_el: Any) -> list[str]:
    """Return the list of supported pattern names for a UIA element.

    Uses the lightweight ``Is*PatternAvailable`` boolean properties
    instead of actually retrieving each pattern interface.  This
    avoids expensive COM round-trips during tree walking.

    Note: Some elements falsely report patterns as available
    (the boolean property is ``True`` but ``GetCurrentPattern``
    returns a null pointer).  The ``do_action`` dispatcher
    handles this gracefully by returning ``False``.

    Args:
        uia_el: The ``IUIAutomationElement``.

    Returns:
        List of action name strings (e.g. ``["invoke", "set_value"]``).
    """
    patterns: list[str] = []
    for prop_id, action_name in _PATTERN_AVAILABILITY:
        try:
            if uia_el.GetCurrentPropertyValue(prop_id):
                patterns.append(action_name)
        except Exception:
            pass

    return patterns

"""AT-SPI2 backend for Linux.

Uses ``gi.repository.Atspi`` (PyGObject) to read the accessibility
tree exposed by Qt, GTK, Firefox, LibreOffice, and other native
Linux applications.

This is the primary backend on Linux.  Electron/Chromium apps that
only expose 2-3 elements via AT-SPI2 should use the CDP backend
instead.

Requires:
    - System package: ``libatk-adaptor``, ``at-spi2-core``
    - Python package: ``PyGObject`` (installed automatically with ``pip install touchpoint``)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

from touchpoint.backends.base import (
    Backend,
    make_element_not_found_error,
    make_malformed_element_id_error,
    make_malformed_window_id_error,
    make_window_not_found_error,
)
from touchpoint.core.element import Element
from touchpoint.core.exceptions import ActionFailedError
from touchpoint.core.types import Role, State
from touchpoint.core.window import Window
from touchpoint.utils.scale import get_scale_factor

# ---------------------------------------------------------------------------
# Window-management helpers (EWMH via wmctrl / xdotool)
# ---------------------------------------------------------------------------
#
# AT-SPI itself exposes Component.set_position / set_size on most apps, but
# minimize / fullscreen / close / activate require talking to the window
# manager directly.  We use wmctrl as the primary EWMH client with xdotool
# as a fallback.  Wayland sessions don't support either tool.

_HAS_WMCTRL: bool = shutil.which("wmctrl") is not None
_HAS_XDOTOOL: bool = shutil.which("xdotool") is not None
_IS_WAYLAND: bool = (
    bool(os.environ.get("WAYLAND_DISPLAY"))
    and not os.environ.get("DISPLAY")
)


def _run_window_tool(cmd: list[str], timeout: float = 1.0) -> bool:
    """Run a window-management subprocess command.

    Returns True if the command exited 0, False on any failure
    (non-zero exit, timeout, FileNotFoundError, OSError).
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def _wayland_error(action: str, element_id: str) -> ActionFailedError:
    """Build the standard 'Wayland not supported' error for window mgmt."""
    return ActionFailedError(
        action=action,
        element_id=element_id,
        reason=(
            "Wayland sessions are not yet supported for window "
            "management — requires an X11 session"
        ),
    )


def _tool_required_error(
    tool: str, action: str, window_id: str,
) -> ActionFailedError:
    """Build a 'specific tool required' error.

    wmctrl is mandatory for every window-management op (the AT-SPI →
    X11 window-id mapping uses ``wmctrl -lpG``).  xdotool is mandatory
    additionally for ``minimize_window`` (the only op without a wmctrl
    path) and is used as a fallback for several others.
    """
    return ActionFailedError(
        action=action,
        element_id=window_id,
        reason=(
            f"{tool} is required for this operation — "
            f"install it: sudo apt install {tool} "
            "(or dnf/pacman equivalent)"
        ),
    )


def _check_wmctrl_or_raise(action: str, window_id: str) -> None:
    """Raise if Wayland or wmctrl is not installed.

    wmctrl is the mandatory floor for any window-management op because
    the AT-SPI → X11 window-id mapping depends on ``wmctrl -lpG``.
    Individual ops layer additional tool requirements on top
    (e.g. minimize also needs xdotool).
    """
    if _IS_WAYLAND:
        raise _wayland_error(action, window_id)
    if not _HAS_WMCTRL:
        raise _tool_required_error("wmctrl", action, window_id)


def _title_matches(known: str | None, candidate: str) -> bool:
    """Compare a known AT-SPI title against a wmctrl candidate title.

    AT-SPI and wmctrl can report titles that differ by a trailing
    " - AppName" suffix or similar minor asymmetry.  We accept exact
    matches and either-direction substring matches; bail when we
    have nothing to compare against.
    """
    if not known:
        return False
    if known == candidate:
        return True
    return known in candidate or candidate in known

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
    "ROLE_DEFINITION": Role.SECTION,
    # Toggles & Range (AT-SPI2 ≥ 2.28)
    "ROLE_SWITCH": Role.SWITCH,
    "ROLE_TIMER": Role.TIMER,
    "ROLE_LEVEL_BAR": Role.METER,
    # Terminals (Konsole, gnome-terminal)
    "ROLE_TERMINAL": Role.TEXT_FIELD,
    # Auto-complete dropdowns (Firefox URL bar, GtkEntryCompletion)
    "ROLE_AUTOCOMPLETE": Role.LIST,
    # Qt list-box / GTK list-box, distinct from ROLE_LIST
    "ROLE_LIST_BOX": Role.LIST,
    # Tree-table (Nautilus list view, file managers)
    "ROLE_TREE_TABLE": Role.TREE,
    # <fieldset> / aria-grouped containers
    "ROLE_GROUPING": Role.GROUP,
    # Document subtypes (LibreOffice, Thunderbird, Evolution)
    "ROLE_DOCUMENT_SPREADSHEET": Role.DOCUMENT,
    "ROLE_DOCUMENT_PRESENTATION": Role.DOCUMENT,
    "ROLE_DOCUMENT_TEXT": Role.DOCUMENT,
    "ROLE_DOCUMENT_EMAIL": Role.DOCUMENT,
    # Info banners (Firefox, Nautilus)
    "ROLE_INFO_BAR": Role.NOTIFICATION,
    # Block-level rich text
    "ROLE_BLOCK_QUOTE": Role.SECTION,
    # HTML5 media (Firefox / Chromium AT-SPI bridge)
    "ROLE_AUDIO": Role.GROUP,
    "ROLE_VIDEO": Role.GROUP,
    # GTK split-button
    "ROLE_PUSH_BUTTON_MENU": Role.SPLIT_BUTTON,
    # Description lists (HTML <dl>/<dt>/<dd>)
    "ROLE_DESCRIPTION_LIST": Role.LIST,
    "ROLE_DESCRIPTION_TERM": Role.LIST_ITEM,
    "ROLE_DESCRIPTION_VALUE": Role.LIST_ITEM,
    # Comments / annotations (Google Docs, LibreOffice)
    "ROLE_COMMENT": Role.NOTE,
    # Date pickers
    "ROLE_DATE_EDITOR": Role.TEXT_FIELD,
    # Star ratings (media players, file managers)
    "ROLE_RATING": Role.SLIDER,
    # Captions (Firefox <caption>, LibreOffice)
    "ROLE_CAPTION": Role.LABEL,
    # Modern document edit roles (Google Docs, LibreOffice 7+)
    "ROLE_FOOTNOTE": Role.SECTION,
    "ROLE_MARK": Role.TEXT,
    "ROLE_SUGGESTION": Role.SECTION,
    "ROLE_CONTENT_INSERTION": Role.SECTION,
    "ROLE_CONTENT_DELETION": Role.SECTION,
    # Multi-page document pages
    "ROLE_PAGE": Role.SECTION,
    # Sub/superscript (Firefox/LibreOffice)
    "ROLE_SUBSCRIPT": Role.TEXT,
    "ROLE_SUPERSCRIPT": Role.TEXT,
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

_WINDOW_ROLES: set[str] = {
    # Standard top-level containers.
    "frame", "window", "dialog", "popup menu",
    # GTK / Qt specialised choosers — registered as their own role
    # rather than ``dialog``, so they need explicit listing or they
    # silently disappear from ``windows()`` / ``snapshot()``.
    "file chooser", "color chooser", "font chooser",
    # Alerts / notifications / IME composition windows.
    "alert", "notification", "input method window",
}


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

    # Maximum children to iterate per accessible node.  Prevents
    # pathological cases like LibreOffice Calc's TABLE accessible
    # that reports get_child_count() == 2_147_483_647 (INT_MAX).
    _MAX_CHILDREN_PER_NODE: int = 500

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
            # Cap iteration per node — some apps (e.g.
            # LibreOffice Calc) report INT_MAX children for
            # spreadsheet tables, causing the walk to hang.
            child_count = min(acc.get_child_count(),
                              self._MAX_CHILDREN_PER_NODE)
            for i in range(child_count):
                if self._element_count >= self._max_elements:
                    break
                child = acc.get_child_at_index(i)
                if child is None:
                    continue
                eid = f"{root_element}.{i}"
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
            for i in range(min(win_acc.get_child_count(),
                               self._MAX_CHILDREN_PER_NODE)):
                if self._element_count >= self._max_elements:
                    break
                child = win_acc.get_child_at_index(i)
                if child is None:
                    continue
                eid = f"{win_id}:{i}"
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
                # Cap children-per-node — LibreOffice Calc tables report
                # INT_MAX children which would hang this descent loop.
                # Same fix as the bulk-walk paths (commit 1e298b2).
                n = min(n, self._MAX_CHILDREN_PER_NODE)
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

    def _validate_query_id(self, element_id: str) -> None:
        """Raise ``ValueError`` if *element_id* is structurally malformed.

        Query methods (:meth:`get_element_by_id`, :meth:`get_text_content`)
        follow the ABC contract of ``ValueError`` on a malformed id and
        ``None`` on a well-formed-but-absent one — matching the macOS AX
        and Windows UIA backends. Action methods use the
        :class:`ActionFailedError`-raising :meth:`_validate_element_id`
        instead.
        """
        parts = element_id.split(":")
        if len(parts) < 3 or parts[0] != "atspi":
            raise ValueError(f"Malformed element ID: {element_id!r}")
        self._parse_id(element_id)

    def get_element_by_id(self, element_id: str) -> Element | None:
        """Retrieve a single element by its AT-SPI2 path id.

        Navigates the tree to the accessible at *element_id* and
        returns a fresh :class:`Element` snapshot.

        Args:
            element_id: The element's id (e.g.
                ``"atspi:2269:1:2.1.0"``).

        Returns:
            The :class:`Element` if found, ``None`` otherwise.

        Raises:
            ValueError: If *element_id* is structurally malformed.
        """
        self._validate_query_id(element_id)
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
        acc = self._resolve_element_or_raise(element_id, action)

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
        acc = self._resolve_element_or_raise(element_id, "set_value")

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
        acc = self._resolve_element_or_raise(
            element_id, "set_numeric_value",
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
        acc = self._resolve_element_or_raise(element_id, "focus")

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

    def get_text_content(self, element_id: str) -> str | None:
        """Return the full text of an element, recursively if needed.

        Tries the AT-SPI2 ``Text`` interface on the element first; if
        absent, walks children recursively.  Leaf nodes without a Text
        interface fall back to the accessible name.

        Returns ``""`` for an accessible but empty element, ``None`` when
        no text interface or accessible name is reachable at all.

        Raises:
            ValueError: If *element_id* is structurally malformed.
        """
        self._validate_query_id(element_id)
        Atspi = self._atspi
        acc = self._resolve_element(element_id)
        if acc is None:
            return None
        parts: list[str] = []
        self._collect_text_recursive(Atspi, acc, parts, depth=0)
        if not parts:
            return None
        return "\n".join(p for p in parts if p)

    def _collect_text_recursive(
        self,
        Atspi: object,
        acc: object,
        out: list[str],
        depth: int,
        max_depth: int = 20,
    ) -> None:
        """Walk *acc* depth-first, appending text segments to *out*.

        When an element exposes the AT-SPI Text interface it is treated as a
        content leaf: its text is collected and recursion stops.  Children of
        such elements are implementation detail (search bars, scroll overlays,
        etc.) and must not contribute to the output.

        For container elements without a Text interface the walk recurses into
        children.  True leaf nodes without a Text interface fall back to the
        accessible name (covers list items, labels, buttons, etc.).
        """
        if depth > max_depth:
            return
        try:
            ifaces = acc.get_interfaces()
        except Exception:
            return
        if "Text" in ifaces:
            try:
                count = acc.get_character_count()
                if count > 0:
                    # Use the explicit class method — the instance method
                    # is shadowed by Accessible.get_text in newer PyGObject
                    # and silently returns None.
                    text = Atspi.Text.get_text(acc, 0, count)
                    out.append(text if text else "")
                else:
                    # Accessible but empty — append "" so the caller can
                    # distinguish "empty element" (parts=[""]) from
                    # "no text reachable at all" (parts=[]).
                    out.append("")
            except Exception:
                pass
            # Stop here: children of a Text node are UI chrome, not content.
            return

        try:
            child_count = min(
                acc.get_child_count(), self._MAX_CHILDREN_PER_NODE,
            )
        except Exception:
            child_count = 0

        if child_count > 0:
            try:
                for i in range(child_count):
                    child = acc.get_child_at_index(i)
                    if child is not None:
                        self._collect_text_recursive(
                            Atspi, child, out, depth + 1, max_depth,
                        )
            except Exception:
                pass
        else:
            # Leaf node with no Text interface — use the accessible name
            # as a fallback (covers list items, labels, buttons, etc. whose
            # text lives in the name attribute rather than a Text interface).
            try:
                name = acc.get_name()
                if name and name.strip():
                    out.append(name)
            except Exception:
                pass

    def _build_text_offset_map(
        self,
        Atspi: object,
        acc: object,
        out: "list[tuple[object, int, int, bool]]",
        g_offset: "list[int]",
        depth: int = 0,
        max_depth: int = 20,
    ) -> None:
        """Walk *acc* depth-first, building a list of (acc, global_start, global_end, selectable).

        Mirrors _collect_text_recursive exactly — same traversal order, same
        stop-at-Text-interface rule, same leaf-name-fallback, same
        _MAX_CHILDREN_PER_NODE cap — so the global offsets correspond to
        positions in the string returned by get_text_content() (non-empty
        parts joined with '\\n').

        The ``selectable`` flag is True only for Text-interface nodes that
        support add_selection().  Name-fallback leaf entries are recorded with
        selectable=False so _find_text_node_for_offset can raise a clear error
        rather than silently mapping into the wrong text node.
        """
        if depth > max_depth:
            return
        try:
            ifaces = acc.get_interfaces()
        except Exception:
            return

        if "Text" in ifaces:
            try:
                count = acc.get_character_count()
                if count > 0:
                    text = Atspi.Text.get_text(acc, 0, count)
                    if text:
                        if out:  # '\n' separator between parts (mirrors "\n".join)
                            g_offset[0] += 1
                        g_start = g_offset[0]
                        g_offset[0] += len(text)
                        out.append((acc, g_start, g_offset[0], True))
            except Exception:
                pass
            return  # Text node is a leaf — stop recursing

        try:
            child_count = min(
                acc.get_child_count(), self._MAX_CHILDREN_PER_NODE,
            )
        except Exception:
            child_count = 0

        if child_count > 0:
            try:
                for i in range(child_count):
                    child = acc.get_child_at_index(i)
                    if child is not None:
                        self._build_text_offset_map(
                            Atspi, child, out, g_offset, depth + 1, max_depth,
                        )
            except Exception:
                pass
        else:
            # Leaf node with no Text interface — name fallback (mirrors
            # _collect_text_recursive).  Record as non-selectable so
            # _find_text_node_for_offset can produce a clear error.
            try:
                name = acc.get_name()
                if name and name.strip():
                    if out:
                        g_offset[0] += 1
                    g_start = g_offset[0]
                    g_offset[0] += len(name)
                    out.append((acc, g_start, g_offset[0], False))
            except Exception:
                pass

    def _find_text_node_for_offset(
        self,
        Atspi: object,
        acc: object,
        start: int,
        end: int,
        element_id: str,
    ) -> "tuple[object, int, int]":
        """Locate the descendant text node covering [start, end).

        Walks *acc* with _build_text_offset_map to recover the same offset
        space as get_text_content(), then maps the requested range to the
        single selectable text node that contains it.

        Returns:
            (node_acc, local_start, local_end) ready to pass to
            Text.add_selection().

        Raises:
            ActionFailedError: If no text nodes exist, the range is out of
                bounds, the range spans more than one text node, or the
                matched node is a name-fallback leaf (not selectable).
        """
        out: list[tuple[object, int, int, bool]] = []
        self._build_text_offset_map(Atspi, acc, out, [0])
        if not out:
            raise ActionFailedError(
                action="select_text",
                element_id=element_id,
                reason="element does not support the Text interface",
            )

        if start == end:
            entry = next((e for e in out if e[1] <= start <= e[2]), None)
            if entry is None:
                raise ActionFailedError(
                    action="select_text",
                    element_id=element_id,
                    reason=f"cursor position {start} is out of bounds",
                )
            node_acc, g_start, _, selectable = entry
            if not selectable:
                raise ActionFailedError(
                    action="select_text",
                    element_id=element_id,
                    reason="text segment does not support selection",
                )
            local = start - g_start
            return (node_acc, local, local)

        start_entry = next((e for e in out if e[1] <= start < e[2]), None)
        end_entry = next((e for e in out if e[1] < end <= e[2]), None)

        if start_entry is None or end_entry is None:
            raise ActionFailedError(
                action="select_text",
                element_id=element_id,
                reason=f"text range [{start}, {end}) is out of bounds",
            )

        if start_entry[0] is not end_entry[0]:
            raise ActionFailedError(
                action="select_text",
                element_id=element_id,
                reason=(
                    "selection spans multiple text segments; "
                    "narrow the selection to a single paragraph"
                ),
            )

        node_acc, g_start, _, selectable = start_entry
        if not selectable:
            raise ActionFailedError(
                action="select_text",
                element_id=element_id,
                reason="text segment does not support selection",
            )
        return (node_acc, start - g_start, end - g_start)

    def select_text(
        self, element_id: str, start: int, end: int,
    ) -> bool:
        """Select a range of text within an element via AT-SPI2.

        Uses the ``Text`` interface's selection methods.  Clears any
        existing selections first, then adds the requested selection.

        If the element itself lacks the Text interface (e.g. a document
        container whose content lives in per-paragraph child nodes),
        the method walks the subtree with _find_text_node_for_offset to
        locate the correct text node and re-maps the offsets.  Raises
        ActionFailedError if the range spans more than one text node.

        Args:
            element_id: The target element's id.
            start: Start offset (0-based character index in the string
                returned by get_text_content()).
            end: End offset (exclusive).

        Returns:
            ``True`` if the selection was applied.

        Raises:
            ActionFailedError: If the element has no accessible text,
                the range is out of bounds, the range spans multiple
                text nodes, or the selection fails.
        """
        acc = self._resolve_element_or_raise(element_id, "select_text")

        Atspi = self._atspi

        try:
            text_iface = acc.get_text_iface()
        except Exception:
            text_iface = None

        if text_iface is None:
            # Element lacks Text interface — locate the right descendant
            # text node using the same offset space as get_text_content().
            acc, start, end = self._find_text_node_for_offset(
                Atspi, acc, start, end, element_id,
            )

        try:
            n_sel = Atspi.Text.get_n_selections(acc)
            for i in range(n_sel - 1, -1, -1):
                Atspi.Text.remove_selection(acc, i)
            result = Atspi.Text.add_selection(acc, start, end)
            if not result:
                raise ActionFailedError(
                    action="select_text",
                    element_id=element_id,
                    reason="selection failed (element may have become unavailable)",
                )
            return True
        except ActionFailedError:
            raise
        except Exception as exc:
            raise ActionFailedError(
                action="select_text",
                element_id=element_id,
                reason=str(exc),
            ) from exc

    def activate_window(self, window_id: str) -> bool:
        """Bring a window to the foreground.

        Uses wmctrl first because it sends ``_NET_ACTIVE_WINDOW`` with
        pager source — most DEs honour pager sources even with focus-
        stealing prevention on (xdotool's "application" source can get
        blocked).  Falls back to xdotool, then a last-ditch AT-SPI
        ``grab_focus`` (occasionally propagates to window activation on
        GTK).
        """
        self._validate_window_id_or_raise(window_id, "activate_window")
        _check_wmctrl_or_raise("activate_window", window_id)
        x11_id = self._x11_id_or_raise(window_id, "activate_window")

        if _HAS_WMCTRL and _run_window_tool(["wmctrl", "-ia", x11_id]):
            return True
        if _HAS_XDOTOOL and _run_window_tool(
            ["xdotool", "windowactivate", x11_id],
        ):
            return True

        # Last resort: AT-SPI grab_focus on the window frame.
        parts = self._parse_id(window_id)
        try:
            result = self._find_window_accessible(
                int(parts[1]), parts[2],
            )
        except Exception:
            result = None
        if result is not None:
            try:
                comp = result[0].get_component_iface()
                if comp is not None and comp.grab_focus():
                    return True
            except Exception:
                pass
        return False

    def minimize_window(self, window_id: str) -> bool:
        """Minimize a window.

        Uses xdotool's ``windowminimize`` (sends
        ``WM_CHANGE_STATE IconicState`` — the ICCCM-standard iconify
        request).  wmctrl has no real minimize path:
        ``_NET_WM_STATE_HIDDEN`` is read-only per the EWMH spec
        (set by the WM, not by clients), so ``wmctrl -b add,hidden``
        succeeds without actually minimizing on spec-compliant WMs.

        Use :meth:`activate_window` to restore.
        """
        self._validate_window_id_or_raise(window_id, "minimize_window")
        # wmctrl is the floor (needed for id mapping); xdotool is the
        # one tool that can actually perform the minimize.
        _check_wmctrl_or_raise("minimize_window", window_id)
        if not _HAS_XDOTOOL:
            raise _tool_required_error(
                "xdotool", "minimize_window", window_id,
            )
        x11_id = self._x11_id_or_raise(window_id, "minimize_window")
        return _run_window_tool(["xdotool", "windowminimize", x11_id])

    def fullscreen_window(
        self, window_id: str, fullscreen: bool = True,
    ) -> bool:
        """Enter or exit fullscreen via EWMH ``_NET_WM_STATE_FULLSCREEN``.

        wmctrl only — xdotool has no direct fullscreen toggle.
        """
        self._validate_window_id_or_raise(window_id, "fullscreen_window")
        # wmctrl is the only tool that can do this; _check_wmctrl_or_raise
        # also covers the Wayland and missing-wmctrl cases uniformly.
        _check_wmctrl_or_raise("fullscreen_window", window_id)
        x11_id = self._x11_id_or_raise(window_id, "fullscreen_window")
        op = "add,fullscreen" if fullscreen else "remove,fullscreen"
        return _run_window_tool(
            ["wmctrl", "-i", "-r", x11_id, "-b", op],
        )

    def close_window(self, window_id: str) -> bool:
        """Politely close a window via EWMH ``_NET_CLOSE_WINDOW``.

        The window manager forwards the request to the app, which may
        prompt the user (e.g. unsaved changes).  Not a force-kill.
        """
        self._validate_window_id_or_raise(window_id, "close_window")
        _check_wmctrl_or_raise("close_window", window_id)
        x11_id = self._x11_id_or_raise(window_id, "close_window")
        if _HAS_WMCTRL and _run_window_tool(["wmctrl", "-i", "-c", x11_id]):
            return True
        if _HAS_XDOTOOL and _run_window_tool(
            ["xdotool", "windowclose", x11_id],
        ):
            return True
        return False

    def move_window(self, window_id: str, x: int, y: int) -> bool:
        """Move a window so its top-left corner is at ``(x, y)``.

        Coordinates are absolute X11 screen pixels (primary monitor
        origin is ``0,0``; negative or out-of-range coordinates
        reach secondary monitors).  Note: wmctrl interprets ``-1``
        in either axis as "leave unchanged"; this method clamps an
        exact ``-1`` to ``-2`` so the move always applies.

        Uses wmctrl's ``_NET_MOVERESIZE_WINDOW``; falls back to
        xdotool (note: xdotool's ``windowmove`` positions the
        client window, not the frame, so results can be off by the
        ``_NET_FRAME_EXTENTS`` on reparenting WMs).

        AT-SPI ``Component.set_position`` is intentionally not used —
        GTK/Qt accessibility bridges no-op on top-level windows.
        """
        self._validate_window_id_or_raise(window_id, "move_window")
        # wmctrl reads -1 as "no change" for either axis.  Clamp.
        if x == -1:
            x = -2
        if y == -1:
            y = -2
        _check_wmctrl_or_raise("move_window", window_id)
        x11_id = self._x11_id_or_raise(window_id, "move_window")
        if _HAS_WMCTRL and _run_window_tool(
            ["wmctrl", "-i", "-r", x11_id, "-e",
             f"0,{x},{y},-1,-1"],
        ):
            return True
        if _HAS_XDOTOOL and _run_window_tool(
            ["xdotool", "windowmove", x11_id, str(x), str(y)],
        ):
            return True
        return False

    def resize_window(
        self, window_id: str, width: int, height: int,
    ) -> bool:
        """Resize a window to ``width`` x ``height`` pixels.

        Width and height must be positive integers.

        Uses wmctrl's ``_NET_MOVERESIZE_WINDOW``; falls back to
        xdotool ``windowsize``.

        AT-SPI ``Component.set_size`` is intentionally not used —
        GTK and Qt bridges no-op for top-level resize (they return
        ``True`` without effect).  Apps with ``WM_NORMAL_HINTS`` size
        increments (terminals, etc.) may snap to the nearest valid
        size.
        """
        self._validate_window_id_or_raise(window_id, "resize_window")
        if width <= 0 or height <= 0:
            raise ActionFailedError(
                action="resize_window",
                element_id=window_id,
                reason=(
                    f"width and height must be positive integers, "
                    f"got width={width}, height={height}"
                ),
            )
        _check_wmctrl_or_raise("resize_window", window_id)
        x11_id = self._x11_id_or_raise(window_id, "resize_window")
        if _HAS_WMCTRL and _run_window_tool(
            ["wmctrl", "-i", "-r", x11_id, "-e",
             f"0,-1,-1,{width},{height}"],
        ):
            return True
        if _HAS_XDOTOOL and _run_window_tool(
            ["xdotool", "windowsize", x11_id, str(width), str(height)],
        ):
            return True
        return False

    # -- Private helpers --------------------------------------------------

    def _validate_window_id_or_raise(
        self, window_id: str, action: str,
    ) -> None:
        """Raise :class:`ActionFailedError` if *window_id* is malformed.

        Window IDs are exactly three colon-separated parts; the four-part
        ``atspi:<pid>:<token>:<child_path>`` form is an *element* id,
        which is rejected here so it can't accidentally be operated on
        as a window.
        """
        try:
            parts = self._parse_id(window_id)
        except ValueError as exc:
            raise make_malformed_window_id_error(
                action, window_id, "atspi:<pid>:<token>",
            ) from exc
        if len(parts) != 3 or parts[0] != "atspi":
            raise make_malformed_window_id_error(
                action, window_id, "atspi:<pid>:<token>",
            )

    def _x11_id_or_raise(self, window_id: str, action: str) -> str:
        """Return the X11 window ID for *window_id*, or raise not-found."""
        x11_id = self._atspi_to_x11_window_id(window_id)
        if x11_id is None:
            raise make_window_not_found_error(action, window_id)
        return x11_id

    def _atspi_to_x11_window_id(self, window_id: str) -> str | None:
        """Map an AT-SPI window ID to its X11 hex window ID.

        Uses ``wmctrl -lpG`` and matches by PID + window title.  Returns
        ``None`` if the window cannot be located, or multiple windows
        share the PID and the title cannot disambiguate them.  Callers
        are responsible for ensuring wmctrl is installed first
        (see :func:`_check_wmctrl_or_raise`); if it isn't, the
        underlying ``subprocess.run`` raises ``FileNotFoundError``
        which is swallowed and returns ``None``.
        """
        parts = self._parse_id(window_id)
        if len(parts) < 3:
            return None
        try:
            pid = int(parts[1])
        except ValueError:
            return None

        win_title: str | None = None
        try:
            result = self._find_window_accessible(pid, parts[2])
            if result is not None:
                win_title = result[0].get_name() or None
        except Exception:
            pass

        try:
            out = subprocess.run(
                ["wmctrl", "-lpG"],
                capture_output=True,
                timeout=1.0,
                check=False,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return None
        if out.returncode != 0:
            return None

        text = out.stdout.decode(errors="replace")
        matches: list[tuple[str, str]] = []
        for line in text.splitlines():
            fields = line.split(None, 8)
            # Format: hex_id desktop pid x y w h host title
            if len(fields) < 9:
                continue
            try:
                line_pid = int(fields[2])
            except ValueError:
                continue
            if line_pid != pid:
                continue
            matches.append((fields[0], fields[8]))

        if not matches:
            return None
        if len(matches) == 1:
            # Single PID match — trust it (matches the common case where
            # AT-SPI and wmctrl agree on one window for the process).
            return matches[0][0]
        # Multiple windows same PID — disambiguate by title.  AT-SPI and
        # wmctrl may report slightly different titles (e.g. one strips
        # " - AppName"); see _title_matches for the relaxed rule.
        if win_title:
            for hex_id, line_title in matches:
                if _title_matches(win_title, line_title):
                    return hex_id
        return None

    def _parse_id(self, id_str: str) -> list[str]:
        """Validate and split an AT-SPI2 element / window ID.

        ID format:
            ``"atspi:{pid}:{dbus_path_id}"`` for windows, or
            ``"atspi:{pid}:{dbus_path_id}:{child.path}"`` for
            elements.  *pid* is the OS process ID. *dbus_path_id* is
            the trailing segment of the accessible's D-Bus object path:
            an integer on most toolkits (GTK3, Qt), but a UUID-style
            string on GTK4 apps (e.g. gnome-calculator), whose paths
            look like ``/org/<app>/a11y/135d3278_0e7f_4d4a_...``. It is
            string-matched during resolution, so it need not be numeric.

        Returns:
            The colon-split parts list (e.g.
            ``['atspi', '2269', '1', '2.1.0']``).

        Raises:
            ValueError: If the PID or a child-path index is not a valid
                integer.
        """
        parts = id_str.split(":")
        # Only the PID must be numeric. The D-Bus path suffix (parts[2])
        # may be a non-numeric UUID on GTK4 apps; resolution compares it
        # as a string, so do not reject it here.
        if len(parts) > 1:
            try:
                int(parts[1])
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

    def _validate_element_id(self, element_id: str, action: str) -> None:
        """Check *element_id* has a well-formed AT-SPI2 shape.

        Wraps :meth:`_parse_id` (which raises ``ValueError`` on bad
        numeric components) and additionally checks the prefix and
        minimum part count.

        Raises:
            ActionFailedError: If the prefix is wrong, the PID is
                non-integer, the dbus_path is non-integer, or any
                child-path segment is non-integer.
        """
        parts = element_id.split(":")
        if len(parts) < 3 or parts[0] != "atspi":
            raise make_malformed_element_id_error(
                action, element_id,
                "atspi:<pid>:<dbus_path_id>[:<child_path>]",
            )
        try:
            self._parse_id(element_id)
        except ValueError as exc:
            raise make_malformed_element_id_error(
                action, element_id,
                "atspi:<pid>:<dbus_path_id>[:<child_path>]",
            ) from exc

    def _resolve_element_or_raise(
        self, element_id: str, action: str,
    ):
        """Validate + look up an element.  Raises ActionFailedError on either failure.

        Separates the three failure types:
            - malformed element_id → ActionFailedError ("malformed ...")
            - well-formed but no element → ActionFailedError ("not found")
            - operational failure of the op itself → caller raises with its own reason
        """
        self._validate_element_id(element_id, action)
        acc = self._resolve_element(element_id)
        if acc is None:
            raise make_element_not_found_error(action, element_id)
        return acc

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

        # Defunct elements are stale references whose underlying object
        # no longer exists.  Skip unconditionally — acting on them always
        # fails, and they appear after the SHOWING filter was removed.
        if State.DEFUNCT in states:
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
            n_children = min(acc.get_child_count(),
                             self._MAX_CHILDREN_PER_NODE)
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
            n_children = min(acc.get_child_count(),
                             self._MAX_CHILDREN_PER_NODE)
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
    ) -> "Element | None":
        """Recursively build an Element with children populated.

        Args:
            max_depth: Stop recursing beyond this depth.  ``None``
                imposes no depth limit.
            current_depth: How deep we are from the starting point.
            window_id: The window id to attach to every element.
        """
        # Skip defunct elements in tree walks just as _check_filter does
        # in flat walks — defunct objects are stale references whose
        # underlying accessible no longer exists.  Use the raw AT-SPI
        # state-set check to avoid a redundant full translation pass
        # (build_element will translate states again for the Element).
        try:
            if acc.get_state_set().contains(
                self._atspi.StateType.DEFUNCT
            ):
                return None
        except Exception:
            return None  # element so stale that state_set call threw

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
        child_count = min(acc.get_child_count(),
                          self._MAX_CHILDREN_PER_NODE)
        for i in range(child_count):
            if self._element_count >= self._max_elements:
                break
            child = acc.get_child_at_index(i)
            if child is None:
                continue
            child_id = f"{element_id}.{i}"
            child_el = self._to_element_tree(
                child, app_name, pid, child_id, element_id,
                max_depth, current_depth + 1,
                window_id=window_id,
            )
            if child_el is not None:
                element.children.append(child_el)
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

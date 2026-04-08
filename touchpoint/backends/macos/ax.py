"""macOS Accessibility backend for Touchpoint.

Uses the macOS Accessibility API (``AXUIElement``) via ``pyobjc`` to
read the accessibility tree exposed by Cocoa, AppKit, SwiftUI, and
most native macOS applications.

This is the primary backend on macOS.  Electron/Chromium apps that
only expose a few elements via the AX API should use the CDP backend
instead.

Requires:
    - Python packages (installed automatically with ``pip install touchpoint-py``):
        - ``pyobjc-framework-ApplicationServices``
          (transitively pulls in ``pyobjc-framework-Quartz`` and
          ``pyobjc-framework-Cocoa``)
    - macOS Accessibility permission must be granted for the Python
      process in System Settings → Privacy & Security → Accessibility.

Usage::

    from touchpoint.backends.macos import AxBackend

    backend = AxBackend()
    if backend.is_available():
        apps = backend.get_applications()
        windows = backend.get_windows()
        elements = backend.get_elements(app="Safari")
"""

from __future__ import annotations

import hashlib
import sys
import uuid
from typing import Any

from touchpoint.backends.base import Backend
from touchpoint.core.element import Element
from touchpoint.core.exceptions import ActionFailedError
from touchpoint.core.types import Role, State
from touchpoint.core.window import Window

# ---------------------------------------------------------------------------
# Role mapping: macOS AXRole strings → Touchpoint Role
# ---------------------------------------------------------------------------
# macOS AX roles are strings like "AXButton", "AXTextField", etc.
# We map them to our unified Role enum.  Anything not in this dict
# becomes Role.UNKNOWN with the original preserved in Element.raw_role.
#
# Reference: Apple Accessibility Role Constants
# https://developer.apple.com/documentation/appkit/nsaccessibility/role
# ---------------------------------------------------------------------------

_AX_ROLE_MAP: dict[str, Role] = {
    # Containers / Structure
    "AXApplication": Role.APPLICATION,
    "AXWindow": Role.WINDOW,
    "AXSheet": Role.DIALOG,
    "AXDialog": Role.DIALOG,
    "AXDrawer": Role.PANEL,
    "AXGroup": Role.GROUP,
    "AXScrollArea": Role.PANEL,
    "AXSplitGroup": Role.PANEL,
    "AXSplitter": Role.SEPARATOR,
    "AXBrowser": Role.PANEL,

    # Interactive
    "AXButton": Role.BUTTON,
    "AXRadioButton": Role.RADIO_BUTTON,
    "AXCheckBox": Role.CHECK_BOX,
    "AXPopUpButton": Role.COMBO_BOX,
    "AXMenuButton": Role.BUTTON,
    "AXDisclosureTriangle": Role.TOGGLE_BUTTON,
    "AXLink": Role.LINK,
    "AXIncrementor": Role.SPIN_BUTTON,

    # Text
    "AXTextField": Role.TEXT_FIELD,
    "AXTextArea": Role.TEXT_FIELD,
    "AXSecureTextField": Role.PASSWORD_TEXT,
    "AXStaticText": Role.TEXT,
    "AXHeading": Role.HEADING,

    # Menus
    "AXMenuBar": Role.MENU_BAR,
    "AXMenuBarItem": Role.MENU_ITEM,
    "AXMenu": Role.MENU,
    "AXMenuItem": Role.MENU_ITEM,
    "AXMenuExtra": Role.MENU_ITEM,

    # Lists & Trees
    "AXList": Role.LIST,
    "AXOutline": Role.TREE,
    "AXOutlineRow": Role.TREE_ITEM,
    "AXRow": Role.TABLE_ROW,

    # Tables
    "AXTable": Role.TABLE,
    "AXCell": Role.TABLE_CELL,
    "AXColumn": Role.TABLE_COLUMN_HEADER,
    "AXSortButton": Role.BUTTON,

    # Tabs
    "AXTabGroup": Role.TAB_LIST,
    "AXRadioGroup": Role.GROUP,

    # Selection & Range
    "AXComboBox": Role.COMBO_BOX,
    "AXSlider": Role.SLIDER,
    "AXScrollBar": Role.SCROLL_BAR,
    "AXProgressIndicator": Role.PROGRESS_BAR,
    "AXValueIndicator": Role.SLIDER,
    "AXRelevanceIndicator": Role.PROGRESS_BAR,
    "AXLevelIndicator": Role.PROGRESS_BAR,

    # Toolbars & Status
    "AXToolbar": Role.TOOLBAR,
    "AXStatusBar": Role.STATUS_BAR,

    # Media & Content
    "AXImage": Role.IMAGE,
    "AXWebArea": Role.DOCUMENT,
    "AXCanvas": Role.CANVAS,

    # Containers / Layout
    "AXLayoutArea": Role.GROUP,
    "AXLayoutItem": Role.GROUP,
    "AXMatte": Role.GROUP,
    "AXRuler": Role.GROUP,
    "AXRulerMarker": Role.GROUP,
    "AXHandle": Role.GROUP,

    # System / OS-level
    "AXSystemWide": Role.APPLICATION,
    "AXGrowArea": Role.GROUP,
    "AXDockItem": Role.BUTTON,

    # Alerts
    "AXAlert": Role.ALERT,

    # Popover / Tooltip
    "AXPopover": Role.TOOLTIP,
    "AXHelpTag": Role.TOOLTIP,

    # Landmark / Navigation (web content)
    "AXLandmarkNavigation": Role.NAVIGATION,
    "AXLandmarkBanner": Role.BANNER,
    "AXLandmarkSearch": Role.SEARCH,
    "AXLandmarkContentInfo": Role.CONTENT_INFO,
    "AXLandmarkMain": Role.LANDMARK,
    "AXLandmarkComplementary": Role.LANDMARK,
    "AXLandmarkRegion": Role.LANDMARK,

    # Content types (web)
    "AXArticle": Role.ARTICLE,
    "AXDocumentWeb": Role.DOCUMENT,
    "AXDocumentArticle": Role.ARTICLE,

    # Title bar
    "AXTitleBar": Role.TITLE_BAR,

    # Date/time
    "AXDateField": Role.TEXT_FIELD,
    "AXTimeField": Role.TEXT_FIELD,

    # Color well
    "AXColorWell": Role.BUTTON,

    # Disclosure
    "AXDisclosureRow": Role.TREE_ITEM,
}


# ---------------------------------------------------------------------------
# AX Subrole → Role overrides
# ---------------------------------------------------------------------------
# Some elements share the same AXRole but differ by AXSubrole.  This
# table lets us refine the mapping for well-known subroles.
# ---------------------------------------------------------------------------

_AX_SUBROLE_MAP: dict[str, Role] = {
    "AXSwitch": Role.SWITCH,
    "AXToggle": Role.TOGGLE_BUTTON,
    "AXSecureTextField": Role.PASSWORD_TEXT,
    "AXSearchField": Role.TEXT_FIELD,
    "AXTabButton": Role.TAB,
    "AXTabPanel": Role.TAB_PANEL,
    "AXCloseButton": Role.BUTTON,
    "AXMinimizeButton": Role.BUTTON,
    "AXZoomButton": Role.BUTTON,
    "AXFullScreenButton": Role.BUTTON,
    "AXToolbarButton": Role.BUTTON,
    "AXSortButton": Role.BUTTON,
    "AXDialog": Role.DIALOG,
    "AXFloatingWindow": Role.WINDOW,
    "AXStandardWindow": Role.WINDOW,
    "AXSystemDialog": Role.DIALOG,
    "AXSystemFloatingWindow": Role.WINDOW,
    "AXContentList": Role.LIST,
    "AXDefinitionList": Role.LIST,
    "AXDescriptionList": Role.LIST,
    "AXTextAttachment": Role.IMAGE,
    "AXSectionListItem": Role.LIST_ITEM,
}


# ---------------------------------------------------------------------------
# Window roles / subroles that represent top-level OS windows
# ---------------------------------------------------------------------------

_WINDOW_ROLES: set[str] = {"AXWindow", "AXSheet", "AXDialog"}

# Roles whose user-visible text lives in AXValue rather than AXTitle.
# For these roles, _ax_name() will fall back to AXValue when AXTitle
# and AXDescription are both empty.
_AX_VALUE_NAME_ROLES: frozenset[str] = frozenset({
    "AXStaticText",
    "AXHeading",
    "AXCheckBox",
    "AXRadioButton",
    "AXMenuItem",
    "AXMenuBarItem",
})


def _ax_name(element: Any) -> str:
    """Derive the user-visible name for an AX element.

    Priority: ``AXTitle`` > ``AXDescription`` > ``AXValue``
    (the ``AXValue`` fallback only applies to roles in
    ``_AX_VALUE_NAME_ROLES`` such as ``AXStaticText``).
    """
    title = _get_ax_attr(element, "AXTitle")
    if title and str(title).strip():
        return str(title)
    desc = _get_ax_attr(element, "AXDescription")
    if desc and str(desc).strip():
        return str(desc)
    # Fallback: AXValue for text-like roles.
    role = _get_ax_attr(element, "AXRole", "")
    if role in _AX_VALUE_NAME_ROLES:
        val = _get_ax_attr(element, "AXValue")
        if val is not None and str(val).strip():
            return str(val)
    return ""


def _get_ax_attr(element: Any, attr: str, default: Any = None) -> Any:
    """Safely read an AX attribute from an AXUIElement.

    macOS AX attributes are fetched one at a time via
    ``AXUIElementCopyAttributeValue``.  The pyobjc bridge wraps
    this as attribute-style access or via helper functions.

    Returns *default* if the attribute is missing, not supported,
    or the element is stale.
    """
    try:
        from ApplicationServices import (
            AXUIElementCopyAttributeValue,
        )

        err, value = AXUIElementCopyAttributeValue(
            element, attr, None,
        )
        if err == 0 and value is not None:
            return value
    except Exception:
        pass
    return default


def _get_ax_attr_names(element: Any) -> list[str]:
    """Return the list of attribute names an AX element supports."""
    try:
        from ApplicationServices import (
            AXUIElementCopyAttributeNames,
        )

        err, names = AXUIElementCopyAttributeNames(element, None)
        if err == 0 and names is not None:
            return list(names)
    except Exception:
        pass
    return []


def _get_ax_actions(element: Any) -> list[str]:
    """Return the list of action names an AX element supports."""
    try:
        from ApplicationServices import AXUIElementCopyActionNames

        err, names = AXUIElementCopyActionNames(element, None)
        if err == 0 and names is not None:
            return list(names)
    except Exception:
        pass
    return []


def _perform_ax_action(element: Any, action: str) -> bool:
    """Perform an AX action on an element.  Returns True on success."""
    try:
        from ApplicationServices import AXUIElementPerformAction

        err = AXUIElementPerformAction(element, action)
        return err == 0
    except Exception:
        return False


def _set_ax_attr(element: Any, attr: str, value: Any) -> bool:
    """Set an AX attribute on an element.  Returns True on success."""
    try:
        from ApplicationServices import AXUIElementSetAttributeValue

        err = AXUIElementSetAttributeValue(element, attr, value)
        return err == 0
    except Exception:
        return False


def _ax_position(element: Any) -> tuple[float, float] | None:
    """Extract the (x, y) position from an AXUIElement.

    macOS AX stores position as an ``AXValue`` wrapping a
    ``CGPoint``.  The coordinate system has origin at the **top-left**
    of the primary display (same as Quartz/CGEvent, despite older
    documentation saying bottom-left — the Accessibility API uses
    top-left since macOS 10.10+).
    """
    val = _get_ax_attr(element, "AXPosition")
    if val is None:
        return None
    # AXValueRef → CGPoint via AXValueGetValue (pyobjc returns a tuple).
    try:
        from ApplicationServices import AXValueGetValue, kAXValueCGPointType

        ok, point = AXValueGetValue(val, kAXValueCGPointType, None)
        if ok and point is not None:
            return (point.x, point.y)
    except Exception:
        pass
    # Fallback: some pyobjc versions expose .x/.y directly.
    try:
        return (val.x, val.y)
    except AttributeError:
        pass
    try:
        return (float(val[0]), float(val[1]))
    except (TypeError, IndexError):
        pass
    return None


def _ax_size(element: Any) -> tuple[float, float] | None:
    """Extract the (width, height) from an AXUIElement."""
    val = _get_ax_attr(element, "AXSize")
    if val is None:
        return None
    # AXValueRef → CGSize via AXValueGetValue (pyobjc returns a tuple).
    try:
        from ApplicationServices import AXValueGetValue, kAXValueCGSizeType

        ok, sz = AXValueGetValue(val, kAXValueCGSizeType, None)
        if ok and sz is not None:
            return (sz.width, sz.height)
    except Exception:
        pass
    # Fallback: some pyobjc versions expose .width/.height directly.
    try:
        return (val.width, val.height)
    except AttributeError:
        pass
    try:
        return (float(val[0]), float(val[1]))
    except (TypeError, IndexError):
        pass
    return None


class AxBackend(Backend):
    """macOS Accessibility backend via ``pyobjc``.

    Connects to the macOS Accessibility API and queries the
    accessibility tree.  Works with Cocoa, AppKit, SwiftUI, and
    most native macOS desktop applications.

    The ``pyobjc`` modules are imported lazily at init time so
    that the rest of Touchpoint can be imported on any platform.

    Raises:
        BackendUnavailableError: If ``pyobjc`` is not installed
            or Accessibility permissions are not granted.
    """

    # Alias lists for convenience action helpers (_try_actions).
    # macOS AX actions are strings like "AXPress", "AXShowMenu".
    ACTION_ALIASES: dict[str, list[str]] = {
        "click": ["AXPress"],
        "double_click": [],  # no native double-click — fallback only
        "right_click": ["AXShowMenu"],
    }

    def __init__(self) -> None:
        self._available: bool = False
        self._ax_module: Any = None  # ApplicationServices module
        self._acc_refs: dict[str, Any] = {}   # element_id → AXUIElement
        self._hit_refs: dict[str, Any] = {}   # hit_id → AXUIElement
        self._hit_order: list[str] = []
        self._max_hit_refs: int = 256
        self._element_count: int = 0
        self._max_elements: int = sys.maxsize
        # Filter hints — set per get_elements() call.
        self._filter_role: Role | None = None
        self._filter_states: list[State] | None = None
        self._filter_named_only: bool = False
        self._skip_subtree_roles: set[Role] | None = None

        if sys.platform != "darwin":
            return

        try:
            import ApplicationServices  # noqa: F401

            self._ax_module = ApplicationServices
            # Check if the process is trusted for Accessibility access.
            from ApplicationServices import AXIsProcessTrusted

            self._available = AXIsProcessTrusted()
        except ImportError:
            pass

    # -- Availability -----------------------------------------------------

    def is_available(self) -> bool:
        """Check if the macOS AX API is accessible.

        Returns:
            ``True`` if pyobjc is installed and Accessibility
            permission is granted in System Settings.
        """
        return self._available

    # -- Backend ABC: routing methods -------------------------------------

    def get_owned_pids(self) -> set[int]:
        """AX backend does not own specific PIDs; returns empty set."""
        return set()

    def owns_element(self, element_id: str) -> bool:
        """Return ``True`` if *element_id* belongs to this AX backend."""
        return isinstance(element_id, str) and element_id.startswith("ax:")

    def claims_app(self, app_name: str) -> bool:
        """Return ``True`` if *app_name* is a native macOS application."""
        if not self._available:
            return False
        try:
            from AppKit import NSWorkspace

            apps = NSWorkspace.sharedWorkspace().runningApplications()
            app_lower = app_name.lower()
            for app in apps:
                name = app.localizedName()
                if name and name.lower() == app_lower:
                    return True
        except Exception:
            pass
        return False

    def get_topmost_pid_at(self, x: int, y: int) -> int | None:
        """Return the PID of the topmost window at ``(x, y)``.

        Uses ``CGWindowListCopyWindowInfo`` for compositor Z-order,
        which gives an accurate stacking answer on macOS.
        """
        try:
            from Quartz import (
                CGWindowListCopyWindowInfo,
                kCGWindowListOptionOnScreenOnly,
                kCGWindowListExcludeDesktopElements,
                kCGNullWindowID,
            )

            # Get Z-ordered window list (front-to-back).
            opts = (
                kCGWindowListOptionOnScreenOnly
                | kCGWindowListExcludeDesktopElements
            )
            windows = CGWindowListCopyWindowInfo(opts, kCGNullWindowID)
            if windows is None:
                return None

            for win_info in windows:
                bounds = win_info.get("kCGWindowBounds")
                if bounds is None:
                    continue
                wx = bounds.get("X", 0)
                wy = bounds.get("Y", 0)
                ww = bounds.get("Width", 0)
                wh = bounds.get("Height", 0)

                if wx <= x < wx + ww and wy <= y < wy + wh:
                    pid = win_info.get("kCGWindowOwnerPID")
                    if pid:
                        return int(pid)
        except Exception:
            pass

        # Fallback: AX element_at.
        try:
            el = self.get_element_at(x, y)
            if el is not None and el.pid:
                return el.pid
        except Exception:
            pass
        return None

    # -- Discovery --------------------------------------------------------

    def get_applications(self) -> list[str]:
        """List applications visible in the macOS accessibility tree.

        Uses ``NSWorkspace.runningApplications()`` filtered to
        Regular (GUI) and Accessory applications that have at
        least one on-screen window (via CGWindowList).

        Returns:
            Application names (e.g. ``["Safari", "Finder", "Slack"]``).
        """
        if not self._available:
            return []

        try:
            from AppKit import (
                NSWorkspace,
                NSApplicationActivationPolicyRegular,
                NSApplicationActivationPolicyAccessory,
            )

            _ALLOWED_POLICIES = {
                NSApplicationActivationPolicyRegular,
                NSApplicationActivationPolicyAccessory,
            }

            # Build a set of PIDs that have on-screen windows so we
            # can cheaply filter accessory apps without slow AX calls.
            # Desktop elements (Dock, Wallpaper) are included so that
            # the Dock app passes the filter — matching Linux/Windows
            # which expose taskbar/desktop chrome.
            pids_with_windows: set[int] = set()
            try:
                from Quartz import (
                    CGWindowListCopyWindowInfo,
                    kCGWindowListOptionOnScreenOnly,
                    kCGNullWindowID,
                )

                cg_wins = CGWindowListCopyWindowInfo(
                    kCGWindowListOptionOnScreenOnly,
                    kCGNullWindowID,
                )
                if cg_wins:
                    for w in cg_wins:
                        pid = w.get("kCGWindowOwnerPID")
                        if pid:
                            pids_with_windows.add(int(pid))
            except Exception:
                pass

            apps = NSWorkspace.sharedWorkspace().runningApplications()
            result: list[str] = []
            for app in apps:
                if app.activationPolicy() not in _ALLOWED_POLICIES:
                    continue
                name = app.localizedName()
                if not name:
                    continue
                # Accessory apps need at least one on-screen window
                # OR an extras menu bar (status-bar items like
                # Wi-Fi, Battery, Siri, Spotlight).
                if app.activationPolicy() == NSApplicationActivationPolicyAccessory:
                    pid = app.processIdentifier()
                    if pid <= 0:
                        continue
                    if pid not in pids_with_windows:
                        # Lightweight probe: only AXExtrasMenuBar
                        # (avoids slow AX calls to background daemons).
                        try:
                            from ApplicationServices import (
                                AXUIElementCreateApplication,
                            )
                            ax_app = AXUIElementCreateApplication(pid)
                            if _get_ax_attr(ax_app, "AXExtrasMenuBar") is None:
                                continue
                        except Exception:
                            continue
                result.append(name)
            return result
        except Exception:
            return []

    def get_windows(self) -> list[Window]:
        """List all windows from the macOS accessibility tree.

        For each GUI application, creates an AXUIElement for the
        application's PID and queries its ``AXWindows`` attribute.

        Returns:
            List of :class:`~touchpoint.core.window.Window` instances.
        """
        if not self._available:
            return []

        try:
            from AppKit import (
                NSWorkspace,
                NSApplicationActivationPolicyRegular,
                NSApplicationActivationPolicyAccessory,
            )
            from ApplicationServices import AXUIElementCreateApplication
        except ImportError:
            return []

        _ALLOWED_POLICIES = {
            NSApplicationActivationPolicyRegular,
            NSApplicationActivationPolicyAccessory,
        }

        # Build a set of PIDs with on-screen windows to cheaply
        # filter accessory apps (avoids slow AX queries).
        # Desktop elements are included so the Dock passes.
        pids_with_windows: set[int] = set()
        try:
            from Quartz import (
                CGWindowListCopyWindowInfo,
                kCGWindowListOptionOnScreenOnly,
                kCGNullWindowID,
            )

            cg_wins = CGWindowListCopyWindowInfo(
                kCGWindowListOptionOnScreenOnly,
                kCGNullWindowID,
            )
            if cg_wins:
                for w in cg_wins:
                    pid = w.get("kCGWindowOwnerPID")
                    if pid:
                        pids_with_windows.add(int(pid))
        except Exception:
            pass

        windows: list[Window] = []
        workspace = NSWorkspace.sharedWorkspace()
        apps = workspace.runningApplications()
        frontmost_pid = workspace.frontmostApplication().processIdentifier()

        for app in apps:
            policy = app.activationPolicy()
            if policy not in _ALLOWED_POLICIES:
                continue

            app_name = app.localizedName() or ""
            pid = app.processIdentifier()
            if pid <= 0:
                continue

            # Skip accessory apps that have no on-screen windows.
            if (
                policy == NSApplicationActivationPolicyAccessory
                and pid not in pids_with_windows
            ):
                continue

            ax_app = AXUIElementCreateApplication(pid)
            ax_windows = _get_ax_attr(ax_app, "AXWindows")
            if not ax_windows:
                continue

            is_frontmost_app = pid == frontmost_pid

            for win_idx, ax_win in enumerate(ax_windows):
                title = _get_ax_attr(ax_win, "AXTitle", "")
                role = _get_ax_attr(ax_win, "AXRole", "")

                # Position and size.
                pos = _ax_position(ax_win)
                sz = _ax_size(ax_win)
                position = (round(pos[0]), round(pos[1])) if pos else (0, 0)
                size = (round(sz[0]), round(sz[1])) if sz else (0, 0)

                # Active: AXMain is per-app; combine with frontmost app check.
                is_main = bool(_get_ax_attr(ax_win, "AXMain", False))
                is_active = is_main and is_frontmost_app

                # Visible: check AXMinimized.
                is_minimized = bool(
                    _get_ax_attr(ax_win, "AXMinimized", False)
                )
                is_visible = not is_minimized and size[0] > 0 and size[1] > 0

                win_token = self._window_token(ax_win, fallback_index=win_idx)
                win_id = f"ax:{pid}:{win_token}"

                # Raw: extra AX-specific data.
                raw: dict = {}
                subrole = _get_ax_attr(ax_win, "AXSubrole")
                if subrole:
                    raw["subrole"] = str(subrole)
                identifier = _get_ax_attr(ax_win, "AXIdentifier")
                if identifier:
                    raw["identifier"] = str(identifier)

                windows.append(Window(
                    id=win_id,
                    title=str(title) if title else "",
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
        """Get UI elements from the macOS accessibility tree.

        Walks each scoped window's subtree and returns every element
        found.  When ``role``, ``states``, or ``named_only`` are
        provided and ``tree`` is ``False``, non-matching elements
        are skipped during the walk (their children are still
        visited).

        Args:
            app: Only include elements from this application.
            window_id: Only include elements under this window
                (format ``"ax:{pid}:{window_token}"``).
            tree: If ``True``, populate each element's ``children``
                list recursively.
            max_depth: Maximum depth to walk.  ``None`` walks
                everything.
            root_element: Start from this element id instead of
                the window roots.
            lightweight: If ``True``, build elements with only
                ``name``, ``role``, and ``states`` populated.  Use
                :meth:`inflate_element` to fill in the rest.
            max_elements: Maximum number of elements to collect.
            role: Early-skip hint — only include elements with this
                role.  Ignored when ``tree=True``.
            states: Early-skip hint — only include elements that
                have **all** of these states.  Ignored when
                ``tree=True``.
            named_only: If ``True``, skip elements with empty or
                whitespace-only names.  Ignored when ``tree=True``.
            skip_subtree_roles: If provided, emit elements with
                these roles but do **not** descend into their
                children.

        Returns:
            List of :class:`Element` instances.
        """
        if not self._available:
            return []

        # Reset per-call state.
        self._element_count = 0
        self._max_elements = (
            max_elements if max_elements is not None else sys.maxsize
        )
        self._filter_role = role if not tree else None
        self._filter_states = states if not tree else None
        self._filter_named_only = named_only and not tree
        self._skip_subtree_roles = skip_subtree_roles

        # Clear element refs from previous walk.
        self._acc_refs.clear()

        light = lightweight and not tree
        _build = self._build_light_element if light else self._build_element
        _collect = self._collect_light_flat if light else self._collect_flat

        # -- root_element: resolve and walk from a specific node ----------
        if root_element is not None:
            ax_el = self._resolve_element(root_element)
            if ax_el is None:
                return []

            parts = root_element.split(":")
            pid = int(parts[1])
            win_token = parts[2]
            app_name = self._get_app_name_for_pid(pid)
            win_id = f"ax:{pid}:{win_token}"

            children = _get_ax_attr(ax_el, "AXChildren") or []
            elements: list[Element] = []

            for i, child in enumerate(children):
                if self._element_count >= self._max_elements:
                    break
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
                                root_element, window_id=win_id,
                                _pre=pre,
                            )
                        )
                    recurse = max_depth is None or max_depth > 0
                    if recurse and self._skip_subtree_roles is not None:
                        _role = (
                            pre[0] if pre is not None
                            else self._translate_role(child)[0]
                        )
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

        for ax_win, app_name, pid, win_id in roots:
            children = _get_ax_attr(ax_win, "AXChildren") or []
            for i, child in enumerate(children):
                if self._element_count >= self._max_elements:
                    break
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
                        _role = (
                            pre[0] if pre is not None
                            else self._translate_role(child)[0]
                        )
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
        """Get the element at a screen coordinate via AX.

        Uses ``AXUIElementCopyElementAtPosition`` on the system-wide
        element, which returns the deepest accessible element at the
        given position.

        Args:
            x: Horizontal pixel coordinate (screen-absolute).
            y: Vertical pixel coordinate (screen-absolute).

        Returns:
            The deepest :class:`Element` at ``(x, y)``, or ``None``.
        """
        if not self._available:
            return None

        try:
            from ApplicationServices import (
                AXUIElementCreateSystemWide,
                AXUIElementCopyElementAtPosition,
            )

            system = AXUIElementCreateSystemWide()
            err, hit_element = AXUIElementCopyElementAtPosition(
                system, float(x), float(y), None,
            )
            if err != 0 or hit_element is None:
                return None

            # Determine PID and app name from the hit element.
            pid = self._get_pid_from_element(hit_element)
            if pid is None:
                return None

            app_name = self._get_app_name_for_pid(pid)

            # Build a stable hit-test ID and cache the element ref.
            # This avoids coordinate-baked IDs that can drift when UI
            # layout changes.
            hit_token = uuid.uuid4().hex[:12]
            element_id = f"ax:{pid}:hit:{hit_token}"
            self._cache_hit_element(element_id, hit_element)
            return self._build_element(
                hit_element, app_name, pid, element_id,
            )
        except Exception:
            return None

    def get_element_by_id(self, element_id: str) -> Element | None:
        """Retrieve a single element by its macOS AX path id.

        Navigates the tree to the accessible at *element_id* and
        returns a fresh :class:`Element` snapshot.

        Args:
            element_id: The element's id (e.g.
                ``"ax:1234:n812:2.1.0"``).

        Returns:
            The :class:`Element` if found, ``None`` otherwise.
        """
        ax_el = self._resolve_element(element_id)
        if ax_el is None:
            return None

        parts = element_id.split(":")
        pid = int(parts[1])
        app_name = self._get_app_name_for_pid(pid)

        # Parent id: everything up to the last '.' in the child path.
        parent_id: str | None = None
        if len(parts) >= 4 and "." in parts[3]:
            parent_id = element_id.rsplit(".", 1)[0]

        win_id = ":".join(parts[:3])
        return self._build_element(
            ax_el, app_name, pid, element_id, parent_id,
            window_id=win_id,
        )

    # -- Actions ----------------------------------------------------------

    def do_action(self, element_id: str, action: str) -> bool:
        """Perform an action on an element via macOS AX.

        Args:
            element_id: The target element's id.
            action: Action name to invoke (e.g. ``"AXPress"``).

        Returns:
            ``True`` if the action was dispatched successfully.

        Raises:
            ActionFailedError: If the element cannot be found or
                the action is not supported.
        """
        ax_el = self._resolve_element(element_id)
        if ax_el is None:
            raise ActionFailedError(
                action=action,
                element_id=element_id,
                reason="element not found in the accessibility tree",
            )

        # Attempt the action directly.
        available_actions = _get_ax_actions(ax_el)
        for available in available_actions:
            if available.lower() == action.lower():
                if _perform_ax_action(ax_el, available):
                    return True

        raise ActionFailedError(
            action=action,
            element_id=element_id,
            reason=f"action {action!r} not supported, "
                   f"available: {available_actions}",
        )

    def set_value(self, element_id: str, value: str, replace: bool) -> bool:
        """Set the text value of an editable element via macOS AX.

        Uses the ``AXValue`` attribute when the element supports it.
        In replace mode, sets the entire value.  In insert mode,
        reads the existing value and appends.  For text fields,
        also tries ``AXSelectedText`` for cursor-aware insertion.

        Args:
            element_id: The target element's id.
            value: The text to write.
            replace: If ``True``, replace the entire field.

        Returns:
            ``True`` if the value was set.

        Raises:
            ActionFailedError: If the element cannot be found or
                does not support value editing.
        """
        ax_el = self._resolve_element(element_id)
        if ax_el is None:
            raise ActionFailedError(
                action="set_value",
                element_id=element_id,
                reason="element not found in the accessibility tree",
            )

        # Check if AXValue is settable.
        try:
            from ApplicationServices import (
                AXUIElementIsAttributeSettable,
            )
            err, settable = AXUIElementIsAttributeSettable(
                ax_el, "AXValue", None,
            )
            is_settable = (err == 0 and settable)
        except Exception:
            is_settable = False

        if not is_settable:
            raise ActionFailedError(
                action="set_value",
                element_id=element_id,
                reason="element does not support setting AXValue",
            )

        try:
            if replace:
                ok = _set_ax_attr(ax_el, "AXValue", value)
                if ok:
                    return True
            else:
                # Insert mode — try AXSelectedText first for
                # cursor-aware insertion.
                try:
                    from ApplicationServices import (
                        AXUIElementIsAttributeSettable,
                    )
                    err_s, settable_s = AXUIElementIsAttributeSettable(
                        ax_el, "AXSelectedText", None,
                    )
                    if err_s == 0 and settable_s:
                        if _set_ax_attr(ax_el, "AXSelectedText", value):
                            return True
                except Exception:
                    pass

                # Fallback: read current value and append.
                current = _get_ax_attr(ax_el, "AXValue", "")
                ok = _set_ax_attr(ax_el, "AXValue", str(current) + value)
                if ok:
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
            reason="failed to set AXValue",
        )

    def set_numeric_value(
        self, element_id: str, value: float,
    ) -> bool:
        """Set a numeric value via macOS AX.

        Used for sliders, spinboxes, and other range-valued controls.

        Args:
            element_id: The target element's id.
            value: The numeric value to set.

        Returns:
            ``True`` if the value was set.

        Raises:
            ActionFailedError: If the element cannot be found or
                does not support numeric values.
        """
        ax_el = self._resolve_element(element_id)
        if ax_el is None:
            raise ActionFailedError(
                action="set_numeric_value",
                element_id=element_id,
                reason="element not found in the accessibility tree",
            )

        # Check if AXValue is settable.
        try:
            from ApplicationServices import (
                AXUIElementIsAttributeSettable,
            )
            err, settable = AXUIElementIsAttributeSettable(
                ax_el, "AXValue", None,
            )
            if err != 0 or not settable:
                raise ActionFailedError(
                    action="set_numeric_value",
                    element_id=element_id,
                    reason="element does not support setting AXValue",
                )
        except ActionFailedError:
            raise
        except Exception as exc:
            raise ActionFailedError(
                action="set_numeric_value",
                element_id=element_id,
                reason=str(exc),
            ) from exc

        try:
            ok = _set_ax_attr(ax_el, "AXValue", value)
            if ok:
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
            reason="failed to set AXValue",
        )

    def focus_element(self, element_id: str) -> bool:
        """Move keyboard focus to an element via macOS AX.

        Sets ``AXFocused`` to ``True``, then raises the owning
        window via ``AXRaise`` for reliability.

        Args:
            element_id: The target element's id.

        Returns:
            ``True`` if focus was moved.

        Raises:
            ActionFailedError: If the element cannot be found or
                cannot receive focus.
        """
        ax_el = self._resolve_element(element_id)
        if ax_el is None:
            raise ActionFailedError(
                action="focus",
                element_id=element_id,
                reason="element not found in the accessibility tree",
            )

        # Primary: set AXFocused.
        focused = _set_ax_attr(ax_el, "AXFocused", True)
        if focused:
            # Also raise the window that owns this element so
            # the focused element is visible.
            self._raise_window_for_element(element_id)
            return True

        # Fallback: try AXPress action (some elements gain focus
        # when pressed but don't support AXFocused directly).
        actions = _get_ax_actions(ax_el)
        for action in actions:
            if action.lower() in ("axpress", "axfocus"):
                if _perform_ax_action(ax_el, action):
                    return True

        raise ActionFailedError(
            action="focus",
            element_id=element_id,
            reason="element does not support AXFocused "
                   "and has no focus action",
        )

    def select_text(
        self, element_id: str, start: int, end: int,
    ) -> bool:
        """Select a range of text within an element via macOS AX.

        TODO: Implement using settable ``AXSelectedTextRange``.
        """
        raise ActionFailedError(
            action="select_text",
            element_id=element_id,
            reason="select_text not yet implemented for macOS AX backend",
        )

    def activate_window(self, window_id: str) -> bool:
        """Bring a window to the foreground via macOS AX.

        Resolves the window element and performs ``AXRaise``, then
        activates the owning application via
        ``NSRunningApplication.activateWithOptions:``.

        Args:
            window_id: Format ``"ax:{pid}:{window_token}"``.

        Returns:
            ``True`` if the window was activated.
        """
        parts = window_id.split(":")
        if len(parts) < 3 or parts[0] != "ax":
            return False

        try:
            pid = int(parts[1])
        except ValueError:
            return False

        win_token = parts[2]
        ax_win = self._get_window_element(pid, win_token)
        if ax_win is None:
            return False

        # Raise the window.
        _perform_ax_action(ax_win, "AXRaise")

        # Activate the application.
        try:
            from AppKit import NSWorkspace, NSApplicationActivateIgnoringOtherApps

            for app in NSWorkspace.sharedWorkspace().runningApplications():
                if app.processIdentifier() == pid:
                    app.activateWithOptions_(
                        NSApplicationActivateIgnoringOtherApps
                    )
                    return True
        except Exception:
            pass

        return True  # Window was raised even if app activation failed.

    # -- Inflate ----------------------------------------------------------

    def inflate_element(self, element: Element) -> Element:
        """Inflate a lightweight element into a fully populated one.

        Looks up the AX element cached during the lightweight walk
        and performs the remaining attribute reads (position, size,
        actions, value, description).

        If the element is no longer cached, falls back to
        :meth:`get_element_by_id`.
        """
        acc = self._acc_refs.get(element.id)
        if acc is None:
            return self.get_element_by_id(element.id) or element

        try:
            return self._build_element(
                acc, element.app, element.pid, element.id,
                element.parent_id, window_id=element.window_id,
            )
        except Exception:
            # AX element went stale (removed from UI).
            self._acc_refs.pop(element.id, None)
            return self.get_element_by_id(element.id) or element

    # -- Private helpers --------------------------------------------------

    def _get_roots(
        self,
        app: str | None,
        window_id: str | None,
    ) -> list[tuple[Any, str, int, str]]:
        """Find window accessibles to walk based on scoping params.

        Returns:
            List of ``(ax_window, app_name, pid, win_id)`` tuples.
        """
        if not self._available:
            return []

        roots: list[tuple[Any, str, int, str]] = []

        # If a specific window_id is provided, resolve it directly.
        if window_id is not None:
            parts = window_id.split(":")
            if len(parts) >= 3 and parts[0] == "ax":
                try:
                    pid = int(parts[1])
                except ValueError:
                    return roots
                win_token = parts[2]
                ax_win = self._get_window_element(pid, win_token)
                if ax_win is not None:
                    app_name = self._get_app_name_for_pid(pid)
                    roots.append((ax_win, app_name, pid, window_id))
            return roots

        # Walk all GUI apps, optionally filtered by name.
        # Include Regular (0) and Accessory (1) activation policies.
        # Accessory apps (e.g. Spotlight, osascript dialogs) have
        # windows but no Dock icon.  Policy 2 (Prohibited) is
        # excluded because those processes rarely own UI.
        try:
            from AppKit import (
                NSWorkspace,
                NSApplicationActivationPolicyRegular,
                NSApplicationActivationPolicyAccessory,
            )
            from ApplicationServices import AXUIElementCreateApplication
        except ImportError:
            return roots

        _ALLOWED_POLICIES = {
            NSApplicationActivationPolicyRegular,
            NSApplicationActivationPolicyAccessory,
        }

        # Build a set of PIDs with on-screen windows so we can
        # skip accessory apps that have no visible UI (avoids slow
        # AX calls to unresponsive background daemons).
        # Desktop elements are included so the Dock passes.
        pids_with_windows: set[int] = set()
        try:
            from Quartz import (
                CGWindowListCopyWindowInfo,
                kCGWindowListOptionOnScreenOnly,
                kCGNullWindowID,
            )

            cg_wins = CGWindowListCopyWindowInfo(
                kCGWindowListOptionOnScreenOnly,
                kCGNullWindowID,
            )
            if cg_wins:
                for w in cg_wins:
                    pid = w.get("kCGWindowOwnerPID")
                    if pid:
                        pids_with_windows.add(int(pid))
        except Exception:
            pass

        app_lower = app.lower() if app is not None else None
        ws_apps = NSWorkspace.sharedWorkspace().runningApplications()

        for ws_app in ws_apps:
            policy = ws_app.activationPolicy()
            if policy not in _ALLOWED_POLICIES:
                continue
            name = ws_app.localizedName() or ""
            if app_lower is not None and name.lower() != app_lower:
                continue

            pid = ws_app.processIdentifier()
            if pid <= 0:
                continue

            # Accessory apps without CG windows: lightweight probe
            # for AXExtrasMenuBar only (avoids slow AX calls to
            # unresponsive background daemons).
            if (
                policy == NSApplicationActivationPolicyAccessory
                and pid not in pids_with_windows
            ):
                ax_app = AXUIElementCreateApplication(pid)
                ax_extras = _get_ax_attr(ax_app, "AXExtrasMenuBar")
                if ax_extras is not None:
                    ex_id = f"ax:{pid}:extras"
                    roots.append((ax_extras, name, pid, ex_id))
                continue

            ax_app = AXUIElementCreateApplication(pid)
            ax_windows = _get_ax_attr(ax_app, "AXWindows") or []

            for win_idx, ax_win in enumerate(ax_windows):
                win_token = self._window_token(ax_win, fallback_index=win_idx)
                win_id = f"ax:{pid}:{win_token}"
                roots.append((ax_win, name, pid, win_id))

            # Include the application menu bar as a root so that
            # menu items are discoverable via find() / elements().
            ax_menubar = _get_ax_attr(ax_app, "AXMenuBar")
            if ax_menubar is not None:
                mb_id = f"ax:{pid}:menubar"
                roots.append((ax_menubar, name, pid, mb_id))

            # Include the extras menu bar (status-bar icons) for
            # processes like ControlCenter and SystemUIServer.
            ax_extras = _get_ax_attr(ax_app, "AXExtrasMenuBar")
            if ax_extras is not None:
                ex_id = f"ax:{pid}:extras"
                roots.append((ax_extras, name, pid, ex_id))

            # Context/popup menus are direct children of the app
            # element with role AXMenu, not inside AXWindows.
            # Also, apps with no windows but direct UI children
            # (e.g. the Dock's AXList) need their children as roots.
            ax_children = _get_ax_attr(ax_app, "AXChildren") or []
            popup_count = 0
            app_child_count = 0
            for child in ax_children:
                child_role = _get_ax_attr(child, "AXRole", "")
                if child_role == "AXMenu":
                    popup_id = f"ax:{pid}:popup{popup_count}"
                    roots.append((child, name, pid, popup_id))
                    popup_count += 1
                elif not ax_windows and child_role != "AXMenuBar":
                    child_id = f"ax:{pid}:app{app_child_count}"
                    roots.append((child, name, pid, child_id))
                    app_child_count += 1

        return roots

    def _get_window_element(
        self, pid: int, win_token: str,
    ) -> Any | None:
        """Get the AXUIElement for a specific window by PID and token.

        Args:
            pid: Process ID of the application.
            win_token: Stable token in the window id.

        Returns:
            The AXUIElement for the window, or ``None``.
        """
        try:
            from ApplicationServices import AXUIElementCreateApplication

            ax_app = AXUIElementCreateApplication(pid)
            ax_windows = _get_ax_attr(ax_app, "AXWindows") or []

            for idx, ax_win in enumerate(ax_windows):
                token = self._window_token(ax_win, fallback_index=idx)
                if token == win_token:
                    return ax_win
        except Exception:
            pass
        return None

    @staticmethod
    def _window_token(ax_win: Any, fallback_index: int | None = None) -> str:
        """Derive a stable per-window token for IDs.

        Priority:
            1) ``AXWindowNumber`` when present (most stable)
            2) ``AXIdentifier`` when present
            3) Fingerprint from title/subrole/geometry
        """
        win_num = _get_ax_attr(ax_win, "AXWindowNumber")
        try:
            if win_num is not None:
                return f"n{int(win_num)}"
        except Exception:
            pass

        identifier = _get_ax_attr(ax_win, "AXIdentifier")
        if identifier:
            digest = hashlib.sha1(str(identifier).encode("utf-8")).hexdigest()[:12]
            return f"i{digest}"

        title = str(_get_ax_attr(ax_win, "AXTitle", "") or "")
        subrole = str(_get_ax_attr(ax_win, "AXSubrole", "") or "")
        pos = _ax_position(ax_win) or (0.0, 0.0)
        size = _ax_size(ax_win) or (0.0, 0.0)
        sig = (
            f"{title}\x1f{subrole}\x1f"
            f"{round(pos[0])},{round(pos[1])}\x1f"
            f"{round(size[0])},{round(size[1])}"
        )
        digest = hashlib.sha1(sig.encode("utf-8")).hexdigest()[:12]
        if fallback_index is not None:
            return f"f{digest}"
        return f"f{digest}"

    def _cache_hit_element(self, hit_id: str, ax_el: Any) -> None:
        """Cache hit-test IDs so get_element_by_id is stable."""
        self._hit_refs[hit_id] = ax_el
        self._hit_order.append(hit_id)
        while len(self._hit_order) > self._max_hit_refs:
            stale = self._hit_order.pop(0)
            self._hit_refs.pop(stale, None)

    def _resolve_element(self, element_id: str) -> Any | None:
        """Navigate the AX tree to the element at *element_id*.

        ID format:
            ``"ax:{pid}:{window_token}"`` — window-level, or
            ``"ax:{pid}:{window_token}:{child.path}"`` — element.

        Also handles synthetic IDs:
            ``"ax:{pid}:hit:{token}"`` — hit-test (cached),
            ``"ax:{pid}:menubar"`` — app menu bar,
            ``"ax:{pid}:extras"`` — extras (status-bar) menu bar,
            ``"ax:{pid}:popup{N}"`` — Nth context/popup menu,
            ``"ax:{pid}:app{N}"`` — Nth direct app child (Dock etc.).

        Returns:
            The AXUIElement, or ``None``.
        """
        # Check the lightweight cache first.
        cached = self._acc_refs.get(element_id)
        if cached is not None:
            return cached

        parts = element_id.split(":")
        if len(parts) < 3 or parts[0] != "ax":
            return None

        try:
            pid = int(parts[1])
        except ValueError:
            return None

        # Synthetic hit-test ID — resolve from cache.
        if parts[2] == "hit":
            return self._hit_refs.get(element_id)

        # Synthetic menubar / extras-menubar IDs.
        if parts[2] in ("menubar", "extras"):
            try:
                from ApplicationServices import AXUIElementCreateApplication

                ax_app = AXUIElementCreateApplication(pid)
                attr = "AXMenuBar" if parts[2] == "menubar" else "AXExtrasMenuBar"
                root = _get_ax_attr(ax_app, attr)
                if root is None:
                    return None
                # 3-part ID → menubar root itself.
                if len(parts) < 4 or not parts[3]:
                    return root
                # Walk child path.
                current = root
                for idx_str in parts[3].split("."):
                    try:
                        idx = int(idx_str)
                    except ValueError:
                        return None
                    children = _get_ax_attr(current, "AXChildren")
                    if children is None or idx >= len(children):
                        return None
                    current = children[idx]
                return current
            except Exception:
                pass
            return None

        # Popup/context menu IDs (ax:{pid}:popup{N}:{child_path}).
        if parts[2].startswith("popup"):
            try:
                from ApplicationServices import AXUIElementCreateApplication

                popup_idx = int(parts[2][5:])
                ax_app = AXUIElementCreateApplication(pid)
                ax_children = _get_ax_attr(ax_app, "AXChildren") or []
                menus = [
                    c for c in ax_children
                    if _get_ax_attr(c, "AXRole", "") == "AXMenu"
                ]
                if popup_idx >= len(menus):
                    return None
                root = menus[popup_idx]
                if len(parts) < 4 or not parts[3]:
                    return root
                current = root
                for idx_str in parts[3].split("."):
                    try:
                        idx = int(idx_str)
                    except ValueError:
                        return None
                    children = _get_ax_attr(current, "AXChildren")
                    if children is None or idx >= len(children):
                        return None
                    current = children[idx]
                return current
            except Exception:
                pass
            return None

        # Direct app-child IDs for windowless apps like the Dock
        # (ax:{pid}:app{N}:{child_path}).
        if parts[2].startswith("app") and len(parts[2]) > 3:
            try:
                from ApplicationServices import AXUIElementCreateApplication

                child_idx = int(parts[2][3:])
                ax_app = AXUIElementCreateApplication(pid)
                ax_children = _get_ax_attr(ax_app, "AXChildren") or []
                non_menu = [
                    c for c in ax_children
                    if _get_ax_attr(c, "AXRole", "") not in
                    ("AXMenuBar", "AXMenu")
                ]
                if child_idx >= len(non_menu):
                    return None
                root = non_menu[child_idx]
                if len(parts) < 4 or not parts[3]:
                    return root
                current = root
                for idx_str in parts[3].split("."):
                    try:
                        idx = int(idx_str)
                    except ValueError:
                        return None
                    children = _get_ax_attr(current, "AXChildren")
                    if children is None or idx >= len(children):
                        return None
                    current = children[idx]
                return current
            except Exception:
                pass
            return None

        win_token = parts[2]
        ax_win = self._get_window_element(pid, win_token)
        if ax_win is None:
            return None

        # 3-part ID → window itself.
        if len(parts) < 4 or not parts[3]:
            return ax_win

        # Walk the child path (e.g. "2.1.0").
        current = ax_win
        for idx_str in parts[3].split("."):
            try:
                idx = int(idx_str)
            except ValueError:
                return None
            children = _get_ax_attr(current, "AXChildren")
            if children is None or idx >= len(children):
                return None
            current = children[idx]

        return current

    def _get_pid_from_element(self, ax_el: Any) -> int | None:
        """Extract the PID from an AXUIElement."""
        try:
            from ApplicationServices import AXUIElementGetPid

            err, pid = AXUIElementGetPid(ax_el, None)
            if err == 0:
                return pid
        except Exception:
            pass
        return None

    def _get_app_name_for_pid(self, pid: int) -> str:
        """Get the application name for a given PID."""
        try:
            from AppKit import NSWorkspace

            for app in NSWorkspace.sharedWorkspace().runningApplications():
                if app.processIdentifier() == pid:
                    return app.localizedName() or ""
        except Exception:
            pass
        return ""

    def _raise_window_for_element(self, element_id: str) -> None:
        """Raise the window that owns *element_id*."""
        parts = element_id.split(":")
        if len(parts) >= 3:
            win_id = ":".join(parts[:3])
            self.activate_window(win_id)

    @staticmethod
    def _translate_role(ax_el: Any) -> tuple[Role, str]:
        """Map an AX element's role to ``(Role, raw_role_string)``.

        Checks both ``AXRole`` and ``AXSubrole`` — subrole takes
        priority when present in ``_AX_SUBROLE_MAP``.
        """
        raw_role = str(_get_ax_attr(ax_el, "AXRole", ""))
        subrole = str(_get_ax_attr(ax_el, "AXSubrole", "") or "")

        # Subrole override takes priority.
        if subrole and subrole in _AX_SUBROLE_MAP:
            return _AX_SUBROLE_MAP[subrole], raw_role

        role = _AX_ROLE_MAP.get(raw_role, Role.UNKNOWN)
        return role, raw_role

    @staticmethod
    def _translate_states(ax_el: Any) -> list[State]:
        """Assemble a list of :class:`State` from AX element attributes.

        macOS AX doesn't have a state set — we query individual
        attributes and build the list procedurally.
        """
        states: list[State] = []

        try:
            # Enabled
            enabled = _get_ax_attr(ax_el, "AXEnabled")
            if enabled is not None:
                if enabled:
                    states.append(State.ENABLED)
                    states.append(State.SENSITIVE)

            # Focused
            focused = _get_ax_attr(ax_el, "AXFocused")
            if focused:
                states.append(State.FOCUSED)

            # Focusable — check if AXFocused is a settable attribute.
            attr_names = _get_ax_attr_names(ax_el)
            if "AXFocused" in attr_names:
                states.append(State.FOCUSABLE)

            # Selected
            selected = _get_ax_attr(ax_el, "AXSelected")
            if selected:
                states.append(State.SELECTED)
            if "AXSelected" in attr_names:
                states.append(State.SELECTABLE)

            # Value-based states (checkboxes, toggles).
            role = str(_get_ax_attr(ax_el, "AXRole", ""))
            if role in ("AXCheckBox", "AXRadioButton"):
                val = _get_ax_attr(ax_el, "AXValue")
                if val == 1 or val is True:
                    states.append(State.CHECKED)
                elif val == 2:
                    states.append(State.INDETERMINATE)

            # Expanded / Collapsed — via AXExpanded attribute.
            expanded = _get_ax_attr(ax_el, "AXExpanded")
            if expanded is not None:
                states.append(State.EXPANDABLE)
                if expanded:
                    states.append(State.EXPANDED)
                else:
                    states.append(State.COLLAPSED)

            # Visibility — macOS elements are visible by default
            # if they have position and size.  Minimized windows
            # are offscreen.
            pos = _ax_position(ax_el)
            sz = _ax_size(ax_el)
            if pos is not None and sz is not None:
                if sz[0] > 0 and sz[1] > 0:
                    states.append(State.VISIBLE)
                    states.append(State.SHOWING)
                else:
                    states.append(State.OFFSCREEN)

            # Editable — check if AXValue is settable.
            if "AXValue" in attr_names:
                try:
                    from ApplicationServices import (
                        AXUIElementIsAttributeSettable,
                    )
                    err, settable = AXUIElementIsAttributeSettable(
                        ax_el, "AXValue", None,
                    )
                    if err == 0 and settable:
                        states.append(State.EDITABLE)
                    elif err == 0 and not settable:
                        # Value present but not settable → read-only.
                        if role in ("AXTextField", "AXTextArea"):
                            states.append(State.READ_ONLY)
                except Exception:
                    pass

            # Modal
            modal = _get_ax_attr(ax_el, "AXModal")
            if modal:
                states.append(State.MODAL)

            # Required
            required = _get_ax_attr(ax_el, "AXRequired")
            if required:
                states.append(State.REQUIRED)

            # Busy
            busy = _get_ax_attr(ax_el, "AXElementBusy")
            if busy:
                states.append(State.BUSY)

            # Orientation
            orientation = _get_ax_attr(ax_el, "AXOrientation")
            if orientation is not None:
                orient_str = str(orientation)
                if "horizontal" in orient_str.lower():
                    states.append(State.HORIZONTAL)
                elif "vertical" in orient_str.lower():
                    states.append(State.VERTICAL)

            # Visited (links)
            visited = _get_ax_attr(ax_el, "AXVisited")
            if visited:
                states.append(State.VISITED)

            # Has popup
            has_popup = _get_ax_attr(ax_el, "AXHasPopup")
            if has_popup:
                states.append(State.HAS_POPUP)

            # CLICKABLE — inferred from role (consistent with CDP).
            if role in (
                "AXButton", "AXLink", "AXMenuItem", "AXMenuBarItem",
                "AXMenuExtra", "AXDisclosureTriangle", "AXPopUpButton",
                "AXComboBox", "AXMenuButton",
            ):
                states.append(State.CLICKABLE)

            # MULTI_LINE / SINGLE_LINE for text controls.
            if role == "AXTextArea":
                states.append(State.MULTI_LINE)
            elif role == "AXTextField" or role == "AXSecureTextField":
                states.append(State.SINGLE_LINE)

        except Exception:
            pass

        return states

    def _check_filter(
        self, ax_el: Any,
    ) -> tuple[Role, str, list[State]] | None:
        """Check *ax_el* against the active filter hints.

        Returns ``None`` when the element should be **skipped**
        (role or states mismatch).  Otherwise returns the
        already-translated ``(role, raw_role, states)`` tuple so
        callers can feed them into ``_build_element`` without
        redundant AX attribute reads.
        """
        role: Role | None = None
        raw_role: str | None = None
        states: list[State] | None = None

        if self._filter_named_only:
            name = _ax_name(ax_el)
            if not name:
                return None

        if self._filter_role is not None:
            role, raw_role = self._translate_role(ax_el)
            if role != self._filter_role:
                return None
        if self._filter_states:
            states = self._translate_states(ax_el)
            if not all(s in states for s in self._filter_states):
                return None

        if role is None:
            role, raw_role = self._translate_role(ax_el)
        if states is None:
            states = self._translate_states(ax_el)
        assert raw_role is not None
        return role, raw_role, states

    # -----------------------------------------------------------------
    # Lightweight element building (for find() optimisation)
    # -----------------------------------------------------------------

    def _build_light_element(
        self,
        ax_el: Any,
        app_name: str,
        pid: int,
        element_id: str,
        parent_id: str | None = None,
        window_id: str | None = None,
        _pre: tuple[Role, str, list[State]] | None = None,
    ) -> Element:
        """Build a lightweight :class:`Element` — only name, role, states.

        Skips position, size, actions, value, description reads.
        The AX element reference is stored in :attr:`_acc_refs` so
        :meth:`inflate_element` can fill in the rest later.
        """
        self._acc_refs[element_id] = ax_el
        if _pre is not None:
            role, raw_role, states = _pre
        else:
            role, raw_role = self._translate_role(ax_el)
            states = self._translate_states(ax_el)

        name = _ax_name(ax_el)

        return Element(
            id=element_id,
            name=name,
            role=role,
            states=states,
            position=(0, 0),
            size=(0, 0),
            app=app_name,
            pid=pid,
            backend="ax",
            raw_role=raw_role,
            parent_id=parent_id,
            window_id=window_id,
        )

    def _collect_light_flat(
        self,
        ax_el: Any,
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
        children = _get_ax_attr(ax_el, "AXChildren")
        if not children:
            return
        for i, child in enumerate(children):
            if self._element_count >= self._max_elements:
                break
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
                    _role = (
                        pre[0] if pre is not None
                        else self._translate_role(child)[0]
                    )
                    if _role in self._skip_subtree_roles:
                        recurse = False
                if recurse:
                    self._collect_light_flat(
                        child, app_name, pid, child_id, out,
                        max_depth, current_depth + 1,
                        window_id=window_id,
                    )

    def _build_element(
        self,
        ax_el: Any,
        app_name: str,
        pid: int,
        element_id: str,
        parent_id: str | None = None,
        window_id: str | None = None,
        _pre: tuple[Role, str, list[State]] | None = None,
    ) -> Element:
        """Build a full :class:`Element` from a macOS AX element.

        Reads all AX attributes: name, role, states, position, size,
        actions, value, and description.

        Args:
            _pre: Pre-computed ``(role, raw_role, states)`` from
                :meth:`_check_filter`.  Avoids redundant AX calls.
        """
        # Role mapping.
        if _pre is not None:
            role, raw_role, states = _pre
        else:
            role, raw_role = self._translate_role(ax_el)
            states = self._translate_states(ax_el)

        name = _ax_name(ax_el)

        # Position (center of bounding box) and size.
        pos = _ax_position(ax_el)
        sz = _ax_size(ax_el)
        if pos is not None and sz is not None:
            position = (
                round(pos[0] + sz[0] / 2),
                round(pos[1] + sz[1] / 2),
            )
            size = (round(sz[0]), round(sz[1]))
        else:
            position = (0, 0)
            size = (0, 0)

        # Actions from the AX Action interface.
        actions = _get_ax_actions(ax_el)

        # Value: read AXValue.
        value: str | None = None
        raw_value = _get_ax_attr(ax_el, "AXValue")
        if raw_value is not None:
            value = str(raw_value)

        # Description / help text.
        description: str | None = None
        help_text = _get_ax_attr(ax_el, "AXHelp")
        if help_text:
            description = str(help_text)
        elif not name:
            # If no title, AXDescription is used as name.
            # Try AXRoleDescription for description instead.
            role_desc = _get_ax_attr(ax_el, "AXRoleDescription")
            if role_desc:
                description = str(role_desc)

        # Raw: extra AX-specific attributes.
        raw: dict = {}
        subrole = _get_ax_attr(ax_el, "AXSubrole")
        if subrole:
            raw["subrole"] = str(subrole)
        identifier = _get_ax_attr(ax_el, "AXIdentifier")
        if identifier:
            raw["identifier"] = str(identifier)
        dom_id = _get_ax_attr(ax_el, "AXDOMIdentifier")
        if dom_id:
            raw["dom_id"] = str(dom_id)

        return Element(
            id=element_id,
            name=str(name) if name else "",
            role=role,
            states=states,
            position=position,
            size=size,
            app=app_name,
            pid=pid,
            backend="ax",
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
        ax_el: Any,
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
            max_depth: Stop recursing beyond this depth.
            current_depth: Current depth from the starting point.
            window_id: The window id to attach to every element.
        """
        if self._element_count >= self._max_elements:
            return
        children = _get_ax_attr(ax_el, "AXChildren")
        if not children:
            return
        for i, child in enumerate(children):
            if self._element_count >= self._max_elements:
                break
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
                _role = (
                    pre[0] if pre is not None
                    else self._translate_role(child)[0]
                )
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
        ax_el: Any,
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
            max_depth: Stop recursing beyond this depth.
            current_depth: Current depth from the starting point.
            window_id: The window id to attach to every element.
        """
        element = self._build_element(
            ax_el, app_name, pid, element_id, parent_id,
            window_id=window_id,
        )
        self._element_count += 1

        if max_depth is not None and current_depth >= max_depth:
            return element
        if self._element_count >= self._max_elements:
            return element
        if (self._skip_subtree_roles is not None
                and element.role in self._skip_subtree_roles):
            return element

        children = _get_ax_attr(ax_el, "AXChildren") or []
        for i, child in enumerate(children):
            if self._element_count >= self._max_elements:
                break
            child_id = f"{element_id}.{i}"
            element.children.append(
                self._to_element_tree(
                    child, app_name, pid, child_id, element_id,
                    max_depth, current_depth + 1,
                    window_id=window_id,
                )
            )
        return element

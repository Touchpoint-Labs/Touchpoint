"""Chrome DevTools Protocol (CDP) backend for Touchpoint.

Targets Electron and Chromium-based applications that don't expose
meaningful accessibility trees via AT-SPI2 or UIA (e.g. Slack,
Discord, VS Code).

Connects via WebSocket to a CDP ``--remote-debugging-port`` and
calls ``Accessibility.getFullAXTree`` to retrieve the in-browser
accessibility tree, translating CDP nodes to Touchpoint
:class:`~touchpoint.core.element.Element` objects.

Auto-discovers CDP ports by scanning process command lines for
``--remote-debugging-port`` flags (``/proc`` on Linux, PowerShell
``Get-CimInstance`` on Windows), or uses explicit registration via
``tp.configure(cdp_ports={"Slack": 9222})``.

Dependencies: ``websocket-client`` (installed automatically with ``pip install touchpoint-py``).
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import subprocess
import sys
import urllib.request
from typing import Any

from touchpoint.backends.base import Backend
from touchpoint.core.element import Element
from touchpoint.core.exceptions import ActionFailedError
from touchpoint.core.types import Role, State
from touchpoint.core.window import Window
from touchpoint.utils.scale import get_scale_factor

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Role mapping: CDP AX role strings → Touchpoint Role
# ---------------------------------------------------------------------------
# CDP roles arrive as camelCase strings in AXNode.role.value.
# Anything not in this dict becomes Role.UNKNOWN with the original
# preserved in Element.raw_role.
# ---------------------------------------------------------------------------

_CDP_ROLE_MAP: dict[str, Role] = {
    # Structural
    "application": Role.APPLICATION,
    "window": Role.WINDOW,
    "dialog": Role.DIALOG,
    "alertDialog": Role.ALERT_DIALOG,
    "alert": Role.ALERT,
    "document": Role.DOCUMENT,
    "rootWebArea": Role.DOCUMENT,
    "webView": Role.DOCUMENT,
    # Interactive
    "button": Role.BUTTON,
    "toggleButton": Role.TOGGLE_BUTTON,
    "checkBox": Role.CHECK_BOX,
    "radioButton": Role.RADIO_BUTTON,
    "radio": Role.RADIO_BUTTON,
    "link": Role.LINK,
    "switch": Role.SWITCH,
    "colorWell": Role.BUTTON,
    # Text
    "textField": Role.TEXT_FIELD,
    "textFieldWithComboBox": Role.TEXT_FIELD,
    "searchBox": Role.TEXT_FIELD,
    "textbox": Role.TEXT_FIELD,
    "staticText": Role.TEXT,
    "labelText": Role.LABEL,
    "heading": Role.HEADING,
    "paragraph": Role.PARAGRAPH,
    "code": Role.TEXT,
    "emphasis": Role.TEXT,
    "strong": Role.TEXT,
    "mark": Role.TEXT,
    "subscript": Role.TEXT,
    "superscript": Role.TEXT,
    "time": Role.TEXT,
    "abbr": Role.TEXT,
    "ruby": Role.TEXT,
    "rubyAnnotation": Role.TEXT,
    # Date/Time inputs
    "date": Role.TEXT_FIELD,
    "dateTime": Role.TEXT_FIELD,
    "inputTime": Role.TEXT_FIELD,
    # Menus
    "menuBar": Role.MENU_BAR,
    "menu": Role.MENU,
    "menuItem": Role.MENU_ITEM,
    "menuItemCheckBox": Role.CHECK_MENU_ITEM,
    "menuItemRadio": Role.RADIO_MENU_ITEM,
    # Lists & Trees
    "list": Role.LIST,
    "listBox": Role.LIST,
    "listItem": Role.LIST_ITEM,
    "listBoxOption": Role.LIST_ITEM,
    "menuListOption": Role.LIST_ITEM,
    "tree": Role.TREE,
    "treeGrid": Role.TREE,
    "treeItem": Role.TREE_ITEM,
    # Tables
    "table": Role.TABLE,
    "row": Role.TABLE_ROW,
    "cell": Role.TABLE_CELL,
    "gridCell": Role.GRID_CELL,
    "columnHeader": Role.TABLE_COLUMN_HEADER,
    "rowHeader": Role.TABLE_ROW_HEADER,
    "grid": Role.GRID,
    "listGrid": Role.GRID,
    # Tabs
    "tabList": Role.TAB_LIST,
    "tab": Role.TAB,
    "tabPanel": Role.TAB_PANEL,
    # Selection & Range
    "comboBoxGrouping": Role.COMBO_BOX,
    "comboBoxMenuButton": Role.COMBO_BOX,
    "comboBoxSelect": Role.COMBO_BOX,
    "comboBox": Role.COMBO_BOX,
    "menuListPopup": Role.COMBO_BOX,
    "slider": Role.SLIDER,
    "spinButton": Role.SPIN_BUTTON,
    "scrollBar": Role.SCROLL_BAR,
    "progressIndicator": Role.PROGRESS_BAR,
    "meter": Role.METER,
    # Toolbars & Status
    "toolbar": Role.TOOLBAR,
    "status": Role.STATUS_BAR,
    "splitter": Role.SEPARATOR,
    # Media & Content
    "image": Role.IMAGE,
    "svgRoot": Role.IMAGE,
    "graphicsSymbol": Role.IMAGE,
    "canvas": Role.CANVAS,
    "figure": Role.FIGURE,
    "figcaption": Role.FIGURE,
    "math": Role.MATH,
    # Containers
    "group": Role.GROUP,
    "radioGroup": Role.GROUP,
    "rowGroup": Role.GROUP,
    "details": Role.GROUP,
    "audio": Role.GROUP,
    "video": Role.GROUP,
    "graphicsObject": Role.GROUP,
    "genericContainer": Role.SECTION,
    "section": Role.SECTION,
    "sectionWithoutName": Role.SECTION,
    "region": Role.SECTION,
    "blockquote": Role.SECTION,
    "definition": Role.SECTION,
    "term": Role.SECTION,
    "form": Role.FORM,
    "graphicsDocument": Role.DOCUMENT,
    # Alerts & Live regions
    "log": Role.LOG,
    "timer": Role.TIMER,
    "note": Role.NOTE,
    "feed": Role.FEED,
    "marquee": Role.NOTIFICATION,
    # Tooltips & Popups
    "tooltip": Role.TOOLTIP,
    "disclosureTriangle": Role.SPLIT_BUTTON,
    # Landmarks
    "banner": Role.BANNER,
    "navigation": Role.NAVIGATION,
    "search": Role.SEARCH,
    "contentInfo": Role.CONTENT_INFO,
    "complementary": Role.LANDMARK,
    "main": Role.LANDMARK,
    # Headers & Footers
    "header": Role.HEADER,
    "sectionHeader": Role.HEADER,
    "footer": Role.FOOTER,
    "sectionFooter": Role.FOOTER,
    # Window chrome
    "titleBar": Role.TITLE_BAR,
    # Content types
    "article": Role.ARTICLE,
    # Label variants
    "legend": Role.LABEL,
    # Frames
    "iframe": Role.FRAME,
    "iframePresentational": Role.FRAME,
    # Panels
    "pane": Role.PANEL,
    # Inline text (text rendering internals)
    "inlineTextBox": Role.TEXT,
    "lineBreak": Role.TEXT,
    # Generic container (div/span with no ARIA role)
    "generic": Role.SECTION,
    # Catch-all
    "unknown": Role.UNKNOWN,
}

# Pre-built lowercase lookup for case-insensitive role matching.
_CDP_ROLE_MAP_LOWER: dict[str, Role] = {
    k.lower(): v for k, v in _CDP_ROLE_MAP.items()
}

# Pre-built reverse map: Touchpoint Role → CDP role string (first match).
_CDP_REVERSE_ROLE_MAP: dict[Role, str] = {}
for _k, _v in _CDP_ROLE_MAP.items():
    _CDP_REVERSE_ROLE_MAP.setdefault(_v, _k)

# ---------------------------------------------------------------------------
# HTML tag → Role mapping (for source="dom")
# ---------------------------------------------------------------------------
# Maps HTML tag names to Touchpoint Roles.  Elements with an explicit
# [role] attribute use that instead.  Tags not in this dict become
# Role.SECTION.
# ---------------------------------------------------------------------------

_TAG_ROLE_MAP: dict[str, Role] = {
    "a": Role.LINK,
    "article": Role.ARTICLE,
    "aside": Role.LANDMARK,
    "audio": Role.GROUP,
    "button": Role.BUTTON,
    "canvas": Role.CANVAS,
    "caption": Role.LABEL,
    "code": Role.TEXT,
    "details": Role.GROUP,
    "dialog": Role.DIALOG,
    "embed": Role.FRAME,
    "fieldset": Role.GROUP,
    "figcaption": Role.FIGURE,
    "figure": Role.FIGURE,
    "footer": Role.FOOTER,
    "form": Role.FORM,
    "h1": Role.HEADING,
    "h2": Role.HEADING,
    "h3": Role.HEADING,
    "h4": Role.HEADING,
    "h5": Role.HEADING,
    "h6": Role.HEADING,
    "header": Role.HEADER,
    "hr": Role.SEPARATOR,
    "iframe": Role.FRAME,
    "img": Role.IMAGE,
    "input": Role.TEXT_FIELD,
    "label": Role.LABEL,
    "legend": Role.LABEL,
    "li": Role.LIST_ITEM,
    "main": Role.LANDMARK,
    "math": Role.MATH,
    "menu": Role.MENU,
    "meter": Role.METER,
    "nav": Role.NAVIGATION,
    "object": Role.FRAME,
    "ol": Role.LIST,
    "optgroup": Role.GROUP,
    "option": Role.LIST_ITEM,
    "output": Role.STATUS_BAR,
    "p": Role.PARAGRAPH,
    "picture": Role.IMAGE,
    "pre": Role.SECTION,
    "progress": Role.PROGRESS_BAR,
    "search": Role.SEARCH,
    "section": Role.SECTION,
    "select": Role.COMBO_BOX,
    "summary": Role.BUTTON,
    "svg": Role.IMAGE,
    "table": Role.TABLE,
    "tbody": Role.GROUP,
    "td": Role.TABLE_CELL,
    "textarea": Role.TEXT_FIELD,
    "tfoot": Role.GROUP,
    "th": Role.TABLE_COLUMN_HEADER,
    "thead": Role.GROUP,
    "tr": Role.TABLE_ROW,
    "ul": Role.LIST,
    "video": Role.GROUP,
}

# Refine <input type="..."> → Role.
_INPUT_TYPE_ROLE: dict[str, Role] = {
    "button": Role.BUTTON,
    "checkbox": Role.CHECK_BOX,
    "color": Role.BUTTON,
    "email": Role.TEXT_FIELD,
    "file": Role.BUTTON,
    "hidden": Role.UNKNOWN,
    "image": Role.BUTTON,
    "number": Role.SPIN_BUTTON,
    "password": Role.PASSWORD_TEXT,
    "radio": Role.RADIO_BUTTON,
    "range": Role.SLIDER,
    "reset": Role.BUTTON,
    "search": Role.TEXT_FIELD,
    "submit": Role.BUTTON,
    "tel": Role.TEXT_FIELD,
    "text": Role.TEXT_FIELD,
    "url": Role.TEXT_FIELD,
}

# Explicit [role="..."] → Touchpoint Role.
_ARIA_ROLE_MAP: dict[str, Role] = {
    "alert": Role.ALERT,
    "alertdialog": Role.ALERT_DIALOG,
    "article": Role.ARTICLE,
    "banner": Role.BANNER,
    "button": Role.BUTTON,
    "cell": Role.TABLE_CELL,
    "checkbox": Role.CHECK_BOX,
    "combobox": Role.COMBO_BOX,
    "complementary": Role.LANDMARK,
    "contentinfo": Role.CONTENT_INFO,
    "dialog": Role.DIALOG,
    "document": Role.DOCUMENT,
    "feed": Role.FEED,
    "figure": Role.FIGURE,
    "form": Role.FORM,
    "grid": Role.GRID,
    "gridcell": Role.GRID_CELL,
    "group": Role.GROUP,
    "heading": Role.HEADING,
    "img": Role.IMAGE,
    "link": Role.LINK,
    "list": Role.LIST,
    "listbox": Role.LIST,
    "listitem": Role.LIST_ITEM,
    "log": Role.LOG,
    "main": Role.LANDMARK,
    "math": Role.MATH,
    "menu": Role.MENU,
    "menubar": Role.MENU_BAR,
    "menuitem": Role.MENU_ITEM,
    "menuitemcheckbox": Role.CHECK_MENU_ITEM,
    "menuitemradio": Role.RADIO_MENU_ITEM,
    "meter": Role.METER,
    "navigation": Role.NAVIGATION,
    "note": Role.NOTE,
    "option": Role.LIST_ITEM,
    "progressbar": Role.PROGRESS_BAR,
    "radio": Role.RADIO_BUTTON,
    "radiogroup": Role.GROUP,
    "region": Role.SECTION,
    "row": Role.TABLE_ROW,
    "rowgroup": Role.GROUP,
    "rowheader": Role.TABLE_ROW_HEADER,
    "scrollbar": Role.SCROLL_BAR,
    "search": Role.SEARCH,
    "searchbox": Role.TEXT_FIELD,
    "separator": Role.SEPARATOR,
    "slider": Role.SLIDER,
    "spinbutton": Role.SPIN_BUTTON,
    "status": Role.STATUS_BAR,
    "switch": Role.SWITCH,
    "tab": Role.TAB,
    "table": Role.TABLE,
    "tablist": Role.TAB_LIST,
    "tabpanel": Role.TAB_PANEL,
    "textbox": Role.TEXT_FIELD,
    "timer": Role.TIMER,
    "toolbar": Role.TOOLBAR,
    "tooltip": Role.TOOLTIP,
    "tree": Role.TREE,
    "treegrid": Role.TREE,
    "treeitem": Role.TREE_ITEM,
}

# ---------------------------------------------------------------------------
# JS DOM walker (injected via Runtime.evaluate in source="dom" mode)
# ---------------------------------------------------------------------------
# Walks the live DOM, extracts visible elements with text/interactivity,
# respects maxElements and maxDepth.  Returns a JSON array of objects.
# ---------------------------------------------------------------------------

_DOM_WALKER_JS = r"""
(function(rootSelector, maxElements, maxDepth) {
    var INTERACTIVE = new Set([
        'a','button','input','select','textarea','details','summary'
    ]);
    var results = [];
    var count = 0;

    function isVisible(el) {
        if (el.offsetWidth === 0 && el.offsetHeight === 0) return false;
        var st = window.getComputedStyle(el);
        if (st.display === 'none' || st.visibility === 'hidden') return false;
        if (parseFloat(st.opacity) === 0) return false;
        return true;
    }

    function directText(el) {
        var text = '';
        for (var i = 0; i < el.childNodes.length; i++) {
            if (el.childNodes[i].nodeType === 3) {
                text += el.childNodes[i].textContent;
            }
        }
        return text.trim();
    }

    function walk(node, depth) {
        if (count >= maxElements) return;
        if (depth > maxDepth) return;
        if (node.nodeType !== 1) return;
        if (!isVisible(node)) return;
        // Skip entire subtree of aria-hidden elements — they are
        // removed from the accessibility tree including all children.
        if (node.getAttribute('aria-hidden') === 'true') return;

        var tag = node.tagName.toLowerCase();
        var ariaRole = node.getAttribute('role') || '';
        var ariaLabel = node.getAttribute('aria-label') || '';
        var ariaDesc = node.getAttribute('aria-describedby');
        var title = node.getAttribute('title') || '';

        // Get direct text content (not inherited from children).
        var dText = directText(node);

        // Name priority: aria-label > title > direct text > full textContent for leaf nodes.
        var name = ariaLabel || title || dText;
        if (!name && node.children.length === 0) {
            name = (node.textContent || '').trim();
        }

        // Input value.  Use ?? (nullish coalescing) so that empty
        // strings are preserved — || would coerce "" to null.
        var value = null;
        if (tag === 'input' || tag === 'textarea' || tag === 'select') {
            value = node.value ?? null;
        }

        // Determine if this element is meaningful.
        var isInteractive = INTERACTIVE.has(tag) ||
            node.hasAttribute('onclick') ||
            node.hasAttribute('tabindex') ||
            ariaRole !== '';
        var hasText = !!(name || value);

        if (isInteractive || hasText) {
            var rect = node.getBoundingClientRect();
            var inputType = (tag === 'input') ? (node.type || 'text') : '';
            var descText = '';
            if (ariaDesc) {
                descText = ariaDesc.split(/\s+/).map(function(id) {
                    var el = document.getElementById(id);
                    return el ? (el.textContent || '').trim() : '';
                }).filter(Boolean).join(' ');
            }

            // Collect states.
            var disabled = node.disabled === true || node.getAttribute('aria-disabled') === 'true';
            var checked = node.checked === true || node.getAttribute('aria-checked') === 'true';
            var expanded = node.getAttribute('aria-expanded');
            var hidden = node.getAttribute('aria-hidden') === 'true';
            var required = node.required === true || node.getAttribute('aria-required') === 'true';
            var readOnly = node.readOnly === true || node.getAttribute('aria-readonly') === 'true';
            var focused = document.activeElement === node;

            results.push({
                tag: tag,
                role: ariaRole,
                inputType: inputType,
                name: name.substring(0, 2000),
                value: value ? value.substring(0, 2000) : null,
                description: descText ? descText.substring(0, 1000) : null,
                title: title || null,
                x: Math.round(rect.left + window.scrollX),
                y: Math.round(rect.top + window.scrollY),
                rectX: Math.round(rect.left),
                rectY: Math.round(rect.top),
                w: Math.round(rect.width),
                h: Math.round(rect.height),
                disabled: disabled,
                checked: checked,
                expanded: expanded,
                hidden: hidden,
                required: required,
                readOnly: readOnly,
                focused: focused,
                depth: depth
            });
            count++;
            if (count >= maxElements) return;
        }

        // Recurse into shadow DOM children, then light DOM children.
        // Both must be walked so that slotted content is not lost.
        if (node.shadowRoot) {
            for (var i = 0; i < node.shadowRoot.children.length; i++) {
                walk(node.shadowRoot.children[i], depth + 1);
            }
        }
        for (var i = 0; i < node.children.length; i++) {
            walk(node.children[i], depth + 1);
        }
    }

    var startNode = rootSelector
        ? document.querySelector(rootSelector)
        : document.body;
    if (!startNode) startNode = document.body;
    walk(startNode, 0);
    return JSON.stringify(results);
})
"""

# ---------------------------------------------------------------------------
# CDP modifier key flag constants (for Input.dispatchKeyEvent)
# ---------------------------------------------------------------------------

_MODIFIER_ALT = 1
_MODIFIER_CTRL = 2
_MODIFIER_META = 4
_MODIFIER_SHIFT = 8

# ---------------------------------------------------------------------------
# Key name → CDP key descriptor mapping
# ---------------------------------------------------------------------------
# Maps Touchpoint canonical key names to (key, code, keyCode) tuples
# for Input.dispatchKeyEvent.
# ---------------------------------------------------------------------------

_KEY_MAP: dict[str, tuple[str, str, int]] = {
    # Navigation
    "enter": ("Enter", "Enter", 13),
    "tab": ("Tab", "Tab", 9),
    "escape": ("Escape", "Escape", 27),
    "backspace": ("Backspace", "Backspace", 8),
    "delete": ("Delete", "Delete", 46),
    "space": (" ", "Space", 32),
    "up": ("ArrowUp", "ArrowUp", 38),
    "down": ("ArrowDown", "ArrowDown", 40),
    "left": ("ArrowLeft", "ArrowLeft", 37),
    "right": ("ArrowRight", "ArrowRight", 39),
    "home": ("Home", "Home", 36),
    "end": ("End", "End", 35),
    "pageup": ("PageUp", "PageUp", 33),
    "pagedown": ("PageDown", "PageDown", 34),
    # Function keys
    "f1": ("F1", "F1", 112),
    "f2": ("F2", "F2", 113),
    "f3": ("F3", "F3", 114),
    "f4": ("F4", "F4", 115),
    "f5": ("F5", "F5", 116),
    "f6": ("F6", "F6", 117),
    "f7": ("F7", "F7", 118),
    "f8": ("F8", "F8", 119),
    "f9": ("F9", "F9", 120),
    "f10": ("F10", "F10", 121),
    "f11": ("F11", "F11", 122),
    "f12": ("F12", "F12", 123),
    # Modifiers (for press_key / hotkey — key name only, no text)
    "ctrl": ("Control", "ControlLeft", 17),
    "alt": ("Alt", "AltLeft", 18),
    "shift": ("Shift", "ShiftLeft", 16),
    "super": ("Meta", "MetaLeft", 91),
    "meta": ("Meta", "MetaLeft", 91),
}


# ---------------------------------------------------------------------------
# WebSocket connection — thin sync wrapper
# ---------------------------------------------------------------------------

class _CDPConnection:
    """Synchronous CDP WebSocket connection.

    Wraps ``websocket-client`` to send JSON-RPC commands and receive
    responses.  Supports session multiplexing via ``session_id``.
    """

    def __init__(self, ws_url: str) -> None:
        self._ws_url = ws_url
        self._ws: Any = None
        self._next_id = 0

    def connect(self) -> None:
        """Open the WebSocket connection."""
        import websocket  # websocket-client

        self._ws = websocket.create_connection(
            self._ws_url,
            timeout=10,
            suppress_origin=True,
        )

    def close(self) -> None:
        """Close the WebSocket connection."""
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None

    @property
    def connected(self) -> bool:
        """Whether the WebSocket is open."""
        return self._ws is not None and self._ws.connected

    def send(
        self,
        method: str,
        params: dict | None = None,
        session_id: str | None = None,
        timeout: float = 30.0,
    ) -> dict:
        """Send a CDP command and wait for the response.

        Args:
            method: CDP method (e.g. ``"Accessibility.getFullAXTree"``).
            params: Method parameters.
            session_id: Target session ID for page-level commands.
            timeout: Response timeout in seconds.

        Returns:
            The ``result`` dict from the response.

        Raises:
            RuntimeError: If the command fails or times out.
        """
        if self._ws is None:
            raise RuntimeError("CDP connection is not open")

        self._next_id += 1
        msg_id = self._next_id
        msg: dict[str, Any] = {"id": msg_id, "method": method}
        if params:
            msg["params"] = params
        if session_id:
            msg["sessionId"] = session_id

        old_timeout = self._ws.gettimeout()
        self._ws.settimeout(timeout)
        try:
            self._ws.send(json.dumps(msg))

            # Read messages until we get a response matching our id.
            # Discard events and responses to other sessions.
            while True:
                raw = self._ws.recv()
                resp = json.loads(raw)

                # Auto-dismiss JavaScript dialogs (alert, confirm,
                # prompt, beforeunload) to prevent page blocking.
                if resp.get("method") == "Page.javascriptDialogOpening":
                    evt_session = resp.get("sessionId")
                    dialog_type = (
                        resp.get("params", {}).get("type", "")
                    )
                    # Reject beforeunload dialogs to prevent
                    # accidental navigation and data loss.
                    accept = dialog_type != "beforeunload"
                    self._next_id += 1
                    dismiss: dict[str, Any] = {
                        "id": self._next_id,
                        "method": "Page.handleJavaScriptDialog",
                        "params": {"accept": accept},
                    }
                    if evt_session:
                        dismiss["sessionId"] = evt_session
                    try:
                        self._ws.send(json.dumps(dismiss))
                    except Exception:
                        pass
                    continue

                if resp.get("id") == msg_id:
                    if "error" in resp:
                        err = resp["error"]
                        raise RuntimeError(
                            f"CDP error ({err.get('code', '?')}): "
                            f"{err.get('message', 'unknown')}"
                        )
                    return resp.get("result", {})
        finally:
            self._ws.settimeout(old_timeout)


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------

def discover_cdp_ports() -> dict[int, int]:
    """Discover processes launched with ``--remote-debugging-port``.

    On Linux, scans ``/proc/*/cmdline``.  On Windows, uses PowerShell
    ``Get-CimInstance`` to query process command lines.  Returns an
    empty dict on unsupported platforms.

    Returns:
        Dict mapping ``{port: pid}`` — always the main browser process
        PID, not a renderer/zygote child PID.
    """
    if sys.platform.startswith("linux"):
        return _discover_cdp_ports_linux()
    if sys.platform == "win32":
        return _discover_cdp_ports_windows()
    if sys.platform == "darwin":
        return _discover_cdp_ports_darwin()
    return {}


def _discover_cdp_ports_linux() -> dict[int, int]:
    """Linux implementation: scan ``/proc/*/cmdline``."""
    ports: dict[int, int] = {}
    proc = "/proc"
    try:
        entries = os.listdir(proc)
    except OSError:
        return ports

    for entry in entries:
        if not entry.isdigit():
            continue
        cmdline_path = os.path.join(proc, entry, "cmdline")
        try:
            with open(cmdline_path, "rb") as f:
                cmdline = f.read().decode("utf-8", errors="replace")
        except (OSError, PermissionError):
            continue

        # Skip Chrome/Electron child processes.  Renderer, zygote,
        # GPU, and utility processes all inherit the parent's cmdline
        # (including --remote-debugging-port) on Linux, so without
        # this guard we'd record a renderer PID instead of the main
        # browser PID.
        if re.search(r"--type=\w", cmdline):
            continue

        match = re.search(r"--remote-debugging-port=(\d+)", cmdline)
        if match:
            port = int(match.group(1))
            pid = int(entry)
            # Keep the smallest PID for this port — the parent process
            # always has a lower PID than its children.
            if port not in ports or pid < ports[port]:
                ports[port] = pid

    return ports


def _discover_cdp_ports_windows() -> dict[int, int]:
    """Windows implementation: query PowerShell for process command lines.

    Uses ``Get-CimInstance Win32_Process`` via PowerShell, which is
    available on all Windows versions from Win8 onward (Win10/11
    ship with PowerShell 5.1).  ``wmic`` was removed from Windows 11
    versions 24H2/25H2 and will be fully gone in 2026, so it is no
    longer used.

    Outputs CSV via ``ConvertTo-Csv`` so each row is one process with
    ``ProcessId`` and ``CommandLine`` columns — no blank-line state
    machine needed.
    """
    ports: dict[int, int] = {}
    ps_cmd = (
        "Get-CimInstance Win32_Process -Property ProcessId,CommandLine "
        "| Select-Object ProcessId,CommandLine "
        "| ConvertTo-Csv -NoTypeInformation"
    )
    try:
        output = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                ps_cmd,
            ],
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return ports

    reader = csv.DictReader(io.StringIO(output))
    for row in reader:
        cmdline = (row.get("CommandLine") or "").strip()
        if not cmdline:
            continue
        # Skip renderer/GPU/utility child processes.
        if re.search(r"--type=\w", cmdline):
            continue
        match = re.search(r"--remote-debugging-port=(\d+)", cmdline)
        if not match:
            continue
        try:
            pid = int(row.get("ProcessId") or 0)
        except ValueError:
            continue
        if not pid:
            continue
        port = int(match.group(1))
        # Keep the lowest PID for a given port (the browser main process).
        if port not in ports or pid < ports[port]:
            ports[port] = pid

    return ports


def _get_app_name_for_pid(pid: int) -> str:
    """Get the process name for *pid*.

    On Linux reads ``/proc/{pid}/comm``.  On Windows uses ``tasklist``.

    Returns:
        Process name (e.g. ``"electron"``, ``"chrome"``), or
        ``"unknown"`` on failure.
    """
    if sys.platform.startswith("linux"):
        return _get_app_name_linux(pid)
    if sys.platform == "win32":
        return _get_app_name_windows(pid)
    if sys.platform == "darwin":
        return _get_app_name_darwin(pid)
    return "unknown"


def _get_app_name_linux(pid: int) -> str:
    """Linux: read ``/proc/{pid}/comm``."""
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f.read().strip()
    except (OSError, PermissionError):
        return "unknown"


def _get_app_name_windows(pid: int) -> str:
    """Windows: query ``tasklist`` for the process image name."""
    try:
        output = subprocess.check_output(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            text=True,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    # Output: "image_name.exe","PID","Session","Mem",...
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("INFO:"):
            continue
        # First quoted field is the image name.
        if line.startswith('"'):
            name = line.split('"')[1]  # e.g. "chrome.exe"
            # Strip .exe suffix for consistency with Linux comm names.
            if name.lower().endswith(".exe"):
                name = name[:-4]
            return name.lower()
    return "unknown"


def _discover_cdp_ports_darwin() -> dict[int, int]:
    """macOS implementation: scan running processes via ``ps``.

    Uses ``ps -eo pid,args`` to list all processes and their full
    command lines, then searches for ``--remote-debugging-port=N``.
    Filters out renderer/GPU/utility child processes the same way
    as the Linux/Windows implementations.
    """
    ports: dict[int, int] = {}
    try:
        output = subprocess.check_output(
            ["ps", "-eo", "pid,args"],
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ports

    for line in output.splitlines()[1:]:  # skip header
        line = line.strip()
        if not line:
            continue
        # Skip renderer / GPU / utility child processes.
        if re.search(r"--type=\w", line):
            continue
        match = re.search(r"--remote-debugging-port=(\d+)", line)
        if not match:
            continue
        parts = line.split(None, 1)
        try:
            pid = int(parts[0])
        except (ValueError, IndexError):
            continue
        port = int(match.group(1))
        if port not in ports or pid < ports[port]:
            ports[port] = pid

    return ports


def _get_app_name_darwin(pid: int) -> str:
    """macOS: get process name via ``ps``."""
    try:
        output = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "comm="],
            text=True,
            timeout=5,
        )
        name = output.strip()
        if name:
            # ``ps -o comm=`` returns the full path; take basename.
            return os.path.basename(name).lower()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def _list_targets(port: int) -> list[dict]:
    """List CDP targets at the given debugging port.

    Args:
        port: The ``--remote-debugging-port`` value.

    Returns:
        List of target info dicts from ``/json/list``.
    """
    url = f"http://127.0.0.1:{port}/json/list"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            return json.loads(resp.read())
    except Exception:
        return []


def _get_browser_ws_url(port: int) -> str | None:
    """Get the browser-level WebSocket URL for a debugging port.

    Args:
        port: The ``--remote-debugging-port`` value.

    Returns:
        The ``webSocketDebuggerUrl`` or ``None``.
    """
    url = f"http://127.0.0.1:{port}/json/version"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            data = json.loads(resp.read())
            return data.get("webSocketDebuggerUrl")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# AXNode processing helpers
# ---------------------------------------------------------------------------

def _ax_property_value(
    properties: list[dict] | None, name: str,
) -> Any:
    """Extract a named property value from an AXNode's properties list.

    Returns:
        The property's ``value.value``, or ``None`` if not found.
    """
    if not properties:
        return None
    for prop in properties:
        if prop.get("name") == name:
            return prop.get("value", {}).get("value")
    return None


def _translate_cdp_role(node: dict) -> tuple[Role, str]:
    """Map a CDP AXNode's role to ``(Role, raw_role_string)``.

    Args:
        node: A CDP AXNode dict.

    Returns:
        ``(Role, raw_role)`` — the mapped role and the original
        CDP role string.
    """
    role_obj = node.get("role", {})
    raw_role = role_obj.get("value", "") if isinstance(role_obj, dict) else ""
    # CDP sends roles in varying case: PascalCase ("StaticText"),
    # camelCase ("staticText"), or all-lower ("treeitem").
    # Use a case-insensitive lookup against _CDP_ROLE_MAP.
    role = _CDP_ROLE_MAP_LOWER.get(raw_role.lower(), Role.UNKNOWN)

    # Detect password inputs: AX nodes for <input type="password">
    # report role "textField" but carry an inputType property.
    if role == Role.TEXT_FIELD:
        input_type = _ax_property_value(
            node.get("properties"), "inputType",
        )
        if input_type == "password":
            role = Role.PASSWORD_TEXT

    return role, raw_role


# Roles that imply SELECTABLE / CLICKABLE states (mirrors what
# AT-SPI and UIA backends always report for these roles).
_SELECTABLE_ROLES = frozenset({
    Role.LIST_ITEM, Role.TAB, Role.TREE_ITEM,
    Role.RADIO_BUTTON, Role.CHECK_BOX,
    Role.RADIO_MENU_ITEM, Role.CHECK_MENU_ITEM,
})
_CLICKABLE_ROLES = frozenset({
    Role.BUTTON, Role.LINK, Role.MENU_ITEM,
    Role.TOGGLE_BUTTON, Role.SWITCH, Role.SPLIT_BUTTON,
    Role.COMBO_BOX, Role.TAB,
})

# Tags and roles that are natively focusable in the DOM.
_FOCUSABLE_TAGS = frozenset({
    "a", "button", "input", "select", "textarea", "summary",
})
_FOCUSABLE_ROLES = frozenset({
    Role.BUTTON, Role.LINK, Role.CHECK_BOX, Role.RADIO_BUTTON,
    Role.TAB, Role.MENU_ITEM, Role.TOGGLE_BUTTON, Role.SWITCH,
    Role.COMBO_BOX, Role.SPLIT_BUTTON, Role.SLIDER,
    Role.SPIN_BUTTON, Role.TEXT_FIELD, Role.PASSWORD_TEXT,
    Role.CHECK_MENU_ITEM, Role.RADIO_MENU_ITEM, Role.TREE_ITEM,
})

# Action-inference role sets (shared by _build_element and
# _build_dom_element to avoid divergence).
_ACTION_CLICKABLE_ROLES = frozenset({
    Role.BUTTON, Role.LINK, Role.CHECK_BOX, Role.RADIO_BUTTON,
    Role.TAB, Role.MENU_ITEM, Role.TOGGLE_BUTTON, Role.SWITCH,
    Role.COMBO_BOX, Role.SPLIT_BUTTON,
    Role.CHECK_MENU_ITEM, Role.RADIO_MENU_ITEM,
})
_ACTION_EDITABLE_ROLES = frozenset({
    Role.TEXT_FIELD, Role.PASSWORD_TEXT, Role.COMBO_BOX,
})
_ACTION_RANGE_ROLES = frozenset({
    Role.SLIDER, Role.SPIN_BUTTON, Role.SCROLL_BAR,
})


def _translate_cdp_states(
    node: dict,
    role: Role | None = None,
) -> list[State]:
    """Map a CDP AXNode's properties to a list of Touchpoint States.

    Reads boolean/tristate/enum properties and translates them.
    Also infers inverse states (e.g. NOT disabled → ENABLED).

    Args:
        node: A CDP AXNode dict.
        role: Pre-computed role to avoid redundant re-resolution.
            If ``None``, the role is resolved from *node*.

    Returns:
        List of :class:`State` values.
    """
    props = node.get("properties")
    states: list[State] = []

    # Build a dict for O(1) lookups instead of scanning the
    # ~30-entry properties list 18 times.
    _prop_map: dict[str, Any] = {}
    if props:
        for p in props:
            pname = p.get("name")
            if pname is not None:
                _prop_map[pname] = p.get("value", {}).get("value")

    def _prop(name: str) -> Any:
        return _prop_map.get(name)

    # Hidden / ignored → skip the VISIBLE/SHOWING states.
    ignored = node.get("ignored", False)
    hidden = _prop("hidden")
    if not ignored and not hidden:
        states.append(State.VISIBLE)
        states.append(State.SHOWING)

    # Focusable / focused.
    if _prop("focusable"):
        states.append(State.FOCUSABLE)
    if _prop("focused"):
        states.append(State.FOCUSED)

    # Enabled / sensitive (inversion of "disabled").
    disabled = _prop("disabled")
    if not disabled:
        states.append(State.ENABLED)
        states.append(State.SENSITIVE)

    # Editable.
    editable = _prop("editable")
    if editable and editable != "false":
        states.append(State.EDITABLE)

    # Readonly.
    if _prop("readOnly"):
        states.append(State.READ_ONLY)

    # Multiline / single-line.
    multiline = _prop("multiline")
    if multiline:
        states.append(State.MULTI_LINE)
    elif editable and editable != "false":
        # Text fields without multiline are single-line.
        states.append(State.SINGLE_LINE)

    # Selection.
    if _prop("selected"):
        states.append(State.SELECTED)

    # Checked (boolean or tristate).
    checked = _prop("checked")
    if checked == "mixed":
        states.append(State.INDETERMINATE)
    elif checked and checked != "false":
        states.append(State.CHECKED)

    # Pressed.
    pressed = _prop("pressed")
    if pressed and pressed != "false":
        states.append(State.PRESSED)

    # Expanded / collapsed / expandable.
    expanded = _prop("expanded")
    if expanded is not None:
        states.append(State.EXPANDABLE)
        if expanded and expanded != "false" and expanded != 0:
            states.append(State.EXPANDED)
        else:
            states.append(State.COLLAPSED)

    # Modal.
    if _prop("modal"):
        states.append(State.MODAL)

    # Required.
    if _prop("required"):
        states.append(State.REQUIRED)

    # Invalid.
    invalid = _prop("invalid")
    if invalid and invalid != "false":
        states.append(State.INVALID)

    # Busy.
    if _prop("busy"):
        states.append(State.BUSY)

    # Has popup.
    has_popup = _prop("hasPopup")
    if has_popup and has_popup != "false":
        states.append(State.HAS_POPUP)

    # Multiselectable.
    if _prop("multiselectable"):
        states.append(State.MULTISELECTABLE)

    # Orientation.
    orientation = _prop("orientation")
    if orientation == "horizontal":
        states.append(State.HORIZONTAL)
    elif orientation == "vertical":
        states.append(State.VERTICAL)

    # Infer SELECTABLE / CLICKABLE from role (consistent with
    # AT-SPI and UIA backends which always report these).
    if role is None:
        role, _ = _translate_cdp_role(node)
    if role in _SELECTABLE_ROLES:
        states.append(State.SELECTABLE)
    if role in _CLICKABLE_ROLES:
        states.append(State.CLICKABLE)

    return states


# ---------------------------------------------------------------------------
# CDP Backend
# ---------------------------------------------------------------------------


class CdpBackend(Backend):
    """CDP backend for Electron and Chromium-based applications.

    Connects to one or more ``--remote-debugging-port`` endpoints and
    queries their accessibility trees via the CDP Accessibility domain.

    Each debugging port corresponds to one browser/Electron process.
    Each page target within that process becomes a Touchpoint
    "window".

    Args:
        configured_ports: Dict mapping ``{app_name: port}`` for
            explicitly registered CDP applications.
        auto_discover: If ``True``, scan ``/proc/*/cmdline`` for
            ``--remote-debugging-port`` flags.
    """

    ACTION_ALIASES: dict[str, list[str]] = {
        "click": ["click"],
        "double_click": ["double_click"],
        "right_click": ["right_click"],
    }

    def __init__(
        self,
        configured_ports: dict[str, int] | None = None,
        auto_discover: bool = True,
    ) -> None:
        self._configured_ports: dict[str, int] = configured_ports or {}
        self._auto_discover = auto_discover

        # port → _CDPConnection (browser-level WebSocket).
        self._connections: dict[int, _CDPConnection] = {}
        # port → {targetId: sessionId} for attached page targets.
        self._sessions: dict[int, dict[str, str]] = {}
        # port → pid.
        self._port_pids: dict[int, int] = {}
        # pid → app_name (from /proc or configured).
        self._pid_names: dict[int, str] = {}
        # All known CDP PIDs (for multi-backend routing).
        self._cdp_pids: set[int] = set()

        # Per-call state for element collection.
        self._element_count: int = 0
        self._max_elements: int = sys.maxsize
        self._filter_role: Role | None = None
        self._filter_states: list[State] | None = None
        self._filter_named_only: bool = False

        # nodeId → backendDOMNodeId cache (per session).
        # Structure: {(port, targetId): {nodeId: backendDOMNodeId}}.
        self._dom_node_cache: dict[tuple[int, str], dict[str, int]] = {}

        # Viewport offset cache: (port, targetId) → (offset_x, offset_y).
        # Cleared at the start of each get_elements / get_element_at call.
        self._viewport_cache: dict[tuple[int, str], tuple[int, int]] = {}

        # Platform PID → display name (injected by the public API via
        # set_pid_display_names so we can resolve display names like
        # "Google Chrome" without importing touchpoint — avoids a
        # circular dependency).
        self._platform_display_names: dict[int, str] = {}

        # Run initial discovery.
        self.refresh_targets()

    # -- Discovery / connection management --------------------------------

    def refresh_targets(self) -> None:
        """Discover CDP ports and connect to them.

        Called on init and can be called again to pick up newly
        launched CDP applications.
        """
        # 0. Clean up dead connections and stale caches.
        dead_ports = [
            p for p, c in self._connections.items()
            if not c.connected
        ]
        for port in dead_ports:
            conn = self._connections.pop(port, None)
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            self._sessions.pop(port, None)
            stale_keys = [
                k for k in self._dom_node_cache if k[0] == port
            ]
            for k in stale_keys:
                del self._dom_node_cache[k]
            # Clean up PID tracking so stale PIDs don't cause
            # mis-routing after the connection dies.
            pid = self._port_pids.pop(port, None)
            if pid is not None and pid not in self._port_pids.values():
                self._cdp_pids.discard(pid)
                self._pid_names.pop(pid, None)
        self._viewport_cache.clear()

        # 1. Gather ports from auto-discovery + explicit config.
        ports: dict[int, int] = {}  # port → pid

        if self._auto_discover:
            ports.update(discover_cdp_ports())

        for app_name, port in self._configured_ports.items():
            if port not in ports:
                ports[port] = 0
            # Register the configured name.  Use the real PID when
            # known; for undiscovered ports (pid=0), key by -port to
            # avoid all offline ports colliding on _pid_names[0].
            pid = ports.get(port, 0)
            key = pid if pid else -port
            self._pid_names[key] = app_name

        # 2. Update port → pid mappings.
        self._port_pids.update(ports)
        for port, pid in ports.items():
            if pid:
                self._cdp_pids.add(pid)
                if pid not in self._pid_names:
                    self._pid_names[pid] = _get_app_name_for_pid(pid)

        # 3. Connect to new ports.
        for port in ports:
            if port in self._connections and self._connections[port].connected:
                continue
            ws_url = _get_browser_ws_url(port)
            if ws_url is None:
                continue
            conn = _CDPConnection(ws_url)
            try:
                conn.connect()
            except Exception:
                continue
            self._connections[port] = conn
            self._sessions[port] = {}

    def close(self) -> None:
        """Close all CDP WebSocket connections and clear caches."""
        for conn in list(self._connections.values()):
            try:
                conn.close()
            except Exception:
                pass
        self._connections.clear()
        self._sessions.clear()
        self._dom_node_cache.clear()
        self._viewport_cache.clear()
        self._port_pids.clear()
        self._pid_names.clear()
        self._cdp_pids.clear()

    # -- Backend ABC: routing methods -------------------------------------

    def get_owned_pids(self) -> set[int]:
        """Return the set of browser PIDs this CDP backend manages."""
        return set(self._cdp_pids)

    def owns_element(self, element_id: str) -> bool:
        """Return ``True`` if *element_id* was produced by this backend."""
        return isinstance(element_id, str) and element_id.startswith("cdp:")

    def claims_app(self, app_name: str) -> bool:
        """Return ``True`` if *app_name* maps to a CDP-managed process.

        Matches against the process comm name stored in ``_pid_names``
        (e.g. ``"chrome"`` on Linux, ``"chrome.exe"`` on Windows)
        and against the platform display names injected via
        :meth:`set_pid_display_names`.
        """
        if not self._cdp_pids:
            return False
        app_lower = app_name.lower()
        # Route 1 — direct comm-name match.
        for pid, name in self._pid_names.items():
            if name.lower() == app_lower and pid in self._cdp_pids:
                return True
        # Route 2 — platform display-name match.
        for pid, name in self._platform_display_names.items():
            if name.lower() == app_lower and pid in self._cdp_pids:
                return True
        return False

    def set_pid_display_names(self, mapping: dict[int, str]) -> None:
        """Store platform display names for cross-backend name resolution."""
        self._platform_display_names = mapping

    def get_pids_for_app(self, app_name: str) -> set[int]:
        """Return the PIDs whose comm-name matches *app_name*."""
        app_lower = app_name.lower()
        return {
            pid for pid, name in self._pid_names.items()
            if name.lower() == app_lower
        }

    def get_topmost_pid_at(self, x: int, y: int) -> int | None:
        """CDP cannot determine the topmost window at a coordinate."""
        return None

    def _ensure_session(self, port: int, target_id: str) -> str | None:
        """Attach to a page target if not already attached.

        Returns:
            The ``sessionId``, or ``None`` on failure.
        """
        sessions = self._sessions.get(port)
        if sessions is None:
            return None
        if target_id in sessions:
            return sessions[target_id]

        conn = self._connections.get(port)
        if conn is None or not conn.connected:
            return None

        try:
            result = conn.send("Target.attachToTarget", {
                "targetId": target_id,
                "flatten": True,
            })
            session_id = result.get("sessionId")
            if session_id:
                sessions[target_id] = session_id
                # Enable required CDP domains for this session.
                for domain in ("Accessibility", "DOM", "Page"):
                    try:
                        conn.send(
                            f"{domain}.enable", {},
                            session_id=session_id,
                        )
                    except Exception:
                        logger.warning(
                            "Failed to enable %s domain for "
                            "target %s on port %d",
                            domain, target_id, port,
                        )
            return session_id
        except Exception:
            return None

    def _get_targets(
        self, port: int, types: set[str] | None = None,
    ) -> list[dict]:
        """List targets for a given port, filtered by type.

        Args:
            port: CDP debugging port.
            types: Target types to include (e.g. ``{"page"}`` or
                ``{"page", "iframe"}``).  Defaults to ``{"page"}``.

        Returns:
            List of matching target info dicts.
        """
        if types is None:
            types = {"page"}
        targets = _list_targets(port)
        return [t for t in targets if t.get("type") in types]

    def _send(
        self,
        port: int,
        target_id: str,
        method: str,
        params: dict | None = None,
        timeout: float = 30.0,
    ) -> dict:
        """Send a CDP command to a specific page target.

        Ensures the session is attached before sending.

        Returns:
            The ``result`` dict.

        Raises:
            RuntimeError: If connection/session is unavailable or
                the command fails.
        """
        session_id = self._ensure_session(port, target_id)
        if session_id is None:
            raise RuntimeError(
                f"Cannot attach to CDP target {target_id} on port {port}"
            )
        conn = self._connections.get(port)
        if conn is None or not conn.connected:
            raise RuntimeError(
                f"No CDP connection for port {port}"
            )
        try:
            return conn.send(method, params, session_id=session_id,
                             timeout=timeout)
        except RuntimeError as exc:
            # Retry once on stale session errors (e.g. after navigation).
            err_msg = str(exc).lower()
            if "session" in err_msg or "target" in err_msg:
                self._sessions.get(port, {}).pop(target_id, None)
                self._dom_node_cache.pop((port, target_id), None)
                session_id = self._ensure_session(port, target_id)
                if session_id is None:
                    raise
                return conn.send(method, params,
                                 session_id=session_id,
                                 timeout=timeout)
            raise

    # -- Backend interface ------------------------------------------------

    def is_available(self) -> bool:
        """Check whether websocket-client is installed.

        Does not check whether any CDP app is currently running —
        that is connection state, not capability.
        """
        try:
            import websocket  # noqa: F401
        except ImportError:
            return False
        return True

    def get_applications(self) -> list[str]:
        """List CDP application names.

        Returns one application name per unique CDP process (port).
        """
        apps: list[str] = []
        for port, pid in self._port_pids.items():
            if port not in self._connections:
                continue
            name = self._pid_names.get(pid, _get_app_name_for_pid(pid))
            if name and name not in apps:
                apps.append(name)
        return apps

    def get_windows(self) -> list[Window]:
        """List all page targets as Touchpoint windows.

        Each browser tab / Electron window becomes a Window whose
        ``id`` is ``"cdp:{port}:{targetId}"``.
        """
        windows: list[Window] = []
        for port in list(self._connections):
            pid = self._port_pids.get(port, 0)
            app_name = self._pid_names.get(pid, "unknown")
            targets = self._get_targets(port, types={"page"})
            for target in targets:
                target_id = target.get("id", "")
                title = target.get("title", "")

                # Get real window bounds via JS (works on all
                # Chromium/Electron apps, unlike
                # Browser.getWindowForTarget which is Chrome-only).
                position, size, is_active = self._get_window_bounds(
                    port, target_id,
                )

                windows.append(Window(
                    id=f"cdp:{port}:{target_id}",
                    title=title,
                    app=app_name,
                    pid=pid,
                    position=position,
                    size=size,
                    is_active=is_active,
                    is_visible=True,
                    raw={"url": target.get("url", "")},
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
        """Get UI elements from the CDP accessibility tree.

        Calls ``Accessibility.getFullAXTree`` to retrieve the full
        accessibility tree, filters ignored nodes, re-parents children
        of ignored nodes, and builds :class:`Element` instances.

        When ``role``, ``states``, or ``named_only`` are provided and
        ``tree=False``, ``queryAXTree`` is used as a server-side
        optimization when possible.
        """
        # Reset per-call state.
        self._element_count = 0
        self._max_elements = (
            max_elements if max_elements is not None else sys.maxsize
        )
        self._filter_role = role if not tree else None
        self._filter_states = states if not tree else None
        self._filter_named_only = named_only and not tree
        self._viewport_cache.clear()

        # Determine which port+target combinations to query.
        targets = self._resolve_targets(app, window_id)
        if not targets:
            return []

        all_elements: list[Element] = []

        for port, target_id, app_name, pid in targets:
            if self._element_count >= self._max_elements:
                break

            win_id = f"cdp:{port}:{target_id}"

            # Handle root_element scoping.
            root_node_id: str | None = None
            if root_element is not None:
                r_parts = self._parse_id(root_element)
                if (r_parts["port"] != port
                        or r_parts["target_id"] != target_id):
                    continue
                rn = r_parts["node_id"]
                if rn.startswith("dom:"):
                    # DOM-sourced IDs cannot scope an AX tree walk.
                    # Silently skip rather than return wrong results.
                    continue
                root_node_id = rn

            # Decide strategy: queryAXTree or getFullAXTree.
            nodes = self._fetch_ax_tree(
                port, target_id,
                max_depth=max_depth,
                root_node_id=root_node_id,
                query_role=role if not tree else None,
                query_name=None,  # queryAXTree name filter is exact
            )
            if not nodes:
                continue

            # Cache DOM node IDs.  Store *every* nodeId so that
            # nodes without a backendDOMNodeId are recorded as -1
            # (sentinel) and won't trigger a full AX tree refetch
            # later in _resolve_backend_node_id().
            cache_key = (port, target_id)
            self._dom_node_cache[cache_key] = {}
            for node in nodes:
                nid = node.get("nodeId", "")
                if not nid:
                    continue
                backend_nid = node.get("backendDOMNodeId")
                self._dom_node_cache[cache_key][nid] = (
                    backend_nid if backend_nid is not None else -1
                )

            if tree:
                tree_elements = self._build_tree(
                    nodes, port, target_id, app_name, pid,
                    win_id, max_depth, root_node_id,
                )
                # Graft cross-origin iframe subtrees under FRAME
                # nodes when building a tree.
                iframe_info = self._get_iframe_targets_for_page(
                    port, target_id,
                )
                for iframe_tid, frame_id in iframe_info:
                    if self._element_count >= self._max_elements:
                        break
                    self._graft_iframe_tree(
                        port, iframe_tid, frame_id,
                        target_id, app_name, pid,
                        tree_elements, max_depth,
                    )
                all_elements.extend(tree_elements)
            else:
                flat_elements = self._build_flat(
                    nodes, port, target_id, app_name, pid,
                    win_id, lightweight,
                    root_node_id=root_node_id,
                )
                all_elements.extend(flat_elements)

        return all_elements

    # ------------------------------------------------------------------
    # DOM-based element discovery (source="dom")
    # ------------------------------------------------------------------

    def get_dom_elements(
        self,
        app: str | None = None,
        window_id: str | None = None,
        root_element: str | None = None,
        tree: bool = False,
        max_depth: int | None = None,
        max_elements: int | None = None,
        role: Role | None = None,
        states: list[State] | None = None,
        named_only: bool = False,
    ) -> list[Element]:
        """Get UI elements by walking the live DOM.

        Unlike :meth:`get_elements` which uses the accessibility tree,
        this walks the real DOM and extracts all visible elements
        that have text content or are interactive.  This catches
        content the AX tree misses (canvas-rendered apps, virtual
        lists, unannotated divs, shadow DOM).

        Element IDs use a ``d``-prefixed ``backendNodeId`` to
        distinguish them from AX-sourced elements.
        """
        self._viewport_cache.clear()

        effective_max = (
            max_elements if max_elements is not None else sys.maxsize
        )
        effective_depth = max_depth if max_depth is not None else 50

        targets = self._resolve_targets(app, window_id)
        if not targets:
            return []

        all_elements: list[Element] = []
        count = 0

        for port, target_id, app_name, pid in targets:
            if count >= effective_max:
                break

            win_id = f"cdp:{port}:{target_id}"

            # Handle root_element scoping.
            root_selector = "null"
            if root_element is not None:
                r_parts = self._parse_id(root_element)
                if (r_parts["port"] != port
                        or r_parts["target_id"] != target_id):
                    continue
                # For DOM elements (dom:{x},{y} prefix), resolve to a
                # CSS selector via a temporary data attribute.
                node_id_str = r_parts["node_id"]
                if node_id_str.startswith("dom:"):
                    coords = node_id_str[4:]
                    try:
                        css_x_s, css_y_s = coords.split(",", 1)
                        css_x = int(css_x_s)
                        css_y = int(css_y_s)
                    except ValueError:
                        continue
                    # Resolve to backendNodeId via coordinates.
                    try:
                        loc_result = self._send(
                            port, target_id,
                            "DOM.getNodeForLocation",
                            {
                                "x": css_x,
                                "y": css_y,
                                "includeUserAgentShadowDOM": False,
                                "ignorePointerEventsNone": False,
                            },
                        )
                        backend_nid = loc_result.get("backendNodeId")
                        if not backend_nid:
                            continue
                        # Inject a temporary marker for the root selector.
                        sel_id = f"tp_root_{backend_nid}"
                        desc = self._send(
                            port, target_id,
                            "DOM.describeNode",
                            {"backendNodeId": backend_nid},
                        )
                        self._send(
                            port, target_id,
                            "DOM.setAttributeValue",
                            {
                                "nodeId": desc["node"]["nodeId"],
                                "name": "data-tp-root",
                                "value": sel_id,
                            },
                        )
                        root_selector = f"'[data-tp-root=\"{sel_id}\"]'"
                    except Exception:
                        continue
                else:
                    # AX node ID — can't use as DOM root, skip.
                    continue

            remaining = effective_max - count

            # When Python-side filters (role, named_only, states) are
            # active, the JS walker may return mostly non-matching
            # elements.  Fetch the full DOM and let Python enforce
            # the limit after filtering.
            has_filters = role is not None or named_only or states
            js_limit = 50_000 if has_filters else remaining

            # Inject the DOM walker.
            try:
                expr = (
                    f"{_DOM_WALKER_JS}"
                    f"({root_selector}, {js_limit}, {effective_depth})"
                )
                result = self._send(
                    port, target_id,
                    "Runtime.evaluate",
                    {"expression": expr, "returnByValue": True},
                )
            except Exception:
                continue

            value = result.get("result", {}).get("value")
            if not value:
                continue

            try:
                dom_nodes = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                continue

            # Get screen offset for coordinate conversion.
            off_x, off_y = self._get_screen_offset(port, target_id)

            # Build Element objects.
            for dnode in dom_nodes:
                if count >= effective_max:
                    break

                el = self._build_dom_element(
                    dnode, app_name, pid, port, target_id,
                    win_id, off_x, off_y,
                )
                if el is None:
                    continue  # aria-hidden element, dropped

                # Apply filters.
                if role is not None and el.role != role:
                    continue
                if named_only and not (el.name and el.name.strip()):
                    continue
                if states:
                    if not all(s in el.states for s in states):
                        continue

                all_elements.append(el)
                count += 1

            # Clean up injected root marker if we set one.
            if root_selector != "null" and "data-tp-root" in root_selector:
                try:
                    self._send(
                        port, target_id,
                        "Runtime.evaluate",
                        {
                            "expression": (
                                "document.querySelector("
                                f"{root_selector}"
                                ").removeAttribute('data-tp-root')"
                            ),
                            "returnByValue": True,
                        },
                    )
                except Exception:
                    pass

        return all_elements

    def _build_dom_element(
        self,
        dnode: dict,
        app_name: str,
        pid: int,
        port: int,
        target_id: str,
        win_id: str,
        off_x: int,
        off_y: int,
    ) -> Element | None:
        """Build an :class:`Element` from a DOM walker result dict.

        Returns ``None`` for aria-hidden elements (they should be
        dropped entirely, not tagged OFFSCREEN).

        No CDP roundtrips are made during discovery.  The element's
        CSS-viewport center coordinates are encoded directly into the
        element ID (``dom:{cx},{cy}`` suffix).  The ``backendNodeId``
        is resolved lazily, only when an action is actually requested
        via :meth:`do_action`, :meth:`set_value`, or
        :meth:`focus_element`.
        """
        tag = dnode.get("tag", "div")
        aria_role = dnode.get("role", "")
        input_type = dnode.get("inputType", "")

        # Determine role.
        if aria_role:
            role = _ARIA_ROLE_MAP.get(
                aria_role.lower(), Role.UNKNOWN,
            )
            raw_role = aria_role
        elif tag == "input" and input_type:
            role = _INPUT_TYPE_ROLE.get(
                input_type.lower(), Role.TEXT_FIELD,
            )
            raw_role = f"input[{input_type}]"
        else:
            role = _TAG_ROLE_MAP.get(tag, Role.SECTION)
            raw_role = tag

        # Name.
        name = dnode.get("name", "") or ""

        # Value.
        value = dnode.get("value")

        # Description.
        description = dnode.get("description")

        # States.
        el_states: list[State] = [State.VISIBLE, State.SHOWING]
        if not dnode.get("disabled", False):
            el_states.append(State.ENABLED)
            el_states.append(State.SENSITIVE)
        if dnode.get("checked", False):
            el_states.append(State.CHECKED)
        if dnode.get("focused", False):
            el_states.append(State.FOCUSED)
        if dnode.get("required", False):
            el_states.append(State.REQUIRED)
        if dnode.get("readOnly", False):
            el_states.append(State.READ_ONLY)

        # aria-hidden elements should be dropped entirely, not tagged.
        if dnode.get("hidden", False):
            return None

        expanded = dnode.get("expanded")
        if expanded == "true":
            el_states.extend([State.EXPANDABLE, State.EXPANDED])
        elif expanded == "false":
            el_states.extend([State.EXPANDABLE, State.COLLAPSED])

        # Position & size.
        # x/y are page (document) coords — stable across scrolls, used in the ID.
        # rectX/rectY are viewport coords at discovery time — used for element.position.
        css_x = dnode.get("x", 0)
        css_y = dnode.get("y", 0)
        rect_x = dnode.get("rectX", css_x)
        rect_y = dnode.get("rectY", css_y)
        w = dnode.get("w", 0)
        h = dnode.get("h", 0)
        cx = off_x + rect_x + w // 2
        cy = off_y + rect_y + h // 2

        # Convert CSS/logical coords to physical pixels for the
        # public API (screenshot-compatible coordinate space).
        _s = get_scale_factor(cx, cy)
        cx = round(cx * _s)
        cy = round(cy * _s)
        w = round(w * _s)
        h = round(h * _s)

        # Focusable heuristic — natively focusable tags plus roles that
        # are inherently interactive.  Plain "presentation" / "none" /
        # structural roles are NOT focusable.
        if tag in _FOCUSABLE_TAGS or role in _FOCUSABLE_ROLES:
            el_states.append(State.FOCUSABLE)

        # SELECTABLE / CLICKABLE inferred from role (consistent with
        # AX source and AT-SPI/UIA backends).
        if role in _SELECTABLE_ROLES:
            el_states.append(State.SELECTABLE)
        if role in _CLICKABLE_ROLES:
            el_states.append(State.CLICKABLE)

        # EDITABLE / MULTI_LINE / SINGLE_LINE for text inputs.
        if tag == "textarea":
            el_states.extend([State.EDITABLE, State.MULTI_LINE])
        elif role in _ACTION_EDITABLE_ROLES:
            el_states.extend([State.EDITABLE, State.SINGLE_LINE])

        # Actions.
        actions: list[str] = []
        if role in _ACTION_CLICKABLE_ROLES:
            actions.append("click")
        if role in _ACTION_EDITABLE_ROLES:
            actions.append("set-text")
        if role in _ACTION_RANGE_ROLES:
            actions.append("set-value")

        # Page (document) center coords in CSS pixels — embedded in
        # the ID so that clicks can scroll-then-click without a CDP
        # roundtrip.  Use the *original* CSS w/h (from dnode), not the
        # physical-scaled w/h, because the click path sends these as
        # CSS coordinates to Input.dispatchMouseEvent.
        w_css = dnode.get("w", 0)
        h_css = dnode.get("h", 0)
        center_css_x = css_x + w_css // 2
        center_css_y = css_y + h_css // 2

        # ID encodes page-space coords (rect + scrollX/Y from JS walker).
        # Format:  cdp:{port}:{targetId}:dom:{page_cx},{page_cy}
        eid = f"cdp:{port}:{target_id}:dom:{center_css_x},{center_css_y}"

        return Element(
            id=eid,
            name=name,
            role=role,
            states=el_states,
            position=(cx, cy),
            size=(w, h),
            app=app_name,
            pid=pid,
            backend="cdp",
            raw_role=raw_role,
            actions=actions,
            value=value,
            description=description,
            window_id=win_id,
            raw={"tag": tag, "source": "dom"},
        )

    def get_element_at(self, x: int, y: int) -> Element | None:
        """Get the deepest element at a screen coordinate.

        Uses ``document.elementFromPoint()`` via JS to hit-test
        within CDP targets, then resolves the resulting DOM node
        to its accessibility tree entry.  Coordinates are expected
        in screen-absolute **physical** pixels; this method converts
        to viewport-relative CSS pixels for each target.

        Note: This method is called by the multi-backend router only
        when the PID at (x, y) is a known CDP PID.

        Returns:
            The deepest :class:`Element` at ``(x, y)``, or ``None``.
        """
        self._viewport_cache.clear()

        # Convert incoming physical coords to logical/CSS for
        # comparison with window offsets and elementFromPoint.
        _s = get_scale_factor(x, y)
        logical_x = round(x / _s)
        logical_y = round(y / _s)

        # Try each connected port/target.
        for port in list(self._connections):
            pid = self._port_pids.get(port, 0)
            app_name = self._pid_names.get(pid, "unknown")
            targets = self._get_targets(port, types={"page", "iframe"})

            for target in targets:
                target_id = target.get("id", "")
                try:
                    # Convert logical screen coords to viewport
                    # coords using real window bounds (both logical).
                    off_x, off_y = self._get_screen_offset(
                        port, target_id,
                    )
                    vp_x = logical_x - off_x
                    vp_y = logical_y - off_y

                    if vp_x < 0 or vp_y < 0:
                        continue

                    # Use JS elementFromPoint to hit-test.  This is
                    # reliable across all Chrome/Chromium versions
                    # (unlike DOM.getNodeForLocation which can fail
                    # silently).
                    resolve_result = self._send(
                        port, target_id,
                        "Runtime.evaluate",
                        {
                            "expression": (
                                "(function(x,y){"
                                "var el=document.elementFromPoint(x,y);"
                                "if(!el)return null;"
                                "return el;"
                                f"}})({vp_x},{vp_y})"
                            ),
                            "returnByValue": False,
                        },
                    )
                    remote_obj = resolve_result.get("result", {})
                    object_id = remote_obj.get("objectId")
                    if object_id is None:
                        continue

                    try:
                        # Resolve the JS object to a DOM node.
                        desc_result = self._send(
                            port, target_id,
                            "DOM.describeNode",
                            {"objectId": object_id},
                        )
                        backend_nid = (
                            desc_result.get("node", {})
                            .get("backendNodeId")
                        )
                        if backend_nid is None:
                            continue
                    finally:
                        # Always release the JS object reference.
                        try:
                            self._send(
                                port, target_id,
                                "Runtime.releaseObject",
                                {"objectId": object_id},
                            )
                        except Exception:
                            pass

                    # Get the AX node for this DOM node.
                    ax_result = self._send(
                        port, target_id,
                        "Accessibility.getPartialAXTree",
                        {"backendNodeId": backend_nid,
                         "fetchRelatives": False},
                    )
                    ax_nodes = ax_result.get("nodes", [])
                    if not ax_nodes:
                        continue

                    node = ax_nodes[0]
                    if node.get("ignored"):
                        continue

                    win_id = f"cdp:{port}:{target_id}"
                    node_id = node.get("nodeId", "")
                    eid = f"cdp:{port}:{target_id}:{node_id}"
                    return self._build_element(
                        node, app_name, pid, eid,
                        port, target_id,
                        window_id=win_id,
                    )
                except Exception:
                    continue

        return None

    def get_element_by_id(self, element_id: str) -> Element | None:
        """Retrieve a single element by its CDP id.

        ID format: ``"cdp:{port}:{targetId}:{nodeId}"``.

        Fetches the AXNode via ``Accessibility.getPartialAXTree``
        and builds a full Element with box model geometry.
        """
        parts = self._parse_id(element_id)
        port = parts["port"]
        target_id = parts["target_id"]
        node_id = parts["node_id"]

        pid = self._port_pids.get(port, 0)
        app_name = self._pid_names.get(pid, "unknown")
        win_id = f"cdp:{port}:{target_id}"

        # DOM-sourced elements: re-discover via page coordinates.
        if node_id.startswith("dom:"):
            coords = node_id[4:]
            try:
                css_x_s, css_y_s = coords.split(",", 1)
                css_x = int(css_x_s)
                css_y = int(css_y_s)
            except ValueError:
                return None
            try:
                vp_x, vp_y = self._scroll_to_vp(port, target_id, css_x, css_y)
                loc = self._send(
                    port, target_id,
                    "DOM.getNodeForLocation",
                    {
                        "x": vp_x,
                        "y": vp_y,
                        "includeUserAgentShadowDOM": False,
                        "ignorePointerEventsNone": False,
                    },
                )
                backend_nid = loc.get("backendNodeId")
                if backend_nid is None:
                    return None
                result = self._send(
                    port, target_id,
                    "Accessibility.getPartialAXTree",
                    {"backendNodeId": backend_nid, "fetchRelatives": False},
                )
                ax_nodes = result.get("nodes", [])
                if ax_nodes:
                    return self._build_element(
                        ax_nodes[0], app_name, pid, element_id,
                        port, target_id, window_id=win_id,
                    )
            except Exception:
                pass
            return None

        # Look up the backendDOMNodeId from cache.
        cache_key = (port, target_id)
        cache = self._dom_node_cache.get(cache_key, {})
        backend_nid = cache.get(node_id)

        # Try to fetch the node by backendDOMNodeId if available.
        # Otherwise fall back to fetching by nodeId (via full tree
        # scan).
        ax_nodes: list[dict] = []
        if backend_nid is not None:
            try:
                result = self._send(
                    port, target_id,
                    "Accessibility.getPartialAXTree",
                    {"backendNodeId": backend_nid,
                     "fetchRelatives": False},
                )
                ax_nodes = result.get("nodes", [])
            except Exception:
                pass

        if not ax_nodes:
            # Fallback: fetch the full AX tree and search for the
            # matching nodeId.
            try:
                all_nodes = self._fetch_ax_tree(
                    port, target_id,
                )
                for node in all_nodes:
                    # Repopulate the cache while we're here.
                    nid = node.get("nodeId", "")
                    bnid = node.get("backendDOMNodeId")
                    if bnid is not None:
                        self._dom_node_cache.setdefault(
                            cache_key, {},
                        )[nid] = bnid
                    if nid == node_id:
                        return self._build_element(
                            node, app_name, pid, element_id,
                            port, target_id,
                            window_id=win_id,
                        )
            except Exception:
                return None

        # Find the matching node.
        for node in ax_nodes:
            if node.get("nodeId") == node_id:
                return self._build_element(
                    node, app_name, pid, element_id,
                    port, target_id,
                    window_id=win_id,
                )

        # If only one node returned, use it.  When >1 are returned
        # (target + relatives), use the first non-ignored node — the
        # backendDOMNodeId lookup already ensures the correct DOM node.
        if len(ax_nodes) >= 1:
            best = ax_nodes[0]
            for candidate in ax_nodes:
                if not candidate.get("ignored", False):
                    best = candidate
                    break
            return self._build_element(
                best, app_name, pid, element_id,
                port, target_id,
                window_id=win_id,
            )

        return None

    def inflate_element(self, element: Element) -> Element:
        """Inflate a lightweight element with box model geometry.

        Fetches ``DOM.getBoxModel`` for the element and updates
        its position and size fields.
        """
        parts = self._parse_id(element.id)
        port = parts["port"]
        target_id = parts["target_id"]
        node_id = parts["node_id"]

        position, size = self._get_box_model(port, target_id, node_id)
        return Element(
            id=element.id,
            name=element.name,
            role=element.role,
            states=element.states,
            position=position,
            size=size,
            app=element.app,
            pid=element.pid,
            backend="cdp",
            raw_role=element.raw_role,
            actions=element.actions,
            value=element.value,
            description=element.description,
            children=element.children,
            parent_id=element.parent_id,
            window_id=element.window_id,
            raw=element.raw,
        )

    # -- Actions ----------------------------------------------------------

    def do_action(self, element_id: str, action: str) -> bool:
        """Perform an action on a CDP element.

        Dispatches mouse events via CDP's Input domain after
        scrolling the element into view and computing its center
        coordinates.
        """
        parts = self._parse_id(element_id)
        port = parts["port"]
        target_id = parts["target_id"]
        node_id = parts["node_id"]
        action_lower = action.lower()

        # DOM-sourced elements: dispatch mouse events directly at the
        # DOM fast path for clicks: stored page coords, zero extra
        # roundtrips at discovery time.  One atomic JS call scrolls the
        # element into view and returns fresh viewport coords.
        if node_id.startswith("dom:") and action_lower in (
            "click", "double_click", "right_click",
        ):
            coords = node_id[4:]
            try:
                css_x_s, css_y_s = coords.split(",", 1)
                css_x = int(css_x_s)
                css_y = int(css_y_s)
            except ValueError:
                raise ActionFailedError(
                    action=action,
                    element_id=element_id,
                    reason=f"malformed DOM element ID coords: {coords!r}",
                ) from None
            vp_x, vp_y = self._scroll_to_vp(port, target_id, css_x, css_y)
            button = "right" if action_lower == "right_click" else "left"
            click_count = 2 if action_lower == "double_click" else 1
            try:
                for etype, extra in [
                    ("mouseMoved", {}),
                    ("mousePressed", {"button": button, "clickCount": click_count}),
                    ("mouseReleased", {"button": button, "clickCount": click_count}),
                ]:
                    self._send(
                        port, target_id,
                        "Input.dispatchMouseEvent",
                        {"type": etype, "x": vp_x, "y": vp_y, **extra},
                    )
            except Exception as exc:
                raise ActionFailedError(
                    action=action,
                    element_id=element_id,
                    reason=f"mouse dispatch failed: {exc}",
                ) from exc
            return True

        # AX-sourced elements (or DOM elements needing set_value/focus):
        # resolve backendNodeId lazily.
        backend_nid = self._resolve_backend_node_id(
            port, target_id, node_id,
        )
        if backend_nid is None:
            raise ActionFailedError(
                action=action,
                element_id=element_id,
                reason="element not found in the accessibility tree",
            )

        if action_lower in ("click", "double_click", "right_click"):
            return self._click_element(
                port, target_id, backend_nid,
                element_id, action_lower,
            )

        raise ActionFailedError(
            action=action,
            element_id=element_id,
            reason=f"action {action!r} not supported by CDP backend",
        )

    def set_value(self, element_id: str, value: str, replace: bool) -> bool:
        """Set text value on a CDP element.

        For ``<select>`` elements, selects the ``<option>`` whose text
        or value matches *value* (case-insensitive).  For all other
        elements, focuses, optionally selects all existing text
        (for replace mode), then inserts the new text.
        """
        parts = self._parse_id(element_id)
        port = parts["port"]
        target_id = parts["target_id"]
        node_id = parts["node_id"]

        backend_nid = self._resolve_backend_node_id(
            port, target_id, node_id,
        )
        if backend_nid is None:
            raise ActionFailedError(
                action="set_value",
                element_id=element_id,
                reason="element not found in the accessibility tree",
            )

        # Detect <select> elements for option-selection logic.
        try:
            desc = self._send(port, target_id, "DOM.describeNode", {
                "backendNodeId": backend_nid, "depth": 0,
            })
            node_name = desc.get("node", {}).get(
                "nodeName", "",
            ).upper()
        except Exception:
            node_name = ""

        if node_name == "SELECT":
            return self._set_select_value(
                port, target_id, backend_nid, element_id, value,
            )

        # Focus the element.
        try:
            self._send(port, target_id, "DOM.focus", {
                "backendNodeId": backend_nid,
            })
        except Exception as exc:
            raise ActionFailedError(
                action="set_value",
                element_id=element_id,
                reason=f"cannot focus element: {exc}",
            ) from exc

        if replace:
            # Select all existing text (Ctrl+A on Linux/Windows, Cmd+A on macOS).
            mod = _MODIFIER_META if sys.platform == "darwin" else _MODIFIER_CTRL
            self._dispatch_key(port, target_id, "a", modifiers=mod)

        # Insert the text.
        try:
            self._send(port, target_id, "Input.insertText", {
                "text": value,
            })
        except Exception as exc:
            raise ActionFailedError(
                action="set_value",
                element_id=element_id,
                reason=f"insertText failed: {exc}",
            ) from exc

        return True

    def _set_select_value(
        self,
        port: int,
        target_id: str,
        backend_nid: int,
        element_id: str,
        value: str,
    ) -> bool:
        """Select an ``<option>`` by text or value on a ``<select>``.

        Matches option text (trimmed) or option value attribute
        against *value*, case-insensitively.
        """
        try:
            resolve_result = self._send(
                port, target_id, "DOM.resolveNode",
                {"backendNodeId": backend_nid},
            )
            object_id = resolve_result.get(
                "object", {},
            ).get("objectId")
            if not object_id:
                raise RuntimeError("resolveNode returned no objectId")
        except Exception as exc:
            raise ActionFailedError(
                action="set_value",
                element_id=element_id,
                reason=f"cannot resolve select element: {exc}",
            ) from exc

        try:
            result = self._send(port, target_id, "Runtime.callFunctionOn", {
                "objectId": object_id,
                "functionDeclaration": (
                    "function(val) {"
                    "  var vl = val.toLowerCase();"
                    "  for (var i = 0; i < this.options.length; i++) {"
                    "    var t = this.options[i].text.trim();"
                    "    if (t === val || t.toLowerCase() === vl"
                    "        || this.options[i].value === val"
                    "        || this.options[i].value.toLowerCase() === vl) {"
                    "      this.selectedIndex = i;"
                    "      this.dispatchEvent("
                    "        new Event('input', {bubbles: true}));"
                    "      this.dispatchEvent("
                    "        new Event('change', {bubbles: true}));"
                    "      return true;"
                    "    }"
                    "  }"
                    "  return false;"
                    "}"
                ),
                "arguments": [{"value": value}],
                "returnByValue": True,
            })
            matched = result.get("result", {}).get("value", False)
            if not matched:
                raise ActionFailedError(
                    action="set_value",
                    element_id=element_id,
                    reason=f"no <option> matching {value!r}",
                )
            return True
        except ActionFailedError:
            raise
        except Exception as exc:
            raise ActionFailedError(
                action="set_value",
                element_id=element_id,
                reason=f"select option failed: {exc}",
            ) from exc
        finally:
            try:
                self._send(
                    port, target_id, "Runtime.releaseObject",
                    {"objectId": object_id},
                )
            except Exception:
                pass

    def set_numeric_value(
        self, element_id: str, value: float,
    ) -> bool:
        """Set a numeric value on a range element (slider, etc).

        Uses ``Runtime.callFunctionOn`` to set the element's
        ``.value`` property and dispatch an ``input`` event.
        """
        parts = self._parse_id(element_id)
        port = parts["port"]
        target_id = parts["target_id"]
        node_id = parts["node_id"]

        backend_nid = self._resolve_backend_node_id(
            port, target_id, node_id,
        )
        if backend_nid is None:
            raise ActionFailedError(
                action="set_numeric_value",
                element_id=element_id,
                reason="element not found in the accessibility tree",
            )

        # Resolve to a RemoteObject for JS evaluation.
        try:
            resolve_result = self._send(
                port, target_id, "DOM.resolveNode", {
                    "backendNodeId": backend_nid,
                },
            )
            object_id = resolve_result.get("object", {}).get("objectId")
            if not object_id:
                raise RuntimeError("resolveNode returned no objectId")
        except Exception as exc:
            raise ActionFailedError(
                action="set_numeric_value",
                element_id=element_id,
                reason=f"cannot resolve DOM node: {exc}",
            ) from exc

        try:
            self._send(port, target_id, "Runtime.callFunctionOn", {
                "objectId": object_id,
                "functionDeclaration": (
                    "function(v) { "
                    "  this.value = v; "
                    "  this.dispatchEvent(new Event('input', "
                    "    {bubbles: true})); "
                    "  this.dispatchEvent(new Event('change', "
                    "    {bubbles: true})); "
                    "}"
                ),
                "arguments": [{"value": value}],
            })
        except Exception as exc:
            raise ActionFailedError(
                action="set_numeric_value",
                element_id=element_id,
                reason=f"callFunctionOn failed: {exc}",
            ) from exc
        finally:
            try:
                self._send(
                    port, target_id, "Runtime.releaseObject",
                    {"objectId": object_id},
                )
            except Exception:
                pass

        return True

    def focus_element(self, element_id: str) -> bool:
        """Move keyboard focus to a CDP element.

        Uses ``DOM.focus()`` with the element's backendNodeId.
        """
        parts = self._parse_id(element_id)
        port = parts["port"]
        target_id = parts["target_id"]
        node_id = parts["node_id"]

        backend_nid = self._resolve_backend_node_id(
            port, target_id, node_id,
        )
        if backend_nid is None:
            raise ActionFailedError(
                action="focus",
                element_id=element_id,
                reason="element not found in the accessibility tree",
            )

        try:
            self._send(port, target_id, "DOM.focus", {
                "backendNodeId": backend_nid,
            })
            return True
        except Exception as exc:
            raise ActionFailedError(
                action="focus",
                element_id=element_id,
                reason=f"DOM.focus failed: {exc}",
            ) from exc

    def select_text(
        self, element_id: str, start: int, end: int,
    ) -> bool:
        """Select a range of text within a CDP element.

        For ``<input>`` and ``<textarea>`` elements, uses
        ``setSelectionRange()``.  For contentEditable elements
        and other text nodes, uses the ``Selection`` API with
        ``setBaseAndExtent()``.
        """
        parts = self._parse_id(element_id)
        port = parts["port"]
        target_id = parts["target_id"]
        node_id = parts["node_id"]

        backend_nid = self._resolve_backend_node_id(
            port, target_id, node_id,
        )
        if backend_nid is None:
            raise ActionFailedError(
                action="select_text",
                element_id=element_id,
                reason="element not found in the accessibility tree",
            )

        # Focus the element first.
        try:
            self._send(port, target_id, "DOM.focus", {
                "backendNodeId": backend_nid,
            })
        except Exception:
            pass  # best-effort — continue to select anyway

        # Resolve to a JS object reference.
        try:
            resolve_result = self._send(
                port, target_id, "DOM.resolveNode",
                {"backendNodeId": backend_nid},
            )
            object_id = resolve_result.get(
                "object", {},
            ).get("objectId")
            if not object_id:
                raise RuntimeError("resolveNode returned no objectId")
        except Exception as exc:
            raise ActionFailedError(
                action="select_text",
                element_id=element_id,
                reason=f"cannot resolve element: {exc}",
            ) from exc

        try:
            result = self._send(port, target_id, "Runtime.callFunctionOn", {
                "objectId": object_id,
                "functionDeclaration": (
                    "function(s, e) {"
                    "  if (typeof this.setSelectionRange === 'function') {"
                    "    try { this.setSelectionRange(s, e); return true; }"
                    "    catch(_) {}"
                    "  }"
                    "  if (this.nodeType === 3) {"
                    "    var sel = window.getSelection();"
                    "    var range = document.createRange();"
                    "    range.setStart(this, s);"
                    "    range.setEnd(this, e);"
                    "    sel.removeAllRanges();"
                    "    sel.addRange(range);"
                    "    return true;"
                    "  }"
                    "  var el = this;"
                    "  if (!el.isContentEditable) {"
                    "    while (el && !el.isContentEditable) el = el.parentElement;"
                    "  }"
                    "  if (el) {"
                    "    var tw = document.createTreeWalker("
                    "      el, NodeFilter.SHOW_TEXT, null, false);"
                    "    var node, charCount = 0, startNode, startOff, endNode, endOff;"
                    "    while ((node = tw.nextNode())) {"
                    "      var len = node.textContent.length;"
                    "      if (!startNode && charCount + len > s) {"
                    "        startNode = node; startOff = s - charCount;"
                    "      }"
                    "      if (!endNode && charCount + len >= e) {"
                    "        endNode = node; endOff = e - charCount; break;"
                    "      }"
                    "      charCount += len;"
                    "    }"
                    "    if (!startNode || !endNode) return false;"
                    "    var sel = window.getSelection();"
                    "    var range = document.createRange();"
                    "    range.setStart(startNode, startOff);"
                    "    range.setEnd(endNode, endOff);"
                    "    sel.removeAllRanges();"
                    "    sel.addRange(range);"
                    "    return true;"
                    "  }"
                    "  return false;"
                    "}"
                ),
                "arguments": [{"value": start}, {"value": end}],
                "returnByValue": True,
            })
            success = result.get("result", {}).get("value", False)
            if not success:
                raise ActionFailedError(
                    action="select_text",
                    element_id=element_id,
                    reason="element does not support text selection "
                           "(not an input, textarea, or contentEditable)",
                )
            return True
        except ActionFailedError:
            raise
        except Exception as exc:
            raise ActionFailedError(
                action="select_text",
                element_id=element_id,
                reason=f"selection failed: {exc}",
            ) from exc
        finally:
            try:
                self._send(
                    port, target_id, "Runtime.releaseObject",
                    {"objectId": object_id},
                )
            except Exception:
                pass

    def activate_window(self, window_id: str) -> bool:
        """Activate a CDP window (browser tab).

        Uses ``Target.activateTarget`` and ``Page.bringToFront``.
        """
        parts = self._parse_id(window_id)
        port = parts["port"]
        target_id = parts["target_id"]

        conn = self._connections.get(port)
        if conn is None or not conn.connected:
            return False

        try:
            conn.send("Target.activateTarget", {
                "targetId": target_id,
            })
        except Exception:
            pass

        try:
            self._send(port, target_id, "Page.bringToFront", {})
            return True
        except Exception:
            return False

    # -- Private helpers --------------------------------------------------

    def _parse_id(self, id_str: str) -> dict[str, Any]:
        """Parse a CDP element/window ID.

        Formats:
            ``"cdp:{port}:{targetId}"`` — window
            ``"cdp:{port}:{targetId}:{axNodeId}"`` — AX element
            ``"cdp:{port}:{targetId}:dom:{px},{py}"`` — DOM element
                (coords are page-space / document coordinates, i.e.
                ``getBoundingClientRect() + scrollX/Y``; viewport
                coords are computed fresh at action time)

        Returns:
            Dict with keys ``port`` (int), ``target_id`` (str),
            ``node_id`` (str or ``""``).

        Raises:
            ValueError: If the ID format is invalid.
        """
        parts = id_str.split(":", 3)
        if len(parts) < 3 or parts[0] != "cdp":
            raise ValueError(f"Malformed CDP ID: {id_str!r}")
        try:
            port = int(parts[1])
        except ValueError:
            raise ValueError(f"Malformed CDP ID: {id_str!r}") from None
        target_id = parts[2]
        node_id = parts[3] if len(parts) > 3 else ""
        return {
            "port": port,
            "target_id": target_id,
            "node_id": node_id,
        }

    def _resolve_targets(
        self,
        app: str | None,
        window_id: str | None,
    ) -> list[tuple[int, str, str, int]]:
        """Resolve scoping params to ``(port, targetId, app, pid)``
        tuples.
        """
        if window_id is not None:
            parts = self._parse_id(window_id)
            port = parts["port"]
            target_id = parts["target_id"]
            pid = self._port_pids.get(port, 0)
            app_name = self._pid_names.get(pid, "unknown")
            return [(port, target_id, app_name, pid)]

        results: list[tuple[int, str, str, int]] = []
        app_lower = app.lower() if app else None

        for port in list(self._connections):
            pid = self._port_pids.get(port, 0)
            app_name = self._pid_names.get(pid, "unknown")

            if app_lower is not None:
                # Exact comm-name match (case-insensitive).
                # If the user-supplied app name doesn't match the
                # /proc/comm name, check the platform display-name
                # mapping injected by the public API.  This avoids
                # importing touchpoint (circular dependency).
                #
                # We intentionally do NOT use substring matching
                # (e.g. "chrome" in "google chrome") because it causes
                # false positives ("chrome" would match "chromebook-app").
                if app_name.lower() != app_lower:
                    display = self._platform_display_names.get(pid, "")
                    if display.lower() != app_lower:
                        continue

            targets = self._get_targets(port, types={"page"})
            for target in targets:
                target_id = target.get("id", "")
                results.append((port, target_id, app_name, pid))

        return results

    def _fetch_ax_tree(
        self,
        port: int,
        target_id: str,
        max_depth: int | None = None,
        root_node_id: str | None = None,
        query_role: Role | None = None,
        query_name: str | None = None,
    ) -> list[dict]:
        """Fetch the accessibility tree from CDP.

        Uses ``queryAXTree`` when role/name filters allow, otherwise
        falls back to ``getFullAXTree``.

        Returns:
            List of AXNode dicts.
        """
        # Try queryAXTree for targeted server-side filtering
        # (only when not fetching a full tree from a root and
        # max_depth is not set — queryAXTree returns full-depth
        # subtrees which defeats depth limiting).
        if (root_node_id is None
                and max_depth is None
                and (query_role is not None or query_name is not None)):
            params: dict[str, Any] = {}
            if query_role is not None:
                # queryAXTree expects the CDP role string.
                # Reverse-map from Touchpoint Role → CDP string.
                cdp_role = self._role_to_cdp_string(query_role)
                if cdp_role:
                    params["role"] = cdp_role
            if query_name is not None:
                params["accessibleName"] = query_name

            if params:
                try:
                    result = self._send(
                        port, target_id,
                        "Accessibility.queryAXTree", params,
                    )
                    nodes = result.get("nodes", [])
                    if nodes:
                        return nodes
                except Exception:
                    pass
            # Fall through to getFullAXTree if queryAXTree fails
            # or returns empty results.

        # Default: getFullAXTree.
        params = {}
        if max_depth is not None:
            params["depth"] = max_depth + 2  # CDP depth=1 → root only; +2 gives max_depth levels below root
        if root_node_id is not None:
            params["accessibilityNodeId"] = root_node_id
        try:
            result = self._send(
                port, target_id,
                "Accessibility.getFullAXTree", params or None,
            )
            return result.get("nodes", [])
        except Exception:
            return []

    def _build_flat(
        self,
        nodes: list[dict],
        port: int,
        target_id: str,
        app_name: str,
        pid: int,
        win_id: str,
        lightweight: bool,
        root_node_id: str | None = None,
    ) -> list[Element]:
        """Build a flat list of Elements from AXNodes.

        Filters out ignored nodes.  Re-parents children of ignored
        nodes are naturally handled because the flat list doesn't
        use the parent/child relationships.

        When *root_node_id* is provided, only elements that are
        descendants of (or equal to) that node are included.  This
        is a safety net for when ``Accessibility.getFullAXTree``
        returns the full tree despite ``accessibilityNodeId`` being
        set (older Chrome versions).
        """        # Pre-compute the set of node IDs in the requested subtree.
        # BFS from root_node_id using the parentId links in reverse.
        descendant_ids: set[str] | None = None
        if root_node_id is not None:
            children_map: dict[str, list[str]] = {}
            for _n in nodes:
                _nid = _n.get("nodeId", "")
                _pnid = _n.get("parentId", "")
                if _nid and _pnid:
                    children_map.setdefault(_pnid, []).append(_nid)
            descendant_ids = set()
            queue = [root_node_id]
            while queue:
                cur = queue.pop()
                descendant_ids.add(cur)
                queue.extend(children_map.get(cur, []))
        elements: list[Element] = []

        for node in nodes:
            if self._element_count >= self._max_elements:
                break

            # Skip ignored nodes.
            if node.get("ignored"):
                continue

            # Enforce subtree scoping (safety net for older Chrome that
            # ignores accessibilityNodeId in getFullAXTree).
            if descendant_ids is not None:
                node_id_check = node.get("nodeId", "")
                if node_id_check not in descendant_ids:
                    continue

            # Skip the root "rootWebArea" in flat mode — it's the
            # document itself, not a meaningful UI element.
            # Also filter inlineTextBox and lineBreak — these are
            # text-rendering internals, never useful as elements.
            role_val = node.get("role", {})
            raw_role = (
                role_val.get("value", "")
                if isinstance(role_val, dict) else ""
            )
            if raw_role == "rootWebArea" and self._filter_role is None:
                continue
            if raw_role in ("inlineTextBox", "lineBreak"):
                continue

            # Apply filters.
            pre = self._check_filter(node)
            if pre is None:
                continue

            node_id = node.get("nodeId", "")
            eid = f"cdp:{port}:{target_id}:{node_id}"

            # Determine parent_id.
            parent_nid = node.get("parentId")
            parent_id: str | None = None
            if parent_nid:
                parent_id = f"cdp:{port}:{target_id}:{parent_nid}"

            if lightweight:
                role, raw_r, states = pre
                el = Element(
                    id=eid,
                    name=self._ax_name(node),
                    role=role,
                    states=states,
                    position=(0, 0),
                    size=(0, 0),
                    app=app_name,
                    pid=pid,
                    backend="cdp",
                    raw_role=raw_r,
                    parent_id=parent_id,
                    window_id=win_id,
                )
            else:
                el = self._build_element(
                    node, app_name, pid, eid,
                    port, target_id,
                    parent_id=parent_id,
                    window_id=win_id,
                    _pre=pre,
                )

            # Post-filter: if PASSWORD_TEXT was requested but _build_element
            # resolved the DOM node as a regular text field, skip it.
            if (self._filter_role == Role.PASSWORD_TEXT
                    and el.role != Role.PASSWORD_TEXT):
                continue

            elements.append(el)
            self._element_count += 1

        return elements

    def _build_tree(
        self,
        nodes: list[dict],
        port: int,
        target_id: str,
        app_name: str,
        pid: int,
        win_id: str,
        max_depth: int | None,
        root_node_id: str | None,
    ) -> list[Element]:
        """Build a tree of Elements from AXNodes.

        Re-parents children of ignored nodes to their nearest
        non-ignored ancestor.
        """
        # Index nodes by nodeId.
        node_map: dict[str, dict] = {}
        for node in nodes:
            nid = node.get("nodeId", "")
            if nid:
                node_map[nid] = node

        # Build a parent→children mapping, skipping ignored nodes.
        # For ignored nodes, connect their children to their parent.
        children_map: dict[str, list[str]] = {}
        parent_map: dict[str, str] = {}

        def _find_real_parent(nid: str) -> str | None:
            """Walk up until we find a non-ignored parent."""
            visited: set[str] = set()
            current = nid
            while current in node_map:
                if current in visited:
                    break
                visited.add(current)
                p = node_map[current].get("parentId")
                if p is None:
                    return None
                if p not in node_map:
                    return None
                if not node_map[p].get("ignored"):
                    return p
                current = p
            return None

        for node in nodes:
            nid = node.get("nodeId", "")
            if not nid:
                continue
            if node.get("ignored"):
                continue
            real_parent = _find_real_parent(nid)
            if real_parent:
                parent_map[nid] = real_parent
                children_map.setdefault(real_parent, []).append(nid)

        # Find root nodes (non-ignored, no non-ignored parent).
        root_nids: list[str] = []
        if root_node_id and root_node_id in node_map:
            root_nids = [root_node_id]
        else:
            for node in nodes:
                nid = node.get("nodeId", "")
                if not nid or node.get("ignored"):
                    continue
                if nid not in parent_map:
                    root_nids.append(nid)

        # Recursively build element trees.
        def _build_subtree(
            nid: str, depth: int,
        ) -> Element | None:
            if self._element_count >= self._max_elements:
                return None
            node = node_map.get(nid)
            if node is None or node.get("ignored"):
                return None

            # Filter text-rendering internals.
            _rv = node.get("role", {})
            _rr = _rv.get("value", "") if isinstance(_rv, dict) else ""
            if _rr in ("inlineTextBox", "lineBreak"):
                return None

            eid = f"cdp:{port}:{target_id}:{nid}"
            p_nid = parent_map.get(nid)
            p_id = (
                f"cdp:{port}:{target_id}:{p_nid}"
                if p_nid else None
            )

            el = self._build_element(
                node, app_name, pid, eid,
                port, target_id,
                parent_id=p_id,
                window_id=win_id,
            )
            self._element_count += 1

            if max_depth is not None and depth >= max_depth:
                return el

            for child_nid in children_map.get(nid, []):
                if self._element_count >= self._max_elements:
                    break
                child_el = _build_subtree(child_nid, depth + 1)
                if child_el is not None:
                    el.children.append(child_el)

            return el

        result: list[Element] = []
        for root_nid in root_nids:
            if self._element_count >= self._max_elements:
                break
            el = _build_subtree(root_nid, 0)
            if el is not None:
                result.append(el)

        return result

    def _check_filter(
        self, node: dict,
    ) -> tuple[Role, str, list[State]] | None:
        """Check a CDP AXNode against active filter hints.

        Returns ``None`` when the element should be skipped.
        Otherwise returns ``(role, raw_role, states)`` for the
        builder to use.
        """
        if self._filter_named_only:
            name = self._ax_name(node)
            if not (name and name.strip()):
                return None

        role, raw_role = _translate_cdp_role(node)

        if self._filter_role is not None:
            if role != self._filter_role:
                # Allow TEXT_FIELD through when PASSWORD_TEXT is requested
                # because Chrome AX reports passwords as textbox; the
                # _build_element step resolves it via DOM.
                if not (self._filter_role == Role.PASSWORD_TEXT
                        and role == Role.TEXT_FIELD):
                    return None

        states = _translate_cdp_states(node, role=role)

        if self._filter_states:
            if not all(s in states for s in self._filter_states):
                return None

        return role, raw_role, states

    def _build_element(
        self,
        node: dict,
        app_name: str,
        pid: int,
        element_id: str,
        port: int,
        target_id: str,
        parent_id: str | None = None,
        window_id: str | None = None,
        _pre: tuple[Role, str, list[State]] | None = None,
    ) -> Element:
        """Build a full :class:`Element` from a CDP AXNode.

        Includes box model geometry via ``DOM.getBoxModel``.
        """
        if _pre is not None:
            role, raw_role, states = _pre
        else:
            role, raw_role = _translate_cdp_role(node)
            states = _translate_cdp_states(node, role=role)

        # Detect <input type="password"> — Chrome's AX tree reports
        # these as "textbox" with no distinguishing AX property, so we
        # must resolve via the backing DOM node.
        if role == Role.TEXT_FIELD:
            backend_dom_nid = node.get("backendDOMNodeId")
            if backend_dom_nid is not None:
                try:
                    desc = self._send(
                        port, target_id, "DOM.describeNode",
                        {"backendNodeId": backend_dom_nid},
                    )
                    attrs = desc.get("node", {}).get("attributes", [])
                    for i in range(0, len(attrs) - 1, 2):
                        if attrs[i] == "type" and attrs[i + 1] == "password":
                            role = Role.PASSWORD_TEXT
                            break
                except Exception:
                    pass

        node_id = node.get("nodeId", "")

        # Position and size from box model.
        position, size = self._get_box_model(port, target_id, node_id)

        # Name.
        name = self._ax_name(node)

        # Value.
        value: str | None = None
        value_obj = node.get("value")
        if isinstance(value_obj, dict):
            v = value_obj.get("value")
            if v is not None:
                value = str(v)

        # Description.
        description: str | None = None
        desc_obj = node.get("description")
        if isinstance(desc_obj, dict):
            d = desc_obj.get("value")
            if d:
                description = str(d)

        # Actions — CDP doesn't have named actions like AT-SPI.
        # We infer clickability from the role.
        actions: list[str] = []
        if role in _ACTION_CLICKABLE_ROLES:
            actions.append("click")
        if role in _ACTION_EDITABLE_ROLES or State.EDITABLE in states:
            actions.append("set-text")
        if role in _ACTION_RANGE_ROLES:
            actions.append("set-value")

        # Raw — include CDP-specific data.
        raw: dict = {}
        props = node.get("properties")
        if props:
            raw["properties"] = props

        return Element(
            id=element_id,
            name=name,
            role=role,
            states=states,
            position=position,
            size=size,
            app=app_name,
            pid=pid,
            backend="cdp",
            raw_role=raw_role,
            actions=actions,
            value=value,
            description=description,
            parent_id=parent_id,
            window_id=window_id,
            raw=raw,
        )

    @staticmethod
    def _ax_name(node: dict) -> str:
        """Extract the accessible name from a CDP AXNode.

        Returns:
            The name string, or ``""`` if not available.
        """
        name_obj = node.get("name")
        if isinstance(name_obj, dict):
            v = name_obj.get("value")
            if v is not None:
                return str(v)
        return ""

    def _get_box_model(
        self,
        port: int,
        target_id: str,
        node_id: str,
    ) -> tuple[tuple[int, int], tuple[int, int]]:
        """Get position (center) and size for a CDP element.

        Uses ``DOM.getBoxModel`` via the backendDOMNodeId and
        converts CSS viewport coordinates to screen-absolute
        using ``Browser.getWindowForTarget``.

        Returns:
            ``(position, size)`` — both as ``(int, int)`` tuples.
            Returns ``(0, 0), (0, 0)`` on failure.
        """
        backend_nid = self._resolve_backend_node_id(
            port, target_id, node_id,
        )
        if backend_nid is None:
            return (0, 0), (0, 0)

        try:
            # Do NOT scroll here — this method is called for every
            # element during discovery and scrolling each one into
            # view causes excessive visual glitching.  Elements that
            # are offscreen will simply report (0,0) / (0,0).
            result = self._send(
                port, target_id,
                "DOM.getBoxModel",
                {"backendNodeId": backend_nid},
            )
            model = result.get("model", {})
            content = model.get("content", [])
            width = model.get("width", 0)
            height = model.get("height", 0)

            if content and len(content) >= 8:
                # content quad: [x1,y1, x2,y2, x3,y3, x4,y4].
                # Average all four points for correct centre even on
                # CSS-transformed (rotated/skewed) elements.
                off_x, off_y = self._get_screen_offset(port, target_id)
                cx = off_x + int((content[0]+content[2]+content[4]+content[6]) / 4)
                cy = off_y + int((content[1]+content[3]+content[5]+content[7]) / 4)

                # Convert CSS/logical coords to physical pixels.
                _s = get_scale_factor(cx, cy)
                return (
                    (round(cx * _s), round(cy * _s)),
                    (round(int(width) * _s), round(int(height) * _s)),
                )

            return (0, 0), (0, 0)
        except Exception:
            return (0, 0), (0, 0)

    def _resolve_backend_node_id(
        self,
        port: int,
        target_id: str,
        node_id: str,
    ) -> int | None:
        """Resolve an AXNode's nodeId to a backendDOMNodeId.

        For DOM-sourced elements (``d``-prefixed IDs), the value
        *is* the backendNodeId — return it directly.

        Checks the cache first, then fetches from CDP if missing.

        Returns:
            The ``backendDOMNodeId`` (int) or ``None``.
        """
        # DOM-sourced elements: stored coords are PAGE coords.
        # Scroll element into view atomically, get viewport coords,
        # then resolve to backendNodeId via DOM.getNodeForLocation.
        if node_id.startswith("dom:"):
            coords = node_id[4:]  # strip "dom:"
            try:
                css_x_s, css_y_s = coords.split(",", 1)
                css_x = int(css_x_s)
                css_y = int(css_y_s)
            except ValueError:
                return None
            try:
                vp_x, vp_y = self._scroll_to_vp(port, target_id, css_x, css_y)
                result = self._send(
                    port, target_id,
                    "DOM.getNodeForLocation",
                    {
                        "x": vp_x,
                        "y": vp_y,
                        "includeUserAgentShadowDOM": False,
                        "ignorePointerEventsNone": False,
                    },
                )
                return result.get("backendNodeId")
            except Exception:
                return None

        cache_key = (port, target_id)
        cache = self._dom_node_cache.get(cache_key, {})
        if node_id in cache:
            val = cache[node_id]
            return None if val == -1 else val

        # Fallback: fetch the full AX tree and search for the
        # matching nodeId to extract its backendDOMNodeId.
        try:
            all_nodes = self._fetch_ax_tree(
                port, target_id,
            )
            found: int | None = None
            sub_cache = self._dom_node_cache.setdefault(
                cache_key, {},
            )
            for ax_node in all_nodes:
                nid = ax_node.get("nodeId", "")
                if not nid:
                    continue
                backend_nid = ax_node.get("backendDOMNodeId")
                sub_cache[nid] = (
                    backend_nid if backend_nid is not None else -1
                )
                if nid == node_id and backend_nid is not None:
                    found = backend_nid
            if found is not None:
                return found
        except Exception:
            pass

        return None

    def _release_object(
        self, port: int, target_id: str, object_id: str,
    ) -> None:
        """Release a JS object reference (best-effort, ignore errors)."""
        try:
            self._send(
                port, target_id, "Runtime.releaseObject",
                {"objectId": object_id},
            )
        except Exception:
            pass

    def _click_element(
        self,
        port: int,
        target_id: str,
        backend_nid: int,
        element_id: str,
        action: str,
    ) -> bool:
        """Click a CDP element by dispatching mouse events.

        Resolves the element to a DOM node, then uses an atomic
        JavaScript call to scroll into view and read fresh viewport
        coordinates in one round-trip — avoiding stale coordinates
        from a fire-and-forget scroll followed by a separate box
        model query.
        """
        # Resolve backendNodeId → DOM objectId so we can call JS on it.
        try:
            resolve_result = self._send(
                port, target_id,
                "DOM.resolveNode",
                {"backendNodeId": backend_nid},
            )
            object_id = resolve_result.get("object", {}).get("objectId")
        except Exception:
            object_id = None

        if object_id:
            # Try to get coordinates via scrollIntoView + getBoundingClientRect.
            x = y = None
            expr = (
                "(function(){"
                "this.scrollIntoView({block:'center',inline:'center',behavior:'instant'});"
                "var r=this.getBoundingClientRect();"
                "return [r.x+r.width/2, r.y+r.height/2, r.width, r.height];"
                "})"
            )
            try:
                js_result = self._send(
                    port, target_id,
                    "Runtime.callFunctionOn",
                    {
                        "functionDeclaration": expr,
                        "objectId": object_id,
                        "returnByValue": True,
                    },
                )
                val = js_result.get("result", {}).get("value")
                if val and len(val) >= 4 and val[2] > 0 and val[3] > 0:
                    x = round(val[0])
                    y = round(val[1])
            except Exception:
                pass  # coordinates unavailable — try JS fallback below

            if x is None or y is None:
                # No coordinates (zero geometry, JS threw, or detached
                # element).  Fall back to JS event dispatch which works
                # even on elements with no layout.
                js_click_fn = {
                    "click": "function() { this.click(); }",
                    "double_click": (
                        "function() {"
                        "  this.dispatchEvent(new MouseEvent("
                        "    'dblclick', {bubbles:true, cancelable:true}));"
                        "}"
                    ),
                    "right_click": (
                        "function() {"
                        "  this.dispatchEvent(new MouseEvent("
                        "    'contextmenu', {bubbles:true, cancelable:true}));"
                        "}"
                    ),
                }.get(action)
                if js_click_fn is None:
                    self._release_object(port, target_id, object_id)
                    raise ActionFailedError(
                        action=action,
                        element_id=element_id,
                        reason="element has no visible geometry",
                    )
                try:
                    self._send(
                        port, target_id,
                        "Runtime.callFunctionOn",
                        {
                            "functionDeclaration": js_click_fn,
                            "objectId": object_id,
                        },
                    )
                    return True
                except ActionFailedError:
                    raise
                except Exception as js_exc:
                    raise ActionFailedError(
                        action=action,
                        element_id=element_id,
                        reason=f"element has no visible geometry "
                               f"and JS click also failed: {js_exc}",
                    ) from js_exc
                finally:
                    self._release_object(port, target_id, object_id)

            # Got coordinates — release JS ref, then dispatch mouse events.
            self._release_object(port, target_id, object_id)
        else:
            # Fallback: use the two-step approach if resolveNode fails
            # (e.g. for synthetic AX nodes with no DOM backing).
            try:
                self._send(
                    port, target_id,
                    "DOM.scrollIntoViewIfNeeded",
                    {"backendNodeId": backend_nid},
                )
            except Exception:
                pass

            try:
                result = self._send(
                    port, target_id,
                    "DOM.getBoxModel",
                    {"backendNodeId": backend_nid},
                )
            except Exception as exc:
                raise ActionFailedError(
                    action=action,
                    element_id=element_id,
                    reason=f"cannot get box model: {exc}",
                ) from exc

            model = result.get("model", {})
            content = model.get("content", [])
            if not content or len(content) < 8:
                # No geometry from getBoxModel either — try resolving
                # to a JS object for element.click() as last resort.
                try:
                    resolve2 = self._send(
                        port, target_id, "DOM.resolveNode",
                        {"backendNodeId": backend_nid},
                    )
                    oid2 = resolve2.get("object", {}).get("objectId")
                    if oid2:
                        js_fn = {
                            "click": "function() { this.click(); }",
                            "double_click": (
                                "function() {"
                                "  this.dispatchEvent(new MouseEvent("
                                "    'dblclick', {bubbles:true, cancelable:true}));"
                                "}"
                            ),
                            "right_click": (
                                "function() {"
                                "  this.dispatchEvent(new MouseEvent("
                                "    'contextmenu', {bubbles:true, cancelable:true}));"
                                "}"
                            ),
                        }.get(action)
                        if js_fn:
                            try:
                                self._send(
                                    port, target_id,
                                    "Runtime.callFunctionOn",
                                    {"functionDeclaration": js_fn, "objectId": oid2},
                                )
                                return True
                            finally:
                                self._release_object(port, target_id, oid2)
                        else:
                            self._release_object(port, target_id, oid2)
                except ActionFailedError:
                    raise
                except Exception as fallback_exc:
                    raise ActionFailedError(
                        action=action,
                        element_id=element_id,
                        reason=f"element has no visible geometry "
                               f"and JS click fallback failed: {fallback_exc}",
                    ) from fallback_exc
                raise ActionFailedError(
                    action=action,
                    element_id=element_id,
                    reason="element has no visible geometry",
                )
            x = round((content[0] + content[2] + content[4] + content[6]) / 4)
            y = round((content[1] + content[3] + content[5] + content[7]) / 4)

        button = "right" if action == "right_click" else "left"
        click_count = 2 if action == "double_click" else 1

        try:
            for etype, extra in [
                ("mouseMoved", {}),
                ("mousePressed", {"button": button, "clickCount": click_count}),
                ("mouseReleased", {"button": button, "clickCount": click_count}),
            ]:
                self._send(
                    port, target_id,
                    "Input.dispatchMouseEvent",
                    {"type": etype, "x": x, "y": y, **extra},
                )
            return True
        except Exception as exc:
            raise ActionFailedError(
                action=action,
                element_id=element_id,
                reason=f"mouse dispatch failed: {exc}",
            ) from exc

    def _dispatch_key(
        self,
        port: int,
        target_id: str,
        key: str,
        modifiers: int = 0,
    ) -> None:
        """Dispatch a key press+release via CDP Input domain.

        Args:
            key: A single character or a canonical key name.
            modifiers: Bitmask of modifier flags.
        """
        key_lower = key.lower()
        if key_lower in _KEY_MAP:
            key_val, code, key_code = _KEY_MAP[key_lower]
            text = ""
        elif len(key) == 1:
            key_val = key
            code = f"Key{key.upper()}" if key.isalpha() else ""
            key_code = ord(key.upper()) if key.isalpha() else ord(key)
            text = key
        else:
            key_val = key
            code = ""
            key_code = 0
            text = ""

        # Suppress text insertion when modifier keys are active
        # (e.g. Ctrl+A should not insert a literal "a").
        if modifiers:
            text = ""

        try:
            self._send(port, target_id, "Input.dispatchKeyEvent", {
                "type": "keyDown",
                "key": key_val,
                "code": code,
                "windowsVirtualKeyCode": key_code,
                "modifiers": modifiers,
                **({"text": text} if text else {}),
            })
            self._send(port, target_id, "Input.dispatchKeyEvent", {
                "type": "keyUp",
                "key": key_val,
                "code": code,
                "windowsVirtualKeyCode": key_code,
                "modifiers": modifiers,
            })
        except Exception as exc:
            raise RuntimeError(
                f"Key dispatch failed for {key!r}: {exc}"
            ) from exc

    def _scroll_to_vp(
        self, port: int, target_id: str, px: int, py: int,
    ) -> tuple[int, int]:
        """Scroll element into view if needed and return viewport coords.

        ``px``/``py`` are page-space (document) coordinates.  If the
        point is already inside the current viewport nothing is scrolled.
        Uses ``elementFromPoint`` + ``scrollIntoView`` to handle nested
        scroll containers (not just the window).
        Returns ``(vp_x, vp_y)`` — fresh viewport coordinates after any
        scroll, suitable for ``Input.dispatchMouseEvent``.
        """
        expr = (
            f"(function(px,py){{"
            # First try scrollIntoView via a temporary element lookup.
            # Create a probe at document coords, scroll into view,
            # then read back viewport coords.
            f"var vx=px-window.scrollX,vy=py-window.scrollY;"
            f"if(vx>=0&&vx<window.innerWidth&&vy>=0&&vy<window.innerHeight){{"
            f"return [vx,vy];"
            f"}}"
            # Out of viewport — scroll the window first.
            f"window.scrollTo({{top:Math.max(0,py-innerHeight/2),"
            f"left:Math.max(0,px-innerWidth/2),behavior:'instant'}});"
            f"vx=px-window.scrollX;vy=py-window.scrollY;"
            # Now try elementFromPoint to handle nested containers.
            f"var el=document.elementFromPoint(vx,vy);"
            f"if(el){{el.scrollIntoView({{block:'center',inline:'center',behavior:'instant'}});}}"
            f"vx=px-window.scrollX;vy=py-window.scrollY;"
            f"return [vx,vy];"
            f"}})({px},{py})"
        )
        try:
            sr = self._send(
                port, target_id,
                "Runtime.evaluate",
                {"expression": expr, "returnByValue": True},
            )
            sv = sr.get("result", {}).get("value")
            if sv and len(sv) >= 2:
                return int(sv[0]), int(sv[1])
            raise RuntimeError("scroll JS returned no value")
        except Exception as exc:
            raise ActionFailedError(
                action="scroll_to_viewport",
                element_id="",
                reason=f"cannot scroll element into view: {exc}",
            ) from exc

    @staticmethod
    def _role_to_cdp_string(role: Role) -> str | None:
        """Reverse-map a Touchpoint Role to a CDP role string.

        Returns the first matching CDP role string, or ``None``
        if no mapping exists.
        """
        return _CDP_REVERSE_ROLE_MAP.get(role)

    # -- Coordinate conversion / window geometry ----------------------------

    def _get_window_bounds(
        self,
        port: int,
        target_id: str,
    ) -> tuple[tuple[int, int], tuple[int, int], bool]:
        """Get window position, outer size, and focus state via JS.

        Uses ``Runtime.evaluate`` to read ``window.screenX``,
        ``window.screenY``, ``window.outerWidth``,
        ``window.outerHeight``, and ``document.hasFocus()``.
        These work universally across Chrome, Chromium, and all
        Electron-based applications (unlike
        ``Browser.getWindowForTarget`` which is Chrome-only).

        Returns:
            ``(position, size, is_active)`` where position and size
            are ``(int, int)`` tuples, and is_active is a boolean.
            Returns ``(0, 0), (0, 0), False`` on failure.
        """
        try:
            result = self._send(
                port, target_id, "Runtime.evaluate",
                {"expression": (
                    "JSON.stringify({"
                    "x:window.screenX,"
                    "y:window.screenY,"
                    "w:window.outerWidth,"
                    "h:window.outerHeight,"
                    "f:document.hasFocus()})"
                )},
            )
            data = json.loads(
                result.get("result", {}).get("value", "{}"),
            )
            wx = int(data.get("x", 0))
            wy = int(data.get("y", 0))
            ww = int(data.get("w", 0))
            wh = int(data.get("h", 0))

            # Convert CSS/logical window geometry to physical pixels.
            _s = get_scale_factor(wx, wy)
            return (
                (round(wx * _s), round(wy * _s)),
                (round(ww * _s), round(wh * _s)),
                bool(data.get("f", False)),
            )
        except Exception:
            return (0, 0), (0, 0), False

    def _get_screen_offset(
        self,
        port: int,
        target_id: str,
    ) -> tuple[int, int]:
        """Get ``(offset_x, offset_y)`` for converting CSS viewport
        coordinates to screen-absolute pixel coordinates.

        Uses ``Runtime.evaluate`` to read ``window.screenX`` /
        ``window.screenY`` + the browser chrome height (difference
        between outer height and viewport height).

        Results are cached per ``(port, targetId)`` for the duration
        of the current query.  Clear ``_viewport_cache`` at the start
        of each top-level call.
        """
        cache_key = (port, target_id)
        if cache_key in self._viewport_cache:
            return self._viewport_cache[cache_key]

        offset = (0, 0)
        try:
            result = self._send(
                port, target_id, "Runtime.evaluate",
                {"expression": (
                    "JSON.stringify({"
                    "x:window.screenX,"
                    "y:window.screenY,"
                    "oh:window.outerHeight,"
                    "ih:window.innerHeight})"
                )},
            )
            data = json.loads(
                result.get("result", {}).get("value", "{}"),
            )
            win_x = int(data.get("x", 0))
            win_y = int(data.get("y", 0))
            outer_h = int(data.get("oh", 0))
            inner_h = int(data.get("ih", 0))

            # Chrome height = outer - inner (title bar + tab bar +
            # address bar).  For Electron apps where outer==inner,
            # this is 0.
            chrome_h = max(0, outer_h - inner_h)
            offset = (win_x, win_y + chrome_h)
        except Exception:
            pass

        self._viewport_cache[cache_key] = offset
        return offset

    # -- Cross-origin iframe support --------------------------------------

    def _get_iframe_targets_for_page(
        self,
        port: int,
        page_target_id: str,
    ) -> list[tuple[str, str]]:
        """Find cross-origin iframe targets that belong to a page.

        Uses ``Page.getFrameTree`` on the parent to enumerate child
        frames, then matches their URLs against iframe targets from
        the target list.

        Returns:
            List of ``(iframe_target_id, frame_id)`` tuples.
        """
        # Gather all iframe targets for this port.
        all_targets = _list_targets(port)
        iframe_targets = [
            t for t in all_targets if t.get("type") == "iframe"
        ]
        if not iframe_targets:
            return []

        # Get the page's frame tree.
        try:
            result = self._send(
                port, page_target_id, "Page.getFrameTree", {},
            )
        except Exception:
            return []

        # Walk the frame tree to collect child frame URLs → frameIds.
        # Use a multimap because multiple iframes can share the same URL.
        child_frames: dict[str, list[str]] = {}  # url → [frameId, ...]

        def _walk(tree: dict) -> None:
            for child in tree.get("childFrames", []):
                frame = child.get("frame", {})
                url = frame.get("url", "")
                frame_id = frame.get("id", "")
                if url and frame_id:
                    child_frames.setdefault(url, []).append(frame_id)
                _walk(child)

        _walk(result.get("frameTree", {}))
        if not child_frames:
            return []

        # Match iframe targets by URL.
        matched: list[tuple[str, str]] = []
        for itarget in iframe_targets:
            iframe_url = itarget.get("url", "")
            iframe_tid = itarget.get("id", "")
            frame_ids = child_frames.get(iframe_url, [])
            if frame_ids:
                # Pop the first available frameId so each iframe
                # target gets its own unique frame mapping.
                matched.append((iframe_tid, frame_ids.pop(0)))

        return matched

    def _graft_iframe_tree(
        self,
        port: int,
        iframe_target_id: str,
        frame_id: str,
        parent_target_id: str,
        app_name: str,
        pid: int,
        tree_elements: list[Element],
        max_depth: int | None,
    ) -> None:
        """Graft a cross-origin iframe's AX subtree under the parent
        page's ``FRAME`` node.

        Uses ``DOM.getFrameOwner`` to find the iframe element in the
        parent's DOM, then resolves it to an AX node in the parent
        tree.  If matching fails, the iframe content is appended as
        additional root elements.
        """
        # Find the iframe element in the parent's DOM.
        frame_element: Element | None = None
        try:
            owner = self._send(
                port, parent_target_id,
                "DOM.getFrameOwner", {"frameId": frame_id},
            )
            owner_backend_nid = owner.get("backendNodeId")
            if owner_backend_nid is not None:
                cache_key = (port, parent_target_id)
                cache = self._dom_node_cache.get(cache_key, {})
                # Reverse lookup: backendDOMNodeId → nodeId.
                for nid, bnid in cache.items():
                    if bnid == owner_backend_nid:
                        frame_eid = (
                            f"cdp:{port}:{parent_target_id}:{nid}"
                        )
                        frame_element = self._find_element_in_tree(
                            tree_elements, frame_eid,
                        )
                        break
        except Exception:
            pass

        # Query the iframe target's AX tree.
        iframe_win_id = f"cdp:{port}:{iframe_target_id}"
        nodes = self._fetch_ax_tree(
            port, iframe_target_id, max_depth=max_depth,
        )
        if not nodes:
            return

        # Cache DOM node IDs for the iframe target.
        cache_key = (port, iframe_target_id)
        self._dom_node_cache[cache_key] = {}
        for node in nodes:
            nid = node.get("nodeId", "")
            if not nid:
                continue
            backend_nid = node.get("backendDOMNodeId")
            self._dom_node_cache[cache_key][nid] = (
                backend_nid if backend_nid is not None else -1
            )

        iframe_elements = self._build_tree(
            nodes, port, iframe_target_id, app_name, pid,
            iframe_win_id, max_depth, None,
        )

        if frame_element is not None:
            # Graft under the FRAME node.
            frame_element.children.extend(iframe_elements)
        else:
            # No matching FRAME node found — add as top-level roots.
            tree_elements.extend(iframe_elements)

    @staticmethod
    def _find_element_in_tree(
        elements: list[Element],
        element_id: str,
    ) -> Element | None:
        """Search for an element by ID in a tree (iterative)."""
        stack = list(elements)
        while stack:
            el = stack.pop()
            if el.id == element_id:
                return el
            stack.extend(el.children)
        return None

    # -- CDP screenshot ---------------------------------------------------

    def take_screenshot(
        self,
        port: int | None = None,
        target_id: str | None = None,
        element_id: str | None = None,
        padding: int = 0,
    ) -> bytes:
        """Capture a screenshot via CDP's ``Page.captureScreenshot``.

        Can capture the full viewport of a page target, or clip to
        a specific element's bounding box.

        Args:
            port: CDP port (required unless *element_id* is given).
            target_id: Target ID (required unless *element_id* is
                given).
            element_id: If specified, clip the screenshot to this
                element's bounds.  Overrides *port* / *target_id*.
            padding: Extra pixels around the element clip region.

        Returns:
            PNG image bytes.
        """
        import base64

        clip: dict[str, Any] | None = None

        if element_id is not None:
            parts = self._parse_id(element_id)
            port = parts["port"]
            target_id = parts["target_id"]
            node_id = parts["node_id"]

            backend_nid = self._resolve_backend_node_id(
                port, target_id, node_id,
            )
            if backend_nid is None:
                raise ValueError(
                    f"element {element_id!r} not found"
                )

            # Scroll into view so box model is valid.
            try:
                self._send(
                    port, target_id,
                    "DOM.scrollIntoViewIfNeeded",
                    {"backendNodeId": backend_nid},
                )
            except Exception:
                pass

            result = self._send(
                port, target_id, "DOM.getBoxModel",
                {"backendNodeId": backend_nid},
            )
            model = result.get("model", {})
            content = model.get("content", [])
            width = model.get("width", 0)
            height = model.get("height", 0)

            if (not content or len(content) < 2
                    or width <= 0 or height <= 0):
                raise ValueError(
                    "element has no visible geometry"
                )

            clip = {
                "x": max(0, content[0] - padding),
                "y": max(0, content[1] - padding),
                "width": width + padding * 2,
                "height": height + padding * 2,
                "scale": 1,
            }

        if port is None or target_id is None:
            raise ValueError(
                "Must specify port+target_id or element_id"
            )

        params: dict[str, Any] = {"format": "png"}
        if clip is not None:
            params["clip"] = clip

        result = self._send(
            port, target_id, "Page.captureScreenshot", params,
        )
        return base64.b64decode(result.get("data", ""))

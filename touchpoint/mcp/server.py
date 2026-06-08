"""Touchpoint MCP server — vision and no-vision modes.

Exposes the Touchpoint UI-automation API as MCP tools so LLM agents
(Claude Desktop, Cursor, Copilot, etc.) can observe and interact
with desktop applications.

Architecture
~~~~~~~~~~~~
- Each ``@_mcp_tool()`` function is a thin wrapper around the
  corresponding ``tp.*`` call.
- **Alias system** — Element and window IDs from backends can be
  long (e.g. ``atspi:101196:1:0.0.0.0.0.3.1.0.0.1``).  The MCP
  layer assigns short ephemeral aliases like ``atspi1``, ``cdp3``
  and translates them back on input.  The agent only ever sees
  the short form.
- Element-returning tools use a compact MCP-specific format that
  strips coordinates, verbose states, window IDs, and action lists
  to minimise token usage.
- Roles and states are accepted as **case-insensitive strings**.
- The ``touchpoint`` prompt provides an opinionated workflow for
  desktop automation.
- ``screenshot`` returns ``ImageContent`` (base64 PNG).
- Session-scoped alias, activation, and snapshot-baseline state is kept
  in memory for concise IDs and no-vision verification.

Run::

    touchpoint-mcp              # stdio (default)
    python -m touchpoint.mcp.server
"""

from __future__ import annotations

import io
import re
import json
import os
import sys
import threading
import time
from functools import wraps

from mcp.server.fastmcp import FastMCP, Image

import touchpoint as tp
from touchpoint import (
    Role, State,
    INTERACTIVE_ROLES, CONTAINER_ROLES, STRUCTURAL_ROLES,
)


def _parse_env_bool(value: str) -> bool:
    """Parse common truthy/falsey strings from environment variables."""
    v = value.strip().lower()
    if v in {"1", "true", "yes", "on"}:
        return True
    if v in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"invalid boolean value {value!r}")


def _configure_from_env() -> None:
    """Apply optional runtime config from ``TOUCHPOINT_*`` env vars.

    Supported variables:
    - ``TOUCHPOINT_CDP_PORTS``: JSON object mapping app -> port,
      e.g. ``{"Google Chrome": 9222}``.
    - ``TOUCHPOINT_CDP_APP`` + ``TOUCHPOINT_CDP_PORT``: convenience
      pair for a single app mapping.
    - ``TOUCHPOINT_CDP_DISCOVER``: bool (true/false).
    - ``TOUCHPOINT_CDP_REFRESH_INTERVAL``: float seconds.
    - ``TOUCHPOINT_SCALE_FACTOR``: float display scale (e.g. ``1.25``).
    - ``TOUCHPOINT_FUZZY_THRESHOLD``: float 0.0–1.0 (default 0.6).
    - ``TOUCHPOINT_FALLBACK_INPUT``: bool (true/false, default true).
    - ``TOUCHPOINT_MAX_ELEMENTS``: int (default 5000).
    - ``TOUCHPOINT_MAX_DEPTH``: int (default 20).
    - ``TOUCHPOINT_AX_MESSAGING_TIMEOUT``: float seconds (default 1.0).
    """
    cfg: dict[str, object] = {}

    raw_ports = os.environ.get("TOUCHPOINT_CDP_PORTS")
    if raw_ports:
        parsed = json.loads(raw_ports)
        if not isinstance(parsed, dict):
            raise ValueError("TOUCHPOINT_CDP_PORTS must be a JSON object")
        ports: dict[str, int] = {}
        for k, v in parsed.items():
            if not isinstance(k, str):
                raise ValueError("TOUCHPOINT_CDP_PORTS keys must be strings")
            if isinstance(v, bool) or not isinstance(v, int):
                raise ValueError("TOUCHPOINT_CDP_PORTS values must be integers")
            ports[k] = v
        cfg["cdp_ports"] = ports

    cdp_app = os.environ.get("TOUCHPOINT_CDP_APP")
    cdp_port = os.environ.get("TOUCHPOINT_CDP_PORT")
    if cdp_app and cdp_port:
        port = int(cdp_port)
        existing = cfg.get("cdp_ports")
        merged: dict[str, int] = dict(existing) if isinstance(existing, dict) else {}
        merged[cdp_app] = port
        cfg["cdp_ports"] = merged

    raw_discover = os.environ.get("TOUCHPOINT_CDP_DISCOVER")
    if raw_discover is not None:
        cfg["cdp_discover"] = _parse_env_bool(raw_discover)

    raw_refresh = os.environ.get("TOUCHPOINT_CDP_REFRESH_INTERVAL")
    if raw_refresh is not None:
        cfg["cdp_refresh_interval"] = float(raw_refresh)

    raw_scale = os.environ.get("TOUCHPOINT_SCALE_FACTOR")
    if raw_scale is not None:
        cfg["scale_factor"] = float(raw_scale)

    raw_fuzzy = os.environ.get("TOUCHPOINT_FUZZY_THRESHOLD")
    if raw_fuzzy is not None:
        cfg["fuzzy_threshold"] = float(raw_fuzzy)

    raw_fallback = os.environ.get("TOUCHPOINT_FALLBACK_INPUT")
    if raw_fallback is not None:
        cfg["fallback_input"] = _parse_env_bool(raw_fallback)

    raw_max_els = os.environ.get("TOUCHPOINT_MAX_ELEMENTS")
    if raw_max_els is not None:
        cfg["max_elements"] = int(raw_max_els)

    raw_max_depth = os.environ.get("TOUCHPOINT_MAX_DEPTH")
    if raw_max_depth is not None:
        cfg["max_depth"] = int(raw_max_depth)

    raw_ax_timeout = os.environ.get("TOUCHPOINT_AX_MESSAGING_TIMEOUT")
    if raw_ax_timeout is not None:
        cfg["ax_messaging_timeout"] = float(raw_ax_timeout)

    if cfg:
        tp.configure(**cfg)


try:
    _configure_from_env()
except Exception as exc:  # pragma: no cover - startup surface
    print(
        f"[touchpoint-mcp] warning: failed to apply env config: {exc}",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Mode selection
# ---------------------------------------------------------------------------
#
# The server has two modes:
#
# - "vision" (default) -- adds screenshot(), get_element(), mouse_move(),
#   move_window(), resize_window().  Use when the agent has visual context.
# - "no-vision"        -- adds structured snapshot() + diff_snapshot() with
#   automatic post-action verify flags.  Use when the agent has no visual
#   context (local models without image support, headless setups).
#
# Select with the TOUCHPOINT_MODE env var.

_VALID_MODES = ("vision", "no-vision")
_MODE = os.environ.get("TOUCHPOINT_MODE", "vision").strip().lower()
if _MODE not in _VALID_MODES:
    print(
        f"[touchpoint-mcp] warning: invalid TOUCHPOINT_MODE={_MODE!r}, "
        f"falling back to 'vision'",
        file=sys.stderr,
    )
    _MODE = "vision"


# ---------------------------------------------------------------------------
# Server instance
# ---------------------------------------------------------------------------

_role_values = ", ".join(r.value for r in Role)
_state_values = ", ".join(s.value for s in State)

# Shared across both instruction modes — keep the guidance general.
_CDP_SETUP_NOTE = (
    "Web and Electron page content is reachable only when the app was "
    "launched with remote debugging enabled (a debugging port; a separate "
    "user data dir is recommended). If a browser or Electron app exposes "
    "only its native window chrome and no page content, that is usually "
    "why -- relaunch it with remote debugging. diagnostics() reports "
    "whether any web-content targets are attached.\n"
)

_VISION_INSTRUCTIONS = (
        "You are an AI agent that controls a desktop computer through Touchpoint.\n"
        "You can see the screen, find UI elements, and interact with them.\n"
        "\n"
        "Always locate elements by ID using find() or snapshot() before acting.\n"
        "Do not estimate or guess screen coordinates from screenshots.\n"
        "\n"
        "== Workflow ==\n"
        "1. ORIENT  -- Take a screenshot to see the current screen state.\n"
        "2. LOCATE  -- Use find(query) to search for elements by name.\n"
        "             Use snapshot(app=...) when find() returns ambiguous\n"
        "             results or you need to understand container structure\n"
        "             (e.g. which dialog a button belongs to).  Both return\n"
        "             element IDs you will use to act.\n"
        "3. ACT     -- Call the appropriate action tool (click, set_value,\n"
        "             type_text, press_key, etc.) using the element ID.\n"
        "4. VERIFY  -- Take a screenshot to confirm the action had the\n"
        "             intended effect.  Check action flags first (see below)\n"
        "             -- they signal structural changes for free.\n"
        "\n"
        "Repeat this loop as needed.  When uncertain, observe first.\n"
        "\n"
        "== Action Flags ==\n"
        "Every action appends a flag when something notable happened at the\n"
        "window level.  Read it before deciding the next step.\n"
        "\n"
        "(new window: [alias] 'Title') -- dialog or popup appeared.\n"
        "(window closed: 'Title', ...) -- one or more windows were dismissed.\n"
        "(window title changed: 'New Title') -- in-app navigation occurred.\n"
        "No flag means no structural window event was detected -- the action\n"
        "likely applied normally.  Take a screenshot to confirm visual outcomes.\n"
        "\n"
        "== How find() Works ==\n"
        "find() matches text in 4 stages: exact -> contains -> word match -> fuzzy.\n"
        "It short-circuits: if an exact match exists, partial matches are skipped.\n"
        "\n"
        "- Use the FULL visible text of the element for precise matches.\n"
        '  "Send Message" finds the button exactly.  "Send" might match\n'
        "  many things (Send, Send Message, Sending...).\n"
        "- Do NOT invent longer or more creative queries -- the element\n"
        "  text is usually short and literal.\n"
        "- If find() returns nothing, loosen before changing strategy:\n"
        "  drop role/states filters, then simplify the query.  As a last\n"
        '  resort, use snapshot(app="...") to browse what is available,\n'
        "  or take a screenshot to check the UI state.\n"
        "- find(source=\"dom\") searches the live DOM -- useful when the\n"
        "  accessibility tree misses elements on a web page.\n"
        "\n"
        "== When to Use snapshot() ==\n"
        "snapshot() returns a structured tree of the active window with\n"
        "element IDs.  Use it when:\n"
        "- find() returns multiple elements with the same name and you\n"
        "  need container context to pick the right one.\n"
        "- You need a broad view of what is in a window without taking\n"
        "  a screenshot.\n"
        "- IDs went stale and you want to re-orient without re-screenshotting.\n"
        "snapshot(element_id=<id>) scopes the walk to a sub-tree -- useful\n"
        "for large apps or when the window tree is truncated.\n"
        "\n"
        "== Reading Body Text ==\n"
        "read_text(element_id) returns text exposed by that element's backend.\n"
        "Containers often expose aggregate text (article, document body,\n"
        "terminal).  Use it instead of\n"
        "OCR-reading a screenshot when you need exact prose content --\n"
        "it is faster, cheaper, and verbatim.\n"
        "Workflow: screenshot or snapshot() to identify the container ID,\n"
        "then read_text(container_id).\n"
        "\n"
        "== Scoping ==\n"
        "- ALWAYS scope to an app when possible:\n"
        '  find("Send", app="Slack") instead of find("Send").\n'
        "  Unscoped searches return elements from ALL apps -- noisy and slow.\n"
        "- If an app has multiple windows and you're only working with\n"
        "  one of them, filter your calls to that window_id.\n"
        "\n"
        "== Element IDs ==\n"
        "- IDs like 'cdp1', 'atspi3', 'uia5' are short session-scoped aliases.\n"
        "- They are valid until the UI changes significantly (navigation,\n"
        "  window close, app restart).\n"
        "- After major UI changes, re-run find() to get fresh IDs.\n"
        "- Each element also shows @(x,y) screen coordinates for spatial\n"
        "  disambiguation when multiple elements share the same name.\n"
        "- 'cdp' prefix = web/Electron content (browsers, Electron apps).\n"
        "- 'atspi' prefix = native Linux UI (AT-SPI2).\n"
        "- 'uia' prefix = native Windows UI (UI Automation).\n"
        "- 'ax' prefix = native macOS UI (Accessibility).\n"
        "\n"
        "== Browser & Electron apps ==\n"
        f"{_CDP_SETUP_NOTE}"
        "\n"
        "== Action Patterns ==\n"
        "- Prefer keyboard shortcuts over clicking when you know the\n"
        "  exact shortcut -- they are faster and more reliable.\n"
        "- Text input has two tools:\n"
        "  * type_text(text) -- sends raw keystrokes to whatever has focus.\n"
        "    Click/focus a field first.  Works with any text input.\n"
        "  * set_value(element_id, value) -- targets a specific element by ID.\n"
        "    Inserts at cursor by default.  Pass replace=True to clear the\n"
        "    field and replace all content.  Can sometimes be used on\n"
        "    dropdowns too.\n"
        "- If click() doesn't have the expected effect, use get_element()\n"
        "  to see available actions, then call action() with the exact name.\n"
        "- activate_window(window_id) brings a window to the front.\n"
        "  Call it before type_text or press_key when switching apps.\n"
        "\n"
        "== Waiting ==\n"
        "- After actions that trigger UI changes (navigation, loading, dialogs),\n"
        '  use wait_for(element="expected text") before acting on new content.\n'
        '- Use wait_for(element="Loading", gone=True) to wait for spinners/\n'
        "  loaders to disappear.\n"
        '- Use wait_for_app(app="AppName") after launching an application.\n'
        '- Use wait_for_window(title="Dialog") after triggering a new window.\n'
        "\n"
        "== Scrolling ==\n"
        "- mouse_move(element_id) positions the cursor on an element.\n"
        "  Use it before scroll() to scroll within a specific area.\n"
        "- scroll() scrolls at the current cursor position.\n"
        "\n"
        "== Screenshots ==\n"
        '- screenshot(app="...") crops to a specific app -- much cheaper\n'
        "  than a full-desktop screenshot.\n"
        "- Use screenshots for initial orientation, visual verification,\n"
        "  and confirming rendered output (charts, images, layout).\n"
        "- For reading body text, prefer read_text() over OCR from a screenshot.\n"
        "\n"
        "== Error Recovery ==\n"
        "- ActionFailedError on click/set_value/focus: the element cannot\n"
        "  perform this action (disabled, wrong type, or stale ID).  Try:\n"
        "  1. Re-run find() to get a fresh ID for the same element.\n"
        "  2. If re-find returns nothing, the UI changed -- take a screenshot\n"
        "     to see the current state.\n"
        "  3. Try a different approach (keyboard shortcut instead of click).\n"
        "- find() returns empty results: loosen filters, simplify the\n"
        "  query, or fall back to snapshot(app=...) to browse what is\n"
        "  available.  A screenshot can also help.\n"
        "- TimeoutError from wait_for: the expected element never appeared.\n"
        "  Take a screenshot to see what actually happened, then decide\n"
        "  whether to retry or take a different action.\n"
        "\n"
        "== Desktop, Taskbar, and System Tray ==\n"
        "Desktop icons and taskbar items belong to a shell application\n"
        "(plasmashell on KDE, gnome-shell on GNOME, Finder on macOS,\n"
        "explorer.exe on Windows).\n"
        "Scope snapshot() or find() to that app to reach them.\n"
        "Desktop icons often use non-interactive roles and may only appear\n"
        "with snapshot(all_elements=True).\n"
        "Double-click icons to launch: click(element_id, double_click=True).\n"
        "\n"
        "== Missing Elements ==\n"
        "Some toolkit elements are visually present but absent from the\n"
        "accessibility tree.  Before concluding an element is truly\n"
        "missing, loosen your search: drop role/states filters, simplify\n"
        "the query, broaden the scope.  Only after these fail, take a\n"
        "screenshot to confirm the element is actually on screen.\n"
        "\n"
        "If an element is confirmed visible but not in the tree:\n"
        "  1. Keyboard -- use arrow keys, Tab, Enter, Escape, or\n"
        "     keyboard shortcuts to reach the target without clicking.\n"
        "  2. Coordinates -- derive x,y from nearby elements' @(x,y)\n"
        "     or from a screenshot, then use click(x=, y=).\n"
        "  NEVER guess coordinates.  Always derive them from known\n"
        "  element positions or a screenshot.\n"
        "This is a last resort -- in most cases find() or snapshot()\n"
        "will return what you need.\n"
        "\n"
        "== Coordinate Mode ==\n"
        "click() and mouse_move() accept x,y screen coordinates as an\n"
        "alternative to element_id.  This is a LAST RESORT -- only use\n"
        "coordinates when element-ID-based actions do not work:\n"
        "  1. Clicking by ID triggers an unintended action (e.g. opens\n"
        "     a dropdown instead of focusing) -- use the element's own\n"
        "     @(x,y) shown in find() output.\n"
        "  2. An element is visible but not in the accessibility tree\n"
        "     (see Missing Elements above for the full escalation path).\n"
        "NEVER guess or estimate coordinates.  Always derive them from\n"
        "element positions shown in find() output or a screenshot.\n"
        "\n"
        "== Valid Roles ==\n"
        f"{_role_values}\n"
        "\n"
        "== Valid States ==\n"
        f"{_state_values}"
    )


_NO_VISION_INSTRUCTIONS = (
    "Touchpoint gives you structured access to desktop UI through "
    "accessibility APIs. You read UI trees, pick elements by ID, and "
    "act on them.\n"
    "\n"
    "== Core Workflow ==\n"
    "\n"
    "1. ORIENT. Call windows() to list open windows.\n"
    "2. VIEW. Call snapshot(window_id=...) to get an indented tree of "
    "that window's interactive elements and semantic containers. Read "
    "the indentation to understand structure. Example: a button "
    "indented under a dialog labelled \"Confirm Delete\" is that "
    "dialog's button.\n"
    "3. ACT. Call click(element_id), set_value(element_id, text), "
    "press_key(...), etc., using an element ID from snapshot() or "
    "find(). Element-scoped actions bring their window to focus "
    "automatically. type_text and press_key have no element target — "
    "they fire into whichever window was last activated. Call "
    "activate_window(window_id) before them whenever you are switching "
    "to a different window or have not yet activated any window.\n"
    "4. CHECK THE FLAG. Every action appends a categorical flag when "
    "something notable happened. Read it before deciding the next "
    "step.\n"
    "5. VERIFY. When you expect a specific UI change, call "
    "wait_for(element=...) instead of polling snapshot() in a loop.\n"
    "\n"
    "== Reading Auto-Verify Flags ==\n"
    "\n"
    "Multiple flags may appear together, separated by two spaces.\n"
    "\n"
    "(no change detected) — no accessibility-tree change was observable "
    "after a short retry delay. This can mean failure, but in-place UI "
    "updates and text entry are sometimes not reflected immediately. "
    "Re-snapshot or call diff_snapshot() before retrying; do not blindly "
    "repeat type_text(), because that can duplicate input.\n"
    "(new window: [alias] 'Title') — dialog or popup appeared. "
    "Pass the alias to snapshot(window_id=...) to see its "
    "contents, or call plain snapshot() if it auto-focused.\n"
    "(window closed: 'Title', ...) — one or more windows were dismissed. "
    "All IDs from those windows are stale. Call snapshot() to see current "
    "state.\n"
    "(window title changed: 'New Title') — in-app navigation "
    "happened. IDs from before the action are likely stale. "
    "Call snapshot() for fresh IDs.\n"
    "(focus moved) — keyboard focus shifted to a new element.\n"
    "No flag means no window-level or focus event occurred — "
    "the action likely took effect normally. For in-window state "
    "changes (a checkbox toggling, a value updating) no flag fires "
    "either; use diff_snapshot() if you need to confirm those.\n"
    "\n"
    "== Element IDs ==\n"
    "\n"
    "IDs like atspi3, uia12, cdp7 are short session aliases. They "
    "become stale after navigation, window close, or large UI "
    "rebuilds. If an action flags (no change detected) or errors, "
    "re-run snapshot() for fresh IDs.\n"
    "\n"
    "When two elements share the same name inside the same parent "
    "container, use the surrounding tree context (which dialog, which "
    "row, which menu) to pick the right ID.\n"
    "\n"
    "== Browser & Electron apps ==\n"
    f"{_CDP_SETUP_NOTE}"
    "\n"
    "== find() — shortcut for known names ==\n"
    "\n"
    "find() is for when you already know the element's name. It "
    "searches the full accessibility tree with fuzzy matching and "
    "returns ranked results with IDs. Use it to re-locate a named "
    "element after IDs go stale, or when the task names the target "
    "explicitly (e.g. \"click Submit\"). Do not use find() to "
    "explore or orient — snapshot() is the right tool for that.\n"
    "\n"
    "== When An Element Is Missing From snapshot() ==\n"
    "\n"
    "snapshot() filters to interactive and container elements by "
    "default. If your target is not visible:\n"
    "\n"
    "1. If the snapshot ended with a truncation notice, call "
    "snapshot(element_id=<container-id>) on the container closest to "
    "where your target should be. This sub-trees the walk from that "
    "element so you see its full contents.\n"
    "2. Call find(query, app=...) if you know the element's name. "
    "Example: find(\"Send\", app=\"Slack\"). find() searches the full "
    "accessibility tree, including elements snapshot filtered out.\n"
    "3. Call snapshot(all_elements=True) to disable the filter and "
    "see every named element.\n"
    "4. Fall back to keyboard navigation: press_key(\"Tab\") to move "
    "focus, press_key(\"Enter\") to act. Some elements are only "
    "reachable by keyboard.\n"
    "\n"
    "== Menus ==\n"
    "\n"
    "Menu containers show as a single header line when closed. Their "
    "items are not shown — clicking opens the menu. After a menu opens "
    "you will see (new window: ...) or the menu items will appear as "
    "children in a fresh snapshot(). Always re-snapshot after opening "
    "a menu to see its current contents.\n"
    "\n"
    "== action() — Native Accessibility Actions ==\n"
    "\n"
    "Some interactive elements expose non-standard native actions in "
    "the snapshot as actions=[...]. For example: "
    "[atspi5] tree_item 'Documents' actions=[expand, collapse]. "
    "These actions cannot be triggered by click() — use "
    "action(element_id, action_name) instead.\n"
    "Only call action() when: (a) the element shows actions=[...] in "
    "the snapshot, AND (b) click() either failed silently "
    "(no change detected) or is not the right verb for the action. "
    "Do not guess action names — only use names listed in the snapshot.\n"
    "\n"
    "== Desktop, Taskbar, and System Tray ==\n"
    "\n"
    "Desktop icons and taskbar items belong to a shell application "
    "(plasmashell on KDE, gnome-shell on GNOME, Finder on macOS, "
    "explorer.exe on Windows). "
    "They are not the focused window, so default snapshot() will not "
    "show them. To interact with them: call apps() to find the shell "
    "app name, then snapshot(app=\"plasmashell\") or equivalent. "
    "Desktop icons often use non-interactive roles and may only appear "
    "with snapshot(all_elements=True). Use double_click=True on icons "
    "to launch them.\n"
    "\n"
    "== Scoping ==\n"
    "\n"
    "Always scope find() and snapshot() to an app or window when you "
    "know the target. snapshot(app=\"Firefox\") and find(\"Submit\", "
    "app=\"Firefox\") are faster and less noisy than unscoped calls. "
    "Use snapshot(element_id=<id>) to scope to a sub-tree within a "
    "window — useful for large apps or when snapshot is truncated.\n"
    "When switching between apps mid-task, make sure to act on an "
    "element in the target window first — this brings it to focus "
    "automatically.\n"
    "\n"
    "== Diffing ==\n"
    "\n"
    "diff_snapshot() returns what changed since your last snapshot: "
    "added, removed, and state-changed elements. Use it when a flag "
    "says something changed and you need details, or to confirm a "
    "subtle state flip like a checkbox.\n"
    "\n"
    "== Input and Reading Tools ==\n"
    "\n"
    "read_text(element_id) is the only way to read text content. "
    "find() matches element names — button labels, link text, field "
    "labels — NOT prose body text inside articles, paragraphs, or "
    "documents.  snapshot() shows structure and element names only, "
    "not content.\n"
    "Workflow for reading body text: snapshot() → identify the "
    "section, article, or container that holds the text → call "
    "read_text(that_element_id) to get its full prose content.  "
    "Do not use find() to search for prose content.\n"
    "set_value(element_id, text, replace=True) clears the field "
    "first. Use replace=False (default) to insert at the current "
    "cursor position. Can sometimes be used on dropdowns too.\n"
    "set_numeric_value is for sliders and spinners, not text fields.\n"
    "select_text(element_id, text, occurrence=N) selects the Nth "
    "match of a substring in any text-bearing element, including "
    "read-only web content and document bodies.\n"
    "type_text(text) types into whatever currently has focus. Call "
    "focus(element_id) first if unsure. Pass raw=True when literal "
    "backslash escape text such as \\\\n must remain unchanged.\n"
    "press_key accepts a single key \"Enter\" or a chord "
    "[\"ctrl\",\"shift\",\"t\"].\n"
    "scroll(direction, amount=3, element_id=None) scrolls at that "
    "element if given, else at the cursor.\n"
    "\n"
    "== Valid Roles ==\n"
    f"{_role_values}\n"
    "\n"
    "== Valid States ==\n"
    f"{_state_values}"
)


_INSTRUCTIONS = (
    _VISION_INSTRUCTIONS if _MODE == "vision" else _NO_VISION_INSTRUCTIONS
)

mcp = FastMCP("Touchpoint", instructions=_INSTRUCTIONS)
_tool_lock = threading.RLock()


def _serialized_tool(fn):
    """Serialize one complete MCP tool workflow and its session state."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        with _tool_lock:
            return fn(*args, **kwargs)
    return wrapper


def _mcp_tool():
    """Register a tool with MCP after adding request serialization."""
    def decorator(fn):
        return mcp.tool()(_serialized_tool(fn))
    return decorator


def _tool_if(modes: set[str]):
    """Register a tool only when the current mode is in *modes*.

    Tools gated out of the current mode are not registered with the MCP
    server, so the agent never sees them in its tool list.  Used on the
    few tools that differ between vision and no-vision modes; everything
    that exists in both modes keeps the plain ``@_mcp_tool()`` decorator.
    """
    def decorator(fn):
        if _MODE in modes:
            return mcp.tool()(_serialized_tool(fn))
        return fn
    return decorator


# ---------------------------------------------------------------------------
# Alias system — short ephemeral IDs for MCP
# ---------------------------------------------------------------------------

_alias_to_real: dict[str, str] = {}
_real_to_alias: dict[str, str] = {}
_alias_to_window: dict[str, str] = {}  # element alias → real window ID
_last_activated_window: str | None = None  # real window ID last brought to front
_counters: dict[str, int] = {}

# Pattern to detect backend prefix from real IDs.
_BACKEND_PREFIX_RE = re.compile(r"^(atspi|cdp|uia|ax|dom)(?=:)")


def _alias(real_id: str, window_id: str | None = None) -> str:
    """Return (or create) a short alias for a real backend ID.

    ``"cdp:9223:93C006A6A1B4D97B2DB98110132BC9F2:813"`` -> ``"cdp1"``.
    Already-aliased IDs are returned unchanged.

    When *window_id* is supplied the mapping is stored in
    ``_alias_to_window`` so action tools can auto-activate the
    containing window before acting.
    """
    if real_id in _real_to_alias:
        short = _real_to_alias[real_id]
        if window_id:
            _alias_to_window[short] = window_id
        return short

    m = _BACKEND_PREFIX_RE.match(real_id)
    prefix = m.group(1) if m else "e"

    _counters[prefix] = _counters.get(prefix, 0) + 1
    short = f"{prefix}{_counters[prefix]}"

    _alias_to_real[short] = real_id
    _real_to_alias[real_id] = short
    if window_id:
        _alias_to_window[short] = window_id
    return short


def _resolve(alias_or_real: str) -> str:
    """Translate a short alias back to the real backend ID.

    Passes through unknown strings unchanged (backward-compat with
    raw IDs).
    """
    return _alias_to_real.get(alias_or_real, alias_or_real)


def _do_activate(window_real_id: str) -> bool:
    """Activate a window by its real ID.

    Updates ``_last_activated_window`` only when activation succeeds.
    Returns ``True`` on success, ``False`` otherwise.
    """
    global _last_activated_window
    try:
        ok = tp.activate_window(window_real_id)
        if ok:
            _last_activated_window = window_real_id
            return True
        print(
            f"[touchpoint-mcp] auto-activate returned False for {window_real_id!r}",
            file=sys.stderr,
        )
        return False
    except Exception:
        print(
            f"[touchpoint-mcp] auto-activate failed for {window_real_id!r}",
            file=sys.stderr,
        )
        return False


def _auto_activate_element(element_id: str) -> None:
    """Activate the window containing *element_id* before an element action."""
    window_real_id = _alias_to_window.get(element_id)
    if window_real_id is None:
        try:
            el = tp.get_element(_resolve(element_id))
            if isinstance(el, tp.Element) and el.window_id:
                window_real_id = el.window_id
                if element_id in _alias_to_real:
                    _alias_to_window[element_id] = window_real_id
        except Exception:
            pass
    if window_real_id:
        _do_activate(window_real_id)


def _auto_activate_last() -> None:
    """Re-activate the last known window before a focus-dependent action.

    Used by element-less tools (type_text, press_key) that send input
    to whatever has OS focus.  If no window has been activated in this
    session this is a no-op.
    """
    if _last_activated_window:
        _do_activate(_last_activated_window)


# ---------------------------------------------------------------------------
# Snapshot state (no-vision mode only)
# ---------------------------------------------------------------------------
#
# Captured on every snapshot() and action tool call in no-vision mode.
# Used for auto-verify flags on actions ("new window", "focus moved",
# "no change detected") and for diff_snapshot() output.
#
# In vision mode these stay empty — the ``_capture_state()`` helper
# returns ``None`` so the overhead is skipped entirely.

_snap_window_ids: set[str] = set()
_snap_window_titles: dict[str, str] = {}       # real window id -> title
_snap_focused_window: str | None = None        # real window id
_snap_focused_element: str | None = None       # real element id
# Per-element summary snapshot used for diff_snapshot().
# Value: (name, role_value, states_tuple, value_or_none, position_xy)
_snap_elements: dict[str, tuple[str, str, tuple[str, ...], str | None, tuple[int, int]]] = {}

# Containers that become opaque (header-only, no children) when they
# appear nested inside another container of the same kind.  Used by
# _snapshot_walk's inside_menu logic to suppress sub-menus while still
# showing top-level menu items.  Add other roles here if new "nested =
# closed" patterns are identified on other backends.
_NESTED_OPAQUE_CONTAINERS: frozenset[Role] = frozenset({Role.MENU})


def _element_summary(
    el: tp.Element,
) -> tuple[str, str, tuple[str, ...], str | None, tuple[int, int]]:
    """Reduce an Element to the immutable tuple we diff against."""
    return (
        el.name,
        el.role.value,
        tuple(sorted(s.value for s in el.states)),
        el.value,
        el.position,
    )


def _capture_state(window_id: str | None = None) -> dict:
    """Capture a lightweight snapshot of UI state for diff / auto-verify.

    When ``window_id`` is given (e.g. from ``snapshot()``), the element
    list is scoped to that window so ``diff_snapshot()`` compares against
    the right baseline.  Without it, falls back to the currently active
    window, which is the right choice for action-tool auto-verify.

    Returns window-level state in both modes.  In no-vision mode also
    captures element-level state for the ``(no change detected)`` diff.
    """
    try:
        wins = tp.windows()
    except Exception:
        wins = []
    window_ids = {w.id for w in wins}
    window_titles = {w.id: w.title for w in wins}
    focused_win = next((w for w in wins if w.is_active), None)
    focused_win_id = focused_win.id if focused_win is not None else None

    # Use the caller-supplied window when available (snapshot target),
    # otherwise fall back to the active window (action auto-verify).
    target_win_id = window_id if window_id is not None else focused_win_id

    # Element-level capture is only needed in no-vision mode for the
    # (no change detected) diff.  Vision mode verifies with screenshot.
    elements_map: dict[str, tuple[str, str, tuple[str, ...], str | None, tuple[int, int]]] = {}
    focused_el_id: str | None = None
    if _MODE == "no-vision" and target_win_id is not None:
        try:
            els = tp.elements(
                window_id=target_win_id,
                named_only=True,
                max_elements=tp.configure()["max_elements"],
            )
            if isinstance(els, list):
                for el in els:
                    elements_map[el.id] = _element_summary(el)
                    if State.FOCUSED in el.states and focused_el_id is None:
                        focused_el_id = el.id
        except Exception:
            pass

    return {
        "window_ids": window_ids,
        "window_titles": window_titles,
        "focused_window": focused_win_id,
        "focused_element": focused_el_id,
        "elements": elements_map,
    }


def _apply_state(state: dict) -> None:
    """Store a captured state as the new baseline."""
    global _snap_focused_window, _snap_focused_element
    _snap_window_ids.clear()
    _snap_window_ids.update(state["window_ids"])
    _snap_window_titles.clear()
    _snap_window_titles.update(state["window_titles"])
    _snap_focused_window = state["focused_window"]
    _snap_focused_element = state["focused_element"]
    _snap_elements.clear()
    _snap_elements.update(state["elements"])


def _diff_flags(before: dict, after: dict) -> str:
    """Build the auto-verify suffix for an action response.

    All applicable flags are reported, joined by two spaces.  The only
    exclusive flag is ``(no change detected)`` (no-vision only) — it
    cannot coexist with any other event.  ``(focus moved)`` is suppressed
    when a structural window event already implies a focus shift.

    Possible flags (all may appear together except where noted):
      ``(new window: [id] 'title')``     — dialog / popup appeared
      ``(window closed: 'title', ...)``  — one or more windows dismissed
      ``(window title changed: 'title')``— in-app navigation
      ``(focus moved)``                  — keyboard focus shifted (no-vision only)
      ``(no change detected)``           — silent fail (no-vision only, exclusive)
      ``""``                             — normal change, nothing notable
    """

    flags: list[str] = []

    # New windows.
    new_window_ids = after["window_ids"] - before["window_ids"]
    if new_window_ids:
        parts = [
            f"[{_alias(wid)}] {after['window_titles'].get(wid, '')!r}"
            for wid in sorted(new_window_ids)
        ]
        flags.append(f"(new window: {', '.join(parts)})")

    # Closed windows — report all of them, with the previously focused
    # one first because it is usually the most relevant context loss.
    closed_window_ids = before["window_ids"] - after["window_ids"]
    if closed_window_ids:
        focused_was = before.get("focused_window")
        ordered_ids = sorted(
            closed_window_ids,
            key=lambda wid: (
                wid != focused_was,
                before["window_titles"].get(wid, ""),
                wid,
            ),
        )
        parts = [repr(before["window_titles"].get(wid, "")) for wid in ordered_ids]
        flags.append(f"(window closed: {', '.join(parts)})")

    # Title of the focused window changed → in-app navigation.
    focused_wid = before.get("focused_window")
    if (
        focused_wid
        and focused_wid in before["window_ids"]
        and focused_wid in after["window_ids"]
    ):
        b_title = before["window_titles"].get(focused_wid, "")
        a_title = after["window_titles"].get(focused_wid, "")
        if b_title != a_title:
            flags.append(f"(window title changed: {a_title!r})")

    # Nothing at all changed → silent fail signal (no-vision only).
    # Vision agents verify with screenshot; the flag has known false
    # positives on Linux/AT-SPI (widget/a11y divergence) that a
    # screenshot immediately resolves.
    if not flags and _MODE == "no-vision":
        unchanged = (
            after["window_ids"] == before["window_ids"]
            and after["window_titles"] == before["window_titles"]
            and after["focused_window"] == before["focused_window"]
            and after["focused_element"] == before["focused_element"]
            and set(after["elements"].keys()) == set(before["elements"].keys())
            and all(
                after["elements"][k] == before["elements"][k]
                for k in after["elements"]
            )
        )
        if unchanged:
            return "(no change detected)"

    # Focus moved — only add when no structural window event already
    # implies it (closing / opening a window always shifts focus).
    if not flags and after["focused_element"] != before["focused_element"]:
        flags.append("(focus moved)")

    return "  ".join(flags)


def _verify_wrap(before: dict, result: str) -> str:
    """Capture after-state, update baseline, append flag suffix.

    If the first capture shows no change, waits 200 ms and retries
    once.  Many UI frameworks register accessibility events slightly
    after an action returns (dialogs, focus shifts, title changes) so
    a single immediate capture can miss them.
    """
    after = _capture_state()
    flag = _diff_flags(before, after)
    if _MODE == "no-vision" and flag == "(no change detected)":
        time.sleep(0.2)
        after = _capture_state()
        flag = _diff_flags(before, after)
    _apply_state(after)
    if flag:
        return f"{result}  {flag}"
    return result


# ---------------------------------------------------------------------------
# Compact MCP element formatter
# ---------------------------------------------------------------------------

# States that are interesting enough to show.  Most elements are
# visible+enabled+sensitive -- showing those is noise.
_INTERESTING_STATES: frozenset[State] = frozenset({
    State.FOCUSED,
    State.CHECKED,
    State.SELECTED,
    State.EXPANDED,
    State.COLLAPSED,
    State.BUSY,
    State.READ_ONLY,
    State.REQUIRED,
    State.INVALID,
    State.PRESSED,
    State.MODAL,
    State.INDETERMINATE,
    State.OFFSCREEN,
    State.HAS_POPUP,   # clicking opens a menu/dropdown, not a direct action
    State.DEFUNCT,     # stale element — act on it will fail
    State.EDITABLE,    # text field that accepts input (some backends use this over READ_ONLY)
})

# Native action names that are already covered by the standard click() /
# right_click() MCP tools.  Actions in this set are suppressed from
# snapshot output — showing them adds noise without giving the agent any
# new capability.  Covers all four backends (AT-SPI, UIA, AX, CDP).
_STANDARD_ACTION_NAMES: frozenset[str] = frozenset({
    # AT-SPI click aliases
    "click", "toggle", "press", "activate", "doDefault",
    # AT-SPI right-click aliases
    "ShowMenu", "show_menu",
    # UIA click aliases
    "invoke", "select",
    # AX click / right-click aliases
    "AXPress", "AXShowMenu",
    # CDP (same string as the tool verb)
    "double_click", "right_click",
})


def _mcp_format_element(el: tp.Element) -> str:
    """Format a single element for MCP output.

    Compact one-liner: ``[cdp1] button 'Close' @(512,340) app=Discord``

    Omits window ID, action list, and noise states.
    """
    short_id = _alias(el.id, el.window_id)
    x, y = el.position
    parts = [
        f"[{short_id}]",
        el.role.value,
        repr(el.name),
        f"@({x},{y})",
        f"app={el.app}",
    ]

    # Interesting states only.
    interesting = [s.value for s in el.states if s in _INTERESTING_STATES]
    if interesting:
        parts.append(",".join(interesting))

    if el.value is not None:
        parts.append(f"value={el.value!r}")

    return " ".join(parts)


def _mcp_format_element_detail(el: tp.Element) -> str:
    """Format a single element with full detail for ``get_element``.

    Like ``_mcp_format_element`` but appends actions and description
    so the agent can discover raw action names for use with the
    ``action()`` tool.
    """
    base = _mcp_format_element(el)
    extras: list[str] = []
    if el.actions:
        extras.append(f"actions=[{', '.join(el.actions)}]")
    if el.description:
        extras.append(f"description={el.description!r}")
    if extras:
        return base + " " + " ".join(extras)
    return base


def _mcp_format_elements(elements: list[tp.Element]) -> str:
    """Format a list of elements for MCP output."""
    if not elements:
        return "No elements found."
    return "\n".join(_mcp_format_element(el) for el in elements)



def _snapshot_line(
    el: tp.Element,
    include_position: bool = False,
) -> str:
    """Compact one-line render of an element for snapshot output.

    Similar to :func:`_mcp_format_element` but omits ``@(x,y)`` and
    ``app=`` by default — the surrounding tree structure supplies
    context.  ``include_position=True`` is used on siblings whose
    names collide within the same parent.
    """
    short_id = _alias(el.id, el.window_id)
    parts = [f"[{short_id}]", el.role.value, repr(el.name)]
    interesting = [s.value for s in el.states if s in _INTERESTING_STATES]
    if interesting:
        parts.append(",".join(interesting))
    if el.value is not None:
        parts.append(f"value={el.value!r}")
    if include_position:
        x, y = el.position
        parts.append(f"@({x},{y})")
    # Show non-standard actions — ones not already covered by click() /
    # right_click().  These are the only cases where action() adds value.
    extra_actions = [a for a in el.actions if a not in _STANDARD_ACTION_NAMES]
    if extra_actions:
        parts.append(f"actions=[{', '.join(extra_actions)}]")
    return " ".join(parts)


def _snapshot_container_line(el: tp.Element) -> str:
    """Header line for a semantic container.

    Renders as e.g. ``dialog "Confirm Delete" modal [atspi3]``.
    Named containers get an alias so the agent can pass it to
    ``snapshot(element_id=...)`` for sub-tree scoping.
    Value is shown for containers where it carries meaning
    (e.g. ``progress_bar "Loading" value='42%'``).
    """
    parts = [el.role.value]
    if el.name and el.name.strip():
        parts.append(f'"{el.name}"')
    interesting = [s.value for s in el.states if s in _INTERESTING_STATES]
    if interesting:
        parts.append(",".join(interesting))
    # Only show value when it carries distinct information — skip when it
    # duplicates the name (e.g. AT-SPI/AX headings where both fields hold
    # the same text content).
    if el.value is not None and el.value != el.name:
        parts.append(f"value={el.value!r}")
    if el.name and el.name.strip():
        parts.append(f"[{_alias(el.id, el.window_id)}]")
    return " ".join(parts)


def _snapshot_walk(
    els: list[tp.Element],
    depth: int,
    out: list[str],
    all_elements: bool,
    inside_menu: bool = False,
) -> None:
    """Recursive render pass over a tree of Elements.

    Classification:
      - CONTAINER_ROLES → emit a header line, recurse children one
        indent deeper.
      - INTERACTIVE_ROLES → emit a leaf line, recurse children one
        indent deeper (menus / rows may have interactive descendants).
      - STRUCTURAL_ROLES → skip the line, recurse children at the
        SAME indent (reparent to kept ancestor).
      - Anything else → skip unless ``all_elements=True`` or the
        element has a name AND has no kept descendants (leaf text).

    ``inside_menu`` is True when the current element list is the
    direct children of a MENU container.  Any nested MENU encountered
    while inside_menu=True is a closed sub-menu and is rendered as a
    header-only line (no children).  This lets top-level menu contents
    be visible while preventing sub-menu explosion across all backends:

    - Qt:     menu_bar → menu_item → MENU (popup).  The EXPANDED check
              on menu_item skips its children when the submenu is closed,
              so the MENU child is never visited as a CONTAINER_ROLES node
              in the normal case.  When the menu IS open (EXPANDED set),
              the MENU is visited with inside_menu=False, then its items
              are walked with inside_menu=True.
    - GTK/LO: menu_bar → MENU 'File' → MENU 'New' (sub) → menu_items.
              The outer MENU sets inside_menu=True; the inner MENU is
              then rendered as a header without recursing.
    - CDP:    A MENU that appears in the tree is open (closed menus are
              aria-hidden and never returned).  Its menu_items are shown
              at inside_menu=True; any nested MENU stays opaque.

    Same-parent name collisions are annotated with a position suffix
    in a second pass (see :func:`_snapshot_render`).
    """
    # Pre-compute which siblings collide by (role, name) at this level.
    collision_keys: set[tuple[str, str]] = set()
    name_counts: dict[tuple[str, str], int] = {}
    for el in els:
        if el.role in INTERACTIVE_ROLES and el.name:
            key = (el.role.value, el.name)
            name_counts[key] = name_counts.get(key, 0) + 1
    for key, count in name_counts.items():
        if count > 1:
            collision_keys.add(key)

    indent = "  " * depth
    for el in els:
        role = el.role
        if role in CONTAINER_ROLES:
            out.append(f"{indent}{_snapshot_container_line(el)}")
            if el.children:
                if role in _NESTED_OPAQUE_CONTAINERS and inside_menu:
                    pass  # nested container = closed sub-menu — header only
                else:
                    _snapshot_walk(
                        el.children, depth + 1, out, all_elements,
                        inside_menu=(role in _NESTED_OPAQUE_CONTAINERS),
                    )
        elif role in INTERACTIVE_ROLES:
            include_pos = (el.role.value, el.name) in collision_keys
            out.append(
                f"{indent}{_snapshot_line(el, include_position=include_pos)}"
            )
            if el.children:
                # menu_item with a MENU child = a sub-menu trigger.
                # Only recurse when the item is EXPANDED (open).
                # This handles Qt's menu_item → MENU → menu_item chain.
                _SUBMENU_ROLES = (
                    Role.MENU_ITEM,
                    Role.CHECK_MENU_ITEM,
                    Role.RADIO_MENU_ITEM,
                )
                has_menu_child = any(
                    c.role == Role.MENU for c in el.children
                )
                if (
                    role in _SUBMENU_ROLES
                    and has_menu_child
                    and State.EXPANDED not in el.states
                ):
                    pass  # closed submenu — skip children
                else:
                    _snapshot_walk(
                        el.children, depth + 1, out, all_elements,
                        inside_menu=inside_menu,
                    )
        elif role in STRUCTURAL_ROLES:
            # Named structural nodes (e.g. a fieldset "Shipping Address")
            # provide essential context — render as a container header and
            # recurse one level deeper so children stay visually nested.
            # Anonymous wrappers (no name) are transparent: skip the node
            # and reparent children to the current indent level.
            if el.name and el.name.strip():
                out.append(f"{indent}{_snapshot_container_line(el)}")
                if el.children:
                    _snapshot_walk(
                        el.children, depth + 1, out, all_elements,
                        inside_menu=inside_menu,
                    )
            elif el.children:
                _snapshot_walk(el.children, depth, out, all_elements, inside_menu=inside_menu)
        else:
            # Content / unknown role.  Only emit in all_elements mode.
            # The named-leaf fallback was removed because it produced
            # enormous noise: individual character/word text nodes from
            # web pages, alt-text blobs, and decorative copy that add
            # nothing an agent can act on.
            if all_elements and el.name and el.name.strip():
                out.append(
                    f"{indent}{_snapshot_line(el, include_position=False)}"
                )
            if el.children:
                _snapshot_walk(el.children, depth, out, all_elements)


def _count_tree(els: list[tp.Element]) -> int:
    """Count all nodes in a tree (roots + all descendants)."""
    total = 0
    for el in els:
        total += 1
        if el.children:
            total += _count_tree(el.children)
    return total


def _snapshot_render(
    roots: list[tp.Element],
    all_elements: bool = False,
    effective_max_depth: int | None = None,
) -> str:
    """Render a tree of Elements as a snapshot string."""
    lines: list[str] = []
    _snapshot_walk(roots, 0, lines, all_elements)
    if not lines:
        return "(no matching elements)"

    def _tree_depth(els: list[tp.Element], d: int = 0) -> int:
        if not els:
            return d
        return max(_tree_depth(el.children, d + 1) for el in els)

    notices: list[str] = []
    cfg = tp.configure()

    # Element cap hit — backend walk hit max_elements before finishing.
    # _count_tree is the raw backend count (includes anonymous wrappers
    # the renderer filters out), so it does not match the displayed line
    # count.  Don't show the number; just signal that content was cut.
    element_count = _count_tree(roots)
    if element_count >= cfg["max_elements"]:
        notices.append(
            "(snapshot truncated — call snapshot(element_id=<id>) on a "
            "specific container to scope the walk and see its contents)"
        )

    # Depth cap hit — deeper levels of the tree are not shown.
    used_depth = effective_max_depth if effective_max_depth is not None else cfg["max_depth"]
    if _tree_depth(roots) > used_depth:
        notices.append(
            "(snapshot depth-limited — call snapshot(element_id=<id>) on "
            "the deepest container shown to walk further into it)"
        )

    if notices:
        lines.extend(notices)
    return "\n".join(lines)


def _mcp_format_window(w: tp.Window) -> str:
    """Format a single window for MCP output with aliased ID."""
    short_id = _alias(w.id)
    parts = [f"[{short_id}]", repr(w.title), f"({w.size[0]}x{w.size[1]})", f"app={w.app}"]
    if w.is_active:
        parts.append("active")
    return " ".join(parts)


def _mcp_format_windows(windows: list[tp.Window]) -> str:
    """Format a list of windows for MCP output."""
    if not windows:
        return "No windows found."
    return "\n".join(_mcp_format_window(w) for w in windows)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_role(value: str | None) -> Role | None:
    """Convert a case-insensitive string to a Role enum, or None."""
    if value is None:
        return None
    key = value.strip().upper()
    try:
        return Role[key]
    except KeyError:
        # Try matching by value (lowercase).
        v = value.strip().lower()
        for member in Role:
            if member.value == v:
                return member
        raise ValueError(
            f"Unknown role {value!r}. Valid roles: "
            f"{', '.join(r.value for r in Role)}"
        )


def _parse_states(values: list[str] | None) -> list[State] | None:
    """Convert case-insensitive strings to State enums, or None."""
    if not values:
        return None
    result: list[State] = []
    for v in values:
        key = v.strip().upper()
        try:
            result.append(State[key])
        except KeyError:
            low = v.strip().lower()
            found = False
            for member in State:
                if member.value == low:
                    result.append(member)
                    found = True
                    break
            if not found:
                raise ValueError(
                    f"Unknown state {v!r}. Valid states: "
                    f"{', '.join(s.value for s in State)}"
                )
    return result


def _ok(action: str, success: bool) -> str:
    """Standard action result text."""
    if success:
        return f"{action}: OK"
    return f"{action}: failed"


def _err(exc: Exception) -> str:
    """Format an exception as a clean error string."""
    return f"Error: {type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Tools -- Discovery
# ---------------------------------------------------------------------------


@_mcp_tool()
def apps() -> str:
    """List applications with accessible UI elements.

    Returns application names visible in the accessibility tree.
    Use these names to scope other tools (find, elements, screenshot).
    """
    try:
        result = tp.apps()
        if not result:
            return "No applications found."
        return "\n".join(result)
    except Exception as exc:
        return _err(exc)


@_mcp_tool()
def diagnostics(probe: bool = True) -> str:
    """Report backend, input, CDP, and optional dependency health.

    Args:
        probe: If true (default), initialise optional providers and perform
            discovery. Pass false for a side-effect-free state snapshot.
    """
    try:
        return json.dumps(tp.diagnostics(probe=probe), indent=2, sort_keys=True)
    except Exception as exc:
        return _err(exc)


@_mcp_tool()
def windows() -> str:
    """List all open windows.

    Returns window IDs, titles, sizes, and app names.
    Use window IDs to scope find/elements queries or activate_window.
    """
    try:
        result = tp.windows()
        return _mcp_format_windows(result)
    except Exception as exc:
        return _err(exc)


# ---------------------------------------------------------------------------
# Tools -- Element retrieval
# ---------------------------------------------------------------------------


@_mcp_tool()
def find(
    query: str,
    app: str | None = None,
    window_id: str | None = None,
    role: str | None = None,
    states: list[str] | None = None,
    max_results: int | None = None,
    fields: list[str] | None = None,
    source: str = "full",
) -> str:
    """Search for UI elements by name — buttons, links, labels, fields.

    Matches against element names (button labels, link text, field
    labels), NOT body text or prose content inside articles or
    documents.

    Returns element IDs that you can use with click, set_value, etc.
    Use the FULL visible text for best results (e.g. "Send Message"
    not just "Send").

    Args:
        query: Text to search for (e.g. "Send Message", "Submit", "Search").
        app: Scope to this application (e.g. "Firefox", "Slack").
        window_id: Scope to this window.
        role: Only match this role (e.g. "button", "text_field", "link").
        states: Only match elements with ALL these states (e.g. ["enabled", "visible"]).
        max_results: Maximum matches to return.
        fields: Which fields to search -- ["name"], ["name", "value"], or ["name", "value", "description"].
        source: "full" (default, merged native+web), "cdp_ax" (CDP accessibility tree only), "native" (platform only), or "dom" (live DOM). "ax" remains as a compatibility alias for "cdp_ax".
    """
    try:
        results = tp.find(
            query,
            app=app,
            window_id=_resolve(window_id) if window_id else None,
            role=_parse_role(role),
            states=_parse_states(states),
            max_results=max_results,
            fields=fields,
            source=source,
        )
        if isinstance(results, str):
            return results
        if not results:
            return "No elements found."
        return _mcp_format_elements(results)
    except Exception as exc:
        return _err(exc)



@_tool_if({"vision"})
def get_element(element_id: str) -> str:
    """Get a single element by its ID with full detail.

    Returns a fresh snapshot with current states, value, supported
    actions, and description.  Use this to inspect an element
    before calling the ``action()`` tool — the actions list shows
    exactly which raw action names are available.

    Args:
        element_id: The element ID (from find/elements results).
    """
    try:
        result = tp.get_element(_resolve(element_id))
        if result is None:
            return f"Element {element_id!r} not found."
        return _mcp_format_element_detail(result)
    except Exception as exc:
        return _err(exc)


# ---------------------------------------------------------------------------
# Tools -- Snapshot (no-vision mode)
# ---------------------------------------------------------------------------


def _resolve_snapshot_window(
    app: str | None,
    window_id: str | None,
) -> tp.Window | None:
    """Pick the window to snapshot based on app / window_id / active.

    Precedence:
      1. ``window_id`` — explicit, resolved through the alias map.
      2. ``app`` — first active window for that app; falls back to
         the first visible window.
      3. Currently active window across all apps.
    """
    wins = tp.windows()
    if not wins:
        return None
    if window_id is not None:
        real = _resolve(window_id)
        for w in wins:
            if w.id == real:
                return w
        return None
    if app is not None:
        app_lower = app.lower()
        app_wins = [w for w in wins if w.app.lower() == app_lower]
        if not app_wins:
            return None
        active = next((w for w in app_wins if w.is_active), None)
        return active or app_wins[0]
    active = next((w for w in wins if w.is_active), None)
    return active or wins[0]


@_mcp_tool()
def snapshot(
    app: str | None = None,
    window_id: str | None = None,
    element_id: str | None = None,
    all_elements: bool = False,
    max_depth: int | None = None,
) -> str:
    """Return a tree-structured view of a window's UI.

    The primary orient tool in no-vision mode.  In vision mode, use it
    as a structural complement to screenshot() — cheaper than a
    screenshot and gives element IDs directly.  Default behaviour: pick
    the currently active window, walk its accessibility tree, prune
    anonymous structural wrappers, preserve semantic containers (dialogs,
    menus, lists, etc.), and emit an indented text view with one line per
    interactive element.

    Args:
        app: Snapshot the given app's active window (or first window).
            Case-insensitive.
        window_id: Snapshot a specific window by ID.
        element_id: Start the tree walk from a specific element instead
            of the window root.  Use to dig into a container whose
            children were not visible in a previous (truncated) snapshot.
            To read the text content of a container, use read_text()
            rather than snapshot().
        all_elements: If true, include every named element — not just
            interactive + container roles.  Use when the default
            filter is hiding something.
        max_depth: Maximum tree depth to walk.  Defaults to the
            configured value (typically 20).  Decrease for a faster
            overview of a large window.
    """
    try:
        # When element_id is the primary target (no app/window_id given),
        # skip window resolution entirely.  Using the active window as a
        # fallback would pass a mismatched window_id to tp.elements(),
        # causing wrong backend routing (e.g. AT-SPI window + CDP element).
        element_id_only = element_id is not None and app is None and window_id is None

        if element_id_only:
            target = None
        else:
            target = _resolve_snapshot_window(app, window_id)
            if target is None:
                return "No matching window found."

        header_parts: list[str] = []
        if target is not None:
            header_parts = [f"window: {target.title!r}", f"[{_alias(target.id)}]"]
            if target.app:
                header_parts.append(f"app={target.app}")

        # Walk the full tree; the renderer decides what to keep.
        # named_only at the backend level would drop legitimately
        # unnamed containers (menu_bar, toolbar, list, etc.) whose
        # role is the context we care about.
        real_element_id = _resolve(element_id) if element_id is not None else None
        els = tp.elements(
            window_id=target.id if target is not None else None,
            root_element=real_element_id,
            tree=True,
            named_only=False,
            max_depth=max_depth,
        )
        if not isinstance(els, list):
            els = []

        body = _snapshot_render(els, all_elements=all_elements, effective_max_depth=max_depth)

        # Refresh baseline scoped to the window we just snapshotted.
        # A subtree-only snapshot has no resolved target window, so look
        # up the root element and use its owning window instead of silently
        # falling back to whichever unrelated window is currently active.
        baseline_window_id = target.id if target is not None else None
        if baseline_window_id is None and real_element_id is not None:
            try:
                root = tp.get_element(real_element_id)
            except Exception:
                root = None
            if isinstance(root, tp.Element):
                baseline_window_id = root.window_id
        _apply_state(_capture_state(window_id=baseline_window_id))

        header = " ".join(header_parts) if header_parts else f"element: [{element_id}]"
        return header + "\n" + body
    except Exception as exc:
        return _err(exc)


@_tool_if({"no-vision"})
def diff_snapshot(verbose: bool = False) -> str:
    """Report what changed since the last snapshot or action.

    Compares the current accessibility tree of the focused window
    against the state captured at the most recent ``snapshot()`` or
    action call, and returns a short list of additions, removals, and
    state changes.

    Args:
        verbose: If true, also report name and position changes.
            Default: states and value changes only.
    """
    try:
        before = {
            "window_ids": set(_snap_window_ids),
            "window_titles": dict(_snap_window_titles),
            "focused_window": _snap_focused_window,
            "focused_element": _snap_focused_element,
            "elements": dict(_snap_elements),
        }
        after = _capture_state()
        lines: list[str] = []

        # Window-level changes.
        added_wins = after["window_ids"] - before["window_ids"]
        removed_wins = before["window_ids"] - after["window_ids"]
        for wid in added_wins:
            title = after["window_titles"].get(wid, "")
            lines.append(f"+window {title!r}")
        for wid in removed_wins:
            title = before["window_titles"].get(wid, "")
            lines.append(f"-window {title!r}")

        if (
            before["focused_window"] is not None
            and after["focused_window"] is not None
            and before["focused_window"] != after["focused_window"]
        ):
            b_title = before["window_titles"].get(before["focused_window"], "")
            a_title = after["window_titles"].get(after["focused_window"], "")
            lines.append(
                f"focused window changed: {b_title!r} -> {a_title!r} "
                "(element IDs from earlier snapshots are stale)"
            )
            _apply_state(after)
            return "\n".join(lines) if lines else "(no changes)"

        # Element-level changes within the focused window.
        before_els = before["elements"]
        after_els = after["elements"]
        added = set(after_els) - set(before_els)
        removed = set(before_els) - set(after_els)
        common = set(before_els) & set(after_els)

        for real_id in added:
            name, role, states, val, _pos = after_els[real_id]
            alias = _alias(real_id)
            lines.append(f"+[{alias}] {role} {name!r}")

        for real_id in removed:
            name, role, states, val, _pos = before_els[real_id]
            alias = _alias(real_id)
            lines.append(f"-[{alias}] {role} {name!r}")

        for real_id in common:
            b = before_els[real_id]
            a = after_els[real_id]
            if b == a:
                continue
            b_name, b_role, b_states, b_val, b_pos = b
            a_name, a_role, a_states, a_val, a_pos = a
            changes: list[str] = []
            if b_states != a_states:
                gained = set(a_states) - set(b_states)
                lost = set(b_states) - set(a_states)
                if gained:
                    changes.append(f"+states={sorted(gained)}")
                if lost:
                    changes.append(f"-states={sorted(lost)}")
            if b_val != a_val:
                changes.append(f"value: {b_val!r} -> {a_val!r}")
            if verbose:
                if b_name != a_name:
                    changes.append(f"name: {b_name!r} -> {a_name!r}")
                if b_pos != a_pos:
                    changes.append(f"pos: {b_pos} -> {a_pos}")
            if not changes:
                continue
            alias = _alias(real_id)
            lines.append(
                f"~[{alias}] {a_role} {a_name!r} " + "; ".join(changes)
            )

        _apply_state(after)
        return "\n".join(lines) if lines else "(no changes)"
    except Exception as exc:
        return _err(exc)


# ---------------------------------------------------------------------------
# Tools -- Screenshot
# ---------------------------------------------------------------------------


@_tool_if({"vision"})
def screenshot(
    app: str | None = None,
    window_id: str | None = None,
    element_id: str | None = None,
    padding: int = 0,
    monitor: int | None = None,
) -> Image:
    """Capture the screen and return an image.

    With no arguments, captures the full desktop.  Specify one
    parameter to crop to a specific target.

    Args:
        app: Crop to this application's window.
        window_id: Crop to this specific window.
        element_id: Crop to this element's bounding box.
        padding: Extra pixels around the crop region.
        monitor: Capture only this monitor (0-indexed).
    """
    try:
        element_arg: tp.Element | str | None = (
            _resolve(element_id) if element_id else None
        )
        img = tp.screenshot(
            app=app,
            window_id=_resolve(window_id) if window_id else None,
            element=element_arg,
            padding=padding,
            monitor=monitor,
        )
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return Image(data=buf.getvalue(), format="png")
    except Exception as exc:
        return _err(exc)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Tools -- Element actions
# ---------------------------------------------------------------------------


@_tool_if({"vision"})
def click(
    element_id: str | None = None,
    x: int | None = None,
    y: int | None = None,
    button: str = "left",
    double_click: bool = False,
) -> str:
    """Click an element by ID, or at screen coordinates.

    Pass ``element_id`` to click via the element's native
    accessibility action (most reliable).  Pass ``x`` and ``y``
    to click directly at screen coordinates instead — useful
    when clicking by ID triggers an unintended action (e.g.
    opens a dropdown instead of focusing a text entry).
    Every element shows its position as @(x,y) in listings.
    Coordinate clicks always report OK even if nothing was hit —
    verify the result with a screenshot or find().

    Args:
        element_id: The element ID to click.
        x: Screen X coordinate (use with y instead of element_id).
        y: Screen Y coordinate (use with x instead of element_id).
        button: "left" (default) or "right".
        double_click: If true, perform a double-click instead.
            Cannot be combined with button="right".
    """
    try:
        if double_click and button == "right":
            return "Error: double right-click is not supported."
        if x is not None and y is not None:
            # Coordinate click — no element context, no auto-activate.
            before = _capture_state()
            if double_click:
                tp.double_click_at(x, y)
                return _verify_wrap(before, f"double_click_at({x}, {y}): OK")
            elif button == "right":
                tp.right_click_at(x, y)
                return _verify_wrap(before, f"right_click_at({x}, {y}): OK")
            else:
                tp.click_at(x, y)
                return _verify_wrap(before, f"click_at({x}, {y}): OK")
        if element_id is None:
            return "Error: provide element_id or both x and y."
        _auto_activate_element(element_id)
        before = _capture_state()
        real_id = _resolve(element_id)
        if double_click:
            return _verify_wrap(before, _ok("double_click", tp.double_click(real_id)))
        elif button == "right":
            return _verify_wrap(before, _ok("right_click", tp.right_click(real_id)))
        else:
            return _verify_wrap(before, _ok("click", tp.click(real_id)))
    except Exception as exc:
        return _err(exc)


@_tool_if({"no-vision"})
def click(  # noqa: F811 - two mode-gated tools intentionally share a name
    element_id: str,
    button: str = "left",
    double_click: bool = False,
) -> str:
    """Click an element by its ID.

    Args:
        element_id: The element ID to click.
        button: "left" (default) or "right".
        double_click: If true, perform a double-click instead.
            Cannot be combined with button="right".
    """
    try:
        if double_click and button == "right":
            return "Error: double right-click is not supported."
        _auto_activate_element(element_id)
        before = _capture_state()
        real_id = _resolve(element_id)
        if double_click:
            result = _ok("double_click", tp.double_click(real_id))
        elif button == "right":
            result = _ok("right_click", tp.right_click(real_id))
        else:
            result = _ok("click", tp.click(real_id))
        return _verify_wrap(before, result)
    except Exception as exc:
        return _err(exc)


@_mcp_tool()
def set_value(element_id: str, value: str, replace: bool = False) -> str:
    """Set the value of an editable element.

    Calls the platform's native value-setting API.  Behaviour depends
    on the target widget:
      * Text fields / text areas: writes the text.
      * Combo boxes / dropdowns / <select>: picks the option whose
        label or value matches *value*.  Prefer this over clicking
        the dropdown and then clicking a popup item — picking by name
        is one atomic call and avoids popup-click failures on some
        toolkits.
      * Other editable widgets: whatever their value interface accepts.

    Args:
        element_id: The element ID (a text field, combo box, etc.).
        value: The text or option label to set.
        replace: If true, clear the field first and replace all content.
                 If false (default), insert at the current cursor position.
    """
    try:
        _auto_activate_element(element_id)
        before = _capture_state()
        real_id = _resolve(element_id)
        ok = tp.set_value(real_id, value, replace=replace)
        result = _ok("set_value", ok)
        if ok and _MODE == "no-vision":
            try:
                actual = tp.get_text_content(real_id)
                if actual is not None:
                    if value in actual:
                        result += f"  value: {actual!r}"
                    else:
                        result += f"  value mismatch (got {actual!r}) — verify with snapshot() that the change applied"
            except Exception:
                pass
        return _verify_wrap(before, result)
    except Exception as exc:
        return _err(exc)


@_mcp_tool()
def set_numeric_value(element_id: str, value: float) -> str:
    """Set the numeric value of a range element (slider, spinbox).

    Args:
        element_id: The element ID (a slider, spin button, etc.).
        value: The numeric value to set.
    """
    try:
        _auto_activate_element(element_id)
        before = _capture_state()
        real_id = _resolve(element_id)
        ok = tp.set_numeric_value(real_id, value)
        result = _ok("set_numeric_value", ok)
        if ok and _MODE == "no-vision":
            try:
                actual = tp.get_text_content(real_id)
                if actual is not None:
                    result += f"  value: {actual!r}"
            except Exception:
                pass
        return _verify_wrap(before, result)
    except Exception as exc:
        return _err(exc)


@_mcp_tool()
def focus(element_id: str) -> str:
    """Move keyboard focus to an element.

    Args:
        element_id: The element ID to focus.
    """
    try:
        _auto_activate_element(element_id)
        before = _capture_state()
        result = _ok("focus", tp.focus(_resolve(element_id)))
        return _verify_wrap(before, result)
    except Exception as exc:
        return _err(exc)


@_mcp_tool()
def select_text(element_id: str, text: str, occurrence: int = 1) -> str:
    """Select a substring within an element's text content.

    Finds the text within the element and applies a native text
    selection over that range.  Works on editable fields and document
    bodies on all backends.  On web content (CDP) and Windows (UIA)
    also works on read-only containers such as articles and sections.
    Useful for formatting, copying, or replacing specific text.

    Args:
        element_id: The element ID containing the text.
        text: The exact substring to select.
        occurrence: Which occurrence to select (1 = first, 2 = second, etc.).
    """
    try:
        _auto_activate_element(element_id)
        before = _capture_state()
        result = _ok(
            "select_text",
            tp.select_text(_resolve(element_id), text, occurrence=occurrence),
        )
        return _verify_wrap(before, result)
    except Exception as exc:
        return _err(exc)


@_mcp_tool()
def action(element_id: str, action_name: str) -> str:
    """Perform a raw accessibility action by exact name.

    Only call this when the element shows actions=[...] in snapshot()
    output AND click() either failed silently or is not the right verb.
    Use the exact action name shown in the snapshot — do not guess.
    Example: an element showing actions=[expand, collapse] can be acted
    on with action(element_id, "expand").

    Args:
        element_id: The element ID.
        action_name: Exact action name from the snapshot actions=[...] list.
    """
    try:
        _auto_activate_element(element_id)
        before = _capture_state()
        result = _ok(
            f"action({action_name!r})",
            tp.action(_resolve(element_id), action_name),
        )
        return _verify_wrap(before, result)
    except Exception as exc:
        return _err(exc)


@_mcp_tool()
def read_text(element_id: str) -> str:
    """Return text content exposed by an element or container.

    Some backends expose aggregate descendant text for containers.
    Pass a container ID from snapshot() to read an entire section,
    article, dialog, or document body when the backend supports it.

    Workflow: snapshot() to find the right container, read_text(id)
    to read its full contents.

    Args:
        element_id: The element or container ID to read text from.
    """
    try:
        content = tp.get_text_content(_resolve(element_id))
        if content is None:
            return f"Error: element {element_id!r} does not expose text content via the accessibility API."
        return content
    except Exception as exc:
        return _err(exc)



# ---------------------------------------------------------------------------
# Tools -- Keyboard input
# ---------------------------------------------------------------------------


@_mcp_tool()
def type_text(text: str, raw: bool = False) -> str:
    """Type text into the currently focused element.

    Simulates keyboard input.  Focus a text field first with
    click() or focus(), then type into it.

    Special characters:
      \\n = Enter (line break),  \\t = Tab (next field),
      \\b = Backspace (delete previous character).

    Args:
        text: The text to type.
        raw: If true, type literal backslashes without converting
            ``\\n``, ``\\t``, or ``\\b`` escape sequences.
    """
    try:
        _auto_activate_last()
        before = _capture_state()
        # Normalise literal escape sequences from MCP/JSON callers
        # to real characters so the public API handles them.
        if not raw:
            text = text.replace("\\n", "\n").replace("\\t", "\t").replace("\\b", "\b")
        tp.type_text(text)
        return _verify_wrap(before, "type_text: OK")
    except Exception as exc:
        return _err(exc)


@_mcp_tool()
def press_key(keys: str | list[str], repeat: int = 1) -> str:
    """Press a key or key combination.

    Single key: "enter", "tab", "escape", "f5", "backspace".
    Combination: ["ctrl", "s"], ["ctrl", "shift", "p"], ["alt", "f4"].

    Args:
        keys: A single key name, or a list of keys for a combination
              (all held together, then released in reverse order).
        repeat: Number of times to press (default 1).
    """
    try:
        _auto_activate_last()
        before = _capture_state()
        for _ in range(repeat):
            if isinstance(keys, list):
                tp.hotkey(*keys)
            else:
                tp.press_key(keys)
        if isinstance(keys, list):
            label = f"hotkey({', '.join(keys)})"
        else:
            label = f"press_key({keys})"
        if repeat > 1:
            label += f" x{repeat}"
        return _verify_wrap(before, f"{label}: OK")
    except Exception as exc:
        return _err(exc)


# ---------------------------------------------------------------------------
# Tools -- Mouse / scroll
# ---------------------------------------------------------------------------


@_tool_if({"vision"})
def mouse_move(
    element_id: str | None = None,
    x: int | None = None,
    y: int | None = None,
) -> str:
    """Move the mouse cursor to an element or to screen coordinates.

    Use this before scroll() to scroll within a specific area.

    Args:
        element_id: The element ID to move the cursor to.
        x: Screen X coordinate (use with y instead of element_id).
        y: Screen Y coordinate (use with x instead of element_id).
    """
    try:
        if x is not None and y is not None:
            tp.mouse_move(x, y)
            return f"mouse_move: OK -- cursor at ({x}, {y})"
        if element_id is None:
            return "Error: provide element_id or both x and y."
        _auto_activate_element(element_id)
        el = tp.get_element(_resolve(element_id))
        if el is None:
            return f"Error: element {element_id!r} not found."
        tp.mouse_move(*el.position)
        return f"mouse_move: OK -- cursor at {el.position}"
    except Exception as exc:
        return _err(exc)


@_mcp_tool()
def scroll(
    direction: str,
    amount: int = 3,
    element_id: str | None = None,
) -> str:
    """Scroll by mouse wheel ticks.

    With ``element_id``, moves the cursor to that element's position
    first, then scrolls — useful for scrolling inside a specific list,
    panel, or container.  Without it, scrolls at the current cursor
    position.

    Args:
        direction: One of "up", "down", "left", "right".
        amount: Number of scroll ticks (default 3).
        element_id: Optional element to scroll at.  When given, cursor
            is moved to the element's center first.
    """
    try:
        el = None
        if element_id is not None:
            el = tp.get_element(_resolve(element_id))
            if el is None:
                return f"Error: element {element_id!r} not found."
            _auto_activate_element(element_id)
        else:
            _auto_activate_last()
        before = _capture_state()
        if el is not None:
            tp.mouse_move(*el.position)
        tp.scroll(direction=direction, amount=amount)
        target = f" at {element_id}" if element_id is not None else ""
        return _verify_wrap(
            before, f"scroll({direction}, {amount}){target}: OK"
        )
    except Exception as exc:
        return _err(exc)


# ---------------------------------------------------------------------------
# Tools -- Window actions
# ---------------------------------------------------------------------------


@_mcp_tool()
def activate_window(window_id: str) -> str:
    """Bring a window to the foreground.

    Element-scoped actions (click, set_value, select_text, etc.) bring
    their window to focus automatically — you rarely need this.  Use it
    when you need to type_text or press_key in a window without first
    clicking an element in it (e.g. sending a keyboard shortcut to a
    window you haven't interacted with yet in this task).

    Args:
        window_id: The window ID to activate (from windows()).
    """
    try:
        before = _capture_state()
        ok = _do_activate(_resolve(window_id))
        result = _ok("activate_window", ok)
        return _verify_wrap(before, result)
    except Exception as exc:
        return _err(exc)


@_mcp_tool()
def minimize_window(window_id: str) -> str:
    """Minimize a window to the dock/taskbar.

    Use windows() to find the window ID first.
    To restore a minimized window, use activate_window().

    Args:
        window_id: The window ID to minimize.
    """
    try:
        return _ok(
            "minimize_window",
            tp.minimize_window(_resolve(window_id)),
        )
    except Exception as exc:
        return _err(exc)


@_mcp_tool()
def fullscreen_window(window_id: str, fullscreen: bool = True) -> str:
    """Enter or exit fullscreen for a window.

    Use windows() to find the window ID first.

    Args:
        window_id: The window ID.
        fullscreen: True to enter fullscreen (default), False to exit.
    """
    try:
        return _ok(
            "fullscreen_window",
            tp.fullscreen_window(_resolve(window_id), fullscreen=fullscreen),
        )
    except Exception as exc:
        return _err(exc)


@_mcp_tool()
def close_window(window_id: str) -> str:
    """Close a window.

    Use windows() to find the window ID first.

    Args:
        window_id: The window ID to close.
    """
    try:
        return _ok("close_window", tp.close_window(_resolve(window_id)))
    except Exception as exc:
        return _err(exc)


@_tool_if({"vision"})
def move_window(window_id: str, x: int, y: int) -> str:
    """Move a window to a new screen position.

    Use windows() to find the window ID first.

    Args:
        window_id: The window ID to move.
        x: New horizontal position of the top-left corner.
        y: New vertical position of the top-left corner.
    """
    try:
        return _ok(
            "move_window",
            tp.move_window(_resolve(window_id), x, y),
        )
    except Exception as exc:
        return _err(exc)


@_tool_if({"vision"})
def resize_window(window_id: str, width: int, height: int) -> str:
    """Resize a window.

    Use windows() to find the window ID first.

    Args:
        window_id: The window ID to resize.
        width: New width in pixels.
        height: New height in pixels.
    """
    try:
        return _ok(
            "resize_window",
            tp.resize_window(_resolve(window_id), width, height),
        )
    except Exception as exc:
        return _err(exc)


# ---------------------------------------------------------------------------
# Tools -- Waiting
# ---------------------------------------------------------------------------


@_mcp_tool()
def wait_for(
    element: str | list[str],
    app: str | None = None,
    window_id: str | None = None,
    role: str | None = None,
    states: list[str] | None = None,
    fields: list[str] | None = None,
    mode: str = "any",
    timeout: float = 10.0,
    source: str = "full",
    max_results: int = 5,
    wait_for_new: bool = False,
    gone: bool = False,
) -> str:
    """Wait for elements to appear or disappear.

    Polls until matching elements are found (or gone) or timeout.
    Use after actions that trigger UI changes.

    Args:
        element: Text to search for.  Pass a single string (e.g.
            "Submit") or a list of strings (e.g. ["Success", "Error"])
            for multi-query mode.  With mode="any", returns as soon
            as any query matches.  With mode="all", waits until every
            query has matched.
        app: Scope to this application.
        window_id: Scope to this window.
        role: Only match this role.
        states: Only match elements with ALL these states.
        fields: Which fields to search (default: ["name"]).
        mode: "any" (return when any query matches) or "all"
            (wait for all queries to match).  Only meaningful when
            element is a list.
        timeout: Maximum seconds to wait (default 10).
        source: "full" (default), "cdp_ax", "native", or "dom".
            "ax" remains as a compatibility alias for "cdp_ax".
        max_results: Maximum elements to return (default 5).
        wait_for_new: If true, ignore elements already present -- wait for NEW ones.
        gone: If true, wait for matching elements to DISAPPEAR instead.
    """
    try:
        results = tp.wait_for(
            element,
            app=app,
            window_id=_resolve(window_id) if window_id else None,
            role=_parse_role(role),
            states=_parse_states(states),
            fields=fields,
            mode=mode,
            timeout=timeout,
            source=source,
            max_results=max_results,
            wait_for_new=wait_for_new,
            gone=gone,
        )
        if gone:
            return "Elements gone."
        if not results:
            return "No elements found."
        return _mcp_format_elements(results)
    except TimeoutError as exc:
        return f"Timed out: {exc}"
    except Exception as exc:
        return _err(exc)


@_mcp_tool()
def wait_for_app(
    app: str,
    timeout: float = 10.0,
    gone: bool = False,
) -> str:
    """Wait for an application to appear or disappear.

    Polls the application list until the app is found (or gone).
    Use after launching or closing an application.

    Args:
        app: Application name to wait for (e.g. "Firefox", "Slack").
        timeout: Maximum seconds to wait (default 10).
        gone: If true, wait for the app to DISAPPEAR instead.
    """
    try:
        tp.wait_for_app(app, timeout=timeout, gone=gone)
        if gone:
            return f"App '{app}' is gone."
        return f"App '{app}' found."
    except TimeoutError as exc:
        return f"Timed out: {exc}"
    except Exception as exc:
        return _err(exc)


@_mcp_tool()
def wait_for_window(
    title: str,
    app: str | None = None,
    timeout: float = 10.0,
    gone: bool = False,
) -> str:
    """Wait for a window to appear or disappear.

    Polls the window list until a window with a matching title is
    found (or gone).  Use after actions that open or close windows.
    When found, auto-activates the window so subsequent type_text
    and press_key target it.

    Args:
        title: Window title to search for (substring match).
        app: Only look for windows in this application.
        timeout: Maximum seconds to wait (default 10).
        gone: If true, wait for the window to DISAPPEAR instead.
    """
    try:
        result = tp.wait_for_window(title, app=app, timeout=timeout, gone=gone)
        if gone:
            return f"Window '{title}' is gone."
        _do_activate(result.id)
        _apply_state(_capture_state())
        return f"Window found: {_mcp_format_window(result)}"
    except TimeoutError as exc:
        return f"Timed out: {exc}"
    except Exception as exc:
        return _err(exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the Touchpoint MCP server (stdio transport)."""
    mcp.run()


if __name__ == "__main__":
    main()

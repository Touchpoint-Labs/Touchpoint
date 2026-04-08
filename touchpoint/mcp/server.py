"""Touchpoint MCP server — 19 tools, 1 prompt.

Exposes the Touchpoint UI-automation API as MCP tools so LLM agents
(Claude Desktop, Cursor, Copilot, etc.) can observe and interact
with desktop applications.

Architecture
~~~~~~~~~~~~
- Each ``@mcp.tool()`` function is a thin wrapper around the
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
- No persistent server-side state beyond the alias map.

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

from mcp.server.fastmcp import FastMCP, Image

import touchpoint as tp
from touchpoint import Role, State


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
    - ``TOUCHPOINT_MAX_DEPTH``: int (default 10).
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
# Server instance
# ---------------------------------------------------------------------------

_role_values = ", ".join(r.value for r in Role)
_state_values = ", ".join(s.value for s in State)

mcp = FastMCP(
    "Touchpoint",
    instructions=(
        "You are an AI agent that controls a desktop computer through Touchpoint.\n"
        "You can see the screen, find UI elements, and interact with them.\n"
        "\n"
        "Always locate elements by ID using find() or elements() before acting.\n"
        "Do not estimate or guess screen coordinates from screenshots.\n"
        "\n"
        "== Workflow ==\n"
        "1. ORIENT  -- Take a screenshot to see the current screen state.\n"
        "2. LOCATE  -- Use find(query) to search for elements by name, or\n"
        "             elements(app) for a broader view.  Results include\n"
        "             element IDs you will use to act.\n"
        "3. ACT     -- Call the appropriate action tool (click, set_value,\n"
        "             type_text, press_key, etc.) using the element ID.\n"
        "4. VERIFY  -- Take another screenshot or re-find to confirm the\n"
        "             action had the intended effect.\n"
        "\n"
        "Repeat this loop as needed.  When uncertain, observe first.\n"
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
        '  resort, use elements(app="...", named_only=True) to browse what\n'
        "  is actually visible, or take a screenshot to check the UI state.\n"
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
        "== Action Patterns ==\n"
        "- Prefer keyboard shortcuts over clicking when you know the\n"
        "  exact shortcut -- they are faster and more reliable.\n"
        "- Text input has two tools:\n"
        "  * type_text(text) -- sends raw keystrokes to whatever has focus.\n"
        "    Click/focus a field first.  Works with any text input.\n"
        "  * set_value(element_id, value) -- targets a specific element by ID.\n"
        "    Inserts at cursor by default.  Pass replace=True to clear the\n"
        "    field and replace all content.  Useful when you need to replace\n"
        "    existing text in one step.\n"
        "- If click() doesn't have the expected effect, use get_element()\n"
        "  to see available actions, then call action() with the exact name.\n"
        "- activate_window(window_id) brings a window to the front.\n"
        "  Use it when switching between apps.\n"
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
        "\n"
        "== Filtering ==\n"
        "- Prefer named_only=True with elements() -- unnamed elements are\n"
        "  rarely useful.  Drop it only as a last resort.\n"
        '- role parameter narrows by element type: "button", "text_field",\n'
        '  "link", "menu_item", etc.\n'
        '- states parameter: "focused", "enabled", "checked", "selected".\n'
        "- Start with specific filters and broaden if needed.\n"
        "- source controls element origin:\n"
        '  "full" (default) = merged native + web for browser apps.\n'
        '  "ax" = CDP accessibility tree only (web content).\n'
        '  "native" = platform-native elements only.\n'
        '  "dom" = live DOM walker (web content, noisier but catches\n'
        "  elements the AX tree misses).\n"
        "\n"
        "== Error Recovery ==\n"
        "- ActionFailedError on click/set_value/focus: the element cannot\n"
        "  perform this action (disabled, wrong type, or stale ID).  Try:\n"
        "  1. Re-run find() to get a fresh ID for the same element.\n"
        "  2. If re-find returns nothing, the UI changed -- take a screenshot\n"
        "     to see the current state.\n"
        "  3. Try a different approach (keyboard shortcut instead of click).\n"
        "- find() returns empty results: loosen filters, simplify the\n"
        "  query, or fall back to elements(app=..., named_only=True) to\n"
        "  see what is available.  A screenshot can also help.\n"
        "- TimeoutError from wait_for: the expected element never appeared.\n"
        "  Take a screenshot to see what actually happened, then decide\n"
        "  whether to retry or take a different action.\n"
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
        "This is a last resort -- in most cases find() or elements()\n"
        "will return what you need.\n"
        "\n"
        "== Coordinate Mode ==\n"
        "click() and mouse_move() accept x,y screen coordinates as an\n"
        "alternative to element_id.  This is a LAST RESORT -- only use\n"
        "coordinates when element-ID-based actions do not work:\n"
        "  1. Clicking by ID triggers an unintended action (e.g. opens\n"
        "     a dropdown instead of focusing) -- use the element's own\n"
        "     @(x,y) shown in find/elements output.\n"
        "  2. An element is visible but not in the accessibility tree\n"
        "     (see Missing Elements above for the full escalation path).\n"
        "NEVER guess or estimate coordinates.  Always derive them from\n"
        "element positions shown in find/elements output.\n"
        "\n"
        "== Valid Roles ==\n"
        f"{_role_values}\n"
        "\n"
        "== Valid States ==\n"
        f"{_state_values}"
    ),
)


# ---------------------------------------------------------------------------
# Alias system — short ephemeral IDs for MCP
# ---------------------------------------------------------------------------

_alias_to_real: dict[str, str] = {}
_real_to_alias: dict[str, str] = {}
_counters: dict[str, int] = {}

# Pattern to detect backend prefix from real IDs.
_BACKEND_PREFIX_RE = re.compile(r"^(atspi|cdp|uia|ax|dom)(?=:)")


def _alias(real_id: str) -> str:
    """Return (or create) a short alias for a real backend ID.

    ``"cdp:9223:93C006A6A1B4D97B2DB98110132BC9F2:813"`` -> ``"cdp1"``.
    Already-aliased IDs are returned unchanged.
    """
    if real_id in _real_to_alias:
        return _real_to_alias[real_id]

    m = _BACKEND_PREFIX_RE.match(real_id)
    prefix = m.group(1) if m else "e"

    _counters[prefix] = _counters.get(prefix, 0) + 1
    short = f"{prefix}{_counters[prefix]}"

    _alias_to_real[short] = real_id
    _real_to_alias[real_id] = short
    return short


def _resolve(alias_or_real: str) -> str:
    """Translate a short alias back to the real backend ID.

    Passes through unknown strings unchanged (backward-compat with
    raw IDs).
    """
    return _alias_to_real.get(alias_or_real, alias_or_real)


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
})


def _mcp_format_element(el: tp.Element) -> str:
    """Format a single element for MCP output.

    Compact one-liner: ``[cdp1] button 'Close' @(512,340) app=Discord``

    Omits window ID, action list, and noise states.
    """
    short_id = _alias(el.id)
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


def _mcp_format_elements_tree(
    elements: list[tp.Element], depth: int = 0,
) -> str:
    """Format elements with tree indentation, recursing into children."""
    lines: list[str] = []
    for el in elements:
        indent = "  " * depth
        lines.append(f"{indent}{_mcp_format_element(el)}")
        if el.children:
            lines.append(_mcp_format_elements_tree(el.children, depth + 1))
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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
    """Search for UI elements by name.

    Finds elements matching a text query, ranked by match quality.
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
        source: "full" (default, merged native+web), "ax" (CDP accessibility tree only), "native" (platform only), or "dom" (live DOM).
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


@mcp.tool()
def elements(
    app: str | None = None,
    window_id: str | None = None,
    tree: bool = False,
    max_depth: int | None = None,
    root_element: str | None = None,
    max_elements: int | None = None,
    role: str | None = None,
    states: list[str] | None = None,
    named_only: bool = False,
    sort_by: str | None = None,
    source: str = "full",
) -> str:
    """Get UI elements from the accessibility tree.

    Returns a broad view of available elements.  Use find() instead
    when you know the element's name -- it is faster and ranked.

    Args:
        app: Scope to this application.
        window_id: Scope to this window.
        tree: If true, include parent/child hierarchy.
        max_depth: Maximum tree depth (0 = immediate children only).
        root_element: Start from this element ID (drill into a container).
        max_elements: Maximum elements to return (prevents huge results).
        role: Only include this role (e.g. "button", "text_field").
        states: Only include elements with ALL these states.
        named_only: If true, exclude elements with empty names.
        sort_by: None (default, tree order) or "position" for reading order (top-to-bottom, left-to-right).
        source: "full" (default, merged native+web), "ax" (CDP AX tree only), "native" (platform only), or "dom" (live DOM).
    """
    try:
        results = tp.elements(
            app=app,
            window_id=_resolve(window_id) if window_id else None,
            tree=tree,
            max_depth=max_depth,
            root_element=_resolve(root_element) if root_element else None,
            max_elements=max_elements,
            role=_parse_role(role),
            states=_parse_states(states),
            named_only=named_only,
            sort_by=sort_by,
            source=source,
        )
        if isinstance(results, str):
            return results
        if not results:
            return "No elements found."
        if tree:
            return _mcp_format_elements_tree(results)
        return _mcp_format_elements(results)
    except Exception as exc:
        return _err(exc)


@mcp.tool()
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
# Tools -- Screenshot
# ---------------------------------------------------------------------------


@mcp.tool()
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


@mcp.tool()
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
            if double_click:
                tp.double_click_at(x, y)
                return f"double_click_at({x}, {y}): OK"
            elif button == "right":
                tp.right_click_at(x, y)
                return f"right_click_at({x}, {y}): OK"
            else:
                tp.click_at(x, y)
                return f"click_at({x}, {y}): OK"
        if element_id is None:
            return "Error: provide element_id or both x and y."
        real_id = _resolve(element_id)
        if double_click:
            return _ok("double_click", tp.double_click(real_id))
        elif button == "right":
            return _ok("right_click", tp.right_click(real_id))
        else:
            return _ok("click", tp.click(real_id))
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def set_value(element_id: str, value: str, replace: bool = False) -> str:
    """Set text content of an editable element.

    Args:
        element_id: The element ID (a text field, combo box, etc.).
        value: The text to write.
        replace: If true, clear the field first and replace all content.
                 If false (default), insert at the current cursor position.
    """
    try:
        return _ok("set_value", tp.set_value(_resolve(element_id), value, replace=replace))
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def set_numeric_value(element_id: str, value: float) -> str:
    """Set the numeric value of a range element (slider, spinbox).

    Args:
        element_id: The element ID (a slider, spin button, etc.).
        value: The numeric value to set.
    """
    try:
        return _ok("set_numeric_value", tp.set_numeric_value(_resolve(element_id), value))
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def focus(element_id: str) -> str:
    """Move keyboard focus to an element.

    Args:
        element_id: The element ID to focus.
    """
    try:
        return _ok("focus", tp.focus(_resolve(element_id)))
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def select_text(element_id: str, text: str, occurrence: int = 1) -> str:
    """Select a substring within an element's text content.

    Finds the text within the element and applies a native text
    selection over that range.  Useful for formatting, copying,
    or replacing specific text within a document or text field.

    Args:
        element_id: The element ID containing the text.
        text: The exact substring to select.
        occurrence: Which occurrence to select (1 = first, 2 = second, etc.).
    """
    try:
        return _ok(
            "select_text",
            tp.select_text(_resolve(element_id), text, occurrence=occurrence),
        )
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def action(element_id: str, action_name: str) -> str:
    """Perform a raw accessibility action by exact name.

    Use this when the convenience functions (click, focus, etc.)
    do not cover what you need.  Call ``get_element`` first to
    see the element's actions list, then pass the exact name here.

    Args:
        element_id: The element ID.
        action_name: Exact action name (e.g. "activate", "expand or collapse", "ShowMenu").
    """
    try:
        return _ok(f"action({action_name!r})", tp.action(_resolve(element_id), action_name))
    except Exception as exc:
        return _err(exc)


# ---------------------------------------------------------------------------
# Tools -- Keyboard input
# ---------------------------------------------------------------------------


@mcp.tool()
def type_text(text: str) -> str:
    """Type text into the currently focused element.

    Simulates keyboard input.  Focus a text field first with
    click() or focus(), then type into it.

    Special characters:
      \\n = Enter (line break),  \\t = Tab (next field),
      \\b = Backspace (delete previous character).

    Args:
        text: The text to type.
    """
    try:
        # Normalise literal escape sequences from MCP/JSON callers
        # to real characters so the public API handles them.
        tp.type_text(
            text.replace("\\n", "\n").replace("\\t", "\t").replace("\\b", "\b")
        )
        return "type_text: OK"
    except Exception as exc:
        return _err(exc)


@mcp.tool()
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
        return f"{label}: OK"
    except Exception as exc:
        return _err(exc)


# ---------------------------------------------------------------------------
# Tools -- Mouse / scroll
# ---------------------------------------------------------------------------


@mcp.tool()
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
        el = tp.get_element(_resolve(element_id))
        if el is None:
            return f"Error: element {element_id!r} not found."
        tp.mouse_move(*el.position)
        return f"mouse_move: OK -- cursor at {el.position}"
    except Exception as exc:
        return _err(exc)


@mcp.tool()
def scroll(direction: str, amount: int = 3) -> str:
    """Scroll at the current cursor position.

    Move the cursor to the target area first with mouse_move(),
    then call scroll().

    Args:
        direction: One of "up", "down", "left", "right".
        amount: Number of scroll ticks (default 3).
    """
    try:
        tp.scroll(direction=direction, amount=amount)
        return f"scroll({direction}, {amount}): OK"
    except Exception as exc:
        return _err(exc)


# ---------------------------------------------------------------------------
# Tools -- Window actions
# ---------------------------------------------------------------------------


@mcp.tool()
def activate_window(window_id: str) -> str:
    """Bring a window to the foreground.

    Use windows() to find the window ID first.

    Args:
        window_id: The window ID to activate.
    """
    try:
        return _ok("activate_window", tp.activate_window(_resolve(window_id)))
    except Exception as exc:
        return _err(exc)


# ---------------------------------------------------------------------------
# Tools -- Waiting
# ---------------------------------------------------------------------------


@mcp.tool()
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
        source: "full" (default), "ax", "native", or "dom".
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


@mcp.tool()
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


@mcp.tool()
def wait_for_window(
    title: str,
    app: str | None = None,
    timeout: float = 10.0,
    gone: bool = False,
) -> str:
    """Wait for a window to appear or disappear.

    Polls the window list until a window with a matching title is
    found (or gone).  Use after actions that open or close windows.

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

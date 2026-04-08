# Touchpoint — Architecture & Design Decisions

> Internal reference document for contributors. Updated as the project evolves.

---

## What Touchpoint Is

- A **Python client library**: `pip install touchpoint-py`
- Gives agent developers **structured access to desktop UI** (elements, windows, positions, actions) via native accessibility APIs
- **Tree-first approach**: the accessibility tree is the primary perception method; screenshots are a simple optional utility
- The **library** is unopinionated — it provides primitives, not workflows
- The **MCP server** is opinionated — its instructions guide agents toward effective patterns (scoping, keyboard-first, verify-after-act)

## What Touchpoint Is NOT

- Not an agent, framework, or LLM integration
- Not a vision/screenshot analysis pipeline
- Not workflow recording/playback

---

## Project Structure

```
touchpoint/
├── __init__.py              ← public API surface (tp.elements(), tp.find(), etc.)
├── core/
│   ├── element.py           ← Element dataclass
│   ├── window.py            ← Window dataclass
│   ├── types.py             ← Role, State enums
│   └── exceptions.py        ← custom exceptions
├── backends/
│   ├── base.py              ← Backend ABC + InputProvider ABC
│   ├── linux/
│   │   ├── atspi.py         ← AT-SPI2 backend (native Linux apps)
│   │   └── x11/
│   │       └── input.py     ← XdotoolInput (raw keyboard/mouse via xdotool)
│   ├── windows/
│   │   ├── uia.py           ← UIAutomation backend (native Windows apps)
│   │   └── input.py         ← SendInputProvider (ctypes → SendInput)
│   ├── macos/
│   │   ├── ax.py            ← macOS AX backend (native macOS apps via pyobjc)
│   │   └── input.py         ← CGEventInput (raw keyboard/mouse via CGEvent)
│   └── cdp/
│       └── cdp.py           ← Chrome DevTools Protocol (Electron/Chromium apps)
├── matching/
│   └── matcher.py           ← fuzzy/smart matching pipeline
├── format/
│   └── formatter.py         ← output formatting (flat, json, tree)
├── mcp/
│   └── server.py            ← MCP server (touchpoint-mcp entry point)
└── utils/
    ├── screenshot.py        ← basic screenshot utility (returns PIL.Image)
    └── scale.py             ← DPR/scale factor detection (X11 xrdb, Win32 per-monitor)

tests/
├── conftest.py          ← shared fixtures, platform detection, skip helpers
├── test_discovery.py    ← windows() / apps() listing
├── test_elements.py     ← elements(), get_element(), element_at(), filtering
├── test_find.py         ← find() matching, fields, formatting
├── test_config.py       ← configure() validation & behaviour
├── test_actions.py      ← focus, click, set_value (destructive, gated)
├── test_cdp.py          ← CDP backend: connection, tree, DOM, actions, cross-origin
├── test_format.py       ← flat / json / tree formatter output
├── test_input.py        ← InputProvider keyboard/mouse simulation
├── test_matcher.py      ← 4-stage matching pipeline
├── test_scale.py        ← scale factor detection & configure(scale_factor=...) override
├── test_screenshot.py   ← screenshot capture & crop
└── test_wait.py         ← wait_for() / wait_for_app() / wait_for_window() polling
```

---

## Core Data Models

### Element

```python
@dataclass
class Element:
    id: str                    # stable identifier (e.g. atspi:{pid}:{dbus_path}:{child_path})
    name: str                  # "Send now", "File", "Close"
    role: Role                 # button, menu_item, text_field, etc.
    states: list[State]        # visible, enabled, focused, checked, etc.
    position: tuple[int, int]  # (x, y) screen coordinates (center of element)
    size: tuple[int, int]      # (width, height)
    actions: list[str]         # ["click", "press", "activate"]
    value: str | None          # current value (for text fields, sliders, etc.)
    description: str | None    # accessibility description if available
    children: list[Element]    # child elements (empty by default, populated with tree=True)
    parent_id: str | None      # parent element id
    window_id: str | None      # top-level window id this element belongs to
    app: str                   # which application owns this element
    pid: int                   # process id of owning app
    backend: str               # "atspi", "cdp", "uia", "ax" — which backend found this
    raw_role: str              # original role string from the backend before mapping
    raw: dict                  # backend-specific extra data (empty dict by default)
```

### Window

```python
@dataclass
class Window:
    id: str
    title: str
    app: str
    pid: int
    position: tuple[int, int]
    size: tuple[int, int]
    is_active: bool
    is_visible: bool
    raw: dict                  # backend-specific extra data (empty dict by default)
```

### Design decisions:
- `Element.children` is **empty by default** (flat mode). Populated only when user requests `tree=True`. Keeps the common case fast.
- `Element.raw_role` preserves the original backend role string even when it maps to `Role.UNKNOWN`.
- `raw` dict on both Element and Window is the **escape hatch** for backend-specific data. Each backend dumps extra attributes it has into `raw` — for AT-SPI2 this is the accessible's `get_attributes()` dict (toolkit name, widget class, etc.). Costs nothing when empty.
- **Format philosophy**: flat and tree formats are curated for LLM prompts (no `raw`). JSON format is complete for machines and power users (includes `raw` when non-empty).
- Roles are a Python enum for common ones. Unmapped roles become `Role.UNKNOWN` with the original in `raw_role`.

---

## Role & State Enums

### Role (unified across all backends)

Each backend translates its native roles to these.  **73 roles** total — common ones include:
- BUTTON, TEXT_FIELD, LABEL, MENU, MENU_ITEM, MENU_BAR
- CHECK_BOX, RADIO_BUTTON, COMBO_BOX, TOGGLE_BUTTON
- LIST, LIST_ITEM, TAB, TAB_LIST
- TREE, TREE_ITEM, TABLE, TABLE_CELL
- SLIDER, SCROLL_BAR, PROGRESS_BAR
- TOOLBAR, STATUS_BAR, DIALOG, WINDOW, PANEL
- LINK, IMAGE, SEPARATOR, HEADING
- TEXT, DOCUMENT, APPLICATION
- SWITCH, SPLIT_BUTTON, PASSWORD_TEXT, LANDMARK, NAVIGATION
- BANNER, SEARCH, CONTENT_INFO, HEADER, FOOTER, ARTICLE, FEED
- UNKNOWN (with raw_role preserving original)

### State

**32 states** total:
- VISIBLE, SHOWING, ENABLED, SENSITIVE, FOCUSABLE, FOCUSED, CLICKABLE
- SELECTED, SELECTABLE, CHECKED, PRESSED
- EXPANDABLE, EXPANDED, COLLAPSED
- EDITABLE, READ_ONLY, MULTI_LINE, SINGLE_LINE
- MODAL, ACTIVE, RESIZABLE, REQUIRED, INVALID
- HORIZONTAL, VERTICAL, BUSY, INDETERMINATE
- HAS_POPUP, MULTISELECTABLE, OFFSCREEN, DEFUNCT, VISITED

---

## Two-Layer Backend Architecture

Touchpoint separates accessibility into two distinct layers, each with its own ABC:

### Layer 1 — Backend (structured accessibility)

The `Backend` ABC handles **element-aware** operations: discovering the UI tree, reading element properties, and performing native accessibility actions. It has **11 abstract methods** and **7 concrete methods** with safe defaults:

```python
class Backend(ABC):
    # -- Discovery (5 abstract) --
    def get_applications(self) -> list[str]: ...
    def get_windows(self) -> list[Window]: ...
    def get_elements(self, app, window_id, tree, max_depth, root_element) -> list[Element]: ...
    def get_element_at(self, x: int, y: int) -> Element | None: ...
    def get_element_by_id(self, element_id: str) -> Element | None: ...

    # -- Actions (5 abstract) --
    def do_action(self, element_id: str, action: str) -> bool: ...
    def set_value(self, element_id: str, value: str, replace: bool) -> bool: ...
    def set_numeric_value(self, element_id: str, value: float) -> bool: ...
    def focus_element(self, element_id: str) -> bool: ...
    def select_text(self, element_id: str, start: int, end: int) -> bool: ...

    # -- Availability (1 abstract) --
    def is_available(self) -> bool: ...

    # -- Concrete defaults (7 methods) --
    def inflate_element(self, element: Element) -> Element: ...
    def activate_window(self, window_id: str) -> bool: ...
    def get_owned_pids(self) -> set[int]: ...
    def owns_element(self, element_id: str) -> bool: ...
    def claims_app(self, app_name: str) -> bool: ...
    def get_topmost_pid_at(self, x: int, y: int) -> int | None: ...
    def set_pid_display_names(self, mapping: dict[int, str]) -> None: ...
```

### Layer 2 — InputProvider (raw OS input)

The `InputProvider` ABC handles **coordinate-based, element-blind** input simulation. It has **9 abstract methods** and **1 concrete method**:

```python
class InputProvider(ABC):
    # -- Keyboard (3 methods) --
    def type_text(self, text: str) -> None: ...
    def press_key(self, key: str) -> None: ...
    def hotkey(self, *keys: str) -> None: ...

    # -- Mouse (5 methods) --
    def click_at(self, x: int, y: int) -> None: ...
    def double_click_at(self, x: int, y: int) -> None: ...
    def right_click_at(self, x: int, y: int) -> None: ...
    def scroll(self, x, y, direction, amount) -> None: ...
    def mouse_move(self, x: int, y: int) -> None: ...

    # -- Availability (1 method) --
    def is_available(self) -> bool: ...

    # -- Concrete default (1 method) --
    def activate_window(self, title: str, pid: int) -> bool: ...
```

### Why two ABCs?

| Concern | Backend | InputProvider |
|---------|---------|---------------|
| Knows about elements? | Yes | No — coordinates only |
| Talks to a11y APIs? | AT-SPI2, UIA, AX, CDP | OS input queue |
| Feedback? | Returns `bool` (success/fail) | Fire-and-forget (`None`) |
| Fails how? | `ActionFailedError` | `RuntimeError` |
| Purpose | Primary path — structured, reliable | Fallback — when native a11y can't do it |

### How they work together

```
tp.click(element)
    │
    ├─ try: Backend.do_action(id, "click"/"press"/"activate")
    │      └─ success? → return True
    │
    ├─ all aliases failed, AND fallback_input is True?
    │      ├─ Backend.get_element_by_id(id) → fresh Element with (x, y)
    │      ├─ InputProvider.click_at(x, y)
    │      └─ return True
    │
    └─ fallback_input is False? → raise ActionFailedError

tp.set_value(element, text)
    │
    ├─ try: Backend.set_value(id, text, replace)
    │      └─ success? → return True
    │
    ├─ failed, AND fallback_input is True?
    │      ├─ try: Backend.focus_element(id) — best-effort, ignore failure
    │      ├─ if replace: InputProvider.hotkey("ctrl", "a")
    │      ├─ InputProvider.type_text(text)
    │      └─ return True
    │
    └─ fallback_input is False? → raise ActionFailedError
```

### Implementations per platform

| Platform | Backend | InputProvider |
|----------|---------|---------------|
| Linux / X11 | `AtSpiBackend` (AT-SPI2 via PyGObject) | `XdotoolInput` (xdotool subprocess) |
| Windows | `UiaBackend` (UIAutomation via comtypes) | `SendInputProvider` (ctypes → SendInput) |
| macOS | `AxBackend` (macOS AX via pyobjc) | `CGEventInput` (CGEvent via pyobjc) |
| CDP (browser) | `CdpBackend` (WebSocket → CDP) | Internal (`Input.dispatch*Event`) |

Touchpoint holds multiple backends and **merges their results**.
The platform backend (AT-SPI2 or UIA) always runs.  The CDP backend
runs alongside it when ``websocket-client`` is installed and at least
one Chromium/Electron app is launched with ``--remote-debugging-port``.

**Multi-backend routing rules:**

- **Element IDs** encode their backend: ``atspi:…``, ``uia:…``, ``cdp:…``, ``ax:…``.
  Action/lookup functions route to the correct backend by prefix.
- **`windows()`** merges: platform windows whose PID matches a CDP
  process are replaced by the richer CDP windows.
- **`elements()`** merges using document-subtree stripping.  For
  CDP-backed apps (scoped by ``app=``), both CDP and platform backends
  are queried; ``Role.DOCUMENT`` elements and their descendants are
  stripped from the platform results (AT-SPI / UIA) to avoid
  duplicating the web content that CDP already covers, while keeping
  native UI (title bars, toolbars, dialogs).  When unscoped, the same
  stripping is applied to platform elements whose PID matches a CDP
  process.  When scoped to a specific ``window_id`` or
  ``root_element``, routing is by ID prefix.
- **Actions** (``click``, ``set_value``, etc.) route by element ID prefix.
  InputProvider fallback (xdotool / SendInput) is **not** used for CDP
  elements — CDP actions use ``Input.dispatch*Event`` internally.
  Fallback is intentionally blocked: for clicks, CDP scrolls the element
  into view before dispatching; xdotool cannot, so fallback would click
  the wrong spot or empty space. For ``set_value``, the common CDP
  failure modes (element not in AX tree, ``DOM.focus`` rejected) also
  break the fallback focus step, causing xdotool to type into whatever
  Chrome currently has focused — silent data corruption.
- **`find()`** searches both backends and merges results.

---

## Public API

```python
import touchpoint as tp

# --- Discovery ---
tp.apps()                              → list[str]
tp.windows()                           → list[Window]
tp.elements(app="Slack")               → list[Element]
tp.elements(app="Slack", tree=True)    → list[Element] (with children populated)
tp.elements(app="Slack", tree=True, max_depth=2)  → shallow overview (2 levels)
tp.elements(root_element="atspi:2269:1:4.0", tree=True, max_depth=3)  → drill into container
tp.element_at(500, 300)                → Element | None

# --- Finding / Matching ---
tp.find("Send")                        → list[Element]  (all matches, best first)
tp.find("Send", role=tp.Role.BUTTON)   → list[Element]  (filtered by role)
tp.find("Send", max_results=1)         → list[Element]  (top match only)
tp.find("Send", format="flat")         → str            (flat formatted)
tp.find("Send", format="json")         → str            (json formatted)
# tp.find(..., format="tree")  → ValueError (results are ranked, not hierarchical)

# --- Element Lookup ---
tp.get_element("atspi:2269:1:2.1")       → Element | None  (fresh snapshot by id)
tp.get_element("cdp:9222:TID:4", format="flat") → str     (formatted output)

# --- Waiting ---
tp.wait_for("Submit", timeout=10)       → list[Element]  (poll until found)
tp.wait_for(["Success", "Error"])       → list[Element]  (race: first match wins)
tp.wait_for(["A", "B"], mode="all")    → list[Element]  (all must appear)
tp.wait_for("Result", max_results=3)    → list[Element]  (cap returned matches)
tp.wait_for("Article", wait_for_new=True) → list[Element]  (ignore pre-existing)
tp.wait_for("Loading", gone=True)       → True           (poll until gone)
tp.wait_for_app("Firefox")              → True           (poll until app appears)
tp.wait_for_app("Firefox", gone=True)   → True           (poll until app disappears)
tp.wait_for_window("Settings")          → Window         (poll until window appears)
tp.wait_for_window("Settings", gone=True) → True         (poll until window disappears)

# --- Element-targeted Actions ---
tp.click(element)                      → bool   # tries click/press/activate, fallback to InputProvider
tp.double_click(element)               → bool   # tries double_click/activate, fallback to InputProvider
tp.right_click(element)                → bool   # tries ShowMenu/show_menu, fallback to InputProvider
tp.set_value(element, "hello")         → bool   # insert at cursor, fallback: focus → type
tp.set_value(element, "new", replace=True) → bool # replace, fallback: focus → select-all → type
tp.set_numeric_value(element, 75.0)    → bool   # sliders, spinboxes (Value interface, no fallback)
tp.focus(element)                      → bool   # grab_focus / SetFocus (native only, no click fallback)
tp.action(element, "ShowMenu")         → bool   # raw action, no aliases, no fallback

# --- Coordinate / Global Input (InputProvider) ---
tp.click_at(500, 300)                  → None
tp.double_click_at(500, 300)           → None
tp.right_click_at(500, 300)            → None
tp.type_text("hello")                  → None
tp.press_key("enter")                  → None
tp.hotkey("ctrl", "c")                 → None
tp.scroll(500, 300, direction="down", amount=3) → None
tp.mouse_move(500, 300)                → None

# --- Screenshot ---
tp.screenshot()                        → PIL.Image  (full desktop)
tp.screenshot(app="Slack")             → PIL.Image  (cropped to app window)
tp.screenshot(window_id="atspi:...")   → PIL.Image  (cropped to specific window)
tp.screenshot(element=button)          → PIL.Image  (cropped to element bounds)
tp.screenshot(element=button, padding=20) → PIL.Image (element + 20px margin)
tp.screenshot(monitor=0)              → PIL.Image  (specific monitor only)
tp.monitor_count()                     → int        (number of monitors)

# --- Formatting ---
tp.elements(app="Slack", format="flat")    → str   (one line per element, compact)
tp.elements(app="Slack", format="json")    → str   (JSON array, all fields)
tp.elements(app="Slack", format="tree")    → str   (indented hierarchy)

# --- Source Selection ---
tp.elements(app="Chrome", source="full")    → list[Element]  (default: merged native UI + web content)
tp.elements(app="Chrome", source="ax")      → list[Element]  (CDP AX tree only, web content)
tp.elements(app="Chrome", source="native")  → list[Element]  (platform-native only, e.g. toolbar/tabs)
tp.elements(app="Chrome", source="dom")     → list[Element]  (DOM walker, web content)

# --- Configuration ---
tp.configure(fuzzy_threshold=0.8)      # minimum fuzzy match score (default 0.6)
tp.configure(fallback_input=False)     # disable xdotool fallback (default True)
tp.configure(type_chunk_size=50)       # split long text into 50-char chunks (default 40)
tp.configure(max_elements=5000)        # max elements to collect per call (default 5000)
tp.configure(max_depth=10)             # default max depth for tree walks (default 10)
tp.configure(cdp_ports={"Slack": 9222})  # explicit CDP port mapping (default None = auto-discover)
tp.configure(cdp_discover=False)       # disable cdp auto discover (default True)
tp.configure(scale_factor=1.25)        # override DPR scaling (default None = auto-detect)
tp.configure(cdp_refresh_interval=10)  # seconds between CDP auto-refresh (default 5)
```

### API design decisions:
- `tp.find()` returns a **list of `Element`** (not `MatchResult`), ranked best-first. Match scores and match types are internal — order is the signal, `fuzzy_threshold` in `configure()` gates quality. Use `max_results=1` for single-match use cases.
- `format=` parameter on both `elements()` and `find()` for convenience. Returns string instead of list[Element] when format is specified. `elements()` supports three formats: `flat`, `json`, `tree`. `find()` supports `flat` and `json` only — `tree` raises `ValueError` because search results are ranked, not hierarchical.
- No auto-role detection from query strings. Role filtering is always explicit via `role=` parameter.
- **Convenience actions** (`click`, `double_click`, `right_click`) try multiple alias names automatically (e.g. `click` tries `click` → `press` → `activate`). If all fail and `fallback_input=True`, resolves a fresh position via `get_element_by_id` and uses InputProvider for coordinate-based input. **Raw `action()`** takes the exact name with no aliases and no fallback.
- **`focus()`** calls `Backend.focus_element()` directly — it has its own backend method because focus is a Component interface operation, not an Action. No alias indirection. No InputProvider fallback — clicking has semantic side effects beyond focus (opens dropdowns, moves cursors, triggers selections). If the agent wants click-to-focus, it should call `tp.click()` explicitly.
- **`set_value`** inserts at cursor by default. Pass `replace=True` to clear the field first. Uses native EditableText/Value interface via the Backend. If that fails and `fallback_input=True`, falls back to: best-effort native focus (ignore failure) → select-all (if replace) → type via InputProvider. Focus failure is tolerated because the element may already be focused, or the agent may have clicked it before calling `set_value`.
- **`set_numeric_value`** sets sliders, spinboxes, and other range-valued controls via the native Value interface. Separate from `set_value` because it takes a `float`, not a `str`. No InputProvider fallback.
- **`get_element(id)`** retrieves a fresh Element snapshot by id. Used internally for fallback (resolving current position from string ids), but also useful for agents that want to re-check an element's state after performing an action. Accepts optional `format=` parameter for direct formatted output.
- **`wait_for(query)`** polls `find()` at a configurable interval until matching elements appear or timeout. Supports single query or list of queries with `mode="any"` (race — first match wins) or `mode="all"` (convergence — all must match). `max_results` caps the returned list (e.g. `max_results=3`). `wait_for_new=True` snapshots existing matches at call time and only returns elements whose IDs are new — useful when the query already matches on-screen content but you're waiting for fresh results (e.g. after navigation). Returns matching elements. Raises `TimeoutError` on timeout.
- **`wait_for(query, gone=True)`** is the inverse — polls until no matches remain. Returns `True` on success, raises `TimeoutError` if elements persist.
- **`wait_for_app(app)`** polls `apps()` until the app appears (or disappears with `gone=True`). Returns `True`. Raises `TimeoutError`.
- **`wait_for_window(title)`** polls `windows()` until a window with matching title appears (returns `Window`) or disappears with `gone=True` (returns `True`). Raises `TimeoutError`.
- All action functions accept **`Element | str`** — either an Element object or a bare id string. LLMs can use IDs directly from previous output.
- Actions prefer native accessibility action first, fall back to coordinate-based input (InputProvider) if native fails and `fallback_input=True`.
- `configure()` has exactly **9 knobs**: `fuzzy_threshold` (default 0.6), `fallback_input` (default True), `type_chunk_size` (default 40, splits long text into chunks for xdotool), `max_elements` (default 5000), `max_depth` (default 10), `cdp_ports` (default None, dict mapping app names to debugging ports), `cdp_discover` (default True, auto-scans /proc for --remote-debugging-port), `scale_factor` (default None, auto-detects from Xft.dpi/Win32 per-monitor DPI; pass a float to override), and `cdp_refresh_interval` (default 5.0, seconds between automatic CDP refresh cycles). Set-and-forget flags, not per-call parameters.
- **`source=` parameter** on `elements()`, `find()`, `wait_for()` controls element sources: `"full"` (default) merges native platform UI (toolbar, tabs, menus) with CDP web content for browser/Electron apps; `"ax"` returns CDP AX tree only (web content, no native merge); `"native"` returns platform-native elements only (no web content); `"dom"` uses the CDP DOM walker instead of the AX tree. For non-CDP apps, `"full"` and `"native"` produce platform-native results; `"ax"` and `"dom"` raise `TouchpointError` because they explicitly request CDP-specific sources.

### Parameter split — Backend ABC vs Public API:
- **Backend methods** (`get_applications`, `get_windows`, `get_elements`, etc.) handle **scoping only** — they determine *which subtree* to walk. Parameters: `app`, `window_id`, `tree`, `max_depth`, `root_element`. Backends also accept optional **filter hints** (`role`, `states`) that are applied as early-skip checks during the walk when `tree=False` — non-matching elements are never built, but their children are still visited (a non-matching parent may have matching descendants). When `tree=True`, hints are ignored because the tree structure requires all nodes.
- **Public API** (`tp.elements()`, `tp.find()`, etc.) passes `role`/`states` to the backend for early filtering, then applies post-filtering for remaining criteria (`named_only`, custom `filter`, `sort_by`). The post-filter is effectively a no-op for role/states when the backend already handled them.
- `states` accepts a list of `State` values with **AND** logic (element must have *all* listed states). `states=None` (default) means no filtering. Example: `states=[State.VISIBLE, State.ENABLED]` returns only elements that are both visible and enabled.
- No `visible_only` flag — visibility is just another state, not a special case.

---

## Matching Pipeline

When `tp.find("send message")` is called:

```
"send message"
      │
      ▼
 ┌───────────┐
 │ Exact Match│  → element.name == "send message" ?
 └─────┬─────┘
       │ no
       ▼
 ┌───────────────┐
 │ Contains       │  → element.name contains "send message" as substring ?
 └──────┬────────┘
        │ no
        ▼
 ┌───────────────────┐
 │ Contains Words     │  → element.name contains all words {"send", "message"} ?
 └──────┬────────────┘
        │ no
        ▼
 ┌───────────┐
 │ Fuzzy Match│  → rank by string similarity (rapidfuzz)
 └─────┬─────┘
       │
       ▼
 Return ranked list with confidence scores
```

Stages run in order; if an earlier stage produces results the later stages are skipped:

1. **Exact** — case-insensitive full-string equality (score 1.0).
2. **Contains** — query appears as a substring of the element name (score 0.7–0.9 by length ratio).
3. **Contains Words** — all query words appear in the element name regardless of order (score 0.65–0.85 by word coverage). Only fires for multi-word queries. Catches "Message Send" when searching "send message".
4. **Fuzzy** — Levenshtein-based similarity via `rapidfuzz` (score = fuzzy ratio × 0.01, gated by `fuzzy_threshold`).

- Uses `rapidfuzz` if installed. No fallback — exact, contains, and contains-words stages still work without it.
- Results are filtered by optional `role=`, `app=`, `states=` parameters BEFORE matching runs.
- Fuzzy threshold is configurable via `tp.configure(fuzzy_threshold=0.8)`. Default is `0.6`.

---

## CDP Backend

### Overview

- For Electron/Chromium apps that don't expose useful AT-SPI2/UIA trees (Slack returns 2 elements via AT-SPI2, but 568 via CDP).
- Requires app to be launched with `--remote-debugging-port=XXXX`.
- Auto-discovers CDP ports by scanning `/proc/*/cmdline` (Linux), PowerShell `Get-CimInstance Win32_Process` (Windows), or `ps -eo pid,args` (macOS).
- Users can also register ports explicitly: `tp.configure(cdp_ports={"Slack": 9222})`.
- Connects via synchronous WebSocket (`websocket-client`), calls `Accessibility.getFullAXTree`.
- Translates CDP accessibility nodes to Touchpoint `Element` objects.

### CDP Domains Used

| Domain | Methods | Purpose |
|---|---|---|
| **Accessibility** | `enable`, `getFullAXTree`, `getPartialAXTree`, `queryAXTree` | Primary element discovery |
| **DOM** | `enable`, `getBoxModel`, `scrollIntoViewIfNeeded`, `describeNode`, `focus`, `resolveNode`, `getNodeForLocation`, `setAttributeValue`, `getFrameOwner` | Geometry, focus, DOM walker, password detection |
| **Input** | `dispatchMouseEvent`, `dispatchKeyEvent`, `insertText` | Click, type, keyboard actions |
| **Page** | `enable`, `captureScreenshot`, `bringToFront`, `handleJavaScriptDialog`, `getFrameTree` | Screenshots, window activation, dialog handling |
| **Runtime** | `evaluate`, `callFunctionOn`, `releaseObject` | DOM walker JS, select/slider value setting, coordinate conversion |
| **Target** | `attachToTarget`, `activateTarget` | Session management, tab discovery |

### Source Architecture

The `source=` parameter controls how elements are collected for browser/Electron apps:

- **Full** (`source="full"`, default): Merges platform-native UI elements (toolbar, tabs, address bar, menus via AT-SPI/UIA) with CDP web content (AX tree). Native elements are collected first with a budget, then CDP fills the remainder.
- **AX tree** (`source="ax"`): `Accessibility.getFullAXTree` returns the browser's accessibility tree only. Fast, semantically rich (roles, states, names), but covers only web content — no native toolbar/menu elements.
- **Native** (`source="native"`): Platform backend only (AT-SPI on Linux, UIA on Windows). Returns native UI elements without any CDP web content. Useful for interacting with the browser chrome (toolbar, tabs, menus) without the noise of web content.
- **DOM walker** (`source="dom"`): `Runtime.evaluate` runs a JS function that walks the live DOM, collecting tag names, attributes, text content, and bounding rectangles. Catches elements the AX tree misses but produces noisier output with less semantic information.

For non-CDP apps, `"full"` and `"native"` produce platform-native results.  `"ax"` and `"dom"` raise `TouchpointError` because they explicitly request CDP-specific element sources.

### Dialog Auto-Dismiss

JavaScript dialogs (`alert`, `confirm`, `prompt`, `beforeunload`) freeze all CDP communication on a target until handled. Since the backend is synchronous, dialogs are auto-dismissed inline during `conn.send()` to prevent deadlocks:

- `alert` / `confirm` / `prompt` → accepted (prevents hang)
- `beforeunload` → rejected (prevents accidental navigation and data loss)

### Cross-Origin Iframes

Each cross-origin iframe becomes a separate CDP target. `_get_iframe_targets_for_page()` discovers them via `Page.getFrameTree` + `DOM.getFrameOwner`. Element discovery walks all iframe targets for a page and merges results.

---

## Input Providers

Each platform needs raw keyboard/mouse simulation as a **fallback** when native accessibility actions aren't available.

| Platform | Class | Mechanism | Dependency |
|----------|-------|-----------|------------|
| Linux / X11 | `XdotoolInput` | `xdotool` subprocess | System package |
| Windows | `SendInputProvider` | `ctypes` → `SendInput()` | None (stdlib) |
| macOS | `CGEventInput` | `pyobjc` → `CGEvent*` | `pyobjc-framework-ApplicationServices` |
| CDP | `CdpBackend` (internal) | `Input.dispatch*Event` | None (uses existing WebSocket) |

Key design decisions:
- InputProvider methods return `None` (fire-and-forget), not `bool`. There's no feedback from OS input events.
- InputProvider has **no knowledge of elements**. The public API bridges the gap (reads position from Element, passes coordinates to InputProvider).
- Fallback is controlled by `tp.configure(fallback_input=True)` — a set-and-forget flag.
- Linux uses xdotool directly (not pyautogui) because: zero pip deps, `--sync`/`--clearmodifiers` flags, subprocess isolation.
- Windows/macOS use native APIs directly (not pyautogui) because: the key-mapping table is the same work either way, and going native avoids pyautogui's PAUSE/FailSafe defaults and transitive dependencies.

---

## Screenshot Utility

- `tp.screenshot()` returns a `PIL.Image`. No encoding, no base64 — the agent developer decides what to do with it.
- Supports cropping to app window, specific window ID, element bounds (with optional padding), or a specific monitor.
- Uses `PIL.ImageGrab.grab()` on all platforms (X11, Win32 GDI, macOS screencapture). Wayland is not supported — requires XWayland.
- Captures screen framebuffer pixels — if the target is occluded by another window, the occluding pixels are captured.
- `tp.monitor_count()` returns the number of monitors (uses `screeninfo` if available, falls back to single-desktop assumption).

---

## Dependencies

All dependencies are installed automatically via `pip install touchpoint-py`. Platform-specific packages use `sys_platform` markers so only the right ones are installed.

| Package | What for | Platform |
|---------|----------|----------|
| `PyGObject` (gi) | AT-SPI2 via `gi.repository.Atspi` | Linux |
| `comtypes` | UIAutomation COM interface | Windows |
| `pyobjc-framework-ApplicationServices` | macOS AX + CGEvent input | macOS |
| `websocket-client` | CDP backend (sync WebSocket) | All |
| `rapidfuzz` | Fuzzy matching in `tp.find()` | All |
| `Pillow` | Screenshots | All |
| `screeninfo` | Monitor geometry for screenshots | All |
| `mcp[cli]` | MCP server | All |
| `xdotool` | Input simulation | Linux (system package, not pip) |

---

## Development Status

| Module | Status | Notes |
|--------|--------|-------|
| `core/types.py` | Complete | 73 Role values, 32 State values |
| `core/element.py` | Complete | 17-field dataclass |
| `core/window.py` | Complete | 9-field dataclass |
| `core/exceptions.py` | Complete | 3 exception classes |
| `backends/base.py` | Complete | Backend ABC (10 abstract + 7 concrete) + InputProvider ABC (9 methods) |
| `backends/linux/atspi.py` | Complete | Full AT-SPI2 implementation, live-tested |
| `backends/linux/x11/input.py` | Complete | XdotoolInput — full xdotool implementation |
| `backends/windows/input.py` | Complete | SendInputProvider — full ctypes implementation |
| `backends/windows/uia.py` | Complete | Full UIA implementation via comtypes |
| `backends/macos/ax.py` | Complete | macOS AX backend via pyobjc, live-tested |
| `backends/macos/input.py` | Complete | CGEventInput — CGEvent via pyobjc |
| `backends/cdp/cdp.py` | Complete | CDP Backend (Electron/Chromium apps via WebSocket) |
| `__init__.py` | Complete | Public API (discovery, find, wait, actions, input, fallback, configure) |
| `mcp/server.py` | Complete | MCP server — 19 tools, instructions-based guidance (touchpoint-mcp entry point) |
| `matching/matcher.py` | Complete | 4-stage pipeline (exact → contains → contains-words → fuzzy) |
| `format/formatter.py` | Complete | flat / json / tree formatters |
| `utils/screenshot.py` | Complete | Full-screen + region capture via Pillow |
| `utils/scale.py` | Complete | DPR scale factor: X11 xrdb, Win32 per-monitor, macOS NSScreen, user override via `configure()` |

---

## Future (Post-Launch)

### High Priority
- **Async CDP architecture** — The sync `websocket-client` inside `conn.send()` blocks on every CDP call, forces inline auto-dismiss of JS dialogs (no way to surface them to the agent), and serialises multi-tab operations. Moving to async WebSocket + event loop would enable: proper dialog queuing for agent inspection, concurrent multi-tab queries, and CDP event subscriptions (navigation, console, network).

### Medium Priority
- **Text selection tool** — Add `select_text(element_id, text)` to programmatically select text within elements. The agent-facing API is text-based (pass the substring to select — natural for agents, no offset counting); the Backend ABC exposes the low-level `select_text(element_id, start, end)` with character offsets; the public API bridges them by reading the element's text content, finding the substring, and calling the backend with offsets. All four backends have native support: AT-SPI2 has `Text.setSelection()` via D-Bus, UIA has `ITextRangeProvider.Select()` via `TextPattern` (currently unused), macOS AX has settable `AXSelectedTextRange`, and CDP can use `element.setSelectionRange()` for inputs or `Selection.setBaseAndExtent()` for contentEditable.
- **Window management tools** — Add `minimize_window`, `maximize_window`, `close_window`, `move_window`, `resize_window` convenience tools. All three platforms have native support: AT-SPI2 exposes window actions + `Component.set_position/set_size`, UIA has `WindowPattern` (Close/SetVisualState) + `TransformPattern` (Move/Resize), macOS AX has `AXMinimize`/`AXClose` buttons + settable `AXPosition`/`AXSize`, and CDP has `Browser.setWindowBounds` + `Target.closeTarget`. Currently agents must find and click title-bar buttons (which may not be in the a11y tree depending on toolkit/theme). Follows the same native-first, fallback pattern as click: try accessibility action first, fall back to OS-level APIs (e.g. `xdotool`/`wmctrl`/KWin D-Bus on Linux, `SetWindowPlacement` on Windows). Declare on Backend ABC, implement per-backend, expose via public API and MCP server.
- **Backend role/state inference parity** — CDP leads in role coverage (57/59) and now detects INDETERMINATE for tri-state checkboxes. AT-SPI2 has full state coverage (33/33) and added SWITCH, TIMER, METER role mappings (now 53/59 — remaining gaps are FEED, NOTE, ALERT_DIALOG, and landmark subtypes which AT-SPI2 doesn't distinguish). UIA states improved significantly (24/33 after adding INDETERMINATE, REQUIRED, BUSY, HORIZONTAL, VERTICAL, HAS_POPUP) but its **role coverage remains the weakest at 36/59** — many high-impact gaps (HEADING, LABEL, ALERT, landmarks) are closeable via `AriaRole`/`AriaProperties`. Remaining feasible UIA state gaps: RESIZABLE (Window pattern), INVALID (`IsDataValidForForm`), MULTISELECTABLE (Selection pattern). macOS AX sits at 46/59 roles, 26/33 states with feasible gaps in PRESSED, ACTIVE, MULTISELECTABLE via existing AX attributes. The biggest remaining cross-backend gap is **UIA role mappings** — agents on Windows see far more UNKNOWN roles than on other platforms.
- **Batch action tool** — MCP tool that accepts a list of actions and executes them sequentially server-side, reducing LLM round trips for known workflows (e.g. fill a row of cells: click → type → tab → type → tab → type). Action tools only (no discovery), stop on error, no variables/piping. Depends on user demand — `type_text` with `\t`/`\n` already covers the most common case.
- Wayland input backend (when X11/xdotool isn't available — `libei` / `xdg-desktop-portal` RemoteDesktop)

### Lower Priority
- **Documentation website** — full API reference (auto-generated from docstrings), configuration guide, Element/Window field reference, troubleshooting/FAQ, CDP setup details. mkdocs-material + GitHub Pages. README stays concise and links to the docs site.
- **Tooltip and notification visibility** — Tooltips (UIA ToolTip=50022, AT-SPI `"tool tip"`) and notifications (UIA Pane with WS_EX_NOACTIVATE, AT-SPI `"notification"`) are currently filtered from root discovery on Windows and Linux. Tooltips can be the only way to read icon-only toolbar labels; notifications may contain actionable buttons. macOS exposes tooltips via `AXHelp` attributes (no root change needed) and notifications partially via Notification Center's `AXWindows`. Could be noisy for `elements()` — consider gating behind a flag like `include_transient=True` or a `tp.configure()` knob. Post-launch, low urgency.
- Code organisation — `cdp.py` (3.6k lines) and `__init__.py` (2.3k lines) are large single files. Natural split points exist (WebSocket transport, role maps, DOM walker, port discovery for CDP; input provider, filtering, config for `__init__`). Worth splitting when the team grows.
- Element caching (per-app TTL cache for `get_elements()` results — deferred because lightweight mode, `max_depth`/`max_elements` limits, and stable IDs already minimise walk cost; cache adds staleness risk for marginal speedup)
- Optional semantic matching (embeddings-based, for agent devs who want it)

<p align="center">
  <h1 align="center">Touchpoint</h1>
  <p align="center">
    <strong>Give your AI agent eyes and hands on any desktop.</strong>
  </p>
  <p align="center">
    <a href="https://pypi.org/project/touchpoint-py/"><img src="https://img.shields.io/pypi/v/touchpoint-py?color=blue" alt="PyPI"></a>
    <a href="https://pypi.org/project/touchpoint-py/"><img src="https://img.shields.io/pypi/pyversions/touchpoint-py" alt="Python"></a>
    <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License"></a>
    <a href="#status"><img src="https://img.shields.io/badge/status-alpha-orange" alt="Alpha"></a>
    <br>
    <img src="https://img.shields.io/badge/Linux-FCC624?logo=linux&logoColor=black" alt="Linux">
    <img src="https://img.shields.io/badge/macOS-000000?logo=apple&logoColor=white" alt="macOS">
    <img src="https://img.shields.io/badge/Windows-0078D6?logo=windows&logoColor=white" alt="Windows">
  </p>
  <p align="center">
    <code>pip install touchpoint-py</code>
  </p>
</p>

<p align="center"><img src="docs/demo.gif" width="720" alt="Touchpoint demo — AI agent creates a formatted Excel table using Touchpoint"></p>
<p align="center"><em>AI agent researches data in Chrome, then creates a formatted Excel table — full task completed in ~12 minutes</em></p>

---

Touchpoint is a **cross-platform Python library** for reading and interacting with desktop UI through native accessibility APIs. One import, one API — works on Linux, macOS, and Windows, with built-in support for Chromium and Electron apps via CDP (Chrome DevTools Protocol).

Instead of scraping pixels or running vision models, Touchpoint reads the real accessibility tree — structured names, roles, states, and positions for every element on screen. Fast and reliable, with no model inference needed. Ships with an MCP server so LLM agents like Claude or Cursor can control any desktop app out of the box.

```python
import touchpoint as tp

elements = tp.find("Send", role=tp.Role.BUTTON, app="Slack")
tp.click(elements[0])
```

### Why Touchpoint?

| | Screenshot / vision | Browser automation | **Touchpoint** |
|---|---|---|---|
| Native desktop apps | ⚠️ inaccurate or slow | ❌ | ✅ structured access |
| Browsers | ⚠️ inaccurate or slow | ✅ | ✅ via CDP |
| Electron apps (Slack, VS Code, ...) | ⚠️ inaccurate or slow | ⚠️ web content only | ✅ native + web |
| Structured element data | ❌ needs OCR/vision models | ✅ web only | ✅ names, roles, states, positions |
| Works across Linux, macOS, Windows | ✅ | ✅ | ✅ |

---

## Table of Contents

- [Table of Contents](#table-of-contents)
- [Install](#install)
  - [Platform requirements](#platform-requirements)
- [Quick Start](#quick-start)
  - [Element IDs](#element-ids)
  - [Output formats](#output-formats)
- [MCP Server](#mcp-server)
  - [Tools](#tools)
  - [Client setup](#client-setup)
  - [Environment variables](#environment-variables)
- [Browser \& Electron Apps (CDP)](#browser--electron-apps-cdp)
  - [Setup](#setup)
- [API Reference](#api-reference)
  - [Discovery](#discovery)
  - [Search \& Wait](#search--wait)
  - [Actions](#actions)
  - [Input](#input)
  - [Screenshot \& Config](#screenshot--config)
- [Architecture](#architecture)
- [Configuration](#configuration)
- [Development](#development)
- [Status](#status)
  - [Known limitations](#known-limitations)
- [License](#license)

---

## Install

Requires **Python 3.10+**.

```bash
pip install touchpoint-py
```

Everything is included: your platform's native backend, CDP support for browsers and Electron apps, the MCP server, and screenshot capabilities. Platform-specific dependencies are installed automatically via pip environment markers.

### Platform requirements

| Platform | Backend | Requirement |
|----------|---------|-------------|
| **Linux** | AT-SPI2 | Install `xdotool` for input. Most desktops include `python3-gi` and `gir1.2-atspi-2.0` — install them if missing. |
| **Windows** | UI Automation | None — uses built-in COM APIs |
| **macOS** | Accessibility (AX) | Grant permission: System Settings → Privacy & Security → Accessibility |

---

## Quick Start

```python
import touchpoint as tp

# Discover
apps = tp.apps()                            # ["Firefox", "Slack", "Terminal", ...]
windows = tp.windows()                      # Window objects with title, position, size
all_els = tp.elements(app="Firefox", named_only=True)  # only elements with text labels

# Find
results = tp.find("Search", role=tp.Role.TEXT_FIELD, app="Firefox")

# Act
tp.set_value(results[0], "touchpoint python", replace=True)
tp.press_key("enter")
tp.hotkey("ctrl", "s")                      # keyboard shortcuts

# Wait for UI changes
tp.wait_for("results", app="Firefox", timeout=10)

# Screenshot
img = tp.screenshot()                       # full desktop → PIL.Image
img = tp.screenshot(app="Firefox")           # cropped to app window
```

### Element IDs

Every element has a unique ID like `atspi:1234:1:2.0` or `cdp:9222:TID:4`. Action functions accept either an `Element` object or a bare ID string — useful for storing references across steps:

```python
results = tp.find("Send", max_results=1)
element_id = results[0].id                  # "atspi:1234:1:5.2"

# later...
tp.click(element_id)                        # works with just the string
```

### Output formats

Control how results are returned:

```python
tp.elements(app="Slack", format="flat")     # one compact line per element (best for LLMs)
tp.elements(app="Slack", format="tree")     # indented parent/child hierarchy
tp.elements(app="Slack", format="json")     # full JSON with all fields
```

---

## MCP Server

Touchpoint ships an [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) server with **19 tools**, ready for any MCP-compatible client. Use it to let LLM agents like Claude, Cursor, or Copilot control your desktop.

### Tools

| Category | Tools |
|----------|-------|
| **Discovery** | `apps`, `windows`, `find`, `elements`, `get_element` |
| **Screenshot** | `screenshot` (returns image data the LLM can see) |
| **Actions** | `click` (left/right/double), `set_value`, `set_numeric_value`, `focus`, `action` |
| **Keyboard** | `type_text`, `press_key` (single key or combo) |
| **Mouse** | `mouse_move`, `scroll` |
| **Window** | `activate_window` |
| **Waiting** | `wait_for`, `wait_for_app`, `wait_for_window` |

The MCP server includes built-in instructions that teach LLM agents how to work effectively — the **orient → locate → act → verify** loop, how to use `find()`, and how to recover from errors.

```
         ┌──────────┐
    ┌───▶│  ORIENT  │  screenshot · apps · windows
    │    └────┬─────┘
    │         ▼
    │    ┌──────────┐
    │    │  LOCATE  │  find · elements · get_element
    │    └────┬─────┘
    │         ▼
    │    ┌──────────┐
    │    │   ACT    │  click · set_value · type_text · press_key
    │    └────┬─────┘
    │         ▼
    │    ┌──────────┐
    │    │  VERIFY  │───▶ Done ✅
    │    └────┬─────┘
    │         │ not yet
    └─────────┘
```

### Client setup

<details>
<summary><strong>Claude Desktop</strong></summary>

Config file location:
- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "touchpoint": {
      "command": "touchpoint-mcp"
    }
  }
}
```

If using a virtualenv, use the full path: `"/path/to/venv/bin/touchpoint-mcp"`

</details>

<details>
<summary><strong>VS Code / GitHub Copilot</strong></summary>

Add to `.vscode/mcp.json` in your workspace:

```json
{
  "servers": {
    "touchpoint": {
      "command": "touchpoint-mcp"
    }
  }
}
```

</details>

<details>
<summary><strong>Cursor</strong></summary>

Create or edit `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "touchpoint": {
      "command": "touchpoint-mcp"
    }
  }
}
```

</details>

<details>
<summary><strong>Windsurf</strong></summary>

Edit `~/.codeium/windsurf/mcp_config.json`:

```json
{
  "mcpServers": {
    "touchpoint": {
      "command": "touchpoint-mcp"
    }
  }
}
```

</details>

<details>
<summary><strong>Claude Code (CLI)</strong></summary>

```bash
claude mcp add touchpoint -- touchpoint-mcp
```

</details>

<details>
<summary><strong>OpenClaw</strong></summary>

Add to `mcpServers` in `~/.openclaw/openclaw.json`:

```json
{
  "mcpServers": {
    "touchpoint": {
      "command": "touchpoint-mcp"
    }
  }
}
```

</details>

### Environment variables

<details>
<summary>All optional — click to see available settings</summary>

<br>

| Variable | Example | Description |
|----------|---------|-------------|
| `TOUCHPOINT_CDP_DISCOVER` | `true` | Auto-discover CDP ports from running processes |
| `TOUCHPOINT_CDP_PORTS` | `{"Chrome": 9222}` | Explicit app-to-port mapping (JSON) |
| `TOUCHPOINT_CDP_APP` | `Google Chrome` | Single app name (pair with `_PORT`) |
| `TOUCHPOINT_CDP_PORT` | `9222` | Single port (pair with `_APP`) |
| `TOUCHPOINT_CDP_REFRESH_INTERVAL` | `5.0` | Seconds between CDP port scans |
| `TOUCHPOINT_SCALE_FACTOR` | `1.25` | Display scale override |
| `TOUCHPOINT_FUZZY_THRESHOLD` | `0.6` | Minimum match score for find() (0.0–1.0) |
| `TOUCHPOINT_FALLBACK_INPUT` | `true` | Use coordinate fallback when native actions fail |
| `TOUCHPOINT_MAX_ELEMENTS` | `5000` | Maximum elements per query |
| `TOUCHPOINT_MAX_DEPTH` | `10` | Default tree depth limit |

</details>

---

## Browser & Electron Apps (CDP)

Native accessibility APIs return limited data for Electron and Chromium apps (Slack, Discord, VS Code, etc.). Touchpoint's CDP backend connects via Chrome DevTools Protocol to get the full web content.

**Auto-discovery** is enabled by default — Touchpoint automatically finds running browsers and Electron apps that were launched with a debug port. No manual configuration needed beyond launching the app with the flag.

### Setup

1. **Launch the app with a debug port:**

```bash
# Linux
google-chrome --remote-debugging-port=9222 --user-data-dir=/tmp/tp-chrome

# macOS
open -na "Google Chrome" --args --remote-debugging-port=9222 --user-data-dir=/tmp/tp-chrome

# Windows
start chrome --remote-debugging-port=9222 --user-data-dir=%TEMP%\tp-chrome
```

2. **Configure Touchpoint:**

```python
import touchpoint as tp

tp.configure(cdp_discover=True)             # auto-discover from running processes
# or
tp.configure(cdp_ports={"Google Chrome": 9222})  # explicit mapping
```

3. **Control what you get with the `source` parameter:**

```python
tp.elements(app="Google Chrome", source="full")     # native chrome + web content (default)
tp.elements(app="Google Chrome", source="ax")       # web content only (CDP accessibility tree)
tp.elements(app="Google Chrome", source="native")   # native UI only (toolbar, tabs, menus)
tp.elements(app="Google Chrome", source="dom")      # DOM walker (catches what AX misses)
```

CDP results are merged with native backend results — you get the toolbar and window controls from AT-SPI2/UIA/AX, combined with the full web page content from CDP, in a single `elements()` call.

---

## API Reference

### Discovery

| Function | Description |
|----------|-------------|
| `tp.apps()` | List application names in the accessibility tree |
| `tp.windows()` | All windows with id, title, app, position, size, active state |
| `tp.elements(app, role, states, ...)` | UI elements, with filtering, tree mode, and formatting |
| `tp.element_at(x, y)` | Deepest element at screen coordinates |
| `tp.get_element(id)` | Fresh snapshot of a single element by ID |

### Search & Wait

| Function | Description |
|----------|-------------|
| `tp.find(query, app, role, ...)` | Search by name — 4-stage matching: exact → contains → word → fuzzy |
| `tp.wait_for(query, ...)` | Poll until elements appear (or disappear with `gone=True`) |
| `tp.wait_for_app(app, ...)` | Poll until an app appears or disappears |
| `tp.wait_for_window(title, ...)` | Poll until a window appears or disappears |

### Actions

| Function | Description |
|----------|-------------|
| `tp.click(element)` | Click via accessibility action, with coordinate fallback |
| `tp.double_click(element)` | Double-click |
| `tp.right_click(element)` | Right-click / context menu |
| `tp.set_value(element, text)` | Set text content (`replace=True` to clear first) |
| `tp.set_numeric_value(element, n)` | Set slider or spinbox value |
| `tp.focus(element)` | Move keyboard focus |
| `tp.action(element, name)` | Execute a raw accessibility action by name |
| `tp.activate_window(window)` | Bring a window to the foreground |

### Input

| Function | Description |
|----------|-------------|
| `tp.type_text(text)` | Type into the currently focused element |
| `tp.press_key(key)` | Press and release a key (`"enter"`, `"tab"`, `"escape"`) |
| `tp.hotkey(*keys)` | Key combination (`tp.hotkey("ctrl", "s")`) |
| `tp.click_at(x, y)` | Click at screen coordinates |
| `tp.double_click_at(x, y)` | Double-click at coordinates |
| `tp.right_click_at(x, y)` | Right-click at coordinates |
| `tp.mouse_move(x, y)` | Move the cursor |
| `tp.scroll(direction, amount)` | Scroll at current cursor position |

### Screenshot & Config

| Function | Description |
|----------|-------------|
| `tp.screenshot(app, element, ...)` | Full desktop or cropped to app/window/element/monitor |
| `tp.monitor_count()` | Number of connected monitors |
| `tp.configure(...)` | Set runtime options (see [Configuration](#configuration)) |

All action functions accept an `Element` object or a string ID. `elements()`, `find()`, and `get_element()` support `format="flat"`, `format="json"`, or `format="tree"` (elements only) to return pre-formatted strings instead of objects.

---

## Architecture

```
┌───────────────────────────────────────────────────────┐
│               import touchpoint as tp                 │
│  tp.find() · tp.click() · tp.screenshot() · ...       │
│                    (Public API)                       │
├─────────────────────────┬─────────────────────────────┤
│     Backend (ABC)       │    InputProvider (ABC)      │
├─────────────────────────┼─────────────────────────────┤
│  AT-SPI2     (Linux)    │  Xdotool       (X11)        │
│  UIA         (Windows)  │  SendInput     (Win32)      │
│  AX          (macOS)    │  CGEvent       (macOS)      │
│  CDP         (browsers) │                             │
├─────────────────────────┴─────────────────────────────┤
│  Utilities: formatter · matcher · screenshot · scale  │
└───────────────────────────────────────────────────────┘
```

**Two-layer design:**

- **Backend** reads the accessibility tree and runs structured actions (click, set_value, focus). Element-aware and reliable.
- **InputProvider** simulates raw keyboard and mouse input. Coordinate-based and element-blind. Used as an automatic fallback when a native accessibility action isn't available.

CDP runs alongside the platform backend. Their results are **merged**: native window chrome (toolbar, tabs, menus) from AT-SPI2/UIA/AX, plus full web content from CDP, unified under one API.

For detailed internals, see [ARCHITECTURE.md](ARCHITECTURE.md).

---

## Configuration

```python
tp.configure(
    fuzzy_threshold=0.6,          # minimum match score for find() (0.0–1.0)
    fallback_input=True,          # use InputProvider when native actions fail
    type_chunk_size=40,           # split long text into chunks for typing (0 = disable)
    max_elements=5000,            # max elements per query
    max_depth=10,                 # default tree depth limit
    scale_factor=None,            # display scale override (None = auto-detect)
    cdp_ports={"Chrome": 9222},   # explicit CDP port mapping
    cdp_discover=True,            # auto-discover CDP ports from running processes
    cdp_refresh_interval=5.0,     # seconds between CDP target scans
)
```

---

## Development

```bash
git clone https://github.com/Touchpoint-Labs/touchpoint.git
cd touchpoint
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

---

## Status

**Alpha** — fully functional and tested on all three platforms. The API may change before 1.0 based on user feedback.

| Platform | Backend | Input | CDP | Tests |
|----------|---------|-------|-----|-------|
| Linux (X11) | ✅ AT-SPI2 | ✅ xdotool | ✅ | ✅ |
| Windows | ✅ UIA | ✅ SendInput | ✅ | ✅ |
| macOS | ✅ AX | ✅ CGEvent | ✅ | ✅ |

### Known limitations

- **Wayland input** — The Linux InputProvider uses `xdotool`, which requires X11. On pure Wayland (no XWayland), keyboard/mouse simulation is unavailable. The accessibility tree and native actions still work.

- **Synchronous CDP** — CDP calls block on WebSocket responses. JavaScript dialogs (alert, confirm, prompt) are auto-dismissed to prevent deadlocks. An async rewrite is planned.

- **No browser navigation API** — Touchpoint doesn't have built-in URL navigation. Agents can navigate by interacting with UI elements directly: find the address bar, type a URL, press Enter.

---

## Roadmap

### High Priority
- **Async CDP architecture** — non-blocking WebSocket, proper dialog queuing, concurrent multi-tab queries

### Medium Priority
- **Text selection tool** — select text within an element by content (e.g. "select this word"), across all backends
- **Window management tools** — minimize, maximize, close, move, resize windows
- **Backend role/state parity** — close remaining role mapping gaps (especially UIA on Windows)
- **Batch action tool** — execute a sequence of actions in one call to reduce LLM round trips
- **Wayland input backend** — `libei` / `xdg-desktop-portal` RemoteDesktop when X11 isn't available

### Lower Priority
- Tooltip and notification visibility
- Code organisation (split large files)
- Element caching

---

## License

[MIT](LICENSE)

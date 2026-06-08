# Changelog

All notable changes to Touchpoint will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] — 2026-06-07

### Added
- **No-vision MCP mode.** Setting `TOUCHPOINT_MODE=no-vision` lets agents
  drive the desktop from a compact structured `snapshot()` of the active
  window — plus `diff_snapshot()` and automatic verify flags on actions —
  instead of screenshots. Any model can now control the desktop, including
  local models with no vision capability.
- **Cross-platform window management.** New APIs `tp.minimize_window()`,
  `tp.fullscreen_window()`, `tp.close_window()`, `tp.move_window()`, and
  `tp.resize_window()` (with matching MCP tools) let agents arrange and
  control application windows on Linux, Windows, and macOS.
- **`select_text` now works everywhere.** Native structured text selection
  is complete across all backends — Linux, Windows, macOS, and web/Electron
  — so agents can select substrings without mouse drags or triple-clicks on
  any platform.
- **Read element text directly.** The `read_text` MCP tool and
  `tp.get_text_content()` return the full text of an element or container
  (article, document body, terminal) verbatim — no screenshot OCR needed.
- **`tp.diagnostics()`** (and a matching MCP tool) reports the health of
  backends, input, CDP targets, timeouts, and optional dependencies, making
  it easy to confirm a working setup or troubleshoot a misconfigured one.
- **Role-classification sets.** `INTERACTIVE_ROLES`, `CONTAINER_ROLES`, and
  `STRUCTURAL_ROLES` are exported for grouping elements by kind (e.g. what an
  agent can act on vs. layout containers).

### Changed
- **Broader role and state coverage on Windows and macOS.** Both backends
  now recognize many more element kinds (headings, landmarks, form roles,
  labels, figures, notes, meters, split buttons, and more) and additional
  states such as pressed, active, invalid, multi-selectable, and resizable —
  bringing Windows and macOS much closer to parity with Linux.
- **Thread-safe, more reliable automation.** Public API calls and complete
  MCP tool workflows now serialize access to shared backend and session
  state, so concurrent or worker-thread usage no longer corrupts state or
  stalls.
- **macOS responsiveness hardening.** macOS now uses a configurable
  messaging timeout (`ax_messaging_timeout` / `TOUCHPOINT_AX_MESSAGING_TIMEOUT`,
  default 1 second) applied throughout element traversal, so an unresponsive
  app is detected and skipped instead of hanging the whole session.
- **Faster element search on large Windows desktops.** Windows searches now
  skip obvious leaf controls, use direct control-type filters for exact role
  queries, and fully read only matched elements, reducing overhead on big
  accessibility trees.
- **`tp.configure()` is now also a getter** — calling it with no arguments
  returns a copy of the current configuration.

### Removed
- **The `elements` MCP tool** has been replaced by `snapshot()`, which returns
  a compact structured tree of the active window (in both vision and no-vision
  modes). The `tp.elements()` Python API is unchanged.

### Fixed
- **CDP windows now support OS-level window management.** Minimize, fullscreen,
  close, move, and resize on a browser/Electron page are routed to the
  underlying native OS window (resolved by owning process), with a clear error
  only when no native window can be found. (Window *activation* was already
  supported.)
- **GTK4 apps are now controllable.** Some GTK4 apps expose elements under
  UUID-style paths that were previously misread as malformed, so they appeared
  in listings but every action failed. They now work correctly.
- **Consistent errors for malformed element IDs.** All backends now raise a
  clear `ValueError` when given a structurally invalid element ID, instead of
  some platforms silently returning nothing.
- **Reliable Windows text selection.** Text selection reads and selects from a
  single source to avoid offset drift, and falls back to a native message for
  classic edit controls that don't expose the modern text pattern. Multiline
  edit fields are no longer mislabeled as single-line, and ordinary controls
  are no longer falsely marked invalid.
- **`wait_for` correctness.** `wait_for(..., wait_for_new=True)` now records a
  correct baseline of existing matches (previously it could behave as a no-op),
  `mode` is validated immediately, and repeated queries are de-duplicated within
  each polling cycle to avoid redundant tree scans.
- **Multi-monitor screenshots.** Captures of elements on monitors positioned
  above or to the left of the primary display now expand correctly instead of
  being clipped back to the primary display's origin.

### Platform support after 0.3.0

| Feature | Linux (AT-SPI2) | Windows (UIA) | macOS (AX) | Web / Electron (CDP) |
|---|---|---|---|---|
| `select_text` | ✅ Full | ✅ Full | ✅ Full | ✅ Full |
| Window management | ✅ X11 + `wmctrl` | ✅ Full | ✅ Full | 🚧 Routed to OS window |

## [0.2.0] — 2026-04-11

### Added
- **`tp.select_text(element, text, occurrence=1)`** — programmatic text
  selection on input fields and contentEditable elements. Finds the
  substring inside the element's text content and selects it natively
  (no mouse drag, no triple-click). Pass `occurrence=2` to select the
  second match, etc.
- New abstract method `Backend.select_text(element_id, start, end)` on
  the backend ABC. Backends that don't support it raise a clear
  `ActionFailedError` instead of crashing.
- **`select_text` MCP tool** in the Touchpoint MCP server, bringing the
  total to 20 tools.
- Linux (AT-SPI2) and web/Electron (CDP) backends fully implement
  `select_text`. The CDP implementation handles `<input>` elements via
  `setSelectionRange`, plus contentEditable elements via the Selection
  API with TreeWalker for multi-node selections.

### Fixed
- **AT-SPI2 hang on LibreOffice Calc.** Calc reports `INT_MAX`
  (2³¹ − 1) children for spreadsheet tables, which previously caused
  Touchpoint to attempt walking 2 billion accessibility nodes.
  Children-per-node are now capped at 500 across all walk paths
  (`_collect_flat`, `_collect_light_flat`, `_to_element_tree`, and the
  geometry descent path used by `tp.element_at()`).
- **CDP click on zero-geometry elements.** Some elements (custom CSS,
  hidden-but-clickable patterns) report an empty bounding box, which
  used to make `tp.click()` silently no-op. We now fall back to a
  JavaScript `element.click()` invocation when geometry is unavailable,
  with proper object cleanup in the failure path.

### Platform support for `select_text`

| Platform | Status |
|---|---|
| Linux (AT-SPI2) | ✅ Full |
| Web / Electron (CDP) | ✅ Full |
| Windows (UIA) | 🚧 Stub — raises `ActionFailedError("not yet implemented")` |
| macOS (AX) | 🚧 Stub — raises `ActionFailedError("not yet implemented")` |

Windows and macOS implementations are in progress.

## [0.1.1] — 2026-03

Initial public release. See the [v0.1.1
tag](https://github.com/Touchpoint-Labs/touchpoint/releases/tag/v0.1.1)
for details.

[0.3.0]: https://github.com/Touchpoint-Labs/touchpoint/releases/tag/v0.3.0
[0.2.0]: https://github.com/Touchpoint-Labs/touchpoint/releases/tag/v0.2.0
[0.1.1]: https://github.com/Touchpoint-Labs/touchpoint/releases/tag/v0.1.1

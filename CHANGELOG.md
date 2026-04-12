# Changelog

All notable changes to Touchpoint will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.2.0]: https://github.com/Touchpoint-Labs/touchpoint/releases/tag/v0.2.0
[0.1.1]: https://github.com/Touchpoint-Labs/touchpoint/releases/tag/v0.1.1

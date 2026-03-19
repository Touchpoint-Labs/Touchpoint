"""Unified types for Touchpoint.

Defines :class:`Role` and :class:`State` enums that every backend
(AT-SPI2, CDP, Windows UIA, macOS AX) maps its native types to.
Both use the ``str`` mixin so values serialize cleanly to JSON
without needing ``.value``.

Example::

    >>> from touchpoint.core.types import Role, State
    >>> Role.BUTTON
    <Role.BUTTON: 'button'>
    >>> str(Role.BUTTON)
    'button'
    >>> Role.BUTTON == "button"
    True
"""

from enum import Enum


class Role(str, Enum):
    """Unified UI element roles across all backends.

    Each backend translates its native role identifiers to one of these
    values.  For example, AT-SPI2's ``ROLE_PUSH_BUTTON``, Windows UIA's
    ``ButtonControlType``, macOS's ``AXButton``, and CDP's ``button``
    all map to :attr:`Role.BUTTON`.

    Roles that don't map to any known value become :attr:`Role.UNKNOWN`;
    the original string is preserved in ``Element.raw_role``.

    Inherits from ``str`` so comparisons like ``role == "button"`` and
    JSON serialisation work without ``.value``.
    """

    APPLICATION = "application"
    WINDOW = "window"
    DIALOG = "dialog"
    PANEL = "panel"
    FRAME = "frame"

    # Interactive
    BUTTON = "button"
    TOGGLE_BUTTON = "toggle_button"
    CHECK_BOX = "check_box"
    RADIO_BUTTON = "radio_button"
    LINK = "link"

    # Text
    TEXT_FIELD = "text_field"
    TEXT = "text"
    LABEL = "label"
    HEADING = "heading"
    PARAGRAPH = "paragraph"

    # Menus
    MENU_BAR = "menu_bar"
    MENU = "menu"
    MENU_ITEM = "menu_item"

    # Lists & Trees
    LIST = "list"
    LIST_ITEM = "list_item"
    TREE = "tree"
    TREE_ITEM = "tree_item"

    # Tables
    TABLE = "table"
    TABLE_ROW = "table_row"
    TABLE_CELL = "table_cell"
    TABLE_COLUMN_HEADER = "table_column_header"
    TABLE_ROW_HEADER = "table_row_header"

    # Tabs
    TAB_LIST = "tab_list"
    TAB = "tab"

    # Selection & Range
    COMBO_BOX = "combo_box"
    SLIDER = "slider"
    SPIN_BUTTON = "spin_button"
    SCROLL_BAR = "scroll_bar"
    PROGRESS_BAR = "progress_bar"

    # Toolbars & Status
    TOOLBAR = "toolbar"
    STATUS_BAR = "status_bar"
    SEPARATOR = "separator"

    # Media & Content
    IMAGE = "image"
    ICON = "icon"
    DOCUMENT = "document"
    CANVAS = "canvas"
    FIGURE = "figure"
    MATH = "math"

    # Containers
    GROUP = "group"
    SECTION = "section"
    FORM = "form"
    GRID = "grid"
    GRID_CELL = "grid_cell"

    # Alerts & Live regions
    ALERT = "alert"
    ALERT_DIALOG = "alert_dialog"
    NOTIFICATION = "notification"
    LOG = "log"
    TIMER = "timer"
    METER = "meter"
    NOTE = "note"
    FEED = "feed"

    # Tooltips & Popups
    TOOLTIP = "tooltip"
    SPLIT_BUTTON = "split_button"

    # Toggles & Password
    SWITCH = "switch"
    PASSWORD_TEXT = "password_text"

    # Menu variants
    CHECK_MENU_ITEM = "check_menu_item"
    RADIO_MENU_ITEM = "radio_menu_item"

    # Landmarks (web / ARIA)
    LANDMARK = "landmark"
    NAVIGATION = "navigation"
    BANNER = "banner"
    SEARCH = "search"
    CONTENT_INFO = "content_info"

    # Headers & Footers
    HEADER = "header"
    FOOTER = "footer"

    # Tab content
    TAB_PANEL = "tab_panel"

    # Window chrome
    TITLE_BAR = "title_bar"

    # Content types
    ARTICLE = "article"

    # Catch-all
    UNKNOWN = "unknown"


class State(str, Enum):
    """Unified UI element states across all backends.

    An element can have multiple states simultaneously — for example
    a focused text field might have ``[VISIBLE, ENABLED, FOCUSABLE,
    FOCUSED, EDITABLE, SINGLE_LINE]``.

    Each backend translates its native state flags to a ``list[State]``.
    For instance, AT-SPI2's ``STATE_SENSITIVE`` maps to :attr:`State.SENSITIVE`,
    Windows UIA's ``IsEnabled`` maps to :attr:`State.ENABLED`, and macOS's
    ``AXEnabled`` does the same.

    Inherits from ``str`` so comparisons like ``state == "focused"`` and
    JSON serialisation work without ``.value``.
    """

    # Visibility
    VISIBLE = "visible"
    SHOWING = "showing"

    # Interaction
    ENABLED = "enabled"
    SENSITIVE = "sensitive"
    FOCUSABLE = "focusable"
    FOCUSED = "focused"
    CLICKABLE = "clickable"

    # Selection
    SELECTED = "selected"
    SELECTABLE = "selectable"
    CHECKED = "checked"
    PRESSED = "pressed"

    # Expansion
    EXPANDABLE = "expandable"
    EXPANDED = "expanded"
    COLLAPSED = "collapsed"

    # Text
    EDITABLE = "editable"
    READ_ONLY = "read_only"
    MULTI_LINE = "multi_line"
    SINGLE_LINE = "single_line"

    # Window/Dialog
    MODAL = "modal"
    ACTIVE = "active"
    RESIZABLE = "resizable"

    # Validation
    REQUIRED = "required"
    INVALID = "invalid"

    # Orientation
    HORIZONTAL = "horizontal"
    VERTICAL = "vertical"

    # Async / Live
    BUSY = "busy"
    INDETERMINATE = "indeterminate"

    # Popups
    HAS_POPUP = "has_popup"

    # Multi-select
    MULTISELECTABLE = "multiselectable"

    # Off-screen / Stale
    OFFSCREEN = "offscreen"
    DEFUNCT = "defunct"

    # Link history
    VISITED = "visited"

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


# ---------------------------------------------------------------------------
# Role classification sets
# ---------------------------------------------------------------------------
#
# Consumers (currently the no-vision MCP snapshot renderer) use these to
# decide what deserves a line in the tree, what adds context, and what is
# anonymous scaffolding safe to skip.  Kept as data here so any future
# filtering logic can reuse the same classification.

INTERACTIVE_ROLES: frozenset[Role] = frozenset({
    Role.BUTTON,
    Role.TOGGLE_BUTTON,
    Role.CHECK_BOX,
    Role.RADIO_BUTTON,
    Role.LINK,
    Role.TEXT_FIELD,
    Role.PASSWORD_TEXT,
    Role.COMBO_BOX,
    Role.SLIDER,
    Role.SPIN_BUTTON,
    Role.SWITCH,
    Role.SPLIT_BUTTON,
    Role.MENU_ITEM,
    Role.CHECK_MENU_ITEM,
    Role.RADIO_MENU_ITEM,
    Role.TAB,
    Role.LIST_ITEM,
    Role.TREE_ITEM,
    Role.TABLE_CELL,
    Role.TABLE_ROW,
    Role.GRID_CELL,
    # Column/row headers are clickable (sort, select row).
    Role.TABLE_COLUMN_HEADER,
    Role.TABLE_ROW_HEADER,
    # Icons are directly clickable in file managers and launchers.
    Role.ICON,
})
"""Elements an agent can act on directly."""


CONTAINER_ROLES: frozenset[Role] = frozenset({
    Role.DIALOG,
    Role.ALERT_DIALOG,
    Role.ALERT,
    Role.NOTIFICATION,
    Role.MENU,
    Role.MENU_BAR,
    Role.LIST,
    Role.TREE,
    Role.TABLE,
    Role.GRID,
    Role.FORM,
    Role.TAB_LIST,
    Role.TAB_PANEL,
    Role.NAVIGATION,
    Role.BANNER,
    Role.SEARCH,
    Role.TOOLBAR,
    Role.STATUS_BAR,
    Role.HEADER,
    Role.FOOTER,
    Role.ARTICLE,
    Role.TOOLTIP,
    # Structural context an agent needs to orient itself.
    Role.HEADING,      # document / page section skeleton
    Role.LABEL,        # describes adjacent form fields
    Role.PROGRESS_BAR, # loading / progress state signal
    Role.LANDMARK,     # generic ARIA landmark (<main>, etc.)
    Role.CONTENT_INFO, # ARIA contentinfo (<footer role="contentinfo">)
    Role.LOG,          # live log regions
    Role.DOCUMENT,     # document body (LibreOffice Writer, word processors)
    Role.FIGURE,       # <figure> / semantic illustration block
    Role.TIMER,        # countdown / elapsed-time indicator
    Role.METER,        # scalar measurement (like progress_bar)
    Role.NOTE,         # aside / annotation
    Role.FEED,         # paginated content stream
})
"""Semantic containers that add context when preserved in a tree view."""


STRUCTURAL_ROLES: frozenset[Role] = frozenset({
    Role.PANEL,
    Role.GROUP,
    Role.SECTION,
    Role.FRAME,
    Role.SEPARATOR,
    Role.SCROLL_BAR,
    Role.TITLE_BAR,    # window chrome (min/max/close buttons inside)
    # Opaque visual leaves — shown when named, invisible when unnamed.
    # INTERACTIVE has no name guard, so decorative unnamed instances
    # would flood snapshots on image-heavy pages.
    Role.IMAGE,
    Role.CANVAS,
    Role.MATH,
})
"""Anonymous wrappers whose children can be reparented during rendering."""


# Roles intentionally absent from all three sets above.
# TEXT and PARAGRAPH are left out because CDP maps sub-word inline nodes
# (inlineTextBox, lineBreak, <em>, <strong>, <code>, <p>, …) to them,
# which would flood web-page snapshots if promoted to any visible set.
# APPLICATION and WINDOW are window roots, handled by windows() not elements().
_UNCLASSIFIED_ROLES: frozenset[Role] = frozenset({
    Role.UNKNOWN,
    Role.APPLICATION,
    Role.WINDOW,
    Role.TEXT,
    Role.PARAGRAPH,
})
